import asyncio
import secrets
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon.tl.types import KeyboardButtonCallback

from ...clients.dialogue import signature
from ...target_bots import JR
from .ocr_service import extract_ocr_candidates
from .result_parser import classify, is_jr_pending_message
from .verification_service import VerificationError


@dataclass
class Choice:
    future: asyncio.Future
    count: int


class ManualVerificationService:
    def __init__(self, bot, dialogue, target, cfg):
        self.bot, self.dialogue, self.target, self.cfg = bot, dialogue, target, cfg
        self.choices = {}

    async def callback(self, update, context):
        query = update.callback_query
        chat = update.effective_chat
        if query.from_user.id != self.cfg.admin_id or not chat or chat.type != 'private' or chat.id != self.cfg.admin_id:
            await query.answer('仅限管理员私聊使用。', show_alert=True)
            return
        _, token, value = query.data.split(':')
        choice = self.choices.get(token)
        index = int(value)
        if not choice or choice.future.done() or not 0 <= index < choice.count:
            await query.answer('选项已使用或过期。', show_alert=True)
            return
        choice.future.set_result(index)
        await query.answer('已收到选择。')

    async def _choose(self, caption, labels, *, image=None):
        token = secrets.token_urlsafe(16)
        future = asyncio.get_running_loop().create_future()
        self.choices[token] = Choice(future, len(labels))
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f'jrverify:{token}:{index}')]
            for index, label in enumerate(labels)
        ])
        prompt = None
        try:
            if image is None:
                prompt = await asyncio.wait_for(self.bot.send_message(
                    self.cfg.admin_id, caption, reply_markup=keyboard, parse_mode=None), self.cfg.reply_timeout)
            else:
                prompt = await asyncio.wait_for(self.bot.send_photo(
                    self.cfg.admin_id, image, caption=caption, reply_markup=keyboard, parse_mode=None), self.cfg.reply_timeout)
            return await asyncio.wait_for(future, 120), prompt
        except TimeoutError:
            raise VerificationError('manual_choice_timeout') from None
        finally:
            self.choices.pop(token, None)
            if prompt is not None:
                try:
                    await asyncio.wait_for(prompt.edit_reply_markup(reply_markup=None), 3)
                except Exception:
                    # The token is already invalid even when removing the keyboard fails.
                    pass

    @staticmethod
    def _buttons(message):
        return [(row, col, label, button.data)
                for row, col, label, _ in extract_ocr_candidates(message)
                if isinstance(button := message.buttons[row][col].button, KeyboardButtonCallback)
                and not getattr(button, 'requires_password', False)]

    async def solve(self, client, current, original_query, *, task, account):
        candidates = self._buttons(current)
        if not candidates or not getattr(current, 'photo', None):
            raise VerificationError('missing_image_or_candidates')
        old_signature = signature(current)
        image = await asyncio.wait_for(client.download_media(current, file=bytes), self.cfg.reply_timeout)
        if not image:
            raise VerificationError('image_download_failed')
        caption = f'巨人验证｜任务 {task}\n协议号尾号 {account[-4:]}\n请按原图选择，限时 2 分钟。'
        selected, prompt = await self._choose(caption, [item[2] for item in candidates], image=image)
        latest = await asyncio.wait_for(client.get_messages(JR.bot, ids=current.id), self.cfg.reply_timeout)
        if (not latest or classify(latest) != 'manual' or signature(latest) != old_signature
                or getattr(latest.photo, 'id', None) != current.photo.id or self._buttons(latest) != candidates):
            raise VerificationError('manual_prompt_changed')
        row, col, _, _ = candidates[selected]
        callback = await asyncio.wait_for(latest.click(row, col), self.cfg.reply_timeout)
        if '验证通过' not in (getattr(callback, 'message', '') or ''):
            reply = await self.dialogue.wait(client, JR.bot, current.id,
                lambda m: not is_jr_pending_message(m), self.cfg.reply_timeout,
                original=latest, old_signature=old_signature)
            if not reply or '验证通过' not in (reply.text or ''):
                raise VerificationError('manual_verification_unconfirmed')
        try:
            await asyncio.wait_for(prompt.delete(), 3)
        except Exception:
            # Cleanup is independent of the confirmed verification.
            pass
        sent = await self.target.submit(client, original_query)
        reply = await self.target.ready(client, sent.id)
        if not reply:
            raise VerificationError('resubmission_reply_timeout')
        return reply, sent.id
