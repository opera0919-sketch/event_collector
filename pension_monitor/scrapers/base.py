# -*- coding: utf-8 -*-
"""스크레이퍼 공통 유틸 (Playwright 페이지 로드 + 범용 추출)."""

import asyncio
from urllib.parse import urlparse

from ..config import UA

# 모바일 UA — 데스크톱 UA 로 열었을 때 앱 설치 페이지 등으로 밀려난 경우의 2차 시도용.
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

# ── 수집 실패 사유 (B2) ───────────────────────────────────────────────────
# 0건/예외를 한 덩어리 '실패'로만 기록하면 원인을 로그 전문에서 찾아야 한다
# (운영 2026-10~11: 미래에셋 실패가 '껍데기만 렌더'와 '앱 스토어로 이동' 두 가지였는데
#  요약에는 둘 다 firms_failed 한 줄이었다). 스크레이퍼가 아는 만큼 사유를 남긴다.
#   redirected    : 목록 페이지가 다른 도메인으로 이동(또는 이동 시도)
#   empty_shell   : 페이지 틀만 있고 본문이 비어 있음 (지연 로드 미완)
#   selector_zero : 본문은 있는데 목록 셀렉터에 맞는 항목이 0건 (마크업 변경 의심)
#   timeout / error : 예외 (collect 가 예외 종류로 분류)
FAIL_REASONS = {}


def note_failure(firm, reason):
    FAIL_REASONS[firm] = reason


def clear_failure(firm):
    FAIL_REASONS.pop(firm, None)


def _same_site(url, host):
    h = (urlparse(url).hostname or "").lower()
    host = host.lower()
    return h == host or h.endswith("." + host)


def offsite_attempts(page):
    """load_page(stay_on_host=…) 가 막은 타 도메인 이동 URL 목록."""
    return list(getattr(page, "_pm_offsite", []) or [])


async def classify_empty(page, host):
    """목록이 0건일 때의 사유 분류 (redirected / empty_shell / selector_zero)."""
    try:
        if offsite_attempts(page) or (page.url.startswith("http")
                                      and not _same_site(page.url, host)):
            return "redirected"
        body = await page.inner_text("body")
        return "empty_shell" if len(" ".join(body.split())) < 200 else "selector_zero"
    except Exception:
        return "error"

# 날짜 패턴을 포함한 가장 안쪽 요소 + 인접 앵커를 수집하는 범용 추출기
JS_GENERIC_ITEMS = r"""
() => {
  const dateRe = /\d{4}[.\-\/년]\s?\d{1,2}[.\-\/월]\s?\d{1,2}/;
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('li, tr, dl, div, p, a')) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 400 || !dateRe.test(t)) continue;
    let inner = false;
    for (const c of el.children) {
      const ct = (c.innerText || '').trim().replace(/\s+/g, ' ');
      if (ct && dateRe.test(ct) && ct.length >= t.length * 0.8) { inner = true; break; }
    }
    if (inner) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    const a = el.closest('a[href]') || el.querySelector('a[href]');
    out.push({ text: t, href: a ? a.href : null,
               onclick: a ? (a.getAttribute('onclick') || '') : '' });
    if (out.length >= 60) break;
  }
  return out;
}
"""


