import asyncio
import logging
import time
from contextlib import asynccontextmanager
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession
from .session_store import save_authorized_session
from ..models.account import Account
from ..models.enums import AccountState
from ..utils.retry import retry_safe

log = logging.getLogger(__name__)


class AccountUnavailable(RuntimeError):
    pass


class UserbotManager:
    def __init__(self, cfg, repository, factory=None):
        self.cfg, self.repository = cfg, repository
        self.factory = factory or (lambda path: TelegramClient(str(path) if not isinstance(path, MemorySession) else path, cfg.api_id, cfg.api_hash,
                                    flood_sleep_threshold=0, request_retries=0, connection_retries=0,
                                    auto_reconnect=False, retry_delay=cfg.retry_base,
                                    raise_last_call_error=isinstance(path, MemorySession)))
        self.accounts: dict[str, Account] = {}
        self.semaphore = asyncio.Semaphore(cfg.concurrency)
        self.stopping = False

    def load(self):
        if self.accounts:
            return tuple(self.accounts.values())
        if not self.cfg.sessions.is_dir():
            log.warning('Session directory missing; no login will be requested')
            return ()
        for path in sorted(self.cfg.sessions.glob('*.session')):
            if not path.stem.isdigit() or path.is_symlink():
                log.warning('Skipping unsupported session filename')
                continue
            try:
                self.accounts[path.stem] = Account(path.stem, path, self.factory(path))
            except Exception as error:
                log.error('Session load failed error_type=%s', type(error).__name__)
                self.accounts[path.stem] = Account(path.stem, path, None, AccountState.ERROR,
                                                   error_type=type(error).__name__)
        return tuple(self.accounts.values())

    async def start(self):
        cooldowns = {r['phone']: r['until_at'] for r in await self.repository.cooldowns()}
        self.load()
        for a in self.accounts.values():
            a.unavailable_until = cooldowns.get(a.key, 0)
        await asyncio.gather(*(self.refresh(a, reconnect=True) for a in tuple(self.accounts.values())))

    def exists(self, key):
        return key in self.accounts or (self.cfg.sessions / (key + '.session')).exists()

    def login_client(self):
        return self.factory(MemorySession())

    async def enroll(self, key, session):
        if self.stopping or self.exists(key):
            raise AccountUnavailable('existing_or_stopping')
        path = save_authorized_session(self.cfg.sessions, key, session)
        account = Account(key, path, self.factory(path))
        self.accounts[key] = account
        await self.refresh(account, reconnect=True)
        if account.state != AccountState.ONLINE:
            raise AccountUnavailable('saved_but_reconnect_failed')
        return account

    async def flood_wait(self, account, seconds):
        account.unavailable_until = max(account.unavailable_until, time.time() + max(seconds, 1))
        account.state = AccountState.UNAVAILABLE
        await self.repository.set_cooldown(account.key, account.unavailable_until)

    async def refresh(self, account, *, reconnect=False):
        async with self.semaphore, account.lock:
            if account.client is None:
                account.state = AccountState.ERROR
                return account.state
            if account.unavailable_until > time.time():
                account.state = AccountState.UNAVAILABLE
                return account.state
            try:
                if not account.client.is_connected():
                    account.state = AccountState.OFFLINE
                    if not reconnect:
                        return account.state
                    await retry_safe(lambda: asyncio.wait_for(account.client.connect(), self.cfg.reply_timeout),
                                     attempts=self.cfg.retry_attempts, base=self.cfg.retry_base)
                authorized = await asyncio.wait_for(account.client.is_user_authorized(), self.cfg.reply_timeout)
                account.state = AccountState.ONLINE if authorized else AccountState.UNAUTHORIZED
                account.error_type = ''
            except asyncio.CancelledError:
                raise
            except FloodWaitError as error:
                await self.flood_wait(account, error.seconds)
            except Exception as error:
                account.state = AccountState.ERROR
                account.error_type = type(error).__name__
                log.warning('Account connection failed error_type=%s', account.error_type)
            return account.state

    @asynccontextmanager
    async def operation(self, account):
        async with self.semaphore, account.lock:
            if self.stopping or account.unavailable_until > time.time() or account.state != AccountState.ONLINE:
                raise AccountUnavailable('account_unavailable')
            if not account.client.is_connected():
                account.state = AccountState.OFFLINE
                raise AccountUnavailable('account_disconnected')
            try:
                yield account.client
            except FloodWaitError as error:
                await self.flood_wait(account, error.seconds)
                raise
            except (ConnectionError, OSError):
                account.state = AccountState.ERROR
                raise

    async def monitor(self):
        while not self.stopping:
            await asyncio.sleep(self.cfg.reconnect_interval)
            await asyncio.gather(*(self.refresh(a, reconnect=True) for a in tuple(self.accounts.values())))

    async def close(self):
        self.stopping = True
        async def disconnect(account):
            if account.client is None:
                return
            try:
                async with self.semaphore:
                    await asyncio.wait_for(account.client.disconnect(), self.cfg.reply_timeout)
            except Exception as error:
                log.warning('Disconnect failed error_type=%s', type(error).__name__)
            account.state = AccountState.OFFLINE
        await asyncio.gather(*(disconnect(a) for a in tuple(self.accounts.values())))
