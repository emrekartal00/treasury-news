# treasury-news

Automation for a daily research-digest pipeline: it signs into a **subscribed** research
portal, downloads newly published items, and (in later stages) archives them, generates
AI summaries, serves an internal catalog, and emails a daily digest.

> All target addresses, database schema, internal endpoints, and credentials are kept
> **out of this repo** and supplied via environment variables. See `.env.example`.

## Requirements
- Node.js 20.6+ (uses the built-in `.env` loader; developed on Node 24)
- Google Chrome installed (the scraper drives your installed Chrome)

## Setup
```bash
npm install
cp .env.example .env      # then fill .env with your real values (kept local, gitignored)
```

## Usage
```bash
# One-time: open a browser, log in to the portal; the session is saved to ./user-data
node recon.mjs

# Daily: download newly published items you haven't fetched before
node daily.mjs            # everything new since last run
node daily.mjs --days 3   # items published in the last 3 days
node daily.mjs --max 10   # cap downloads this run
```

Downloaded files land in `downloads/YYYY-MM-DD/` and dedupe state in `state/`
(both gitignored).

## Configuration
Every endpoint is an environment variable (`config.mjs` reads them; committed defaults are
placeholders). Fill them in `.env`:

| Var | Purpose |
|-----|---------|
| `TARGET_ORIGIN` / `TARGET_HOMEPAGE` / `TARGET_MYCONTENT` | Portal URLs |
| `TARGET_FEED_BASE` / `TARGET_REPORT_PREFIX` | Content feed + report path |
| `DB_*` | OracleDB connection (later stages) |
| `LLM_*` | AI summarization endpoint (later stages) |
| `SMTP_*`, `MAIL_*` | Daily email (later stages) |

## Notes
- Only use with an account you're entitled to; respect the portal's terms.
- The saved login session (`user-data/`) is sensitive — never commit or share it.
