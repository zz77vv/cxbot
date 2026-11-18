"""Offline operator reconciliation. Stop the bot and verify upstream records first."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telegram_bot.storage.database import Database
from telegram_bot.storage.repositories.query_repository import QueryRepository
from telegram_bot.utils.process_lock import ProcessLock


async def review(args):
    lock = ProcessLock(args.database)
    lock.acquire()
    try:
        db = await Database(args.database).open()
        try:
            repo = QueryRepository(db)
            if args.task:
                if args.delivery:
                    row = await repo.get(args.task)
                    if not row or row['state'] != 'success':
                        raise ValueError('仅能核实已成功查询任务的交付状态')
                    await repo.delivery(args.task, args.delivery)
                else:
                    await repo.review(args.task, args.decision, args.reason or '')
                print('人工结论已记录；未执行 Telegram 请求或结果重发。')
            else:
                rows = await repo.recent_metadata()
                for row in rows:
                    print(dict(row))
        finally:
            await db.close()
    finally:
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--task')
    parser.add_argument('--decision', choices=('failed', 'success', 'manual'))
    parser.add_argument('--reason')
    parser.add_argument('--delivery', choices=('delivered', 'not_started'))
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error('数据库不存在')
    if args.task and not (args.decision or args.delivery):
        parser.error('需要人工结论或交付结论')
    if args.task and args.decision and not args.reason:
        parser.error('需要核实原因 --reason')
    asyncio.run(review(args))
