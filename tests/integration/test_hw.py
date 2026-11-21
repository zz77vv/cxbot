from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import message
from telethon.tl.types import KeyboardButtonCallback

from telegram_bot.clients.dialogue import Dialogue
from telegram_bot.models.enums import QueryState
from telegram_bot.services.hw_query_service import HWQueryService
from test_jr import setup_jr


def hw_message(text='', id=11, **extras):
    return message(text, id=id, sender_id=900, **extras)


async def test_explicit_hw_insufficient_balance_is_closed_without_export(setup_jr, manager, bot, client, monkeypatch):
    _, queries, _, exports, _ = setup_jr
    monkeypatch.setattr('telegram_bot.services.hw_query_service.RESULT_WAIT', 0.03)
    service = HWQueryService(manager.cfg, manager, Dialogue(manager.cfg), queries, exports)
    client.send_message.return_value = SimpleNamespace(id=10, peer_id=SimpleNamespace(user_id=900))
    rejected = hw_message('余额不足，请充值', reply_to_msg_id=10)
    client.get_messages.return_value = [rejected]
    report = await service.execute('1:reject', 'fixture')
    assert report.state == QueryState.FAILED and report.reason == 'hw_insufficient_balance'
    assert (await queries.get(report.task_id))['state'] == 'failed'
    rejected.click.assert_not_awaited()
    client.send_message.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('mode', ['file', 'late_file', 'text', 'file_timeout', 'click_timeout', 'result_timeout'])
async def test_hw_result_delivery_and_no_repetition(setup_jr, manager, bot, client, monkeypatch, mode):
    _, queries, _, exports, _ = setup_jr
    monkeypatch.setattr('telegram_bot.services.hw_query_service.RESULT_WAIT', 0.03)
    monkeypatch.setattr('telegram_bot.services.hw_query_service.EXPORT_WAIT', 0.03)
    service = HWQueryService(manager.cfg, manager, Dialogue(manager.cfg), queries, exports)
    result = hw_message('查询结果\n脱敏测试记录\n消耗 105 盒能', 12)
    if mode not in ('text', 'result_timeout'):
        result.buttons = [[KeyboardButtonCallback('📥 导出', b'export'),
                           KeyboardButtonCallback('AI分析(20点)', b'ai')]]
    sent = SimpleNamespace(id=10, peer_id=SimpleNamespace(user_id=900))
    client.send_message.return_value = sent
    old_file = hw_message('已导出为文件', 9, document=True, file=SimpleNamespace(name='旧文件.txt'))
    previous_export = hw_message('已导出为文件', 14, document=True, file=SimpleNamespace(name='同批旧文件.txt'),
                                 reply_to_msg_id=8)
    new_file = hw_message('📥 已导出为文件', 15, document=True, file=SimpleNamespace(name='原始结果.txt'),
                          reply_to_msg_id=12)
    unrelated = hw_message('已导出为文件', 16, document=True, file=SimpleNamespace(name='其他任务.txt'),
                           reply_to_msg_id=8)
    progress = hw_message('🔎 数据查询中\n提示', 11)
    done = hw_message('✅ 快捷查询完成', 13)

    async def history(target, **kwargs):
        history = [old_file, progress, done]
        if mode != 'result_timeout':
            history += [result, previous_export]
        if result.click.await_count:
            history.append(unrelated)
            if mode in ('file', 'late_file'):
                history.append(new_file)
        return sorted(history, key=lambda m: m.id, reverse=True)[:kwargs['limit']]

    async def download(msg, file):
        from pathlib import Path
        assert msg is new_file
        Path(file).write_bytes(b'original\r\nTXT')
        return file

    payloads = []
    async def delivered(**kwargs):
        payloads.append((kwargs['filename'], kwargs['document'].read()))

    client.get_messages.side_effect = history
    client.download_media.side_effect = download
    bot.send_document.side_effect = delivered
    if mode in ('click_timeout', 'late_file'):
        result.click.side_effect = TimeoutError
    report = await service.execute('1:2', 'fixture query')
    client.send_message.assert_awaited_once_with('@hewuqi', 'fixture query')
    if mode not in ('text', 'result_timeout'):
        result.click.assert_awaited_once_with(0, 0)
    else:
        result.click.assert_not_awaited()
    reopened = HWQueryService(manager.cfg, manager, Dialogue(manager.cfg), queries, exports)
    duplicate = await reopened.execute('1:2', 'fixture query')
    assert duplicate.duplicate
    client.send_message.assert_awaited_once()

    row = await queries.get(report.task_id)
    if mode in ('file', 'late_file'):
        assert report.state == QueryState.SUCCESS
        assert payloads == [('原始结果.txt', b'original\r\nTXT')]
        assert row['delivery_state'] == 'delivered'
    elif mode == 'text':
        assert report.state == QueryState.SUCCESS
        assert bot.send_message.await_args.kwargs['text'] == result.text
        bot.send_document.assert_not_awaited()
    else:
        assert report.state == QueryState.PENDING_REVIEW
        assert row['delivery_state'] == 'not_started'
        bot.send_document.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        blocked = await service.execute('1:3', 'different fixture')
        assert blocked.reason == 'hw_pending_task'
        client.send_message.assert_awaited_once()


