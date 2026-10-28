import logging
from telegram import BotCommand, BotCommandScopeChat
from telegram.error import BadRequest, NetworkError
from ..utils.retry import retry_safe


def missing_private_chat(error):
    return isinstance(error, BadRequest) and 'chat not found' in str(error).lower()


async def set_admin_menu(bot, cfg, specs):
    try:
        await retry_safe(lambda: bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in specs.items()],
            scope=BotCommandScopeChat(cfg.admin_id)),
            attempts=cfg.retry_attempts, base=cfg.retry_base, retry_on=(NetworkError,))
        return True
    except BadRequest as error:
        if not missing_private_chat(error):
            raise
        logging.getLogger(__name__).info('Admin menu deferred until first private interaction')
        return False
