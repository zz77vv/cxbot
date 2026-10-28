from .guards import protected
from ..utils.message_format import reply_long


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        rows = await ctx.status.snapshot()
        lines = ['📡 协议号状态']
        for key, state, error, until in rows:
            lines.append(f'{key}: {state}' + (f' ({error})' if error else ''))
        lines.append(f'总账号：{len(rows)}；在线：{sum(r[1] == "online" for r in rows)}')
        lines.append(f'JR 活跃请求：{len(ctx.jr.active)}；接收新请求：{ctx.jr.accepting}')
        await reply_long(update.effective_message, '\n'.join(lines))
    return handle
