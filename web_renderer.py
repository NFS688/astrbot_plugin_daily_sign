from __future__ import annotations

import asyncio

from astrbot import logger

from .constants import LOG_PREFIX, RENDER_DEVICE_SCALE_FACTOR

_playwright = None
_browser = None
_browser_lock = asyncio.Lock()


async def _init_browser_locked():
    global _playwright, _browser
    if _browser is not None:
        return _browser

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        raise RuntimeError(
            "渲染功能依赖 Playwright，请先安装 playwright 及浏览器二进制。"
        ) from e

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception:
        await playwright.stop()
        raise

    _playwright = playwright
    _browser = browser
    logger.info(f"{LOG_PREFIX} 浏览器内核已启动")
    return _browser


async def _shutdown_browser_locked() -> None:
    global _playwright, _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None

    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


async def _refresh_browser(failed_browser=None) -> None:
    async with _browser_lock:
        if failed_browser is not None and _browser is not None and _browser is not failed_browser:
            return
        await _shutdown_browser_locked()
        await _init_browser_locked()


async def _get_browser():
    if _browser is not None:
        return _browser

    async with _browser_lock:
        return await _init_browser_locked()


async def render_html_to_png(
    html_content: str,
    width: int,
    height: int,
    selector: str = "#card",
) -> bytes:
    last_exc: Exception | None = None

    for attempt in range(2):
        browser = None
        try:
            browser = await _get_browser()
            page = await browser.new_page(
                viewport={"width": int(width), "height": int(height)},
                device_scale_factor=RENDER_DEVICE_SCALE_FACTOR,
            )
            try:
                await page.set_content(html_content, wait_until="domcontentloaded")
                locator = page.locator(selector)
                if await locator.count() > 0:
                    return await locator.first.screenshot(type="png")
                return await page.screenshot(type="png", full_page=True)
            finally:
                await page.close()
        except Exception as e:
            last_exc = e
            if attempt == 0:
                await _refresh_browser(browser)
                continue
            raise

    assert last_exc is not None
    raise last_exc


async def init_web_renderer() -> None:
    try:
        await _get_browser()
    except Exception as e:
        logger.warning(f"{LOG_PREFIX} 浏览器预热失败: {e}")


async def shutdown_web_renderer() -> None:
    async with _browser_lock:
        await _shutdown_browser_locked()
