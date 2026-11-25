"""通用轻量工具：跨平台单实例进程锁、引用计数键锁、UTF-16 文本分段、进度编辑节流。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from telegram.error import BadRequest, RetryAfter, TelegramError

log = logging.getLogger(__name__)


class ProcessLock:
    """跨平台单实例文件锁，防止手误双开损坏 Telethon Session 与 SQLite。"""

    def __init__(self, target_path: Path):
        self.path = Path(f"{target_path}.instance.lock")
        self._fp = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self._fp.write(b"0")
            self._fp.flush()
        self._fp.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fp.close()
            self._fp = None
            raise RuntimeError("已有另一个 cxbot 实例正在运行，禁止双开！") from None

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            finally:
                self._fp = None


class KeyedLocks:
    """引用计数的按 Key 互斥锁，无协程持有时自动清理，杜绝内存泄漏。"""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @property
    def active_keys(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, key: str):
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._refs[key] = self._refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._refs[key] -= 1
            if self._refs[key] <= 0:
                self._refs.pop(key, None)
                self._locks.pop(key, None)


def utf16_len(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def split_text(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    cur = ""
    cur_len = 0
    for line in (text or "").splitlines(keepends=True):
        size = utf16_len(line)
        if cur and cur_len + size > limit:
            chunks.append(cur)
            cur, cur_len = "", 0
        if size <= limit:
            cur += line
            cur_len += size
        else:
            for ch in line:
                csize = utf16_len(ch)
                if cur_len + csize > limit:
                    chunks.append(cur)
                    cur, cur_len = "", 0
                cur += ch
                cur_len += csize
    if cur:
        chunks.append(cur)
    return chunks or [""]


class ProgressEditor:
    """单条进度消息的节流编辑器，避免多页翻页触发 Bot API 429 限流。"""

    def __init__(self, message, *, min_interval: float = 1.2):
        self.message = message
        self.min_interval = min_interval
        self._last_text = getattr(message, "text", "") or ""
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def update(self, text: str, *, force: bool = False) -> None:
        async with self._lock:
            if text == self._last_text:
                return
            now = time.monotonic()
            if not force and (now - self._last_edit) < self.min_interval:
                return
            try:
                await self.message.edit_text(text)
                self._last_text = text
                self._last_edit = time.monotonic()
            except RetryAfter as exc:
                self._last_edit = time.monotonic() + float(getattr(exc, "retry_after", 3))
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    log.warning("编辑进度消息失败", exc_info=exc)
            except TelegramError as exc:
                log.warning("编辑进度消息失败", exc_info=exc)
