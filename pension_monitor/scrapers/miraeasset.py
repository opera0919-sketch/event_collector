# -*- coding: utf-8 -*-
"""미래에셋증권 진행중 이벤트 목록.

페이지 로드가 간헐적으로 매우 느림 → commit 대기 + 재시도.
DOM 구조 미확정이라 범용 추출기(날짜 패턴 + 인접 앵커)로 파싱.
"""

import re

from .base import load_page, debug_dump, JS_GENERIC_ITEMS

LIST_URL = "https://securities.miraeasset.com/mw/mki/mki7000/r01.do"

_PERIOD_RE = re.compile(
    r"(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})\s*~\s*(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})")
_NOISE = ("당첨자", "유효기간", "인증범위", "지난 이벤트", "참여한 이벤트")


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=9000, retries=4, timeout_ms=40000)
    try:
        items = await page.evaluate(JS_GENERIC_ITEMS)
        events, seen = [], set()
        for it in items:
            text = it["text"]
            if any(n in text for n in _NOISE):
                continue
            m = _PERIOD_RE.search(text)
            if not m:
                continue
            name = text[: m.start()].strip(" :~-·.")
            name = re.sub(r"(이벤트\s?기간|기간)$", "", name).strip(" :")
            if not name or len(name) < 4 or name in seen:
                continue
            seen.add(name)
            events.append({
                "firm_name": "미래에셋증권",
                "event_name": name[:120],
                "start_date": f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
                "end_date": f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}",
                "event_url": it.get("href") or LIST_URL,
                "raw_text": text,
            })
        if not events:
            await debug_dump(page, "미래에셋증권")
        return events
    finally:
        await page.close()
