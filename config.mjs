// config.mjs — ALL target endpoints come from environment variables.
// The defaults committed here are PLACEHOLDERS on purpose: this file is public.
// Real values live in .env (gitignored). See .env.example / LOCAL_SETUP.md.
//
// Loads .env automatically (Node's built-in loader; no dotenv dependency).
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const envPath = join(__dirname, '.env');
if (existsSync(envPath)) {
  try { process.loadEnvFile(envPath); } catch { /* older node / parse issue: ignore */ }
}

const ORIGIN = process.env.TARGET_ORIGIN || 'https://REDACTED.example.com';

export const cfg = {
  ORIGIN,
  // Landing page the scraper opens (also drives the login redirect).
  HOMEPAGE:  process.env.TARGET_HOMEPAGE  || `${ORIGIN}/REDACTED/homepage.html`,
  // "My Content" page used to warm the session before hitting the feed.
  MYCONTENT: process.env.TARGET_MYCONTENT || `${ORIGIN}/REDACTED/my-content`,
  // Personalized content feed (JSON). Real base path is env-provided.
  FEED_BASE: process.env.TARGET_FEED_BASE || `${ORIGIN}/REDACTED/feed`,
  // Path prefix under which individual reports live; used to match report URLs.
  REPORT_PREFIX: process.env.TARGET_REPORT_PREFIX || '/REDACTED/reports',
  feedUrl(offset, limit) {
    return `${this.FEED_BASE}?offset=${offset}&limit=${limit}&getNewResults=false`;
  },
};

// Report-URL matcher, built from the (masked) path prefix so no real path is hardcoded.
export function reportRegex() {
  const p = cfg.REPORT_PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`${p}/(\\d{4})/(\\d{2})/(\\d{2})/([0-9a-f-]{36})\\.html`);
}
