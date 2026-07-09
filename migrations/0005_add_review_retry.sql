-- 0005: 검토 플래그 이벤트의 재시도 상한 (작업지시서 M4)
--
-- needs_review=True 인 이벤트는 캐시가 영영 미스라 매 배치 LLM 재추출을
-- 유발한다. 그런데 상당수 플래그(G6 요일 불일치=원문 오타, G8 커버리지=LLM
-- 반복 실패 등)는 같은 원문이면 매번 재발한다 → 예산(STRUCT_BUDGET)만 잠식.
-- 같은 원문·스키마에 대해 REVIEW_RETRY_LIMIT(3)회 초과 실패 시 캐시 재사용을
-- 허용하되 needs_review 플래그는 유지한다 — 플래그는 살리고 비용은 끊는다.
--
-- review_retry_key 가 필요한 이유: source_content_hash/extract_schema_version
-- 은 rows_fresh(추출 성공) 일 때만 DB에 기록되므로(B1), 추출 실패 이벤트는
-- 그 값이 stale/NULL 이다. 카운터가 '무엇에 대한 실패 횟수'인지 식별하려면
-- 실패 당시의 원문해시:스키마버전을 함께 저장해야 한다. 키가 달라지면(원문
-- 변경·스키마 상향) 이전 카운터는 무효 → 재시도가 자동 재개된다.

ALTER TABLE public.pension_events
  ADD COLUMN IF NOT EXISTS review_retry_count integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS review_retry_key   text;

COMMENT ON COLUMN public.pension_events.review_retry_count IS
  '같은 원문·스키마(review_retry_key)에 대한 재추출 실패(플래그 잔존) 횟수. 상한 초과 시 캐시 허용';
COMMENT ON COLUMN public.pension_events.review_retry_key IS
  '실패 카운트의 대상 식별자 "{source_content_hash}:{extract_schema_version}". 불일치 시 카운터 무효(재시도 재개)';
