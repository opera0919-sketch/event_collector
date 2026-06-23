# -*- coding: utf-8 -*-
"""이미지 배너 이벤트 인식 (Google Gemini 무료 티어).

국내 증권사 이벤트는 대부분 이미지 배너로 게시되어 텍스트 추출이 불가능하다.
배너 이미지를 다운로드해 Gemini 비전으로 참여조건/혜택/대상계좌를 추출한다.

비용:
  - GEMINI_API_KEY(Google AI Studio 무료 키) 없으면 전체 no-op
  - 무료 티어(gemini-2.5-flash): 10 RPM / 1,500 RPD → 주 1회 수십 건이면 충분
  - main 에서 '신규/변경 + 혜택 빈 건'에만 호출 + DB 캐시로 1회만 인식
"""

import base64
import json
import os
import time

import requests

from .config import UA

API_KEY = os.environ.get("GEMINI_API_KEY") or ""
MODEL = os.environ.get("VISION_MODEL", "gemini-2.5-flash")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Gemini responseSchema = OpenAPI 서브셋 (additionalProperties 미지원 → 넣지 않음)
_SCHEMA = {
    "type": "object",
    "properties": {
        "benefits": {"type": "string", "description": (
            "혜택을 '한 줄에 하나씩' 표준 형식으로 정리. 각 줄 형식: '조건 → 리워드 (지급방식·인원)'. "
            "줄 구분은 줄바꿈(\\n). 금액 구간/조건이 여러 개면 모두 빠짐없이 각 줄로 나열(요약·대표값 금지). "
            "리워드는 상품명+금액을 그대로, 지급방식은 (전원)/(선착순 N명)/(추첨 N명)로 표기. "
            "광고 문구·인사말·면책/유의사항은 제외(혜택만). "
            "예:\\n'IRP 순입금 1백만~3백만원 → 신세계 모바일상품권 2만원 (전원)\\n"
            "IRP 순입금 3백만원 이상 → 신세계 모바일상품권 3만원 (전원)'")},
        "conditions": {"type": "string", "description": (
            "참여조건을 '한 줄에 하나씩' 라벨 형식으로 정리(줄바꿈 구분). 해당하는 항목만: "
            "'대상: ...', '기간: ...', '신청: ...'(신청필수/마케팅동의 등), '유지조건: ...', '한도: ...'. "
            "광고 문구·중복 설명 제외, 핵심 요건만 간결히.")},
        "period_start": {"type": "string", "description": (
            "이벤트 시작일을 YYYY-MM-DD로. 자료의 '기간/이벤트기간' 표기 기준(접수/추첨/지급일 아님). "
            "연도가 없으면 빈 문자열, 상시/무기한이면 빈 문자열.")},
        "period_end": {"type": "string", "description": (
            "이벤트 종료일을 YYYY-MM-DD로. '기간' 표기의 마지막 날짜. 상시/무기한이면 빈 문자열.")},
        "acct_pension": {"type": "boolean", "description": "연금저축 계좌 대상이면 true"},
        "acct_irp": {"type": "boolean", "description": "IRP 계좌 대상이면 true"},
        "acct_dc": {"type": "boolean", "description": "DC(확정기여) 퇴직연금 대상이면 true"},
        "acct_etc": {"type": "string", "description": "기타 대상계좌(ISA 등). 없으면 빈 문자열"},
        "is_pension": {"type": "boolean", "description": "연금(연금저축/IRP/DC/퇴직연금) 관련이면 true"},
    },
    "required": ["benefits", "conditions", "period_start", "period_end",
                 "acct_pension", "acct_irp", "acct_dc", "acct_etc", "is_pension"],
}

