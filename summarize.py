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


def pending(cur, limit):
    cur.execute("""
        SELECT r.report_id, r.title, r.distribution_headline, rt.plain_text
        FROM reports r
        JOIN report_text rt ON rt.report_id = r.report_id
        LEFT JOIN report_summary s ON s.report_id = r.report_id
        WHERE s.report_id IS NULL AND rt.plain_text IS NOT NULL
        ORDER BY r.publication_ts DESC
    """)
    rows = cur.fetchall()
    return rows[:limit] if limit else rows


def store_summary(con, rid, data, tokens):
    cur = con.cursor()
    sj = json.dumps(data, ensure_ascii=False).encode("utf-8")
    cur.setinputsizes(sj=oracledb.DB_TYPE_BLOB, hl=oracledb.DB_TYPE_NVARCHAR)
    cur.execute("""
        MERGE INTO report_summary t USING (SELECT :id AS report_id FROM dual) s
        ON (t.report_id = s.report_id)
        WHEN MATCHED THEN UPDATE SET summary_json=:sj, headline=:hl, model=:m, prompt_ver=:pv,
             input_tokens=:it, generated_at=SYSTIMESTAMP, status='OK'
        WHEN NOT MATCHED THEN INSERT (report_id, summary_json, headline, model, prompt_ver,
             input_tokens, generated_at, status)
        VALUES (:id, :sj, :hl, :m, :pv, :it, SYSTIMESTAMP, 'OK')
    """, {"id": rid, "sj": sj, "hl": data["headline"], "m": MODEL_LABEL,
          "pv": PROMPT_VER, "it": tokens})
    cur.execute("UPDATE reports SET status='SUMMARIZED' WHERE report_id=:id", {"id": rid})
    con.commit()
    cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args, _ = ap.parse_known_args()

    con = db_conn.connect()
    cur = con.cursor()
    rows = pending(cur, args.limit)
    print(f"{len(rows)} report(s) need a summary.")
    ok = fail = 0
    for rid, title, headline, text in rows:
        try:
            user = build_user(title, headline, text)
            tokens = ai.estimate_tokens(user)
            content, finish = ai.chat(SYSTEM_PROMPT, user, max_tokens=2048)
            if finish == "length":
                raise ai.LLMError("truncated output (finish_reason=length)")
            data = validate(ai.parse_json(content))
            store_summary(con, rid, data, tokens)
            ok += 1
            print(f"  [ok]   {rid[:8]}  {data['stance']:8} {data['headline'][:60]}")
        except Exception as exc:
            fail += 1
            print(f"  [FAIL] {rid[:8]}  {exc}")
    cur.close()
    con.close()
    print(f"\nSummarize done. ok={ok} failed={fail}")


if __name__ == "__main__":
    main()
