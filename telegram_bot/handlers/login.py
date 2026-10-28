import logging
from .guards import protected


def login_route(ctx, action):
    @protected(ctx)
    async def handle(update, context):
        chat, user, message = update.effective_chat, update.effective_user, update.effective_message
        private = chat.type == 'private'
        if not private:
            await message.reply_text('登录只能在管理员私聊中进行。')
            return
        if action == 'submit':
            # Do not accept edited/replayed credentials or log the Update object.
            if not update.message:
                return
            if user.id not in ctx.logins.attempts:
                return
            try:
                await message.delete()
            except Exception:
                logging.getLogger(__name__).warning('Login message deletion failed; no content logged')
            reply = await ctx.logins.submit(user.id, chat.id, private, message.text, update.update_id)
        else:
            reply = await getattr(ctx.logins, action)(user.id, chat.id, private)
        if reply:
            await context.bot.send_message(chat_id=chat.id, text=reply)
    return handle


def start_handler(ctx):
    return login_route(ctx, 'begin')


def cancel_handler(ctx):
    return login_route(ctx, 'cancel')


def input_handler(ctx):
    return login_route(ctx, 'submit')
