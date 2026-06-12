# -*- coding: utf-8 -*-
"""삼성증권 모바일웹 이벤트 목록.

실측: 기간이 <span class="data">YYYY-MM-DD ~ YYYY-MM-DD</span> 에 있고
제목/카테고리는 상위 컨테이너에 함께 있음 → span.data 기준 컨테이너 역추적.
카테고리 탭에 '연금' 존재 (텍스트에 카테고리 라벨 포함됨).
"""

import re

from .base import load_page, debug_dump

LIST_URL = "https://www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=eventList"

_PERIOD_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\s*~\s*(\d{4})-(\d{1,2})-(\d{1,2})")

JS_ITEMS = r"""
() => {
  const out = [];
  for (const span of document.querySelectorAll('span.data')) {
    const period = (span.innerText || '').trim();
    if (!/\d{4}-\d{1,2}-\d{1,2}/.test(period)) continue;
    // 제목이 포함될 만큼 큰 컨테이너로 역추적 (최대 4단계)
    let node = span, container = null;
    for (let i = 0; i < 4 && node.parentElement; i++) {
      node = node.parentElement;
      const t = (node.innerText || '').trim().replace(/\s+/g, ' ');
      if (t.length > period.length + 6) { container = node; break; }
    }
    if (!container) continue;
    const text = (container.innerText || '').trim().replace(/\s+/g, ' ');
    const a = container.closest('a[href]') || container.querySelector('a[href]');
    out.push({ text: text.slice(0, 300), period,
               href: a ? a.href : null,
               onclick: a ? (a.getAttribute('onclick') || '').slice(0, 150) : '' });
    if (out.length >= 60) break;
  }
  return out;
}
"""

_CATEGORIES = ["국내주식", "연금", "금융상품/ISA", "해외주식", "파생", "기타"]


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=6000)
    try:
        items = await page.evaluate(JS_ITEMS)
        events, seen = [], set()
        for it in items:
            m = _PERIOD_RE.search(it["period"]) or _PERIOD_RE.search(it["text"])
            if not m:
                continue
            text = it["text"]
            # 제목 = 기간/카테고리/버튼 라벨 제거
            name = text.replace(it["period"], " ")
            name = re.sub(r"^\[?신규\]?", "", name).strip()
            category = next((c for c in _CATEGORIES if name.endswith(c)), None)
            if category:
                name = name[: -len(category)].strip()
            name = re.sub(r"(신청하기|자세히보기|바로가기|오늘마감|D-\d+)\s*$", "", name).strip()
            name = re.sub(r"\s{2,}", " ", name)
            if not name or name in seen:
                continue
            seen.add(name)
            start = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            end = f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
            # 상세 URL: goIngView('3808', ...) → mbw eventView
            href = it.get("href") or ""
            vm = re.search(r"goIngView\('(\d+)'", href + " " + (it.get("onclick") or ""))
            if vm:
                url = ("https://www.samsungpop.com/mbw/customer/noticeEvent.do"
                       f"?cmd=eventView&MenuSeqNo={vm.group(1)}")
            elif href.startswith("http"):
                url = href
            else:
                url = LIST_URL
            events.append({
                "firm_name": "삼성증권",
                "event_name": name[:120],
                "start_date": start,
                "end_date": end,
                "event_url": url,
                "raw_text": text + (f" [카테고리:{category}]" if category else ""),
                "_category": category,
            })
        if not events:
            await debug_dump(page, "삼성증권")
        return events
    finally:
        await page.close()
