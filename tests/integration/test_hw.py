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
    msg = SimpleNamespace(text='/hw@fixture 查询  内容\n下一行', reply_text=reply)
    update = SimpleNamespace(effective_user=username, effective_chat=chat, effective_message=msg, update_id=2)
    handle = make_handler(ctx)

    await handle(update, None)
    username.id = cfg.admin_id
    chat.type = 'group'
    await handle(update, None)
    execute.assert_not_awaited()
    chat.type = 'private'
    await handle(update, None)
    execute.assert_awaited_once_with(f'{cfg.admin_id}:2', '查询  内容\n下一行')
