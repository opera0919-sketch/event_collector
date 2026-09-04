# 파이프라인 성능·구조 개선안 (2026-09) — 진단 근거와 설계

대상: `pension_monitor` 일일 배치 (GitHub Actions → 5개 증권사 수집 → Gemini 구조화 → Supabase → 리포트/메일)
근거 데이터: `data/events_latest.json` 커밋 스냅샷 30개(07-23~09-03), Supabase `event_changes`(551행)·`monitoring_runs`(59행), 코드(`pension_monitor/*`).

---

## 0. 한 페이지 요약

| # | 요구 | 진단 결론 | 핵심 해결책 |
|---|---|---|---|
| 1 | 문자열/OCR 결과가 매일 달라 반복 overwrite | churn 은 LLM 요동이 아니라 **"재추출을 매일 유발하는 입력 불안정"** 이 1차 원인. KB 5건은 30일 중 **30일 해시 변경**, 한투는 **상세 fetch 실패(빈 본문)** 가 "내용 변경"으로 오인됨. 그 위에 LLM 표현 요동이 그대로 DB 를 덮어씀 | (S1) fetch 실패를 상태로 분리 (S2) 본문 컨테이너 한정 해시 (S3) **사실 시그니처(fact signature) 비교 게이트** — 수치·티어·기간이 같으면 기존 문장 유지, 변경 로그 없음 (S4) 기간 스티키 규칙 (S5) 일일=변화감지, 재추출=해시 변경 시/주 1회 |
| 2 | KB증권: 모바일 배너 → 개별 이벤트 페이지 본문 fetch | 현행은 PC JSP 목록 → PC 상세 전체 페이지 텍스트(네비·타 이벤트·광고 포함) 를 OCR/해시 입력으로 사용. 모바일은 3차 폴백 | 모바일 우선 스크레이퍼: `linkcd=et*` 배너 → 개별 페이지 렌더 → **본문 컨테이너 텍스트 + 해당 이벤트 디자인 폴더 이미지 전부**. seq↔linkcd ID 매핑으로 기존 행 연속성 유지 |
| 3 | DB/코드 구조 최적화 | 마스터 테이블 1개에 정체성·기간·추출·검증·재시도 메타 45개 컬럼 혼재, 원문 스냅샷 부재(디버깅 불가), 상세 fetch 로직이 `main.py` 에 증권사별 분기로 산재, 이미지 선택기 3중 중복 | `event_snapshots`(원문)·`event_extractions`(LLM 산출 이력) 분리, 마스터 슬림화, `fetch_detail` 을 스크레이퍼로 이관, diff 계산 순수함수화 |
| 4 | 추가 개선 | 한투 상세 도메인 폴백 부재, 잔고유지기간을 이벤트 기간으로 오인, 리포트 '변경' 섹션이 노이즈, cron 은 매일인데 코드는 weekly, 리포트 md 매일 커밋(레포 비대), 의존성 파일 불일치 | 아래 §5 |

목표 지표(현재 → 목표): 일 평균 변경 로그 **9.3건 → ≤2건**, 캐시 적중률 **~55% → ≥90%**, Gemini 호출/일 **10~20 → ≤5**, 빈 본문 해시(`cbe5cfdf…`) 발생 **주 3~5회 → 0**.

---

## 1. 진단 근거

### 1.1 변경 로그는 매일 발생하지만 실제 이벤트 변동은 없다
`monitoring_runs` 최근 25회: 신규/종료는 대부분 0, `events_changed` 는 **4~15건/일**(평균 9.3). `event_changes` 551건 중 필드 분포:

| 필드 | 건수 | 이벤트 수 | null↔값 진동 | 비고 |
|---|---|---|---|---|
| conditions | 241 | 34 | 24 | 문장 표현 요동 |
| benefits | 157 | 33 | 6 | 문장 표현 요동 |
| end_date | 37 | 6 | **32** | 값↔NULL 진동 |
| start_date | 35 | 4 | **30** | 값↔NULL 진동 |
| acct_etc | 5 | 3 | 5 | |

- KB 「TDF & ETF 시즌3」 conditions 는 34회 변경에 **33개 서로 다른 값** — 같은 원문을 매번 다르게 요약.
- KB 「시즌3 연금저축」 benefits 25회 변경에 12개 값 — 동일 이미지에서 12가지 표현이 순환.
- KB 「ETF 수수료」 start/end 는 20회 진동, 값은 2종(`2025-12-29/2026-12-28` ↔ NULL, 가끔 `2026-01-01/12-31`).

