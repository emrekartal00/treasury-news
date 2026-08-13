"""digest.py - build/refresh DAILY_DIGEST for a date (deterministic, no AI call).

Membership = reports fetched that day (TRUNC(scraped_at) = date) that have an OK summary.
Stores a compact overview + the ordered report_ids. Default date = the DB's current date.

Run:  python digest.py [--date YYYY-MM-DD]
"""
import argparse
import json

import oracledb

import db_conn


def db_today(cur):
    cur.execute("SELECT TO_CHAR(SYSDATE, 'YYYY-MM-DD') FROM dual")
    return cur.fetchone()[0]


def build(con, date_str):
    cur = con.cursor()
    cur.execute("""
        SELECT r.report_id, r.title, s.summary_json
        FROM reports r
        JOIN report_summary s ON s.report_id = r.report_id
        WHERE TRUNC(r.scraped_at) = TO_DATE(:d, 'YYYY-MM-DD') AND s.status = 'OK'
        ORDER BY r.publication_ts DESC NULLS LAST
    """, {"d": date_str})
    rows = cur.fetchall()
    if not rows:
        print(f"No summarized reports fetched on {date_str}; nothing to digest.")
        return 0

    items, report_ids, by_stance = [], [], {}
    for rid, title, sj in rows:
        data = db_conn.as_json(sj) or {}
        stance = data.get("stance", "n/a")
        by_stance[stance] = by_stance.get(stance, 0) + 1
        items.append({"report_id": rid, "headline": data.get("headline") or title, "stance": stance})
        report_ids.append(rid)

    overview = {"date": date_str, "counts": {"total": len(rows), "by_stance": by_stance}, "items": items}
    ov = json.dumps(overview, ensure_ascii=False).encode("utf-8")
    rids = json.dumps(report_ids, ensure_ascii=False).encode("utf-8")

    cur.setinputsizes(ov=oracledb.DB_TYPE_BLOB, rids=oracledb.DB_TYPE_BLOB)
    cur.execute("""
        MERGE INTO daily_digest t USING (SELECT TO_DATE(:d,'YYYY-MM-DD') AS digest_date FROM dual) s
        ON (t.digest_date = s.digest_date)
        WHEN MATCHED THEN UPDATE SET overview_json=:ov, report_ids=:rids, generated_at=SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (digest_date, overview_json, report_ids, generated_at)
        VALUES (TO_DATE(:d,'YYYY-MM-DD'), :ov, :rids, SYSTIMESTAMP)
    """, {"d": date_str, "ov": ov, "rids": rids})
    con.commit()
    cur.close()
    print(f"Digest for {date_str}: {len(rows)} report(s), stances={by_stance}")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args, _ = ap.parse_known_args()
    con = db_conn.connect()
    cur = con.cursor()
    date_str = args.date or db_today(cur)
    cur.close()
    build(con, date_str)
    con.close()


if __name__ == "__main__":
    main()
