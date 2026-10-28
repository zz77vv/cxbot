import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .enums import AccountState, Outcome


@dataclass
class Account:
    key: str
    session_path: Path
    client: Any
    state: AccountState = AccountState.OFFLINE
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    unavailable_until: float = 0
    error_type: str = ''


@dataclass(frozen=True)
class OperationResult:
    outcome: Outcome
    reason: str = ''
    error_type: str = ''
    value: int | None = None
    retry_after: float = 0
