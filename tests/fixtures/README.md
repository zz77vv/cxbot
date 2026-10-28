# 测试数据

测试图片在内存中生成，所有 Telegram 消息为模拟对象，Session 为临时伪文件。
SQLite 夹具按旧源码表结构在 pytest tmp_path 创建。没有真实用户查询、生产凭据、Session 密钥或生产数据库。
网络保护只允许 Windows asyncio socketpair 需要的本机回环连接；其他地址立即使测试失败。
