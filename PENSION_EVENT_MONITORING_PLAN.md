# 증권사 연금 이벤트 모니터링 루틴 — 계획안

> 작성일: 2026-06-12 / 상태: **검토 완료 + 운영 환경 실측 완료** / 버전: v1.2
>
> v1.0 초안 작성 후 실행 환경에서 구현 가능성을 직접 검증하고, 재검토에서 발굴한
> 개선 사항을 반영(v1.1)한 뒤, GitHub Actions 운영 환경에서 6개사 접근성을
> 실측하고 사용자 결정 사항을 확정한 버전입니다. 검증 결과는 §2·§2.1,
> 개선 사항은 §9, 확정된 결정 사항은 §10에 정리했습니다.

---

## 1. 목적 및 범위

국내 6개 증권사의 **연금 관련 이벤트** 진행 내역을 공식 홈페이지에서 수집하여
Supabase DB로 관리하고, 매주 월요일(또는 별도 요청 시) 업데이트 후
전 주 대비 변동 사항과 인사이트를 요약한 리포트를 메일로 발송한다.

| 항목 | 내용 |
|---|---|
| 대상 증권사 | 미래에셋증권, 한국투자증권, 삼성증권, KB증권, NH투자증권 (키움증권은 WAF 상시 차단으로 2026-07부터 수집 대상 제외) |
| 데이터 출처 | 각 증권사 **공식 홈페이지** (이벤트 목록/상세 페이지) |
| 이벤트 범위 | 연금 관련 이벤트만 (연금저축, IRP, DC, 퇴직연금, 디폴트옵션 등) |
| 저장소 | Supabase (기존 프로젝트 `fbkriifozbwuaoegmmcf`, ap-southeast-1, ACTIVE) |
| 주기 | 매주 월요일 09:00 KST 자동 실행 + 별도 요청 시 수동 실행 |
| 산출물 | DB 업데이트 + 주간 리포트 메일 발송 |

### 필수 관리 항목 (요구사항)

증권사명 · 진행중/종료 여부(모니터링 일자 기준) · 이벤트명 · 시작일자 · 종료일자 ·
대상계좌(연금저축 / IRP / DC / 기타 — **4개 열**) · 참여조건 · 혜택내용 · 비고

---

## 2. 구현 가능성 사전 검증 결과 (2026-06-12 실측)

계획 확정 전에 실행 환경에서 직접 테스트한 결과이며, 아키텍처 선택(§3)의 근거가 된다.

| # | 검증 항목 | 결과 | 근거 |
|---|---|---|---|
| 1 | Supabase 접근 | ✅ 가능 | MCP로 프로젝트 조회·테이블 목록 확인 완료. 기존 테이블(recipes 등)과 충돌 없음 |
| 2 | Claude 세션에서 증권사 사이트 직접 접근 | ❌ **차단** | 6개사 전부 HTTP 403, 응답 헤더 `x-deny-reason: host_not_allowed` — 세션 네트워크 정책이 허용목록 방식 (pypi 200, google 403으로 교차 확인) |
| 3 | WebFetch(서버측 페치) | ❌ 차단 | 키움·미래에셋·삼성 이벤트 페이지 모두 403 |
| 4 | WebSearch | ✅ 가능 | 공식 이벤트 페이지 URL 및 이벤트 개요 확보 가능 (예: 키움 `…/VIngEventView`, 미래에셋 `…/mki7000/r01.do`) |
| 5 | 세션 내 이메일 발송 도구 | ❌ 없음 | 연결된 MCP는 Notion/Figma/Supabase/Canva/Drive/Linear뿐. Gmail 등 메일 커넥터 미연결 |
| 6 | GitHub Actions 사용 | ✅ 가능 | 레포 연동 확인. 스케줄(cron) + 수동(workflow_dispatch) 실행, Secrets로 키 관리 가능 |

**결론: 구현 가능.** 단, "Claude 세션이 직접 크롤링 + 메일 발송"하는 단순 구조는
현 환경 제약(2·3·5번)으로 불가능하므로, **수집·발송 실행체를 GitHub Actions로
분리**하는 구조를 채택한다(§3). 세션의 역할은 트리거·검수·리포트 코멘트로 한정한다.

