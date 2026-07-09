-- 0004: 수집 제외 증권사(EXCLUDED_FIRMS)의 오종료 행 보정 (E8 잔여분)
-- db.sync 의 미노출 루프는 `status='종료'` 행을 건너뛰므로, 코드 수정만으로는
-- 이미 자동 종료된 과거 행이 복구되지 않는다. 1회성 보정.
--
-- 대상: 키움증권 — EverSafe WAF 상시 차단으로 수집 대상에서 제외되어 있으나
--       DB 에 과거 웹검색 수집분이 남아 missed_count >= 2 로 종료 처리됨.

UPDATE public.pension_events
SET    status        = '진행중',
       closed_at     = NULL,
       missed_count  = 0,
       close_reason  = NULL,
       needs_review  = true,
       review_reason = '수집 제외 증권사 — 수동 검증 필요'
WHERE  firm_name = '키움증권'
  AND  status = '종료'
  AND  (end_date IS NULL OR end_date >= CURRENT_DATE);

-- 웹검색 수집분(정책상 폐기된 소스)은 추출방식을 명시해 정상 수집분과 섞이지 않게 한다.
UPDATE public.pension_events
SET    extract_method = 'websearch'
WHERE  firm_name = '키움증권'
  AND  (remarks ILIKE '%검색 수집%' OR extract_method IS NULL);
