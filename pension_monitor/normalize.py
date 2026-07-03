# -*- coding: utf-8 -*-
"""정규화 v2 — 검증 게이트를 통과한 값만 적재한다 (REDESIGN.md §4).

게이트:
  G1 정크 차단   : '정보 없음' 류(한/영) 응답, 실질 없는 리워드 행 제거
  G2 근거 대조   : 텍스트 추출은 리워드의 수치 토큰이 원문에 존재하는지 검사
  G3 무회귀      : 추출 실패가 기존 양호 값을 덮어쓰지 않음
  G4 기간 신뢰원 : list(신뢰 증권사) > detail(본문 정규식) > llm, 확신 없으면 null
  G5 계좌 보수화 : 명시 신호/LLM OR, '연금' 단독 추정 폐지

산출: 이벤트 dict 에 캐노니컬 텍스트(conditions/benefits) + 구조화 행
(benefit_rows/condition_rows) + 검증 메타(needs_review/review_reason/
extract_method/date_source/last_verified_at/rows_fresh)를 채운다.
"""

import datetime as dt
import re
import time

from . import db, vision
from .classify import extract_period, suspicious_dates

# ── 예산 (Gemini 무료 티어 보호 — 기존 운영값 유지) ─────────────────
STRUCT_BUDGET = 40       # 1회 실행 Gemini 호출 상한
TIME_BUDGET_SEC = 360    # 구조화 전체 시간 예산(초)
PACE_SEC = 6.5           # 10 RPM 준수 간격
TEXT_MIN = 200           # 이 길이 이상이면 본문 텍스트로 구조화, 미만이면 이미지 OCR

# 목록의 기간 표기를 신뢰하는 증권사 (KB 는 게시일(idt) 혼입 이력 → 불신)
TRUSTED_LIST_DATES = {"미래에셋증권", "NH투자증권", "한국투자증권", "삼성증권"}

# G1: 무의미 응답 패턴 (한/영)
JUNK_RE = re.compile(
    r"(no\s+(benefits?|conditions?|informations?)|information\s+(found|available)"
    r"|not\s+(found|specified|mentioned|provided)|unknown|n/?a\b"
    r"|없습니다|내용\s*없음|정보\s*없음|정보가\s*없|명시되|확인\s*필요|알\s*수\s*없|해당\s*없)", re.I)
# 리워드 실질 토큰 — 하나도 없으면 혜택으로 인정하지 않음
SUBSTANCE_RE = re.compile(
    r"[\d만천백십억,\.]+\s*원|\d+(\.\d+)?\s*%|무료|우대|면제|할인|상품권|쿠폰|포인트"
    r"|캐시백|수수료|지원금|교환권|기프트|커피|아메리카노|이자")


def _flag(ev, reason):
    ev["needs_review"] = True
    prev = ev.get("review_reason")
    if not prev:
        ev["review_reason"] = reason
    elif reason not in prev:
        ev["review_reason"] = f"{prev} / {reason}"
    # 리포트 '검토 필요' 섹션 호환 (remarks 노출 경로 유지)
    ev["remarks"] = ev["review_reason"]


def _norm_src(text):
    return re.sub(r"[\s,]", "", text or "")


def _grounded(row, norm_src):
    """G2: 행의 수치 토큰(숫자+단위)이 원문에 존재하는가. 원문 없으면 검사 불가(True)."""
    if not norm_src:
        return True
    blob = f"{row['condition_text']} {row['benefit_text']}"
    for m in re.finditer(r"(\d[\d,\.]*)\s*(만원|천원|백만원|만|천원|원|%|명|회|주|건)?", blob):
        token = m.group(1).replace(",", "") + (m.group(2) or "")
        if len(token) < 2:          # '2' 단독 등 무의미 토큰은 건너뜀
            continue
        if token not in norm_src:
            return False
    return True


def clean_rows(res, source_text=None, source_kind="llm-text"):
    """Gemini 구조화 응답 → 게이트(G1/G2) 통과 행. 반환: (혜택행, 조건행, 근거일치)."""
    out_b, out_c, grounded_all = [], [], True
    for b in (res.get("benefits") or []):
        cond = " ".join(str(b.get("condition") or "").split())
        rew = " ".join(str(b.get("reward") or "").split())
        if not rew or JUNK_RE.search(rew) or JUNK_RE.search(cond):
            continue
        if not (SUBSTANCE_RE.search(rew) or SUBSTANCE_RE.search(cond)):
            continue
        method = b.get("method") if b.get("method") in ("전원", "선착순", "추첨") else None
        try:
            limit = int(b.get("limit_count") or 0) or None
        except (TypeError, ValueError):
            limit = None
        row = {"tier_no": len(out_b) + 1, "condition_text": cond or "-",
               "benefit_text": rew, "award_method": method, "award_limit": limit,
               "source": source_kind}
        if source_kind == "llm-text" and not _grounded(row, _norm_src(source_text)):
            grounded_all = False
        out_b.append(row)
    for c in (res.get("conditions") or []):
        val = " ".join(str(c.get("value") or "").split())
        if not val or JUNK_RE.search(val):
            continue
        label = c.get("label") if c.get("label") in ("대상", "기간", "신청", "유지조건", "한도") else "기타"
        out_c.append({"ord": len(out_c) + 1, "label": label,
                      "value_text": val, "source": source_kind})
    return out_b, out_c, grounded_all