### 2.1 운영 환경(GitHub Actions) 접근성 실측 결과 — 2026-06-12

`site-access-test` 워크플로(`scripts/site_access_test.py`)로 실제 운영 환경에서
curl + Playwright(헤드리스 Chromium) 이중 측정. 러너 IP: 미국 Azure (Phoenix, AS8075).

| 증권사 | curl | 브라우저 렌더링 | 판정 |
|---|---|---|---|
| 미래에셋증권 | 200 (35KB) | ✅ 「목록 - 진행중 이벤트」 정상 렌더링 | **수집 가능** |
| 한국투자증권 | 1차 000 / 2차 200 (177KB) | ❌ 1차 연결끊김 / 2차 타임아웃 | **간헐적 — requests 수집 가능** (§2.2) |
| 삼성증권 | 200 (46KB) | ✅ 이벤트 목록 + '연금' 카테고리 탭 확인 | **수집 가능** |
| 키움증권 | 200 (213KB) | ✅ 이벤트 전체보기 정상 렌더링 | **수집 가능** |
| KB증권 | 200 (10KB) | ✅ 모바일 이벤트 페이지 정상 | **수집 가능** |
| NH투자증권 | 200 (53KB) | ✅ 진행중 이벤트 목록 + '연금/ISA' 탭 확인 | **수집 가능** |

**5/6 수집 가능 확정.** 보너스: 삼성증권·NH투자증권은 이벤트 목록에 자체
'연금' 카테고리 필터가 있어 연금 이벤트 선별 정확도를 높일 수 있다.

### 2.2 한국투자증권 접근성 분석 및 대응 (2회 실측 종합)

| 실측 | 러너 IP | curl | 브라우저(Playwright) |
|---|---|---|---|
| 1차 | 미국 Phoenix (Azure) | 000 — 연결 자체 거부 | `ERR_EMPTY_RESPONSE` |
| 2차 | 미국 Virginia (Azure) | **200, 177KB 정상 수신** (m. 도메인 포함 전부 200) | 30초 타임아웃 |

- **판정: 고정적 해외 IP 차단이 아니라 간헐적/부분적 접근 제한.** 러너 IP에 따라
  연결 거부가 발생하며, 연결되더라도 브라우저 전체 페이지 로드는 느려 타임아웃.
- **핵심 발견**: 이벤트 목록 페이지(Event.jsp)는 **서버 렌더링**이라 단순 HTTP
  요청만으로 본문 HTML(177KB) 확보 가능 → 한투는 Playwright 불필요.
- 확정 대응 (구현 반영):
  1. 한투 수집기는 **requests 기반** + 재시도(최대 5회, 지수 백오프) + 넉넉한
     타임아웃으로 구현. 간헐 거부는 재시도·재실행으로 흡수.
  2. 그래도 실패한 주는 리포트에 "한국투자증권 수집 실패(재시도 예정)"를 명시하고
     직전 데이터를 유지 — 종료 오판 방지 규칙(2회 연속 미노출)과 결합.
  3. 지속 실패 시 최후 수단: 세션 네트워크 정책에 `*.koreainvestment.com` 허용 후
     세션 보조 수집, 또는 self-hosted runner(국내 IP).

---

## 3. 아키텍처

```
[매주 월 09:00 KST cron]──┐
[별도 요청: Claude 세션이 ├─> GitHub Actions 워크플로 (ubuntu runner)
 workflow_dispatch 트리거]┘        │
                                   ├─ 1. 수집  : Playwright(헤드리스) + requests로 6개사 이벤트 페이지 크롤링
                                   ├─ 2. 필터  : 연금 키워드 분류기로 연금 이벤트만 선별
                                   ├─ 3. 적재  : Supabase upsert (신규/변경/종료 감지 포함)
                                   ├─ 4. 리포트: 전 주 스냅샷 대비 변동 요약 Markdown 생성
                                   └─ 5. 발송  : 메일 전송 (수신: opera0919@gmail.com)
                                                + 리포트를 repo `reports/`에 커밋(아카이브)
```

