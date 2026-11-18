from .guards import protected
from ..utils.message_format import reply_long
from ..utils.user_messages import state_text, reason_text


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        rows = await ctx.status.snapshot()
        lines = ['📡 协议号状态']
        for key, state, error, until in rows:
            lines.append(f'{key}：{state_text(state)}' + (f'（{reason_text(error)}）' if error else ''))
        lines.append(f'总账号：{len(rows)}；在线：{sum(r[1] == "online" for r in rows)}')
        lines.append(f'巨人活跃请求：{len(ctx.jr.active)}；接收新请求：{"是" if ctx.jr.accepting else "否"}')
        await reply_long(update.effective_message, '\n'.join(lines))
    return handle
