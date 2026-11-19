import re

from .guards import protected
from ..utils.message_format import reply_long
from ..utils.user_messages import reason_text, state_text


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        message = update.effective_message
        if not update.effective_chat or update.effective_chat.type != 'private':
            await message.reply_text('请在机器人私聊中使用 /jrtasks。')
            return

        queries = ctx.jr.repository
        args = context.args or []
        if not args:
            rows = await queries.list_exceptions()
            if not rows:
                await message.reply_text('目前没有可解除的异常任务。')
                return
            lines = ['异常任务（最多显示 20 条）', '解除指定任务：/jrtasks 任务号 confirm']
            for row in rows:
                lines.append(f'{row["task_id"]}\n{state_text(row["state"])} · {reason_text(row["reason"])}')
            await reply_long(message, '\n\n'.join(lines))
            return

        if not (len(args) == 2 and re.fullmatch(r'[0-9a-f]{32}', args[0]) and args[1] == 'confirm'):
            await message.reply_text('请发送 /jrtasks 任务号 confirm 确认解除。解除会释放账号占用，结果记为未核实。')
            return

        if await queries.release_exception(args[0]):
            await message.reply_text(f'已解除 {args[0]}。结果未核实，账号占用已释放，未重发查询。')
        else:
            await message.reply_text('该任务当前不可解除，可能已处理或正在交付。可用 /jrtasks 刷新列表。')

    return handle
