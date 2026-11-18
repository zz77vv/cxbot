from dataclasses import dataclass


@dataclass(frozen=True)
class TargetBot:
    name: str
    bot: str
    checkin_cmd: str
    balance_cmd: str
    balance_pattern: str
    checkin_flow: str = 'simple_text'
    success_keywords: tuple[str, ...] = ('签到成功', '已签到', '打卡成功')
    balance_timeout: float = 20
    checkin_timeout: float = 20
    checkin_buttons: tuple[str, ...] = ()
    balance_button: str = ''

    @property
    def key(self):
        return self.bot.strip().casefold()


TARGETS = (
    TargetBot('中核验', '@zhonghybot', '/start', '/start', r'金币[🪙]?\s*[:：]\s*(\d+)',
              'button_chain', ('签到成功', '获得', '当前金币', '金币'),
              checkin_buttons=('获得金币', '签到')),
    TargetBot('巨人', '@jrsgk', '/checkin', '/balance', r'积分余额[：:]\s*(\d+)'),
    TargetBot('核武器', '@hewuqi', '/qd', '个人', r'盒能\s*[:：]\s*`?(\d+)`?', balance_timeout=60),
)
JR = TARGETS[1]
MIN_INTERVAL = 86400
RANDOM_OFFSET_MAX = 900
JR_MAX_PAGES = 200
JR_PAGE_TIMEOUT = 30
JR_PAGE_INTERVAL = 1.0
JR_POLL_INTERVAL = 0.8
JR_BUY_AMOUNTS = (5, 10, 50)
JR_NEXT_PAGE_KEYWORDS = ('下一页', '下页', '›', '»')
JR_VERIFY_KEYWORDS = ('安全验证', '点击下图', '验证')
JR_PENDING_KEYWORDS = ('正在查询', '查询中', '请稍候', '请稍等')
JR_EXPORT_KEYWORDS = ('导出数据',)
JR_FREE_EXPORT_KEYWORDS = ('免费用户暂不支持导出', '请充值后再使用')
JR_EXPORT_SUCCESS_KEYWORDS = ('成功导出',)
JR_FREE_DAILY_LIMIT = 5
JR_MEMBERSHIP_RECHECK_INTERVAL = 86400
OCR_MAX_ATTEMPTS = 3
OCR_MIN_CONFIDENCE = 0.35
OCR_IGNORE_BUTTON_KEYWORDS = ('刷新', '换一张', '看不清', '取消', '返回', '关闭',
                              '购买', '首页', '上一页', '下一页', '尾页', '导出')
