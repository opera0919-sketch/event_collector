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

MAX_DETAIL_FETCH = 36       # 상세 페이지 조회 상한 (6개사 연금 이벤트 전건 커버; 시간은 아래 예산이 보호)
DETAIL_BUDGET_SEC = 200     # 상세 조회 전체 시간 예산 (초과 시 중단 → 런 행 방지)

# 매 실행 구조적으로 실패하는(헤드리스 차단 등) 증권사 — 재시도 패스에서 제외해
# 불필요한 풀 타임아웃 대기를 없앤다. 1차 시도만 하고 실패로 둔다.
KNOWN_HARD = {"키움증권"}

# 일시 네트워크 블립(미래에셋/NH 간헐 타임아웃) 흡수용: 실패 증권사 재시도 패스 수와 간격.
RETRY_PASSES = 2
RETRY_PASS_DELAY_SEC = 8

# 상세 로그(이벤트 전건·리포트 전문)는 로그/토큰 비용이 커서 기본 off.
# 디버깅 시 DEBUG=1 로 활성화.
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


# 개별 상세가 아니라 목록 페이지 그 자체인 URL(상세 본문 없음) — 경로(path) 기준 판정.
# 쿼리스트링(예: 미래에셋 상세의 returnURL=/…/r01.do)에 목록 경로가 들어가도 오판 않도록 path 만 본다.
_LIST_PATH_SUFFIXES = ("eventList", "r01.do", "CUST_09_0003.jsp")


def _is_list_url(url: str) -> bool:
    from urllib.parse import urlparse
    p = urlparse(url)
    # 삼성은 목록/상세가 같은 경로(noticeEvent.do)에 cmd=eventList/eventView 로 구분.
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
                print(f"[수집실패] {firm}: {type(e).__name__}: {str(e)[:160]}")
                failed.append(firm)

        # 간헐 지연/거부(일시 타임아웃) 대응: 실패 증권사를 시간 간격을 두고 여러 번
        # 재시도해 짧은 네트워크 블립을 흡수한다 (구조적 실패 증권사 KNOWN_HARD 는 제외).
        for attempt in range(RETRY_PASSES):
            retryable = [f for f in failed if f not in KNOWN_HARD]
            if not retryable:
                break
            await asyncio.sleep(RETRY_PASS_DELAY_SEC)
            print(f"[재시도 {attempt + 1}/{RETRY_PASSES}] {retryable}")
            still = [f for f in failed if f in KNOWN_HARD]
            for firm, fn, needs_browser in SCRAPERS:
                if firm not in retryable:
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
        # (키움 등 WAF 차단 증권사는 수집 실패로 처리하고 리포트에 명시)

        # 연금 이벤트만 상세 보강
        pension = [e for e in events
                   if is_pension(e["event_name"] + " " + e.get("raw_text", ""))]
        await enrich_details(browser, pension)
        await browser.close()
    return events, failed


JS_LARGEST_IMG = r"""
() => {
  const imgs = Array.from(document.querySelectorAll('img'))
    .map(i => ({src: i.currentSrc || i.src || '', w: i.naturalWidth, h: i.naturalHeight}))
    .filter(i => i.src && i.w >= 280 && i.h >= 180
                 && !/logo|icon|btn|sprite|nav_|bullet|arrow|blank|dot/i.test(i.src))
    .sort((a, b) => (b.w * b.h) - (a.w * a.h));
  return imgs.length ? imgs[0].src : null;
}
"""


def _img_from_html(soup, base_url):
    """정적 HTML 에서 콘텐츠 배너 이미지 1개 추출 (아이콘/로고 제외)."""
    import re as _re
    from urllib.parse import urljoin
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or _re.search(r"(logo|icon|btn|bullet|sprite|blank|dot|arrow|nav_)", src, _re.I):
            continue
        if _re.search(r"(cmd=down|/event/|fileUpload|mlist|/public/mw/event|upload\.file|/img/|/images/)",
                      src, _re.I):
            return urljoin(base_url, src)
    return None


