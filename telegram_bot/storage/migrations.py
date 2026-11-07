from .sql import one
from ..target_bots import TARGETS


class SchemaError(ValueError):
    """Safe startup diagnostic containing internal schema names only."""
    pass


LEGACY_COLUMNS = {
    'last_sign_time': {'phone', 'timestamp'},
    'jr_membership': {'phone', 'status', 'checked_at'},
    'jr_daily_usage': {'phone', 'usage_date', 'query_count'},
    'jr_result_cache': {'cache_key', 'result_text', 'source_phone', 'created_at', 'expires_at'},
    'banned_users': {'user_id', 'reason', 'banned_at'},
}


async def columns(conn, table):
    # Only constant internal table names reach this function.
    async with conn.execute(f'PRAGMA table_info({table})') as cur:
        return await cur.fetchall()


async def validate_legacy(conn):
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        tables = {row[0] for row in await cur.fetchall()}
    allowed = set(LEGACY_COLUMNS) | {'schema_migrations', 'checkin_state', 'checkin_attempts',
                                     'query_tasks', 'file_leases', 'account_cooldowns', 'jr_query_cooldowns'}
    unknown = {t for t in tables if not t.startswith('sqlite_')} - allowed
    if unknown:
        raise SchemaError('数据库包含未知表；停止迁移，请人工审核结构')
    for table, required in LEGACY_COLUMNS.items():
        if table not in tables:
            continue
        info = await columns(conn, table)
        if not required <= {r[1] for r in info}:
            raise SchemaError(f'{table} 结构不兼容；停止迁移')
        optional = {'last_sign_time': {'bot'}, 'jr_result_cache': {'result_type', 'file_path', 'file_name'}}
        if {r[1] for r in info} - required - optional.get(table, set()):
            raise SchemaError(f'{table} 存在未知字段；停止迁移')
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        expected = {'jr_membership': ['phone'], 'jr_daily_usage': ['phone', 'usage_date'],
                    'jr_result_cache': ['cache_key'], 'banned_users': ['user_id']}
        if table == 'last_sign_time':
            if pk not in (['phone'], ['phone', 'bot']):
                raise SchemaError('last_sign_time 主键不兼容')
        elif pk != expected[table]:
            raise SchemaError(f'{table} 主键不兼容')
    if 'schema_migrations' in tables:
        row = await one(conn, 'SELECT MAX(version) FROM schema_migrations')
        version = row[0] or 0
        if version > 2:
            raise SchemaError('数据库版本高于当前程序，禁止降级运行')
        expected = {
            'schema_migrations': {'version'},
            'checkin_state': {'phone','bot','last_attempt','last_success','next_at','outcome','error_type','reason'},
            'checkin_attempts': {'id','phone','bot','attempted_at','outcome','error_type','reason'},
            'query_tasks': {'task_id','request_id','cache_key','account','state','phase','created_at','updated_at','usage_date','reason','sent_id','source_id','result_type','result_text','file_path','file_name','export_status','delivery_state','delivery_cursor','delivered_at','reserved'},
            'file_leases': {'path','owner'},
            'account_cooldowns': {'phone','until_at'},
        }
        if version >= 2:
            expected['jr_query_cooldowns'] = {'phone', 'until_at'}
        for table, required in expected.items():
            if table not in tables or {r[1] for r in await columns(conn, table)} != required:
                raise SchemaError(f'{table} V2 结构不兼容；停止迁移')


