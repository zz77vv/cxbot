"""Sequential batches using the existing single-query service."""
import asyncio
import logging
import re

from .guards import protected
from ..models.enums import QueryState
from ..services.jr.progress import JRProgress

log = logging.getLogger(__name__)


def parse_items(text):
    match = re.match(r'^/(?:jrbatch|jr)(?:@\w+)?(?:\+|\s+)(.*)$', (text or '').strip(), re.I | re.S)
    if not match:
        return []
    return list(dict.fromkeys(line.strip() for line in match[1].splitlines() if line.strip()))


def make_handler(ctx):
    active = set()

    @protected(ctx)
    async def handle(update, context):
        message = update.effective_message
        items = parse_items(message.text)
        if not items:
            await message.reply_text('用法：/jrbatch 后每行一条查询内容。\n每条沿用 /jr 查询和付费规则。')
            return
        batch_id = f'{update.effective_chat.id}:{update.update_id}:batch'
        if batch_id in active:
            return
        active.add(batch_id)
        completed = success = failed = review = 0
        summary = None
        try:
            summary = await message.reply_text(f'批量查询共 {len(items)} 条，已去除空行和重复项。\n逐条执行，沿用 /jr 查询和付费规则。')
            for index, text in enumerate(items, 1):
                if not ctx.jr.accepting:
                    break
                status = await message.reply_text(f'批量第 {index}/{len(items)} 条\n{JRProgress.initial_text()}')
                progress = JRProgress(status, ctx.config.reply_timeout)
                report = await ctx.jr.execute(f'{batch_id}:{index}', text, progress=progress)
                await progress.finish(report)
                completed += 1
                if report.state == QueryState.SUCCESS and not progress.review_required and report.reason != 'delivery_requires_review':
                    success += 1
                elif report.state == QueryState.FAILED:
                    failed += 1
                else:
                    review += 1
                try:
                    await summary.edit_text(f'批量已处理 {completed}/{len(items)} 条\n成功 {success}，失败 {failed}，待核实 {review}')
                except Exception as error:
                    log.warning('event=batch_progress_failed error_type=%s', type(error).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error('event=batch_interrupted error_type=%s', type(error).__name__)
            if summary is not None:
                try:
                    await summary.edit_text(f'批量中断，已处理 {completed}/{len(items)} 条。\n当前第 {completed + 1} 条需核对任务状态，其余尚未执行。')
                except Exception:
                    log.warning('event=batch_interruption_notice_failed')
        finally:
            active.discard(batch_id)

    return handle
