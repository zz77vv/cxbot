"""单线程隔离的离线 OCR 服务（懒加载 ddddocr + 自适应阈值切分 + 按钮精确匹配）。"""
from __future__ import annotations

import asyncio
import logging
import math
import unicodedata
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageFilter, ImageOps

from .targets import OCR_IGNORE_BUTTON_KEYWORDS, OCR_MIN_CONFIDENCE

log = logging.getLogger(__name__)
SEGMENT_THRESHOLDS = (115, 140, 90)


class OCRError(RuntimeError):
    def __init__(self, code: str, confidence: float | None = None):
        self.code = code
        self.confidence = confidence
        super().__init__(code)


@dataclass(frozen=True)
class CaptchaCandidate:
    row: int
    col: int
    label: str
    normalized: str


def normalize_text(text: str) -> str:
    norm = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in norm if ch.isalnum() or "\u4e00" <= ch <= "\u9fff").lower()


def extract_captcha_candidates(message) -> list[CaptchaCandidate]:
    items: list[CaptchaCandidate] = []
    for r, row in enumerate(getattr(message, "buttons", None) or []):
        for c, btn in enumerate(row):
            label = (getattr(btn, "text", None) or "").strip()
            norm = normalize_text(label)
            if not norm:
                continue
            if any(kw in label for kw in OCR_IGNORE_BUTTON_KEYWORDS):
                continue
            items.append(CaptchaCandidate(r, c, label, norm))
    return items


def _to_png_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _default_engine_factory():
    # 懒加载 ddddocr/onnxruntime，降低启动耗时和闲时内存占用
    with warnings.catch_warnings():
        import ddddocr

        return ddddocr.DdddOcr(beta=True, show_ad=False)


def _segment_band(denoised: Image.Image, threshold: int) -> list[list[int]]:
    bw = denoised.point(lambda v: 0 if v < threshold else 255)
    bw_w, bw_h = bw.size
    pix = bw.load()
    col_counts = [sum(1 for y in range(bw_h) if pix[x, y] == 0) for x in range(bw_w)]

    segments: list[list[int]] = []
    in_seg = False
    start = 0
    for x, cnt in enumerate(col_counts):
        if cnt >= 5 and not in_seg:
            in_seg = True
            start = x
        elif cnt < 5 and in_seg:
            in_seg = False
            if x - start >= 12:
                segments.append([start, x])
    if in_seg and bw_w - start >= 12:
        segments.append([start, bw_w])

    merged: list[list[int]] = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] < 15:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)
    return merged


def _classify_with_confidence(image_bytes: bytes, engine) -> tuple[str, float | None]:
    result = engine.classification(image_bytes, probability=True, png_fix=True)
    if isinstance(result, str):
        return result, None
    if "text" in result:
        conf = result.get("confidence")
        return str(result.get("text") or ""), (float(conf) if conf is not None else None)

    charsets = result.get("charsets") or []
    probs = result.get("probability") or []
    chars: list[str] = []
    scores: list[float] = []
    prev = None
    for row in probs:
        if (
            len(row) != len(charsets)
            or not row
            or any(not math.isfinite(v) or (v != -1 and not 0 <= v <= 1) for v in row)
        ):
            raise OCRError("InvalidProbabilities")
        idx = max(range(len(row)), key=row.__getitem__)
        if row[idx] < 0:
            raise OCRError("InvalidProbabilities")
        ch = charsets[idx]
        if ch:
            if idx != prev:
                chars.append(ch)
            scores.append(float(row[idx]))
        prev = idx if ch else None
    return "".join(chars), (sum(scores) / len(scores) if scores else 0.0)


def recognize_duplicate_char_sync(
    image_bytes: bytes,
    candidate_labels: list[str],
    engine,
) -> tuple[int, str, float]:
    """识别「点击图中出现两次的字」验证码（自适应多阈值切分 + 按钮精确匹配）。"""
    if not candidate_labels:
        raise OCRError("InvalidCandidates")

    with Image.open(BytesIO(image_bytes)) as src:
        image = src.convert("RGB")

    width, height = image.size
    if width < 100 or height < 80:
        raise OCRError("InvalidImageSize")

    gray = image.convert("L")
    band = gray.crop((0, int(height * 0.40), width, int(height * 0.90)))
    denoised = band.filter(ImageFilter.MedianFilter(size=3))
    bw_h = band.size[1]

    merged: list[list[int]] = []
    for threshold in SEGMENT_THRESHOLDS:
        merged = _segment_band(denoised, threshold)
        if len(merged) >= 2:
            break
    if len(merged) < 2:
        raise OCRError("SegmentationFailed")

    charset = "".join(dict.fromkeys(ch for label in candidate_labels for ch in label))
    engine.set_ranges(charset)

    counts: dict[str, int] = {}
    confs: dict[str, float] = {}
    saw_invalid_conf = False
    saw_low_conf = False

    for left, right in merged:
        crop = band.crop((left, 0, right, bw_h)).convert("RGB")
        views = (crop, ImageOps.autocontrast(ImageOps.grayscale(crop)).convert("RGB"))
        readings = [_classify_with_confidence(_to_png_bytes(v), engine) for v in views]
        texts = [normalize_text(t) for t, _ in readings]
        scores = [s for _, s in readings]
        if not texts[0] or texts[0] != texts[1]:
            continue
        if any(s is None or not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
            saw_invalid_conf = True
            continue
        sc = min(scores)
        if sc < OCR_MIN_CONFIDENCE:
            saw_low_conf = True
            continue
        ch = texts[0][0]
        counts[ch] = counts.get(ch, 0) + 1
        confs[ch] = min(confs.get(ch, sc), sc)

    duplicates = [ch for ch, n in counts.items() if n >= 2]
    if len(duplicates) != 1:
        if saw_invalid_conf and not counts:
            raise OCRError("InvalidConfidence")
        if saw_low_conf and not counts:
            raise OCRError("LowConfidence")
        raise OCRError("RecognitionDisagreement")

    target_char = duplicates[0]
    matched_indices = [i for i, label in enumerate(candidate_labels) if label == target_char]
    if len(matched_indices) != 1:
        raise OCRError("NonUniqueCandidateMatch")
    return matched_indices[0], target_char, confs[target_char]


class OCRService:
    def __init__(self, timeout: float = 20.0, engine_factory=None):
        self.timeout = timeout
        self._engine_factory = engine_factory or _default_engine_factory
        self._engine = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-worker")
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future | None = None
        self._closed = False

    def _run_sync(self, image_bytes: bytes, candidate_labels: list[str]):
        if self._engine is None:
            self._engine = self._engine_factory()
        return recognize_duplicate_char_sync(image_bytes, candidate_labels, self._engine)

    async def recognize_captcha(
        self, image_bytes: bytes, candidate_labels: list[str]
    ) -> tuple[int, str, float]:
        async with self._lock:
            if self._closed or (self._pending is not None and not self._pending.done()):
                raise OCRError("OCRBusyOrClosed")
            loop = asyncio.get_running_loop()
            self._pending = loop.run_in_executor(
                self._executor, self._run_sync, image_bytes, candidate_labels
            )
            self._pending.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )
            try:
                return await asyncio.wait_for(asyncio.shield(self._pending), self.timeout)
            except TimeoutError:
                log.warning("OCR 识别超时")
                raise OCRError("TimeoutError") from None

    async def close(self) -> None:
        self._closed = True
        if self._pending and not self._pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._pending), self.timeout)
            except Exception:
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)
