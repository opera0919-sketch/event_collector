#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""미래에셋증권 이벤트 목록의 대체 수집 경로 조사 (일회성 탐침).

배경: 2026-11-05 부터 모바일 웹 목록(/mw/mki/mki7000/r01.do)이
/public/mts/html/AppLink.html?id=event 로 넘어가고, 그 문서는 앱 스토어로 이동한다.
후보 URL 을 (1) 정적 요청 (2) 브라우저 렌더 두 방식으로 열어 무엇이 돌아오는지 기록한다.
결과: data/probe_mirae.json + 표준 출력. 외부로 보내는 것은 공개 페이지 GET 뿐이다.

실행: python scripts/probe_mirae.py
"""

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from pension_monitor.config import UA
from pension_monitor.scrapers.base import MOBILE_UA

BASE = "https://securities.miraeasset.com"
KNOWN_ID = "202609012"          # 운영 DB 에 있는 진행중 이벤트 (개인연금 입금 이벤트)
CANDIDATES = [
    ("mw_list", BASE + "/mw/mki/mki7000/r01.do"),
    ("mw_detail", BASE + f"/mw/mki/mki7000/v01.do?cs_ecis_id={KNOWN_ID}&pub_sect=O&mod=S"
                         "&returnURL=/mw/mki/mki7000/r01.do"),
    ("applink", BASE + "/public/mts/html/AppLink.html?id=event"),
    ("pc_home", BASE + "/"),
    ("pc_list_hki", BASE + "/hki/hki7000/r01.do"),
    ("pc_list_hki3", BASE + "/hki/hki3028/r01.do"),
    ("pc_detail_hki", BASE + f"/hki/hki7000/v01.do?cs_ecis_id={KNOWN_ID}"),
    ("mw_home", BASE + "/mw/main.do"),
    ("list_img_dir", BASE + f"/public/mw/event/{KNOWN_ID}/"),
]
_EVENT_HINT = re.compile(r"이벤트|event|mki7000|hki7000", re.I)


def _summ_html(html):
    text = " ".join(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html,
                           flags=re.S | re.I).split())
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    hrefs = re.findall(r"""(?:href|action|src)\s*=\s*["']([^"'#]{4,200})["']""", html, re.I)
    urls_in_js = re.findall(r"""["'](/[A-Za-z0-9_/.\-]{6,120}\.(?:do|json|jsp)[^"']{0,80})["']""", html)
    return {
        "len": len(html),
        "title": (title.group(1).strip()[:80] if title else None),
        "text_head": text[:300],
        "n_detailView": html.count("detailView"),
        "n_cs_ecis_id": html.count("cs_ecis_id"),
        "event_links": sorted({h for h in hrefs if _EVENT_HINT.search(h)})[:40],
        "do_urls_in_js": sorted(set(urls_in_js))[:60],
        "period_hits": re.findall(r"\d{4}\.\d{2}\.\d{2}\s*~\s*\d{4}\.\d{2}\.\d{2}", text)[:8],
    }


def static_probe(name, url, ua):
    out = {"name": name, "url": url}
    try:
        r = requests.get(url, headers={"User-Agent": ua, "Accept-Language": "ko-KR,ko;q=0.9"},
                         timeout=25, allow_redirects=True)
        r.encoding = r.apparent_encoding
        out.update(status=r.status_code, final_url=r.url,
                   redirects=[h.headers.get("Location") for h in r.history],
                   **_summ_html(r.text))
        if name == "applink":
            out["html_head"] = r.text[:3500]          # 이동 판단 로직을 보기 위해 원문 일부 보존
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


JS_DOM = r"""
() => ({
  n_li_detailView: document.querySelectorAll('li[onclick*="detailView"]').length,
  n_onclick_detail: document.querySelectorAll('[onclick*="detailView"], [onclick*="cs_ecis_id"], a[href*="cs_ecis_id"]').length,
  body_len: (document.body ? document.body.innerText : '').length,
  body_head: (document.body ? document.body.innerText : '').trim().replace(/\s+/g, ' ').slice(0, 300),
  event_anchors: Array.from(document.querySelectorAll('a')).map(a => ({
      t: (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 50),
      h: (a.getAttribute('href') || '').slice(0, 140),
      o: (a.getAttribute('onclick') || '').slice(0, 140)}))
    .filter(x => /이벤트|event/i.test(x.t + x.h + x.o)).slice(0, 30),
  sample_items: Array.from(document.querySelectorAll('[onclick*="detailView"]')).slice(0, 4).map(e => ({
      tag: e.tagName, o: (e.getAttribute('onclick') || '').slice(0, 120),
      t: (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 160)})),
  imgs_event: Array.from(document.querySelectorAll('img')).map(i => i.currentSrc || i.src || '')
    .filter(s => /event/i.test(s)).slice(0, 12),
})
"""


async def render_probe(browser, name, url, ua, guard):
    """브라우저 렌더. guard=True 면 타 도메인 이동을 막고 원래 문서를 유지한다."""
    from pension_monitor.scrapers import base
    out = {"name": name, "url": url, "guard": guard, "ua": "mobile" if ua == MOBILE_UA else "desktop"}
    xhr = []
    try:
        page = await browser.new_page(user_agent=ua, locale="ko-KR")
        page.on("request", lambda r: xhr.append(r.url[:200]) if r.resource_type in ("xhr", "fetch", "document") else None)
        if guard:
            page._pm_offsite = []
            await page.route("**/*", base._make_offsite_guard(page, "miraeasset.com"))
        try:
            await page.goto(url, wait_until="commit", timeout=40000)
            await page.wait_for_timeout(9000)
            out["final_url"] = page.url
            out["title"] = (await page.title())[:80]
            out.update(await page.evaluate(JS_DOM))
            out["blocked_offsite"] = base.offsite_attempts(page)
        finally:
            out["requests"] = [u for u in dict.fromkeys(xhr) if "google" not in u][:25]
            await page.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


async def main():
    from playwright.async_api import async_playwright
    findings = {"static": [], "render": []}
    for name, url in CANDIDATES:
        for label, ua in (("desktop", UA), ("mobile", MOBILE_UA)):
            f = static_probe(name, url, ua)
            f["ua"] = label
            findings["static"].append(f)
            print(f"[static:{label}] {name}: status={f.get('status')} final={str(f.get('final_url'))[:90]} "
                  f"len={f.get('len')} detailView={f.get('n_detailView')} ecis={f.get('n_cs_ecis_id')} "
                  f"periods={len(f.get('period_hits') or [])} err={f.get('error')}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for name in ("mw_list", "mw_detail", "pc_list_hki", "pc_home", "pc_detail_hki"):
            url = dict(CANDIDATES)[name]
            for ua in (UA, MOBILE_UA):
                f = await render_probe(browser, name, url, ua, guard=True)
                findings["render"].append(f)
                print(f"[render:{f['ua']}] {name}: final={str(f.get('final_url'))[:90]} "
                      f"li={f.get('n_li_detailView')} detail-like={f.get('n_onclick_detail')} "
                      f"body={f.get('body_len')} blocked={len(f.get('blocked_offsite') or [])} "
                      f"err={f.get('error')}")
        await browser.close()
    os.makedirs("data", exist_ok=True)
    with open("data/probe_mirae.json", "w", encoding="utf-8") as fp:
        json.dump(findings, fp, ensure_ascii=False, indent=1)
    print("저장: data/probe_mirae.json")


if __name__ == "__main__":
    asyncio.run(main())
