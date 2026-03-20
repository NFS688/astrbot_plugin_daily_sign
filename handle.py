from __future__ import annotations

import datetime
import re
import string
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Reply

from .constants import LOG_PREFIX
from .database import SignData


class SignTransactionError(RuntimeError):
    """Raised when the sign transaction cannot be committed atomically."""


def is_valid_userid(userid: str) -> bool:
    if not userid or len(userid.strip()) == 0:
        return False
    userid = userid.strip()
    if len(userid) > 64:
        return False
    allowed_chars = string.ascii_letters + string.digits + "_-:@."
    return all(c in allowed_chars for c in userid)


def extract_ids_from_text(text: str) -> list[str]:
    if not text:
        return []
    ids: list[str] = []
    ids.extend(re.findall(r"\[CQ:at,[^\]]*?(?:qq|id)=(\d+)[^\]]*?\]", text))
    ids.extend(re.findall(r"@<[^:<>]+:(?P<uid>[^:<>]+)>", text))
    ids.extend(re.findall(r"(?<!\d)(\d{5,20})(?!\d)", text))

    ordered: list[str] = []
    seen: set[str] = set()
    for item in ids:
        value = str(item).strip()
        if not value or value in seen or not is_valid_userid(value):
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def resolve_target_user_id(event: AstrMessageEvent, target: str = "") -> str:
    ids: list[str] = []

    for comp in event.get_messages():
        if isinstance(comp, Reply) and comp.sender_id:
            sid = str(comp.sender_id).strip()
            if is_valid_userid(sid):
                ids.append(sid)

    for comp in event.get_messages():
        if isinstance(comp, At):
            qq = str(comp.qq or "").strip()
            if qq and qq.lower() != "all" and is_valid_userid(qq):
                ids.append(qq)

    ids.extend(extract_ids_from_text(target))
    ids.extend(extract_ids_from_text(event.get_message_str()))

    for uid in ids:
        if uid:
            return uid

    return str(event.get_sender_id() or event.get_session_id())


