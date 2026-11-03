from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import KeyboardButtonCallback

from telegram_bot.services.jr.manual_verification import ManualVerificationService
from telegram_bot.services.jr.verification_service import VerificationError


@pytest.mark.parametrize('action,changed', [(0, False), (1, False), (0, True)])
async def test_manual_choice_then_explicit_continue(cfg, action, changed):
    def prompt(photo_id):
        return SimpleNamespace(
            id=9, text='验证', media=True, photo=SimpleNamespace(id=photo_id),
            buttons=[[SimpleNamespace(text='字', button=KeyboardButtonCallback('字', b'choice'))]],
            click=AsyncMock(return_value=SimpleNamespace(message='验证通过')))

    current = prompt(3)
    latest = prompt(4) if changed else current
    client = SimpleNamespace(download_media=AsyncMock(return_value=b'image'),
                             get_messages=AsyncMock(return_value=latest))
    target = SimpleNamespace(submit=AsyncMock(return_value=SimpleNamespace(id=10)),
                             ready=AsyncMock(return_value=SimpleNamespace(id=11)))
    cleanup = SimpleNamespace(edit_reply_markup=AsyncMock())
    bot = SimpleNamespace()
    service = ManualVerificationService(bot, SimpleNamespace(), target, cfg)

    async def select(markup, index):
        query = SimpleNamespace(from_user=SimpleNamespace(id=cfg.admin_id), answer=AsyncMock(),
                                data=markup.inline_keyboard[index][0].callback_data)
        update = SimpleNamespace(callback_query=query,
                                 effective_chat=SimpleNamespace(type='private', id=cfg.admin_id))
        await service.callback(update, None)
        await service.callback(update, None)
        assert query.answer.await_args.kwargs == {'show_alert': True}
        return cleanup

    async def show_image(chat, image, **kwargs):
        assert chat == cfg.admin_id and image == b'image'
        target.submit.assert_not_awaited()
        return await select(kwargs['reply_markup'], 0)

    async def show_continue(chat, text, **kwargs):
        current.click.assert_awaited_once_with(0, 0)
        target.submit.assert_not_awaited()
        return await select(kwargs['reply_markup'], action)

    bot.send_photo = AsyncMock(side_effect=show_image)
    bot.send_message = AsyncMock(side_effect=show_continue)
    if changed or action:
        code = 'manual_prompt_changed' if changed else 'manual_query_abandoned'
        with pytest.raises(VerificationError, match=code):
            await service.solve(client, current, 'fixture', task='test', account='1234')
        target.submit.assert_not_awaited()
        if changed:
            latest.click.assert_not_awaited()
    else:
        reply, after = await service.solve(client, current, 'fixture', task='test', account='1234')
        assert reply.id == 11 and after == 10
        target.submit.assert_awaited_once_with(client, 'fixture')
    assert not service.choices
