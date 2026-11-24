"""自动定时签到（24h 最小间隔 + 随机抖动）与手动防重入签到。"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from telethon.errors import FloodWaitError

from .db import Database
from .targets import CHECKIN_JITTER_MAX, CHECKIN_MIN_INTERVAL, TARGETS, TargetBot
from .userbots import (
    Account,
    AccountUnavailable,
    UserbotPool,
    find_button,
    msg_signature,
    wait_reply,
)

log = logging.getLogger(__name__)
MANUAL_TIMEOUT = 240.0


@dataclass(frozen=True)
class CheckinResult:
    status: str  # success | skipped | failed | timeout | unavailable | rate_limited
    reason: str = ""


class CheckinService:
    def __init__(self, pool: UserbotPool, db: Database, rng: random.Random | None = None):
        self.pool = pool
        self.db = db
        self.rng = rng or random.Random()
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.manual_lock = asyncio.Lock()

    async def _perform(self, client, target: TargetBot) -> CheckinResult:
        rpc_timeout = self.pool.cfg.reply_timeout
        sent = await asyncio.wait_for(
            client.send_message(target.username, target.checkin_cmd), rpc_timeout
        )
        last_msg = None
        old_sig = None

        for label in target.checkin_buttons:
            msg = await wait_reply(
                client,
                target.username,
                sent.id,
                lambda m, kw=label: find_button(m, (kw,)) is not None,
                45.0,
                original=last_msg,
                old_sig=old_sig,
                rpc_timeout=rpc_timeout,
            )
            if msg is None:
                return CheckinResult("failed", f"未找到按钮[{label}]")
            r, c, _ = find_button(msg, (label,))
            old_sig = msg_signature(msg)
            last_msg = msg
            await asyncio.wait_for(msg.click(r, c), rpc_timeout)

        def _confirmed(m) -> bool:
            text = getattr(m, "text", "") or ""
            if any(k in text for k in ("签到失败", "操作失败", "请求失败", "未获得")):
                return False
            if target.checkin_buttons and m.id == getattr(last_msg, "id", None) and msg_signature(m) == old_sig:
                return False
            return any(k in text for k in target.checkin_success_keywords)

        reply = await wait_reply(
            client,
            target.username,
            last_msg.id if last_msg else sent.id,
            _confirmed,
            target.checkin_timeout,
            original=last_msg,
            old_sig=old_sig,
            rpc_timeout=rpc_timeout,
        )
        if reply is not None:
            return CheckinResult("success", "已签到成功")
        return CheckinResult("timeout", "等待签到确认超时")

    async def run_one(self, account: Account, target: TargetBot) -> CheckinResult:
        now = time.time()
        row = await self.db.init_checkin(
            account.phone, target.key, now + self.rng.uniform(0, CHECKIN_MIN_INTERVAL)
        )
        last = max(row["last_attempt_at"], row["last_success_at"])
        if last and now < last + CHECKIN_MIN_INTERVAL:
            return CheckinResult("skipped", "24小时内已尝试")

        try:
            async with self.pool.use(account) as client:
                try:
                    res = await self._perform(client, target)
                except FloodWaitError as exc:
                    await self.pool.mark_flood_wait(account, exc.seconds)
                    res = CheckinResult("rate_limited", f"限流({exc.seconds}s)")
                except TimeoutError:
                    res = CheckinResult("timeout", "通信超时")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("签到异常 phone=%s target=%s", account.phone, target.name, exc_info=exc)
                    res = CheckinResult("failed", type(exc).__name__)
        except AccountUnavailable:
            return CheckinResult("unavailable", "账号不可用")

        finished = time.time()
        next_at = max(
            finished + CHECKIN_MIN_INTERVAL + self.rng.uniform(0, CHECKIN_JITTER_MAX),
            account.flood_until,
        )
        await self.db.record_checkin(
            account.phone,
            target.key,
            now=finished,
            next_at=next_at,
            status=res.status,
            success=(res.status == "success"),
        )
        return res

    async def run_manual_all(self) -> list[tuple[str, list[tuple[str, CheckinResult]]]]:
        accounts = tuple(self.pool.accounts.values())
        unfinished = CheckinResult("timeout", "总耗时超限")
        results = {acc.phone: [(t.name, unfinished) for t in TARGETS] for acc in accounts}

        async def _per_account(acc: Account):
            if len(accounts) > 1:
                await asyncio.sleep(self.rng.uniform(0, min(10.0, float(len(accounts)))))
            for idx, target in enumerate(TARGETS):
                results[acc.phone][idx] = (target.name, await self.run_one(acc, target))

        try:
            async with asyncio.timeout(MANUAL_TIMEOUT):
                await asyncio.gather(*(_per_account(acc) for acc in accounts))
        except TimeoutError:
            log.warning("手动批量签到超时")
        return list(results.items())

    async def _scheduler_loop(self, account: Account, target: TargetBot) -> None:
        while not self.pool.stopping:
            try:
                now = time.time()
                row = await self.db.init_checkin(
                    account.phone, target.key, now + self.rng.uniform(0, CHECKIN_MIN_INTERVAL)
                )
                delay = max(0.0, row["next_at"] - time.time())
                await asyncio.sleep(delay)
                res = await self.run_one(account, target)
                if res.status in ("unavailable", "skipped"):
                    wait_s = max(60.0, min(600.0, account.flood_until - time.time()))
                    await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("签到调度异常 phone=%s target=%s", account.phone, target.name, exc_info=exc)
                await asyncio.sleep(60)

    def ensure_scheduled(self, create_task) -> None:
        for acc in self.pool.accounts.values():
            for target in TARGETS:
                key = (acc.phone, target.key)
                if key not in self._tasks or self._tasks[key].done():
                    self._tasks[key] = create_task(
                        self._scheduler_loop(acc, target),
                        name=f"checkin:{acc.phone}:{target.name}",
                    )
