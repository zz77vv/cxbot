import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


def read_private_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ConfigError(f'.env 第 {number} 行缺少等号')
        key, value = line.split('=', 1)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            raise ConfigError(f'.env 第 {number} 行键名无效')
        values[key.strip()] = value.strip().strip('"\'')
    return values


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    admin_id: int
    api_id: int
    api_hash: str = field(repr=False)
    database: Path
    sessions: Path
    log_file: Path
    cache: Path
    concurrency: int = 5
    jr_balance_concurrency: int = 8
    reply_timeout: float = 30
    export_timeout: float = 45
    retry_attempts: int = 3
    retry_base: float = 2
    reconnect_interval: float = 60
    cache_ttl: int = 300
    cleanup_interval: int = 60
    ocr_timeout: float = 20
    shutdown_timeout: float = 30

    @classmethod
    def load(cls, root: Path | None = None, environ=None):
        root = (root or Path(__file__).resolve().parents[1]).resolve()
        env = read_private_env(root / '.env')
        env.update(os.environ if environ is None else environ)

        def required(name):
            value = env.get(name, '').strip()
            if not value:
                raise ConfigError(f'缺少配置 {name}')
            return value

        def positive(name, default=None, cast=int):
            raw = env.get(name, default)
            try:
                value = cast(raw)
                if value <= 0 or (isinstance(value, float) and not __import__('math').isfinite(value)):
                    raise ValueError
                return value
            except (ValueError, TypeError):
                raise ConfigError(f'{name} 必须为有效正数') from None

        token = required('BOT_TOKEN')
        api_hash = required('API_HASH')
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
            raise ConfigError('BOT_TOKEN 格式错误')
        if not re.fullmatch(r'[0-9a-fA-F]{32}', api_hash):
            raise ConfigError('API_HASH 格式错误')

        def path(name, default):
            raw = env.get(name, default).strip()
            if not raw:
                raise ConfigError(f'{name} 路径为空')
            candidate = Path(raw).expanduser()
            return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

        cfg = cls(token, positive('ADMIN_ID'), positive('API_ID'), api_hash,
                  path('DATABASE_PATH', 'data/bot.db'), path('SESSIONS_DIR', 'sessions'),
                  path('LOG_PATH', 'logs/bot.log'), path('CACHE_DIR', '.jr_cache_files'),
                  positive('MAX_CONCURRENCY', 5), positive('JR_BALANCE_CONCURRENCY', 8),
                  positive('REPLY_TIMEOUT', 30, float), positive('EXPORT_TIMEOUT', 45, float),
                  positive('RETRY_ATTEMPTS', 3), positive('RETRY_BASE', 2, float),
                  positive('RECONNECT_INTERVAL', 60, float),
                  ocr_timeout=positive('OCR_TIMEOUT', 20, float),
                  shutdown_timeout=positive('SHUTDOWN_TIMEOUT', 30, float))
        for protected in (cfg.database, cfg.sessions, cfg.log_file):
            if protected == cfg.cache or cfg.cache in protected.parents:
                raise ConfigError('缓存目录不得包含数据库、Session 或日志')
        if cfg.cache == root or cfg.cache in root.parents or cfg.cache == Path(cfg.cache.anchor):
            raise ConfigError('缓存目录必须为专用子目录')
        if cfg.sessions in cfg.cache.parents:
            raise ConfigError('缓存目录不得位于 Session 目录内')
        if cfg.database == cfg.log_file or cfg.sessions in cfg.database.parents or cfg.sessions in cfg.log_file.parents:
            raise ConfigError('数据库、日志和 Session 路径必须分离')
        return cfg
