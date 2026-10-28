import re
from ...clients.dialogue import find_button, signature
from ...target_bots import (JR_BUY_AMOUNTS, JR_NEXT_PAGE_KEYWORDS, JR_PENDING_KEYWORDS,
                            JR_VERIFY_KEYWORDS, JR_EXPORT_KEYWORDS)

JR_PAGE_RE = re.compile(r'[（(]?\s*第\s*(\d+)\s*页\s*[,，]\s*共\s*(\d+)\s*页\s*[）)]?', re.I)
JR_TOTAL_HEADER_RE = re.compile(r'^\s*🔍️?\s*共\s*(\d+)\s*条信息\s*[:：]?\s*$', re.I)


def is_jr_pending_message(message):
    return any(k in (getattr(message, 'text', '') or '') for k in JR_PENDING_KEYWORDS)


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
    match = JR_PAGE_RE.search(getattr(message, 'text', '') or '')
    if match:
        current, total = map(int, match.groups())
        if 0 < current <= total:
            return current, total
    return None


def clean_page_text(message):
    return JR_PAGE_RE.sub('', getattr(message, 'text', '') or '', count=1).strip()


def page_content_signature(message):
    return parse_page_info(message), clean_page_text(message), signature(message)[1]


def classify(message):
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
    if (find_buy_button(message) or parse_page_info(message)
            or find_button(message, JR_EXPORT_KEYWORDS) or JR_TOTAL_HEADER_RE.search(text.splitlines()[0] if text else '')):
        return 'result'
    # Unknown text must not be charged, cached, or recorded as a successful result.
    return 'unknown'


def normalize_jr_full_result(text):
    lines, header, blank = [], False, False
    for raw in (text or '').replace('`', '').splitlines():
        line = raw.rstrip()
        if JR_TOTAL_HEADER_RE.match(line.strip()):
            if header:
                continue
            header = True
        if not line.strip():
            if lines and not blank:
                lines.append('')
            blank = True
            continue
        lines.append(line)
        blank = False
    return '\n'.join(lines).strip()
