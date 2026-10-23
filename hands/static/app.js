// hands dashboard. Everything shown here may contain agent-written text: build DOM with textContent only.
"use strict";
const $ = (sel) => document.querySelector(sel);
let current = null;
let timer = null;

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : String(k));
  return e;
}

async function api(path, opts) {
  let r;
  try { r = await fetch(path, opts); }
  catch (e) {                         // no response at all (Safari: "Load failed"): the request may never have arrived
    const err = new Error(`network error (${e && e.message ? e.message : "no response"})`);
    err.network = true;
    throw err;
  }
  if (r.status === 401) { $("#main").replaceChildren(el("p", {}, "Not logged in. On the VPS run: hands-dash url")); throw new Error("401"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || r.status);
  return body;
}

// ---- in-page notices: never a browser dialog (they block the page and look foreign)
function toast(msg, bad) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const t = el("div", { class: bad ? "toast bad" : "toast", role: bad ? "alert" : "status" },
    el("span", { class: "dot", "aria-hidden": "true" }), el("span", {}, msg),
    el("button", { class: "x", type: "button", "aria-label": "Dismiss", on: { click: () => t.remove() } }, "×"));
  box.append(t);
  setTimeout(() => t.remove(), bad ? 9000 : 4000);
}
// showModal() where it exists; otherwise the dialog opens non-modally, fixed near the top (old Safari)
function openDialog(dlg) {
  if (typeof dlg.showModal === "function") { dlg.showModal(); return; }
  dlg.classList.add("fallback");
  dlg.setAttribute("open", "");
}

const badge = (s) => el("span", { class: `badge s-${String(s || "").replace(/[^a-z_]/g, "")}` }, s || "?");
const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
const localTs = (iso) => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleString() : (iso || ""); };
let mode = "project";        // "project" | "new" | "creating" | "empty" (every project closed)
let creatingSlug = null;
let creatingReset = false;   // the "creating" view is following a reset (destroy + create), not a new project
let projects = [];           // last /api/projects result (open and closed)

async function loadProjects() {
  const list = await api("/api/projects");
  projects = list;
  const open = list.filter((p) => !p.hidden);
  if (current && !open.some((p) => p.name === current)) current = null;
  renderTabs(open, list.filter((p) => p.hidden));
  if (mode === "empty" && open.length) return select(open[0].name);
  if (mode !== "project") return;
  if (!current && open.length) return select(open[0].name);
  if (!open.length) { if (list.length) showEmpty(); else showNew(); }
}

function select(name) {
  mode = "project";
  domainAck = false;
  if (current && current !== name) hands.store.close(current);
  current = name;
  delete envPanels[name];              // fresh editor (and listing) each time a project is opened
  try { localStorage.setItem("hands.project", name); } catch (e) { /* ignore */ }
  $("#main").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  viewerTitle.textContent = "File";
  viewer.textContent = "Pick an attempt file.";
  lastDetail = null; lastLog = null; attemptsCache = { name: null, rows: [] };
  logPanel.clear();
  hands.store.open(name);              // change feed + first fetch; onSnapshot renders what arrives
  const known = hands.store.get(name);
  if (known && known.detail) onSnapshot(name, { detail: known.detail });   // seen before: paint now, a 304 publishes nothing
  loadProjects().catch(() => {});      // tab highlight at once, not on the next tick
}

// The store publishes (name, patch) when a project's detail, log or attempts changed on the server (or an
// error occurred); the page re-renders from the full snapshot, patched in place.
function onSnapshot(name, patch) {
  if (name !== current || mode !== "project") return;
  const snap = hands.store.get(name);
  if (!snap) return;
  if (patch.error !== undefined && !patch.detail) { $("#clock").textContent = `update failed: ${patch.error}`; return; }
  const d = snap.detail;
  if (!d) return;
  if (d.error) { $("#main").replaceChildren(el("section", { class: "reason" }, d.error)); return; }
  lastDetail = d; lastLog = snap.log;
  attemptsCache = { name, rows: snap.attempts || [] };
  render(name, d, snap.log);
  if (patch.log || !logPanel.lines().length) logPanel.update((snap.log && snap.log.lines) || []);
  $("#clock").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// ---- project tabs (WAI-ARIA tabs: roving tabindex, arrows move, Enter opens, Delete closes)
// The "+" button is created once and never removed from the strip: Chromium and WebKit drop a click when
// the pressed node leaves the DOM between pointerdown and pointerup, even for a moment, and the 4 s refresh
// used to rebuild the whole strip, so the first click on "+" sometimes did nothing (requirement 4). Tabs
// are reconciled in place by project name for the same reason; the strip is touched only when something
// changed.
let lastScrolledTo = null;
let lastTabsKey = null;
const addBtn = el("button", { class: "tab-add", type: "button", "data-name": "+", "aria-haspopup": "menu", "aria-expanded": "false",
  "aria-label": "New project or reopen a closed one", title: "New project or reopen a closed one", on: { click: () => toggleMenu() } }, "+");
function tabButton(p) {
  return el("button", {
    class: "tab", type: "button", role: "tab", "data-name": p.name, title: p.name,
    on: { click: (ev) => { if (ev.target.closest(".x")) closeTab(p); else select(p.name); }, keydown: (ev) => tabKey(ev, p) } },
    el("span", { class: "tname" }, p.name), badge(""), roundTag(p.round),
    el("span", { class: "x", "aria-label": `Close ${p.name}`, title: "Close tab (Delete)" }, "×"));
}
// change-round state per project (0.5.8): "C1" tag coloured by state; nothing for round 0
function roundState(r) {
  if (!r) return "";
  if (r.status === "merged") return "merged";
  if (r.status === "merging" || r.status === "discarding") return r.status;
  return r.merge_ready || (r.total && r.passed === r.total) ? "all passed" : "open";
}
function roundTag(r) {
  const st = roundState(r);
  if (!st) return el("span", { class: "rtag none" });
  const text = { "all passed": r.self ? "ready" : "merge?", open: "", merged: "merged", merging: "merging", discarding: "discarding" }[st];
  const title = { "all passed": r.self ? "all steps passed; merged by Install update" : "all steps passed: ready to merge",
    open: `building (${r.passed}/${r.total} steps passed)`, merged: "merged into main", merging: "merge in progress", discarding: "discard in progress" }[st];
  return el("span", { class: `rtag r-${st.replace(/ /g, "_")}`, title: `Change round C${r.id}: ${title}` }, `C${r.id}${text ? " " + text : ""}`);
}
function renderTabs(open, hidden) {
  const strip = $("#tabs");
  const selected = mode === "project" ? current : null;
  addBtn.setAttribute("aria-current", String(mode === "new" || mode === "creating"));
  if (!strip.contains(addBtn)) strip.append(addBtn);
  const key = JSON.stringify([open.map((p) => [p.name, p.error ? "error" : p.status, roundState(p.round), p.round && p.round.id]),
    selected, open.length ? 0 : hidden.length]);
  if (key === lastTabsKey) { updateTabShadows(); return; }
  lastTabsKey = key;
  const have = new Map([...strip.querySelectorAll(".tab")].map((t) => [t.dataset.name, t]));
  const want = new Set(open.map((p) => p.name));
  for (const [name, t] of have) if (!want.has(name)) t.remove();
  let cursor = strip.firstChild;                    // walk the strip, keep nodes that are already in place
  open.forEach((p, i) => {
    let t = have.get(p.name);
    if (!t) { t = tabButton(p); }
    if (t !== cursor) strip.insertBefore(t, cursor); else cursor = cursor.nextSibling;
    t.setAttribute("aria-selected", String(p.name === selected));
    t.tabIndex = (selected ? p.name === selected : i === 0) ? 0 : -1;
    const status = p.error ? "error" : p.status;
    const b = t.querySelector(".badge");
    if (b.textContent !== (status || "?")) b.replaceWith(badge(status));
    const rt = t.querySelector(".rtag"), nt = roundTag(p.round);
    if (rt && (rt.className !== nt.className || rt.textContent !== nt.textContent)) rt.replaceWith(nt);
  });
  for (const h of strip.querySelectorAll(".tabs-hint")) h.remove();
  if (!open.length) addBtn.after(el("span", { class: "tabs-hint" }, hidden.length ? `${hidden.length} closed` : "No projects yet"));
  if (menuEl) positionMenu();
  if (selected && selected !== lastScrolledTo) {
    lastScrolledTo = selected;
    const t = strip.querySelector('.tab[aria-selected="true"]');
    if (t) try { t.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (e) { /* old Safari */ }
  }
  updateTabShadows();
}

function updateTabShadows() {
  const s = $("#tabs"), w = $("#tabswrap");
  w.classList.toggle("scroll-l", s.scrollLeft > 2);
  w.classList.toggle("scroll-r", s.scrollLeft + s.clientWidth < s.scrollWidth - 2);
}

function tabKey(ev, p) {
  const tabs = [...$("#tabs").querySelectorAll(".tab")];
  const i = tabs.findIndex((t) => t.dataset.name === p.name);
  const go = (j) => { ev.preventDefault(); const t = tabs[(j + tabs.length) % tabs.length]; t.focus(); t.scrollIntoView && t.scrollIntoView({ block: "nearest", inline: "nearest" }); };
  if (ev.key === "ArrowRight") go(i + 1);
  else if (ev.key === "ArrowLeft") go(i - 1);
  else if (ev.key === "Home") go(0);
  else if (ev.key === "End") go(tabs.length - 1);
  else if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); closeTab(p); }
}

async function closeTab(p) {
  const live = p.alive || p.status === "planning";
  const what = p.status === "planning" ? "planning" : "building";
  const ok = await confirmDialog({
    title: `Close ${p.name}?`,
    warn: live ? `${p.name} is ${what} right now. Closing the tab does not stop it: the ${what === "planning" ? "planner" : "runner"} ` +
                 "keeps going in the background and you will not see it until you reopen the tab." : "",
    body: "Closing a tab only hides the project here. Nothing is deleted: its repos, state and settings stay, " +
          "and you can reopen it any time from the + button.",
    ok: "Close tab" });
  if (!ok) return;
  try { await api(`/api/p/${encodeURIComponent(p.name)}/hide`, { method: "POST", headers: { "X-Hands": "1" } }); }
  catch (e) { toast(`Could not close ${p.name}: ${e.message}`, true); return; }
  const open = projects.filter((x) => !x.hidden);
  const i = open.findIndex((x) => x.name === p.name);
  const next = open[i + 1] || open[i - 1];
  projects = projects.map((x) => (x.name === p.name ? { ...x, hidden: true } : x));
  if (current === p.name) {
    current = null;
    try { localStorage.removeItem("hands.project"); } catch (e) { /* ignore */ }
    if (next) select(next.name);
  }
  await loadProjects().catch(() => {});
  const t = $("#tabs .tab[aria-selected='true']") || $("#tabs .tab-add");
  if (t) t.focus();
}

async function reopen(name) {
  closeMenu(false);
  const unhide = () => api(`/api/p/${encodeURIComponent(name)}/unhide`, { method: "POST", headers: { "X-Hands": "1" } });
  try {
    try { await unhide(); }
    catch (e) { if (!e.network) throw e; await unhide(); }   // idempotent: one retry when no response came back
  } catch (e) { toast(`Could not reopen ${name}: ${e.message}`, true); return; }
  projects = projects.map((x) => (x.name === name ? { ...x, hidden: false } : x));
  select(name);
}

