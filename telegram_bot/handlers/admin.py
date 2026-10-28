from .guards import protected


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
            await update.effective_message.reply_text('用法：/unban 用户ID')
            return
        removed = await ctx.users.unban(int(context.args[0]))
        await update.effective_message.reply_text('✅ 已解除封禁。' if removed else 'ℹ️ 该用户当前未被封禁。')
    return handle
