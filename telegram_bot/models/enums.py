from enum import StrEnum


class Outcome(StrEnum):
    SUCCESS = 'success'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    PENDING_REVIEW = 'pending_review'
    MANUAL = 'manual'
    UNAVAILABLE = 'unavailable'
    TIMEOUT = 'timeout'
    FORMAT_CHANGED = 'format_changed'
    RATE_LIMITED = 'rate_limited'
    SKIPPED = 'skipped'


class AccountState(StrEnum):
    ONLINE = 'online'
    OFFLINE = 'offline'
    UNAUTHORIZED = 'unauthorized'
    ERROR = 'error'
    UNAVAILABLE = 'unavailable'


class QueryState(StrEnum):
    PREPARED = 'prepared'
    SUBMITTING = 'submitting'
    SUBMITTED = 'submitted'
    PURCHASING = 'purchasing'
    EXPORTING = 'exporting'
    COLLECTING = 'collecting'
    SUCCESS = 'success'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    PENDING_REVIEW = 'pending_review'
    MANUAL = 'manual'


UNRESOLVED = tuple(s.value for s in QueryState if s not in {QueryState.SUCCESS, QueryState.FAILED})
