import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.sessions import MemorySession, SQLiteSession
from telethon.crypto import AuthKey
from telegram_bot.services.login_service import LoginService
from telegram_bot.clients.session_store import save_authorized_session
from telegram_bot.clients.userbot_manager import UserbotManager
from telegram_bot.storage.repositories.account_repository import AccountRepository
from telegram_bot.handlers.guards import admin_guard
from telegram.ext import ApplicationHandlerStop


@pytest.fixture
def login(cfg):
    client=SimpleNamespace(connect=AsyncMock(),disconnect=AsyncMock(),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash='temporary-hash')),
        sign_in=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(phone='8613800000000')),session=MemorySession())
    manager=SimpleNamespace(exists=Mock(return_value=False),login_client=Mock(return_value=client),
        enroll=AsyncMock(return_value=SimpleNamespace(key='8613800000000')))
    added=Mock()
    service=LoginService(cfg,manager,added)
    return service,client,manager,added


async def start_phone(service,cfg):
    await service.begin(cfg.admin_id,cfg.admin_id,True)
    return await service.submit(cfg.admin_id,cfg.admin_id,True,'+8613800000000')


async def test_login_success(login,cfg):
    service,client,manager,added=login
    assert '验证码' in await start_phone(service,cfg)
    reply=await service.submit(cfg.admin_id,cfg.admin_id,True,'12345')
    assert '登录成功' in reply and not service.attempts
    client.is_user_authorized.assert_awaited_once()
    manager.enroll.assert_awaited_once();added.assert_called_once()
    client.disconnect.assert_awaited_once()


async def test_login_two_factor(login,cfg):
    service,client,manager,added=login
    await start_phone(service,cfg)
    client.sign_in.side_effect=[SessionPasswordNeededError(None),None]
    assert '两步验证' in await service.submit(cfg.admin_id,cfg.admin_id,True,'12345')
    assert '登录成功' in await service.submit(cfg.admin_id,cfg.admin_id,True,'test-only-secret')
    assert not service.attempts


@pytest.mark.parametrize('user,private',[(999,True),(123,False)])
async def test_login_unauthorized(login,cfg,user,private):
    service,client,manager,_=login
    await service.begin(user,user,private)
    await service.submit(user,user,private,'12345')
    assert not service.attempts
    manager.login_client.assert_not_called()


async def test_existing_session_no_overwrite(login,cfg):
    service,client,manager,_=login;manager.exists.return_value=True
    assert '禁止覆盖' in await start_phone(service,cfg)
    client.connect.assert_not_awaited()


@pytest.mark.parametrize('failure',[ValueError('invalid'),FloodWaitError(None,capture=30)])
async def test_login_failure_cleans_state(login,cfg,failure):
    service,client,manager,_=login
    await start_phone(service,cfg);client.sign_in.side_effect=failure
    await service.submit(cfg.admin_id,cfg.admin_id,True,'12345')
    assert not service.attempts
    manager.enroll.assert_not_awaited();client.disconnect.assert_awaited_once()


async def test_authorization_required(login,cfg):
    service,client,manager,_=login
    await start_phone(service,cfg);client.is_user_authorized.return_value=False
    assert '未完成' in await service.submit(cfg.admin_id,cfg.admin_id,True,'12345')
    manager.enroll.assert_not_awaited()


async def test_wrong_identity_is_rejected(login,cfg):
    service,client,manager,_=login
    await start_phone(service,cfg);client.get_me.return_value=SimpleNamespace(phone='other')
    await service.submit(cfg.admin_id,cfg.admin_id,True,'12345')
    manager.enroll.assert_not_awaited()


async def test_timeout_cancel_and_close(login,cfg):
    service,client,_,_=login
    await start_phone(service,cfg)
    service.attempts[cfg.admin_id].expires=time.monotonic()-1
    await service.expire();assert not service.attempts
    await start_phone(service,cfg);await service.cancel(cfg.admin_id,cfg.admin_id,True)
    assert not service.attempts
    await start_phone(service,cfg);await service.close();assert not service.attempts


def test_session_store_real_reload_and_no_clobber(tmp_path):
    session=MemorySession();session.set_dc(2,'149.154.167.51',443);session.auth_key=AuthKey(b'x'*256)
    path=save_authorized_session(tmp_path/'sessions','8613800000000',session)
    loaded=SQLiteSession(str(path))
    assert loaded.auth_key.key==session.auth_key.key and loaded.dc_id==2
    loaded.close();before=path.read_bytes()
    with pytest.raises(FileExistsError):save_authorized_session(path.parent,'8613800000000',session)
    assert path.read_bytes()==before and list((path.parent/'.pending').iterdir())==[]


