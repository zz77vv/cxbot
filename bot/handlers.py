"""Telegram Bot 命令与回调路由（单管理员守卫 + 节流进度编辑 + 在途请求追踪）。"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from pathlib import Path

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .targets import JR_FREE_DAILY_LIMIT, TARGETS
from .utils import ProgressEditor, split_text
from .zhy import ZHYResult, classify_zhy_result, format_3ys_batch_summary, parse_3ys_batch

log = logging.getLogger(__name__)

JR_ROUTE_RE = re.compile(r"^/jr(?:@\w+)?(?:\+|\s|$)", re.I)
JR_PARSE_RE = re.compile(r"^/jr(?:@(?P<bot>\w+))?(?:(?:\+|\s+)(?P<body>.*))?$", re.I | re.S)

COMMANDS = {
    "start": "显示管理员菜单",
    "help": "显示使用帮助",
    "jr": "巨人查询（支持 /jr+内容 与多行批量）",
    "hw": "核武器私聊查询（支持多行批量）",
    "3ys": "中核验旧三要素（支持多行批量汇总）",
    "qd": "手动触发全账号签到",
    "jf": "查询全账号三平台积分",
    "status": "查看协议号与运行状态",
    "release": "手动取消并释放进行中的请求",
    "login": "私聊登录添加协议号",
    "cancel": "取消当前登录流程",
}

STATE_LABELS = {
    "online": "🟢 在线",
    "offline": "⚪ 离线",
    "unauthorized": "🔴 未授权",
    "unavailable": "🟡 冷却中",
    "error": "🔴 异常",
    "success": "成功",
    "skipped": "已跳过",
    "failed": "失败",
    "timeout": "超时",
    "rate_limited": "限流",
}


def _short_label(text: str, limit: int = 24) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


async def safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("编辑消息失败", exc_info=exc)
    except TelegramError as exc:
        log.warning("编辑消息失败", exc_info=exc)


async def _mark_cancelled(target, text: str) -> None:
    try:
        if isinstance(target, ProgressEditor):
            await asyncio.wait_for(target.update(text, force=True), 2.0)
        else:
            await asyncio.wait_for(safe_edit(target, text), 2.0)
    except Exception:
        pass


async def send_long(message, text: str) -> None:
    for chunk in split_text(text):
        await message.reply_text(chunk)


def parse_jr_input(raw_text: str, bot_username: str | None = None) -> str | None:
    m = JR_PARSE_RE.match((raw_text or "").strip())
    if not m:
        return None
    if m["bot"] and bot_username and m["bot"].casefold() != bot_username.casefold():
        return None
    return (m["body"] or "").strip()


async def sync_admin_menu(bot, admin_id: int) -> None:
    try:
        await bot.set_my_commands([], scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in COMMANDS.items()],
            scope=BotCommandScopeChat(admin_id),
        )
    except BadRequest as exc:
        if "chat not found" not in str(exc).lower():
            log.warning("同步管理员菜单失败", exc_info=exc)
    except TelegramError as exc:
        log.warning("同步管理员菜单失败", exc_info=exc)


def register_handlers(application: Application, app) -> None:
    balance_lock = asyncio.Lock()
    hw_batch_lock = asyncio.Lock()
    zhy_batch_lock = asyncio.Lock()

    async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != app.cfg.admin_id:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ 仅限管理员使用本机器人。")
            raise ApplicationHandlerStop
        if not app.accepting:
            if update.effective_message:
                await update.effective_message.reply_text("⏳ 机器人正在关闭或重启，请稍后再试。")
            raise ApplicationHandlerStop

    async def on_start_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat and update.effective_chat.type == "private":
            await sync_admin_menu(context.bot, app.cfg.admin_id)
        lines = ["🛠 管理员控制台（v3）\n"]
        lines.extend(f"/{cmd} - {desc}" for cmd, desc in COMMANDS.items())
        lines.append("/jr+内容 - 巨人查询快捷方式")
        await update.effective_message.reply_text("\n".join(lines))

    async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with app.track_request("status", "/status 状态检查"):
            accounts = tuple(app.pool.accounts.values())
            await asyncio.gather(*(app.pool.refresh(a) for a in accounts if not a.lock.locked()))
            meta_map = await app.db.all_account_meta()
            checkins = await app.db.all_checkins()
            bot_names = {t.key: t.name for t in TARGETS}
            ck_by_phone: dict[str, list[str]] = {}
            for row in checkins:
                if row["bot_key"] in bot_names:
                    st = STATE_LABELS.get(row["last_status"], row["last_status"])
                    ck_by_phone.setdefault(row["phone"], []).append(f"{bot_names[row['bot_key']]}:{st}")

            now = time.time()
            lines = ["📡 协议号运行状态"]
            for acc in accounts:
                meta = meta_map.get(acc.phone, {})
                st = STATE_LABELS.get(acc.state.value, acc.state.value)
                extras: list[str] = []
                if acc.error_type:
                    extras.append(acc.error_type)
                if acc.flood_until > now:
                    extras.append(f"限流剩{math.ceil(acc.flood_until - now)}s")
                cd = meta.get("jr_cooldown_until", 0.0)
                if cd > now:
                    extras.append(f"JR冷却剩{math.ceil(cd - now)}s")
                mem = meta.get("jr_membership", "unknown")
                if mem == "member":
                    extras.append("JR会员")
                else:
                    used = await app.db.get_jr_daily_usage(acc.phone)
                    extras.append(f"JR今日 {used}/{JR_FREE_DAILY_LIMIT}")
                suffix = f"（{' · '.join(extras)}）" if extras else ""
                lines.append(f"• {acc.phone}：{st}{suffix}")
                if acc.phone in ck_by_phone:
                    lines.append("  最近签到：" + "，".join(ck_by_phone[acc.phone]))

            online_cnt = sum(1 for a in accounts if a.state.value == "online")
            active = app.list_active_requests(exclude_current=True)
            lines.append(f"\n总账号：{len(accounts)} ｜ 在线：{online_cnt} ｜ 进行中请求：{len(active)}")
            if active:
                now_mono = time.monotonic()
                for req in active:
                    elapsed = max(0, int(now_mono - req.started_at))
                    lines.append(f"• {req.label}（已运行 {elapsed}s）")
                lines.append("发送 /release 可手动取消并释放。")
            await send_long(update.effective_message, "\n".join(lines))

    async def on_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if app.checkins.manual_lock.locked():
            await msg.reply_text("已有手动签到正在进行，请稍候。")
            return
        async with app.track_request("qd", "/qd 手动签到"), app.checkins.manual_lock:
            progress = await msg.reply_text("⏳ 正在执行全账号签到，最多等待 4 分钟…")
            try:
                results = await app.checkins.run_manual_all()
            except asyncio.CancelledError:
                await _mark_cancelled(progress, "🛑 手动签到已取消并释放。")
                raise
            lines = ["📋 手动签到汇总"]
            for phone, items in results:
                sub = "\n".join(
                    f"  {name}：{STATE_LABELS.get(r.status, r.status)}（{r.reason}）"
                    for name, r in items
                )
                lines.append(f"{phone}\n{sub}")
            text = "\n\n".join(lines) if results else "暂无已登录协议号。"
            chunks = split_text(text)
            await safe_edit(progress, chunks[0])
            for chunk in chunks[1:]:
                await msg.reply_text(chunk)

    async def on_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if balance_lock.locked():
            await msg.reply_text("已有积分查询正在进行，请稍候。")
            return
        async with app.track_request("jf", "/jf 积分查询"), balance_lock:
            progress = await msg.reply_text("⏳ 正在查询全账号三平台积分，最多等待 4 分钟…")
            try:
                results = await app.balances.query_all()
            except asyncio.CancelledError:
                await _mark_cancelled(progress, "🛑 积分查询已取消并释放。")
                raise
            lines = ["📊 账号积分汇总"]
            for phone, items in results:
                sub = "\n".join(
                    f"  {name}：{r.value}" if r.ok else f"  {name}：失败（{r.reason}）"
                    for name, r in items
                )
                lines.append(f"{phone}\n{sub}")
            text = "\n\n".join(lines) if results else "暂无已登录协议号。"
            chunks = split_text(text)
            await safe_edit(progress, chunks[0])
            for chunk in chunks[1:]:
                await msg.reply_text(chunk)

    async def _deliver_text(chat_id: int, bot, editor: ProgressEditor, text: str) -> None:
        chunks = split_text(text)
        await editor.update(chunks[0], force=True)
        for chunk in chunks[1:]:
            await bot.send_message(chat_id=chat_id, text=chunk)

    async def _deliver_jr_result(chat_id: int, bot, editor: ProgressEditor, res) -> None:
        if not res.ok:
            await editor.update(f"❌ 巨人查询未完成：{res.reason}", force=True)
            return
        if res.kind == "empty":
            await editor.update(res.text or "查询空", force=True)
            return
        if res.kind == "file" and res.file_path:
            path = Path(res.file_path)
            with path.open("rb") as f:
                caption = (
                    "⚠️ 部分结果（翻页未完整）"
                    if res.partial
                    else ("✅ 巨人查询完成（缓存）" if res.from_cache else "✅ 巨人查询完成")
                )
                await bot.send_document(chat_id=chat_id, document=f, filename=res.file_name, caption=caption)
            try:
                await editor.message.delete()
            except TelegramError:
                await editor.update("✅ 结果文件已发送。", force=True)
            return

        prefix = "✅ 巨人查询完成（命中缓存）\n\n" if res.from_cache else "✅ 巨人查询完成\n\n"
        await _deliver_text(chat_id, bot, editor, prefix + res.text)

    async def on_jr(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        body = parse_jr_input(msg.text, context.bot.username)
        if body is None:
            return
        if not body:
            await msg.reply_text("用法：/jr 查询内容 或 /jr+查询内容\n支持多行批量（每行一条）。")
            return

        lines = list(dict.fromkeys(ln.strip() for ln in body.splitlines() if ln.strip()))
        chat_id = update.effective_chat.id
        req_label = f"/jr {_short_label(lines[0])}" if len(lines) == 1 else f"/jr 批量({len(lines)}条)"

        async with app.track_request("jr", req_label):
            if len(lines) == 1:
                progress = await msg.reply_text("⏳ 收到巨人查询任务，准备处理…")
                editor = ProgressEditor(progress)
                try:
                    res = await app.jr.query(lines[0], on_progress=editor.update)
                except asyncio.CancelledError:
                    await _mark_cancelled(editor, "🛑 巨人查询已手动取消并释放。")
                    raise
                await _deliver_jr_result(chat_id, context.bot, editor, res)
                return

            summary = await msg.reply_text(f"📦 巨人批量查询：共 {len(lines)} 条（已去重），逐条执行…")
            summary_ed = ProgressEditor(summary, min_interval=1.0)
            hit_cnt = empty_cnt = fail_cnt = 0
            stopped_reason = ""
            p_ed: ProgressEditor | None = None
            try:
                for idx, item in enumerate(lines, 1):
                    if not app.accepting or app.pool.stopping:
                        stopped_reason = "程序正在关闭"
                        break
                    p_msg = await msg.reply_text(f"⏳ [{idx}/{len(lines)}] 正在查询…")
                    p_ed = ProgressEditor(p_msg)
                    res = await app.jr.query(
                        item,
                        on_progress=lambda t, ed=p_ed, i=idx: ed.update(f"[{i}/{len(lines)}] {t}"),
                    )
                    await _deliver_jr_result(chat_id, context.bot, p_ed, res)
                    p_ed = None
                    if res.ok:
                        if res.kind == "empty":
                            empty_cnt += 1
                        else:
                            hit_cnt += 1
                    else:
                        fail_cnt += 1
                        if res.fatal_batch:
                            stopped_reason = res.reason
                            break
                    await summary_ed.update(
                        f"📦 巨人批量进度：{idx}/{len(lines)}（命中 {hit_cnt}，空 {empty_cnt}，失败 {fail_cnt}）"
                    )
            except asyncio.CancelledError:
                if p_ed is not None:
                    await _mark_cancelled(p_ed, "🛑 已手动取消。")
                remain = len(lines) - hit_cnt - empty_cnt - fail_cnt
                await _mark_cancelled(
                    summary_ed,
                    f"🛑 巨人批量已手动取消并释放：命中 {hit_cnt}，空 {empty_cnt}，失败 {fail_cnt}，未执行 {remain}",
                )
                raise

            remain = len(lines) - hit_cnt - empty_cnt - fail_cnt
            if stopped_reason:
                await summary_ed.update(
                    f"⚠️ 巨人批量已提前暂停：命中 {hit_cnt}，空 {empty_cnt}，失败 {fail_cnt}，未执行 {remain}\n原因：{stopped_reason}",
                    force=True,
                )
            else:
                await summary_ed.update(
                    f"✅ 巨人批量完成：共 {len(lines)} 条（命中 {hit_cnt}，空 {empty_cnt}，失败 {fail_cnt}）",
                    force=True,
                )

    async def on_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not update.effective_chat or update.effective_chat.type != "private":
            await msg.reply_text("请在机器人私聊中使用 /hw。")
            return
        parts = (msg.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await msg.reply_text("用法：/hw 查询内容\n支持多行批量（每行一条）。")
            return
        if hw_batch_lock.locked():
            await msg.reply_text("已有核武器查询任务进行中，请稍候。")
            return

        lines = list(dict.fromkeys(ln.strip() for ln in parts[1].splitlines() if ln.strip()))
        chat_id = update.effective_chat.id
        req_label = f"/hw {_short_label(lines[0])}" if len(lines) == 1 else f"/hw 批量({len(lines)}条)"

        async with app.track_request("hw", req_label), hw_batch_lock:
            if len(lines) == 1:
                progress = await msg.reply_text("⏳ 正在查询核武器…")
                editor = ProgressEditor(progress)
                try:
                    res = await app.hw.query(lines[0], on_progress=editor.update)
                except asyncio.CancelledError:
                    await _mark_cancelled(editor, "🛑 核武器查询已手动取消并释放。")
                    raise
                if not res.ok:
                    await editor.update(f"❌ 核武器查询失败：{res.reason}", force=True)
                elif res.kind == "file" and res.file_path:
                    with Path(res.file_path).open("rb") as f:
                        await context.bot.send_document(chat_id=chat_id, document=f, filename=res.file_name)
                    try:
                        await progress.delete()
                    except TelegramError:
                        await editor.update("✅ 核武器 TXT 已发送。", force=True)
                else:
                    await _deliver_text(chat_id, context.bot, editor, res.text)
                return

            summary = await msg.reply_text(f"📦 核武器批量查询：共 {len(lines)} 条，逐条执行…")
            summary_ed = ProgressEditor(summary, min_interval=1.0)
            ok_cnt = fail_cnt = 0
            stopped_reason = ""
            try:
                for idx, item in enumerate(lines, 1):
                    if not app.accepting or app.pool.stopping:
                        stopped_reason = "程序正在关闭"
                        break
                    await summary_ed.update(f"⏳ 核武器批量第 {idx}/{len(lines)} 条（成功 {ok_cnt}，失败 {fail_cnt}）…")
                    res = await app.hw.query(item)
                    if res.ok:
                        ok_cnt += 1
                        if res.kind == "file" and res.file_path:
                            with Path(res.file_path).open("rb") as f:
                                await context.bot.send_document(chat_id=chat_id, document=f, filename=res.file_name)
                        else:
                            for chunk in split_text(res.text):
                                await context.bot.send_message(chat_id=chat_id, text=chunk)
                    else:
                        fail_cnt += 1
                        await context.bot.send_message(chat_id=chat_id, text=f"❌ 第 {idx} 条查询失败：{res.reason}")
                        if res.fatal_batch:
                            stopped_reason = res.reason
                            break
            except asyncio.CancelledError:
                remain = len(lines) - ok_cnt - fail_cnt
                await _mark_cancelled(
                    summary_ed,
                    f"🛑 核武器批量已手动取消并释放：成功 {ok_cnt}，失败 {fail_cnt}，未执行 {remain}",
                )
                raise

            remain = len(lines) - ok_cnt - fail_cnt
            if stopped_reason:
                await summary_ed.update(
                    f"⚠️ 核武器批量已提前暂停：成功 {ok_cnt}，失败 {fail_cnt}，未执行 {remain}\n原因：{stopped_reason}",
                    force=True,
                )
            else:
                await summary_ed.update(
                    f"✅ 核武器批量完成：共 {len(lines)} 条，成功 {ok_cnt}，失败 {fail_cnt}。",
                    force=True,
                )

    async def on_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        now_mono = time.monotonic()
        released = await app.release_requests(exclude_current=True)
        if not released:
            await msg.reply_text("当前没有进行中的请求需要释放。")
            return
        lines = [f"🧹 已手动释放 {len(released)} 个进行中请求："]
        for req in released:
            elapsed = max(0, int(now_mono - req.started_at))
            lines.append(f"• {req.label}（已运行 {elapsed}s）")
        await send_long(msg, "\n".join(lines))

    async def on_3ys(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        parts = (msg.text or "").split(maxsplit=1)
        body = parts[1].strip() if len(parts) == 2 else ""
        if not body:
            await msg.reply_text(
                "用法：/3ys 姓名 身份证 手机号\n支持多行批量核验（每行一条，自动去重并汇总结果）。"
            )
            return

        items, errors = parse_3ys_batch(body)
        if errors:
            preview = "\n".join(errors[:10])
            more = f"\n…另有 {len(errors) - 10} 行格式错误" if len(errors) > 10 else ""
            await msg.reply_text(f"❌ 格式校验未通过（未消耗金币）：\n{preview}{more}")
            return
        if not items:
            await msg.reply_text("用法：/3ys 姓名 身份证 手机号")
            return
        if zhy_batch_lock.locked():
            await msg.reply_text("已有旧三要素批量核验进行中，请稍候。")
            return

        chat_id = update.effective_chat.id
        if len(items) == 1:
            name, id_card, phone = items[0]
            async with app.track_request("3ys", f"/3ys {_short_label(name)}"):
                progress = await msg.reply_text("⏳ 正在提交中核验旧三要素…")
                editor = ProgressEditor(progress)
                try:
                    res = await app.zhy.verify_old_3ys(
                        name, id_card, phone, on_progress=editor.update
                    )
                except asyncio.CancelledError:
                    await _mark_cancelled(editor, "🛑 旧三要素核验已手动取消并释放。")
                    raise
                if not res.ok:
                    await editor.update(f"❌ 旧三要素核验失败：{res.reason}", force=True)
                    return
                prefix = "♻️（命中缓存）\n" if res.from_cache else ""
                await _deliver_text(chat_id, context.bot, editor, prefix + res.text)
            return

        async with zhy_batch_lock, app.track_request("3ys", f"/3ys 批量({len(items)}条)"):
            summary = await msg.reply_text(
                f"📦 旧三要素批量核验：共 {len(items)} 条（已去重），准备并发调度…"
            )
            summary_ed = ProgressEditor(summary, min_interval=1.0)
            collected: dict[int, ZHYResult] = {}
            try:
                entries, stopped_reason = await app.zhy.verify_old_3ys_batch(
                    items,
                    on_progress=summary_ed.update,
                    should_stop=lambda: not app.accepting or app.pool.stopping,
                    collected=collected,
                )
            except asyncio.CancelledError:
                partial_entries = [(items[i], collected[i]) for i in sorted(collected)]
                remain = len(items) - len(partial_entries)
                await _mark_cancelled(
                    summary_ed,
                    f"🛑 旧三要素批量已手动取消并释放（已完成 {len(partial_entries)}/{len(items)}，未执行 {remain}）。",
                )
                if partial_entries:
                    report = format_3ys_batch_summary(
                        partial_entries, total=len(items), stopped_reason="手动取消"
                    )
                    for chunk in split_text(report):
                        try:
                            await asyncio.wait_for(
                                context.bot.send_message(chat_id=chat_id, text=chunk), 2.0
                            )
                        except Exception:
                            break
                raise

            match_cnt = sum(1 for _, r in entries if classify_zhy_result(r) == "match")
            mismatch_cnt = sum(1 for _, r in entries if classify_zhy_result(r) == "mismatch")
            err_cnt = len(entries) - match_cnt - mismatch_cnt
            remain = len(items) - len(entries)
            if stopped_reason:
                await summary_ed.update(
                    f"⚠️ 旧三要素批量提前暂停：🟢 {match_cnt} ｜ 🔴 {mismatch_cnt} ｜ 🟡 {err_cnt}（未执行 {remain}）\n原因：{stopped_reason}",
                    force=True,
                )
            else:
                await summary_ed.update(
                    f"✅ 旧三要素批量完成（共 {len(items)} 条）：🟢 一致 {match_cnt} ｜ 🔴 不一致 {mismatch_cnt} ｜ 🟡 出错/失败 {err_cnt}",
                    force=True,
                )
            if entries:
                report = format_3ys_batch_summary(
                    entries, total=len(items), stopped_reason=stopped_reason
                )
                for chunk in split_text(report):
                    await context.bot.send_message(chat_id=chat_id, text=chunk)

    async def on_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or update.effective_chat.type != "private":
            await update.effective_message.reply_text("登录只能在管理员私聊中进行。")
            return
        reply = await app.login.begin()
        await update.effective_message.reply_text(reply)

    async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or update.effective_chat.type != "private":
            await update.effective_message.reply_text("请在管理员私聊中使用 /cancel。")
            return
        reply = await app.login.cancel()
        await update.effective_message.reply_text(reply)

    async def on_login_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.effective_chat or update.effective_chat.type != "private":
            return
        if not app.login.is_active:
            return
        raw = update.message.text or ""
        try:
            await update.message.delete()
        except TelegramError:
            pass
        reply = await app.login.submit(raw)
        if reply:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        log.error("处理 Telegram 更新异常", exc_info=context.error)

    application.add_handler(MessageHandler(filters.ALL, admin_only), group=-1)
    application.add_handler(
        CallbackQueryHandler(app.manual_verify.handle_callback, pattern=r"^jrverify:[\w-]+:\d+$")
    )
    application.add_handler(CommandHandler(["start", "help"], on_start_help))
    application.add_handler(CommandHandler("status", on_status, block=False))
    application.add_handler(CommandHandler(["release", "stop"], on_release, block=False))
    application.add_handler(CommandHandler("qd", on_checkin))
    application.add_handler(CommandHandler("jf", on_balance))
    application.add_handler(CommandHandler("hw", on_hw))
    application.add_handler(CommandHandler("3ys", on_3ys))
    application.add_handler(CommandHandler("login", on_login))
    application.add_handler(CommandHandler("cancel", on_cancel))
    # block=False 确保 /jr 等待人工验证回调时不会被阻塞
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(JR_ROUTE_RE), on_jr, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_login_input))
    application.add_error_handler(on_error)
