"""中核验 (@zhonghybot) 旧三要素核验单元测试。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import KeyboardButtonCallback

from bot.balance import BalanceService
from bot.config import Config
from bot.db import Database
from bot.targets import ZHONGHY
from bot.userbots import Account, AccountState, UserbotPool
from bot.zhy import ZHYResult, ZHYService, format_3ys_batch_summary, parse_3ys_args, parse_3ys_batch


def _cfg(tmp_path: Path) -> Config:
    return Config(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        admin_id=100,
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        database=tmp_path / "bot.db",
        sessions_dir=tmp_path / "sessions",
        log_file=tmp_path / "bot.log",
        cache_dir=tmp_path / "cache",
        reply_timeout=5.0,
        export_timeout=5.0,
    )


def _btn(text: str):
    return SimpleNamespace(text=text, button=KeyboardButtonCallback(text=text, data=text.encode()))


def _msg(mid: int, text: str = "", *, buttons=None, out=False, edit_date=None):
    return SimpleNamespace(
        id=mid,
        text=text,
        buttons=buttons,
        out=out,
        edit_date=edit_date,
        click=AsyncMock(return_value=SimpleNamespace(message="")),
    )


def test_parse_3ys_args_validation():
    parsed, err = parse_3ys_args("张三 53292920080305191x 15356921055")
    assert err == ""
    assert parsed == ("张三", "53292920080305191X", "15356921055")

    parsed15, err15 = parse_3ys_args("李四 130503670401001 13800138000")
    assert err15 == ""
    assert parsed15 == ("李四", "130503670401001", "13800138000")

    assert parse_3ys_args("张三 532929200803051914")[0] is None
    assert parse_3ys_args("张三 bad_id 15356921055")[0] is None
    assert parse_3ys_args("张三 532929200803051914 12345")[0] is None


async def test_zhy_old_3ys_full_flow_with_menu_edit_and_pending_edit(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        menu = _msg(
            2,
            "欢迎您使用 中的核验机器人\n您的UID: 683298845\n您的金币 💲：3\n您的钻石 💎：8",
            buttons=[[_btn("获得金币"), _btn("充值钻石")], [_btn("二要素核验"), _btn("旧三要素核验")]],
        )
        prompt_edited = _msg(
            2,
            "🔐 旧三要素核验\n消耗: 1 金币/次\n\n发送: 姓名 身份证号 手机号",
            edit_date="2026-11-27T19:00:01",
        )
        pending_msg = _msg(4, "🕒 旧三要素核验中...")
        final_text = "🟢 运营商三要素核验一致\n🟢 12306信息核验一致"
        final_edited = _msg(4, final_text, edit_date="2026-11-27T19:00:05")

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=[_msg(1, "/start", out=True), _msg(3, "q", out=True)]),
            # 1) wait_reply menu -> [menu]
            # 2) wait_reply prompt_msg -> original=menu -> prompt_edited
            # 3) wait_reply first -> [pending_msg]
            # 4) wait_reply final_msg -> original=pending_msg -> final_edited
            get_messages=AsyncMock(side_effect=[[menu], prompt_edited, [pending_msg], final_edited]),
        )

        pool = UserbotPool(cfg, db)
        acc = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        pool.accounts["10001"] = acc

        balances = BalanceService(pool, cache_ttl=60)
        # 预先写入余额缓存，验证核验成功后缓存会被失效
        balances._cache[("10001", ZHONGHY.key)] = (3, 10**12)

        svc = ZHYService(cfg, pool, balances)
        res = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")

        assert res.ok and res.text == final_text
        menu.click.assert_awaited_once_with(1, 1)
        assert client.send_message.await_args_list[0].args == (ZHONGHY.username, "/start")
        assert client.send_message.await_args_list[1].args == (
            ZHONGHY.username,
            "张三 532929200803051914 15356921055",
        )
        assert balances.peek_cached("10001", ZHONGHY.key) is None
    finally:
        await db.close()


async def test_zhy_auto_switches_account_when_coins_insufficient(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        menu1 = _msg(
            2,
            "欢迎您使用 中的核验机器人\n您的金币 🪙：0",
            buttons=[[_btn("旧三要素核验")]],
        )
        c1 = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(1, "/start", out=True)),
            get_messages=AsyncMock(return_value=[menu1]),
        )

        menu2 = _msg(
            12,
            "欢迎您使用 中的核验机器人\n您的金币 💲：5",
            buttons=[[_btn("旧三要素核验")]],
        )
        prompt2 = _msg(12, "🔐 旧三要素核验\n消耗: 1 金币/次\n\n发送: 姓名 身份证号 手机号", edit_date="t1")
        direct_res = _msg(14, "🟠 核验失败 请稍后重试")
        c2 = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=[_msg(11, "/start", out=True), _msg(13, "q", out=True)]),
            get_messages=AsyncMock(side_effect=[[menu2], prompt2, [direct_res]]),
        )

        pool = UserbotPool(cfg, db)
        a1 = Account("10001", tmp_path / "1.session", c1, AccountState.ONLINE)
        a2 = Account("10002", tmp_path / "2.session", c2, AccountState.ONLINE)
        pool.accounts["10001"] = a1
        pool.accounts["10002"] = a2

        balances = BalanceService(pool, cache_ttl=60)
        svc = ZHYService(cfg, pool, balances)

        res = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")
        assert res.ok
        assert res.phone == "10002"
        assert res.text == "🟠 核验失败 请稍后重试"
        assert svc._is_in_insufficient_cooldown("10001")

        # 刷新余额后可解除冷却
        svc.clear_insufficient("10001")
        assert not svc._is_in_insufficient_cooldown("10001")
    finally:
        await db.close()


def test_parse_3ys_batch_dedup_and_error_report():
    items, errs = parse_3ys_batch(
        "张三 53292920080305191x 15356921055\n"
        "\n"
        "张三 53292920080305191X 15356921055\n"
        "李四 130503670401001 13800138000"
    )
    assert errs == []
    assert items == [
        ("张三", "53292920080305191X", "15356921055"),
        ("李四", "130503670401001", "13800138000"),
    ]

    bad_items, bad_errs = parse_3ys_batch(
        "张三 532929200803051914 15356921055\n"
        "李四 bad_id 123"
    )
    assert len(bad_items) == 1
    assert len(bad_errs) == 1 and bad_errs[0].startswith("第 2 行：")


def test_format_3ys_batch_summary():
    entries = [
        (("张三", "532929200803051914", "15356921055"), ZHYResult(ok=True, text="🟢 一致")),
        (("李四", "130503670401001", "13800138000"), ZHYResult(ok=False, reason="金币不足")),
    ]
    text = format_3ys_batch_summary(entries, total=3, stopped_reason="金币不足")
    assert "成功 1，失败 1，未执行 1" in text
    assert "原因：金币不足" in text
    assert "【1】张三 532929200803051914 15356921055\n🟢 一致" in text
    assert "【2】李四 130503670401001 13800138000\n❌ 失败：金币不足" in text


async def test_zhy_fatal_batch_flag_on_all_insufficient(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        menu = _msg(2, "您的金币 💲：0", buttons=[[_btn("旧三要素核验")]])
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(1, "/start", out=True)),
            get_messages=AsyncMock(return_value=[menu]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        svc = ZHYService(cfg, pool, BalanceService(pool, cache_ttl=60))

        res = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")
        assert not res.ok and res.fatal_batch
    finally:
        await db.close()
