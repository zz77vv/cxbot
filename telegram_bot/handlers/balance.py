import asyncio

from telegram.error import BadRequest, TelegramError

from .guards import protected
from ..utils.message_format import split_text_by_lines
from ..utils.user_messages import state_text, reason_text


def make_handler(ctx):
    active = asyncio.Lock()

    @protected(ctx)
    async def handle(update, context):
        message = update.effective_message
        if active.locked():
            await message.reply_text('已有积分查询正在进行，请等它完成。')
            return
        async with active:
            await query_and_report(message)

    async def query_and_report(message):
        progress = await message.reply_text('⏳ 查询账号积分中，最多等待 4 分钟…')
        try:
            results = await ctx.balances.all()
        except Exception:
            await progress.edit_text('积分查询异常中断，请稍后重试。')
            raise
        lines = ['📊 账号积分汇总']
        for account, targets in results:
            lines.append(account + '\n' + '\n'.join(
                f'  {name}：' + (f'积分：{r.value}' if r.value is not None else f'{state_text(r.outcome)}（{reason_text(r.reason)}）') for name, r in targets))
        first, *rest = split_text_by_lines('\n\n'.join(lines), escape_html=True)
        try:
            await progress.edit_text(first, parse_mode='HTML')
        except TelegramError as error:
            if not (isinstance(error, BadRequest) and 'message is not modified' in str(error).lower()):
                await message.reply_text(first, parse_mode='HTML')
        for chunk in rest:
            await message.reply_text(chunk, parse_mode='HTML')
    return handle
