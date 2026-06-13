# -*- coding: utf-8 -*-
"""KB증권 모바일 이벤트 목록.

실측: 목록이 XHR 로 지연 로드(초기 body 는 메뉴/탭만) → linkcd=et* 앵커가
나타날 때까지 대기 + 스크롤. 상세는 go.able?linkcd=etXXXXX.
"""

import re

from .base import load_page, debug_dump

LIST_URL = "https://m.kbsec.com/go.able?linkcd=m06020000"
DESKTOP_LIST_URL = "https://www.kbsec.com/cs/notice/jsp/CUST_09_0003.jsp"  # 진행중인 이벤트 (PC)

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
    # 1차: PC 진행중 이벤트 JSP (서버렌더링 기대) — requests
    from .static_generic import fetch_html, parse_generic, debug_static
    try:
        html = fetch_html(DESKTOP_LIST_URL, retries=3)
        events = parse_generic(html, "KB증권", DESKTOP_LIST_URL)
        if events:
            return events
        debug_static(html, "KB증권(PC)")
    except Exception as e:
        print(f"[KB] PC 목록 실패: {type(e).__name__}: {e}")

    # 2차: PC 페이지 브라우저 렌더링
    try:
        page = await load_page(browser, DESKTOP_LIST_URL, wait_ms=8000)
        try:
            from .base import JS_GENERIC_ITEMS
            items = await page.evaluate(JS_GENERIC_ITEMS)
            events, seen = [], set()
            for it in items:
                m = _PERIOD_RE.search(it["text"])
                if not m:
                    continue
                name = it["text"][: m.start()].strip(" :~-·.")
                if not name or len(name) < 4 or name in seen:
                    continue
                seen.add(name)
                events.append({
                    "firm_name": "KB증권",
                    "event_name": name[:120],
                    "start_date": f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
                    "end_date": f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}",
                    "event_url": it.get("href") or DESKTOP_LIST_URL,
                    "raw_text": it["text"],
                })
            if events:
                return events
            await debug_dump(page, "KB증권(PC렌더)")
        finally:
            await page.close()
    except Exception as e:
        print(f"[KB] PC 렌더 실패: {type(e).__name__}")

    # 3차: 모바일 (XHR 지연 로드 대기 + 스크롤)
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