async def test_manager_enroll_and_restart_load(cfg,db,client):
    session=MemorySession();session.set_dc(2,'149.154.167.51',443);session.auth_key=AuthKey(b'x'*256)
    manager=UserbotManager(cfg,AccountRepository(db),factory=lambda p:client)
    account=await manager.enroll('8613800000000',session)
    assert account.state.value=='online'
    await manager.close()
    restarted=UserbotManager(cfg,AccountRepository(db),factory=lambda p:client)
    assert len(restarted.load())==1
    await restarted.start();assert restarted.accounts['8613800000000'].state.value=='online'
    await restarted.close()


async def test_global_guard_all_messages(cfg):
    update=SimpleNamespace(effective_user=SimpleNamespace(id=999),effective_message=SimpleNamespace(reply_text=AsyncMock()))
    with pytest.raises(ApplicationHandlerStop):await admin_guard(SimpleNamespace(config=cfg))(update,None)


async def test_duplicate_credentials_and_overlapping_begin(login,cfg):
    service,client,manager,_=login
    await start_phone(service,cfg)
    assert '已有登录流程' in await service.begin(cfg.admin_id,cfg.admin_id,True)
    client.sign_in.side_effect=SessionPasswordNeededError(None)
    await service.submit(cfg.admin_id,cfg.admin_id,True,'12345',request_id=10)
    assert await service.submit(cfg.admin_id,cfg.admin_id,True,'12345',request_id=10) is None
    assert client.sign_in.await_count==1
    await service.close()


async def test_timeout_during_rpc(login,cfg):
    service,client,manager,_=login
    await start_phone(service,cfg)
    service.attempts[cfg.admin_id].expires=time.monotonic()+0.01
    async def hang(**kwargs):await asyncio.Event().wait()
    client.sign_in.side_effect=hang
    await service.submit(cfg.admin_id,cfg.admin_id,True,'12345')
    assert not service.attempts
    manager.enroll.assert_not_awaited()


async def test_sensitive_error_not_logged(login,cfg,caplog):
    service,client,manager,_=login
    await start_phone(service,cfg)
    client.sign_in.side_effect=ValueError('test-only-credential')
    reply=await service.submit(cfg.admin_id,cfg.admin_id,True,'test-only-credential')
    assert 'test-only-credential' not in reply+caplog.text
    assert 'phase=code error_type=ValueError' in caplog.text


async def test_phone_stage_diagnostics_without_payload(login,cfg,caplog):
    service,client,manager,_=login
    await service.begin(cfg.admin_id,cfg.admin_id,True)
    reply=await service.submit(cfg.admin_id,cfg.admin_id,True,'not-a-phone-secret')
    assert '手机号格式无效' in reply
    assert 'phase=phone error_type=ValueError' in caplog.text
    assert 'not-a-phone-secret' not in caplog.text+reply


async def test_invalid_api_pair_diagnostic(login,cfg,caplog):
    from telethon.errors import ApiIdInvalidError
    service,client,manager,_=login
    client.send_code_request.side_effect=ApiIdInvalidError(None)
    reply=await start_phone(service,cfg)
    assert '不匹配' in reply
    assert 'phase=phone error_type=ApiIdInvalidError' in caplog.text
    assert not service.attempts


def test_current_targets_and_menu():
    from telegram_bot.target_bots import TARGETS
    from telegram_bot.handlers.registration import COMMANDS
    assert {t.bot for t in TARGETS}=={'@zhonghybot','@jrsgk','@hewuqi'}
    assert set(COMMANDS)=={'start','help','jr','qd','jf','status','login','cancel'}


async def test_login_dc_routing_retry_is_bounded(login,cfg):
    from telethon.errors import PhoneMigrateError
    service,client,manager,_=login
    client.send_code_request.side_effect=[PhoneMigrateError(None,capture=2),SimpleNamespace(phone_code_hash='transient')]
    assert '验证码' in await start_phone(service,cfg)
    assert client.send_code_request.await_count==2
    await service.close()
    client.send_code_request.side_effect=PhoneMigrateError(None,capture=2)
    before=client.send_code_request.await_count
    await start_phone(service,cfg)
    assert client.send_code_request.await_count-before==2 and not service.attempts


async def test_timeout_does_not_repeat_code_request(login,cfg):
    service,client,manager,_=login
    client.send_code_request.side_effect=TimeoutError()
    await start_phone(service,cfg)
    client.send_code_request.assert_awaited_once()


async def test_login_factory_preserves_underlying_rpc_error(cfg,db):
    manager=UserbotManager(cfg,AccountRepository(db))
    client=manager.login_client()
    assert client._raise_last_call_error and client._request_retries==0
    await client.disconnect()
