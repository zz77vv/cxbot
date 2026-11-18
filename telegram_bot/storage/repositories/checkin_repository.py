from ..sql import one


class CheckinRepository:
    def __init__(self, db):
        self.db = db

    async def get(self, account, bot):
        return await self.db.fetchone('SELECT * FROM checkin_state WHERE phone=? AND bot=?', (account, bot))

    async def initialize(self, account, bot, now, rng):
        async with self.db.transaction() as c:
            row = await one(c, 'SELECT * FROM checkin_state WHERE phone=? AND bot=?', (account, bot))
            if row:
                return row
            old = await one(c, 'SELECT timestamp FROM last_sign_time WHERE phone=? AND lower(trim(bot))=?', (account, bot))
            next_at = now + rng.uniform(0, 86400) if not old else max(
                now + rng.uniform(1, 5), old[0] + 86400 + rng.uniform(0, 900))
            await c.execute("INSERT INTO checkin_state(phone,bot,next_at,outcome) VALUES(?,?,?,'unknown')", (account, bot, next_at))
            return await one(c, 'SELECT * FROM checkin_state WHERE phone=? AND bot=?', (account, bot))

    async def attempted(self, account, bot, now):
        async with self.db.transaction() as c:
            await c.execute("UPDATE checkin_state SET last_attempt=?,outcome='pending_review',reason='attempt_in_progress',next_at=? WHERE phone=? AND bot=?",
                            (now, now + 86400, account, bot))

    async def finish(self, account, bot, now, result, next_at):
        async with self.db.transaction() as c:
            await c.execute('INSERT INTO checkin_attempts(phone,bot,attempted_at,outcome,error_type,reason) VALUES(?,?,?,?,?,?)',
                            (account, bot, now, result.outcome.value, result.error_type, result.reason))
            await c.execute('UPDATE checkin_state SET outcome=?,reason=?,error_type=?,next_at=? WHERE phone=? AND bot=?',
                            (result.outcome.value, result.reason, result.error_type, next_at, account, bot))
            if result.outcome.value == 'success':
                await c.execute('UPDATE checkin_state SET last_success=? WHERE phone=? AND bot=?', (now, account, bot))
                await c.execute('INSERT INTO last_sign_time VALUES(?,?,?) ON CONFLICT(phone,bot) DO UPDATE SET timestamp=excluded.timestamp', (account, bot, now))

    async def legacy_baseline(self, account, bot):
        row = await self.db.fetchone('SELECT timestamp FROM last_sign_time WHERE phone=? AND lower(trim(bot))=?', (account, bot))
        return row[0] if row else None
