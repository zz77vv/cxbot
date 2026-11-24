"""管理员私聊登录协议号状态机（手机号 → 验证码 → 可选两步验证，不落盘明文凭据）。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from telethon.errors import (
    FloodWaitError,
    NetworkMigrateError,
    PhoneMigrateError,
    SessionPasswordNeededError,
    UserMigrateError,
)

from .config import Config
from .userbots import UserbotPool

log = logging.getLogger(__name__)


@dataclass(repr=False)
class _Attempt:
    expires_at: float
    phase: str = "phone"  # phone | code | password
    phone: str = ""
    code_hash: str = field(default="", repr=False)
    client: object = field(default=None, repr=False)


class LoginService:
    def __init__(self, cfg: Config, pool: UserbotPool, on_enrolled=None):
        self.cfg = cfg
        self.pool = pool
        self.on_enrolled = on_enrolled
        self._attempt: _Attempt | None = None
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0

    @property
    def is_active(self) -> bool:
        return self._attempt is not None and time.monotonic() < self._attempt.expires_at

    async def _drop(self) -> None:
        attempt = self._attempt
        self._attempt = None
        if attempt and attempt.client is not None:
            try:
                await asyncio.wait_for(attempt.client.disconnect(), self.cfg.reply_timeout)
            except Exception:
                pass

    async def begin(self) -> str:
        async with self._lock:
            if time.time() < self._cooldown_until:
                return "电报请求受限，正在冷却等待，请稍后再试。"
            if self.is_active:
                return "已有登录流程进行中，请继续输入，或先发送 /cancel 取消。"
            await self._drop()
            self._attempt = _Attempt(time.monotonic() + self.cfg.login_ttl)
            return (
                "请输入国际格式手机号（例如 +8613800000000）。\n"
                "限时 5 分钟，随时可发 /cancel 退出。输入后机器人会尝试自动删除你的消息。"
            )

    async def cancel(self) -> str:
        async with self._lock:
            await self._drop()
        return "登录流程已取消。"

    async def submit(self, raw_value: str) -> str | None:
        async with self._lock:
            attempt = self._attempt
            if attempt is None:
                return None
            if time.monotonic() >= attempt.expires_at:
                await self._drop()
                return "登录已超时，请重新发送 /login。"
            try:
                remaining = max(0.1, attempt.expires_at - time.monotonic())
                return await asyncio.wait_for(
                    self._step(attempt, raw_value),
                    min(self.cfg.reply_timeout, remaining),
                )
            except SessionPasswordNeededError:
                attempt.phase = "password"
                return "该账号开启了两步验证，请输入两步验证密码（或 /cancel 退出）。"
            except FloodWaitError as exc:
                self._cooldown_until = time.time() + max(1, exc.seconds)
                await self._drop()
                return f"触发电报限流，请等待约 {exc.seconds} 秒后再重新 /login。"
            except asyncio.CancelledError:
                await self._drop()
                raise
            except ValueError as exc:
                code = str(exc)
                if code == "invalid_phone":
                    return "手机号格式不对，请输入带国际区号的数字（如 +8613800000000），或 /cancel 取消。"
                if code == "invalid_code":
                    return "验证码格式不对，请输入 4-8 位数字验证码，或 /cancel 取消。"
                await self._drop()
                return f"登录校验失败（{code}），流程已退出，请重新 /login。"
            except Exception as exc:
                log.warning("登录失败 phase=%s", attempt.phase, exc_info=exc)
                friendly = {
                    "PhoneCodeInvalidError": "验证码错误",
                    "PhoneCodeExpiredError": "验证码已过期",
                    "PasswordHashInvalidError": "两步验证密码错误",
                    "PhoneNumberInvalidError": "手机号无效",
                    "PhoneNumberBannedError": "该手机号已被电报封禁",
                    "PhoneNumberFloodError": "该手机号请求过于频繁",
                    "TimeoutError": "连接电报超时",
                    "FileExistsError": "该账号 Session 已存在，未覆盖",
                }.get(type(exc).__name__, f"发生错误({type(exc).__name__})")
                await self._drop()
                return f"登录未完成：{friendly}。流程已退出，可重新 /login。"

    async def _step(self, attempt: _Attempt, value: str) -> str:
        if attempt.phase == "phone":
            cleaned = value.strip()
            if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", cleaned):
                raise ValueError("invalid_phone")
            phone = cleaned.lstrip("+")
            if self.pool.exists(phone):
                await self._drop()
                return f"账号 {phone} 已存在，禁止覆盖。可通过 /status 查看。"
            attempt.phone = phone
            attempt.client = self.pool.create_login_client()
            await attempt.client.connect()
            try:
                resp = await attempt.client.send_code_request("+" + phone)
            except (PhoneMigrateError, NetworkMigrateError, UserMigrateError):
                resp = await attempt.client.send_code_request("+" + phone)
            attempt.code_hash = resp.phone_code_hash
            attempt.phase = "code"
            return "验证码已发送，请输入收到的数字验证码："

        if attempt.phase == "code":
            code = re.sub(r"[\s-]", "", value)
            if not re.fullmatch(r"[0-9]{4,8}", code):
                raise ValueError("invalid_code")
            await attempt.client.sign_in(
                phone="+" + attempt.phone,
                code=code,
                phone_code_hash=attempt.code_hash,
            )
        else:
            await attempt.client.sign_in(password=value)

        if not await attempt.client.is_user_authorized():
            raise ValueError("not_authorized")
        me = await attempt.client.get_me()
        if str(getattr(me, "phone", "")).lstrip("+") != attempt.phone:
            raise ValueError("account_mismatch")

        session = attempt.client.session
        await attempt.client.disconnect()
        attempt.client = None
        acc = await self.pool.enroll(attempt.phone, session)
        await self._drop()
        if self.on_enrolled:
            self.on_enrolled()
        return f"✅ 登录成功！账号 {acc.phone} 已安全保存并上线，已加入自动签到管理。"

    async def cleanup_loop(self) -> None:
        while not self.pool.stopping:
            await asyncio.sleep(5)
            async with self._lock:
                if self._attempt and time.monotonic() >= self._attempt.expires_at:
                    await self._drop()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()
