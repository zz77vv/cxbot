from .guards import protected


def make_handler(ctx, specs):
    @protected(ctx, admin=False)
    async def handle(update, context):
        if update.effective_user.id != ctx.config.admin_id:
            await update.effective_message.reply_text('当前暂无对外开放的功能。')
            return
        text = '🛠 管理员控制台\n\n' + '\n'.join(f'/{name} - {description}' for name, description in specs.items())
        await update.effective_message.reply_text(text + '\n/jr+查询内容 - 巨人查询快捷入口')
    return handle
