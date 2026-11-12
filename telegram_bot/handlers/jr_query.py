import re
import asyncio
import logging
from collections import OrderedDict
from .guards import protected
from . import jr_batch
from ..services.jr.progress import JRProgress
from ..models.enums import QueryState

log = logging.getLogger(__name__)

JR_ROUTE = re.compile(r'^/jr(?:@\w+)?(?:\+|\s|$)', re.I)


def parse_request(text, bot_username=None):
    match = re.match(r'^/jr(?:@(?P<bot>\w+))?(?:(?:\+|\s+)(?P<body>.*))?$', (text or '').strip(), re.I | re.S)
    if not match:
        return None
    if match['bot'] and bot_username and match['bot'].casefold() != bot_username.casefold():
        return None
    return (match['body'] or '').strip()


def make_handler(ctx):
    seen = OrderedDict()
    batch_handler = jr_batch.make_handler(ctx)

    @protected(ctx)
    async def handle(update, context):
        text = parse_request(update.effective_message.text, context.bot.username)
        if text is None:
            return
        if not text:
            await update.effective_message.reply_text('用法：/jr 查询内容\n或 /jr+查询内容')
            return
        request_id = f'{update.effective_chat.id}:{update.update_id}'
        if request_id in seen:
            return
        # Mark before the first await so concurrent copies of one update share no sends.
        seen[request_id] = False
        progress = None
        try:
            status = await update.effective_message.reply_text(JRProgress.initial_text())
            progress = JRProgress(status, ctx.config.reply_timeout)
            # update_id is durable across retries; identical updates cannot submit twice.
            report = await ctx.jr.execute(request_id, text, progress=progress)
            await progress.finish(report)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error('event=jr_progress_failed error_type=%s', type(error).__name__)
            if progress is not None:
                await progress.fail(progress.task_id, QueryState.PENDING_REVIEW, f'uncertain_{type(error).__name__}')
        finally:
            seen[request_id] = True
            # Never evict an active update: it could still be delivering a result.
            for key, completed in list(seen.items()):
                if len(seen) <= 512:
                    break
                if completed:
                    seen.pop(key)
    async def route(update, context):
        message = update.effective_message
        text = parse_request(message.text, context.bot.username) if message else None
        if text and len([line for line in text.splitlines() if line.strip()]) > 1:
            return await batch_handler(update, context)
        return await handle(update, context)

    return route
