# -*- coding: utf-8 -*-
"""NH투자증권 진행중 이벤트 → 목록 JSON(eventList.json) 직접 수집.

실측: 모바일 목록은 eventList.json(XHR) 으로 렌더되며, 응답의
result.content[] 각 항목에 개별 상세의 모든 데이터가 들어 있다.
  mNo, mTitle, mStartDttm(YYYYMMDD), mEndDttm, mContent(상세 본문 HTML),
  mSummary/mAlt(요약), mFile1·2(배너 파일명)
→ Playwright/추가 상세요청 없이 requests 한 번으로 상세 본문까지 확보.
상세 본문(mContent)을 1차 소스로 사용하고, 본문 내 이미지를 OCR 대상으로 둔다.
사용자 노출 링크는 eventView?mNo=… 로 보존.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from ..config import UA

LIST_JSON = "https://m.nhsec.com/customer/event/eventList.json"
EVENT_VIEW = "https://m.nhsec.com/customer/event/eventView?mNo={mno}"
_FILE_BASE = "https://m.nhsec.com/fileUpload/nhmobile/event/{name}"


def _to_iso(d):
    """'20260527' → '2026-05-27'. 형식 불명 시 None."""
    d = (d or "").strip()
    if not re.fullmatch(r"\d{8}", d):
        return None
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _content_image(mcontent, mfiles):
    """상세 본문(mContent) HTML 의 첫 콘텐츠 이미지 → OCR 대상. 없으면 배너 파일."""
    if mcontent:
        soup = BeautifulSoup(mcontent, "html.parser")
        for img in soup.find_all("img"):
            src = (img.get("src") or img.get("data-src") or "").strip()
            if src and not re.search(r"(logo|icon|btn|bullet|sprite|blank|dot|arrow)", src, re.I):
                if src.startswith("//"):
                    src = "https:" + src
                return src if src.startswith("http") else None
    for name in mfiles:
        if name:
            return _FILE_BASE.format(name=name)
    return None


def _fetch_json(retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(LIST_JSON, headers={
                "User-Agent": UA,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://m.nhsec.com/customer/event/eventList",
            }, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 ** attempt)
    raise last


async def scrape(browser=None):
    data = _fetch_json()
    content = (((data or {}).get("result") or {}).get("content")) or []
    events, seen = [], set()
    for it in content:
        if not isinstance(it, dict) or it.get("mDelYn") == "Y":
            continue
        mno = str(it.get("mNo") or "").strip()
        name = (it.get("mTitle") or "").strip()
        if not name or not mno or name in seen:
            continue
        seen.add(name)
        start, end = _to_iso(it.get("mStartDttm")), _to_iso(it.get("mEndDttm"))
        mcontent = it.get("mContent") or ""
        # 상세 본문 HTML → 텍스트 (1차 소스). 이미지 공지면 본문 이미지 OCR.
        detail_text = ""
        if mcontent:
            soup = BeautifulSoup(mcontent, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            detail_text = soup.get_text("\n", strip=True)
        summary = (it.get("mSummary") or it.get("mAlt") or "").strip()
        image_url = _content_image(mcontent, [it.get("mFile2"), it.get("mFile1")])
        events.append({
            "firm_name": "NH투자증권",
            "event_name": name[:120],
            "start_date": start,
            "end_date": end,
            "event_url": EVENT_VIEW.format(mno=mno),
            "raw_text": " ".join((name + " " + summary).split())[:300],
            "_detail_text": detail_text[:8000] if detail_text else "",
            "_image_url": image_url,
            "_benefits_hint": summary[:200] or None,
        })
    return events
