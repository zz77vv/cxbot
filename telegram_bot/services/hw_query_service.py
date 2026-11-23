import asyncio
import json
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
EXPORT_LINK_GRACE = 3
CHARGE_LINE = re.compile(r'消耗\s*[0-9]+\s*盒能')
INSUFFICIENT_BALANCE = re.compile(r'^\s*(?:[❌⚠️💰💲]\s*)*(?:盒能|余额|积分)不足(?:[\s，。！!：:]|$)')
STATUS_MARKERS = ('数据查询中', '快捷查询完成', '安全验证', '请先验证', '请输入验证码')
EXPORT_REVIEW_REASONS = {'hw_export_timeout', 'hw_export_unconfirmed'}


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


def same_chat_reference(message, sender):
    peer = getattr(getattr(message, 'reply_to', None), 'reply_to_peer_id', None)
    return peer is None or getattr(peer, 'user_id', None) == sender


def reply_id(message):
    reply = getattr(message, 'reply_to_msg_id', None)
    return reply if reply is not None else getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)


def reply_matches(message, sender, related_ids):
    reply = reply_id(message)
    return from_sender(message, sender) and same_chat_reference(message, sender) and (
        reply is None or reply in related_ids)


def linked_reply(message, sender, related_ids):
    return from_sender(message, sender) and same_chat_reference(message, sender) and reply_id(message) in related_ids


def insufficient_balance(message):
    return bool(INSUFFICIENT_BALANCE.match(getattr(message, 'text', '') or ''))


def candidate_metadata(message):
    file = getattr(message, 'file', None)
    document = getattr(message, 'document', None)
    document_id = getattr(document, 'id', None)
    name = getattr(file, 'name', '') or ''
    if not document_id or not name.lower().endswith('.txt'):
        return None
    date = getattr(message, 'date', None)
    return {'id': message.id, 'sender': message.sender_id, 'document': document_id,
            'name': ' '.join(name.split())[:160], 'size': getattr(file, 'size', None),
            'at': date.timestamp() if date else time.time()}


def is_export_review(row):
    return bool(row and row['request_id'].startswith('hw:') and row['state'] == QueryState.PENDING_REVIEW
                and not row['result_type'] and row['reason'] in EXPORT_REVIEW_REASONS)


def export_candidates(row):
    if not is_export_review(row) or not row['result_text']:
        return []
    try:
        rows = json.loads(row['result_text'])
    except (ValueError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


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
                exported, unconfirmed, candidates = await self._export(task, client, sent.id, result, button, sender)
                if not exported:
                    report = await self._hold(task, 'hw_export_unconfirmed' if unconfirmed else reason)
                    if candidates:
                        await self.queries.save_hw_export_candidates(task, candidates)
                    return report
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
        candidates = {}

        def is_file(message, *, accept_candidate=False):
            nonlocal unconfirmed
            name = getattr(getattr(message, 'file', None), 'name', '') or ''
            if not (from_sender(message, sender) and getattr(message, 'document', None)
                    and name.lower().endswith('.txt')):
                return False
            if linked_reply(message, sender, related_ids):
                return True
            unconfirmed = True
            if len(candidates) < 5 and reply_matches(message, sender, {sent_id, result.id}):
                metadata = candidate_metadata(message)
                if metadata:
                    candidates[message.id] = metadata
                    return accept_candidate
            return False

        deadline = time.monotonic() + EXPORT_WAIT
        file = await self.dialogue.wait(
            client, HW.bot, baseline,
            lambda message: is_file(message, accept_candidate=True), EXPORT_WAIT,
        )
        if file and not linked_reply(file, sender, related_ids):
            grace = min(EXPORT_LINK_GRACE, max(0, deadline - time.monotonic()))
            try:
                file = await self.dialogue.wait(client, HW.bot, baseline, is_file, grace) if grace else None
            except OSError:
                # The candidate is already known. Preserve it if the extra read fails.
                file = None
        return file, unconfirmed, list(candidates.values())

    async def find_export_candidates(self, row):
        """Administrator-initiated read of the existing upstream conversation."""
        if not is_export_review(row) or not row['source_id']:
            return False
        account = self.accounts.accounts.get(row['account'])
        if not account:
            return False
        try:
            async with self.accounts.operation(account, wait=False) as client:
                source = await self.dialogue.messages(client, HW.bot, ids=row['source_id'])
                sender = getattr(source, 'sender_id', None)
                if not sender or getattr(source, 'out', False):
                    return False
                history = await self.dialogue.messages(client, HW.bot, min_id=row['source_id'], reverse=True, limit=30)
                found = {item['id']: item for item in export_candidates(row)}
                for message in history:
                    date = getattr(message, 'date', None)
                    if (message.id <= row['source_id'] or not date
                            or not row['created_at'] <= date.timestamp() <= row['updated_at'] + 600
                            or not reply_matches(message, sender, {row['source_id'], row['sent_id']})):
                        continue
                    item = candidate_metadata(message)
                    if item and len(found) < 5:
                        found[item['id']] = item
                if found:
                    await self.queries.save_hw_export_candidates(row['task_id'], list(found.values()))
                return bool(found)
        except Exception as error:
            log.warning('HW candidate search failed error_type=%s', type(error).__name__)
            return False

    async def deliver_export_candidate(self, row, message_id):
        choice = next((item for item in export_candidates(row) if item['id'] == message_id), None)
        account = self.accounts.accounts.get(row['account']) if choice else None
        if not account:
            return '文件不可用，请在上游原聊天核对。'
        try:
            async with self.accounts.operation(account, wait=False) as client:
                task = row['task_id']
                if not await self.queries.claim_hw_export_candidate(task, row['result_text']):
                    return '任务状态已变化，请刷新列表。'
                try:
                    message = await self.dialogue.messages(client, HW.bot, ids=message_id)
                    if (not message or message.id != message_id or not from_sender(message, choice['sender'])
                            or getattr(getattr(message, 'document', None), 'id', None) != choice['document']):
                        raise ValueError('hw_candidate_changed')
                    result = await self.exports.download(task, client, message)
                    await self.queries.complete(task, result, time.time(), 0, cache_result=False)
                except asyncio.CancelledError:
                    await self._hold(task, row['reason'])
                    raise
                except Exception as error:
                    log.warning('HW confirmed file read failed task=%s error_type=%s', task, type(error).__name__)
                    await self._hold(task, row['reason'])
                    return '未能读取选定的原文件。可以核对后再取一次，不会重新查询或导出。'
        except AccountUnavailable:
            return '原协议号暂不可用，请稍后再试。'
        try:
            status = await self.exports.deliver(task, result)
        except Exception:
            return '文件已保存，交付待确认。请在任务列表核对。'
        return '已回传选定的原 TXT。' if status == 'delivered' else '交付待确认，请刷新任务列表。'

    async def _hold(self, task, reason):
        await self.queries.transition(task, QueryState.PENDING_REVIEW, reason)
        return QueryReport(task, QueryState.PENDING_REVIEW, reason)
