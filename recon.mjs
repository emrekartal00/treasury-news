// recon.mjs — interactive reconnaissance for Marquee daily PDF.
//
// What it does:
//   1. Launches your installed Chrome with a PERSISTENT profile (./user-data),
//      so once you log in the session (cookies) is reused on future runs.
//   2. Logs EVERY network request/response to ./recon/traffic.jsonl.
//   3. Saves any file Marquee triggers as a download into ./downloads/.
//   4. Stays open until you close the browser window.
//
// How to use:
//   node recon.mjs
//   -> A Chrome window opens. Log in to Marquee if asked.
//   -> Navigate exactly the way you normally do to get the daily PDF
//      (open the macro page, click through, download/open the PDF).
//   -> Close the window when done. Then we read ./recon/traffic.jsonl together.

import { chromium } from 'playwright';
import { mkdirSync, appendFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { cfg } from './config.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const START_URL = process.argv[2] || cfg.HOMEPAGE;

const userDataDir = join(__dirname, 'user-data');
const reconDir = join(__dirname, 'recon');
const downloadsDir = join(__dirname, 'downloads');
mkdirSync(reconDir, { recursive: true });
mkdirSync(downloadsDir, { recursive: true });
const logPath = join(reconDir, 'traffic.jsonl');

const log = (obj) => appendFileSync(logPath, JSON.stringify(obj) + '\n');
const interesting = (url, ct = '') =>
  /pdf|document|research|publication|content|\/api\/|\.json/i.test(url) ||
  /pdf|json/i.test(ct);

console.log(`\n▶ Opening Chrome with persistent profile at ./user-data`);
console.log(`▶ Start URL: ${START_URL}`);
console.log(`▶ Logging all traffic to ./recon/traffic.jsonl`);
console.log(`▶ Downloads saved to ./downloads/\n`);
console.log(`   Log in if needed, then reproduce your normal steps to get the`);
console.log(`   daily PDF. CLOSE the browser window when finished.\n`);

const context = await chromium.launchPersistentContext(userDataDir, {
  headless: false,
  channel: 'chrome', // use your installed Chrome, no download needed
  acceptDownloads: true,
  viewport: { width: 1440, height: 900 },
});

context.on('request', (req) => {
  if (interesting(req.url())) {
    log({ t: Date.now(), kind: 'request', method: req.method(), url: req.url(),
          resourceType: req.resourceType() });
  }
});

context.on('response', async (res) => {
  const url = res.url();
  const ct = res.headers()['content-type'] || '';
  if (interesting(url, ct)) {
    log({ t: Date.now(), kind: 'response', status: res.status(), url,
          contentType: ct, length: res.headers()['content-length'] || '' });
    if (/application\/pdf/i.test(ct)) {
      console.log(`  📄 PDF response: ${res.status()}  ${url}`);
    }
  }
});

const page = context.pages()[0] || (await context.newPage());

page.on('download', async (dl) => {
  const name = dl.suggestedFilename() || `download-${Date.now()}.pdf`;
  const dest = join(downloadsDir, name);
  await dl.saveAs(dest);
  console.log(`  ⬇️  Saved download: ${name}  (from ${dl.url()})`);
  log({ t: Date.now(), kind: 'download', filename: name, url: dl.url() });
});

await page.goto(START_URL, { waitUntil: 'domcontentloaded' }).catch((e) =>
  console.log(`  (navigation note: ${e.message})`)
);

// Keep the process alive until the user closes the browser.
await new Promise((resolve) => context.on('close', resolve));
console.log('\n✅ Browser closed. Review ./recon/traffic.jsonl');
