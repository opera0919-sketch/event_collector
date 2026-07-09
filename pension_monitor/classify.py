# -*- coding: utf-8 -*-
"""연금 이벤트 판별 및 대상계좌/필드 추출 휴리스틱."""

import datetime as dt
import hashlib
import re

from .config import (
    PENSION_KEYWORDS, ACCT_PENSION_KW, ACCT_IRP_KW, ACCT_DC_KW, ACCT_ETC_KW,
)

# 이전 유치형/배수 조항 탐지용 공통 정규식 (normalize 의 OCR 조건·커버리지 게이트와 공유)
_TRANSFER_HINT = re.compile(r"이전|가져오|수관|전환")
_MULT_HINT = re.compile(r"\d(?:\.\d)?\s*배")


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
    # 통칭 해석은 '퇴직연금'→IRP/DC(의미상 동치)만 유지.
    # '연금' 단독 → 연금저축+IRP 추정 규칙은 과대 표기라 폐지 (정확성 우선,
    # 미확정 시 normalize 가 LLM 판정 병합 후에도 없으면 '대상계좌 미확인' 플래그).
    if not any([acct["acct_pension"], acct["acct_irp"], acct["acct_dc"]]):
        if "퇴직연금" in t:
            acct["acct_irp"] = True
            acct["acct_dc"] = True
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
    """스크레이퍼가 준 기간이 의심스러운지(게시일 오인 등) 판정.
    장기 이벤트(예: 2024 시작 ~ 2026 종료)는 정상으로 두고, 종료일이 지나치게
    과거(게시일 오인)거나 1일 이하·역전·누락인 경우만 의심으로 본다."""
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
    if ed and not (2026 <= ed.year <= 2028):
        return True                       # 종료일 연도 비정상(과거 게시일 등)
    return False



# 사이트 공통 문구(네비/푸터/배너) — 상세 추출에서 제외
_BOILERPLATE = (
    "사고신고", "지급정지", "고객센터", "Copyright", "개인정보", "이용약관",
    "공지사항", "전체메뉴", "로그인", "인증센터", "본문 바로가기", "절세 혜택을, 퇴직연금으로",
    "당사는", "금융투자상품", "원금 손실",
)


def _is_boiler(line: str) -> bool:
    return any(b in line for b in _BOILERPLATE)


# 유의사항 블록 시작 신호 — 이 앞뒤(배수·제외재원·중복불가 등)는 반드시 보존
_TAIL_MARKERS = ("유의사항", "Bonus Tip", "보너스", "상세예시", "※", "필수 확인",
                 "배수", "배 인정", "중복 지급", "제외됩니다")


def clip_detail(text: str, limit: int = 8000) -> str:
    """앞에서 자르지 말고, 보일러플레이트를 걷어낸 뒤 '본문 + 유의사항 꼬리'를 보존.

    삼성 mbw 처럼 상단 네비/메뉴가 길고 배수·제외재원이 하단 유의사항에 있는
    사이트는 앞에서부터 8000자 절단 시 핵심 조항이 통째로 소실된다. 꼬리 신호가
    나타나는 지점부터 끝까지는 예산 내에서 무조건 확보한다."""
    lines = [l for l in (text or "").splitlines() if l.strip() and not _is_boiler(l)]
    body = "\n".join(lines)
    if len(body) <= limit:
        return body
    # 꼬리 신호가 처음 나타나는 지점부터 끝까지는 무조건 확보
    idx = min((body.find(m) for m in _TAIL_MARKERS if body.find(m) >= 0), default=-1)
    if idx < 0:
        return body[:limit]
    tail = body[idx:]
    if len(tail) >= limit:
        return tail[:limit]              # 꼬리만으로 예산 초과 → 꼬리 우선
    head_budget = limit - len(tail) - 20
    return body[:head_budget] + "\n…(중략)…\n" + tail


# ── G6: 요일 정합성 검사 (본문 'YYYY.MM.DD(요일)' 표기의 실제 요일 대조) ─────
_WD = "월화수목금토일"
_DATE_WD_RE = re.compile(
    r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})\s*[일]?\s*\(([월화수목금토일])\)")


