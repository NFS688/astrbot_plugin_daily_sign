from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from .constants import (
    PLUGIN_DATA_DIR,
    SIGN_DB_PATH,
)


class SignData:
    def __init__(self) -> None:
        PLUGIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = SIGN_DB_PATH

    async def _open_connection(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout = 2000")
        await self._init_db(conn)
        return conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._open_connection()
        try:
            yield conn
        finally:
            await conn.close()

    async def _init_db(self, conn: aiosqlite.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sign_data (
                uid INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT DEFAULT '',
                total_days INTEGER DEFAULT 0,
                last_sign TEXT DEFAULT '',
                continuous_days INTEGER DEFAULT 0,
                impression REAL DEFAULT 0.00,
                level INTEGER DEFAULT 0
            )
            """
        )
        await self._ensure_unique_user_index(conn)
        await self._ensure_schema_columns(conn)
        await conn.commit()

    async def _ensure_unique_user_index(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_sign_data_user_id'
            LIMIT 1
            """
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            return

        await conn.execute(
            """
            DELETE FROM sign_data
            WHERE uid NOT IN (
                SELECT MAX(uid) FROM sign_data GROUP BY user_id
            )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sign_data_user_id
            ON sign_data(user_id)
            """
        )

    async def _ensure_schema_columns(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute("PRAGMA table_info(sign_data)") as cursor:
            rows = await cursor.fetchall()
        existing = {str(row[1]) for row in rows}

        migrations = {
            "nickname": "TEXT DEFAULT ''",
            "impression_boost": "REAL DEFAULT 0.0",
        }
        for column_name, column_def in migrations.items():
            if column_name in existing:
                continue
            await conn.execute(
                f"ALTER TABLE sign_data ADD COLUMN {column_name} {column_def}"
            )

    async def _get_user_data(self, user_id: str) -> dict[str, Any] | None:
        async with self.connection() as conn:
            async with conn.execute(
                """
                SELECT
                    uid,
                    user_id,
                    nickname,
                    total_days,
                    last_sign,
                    continuous_days,
                    impression,
                    level
                FROM sign_data
                WHERE user_id = ?
                """,
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None

                columns = [
                    "uid",
                    "user_id",
                    "nickname",
                    "total_days",
                    "last_sign",
                    "continuous_days",
                    "impression",
                    "level",
                ]
                return dict(zip(columns, row, strict=True))

    async def _get_ranking(self, limit: int = 10) -> list[tuple[Any, ...]]:
        async with self.connection() as conn:
            async with conn.execute(
                """
                SELECT user_id, impression, nickname
                FROM sign_data
                ORDER BY impression DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                return await cursor.fetchall()

    async def _close(self) -> None:
        return None
