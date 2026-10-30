import re
from .guards import protected
from ..utils.user_messages import state_text, reason_text

JR_ROUTE = re.compile(r'^/jr(?:@\w+)?(?:\+|\s|$)', re.I)


def parse_request(text, bot_username=None):
    match = re.match(r'^/jr(?:@(?P<bot>\w+))?(?:(?:\+|\s+)(?P<body>.*))?$', (text or '').strip(), re.I | re.S)
    if not match:
        return None
    if match['bot'] and bot_username and match['bot'].casefold() != bot_username.casefold():
        return None
    return (match['body'] or '').strip()


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        text = parse_request(update.effective_message.text, context.bot.username)
        if text is None:
            return
        if not text:
            await update.effective_message.reply_text('用法：/jr 查询内容\n或 /jr+查询内容')
            return
        status = await update.effective_message.reply_text('⏳ 正在检查缓存、任务状态和可用账号...')
        # update_id is durable across retries; identical updates cannot submit twice.
        report = await ctx.jr.execute(f'{update.effective_chat.id}:{update.update_id}', text)
        await status.edit_text(f'任务：{report.task_id or "未创建"}\n状态：{state_text(report.state)}\n说明：{reason_text(report.reason)}')
    return handle