def weekday_conflicts(text: str) -> list:
    """본문의 'YYYY.MM.DD(요일)' 표기 중 실제 요일과 불일치하는 항목 목록.
    원문 오타일 수도 있으나 시즌 기간 오추출(예: 시즌3이 시즌2 기간을 그대로
    물려받음)을 비용 0 으로 잡아내는 신호다."""
    bad = []
    for y, m, d, wd in _DATE_WD_RE.findall(text or ""):
        try:
            actual = _WD[dt.date(int(y), int(m), int(d)).weekday()]
        except ValueError:
            continue
        if actual != wd:
            bad.append(f"{y}-{int(m):02d}-{int(d):02d}({wd}≠{actual})")
    return bad


# ── G9: 잔고유지기간 역산으로 종료일 폴백 ───────────────────────────────
# 업계 관행: 잔고유지 시작일 = 이벤트 종료 다음날 (KB 시즌2 종료 6/30 → 유지 7/1).
_HOLD_RE = re.compile(
    r"(?:잔고\s*유지|유지)\s*기간[^0-9]{0,10}"
    r"(?:(\d{2,4})[.\-/년]\s*)?(\d{1,2})[.\-/월]\s*(\d{1,2})")


def infer_end_from_hold(text, year_hint):
    """잔고유지 시작일 - 1일 = 이벤트 종료일 로 역산. 실패 시 None."""
    m = _HOLD_RE.search(" ".join((text or "").split()))
    if not m:
        return None
    y = int(m.group(1) or year_hint)
    y = 2000 + y if y < 100 else y
    try:
        hold_start = dt.date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return (hold_start - dt.timedelta(days=1)).isoformat()


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


# 증권사별 상세 URL 의 고유 식별자 파라미터 (KB seq, NH mNo, 미래에셋 cs_ecis_id,
# 삼성 MenuSeqNo, 한투 num). 자연키(이벤트명/기간)는 정규화 결과라 실행마다 흔들릴
# 수 있어, 이 불변 ID 를 DB 매칭의 1차 키로 쓴다.
_SID_PARAMS = ("seq", "mNo", "cs_ecis_id", "MenuSeqNo", "num")


def source_event_id(ev: dict):
    """이벤트 상세 URL 에서 증권사 측 고유 ID 추출. 목록 URL 폴백 등 ID 가 없는
    경우 None (그 경우 자연키 매칭으로 폴백)."""
    if ev.get("_detail_id"):
        return str(ev["_detail_id"])
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(ev.get("event_url") or "").query)
    for p in _SID_PARAMS:
        v = qs.get(p)
        if v and v[0]:
            return v[0]
    return None


def content_hash(ev: dict) -> str:
    """변경 감지 해시 — 원천 데이터(식별자/기간)만 사용 (REDESIGN.md G6).
    LLM 산출물(conditions/benefits)을 포함하면 표현 요동만으로 허위 '변경'이
    기록되므로 제외한다. 조건/혜택의 실질 변경 반영은 normalize 의 캐시 규칙
    (동일 종료일 + 캐노니컬 존재 시 재추출 생략)과 sync 의 필드 비교가 담당."""
    basis = "|".join(str(ev.get(k) or "") for k in (
        "firm_name", "source_event_id", "event_name", "start_date", "end_date",
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def source_content_hash(ev: dict) -> str:
    """재추출 트리거 해시 — 상세 본문/이미지(LLM 입력 원문)가 바뀌면 재추출.

    content_hash 는 리포트용(원천 식별자/기간)이라 조항이 바뀌어도 흔들리지
    않는다. 반면 이 해시는 '무엇을 LLM 에 넣었는가'를 반영하므로, 상세 본문의
    유의사항/배수 조항이 바뀌면(또는 추출 개선으로 더 많이 읽으면) 캐시가
    무효화돼 재추출이 일어난다 (누락의 영구화 방지)."""
    basis = (ev.get("_detail_text") or "") + "|" + "|".join(ev.get("_image_urls") or [])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
