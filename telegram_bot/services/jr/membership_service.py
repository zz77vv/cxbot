import time
from ...target_bots import JR_FREE_DAILY_LIMIT


class MembershipService:
    def __init__(self, repository):
        self.repository = repository

    async def state(self, account):
        return await self.repository.state(account, time.time())

    async def eligible(self, account):
        status, checked_at, used = await self.state(account)
        return status == 'member' or used < JR_FREE_DAILY_LIMIT
