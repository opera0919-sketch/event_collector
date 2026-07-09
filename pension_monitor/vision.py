# -*- coding: utf-8 -*-
"""이벤트 상세(텍스트/이미지)의 Gemini 구조화 추출 — v2.

정확성 원칙:
  - 응답은 자유 문자열이 아니라 **구조화 배열**(혜택 티어/조건 라벨)로 받는다
    → 취약한 문자열 재파싱 제거, DB 자식 테이블(event_benefits/conditions)에 직결.
  - 자료에 없는 내용은 추측하지 않도록 evidence_missing 플래그를 스키마에 강제.
  - 다단 배너 잘림이 OCR 저정확도의 주원인 → 상세 이미지 최대 3장을 한 요청에 전달.
  - 산출물 검증(정크/근거 대조)은 normalize.py 의 게이트가 담당.

비용:
  - GEMINI_API_KEY(Google AI Studio 무료 키) 없으면 전체 no-op
  - 기본 모델 gemini-2.5-flash (VISION_MODEL 로 재정의 가능). 무료 티어 RPM/RPD
    보호는 호출측 예산 상한 + 6.5s 페이싱 + 아래 429 연속 차단이 담당.
"""

import base64
import json
import os
import time

import requests

from .config import UA

API_KEY = os.environ.get("GEMINI_API_KEY") or ""
# 정확성 우선: flash-lite 대비 OCR/추출 품질이 높은 gemini-2.5-flash 를 기본으로.
MODEL = os.environ.get("VISION_MODEL", "gemini-2.5-flash")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MAX_IMAGES = 3  # 한 요청에 전달할 상세 이미지 상한 (다단 배너 커버, 호출 수는 1회)

# 스키마/프롬프트 버전 — 이 값을 올리면 캐시가 무효화돼 전건이 재추출된다
# (normalize.py 캐시 조건이 old.extract_schema_version 와 비교). 스키마·_RULES 를
# 바꿀 때마다 +1. v3: multipliers[]/stackable/annual_claim_limit 신설 + 제외 규칙 정정.
EXTRACT_SCHEMA_VERSION = 3

