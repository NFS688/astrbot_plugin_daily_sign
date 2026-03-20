from __future__ import annotations

import asyncio
import base64
import datetime
import html
import json
import os
import random
import re
import traceback
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import aiohttp

from astrbot import logger
from astrbot.core.config.default import VERSION

from .constants import (
    BG_URL,
    DEFAULT_NEXT_SCORE,
    DEFAULT_NICKNAME,
    IMAGE_DIR,
    LOCAL_BG_DIR,
    LOG_PREFIX,
    PLUGIN_VERSION,
    RANKING_BASE_CARD_HEIGHT,
    RANKING_CARD_WIDTH,
    RANKING_MIN_CARD_HEIGHT,
    RANKING_ROW_HEIGHT,
    SIGN_CARD_HEIGHT,
    SIGN_CARD_WIDTH,
)
from .html_builder import (
    build_avatar_html,
    build_ranking_card_html,
    build_ranking_row_html,
    build_ranking_top_item_html,
    build_sign_card_html,
)
from .web_renderer import (
    init_web_renderer,
    render_html_to_png,
    shutdown_web_renderer,
)

_draw_initialized = False
DEFAULT_WALLET_LABEL = "余额"
_CACHE_DATE_FORMAT = "%Y-%m-%d"
_BACKGROUND_CACHE_PATTERN = re.compile(
    r"^background-[0-9A-Za-z_-]+-(\d{4}-\d{2}-\d{2})$"
)
_SIGN_CACHE_PATTERN = re.compile(r"^[0-9A-Za-z_-]+-(\d{4}-\d{2}-\d{2})$")


def _sanitize_path_token(value: object, default: str = "unknown") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    cleaned = re.sub(r"[^0-9A-Za-z_-]", "_", text)
    cleaned = cleaned.strip("_")
    return cleaned or default


def _join_image_path(filename: str) -> Path:
    image_dir_abs = IMAGE_DIR.resolve()
    path = (IMAGE_DIR / filename).resolve()
    if not str(path).startswith(str(image_dir_abs) + os.sep):
        raise ValueError(f"非法图片路径: {filename}")
    return path


def _build_background_path(userid: object, date_text: object) -> Path:
    safe_uid = _sanitize_path_token(userid)
    safe_date = _sanitize_path_token(date_text)
    return _join_image_path(f"background-{safe_uid}-{safe_date}.png")


def _build_sign_cache_path(userid: object, date_text: object) -> Path:
    safe_uid = _sanitize_path_token(userid)
    safe_date = _sanitize_path_token(date_text)
    return _join_image_path(f"{safe_uid}-{safe_date}.png")


def _save_content(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _read_content(path: Path) -> bytes:
    return path.read_bytes()


def _extract_cache_date(path: Path) -> datetime.date | None:
    stem = path.stem
    for pattern in (_BACKGROUND_CACHE_PATTERN, _SIGN_CACHE_PATTERN):
        matched = pattern.fullmatch(stem)
        if matched is None:
            continue
        try:
            return datetime.datetime.strptime(
                matched.group(1),
                _CACHE_DATE_FORMAT,
            ).date()
        except Exception:
            return None
    return None


def _cleanup_sign_cache_files(retention_days: int) -> int:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    keep_days = max(0, int(retention_days))
    today = datetime.date.today()
    cutoff_date = (
        today - datetime.timedelta(days=keep_days - 1)
        if keep_days > 0
        else today + datetime.timedelta(days=1)
    )

    removed = 0
    for path in IMAGE_DIR.iterdir():
        if not path.is_file():
            continue

        cache_date = _extract_cache_date(path)
        if cache_date is None:
            continue
        if keep_days > 0 and cache_date >= cutoff_date:
            continue

        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue

    return removed


def _detect_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _bytes_to_data_url(data: bytes, mime: str | None = None) -> str:
    if not data:
        return ""
    mime_type = mime or _detect_mime(data)
    encoded = base64.b64encode(data).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


async def init_draw() -> None:
    global _draw_initialized
    if _draw_initialized:
        return

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_BG_DIR.mkdir(parents=True, exist_ok=True)
    await init_web_renderer()
    _draw_initialized = True


async def shutdown_draw() -> None:
    global _draw_initialized
    await shutdown_web_renderer()
    _draw_initialized = False


async def cleanup_sign_cache(retention_days: int) -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _cleanup_sign_cache_files,
        int(retention_days),
    )


