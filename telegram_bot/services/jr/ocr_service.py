"""Offline OCR compatibility. No Telegram clicks or automatic verification submission."""
import asyncio
import logging
import math
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from PIL import Image, ImageFilter, ImageOps
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

def _segment_char_boxes(bw_image: Image.Image, min_width: int = 15):
    """通过垂直投影自动切分单字边界框 (left, top, right, bottom)。"""
    w, h = bw_image.size
    pix = bw_image.load()
    col_counts = [sum(1 for y in range(h) if pix[x, y] == 0) for x in range(w)]
    segments = []
    in_seg = False
    start = 0
    for x, cnt in enumerate(col_counts):
        if cnt >= 3 and not in_seg:
            in_seg = True
            start = x
        elif cnt < 3 and in_seg:
            in_seg = False
            if x - start >= min_width:
                segments.append((start, x))
    if in_seg and w - start >= min_width:
        segments.append((start, w))
    boxes = []
    for left, right in segments:
        crop = bw_image.crop((left, 0, right, h))
        bbox = ImageOps.invert(crop).getbbox()
        if bbox:
            boxes.append((left + bbox[0], bbox[1], left + bbox[2], bbox[3]))
    return boxes
    
def _recognize_captcha_sync(
        image_bytes: bytes,
        candidate_charset: str,
        candidate_count: int,
        ocr_engine,
):
    """
    识别“点出现了两次的那个字”类型验证码。

    流程：截取下半部分 -> 中值滤波去干扰线/噪点 -> 垂直投影切分单字 ->
    限定候选按钮字符集双视图一致识别 -> 统计频次找出唯一出现 >= 2 次的字。
    """
    if ocr_engine is None:
        ocr_engine = ddddocr.DdddOcr(beta=True, show_ad=False)

    with Image.open(BytesIO(image_bytes)) as source:
        gray = source.convert("L")

    width, height = gray.size
    if width < 100 or height < 80:
        raise OCRRecognitionError('InvalidImageSize')

    bottom = gray.crop((0, int(height * 0.40), width, int(height * 0.96)))
    denoised = bottom.filter(ImageFilter.MedianFilter(size=3))
    bw = denoised.point(lambda value: 0 if value < 115 else 255)

    boxes = _segment_char_boxes(bw)
    if len(boxes) < 2:
        raise OCRRecognitionError('InstructionIndexAmbiguous')

    ocr_engine.set_ranges(candidate_charset)
    recognized_chars = []
    char_scores = {}

    for box in boxes:
        view_gray = ImageOps.expand(denoised.crop(box), border=12, fill=255)
        view_bw = ImageOps.expand(bw.crop(box), border=12, fill=255)
        readings = [
            _classification_with_confidence(_pil_to_png_bytes(view), ocr_engine)
            for view in (view_gray, view_bw)
        ]
        texts = [normalize_ocr_text(text) for text, _ in readings]
        scores = [score for _, score in readings]
        if not texts[0] or texts[0] != texts[1]:
            continue
        if any(score is None or not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
            continue
        ch = texts[0][0]
        min_score = min(scores)
        if min_score >= OCR_MIN_CONFIDENCE:
            recognized_chars.append(ch)
            char_scores[ch] = min(char_scores.get(ch, min_score), min_score)

    if not recognized_chars:
        raise OCRRecognitionError('RecognitionDisagreement')

    counts = Counter(recognized_chars)
    duplicates = [ch for ch, cnt in counts.items() if cnt >= 2]
    if len(duplicates) != 1:
        raise OCRRecognitionError('RecognitionDisagreement')

    target_char = duplicates[0]
    return None, target_char, char_scores[target_char]
    
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
