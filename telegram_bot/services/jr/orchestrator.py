import asyncio
import logging
import time
from telethon.errors import FloodWaitError
from ...clients.userbot_manager import AccountUnavailable
from ...models.enums import QueryState
from ...models.query import QueryReport
from ...storage.repositories.query_repository import result_from_row
from ...utils.keyed_locks import KeyedLocks
from .cache_service import jr_cache_key, CacheInconsistent
from .account_selector import NoEligibleAccountError, RateLimitEncountered
from .pagination import CollectedPages, ManualVerification
from .result_parser import classify, find_buy_button
from .verification_service import VerificationError

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, manager, repository, selector, target, pagination, exports, cache, verification):
        self.manager, self.repository, self.selector = manager, repository, selector
        self.target, self.pagination, self.exports, self.cache = target, pagination, exports, cache
        self.verification = verification
        self.locks = KeyedLocks()
        self.accepting = True
        self.active = set()

    async def execute(self, request_id, request_text, *, progress=None):
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
                if progress is not None:
                    progress.task_id = task
                if not fresh:
                    if progress is not None and row['state'] == 'success' and row['delivery_state'] != 'delivered':
                        progress.review_required = True
                    return QueryReport(task, QueryState(row['state']), 'existing_task_no_resubmission', duplicate=True)
                return await self._run(task, request_text, progress=progress)
        finally:
            self.active.discard(current_task)

    async def _run(self, task, text, *, progress=None):
        cached = None
        try:
            if progress is not None:
                await progress.advance(2)
            async with self.cache.lock:
                cached = await self.cache.get(jr_cache_key(text))
                cached_needs_file = bool(cached and progress is not None and cached.kind == 'text'
                                         and not progress.text_fits(cached.text))
                if cached and not cached_needs_file:
                    await self.repository.complete(task, cached, time.time(), self.manager.cfg.cache_ttl, from_cache=True)
            if cached:
                if progress is not None:
                    await progress.advance(6, detail='已命中有效缓存，正在整理结果。')
                if cached_needs_file:
                    # Cached text has no file to lease. Prepare outside cache.lock,
                    # then record the new task reference without changing the cache.
                    cached = await self.exports.from_pages(task, CollectedPages(cached.text, 1, False),
                                                           status=cached.export_status, force_file=True)
                    async with self.cache.lock:
                        await self.repository.complete(task, cached, time.time(), self.manager.cfg.cache_ttl, from_cache=True)
                return await self._deliver(task, cached, 'cache_hit', progress=progress)
            ranked = await self.selector.rank()
            if not ranked:
                await self.repository.transition(task, QueryState.FAILED, 'no_eligible_account')
                return QueryReport(task, QueryState.FAILED, 'no_eligible_account')
            # Only one chosen account may receive the request. A limit never triggers rotation.
            account = ranked[0][0]
            async with self.manager.operation(account) as client:
                if not await self.repository.reserve(task, account.key, time.time()):
                    await self.repository.transition(task, QueryState.FAILED, 'account_busy_or_daily_limit')
                    return QueryReport(task, QueryState.FAILED, 'account_busy_or_daily_limit')
                if progress is not None:
                    await progress.advance(3)
                sent = await self.target.submit(client, text)
                query_after_id = sent.id
                await self.repository.transition(task, QueryState.SUBMITTED, sent_id=sent.id)
                reply = await self.target.ready(client, sent.id)
                if not reply:
                    return await self._hold(task, QueryState.PENDING_REVIEW, 'query_reply_timeout', progress=progress)
                await self.repository.transition(task, QueryState.SUBMITTED, source_id=reply.id)
                kind = classify(reply)
                if progress is not None:
                    await progress.advance(4)
                if kind == 'manual':
                    try:
                        reply, query_after_id = await self.verification.solve(client, reply, text)
                    except VerificationError as error:
                        log.warning('event=jr_verification_failed task=%s reason=%s', task, error)
                        return await self._hold(task, QueryState.MANUAL, f'verification_failed:{error}', progress=progress)
                    await self.repository.transition(task, QueryState.SUBMITTED,
                                                     sent_id=query_after_id, source_id=reply.id)
                    kind = classify(reply)
                if kind == 'failed':
                    await self.repository.transition(task, QueryState.FAILED, 'explicit_target_failure')
                    return QueryReport(task, QueryState.FAILED, 'explicit_target_failure')
                if kind != 'result':
                    return await self._hold(task, QueryState.PENDING_REVIEW, 'unrecognized_target_reply', progress=progress)
                buy = find_buy_button(reply)
                if buy:
                    await self.repository.transition(task, QueryState.PURCHASING, source_id=reply.id)
                    reply = await self.target.click_and_wait(client, reply, buy)
                    if not reply:
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'purchase_reply_timeout', progress=progress)
                    if classify(reply) == 'manual':
                        return await self._hold(task, QueryState.MANUAL, 'verification_after_purchase', progress=progress)
                    if classify(reply) != 'result' or find_buy_button(reply):
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'purchase_not_confirmed', progress=progress)
                if progress is not None:
                    await progress.advance(5)
                if getattr(reply, 'document', None):
                    if progress is not None:
                        await progress.advance(6, detail='正在接收目标返回的结果文件。')
                    result, _, _ = await self.exports.attempt(task, client, reply, query_after_id)
                    if not result:
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'document_download_failed', progress=progress)
                else:
                    await self.repository.transition(task, QueryState.COLLECTING, source_id=reply.id)
                    pages = await self.pagination.collect_result(client, reply, on_page=progress.page if progress is not None else None)
                    if not pages.text:
                        return await self._hold(task, QueryState.PENDING_REVIEW, 'empty_result', progress=progress)
                    if progress is not None:
                        await progress.advance(6)
                    result = await self.exports.from_pages(task, pages,
                        force_file=progress is not None and not progress.text_fits(pages.text))
                async with self.cache.lock:
                    await self.repository.complete(task, result, time.time(), self.manager.cfg.cache_ttl)
            return await self._deliver(task, result, 'query_completed', progress=progress)
        except asyncio.CancelledError:
            row = await self.repository.get(task)
            if row['state'] != 'success':
                state = QueryState.FAILED if row['state'] == 'prepared' else QueryState.PENDING_REVIEW
                await self.repository.transition(task, state, 'cancelled_before_submission' if state == QueryState.FAILED else 'cancelled_after_intent')
            raise
        except (CacheInconsistent, ManualVerification) as error:
            return await self._hold(task, QueryState.MANUAL, str(error), progress=progress)
        except NoEligibleAccountError as error:
            await self.repository.transition(task, QueryState.FAILED, error.reason)
            return QueryReport(task, QueryState.FAILED, error.reason)
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
            return await self._hold(task, QueryState.PENDING_REVIEW, f'uncertain_{type(error).__name__}', progress=progress)

    async def _hold(self, task, state, reason, *, progress=None):
        await self.repository.transition(task, state, reason)
        if progress is not None:
            await progress.fail(task, state, reason)
        else:
            await self.exports.notify(task, state.value, reason)
        return QueryReport(task, state, reason)

    async def _deliver(self, task, result, reason, *, progress=None):
        try:
            if progress is not None:
                await progress.advance(7)
                await self.exports.deliver(task, result, progress=progress)
            else:
                await self.exports.deliver(task, result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error('Delivery uncertain task=%s error_type=%s', task, type(error).__name__)
            if progress is not None:
                await progress.fail(task, QueryState.PENDING_REVIEW, 'result_saved_no_automatic_resend')
            else:
                await self.exports.notify(task, 'delivery_unknown', 'result_saved_no_automatic_resend')
            return QueryReport(task, QueryState.SUCCESS, 'delivery_requires_review', result)
        return QueryReport(task, QueryState.SUCCESS, reason, result)