- **실행체 = GitHub Actions를 선택한 이유**: §2의 제약을 모두 우회한다.
  GitHub-hosted runner는 외부 사이트 접근이 자유롭고, SMTP 발송이 가능하며,
  cron으로 "매주 월요일" 요구사항을 정확히 충족한다.
- **"별도 요청 시" 처리**: 사용자가 Claude 세션에서 "업데이트 해줘"라고 하면,
  Claude가 GitHub MCP의 `actions_run_trigger`로 워크플로를 수동 실행하고
  완료 후 결과/리포트를 세션에서 요약해 준다. 네트워크 정책 변경이 필요 없다.
- **대안(차안)**: 사용자가 Claude Code 환경의 네트워크 정책을 변경(증권사 도메인
  허용)하면 세션 내 직접 수집도 가능해진다. 단, 메일 발송은 여전히 별도 수단이
  필요하므로 기본안은 Actions 일원화.

---

## 4. DB 설계 (Supabase / PostgreSQL 17)

기존 프로젝트에 신규 테이블 3개를 추가한다(기존 recipes 등과 무관, 충돌 없음).
모든 테이블 RLS 활성화, 쓰기는 service_role 키(GitHub Secrets 보관)로만 수행.

```sql
-- ① 이벤트 본 테이블 (필수 항목 전부 포함)
create table pension_events (
  id              bigint generated always as identity primary key,
  firm_name       text not null,                       -- 증권사명
  event_name      text not null,                       -- 이벤트명
  status          text not null default '진행중'
                  check (status in ('진행중','종료')),  -- 모니터링 일자 기준
  start_date      date,                                -- 시작일자
  end_date        date,                                -- 종료일자 (상시 이벤트는 NULL)
  acct_pension    boolean not null default false,      -- 대상계좌: 연금저축
  acct_irp        boolean not null default false,      -- 대상계좌: IRP
  acct_dc         boolean not null default false,      -- 대상계좌: DC
  acct_etc        text,                                -- 대상계좌: 기타 (ISA·해외주식 등 명시)
  conditions      text,                                -- 참여조건
  benefits        text,                                -- 혜택내용
  remarks         text,                                -- 비고 (이미지 공지 여부, 중복 이벤트 등)
  event_url       text,                                -- 공식 상세 페이지 (출처 증빙)
  content_hash    text,                                -- 변경 감지용 해시
  first_seen_at   timestamptz not null default now(),
  last_seen_at    timestamptz,
  closed_at       timestamptz,                         -- 종료 판정 시각
  unique (firm_name, event_name, start_date)           -- 자연키 (재게시/중복 방지)
);

-- ② 모니터링 실행 로그 (주기 실행 추적 + 리포트 원문 보관)
create table monitoring_runs (
  id              bigint generated always as identity primary key,
  run_at          timestamptz not null default now(),
  trigger_type    text not null check (trigger_type in ('weekly','manual')),
  firms_ok        int, firms_failed int,
  events_active   int, events_new int, events_closed int, events_changed int,
  report_md       text                                 -- 발송 리포트 원문
);

-- ③ 변경 이력 (전 주 대비 변동 추적의 원천)
create table event_changes (
  id              bigint generated always as identity primary key,
  run_id          bigint references monitoring_runs(id),
  event_id        bigint references pension_events(id),
  change_type     text not null check (change_type in ('신규','종료','변경')),
  field_name      text,                                -- 변경 항목 (기간연장, 혜택변경 등)
  old_value       text,
  new_value       text,
  detected_at     timestamptz not null default now()
);
```

**상태 전이 규칙**
- 신규: 자연키 미존재 → INSERT + `change_type='신규'`
- 변경: `content_hash` 불일치 → 필드별 diff 기록 (기간 연장, 혜택 변경 등)
- 종료: ① `end_date` < 모니터링일 이면 즉시 종료 처리,
  ② end_date 미경과인데 목록에서 사라진 경우 **2회 연속 미노출 시** 종료 처리
  (일시적 페이지 오류로 인한 오판 방지)

---

## 5. 수집 설계

