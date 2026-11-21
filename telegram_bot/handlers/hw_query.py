from .guards import protected
from ..utils.user_messages import reason_text


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        message = update.effective_message
        if not update.effective_chat or update.effective_chat.type != 'private':
            await message.reply_text('请在机器人私聊中使用 /hw。')
            return
        parts = (message.text or '').split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await message.reply_text('用法：/hw 查询内容')
            return
        progress = await message.reply_text('⏳ 正在查询 HWQ…')
        request = f'{update.effective_chat.id}:{update.update_id}'
        report = await ctx.hw.execute(request, parts[1].strip())
        note = reason_text(report.reason)
        if report.reason == 'hw_pending_task':
            note += '\n请在 /jrtasks 中核实或解除。'
        if report.task_id and report.reason != 'query_completed':
            note += f'\n任务：{report.task_id}'
        await progress.edit_text(note)

    return handle
