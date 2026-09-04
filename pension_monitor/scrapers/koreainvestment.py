# -*- coding: utf-8 -*-
"""한국투자증권: 서버렌더링 JSP → requests + BeautifulSoup.

목록 구조 (실측): <a class="event_thum_box" href="javascript:doView('6711')">
  텍스트: "{이벤트명} 진행중 {부제} 기간 : 2026.06.09 ~ 2026.07.31"
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from ..config import UA

# 간헐적 연결 거부(실측) → 도메인 폴백
_DOMAINS = ["securities.koreainvestment.com", "m.koreainvestment.com"]
LIST_URL = ("https://{domain}/main/customer/notice/Event.jsp"
            "?gubun=i&currentPage={page}&userRowsPerPage=10")
DETAIL_URL = ("https://{domain}/main/customer/notice/Event.jsp"
              "?gubun=i&cmd=TF04gb010002&num={num}")

_PERIOD_RE = re.compile(r"기간\s*:\s*(\d{4}\.\d{1,2}\.\d{1,2})\s*~\s*(\d{4}\.\d{1,2}\.\d{1,2})")
_VIEW_RE = re.compile(r"doView\('(\d+)'\)")


def _get(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            last = e
            time.sleep(2 ** attempt)
    raise last


def _get_list(page_no):
    last = None
    for domain in _DOMAINS:
        try:
            return _get(LIST_URL.format(domain=domain, page=page_no))
        except Exception as e:
            last = e
    raise last


def _to_iso(d):
    y, m, dd = d.split(".")
    return f"{y}-{int(m):02d}-{int(dd):02d}"


def fetch_detail_text(num: str) -> str:
    """상세 본문 텍스트. 목록과 같은 도메인 폴백을 적용하고, 전부 실패하면 raise.

    종전엔 실패를 "" 로 삼켜 호출측이 '빈 본문'을 정상 내용으로 오인했다 —
    빈 본문 해시(sha256('|')=cbe5cfdf…)가 재추출 트리거를 매일 뒤집던 원인(S1)."""
    last = None
    for domain in _DOMAINS:
        try:
            soup = BeautifulSoup(_get(DETAIL_URL.format(domain=domain, num=num)), "html.parser")
            return soup.get_text("\n", strip=True)
        except Exception as e:
            last = e
    raise last


async def scrape(browser=None):
    events = []
    for page_no in range(1, 5):
        soup = BeautifulSoup(_get_list(page_no), "html.parser")
        boxes = soup.select("a.event_thum_box")
        if not boxes:
            break
        for a in boxes:
            text = " ".join(a.get_text(" ", strip=True).split())
            m = _PERIOD_RE.search(text)
            if not m:
                continue
            name = text.split("진행중")[0].strip() or text[:60]
            # 목록의 부제(진행중 ~ 기간 사이)가 혜택 요약인 경우가 많음
            sub = text[text.find("진행중") + 3: m.start()].strip(" :") if "진행중" in text else ""
            sub = sub.replace("기간", "").strip(" :")
            vm = _VIEW_RE.search(a.get("href", "") or "")
            num = vm.group(1) if vm else None
            events.append({
                "firm_name": "한국투자증권",
                "event_name": name[:120],
                "start_date": _to_iso(m.group(1)),
                "end_date": _to_iso(m.group(2)),
                "event_url": (DETAIL_URL.format(domain=_DOMAINS[0], num=num) if num
                              else LIST_URL.format(domain=_DOMAINS[0], page=1)),
                "raw_text": text,
                "_detail_id": num,
                "_benefits_hint": sub[:200] if sub else None,
            })
        if len(boxes) < 10:
            break
    return events
