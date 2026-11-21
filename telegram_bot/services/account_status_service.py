import asyncio
from ..target_bots import TARGETS


class AccountStatusService:
    def __init__(self, manager, checkins=None):
        self.manager = manager
        self.checkins = checkins

    async def snapshot(self):
        accounts = tuple(self.manager.accounts.values())
        await asyncio.gather(*(self.manager.refresh(a) for a in accounts if not a.lock.locked()))
        return [(a.key, a.state.value, a.error_type, a.unavailable_until) for a in accounts]

    async def checkin_status(self):
        names = {target.key: target.name for target in TARGETS}
        by_account = {}
        if self.checkins is not None:
            for row in await self.checkins.status():
                if row['bot'] in names:
                    by_account.setdefault(row['phone'], []).append((names[row['bot']], row['outcome']))
        return by_account
