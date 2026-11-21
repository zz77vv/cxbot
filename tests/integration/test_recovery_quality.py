import asyncio
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from telegram_bot.clients.request_tracker import RequestTracker
from telegram_bot.handlers import balance, jr_tasks
from telegram_bot.lifecycle import Lifecycle
from telegram_bot.models.query import JRQueryResult
from telegram_bot.models.account import OperationResult
from telegram_bot.models.enums import Outcome
from telegram_bot.services.checkin_service import CheckinService
from telegram_bot.services.jr.account_selector import AccountExclusion, NoEligibleAccountError
from telegram_bot.services.jr.scheduler import AccountScheduler
from telegram_bot.storage.database import Database
from test_jr import setup_jr


async def test_saved_delivery_can_be_retried_once_from_task_menu(setup_jr, cfg, bot):
    jr, queries, target, _, _ = setup_jr
    row, _ = await queries.claim('hw:fixture', 'hw:fixture', time.time())
    task = row['task_id']
    await queries.complete(task, JRQueryResult('text', '已保存的文本'), time.time(), 0, cache_result=False)
    await queries.recover()
    assert (await queries.get(task))['delivery_state'] == 'unknown'
    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(), jr=jr)
    edit = AsyncMock()
    query = SimpleNamespace(data=f'jrtask:select:{task}', answer=AsyncMock(), edit_message_text=edit)
    update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                             effective_user=SimpleNamespace(id=cfg.admin_id), callback_query=query)
    callback = jr_tasks.make_callback(ctx)
    await callback(update, None)
    buttons = edit.call_args.kwargs['reply_markup'].inline_keyboard
    retry = buttons[1][0].callback_data
    assert len(retry.encode()) <= 64
    query.data = retry
    await callback(update, None)
    await callback(update, None)
    assert (await queries.get(task))['delivery_state'] == 'delivered'
    assert bot.send_message.await_args.kwargs['text'] == '已保存的文本'
    bot.send_message.assert_awaited_once()
    target.submit.assert_not_awaited()


async def test_partial_telegram_delivery_can_be_acknowledged_but_not_replayed(setup_jr):
    _, queries, _, _, _ = setup_jr
    row, _ = await queries.claim('fixture', 'key', time.time())
    task = row['task_id']
    await queries.complete(task, JRQueryResult('text', '已保存'), time.time(), 0)
    await queries.delivery(task, 'unknown', 1)
    row = await queries.get_exception(task)
    assert not await queries.resolve_delivery(task, row['updated_at'], received=False)
    assert await queries.resolve_delivery(task, row['updated_at'], received=True)


async def test_balance_falls_back_to_new_message_and_rejects_duplicate(cfg):
    started, finish = asyncio.Event(), asyncio.Event()

    async def fetch():
        started.set()
        await finish.wait()
        return []

    query = AsyncMock(side_effect=fetch)
    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(), balances=SimpleNamespace(all=query))
    progress = SimpleNamespace(edit_text=AsyncMock(side_effect=TimedOut()))
    reply = AsyncMock(return_value=progress)
    second = AsyncMock()
    handle = balance.make_handler(ctx)

    def update(send):
        return SimpleNamespace(effective_user=SimpleNamespace(id=cfg.admin_id),
                               effective_message=SimpleNamespace(reply_text=send))

    task = asyncio.create_task(handle(update(reply), None))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await handle(update(second), None)
        assert '已有积分查询' in second.call_args.args[0]
    finally:
        finish.set()
        await task
    reply.assert_awaited_with('📊 账号积分汇总', parse_mode='HTML')
    query.assert_awaited_once()


async def test_queue_timeout_occurs_before_reservation(monkeypatch):
    monkeypatch.setattr('telegram_bot.services.jr.scheduler.ACCOUNT_WAIT_TIMEOUT', 0.03)
    wait = NoEligibleAccountError((AccountExclusion('fixture', 'account_busy', time.time()+30),))
    queries = SimpleNamespace(reserve=AsyncMock())
    selector = SimpleNamespace(rank=AsyncMock(side_effect=wait))
    scheduler = AccountScheduler(SimpleNamespace(), selector, queries)
    with pytest.raises(NoEligibleAccountError) as error:
        async with scheduler.acquire('fixture'):
            pytest.fail('queue timeout must not yield an account')
    assert error.value.reason == 'jr_account_wait_timeout'
    queries.reserve.assert_not_awaited()


async def test_polling_stop_timeout_still_cleans_resources(cfg):
    async def stall():
        await asyncio.Event().wait()

    resources = [SimpleNamespace(close=AsyncMock()) for _ in range(4)]
    ctx = SimpleNamespace(config=replace(cfg, shutdown_timeout=0.02), requests=RequestTracker(),
                          jr=SimpleNamespace(active=set(), accepting=True), logins=resources[0],
                          accounts=resources[1], ocr=resources[2], database=resources[3])
    bot = SimpleNamespace(stop_polling=stall, close=AsyncMock())
    lifecycle = Lifecycle(ctx, bot, None)
    await asyncio.wait_for(lifecycle.close(), 1)
    await lifecycle.close()
    for item in [bot, *resources]:
        item.close.assert_awaited_once()


async def test_manual_checkin_timeout_preserves_confirmed_items(manager, monkeypatch):
    monkeypatch.setattr('telegram_bot.services.checkin_service.MANUAL_CHECKIN_TIMEOUT', 0.03)
    checkins = CheckinService(manager, None, None, rng=SimpleNamespace(uniform=lambda *_: 0))

    async def run_one(account, target):
        if target.name == '中核验':
            return OperationResult(Outcome.SUCCESS)
        await asyncio.Event().wait()

    checkins.run_one = AsyncMock(side_effect=run_one)
    [(account, results)] = await checkins.manual()
    assert [result.outcome for _, result in results] == [
        Outcome.SUCCESS, Outcome.PENDING_REVIEW, Outcome.PENDING_REVIEW]
    assert checkins.run_one.await_count == 2


async def test_current_database_reuses_daily_snapshot_and_preserves_migration_copy(tmp_path):
    path = tmp_path / 'bot.db'
    database = await Database(path).open()
    await database.close()
    migration = tmp_path / 'bot.db.backup-fixture'
    migration.write_bytes(b'keep')
    for day in range(1, 9):
        (tmp_path / f'bot.db.backup-daily-200101{day:02}').write_bytes(b'old synthetic snapshot')
    snapshots = []
    for _ in range(2):
        database = await Database(path).open()
        snapshots.append(database.backup_path)
        await database.close()
    assert snapshots[0] == snapshots[1]
    assert len(list(tmp_path.glob('bot.db.backup-daily-*'))) == 7
    assert migration.read_bytes() == b'keep'
    copy = sqlite3.connect(snapshots[0])
    try:
        assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert copy.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 2
    finally:
        copy.close()
