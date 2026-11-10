# -*- coding: utf-8 -*-
"""미래에셋증권 진행중 이벤트 목록 → 개별 상세 페이지.

실측 구조 (목록, JS 렌더):
  <li onclick="detailView('202606006','1','null','O')">
    <a class="eventLst" href="#none">
      <span class="ico_label evN"><em>NEW|진행중</em></span>
      <span class="img"><img src="/public/mw/event/{ID}/...jpg" alt="..."></span>
      <p class="subject">{이벤트명}</p>           ← 깨끗한 제목
      <span class="date">YYYY.MM.DD ~ YYYY.MM.DD</span>
상세 URL: /mw/mki/mki7000/v01.do?cs_ecis_id={ID}&pub_sect={pub_sect}&mod=S&returnURL=...
  → cs_ecis_id = detailView 1번째 인자, pub_sect = 4번째 인자.
  상세 페이지(euc-kr)는 본문 텍스트가 충실하고, 본문 이미지는
  /public/mw/event/{ID}/1-1_Mobile_detail_720*.jpg 형태 → 상세에서 추출/OCR.
정적 HTML 에는 기간이 없음(JS 주입) → Playwright 필수, 간헐 지연은 재시도로 대응.

수집 경로 (2026-11-10 실측으로 재구성 — scripts/probe_mirae.py, data/probe_mirae.json):
  2026-11-05 부터 모바일 웹 목록(/mw/mki/mki7000/r01.do)이 앱 연결 문서
  (/public/mts/html/AppLink.html?id=event)로 넘어가고 그 문서는 앱 스토어로 이동한다.
  목록 화면만 앱 전용이 됐고, 상세(v01.do?cs_ecis_id=…)와 아래 두 경로는 그대로 열린다.
  1) 목록 API  POST /mw/mki/mki7000/a01.json  — 모바일 홈의 '다양한 혜택 더보기'가 부르는
     JSON. {"list":[{cs_ecis_id, s_title, strt_day, end_day, pub_sect, evnt_sect,
     evnt_ctns_m_url, …}]}. 브라우저가 필요 없다.
  2) PC 목록   GET  /hki/hki7000/r05.do       — 서버 렌더 HTML,
     <dl class="eventCont"> … doView('{id}','{sect}','{url}') … dd.evTit / dd.evDate
  3) 모바일 목록(브라우저) — 종전 경로. 복구될 경우에 대비해 마지막 수단으로 남긴다.
     실패 모드: (a) 껍데기만 렌더(목록 li 미주입) → li 셀렉터를 최대 30초 대기
               (b) 타 도메인(앱 스토어) 이동 → 이동을 취소해 원래 문서 유지, 사유 분류
  세 경로 모두 상세 URL 은 같은 형식(DETAIL_URL)으로 만든다 → source_event_id(cs_ecis_id)와
  event_url 이 기존 DB 행과 그대로 이어진다.
"""

import json
import re
import time

import requests

from ..config import UA
from .base import (load_page, debug_dump, classify_empty, note_failure, clear_failure,
                   offsite_attempts, OffsiteRedirect)
from .static_generic import PERIOD_RE, parse_period, fetch_html

FIRM = "미래에셋증권"
SITE_HOST = "miraeasset.com"
LIST_SELECTOR = 'li[onclick*="detailView"]'
SELECTOR_TIMEOUT_MS = 30000     # 목록 li 가 나타나기를 기다리는 상한 (종전: 고정 9초)

BASE = "https://securities.miraeasset.com"
LIST_URL = f"{BASE}/mw/mki/mki7000/r01.do"
API_URL = f"{BASE}/mw/mki/mki7000/a01.json"
PC_LIST_URL = f"{BASE}/hki/hki7000/r05.do"
API_PAGE_SIZE = 50
MAX_PAGES = 5                   # 페이지 순회 상한 (진행중 이벤트는 통상 10~20건)
DETAIL_URL = (BASE + "/mw/mki/mki7000/v01.do"
              "?cs_ecis_id={cid}&pub_sect={sect}&mod=S&returnURL=/mw/mki/mki7000/r01.do")

# 목록 li 들에서 detailView 인자 + 제목 + 기간 + 배너를 구조적으로 수집
JS_LI_ITEMS = r"""
() => {
  const out = [];
  for (const li of document.querySelectorAll('li[onclick*="detailView"]')) {
    const oc = li.getAttribute('onclick') || '';
    const subj = li.querySelector('.subject, p.subject');
    const date = li.querySelector('.date, span.date');
    const img = li.querySelector('img');
    out.push({
      onclick: oc,
      subject: subj ? (subj.innerText || '').trim() : '',
      date: date ? (date.innerText || '').trim() : '',
      text: (li.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 300),
      alt: img ? (img.getAttribute('alt') || '') : '',
      src: img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '',
    });
    if (out.length >= 60) break;
  }
  return out;
}
"""

