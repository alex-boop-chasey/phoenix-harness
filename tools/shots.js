// Playwright driver for tools/ui-shots.sh. Runs INSIDE the rig container (no network, dashboard on
// localhost). Logs in with a one-time code, then screenshots each requested page at 1440 and 390 wide.
//
//   node shots.js <base-url> <login-code> <out-dir> <pages>
//
// <pages>: space-separated. A project name screenshots that project's view; "new" screenshots the
// New project form. Files: <out>/<page>-<width>.png
"use strict";
const path = require("path");
const { chromium } = require("playwright");

const [base, code, out, pagesArg] = process.argv.slice(2);
const pages = (pagesArg || "").trim().split(/\s+/).filter(Boolean);
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];

async function capture(context, name, width) {
  const page = await context.newPage();
  try {
    await page.goto(base + "/", { waitUntil: "networkidle" });
    if (name === "new") {
      await page.locator("#newproject").click();
      await page.locator("#slug").waitFor({ timeout: 10000 });
    } else {
      await page.locator("#projects .pcard", { hasText: name }).first().click();
      await page.locator("main h2", { hasText: name }).first().waitFor({ timeout: 10000 });
      // the sidebar highlight catches up on the app's 4 s refresh tick
      await page.locator('#projects .pcard[aria-current="true"]', { hasText: name }).waitFor({ timeout: 8000 });
    }
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(700); // attempts list and usage meters load async
    await page.screenshot({ path: path.join(out, `${name}-${width}.png`), fullPage: true });
    console.log(`shot ${name}-${width}.png`);
  } finally {
    await page.close();
  }
}

// Mock mode: `node shots.js --mock <base-url> <out-dir> <pages>` — static design pages, no login,
// each captured in light and dark (<page>-<light|dark>-<width>.png). Any console error (CSP
// violations are reported there) fails the run.
async function mockMode() {
  const [mbase, mout, mpagesArg] = process.argv.slice(3);
  const mpages = (mpagesArg || "").trim().split(/\s+/).filter(Boolean);
  if (!mbase || !mout || !mpages.length) {
    console.error("usage: node shots.js --mock <base-url> <out-dir> <pages>");
    process.exit(2);
  }
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const errors = [];
  try {
    for (const scheme of ["light", "dark"]) {
      for (const vp of VIEWPORTS) {
        const context = await browser.newContext({ viewport: vp, colorScheme: scheme });
        for (const name of mpages) {
          const page = await context.newPage();
          page.on("console", (m) => { if (m.type() === "error") errors.push(`${name}: ${m.text()}`); });
          page.on("pageerror", (e) => errors.push(`${name}: ${e.message}`));
          const resp = await page.goto(`${mbase}/${name}.html`, { waitUntil: "networkidle" });
          if (!resp || resp.status() !== 200) throw new Error(`${name}.html: HTTP ${resp && resp.status()}`);
          await page.waitForTimeout(300);
          await page.screenshot({ path: path.join(mout, `${name}-${scheme}-${vp.width}.png`), fullPage: true });
          console.log(`shot ${name}-${scheme}-${vp.width}.png`);
          await page.close();
        }
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
  if (errors.length) {
    console.error("browser console errors:\n  " + errors.join("\n  "));
    process.exit(1);
  }
  console.log("console clean: no errors, no CSP violations");
}

(async () => {
  if (base === "--mock") return mockMode();
  if (!base || !code || !out || !pages.length) {
    console.error("usage: node shots.js <base-url> <login-code> <out-dir> <pages>");
    process.exit(2);
  }
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  try {
    // Log in once (the code is single use), keep the session cookie as storage state.
    const login = await browser.newContext();
    const p = await login.newPage();
    const resp = await p.goto(`${base}/login?c=${encodeURIComponent(code)}`);
    if (!resp || resp.status() !== 200 || p.url() !== base + "/") {
      throw new Error(`login failed (status ${resp && resp.status()}, url ${p.url()})`);
    }
    const state = await login.storageState();
    await login.close();

    for (const vp of VIEWPORTS) {
      const context = await browser.newContext({ viewport: vp, storageState: state });
      for (const name of pages) await capture(context, name, vp.width);
      await context.close();
    }
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(`shots.js failed: ${e.message}`); process.exit(1); });
