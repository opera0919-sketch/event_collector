# -*- coding: utf-8 -*-
"""NH투자증권 모바일웹: li 항목에 "{이벤트명} 이벤트기간 :YYYY.MM.DD ~ YYYY.MM.DD"."""

import re

from .base import load_page, debug_dump

LIST_URL = "https://m.nhqv.com/customer/event/eventList"

_PERIOD_RE = re.compile(r"이벤트기간\s*:?\s*(\d{4}\.\d{1,2}\.\d{1,2})\s*~\s*(\d{4}\.\d{1,2}\.\d{1,2})")

JS_ITEMS = r"""
() => {
  const out = [];
  for (const li of document.querySelectorAll('li')) {
    const t = (li.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t.includes('이벤트기간')) continue;
    // 중첩 li 중 가장 안쪽(이름 포함된 텍스트가 충분히 긴 것) 선택
    if (li.querySelector('li')) continue;
    const a = li.closest('a[href]') || li.querySelector('a[href]');
    let detail = null;
    const el = a || li;
    for (const attr of el.getAttributeNames ? el.getAttributeNames() : []) {
      const v = el.getAttribute(attr) || '';
      const m = v.match(/eventView\?mNo=(\d+)/) || v.match(/mNo[='"]?(\d+)/);
      if (m) { detail = m[1]; break; }
    }
    out.push({ text: t, mno: detail });
    if (out.length >= 60) break;
  }
  return out;
}
"""


def _to_iso(d):
    y, m, dd = d.split(".")
    return f"{y}-{int(m):02d}-{int(dd):02d}"


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=6000)
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
            url = (f"https://m.nhqv.com/customer/event/eventView?mNo={it['mno']}"
                   if it.get("mno") else LIST_URL)
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
