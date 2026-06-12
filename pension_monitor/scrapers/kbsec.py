# -*- coding: utf-8 -*-
"""KB증권 모바일 이벤트 목록.

실측: 목록이 XHR 로 지연 로드(초기 body 는 메뉴/탭만) → linkcd=et* 앵커가
나타날 때까지 대기 + 스크롤. 상세는 go.able?linkcd=etXXXXX.
"""

import re

from .base import load_page, debug_dump

LIST_URL = "https://m.kbsec.com/go.able?linkcd=m06020000"

_PERIOD_RE = re.compile(
    r"(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})\s*~\s*(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})")

JS_ET_ANCHORS = r"""
() => Array.from(document.querySelectorAll('a'))
  .map(a => {
    const h = (a.getAttribute('href') || '') + ' ' + (a.getAttribute('onclick') || '');
    const m = h.match(/linkcd=(et\w+)/i);
    return m ? { text: (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 250),
                 linkcd: m[1] } : null;
  })
  .filter(x => x && x.text.length > 4)
  .slice(0, 60)
"""


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=5000)
    try:
        try:
            await page.wait_for_selector("a[href*='linkcd=et'], a[onclick*='linkcd=et']",
                                         timeout=20000)
        except Exception:
            # 지연 로드 유도: 스크롤
            for _ in range(3):
                await page.mouse.wheel(0, 1500)
                await page.wait_for_timeout(2000)
        items = await page.evaluate(JS_ET_ANCHORS)
        events, seen = [], set()
        for it in items:
            text = it["text"]
            m = _PERIOD_RE.search(text)
            name = (text[: m.start()] if m else text).strip(" :~-·.")
            if not name or len(name) < 4 or name in seen:
                continue
            seen.add(name)
            ev = {
                "firm_name": "KB증권",
                "event_name": name[:120],
                "start_date": None,
                "end_date": None,
                "event_url": f"https://m.kbsec.com/go.able?linkcd={it['linkcd']}",
                "raw_text": text,
            }
            if m:
                ev["start_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                ev["end_date"] = f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
            events.append(ev)
        if not events:
            await debug_dump(page, "KB증권")
        return events
    finally:
        await page.close()
