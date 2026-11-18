import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from conftest import message
from telegram_bot.clients.dialogue import Dialogue
from telegram_bot.clients.userbot_manager import UserbotManager
from telegram_bot.storage.repositories.account_repository import AccountRepository
from telegram_bot.storage.repositories.checkin_repository import CheckinRepository
from telegram_bot.services.checkin_service import CheckinService
from telegram_bot.services.balance_service import BalanceService,parse_balance
from telegram_bot.target_bots import TARGETS
from telegram_bot.models.enums import Outcome,AccountState
from telegram_bot.lifecycle import TaskRegistry
from telethon.errors import FloodWaitError


async def test_sessions_load_preserves_files(cfg,db,client):
    cfg.sessions.mkdir()
    (cfg.sessions/'10001.session').write_bytes(b'fixture')
    (cfg.sessions/'not-phone.session').write_bytes(b'skip')
    before=(cfg.sessions/'10001.session').read_bytes()
    manager=UserbotManager(cfg,AccountRepository(db),factory=lambda p:client)
    assert len(manager.load())==1 and len(manager.load())==1
    await manager.start()
    assert manager.accounts['10001'].state==AccountState.ONLINE
    assert (cfg.sessions/'10001.session').read_bytes()==before
    await manager.close()
    client.disconnect.assert_awaited()


async def test_start_failure_isolated(cfg,db,client):
    cfg.sessions.mkdir()
    for key in ['10001','10002']: (cfg.sessions/f'{key}.session').touch()
    bad=SimpleNamespace(is_connected=lambda:False, connect=AsyncMock(side_effect=ValueError('broken')))
    manager=UserbotManager(cfg,AccountRepository(db),factory=lambda p:bad if p.stem=='10001' else client)
    await manager.start()
    assert manager.accounts['10001'].state==AccountState.ERROR
    assert manager.accounts['10002'].state==AccountState.ONLINE


async def test_unauthorized_no_interactive_login(manager,client):
    client.is_user_authorized.return_value=False
    account=next(iter(manager.accounts.values()))
    await manager.refresh(account,reconnect=True)
    assert account.state==AccountState.UNAUTHORIZED
    assert not hasattr(client,'start')


@pytest.mark.parametrize('reply,expected', [(message('签到成功'),Outcome.SUCCESS), (None,Outcome.PENDING_REVIEW)])
async def test_checkin_success_timeout(manager,db,reply,expected):
    dialogue=SimpleNamespace(wait=AsyncMock(return_value=reply))
    repo=CheckinRepository(db)
    service=CheckinService(manager,repo,dialogue,random.Random(42))
    account=next(iter(manager.accounts.values()))
    result=await service.run_one(account,TARGETS[1])
    assert result.outcome==expected
    row=await repo.get(account.key,TARGETS[1].key)
    assert row['last_attempt'] is not None
    assert bool(row['last_success'])==(expected==Outcome.SUCCESS)
    assert row['next_at']>=row['last_attempt']+86400
    assert (await service.run_one(account,TARGETS[1])).outcome==Outcome.SKIPPED


async def test_checkin_rpc_failure_not_success(manager,db,client):
    client.send_message.side_effect=ValueError('RPC fixture')
    service=CheckinService(manager,CheckinRepository(db),SimpleNamespace())
    account=next(iter(manager.accounts.values()))
    result=await service.run_one(account,TARGETS[1])
    assert result.outcome==Outcome.UNKNOWN
    assert (await service.repository.get(account.key,TARGETS[1].key))['last_success'] is None


async def test_checkin_missing_button(manager,db):
    service=CheckinService(manager,CheckinRepository(db),SimpleNamespace(wait=AsyncMock(return_value=None)))
    result=await service.run_one(next(iter(manager.accounts.values())),TARGETS[0])
    assert result.outcome==Outcome.FAILED


@pytest.mark.parametrize('index,text,value',[(0,'金币🪙: 0',0),(1,'积分余额：123',123),(2,'盒能：`10`',10)])
def test_original_balance_patterns(index,text,value):
    assert parse_balance(text,TARGETS[index]).value==value
    assert parse_balance('changed format',TARGETS[index]).outcome==Outcome.FORMAT_CHANGED


async def test_balance_format_distinct_from_timeout(manager):
    class Poll:
        async def wait(self,client,bot,after_id,predicate,timeout):
            predicate(message('new format'))
            return None
    service=BalanceService(manager,Poll())
    account=next(iter(manager.accounts.values()))
    assert (await service.query(account,TARGETS[1])).outcome==Outcome.FORMAT_CHANGED
    service.dialogue=SimpleNamespace(wait=AsyncMock(return_value=None))
    assert (await service.query(account,TARGETS[1])).outcome==Outcome.TIMEOUT


async def test_flood_wait_persisted(manager,client,db):
    client.send_message.side_effect=FloodWaitError(None,capture=90)
    account=next(iter(manager.accounts.values()))
    result=await BalanceService(manager,Dialogue(manager.cfg)).query(account,TARGETS[1])
    assert result.outcome==Outcome.RATE_LIMITED
    assert account.state==AccountState.UNAVAILABLE
    assert (await db.fetchone('SELECT until_at FROM account_cooldowns'))[0]>0


async def test_account_operations_serial(manager):
    account=next(iter(manager.accounts.values()))
    active=peak=0
    async def operation():
        nonlocal active,peak
        async with manager.operation(account):
            active+=1; peak=max(peak,active)
            await asyncio.sleep(0)
            active-=1
    await asyncio.gather(*(operation() for _ in range(5)))
    assert peak==1


async def test_scheduler_duplicate_and_cancel(manager,db):
    service=CheckinService(manager,CheckinRepository(db),SimpleNamespace())
    registry=TaskRegistry()
    service.start(registry); service.start(registry)
    assert len(registry.tasks)==3
    await asyncio.sleep(0.01)
    await registry.close()
    assert all(t.done() for t in service.tasks.values())


async def test_real_telethon_sqlite_session_format(cfg,db):
    from telethon.sessions import SQLiteSession
    cfg.sessions.mkdir()
    path=cfg.sessions/'10001.session'
    session=SQLiteSession(str(path));session.save();session.close()
    before=path.read_bytes()
    manager=UserbotManager(cfg,AccountRepository(db))
    accounts=manager.load()
    assert len(accounts)==1 and accounts[0].client.session.filename==str(path)
    assert path.read_bytes()==before
    await manager.close()
