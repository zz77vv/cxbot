"""Private admin login. Codes/passwords are transient arguments, never persisted."""
import asyncio
import re
import time
from dataclasses import dataclass, field
from collections import deque
from telethon.errors import SessionPasswordNeededError, FloodWaitError


@dataclass(repr=False)
class LoginAttempt:
    expires: float
    phase: str = 'phone'
    phone: str = ''
    code_hash: str = field(default='', repr=False)
    client: object = field(default=None, repr=False)


class LoginService:
    def __init__(self, cfg, manager, on_added):
        self.cfg, self.manager, self.on_added = cfg, manager, on_added
        self.attempts = {}
        self.lock = asyncio.Lock()
        self.cooldown_until = 0
        self.seen_updates = deque(maxlen=512)

    def allowed(self, user, chat, private):
        return private and user == self.cfg.admin_id and chat == user

    async def _drop(self, actor):
        attempt = self.attempts.pop(actor, None)
        if attempt and attempt.client:
            try:
                await asyncio.wait_for(attempt.client.disconnect(), self.cfg.reply_timeout)
            except Exception:
                # Cleanup failure must never expose the RPC exception or credential.
                return

    async def begin(self, user, chat, private):
        if not self.allowed(user, chat, private):
            return '登录仅限管理员私聊。'
        async with self.lock:
            if time.time() < self.cooldown_until:
                return 'Telegram 限流冷却中，请稍后再试。'
            old = self.attempts.get(user)
            if old and time.monotonic() < old.expires:
                return '已有登录流程，请继续输入或先 /cancel。'
            await self._drop(user)
            self.attempts[user] = LoginAttempt(time.monotonic() + self.cfg.login_ttl)
            return '请输入国际格式手机号（例如 +8613800000000）。流程限时 5 分钟，可用 /cancel 退出。验证码和密码输入后将尝试删除消息；机器人私聊不是端到端加密。'

    async def cancel(self, user, chat, private):
        if not self.allowed(user, chat, private):
            return '登录仅限管理员私聊。'
        async with self.lock:
            await self._drop(user)
        return '登录已取消。'

    async def submit(self, user, chat, private, value, request_id=None):
        if not self.allowed(user, chat, private):
            return '登录仅限管理员私聊。'
        async with self.lock:
            if request_id is not None:
                if request_id in self.seen_updates:
                    return None
                self.seen_updates.append(request_id)
            attempt = self.attempts.get(user)
            if not attempt:
                return None
            if time.monotonic() >= attempt.expires:
                await self._drop(user)
                return '登录已超时，请重新 /login。'
            try:
                return await asyncio.wait_for(self._step(attempt, value),
                    min(self.cfg.reply_timeout, max(0.01, attempt.expires - time.monotonic())))
            except SessionPasswordNeededError:
                attempt.phase = 'password'
                return '账号启用了两步验证，请输入密码；或 /cancel 退出。'
            except FloodWaitError as error:
                self.cooldown_until = time.time() + max(1, error.seconds)
                await self._drop(user)
                return 'Telegram 要求等待，登录已退出；冷却结束后重新 /login。'
            except asyncio.CancelledError:
                await self._drop(user)
                raise
            except Exception as error:
                reasons = {
                    'PhoneCodeInvalidError': '验证码错误',
                    'PhoneCodeExpiredError': '验证码已过期',
                    'PasswordHashInvalidError': '两步验证密码错误',
                    'PhoneNumberInvalidError': '手机号无效',
                    'PhoneNumberBannedError': '该号码被 Telegram 限制',
                    'TimeoutError': '连接或流程超时，外部登录结果需核实',
                    'FileExistsError': 'Session 已存在，未覆盖',
                }
                reason = reasons.get(type(error).__name__, '当前步骤的格式、连接、保存或授权验证失败')
                await self._drop(user)
                return f'登录未完成：{reason}，已退出。请重新 /login。若已保存 Session，可通过 /status 或重启核实。'
            finally:
                value = None

    async def _step(self, attempt, value):
        if attempt.phase == 'phone':
            if not re.fullmatch(r'\+?[1-9][0-9]{6,14}', value.strip()):
                raise ValueError('invalid_phone')
            attempt.phone = value.strip().lstrip('+')
            if self.manager.exists(attempt.phone):
                await self._drop(self.cfg.admin_id)
                return '该账号已存在 Session，禁止覆盖；请使用 /status 查看状态。'
            attempt.client = self.manager.login_client()
            await attempt.client.connect()
            response = await attempt.client.send_code_request('+' + attempt.phone)
            attempt.code_hash = response.phone_code_hash
            attempt.phase = 'code'
            return '请输入 Telegram 登录验证码；如验证码被 Telegram 判定为转发而失效，请取消后使用服务器本地登录。'
        if attempt.phase == 'code':
            code = re.sub(r'[\s-]', '', value)
            if not re.fullmatch(r'[0-9]{4,8}', code):
                raise ValueError('invalid_code')
            await attempt.client.sign_in(phone='+' + attempt.phone, code=code, phone_code_hash=attempt.code_hash)
        else:
            await attempt.client.sign_in(password=value)
        if not await attempt.client.is_user_authorized():
            raise ValueError('not_authorized')
        me = await attempt.client.get_me()
        if str(getattr(me, 'phone', '')).lstrip('+') != attempt.phone:
            raise ValueError('account_mismatch')
        session = attempt.client.session
        await attempt.client.disconnect()
        attempt.client = None
        account = await self.manager.enroll(attempt.phone, session)
        await self._drop(self.cfg.admin_id)
        self.on_added()
        return f'登录成功：{account.key}；Session 已安全保存并重新连接验证，已纳入账号管理。'

    async def expire(self):
        async with self.lock:
            for actor, attempt in tuple(self.attempts.items()):
                if time.monotonic() >= attempt.expires:
                    await self._drop(actor)

    async def loop(self):
        while True:
            await asyncio.sleep(5)
            await self.expire()

    async def close(self):
        async with self.lock:
            for actor in tuple(self.attempts):
                await self._drop(actor)
