# -*- coding: utf-8 -*-
"""WAF(EverSafe)로 직접 수집이 막힌 증권사용 웹검색 폴백.

키움증권 이벤트 페이지는 EverSafe 안티봇 WAF가 데이터센터 IP를 차단한다
(probe: {"eversafeThreat":true} → /e/common/error). GitHub Actions(미국 IP)에서
직접 수집이 구조적으로 불가하므로, Claude API의 서버측 web_search 도구로
공식 도메인(kiwoom.com)에 한정해 진행중 연금 이벤트를 가져온다.
검색은 Anthropic 인프라에서 실행되어 차단 IP를 거치지 않는다.

ANTHROPIC_API_KEY 없으면 no-op.
"""

import json
import os

import requests

API_KEY = os.environ.get("ANTHROPIC_API_KEY") or ""
MODEL = os.environ.get("WEBSEARCH_MODEL", "claude-haiku-4-5")

_PROMPT = """키움증권(kiwoom.com) 공식 홈페이지에 현재 진행중인 '연금' 관련 이벤트
(연금저축/IRP/DC/퇴직연금)를 웹검색으로 찾아라. 오늘 날짜 기준 진행중인 것만.
각 이벤트를 아래 JSON 배열로만 출력(설명 금지):
[{"event_name":"이벤트명","start_date":"YYYY-MM-DD 또는 null","end_date":"YYYY-MM-DD 또는 null",
  "acct_pension":true/false,"acct_irp":true/false,"acct_dc":true/false,
  "acct_etc":"ISA 등 또는 빈문자열","benefits":"혜택 요약","conditions":"참여조건","event_url":"공식 URL"}]
없으면 빈 배열 []. 추측 금지."""

_TOOLS = [{
    "type": "web_search_20260209",
    "name": "web_search",
    "allowed_domains": ["kiwoom.com", "www1.kiwoom.com", "www.kiwoom.com", "kiwoomam.com"],
    "max_uses": 5,
}]


def enabled() -> bool:
    return bool(API_KEY)


def _post(messages):
    body = {"model": MODEL, "max_tokens": 2048, "tools": _TOOLS, "messages": messages}
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json",
                 "anthropic-beta": "web-search-2025-03-05"},
        data=json.dumps(body), timeout=120)
    r.raise_for_status()
    return r.json()


def fetch_kiwoom_pension(firm="키움증권"):
    """키움 연금 이벤트 목록을 web_search로 수집. 실패/미설정 시 []."""
    if not enabled():
        return []
    messages = [{"role": "user", "content": _PROMPT}]
    try:
        data = _post(messages)
        # 서버측 검색 루프가 pause_turn 으로 멈추면 이어서 재요청 (최대 4회)
        for _ in range(4):
            if data.get("stop_reason") != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": data["content"]}]
            data = _post(messages)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < 0:
            print("[websearch] 키움 JSON 미발견")
            return []
        rows = json.loads(text[start:end + 1])
    except Exception as e:
        print(f"[websearch] 키움 수집 실패: {type(e).__name__}: {str(e)[:140]}")
        return []

    events = []
    for r in rows:
        name = (r.get("event_name") or "").strip()
        if not name:
            continue
        events.append({
            "firm_name": firm,
            "event_name": name[:120],
            "start_date": r.get("start_date") if r.get("start_date") not in ("", "null") else None,
            "end_date": r.get("end_date") if r.get("end_date") not in ("", "null") else None,
            "acct_pension": bool(r.get("acct_pension")),
            "acct_irp": bool(r.get("acct_irp")),
            "acct_dc": bool(r.get("acct_dc")),
            "acct_etc": (r.get("acct_etc") or None) or None,
            "benefits": (r.get("benefits") or None),
            "conditions": (r.get("conditions") or None),
            "event_url": r.get("event_url") or "https://www1.kiwoom.com/h/customer/event/VIngEventView",
            "raw_text": name,
            "remarks": "web_search 수집(공식 도메인 한정) — EverSafe 직접차단 대응",
            "_via_search": True,
        })
    print(f"[websearch] 키움 {len(events)}건 수집")
    return events
