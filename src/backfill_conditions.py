#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pension_events.conditions 자유 텍스트 → 타입드 컬럼 1회성 백필.

작업지시서 conditions_restructure_workorder.md §3 구현.
- 라벨(`라벨: 값`) 파싱 → eligibility/exclusions/apply_required/
  marketing_consent_required/annual_cap_krw/hold_condition/cond_notes
- `기간:` 라인은 날짜 정규화 후 start_date/end_date 가 NULL 인 경우에만 보충
  (기존 값 우선, 덮어쓰기 금지). conditions 원문은 수정하지 않는다.
- 단일 트랜잭션, 재실행 안전(규칙 기반 재계산 → 매 실행 동일 결과).

실행: DATABASE_URL 환경변수(.env 지원) 필요. 키 하드코딩 금지.
  python src/backfill_conditions.py [--dry-run]

파싱 함수(parse_conditions/parse_kr_date)는 순수 함수로, 수집 파이프라인과
validate_conditions.py 에서 재사용한다 (docs/pipeline_mapping.md §1).
"""

import argparse
import os
import re
import sys
from datetime import date

# ── 날짜 정규화 (§3.2) ─────────────────────────────────────────────
PATTERNS = [
    r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})",      # 2026.07.01 / 2026/03/30 / 2026-07-01
    r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일",       # 2026년 4월 1일(수)
]


def parse_kr_date(s: str):
    for p in PATTERNS:
        m = re.search(p, s)
        if m:
            y, mo, d = map(int, m.groups())
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    return None


def parse_period_line(value: str):
    """'기간' 라인 값 'A ~ B' → (startISO|None, endISO|None). B 누락 시 end None."""
    parts = re.split(r"[~∼]", value, maxsplit=1)
    start = parse_kr_date(parts[0]) if parts and parts[0].strip() else None
    end = parse_kr_date(parts[1]) if len(parts) > 1 and parts[1].strip() else None
    return (start.isoformat() if start else None, end.isoformat() if end else None)


# ── 라벨 → 컬럼 매핑 (§3.1) ────────────────────────────────────────
ELIGIBILITY_LABELS = ("대상", "대상고객", "대상계좌", "대상상품", "참여조건", "요건")
EXCLUSION_LABELS = ("제외", "제외대상")
APPLY_LABELS = ("신청", "신청필수")
HOLD_LABELS = ("유지조건",)
PERIOD_LABELS = ("기간",)
NOTE_LABELS = ("합산기준", "순입금 산정", "중복지급", "혜택지급", "적용매체", "기타", "조건")

# 연간 한도: '연 3만원', '연간 3만원', '연간 누적 3만원' 수용 (지시서 §3.1 + '누적' 변형).
# 폴백: '연간' 리터럴 + 수식어(≤20자) + 금액 — "연간 혜택제공 가능금액 최대 3만원" 류.
#   ('연간?'처럼 느슨하게 하면 '연금…한도 600만원'이 오검출되므로 폴백은 리터럴 한정)
_CAP_RE = re.compile(r"연간?\s*(?:누적\s*)?([0-9,]+)\s*만\s*원")
_CAP_FALLBACK_RE = re.compile(r"연간[^0-9%]{0,20}?([0-9,]+)\s*만\s*원")
# 연간 한도임이 문면상 명시된 추가 표현 (감독규정/연도 기준) — 오검출 위험 낮은 것만
_CAP_EXTRA_RES = (
    re.compile(r"감독규정에\s*의해\s*최대\s*([0-9,]+)\s*만\s*원"),
    re.compile(r"연도.{0,30}?([0-9,]+)\s*만\s*원"),
)


def _find_cap(text: str):
    for rx in (_CAP_RE, _CAP_FALLBACK_RE, *_CAP_EXTRA_RES):
        m = rx.search(text)
        if m:
            return int(m.group(1).replace(",", "")) * 10000
    return None
_APPLY_FALSE_RE = re.compile(r"불필요|자동\s*참여|자동참여|없음|없이")
_MKT_NEG_RE = re.compile(r"동의\s*(불필요|없이|하지\s*않)")


def _split_lines(text: str):
    """캐노니컬(\\n) + 구세대(' | ') 구분자 모두 라인으로 분해."""
    out = []
    for chunk in (text or "").split("\n"):
        out.extend(p.strip() for p in chunk.split(" | ") if p.strip())
    return out


# eligibility 본문에 섞인 '…제외' 절 추출 (§7-1 확정: exclusions 로 분리·복사.
# eligibility 원문은 §3.1 결정대로 수정하지 않는다)
_EXCL_PAREN_RE = re.compile(r"\(([^()]*제외[^()]*)\)")


def _exclusion_clauses(text: str):
    out = []
    # 괄호 안: '/' 로 절 분할 후 '제외'를 포함한 절만 (혼합 괄호에서 비제외 절 배제)
    for m in _EXCL_PAREN_RE.finditer(text):
        for seg in m.group(1).split("/"):
            seg = seg.strip(" .")
            if "제외" in seg and len(seg) >= 4:
                out.append(seg)
    # 괄호 밖: 구분자·문장 경계로 분할 후 '제외'로 끝나는 절만
    plain = _EXCL_PAREN_RE.sub(" ", text)
    for seg in re.split(r"[,/;·]|\.\s", plain):
        seg = seg.strip(" .")
        if seg.endswith("제외") and len(seg) >= 4:
            out.append(seg)
    return out


def _label_value(line: str):
    """'라벨: 값' → (라벨, 값). 라벨 없으면 (None, 원문)."""
    m = re.match(r"^\s*([^:：]{1,12})\s*[:：]\s*(.*)$", line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, line.strip()


def parse_conditions(text: str) -> dict:
    """conditions 원문 → 신규 컬럼 dict (기간은 'period' 키로 별도 반환)."""
    out = {"eligibility": None, "exclusions": None, "apply_required": None,
           "marketing_consent_required": None, "annual_cap_krw": None,
           "hold_condition": None, "cond_notes": None,
           "period": (None, None)}
    if not (text or "").strip():
        return out
    elig, excl, holds, notes = [], [], [], []
    cap_from_label = None
    for line in _split_lines(text):
        label, value = _label_value(line)
        if label in ELIGIBILITY_LABELS:
            elig.append(value)
        elif label in EXCLUSION_LABELS:
            excl.append(value)
        elif label in APPLY_LABELS:
            if _APPLY_FALSE_RE.search(value):
                out["apply_required"] = False
            elif "필수" in value or label == "신청필수":
                out["apply_required"] = True
            else:
                notes.append(f"신청: {value}")     # 판정 불가 → NULL + 원문 보존
        elif label in HOLD_LABELS:
            if value.strip():
                holds.append(value.strip())        # 복수 라인 → '; ' 연결 (원문 그대로)
        elif label in PERIOD_LABELS:
            # 기간 라벨이 여러 번 나오면(예: 상품권 유효기간) 첫 이벤트 기간만 사용
            if out["period"] == (None, None):
                out["period"] = parse_period_line(value)
            else:
                notes.append(f"기간: {value}")
        elif label == "한도":
            cap_from_label = _find_cap(value)
            notes.append(f"한도: {value}")
        elif label in NOTE_LABELS:
            notes.append(f"{label}: {value}")
        elif label:
            notes.append(f"{label}: {value}")      # 미매핑 라벨
        else:
            notes.append(value)                    # 라벨 없는 서술
    # 한도: '한도' 라인 우선, 없으면 본문 전체
    out["annual_cap_krw"] = cap_from_label if cap_from_label is not None else _find_cap(text)
    out["hold_condition"] = "; ".join(holds) or None
    # 마케팅 동의: 본문 전체 판정 (부정 표현 우선)
    if "마케팅" in text and "동의" in text:
        if _MKT_NEG_RE.search(text):
            out["marketing_consent_required"] = False
        elif any(x in text for x in ("SMS", "PUSH", "필수")):
            out["marketing_consent_required"] = True
    out["eligibility"] = "; ".join(elig) or None
    # §7-1: eligibility 내 '…제외' 절을 exclusions 로 분리 (라벨 명시분 뒤에 추가, 중복 제거)
    if out["eligibility"]:
        for clause in _exclusion_clauses(out["eligibility"]):
            if clause not in excl:
                excl.append(clause)
    out["exclusions"] = "; ".join(excl) or None
    out["cond_notes"] = "\n".join(notes) or None
    return out


# ── DB 적용 (단일 트랜잭션) ────────────────────────────────────────
def build_update_sql(row_id: int, parsed: dict, cur_start, cur_end) -> str:
    def q(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    ps, pe = parsed["period"]
    sets = [f"eligibility = {q(parsed['eligibility'])}",
            f"exclusions = {q(parsed['exclusions'])}",
            f"apply_required = {q(parsed['apply_required'])}",
            f"marketing_consent_required = {q(parsed['marketing_consent_required'])}",
            f"annual_cap_krw = {q(parsed['annual_cap_krw'])}",
            f"hold_condition = {q(parsed['hold_condition'])}",
            f"cond_notes = {q(parsed['cond_notes'])}"]
    # 기간 보충: 기존 값이 NULL 인 경우에만 (덮어쓰기 금지)
    if ps and cur_start is None:
        sets.append(f"start_date = {q(ps)}")
    if pe and cur_end is None:
        sets.append(f"end_date = {q(pe)}")
    return f"UPDATE public.pension_events SET {', '.join(sets)} WHERE id = {row_id};"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL 미설정 (.env 또는 환경변수)")
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, conditions, start_date, end_date "
                        "FROM public.pension_events WHERE conditions IS NOT NULL ORDER BY id")
            rows = cur.fetchall()
            stmts = [build_update_sql(i, parse_conditions(c), s, e) for i, c, s, e in rows]
            if args.dry_run:
                print("\n".join(stmts))
                return
            for st in stmts:
                cur.execute(st)
        conn.commit()                       # 전체 성공 시에만 커밋 (부분 실패 → 롤백)
        print(f"[백필] {len(rows)}행 갱신 완료 (트랜잭션 1개)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
