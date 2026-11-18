# 管理员私聊登录

所有业务入口仅允许配置管理员 `683298845`，配置加载拒绝其他管理员 ID。登录还要求私聊且 chat_id 等于管理员 ID。
命令菜单：start、help、jr（兼容快捷语法）、qd、jf、status、login、cancel。
保留的三个目标由 target_bots.py 统一配置；封禁表保留，不提供解除封禁入口。

在配置 BOT_TOKEN、API_ID、API_HASH 并经部署者确认启动后，管理员私聊发送 `/login`。
依次输入国际格式手机号、Telegram 发来的验证码；如要求两步验证，再输入密码。
不要在 Codex 聊天或群聊提供这些登录信息。机器人私聊不是端到端加密，输入消息会尝试立即删除，但删除不能保证收件通知或其他设备副本消失。
Telegram 可能使在聊天中发送的登录验证码失效；本功能不绕过该保护，失败后退出，需重新登录或改用授权的服务器本地交互流程。

LOGIN_TTL 默认 300 秒，期限从发起登录开始计算。每个管理员私聊同时仅一个流程，重复发起要求先取消；输入处理串行且更新 ID 去重。
错误号码、错误/过期验证码、密码错误、RPC/网络错误和授权不一致都退出并断开临时客户端；限流期间禁止重新发起。
使用 `/cancel` 主动退出。进程退出清理所有未完成流程；重启不恢复验证码或密码，也不自动重发登录。

验证码、两步验证密码只作为瞬时内存参数，不写业务 SQLite、日志、源码或 Session。
临时客户端使用 MemorySession；授权状态和账号手机号验证通过后，将授权密钥写入 sessions/.pending/ 内的随机文件，SQLite 关闭后以原子硬链接发布为数字手机号.session。
已有目标文件时系统拒绝发布。文件权限为 600，临时目录 700；硬链接不支持的文件系统会安全失败，不退回覆盖保存。
随后账号管理器用保存文件重新连接并检查 is_user_authorized，成功才提示登录成功并增量创建签到任务。
若发布后连接失败，保留已经授权的文件供 /status 或重启核实，不删除或覆盖它。
重启加载忽略隐藏临时目录；Session 仍是标准 Telethon SQLite 格式。

服务器应将 SESSIONS_DIR 设为 `/root/cxbot/sessions/`。源码仓库和发布允许清单不包含任何 Session 或私有 .env。
截至 2026-10-28 的历史验证仅覆盖模拟 Telegram 响应和临时 SQLite Session；是否已完成真实 Telegram 登录或服务器验收，应以实际运行记录为准。
接口依据：[Telethon 授权接口](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.auth.AuthMethods.sign_in) 与 [Session 说明](https://docs.telethon.dev/en/stable/concepts/sessions.html)。

首次使用新机器人时先在管理员私聊发送 `/start` 建立聊天。菜单若因 Chat not found 无法注册，会延后到该私聊建立后补齐；不会阻止 polling 启动。
