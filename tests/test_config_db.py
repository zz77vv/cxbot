import logging
import time
from pathlib import Path

import pytest

from bot.config import Config, ConfigError
from bot.db import CachedResult, Database, make_cache_key
from bot.logging_setup import SafeRedactFilter


def _base_env(tmp_path: Path, **overrides) -> dict[str, str]:
    env = {
        "BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        "ADMIN_ID": "999888777",
        "API_ID": "12345",
        "API_HASH": "0123456789abcdef0123456789abcdef",
        "DATABASE_PATH": str(tmp_path / "data" / "bot.db"),
        "SESSIONS_DIR": str(tmp_path / "sessions"),
        "LOG_PATH": str(tmp_path / "logs" / "bot.log"),
        "CACHE_DIR": str(tmp_path / "cache"),
    }
    env.update(overrides)
    return env


def test_config_allows_any_positive_admin_id(tmp_path):
    cfg = Config.load(root=tmp_path, environ=_base_env(tmp_path, ADMIN_ID="424242"))
    assert cfg.admin_id == 424242
    assert cfg.balance_cache_ttl == 60


def test_config_rejects_unsafe_cache_dir(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(root=tmp_path, environ=_base_env(tmp_path, CACHE_DIR=str(tmp_path)))


def test_redact_filter_masks_secrets_and_keeps_location():
    flt = SafeRedactFilter(("secret-token",))
    try:
        raise RuntimeError("boom secret-token")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("test", logging.ERROR, __file__, 1, "fail %s", ("secret-token",), sys.exc_info())
    assert flt.filter(rec)
    assert "secret-token" not in rec.msg
    assert "[REDACTED]" in rec.msg
    assert "error_type=RuntimeError" in rec.msg
    assert "test_config_db.py:" in rec.msg


async def test_database_lifecycle_and_cache(tmp_path):
    db = await Database(tmp_path / "bot.db").open()
    try:
        await db.set_jr_cooldown("10001", time.time() + 60, force_free=True)
        meta = await db.get_account_meta("10001")
        assert meta["jr_membership"] == "free"
        assert meta["jr_cooldown_until"] > time.time()

        await db.set_jr_membership("10001", "member")
        meta = await db.get_account_meta("10001")
        assert meta["jr_membership"] == "member"
        assert meta["jr_cooldown_until"] == 0.0

        assert await db.get_jr_daily_usage("10001") == 0
        assert await db.inc_jr_daily_usage("10001") == 1
        assert await db.inc_jr_daily_usage("10001") == 2

        key = make_cache_key("jr", "  Alice  Bob ")
        assert key == make_cache_key("jr", "alice bob")
        await db.set_cache(key, CachedResult("text", text="hello"), ttl=60)
        cached = await db.get_cache(key)
        assert cached is not None and cached.text == "hello"
    finally:
        await db.close()
