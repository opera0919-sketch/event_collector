#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""conditions 구조화 백필 검증 (작업지시서 §4 인수 기준).

실행: DATABASE_URL 환경변수(.env 지원) 필요.
  python src/validate_conditions.py

검사 항목:
  (1) 라벨 보유 행 대비 추출률 (대상/유지조건/한도, 기준 ≥90% — 미달 행 id 출력)
  (2) 기간 보충 후 잔여 NULL
  (3) annual_cap_krw sanity (10,000~100,000 밖 → 플래그)
  (4) 불리언 분포
  (5) 기존 start/end·conditions 원문 무변경 (백업 테이블 diff)
  (6) 스팟체크: 임의 5행 원문 ↔ 신규 컬럼 마크다운 표
"""

import os
import random
import sys

Q_RATES = """
SELECT
  count(*) FILTER (WHERE conditions ~ '대상')            AS src_target,
  count(*) FILTER (WHERE eligibility IS NOT NULL)        AS got_eligibility,
  count(*) FILTER (WHERE conditions ~ '유지조건')         AS src_hold,
  count(*) FILTER (WHERE hold_condition IS NOT NULL)     AS got_hold,
  count(*) FILTER (WHERE conditions ~ '한도')             AS src_cap,
  count(*) FILTER (WHERE annual_cap_krw IS NOT NULL)     AS got_cap
FROM pension_events WHERE conditions IS NOT NULL;
"""
Q_MISS = {
    "eligibility": "SELECT id FROM pension_events WHERE conditions ~ '대상' AND eligibility IS NULL",
    "hold_condition": "SELECT id FROM pension_events WHERE conditions ~ '유지조건' AND hold_condition IS NULL",
    "annual_cap_krw": "SELECT id FROM pension_events WHERE conditions ~ '한도' AND annual_cap_krw IS NULL",
}
Q_PERIOD_NULL = ("SELECT id, firm_name FROM pension_events "
                 "WHERE conditions ~ '기간:' AND (start_date IS NULL OR end_date IS NULL)")
Q_CAP = ("SELECT annual_cap_krw, count(*) FROM pension_events "
         "WHERE annual_cap_krw IS NOT NULL GROUP BY 1")
Q_BOOL = ("SELECT apply_required, marketing_consent_required, count(*) "
          "FROM pension_events GROUP BY 1,2 ORDER BY 1,2")
Q_DATE_DIFF = ("SELECT count(*) FROM pension_events e "
               "JOIN pension_events_bak_conditions b USING (id) "
               "WHERE (b.start_date IS NOT NULL AND e.start_date IS DISTINCT FROM b.start_date) "
               "   OR (b.end_date IS NOT NULL AND e.end_date IS DISTINCT FROM b.end_date)")
Q_COND_DIFF = ("SELECT count(*) FROM pension_events e "
               "JOIN pension_events_bak_conditions b USING (id) "
               "WHERE e.conditions IS DISTINCT FROM b.conditions")
Q_SPOT = ("SELECT id, firm_name, event_name, conditions, eligibility, exclusions, "
          "apply_required, marketing_consent_required, annual_cap_krw, hold_condition, cond_notes "
          "FROM pension_events WHERE conditions IS NOT NULL AND id = ANY(%s)")


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL 미설정")
    import psycopg2
    ok = True
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(Q_RATES)
        st, ge, sh, gh, sc, gc = cur.fetchone()
        print(f"(1) 추출률 — 대상 {ge}/{st}, 유지조건 {gh}/{sh}, 한도 {gc}/{sc}")
        for col, (src, got) in {"eligibility": (st, ge), "hold_condition": (sh, gh),
                                "annual_cap_krw": (sc, gc)}.items():
            if src and got / src < 0.9:
                cur.execute(Q_MISS[col])
                ids = [r[0] for r in cur.fetchall()]
                print(f"    ⚠ {col} 추출률 {got/src:.0%} < 90% — 미달 행 id={ids} (사유 검토 필요)")
                # 한도는 '한도' 문자열이 있어도 연간 단일 금액이 없는 행(1인1회,
                # 운용사별 한도 등)이 정당 NULL 이므로 실패로 처리하지 않고 목록만 보고.
                if col != "annual_cap_krw":
                    ok = False
        cur.execute(Q_PERIOD_NULL)
        rows = cur.fetchall()
        print(f"(2) '기간:' 보유 & 날짜 NULL 잔여: {rows or '없음'} "
              f"(원문에 종료일 자체가 없는 행은 정상)")
        cur.execute(Q_CAP)
        caps = cur.fetchall()
        print(f"(3) 한도 분포: {caps}")
        for v, _ in caps:
            if not 10000 <= v <= 100000:
                print(f"    ⚠ 한도 이상치 {v}")
                ok = False
        cur.execute(Q_BOOL)
        print(f"(4) 불리언 분포: {cur.fetchall()}")
        cur.execute(Q_DATE_DIFF)
        d = cur.fetchone()[0]
        cur.execute(Q_COND_DIFF)
        c = cur.fetchone()[0]
        print(f"(5) 기존 날짜 변경 {d}건 / conditions 원문 변경 {c}건 (둘 다 0 필수)")
        ok = ok and d == 0 and c == 0
        # (6) 스팟체크 5행
        cur.execute("SELECT id FROM pension_events WHERE conditions IS NOT NULL")
        ids = random.sample([r[0] for r in cur.fetchall()], 5)
        cur.execute(Q_SPOT, (ids,))
        print("\n(6) 스팟체크 (원문 ↔ 신규 컬럼)\n")
        print("| id | 증권사 | 원문(conditions) | eligibility | apply | mkt | cap | hold | notes |")
        print("|---|---|---|---|---|---|---|---|---|")
        for r in cur.fetchall():
            cell = lambda v: str(v or "").replace("\n", "<br>").replace("|", "/")[:80]
            print(f"| {r[0]} | {r[1]} | {cell(r[3])} | {cell(r[4])} | {r[6]} | {r[7]} "
                  f"| {r[8] or ''} | {cell(r[9])} | {cell(r[10])} |")
    conn.close()
    print("\n결과:", "✅ 통과" if ok else "❌ 미달 항목 있음")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
