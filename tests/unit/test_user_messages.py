import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram_bot.models.enums import QueryState, Outcome, AccountState
from telegram_bot.models.query import QueryReport
from telegram_bot.models.account import OperationResult
from telegram_bot.utils.user_messages import state_text, reason_text, REASONS
from telegram_bot.clients.request_tracker import RequestTracker
from telegram_bot.handlers import jr_query, status, balance, checkin


@pytest.mark.parametrize('value', list(QueryState)+list(Outcome)+list(AccountState))
def test_all_states_are_chinese_without_changing_enums(value):
    assert re.search(r'[\u4e00-\u9fff]',state_text(value))
    assert not re.search(r'[A-Za-z_]',state_text(value))


@pytest.mark.parametrize('code', list(REASONS)+[
    'verification_failed:ocr:LowConfidence', 'verification_failed:image_download:TimeoutError',
    'verification_failed:wait_after_click:RuntimeError', 'uncertain_ValueError',
    'operator_review_abandoned', 'unrecognized_code_with_private_payload', '',
])
def test_reasons_do_not_expose_english_or_unknown_payload(code):
    text=reason_text(code)
    assert re.search(r'[\u4e00-\u9fff]',text)
    assert not re.search(r'[A-Za-z_]',text)
    assert 'private_payload' not in text


def user_context(cfg):
    return SimpleNamespace(config=cfg,requests=RequestTracker(),
        status=SimpleNamespace(snapshot=AsyncMock(return_value=[('10001','online','',0),('10002','error','TimeoutError',0)])),
        jr=SimpleNamespace(active=set(),accepting=True,execute=AsyncMock(return_value=QueryReport('task123',QueryState.MANUAL,'verification_failed:ocr:LowConfidence'))),
        balances=SimpleNamespace(all=AsyncMock(return_value=[('10001',[('巨人',OperationResult(Outcome.TIMEOUT,'balance_reply_timeout'))])])),
        checkins=SimpleNamespace(manual=AsyncMock(return_value=[('10001',[('巨人',OperationResult(Outcome.SKIPPED,'minimum_24h_interval'))])])) )


@pytest.mark.parametrize('module,expected',[(status,'在线'),(balance,'等待积分回复超时'),(checkin,'不足二十四小时'),(jr_query,'图片识别置信度不足')])
async def test_real_handlers_render_chinese(cfg,module,expected):
    ctx=user_context(cfg)
    edited=SimpleNamespace(edit_text=AsyncMock())
    msg=SimpleNamespace(text='/jr fixture',reply_text=AsyncMock(return_value=edited))
    update=SimpleNamespace(effective_user=SimpleNamespace(id=cfg.admin_id),effective_message=msg,
                           effective_chat=SimpleNamespace(id=cfg.admin_id),update_id=1)
    await module.make_handler(ctx)(update,SimpleNamespace(bot=SimpleNamespace(username='fixturebot')))
    rendered='\n'.join(str(c.args[0]) for c in msg.reply_text.await_args_list)+ '\n'.join(str(c.args[0]) for c in edited.edit_text.await_args_list)
    assert expected in rendered
    for raw in ('online','TimeoutError','minimum_24h_interval','LowConfidence','manual','True'):
        assert raw not in rendered
    if module is jr_query:
        assert 'task123' in rendered
        assert ctx.jr.execute.return_value.reason=='verification_failed:ocr:LowConfidence'


async def test_background_notification_translates_only_at_presentation(cfg,db,bot):
    from telegram_bot.services.jr.export_service import ExportService
    service=ExportService(cfg,None,None,None,bot)
    await service.notify('task123','manual','verification_failed:ocr:LowConfidence')
    text=bot.send_message.await_args.kwargs['text']
    assert '需要人工处理' in text and '识别图片文字' in text and '置信度不足' in text
    assert 'task123' in text and 'manual' not in text and 'LowConfidence' not in text
