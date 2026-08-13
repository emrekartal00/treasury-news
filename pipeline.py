"""pipeline.py - run the daily pipeline end to end.

Stages, in order:  scrape -> ingest -> summarize -> digest -> mail
Each stage is its own script (also runnable on its own). This runner invokes them as
subprocesses so one language/venv drives all of them and failures are isolated.

Run:
  python pipeline.py                 # all stages
  python pipeline.py --only ingest   # just one stage
  python pipeline.py --from summarize# from a stage to the end
  python pipeline.py -- --days 3      # args after `--` go to the scrape stage (daily.py)
"""
import argparse
import subprocess
import sys

STAGES = [
    ("scrape", "daily.py"),
    ("ingest", "ingest.py"),
    ("summarize", "summarize.py"),
    ("digest", "digest.py"),
    ("mail", "mailer.py"),
]


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

    for name, script in selected:
        stage_args = extra if name == "scrape" else []
        print(f"\n===== stage: {name} ({script}) =====")
        rc = subprocess.run([sys.executable, script, *stage_args]).returncode
        if rc != 0:
            print(f"[STOP] stage '{name}' exited with code {rc}")
            sys.exit(rc)

    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
