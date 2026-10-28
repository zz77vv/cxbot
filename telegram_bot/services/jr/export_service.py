import asyncio
import logging
import re
import uuid
from pathlib import Path
from ...clients.dialogue import find_button
from ...models.query import JRQueryResult
from ...models.enums import QueryState
from ...target_bots import JR, JR_EXPORT_KEYWORDS, JR_FREE_EXPORT_KEYWORDS, JR_EXPORT_SUCCESS_KEYWORDS
from ...utils.paths import confined
from ...utils.message_format import split_text_by_lines
from .result_parser import normalize_jr_full_result

log = logging.getLogger(__name__)


def jr_original_file_name(message):
    name = (getattr(getattr(message, 'file', None), 'name', '') or '').strip()
    name = re.split(r'[\\/]', name)[-1].strip()
    return re.sub(r'[\x00-\x1f]', '_', name) or f'jr_export_{message.id}.bin'


def jr_message_matches_export_request(message, *, baseline_id, query_after_id, target_bot_id=None):
    if getattr(message, 'out', False) or message.id <= max(baseline_id, query_after_id):
        return False
    sender = getattr(message, 'sender_id', None)
    if sender is not None and target_bot_id is not None and sender != target_bot_id:
        return False
    reply_id = getattr(message, 'reply_to_msg_id', None)
    if reply_id is None:
        reply_id = getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)
    if reply_id is not None and reply_id < query_after_id:
        return False
    return bool(getattr(message, 'document', None) or (getattr(message, 'file', None) and getattr(message, 'media', None)))


class ExportService:
    def __init__(self, cfg, dialogue, queries, cache, bot=None):
        self.cfg, self.dialogue, self.queries, self.cache = cfg, dialogue, queries, cache
        self.bot = bot

    async def download(self, task, client, message):
        name = jr_original_file_name(message)
        # Fixed disk basename avoids both POSIX traversal and Windows reserved device names.
        async with self.cache.lock:
            path = confined(self.cfg.cache, self.cfg.cache / uuid.uuid4().hex / 'result.bin')
            path.parent.mkdir(parents=True, exist_ok=False)
            await self.queries.attach_file(task, str(path), name)
        downloaded = await asyncio.wait_for(client.download_media(message, file=str(path)), self.cfg.export_timeout)
        actual = confined(self.cfg.cache, downloaded or path)
        if actual != path or not actual.is_file():
            raise FileNotFoundError('download_missing_or_unexpected_path')
        return JRQueryResult('file', file_path=str(path), file_name=name, export_status='member')

    async def attempt(self, task, client, message, query_after_id):
        if getattr(message, 'document', None):
            return await self.download(task, client, message), 'member', ''
        button = find_button(message, JR_EXPORT_KEYWORDS)
        if not button:
            return None, 'unknown', 'export_button_missing'
        before = await self.dialogue.messages(client, JR.bot, limit=1)
        baseline = max([message.id, *(m.id for m in before)])
        await self.queries.transition(task, QueryState.EXPORTING, source_id=message.id)
        r, c, _ = button
        answer = await asyncio.wait_for(message.click(r, c), self.cfg.reply_timeout)
        feedback = getattr(answer, 'message', '') or ''
        if any(k in feedback for k in JR_FREE_EXPORT_KEYWORDS):
            return None, 'free', 'free_export_unavailable'
        success_seen = any(k in feedback for k in JR_EXPORT_SUCCESS_KEYWORDS)

        def accepted(candidate):
            nonlocal success_seen
            text = getattr(candidate, 'text', '') or ''
            if any(k in text for k in JR_EXPORT_SUCCESS_KEYWORDS):
                success_seen = True
            return (any(k in text for k in JR_FREE_EXPORT_KEYWORDS)
                    or jr_message_matches_export_request(candidate, baseline_id=baseline, query_after_id=query_after_id,
                                                         target_bot_id=getattr(message, 'sender_id', None)))
        candidate = await self.dialogue.wait(client, JR.bot, baseline, accepted, self.cfg.export_timeout)
        if candidate and any(k in (getattr(candidate, 'text', '') or '') for k in JR_FREE_EXPORT_KEYWORDS):
            return None, 'free', 'free_export_unavailable'
        if candidate:
            try:
                return await self.download(task, client, candidate), 'member', ''
            except (OSError, TimeoutError) as error:
                log.warning('Export download failed task=%s error_type=%s', task, type(error).__name__)
                return None, 'member', 'export_download_failed'
        return None, 'member' if success_seen else 'unknown', 'export_file_timeout'

    async def deliver(self, task, result):
        """Record intent before every send; ambiguous delivery never auto-retries."""
        row = await self.queries.get(task)
        if row['delivery_state'] != 'not_started':
            return row['delivery_state']
        async with self.cache.lock:
            await self.queries.delivery(task, 'sending', 0)
            try:
                if result.kind == 'file':
                    path = confined(self.cfg.cache, result.file_path or '')
                    with path.open('rb') as stream:
                        await self.bot.send_document(chat_id=self.cfg.admin_id, document=stream, filename=result.file_name)
                    await self.queries.delivery(task, 'delivered', 1)
                else:
                    cleaned = normalize_jr_full_result(result.text)
                    if not cleaned:
                        raise ValueError('empty_result')
                    pieces = split_text_by_lines(cleaned, limit=3600)
                    for index, piece in enumerate(pieces):
                        title = '✅ 巨人查询完成' if index == 0 else f'—— 续 {index + 1}/{len(pieces)} ——'
                        await self.bot.send_message(chat_id=self.cfg.admin_id, text=f'{title}\n\n{piece}')
                        await self.queries.delivery(task, 'sending', index + 1)
                    await self.queries.delivery(task, 'delivered', len(pieces))
                return 'delivered'
            except BaseException:
                await self.queries.delivery(task, 'unknown')
                raise

    async def notify(self, task, state, reason):
        try:
            await self.bot.send_message(chat_id=self.cfg.admin_id,
                text=f'⚠️ JR 任务 {task}\n状态：{state}\n原因：{reason}\n请核实目标机器人记录；系统不会自动重做。')
        except Exception as error:
            log.error('Admin notification failed task=%s error_type=%s', task, type(error).__name__)
