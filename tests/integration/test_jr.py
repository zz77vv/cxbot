import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec
import pytest
from conftest import message,button
from telegram_bot.storage.repositories.query_repository import QueryRepository
from telegram_bot.storage.repositories.cache_repository import CacheRepository
from telegram_bot.services.jr.cache_service import CacheService,jr_cache_key
from telegram_bot.services.jr.orchestrator import Orchestrator
from telegram_bot.services.jr.export_service import ExportService
from telegram_bot.services.jr.pagination import PaginationService
from telegram_bot.models.account import OperationResult
from telegram_bot.models.enums import Outcome,QueryState
from telegram_bot.models.query import JRQueryResult
from telegram_bot.services.jr.ocr_service import OCRService
from telegram_bot.services.jr.verification_service import VerificationService, VerificationError
from telegram_bot.services.jr.account_selector import AccountExclusion, NoEligibleAccountError


@pytest.fixture
def setup_jr(cfg,db,manager,bot):
    queries=QueryRepository(db)
    cache=CacheService(cfg,CacheRepository(db))
    account=next(iter(manager.accounts.values()))
    selector=SimpleNamespace(rank=AsyncMock(return_value=[(account,OperationResult(Outcome.SUCCESS,value=10))]))
    target=SimpleNamespace(submit=AsyncMock(return_value=message(id=10)),
        ready=AsyncMock(return_value=message('🔍共1条信息\nfixture result')),
        click_and_wait=AsyncMock(return_value=message('🔍共1条信息\nfixture result',id=12)))
    exports=ExportService(cfg,SimpleNamespace(),queries,cache,bot)
    ocr=create_autospec(OCRService,instance=True)
    ocr.recognize_captcha.side_effect=VerificationError('mock_recognition_failed')
    verification=VerificationService(ocr,SimpleNamespace(wait=AsyncMock()),target,cfg)
    orchestrator=Orchestrator(manager,queries,selector,target,PaginationService(target),exports,cache,verification)
    return orchestrator,queries,target,exports,cache


async def test_query_success_and_cache(setup_jr,db,bot):
    jr,repo,target,exports,cache=setup_jr
    report=await jr.execute('update1','fixture query')
    assert report.state==QueryState.SUCCESS
    assert (await repo.get(report.task_id))['delivery_state']=='delivered'
    second=await jr.execute('update2',' FIXTURE  query ')
    assert second.reason=='cache_hit'
    assert target.submit.await_count==1
    assert (await db.fetchone('SELECT query_count FROM jr_daily_usage'))[0]==1
    assert bot.send_message.await_count==2


async def test_selection_rejection_never_reserves_or_submits(setup_jr,db):
    jr,repo,target,*_=setup_jr
    jr.selector.rank.side_effect=NoEligibleAccountError((
        AccountExclusion('10001','account_has_unresolved_task'),))
    report=await jr.execute('blocked-update','fixture')
    assert report.reason=='account_has_unresolved_task' and report.state==QueryState.FAILED
    row=await repo.get(report.task_id)
    assert row['account'] is None and row['reserved']==0 and row['sent_id'] is None
    target.submit.assert_not_awaited()
    assert await db.fetchone('SELECT * FROM jr_daily_usage') is None


async def test_duplicate_update_does_not_deliver_twice(setup_jr,bot):
    jr,repo,target,*_=setup_jr
    reports=await asyncio.gather(*(jr.execute('same-update','fixture') for _ in range(6)))
    assert sum(r.duplicate for r in reports)==5
    assert target.submit.await_count==1 and bot.send_message.await_count==1
    assert not jr.locks._locks


async def test_timeout_blocks_same_query_across_restart(setup_jr):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=None
    first=await jr.execute('u1','fixture')
    assert first.state==QueryState.PENDING_REVIEW
    await repo.recover()
    second=await jr.execute('u2','fixture')
    assert second.duplicate and target.submit.await_count==1
    row=await repo.get(first.task_id)
    assert row['sent_id']==10 and row['reserved']==1


async def test_send_timeout_is_unknown_without_retry(setup_jr):
    jr,repo,target,*_=setup_jr
    target.submit.side_effect=TimeoutError()
    first=await jr.execute('u1','fixture')
    assert first.state==QueryState.PENDING_REVIEW
    assert (await repo.get(first.task_id))['state']=='pending_review'
    await jr.execute('u2','fixture')
    assert target.submit.await_count==1


async def test_purchase_timeout_never_clicks_again(setup_jr):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=message('preview',buttons=[[button('购买 5 积分')]])
    target.click_and_wait.return_value=None
    first=await jr.execute('u1','fixture')
    assert first.state==QueryState.PENDING_REVIEW
    await jr.execute('u2','fixture')
    assert target.click_and_wait.await_count==1


async def test_unknown_reply_is_not_success(setup_jr,db):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=message('format changed')
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.PENDING_REVIEW
    assert await db.fetchone('SELECT * FROM jr_result_cache') is None