function showEmpty() {
  mode = "empty";
  $("#main").replaceChildren(el("section", {}, el("div", { class: "empty" }, el("span", { class: "glyph" }, "▦"),
    el("strong", {}, "No open projects"),
    el("span", { class: "small" }, "Every project is closed. Reopen one from the + button, or start a new one."),
    el("div", { class: "row" },
      el("button", { class: "btn primary", type: "button", on: { click: showNew } }, "New project"),
      el("button", { class: "btn", type: "button", on: { click: () => toggleMenu() } }, "Reopen a closed project…")))));
}

// ---- "+" menu: position: fixed, offset computed from the button (Safari-safe; the scrolling strip cannot clip it)
let menuEl = null;
function ago(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(d)) return "";
  const s = Math.max(0, (Date.now() - d) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function positionMenu() {
  const add = $("#tabs .tab-add");
  if (!menuEl || !add) return;
  const r = add.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.left, window.innerWidth - menuEl.offsetWidth - 8));
  menuEl.style.left = `${left}px`;            // CSSOM, allowed by the CSP (style attributes are not)
  menuEl.style.top = `${r.bottom + 6}px`;
}
function onDocPointer(ev) {
  if (menuEl && !menuEl.contains(ev.target) && !ev.target.closest(".tab-add")) closeMenu(false);
}
function closeMenu(refocus) {
  if (!menuEl) return;
  menuEl.remove();
  menuEl = null;
  const add = $("#tabs .tab-add");
  if (add) { add.setAttribute("aria-expanded", "false"); if (refocus) add.focus(); }
  document.removeEventListener("pointerdown", onDocPointer, true);
  window.removeEventListener("resize", positionMenu);
  window.removeEventListener("scroll", positionMenu, true);
}
function toggleMenu() {
  if (menuEl) return closeMenu(true);
  const add = $("#tabs .tab-add");
  if (!add) return;
  const hidden = projects.filter((p) => p.hidden);
  const item = (kids, onclick, attrs) =>
    el("button", { class: "menu-item", type: "button", role: "menuitem", on: { click: onclick }, ...(attrs || {}) }, ...kids);
  menuEl = el("div", { class: "menu", role: "menu", "aria-label": "Add project", on: { keydown: menuKey } },
    item(["＋ New project", el("span", { class: "k" }, "N")], () => { closeMenu(false); showNew(); }),
    el("div", { class: "menu-sep" }),
    el("div", { class: "menu-head" }, "Reopen a closed project"),
    hidden.length ? hidden.map((p) => item(
      [badge(p.error ? "error" : p.status), el("span", { class: "mname" }, p.name), el("span", { class: "mwhen" }, ago(p.updated_at))],
      () => reopen(p.name), { title: `${p.name} · ${p.status}${p.updated_at ? ` · last activity ${localTs(p.updated_at)}` : ""}` }))
      : el("div", { class: "muted" }, "No closed projects."));
  document.body.append(menuEl);
  add.setAttribute("aria-expanded", "true");
  positionMenu();
  menuEl.querySelector(".menu-item").focus();
  setTimeout(() => document.addEventListener("pointerdown", onDocPointer, true), 0);
  window.addEventListener("resize", positionMenu);
  window.addEventListener("scroll", positionMenu, true);
}
function menuKey(ev) {
  const items = [...menuEl.querySelectorAll(".menu-item")];
  const i = items.indexOf(document.activeElement);
  const go = (j) => { ev.preventDefault(); items[(j + items.length) % items.length].focus(); };
  if (ev.key === "ArrowDown") go(i + 1);
  else if (ev.key === "ArrowUp") go(i - 1);
  else if (ev.key === "Home") go(0);
  else if (ev.key === "End") go(items.length - 1);
  else if (ev.key === "Escape") { ev.preventDefault(); closeMenu(true); }
  else if (ev.key === "Tab") closeMenu(false);
  else if (ev.key === "n" || ev.key === "N") { ev.preventDefault(); closeMenu(false); showNew(); }
}

// ---- confirmation dialog (<dialog>; non-modal fallback where showModal is missing; never a browser dialog)
// requireText: the OK button stays disabled until exactly that text is typed (used for a reset).
// checkbox: a tick box with that label; the promise then resolves {ok: true, checked} instead of true.
function confirmDialog({ title, body, warn, ok, okClass, requireText, checkbox }) {
  return new Promise((resolve) => {
    let done = false;
    const box = checkbox ? el("input", { type: "checkbox", id: "dlg-check" }) : null;
    const finish = (v) => {
      if (done) return;
      done = true;
      resolve(v === true && box ? { ok: true, checked: box.checked } : v);
      if (typeof dlg.close === "function") dlg.close();
      dlg.remove();
    };
    const cancel = el("button", { class: "btn", type: "button", on: { click: () => finish(false) } }, "Cancel");
    const okBtn = el("button", { class: okClass || "btn danger", type: "button", ...(requireText ? { disabled: "" } : {}),
      on: { click: () => { if (!okBtn.disabled) finish(true); } } }, ok || "OK");
    const field = requireText
      ? el("input", { type: "text", id: "dlg-text", autocomplete: "off", spellcheck: "false", "data-testid": "dlg-text",
          on: { input: () => { okBtn.disabled = field.value.trim() !== requireText; } } })
      : null;
    const dlg = el("dialog", { class: "dialog", "aria-labelledby": "dlg-title", on: { close: () => finish(false) } },
      el("h2", { id: "dlg-title" }, title),
      warn ? el("div", { class: "notice warn" }, warn) : null,
      el("p", { class: "muted" }, body),
      field ? el("label", { class: "field", for: "dlg-text" }, `Type ${requireText}`, field) : null,
      box ? el("label", { class: "check", for: "dlg-check" }, box, el("span", {}, checkbox)) : null,
      el("div", { class: "row" }, cancel, okBtn));
    document.body.append(dlg);
    openDialog(dlg);
    (field || cancel).focus();
  });
}

async function action(name, act, label) {
  if (!(await confirmDialog({ title: `${label} for ${name}?`, body: `Runs \`hands ${name} ${act}\` on the VPS.`, ok: label, okClass: "btn primary" }))) return;
  const out = document.getElementById("action-out");
  out.textContent = `${label}…`;
  try {
    const r = await api(`/api/p/${encodeURIComponent(name)}/action/${act}`,
      { method: "POST", headers: { "X-Hands": "1" } });
    out.textContent = `rc=${r.rc}\n${r.output}`;
  } catch (e) { out.textContent = `failed: ${e.message}`; }
  refresh(false);
}

// ---- delegated actions (0.6): every control inside #main carries data-act (+ data-arg/...) and is handled
// here with the snapshot of the moment, never by a closure on the node (a patch keeps the node).
let lastDetail = null;                 // the detail the page was last rendered from
let lastLog = null;
let logPanel = null;                   // hands.logpanel instance, created once the scripts are loaded
const actionOut = el("pre", { id: "action-out", class: "out", "data-self-managed": "" }, "");
function dispatch(ev) {
  const t = ev.target.closest("[data-act]");
  if (!t) return;
  const d = lastDetail, a = t.dataset;
  if (t.tagName === "A") ev.preventDefault();
  if (a.act === "run" && d) action(d.name, a.arg, a.label);
  else if (a.act === "plan" && d) planAction(d.name, a.arg, a.label, a.head || "");
  else if (a.act === "round" && d && d.round && d.round.merge) roundAction(d, a.arg, d.round.merge);
  else if (a.act === "attempt-file" && d) { viewerTitle.textContent = `${a.att} / ${a.file}`; showFile(d.name, a.att, a.file, viewer); openPanel("viewer"); }
  else if (a.act === "plan-file" && d) showPlanFile(d.name, a.arg);
  else if (a.act === "focus-amend") { const x = document.getElementById("amendtext"); if (x) { x.scrollIntoView({ block: "center" }); x.focus(); } }
  else if (a.act === "select") select(a.arg);
  else if (a.act === "usage-refresh") refreshUsage(t);
  else if (a.act === "style-ref-add" && d) toggleAddRefs(d.name);
  else if (a.act === "style-ref-retry" && d) refAction(d.name, `/style-refs/${a.arg}/retry`, "POST", `Retrying style ref ${a.arg}…`);
  else if (a.act === "style-ref-remove" && d) removeRef(d.name, a.arg);
  else if (a.act === "setup-retry" && d) refAction(d.name, "/setup/retry", "POST", "Retrying setup…");
  else if (a.act === "plan-chat") planChat(a.arg, t);
  else if (a.act === "plan-chat-reset") planChatReset(a.arg, t);
  else if (a.act === "project-reset") projectReset(a.arg, t);
  else if (a.act === "brief-approve" && d) approveBrief(d.name, a.arg, t);
}
function openPanel(key) {
  const p = document.querySelector(`details.panel[data-panel="${key}"]`);
  if (p && !p.open) { p.open = true; p.scrollIntoView({ block: "nearest" }); }
}
// a node that owns its state is rendered once; later trees carry a keyed stand-in the patch leaves alone
const keep = (node, key) => (node.isConnected ? el(node.tagName.toLowerCase(), { "data-key": key, "data-self-managed": "" }) : node);

