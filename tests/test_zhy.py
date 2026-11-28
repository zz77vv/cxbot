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
from bot.zhy import (
    ZHYResult,
    ZHYService,
    classify_zhy_result,
    extract_zhy_conclusion,
    format_3ys_batch_summary,
    parse_3ys_args,
    parse_3ys_batch,
)


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
        balances._cache[("10001", ZHONGHY.key)] = (3, 10**12)

        svc = ZHYService(cfg, db, pool, balances)
        res = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")

        assert res.ok and res.text == final_text
        menu.click.assert_awaited_once_with(1, 1)
        assert client.send_message.await_args_list[0].args == (ZHONGHY.username, "/start")
        assert client.send_message.await_args_list[1].args == (
            ZHONGHY.username,
            "张三 532929200803051914 15356921055",
        )
        # 菜单识别 3 金币后扣 1，应回写为 2
        assert balances.peek_cached("10001", ZHONGHY.key) == 2
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
        svc = ZHYService(cfg, db, pool, balances)

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


def test_extract_zhy_conclusion_and_classify():
    raw = (
        "name: 吉娜\n"
        "idcard: 511002200404276821\n"
        "phone: 13678226436\n\n"
        "🔴 运营商三要素核验不一致\n\n"
        "🟢 12306信息核验一致"
    )
    assert extract_zhy_conclusion(raw) == "🔴 运营商三要素核验不一致 ｜ 🟢 12306信息核验一致"
    assert classify_zhy_result(ZHYResult(ok=True, text=raw)) == "mismatch"
    assert classify_zhy_result(ZHYResult(ok=True, text="name: A\n🟢 运营商三要素核验一致")) == "match"
    assert classify_zhy_result(ZHYResult(ok=True, text="name: B\n🟡 核验出错")) == "error"
    assert classify_zhy_result(ZHYResult(ok=True, text="运营商三要素核验一致")) == "match"
    assert classify_zhy_result(ZHYResult(ok=True, text="运营商三要素核验不一致\n12306信息核验一致")) == "mismatch"
    assert classify_zhy_result(ZHYResult(ok=True, text="核验出错")) == "error"
    assert classify_zhy_result(ZHYResult(ok=False, reason="超时")) == "error"


def test_format_3ys_batch_summary():
    # 故意乱序传入，验证按 一致 -> 不一致 -> 出错/失败 分组排序
    entries = [
        (
            ("陈丕福", "432421196709289810", "13927381466"),
            ZHYResult(ok=True, text="🟡 核验出错"),
        ),
        (
            ("陈洋", "210181197106288335", "15998890119"),
            ZHYResult(ok=True, text="name: 陈洋\n\n🔴 运营商三要素核验不一致"),
        ),
        (
            ("杨璐", "510202198001081886", "15683602834"),
            ZHYResult(ok=True, text="name: 杨璐\nidcard: 510202198001081886\nphone: 15683602834\n\n🟢 运营商三要素核验一致", from_cache=True),
        ),
    ]
    text = format_3ys_batch_summary(entries, total=4, stopped_reason="金币不足")
    assert "🟢 一致：1 ｜ 🔴 不一致：1 ｜ 🟡 出错/失败：1" in text
    assert "提前暂停（未执行 1 条）：金币不足" in text
    assert "1. 杨璐  510202198001081886  15683602834\n   └ 🟢 运营商三要素核验一致（缓存）" in text
    assert "2. 陈洋  210181197106288335  15998890119\n   └ 🔴 运营商三要素核验不一致" in text
    assert "3. 陈丕福  432421196709289810  13927381466\n   └ 🟡 核验出错" in text
    assert "name:" not in text


async def test_zhy_batch_concurrent_execution(tmp_path):
    import asyncio
    import time

    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        pool = UserbotPool(cfg, db)
        for i in range(3):
            phone = f"1000{i + 1}"
            menu = _msg(2, "您的金币 💲：5", buttons=[[_btn("旧三要素核验")]])
            prompt = _msg(2, "🔐 旧三要素核验\n发送: 姓名 身份证号 手机号", edit_date="t1")

            async def _delayed_send(target, text, _i=i):
                if text == "/start":
                    return _msg(1, text, out=True)
                await asyncio.sleep(0.15)
                return _msg(10 + _i, text, out=True)

            res_msg = _msg(20 + i, f"name: 用户{i}\n\n🟢 运营商三要素核验一致")
            client = SimpleNamespace(
                is_connected=lambda: True,
                send_message=AsyncMock(side_effect=_delayed_send),
                get_messages=AsyncMock(side_effect=[[menu], prompt, [res_msg]]),
            )
            pool.accounts[phone] = Account(phone, tmp_path / f"{i}.session", client, AccountState.ONLINE)

        svc = ZHYService(cfg, db, pool, BalanceService(pool, cache_ttl=60))
        items = [
            ("用户0", "532929200803051914", "15356921055"),
            ("用户1", "510202198001081886", "15683602834"),
            ("用户2", "512223196903081978", "19132696215"),
        ]
        t0 = time.monotonic()
        entries, stopped = await svc.verify_old_3ys_batch(items)
        elapsed = time.monotonic() - t0

        assert stopped == ""
        assert len(entries) == 3
        assert all(r.ok for _, r in entries)
        assert [e[0][0] for e in entries] == ["用户0", "用户1", "用户2"]
        assert elapsed < 0.35
    finally:
        await db.close()


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
        svc = ZHYService(cfg, db, pool, BalanceService(pool, cache_ttl=60))

        res = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")
        assert not res.ok and res.fatal_batch
    finally:
        await db.close()


