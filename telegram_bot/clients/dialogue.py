import asyncio
import time
from ..utils.retry import retry_safe


def signature(message):
    return (getattr(message, 'text', '') or '',
            tuple(tuple(getattr(b, 'text', '') for b in row) for row in (getattr(message, 'buttons', None) or [])),
            str(getattr(message, 'edit_date', None)))


def find_button(message, keywords):
    for r, row in enumerate(getattr(message, 'buttons', None) or []):
        for c, button in enumerate(row):
            label = getattr(button, 'text', '') or ''
            if any(key in label for key in keywords):
                return r, c, label
    return None


class Dialogue:
    """Read-only bounded polling shared by check-in, balance and JR."""
    def __init__(self, cfg):
        self.cfg = cfg

    async def messages(self, client, bot, **kwargs):
        return await retry_safe(lambda: asyncio.wait_for(client.get_messages(bot, **kwargs), self.cfg.reply_timeout),
                                attempts=self.cfg.retry_attempts, base=self.cfg.retry_base)

    async def wait(self, client, bot, after_id, predicate, timeout, *, original=None, old_signature=None, interval=0.8):
        async def poll():
            while True:
                if original is not None:
                    refreshed = await self.messages(client, bot, ids=original.id)
                    if (refreshed and not getattr(refreshed, 'out', False)
                            and signature(refreshed) != old_signature and predicate(refreshed)):
                        return refreshed
                messages = await self.messages(client, bot, limit=10)
                for message in sorted(messages, key=lambda m: m.id):
                    if message.id > after_id and not getattr(message, 'out', False) and predicate(message):
                        return message
                await asyncio.sleep(interval)
        try:
            return await asyncio.wait_for(poll(), timeout)
        except TimeoutError:
            return None