// ---- style refs and setup (Phase 2): the panel is nextform.js's; the add-URL form owns its fields, so it is
// built once per project and kept across patches
let addRefs = { name: null, node: null };
function addRefsNode(name) {
  if (addRefs.name !== name) {
    const list = hands.nextform.styleUrlList(document);
    const out = el("p", { class: "muted small" }, "");
    const save = el("button", { class: "btn sm primary", type: "submit" }, "Capture these URLs");
    const form = el("form", { class: "addrefs hidden", "data-key": `style-ref-add-form:${name}`, "data-self-managed": "", on: { submit: async (ev) => {
      ev.preventDefault();
      const urls = list.values();
      if (!list.validate() || !urls.length) { out.textContent = urls.length ? "Fix the URLs marked above." : "Enter at least one URL."; return; }
      save.disabled = true;
      out.textContent = "Saving…";
      try {
        await api(`/api/projects/${encodeURIComponent(name)}/style-refs`, { method: "POST",
          headers: { "X-Hands": "1", "Content-Type": "application/json" }, body: JSON.stringify({ urls }) });
        list.reset(); out.textContent = ""; form.classList.add("hidden");
        toast(`Capturing ${urls.length} style URL${urls.length === 1 ? "" : "s"}…`);
      } catch (e) { out.textContent = `failed: ${e.message}`; }
      save.disabled = false;
      refresh(false);
    } } }, el("p", { class: "muted" }, "Added as a change request: each URL is captured, analysed and frozen before the next planning round."),
      list.root, el("div", { class: "row" }, save), out);
    addRefs = { name, node: form };
  }
  return keep(addRefs.node, `style-ref-add-form:${name}`);
}
function toggleAddRefs(name) {
  if (addRefs.name !== name || !addRefs.node) return;
  const hidden = addRefs.node.classList.toggle("hidden");
  if (!hidden) { const i = addRefs.node.querySelector("input"); if (i) i.focus(); }
}
function styleRefsHint(n) {
  const refs = n.style_refs || [];
  const failed = refs.filter((r) => r.status === "failed").length;
  const s = n.setup && n.setup.status && n.setup.status !== "none" ? ` · setup ${n.setup.status}` : "";
  return `${refs.length} ref${refs.length === 1 ? "" : "s"}${failed ? ` · ${failed} failed` : ""}${s}`;
}
async function refAction(name, path, method, label) {
  try {
    await api(`/api/projects/${encodeURIComponent(name)}${path}`, { method, headers: { "X-Hands": "1" } });
    toast(label);
  } catch (e) { toast(`refused: ${e.message}`, true); }
  refresh(false);
}
async function removeRef(name, index) {
  if (!(await confirmDialog({ title: `Remove style ref ${index} from ${name}?`, body: "The URL and its frozen files are removed from the project.",
    ok: "Remove", okClass: "btn danger" }))) return;
  refAction(name, `/style-refs/${index}`, "DELETE", `Removing style ref ${index}…`);
}
async function planChat(name, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/projects/${encodeURIComponent(name)}/plan-chat`, { method: "POST", headers: { "X-Hands": "1" } });
    location.hash = `#/sessions/${encodeURIComponent(r.session_id)}`;
  } catch (e) { toast(`Planning chat refused: ${e.message}`, true); }
  if (btn) btn.disabled = false;
}
// Resets. Both taps are POSTs with the same Origin/X-Hands checks as every other action; the chat reset is
// immediate, the project reset runs detached (its progress is the Creation panel's log).
async function planChatReset(name, btn) {
  if (!(await confirmDialog({ title: `Reset the planning chat of ${name}?`,
    warn: "The conversation and the draft brief (brief.md and coverage.md) are deleted.",
    body: "A brief you already approved stays in the spec repo, and so do the style refs and the setup. The next chat starts from the beginning.",
    ok: "Reset planning chat", okClass: "btn danger" }))) return;
  if (btn) btn.disabled = true;
  try {
    await api(`/api/projects/${encodeURIComponent(name)}/plan-chat/reset`, { method: "POST", headers: { "X-Hands": "1" } });
    toast("Planning chat reset.");
  } catch (e) { toast(`Reset refused: ${e.message}`, true); }
  if (btn) btn.disabled = false;
  refresh(false);
}
async function projectReset(name, btn) {
  const r = await confirmDialog({ title: `Reset ${name}?`,
    warn: "The GitHub repos, the deploy keys, the plan, the brief and the whole build history are deleted; the project is then created again from its saved settings.",
    body: "Description, framework, libraries and style ref URLs are replayed. The planner is not started. The project's variables are kept unless you tick the box.",
    requireText: name, checkbox: "Also wipe the environment variables", ok: "Reset project", okClass: "btn danger" });
  if (!r) return;
  if (btn) btn.disabled = true;
  try {
    await api(`/api/projects/${encodeURIComponent(name)}/reset`, { method: "POST",
      headers: { "X-Hands": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: name, wipe_env: !!r.checked }) });
    toast(`Resetting ${name}: destroy, then create again.`);
    // the project disappears from the tab strip while it is destroyed: follow the log instead, as a create does
    mode = "creating"; creatingSlug = name; creatingChat = false; creatingChatStarted = false;
    creatingEnvError = ""; creatingReset = true;
  } catch (e) { toast(`Reset refused: ${e.message}`, true); }
  if (btn) btn.disabled = false;
  refresh(false);
}
function resetSection(d) {
  if (!d.managed || d.imported || d.is_self) return null;
  return el("section", { "data-key": "reset" },
    el("p", { class: "muted" }, "Start this project over: its GitHub repos, deploy keys, clones, plan and build history "
      + "are deleted, then the project is created again from the same description, framework, libraries and style ref URLs."),
    el("div", { class: "row" }, el("button", { class: "btn danger", type: "button", "data-act": "project-reset",
      "data-arg": d.name, "data-testid": "project-reset" }, "Reset project")));
}
function planChatHint(ps) {
  const s = hands.nextform.planStatus(ps);
  return s === "none" ? "" : s === "brief_ready" ? "brief ready" : s;
}
// Hash-pinned approval: the button carries the hash the page shows; the server refuses any other brief.
async function approveBrief(name, sha, btn) {
  const short = hands.nextform.shortSha(sha);
  if (!short) { toast("No brief hash to approve; refresh the page.", true); return; }
  if (!(await confirmDialog({ title: `Approve brief ${short} for ${name}?`,
    body: "Copies exactly this brief and its coverage table into the spec repo. Planning starts only when you press Start.",
    ok: "Approve brief", okClass: "btn primary" }))) return;
  if (btn) btn.disabled = true;
  try {
    await api(`/api/projects/${encodeURIComponent(name)}/plan-chat/approve`, { method: "POST",
      headers: { "X-Hands": "1", "Content-Type": "application/json" }, body: JSON.stringify({ sha256: sha }) });
    toast(`Brief ${short} approved.`);
  } catch (e) { toast(`Approval refused: ${e.message}`, true); }
  if (btn) btn.disabled = false;
  refresh(false);
}

const PHASES = { coder: "coding", check: "checking changes", acceptance: "running checks", evaluator: "independent review" };
// The overview strip (0.6): what is happening now, how far the build is, and the runner actions; sticky
// under the tab strip so it stays in view while the panels below scroll.
function statusSection(d) {
  const btn = (act, label, enabled) =>
    el("button", { class: "act", "data-act": "run", "data-arg": act, "data-label": label, ...(enabled ? {} : { disabled: "" }) }, label);
  const total = d.steps.length, pct = total ? Math.round(100 * d.passed / total) : 0;
  const fill = el("span", { class: `fill ${d.status === "escalation" ? "bad" : d.status === "done" ? "ok" : ""}` });
  fill.style.width = `${pct}%`;                                  // CSSOM, allowed by the CSP (style attributes are not)
  const cur = d.current
    ? el("span", {}, el("span", { class: "sid" }, d.current.step), ` attempt ${d.current.attempt} · ${PHASES[d.current.phase] || d.current.phase}`,
        d.alive ? "" : " (stopped)")
    : el("span", { class: "muted" }, d.status === "done" ? "all steps passed" : "nothing running");
  return el("section", { class: "overview", "data-key": "status" },
    el("div", { class: "row" }, el("h2", {}, d.name), badge(d.status),
      el("span", { class: `dot ${d.alive ? "live" : ""}`, "aria-hidden": "true" }),
      el("span", { class: "muted" }, d.alive ? "runner alive" : "runner not running"),
      d.stop ? el("span", { class: "badge s-stopped" }, "STOP requested") : null,
      el("span", { class: "grow" }),
      el("span", { class: "muted small" }, `${d.passed}/${total} steps`)),
    el("span", { class: "bar", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": "steps passed" }, fill),
    el("div", { class: "now-line" }, d.alive && d.current ? el("span", { class: "spinner sm", "aria-label": "running" }) : null,
      el("span", {}, "Now: ", cur),
      d.alive && d.activity && d.activity.text ? el("span", { class: "act" }, d.activity.text) : null),
    d.reason ? el("p", { class: d.status === "escalation" ? "reason" : "muted" }, d.reason) : null,
    el("p", { class: "muted small" },
      `${d.passed}/${d.steps.length} steps passed · ${d.total_sessions} sessions · ~$${d.total_cost_usd} API-equivalent · ` +
      `coder ${d.model_override || d.config.coder_model} · eval ${d.model_override || d.config.eval_model} · ${d.config.image}`),
    roundLine(d),
    d.model_override ? el("p", { class: "reason" }, `Model override active: ${d.model_override}`) : null,
    d.spec_error ? (d.setup && !d.setup.spec_ready
      ? el("p", { class: "muted" }, "No plan yet: the spec repo has no steps.json.")
      : el("p", { class: "reason" }, `Spec: ${d.spec_error}`)) : null,
    el("div", { class: "row" },
      btn("start", "Start", !d.alive && d.status !== "escalation" && !d.spec_error),
      btn("stop", "Stop at next safe point", d.alive && !d.stop),
      btn("ack", "Acknowledge escalation", !d.alive && d.status === "escalation"),
      btn("ack-clear-model", "Ack + clear model override", !d.alive && !!d.model_override)),
    keep(actionOut, "action-out"));
}

function stepsSection(d) {
  return el("section", { "data-key": "steps" }, el("h2", {}, "Steps"),
    el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Step", "Title", "Status", "Att.", "Commit"].map((h) => el("th", {}, h)))),
      el("tbody", {}, d.steps.map((s) => [
        el("tr", { "data-key": `step:${s.id}` }, el("td", {}, s.id), el("td", {}, s.title, s.type === "human" ? " (human)" : ""),
          el("td", {}, badge(s.status)), el("td", { class: "num" }, s.attempts), el("td", {}, (s.commit || "").slice(0, 9))),
        s.status === "failed" && s.reasons.length
          ? el("tr", { "data-key": `step:${s.id}:why` }, el("td", {}), el("td", { colspan: "4", class: "reason" }, s.reasons.slice(0, 4).join("\n"))) : null,
      ])))));
}

function metricsSection(d) {
  const rows = d.metrics.slice().reverse();
  return el("section", { "data-key": "metrics" }, el("h2", {}, "Sessions"),
    el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Time", "Role", "Step", "Result", "Turns", "$", "Dur"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((m) => el("tr", { "data-key": `metric:${m.ts}:${m.role}:${m.step}#${m.attempt}` },
        el("td", {}, localTs(m.ts)), el("td", {}, m.role), el("td", {}, `${m.step}#${m.attempt}`),
        el("td", {}, m.kind), el("td", { class: "num" }, m.turns), el("td", { class: "num" }, Number(m.cost_usd || 0).toFixed(2)),
        el("td", { class: "num" }, `${Math.round((m.duration_s || 0) / 60)}m`)))))));
}

function progressSection(d) {
  return el("section", { "data-key": "progress" }, el("h2", {}, "Progress notes"),
    el("div", { class: "scroll" }, d.progress.slice().reverse().map((p) =>
      el("div", { class: "attempt", "data-key": `note:${p.step}:${p.ts}` }, el("strong", {}, `${p.step} ${p.title || ""}`), " ",
        el("span", { class: "muted" }, `${(p.commit || "").slice(0, 9)} · ${localTs(p.ts)}`),
        el("div", {}, p.summary || ""),
        (p.risks || []).length ? el("div", { class: "muted" }, "Risks: " + p.risks.join("; ")) : null))));
}

async function showFile(name, att, f, target) {
  target.textContent = "Loading…";
  try { target.textContent = await api(`/api/p/${encodeURIComponent(name)}/attempts/${encodeURIComponent(att)}/${encodeURIComponent(f)}`); }
  catch (e) { target.textContent = `failed: ${e.message}`; }
}

const viewer = el("pre", { "data-self-managed": "" }, "Pick an attempt file.");
const viewerTitle = el("h2", { "data-self-managed": "" }, "File");

let attemptsCache = { name: null, rows: [] };      // the last attempts list, rendered synchronously
function attemptsSection(name, rows) {
  return el("section", { "data-key": "attempts" }, el("h2", {}, "Attempts"),
    el("div", { class: "scroll" }, rows.map((a) => el("div", { class: "attempt", "data-key": `attempt:${a.name}` },
      el("div", { class: "row" }, el("strong", {}, a.name), el("span", { class: "muted" }, when(a.mtime)),
        a.verdict_pass === true ? badge("passed") : a.verdict_pass === false ? badge("failed") : null),
      a.reason ? el("div", { class: "reason" }, a.reason) : null,
      hands.nextform.toolUsage(document, a.tool_usage),
      el("div", { class: "files" }, a.files.map((f) =>
        el("button", { "data-act": "attempt-file", "data-att": a.name, "data-file": f.name }, f.name)))))));
}

