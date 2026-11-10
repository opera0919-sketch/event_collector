# -*- coding: utf-8 -*-
"""스크레이퍼 브라우저 검증 — 실제 Chromium 으로 로컬 가짜 사이트를 연다 (외부 접속 없음).

운영 로그에서 본 미래에셋 목록의 실패 모드를 로컬에서 재현하고, 보강한 수집 경로가
(1) 늦게 주입되는 목록을 기다리고 (2) 다른 도메인으로 화면이 넘어가는 것을 막아 목록을
읽으며 (3) 끝내 못 읽을 때는 사유를 분류해 남기는지 확인한다.

실행: python tests/test_scraper_browser.py   (playwright + chromium 필요, 없으면 건너뜀)
  PW_CHROMIUM=/path/to/chrome 로 브라우저 실행 파일을 지정할 수 있다.
'사이트'는 localhost, '다른 도메인'은 127.0.0.1 로 구분한다(호스트 이름이 다르면 타 사이트).
"""

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_LI = ("<li onclick=\\\"detailView('202609012','1','null','O')\\\"><a class='eventLst' href='#none'>"
       "<p class='subject'>「투자하는 연금」 2026년 개인연금 입금 이벤트</p>"
       "<span class='date'>2026.10.01 ~ 2026.12.31</span></a></li>"
       "<li onclick=\\\"detailView('202609014','1','null','O')\\\"><a class='eventLst' href='#none'>"
       "<p class='subject'>「투자하는 연금」2026년 IRP 입금 이벤트</p>"
       "<span class='date'>2026.10.01 ~ 2026.12.31</span></a></li>")
_SHELL = "<html><body><div>닫기 홈 미래에셋증권 닫기 로그인</div><ul id='l'></ul>%s</body></html>"
_STORE_TEXT = "google_logo Play 게임 앱 미래에셋증권 MAPS 설치 " + "앱 정보 다양한 금융상품 " * 60


def _inject(ms):
    return ("<script>setTimeout(function(){document.getElementById('l').innerHTML=\"%s\";},%d);"
            "</script>" % (_LI, ms))


def _pages(store_port):
    store = "http://127.0.0.1:%d/store" % store_port
    return {
        "/ok": _SHELL % _inject(300),
        "/slow": _SHELL % _inject(5000),                    # 짧은 고정 대기로는 놓치는 지연 주입
        "/jsredirect": _SHELL % (_inject(2500) +
                                 "<script>setTimeout(function(){location.href='%s';},600);</script>" % store),
        "/shell": _SHELL % "",                               # 끝내 목록이 안 뜸 (본문 37자 수준)
        "/nolist": _SHELL % ("<p>" + "이벤트 안내 문구 " * 60 + "</p>"),   # 본문은 있는데 목록 구조가 다름
        "/store": "<html><body>%s</body></html>" % _STORE_TEXT,
    }


def _serve(host, pages_ref):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/srvredirect":                       # 서버가 곧바로 다른 도메인으로 보냄
                self.send_response(302)
                self.send_header("Location", pages_ref["store_url"])
                self.end_headers()
                return
            body = pages_ref["pages"].get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer((host, 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run():
    from playwright.async_api import async_playwright
    from pension_monitor.scrapers import base, miraeasset as ma

    ref = {}
    site = _serve("localhost", ref)
    store = _serve("127.0.0.1", ref)
    ref["pages"] = _pages(store.server_address[1])
    ref["store_url"] = "http://127.0.0.1:%d/store" % store.server_address[1]
    root = "http://localhost:%d" % site.server_address[1]
    ma.SITE_HOST = "localhost"
    ma.SELECTOR_TIMEOUT_MS = 9000

    async with async_playwright() as pw:
        kw = {"executable_path": os.environ["PW_CHROMIUM"]} if os.environ.get("PW_CHROMIUM") else {}
        browser = await pw.chromium.launch(**kw)
        try:
            # 0) 재현 확인: 보호 장치 없이 열면 화면이 '스토어'로 넘어가 목록이 0건이 된다
            #    (운영 2026-11-06·09 로그의 상태 — 본문 전체가 앱 소개 문구)
            page = await base.load_page(browser, root + "/jsredirect", wait_ms=3500, retries=1)
            try:
                assert "127.0.0.1" in page.url, page.url
                assert await page.evaluate(ma.JS_LI_ITEMS) == []
                assert "Play" in await page.inner_text("body")
            finally:
                await page.close()
            print("OK 재현: 보호 없이 열면 타 도메인으로 넘어가 목록 0건")

            # 1) 정상
            got = await ma.scrape(browser, list_url=root + "/ok")
            assert len(got) == 2 and "cs_ecis_id=202609012" in got[0]["event_url"], got
            assert ma.FIRM not in base.FAIL_REASONS
            print("OK 정상 목록 2건")

            # 2) 지연 주입(5초): 셀렉터를 기다려 수집. 일찍 뜨면 일찍 끝난다(고정 대기 아님)
            got = await ma.scrape(browser, list_url=root + "/slow")
            assert len(got) == 2, got
            print("OK 지연 주입 목록 — 셀렉터 대기로 수집")

            # 3) 스크립트가 타 도메인으로 이동 시도 → 차단하고 원래 문서에서 목록 수집
            got = await ma.scrape(browser, list_url=root + "/jsredirect")
            assert len(got) == 2, got
            assert ma.FIRM not in base.FAIL_REASONS
            print("OK 타 도메인 이동 차단 후 목록 수집")

            # 4) 서버가 목록 URL 을 타 도메인으로 보냄 → 수집 불가, 사유 redirected
            got = await ma.scrape(browser, list_url=root + "/srvredirect")
            assert got == [] and base.FAIL_REASONS.get(ma.FIRM) == "redirected", base.FAIL_REASONS
            print("OK 서버 리다이렉트 → 사유 redirected")

            # 5) 껍데기만 렌더 → 사유 empty_shell
            ma.SELECTOR_TIMEOUT_MS = 2500
            got = await ma.scrape(browser, list_url=root + "/shell")
            assert got == [] and base.FAIL_REASONS.get(ma.FIRM) == "empty_shell", base.FAIL_REASONS
            print("OK 빈 화면 → 사유 empty_shell")

            # 6) 본문은 있는데 목록 구조가 다름 → 사유 selector_zero
            got = await ma.scrape(browser, list_url=root + "/nolist")
            assert got == [] and base.FAIL_REASONS.get(ma.FIRM) == "selector_zero", base.FAIL_REASONS
            print("OK 목록 구조 불일치 → 사유 selector_zero")

            # 7) 성공하면 직전 실패 사유가 지워진다 (재시도 패스에서 복구된 경우)
            got = await ma.scrape(browser, list_url=root + "/ok")
            assert len(got) == 2 and ma.FIRM not in base.FAIL_REASONS
            print("OK 복구 시 실패 사유 해제")
        finally:
            await browser.close()
            site.shutdown()
            store.shutdown()
    print("스크레이퍼 브라우저 검증 8종 통과")


if __name__ == "__main__":
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("SKIP playwright 미설치 — 브라우저 검증 건너뜀")
        sys.exit(0)
    asyncio.run(_run())
