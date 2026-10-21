"""mailer.py - compose + send the daily digest email; idempotent via EMAIL_LOG.

Loads DAILY_DIGEST for a date, renders an HTML email (overview + one card per report,
with links to the web app), sends it through the LOCAL OUTLOOK desktop app via COM
(pywin32) - no SMTP, sends as the logged-in Outlook/Exchange account - and records the
result in EMAIL_LOG. Never double-sends a day (unless --force).

EMAIL_LOG.report_ids (migration 003) keeps the ids that actually went out, IN EMAIL
ORDER, so the PRISMA library can show "the latest email" on top of its list. The
column is optional: before 003 is run the log row is written without it.

Title rule (shared with the PRISMA library): a card's title is the report's own
title (REPORTS.TITLE); the summary headline sits under it as a grey line, unless it
is empty, NO_CONTENT or the same text as the title.

Executive summary (owner decision D17, 2026-10-20): on top of the cards, ONE Turkish
summary across the day's research, built by exec_summary.py (port of treasury-report's
research_summary): the first 30 reports are scanned - the mail's own reports in mail order,
then the rest of the day's digest - every sentence cites its report(s) [R#] (linked to the
same pages as the cards, and each card shows its own [R#]), numbers are slots the code fills
from the reports, the code's template is used when the model's text fails the checks. It
never stops the mail: an error or the time limit (EXEC_SUMMARY_TIMEOUT) sends the cards
without it. Switches below (hand-edited, default OFF):
  EXEC_SUMMARY = True          the summary goes into the real mail;
  EXEC_SUMMARY_DRY_RUN = True  the mail WITH the summary is written to
                               state/exec_summary_dry_run.html; nothing is sent, nothing is
                               logged, and an already-sent day can be dry-run again. It acts
                               ONLY when mailer.py is run by hand (F5 / its own main()): inside
                               pipeline.py (which sets PIPELINE_ENV) it is ignored and the real
                               mail goes out as usual.
exec_summary.py is imported lazily: if it is missing or broken, the mail goes without it.

Run:  python mailer.py [--date YYYY-MM-DD] [--force] [--preview]
  --preview opens the mail in Outlook for review instead of sending.
"""
import argparse
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import oracledb

import ai
import curate
import db_conn

# ═══════════════════════════════════════════════ CONFIG (edit by hand)
EXEC_SUMMARY = False            # True: the Turkish executive summary goes on top of the mail
EXEC_SUMMARY_DRY_RUN = False    # True: write the mail WITH the summary to DRY_RUN_FILE; send nothing
EXEC_SUMMARY_TIMEOUT = 120      # seconds for the whole summary (every model call); then mail without it
# ════════════════════════════════════════════════════════════════════════

DRY_RUN_FILE = Path(__file__).parent / "state" / "exec_summary_dry_run.html"
#: pipeline.py sets this in the mail stage's environment: the dry run never acts there.
PIPELINE_ENV = "TREASURY_NEWS_PIPELINE"


def _exec_summary():
    """exec_summary.py, or None when it is missing or fails to import (logged) - the mail
    then goes out without the summary."""
    try:
        import exec_summary
        return exec_summary
    except Exception as exc:                     # noqa: BLE001 - never stops the mail
        print(f"[summary] exec_summary.py not usable ({exc}); mailing without the summary.")
        return None


def db_today(cur):
    cur.execute("SELECT TO_CHAR(SYSDATE, 'YYYY-MM-DD') FROM dual")
    return cur.fetchone()[0]


def already_sent(cur, date_str):
    cur.execute("SELECT COUNT(*) FROM email_log WHERE digest_date = TO_DATE(:d,'YYYY-MM-DD') AND status='SENT'",
                {"d": date_str})
    return cur.fetchone()[0] > 0


def load_digest(cur, date_str):
    cur.execute("SELECT overview_json, report_ids FROM daily_digest WHERE digest_date = TO_DATE(:d,'YYYY-MM-DD')",
                {"d": date_str})
    row = cur.fetchone()
    if not row:
        return None, []
    overview = db_conn.as_json(row[0]) or {}
    report_ids = db_conn.as_json(row[1]) or []
    return overview, report_ids


