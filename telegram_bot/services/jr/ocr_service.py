"""Offline OCR compatibility. No Telegram clicks or automatic verification submission."""
import asyncio
import logging
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from PIL import Image, ImageOps
import warnings
# This dependency changes the global warning filters during import.
with warnings.catch_warnings():
    import ddddocr
from ...target_bots import OCR_MIN_CONFIDENCE, OCR_MAX_ATTEMPTS, OCR_IGNORE_BUTTON_KEYWORDS
from ...models.enums import Outcome

log = logging.getLogger(__name__)


class OCRRecognitionError(RuntimeError):
    """Safe OCR diagnostic fields; never carries recognized text or images."""
    def __init__(self, code: str, *, confidence=None):
        self.code = code
        self.confidence = confidence
        super().__init__(code)

def normalize_ocr_text(text: str) -> str:
    """统一全半角并去掉 OCR/按钮中的空格、符号和表情。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return "".join(
        ch for ch in normalized
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    ).lower()

def extract_ocr_candidates(message):
    """提取验证码候选按钮，排除刷新、分页、购买等功能按钮。"""
    candidates = []
    for row_idx, row in enumerate(getattr(message, "buttons", None) or []):
        for col_idx, button in enumerate(row):
            label = (getattr(button, "text", None) or "").strip()
            normalized = normalize_ocr_text(label)
            if not normalized:
                continue
            if any(keyword in label for keyword in OCR_IGNORE_BUTTON_KEYWORDS):
                continue
            candidates.append((row_idx, col_idx, label, normalized))
    return candidates

def _pil_to_png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()

def _classification_with_confidence(image_bytes: bytes, ocr_engine):
    """执行一次 OCR，并从概率结果中计算平均置信度。"""
    result = ocr_engine.classification(
        image_bytes,
        probability=True,
        png_fix=True,
    )

    if isinstance(result, str):
        return result, None

    # Some adapters return decoded text and a score directly.
    if "text" in result:
        confidence = result.get("confidence")
        return (
            str(result.get("text") or ""),
            float(confidence) if confidence is not None else None,
        )

    # 兼容旧版 ddddocr 的概率结果结构。
    charsets = result.get("charsets") or []
    probabilities = result.get("probability") or []
    recognized = []
    scores = []
    previous = None
    for row in probabilities:
        # Restricted ddddocr output uses -1 for characters absent from its
        # vocabulary. Do not renormalize candidate probabilities: doing so
        # would turn a weak prediction into an apparently confident one.
        if (len(row) != len(charsets) or not row
                or any(not math.isfinite(value) or (value != -1 and not 0 <= value <= 1)
                       for value in row)):
            raise OCRRecognitionError('InvalidProbabilities')
        index = max(range(len(row)), key=row.__getitem__)
        char = charsets[index]
        if row[index] < 0:
            raise OCRRecognitionError('InvalidProbabilities')
        if char:
            # CTC repeats adjacent frames for a single character. A blank
            # resets the run, so two identical letters separated by a blank
            # remain two letters. Keep every nonblank score in the mean.
            if index != previous:
                recognized.append(char)
            scores.append(float(row[index]))
        previous = index if char else None

    confidence = sum(scores) / len(scores) if scores else 0.0
    return "".join(recognized), confidence

def _recognize_captcha_sync(
    image_bytes: bytes,
    candidate_charset: str,
    candidate_count: int,
    ocr_engine,
):
    """
    识别“请点击第 N 个字”类型验证码。

    先从上半部分的红色数字识别 N，再按照图片中的等宽位置裁出第 N
    个汉字。Telegram 按钮的排列顺序完全不参与位置判断。
    """
    if ocr_engine is None:
        ocr_engine = ddddocr.DdddOcr(beta=True, show_ad=False)

    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")

    width, height = image.size
    if width < 100 or height < 80:
        raise OCRRecognitionError('InvalidImageSize')

    # 顶部要求中的序号是红色。只保留红色像素，可以避免正文汉字和
    # 下方 1/2/3/4 编号干扰序号识别。
    instruction = image.crop((0, 0, width, max(1, int(height * 0.48))))
    red_mask = Image.new("L", instruction.size, 255)
    red_mask.putdata([
        0 if (
            red >= 110
            and red - green >= 35
            and red - blue >= 35
            and red >= green * 1.25
            and red >= blue * 1.25
        ) else 255
        for red, green, blue in instruction.getdata()
    ])

    red_bbox = ImageOps.invert(red_mask).getbbox()
    if red_bbox:
        left, top, right, bottom = red_bbox
        margin_x = max(4, int((right - left) * 0.15))
        margin_y = max(4, int((bottom - top) * 0.15))
        red_mask = red_mask.crop((
            max(0, left - margin_x),
            max(0, top - margin_y),
            min(red_mask.width, right + margin_x),
            min(red_mask.height, bottom + margin_y),
        ))
    else:
        # 极端情况下颜色压缩严重，退回到整个顶部区域识别数字。
        red_mask = ImageOps.autocontrast(ImageOps.grayscale(instruction))

    ocr_engine.set_ranges("123456789")
    index_text = ocr_engine.classification(_pil_to_png_bytes(red_mask))
    index_text = unicodedata.normalize('NFKC', str(index_text)).strip()
    if not re.fullmatch(r'[1-9]', index_text) or int(index_text) > candidate_count:
        raise OCRRecognitionError('InstructionIndexAmbiguous')
    target_index = int(index_text)

    # Preserve more of the glyph than the old 35%-95% crop. Stay within
    # the selected slot: never include a neighbouring answer. This remains
    # specific to the supported equal-width JR layout, not a text detector.
    slot_width = width / candidate_count
    slot_left = (target_index - 1) * slot_width
    word_crop = image.crop((
        int(slot_left + slot_width * 0.20),
        int(height * 0.45),
        int(slot_left + slot_width),
        int(height * 0.96),
    ))
    ocr_engine.set_ranges(candidate_charset)
    # Exactly two views, on the existing single worker. Preserve antialiased
    # strokes instead of destroying them with a fixed binary threshold.
    # Agreement is required; never select whichever view has the best score.
    views = (word_crop, ImageOps.autocontrast(ImageOps.grayscale(word_crop)))
    readings = [_classification_with_confidence(_pil_to_png_bytes(view), ocr_engine)
                for view in views]
    texts = [normalize_ocr_text(text) for text, _ in readings]
    if not texts[0] or texts[0] != texts[1]:
        raise OCRRecognitionError('RecognitionDisagreement')
    scores = [score for _, score in readings]
    if any(score is None or not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
        raise OCRRecognitionError('InvalidConfidence')
    return target_index, readings[0][0], min(scores)


@dataclass(frozen=True)
class OCRResult:
    outcome: Outcome
    text: str = ''
    confidence: float | None = None
    target_index: int | None = None
    error_type: str = ''


class OCRService:
    def __init__(self, timeout=20, engine_factory=None):
        self.timeout = timeout
        self.engine_factory = engine_factory or (lambda: ddddocr.DdddOcr(beta=True, show_ad=False))
        self.engine = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='jr-ocr')
        self.lock = asyncio.Lock()
        self.pending = None
        self.closed = False
        self.range_restricted = False

    def _work(self, image_bytes, charset, count, positional):
        if self.engine is None or (not positional and not charset and self.range_restricted):
            self.engine = self.engine_factory()
            self.range_restricted = False
        if positional:
            self.range_restricted = True
            return _recognize_captcha_sync(image_bytes, charset, count, self.engine)
        with Image.open(BytesIO(image_bytes)) as source:
            payload = _pil_to_png_bytes(source.convert('RGB'))
        if charset:
            self.engine.set_ranges(charset)
            self.range_restricted = True
        text, confidence = _classification_with_confidence(payload, self.engine)
        return None, text, confidence

    async def recognize(self, image_bytes, charset='', count=0, *, positional=False):
        async with self.lock:
            if self.closed or (self.pending is not None and not self.pending.done()):
                return OCRResult(Outcome.UNAVAILABLE, error_type='OCRBusyOrClosed')
            if positional and (count < 1 or not charset):
                return OCRResult(Outcome.FAILED, error_type='InvalidCandidates')
            self.pending = asyncio.get_running_loop().run_in_executor(
                self.executor, self._work, image_bytes, charset, count, positional)
            # Observe late failures after timeout without losing the busy flag.
            self.pending.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
            try:
                index, text, confidence = await asyncio.wait_for(asyncio.shield(self.pending), self.timeout)
                if not normalize_ocr_text(text):
                    return OCRResult(Outcome.FAILED, error_type='EmptyRecognition')
                if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
                    return OCRResult(Outcome.FAILED, error_type='InvalidConfidence')
                if confidence is not None and confidence < OCR_MIN_CONFIDENCE:
                    return OCRResult(Outcome.FAILED, text, confidence, index, 'LowConfidence')
                return OCRResult(Outcome.SUCCESS, text, confidence, index)
            except TimeoutError:
                log.warning('OCR recognition timeout')
                return OCRResult(Outcome.TIMEOUT, error_type='TimeoutError')
            except OCRRecognitionError as error:
                log.warning('event=ocr_failed code=%s', error.code)
                return OCRResult(Outcome.FAILED, error_type=error.code)
            except Exception as error:
                log.warning('OCR recognition error_type=%s', type(error).__name__)
                return OCRResult(Outcome.FAILED, error_type=type(error).__name__)

    async def recognize_captcha(self, image_bytes, candidate_charset, candidate_count):
        """Tuple adapter preserving safe failure codes without OCR text."""
        result = await self.recognize(image_bytes, candidate_charset, candidate_count, positional=True)
        if result.outcome != Outcome.SUCCESS:
            raise OCRRecognitionError(result.error_type, confidence=result.confidence)
        return result.target_index, result.text, result.confidence

    async def close(self):
        self.closed = True
        if self.pending and not self.pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.pending), self.timeout)
            except TimeoutError:
                log.error('OCR native worker still running at shutdown')
            except Exception as error:
                log.warning('OCR worker shutdown error_type=%s', type(error).__name__)
        self.executor.shutdown(wait=False, cancel_futures=True)