### 1.2 재추출 트리거(`source_content_hash`) 가 매일 뒤집힌다 — 증권사별로 극단적 차이

| 이벤트 | 해시 변경 / 관측일 | LLM 재추출 성공일 |
|---|---|---|
| KB 10010044 (시즌3 연금저축) | **29/29** | 27 |
| KB 10010041 (TDF&ETF 시즌3) | **29/29** | 25 |
| KB 10010039 (시즌3 IRP) | **29/29** | 15 |
| KB 10009676 (DC 첫만남) | **29/29** | 23 |
| KB 10009370 (ETF 수수료) | **29/29** | 20 |
| 한투 6730 / 6137 / 6744 / 6743 / 6754 | 18/28 · 17/28 · 14/22 · 13/22 · 10/17 | 13 · 15 · 11 · 7 · 10 |
| 미래 4건 · NH 5건 · 삼성 3722 | **0** | 0~1 |
| 삼성 3867 / 3871 | 1/23 · 2/23 | 5 · 3 |

미래·NH·삼성은 07-20 의 해시 정규화 패치(`8b7d82c`) 이후 캐시가 정상 작동한다. **churn 은 KB·한투에 국한**되며, 두 회사의 공통점은 "상세 본문을 **페이지 전체 텍스트**(`soup.get_text` / `page.inner_text('body')`)로 가져오고, 이미지를 **페이지 전체**에서 고른다"는 점이다.

### 1.3 결정적 증거: 빈 본문 해시 `cbe5cfdf7c2118a9`
`sha256("|")[:16] == "cbe5cfdf7c2118a9"` — 즉 `_detail_text == ""` 이고 `_image_urls == []` 인 상태의 해시다. 이 값이 09-02 에 한투 6754 와 KB 10010041 두 이벤트에 동시에 기록됐고, 한투 6754 는 관측 18일 중 **11일**이 이 값이다.

발생 경로: `koreainvestment.fetch_detail_text` 는 예외를 삼켜 `""` 을 반환 → `enrich_details` 가 그대로 `_detail_text=""` 저장 → `normalize`:
1. 텍스트 길이 < `TEXT_MIN` → 텍스트 추출 생략, `_resolve_banner_images` 가 상세 페이지를 **다시** 요청해 페이지 전체 이미지로 OCR (두 번째 요청은 성공하기도 → `rows_fresh=True` 인데 해시는 빈 값).
2. 해시가 어제와 달라 캐시 미스 → 재추출 → 문장 교체.
3. 다음 날 fetch 가 성공하면 해시가 또 달라짐 → 재추출 → 문장 교체. **하루 실패가 이틀치 overwrite 를 만든다.**
4. KB 는 목록 날짜를 불신(`TRUSTED_LIST_DATES` 제외)하므로 본문이 비면 `reconcile_period` 가 `start/end=None` 으로 확정하고, `db.sync` 의 `_RAW_FIELDS` 는 None 도 그대로 덮어쓴다 → 09-02 「TDF&ETF 시즌3」 `2026-04-01/06-30 → NULL`, 09-03 `NULL → 2026-04-01/06-30`.

### 1.4 KB 는 fetch 가 성공해도 매일 다르다
KB 10010044 의 `image_url` 은 같은 디자인 폴더(`design/20260623165649/`) 안에서 `img_01 / img_04 / img_05` 를 오간다. `_imgs_from_html` 이 PC 상세 페이지 **전체**에서 `/img/|/images/|/event/` 패턴을 모아 URL 사전순 상위 3장을 고르므로, 페이지에 실린 **다른 이벤트/광고 배너가 바뀌면 상위 3장이 바뀐다**. 본문 텍스트도 전체 페이지라서 우측/하단의 "진행중 이벤트" 목록·공지·시세 등 휘발 요소가 해시에 섞인다. 07-20 패치는 "선택의 결정론"만 확보했고 **"후보 집합의 안정성"** 은 확보하지 못했다.

### 1.5 잔고유지기간 오인
KB 10010044 의 기간이 간헐적으로 `2026-10-01 ~ 2026-10-31` 로 기록된다. 이는 원문의 "잔고유지기간(26.10.1~10.31)" 로, 이벤트 기간이 아니라 **혜택 지급 조건**이다. `_PERIOD_NEAR` 정규식은 `(?<!유지)기간` 으로 방어하지만 LLM `period_start/end` 경로는 검증이 없다.

