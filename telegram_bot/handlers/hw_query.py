import asyncio
import logging

from telegram.error import TelegramError

from .guards import protected
from ..models.enums import QueryState
from ..utils.user_messages import reason_text

log = logging.getLogger(__name__)


def report_note(report):
    note = reason_text(report.reason)
    if report.reason == 'hw_export_unconfirmed':
        note += '\n请在 /jrtasks 查看候选文件，核对后可回传原 TXT。'
    elif report.reason in {'hw_pending_task', 'hw_result_unconfirmed', 'hw_export_timeout'}:
        note += '\n请在 /jrtasks 中核实或解除。'
    if report.task_id and report.reason != 'query_completed':
        note += f'\n任务：{report.task_id}'
    return note


async def run_batch(ctx, message, request, lines):
    items = list(dict.fromkeys(lines))
    count = len(items)
    progress = await message.reply_text(f'⏳ HWQ 批量查询，共 {count} 条（已去除空行和重复项）。')
    success = failed = review = 0
    stop = ''
    for index, text in enumerate(items, 1):
        if not ctx.requests.accepting:
            stop = reason_text('shutting_down')
            break
        try:
            await progress.edit_text(f'⏳ HWQ 批量查询，第 {index}/{count} 条。\n成功 {success}，失败 {failed}。')
        except TelegramError:
            log.warning('HW batch progress update failed index=%s', index)
        if not ctx.requests.accepting:
            stop = reason_text('shutting_down')
            break
        # Keep the first item's durable ID compatible with older /hw updates.
        item_request = request if index == 1 else f'{request}:batch:{index}'
        try:
            report = await ctx.hw.execute(item_request, text)
        except Exception as error:
            log.warning('HW batch interrupted index=%s error_type=%s', index, type(error).__name__)
            review += 1
            stop = f'第 {index} 条状态不确定，请先核实。'
            break
        if report.duplicate or report.reason in {'hw_busy', 'hw_pending_task', 'account_unavailable_before_submission'}:
            stop = report_note(report)
            break
        if report.state == QueryState.SUCCESS and report.reason != 'delivery_requires_review':
            success += 1
        elif report.state == QueryState.FAILED:
            failed += 1
            if report.reason == 'hw_insufficient_balance':
                stop = f'第 {index} 条余额不足。{report_note(report)}'
                break
        else:
            review += 1
            stop = f'第 {index} 条待核实。{report_note(report)}'
            break

    remaining = count - success - failed - review
    title = 'HWQ 批量已暂停' if stop else 'HWQ 批量已完成'
    summary = f'{title}，共 {count} 条。\n成功 {success}，失败 {failed}，待核实 {review}，未提交 {remaining}。'
    if stop:
        summary += f'\n{stop}\n不会重发查询或重复点导出。'
    try:
        await progress.edit_text(summary)
    except TelegramError:
        await message.reply_text(summary)


def make_handler(ctx):
    active = asyncio.Lock()

    @protected(ctx)
    async def handle(update, context):
        message = update.effective_message
        if not update.effective_chat or update.effective_chat.type != 'private':
            await message.reply_text('请在机器人私聊中使用 /hw。')
            return
        parts = (message.text or '').split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await message.reply_text('用法：/hw 查询内容\n批量查询时，每行一条。')
            return
        if active.locked():
            await message.reply_text(reason_text('hw_busy'))
            return
        request = f'{update.effective_chat.id}:{update.update_id}'
        lines = [line.strip() for line in parts[1].splitlines() if line.strip()]
        async with active:
            if len(lines) > 1:
                await run_batch(ctx, message, request, lines)
                return
            progress = await message.reply_text('⏳ 正在查询 HWQ…')
            report = await ctx.hw.execute(request, lines[0])
            note = report_note(report)
            try:
                await progress.edit_text(note)
            except TelegramError:
                await message.reply_text(note)

    return handle
