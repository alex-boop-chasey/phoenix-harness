// Playwright driver for tools/ui-shots.sh. Runs INSIDE the rig container (no network, dashboard on
// localhost). Logs in with a one-time code, then screenshots each requested page at 1440 and 390 wide,
// in light and dark. Any browser console error (CSP violations included) fails the run.
//
//   node shots.js <base-url> <login-code> <out-dir> <pages>
//
// <pages>: space-separated. A project name screenshots that project's view; "new" the New project
// form; "sessions" / "files" those sections; "nav" the phone drawer open; "tabs" the builder as it
// opens (first open tab, or the empty state / New project form when nothing is open); "menu" the
// "+" menu open (with the reopen list when projects are closed); "close" the close-tab confirmation;
// "sessions-new" / "sessions-delete" / "sessions-telegram" the Session manager dialogs.
// Files: <out>/<page>-<theme>-<width>.png
// Before the screenshots a tour clicks every section and every view-only control (tabs, New
// project, attempt files, plan files, planner log), closes a tab and reopens it from "+", walks the
// tabs with the keyboard, and fails on any console error.
"use strict";
const path = require("path");
const { chromium } = require("playwright");

const [base, code, out, pagesArg] = process.argv.slice(2);
const pages = (pagesArg || "").trim().split(/\s+/).filter(Boolean);
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];
const errors = [];
// Deliberate server refusals (a flow may ask for a refused path on purpose): Chromium logs every
// non-2xx resource as a console error, so a refusal we provoke is registered here and its console line is
// expected instead of failing the run. Anything else still fails.
const expected = [];
const expectFail = (re, status) => expected.push({ re, status, seen: 0 });
function isExpected(m) {
  const url = (m.location && m.location().url) || "";
  if (process.env.UI_SHOTS_DEBUG) console.log(`console error at ${url}: ${m.text()}`);
  const hit = expected.find((x) => x.re.test(url) && m.text().includes(`status of ${x.status}`));
  if (hit) hit.seen++;
  return !!hit;
}

function watch(page, label) {
  page.on("console", (m) => { if (m.type() === "error" && !isExpected(m)) errors.push(`${label}: ${m.text()}`); });
  page.on("pageerror", (e) => errors.push(`${label}: ${e.message}`));
  // unhandled promise rejections are not always surfaced as pageerror: log them as console errors
  page.addInitScript(() => window.addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason; console.error(`unhandled rejection: ${r && r.stack ? r.stack : r}`); }));
}

const tab = (page, name) => page.locator(`#tabs .tab[data-name="${name}"]`);

async function selectProject(page, name) {
  await tab(page, name).click();
  await page.locator("main h2", { hasText: name }).first().waitFor({ timeout: 10000 });
  await page.locator(`#tabs .tab[data-name="${name}"][aria-selected="true"]`).waitFor({ timeout: 8000 });
}

async function openMenu(page) {
  await page.locator("#tabs .tab-add").click();
  await page.locator(".menu[role=menu]").waitFor({ timeout: 5000 });
}

// Requirement 4: one click on "+" opens the menu even when the periodic refresh re-renders the tab strip
// between pointer down and pointer up (that used to replace the button and swallow the click).
async function plusDuringRefresh(page) {
  const add = page.locator("#tabs .tab-add");
  const box = await add.boundingBox();
  if (!box) throw new Error("plus: no + button");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // the refresh, twice, with the project list changed in between so the strip really re-renders
  await page.evaluate(async () => {
    const before = projects;
    renderTabs(projects.filter((p) => !p.hidden).slice(1), projects.filter((p) => p.hidden));
    renderTabs(before.filter((p) => !p.hidden), before.filter((p) => p.hidden));
    await loadProjects();
  });
  await page.mouse.up();
  await page.locator(".menu[role=menu]").waitFor({ timeout: 2000 }).catch(() => { throw new Error("plus: the first click during a refresh did not open the menu"); });
  if ((await page.locator("#tabs .tab-add[aria-expanded='true']").count()) !== 1) throw new Error("plus: aria-expanded not set");
  // and a second click (same element) closes it; a third opens it again
  await add.click();
  await page.locator(".menu[role=menu]").waitFor({ state: "detached", timeout: 2000 });
  await add.click();
  await page.locator(".menu[role=menu]").waitFor({ timeout: 2000 });
  await page.keyboard.press("Escape");
  await page.locator(".menu[role=menu]").waitFor({ state: "detached", timeout: 2000 });
  return ", + during refresh ok";
}