---

## 2. 문제 1 — 반복 overwrite 해결 설계

### S1. Fetch 결과를 "상태"로 다룬다 (실패 ≠ 내용 변경)
```python
@dataclass
class DetailContent:
    status: Literal["ok", "empty", "error", "blocked"]
    text: str            # 본문 컨테이너 텍스트 (S2)
    images: list[str]    # 본문 컨테이너 내부 이미지
    scope_hash: str      # S2 의 안정 해시
    fetched_at: str
```
- 스크레이퍼별 `fetch_detail(ev) -> DetailContent` (현재 `main.enrich_details` 의 증권사 분기 이관).
- `status != "ok"` 이면: 해시 갱신 금지, 재추출 금지, 기간/캐노니컬 변경 금지, `detail_fetch_failures += 1` 만 기록. 기존 DB 값을 그대로 재사용(현 G3 무회귀를 **fetch 단계로 앞당김**).
- `text` 가 200자 미만이고 이미지도 없으면 `empty` 로 분류 — 빈 값은 절대 해시에 들어가지 않는다.
- 한투 상세: `_DOMAINS` 폴백을 상세에도 적용, 재시도 3회 지수 백오프(현재 목록만 폴백).

### S2. 해시 입력을 "본문 컨테이너"로 한정 + 라인 정규화
| 증권사 | 본문 컨테이너 (실측/예상 — 배포 전 collect-test 로 확정) | 이미지 범위 |
|---|---|---|
| KB (모바일, §3) | 이벤트 상세 콘텐츠 영역 (`et*` 페이지 본문) | 해당 이벤트 `etcimg.kbsec.com/html/design/<folder>/` 폴더의 이미지 **전부** |
| 한투 | `Event.jsp?cmd=TF04gb010002` 의 게시글 본문 div | 본문 div 내부 img |
| 미래 | 상세 v01.do 본문 (`/public/mw/event/{ID}/` 이미지) | 동일 ID 폴더 |
| 삼성 | eventView 본문 | `cmd=down&…event.jpg` |
| NH | `mContent` (이미 컨테이너) | 이미 컨테이너 |

정규화 규칙(의미 보존): 공백 축약, 조회수/등록일/"오늘"/D-day 패턴 제거, 이미지 URL 은 쿼리스트링 제거 후 정렬. 여기에 **유사도 임계**를 둔다: `difflib.SequenceMatcher(old_text, new_text).ratio() ≥ 0.97` 이고 수치 토큰 집합이 동일하면 "동일 원문"으로 간주(사이트 템플릿 미세 변경 흡수). 원문(`text`)은 `event_snapshots` 에 저장해 다음 실행에서 비교 가능하게 한다(§4.1).

### S3. 사실 시그니처(fact signature) 게이트 — LLM 표현 요동 차단
재추출이 일어나더라도 **의미가 같으면 문장을 교체하지 않는다.**
```python
def fact_signature(ev) -> dict:
    return {
        "tiers": sorted((num_tokens(r.condition_text), num_tokens(r.benefit_text),
                         r.award_method, r.award_limit) for r in ev.benefit_rows),
        "conds": sorted((r.label, num_tokens(r.value_text)) for r in ev.condition_rows),
        "mults": sorted((m.source_type, m.multiplier, m.scope, m.min_threshold_krw) for m in ev.multiplier_rows),
        "period": (ev.start_date, ev.end_date),
        "flags": (ev.apply_required, ev.marketing_consent_required, ev.annual_cap_krw,
                  ev.stackable, ev.annual_claim_limit),
        "accts": (ev.acct_pension, ev.acct_irp, ev.acct_dc, norm(ev.acct_etc)),
    }
```
`num_tokens` 는 `1백만원/100만원/1,000,000원` 을 정수로 정규화(`_grounded` 의 토큰화 재사용·확장).
- `sig(new) == sig(old)` → **기존 캐노니컬 텍스트·자식 행 유지**, `last_verified_at` 만 갱신, 변경 로그 없음.
- 다르면 → 교체 + 변경 로그에 **시그니처 diff**(예: `티어 3→4`, `한도 30,000→50,000`) 기록. 리포트 '변경' 섹션은 이 diff 만 노출.
- 티어 수가 줄었거나 grounded 실패면 교체 대신 `needs_review` (현 G2/G3 유지).

