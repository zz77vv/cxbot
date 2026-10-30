import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest
from conftest import button, message
from telegram_bot.clients.dialogue import signature
from telegram_bot.services.jr.ocr_service import OCRService, OCRRecognitionError, OCRResult
from telegram_bot.models.enums import Outcome
from telegram_bot.services.jr.verification_service import VerificationError, VerificationService
from telegram_bot.target_bots import JR, OCR_MAX_ATTEMPTS, OCR_MIN_CONFIDENCE


@pytest.fixture
def verification(cfg, client):
    ocr = create_autospec(OCRService, instance=True)
    ocr.recognize_captcha.return_value = (1, '字', 0.9)
    dialogue = SimpleNamespace(wait=AsyncMock(return_value=message('验证通过', id=12)))
    target = SimpleNamespace(submit=AsyncMock(return_value=message(id=20)),
                             ready=AsyncMock(return_value=message('未查询到结果', id=21)))
    client.download_media.return_value = b'fixture image'
    return VerificationService(ocr, dialogue, target, cfg)


def prompt(**kwargs):
    return message('点击下图', media=True, buttons=[[button('取消'), button('字')]], **kwargs)


async def test_success_uses_shared_ocr_and_waits_for_edit(verification, client):
    current = prompt()
    old = signature(current)
    reply, after = await verification.solve(client, current, 'fixture query')
    assert reply.id == 21 and after == 20
    verification.ocr.recognize_captcha.assert_awaited_once_with(b'fixture image', '字', 1)
    current.click.assert_awaited_once_with(0, 1)
    args = verification.dialogue.wait.await_args
    assert args.args[:3] == (client, JR.bot, current.id)
    assert args.kwargs == dict(original=current, old_signature=old)
    verification.target.submit.assert_awaited_once_with(client, 'fixture query')
    verification.target.ready.assert_awaited_once_with(client, 20)


@pytest.mark.parametrize('confidence', [None, 0.0, OCR_MIN_CONFIDENCE - 0.01, float('nan')])
async def test_unknown_or_low_confidence_never_clicks(verification, client, confidence):
    current = prompt()
    verification.ocr.recognize_captcha.return_value = (1, '字', confidence)
    with pytest.raises(VerificationError, match='low_or_unknown_confidence'):
        await verification.solve(client, current, 'fixture')
    current.click.assert_not_awaited()
    verification.target.submit.assert_not_awaited()


@pytest.mark.parametrize('labels,recognized', [(['字', '字'], '字'), (['字'], '两个字'), (['字'], '')])
async def test_requires_unique_exact_match(verification, client, labels, recognized):
    current = message('验证', media=True, buttons=[[button(s) for s in labels]])
    verification.ocr.recognize_captcha.return_value = (1, recognized, 0.9)
    with pytest.raises(VerificationError, match='non_unique_exact_match'):
        await verification.solve(client, current, 'fixture')
    current.click.assert_not_awaited()


async def test_repeated_prompts_are_bounded(verification, client):
    prompts = [prompt(id=i) for i in range(11, 12 + OCR_MAX_ATTEMPTS)]
    verification.dialogue.wait.side_effect = prompts[1:]
    with pytest.raises(VerificationError, match='attempts_exhausted'):
        await verification.solve(client, prompts[0], 'fixture')
    assert verification.ocr.recognize_captcha.await_count == OCR_MAX_ATTEMPTS
    assert sum(p.click.await_count for p in prompts) == OCR_MAX_ATTEMPTS
    verification.target.submit.assert_not_awaited()


async def test_callback_only_success_allows_one_resubmission(verification, client):
    current = prompt()
    current.click.return_value = SimpleNamespace(message='验证通过')
    verification.dialogue.wait.return_value = None
    assert (await verification.solve(client, current, 'fixture'))[1] == 20
    verification.target.submit.assert_awaited_once()


async def test_direct_result_does_not_resubmit(verification, client):
    current = prompt()
    verification.dialogue.wait.return_value = message('未查询到结果', id=12)
    assert (await verification.solve(client, current, 'fixture'))[1] == current.id
    verification.target.submit.assert_not_awaited()


@pytest.mark.parametrize('step', ['download', 'ocr', 'click', 'wait', 'submit', 'ready'])
async def test_errors_are_not_retried_or_leaked(verification, client, step):
    current = prompt()
    operation = {'download': client.download_media, 'ocr': verification.ocr.recognize_captcha,
                 'click': current.click, 'wait': verification.dialogue.wait,
                 'submit': verification.target.submit, 'ready': verification.target.ready}[step]
    operation.side_effect = TimeoutError('private fixture data')
    stage = {'download': 'image_download', 'ocr': 'ocr', 'click': 'button_click',
             'wait': 'wait_after_click', 'submit': 'resubmit', 'ready': 'wait_result'}[step]
    with pytest.raises(VerificationError, match=f'^{stage}:TimeoutError$'):
        await verification.solve(client, current, 'fixture')
    operation.assert_awaited_once()
    assert verification.target.submit.await_count <= 1


@pytest.mark.parametrize('code', ['LowConfidence', 'EmptyRecognition', 'OCRBusyOrClosed',
                                 'InstructionIndexAmbiguous', 'InvalidImageSize', 'TimeoutError'])
async def test_ocr_failure_code_survives_adapter_and_verification(verification, client, code, caplog):
    service = OCRService()
    service.recognize = AsyncMock(return_value=OCRResult(Outcome.FAILED, 'private OCR text', 0.1, error_type=code))
    verification.ocr = service
    current = prompt()
    try:
        with pytest.raises(VerificationError, match=f'^ocr:{code}$'):
            await verification.solve(client, current, 'private query')
        assert 'stage=ocr' in caplog.text and f'code={code}' in caplog.text
        assert 'confidence=0.1' in caplog.text and 'candidates=1' in caplog.text
        assert 'private OCR text' not in caplog.text and 'private query' not in caplog.text
        current.click.assert_not_awaited()
        verification.target.submit.assert_not_awaited()
    finally:
        await service.close()


async def test_ambiguous_instruction_has_safe_code_without_raw_ocr(caplog):
    from io import BytesIO
    from PIL import Image
    from unittest.mock import Mock
    output = BytesIO()
    Image.new('RGB', (200, 100), 'white').save(output, format='PNG')
    engine = Mock()
    engine.classification.return_value = 'private unrecognized instruction'
    service = OCRService(engine_factory=lambda: engine)
    try:
        with pytest.raises(OCRRecognitionError, match='^InstructionIndexAmbiguous$'):
            await service.recognize_captcha(output.getvalue(), '字', 1)
        assert 'InstructionIndexAmbiguous' in caplog.text
        assert 'private unrecognized instruction' not in caplog.text
    finally:
        await service.close()


async def test_cancellation_propagates(verification, client):
    verification.ocr.recognize_captcha.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await verification.solve(client, prompt(), 'fixture')
    verification.target.submit.assert_not_awaited()


async def test_second_resubmission_is_blocked(verification, client):
    verification.target.ready.return_value = prompt(id=21)
    with pytest.raises(VerificationError, match='repeated_resubmission_blocked'):
        await verification.solve(client, prompt(), 'fixture')
    verification.target.submit.assert_awaited_once()
