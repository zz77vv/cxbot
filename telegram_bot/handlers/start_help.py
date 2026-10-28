from .guards import protected


def make_handler(ctx, specs):
    @protected(ctx)
    async def handle(update, context):
        text = '🛠 管理员控制台\n\n' + '\n'.join(f'/{name} - {description}' for name, description in specs.items())
        await update.effective_message.reply_text(text + '\n/jr+查询内容 - 巨人查询快捷入口')
    return handle