LLM 측 보강: `generationConfig` 에 `seed` 고정 + `topK=1`(Gemini v1beta 지원), 프롬프트에 **이전 승인 추출(JSON)** 을 "참고 — 원문이 바뀌지 않았다면 동일하게 출력" 으로 제공(diff-aware 재추출). 표현 요동을 원천에서 줄인다.

### S4. 기간 스티키 규칙
- `db.sync` 에서 `start_date/end_date` 는 **새 값이 확정 출처(list/detail)** 일 때만 교체. LLM 출처(`llm`)·추론(`hold_inferred`)·None 은 기존 확정값을 덮지 못한다.
- 추출 기간이 본문의 잔고유지기간(`_HOLD_RE` 매치 구간)과 같으면 거부.
- 진행중 목록에 노출 중인데 `end_date < today` 이면 의심 플래그(현 KB 「TDF&ETF 시즌3」 `~06-30` 이 9월에도 진행중 — 실제로는 시즌 갱신 미반영 가능성).
- KB 목록 날짜 불신 정책은 **모바일 페이지 기간 표기 확인 후** 재평가(§3). 게시일(`idt`) 혼입은 PC 앵커 파싱 문제였다.

### S5. 스케줄 분리 — 매일 "변화 감지", 재추출은 "해시 변경 시 + 주 1회 검증"
- 일일 실행: 목록 수집 + `fetch_detail` + `scope_hash` 비교. 해시 동일 → LLM 0회, `last_seen_at` 갱신만. 신규/변경/종료만 메일.
- 재추출 조건: (a) `scope_hash` 변경, (b) 신규, (c) 주 1회(월) 전건 재검증 — 단 S3 게이트로 동일 시그니처는 무변경.
- 효과: Gemini 호출을 하루 최대 40회에서 실질 0~5회로, 무료 티어 RPD 여유 확보. `TRIGGER_TYPE` 을 `daily|weekly_verify|manual` 로 정정(현재 cron 은 월~금 매일인데 값은 `weekly`).

---

## 3. 문제 2 — KB증권 모바일 배너 → 개별 이벤트 페이지 수집

### 3.1 현행과 목표
| | 현행 | 목표 |
|---|---|---|
| 목록 | PC `CUST_09_0003.jsp` 정적 파싱(1차) → PC 렌더(2차) → 모바일 `m06020000`(3차) | **모바일 `m06020000` 1차** (사용자 지정 소스), PC 는 폴백 |
| 상세 URL | `www.kbsec.com/go.able?linkcd=s060902030000&seq=…&idt=…` | `m.kbsec.com/go.able?linkcd=et…` (배너별 개별 페이지) |
| 본문 | PC 상세 **전체 페이지** 텍스트 + 페이지 전체 이미지 상위 3장 | 개별 페이지 **본문 컨테이너** 텍스트 + 해당 이벤트 디자인 폴더 이미지 전부 |
| 기간 | 목록 불신 → 본문 정규식 → LLM → NULL | 개별 페이지 기간 표기(있으면 `detail` 출처) → 본문 정규식 → LLM |

### 3.2 스크레이퍼 설계 (`scrapers/kbsec.py` 재작성)
1. 목록: Playwright 로 `m06020000` 로드 → `a[href*='linkcd=et'], a[onclick*='linkcd=et']` 대기(현 `JS_ET_ANCHORS` 재사용) + 스크롤로 지연 로드 유도. 각 앵커에서 `linkcd`, 배너 `img.alt/src`, 인접 텍스트(제목·기간) 수집. 배너가 이미지뿐이면 제목은 `alt` → 없으면 개별 페이지 `<title>`/h1.
2. 상세: 각 `go.able?linkcd=etXXXX` 를 **같은 브라우저 컨텍스트**에서 렌더(세션 의존 가능성). 본문 컨테이너 셀렉터는 첫 배포 전 `collect-test` 워크플로에서 DOM 덤프로 확정(`scripts/dump_structure.py` 확장). 폴백: 가장 큰 `etcimg.kbsec.com/html/design/<folder>/` 이미지 묶음을 포함하는 최소 공통 조상.
3. 이미지: 같은 `<folder>` 의 `img_01…img_NN` 을 순서대로 전부 확보. `vision.MAX_IMAGES=3` 은 KB 다단 배너(실측 img_05, img_11 등 10장 이상)를 잘라 OCR 누락을 만든다 → KB 는 **세로 스티칭 1장**(Pillow) 또는 상한 8장으로 상향. 스티칭이 해상도/용량(7MB) 한도를 넘으면 2분할.
4. 텍스트: 컨테이너 `inner_text` + 이미지 `alt`. 텍스트 200자 이상이면 텍스트 추출 우선(현 규칙), 아니면 OCR.
5. 스크린샷 폴백(현 `_screenshot_b64`) 은 컨테이너 단위 `element.screenshot()` 으로 축소(전체 페이지 스크린샷은 네비 포함 → OCR 노이즈).