# 텍스트·이미지 공통 추출 규칙 (형식 통일의 핵심)
_RULES = (
    "연금 이벤트의 참여조건과 혜택, 대상계좌를 표준 형식으로 정확히 추출하라.\n"
    "- 혜택: 표/단계로 제시된 '모든 금액조건과 리워드'를 하나도 빠짐없이, 한 줄에 하나씩 "
    "'조건 → 리워드 (지급방식·인원)' 형식으로. 입금/거래/이전/계좌개설 등 조건별 리워드가 다르면 각 조합을 "
    "모두 별도 줄로(임의 요약·대표값 금지). 지급방식(전원/선착순/추첨)과 인원·한도 수치 포함.\n"
    "- 조건: '대상/기간/신청/유지조건/한도' 라벨로 한 줄씩, 핵심 요건만.\n"
    "- 기간(period_start/period_end): 이벤트 '기간' 표기의 시작·종료일을 YYYY-MM-DD로. "
    "접수일·추첨일·지급일이 아닌 이벤트 진행 기간 기준. 상시/무기한이면 빈 값.\n"
    "- 광고 문구·인사말·면책/유의사항은 제외한다.\n"
    "- 자료에 없는 내용은 추측하지 말고 빈 값으로 둔다."
)
_PROMPT = "이 이미지는 국내 증권사의 이벤트 상세 페이지다. 한국어로 적힌 내용을 읽고 " + _RULES
_PROMPT_TEXT = (
    "아래는 국내 증권사 이벤트 상세 페이지의 본문 텍스트다(네비게이션 등 무관한 내용이 섞여 있을 수 있으니 "
    "이벤트 본문만 사용). " + _RULES)


def enabled() -> bool:
    return bool(API_KEY)


def _media_type(url: str) -> str:
    u = url.lower()
    if ".png" in u:
        return "image/png"
    if ".gif" in u:
        return "image/gif"
    if ".webp" in u:
        return "image/webp"
    return "image/jpeg"


def fetch_image_b64(url: str, referer: str = "") -> tuple:
    """이미지 다운로드 → (base64, media_type). 실패 시 (None, None)."""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        if len(r.content) > 7_000_000:   # 과대 이미지 방지 (~7MB, Gemini inline 한도 고려)
            return None, None
        return base64.standard_b64encode(r.content).decode("ascii"), _media_type(url)
    except Exception as e:
        print(f"[vision] 이미지 다운로드 실패 {url[:80]}: {type(e).__name__}")
        return None, None


def _generate(parts, schema=None, retries=2):
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0}}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema
    url = ENDPOINT.format(model=MODEL)
    headers = {"x-goog-api-key": API_KEY, "content-type": "application/json"}
    for attempt in range(retries + 1):
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
        # 429(RPM 초과)·5xx(일시 과부하/503) → 대기 후 재시도
        if r.status_code in (429, 500, 502, 503, 529) and attempt < retries:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts_out = cands[0].get("content", {}).get("parts", []) or []
        return "".join(p.get("text", "") for p in parts_out)
    return ""


# 일일 쿼터 소진(429) 감지 시 이후 호출을 즉시 건너뛴다(429 재시도 폭주로 런이 길어지는 것 방지).
_BLOCKED = False


def blocked() -> bool:
    return _BLOCKED


def _run(parts, label: str) -> dict:
    global _BLOCKED
    try:
        text = _generate(parts, schema=_SCHEMA)
        parsed = json.loads(text)
        print(f"[vision] {label} 성공 → benefits={str(parsed.get('benefits','')).replace(chr(10),' ')[:40]}")
        return parsed
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            if not _BLOCKED:
                print("[vision] 429 쿼터 소진 감지 → 이후 Gemini 호출 건너뜀(휴리스틱 폴백)")
            _BLOCKED = True
        print(f"[vision] {label} 실패: {type(e).__name__}: {msg[:120]}")
        return {}


def extract(image_url: str, referer: str = "", hint: str = "") -> dict:
    """배너 이미지에서 이벤트 정보를 표준 형식으로 추출. 실패/미설정 시 빈 dict."""
    if not enabled() or _BLOCKED:
        return {}
    b64, media = fetch_image_b64(image_url, referer)
    if not b64:
        return {}
    parts = [
        {"inline_data": {"mime_type": media, "data": b64}},
        {"text": _PROMPT + (f"\n참고(이벤트명): {hint}" if hint else "")},
    ]
    return _run(parts, f"OCR {image_url[:50]}")


def extract_from_text(detail_text: str, hint: str = "") -> dict:
    """상세 페이지 본문 텍스트에서 이벤트 정보를 (이미지와) 동일 표준 형식으로 추출.
    형식 통일의 핵심 — 텍스트 기반 증권사(한투/삼성/KB)도 이미지 기반과 같은 구조로 정규화."""
    if not enabled() or _BLOCKED:
        return {}
    text = (detail_text or "").strip()
    if len(text) < 60:
        return {}
    parts = [{"text": _PROMPT_TEXT + (f"\n참고(이벤트명): {hint}" if hint else "")
              + "\n\n[상세 본문]\n" + text[:8000]}]
    return _run(parts, f"본문구조화 {hint[:24]}")