async def load_page(browser, url, wait_ms=6000, retries=3, timeout_ms=30000,
                    wait_selector=None, selector_timeout_ms=30000,
                    stay_on_host=None, user_agent=None):
    """간헐 지연/거부 대응: commit 까지만 기다린 뒤 대기, 실패 시 재시도.

    성공 시 page 반환(호출자가 close), 모두 실패 시 마지막 예외 raise.

    wait_selector : 주어지면 고정 대기 대신 이 셀렉터가 나타날 때까지(최대
        selector_timeout_ms) 기다린 뒤 wait_ms 만큼만 더 둔다. 지연 로드 목록이 고정 대기
        안에 안 들어와 0건이 되던 문제에 대한 대응이며, 일찍 뜨면 일찍 끝난다. 셀렉터가
        끝내 안 나타나도 예외를 내지 않는다 — 호출자가 0건과 사유를 판정한다.
    stay_on_host  : 주어지면 메인 프레임이 이 호스트(와 하위 도메인) 밖으로 이동하려는
        요청을 막고 기록한다(offsite_attempts). 스크립트가 앱 설치 페이지 등으로 화면을
        넘겨 버려도 원래 문서가 남아 목록을 읽을 수 있다. iframe·리소스 요청은 건드리지 않는다.
    """
    last = None
    for attempt in range(retries):
        page = await browser.new_page(user_agent=user_agent or UA, locale="ko-KR")
        try:
            if stay_on_host:
                page._pm_offsite = []
                await page.route("**/*", _make_offsite_guard(page, stay_on_host))
            await page.goto(url, wait_until="commit", timeout=timeout_ms)
            # 서버가 문서 요청 자체를 다른 도메인으로 돌려보낸 경우(302)에는 가드가 개입할
            # 틈이 없고 기다려도 목록이 뜨지 않는다 — 셀렉터 대기를 건너뛰어 시간을 아낀다.
            landed_offsite = bool(stay_on_host) and page.url.startswith("http") \
                and not _same_site(page.url, stay_on_host)
            if wait_selector and not landed_offsite:
                try:
                    await page.wait_for_selector(wait_selector, state="attached",
                                                 timeout=selector_timeout_ms)
                except Exception:
                    pass
            await page.wait_for_timeout(wait_ms)
            return page
        except Exception as e:
            last = e
            blocked = offsite_attempts(page)
            await page.close()
            if blocked:
                # 첫 문서 요청부터 타 도메인으로 넘어갔다 — 다시 해도 같으므로 재시도하지 않는다
                raise OffsiteRedirect(blocked[0]) from e
            await asyncio.sleep(2 ** attempt)
    raise last


class OffsiteRedirect(Exception):
    """목록 URL 이 문서 로드 단계에서 다른 도메인으로 넘어감 (서버 리다이렉트 등)."""


def _make_offsite_guard(page, host):
    async def guard(route, request):
        try:
            if request.is_navigation_request() and request.url.startswith("http"):
                try:
                    is_main = request.frame == page.main_frame
                except Exception:
                    is_main = True          # 프레임 생성 전의 이동 요청 = 최상위 문서
                if is_main and not _same_site(request.url, host):
                    page._pm_offsite.append(request.url)
                    # abort() 로 끊으면 Chromium 이 오류 페이지(chrome-error://)를 띄워 원래
                    # 문서가 사라진다(로컬 재현으로 확인). '204 No Content' 로 답하면 브라우저가
                    # 이동을 취소하고 현재 문서를 그대로 둔다.
                    await route.fulfill(status=204, body="")
                    return
        except Exception:
            pass
        try:
            await route.continue_()
        except Exception:
            pass                            # 페이지가 이미 닫힌 뒤 도착한 요청
    return guard


async def debug_dump(page, label, max_anchors=20):
    """파서가 0건일 때 구조 파악용 로그."""
    try:
        body = await page.inner_text("body")
        print(f"[debug:{label}] body({len(body)}자) 앞부분: {' '.join(body.split())[:600]}")
        anchors = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]'))
                 .filter(a => /event|evt|et0/i.test(a.getAttribute('href') || '')
                              || /이벤트/.test(a.innerText || ''))
                 .slice(0, 30)
                 .map(a => ({t: (a.innerText||'').trim().replace(/\\s+/g,' ').slice(0,100),
                             h: a.getAttribute('href'),
                             o: (a.getAttribute('onclick')||'').slice(0,100)}))""")
        for a in anchors[:max_anchors]:
            print(f"[debug:{label}] anchor: {a}")
        frames = [f.url for f in page.frames if f != page.main_frame]
        if frames:
            print(f"[debug:{label}] iframes: {frames}")
    except Exception as e:
        print(f"[debug:{label}] dump 실패: {e}")