class DataHandle:
    def __init__(
        self,
        userid: str = "0",
        nickname: str = "",
        add_coins: int | None = 0,
        add_impression: float | None = 0,
        next_score: float | None = 25,
    ) -> None:
        self.userid = str(userid)
        self.nickname = str(nickname or "")
        self.sign_db = SignData()
        self.userdata: dict[str, Any] | None = None
        self.add_coins = add_coins or 0
        self.add_impression = add_impression or 0
        self.next_score = next_score or 25
        self.applied_boost = 0.0

    async def load_data(self) -> dict[str, Any]:
        sign_data = await self.sign_db._get_user_data(self.userid)

        merged_data: dict[str, Any] = {
            "user_id": self.userid,
            "nickname": self.nickname,
            "coins": 0,
            "total_days": 0,
            "last_sign": "",
            "continuous_days": 0,
            "impression": 0.0,
            "level": 1,
        }

        if sign_data:
            merged_data.update(sign_data)

        self.userdata = merged_data
        return self.userdata

    async def close(self) -> None:
        await self.sign_db._close()

    def _calc_level_by_impression(self, impression: float) -> int:
        try:
            score = float(self.next_score or 25)
            if score <= 0:
                score = 25
            level = int(max(0.0, impression) / score) + 1
            return max(1, min(8, level))
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 计算好感度等级失败，已使用默认等级: {e}")
            return 1

    @staticmethod
    def _apply_sign_streak_bonus(base_coins: int, next_continuous_days: int) -> int:
        if next_continuous_days >= 7:
            bonus_rate = 0.15
        elif next_continuous_days >= 3:
            bonus_rate = 0.10
        else:
            bonus_rate = 0.0

        if bonus_rate <= 0:
            return int(base_coins)

        bonus = int(int(base_coins) * bonus_rate)
        return int(base_coins) + bonus

    async def _update_data(self, wallet_plugin: Any | None = None) -> bool:
        try:
            async with self.sign_db.connection() as sign_conn:
                if wallet_plugin is None:
                    return await self._update_data_with_connection(sign_conn)

                async with wallet_plugin.api_wallet_write_lock():
                    wallet_schema = await wallet_plugin.api_attach_wallet_database(
                        sign_conn
                    )
                    return await self._update_data_with_connection(
                        sign_conn,
                        wallet_plugin=wallet_plugin,
                        wallet_schema=wallet_schema,
                    )
        except SignTransactionError:
            raise
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 更新用户数据失败: {e}")
            raise

    async def _update_data_with_connection(
        self,
        sign_conn: Any,
        wallet_plugin: Any | None = None,
        wallet_schema: str | None = None,
    ) -> bool:
        await sign_conn.execute("BEGIN IMMEDIATE")

        try:
            await sign_conn.execute(
                "INSERT OR IGNORE INTO sign_data (user_id, nickname) VALUES (?, ?)",
                (self.userid, self.nickname),
            )

            async with sign_conn.execute(
                """
                SELECT
                    s.total_days,
                    s.last_sign,
                    s.continuous_days,
                    s.impression,
                    s.level,
                    s.impression_boost
                FROM sign_data AS s
                WHERE s.user_id = ?
                """,
                (self.userid,),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                raise RuntimeError("签到用户数据缺失")

            old_total_days = int(row[0] or 0)
            old_last_sign = str(row[1] or "")
            old_continuous_days = int(row[2] or 0)
            old_impression = float(row[3] or 0.0)
            old_impression_boost = float(row[5] or 0.0)

            now = datetime.datetime.now()
            today_text = now.strftime("%Y-%m-%d")
            if old_last_sign.startswith(today_text):
                self.userdata = {
                    "user_id": self.userid,
                    "nickname": self.nickname,
                    "total_days": old_total_days,
                    "last_sign": old_last_sign,
                    "continuous_days": old_continuous_days,
                    "impression": old_impression,
                    "level": int(row[4] or self._calc_level_by_impression(old_impression)),
                }
                await sign_conn.commit()
                return False

            yesterday_text = (now.date() - datetime.timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
            continuous_days = (
                old_continuous_days + 1 if old_last_sign.startswith(yesterday_text) else 1
            )
            awarded_coins = self._apply_sign_streak_bonus(
                base_coins=int(self.add_coins or 0),
                next_continuous_days=continuous_days,
            )
            total_days = old_total_days + 1
            last_sign = now.strftime("%Y-%m-%d %H:%M:%S")
            boosted_add = float(self.add_impression or 0.0)
            if old_impression_boost > 0:
                boosted_add = round(boosted_add * (1 + old_impression_boost), 2)
                self.applied_boost = old_impression_boost
            self.add_impression = boosted_add
            impression = max(0.0, old_impression + boosted_add)
            level = self._calc_level_by_impression(impression)

            await sign_conn.execute(
                """
                UPDATE sign_data
                SET total_days = ?,
                    last_sign = ?,
                    continuous_days = ?,
                    impression = ?,
                    level = ?,
                    nickname = ?,
                    impression_boost = 0.0
                WHERE user_id = ?
                """,
                (
                    total_days,
                    last_sign,
                    continuous_days,
                    impression,
                    level,
                    self.nickname,
                    self.userid,
                ),
            )
            new_balance: int | None = None
            if wallet_plugin is not None:
                if not wallet_schema:
                    raise RuntimeError("Wallet schema was not attached")
                wallet_ok, new_balance = await wallet_plugin.api_change_balance_in_connection(
                    conn=sign_conn,
                    user_id=self.userid,
                    delta=awarded_coins,
                    nickname=self.nickname,
                    allow_negative=False,
                    schema=wallet_schema,
                )
                if not wallet_ok:
                    raise SignTransactionError("签到失败，钱包入账失败，请稍后重试。")
            await sign_conn.commit()
        except Exception:
            try:
                await sign_conn.rollback()
            except Exception:
                pass
            raise

        self.add_coins = awarded_coins
        self.userdata = {
            "user_id": self.userid,
            "nickname": self.nickname,
            "total_days": total_days,
            "last_sign": last_sign,
            "continuous_days": continuous_days,
            "impression": impression,
            "level": level,
        }
        if new_balance is not None:
            self.userdata["coins"] = int(new_balance)
        return True
