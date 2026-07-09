# -*- coding: utf-8 -*-
"""연금 이벤트 모니터링 파이프라인 v2 엔트리포인트 (REDESIGN.md §6).

collect(목록) → detail 보강(본문 + 이미지 최대 3장) → normalize(검증 게이트)
→ db.sync(마스터 + 조건·혜택 자식 테이블) → report/mail/xlsx

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
import re
import sys

from . import db, mailer, normalize, report as report_mod
from .classify import (is_pension, detect_accounts, extract_details, content_hash,
                       source_event_id, clip_detail)
from .config import TRIGGER_TYPE
from .scrapers import SCRAPERS
from .scrapers.base import load_page
from .scrapers.koreainvestment import fetch_detail_text

MAX_DETAIL_FETCH = 36       # 상세 페이지 조회 상한 (대상 증권사 연금 이벤트 전건 커버)
DETAIL_BUDGET_SEC = 200     # 상세 조회 전체 시간 예산 (초과 시 중단 → 런 행 방지)

# 일시 네트워크 블립(미래에셋/NH 간헐 타임아웃) 흡수용: 실패 증권사 재시도 패스 수와 간격.
RETRY_PASSES = 2
RETRY_PASS_DELAY_SEC = 8

# 상세 로그(이벤트 전건·리포트 전문)는 로그/토큰 비용이 커서 기본 off. DEBUG=1 로 활성화.
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


# 개별 상세가 아니라 목록 페이지 그 자체인 URL(상세 본문 없음) — 경로(path) 기준 판정.
_LIST_PATH_SUFFIXES = ("eventList", "r01.do", "CUST_09_0003.jsp")


def _is_list_url(url: str) -> bool:
    from urllib.parse import urlparse
    p = urlparse(url)
    return p.path.rstrip("/").endswith(_LIST_PATH_SUFFIXES) or "eventList" in p.query


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
    """대상 증권사(config.FIRMS) 수집. 반환: (events, firms_failed)"""
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
                print(f"[수집실패] {firm}: {type(e).__name__}: {str(e)[:160]}")
                failed.append(firm)

        # 간헐 지연/거부(일시 타임아웃) 대응: 실패 증권사를 간격을 두고 재시도.
        for attempt in range(RETRY_PASSES):
            if not failed:
                break
            await asyncio.sleep(RETRY_PASS_DELAY_SEC)
            print(f"[재시도 {attempt + 1}/{RETRY_PASSES}] {failed}")
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

        # 정책: 웹검색 방식 미사용 — 데이터는 실제 페이지에서만 수집한다.
        # (키움증권은 EverSafe WAF 상시 차단으로 수집 대상 제외 — REDESIGN.md §2)

        # 연금 이벤트만 상세 보강
        pension = [e for e in events
                   if is_pension(e["event_name"] + " " + e.get("raw_text", ""))]
        await enrich_details(browser, pension)
        await browser.close()
    return events, failed


# 상세 페이지의 콘텐츠 이미지 상위 3장 (다단 배너 대응 — OCR 은 전부 한 요청에 전달)
JS_CONTENT_IMGS = r"""
() => Array.from(document.querySelectorAll('img'))
  .map(i => ({src: i.currentSrc || i.src || '', w: i.naturalWidth, h: i.naturalHeight}))
  .filter(i => i.src && i.w >= 280 && i.h >= 180
               && !/logo|icon|btn|sprite|nav_|bullet|arrow|blank|dot/i.test(i.src))
  .sort((a, b) => (b.w * b.h) - (a.w * a.h))
  .slice(0, 3)
  .map(i => i.src)
