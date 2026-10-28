# Telegram 机器人模块化 V2

## 当前部署方式（2026-10-28）

用户已确认采用全新数据库部署，不导入旧 bot.db。首次配置完成后程序会创建新库。
服务器源码目录为 `/root/cxbot`，独立虚拟环境为 `/root/cxbot/.venv`。
Ubuntu 24.04 / Python 3.12.3 上已通过 107 项离线测试、依赖检查和本地 OCR 模型调用。
目前尚未接入真实 Telegram 配置或创建 Session，机器人没有启动。

Session 需要部署者单独进行交互登录后保存到 `sessions/数字.session`；主程序只加载现有 Session，不会批量登录或向聊天索取登录验证码、两步验证密码。新部署不需要执行旧数据库迁移步骤。

GitHub 使用独立源码镜像，目标为私有仓库 `zz77vv/cxbot` 的 `main`。不上传 `.env`、Session、数据库、缓存、日志、服务器登录信息或运行环境。首次发布状态与后续同步规则见 [GitHub 发布说明](docs/GITHUB.md)。

基于用户提供的旧版单文件重构。保留四目标多账号签到、余额查询、JR 原有入口与结果交付、独立 OCR、会员/每日用量和 SQLite 数据；移除自动广告系统。V2 位于独立目录，未覆盖旧文件、数据库或 Session，未部署服务器。

Python 3.10–3.12，实际验证 Python 3.12.14 / Windows x64。依赖版本见 requirements.txt，完整实测环境见 requirements-lock.txt。Linux 服务器仍需独立安装和验收。

## 安装与配置

```bash
cd telegram_bot_v2
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python scripts/check_environment.py --ocr-smoke
cp .env.example .env
chmod 600 .env
```

Windows 激活命令为 `.venv\Scripts\Activate.ps1`；本次工作区实际虚拟环境在项目的上一级 `.venv`。

在服务器本地填写 `.env`：BOT_TOKEN、ADMIN_ID、API_ID、API_HASH。环境变量优先于 `.env`。不要将凭据发送到聊天中。源文件中发现硬编码凭据，建议管理员在本地评估必要的轮换；交付物没有复制其值。

相对路径始终相对于项目根目录，不受启动工作目录影响。默认运行数据：

```text
data/bot.db                  SQLite 数据和 V2 任务账本
data/bot.db.backup-*         每次打开已有库前的 SQLite 一致备份
data/bot.db.instance.lock    操作系统进程互斥锁（不要运行中删除）
sessions/数字.session       已授权的 Telethon SQLite Session
logs/bot.log                 20 MB 轮转，保留 5 个历史文件
.jr_cache_files/             结果文件和临时下载，禁止混放其他文件
```

可在 `.env` 设置 DATABASE_PATH、SESSIONS_DIR、LOG_PATH、CACHE_DIR、并发和超时等。四目标指令、正则、按钮和业务上限集中在 telegram_bot/target_bots.py；签到间隔、偏移、分页和缓存寿命保持原值。

旧数据先按 [升级和回滚步骤](docs/DEPLOYMENT.md) 制作副本并演练迁移。没有 Session 时 V2 仍可显示管理员状态，但不能执行账号业务；程序不会代为登录。未授权账号需部署者独立处理。

## 运行和停止

```bash
python main.py
```

Linux 支持 SIGTERM/SIGINT；Windows 使用 Ctrl+C。关闭时停止接收任务，等待在途请求最多 SHUTDOWN_TIMEOUT 秒，取消剩余请求并记录待核实状态，再关闭后台任务、Bot API、OCR、用户客户端和数据库。不要同时运行旧版和 V2 访问同一批 Session。不要通过删除锁文件启动第二个进程。

管理员命令：`/start`、`/help`、`/jr 内容`、`/jr+内容`、`/qd`、`/jf`、`/zt`、`/status`、`/unban 用户ID`。普通用户只能获得无开放功能提示；封禁用户被提前拦截。Bot API 菜单不允许 `+` 字符，故菜单注册 jr，帮助中同时说明 jr+。

## 关键行为

- 签到首次在未来 24 小时内随机；以后至少 24 小时，最多额外 15 分钟。手动签到同样检查间隔。失败不会写确认成功时间；历史 timestamp 仅作为兼容间隔基线。失败后的下一次写操作也至少间隔 24 小时，读取/连接采用有界退避。
- JR 使用实时积分降序、相同积分保持原账号顺序；会员不加排序权重。非会员或未知身份每日 5 次，北京时间换日。明确无结果回复也记一次完成；未知任务暂时预留额度并阻止该账号再次查询。
- 请求、购买和导出点击均不自动重试。Telegram FloodWait 持久化；任一账号仍处于 FloodWait 时暂停新的 JR 选号，不用其他账号绕开限制。
- 查询意图在发送前提交 SQLite。超时不能证明未执行；未知状态保留任务 ID、账号、阶段和消息 ID，暂停同键请求和相关账号并通知管理员。重启只恢复核实状态，不重发。
- 安全验证进入 manual 状态并通知管理员；不自动点击验证、不自动重发原请求。OCR 的原图片读取/预处理、beta 参数、置信度和离线 recognize_captcha 兼容接口保留，可独立调用测试。
- 文件优先导出；明确不能导出或等待文件超时后，对现有结果作分页回退；最多 200 页，重复或未前进立即停止。未知购买结果不会回退为另一轮购买。
- 缓存 TTL 300 秒，后台每 60 秒清理。未交付和待核实任务引用的文件可以超过 TTL 保留。丢失缓存文件进入人工处理，不自动重新购买。交付异常可能已经发送，必须先核实，不自动重复发送。
- 查询原文不写日志或任务账本；账本保存规范化 SHA256、必要账号/消息 ID。实际结果仍可能含敏感信息，应限制数据库、缓存、备份和日志的服务器访问权限。

## 测试

```bash
python -m compileall -q telegram_bot main.py scripts tests
python -m pytest -q --basetemp .test-tmp
python -m pip check
python scripts/check_environment.py --ocr-smoke
```

测试使用临时 SQLite、模拟 Session 和 Telegram 响应；外网连接被阻断，允许 Windows asyncio 所需本机回环。当前 Codex Windows 沙箱无法初始化 asyncio，所以本次完整异步测试在获准的沙箱外隔离环境执行。真实 Telegram、生产数据和 Linux 部署没有被验证；详见 [测试与交付报告](docs/TEST_REPORT.md)。

## 排错与维护

配置错误会以字段名报错且不回显值。账号 unauthorized：检查部署者提供的已授权 Session；error：看模块日志的异常类型；unavailable：等待持久化 FloodWait 结束。余额 format_changed 与零余额不同，需要依据实际合法响应修改对应目标正则并补充测试。

pending_review/manual/交付 unknown：先停止机器人，运行 `python scripts/review_tasks.py --database data/bot.db` 查看任务元信息，按 [人工核实流程](docs/DEPLOYMENT.md) 核实后处理。不要直接删除任务或清空缓存来绕过保护。

缓存目录外的旧绝对文件路径不会被下载、发送或删除，必须由部署者选择沿用原缓存根目录，或在副本上人工审计后重新映射路径。备份不自动清理，需设置服务器独立的容量监控和保留策略。

模块定位见 [维护与依赖说明](docs/MODULES.md)、[逐函数迁移表](docs/MIGRATION_MAP.md) 和 [改动清单](docs/CHANGES.md)。原检查报告见 [阶段 A](docs/AUDIT.md)。
