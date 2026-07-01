/**
 * Demo GIF Generator for ThoughtMachine Frontend
 *
 * Generates an animated GIF showing:
 * 1. Multiple session tabs with worker panel state persisting per tab
 * 2. Worker panel open/close scoped to each session
 * 3. The error boundary fallback UI
 *
 * Usage:
 *   1. Start the Vite dev server:  cd web_ui/frontend && npm run dev
 *   2. Install puppeteer:           npm install puppeteer
 *   3. Run this script:            node scripts/generate_demo_gif.mjs
 *
 * Requires:  puppeteer (for browser automation)
 *            ffmpeg    (optional, for GIF conversion — apt/brew install ffmpeg)
 */

import puppeteer from 'puppeteer';
import { mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUTPUT_DIR = join(__dirname, '..', 'demo_output');
const TARGET_URL = process.env.VITE_DEV_URL || 'http://localhost:5173';

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function main() {
  mkdirSync(OUTPUT_DIR, { recursive: true });

  console.log(`Launching browser → ${TARGET_URL}`);
  const browser = await puppeteer.launch({
    headless: 'new',
    defaultViewport: { width: 1280, height: 800 },
  });

  const page = await browser.newPage();
  const screenshots = [];

  async function shot(name) {
    const path = join(OUTPUT_DIR, `${name}.png`);
    await page.screenshot({ path, fullPage: false });
    screenshots.push(path);
    console.log(`  📸 ${name}`);
  }

  // ─── Step 1: Load the app ───────────────────────────────
  await page.goto(TARGET_URL, { waitUntil: 'networkidle0' });
  await sleep(1000);
  await shot('01-app-loaded');

  // ─── Step 2: Open a worker panel in session tab 1 ──────
  const workerToggleBtn = await page.$('button:has(svg), [class*="worker-toggle"], [class*="toggle-panel"]');
  if (workerToggleBtn) {
    await workerToggleBtn.click();
    await sleep(500);
  }
  await shot('02-worker-panel-open');

  // ─── Step 3: Create a second session tab ────────────────
  const newTabBtn = await page.$('button:has-text("+"), [class*="add-session"], [class*="new-session"]');
  if (newTabBtn) {
    await newTabBtn.click();
    await sleep(800);
  }
  await shot('03-second-session-tab');

  // ─── Step 4: Show worker panel is CLOSED in tab 2 ──────
  const secondTab = await page.$('[class*="session-tab"]:nth-child(2)');
  if (secondTab) {
    await secondTab.click();
    await sleep(500);
  }
  await shot('04-tab2-worker-closed');

  // ─── Step 5: Open worker in tab 2, switch back to tab 1 ──
  if (workerToggleBtn) {
    await workerToggleBtn.click();
    await sleep(500);
  }
  const firstTab = await page.$('[class*="session-tab"]:nth-child(1)');
  if (firstTab) {
    await firstTab.click();
    await sleep(500);
  }
  await shot('05-tab1-still-open');

  // ─── Step 6: Error boundary demo ────────────────────────
  await page.evaluate(() => {
    throw new Error('Demo error: triggered by GIF generator');
  });
  await sleep(1000);
  await shot('06-error-boundary');

  // ─── Step 7: Reload back to normal ──────────────────────
  await page.goto(TARGET_URL, { waitUntil: 'networkidle0' });
  await sleep(1000);
  await shot('07-recovered');

  await browser.close();
  console.log(`\n✅ ${screenshots.length} screenshots saved to ${OUTPUT_DIR}/`);

  // ─── Stitch into GIF (requires ffmpeg) ──────────────────
  console.log('\nNow generating GIF via ffmpeg...');
  try {
    const { execSync } = await import('child_process');
    execSync(
      `ffmpeg -y -framerate 1 -pattern_type glob -i '${OUTPUT_DIR}/*.png' ` +
      `-vf "fps=10,scale=iw*0.8:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" ` +
      `'${OUTPUT_DIR}/demo.gif'`,
      { stdio: 'inherit', timeout: 30000 }
    );
    console.log(`\n🎬 GIF created: ${OUTPUT_DIR}/demo.gif`);
  } catch (e) {
    console.log('\n⚠️  ffmpeg not found. Install it to generate a GIF, or use the PNG sequence directly.');
    console.log('   • macOS: brew install ffmpeg');
    console.log('   • Ubuntu: sudo apt install ffmpeg');
    console.log('   • Or use https://ezgif.com to stitch the PNGs online');
  }

  console.log('\nDone! Open demo_output/ to see the results.');
}

main().catch(err => {
  console.error('❌ Failed:', err.message);
  process.exit(1);
});
