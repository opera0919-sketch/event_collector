# -*- coding: utf-8 -*-
"""Gmail SMTP 발송. MAIL_SENDER/MAIL_APP_PASSWORD 미설정 시 스킵."""

import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import MAIL_SENDER, MAIL_APP_PASSWORD, MAIL_RECIPIENTS


def enabled() -> bool:
    return bool(MAIL_SENDER and MAIL_APP_PASSWORD and MAIL_RECIPIENTS)


def _md_to_html(md: str) -> str:
    """간이 마크다운 → HTML (표/헤더/리스트만)."""
    html_lines, in_table, in_list = [], False, False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= {"-", " ", ":"} for c in cells):
                continue
            if not in_table:
                html_lines.append("<table border='1' cellpadding='4' "
                                  "style='border-collapse:collapse;font-size:13px'>")
                in_table = True
                html_lines.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
            else:
                html_lines.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            html_lines.append("</table>")
            in_table = False
        if line.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        if line.startswith("# "):
            html_lines.append(f"<h2>{line[2:]}</h2>")
        elif line.startswith("## "):
            html_lines.append(f"<h3>{line[3:]}</h3>")
        elif line.strip() == "---":
            html_lines.append("<hr>")
        elif line.strip():
            html_lines.append(f"<p>{line}</p>")
    if in_table:
        html_lines.append("</table>")
    if in_list:
        html_lines.append("</ul>")
    import re
    html = "\n".join(html_lines)
    html = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', html)
    html = html.replace("**", "")
    return f"<html><body style='font-family:sans-serif'>{html}</body></html>"


def send(subject: str, report_md: str, attachments=None) -> bool:
    """attachments: [(filename, bytes, mime_subtype)] — 예: ('events.xlsx', b'...',
    'vnd.openxmlformats-officedocument.spreadsheetml.sheet'). DB 테이블 xlsx 첨부용."""
    if not enabled():
        print("[mailer] SMTP 미설정 → 발송 스킵")
        return False
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = MAIL_SENDER
    msg["To"] = ", ".join(MAIL_RECIPIENTS)
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(report_md, "plain", "utf-8"))
    body.attach(MIMEText(_md_to_html(report_md), "html", "utf-8"))
    msg.attach(body)
    for fn, data, subtype in (attachments or []):
        part = MIMEApplication(data, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=fn)
        msg.attach(part)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(MAIL_SENDER, MAIL_APP_PASSWORD)
        s.sendmail(MAIL_SENDER, MAIL_RECIPIENTS, msg.as_string())
    print(f"[mailer] 발송 완료 → {', '.join(MAIL_RECIPIENTS)}")
    return True
