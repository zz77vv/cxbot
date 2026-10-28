import asyncio
import logging
from telegram.ext import Application
from ..handlers.registration import configure_menus

log = logging.getLogger(__name__)


def create_application(cfg):
    return (Application.builder().token(cfg.bot_token).concurrent_updates(cfg.concurrency)
            .connect_timeout(cfg.reply_timeout).read_timeout(cfg.reply_timeout)
            .write_timeout(cfg.export_timeout).build())


class BotApplication:
    def __init__(self, application, cfg):
        self.application, self.cfg = application, cfg
        self.initialize_attempted = False

    async def start(self):
        self.initialize_attempted = True
        await self.application.initialize()
        await configure_menus(self.application.bot, self.cfg)
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=False)

    async def stop_polling(self):
        updater = self.application.updater
        if updater and updater.running:
            await updater.stop()

    async def close(self):
        stages = [('polling', self.stop_polling)]
        if self.application.running:
            stages.append(('application-stop', self.application.stop))
        if self.initialize_attempted:
            stages.append(('application-shutdown', self.application.shutdown))
            # Also close partially initialized components if an earlier step fails.
            if self.application.updater:
                stages.append(('updater-shutdown', self.application.updater.shutdown))
            stages.append(('bot-shutdown', self.application.bot.shutdown))
        for name, close in stages:
            try:
                await asyncio.wait_for(close(), self.cfg.shutdown_timeout)
            except Exception as error:
                log.warning('Bot close stage=%s error_type=%s', name, type(error).__name__)
