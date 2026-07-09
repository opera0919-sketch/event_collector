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
from .classify import (
    extract_period, suspicious_dates, weekday_conflicts, infer_end_from_hold,
    source_content_hash, _TRANSFER_HINT, _MULT_HINT,
)

# ── 예산 (Gemini 무료 티어 보호 — 기존 운영값 유지) ─────────────────
STRUCT_BUDGET = 40       # 1회 실행 Gemini 호출 상한
TIME_BUDGET_SEC = 360    # 구조화 전체 시간 예산(초)
PACE_SEC = 6.5           # 10 RPM 준수 간격
TEXT_MIN = 200           # 이 길이 이상이면 본문 텍스트로 구조화, 미만이면 이미지 OCR

# 목록의 기간 표기를 신뢰하는 증권사 (KB 는 게시일(idt) 혼입 이력 → 불신)
TRUSTED_LIST_DATES = {"미래에셋증권", "NH투자증권", "한국투자증권", "삼성증권"}

# G1: 무의미 응답 패턴 (한/영). 실전(2026-07-05~06)에서 구코드가 "혜택 없음
# (자료 없음)" 류를 재오염시켰는데 당시 패턴에 없어 통과된 사례가 있어 보강.
JUNK_RE = re.compile(
    r"(no\s+(benefits?|conditions?|informations?)|information\s+(found|available)"
    r"|not\s+(found|specified|mentioned|provided)|unknown|n/?a\b"
    r"|없습니다|내용\s*없음|정보\s*없음|정보가\s*없|명시되|확인\s*필요|알\s*수\s*없|해당\s*없"
    r"|혜택\s*없음|자료\s*없음|제공\s*내용\s*없음)", re.I)
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


# 배수(승수) 조항 정규화 — enum 밖 값은 '기타'/'인정금액'으로 보수화
_MULT_SOURCE_TYPES = ("타사이전", "타사ISA만기전환", "당사ISA만기전환", "퇴직금입금",
                      "개인납입", "비대면최초신규", "기타")
_MULT_SCOPES = ("인정금액", "리워드금액")


def clean_multipliers(res, source_kind="llm-text"):
    """Gemini multipliers[] → 자식 테이블 적재용 행. 배수 0 이하/무효는 제거."""
    out = []
    for m in (res.get("multipliers") or []):
        try:
            mult = float(m.get("multiplier") or 0)
        except (TypeError, ValueError):
            continue
        if mult <= 0:
            continue
        st = m.get("source_type") if m.get("source_type") in _MULT_SOURCE_TYPES else "기타"
        scope = m.get("scope") if m.get("scope") in _MULT_SCOPES else "인정금액"
        try:
            thr = int(m.get("min_threshold_krw") or 0)
        except (TypeError, ValueError):
            thr = 0
        extra = " ".join(str(m.get("extra_condition") or "").split())
        out.append({"source_type": st, "multiplier": mult, "scope": scope,
                    "min_threshold_krw": thr, "extra_condition": extra,
                    "source": source_kind})
    return out


# G8: 이전 유치형 이벤트가 반드시 담고 있어야 할 조항 (원문엔 있는데 추출엔 없으면 결함)
_REQUIRED_WHEN_TRANSFER = {
    "배수": _MULT_HINT,
    "제외재원": re.compile(r"제외|불인정|인정되지"),
}


def check_coverage(ev):
    """G8: '이전 유치형인데 배수·제외재원이 원문엔 있으나 추출엔 없다'를 결함으로 표기.
    → E1/E2(삼성 배수·제외재원 누락)를 첫 배치부터 needs_review 로 노출."""
    src = ev.get("_detail_text", "") or ""
    if not _TRANSFER_HINT.search((ev.get("event_name") or "") + src):
        return
    blob = " ".join(str(ev.get(k) or "") for k in
                    ("conditions", "cond_notes", "benefits", "eligibility", "exclusions"))
    if ev.get("multiplier_rows"):
        blob += " 배수 " + " ".join(f"{r['multiplier']}배" for r in ev["multiplier_rows"])
    for label, rx in _REQUIRED_WHEN_TRANSFER.items():
        if rx.search(src) and not rx.search(blob):
            _flag(ev, f"{label} 조항 원문에 존재하나 추출 누락 — 원문 확인 필요")


