import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest
from conftest import message, button
from test_jr import setup_jr
from telegram_bot.handlers.jr_query import make_handler
from telegram_bot.clients.request_tracker import RequestTracker
from telegram_bot.models.enums import QueryState, AccountState, Outcome
from telegram_bot.models.account import Account, OperationResult
from telegram_bot.services.jr.progress import JRProgress
from telegram_bot.services.jr.account_selector import AccountSelector
from telegram_bot.services.jr.membership_service import MembershipService
from telegram_bot.storage.repositories.membership_repository import MembershipRepository, usage_date


@pytest.fixture
def progress():
    return JRProgress(SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock()), interval=0)


async def execute(jr, progress, request='u1', text='fixture'):
    report = await jr.execute(request, text, progress=progress)
    await progress.finish(report)
    return report


def edits(progress):
    return [c.args[0] for c in progress.message.edit_text.await_args_list]


def page(n, total=7):
    return message(f'🔍️共39条信息 (第 {n} 页，共 {total} 页):\n数据 {n}', id=20+n,
                   buttons=[[button('下一页')]] if n < total else None)


async def test_single_text_edits_progress_without_new_result_message(setup_jr, progress, bot, db):
    jr, repo, target, *_ = setup_jr
    report = await execute(jr, progress)
    assert report.state == QueryState.SUCCESS
    assert edits(progress)[-1] == '✅ 巨人查询完成\n\n🔍共1条信息\nfixture result'
    assert all(any(f'{n}/7 ' in s for s in edits(progress)) for n in range(2,8))
    bot.send_message.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    progress.message.delete.assert_not_awaited()
    assert (await repo.get(report.task_id))['delivery_state'] == 'delivered'
    assert (await db.fetchone('SELECT query_count FROM jr_daily_usage'))[0] == 1


async def test_seven_pages_document_then_delete_only_once(setup_jr, progress, bot, monkeypatch):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL', 0)
    jr, repo, target, *_ = setup_jr
    target.ready.return_value = page(1)
    target.click_and_wait.side_effect = [page(n) for n in range(2,8)]
    actions=[]
    async def send(**kwargs):
        text=kwargs['document'].read().decode('utf-8')
        assert text.count('🔍共39条信息') == 1
        assert all(f'数据 {n}' in text for n in range(1,8))
        assert '页' not in text
        actions.append('file')
    async def delete(): actions.append('delete')
    bot.send_document.side_effect=send
    progress.message.delete.side_effect=delete
    report=await execute(jr,progress)
    assert actions==['file','delete'] and progress.deleted
    assert all(any(f'已收集 {n}/7 页' in s for s in edits(progress)) for n in range(1,8))
    assert target.click_and_wait.await_count==6
    await jr.exports.deliver(report.task_id,report.result,progress=progress)
    assert actions==['file','delete']
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('case', ['timeout','skip','ocr','purchase_timeout'])
async def test_failure_stays_in_one_progress_message(setup_jr,progress,bot,case):
    jr,repo,target,*_=setup_jr
    if case=='timeout': target.ready.return_value=None
    elif case=='skip':
        target.ready.return_value=page(1)
        target.click_and_wait.return_value=page(3)
    elif case=='ocr':
        target.ready.return_value=message('安全验证',buttons=[[button('字')]],media=True)
        client=next(iter(jr.manager.accounts.values())).client
        client.download_media.return_value=b'fixture'
    else:
        target.ready.return_value=message('预览',buttons=[[button('购买 5 积分')]])
        target.click_and_wait.return_value=None
    report=await execute(jr,progress)
    assert report.state in (QueryState.PENDING_REVIEW,QueryState.MANUAL)
    assert '状态：' in edits(progress)[-1]
    bot.send_message.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    again=JRProgress(SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock()),interval=0)
    duplicate=await execute(jr,again,'u2')
    assert duplicate.duplicate and target.submit.await_count==1
    assert target.click_and_wait.await_count <= 1


