"""Lease an eligible account; never hold account locks while waiting for JR."""
import asyncio
import math
import time
from contextlib import AsyncExitStack, asynccontextmanager

from ...clients.userbot_manager import AccountUnavailable
from .account_selector import AccountExclusion, NoEligibleAccountError, RateLimitEncountered


class AccountScheduler:
    def __init__(self, manager, selector, queries):
        self.manager, self.selector, self.queries = manager, selector, queries

    async def _wait(self, until, progress):
        delay = max(0, until - time.time())
        if progress is not None:
            await progress.advance(2, detail=f'协议号暂忙或正在冷却，约 {math.ceil(delay)} 秒后重新选号。')
        await asyncio.sleep(min(delay, 60))

    def _check_telegram_limit(self):
        if any(a.unavailable_until > time.time() for a in self.manager.accounts.values()):
            raise RateLimitEncountered('telegram_flood_wait_no_account_rotation')

    @asynccontextmanager
    async def acquire(self, task, *, progress=None):
        while True:
            try:
                ranked = await self.selector.rank()
            except NoEligibleAccountError as error:
                if error.retry_at:
                    await self._wait(error.retry_at, progress)
                    continue
                raise
            exclusions = []
            for account, _ in ranked:
                self._check_telegram_limit()
                # AccountUnavailable can only be handled during entry, never
                # around yield: failures after submission belong to the caller.
                async with AsyncExitStack() as stack:
                    try:
                        client = await stack.enter_async_context(self.manager.operation(account, wait=False))
                    except AccountUnavailable:
                        self._check_telegram_limit()
                        exclusions.append(AccountExclusion(account.key, 'account_busy' if account.lock.locked()
                                                           else 'account_not_online',
                                                           time.time() + 1 if account.lock.locked() else 0))
                        continue
                    if await self.queries.reserve(task, account.key, time.time(), cooldown_seconds=60):
                        yield account, client
                        return
                    until = await self.queries.cooldown_until(account.key)
                    exclusions.append(AccountExclusion(account.key, 'account_busy_or_daily_limit',
                                                       until if until > time.time() else 0))
            error = NoEligibleAccountError(tuple(exclusions))
            if not error.retry_at:
                raise error
            await self._wait(error.retry_at, progress)