async def get_background(userid: object, time_text: object) -> bytes | None:
    path = _build_background_path(userid, time_text)
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _read_content, path)
    except Exception as e:
        logger.warning(f"{LOG_PREFIX} 获取签到背景失败: {e}")
        return None


class ImageGen:
    def __init__(
        self,
        userdata: dict,
        nickname: str | None = DEFAULT_NICKNAME,
        wallet_name: str | None = DEFAULT_WALLET_LABEL,
        add_coins: int | None = 0,
        add_impression: float | None = 0,
        next_score: float | None = DEFAULT_NEXT_SCORE,
        use_local_bg: bool | None = False,
    ) -> None:
        self.userid = userdata.get("user_id")
        self.nickname = (
            str(nickname or DEFAULT_NICKNAME).replace("\r", " ").replace("\n", " ")
        )
        self.nickname = self.nickname.strip() or DEFAULT_NICKNAME

        self.wallet_name = str(wallet_name or DEFAULT_WALLET_LABEL)
        self.impression = userdata.get("impression")
        self.coins = userdata.get("coins")
        self.add_impression = add_impression
        self.add_coins = add_coins
        self.last_sign = userdata.get("last_sign")
        self.total_days = userdata.get("total_days", 0) or 0
        self.continuous_days = userdata.get("continuous_days", 0) or 0
        self.level = userdata.get("level")
        self.next_score = float(next_score or DEFAULT_NEXT_SCORE)
        self.use_local_bg = bool(use_local_bg)

        self.today = datetime.datetime.now().strftime("%Y-%m-%d")

        self.avatar_data: bytes | None = None
        self.bg_data: bytes | None = None
        self.saying_text: str = ""

    async def _get_saying(self, session: aiohttp.ClientSession) -> str:
        api = "https://uapis.cn/api/v1/saying"
        fallback = "心有猛虎，细嗅蔷薇。"

        try:
            async with session.get(api, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return str(data.get("text") or fallback)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 获取一言失败: {e}")

        return fallback

    async def _prepare_resources(self) -> None:
        async with aiohttp.ClientSession() as session:
            if self.use_local_bg:
                self.bg_data = await self._get_bg_local()
            else:
                self.bg_data = await self._get_bg_remote(session)

            self.avatar_data = await self._get_avatar(session)
            self.saying_text = await self._get_saying(session)

        if not self.bg_data:
            raise RuntimeError("没有可用的背景图片")

    async def _get_bg_local(self) -> bytes | None:
        try:
            bg_path = _build_background_path(self.userid, self.today)
            img_files = [
                file_path
                for file_path in LOCAL_BG_DIR.iterdir()
                if file_path.is_file()
                and file_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            ]
            if not img_files:
                return None

            chosen_img = random.choice(img_files)
            loop = asyncio.get_running_loop()
            bg_data = await loop.run_in_executor(None, _read_content, chosen_img)
            await loop.run_in_executor(None, _save_content, bg_path, bg_data)
            return bg_data
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 获取本地背景失败: {e}")
            return None

    async def _get_bg_remote(self, session: aiohttp.ClientSession) -> bytes | None:
        try:
            bg_path = _build_background_path(self.userid, self.today)
            async with session.get(BG_URL, timeout=30) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"{LOG_PREFIX} 背景接口请求失败，状态码: {resp.status}"
                    )
                    return None

                payload = await resp.read()
                response_dict = json.loads(payload)
                image_url = response_dict.get("data")
                if not image_url:
                    return None

                async with session.get(image_url, timeout=30) as img_resp:
                    if img_resp.status != 200:
                        logger.warning(
                            f"{LOG_PREFIX} 背景图片下载失败，状态码: {img_resp.status}"
                        )
                        return None

                    bg_data = await img_resp.read()
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, _save_content, bg_path, bg_data)
                    return bg_data
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 获取远程背景失败: {e}")
            return None

    async def _get_avatar(self, session: aiohttp.ClientSession) -> bytes | None:
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={self.userid}&s=640"
        try:
            async with session.get(avatar_url, timeout=20) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 获取头像失败: {e}")
        return None

    def _get_hour_word(self) -> str:
        hour = datetime.datetime.now().hour
        if 6 <= hour < 11:
            return "早上好"
        if 11 <= hour < 14:
            return "中午好"
        if 14 <= hour < 19:
            return "下午好"
        if 19 <= hour < 24:
            return "晚上好"
        return "凌晨好"

    @staticmethod
    def _safe_level(level: Any) -> int:
        try:
            return max(1, min(8, int(level or 1)))
        except Exception:
            return 1

    def _get_streak_bonus_percent(self) -> int:
        streak = int(self.continuous_days or 0)
        if streak >= 7:
            return 15
        if streak >= 3:
            return 10
        return 0

    @staticmethod
    def _to_decimal(value: Any, default: str = "0") -> Decimal:
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal(default)

    async def _image_cache(self) -> bytes | None:
        try:
            image_path = _build_sign_cache_path(self.userid, self.today)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _read_content, image_path)
        except FileNotFoundError:
            logger.debug(f"{LOG_PREFIX} 未找到签到图片缓存: {self.userid}-{self.today}")
            return None
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取签到图片缓存失败: {e}")
            return None

    def _build_html(self) -> str:
        bg_url = _bytes_to_data_url(self.bg_data or b"")
        avatar_url = _bytes_to_data_url(self.avatar_data or b"")
        current_level = self._safe_level(self.level)

        impression = self._to_decimal(self.impression or 0).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        target_score = self._to_decimal(current_level * self.next_score, "0.01")
        if target_score <= 0:
            target_score = Decimal("0.01")
        target_score = target_score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        ratio = float(max(Decimal("0"), min(Decimal("1"), impression / target_score)))
        progress_percent = ratio * 100.0

        safe_nickname = html.escape(self.nickname)
        safe_hour_word = html.escape(self._get_hour_word())
        safe_coin = html.escape(f"{self.wallet_name} + {int(self.add_coins or 0)}")
        safe_total = html.escape(f"你有 {int(self.coins or 0)} 枚{self.wallet_name}")
        safe_level = html.escape(f"Level {current_level}")
        safe_bonus = html.escape(f"连签加成: {self._get_streak_bonus_percent()}%")
        safe_date = html.escape(str(self.last_sign or self.today))
        safe_saying = html.escape(self.saying_text)
        safe_footer = html.escape(
            f"Created By AstrBot {VERSION} & Daily Sign Plugin {PLUGIN_VERSION}"
        )

        fallback_char = html.escape(safe_nickname[:1] or "?")
        avatar_html = build_avatar_html(
            css_class="avatar",
            avatar_url=avatar_url,
            fallback_text=fallback_char,
        )

        return build_sign_card_html(
            bg_url=bg_url,
            avatar_html=avatar_html,
            safe_nickname=safe_nickname,
            safe_hour_word=safe_hour_word,
            safe_coin=safe_coin,
            safe_level=safe_level,
            safe_total=safe_total,
            total_days=int(self.total_days or 0),
            continuous_days=int(self.continuous_days or 0),
            impression_text=f"{impression:.2f}",
            target_score_text=f"{target_score:.2f}",
            progress_percent=progress_percent,
            safe_bonus=safe_bonus,
            safe_date=safe_date,
            safe_saying=safe_saying,
            safe_footer=safe_footer,
        )

    async def _draw(self) -> bytes | None:
        try:
            await self._prepare_resources()
            html_content = self._build_html()
            image_data = await render_html_to_png(
                html_content=html_content,
                width=SIGN_CARD_WIDTH,
                height=SIGN_CARD_HEIGHT,
                selector="#card",
            )
            image_path = _build_sign_cache_path(self.userid, self.today)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _save_content, image_path, image_data)
            return image_data
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 签到图片生成失败: {e}")
            logger.error(f"{LOG_PREFIX}\n{traceback.format_exc()}")
            return None


