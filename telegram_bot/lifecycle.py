import asyncio
import logging

log = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self, stop_event=None):
        self.tasks = set()
        self.stop_event = stop_event
        self.failure = None

    def start(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            self.failure = type(task.exception()).__name__
            log.error('Background task failed name=%s error_type=%s', task.get_name(), type(task.exception()).__name__)
            if self.stop_event:
                self.stop_event.set()

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


class Lifecycle:
    def __init__(self, ctx, bot, queries):
        self.ctx, self.bot, self.queries = ctx, bot, queries
        self.stop_event = asyncio.Event()
        self.registry = TaskRegistry(self.stop_event)
        self.closed = False

    async def run(self):
        try:
            recovered = await self.queries.recover()
            await self.ctx.cache.repository.clear_stale_leases()
            await self.ctx.accounts.start()
            await self.bot.start()
            for row in recovered:
                await self.ctx.jr.exports.notify(row['task_id'], row['state'], 'restart_requires_review')
            await self.ctx.cache.cleanup()
            self.ctx.checkins.start(self.registry)
            self.registry.start(self.ctx.accounts.monitor(), 'account-monitor')
            self.registry.start(self.ctx.cache.loop(), 'cache-cleanup')
            log.info('V2 started accounts=%d', len(self.ctx.accounts.accounts))
            await self.stop_event.wait()
            if self.registry.failure:
                raise RuntimeError('critical_background_task_failed')
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.ctx.jr.accepting = False
        self.ctx.requests.accepting = False
        try:
            await self.bot.stop_polling()
        except Exception as error:
            log.warning('Stop polling error_type=%s', type(error).__name__)
        active = tuple(self.ctx.jr.active | self.ctx.requests.active)
        if active:
            _, pending = await asyncio.wait(active, timeout=self.ctx.config.shutdown_timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
        await self.registry.close()
        for name, close in [('bot', self.bot.close), ('ocr', self.ctx.ocr.close),
                             ('accounts', self.ctx.accounts.close), ('database', self.ctx.database.close)]:
            try:
                await close()
            except Exception as error:
                log.error('Resource close failed resource=%s error_type=%s', name, type(error).__name__)