### 3.3 ID 연속성 (중복 행 방지)
- 기존 행의 `source_event_id` 는 PC `seq`. 모바일 `linkcd(et…)` 로 바꾸면 동일 이벤트가 신규로 잡힌다.
- `pension_events.source_ids jsonb` (예: `{"seq":"10010044","linkcd":"et0601..."}`) 추가, 매칭은 `seq` → `linkcd` → 정규화 제목 순. 첫 배치에서 모바일 제목과 PC 제목을 대조해 백필(1회성 스크립트, `--dry-run` 지원).
- PC 상세 URL 은 `event_url_pc` 로 보존(리포트 링크 호환), 노출 링크는 모바일.

### 3.4 검증 절차 (이 세션의 한계 명시)
이 세션 환경은 egress 정책상 `m.kbsec.com`/`www.kbsec.com` 접속이 차단되어(403 CONNECT) 모바일 DOM 을 실측하지 못했다. 따라서:
1. `collect-test` 워크플로(`--collect-only`)에 KB 전용 덤프 단계 추가 → `data/probe_findings.json` 에 et 앵커·컨테이너·이미지 폴더 구조 기록.
2. 덤프로 셀렉터 확정 → 스크레이퍼 구현 → `--collect-only` 로 `events_latest.json` 의 KB 5건 `scope_hash` 가 **연속 3회 동일**한지 확인 후 본 워크플로에 반영.

---

## 4. 문제 3 — DB·코드 구조

### 4.1 DB
현황: `pension_events` 45컬럼(정체성·기간·계좌·캐노니컬 텍스트·타입드 조건·검증 메타·재시도 메타·해시 3종), 원문 스냅샷 없음, `pension_events_bak_conditions`(PK 없음, 07-05 백업) 잔존, `event_changes.event_id` 인덱스 없음(advisor 경고), `monitoring_runs.report_md` 에 리포트 전문 59개 중복 보관.

제안 스키마(마이그레이션 0006~):
```sql
-- 원문 스냅샷: fetch 결과를 그대로 보존 → 해시 비교·디버깅·재추출 재현
create table event_snapshots (
  id bigserial primary key,
  event_id bigint references pension_events(id) on delete cascade,
  run_id bigint references monitoring_runs(id),
  fetch_status text not null,          -- ok|empty|error|blocked
  scope_hash text,
  text text, image_urls jsonb,
  fetched_at timestamptz default now()
);
create index on event_snapshots(event_id, fetched_at desc);

-- LLM 산출 이력: 승인/거부와 시그니처를 함께 보관 (표현 요동 분석 가능)
create table event_extractions (
  id bigserial primary key,
  event_id bigint references pension_events(id) on delete cascade,
  snapshot_id bigint references event_snapshots(id),
  schema_version int, model text, method text,   -- text|ocr
  raw jsonb, fact_signature jsonb,
  accepted boolean, reject_reason text,
  created_at timestamptz default now()
);
create index on event_extractions(event_id, created_at desc);

alter table pension_events
  add column source_ids jsonb,                       -- {"seq":..,"linkcd":..}
  add column accepted_extraction_id bigint references event_extractions(id),
  add column detail_fetch_failures int default 0;

create index on event_changes(event_id);
drop table pension_events_bak_conditions;            -- 07-05 백업, 검증 완료
alter table monitoring_runs drop column report_md;   -- reports/ 와 중복 (또는 storage 로)
```
- `conditions/benefits` 캐노니컬 텍스트는 유지(리포트용)하되 **자식 테이블에서 렌더한 파생값**임을 트리거 없이 코드 규약으로 고정(현행). 장기적으로는 뷰 `v_pension_events_report` 로 대체 가능.
- `content_hash`(원천 식별)·`source_content_hash`(재추출 트리거)·`review_retry_key` 는 스냅샷 테이블이 들어오면 `scope_hash` 하나로 통합.
- `event_changes` 는 사실 시그니처 diff 만 기록(§S3) → 행 수·노이즈 급감.

