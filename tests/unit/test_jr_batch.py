from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from telegram_bot.handlers.jr_query import make_handler
from telegram_bot.clients.request_tracker import RequestTracker
from telegram_bot.models.enums import QueryState


@pytest.mark.parametrize('command', ['/jr', '/jr+', '/jr@testbot'])
async def test_batch_reuses_service_and_continues_after_failed_item(command):
    reports = [SimpleNamespace(state=state, reason='explicit_target_failure', task_id=str(i))
               for i, state in enumerate((QueryState.FAILED, QueryState.SUCCESS))]
    execute = AsyncMock(side_effect=reports)
    ctx = SimpleNamespace(config=SimpleNamespace(admin_id=123, reply_timeout=1),
                          requests=RequestTracker(), jr=SimpleNamespace(execute=execute, accepting=True))
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(text=command + '\n first query \n\nsecond\nfirst query',
                              reply_text=AsyncMock(return_value=status))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123),
                             effective_message=message, update_id=9)
    await make_handler(ctx)(update, SimpleNamespace(bot=SimpleNamespace(username='testbot')))
    assert [call.args for call in execute.await_args_list] == [
        ('123:9:batch:1', 'first query'), ('123:9:batch:2', 'second')]
    assert '成功 1，失败 1，待核实 0' in status.edit_text.await_args.args[0]


async def test_batch_denies_nonadmin():
    execute = AsyncMock()
    ctx = SimpleNamespace(config=SimpleNamespace(admin_id=123), jr=SimpleNamespace(execute=execute))
    message = SimpleNamespace(text='/jr\nfirst\nsecond', reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=456), effective_message=message)
    await make_handler(ctx)(update, SimpleNamespace())
    execute.assert_not_awaited()