"""


def _imgs_from_html(soup, base_url, limit=3):
    """정적 HTML 에서 콘텐츠 배너 이미지 최대 limit 장 (아이콘/로고 제외)."""
    import re as _re
    from urllib.parse import urljoin
    out = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or _re.search(r"(logo|icon|btn|bullet|sprite|blank|dot|arrow|nav_)", src, _re.I):
            continue
        if _re.search(r"(cmd=down|/event/|fileUpload|mlist|/public/mw/event|upload\.file|/img/|/images/)",
                      src, _re.I):
            out.append(urljoin(base_url, src))
            if len(out) >= limit:
                break
    return out


async def enrich_details(browser, pension_events):
    """상세 페이지에서 본문 텍스트 + 콘텐츠 이미지(_image_urls)를 확보.
    스크레이퍼가 이미 채운 경우(NH eventList.json)는 재조회 생략."""
    import time
    from bs4 import BeautifulSoup
    from .scrapers.static_generic import fetch_html

    started = time.monotonic()
    fetched = 0
    for ev in pension_events:
        if fetched >= MAX_DETAIL_FETCH or time.monotonic() - started > DETAIL_BUDGET_SEC:
            print(f"[상세] 예산 도달 — {fetched}건 조회 후 중단")
            break
        if ev.get("_detail_text"):
            continue
        url = ev.get("_content_url") or ev.get("event_url") or ""
        if not url.startswith("http") or _is_list_url(url):
            continue
        # 한투: 상세 텍스트 전용 엔드포인트
        if ev["firm_name"] == "한국투자증권" and ev.get("_detail_id"):
            try:
                ev["_detail_text"] = clip_detail(fetch_detail_text(ev["_detail_id"]) or "")
                fetched += 1
            except Exception as e:
                print(f"[상세실패] 한국투자증권 {ev['event_name'][:24]}: {type(e).__name__}")
            continue
        need_img = not ev.get("_image_urls")
        detail_text = ""
        try:
            try:
                soup = BeautifulSoup(fetch_html(url, retries=1), "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                detail_text = soup.get_text("\n", strip=True)
                if need_img:
                    imgs = _imgs_from_html(soup, url)
                    if imgs:
                        ev["_image_urls"] = imgs
                        need_img = False
                # P2-1: 'Bonus Tip 상세예시' 등 배수 계산 예시가 별도 링크에 있는 경우 병합
                detail_text += _fetch_sub_content(soup, url)
            except Exception:
                detail_text = ""
            # 정적 텍스트가 빈약하거나(JS 렌더), 이미지가 여전히 없으면 브라우저로
            # 재확보한다. 실전(7/6)에서 KB 이미지 공지 건이 static fetch 에서
            # 200자 넘는 메뉴/네비 텍스트만 얻고 실제 콘텐츠(이미지)는 JS 렌더
            # 전용이라, 길이만 보고 렌더 자체를 건너뛰어 스크린샷 폴백이 발동하지
            # 못한 문제가 있었다 — need_img 인 동안은 짧은 '충분해 보이는' 텍스트도
            # 신뢰하지 않고 렌더를 한 번 더 시도한다(상한 2500자로 비용 제어).
            if len(detail_text) < 200 or (need_img and len(detail_text) < 2500):
                page = await load_page(browser, url, wait_ms=4000, retries=2)
                try:
                    detail_text = await page.inner_text("body")
                    if need_img:
                        srcs = await page.evaluate(JS_CONTENT_IMGS)
                        if srcs:
                            ev["_image_urls"] = srcs
                        elif len(detail_text) < 400:
                            # 최후 분기: 이미지 URL 추적 불가(iframe/지연로드/세션 URL)면
                            # 렌더링된 화면 자체를 스크린샷 → OCR 대상 (KB 이미지 공지 등)
                            import base64 as _b64
                            shot = await page.screenshot(full_page=True, type="jpeg",
                                                         quality=70)
                            if 10_000 < len(shot) <= 6_500_000:
                                ev["_screenshot_b64"] = _b64.b64encode(shot).decode("ascii")
                                print(f"[상세] {ev['firm_name']} {ev['event_name'][:24]}: "
                                      f"스크린샷 OCR 폴백 ({len(shot) // 1024}KB)")
                finally:
                    await page.close()
            fetched += 1
        except Exception as e:
            print(f"[상세실패] {ev['firm_name']} {ev['event_name'][:30]}: {type(e).__name__}")
        if detail_text:
            # 앞에서 자르지 않고 '본문 + 유의사항 꼬리(배수·제외재원)'를 보존해 절단
            ev["_detail_text"] = clip_detail(detail_text)


# P2-1: 배수 계산 예시가 담긴 서브 컨텐츠('Bonus Tip 상세예시' 등) 링크 1개 추적
_SUB_LINK_RE = re.compile(r"(상세\s*예시|Bonus\s*Tip|보너스|자세히|계산\s*예시)", re.I)


def _fetch_sub_content(soup, base_url) -> str:
    """상세 페이지의 서브 링크(배수 예시 등) 1개를 따라가 본문에 병합.
    레이어(모달)로만 뜨는 사이트는 정적 fetch 로 안 잡히므로 best-effort."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    from .scrapers.static_generic import fetch_html
    for a in soup.find_all("a", href=True):
        if not _SUB_LINK_RE.search(a.get_text(" ", strip=True)):
            continue
        sub = urljoin(base_url, a["href"])
        if sub == base_url or not sub.startswith("http"):
            continue
        try:
            s2 = BeautifulSoup(fetch_html(sub, retries=1), "html.parser")
            for tag in s2(["script", "style"]):
                tag.decompose()
            return "\n\n[상세예시]\n" + s2.get_text("\n", strip=True)
        except Exception:
            return ""
    return ""


