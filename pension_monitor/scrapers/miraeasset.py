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

실패 모드 (운영 2026-10~11 로그):
  (a) 껍데기만 렌더 — 본문 37자("닫기 홈 미래에셋증권 …"), 목록 li 미주입. 종전의
      고정 9초 대기 안에 목록이 안 들어온 경우 → li 셀렉터를 최대 30초 기다린다.
  (b) 앱 설치 페이지로 이동 — 본문 전체가 Google Play 의 MAPS 앱 소개(3,800자).
      → 메인 프레임의 타 도메인 이동을 막아 원래 문서를 유지하고, 그래도 0건이면
        모바일 UA 로 한 번 더 연다. 어느 쪽이든 사유를 분류해 남긴다.
"""

import re

from .base import (load_page, debug_dump, classify_empty, note_failure, clear_failure,
                   offsite_attempts, OffsiteRedirect, MOBILE_UA)
from .static_generic import PERIOD_RE, parse_period

FIRM = "미래에셋증권"
SITE_HOST = "miraeasset.com"
LIST_SELECTOR = 'li[onclick*="detailView"]'
SELECTOR_TIMEOUT_MS = 30000     # 목록 li 가 나타나기를 기다리는 상한 (종전: 고정 9초)

BASE = "https://securities.miraeasset.com"
LIST_URL = f"{BASE}/mw/mki/mki7000/r01.do"
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


async def _attempt(browser, list_url, user_agent=None):
    """목록 1회 시도. 반환: (events, 실패 사유 또는 None)."""
    try:
        page = await load_page(browser, list_url, wait_ms=1500, retries=2, timeout_ms=45000,
                               wait_selector=LIST_SELECTOR,
                               selector_timeout_ms=SELECTOR_TIMEOUT_MS,
                               stay_on_host=SITE_HOST, user_agent=user_agent)
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
        print(f"[{FIRM}] 목록 0건 — 사유={reason} (url={page.url[:100]}, "
              f"차단한 이동={[u[:80] for u in offsite_attempts(page)][:2]})")
        await debug_dump(page, FIRM)
        return [], reason
    finally:
        await page.close()


async def scrape(browser, list_url=None):
    # 일시 장애 대응은 collect() 의 재시도 패스가 담당 → 여기선 패스당 2가지 방식만 시도.
    list_url = list_url or LIST_URL
    events, reason = await _attempt(browser, list_url)
    if not events and reason == "redirected":
        events, _ = await _attempt(browser, list_url, user_agent=MOBILE_UA)
        reason = reason if not events else None
        if events:
            print(f"[{FIRM}] 모바일 UA 재시도로 목록 확보")
    if events:
        clear_failure(FIRM)
    else:
        note_failure(FIRM, reason or "selector_zero")
    return events
