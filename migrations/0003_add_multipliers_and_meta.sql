-- 0003: 배수(승수) 자식 테이블 + 배수/재추출 메타 컬럼
-- (event_collector 수집 정확성 개선 제안서 §P3)
-- 적용: Supabase apply_migration. 재실행 안전(IF NOT EXISTS).
--
-- 핵심: scope 분리. KB 시즌3처럼 '실적 N배 인정(인정금액)'과 '혜택 N배 지급
-- (리워드금액)'이 공존하는 케이스를, 한 컬럼에 뭉개면 두 배수의 곱(실이전 1억
-- → 인정 2억 → 70만 × 혜택2배 = 140만원)을 재현할 수 없다.

-- 1) 배수 자식 테이블 (신규)
CREATE TABLE IF NOT EXISTS public.pension_event_multipliers (
  id                bigserial PRIMARY KEY,
  event_id          bigint NOT NULL REFERENCES public.pension_events(id) ON DELETE CASCADE,
  source_type       text NOT NULL,   -- 타사이전 | 타사ISA만기전환 | 당사ISA만기전환
                                      -- | 퇴직금입금 | 개인납입 | 비대면최초신규 | 기타
  multiplier        numeric(4,2) NOT NULL,
  scope             text NOT NULL,   -- 인정금액(실적배수) | 리워드금액(혜택배수)
  min_threshold_krw bigint DEFAULT 0,
  extra_condition   text,
  source            text,            -- llm-text | llm-ocr | manual
  UNIQUE (event_id, source_type, scope, extra_condition)
);

COMMENT ON TABLE public.pension_event_multipliers IS
  '이벤트별 순입금/실적/혜택 인정 배수. scope 로 인정금액(실적)·리워드금액(혜택)을 구분';

-- 2) pension_events 추가 컬럼
ALTER TABLE public.pension_events
  ADD COLUMN IF NOT EXISTS stackable              boolean,   -- 이벤트 내 복수 혜택 중복 수령 가능
  ADD COLUMN IF NOT EXISTS annual_claim_limit     integer,   -- 연간 수령 횟수 제한 (삼성=1)
  ADD COLUMN IF NOT EXISTS source_content_hash    text,      -- 재추출 트리거(원문 해시)
  ADD COLUMN IF NOT EXISTS extract_schema_version integer,   -- 스키마 개선 시 전건 재추출
  ADD COLUMN IF NOT EXISTS close_reason           text;      -- expired|missed|firm_excluded|manual

COMMENT ON COLUMN public.pension_events.stackable IS
  '이벤트 내 복수 혜택(신규+순입금+순매수 등) 중복 수령 가능 여부';
COMMENT ON COLUMN public.pension_events.source_content_hash IS
  '상세 본문/이미지(LLM 입력) 해시. 조항이 바뀌면 캐시 무효화 → 재추출 (누락 영구화 방지)';
COMMENT ON COLUMN public.pension_events.extract_schema_version IS
  '추출 스키마/프롬프트 버전. vision.EXTRACT_SCHEMA_VERSION 상향 시 전건 재추출';
COMMENT ON COLUMN public.pension_events.close_reason IS
  '종료 사유: expired(만기) | missed(연속 미노출) | firm_excluded(수집제외) | manual';
