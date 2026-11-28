"""中核验 (@zhonghybot) 旧三要素核验服务：主菜单点选 → 原消息编辑输入 → 占位消息追踪。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from telethon.errors import FloodWaitError

from .balance import BalanceService, parse_balance_text
from .config import Config
from .db import CachedResult, Database, make_cache_key
from .targets import (
    ZHY_INSUFFICIENT_KEYWORDS,
    ZHY_MENU_TIMEOUT,
    ZHY_OLD_3YS_BUTTON,
    ZHY_OLD_3YS_COST,
    ZHY_PENDING_KEYWORDS,
    ZHY_RESULT_TIMEOUT,
    ZHONGHY,
)
from .userbots import (
    Account,
    AccountState,
    AccountUnavailable,
    UserbotPool,
    find_button,
    msg_signature,
    wait_reply,
)

log = logging.getLogger(__name__)

INSUFFICIENT_COOLDOWN = 900.0  # 金币不足的账号 15 分钟内跳过
ID_CARD_RE = re.compile(r"^(?:\d{15}|\d{17}[0-9X])$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ZHYResult:
    ok: bool
    text: str = ""
    reason: str = ""
    phone: str = ""
    fatal_batch: bool = False
    from_cache: bool = False


def parse_3ys_args(raw: str) -> tuple[tuple[str, str, str] | None, str]:
    """解析并校验 `/3ys 姓名 身份证 手机号` 参数。"""
    parts = (raw or "").split()
    if len(parts) != 3:
        return None, "用法：/3ys 姓名 身份证 手机号（以空格分隔，共 3 项）。"
    name, id_card, phone = parts[0].strip(), parts[1].strip().upper(), parts[2].strip()
    if not name:
        return None, "姓名不能为空。"
    if not ID_CARD_RE.fullmatch(id_card):
        return None, "身份证格式不正确（需 15 位或 18 位，末位 X 自动兼容大小写）。"
    if not PHONE_RE.fullmatch(phone):
        return None, "手机号格式不正确（需 11 位中国大陆手机号）。"
    return (name, id_card, phone), ""


def parse_3ys_batch(raw: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """解析多行 `/3ys` 输入，返回 (去重后合法条目, 错误行说明列表)。"""
    items: list[tuple[str, str, str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for line_no, line in enumerate((raw or "").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        parsed, err = parse_3ys_args(stripped)
        if not parsed:
            errors.append(f"第 {line_no} 行：{err}")
            continue
        if parsed not in seen:
            seen.add(parsed)
            items.append(parsed)
    return items, errors


def extract_zhy_conclusion(text: str) -> str:
    """剥离机器人回复中重复的 name/idcard/phone 行，提取紧凑结论。"""
    kept: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^(?:name|idcard|phone)\s*[:：]", line, re.I):
            continue
        kept.append(line)
    return " ｜ ".join(kept) if kept else (text or "").strip()


def classify_zhy_result(res: ZHYResult) -> str:
    """归类核验结论（纯文字判断）：match（一致）/ mismatch（不一致）/ error（出错或失败）。"""
    if not res.ok:
        return "error"
    conclusion = extract_zhy_conclusion(res.text)
    if any(k in conclusion for k in ("出错", "失败", "异常", "重试", "错误")):
        return "error"
    if "不一致" in conclusion:
        return "mismatch"
    if "一致" in conclusion:
        return "match"
    return "error"


_SUMMARY_ORDER = {"match": 0, "mismatch": 1, "error": 2}


def format_3ys_batch_summary(
    entries: list[tuple[tuple[str, str, str], ZHYResult]],
    *,
    total: int,
    stopped_reason: str = "",
) -> str:
    """将批量核验结果按「一致 → 不一致 → 出错/失败」分组排序并输出紧凑报告。"""
    match_cnt = mismatch_cnt = err_cnt = 0
    for _, r in entries:
        kind = classify_zhy_result(r)
        if kind == "match":
            match_cnt += 1
        elif kind == "mismatch":
            mismatch_cnt += 1
        else:
            err_cnt += 1

    ordered = sorted(entries, key=lambda item: _SUMMARY_ORDER[classify_zhy_result(item[1])])
    remain = max(0, total - len(entries))
    lines = [
        f"📋 旧三要素核验汇总（共 {total} 条）",
        f"🟢 一致：{match_cnt} ｜ 🔴 不一致：{mismatch_cnt} ｜ 🟡 出错/失败：{err_cnt}",
    ]
    if stopped_reason:
        lines.append(f"⚠️ 提前暂停（未执行 {remain} 条）：{stopped_reason}")

    blocks = ["\n".join(lines)]
    for idx, ((name, id_card, phone), res) in enumerate(ordered, 1):
        if res.ok:
            suffix = "（缓存）" if res.from_cache else ""
            detail = f"{extract_zhy_conclusion(res.text)}{suffix}"
        else:
            detail = f"❌ {res.reason}"
        blocks.append(f"{idx}. {name}  {id_card}  {phone}\n   └ {detail}")
    return "\n\n".join(blocks)


def is_zhy_insufficient(text: str) -> bool:
    return any(k in (text or "") for k in ZHY_INSUFFICIENT_KEYWORDS)


def is_zhy_pending(msg) -> bool:
    return any(k in (getattr(msg, "text", "") or "") for k in ZHY_PENDING_KEYWORDS)


def is_zhy_3ys_prompt(msg) -> bool:
    text = getattr(msg, "text", "") or ""
    if is_zhy_insufficient(text):
        return True
    if "发送" not in text and "身份证" not in text:
        return False
    return "旧三要素" in text or "获得金币" not in text


class ZHYService:
    """中核验旧三要素服务：结果缓存、多空闲号并发抢占、金币回写与不足自动换号。"""

    def __init__(self, cfg: Config, db: Database, pool: UserbotPool, balances: BalanceService):
        self.cfg = cfg
        self.db = db
        self.pool = pool
        self.balances = balances
        self._insufficient_until: dict[str, float] = {}

    @staticmethod
    def _cache_key(name: str, id_card: str, phone: str) -> str:
        return make_cache_key("zhy3ys", f"{name} {id_card} {phone}")

    async def _get_cached_3ys(self, name: str, id_card: str, phone: str) -> ZHYResult | None:
        if self.cfg.result_cache_ttl <= 0:
            return None
        hit = await self.db.get_cache(self._cache_key(name, id_card, phone))
        if hit and hit.text:
            return ZHYResult(ok=True, text=hit.text, from_cache=True)
        return None

    async def _save_cached_3ys(self, name: str, id_card: str, phone: str, res: ZHYResult) -> None:
        if self.cfg.result_cache_ttl <= 0 or res.from_cache or not res.ok:
            return
        if classify_zhy_result(res) not in ("match", "mismatch"):
            return
        await self.db.set_cache(
            self._cache_key(name, id_card, phone),
            CachedResult(result_type="text", text=res.text),
            self.cfg.result_cache_ttl,
        )

    def clear_insufficient(self, phone: str) -> None:
        self._insufficient_until.pop(phone, None)

    def _is_in_insufficient_cooldown(self, phone: str) -> bool:
        return self._insufficient_until.get(phone, 0.0) > time.monotonic()

    def _mark_insufficient(self, phone: str) -> None:
        self._insufficient_until[phone] = time.monotonic() + INSUFFICIENT_COOLDOWN
        self.balances.invalidate(phone, ZHONGHY.key)

    def _pick_candidates(self, online: list[Account]) -> tuple[list[Account], bool]:
        """筛选并按已知金币余额降序排序；返回 (候选列表, 是否全部因金币不足被跳过)。"""
        not_cooled = [acc for acc in online if not self._is_in_insufficient_cooldown(acc.phone)]
        if not not_cooled:
            return [], True

        known_rich: list[tuple[int, Account]] = []
        unknown: list[Account] = []
        skipped_low = 0
        for acc in not_cooled:
            cached = self.balances.peek_cached(acc.phone, ZHONGHY.key)
            if cached is None:
                unknown.append(acc)
            elif cached < ZHY_OLD_3YS_COST:
                skipped_low += 1
            else:
                known_rich.append((cached, acc))

        known_rich.sort(key=lambda item: item[0], reverse=True)
        ordered = [acc for _, acc in known_rich] + unknown
        return ordered, (not ordered and skipped_low > 0)

    async def _verify_old_3ys_with_account(
        self,
        account: Account,
        name: str,
        id_card: str,
        phone: str,
        on_progress: ProgressCallback | None,
    ) -> tuple[ZHYResult | None, bool]:
        """返回 (ZHYResult|None, should_try_next_account)。"""
        try:
            async with self.pool.hold_account(account, wait=False) as client:
                if on_progress:
                    await on_progress(f"🧭 正在打开中核验菜单（尾号 {account.phone[-4:]}）…")

                # 1. 发送 /start 获取主菜单并检查金币余额
                async with self.pool.rpc_slot():
                    sent_start = await asyncio.wait_for(
                        client.send_message(ZHONGHY.username, ZHONGHY.checkin_cmd),
                        self.cfg.reply_timeout,
                    )

                menu = await wait_reply(
                    client,
                    ZHONGHY.username,
                    sent_start.id,
                    lambda m: (
                        find_button(m, ZHY_OLD_3YS_BUTTON) is not None
                        or parse_balance_text(getattr(m, "text", ""), ZHONGHY) is not None
                    ),
                    ZHY_MENU_TIMEOUT,
                    rpc_timeout=self.cfg.reply_timeout,
                )
                if menu is None:
                    return ZHYResult(ok=False, reason="等待中核验主菜单超时。"), False

                coins = parse_balance_text(getattr(menu, "text", ""), ZHONGHY)
                if coins is not None and coins < ZHY_OLD_3YS_COST:
                    self._mark_insufficient(account.phone)
                    return None, True

                btn = find_button(menu, ZHY_OLD_3YS_BUTTON)
                if btn is None:
                    return ZHYResult(ok=False, reason="中核验主菜单中未找到「旧三要素核验」按钮。"), False

                # 2. 点击「旧三要素核验」按钮，等待原菜单编辑为输入提示
                old_sig = msg_signature(menu)
                async with self.pool.rpc_slot():
                    try:
                        cb = await asyncio.wait_for(menu.click(btn[0], btn[1]), self.cfg.reply_timeout)
                    except TimeoutError:
                        cb = None
                cb_text = getattr(cb, "message", "") or ""
                if is_zhy_insufficient(cb_text):
                    self._mark_insufficient(account.phone)
                    return None, True

                prompt_msg = await wait_reply(
                    client,
                    ZHONGHY.username,
                    menu.id,
                    is_zhy_3ys_prompt,
                    ZHY_MENU_TIMEOUT,
                    original=menu,
                    old_sig=old_sig,
                    rpc_timeout=self.cfg.reply_timeout,
                )
                if prompt_msg is None:
                    return ZHYResult(ok=False, reason="等待旧三要素输入提示超时。"), False
                if is_zhy_insufficient(getattr(prompt_msg, "text", "")):
                    self._mark_insufficient(account.phone)
                    return None, True

                # 3. 发送「姓名 身份证号 手机号」
                if on_progress:
                    await on_progress(f"📤 已发送旧三要素信息，等待核验（尾号 {account.phone[-4:]}）…")
                async with self.pool.rpc_slot():
                    sent_query = await asyncio.wait_for(
                        client.send_message(ZHONGHY.username, f"{name} {id_card} {phone}"),
                        self.cfg.reply_timeout,
                    )

                # 4. 等待占位消息或直接结果
                first = await wait_reply(
                    client,
                    ZHONGHY.username,
                    sent_query.id,
                    lambda m: bool((getattr(m, "text", "") or "").strip()),
                    ZHY_MENU_TIMEOUT,
                    rpc_timeout=self.cfg.reply_timeout,
                )
                if first is None:
                    return ZHYResult(ok=False, reason="等待中核验响应超时。"), False

                first_text = (getattr(first, "text", "") or "").strip()
                if is_zhy_insufficient(first_text):
                    self._mark_insufficient(account.phone)
                    return None, True

                final_msg = first
                if is_zhy_pending(first):
                    if on_progress:
                        await on_progress(f"🕒 旧三要素核验中，等待结果返回（尾号 {account.phone[-4:]}）…")
                    final_msg = await wait_reply(
                        client,
                        ZHONGHY.username,
                        first.id,
                        lambda m: bool((getattr(m, "text", "") or "").strip()) and not is_zhy_pending(m),
                        ZHY_RESULT_TIMEOUT,
                        original=first,
                        old_sig=msg_signature(first),
                        rpc_timeout=self.cfg.reply_timeout,
                    )
                    if final_msg is None:
                        return ZHYResult(ok=False, reason="等待旧三要素最终结果超时。"), False

                final_text = (getattr(final_msg, "text", "") or "").strip()
                if is_zhy_insufficient(final_text):
                    self._mark_insufficient(account.phone)
                    return None, True

                if coins is not None:
                    remain = max(0, coins - ZHY_OLD_3YS_COST)
                    if remain < ZHY_OLD_3YS_COST:
                        self._mark_insufficient(account.phone)
                    self.balances.set_cached(account.phone, ZHONGHY.key, remain)
                else:
                    self.balances.invalidate(account.phone, ZHONGHY.key)
                return ZHYResult(ok=True, text=final_text, phone=account.phone), False
        except AccountUnavailable:
            return None, True
        except FloodWaitError as exc:
            return ZHYResult(ok=False, reason=f"触发电报限流({exc.seconds}s)。", fatal_batch=True), False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("中核验旧三要素异常 phone=%s", account.phone, exc_info=exc)
            return ZHYResult(ok=False, reason=f"核验异常({type(exc).__name__})。"), False

    async def verify_old_3ys(
        self,
        name: str,
        id_card: str,
        phone: str,
        *,
        on_progress: ProgressCallback | None = None,
        wait_for_account: bool = True,
        stop_event: asyncio.Event | None = None,
        use_cache: bool = True,
    ) -> ZHYResult:
        """执行中核验旧三要素核验（支持缓存命中与多协程并发抢占空闲协议号）。"""
        if use_cache:
            cached = await self._get_cached_3ys(name, id_card, phone)
            if cached is not None:
                return cached

        deadline = time.monotonic() + 90.0
        while True:
            if stop_event and stop_event.is_set():
                return ZHYResult(ok=False, reason="批量任务已停止。")

            all_online = [
                acc for acc in self.pool.accounts.values()
                if acc.state == AccountState.ONLINE
            ]
            if not all_online:
                return ZHYResult(ok=False, reason="当前没有在线的协议号。", fatal_batch=True)

            eligible, all_insufficient = self._pick_candidates(all_online)
            if not eligible:
                msg = (
                    "所有在线协议号中核验金币不足（15 分钟内自动跳过；可发 /jf 刷新余额解除）。"
                    if all_insufficient
                    else "当前没有可用的在线协议号。"
                )
                return ZHYResult(ok=False, reason=msg, fatal_batch=True)

            idle = [acc for acc in eligible if not acc.lock.locked()]
            if idle:
                skipped_count = 0
                for acc in idle:
                    if stop_event and stop_event.is_set():
                        return ZHYResult(ok=False, reason="批量任务已停止。")
                    res, try_next = await self._verify_old_3ys_with_account(
                        acc, name, id_card, phone, on_progress
                    )
                    if res is not None:
                        await self._save_cached_3ys(name, id_card, phone, res)
                        return res
                    if try_next:
                        skipped_count += 1
                        if on_progress:
                            await on_progress(f"⚠️ 尾号 {acc.phone[-4:]} 金币不足或暂忙，自动尝试下一个协议号…")
                # 若只是部分号在忙，检查是否还有其他可用号可等
                still_eligible, _ = self._pick_candidates([
                    acc for acc in self.pool.accounts.values()
                    if acc.state == AccountState.ONLINE
                ])
                if not still_eligible:
                    return ZHYResult(
                        ok=False,
                        reason="所有可用协议号中核验金币不足或暂不可用。",
                        fatal_batch=True,
                    )
                if not wait_for_account or time.monotonic() >= deadline:
                    return ZHYResult(
                        ok=False,
                        reason="所有可用协议号暂忙或金币不足。" if skipped_count else "未能找到可用协议号。",
                        fatal_batch=not still_eligible,
                    )
            else:
                if not wait_for_account or time.monotonic() >= deadline:
                    return ZHYResult(ok=False, reason="所有可用协议号均在忙碌中，请稍候再试。")

            await asyncio.sleep(0.25)

    async def verify_old_3ys_batch(
        self,
        items: list[tuple[str, str, str]],
        *,
        on_progress: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
        collected: dict[int, ZHYResult] | None = None,
    ) -> tuple[list[tuple[tuple[str, str, str], ZHYResult]], str]:
        """多空闲协议号并发批量核验（先查缓存，仅未命中项调度协议号）。"""
        if not items:
            return [], ""

        results = collected if collected is not None else {}
        counts = {"match": 0, "mismatch": 0, "error": 0}
        pending_items: list[tuple[int, tuple[str, str, str]]] = []
        for idx, item in enumerate(items):
            cached = await self._get_cached_3ys(*item)
            if cached is not None:
                results[idx] = cached
                counts[classify_zhy_result(cached)] += 1
            else:
                pending_items.append((idx, item))

        if not pending_items:
            return [(items[i], results[i]) for i in sorted(results)], ""

        all_online = [
            acc for acc in self.pool.accounts.values()
            if acc.state == AccountState.ONLINE
        ]
        eligible, all_insufficient = self._pick_candidates(all_online)
        if not eligible:
            reason = (
                "所有在线协议号中核验金币不足（15 分钟内自动跳过；可发 /jf 刷新余额解除）。"
                if all_insufficient
                else "当前没有可用的在线协议号。"
            )
            return [(items[i], results[i]) for i in sorted(results)], reason

        worker_count = min(len(pending_items), len(eligible), max(1, self.cfg.max_concurrency))
        queue: asyncio.Queue[tuple[int, tuple[str, str, str]]] = asyncio.Queue()
        for pair in pending_items:
            queue.put_nowait(pair)

        stop_event = asyncio.Event()
        stopped_reason = ""

        if on_progress:
            await on_progress(
                f"🚀 已启用 {worker_count} 路并发核验：{len(results)}/{len(items)}…"
            )

        async def _worker():
            nonlocal stopped_reason
            while not stop_event.is_set():
                if should_stop and should_stop():
                    stopped_reason = stopped_reason or "程序正在关闭"
                    stop_event.set()
                    break
                try:
                    idx, (name, id_card, phone) = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    res = await self.verify_old_3ys(
                        name, id_card, phone, stop_event=stop_event, use_cache=False
                    )
                    if stop_event.is_set() and not res.ok and res.reason == "批量任务已停止。":
                        break
                    await self._save_cached_3ys(name, id_card, phone, res)
                    results[idx] = res
                    counts[classify_zhy_result(res)] += 1
                    if not res.ok and res.fatal_batch and not stopped_reason:
                        stopped_reason = res.reason
                        stop_event.set()
                    if on_progress:
                        done = len(results)
                        await on_progress(
                            f"⚡ {worker_count} 路并发核验中：{done}/{len(items)}"
                            f"（🟢 {counts['match']} ｜ 🔴 {counts['mismatch']} ｜ 🟡 {counts['error']}）"
                        )
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(_worker()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            stop_event.set()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        ordered = [(items[i], results[i]) for i in sorted(results)]
        return ordered, stopped_reason