def load_report(cur, rid):
    cur.execute("""
        SELECT r.title, r.distribution_headline, r.authors, r.source_path, r.download_path,
               s.summary_json, r.source, TO_CHAR(r.publication_date,'YYYY-MM-DD'), r.report_types
        FROM reports r LEFT JOIN report_summary s ON s.report_id = r.report_id
        WHERE r.report_id = :id
    """, {"id": rid})
    row = cur.fetchone()
    if not row:
        return None
    title, headline, authors_b, src, dl, sj, source, pub_date, rtypes_b = row
    return {
        "id": rid, "title": title, "headline": headline,
        "authors": db_conn.as_json(authors_b) or [],
        "source_path": src, "download": dl, "source": source or "gs",
        "date": pub_date,
        "report_types": db_conn.as_json(rtypes_b) or [],     # the bank's own labels (summary's equity rule)
        "summary": db_conn.as_json(sj) or {},
    }


def summary_line(title, summary):
    """The grey second line under the title: the summary headline, unless it is
    empty, NO_CONTENT or the title itself. Same rule as the PRISMA library
    (customs/arastirma/data_source.py::alt_satir)."""
    line = ((summary or {}).get("headline") or "").strip()
    if line in ("", "NO_CONTENT"):
        return None
    if line.casefold() == (title or "").strip().casefold():
        return None
    return line


def has_report_ids_column(cur):
    """True once migration 003 has added EMAIL_LOG.report_ids."""
    try:
        cur.execute("SELECT report_ids FROM email_log WHERE 1 = 0")
        cur.fetchall()
        return True
    except oracledb.DatabaseError:
        return False


def log_email(cur, row, report_ids):
    """Insert one EMAIL_LOG row; report_ids (email order) only when the column exists,
    so a missing migration never breaks the log that prevents double sends."""
    cols = ["id", "digest_date", "subject", "recipients", "status", "message_id", "error", "sent_at"]
    vals = [":id", "TO_DATE(:d,'YYYY-MM-DD')", ":subj", ":rcpt", ":status", ":mid", ":err",
            "SYSTIMESTAMP"]
    sizes = dict(rcpt=oracledb.DB_TYPE_NCLOB, subj=oracledb.DB_TYPE_NVARCHAR,
                 err=oracledb.DB_TYPE_NCLOB)
    params = dict(row)
    if has_report_ids_column(cur):
        cols.append("report_ids")
        vals.append(":rids")
        sizes["rids"] = oracledb.DB_TYPE_BLOB
        params["rids"] = json.dumps(report_ids, ensure_ascii=False).encode("utf-8")
    cur.setinputsizes(**sizes)
    cur.execute(f"INSERT INTO email_log ({', '.join(cols)}) VALUES ({', '.join(vals)})", params)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def count_stances(reports):
    """Recompute overview counts for the (possibly curated) set actually in the email."""
    by = {}
    for r in reports:
        st = (r.get("summary") or {}).get("stance") or "n/a"
        by[st] = by.get(st, 0) + 1
    return {"total": len(reports), "by_stance": by}


# --- email styling (editorial research note; email-safe: tables + inline CSS + web-safe fonts) ---
_INK = "#1c1c1a"
_MUTED = "#6f6b64"
_BODY = "#2b2a27"
_HAIR = "#e6e3dc"
_ACCENT = "#0b5f57"
_ACCENT_DK = "#083f3a"
_CANVAS = "#f7f6f2"
_CARD = "#ffffff"
_SERIF = "Georgia,'Times New Roman',serif"
_SANS = "Arial,Helvetica,sans-serif"


def _button(read_href):
    # Single primary button; the report page it opens already renders the PDF, so a
    # separate PDF button would be redundant. Darker bottom edge reads as pressable in Outlook.
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td bgcolor="{_ACCENT}" style="border-radius:6px;border-bottom:3px solid {_ACCENT_DK};'
            f'padding:13px 28px;text-align:center;mso-padding-alt:13px 28px;">'
            f'<a href="{esc(read_href)}" style="font-family:{_SANS};font-size:13px;font-weight:bold;'
            f'color:#ffffff;text-decoration:none;letter-spacing:.06em;">'
            f'READ REPORT</a></td></tr></table>')


def _key_points(kps):
    rows = []
    for kp in (kps or [])[:4]:
        pt = esc(kp.get("point", "") if isinstance(kp, dict) else str(kp))
        if not pt:
            continue
        rows.append(
            f'<tr><td valign="top" width="18" style="font-family:{_SERIF};font-size:15px;'
            f'line-height:1.55;color:{_ACCENT};">&#8212;</td>'
            f'<td style="font-family:{_SANS};font-size:14px;line-height:1.55;color:{_BODY};'
            f'padding-bottom:5px;">{pt}</td></tr>')
    if not rows:
        return ""
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            'style="margin-top:8px;">' + "".join(rows) + "</table>")


