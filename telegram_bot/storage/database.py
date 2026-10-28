import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import aiosqlite


class Database:
    """One connection, one transaction lock; no cursor leaks or implicit commits."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.connection = None
        self.lock = asyncio.Lock()
        self.backup_path = None

    async def open(self):
        from .migrations import migrate, validate_legacy
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        self.connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            await self.connection.execute('PRAGMA busy_timeout=10000')
            await validate_legacy(self.connection)
            if existed:
                self.backup_path = self.path.with_name(
                    self.path.name + f'.backup-{int(time.time())}-{uuid.uuid4().hex[:8]}')
                with sqlite3.connect(self.backup_path) as destination:
                    await self.connection.backup(destination)
            await self.connection.execute('PRAGMA journal_mode=WAL')
            await self.connection.execute('PRAGMA synchronous=FULL')
            await self.connection.execute('PRAGMA foreign_keys=ON')
            await migrate(self)
        except BaseException:
            await self.close()
            raise
        return self

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                await self.connection.execute('BEGIN IMMEDIATE')
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def fetchone(self, sql, params=()):
        async with self.lock:
            async with self.connection.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, sql, params=()):
        async with self.lock:
            async with self.connection.execute(sql, params) as cursor:
                return await cursor.fetchall()

    async def close(self):
        async with self.lock:
            if self.connection:
                await self.connection.close()
                self.connection = None
