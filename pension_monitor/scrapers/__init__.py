# -*- coding: utf-8 -*-
from . import miraeasset, koreainvestment, samsungpop, kbsec, nhqv

# (증권사명, 수집함수(browser) -> list[dict], playwright 필요 여부)
# 키움증권은 WAF(eversafe) 상시 차단으로 수집 대상에서 제외 (config.FIRMS 참조)
SCRAPERS = [
    ("미래에셋증권", miraeasset.scrape, True),
    ("한국투자증권", koreainvestment.scrape, False),
    ("삼성증권", samsungpop.scrape, True),
    ("KB증권", kbsec.scrape, True),
    ("NH투자증권", nhqv.scrape, False),  # eventList.json(상세 본문 포함) requests 수집
]
