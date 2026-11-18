import html
import logging
from dataclasses import replace
import pytest
from telegram_bot.config import Config, ConfigError
from telegram_bot.logging_config import RedactFilter
from telegram_bot.utils.message_format import split_text_by_lines, utf16_len
from telegram_bot.utils.paths import confined
from telegram_bot.utils.process_lock import ProcessLock


def env():
    return {'BOT_TOKEN': '12345:' + 'X'*30, 'ADMIN_ID': '683298845', 'API_ID': '456', 'API_HASH': 'a'*32}


@pytest.mark.parametrize('name', ['BOT_TOKEN','ADMIN_ID','API_ID','API_HASH'])
def test_missing_config(tmp_path, name):
    values = env(); values.pop(name)
    with pytest.raises(ConfigError):
        Config.load(tmp_path, values)


@pytest.mark.parametrize('key,value', [('ADMIN_ID','x'),('ADMIN_ID','0'),('API_ID','-1'),
    ('MAX_CONCURRENCY','0'),('REPLY_TIMEOUT','nan'),('RETRY_ATTEMPTS','1.5'),
    ('BOT_TOKEN','bad'),('API_HASH','oops'),('DATABASE_PATH',''),('CACHE_DIR','.')])
def test_invalid_config(tmp_path, key, value):
    values=env(); values[key]=value
    with pytest.raises(ConfigError):
        Config.load(tmp_path, values)


def test_env_precedence_and_paths(tmp_path):
    (tmp_path/'.env').write_text('ADMIN_ID=999\nAPI_ID=999\n',encoding='utf-8')
    cfg=Config.load(tmp_path,env())
    assert cfg.admin_id==683298845 and cfg.database==tmp_path/'data/bot.db'
    assert cfg.bot_token not in repr(cfg) and cfg.api_hash not in repr(cfg)


def test_env_malformed_and_overlap(tmp_path):
    (tmp_path/'.env').write_text('no-equals',encoding='utf-8')
    with pytest.raises(ConfigError): Config.load(tmp_path,env())
    (tmp_path/'.env').unlink()
    values=env(); values.update(CACHE_DIR='data')
    with pytest.raises(ConfigError): Config.load(tmp_path,values)


@pytest.mark.parametrize('text', ['a'*20000, '<>&'*5000, '😀'*6000, 'a\n\nb\n', 'abc\n'+'z'*9000], ids=['long-line','html','emoji','blank-lines','mixed'])
def test_chunk_escape_and_utf16(text):
    chunks=split_text_by_lines(text,escape_html=True)
    assert all(utf16_len(x)<=3600 for x in chunks)
    assert html.unescape(''.join(chunks))==text


def test_confined(tmp_path):
    with pytest.raises(ValueError): confined(tmp_path/'cache',tmp_path/'elsewhere')
    with pytest.raises(ValueError): confined(tmp_path,tmp_path)


def test_log_redaction():
    record=logging.LogRecord('test',20,'',1,'credential %s',('private-value',),None)
    assert RedactFilter(('private-value',)).filter(record)
    assert 'private-value' not in record.getMessage()


def test_single_process_lock(tmp_path):
    first,second=ProcessLock(tmp_path/'bot.db'),ProcessLock(tmp_path/'bot.db')
    first.acquire()
    try:
        with pytest.raises(RuntimeError): second.acquire()
    finally: first.close()
    second.acquire(); second.close()
