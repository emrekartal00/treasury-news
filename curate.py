"""curate.py - AI curation layer for the EMAIL.

Division of labour (the model is a medium 27B - keep deterministic work OUT of it):
  * CODE decides the age window and the daily cap.
  * The MODEL only drops non-treasury / non-research / equity items and RANKS the rest
    (tiered, latest-first) per prompts/curate.system.txt.

Flow in select():
  1. Age window (code): keep reports published on/after the cutoff. At most 1 day old on a
     normal weekday; on Monday, 3 days old so Fri/Sat/Sun research still gets mailed.
  2. Rank (model): send the survivors, numbered [1..N]; it returns include (ranked, best
     first) + exclude (with reasons).
  3. Cap (code): mail only the top MAX_INCLUDED of the ranked include list.

The web archive / DAILY_DIGEST / "View full digest" keep EVERYTHING; this only shapes the
outbound email. Fail-open: if the LLM is unavailable, mail the newest MAX_INCLUDED survivors.
Disable entirely with CURATE_EMAIL=0. ai.py is not touched by this module.
"""
import os
from datetime import date, timedelta
from pathlib import Path

import ai

HERE = Path(__file__).parent
SYSTEM_PROMPT = (HERE / "prompts" / "curate.system.txt").read_text(encoding="utf-8")

MAX_INCLUDED = 10   # hard cap: only the top-ranked N reports go in the email


def enabled():
    return str(os.environ.get("CURATE_EMAIL", "1")).strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def min_date(today=None):
    """Oldest acceptable publication date (YYYY-MM-DD). Tue-Fri: 1 day; Monday: 3 days
    (covers the unmailed Friday/Saturday/Sunday)."""
    today = today or date.today()
    back = 3 if today.weekday() == 0 else 1   # Monday == 0
    return (today - timedelta(days=back)).isoformat()


def _within_window(reports, cutoff):
    """Split into (kept, stale). Stale = a known date earlier than cutoff. Reports with no
    date are kept (we can't confirm they're stale)."""
    kept, stale = [], []
    for r in reports:
        d = r.get("date")
        (stale if (d and d < cutoff) else kept).append(r)
    return kept, stale


def _candidate_block(i, r):
    s = r.get("summary") or {}
    title = r.get("title") or r.get("headline") or "(untitled)"
    lines = [f"[{i}] {title}", f"    date: {r.get('date') or 'n/a'}"]
    if r.get("authors"):
        lines.append(f"    authors: {', '.join(r['authors'])}")
    lines.append(f"    stance: {s.get('stance') or 'n/a'}")
    if s.get("instruments"):
        lines.append(f"    instruments: {', '.join(str(x) for x in s['instruments'])}")
    lines.append(f"    abstract: {s.get('one_paragraph') or 'NO_CONTENT'}")
    return "\n".join(lines)


def build_user(reports):
    blocks = [_candidate_block(i, r) for i, r in enumerate(reports, 1)]
    return ("Candidate reports:\n\n" + "\n\n".join(blocks)
            + "\n\nReturn the JSON object described in the instructions.")


def _parse_order(obj, n):
    """Ordered list of 1-based indices to include, in the model's priority order, or None if
    unusable. Accepts "include" (kept in order); else derives from "exclude" (rest, original
    order). Out-of-range/duplicate indices dropped."""
    def _ints(seq):
        out = []
        for x in seq or []:
            v = x.get("n") if isinstance(x, dict) else x
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= v <= n and v not in out:
                out.append(v)
        return out

    if isinstance(obj.get("include"), list):
        return _ints(obj["include"])
    if isinstance(obj.get("exclude"), list):
        excluded = set(_ints(obj["exclude"]))
        return [i for i in range(1, n + 1) if i not in excluded]
    return None


def select(reports, verbose=True, today=None):
    """Return the reports to email: the age-windowed survivors, ranked by the model, capped
    at MAX_INCLUDED. Fail-open to the newest MAX_INCLUDED survivors."""
    if not reports or not enabled():
        if reports and verbose:
            print(f"[curate] disabled (CURATE_EMAIL) - keeping all {len(reports)} report(s).")
        return list(reports)

    cutoff = min_date(today)
    survivors, stale = _within_window(reports, cutoff)
    if verbose and stale:
        print(f"[curate] age window (>= {cutoff}): dropped {len(stale)} stale, "
              f"{len(survivors)} within window.")
    if not survivors:
        if verbose:
            print(f"[curate] no reports within the age window (>= {cutoff}); nothing to mail.")
        return []

    n = len(survivors)
    try:
        content, finish = ai.chat(SYSTEM_PROMPT, build_user(survivors),
                                  temperature=0.1, max_tokens=2048)
        if finish == "length":
            raise ai.LLMError("truncated curation output (finish_reason=length)")
        order = _parse_order(ai.parse_json(content), n)
        if order is None:
            raise ai.LLMError("no include/exclude list in model output")
    except Exception as exc:
        if verbose:
            print(f"[curate] LLM unavailable ({exc}); mailing newest {MAX_INCLUDED} survivor(s).")
        return survivors[:MAX_INCLUDED]

    ranked = [survivors[i - 1] for i in order]        # topic-relevant, tiered, best first
    selected = ranked[:MAX_INCLUDED]                  # code cap
    if verbose:
        for rank, r in enumerate(selected, 1):
            title = (r.get("title") or r.get("headline") or "(untitled)")[:60]
            print(f"[curate] mail #{rank}  {title}")
        print(f"[curate] {n} in window -> AI kept {len(ranked)} relevant "
              f"-> mailing top {len(selected)} (cap {MAX_INCLUDED}).")
    return selected
