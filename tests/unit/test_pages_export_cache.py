import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from conftest import message,button
from telegram_bot.services.jr.pagination import PaginationService
from telegram_bot.services.jr.result_parser import normalize_jr_full_result,find_buy_button
from telegram_bot.services.jr.export_service import ExportService,jr_original_file_name,jr_message_matches_export_request
from telegram_bot.services.jr.cache_service import CacheService
from telegram_bot.storage.repositories.query_repository import QueryRepository
from telegram_bot.storage.repositories.cache_repository import CacheRepository
from telegram_bot.models.query import JRQueryResult


async def test_pagination_and_repeated_page(monkeypatch):
    monkeypatch.setattr('telegram_bot.services.jr.pagination.JR_PAGE_INTERVAL',0)
    first=message('第1页，共2页\nfixture A',buttons=[[button('下一页')]])
    last=message('第2页，共2页\nfixture B')
    target=SimpleNamespace(click_and_wait=AsyncMock(return_value=last))
    assert await PaginationService(target).collect(None,first)=='fixture A\n\nfixture B'
    target.click_and_wait.return_value=message(first.text,id=12,buttons=first.buttons)
    with pytest.raises(ValueError): await PaginationService(target).collect(None,first)
    with pytest.raises(ValueError): await PaginationService(target).collect(None,first,max_pages=201)


async def test_pagination_hard_limit_and_timeout():
    first=message('第1页，共2页\nfixture',buttons=[[button('下一页')]])
    target=SimpleNamespace(click_and_wait=AsyncMock(return_value=None))
    with pytest.raises(ValueError): await PaginationService(target).collect(None,first,max_pages=1)
    with pytest.raises(TimeoutError): await PaginationService(target).collect(None,first)


def test_original_normalization_and_buy_rules():
    assert normalize_jr_full_result('🔍共1条信息\n`A`\n\n\n🔍共1条信息\nB')=='🔍共1条信息\nA\n\nB'
    assert find_buy_button(message(buttons=[[button('购买 10 积分')]]))[2]=='购买 10 积分'
    assert find_buy_button(message(buttons=[[button('购买')]])) is not None


def test_export_file_correlation_and_name():
    file=message(id=20,document=True,media=True,file=SimpleNamespace(name='../../result.txt'))
    assert jr_original_file_name(file)=='result.txt'
    assert jr_message_matches_export_request(file,baseline_id=19,query_after_id=10,target_bot_id=999)
    assert not jr_message_matches_export_request(file,baseline_id=20,query_after_id=10)
    assert not jr_message_matches_export_request(file,baseline_id=19,query_after_id=10,target_bot_id=1000)
    file.reply_to_msg_id=9
    assert not jr_message_matches_export_request(file,baseline_id=19,query_after_id=10)


async def test_export_single_click_download_and_deliver(cfg,db,client,bot):
    repo=QueryRepository(db); cache=CacheService(cfg,CacheRepository(db))
    row,_=await repo.claim('u1','key',time.time()); task=row['task_id']
    original=message('🔍共1条信息',buttons=[[button('导出数据')]])
    file=message(id=20,document=True,media=True,file=SimpleNamespace(name='result.txt'))
    dialogue=SimpleNamespace(messages=AsyncMock(return_value=[original]),wait=AsyncMock(return_value=file))
    async def download(message,file):
        from pathlib import Path
        Path(file).write_bytes(b'fixture'); return file
    client.download_media.side_effect=download
    exports=ExportService(cfg,dialogue,repo,cache,bot)
    result,status,_=await exports.attempt(task,client,original,10)
    assert result.kind=='file' and status=='member' and original.click.await_count==1
    await repo.complete(task,result,time.time(),300)
    await exports.deliver(task,result); await exports.deliver(task,result)
    assert bot.send_document.await_count==1


async def test_free_export_and_timeout_fallback(cfg,db,client,bot):
    repo=QueryRepository(db); cache=CacheService(cfg,CacheRepository(db))
    row,_=await repo.claim('u1','key',time.time())
    original=message(buttons=[[button('导出数据')]])
    original.click.return_value=SimpleNamespace(message='免费用户暂不支持导出')
    dialogue=SimpleNamespace(messages=AsyncMock(return_value=[]),wait=AsyncMock(return_value=None))
    exports=ExportService(cfg,dialogue,repo,cache,bot)
    result,status,_=await exports.attempt(row['task_id'],client,original,10)
    assert result is None and status=='free'
    original.click.return_value=SimpleNamespace(message='')
    result,status,reason=await exports.attempt(row['task_id'],client,original,10)
    assert result is None and status=='unknown' and reason=='export_file_timeout'


async def test_cache_cleanup_protects_active_and_task_files(cfg,db):
    cfg.cache.mkdir()
    valid=cfg.cache/'valid.bin'; valid.write_bytes(b'valid')
    task_file=cfg.cache/'task.bin'; task_file.write_bytes(b'task')
    orphan=cfg.cache/'orphan.bin'; orphan.write_bytes(b'orphan')
    outside=cfg.cache.parent/'outside.bin'; outside.write_bytes(b'outside')
    for path in [valid,task_file,orphan]: os.utime(path,(1,1))
    now=time.time()
    async with db.transaction() as c:
        await c.executemany('INSERT INTO jr_result_cache VALUES(?,?,?,?,?,?,?,?)',[
            ('live','','a',0,now+300,'file',str(valid),'valid.bin'),
            ('expired','','a',0,1,'file',str(valid),'valid.bin'),
            ('unsafe','','a',0,1,'file',str(outside),'outside.bin')])
    queries=QueryRepository(db); row,_=await queries.claim('task','task-key',now)
    await queries.attach_file(row['task_id'],str(task_file),'task.bin')
    cache=CacheService(cfg,CacheRepository(db))
    assert await cache.cleanup(now)==1
    assert valid.exists() and task_file.exists() and outside.exists() and not orphan.exists()


async def test_cleanup_protects_delivery_lease(cfg,db):
    cfg.cache.mkdir(); path=cfg.cache/'lease.bin'; path.write_bytes(b'fixture'); os.utime(path,(1,1))
    repo=CacheRepository(db); cache=CacheService(cfg,repo)
    async with repo.lease(str(path),'test'):
        await cache.cleanup()
        assert path.exists()
    await cache.cleanup()
    assert not path.exists()


async def test_export_download_failure_can_fall_back_to_existing_pages(cfg,db,client,bot):
    queries=QueryRepository(db);row,_=await queries.claim('u1','key',time.time())
    original=message('第1页，共1页\nfixture',buttons=[[button('导出数据')]])
    file=message(id=20,document=True,media=True,file=SimpleNamespace(name='result.txt'))
    dialogue=SimpleNamespace(messages=AsyncMock(return_value=[original]),wait=AsyncMock(return_value=file))
    exports=ExportService(cfg,dialogue,queries,CacheService(cfg,CacheRepository(db)),bot)
    client.download_media.side_effect=OSError('fixture failure')
    result,status,reason=await exports.attempt(row['task_id'],client,original,10)
    assert result is None and status=='member' and reason=='export_download_failed'
    original.click.assert_awaited_once()