def render_benefits(rows):
    """혜택 행 → 캐노니컬 텍스트 ('조건 → 리워드 (방식 N명)' 줄바꿈 구분)."""
    out = []
    for r in rows:
        suffix = ""
        if r.get("award_method"):
            n = f" {r['award_limit']:,}명" if r.get("award_limit") else ""
            suffix = f" ({r['award_method']}{n})"
        elif r.get("award_limit"):
            suffix = f" ({r['award_limit']:,}명)"
        out.append(f"{r['condition_text']} → {r['benefit_text']}{suffix}")
    return "\n".join(out)


def render_conditions(rows):
    return "\n".join(f"{r['label']}: {r['value_text']}" if r["label"] != "기타"
                     else r["value_text"] for r in rows)


def _valid_iso(s, lo, hi):
    try:
        d = dt.date.fromisoformat((s or "").strip())
        return d.isoformat() if lo <= d.year <= hi else None
    except (TypeError, ValueError):
        return None


def reconcile_period(ev, res):
    """G4: 기간 신뢰원 규칙. 확신 없으면 비우고 검토 표기 (틀린 날짜 노출 금지)."""
    year = dt.date.today().year
    ps = _valid_iso((res or {}).get("period_start"), year - 2, year + 2)
    pe = _valid_iso((res or {}).get("period_end"), year - 1, year + 2)
    if ps and pe and ps > pe:
        ps = pe = None
    list_ok = (ev["firm_name"] in TRUSTED_LIST_DATES
               and ev.get("start_date") and ev.get("end_date")
               and not suspicious_dates(ev.get("start_date"), ev.get("end_date")))
    if list_ok:
        ev["date_source"] = "list"
        if pe and pe != ev["end_date"]:
            _flag(ev, f"기간 불일치(목록 {ev['end_date']} vs 상세 {pe}) — 목록 우선 적용")
        return
    # 목록 불신/누락 → 상세 본문 '기간 :' 정규식
    ds, de = extract_period(ev.get("_detail_text", ""))
    if ds and de and not suspicious_dates(ds, de):
        ev["start_date"], ev["end_date"], ev["date_source"] = ds, de, "detail"
        return
    # → LLM 추출 (양끝 모두 유효할 때만)
    if ps and pe:
        ev["start_date"], ev["end_date"], ev["date_source"] = ps, pe, "llm"
        return
    if pe:
        ev["start_date"], ev["end_date"], ev["date_source"] = None, pe, "llm"
        return
    # 확신 없음: 의심스러운 목록 날짜는 노출하지 않는다
    had = ev.get("start_date") or ev.get("end_date")
    if had and suspicious_dates(ev.get("start_date"), ev.get("end_date")):
        ev["start_date"] = ev["end_date"] = None
        ev["date_source"] = None
        _flag(ev, "기간 미확인(목록 날짜 게시일 오인 의심)")
    elif not had:
        ev["date_source"] = None      # 진짜 상시(기간 무표기) — 검토 불요


def apply_accounts(ev, res):
    """G5: 명시 키워드 판정(detect_accounts 결과가 ev 에 선반영됨) OR LLM 판정."""
    for k in ("acct_pension", "acct_irp", "acct_dc"):
        if (res or {}).get(k):
            ev[k] = True
    etc = str((res or {}).get("acct_etc") or "").strip()
    if etc and not ev.get("acct_etc") and not JUNK_RE.search(etc):
        ev["acct_etc"] = etc[:60]
    if not any([ev.get("acct_pension"), ev.get("acct_irp"), ev.get("acct_dc"),
                ev.get("acct_etc")]):
        _flag(ev, "대상계좌 미확인")


def _resolve_banner_images(ev):
    """OCR 대상 이미지 확보: 스크레이퍼 제공(_image_urls) 우선, 없으면 상세 정적 HTML."""
    if ev.get("_image_urls"):
        return ev["_image_urls"][:vision.MAX_IMAGES]
    url = ev.get("_content_url") or ev.get("event_url") or ""
    if not url.startswith("http"):
        return []
    try:
        from urllib.parse import urljoin
        from bs4 import BeautifulSoup
        from .scrapers.static_generic import fetch_html
        soup = BeautifulSoup(fetch_html(url, retries=1), "html.parser")
        out = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or re.search(r"(logo|icon|btn|bullet|sprite|blank|dot|arrow|nav_)", src, re.I):
                continue
            if re.search(r"(cmd=down|/event/|fileUpload|mlist|/public/mw/event|upload\.file)", src, re.I):
                out.append(urljoin(url, src))
            if len(out) >= vision.MAX_IMAGES:
                break
        return out
    except Exception as e:
        print(f"[배너] 해상 실패 {ev['event_name'][:24]}: {type(e).__name__}")
        return []


