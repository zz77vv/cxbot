import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import button, message
from test_jr import setup_jr
from test_jr_cooldown import add_account, real_selector
from telegram_bot.models.account import OperationResult
from telegram_bot.models.enums import Outcome, QueryState
from telegram_bot.services.jr.account_selector import RateLimitEncountered
from telegram_bot.services.jr.scheduler import AccountScheduler


async def test_concurrent_free_queries_use_idle_account_with_stale_rankings(
        setup_jr, manager, client, db):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    jr.selector = real_selector(manager, db)
    all_balances_requested = asyncio.Event()
    both_queries_submitted = asyncio.Event()
    finish_replies = asyncio.Event()
    balance_calls = 0
    reply_calls = 0

    async def balance(*args):
        nonlocal balance_calls
        balance_calls += 1
        # Force both real selector calls to rank the same two free accounts
        # before the scheduler has reserved either one.
        if balance_calls == 4:
            all_balances_requested.set()
        await all_balances_requested.wait()
        return OperationResult(Outcome.SUCCESS, value=100)

    async def ready(*args):
        nonlocal reply_calls
        reply_calls += 1
        if reply_calls == 2:
            both_queries_submitted.set()
        await finish_replies.wait()
        return message('未查询到结果')

    jr.selector.balances.query.side_effect = balance
    target.ready.side_effect = ready
    tasks = [asyncio.create_task(jr.execute('concurrent-1', 'first free query')),
             asyncio.create_task(jr.execute('concurrent-2', 'second free query'))]
    try:
        await asyncio.wait_for(both_queries_submitted.wait(), 3)
        rows = await db.fetchall('SELECT account,reserved FROM query_tasks')
        assert {r['account'] for r in rows} == {'10001', '10002'}
        assert all(r['reserved'] == 1 for r in rows)
        finish_replies.set()
        reports = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert all(report.state == QueryState.SUCCESS for report in reports)
        usage = await db.fetchall('SELECT phone,query_count FROM jr_daily_usage ORDER BY phone')
        assert [(r['phone'], r['query_count']) for r in usage] == [('10001', 1), ('10002', 1)]
        assert target.submit.await_count == 2
    finally:
        finish_replies.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_flood_wait_after_ranking_never_rotates_to_another_account(
        setup_jr, manager, client, db):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    selector = real_selector(manager, db)
    both_balances_requested = asyncio.Event()
    calls = 0

    async def balance(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            # The selector's initial global FloodWait check already passed.
            manager.accounts['10001'].unavailable_until = time.time() + 60
            both_balances_requested.set()
        await both_balances_requested.wait()
        return OperationResult(Outcome.SUCCESS, value=100)

    selector.balances.query.side_effect = balance
    scheduler = AccountScheduler(manager, selector, repo)
    row, _ = await repo.claim('flood-race', 'flood-race-key', time.time())
    with pytest.raises(RateLimitEncountered):
        async with scheduler.acquire(row['task_id']):
            pytest.fail('Telegram FloodWait must stop acquisition before rotating accounts')

    row = await repo.get(row['task_id'])
    assert row['account'] is None and row['reserved'] == 0
    assert await db.fetchall('SELECT * FROM jr_daily_usage') == []
    assert await repo.cooldown_until('10002') == 0
    assert all(not a.lock.locked() for a in manager.accounts.values())
    target.submit.assert_not_awaited()


async def test_cancelling_all_cooling_queue_releases_locks_and_uses_no_quota(
        setup_jr, manager, client, db):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    jr.selector = real_selector(manager, db)
    until = time.time() + 60
    async with db.transaction() as c:
        await c.executemany('INSERT INTO jr_query_cooldowns VALUES(?,?)',
                            [('10001', until), ('10002', until)])
    waiting = asyncio.Event()

    async def advance(*args, **kwargs):
        if '冷却' in kwargs.get('detail', ''):
            waiting.set()

    progress = SimpleNamespace(advance=AsyncMock(side_effect=advance))
    task = asyncio.create_task(jr.execute('cancel-queue', 'queued query', progress=progress))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        row = await db.fetchone('SELECT * FROM query_tasks')
        assert row['state'] == 'prepared' and row['account'] is None
        assert row['reserved'] == 0 and row['sent_id'] is None
        assert all(not a.lock.locked() for a in manager.accounts.values())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    row = await repo.get(row['task_id'])
    assert row['state'] == 'failed' and row['reason'] == 'cancelled_before_submission'
    assert row['reserved'] == 0 and row['account'] is None
    assert await db.fetchall('SELECT * FROM jr_daily_usage') == []
    assert all(not a.lock.locked() for a in manager.accounts.values())
    assert not jr.active and not jr.locks._locks
    target.submit.assert_not_awaited()
    jr.selector.balances.query.assert_not_awaited()


async def test_explicit_cooldown_after_purchase_stays_pending_without_rotation(
        setup_jr, manager, client, db):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    jr.selector = real_selector(manager, db)
    target.ready.return_value = message('preview', buttons=[[button('购买 5 积分')]])
    target.click_and_wait.return_value = message('⏱️ 查询过于频繁，请 17 秒后再试。')

    report = await asyncio.wait_for(jr.execute('purchase', 'purchase query'), 3)
    assert report.state == QueryState.PENDING_REVIEW
    assert report.reason == 'purchase_not_confirmed'
    row = await repo.get(report.task_id)
    assert row['account'] == '10001' and row['reserved'] == 1
    assert row['sent_id'] == 10 and row['state'] == 'pending_review'
    assert await repo.unresolved_account('10001')
    assert await repo.cooldown_until('10002') == 0
    assert await db.fetchall('SELECT * FROM jr_daily_usage') == []
    assert await db.fetchall('SELECT * FROM jr_result_cache') == []
    repeated = await jr.execute('purchase-again', 'purchase query')
    assert repeated.duplicate and repeated.task_id == report.task_id
    target.submit.assert_awaited_once()
    target.ready.assert_awaited_once()
    target.click_and_wait.assert_awaited_once()


@pytest.mark.parametrize('reply', [
    '⏱️ 查询过于频繁，请稍后再试。',
    '⏱️ 查询过于频繁，请 秒后再试。',
    '⏱️ 查询过于频繁，请 sixty 秒后再试。',
])
async def test_cooldown_without_numeric_delay_is_pending_and_never_retried(
        setup_jr, manager, client, db, reply):
    jr, repo, target, *_ = setup_jr
    add_account(manager, client)
    jr.selector = real_selector(manager, db)
    target.ready.return_value = message(reply)

    report = await asyncio.wait_for(jr.execute('malformed', 'malformed cooldown query'), 3)
    assert report.state == QueryState.PENDING_REVIEW
    assert report.reason == 'unrecognized_target_reply'
    row = await repo.get(report.task_id)
    assert row['account'] == '10001' and row['reserved'] == 1
    assert row['sent_id'] == 10 and row['state'] == 'pending_review'
    assert await repo.unresolved_account('10001')
    assert await repo.cooldown_until('10002') == 0
    assert await db.fetchall('SELECT * FROM jr_daily_usage') == []
    assert await db.fetchall('SELECT * FROM jr_result_cache') == []
    repeated = await jr.execute('malformed-again', 'malformed cooldown query')
    assert repeated.duplicate and repeated.task_id == report.task_id
    target.submit.assert_awaited_once()
    target.ready.assert_awaited_once()
    target.click_and_wait.assert_not_awaited()
