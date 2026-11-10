#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""공통 설정: 대상 증권사, 연금 키워드, 환경변수."""

import datetime as _dt
import os

# 업무 기준일은 한국 시간이다. 러너는 UTC 라서 dt.date.today() 를 쓰면 실행 시각에 따라
# 날짜가 하루 어긋난다 (운영 실측: 22:30 UTC 예약이 지연 없이 돌던 8월에는 리포트가
# 일요일 날짜(2026-08-23 등)로 저장됐고, 지연이 커진 뒤에는 월요일 날짜가 됐다).
# 리포트 파일명·'종료' 판정·종료 임박 계산이 모두 이 날짜를 쓴다. 한국은 서머타임이 없다.
KST = _dt.timezone(_dt.timedelta(hours=9), "KST")


def today_kst() -> _dt.date:
    """한국 시간 기준 오늘 날짜 (러너 시간대와 무관)."""
    return _dt.datetime.now(KST).date()


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 키움증권은 WAF(eversafe) 상시 차단으로 수집 대상에서 제외 (2026-07 결정)
FIRMS = [
    "미래에셋증권", "한국투자증권", "삼성증권", "KB증권", "NH투자증권",
]

# 수집 불가(WAF 차단 등)로 FIRMS 에서 제외한 증권사. DB 에 과거 웹검색 수집분이
# 남아 있어도 '미노출 → 종료'로 자동 판정하지 않는다(수집 안 하는 회사를 '사라졌다'고
# 종료 처리하는 것은 오류 — E8). 대신 needs_review 로 수동 검증 대상 표기.
EXCLUDED_FIRMS = {"키움증권"}

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

# 환경변수 (빈 문자열로 주입돼도 기본값 적용)
# SUPABASE_URL 은 비밀값이 아니므로 프로젝트 기본값 내장, 키만 Secrets 필수
# .strip(): GitHub Secrets 에 값을 붙여넣을 때 끝에 개행/공백이 섞이면 그대로
# 헤더(Authorization: Bearer …\n)에 들어가 PostgREST 가 토큰을 거부(401)한다.
# 가장 흔한 인증 실패 원인이라 모든 자격증명을 양끝 공백 제거 후 사용한다.
SUPABASE_URL = ((os.environ.get("SUPABASE_URL") or "").strip()
                or "https://fbkriifozbwuaoegmmcf.supabase.co").rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
MAIL_SENDER = (os.environ.get("MAIL_SENDER") or "").strip()
MAIL_APP_PASSWORD = (os.environ.get("MAIL_APP_PASSWORD") or "").strip()
MAIL_RECIPIENTS = [
    x.strip()
    for x in (os.environ.get("MAIL_RECIPIENTS") or "opera0919@gmail.com").split(",")
    if x.strip()
]
# 값은 weekly | manual 두 가지뿐이다. 'weekly' 는 이름과 달리 "예약(schedule) 실행"을
# 뜻한다(현재 평일 매일). DB 의 monitoring_runs_trigger_type_check 제약이 이 두 값만
# 허용하므로, 이름을 바꾸려면 제약 마이그레이션이 먼저다 — 값은 그대로 둔다.
TRIGGER_TYPE = os.environ.get("TRIGGER_TYPE") or "manual"
# 증권사 수집이 이 횟수만큼 연속 실패하면 잡을 실패(빨강)로 끝낸다 (산출물·메일은 유지).
try:
    FAIL_STREAK_ALERT = max(1, int(os.environ.get("FAIL_STREAK_ALERT") or 2))
except ValueError:
    FAIL_STREAK_ALERT = 2