def _apply_success(ev, b_rows, c_rows, grounded, method):
    ev["benefit_rows"], ev["condition_rows"] = b_rows, c_rows
    ev["rows_fresh"] = True
    ev["benefits"] = render_benefits(b_rows)
    if c_rows:
        ev["conditions"] = render_conditions(c_rows)
    ev["extract_method"] = method
    ev["last_verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    ev["remarks"] = None
    if not grounded:
        _flag(ev, "근거 불일치(추출 수치가 원문과 대조 실패)")


def _reuse_cached(ev, old):
    ev["benefits"] = old["benefits"]
    ev["conditions"] = old.get("conditions") or ev.get("conditions")
    ev["extract_method"] = old.get("extract_method")
    ev["remarks"] = None
    for k in ("acct_pension", "acct_irp", "acct_dc"):
        if old.get(k):
            ev[k] = True
    if old.get("acct_etc") and not ev.get("acct_etc"):
        ev["acct_etc"] = old["acct_etc"]


def normalize_events(pension, existing):
    """전 이벤트 정규화 오케스트레이션. Gemini 미설정 시 휴리스틱 값 유지 + 기간 규칙만 적용."""
    idx = db.build_index(existing)
    started = time.monotonic()
    n_call, n_cache = 0, 0

    def _may_call():
        return (vision.enabled() and not vision.blocked() and n_call < STRUCT_BUDGET
                and time.monotonic() - started <= TIME_BUDGET_SEC)

    for ev in pension:
        old = db.find_existing(idx, ev)
        res = {}
        # 캐시: 같은 이벤트 + 같은 종료일 + 기존 캐노니컬 양호(검토 플래그 없음) → 재호출 생략
        if (old and old.get("end_date") == ev.get("end_date")
                and (old.get("benefits") or "").strip()
                and ("\n" in old["benefits"] or "→" in old["benefits"])
                and not old.get("needs_review")):
            _reuse_cached(ev, old)
            n_cache += 1
            reconcile_period(ev, res)
            apply_accounts(ev, res)
            continue

        b_rows, c_rows, grounded = [], [], True
        text = (ev.get("_detail_text") or "").strip()
        # 1) 본문 텍스트 우선 (저비용 + 근거 대조 가능)
        if len(text) >= TEXT_MIN and _may_call():
            if n_call:
                time.sleep(PACE_SEC)
            n_call += 1
            res = vision.extract_from_text(text, hint=ev["event_name"])
            b_rows, c_rows, grounded = clean_rows(res, source_text=text, source_kind="llm-text")
        # 2) 실패/빈약 시 상세 이미지 OCR (최대 3장 1요청)
        if not b_rows and _may_call():
            imgs = _resolve_banner_images(ev)
            if imgs:
                if n_call:
                    time.sleep(PACE_SEC)
                n_call += 1
                res2 = vision.extract(imgs, referer=ev.get("event_url") or "",
                                      hint=ev["event_name"])
                b2, c2, _ = clean_rows(res2, source_kind="llm-ocr")
                if b2:
                    res, b_rows, c_rows, grounded = res2, b2, c2, True

        if b_rows:
            method = "text" if b_rows[0]["source"] == "llm-text" else "ocr"
            _apply_success(ev, b_rows, c_rows, grounded, method)
        else:
            # G3 무회귀: 기존 양호 값 유지 > 목록 요약 폴백 > 미확인 표기
            if old and (old.get("benefits") or "").strip():
                _reuse_cached(ev, old)
                if old.get("needs_review"):
                    _flag(ev, old.get("review_reason") or "재검증 실패 — 원문 확인 필요")
            elif (ev.get("benefits") or "").strip():
                ev["extract_method"] = "heuristic"   # Gemini 미설정/실패 시 휴리스틱 유지
            elif ev.get("_benefits_hint"):
                ev["benefits"] = ev["_benefits_hint"]
                ev["extract_method"] = "hint"
                _flag(ev, "목록 요약 폴백(상세 추출 실패) — 원문 확인 필요")
            else:
                ev["extract_method"] = "none"
                _flag(ev, "혜택 미확인(본문/이미지 추출 실패) — 원문 확인 필요")

        reconcile_period(ev, res)
        apply_accounts(ev, res)
        # DB NOT NULL 컬럼 — 플래그 미발생 시에도 명시적 False 로 적재 (null 금지)
        ev["needs_review"] = bool(ev.get("needs_review"))

    print(f"[정규화] Gemini {n_call}건 호출, 캐시 재사용 {n_cache}건")
