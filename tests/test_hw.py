from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import KeyboardButtonCallback

from bot.config import Config
from bot.db import Database
from bot.hw import HWService
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
        reply_timeout=5.0,
        export_timeout=5.0,
    )


def _hw_msg(mid, text="", *, sender_id=777, reply_to=None, buttons=None, document=None, file_name=None, out=False):
    return SimpleNamespace(
        id=mid,
        text=text,
        sender_id=sender_id,
        peer_id=SimpleNamespace(user_id=sender_id),
        reply_to_msg_id=reply_to,
        buttons=buttons,
        document=document,
        media=object() if document else None,
        file=SimpleNamespace(name=file_name) if file_name else None,
        out=out,
        click=AsyncMock(),
    )


async def test_hw_auto_switches_account_on_insufficient_balance(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        # 账号 1：盒能不足
        c1 = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_hw_msg(10, "q", out=True)),
            get_messages=AsyncMock(return_value=[_hw_msg(11, "❌ 盒能不足", reply_to=10)]),
        )
        # 账号 2：查询成功返回文本
        c2 = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_hw_msg(20, "q", out=True)),
            get_messages=AsyncMock(return_value=[_hw_msg(21, "查询结果\n姓名：张三\n消耗 2 盒能", reply_to=20)]),
        )

        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", c1, AccountState.ONLINE)
        pool.accounts["10002"] = Account("10002", tmp_path / "2.session", c2, AccountState.ONLINE)

        hw = HWService(cfg, pool)
        res = await hw.query("张三")
        assert res.ok and res.kind == "text" and "姓名：张三" in res.text
        assert c1.send_message.await_count == 1
        assert c2.send_message.await_count == 1
    finally:
        await db.close()


async def test_hw_export_txt_download(tmp_path):
    cfg = _make_cfg(tmp_path)
    db = await Database(cfg.database).open()
    try:
        btn = SimpleNamespace(button=KeyboardButtonCallback(text="📥 导出数据", data=b"exp"))
        reply = _hw_msg(11, "查询结果", reply_to=10, buttons=[[btn]])
        txt_msg = _hw_msg(12, "", reply_to=11, document=object(), file_name="hw_out.txt")

        async def _download(m, file):
            Path(file).write_text("hw exported lines", encoding="utf-8")
            return file

        c1 = SimpleNamespace(
            is_connected=lambda: True,
            send_message=AsyncMock(return_value=_hw_msg(10, "q", out=True)),
            get_messages=AsyncMock(side_effect=[[reply], [reply], [txt_msg]]),
            download_media=AsyncMock(side_effect=_download),
        )
        pool = UserbotPool(cfg, db)
        pool.accounts["10001"] = Account("10001", tmp_path / "1.session", c1, AccountState.ONLINE)

        hw = HWService(cfg, pool)
        res = await hw.query("李四")
        assert res.ok and res.kind == "file" and res.file_name == "hw_out.txt"
        assert Path(res.file_path).read_text(encoding="utf-8") == "hw exported lines"
    finally:
        await db.close()
