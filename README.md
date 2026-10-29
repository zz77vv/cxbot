# cxbot — Telegram 机器人 V2

> 这是当前源码的使用与维护说明，不是不可修改的历史需求清单。后续功能调整应以明确的新需求和实际代码为起点，评估影响、权限与安全边界后修改代码、测试和文档。

## 项目概览

Python 模块化 Telegram 机器人，使用 `python-telegram-bot`、`Telethon`、`aiosqlite`、`Pillow` 和 `ddddocr`。主要代码在 `telegram_bot/`，运行入口是 `main.py`。

- 管理三个目标机器人的签到和积分查询（目标规则见 `telegram_bot/target_bots.py`）。
- 处理 JR 查询、账号选择、会员/每日额度、结果缓存、文件导出与分页交付。
- 管理管理员私聊登录、Telethon Session、SQLite 任务状态及异常核实。
- 提供 `scripts/` 运维脚本、`tests/` 单元/集成测试。

## 快速开始

在项目根目录运行（推荐 Python 3.12）：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
python scripts/check_environment.py --ocr-smoke
python main.py
```

Windows 下虚拟环境激活命令为 `.venv\Scripts\activate`。配置文件 `.env` 应只在自己的运行环境中填写；所需配置项见 `.env.example`，包括 `BOT_TOKEN`、`ADMIN_ID`、`API_ID`、`API_HASH`。环境变量优先于 `.env`。

首次部署默认新建 SQLite 数据库，数据库、Session、缓存与日志路径由 `telegram_bot/config.py` 和 `.env` 决定。示例运行数据包括 `data/bot.db`、`sessions/`、`logs/` 和 `.jr_cache_files/`。不要把实际 `.env`、Session、数据库、缓存、日志或密钥提交到 GitHub。

## 管理员命令

| 命令 | 用途 |
|---|---|
| `/start`、`/help` | 菜单与帮助 |
| `/jr 内容`、`/jr+内容` | JR 查询 |
| `/qd` | 手动签到 |
| `/jf` | 查询积分 |
| `/status` | 查看运行状态 |
| `/login`、`/cancel` | 私聊登录 / 取消登录 |

当前命令在 `telegram_bot/handlers/registration.py` 注册；业务入口受管理员权限校验保护。

## 当前实现与运行边界

- 签到调度、积分解析、JR 账号选择、限额与缓存等具体参数以实际代码及配置为准。
- 对可能已收费、已提交或已交付但结果不明确的任务，当前实现会记录状态并等待核实，不盲目重复执行。
- 当前安全验证处理逻辑是转人工核实；OCR 模块可独立识别图片，但不代表已实现或允许绕过第三方访问控制。
- 当前账户与结果数据存储使用 SQLite。不要用删除运行数据或锁文件的方式绕过状态检查。

这些是**现有行为的描述**，不是禁止今后变更的永久规则。提出新功能时，应基于源码、实际服务能力及安全要求重新评估，而不是仅凭旧文档拒绝修改。

## 测试与状态

```bash
python -m compileall -q telegram_bot main.py scripts tests
python -m pytest -q --basetemp .test-tmp
python -m pip check
python scripts/check_environment.py --ocr-smoke
```

截至 **2026-10-28** 的仓库记录：离线测试报告记载 123 项通过；该记录不能证明真实 Telegram 登录、生产服务器运行或长期稳定性已完成验收。必要时重新执行上述检查并核对部署环境。`docs/test-results.xml` 是既有测试运行记录，不代表当前重新测试。

## 文档与维护

- [模块结构与修改入口](docs/MODULES.md)
- [部署、备份与回滚](docs/DEPLOYMENT.md)
- [管理员私聊登录](docs/LOGIN.md)

GitHub 源码位于私有仓库 `zz77vv/cxbot` 的 `main` 分支。发布前检查变更、依赖和测试；同步时避免强制覆盖其他提交。`docs/` 中的 JSON/XML 是历史映射、审计或测试数据，其中 `legacy_inventory.json`、`migration_map.json` 仍被测试或工具使用，不要与过期 Markdown 一并删除。

遇到问题时，先查 `telegram_bot/` 中的实际实现与异常日志（不记录敏感值）；需要人工核实或恢复的任务，使用 `scripts/review_tasks.py`、`scripts/deliver_saved.py`，操作细节见部署文档。
