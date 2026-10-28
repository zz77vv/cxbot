import asyncio
import threading
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import pytest
from PIL import Image
from telegram import Update,User
from telegram.ext import Application
from telegram_bot.handlers.jr_query import parse_request
from telegram_bot.handlers.registration import register,COMMANDS,configure_menus
from telegram_bot.handlers.guards import authorized,banned_guard
from telegram.ext import ApplicationHandlerStop
from telegram_bot.services.jr.ocr_service import OCRService,normalize_ocr_text
from telegram_bot.models.enums import Outcome
from telegram_bot.lifecycle import Lifecycle,TaskRegistry
from telegram_bot.clients.bot_application import BotApplication
from telegram_bot.clients.request_tracker import RequestTracker


@pytest.mark.parametrize('text,expected',[('/jr test','test'),('/jr+test','test'),('/JR+ test','test'),
    ('/jr',''),('/jr+',''),('/jr@testbot test','test'),('/jr@other test',None),('/jrxxx',None)])
def test_parse_routes(text,expected):
    assert parse_request(text,'testbot')==expected


def context(cfg):
    return SimpleNamespace(config=cfg,requests=RequestTracker(),users=SimpleNamespace(is_banned=AsyncMock(return_value=False)),
        checkins=SimpleNamespace(manual=AsyncMock(return_value=[])),
        balances=SimpleNamespace(all=AsyncMock(return_value=[])),
        status=SimpleNamespace(snapshot=AsyncMock(return_value=[])),
        jr=SimpleNamespace(execute=AsyncMock(),active=set(),accepting=True))


def telegram_update(text,application,user_id=123):
    application.bot._bot_user = User(999, 'TestBot', True, username='testbot')
    update=Update.de_json({'update_id':1,'message':{'message_id':1,'date':1,
        'chat':{'id':user_id,'type':'private'},'from':{'id':user_id,'is_bot':False,'first_name':'Test'},
        'text':text,'entities':[{'type':'bot_command','offset':0,'length':3}]}},application.bot)
    return update


@pytest.mark.parametrize('text',['/jr fixture','/jr+fixture','/jr+','/jr'])
def test_actual_ptb_route_matches_once(cfg,text):
    app=Application.builder().token('12345:'+'X'*30).build()
    callbacks=register(app,context(cfg))
    assert set(callbacks)==set(COMMANDS)
    update=telegram_update(text,app)
    assert sum(bool(h.check_update(update)) for h in app.handlers[0])==1


async def test_nonadmin_denied_by_real_handler(cfg):
    ctx=context(cfg)
    app=Application.builder().token('12345:'+'X'*30).build()
    callbacks=register(app,ctx)
    update=SimpleNamespace(effective_user=SimpleNamespace(id=999),effective_message=SimpleNamespace(reply_text=AsyncMock()))
    await callbacks['qd'](update,SimpleNamespace())
    ctx.checkins.manual.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once()
    assert not authorized(None,cfg.admin_id)


async def test_banned_guard(cfg):
    ctx=context(cfg); ctx.users.is_banned.return_value=True
    update=SimpleNamespace(effective_user=SimpleNamespace(id=999),effective_message=SimpleNamespace(reply_text=AsyncMock()))
    with pytest.raises(ApplicationHandlerStop): await banned_guard(ctx)(update,None)
    update.effective_user.id=cfg.admin_id
    await banned_guard(ctx)(update,None)


async def test_menu_registration_consistency(cfg,bot):
    await configure_menus(bot,cfg)
    assert bot.delete_my_commands.await_count==15
    menu=bot.set_my_commands.call_args.args[0]
    assert {x.command for x in menu}==set(COMMANDS)
    assert bot.set_my_commands.call_args_list[0].args[0]==[]


def image_bytes():
    out=BytesIO(); Image.new('RGB',(200,100),'white').save(out,format='PNG'); return out.getvalue()


class Engine:
    def __init__(self,confidence=0.9): self.confidence=confidence
    def set_ranges(self,*args): pass
    def classification(self,*args,**kwargs):
        if kwargs.get('probability'): return {'text':'测试','confidence':self.confidence}
        return '1'


