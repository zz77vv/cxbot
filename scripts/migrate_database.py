"""Explicit migration of an operator-chosen COPY; does not connect to Telegram."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telegram_bot.storage.database import Database
from telegram_bot.utils.process_lock import ProcessLock


async def migrate_copy(path):
    lock = ProcessLock(path)
    lock.acquire()
    try:
        db = await Database(path).open()
        try:
            print('迁移成功，schema=1；迁移前备份：', db.backup_path)
        finally:
            await db.close()
    finally:
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database-copy', type=Path, required=True)
    args = parser.parse_args()
    if not args.database_copy.is_file():
        parser.error('必须指定已存在的数据库副本')
    asyncio.run(migrate_copy(args.database_copy.resolve()))