// ---- environment variables editor (step 07). Used by the New project form (rows kept locally until
// create has started), the creating page and the project page (pending/project API). Secret values are
// write-only: the server never returns them and the inputs are cleared after every save.
const ENV_KEY = /^[A-Z_][A-Z0-9_]*$/;
const ENV_RESERVED = new Set(["CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "TG_TOKEN", "TG_CHAT_ID", "PATH",
  "HOME", "USER", "SHELL", "CI", "NODE_OPTIONS"]);
const ENV_HINT = "Secret: saved write-only, shown as •••• from now on, seen only by the build containers. " +
  "Public: shown here and may end up in the built app's browser bundle (e.g. a public API URL). Values are passed as typed: no quotes needed.";
function envKeyError(k) {
  if (!k) return "Key required";
  if (k.length > 128) return "Key too long (max 128)";
  if (!ENV_KEY.test(k)) return "UPPER_CASE letters, digits and _ only; cannot start with a digit";
  if (ENV_RESERVED.has(k) || k.startsWith("HANDS_") || k.startsWith("CLAUDE_")) return `${k} is reserved`;
  return "";
}
function envValueError(v) {
  if (/[\r\n\0]/.test(v)) return "One line, no control characters";
  if (new TextEncoder().encode(v).length > 8192) return "Value too long (max 8 KB)";
  return "";
}
function parseEnvBlock(text) {
  const out = [];
  for (let line of String(text || "").split(/\r?\n/)) {
    line = line.replace(/^\s+/, "");
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice(7).trimStart();
    const i = line.indexOf("=");
    if (i <= 0) continue;
    out.push({ key: line.slice(0, i).trim(), value: line.slice(i + 1) });
  }
  return out;
}

function envEditor(opts) {
  // opts.name: project (or creating slug) to load/save through the API; opts.local: no server, rows kept here
  const rows = [];
  let info = null, msg = null, loaded = !!opts.local;
  const root = el("div", { class: "envedit" });
  const newRow = (r) => ({ key: "", value: "", secret: true, existing: false, removed: false, err: {}, ...r });
  const fromServer = (v) => newRow({ key: v.key, value: v.secret ? "" : v.value, orig: v.secret ? "" : v.value,
                                     secret: v.secret, existing: true, wasSecret: v.secret });

  async function load() {
    try {
      const d = await api(`/api/p/${encodeURIComponent(opts.name)}/env`);
      rows.length = 0;
      for (const v of d.vars) rows.push(fromServer(v));
      info = d;
      loaded = true;
    } catch (e) { msg = { bad: true, text: `could not load variables: ${e.message}` }; }
    render();
  }

  function validate() {
    const set = [], del = [], seen = new Set();
    let ok = true;
    for (const r of rows) {
      r.err = {};
      if (r.removed) { del.push(r.key); continue; }
      if (!r.existing && !r.key && !r.value) continue;                          // blank row: ignore
      const ke = envKeyError(r.key);
      if (ke) r.err.key = ke;
      else if (seen.has(r.key)) r.err.key = `${r.key} is listed twice`;
      seen.add(r.key);
      const ve = envValueError(r.value);
      if (ve) r.err.value = ve;
      else if (!r.existing && !r.value && r.secret) r.err.value = "Value required";
      else if (r.existing && r.wasSecret && !r.secret && !r.value) r.err.value = "Enter the value again to make it public";
      if (r.err.key || r.err.value) { ok = false; continue; }
      if (!r.existing) set.push({ key: r.key, value: r.value, public: !r.secret });
      else {
        const valueChanged = r.wasSecret ? !!r.value : r.value !== r.orig;   // public rows are pre-filled
        if (valueChanged || r.secret !== r.wasSecret) {
          const it = { key: r.key, public: !r.secret };
          if (valueChanged) it.value = r.value;
          set.push(it);
        }
      }
    }
    return ok ? { set, delete: del } : null;
  }

  async function save() {
    const req = validate();
    if (!req) { msg = { bad: true, text: "Fix the highlighted rows." }; render(); return; }
    if (!req.set.length && !req.delete.length) { msg = { text: "Nothing to save." }; render(); return; }
    const body = {};
    if (req.set.length) body.set = req.set;
    if (req.delete.length) body.delete = req.delete;
    try {
      const d = await api(`/api/p/${encodeURIComponent(opts.name)}/env`, { method: "POST",
        headers: { "X-Hands": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) });
      rows.length = 0;
      for (const v of d.vars) rows.push(fromServer(v));
      info = d;
      msg = { ok: true, text: `Saved: ${req.set.length} set, ${req.delete.length} deleted.` };
    } catch (e) { msg = { bad: true, text: `not saved: ${e.message}` }; }
    render();
  }

  function rowEl(r) {
    const keyIn = el("input", { class: "input mono", type: "text", value: r.key, placeholder: "KEY", spellcheck: "false",
      autocomplete: "off", "aria-label": "Key", maxlength: "128", ...(r.existing ? { readonly: "" } : {}),
      ...(r.err.key ? { "aria-invalid": "true" } : {}) });
    keyIn.addEventListener("input", () => { r.key = keyIn.value.trim(); });
    keyIn.addEventListener("paste", (ev) => {
      const text = (ev.clipboardData || window.clipboardData || {}).getData ? (ev.clipboardData || window.clipboardData).getData("text") : "";
      const parsed = parseEnvBlock(text);
      if (!parsed.length || !text.includes("=")) return;              // plain key: let the browser paste it
      ev.preventDefault();
      if (r.existing) { for (const p of parsed) rows.push(newRow(p)); }
      else { Object.assign(r, parsed[0]); for (const p of parsed.slice(1)) rows.splice(rows.indexOf(r) + 1, 0, newRow(p)); }
      msg = { text: `Pasted ${parsed.length} variable(s), all Secret. Save to keep them.` };
      render();
    });
    const valIn = el("input", { class: "input", type: r.secret ? "password" : "text", value: r.value, "aria-label": `Value of ${r.key || "new variable"}`,
      placeholder: r.existing && r.wasSecret ? "•••••••• (unchanged; type to replace)" : "value", autocomplete: "off", spellcheck: "false",
      ...(r.err.value ? { "aria-invalid": "true" } : {}) });
    valIn.addEventListener("input", () => { r.value = valIn.value; });
    const seg = el("div", { class: "seg", role: "group", "aria-label": `Visibility of ${r.key || "new variable"}` },
      el("button", { type: "button", "aria-pressed": String(r.secret), on: { click: () => { r.secret = true; valIn.type = "password"; seg.children[0].setAttribute("aria-pressed", "true"); seg.children[1].setAttribute("aria-pressed", "false"); } } }, "Secret"),
      el("button", { type: "button", "aria-pressed": String(!r.secret), on: { click: () => { r.secret = false; valIn.type = "text"; seg.children[0].setAttribute("aria-pressed", "false"); seg.children[1].setAttribute("aria-pressed", "true"); } } }, "Public"));
    const rm = el("button", { class: "btn quiet sm", type: "button", "aria-label": `Remove ${r.key || "row"}`, title: "Remove",
      on: { click: () => { if (r.existing) r.removed = true; else rows.splice(rows.indexOf(r), 1); render(); } } }, "×");
    if (r.removed) {
      return el("div", { class: "envrow removed" }, el("span", { class: "mono" }, r.key), el("span", { class: "muted" }, "will be deleted on Save"),
        el("button", { class: "btn quiet sm", type: "button", on: { click: () => { r.removed = false; render(); } } }, "Undo"));
    }
    return el("div", { class: "envrow" }, keyIn, valIn, seg, rm,
      r.err.key ? el("div", { class: "error ek" }, r.err.key) : null, r.err.value ? el("div", { class: "error ev" }, r.err.value) : null);
  }

  function render() {
    const live = rows.filter((r) => !r.removed).length;
    root.replaceChildren(...[
      info && info.problems && info.problems.length ? el("div", { class: "notice warn" },
        el("strong", {}, "Some lines in project.env cannot be managed here:"), el("ul", {}, info.problems.map((p) => el("li", {}, p)))) : null,
      info && !info.active && !info.pending ? el("p", { class: "muted small" },
        "This project's config has no project_env, so the runner does not pass these variables to containers yet.") : null,
      info && info.pending ? el("p", { class: "muted small" }, "Saved while the project is being created; moved into place when it finishes.") : null,
      !loaded ? el("p", { class: "muted" }, "Loading…") :
        el("div", { class: "envrows" }, rows.length ? rows.map(rowEl) : el("p", { class: "muted" }, "No variables yet.")),
      el("div", { class: "row" },
        el("button", { class: "btn sm", type: "button", on: { click: () => {
          rows.push(newRow()); msg = null; render();
          const last = [...root.querySelectorAll(".envrow:not(.removed) .input")].pop(); // value input of the new row
          const keyOfLast = last && last.previousElementSibling; if (keyOfLast) keyOfLast.focus(); } } }, "+ Add variable"),
        opts.local ? null : el("button", { class: "btn primary sm", type: "button", on: { click: save } }, live || rows.some((r) => r.removed) ? "Save variables" : "Save"),
        opts.local ? null : el("button", { class: "btn quiet sm", type: "button", title: "Discard unsaved changes and reload", on: { click: () => { msg = null; load(); } } }, "Reload"),
        msg ? el("span", { class: msg.bad ? "reason" : msg.ok ? "s-ok" : "muted", role: "status" }, msg.text) : null),
      el("p", { class: "muted small hint" }, ENV_HINT, " Paste a block of KEY=value lines into a Key field to add several rows at once."),
    ].filter(Boolean));
  }

  root.collect = () => {           // local mode: the New project form calls this before creating
    const req = validate();
    render();
    if (!req) throw new Error("fix the environment variables first");
    return req.set;
  };
  root.reload = load;
  if (!opts.local) load(); else render();
  return root;
}

const envPanels = {};               // one editor per project, kept across the 4 s refresh so edits survive
function envPanel(key, name) {
  if (!envPanels[key]) envPanels[key] = envEditor({ name });
  return envPanels[key];
}
function envSection(name) {
  return el("section", { class: "envsec", "data-key": "env" }, el("h2", {}, "Environment"),
    el("p", { class: "muted" }, "Variables for this project's build containers (coder, acceptance tests, evaluator): test API keys, service URLs. Stored under ~/.config/hands/secrets/, never in the repos."),
    envPanel(name, name));
}

