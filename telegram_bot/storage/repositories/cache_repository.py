from contextlib import asynccontextmanager
from ...models.enums import UNRESOLVED


class CacheRepository:
    def __init__(self, db):
        self.db = db

    async def get(self, key, now):
        return await self.db.fetchone('SELECT * FROM jr_result_cache WHERE cache_key=? AND expires_at>?', (key, now))

    @asynccontextmanager
    async def lease(self, path, owner):
        async with self.db.transaction() as c:
            await c.execute('INSERT OR IGNORE INTO file_leases VALUES(?,?)', (path, owner))
        try:
            yield
        finally:
            async with self.db.transaction() as c:
                await c.execute('DELETE FROM file_leases WHERE path=? AND owner=?', (path, owner))

    async def expire_and_references(self, now, ttl):
        async with self.db.transaction() as c:
            await c.execute('DELETE FROM jr_result_cache WHERE expires_at<=?', (now,))
            # Keep undelivered results and unresolved tasks even beyond the cache TTL.
            await c.execute("UPDATE query_tasks SET result_text=NULL,file_path=NULL,file_name=NULL WHERE state='success' AND delivery_state='delivered' AND updated_at<?", (now - ttl,))
            await c.execute("UPDATE query_tasks SET result_text=NULL,file_path=NULL,file_name=NULL WHERE state='failed' AND updated_at<?", (now - ttl,))
            async with c.execute('SELECT file_path FROM jr_result_cache WHERE file_path IS NOT NULL UNION SELECT file_path FROM query_tasks WHERE file_path IS NOT NULL UNION SELECT path FROM file_leases') as cur:
                return [r[0] for r in await cur.fetchall()]

    async def clear_stale_leases(self):
        # Called only after acquiring the process-wide database lock.
        async with self.db.transaction() as c:
            await c.execute('DELETE FROM file_leases')
