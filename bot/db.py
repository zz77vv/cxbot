"""全新极简异步 SQLite 存储层（4 张核心表，WAL 模式，无历史遗留）。"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

BEIJING_TZ = timezone(timedelta(hours=8))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    phone TEXT PRIMARY KEY,
    flood_until REAL NOT NULL DEFAULT 0,
    jr_cooldown_until REAL NOT NULL DEFAULT 0,
    jr_membership TEXT NOT NULL DEFAULT 'unknown' CHECK(jr_membership IN ('unknown', 'free', 'member')),
    jr_membership_checked_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS checkins (
    phone TEXT NOT NULL,
    bot_key TEXT NOT NULL,
    last_attempt_at REAL NOT NULL DEFAULT 0,
    last_success_at REAL NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (phone, bot_key)
);

CREATE TABLE IF NOT EXISTS jr_usage (
    phone TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (phone, usage_date)
);

CREATE TABLE IF NOT EXISTS result_cache (
    cache_key TEXT PRIMARY KEY,
    result_type TEXT NOT NULL CHECK(result_type IN ('text', 'file')),
    result_text TEXT NOT NULL DEFAULT '',
    file_path TEXT,
    file_name TEXT,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_result_cache_expires ON result_cache(expires_at);
"""


def beijing_date(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, BEIJING_TZ).date().isoformat()


def make_cache_key(prefix: str, text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip()).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


@dataclass(frozen=True)
class CachedResult:
    result_type: str  # 'text' | 'file'
    text: str = ""
    file_path: str | None = None
    file_name: str | None = None


