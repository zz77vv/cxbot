from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.errors import SessionPasswordNeededError

from bot.balance import BalanceService
from bot.checkin import CheckinService
from bot.config import Config
from bot.db import Database
from bot.login import LoginService
from bot.targets import JR, ZHONGHY
from bot.userbots import Account, AccountState, UserbotPool


def _make_cfg(tmp_path: Path) -> Config:
    return Config(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        admin_id=100,
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        database=tmp_path / "bot.db",
        sessions_dir=tmp_path / "sessions",
        log_file=tmp_path / "bot.log",
        cache_dir=tmp_path / "cache",
        balance_cache_ttl=60,
        reply_timeout=5.0,
    )


async def test_balance_short_cache_and_invalidation(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=SimpleNamespace(id=1, out=True)),
            get_messages=AsyncMock(return_value=[SimpleNamespace(id=2, text="当前积分余额：88", out=False)]),
        )
        pool = UserbotPool(cfg, db)
        acc = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        pool.accounts["10001"] = acc

        bal = BalanceService(pool, cache_ttl=60)
        r1 = await bal.query(acc, JR, use_cache=True)
        r2 = await bal.query(acc, JR, use_cache=True)
        assert r1.ok and r1.value == 88
        assert r2.ok and r2.value == 88 and r2.reason == "cached"
        assert client.send_message.await_count == 1

        bal.invalidate("10001", JR.key)
        r3 = await bal.query(acc, JR, use_cache=True)
        assert r3.ok and client.send_message.await_count == 2
    finally:
        await db.close()


async def test_checkin_button_chain_and_24h_skip(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        m1 = SimpleNamespace(id=2, text="欢迎", buttons=[[SimpleNamespace(text="获得金币")]], out=False, edit_date=None, click=AsyncMock())
        m2 = SimpleNamespace(id=3, text="选择", buttons=[[SimpleNamespace(text="每日签到")]], out=False, edit_date=None, click=AsyncMock())
        m3 = SimpleNamespace(id=4, text="签到成功！获得 5 金币", buttons=None, out=False, edit_date=None)

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=SimpleNamespace(id=1, out=True)),
            get_messages=AsyncMock(side_effect=[[m1], m1, [m2], m2, [m3]]),
        )
        pool = UserbotPool(cfg, db)
        acc = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        pool.accounts["10001"] = acc

        ck = CheckinService(pool, db)
        r1 = await ck.run_one(acc, ZHONGHY)
        assert r1.status == "success"

        # 24 小时内再次签到应跳过
        r2 = await ck.run_one(acc, ZHONGHY)
        assert r2.status == "skipped"
    finally:
        await db.close()


async def test_login_state_machine_with_2fa(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        fake_client = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash123")),
            sign_in=AsyncMock(side_effect=[SessionPasswordNeededError(None), None]),
            is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=SimpleNamespace(phone="8613800000000")),
            session=SimpleNamespace(dc_id=1, server_address="127.0.0.1", port=443, auth_key=b"k" * 256),
        )
        enrolled_called = False

        pool = UserbotPool(cfg, db)
        pool.create_login_client = lambda: fake_client
        pool.enroll = AsyncMock(return_value=Account("8613800000000", tmp_path / "a.session", None, AccountState.ONLINE))

        def _on_enrolled():
            nonlocal enrolled_called
            enrolled_called = True

        login = LoginService(cfg, pool, on_enrolled=_on_enrolled)
        assert "请输入国际格式手机号" in await login.begin()
        assert "手机号格式不对" in await login.submit("abc")
        assert "验证码已发送" in await login.submit("+8613800000000")
        assert "两步验证" in await login.submit("12345")
        assert "登录成功" in await login.submit("my-2fa-pwd")
        assert enrolled_called
        assert not login.is_active
    finally:
        await db.close()
