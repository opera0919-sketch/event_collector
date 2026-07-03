# 파이프라인 전면 재개편 설계 (v2) — 2026-07-03

목표: **수집 데이터의 정확성 최우선**. 틀린 값을 노출하느니 비우고 '검토 필요'로
표기한다. 모든 적재 값은 출처(원문 텍스트/이미지 OCR/목록 요약)와 검증 상태를 갖는다.

## 1. 왜 v1 개선이 반영되지 않았나 (실패 원인 진단)

| # | 원인 | 증거 | v2 대응 |
|---|---|---|---|
| 1 | **PR #3 미머지** — 정기 실행은 main(구코드)에서 돎 | 7/2 23:38 run(`d730756`)이 키움 재시도·sid 미기록 신규행 삽입 | 재개편도 같은 브랜치 PR — **머지 전까지는 어떤 코드 개선도 정기 실행에 반영되지 않음** (운영 체크리스트 §7) |
| 2 | 구코드 실행이 DB 재오염 | sid null 신규행(KB 시즌3, seq=10010044 중복 재발) | 마이그레이션 재정리 + `(firm, source_event_id)` 유니크 인덱스는 sid 기록 코드가 머지돼야 효력 |
| 3 | **Gemini 정크가 DB 적재** | `conditions="No conditions information found in the provided text."`, `benefits="… → (혜택 내용 없음)"` 이 '변경'으로 기록·적재됨 | 정크 필터가 한국어 문구만 검사했음 → §4 검증 게이트(한/영 정크, 근거 대조, 무회귀) |
| 4 | 기간 정확도 | KB `idt=`(게시일) 오인, 같은 이벤트가 실행마다 다른 기간으로 정규화 | §4 기간 신뢰원 규칙 — 확신 없으면 비우고 `needs_review` |
| 5 | OCR 정확도 낮음 | 기본 모델이 `gemini-2.5-flash-lite`, 배너 1장만 전달(다단 배너 잘림), 자유 문자열 스키마 | §5 — `gemini-2.5-flash` 기본, 다중 이미지 단일 요청, 구조화 배열 스키마 |

## 2. 키움증권 지속 실패 원인 (조사 결론)

`data/probe_findings.json` 실측 기준:

- 모바일 목록 XHR `SIngEventListAjax` → **400 `{"eversafeThreat":true,"message":"CD-14000"}`**
  — F1Security **EverSafe** 안티봇이 요청을 봇으로 판정해 명시적으로 차단.
- 데스크톱 `/e/common/event/VIngEventView` → 400 + 난독화 JS 챌린지(`var TV3fk = {…}`)
  응답. 챌린지를 실행해 `evfw=` 토큰을 얻어야 이후 요청이 통과하는 구조.
- 정적 목록 HTML(190KB)은 200이지만 기간 데이터가 XHR 주입이라 내용이 없음.
  헤드리스 브라우저는 자동화 신호 탐지로 `/e/common/error` 프레임에 격리됨.

**"웹 검색으로는 되는데 여기서는 안 되는" 이유**: 검색엔진은 화이트리스트된
크롤러(IP/UA)로 수집한 **캐시/색인**을 보여주고, 사람 브라우저는 EverSafe JS
챌린지를 정상 실행해 토큰을 발급받는다. 반면 이 파이프라인은
① GitHub Actions(Azure) 데이터센터 IP + ② python-requests TLS 지문 +
③ 헤드리스 자동화 신호의 3중 조건으로 챌린지 이전 단계에서 차단된다.

**결론**: 키움은 사이트가 의도적으로 자동 수집을 거부하는 경우로, TLS 지문
위장 등 우회는 안티봇 통제 회피에 해당해 채택하지 않는다. **수집 대상 제외
유지**. 필요 시 대안: (a) 수동 등록 경로(마스터 테이블에 `source='manual'` 행
직접 입력 — 만기 자동 종료가 수명 관리), (b) 공식 제휴/공시 채널 확보.

## 3. DB 재구조화 — 마스터 / 조건·혜택 이원화