### 4.2 코드
| 현황 | 문제 | 제안 |
|---|---|---|
| `main.enrich_details` (80줄) 에 한투 특례·KB 스크린샷·서브링크 병합·정적/렌더 이중 시도 | 증권사 지식이 `main` 에 누수, 테스트 불가 | 각 스크레이퍼에 `fetch_detail(browser, ev) -> DetailContent` 구현. `main` 은 `for ev: ev.detail = scraper.fetch_detail(...)` 만 |
| 이미지 후보 선택이 `main._imgs_from_html`, `main.JS_CONTENT_IMGS`, `normalize._resolve_banner_images` 3곳 | 규칙 불일치(정규식이 조금씩 다름), 한투는 `_image_urls` 가 ev 에 남지 않아 해시에서 빠짐 | `content.py` 1곳(`select_images(container) `). `_resolve_banner_images` 의 재요청 제거(스냅샷 재사용) |
| `normalize.normalize_events` 150줄 루프에 캐시·재시도 한도·텍스트→OCR 폴백·병합·플래그가 얽힘 | 분기 검증 어려움(M4 카운터 버그 이력) | `decide(ev, old) -> Cached\|Skip\|Extract`, `extract(detail) -> Extraction`, `merge(old, new) -> (ev, changes)` 3단계 순수함수 + 시그니처 게이트 |
| `db.sync` 가 diff 계산과 PostgREST 쓰기를 한 함수에서 수행, `fetch_all_events` 를 main 과 sync 가 각각 호출 | 오프라인 테스트가 쓰기 경로를 못 덮음, 동일 조회 2회 | `compute_diff(scraped, existing) -> Diff` (순수) + `apply(diff)`; existing 은 1회 조회 후 전달 |
| `_RAW_FIELDS` 무조건 덮어쓰기 | 기간 null 진동 | §S4 |
| `requirements.txt`(pandas, anthropic 포함) ≠ 워크플로 `pip install …` | 재현성 없음 | `requirements.txt` 를 실제 의존성으로 정리 + 워크플로는 `pip install -r`; playwright 브라우저 캐시(`actions/cache`) |
| `youtube_pension_db.py` 루트 잔존, `scripts/`·`src/` 분리 기준 불명 | 탐색 비용 | `legacy/` 로 이동 또는 삭제, `src/backfill_conditions.py` 의 파서는 `pension_monitor/conditions.py` 로 이관(현재 `normalize` 가 `src.` 를 import — 패키지 경계 위반) |
| `reports/YYYY-MM-DD.md` 매일 커밋(현재 60개) | 레포 비대, `git pull --rebase` 충돌 위험 | `reports/latest.md` + DB(or Storage) 만 유지, 과거분은 `reports/archive/` 로 월 1회 정리 |
| 오프라인 테스트 1파일 582줄, assert 기반 | 회귀 범위 좁음 | pytest 전환 + **녹화 픽스처**(증권사별 상세 HTML 1벌) 로 `fetch_detail`·`scope_hash` 안정성 테스트, 시그니처 게이트 테스트("표현만 다른 두 추출 → 무변경") |

---

## 5. 문제 4 — 추가 개선 발굴

