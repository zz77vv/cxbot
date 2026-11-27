"""三目标积分查询服务（内置短时内存缓存，避免批量查询高频刷 /balance 触发限流）。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from telethon.errors import FloodWaitError

from .targets import TARGETS, TargetBot
from .userbots import Account, AccountUnavailable, UserbotPool, wait_reply

log = logging.getLogger(__name__)
BATCH_TIMEOUT = 240.0


@dataclass(frozen=True)
class BalanceResult:
    ok: bool
    value: int | None = None
    reason: str = ""
    rate_limited: bool = False


def parse_balance_text(text: str, target: TargetBot) -> int | None:
    match = re.search(target.balance_pattern, text or "")
    return int(match.group(1)) if match else None


class BalanceService:
    def __init__(self, pool: UserbotPool, cache_ttl: int = 60, on_balance_updated=None):
        self.pool = pool
        self.cache_ttl = cache_ttl
        self.on_balance_updated = on_balance_updated
        # (phone, bot_key) -> (value, expires_at)
        self._cache: dict[tuple[str, str], tuple[int, float]] = {}

    def invalidate(self, phone: str, bot_key: str | None = None) -> None:
        if bot_key is None:
            for k in [k for k in self._cache if k[0] == phone]:
                self._cache.pop(k, None)
        else:
            self._cache.pop((phone, bot_key), None)

    def _get_cached(self, cache_key: tuple[str, str]) -> BalanceResult | None:
        if self.cache_ttl <= 0:
            return None
        cached = self._cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return BalanceResult(ok=True, value=cached[0], reason="cached")
        return None

    def peek_cached(self, phone: str, bot_key: str) -> int | None:
        """只读获取未过期的缓存余额，未命中返回 None。"""
        hit = self._get_cached((phone, bot_key))
        return hit.value if hit is not None else None

    async def query(
        self, account: Account, target: TargetBot, *, use_cache: bool = False
    ) -> BalanceResult:
        cache_key = (account.phone, target.key)
        if use_cache:
            hit = self._get_cached(cache_key)
            if hit is not None:
                return hit

        try:
            async with self.pool.hold_account(account) as client:
                # Double-checked locking: 防止并发穿透重复发 /balance
                if use_cache:
                    hit = self._get_cached(cache_key)
                    if hit is not None:
                        return hit
                async with self.pool.rpc_slot():
                    sent = await asyncio.wait_for(
                        client.send_message(target.username, target.balance_cmd),
                        self.pool.cfg.reply_timeout,
                    )
                    observed = False

                    def _matched(msg) -> bool:
                        nonlocal observed
                        text = getattr(msg, "text", "") or ""
                        if text:
                            observed = True
                        return parse_balance_text(text, target) is not None

                    reply = await wait_reply(
                        client,
                        target.username,
                        sent.id,
                        _matched,
                        target.balance_timeout,
                        rpc_timeout=self.pool.cfg.reply_timeout,
                    )
                if reply is not None:
                    val = parse_balance_text(getattr(reply, "text", ""), target)
                    if val is not None:
                        if self.cache_ttl > 0:
                            self._cache[cache_key] = (val, time.monotonic() + self.cache_ttl)
                        if self.on_balance_updated:
                            self.on_balance_updated(account.phone, target.key, val)
                        return BalanceResult(ok=True, value=val)
                return BalanceResult(
                    ok=False,
                    reason="回复格式已变化" if observed else "等待回复超时",
                )
        except AccountUnavailable:
            return BalanceResult(ok=False, reason="账号不可用")
        except FloodWaitError as exc:
            return BalanceResult(ok=False, reason=f"触发限流({exc.seconds}s)", rate_limited=True)
        except TimeoutError:
            return BalanceResult(ok=False, reason="通信超时")
        except Exception as exc:
            log.warning("查询积分失败 phone=%s target=%s", account.phone, target.name, exc_info=exc)
            return BalanceResult(ok=False, reason=f"异常({type(exc).__name__})")

    async def query_all(self) -> list[tuple[str, list[tuple[str, BalanceResult]]]]:
        accounts = tuple(self.pool.accounts.values())
        unfinished = BalanceResult(ok=False, reason="总耗时超限")
        results: dict[str, list[tuple[str, BalanceResult]]] = {
            acc.phone: [(t.name, unfinished) for t in TARGETS] for acc in accounts
        }

        async def _per_account(acc: Account):
            for idx, target in enumerate(TARGETS):
                res = await self.query(acc, target, use_cache=False)
                results[acc.phone][idx] = (target.name, res)

        try:
            async with asyncio.timeout(BATCH_TIMEOUT):
                await asyncio.gather(*(_per_account(acc) for acc in accounts))
        except TimeoutError:
            log.warning("批量积分查询超时")
        return list(results.items())
