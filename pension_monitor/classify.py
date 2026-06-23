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


_DC_RE = re.compile(r"DC형|확정기여|(?<![A-Za-z])DC(?![A-Za-z])")


def detect_accounts(text: str) -> dict:
    """대상계좌 4개 열 판별. 명시 키워드 우선, 없으면 통칭 규칙 적용."""
    t = text or ""
    acct = {
        "acct_pension": any(kw in t for kw in ACCT_PENSION_KW) or "개인연금" in t,
        "acct_irp": any(kw in t for kw in ACCT_IRP_KW),
        "acct_dc": bool(_DC_RE.search(t)),
        "acct_etc": None,
    }
    etc = [kw for kw in ACCT_ETC_KW if kw in t]
    if etc:
        acct["acct_etc"] = ", ".join(sorted(set(k.upper() for k in etc)))
    # 구체 계좌 미상 시 통칭 해석: 퇴직연금→IRP/DC, 그 외 '연금'→연금저축+IRP
    if not any([acct["acct_pension"], acct["acct_irp"], acct["acct_dc"]]):
        if "퇴직연금" in t:
            acct["acct_irp"] = True
            acct["acct_dc"] = True
        elif "연금" in t:
            acct["acct_pension"] = True
            acct["acct_irp"] = True
    return acct


_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
# '기간' 키워드 근처의 'YYYY..MM..DD ~ YYYY..MM..DD' (한글/숫자 날짜 모두).
# 유지/접수/추첨/지급/잔고/입금 '…기간'은 이벤트 기간이 아니므로 제외(부정 룩비하인드).
_PERIOD_NEAR = re.compile(
    r"(?<!유지)(?<!접수)(?<!추첨)(?<!지급)(?<!잔고)(?<!입금)(?<!인정)기간[^0-9]{0,12}"
    r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})"
    r"[^0-9~∼-]{0,12}[~∼-][^0-9]{0,12}"
    r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def _mk(y, m, d):
    try:
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            return f"{int(y)}-{int(m):02d}-{int(d):02d}"
    except (TypeError, ValueError):
        pass
    return None


def extract_period(text: str):
    """상세 본문에서 '기간 : YYYY..~..YYYY..' 형태의 이벤트 기간을 우선 추출.
    KB처럼 목록의 게시일이 시작/종료일로 잘못 들어가는 것을 본문 기준으로 교정하기 위함.
    반환: (시작ISO, 종료ISO) 또는 (None, None)."""
    t = " ".join((text or "").split())
    m = _PERIOD_NEAR.search(t)
    if m:
        s = _mk(*m.group(1, 2, 3))
        e = _mk(*m.group(4, 5, 6))
        if s and e and s <= e:
            return s, e
    return None, None


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


def suspicious_dates(start, end) -> bool:
    """스크레이퍼가 준 기간이 의심스러운지(게시일 오인 등) 판정."""
    import datetime as _dt
    def iso(s):
        try:
            return _dt.date.fromisoformat(s)
        except (TypeError, ValueError):
            return None
    sd, ed = iso(start), iso(end)
    if not start or not end:
        return True                       # 한쪽 누락
    if sd and ed and (sd > ed or (ed - sd).days <= 1):
        return True                       # 역전 또는 1일 이하(게시일 오인 의심)
    for x in (sd, ed):
        if x and not (2025 <= x.year <= 2027):
            return True                   # 연도 비정상
    return False



# 사이트 공통 문구(네비/푸터/배너) — 상세 추출에서 제외
_BOILERPLATE = (
    "사고신고", "지급정지", "고객센터", "Copyright", "개인정보", "이용약관",
    "공지사항", "전체메뉴", "로그인", "인증센터", "본문 바로가기", "절세 혜택을, 퇴직연금으로",
    "당사는", "금융투자상품", "원금 손실",
)


def _is_boiler(line: str) -> bool:
    return any(b in line for b in _BOILERPLATE)


def extract_details(detail_text: str) -> dict:
    """상세 페이지 본문에서 참여조건/혜택 휴리스틱 추출."""
    out = {"conditions": None, "benefits": None, "remarks": None}
    if not detail_text:
        return out
    text = " ".join(detail_text.split())
    if len(text) < 80:
        out["remarks"] = "상세 이미지 공지 (텍스트 추출 불가)"
        return out

    lines = [l.strip() for l in detail_text.splitlines()
             if l.strip() and len(l.strip()) >= 6 and not _is_boiler(l)]
    cond, bene = [], []
    for i, line in enumerate(lines):
        compact = line.replace(" ", "")
        if any(k in compact for k in ("참여대상", "이벤트대상", "참여조건", "응모대상", "대상고객", "참여방법")) \
                or compact.startswith(("대상:", "대상：")):   # 상세 페이지 흔한 표기 "대상 : ..."
            cond.append(" / ".join(lines[i:i + 2])[:300])
        # 혜택: 키워드 + 금액/수치 동반 시에만. 다른 이벤트 제목(…이벤트)으로 끝나는 줄 제외
        # (다단계 금액조건/리워드를 빠짐없이 담기 위해 줄 단위로 모두 수집)
        if any(k in compact for k in ("혜택", "지급", "경품", "리워드", "상품권", "수수료우대", "캐시백",
                                      "증정", "당첨", "추첨", "선착순")) \
                and re.search(r"[\d만천]+\s?원|\d+%|무료|평생|\d+명", compact) \
                and not compact.endswith("이벤트"):
            bene.append(line[:300])
    if cond:
        out["conditions"] = " | ".join(dict.fromkeys(cond))[:2000]
    if bene:
        # 다단계 혜택을 누락 없이 — 한도를 넉넉히
        out["benefits"] = " | ".join(dict.fromkeys(bene))[:3000]
    if not cond and not bene:
        out["remarks"] = "상세 자동추출 실패 — 원문 확인 필요 (이미지 공지 가능성)"
    return out


def content_hash(ev: dict) -> str:
    basis = "|".join(str(ev.get(k) or "") for k in (
        "firm_name", "event_name", "start_date", "end_date",
        "conditions", "benefits", "acct_etc",
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