def _card(r, base_url, rnum=None):
    s = r.get("summary") or {}
    rid = r["id"]
    read = report_href(rid, base_url, r.get("source_path")) or "#"
    raw_title = r.get("title") or r.get("headline") or "(untitled)"
    title = esc(raw_title)
    line = summary_line(raw_title, s)
    authors = esc(", ".join(r["authors"])) if r.get("authors") else ""
    src_label = esc((r.get("source") or "").upper())
    if rnum:                       # the card's own [R#]: the summary's citations point here
        src_label = f"[{esc(rnum)}] {src_label}"

    inner = [f'<a href="{esc(read)}" style="font-family:{_SANS};font-size:18px;line-height:1.35;'
             f'font-weight:bold;color:{_INK};text-decoration:none;">{title}</a>']
    if line:
        inner.append(f'<div style="font-family:{_SANS};font-size:14px;line-height:1.45;'
                     f'color:{_MUTED};margin-top:4px;">{esc(line)}</div>')
    if authors or src_label:
        src_html = (f'<span style="font-family:{_SANS};font-size:11px;font-weight:bold;'
                    f'letter-spacing:.05em;color:{_ACCENT};">{src_label}</span>'
                    if src_label else "")
        sep = ' &nbsp;&middot;&nbsp; ' if (src_label and authors) else ""
        inner.append(
            f'<div style="margin:8px 0 12px;">{src_html}{sep}'
            f'<span style="font-family:{_SANS};font-size:12px;color:{_MUTED};">{authors}</span></div>')
    if s.get("one_paragraph"):
        inner.append(f'<div style="font-family:{_SANS};font-size:15px;line-height:1.6;'
                     f'color:{_BODY};">{esc(s["one_paragraph"])}</div>')
    elif not s:
        inner.append(f'<div style="font-family:{_SANS};font-size:13px;color:{_MUTED};'
                     f'font-style:italic;">Summary unavailable.</div>')
    kp_html = _key_points(s.get("key_points"))
    if kp_html:
        inner.append(kp_html)
    inner.append(f'<div style="margin-top:16px;">{_button(read)}</div>')

    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'bgcolor="{_CARD}" style="background:{_CARD};border:1px solid {_HAIR};'
            f'border-radius:6px;"><tr><td style="padding:24px 28px;">'
            f'{"".join(inner)}</td></tr></table>')


def report_href(rid, base_url, source_path=None):
    """THE "read" target of a report - the cards and the summary's [R#] links both use it:
    the web app's report page, else the report's own source link, else None."""
    return f"{base_url}/reports/{quote(str(rid), safe='')}" if base_url else (source_path or None)


def _model_ask(deadline, state):
    """``ask(system, user)`` for exec_summary.write: one ai.chat call (the existing client,
    called as curate.py calls it), its HTTP timeout cut to what is left of the time limit.
    Past the limit it raises and marks ``state["timed_out"]``."""
    def ask(system, user):
        left = deadline - time.monotonic()
        if left <= 1:
            state["timed_out"] = True
            raise TimeoutError("executive summary time limit reached")
        try:
            content, _ = ai.chat(system, user, temperature=0.2, max_tokens=2048, timeout=left)
        except Exception:
            if time.monotonic() >= deadline - 1:
                state["timed_out"] = True
            raise
        return content
    return ask


def build_summary(sent, all_reports, date_str, base_url="", ask=None, timeout=None):
    """The executive summary dict (exec_summary.summarize + "rnum"/"href"), or None: no
    usable report, an error, or the time limit - the mail then goes out without it.
    ``sent`` = the mail's reports (mail order), ``all_reports`` = the day's digest."""
    xs = _exec_summary()
    if xs is None:
        return None
    timeout = EXEC_SUMMARY_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    state = {}
    ask = ask or _model_ask(deadline, state)
    box = {}

    def work():
        try:
            picked = xs.pick_reports(sent, all_reports)
            summary = xs.summarize(picked, ask, date_str)
            if summary is not None:
                summary["rnum"] = {r["id"]: "R%d" % i for i, r in enumerate(picked, 1)}
                summary["href"] = {r["id"]: report_href(r["id"], base_url, r.get("source_path"))
                                   for r in picked}       # digest-only reports link too
            box["summary"] = summary
        except Exception as exc:                 # noqa: BLE001 - the mail goes out without it
            box["error"] = exc

    worker = threading.Thread(target=work, name="exec-summary", daemon=True)
    worker.start()
    worker.join(max(timeout, 0))
    if worker.is_alive() or state.get("timed_out"):
        print(f"[summary] skipped: time limit ({timeout}s) reached; mailing without it.")
        return None
    if "error" in box:
        print(f"[summary] skipped: {box['error']}")
        return None
    return box.get("summary")


