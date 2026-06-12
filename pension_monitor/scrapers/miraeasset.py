# -*- coding: utf-8 -*-
"""미래에셋증권 진행중 이벤트 목록.

서버렌더링(.do) → requests 우선. 브라우저 로드는 간헐적으로 매우 느려 폴백으로만 사용.
"""

from .base import load_page, debug_dump, JS_GENERIC_ITEMS
from .static_generic import fetch_html, parse_generic, debug_static, PERIOD_RE, parse_period

LIST_URL = "https://securities.miraeasset.com/mw/mki/mki7000/r01.do"


async def scrape(browser):
    html = None
    try:
        html = fetch_html(LIST_URL)
        events = parse_generic(html, "미래에셋증권", LIST_URL)
        if events:
            return events
    except Exception as e:
        print(f"[미래에셋] 정적 수집 실패: {type(e).__name__}: {e}")

    # 폴백: 브라우저 렌더링
    try:
        page = await load_page(browser, LIST_URL, wait_ms=9000, retries=3, timeout_ms=60000)
    except Exception:
        if html:
            debug_static(html, "미래에셋증권")
        raise
    try:
        events, seen = [], set()
        for it in await page.evaluate(JS_GENERIC_ITEMS):
            m = PERIOD_RE.search(it["text"])
            if not m:
                continue
            name = it["text"][: m.start()].strip(" :~-·.")
            if not name or len(name) < 4 or name in seen:
                continue
            seen.add(name)
            start, end = parse_period(m)
            events.append({
                "firm_name": "미래에셋증권", "event_name": name[:120],
                "start_date": start, "end_date": end,
                "event_url": it.get("href") or LIST_URL, "raw_text": it["text"],
            })
        if not events:
            await debug_dump(page, "미래에셋증권")
            if html:
                debug_static(html, "미래에셋증권")
        return events
    finally:
        await page.close()
