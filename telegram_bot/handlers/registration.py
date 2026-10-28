import logging
from telegram import (BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
                       BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats,
                       BotCommandScopeAllChatAdministrators)
from telegram.ext import CommandHandler, MessageHandler, filters
from telegram.error import NetworkError
from . import start_help, checkin, balance, jr_query, status, login
from .guards import admin_guard
from ..utils.retry import retry_safe

COMMANDS = {
    'start': '显示当前身份对应的功能菜单',
    'help': '显示当前身份对应的帮助',
    'jr': '查询内容 - 巨人查询（也支持 /jr+内容）',
    'qd': '手动触发所有账号签到',
    'jf': '查询所有账号签到积分',
    'status': '查看运行状态',
    'login': '私聊登录协议号',
    'cancel': '取消私聊登录',
}


def register(application, ctx):
    menu = start_help.make_handler(ctx, COMMANDS)
    state = status.make_handler(ctx)
    callbacks = {'start': menu, 'help': menu, 'jr': jr_query.make_handler(ctx),
                 'qd': checkin.make_handler(ctx), 'jf': balance.make_handler(ctx),
                 'status': state, 'login': login.start_handler(ctx), 'cancel': login.cancel_handler(ctx)}
    application.add_handler(MessageHandler(filters.ALL, admin_guard(ctx)), group=-1)
    for command, callback in callbacks.items():
        if command == 'jr':
            application.add_handler(MessageHandler(filters.TEXT & filters.Regex(jr_query.JR_ROUTE), callback))
        else:
            application.add_handler(CommandHandler(command, callback))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, login.input_handler(ctx)))

    async def on_error(update, context):
        logging.getLogger(__name__).error('Update handling failed error_type=%s', type(context.error).__name__)
    application.add_error_handler(on_error)
    return callbacks


async def configure_menus(bot, cfg):
    scopes = (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats(),
              BotCommandScopeAllChatAdministrators(), BotCommandScopeChat(cfg.admin_id))
    for scope in scopes:
        for language in ('', 'zh', 'en'):
            await retry_safe(lambda: bot.delete_my_commands(scope=scope, language_code=language),
                             attempts=cfg.retry_attempts, base=cfg.retry_base, retry_on=(NetworkError,))
    await retry_safe(lambda: bot.set_my_commands([], scope=BotCommandScopeDefault()),
                     attempts=cfg.retry_attempts, base=cfg.retry_base, retry_on=(NetworkError,))
    await retry_safe(lambda: bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS.items()],
                                               scope=BotCommandScopeChat(cfg.admin_id)),
                     attempts=cfg.retry_attempts, base=cfg.retry_base, retry_on=(NetworkError,))