async function openNewForm(page) {
  await openMenu(page);
  await page.locator(".menu .menu-item", { hasText: "New project" }).click();
  await page.locator("#slug").waitFor({ timeout: 10000 });
}

// ---- environment variables editor helpers (step 07)
const envRoot = (page) => page.locator("section.envsec .envedit, form .envedit").first();
async function addEnvRow(page, key, value, isPublic) {
  const root = envRoot(page);
  await root.locator("button", { hasText: "+ Add variable" }).click();
  const row = root.locator(".envrow:not(.removed)").last();
  await row.locator("input").nth(0).fill(key);
  if (value !== undefined) await row.locator("input").nth(1).fill(value);
  if (isPublic) await row.locator(".seg button", { hasText: "Public" }).click();
  return row;
}
async function pasteInto(locator, text) {
  await locator.evaluate((input, t) => {
    const dt = new DataTransfer();
    dt.setData("text/plain", t);
    input.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  }, text);
}
async function saveEnv(page) {
  const root = envRoot(page);
  await root.locator("button", { hasText: /^Save/ }).click();
  const status = root.locator('[role="status"]', { hasText: /^(Saved|not saved|Fix|Nothing)/ });
  await status.waitFor({ timeout: 10000 });
  return (await status.textContent()) || "";
}