# Gemini responseSchema = OpenAPI 서브셋 (additionalProperties 미지원 → 넣지 않음)
_SCHEMA = {
    "type": "object",
    "properties": {
        "is_pension": {"type": "boolean",
                       "description": "연금(연금저축/IRP/DC/퇴직연금) 관련 이벤트면 true"},
        "evidence_missing": {"type": "boolean", "description": (
            "자료에 혜택/조건 정보가 실제로 없어 추출이 불가능하면 true. "
            "true 인 경우 benefits/conditions 는 빈 배열로 두고 절대 추측하지 않는다.")},
        "period_start": {"type": "string", "description": (
            "이벤트 시작일 YYYY-MM-DD. '기간/이벤트기간' 표기 기준(접수·추첨·지급일 아님). "
            "자료에 없거나 상시/무기한이면 빈 문자열. 추측 금지.")},
        "period_end": {"type": "string", "description": (
            "이벤트 종료일 YYYY-MM-DD. '기간' 표기의 마지막 날짜. 없으면 빈 문자열. 추측 금지.")},
        "benefits": {"type": "array", "description": (
            "표/단계로 제시된 모든 조건→리워드 조합을 하나도 빠짐없이 각각 한 항목으로 "
            "(임의 요약·대표값 금지). 광고 문구·유의사항 제외."),
            "items": {"type": "object", "properties": {
                "condition": {"type": "string", "description":
                              "충족 조건, 자료 표기 그대로 간결히 (예: 'IRP 순입금 1백만원 이상 ~ 3백만원 미만')"},
                "reward": {"type": "string", "description":
                           "리워드 상품명+금액, 자료 표기 그대로 (예: '신세계 모바일상품권 2만원')"},
                "method": {"type": "string", "enum": ["전원", "선착순", "추첨", "기타"],
                           "description": "지급 방식"},
                "limit_count": {"type": "integer", "description":
                                "선착순/추첨 인원 수. 명시 없으면 0"},
            }, "required": ["condition", "reward", "method", "limit_count"]}},
        "conditions": {"type": "array", "description":
                       "참여조건을 라벨별 한 항목씩. 핵심 요건만, 광고 문구 제외.",
            "items": {"type": "object", "properties": {
                "label": {"type": "string",
                          "enum": ["대상", "기간", "신청", "유지조건", "한도", "기타"]},
                "value": {"type": "string"},
            }, "required": ["label", "value"]}},
        "acct_pension": {"type": "boolean", "description": "연금저축 계좌 대상이면 true"},
        "acct_irp": {"type": "boolean", "description": "IRP 계좌 대상이면 true"},
        "acct_dc": {"type": "boolean", "description": "DC(확정기여) 퇴직연금 대상이면 true"},
        "acct_etc": {"type": "string", "description": "기타 대상계좌(ISA 등). 없으면 빈 문자열"},
        "multipliers": {"type": "array", "description": (
            "순입금/실적 인정 배수(승수) 조항. 'N배 인정', 'N배로 인정', '실적 N배', "
            "'혜택 N배', '지급조건 금액을 N배로' 등. 배수가 없으면 빈 배열. "
            "'Bonus Tip', '유의사항', '※' 하위에 있어도 반드시 추출할 것."),
            "items": {"type": "object", "properties": {
                "source_type": {"type": "string", "enum": [
                    "타사이전", "타사ISA만기전환", "당사ISA만기전환", "퇴직금입금",
                    "개인납입", "비대면최초신규", "기타"],
                    "description": "배수가 적용되는 재원/자격"},
                "multiplier": {"type": "number",
                               "description": "배수 값 (1.5, 2, 3 등)"},
                "scope": {"type": "string", "enum": ["인정금액", "리워드금액"],
                          "description": (
                    "'실적/순입금액을 N배 인정' → 인정금액. "
                    "'혜택을 N배 지급' → 리워드금액. KB 시즌3은 두 종류가 공존함")},
                "min_threshold_krw": {"type": "integer", "description": (
                    "배수 적용 최소금액(원). '순수 이전금액 1천만원 이상 시' → 10000000. "
                    "문턱 없으면 0")},
                "extra_condition": {"type": "string",
                                    "description": "추가 요건 (예: 'WRAP 가입', '보험사 이전'). 없으면 빈 문자열"},
            }, "required": ["source_type", "multiplier", "scope",
                            "min_threshold_krw", "extra_condition"]}},
        "stackable": {"type": "boolean", "description": (
            "이벤트 내 복수 혜택(신규+순입금+순매수 등)을 중복 수령할 수 있으면 true. "
            "'중복 지급되지 않으며 최대 혜택 1회' 류 문구가 있으면 false")},
        "annual_claim_limit": {"type": "integer", "description": (
            "연간 수령 가능 횟수 제한. '연간 총 1회로 제한' → 1. 명시 없으면 0")},
    },
    "required": ["is_pension", "evidence_missing", "period_start", "period_end",
                 "benefits", "conditions",
                 "acct_pension", "acct_irp", "acct_dc", "acct_etc",
                 "multipliers", "stackable", "annual_claim_limit"],
}

_RULES = (
    "국내 증권사 연금 이벤트의 참여조건과 혜택, 대상계좌, 기간을 스키마에 맞춰 정확히 추출하라.\n"
    "- 혜택(benefits): 금액 구간/조건별 리워드가 다르면 각 조합을 모두 별도 항목으로. "
    "요약하거나 대표값만 남기지 말 것. 조건과 리워드는 자료 표기(금액·상품명)를 그대로 옮길 것.\n"
    "- 조건(conditions): 대상/기간/신청(신청필수·마케팅동의 등)/유지조건/한도 라벨로 핵심 요건만.\n"
    "- 기간: 이벤트 '기간' 표기 기준. 접수일·추첨일·지급일을 기간으로 쓰지 말 것.\n"
    "- 자료에 없는 내용은 절대 추측하지 말 것. 정보가 없으면 evidence_missing=true 로 두고 "
    "해당 필드는 빈 값/빈 배열로 남길 것. '정보 없음' 같은 문구를 값으로 넣지 말 것.\n"
    "- 배수(multipliers): 순입금/실적/혜택을 'N배' 인정·지급하는 조항을 하나도 빠짐없이. "
    "실적을 N배 인정 → scope=인정금액, 혜택을 N배 지급 → scope=리워드금액 으로 구분.\n"
    "- 인사말·상품 리스크 고지(원금손실·세법개정 등)·수신거부 안내는 제외.\n"
    "- 다만 아래 항목은 '유의사항'/'Bonus Tip'/'※' 아래에 있어도 반드시 추출한다:\n"
    "  · 배수(승수) 인정 조항 및 그 최소금액 문턱\n"
    "  · 순입금 인정 재원의 포함/제외 (개인납입·타사이전·ISA만기전환·퇴직금·당사내이전·재수관)\n"
    "  · 이벤트 간 중복 지급 가능 여부, 연간 수령 횟수 제한, 개인별 총한도\n"
    "  · 잔고유지 기간(시작~종료일)"
)
_PROMPT_IMG = ("다음 이미지는 한 이벤트의 상세 페이지 배너다(여러 장이면 위→아래 순서로 "
               "이어지는 하나의 페이지). 한국어 내용을 읽고 " + _RULES)
