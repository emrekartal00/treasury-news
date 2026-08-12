# treasury-news

Automation for a daily research-digest pipeline: it signs into a **subscribed** research
portal, downloads newly published items, and (in later stages) archives them to a database,
generates AI summaries, and emails a daily digest. Written in Python.

> All target addresses, database schema, internal endpoints, and credentials are kept
> **out of this repo** and supplied via environment variables. See `.env.example`.

## Requirements
- Python 3.9+
- Google Chrome installed (the scraper drives system Chrome; otherwise run
  `python -m playwright install chromium`)

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env      # then fill .env with your real values (kept local, gitignored)
```

## First: check the machine can run this
On a locked-down machine, verify Playwright can launch a browser and reach the portal:
```bash
python check_env.py
```

## Usage
```bash
# One-time: open a browser, log in; the session is saved to ./user-data
python recon.py

# Daily: download newly published items you haven't fetched before
python daily.py              # everything new since last run
python daily.py --days 3     # items published in the last 3 days
python daily.py --max 10     # cap downloads this run
```

Downloaded files land in `downloads/YYYY-MM-DD/`; dedupe state in `state/` (both gitignored).

### Running from Spyder
Spyder's IPython console owns an asyncio loop, which conflicts with Playwright's sync API.
The scripts route the browser work through a worker thread (`_util.py`) so **Run file**
works. If you hit event-loop errors, run in a dedicated console, or set the file's run
configuration to *Execute in an external system terminal*.

## Configuration
Every endpoint is an environment variable (`config.py` reads them; committed defaults are
placeholders). Fill them in `.env` — see `.env.example`.

## Notes
- Only use with an account you're entitled to; respect the portal's terms.
- The saved login session (`user-data/`) is sensitive — never commit or share it.
