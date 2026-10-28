# 数据备份、迁移、部署和回滚

本次只构建本地 V2，没有连接服务器、替换生产进程或打开生产数据。以下命令由部署者在服务器确认后执行。先做演练，再安排维护窗口；仅服务停止期间复制 Session。

## 数据库演练

1. 使用 Python 3.12 的独立虚拟环境安装依赖。先运行离线测试和环境检查。
2. 制作一致性快照，目标必须是全新的文件：

```bash
python scripts/snapshot_database.py --source /srv/legacy/bot.db --destination /srv/v2-rehearsal/bot.db
python scripts/migrate_database.py --database-copy /srv/v2-rehearsal/bot.db
```

snapshot 使用 SQLite backup，源连接为 mode=ro，不用直接复制正在写入的 .db 文件。目标存在时拒绝覆盖。迁移脚本取得进程锁，打开副本前再生成 .backup-时间-随机值。迁移仅支持源码中确认的旧表、主键及字段，未知结构停止；不得为了迁移成功手动删除未知表。

3. 核对五类旧业务表行数、抽样记录和缓存文件引用。检查 last_sign_time 单 phone 表是否展开四目标；历史 timestamp 不会变成 V2 的“确认成功”时间。迁移不删除历史用量、会员、封禁或过期缓存记录；缓存清理由运行期独立执行。
4. schema_migrations 当前为 1。新增 checkin_state、checkin_attempts、query_tasks、file_leases、account_cooldowns；旧缓存缺失 result_type/file_path/file_name 时仅增加字段。事务失败回滚。高于 1 的版本拒绝降级运行。

## 切换

停止旧进程，确认退出；保存旧代码、私有配置、数据库一致快照、Session 完整副本以及缓存文件。不要将这些运行数据加入 Git 或交付压缩包。Windows/服务器文件权限由部署者设置。

在 V2 独立目录配置 .env 指向数据库副本、Session 副本以及专用缓存目录。Session 格式保持原 Telethon SQLite 格式，不转换、不重登录、不批量迁移；只接收数字文件名。首次验证使用副本，且同一 Session 不得与旧版并行运行。

旧缓存保存绝对路径；有两种经审核的部署方式：沿用该专用缓存目录并配置 CACHE_DIR；或复制到新的专用根目录并在数据库副本中逐条核验/重新映射。V2 不猜测旧路径、不删除根目录外文件。不要在未迁移文件引用时启动缓存清理。

```bash
python scripts/check_environment.py --ocr-smoke
python main.py
```

首次接入生产 Telegram 前，核对管理员 ID、四个目标、Session 路径、实际消息格式和管理员菜单。先检查 /status，不通过真实批量或付费查询来替代离线测试。真实签到和查询由管理员在授权范围内验收。

Linux 可使用 systemd（修改路径和用户后由部署者安装）：

```ini
[Unit]
Description=Telegram Bot V2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=telegrambot
WorkingDirectory=/srv/telegram_bot_v2
ExecStart=/srv/telegram_bot_v2/.venv/bin/python /srv/telegram_bot_v2/main.py
Restart=on-failure
RestartSec=15
KillSignal=SIGTERM
TimeoutStopSec=120
UMask=0077

[Install]
WantedBy=multi-user.target
```

systemd 模板未在当前 Windows 环境执行验证。正式部署安装运行依赖即可；测试依赖单独用于演练。不要将服务环境或 Token 写入 systemd 公共日志。日志 20 MB × (当前 + 5 历史)，备份和未核实结果没有自动过期删除，需监控磁盘。

## 人工核实与结果交付

关机前后遇到不明确任务都不自动重发。任务元数据可恢复；未写入成功账本的外部付费结果必须人工查目标机器人记录。OCR 安全验证应由授权管理员在对应会话手动完成，并核实原请求是否收费；不要简单再次发送原请求。

停止 V2 后查看最近 100 个任务（不打印查询原文和结果）：

```bash
python scripts/review_tasks.py --database data/bot.db
```

确实确认没有执行/没有费用后才能标为 failed 释放预留；确认已执行则标为 success，原子累计已预留用量；仍不确定保留 manual。reason 不要写查询原文或凭据。

```bash
python scripts/review_tasks.py --database data/bot.db --task TASK_ID --decision failed --reason verified_no_execution
python scripts/review_tasks.py --database data/bot.db --task TASK_ID --decision success --reason verified_completed_upstream
```

人工标为 success 不会伪造、下载或自动发送结果。查询成功但交付 unknown 时核对管理员消息：已收到可标 delivered；明确未收到（包括分段发送部分失败时已自行整理收件情况）才可设置 not_started 并显式重发保存结果：

```bash
python scripts/review_tasks.py --database data/bot.db --task TASK_ID --delivery delivered
python scripts/review_tasks.py --database data/bot.db --task TASK_ID --delivery not_started
python scripts/deliver_saved.py --task TASK_ID
```

deliver_saved 使用同目录 .env，只发已保存结果给配置的管理员，不创建 Telethon 用户客户端、不查询、不购买。务必确认不是部分已收到后又重复全量发送；分段进度 delivery_cursor 在账本保留。缺失文件和没有保存结果的任务不能由此重建。所有维护脚本与主进程共用数据库进程锁。

## 回滚

停止 V2，保存当前 V2 数据、日志和待核实任务清单。外部付费状态不能随数据库回滚，先核实所有 submitting/submitted/purchasing/exporting/collecting/pending_review/manual 以及交付 unknown。

恢复此前保留的旧代码和私有配置，使用切换前一致快照及对应 Session/缓存副本。离线恢复时用全新目录；不要覆盖唯一备份，也不要把旧 .db 与 V2 的 -wal/-shm 文件混用。不要将 V2 库直接交给旧代码运行。

V2 新版本升级同样先停止、快照、在副本上迁移和测试，再切换。回滚后旧版仍有本报告指出的重复付费和签到误判问题，不能把回滚当作问题已经修复。