_DETAILVIEW_RE = re.compile(
    r"detailView\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'")
_LABELS = re.compile(r"^(NEW|진행중|종료임박|마감임박|이벤트)\s*")


def parse_items(items):
    """목록 li 항목(JS_LI_ITEMS 결과) → 이벤트 dict 목록. 브라우저 없이 검증 가능한 순수 함수."""
    events, seen = [], set()
    for it in items:
        # 기간: span.date 우선, 없으면 텍스트 전체에서
        m = PERIOD_RE.search(it.get("date") or "") or PERIOD_RE.search(it.get("text") or "")
        period = parse_period(m) if m else None
        if not period:
            continue
        # 이름: p.subject(깨끗) 우선 → img alt(=null 제외) → 텍스트 잔여
        name = (it.get("subject") or "").strip()
        alt = (it.get("alt") or "").strip()
        if not name and alt and alt.lower() != "null":
            name = alt
        if not name:
            name = PERIOD_RE.sub(" ", it.get("text") or "")
            name = _LABELS.sub("", name).strip(" :~-·.")
        name = " ".join(name.split())
        if not name or len(name) < 4 or name in seen:
            continue
        seen.add(name)
        # 상세 URL: detailView('{cid}', '{?}', '{?}', '{sect}')
        dm = _DETAILVIEW_RE.search(it.get("onclick") or "")
        cid = dm.group(1) if dm else None
        sect = dm.group(4) if dm else "O"
        if not cid:
            # 폴백: 배너 경로 /public/mw/event/{ID}/... 에서 ID 추출
            sm = re.search(r"/event/(\d+)/", it.get("src") or "")
            cid = sm.group(1) if sm else None
        event_url = DETAIL_URL.format(cid=cid, sect=sect) if cid else LIST_URL
        events.append({
            "firm_name": "미래에셋증권",
            "event_name": name[:120],
            "start_date": period[0],
            "end_date": period[1],
            "event_url": event_url,
            "raw_text": " ".join((name + " " + alt).split())[:300],
        })
    return events


