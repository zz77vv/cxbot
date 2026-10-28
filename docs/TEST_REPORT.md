# 测试与交付报告

验收日期：2026-10-28。实现位于独立 telegram_bot_v2 目录，工作区上一级 .venv 为本次隔离运行环境。

## 已完成并通过测试

最终完整离线测试：**107 passed in 2.83s**，使用 pytest 8.4.2、pytest-asyncio 1.2.0，`-W error`。原始记录在 test-results.xml，不以语法检查替代功能测试。

```text
python -m pytest -q --basetemp .test-tmp-delivery --tb=short --junitxml docs/test-results.xml -W error
107 passed in 2.83s
```

前序阶段执行过基础 49 项、扩展 94/95 项、边界 103 项、关闭与 Session 105 项等验证；测试发现并修复了 Windows 文件锁重复读取、存储迁移导入环、未交付成功任务重启后可能重新购买的漏洞。首次沙箱运行因临时目录/本地 socketpair 权限失败或挂起，不计为通过；后续将临时目录限于工作区，并在获准的沙箱外虚拟环境运行，测试仍阻断外网。

| 验收项目 | 已执行证据 |
|---|---|
| Python 语法和模块导入 | compileall；全部包导入；AST 无循环依赖 |
| 配置 | 缺项、错误类型、零/负值、NaN、凭据格式、.env 优先级、路径交叉校验 |
| 数据库旧结构 | 临时 phone 主键及 phone+bot 兼容迁移；重复打开；一致备份；缓存增列；未知表/字段拒绝且字节不变 |
| 数据保留 | 会员、历史日期用量、封禁、签到记录重新打开后保留 |
| 并发/事务 | 账号操作串行、请求并发、事务异常/取消回滚、数据库重新打开、进程锁互斥 |
| Session | 临时数字文件发现；非数字跳过；启动失败隔离；未授权状态；真实 Telethon SQLiteSession 临时文件格式/构造兼容及加载字节不变 |
| 签到 | 成功、超时、异常、按钮缺失、成功时间不误写、24h 限制、首次计划持久化、任务去重和取消 |
| 余额 | 四个原正则、零值、格式变化/超时区分、持久化 FloodWait |
| JR 入口 | 两种语法、空参数、大小写、bot mention；实际 PTB check_update 只匹配一个业务处理器 |
| JR 状态 | 首回复超时、发送超时、购买超时、未知格式、安全验证人工暂停、明确失败、明确无结果、重启保护、同键去重、额度预留 |
| 账号选择 | 实时积分降序；FloodWait 不换号重试；每日限额原子检查；北京时间换日 |
| 文件和分页 | 导出一次、文件关联、下载/发送、免费/超时/下载失败回退、缺失文件异常、重复页、未前进/200页限制 |
| 缓存和清理 | 同键缓存复用、不重复累计用量、有效引用/任务/交付 lease 保护、无主文件清理、根目录外文件不删除 |
| 消息格式 | 超长单行、HTML 特殊字符、emoji UTF-16 长度、空行、结果标题整理 |
| OCR | 模块导入、模拟图片、旧概率/tuple 适配、置信度、错误图片、超时后 busy 防积压 |
| 权限/命令 | 非管理员无法调用业务、封禁拦截、解封、菜单/注册/帮助的共用清单 |
| 生命周期 | 所有在途请求追踪、任务取消、幂等关闭、部分 Bot 初始化失败、后台异常触发退出 |
| 删除功能 | 运行源码无旧广告处理器/任务/状态/群组；没有 /dg 或 /tz 注册 |
| 模块迁移 | 105 个旧定义逐一映射，目标文件存在；无散落业务 SQL、无 import * |

语法检查命令：`python -m compileall -q telegram_bot main.py scripts tests`，已通过。依赖检查：`python -m pip check` 返回 `No broken requirements found.`

运行依赖实测：Python 3.12.14，Telethon 1.42.0，python-telegram-bot 22.5，aiosqlite 0.21.0，Pillow 11.3.0，ddddocr 1.5.6，onnxruntime 1.31.1。完整含测试依赖的版本快照在 requirements-lock.txt；没有推断服务器原版本，也没有直接升级服务器环境。

独立真实 OCR 检查 `python scripts/check_environment.py --ocr-smoke` 已执行。所有列出的库真实导入成功；ddddocr beta 模型在本地生成的 1234 图片上完成推理，返回 LowConfidence，按 0.35 阈值拒绝作为可靠识别。该结果证明模型可运行，不证明真实图像识别准确率。

源码只读哈希核对、原凭据内存匹配扫描及压缩包允许清单审核见 release_audit.json；文件 SHA256 清单见 release_manifest.json。原源码未覆盖。没有提供生产 DB 或 Session，因此不能声称对其做过逐字节验收；本次没有打开它们。

## 已完成但未能充分测试

- 四目标真实消息/按钮/文件格式：均按旧源码迁移，并用模拟响应验证；没有真实 Telegram 网络验收。
- 断网重连和 FloodWait：验证了模拟异常和持久化逻辑，没有触发真实限流。
- 生产库兼容性：仅根据旧源码创建夹具，真实结构须部署者在副本上验证。
- OCR：真实引擎已运行，生产图片质量和准确率未验证；第三方安全验证按需求人工处理。
- Linux、systemd、长期压力及磁盘容量：提供模板和说明，当前 Windows 环境不能证明服务器长期稳定性。
- Telegram 无幂等保证的外部收费/交付：意图账本能阻止自动重做，不能凭网络超时证明外部事务结果。

## 尚未完成

- 真实 Telegram 端到端验收、生产数据迁移演练、服务器切换和持续运行观测尚未执行。
- GitHub 同步未执行：工作区没有 Git 仓库、目标远程、分支或独立脱敏镜像/同步配置；遵守上级 AGENTS.md，没有猜测或新建仓库。无已验证远程 SHA 可报告。

## 需要人工确认

- 服务器配置、备份位置、数据库副本结构、Session 副本和缓存绝对路径映射。
- 硬编码旧凭据是否需要轮换；新凭据只在服务器本地填写。
- 未知格式、验证暂停、未核实收费和交付 unknown 的最终状态；操作步骤在 DEPLOYMENT.md。
- OCR 原生线程若永久卡死，Python 不能安全强行终止该线程；超时后拒绝继续排队，需要进程监督器处理。备份、任务元数据和未交付结果没有自动删除策略，需要服务器容量管理。

没有尚未披露的占位业务函数；交付为可离线验证的本地源码版本，不能将测试通过表述为生产上线验收通过。

## 接口参考

实现时核对了 [Telethon Session 说明](https://docs.telethon.dev/en/stable/concepts/sessions.html)、[PTB Application 生命周期文档](https://docs.python-telegram-bot.org/en/v22.5/telegram.ext.application.html) 和 [ddddocr 项目发布信息](https://pypi.org/project/ddddocr/1.5.6/)。最终兼容性判断以本次实际安装版本、包内实现检查和离线测试为准。