def render(overview, reports, base_url, summary=None):
    date = esc(overview.get("date", ""))
    n = overview.get("counts", {}).get("total", len(reports))

    masthead = (
        f'<div style="font-family:{_SANS};font-size:11px;font-weight:bold;letter-spacing:.22em;'
        f'color:{_ACCENT};">DAILY RESEARCH DIGEST</div>'
        f'<div style="font-family:{_SERIF};font-size:26px;line-height:1.15;color:{_INK};'
        f'margin:6px 0 3px;">{date}</div>'
        f'<div style="font-family:{_SANS};font-size:13px;color:{_MUTED};">'
        f'{n} report{"s" if n != 1 else ""}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="margin:14px 0 22px;"><tr>'
        f'<td height="2" bgcolor="{_ACCENT}" style="font-size:0;line-height:0;">&nbsp;</td>'
        f'</tr></table>')

    hrefs = dict((summary or {}).get("href") or {})

    def href(rid):
        return hrefs.get(rid) or report_href(rid, base_url)

    xs = _exec_summary() if summary else None
    if xs is None:
        summary = None
    rnum = (summary or {}).get("rnum") or {}
    cards = []
    if summary:
        cards.append(xs.to_html(summary, href))
        cards.append('<div style="height:16px;line-height:16px;font-size:0;">&nbsp;</div>')
    for r in reports:
        cards.append(_card(r, base_url, rnum.get(r["id"])))
        cards.append('<div style="height:16px;line-height:16px;font-size:0;">&nbsp;</div>')

    digest_link = ""
    if base_url:
        digest_link = (f' &nbsp;&middot;&nbsp; <a href="{esc(base_url)}/digest/{date}" '
                       f'style="color:{_MUTED};text-decoration:underline;">View full digest</a>')
    footer = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="margin-top:8px;"><tr>'
        f'<td height="1" bgcolor="{_HAIR}" style="font-size:0;line-height:0;">&nbsp;</td></tr>'
        f'<tr><td style="padding:14px 0;font-family:{_SANS};font-size:11px;line-height:1.6;'
        f'color:{_MUTED};">Internal use only.{digest_link}</td></tr></table>')

    pre = f"{n} new research report{'s' if n != 1 else ''} for {overview.get('date','')}."
    preheader = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
                 f'font-size:1px;line-height:1px;color:{_CANVAS};">{esc(pre)}'
                 + "&nbsp;&zwnj;" * 30 + '</div>')

    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only">'
        '</head>'
        f'<body style="margin:0;padding:0;background:{_CANVAS};">'
        + preheader
        + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{_CANVAS}" style="background:{_CANVAS};"><tr>'
        '<td align="center" style="padding:30px 12px 44px;">'
        '<!--[if mso]><table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="1200"><tr><td><![endif]-->'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="1200" '
        'style="width:1200px;max-width:1200px;">'
        f'<tr><td style="padding:0 8px;">{masthead}{"".join(cards)}{footer}</td></tr></table>'
        '<!--[if mso]></td></tr></table><![endif]-->'
        '</td></tr></table></body></html>')

    # plain-text fallback (Outlook uses HTMLBody; kept for completeness)
    text = [f"Daily Research Digest - {overview.get('date','')} - {n} report(s)", ""]
    if summary:
        text += [xs.to_text(summary, href), ""]
    for r in reports:
        s = r.get("summary") or {}
        title = r.get("title") or r.get("headline") or "(untitled)"
        text.append("* " + title)
        line = summary_line(title, s)
        if line:
            text.append("  " + line)
        if s.get("one_paragraph"):
            text.append("  " + s["one_paragraph"])
        link = report_href(r["id"], base_url, r.get("source_path")) or ""
        if link:
            text.append("  " + link)
        text.append("")
    return html, "\n".join(text)


