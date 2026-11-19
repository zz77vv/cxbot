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
    assert task in reply.call_args.args[0]
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
