import time
import uuid
from ..sql import one
from .membership_repository import usage_date
from ...models.enums import UNRESOLVED, QueryState
from ...models.query import JRQueryResult


PLACEHOLDERS = ','.join('?' for _ in UNRESOLVED)


def result_from_row(row):
    if not row or not row['result_type']:
        return None
    return JRQueryResult(row['result_type'], row['result_text'] or '', row['file_path'],
                         row['file_name'], row['export_status'] or 'unknown')


class QueryRepository:
    def __init__(self, db):
        self.db = db

    async def claim(self, request_id, key, now):
        async with self.db.transaction() as c:
            old = await one(c, 'SELECT * FROM query_tasks WHERE request_id=?', (request_id,))
            if old:
                return old, False
            old = await one(c, f'SELECT * FROM query_tasks WHERE cache_key=? AND '
                            f'(state IN ({PLACEHOLDERS}) OR (state=\'success\' AND delivery_state<>\'delivered\')) ORDER BY created_at DESC LIMIT 1',
                            (key, *UNRESOLVED))
            if old:
                return old, False
            task = uuid.uuid4().hex
            await c.execute('INSERT INTO query_tasks(task_id,request_id,cache_key,state,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                            (task, request_id, key, 'prepared', 'prepared', now, now))
            return await one(c, 'SELECT * FROM query_tasks WHERE task_id=?', (task,)), True

    async def get(self, task):
        return await self.db.fetchone('SELECT * FROM query_tasks WHERE task_id=?', (task,))

    async def recent_metadata(self, limit=100):
        return await self.db.fetchall('SELECT task_id,account,state,phase,sent_id,source_id,delivery_state,delivery_cursor FROM query_tasks ORDER BY created_at DESC LIMIT ?', (limit,))

    async def unresolved_account(self, account, excluding=''):
        return await self.db.fetchone(f'SELECT task_id FROM query_tasks WHERE account=? AND task_id<>? AND state IN ({PLACEHOLDERS}) LIMIT 1',
                                     (account, excluding, *UNRESOLVED)) is not None

    async def cooldown_until(self, account):
        row = await self.db.fetchone('SELECT until_at FROM jr_query_cooldowns WHERE phone=?', (account,))
        return row[0] if row else 0

    async def refresh_cooldown(self, account, now):
        async with self.db.transaction() as c:
            membership = await one(c, 'SELECT status FROM jr_membership WHERE phone=?', (account,))
            if not membership or membership[0] != 'member':
                await c.execute('INSERT INTO jr_query_cooldowns VALUES(?,?) ON CONFLICT(phone) '
                                'DO UPDATE SET until_at=MAX(until_at,excluded.until_at)', (account, now + 60))

    async def defer_cooldown(self, task, account, seconds, now):
        """Only a confirmed rejection before purchase can return to scheduling."""
        async with self.db.transaction() as c:
            await c.execute('INSERT INTO jr_query_cooldowns VALUES(?,?) ON CONFLICT(phone) '
                            'DO UPDATE SET until_at=MAX(until_at,excluded.until_at)', (account, now + seconds))
            await c.execute("INSERT INTO jr_membership VALUES(?,'free',?) ON CONFLICT(phone) "
                            "DO UPDATE SET status='free',checked_at=excluded.checked_at", (account, now))
            await c.execute("UPDATE query_tasks SET state='prepared',phase='jr_cooldown',"
                            "reason='jr_query_cooldown',reserved=0,account=NULL,usage_date=NULL,"
                            "updated_at=? WHERE task_id=?", (now, task))

    async def reserve(self, task, account, now, limit=5, *, cooldown_seconds=0):
        async with self.db.transaction() as c:
            cooldown = await one(c, 'SELECT until_at FROM jr_query_cooldowns WHERE phone=?', (account,))
            if cooldown and cooldown[0] > now:
                return False
            blocked = await one(c, f'SELECT task_id FROM query_tasks WHERE account=? AND task_id<>? AND state IN ({PLACEHOLDERS}) LIMIT 1',
                                (account, task, *UNRESOLVED))
            if blocked:
                return False
            membership = await one(c, 'SELECT status FROM jr_membership WHERE phone=?', (account,))
            day = usage_date(now)
            used = await one(c, 'SELECT query_count FROM jr_daily_usage WHERE phone=? AND usage_date=?', (account, day))
            held = await one(c, 'SELECT COUNT(*) FROM query_tasks WHERE account=? AND usage_date=? AND reserved=1', (account, day))
            if (not membership or membership[0] != 'member') and (used[0] if used else 0) + held[0] >= limit:
                return False
            await c.execute("UPDATE query_tasks SET account=?,usage_date=?,reserved=1,state='submitting',phase='submitting',updated_at=? WHERE task_id=?", (account, day, now, task))
            if cooldown_seconds and (not membership or membership[0] != 'member'):
                await c.execute('INSERT INTO jr_query_cooldowns VALUES(?,?) ON CONFLICT(phone) '
                                'DO UPDATE SET until_at=MAX(until_at,excluded.until_at)', (account, now + cooldown_seconds))
            return True

    async def transition(self, task, state, reason='', *, sent_id=None, source_id=None):
        async with self.db.transaction() as c:
            await c.execute('UPDATE query_tasks SET state=?,phase=?,reason=?,updated_at=?,sent_id=COALESCE(?,sent_id),source_id=COALESCE(?,source_id),reserved=CASE WHEN ?=\'failed\' THEN 0 ELSE reserved END WHERE task_id=?',
                            (state.value, state.value, reason, time.time(), sent_id, source_id, state.value, task))

    async def complete(self, task, result, now, ttl, *, from_cache=False):
        async with self.db.transaction() as c:
            row = await one(c, 'SELECT * FROM query_tasks WHERE task_id=?', (task,))
            if row['state'] == 'success':
                return
            account = row['account'] or ''
            if row['reserved'] and not from_cache:
                await c.execute('INSERT INTO jr_daily_usage VALUES(?,?,1) ON CONFLICT(phone,usage_date) DO UPDATE SET query_count=query_count+1', (account, row['usage_date']))
                if result.export_status in ('member', 'free'):
                    await c.execute('INSERT INTO jr_membership VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at', (account, result.export_status, now))
                    if result.export_status == 'member':
                        await c.execute('DELETE FROM jr_query_cooldowns WHERE phone=?', (account,))
            await c.execute("UPDATE query_tasks SET state='success',phase='complete',reason='',reserved=0,result_type=?,result_text=?,file_path=?,file_name=?,export_status=?,updated_at=? WHERE task_id=?",
                            (result.kind, result.text, result.file_path, result.file_name, result.export_status, now, task))
            if not from_cache:
                await c.execute('INSERT INTO jr_result_cache(cache_key,result_text,source_phone,created_at,expires_at,result_type,file_path,file_name) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET result_text=excluded.result_text,source_phone=excluded.source_phone,created_at=excluded.created_at,expires_at=excluded.expires_at,result_type=excluded.result_type,file_path=excluded.file_path,file_name=excluded.file_name',
                                (row['cache_key'], result.text, account, now, now + ttl, result.kind, result.file_path, result.file_name))

    async def attach_file(self, task, path, name):
        async with self.db.transaction() as c:
            await c.execute('UPDATE query_tasks SET file_path=?,file_name=? WHERE task_id=?', (path, name, task))

    async def delivery(self, task, state, cursor=None):
        async with self.db.transaction() as c:
            await c.execute('UPDATE query_tasks SET delivery_state=?,delivery_cursor=COALESCE(?,delivery_cursor),delivered_at=CASE WHEN ?=\'delivered\' THEN ? ELSE delivered_at END WHERE task_id=?',
                            (state, cursor, state, time.time(), task))

    async def recover(self):
        """Keep intent/message IDs. Recovery NEVER submits or clicks again."""
        async with self.db.transaction() as c:
            await c.execute("UPDATE query_tasks SET state='failed',reason='restart_before_submission',reserved=0 WHERE state='prepared'")
            await c.execute("UPDATE query_tasks SET phase=state,state='pending_review',reason='restart_after_intent' WHERE state IN ('submitting','submitted','purchasing','exporting','collecting','unknown')")
            await c.execute("UPDATE query_tasks SET delivery_state='unknown' WHERE delivery_state='sending'")
            async with c.execute("SELECT task_id,account,state,phase,delivery_state FROM query_tasks WHERE state IN ('pending_review','manual') OR delivery_state='unknown'") as cur:
                return await cur.fetchall()

    async def review(self, task, decision, reason):
        if decision not in {'failed', 'success', 'manual'} or not reason.strip():
            raise ValueError('需要人工核实结论和原因')
        async with self.db.transaction() as c:
            row = await one(c, 'SELECT * FROM query_tasks WHERE task_id=?', (task,))
            if not row:
                raise ValueError('任务不存在')
            if row['state'] in ('success', 'failed'):
                raise ValueError('任务已终结')
            if decision == 'success' and row['reserved']:
                await c.execute('INSERT INTO jr_daily_usage VALUES(?,?,1) ON CONFLICT(phone,usage_date) DO UPDATE SET query_count=query_count+1', (row['account'], row['usage_date']))
            await c.execute('UPDATE query_tasks SET state=?,reason=?,reserved=CASE WHEN ?=\'manual\' THEN reserved ELSE 0 END,updated_at=? WHERE task_id=?', (decision, reason, decision, time.time(), task))
