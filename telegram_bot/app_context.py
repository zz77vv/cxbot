from dataclasses import dataclass
from .config import Config
from .clients.userbot_manager import UserbotManager
from .services.checkin_service import CheckinService
from .services.balance_service import BalanceService
from .services.account_status_service import AccountStatusService
from .services.jr.orchestrator import Orchestrator
from .services.jr.ocr_service import OCRService
from .services.jr.cache_service import CacheService
from .storage.database import Database
from .storage.repositories.user_repository import UserRepository
from .clients.request_tracker import RequestTracker


@dataclass(frozen=True)
class AppContext:
    config: Config
    database: Database
    accounts: UserbotManager
    checkins: CheckinService
    balances: BalanceService
    status: AccountStatusService
    jr: Orchestrator
    ocr: OCRService
    cache: CacheService
    users: UserRepository
    requests: RequestTracker
