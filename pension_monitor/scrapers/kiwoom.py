# -*- coding: utf-8 -*-
"""키움증권 이벤트 목록.

실측: 헤드리스 브라우저는 에러 iframe, 정적 HTML(190KB)에는 기간 패턴 없음
(목록 XHR 주입). 상세 페이지는 evtUser*View 패턴 → 정적 HTML 의 evtUser 앵커에서
이벤트명(텍스트/img alt)을 얻고, 기간은 상세 페이지에서 추출.
"""

import re

from .static_generic import fetch_html, parse_generic, PERIOD_RE, parse_period

LIST_URLS = [
    "https://www1.kiwoom.com/wm/evt/evtMainEC210017View",  # 이벤트 전체보기 (검색 확인)
    "https://www1.kiwoom.com/h/customer/event/VIngEventView",
    "https://www.kiwoom.com/e/m/common/event/VIngEventView",  # 모바일
]

_ANCHOR_RE = re.compile(
    r"<a[^>]+href=[\"']([^\"']*evtUser\w*View[^\"']*)[\"'][^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ALT_RE = re.compile(r"alt=[\"']([^\"']+)[\"']")
MAX_DETAIL = 30


def _clean(s):
    return " ".join(_TAG_RE.sub(" ", s).split())


async def scrape(browser=None):
    events, seen = [], set()
    for url in LIST_URLS:
        try:
            html = fetch_html(url, retries=3)
        except Exception as e:
            print(f"[키움] {url} 실패: {type(e).__name__}")
            continue
        # 1) 기간이 포함된 일반 구조
        events = parse_generic(html, "키움증권", url)
        if events:
            return events
        # 2) evtUser*View 앵커 (이름만, 기간은 상세에서)
        candidates = []
        for m in _ANCHOR_RE.finditer(html):
            href, inner = m.group(1), m.group(2)
            name = _clean(inner)
            if not name:
                am = _ALT_RE.search(inner)
                name = am.group(1).strip() if am else ""
            name = re.sub(r"(이벤트\s?배너|배너|바로가기)$", "", name).strip()
            if not name or len(name) < 4 or name in seen:
                continue
            seen.add(name)
            if href.startswith("/"):
                base = url.split("/", 3)
                href = f"{base[0]}//{base[2]}{href}"
            candidates.append({"name": name, "href": href})
        if candidates:
            for c in candidates[:MAX_DETAIL]:
                ev = {
                    "firm_name": "키움증권",
                    "event_name": c["name"][:120],
                    "start_date": None,
                    "end_date": None,
                    "event_url": c["href"],
                    "raw_text": c["name"],
                }
                events.append(ev)
            # 연금 후보만 상세에서 기간 보강 (main.enrich 와 별개로 즉시 처리)
            from ..classify import is_pension
            for ev in events:
                if not is_pension(ev["event_name"]):
                    continue
                try:
                    dhtml = fetch_html(ev["event_url"], retries=2)
                    dm = PERIOD_RE.search(_clean(dhtml)[:5000])
                    p = parse_period(dm) if dm else None
                    if p:
                        ev["start_date"], ev["end_date"] = p
                except Exception:
                    pass
            return events
        # 디버그: evt 관련 앵커 표본
        hints = re.findall(r"href=[\"']([^\"']*(?:evt|Event)[^\"']{0,80})[\"']", html)[:15]
        print(f"[debug-static:키움증권] {url} evt 앵커 표본: {hints}")

    # 최후: 모바일 페이지 브라우저 렌더링 (메인 도메인은 헤드리스 차단 실측)
    if browser is not None:
        from .base import load_page, debug_dump, JS_GENERIC_ITEMS
        try:
            page = await load_page(browser, LIST_URLS[2], wait_ms=8000, retries=2)
            try:
                items = await page.evaluate(JS_GENERIC_ITEMS)
                for it in items:
                    m = PERIOD_RE.search(it["text"])
                    p = parse_period(m) if m else None
                    if not p:
                        continue
                    name = it["text"][: m.start()].strip(" :~-·.")
                    if not name or len(name) < 4 or name in seen:
                        continue
                    seen.add(name)
                    events.append({
                        "firm_name": "키움증권", "event_name": name[:120],
                        "start_date": p[0], "end_date": p[1],
                        "event_url": it.get("href") or LIST_URLS[2],
                        "raw_text": it["text"],
                    })
                if not events:
                    await debug_dump(page, "키움증권(모바일)")
            finally:
                await page.close()
        except Exception as e:
            print(f"[키움] 모바일 렌더 실패: {type(e).__name__}")
    return events
