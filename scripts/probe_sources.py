#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_sources.py
================
혜택 추출/키움 수집/이미지 인식 구현을 위한 1회 통합 조사.
결과는 data/probe_findings.json 에 (값은 절단해서) 기록 → 세션에서 작은 파일만 읽음.

조사 항목:
  1) 키움: 이벤트 목록 XHR/API 엔드포인트 탐지 (Playwright 네트워크 캡처)
  2) KB:  PC 진행중 이벤트 JSP 의 개별 상세 링크 구조 (requests)
  3) NH:  목록 li 의 mNo/상세 URL 구조 (Playwright)
  4) 이미지: 이미지 배너 상세 페이지의 <img> 구조 (삼성 3722, 미래에셋)
"""

import asyncio
import json
import re

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

findings = {}


# ── 2) KB 상세 링크 구조 (requests) ────────────────────────────────
def probe_kb():
    out = {}
    url = "https://www.kbsec.com/cs/notice/jsp/CUST_09_0003.jsp"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        r.encoding = r.apparent_encoding
        html = r.text
        out["status"] = r.status_code
        out["len"] = len(html)
        # 이벤트 상세로 보이는 링크: go.able / seq / idt / eventView / fnView / goView
        anchors = re.findall(
            r"<a[^>]+(?:href|onclick)=[\"']([^\"']*(?:able|seq=|event|View|goDetail|fnGo)[^\"']*)[\"']",
            html, re.IGNORECASE)
        out["anchor_samples"] = list(dict.fromkeys(anchors))[:20]
        # onclick 함수 패턴
        out["onclick_fns"] = list(dict.fromkeys(
            re.findall(r"onclick=[\"'](\w+\([^\"']{0,80})", html)))[:20]
        # 날짜를 포함한 행(li/tr) 의 원문 일부
        rows = re.findall(r"<(?:li|tr)[^>]*>.*?</(?:li|tr)>", html, re.DOTALL)
        date_rows = [re.sub(r"\s+", " ", x)[:600] for x in rows
                     if re.search(r"\d{4}[.\-]\d{1,2}[.\-]\d{1,2}", x)]
        out["date_row_html"] = date_rows[:3]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ── 키움/NH/이미지: Playwright ─────────────────────────────────────
async def probe_kiwoom(browser):
    out = {}
    for label, url in [
        ("desktop", "https://www1.kiwoom.com/h/customer/event/VIngEventView"),
        ("mobile", "https://www.kiwoom.com/e/m/common/event/VIngEventView"),
    ]:
        xhrs = []
        page = await browser.new_page(user_agent=UA, locale="ko-KR")

        async def on_response(resp):
            try:
                u = resp.url
                ct = resp.headers.get("content-type", "")
                interesting = ("json" in ct or re.search(r"(api|json|list|event|evt|Ajax|ajax|\.do)", u))
                if not interesting:
                    return
                body = ""
                if "json" in ct or "text" in ct or "html" in ct:
                    try:
                        body = (await resp.text())[:400]
                    except Exception:
                        body = ""
                # 이벤트성 데이터로 보이는지(날짜/이벤트 키워드)
                score = bool(re.search(r"\d{4}[.\-]?\d{2}[.\-]?\d{2}|이벤트|event|title|subject", body))
                xhrs.append({"url": u[:180], "status": resp.status, "ct": ct[:40],
                             "blen": len(body), "hit": score, "snip": body[:200]})
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(5000)
            out[label + "_final"] = page.url
        except Exception as e:
            out[label + "_error"] = f"{type(e).__name__}: {str(e)[:150]}"
        # 이벤트성 응답 우선 정렬
        xhrs.sort(key=lambda x: (not x["hit"], -x["blen"]))
        out[label] = xhrs[:25]
        # iframe 들
        out[label + "_frames"] = [f.url[:150] for f in page.frames][:10]
        await page.close()
    return out


async def probe_nh(browser):
    out = {}
    url = "https://m.nhsec.com/customer/event/eventList"
    page = await browser.new_page(user_agent=UA, locale="ko-KR")
    try:
        await page.goto(url, wait_until="commit", timeout=45000)
        await page.wait_for_timeout(7000)
        items = await page.evaluate(r"""
        () => {
          const out = [];
          const nodes = document.querySelectorAll('div.txtRight, li');
          for (const el of nodes) {
            const t = (el.innerText||'').replace(/\s+/g,' ').trim();
            if (!t.includes('이벤트기간')) continue;
            if (el.tagName==='LI' && el.querySelector('li')) continue;
            const a = el.closest('a') || el.querySelector('a');
            const attrs = {};
            const tgt = a || el;
            for (const n of (tgt.getAttributeNames?tgt.getAttributeNames():[])) attrs[n] = (tgt.getAttribute(n)||'').slice(0,120);
            out.push({ text: t.slice(0,80), tag: tgt.tagName,
                       html: tgt.outerHTML.slice(0,300), attrs });
            if (out.length>=6) break;
          }
          return out;
        }
        """)
        out["items"] = items
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    finally:
        await page.close()
    return out


async def probe_images(browser):
    out = {}
    targets = [
        ("삼성_3722", "https://www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=eventView&MenuSeqNo=3722"),
        ("미래에셋_목록", "https://securities.miraeasset.com/mw/mki/mki7000/r01.do"),
    ]
    for label, url in targets:
        page = await browser.new_page(user_agent=UA, locale="ko-KR")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000)
            imgs = await page.evaluate(r"""
            () => Array.from(document.querySelectorAll('img'))
              .map(i => ({src: (i.currentSrc||i.src||'').slice(0,200),
                          alt: (i.alt||'').slice(0,80),
                          w: i.naturalWidth, h: i.naturalHeight}))
              .filter(i => i.w >= 200 && i.h >= 150)
              .slice(0, 15)
            """)
            out[label] = imgs
            body = await page.inner_text("body")
            out[label + "_textlen"] = len(" ".join(body.split()))
        except Exception as e:
            out[label] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        finally:
            await page.close()
    return out


async def main():
    findings["kb"] = probe_kb()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        findings["kiwoom"] = await probe_kiwoom(browser)
        findings["nh"] = await probe_nh(browser)
        findings["images"] = await probe_images(browser)
        await browser.close()

    import pathlib
    pathlib.Path("data").mkdir(exist_ok=True)
    with open("data/probe_findings.json", "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=1)
    print("[probe] data/probe_findings.json 기록 완료")
    print(json.dumps(findings, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    asyncio.run(main())
