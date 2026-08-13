# treasury-news

A daily research-digest pipeline. Each weekday it signs into a **subscribed** research
portal, downloads newly published items, archives them in OracleDB, has a self-hosted LLM
summarize each one, and emails a digest. A separate web app serves the archive. All Python.

> All target addresses, database schema, internal endpoints, and credentials are kept
> **out of this repo** and supplied via environment variables. See `.env.example`.

## The pipeline (runs on one machine, daily)

`scrape -> store -> summarize -> digest -> email`, orchestrated by `pipeline.py`.

| Stage | Script | What it does |
|-------|--------|--------------|
| scrape | `daily.py` | Reads the feed, downloads new items (deduped) to `downloads/` |
| store | `ingest.py` (+ `db_conn.py`) | HTML -> text; `MERGE` PDF/text/metadata into Oracle |
| summarize | `summarize.py` (+ `ai.py`, `prompts/`) | LLM -> validated JSON summary per report |
| digest | `digest.py` | Rolls up the day's summarized reports |
| email | `mailer.py` | Renders + sends the HTML digest; idempotent via `email_log` |

Supporting: `recon.py` (one-time login), `check_env.py` / `diag.py` (environment probes),
`config.py` (env-driven endpoints), `_util.py` (Spyder-safe Playwright runner).

## Requirements
- Python 3.9+
- Google Chrome (the scraper drives system Chrome; otherwise `python -m playwright install chromium`)
- OracleDB reachable; an OpenAI-compatible chat endpoint; an SMTP relay

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env          # fill with real values (kept local, gitignored)
python check_env.py           # verify browser + portal + Oracle + endpoints
```

## Usage
```bash
python recon.py               # one-time: log in; session saved to ./user-data
python pipeline.py            # run the whole day: scrape -> ... -> email
python pipeline.py -- --days 3        # pass scraper args after `--`
python pipeline.py --only summarize   # run a single stage (debugging)
```
Each stage is also runnable on its own (`python ingest.py`, `python summarize.py`, ...).
Downloaded files land in `downloads/YYYY-MM-DD/`; dedupe state in `state/` (both gitignored).

### Running from Spyder
Spyder's IPython console owns an asyncio loop that conflicts with Playwright's sync API.
The scripts route browser work through a worker thread (`_util.py`) so **Run file** works.
If you hit event-loop errors, use a dedicated console or *Execute in an external system terminal*.

## Web app (`webapp/`)
A read-only Flask viewer for the archive (catalog, `LIKE` search, PDF-from-BLOB, summaries),
containerized for OpenShift. It only reads Oracle. See `webapp/README.md`.

## Configuration
Every endpoint is an environment variable (`config.py` reads them; committed defaults are
placeholders). Fill them in `.env` — see `.env.example`.

## Notes
- Only use with an account you're entitled to; respect the portal's terms.
- The saved login session (`user-data/`) is sensitive — never commit or share it.
- The database schema (DDL) and architecture docs are kept local, not in this repo.