async def test_ocr_image_mock_and_legacy_adapter():
    service=OCRService(engine_factory=Engine)
    try:
        result=await service.recognize(image_bytes())
        assert result.outcome==Outcome.SUCCESS and result.text=='测试'
        index,text,confidence=await service.recognize_captcha(image_bytes(),'测试',2)
        assert index==1 and text=='测试' and confidence==0.9
        assert normalize_ocr_text('Ａ b！')=='ab'
    finally: await service.close()


async def test_ocr_low_confidence_bad_image():
    service=OCRService(engine_factory=lambda:Engine(0.1))
    try:
        assert (await service.recognize(image_bytes())).error_type=='LowConfidence'
        assert (await service.recognize(b'bad image')).outcome==Outcome.FAILED
    finally: await service.close()


async def test_ocr_timeout_cannot_enqueue_more_work():
    gate=threading.Event()
    class SlowEngine(Engine):
        def classification(self,*a,**kw): gate.wait(2); return super().classification(*a,**kw)
    service=OCRService(timeout=0.01,engine_factory=SlowEngine)
    try:
        assert (await service.recognize(image_bytes())).outcome==Outcome.TIMEOUT
        assert (await service.recognize(image_bytes())).outcome==Outcome.UNAVAILABLE
    finally:
        gate.set(); await service.close()


async def test_lifecycle_close_cancels_tasks_and_resources(cfg):
    ctx=SimpleNamespace(config=cfg,requests=RequestTracker(),jr=SimpleNamespace(accepting=True,active=set()),
        ocr=SimpleNamespace(close=AsyncMock()),accounts=SimpleNamespace(close=AsyncMock()),
        database=SimpleNamespace(close=AsyncMock()))
    bot=SimpleNamespace(stop_polling=AsyncMock(),close=AsyncMock())
    life=Lifecycle(ctx,bot,SimpleNamespace())
    task=life.registry.start(asyncio.Event().wait(),'fixture-task')
    await life.close(); await life.close()
    assert task.cancelled() and not ctx.jr.accepting
    for resource in [ctx.ocr,ctx.accounts,ctx.database,bot]: resource.close.assert_awaited_once()


async def test_partial_bot_initialize_cleanup(cfg):
    application=SimpleNamespace(initialize=AsyncMock(side_effect=RuntimeError('fixture')),
        shutdown=AsyncMock(),running=False,updater=SimpleNamespace(running=False,shutdown=AsyncMock()),
        bot=SimpleNamespace(shutdown=AsyncMock()))
    wrapper=BotApplication(application,cfg)
    with pytest.raises(RuntimeError): await wrapper.start()
    await wrapper.close()
    application.shutdown.assert_awaited_once()
    application.bot.shutdown.assert_awaited_once()


async def test_background_failure_triggers_shutdown():
    event=asyncio.Event(); registry=TaskRegistry(event)
    async def fail(): raise RuntimeError('fixture')
    registry.start(fail(),'fixture-failure')
    await asyncio.wait_for(event.wait(),1)
    await registry.close()


async def test_shutdown_cancels_tracked_command_after_grace_period(cfg):
    from dataclasses import replace
    cfg=replace(cfg,shutdown_timeout=0.01)
    tracker=RequestTracker();started=asyncio.Event()
    async def command():
        async with tracker.track():
            started.set();await asyncio.Event().wait()
    task=asyncio.create_task(command());await started.wait()
    ctx=SimpleNamespace(config=cfg,requests=tracker,jr=SimpleNamespace(accepting=True,active=set()),
        ocr=SimpleNamespace(close=AsyncMock()),accounts=SimpleNamespace(close=AsyncMock()),
        database=SimpleNamespace(close=AsyncMock()))
    life=Lifecycle(ctx,SimpleNamespace(stop_polling=AsyncMock(),close=AsyncMock()),None)
    await life.close()
    assert task.cancelled() and not tracker.active and not tracker.accepting
