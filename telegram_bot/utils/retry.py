import asyncio


async def retry_safe(operation, *, attempts=3, base=2, retry_on=(ConnectionError, OSError)):
    """Only for reads/connections explicitly safe to repeat; never query/buy/send."""
    for attempt in range(attempts):
        try:
            return await operation()
        except retry_on:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(base * 2 ** attempt)
