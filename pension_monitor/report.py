# -*- coding: utf-8 -*-
"""주간 리포트 생성 (계획안 §7 형식)."""

import datetime as dt


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
        f"# 연금 이벤트 위클리 ({today.isoformat()} 기준)",
        "",
        "## 요약",
        f"진행중 {len(active)}건 | 🆕 신규 {len(diff['new'])}건 | "
        f"🔚 종료 {len(diff['closed'])}건 | ✏️ 변경 {len(diff['changed'])}건 (전 주 대비)",
        "",
    ]
    if firms_failed:
        lines += [f"⚠️ 수집 실패: {', '.join(firms_failed)} (재시도 예정, 직전 데이터 유지)", ""]

    lines.append("## 전 주 대비 주요 변동")
    if not (diff["new"] or diff["closed"] or diff["changed"]):
        lines.append("- 변동 없음")
    for ev in diff["new"]:
        lines.append(f"- 🆕 {ev['firm_name']} 「{ev['event_name']}」 ({_fmt_period(ev)})")
    for ev in diff["closed"]:
        lines.append(f"- 🔚 {ev['firm_name']} 「{ev['event_name']}」 종료")
    for ev, f, o, n in diff["changed"]:
        lines.append(f"- ✏️ {ev['firm_name']} 「{ev['event_name']}」 {f}: {o} → {n}")
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
        benefits = (ev.get("benefits") or ev.get("remarks") or "")[:60].replace("|", "/")
        name = ev["event_name"][:40].replace("|", "/")
        url = ev.get("event_url") or ""
        name_cell = f"[{name}]({url})" if url else name
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
                  if f not in by_firm]
    if most:
        lines.append(f"- 진행중 이벤트 최다 증권사: {most} ({len(by_firm[most])}건)")
    lines.append(f"- 대상계좌 분포: 연금저축 {ps_n}건 / IRP {irp_n}건")
    if diff["new"]:
        lines.append(f"- 이번 주 신규 {len(diff['new'])}건 — "
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
