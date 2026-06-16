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
        "benefits": {"type": "string", "description": "혜택 요약 (금액/상품권/수수료우대 등 핵심만 1~3문장)"},
        "conditions": {"type": "string", "description": "참여조건 (대상고객/순입금/기간 등)"},
        "acct_pension": {"type": "boolean", "description": "연금저축 계좌 대상이면 true"},
        "acct_irp": {"type": "boolean", "description": "IRP 계좌 대상이면 true"},
        "acct_dc": {"type": "boolean", "description": "DC(확정기여) 퇴직연금 대상이면 true"},
        "acct_etc": {"type": "string", "description": "기타 대상계좌(ISA 등). 없으면 빈 문자열"},
        "is_pension": {"type": "boolean", "description": "연금(연금저축/IRP/DC/퇴직연금) 관련이면 true"},
    },
    "required": ["benefits", "conditions", "acct_pension", "acct_irp", "acct_dc", "acct_etc", "is_pension"],
}

_PROMPT = (
    "이 이미지는 국내 증권사의 이벤트 배너다. 한국어로 적힌 내용을 읽고 "
    "연금 이벤트의 참여조건과 혜택, 대상계좌를 정확히 추출하라. "
    "이미지에 없는 내용은 추측하지 말고 빈 값으로 둔다."
)


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
        if r.status_code == 429 and attempt < retries:   # 무료 티어 RPM 초과 → 대기 후 재시도
            time.sleep(20)
            continue
        r.raise_for_status()
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts_out = cands[0].get("content", {}).get("parts", []) or []
        return "".join(p.get("text", "") for p in parts_out)
    return ""


def extract(image_url: str, referer: str = "", hint: str = "") -> dict:
    """배너 이미지에서 이벤트 정보 추출. 실패/미설정 시 빈 dict."""
    if not enabled():
        return {}
    b64, media = fetch_image_b64(image_url, referer)
    if not b64:
        return {}
    parts = [
        {"inline_data": {"mime_type": media, "data": b64}},
        {"text": _PROMPT + (f"\n참고(이벤트명): {hint}" if hint else "")},
    ]
    try:
        text = _generate(parts, schema=_SCHEMA)
        parsed = json.loads(text)
        print(f"[vision] OCR 성공: {image_url[:60]} → benefits={str(parsed.get('benefits',''))[:40]}")
        return parsed
    except Exception as e:
        print(f"[vision] OCR 실패 {image_url[:60]}: {type(e).__name__}: {str(e)[:120]}")
        return {}
