# -*- coding: utf-8 -*-
"""주간 리포트 생성 (계획안 §7 형식)."""

import datetime as dt
import io

# 메일 첨부 xlsx 의 열 구성 (pension_events 테이블)
_XLSX_COLS = [
    ("firm_name", "증권사"), ("event_name", "이벤트명"), ("status", "상태"),
    ("start_date", "시작일"), ("end_date", "종료일"),
    ("acct_pension", "연금저축"), ("acct_irp", "IRP"), ("acct_dc", "DC"), ("acct_etc", "기타계좌"),
    ("conditions", "참여조건"), ("benefits", "혜택내용"), ("remarks", "비고"), ("event_url", "출처URL"),
]


def build_xlsx(rows: list) -> bytes:
    """DB 테이블(pension_events 행 리스트)을 xlsx 바이트로 직렬화. openpyxl 사용."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "pension_events"
    ws.append([label for _, label in _XLSX_COLS])
    for c in ws[1]:
        c.font = Font(bold=True)
    # 진행중 우선, 증권사·종료일 순
    rows = sorted(rows or [], key=lambda e: (e.get("status") != "진행중",
                                             e.get("firm_name") or "", e.get("end_date") or "9999"))
    for r in rows:
        out = []
        for key, _ in _XLSX_COLS:
            v = r.get(key)
            if isinstance(v, bool):
                v = "○" if v else ""
            out.append("" if v is None else v)
        ws.append(out)
    widths = {"이벤트명": 36, "참여조건": 60, "혜택내용": 80, "출처URL": 50}
    for i, (_, label) in enumerate(_XLSX_COLS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths.get(label, 12)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _b(v):
    return "○" if v else ""


def _fmt_period(ev):
    s = ev.get("start_date") or "?"
    e = ev.get("end_date") or "상시"
    return f"{s} ~ {e}"


def build_report(diff: dict, firms_failed: list) -> str:
    today = dt.date.today()
    active = sorted(diff["active"], key=lambda e: (e["firm_name"], e.get("end_date") or "9999"))
    lines = [
        f"# 연금 이벤트 리포트 ({today.isoformat()} 기준)",
        "",
        "## 요약",
        f"진행중 {len(active)}건 | 🆕 신규 {len(diff['new'])}건 | "
        f"🔚 종료 {len(diff['closed'])}건 | ✏️ 변경 {len(diff['changed'])}건 (직전 대비)",
        "",
    ]
    if firms_failed:
        lines += [f"⚠️ 수집 실패: {', '.join(firms_failed)} (재시도 예정, 직전 데이터 유지)", ""]

    lines.append("## 직전 대비 주요 변동")
    if not (diff["new"] or diff["closed"] or diff["changed"]):
        lines.append("- 변동 없음")
    for ev in diff["new"]:
        lines.append(f"- 🆕 {ev['firm_name']} 「{ev['event_name']}」 ({_fmt_period(ev)})")
    for ev in diff["closed"]:
        lines.append(f"- 🔚 {ev['firm_name']} 「{ev['event_name']}」 종료")
    for ev, f, o, n in diff["changed"]:
        o1 = str(o or "").replace("\n", " · ")[:80]
        n1 = str(n or "").replace("\n", " · ")[:80]
        lines.append(f"- ✏️ {ev['firm_name']} 「{ev['event_name']}」 {f}: {o1} → {n1}")
    lines.append("")

    soon = [e for e in active if e.get("end_date")
            and 0 <= (dt.date.fromisoformat(e["end_date"]) - today).days <= 7]
    lines.append("## 종료 임박 (7일 이내 마감)")
    if soon:
        for ev in sorted(soon, key=lambda e: e["end_date"]):
            dday = (dt.date.fromisoformat(ev["end_date"]) - today).days
            lines.append(f"- {ev['firm_name']} 「{ev['event_name']}」 {ev['end_date']} 마감 (D-{dday})")
    else:
        lines.append("- 해당 없음")
    lines.append("")

    lines += [
        "## 진행중 이벤트 현황 (증권사별)",
        "| 증권사 | 이벤트명 | 기간 | 연금저축 | IRP | DC | 기타 | 혜택 요약 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for ev in active:
        # 표준 형식 혜택은 줄바꿈 포함 → 표 셀에선 ' · '로 접어 한 줄로(원문/xlsx는 줄바꿈 유지)
        benefits = (ev.get("benefits") or ev.get("remarks") or "")
        benefits = benefits.replace("\n", " · ").replace("|", "/")[:70]
        name = ev["event_name"][:40].replace("|", "/")
        url = ev.get("event_url") or ""
        name_cell = f"[{name}]({url})" if url.startswith("http") else name
        lines.append(
            f"| {ev['firm_name']} | {name_cell} | {_fmt_period(ev)} "
            f"| {_b(ev.get('acct_pension'))} | {_b(ev.get('acct_irp'))} "
            f"| {_b(ev.get('acct_dc'))} | {ev.get('acct_etc') or ''} | {benefits} |")
    lines.append("")

    # 인사이트: 규칙 기반 1차 (LLM 고도화는 후속 단계)
    lines.append("## 인사이트")
    by_firm = {}
    for ev in active:
        by_firm.setdefault(ev["firm_name"], []).append(ev)
    irp_n = sum(1 for e in active if e.get("acct_irp"))
    ps_n = sum(1 for e in active if e.get("acct_pension"))
    most = max(by_firm.items(), key=lambda kv: len(kv[1]))[0] if by_firm else None
    none_firms = [f for f in ("미래에셋증권", "한국투자증권", "삼성증권", "키움증권", "KB증권", "NH투자증권")
                  if f not in by_firm and f not in firms_failed]
    if most:
        lines.append(f"- 진행중 이벤트 최다 증권사: {most} ({len(by_firm[most])}건)")
    lines.append(f"- 대상계좌 분포: 연금저축 {ps_n}건 / IRP {irp_n}건")
    if diff["new"]:
        lines.append(f"- 신규 {len(diff['new'])}건 — "
                     + ", ".join(sorted({e['firm_name'] for e in diff['new']})))
    if none_firms:
        lines.append(f"- 연금 이벤트 미진행: {', '.join(none_firms)}")
    lines.append("")

    review = [e for e in active if e.get("remarks")]
    if review:
        lines.append("## 검토 필요")
        for ev in review:
            lines.append(f"- {ev['firm_name']} 「{ev['event_name']}」 — {ev['remarks']}")
        lines.append("")

    lines.append(f"---\n*자동 생성: pension_monitor / 데이터 출처: 각 증권사 공식 홈페이지*")
    return "\n".join(lines)