async def test_ocr_and_purchase_preserve_existing_send_limits(setup_jr,progress,bot,db):
    jr,repo,target,*_=setup_jr
    prompt=message('安全验证',buttons=[[button('字')]],media=True)
    next(iter(jr.manager.accounts.values())).client.download_media.return_value=b'fixture'
    jr.verification.ocr.recognize_captcha.side_effect=None
    jr.verification.ocr.recognize_captcha.return_value=(1,'字',0.9)
    jr.verification.dialogue.wait.return_value=message('验证通过')
    target.ready.side_effect=[prompt,message('预览',buttons=[[button('购买 5 积分')]])]
    report=await execute(jr,progress)
    assert report.state==QueryState.SUCCESS and progress.text_delivered
    prompt.click.assert_awaited_once_with(0,0)
    assert target.submit.await_count==2 and target.click_and_wait.await_count==1
    assert (await db.fetchone('SELECT query_count FROM jr_daily_usage'))[0]==1
    assert any('4/7 处理验证与购买' in s for s in edits(progress))
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('long_text', [False, True])
async def test_cache_hit_does_not_submit_and_long_text_becomes_file(setup_jr,progress,bot,db,long_text):
    from telegram_bot.services.jr.cache_service import jr_cache_key
    jr,repo,target,*_=setup_jr
    text='🔍共1条信息\n'+('长内容'*2000 if long_text else '缓存')
    async with db.transaction() as c:
        await c.execute('INSERT INTO jr_result_cache VALUES(?,?,?,?,?,?,?,?)',
                        (jr_cache_key('fixture'),text,'10001',time.time(),time.time()+300,'text',None,None))
    report=await execute(jr,progress)
    assert report.reason=='cache_hit'
    target.submit.assert_not_awaited()
    jr.selector.rank.assert_not_awaited()
    assert await db.fetchone('SELECT * FROM jr_daily_usage') is None
    if long_text:
        assert report.result.kind=='file' and progress.deleted
        assert Path(report.result.file_path).read_text(encoding='utf-8')==text
        assert (await db.fetchone('SELECT result_type FROM jr_result_cache'))[0]=='text'
    else: assert progress.text_delivered
    bot.send_message.assert_not_awaited()


async def test_oversized_single_page_is_one_document(setup_jr,progress,bot):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=message('🔍共1条信息 (第1页，共1页):\n'+'😀'*3000)
    report=await execute(jr,progress)
    assert report.result.kind=='file' and progress.deleted
    bot.send_document.assert_awaited_once()
    bot.send_message.assert_not_awaited()


async def test_file_cache_hit_uses_same_delivery_cleanup_without_query(setup_jr,progress,bot,monkeypatch):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL',0)
    jr,repo,target,*_=setup_jr
    target.ready.return_value=page(1,2)
    target.click_and_wait.return_value=page(2,2)
    first=await execute(jr,progress)
    again=JRProgress(SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock()),interval=0)
    cached=await execute(jr,again,'u2')
    assert cached.reason=='cache_hit' and again.deleted and progress.deleted
    assert cached.result.file_path==first.result.file_path
    assert target.submit.await_count==1 and target.click_and_wait.await_count==1
    assert bot.send_document.await_count==2
    bot.send_message.assert_not_awaited()


def test_text_limit_counts_utf16_and_final_heading():
    from telegram_bot.utils.message_format import utf16_len
    size=4096-utf16_len('✅ 巨人查询完成\n\n')
    assert JRProgress.text_fits('字'*size)
    assert not JRProgress.text_fits('字'*(size+1))
    assert not JRProgress.text_fits('😀'*size)


@pytest.mark.parametrize('failure', ['send','delete','text_edit','file_record'])
async def test_delivery_uncertainty_never_requeries_or_resends(setup_jr,progress,bot,monkeypatch,failure):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL',0)
    jr,repo,target,*_=setup_jr
    if failure=='text_edit':
        original=progress.message.edit_text
        async def edit(text):
            if text.startswith('✅ 巨人查询完成'):raise TimeoutError()
        original.side_effect=edit
    else:
        target.ready.return_value=page(1,2)
        target.click_and_wait.return_value=page(2,2)
        if failure=='send':bot.send_document.side_effect=TimeoutError()
        elif failure=='delete':progress.message.delete.side_effect=TimeoutError()
        else:
            original=repo.delivery
            async def record(task,state,cursor=None):
                if state=='delivered':raise RuntimeError('fixture')
                return await original(task,state,cursor)
            monkeypatch.setattr(repo,'delivery',record)
    report=await execute(jr,progress)
    row=await repo.get(report.task_id)
    if failure=='delete':
        assert row['delivery_state']=='delivered' and report.reason=='query_completed'
        assert '文件已发送' in edits(progress)[-1]
    else:
        assert row['delivery_state']=='unknown' and report.reason=='delivery_requires_review'
        assert '等待人工核实' in edits(progress)[-1]
        progress.message.delete.assert_not_awaited()
    files=bot.send_document.await_count
    again=JRProgress(SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock()),interval=0)
    await execute(jr,again,'u1')
    assert bot.send_document.await_count==files and target.submit.await_count==1
    bot.send_message.assert_not_awaited()


