# cxbot v3

从零重新设计的轻量化 Telegram 多协议号查询与签到机器人。

## 核心特性
- **极简架构**：`bot/` 下仅 13 个核心模块，逻辑清晰直白，无历史包袱。
- **全新 SQLite 存储**：仅 4 张表（`accounts`、`checkins`、`jr_usage`、`result_cache`），WAL 模式。
- **单管理员配置**：完全由 `.env` 中的 `ADMIN_ID` 控制，无硬编码。
- **巨人 (`/jr`) 全流程**：
  - 命中缓存秒回；未命中按积分降序选号（内置 60 秒内存积分缓存，批量查询不频繁刷 `/balance`）。
  - 免费号 60 秒独立冷却 + 北京时间每日 5 次额度；遇频率限制自动顺延冷却并换号。
  - 安全验证**优先自动 OCR 识别**（修复重复字切分与按钮索引匹配），失败**无缝转管理员私聊按钮点选**，等待期间不占用全局 RPC 信号量。
  - 结果**优先尝试点击「导出数据」**并自动识别会员/免费身份；免费号或导出失败自动降级本地翻页（最多 200 页）并导出 TXT。
- **核武器 (`/hw`) 全流程**：严格串行防串台，盒能不足自动切换下一个在线空闲协议号，支持文本与 TXT 导出及批量查询。
- **三目标自动签到 & 积分查询**：中核验 (`@zhonghybot`)、巨人 (`@jrsgk`)、核武器 (`@hewuqi`)，24 小时最小间隔自动签到，`/qd` 与 `/jf` 均带防重入锁。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux:   source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入 BOT_TOKEN / ADMIN_ID / API_ID / API_HASH
python main.py
```

## 管理员命令

| 命令 | 说明 |
|---|---|
| `/start` `/help` | 显示控制台菜单 |
| `/jr 内容` `/jr+内容` | 巨人查询（支持多行批量） |
| `/hw 内容` | 核武器私聊查询（支持多行批量） |
| `/qd` | 手动触发全账号签到 |
| `/jf` | 查询全账号三平台积分 |
| `/status` | 查看协议号在线、冷却、额度与签到状态 |
| `/login` `/cancel` | 私聊登录 / 取消登录协议号 |
