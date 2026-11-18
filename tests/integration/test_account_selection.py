import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram_bot.models.account import OperationResult
from telegram_bot.models.enums import AccountState, Outcome, QueryState
from telegram_bot.services.jr.account_selector import AccountSelector, NoEligibleAccountError
from telegram_bot.services.jr.membership_service import MembershipService
from telegram_bot.storage.repositories.membership_repository import MembershipRepository, usage_date
from telegram_bot.storage.repositories.query_repository import QueryRepository


def selector(manager, db):
    balances = SimpleNamespace(query=AsyncMock(return_value=OperationResult(Outcome.SUCCESS, value=126)))
    return AccountSelector(manager, balances, MembershipService(MembershipRepository(db)), QueryRepository(db))


@pytest.mark.parametrize('state', [QueryState.MANUAL, QueryState.PENDING_REVIEW])
async def test_unresolved_account_is_rejected_before_balance(manager, db, state):
    sel = selector(manager, db)
    row, _ = await sel.queries.claim('old', 'old-key', time.time())
    await sel.queries.reserve(row['task_id'], '10001', time.time())
    await sel.queries.transition(row['task_id'], state, 'fixture')
    with pytest.raises(NoEligibleAccountError, match='account_has_unresolved_task'):
        await sel.rank()
    sel.balances.query.assert_not_awaited()
    # An explicit operator decision releases the hold; it never sends or retries.
    await sel.queries.review(row['task_id'], 'failed', 'verified_no_result')
    assert (await sel.queries.get(row['task_id']))['reserved'] == 0
    sel.balances.query.assert_not_awaited()
    assert (await sel.rank())[0][1].value == 126
    assert await db.fetchone('SELECT * FROM jr_daily_usage') is None


@pytest.mark.parametrize('used,allowed', [(4, True), (5, False)])
async def test_unknown_membership_daily_boundary(manager, db, used, allowed):
    sel = selector(manager, db)
    async with db.transaction() as c:
        await c.execute('INSERT INTO jr_daily_usage VALUES(?,?,?)', ('10001', usage_date(time.time()), used))
    if allowed:
        assert len(await sel.rank()) == 1
    else:
        with pytest.raises(NoEligibleAccountError, match='daily_query_limit_reached'):
            await sel.rank()
        sel.balances.query.assert_not_awaited()


@pytest.mark.parametrize('outcome', [Outcome.TIMEOUT, Outcome.FORMAT_CHANGED, Outcome.FAILED])
async def test_balance_failure_reason_is_preserved(manager, db, outcome):
    sel = selector(manager, db)
    sel.balances.query.return_value = OperationResult(outcome)
    with pytest.raises(NoEligibleAccountError, match='balance_query_' + outcome.value):
        await sel.rank()
    sel.balances.query.assert_awaited_once()


async def test_offline_and_empty_accounts(manager, db):
    sel = selector(manager, db)
    next(iter(manager.accounts.values())).state = AccountState.OFFLINE
    with pytest.raises(NoEligibleAccountError, match='account_not_online'):
        await sel.rank()
    sel.balances.query.assert_not_awaited()
    manager.accounts.clear()
    with pytest.raises(NoEligibleAccountError, match='no_accounts_loaded'):
        await sel.rank()
