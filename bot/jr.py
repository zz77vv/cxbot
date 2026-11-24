"""巨人 (@jrsgk) 查询全流程：缓存 → 选号/冷却 → 自动OCR+人工验证 → 购买 → 导出优先+翻页降级。"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon.errors import FloodWaitError
from telethon.tl.types import KeyboardButtonCallback

from .balance import BalanceService
from .config import Config
from .db import CachedResult, Database, make_cache_key
from .ocr import OCRError, OCRService, extract_captcha_candidates
from .targets import (
    JR,
    JR_BUY_AMOUNTS,
    JR_EXPORT_KEYWORDS,
    JR_EXPORT_SUCCESS_KEYWORDS,
    JR_FREE_COOLDOWN_SECONDS,
    JR_FREE_DAILY_LIMIT,
    JR_FREE_EXPORT_KEYWORDS,
    JR_MAX_PAGES,
    JR_NEXT_PAGE_KEYWORDS,
    JR_PAGE_INTERVAL,
    JR_PAGE_TIMEOUT,
    JR_PENDING_KEYWORDS,
    JR_VERIFY_KEYWORDS,
    OCR_MAX_ATTEMPTS,
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

JR_PAGE_RE = re.compile(r"(?:第\s*)?(\d+)\s*(?:页\s*[,，、;；/]?\s*共\s*|[/／]\s*(?:共\s*)?)(\d+)\s*页?", re.I)
JR_HEADER_RE = re.compile(
    r"^\s*🔍[\ufe0e\ufe0f]?\s*共\s*(\d+)\s*条\s*信息"
    r"(?:\s*[(（]?\s*" + JR_PAGE_RE.pattern + r"\s*[)）]?)?\s*[:：]?\s*$",
    re.I,
)
_PAGE_LINE_RE = re.compile(r"^\s*[(（]?\s*" + JR_PAGE_RE.pattern + r"\s*[)）]?\s*[:：]?\s*$", re.I)
_COOLDOWN_RE = re.compile(r"查询过于频繁[，,]\s*请\s*(\d{1,6})\s*秒后再试")
ACCOUNT_WAIT_TIMEOUT = 180.0

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class JRResult:
    ok: bool
    kind: str = "text"  # text | file | empty | error
    text: str = ""
    file_path: str | None = None
    file_name: str | None = None
    reason: str = ""
    from_cache: bool = False
    partial: bool = False


# ---------------- 纯函数解析器 ----------------

def _first_line(msg) -> str:
    return next((ln.strip() for ln in (getattr(msg, "text", "") or "").splitlines() if ln.strip()), "")


def parse_header(msg) -> tuple[int, int | None, int | None] | None:
    m = JR_HEADER_RE.fullmatch(unicodedata.normalize("NFKC", _first_line(msg)))
    if not m:
        return None
    count, cur, tot = m.groups()
    return int(count), (int(cur) if cur else None), (int(tot) if tot else None)


def is_pending_message(msg) -> bool:
    text = getattr(msg, "text", "") or ""
    return any(k in text for k in JR_PENDING_KEYWORDS)


def parse_cooldown_seconds(msg) -> int | None:
    m = _COOLDOWN_RE.search(getattr(msg, "text", "") or "")
    return max(1, int(m.group(1))) if m else None


def find_buy_button(msg) -> tuple[int, int, str] | None:
    generic = None
    for r, row in enumerate(getattr(msg, "buttons", None) or []):
        for c, btn in enumerate(row):
            label = getattr(btn, "text", "") or ""
            if "购买" in label:
                generic = generic or (r, c, label)
                if any(int(n) in JR_BUY_AMOUNTS for n in re.findall(r"\d+", label)):
                    return r, c, label
    return generic


def find_next_page_button(msg) -> tuple[int, int, str] | None:
    for r, row in enumerate(getattr(msg, "buttons", None) or []):
        for c, btn in enumerate(row):
            if (getattr(btn, "text", "") or "") == "下一页":
                return r, c, "下一页"
    return find_button(msg, JR_NEXT_PAGE_KEYWORDS)


def is_verification_message(msg) -> bool:
    text = getattr(msg, "text", "") or ""
    if "验证通过" in text:
        return False
    if (
        parse_header(msg)
        or find_buy_button(msg)
        or find_next_page_button(msg)
        or find_button(msg, JR_EXPORT_KEYWORDS)
    ):
        return False
    if any(k in text for k in JR_VERIFY_KEYWORDS):
        return True
    return bool(getattr(msg, "media", None) and getattr(msg, "buttons", None))


def parse_page_info(msg) -> tuple[int, int] | None:
    hdr = parse_header(msg)
    if hdr and hdr[1] is not None and hdr[2] is not None:
        return hdr[1], hdr[2]
    m = _PAGE_LINE_RE.fullmatch(unicodedata.normalize("NFKC", _first_line(msg)))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def clean_page_text(msg) -> str:
    lines = (getattr(msg, "text", "") or "").strip().splitlines()
    if not lines:
        return ""
    hdr = parse_header(msg)
    if hdr:
        lines[0] = f"🔍共{hdr[0]}条信息"
    elif _PAGE_LINE_RE.fullmatch(unicodedata.normalize("NFKC", lines[0])):
        lines.pop(0)
    return "\n".join(lines).strip()


def normalize_full_text(text: str) -> str:
    lines: list[str] = []
    seen_header = False
    prev_blank = False
    for raw in (text or "").replace("`", "").splitlines():
        line = raw.rstrip()
        m = JR_HEADER_RE.fullmatch(unicodedata.normalize("NFKC", line.strip()))
        if m:
            if seen_header:
                continue
            seen_header = True
            line = f"🔍共{int(m.group(1))}条信息"
        if not line.strip():
            if lines and not prev_blank:
                lines.append("")
            prev_blank = True
            continue
        lines.append(line)
        prev_blank = False
    return "\n".join(lines).strip()


def classify_message(msg) -> str:
    if parse_cooldown_seconds(msg) is not None:
        return "cooldown"
    if is_verification_message(msg):
        return "manual"
    if is_pending_message(msg):
        return "pending"
    if getattr(msg, "document", None):
        return "result"
    text = getattr(msg, "text", "") or ""
    if any(k in text for k in ("积分不足", "余额不足", "查询失败")):
        return "failed"
    if any(k in text for k in ("未查询到", "未找到", "无查询结果")):
        return "result"
    if (
        find_buy_button(msg)
        or parse_page_info(msg)
        or find_next_page_button(msg)
        or find_button(msg, JR_EXPORT_KEYWORDS)
        or parse_header(msg)
    ):
        return "result"
    return "unknown"


def is_basic_info_only(msg, query: str) -> bool:
    if any(getattr(msg, attr, None) for attr in ("buttons", "media", "document", "file")):
        return False
    q = query.strip().upper()
    if not re.fullmatch(r"[0-9]{17}[0-9X]", q):
        return False
    labels = ("证件", "出生", "性别", "生日", "年龄", "生肖")
    lines = [ln for ln in (getattr(msg, "text", "") or "").splitlines() if ln.strip()]
    if len(lines) != len(labels):
        return False
    for expected, line in zip(labels, lines):
        name, sep, val = line.replace("：", ":").partition(":")
        if not sep or name.strip() != expected or ":" in val:
            return False
        if expected == "证件" and val.strip().upper() != q:
            return False
    return True


def safe_filename(msg, default_prefix: str = "巨人导出") -> str:
    raw = (getattr(getattr(msg, "file", None), "name", "") or "").strip()
    raw = re.split(r"[\\/]", raw)[-1].strip()
    cleaned = re.sub(r"[\x00-\x1f]", "_", raw)
    return cleaned or f"{default_prefix}_{msg.id}.txt"


# ---------------- 人工验证回调管理器 ----------------

class ManualVerifyManager:
    """将图片验证码转发给管理员私聊点选（等待期间不占全局 RPC 信号量）。"""

    def __init__(self, cfg: Config, bot=None):
        self.cfg = cfg
        self.bot = bot
        self._pending: dict[str, tuple[asyncio.Future[int], int]] = {}

    async def handle_callback(self, update, context) -> None:
        query = update.callback_query
        chat = update.effective_chat
        user = update.effective_user
        if not user or user.id != self.cfg.admin_id or not chat or chat.type != "private":
            await query.answer("仅限管理员私聊操作。", show_alert=True)
            return
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[0] != "jrverify":
            await query.answer("按钮格式无效。", show_alert=True)
            return
        token, raw_idx = parts[1], parts[2]
        entry = self._pending.get(token)
        if not entry or not raw_idx.isdigit():
            await query.answer("验证已过期或已处理。", show_alert=True)
            return
        fut, count = entry
        idx = int(raw_idx)
        if fut.done() or not 0 <= idx < count:
            await query.answer("选项已失效。", show_alert=True)
            return
        fut.set_result(idx)
        await query.answer("已收到选择，正在提交…")

    async def ask_admin(self, phone: str, image_bytes: bytes, labels: list[str], timeout: float = 120.0) -> int:
        if self.bot is None:
            raise RuntimeError("bot_not_ready")
        token = secrets.token_urlsafe(12)
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._pending[token] = (fut, len(labels))
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"jrverify:{token}:{i}")]
            for i, label in enumerate(labels)
        ])
        caption = f"🔐 巨人安全验证（协议号尾号 {phone[-4:]}）\n自动识别未通过，请按原图点击对应选项（限时 {int(timeout)} 秒）："
        prompt = None
        try:
            prompt = await asyncio.wait_for(
                self.bot.send_photo(self.cfg.admin_id, image_bytes, caption=caption, reply_markup=keyboard),
                self.cfg.reply_timeout,
            )
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(token, None)
            if prompt is not None:
                try:
                    await asyncio.wait_for(prompt.delete(), 5)
                except Exception:
                    try:
                        await asyncio.wait_for(prompt.edit_reply_markup(reply_markup=None), 3)
                    except Exception:
                        pass


# ---------------- 巨人查询主服务 ----------------

class JRService:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        pool: UserbotPool,
        balances: BalanceService,
        ocr: OCRService,
        manual_verify: ManualVerifyManager,
    ):
        self.cfg = cfg
        self.db = db
        self.pool = pool
        self.balances = balances
        self.ocr = ocr
        self.manual_verify = manual_verify
        self._query_locks: dict[str, asyncio.Lock] = {}

    def _get_query_lock(self, key: str) -> asyncio.Lock:
        lock = self._query_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._query_locks[key] = lock
        return lock

    async def _save_text_file(self, text: str, filename: str) -> Path:
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        out_dir = self.cfg.cache_dir / uuid.uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=False)
        path = out_dir / "result.txt"
        await asyncio.to_thread(path.write_bytes, text.encode("utf-8"))
        return path

    async def _download_document(self, client, message) -> tuple[Path, str]:
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        out_dir = self.cfg.cache_dir / uuid.uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=False)
        target = out_dir / "result.bin"
        name = safe_filename(message)
        async with self.pool.rpc_slot():
            downloaded = await asyncio.wait_for(
                client.download_media(message, file=str(target)),
                self.cfg.export_timeout,
            )
        actual = Path(downloaded or target).resolve()
        if actual != target.resolve() or not actual.is_file():
            raise FileNotFoundError("下载结果文件失败")
        return actual, name

    async def _pick_account(self, on_progress: ProgressCallback | None) -> tuple[Account | None, str]:
        """按积分降序选号；若全在冷却/忙碌则等待最早到期（最多 180 秒）。"""
        deadline = time.monotonic() + ACCOUNT_WAIT_TIMEOUT
        while True:
            if self.pool.any_flood_active():
                return None, "存在账号触发电报限流，已暂停巨人调度，请稍后再试。"

            accounts = tuple(self.pool.accounts.values())
            if not accounts:
                return None, "尚未登录任何协议号，请先使用 /login 登录。"

            meta_map = await self.db.all_account_meta()
            now = time.time()
            candidates: list[Account] = []
            earliest_retry = 0.0
            reasons: set[str] = set()

            for acc in accounts:
                if acc.state != AccountState.ONLINE:
                    reasons.add("离线/未授权")
                    continue
                meta = meta_map.get(acc.phone, {})
                membership = meta.get("jr_membership", "unknown")
                if membership != "member":
                    used = await self.db.get_jr_daily_usage(acc.phone)
                    if used >= JR_FREE_DAILY_LIMIT:
                        reasons.add("免费号今日额度已满")
                        continue
                cd = meta.get("jr_cooldown_until", 0.0)
                if cd > now:
                    earliest_retry = min(earliest_retry, cd) if earliest_retry else cd
                    reasons.add("冷却中")
                    continue
                if acc.lock.locked():
                    retry = now + 1.5
                    earliest_retry = min(earliest_retry, retry) if earliest_retry else retry
                    reasons.add("忙碌中")
                    continue
                candidates.append(acc)

            if candidates:
                bal_results = await asyncio.gather(
                    *(self.balances.query(acc, JR, use_cache=True) for acc in candidates)
                )
                if any(r.rate_limited for r in bal_results):
                    return None, "查询积分时触发电报限流，已停止调度。"
                ranked = sorted(
                    ((acc, r.value) for acc, r in zip(candidates, bal_results) if r.ok and r.value is not None),
                    key=lambda item: -item[1],
                )
                if ranked:
                    return ranked[0][0], ""
                reasons.add("积分查询失败")

            if earliest_retry and time.monotonic() < deadline:
                wait_s = max(1.0, min(earliest_retry - time.time(), deadline - time.monotonic(), 30.0))
                if on_progress:
                    await on_progress(f"⏳ 协议号暂忙或冷却中，约 {math.ceil(wait_s)} 秒后自动重试选号…")
                await asyncio.sleep(wait_s)
                continue

            detail = "、".join(sorted(reasons)) or "无可用账号"
            return None, f"暂无可用协议号（{detail}）。"

    async def _solve_verification(
        self, client, account: Account, current_msg, original_query: str, on_progress: ProgressCallback | None
    ):
        """先尝试自动 OCR（最多 3 次），失败后无缝转管理员私聊人工点选。"""
        msg = current_msg
        resubmitted = False

        # 阶段 1：自动 OCR
        for attempt in range(1, OCR_MAX_ATTEMPTS + 1):
            cands = extract_captcha_candidates(msg)
            if not cands or not getattr(msg, "media", None):
                break
            try:
                async with self.pool.rpc_slot():
                    img_bytes = await asyncio.wait_for(
                        client.download_media(msg, file=bytes), self.cfg.reply_timeout
                    )
                if not isinstance(img_bytes, bytes) or not img_bytes:
                    break
                btn_idx, _, _ = await self.ocr.recognize_captcha(img_bytes, [c.normalized for c in cands])
                chosen = cands[btn_idx]
                old_sig = msg_signature(msg)
                async with self.pool.rpc_slot():
                    cb = await asyncio.wait_for(msg.click(chosen.row, chosen.col), self.cfg.reply_timeout)
                    passed = "验证通过" in (getattr(cb, "message", "") or "")
                    reply = await wait_reply(
                        client,
                        JR.username,
                        msg.id,
                        lambda m: not is_pending_message(m),
                        self.cfg.reply_timeout,
                        original=msg,
                        old_sig=old_sig,
                        rpc_timeout=self.cfg.reply_timeout,
                    )
                passed = passed or bool(reply and "验证通过" in (getattr(reply, "text", "") or ""))
                if passed and not resubmitted:
                    async with self.pool.rpc_slot():
                        sent = await asyncio.wait_for(
                            client.send_message(JR.username, original_query), self.cfg.reply_timeout
                        )
                        resubmitted = True
                        reply = await wait_reply(
                            client,
                            JR.username,
                            sent.id,
                            lambda m: not is_pending_message(m),
                            self.cfg.reply_timeout,
                            rpc_timeout=self.cfg.reply_timeout,
                        )
                    return reply, sent.id
                if reply is None:
                    break
                if classify_message(reply) == "manual":
                    msg = reply
                    continue
                return reply, msg.id
            except OCRError as exc:
                log.info("自动 OCR 未通过 attempt=%d code=%s，准备回退人工验证", attempt, exc.code)
                break
            except Exception as exc:
                log.warning("自动验证过程异常，回退人工验证", exc_info=exc)
                break

        # 阶段 2：回退管理员私聊人工点选（不持有 rpc_sem！）
        cands = [
            c
            for c in extract_captcha_candidates(msg)
            if isinstance(msg.buttons[c.row][c.col].button, KeyboardButtonCallback)
            and not getattr(msg.buttons[c.row][c.col].button, "requires_password", False)
        ]
        if not cands or not getattr(msg, "media", None):
            return None, msg.id

        if on_progress:
            await on_progress("🔐 自动识别未通过，已发送验证码图片到私聊，请在 2 分钟内点击选择…")

        async with self.pool.rpc_slot():
            img_bytes = await asyncio.wait_for(
                client.download_media(msg, file=bytes), self.cfg.reply_timeout
            )
        if not img_bytes:
            return None, msg.id

        old_sig = msg_signature(msg)
        # 注意：此处等待管理员点击，完全不占用 pool.rpc_sem
        selected = await self.manual_verify.ask_admin(account.phone, img_bytes, [c.label for c in cands])
        chosen = cands[selected]

        async with self.pool.rpc_slot():
            cb = await asyncio.wait_for(msg.click(chosen.row, chosen.col), self.cfg.reply_timeout)
            passed = "验证通过" in (getattr(cb, "message", "") or "")
            if not passed:
                reply = await wait_reply(
                    client,
                    JR.username,
                    msg.id,
                    lambda m: not is_pending_message(m),
                    self.cfg.reply_timeout,
                    original=msg,
                    old_sig=old_sig,
                    rpc_timeout=self.cfg.reply_timeout,
                )
                passed = bool(reply and "验证通过" in (getattr(reply, "text", "") or ""))
                if not passed:
                    return reply, msg.id

            sent = await asyncio.wait_for(
                client.send_message(JR.username, original_query), self.cfg.reply_timeout
            )
            reply = await wait_reply(
                client,
                JR.username,
                sent.id,
                lambda m: not is_pending_message(m),
                self.cfg.reply_timeout,
                rpc_timeout=self.cfg.reply_timeout,
            )
            return reply, sent.id

    async def _try_export(self, client, account: Account, reply_msg, after_id: int) -> tuple[JRResult | None, str]:
        """优先尝试导出文件，并顺便识别会员/免费身份。返回 (JRResult|None, membership_detected)。"""
        if getattr(reply_msg, "document", None):
            path, name = await self._download_document(client, reply_msg)
            await self.db.set_jr_membership(account.phone, "member")
            return JRResult(ok=True, kind="file", file_path=str(path), file_name=name), "member"

        btn = find_button(reply_msg, JR_EXPORT_KEYWORDS)
        if not btn:
            return None, "unknown"

        r, c, _ = btn
        async with self.pool.rpc_slot():
            before = await asyncio.wait_for(
                client.get_messages(JR.username, limit=1), self.cfg.reply_timeout
            )
            baseline = max([reply_msg.id, after_id, *(m.id for m in before)])
            cb = await asyncio.wait_for(reply_msg.click(r, c), self.cfg.reply_timeout)

        feedback = getattr(cb, "message", "") or ""
        if any(k in feedback for k in JR_FREE_EXPORT_KEYWORDS):
            await self.db.set_jr_membership(account.phone, "free")
            return None, "free"

        def _export_reply(m) -> bool:
            text = getattr(m, "text", "") or ""
            if any(k in text for k in JR_FREE_EXPORT_KEYWORDS):
                return True
            return bool(getattr(m, "document", None) or (getattr(m, "file", None) and getattr(m, "media", None)))

        async with self.pool.rpc_slot():
            cand = await wait_reply(
                client,
                JR.username,
                baseline,
                _export_reply,
                self.cfg.export_timeout,
                rpc_timeout=self.cfg.reply_timeout,
            )
        if cand is None:
            return None, "unknown"
        if any(k in (getattr(cand, "text", "") or "") for k in JR_FREE_EXPORT_KEYWORDS):
            await self.db.set_jr_membership(account.phone, "free")
            return None, "free"

        try:
            path, name = await self._download_document(client, cand)
            await self.db.set_jr_membership(account.phone, "member")
            return JRResult(ok=True, kind="file", file_path=str(path), file_name=name), "member"
        except Exception as exc:
            log.warning("导出文件下载失败，降级本地翻页", exc_info=exc)
            return None, "member"

    async def _collect_pages(
        self, client, first_msg, on_progress: ProgressCallback | None
    ) -> tuple[str, int, bool, bool]:
        """本地翻页收集（最多 200 页），返回 (merged_text, page_count, is_paginated, timed_out)。"""
        pages: list[str] = []
        seen_sigs: set[tuple] = set()
        current = first_msg
        first_info = parse_page_info(first_msg)
        paginated = bool((first_info and first_info[1] > 1) or find_next_page_button(first_msg))
        timed_out = False

        for page_idx in range(1, JR_MAX_PAGES + 1):
            sig = (parse_page_info(current), clean_page_text(current))
            if sig in seen_sigs:
                break
            seen_sigs.add(sig)
            pages.append(clean_page_text(current))

            info = parse_page_info(current)
            if on_progress and paginated:
                if info:
                    await on_progress(f"📄 正在收集结果：第 {info[0]}/{info[1]} 页…")
                else:
                    await on_progress(f"📄 正在收集结果：第 {page_idx} 页…")

            if info and info[0] >= info[1]:
                break
            nxt = find_next_page_button(current)
            if not nxt or page_idx == JR_MAX_PAGES:
                break

            r, c, _ = nxt
            old_sig = msg_signature(current)
            try:
                async with self.pool.rpc_slot():
                    await asyncio.wait_for(current.click(r, c), self.cfg.reply_timeout)
                    changed = await wait_reply(
                        client,
                        JR.username,
                        current.id,
                        lambda m: not is_pending_message(m),
                        JR_PAGE_TIMEOUT,
                        original=current,
                        old_sig=old_sig,
                        rpc_timeout=self.cfg.reply_timeout,
                    )
            except TimeoutError:
                changed = None

            if changed is None:
                timed_out = True
                break
            if classify_message(changed) != "result":
                timed_out = True
                break
            current = changed
            await asyncio.sleep(JR_PAGE_INTERVAL)

        merged = normalize_full_text("\n\n".join(p for p in pages if p))
        return merged, len(pages), paginated, timed_out

    async def query(self, text: str, *, on_progress: ProgressCallback | None = None) -> JRResult:
        text = (text or "").strip()
        if not text:
            return JRResult(ok=False, kind="error", reason="查询内容不能为空。")

        cache_key = make_cache_key("jr", text)
        qlock = self._get_query_lock(cache_key)
        async with qlock:
            # 1. 查缓存
            cached = await self.db.get_cache(cache_key)
            if cached is not None:
                if cached.result_type == "file":
                    if cached.file_path and Path(cached.file_path).is_file():
                        return JRResult(
                            ok=True,
                            kind="file",
                            file_path=cached.file_path,
                            file_name=cached.file_name or "巨人查询结果.txt",
                            from_cache=True,
                        )
                elif cached.text:
                    return JRResult(ok=True, kind="text", text=cached.text, from_cache=True)

            # 2. 选号并执行（若遇冷却拒绝则自动换号重试）
            while True:
                if on_progress:
                    await on_progress("🔎 正在选择可用协议号…")
                account, err = await self._pick_account(on_progress)
                if account is None:
                    return JRResult(ok=False, kind="error", reason=err)

                try:
                    async with self.pool.hold_account(account, wait=False) as client:
                        meta = await self.db.get_account_meta(account.phone)
                        if meta["jr_membership"] != "member":
                            await self.db.set_jr_cooldown(
                                account.phone, time.time() + JR_FREE_COOLDOWN_SECONDS
                            )

                        if on_progress:
                            await on_progress(f"📤 正在向巨人提交查询（尾号 {account.phone[-4:]}）…")
                        async with self.pool.rpc_slot():
                            sent = await asyncio.wait_for(
                                client.send_message(JR.username, text), self.cfg.reply_timeout
                            )
                            after_id = sent.id
                            reply = await wait_reply(
                                client,
                                JR.username,
                                after_id,
                                lambda m: not is_pending_message(m),
                                self.cfg.reply_timeout,
                                rpc_timeout=self.cfg.reply_timeout,
                            )

                        if reply is None:
                            return JRResult(ok=False, kind="error", reason="等待巨人回复超时，请稍后再试。")

                        kind = classify_message(reply)
                        if kind == "manual":
                            if on_progress:
                                await on_progress("🔐 遇到安全验证，正在自动识别…")
                            try:
                                reply, after_id = await self._solve_verification(
                                    client, account, reply, text, on_progress
                                )
                            except TimeoutError:
                                return JRResult(ok=False, kind="error", reason="人工验证超时，本次查询已取消。")
                            if reply is None:
                                return JRResult(ok=False, kind="error", reason="安全验证未完成，本次查询已取消。")
                            kind = classify_message(reply)

                        if kind == "cooldown":
                            cd_sec = parse_cooldown_seconds(reply) or JR_FREE_COOLDOWN_SECONDS
                            await self.db.set_jr_cooldown(
                                account.phone, time.time() + cd_sec, force_free=True
                            )
                            if on_progress:
                                await on_progress(f"⏳ 尾号 {account.phone[-4:]} 触发频率限制({cd_sec}s)，自动切换其他账号…")
                            continue

                        if kind == "failed":
                            self.balances.invalidate(account.phone, JR.key)
                            return JRResult(ok=False, kind="error", reason="巨人返回查询失败或积分不足。")

                        if kind != "result":
                            if is_basic_info_only(reply, text):
                                return JRResult(ok=True, kind="empty", text="查询空")
                            return JRResult(ok=False, kind="error", reason="无法识别巨人回复内容，请稍后重试。")

                        # 3. 付费购买确认
                        buy = find_buy_button(reply)
                        if buy:
                            if on_progress:
                                await on_progress(f"💳 正在确认购买（{buy[2]}）…")
                            old_sig = msg_signature(reply)
                            async with self.pool.rpc_slot():
                                await asyncio.wait_for(reply.click(buy[0], buy[1]), self.cfg.reply_timeout)
                                reply = await wait_reply(
                                    client,
                                    JR.username,
                                    reply.id,
                                    lambda m: not is_pending_message(m),
                                    JR_PAGE_TIMEOUT,
                                    original=reply,
                                    old_sig=old_sig,
                                    rpc_timeout=self.cfg.reply_timeout,
                                )
                            if reply is None or classify_message(reply) != "result" or find_buy_button(reply):
                                return JRResult(ok=False, kind="error", reason="购买后未收到确认结果，请核对上游记录。")

                        # 4. 优先尝试「导出数据」，失败或免费号无缝降级本地翻页
                        if on_progress:
                            await on_progress("📥 正在获取完整结果（优先尝试导出）…")
                        exported, _ = await self._try_export(client, account, reply, after_id)
                        if exported is not None:
                            await self.db.inc_jr_daily_usage(account.phone)
                            self.balances.invalidate(account.phone, JR.key)
                            await self.db.set_cache(
                                cache_key,
                                CachedResult("file", file_path=exported.file_path, file_name=exported.file_name),
                                self.cfg.result_cache_ttl,
                            )
                            return exported

                        merged, page_count, paginated, timed_out = await self._collect_pages(
                            client, reply, on_progress
                        )
                        if not merged:
                            return JRResult(ok=False, kind="error", reason="未收集到有效查询结果。")

                        await self.db.inc_jr_daily_usage(account.phone)
                        self.balances.invalidate(account.phone, JR.key)

                        # 多页、超长或部分超时结果导出为 TXT 文件
                        need_file = paginated or timed_out or len(merged) > 3500
                        if need_file:
                            fname = "巨人查询部分结果.txt" if timed_out else "巨人查询结果.txt"
                            body = (
                                f"部分结果（翻页超时，已收集 {page_count} 页）\n\n{merged}"
                                if timed_out
                                else merged
                            )
                            path = await self._save_text_file(body, fname)
                            res = JRResult(
                                ok=True,
                                kind="file",
                                file_path=str(path),
                                file_name=fname,
                                partial=timed_out,
                            )
                            if not timed_out:
                                await self.db.set_cache(
                                    cache_key,
                                    CachedResult("file", file_path=str(path), file_name=fname),
                                    self.cfg.result_cache_ttl,
                                )
                            return res

                        await self.db.set_cache(
                            cache_key,
                            CachedResult("text", text=merged),
                            self.cfg.result_cache_ttl,
                        )
                        return JRResult(ok=True, kind="text", text=merged)

                except AccountUnavailable:
                    continue
                except FloodWaitError as exc:
                    return JRResult(ok=False, kind="error", reason=f"触发电报限流，请等待 {exc.seconds} 秒后再试。")
                except Exception as exc:
                    log.error("巨人查询异常", exc_info=exc)
                    return JRResult(ok=False, kind="error", reason=f"查询发生异常（{type(exc).__name__}）。")
