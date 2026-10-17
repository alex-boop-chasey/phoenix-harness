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
  const r = await fetch(path, opts);
  if (r.status === 401) { $("#main").replaceChildren(el("p", {}, "Not logged in. On the VPS run: hands-dash url")); throw new Error("401"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((body && body.error) || r.status);
  return body;
}

const badge = (s) => el("span", { class: `badge s-${String(s || "").replace(/[^a-z_]/g, "")}` }, s || "?");
const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
const localTs = (iso) => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleString() : (iso || ""); };
let mode = "project";        // "project" | "new" | "creating" | "empty" (every project closed)
let creatingSlug = null;
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
  current = name;
  delete envPanels[name];              // fresh editor (and listing) each time a project is opened
  try { localStorage.setItem("hands.project", name); } catch (e) { /* ignore */ }
  $("#main").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  viewerTitle.textContent = "File";
  viewer.textContent = "Pick an attempt file.";
  refresh(true);
  loadProjects().catch(() => {});      // tab highlight at once, not on the next tick
}

// ---- project tabs (WAI-ARIA tabs: roving tabindex, arrows move, Enter opens, Delete closes)
let lastScrolledTo = null;
function renderTabs(open, hidden) {
  const strip = $("#tabs");
  const active = document.activeElement;
  const focused = active && strip.contains(active) ? active.dataset.name : null;
  const selected = mode === "project" ? current : null;
  const tabs = open.map((p, i) => el("button", {
    class: "tab", type: "button", role: "tab", "data-name": p.name, title: p.name,
    "aria-selected": String(p.name === selected), tabindex: (selected ? p.name === selected : i === 0) ? "0" : "-1",
    on: { click: (ev) => { if (ev.target.closest(".x")) closeTab(p); else select(p.name); }, keydown: (ev) => tabKey(ev, p) } },
    el("span", { class: "tname" }, p.name), badge(p.error ? "error" : p.status),
    el("span", { class: "x", "aria-label": `Close ${p.name}`, title: "Close tab (Delete)" }, "×")));
  const add = el("button", { class: "tab-add", type: "button", "data-name": "+", "aria-haspopup": "menu",
    "aria-expanded": String(!!menuEl), "aria-current": String(mode === "new" || mode === "creating"),
    "aria-label": "New project or reopen a closed one", title: "New project or reopen a closed one",
    on: { click: () => toggleMenu() } }, "+");
  strip.replaceChildren(...tabs, add,
    ...(open.length ? [] : [el("span", { class: "tabs-hint" }, hidden.length ? `${hidden.length} closed` : "No projects yet")]));
  if (focused) { const t = strip.querySelector(`[data-name="${CSS.escape(focused)}"]`); if (t) t.focus(); }
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
  catch (e) { alert(`failed: ${e.message}`); return; }
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
  try { await api(`/api/p/${encodeURIComponent(name)}/unhide`, { method: "POST", headers: { "X-Hands": "1" } }); }
  catch (e) { alert(`failed: ${e.message}`); return; }
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

// ---- confirmation dialog (<dialog>; falls back to window.confirm where showModal is missing)
function confirmDialog({ title, body, warn, ok, okClass }) {
  if (typeof HTMLDialogElement !== "function" || !HTMLDialogElement.prototype.showModal) {
    return Promise.resolve(confirm(`${title}\n\n${warn ? warn + "\n\n" : ""}${body}`));
  }
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; resolve(v); dlg.close(); dlg.remove(); };
    const cancel = el("button", { class: "btn", type: "button", on: { click: () => finish(false) } }, "Cancel");
    const okBtn = el("button", { class: okClass || "btn danger", type: "button", on: { click: () => finish(true) } }, ok || "OK");
    const dlg = el("dialog", { class: "dialog", "aria-labelledby": "dlg-title", on: { close: () => finish(false) } },
      el("h2", { id: "dlg-title" }, title),
      warn ? el("div", { class: "notice warn" }, warn) : null,
      el("p", { class: "muted" }, body),
      el("div", { class: "row" }, cancel, okBtn));
    document.body.append(dlg);
    dlg.showModal();
    cancel.focus();
  });
}

