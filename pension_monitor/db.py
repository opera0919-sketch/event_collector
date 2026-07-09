# -*- coding: utf-8 -*-
"""Supabase(PostgREST) 적재 + 변동 감지.

SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 미설정 시 모든 함수가 no-op 으로
동작하고 main 에서 로컬 diff 만 수행한다.
"""

import datetime as dt

import requests

from .config import SUPABASE_URL, SUPABASE_KEY, FIRMS, EXCLUDED_FIRMS

MISSED_LIMIT = 2  # 연속 미노출 N회 → 종료 처리

# 인증/네트워크 등으로 DB 가 사용 불가로 판명되면 True → 이후 모든 호출 no-op.
# (읽기 1회 실패가 리포트·메일까지 무산시키지 않도록 파이프라인을 로컬-온리로 강등)
_DB_DOWN = False


def enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY) and not _DB_DOWN


def _headers(prefer=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _get(path, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=_headers(),
                     params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path, payload, prefer="return=representation"):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=_headers(prefer),
                      json=payload, timeout=30)
    if not r.ok:
        print(f"[db] POST {path} {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.text else None


def _patch(path, params, payload):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=_headers("return=representation"),
                       params=params, json=payload, timeout=30)
    if not r.ok:
        print(f"[db] PATCH {path} {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.text else None


def _delete(path, params):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{path}", headers=_headers("return=minimal"),
                        params=params, timeout=30)
    if not r.ok:
        print(f"[db] DELETE {path} {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return None


# 조건 타입드 컬럼 (docs/pipeline_mapping.md) — 신선한 추출 성공 건만 갱신 (무회귀)
_TYPED_COND_COLS = (
    "eligibility", "exclusions", "apply_required", "marketing_consent_required",
    "annual_cap_krw", "hold_condition", "cond_notes",
)

# 마스터 upsert 대상 컬럼 (자식 테이블/내부 키 제외)
_MASTER_COLS = (
    "firm_name", "event_name", "status", "start_date", "end_date",
    "acct_pension", "acct_irp", "acct_dc", "acct_etc",
    "conditions", "benefits", "remarks", "event_url", "content_hash",
    "source_event_id", "image_url", "extract_method", "date_source",
    "needs_review", "review_reason", "last_verified_at",
    "stackable", "annual_claim_limit", "source_content_hash",
    "extract_schema_version", "close_reason",
) + _TYPED_COND_COLS
_COND_COLS = ("ord", "label", "value_text", "source")
_BEN_COLS = ("tier_no", "condition_text", "benefit_text", "award_method", "award_limit", "source")
_MULT_COLS = ("source_type", "multiplier", "scope", "min_threshold_krw",
              "extra_condition", "source")


def replace_children(event_id, condition_rows, benefit_rows, multiplier_rows=None):
    """이벤트의 조건/혜택/배수 자식 행 교체 (신선한 추출 성공 건만 — 무회귀 원칙)."""
    if not (enabled() and event_id):
        return
    if benefit_rows:
        _safe(_delete, "event_benefits", {"event_id": f"eq.{event_id}"})
        _safe(_post, "event_benefits",
              [{**{k: r.get(k) for k in _BEN_COLS}, "event_id": event_id}
               for r in benefit_rows], prefer="return=minimal")
    if condition_rows:
        _safe(_delete, "event_conditions", {"event_id": f"eq.{event_id}"})
        _safe(_post, "event_conditions",
              [{**{k: r.get(k) for k in _COND_COLS}, "event_id": event_id}
               for r in condition_rows], prefer="return=minimal")
    # 배수 자식 테이블: 신선 추출 시 항상 교체(빈 배열이면 기존 행 삭제 = 배수 없음 반영)
    if multiplier_rows is not None:
        _safe(_delete, "pension_event_multipliers", {"event_id": f"eq.{event_id}"})
        if multiplier_rows:
            _safe(_post, "pension_event_multipliers",
                  [{**{k: r.get(k) for k in _MULT_COLS}, "event_id": event_id}
                   for r in multiplier_rows], prefer="return=minimal")


def _safe(fn, *a, **k):
    """DB 쓰기 1건을 안전 실행 — 실패해도 파이프라인(리포트/메일)은 계속."""
    try:
        return fn(*a, **k)
    except Exception as e:
        print(f"[db] 쓰기 실패(무시): {type(e).__name__}: {str(e)[:160]}")
        return None


def fetch_all_events():
    """기존 이벤트 전건 조회. 인증/조회 실패 시 DB 를 강등(no-op)하고 [] 반환.

    설계 의도(키 없으면 no-op, 쓰기는 _safe 로 보호)와 달리 이 읽기 경로만
    무방비라 401 한 번에 런 전체(리포트·메일 포함)가 죽던 문제를 막는다.
    실패해도 이후 수집·리포트·메일은 로컬 데이터로 계속 진행된다.
    """
    global _DB_DOWN
    try:
        return _get("pension_events", {"select": "*", "limit": "10000"})
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code in (401, 403):
            print(f"[db] 인증 실패({code}) — SUPABASE_SERVICE_ROLE_KEY 가 이 프로젝트의 "
                  "service_role 키가 아닙니다(만료/오타/잘림 또는 anon·publishable 키 혼동). "
                  "DB 동기화는 건너뛰고 수집·리포트·메일은 그대로 진행합니다.")
        else:
            print(f"[db] 조회 실패(HTTP {code}) — DB 동기화 생략, 파이프라인은 계속.")
        _DB_DOWN = True
        return []
    except Exception as e:
        print(f"[db] 조회 실패({type(e).__name__}: {str(e)[:120]}) — DB 동기화 생략, 파이프라인은 계속.")
        _DB_DOWN = True
        return []


def fetch_children(table: str) -> list:
    """자식 테이블(event_benefits/event_conditions) 전건 조회 — xlsx 첨부의 상세
    시트용. 조회 실패는 조용히 빈 목록으로 무시(리포트/메일은 항상 계속 진행)."""
    if not enabled():
        return []
    try:
        return _get(table, {"select": "*", "limit": "10000"})
    except Exception as e:
        print(f"[db] {table} 조회 실패(무시): {type(e).__name__}")
        return []


def build_index(existing: list) -> dict:
    """기존 이벤트 매칭 인덱스. source_event_id(불변) 우선, 자연키 폴백."""
    idx = {}
    for e in existing or []:
        sid = e.get("source_event_id")
        if sid:
            idx[("sid", e["firm_name"], sid)] = e
        idx[(e["firm_name"], e["event_name"], e.get("start_date"))] = e
    return idx


def find_existing(idx: dict, ev: dict):
    """수집 이벤트와 같은 DB 행 찾기. 자연키(이벤트명/기간)는 정규화가 흔들리면
    바뀌므로 상세 URL 의 고유 ID 매칭을 우선한다 (중복/허위 신규 방지)."""
    sid = ev.get("source_event_id")
    if sid:
        old = idx.get(("sid", ev["firm_name"], sid))
        if old is not None:
            return old
    return idx.get((ev["firm_name"], ev["event_name"], ev.get("start_date")))


def sync(scraped: list, firms_failed: list, trigger_type: str):
    """수집 결과를 DB와 동기화하고 변동 내역을 반환한다.

    반환: dict(new=[...], closed=[...], changed=[(event, field, old, new)...],
              active=[...], run_id=int|None)
    """
    today = dt.date.today().isoformat()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    existing = fetch_all_events() if enabled() else []
    idx = build_index(existing)
    matched_ids = set()
    failed_set = set(firms_failed)

    # ── 1단계: 변동 판정(순수 계산) + DB 쓰기는 건별 안전 실행 ──────
    # '변경' 이력 기록 대상: 원천 필드는 항상, 콘텐츠 필드는 이번 실행에서 실제
    # 재추출된 경우만(rows_fresh) — LLM 표현 요동이 변경 이력을 오염시키지 않게.
    _RAW_FIELDS = ("event_name", "start_date", "end_date")
    _CONTENT_FIELDS = ("conditions", "benefits", "acct_etc")
    _BOOL_FIELDS = ("acct_pension", "acct_irp", "acct_dc")
    _META_FIELDS = ("image_url", "extract_method", "date_source",
                    "needs_review", "review_reason", "remarks")
    new_events, changed = [], []
    for ev in scraped:
        ev["status"] = "종료" if (ev.get("end_date") and ev["end_date"] < today) else "진행중"
        old = find_existing(idx, ev)
        if old is None:
            new_events.append(ev)
            if enabled():
                row = {k: ev.get(k) for k in _MASTER_COLS}
                # NOT NULL boolean 컬럼은 None 전송 시 23502 로 INSERT 전체가 실패 → 강제 bool
                for f in ("acct_pension", "acct_irp", "acct_dc", "needs_review"):
                    row[f] = bool(row.get(f))
                row["last_seen_at"] = now
                created = _safe(_post, "pension_events", row)
                ev["id"] = created[0]["id"] if created else None
        else:
            matched_ids.add(old["id"])
            ev["id"] = old["id"]
            updates = {"last_seen_at": now, "missed_count": 0, "status": ev["status"]}
            if ev.get("source_event_id") and not old.get("source_event_id"):
                updates["source_event_id"] = ev["source_event_id"]
            for f in _RAW_FIELDS:
                if (old.get(f) or None) != (ev.get(f) or None):
                    changed.append((ev, f, old.get(f), ev.get(f)))
                    updates[f] = ev.get(f)
            for f in _CONTENT_FIELDS:
                if (old.get(f) or None) != (ev.get(f) or None):
                    updates[f] = ev.get(f)
                    if ev.get("rows_fresh"):
                        changed.append((ev, f, old.get(f), ev.get(f)))
            for f in _BOOL_FIELDS:
                if bool(old.get(f)) != bool(ev.get(f)):
                    updates[f] = bool(ev.get(f))
            for f in _META_FIELDS:
                if f in ev and (old.get(f) or None) != (ev.get(f) or None):
                    updates[f] = ev.get(f)
            # 타입드 조건 컬럼 + 배수 메타(stackable/annual_claim_limit): 이번 실행에서
            # 실제 재추출된 경우에만 갱신 (캐시/실패 건이 기존 값을 null 로 덮지 않도록 — 무회귀)
            if ev.get("rows_fresh"):
                for f in (*_TYPED_COND_COLS, "stackable", "annual_claim_limit"):
                    if old.get(f) != ev.get(f):    # False 도 유의미 값 — or-coalesce 금지
                        updates[f] = ev.get(f)
            if ev.get("last_verified_at"):
                updates["last_verified_at"] = ev["last_verified_at"]
            if old.get("content_hash") != ev.get("content_hash"):
                updates["content_hash"] = ev.get("content_hash")
            # 재추출 트리거 메타는 산출물과 무관하게 원문/스키마 상태를 따라 항상 동기화
            for f in ("source_content_hash", "extract_schema_version"):
                if f in ev and old.get(f) != ev.get(f):
                    updates[f] = ev.get(f)
            if old["status"] == "진행중" and ev["status"] == "종료":
                updates["closed_at"] = now
            if enabled():
                _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"}, updates)
        # 자식 테이블(조건/혜택/배수 행): 신선한 추출 성공 건만 교체 (무회귀)
        if ev.get("rows_fresh") and ev.get("id"):
            replace_children(ev["id"], ev.get("condition_rows") or [],
                             ev.get("benefit_rows") or [],
                             multiplier_rows=ev.get("multiplier_rows") or [])

    # 목록 미노출/만기 처리. 종료일 경과는 수집 성공 여부와 무관한 사실이므로
    # 실패 증권사 건이라도 즉시 종료 처리한다 (미만기 건만 오판 방지를 위해 유지).
    closed = []
    for old in existing:
        if old["id"] in matched_ids or old["status"] == "종료":
            continue
        expired = bool(old.get("end_date") and old["end_date"] < today)
        # 수집 제외 증권사(WAF 차단 등)는 '미노출 → 종료'로 판정하지 않는다(E8).
        # 만기(종료일 경과)는 수집 여부와 무관한 사실이므로 그대로 종료하되,
        # 미만기 건은 자동 종료 대신 수동 검증 대상으로만 표기한다.
        if old["firm_name"] in EXCLUDED_FIRMS and not expired:
            if enabled():
                _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"},
                      {"needs_review": True,
                       "review_reason": "수집 제외 증권사 — 수동 검증 필요"})
            continue
        if not expired and old["firm_name"] in failed_set:
            continue
        missed = (old.get("missed_count") or 0) + 1
        if expired or missed >= MISSED_LIMIT:
            closed.append(old)
            old["status"] = "종료"
            reason = "expired" if expired else (
                "firm_excluded" if old["firm_name"] in EXCLUDED_FIRMS else "missed")
            if enabled():
                _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"},
                      {"status": "종료", "closed_at": now, "missed_count": missed,
                       "close_reason": reason})
        elif enabled():
            _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"}, {"missed_count": missed})

    active = [e for e in scraped if e["status"] == "진행중"]
    # 수집 실패 증권사의 기존 진행중(미만기) 건은 리포트에서 사라지지 않도록 유지
    # 노출한다 (최종 확인일을 함께 표기 → "직전 데이터 유지" 문구와 동작 일치).
    for old in existing:
        if (old["firm_name"] in failed_set and old["status"] == "진행중"
                and old["id"] not in matched_ids):
            old["stale_seen"] = (old.get("last_seen_at") or "")[:10] or None
            active.append(old)
    result = {"new": new_events, "closed": closed, "changed": changed,
              "active": active, "run_id": None}

    # ── 2단계: 실행 로그/변동이력 기록 (실패해도 리포트·메일에는 영향 없음) ──
    if enabled():
        run = _safe(_post, "monitoring_runs", {
            "trigger_type": trigger_type,
            "firms_ok": len(FIRMS) - len(firms_failed),
            "failed_firms": ", ".join(firms_failed) or None,   # 증권사별 실패 이력 추적
            "firms_failed": len(firms_failed),
            "events_active": len(active),
            "events_new": len(new_events),
            "events_closed": len(closed),
            "events_changed": len(changed),
        })
        run_id = run[0]["id"] if run else None
        result["run_id"] = run_id
        # PostgREST 일괄 insert 는 모든 객체의 키 집합이 동일해야 함(PGRST102).
        # 신규/종료/변경 로그의 키를 통일(미사용 필드는 None)해서 한 번에 적재.
        def _log(eid, ctype, field=None, old=None, new=None):
            return {"run_id": run_id, "event_id": eid, "change_type": ctype,
                    "field_name": field[:60] if field else None,
                    "old_value": str(old)[:2000] if old is not None else None,
                    "new_value": str(new)[:2000] if new is not None else None}
        logs = []
        for ev in new_events:
            if ev.get("id"):
                logs.append(_log(ev["id"], "신규"))
        for old in closed:
            if old.get("id"):
                logs.append(_log(old["id"], "종료"))
        for ev, f, o, n in changed:
            if ev.get("id"):
                logs.append(_log(ev["id"], "변경", f, o, n))
        if logs:
            _safe(_post, "event_changes", logs, prefer="return=minimal")
    return result


def save_report(run_id, report_md):
    if enabled() and run_id:
        _patch("monitoring_runs", {"id": f"eq.{run_id}"}, {"report_md": report_md})