def _cleanup_cache_files_sync(cache_dir: Path, active: set[Path], now: float) -> int:
    if not cache_dir.is_dir():
        return 0
    deleted = 0
    base = cache_dir.resolve()
    for f in cache_dir.rglob("*"):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            resolved = f.resolve()
            if not resolved.is_relative_to(base):
                continue
            if resolved not in active and (now - f.stat().st_mtime) > 120:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    for d in sorted(cache_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not d.is_symlink():
            try:
                d.rmdir()
            except OSError:
                pass
    return deleted


class Database:
    """单连接 + 互斥事务锁的轻量级 SQLite 封装。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> Database:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        await self.conn.execute("PRAGMA busy_timeout=10000")
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(SCHEMA_SQL)
        # 兼容平滑补列（已有 v3 库自动补 jr_membership_checked_at）
        async with self.conn.execute("PRAGMA table_info(accounts)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "jr_membership_checked_at" not in cols:
            await self.conn.execute(
                "ALTER TABLE accounts ADD COLUMN jr_membership_checked_at REAL NOT NULL DEFAULT 0"
            )
        return self

    async def close(self) -> None:
        async with self._lock:
            if self.conn is not None:
                await self.conn.close()
                self.conn = None

    @asynccontextmanager
    async def tx(self):
        async with self._lock:
            assert self.conn is not None, "Database not opened"
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def fetchone(self, sql: str, params=()):
        async with self._lock:
            assert self.conn is not None, "Database not opened"
            async with self.conn.execute(sql, params) as cur:
                return await cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        async with self._lock:
            assert self.conn is not None, "Database not opened"
            async with self.conn.execute(sql, params) as cur:
                return await cur.fetchall()

    # ---------- 账号状态与 JR 会员/冷却 ----------

    async def get_account_meta(self, phone: str) -> dict:
        row = await self.fetchone("SELECT * FROM accounts WHERE phone=?", (phone,))
        if not row:
            return {
                "phone": phone,
                "flood_until": 0.0,
                "jr_cooldown_until": 0.0,
                "jr_membership": "unknown",
                "jr_membership_checked_at": 0.0,
            }
        return dict(row)

    async def all_account_meta(self) -> dict[str, dict]:
        rows = await self.fetchall("SELECT * FROM accounts")
        return {r["phone"]: dict(r) for r in rows}

    async def set_flood_wait(self, phone: str, until_at: float) -> None:
        now = time.time()
        async with self.tx() as c:
            await c.execute(
                """
                INSERT INTO accounts(phone, flood_until, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    flood_until = MAX(flood_until, excluded.flood_until),
                    updated_at = excluded.updated_at
                """,
                (phone, until_at, now),
            )

    async def set_jr_cooldown(self, phone: str, until_at: float, *, force_free: bool = False) -> None:
        now = time.time()
        async with self.tx() as c:
            if force_free:
                await c.execute(
                    """
                    INSERT INTO accounts(phone, jr_cooldown_until, jr_membership, jr_membership_checked_at, updated_at)
                    VALUES(?, ?, 'free', ?, ?)
                    ON CONFLICT(phone) DO UPDATE SET
                        jr_cooldown_until = MAX(jr_cooldown_until, excluded.jr_cooldown_until),
                        jr_membership = 'free',
                        jr_membership_checked_at = excluded.jr_membership_checked_at,
                        updated_at = excluded.updated_at
                    """,
                    (phone, until_at, now, now),
                )
            else:
                await c.execute(
                    """
                    INSERT INTO accounts(phone, jr_cooldown_until, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(phone) DO UPDATE SET
                        jr_cooldown_until = MAX(jr_cooldown_until, excluded.jr_cooldown_until),
                        updated_at = excluded.updated_at
                    """,
                    (phone, until_at, now),
                )

    async def set_jr_membership(self, phone: str, status: str) -> None:
        assert status in ("unknown", "free", "member")
        now = time.time()
        async with self.tx() as c:
            await c.execute(
                """
                INSERT INTO accounts(phone, jr_membership, jr_membership_checked_at, jr_cooldown_until, updated_at)
                VALUES(?, ?, ?, 0.0, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    jr_membership = excluded.jr_membership,
                    jr_membership_checked_at = excluded.jr_membership_checked_at,
                    jr_cooldown_until = CASE WHEN excluded.jr_membership='member' THEN 0 ELSE jr_cooldown_until END,
                    updated_at = excluded.updated_at
                """,
                (phone, status, now, now),
            )

    # ---------- JR 每日用量 ----------

    async def get_jr_daily_usage(self, phone: str, day: str | None = None) -> int:
        day = day or beijing_date()
        row = await self.fetchone(
            "SELECT used_count FROM jr_usage WHERE phone=? AND usage_date=?", (phone, day)
        )
        return int(row["used_count"]) if row else 0

    async def inc_jr_daily_usage(self, phone: str, day: str | None = None) -> int:
        day = day or beijing_date()
        async with self.tx() as c:
            await c.execute(
                """
                INSERT INTO jr_usage(phone, usage_date, used_count) VALUES(?, ?, 1)
                ON CONFLICT(phone, usage_date) DO UPDATE SET used_count = used_count + 1
                """,
                (phone, day),
            )
            async with c.execute(
                "SELECT used_count FROM jr_usage WHERE phone=? AND usage_date=?", (phone, day)
            ) as cur:
                row = await cur.fetchone()
                return int(row["used_count"])

    # ---------- 签到状态 ----------

    async def get_checkin(self, phone: str, bot_key: str) -> dict | None:
        row = await self.fetchone(
            "SELECT * FROM checkins WHERE phone=? AND bot_key=?", (phone, bot_key)
        )
        return dict(row) if row else None

    async def all_checkins(self) -> list[dict]:
        rows = await self.fetchall("SELECT * FROM checkins WHERE last_attempt_at > 0")
        return [dict(r) for r in rows]

    async def init_checkin(self, phone: str, bot_key: str, next_at: float) -> dict:
        async with self.tx() as c:
            await c.execute(
                "INSERT OR IGNORE INTO checkins(phone, bot_key, next_at) VALUES(?, ?, ?)",
                (phone, bot_key, next_at),
            )
            async with c.execute(
                "SELECT * FROM checkins WHERE phone=? AND bot_key=?", (phone, bot_key)
            ) as cur:
                return dict(await cur.fetchone())

    async def record_checkin(
        self, phone: str, bot_key: str, *, now: float, next_at: float, status: str, success: bool
    ) -> None:
        async with self.tx() as c:
            await c.execute(
                """
                INSERT INTO checkins(phone, bot_key, last_attempt_at, last_success_at, next_at, last_status)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone, bot_key) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = CASE WHEN ? THEN excluded.last_success_at ELSE last_success_at END,
                    next_at = excluded.next_at,
                    last_status = excluded.last_status
                """,
                (phone, bot_key, now, now if success else 0.0, next_at, status, int(success)),
            )

    # ---------- 结果缓存与历史清理 ----------

    async def get_cache(self, cache_key: str) -> CachedResult | None:
        row = await self.fetchone(
            "SELECT * FROM result_cache WHERE cache_key=? AND expires_at>?",
            (cache_key, time.time()),
        )
        if not row:
            return None
        return CachedResult(
            result_type=row["result_type"],
            text=row["result_text"] or "",
            file_path=row["file_path"],
            file_name=row["file_name"],
        )

    async def set_cache(self, cache_key: str, item: CachedResult, ttl: int) -> None:
        expires_at = time.time() + ttl
        async with self.tx() as c:
            await c.execute(
                """
                INSERT INTO result_cache(cache_key, result_type, result_text, file_path, file_name, expires_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_type = excluded.result_type,
                    result_text = excluded.result_text,
                    file_path = excluded.file_path,
                    file_name = excluded.file_name,
                    expires_at = excluded.expires_at
                """,
                (cache_key, item.result_type, item.text, item.file_path, item.file_name, expires_at),
            )

    async def purge_expired_cache(self, cache_dir: Path) -> int:
        now = time.time()
        cutoff_day = beijing_date(now - 30 * 86400)
        async with self.tx() as c:
            await c.execute("DELETE FROM result_cache WHERE expires_at<=?", (now,))
            await c.execute("DELETE FROM jr_usage WHERE usage_date<?", (cutoff_day,))
            async with c.execute(
                "SELECT file_path FROM result_cache WHERE file_path IS NOT NULL"
            ) as cur:
                active = {Path(r[0]).resolve() for r in await cur.fetchall() if r[0]}

        return await asyncio.to_thread(_cleanup_cache_files_sync, cache_dir, active, now)
