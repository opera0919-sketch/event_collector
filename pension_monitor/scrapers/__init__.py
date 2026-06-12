# -*- coding: utf-8 -*-
from . import miraeasset, koreainvestment, samsungpop, kiwoom, kbsec, nhqv

# (증권사명, 수집함수(browser) -> list[dict], playwright 필요 여부)
SCRAPERS = [
    ("미래에셋증권", miraeasset.scrape, True),
    ("한국투자증권", koreainvestment.scrape, False),
    ("삼성증권", samsungpop.scrape, True),
    ("키움증권", kiwoom.scrape, True),  # 정적 우선이지만 모바일 렌더 폴백에 browser 필요
    ("KB증권", kbsec.scrape, True),
    ("NH투자증권", nhqv.scrape, True),
]
