# -*- coding: utf-8 -*-
"""사실 시그니처(fact signature) — LLM 표현 요동과 '실질 변경'을 구분한다 (S3).

같은 원문을 재추출하면 Gemini 는 문장을 매번 조금씩 다르게 쓴다
("및→후", 어순, '상품권' 접두 탈락, 티어 순서 등). 운영 관측: KB 「TDF&ETF 시즌3」
conditions 는 34회 변경에 33개의 서로 다른 값. 이 모듈은 문장에서 **경제적 실체**
(금액·문턱·인원·%·배수·날짜·판정 플래그)만 뽑아 비교 가능한 키로 만든다.
시그니처가 같으면 db.sync 는 기존 캐노니컬 텍스트·자식 행을 유지한다.

원칙: 순수 함수, 외부 의존 없음. 같은 사실 → 같은 키(표기 무관), 다른 사실 → 다른 키.
'같다'를 놓치면(false different) 오늘처럼 덮어쓸 뿐이고, '다르다'를 놓치면(false
same) 실제 변경을 잃으므로, 토큰화는 보수적으로(정보를 버리지 않는 쪽으로) 한다.
"""

import re

# 날짜 표기 통일: 2026.10.1 / 26.10.01 / 2026년 6월 30일 / 2026-06-30 → D:2026-10-01
_DATE_RE = re.compile(r"(\d{2,4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")
# 한글 단위 금액: '1백만원', '3천만원', '1억 5천만원', '30,000원', '2만원', '0.0042087%'
_UNITS = {"억": 10 ** 8, "천만": 10 ** 7, "백만": 10 ** 6, "십만": 10 ** 5,
          "만": 10 ** 4, "천": 10 ** 3, "백": 10 ** 2}
_PART_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(억|천만|백만|십만|만|천|백)?")
_AMOUNT_RE = re.compile(
    r"((?:\d[\d,]*(?:\.\d+)?\s*(?:억|천만|백만|십만|만|천|백)?\s*)+)"
    r"(원|%|명|회|배|주|건|매|잔|개|차|일|개월|년)?")
_METHOD_RE = re.compile(r"(전원|선착순|추첨)")


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else ("%.10g" % v)


def _date_sub(m):
    y = int(m.group(1))
    y = 2000 + y if y < 100 else y
    return f" D:{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d} "


def num_tokens(text) -> list:
    """텍스트의 수치 토큰 목록(정규화). 예:
    '1백만원 이상 ~ 3백만원 미만 → 신세계 상품권 2만원 (추첨 3,000명)'
      → ['1000000원', '3000000원', '20000원', '3000명']
    '기간 2026.10.1~10.31'  → ['D:2026-10-01', '10.31']  (남는 숫자도 버리지 않음)"""
    t = _DATE_RE.sub(_date_sub, text or "")
    out = re.findall(r"D:\d{4}-\d{2}-\d{2}", t)
    t = re.sub(r"D:\d{4}-\d{2}-\d{2}", " ", t)
    for m in _AMOUNT_RE.finditer(t):
        body, suffix = m.group(1), m.group(2) or ""
        parts = _PART_RE.findall(body)
        if not parts:
            continue
        if any(unit for _, unit in parts):
            val = 0.0
            for num, unit in parts:
                val += float(num.replace(",", "")) * _UNITS.get(unit, 1)
            out.append(_fmt(val) + suffix)
        else:
            # 단위 없는 숫자 나열('1 2 3')은 각각 토큰
            for num, _ in parts:
                out.append(_fmt(float(num.replace(",", ""))) + suffix)
    return out


def _lines(text) -> list:
    out = []
    for chunk in (text or "").split("\n"):
        out.extend(p.strip() for p in chunk.split(" | ") if p.strip())
    return out


def _tier_sig(line: str) -> tuple:
    m = _METHOD_RE.search(line)
    return (tuple(sorted(num_tokens(line))), m.group(1) if m else None)


def fact_signature(row: dict, multipliers=None) -> tuple:
    """이벤트 행(마스터 컬럼 dict)의 사실 시그니처.

    구성: 혜택 티어 멀티셋(티어별 수치 토큰 + 지급방식) · 조건의 수치 토큰 멀티셋 ·
    파서 유래 판정(신청필수/마케팅동의/연간한도) · 배수 행 집합.
    stackable/annual_claim_limit 은 LLM 불리언이라 실행 간 흔들려 제외한다(게이트가
    '동일'로 판정하면 기존 값을 그대로 유지하므로 데이터가 손실되진 않는다)."""
    tiers = tuple(sorted(_tier_sig(l) for l in _lines(row.get("benefits"))))
    conds = tuple(sorted(num_tokens(" ".join(_lines(row.get("conditions"))))))
    flags = (row.get("apply_required"), row.get("marketing_consent_required"),
             row.get("annual_cap_krw"))
    mults = tuple(sorted(
        (str(m.get("source_type") or ""), _fmt(float(m.get("multiplier") or 0)),
         str(m.get("scope") or ""), int(m.get("min_threshold_krw") or 0))
        for m in (multipliers or [])))
    return (tiers, conds, flags, mults)


def tier_count(row: dict) -> int:
    return len(_lines(row.get("benefits")))
