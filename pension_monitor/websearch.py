# -*- coding: utf-8 -*-
"""WAF(EverSafe)로 직접 수집이 막힌 증권사용 웹검색 폴백 (Gemini 무료 티어).

키움증권 이벤트 페이지는 EverSafe 안티봇 WAF가 데이터센터 IP를 차단한다
(probe: {"eversafeThreat":true} → /e/common/error). GitHub Actions(미국 IP)에서
직접 수집이 구조적으로 불가하므로, Gemini의 Google Search 그라운딩으로
공식 도메인(kiwoom.com)에 한정해 진행중 연금 이벤트를 가져온다.
검색은 Google 인프라에서 실행되어 차단 IP를 거치지 않는다.

GEMINI_API_KEY 없으면 no-op.
"""

import json
import os

import requests

API_KEY = os.environ.get("GEMINI_API_KEY") or ""
MODEL = os.environ.get("WEBSEARCH_MODEL", "gemini-2.5-flash")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_PROMPT = """키움증권(공식 홈페이지 kiwoom.com)에 오늘 날짜 기준 현재 진행중인 '연금' 관련 이벤트
(연금저축/IRP/DC/퇴직연금)를 Google 검색으로 찾아라. 반드시 kiwoom.com 공식 페이지 정보만 사용.
결과를 아래 JSON 배열로만 출력(코드블록/설명 없이 JSON만):
[{"event_name":"이벤트명","start_date":"YYYY-MM-DD 또는 null","end_date":"YYYY-MM-DD 또는 null",
  "acct_pension":true/false,"acct_irp":true/false,"acct_dc":true/false,
  "acct_etc":"ISA 등 또는 빈문자열","benefits":"혜택 요약","conditions":"참여조건",
  "event_url":"kiwoom.com 공식 URL"}]
진행중인 연금 이벤트가 없으면 빈 배열 []. 추측 금지."""


def enabled() -> bool:
    return bool(API_KEY)


def _search(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0},
    }
    url = ENDPOINT.format(model=MODEL)
    r = requests.post(url, headers={"x-goog-api-key": API_KEY, "content-type": "application/json"},
                      data=json.dumps(body), timeout=120)
    r.raise_for_status()
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = cands[0].get("content", {}).get("parts", []) or []
    return "".join(p.get("text", "") for p in parts)


def fetch_kiwoom_pension(firm="키움증권"):
    """키움 연금 이벤트 목록을 Google Search 그라운딩으로 수집. 실패/미설정 시 []."""
    if not enabled():
        return []
    try:
        text = _search(_PROMPT)
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < 0:
            print(f"[websearch] 키움 JSON 미발견. 응답앞부분: {text[:200]!r}")
            return []
        rows = json.loads(text[start:end + 1])
        if not rows:
            print(f"[websearch] 키움 빈 배열. 응답앞부분: {text[:200]!r}")
    except Exception as e:
        print(f"[websearch] 키움 수집 실패: {type(e).__name__}: {str(e)[:140]}")
        return []

    events = []
    for r in rows:
        name = (r.get("event_name") or "").strip()
        if not name:
            continue
        url = r.get("event_url") or "https://www1.kiwoom.com/h/customer/event/VIngEventView"
        if "kiwoom" not in url:   # 공식 도메인 검증
            url = "https://www1.kiwoom.com/h/customer/event/VIngEventView"
        events.append({
            "firm_name": firm,
            "event_name": name[:120],
            "start_date": r.get("start_date") if r.get("start_date") not in ("", "null", None) else None,
            "end_date": r.get("end_date") if r.get("end_date") not in ("", "null", None) else None,
            "acct_pension": bool(r.get("acct_pension")),
            "acct_irp": bool(r.get("acct_irp")),
            "acct_dc": bool(r.get("acct_dc")),
            "acct_etc": (r.get("acct_etc") or None) or None,
            "benefits": (r.get("benefits") or None),
            "conditions": (r.get("conditions") or None),
            "event_url": url,
            "raw_text": name,
            "remarks": "Google 검색 수집(공식 도메인 한정) — EverSafe 직접차단 대응",
            "_via_search": True,
        })
    print(f"[websearch] 키움 {len(events)}건 수집")
    return events