@dataclass
class RankingEntry:
    rank: int
    user_id: str
    nickname: str
    attitude: str
    impression_text: str
    progress_text: str
    progress_ratio: float


class ImpressionRankingImageGen:
    def __init__(
        self,
        entries: list[RankingEntry],
        title: str = "好感度排行",
        max_impression: float = 200.0,
        updated_text: str = "",
    ) -> None:
        self.entries = list(entries or [])
        self.title = str(title or "好感度排行")
        self.max_impression = float(max(0.0, max_impression))
        self.updated_text = str(updated_text or "")
        self.avatar_map: dict[str, bytes] = {}

    async def _prepare_avatars(self) -> None:
        user_ids: list[str] = []
        seen: set[str] = set()
        for item in self.entries:
            uid = str(getattr(item, "user_id", "") or "").strip()
            if not uid or uid in seen or not uid.isdigit():
                continue
            seen.add(uid)
            user_ids.append(uid)

        if not user_ids:
            return

        semaphore = asyncio.Semaphore(8)
        avatar_map: dict[str, bytes] = {}

        async def fetch_one(session: aiohttp.ClientSession, user_id: str) -> None:
            url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=160"
            try:
                async with semaphore:
                    async with session.get(url, timeout=12) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.read()
                        if data:
                            avatar_map[user_id] = data
            except Exception:
                return

        async with aiohttp.ClientSession() as session:
            tasks = [fetch_one(session, uid) for uid in user_ids]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        self.avatar_map = avatar_map

    @staticmethod
    def _avatar_fallback_text(name: str) -> str:
        stripped = str(name or "").strip()
        return stripped[0] if stripped else "#"

    @staticmethod
    def _safe_ratio(value: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0

    def _build_top_item(self, entry: RankingEntry) -> str:
        uid = str(entry.user_id)
        avatar_data = self.avatar_map.get(uid)
        avatar_url = _bytes_to_data_url(avatar_data) if avatar_data else ""

        text = html.escape(self._avatar_fallback_text(entry.nickname))
        avatar_html = build_avatar_html(
            css_class="avatar",
            avatar_url=avatar_url,
            fallback_text=text,
        )
        safe_name = html.escape(str(entry.nickname))
        safe_attitude = html.escape(str(entry.attitude))
        safe_score = html.escape(str(entry.impression_text))

        return build_ranking_top_item_html(
            rank=int(entry.rank),
            avatar_html=avatar_html,
            safe_name=safe_name,
            safe_attitude=safe_attitude,
            safe_score=safe_score,
        )

    def _build_row(self, entry: RankingEntry) -> str:
        uid = str(entry.user_id)
        avatar_data = self.avatar_map.get(uid)
        avatar_url = _bytes_to_data_url(avatar_data) if avatar_data else ""
        ratio = self._safe_ratio(entry.progress_ratio) * 100

        text = html.escape(self._avatar_fallback_text(entry.nickname))
        avatar_html = build_avatar_html(
            css_class="mini-avatar",
            avatar_url=avatar_url,
            fallback_text=text,
        )

        safe_name = html.escape(str(entry.nickname))
        safe_attitude = html.escape(str(entry.attitude))
        safe_progress = html.escape(str(entry.progress_text))

        return build_ranking_row_html(
            rank=int(entry.rank),
            avatar_html=avatar_html,
            safe_name=safe_name,
            safe_attitude=safe_attitude,
            safe_progress=safe_progress,
            ratio_percent=ratio,
        )

    def _build_html(self, canvas_height: int) -> str:
        safe_title = html.escape(self.title)
        safe_updated = html.escape(self.updated_text or "未知")

        top_entries = self.entries[:3]
        list_entries = self.entries[3:]

        top_html = "".join(self._build_top_item(item) for item in top_entries)
        rows_html = "".join(self._build_row(item) for item in list_entries)

        safe_footer = html.escape(
            f"Created By AstrBot {VERSION} · Daily Sign Plugin {PLUGIN_VERSION}"
        )

        return build_ranking_card_html(
            canvas_height=canvas_height,
            safe_title=safe_title,
            safe_updated=safe_updated,
            top_html=top_html,
            rows_html=rows_html,
            safe_footer=safe_footer,
        )

    async def draw(self) -> bytes | None:
        if not self.entries:
            return None

        try:
            await self._prepare_avatars()
            canvas_height = max(
                RANKING_MIN_CARD_HEIGHT,
                RANKING_BASE_CARD_HEIGHT
                + max(0, len(self.entries) - 3) * RANKING_ROW_HEIGHT,
            )
            html_content = self._build_html(canvas_height=canvas_height)
            return await render_html_to_png(
                html_content=html_content,
                width=RANKING_CARD_WIDTH,
                height=canvas_height,
                selector="#card",
            )
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 排行榜图片生成失败: {e}")
            logger.error(f"{LOG_PREFIX}\n{traceback.format_exc()}")
            return None
