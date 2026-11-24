"""应用组装与生命周期管理（启动、后台任务托管、优雅逆序停机）。"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from telegram.ext import Application

from .balance import BalanceService
from .checkin import CheckinService
from .config import Config, ConfigError
from .db import Database
from .handlers import register_handlers, sync_admin_menu
from .hw import HWService
from .jr import JRService, ManualVerifyManager
from .logging_setup import setup_logging
from .login import LoginService
from .ocr import OCRService
from .userbots import UserbotPool

log = logging.getLogger(__name__)


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.database)
        self.pool = UserbotPool(cfg, self.db)
        self.ocr = OCRService(timeout=cfg.ocr_timeout)
        self.balances = BalanceService(self.pool, cache_ttl=cfg.balance_cache_ttl)
        self.checkins = CheckinService(self.pool, self.db)
        self.manual_verify = ManualVerifyManager(cfg)
        self.jr = JRService(cfg, self.db, self.pool, self.balances, self.ocr, self.manual_verify)
        self.hw = HWService(cfg, self.pool)
        self.login = LoginService(cfg, self.pool, on_enrolled=self._on_account_enrolled)

        self.application = (
            Application.builder()
            .token(cfg.bot_token)
            .concurrent_updates(cfg.max_concurrency)
            .connect_timeout(cfg.reply_timeout)
            .read_timeout(cfg.reply_timeout)
            .write_timeout(cfg.export_timeout)
            .build()
        )
        self.manual_verify.bot = self.application.bot
        register_handlers(self.application, self)

        self.stop_event = asyncio.Event()
        self._bg_tasks: set[asyncio.Task] = set()
        self._initialized = False

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _on_account_enrolled(self) -> None:
        self.checkins.ensure_scheduled(self._spawn)

    async def _cache_purge_loop(self) -> None:
        while not self.pool.stopping:
            await asyncio.sleep(60)
            try:
                await self.db.purge_expired_cache(self.cfg.cache_dir)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("清理过期缓存失败", exc_info=exc)

    async def run(self) -> None:
        log_handler = setup_logging(self.cfg)
        try:
            await self.db.open()
            await self.pool.start()
            await self.application.initialize()
            self._initialized = True
            await sync_admin_menu(self.application.bot, self.cfg.admin_id)
            await self.application.start()
            await self.application.updater.start_polling(drop_pending_updates=False)

            self.checkins.ensure_scheduled(self._spawn)
            self._spawn(self.pool.monitor_loop(), name="account-monitor")
            self._spawn(self.login.cleanup_loop(), name="login-cleanup")
            self._spawn(self._cache_purge_loop(), name="cache-purge")
            log.info("cxbot v3 启动完成，已加载协议号: %d", len(self.pool.accounts))

            loop = asyncio.get_running_loop()
            installed = []
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.stop_event.set)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            try:
                await self.stop_event.wait()
            finally:
                for sig in installed:
                    loop.remove_signal_handler(sig)
        finally:
            await self.close()
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()

    async def close(self) -> None:
        self.pool.stopping = True
        for task in tuple(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*tuple(self._bg_tasks), return_exceptions=True)

        if self._initialized:
            for step in (
                lambda: self.application.updater.stop() if self.application.updater.running else asyncio.sleep(0),
                lambda: self.application.stop() if self.application.running else asyncio.sleep(0),
                self.application.shutdown,
            ):
                try:
                    await asyncio.wait_for(step(), self.cfg.shutdown_timeout)
                except Exception as exc:
                    log.warning("关闭 Bot 阶段异常", exc_info=exc)

        for closer in (self.login.close, self.ocr.close, self.pool.close, self.db.close):
            try:
                await closer()
            except Exception as exc:
                log.warning("释放资源异常", exc_info=exc)


def main() -> None:
    try:
        cfg = Config.load()
        asyncio.run(BotApp(cfg).run())
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"启动或运行失败：{type(exc).__name__}，请查看日志文件。", file=sys.stderr)
        raise SystemExit(1)
