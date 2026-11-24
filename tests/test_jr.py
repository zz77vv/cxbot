import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import KeyboardButtonCallback

from bot.balance import BalanceResult
from bot.config import Config
from bot.db import Database
from bot.jr import JRService, ManualVerifyManager
from bot.ocr import OCRError
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
        max_concurrency=1,  # 设为 1 专门验证人工验证不阻塞全局 RPC 信号量
        reply_timeout=5.0,
        export_timeout=5.0,
    )


def _msg(mid, text="", *, buttons=None, media=None, document=None, file_name=None, out=False, edit_date=None):
    file_obj = SimpleNamespace(name=file_name) if file_name else None
    m = SimpleNamespace(
        id=mid,
        text=text,
        buttons=buttons,
        media=media,
        document=document,
        file=file_obj,
        out=out,
        edit_date=edit_date,
        click=AsyncMock(return_value=SimpleNamespace(message="")),
    )
    return m


def _btn(text):
    raw = KeyboardButtonCallback(text=text, data=text.encode())
    return SimpleNamespace(text=text, button=raw)


async def test_jr_priority_export_marks_member_and_caches(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(1, "query", out=True)),
            get_messages=AsyncMock(),
            download_media=AsyncMock(),
        )
        result_msg = _msg(2, "🔍共10条信息\n数据1", buttons=[[_btn("导出数据")]])
        export_doc = _msg(3, "", document=object(), file_name="export.txt")

        async def _download(m, file):
            Path(file).write_text("full exported content", encoding="utf-8")
            return file

        client.download_media.side_effect = _download
        client.get_messages.side_effect = [
            [result_msg],  # wait_reply after submit
            [result_msg],  # baseline before click export
            [export_doc],  # wait_reply after click export
        ]

        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "10001.session", client, AccountState.ONLINE)
        balances = SimpleNamespace(
            query=AsyncMock(return_value=BalanceResult(ok=True, value=50)),
            invalidate=lambda *a, **kw: None,
        )
        ocr = SimpleNamespace(recognize_captcha=AsyncMock())
        manual = ManualVerifyManager(cfg)
        jr = JRService(cfg, db, pool, balances, ocr, manual)

        res = await jr.query("test_member")
        assert res.ok and res.kind == "file" and res.file_name == "export.txt"
        assert (await db.get_account_meta("10001"))["jr_membership"] == "member"

        # 第二次命中缓存，不触发新的 send_message
        res2 = await jr.query("test_member")
        assert res2.ok and res2.from_cache
        assert client.send_message.await_count == 1
    finally:
        await db.close()


async def test_jr_free_export_falls_back_to_pagination(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_msg(1, "q", out=True)),
            get_messages=AsyncMock(),
        )
        page1 = _msg(2, "🔍共2条信息 (1/2)\n第一条", buttons=[[_btn("导出数据"), _btn("下一页")]])
        page1.click = AsyncMock(side_effect=[
            SimpleNamespace(message="免费用户暂不支持导出"),  # 点击导出返回免费提示
            SimpleNamespace(message=""),  # 点击下一页
        ])
        page2 = _msg(2, "🔍共2条信息 (2/2)\n第二条", edit_date="t2")

        async def _get_msgs(bot, **kwargs):
            if "ids" in kwargs:
                return page2
            return [page1]

        client.get_messages.side_effect = _get_msgs
        pool = UserbotPool(cfg, db)
        pool.accounts["10002"] = Account("10002", tmp_path / "10002.session", client, AccountState.ONLINE)
        balances = SimpleNamespace(
            query=AsyncMock(return_value=BalanceResult(ok=True, value=20)),
            invalidate=lambda *a, **kw: None,
        )
        jr = JRService(cfg, db, pool, balances, SimpleNamespace(), ManualVerifyManager(cfg))

        res = await jr.query("test_free_pages")
        assert res.ok and res.kind == "file"
        content = Path(res.file_path).read_text(encoding="utf-8")
        assert "第一条" in content and "第二条" in content
        assert (await db.get_account_meta("10002"))["jr_membership"] == "free"
    finally:
        await db.close()


async def test_jr_manual_verify_fallback_does_not_block_global_rpc_semaphore(tmp_path):
    cfg = _make_cfg(tmp_path)  # max_concurrency = 1
    db = await Database(cfg.database).open()
    try:
        verify_msg = _msg(2, "安全验证，请点击下图", buttons=[[_btn("甲"), _btn("乙")]], media=object())
        verify_msg.click = AsyncMock(return_value=SimpleNamespace(message="验证通过"))
        final_msg = _msg(4, "🔍共1条信息\n验证后结果")

        client = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(side_effect=[_msg(1, "q", out=True), _msg(3, "q", out=True)]),
            get_messages=AsyncMock(side_effect=[[verify_msg], [final_msg]]),
            download_media=AsyncMock(return_value=b"fake-img"),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10003"] = Account("10003", tmp_path / "10003.session", client, AccountState.ONLINE)
        balances = SimpleNamespace(
            query=AsyncMock(return_value=BalanceResult(ok=True, value=30)),
            invalidate=lambda *a, **kw: None,
        )
        ocr = SimpleNamespace(recognize_captcha=AsyncMock(side_effect=OCRError("LowConfidence")))

        admin_waiting = asyncio.Event()
        admin_release = asyncio.Event()
        manual = ManualVerifyManager(cfg)

        async def _fake_ask(phone, image_bytes, labels, timeout=120.0):
            admin_waiting.set()
            await admin_release.wait()
            return 1  # 选择"乙"

        manual.ask_admin = _fake_ask
        jr = JRService(cfg, db, pool, balances, ocr, manual)

        task = asyncio.create_task(jr.query("need_verify"))
        await asyncio.wait_for(admin_waiting.wait(), 3.0)

        # 关键验证：即使 max_concurrency=1，人工验证等待期间全局 rpc_sem 必须是空闲的！
        assert not pool.rpc_sem.locked()

        admin_release.set()
        res = await asyncio.wait_for(task, 5.0)
        assert res.ok and res.kind == "text" and "验证后结果" in res.text
    finally:
        await db.close()
