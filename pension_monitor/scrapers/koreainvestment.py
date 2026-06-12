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

LIST_URL = ("https://securities.koreainvestment.com/main/customer/notice/Event.jsp"
            "?gubun=i&currentPage={page}&userRowsPerPage=10")
DETAIL_URL = ("https://securities.koreainvestment.com/main/customer/notice/Event.jsp"
              "?gubun=i&cmd=TF04gb010002&num={num}")

_PERIOD_RE = re.compile(r"기간\s*:\s*(\d{4}\.\d{1,2}\.\d{1,2})\s*~\s*(\d{4}\.\d{1,2}\.\d{1,2})")
_VIEW_RE = re.compile(r"doView\('(\d+)'\)")


def _get(url, retries=5):
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


def _to_iso(d):
    y, m, dd = d.split(".")
    return f"{y}-{int(m):02d}-{int(dd):02d}"


def fetch_detail_text(num: str) -> str:
    try:
        soup = BeautifulSoup(_get(DETAIL_URL.format(num=num)), "html.parser")
        return soup.get_text("\n", strip=True)
    except Exception:
        return ""


async def scrape(browser=None):
    events = []
    for page_no in range(1, 5):
        soup = BeautifulSoup(_get(LIST_URL.format(page=page_no)), "html.parser")
        boxes = soup.select("a.event_thum_box")
        if not boxes:
            break
        for a in boxes:
            text = " ".join(a.get_text(" ", strip=True).split())
            m = _PERIOD_RE.search(text)
            if not m:
                continue
            name = text.split("진행중")[0].strip() or text[:60]
            vm = _VIEW_RE.search(a.get("href", "") or "")
            num = vm.group(1) if vm else None
            events.append({
                "firm_name": "한국투자증권",
                "event_name": name[:120],
                "start_date": _to_iso(m.group(1)),
                "end_date": _to_iso(m.group(2)),
                "event_url": DETAIL_URL.format(num=num) if num else LIST_URL.format(page=1),
                "raw_text": text,
                "_detail_id": num,
            })
        if len(boxes) < 10:
            break
    return events
