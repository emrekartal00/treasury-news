"""window.py - the active publication-date window shared by the pipeline stages.

The email, and the stages that feed it (summarize, digest, curate), cover reports published in
the last 1 day - or the last 3 days on a Monday, to sweep up the unmailed Fri/Sat/Sun. Using
ONE cutoff everywhere means a fresh/backfill run doesn't summarize or curate the whole
multi-day scrape backlog - only the reports that could actually go in today's email.

(daily.py scrapes a wider 5-day window as a download buffer; these downstream stages narrow
it to the active window via min_pub_date().)
"""
from datetime import date, timedelta


def active_days(today=None):
    """1 on a normal weekday; 3 on Monday (covers the unmailed Fri/Sat/Sun)."""
    today = today or date.today()
    return 3 if today.weekday() == 0 else 1   # Monday == 0


def min_pub_date(today=None):
    """Oldest publication date (YYYY-MM-DD) today's pipeline should process."""
    today = today or date.today()
    return (today - timedelta(days=active_days(today))).isoformat()
