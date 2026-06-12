# -*- coding: utf-8 -*-
"""연금 이벤트 모니터링 파이프라인 엔트리포인트.

실행:
  python -m pension_monitor.main                # 전체 (DB/메일은 자격증명 있을 때만)
  python -m pension_monitor.main --collect-only # 수집·분류만 (검증용)
"""

import argparse
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


async def collect():
    """6개사 수집. 반환: (events, firms_failed)"""
    from playwright.async_api import async_playwright
    events, failed = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for firm, fn, needs_browser in SCRAPERS:
            try:
                got = await fn(browser) if needs_browser else await fn()
                print(f"[수집] {firm}: {len(got)}건")
                if not got:
                    failed.append(firm)
                events.extend(got)
            except Exception as e:
                print(f"[수집실패] {firm}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed.append(firm)
        # 연금 이벤트만 상세 보강
        pension = [e for e in events if is_pension(e["event_name"] + " " + e.get("raw_text", ""))]
        await enrich_details(browser, pension)
        await browser.close()
    return events, failed


async def enrich_details(browser, pension_events):
    fetched = 0
    for ev in pension_events:
        if fetched >= MAX_DETAIL_FETCH:
            break
        detail_text = ""
        try:
            if ev["firm_name"] == "한국투자증권" and ev.get("_detail_id"):
                detail_text = fetch_detail_text(ev["_detail_id"])
            elif (ev.get("event_url") or "").startswith("http") \
                    and ev["event_url"] != ev.get("_list_url"):
                page = await load_page(browser, ev["event_url"], wait_ms=4000, retries=2)
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
        blob = " ".join([ev["event_name"], ev.get("raw_text", ""), ev.get("_detail_text", "")[:1000] if ev.get("_detail_text") else ""])
        if ev.get("_category") == "연금" or is_pension(blob):
            ev.update(detect_accounts(blob))
            details = extract_details(ev.get("_detail_text", ""))
            ev["conditions"] = details["conditions"]
            ev["benefits"] = details["benefits"]
            ev["remarks"] = details["remarks"]
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
    print(f"\n전체 {len(events)}건 중 연금 관련 {len(pension)}건 (수집 실패: {failed or '없음'})")
    for ev in pension:
        print(f"  - [{ev['firm_name']}] {ev['event_name']} ({ev.get('start_date')}~{ev.get('end_date')}) "
              f"연금저축={ev.get('acct_pension')} IRP={ev.get('acct_irp')} DC={ev.get('acct_dc')} 기타={ev.get('acct_etc')}")

    pathlib.Path("data").mkdir(exist_ok=True)
    with open("data/events_latest.json", "w", encoding="utf-8") as f:
        json.dump({"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "firms_failed": failed, "events": pension}, f, ensure_ascii=False, indent=1)
    print("[저장] data/events_latest.json")

    if args.collect_only:
        return

    if len(failed) >= 6:
        print("[중단] 전 증권사 수집 실패 — DB 동기화 생략")
        sys.exit(1)

    diff = db.sync(pension, failed, TRIGGER_TYPE)
    report_md = report_mod.build_report(diff, failed)
    db.save_report(diff.get("run_id"), report_md)

    today = dt.date.today().isoformat()
    pathlib.Path("reports").mkdir(exist_ok=True)
    report_path = f"reports/{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"[저장] {report_path}")
    print("\n" + "=" * 60 + "\n" + report_md + "\n" + "=" * 60)

    subject = (f"[연금이벤트 위클리] {today} — 진행중 {len(diff['active'])}건 "
               f"(신규 {len(diff['new'])}, 종료 {len(diff['closed'])})")
    mailer.send(subject, report_md)


if __name__ == "__main__":
    main()
