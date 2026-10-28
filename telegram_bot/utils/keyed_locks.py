import asyncio
from contextlib import asynccontextmanager


class KeyedLocks:
    def __init__(self):
        self._locks = {}
        self._users = {}

    @asynccontextmanager
    async def hold(self, key):
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                del self._locks[key]
