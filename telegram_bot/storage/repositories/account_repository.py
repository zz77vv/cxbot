class AccountRepository:
    def __init__(self, db):
        self.db = db

    async def cooldowns(self):
        return await self.db.fetchall('SELECT phone,until_at FROM account_cooldowns')

    async def set_cooldown(self, account, until):
        async with self.db.transaction() as c:
            await c.execute('INSERT INTO account_cooldowns VALUES(?,?) ON CONFLICT(phone) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)', (account, until))
