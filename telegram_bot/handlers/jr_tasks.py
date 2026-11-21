import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .guards import authorized, protected
from ..utils.user_messages import reason_text, state_text

CALLBACK_PATTERN = re.compile(r'^jrtask:(?:list|(?:select|confirm):[0-9a-f]{32})\Z')
UNAVAILABLE_TEXT = '该任务当前不可解除，可能已处理或正在交付。'


def task_button(label, action):
    return InlineKeyboardButton(label, callback_data=f'jrtask:{action}')


def back_keyboard():
    return InlineKeyboardMarkup([[task_button('返回列表', 'list')]])


async def show_list(queries, send):
    rows = await queries.list_exceptions()
    if not rows:
        await send('目前没有可解除的异常任务。')
        return
    buttons = []
    for index, row in enumerate(rows, 1):
        task = row['task_id']
        label = f'{index}. {state_text(row["state"])} · {task[-8:]}'
        buttons.append([task_button(label, f'select:{task}')])
    await send('请选择要查看的异常任务（最多显示 20 条）。', reply_markup=InlineKeyboardMarkup(buttons))


async def release(queries, task):
    if await queries.release_exception(task):
        return f'已解除 {task}。结果未核实，账号占用已释放，未重发查询。'
    return UNAVAILABLE_TEXT


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
            await show_list(queries, message.reply_text)
            return

        if not (len(args) == 2 and re.fullmatch(r'[0-9a-f]{32}', args[0]) and args[1] == 'confirm'):
            await message.reply_text('发 /jrtasks，点选任务后确认解除。')
            return

        await message.reply_text(await release(queries, args[0]))

    return handle


def make_callback(ctx):
    async def handle(update, context):
        query = update.callback_query
        user, chat = update.effective_user, update.effective_chat
        if not user or not authorized(user.id, ctx.config.admin_id) or not chat or chat.type != 'private':
            await query.answer('请在管理员私聊中操作。', show_alert=True)
            return
        if not ctx.requests.accepting:
            await query.answer('机器人正在关闭，请稍后再试。', show_alert=True)
            return
        if not isinstance(query.data, str) or not CALLBACK_PATTERN.fullmatch(query.data):
            await query.answer('按钮已失效，请重新发送 /jrtasks。', show_alert=True)
            return

        async with ctx.requests.track():
            await query.answer()
            _, action, *args = query.data.split(':')
            queries = ctx.jr.repository
            if action == 'list':
                await show_list(queries, query.edit_message_text)
                return
            task = args[0]
            if action == 'confirm':
                await query.edit_message_text(await release(queries, task), reply_markup=back_keyboard())
                return
            row = await queries.get_exception(task)
            if not row:
                await query.edit_message_text(UNAVAILABLE_TEXT, reply_markup=back_keyboard())
                return
            text = (f'解除任务 {task}？\n{state_text(row["state"])} · {reason_text(row["reason"])}\n\n'
                    '确认后释放账号占用，结果标记为未核实。不会重发查询。')
            markup = InlineKeyboardMarkup([[task_button('确认解除', f'confirm:{task}'),
                                            task_button('取消，返回列表', 'list')]])
            await query.edit_message_text(text, reply_markup=markup)

    return handle
