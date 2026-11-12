-- 0002: pension_events `conditions` 자유 텍스트 → 타입드 컬럼 분해
-- (작업지시서 conditions_restructure_workorder.md §2)
-- 적용: Supabase apply_migration. 실행 전 백업 테이블 생성 필수.

-- 사전 백업 (감사·롤백 대비, 재실행 안전)
CREATE TABLE IF NOT EXISTS public.pension_events_bak_conditions AS
SELECT * FROM public.pension_events;

-- Data API 권한 (2026-10-30 부터 public 신규 테이블은 자동 부여되지 않는다).
-- 감사·롤백 전용 백업이라 service_role 만 준다 — 익명/로그인 사용자에게 과거 스냅샷을
-- 노출할 이유가 없다. 재실행해도 무해하다.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.pension_events_bak_conditions TO service_role;

-- 컬럼 추가. 불리언 기본값은 NULL(미확인) — 문구가 확인될 때만 true/false.
ALTER TABLE public.pension_events
  ADD COLUMN IF NOT EXISTS eligibility                 text,
  ADD COLUMN IF NOT EXISTS exclusions                  text,
  ADD COLUMN IF NOT EXISTS apply_required              boolean,
  ADD COLUMN IF NOT EXISTS marketing_consent_required  boolean,
  ADD COLUMN IF NOT EXISTS annual_cap_krw              integer,
  ADD COLUMN IF NOT EXISTS hold_condition              text,
  ADD COLUMN IF NOT EXISTS cond_notes                  text;

COMMENT ON COLUMN public.pension_events.hold_condition IS
  '유지조건 원문(날짜/비날짜 단일값). 예: "2026.06.30까지 잔고 유지", "혜택 지급 시점까지"';
COMMENT ON COLUMN public.pension_events.annual_cap_krw IS
  '연간 혜택 한도(KRW). 퇴직연금 감독규정 연 3만원 → 30000';
