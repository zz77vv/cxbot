import asyncio


class AccountStatusService:
    def __init__(self, manager):
        self.manager = manager

    async def snapshot(self):
        accounts = tuple(self.manager.accounts.values())
        await asyncio.gather(*(self.manager.refresh(a) for a in accounts))
        return [(a.key, a.state.value, a.error_type, a.unavailable_until) for a in accounts]
