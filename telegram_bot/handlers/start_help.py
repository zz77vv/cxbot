from .guards import protected
from .menu_sync import set_admin_menu


def make_handler(ctx, specs):
    @protected(ctx)
    async def handle(update, context):
        if update.effective_chat.type == 'private':
            await set_admin_menu(context.bot, ctx.config, specs)
        text = '🛠 管理员控制台\n\n' + '\n'.join(f'/{name} - {description}' for name, description in specs.items())
        await update.effective_message.reply_text(text + '\n/jr+查询内容 - 巨人查询快捷入口')
    return handle
