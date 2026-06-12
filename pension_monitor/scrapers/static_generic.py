# -*- coding: utf-8 -*-
"""서버렌더링 페이지용 범용 파서: requests + BeautifulSoup 로
기간 패턴(YYYY.MM.DD ~ YYYY.MM.DD)을 포함한 가장 안쪽 요소를 이벤트로 추출."""

import re
import time

import requests
from bs4 import BeautifulSoup

from ..config import UA

PERIOD_RE = re.compile(
    r"(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})\s*~\s*(\d{4})[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})")
_NOISE = ("당첨자", "유효기간", "인증범위", "지난 이벤트", "참여한 이벤트", "Copyright")


def fetch_html(url, retries=4):
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


def parse_period(m):
    return (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
            f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}")


def parse_generic(html, firm, list_url):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    events, seen = [], set()
    scan_tags = ["li", "tr", "dl", "div", "p", "a", "span", "em", "strong", "dt", "dd"]
    for el in soup.find_all(scan_tags):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text or len(text) > 400:
            continue
        m = PERIOD_RE.search(text)
        if not m or any(n in text for n in _NOISE):
            continue
        # 동일 기간 패턴을 가진 자식이 있으면 부모는 스킵 (가장 안쪽 우선)
        inner = False
        for c in el.find_all(scan_tags):
            ct = " ".join(c.get_text(" ", strip=True).split())
            if ct and PERIOD_RE.search(ct) and len(ct) >= len(text) * 0.8:
                inner = True
                break
        if inner:
            continue
        name = text[: m.start()].strip(" :~-·.[]")
        name = re.sub(r"(이벤트\s?기간|기간|D-\d+|오늘마감)$", "", name).strip(" :")
        if not name or len(name) < 4 or name in seen:
            continue
        seen.add(name)
        a = el if el.name == "a" else (el.find("a", href=True) or el.find_parent("a", href=True))
        href = a.get("href") if a else None
        if href and not href.startswith("http"):
            href = None
        start, end = parse_period(m)
        events.append({
            "firm_name": firm,
            "event_name": name[:120],
            "start_date": start,
            "end_date": end,
            "event_url": href or list_url,
            "raw_text": text,
            "_onclick": (a.get("onclick") or a.get("href") or "") if a else "",
        })
    return events


def debug_static(html, firm, max_items=12):
    """0건일 때: 원본 HTML 에서 기간 패턴 주변 문맥을 로그로 출력."""
    plain = re.sub(r"<[^>]+>", " ", html)
    plain = " ".join(plain.split())
    hits = 0
    for m in PERIOD_RE.finditer(plain):
        s = max(0, m.start() - 100)
        print(f"[debug-static:{firm}] …{plain[s:m.end() + 30]}…")
        hits += 1
        if hits >= max_items:
            break
    if hits == 0:
        print(f"[debug-static:{firm}] 기간 패턴 없음 (html {len(html)}자)")
