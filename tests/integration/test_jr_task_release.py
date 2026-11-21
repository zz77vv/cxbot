import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram_bot.clients.request_tracker import RequestTracker
from telegram_bot.handlers.jr_tasks import make_handler
from telegram_bot.models.enums import QueryState
from telegram_bot.storage.repositories.query_repository import QueryRepository


async def test_admin_can_list_and_confirm_release_without_upstream_call(cfg, db):
    queries = QueryRepository(db)
    row, _ = await queries.claim('request', 'cache-key', time.time())
    task = row['task_id']
    assert await queries.reserve(task, 'fixture-account', time.time())
    await queries.transition(task, QueryState.PENDING_REVIEW, 'query_reply_timeout', sent_id=10)
    reply = AsyncMock()
    execute = AsyncMock()
    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(),
                          jr=SimpleNamespace(repository=queries, execute=execute))
    handle = make_handler(ctx)
    update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                             effective_message=SimpleNamespace(reply_text=reply),
                             effective_user=SimpleNamespace(id=cfg.admin_id))

    await handle(update, SimpleNamespace(args=[]))
    assert task in reply.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
    await handle(update, SimpleNamespace(args=[task]))
    assert (await queries.get(task))['reserved'] == 1
    update.effective_user.id += 1
    await handle(update, SimpleNamespace(args=[task, 'confirm']))
    assert (await queries.get(task))['reserved'] == 1
    update.effective_user.id = cfg.admin_id
    update.effective_chat.type = 'group'
    await handle(update, SimpleNamespace(args=[task, 'confirm']))
    assert (await queries.get(task))['reserved'] == 1
    update.effective_chat.type = 'private'
    await handle(update, SimpleNamespace(args=[task, 'confirm']))
    record = await queries.get(task)
    assert record['state'] == 'failed' and record['reserved'] == 0 and record['sent_id'] == 10
    assert record['reason'] == 'operator_closed_unverified:query_reply_timeout'
    assert not await queries.unresolved_account('fixture-account')
    assert await db.fetchone('SELECT * FROM jr_daily_usage') is None
    await handle(update, SimpleNamespace(args=[task, 'confirm']))
    assert '不可解除' in reply.call_args.args[0]
    execute.assert_not_awaited()


async def test_release_does_not_close_inflight_or_sending_task(db):
    queries = QueryRepository(db)
    row, _ = await queries.claim('request', 'cache-key', time.time())
    task = row['task_id']
    assert await queries.reserve(task, 'fixture-account', time.time())
    assert not await queries.release_exception(task)
    await queries.transition(task, QueryState.PENDING_REVIEW, 'partial_page_timeout')
    await queries.delivery(task, 'sending', 0)
    assert not await queries.release_exception(task)
    assert (await queries.get(task))['reserved'] == 1


async def test_buttons_preview_back_and_confirm_release_only_selected_task(cfg, db):
    from telegram_bot.handlers.jr_tasks import make_callback

    queries = QueryRepository(db)
    task, other = [
        (await queries.claim(f'button-{i}', f'key-{i}', time.time()))[0]['task_id']
        for i in range(2)
    ]
    assert await queries.reserve(task, 'fixture-account', time.time())
    await queries.transition(task, QueryState.PENDING_REVIEW, 'query_reply_timeout', sent_id=10)
    await queries.transition(other, QueryState.MANUAL, 'verification_required')
    execute = AsyncMock()
    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(),
                          jr=SimpleNamespace(repository=queries, execute=execute))
    reply, edit, answer = AsyncMock(), AsyncMock(), AsyncMock()
    update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                             effective_message=SimpleNamespace(reply_text=reply),
                             effective_user=SimpleNamespace(id=cfg.admin_id),
                             callback_query=SimpleNamespace(edit_message_text=edit, answer=answer))
    command, callback = make_handler(ctx), make_callback(ctx)

    def buttons(mock):
        return [button for row in mock.call_args.kwargs['reply_markup'].inline_keyboard for button in row]

    async def click(button):
        update.callback_query.data = button.callback_data
        await callback(update, None)

    await command(update, SimpleNamespace(args=[]))
    selected = next(button for button in buttons(reply) if task in button.callback_data)
    await click(selected)
    assert task in edit.call_args.args[0]
    assert (await queries.get(task))['reserved'] == 1
    confirm, back = buttons(edit)
    await click(back)
    assert (await queries.get(task))['reserved'] == 1
    await click(selected)
    confirm, _ = buttons(edit)
    update.effective_user.id += 1
    await click(confirm)
    update.effective_user.id = cfg.admin_id
    update.effective_chat.type = 'group'
    await click(confirm)
    update.effective_chat.type = 'private'
    assert (await queries.get(task))['reserved'] == 1
    await click(confirm)
    assert '已解除' in edit.call_args.args[0]
    await click(confirm)
    assert '不可解除' in edit.call_args.args[0]
    row = await queries.get(task)
    assert row['state'] == 'failed' and row['reserved'] == 0 and row['sent_id'] == 10
    assert (await queries.get(other))['state'] == 'manual'
    execute.assert_not_awaited()
