# treasury-news

A daily research-digest pipeline. Each weekday it signs into one or more **subscribed**
research portals, downloads newly published reports, archives them in OracleDB, has a
self-hosted LLM summarize each one, AI-curates which to include, and emails a digest. A
separate web app serves the archive. All Python.

> All target addresses, database schema, internal endpoints, and credentials are kept
> **out of this repo** and supplied via environment variables. See `.env.example`.

## The pipeline (runs on one machine, daily)

`scrape -> store -> summarize -> digest -> email`, orchestrated by `pipeline.py`.

| Stage | Script | What it does |
|-------|--------|--------------|
| scrape | `daily.py` | For each portal in `SOURCES`, reads its feed and downloads new reports (deduped) to `downloads/<source>/YYYY-MM-DD/` |
| store | `ingest.py` (+ `db_conn.py`) | HTML/PDF -> text; `MERGE` PDF/text/metadata into Oracle (tagged with its `source`) |
| summarize | `summarize.py` (+ `ai.py`, `prompts/`) | LLM -> validated JSON summary per report |
| digest | `digest.py` | Rolls up the day's summarized reports |
| email | `mailer.py` (+ `curate.py`) | AI-curates which reports to include, renders + sends the HTML digest via local Outlook; idempotent via `email_log` |

Supporting: `recon.py` (per-portal login + traffic capture), `check_env.py` / `diag.py`
(environment probes), `config.py` (env loader), `_util.py` (Spyder-safe Playwright runner).

## Sources (pluggable adapters)

Each portal is a small adapter in `sources/<key>.py` (a subclass of `sources/base.py`).
`SOURCES` (comma-separated) lists which to scrape; `daily.py --source <key>` runs one.
**Adding a portal = one adapter file + its `<KEY>_*` env vars — no pipeline changes.**

- **Feeds are per-portal:** a JSON API where one exists, otherwise parsed from the portal's
  HTML.
- **Body text** comes from the report HTML; when a portal renders articles client-side (a JS
  shell), `ingest.py` falls back to extracting text from the PDF (`pypdf`).
- **Auth:** most portals authenticate via a session cookie persisted in `./user-data` — log
  in once with `recon.py` and unattended runs reuse it. Portals whose session can't persist
  across launches use a scripted (or interactive `--login`) sign-in each run, with
  credentials in `.env`.
- Reports are namespaced `report_id = <source>:<native-id>` (the original source keeps its
  bare id) and every row carries a `source` column.

## Requirements
- Python 3.9+
- Google Chrome (the scraper drives system Chrome; otherwise `python -m playwright install chromium`)
- OracleDB reachable; an OpenAI-compatible chat endpoint
- Emailing uses the local **Outlook desktop app** via COM (`pywin32`) — Windows only

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env          # fill with real values (kept local, gitignored)
python check_env.py           # verify browser + portal + Oracle + endpoints
```

## Usage
```bash
python recon.py https://<portal-url>   # one-time per portal: log in; session saved to ./user-data
python pipeline.py                     # run the whole day for every portal in SOURCES
python pipeline.py -- --days 3         # pass scraper args after `--`
python pipeline.py --only summarize    # run a single stage (debugging)

python daily.py --source <key>         # scrape one portal (e.g. --max 5, --days 3)
python daily.py --source <key> --login # interactive sign-in for a non-persistent-session portal
python mailer.py --preview             # open the curated digest in Outlook without sending
python mailer.py --no-curate           # email every report (skip AI curation)
```
Each stage is also runnable on its own. Downloaded files land in
`downloads/<source>/YYYY-MM-DD/`; per-portal dedupe state in `state/seen_<source>.json`
(both gitignored). A single portal failing (e.g. an expired session) warns but does not
block the rest of the run.

## AI email curation
`mailer.py` calls `curate.select()` to trim the *email* to the reports worth pushing (the
web archive and the "View full digest" link still show everything). Edit the include/exclude
topic rules in `prompts/curate.system.txt`. Toggle with `CURATE_EMAIL=0`, or `--no-curate`
for one run. Fail-open: if the LLM is unavailable, all reports are kept.

### Running from Spyder
Spyder's IPython console owns an asyncio loop that conflicts with Playwright's sync API.
The scripts route browser work through a worker thread (`_util.py`) so **Run file** works.
If you hit event-loop errors, use a dedicated console or *Execute in an external system terminal*.

## Web app (`webapp/`)
A read-only Flask viewer for the archive (catalog with source tags, `LIKE` search,
PDF-from-BLOB, summaries), containerized for OpenShift. It only reads Oracle. See
`webapp/README.md`.

## Configuration
Every endpoint is an environment variable (`config.py` loads `.env`; committed defaults are
placeholders). Fill them in `.env` — see `.env.example`.

## Notes
- Only use with accounts you're entitled to; respect each portal's terms.
- The saved login session (`user-data/`) and any stored portal credentials are sensitive —
  never commit or share them.
- The schema migrations live in `db/migrations/` (run them as the schema owner). Deeper
  architecture docs and `db/discover.sql` are kept local, not in this repo.