// ---- session manager helpers (step 11). The rig's backend is a stand-in (tools/rigdash.py): the
// terminal runs a fake claude that echoes lines, so the flow covers the page, the WebSocket and the
// actions without tmux or real Claude.
const conn = (page) => page.locator("#s-conn");
async function termLines(page) {
  return page.evaluate(() => {
    const t = window.hands.sessions.state.term;
    if (!t) return [];
    const b = t.buffer.active, out = [];
    for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l) out.push(l.translateToString(true)); }
    return out;
  });
}
async function waitTermText(page, text, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const lines = await termLines(page);
    if (lines.some((l) => l.includes(text))) return lines;
    await page.waitForTimeout(150);
  }
  throw new Error(`terminal never showed ${JSON.stringify(text)}; last lines: ${JSON.stringify((await termLines(page)).filter(Boolean).slice(-6))}`);
}
async function waitConnected(page) {
  try { await conn(page).filter({ hasText: /^Connected/ }).waitFor({ timeout: 15000 }); }
  catch (e) {
    const st = await page.evaluate(() => ({ conn: document.getElementById("s-conn").textContent, badge: document.getElementById("s-badge").textContent,
      state: window.hands && window.hands.sessions ? window.hands.sessions.state.state : "?", term: !!(window.hands && window.hands.sessions.state.term),
      xterm: typeof Terminal, fit: typeof FitAddon, selected: window.hands.sessions.state.selected, rows: window.hands.sessions.state.rows.length,
      list: document.getElementById("slist").textContent.slice(0, 80), dialogs: document.querySelectorAll("dialog").length }));
    throw new Error(`not connected: ${JSON.stringify(st)}; console: ${errors.slice(-3).join(" | ")}`);
  }
}
async function newSession(page, folder) {
  await page.locator("#s-new").click();
  const dlg = page.locator("dialog.dialog[open]");
  await dlg.waitFor({ timeout: 5000 });
  if (folder !== undefined) await dlg.locator("#s-cwd").fill(folder);
  await dlg.locator("button[type=submit]").click();
  return dlg;
}
async function sessionsFlow(page) {
  await page.locator('#nav a[data-section="sessions"]').click();
  await page.locator("#page-sessions:not([hidden])").waitFor({ timeout: 5000 });
  await page.locator("#slist .empty").waitFor({ timeout: 10000 });
  if ((await page.locator("#slist .sitem").count()) !== 0) throw new Error("sessions: expected an empty list at start");

  // an empty folder is refused inline, the dialog stays open (server-side validation is unit-tested;
  // a deliberate 400 here would count as a console error and fail the rig)
  let dlg = await newSession(page, "");
  await dlg.locator(".error", { hasText: /folder/ }).waitFor({ timeout: 5000 });
  await dlg.locator("button", { hasText: "Cancel" }).click();
  await dlg.waitFor({ state: "detached", timeout: 5000 });

  // new session in the default folder (~/ops), live, output arrives, typing echoes back
  await page.locator("#s-new").click();
  dlg = page.locator("dialog.dialog[open]");
  await dlg.waitFor({ timeout: 5000 });
  const def = await dlg.locator("#s-cwd").inputValue();
  await dlg.locator("button[type=submit]").click();
  await dlg.waitFor({ state: "detached", timeout: 10000 });
  await waitConnected(page);
  if (!(await page.locator("#s-badge", { hasText: "live" }).count())) throw new Error("sessions: badge is not live after New session");
  await waitTermText(page, "ready");
  await page.locator("#xterm").click();
  await page.keyboard.type("hello phoenix");
  await page.keyboard.press("Enter");
  await waitTermText(page, "fake claude: hello phoenix");
  const styles = await page.evaluate(() => ({
    shim: document.querySelectorAll(".xterm-style-shim").length, adopted: document.adoptedStyleSheets.length,
    blocked: Array.from(document.querySelectorAll("#xterm style")).length,
    rowFont: (() => { const s = document.querySelector("#xterm .xterm-rows span"); return s ? getComputedStyle(s).fontFamily : ""; })(),
    bg: getComputedStyle(document.querySelector("#xterm .xterm-screen") || document.body).backgroundColor,
  }));
  if (styles.blocked || styles.adopted < 2) throw new Error(`sessions: xterm styles not via adopted sheets: ${JSON.stringify(styles)}`);
  await page.evaluate(() => window.hands.sessions.poll());
  await page.locator("#slist .sitem .sname", { hasText: "hello phoenix" }).waitFor({ timeout: 10000 });   // title from the transcript
  const sid = await page.locator("#slist .sitem").first().getAttribute("data-id");

  // network blip: drop the socket from the client side; the page reconnects with a fresh ticket
  await page.evaluate(() => window.hands.sessions.state.ws.close());
  await conn(page).filter({ hasText: /Reconnecting/ }).waitFor({ timeout: 5000 });
  await waitConnected(page);
  await page.locator("#xterm").click();
  await page.keyboard.type("after blip");
  await page.keyboard.press("Enter");
  await waitTermText(page, "fake claude: after blip");

  // leave and come back: the session is still there and reconnects by itself (hash keeps the selection)
  await page.reload({ waitUntil: "networkidle" });
  if (!location_is_session(page.url(), sid)) throw new Error(`sessions: url after reload ${page.url()}`);
  await waitConnected(page);
  await waitTermText(page, "ready");

  // stop, resume, stop again; a second session to show a stopped row next to a live one
  await page.locator("#s-stop").click();
  dlg = page.locator("dialog.dialog[open]");
  await dlg.locator("button", { hasText: "Stop session" }).click();
  await page.locator("#s-badge", { hasText: "stopped" }).waitFor({ timeout: 10000 });
  await conn(page).filter({ hasText: /stopped/ }).waitFor({ timeout: 5000 });
  if (!(await page.locator("#s-resume").isEnabled())) throw new Error("sessions: Resume disabled after stop");
  await page.locator("#s-resume").click();
  await waitConnected(page);
  await waitTermText(page, "ready");
  await page.locator("#xterm").click();
  await page.keyboard.type("resumed");
  await page.keyboard.press("Enter");
  await waitTermText(page, "fake claude: resumed");

  dlg = await newSession(page, def.replace(/\/ops$/, "/projects/demo"));
  await dlg.waitFor({ state: "detached", timeout: 10000 });
  await waitConnected(page);
  await page.locator("#xterm").click();
  await page.keyboard.type("second session, stopped on purpose");
  await page.keyboard.press("Enter");
  await waitTermText(page, "fake claude: second");
  await page.locator("#s-stop").click();
  await page.locator("dialog.dialog[open] button", { hasText: "Stop session" }).click();
  await page.locator("#s-badge", { hasText: "stopped" }).waitFor({ timeout: 10000 });
  if ((await page.locator("#slist .sitem").count()) !== 2) throw new Error("sessions: expected 2 sessions");

  // delete a third one (confirmation), list goes back to 2
  dlg = await newSession(page);
  await dlg.waitFor({ state: "detached", timeout: 10000 });
  await waitConnected(page);
  await page.locator("#s-delete").click();
  dlg = page.locator("dialog.dialog[open]");
  await dlg.locator("button", { hasText: "Cancel" }).click();
  if ((await page.locator("#slist .sitem").count()) !== 3) throw new Error("sessions: Cancel deleted the session");
  await page.locator("#s-delete").click();
  await page.locator("dialog.dialog[open] button", { hasText: "Delete session" }).click();
  await page.locator("#slist .sitem").nth(2).waitFor({ state: "detached", timeout: 10000 });
  await conn(page).filter({ hasText: /deleted/ }).waitFor({ timeout: 5000 });

  // Telegram (step 12): the selected session goes live from the head button (restart with --channels),
  // badges appear in head and list, a second live one is refused inline in the New session dialog,
  // and the toggle dialog explains why (both refusals are the page's own pre-check: the server's 409 is
  // unit-tested, and a deliberate 409 here would count as a console error); then "Leave Telegram" frees
  // the slot and the live one takes it back
  await page.locator("#slist .sitem", { hasText: "hello phoenix" }).click();
  await waitConnected(page);
  if (!(await page.locator("#s-tg").isHidden())) throw new Error("telegram: badge shown before going live");
  await page.locator("#s-telegram", { hasText: "Go live on Telegram" }).click();
  dlg = page.locator("dialog.dialog[open]");
  await dlg.locator("button", { hasText: "Restart live on Telegram" }).click();
  await waitConnected(page);
  await waitTermText(page, "ready");
  await page.locator("#s-tg", { hasText: "live on Telegram" }).waitFor({ timeout: 10000 });
  await page.locator("#slist .sitem", { hasText: "hello phoenix" }).locator(".badge.s-telegram").waitFor({ timeout: 5000 });
  if (!(await page.locator("#s-count", { hasText: "on Telegram" }).count())) throw new Error("telegram: count line lacks 'on Telegram'");
  await page.locator("#s-new").click();
  dlg = page.locator("dialog.dialog[open]");
  await dlg.locator(".hint", { hasText: "is live now" }).waitFor({ timeout: 5000 });
  await dlg.locator("#s-tg-new").check();
  await dlg.locator("button[type=submit]").click();
  await dlg.locator("#s-tg-err", { hasText: /already live on Telegram/ }).waitFor({ timeout: 5000 });
  if ((await page.locator("#slist .sitem").count()) !== 2) throw new Error("telegram: a second live session was created");
  await dlg.locator("button", { hasText: "Cancel" }).click();
  await dlg.waitFor({ state: "detached", timeout: 5000 });
  // the stopped second session cannot take the slot either: its dialog warns, the server refuses, no restart
  await page.locator("#slist .sitem", { hasText: "second session" }).click();
  await page.locator("#s-badge", { hasText: "stopped" }).waitFor({ timeout: 10000 });
  await page.locator("#s-telegram", { hasText: "Go live on Telegram" }).click();
  dlg = page.locator("dialog.dialog[open]");
  await dlg.locator(".notice.warn", { hasText: "is live on Telegram now" }).waitFor({ timeout: 5000 });
  await dlg.locator("button", { hasText: "Resume live on Telegram" }).click();
  await conn(page).filter({ hasText: /Could not go live on Telegram: another session is already live/ }).waitFor({ timeout: 10000 });
  if (!(await page.locator("#s-badge", { hasText: "stopped" }).count())) throw new Error("telegram: refused session was started anyway");
  // leave Telegram on the live one (restart without the channel), the badge goes away, then back on for the captures
  await page.locator("#slist .sitem", { hasText: "hello phoenix" }).click();
  await waitConnected(page);
  await page.locator("#s-telegram", { hasText: "Leave Telegram" }).click();
  await page.locator("dialog.dialog[open] button", { hasText: "Restart without Telegram" }).click();
  await waitConnected(page);
  await page.locator("#s-tg[hidden]").waitFor({ state: "attached", timeout: 10000 });
  if (await page.locator("#slist .badge.s-telegram").count()) throw new Error("telegram: list badge left after leaving");
  await page.locator("#s-telegram", { hasText: "Go live on Telegram" }).click();
  await page.locator("dialog.dialog[open] button", { hasText: "Restart live on Telegram" }).click();
  await waitConnected(page);
  await page.locator("#s-tg", { hasText: "live on Telegram" }).waitFor({ timeout: 10000 });
  return `, sessions (new, type, reconnect, reload, stop, resume, 2nd stopped, delete, telegram on/refused/off/on; xterm via ${styles.adopted} adopted sheets, font ${styles.rowFont.split(",")[0]})`;
}
const location_is_session = (url, sid) => url.endsWith(`#/sessions/${sid}`);

