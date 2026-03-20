from __future__ import annotations

import asyncio
import base64
import datetime
import json
import random
import sys
import traceback
from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

from .constants import (
    CACHE_CLEANUP_STATE_PATH,
    DEFAULT_CACHE_CLEANUP_INTERVAL_DAYS,
    DEFAULT_CACHE_CLEANUP_TIME,
    DEFAULT_CACHE_RETENTION_DAYS,
    DEFAULT_ENABLE_CACHE_CLEANUP,
    DEFAULT_NEXT_SCORE,
    DEFAULT_RANKING_LIMIT,
    LOG_PREFIX,
)
from .database import SignData
from .draw import (
    ImageGen,
    ImpressionRankingImageGen,
    RankingEntry,
    cleanup_sign_cache,
    get_background,
    init_draw,
    shutdown_draw,
)
from .handle import (
    DataHandle,
    SignTransactionError,
    resolve_target_user_id,
)

SHOP_PROVIDER = "astrbot_plugin_daily_sign"
SHOP_PROVIDER_LABEL = "📝 签到商店"
CACHE_CLEANUP_RETRY_COUNT = 3
CACHE_CLEANUP_RETRY_DELAY_SECONDS = 1


class SignPlugin(Star):
    """每日签到插件，支持签到、好感度排行、签到背景查询。"""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.cache_cleanup_task: asyncio.Task | None = None

    async def terminate(self) -> None:
        task = self.cache_cleanup_task
        self.cache_cleanup_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            await self._unregister_shop_items()
        finally:
            await shutdown_draw()

    async def initialize(self) -> None:
        await self._unregister_shop_items()
        await self._register_shop_items()
        self._ensure_cache_cleanup_task()

    @staticmethod
    def _sanitize_positive_int(raw_value: Any, default: int) -> int:
        try:
            value = int(raw_value)
        except Exception:
            return int(default)
        return max(1, value)

    @staticmethod
    def _sanitize_retention_days(raw_value: Any, default: int) -> int:
        try:
            value = int(raw_value)
        except Exception:
            return int(default)
        return max(0, value)

    def _is_cache_cleanup_enabled(self) -> bool:
        return bool(
            self.config.get(
                "enable_cache_cleanup",
                DEFAULT_ENABLE_CACHE_CLEANUP,
            )
        )

    def _get_cache_cleanup_interval_days(self) -> int:
        return self._sanitize_positive_int(
            self.config.get(
                "cache_cleanup_interval_days",
                DEFAULT_CACHE_CLEANUP_INTERVAL_DAYS,
            ),
            DEFAULT_CACHE_CLEANUP_INTERVAL_DAYS,
        )

    def _get_cache_retention_days(self) -> int:
        return self._sanitize_retention_days(
            self.config.get(
                "cache_retention_days",
                DEFAULT_CACHE_RETENTION_DAYS,
            ),
            DEFAULT_CACHE_RETENTION_DAYS,
        )

    def _get_cache_cleanup_time(self) -> datetime.time:
        raw_value = str(
            self.config.get(
                "cache_cleanup_time",
                DEFAULT_CACHE_CLEANUP_TIME,
            )
            or DEFAULT_CACHE_CLEANUP_TIME
        ).strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.datetime.strptime(raw_value, fmt).time()
            except ValueError:
                continue

        logger.warning(
            f"{LOG_PREFIX} 非法缓存清理时间配置: {raw_value}，已回退到 {DEFAULT_CACHE_CLEANUP_TIME}"
        )
        return datetime.datetime.strptime(
            DEFAULT_CACHE_CLEANUP_TIME,
            "%H:%M",
        ).time()

    @staticmethod
    def _get_local_now() -> datetime.datetime:
        return datetime.datetime.now().astimezone()

    async def _load_cache_cleanup_state(self) -> datetime.datetime | None:
        if not CACHE_CLEANUP_STATE_PATH.exists():
            return None

        try:
            content = await asyncio.to_thread(
                CACHE_CLEANUP_STATE_PATH.read_text,
                encoding="utf-8",
            )
            payload = json.loads(content or "{}")
            raw_value = str(payload.get("last_cleanup_at") or "").strip()
            if not raw_value:
                return None
            parsed = datetime.datetime.fromisoformat(raw_value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=self._get_local_now().tzinfo)
            return parsed.astimezone()
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取缓存清理状态失败: {e}")
            return None

    async def _save_cache_cleanup_state(self, cleanup_at: datetime.datetime) -> None:
        try:
            CACHE_CLEANUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(
                {"last_cleanup_at": cleanup_at.isoformat()},
                ensure_ascii=False,
                indent=2,
            )
            await asyncio.to_thread(
                CACHE_CLEANUP_STATE_PATH.write_text,
                content,
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 写入缓存清理状态失败: {e}")

    def _compute_next_cleanup_run_at(
        self,
        now: datetime.datetime,
        cleanup_time: datetime.time,
        interval_days: int,
        last_cleanup_at: datetime.datetime | None,
    ) -> datetime.datetime:
        scheduled_today = now.replace(
            hour=cleanup_time.hour,
            minute=cleanup_time.minute,
            second=cleanup_time.second,
            microsecond=0,
        )
        if now >= scheduled_today:
            if last_cleanup_at is None:
                return now
            days_since_cleanup = (now.date() - last_cleanup_at.date()).days
            if days_since_cleanup >= interval_days:
                return now
            return scheduled_today + datetime.timedelta(days=1)
        return scheduled_today

    def _ensure_cache_cleanup_task(self) -> None:
        if not self._is_cache_cleanup_enabled():
            return
        if self.cache_cleanup_task is not None and not self.cache_cleanup_task.done():
            return
        self.cache_cleanup_task = asyncio.create_task(
            self._cache_cleanup_loop(),
            name="daily_sign_cache_cleanup",
        )

    async def _cleanup_sign_cache_with_retry(self, retention_days: int) -> int:
        max_attempts = 1 + CACHE_CLEANUP_RETRY_COUNT
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await cleanup_sign_cache(retention_days)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                if attempt >= max_attempts:
                    break
                logger.warning(
                    f"{LOG_PREFIX} 定时清理签到缓存第 {attempt} 次执行失败: {e}，"
                    f"{CACHE_CLEANUP_RETRY_DELAY_SECONDS} 秒后重试。"
                )
                await asyncio.sleep(CACHE_CLEANUP_RETRY_DELAY_SECONDS)

        assert last_error is not None
        raise last_error

    async def _cache_cleanup_loop(self) -> None:
        while True:
            interval_days = self._get_cache_cleanup_interval_days()
            cleanup_time = self._get_cache_cleanup_time()
            retention_days = self._get_cache_retention_days()
            last_cleanup_at = await self._load_cache_cleanup_state()
            now = self._get_local_now()
            next_run_at = self._compute_next_cleanup_run_at(
                now=now,
                cleanup_time=cleanup_time,
                interval_days=interval_days,
                last_cleanup_at=last_cleanup_at,
            )
            delay_seconds = max(0.0, (next_run_at - now).total_seconds())
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

            cleanup_started_at = self._get_local_now()
            try:
                removed_count = await self._cleanup_sign_cache_with_retry(
                    retention_days
                )
                logger.info(
                    f"{LOG_PREFIX} 已清理 {removed_count} 个签到缓存文件，保留最近 {retention_days} 天缓存。"
                )
                await self._save_cache_cleanup_state(cleanup_started_at)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} 定时清理签到缓存失败，已重试 {CACHE_CLEANUP_RETRY_COUNT} 次: {e}"
                )

            await asyncio.sleep(1)

    def _get_shop_plugin(self) -> Any | None:
        for metadata in self.context.get_all_stars() or []:
            if getattr(metadata, "root_dir_name", "") != "astrbot_plugin_shop":
                continue
            star_cls = getattr(metadata, "star_cls", None)
            if star_cls is not None:
                return star_cls
        return None

    def _get_shop_item_class(self) -> type | None:
        shop = self._get_shop_plugin()
        if shop is None:
            return None
        base_package = type(shop).__module__.rsplit(".", 1)[0]
        registry = sys.modules.get(f"{base_package}.shop_registry")
        if registry is None:
            return None
        return getattr(registry, "ShopItem", None)

    async def _register_shop_items(self) -> None:
        shop = self._get_shop_plugin()
        if shop is None:
            logger.debug(f"{LOG_PREFIX} 商店插件未安装，跳过好感度加成卡注册。")
            return
        ShopItem = self._get_shop_item_class()
        if ShopItem is None:
            logger.warning(f"{LOG_PREFIX} 无法获取 ShopItem 类，跳过商店注册。")
            return

        cards = [
            (
                "impression_boost_basic",
                "初级好感度加成卡",
                30,
                "使用后下次签到好感度获得 10% 额外加成",
                0.10,
            ),
            (
                "impression_boost_intermediate",
                "中级好感度加成卡",
                80,
                "使用后下次签到好感度获得 20% 额外加成",
                0.20,
            ),
            (
                "impression_boost_advanced",
                "高级好感度加成卡",
                150,
                "使用后下次签到好感度获得 30% 额外加成",
                0.30,
            ),
        ]
        items = []
        for item_id, name, price, desc, boost_rate in cards:
            items.append(
                ShopItem(
                    item_id=item_id,
                    display_name=name,
                    price=price,
                    description=desc,
                    max_stack=5,
                    on_use=self._make_on_use_callback(boost_rate, name),
                )
            )
        try:
            await shop.api_register_items(
                provider=SHOP_PROVIDER,
                items=items,
                provider_label=SHOP_PROVIDER_LABEL,
            )
            logger.info(f"{LOG_PREFIX} 已注册 {len(items)} 个好感度加成卡到商店。")
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 注册商店物品失败: {e}")

    async def _unregister_shop_items(self) -> None:
        shop = self._get_shop_plugin()
        if shop is None:
            return

        unregister_provider = getattr(shop, "api_unregister_provider", None)
        if not callable(unregister_provider):
            logger.warning(
                f"{LOG_PREFIX} 商店插件缺少 api_unregister_provider，无法清理商店残留。"
            )
            return

        try:
            removed = await unregister_provider(SHOP_PROVIDER)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 清理商店物品残留失败: {e}")
            return

        if removed:
            logger.info(f"{LOG_PREFIX} 已从商店移除好感度加成卡注册。")

    def _make_on_use_callback(self, boost_rate: float, card_name: str):
        async def on_use(
            user_id: str, item_id: str, provider: str, metadata: dict
        ) -> str | None:
            await self._set_impression_boost(user_id, boost_rate)
            pct = int(boost_rate * 100)
            return f"✨ 已激活「{card_name}」，下次签到好感度将获得 {pct}% 的额外加成！"

        return on_use

    async def _set_impression_boost(self, user_id: str, boost_rate: float) -> None:
        sign_db = SignData()
        try:
            async with sign_db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT OR IGNORE INTO sign_data (user_id) VALUES (?)",
                        (user_id,),
                    )
                    await conn.execute(
                        "UPDATE sign_data SET impression_boost = ? WHERE user_id = ?",
                        (boost_rate, user_id),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        finally:
            await sign_db._close()

    def _get_wallet_plugin(self) -> Any | None:
        get_all_stars = getattr(self.context, "get_all_stars", None)
        if not callable(get_all_stars):
            return None

        try:
            stars = get_all_stars()
        except Exception:
            return None

        for metadata in stars or []:
            if getattr(metadata, "root_dir_name", "") != "astrbot_plugin_wallet":
                continue
            star_cls = getattr(metadata, "star_cls", None)
            if star_cls is None:
                continue
            return star_cls
        return None

    async def _require_wallet_plugin(self, event: AstrMessageEvent) -> Any | None:
        wallet_plugin = self._get_wallet_plugin()
        if wallet_plugin is None:
            event.set_result(
                MessageEventResult().message(
                    "未检测到钱包插件 astrbot_plugin_wallet，请先安装并启用。"
                )
            )
            return None

        required_api = [
            "api_get_wallet_name",
            "api_get_balance",
            "api_wallet_write_lock",
            "api_attach_wallet_database",
            "api_change_balance_in_connection",
        ]
        for api_name in required_api:
            if callable(getattr(wallet_plugin, api_name, None)):
                continue
            event.set_result(
                MessageEventResult().message(
                    f"钱包插件接口缺失: {api_name}，请升级 astrbot_plugin_wallet。"
                )
            )
            return None
        return wallet_plugin

    @staticmethod
    def _clamp_limit(raw_limit: Any) -> int:
        try:
            limit = int(raw_limit)
        except Exception:
            limit = DEFAULT_RANKING_LIMIT
        return max(1, min(50, limit))

    @staticmethod
    def _sanitize_next_score(raw_next_score: Any) -> Decimal:
        default_next_score = Decimal(str(DEFAULT_NEXT_SCORE))
        try:
            next_score = Decimal(str(raw_next_score))
        except Exception:
            next_score = default_next_score
        if next_score <= 0:
            next_score = default_next_score
        return next_score

    @staticmethod
    def _calc_level(impression: Decimal, next_score: Decimal) -> int:
        if next_score <= 0:
            next_score = Decimal(str(DEFAULT_NEXT_SCORE))
        try:
            level = int(impression / next_score) + 1
        except Exception:
            level = 1
        return max(1, min(8, level))

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")

    @filter.regex(r"^签到$")
    async def sign(self, event: AstrMessageEvent) -> None:
        """签到。"""
        await init_draw()
        wallet_plugin = await self._require_wallet_plugin(event)
        if wallet_plugin is None:
            return

        user_id = str(event.get_sender_id() or event.get_session_id())
        nickname = str(event.get_sender_name() or user_id)

        add_coins = random.randint(1, 50)
        add_impression = round(random.uniform(0, 1), 2)

        wallet_name = await wallet_plugin.api_get_wallet_name()
        next_score = self.config.get("next_score", DEFAULT_NEXT_SCORE)

        datahandle = DataHandle(
            userid=user_id,
            nickname=nickname,
            add_coins=add_coins,
            add_impression=add_impression,
            next_score=next_score,
        )

        try:
            sign_success = await datahandle._update_data(wallet_plugin=wallet_plugin)
            userdata = datahandle.userdata or await datahandle.load_data()

            if not sign_success:
                userdata["coins"] = await wallet_plugin.api_get_balance(
                    user_id,
                    nickname=nickname,
                )
                result = MessageEventResult().message("你今天已经签过到啦！")
                image_gen = ImageGen(userdata=userdata, wallet_name=wallet_name)
                img_bytes = await image_gen._image_cache()
                if img_bytes:
                    result.base64_image(base64.b64encode(img_bytes).decode("utf-8"))
                event.set_result(result)
                return

            add_coins = int(datahandle.add_coins or 0)
            add_impression = float(datahandle.add_impression or add_impression)
            if "coins" not in userdata:
                userdata["coins"] = await wallet_plugin.api_get_balance(
                    user_id,
                    nickname=nickname,
                )

            image_gen = ImageGen(
                userdata=userdata,
                nickname=nickname,
                wallet_name=wallet_name,
                add_coins=add_coins,
                add_impression=add_impression,
                next_score=next_score,
                use_local_bg=self.config.get("use_local_bg", False),
            )

            img_bytes = await image_gen._draw()
            if img_bytes:
                result = MessageEventResult().base64_image(
                    base64.b64encode(img_bytes).decode("utf-8")
                )
                if datahandle.applied_boost > 0:
                    pct = int(datahandle.applied_boost * 100)
                    result.message(
                        f"✨ 好感度加成卡生效！本次获得 {pct}% 额外好感度加成"
                    )
                event.set_result(result)
            else:
                event.set_result(
                    MessageEventResult().message(
                        "签到成功，但图片生成失败，请稍后重试。"
                    )
                )

        except SignTransactionError as e:
            logger.warning(f"{LOG_PREFIX} 签到事务失败: {e}")
            event.set_result(MessageEventResult().message(str(e)))
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 签到失败: {e}")
            logger.debug(f"{LOG_PREFIX}\n{traceback.format_exc()}")
            event.set_result(MessageEventResult().message("签到失败，请稍后重试。"))
        finally:
            await datahandle.close()

    @filter.regex(r"^获得签到背景(?:\s+.+)?$")
    async def get_sign_background(self, event: AstrMessageEvent) -> None:
        """获得今天的签到背景，可通过 @ 或用户ID 指定用户。"""
        message = event.get_message_str().strip()
        target = message.removeprefix("获得签到背景").strip()
        user_id = resolve_target_user_id(event, target)
        today = datetime.datetime.now().strftime("%Y-%m-%d")

        try:
            img_bytes = await get_background(user_id, today)
            if img_bytes:
                event.set_result(
                    MessageEventResult().base64_image(
                        base64.b64encode(img_bytes).decode("utf-8")
                    )
                )
            else:
                event.set_result(
                    MessageEventResult().message(
                        "未找到该用户今天的签到背景，请先完成今日签到。"
                    )
                )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 获取签到背景失败: {e}")
            event.set_result(
                MessageEventResult().message("获取签到背景失败，请稍后重试。")
            )

    @filter.regex(r"^好感度排行$")
    async def impression_ranking(self, event: AstrMessageEvent) -> None:
        """查看好感度排行。"""
        await init_draw()

        limit = self._clamp_limit(
            self.config.get("ranking_limit", DEFAULT_RANKING_LIMIT)
        )
        next_score = self._sanitize_next_score(
            self.config.get("next_score", DEFAULT_NEXT_SCORE)
        )
        max_impression = (next_score * Decimal("8")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        entries: list[RankingEntry] = []

        sign_db = SignData()
        try:
            rows = await sign_db._get_ranking(limit=limit)
            for index, row in enumerate(rows, start=1):
                user_id = str((row[0] if len(row) > 0 else "") or "").strip()
                if not user_id:
                    continue

                raw_impression = row[1] if len(row) > 1 else 0
                raw_nickname = row[2] if len(row) > 2 else ""

                try:
                    impression = Decimal(str(raw_impression or 0)).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP,
                    )
                except Exception:
                    impression = Decimal("0.00")

                level = self._calc_level(impression, next_score)
                attitude = f"Level {level}"
                nickname = str(raw_nickname or user_id)
                progress_ratio = float(
                    max(
                        Decimal("0"),
                        min(Decimal("1"), impression / max_impression),
                    )
                )
                impression_str = self._format_decimal(impression)
                max_impression_str = self._format_decimal(max_impression)
                entries.append(
                    RankingEntry(
                        rank=index,
                        user_id=user_id,
                        nickname=nickname,
                        attitude=attitude,
                        impression_text=impression_str,
                        progress_text=f"{impression_str}/{max_impression_str}",
                        progress_ratio=progress_ratio,
                    )
                )
        finally:
            await sign_db._close()

        if not entries:
            event.set_result(MessageEventResult().message("暂无好感度数据"))
            return

        updated_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        image_gen = ImpressionRankingImageGen(
            entries=entries,
            title="好感度排行",
            max_impression=float(max_impression),
            updated_text=updated_text,
        )
        img_bytes = await image_gen.draw()

        if not img_bytes:
            event.set_result(
                MessageEventResult().message("排行榜图片生成失败，请稍后重试。")
            )
            return

        event.set_result(
            MessageEventResult().base64_image(
                base64.b64encode(img_bytes).decode("utf-8")
            )
        )
