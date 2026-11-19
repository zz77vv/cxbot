from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from conftest import message, button
from telegram_bot.models.enums import QueryState
from telegram_bot.services.jr.result_parser import parse_result_header, parse_page_info, classify, normalize_jr_full_result
from telegram_bot.services.jr.target_client import TargetClient

# Reuse the real repositories, cache, pagination and delivery fixture.
from test_jr import setup_jr


def page(n, total=7, count=39, *, next_button=True):
    return message(f'🔍️共{count}条信息 (第 {n} 页，共 {total} 页):\n记录 {n}',
                   id=10+n, buttons=[[button('下一页')]] if next_button else None)


@pytest.mark.parametrize('title', [
    '🔍️共39条信息 (第 1 页，共 7 页):',
    '🔍︎ 共 ３９ 条 信息（第１页，共７页）：',
    '🔍共39条信息 (1/7):',
    '🔍 共39条信息 第 1 / 7 页:',
    '🔍共39条信息（第1页 共7页）',
    '\n\n 🔍️共39条信息 (第1页，共7页):',
])
def test_actual_headers(title):
    msg=message(title+'\nfixture')
    assert classify(msg)=='result'
    assert parse_page_info(msg)==(1,7)
    assert parse_result_header(msg).total_records==39


def test_local_continuation_is_not_a_page_marker():
    assert parse_page_info(message('—— 续 2/2 ——\nfixture')) is None
    assert parse_page_info(message('🔍共39条信息\n正文提到第 1 页，共 7 页')) is None
    body='🔍共39条信息\n—— 续 2/2 ——\n1/7'
    assert normalize_jr_full_result(body)==body


async def test_real_seven_pages_send_one_utf8_txt_and_cache(setup_jr,bot,monkeypatch):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL',0)
    jr,repo,target,exports,_=setup_jr
    target.ready.return_value=page(1)
    target.click_and_wait.side_effect=[page(n,next_button=n<7) for n in range(2,8)]
    captured=[]
    async def sent_document(**kwargs):
        captured.append((kwargs['filename'],kwargs['document'].read()))
    bot.send_document.side_effect=sent_document
    report=await jr.execute('paged','fixture')
    assert report.state==QueryState.SUCCESS and report.result.kind=='file'
    assert target.click_and_wait.await_count==6
    assert bot.send_document.await_count==1
    bot.send_message.assert_not_awaited()
    name,payload=captured[0]
    assert name.endswith('.txt')
    text=payload.decode('utf-8')
    assert text.count('🔍共39条信息')==1 and '页' not in text
    assert text=='🔍共39条信息\n记录 1'+''.join(f'\n\n记录 {n}' for n in range(2,8))
    assert Path(report.result.file_path).read_bytes()==payload
    await jr.execute('paged-again','fixture')
    assert bot.send_document.await_count==2 and target.submit.await_count==1


@pytest.mark.parametrize('title', ['🔍️共999条信息 (第 1 页，共 1 页):', '🔍共999条信息'])
async def test_single_page_remains_text_regardless_length(setup_jr,bot,title):
    jr,repo,target,exports,_=setup_jr
    target.ready.return_value=message(title+'\n'+('长内容\n'*1500),buttons=[[button('导出数据')]])
    report=await jr.execute('single','fixture')
    assert report.state==QueryState.SUCCESS and report.result.kind=='text'
    assert bot.send_message.await_count>1  # Telegram transport splitting only.
    bot.send_document.assert_not_awaited()
    target.click_and_wait.assert_not_awaited()
    target.ready.return_value.click.assert_not_awaited()


async def test_headerless_button_chain_to_confirmed_end(setup_jr,bot,monkeypatch):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL',0)
    jr,repo,target,*_=setup_jr
    target.ready.return_value=message('🔍共2条信息\nA',buttons=[[button('下一页')]])
    target.click_and_wait.return_value=message('🔍共2条信息\nB')
    report=await jr.execute('buttons','fixture')
    assert report.state==QueryState.SUCCESS and report.result.kind=='file'
    assert Path(report.result.file_path).read_text(encoding='utf-8')=='🔍共2条信息\nA\n\nB'
    bot.send_document.assert_awaited_once()


