# -*- coding: utf-8 -*-
"""이미지 배너 이벤트 인식 (Claude Vision).

국내 증권사 이벤트는 대부분 이미지 배너로 게시되어 텍스트 추출이 불가능하다.
배너 이미지를 다운로드해 Claude Haiku 4.5 비전으로 참여조건/혜택/대상계좌를 추출한다.

비용 통제:
  - ANTHROPIC_API_KEY 없으면 전체 no-op (파이프라인 안 깨짐)
  - main 에서 '신규/변경 이벤트 + 혜택 비어있음'에만 호출 → 안정 이벤트는 재인식 안 함
  - 가장 저렴한 멀티모달 모델(Haiku 4.5, $1/$5) 사용
"""

import base64
import json
import os

import requests

from .config import UA

API_KEY = os.environ.get("ANTHROPIC_API_KEY") or ""
MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5")

_SCHEMA = {
    "type": "object",
    "properties": {
        "benefits": {"type": "string", "description": "혜택 요약 (금액/상품권/수수료우대 등 핵심만 1~3문장)"},
        "conditions": {"type": "string", "description": "참여조건 (대상고객/순입금/기간 등)"},
        "acct_pension": {"type": "boolean", "description": "연금저축 계좌 대상이면 true"},
        "acct_irp": {"type": "boolean", "description": "IRP 계좌 대상이면 true"},
        "acct_dc": {"type": "boolean", "description": "DC(확정기여) 퇴직연금 대상이면 true"},
        "acct_etc": {"type": "string", "description": "기타 대상계좌(ISA 등) 없으면 빈 문자열"},
        "is_pension": {"type": "boolean", "description": "연금(연금저축/IRP/DC/퇴직연금) 관련 이벤트면 true"},
    },
    "required": ["benefits", "conditions", "acct_pension", "acct_irp", "acct_dc", "acct_etc", "is_pension"],
    "additionalProperties": False,
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
        if len(r.content) > 4_500_000:   # 과대 이미지 방지 (~4.5MB)
            return None, None
        return base64.standard_b64encode(r.content).decode("ascii"), _media_type(url)
    except Exception as e:
        print(f"[vision] 이미지 다운로드 실패 {url[:80]}: {type(e).__name__}")
        return None, None


def extract(image_url: str, referer: str = "", hint: str = "") -> dict:
    """배너 이미지에서 이벤트 정보 추출. 실패/미설정 시 빈 dict."""
    if not enabled():
        return {}
    b64, media = fetch_image_b64(image_url, referer)
    if not b64:
        return {}
    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": _PROMPT + (f"\n참고(이벤트명): {hint}" if hint else "")},
            ],
        }],
    }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            data=json.dumps(body), timeout=60)
        r.raise_for_status()
        data = r.json()
        text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "")
        parsed = json.loads(text)
        print(f"[vision] OCR 성공: {image_url[:60]} → benefits={parsed.get('benefits','')[:40]}")
        return parsed
    except Exception as e:
        print(f"[vision] OCR 실패 {image_url[:60]}: {type(e).__name__}: {str(e)[:120]}")
        return {}
