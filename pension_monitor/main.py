# -*- coding: utf-8 -*-
"""연금 이벤트 모니터링 파이프라인 엔트리포인트.

실행:
  python -m pension_monitor.main                # 전체 (DB/메일은 자격증명 있을 때만)
  python -m pension_monitor.main --collect-only # 수집·분류만 (검증용)
"""

import argparse
import os
import asyncio
import datetime as dt
import json
import pathlib
import sys
import traceback

from . import db, mailer, report as report_mod
from .classify import is_pension, detect_accounts, extract_details, content_hash
from .config import TRIGGER_TYPE
from .scrapers import SCRAPERS
from .scrapers.base import load_page
from .scrapers.koreainvestment import fetch_detail_text

MAX_DETAIL_FETCH = 20  # 상세 페이지 조회 상한 (사이트 부하 배려)

# 상세 로그(이벤트 전건·리포트 전문)는 로그/토큰 비용이 커서 기본 off.
# 디버깅 시 DEBUG=1 로 활성화.
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def write_step_summary(line: str):
    """GitHub Actions Step Summary 에 한 줄 기록 (실패 분석을 로그 전체 없이)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


async def collect():
    """6개사 수집. 반환: (events, firms_failed)"""
    from playwright.async_api import async_playwright
    events, failed = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        async def run_one(firm, fn, needs_browser):
            got = await fn(browser) if needs_browser else await fn()
            print(f"[수집] {firm}: {len(got)}건")
            return got

        for firm, fn, needs_browser in SCRAPERS:
            try:
                got = await run_one(firm, fn, needs_browser)
                if not got:
                    failed.append(firm)
                events.extend(got)
            except Exception as e:
                print(f"[수집실패] {firm}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed.append(firm)

        # 간헐 지연/거부 대응: 실패 증권사 1회 재시도
        if failed:
            print(f"[재시도] {failed}")
            still = []
            for firm, fn, needs_browser in SCRAPERS:
                if firm not in failed:
                    continue
                try:
                    got = await run_one(firm, fn, needs_browser)
                    if got:
                        events.extend(got)
                    else:
                        still.append(firm)
                except Exception as e:
                    print(f"[재시도실패] {firm}: {type(e).__name__}")
                    still.append(firm)
            failed = still
        # 연금 이벤트만 상세 보강
        pension = [e for e in events if is_pension(e["event_name"] + " " + e.get("raw_text", ""))]
        await enrich_details(browser, pension)
        await browser.close()
    return events, failed


async def enrich_details(browser, pension_events):
    from bs4 import BeautifulSoup
    from .scrapers.static_generic import fetch_html

    fetched = 0
    for ev in pension_events:
        if fetched >= MAX_DETAIL_FETCH:
            break
        url = ev.get("event_url") or ""
        if not url.startswith("http") or url.rstrip("/").endswith(("eventList", "r01.do")):
            continue
        detail_text = ""
        try:
            if ev["firm_name"] == "한국투자증권" and ev.get("_detail_id"):
                detail_text = fetch_detail_text(ev["_detail_id"])
            else:
                # requests 우선 (키움은 헤드리스 차단이라 필수), 부족하면 브라우저
                try:
                    soup = BeautifulSoup(fetch_html(url, retries=2), "html.parser")
                    for tag in soup(["script", "style"]):
                        tag.decompose()
                    detail_text = soup.get_text("\n", strip=True)
                except Exception:
                    detail_text = ""
                if len(detail_text) < 200 and ev["firm_name"] != "키움증권":
                    page = await load_page(browser, url, wait_ms=4000, retries=2)
                    try:
                        detail_text = await page.inner_text("body")
                    finally:
                        await page.close()
            fetched += 1
        except Exception as e:
            print(f"[상세실패] {ev['firm_name']} {ev['event_name'][:30]}: {type(e).__name__}")
        if detail_text:
            ev["_detail_text"] = detail_text[:8000]


def classify_all(events):
    out = []
    for ev in events:
        # 연금 판별/계좌 판별은 목록 텍스트만 사용 — 상세 페이지의 네비/배너 문구 오염 방지
        blob = " ".join([ev["event_name"], ev.get("raw_text", "")])
        if ev.get("_category") == "연금" or is_pension(blob):
            ev.update(detect_accounts(blob))
            details = extract_details(ev.get("_detail_text", ""))
            ev["conditions"] = details["conditions"] or ev.get("_conditions_hint")
            ev["benefits"] = details["benefits"] or ev.get("_benefits_hint")
            ev["remarks"] = None if ev["benefits"] else details["remarks"]
            if not ev.get("start_date") and not ev.get("end_date") and ev.get("_detail_text"):
                from .classify import parse_dates
                s, e = parse_dates(ev["_detail_text"][:2000])
                ev["start_date"], ev["end_date"] = ev.get("start_date") or s, ev.get("end_date") or e
            ev["content_hash"] = content_hash(ev)
            out.append({k: v for k, v in ev.items() if not k.startswith("_") or k == "_detail_id"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()

    events, failed = asyncio.run(collect())
    pension = classify_all(events)
    by_firm = {}
    for ev in pension:
        by_firm[ev["firm_name"]] = by_firm.get(ev["firm_name"], 0) + 1
    print(f"전체 {len(events)}건 중 연금 관련 {len(pension)}건 "
          f"(증권사별 {by_firm}, 수집 실패: {failed or '없음'})")
    if DEBUG:
        for ev in pension:
            print(f"  - [{ev['firm_name']}] {ev['event_name']} ({ev.get('start_date')}~{ev.get('end_date')}) "
                  f"연금저축={ev.get('acct_pension')} IRP={ev.get('acct_irp')} DC={ev.get('acct_dc')} 기타={ev.get('acct_etc')}")

    pathlib.Path("data").mkdir(exist_ok=True)
    with open("data/events_latest.json", "w", encoding="utf-8") as f:
        json.dump({"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "firms_failed": failed, "events": pension}, f, ensure_ascii=False, indent=1)

    if args.collect_only:
        _write_summary(len(pension), by_firm, failed, diff=None)
        return

    if len(failed) >= 6:
        print("[중단] 전 증권사 수집 실패 — DB 동기화 생략")
        _write_summary(0, by_firm, failed, diff=None, note="전 증권사 수집 실패")
        sys.exit(1)

    diff = db.sync(pension, failed, TRIGGER_TYPE)
    report_md = report_mod.build_report(diff, failed)
    db.save_report(diff.get("run_id"), report_md)

    today = dt.date.today().isoformat()
    pathlib.Path("reports").mkdir(exist_ok=True)
    # 날짜별 아카이브 + 항상 같은 경로(latest.md)에 사본 → 온디맨드 확인 시 작은 파일 하나만 읽으면 됨
    with open(f"reports/{today}.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    with open("reports/latest.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"[저장] reports/{today}.md (+ latest.md)")

    _write_summary(len(diff["active"]), by_firm, failed, diff=diff)

    subject = (f"[연금이벤트 위클리] {today} — 진행중 {len(diff['active'])}건 "
               f"(신규 {len(diff['new'])}, 종료 {len(diff['closed'])})")
    sent = mailer.send(subject, report_md)
    write_step_summary(
        f"진행중 {len(diff['active'])} · 신규 {len(diff['new'])} · 종료 {len(diff['closed'])} "
        f"· 변경 {len(diff['changed'])} · 수집실패 {failed or '없음'} · 메일 {'발송' if sent else '스킵'}")


def _write_summary(active, by_firm, failed, diff, note=""):
    """관측용 초소형 요약(JSON). 세션에서 로그 대신 이 파일만 읽으면 됨."""
    summary = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "trigger": TRIGGER_TYPE,
        "active": active,
        "by_firm": by_firm,
        "firms_failed": failed,
        "new": len(diff["new"]) if diff else None,
        "closed": len(diff["closed"]) if diff else None,
        "changed": len(diff["changed"]) if diff else None,
        "note": note,
    }
    pathlib.Path("data").mkdir(exist_ok=True)
    with open("data/last_run_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"[요약] {summary}")


if __name__ == "__main__":
    main()
