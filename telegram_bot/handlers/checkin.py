from .guards import protected
from ..utils.message_format import reply_long
from ..utils.user_messages import state_text, reason_text


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        await update.effective_message.reply_text('⏳ 手动签到中；24 小时间隔仍然有效。')
        results = await ctx.checkins.manual()
        lines = ['📋 手动签到汇总']
        for account, targets in results:
            lines.append(account + '\n' + '\n'.join(f'  {name}：{state_text(r.outcome)}（{reason_text(r.reason)}）' for name, r in targets))
        await reply_long(update.effective_message, '\n\n'.join(lines))
    return handle