### 5.1 수집 대상 페이지 (실측 검증 완료 — 2026-06-12 확정)

| 증권사 | 이벤트 목록 페이지 | 실측 |
|---|---|---|
| 미래에셋증권 | `securities.miraeasset.com/mw/mki/mki7000/r01.do` | ✅ 렌더링 확인 |
| 한국투자증권 | `securities.koreainvestment.com/main/customer/notice/Event.jsp?gubun=i` | ⚠️ 간헐 거부, 서버렌더링이라 requests+재시도로 수집 (§2.2) |
| 삼성증권 | `www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=eventList` | ✅ 렌더링 확인, '연금' 카테고리 탭 보유 |
| 키움증권 | `www1.kiwoom.com/h/customer/event/VIngEventView` | ✅ 렌더링 확인 |
| KB증권 | `m.kbsec.com/go.able?linkcd=m06020000` | ✅ 렌더링 확인 (모바일웹) |
| NH투자증권 | `m.nhqv.com/customer/event/eventList` (→ m.nhsec.com 리다이렉트) | ✅ 렌더링 확인, '연금/ISA' 탭 보유 |

### 5.2 수집 방식

- **Playwright(헤드리스 Chromium)** 를 기본 수집기로 사용.
  국내 증권사 이벤트 페이지는 JS 렌더링(SPA)·POST 기반 목록이 많아
  requests+BeautifulSoup만으로는 누락 위험이 큼. 정적 페이지로 확인되는 곳만
  requests로 경량화.
- 증권사별 파서를 모듈로 분리(`scrapers/miraeasset.py` 등) — 사이트 개편 시
  해당 모듈만 수정.
- 요청 간 지연(2~3초)·일반 브라우저 UA 사용. robots.txt 및 사이트 부하 존중
  (주 1회 실행이라 부하 미미).

### 5.3 연금 이벤트 필터

기존 `youtube_pension_db.py`의 `PENSION_KEYWORDS`를 재활용·보강한
키워드 매칭을 1차 필터로 사용:

```
연금, 퇴직연금, 개인연금, 연금저축, 연저펀, IRP, DC, DB형, 디폴트옵션,
연금계좌, 연금수령, 세액공제, 과세이연, 노후, 은퇴, TDF, 계좌이전(연금 문맥)
```

제목+상세 텍스트에서 매칭. 모호 건(예: "계좌이전 이벤트"가 연금인지 불분명)은
`remarks`에 표시하고 리포트의 "검토 필요" 섹션에 노출 → 수동 확정.

### 5.4 항목 추출

- 이벤트명·기간: 목록 페이지에서 구조적으로 추출 (가장 안정적)
- 대상계좌·참여조건·혜택: 상세 페이지 텍스트에서 규칙 기반 추출.
  **상세가 이미지 배너뿐인 경우**(증권사 이벤트의 흔한 패턴) 텍스트 추출이
  불가능하므로 `remarks='상세 이미지 공지'` + `event_url` 보존으로 처리하고,
  2단계 개선(§9-④)에서 Claude API 비전으로 이미지 파싱을 도입.

---

## 6. 루틴 및 트리거

| 트리거 | 방식 | 비고 |
|---|---|---|
| 정기 | GitHub Actions cron `0 0 * * 1` (UTC) = **매주 월요일 09:00 KST** | trigger_type='weekly' |
| 별도 요청 | 사용자가 Claude 세션에 요청 → Claude가 `workflow_dispatch` 트리거 → 완료 후 결과 요약 | trigger_type='manual' |
| 실패 알림 | 수집 0건·파서 오류·발송 실패 시 오류 메일 + Actions 실패 표시 | §9-⑥ |

---

## 7. 주간 리포트 형식

수신: `opera0919@gmail.com` / 제목: `[연금이벤트 위클리] 2026-06-15 — 진행중 18건 (신규 3, 종료 2)`

