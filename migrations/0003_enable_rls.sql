-- 0003: public 노출 테이블 RLS 활성화 (Supabase 보안 advisor: rls_disabled_in_public)
-- 적용: Supabase apply_migration.
--
-- 배경: 수집 파이프라인(pension_monitor)은 service_role 키로만 접근하며,
-- service_role 은 RLS 를 우회한다. 형제 테이블(pension_events / event_changes /
-- monitoring_runs)은 이미 RLS on + 정책 없음(= anon/authenticated 전면 차단) 상태다.
-- 아래 세 테이블만 RLS 가 꺼져 anon 키로 전건 읽기/수정이 가능했으므로 동일 패턴으로 잠근다.
-- 정책을 추가하지 않으므로 anon/authenticated 에는 전면 차단, 파이프라인(service_role)만 접근.

ALTER TABLE public.event_conditions              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_benefits               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pension_events_bak_conditions ENABLE ROW LEVEL SECURITY;
