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


# ── NH eventList.json 구조 (requests) ─────────────────────────────
def probe_nh_json():
    out = {}
    url = "https://m.nhsec.com/customer/event/eventList.json"
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                         "X-Requested-With": "XMLHttpRequest",
                         "Referer": "https://m.nhsec.com/customer/event/eventList"}, timeout=30)
        out["status"] = r.status_code
        out["len"] = len(r.text)
        try:
            j = r.json()
        except Exception:
            out["not_json_head"] = r.text[:500]
            return out

        def walk(node, path, depth=0):
            """이벤트 목록으로 보이는 list[dict] 를 찾아 키/샘플을 기록."""
            if depth > 5:
                return
            if isinstance(node, list) and node and isinstance(node[0], dict):
                keys = sorted(node[0].keys())
                if any(re.search(r"mNo|title|subject|nm|name|start|end|period|date|content",
                                 " ".join(keys), re.I)):
                    out.setdefault("lists", []).append({
                        "path": path, "count": len(node), "keys": keys,
                        "sample": {k: str(v)[:120] for k, v in node[0].items()}})
                for i, it in enumerate(node[:2]):
                    walk(it, f"{path}[{i}]", depth + 1)
            elif isinstance(node, dict):
                out.setdefault("top_paths", []).append(path or "root")
                for k, v in node.items():
                    walk(v, f"{path}.{k}" if path else k, depth + 1)

        walk(j, "")
        out["top_paths"] = list(dict.fromkeys(out.get("top_paths", [])))[:30]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
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


# 상세 페이지 이미지 + 텍스트 길이 (OCR 대상 파악용)
_DETAIL_IMG_JS = r"""
() => Array.from(document.querySelectorAll('img'))
  .map(i => ({src: (i.currentSrc||i.src||i.getAttribute('data-src')||'').slice(0,200),
              alt: (i.alt||'').slice(0,80), w: i.naturalWidth, h: i.naturalHeight}))
  .filter(i => i.src && i.w >= 200 && i.h >= 150
               && !/logo|icon|btn|sprite|nav_|bullet|arrow|blank|dot/i.test(i.src))
  .slice(0, 12)
"""


async def probe_detail_nav(browser, list_url, item_js, click_js, wait_list=7000):
    """리스트 첫 항목을 클릭해 ① 상세 URL 구조 ② 상세 페이지 이미지/텍스트를 파악.

    item_js: 리스트 항목들의 링크/onclick/속성 표본을 반환하는 JS
    click_js: 첫 항목(또는 그 링크)을 click() 하는 JS
    """
    out = {"xhr": []}
    page = await browser.new_page(user_agent=UA, locale="ko-KR")

    async def on_resp(resp):
        try:
            u = resp.url
            if re.search(r"(event|evt|detail|view|notice|\.do|Ajax|ajax|api)", u, re.I) \
                    and not re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|woff|ico)(\?|$)", u, re.I):
                ct = resp.headers.get("content-type", "")
                body = ""
                if "json" in ct or "text" in ct or "html" in ct:
                    try:
                        body = (await resp.text())[:300]
                    except Exception:
                        body = ""
                out["xhr"].append({"url": u[:200], "status": resp.status,
                                   "ct": ct[:40], "snip": body[:200]})
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        await page.goto(list_url, wait_until="commit", timeout=45000)
        await page.wait_for_timeout(wait_list)
        out["items"] = await page.evaluate(item_js)
        out["before_url"] = page.url
        out["xhr"] = []  # 리스트 로딩 XHR 은 버리고, 클릭 이후만 본다
        try:
            await page.evaluate(click_js)
            await page.wait_for_timeout(6000)
            out["after_url"] = page.url
            out["frames"] = [f.url[:160] for f in page.frames][:8]
            out["detail_imgs"] = await page.evaluate(_DETAIL_IMG_JS)
            body = await page.inner_text("body")
            compact = " ".join(body.split())
            out["detail_textlen"] = len(compact)
            out["detail_text_head"] = compact[:600]
        except Exception as e:
            out["click_error"] = f"{type(e).__name__}: {str(e)[:150]}"
        out["xhr"] = out["xhr"][:20]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    finally:
        await page.close()
    return out


# 미래에셋: 리스트 li(기간 포함, 배너 img) → 첫 항목 클릭
_MIRAE_ITEM_JS = r"""
() => {
  const dateRe = /\d{4}[.\-\/]\s?\d{1,2}[.\-\/]\s?\d{1,2}/;
  const out = [];
  for (const li of document.querySelectorAll('li')) {
    const t = (li.innerText||'').replace(/\s+/g,' ').trim();
    if (!dateRe.test(t) || li.querySelector('li') || t.length > 300) continue;
    const a = li.closest('a[href]') || li.querySelector('a[href]');
    const img = li.querySelector('img');
    const tgt = a || li;
    const attrs = {};
    for (const n of (tgt.getAttributeNames?tgt.getAttributeNames():[])) attrs[n]=(tgt.getAttribute(n)||'').slice(0,140);
    out.push({ text: t.slice(0,90), tag: tgt.tagName,
               href: a?a.getAttribute('href'):null,
               onclick: tgt.getAttribute('onclick')||'',
               imgsrc: img?(img.getAttribute('src')||''):'',
               html: li.outerHTML.slice(0,400), attrs });
    if (out.length>=6) break;
  }
  return out;
}
"""
_MIRAE_CLICK_JS = r"""
() => {
  const dateRe = /\d{4}[.\-\/]\s?\d{1,2}[.\-\/]\s?\d{1,2}/;
  for (const li of document.querySelectorAll('li')) {
    const t = (li.innerText||'').replace(/\s+/g,' ').trim();
    if (!dateRe.test(t) || li.querySelector('li') || t.length > 300) continue;
    const tgt = li.querySelector('a, button, img') || li;
    tgt.click();
    return;
  }
}
"""

