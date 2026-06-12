# -*- coding: utf-8 -*-
"""KB증권 모바일 이벤트 목록.

1차 프로브에서 날짜 패턴 미검출. KB 이벤트 상세는 go.able?linkcd=etXXXXX 패턴
(검색으로 확인) → linkcd=et* 앵커를 이벤트 항목으로 수집.
날짜가 목록에 없으면 상세 페이지에서 추출 (main 단계에서 처리).
"""

import re

from .base import load_page, debug_dump, JS_GENERIC_ITEMS

LIST_URL = "https://m.kbsec.com/go.able?linkcd=m06020000"

_PERIOD_RE = re.compile(
    r"(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})\s*~\s*(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})")

JS_ET_ANCHORS = r"""
() => Array.from(document.querySelectorAll('a[href]'))
  .filter(a => /linkcd=et\w+/i.test(a.getAttribute('href') || '')
               || /linkcd=et\w+/i.test(a.getAttribute('onclick') || ''))
  .slice(0, 60)
  .map(a => {
    const h = a.getAttribute('href') || '';
    const o = a.getAttribute('onclick') || '';
    const m = (h + ' ' + o).match(/linkcd=(et\w+)/i);
    return { text: (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 250),
             linkcd: m ? m[1] : null };
  })
"""


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=8000)
    try:
        events, seen = [], set()

        def add(text, url):
            m = _PERIOD_RE.search(text)
            name = (text[: m.start()] if m else text).strip(" :~-·.")
            if not name or len(name) < 4 or name in seen:
                return
            seen.add(name)
            ev = {
                "firm_name": "KB증권",
                "event_name": name[:120],
                "start_date": None,
                "end_date": None,
                "event_url": url or LIST_URL,
                "raw_text": text,
            }
            if m:
                ev["start_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                ev["end_date"] = f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
            events.append(ev)

        for it in await page.evaluate(JS_GENERIC_ITEMS):
            add(it["text"], it.get("href"))
        if not events:
            for it in await page.evaluate(JS_ET_ANCHORS):
                if it.get("linkcd"):
                    add(it["text"], f"https://m.kbsec.com/go.able?linkcd={it['linkcd']}")
        if not events:
            await debug_dump(page, "KB증권")
        return events
    finally:
        await page.close()
