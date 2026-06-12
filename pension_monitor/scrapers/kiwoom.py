# -*- coding: utf-8 -*-
"""키움증권 이벤트 전체보기.

1차 프로브에서 날짜 패턴 미검출 → 범용 추출 + 이벤트성 앵커 수집 이중 전략.
0건이면 debug_dump 로 구조를 로그에 남겨 다음 라운드에 셀렉터 확정.
"""

import re

from .base import load_page, debug_dump, JS_GENERIC_ITEMS

LIST_URL = "https://www1.kiwoom.com/h/customer/event/VIngEventView"

_PERIOD_RE = re.compile(
    r"(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})\s*~\s*(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})")

# 이벤트 상세로 보이는 앵커 수집 (날짜가 목록에 없을 경우 대비)
JS_EVENT_ANCHORS = r"""
() => Array.from(document.querySelectorAll('a[href]'))
  .filter(a => /Event|event|evt/i.test(a.getAttribute('href') || '')
               && (a.innerText || '').trim().length > 8)
  .slice(0, 60)
  .map(a => ({ text: (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 250),
               href: a.href }))
"""


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=8000)
    try:
        events, seen = [], set()

        def add(text, href):
            m = _PERIOD_RE.search(text)
            name = (text[: m.start()] if m else text).strip(" :~-·.")
            name = re.sub(r"(이벤트\s?기간|기간)$", "", name).strip(" :")
            if not name or len(name) < 4 or name in seen:
                return
            seen.add(name)
            ev = {
                "firm_name": "키움증권",
                "event_name": name[:120],
                "start_date": None,
                "end_date": None,
                "event_url": href or LIST_URL,
                "raw_text": text,
            }
            if m:
                ev["start_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                ev["end_date"] = f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
            events.append(ev)

        for it in await page.evaluate(JS_GENERIC_ITEMS):
            add(it["text"], it.get("href"))
        if not events:
            for it in await page.evaluate(JS_EVENT_ANCHORS):
                # 메뉴/네비 앵커 제외: 상세 페이지 패턴만
                if re.search(r"(EventView|evtUser|EC\d{6})", it["href"] or ""):
                    add(it["text"], it["href"])
        if not events:
            await debug_dump(page, "키움증권")
        return events
    finally:
        await page.close()
