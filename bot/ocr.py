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


def _tight_crop_with_padding(
    band: Image.Image, left: int, right: int, *, threshold: int = 160, border: int = 10
) -> Image.Image:
    """按垂直投影紧致裁剪字符并在四周补纯白留白，提升边缘识别率。"""
    sub = band.crop((left, 0, right, band.size[1]))
    sw, sh = sub.size
    pix = sub.load()
    rows = [y for y in range(sh) if any(pix[x, y] < threshold for x in range(sw))]
    if rows:
        top = max(0, rows[0] - 2)
        bottom = min(sh, rows[-1] + 3)
        sub = sub.crop((0, top, sw, bottom))
    padded = ImageOps.expand(sub, border=border, fill=255)
    return padded.convert("RGB")


def _safe_classify(image_bytes: bytes, engine) -> tuple[str, float | None] | None:
    try:
        return _classify_with_confidence(image_bytes, engine)
    except (StopIteration, RuntimeError):
        return None


def _match_unique_candidate(target_char: str, candidate_labels: list[str], conf: float) -> tuple[int, str, float]:
    matched_indices = [i for i, label in enumerate(candidate_labels) if label == target_char]
    if len(matched_indices) != 1:
        raise OCRError("NonUniqueCandidateMatch")
    return matched_indices[0], target_char, conf


def recognize_duplicate_char_sync(
    image_bytes: bytes,
    candidate_labels: list[str],
    engine,
) -> tuple[int, str, float]:
    """识别「点击图中出现两次的字」验证码（紧致补白 + 双视图 + 微旋投票 + 整行兜底）。"""
    if not candidate_labels:
        raise OCRError("InvalidCandidates")

    with Image.open(BytesIO(image_bytes)) as src:
        image = src.convert("RGB")

    width, height = image.size
    if width < 100 or height < 80:
        raise OCRError("InvalidImageSize")

    gray = image.convert("L")
    band = gray.crop((0, int(height * 0.35), width, int(height * 0.90)))
    denoised = band.filter(ImageFilter.MedianFilter(size=3))

    merged: list[list[int]] = []
    used_threshold = SEGMENT_THRESHOLDS[0]
    for threshold in SEGMENT_THRESHOLDS:
        merged = _segment_band(denoised, threshold)
        if len(merged) >= 2:
            used_threshold = threshold
            break

    charset = "".join(dict.fromkeys(ch for label in candidate_labels for ch in label))
    candidate_set = set(candidate_labels)
    engine.set_ranges(charset)

    counts: dict[str, int] = {}
    confs: dict[str, float] = {}
    saw_invalid_conf = False
    saw_low_conf = False
    segment_crops: list[Image.Image] = []
    segment_best: list[tuple[str, float] | None] = []

    # 1. 第一级：切分单字 + 严苛双视图一致校验
    if len(merged) >= 2:
        for left, right in merged:
            crop = _tight_crop_with_padding(band, left, right, threshold=max(150, used_threshold + 20))
            segment_crops.append(crop)
            views = (crop, ImageOps.autocontrast(ImageOps.grayscale(crop)).convert("RGB"))
            readings = [_classify_with_confidence(_to_png_bytes(v), engine) for v in views]
            texts = [normalize_text(t) for t, _ in readings]
            scores = [s for _, s in readings]

            # 记录单视图中置信度最高的候选字，供第二级微旋/放宽投票使用
            best_single: tuple[str, float] | None = None
            for t, s in zip(texts, scores):
                if t and t[0] in candidate_set and s is not None and math.isfinite(s) and OCR_MIN_CONFIDENCE <= s <= 1:
                    if best_single is None or s > best_single[1]:
                        best_single = (t[0], s)
            segment_best.append(best_single)

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
        if len(duplicates) == 1:
            return _match_unique_candidate(duplicates[0], candidate_labels, confs[duplicates[0]])

    # 若既没有命中，且完全因为无效/低置信度导致无有效计票，直接维持原错误语义
    if len(merged) >= 2 and not counts and (saw_invalid_conf or saw_low_conf):
        if saw_invalid_conf:
            raise OCRError("InvalidConfidence")
        raise OCRError("LowConfidence")

    # 2. 第二级：对倾斜/未达成双视图一致的切分块做 ±10° 微旋与单视图投票兜底
    if len(segment_crops) >= 2:
        vote_counts: dict[str, int] = {}
        vote_confs: dict[str, float] = {}
        for idx, crop in enumerate(segment_crops):
            best = segment_best[idx]
            if best is None:
                for angle in (-10, 10):
                    rot = crop.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
                    reading = _safe_classify(_to_png_bytes(rot), engine)
                    if reading is None:
                        continue
                    t, s = normalize_text(reading[0]), reading[1]
                    if t and t[0] in candidate_set and s is not None and math.isfinite(s) and OCR_MIN_CONFIDENCE <= s <= 1:
                        if best is None or s > best[1]:
                            best = (t[0], s)
            if best is not None:
                ch, sc = best
                vote_counts[ch] = vote_counts.get(ch, 0) + 1
                vote_confs[ch] = min(vote_confs.get(ch, sc), sc)
        vote_dups = [ch for ch, n in vote_counts.items() if n >= 2]
        if len(vote_dups) == 1:
            return _match_unique_candidate(vote_dups[0], candidate_labels, vote_confs[vote_dups[0]])

    # 3. 第三级：整行直接识别兜底（应对轻度粘连或投影切分不全）
    clean_band = denoised.point(lambda v: 0 if v < 140 else 255).convert("RGB")
    line_views = (
        ImageOps.expand(clean_band, border=10, fill=(255, 255, 255)),
        ImageOps.expand(band.convert("RGB"), border=10, fill=(255, 255, 255)),
    )
    for lv in line_views:
        reading = _safe_classify(_to_png_bytes(lv), engine)
        if reading is None:
            continue
        txt, sc = normalize_text(reading[0]), reading[1]
        if sc is None or not math.isfinite(sc) or sc < OCR_MIN_CONFIDENCE:
            continue
        line_counts: dict[str, int] = {}
        for ch in txt:
            if ch in candidate_set:
                line_counts[ch] = line_counts.get(ch, 0) + 1
        line_dups = [ch for ch, n in line_counts.items() if n >= 2]
        if len(line_dups) == 1:
            return _match_unique_candidate(line_dups[0], candidate_labels, sc)

    if len(merged) < 2:
        raise OCRError("SegmentationFailed")
    raise OCRError("RecognitionDisagreement")


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
