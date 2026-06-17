#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dump_structure.py
=================
파서 작성을 위한 이벤트 목록 DOM 구조 덤프.
각 사이트에서 날짜 패턴(YYYY.MM.DD 등)이 포함된 목록성 요소와 앵커를 추출해 출력한다.
"""

import asyncio
import json
import re

DATE_RE = r"\d{4}[.\-/년\s]{1,2}\d{1,2}[.\-/월\s]{1,2}\d{1,2}"

# 브라우저에서 실행할 수집 스크립트: 날짜 패턴을 포함한 '가장 안쪽' 요소들을 찾고
# 해당 요소에서 가장 가까운 a[href] 와 li/tr 컨테이너 정보를 보고한다.
JS_PROBE = r"""
() => {
  const dateRe = /\d{4}[.\-\/년]\s?\d{1,2}[.\-\/월]\s?\d{1,2}/;
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('li, tr, dl, div, p, span, a');
  for (const el of nodes) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 300 || !dateRe.test(t)) continue;
    // 같은 텍스트를 가진 자식이 있으면 자식이 더 안쪽 → 부모는 스킵
    let inner = false;
    for (const c of el.children) {
      const ct = (c.innerText || '').trim().replace(/\s+/g, ' ');
      if (ct && dateRe.test(ct) && ct.length >= t.length * 0.8) { inner = true; break; }
    }
    if (inner) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    const a = el.closest('a[href]') || el.querySelector('a[href]');
    const container = el.closest('li, tr');
    out.push({
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 60),
      text: t.slice(0, 180),
      href: a ? a.getAttribute('href') : null,
      onclick: a ? (a.getAttribute('onclick') || '').slice(0, 120) : null,
      container: container ? container.tagName.toLowerCase() + '.' + (container.className || '').toString().slice(0, 50) : null,
    });
    if (out.length >= 25) break;
  }
  return out;
}
"""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

PW_TARGETS = [
    ("미래에셋증권", "https://securities.miraeasset.com/mw/mki/mki7000/r01.do"),
    ("삼성증권",     "https://www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=eventList"),
    ("키움증권",     "https://www1.kiwoom.com/h/customer/event/VIngEventView"),
    ("KB증권",       "https://m.kbsec.com/go.able?linkcd=m06020000"),
    ("NH투자증권",   "https://m.nhqv.com/customer/event/eventList"),
]


async def probe_playwright():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for name, url in PW_TARGETS:
            print(f"\n{'='*70}\n[{name}] {url}\n{'='*70}")
            page = await browser.new_page(user_agent=UA, locale="ko-KR")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
                items = await page.evaluate(JS_PROBE)
                print(json.dumps(items, ensure_ascii=False, indent=1))
            except Exception as e:
                print(f"ERROR: {type(e).__name__}: {e}")
            finally:
                await page.close()
        await browser.close()


def probe_koreainvestment():
    """한투: 서버렌더링 → requests + BeautifulSoup. 간헐 거부 대비 재시도."""
    import time
    import requests
    from bs4 import BeautifulSoup

    url = "https://securities.koreainvestment.com/main/customer/notice/Event.jsp?gubun=i"
    print(f"\n{'='*70}\n[한국투자증권] {url}\n{'='*70}")
    html = None
    for attempt in range(5):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding
            html = r.text
            break
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {type(e).__name__}: {e}")
            time.sleep(2 ** attempt)
    if html is None:
        print("ERROR: all attempts failed")
        return
    print(f"  html length: {len(html)}")
    soup = BeautifulSoup(html, "html.parser")
    date_re = re.compile(DATE_RE)
    count = 0
    for el in soup.find_all(["li", "tr", "dt", "dd", "a"]):
        t = " ".join(el.get_text(" ", strip=True).split())
        if not t or len(t) > 300 or not date_re.search(t):
            continue
        # 안쪽 우선: 동일 패턴 자식 있으면 스킵
        if any(date_re.search(" ".join(c.get_text(" ", strip=True).split()))
               for c in el.find_all(["li", "tr", "dt", "dd", "a"])):
            continue
        a = el if el.name == "a" else el.find("a", href=True)
        print(json.dumps({
            "tag": el.name,
            "cls": " ".join(el.get("class", []))[:60],
            "text": t[:180],
            "href": a.get("href") if a else None,
        }, ensure_ascii=False))
        count += 1
        if count >= 25:
            break


if __name__ == "__main__":
    probe_koreainvestment()
    asyncio.run(probe_playwright())
