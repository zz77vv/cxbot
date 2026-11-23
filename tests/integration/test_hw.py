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


@pytest.mark.parametrize('old_task,changed', [(False, False), (True, False), (False, True)])
async def test_hw_standalone_txt_waits_for_exact_admin_choice(setup_jr, manager, bot, client, monkeypatch,
                                                     old_task, changed):
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    from telegram_bot.clients.request_tracker import RequestTracker
    from telegram_bot.handlers.jr_tasks import make_callback
    from telegram_bot.services.hw_query_service import export_candidates

    jr, queries, _, exports, _ = setup_jr
    cfg = manager.cfg
    monkeypatch.setattr('telegram_bot.services.hw_query_service.EXPORT_WAIT', 0.03)
    service = HWQueryService(cfg, manager, Dialogue(cfg), queries, exports)
    client.send_message.return_value = SimpleNamespace(id=10, peer_id=SimpleNamespace(user_id=900))
    upstream = hw_message('查询结果\n测试', 12, buttons=[[KeyboardButtonCallback('导出', b'export')]])
    file = hw_message('已导出为文件', 13, document=SimpleNamespace(id=99),
                      file=SimpleNamespace(name='上游结果.txt', size=154), date=datetime.now(timezone.utc))

    def history(target, **options):
        identifier = options.get('ids')
        if identifier:
            return upstream if identifier == 12 else file
        if options.get('min_id'):
            return [file]
        return ([file, upstream] if upstream.click.await_count else [upstream])[:options['limit']]

    async def download(message, file):
        Path(file).write_bytes(b'original fixture')
        return file

    client.get_messages.side_effect = history
    client.download_media.side_effect = download
    if old_task:
        row, _ = await queries.claim('hw:old', 'hw:fixture', time.time() - 30)
        task = row['task_id']
        await queries.start_hw(task, next(iter(manager.accounts)))
        await queries.transition(task, QueryState.PENDING_REVIEW, 'hw_export_unconfirmed', sent_id=10, source_id=12)
    else:
        report = await service.execute('fixture', 'sample')
        assert report.reason == 'hw_export_unconfirmed'
        task = report.task_id
    bot.send_document.assert_not_awaited()
    client.download_media.assert_not_awaited()
    row = await queries.get_exception(task)
    if old_task:
        assert not export_candidates(row)
        assert await service.find_export_candidates(row)
        row = await queries.get_exception(task)
    assert [item['id'] for item in export_candidates(row)] == [13]

    ctx = SimpleNamespace(config=cfg, requests=RequestTracker(), hw=service, jr=jr)
    edit = AsyncMock()
    query = SimpleNamespace(data=f'jrtask:file:{task}:13', answer=AsyncMock(), edit_message_text=edit)
    update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                             effective_user=SimpleNamespace(id=cfg.admin_id), callback_query=query)
    callback = make_callback(ctx)
    await callback(update, None)
    assert '154 字节' in edit.call_args.args[0]
    query.data = edit.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
    assert len(query.data.encode()) <= 64
    client.download_media.assert_not_awaited()
    if changed:
        file.document.id = 100
    await callback(update, None)
    if changed:
        assert (await queries.get(task))['state'] == QueryState.PENDING_REVIEW
        client.download_media.assert_not_awaited()
        bot.send_document.assert_not_awaited()
    else:
        await callback(update, None)
        assert (await queries.get(task))['delivery_state'] == 'delivered'
        client.download_media.assert_awaited_once()
        bot.send_document.assert_awaited_once()
        assert bot.send_document.call_args.kwargs['filename'] == '上游结果.txt'
    assert client.send_message.await_count == upstream.click.await_count == (0 if old_task else 1)


async def test_hw_unlinked_text_does_not_deliver_or_repeat(setup_jr, manager, client, bot, monkeypatch):
    _, queries, _, exports, _ = setup_jr
    monkeypatch.setattr('telegram_bot.services.hw_query_service.RESULT_WAIT', 0.03)
    service = HWQueryService(manager.cfg, manager, Dialogue(manager.cfg), queries, exports)
    client.send_message.return_value = SimpleNamespace(id=10, peer_id=SimpleNamespace(user_id=900))
    client.get_messages.return_value = [hw_message('查询结果\n无关联的模拟文本')]
    report = await service.execute('1:unlinked-text', 'fixture')
    assert report.state == QueryState.PENDING_REVIEW and report.reason == 'hw_result_unconfirmed'
    bot.send_message.assert_not_awaited()
    assert (await service.execute('1:unlinked-text', 'fixture')).duplicate
    client.send_message.assert_awaited_once()


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
    result = hw_message('查询结果\n脱敏测试记录\n消耗 105 盒能', 12, reply_to_msg_id=10)
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


@pytest.mark.parametrize('case', ['unlinked', 'other_task', 'unlinked_parent', 'cross_chat',
                                  'direct', 'valid_after_unlinked'])
async def test_hw_export_requires_same_chat_reply_chain(setup_jr, manager, bot, client, monkeypatch, case):
    from pathlib import Path

    _, queries, _, exports, _ = setup_jr
    monkeypatch.setattr('telegram_bot.services.hw_query_service.EXPORT_WAIT', 0.03)
    cfg = manager.cfg
    service = HWQueryService(cfg, manager, Dialogue(cfg), queries, exports)
    client.send_message.return_value = SimpleNamespace(id=10, peer_id=SimpleNamespace(user_id=900))
    upstream_result = hw_message('查询结果\n模拟文本', 12,
                                 reply_to_msg_id=None if case in ('unlinked_parent', 'direct') else 10,
                                 buttons=[[KeyboardButtonCallback('导出', b'export')]])
    reply = {'unlinked': None, 'other_task': 8, 'direct': 10, 'valid_after_unlinked': None}.get(case, 12)
    candidate = hw_message('已导出为文件', 13, reply_to_msg_id=reply, document=True,
                           file=SimpleNamespace(name='待验证.txt'))
    if case == 'cross_chat':
        candidate.reply_to = SimpleNamespace(reply_to_peer_id=SimpleNamespace(user_id=999))
    verified_file = hw_message('已导出为文件', 14, reply_to_msg_id=12, document=True,
                               file=SimpleNamespace(name='已验证.txt'))

    def history(target, **options):
        rows = [upstream_result]
        if upstream_result.click.await_count:
            rows.insert(0, candidate)
            if case == 'valid_after_unlinked':
                rows.insert(0, verified_file)
        return rows[:options['limit']]

    async def download(msg, file):
        assert msg is (verified_file if case == 'valid_after_unlinked' else candidate)
        Path(file).write_bytes(b'fixture')
        return file

    client.get_messages.side_effect = history
    client.download_media.side_effect = download
    report = await service.execute('1:correlate', 'fixture')
    if case in ('direct', 'valid_after_unlinked'):
        assert report.state == QueryState.SUCCESS
        bot.send_document.assert_awaited_once()
    else:
        assert report.state == QueryState.PENDING_REVIEW and report.reason == 'hw_export_unconfirmed'
        assert (await queries.get(report.task_id))['delivery_state'] == 'not_started'
        bot.send_document.assert_not_awaited()
        client.download_media.assert_not_awaited()
    assert (await service.execute('1:correlate', 'fixture')).duplicate
    client.send_message.assert_awaited_once()
    upstream_result.click.assert_awaited_once_with(0, 0)


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
