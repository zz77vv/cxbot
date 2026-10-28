class UserRepository:
    def __init__(self, db):
        self.db = db

    async def is_banned(self, user_id):
        return await self.db.fetchone('SELECT 1 FROM banned_users WHERE user_id=?', (user_id,)) is not None