function showNew() {
  mode = "new";
  loadProjects().catch(() => {});
  const slug = el("input", { id: "slug", autocomplete: "off", spellcheck: "false", maxlength: "32",
    pattern: "[a-z][a-z0-9\\-]{1,30}[a-z0-9]", placeholder: "e.g. bookings", required: "" });   // "-" escaped: pattern uses the v flag
  const desc = el("textarea", { id: "desc", rows: "5", maxlength: "4000", required: "",
    placeholder: "One or two sentences: what the app is, who it is for. The planner asks the rest." });
  const out = el("pre", {}, "");
  const submit = el("button", { class: "act", type: "submit" }, "Create project");
  const withClaude = el("button", { class: "btn primary", type: "submit", value: "chat", "data-testid": "plan-with-claude" },
    hands.nextform.CREATE_PLAN_LABEL);
  const styleUrls = hands.nextform.styleUrlList(document);
  const setupFields = hands.nextform.setupFields(document);
  const envLocal = envEditor({ local: true });
  // Start from an existing repo (step 15): the app repo already exists; hands only adds a deploy key to it.
  const fromRepo = el("input", { type: "checkbox", id: "fromrepo" });
  const repo = el("input", { id: "repo", autocomplete: "off", spellcheck: "false", maxlength: "140",
    placeholder: "owner/repo", pattern: "[A-Za-z0-9][A-Za-z0-9\\-]{0,38}/[A-Za-z0-9._\\-]{1,100}" });
  const branch = el("input", { id: "branch", autocomplete: "off", spellcheck: "false", maxlength: "120",
    placeholder: "default branch", pattern: "[A-Za-z0-9][A-Za-z0-9._\\/\\-]{0,119}" });   // "/" and "-" escaped: v flag
  const ruleset = el("input", { type: "checkbox", id: "ruleset" });
  const repoFields = el("div", { class: "subfields hidden" },
    el("label", { for: "repo" }, "Existing repo"), repo,
    el("p", { class: "muted" }, "owner/repo on GitHub. The token in github.env needs admin access on it. The repo is never deleted or re-created; Phoenix adds a write deploy key and clones it."),
    el("label", { for: "branch" }, "Branch (optional)"), branch,
    el("p", { class: "muted" }, "The runner builds on this branch. Leave empty for the repo's default branch."),
    el("label", { class: "check" }, ruleset, el("span", {}, "Add Phoenix's branch protection to that repo",
      el("span", { class: "hint" }, "Ruleset protect-main: no deletion, no force-push. Leave off if the repo has its own rules."))));
  const syncRepoFields = () => {
    repoFields.classList.toggle("hidden", !fromRepo.checked);
    repo.required = fromRepo.checked;
    if (!fromRepo.checked) { repo.value = ""; branch.value = ""; ruleset.checked = false; }
  };
  fromRepo.addEventListener("change", syncRepoFields);
  const form = el("form", { on: { submit: async (ev) => {
    ev.preventDefault();
    let vars;
    try { vars = envLocal.collect(); } catch (e) { out.textContent = e.message; return; }
    if (!styleUrls.validate()) { out.textContent = "Fix the style URLs marked above (or clear them)."; return; }
    const chat = !!(ev.submitter && ev.submitter.value === "chat");
    const importing = fromRepo.checked;
    const q = importing
      ? `Create the GitHub repo ${slug.value}-specs, add a deploy key to ${repo.value} and set everything up? ${repo.value} itself is not changed.`
      : `Create GitHub repos ${slug.value}-specs and ${slug.value}-app and set everything up?`;
    if (!(await confirmDialog({ title: importing ? `Set up ${slug.value} from ${repo.value}?` : `Create ${slug.value}?`, body: q, ok: "Create", okClass: "btn primary" }))) return;
    submit.disabled = true; withClaude.disabled = true;
    out.textContent = "Starting…";
    const body = { slug: slug.value, description: desc.value, style_refs: styleUrls.values(),
      framework: setupFields.framework(), libs: setupFields.libs(), plan_with_claude: chat };
    if (importing) { body.from_repo = repo.value.trim(); body.branch = branch.value.trim(); body.ruleset = ruleset.checked; }
    try {
      await api("/api/new", { method: "POST", headers: { "X-Hands": "1", "Content-Type": "application/json" },
        body: JSON.stringify(body) });
    } catch (e) { out.textContent = `failed: ${e.message}`; submit.disabled = false; withClaude.disabled = false; return; }
    creatingEnvError = "";
    if (vars.length) {
      try {
        await api(`/api/p/${encodeURIComponent(slug.value)}/env`, { method: "POST",
          headers: { "X-Hands": "1", "Content-Type": "application/json" }, body: JSON.stringify({ set: vars }) });
      } catch (e) { creatingEnvError = `The project is being created, but its variables were not saved (${e.message}). Add them below.`; }
    }
    mode = "creating"; creatingSlug = slug.value; creatingChat = chat; creatingChatStarted = false;
    creatingReset = false; refresh(false);
  } } },
    el("label", { for: "slug" }, "Project slug"), slug,
    el("p", { class: "muted" }, "Lowercase letters, digits and hyphens. Becomes the repo names <slug>-specs and <slug>-app (or just <slug>-specs when starting from an existing repo)."),
    el("label", { class: "check" }, fromRepo, el("span", {}, "Start from an existing repo",
      el("span", { class: "hint" }, "Build on a codebase that already exists on GitHub instead of a new, empty app repo."))),
    repoFields,
    el("label", { for: "desc" }, "Idea"), desc,
    el("div", { class: "flabel" }, "Style references (optional)"),
    el("p", { class: "muted" }, `Sites whose look and motion the app should follow, up to ${hands.nextform.MAX_STYLE_URLS} http(s) URLs. Each is captured and analysed before planning; text, images and logos are never copied.`),
    styleUrls.root,
    setupFields.root,
    el("label", {}, "Environment variables (optional)"),
    el("p", { class: "muted" }, "Test API keys and service URLs the build containers need. You can also add them later on the project page."),
    envLocal,
    el("div", { class: "row" }, submit, withClaude),
    el("p", { class: "muted" }, `Create project runs the planner right away. ${hands.nextform.CREATE_PLAN_LABEL}: `
      + `${hands.nextform.CREATE_PLAN_HINT}. Claude interviews you there and writes the brief you approve.`),
    out);
  $("#main").replaceChildren(el("section", {}, el("h2", {}, "New project"),
    el("p", { class: "muted" }, "Creates two private repos with branch protection, read-only/write deploy keys, " +
      "clones under ~/projects/<slug>/ and the runner config. Takes about a minute. Nothing is built until a plan is merged. " +
      "With an existing repo, only the spec repo is created."),
    form));
}

let creatingEnvError = "";
let creatingChat = false;     // created with "Create and plan with Claude": open the planning chat once it exists
let creatingChatStarted = false;   // the POST went out for this create; never a second one from the poller
async function refreshCreating() {
  const slug = creatingSlug;
  try {
    const c = await api(`/api/creating/${encodeURIComponent(slug)}`);
    if (mode !== "creating" || slug !== creatingSlug) return;
    const done = c.state === "done" && c.project_exists;
    $("#main").replaceChildren(...[el("section", {},
      el("div", { class: "row" }, el("h2", {}, `${creatingReset ? "Resetting" : "Creating"} ${slug}`),
        badge(c.state === "failed" ? "escalation" : c.state)),
      c.error ? el("p", { class: "reason" }, c.error) : null,
      el("pre", {}, c.lines.join("\n")),
      done ? el("div", { class: "row out" }, el("button", { class: "act", "data-act": "select", "data-arg": slug }, `Open ${slug}`)) : null,
      c.state === "failed" ? el("p", { class: "muted" }, creatingReset
        ? `The log above says what exists. Fix the cause, then on the VPS: hands ${slug} reset --confirm ${slug}`
        : c.project_exists
          ? `Fix the cause, then on the VPS: hands ${slug} destroy --confirm ${slug}   and create it again.`
          : "Nothing was created. Fix the cause and create it again.") : null),
      c.state === "running" ? el("section", { class: "envsec" }, el("h2", {}, "Environment"),
        creatingEnvError ? el("div", { class: "notice bad" }, creatingEnvError) : null,
        envPanel(`creating:${slug}`, slug)) : null].filter(Boolean));
    // "Create and plan with Claude": open the chat as soon as the project exists. Once per create, whatever the
    // poller does; the request is idempotent server-side. There is no second button here: the project page's
    // "Plan with Claude" panel is the one place that opens or resumes the chat.
    if (hands.nextform.shouldOpenPlanChat({ chat: creatingChat, done, started: creatingChatStarted })) {
      creatingChatStarted = true;
      await planChat(slug);
    }
  } catch (e) { $("#clock").textContent = `update failed: ${e.message}`; }
}

// ---- change rounds (0.5.8, B3): one card per round, the sha-pinned Merge tap and Discard
function roundLine(d) {
  const r = d.round && d.round.record, m = d.round && d.round.merge;
  if (!r || !m) return null;
  const st = roundState({ status: r.status, passed: m.passed, total: m.total, merge_ready: m.ready, self: m.self });
  let text;
  if (r.status === "merged") text = `merged into ${d.round.branch} at ${(r.merged_sha || "").slice(0, 9)}`;
  else if (r.status === "merging") text = "merging…";
  else if (r.status === "discarding") text = "discarding…";
  else text = `${m.passed}/${m.total} steps passed · ${m.ready ? "ready to merge" : st === "all passed" ? m.why : "building"}`;
  return el("p", { class: "muted roundline" }, el("span", { class: `rtag r-${st.replace(/ /g, "_")}` }, `C${r.id}`),
    ` Change round C${r.id} on ${r.branch} (base ${(r.base_sha || "").slice(0, 9)}) · ${text}`);
}

async function roundAction(d, act, m) {
  const name = d.name, r = d.round.record;
  const steps = d.steps.filter((s) => s.round === r.id).map((s) => s.id);
  const short = (x) => (x || "").slice(0, 9);
  let ok;
  if (act === "merge") {
    ok = await confirmDialog({ title: `Merge change C${r.id} into ${d.round.branch}?`,
      body: `${r.branch} at ${short(m.branch_sha)} (${steps.length} step${steps.length === 1 ? "" : "s"}: ${steps.join(", ")}) is squashed onto ` +
        `${d.round.branch} at ${short(m.main_sha)} and pushed. Refused if either head moved since this page showed them. ` +
        `Rollback by hand: tag ${r.pre_tag} = before the change, hands/merged/C${r.id} = the unsquashed work.`,
      ok: `Merge change C${r.id}`, okClass: "btn primary" });
  } else {
    ok = await confirmDialog({ title: `Discard the build of round C${r.id}?`,
      warn: `The work on ${r.branch} (${short(m.branch_sha)}) is thrown away (kept as tag hands/discarded/C${r.id}-…); ${d.round.branch} is not touched. ` +
        `The amendment's steps (${steps.join(", ")}) stay in the spec and go back to pending: the next Start rebuilds them in a fresh round C${r.id}. ` +
        "Discarding the work is not discarding the change request; that needs a spec PR.",
      body: `Project ${name}.`, ok: `Discard round C${r.id}`, okClass: "btn danger" });
  }
  if (!ok) return;
  try {
    await api(`/api/p/${encodeURIComponent(name)}/action/${act}`, { method: "POST",
      headers: { "X-Hands": "1", "Content-Type": "application/json" },
      body: JSON.stringify(act === "merge" ? { branch_sha: m.branch_sha, main_sha: m.main_sha } : { branch_sha: m.branch_sha }) });
    toast(act === "merge" ? `Merging change C${r.id}…` : `Discarding round C${r.id}…`);
  } catch (e) { toast(`${act === "merge" ? "Merge" : "Discard"} refused: ${e.message}`, true); }
  refresh(false);
}

