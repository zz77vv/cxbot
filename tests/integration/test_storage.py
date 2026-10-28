import sqlite3
import time
import asyncio
import random
import pytest
from telegram_bot.storage.database import Database
from telegram_bot.storage.repositories.checkin_repository import CheckinRepository
from telegram_bot.storage.repositories.query_repository import QueryRepository
from telegram_bot.storage.repositories.membership_repository import MembershipRepository, usage_date
from telegram_bot.storage.repositories.user_repository import UserRepository
from telegram_bot.models.enums import QueryState
from telegram_bot.models.query import JRQueryResult
from telegram_bot.target_bots import TARGETS


@pytest.mark.parametrize('with_bot',[False,True])
async def test_legacy_migration_preserves_and_backs_up(cfg,with_bot):
    with sqlite3.connect(cfg.database) as c:
        c.execute('CREATE TABLE last_sign_time(phone TEXT PRIMARY KEY,'+('bot TEXT,' if with_bot else '')+'timestamp REAL NOT NULL)')
        if with_bot: c.execute('INSERT INTO last_sign_time VALUES(?,?,?)',('10001','@JRSGK',123.0))
        else: c.execute('INSERT INTO last_sign_time VALUES(?,?)',('10001',123.0))
        c.execute('CREATE TABLE jr_result_cache(cache_key TEXT PRIMARY KEY,result_text TEXT NOT NULL,source_phone TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL)')
        c.execute('INSERT INTO jr_result_cache VALUES(?,?,?,?,?)',('key','old-result','10001',1,2))
    db=await Database(cfg.database).open()
    assert db.backup_path.is_file()
    with sqlite3.connect(db.backup_path) as c:
        assert c.execute('SELECT COUNT(*) FROM last_sign_time').fetchone()[0]==1
    assert len(await db.fetchall('SELECT * FROM last_sign_time'))==(1 if with_bot else 3)
    assert (await db.fetchone('SELECT * FROM jr_result_cache'))['result_type']=='text'
    assert (await db.fetchone('SELECT result_text FROM jr_result_cache'))[0]=='old-result'
    await db.close()
    db=await Database(cfg.database).open()
    assert len(await db.fetchall('SELECT * FROM last_sign_time'))==(1 if with_bot else 3)
    await db.close()


async def test_unknown_schema_untouched(cfg):
    with sqlite3.connect(cfg.database) as c: c.execute('CREATE TABLE unknown_table(secret TEXT)')
    before=cfg.database.read_bytes()
    with pytest.raises(ValueError): await Database(cfg.database).open()
    assert cfg.database.read_bytes()==before


async def test_transaction_rollback_reopen(cfg,db):
    with pytest.raises(RuntimeError):
        async with db.transaction() as c:
            await c.execute('INSERT INTO banned_users VALUES(1,\'test\',0)')
            raise RuntimeError('crash')
    await db.close()
    reopened=await Database(cfg.database).open()
    assert not await UserRepository(reopened).is_banned(1)
    await reopened.close()


async def test_atomic_usage_cache_and_idempotence(db):
    repo=QueryRepository(db)
    row,fresh=await repo.claim('u1','key',time.time())
    assert fresh and await repo.reserve(row['task_id'],'10001',time.time())
    result=JRQueryResult('text','🔍共1条信息\nfixture')
    await repo.complete(row['task_id'],result,time.time(),300)
    await repo.complete(row['task_id'],result,time.time(),300)
    assert (await MembershipRepository(db).state('10001',time.time()))[2]==1
    assert (await db.fetchone('SELECT result_text FROM jr_result_cache'))[0]==result.text
    old,fresh=await repo.claim('u1','other-key',time.time())
    assert not fresh and old['task_id']==row['task_id']


async def test_reservation_and_restart_block(db):
    repo=QueryRepository(db)
    row,_=await repo.claim('u1','key',time.time())
    task=row['task_id']
    assert await repo.reserve(task,'10001',time.time())
    await repo.transition(task,QueryState.PURCHASING,sent_id=10,source_id=11)
    recovered=await repo.recover()
    assert len(recovered)==1
    old,fresh=await repo.claim('u2','key',time.time())
    assert not fresh and old['state']=='pending_review' and old['sent_id']==10
    assert old['phase']=='purchasing'
    assert await repo.unresolved_account('10001')
    await repo.review(task,'failed','manually verified no charge')
    assert not await repo.unresolved_account('10001')


async def test_daily_limit_atomic(db):
    repo=QueryRepository(db)
    for i in range(5):
        row,_=await repo.claim(str(i),str(i),time.time())
        assert await repo.reserve(row['task_id'],'10001',time.time())
        await repo.complete(row['task_id'],JRQueryResult('text','x'),time.time(),300)
    sixth,_=await repo.claim('six','six',time.time())
    assert not await repo.reserve(sixth['task_id'],'10001',time.time())


async def test_ban_table_preserved(db):
    async with db.transaction() as c: await c.execute("INSERT INTO banned_users VALUES(12,'fixture',0)")
    assert await UserRepository(db).is_banned(12)


async def test_first_schedule_is_persisted(db):
    repo=CheckinRepository(db)
    row=await repo.initialize('10001',TARGETS[0].key,1000,random.Random(42))
    assert 1000<=row['next_at']<=87400 and row['last_success'] is None
    again=await repo.initialize('10001',TARGETS[0].key,2000,random.Random(43))
    assert row['next_at']==again['next_at']


def test_china_usage_date():
    assert usage_date(0)=='1970-01-01'
    assert usage_date(16*3600)=='1970-01-02'
