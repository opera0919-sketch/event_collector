-- 0006: event_changes.event_id 커버링 인덱스 (P0)
-- Supabase performance advisor: unindexed_foreign_keys (event_changes_event_id_fkey).
-- 이벤트별 변경 이력 조회(리포트 diff 분석·churn 진단 쿼리)가 event_id 로 필터하므로
-- FK 컬럼에 인덱스를 둔다. 재실행 안전.

CREATE INDEX IF NOT EXISTS idx_event_changes_event
  ON public.event_changes (event_id, detected_at DESC);