async function waitBuilder(page) {
  // the builder settles on: a selected tab, the New project form (no projects) or the empty state (all closed)
  await page.locator('#tabs .tab[aria-selected="true"], #slug, main .empty').first().waitFor({ timeout: 10000 });
}

async function capture(context, name, width, theme) {
  const page = await context.newPage();
  watch(page, `${name}-${theme}-${width}`);
  try {
    const sec = name.startsWith("sessions") ? "sessions" : "builder";
    await page.goto(`${base}/#/${sec}`, { waitUntil: "networkidle" });
    if (sec === "sessions") {
      await page.locator("#page-sessions h1").waitFor({ timeout: 10000 });
      const live = page.locator("#slist .sitem", { hasText: "hello phoenix" });
      if (await live.count()) {
        // desktop: list + terminal side by side; phone: "sessions" is the list, "sessions-term" the terminal view
        if (width > 900 || name !== "sessions") {
          await live.click();
          await waitConnected(page);
          await waitTermText(page, "ready");
          if (width <= 900 && !(await page.locator("#sessions.show-term").count())) throw new Error("phone: terminal view not shown");
          if (width <= 900 && !(await page.locator("#s-back").isVisible())) throw new Error("phone: back button hidden");
        } else if (await page.locator("#sessions.show-term").count()) throw new Error("phone: list view expected");
        if (name === "sessions-new") await page.locator("#s-new").click();
        if (name === "sessions-delete") await page.locator("#s-delete").click();
        if (name === "sessions-telegram") await page.locator("#s-telegram").click();
        if (name !== "sessions" && name !== "sessions-term") await page.locator("dialog.dialog[open]").waitFor({ timeout: 5000 });
      }
    } else if (name === "new") {
      await openNewForm(page);
      await page.locator("#slug").fill("bakery");
      await page.locator("#desc").fill("Ordering site for a small bakery: menu, opening hours, pre-orders for pickup.");
      await addEnvRow(page, "SUPABASE_URL", "https://abc.supabase.co", true);
      await addEnvRow(page, "SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiJ9.demo");
    } else if (name === "newrepo") {
      await openNewForm(page);
      await page.locator("#slug").fill("bakery");
      await page.locator("#fromrepo").check();
      await page.locator("#repo").fill("acme/bakery-site");
      await page.locator("#branch").fill("develop");
      await page.locator("#desc").fill("Add online pre-orders for pickup to the existing bakery site.");
    } else if (name === "newbad") {
      await openNewForm(page);
      await page.locator("#slug").fill("bakery");
      await page.locator("#desc").fill("Ordering site for a small bakery.");
      await addEnvRow(page, "lower-case", "x");
      await addEnvRow(page, "PATH", "");
      await page.locator("form button[type=submit]").click();
      await envRoot(page).locator(".error").first().waitFor({ timeout: 5000 });
    } else if (name === "env") {
      await selectProject(page, "blog");
      await envRoot(page).locator(".envrow").first().waitFor({ timeout: 10000 });
    } else if (name === "envbad") {
      await selectProject(page, "blog");
      await envRoot(page).locator(".envrow").first().waitFor({ timeout: 10000 });
      await addEnvRow(page, "bad key", "x");
      await addEnvRow(page, "HANDS_SECRET", "y");
      await addEnvRow(page, "SUPABASE_URL", "dup");
      await addEnvRow(page, "NEEDS_VALUE", "");
      await saveEnv(page);
      await envRoot(page).locator(".error").first().waitFor({ timeout: 5000 });
    } else if (name === "nav") {
      if (width <= 900) { await page.locator("#navtoggle").click(); await page.locator("body.nav-open").waitFor({ timeout: 5000 }); }
    } else if (name === "tabs") {
      await waitBuilder(page);
    } else if (name === "menu") {
      await waitBuilder(page);
      await openMenu(page);
    } else if (name === "close") {
      await waitBuilder(page);
      await page.locator('#tabs .tab[aria-selected="true"] .x').click();
      await page.locator("dialog.dialog[open]").waitFor({ timeout: 5000 });
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
  for (const sec of ["sessions", "builder"]) {
    await page.locator(`#nav a[data-section="${sec}"]`).click();
    await page.locator(`#page-${sec}:not([hidden])`).waitFor({ timeout: 5000 });
    await page.locator(`#nav a[data-section="${sec}"][aria-current="page"]`).waitFor({ timeout: 5000 });
  }
  await waitBuilder(page);
  const names = await page.locator("#tabs .tab .tname").allTextContents();
  const closedBefore = (await page.locator("#tabs .tabs-hint").textContent().catch(() => "")) || "";
  if (!names.length) {
    // nothing open: either no projects at all (New project form) or everything closed (empty state + reopen)
    if (await page.locator("main .empty").count()) {
      await page.locator("main .empty button", { hasText: "Reopen" }).click();
      await page.locator(".menu[role=menu]").waitFor({ timeout: 5000 });
      const n = await page.locator(".menu .menu-item .mname").count();
      if (!n) throw new Error("tour: all projects closed but the reopen list is empty");
      await page.keyboard.press("Escape");
      await page.locator(".menu[role=menu]").waitFor({ state: "detached", timeout: 5000 });
    } else {
      await page.locator("#slug").waitFor({ timeout: 10000 });
      await openMenu(page);
      if (!(await page.locator(".menu .muted", { hasText: "No closed projects" }).count())) throw new Error("tour: expected 'No closed projects'");
      await page.keyboard.press("Escape");
    }
    console.log(`tour ok: 0 open projects (${closedBefore || "none"}), + menu works`);
    await page.close();
    return;
  }
  let attemptFiles = 0, planFiles = 0, logs = 0;
  const plusNote = await plusDuringRefresh(page);
  for (const name of names) {
    await selectProject(page, name);
    await page.locator("#attempts-wrap section").waitFor({ timeout: 5000 }).catch(() => {});   // loads async
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

  // keyboard: focus the first tab, ArrowRight, Enter opens the second (if any); Escape closes the menu
  await tab(page, names[0]).focus();
  if (names.length > 1) {
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Enter");
    await page.locator(`#tabs .tab[data-name="${names[1]}"][aria-selected="true"]`).waitFor({ timeout: 8000 });
    await page.locator("main h2", { hasText: names[1] }).first().waitFor({ timeout: 10000 });
  }

  // close a tab (confirmation dialog), then reopen it from "+" and check it comes back with its data
  const victim = names[0];
  await selectProject(page, victim);
  await page.waitForTimeout(700);
  const h2Before = (await page.locator("main section h2").allTextContents()).join("|");
  await tab(page, victim).locator(".x").click();
  const dlg = page.locator("dialog.dialog[open]");
  await dlg.waitFor({ timeout: 5000 });
  await dlg.locator("button", { hasText: "Cancel" }).click();                      // cancel keeps the tab
  await dlg.waitFor({ state: "detached", timeout: 5000 });
  if (!(await tab(page, victim).count())) throw new Error("tour: Cancel removed the tab");
  await tab(page, victim).focus();
  await page.keyboard.press("Delete");                                            // Delete closes too
  await dlg.waitFor({ timeout: 5000 });
  await dlg.locator("button", { hasText: "Close tab" }).click();
  await tab(page, victim).waitFor({ state: "detached", timeout: 8000 });
  if (names.length > 1) await page.locator('#tabs .tab[aria-selected="true"]').waitFor({ timeout: 8000 });
  else await page.locator("main .empty").waitFor({ timeout: 8000 });
  await page.reload({ waitUntil: "networkidle" });                                // hidden status survives a reload
  await waitBuilder(page);
  if (await tab(page, victim).count()) throw new Error("tour: closed tab came back after reload");
  await openMenu(page);
  await page.locator(".menu .menu-item", { has: page.locator(".mname", { hasText: new RegExp(`^${victim}$`) }) }).click();
  await page.locator(`#tabs .tab[data-name="${victim}"][aria-selected="true"]`).waitFor({ timeout: 8000 });
  await page.locator("main h2", { hasText: victim }).first().waitFor({ timeout: 10000 });
  await page.waitForTimeout(700);
  const h2After = (await page.locator("main section h2").allTextContents()).join("|");
  if (h2After !== h2Before) throw new Error(`tour: reopened ${victim} shows different sections: ${h2Before} vs ${h2After}`);

  let envNote = "";
  if (names.includes("wiki")) envNote = await envFlow(page);
  envNote += await sessionsFlow(page);
  await page.locator('#nav a[data-section="builder"]').click();
  await waitBuilder(page);

  await openNewForm(page);
  // the New project form: local rows, paste, validation blocks the submit (no request is sent)
  await addEnvRow(page, "lower", "x");
  let newPosts = 0;
  const onReq = (r) => { if (r.method() === "POST" && r.url().includes("/api/new")) newPosts++; };
  page.on("request", onReq);
  await page.locator("#slug").fill("bakery");
  await page.locator("#desc").fill("x");
  await page.locator("form button[type=submit]").click();
  await envRoot(page).locator(".error").first().waitFor({ timeout: 5000 });
  page.off("request", onReq);
  if (newPosts) throw new Error("tour: create was submitted despite an invalid variable");
  await pasteInto(envRoot(page).locator(".envrow input").first(), "API_URL=https://x\nAPI_KEY=k\n");
  if ((await envRoot(page).locator(".envrow").count()) !== 2) throw new Error("tour: paste into the New project form did not add rows");
  await page.locator("#navtoggle").click().catch(() => {});   // desktop: hidden, that's fine
  console.log(`tour ok: ${names.length} projects, ${attemptFiles} attempt files, ${planFiles} plan files, ${logs} planner logs, ` +
              `keyboard, close/reopen ${victim}, sections + new project${envNote}${plusNote}`);
  await page.close();
}

// Environment panel on the wiki project (project_env configured, no file yet): add 3 variables (2 secret,
// 1 public), save, reload, check masks, replace a secret, delete one, paste a block. Secrets must never
// appear in the DOM or in any API response after the save.
async function envFlow(page) {
  const S1 = "sk_live_SECRETONE_7f3a", S2 = "whsec_SECRETTWO_91bc", S1b = "sk_live_REPLACED_55de", PUB = "https://pub.example";
  const seen = [];
  const onResp = async (r) => {
    if (!r.url().includes("/api/")) return;
    try { seen.push(`${r.url()} :: ${await r.text()}`); } catch (e) { /* redirected/aborted */ }
  };
  const leak = (where) => {
    for (const s of [S1, S2, S1b]) {
      const hit = seen.find((x) => x.includes(s));
      if (hit) throw new Error(`env: secret found in a response after save (${where}): ${hit.slice(0, 120)}`);
    }
  };
  const bodyHas = async (s) => (await page.evaluate(() => document.body.innerHTML)).includes(s);

  await selectProject(page, "wiki");
  const root = envRoot(page);
  await root.locator("button", { hasText: "+ Add variable" }).waitFor({ timeout: 10000 });
  if (await root.locator(".envrow").count()) throw new Error("env: wiki should start with no variables");
  await addEnvRow(page, "STRIPE_SECRET", S1);
  await addEnvRow(page, "STRIPE_WEBHOOK", S2);
  await addEnvRow(page, "PUBLIC_URL", PUB, true);
  page.on("response", onResp);
  let st = await saveEnv(page);
  if (!st.startsWith("Saved: 3 set")) throw new Error(`env: unexpected status after save: ${st}`);
  await page.waitForTimeout(300);
  leak("first save");
  for (const s of [S1, S2]) if (await bodyHas(s)) throw new Error("env: secret in the DOM after save");
  if (!(await bodyHas(PUB))) throw new Error("env: public value missing after save");

  await page.reload({ waitUntil: "networkidle" });
  await waitBuilder(page);
  await selectProject(page, "wiki");
  await root.locator(".envrow").nth(2).waitFor({ timeout: 10000 });
  const keys = await root.locator(".envrow input[readonly]").evaluateAll((els) => els.map((e) => e.value));
  if (keys.join() !== "STRIPE_SECRET,STRIPE_WEBHOOK,PUBLIC_URL") throw new Error(`env: keys after reload: ${keys}`);
  const vals = await root.locator(".envrow").evaluateAll((rows) => rows.map((r) => {
    const v = r.querySelectorAll("input")[1]; return [v.value, v.type, v.placeholder.startsWith("••••")]; }));
  if (vals[0][0] !== "" || vals[0][1] !== "password" || !vals[0][2]) throw new Error(`env: secret row not masked: ${JSON.stringify(vals[0])}`);
  if (vals[2][0] !== PUB || vals[2][1] !== "text") throw new Error(`env: public row wrong: ${JSON.stringify(vals[2])}`);
  for (const s of [S1, S2]) if (await bodyHas(s)) throw new Error("env: secret in the DOM after reload");
  leak("reload");

  // replace the first secret, delete the second
  await root.locator(".envrow").nth(0).locator("input").nth(1).fill(S1b);
  await root.locator(".envrow").nth(1).locator("button[aria-label^=Remove]").click();
  await root.locator(".envrow.removed").waitFor({ timeout: 5000 });
  st = await saveEnv(page);
  if (!st.startsWith("Saved: 1 set, 1 deleted")) throw new Error(`env: unexpected status after replace/delete: ${st}`);
  await page.waitForTimeout(300);
  leak("replace");
  if (await bodyHas(S1b)) throw new Error("env: replaced secret in the DOM");
  if ((await root.locator(".envrow").count()) !== 2) throw new Error("env: delete did not remove the row");

  // paste a block into a new row's Key field: one row per KEY=value line, all Secret
  await root.locator("button", { hasText: "+ Add variable" }).click();
  await pasteInto(root.locator(".envrow:not(.removed)").last().locator("input").first(), "# pasted\nMAIL_FROM=hi@x.test\nexport MAIL_PASS=p4ss\nbad line\n");
  if ((await root.locator(".envrow").count()) !== 4) throw new Error("env: paste did not add rows");
  const pressed = await root.locator(".envrow").nth(3).locator('.seg button[aria-pressed="true"]').textContent();
  if (pressed !== "Secret") throw new Error("env: pasted rows must be Secret");
  st = await saveEnv(page);
  if (!st.startsWith("Saved: 2 set")) throw new Error(`env: unexpected status after paste: ${st}`);
  page.off("response", onResp);
  const finalKeys = await root.locator(".envrow input[readonly]").evaluateAll((els) => els.map((e) => e.value));
  if (finalKeys.join() !== "STRIPE_SECRET,PUBLIC_URL,MAIL_FROM,MAIL_PASS") throw new Error(`env: final keys: ${finalKeys}`);
  return `, env flow (add 3, reload, replace, delete, paste; ${seen.length} responses checked, no secret leaked)`;
}

// Side-by-side sheets: the Project builder (bookings) and the Session manager, per theme
// and width, so the pages can be compared for sidebar, header, buttons, fonts and colours at a glance.
async function compareShots(browser) {
  const fs = require("fs");
  const context = await browser.newContext();
  try {
    for (const theme of ["light", "dark"]) {
      for (const vp of VIEWPORTS) {
        const a = path.join(out, `bookings-${theme}-${vp.width}.png`), b = path.join(out, `sessions-${theme}-${vp.width}.png`);
        if (!fs.existsSync(a) || !fs.existsSync(b)) continue;
        const html = `<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#888;font:13px system-ui;display:flex;gap:12px;padding:12px;align-items:flex-start}` +
          `figure{margin:0}figcaption{color:#fff;padding:4px 0}img{display:block;width:${vp.width}px;max-height:900px;object-fit:cover;object-position:top}</style>` +
          `<figure><figcaption>Project builder · ${theme} · ${vp.width}</figcaption><img src="file://${a}"></figure>` +
          `<figure><figcaption>Session manager · ${theme} · ${vp.width}</figcaption><img src="file://${b}"></figure>`;
        const f = path.join(out, `compare-${theme}-${vp.width}.html`);
        fs.writeFileSync(f, html);
        const page = await context.newPage();
        await page.setViewportSize({ width: vp.width * 2 + 36, height: 940 });
        await page.goto(`file://${f}`, { waitUntil: "load" });
        await page.screenshot({ path: path.join(out, `compare-${theme}-${vp.width}.png`), fullPage: true });
        await page.close();
        fs.unlinkSync(f);
        console.log(`shot compare-${theme}-${vp.width}.png`);
      }
    }
  } finally {
    await context.close();
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
          await page.waitForTimeout(600);
          await page.screenshot({ path: path.join(mout, `${name}-${scheme}-${vp.width}.png`), fullPage: true });
          console.log(`shot ${name}-${scheme}-${vp.width}.png`);
          const res = page.locator("#result");          // probe pages (xterm CSP test) report here
          if (await res.count()) console.log(`  ${name} result: ${await res.textContent()}`);
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
    await compareShots(browser);
  } finally {
    await browser.close();
  }
  if (errors.length) {
    console.error("browser console errors:\n  " + errors.join("\n  "));
    process.exit(1);
  }
  console.log("console clean: no errors, no CSP violations");
})().catch((e) => {
  console.error(`shots.js failed: ${e.message}`);
  if (errors.length) console.error("browser console errors so far:\n  " + errors.join("\n  "));
  process.exit(1);
});