function roundsSection(d) {
  const R = d.round || {}, rec = R.record, m = R.merge || {}, hist = R.history || [];
  if (!rec && !hist.length) return null;
  const short = (x) => (x || "").slice(0, 9);
  const when = (ts) => ts ? ts.replace("T", " ").replace("Z", " UTC") : "";
  const btn = (label, act, enabled, cls = "act") =>
    el("button", { class: cls, "data-act": "round", "data-arg": act, ...(enabled ? {} : { disabled: "" }) }, label);
  const stepList = (id) => d.steps.filter((s) => s.round === id);
  const icon = (s) => el("span", { class: s.status === "passed" ? "ic ok" : s.status === "failed" ? "ic bad" : "ic" },
    s.status === "passed" ? "✓" : s.status === "failed" ? "✗" : s.status === "discarded" ? "–" : "○");
  // every round: the live record first, then earlier rounds from the history (latest first)
  // one card per opening (id + opened_at): a discarded C1 keeps its card next to the rebuilt C1
  const byKey = new Map();
  const keyOf = (id, opened) => `${id}@${opened || ""}`;
  for (const ev of hist) {
    const k = keyOf(ev.id, ev.opened_at);
    const x = byKey.get(k) || { id: ev.id, opened_at: ev.opened_at || "", events: [] };
    x.events.push(ev);
    for (const f of ["request", "pr", "pr_url", "branch", "base_sha", "pre_tag"]) if (ev[f] !== undefined) x[f] = ev[f];
    byKey.set(k, x);
  }
  if (rec) { const k = keyOf(rec.id, rec.opened_at); byKey.set(k, { ...(byKey.get(k) || { events: [] }), ...rec, live: true }); }
  const cards = [...byKey.values()].sort((a, b) => b.id - a.id || (b.opened_at > a.opened_at ? 1 : -1)).map((x) => {
    const last = x.events.length ? x.events[x.events.length - 1] : null;
    const live = x.live && rec;
    let state, cls = "";
    if (live && rec.status === "merged") { state = `merged into ${R.branch} ${short(rec.merged_sha)} at ${when(rec.merged_at)}`; cls = "r-merged"; }
    else if (live && rec.status === "merging") { state = "merging…"; cls = "r-merging"; }
    else if (live && rec.status === "discarding") { state = "discarding…"; cls = "r-discarding"; }
    else if (live) { state = m.all_passed ? `all ${m.total} steps passed: ${m.ready ? "ready to merge" : m.why}` : `open on ${rec.branch} · ${m.passed}/${m.total} steps passed`; cls = m.all_passed ? "r-all_passed" : "r-open"; }
    else if (last && last.event === "merged") { state = `merged into ${R.branch} ${short(last.merged_sha)} at ${when(last.ts)}`; cls = "r-merged"; }
    else if (last && last.event === "discarded") { state = `discarded at ${when(last.ts)} (work kept at ${last.discarded_tag || "a hands/discarded tag"})`; cls = "r-discarded"; }
    else state = last ? last.event : "";
    const steps = live || (last && last.event === "merged") ? stepList(x.id) : (last && last.steps ? last.steps.map((id) => ({ id, status: "discarded" })) : []);
    const taps = live && rec.status === "open" ? el("div", { class: "row" },
      m.self ? el("span", { class: "muted" }, "Merged by Install update (Phoenix updates itself).")
        : btn(`Merge change C${x.id} (${short(m.branch_sha)} → ${R.branch} ${short(m.main_sha)})`, "merge", m.ready, "btn primary"),
      btn(`Discard round C${x.id}`, "discard", !d.alive && !!m.branch_sha, "btn danger"),
      !m.ready && !m.self && m.why ? el("span", { class: "muted" }, m.why) : null) : null;
    return el("div", { class: `round ${cls}`, "data-key": `round:${x.id}@${x.opened_at || ""}` },
      el("div", { class: "row" }, el("span", { class: `rtag ${cls}` }, `C${x.id}`), el("strong", {}, x.request ? x.request.split("\n")[0] : `Change round C${x.id}`),
        x.pr_url ? el("a", { href: x.pr_url, target: "_blank", rel: "noopener" }, `PR #${x.pr}`) : x.pr ? el("span", { class: "muted" }, `PR #${x.pr}`) : null),
      el("p", { class: "muted" }, state, x.base_sha ? ` · base ${short(x.base_sha)} (tag ${x.pre_tag || ""})` : ""),
      steps.length ? el("ul", { class: "rsteps" }, steps.map((s) => el("li", {}, icon(s), " ", el("span", { class: "sid" }, s.id), " ", s.title || ""))) : null,
      live && rec.error ? el("p", { class: "reason" }, rec.error) : null,
      taps);
  });
  const amendHint = el("p", { class: "muted" }, "Change something else: ", el("a", { href: "#", "data-act": "focus-amend" }, "Amend plan"),
    " (the planner appends a new round; passed steps are never rebuilt).");
  return el("section", { class: "rounds", "data-key": "rounds" }, el("h2", {}, "Change rounds"), ...cards,
    R.log ? el("details", {}, el("summary", {}, "Merge / discard log"), el("pre", { class: "log-small" }, R.log)) : null,
    d.managed ? amendHint : null);
}

function setupSection(d) {
  const s = d.setup || {};
  const item = (ok, text, hint) => el("li", {}, el("span", { class: ok ? "s-passed" : "s-pending" }, ok ? "✓ " : "○ "), text,
    hint ? el("div", { class: "muted" }, hint) : null);
  return el("section", { "data-key": "setup" }, el("h2", {}, "Setup"),
    s.description ? el("p", {}, s.description) : null,
    s.repo_specs ? el("p", { class: "muted" }, `Repos: ${s.repo_specs} · ${s.repo_app}${s.imported ? " (existing repo, imported)" : ""}`) : null,
    el("ul", { class: "checklist" },
      item(s.spec_ready, "Plan approved (steps.json merged into the spec repo)",
        s.spec_ready ? null : "See the Plan section: review the plan, then Approve & build."),
      item(s.project_env_keys > 0, `Test keys in project.env (${s.project_env_keys} set)`,
        `Only if the app needs outside services (Supabase, Stripe test keys…). File on the VPS: ${s.project_env || "(none)"}`),
      item(s.allowed_domains > 0, `Extra network domains (${s.allowed_domains} in allowed-domains)`,
        "Only if those services must be reachable from the build containers: one hostname per line in the spec repo's allowed-domains, via PR."),
      item(d.alive || d.passed > 0 || ["running", "paused_rate_limit", "done"].includes(d.status), "Build started",
        s.spec_ready && !d.alive && d.passed === 0 ? "Press Start (or Approve & build in the Plan section)." : null)));
}

let planFeedback = "";
function phaseOf(f) {
  if (f === "docs/design.md") return "Design (brainstorming)";
  if (f === "docs/plan.md") return "Task plan (writing-plans)";
  if (f === "docs/conventions.md") return "Conventions";
  if (f.startsWith("acceptance/")) return "Acceptance tests";
  if (f === "steps.json") return "Machine spec";
  if (f === "PLAN.md") return "Review summary";
  return "";
}

function stepBar(d) {
  const cur = d.current && d.current.step;
  const phaseLabel = { coder: "coding", check: "checking changes", acceptance: "running checks", evaluator: "independent review" };
  const icon = (s) => {
    if (s.id === cur && d.alive) return el("span", { class: "spinner sm", "aria-label": "running" });
    if (s.status === "passed") return el("span", { class: "ic ok", "aria-label": "passed" }, "✓");
    if (s.status === "failed") return el("span", { class: "ic bad", "aria-label": "failed" }, "✗");
    return el("span", { class: "ic", "aria-label": "pending" }, "○");
  };
  return el("aside", { class: "stepbar", "aria-label": "Build steps", "data-key": "stepbar" },
    el("h2", {}, "Steps ", el("span", { class: "muted" }, `${d.passed}/${d.steps.length}`)),
    d.steps.length ? el("ol", {}, d.steps.map((s) => el("li", {
      class: [s.id === cur ? "cur" : "", s.status].join(" "), title: (s.reasons || []).slice(0, 2).join("\n"), "data-key": `rail:${s.id}` },
      icon(s),
      el("div", {}, el("div", {}, el("span", { class: "sid" }, s.id), " ", s.title || ""),
        s.id === cur ? el("div", { class: "muted small" },
          `attempt ${d.current.attempt} · ${phaseLabel[d.current.phase] || d.current.phase}${d.alive ? "" : " (stopped)"}`,
          d.alive && d.activity && d.activity.text ? el("div", { class: "act" }, d.activity.text) : null) :
        s.attempts > 0 && s.status !== "passed" ? el("div", { class: "muted small" }, `${s.attempts} attempt(s)`) : null)))) :
      el("p", { class: "muted" }, "No steps yet: the plan isn't approved."));
}
const planViewer = el("pre", { "data-self-managed": "" }, "Pick a file to view it.");
const planViewerTitle = el("h3", { "data-self-managed": "" }, "File");

