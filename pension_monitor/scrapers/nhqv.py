# -*- coding: utf-8 -*-
"""NH투자증권 모바일웹 진행중 이벤트.

실측 구조: <li> 안에 <div class="txtRight"> "{이벤트명} 이벤트기간 :YYYY.MM.DD ~ YYYY.MM.DD"
m.nhqv.com 은 m.nhsec.com 으로 리다이렉트 + 간헐 지연 → 직접 도메인, 긴 타임아웃, 재시도.
"""

import re

from .base import load_page, debug_dump

LIST_URLS = [
    "https://m.nhsec.com/customer/event/eventList",
    "https://m.nhqv.com/customer/event/eventList",
]

_PERIOD_RE = re.compile(r"이벤트기간\s*:?\s*(\d{4}\.\d{1,2}\.\d{1,2})\s*~\s*(\d{4}\.\d{1,2}\.\d{1,2})")

JS_ITEMS = r"""
() => {
  // 실측 구조: <a class="click_area"> 카드 안에
  //   <p class="event_img"><img src="/fileUpload/nhmobile/event/*.png" alt="이벤트명"></p>
  //   <div class="txtRight"> ... 이벤트기간 :YYYY.MM.DD ~ YYYY.MM.DD
  // → 이름은 img alt(깨끗함), 혜택은 배너 이미지(OCR 대상)
  const out = [];
  const seen = new Set();
  let cards = Array.from(document.querySelectorAll('a.click_area'));
  if (!cards.length) {
    cards = Array.from(document.querySelectorAll('li'))
      .filter(li => (li.innerText || '').includes('이벤트기간') && !li.querySelector('li'));
  }
  for (const el of cards) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t.includes('이벤트기간')) continue;
    if (seen.has(t)) continue; seen.add(t);
    const img = el.querySelector('img');
    out.push({
      text: t,
      alt: img ? (img.getAttribute('alt') || '') : '',
      src: img ? (img.getAttribute('src') || '') : '',
    });
    if (out.length >= 60) break;
  }
  return out;
}
"""


def _to_iso(d):
    y, m, dd = d.split(".")
    return f"{y}-{int(m):02d}-{int(dd):02d}"


async def scrape(browser):
    page, last = None, None
    for url in LIST_URLS:
        try:
            page = await load_page(browser, url, wait_ms=7000, retries=3, timeout_ms=45000)
            break
        except Exception as e:
            last = e
    if page is None:
        raise last
    try:
        items = await page.evaluate(JS_ITEMS)
        events, seen = [], set()
        for it in items:
            text = it["text"]
            m = _PERIOD_RE.search(text)
            if not m:
                continue
            # 이름: img alt 우선(깨끗), 없으면 텍스트 앞부분
            name = (it.get("alt") or "").strip() or text.split("이벤트기간")[0].strip(" :")
            if not name or len(name) < 3 or name in seen:
                continue
            seen.add(name)
            # 배너 이미지(OCR 대상) — 혜택/조건이 이미지에 있음
            src = it.get("src") or ""
            image_url = ("https://m.nhsec.com" + src if src.startswith("/")
                         else (src if src.startswith("http") else None))
            events.append({
                "firm_name": "NH투자증권",
                "event_name": name[:120],
                "start_date": _to_iso(m.group(1)),
                "end_date": _to_iso(m.group(2)),
                "event_url": LIST_URLS[0],
                "raw_text": text,
                "_image_url": image_url,
            })
        if not events:
            await debug_dump(page, "NH투자증권")
        return events
    finally:
        await page.close()
