"""Telethon 协议号池、原子 Session 保存与统一对话轮询。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession, SQLiteSession

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


class AccountState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class AccountUnavailable(RuntimeError):
    """协议号当前不可用（离线、忙碌、冷却中或正在停机）。"""


@dataclass
class Account:
    phone: str
    session_path: Path
    client: Any
    state: AccountState = AccountState.OFFLINE
    flood_until: float = 0.0
    error_type: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def save_authorized_session(sessions_dir: Path, phone: str, session) -> Path:
    """仅保存已授权的 Session，使用独占原子链接防止覆盖已有账号。"""
    if not phone.isascii() or not phone.isdigit():
        raise ValueError("invalid_phone")
    sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = sessions_dir / ".pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    if pending.is_symlink():
        raise ValueError("unsafe_session_dir")

    tmp = pending / f"{uuid.uuid4().hex}.session"
    final = sessions_dir / f"{phone}.session"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        saved = SQLiteSession(str(tmp))
        try:
            saved.set_dc(session.dc_id, session.server_address, session.port)
            saved.auth_key = session.auth_key
            saved.save()
        finally:
            saved.close()
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.link(tmp, final)
        return final
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            tmp.with_name(tmp.name + suffix).unlink(missing_ok=True)


def msg_signature(message: Any) -> tuple:
    return (
        getattr(message, "text", "") or "",
        tuple(
            tuple(getattr(b, "text", "") or "" for b in row)
            for row in (getattr(message, "buttons", None) or [])
        ),
        str(getattr(message, "edit_date", None)),
    )


def find_button(message: Any, keywords: tuple[str, ...]) -> tuple[int, int, str] | None:
    for r, row in enumerate(getattr(message, "buttons", None) or []):
        for c, btn in enumerate(row):
            label = getattr(btn, "text", "") or ""
            if any(k in label for k in keywords):
                return r, c, label
    return None


async def wait_reply(
    client: Any,
    bot_username: str,
    after_id: int,
    predicate: Callable[[Any], bool],
    timeout: float,
    *,
    original: Any = None,
    old_sig: tuple | None = None,
    interval: float = 0.8,
    rpc_timeout: float = 30.0,
) -> Any | None:
    """只读轮询目标机器人回复（同时监听原消息编辑与新消息）。"""
    async def _poll():
        while True:
            if original is not None:
                refreshed = await asyncio.wait_for(
                    client.get_messages(bot_username, ids=original.id), rpc_timeout
                )
                if (
                    refreshed
                    and not getattr(refreshed, "out", False)
                    and msg_signature(refreshed) != old_sig
                    and predicate(refreshed)
                ):
                    return refreshed
            msgs = await asyncio.wait_for(
                client.get_messages(bot_username, limit=10), rpc_timeout
            )
            for m in sorted(msgs, key=lambda x: x.id):
                if m.id > after_id and not getattr(m, "out", False) and predicate(m):
                    return m
            await asyncio.sleep(interval)

    try:
        return await asyncio.wait_for(_poll(), timeout)
    except TimeoutError:
        return None


class UserbotPool:
    """管理所有 Telethon 协议号。

    关键设计：将单账号会话锁 (account.lock) 与全局 RPC 并发信号量 (rpc_sem) 分离：
    - `use(account)` 持有账号锁和信号量，适合常规短时 RPC 交互；
    - `hold_account(account)` 仅持有账号锁，不占全局信号量（用于等待人工验证等长耗时操作），
      需要发起 RPC 时再短时进入 `rpc_slot()`。
    """

    def __init__(self, cfg: Config, db: Database, client_factory=None):
        self.cfg = cfg
        self.db = db
        self._factory = client_factory or self._default_factory
        self.accounts: dict[str, Account] = {}
        self.rpc_sem = asyncio.Semaphore(cfg.max_concurrency)
        self.stopping = False

    def _default_factory(self, session_or_path):
        is_mem = isinstance(session_or_path, MemorySession)
        return TelegramClient(
            session_or_path if is_mem else str(session_or_path),
            self.cfg.api_id,
            self.cfg.api_hash,
            flood_sleep_threshold=0,
            request_retries=1,
            connection_retries=1,
            auto_reconnect=False,
            raise_last_call_error=is_mem,
        )

    def load_sessions(self) -> tuple[Account, ...]:
        if self.accounts:
            return tuple(self.accounts.values())
        if not self.cfg.sessions_dir.is_dir():
            log.info("Session 目录不存在，等待管理员私聊 /login 添加协议号")
            return ()
        for path in sorted(self.cfg.sessions_dir.glob("*.session")):
            if not path.stem.isdigit() or path.is_symlink():
                log.warning("跳过非规范 Session 文件: %s", path.name)
                continue
            try:
                self.accounts[path.stem] = Account(path.stem, path, self._factory(path))
            except Exception as exc:
                log.error("加载 Session 失败: %s", path.stem, exc_info=exc)
                self.accounts[path.stem] = Account(
                    path.stem, path, None, AccountState.ERROR, error_type=type(exc).__name__
                )
        return tuple(self.accounts.values())

    async def start(self) -> None:
        meta = await self.db.all_account_meta()
        self.load_sessions()
        for acc in self.accounts.values():
            acc.flood_until = meta.get(acc.phone, {}).get("flood_until", 0.0)
        await asyncio.gather(*(self.refresh(acc, reconnect=True) for acc in tuple(self.accounts.values())))

    def exists(self, phone: str) -> bool:
        return phone in self.accounts or (self.cfg.sessions_dir / f"{phone}.session").exists()

    def create_login_client(self):
        return self._factory(MemorySession())

    async def enroll(self, phone: str, session) -> Account:
        if self.stopping or self.exists(phone):
            raise AccountUnavailable("existing_or_stopping")
        path = save_authorized_session(self.cfg.sessions_dir, phone, session)
        acc = Account(phone, path, self._factory(path))
        self.accounts[phone] = acc
        await self.refresh(acc, reconnect=True)
        if acc.state != AccountState.ONLINE:
            raise AccountUnavailable("saved_but_reconnect_failed")
        return acc

    async def mark_flood_wait(self, account: Account, seconds: int) -> None:
        until = max(account.flood_until, time.time() + max(1, seconds))
        account.flood_until = until
        account.state = AccountState.UNAVAILABLE
        await self.db.set_flood_wait(account.phone, until)

    def any_flood_active(self) -> bool:
        now = time.time()
        return any(acc.flood_until > now for acc in self.accounts.values())

    async def refresh(self, account: Account, *, reconnect: bool = False) -> AccountState:
        async with account.lock, self.rpc_sem:
            if account.client is None:
                account.state = AccountState.ERROR
                return account.state
            if account.flood_until > time.time():
                account.state = AccountState.UNAVAILABLE
                return account.state
            try:
                if not account.client.is_connected():
                    account.state = AccountState.OFFLINE
                    if not reconnect:
                        return account.state
                    await asyncio.wait_for(account.client.connect(), self.cfg.reply_timeout)
                authorized = await asyncio.wait_for(
                    account.client.is_user_authorized(), self.cfg.reply_timeout
                )
                account.state = AccountState.ONLINE if authorized else AccountState.UNAUTHORIZED
                account.error_type = ""
            except asyncio.CancelledError:
                raise
            except FloodWaitError as exc:
                await self.mark_flood_wait(account, exc.seconds)
            except Exception as exc:
                account.state = AccountState.ERROR
                account.error_type = type(exc).__name__
                log.warning("协议号连接检查失败 phone=%s", account.phone, exc_info=exc)
            return account.state

    @asynccontextmanager
    async def rpc_slot(self):
        async with self.rpc_sem:
            yield

    @asynccontextmanager
    async def hold_account(self, account: Account, *, wait: bool = True):
        """仅持有单账号会话锁（不占用全局 RPC 信号量），适合含人工等待的长流程。"""
        if not wait and account.lock.locked():
            raise AccountUnavailable("account_busy")
        async with account.lock:
            if self.stopping or account.flood_until > time.time() or account.state != AccountState.ONLINE:
                raise AccountUnavailable("account_unavailable")
            if not account.client or not account.client.is_connected():
                account.state = AccountState.OFFLINE
                raise AccountUnavailable("account_disconnected")
            try:
                yield account.client
            except FloodWaitError as exc:
                await self.mark_flood_wait(account, exc.seconds)
                raise
            except (ConnectionError, OSError):
                account.state = AccountState.ERROR
                raise

    @asynccontextmanager
    async def use(self, account: Account, *, wait: bool = True):
        """同时持有账号锁与全局 RPC 信号量，适合常规短时操作。"""
        async with self.hold_account(account, wait=wait) as client:
            async with self.rpc_sem:
                yield client

    async def monitor_loop(self) -> None:
        while not self.stopping:
            await asyncio.sleep(self.cfg.reconnect_interval)
            await asyncio.gather(
                *(self.refresh(acc, reconnect=True) for acc in tuple(self.accounts.values()))
            )

    async def close(self) -> None:
        self.stopping = True

        async def _disconnect(acc: Account):
            if acc.client is None:
                return
            try:
                async with self.rpc_sem:
                    await asyncio.wait_for(acc.client.disconnect(), self.cfg.reply_timeout)
            except Exception as exc:
                log.warning("断开协议号失败 phone=%s", acc.phone, exc_info=exc)
            acc.state = AccountState.OFFLINE

        await asyncio.gather(*(_disconnect(a) for a in tuple(self.accounts.values())))
