import asyncio
import time
from ...models.enums import AccountState, Outcome
from ...target_bots import JR


class RateLimitEncountered(RuntimeError):
    pass


class AccountSelector:
    def __init__(self, manager, balances, memberships, queries):
        self.manager, self.balances = manager, balances
        self.memberships, self.queries = memberships, queries
        self.semaphore = asyncio.Semaphore(manager.cfg.jr_balance_concurrency)

    async def rank(self):
        if any(a.unavailable_until > time.time() for a in self.manager.accounts.values()):
            raise RateLimitEncountered('telegram_flood_wait_no_account_rotation')
        async def evaluate(a):
            if a.state != AccountState.ONLINE or not await self.memberships.eligible(a.key):
                return None
            if await self.queries.unresolved_account(a.key):
                return None
            async with self.semaphore:
                balance = await self.balances.query(a, JR)
            return a, balance
        results = await asyncio.gather(*(evaluate(a) for a in self.manager.accounts.values()))
        if any(item and item[1].outcome == Outcome.RATE_LIMITED for item in results):
            raise RateLimitEncountered('telegram_flood_wait_no_account_rotation')
        return sorted((item for item in results if item and item[1].outcome == Outcome.SUCCESS),
                      key=lambda item: -item[1].value)