async function action(name, act, label) {
  if (!confirm(`${label} for ${name}?`)) return;
  const out = document.getElementById("action-out");
  out.textContent = `${label}…`;
  try {
    const r = await api(`/api/p/${encodeURIComponent(name)}/action/${act}`,
      { method: "POST", headers: { "X-Hands": "1" } });
    out.textContent = `rc=${r.rc}\n${r.output}`;
  } catch (e) { out.textContent = `failed: ${e.message}`; }
  refresh(false);
}

function statusSection(d) {
  const btn = (act, label, enabled) =>
    el("button", { class: "act", on: { click: () => action(d.name, act, label) }, ...(enabled ? {} : { disabled: "" }) }, label);
  const cur = d.current ? `${d.current.step} attempt ${d.current.attempt} · ${d.current.phase}` : "—";
  return el("section", {},
    el("div", { class: "row" }, el("h2", {}, d.name), badge(d.status),
      el("span", { class: "muted" }, d.alive ? "runner alive" : "runner not running"),
      d.stop ? el("span", { class: "badge s-stopped" }, "STOP requested") : null),
    d.reason ? el("p", { class: d.status === "escalation" ? "reason" : "muted" }, d.reason) : null,
    el("p", {}, "Current: ", cur),
    el("p", { class: "muted" },
      `${d.passed}/${d.steps.length} steps passed · ${d.total_sessions} sessions · ~$${d.total_cost_usd} API-equivalent · ` +
      `coder ${d.model_override || d.config.coder_model} · eval ${d.model_override || d.config.eval_model} · ${d.config.image}`),
    d.model_override ? el("p", { class: "reason" }, `Model override active: ${d.model_override}`) : null,
    d.spec_error ? (d.setup && !d.setup.spec_ready
      ? el("p", { class: "muted" }, "No plan yet: the spec repo has no steps.json.")
      : el("p", { class: "reason" }, `Spec: ${d.spec_error}`)) : null,
    el("div", { class: "row" },
      btn("start", "Start", !d.alive && d.status !== "escalation" && !d.spec_error),
      btn("stop", "Stop at next safe point", d.alive && !d.stop),
      btn("ack", "Acknowledge escalation", !d.alive && d.status === "escalation"),
      btn("ack-clear-model", "Ack + clear model override", !d.alive && !!d.model_override)),
    el("pre", { id: "action-out", class: "out" }, ""));
}

function stepsSection(d) {
  return el("section", {}, el("h2", {}, "Steps"),
    el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Step", "Title", "Status", "Att.", "Commit"].map((h) => el("th", {}, h)))),
      el("tbody", {}, d.steps.map((s) => [
        el("tr", {}, el("td", {}, s.id), el("td", {}, s.title, s.type === "human" ? " (human)" : ""),
          el("td", {}, badge(s.status)), el("td", { class: "num" }, s.attempts), el("td", {}, (s.commit || "").slice(0, 9))),
        s.status === "failed" && s.reasons.length
          ? el("tr", {}, el("td", {}), el("td", { colspan: "4", class: "reason" }, s.reasons.slice(0, 4).join("\n"))) : null,
      ])))));
}

function metricsSection(d) {
  const rows = d.metrics.slice().reverse();
  return el("section", {}, el("h2", {}, "Sessions"),
    el("div", { class: "scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Time", "Role", "Step", "Result", "Turns", "$", "Dur"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((m) => el("tr", {},
        el("td", {}, localTs(m.ts)), el("td", {}, m.role), el("td", {}, `${m.step}#${m.attempt}`),
        el("td", {}, m.kind), el("td", { class: "num" }, m.turns), el("td", { class: "num" }, Number(m.cost_usd || 0).toFixed(2)),
        el("td", { class: "num" }, `${Math.round((m.duration_s || 0) / 60)}m`)))))));
}

function progressSection(d) {
  return el("section", {}, el("h2", {}, "Progress notes"),
    el("div", { class: "scroll" }, d.progress.slice().reverse().map((p) =>
      el("div", { class: "attempt" }, el("strong", {}, `${p.step} ${p.title || ""}`), " ",
        el("span", { class: "muted" }, `${(p.commit || "").slice(0, 9)} · ${localTs(p.ts)}`),
        el("div", {}, p.summary || ""),
        (p.risks || []).length ? el("div", { class: "muted" }, "Risks: " + p.risks.join("; ")) : null))));
}

