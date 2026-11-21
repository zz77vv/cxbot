import asyncio
import logging
import re
import time

from telethon.errors import FloodWaitError
from telethon.tl.types import KeyboardButtonCallback

from ..clients.userbot_manager import AccountUnavailable
from ..models.enums import AccountState, QueryState
from ..models.query import JRQueryResult, QueryReport
from ..target_bots import HW
from .jr.cache_service import jr_cache_key

log = logging.getLogger(__name__)
RESULT_WAIT = 90
EXPORT_WAIT = 120
CHARGE_LINE = re.compile(r'消耗\s*[0-9]+\s*盒能')
INSUFFICIENT_BALANCE = re.compile(r'^\s*(?:[❌⚠️💰💲]\s*)*(?:盒能|余额|积分)不足(?:[\s，。！!：:]|$)')
STATUS_MARKERS = ('数据查询中', '快捷查询完成', '安全验证', '请先验证', '请输入验证码')


def export_button(message):
    matches = []
    for row, buttons in enumerate(getattr(message, 'buttons', None) or []):
        for column, button in enumerate(buttons):
            raw = getattr(button, 'button', button)
            label = (getattr(raw, 'text', '') or '').strip().lstrip('📥📤⬇️ ').strip()
            if (isinstance(raw, KeyboardButtonCallback) and not raw.requires_password
                    and label in {'导出', '导出数据'}):
                matches.append((row, column))
    return matches[0] if len(matches) == 1 else None


def result_text(message):
    text = getattr(message, 'text', '') or ''
    if not text.strip() or getattr(message, 'media', None):
        return ''
    first = text.lstrip().splitlines()[0]
    if any(marker in first for marker in STATUS_MARKERS):
        return ''
    if ('查询结果' in first and '\n' in text) or CHARGE_LINE.search(text):
        return text
    return ''


def reply_matches(message, sender, related_ids):
    reply = getattr(message, 'reply_to_msg_id', None)
    return (getattr(message, 'sender_id', None) == sender and not getattr(message, 'out', False)
            and (reply is None or reply in related_ids))


def insufficient_balance(message):
    return bool(INSUFFICIENT_BALANCE.match(getattr(message, 'text', '') or ''))


class HWQueryService:
    def __init__(self, cfg, accounts, dialogue, queries, exports):
        self.cfg, self.accounts, self.dialogue = cfg, accounts, dialogue
        self.queries, self.exports = queries, exports
        self.lock = asyncio.Lock()

    async def execute(self, request, text):
        if self.lock.locked():
            return QueryReport('', QueryState.FAILED, 'hw_busy')
        async with self.lock:
            row, fresh = await self.queries.claim('hw:' + request, 'hw:' + jr_cache_key(text), time.time())
            task = row['task_id']
            if not fresh:
                return QueryReport(task, QueryState(row['state']), 'existing_task_no_resubmission', duplicate=True)
            for account in tuple(self.accounts.accounts.values()):
                if account.state != AccountState.ONLINE or account.lock.locked():
                    continue
                try:
                    async with self.accounts.operation(account, wait=False) as client:
                        blocker = await self.queries.start_hw(task, account.key)
                        if blocker:
                            await self.queries.transition(task, QueryState.FAILED, 'hw_pending_task')
                            return QueryReport(blocker, QueryState.PENDING_REVIEW, 'hw_pending_task')
                        return await self._query(task, account, client, text)
                except AccountUnavailable:
                    continue
            await self.queries.transition(task, QueryState.FAILED, 'account_unavailable_before_submission')
            return QueryReport(task, QueryState.FAILED, 'account_unavailable_before_submission')

    async def _query(self, task, account, client, text):
        reason = 'hw_send_uncertain'
        try:
            sent = await asyncio.wait_for(client.send_message(HW.bot, text), self.cfg.reply_timeout)
            reason = 'hw_result_timeout'
            await self.queries.transition(task, QueryState.SUBMITTED, sent_id=sent.id)
            sender = getattr(getattr(sent, 'peer_id', None), 'user_id', None)
            if sender is None:
                return await self._hold(task, 'unrecognized_target_reply')

            def candidate(message):
                headline = (getattr(message, 'text', '') or '').lstrip().partition('\n')[0]
                return (not any(marker in headline for marker in STATUS_MARKERS)
                        and reply_matches(message, sender, {sent.id})
                        and (export_button(message) is not None or bool(result_text(message))
                             or insufficient_balance(message)))

            result = await self.dialogue.wait(client, HW.bot, sent.id, candidate, RESULT_WAIT)
            if not result:
                return await self._hold(task, reason)
            button = export_button(result)
            if button is None and not result_text(result) and insufficient_balance(result):
                reason = 'hw_insufficient_balance'
                await self.queries.transition(task, QueryState.FAILED, reason, source_id=result.id)
                return QueryReport(task, QueryState.FAILED, reason)
            if button is not None:
                reason = 'hw_export_timeout'
                exported = await self._export(task, client, sent.id, result, button, sender)
                if not exported:
                    return await self._hold(task, reason)
                reason = 'document_download_failed'
                outcome = await self.exports.download(task, client, exported)
            else:
                outcome = JRQueryResult('text', result_text(result))
            await self.queries.complete(task, outcome, time.time(), 0, cache_result=False)
        except asyncio.CancelledError:
            await self._hold(task, 'cancelled_after_intent')
            raise
        except FloodWaitError as error:
            await self.accounts.flood_wait(account, error.seconds)
            return await self._hold(task, reason)
        except Exception as error:
            log.warning('HW request failed task=%s stage=%s error_type=%s', task, reason, type(error).__name__)
            if (await self.queries.get(task))['state'] == QueryState.SUCCESS:
                return QueryReport(task, QueryState.SUCCESS, 'delivery_requires_review')
            return await self._hold(task, reason)

        try:
            await self.exports.deliver(task, outcome, raw_text=True)
        except Exception as error:
            log.warning('HW delivery uncertain task=%s error_type=%s', task, type(error).__name__)
            return QueryReport(task, QueryState.SUCCESS, 'delivery_requires_review')
        return QueryReport(task, QueryState.SUCCESS, 'query_completed', outcome)

    async def _export(self, task, client, sent_id, result, button, sender):
        history = await self.dialogue.messages(client, HW.bot, limit=1)
        baseline = max([result.id, *(message.id for message in history)])
        await self.queries.transition(task, QueryState.EXPORTING, source_id=result.id)
        try:
            await asyncio.wait_for(result.click(*button), self.cfg.reply_timeout)
        except TimeoutError:
            # The callback may already have arrived upstream. Only read for the file.
            pass

        def is_file(message):
            name = getattr(getattr(message, 'file', None), 'name', '') or ''
            return (reply_matches(message, sender, {sent_id, result.id})
                    and getattr(message, 'document', None) and name.lower().endswith('.txt')
                    and (getattr(message, 'reply_to_msg_id', None) is not None
                         or '已导出为文件' in (getattr(message, 'text', '') or '')))

        return await self.dialogue.wait(client, HW.bot, baseline, is_file, EXPORT_WAIT)

    async def _hold(self, task, reason):
        await self.queries.transition(task, QueryState.PENDING_REVIEW, reason)
        return QueryReport(task, QueryState.PENDING_REVIEW, reason)