async def test_security_verification_stops_for_human(setup_jr,client):
    jr,repo,target,*_=setup_jr
    verification=message('安全验证',buttons=[[button('字')]],media=True)
    target.ready.return_value=verification
    client.download_media.return_value=b'fixture image'
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.MANUAL
    verification.click.assert_not_awaited()
    assert target.submit.await_count==1
    assert report.reason=='verification_failed:mock_recognition_failed'
    assert (await repo.get(report.task_id))['state']=='manual'
    await jr.execute('u2','fixture')
    assert target.submit.await_count==1


async def test_verification_resubmits_and_exports_with_new_id(setup_jr,client,db):
    jr,repo,target,exports,*_=setup_jr
    prompt=message('点击下图',buttons=[[button('取消'),button('字')]],media=True)
    client.download_media.return_value=b'fixture image'
    jr.verification.ocr.recognize_captcha.side_effect=None
    jr.verification.ocr.recognize_captcha.return_value=(1,'字',0.9)
    jr.verification.dialogue.wait.return_value=message('验证通过',id=12)
    target.submit.side_effect=[message(id=10),message(id=20)]
    target.ready.side_effect=[prompt,message(id=21,document=True)]
    exports.attempt=AsyncMock(return_value=(JRQueryResult('text','fixture result'),'member',''))
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.SUCCESS
    prompt.click.assert_awaited_once_with(0,1)
    assert target.submit.await_count==2
    assert [c.args[1] for c in target.submit.await_args_list]==['fixture','fixture']
    assert target.ready.await_args.args[1]==20
    assert exports.attempt.await_args.args[3]==20
    assert (await repo.get(report.task_id))['sent_id']==20
    assert (await db.fetchone('SELECT query_count FROM jr_daily_usage'))[0]==1
    await jr.execute('u2','fixture')
    assert target.submit.await_count==2


async def test_explicit_failure_releases_reservation(setup_jr,db):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=message('余额不足')
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.FAILED
    assert (await repo.get(report.task_id))['reserved']==0
    assert await db.fetchone('SELECT * FROM jr_daily_usage') is None


async def test_cancel_persists_intent(setup_jr):
    jr,repo,target,*_=setup_jr
    waiting=asyncio.Event()
    async def wait(*args):
        waiting.set()
        await asyncio.Event().wait()
    target.ready.side_effect=wait
    task=asyncio.create_task(jr.execute('u1','fixture'))
    await waiting.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    row=await repo.db.fetchone('SELECT * FROM query_tasks')
    assert row['state']=='pending_review' and row['sent_id']==10
    assert not jr.active


async def test_delivery_timeout_no_automatic_resend(setup_jr,bot):
    jr,repo,target,*_=setup_jr
    bot.send_message.side_effect=TimeoutError()
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.SUCCESS and report.reason=='delivery_requires_review'
    assert (await repo.get(report.task_id))['delivery_state']=='unknown'
    attempts=bot.send_message.await_count
    await jr.execute('u2','fixture')
    assert bot.send_message.await_count==attempts and target.submit.await_count==1


async def test_missing_cached_file_does_not_purchase(setup_jr,db,cfg):
    jr,repo,target,*_=setup_jr
    async with db.transaction() as c:
        await c.execute('INSERT INTO jr_result_cache VALUES(?,?,?,?,?,?,?,?)',
            (jr_cache_key('fixture'),'','10001',time.time(),time.time()+300,'file',str(cfg.cache/'missing'),'x.txt'))
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.MANUAL
    target.submit.assert_not_awaited()


async def test_concurrent_different_queries_same_account(setup_jr,db):
    jr,repo,target,*_=setup_jr
    reports=await asyncio.gather(jr.execute('u1','fixture 1'),jr.execute('u2','fixture 2'))
    assert all(r.state==QueryState.SUCCESS for r in reports)
    assert (await db.fetchone('SELECT query_count FROM jr_daily_usage'))[0]==2


async def test_success_before_delivery_crash_does_not_repurchase(setup_jr,db):
    jr,repo,target,exports,cache=setup_jr
    row,_=await repo.claim('old-update',jr_cache_key('fixture'),time.time()-1000)
    assert await repo.reserve(row['task_id'],'10001',time.time())
    await repo.complete(row['task_id'],JRQueryResult('text','saved'),time.time()-1000,300)
    await repo.recover()
    report=await jr.execute('new-update','fixture')
    assert report.duplicate and report.state==QueryState.SUCCESS
    target.submit.assert_not_awaited()


async def test_confirmed_empty_result_counts_original_usage(setup_jr,db):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=message('未查询到结果')
    report=await jr.execute('u1','fixture')
    assert report.state==QueryState.SUCCESS
    assert (await db.fetchone('SELECT query_count FROM jr_daily_usage'))[0]==1
