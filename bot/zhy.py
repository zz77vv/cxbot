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
    """中核验旧三要素服务：串行锁保护、按金币优先选号、不足自动换号。"""

    def __init__(self, cfg: Config, pool: UserbotPool, balances: BalanceService):
        self.cfg = cfg
        self.pool = pool
        self.balances = balances
        self.lock = asyncio.Lock()
        self._insufficient_until: dict[str, float] = {}

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

                self.balances.invalidate(account.phone, ZHONGHY.key)
                return ZHYResult(ok=True, text=final_text, phone=account.phone), False
        except AccountUnavailable:
            return None, True
        except FloodWaitError as exc:
            return ZHYResult(ok=False, reason=f"触发电报限流({exc.seconds}s)。"), False
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
    ) -> ZHYResult:
        """执行中核验旧三要素核验（自动选号，金币不足自动切换）。"""
        if self.lock.locked():
            return ZHYResult(ok=False, reason="已有旧三要素核验进行中，请稍候。")

        async with self.lock:
            online = [
                acc for acc in self.pool.accounts.values()
                if acc.state == AccountState.ONLINE and not acc.lock.locked()
            ]
            if not online:
                return ZHYResult(ok=False, reason="当前没有在线空闲的协议号。")

            candidates, all_insufficient = self._pick_candidates(online)
            if not candidates:
                msg = (
                    "所有在线协议号中核验金币不足（15 分钟内自动跳过；可发 /jf 刷新余额解除）。"
                    if all_insufficient
                    else "当前没有可用的在线协议号。"
                )
                return ZHYResult(ok=False, reason=msg)

            skipped_count = 0
            for acc in candidates:
                res, try_next = await self._verify_old_3ys_with_account(
                    acc, name, id_card, phone, on_progress
                )
                if res is not None:
                    return res
                if try_next:
                    skipped_count += 1
                    if on_progress:
                        await on_progress(f"⚠️ 尾号 {acc.phone[-4:]} 金币不足或暂忙，自动尝试下一个协议号…")

            if skipped_count:
                return ZHYResult(ok=False, reason="所有可用协议号中核验金币不足或暂不可用。")
            return ZHYResult(ok=False, reason="未能找到可用协议号执行旧三要素核验。")
