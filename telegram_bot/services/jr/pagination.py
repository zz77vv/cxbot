import asyncio
from .result_parser import (parse_page_info, clean_page_text, page_content_signature,
                            find_next_page_button, is_verification_message, classify)
from ...target_bots import JR_MAX_PAGES, JR_PAGE_INTERVAL


class PaginationService:
    def __init__(self, target):
        self.target = target

    async def collect(self, client, first_message, max_pages=JR_MAX_PAGES):
        if not 1 <= max_pages <= JR_MAX_PAGES:
            raise ValueError('分页上限不得扩大')
        pages, seen, visited = {}, set(), set()
        current, expected_total = first_message, None
        for index in range(max_pages):
            if is_verification_message(current):
                raise ManualVerification('verification_during_pagination')
            if classify(current) != 'result' and index == 0:
                raise ValueError('unrecognized_result')
            info = parse_page_info(current)
            content = page_content_signature(current)
            if content in seen:
                raise ValueError('duplicate_page_content')
            seen.add(content)
            if info:
                number, total = info
                if number in visited or (pages and number != max(pages) + 1) or (not pages and number != 1):
                    raise ValueError('duplicate_or_missing_page')
                if expected_total is not None and expected_total != total:
                    raise ValueError('page_total_changed')
                expected_total = total
                visited.add(number)
            else:
                if expected_total is not None:
                    raise ValueError('page_marker_disappeared')
                number, total = index + 1, None
            pages[number] = clean_page_text(current)
            if info and number == total:
                return '\n\n'.join(pages.values()).strip()
            button = find_next_page_button(current)
            if not button:
                if info and number < total:
                    raise ValueError('next_page_button_missing')
                return '\n\n'.join(pages.values()).strip()
            if index + 1 == max_pages:
                raise ValueError('page_limit_exceeded')
            changed = await self.target.click_and_wait(client, current, button)
            if not changed:
                raise TimeoutError('next_page_timeout')
            if (getattr(changed, 'text', '') or '') == (getattr(current, 'text', '') or ''):
                raise ValueError('page_did_not_advance')
            new_info = parse_page_info(changed)
            if info and new_info and new_info[0] <= info[0]:
                raise ValueError('page_did_not_advance')
            current = changed
            await asyncio.sleep(JR_PAGE_INTERVAL)
        raise ValueError('page_limit_exceeded')


class ManualVerification(RuntimeError):
    pass