let sharedAck = false;
let testsAck = false;
let ackFor = "";                       // head_sha the ticks were given for: a new candidate starts unticked
const RISKY = /\b(curl|wget|nc|ncat|socat|ssh|scp|base64|eval|exec|python3? -c|node -e|perl|env|printenv|sudo)\b|\|\s*(ba)?sh\b|\$\(|`|>\s*\/|https?:\/\//;
let domainAck = false;
const cmdView = (c) => {
  const risky = RISKY.test(c);
  return el("div", { class: risky ? "cmd risky" : "cmd" }, risky ? "⚠ " : "", c);
};

async function planAction(name, act, label, headSha) {
  if (!(await confirmDialog({ title: `${label}?`, body: `Project ${name}.`, ok: label, okClass: "btn primary" }))) return;
  try {
    await api(`/api/p/${encodeURIComponent(name)}/action/${act}`, { method: "POST",
      headers: { "X-Hands": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ feedback: act === "plan" || act === "amend" ? planFeedback : "", head_sha: headSha || "",
        shared_ack: act === "approve" ? sharedAck : false, domains_ack: act === "approve" ? domainAck : false,
        tests_ack: act === "approve" ? testsAck : false }) });
    if (act === "plan" || act === "amend") planFeedback = "";
    if (act === "approve") { sharedAck = false; domainAck = false; testsAck = false; }
  } catch (e) { toast(`${label} failed: ${e.message}`, true); }
  refresh(false);
}

// "Amend plan": a change request for an approved (or hand-written) spec. The planner may only append steps.
function amendForm(d, p, btn) {
  const fb = el("textarea", { rows: "3", maxlength: "8000", class: "feedback",
    placeholder: "What should be added or changed? (e.g. 'add a pricing page with three tiers')" });
  fb.value = planFeedback;
  fb.addEventListener("input", () => { planFeedback = fb.value; });
  return el("div", { class: "amend" },
    el("h3", {}, "Amend plan"),
    el("p", { class: "muted" }, "Describe the change. The planner reads the built app and the approved spec, then appends new steps " +
      `(ids C${(p.round || 0) + (p.state === "approved" || !p.mode || p.mode === "fresh" ? 1 : 0)}-…). ` +
      "Existing steps stay as they are; steps already built cannot change. You review the amendment before anything is built."),
    el("label", { for: "amendtext" }, "Change request"), Object.assign(fb, { id: "amendtext" }),
    el("div", { class: "row out" }, btn("Amend plan", "amend", "Start the planner on this change", !d.alive)));
}

const valList = (v) => Array.isArray(v) ? (v.length ? v.join(", ") : "(none)") : (v || "(none)");

function amendNotice(p) {
  const items = [el("div", {}, el("strong", {}, "New steps: "), el("span", { class: "mono" }, (p.new_steps || []).join(", ") || "(none)")),
    el("div", {}, el("strong", {}, "Built app at: "), el("span", { class: "mono" }, (p.app_sha || "").slice(0, 12)))];
  if (p.request) items.push(el("div", {}, el("strong", {}, "Change request: "), el("span", { class: "goal" }, p.request)));
  return el("div", { class: "notice" }, el("strong", {}, `Amendment C${p.round || "?"}`), ...items);
}

// existing acceptance tests the amendment changes or removes (steps.json tests_changed, checked against the
// real diff by the planner); needs its own tick like the shared-settings box
function testsBox(p) {
  const box = el("input", { type: "checkbox", id: "testsack" });
  box.checked = testsAck;
  box.addEventListener("change", () => { testsAck = box.checked; refresh(false); });
  const row = (x) => el("tr", {}, el("td", { class: "mono" }, x.path), el("td", {}, x.change === "remove" ? "removed" : "changed"),
    el("td", { class: "goal" }, x.why || ""));
  return el("div", { class: "warnbox" },
    el("strong", {}, "This amendment changes or removes existing acceptance tests:"),
    el("table", { class: "diff" }, el("thead", {}, el("tr", {}, ["Test", "Change", "Why"].map((h) => el("th", {}, h)))),
      el("tbody", {}, p.tests_changed.map(row))),
    el("label", { for: "testsack", class: "inline" }, box, " I checked these test changes and want them"));
}

function sharedBox(p) {
  const box = el("input", { type: "checkbox", id: "sharedack" });
  box.checked = sharedAck;
  box.addEventListener("change", () => { sharedAck = box.checked; refresh(false); });
  return el("div", { class: "warnbox" },
    el("strong", {}, "This amendment changes settings that apply to every step:"),
    el("table", { class: "diff" }, el("thead", {}, el("tr", {}, ["Setting", "Was", "Now"].map((h) => el("th", {}, h)))),
      el("tbody", {}, p.shared_changed.map((x) => el("tr", {},
        el("td", { class: "mono" }, x.key), el("td", { class: "cmd old" }, valList(x.old)), el("td", { class: "cmd" }, valList(x.new)))))),
    el("label", { for: "sharedack", class: "inline" }, box, " I checked these changes and want them"));
}

async function showPlanFile(name, path) {
  planViewerTitle.textContent = path;
  planViewer.textContent = "Loading…";
  try { planViewer.textContent = await api(`/api/p/${encodeURIComponent(name)}/planfile?path=${encodeURIComponent(path)}`); }
  catch (e) { planViewer.textContent = `failed: ${e.message}`; }
}

function planSection(d) {
  const p = d.plan || {};
  const st = p.state || "none";
  const btn = (label, act, confirmLabel, enabled = true, head = "") =>
    el("button", { class: "act", "data-act": "plan", "data-arg": act, "data-label": confirmLabel, "data-head": head,
      ...(enabled ? {} : { disabled: "" }) }, label);
  const fb = el("textarea", { rows: "4", maxlength: "8000", class: "feedback",
    placeholder: "What should change? (e.g. 'use Cloudflare Pages, drop the blog, add a pricing page')" });
  fb.value = planFeedback;
  fb.addEventListener("input", () => { planFeedback = fb.value; });
  const log = (p.log || []).length ? el("pre", { class: "log-small" }, p.log.join("\n")) : null;
  const head = el("div", { class: "row" }, el("h2", {}, "Plan"), badge(p.running ? "planning" : st),
    p.pr_url ? el("span", { class: "muted" }, `PR #${p.pr}: ${p.pr_url}`) : null);
  const amendable = d.setup && d.setup.spec_ready;        // steps.json on the spec branch: only amendments from here on
  if (st === "none") {
    if (amendable) {
      return el("section", { "data-key": "plan" }, head, p.error ? el("p", { class: "reason" }, p.error) : null, amendForm(d, p, btn),
        log ? el("details", {}, el("summary", {}, "Planner log"), log) : null);
    }
    return el("section", { "data-key": "plan" }, head,
      el("p", { class: "muted" }, "The planner researches your idea and writes the spec: features, design, steps and acceptance tests. Takes roughly 10-40 minutes."),
      el("div", { class: "row" }, btn("Plan it", "plan", "Start the planner", !d.alive)), log);
  }
  if (st === "planning" || p.running) {
    const a = p.activity || {};
    const mins = p.started ? Math.max(0, Math.round((Date.now() / 1000 - p.started) / 60)) : null;
    const PH = ["Design (brainstorming)", "Task plan (writing-plans)", "Conventions", "Acceptance tests", "Machine spec", "Review summary"];
    const written = new Set(p.files_written || []);
    const phaseDone = (ph) => [...written].some((f) => phaseOf(f) === ph);
    return el("section", { "data-key": "plan" }, head,
      el("div", { class: "now" }, el("span", { class: "spinner", "aria-hidden": "true" }),
        el("div", {},
          el("div", { class: "now-title" }, a.phase ? `Working on: ${a.phase}` : "Planning…",
            a.file ? el("span", { class: "mono muted" }, `  ${a.file}`) : null),
          el("div", { class: "muted" }, a.text || "", mins !== null ? ` · ${mins} min elapsed` : ""))),
      el("ol", { class: "phases" }, PH.map((ph) => el("li", { class: ph === a.phase ? "cur" : phaseDone(ph) ? "done" : "" },
        ph === a.phase ? el("span", { class: "spinner sm", "aria-hidden": "true" }) : el("span", { class: "dot" }, phaseDone(ph) ? "✓" : "○"), " ", ph))),
      el("p", { class: "muted" }, "Typical run: 15–40 minutes. You'll get a Telegram message when the plan is ready."),
      log ? el("details", {}, el("summary", {}, "Planner log"), log) : null);
  }
  const amend = p.mode === "amend";
  if (st === "approved") {
    return el("section", { "data-key": "plan" }, head,
      el("p", { class: "muted" }, `${amend ? `Amendment C${p.round}` : "Plan"} approved and merged${p.pr ? ` (PR #${p.pr})` : ""}. ${p.summary || ""}`),
      p.error ? el("p", { class: "reason" }, p.error) : null,
      amendForm(d, p, btn), log ? el("details", {}, el("summary", {}, "Planner log"), log) : null);
  }
  if (st === "failed" && amend) {
    return el("section", { "data-key": "plan" }, head, el("p", { class: "reason" }, p.error || "the amendment failed"),
      amendForm(d, p, btn), log ? el("details", {}, el("summary", {}, "Planner log"), log) : null);
  }
  const parts = [head];
  if (p.error) parts.push(el("p", { class: "reason" }, p.error));
  if (st === "ready") {
    if ((p.head_sha || "") !== ackFor) { ackFor = p.head_sha || ""; sharedAck = false; domainAck = false; testsAck = false; }
    parts.push(el("p", {}, p.summary || ""));
    if (amend) parts.push(amendNotice(p));
    if (amend && (p.shared_changed || []).length) parts.push(sharedBox(p));
    if (amend && (p.tests_changed || []).length) parts.push(testsBox(p));
    (p.warnings || []).forEach((w) => parts.push(el("div", { class: "notice warn" }, el("strong", {}, "Check before approving"), w)));
    if ((p.domains || []).length) {
      const box = el("input", { type: "checkbox", id: "domack" });
      box.checked = domainAck;
      box.addEventListener("change", () => { domainAck = box.checked; refresh(false); });
      parts.push(el("div", { class: "warnbox" },
        el("strong", {}, "New network access for the build containers (they hold your Claude token and project.env):"),
        el("ul", {}, p.domains.map((x) => el("li", { class: "mono" }, x))),
        el("label", { for: "domack", class: "inline" }, box, " I checked these domains and want to allow them")));
    }
    if (p.shared) parts.push(el("div", { class: "kv" },
      el("div", {}, el("strong", {}, "Builders may change: "), (p.shared.allowed_paths || []).join(", ")),
      (p.shared.allow_text_paths || []).length ? el("div", {}, el("strong", {}, "Text files allowed: "), p.shared.allow_text_paths.join(", ")) : null,
      el("div", {}, el("strong", {}, "Always given to builders: "), (p.shared.always_refs || []).join(", ") || "(none)"),
      el("strong", {}, "Runs before every step's checks (with project.env):"),
      ...[...(p.shared.setup || []), ...(p.shared.regression || [])].map(cmdView),
      p.shared.e2e_command ? el("div", {}, el("strong", {}, "E2E command: "), cmdView(p.shared.e2e_command)) : null,
      el("p", { class: "muted" }, "⚠ marks commands that reach the network, run inline code or read the environment. " +
        "The acceptance test files below are code too: they run with project.env. Open them before approving.")));
  }
  if (p.step_list && p.step_list.length) {
    parts.push(el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Step", "Title", "Type", "Checks"].map((h) => el("th", {}, h)))),
      el("tbody", {}, p.step_list.map((x) => el("tr", { class: x.new ? "new" : "" },
        el("td", {}, x.id, x.new ? el("span", { class: "tag" }, "new") : null), el("td", {}, x.title, el("div", { class: "muted goal" }, x.goal || ""),
          x.notes ? el("div", { class: "muted goal" }, `Notes: ${x.notes}`) : null,
          (x.refs || []).length ? el("div", { class: "muted goal" }, `Refs: ${x.refs.join(", ")}`) : null),
        el("td", {}, x.type || "code", x.max_attempts ? ` (max ${x.max_attempts})` : ""),
        el("td", {}, (x.acceptance || []).map(cmdView), (x.e2e || []).map((f) => el("div", { class: "cmd" }, `e2e: ${f}`)),
          (x.acceptance_files || []).map((f) => el("div", { class: "cmd" }, `owns: acceptance/${f}`)))))))));
  }
  if (p.files && p.files.length) {
    parts.push(el("div", { class: "files" }, el("strong", {}, "Files: "),
      p.files.map((f) => el("button", { "data-act": "plan-file", "data-arg": f }, f))));
    parts.push(keep(planViewerTitle, "plan-viewer-title"), keep(planViewer, "plan-viewer"));
  }
  if (p.plan_md) parts.push(el("h3", {}, "PLAN.md"), el("pre", {}, p.plan_md));
  parts.push(el("label", {}, st === "ready" ? "Feedback (optional, for a re-plan)" : "Feedback for the next attempt (optional)"), fb);
  const canApprove = !d.alive && (!(p.domains || []).length || domainAck) && (!amend || !(p.shared_changed || []).length || sharedAck)
    && (!amend || !(p.tests_changed || []).length || testsAck);
  parts.push(el("div", { class: "row out" },
    st === "ready" ? btn(`Approve & build${amend ? " amendment" : ""} (${(p.head_sha || "").slice(0, 7)})`, "approve",
      `Merge PR #${p.pr} at ${(p.head_sha || "").slice(0, 7)} exactly as reviewed and start building`, canApprove, p.head_sha || "") : null,
    btn(st === "ready" ? "Re-plan with feedback" : "Plan again", amend ? "amend" : "plan",
      st === "ready" ? `Discard this ${amend ? "amendment" : "plan"} and re-plan with your feedback` : "Run the planner again",
      !d.alive && (!amend || !!planFeedback.trim()))));
  if (log) parts.push(el("details", {}, el("summary", {}, "Planner log"), log));
  return el("section", { "data-key": "plan" }, ...parts);
}

async function refresh(full) {
  if (mode === "new" || mode === "empty") return;
  if (mode === "creating") return refreshCreating();
  if (!current) return;
  return hands.store.sync(current);    // `full` is history: attempts come with every changed snapshot
}