```markdown
# 연금 이벤트 위클리 (2026-06-15 기준)

## 요약
진행중 18건 | 🆕 신규 3건 | 🔚 종료 2건 | ✏️ 변경 1건 (전 주 대비)

## 전 주 대비 주요 변동
- 🆕 키움증권 「연금키움 시즌2」 (6/10~7/31, 연금저축·IRP) — 순입금액별 최대 100만원 상품권
- 🔚 삼성증권 「연금 다이렉트 이전」 종료 (6/8 마감)
- ✏️ NH투자증권 「IRP 채우기」 기간 연장 (6/30 → 7/31)

## 진행중 이벤트 현황 (증권사별)
| 증권사 | 이벤트명 | 기간 | 연금저축 | IRP | DC | 기타 | 혜택 요약 |
|---|---|---|---|---|---|---|---|
| ... 6개사 전체 표 ...

## 종료 임박 (7일 이내 마감)
- 키움증권 「연금키움」 6/30 마감 (D-15)
- 미래에셋증권 「연금이전 감사제」 6/20 마감 (D-5)

## 인사이트
- 6월 들어 '타사 이전 시 입금액 2배 인정' 조건이 3개사로 확산 — 이전 고객 유치 경쟁 심화
- IRP 대상 이벤트가 연금저축 대비 2배 — 디폴트옵션 시행 영향 지속
- (3~5줄, 경쟁 동향·혜택 강도 변화·기간 패턴 중심)

## 검토 필요
- KB증권 「○○ 이벤트」 — 연금 관련 여부 모호 (상세 이미지 공지)
```

메일 본문은 HTML 표로 변환해 발송하고, 동일 내용을 `reports/YYYY-MM-DD.md`로
repo에 커밋 + `monitoring_runs.report_md`에 저장(3중 아카이브).

### 메일 발송 수단 (결정 필요 — §10)

| 안 | 방법 | 장단점 |
|---|---|---|
| **A (권장)** | Gmail SMTP + 앱 비밀번호 (Python smtplib, Secrets 보관) | 추가 가입 불필요, 본인 계정 발송, 5분 설정 |
| B | Resend 등 트랜잭션 메일 API | 깔끔한 API지만 발신 도메인 인증 필요 |
| C | Gmail MCP 커넥터를 세션에 연결 → Claude가 직접 발송 | 정기 자동 발송(무세션 상태)이 불가능해 단독으로는 부적합 |

---

## 8. 구현 단계

| Phase | 내용 | 산출물 |
|---|---|---|
| 0 | 사용자 결정 사항 확정 (§10) + Gmail 앱 비밀번호 등 Secrets 등록 | Secrets 4종 |
| 1 | 6개사 이벤트 페이지 URL 확정 + 파서 PoC (Actions에서 접근성 실측 포함) | `scrapers/` 6개 모듈 |
| 2 | Supabase 스키마 생성(§4) + 적재 로직 | 마이그레이션, `db.py` |
| 3 | 변동 감지 + 리포트 생성기 | `report.py` |
| 4 | Actions 워크플로 + 메일 발송 + **2주 시험 운영** (실제 데이터로 항목 정확도 검수) | `.github/workflows/pension-events.yml` |
| 5 | 운영 전환 + 개선 반영 (이미지 공지 파싱 등) | 운영 체크리스트 |

---

## 9. 재검토에서 발굴한 리스크 및 개선 사항 (계획 반영 완료)

