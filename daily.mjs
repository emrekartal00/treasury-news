// daily.mjs — poll "My Content" (Marquee my-stream feed) and download only
// newly-published items. Dedupes by report id via state/seen.json, so it's
// safe to run repeatedly (e.g. once a day from cron/a container).
//
// Usage:
//   node daily.mjs                # download every unseen item in the feed window
//   node daily.mjs --days 3       # only items published in the last 3 days
//   node daily.mjs --max 10       # cap number of downloads this run
//   node daily.mjs --limit 50     # feed page size to scan (default 30)
//
// Output: downloads/YYYY-MM-DD/<title>_<shortid>.{html,pdf,meta.json}
// State:  state/seen.json  (ids already downloaded)

import { chromium } from 'playwright';
import { mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { cfg } from './config.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---- args ----
const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : def;
};
const LIMIT = parseInt(arg('limit', '30'), 10);   // feed page size to scan
const MAX = parseInt(arg('max', '100'), 10);      // cap downloads this run
const DAYS = arg('days', null) ? parseInt(arg('days', '0'), 10) : null;
const sinceMs = DAYS != null ? Date.now() - DAYS * 86400000 : null;

// ---- state ----
const stateDir = join(__dirname, 'state');
mkdirSync(stateDir, { recursive: true });
const seenPath = join(stateDir, 'seen.json');
const seen = existsSync(seenPath) ? JSON.parse(readFileSync(seenPath, 'utf8')) : { ids: {}, lastRun: null };
const save = () => writeFileSync(seenPath, JSON.stringify(seen, null, 2));

const slug = (s) => (s || 'report').replace(/[—–]/g, '-').replace(/[^\w.-]+/g, '_').replace(/_+/g, '_').slice(0, 90);
const dateFromPath = (p) => (p.match(/\/(\d{4})\/(\d{2})\/(\d{2})\//) || []).slice(1).join('-');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Realistic, randomized pause between downloads (~2.5–5s). Keeps traffic
// human-paced without dragging the run out.
const humanPause = () => sleep(2500 + Math.floor(Math.random() * 2500));

const ctx = await chromium.launchPersistentContext(join(__dirname, 'user-data'), {
  headless: false, channel: 'chrome', viewport: { width: 1440, height: 900 },
});
const page = ctx.pages()[0] || (await ctx.newPage());

// Warm the session on the My Content page (mirrors real usage).
console.log('▶ Opening My Content…');
await page.goto(cfg.MYCONTENT, { waitUntil: 'domcontentloaded', timeout: 60000 })
  .catch((e) => console.log('  (goto note:', e.message + ')'));
await page.waitForTimeout(3500);

// ---- fetch the feed (paginate until old/enough) ----
async function fetchPage(offset) {
  const url = cfg.feedUrl(offset, LIMIT);
  const r = await ctx.request.get(url, { timeout: 30000 });
  if (!r.ok()) throw new Error(`feed ${r.status()} at offset ${offset}`);
  const j = await r.json();
  return Array.isArray(j) ? j : (j.results || j.items || []);
}

console.log('▶ Reading feed…');
let feed = [];
for (let offset = 0; offset < 300; offset += LIMIT) {
  const batch = await fetchPage(offset).catch((e) => { console.log('  feed error:', e.message); return []; });
  if (!batch.length) break;
  feed.push(...batch);
  const oldest = Math.min(...batch.map((x) => x.publicationDateTime || 0));
  if (sinceMs && oldest < sinceMs) break;   // past the requested window
  if (!sinceMs) break;                       // no window filter: one page is enough for daily
}

// ---- select newly-published, unseen items ----
let candidates = feed.filter((it) => it.id && !seen.ids[it.id]);
if (sinceMs) candidates = candidates.filter((it) => (it.publicationDateTime || 0) >= sinceMs);
candidates.sort((a, b) => (b.publicationDateTime || 0) - (a.publicationDateTime || 0));
candidates = candidates.slice(0, MAX);

console.log(`▶ Feed items: ${feed.length} | new & unseen: ${candidates.length}` +
  (sinceMs ? ` (last ${DAYS}d)` : '') + (candidates.length ? '' : ' — nothing to do.'));

let ok = 0;
for (const it of candidates) {
  const date = dateFromPath(it.path) || new Date(it.publicationDateTime || Date.now()).toISOString().slice(0, 10);
  const dir = join(__dirname, 'downloads', date);
  mkdirSync(dir, { recursive: true });
  const base = join(dir, `${slug(it.title || it.distributionHeadline)}_${it.id.slice(0, 8)}`);
  const htmlUrl = cfg.ORIGIN + it.path;
  const pdfUrl = it.downloadPath ? cfg.ORIGIN + it.downloadPath : null;
  console.log(`\n• ${date}  ${it.title || it.distributionHeadline}`);

  // HTML: navigate so the SPA authorizes the content route, then save rendered DOM.
  let htmlBytes = 0;
  try {
    await page.goto(htmlUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(
      () => document.title && !/login/i.test(document.title) && document.body.innerText.length > 500,
      { timeout: 45000 }).catch(() => {});
    await page.waitForTimeout(2000);
    let html = '';
    for (let i = 0; i < 5 && !html; i++) { html = await page.content().catch(() => ''); if (!html) await page.waitForTimeout(1500); }
    writeFileSync(`${base}.html`, Buffer.from(html, 'utf8'));
    htmlBytes = Buffer.byteLength(html, 'utf8');
    console.log(`    ✓ html (${htmlBytes} bytes)`);
  } catch (e) { console.log(`    ✗ html failed: ${e.message}`); }

  // PDF: direct authenticated GET (works with cookies).
  let pdfInfo = null;
  if (pdfUrl) {
    try {
      const r = await ctx.request.get(pdfUrl, { timeout: 60000 });
      const body = await r.body();
      const isPdf = body.slice(0, 5).toString('latin1') === '%PDF-';
      writeFileSync(`${base}.${isPdf ? 'pdf' : 'pdf.html'}`, body);
      pdfInfo = { status: r.status(), bytes: body.length, isPdf };
      console.log(`    ✓ pdf (${body.length} bytes${isPdf ? '' : ', NOT a real pdf'})`);
    } catch (e) { console.log(`    ✗ pdf failed: ${e.message}`); }
  }

  writeFileSync(`${base}.meta.json`, JSON.stringify({
    id: it.id, title: it.title, distributionHeadline: it.distributionHeadline,
    date, publicationDateTime: it.publicationDateTime, authors: it.authors,
    synopsis: it.synopsis, reportTypes: it.reportTypes, totalPages: it.totalPages,
    htmlUrl, pdfUrl, htmlBytes, pdf: pdfInfo, fetchedAt: new Date().toISOString(),
  }, null, 2));

  seen.ids[it.id] = { date, title: it.title, fetchedAt: new Date().toISOString() };
  save();
  ok++;
  if (ok < candidates.length) await humanPause(); // human-paced gap between items
}

seen.lastRun = new Date().toISOString();
save();
console.log(`\n✅ Done. Downloaded ${ok} new item(s). Total tracked: ${Object.keys(seen.ids).length}.`);
await ctx.close();
