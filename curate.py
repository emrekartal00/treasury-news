"""curate.py - AI curation layer: pick which reports go into the EMAIL.

The web archive/homepage and DAILY_DIGEST intentionally keep EVERYTHING; this only trims
the *email* down to what is worth pushing. Given the day's candidate reports (title,
authors, stance, instruments, abstract), an LLM returns the subset to include, guided by
the editable INCLUDE/EXCLUDE topic rules in prompts/curate.system.txt.

Design:
- Candidates are numbered [1], [2], ...; the model returns those numbers (never the UUIDs,
  which models mangle). We map the numbers back to reports here.
- Fail-open: if the LLM is unavailable or returns nothing usable, ALL candidates are kept
  (better to over-send than to silently drop research).
- Disable entirely with CURATE_EMAIL=0 (or CURATE_EMAIL in {0,false,no,off}).

Not a standalone pipeline stage - mailer.py calls select() in-process.
"""
import os
from pathlib import Path

import ai

HERE = Path(__file__).parent
SYSTEM_PROMPT = (HERE / "prompts" / "curate.system.txt").read_text(encoding="utf-8")


def enabled():
    return str(os.environ.get("CURATE_EMAIL", "1")).strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _candidate_block(i, r):
    s = r.get("summary") or {}
    title = r.get("title") or r.get("headline") or "(untitled)"
    lines = [f"[{i}] {title}"]
    if r.get("source"):
        lines.append(f"    source: {r['source']}")
    if r.get("authors"):
        lines.append(f"    authors: {', '.join(r['authors'])}")
    lines.append(f"    stance: {s.get('stance') or 'n/a'}")
    if s.get("instruments"):
        lines.append(f"    instruments: {', '.join(str(x) for x in s['instruments'])}")
    if s.get("one_paragraph"):
        lines.append(f"    abstract: {s['one_paragraph']}")
    return "\n".join(lines)


def build_user(reports):
    blocks = [_candidate_block(i, r) for i, r in enumerate(reports, 1)]
    return ("Candidate reports for today's email:\n\n"
            + "\n\n".join(blocks)
            + "\n\nReturn the JSON object described in the instructions.")


def _parse_order(obj, n):
    """Return an ORDERED list of valid 1-based indices to include, in the model's priority
    order (most relevant first), or None if unusable.

    Accepts an explicit "include" list (kept in the model's order); if absent, derives it
    from "exclude" (keep the rest, in original order). Out-of-range/duplicate indices dropped."""
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


def _title(r):
    return (r.get("title") or r.get("headline") or "(untitled)")


def select(reports, verbose=True):
    """Return the curated subset of `reports`, REORDERED by the model's priority (most
    relevant first). Fail-open to all (original order) if the LLM is unavailable.

    Logs the ranked keeps and the drops when verbose."""
    if not reports or not enabled():
        if reports and verbose:
            print(f"[curate] disabled (CURATE_EMAIL) - keeping all {len(reports)} report(s).")
        return list(reports)

    n = len(reports)
    try:
        user = build_user(reports)
        content, finish = ai.chat(SYSTEM_PROMPT, user, temperature=0.1, max_tokens=1024)
        if finish == "length":
            raise ai.LLMError("truncated curation output (finish_reason=length)")
        order = _parse_order(ai.parse_json(content), n)
        if order is None:
            raise ai.LLMError("no include/exclude list in model output")
    except Exception as exc:
        if verbose:
            print(f"[curate] LLM unavailable ({exc}); keeping all {n} report(s).")
        return list(reports)

    selected = [reports[i - 1] for i in order]  # ranked, most relevant first
    if verbose:
        kept = set(order)
        for rank, i in enumerate(order, 1):
            print(f"[curate] keep #{rank}  [{i}] {_title(reports[i - 1])[:60]}")
        for i, r in enumerate(reports, 1):
            if i not in kept:
                print(f"[curate] drop      [{i}] {_title(r)[:60]}")
        print(f"[curate] selected {len(selected)}/{n} report(s), ranked by priority.")
    return selected
