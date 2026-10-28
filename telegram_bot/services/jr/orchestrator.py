import asyncio
import logging
import time
from telethon.errors import FloodWaitError
from ...clients.userbot_manager import AccountUnavailable
from ...models.enums import QueryState
from ...models.query import JRQueryResult, QueryReport
from ...storage.repositories.query_repository import result_from_row
from ...utils.keyed_locks import KeyedLocks
from .cache_service import jr_cache_key, CacheInconsistent
from .account_selector import RateLimitEncountered
from .pagination import ManualVerification
from .result_parser import classify, find_buy_button

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, manager, repository, selector, target, pagination, exports, cache):
        self.manager, self.repository, self.selector = manager, repository, selector
        self.target, self.pagination, self.exports, self.cache = target, pagination, exports, cache
        self.locks = KeyedLocks()
        self.accepting = True
        self.active = set()

    async def execute(self, request_id, request_text):
        if not self.accepting:
            return QueryReport('', QueryState.FAILED, 'shutting_down')
        if not request_text.strip():
            return QueryReport('', QueryState.FAILED, 'empty_query')
        current_task = asyncio.current_task()
        self.active.add(current_task)
        try:
            async with self.locks.hold(jr_cache_key(request_text)):
                row, fresh = await self.repository.claim(str(request_id), jr_cache_key(request_text), time.time())
                task = row['task_id']
                if not fresh:
                    return QueryReport(task, QueryState(row['state']), 'existing_task_no_resubmission', duplicate=True)
                return await self._run(task, request_text)
        finally:
            self.active.discard(current_task)

    async def _run(self, task, text):
        try:
            async with self.cache.lock:
                cached = await self.cache.get(jr_cache_key(text))
                if cached:
                    await self.repository.complete(task, cached, time.time(), self.manager.cfg.cache_ttl, from_cache=True)
            if cached:
                return await self._deliver(task, cached, 'cache_hit')
            ranked = await self.selector.rank()
            if not ranked:
                await self.repository.transition(task, QueryState.FAILED, 'no_available_balance')
                return QueryReport(task, QueryState.FAILED, 'no_available_balance')
            # Only one chosen account may receive the request. A limit never triggers rotation.
            account = ranked[0][0]
            async with self.manager.operation(account) as client:
                if not await self.repository.reserve(task, account.key, time.time()):
                    await self.repository.transition(task, QueryState.FAILED, 'account_busy_or_daily_limit')
                    return QueryReport(task, QueryState.FAILED, 'account_busy_or_daily_limit')
                sent = await self.target.submit(client, text)
                await self.repository.transition(task, QueryState.SUBMITTED, sent_id=sent.id)
                reply = await self.target.ready(client, sent.id)
                if not reply:
                    return await self._hold(task, QueryState.PENDING_REVIEW, 'query_reply_timeout')
                await self.repository.transition(task, QueryState.SUBMITTED, source_id=reply.id)
                kind = classify(reply)
                if kind == 'manual':
                    return await self._hold(task, QueryState.MANUAL, 'security_verification_requires_human')
                if kind == 'failed':
                    await self.repository.transition(task, QueryState.FAILED, 'explicit_target_failure')
                    return QueryReport(task, QueryState.FAILED, 'explicit_target_failure')
                if kind != 'result':
                    return await self._hold(task, QueryState.PENDING_REVIEW, 'unrecognized_target_reply')
                buy = find_buy_button(reply)
                if buy:
                    await self.repository.transition(task, QueryState.PURCHASING, source_id=reply.id)
                    reply = await self.target.click_and_wait(client, reply, buy)
                    if not reply:
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'purchase_reply_timeout')
                    if classify(reply) == 'manual':
                        return await self._hold(task, QueryState.MANUAL, 'verification_after_purchase')
                    if classify(reply) != 'result' or find_buy_button(reply):
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'purchase_not_confirmed')
                exported, status, reason = await self.exports.attempt(task, client, reply, sent.id)
                if exported:
                    result = exported
                else:
                    await self.repository.transition(task, QueryState.COLLECTING, source_id=reply.id)
                    content = await self.pagination.collect(client, reply)
                    if not content:
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'empty_result')
                    result = JRQueryResult('text', content, export_status=status, fallback_reason=reason)
                async with self.cache.lock:
                    await self.repository.complete(task, result, time.time(), self.manager.cfg.cache_ttl)
            return await self._deliver(task, result, 'query_completed')
        except asyncio.CancelledError:
            row = await self.repository.get(task)
            if row['state'] != 'success':
                state = QueryState.FAILED if row['state'] == 'prepared' else QueryState.PENDING_REVIEW
                await self.repository.transition(task, state, 'cancelled_before_submission' if state == QueryState.FAILED else 'cancelled_after_intent')
            raise
        except (CacheInconsistent, ManualVerification) as error:
            return await self._hold(task, QueryState.MANUAL, str(error))
        except RateLimitEncountered:
            await self.repository.transition(task, QueryState.FAILED, 'telegram_flood_wait_no_submission')
            return QueryReport(task, QueryState.FAILED, 'telegram_flood_wait_no_submission')
        except AccountUnavailable:
            await self.repository.transition(task, QueryState.FAILED, 'account_unavailable_before_submission')
            return QueryReport(task, QueryState.FAILED, 'account_unavailable_before_submission')
        except Exception as error:
            log.error('JR error task=%s error_type=%s', task, type(error).__name__)
            row = await self.repository.get(task)
            if row['state'] == 'success':
                return QueryReport(task, QueryState.SUCCESS, 'delivery_requires_review', result_from_row(row))
            if row['state'] == 'prepared':
                await self.repository.transition(task, QueryState.FAILED, 'error_before_submission')
                return QueryReport(task, QueryState.FAILED, 'error_before_submission')
            return await self._hold(task, QueryState.PENDING_REVIEW, f'uncertain_{type(error).__name__}')

    async def _hold(self, task, state, reason):
        await self.repository.transition(task, state, reason)
        await self.exports.notify(task, state.value, reason)
        return QueryReport(task, state, reason)

    async def _deliver(self, task, result, reason):
        try:
            await self.exports.deliver(task, result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error('Delivery uncertain task=%s error_type=%s', task, type(error).__name__)
            await self.exports.notify(task, 'delivery_unknown', 'result_saved_no_automatic_resend')
            return QueryReport(task, QueryState.SUCCESS, 'delivery_requires_review', result)
        return QueryReport(task, QueryState.SUCCESS, reason, result)
