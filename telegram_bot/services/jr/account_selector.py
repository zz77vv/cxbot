import asyncio
import time
from dataclasses import dataclass
from ...models.enums import AccountState, Outcome
from ...target_bots import JR


class RateLimitEncountered(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountExclusion:
    account: str
    reason: str
    retry_at: float = 0


class NoEligibleAccountError(RuntimeError):
    def __init__(self, exclusions: tuple[AccountExclusion, ...]):
        self.exclusions = exclusions
        self.retry_at = min((item.retry_at for item in exclusions if item.retry_at), default=0)
        reasons = {item.reason for item in exclusions}
        self.reason = next(iter(reasons)) if len(reasons) == 1 else 'no_eligible_account'
        if not exclusions:
            self.reason = 'no_accounts_loaded'
        super().__init__(self.reason)


class AccountSelector:
    def __init__(self, manager, balances, memberships, queries):
        self.manager, self.balances = manager, balances
        self.memberships, self.queries = memberships, queries
        self.semaphore = asyncio.Semaphore(manager.cfg.jr_balance_concurrency)

    async def rank(self):
        if any(a.unavailable_until > time.time() for a in self.manager.accounts.values()):
            raise RateLimitEncountered('telegram_flood_wait_no_account_rotation')
        async def evaluate(a):
            if a.state != AccountState.ONLINE:
                return AccountExclusion(a.key, 'account_not_online')
            if not await self.memberships.eligible(a.key):
                return AccountExclusion(a.key, 'daily_query_limit_reached')
            until = await self.queries.cooldown_until(a.key)
            if until > time.time():
                return AccountExclusion(a.key, 'jr_query_cooldown', until)
            if a.lock.locked():
                return AccountExclusion(a.key, 'account_busy', time.time() + 1)
            if await self.queries.unresolved_account(a.key):
                return AccountExclusion(a.key, 'account_has_unresolved_task')
            async with self.semaphore:
                balance = await self.balances.query(a, JR)
            return a, balance
        results = await asyncio.gather(*(evaluate(a) for a in self.manager.accounts.values()))
        evaluated = [item for item in results if not isinstance(item, AccountExclusion)]
        if any(item[1].outcome == Outcome.RATE_LIMITED for item in evaluated):
            raise RateLimitEncountered('telegram_flood_wait_no_account_rotation')
        ranked = sorted((item for item in evaluated if item[1].outcome == Outcome.SUCCESS),
                        key=lambda item: -item[1].value)
        if not ranked:
            exclusions = [item for item in results if isinstance(item, AccountExclusion)]
            exclusions.extend(AccountExclusion(a.key, f'balance_query_{balance.outcome.value}')
                              for a, balance in evaluated)
            raise NoEligibleAccountError(tuple(exclusions))
        return ranked