async def test_hw_handler_only_accepts_admin_private_and_preserves_content(cfg):
    from telegram_bot.clients.request_tracker import RequestTracker
    from telegram_bot.handlers.hw_query import make_handler
    from telegram_bot.models.query import QueryReport

    execute = AsyncMock(return_value=QueryReport('hw-test', QueryState.SUCCESS, 'query_completed'))
    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(), hw=SimpleNamespace(execute=execute))
    username = SimpleNamespace(id=cfg.admin_id + 1)
    chat = SimpleNamespace(id=cfg.admin_id, type='private')
    reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
    msg = SimpleNamespace(text='/hw@fixture 查询  内容', reply_text=reply)
    update = SimpleNamespace(effective_user=username, effective_chat=chat, effective_message=msg, update_id=2)
    handle = make_handler(ctx)

    await handle(update, None)
    username.id = cfg.admin_id
    chat.type = 'group'
    await handle(update, None)
    execute.assert_not_awaited()
    chat.type = 'private'
    await handle(update, None)
    execute.assert_awaited_once_with(f'{cfg.admin_id}:2', '查询  内容')


@pytest.mark.parametrize('stop_reason', ['', 'hw_export_timeout', 'existing_task_no_resubmission'])
async def test_hw_batch_dedupes_in_order_and_stops_without_replaying(cfg, stop_reason):
    from telegram_bot.clients.request_tracker import RequestTracker
    from telegram_bot.handlers.hw_query import make_handler
    from telegram_bot.models.query import QueryReport

    order = []
    async def execute(request, text):
        order.append((request, text))
        if text == '第二条' and stop_reason:
            return QueryReport('needs-review', QueryState.PENDING_REVIEW, stop_reason,
                               duplicate=stop_reason == 'existing_task_no_resubmission')
        return QueryReport('delivered', QueryState.SUCCESS, 'query_completed')

    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(), hw=SimpleNamespace(execute=execute))
    progress = SimpleNamespace(edit_text=AsyncMock())
    msg = SimpleNamespace(text='/hw@fixture 第一  条\n\n 第二条 \n第一  条\n第三条',
                          reply_text=AsyncMock(return_value=progress))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=cfg.admin_id),
                             effective_chat=SimpleNamespace(id=cfg.admin_id, type='private'),
                             effective_message=msg, update_id=23)
    await make_handler(ctx)(update, None)
    req = f'{cfg.admin_id}:23'
    assert order[:2] == [(req, '第一  条'), (f'{req}:batch:2', '第二条')]
    final = progress.edit_text.call_args.args[0]
    if stop_reason:
        assert len(order) == 2
        assert '未提交' in final and '不会重发' in final
    else:
        assert order[2:] == [(f'{req}:batch:3', '第三条')]
        assert '成功 3' in final


async def test_hw_batch_sends_mixed_results_once_even_on_replayed_update(setup_jr, manager, bot, client):
    from pathlib import Path
    from telegram_bot.clients.request_tracker import RequestTracker
    from telegram_bot.handlers.hw_query import make_handler

    _, queries, _, exports, _ = setup_jr
    cfg = manager.cfg
    history = []
    first = hw_message('查询结果\n模拟文本一', 11, reply_to_msg_id=10)
    second = hw_message('查询结果\n模拟文本二', 21, reply_to_msg_id=20,
                        buttons=[[KeyboardButtonCallback('导出', b'export')]])
    file = hw_message('已导出为文件', 22, reply_to_msg_id=21, document=True,
                      file=SimpleNamespace(name='模拟原文件.txt'))

    async def submit(target, text):
        sent_id, response = (10, first) if text == '甲' else (20, second)
        history.append(response)
        return SimpleNamespace(id=sent_id, peer_id=SimpleNamespace(user_id=900))

    async def click(*button):
        history.append(file)

    async def download(message, file):
        Path(file).write_bytes(b'mixed\r\noriginal')
        return file

    delivered = []

    async def capture(**kwargs):
        delivered.append((kwargs['filename'], kwargs['document'].read()))

    client.send_message.side_effect = submit
    client.get_messages.side_effect = lambda target, **kwargs: history[::-1][:kwargs['limit']]
    client.download_media.side_effect = download
    second.click.side_effect = click
    bot.send_document.side_effect = capture
    service = HWQueryService(cfg, manager, Dialogue(cfg), queries, exports)
    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(), hw=service)
    progress = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(text='/hw 甲\n乙\n甲', reply_text=AsyncMock(return_value=progress))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=cfg.admin_id),
                             effective_chat=SimpleNamespace(id=cfg.admin_id, type='private'),
                             effective_message=message, update_id=25)
    handle = make_handler(ctx)
    await handle(update, None)
    assert '成功 2' in progress.edit_text.call_args.args[0]
    await make_handler(ctx)(update, None)
    assert [call.args for call in client.send_message.await_args_list] == [
        ('@hewuqi', '甲'), ('@hewuqi', '乙')]
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs['text'] == first.text
    assert delivered == [('模拟原文件.txt', b'mixed\r\noriginal')]
    second.click.assert_awaited_once_with(0, 0)
