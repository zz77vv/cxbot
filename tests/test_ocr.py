from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from bot.ocr import OCRError, OCRService, extract_captcha_candidates, recognize_duplicate_char_sync


def make_captcha_image(num_glyphs: int = 4) -> bytes:
    """生成带 4 个清晰黑色竖块的合成测试图，确保垂直投影能切出 4 段。"""
    img = Image.new("RGB", (320, 120), "white")
    draw = ImageDraw.Draw(img)
    for i in range(num_glyphs):
        x0 = 20 + i * 70
        draw.rectangle((x0, 55, x0 + 30, 100), fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeEngine:
    def __init__(self, readings):
        self._readings = iter(readings)
        self.ranges = None

    def set_ranges(self, charset):
        self.ranges = charset

    def classification(self, image_bytes, probability=True, png_fix=True):
        text, conf = next(self._readings)
        return {"text": text, "confidence": conf}


def test_ocr_duplicate_char_matches_exact_button_index_even_with_multichar_labels():
    # 4 glyphs, 2 views each = 8 calls. '字' appears twice (glyph 0 and glyph 2).
    readings = [
        ("字", 0.8), ("字", 0.85),
        ("文", 0.9), ("文", 0.9),
        ("字", 0.75), ("字", 0.8),
        ("天", 0.9), ("天", 0.9),
    ]
    engine = FakeEngine(readings)
    # 第一个按钮是双字符 "甲乙"，验证不会因为字符集偏移导致索引错位
    idx, ch, conf = recognize_duplicate_char_sync(
        make_captcha_image(4), ["甲乙", "文", "字", "天"], engine
    )
    assert idx == 2
    assert ch == "字"
    assert conf == pytest.approx(0.75)


def test_ocr_reports_invalid_confidence_and_low_confidence():
    bad_conf = FakeEngine([("字", 0.9), ("字", float("nan"))] * 4)
    with pytest.raises(OCRError) as exc1:
        recognize_duplicate_char_sync(make_captcha_image(4), ["字", "文", "天", "地"], bad_conf)
    assert exc1.value.code == "InvalidConfidence"

    low_conf = FakeEngine([("字", 0.9), ("字", 0.1)] * 4)
    with pytest.raises(OCRError) as exc2:
        recognize_duplicate_char_sync(make_captcha_image(4), ["字", "文", "天", "地"], low_conf)
    assert exc2.value.code == "LowConfidence"


async def test_ocr_service_async_wrapper():
    readings = [("字", 0.8), ("字", 0.8), ("文", 0.9), ("文", 0.9), ("字", 0.7), ("字", 0.7), ("天", 0.9), ("天", 0.9)]
    svc = OCRService(timeout=5, engine_factory=lambda: FakeEngine(readings))
    try:
        idx, ch, conf = await svc.recognize_captcha(make_captcha_image(4), ["文", "字", "天"])
        assert (idx, ch) == (1, "字")
        assert conf == pytest.approx(0.7)
    finally:
        await svc.close()


def test_extract_captcha_candidates_ignores_control_buttons():
    msg = SimpleNamespace(
        buttons=[
            [SimpleNamespace(text="字"), SimpleNamespace(text="刷新"), SimpleNamespace(text="下一页")],
            [SimpleNamespace(text="文"), SimpleNamespace(text="导出数据")],
        ]
    )
    cands = extract_captcha_candidates(msg)
    assert [c.label for c in cands] == ["字", "文"]
