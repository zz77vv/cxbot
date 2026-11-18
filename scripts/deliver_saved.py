"""Explicitly send an operator-confirmed undelivered saved result. No userbot clients."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telegram import Bot
from telegram_bot.config import Config
from telegram_bot.logging_config import configure_logging
from telegram_bot.storage.database import Database
from telegram_bot.storage.repositories.query_repository import QueryRepository, result_from_row
from telegram_bot.storage.repositories.cache_repository import CacheRepository
from telegram_bot.services.jr.cache_service import CacheService
from telegram_bot.services.jr.export_service import ExportService
from telegram_bot.utils.process_lock import ProcessLock


async def deliver(task):
    cfg = Config.load()
    lock = ProcessLock(cfg.database)
    lock.acquire()
    handler = configure_logging(cfg)
    db = None
    try:
        db = await Database(cfg.database).open()
        repo = QueryRepository(db)
        row = await repo.get(task)
        result = result_from_row(row)
        if not row or row['state'] != 'success' or row['delivery_state'] != 'not_started' or not result:
            raise ValueError('结果不可重发；先核实并用 review_tasks 设置交付状态为 not_started')
        async with Bot(cfg.bot_token) as bot:
            exports = ExportService(cfg, None, repo, CacheService(cfg, CacheRepository(db)), bot)
            await exports.deliver(task, result)
        print('已交付保存的结果；没有重发查询或购买。')
    finally:
        if db:
            await db.close()
        handler.close()
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    args = parser.parse_args()
    asyncio.run(deliver(args.task))
