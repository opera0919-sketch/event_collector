#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""공통 설정: 대상 증권사, 연금 키워드, 환경변수."""

import os

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

FIRMS = [
    "미래에셋증권", "한국투자증권", "삼성증권", "키움증권", "KB증권", "NH투자증권",
]

# 연금 이벤트 판별 키워드 (youtube_pension_db.py 의 PENSION_KEYWORDS 보강)
PENSION_KEYWORDS = [
    "연금", "퇴직연금", "개인연금", "연금저축", "연저", "IRP", "irp",
    "DC형", "디폴트옵션", "연금계좌", "연금수령", "과세이연", "TDF",
    "노후", "은퇴",
]

# 대상계좌 판별 키워드
ACCT_PENSION_KW = ["연금저축", "연저"]
ACCT_IRP_KW = ["IRP", "irp", "퇴직연금계좌", "개인형퇴직연금"]
ACCT_DC_KW = ["DC", "확정기여"]
ACCT_ETC_KW = ["ISA", "isa", "CMA"]

# 환경변수 (없으면 해당 단계 스킵)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
MAIL_SENDER = os.environ.get("MAIL_SENDER", "")
MAIL_APP_PASSWORD = os.environ.get("MAIL_APP_PASSWORD", "")
MAIL_RECIPIENTS = [
    x.strip() for x in os.environ.get("MAIL_RECIPIENTS", "opera0919@gmail.com").split(",")
    if x.strip()
]
TRIGGER_TYPE = os.environ.get("TRIGGER_TYPE", "manual")  # weekly | manual