async def test_zhy_balance_reaches_zero_auto_cools_down(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        menu = _msg(2, "您的金币 💲：1", buttons=[[_btn("旧三要素核验")]])
        prompt = _msg(2, "🔐 旧三要素核验\n发送: 姓名 身份证号 手机号", edit_date="t1")
        res_msg = _msg(4, "name: 张三\n\n🟢 运营商三要素核验一致")
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=[_msg(1, "/start", out=True), _msg(3, "q", out=True)]),
            get_messages=AsyncMock(side_effect=[[menu], prompt, [res_msg]]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        balances = BalanceService(pool, cache_ttl=60)
        svc = ZHYService(cfg, db, pool, balances)

        res = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")
        assert res.ok
        assert balances.peek_cached("10001", ZHONGHY.key) == 0
        assert svc._is_in_insufficient_cooldown("10001")
    finally:
        await db.close()


async def test_zhy_cache_hit_and_skip_error_caching(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        menu1 = _msg(2, "您的金币 💲：5", buttons=[[_btn("旧三要素核验")]])
        prompt1 = _msg(2, "🔐 旧三要素核验\n发送: 姓名 身份证号 手机号", edit_date="t1")
        ok_msg = _msg(4, "name: 张三\n\n🟢 运营商三要素核验一致")
        menu2 = _msg(6, "您的金币 💲：4", buttons=[[_btn("旧三要素核验")]])
        prompt2 = _msg(6, "🔐 旧三要素核验\n发送: 姓名 身份证号 手机号", edit_date="t2")
        err_msg = _msg(8, "name: 李四\n\n🟡 核验出错")
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(
                side_effect=[
                    _msg(1, "/start", out=True),
                    _msg(3, "q1", out=True),
                    _msg(5, "/start", out=True),
                    _msg(7, "q2", out=True),
                ]
            ),
            get_messages=AsyncMock(side_effect=[[menu1], prompt1, [ok_msg], [menu2], prompt2, [err_msg]]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        svc = ZHYService(cfg, db, pool, BalanceService(pool, cache_ttl=60))

        # 第一次查询一致 -> 写入缓存；第二次查同一人直接命中缓存（不再发消息）
        r1 = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")
        r2 = await svc.verify_old_3ys("张三", "532929200803051914", "15356921055")
        assert r1.ok and not r1.from_cache
        assert r2.ok and r2.from_cache
        assert client.send_message.await_count == 2

        # 核验出错结果不得写入缓存
        r3 = await svc.verify_old_3ys("李四", "130503670401001", "13800138000")
        assert r3.ok and not r3.from_cache
        assert await svc._get_cached_3ys("李四", "130503670401001", "13800138000") is None
    finally:
        await db.close()


async def test_zhy_batch_cancel_preserves_collected_results(tmp_path):
    import asyncio

    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        menu = _msg(2, "您的金币 💲：5", buttons=[[_btn("旧三要素核验")]])
        prompt = _msg(2, "🔐 旧三要素核验\n发送: 姓名 身份证号 手机号", edit_date="t1")
        first_done = asyncio.Event()

        async def _send(target, text):
            if "15356921055" in text:
                return _msg(3, text, out=True)
            if "15683602834" in text:
                first_done.set()
                await asyncio.sleep(30)
            return _msg(1, text, out=True)

        ok_msg = _msg(4, "name: 用户0\n\n🟢 运营商三要素核验一致")
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=_send),
            get_messages=AsyncMock(side_effect=[[menu], prompt, [ok_msg], [menu], prompt]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        svc = ZHYService(cfg, db, pool, BalanceService(pool, cache_ttl=60))

        items = [
            ("用户0", "532929200803051914", "15356921055"),
            ("用户1", "510202198001081886", "15683602834"),
        ]
        collected: dict[int, ZHYResult] = {}
        task = asyncio.create_task(svc.verify_old_3ys_batch(items, collected=collected))
        await asyncio.wait_for(first_done.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert 0 in collected and collected[0].ok
    finally:
        await db.close()
