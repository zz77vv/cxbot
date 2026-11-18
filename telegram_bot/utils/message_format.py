import html


def utf16_len(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def split_text_by_lines(text: str, limit: int = 3600, *, escape_html=False):
    """Split before escaping, preserving every character and UTF-16 limits."""
    if limit < 12:
        raise ValueError('limit too small')
    chunks, current, length = [], '', 0
    for line in text.splitlines(keepends=True):
        rendered = html.escape(line, quote=False) if escape_html else line
        size = utf16_len(rendered)
        if current and length + size > limit:
            chunks.append(current)
            current, length = '', 0
        for char in line:
            piece = html.escape(char, quote=False) if escape_html else char
            size = utf16_len(piece)
            if length + size > limit:
                chunks.append(current)
                current, length = '', 0
            current += piece
            length += size
    if current:
        chunks.append(current)
    return chunks or ['']


async def reply_long(message, text: str):
    for chunk in split_text_by_lines(text, escape_html=True):
        await message.reply_text(chunk, parse_mode='HTML')
