from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from telegram import BotCommandScopeChat
from telegram.error import BadRequest
from telegram_bot.handlers.registration import configure_menus, COMMANDS
from telegram_bot.handlers.menu_sync import set_admin_menu
from telegram_bot.handlers.start_help import make_handler
from telegram_bot.clients.request_tracker import RequestTracker


async def test_missing_chat_does_not_block_boot(cfg,bot):
    async def delete(**kwargs):
        if isinstance(kwargs['scope'],BotCommandScopeChat):raise BadRequest('Chat not found')
    async def set_commands(commands,**kwargs):
        if isinstance(kwargs['scope'],BotCommandScopeChat):raise BadRequest('Chat not found')
    bot.delete_my_commands.side_effect=delete
    bot.set_my_commands.side_effect=set_commands
    await configure_menus(bot,cfg)


async def test_unrelated_bad_request_is_not_suppressed(cfg,bot):
    bot.set_my_commands.side_effect=BadRequest('invalid command')
    with pytest.raises(BadRequest):await set_admin_menu(bot,cfg,COMMANDS)


async def test_private_start_retries_admin_menu(cfg,bot):
    ctx=SimpleNamespace(config=cfg,requests=RequestTracker())
    update=SimpleNamespace(effective_user=SimpleNamespace(id=cfg.admin_id),
        effective_chat=SimpleNamespace(type='private'),effective_message=SimpleNamespace(reply_text=AsyncMock()))
    await make_handler(ctx,COMMANDS)(update,SimpleNamespace(bot=bot))
    bot.set_my_commands.assert_awaited_once()
    update.effective_message.reply_text.assert_awaited_once()
