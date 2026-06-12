# -*- coding: utf-8 -*-
"""키움증권 이벤트 전체보기.

헤드리스 브라우저에서는 에러 iframe 으로 빠짐(실측) → 정적 HTML(213KB, curl 정상)을
requests 로 받아 범용 파서로 추출. 0건이면 기간 패턴 문맥을 디버그 출력.
"""

from .static_generic import fetch_html, parse_generic, debug_static

LIST_URL = "https://www1.kiwoom.com/h/customer/event/VIngEventView"


async def scrape(browser=None):
    html = fetch_html(LIST_URL)
    events = parse_generic(html, "키움증권", LIST_URL)
    if not events:
        debug_static(html, "키움증권")
    return events
