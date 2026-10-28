import asyncio
import logging
import re
from telethon.errors import FloodWaitError, RPCError
from ..clients.userbot_manager import AccountUnavailable
from ..models.account import OperationResult
from ..models.enums import Outcome
from ..clients.dialogue import find_button
from ..target_bots import TARGETS

log = logging.getLogger(__name__)


def parse_balance(text, target):
    match = re.search(target.balance_pattern, text or '')
    if not match:
        return OperationResult(Outcome.FORMAT_CHANGED, 'balance_pattern_not_matched')
    return OperationResult(Outcome.SUCCESS, value=int(match.group(1)))


class BalanceService:
    def __init__(self, manager, dialogue):
        self.manager, self.dialogue = manager, dialogue

    async def query(self, account, target):
        try:
            async with self.manager.operation(account) as client:
                sent = await asyncio.wait_for(client.send_message(target.bot, target.balance_cmd), self.manager.cfg.reply_timeout)
                if target.balance_button:
                    button_message = await self.dialogue.wait(client, target.bot, sent.id,
                        lambda m: bool(find_button(m, (target.balance_button,))), target.balance_timeout)
                    if not button_message:
                        return OperationResult(Outcome.FAILED, 'balance_button_missing')
                    r, c, _ = find_button(button_message, (target.balance_button,))
                    await asyncio.wait_for(button_message.click(r, c), self.manager.cfg.reply_timeout)
                observed = []

                def accepted(m):
                    if getattr(m, 'text', ''):
                        observed.append(m.text)
                    return parse_balance(getattr(m, 'text', ''), target).outcome == Outcome.SUCCESS

                message = await self.dialogue.wait(client, target.bot, sent.id, accepted, target.balance_timeout)
                if message:
                    return parse_balance(message.text, target)
                return OperationResult(Outcome.FORMAT_CHANGED if observed else Outcome.TIMEOUT,
                                       'balance_pattern_not_matched' if observed else 'balance_reply_timeout')
        except AccountUnavailable:
            return OperationResult(Outcome.UNAVAILABLE, 'account_unavailable')
        except FloodWaitError as error:
            return OperationResult(Outcome.RATE_LIMITED, 'telegram_flood_wait', 'FloodWaitError', retry_after=error.seconds)
        except TimeoutError:
            return OperationResult(Outcome.TIMEOUT, 'balance_transport_timeout', 'TimeoutError')
        except Exception as error:
            log.warning('Balance failed target=%s error_type=%s', target.name, type(error).__name__)
            return OperationResult(Outcome.FAILED, 'balance_error', type(error).__name__)

    async def all(self):
        async def per_account(account):
            return account.key, [(t.name, await self.query(account, t)) for t in TARGETS]
        return await asyncio.gather(*(per_account(a) for a in self.manager.accounts.values()))