def _iso_day(v):
    """'20261001' / '202610010000' → '2026-10-01'. 형식이 아니면 None."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", str(v or ""))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def _event(cid, name, start, end, sect="O", ext_url=None, extra=""):
    """세 경로 공용 이벤트 dict. 외부 페이지형(evnt_sect=2)만 그 URL 을, 나머지는 상세 URL."""
    url = DETAIL_URL.format(cid=cid, sect=sect or "O")
    ev = {"firm_name": FIRM, "event_name": name[:120], "start_date": start, "end_date": end,
          "event_url": url, "raw_text": " ".join((name + " " + (extra or "")).split())[:300]}
    if ext_url and str(ext_url).startswith("http"):
        # 본문이 외부 페이지에 있는 이벤트: 상세 조회는 그 페이지로, 식별자는 cs_ecis_id 유지
        ev["_content_url"] = ext_url
    return ev


def parse_api_items(rows):
    """목록 API(a01.json) 의 list → 이벤트 dict 목록 (순수 함수)."""
    events, seen = [], set()
    for r in rows or []:
        cid = str(r.get("cs_ecis_id") or "").strip()
        name = " ".join(str(r.get("s_title") or "").split())
        start = _iso_day(r.get("strt_day")) or _iso_day(r.get("pub_sdt"))
        end = _iso_day(r.get("end_day")) or _iso_day(r.get("pub_edt"))
        if not cid or len(name) < 4 or not (start and end) or cid in seen:
            continue
        seen.add(cid)
        ext = r.get("evnt_ctns_m_url") if str(r.get("evnt_sect")) == "2" else None
        events.append(_event(cid, name, start, end, sect=str(r.get("pub_sect") or "O"),
                             ext_url=ext, extra=_plain(r.get("expl"))))
    return events


def _plain(html):
    return " ".join(re.sub(r"<[^>]+>", " ", str(html or "")).split())[:160]


_PC_ITEM_RE = re.compile(
    r"doView\(\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)(.*?)</dl>", re.S)


def parse_pc_html(html):
    """PC '진행중인 이벤트'(r05.do) HTML → 이벤트 dict 목록 (순수 함수)."""
    events, seen = [], set()
    for cid, sect, url, body in _PC_ITEM_RE.findall(html or ""):
        tit = re.search(r'class="evTit"[^>]*>(.*?)</dd>', body, re.S)
        m = PERIOD_RE.search(_plain(body))
        period = parse_period(m) if m else None
        name = " ".join(_plain(tit.group(1) if tit else "").split())
        if not period or len(name) < 4 or cid in seen:
            continue
        seen.add(cid)
        events.append(_event(cid, name, period[0], period[1],
                             ext_url=url if sect == "2" else None))
    return events


def _decode(resp):
    for enc in ("utf-8", "cp949"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return resp.content.decode("utf-8", "replace")


def fetch_api(session=None, retries=3):
    """목록 API 를 페이지 단위로 읽는다. 새 ID 가 더 안 나오면 멈춘다(상한 MAX_PAGES)."""
    http = session or requests
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9",
               "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/mw/main.do"}
    rows, ids = [], set()
    for page in range(1, MAX_PAGES + 1):
        data = {"currentPage": str(page), "searchName": "", "strEnd": "S", "pub_chnl": "M",
                "searchColumn": "all", "maxPage": str(API_PAGE_SIZE)}
        last = None
        for attempt in range(retries):
            try:
                r = http.post(API_URL, data=data, headers=headers, timeout=25)
                r.raise_for_status()
                got = (json.loads(_decode(r)) or {}).get("list") or []
                break
            except Exception as e:
                last = e
                time.sleep(2 ** attempt)
        else:
            raise last
        fresh = [x for x in got if str(x.get("cs_ecis_id")) not in ids]
        if not fresh:
            break
        ids.update(str(x.get("cs_ecis_id")) for x in fresh)
        rows.extend(fresh)
        if len(got) < API_PAGE_SIZE:
            break
    return rows


def fetch_pc(retries=3):
    """PC 목록을 페이지 단위로 읽는다 (paging(page) = GET currentPage=N)."""
    events, ids = [], set()
    for page in range(1, MAX_PAGES + 1):
        got = parse_pc_html(fetch_html(f"{PC_LIST_URL}?currentPage={page}", retries=retries))
        fresh = [e for e in got if e["event_url"] not in ids]
        if not fresh:
            break
        ids.update(e["event_url"] for e in fresh)
        events.extend(fresh)
    return events


async def scrape_mobile_list(browser, list_url=None):
    """종전 경로: 모바일 목록을 브라우저로 연다. 반환: (events, 실패 사유 또는 None)."""
    list_url = list_url or LIST_URL
    try:
        page = await load_page(browser, list_url, wait_ms=1500, retries=2, timeout_ms=45000,
                               wait_selector=LIST_SELECTOR,
                               selector_timeout_ms=SELECTOR_TIMEOUT_MS,
                               stay_on_host=SITE_HOST)
    except OffsiteRedirect as e:
        print(f"[{FIRM}] 목록 URL 이 타 도메인으로 이동: {str(e)[:120]}")
        return [], "redirected"
    try:
        events = parse_items(await page.evaluate(JS_LI_ITEMS))
        if events:
            blocked = offsite_attempts(page)
            if blocked:
                print(f"[{FIRM}] 타 도메인 이동 {len(blocked)}회 차단 후 목록 확보: {blocked[0][:100]}")
            return events, None
        reason = await classify_empty(page, SITE_HOST)
        print(f"[{FIRM}] 모바일 목록 0건 — 사유={reason} (url={page.url[:100]}, "
              f"차단한 이동={[u[:80] for u in offsite_attempts(page)][:2]})")
        await debug_dump(page, FIRM)
        return [], reason
    finally:
        await page.close()


async def scrape(browser=None, list_url=None):
    """목록 API → PC 목록 → 모바일 목록(브라우저) 순으로 시도한다.

    앞 경로가 1건이라도 주면 거기서 끝낸다. 전부 실패하면 마지막 경로가 분류한 사유를
    남긴다(collect 가 요약·경보에 싣는다). 일시 장애 재시도는 collect() 의 패스가 담당."""
    for label, fetch in (("목록 API", lambda: parse_api_items(fetch_api())),
                         ("PC 목록", fetch_pc)):
        try:
            events = fetch()
            if events:
                print(f"[{FIRM}] {label}로 {len(events)}건")
                clear_failure(FIRM)
                return events
            print(f"[{FIRM}] {label} 0건 — 다음 경로로")
        except Exception as e:
            print(f"[{FIRM}] {label} 실패: {type(e).__name__}: {str(e)[:120]} — 다음 경로로")
    if browser is None:
        note_failure(FIRM, "error")
        return []
    events, reason = await scrape_mobile_list(browser, list_url)
    if events:
        clear_failure(FIRM)
    else:
        note_failure(FIRM, reason or "selector_zero")
    return events
