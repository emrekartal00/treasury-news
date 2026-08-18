"""pipeline.py - run the daily pipeline end to end.

Stages, in order:  scrape -> ingest -> summarize -> digest -> mail
Each stage is its own script (also runnable on its own). This runner invokes them as
subprocesses so one language/venv drives all of them and failures are isolated.

The summarize stage is RETRIED (up to SUMMARIZE_PASSES, default 5) until no report is left
un-summarized, so the occasional transient LLM error gets another pass. A NO_CONTENT report
is stored with its own status, so it never counts as pending and can't loop forever.

Run:
  python pipeline.py                 # all stages
  python pipeline.py --only ingest   # just one stage
  python pipeline.py --from summarize# from a stage to the end
  python pipeline.py -- --days 3      # args after `--` go to the scrape stage (daily.py)

Multi-source: the scrape stage runs daily.py once per portal listed in SOURCES (comma-
separated, default 'gs'). The other stages are source-agnostic.
"""
import argparse
import os
import subprocess
import sys

import db_conn

STAGES = [
    ("scrape", "daily.py"),
    ("ingest", "ingest.py"),
    ("summarize", "summarize.py"),
    ("digest", "digest.py"),
    ("mail", "mailer.py"),
]

MAX_SUMMARIZE_PASSES = int(os.environ.get("SUMMARIZE_PASSES", "5"))


def pending_summaries():
    """How many reports still need a summary (have text, no summary row yet).
    Matches summarize.pending()'s WHERE clause."""
    con = db_conn.connect()
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM reports r
            JOIN report_text rt ON rt.report_id = r.report_id
            LEFT JOIN report_summary s ON s.report_id = r.report_id
            WHERE s.report_id IS NULL AND rt.plain_text IS NOT NULL
        """)
        return cur.fetchone()[0]
    finally:
        con.close()


def run_summarize():
    """Retry summarize.py until nothing is pending (transient LLM errors get another pass)."""
    for attempt in range(1, MAX_SUMMARIZE_PASSES + 1):
        rc = subprocess.run([sys.executable, "summarize.py"]).returncode
        if rc != 0:
            print(f"[STOP] summarize crashed (code {rc})")
            sys.exit(rc)
        remaining = pending_summaries()
        if remaining == 0:
            print(f"[summarize] all reports summarized (after {attempt} pass(es)).")
            return
        print(f"[summarize] {remaining} still need a summary after pass {attempt}; "
              f"retrying ({attempt}/{MAX_SUMMARIZE_PASSES})...")
    print(f"[summarize] {pending_summaries()} still failing after "
          f"{MAX_SUMMARIZE_PASSES} passes - continuing without them.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run only this stage")
    ap.add_argument("--from", dest="frm", help="run from this stage to the end")
    args, extra = ap.parse_known_args()

    names = [n for n, _ in STAGES]
    if args.only:
        if args.only not in names:
            print(f"unknown stage '{args.only}'. choices: {', '.join(names)}")
            sys.exit(2)
        selected = [(n, s) for n, s in STAGES if n == args.only]
    elif args.frm:
        if args.frm not in names:
            print(f"unknown stage '{args.frm}'. choices: {', '.join(names)}")
            sys.exit(2)
        selected = STAGES[names.index(args.frm):]
    else:
        selected = STAGES

    src_list = [s.strip() for s in os.environ.get("SOURCES", "gs").split(",") if s.strip()]

    for name, script in selected:
        print(f"\n===== stage: {name} ({script}) =====")
        if name == "scrape":
            # One scrape run per configured portal; extra args (e.g. --days 3) go to each.
            for src in src_list:
                print(f"----- source: {src} -----")
                rc = subprocess.run([sys.executable, script, "--source", src, *extra]).returncode
                if rc != 0:
                    print(f"[WARN] scrape of '{src}' exited with code {rc}; continuing.")
            continue
        if name == "summarize":
            run_summarize()
            continue
        rc = subprocess.run([sys.executable, script]).returncode
        if rc != 0:
            print(f"[STOP] stage '{name}' exited with code {rc}")
            sys.exit(rc)

    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
