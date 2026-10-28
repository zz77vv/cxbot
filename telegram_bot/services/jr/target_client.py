import asyncio
from ...clients.dialogue import signature
from ...target_bots import JR, JR_PAGE_TIMEOUT
from .result_parser import is_jr_pending_message


class TargetClient:
    def __init__(self, dialogue, cfg):
        self.dialogue, self.cfg = dialogue, cfg

    async def submit(self, client, text):
        return await asyncio.wait_for(client.send_message(JR.bot, text), self.cfg.reply_timeout)

    async def ready(self, client, after_id):
        return await self.dialogue.wait(client, JR.bot, after_id,
            lambda m: not is_jr_pending_message(m), self.cfg.reply_timeout)

    async def click_and_wait(self, client, message, button):
        old_signature = signature(message)
        r, c, _ = button
        await asyncio.wait_for(message.click(r, c), self.cfg.reply_timeout)
        return await self.dialogue.wait(client, JR.bot, message.id,
            lambda m: not is_jr_pending_message(m), JR_PAGE_TIMEOUT,
            original=message, old_signature=old_signature)