async def test_single_page_header_takes_priority_over_navigation(setup_jr,bot):
    jr,repo,target,*_=setup_jr
    target.ready.return_value=page(1,total=1,next_button=True)
    report=await jr.execute('one-page','fixture')
    assert report.state==QueryState.SUCCESS and report.result.kind=='text'
    target.click_and_wait.assert_not_awaited()
    bot.send_document.assert_not_awaited()


@pytest.mark.parametrize('case', ['skip','total_changed','marker_missing','count_changed',
                                 'duplicate','no_next','unknown_end','verification'])
async def test_incomplete_results_never_delivered_or_cached(setup_jr,bot,db,monkeypatch,case):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL',0)
    jr,repo,target,*_=setup_jr
    first=page(1)
    variants={'skip':page(3),'total_changed':page(2,8),'marker_missing':message('🔍共39条信息\nB'),
              'count_changed':page(2,count=40),'duplicate':page(1),
              'unknown_end':message('格式变化'),'verification':message('安全验证')}
    if case=='no_next':first=page(1,next_button=False)
    target.ready.return_value=first
    target.click_and_wait.return_value=variants.get(case)
    report=await jr.execute('incomplete','fixture')
    assert report.state in (QueryState.PENDING_REVIEW,QueryState.MANUAL)
    bot.send_document.assert_not_awaited()
    assert await db.fetchone('SELECT * FROM jr_result_cache') is None
    assert (await repo.get(report.task_id))['delivery_state']=='not_started'
    await jr.execute('incomplete-repeated','fixture')
    assert target.submit.await_count==1


@pytest.mark.parametrize('recover', [True, False])
async def test_timeout_at_page_60_continues_or_delivers_marked_partial(setup_jr, bot, db, monkeypatch, recover):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL', 0)
    jr, repo, target, *_ = setup_jr
    target.ready.return_value = page(1, total=150, count=750)
    responses = [page(n, total=150, count=750) for n in range(2, 61)]
    responses.append(TimeoutError() if recover else None)
    if recover:
        responses.extend(page(n, total=150, count=750, next_button=n<150) for n in range(62, 151))
    target.click_and_wait.side_effect = responses
    target.confirm_next_page = AsyncMock(return_value=page(61, total=150, count=750) if recover else None)

    report = await jr.execute('large-pages', 'fixture')

    target.submit.assert_awaited_once()
    target.confirm_next_page.assert_awaited_once()
    assert target.confirm_next_page.call_args.args[1].id == 70
    bot.send_document.assert_awaited_once()
    row = await repo.get(report.task_id)
    assert row['delivery_state'] == 'delivered'
    payload = Path(report.result.file_path).read_text(encoding='utf-8')
    if recover:
        assert report.state == QueryState.SUCCESS
        assert target.click_and_wait.await_count == 149
        assert payload.endswith('记录 150')
        assert await db.fetchone('SELECT * FROM jr_result_cache') is not None
    else:
        assert report.state == QueryState.PENDING_REVIEW
        assert target.click_and_wait.await_count == 60
        assert '已收集 60/150 页' in payload and payload.endswith('记录 60')
        assert '部分结果' in bot.send_document.call_args.kwargs['filename']
        assert row['result_type'] == 'file'
        assert await db.fetchone('SELECT * FROM jr_result_cache') is None
        assert (await jr.execute('large-pages-repeat', 'fixture')).duplicate
        target.submit.assert_awaited_once()
        bot.send_document.assert_awaited_once()


async def test_confirm_next_page_only_accepts_linked_reply_or_original_edit(cfg, client):
    current = page(60, total=150)
    unlinked = page(61, total=150)
    linked = page(61, total=150)
    linked.reply_to_msg_id = current.id
    edited = page(61, total=150)
    edited.id = current.id
    wrong_sender = page(61, total=150)
    wrong_sender.id = current.id
    wrong_sender.sender_id = 1000

    async def wait(client, bot, after_id, predicate, timeout, **kwargs):
        assert after_id == current.id and kwargs['original'] is current
        assert not predicate(unlinked) and not predicate(wrong_sender)
        assert predicate(edited) and predicate(linked)
        return edited

    target = TargetClient(SimpleNamespace(wait=wait), cfg)
    result = await target.confirm_next_page(client, current, lambda m: parse_page_info(m) == (61, 150))
    assert result is edited
    current.click.assert_not_awaited()
    client.send_message.assert_not_awaited()
