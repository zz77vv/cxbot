from dataclasses import dataclass
from .enums import QueryState


@dataclass(frozen=True)
class JRQueryResult:
    kind: str
    text: str = ''
    file_path: str | None = None
    file_name: str | None = None
    export_status: str = 'unknown'
    fallback_reason: str | None = None


@dataclass(frozen=True)
class QueryReport:
    task_id: str
    state: QueryState
    reason: str
    result: JRQueryResult | None = None
    duplicate: bool = False