async function showFile(name, att, f, target) {
  target.textContent = "Loading…";
  try { target.textContent = await api(`/api/p/${encodeURIComponent(name)}/attempts/${encodeURIComponent(att)}/${encodeURIComponent(f)}`); }
  catch (e) { target.textContent = `failed: ${e.message}`; }
}

const viewer = el("pre", {}, "Pick an attempt file.");
const viewerTitle = el("h2", {}, "File");

function attemptsSection(name, rows) {
  return el("section", {}, el("h2", {}, "Attempts"),
    el("div", { class: "scroll" }, rows.map((a) => el("div", { class: "attempt" },
      el("div", { class: "row" }, el("strong", {}, a.name), el("span", { class: "muted" }, when(a.mtime)),
        a.verdict_pass === true ? badge("passed") : a.verdict_pass === false ? badge("failed") : null),
      a.reason ? el("div", { class: "reason" }, a.reason) : null,
      el("div", { class: "files" }, a.files.map((f) =>
        el("button", { on: { click: () => { viewerTitle.textContent = `${a.name} / ${f.name}`; showFile(name, a.name, f.name, viewer); } } },
          f.name)))))));
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
  return el("section", { class: "envsec" }, el("h2", {}, "Environment"),
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
  const envLocal = envEditor({ local: true });
  const form = el("form", { on: { submit: async (ev) => {
    ev.preventDefault();
    let vars;
    try { vars = envLocal.collect(); } catch (e) { out.textContent = e.message; return; }
    if (!confirm(`Create GitHub repos ${slug.value}-specs and ${slug.value}-app and set everything up?`)) return;
    submit.disabled = true;
    out.textContent = "Starting…";
    try {
      await api("/api/new", { method: "POST", headers: { "X-Hands": "1", "Content-Type": "application/json" },
        body: JSON.stringify({ slug: slug.value, description: desc.value }) });
    } catch (e) { out.textContent = `failed: ${e.message}`; submit.disabled = false; return; }
    creatingEnvError = "";
    if (vars.length) {
      try {
        await api(`/api/p/${encodeURIComponent(slug.value)}/env`, { method: "POST",
          headers: { "X-Hands": "1", "Content-Type": "application/json" }, body: JSON.stringify({ set: vars }) });
      } catch (e) { creatingEnvError = `The project is being created, but its variables were not saved (${e.message}). Add them below.`; }
    }
    mode = "creating"; creatingSlug = slug.value; refresh(false);
  } } },
    el("label", { for: "slug" }, "Project slug"), slug,
    el("p", { class: "muted" }, "Lowercase letters, digits and hyphens. Becomes the repo names <slug>-specs and <slug>-app."),
    el("label", { for: "desc" }, "Idea"), desc,
    el("label", {}, "Environment variables (optional)"),
    el("p", { class: "muted" }, "Test API keys and service URLs the build containers need. You can also add them later on the project page."),
    envLocal,
    el("div", { class: "row" }, submit), out);
  $("#main").replaceChildren(el("section", {}, el("h2", {}, "New project"),
    el("p", { class: "muted" }, "Creates two private repos with branch protection, read-only/write deploy keys, " +
      "clones under ~/projects/<slug>/ and the runner config. Takes about a minute. Nothing is built until a plan is merged."),
    form));
}

let creatingEnvError = "";
async function refreshCreating() {
  const slug = creatingSlug;
  try {
    const c = await api(`/api/creating/${encodeURIComponent(slug)}`);
    if (mode !== "creating" || slug !== creatingSlug) return;
    const done = c.state === "done" && c.project_exists;
    $("#main").replaceChildren(...[el("section", {},
      el("div", { class: "row" }, el("h2", {}, `Creating ${slug}`), badge(c.state === "failed" ? "escalation" : c.state)),
      c.error ? el("p", { class: "reason" }, c.error) : null,
      el("pre", {}, c.lines.join("\n")),
      done ? el("div", { class: "row out" }, el("button", { class: "act", on: { click: () => select(slug) } }, `Open ${slug}`)) : null,
      c.state === "failed" ? el("p", { class: "muted" }, c.project_exists
        ? `Fix the cause, then on the VPS: hands ${slug} destroy --confirm ${slug}   and create it again.`
        : "Nothing was created. Fix the cause and create it again.") : null),
      c.state === "running" ? el("section", { class: "envsec" }, el("h2", {}, "Environment"),
        creatingEnvError ? el("div", { class: "notice bad" }, creatingEnvError) : null,
        envPanel(`creating:${slug}`, slug)) : null].filter(Boolean));
  } catch (e) { $("#clock").textContent = `update failed: ${e.message}`; }
}

function setupSection(d) {
  const s = d.setup || {};
  const item = (ok, text, hint) => el("li", {}, el("span", { class: ok ? "s-passed" : "s-pending" }, ok ? "✓ " : "○ "), text,
    hint ? el("div", { class: "muted" }, hint) : null);
  return el("section", {}, el("h2", {}, "Setup"),
    s.description ? el("p", {}, s.description) : null,
    s.repo_specs ? el("p", { class: "muted" }, `Repos: ${s.repo_specs} · ${s.repo_app}`) : null,
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
  return el("aside", { class: "stepbar", "aria-label": "Build steps" },
    el("h2", {}, "Steps ", el("span", { class: "muted" }, `${d.passed}/${d.steps.length}`)),
    d.steps.length ? el("ol", {}, d.steps.map((s) => el("li", {
      class: [s.id === cur ? "cur" : "", s.status].join(" "), title: (s.reasons || []).slice(0, 2).join("\n") },
      icon(s),
      el("div", {}, el("div", {}, el("span", { class: "sid" }, s.id), " ", s.title || ""),
        s.id === cur ? el("div", { class: "muted small" },
          `attempt ${d.current.attempt} · ${phaseLabel[d.current.phase] || d.current.phase}${d.alive ? "" : " (stopped)"}`,
          d.alive && d.activity && d.activity.text ? el("div", { class: "act" }, d.activity.text) : null) :
        s.attempts > 0 && s.status !== "passed" ? el("div", { class: "muted small" }, `${s.attempts} attempt(s)`) : null)))) :
      el("p", { class: "muted" }, "No steps yet: the plan isn't approved."));
}
const planViewer = el("pre", {}, "Pick a file to view it.");
const planViewerTitle = el("h3", {}, "File");

const RISKY = /\b(curl|wget|nc|ncat|socat|ssh|scp|base64|eval|exec|python3? -c|node -e|perl|env|printenv|sudo)\b|\|\s*(ba)?sh\b|\$\(|`|>\s*\/|https?:\/\//;
let domainAck = false;
const cmdView = (c) => {
  const risky = RISKY.test(c);
  return el("div", { class: risky ? "cmd risky" : "cmd" }, risky ? "⚠ " : "", c);
};

async function planAction(name, act, label, headSha) {
  if (!confirm(`${label}?`)) return;
  try {
    await api(`/api/p/${encodeURIComponent(name)}/action/${act}`, { method: "POST",
      headers: { "X-Hands": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ feedback: act === "plan" ? planFeedback : "", head_sha: headSha || "" }) });
    if (act === "plan") planFeedback = "";
  } catch (e) { alert(`failed: ${e.message}`); }
  refresh(false);
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
  const btn = (label, onclick, enabled = true) =>
    el("button", { class: "act", on: { click: onclick }, ...(enabled ? {} : { disabled: "" }) }, label);
  const fb = el("textarea", { rows: "4", maxlength: "8000", class: "feedback",
    placeholder: "What should change? (e.g. 'use Cloudflare Pages, drop the blog, add a pricing page')" });
  fb.value = planFeedback;
  fb.addEventListener("input", () => { planFeedback = fb.value; });
  const log = (p.log || []).length ? el("pre", { class: "log-small" }, p.log.join("\n")) : null;
  const head = el("div", { class: "row" }, el("h2", {}, "Plan"), badge(p.running ? "planning" : st),
    p.pr_url ? el("span", { class: "muted" }, `PR #${p.pr}: ${p.pr_url}`) : null);
  if (st === "none") {
    return el("section", {}, head,
      el("p", { class: "muted" }, "The planner researches your idea and writes the spec: features, design, steps and acceptance tests. Takes roughly 10-40 minutes."),
      el("div", { class: "row" }, btn("Plan it", () => planAction(d.name, "plan", "Start the planner"), !d.alive)), log);
  }
  if (st === "planning" || p.running) {
    const a = p.activity || {};
    const mins = p.started ? Math.max(0, Math.round((Date.now() / 1000 - p.started) / 60)) : null;
    const PH = ["Design (brainstorming)", "Task plan (writing-plans)", "Conventions", "Acceptance tests", "Machine spec", "Review summary"];
    const written = new Set(p.files_written || []);
    const phaseDone = (ph) => [...written].some((f) => phaseOf(f) === ph);
    return el("section", {}, head,
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
  if (st === "approved") {
    return el("section", {}, head, el("p", { class: "muted" }, `Plan approved and merged${p.pr ? ` (PR #${p.pr})` : ""}. ${p.summary || ""}`));
  }
  const parts = [head];
  if (p.error) parts.push(el("p", { class: "reason" }, p.error));
  if (st === "ready") {
    parts.push(el("p", {}, p.summary || ""));
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
      el("tbody", {}, p.step_list.map((x) => el("tr", {},
        el("td", {}, x.id), el("td", {}, x.title, el("div", { class: "muted goal" }, x.goal || ""),
          x.notes ? el("div", { class: "muted goal" }, `Notes: ${x.notes}`) : null,
          (x.refs || []).length ? el("div", { class: "muted goal" }, `Refs: ${x.refs.join(", ")}`) : null),
        el("td", {}, x.type || "code", x.max_attempts ? ` (max ${x.max_attempts})` : ""),
        el("td", {}, (x.acceptance || []).map(cmdView), (x.e2e || []).map((f) => el("div", { class: "cmd" }, `e2e: ${f}`)),
          (x.acceptance_files || []).map((f) => el("div", { class: "cmd" }, `owns: acceptance/${f}`)))))))));
  }
  if (p.files && p.files.length) {
    parts.push(el("div", { class: "files" }, el("strong", {}, "Files: "),
      p.files.map((f) => el("button", { on: { click: () => showPlanFile(d.name, f) } }, f))));
    parts.push(planViewerTitle, planViewer);
  }
  if (p.plan_md) parts.push(el("h3", {}, "PLAN.md"), el("pre", {}, p.plan_md));
  parts.push(el("label", {}, st === "ready" ? "Feedback (optional, for a re-plan)" : "Feedback for the next attempt (optional)"), fb);
  parts.push(el("div", { class: "row out" },
    st === "ready" ? btn(`Approve & build (${(p.head_sha || "").slice(0, 7)})`, () => planAction(d.name, "approve",
      `Merge PR #${p.pr} at ${(p.head_sha || "").slice(0, 7)} exactly as reviewed and start building`, p.head_sha),
      !d.alive && (!(p.domains || []).length || domainAck)) : null,
    btn(st === "ready" ? "Re-plan with feedback" : "Plan again", () => planAction(d.name, "plan",
      st === "ready" ? "Discard this plan and re-plan with your feedback" : "Run the planner again"), !d.alive)));
  if (log) parts.push(el("details", {}, el("summary", {}, "Planner log"), log));
  return el("section", {}, ...parts);
}

async function refresh(full) {
  if (mode === "new" || mode === "empty") return;
  if (mode === "creating") return refreshCreating();
  const name = current;
  if (!name) return;
  try {
    const [d, log] = await Promise.all([api(`/api/p/${encodeURIComponent(name)}`), api(`/api/p/${encodeURIComponent(name)}/log?lines=300`)]);
    if (name !== current || mode !== "project") return;
    if (d.error) { $("#main").replaceChildren(el("section", { class: "reason" }, d.error)); return; }
    const keepOut = document.getElementById("action-out")?.textContent || "";
    const logPre = document.getElementById("log");
    const atBottom = !logPre || logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 20;
    const top = statusSection(d);
    const logSec = el("section", {}, el("h2", {}, "Runner log"), el("pre", { id: "log" }, log.lines.join("\n")));
    let att = document.getElementById("attempts-wrap");
    if (full || !att) {
      att = el("div", { id: "attempts-wrap" });
      api(`/api/p/${encodeURIComponent(name)}/attempts`).then((rows) => att.replaceChildren(attemptsSection(name, rows))).catch(() => {});
    }
    const showSetup = d.managed || !(d.setup && d.setup.spec_ready);
    const createSec = d.create && d.create.state !== "done"
      ? el("section", {}, el("h2", {}, `Creation: ${d.create.state}`), d.create.error ? el("p", { class: "reason" }, d.create.error) : null,
        el("pre", {}, d.create.lines.join("\n"))) : null;
    const content = el("div", { class: "content" }, ...[top, createSec,
      d.managed && !(d.plan && d.plan.state === "approved" && d.passed > 0) ? planSection(d) : null,
      showSetup ? setupSection(d) : null,
      envSection(name),
      el("div", { class: "grid2" }, stepsSection(d), metricsSection(d)), logSec,
      el("div", { class: "grid2" }, att, progressSection(d)), el("section", {}, viewerTitle, viewer)].filter(Boolean));
    $("#main").replaceChildren(el("div", { class: "layout" }, content, stepBar(d)));
    document.getElementById("action-out").textContent = keepOut;
    const np = document.getElementById("log");
    if (atBottom) np.scrollTop = np.scrollHeight;
    $("#clock").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $("#clock").textContent = `update failed: ${e.message}`;
  }
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
function meter(label, w, withDay) {
  if (!w) return el("div", { class: "meter muted" }, `${label}: no data`);
  const pct = w.used_pct;
  const lvl = pct === null || pct === undefined ? "" : pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok";
  const fill = el("span", { class: `fill ${lvl}` });
  fill.style.width = `${Math.min(100, Math.max(0, pct || 0))}%`;   // CSSOM, allowed by the CSP (style attrs are not)
  const bar = el("span", { class: "bar" }, fill);
  return el("div", { class: "meter", title: w.resets_at ? `resets ${new Date(w.resets_at * 1000).toLocaleString()}` : "" },
    el("strong", {}, label), " ", bar, " ", pct === null || pct === undefined ? "?" : `${Math.round(pct)}%`,
    w.resets_at ? el("span", { class: "muted" }, ` · resets ${resetLabel(w.resets_at, withDay)} (in ${until(w.resets_at)})`) : null);
}
async function loadUsage() {
  try {
    const u = await api("/api/usage");
    const stale = !u.at || Date.now() / 1000 - u.at > 3 * 3600;
    const btn = el("button", { class: "linkbtn", title: "Refresh usage (one tiny Haiku reply)",
      on: { click: async () => { try { await api("/api/usage/refresh", { method: "POST", headers: { "X-Hands": "1" } }); btn.textContent = "…"; } catch (e) { alert(e.message); } } } },
      u.refreshing ? "…" : "↻");
    $("#usage").replaceChildren(meter("Session", u.five_hour, false), meter("Week", u.seven_day, true),
      el("span", { class: stale ? "muted stale" : "muted" }, u.at ? `as of ${new Date(u.at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "no usage data yet"), btn);
  } catch (e) { /* keep last */ }
}

let ticks = 0;
function tick() {
  if (document.hidden) return;
  const a = document.activeElement;
  const typing = a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT") && !(a.closest && a.closest(".xterm"));
  if (typing) return;   // don't redraw while typing in a form (the terminal's own textarea is fine)
  if (sectionFromHash() === "sessions") { if (window.hands && window.hands.sessions) window.hands.sessions.poll(); ticks += 1; return; }
  if (sectionFromHash() === "files") { if (ticks % 4 === 0) loadUsage(); ticks += 1; return; }   // the file browser reloads on navigation, not on a timer
  loadProjects().catch(() => {});
  if (ticks % 4 === 0) loadUsage();
  ticks += 1;
  refresh(ticks % 8 === 0);   // attempts list reloads every ~30 s
}

// ---- app shell: sections (#/builder, #/sessions, #/files) and the phone drawer
const SECTIONS = { builder: "Project builder", sessions: "Session manager", files: "File browser" };
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
  if (sec === "files" && window.hands && window.hands.files) window.hands.files.show();
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
  if (window.hands && window.hands.files) window.hands.files.init();         // files.js too
  route();
}

document.addEventListener("DOMContentLoaded", async () => {
  initShell();
  let saved = null;
  try { saved = localStorage.getItem("hands.project"); } catch (e) { saved = null; }
  current = saved;
  await loadProjects().catch(() => {});
  loadUsage();
  if (current && current === saved) select(current);
  timer = setInterval(tick, 4000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
});
