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
"""

import re

from .base import load_page, debug_dump
from .static_generic import PERIOD_RE, parse_period

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


async def scrape(browser):
    # 일시 타임아웃 대응은 collect() 의 재시도 패스가 담당 → 여기선 패스당 시간을 적당히 제한.
    page = await load_page(browser, LIST_URL, wait_ms=9000, retries=3, timeout_ms=45000)
    try:
        items = await page.evaluate(JS_LI_ITEMS)
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
        if not events:
            await debug_dump(page, "미래에셋증권")
        return events
    finally:
        await page.close()
