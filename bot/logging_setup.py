"""轮转日志与敏感信息脱敏（保留安全栈帧位置，便于线上排障）。"""
from __future__ import annotations

import logging
import re
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config

_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")


class SafeRedactFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] = ()):
        super().__init__()
        self.secrets = tuple(s for s in secrets if s)

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return _TOKEN_RE.sub("[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        message = self._redact(record.getMessage())
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _, tb = record.exc_info
            frames = traceback.extract_tb(tb)
            location = ""
            if frames:
                last = frames[-1]
                location = f" at {Path(last.filename).name}:{last.lineno}({last.name})"
            message += f" error_type={exc_type.__name__}{location}"
            record.exc_info = None
            record.exc_text = None
        record.msg = message
        record.args = ()
        return True


def setup_logging(cfg: Config) -> logging.Handler:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        cfg.log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.addFilter(SafeRedactFilter((cfg.bot_token, cfg.api_hash)))
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler
