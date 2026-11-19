import asyncio
from ...clients.dialogue import signature
from ...target_bots import JR, JR_PAGE_TIMEOUT
from .result_parser import is_jr_pending_message

PAGE_CONFIRM_TIMEOUT = 5


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

    async def confirm_next_page(self, client, message, is_next_page):
        """Read the same result after an uncertain click, without clicking again."""
        sender = getattr(message, 'sender_id', None)

        def matches(candidate):
            if sender is not None and getattr(candidate, 'sender_id', None) != sender:
                return False
            reply_to = getattr(candidate, 'reply_to_msg_id', None)
            return (candidate.id == message.id or reply_to == message.id) and is_next_page(candidate)

        return await self.dialogue.wait(client, JR.bot, message.id, matches,
            min(self.cfg.reply_timeout, PAGE_CONFIRM_TIMEOUT), original=message,
            old_signature=signature(message))
