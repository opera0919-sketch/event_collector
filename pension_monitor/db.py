# -*- coding: utf-8 -*-
"""Supabase(PostgREST) 적재 + 변동 감지.

SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 미설정 시 모든 함수가 no-op 으로
동작하고 main 에서 로컬 diff 만 수행한다.
"""

import datetime as dt

import requests

from .config import SUPABASE_URL, SUPABASE_KEY

MISSED_LIMIT = 2  # 연속 미노출 N회 → 종료 처리


def enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


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


def _safe(fn, *a, **k):
    """DB 쓰기 1건을 안전 실행 — 실패해도 파이프라인(리포트/메일)은 계속."""
    try:
        return fn(*a, **k)
    except Exception as e:
        print(f"[db] 쓰기 실패(무시): {type(e).__name__}: {str(e)[:160]}")
        return None


def fetch_all_events():
    return _get("pension_events", {"select": "*", "limit": "10000"})


def sync(scraped: list, firms_failed: list, trigger_type: str):
    """수집 결과를 DB와 동기화하고 변동 내역을 반환한다.

    반환: dict(new=[...], closed=[...], changed=[(event, field, old, new)...],
              active=[...], run_id=int|None)
    """
    today = dt.date.today().isoformat()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    existing = fetch_all_events() if enabled() else []
    by_key = {(e["firm_name"], e["event_name"], e.get("start_date")): e for e in existing}
    scraped_keys = set()
    failed_set = set(firms_failed)

    # ── 1단계: 변동 판정(순수 계산) + DB 쓰기는 건별 안전 실행 ──────
    new_events, changed = [], []
    for ev in scraped:
        key = (ev["firm_name"], ev["event_name"], ev.get("start_date"))
        scraped_keys.add(key)
        ev["status"] = "종료" if (ev.get("end_date") and ev["end_date"] < today) else "진행중"
        old = by_key.get(key)
        if old is None:
            new_events.append(ev)
            if enabled():
                row = {k: ev.get(k) for k in (
                    "firm_name", "event_name", "status", "start_date", "end_date",
                    "acct_pension", "acct_irp", "acct_dc", "acct_etc",
                    "conditions", "benefits", "remarks", "event_url", "content_hash")}
                row["last_seen_at"] = now
                created = _safe(_post, "pension_events", row)
                ev["id"] = created[0]["id"] if created else None
        else:
            ev["id"] = old["id"]
            updates = {"last_seen_at": now, "missed_count": 0, "status": ev["status"]}
            if old.get("content_hash") != ev.get("content_hash"):
                for f in ("end_date", "start_date", "conditions", "benefits", "acct_etc"):
                    if (old.get(f) or None) != (ev.get(f) or None):
                        changed.append((ev, f, old.get(f), ev.get(f)))
                        updates[f] = ev.get(f)
                updates["content_hash"] = ev.get("content_hash")
            if old["status"] == "진행중" and ev["status"] == "종료":
                updates["closed_at"] = now
            if enabled():
                _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"}, updates)

    # 목록 미노출 처리 (수집 실패한 증권사는 건드리지 않음 → 오판 방지)
    closed = []
    for key, old in by_key.items():
        if key in scraped_keys or old["status"] == "종료":
            continue
        if old["firm_name"] in failed_set:
            continue
        missed = (old.get("missed_count") or 0) + 1
        if missed >= MISSED_LIMIT or (old.get("end_date") and old["end_date"] < today):
            closed.append(old)
            if enabled():
                _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"},
                      {"status": "종료", "closed_at": now, "missed_count": missed})
        elif enabled():
            _safe(_patch, "pension_events", {"id": f"eq.{old['id']}"}, {"missed_count": missed})

    active = [e for e in scraped if e["status"] == "진행중"]
    result = {"new": new_events, "closed": closed, "changed": changed,
              "active": active, "run_id": None}

    # ── 2단계: 실행 로그/변동이력 기록 (실패해도 리포트·메일에는 영향 없음) ──
    if enabled():
        run = _safe(_post, "monitoring_runs", {
            "trigger_type": trigger_type,
            "firms_ok": 6 - len(firms_failed),
            "firms_failed": len(firms_failed),
            "events_active": len(active),
            "events_new": len(new_events),
            "events_closed": len(closed),
            "events_changed": len(changed),
        })
        run_id = run[0]["id"] if run else None
        result["run_id"] = run_id
        logs = []
        for ev in new_events:
            if ev.get("id"):
                logs.append({"run_id": run_id, "event_id": ev["id"], "change_type": "신규"})
        for old in closed:
            if old.get("id"):
                logs.append({"run_id": run_id, "event_id": old["id"], "change_type": "종료"})
        for ev, f, o, n in changed:
            if ev.get("id"):
                logs.append({"run_id": run_id, "event_id": ev["id"], "change_type": "변경",
                             "field_name": f[:60], "old_value": str(o or "")[:2000],
                             "new_value": str(n or "")[:2000]})
        if logs:
            _safe(_post, "event_changes", logs, prefer="return=minimal")
    return result


def save_report(run_id, report_md):
    if enabled() and run_id:
        _patch("monitoring_runs", {"id": f"eq.{run_id}"}, {"report_md": report_md})
