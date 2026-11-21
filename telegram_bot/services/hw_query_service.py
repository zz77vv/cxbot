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


def from_sender(message, sender):
    return getattr(message, 'sender_id', None) == sender and not getattr(message, 'out', False)


def reply_matches(message, sender, related_ids):
    reply = getattr(message, 'reply_to_msg_id', None)
    return from_sender(message, sender) and (reply is None or reply in related_ids)


def linked_reply(message, sender, related_ids):
    header = getattr(message, 'reply_to', None)
    peer = getattr(header, 'reply_to_peer_id', None)
    reply = getattr(message, 'reply_to_msg_id', None)
    if reply is None:
        reply = getattr(header, 'reply_to_msg_id', None)
    return (from_sender(message, sender) and reply in related_ids
            and (peer is None or getattr(peer, 'user_id', None) == sender))


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

            unconfirmed_text = False

            def candidate(message):
                nonlocal unconfirmed_text
                headline = (getattr(message, 'text', '') or '').lstrip().partition('\n')[0]
                if any(marker in headline for marker in STATUS_MARKERS) or not reply_matches(message, sender, {sent.id}):
                    return False
                if export_button(message) is not None or insufficient_balance(message):
                    return True
                if result_text(message):
                    if linked_reply(message, sender, {sent.id}):
                        return True
                    unconfirmed_text = True
                return False

            result = await self.dialogue.wait(client, HW.bot, sent.id, candidate, RESULT_WAIT)
            if not result:
                return await self._hold(task, 'hw_result_unconfirmed' if unconfirmed_text else reason)
            button = export_button(result)
            if button is None and not result_text(result) and insufficient_balance(result):
                reason = 'hw_insufficient_balance'
                await self.queries.transition(task, QueryState.FAILED, reason, source_id=result.id)
                return QueryReport(task, QueryState.FAILED, reason)
            if button is not None:
                reason = 'hw_export_timeout'
                exported, unconfirmed = await self._export(task, client, sent.id, result, button, sender)
                if not exported:
                    return await self._hold(task, 'hw_export_unconfirmed' if unconfirmed else reason)
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

        related_ids = {sent_id}
        if linked_reply(result, sender, related_ids):
            related_ids.add(result.id)
        unconfirmed = False

        def is_file(message):
            nonlocal unconfirmed
            name = getattr(getattr(message, 'file', None), 'name', '') or ''
            if not (from_sender(message, sender) and getattr(message, 'document', None)
                    and name.lower().endswith('.txt')):
                return False
            if linked_reply(message, sender, related_ids):
                return True
            unconfirmed = True
            return False

        file = await self.dialogue.wait(client, HW.bot, baseline, is_file, EXPORT_WAIT)
        return file, unconfirmed

    async def _hold(self, task, reason):
        await self.queries.transition(task, QueryState.PENDING_REVIEW, reason)
        return QueryReport(task, QueryState.PENDING_REVIEW, reason)