# NH: a.click_area 첫 카드 클릭
_NH_ITEM_JS = r"""
() => {
  const out = [];
  let cards = Array.from(document.querySelectorAll('a.click_area'));
  if (!cards.length) cards = Array.from(document.querySelectorAll('li'))
      .filter(li => (li.innerText||'').includes('이벤트기간') && !li.querySelector('li'));
  for (const el of cards.slice(0,6)) {
    const attrs = {};
    for (const n of (el.getAttributeNames?el.getAttributeNames():[])) attrs[n]=(el.getAttribute(n)||'').slice(0,140);
    const img = el.querySelector('img');
    out.push({ text:(el.innerText||'').replace(/\s+/g,' ').trim().slice(0,90),
               tag: el.tagName, onclick: el.getAttribute('onclick')||'',
               imgsrc: img?(img.getAttribute('src')||''):'',
               html: el.outerHTML.slice(0,400), attrs });
  }
  return out;
}
"""
_NH_CLICK_JS = r"""
() => {
  let el = document.querySelector('a.click_area');
  if (!el) el = Array.from(document.querySelectorAll('li'))
      .find(li => (li.innerText||'').includes('이벤트기간') && !li.querySelector('li'));
  if (el) el.click();
}
"""


async def probe_nh_mno(browser):
    """NH: 각 카드의 mNo 가 어디서 오는지(컨테이너 HTML / 인라인 스크립트) 파악."""
    out = {}
    url = "https://m.nhsec.com/customer/event/eventList"
    page = await browser.new_page(user_agent=UA, locale="ko-KR")
    try:
        await page.goto(url, wait_until="commit", timeout=45000)
        await page.wait_for_timeout(7000)
        out["containers"] = await page.evaluate(r"""
        () => Array.from(document.querySelectorAll('a.click_area')).slice(0,3).map(a => {
          const li = a.closest('li') || a.parentElement;
          const all = {};
          for (const el of [a, li, ...a.querySelectorAll('*')]) {
            for (const n of (el.getAttributeNames?el.getAttributeNames():[])) {
              if (/mno|seq|idx|data-|id|num/i.test(n)) all[el.tagName+'.'+n] = el.getAttribute(n);
            }
          }
          return { liHtml: (li?li.outerHTML:'').slice(0,800), dataAttrs: all };
        })
        """)
        html = await page.content()
        out["mno_hits"] = list(dict.fromkeys(re.findall(r".{0,40}mNo.{0,60}", html, re.I)))[:20]
        out["fn_hits"] = list(dict.fromkeys(
            re.findall(r"(?:function\s+\w+|on\w+\s*=|\.click_area)[^\n;{]{0,90}", html)))[:25]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    finally:
        await page.close()
    return out


async def probe_nh_list(browser):
    """NH: eventList 로딩 시 발생하는 목록 데이터 XHR(본문 포함) 캡처 → mNo 출처 확인."""
    out = {"xhr": []}
    url = "https://m.nhsec.com/customer/event/eventList"
    page = await browser.new_page(user_agent=UA, locale="ko-KR")

    async def on_resp(resp):
        try:
            u = resp.url
            if re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|woff|ico)(\?|$)", u, re.I):
                return
            if "nhsec" not in u and "nhqv" not in u:
                return
            ct = resp.headers.get("content-type", "")
            body = ""
            if "json" in ct or "text" in ct or "html" in ct:
                try:
                    body = await resp.text()
                except Exception:
                    body = ""
            has = bool(re.search(r"mNo|이벤트기간|eventList|getList|listAjax", body, re.I))
            out["xhr"].append({"url": u[:200], "status": resp.status, "ct": ct[:40],
                               "blen": len(body), "has_mno": ("mNo" in body),
                               "hit": has, "snip": body[:400] if has else ""})
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(6000)
        out["xhr"].sort(key=lambda x: (not x["hit"], not x["has_mno"], -x["blen"]))
        out["xhr"] = out["xhr"][:15]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    finally:
        await page.close()
    return out


async def probe_detail(browser):
    out = {}
    out["미래에셋"] = await probe_detail_nav(
        browser, "https://securities.miraeasset.com/mw/mki/mki7000/r01.do",
        _MIRAE_ITEM_JS, _MIRAE_CLICK_JS, wait_list=9000)
    out["NH"] = await probe_detail_nav(
        browser, "https://m.nhsec.com/customer/event/eventList",
        _NH_ITEM_JS, _NH_CLICK_JS, wait_list=7000)
    out["NH_mno"] = await probe_nh_mno(browser)
    out["NH_list"] = await probe_nh_list(browser)
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
    findings["nh_json"] = probe_nh_json()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        findings["kiwoom"] = await probe_kiwoom(browser)
        findings["nh"] = await probe_nh(browser)
        findings["images"] = await probe_images(browser)
        # 개별 상세 페이지 URL 구조 + 상세 이미지/텍스트 (상세-우선 수집 전환용)
        findings["detail_nav"] = await probe_detail(browser)
        await browser.close()

    import pathlib
    pathlib.Path("data").mkdir(exist_ok=True)
    with open("data/probe_findings.json", "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=1)
    print("[probe] data/probe_findings.json 기록 완료")
    print(json.dumps(findings, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    asyncio.run(main())
