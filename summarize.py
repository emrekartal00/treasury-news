"""summarize.py - per-report AI summary (validated JSON) into REPORT_SUMMARY.

Finds STORED reports without a summary, calls the Qwen endpoint, validates the JSON,
stores it, and flips reports.status to SUMMARIZED. Idempotent (re-run picks up the rest).

Run:  python summarize.py [--limit N]
"""
import argparse
import json
import os
from pathlib import Path

import oracledb

import ai
import db_conn
import window

HERE = Path(__file__).parent
SYSTEM_PROMPT = (HERE / "prompts" / "summary.system.txt").read_text(encoding="utf-8")
PROMPT_VER = "s1"
MODEL_LABEL = os.environ.get("LLM_MODEL") or "qwen-gguf"
MAX_INPUT_CHARS = 600000          # ~230k tokens; leaves output room in the ~262k window
STANCES = {"bullish", "bearish", "neutral", "mixed", "n/a"}


def build_user(title, headline, text):
    body = text or ""
    if len(body) > MAX_INPUT_CHARS:
        body = body[:MAX_INPUT_CHARS]
    return f"TITLE: {title or ''}\nHEADLINE: {headline or ''}\n\nREPORT TEXT:\n{body}"


def validate(obj):
    """Coerce the model output into the strict schema; raise if unusable."""
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    kps_in = obj.get("key_points") or []
    if not isinstance(kps_in, list) or not kps_in:
        raise ValueError("key_points missing/empty")
    kps = []
    for kp in kps_in[:6]:
        if isinstance(kp, dict):
            kps.append({"point": str(kp.get("point", ""))[:500],
                        "evidence": str(kp.get("evidence", ""))[:500]})
        else:
            kps.append({"point": str(kp)[:500], "evidence": ""})
    stance = str(obj.get("stance", "n/a")).lower().strip()
    if stance not in STANCES:
        stance = "n/a"
    headline = str(obj.get("headline", "")).strip()[:1000] or kps[0]["point"][:200]
    return {
        "headline": headline,
        "key_points": kps,
        "instruments": [str(x)[:60] for x in (obj.get("instruments") or [])][:20],
        "stance": stance,
        "risk_flags": [str(x)[:300] for x in (obj.get("risk_flags") or [])][:20],
        "one_paragraph": str(obj.get("one_paragraph", "")).strip()[:1200],
    }


def pending(cur, limit, cutoff=None):
    """Reports needing a summary (have text, no summary row). `cutoff` (YYYY-MM-DD) limits to
    reports published on/after it; None = no date limit (a full backfill)."""
    where = "WHERE s.report_id IS NULL AND rt.plain_text IS NOT NULL"
    binds = {}
    if cutoff is not None:
        where += " AND r.publication_date >= TO_DATE(:cutoff, 'YYYY-MM-DD')"
        binds["cutoff"] = cutoff
    cur.execute(f"""
        SELECT r.report_id, r.title, r.distribution_headline, rt.plain_text
        FROM reports r
        JOIN report_text rt ON rt.report_id = r.report_id
        LEFT JOIN report_summary s ON s.report_id = r.report_id
        {where}
        ORDER BY r.publication_ts DESC
    """, binds)
    rows = cur.fetchall()
    return rows[:limit] if limit else rows


def store_summary(con, rid, data, tokens, status="OK"):
    cur = con.cursor()
    sj = json.dumps(data, ensure_ascii=False).encode("utf-8")
    cur.setinputsizes(sj=oracledb.DB_TYPE_BLOB, hl=oracledb.DB_TYPE_NVARCHAR)
    cur.execute("""
        MERGE INTO report_summary t USING (SELECT :id AS report_id FROM dual) s
        ON (t.report_id = s.report_id)
        WHEN MATCHED THEN UPDATE SET summary_json=:sj, headline=:hl, model=:m, prompt_ver=:pv,
             input_tokens=:it, generated_at=SYSTIMESTAMP, status=:status
        WHEN NOT MATCHED THEN INSERT (report_id, summary_json, headline, model, prompt_ver,
             input_tokens, generated_at, status)
        VALUES (:id, :sj, :hl, :m, :pv, :it, SYSTIMESTAMP, :status)
    """, {"id": rid, "sj": sj, "hl": data["headline"], "m": MODEL_LABEL,
          "pv": PROMPT_VER, "it": tokens, "status": status})
    cur.execute("UPDATE reports SET status='SUMMARIZED' WHERE report_id=:id", {"id": rid})
    con.commit()
    cur.close()


def is_no_content(obj):
    """The summary prompt returns a NO_CONTENT object when the input isn't research
    (failed extraction, disclaimers only, etc.)."""
    o = obj or {}
    return (str(o.get("headline", "")).strip().upper() == "NO_CONTENT"
            or str(o.get("one_paragraph", "")).strip().upper() == "NO_CONTENT")


NO_CONTENT = {"headline": "NO_CONTENT", "key_points": [], "instruments": [],
              "stance": "n/a", "risk_flags": [], "one_paragraph": "NO_CONTENT"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--days", type=int, default=None,
                    help="backfill: summarize reports published in the last N days "
                         "(overrides the normal active window)")
    ap.add_argument("--all", action="store_true",
                    help="backfill: summarize ALL un-summarized reports, ignore the date window")
    args, _ = ap.parse_known_args()

    if args.all:
        cutoff = None                                   # full backfill
    elif args.days is not None:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()
    else:
        cutoff = window.min_pub_date()                  # normal daily window

    con = db_conn.connect()
    cur = con.cursor()
    rows = pending(cur, args.limit, cutoff)
    scope = "all dates" if cutoff is None else f"published >= {cutoff}"
    print(f"{len(rows)} report(s) need a summary ({scope}).")
    ok = skipped = fail = 0
    for rid, title, headline, text in rows:
        try:
            user = build_user(title, headline, text)
            tokens = ai.estimate_tokens(user)
            content, finish = ai.chat(SYSTEM_PROMPT, user, max_tokens=2048)
            if finish == "length":
                raise ai.LLMError("truncated output (finish_reason=length)")
            obj = ai.parse_json(content)
            # Non-research / failed extraction: store as NO_CONTENT so it is not retried and
            # stays out of the digest (which only takes status='OK').
            if is_no_content(obj):
                store_summary(con, rid, NO_CONTENT, tokens, status="NO_CONTENT")
                skipped += 1
                print(f"  [skip] {rid[:8]}  NO_CONTENT")
                continue
            data = validate(obj)
            store_summary(con, rid, data, tokens)
            ok += 1
            print(f"  [ok]   {rid[:8]}  {data['stance']:8} {data['headline'][:60]}")
        except Exception as exc:
            fail += 1
            print(f"  [FAIL] {rid[:8]}  {exc}")
    cur.close()
    con.close()
    print(f"\nSummarize done. ok={ok} skipped(NO_CONTENT)={skipped} failed={fail}")


if __name__ == "__main__":
    main()
