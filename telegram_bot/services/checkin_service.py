import asyncio
import logging
import random
import time
from telethon.errors import FloodWaitError
from ..clients.dialogue import find_button, signature
from ..clients.userbot_manager import AccountUnavailable
from ..models.account import OperationResult
from ..models.enums import Outcome
from ..target_bots import TARGETS, MIN_INTERVAL, RANDOM_OFFSET_MAX

log = logging.getLogger(__name__)


class CheckinService:
    def __init__(self, manager, repository, dialogue, rng=None):
        self.manager, self.repository, self.dialogue = manager, repository, dialogue
        self.rng = rng or random.Random()
        self.tasks = {}

    async def perform(self, account, target):
        """Only causally fresh replies can confirm success. Sends/clicks never retried."""
        client = account.client
        sent = await asyncio.wait_for(client.send_message(target.bot, target.checkin_cmd), self.manager.cfg.reply_timeout)
        last_message, old_signature = None, None
        for label in target.checkin_buttons:
            message = await self.dialogue.wait(client, target.bot, sent.id,
                lambda m: bool(find_button(m, (label,))), 45,
                original=last_message, old_signature=old_signature)
            if not message:
                return OperationResult(Outcome.FAILED, 'checkin_button_missing')
            r, c, _ = find_button(message, (label,))
            old_signature = signature(message)
            last_message = message
            await asyncio.wait_for(message.click(r, c), self.manager.cfg.reply_timeout)
        # Broad legacy balance keywords alone are not proof that a button click succeeded.
        def confirmed(m):
            text = getattr(m, 'text', '') or ''
            if any(k in text for k in ('签到失败', '操作失败', '请求失败', '未获得')):
                return False
            if target.checkin_buttons and m.id == getattr(last_message, 'id', None) and signature(m) == old_signature:
                return False
            return any(k in text for k in target.success_keywords)
        reply = await self.dialogue.wait(client, target.bot,
            last_message.id if last_message else sent.id, confirmed, target.checkin_timeout,
            original=last_message, old_signature=old_signature)
        return OperationResult(Outcome.SUCCESS, 'confirmed_by_reply') if reply else OperationResult(Outcome.PENDING_REVIEW, 'checkin_reply_timeout')

    async def run_one(self, account, target):
        await self.repository.initialize(account.key, target.key, time.time(), self.rng)
        try:
            async with self.manager.operation(account):
                row = await self.repository.get(account.key, target.key)
                baseline = await self.repository.legacy_baseline(account.key, target.key)
                last = max(row['last_attempt'] or 0, row['last_success'] or 0, baseline or 0)
                now = time.time()
                if last and now < last + MIN_INTERVAL:
                    return OperationResult(Outcome.SKIPPED, 'minimum_24h_interval')
                await self.repository.attempted(account.key, target.key, now)
                try:
                    result = await self.perform(account, target)
                except asyncio.CancelledError:
                    result = OperationResult(Outcome.PENDING_REVIEW, 'cancelled_after_intent', 'CancelledError')
                    await self.repository.finish(account.key, target.key, time.time(), result, now + MIN_INTERVAL + self.rng.uniform(0, RANDOM_OFFSET_MAX))
                    raise
                except FloodWaitError as error:
                    await self.manager.flood_wait(account, error.seconds)
                    result = OperationResult(Outcome.RATE_LIMITED, 'telegram_flood_wait', 'FloodWaitError', retry_after=error.seconds)
                except TimeoutError:
                    result = OperationResult(Outcome.PENDING_REVIEW, 'checkin_transport_timeout', 'TimeoutError')
                except Exception as error:
                    log.warning('Checkin error target=%s error_type=%s', target.name, type(error).__name__)
                    result = OperationResult(Outcome.UNKNOWN, 'checkin_error', type(error).__name__)
                finished = time.time()
                next_at = max(finished + MIN_INTERVAL + self.rng.uniform(0, RANDOM_OFFSET_MAX), account.unavailable_until)
                await self.repository.finish(account.key, target.key, finished, result, next_at)
                return result
        except AccountUnavailable:
            return OperationResult(Outcome.UNAVAILABLE, 'account_unavailable')

    async def manual(self):
        async def per_account(a):
            await asyncio.sleep(self.rng.uniform(0, min(15, len(self.manager.accounts))))
            return a.key, [(t.name, await self.run_one(a, t)) for t in TARGETS]
        return await asyncio.gather(*(per_account(a) for a in self.manager.accounts.values()))

    async def scheduler(self, account, target):
        while True:
            try:
                row = await self.repository.initialize(account.key, target.key, time.time(), self.rng)
                await asyncio.sleep(max(0, row['next_at'] - time.time()))
                result = await self.run_one(account, target)
                if result.outcome in {Outcome.UNAVAILABLE, Outcome.SKIPPED}:
                    await asyncio.sleep(max(60, min(600, account.unavailable_until - time.time())))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error('Scheduler failure target=%s error_type=%s', target.name, type(error).__name__)
                await asyncio.sleep(60)

    def start(self, registry):
        for account in self.manager.accounts.values():
            for target in TARGETS:
                key = (account.key, target.key)
                if key not in self.tasks or self.tasks[key].done():
                    self.tasks[key] = registry.start(self.scheduler(account, target), f'checkin:{account.key}:{target.name}')