async def enrich_details(browser, pension_events):
    """상세 페이지에서 텍스트 + 배너 이미지(_image_url) 확보.
    스크레이퍼가 이미 _image_url 을 준 경우(NH/미래에셋)는 이미지 탐색 생략."""
    import time
    from bs4 import BeautifulSoup
    from .scrapers.static_generic import fetch_html

    started = time.monotonic()
    fetched = 0
    for ev in pension_events:
        if fetched >= MAX_DETAIL_FETCH or time.monotonic() - started > DETAIL_BUDGET_SEC:
            print(f"[상세] 예산 도달 — {fetched}건 조회 후 중단")
            break
        # 스크레이퍼가 이미 상세 본문을 채운 경우(NH eventList.json) 재조회 불필요.
        if ev.get("_detail_text"):
            continue
        # _content_url: 본문이 별도 엔드포인트에 있는 경우 그쪽을 받는다.
        url = ev.get("_content_url") or ev.get("event_url") or ""
        if not url.startswith("http") or _is_list_url(url):
            continue
        # 한투: 상세 텍스트 경로(혜택 폴백은 목록 부제). 이미지 OCR 불필요.
        if ev["firm_name"] == "한국투자증권" and ev.get("_detail_id"):
            try:
                ev["_detail_text"] = (fetch_detail_text(ev["_detail_id"]) or "")[:8000]
                fetched += 1
            except Exception as e:
                print(f"[상세실패] 한국투자증권 {ev['event_name'][:24]}: {type(e).__name__}")
            continue
        need_img = not ev.get("_image_url")
        detail_text = ""
        try:
            try:
                soup = BeautifulSoup(fetch_html(url, retries=1), "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                detail_text = soup.get_text("\n", strip=True)
                if need_img:
                    img = _img_from_html(soup, url)
                    if img:
                        ev["_image_url"] = img
                        need_img = False
            except Exception:
                detail_text = ""
            # 정적 텍스트가 빈약하면(JS 렌더) 브라우저로 텍스트+이미지 확보 (KB 상세 등)
            if len(detail_text) < 200 and ev["firm_name"] != "키움증권":
                page = await load_page(browser, url, wait_ms=4000, retries=2)
                try:
                    detail_text = await page.inner_text("body")
                    if need_img:
                        src = await page.evaluate(JS_LARGEST_IMG)
                        if src:
                            ev["_image_url"] = src
                finally:
                    await page.close()
            fetched += 1
        except Exception as e:
            print(f"[상세실패] {ev['firm_name']} {ev['event_name'][:30]}: {type(e).__name__}")
        if detail_text:
            ev["_detail_text"] = detail_text[:8000]


def classify_all(events):
    """연금 이벤트 선별 + 계좌/혜택 1차 추출. content_hash/스트립은 finalize 에서.
    (이미지 OCR 보강이 hash 계산 전에 끼어들어야 해서 분리)."""
    out = []
    for ev in events:
        # 연금 판별/계좌 판별은 목록 텍스트만 사용 — 상세 페이지의 네비/배너 문구 오염 방지
        blob = " ".join([ev["event_name"], ev.get("raw_text", "")])
        if not (ev.get("_category") == "연금" or is_pension(blob)):
            continue
        ev.update(detect_accounts(blob))
        details = extract_details(ev.get("_detail_text", ""))
        ev["conditions"] = details["conditions"] or ev.get("_conditions_hint")
        # 혜택: 상세 본문 추출 우선. 목록 요약(_benefits_hint)은 OCR 실패 시
        # 최후 폴백으로만 사용 → 빈약한 요약이 OCR(상세 이미지)을 막지 않게 한다.
        ev["benefits"] = details["benefits"]
        ev["remarks"] = None if ev["benefits"] else details["remarks"]
        if not ev.get("start_date") and not ev.get("end_date") and ev.get("_detail_text"):
            from .classify import parse_dates
            s, e = parse_dates(ev["_detail_text"][:2000])
            ev["start_date"], ev["end_date"] = ev.get("start_date") or s, ev.get("end_date") or e
        out.append(ev)
    return out


def finalize(events):
    """content_hash 계산 + 내부(_) 키 제거 → 저장/동기화용 레코드."""
    final = []
    for ev in events:
        ev["content_hash"] = content_hash(ev)
        final.append({k: v for k, v in ev.items() if not k.startswith("_") or k == "_detail_id"})
    return final


STRUCT_BUDGET = 40       # 1회 실행 Gemini 구조화 호출 상한 (본문 텍스트는 저비용, 이미지 폴백 포함)
OCR_TIME_BUDGET = 360    # 구조화 전체 시간 예산(초)
OCR_PACE_SEC = 6.5       # Gemini 무료티어 10 RPM 준수용 호출 간 간격
_TEXT_MIN = 200          # 이 길이 이상이면 본문 텍스트로 구조화, 미만이면 이미지 OCR

# Gemini가 '명시 안 됨' 류 무의미 응답을 낸 경우 빈 값 처리 (검토필요로 남김)
_OCR_JUNK = ("명시되어 있지", "명시되어있지", "확인 필요", "없습니다", "알 수 없", "정보가 없")


def _apply_structured(ev, res):
    """Gemini 표준 구조화 결과를 이벤트에 반영(덮어쓰기). 무의미 응답은 무시."""
    if not res:
        return False
    ben = (res.get("benefits") or "").strip()
    got = False
    if ben and not any(j in ben for j in _OCR_JUNK):
        ev["benefits"] = ben
        ev["remarks"] = None
        got = True
    cond = (res.get("conditions") or "").strip()
    if cond and not any(j in cond for j in _OCR_JUNK):
        ev["conditions"] = cond
    for k in ("acct_pension", "acct_irp", "acct_dc"):
        if res.get(k):
            ev[k] = True
    if res.get("acct_etc") and not ev.get("acct_etc"):
        ev["acct_etc"] = res["acct_etc"]
    return got


def _resolve_banner(ev):
    """OCR 대상 배너 이미지 URL 확보. 스크레이퍼가 준 _image_url 우선,
    없으면 상세 페이지를 받아 가장 그럴듯한 배너 이미지를 고른다."""
    if ev.get("_image_url"):
        return ev["_image_url"]
    url = ev.get("_content_url") or ev.get("event_url") or ""
    if not url.startswith("http") or _is_list_url(url):
        return None
    try:
        from urllib.parse import urlparse
        from bs4 import BeautifulSoup
        from .scrapers.static_generic import fetch_html
        soup = BeautifulSoup(fetch_html(url, retries=1), "html.parser")
        import re as _re
        for img in soup.find_all("img"):
            src = img.get("src") or ""
            if not src or _re.search(r"(logo|icon|btn|bullet|sprite|blank|dot|arrow|nav_)", src, _re.I):
                continue
            if _re.search(r"(cmd=down|/event/|fileUpload|mlist|/public/mw/event|upload\.file)", src, _re.I):
                if src.startswith("/"):
                    p = urlparse(url)
                    src = f"{p.scheme}://{p.netloc}{src}"
                return src if src.startswith("http") else None
    except Exception as e:
        print(f"[배너] 해상 실패 {ev['event_name'][:24]}: {type(e).__name__}")
    return None


def enrich_structured(pension):
    """모든 이벤트의 조건/혜택을 Gemini로 '동일 표준 형식'으로 구조화(형식·가독성 통일).
    - 상세 본문 텍스트가 충분하면 텍스트로 구조화(저비용), 빈약하면 상세 이미지 OCR.
    - GEMINI_API_KEY 없으면 휴리스틱(classify_all) 값을 그대로 유지."""
    import time
    from . import vision

    if not vision.enabled():
        print("[구조화] GEMINI 미설정 → 휴리스틱 값 유지")
        return
    started = time.monotonic()
    n = 0
    for ev in pension:
        if n >= STRUCT_BUDGET or time.monotonic() - started > OCR_TIME_BUDGET:
            print(f"[구조화] 예산 도달 — {n}건 후 중단")
            break
        text = (ev.get("_detail_text") or "").strip()
        res = {}
        # 1) 본문 텍스트 우선 (이미지 다운로드 불필요 → 저비용)
        if len(text) >= _TEXT_MIN:
            if n:
                time.sleep(OCR_PACE_SEC)
            n += 1
            res = vision.extract_from_text(text, hint=ev["event_name"])
        # 2) 본문이 빈약하거나 혜택을 못 얻으면 상세 이미지 OCR
        if not (res.get("benefits") or "").strip():
            img = ev.get("_image_url") or _resolve_banner(ev)
            if img and n < STRUCT_BUDGET and time.monotonic() - started <= OCR_TIME_BUDGET:
                if n:
                    time.sleep(OCR_PACE_SEC)
                n += 1
                r2 = vision.extract(img, referer=ev.get("event_url") or "", hint=ev["event_name"])
                if (r2.get("benefits") or "").strip():
                    res = r2
        _apply_structured(ev, res)
    print(f"[구조화] Gemini {n}건 호출 (텍스트/이미지 통일 포맷)")


REVERIFY_OCR_BUDGET = 24      # 재검증 단계 OCR 호출 상한 (신규/변경 건 한정)


def _fresh_detail(ev):
    """이벤트의 실제 상세 페이지에서 (본문텍스트, OCR대상 이미지URL)을 새로 읽는다.
    웹검색 미사용 — 증권사별 실제 페이지/엔드포인트만 사용. 실패 시 ('', None)."""
    from bs4 import BeautifulSoup
    from .scrapers.static_generic import fetch_html
    from .scrapers import nhqv, koreainvestment
    firm, url = ev["firm_name"], (ev.get("event_url") or "")
    if firm == "NH투자증권":
        import re as _re
        m = _re.search(r"mNo=(\d+)", url)
        if not m:
            return "", None
        return nhqv.content_to_text_image(nhqv.fetch_content_html(m.group(1)))
    if firm == "한국투자증권" and ev.get("_detail_id"):
        return (koreainvestment.fetch_detail_text(ev["_detail_id"]) or ""), None
    if not url.startswith("http") or _is_list_url(url):
        return "", None
    soup = BeautifulSoup(fetch_html(url, retries=2), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True), _img_from_html(soup, url)


def reverify_new_changed(pension, existing):
    """신규/변경 건을 실제 상세 페이지에서 다시 읽어 조건/혜택을 재추출·덮어쓴다.
    (정책: 웹검색 미사용, 실제 페이지 접속 확인. 재추출 실패/빈값이면 1차 값 유지 + '검토 필요' 플래그.)"""
    import time
    from . import vision
    from .classify import extract_details

    by_key = {(e["firm_name"], e["event_name"], e.get("start_date")): e for e in existing}
    started, n_ocr, n_chk = time.monotonic(), 0, 0
    for ev in pension:
        key = (ev["firm_name"], ev["event_name"], ev.get("start_date"))
        old = by_key.get(key)
        if old is not None and old.get("content_hash") == content_hash(ev):
            continue                               # 미변경 → 재검증 불필요
        n_chk += 1
        try:
            text, image = _fresh_detail(ev)
        except Exception as e:
            print(f"[재검증] 페이지 접속 실패 {ev['firm_name']} {ev['event_name'][:24]}: {type(e).__name__}")
            ev["remarks"] = "재검증 실패(상세 페이지 접속 불가) — 원문 확인 필요"
            continue
        # enrich_structured 가 이미 실제-페이지 데이터로 구조화했으면(혜택 있음) 그 값을
        # 인정하고 재호출하지 않는다(쿼터 절약). 비어 있는 건만 신선한 본문/이미지로 재구조화.
        if ev.get("benefits"):
            ev["remarks"] = None
            continue
        res = {}
        if vision.enabled() and n_ocr < REVERIFY_OCR_BUDGET \
                and time.monotonic() - started < OCR_TIME_BUDGET:
            if len((text or "").strip()) >= _TEXT_MIN:
                if n_ocr:
                    time.sleep(OCR_PACE_SEC)
                n_ocr += 1
                res = vision.extract_from_text(text, hint=ev["event_name"])
            if not (res.get("benefits") or "").strip() and image and n_ocr < REVERIFY_OCR_BUDGET:
                if n_ocr:
                    time.sleep(OCR_PACE_SEC)
                n_ocr += 1
                r2 = vision.extract(image, referer=ev.get("event_url") or "", hint=ev["event_name"])
                if (r2.get("benefits") or "").strip():
                    res = r2
        if not (res.get("benefits") or "").strip() and not vision.enabled():
            det = extract_details(text)                # 폴백: Gemini 미사용 시 휴리스틱
            res = {"benefits": det["benefits"], "conditions": det["conditions"]}
        if _apply_structured(ev, res):
            ev["remarks"] = None
        elif not ev.get("benefits"):
            ev["remarks"] = "재검증 실패(상세 본문/이미지에서 혜택 미확인) — 원문 확인 필요"
    if n_chk:
        print(f"[재검증] 신규/변경 {n_chk}건 확인 (보강 Gemini {n_ocr}건)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()

    events, failed = asyncio.run(collect())
    pension = classify_all(events)
    enrich_structured(pension)        # 모든 이벤트를 Gemini로 동일 표준 형식 구조화(형식 통일)
    # 신규/변경 건은 실제 상세 페이지에서 재구조화·덮어쓰기로 재검증(잘못된 데이터 적재 방지).
    existing = db.fetch_all_events() if db.enabled() else []
    reverify_new_changed(pension, existing)
    # 본문·구조화·재검증 모두 실패한 경우에만 목록 요약을 혜택 폴백으로 사용.
    for ev in pension:
        if not ev.get("benefits") and ev.get("_benefits_hint"):
            ev["benefits"] = ev["_benefits_hint"]
            ev["remarks"] = None
    pension = finalize(pension)       # content_hash + 내부키 제거
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
    # DB 테이블(pension_events) 전체를 xlsx 로 첨부. DB 미사용 시 이번 수집분으로 대체.
    attachments = []
    try:
        table_rows = db.fetch_all_events() if db.enabled() else pension
        xlsx = report_mod.build_xlsx(table_rows)
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
