from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import KeyboardButtonCallback

from telegram_bot.services.jr.manual_verification import ManualVerificationService
from telegram_bot.services.jr.verification_service import VerificationError


@pytest.mark.parametrize('changed,delete_fails', [(False, False), (False, True), (True, False)])
async def test_manual_choice_continues_once_after_verification(cfg, changed, delete_fails):
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
    cleanup = SimpleNamespace(edit_reply_markup=AsyncMock(), delete=AsyncMock(side_effect=RuntimeError if delete_fails else None))
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

    bot.send_photo = AsyncMock(side_effect=show_image)
    bot.send_message = AsyncMock()
    if changed:
        code = 'manual_prompt_changed'
        with pytest.raises(VerificationError, match=code):
            await service.solve(client, current, 'fixture', task='test', account='1234')
        target.submit.assert_not_awaited()
        if changed:
            latest.click.assert_not_awaited()
    else:
        reply, after = await service.solve(client, current, 'fixture', task='test', account='1234')
        assert reply.id == 11 and after == 10
        target.submit.assert_awaited_once_with(client, 'fixture')
    bot.send_message.assert_not_awaited()
    assert cleanup.delete.await_count == int(not changed)
    assert not service.choices
