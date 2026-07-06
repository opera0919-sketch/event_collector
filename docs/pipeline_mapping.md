# 수집 파이프라인 → 조건 타입드 컬럼 매핑 규칙

> 작업지시서 `conditions_restructure_workorder.md` §5 산출물 · 2026-07-05
> 대상: `public.pension_events` 신규 컬럼 (0002 마이그레이션)

향후 모니터링 배치는 자유 텍스트(`conditions`)에 의존하지 않고 **신규 타입드
컬럼을 직접 채운다**. `conditions`는 신규 수집분부터 **원문 아카이브 용도**로만
사용한다.

## 1. 파서 재사용

라벨→컬럼 매핑과 날짜 정규화는 `src/backfill_conditions.py`의 순수 함수를
그대로 재사용한다 (백필과 수집이 같은 규칙을 공유 → 이원화 방지):

- `parse_conditions(text) -> dict` — 라벨 분해 (§2 표)
- `parse_kr_date(s)` / `parse_period_line(value)` — 날짜 정규화

`pension_monitor/normalize.py`의 Gemini 구조화 결과(`condition_rows`:
`{label, value_text}`)를 적재할 때, 라벨별로 아래 표에 따라 타입드 컬럼에
기록한다. (Gemini 스키마의 라벨 enum: 대상/기간/신청/유지조건/한도/기타)

## 2. 라벨 → 컬럼 매핑 (백필 §3.1과 동일)

| 컬럼 | 소스 라벨/패턴 | 규칙 |
|---|---|---|
| `eligibility` | `대상`, `대상고객`, `대상계좌`, `대상상품`, `참여조건`, `요건` | 복수 매칭 시 `; ` 연결 |
| `exclusions` | `제외`, `제외대상` + eligibility 내 "…제외" 절 | 라벨 명시분 우선 + eligibility 본문의 제외 절을 분리·추가 (§7-1 확정). 괄호 안은 `/` 분할 후 '제외' 포함 절만, 평문은 '제외'로 끝나는 절만. **eligibility 원문은 수정하지 않음** (`_exclusion_clauses` 재사용) |
| `apply_required` | `신청`, `신청필수` | 부정("불필요/자동 참여/없음/없이") → **false** 우선 판정 → "필수" → **true** → 그 외 NULL + `cond_notes` 원문 |
| `marketing_consent_required` | 본문 전체 | "마케팅"∧"동의"∧("SMS"∨"PUSH"∨"필수") → true. 부정 표현("동의 불필요/없이/하지 않") → false. 없으면 NULL |
| `annual_cap_krw` | `한도` 라인 우선, 없으면 본문 | ① `연간?(누적)? N만원` ② `연간 …(≤20자)… N만원` ③ `감독규정에 의해 최대 N만원` / `연도 … N만원` → N×10000. 연간 단일 금액이 아닌 한도(운용사별·1인1회·중복지급)는 **NULL 유지** |
| `hold_condition` | `유지조건` | 원문 그대로(trim). 복수 라인은 `; ` 연결. 날짜 분리 안 함(설계 결정) |
| `cond_notes` | `합산기준`, `순입금 산정`, `중복지급`, `혜택지급`, `적용매체`, `기타`, `조건`, 기타 미매핑 라벨·무라벨 서술 | `라벨: 값` 형태 `\n` 연결 |

## 3. 기간 처리

1. 날짜는 `parse_kr_date`(4포맷: `2026.07.01`, `2026/03/30`, `2026-07-01`,
   `2026년 4월 1일(수)`)로 정규화 후 `start_date`/`end_date`에 기록한다.
2. **`conditions`에 기간 라인을 중복 기재하지 않는다** (신규 수집분부터).
   기간의 신뢰원 판정은 `normalize.reconcile_period`(REDESIGN.md G4)가 담당하며
   `date_source` 컬럼에 출처(list/detail/llm)를 남긴다.
3. `기간: A ~ B`에서 B가 비어 있으면 `end_date`는 기록하지 않는다.
4. 백필과 달리 수집 시점에는 기존 값 보호 규칙이 아니라 G4 신뢰원 규칙이
   우선한다 (목록 신뢰 증권사 > 상세 본문 > LLM, 확신 없으면 NULL+검토).

## 4. 판정 불가 원칙 (정확성 최우선)

- 불리언 판정 불가 → **NULL 유지, 추측 금지** (true/false 는 문구 확인 시만)
- 연간 한도 금액이 문면에 없으면 `annual_cap_krw` NULL + 원문은 `cond_notes`
- 파싱 불가 정보는 전부 `cond_notes`로 — 정보 소실 금지

## 5. 정합성 점검

배치 반영 후 `src/validate_conditions.py`로 추출률·이상치·원문 무변경을 점검
한다. 한도 추출률은 '연간 단일 금액이 실제 존재하는 행' 기준으로 해석한다
(예: "운용사별 3만원·합산 9만원", "1인 1회", "중복 지급 가능"은 정당 NULL).

## 6. §7 미해결 질문 확정 (2026-07-05)

1. **제외 절 분리**: eligibility 내 "…제외" 절은 `exclusions`로 분리·복사한다
   (eligibility 원문은 유지). → §2 표 및 `_exclusion_clauses` 반영 완료.
2. **인덱스**: 현행 유지 (신규 인덱스 없음). 1천 행 초과 시 재검토.
