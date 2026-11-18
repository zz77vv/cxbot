import asyncio
import hashlib
import importlib.util
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from conftest import message,button
from telegram_bot.clients.dialogue import Dialogue,signature
from telegram_bot.services.jr.target_client import TargetClient
from telegram_bot.services.jr.account_selector import AccountSelector,RateLimitEncountered
from telegram_bot.services.jr.membership_service import MembershipService
from telegram_bot.services.jr.export_service import ExportService
from telegram_bot.services.jr.cache_service import CacheService
from telegram_bot.storage.database import Database
from telegram_bot.storage.repositories.query_repository import QueryRepository
from telegram_bot.storage.repositories.cache_repository import CacheRepository
from telegram_bot.storage.repositories.membership_repository import MembershipRepository,usage_date
from telegram_bot.models.account import Account,OperationResult
from telegram_bot.models.enums import AccountState,Outcome


async def test_real_dialogue_pending_edit_and_old_outgoing(cfg,client):
    pending=message('正在查询，请稍候',id=11)
    ready=message('🔍共1条信息\nfixture',id=11)
    client.get_messages.side_effect=[[message('old',id=2),message('outgoing',id=20,out=True),pending],[ready]]
    dialogue=Dialogue(cfg)
    result=await dialogue.wait(client,'@jrsgk',10,lambda m:'正在' not in m.text,1,interval=0)
    assert result is ready
    changed=message('第2页，共2页',id=11)
    client.get_messages.side_effect=None;client.get_messages.return_value=changed
    result=await dialogue.wait(client,'@jrsgk',11,lambda m:True,1,original=pending,old_signature=signature(pending))
    assert result is changed


async def test_real_dialogue_timeout(cfg,client):
    result=await Dialogue(cfg).wait(client,'@jrsgk',10,lambda m:True,0.01,interval=0.001)
    assert result is None


async def test_selector_balance_order_and_rate_limit(manager,db):
    repo=QueryRepository(db)
    manager.accounts['10002']=Account('10002',manager.cfg.sessions/'10002.session',None,AccountState.ONLINE)
    balances=SimpleNamespace(query=AsyncMock(side_effect=[OperationResult(Outcome.SUCCESS,value=0),OperationResult(Outcome.SUCCESS,value=50)]))
    selector=AccountSelector(manager,balances,MembershipService(MembershipRepository(db)),repo)
    assert [a.key for a,_ in await selector.rank()]==['10002','10001']
    balances.query.side_effect=[OperationResult(Outcome.RATE_LIMITED),OperationResult(Outcome.SUCCESS,value=100)]
    with pytest.raises(RateLimitEncountered):await selector.rank()


async def test_all_legacy_business_data_survive_reopen(cfg,db):
    async with db.transaction() as c:
        await c.execute('INSERT INTO jr_membership VALUES(?,?,?)',('10001','member',1))
        await c.execute('INSERT INTO jr_daily_usage VALUES(?,?,?)',('10001','2020-01-01',4))
        await c.execute('INSERT INTO banned_users VALUES(?,?,?)',(100,'fixture',1))
        await c.execute('INSERT INTO last_sign_time VALUES(?,?,?)',('10001','@jrsgk',1))
    await db.close()
    reopened=await Database(cfg.database).open()
    try:
        for table in ('jr_membership','jr_daily_usage','banned_users','last_sign_time'):
            assert len(await reopened.fetchall(f'SELECT * FROM {table}'))==1
    finally:await reopened.close()


async def test_unknown_column_migration_is_refused(cfg):
    with sqlite3.connect(cfg.database) as c:
        c.execute('CREATE TABLE last_sign_time(phone TEXT PRIMARY KEY,timestamp REAL,unknown TEXT)')
        c.execute("INSERT INTO last_sign_time VALUES('10001',1,'preserve')")
    before=hashlib.sha256(cfg.database.read_bytes()).hexdigest()
    with pytest.raises(ValueError):await Database(cfg.database).open()
    assert hashlib.sha256(cfg.database.read_bytes()).hexdigest()==before


async def test_cancellation_rolls_back_transaction(db):
    started=asyncio.Event()
    async def writing():
        async with db.transaction() as c:
            await c.execute("INSERT INTO banned_users VALUES(99,'fixture',1)")
            started.set(); await asyncio.Event().wait()
    task=asyncio.create_task(writing());await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert await db.fetchone('SELECT * FROM banned_users WHERE user_id=99') is None


async def test_download_missing_file_is_explicit(cfg,db,client,bot):
    queries=QueryRepository(db);row,_=await queries.claim('u1','key',time.time())
    cache=CacheService(cfg,CacheRepository(db));exports=ExportService(cfg,None,queries,cache,bot)
    client.download_media.return_value=None
    with pytest.raises(FileNotFoundError):await exports.download(row['task_id'],client,message(document=True))
    # The durable reference protects any partial file pending manual review.
    assert (await queries.get(row['task_id']))['file_path']


def test_snapshot_script_consistent_and_no_overwrite(tmp_path):
    script=Path(__file__).resolve().parents[2]/'scripts/snapshot_database.py'
    spec=importlib.util.spec_from_file_location('snapshot_test',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source=tmp_path/'old.db';destination=tmp_path/'copy.db'
    with sqlite3.connect(source) as c:
        c.execute('CREATE TABLE test(value TEXT)');c.execute("INSERT INTO test VALUES('fixture')")
    before=source.read_bytes();module.snapshot(source,destination)
    assert source.read_bytes()==before
    with sqlite3.connect(destination) as c:assert c.execute('SELECT value FROM test').fetchone()[0]=='fixture'
    with pytest.raises(FileExistsError):module.snapshot(source,destination)
