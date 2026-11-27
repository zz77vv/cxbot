"""针对 13 项深度优化的专项单元与集成测试。"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw
from telethon.tl.types import KeyboardButtonCallback

from bot.balance import BalanceResult, BalanceService
from bot.checkin import CheckinService
from bot.config import Config
from bot.db import Database
from bot.hw import HWService
from bot.jr import JRService, ManualVerifyManager
from bot.ocr import OCRError, recognize_duplicate_char_sync
from bot.targets import JR
from bot.userbots import Account, AccountState, UserbotPool, wait_reply
from bot.utils import KeyedLocks, ProcessLock, ProgressEditor


def _cfg(tmp_path: Path, **kw) -> Config:
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
        **kw,
    )


def _btn(text: str):
    return SimpleNamespace(text=text, button=KeyboardButtonCallback(text=text, data=text.encode()))


def _msg(mid: int, text: str = "", *, buttons=None, media=None, out=False, edit_date=None, sender_id=777, reply_to=None):
    return SimpleNamespace(
        id=mid,
        text=text,
        buttons=buttons,
        media=media,
        document=None,
        file=None,
        out=out,
        edit_date=edit_date,
        sender_id=sender_id,
        peer_id=SimpleNamespace(user_id=sender_id),
        reply_to_msg_id=reply_to,
        click=AsyncMock(return_value=SimpleNamespace(message="")),
    )


async def test_keyed_locks_cleans_up_entries():
    locks = KeyedLocks()
    async with locks.hold("k1"):
        assert locks.active_keys == 1
    assert locks.active_keys == 0


def test_process_lock_blocks_second_instance(tmp_path):
    db_path = tmp_path / "bot.db"
    l1 = ProcessLock(db_path)
    l2 = ProcessLock(db_path)
    l1.acquire()
    try:
        with pytest.raises(RuntimeError, match="禁止双开"):
            l2.acquire()
    finally:
        l1.close()
    # 释放后可重新获取
    l2.acquire()
    l2.close()


async def test_progress_editor_throttles_and_forces():
    fake_msg = SimpleNamespace(text="init", edit_text=AsyncMock())
    ed = ProgressEditor(fake_msg, min_interval=60.0)
    await ed.update("step 1")
    await ed.update("step 2")  # 被节流忽略
    assert fake_msg.edit_text.await_count == 1
    await ed.update("final", force=True)  # 强制刷新
    assert fake_msg.edit_text.await_count == 2


async def test_wait_reply_retries_transient_connection_error():
    good = _msg(2, "ok")
    client = SimpleNamespace(
        get_messages=AsyncMock(side_effect=[ConnectionError("net blip"), [good]])
    )
    res = await wait_reply(client, "@bot", 1, lambda m: m.text == "ok", timeout=5.0, interval=0.05)
    assert res is good
    assert client.get_messages.await_count == 2


async def test_manual_verify_retries_when_wrong_click_returns_new_captcha(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        cap1 = _msg(2, "安全验证，请点击下图", buttons=[[_btn("甲"), _btn("乙")]], media=object())
        cap2 = _msg(3, "安全验证，请点击下图（新图）", buttons=[[_btn("丙"), _btn("丁")]], media=object())
        cap2.click = AsyncMock(return_value=SimpleNamespace(message="验证通过"))
        final_res = _msg(5, "🔍共1条信息\n最终数据")

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=[_msg(1, "q", out=True), _msg(4, "q", out=True)]),
            # submit后收到cap1；cap1点错后刷新出cap2；cap2通过重提后收到final_res
            get_messages=AsyncMock(side_effect=[[cap1], cap1, [cap2], [final_res]]),
            download_media=AsyncMock(return_value=b"img-bytes"),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        balances = SimpleNamespace(
            query=AsyncMock(return_value=BalanceResult(ok=True, value=50)),
            invalidate=lambda *a, **kw: None,
        )
        ocr = SimpleNamespace(recognize_captcha=AsyncMock(side_effect=OCRError("LowConfidence")))
        manual = ManualVerifyManager(cfg)
        attempts_seen = []

        async def _ask(phone, img, labels, *, attempt=1, max_attempts=3, timeout=120.0):
            attempts_seen.append((attempt, labels))
            return 0  # 第一次点错甲，第二次点对丙

        manual.ask_admin = _ask
        jr = JRService(cfg, db, pool, balances, ocr, manual)
        res = await jr.query("verify_retry_case")
        assert res.ok and res.kind == "text" and "最终数据" in res.text
        assert attempts_seen == [(1, ["甲", "乙"]), (2, ["丙", "丁"])]
    finally:
        await db.close()


async def test_jr_incomplete_pagination_marked_partial_and_not_cached(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        # 标明共 3 页，但点下一页后返回同一页（页码未前进）
        page1 = _msg(2, "🔍共3条信息 (1/3)\n第一页", buttons=[[_btn("下一页")]])
        stuck_page = _msg(3, "🔍共3条信息 (1/3)\n第一页", buttons=[[_btn("下一页")]])

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(1, "q", out=True)),
            get_messages=AsyncMock(side_effect=[[page1], page1, [stuck_page]]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        # 预先设为近期已确认 free，顺便验证跳过导出按钮
        await db.set_jr_membership("10001", "free")
        balances = SimpleNamespace(
            query=AsyncMock(return_value=BalanceResult(ok=True, value=10)),
            invalidate=lambda *a, **kw: None,
        )
        jr = JRService(cfg, db, pool, balances, SimpleNamespace(), ManualVerifyManager(cfg))
        res = await jr.query("stuck_pages")
        assert res.ok and res.kind == "file" and res.partial is True
        assert "部分结果" in Path(res.file_path).read_text(encoding="utf-8")
        # 确认未写入完整缓存
        from bot.db import make_cache_key
        assert await db.get_cache(make_cache_key("jr", "stuck_pages")) is None
    finally:
        await db.close()


async def test_jr_skips_export_click_for_known_free_account(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        await db.set_jr_membership("10001", "free")
        single_page = _msg(2, "🔍共1条信息\n单页内容", buttons=[[_btn("导出数据")]])
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(1, "q", out=True)),
            get_messages=AsyncMock(return_value=[single_page]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        balances = SimpleNamespace(
            query=AsyncMock(return_value=BalanceResult(ok=True, value=10)),
            invalidate=lambda *a, **kw: None,
        )
        jr = JRService(cfg, db, pool, balances, SimpleNamespace(), ManualVerifyManager(cfg))
        res = await jr.query("known_free")
        assert res.ok and res.kind == "text" and "单页内容" in res.text
        # 关键断言：已知 free 号未点击导出按钮！
        assert single_page.click.await_count == 0
    finally:
        await db.close()


async def test_hw_remembers_insufficient_balance_and_clears_on_balance_update(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        c1 = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(10, "q", out=True)),
            get_messages=AsyncMock(return_value=[_msg(11, "❌ 盒能不足", reply_to=10)]),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", c1, AccountState.ONLINE)

        hw = HWService(cfg, pool)
        r1 = await hw.query("item1")
        assert not r1.ok and r1.fatal_batch
        assert c1.send_message.await_count == 1

        # 第二次直接跳过，不再骚扰 @hewuqi
        r2 = await hw.query("item2")
        assert not r2.ok and r2.fatal_batch and "15 分钟" in r2.reason
        assert c1.send_message.await_count == 1

        # 解除冷却后可再次尝试
        hw.clear_insufficient("10001")
        await hw.query("item3")
        assert c1.send_message.await_count == 2
    finally:
        await db.close()


async def test_checkin_concurrent_calls_do_not_double_checkin(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        async def _slow_send(*a, **kw):
            await asyncio.sleep(0.05)
            return _msg(1, "/checkin", out=True)

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=_slow_send),
            get_messages=AsyncMock(return_value=[_msg(2, "签到成功")]),
        )
        pool = UserbotPool(cfg, db)
        acc = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        pool.accounts["10001"] = acc

        ck = CheckinService(pool, db)
        r1, r2 = await asyncio.gather(ck.run_one(acc, JR), ck.run_one(acc, JR))
        statuses = {r1.status, r2.status}
        assert statuses == {"success", "skipped"}
        assert client.send_message.await_count == 1
    finally:
        await db.close()


async def test_balance_double_checked_cache_prevents_stampede(tmp_path):
    cfg = _cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        async def _slow_send(*a, **kw):
            await asyncio.sleep(0.05)
            return _msg(1, "/balance", out=True)

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=_slow_send),
            get_messages=AsyncMock(return_value=[_msg(2, "积分余额：66")]),
        )
        pool = UserbotPool(cfg, db)
        acc = Account("10001", tmp_path / "1.session", client, AccountState.ONLINE)
        pool.accounts["10001"] = acc

        bal = BalanceService(pool, cache_ttl=60)
        r1, r2 = await asyncio.gather(
            bal.query(acc, JR, use_cache=True),
            bal.query(acc, JR, use_cache=True),
        )
        assert r1.ok and r2.ok and r1.value == 66 and r2.value == 66
        assert client.send_message.await_count == 1
    finally:
        await db.close()


def test_ocr_adaptive_threshold_handles_lighter_glyphs():
    # 灰度 125（高于默认阈值 115，但低于自适应备选阈值 140）
    img = Image.new("RGB", (320, 120), "white")
    draw = ImageDraw.Draw(img)
    for i in range(4):
        x0 = 20 + i * 70
        draw.rectangle((x0, 55, x0 + 30, 100), fill=(125, 125, 125))
    buf = BytesIO()
    img.save(buf, format="PNG")

    readings = iter([
        ("字", 0.9), ("字", 0.9),
        ("文", 0.9), ("文", 0.9),
        ("字", 0.8), ("字", 0.85),
        ("天", 0.9), ("天", 0.9),
    ])

    class _Eng:
        def set_ranges(self, cs):
            pass

        def classification(self, b, probability=True, png_fix=True):
            t, c = next(readings)
            return {"text": t, "confidence": c}

    idx, ch, conf = recognize_duplicate_char_sync(buf.getvalue(), ["文", "字", "天", "地"], _Eng())
    assert idx == 1 and ch == "字" and conf == pytest.approx(0.8)


async def test_manual_verify_cancel_all(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = ManualVerifyManager(cfg)
    loop = asyncio.get_running_loop()
    fut1 = loop.create_future()
    fut2 = loop.create_future()
    mgr._pending["t1"] = (fut1, 2)
    mgr._pending["t2"] = (fut2, 2)
    assert mgr.cancel_all() == 2
    assert fut1.cancelled() and fut2.cancelled()


async def test_track_and_release_requests(tmp_path):
    from bot.app import BotApp

    app = BotApp(_cfg(tmp_path))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker():
        async with app.track_request("jr", "/jr 测试查询"):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    t = asyncio.create_task(worker())
    await started.wait()

    # exclude_current=True 不会把自身（未在 track_request 中或当前任务）误计入
    async with app.track_request("status", "/status 状态检查"):
        active = app.list_active_requests(exclude_current=True)
        assert len(active) == 1
        assert active[0].kind == "jr"
        assert active[0].label == "/jr 测试查询"

        released = await app.release_requests(exclude_current=True)
        assert len(released) == 1
        assert released[0].label == "/jr 测试查询"

    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    with pytest.raises(asyncio.CancelledError):
        await t
    assert app.list_active_requests(exclude_current=False) == []
    await app.ocr.close()
