async def one(connection, sql, params=()):
    """Fetch a row inside a repository-owned transaction, closing the cursor."""
    async with connection.execute(sql, params) as cursor:
        return await cursor.fetchone()
