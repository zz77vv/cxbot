"""配置加载与校验（完全由 .env 和环境变量驱动，无硬编码管理员 ID）。"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """配置缺失或格式无效。"""


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f".env 第 {lineno} 行缺少等号")
        key, val = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError(f".env 第 {lineno} 行键名无效: {key}")
        values[key] = val.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    admin_id: int
    api_id: int
    api_hash: str = field(repr=False)
    database: Path
    sessions_dir: Path
    log_file: Path
    cache_dir: Path
    max_concurrency: int = 5
    reply_timeout: float = 30.0
    export_timeout: float = 60.0
    balance_cache_ttl: int = 60
    result_cache_ttl: int = 300
    ocr_timeout: float = 20.0
    login_ttl: float = 300.0
    reconnect_interval: float = 60.0
    shutdown_timeout: float = 20.0

    @classmethod
    def load(cls, root: Path | None = None, environ: dict[str, str] | None = None) -> Config:
        base = (root or Path(__file__).resolve().parents[1]).resolve()
        env = parse_env_file(base / ".env")
        env.update(os.environ if environ is None else environ)

        def req(name: str) -> str:
            val = env.get(name, "").strip()
            if not val:
                raise ConfigError(f"缺少必填配置: {name}")
            return val

        def pos(name: str, default: int | float | None = None, cast=int):
            raw = env.get(name, default)
            try:
                val = cast(raw)
                if val <= 0 or (isinstance(val, float) and not math.isfinite(val)):
                    raise ValueError
                return val
            except (TypeError, ValueError):
                raise ConfigError(f"{name} 必须为正数") from None

        def resolve_path(name: str, default: str) -> Path:
            raw = env.get(name, default).strip()
            if not raw:
                raise ConfigError(f"{name} 路径不能为空")
            p = Path(raw).expanduser()
            return (base / p).resolve() if not p.is_absolute() else p.resolve()

        token = req("BOT_TOKEN")
        api_hash = req("API_HASH")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigError("BOT_TOKEN 格式无效")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ConfigError("API_HASH 格式无效（应为 32 位十六进制）")

        cfg = cls(
            bot_token=token,
            admin_id=pos("ADMIN_ID"),
            api_id=pos("API_ID"),
            api_hash=api_hash,
            database=resolve_path("DATABASE_PATH", "data/bot.db"),
            sessions_dir=resolve_path("SESSIONS_DIR", "sessions"),
            log_file=resolve_path("LOG_PATH", "logs/bot.log"),
            cache_dir=resolve_path("CACHE_DIR", "cache"),
            max_concurrency=pos("MAX_CONCURRENCY", 5),
            reply_timeout=pos("REPLY_TIMEOUT", 30.0, float),
            export_timeout=pos("EXPORT_TIMEOUT", 60.0, float),
            balance_cache_ttl=pos("BALANCE_CACHE_TTL", 60),
            result_cache_ttl=pos("RESULT_CACHE_TTL", 300),
            ocr_timeout=pos("OCR_TIMEOUT", 20.0, float),
            login_ttl=pos("LOGIN_TTL", 300.0, float),
            reconnect_interval=pos("RECONNECT_INTERVAL", 60.0, float),
            shutdown_timeout=pos("SHUTDOWN_TIMEOUT", 20.0, float),
        )

        # 确保缓存目录不会误删数据库/Session/日志
        for protected in (cfg.database, cfg.sessions_dir, cfg.log_file):
            if protected == cfg.cache_dir or cfg.cache_dir in protected.parents:
                raise ConfigError("CACHE_DIR 不得包含数据库、Session 或日志目录")
        if cfg.cache_dir == base or cfg.cache_dir in base.parents:
            raise ConfigError("CACHE_DIR 必须为独立子目录")
        return cfg
