# -*- coding: utf-8 -*-
"""미래에셋증권 진행중 이벤트 목록.

실측 구조: 렌더링 후 li 안에 상태라벨(NEW/진행중/종료임박) + 기간(span) + 이벤트명은
배너 이미지(alt)로 표시 → li 단위로 기간과 img alt 를 추출.
정적 HTML 에는 기간이 없음(JS 주입) → Playwright 필수, 간헐 지연은 재시도로 대응.
"""

import re

from .base import load_page, debug_dump
from .static_generic import PERIOD_RE, parse_period

LIST_URL = "https://securities.miraeasset.com/mw/mki/mki7000/r01.do"

JS_LI_ITEMS = r"""
() => {
  const dateRe = /\d{4}[.\-\/]\s?\d{1,2}[.\-\/]\s?\d{1,2}\s*~\s*\d{4}[.\-\/]\s?\d{1,2}[.\-\/]\s?\d{1,2}/;
  const out = [];
  for (const li of document.querySelectorAll('li')) {
    const t = (li.innerText || '').trim().replace(/\s+/g, ' ');
    if (!dateRe.test(t) || li.querySelector('li') || t.length > 300) continue;
    const img = li.querySelector('img');
    const a = li.closest('a[href]') || li.querySelector('a[href]');
    out.push({
      text: t,
      alt: img ? (img.getAttribute('alt') || '') : '',
      src: img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '',
      href: a ? a.href : null,
      html: out.length < 2 ? li.outerHTML.slice(0, 500) : null,
    });
    if (out.length >= 40) break;
  }
  return out;
}
"""

_LABELS = re.compile(r"^(NEW|진행중|종료임박|마감임박|이벤트)\s*")


async def scrape(browser):
    page = await load_page(browser, LIST_URL, wait_ms=9000, retries=4, timeout_ms=60000)
    try:
        items = await page.evaluate(JS_LI_ITEMS)
        events, seen = [], set()
        for it in items:
            m = PERIOD_RE.search(it["text"])
            period = parse_period(m) if m else None
            if not period:
                continue
            alt = (it.get("alt") or "").strip()
            # alt 가 "이벤트명: X\n기간: ...\n대상: Y" 구조인 경우 파싱
            name, cond_hint = "", None
            if alt:
                nm = re.search(r"이벤트명\s*[:：]\s*([^\n]+)", alt)
                cm = re.search(r"대상\s*[:：]\s*([^\n]+)", alt)
                name = (nm.group(1) if nm else alt.splitlines()[0]).strip()
                cond_hint = cm.group(1).strip() if cm else None
                name = re.sub(r"(이벤트\s?배너|배너|이미지)$", "", name).strip()
            if not name:
                # alt 없으면 텍스트에서 라벨/기간 제거 후 잔여 텍스트 사용
                name = PERIOD_RE.sub(" ", it["text"])
                name = _LABELS.sub("", name).strip(" :~-·.")
            name = " ".join(name.split())
            if not name or len(name) < 4 or name in seen:
                if it.get("html"):
                    print(f"[debug:미래에셋] li 구조: {it['html']}")
                continue
            seen.add(name)
            src = it.get("src") or ""
            image_url = src if src.startswith("http") else (
                "https://securities.miraeasset.com" + src if src.startswith("/") else None)
            events.append({
                "firm_name": "미래에셋증권",
                "event_name": name[:120],
                "start_date": period[0],
                "end_date": period[1],
                "event_url": it.get("href") or LIST_URL,
                "raw_text": " ".join((name + " " + alt).split())[:300],
                "_conditions_hint": cond_hint,
                "_image_url": image_url,
            })
        if not events:
            await debug_dump(page, "미래에셋증권")
        return events
    finally:
        await page.close()
