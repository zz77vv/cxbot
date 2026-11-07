import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import message
from test_jr import setup_jr
from telegram_bot.models.account import Account, OperationResult
from telegram_bot.models.enums import AccountState, Outcome, QueryState
from telegram_bot.models.query import JRQueryResult
from telegram_bot.services.jr.account_selector import AccountSelector
from telegram_bot.services.jr.membership_service import MembershipService
from telegram_bot.services.jr.result_parser import classify
from telegram_bot.storage.database import Database
from telegram_bot.storage.repositories.membership_repository import MembershipRepository
from telegram_bot.storage.repositories.query_repository import QueryRepository


def real_selector(manager, db):
    balances = SimpleNamespace(query=AsyncMock(return_value=OperationResult(Outcome.SUCCESS, value=100)))
    return AccountSelector(manager, balances, MembershipService(MembershipRepository(db)), QueryRepository(db))


def add_account(manager, client):
    manager.accounts['10002'] = Account('10002', manager.cfg.sessions/'10002.session', client, AccountState.ONLINE)


@pytest.mark.parametrize('text', [
    '⏱️ 查询过于频繁，请 59 秒后再试。\n\n💡 充值用户不受此限制。',
    '⏱️ 查询过于频繁，请1秒后再试。',
])
def test_rate_limit_is_a_distinct_reply(text):
    assert classify(message(text)) == 'cooldown'


async def test_reservation_cooldown_is_atomic_and_persistent(db, cfg):
    repo = QueryRepository(db)
    first, _ = await repo.claim('first', 'key1', 1000)
    assert await repo.reserve(first['task_id'], '10001', 1000, cooldown_seconds=60)
    await repo.complete(first['task_id'], JRQueryResult('text', 'one'), 1001, 300)
    second, _ = await repo.claim('second', 'key2', 1001)
    assert not await repo.reserve(second['task_id'], '10001', 1059, cooldown_seconds=60)
    await db.close()
    reopened = await Database(cfg.database).open()
    try:
        repo = QueryRepository(reopened)
        assert not await repo.reserve(second['task_id'], '10001', 1059, cooldown_seconds=60)
        assert await repo.reserve(second['task_id'], '10001', 1060, cooldown_seconds=60)
    finally:
        await reopened.close()


async def test_next_query_uses_another_free_account(setup_jr, manager, client, db):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    jr.selector = real_selector(manager, db)
    first = await jr.execute('one', 'query one')
    second = await jr.execute('two', 'query two')
    assert first.state == second.state == QueryState.SUCCESS
    assert (await repo.get(first.task_id))['account'] == '10001'
    assert (await repo.get(second.task_id))['account'] == '10002'
    assert target.submit.await_count == 2


async def test_member_queries_have_no_proactive_delay(setup_jr, db):
    jr, repo, target, *_ = setup_jr
    async with db.transaction() as c:
        await c.execute("INSERT INTO jr_membership VALUES('10001','member',0)")
    reports = await asyncio.wait_for(asyncio.gather(
        jr.execute('one', 'one'), jr.execute('two', 'two')), 3)
    assert all(r.state == QueryState.SUCCESS for r in reports)
    assert target.submit.await_count == 2


async def test_explicit_rejection_rotates_without_double_usage(setup_jr, manager, client, db):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    jr.selector = real_selector(manager, db)
    # Even a stale cached membership cannot override explicit upstream feedback.
    async with db.transaction() as c:
        await c.execute("INSERT INTO jr_membership VALUES('10001','member',0)")
    target.ready.side_effect = [message('⏱️ 查询过于频繁，请 17 秒后再试。'), message('未查询到结果')]
    report = await jr.execute('one', 'fixture')
    assert report.state == QueryState.SUCCESS
    assert (await repo.get(report.task_id))['account'] == '10002'
    assert not await repo.unresolved_account('10001')
    rows = await db.fetchall('SELECT phone,query_count FROM jr_daily_usage')
    assert [(r['phone'], r['query_count']) for r in rows] == [('10002', 1)]
    assert (await db.fetchone("SELECT status FROM jr_membership WHERE phone='10001'"))[0] == 'free'
    until = await repo.cooldown_until('10001')
    assert 14 < until - time.time() <= 18
    assert target.submit.await_count == 2