_PROMPT_TEXT = ("아래는 이벤트 상세 페이지의 본문 텍스트다(네비게이션 등 무관한 내용이 "
                "섞여 있을 수 있으니 이벤트 본문만 사용). " + _RULES)


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


def fetch_image_b64(url: str, referer: str = "", retries: int = 3) -> tuple:
    """이미지 다운로드 → (base64, media_type). 실패 시 (None, None)."""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            if len(r.content) > 7_000_000:   # 과대 이미지 방지 (~7MB, inline 한도 고려)
                return None, None
            return base64.standard_b64encode(r.content).decode("ascii"), _media_type(url)
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    print(f"[vision] 이미지 다운로드 실패 {url[:80]}: {type(last).__name__}")
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
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=90)
        # 429(RPM 초과)·5xx(일시 과부하) → 대기 후 재시도
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


# 일일 쿼터 소진(429)이 '연속으로' 확인될 때만 이후 호출을 건너뛴다.
_BLOCKED = False
_consec_429 = 0
_BLOCK_AFTER = 3


def blocked() -> bool:
    return _BLOCKED


def _run(parts, label: str) -> dict:
    global _BLOCKED, _consec_429
    try:
        text = _generate(parts, schema=_SCHEMA)
        parsed = json.loads(text)
        _consec_429 = 0
        n_b = len(parsed.get("benefits") or [])
        n_m = len(parsed.get("multipliers") or [])
        print(f"[vision] {label} 성공 → 혜택 {n_b}행"
              f"{f', 배수 {n_m}건' if n_m else ''}"
              f"{' (evidence_missing)' if parsed.get('evidence_missing') else ''}")
        return parsed
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            _consec_429 += 1
            if _consec_429 >= _BLOCK_AFTER and not _BLOCKED:
                _BLOCKED = True
                print(f"[vision] 429 {_consec_429}회 연속 → 일일 쿼터 소진 판단, 이후 호출 건너뜀")
        print(f"[vision] {label} 실패: {type(e).__name__}: {msg[:120]}")
        return {}


def _image_part(item, referer=""):
    """이미지 항목 → Gemini inline part. 항목은 URL 문자열 또는
    {'b64': <base64>, 'mime': ...}(렌더링 스크린샷 등 URL 재요청이 불가능한 경우)."""
    if isinstance(item, dict) and item.get("b64"):
        return {"inline_data": {"mime_type": item.get("mime", "image/jpeg"),
                                "data": item["b64"]}}
    if isinstance(item, str) and item.startswith("http"):
        b64, media = fetch_image_b64(item, referer)
        if b64:
            return {"inline_data": {"mime_type": media, "data": b64}}
    return None


def extract(image_urls, referer: str = "", hint: str = "") -> dict:
    """상세 이미지(1~MAX_IMAGES장)에서 구조화 추출. 실패/미설정 시 빈 dict.
    다단 배너는 여러 장을 한 요청에 넣어야 잘림 없이 읽힌다."""
    if not enabled() or _BLOCKED:
        return {}
    if isinstance(image_urls, (str, dict)):
        image_urls = [image_urls]
    parts = []
    for item in (image_urls or [])[:MAX_IMAGES]:
        p = _image_part(item, referer)
        if p:
            parts.append(p)
    if not parts:
        return {}
    parts.append({"text": _PROMPT_IMG + (f"\n참고(이벤트명): {hint}" if hint else "")})
    return _run(parts, f"OCR {len(parts) - 1}장 {hint[:24]}")


def extract_from_text(detail_text: str, hint: str = "") -> dict:
    """상세 본문 텍스트에서 (이미지와) 동일 스키마로 구조화 추출."""
    if not enabled() or _BLOCKED:
        return {}
    text = (detail_text or "").strip()
    if len(text) < 60:
        return {}
    # 본문 절단은 호출측(main.clip_detail)이 '본문+유의사항 꼬리' 보존 방식으로
    # 이미 수행한다. 여기서 앞에서부터 재절단하면 꼬리(배수·제외재원)가 소실되므로
    # 이중 절단하지 않는다.
    parts = [{"text": _PROMPT_TEXT + (f"\n참고(이벤트명): {hint}" if hint else "")
              + "\n\n[상세 본문]\n" + text}]
    return _run(parts, f"본문구조화 {hint[:24]}")
