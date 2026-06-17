#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_access_test.py
===================
6개 증권사 이벤트 페이지 접근성 실측 (GitHub Actions runner = 해외 IP 환경).

검증 내용:
  1) HTTP 상태코드 / 최종 URL (리다이렉트 추적)
  2) Playwright 헤드리스 브라우저 렌더링 후 <title> 및 본문에 '이벤트' 텍스트 존재 여부
     → 해외 IP 차단·봇 차단·JS 렌더링 필요 여부를 한 번에 판별
"""

import asyncio
import sys

TARGETS = [
    ("미래에셋증권", "https://securities.miraeasset.com/mw/mki/mki7000/r01.do"),
    ("한국투자증권", "https://securities.koreainvestment.com/main/customer/notice/Event.jsp?gubun=i"),
    ("삼성증권",     "https://www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=eventList"),
    ("키움증권",     "https://www1.kiwoom.com/h/customer/event/VIngEventView"),
    ("KB증권",       "https://m.kbsec.com/go.able?linkcd=m06020000"),
    ("NH투자증권",   "https://m.nhqv.com/customer/event/eventList"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


async def check(browser, name, url):
    result = {"firm": name, "url": url}
    page = await browser.new_page(user_agent=UA, locale="ko-KR")
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)  # JS 렌더링 대기
        result["status"] = resp.status if resp else "no-response"
        result["final_url"] = page.url
        result["title"] = (await page.title()).strip()
        body = await page.inner_text("body")
        body = " ".join(body.split())
        result["body_len"] = len(body)
        result["has_event_kw"] = "이벤트" in body
        result["sample"] = body[:200]
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        await page.close()
    return result


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        results = [await check(browser, n, u) for n, u in TARGETS]
        await browser.close()

    print("\n" + "=" * 70)
    print("실측 결과 요약")
    print("=" * 70)
    ok = 0
    for r in results:
        if "error" in r:
            verdict = f"❌ ERROR  {r['error']}"
        elif r["status"] == 200 and r["has_event_kw"] and r["body_len"] > 500:
            verdict = "✅ OK"
            ok += 1
        else:
            verdict = f"⚠️ CHECK  status={r['status']} body_len={r.get('body_len')} 이벤트kw={r.get('has_event_kw')}"
        print(f"\n[{r['firm']}] {verdict}")
        print(f"  url      : {r['url']}")
        print(f"  final    : {r.get('final_url', '-')}")
        print(f"  title    : {r.get('title', '-')}")
        print(f"  sample   : {r.get('sample', '-')[:150]}")
    print(f"\n총평: {ok}/{len(TARGETS)} 접근 성공")
    return 0 if ok == len(TARGETS) else 0  # 실측 목적이므로 항상 성공 종료, 판정은 로그로


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
