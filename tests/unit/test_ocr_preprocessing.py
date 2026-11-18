"""Offline regression tests; synthetic pixels only, no account or image data."""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from conftest import button, message
from telegram_bot.services.jr.ocr_service import (
    OCRService, OCRRecognitionError, _classification_with_confidence,
    _recognize_captcha_sync,
)
from telegram_bot.services.jr.verification_service import VerificationService, VerificationError


def picture():
    image = Image.new('RGB', (600, 210), 'white')
    draw = ImageDraw.Draw(image)
    # Antialiased stroke shades in slot two, including outside the old crop.
    draw.rectangle((185, 110, 195, 170), fill=(95, 115, 135))
    draw.rectangle((195, 120, 220, 150), fill=(165, 175, 185))
    draw.rectangle((270, 110, 295, 170), fill=(45, 65, 85))
    stream = BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


class Engine:
    def __init__(self, readings=None, index='2'):
        self.readings = iter(readings or [('字', .8), ('字', .7)])
        self.index = index
        self.images = []
        self.ranges = []

    def set_ranges(self, value):
        self.ranges.append(value)

    def classification(self, payload, **kwargs):
        if not kwargs.get('probability'):
            return self.index
        self.images.append(Image.open(BytesIO(payload)).copy())
        text, confidence = next(self.readings)
        return dict(text=text, confidence=confidence)


def decode(rows, charset=None):
    result = dict(charsets=charset or ['', '字', '文'], probability=rows)
    engine = SimpleNamespace(classification=lambda *a, **kw: result)
    return _classification_with_confidence(b'unused', engine)


def test_ctc_collapses_adjacent_frames_without_inflating_score():
    text, score = decode([[.1, .8, .1], [.2, .6, .2], [.9, .05, .05]])
    assert text == '字'
    assert score == pytest.approx(.7)


def test_ctc_blank_preserves_separate_repeated_characters():
    text, score = decode([[.1, .8, .1], [.9, .05, .05], [.1, .8, .1]])
    assert text == '字字'
    assert score == pytest.approx(.8)


def test_restricted_probabilities_are_not_renormalized():
    assert decode([[.001, .00763, -1]]) == ('字', .00763)


@pytest.mark.parametrize('row', [[.1], [], [.1, float('nan'), .2],
                                      [.1, float('inf'), .2], [.1, 1.2, .2], [-1, -1, -1]])
def test_malformed_probabilities_fail_closed(row):
    with pytest.raises(OCRRecognitionError, match='InvalidProbabilities'):
        decode([row])


def test_crop_preserves_strokes_and_gray_levels_with_two_bounded_views():
    engine = Engine()
    assert _recognize_captcha_sync(picture(), '字文天地', 4, engine) == (2, '字', .7)
    assert engine.ranges == ['123456789', '字文天地']
    assert len(engine.images) == 2
    rgb, gray = engine.images
    assert rgb.mode == 'RGB' and gray.mode == 'L'
    assert rgb.size == gray.size == (120, 107)
    assert rgb.getpixel((5, 20)) == (95, 115, 135)
    assert rgb.getpixel((110, 20)) == (45, 65, 85)
    assert len(set(gray.getdata())) > 2


@pytest.mark.parametrize('index', ['12', '22', '2x', '29', '0', '5', ''])
def test_ordinal_is_not_guessed_from_partial_digits(index):
    engine = Engine(index=index)
    with pytest.raises(OCRRecognitionError, match='InstructionIndexAmbiguous'):
        _recognize_captcha_sync(picture(), '字文天地', 4, engine)
    assert engine.images == []


@pytest.mark.parametrize('readings,code', [
    ([('字', .9), ('文', .9)], 'RecognitionDisagreement'),
    ([('字', .9), ('字', .1)], 'LowConfidence'),
    ([('字', .9), ('字', None)], 'InvalidConfidence'),
    ([('字', .9), ('字', float('nan'))], 'InvalidConfidence'),
])
async def test_real_ocr_adapter_failure_never_clicks_or_resubmits(cfg, client, readings, code):
    engine = Engine(readings)
    ocr = OCRService(engine_factory=lambda: engine)
    dialogue = SimpleNamespace(wait=AsyncMock())
    target = SimpleNamespace(submit=AsyncMock(), ready=AsyncMock())
    verification = VerificationService(ocr, dialogue, target, cfg)
    current = message('验证', media=True, buttons=[[button(x) for x in '字文天地']])
    client.download_media.return_value = picture()
    try:
        with pytest.raises(VerificationError, match=code):
            await verification.solve(client, current, 'fixture')
        current.click.assert_not_awaited()
        target.submit.assert_not_awaited()
        dialogue.wait.assert_not_awaited()
        assert len(engine.images) == 2
    finally:
        await ocr.close()
