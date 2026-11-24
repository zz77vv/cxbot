"""Telegram Bot 命令与回调路由（单管理员守卫 + 简洁交互）。"""
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

log = logging.getLogger(__name__)

JR_ROUTE_RE = re.compile(r"^/jr(?:@\w+)?(?:\+|\s|$)", re.I)
JR_PARSE_RE = re.compile(r"^/jr(?:@(?P<bot>\w+))?(?:(?:\+|\s+)(?P<body>.*))?$", re.I | re.S)

COMMANDS = {
    "start": "显示管理员菜单",
    "help": "显示使用帮助",
    "jr": "巨人查询（支持 /jr+内容 与多行批量）",
    "hw": "核武器私聊查询（支持多行批量）",
    "qd": "手动触发全账号签到",
    "jf": "查询全账号三平台积分",
    "status": "查看协议号与运行状态",
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


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    cur = ""
    cur_len = 0
    for line in (text or "").splitlines(keepends=True):
        size = utf16_len(line)
        if cur and cur_len + size > limit:
            chunks.append(cur)
            cur, cur_len = "", 0
        if size <= limit:
            cur += line
            cur_len += size
        else:
            for ch in line:
                csize = utf16_len(ch)
                if cur_len + csize > limit:
                    chunks.append(cur)
                    cur, cur_len = "", 0
                cur += ch
                cur_len += csize
    if cur:
        chunks.append(cur)
    return chunks or [""]


async def safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("编辑消息失败", exc_info=exc)
    except TelegramError as exc:
        log.warning("编辑消息失败", exc_info=exc)


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

    async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != app.cfg.admin_id:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ 仅限管理员使用本机器人。")
            raise ApplicationHandlerStop

    async def on_start_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat and update.effective_chat.type == "private":
            await sync_admin_menu(context.bot, app.cfg.admin_id)
        lines = ["🛠 管理员控制台（v3）\n"]
        lines.extend(f"/{cmd} - {desc}" for cmd, desc in COMMANDS.items())
        lines.append("/jr+内容 - 巨人查询快捷方式")
        await update.effective_message.reply_text("\n".join(lines))

    async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        lines.append(f"\n总账号：{len(accounts)} ｜ 在线：{online_cnt}")
        await send_long(update.effective_message, "\n".join(lines))

    async def on_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if app.checkins.manual_lock.locked():
            await msg.reply_text("已有手动签到正在进行，请稍候。")
            return
        async with app.checkins.manual_lock:
            progress = await msg.reply_text("⏳ 正在执行全账号签到，最多等待 4 分钟…")
            results = await app.checkins.run_manual_all()
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
        async with balance_lock:
            progress = await msg.reply_text("⏳ 正在查询全账号三平台积分，最多等待 4 分钟…")
            results = await app.balances.query_all()
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

    async def _deliver_jr_result(chat_id: int, bot, progress_msg, res) -> None:
        if not res.ok:
            await safe_edit(progress_msg, f"❌ 巨人查询未完成：{res.reason}")
            return
        if res.kind == "empty":
            await safe_edit(progress_msg, res.text or "查询空")
            return
        if res.kind == "file" and res.file_path:
            path = Path(res.file_path)
            with path.open("rb") as f:
                caption = "⚠️ 部分结果（翻页超时）" if res.partial else ("✅ 巨人查询完成（缓存）" if res.from_cache else "✅ 巨人查询完成")
                await bot.send_document(chat_id=chat_id, document=f, filename=res.file_name, caption=caption)
            try:
                await progress_msg.delete()
            except TelegramError:
                await safe_edit(progress_msg, "✅ 结果文件已发送。")
            return

        prefix = "✅ 巨人查询完成（命中缓存）\n\n" if res.from_cache else "✅ 巨人查询完成\n\n"
        chunks = split_text(prefix + res.text)
        await safe_edit(progress_msg, chunks[0])
        for chunk in chunks[1:]:
            await bot.send_message(chat_id=chat_id, text=chunk)

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

        if len(lines) == 1:
            progress = await msg.reply_text("⏳ 收到巨人查询任务，准备处理…")
            res = await app.jr.query(lines[0], on_progress=lambda t: safe_edit(progress, t))
            await _deliver_jr_result(chat_id, context.bot, progress, res)
            return

        summary = await msg.reply_text(f"📦 巨人批量查询：共 {len(lines)} 条（已去重），逐条执行…")
        ok_cnt = fail_cnt = 0
        for idx, item in enumerate(lines, 1):
            if app.pool.stopping:
                break
            p_msg = await msg.reply_text(f"⏳ [{idx}/{len(lines)}] 正在查询…")
            res = await app.jr.query(item, on_progress=lambda t, pm=p_msg, i=idx: safe_edit(pm, f"[{i}/{len(lines)}] {t}"))
            await _deliver_jr_result(chat_id, context.bot, p_msg, res)
            if res.ok:
                ok_cnt += 1
            else:
                fail_cnt += 1
            await safe_edit(summary, f"📦 巨人批量进度：{idx}/{len(lines)}（成功 {ok_cnt}，失败 {fail_cnt}）")

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

        async with hw_batch_lock:
            if len(lines) == 1:
                progress = await msg.reply_text("⏳ 正在查询核武器…")
                res = await app.hw.query(lines[0], on_progress=lambda t: safe_edit(progress, t))
                if not res.ok:
                    await safe_edit(progress, f"❌ 核武器查询失败：{res.reason}")
                elif res.kind == "file" and res.file_path:
                    with Path(res.file_path).open("rb") as f:
                        await context.bot.send_document(chat_id=chat_id, document=f, filename=res.file_name)
                    try:
                        await progress.delete()
                    except TelegramError:
                        await safe_edit(progress, "✅ 核武器 TXT 已发送。")
                else:
                    chunks = split_text(res.text)
                    await safe_edit(progress, chunks[0])
                    for chunk in chunks[1:]:
                        await context.bot.send_message(chat_id=chat_id, text=chunk)
                return

            summary = await msg.reply_text(f"📦 核武器批量查询：共 {len(lines)} 条，逐条执行…")
            ok_cnt = fail_cnt = 0
            for idx, item in enumerate(lines, 1):
                if app.pool.stopping:
                    break
                await safe_edit(summary, f"⏳ 核武器批量第 {idx}/{len(lines)} 条（成功 {ok_cnt}，失败 {fail_cnt}）…")
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
            await safe_edit(summary, f"✅ 核武器批量完成：共 {len(lines)} 条，成功 {ok_cnt}，失败 {fail_cnt}。")

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

    # 注册顺序：全局管理员守卫 -> 回调 -> 命令 -> 登录文本输入
    application.add_handler(MessageHandler(filters.ALL, admin_only), group=-1)
    application.add_handler(
        CallbackQueryHandler(app.manual_verify.handle_callback, pattern=r"^jrverify:[\w-]+:\d+$")
    )
    application.add_handler(CommandHandler(["start", "help"], on_start_help))
    application.add_handler(CommandHandler("status", on_status))
    application.add_handler(CommandHandler("qd", on_checkin))
    application.add_handler(CommandHandler("jf", on_balance))
    application.add_handler(CommandHandler("hw", on_hw))
    application.add_handler(CommandHandler("login", on_login))
    application.add_handler(CommandHandler("cancel", on_cancel))
    # block=False 确保 /jr 等待人工验证回调时不会被阻塞
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(JR_ROUTE_RE), on_jr, block=False))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_login_input))
    application.add_error_handler(on_error)