### `pension_events` (마스터) — 이벤트 정체성·기간·상태·검증 메타
기존 컬럼 유지 + 추가:

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `source_event_id` | text | 증권사 고유 ID(KB seq, NH mNo, 미래 cs_ecis_id, 삼성 MenuSeqNo, 한투 num). 매칭 1차 키 |
| `image_url` | text | OCR 대상 대표 배너 |
| `extract_method` | text | `text` \| `ocr` \| `hint` \| `none` — 조건/혜택의 출처 |
| `date_source` | text | `list` \| `detail` \| `llm` \| null — 기간의 출처 |
| `needs_review` | boolean | 자동 추출을 신뢰할 수 없음 → 리포트 '검토 필요' |
| `review_reason` | text | 검토 사유 (기간 미확인 / 근거 불일치 / 혜택 미확인 등) |
| `last_verified_at` | timestamptz | 조건/혜택이 원문에서 마지막으로 재확인된 시각 |

`conditions`/`benefits` 텍스트 컬럼은 **자식 테이블의 캐노니컬 렌더 사본**으로
유지한다(리포트/메일/xlsx/변경감지용). 구조의 원본은 자식 테이블.

### `event_conditions` (참여조건 — 라벨:값 행)
`event_id FK(cascade)` · `ord` · `label`(대상/기간/신청/유지조건/한도/기타) ·
`value_text` · `source`(llm-text/llm-ocr/heuristic/migration/manual)

### `event_benefits` (혜택 티어 — 조건→리워드 행)
`event_id FK(cascade)` · `tier_no` · `condition_text`(예: 'IRP 순입금 1백만~3백만원') ·
`benefit_text`(예: '신세계 모바일상품권 2만원') · `award_method`(전원/선착순/추첨/기타) ·
`award_limit`(인원, int) · `source`

### 마이그레이션 절차
1. 자식 테이블 신설 + 마스터 컬럼 추가
2. 7/2 구코드 실행 오염 재정리: sid 재백필 → `(firm, sid)` 중복 재병합 → 만기 스윕
3. **정크 정화**: 한/영 정크 패턴에 걸리는 `conditions`/`benefits` → null + `needs_review=true`
4. 기존 캐노니컬 텍스트('조건 → 리워드 (방식)' 줄 형식)를 자식 테이블로 best-effort 백필

## 4. 정규화 규칙 (정밀 설계) — 핵심: 검증 게이트

모든 LLM/휴리스틱 산출물은 아래 게이트를 통과해야 적재된다 (`normalize.py`).

**G1. 정크 차단**: 리워드/조건 값이 정크 패턴
(`no .*information|not (found|specified)|없음|명시되|확인 필요|알 수 없|unknown|n/?a`)에
걸리거나, 리워드에 실질 토큰(금액·%·무료·우대·상품권·쿠폰·포인트·수수료 등)이
전혀 없으면 그 행을 버린다. 전 행이 버려지면 '추출 실패'로 처리(G3).

**G2. 근거 대조(grounding)**: 텍스트 기반 추출은 각 리워드의 숫자·금액 토큰이
원문(공백/콤마 정규화 후)에 실제로 존재하는지 검사. 불일치 → 적재는 하되
`needs_review=true`, 사유 '근거 불일치'. (OCR은 원문 텍스트가 없으므로 대조
불가 — `extract_method='ocr'`로 출처만 기록.)

**G3. 무회귀(no-regression)**: 새 추출이 실패/전량 정크일 때 기존 DB의 양호한
캐노니컬 값을 **절대 덮어쓰지 않는다**. 기존 값 유지 + `last_verified_at`만 미갱신.
기존 값도 없을 때만 목록 요약(`_benefits_hint`) 폴백 + `extract_method='hint'` +
`needs_review=true`.

**G4. 기간 신뢰원 규칙** (`date_source` 기록):
- 신뢰 목록(미래에셋 `span.date`, NH `mStartDttm/mEndDttm`, 한투 목록 '기간:') → `list`
- **KB 목록 날짜는 불신** (게시일 `idt` 혼입 이력) → 상세 본문 '기간' 정규식(`detail`)
  → LLM 추출(`llm`) 순으로만 채움
- 검증: `start<=end`, 종료연도 `[올해-1, 올해+2]`, 시작·종료 1일 이하 간격 의심
- 목록(신뢰) 기간과 LLM 기간이 모순되면 목록 유지 + `needs_review` 사유 기록
- **어느 출처로도 확신 없으면 null + `needs_review`** — 틀린 날짜 노출 금지

