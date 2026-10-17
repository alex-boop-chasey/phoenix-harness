// Playwright driver for tools/ui-shots.sh. Runs INSIDE the rig container (no network, dashboard on
// localhost). Logs in with a one-time code, then screenshots each requested page at 1440 and 390 wide,
// in light and dark. Any browser console error (CSP violations included) fails the run.
//
//   node shots.js <base-url> <login-code> <out-dir> <pages>
//
// <pages>: space-separated. A project name screenshots that project's view; "new" the New project
// form; "sessions" / "files" those sections; "nav" the phone drawer open. Files: <out>/<page>-<theme>-<width>.png
// Before the screenshots a tour clicks every section and every view-only control (projects, New
// project, attempt files, plan files, planner log) and fails on any console error.
"use strict";
const path = require("path");
const { chromium } = require("playwright");

const [base, code, out, pagesArg] = process.argv.slice(2);
const pages = (pagesArg || "").trim().split(/\s+/).filter(Boolean);
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];
const errors = [];

function watch(page, label) {
  page.on("console", (m) => { if (m.type() === "error") errors.push(`${label}: ${m.text()}`); });
  page.on("pageerror", (e) => errors.push(`${label}: ${e.message}`));
}

async function selectProject(page, name) {
  await page.locator("#projects .pcard", { hasText: name }).first().click();
  await page.locator("main h2", { hasText: name }).first().waitFor({ timeout: 10000 });
  // the project list highlight catches up on the app's 4 s refresh tick
  await page.locator('#projects .pcard[aria-current="true"]', { hasText: name }).waitFor({ timeout: 8000 });
}

async function capture(context, name, width, theme) {
  const page = await context.newPage();
  watch(page, `${name}-${theme}-${width}`);
  try {
    const sec = name === "sessions" || name === "files" ? name : "builder";
    await page.goto(`${base}/#/${sec}`, { waitUntil: "networkidle" });
    if (sec !== "builder") {
      await page.locator(`#page-${sec} h1`).waitFor({ timeout: 10000 });
    } else if (name === "new") {
      await page.locator("#newproject").click();
      await page.locator("#slug").waitFor({ timeout: 10000 });
    } else if (name === "nav") {
      if (width <= 900) { await page.locator("#navtoggle").click(); await page.locator("body.nav-open").waitFor({ timeout: 5000 }); }
    } else {
      await selectProject(page, name);
    }
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(700); // attempts list and usage meters load async
    await page.screenshot({ path: path.join(out, `${name}-${theme}-${width}.png`), fullPage: true });
    console.log(`shot ${name}-${theme}-${width}.png`);
  } finally {
    await page.close();
  }
}

// Click through every section and every view-only control; mutating actions (start/stop/ack/plan/
// create) are never clicked.
async function tour(context) {
  const page = await context.newPage();
  watch(page, "tour");
  await page.goto(base + "/", { waitUntil: "networkidle" });
  for (const sec of ["sessions", "files", "builder"]) {
    await page.locator(`#nav a[data-section="${sec}"]`).click();
    await page.locator(`#page-${sec}:not([hidden])`).waitFor({ timeout: 5000 });
    await page.locator(`#nav a[data-section="${sec}"][aria-current="page"]`).waitFor({ timeout: 5000 });
  }
  const names = await page.locator("#projects .pcard .pname").allTextContents();
  if (!names.length) throw new Error("tour: no projects listed");
  let attemptFiles = 0, planFiles = 0, logs = 0;
  for (const name of names) {
    await selectProject(page, name);
    const f = page.locator("#attempts-wrap .files button").first();
    if (await f.count()) {
      await f.click();
      await page.locator("main section:last-of-type pre", { hasText: /\S/ }).waitFor({ timeout: 10000 });
      attemptFiles++;
    }
    const pf = page.locator(".files button", { hasText: /^(steps\.json|PLAN\.md)$/ }).first();
    if (await pf.count()) { await pf.click(); await page.waitForTimeout(400); planFiles++; }
    const sum = page.locator("details summary", { hasText: "Planner log" }).first();
    if (await sum.count()) { await sum.click(); logs++; }
  }
  await page.locator("#newproject").click();
  await page.locator("#slug").waitFor({ timeout: 10000 });
  await page.locator("#navtoggle").click().catch(() => {});   // desktop: hidden, that's fine
  console.log(`tour ok: ${names.length} projects, ${attemptFiles} attempt files, ${planFiles} plan files, ${logs} planner logs, sections + new project`);
  await page.close();
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

    const tourCtx = await browser.newContext({ viewport: VIEWPORTS[0], storageState: state });
    await tour(tourCtx);
    await tourCtx.close();
    for (const theme of ["light", "dark"]) {
      for (const vp of VIEWPORTS) {
        const context = await browser.newContext({ viewport: vp, storageState: state, colorScheme: theme });
        for (const name of pages) await capture(context, name, vp.width, theme);
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
})().catch((e) => { console.error(`shots.js failed: ${e.message}`); process.exit(1); });
