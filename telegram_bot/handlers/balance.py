from .guards import protected
from ..utils.message_format import reply_long
from ..utils.user_messages import state_text, reason_text


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        await update.effective_message.reply_text('⏳ 查询账号积分中...')
        results = await ctx.balances.all()
        lines = ['📊 账号积分汇总']
        for account, targets in results:
            lines.append(account + '\n' + '\n'.join(
                f'  {name}：' + (f'积分：{r.value}' if r.value is not None else f'{state_text(r.outcome)}（{reason_text(r.reason)}）') for name, r in targets))
        await reply_long(update.effective_message, '\n\n'.join(lines))
    return handle