async def test_same_update_handler_sends_exactly_one_placeholder(setup_jr,cfg,bot,monkeypatch):
    jr,*_=setup_jr
    class FastProgress(JRProgress):
        def __init__(self,message,timeout):super().__init__(message,timeout,interval=0)
    monkeypatch.setattr('telegram_bot.handlers.jr_query.JRProgress',FastProgress)
    ui=SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock())
    msg=SimpleNamespace(text='/jr+fixture',reply_text=AsyncMock(return_value=ui))
    update=SimpleNamespace(effective_message=msg,effective_user=SimpleNamespace(id=cfg.admin_id),
                           effective_chat=SimpleNamespace(id=cfg.admin_id),update_id=88)
    ctx=SimpleNamespace(config=cfg,requests=RequestTracker(),jr=jr)
    handler=make_handler(ctx)
    await asyncio.gather(*(handler(update,SimpleNamespace(bot=SimpleNamespace(username='fixturebot'))) for _ in range(5)))
    msg.reply_text.assert_awaited_once()
    assert '1/7 接收查询任务' in msg.reply_text.await_args.args[0]
    assert ui.edit_text.await_args.args[0].startswith('✅ 巨人查询完成')
    bot.send_message.assert_not_awaited()


async def test_edit_failure_does_not_affect_business_and_not_modified_is_success(setup_jr,progress,bot):
    jr,*_=setup_jr
    async def edit(text):
        if text.startswith('⏳'):raise TimeoutError()
        raise BadRequest('Message is not modified')
    progress.message.edit_text.side_effect=edit
    report=await execute(jr,progress)
    assert report.state==QueryState.SUCCESS and progress.text_delivered
    bot.send_message.assert_not_awaited()


async def test_concurrent_requests_edit_only_their_own_message(setup_jr,bot,db):
    jr,repo,target,*_=setup_jr
    async with db.transaction() as c:
        await c.execute("INSERT INTO jr_membership VALUES('10001','member',0)")
    first=JRProgress(SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock()),interval=0)
    second=JRProgress(SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock()),interval=0)
    target.ready.side_effect=[message('🔍共1条信息\n结果甲'),message('🔍共1条信息\n结果乙')]
    reports=await asyncio.gather(execute(jr,first,'a','查询甲'),execute(jr,second,'b','查询乙'))
    assert all(r.state==QueryState.SUCCESS for r in reports)
    assert {edits(first)[-1],edits(second)[-1]}=={'✅ 巨人查询完成\n\n🔍共1条信息\n结果甲','✅ 巨人查询完成\n\n🔍共1条信息\n结果乙'}
    assert not any(reports[1].task_id in s for s in edits(first))
    assert not any(reports[0].task_id in s for s in edits(second))
    bot.send_message.assert_not_awaited()


async def test_failed_placeholder_creation_never_starts_a_query(setup_jr,cfg):
    jr,repo,target,*_=setup_jr
    msg=SimpleNamespace(text='/jr fixture',reply_text=AsyncMock(side_effect=TimeoutError()))
    update=SimpleNamespace(effective_message=msg,effective_user=SimpleNamespace(id=cfg.admin_id),
                           effective_chat=SimpleNamespace(id=cfg.admin_id),update_id=99)
    handler=make_handler(SimpleNamespace(config=cfg,requests=RequestTracker(),jr=jr))
    await handler(update,SimpleNamespace(bot=SimpleNamespace(username='fixturebot')))
    await handler(update,SimpleNamespace(bot=SimpleNamespace(username='fixturebot')))
    msg.reply_text.assert_awaited_once()
    target.submit.assert_not_awaited()


async def test_permanent_member_is_not_expired_or_limited_by_free_quota(setup_jr,db):
    jr,repo,*_=setup_jr
    account=next(iter(jr.manager.accounts.values()))
    member=Account('10099',account.session_path,account.client,AccountState.ONLINE)
    jr.manager.accounts={'10099':member}
    async with db.transaction() as c:
        await c.execute('INSERT INTO jr_membership VALUES(?,?,?)',('10099','member',1))
        await c.execute('INSERT INTO jr_daily_usage VALUES(?,?,?)',('10099',usage_date(time.time()),100))
    balances=SimpleNamespace(query=AsyncMock(return_value=OperationResult(Outcome.SUCCESS,value=126)))
    selector=AccountSelector(jr.manager,balances,MembershipService(MembershipRepository(db)),repo)
    assert (await selector.rank())[0][0].key=='10099'
    row,_=await repo.claim('member','member-key',time.time())
    assert await repo.reserve(row['task_id'],'10099',time.time())
