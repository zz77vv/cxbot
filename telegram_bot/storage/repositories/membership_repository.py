from datetime import datetime, timedelta, timezone


def usage_date(timestamp):
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).date().isoformat()


class MembershipRepository:
    def __init__(self, db):
        self.db = db

    async def state(self, account, now):
        row = await self.db.fetchone('SELECT status,checked_at FROM jr_membership WHERE phone=?', (account,))
        usage = await self.db.fetchone('SELECT query_count FROM jr_daily_usage WHERE phone=? AND usage_date=?', (account, usage_date(now)))
        reserved = await self.db.fetchone('SELECT COUNT(*) FROM query_tasks WHERE account=? AND reserved=1 AND usage_date=?', (account, usage_date(now)))
        return (row['status'] if row else 'unknown', row['checked_at'] if row else 0,
                (usage[0] if usage else 0) + reserved[0])