**G5. 대상계좌 보수 판정**: 명시 키워드(연금저축/IRP/DC…) 또는 LLM 판정의 OR.
'퇴직연금'→IRP+DC 통칭은 유지(의미상 동치), **'연금' 단독 → 연금저축+IRP 추정
규칙은 폐지**(과대 표기). 신호가 전혀 없으면 모두 false + 사유 '대상계좌 미확인'.

**G6. 변경 감지의 원천 기반화**: `content_hash`를 원천 데이터
(`firm|sid|event_name|start|end`)로 한정. LLM 산출물의 표현 요동
(예: 7/2 실행의 "연금저축계좌"↔"연금저축 계좌" 허위 '변경' 5건)이 변경 이력을
오염시키지 않는다. 조건/혜택의 실질 변경은 기간/원문 변경에 동반되며, 캐시
규칙(동일 sid+종료일+캐노니컬 존재 → 재추출 생략)이 재추출 요동을 차단.

## 5. Gemini 활용 재설계 (정확도)

- **모델**: 기본 `gemini-2.5-flash` (기존 flash-lite → 상향, `VISION_MODEL`로 재정의 가능).
  무료 티어 RPD가 낮아지므로 예산 상한·6.5s 페이싱·429 연속차단 로직은 유지.
- **스키마**: 자유 문자열 → **구조화 배열**로 변경. 문자열 재파싱 제거.
  `benefits: [{condition, reward, method, limit_count}]`,
  `conditions: [{label, value}]`, `period_start/end`, `acct_*`, `is_pension`,
  `evidence_missing`(자료에 정보가 없으면 배열 비우고 true — 추측 금지 강화).
- **다중 이미지 OCR**: 상세 본문의 콘텐츠 이미지 **최대 3장을 한 요청에** 전달
  (현재 1장 → 다단 배너 잘림이 저정확도의 주원인). 호출 수는 동일(1회).
- **호출 절감 구조 개편**: 기존 2패스(enrich_structured + reverify_new_changed)를
  단일 normalize 패스 + 검증 게이트로 통합 — 신규/변경 건 재호출 중복 제거.
- 텍스트 우선(본문 ≥200자면 텍스트 구조화, 저비용) → 부족 시 이미지 OCR 폴백 유지.

## 6. 파이프라인 v2

```
collect(목록, 5개사)
  → detail 보강(본문 텍스트 + 콘텐츠 이미지 최대 3장)
  → normalize(Gemini 구조화[캐시 우선] → 게이트 G1~G5 → 캐노니컬 렌더 + 구조화 행)
  → db.sync v2(마스터 upsert[sid 1차 키] + 자식 테이블 교체 + 만기 스윕 + 실패사 유지노출)
  → report/mail/xlsx (검토 필요 사유 명시)
```

- 자식 테이블은 **추출 성공 건만 교체**(delete→insert). 캐시 적중/추출 실패 건은
  기존 자식 행 유지 (무회귀).
- `monitoring_runs.failed_firms`(text) 추가 — 증권사별 실패 이력 추적.

## 7. 운영 체크리스트 (재발 방지)

1. **개선 코드는 main에 머지되기 전까지 정기 실행에 반영되지 않는다.**
   PR 머지 → 다음 실행 확인이 배포의 완결이다.
2. 마이그레이션(DB)과 코드(PR)는 짝이다 — 마이그레이션만 적용된 상태에서
   구코드가 돌면 오염이 재발한다(7/2 사례). 머지 직후 실행 1회로 정합 확인.
3. 검증은 `data/last_run_summary.json` + `reports/latest.md` + '검토 필요' 섹션의
   `review_reason`으로 한다 (로그 전체 불필요 — OPERATIONS.md).

## 8. 테스트 정책 (API 소진 최소화)

- Gemini·증권사 사이트 **실호출 0회**로 검증: 게이트/렌더/sync/리포트는 스키마
  일치 가짜 응답으로 오프라인 단위 검증 (`tests/test_normalize.py`).
- 실동작 확인은 머지 후 정기 실행 1회의 요약 파일로 갈음.
