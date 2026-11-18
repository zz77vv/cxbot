import asyncio
import hashlib
import logging
import re
import time
from ...models.query import JRQueryResult
from ...utils.paths import confined

log = logging.getLogger(__name__)


def normalize_jr_query(text):
    return re.sub(r'\s+', ' ', (text or '').strip()).casefold()


def jr_cache_key(text):
    return hashlib.sha256(normalize_jr_query(text).encode('utf-8')).hexdigest()


class CacheInconsistent(RuntimeError):
    pass


class CacheService:
    def __init__(self, cfg, repository):
        self.cfg, self.repository = cfg, repository
        self.lock = asyncio.Lock()

    async def get(self, key):
        # Caller holds lock until durable task references have been written.
        row = await self.repository.get(key, time.time())
        if not row:
            return None
        if row['result_type'] not in {'file', 'text'}:
            raise CacheInconsistent('invalid_cache_kind')
        if row['result_type'] == 'file':
            try:
                path = confined(self.cfg.cache, row['file_path'] or '')
                if not path.is_file():
                    raise ValueError
            except (OSError, ValueError):
                raise CacheInconsistent('cached_file_missing_or_outside_root') from None
        return JRQueryResult(row['result_type'], row['result_text'], row['file_path'], row['file_name'])

    async def cleanup(self, now=None):
        now = time.time() if now is None else now
        async with self.lock:
            active_paths = await self.repository.expire_and_references(now, self.cfg.cache_ttl)
            active = set()
            for raw in active_paths:
                try:
                    active.add(confined(self.cfg.cache, raw))
                except (ValueError, OSError):
                    log.warning('Ignored out-of-root cache reference')
            if not self.cfg.cache.is_dir():
                return 0
            deleted = 0
            for candidate in self.cfg.cache.rglob('*'):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    path = confined(self.cfg.cache, candidate)
                    if path not in active and now - path.stat().st_mtime >= self.cfg.cache_ttl:
                        path.unlink()
                        deleted += 1
                except (OSError, ValueError):
                    log.warning('Cache cleanup skipped one unsafe/unavailable file')
            for directory in sorted(self.cfg.cache.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                if directory.is_dir() and not directory.is_symlink():
                    try:
                        confined(self.cfg.cache, directory).rmdir()
                    except OSError:
                        # Only empty directories are removable; occupied directories are expected.
                        continue
            return deleted

    async def loop(self):
        while True:
            await asyncio.sleep(self.cfg.cleanup_interval)
            try:
                await self.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error('Cache cleanup error_type=%s', type(error).__name__)
