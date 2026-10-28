import asyncio
from contextlib import asynccontextmanager


class RequestTracker:
    def __init__(self):
        self.accepting = True
        self.active = set()

    @asynccontextmanager
    async def track(self):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            yield
        finally:
            self.active.discard(task)
