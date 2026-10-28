from functools import wraps
from telegram.ext import ApplicationHandlerStop


def authorized(user_id, admin_id):
    return user_id is not None and user_id == admin_id


def protected(ctx, *, admin=True):
    def decorate(callback):
        @wraps(callback)
        async def wrapped(update, context):
            if not update.effective_user or not update.effective_message:
                return
            if admin and not authorized(update.effective_user.id, ctx.config.admin_id):
                await update.effective_message.reply_text('⛔ 该功能仅限管理员使用。')
                return
            if not ctx.requests.accepting:
                await update.effective_message.reply_text('程序正在关闭，请稍后再试。')
                return
            async with ctx.requests.track():
                return await callback(update, context)
        return wrapped
    return decorate


def banned_guard(ctx):
    async def guard(update, context):
        user = update.effective_user
        if user and user.id != ctx.config.admin_id and await ctx.users.is_banned(user.id):
            if update.effective_message:
                await update.effective_message.reply_text('⛔ 你已被封禁，无法继续使用本机器人。')
            raise ApplicationHandlerStop
    return guard
