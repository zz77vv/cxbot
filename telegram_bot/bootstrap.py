import asyncio
import logging
import signal
import sys
from pathlib import Path
from .config import Config, ConfigError
from .logging_config import configure_logging
from .app_context import AppContext
from .lifecycle import Lifecycle
from .clients.userbot_manager import UserbotManager
from .clients.bot_application import BotApplication, create_application
from .clients.dialogue import Dialogue
from .clients.request_tracker import RequestTracker
from .storage.database import Database
from .storage.migrations import SchemaError
from .storage.repositories.account_repository import AccountRepository
from .storage.repositories.checkin_repository import CheckinRepository
from .storage.repositories.query_repository import QueryRepository
from .storage.repositories.cache_repository import CacheRepository
from .storage.repositories.membership_repository import MembershipRepository
from .storage.repositories.user_repository import UserRepository
from .services.login_service import LoginService
from .services.checkin_service import CheckinService
from .services.balance_service import BalanceService
from .services.account_status_service import AccountStatusService
from .services.jr.membership_service import MembershipService
from .services.jr.account_selector import AccountSelector
from .services.jr.target_client import TargetClient
from .services.jr.pagination import PaginationService
from .services.jr.export_service import ExportService
from .services.jr.ocr_service import OCRService
from .services.jr.cache_service import CacheService
from .services.jr.orchestrator import Orchestrator
from .services.jr.manual_verification import ManualVerificationService
from .handlers.registration import register
from .utils.process_lock import ProcessLock


async def run(cfg):
    lock = ProcessLock(cfg.database)
    lock.acquire()
    db = Database(cfg.database)
    lifecycle = None
    log_handler = None
    try:
        log_handler = configure_logging(cfg)
        await db.open()
        manager = UserbotManager(cfg, AccountRepository(db))
        dialogue = Dialogue(cfg)
        queries = QueryRepository(db)
        cache = CacheService(cfg, CacheRepository(db))
        balances = BalanceService(manager, dialogue)
        memberships = MembershipService(MembershipRepository(db))
        selector = AccountSelector(manager, balances, memberships, queries)
        target = TargetClient(dialogue, cfg)
        application = create_application(cfg)
        exports = ExportService(cfg, dialogue, queries, cache, application.bot)
        ocr = OCRService(cfg.ocr_timeout)
        verification = ManualVerificationService(application.bot, dialogue, target, cfg)
        jr = Orchestrator(manager, queries, selector, target, PaginationService(target), exports, cache, None,
                          manual_verification=verification)
        checkins = CheckinService(manager, CheckinRepository(db), dialogue)
        logins = LoginService(cfg, manager, lambda: checkins.start(lifecycle.registry))
        ctx = AppContext(cfg, db, manager, checkins,
                         balances, AccountStatusService(manager), jr, ocr, cache, UserRepository(db), RequestTracker(), logins)
        lifecycle = Lifecycle(ctx, BotApplication(application, cfg), queries)
        register(application, ctx)
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lifecycle.stop_event.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass  # Windows asyncio.run handles Ctrl+C through cancellation.
        try:
            await lifecycle.run()
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
    except Exception as error:
        logging.getLogger(__name__).error('Startup/run failed error_type=%s', type(error).__name__)
        raise
    finally:
        if lifecycle:
            await lifecycle.close()
        else:
            await db.close()
        lock.close()
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()


def main():
    try:
        cfg = Config.load()
        asyncio.run(run(cfg))
    except ConfigError as error:
        print(f'配置错误：{error}', file=sys.stderr)
        raise SystemExit(2)
    except SchemaError as error:
        print(f'数据库兼容性检查失败：{error}', file=sys.stderr)
        raise SystemExit(3)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f'启动/运行失败：{type(error).__name__}；请查看脱敏日志。', file=sys.stderr)
        raise SystemExit(1)
