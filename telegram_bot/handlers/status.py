import math
import time

from .guards import protected
from ..utils.message_format import reply_long
from ..utils.user_messages import state_text, reason_text


def make_handler(ctx):
    @protected(ctx)
    async def handle(update, context):
        rows = await ctx.status.snapshot()
        checkins = await ctx.status.checkin_status()
        lines = ['📡 协议号状态']
        now = time.time()
        for key, state, error, until in rows:
            detail = f'（{reason_text(error)}）' if error else ''
            if until > now:
                detail += f' · 冷却剩余 {math.ceil(until - now)} 秒'
            lines.append(f'{key}：{state_text(state)}{detail}')
            if key in checkins:
                summary = '，'.join(f'{name} {state_text(outcome)}' for name, outcome in checkins[key])
                lines.append(f'  最近签到：{summary}')
        lines.append(f'总账号：{len(rows)}；在线：{sum(r[1] == "online" for r in rows)}')
        lines.append(f'巨人活跃请求：{len(ctx.jr.active)}；接收新请求：{"是" if ctx.jr.accepting else "否"}')
        await reply_long(update.effective_message, '\n'.join(lines))
    return handle
