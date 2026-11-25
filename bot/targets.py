"""上游目标机器人协议规则配置（中核验 / 巨人 / 核武器）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetBot:
    name: str
    username: str
    checkin_cmd: str
    balance_cmd: str
    balance_pattern: str
    checkin_buttons: tuple[str, ...] = ()
    checkin_success_keywords: tuple[str, ...] = ("签到成功", "已签到", "打卡成功")
    balance_timeout: float = 20.0
    checkin_timeout: float = 20.0

    @property
    def key(self) -> str:
        return self.username.strip().casefold()


ZHONGHY = TargetBot(
    name="中核验",
    username="@zhonghybot",
    checkin_cmd="/start",
    balance_cmd="/start",
    balance_pattern=r"金币[🪙]?\s*[:：]\s*(\d+)",
    checkin_buttons=("获得金币", "签到"),
    checkin_success_keywords=("签到成功", "获得", "当前金币", "金币"),
)

JR = TargetBot(
    name="巨人",
    username="@jrsgk",
    checkin_cmd="/checkin",
    balance_cmd="/balance",
    balance_pattern=r"积分余额[：:]\s*(\d+)",
)

HW = TargetBot(
    name="核武器",
    username="@hewuqi",
    checkin_cmd="/qd",
    balance_cmd="个人",
    balance_pattern=r"盒能\s*[:：]\s*`?(\d+)`?",
    balance_timeout=60.0,
)

TARGETS: tuple[TargetBot, ...] = (ZHONGHY, JR, HW)

# 签到与查询通用常量
CHECKIN_MIN_INTERVAL = 86400
CHECKIN_JITTER_MAX = 900

JR_FREE_DAILY_LIMIT = 5
JR_FREE_COOLDOWN_SECONDS = 60
JR_MAX_PAGES = 200
JR_PAGE_TIMEOUT = 30.0
JR_PAGE_INTERVAL = 1.0
JR_BUY_AMOUNTS = (5, 10, 50)
JR_NEXT_PAGE_KEYWORDS = ("下一页", "下页", "›", "»")
JR_VERIFY_KEYWORDS = ("安全验证", "点击下图", "验证")
JR_PENDING_KEYWORDS = ("正在查询", "查询中", "请稍候", "请稍等")
JR_EXPORT_KEYWORDS = ("导出数据",)
JR_FREE_EXPORT_KEYWORDS = ("免费用户暂不支持导出", "请充值后再使用")
JR_EXPORT_SUCCESS_KEYWORDS = ("成功导出",)

OCR_MAX_ATTEMPTS = 3
OCR_MIN_CONFIDENCE = 0.35
OCR_IGNORE_BUTTON_KEYWORDS = (
    "刷新", "换一张", "看不清", "取消", "返回", "关闭",
    "购买", "首页", "上一页", "下一页", "尾页", "导出",
)
