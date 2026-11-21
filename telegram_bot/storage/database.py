import asyncio
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
import aiosqlite

log = logging.getLogger(__name__)
DAILY_BACKUPS_TO_KEEP = 7


class Database:
    """One connection, one transaction lock; no cursor leaks or implicit commits."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.connection = None
        self.lock = asyncio.Lock()
        self.backup_path = None

    async def open(self):
        from .migrations import SCHEMA_VERSION, migrate, validate_legacy
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        self.connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            await self.connection.execute('PRAGMA busy_timeout=10000')
            version = await validate_legacy(self.connection)
            if existed:
                await self._snapshot(migration=version != SCHEMA_VERSION)
            await self.connection.execute('PRAGMA journal_mode=WAL')
            await self.connection.execute('PRAGMA synchronous=FULL')
            await self.connection.execute('PRAGMA foreign_keys=ON')
            await migrate(self)
        except BaseException:
            await self.close()
            raise
        return self

    async def _snapshot(self, *, migration):
        prefix = self.path.name + '.backup-daily-'
        name = (self.path.name + f'.backup-{int(time.time())}-{uuid.uuid4().hex[:8]}' if migration
                else prefix + datetime.now(timezone.utc).strftime('%Y%m%d'))
        destination = self.path.with_name(name)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise OSError('unexpected_backup_path_type')
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + f'.{uuid.uuid4().hex}.tmp')
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(temporary)) as copy:
                    await self.connection.backup(copy)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        self.backup_path = destination
        if migration:
            return
        daily = sorted((p for p in self.path.parent.iterdir()
                        if p.name.startswith(prefix) and re.fullmatch(r'\d{8}', p.name[len(prefix):])
                        and not p.is_symlink() and p.is_file()), reverse=True)
        for expired in daily[DAILY_BACKUPS_TO_KEEP:]:
            try:
                expired.unlink()
            except OSError as error:
                log.warning('Daily backup retention failed error_type=%s', type(error).__name__)

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
