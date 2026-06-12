# -*- coding: utf-8 -*-
"""연금 이벤트 판별 및 대상계좌/필드 추출 휴리스틱."""

import hashlib
import re

from .config import (
    PENSION_KEYWORDS, ACCT_PENSION_KW, ACCT_IRP_KW, ACCT_DC_KW, ACCT_ETC_KW,
)


def is_pension(text: str) -> bool:
    t = text or ""
    return any(kw in t for kw in PENSION_KEYWORDS)


def detect_accounts(text: str) -> dict:
    """대상계좌 4개 열 판별. 명시 키워드가 없으면 모두 False/None → 비고에서 안내."""
    t = text or ""
    acct = {
        "acct_pension": any(kw in t for kw in ACCT_PENSION_KW),
        "acct_irp": any(kw in t for kw in ACCT_IRP_KW),
        "acct_dc": any(kw in t for kw in ACCT_DC_KW)
                   and "DC형" in t or "확정기여" in t,  # 'DC' 단독은 오탐 잦아 보수적으로
        "acct_etc": None,
    }
    etc = [kw for kw in ACCT_ETC_KW if kw in t]
    if etc:
        acct["acct_etc"] = ", ".join(sorted(set(k.upper() for k in etc)))
    # '연금' 만 있고 구체 계좌 미상 → 연금저축+IRP 통칭으로 보는 경우가 많아 둘 다 표기
    if not any([acct["acct_pension"], acct["acct_irp"], acct["acct_dc"]]) and "연금" in t:
        acct["acct_pension"] = True
        acct["acct_irp"] = True
    return acct


_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def parse_dates(text: str):
    """텍스트에서 (시작일, 종료일) 추출. YYYY-MM-DD 문자열 또는 None."""
    found = _DATE_RE.findall(text or "")
    dates = [f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in found]
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        # 단일 날짜는 종료일(마감)로 보는 게 안전
        return None, dates[0]
    return None, None


def extract_details(detail_text: str) -> dict:
    """상세 페이지 본문에서 참여조건/혜택 휴리스틱 추출."""
    out = {"conditions": None, "benefits": None, "remarks": None}
    if not detail_text:
        return out
    text = " ".join(detail_text.split())
    if len(text) < 80:
        out["remarks"] = "상세 이미지 공지 (텍스트 추출 불가)"
        return out

    lines = [l.strip() for l in detail_text.splitlines() if l.strip()]
    cond, bene = [], []
    for i, line in enumerate(lines):
        compact = line.replace(" ", "")
        if any(k in compact for k in ("참여대상", "이벤트대상", "참여조건", "응모대상", "대상고객", "참여방법")):
            cond.append(" / ".join(lines[i:i + 2])[:200])
        if any(k in compact for k in ("혜택", "지급", "경품", "리워드", "상품권", "수수료우대", "캐시백")):
            bene.append(line[:200])
    if cond:
        out["conditions"] = " | ".join(dict.fromkeys(cond))[:500]
    if bene:
        out["benefits"] = " | ".join(dict.fromkeys(bene))[:500]
    if not cond and not bene:
        out["remarks"] = "상세 자동추출 실패 — 원문 확인 필요"
    return out


def content_hash(ev: dict) -> str:
    basis = "|".join(str(ev.get(k) or "") for k in (
        "firm_name", "event_name", "start_date", "end_date",
        "conditions", "benefits", "acct_etc",
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
