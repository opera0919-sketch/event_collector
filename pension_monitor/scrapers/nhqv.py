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
  const out = [];
  // 실측: 이벤트명+기간이 div.txtRight 에 함께 있음
  let nodes = Array.from(document.querySelectorAll('div.txtRight'));
  if (!nodes.length) {
    nodes = Array.from(document.querySelectorAll('li'))
      .filter(li => (li.innerText || '').includes('이벤트기간') && !li.querySelector('li'));
  }
  for (const el of nodes) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t.includes('이벤트기간')) continue;
    const a = el.closest('a[href]') || el.querySelector('a[href]');
    const li = el.closest('li');
    let mno = null;
    for (const node of [a, el, li]) {
      if (!node || !node.getAttributeNames) continue;
      for (const attr of node.getAttributeNames()) {
        const m = (node.getAttribute(attr) || '').match(/mNo[='"]?(\d{2,})/);
        if (m) { mno = m[1]; break; }
      }
      if (mno) break;
    }
    out.push({ text: t, mno });
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
            name = text.split("이벤트기간")[0].strip(" :")
            if not name or len(name) < 3 or name in seen:
                continue
            seen.add(name)
            url = (f"https://m.nhsec.com/customer/event/eventView?mNo={it['mno']}"
                   if it.get("mno") else LIST_URLS[0])
            events.append({
                "firm_name": "NH투자증권",
                "event_name": name[:120],
                "start_date": _to_iso(m.group(1)),
                "end_date": _to_iso(m.group(2)),
                "event_url": url,
                "raw_text": text,
            })
        if not events:
            await debug_dump(page, "NH투자증권")
        return events
    finally:
        await page.close()