def send(subject, html, preview=False):
    """Send via the local Outlook desktop app (COM / pywin32). Windows + Outlook only.
    Sends as the logged-in Outlook/Exchange account - no SMTP credentials needed.
    MAIL_FROM (optional) sends on behalf of a shared mailbox you have permission for."""
    to = os.environ.get("MAIL_TO")
    cc = os.environ.get("MAIL_CC")
    sender = os.environ.get("MAIL_FROM")
    if not to:
        raise RuntimeError("Set MAIL_TO in .env")

    try:
        import pythoncom
        pythoncom.CoInitialize()  # safe if already initialized; needed in some contexts
    except Exception:
        pass
    import win32com.client  # pywin32

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to.replace(",", ";")   # Outlook separates recipients with ';'
    if cc:
        mail.CC = cc.replace(",", ";")
    if sender:
        mail.SentOnBehalfOfName = sender
    mail.Subject = subject
    mail.HTMLBody = html
    if preview:
        mail.Display(True)   # open in Outlook for review; does NOT send
        return "displayed"
    mail.Send()
    return "outlook"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--preview", action="store_true", help="open in Outlook instead of sending")
    ap.add_argument("--no-curate", action="store_true", help="skip AI curation; email every report")
    args, _ = ap.parse_known_args()

    con = db_conn.connect()
    cur = con.cursor()
    date_str = args.date or db_today(cur)

    dry_run = EXEC_SUMMARY_DRY_RUN and not os.environ.get(PIPELINE_ENV)
    if EXEC_SUMMARY_DRY_RUN and not dry_run:
        print("[dry run] EXEC_SUMMARY_DRY_RUN is ignored inside the pipeline; sending as usual.")
    if not args.force and not dry_run and already_sent(cur, date_str):
        print(f"Email already SENT for {date_str}; skipping (use --force).")
        con.close()
        return

    overview, ids = load_digest(cur, date_str)
    if not ids:
        print(f"No digest/reports for {date_str}; nothing to send.")
        con.close()
        return

    reports = [r for r in (load_report(cur, rid) for rid in ids) if r]
    all_reports = list(reports)               # the day's digest, for the summary's scan

    # AI curation: trim the email to the reports worth pushing. The web archive, the
    # homepage, and the "View full digest" link all still show every report.
    if not args.no_curate:
        reports = curate.select(reports)
    if not reports:
        print(f"Curation excluded every report for {date_str}; nothing to send.")
        con.close()
        return
    overview["counts"] = count_stances(reports)
    sent_ids = [r["id"] for r in reports]     # email order = curated order

    base = os.environ.get("WEBAPP_BASE_URL", "").rstrip("/")
    subject = f"Daily Research Digest - {date_str} - {len(reports)} report(s)"
    summary = None
    if EXEC_SUMMARY or dry_run:
        summary = build_summary(reports, all_reports, date_str, base)
        if summary:
            print(f"[summary] {summary['status']}: {len(summary['bullets'])} bullet(s) over "
                  f"{len(summary['refs'])} cited report(s).")
    html, text = render(overview, reports, base, summary)

    if dry_run:
        DRY_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        DRY_RUN_FILE.write_text(html, encoding="utf-8")
        print(f"[dry run] mail for {date_str} written to {DRY_RUN_FILE} - nothing sent, nothing logged.")
        con.close()
        return

    log_id = str(uuid.uuid4())
    recipients = os.environ.get("MAIL_TO", "")
    try:
        result = send(subject, html, preview=args.preview)
        if args.preview:
            print(f"Opened digest for {date_str} in Outlook for review (not sent).")
            con.close()
            return
        log_email(cur, {"id": log_id, "d": date_str, "subj": subject[:500], "rcpt": recipients,
                        "status": "SENT", "mid": (result or "")[:300], "err": None},
                  sent_ids)
        con.commit()
        print(f"Sent digest for {date_str} to {recipients} ({len(reports)} reports).")
    except Exception as exc:
        log_email(cur, {"id": log_id, "d": date_str, "subj": subject[:500], "rcpt": recipients,
                        "status": "FAILED", "mid": None, "err": str(exc)[:2000]},
                  sent_ids)
        con.commit()
        print(f"[FAIL] send: {exc}")
        con.close()
        raise
    con.close()


if __name__ == "__main__":
    main()