async def test_all_cooling_waits_without_account_lock(setup_jr, manager, db):
    jr, repo, target, *_ = setup_jr
    jr.selector = real_selector(manager, db)
    async with db.transaction() as c:
        await c.execute('INSERT INTO jr_query_cooldowns VALUES(?,?)', ('10001', time.time() + 0.15))
    waiting = asyncio.Event()
    progress = SimpleNamespace(advance=AsyncMock(side_effect=lambda *a, **kw: waiting.set()),
                               page=AsyncMock(), text_fits=lambda text: True)
    # Use a real progress-free delivery; the observer is only for scheduling.
    from telegram_bot.services.jr.scheduler import AccountScheduler
    scheduler = AccountScheduler(manager, jr.selector, repo)
    row, _ = await repo.claim('waiting', 'waiting-key', time.time())
    async def acquire():
        async with scheduler.acquire(row['task_id'], progress=progress) as (account, _):
            return account.key
    task = asyncio.create_task(acquire())
    await asyncio.wait_for(waiting.wait(), 1)
    assert not manager.accounts['10001'].lock.locked()
    assert not task.done()
    assert await asyncio.wait_for(task, 2) == '10001'
    target.submit.assert_not_awaited()


async def test_v1_migrates_with_backup_and_data_preserved(cfg):
    db = await Database(cfg.database).open()
    async with db.transaction() as c:
        await c.execute("INSERT INTO jr_membership VALUES('10001','member',42)")
        await c.execute('DROP TABLE IF EXISTS jr_query_cooldowns')
        await c.execute('DELETE FROM schema_migrations WHERE version>1')
    await db.close()
    db = await Database(cfg.database).open()
    try:
        assert (await db.fetchone('SELECT MAX(version) FROM schema_migrations'))[0] == 2
        assert (await db.fetchone('SELECT checked_at FROM jr_membership'))[0] == 42
        assert await db.fetchall('SELECT * FROM jr_query_cooldowns') == []
        with sqlite3.connect(db.backup_path) as backup:
            assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 1
    finally:
        await db.close()


async def test_rejected_task_does_not_block_its_own_account(db, manager, monkeypatch):
    repo = QueryRepository(db)
    row, _ = await repo.claim('reject', 'reject', 1000)
    assert await repo.reserve(row['task_id'], '10001', 1000)
    await repo.defer_cooldown(row['task_id'], '10001', 17, 1000)
    assert not await repo.unresolved_account('10001')
    monkeypatch.setattr(time, 'time', lambda: 1018)
    assert (await real_selector(manager, db).rank())[0][0].key == '10001'


async def test_verification_resubmission_refreshes_free_cooldown(setup_jr, monkeypatch):
    jr, repo, target, *_ = setup_jr
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    target.ready.return_value = message('安全验证')
    async def verified(*args, **kwargs):
        clock[0] += 100  # Human choice can take longer than the original cooldown.
        return message('未查询到结果'), 20
    jr.verification.solve = AsyncMock(side_effect=verified)
    report = await jr.execute('verification', 'verification query')
    assert report.state == QueryState.SUCCESS
    assert await repo.cooldown_until('10001') >= clock[0] + 60


async def test_confirmed_membership_removes_proactive_cooldown(setup_jr):
    jr, repo, target, exports, *_ = setup_jr
    target.ready.return_value = message(id=11, document=True)
    exports.attempt = AsyncMock(return_value=(JRQueryResult('text', 'file result', export_status='member'), 'member', ''))
    report = await jr.execute('member-confirmation', 'query')
    assert report.state == QueryState.SUCCESS
    assert await repo.cooldown_until('10001') == 0
