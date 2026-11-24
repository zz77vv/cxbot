"""核武器 (@hewuqi) 查询服务：严格串行、盒能不足自动换号、支持文本与 TXT 导出。"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from telethon.errors import FloodWaitError
from telethon.tl.types import KeyboardButtonCallback

from .config import Config
from .targets import HW
from .userbots import Account, AccountState, AccountUnavailable, UserbotPool, wait_reply

log = logging.getLogger(__name__)

RESULT_WAIT = 90.0
EXPORT_WAIT = 120.0
CHARGE_LINE_RE = re.compile(r"消耗\s*[0-9]+\s*盒能")
INSUFFICIENT_RE = re.compile(r"^\s*(?:[❌⚠️💰💲]\s*)*(?:盒能|余额|积分)不足(?:[\s，。！!：:]|$)")
STATUS_MARKERS = ("数据查询中", "快捷查询完成", "安全验证", "请先验证", "请输入验证码")

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class HWResult:
    ok: bool
    kind: str = "text"  # text | file | error
    text: str = ""
    file_path: str | None = None
    file_name: str | None = None
    reason: str = ""


def find_hw_export_button(msg) -> tuple[int, int] | None:
    matches: list[tuple[int, int]] = []
    for r, row in enumerate(getattr(msg, "buttons", None) or []):
        for c, btn in enumerate(row):
            raw = getattr(btn, "button", btn)
            label = (getattr(raw, "text", "") or "").strip().lstrip("📥📤⬇️ ").strip()
            if (
                isinstance(raw, KeyboardButtonCallback)
                and not getattr(raw, "requires_password", False)
                and label in {"导出", "导出数据"}
            ):
                matches.append((r, c))
    return matches[0] if len(matches) == 1 else None


def extract_hw_text(msg) -> str:
    text = getattr(msg, "text", "") or ""
    if not text.strip() or getattr(msg, "media", None):
        return ""
    first = text.lstrip().splitlines()[0]
    if any(m in first for m in STATUS_MARKERS):
        return ""
    if ("查询结果" in first and "\n" in text) or CHARGE_LINE_RE.search(text):
        return text
    return ""


def is_hw_insufficient(msg) -> bool:
    return bool(INSUFFICIENT_RE.match(getattr(msg, "text", "") or ""))


def _reply_to_id(msg) -> int | None:
    rid = getattr(msg, "reply_to_msg_id", None)
    if rid is not None:
        return rid
    return getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)


class HWService:
    def __init__(self, cfg: Config, pool: UserbotPool):
        self.cfg = cfg
        self.pool = pool
        self.lock = asyncio.Lock()

    async def _download_txt(self, client, message) -> tuple[Path, str]:
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        out_dir = self.cfg.cache_dir / uuid.uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=False)
        target = out_dir / "hw_result.txt"
        raw_name = (getattr(getattr(message, "file", None), "name", "") or "").strip()
        raw_name = re.split(r"[\\/]", raw_name)[-1].strip() or f"hw_{message.id}.txt"
        downloaded = await asyncio.wait_for(
            client.download_media(message, file=str(target)), self.cfg.export_timeout
        )
        actual = Path(downloaded or target).resolve()
        if actual != target.resolve() or not actual.is_file():
            raise FileNotFoundError("HWQ 导出文件下载失败")
        return actual, raw_name

    async def _query_with_account(
        self, account: Account, text: str, on_progress: ProgressCallback | None
    ) -> tuple[HWResult | None, bool]:
        """返回 (HWResult|None, should_try_next_account)。"""
        try:
            async with self.pool.use(account, wait=False) as client:
                if on_progress:
                    await on_progress(f"📤 正在查询核武器（尾号 {account.phone[-4:]}）…")
                sent = await asyncio.wait_for(
                    client.send_message(HW.username, text), self.cfg.reply_timeout
                )
                sender = getattr(getattr(sent, "peer_id", None), "user_id", None)

                def _candidate(m) -> bool:
                    if sender is not None and getattr(m, "sender_id", None) != sender:
                        return False
                    rid = _reply_to_id(m)
                    if rid is not None and rid != sent.id:
                        return False
                    headline = (getattr(m, "text", "") or "").lstrip().partition("\n")[0]
                    if any(mk in headline for mk in STATUS_MARKERS):
                        return False
                    if find_hw_export_button(m) is not None or is_hw_insufficient(m):
                        return True
                    return bool(extract_hw_text(m))

                reply = await wait_reply(
                    client,
                    HW.username,
                    sent.id,
                    _candidate,
                    RESULT_WAIT,
                    rpc_timeout=self.cfg.reply_timeout,
                )
                if reply is None:
                    return HWResult(ok=False, kind="error", reason="等待核武器回复超时。"), False

                btn = find_hw_export_button(reply)
                if btn is None and not extract_hw_text(reply) and is_hw_insufficient(reply):
                    return None, True

                if btn is not None:
                    if on_progress:
                        await on_progress("📥 正在导出核武器 TXT 结果…")
                    history = await asyncio.wait_for(
                        client.get_messages(HW.username, limit=1), self.cfg.reply_timeout
                    )
                    baseline = max([reply.id, *(m.id for m in history)])
                    try:
                        await asyncio.wait_for(reply.click(*btn), self.cfg.reply_timeout)
                    except TimeoutError:
                        pass

                    def _is_txt(m) -> bool:
                        if sender is not None and getattr(m, "sender_id", None) != sender:
                            return False
                        rid = _reply_to_id(m)
                        if rid is not None and rid not in (sent.id, reply.id):
                            return False
                        fname = (getattr(getattr(m, "file", None), "name", "") or "").lower()
                        return bool(getattr(m, "document", None) and fname.endswith(".txt"))

                    file_msg = await wait_reply(
                        client,
                        HW.username,
                        baseline,
                        _is_txt,
                        EXPORT_WAIT,
                        rpc_timeout=self.cfg.reply_timeout,
                    )
                    if file_msg is None:
                        return HWResult(ok=False, kind="error", reason="等待核武器导出 TXT 超时。"), False
                    path, name = await self._download_txt(client, file_msg)
                    return HWResult(ok=True, kind="file", file_path=str(path), file_name=name), False

                return HWResult(ok=True, kind="text", text=extract_hw_text(reply)), False
        except AccountUnavailable:
            return None, True
        except FloodWaitError as exc:
            return HWResult(ok=False, kind="error", reason=f"触发电报限流({exc.seconds}s)。"), False
        except Exception as exc:
            log.warning("HWQ 查询异常 phone=%s", account.phone, exc_info=exc)
            return HWResult(ok=False, kind="error", reason=f"查询异常({type(exc).__name__})。"), False

    async def query(self, text: str, *, on_progress: ProgressCallback | None = None) -> HWResult:
        text = (text or "").strip()
        if not text:
            return HWResult(ok=False, kind="error", reason="查询内容不能为空。")
        if self.lock.locked():
            return HWResult(ok=False, kind="error", reason="已有核武器查询进行中，请稍候。")

        async with self.lock:
            online = [
                acc for acc in self.pool.accounts.values()
                if acc.state == AccountState.ONLINE and not acc.lock.locked()
            ]
            if not online:
                return HWResult(ok=False, kind="error", reason="当前没有在线空闲的协议号。")

            insufficient_count = 0
            for acc in online:
                res, try_next = await self._query_with_account(acc, text, on_progress)
                if res is not None:
                    return res
                if try_next:
                    insufficient_count += 1
                    if on_progress:
                        await on_progress(f"⚠️ 尾号 {acc.phone[-4:]} 盒能不足或暂忙，自动尝试下一个协议号…")
            if insufficient_count:
                return HWResult(ok=False, kind="error", reason="所有可用协议号盒能不足或暂不可用。")
            return HWResult(ok=False, kind="error", reason="未能找到可用协议号执行核武器查询。")