def classify_all(events):
    """연금 이벤트 선별 + 명시 키워드 기반 계좌 판별 + 휴리스틱 1차 추출.
    기간 교정·LLM 구조화·검증 게이트는 normalize.normalize_events 가 담당."""
    out = []
    for ev in events:
        # 연금/계좌 판별은 목록 텍스트만 사용 — 상세 페이지의 네비/배너 문구 오염 방지
        blob = " ".join([ev["event_name"], ev.get("raw_text", "")])
        if not (ev.get("_category") == "연금" or is_pension(blob)):
            continue
        ev.update(detect_accounts(blob))
        details = extract_details(ev.get("_detail_text", ""))
        ev["conditions"] = details["conditions"] or ev.get("_conditions_hint")
        ev["benefits"] = details["benefits"]
        ev["remarks"] = None if ev["benefits"] else details["remarks"]
        out.append(ev)
    return out


def finalize(events):
    """content_hash(원천 기반) + image_url 확정 + 내부(_) 키 제거."""
    final = []
    for ev in events:
        if ev.get("_image_urls") and not ev.get("image_url"):
            ev["image_url"] = ev["_image_urls"][0]
        ev["content_hash"] = content_hash(ev)
        final.append({k: v for k, v in ev.items() if not k.startswith("_") or k == "_detail_id"})
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()

    events, failed = asyncio.run(collect())
    pension = classify_all(events)
    for ev in pension:
        # 안정 소스 ID(상세 URL 의 증권사 고유 파라미터) — DB 매칭/캐시의 1차 키
        ev["source_event_id"] = source_event_id(ev)
    existing = db.fetch_all_events() if db.enabled() else []
    # 정규화 v2: Gemini 구조화(캐시 우선) → 검증 게이트 → 캐노니컬 + 구조화 행
    normalize.normalize_events(pension, existing)
    pension = finalize(pension)
    by_firm = {}
    for ev in pension:
        by_firm[ev["firm_name"]] = by_firm.get(ev["firm_name"], 0) + 1
    print(f"전체 {len(events)}건 중 연금 관련 {len(pension)}건 "
          f"(증권사별 {by_firm}, 수집 실패: {failed or '없음'})")
    if DEBUG:
        for ev in pension:
            print(f"  - [{ev['firm_name']}] {ev['event_name']} ({ev.get('start_date')}~{ev.get('end_date')}) "
                  f"연금저축={ev.get('acct_pension')} IRP={ev.get('acct_irp')} DC={ev.get('acct_dc')} "
                  f"기타={ev.get('acct_etc')} 검토={ev.get('review_reason')}")

    pathlib.Path("data").mkdir(exist_ok=True)
    with open("data/events_latest.json", "w", encoding="utf-8") as f:
        json.dump({"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "firms_failed": failed, "events": pension}, f, ensure_ascii=False, indent=1)

    if args.collect_only:
        _write_summary(len(pension), by_firm, failed, diff=None)
        return

    if len(failed) >= len(SCRAPERS):
        print("[중단] 전 증권사 수집 실패 — DB 동기화 생략")
        _write_summary(0, by_firm, failed, diff=None, note="전 증권사 수집 실패")
        sys.exit(1)

    diff = db.sync(pension, failed, TRIGGER_TYPE)
    report_md = report_mod.build_report(diff, failed)
    db.save_report(diff.get("run_id"), report_md)

    today = dt.date.today().isoformat()
    pathlib.Path("reports").mkdir(exist_ok=True)
    with open(f"reports/{today}.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    with open("reports/latest.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"[저장] reports/{today}.md (+ latest.md)")

    _write_summary(len(diff["active"]), by_firm, failed, diff=diff)

    subject = (f"[연금이벤트] {today} — 진행중 {len(diff['active'])}건 "
               f"(신규 {len(diff['new'])}, 종료 {len(diff['closed'])})")
    attachments = []
    try:
        if db.enabled():
            table_rows = db.fetch_all_events()
            benefit_rows = db.fetch_children("event_benefits")
            condition_rows = db.fetch_children("event_conditions")
            multiplier_rows = db.fetch_children("pension_event_multipliers")
        else:
            # DB 미설정 폴백: 이번 실행분에서 신선 추출된 이벤트만 자식 행 보유
            table_rows = pension
            benefit_rows = [{**r, "firm_name": ev["firm_name"], "event_name": ev["event_name"]}
                            for ev in pension for r in (ev.get("benefit_rows") or [])]
            condition_rows = [{**r, "firm_name": ev["firm_name"], "event_name": ev["event_name"]}
                              for ev in pension for r in (ev.get("condition_rows") or [])]
            multiplier_rows = [{**r, "firm_name": ev["firm_name"], "event_name": ev["event_name"]}
                               for ev in pension for r in (ev.get("multiplier_rows") or [])]
        xlsx = report_mod.build_xlsx(table_rows, benefit_rows, condition_rows, multiplier_rows)
        attachments = [(f"pension_events_{today}.xlsx", xlsx,
                        "vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
    except Exception as e:
        print(f"[xlsx] 첨부 생성 실패(무시): {type(e).__name__}: {str(e)[:120]}")
    sent = mailer.send(subject, report_md, attachments=attachments)
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