// Collapsible panels (0.6): <details class="panel"> per section; open state remembered per project in
// localStorage (hands.panels.<name>), defaults from the project's state. A patch keeps the live open state.
const PANEL_DEFAULTS = {
  plan: (d) => ["ready", "planning", "failed"].includes((d.plan && d.plan.state) || "") || !!(d.plan && d.plan.running),
  rounds: (d) => !!(d.round && d.round.record && d.round.record.status !== "merged"),
  setup: (d) => !(d.setup && d.setup.spec_ready),
  env: () => false, steps: () => true, metrics: () => false, log: (d) => !!d.alive, attempts: () => false,
  progress: () => false, viewer: () => false, create: () => true, reset: () => false,
  planchat: (d) => !!(d.next && d.next.plan_session && ["open", "brief_ready"].includes(d.next.plan_session.status)),
  stylerefs: (d) => !!(d.next && ((d.next.style_refs || []).some((r) => r.status !== "frozen") || (d.next.setup && d.next.setup.status === "failed"))),
};
function panelsSaved(name) {
  try { const v = JSON.parse(localStorage.getItem(`hands.panels.${name}`) || "{}"); return v && typeof v === "object" ? v : {}; }
  catch (e) { return {}; }
}
function panelOpen(name, key, d) {
  const saved = panelsSaved(name);
  if (typeof saved[key] === "boolean") return saved[key];
  const f = PANEL_DEFAULTS[key];
  return f ? !!f(d) : false;
}
function panel(name, key, title, d, body, hint) {
  if (!body) return null;
  return el("details", { class: "panel", "data-key": `panel:${key}`, "data-panel": key, ...(panelOpen(name, key, d) ? { open: "" } : {}) },
    el("summary", {}, el("span", { class: "ptitle" }, title), hint ? el("span", { class: "muted small" }, hint) : null),
    body);
}
function onPanelToggle(ev) {
  const t = ev.target;
  if (!t || !t.matches || !t.matches("details.panel[data-panel]") || !current) return;
  const saved = panelsSaved(current);
  saved[t.dataset.panel] = t.open;
  try { localStorage.setItem(`hands.panels.${current}`, JSON.stringify(saved)); } catch (e) { /* ignore */ }
}

// Build the page tree for one snapshot and patch it into #main (the first paint replaces "Loading…").
function render(name, d, log) {
  if (!d) return;
  const att = el("div", { id: "attempts-wrap" }, attemptsCache.name === name ? attemptsSection(name, attemptsCache.rows) : null);
  const logSec = keep(logPanel.root, "log");
  const showSetup = d.managed || !(d.setup && d.setup.spec_ready);
  const createSec = d.create && d.create.state !== "done"
    ? el("section", { "data-key": "create" }, el("h2", {}, `Creation: ${d.create.state}`), d.create.error ? el("p", { class: "reason" }, d.create.error) : null,
      el("pre", {}, d.create.lines.join("\n"))) : null;
  const p = d.plan || {};
  const failed = d.steps.filter((s) => s.status === "failed").length;
  const content = el("div", { class: "content" }, ...[statusSection(d),
    panel(name, "create", "Creation", d, createSec),
    d.managed ? panel(name, "plan", "Plan", d, planSection(d), p.running ? "planning…" : p.state && p.state !== "none" ? p.state : "") : null,
    panel(name, "rounds", "Change rounds", d, roundsSection(d)),
    d.next && !d.next.error ? panel(name, "planchat", "Plan with Claude", d, hands.nextform.planChatPanel(document, name, d.next.plan_session),
      planChatHint(d.next.plan_session)) : null,
    d.next && !d.next.error ? panel(name, "stylerefs", "Style refs & setup", d, hands.nextform.styleRefsPanel(document, d.next, addRefsNode(name)),
      styleRefsHint(d.next)) : null,
    showSetup ? panel(name, "setup", "Setup", d, setupSection(d)) : null,
    panel(name, "env", "Environment", d, envSection(name)),
    panel(name, "steps", "Steps", d, stepsSection(d), `${d.passed}/${d.steps.length} passed${failed ? ` · ${failed} failed` : ""}`),
    panel(name, "metrics", "Sessions", d, metricsSection(d), `${d.total_sessions} · ~$${d.total_cost_usd}`),
    panel(name, "log", "Runner log", d, logSec, d.alive ? "live" : ""),
    panel(name, "attempts", "Attempts", d, att, attemptsCache.name === name ? String(attemptsCache.rows.length) : ""),
    panel(name, "progress", "Progress notes", d, progressSection(d), String((d.progress || []).length)),
    panel(name, "viewer", "File", d, el("section", { "data-key": "viewer" }, keep(viewerTitle, "viewer-title"), keep(viewer, "viewer-pre"))),
    panel(name, "reset", "Reset project", d, resetSection(d))].filter(Boolean));
  const next = el("div", { class: "layout" }, content, stepBar(d));
  const live = $("#main").firstElementChild;
  if (live && live.classList.contains("layout")) hands.render.patch(live, next);
  else $("#main").replaceChildren(next);
}

function until(ts) {
  const s = Math.max(0, ts - Date.now() / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function resetLabel(ts, withDay) {
  const dt = new Date(ts * 1000);
  const t = dt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return withDay ? `${dt.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${t}` : t;
}
// A window whose reset time has passed is never shown as current (requirement 6): the bar is dimmed, the
// text says the window reset and when, and a refresh is offered. `expired` comes from the server (same clock).
function meter(label, w, withDay) {
  if (!w) return el("div", { class: "meter muted" }, `${label}: no data`);
  const pct = w.used_pct;
  const lvl = pct === null || pct === undefined ? "" : pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok";
  const fill = el("span", { class: `fill ${lvl}` });
  fill.style.width = `${Math.min(100, Math.max(0, pct || 0))}%`;   // CSSOM, allowed by the CSP (style attrs are not)
  const bar = el("span", { class: "bar" }, fill);
  const when = w.resets_at ? new Date(w.resets_at * 1000).toLocaleString() : "";
  if (w.expired) {
    return el("div", { class: "meter expired", title: `this window reset ${when}; the number is from before the reset` },
      el("strong", {}, label), " ", bar, " ", el("s", {}, pct === null || pct === undefined ? "?" : `${Math.round(pct)}%`),
      el("span", { class: "muted" }, ` · reset ${resetLabel(w.resets_at, withDay)} · stale, refresh for the current window`));
  }
  return el("div", { class: "meter", title: when ? `resets ${when}` : "" },
    el("strong", {}, label), " ", bar, " ", pct === null || pct === undefined ? "?" : `${Math.round(pct)}%`,
    w.resets_at ? el("span", { class: "muted" }, ` · resets ${resetLabel(w.resets_at, withDay)} (in ${until(w.resets_at)})`) : null);
}
async function refreshUsage(btn) {
  try { await api("/api/usage/refresh", { method: "POST", headers: { "X-Hands": "1" } }); if (btn) btn.textContent = "…"; }
  catch (e) { toast(`Usage refresh failed: ${e.message}`, true); }
}
// one automatic refresh per expired window (keyed by its reset time; remembered across reloads), never more:
// every refresh costs one short Haiku reply. After that the ↻ button is the only way.
function autoRefreshUsage(u) {
  const key = ["five_hour", "seven_day"].map((w) => (u[w] && u[w].expired ? `${w}:${u[w].resets_at}` : "")).filter(Boolean).join(",");
  if (!key || u.refreshing) return;
  let done = "";
  try { done = localStorage.getItem("hands.usage.autorefresh") || ""; } catch (e) { done = ""; }
  if (done === key) return;
  try { localStorage.setItem("hands.usage.autorefresh", key); } catch (e) { /* ignore */ }
  refreshUsage(null);
}
async function loadUsage() {
  try {
    const u = await api("/api/usage");
    const old = !u.at || (u.now || Date.now() / 1000) - u.at > 3 * 3600;
    const btn = el("button", { class: "linkbtn", title: "Refresh usage (one tiny Haiku reply)", "data-act": "usage-refresh" },
      u.refreshing ? "…" : "↻");
    const note = u.stale && u.at ? `stale · as of ${new Date(u.at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : u.at ? `as of ${new Date(u.at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "no usage data yet";
    $("#usage").replaceChildren(meter("Session", u.five_hour, false), meter("Week", u.seven_day, true),
      el("span", { class: u.stale || old ? "muted stale" : "muted" }, note), btn);
    autoRefreshUsage(u);
  } catch (e) { /* keep last */ }
}

// Housekeeping tick (0.6): the project page itself is pushed by the store (SSE); this only refreshes the
// tab strip, the usage meters, the creating page and the session list.
const TICK_MS = 5000;
let ticks = 0;
function tick() {
  if (document.hidden) return;
  const a = document.activeElement;
  const typing = a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT") && !(a.closest && a.closest(".xterm"));
  if (typing) return;   // don't redraw while typing in a form (the terminal's own textarea is fine)
  if (sectionFromHash() === "sessions") { if (window.hands && window.hands.sessions) window.hands.sessions.poll(); ticks += 1; return; }
  if (ticks % 2 === 0) loadProjects().catch(() => {});      // every 10 s
  if (ticks % 12 === 0) loadUsage();                         // every 60 s
  if (mode === "creating") refreshCreating();
  ticks += 1;
}

// ---- app shell: sections (#/builder, #/sessions) and the phone drawer
const SECTIONS = { builder: "Project builder", sessions: "Session manager" };
function sectionFromHash() {
  const m = /^#\/([a-z]+)/.exec(location.hash || "");
  return m && SECTIONS[m[1]] ? m[1] : "builder";
}
function route() {
  const sec = sectionFromHash();
  for (const name of Object.keys(SECTIONS)) {
    const page = document.getElementById(`page-${name}`);
    if (page) page.hidden = name !== sec;
  }
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.dataset.section === sec) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  $("#topsection").textContent = SECTIONS[sec];
  document.title = `${SECTIONS[sec]} · Phoenix Harness`;
  setNav(false);
  if (sec === "sessions" && window.hands && window.hands.sessions) {
    const m = /^#\/sessions\/([0-9a-f-]{36})$/.exec(location.hash || "");
    window.hands.sessions.show(m ? m[1] : null);
  }
}
function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  $("#navtoggle").setAttribute("aria-expanded", String(open));
  $("#scrim").hidden = !open;
}
function initShell() {
  $("#navtoggle").addEventListener("click", () => setNav(!document.body.classList.contains("nav-open")));
  $("#scrim").addEventListener("click", () => setNav(false));
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && document.body.classList.contains("nav-open")) { setNav(false); $("#navtoggle").focus(); } });
  window.addEventListener("hashchange", route);
  $("#tabs").addEventListener("scroll", updateTabShadows);
  window.addEventListener("resize", updateTabShadows);
  if (window.hands && window.hands.sessions) window.hands.sessions.init();   // sessions.js loads after this file
  route();
}

document.addEventListener("DOMContentLoaded", async () => {
  initShell();
  document.addEventListener("click", dispatch);
  document.addEventListener("toggle", onPanelToggle, true);     // toggle does not bubble: capture it
  logPanel = hands.logpanel.create(document, hands.store.logAppend);
  hands.store.subscribe(onSnapshot);
  let saved = null;
  try { saved = localStorage.getItem("hands.project"); } catch (e) { saved = null; }
  current = saved;
  await loadProjects().catch(() => {});
  loadUsage();
  if (current && current === saved) select(current);
  timer = setInterval(tick, TICK_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); refresh(false); } });
});