async def migrate(db):
    async with db.transaction() as conn:
        await conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)')
        info = await columns(conn, 'last_sign_time')
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        if info and pk != ['phone', 'bot']:
            await conn.execute('ALTER TABLE last_sign_time RENAME TO last_sign_time_legacy_v1')
            await conn.execute('CREATE TABLE last_sign_time (phone TEXT NOT NULL, bot TEXT NOT NULL, '
                               'timestamp REAL NOT NULL, PRIMARY KEY(phone,bot))')
            if 'bot' in {r[1] for r in info}:
                await conn.execute('INSERT INTO last_sign_time SELECT phone,lower(trim(bot)),timestamp '
                                   'FROM last_sign_time_legacy_v1 WHERE bot IS NOT NULL AND timestamp IS NOT NULL')
            else:
                for target in TARGETS:
                    await conn.execute('INSERT INTO last_sign_time SELECT phone,?,timestamp '
                                       'FROM last_sign_time_legacy_v1 WHERE timestamp IS NOT NULL', (target.key,))
            await conn.execute('DROP TABLE last_sign_time_legacy_v1')
        statements = [
            'CREATE TABLE IF NOT EXISTS last_sign_time (phone TEXT NOT NULL,bot TEXT NOT NULL,timestamp REAL NOT NULL,PRIMARY KEY(phone,bot))',
            "CREATE TABLE IF NOT EXISTS jr_membership (phone TEXT PRIMARY KEY,status TEXT NOT NULL CHECK(status IN ('member','free')),checked_at REAL NOT NULL)",
            'CREATE TABLE IF NOT EXISTS jr_daily_usage (phone TEXT NOT NULL,usage_date TEXT NOT NULL,query_count INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(phone,usage_date))',
            "CREATE TABLE IF NOT EXISTS jr_result_cache (cache_key TEXT PRIMARY KEY,result_text TEXT NOT NULL,source_phone TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,result_type TEXT NOT NULL DEFAULT 'text',file_path TEXT,file_name TEXT)",
            'CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY,reason TEXT NOT NULL,banned_at REAL NOT NULL)',
            'CREATE TABLE IF NOT EXISTS checkin_state (phone TEXT NOT NULL,bot TEXT NOT NULL,last_attempt REAL,last_success REAL,next_at REAL NOT NULL,outcome TEXT NOT NULL,error_type TEXT NOT NULL DEFAULT \'\',reason TEXT NOT NULL DEFAULT \'\',PRIMARY KEY(phone,bot))',
            'CREATE TABLE IF NOT EXISTS checkin_attempts (id INTEGER PRIMARY KEY,phone TEXT NOT NULL,bot TEXT NOT NULL,attempted_at REAL NOT NULL,outcome TEXT NOT NULL,error_type TEXT NOT NULL,reason TEXT NOT NULL)',
            "CREATE TABLE IF NOT EXISTS query_tasks (task_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,cache_key TEXT NOT NULL,account TEXT,state TEXT NOT NULL,phase TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,usage_date TEXT,reason TEXT NOT NULL DEFAULT '',sent_id INTEGER,source_id INTEGER,result_type TEXT,result_text TEXT,file_path TEXT,file_name TEXT,export_status TEXT,delivery_state TEXT NOT NULL DEFAULT 'not_started',delivery_cursor INTEGER NOT NULL DEFAULT 0,delivered_at REAL,reserved INTEGER NOT NULL DEFAULT 0)",
            'CREATE TABLE IF NOT EXISTS file_leases (path TEXT NOT NULL,owner TEXT NOT NULL,PRIMARY KEY(path,owner))',
            'CREATE TABLE IF NOT EXISTS account_cooldowns (phone TEXT PRIMARY KEY,until_at REAL NOT NULL)',
            'CREATE INDEX IF NOT EXISTS idx_jr_result_cache_expires ON jr_result_cache(expires_at)',
            'CREATE INDEX IF NOT EXISTS idx_query_tasks_key ON query_tasks(cache_key,state)',
        ]
        for statement in statements:
            await conn.execute(statement)
        existing = {r[1] for r in await columns(conn, 'jr_result_cache')}
        for name, definition in [('result_type', "TEXT NOT NULL DEFAULT 'text'"), ('file_path', 'TEXT'), ('file_name', 'TEXT')]:
            if name not in existing:
                await conn.execute(f'ALTER TABLE jr_result_cache ADD COLUMN {name} {definition}')
        await conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES (1)')
        # Version 2 adds JR query cooldowns without changing version 1 tables.
        await conn.execute('CREATE TABLE IF NOT EXISTS jr_query_cooldowns '
                           '(phone TEXT PRIMARY KEY,until_at REAL NOT NULL)')
        await conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES (2)')
