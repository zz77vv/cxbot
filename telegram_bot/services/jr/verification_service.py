"""Bounded OCR assistance for the initial JR candidate selection prompt."""
import asyncio
import math
import logging
from dataclasses import dataclass
from typing import Any

from ...clients.dialogue import Dialogue, signature
from ...config import Config
from ...target_bots import JR, OCR_MAX_ATTEMPTS, OCR_MIN_CONFIDENCE
from .ocr_service import OCRRecognitionError, OCRService, extract_ocr_candidates, normalize_ocr_text
from .result_parser import classify, is_jr_pending_message
from .target_client import TargetClient

log = logging.getLogger(__name__)


@dataclass
class VerificationAttempt:
    stage: str = 'candidates'
    attempt: int = 0
    candidates: int = 0
    message_id: int | None = None


class VerificationError(RuntimeError):
    """An uncertain interaction must be reviewed without retrying side effects."""


class VerificationService:
    def __init__(self, ocr: OCRService, dialogue: Dialogue,
                 target: TargetClient, cfg: Config):
        self.ocr, self.dialogue, self.target, self.cfg = ocr, dialogue, target, cfg

    async def solve(self, client: Any, verification_message: Any,
                    original_query: str) -> tuple[Any, int]:
        context = VerificationAttempt()
        try:
            return await self._solve(client, verification_message, original_query, context)
        except VerificationError as error:
            self._log_failure(context, str(error))
            raise
        except OCRRecognitionError as error:
            self._log_failure(context, error.code, error.confidence)
            raise VerificationError(f'{context.stage}:{error.code}') from None
        except Exception as error:
            # Exception messages may contain OCR text, callbacks or request data.
            code = type(error).__name__
            self._log_failure(context, code)
            raise VerificationError(f'{context.stage}:{code}') from None

    @staticmethod
    def _log_failure(context: VerificationAttempt, code: str, confidence=None):
        score = confidence if isinstance(confidence, (int, float)) and math.isfinite(confidence) else 'unknown'
        log.warning('event=verification_failed stage=%s code=%s attempt=%s candidates=%s message_id=%s confidence=%s',
                    context.stage, code, context.attempt, context.candidates, context.message_id, score)

    async def _solve(self, client: Any, current: Any,
                     original_query: str, context: VerificationAttempt) -> tuple[Any, int]:
        query_after_id = current.id
        resubmitted = False
        for attempt in range(1, OCR_MAX_ATTEMPTS + 1):
            context.attempt, context.stage, context.message_id = attempt, 'candidates', current.id
            # The shared helper applies OCR_IGNORE_BUTTON_KEYWORDS.
            candidates = extract_ocr_candidates(current)
            context.candidates = len(candidates)
            if not candidates or not getattr(current, 'media', None):
                raise VerificationError('missing_image_or_candidates')
            context.stage = 'image_download'
            image = await asyncio.wait_for(
                client.download_media(current, file=bytes), self.cfg.reply_timeout)
            if not isinstance(image, bytes) or not image:
                raise VerificationError('image_download_failed')
            charset = ''.join(dict.fromkeys(ch for *_, text in candidates for ch in text))
            context.stage = 'ocr'
            index, recognized, confidence = await self.ocr.recognize_captcha(
                image, charset, len(candidates))
            if (confidence is None or not math.isfinite(confidence)
                    or confidence < OCR_MIN_CONFIDENCE):
                raise VerificationError('low_or_unknown_confidence')
            if not isinstance(index, int) or not 1 <= index <= len(candidates):
                raise VerificationError('invalid_target_index')
            context.stage = 'candidate_match'
            normalized = normalize_ocr_text(recognized)
            matches = [item for item in candidates if normalized and item[3] == normalized]
            if len(matches) != 1:
                raise VerificationError('non_unique_exact_match')
            row, col, _, _ = matches[0]
            old_signature = signature(current)
            context.stage = 'button_click'
            callback = await asyncio.wait_for(current.click(row, col), self.cfg.reply_timeout)
            passed = '验证通过' in (getattr(callback, 'message', '') or '')
            context.stage = 'wait_after_click'
            reply = await self.dialogue.wait(
                client, JR.bot, current.id, lambda m: not is_jr_pending_message(m),
                self.cfg.reply_timeout, original=current, old_signature=old_signature)
            passed = passed or bool(reply and '验证通过' in (getattr(reply, 'text', '') or ''))
            if passed:
                # At most one intentional resubmission. Never retry an uncertain send.
                if resubmitted:
                    raise VerificationError('repeated_resubmission_blocked')
                context.stage = 'resubmit'
                sent = await self.target.submit(client, original_query)
                resubmitted = True
                query_after_id = sent.id
                context.stage = 'wait_result'
                reply = await self.target.ready(client, query_after_id)
                if not reply:
                    raise VerificationError('resubmission_reply_timeout')
            if not reply:
                raise VerificationError('interaction_reply_timeout')
            kind = classify(reply)
            if kind == 'manual':
                current = reply
                continue
            if kind not in ('result', 'failed', 'cooldown'):
                raise VerificationError('unrecognized_interaction_reply')
            return reply, query_after_id
        raise VerificationError('attempts_exhausted')