def check_period_collisions(events):
    """G7: 같은 증권사에서 시즌 넘버가 다른데 기간이 동일하면 오추출 의심 → 표기."""
    import collections
    buckets = collections.defaultdict(list)
    for ev in events:
        if not (ev.get("start_date") and ev.get("end_date")):
            continue
        base = re.sub(r"시즌\s*\d+|\bv?\d+차\b", "", ev.get("event_name") or "").strip()
        buckets[(ev.get("firm_name"), base, ev["start_date"], ev["end_date"])].append(ev)
    for group in buckets.values():
        if len(group) > 1:
            names = {e["event_name"] for e in group}
            if len(names) > 1:      # 이름은 다른데 기간이 같다
                for ev in group:
                    _flag(ev, f"기간 충돌(동일기간 이벤트: {sorted(names)})")


def _is_trustworthy(text, require_substance=True):
    """캐시 재사용/무회귀 폴백 대상 값이 실제로 신뢰할 만한지 재검사.

    G3(무회귀)는 'DB의 기존 값은 이미 검증됐다'고 가정하지만, 이 파이프라인
    바깥(구코드·수동편집 등)에서 값이 다시 쓰였을 수 있다. '좋아 보이는 값을
    영구 신뢰'하는 구멍을 막기 위해 재사용 직전에도 G1/게이트를 다시 적용한다."""
    t = (text or "").strip()
    if not t or JUNK_RE.search(t):
        return False
    return bool(SUBSTANCE_RE.search(t)) if require_substance else True


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
    # G6: 본문의 'YYYY.MM.DD(요일)' 표기와 실제 요일이 어긋나면 표기 (기간 오추출 신호)
    bad_wd = weekday_conflicts(ev.get("_detail_text", ""))
    if bad_wd:
        _flag(ev, f"요일 불일치 {bad_wd}")
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
    # G9: 정규식/LLM 이 기간을 못 줄 때, 잔고유지기간 시작일 - 1일 로 종료일 역산
    #     (KB 시즌3 등 기간 NULL 방지). 추론값이므로 감사 추적 가능하게 표기만.
    if not (ps and pe):
        inferred_end = infer_end_from_hold(ev.get("_detail_text", ""), year)
        if inferred_end:
            ev["end_date"], ev["date_source"] = inferred_end, "hold_inferred"
            if not ev.get("start_date") or ev["start_date"] > inferred_end:
                ev["start_date"] = None
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


def _needs_ocr(ev, res, b_rows, text):
    """텍스트 추출 성공 여부와 무관하게 이미지 OCR 이 필요한지 판정 (P0-4).

    기존엔 'b_rows 가 비었을 때'만 OCR 했다. 그러나 티어표가 텍스트로 나오면
    배수 안내(이미지)는 아예 안 보는 사각이 있었다 — 이전 유치형인데 배수를
    하나도 못 뽑았거나 본문에 'N배'가 보이는데 결과에 없으면 이미지를 본다."""
    if not b_rows:
        return True                                  # 기존 조건 유지
    if _TRANSFER_HINT.search((ev.get("event_name") or "") + text) \
            and not (res or {}).get("multipliers"):
        return True
    if _MULT_HINT.search(text) and not (res or {}).get("multipliers"):
        return True
    return False


# 조건 타입드 컬럼 (docs/pipeline_mapping.md) — 신규 수집분이 직접 채운다
TYPED_COND_FIELDS = ("eligibility", "exclusions", "apply_required",
                     "marketing_consent_required", "annual_cap_krw",
                     "hold_condition", "cond_notes")
# G10 원문 폴백 대상 — 라벨 없는 원문에서도 정규식으로 구조화되는 필드만.
# (cond_notes/eligibility 는 라벨 기반이라 원문 폴백 시 전체 텍스트가 유입됨 → 제외)
_RAW_FALLBACK_FIELDS = ("exclusions", "apply_required",
                        "marketing_consent_required", "annual_cap_krw", "hold_condition")