| # | 리스크/개선 | 대응 (반영 위치) |
|---|---|---|
| ① | 세션 네트워크 차단·메일 도구 부재 — 초안의 "세션 직접 수집" 구조 불가 | 실행체를 GitHub Actions로 변경 (§3) |
| ② | 증권사 페이지 JS 렌더링으로 정적 크롤링 누락 | Playwright 기본 채택 (§5.2) |
| ③ | **GitHub runner가 해외 IP** — 국내 금융사 일부가 해외 IP 차단 가능성 | Phase 1에서 6개사 접근성 실측. 차단 확인 시 대안: (a) 세션 네트워크 정책에 해당 도메인 허용 후 세션 수집 + Actions는 발송만, (b) self-hosted runner |
| ④ | 이벤트 상세가 이미지 배너라 참여조건/혜택 텍스트 추출 불가한 경우 다수 예상 | 1차: '이미지 공지' 비고 + URL 보존. 2차: Claude API 비전 파싱 도입 (§5.4) |
| ⑤ | 종료 판정 오류 — 페이지 일시 오류를 '종료'로 오판 | 2회 연속 미노출 규칙 (§4) |
| ⑥ | 사이트 개편으로 파서 무음 실패(silent failure) | 증권사별 수집 0건 시 경고 메일 + Actions 실패 처리 (§6) |
| ⑦ | 동일 이벤트 재게시·시즌제로 인한 중복 적재 | (증권사, 이벤트명, 시작일) 자연키 + content_hash (§4) |
| ⑧ | 연금 키워드 필터 오탐/미탐 | 모호 건 '검토 필요' 섹션으로 노출해 수동 확정, 키워드 점진 보강 (§5.3) |
| ⑨ | service_role 키 노출 위험 | GitHub Secrets 보관, RLS 전 테이블 활성화, 코드에 키 미포함 (§4) |
| ⑩ | 리포트 유실 | 메일 + repo 커밋 + DB 3중 보관 (§7) |
| ⑪ | 비용 | Supabase 무료 티어(행 수 미미)·Actions 무료 분량(주 1회 ~5분) 내 — 추가 비용 0원 |
| ⑫ | "별도 요청 시" 실행 경로 불명확 | 세션에서 workflow_dispatch 트리거로 표준화 (§6) |

---

## 9.5 구현 및 시험 운영 결과 (2026-06-12)

Phase 1~4 골격 구현 완료, 운영 환경(GitHub Actions)에서 5회 시험 수집 수행.

| 증권사 | 수집 상태 | 방식 |
|---|---|---|
| 한국투자증권 | ✅ 6건 (혜택 요약 포함) | requests + 도메인 폴백 |
| 삼성증권 | ✅ 4건 (상세 혜택 추출 성공) | Playwright + 상세 페이지 |
| KB증권 | ✅ 8건 (연금/ISA 카테고리) | PC 목록 JSP requests |
| NH투자증권 | ✅ 5건 | Playwright (m.nhsec.com) |
| 미래에셋증권 | ✅ 2건 (간헐 지연 → 재시도 패스로 안정화) | Playwright + 배너 alt 파싱 |
| 키움증권 | ❌ 미수집 — **알려진 한계** | 헤드리스 차단 + 목록 XHR 주입으로 정적 HTML 에도 없음 |

- 수집분 23건(시점 기준)은 Supabase `pension_events` 에 적재 완료, 리포트는
  `reports/YYYY-MM-DD.md` + `monitoring_runs` 에 보관.
- **키움 후속 개선안**: ① 목록 XHR 엔드포인트 직접 호출 분석, ② 세션 네트워크
  정책에 kiwoom.com 허용 후 세션 보조 수집, ③ WebSearch 보조. 주간 리포트에는
  "수집 실패(재시도 예정)"로 표기되어 무음 실패는 아님.
- 잔여 품질 과제: 미래에셋 배너 alt 없는 항목은 이벤트명 확보 불가(일부 누락
  가능), KB 상세가 이미지 공지라 참여조건/혜택은 '검토 필요'로 노출 → 2단계
  (Claude API 비전 파싱)에서 개선.

## 10. 사용자 결정 사항 (2026-06-12 확정)

1. **메일 발송 수단**: ✅ A안(Gmail SMTP + 앱 비밀번호) 확정.
   → Phase 0에서 Gmail 앱 비밀번호를 GitHub Secrets(`MAIL_APP_PASSWORD`)로 등록 필요.
2. **수신 주소**: ✅ `opera0919@gmail.com` 단일로 시작, 추후 추가 예정.
   → 수신 목록은 코드가 아닌 Secrets/설정 파일로 관리해 추가가 쉽도록 구현.
3. **한국투자증권 접근 제한 대응**(§2.2): 실측 결과 간헐적 제한으로 확인되어
   requests + 재시도로 해결 — **사용자 추가 설정 불필요** (지속 실패 시에만
   세션 네트워크 정책 허용을 재논의).
4. **종료 임박(7일 이내) 이벤트 알림** 섹션: ✅ 추가 확정 (§7 반영).