1. **한투 상세 fetch 취약**: 상세 URL 이 `securities.` 단일 도메인, 실패 시 `""` 반환. → 도메인 폴백·재시도, 실패는 `DetailContent(status="error")`.
2. **잔고유지기간 오인**(§1.5) → S4 거부 규칙 + Gemini 스키마 `period_*` description 에 "잔고유지·지급·접수 기간 제외" 는 이미 있으나 검증이 없으므로 코드 게이트 추가.
3. **진행중인데 종료일 경과** (KB 「TDF&ETF 시즌3」 `~2026-06-30`): 목록에 노출되는 한 `status='진행중'` 이 유지되지만 만기 스윕은 미노출 행만 본다. `end_date < today` 이면서 목록 노출 중이면 `needs_review('종료일 경과 — 시즌 갱신 의심')`.
4. **리포트 '변경' 섹션**: 현재 LLM 문장 diff 80자 노출 → 사실 diff 로 교체(§S3). 신뢰도 롤업에 "원문 fetch 실패 N건" 추가.
5. **관측성**: `monitoring_runs` 에 `llm_calls`, `cache_hits`, `fetch_failures`, `duration_sec` 컬럼 → 캐시 적중률·실패율 추이를 SQL 한 줄로. Step Summary 에 동일 수치.
6. **Gemini 예산**: S5 로 일일 호출이 급감하므로 `STRUCT_BUDGET` 은 주간 검증일에만 40, 평일 10 으로 분리. 429 연속 차단 로직 유지.
7. **워크플로**: cron 주석 "월~금 07:30 KST" 와 `TRIGGER_TYPE=weekly` 불일치 정정; `timeout-minutes: 25` 대비 상세 예산 200s + 구조화 360s 는 적정. `git pull --rebase … || true` 후 push 실패 시 조용히 지나감 → 실패를 Step Summary 에 표기.
8. **DB 위생**: `event_changes(event_id)` 인덱스, 백업 테이블 정리(advisor 2건 해소), `pension_events.status` 인덱스는 `(status, firm_name)` 로 이미 존재.
9. **키움**: 4행 `source_event_id` NULL·`extract_method='websearch'` 잔존. `EXCLUDED_FIRMS` 정책대로 리포트에서 분리 표기 중 — 수동 등록 경로(`source='manual'`) 를 `source_ids.manual` 로 공식화.
10. **테스트 실행 환경**: 로컬 `python tests/test_offline.py` 는 `openpyxl` 미설치 시 실패. xlsx 테스트를 `pytest.importorskip` 로 분리.

---

## 6. 실행 로드맵

| 단계 | 내용 | 기대 효과 | 리스크 |
|---|---|---|---|
| **P0 (즉시, 코드만)** — *PR #10 에 구현* | S1 빈 본문 차단(한투 폴백·실패 상태화 `_detail_status`), S4 기간 스티키·None 덮어쓰기 금지·잔고유지기간 오인 거부, `event_changes(event_id)` 인덱스(0006, 적용 완료), 실행 통계(LLM 호출/캐시/상세실패)를 요약·Step Summary 에 기록 | 기간 진동 0, 빈 해시 0, 한투 churn 절반 | 낮음 — 무회귀 방향 |
| **P1 (1주)** | S2 컨테이너 해시(미래·삼성·NH·한투) + `event_snapshots`, S3 시그니처 게이트 + `event_extractions`, 리포트 변경 섹션 교체, 관측 컬럼 | 변경 로그 ≤2/일, 캐시 적중 ≥90% | 컨테이너 셀렉터 오판 시 본문 누락 → 스냅샷 텍스트 길이 하한 경보로 방어 |
| **P2 (1~2주)** | KB 모바일 스크레이퍼(§3) — collect-test 로 DOM 확정 → 구현 → 해시 안정성 3회 확인 → 본 워크플로 | KB 5건 churn 0, OCR 입력 완전(다단 배너 전부) | ID 매핑 백필 오류 → `--dry-run` + 유니크 인덱스로 중복 차단 |
| **P3 (2주+)** | S5 스케줄 분리, 코드 모듈 재편(§4.2), 레거시·리포트 커밋 정리, pytest 픽스처 | LLM 호출 ≤5/일, 유지보수성 | 재편 중 회귀 → 픽스처 테스트 선행 |

각 단계는 별도 PR, 머지 후 정기 실행 1회의 `last_run_summary.json` 으로 지표 확인(REDESIGN.md §7 원칙 유지).

---

## 7. 이 문서가 확인하지 못한 것
- KB 모바일 `et*` 페이지의 실제 DOM(본문 컨테이너·기간 표기 위치) — 세션 egress 차단. §3.4 절차로 확정 필요.
- 한투 상세 페이지의 "휘발 텍스트"가 정확히 무엇인지(조회수·관련 이벤트 목록 추정) — 스냅샷 테이블 도입 후 첫 이틀 diff 로 확정.
- Gemini `seed` 고정의 실제 재현율 — P1 에서 동일 스냅샷 2회 호출 실험(오프라인 정책 예외, 2콜)으로 측정.