def _apply_success(ev, b_rows, c_rows, grounded, method,
                   mult_rows=None, stackable=None, annual_claim_limit=None):
    ev["benefit_rows"] = b_rows
    ev["multiplier_rows"] = mult_rows or []
    ev["stackable"] = stackable
    ev["annual_claim_limit"] = (annual_claim_limit or None)
    ev["rows_fresh"] = True
    ev["benefits"] = render_benefits(b_rows)
    # 타입드 조건 컬럼: 백필과 동일 파서를 캐노니컬 라벨 텍스트에 적용 (규칙 이원화 방지).
    # '기간' 라벨 행은 start/end 컬럼과 중복이므로 conditions/자식 행에서 제외
    # (기간 판정은 reconcile_period 가 담당 — pipeline_mapping.md §3).
    from src.backfill_conditions import parse_conditions
    typed = parse_conditions(render_conditions(c_rows))
    for f in TYPED_COND_FIELDS:
        ev[f] = typed[f]
    # G10: LLM 이 conditions 행으로 안 뽑은 타입드 값을 원문에서 직접 재파싱해 보충
    #      (덮어쓰기 아님 — 라벨 기반 결과가 있으면 그 값 우선). E7(마케팅동의 등)
    #      영구 NULL 방지. 단, 라벨 기반 필드(cond_notes/eligibility)는 보충 대상에서
    #      제외한다 — 라벨 없는 원문은 전체가 cond_notes 로 쏟아져 리포트를 오염시키고,
    #      G8 커버리지 검사(blob 에 원문 키워드가 섞임)를 무력화하기 때문. 정규식으로
    #      구조화되는 필드만 보충한다.
    typed_raw = parse_conditions(ev.get("_detail_text", "") or "")
    for f in _RAW_FALLBACK_FIELDS:
        if ev[f] is None:
            ev[f] = typed_raw[f]
    kept = [r for r in c_rows if r["label"] != "기간"]
    for i, r in enumerate(kept, 1):
        r["ord"] = i
    ev["condition_rows"] = kept
    if kept:
        ev["conditions"] = render_conditions(kept)
    ev["extract_method"] = method
    ev["last_verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    ev["remarks"] = None
    ev["review_reason"] = None   # 성공 추출은 이전 검토 사유를 해소 — 안 지우면 xlsx에 낡은 사유가 남는다
    if not grounded:
        _flag(ev, "근거 불일치(추출 수치가 원문과 대조 실패)")


def _reuse_cached(ev, old):
    ev["benefits"] = old["benefits"]
    ev["conditions"] = old.get("conditions") or ev.get("conditions")
    ev["extract_method"] = old.get("extract_method")
    ev["remarks"] = None
    # old 의 검토 사유를 기본값으로 물려받되, 호출부에서 old.needs_review 가 True 면
    # 곧이어 _flag() 가 다시 채운다 — False 인 경우 낡은 사유가 안 남도록 여기서 비운다.
    ev["review_reason"] = None
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
        # 재추출 트리거 메타: LLM 입력 원문 해시 + 스키마 버전. 캐시 조건이 이 둘을
        # 비교하므로, 조항이 바뀌거나 스키마를 고치면 자동으로 재추출된다 (누락 영구화 방지).
        ev["source_content_hash"] = source_content_hash(ev)
        ev["extract_schema_version"] = vision.EXTRACT_SCHEMA_VERSION
        old = db.find_existing(idx, ev)
        res = {}
        # 캐시: 같은 이벤트 + 같은 종료일 + 원문/스키마 불변 + 기존 캐노니컬 양호 → 재호출 생략.
        # source_content_hash/extract_schema_version 불일치면(조항 변경·추출 개선)
        # 재추출을 유발한다. _is_trustworthy 로 old.benefits 정크 여부도 재검사.
        cache_ok = (old
                    and old.get("end_date") == ev.get("end_date")
                    and old.get("source_content_hash") == ev["source_content_hash"]
                    and old.get("extract_schema_version") == vision.EXTRACT_SCHEMA_VERSION
                    and not old.get("needs_review")
                    and _is_trustworthy(old.get("benefits")))
        if cache_ok:
            _reuse_cached(ev, old)
            n_cache += 1
            reconcile_period(ev, res)
            apply_accounts(ev, res)
            continue

        b_rows, c_rows, grounded, mult_rows = [], [], True, []
        stackable = annual_claim_limit = None
        text = (ev.get("_detail_text") or "").strip()
        # 1) 본문 텍스트 우선 (저비용 + 근거 대조 가능)
        if len(text) >= TEXT_MIN and _may_call():
            if n_call:
                time.sleep(PACE_SEC)
            n_call += 1
            res = vision.extract_from_text(text, hint=ev["event_name"])
            b_rows, c_rows, grounded = clean_rows(res, source_text=text, source_kind="llm-text")
            mult_rows = clean_multipliers(res, "llm-text")
            stackable = (res or {}).get("stackable")
            annual_claim_limit = (res or {}).get("annual_claim_limit")
        # 2) 이미지 OCR (최대 3장 1요청). 텍스트 실패 시엔 전체 대체, 텍스트 성공이라도
        #    배수 조항을 못 뽑았으면(이전 유치형/본문 'N배') 배수만 병합한다 (P0-4).
        #    이미지 URL 확보가 불가능했던 건은 렌더링 스크린샷으로 폴백 (KB 이미지 공지 등)
        if _needs_ocr(ev, res, b_rows, text) and _may_call():
            imgs = _resolve_banner_images(ev)
            if not imgs and ev.get("_screenshot_b64"):
                imgs = [{"b64": ev["_screenshot_b64"], "mime": "image/jpeg"}]
            if imgs:
                if n_call:
                    time.sleep(PACE_SEC)
                n_call += 1
                res2 = vision.extract(imgs, referer=ev.get("event_url") or "",
                                      hint=ev["event_name"])
                b2, c2, _ = clean_rows(res2, source_kind="llm-ocr")
                m2 = clean_multipliers(res2, "llm-ocr")
                if b_rows:
                    # 텍스트 티어가 더 정확 → b_rows 덮어쓰지 말고 배수/메타만 보충
                    if m2 and not mult_rows:
                        mult_rows = m2
                    if stackable is None:
                        stackable = (res2 or {}).get("stackable")
                    if not annual_claim_limit:
                        annual_claim_limit = (res2 or {}).get("annual_claim_limit")
                elif b2:
                    res, b_rows, c_rows, grounded = res2, b2, c2, True
                    mult_rows = m2
                    stackable = (res2 or {}).get("stackable")
                    annual_claim_limit = (res2 or {}).get("annual_claim_limit")

        if b_rows:
            method = "text" if b_rows[0]["source"] == "llm-text" else "ocr"
            _apply_success(ev, b_rows, c_rows, grounded, method,
                           mult_rows=mult_rows, stackable=stackable,
                           annual_claim_limit=annual_claim_limit)
        else:
            # G3 무회귀: 기존 양호 값 유지 > 목록 요약 폴백 > 미확인 표기.
            # 세 갈래 모두 _is_trustworthy 로 재검사 — '비어있지 않음'만으로
            # 신뢰하면 파이프라인 밖(구코드·수동편집)에서 재유입된 정크를
            # 계속 '양호한 기존값'으로 오인해 영구 전파하게 된다(실전 재현 사례).
            if old and _is_trustworthy(old.get("benefits")):
                _reuse_cached(ev, old)
                if old.get("needs_review"):
                    _flag(ev, old.get("review_reason") or "재검증 실패 — 원문 확인 필요")
            elif _is_trustworthy(ev.get("benefits")):
                ev["extract_method"] = "heuristic"   # Gemini 미설정/실패 시 휴리스틱 유지
            elif _is_trustworthy(ev.get("_benefits_hint"), require_substance=False):
                ev["benefits"] = ev["_benefits_hint"]
                ev["extract_method"] = "hint"
                _flag(ev, "목록 요약 폴백(상세 추출 실패) — 원문 확인 필요")
            else:
                ev["benefits"] = None
                ev["extract_method"] = "none"
                _flag(ev, "혜택 미확인(본문/이미지 추출 실패) — 원문 확인 필요")

        reconcile_period(ev, res)
        apply_accounts(ev, res)
        # G8: 이전 유치형인데 배수·제외재원이 원문엔 있으나 추출엔 없으면 검토 표기
        check_coverage(ev)
        # DB NOT NULL 컬럼 — 플래그 미발생 시에도 명시적 False 로 적재 (null 금지)
        ev["needs_review"] = bool(ev.get("needs_review"))

    # G7: 동일 증권사 시즌 간 기간 충돌 (이름 다른데 기간 동일) 일괄 검사
    check_period_collisions(pension)
    for ev in pension:
        ev["needs_review"] = bool(ev.get("needs_review"))

    print(f"[정규화] Gemini {n_call}건 호출, 캐시 재사용 {n_cache}건")
