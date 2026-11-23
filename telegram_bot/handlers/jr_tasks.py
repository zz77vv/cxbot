import re
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .guards import authorized, protected
from ..utils.user_messages import reason_text, state_text
from ..storage.repositories.query_repository import result_from_row
from ..services.hw_query_service import export_candidates, is_export_review

CALLBACK_PATTERN = re.compile(
    r'^jrtask:(?:list|(?:select|confirm|scan|whole):[0-9a-f]{32}|'
    r'(?:got|redo):[0-9a-f]{32}:[0-9a-f]{1,16}|(?:file|take):[0-9a-f]{32}:\d{1,12})\Z')
UNAVAILABLE_TEXT = '该任务当前不可解除，可能已处理或正在交付。'
BEIJING = timezone(timedelta(hours=8))


def task_button(label, action):
    return InlineKeyboardButton(label, callback_data=f'jrtask:{action}')


def back_keyboard():
    return InlineKeyboardMarkup([[task_button('返回列表', 'list')]])


async def show_list(queries, send):
    rows = await queries.list_exceptions()
    if not rows:
        await send('目前没有待处理的异常任务。')
        return
    buttons = []
    for index, row in enumerate(rows, 1):
        task = row['task_id']
        state = '交付待确认' if row['state'] == 'success' else state_text(row['state'])
        label = f'{index}. {state} · {task[-8:]}'
        buttons.append([task_button(label, f'select:{task}')])
    await send('请选择要查看的异常任务（最多显示 20 条）。', reply_markup=InlineKeyboardMarkup(buttons))


async def release(queries, task):
    if await queries.release_exception(task):
        return f'已解除 {task}。结果未核实，账号占用已释放，未重发查询。'
    return UNAVAILABLE_TEXT


def delivery_version(row):
    return f'{int(row["updated_at"] * 1_000_000):x}'


async def resolve_delivery(ctx, task, action, version, row):
    if not row or row['state'] != 'success' or delivery_version(row) != version:
        return '处理状态已变化，请返回列表刷新。'
    received = action == 'got'
    result = result_from_row(row)
    if not result or not await ctx.jr.repository.resolve_delivery(task, row['updated_at'], received=received):
        return '交付状态已变化，或已有部分内容发送，请核对后处理。'
    if received:
        return '已标记为收到结果。'
    try:
        status = await ctx.jr.exports.deliver(task, result, raw_text=row['request_id'].startswith('hw:'))
    except Exception:
        return '补发未能确认，请核对私聊消息后再处理。不会自动重发。'
    return '已补发保存的结果，没有重查。' if status == 'delivered' else '交付状态已变化，请返回列表核对。'


def file_details(item):
    date = datetime.fromtimestamp(item['at'], BEIJING).strftime('%m-%d %H:%M:%S')
    size = '未知大小' if item['size'] is None else f'{item["size"]} 字节'
    return f'{item["name"]}\n{date}（北京时间） · {size}'


async def show_export_candidates(query, task, row, *, searched=False):
    items = export_candidates(row)
    buttons = [[task_button(f'查看候选 {i}', f'file:{task}:{item["id"]}')]
               for i, item in enumerate(items, 1)]
    buttons.append([task_button('查找已导出的 TXT', f'scan:{task}')])
    buttons.append([task_button('解除任务', f'whole:{task}')])
    buttons.extend(back_keyboard().inline_keyboard)
    note = ('这些文件不会自动转发。请到 HWQ 原聊天核对后，再确认回传。' if items
            else '未找到候选文件。' if searched else '尚无候选文件，可以只读查找已有导出。')
    await query.edit_message_text(f'任务 {task} · 导出待核实\n{note}',
                                  reply_markup=InlineKeyboardMarkup(buttons))


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
            if action in {'got', 'redo'}:
                text = await resolve_delivery(ctx, task, action, args[1], row)
                await query.edit_message_text(text, reply_markup=back_keyboard())
                return
            if not row:
                await query.edit_message_text(UNAVAILABLE_TEXT, reply_markup=back_keyboard())
                return
            if is_export_review(row) and action != 'whole':
                if action == 'scan':
                    await ctx.hw.find_export_candidates(row)
                    row = await queries.get_exception(task)
                    await show_export_candidates(query, task, row, searched=True)
                elif action in {'file', 'take'}:
                    message_id = int(args[1])
                    item = next((item for item in export_candidates(row) if item['id'] == message_id), None)
                    if not item:
                        await query.edit_message_text('候选文件已变化，请刷新。', reply_markup=back_keyboard())
                    elif action == 'take':
                        text = await ctx.hw.deliver_export_candidate(row, message_id)
                        await query.edit_message_text(text, reply_markup=back_keyboard())
                    else:
                        buttons = [[task_button('确认并回传原 TXT', f'take:{task}:{message_id}')],
                                   [task_button('返回候选文件', f'select:{task}')]]
                        await query.edit_message_text(file_details(item) + '\n\n请确认这份文件属于当前任务。',
                                                      reply_markup=InlineKeyboardMarkup(buttons))
                else:
                    await show_export_candidates(query, task, row)
                return
            if row['state'] == 'success':
                version = delivery_version(row)
                buttons = [[task_button('确认已收到', f'got:{task}:{version}')]]
                if row['delivery_cursor'] == 0:
                    buttons.append([task_button('确认未收到，补发结果', f'redo:{task}:{version}')])
                buttons.extend(back_keyboard().inline_keyboard)
                note = '请先检查聊天记录。仅补发已保存的结果，不会重查。'
                if row['delivery_cursor']:
                    note = '已发送过部分内容，请先核对；这里不会重发整份。'
                await query.edit_message_text(f'任务 {task} 的交付待确认。\n{note}',
                                              reply_markup=InlineKeyboardMarkup(buttons))
                return
            text = (f'解除任务 {task}？\n{state_text(row["state"])} · {reason_text(row["reason"])}\n\n'
                    '确认后释放账号占用，结果标记为未核实。不会重发查询。')
            markup = InlineKeyboardMarkup([[task_button('确认解除', f'confirm:{task}'),
                                            task_button('取消，返回列表', 'list')]])
            await query.edit_message_text(text, reply_markup=markup)

    return handle
