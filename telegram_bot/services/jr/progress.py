"""Request-scoped presentation. It never sends queries or additional status messages."""
import asyncio
import logging
import time
from telegram.error import BadRequest
from ...models.enums import QueryState
from ...utils.message_format import utf16_len
from ...utils.user_messages import state_text, reason_text
from .result_parser import normalize_jr_full_result

log = logging.getLogger(__name__)
STAGES = ('接收查询任务', '检查缓存与协议号', '提交巨人查询', '处理验证与购买',
          '收集查询结果', '整理查询结果', '交付查询结果')


class JRProgress:
    def __init__(self, message, timeout=30, *, interval=0.8):
        self.message, self.timeout, self.interval = message, timeout, interval
        self.task_id = ''
        self.last_text = self.initial_text()
        self.last_edit = 0.0
        self.lock = asyncio.Lock()
        self.text_delivered = False
        self.file_sent = False
        self.deleted = False
        self.delete_attempted = False
        self.review_required = False

    @staticmethod
    def initial_text():
        return '⏳ 1/7 接收查询任务\n正在解析命令并检查重复任务。'

    @staticmethod
    def text_fits(text):
        return utf16_len('✅ 巨人查询完成\n\n' + normalize_jr_full_result(text)) <= 4096

    async def _edit(self, text, *, strict=False):
        async with self.lock:
            if self.deleted:
                if strict:
                    raise RuntimeError('progress_message_deleted')
                return False
            if text == self.last_text:
                return True
            delay = self.interval - (time.monotonic() - self.last_edit)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await asyncio.wait_for(self.message.edit_text(text), self.timeout if strict else min(self.timeout, 5))
            except BadRequest as error:
                if 'message is not modified' not in str(error).lower():
                    if strict:
                        raise
                    log.warning('event=progress_edit_failed error_type=%s', type(error).__name__)
                    return False
            except Exception as error:
                if strict:
                    raise
                log.warning('event=progress_edit_failed error_type=%s', type(error).__name__)
                return False
            finally:
                self.last_edit = time.monotonic()
            self.last_text = text
            return True

    async def advance(self, stage, *, current=None, total=None, detail=''):
        text = f'⏳ {stage}/7 {STAGES[stage - 1]}'
        if self.task_id:
            text += f'\n任务：{self.task_id}'
        if current is not None:
            text += f'\n已收集 {current}/{total} 页' if total is not None else f'\n已收集 {current} 页，继续确认末页。'
        if detail:
            text += '\n' + detail
        await self._edit(text)

    async def page(self, current, total):
        await self.advance(5, current=current, total=total)

    async def deliver_text(self, text):
        if not self.text_fits(text):
            raise ValueError('progress_text_too_long')
        await self._edit('✅ 巨人查询完成\n\n' + text, strict=True)
        self.text_delivered = True

    async def delete_after_file(self):
        if self.delete_attempted:
            return
        self.delete_attempted = True
        try:
            await asyncio.wait_for(self.message.delete(), self.timeout)
            self.deleted = True
        except Exception as error:
            log.warning('event=progress_delete_failed task=%s error_type=%s', self.task_id, type(error).__name__)

    async def fail(self, task, state, reason):
        await self._edit(f'任务：{task or "未创建"}\n状态：{state_text(state)}\n说明：{reason_text(reason)}')

    async def finish(self, report):
        if self.review_required or report.reason == 'delivery_requires_review':
            await self.fail(report.task_id, QueryState.PENDING_REVIEW, 'result_saved_no_automatic_resend')
            return
        if self.deleted or self.text_delivered:
            return
        if self.file_sent:
            await self._edit('✅ 结果文件已发送。\n未能删除本进度消息，可手动删除；系统不会重复发送文件。')
            return
        state = QueryState.PENDING_REVIEW if report.reason == 'delivery_requires_review' else report.state
        await self.fail(report.task_id, state, report.reason)
