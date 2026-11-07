import re
import unicodedata
from dataclasses import dataclass
from ...clients.dialogue import find_button, signature
from ...target_bots import (JR_BUY_AMOUNTS, JR_NEXT_PAGE_KEYWORDS, JR_PENDING_KEYWORDS,
                            JR_VERIFY_KEYWORDS, JR_EXPORT_KEYWORDS)

JR_PAGE_RE = re.compile(r'(?:第\s*)?(\d+)\s*(?:页\s*[,，、;；/]?\s*共\s*|[/／]\s*(?:共\s*)?)(\d+)\s*页?', re.I)
JR_TOTAL_HEADER_RE = re.compile(
    r'^\s*🔍[\ufe0e\ufe0f]?\s*共\s*(\d+)\s*条\s*信息'
    r'(?:\s*[(（]?\s*' + JR_PAGE_RE.pattern + r'\s*[)）]?)?\s*[:：]?\s*$', re.I)
_PAGE_LINE_RE = re.compile(r'^\s*[(（]?\s*' + JR_PAGE_RE.pattern + r'\s*[)）]?\s*[:：]?\s*$', re.I)


@dataclass(frozen=True)
class ResultHeader:
    total_records: int
    current_page: int | None = None
    total_pages: int | None = None


def _first_line(message):
    return next((line.strip() for line in (getattr(message, 'text', '') or '').splitlines()
                 if line.strip()), '')


def parse_result_header(message):
    match = JR_TOTAL_HEADER_RE.fullmatch(unicodedata.normalize('NFKC', _first_line(message)))
    if match:
        count, current, total = match.groups()
        return ResultHeader(int(count), int(current) if current else None,
                            int(total) if total else None)
    return None


def is_jr_pending_message(message):
    return any(k in (getattr(message, 'text', '') or '') for k in JR_PENDING_KEYWORDS)


def parse_query_cooldown(message):
    match = re.search(r'查询过于频繁[，,]\s*请\s*(\d{1,6})\s*秒后再试',
                      getattr(message, 'text', '') or '')
    return max(1, int(match.group(1))) if match else None


def find_buy_button(message):
    generic = None
    for r, row in enumerate(getattr(message, 'buttons', None) or []):
        for c, b in enumerate(row):
            text = b.text or ''
            if '购买' in text:
                generic = generic or (r, c, text)
                if any(int(n) in JR_BUY_AMOUNTS for n in re.findall(r'\d+', text)):
                    return r, c, text
    return generic


def find_next_page_button(message):
    for r, row in enumerate(getattr(message, 'buttons', None) or []):
        for c, b in enumerate(row):
            if b.text == '下一页':
                return r, c, b.text
    return find_button(message, JR_NEXT_PAGE_KEYWORDS)


def is_verification_message(message):
    text = getattr(message, 'text', '') or ''
    if '验证通过' in text:
        return False
    if any(k in text for k in JR_VERIFY_KEYWORDS):
        return True
    return bool(getattr(message, 'media', None) and getattr(message, 'buttons', None)
                and not find_buy_button(message) and not find_next_page_button(message)
                and not find_button(message, JR_EXPORT_KEYWORDS))


def parse_page_info(message):
    header = parse_result_header(message)
    if header and header.current_page is not None:
        current, total = header.current_page, header.total_pages
        if not 0 < current <= total:
            raise ValueError('invalid_page_marker')
        return current, total
    match = _PAGE_LINE_RE.fullmatch(unicodedata.normalize('NFKC', _first_line(message)))
    if match:
        current, total = map(int, match.groups())
        if not 0 < current <= total:
            raise ValueError('invalid_page_marker')
        return current, total
    return None


def clean_page_text(message):
    lines = (getattr(message, 'text', '') or '').strip().splitlines()
    if not lines:
        return ''
    header = parse_result_header(message)
    if header:
        lines[0] = f'🔍共{header.total_records}条信息'
    elif _PAGE_LINE_RE.fullmatch(unicodedata.normalize('NFKC', lines[0])):
        lines.pop(0)
    return '\n'.join(lines).strip()


def page_content_signature(message):
    return parse_page_info(message), clean_page_text(message), signature(message)[1]


def classify(message):
    if parse_query_cooldown(message) is not None:
        return 'cooldown'
    if is_verification_message(message):
        return 'manual'
    if is_jr_pending_message(message):
        return 'pending'
    if getattr(message, 'document', None):
        return 'result'
    text = getattr(message, 'text', '') or ''
    if any(k in text for k in ('积分不足', '余额不足', '查询失败')):
        return 'failed'
    if any(k in text for k in ('未查询到', '未找到', '无查询结果')):
        return 'result'
    if (find_buy_button(message) or parse_page_info(message) or find_next_page_button(message)
            or find_button(message, JR_EXPORT_KEYWORDS) or parse_result_header(message)):
        return 'result'
    # Unknown text must not be charged, cached, or recorded as a successful result.
    return 'unknown'


def normalize_jr_full_result(text):
    lines, header, blank = [], False, False
    for raw in (text or '').replace('`', '').splitlines():
        line = raw.rstrip()
        match = JR_TOTAL_HEADER_RE.fullmatch(unicodedata.normalize('NFKC', line.strip()))
        if match:
            if header:
                continue
            header = True
            line = f'🔍共{int(match.group(1))}条信息'
        if not line.strip():
            if lines and not blank:
                lines.append('')
            blank = True
            continue
        lines.append(line)
        blank = False
    return '\n'.join(lines).strip()
