# -*- coding: utf-8 -*-
"""스크레이퍼 공통 유틸 (Playwright 페이지 로드 + 범용 추출)."""

import asyncio

from ..config import UA

# 날짜 패턴을 포함한 가장 안쪽 요소 + 인접 앵커를 수집하는 범용 추출기
JS_GENERIC_ITEMS = r"""
() => {
  const dateRe = /\d{4}[.\-\/년]\s?\d{1,2}[.\-\/월]\s?\d{1,2}/;
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('li, tr, dl, div, p, a')) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 400 || !dateRe.test(t)) continue;
    let inner = false;
    for (const c of el.children) {
      const ct = (c.innerText || '').trim().replace(/\s+/g, ' ');
      if (ct && dateRe.test(ct) && ct.length >= t.length * 0.8) { inner = true; break; }
    }
    if (inner) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    const a = el.closest('a[href]') || el.querySelector('a[href]');
    out.push({ text: t, href: a ? a.href : null,
               onclick: a ? (a.getAttribute('onclick') || '') : '' });
    if (out.length >= 60) break;
  }
  return out;
}
"""


async def load_page(browser, url, wait_ms=6000, retries=3, timeout_ms=30000):
    """간헐 지연/거부 대응: commit 까지만 기다린 뒤 고정 대기, 실패 시 재시도.

    성공 시 page 반환(호출자가 close), 모두 실패 시 마지막 예외 raise.
    """
    last = None
    for attempt in range(retries):
        page = await browser.new_page(user_agent=UA, locale="ko-KR")
        try:
            await page.goto(url, wait_until="commit", timeout=timeout_ms)
            await page.wait_for_timeout(wait_ms)
            return page
        except Exception as e:
            last = e
            await page.close()
            await asyncio.sleep(2 ** attempt)
    raise last


async def debug_dump(page, label, max_anchors=20):
    """파서가 0건일 때 구조 파악용 로그."""
    try:
        body = await page.inner_text("body")
        print(f"[debug:{label}] body({len(body)}자) 앞부분: {' '.join(body.split())[:600]}")
        anchors = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                 .filter(a => /event|evt|et0/i.test(a.getAttribute('href') || '')
                              || /이벤트/.test(a.innerText || ''))
                 .slice(0, 30)
                 .map(a => ({t: (a.innerText||'').trim().replace(/\\s+/g,' ').slice(0,100),
                             h: a.getAttribute('href'),
                             o: (a.getAttribute('onclick')||'').slice(0,100)}))""")
        for a in anchors[:max_anchors]:
            print(f"[debug:{label}] anchor: {a}")
        frames = [f.url for f in page.frames if f != page.main_frame]
        if frames:
            print(f"[debug:{label}] iframes: {frames}")
    except Exception as e:
        print(f"[debug:{label}] dump 실패: {e}")
