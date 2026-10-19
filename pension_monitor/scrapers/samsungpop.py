# -*- coding: utf-8 -*-
"""삼성증권 모바일웹 이벤트 목록.

두 가지 마크업을 모두 지원한다.
- 구형: 기간이 <span class="data">YYYY-MM-DD ~ YYYY-MM-DD</span> 에 있고 제목/카테고리는
  상위 컨테이너에 함께 있음 → span.data 기준 컨테이너 역추적.
- 신형(2026-10 실측): span.data 가 사라지고 앵커 한 줄에 전부 들어온다.
  innerText = "<카테고리> <이벤트명> <시작> ~ <종료>" (카테고리가 접두),
  href = "javascript:goIngView('3883', '', 'H');"
  → span.data 경로가 0건이면 앵커 경로로 폴백한다. 어느 쪽이 와도 깨지지 않게 둘 다 둔다.
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

# 신형 마크업: 기간을 품은 앵커를 그대로 수집 (span.data 가 없을 때의 폴백)
JS_ANCHOR_ITEMS = r"""
() => {
  const dateRe = /\d{4}-\d{1,2}-\d{1,2}\s*~\s*\d{4}-\d{1,2}-\d{1,2}/;
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 300 || !dateRe.test(t)) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    out.push({ text: t.slice(0, 300), period: (t.match(dateRe) || [''])[0],
               href: a.getAttribute('href') || '',
               onclick: (a.getAttribute('onclick') || '').slice(0, 150) });
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
        if not items:
            # span.data 소실(2026-10 실측) — 앵커 한 줄에서 직접 뽑는다.
            items = await page.evaluate(JS_ANCHOR_ITEMS)
            if items:
                print(f"[삼성증권] span.data 0건 → 앵커 폴백으로 {len(items)}건 수집")
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
            else:
                # 신형 마크업은 카테고리가 접두다. 한 번만 떼어낸다 — 제목이 같은 낱말로
                # 시작하는 경우('국내주식 국내주식 고객감사제이벤트')를 깎지 않기 위함.
                category = next((c for c in _CATEGORIES if name.startswith(c)), None)
                if category:
                    name = name[len(category):].strip()
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
