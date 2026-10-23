// Style references and project setup (Phase 2): the new-project form fields (style URL list, framework select,
// library checkboxes), the project page's style-refs panel, the planning chat panel (Plan with Claude, brief hash
// and approval), an attempt's tool usage and the Session manager rows. Pure helpers plus DOM builders that take the
// document, so the dashboard (app.js) and the static mock pages build the same nodes. Every URL, status and
// reason is set with textContent. Controls on the project page carry data-act for app.js's delegated
// dispatcher; the form fields own their own listeners (the form is rendered once, never patched).
// Browser IIFE that also exports for node --test (tools/js-test.sh).
"use strict";
(function (root) {
  const FRAMEWORK_LABELS = ["None", "Astro + Tailwind", "Astro + Tailwind + AI kit", "Next.js + Tailwind", "Next.js + Tailwind + AI kit"];
  const LIBS = [
    { id: "shadcn", label: "shadcn/ui", hint: "Component library, initialised with its defaults." },
    { id: "gsap", label: "GSAP motion stack", hint: "GSAP with ScrollTrigger and ScrollSmoother (+ @gsap/react on Next.js)." },
  ];
  const MAX_STYLE_URLS = 10;
  const MAX_URL = 2048;
  const REF_STATUSES = ["queued", "capturing", "analysing", "frozen", "failed"];
  const SETUP_STATUSES = ["none", "pending", "running", "done", "failed"];
  const REASONS = {
    empty: "Enter a URL.",
    "too long": `URLs are at most ${MAX_URL} characters.`,
    whitespace: "URLs cannot contain spaces or control characters.",
    scheme: "Only http:// and https:// URLs.",
    credentials: "No user name or password in the URL.",
    host: "The URL has no host.",
    invalid: "Not a valid URL.",
  };

  // The same rules as hands.stylerefs.validate_url (the server checks again): http(s) only, no credentials,
  // a host, at most 2048 characters. {ok, reason}; reason is one of the REASONS keys.
  function validateStyleUrl(url) {
    const bad = (reason) => ({ ok: false, reason });
    const s = typeof url === "string" ? url.trim() : "";
    if (!s) return bad("empty");
    if (s.length > MAX_URL) return bad("too long");
    if (/[\s\u0000-\u001f\u007f]/.test(s)) return bad("whitespace");
    const m = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(s);
    if (!m || !/^https?$/i.test(m[1])) return bad("scheme");
    const rest = s.slice(m[0].length);
    if (!rest.startsWith("//")) return bad("invalid");
    const authority = rest.slice(2).split(/[/?#]/)[0];
    if (authority.includes("@")) return bad("credentials");
    if (!authority || authority.startsWith(":")) return bad("host");
    let u;
    try { u = new URL(s); } catch (e) { return bad("invalid"); }
    if (u.username || u.password) return bad("credentials");
    if (!u.hostname) return bad("host");
    return { ok: true, reason: "" };
  }

  function styleUrlsFromForm(values) {
    return (values || []).filter((v) => typeof v === "string").map((v) => v.trim()).filter(Boolean);
  }

  const reasonText = (reason) => REASONS[reason] || REASONS.invalid;
  const frameworkLabel = (n) => FRAMEWORK_LABELS[Number(n) - 1] || "";

  // ---- planning chat: plan_session {session_id, status: none|open|brief_ready|approved, brief_sha256}
  const PLAN_STATUSES = ["none", "open", "brief_ready", "approved"];
  const SHA_RE = /^[0-9a-f]{64}$/;
  const planStatus = (ps) => (ps && ps.session_id && PLAN_STATUSES.includes(ps.status) ? ps.status : "none");
  const planChatLabel = (status) => (!status || status === "none" ? "Plan with Claude" : "Resume planning chat");
  // The New project form's second submit button: it creates the project and then opens the planning chat.
  const CREATE_PLAN_LABEL = "Create and plan with Claude";
  const CREATE_PLAN_HINT = "Creates the project, then opens the planning chat";
  // Whether that chat should be opened now: once only, and only once the project really exists.
  const shouldOpenPlanChat = (s) => !!(s && s.chat === true && s.done === true && s.started !== true);
  const fullSha = (sha) => { const s = typeof sha === "string" ? sha.trim().toLowerCase() : ""; return SHA_RE.test(s) ? s : ""; };
  const shortSha = (sha) => fullSha(sha).slice(0, 12);

  // ---- tool usage (tools.json from hands.toolusage): {parts: [[label, text]], missing} or null when empty
  const counts = (o) => Object.entries(o && typeof o === "object" ? o : {}).map(([k, v]) => `${k}×${v}`).join(", ");
  const names = (a) => (Array.isArray(a) ? a.filter((x) => typeof x === "string") : []);
  function toolUsageParts(t) {
    if (!t || typeof t !== "object") return null;
    const parts = [["Skills", names(t.skills).join(", ")], ["Tools", counts(t.tools)], ["MCP", counts(t.mcp)],
      ["Plugins", names(t.plugins).join(", ")]].filter(([, v]) => v);
    const missing = names(t.missing);
    return parts.length || missing.length ? { parts, missing } : null;
  }

  // ---- Session manager: hidden sessions (project planning chats) are listed only with "show all" on, or while
  // they are the selected session (the project page links straight to its chat).
  const visibleSessions = (rows, showAll, keepId) =>
    (Array.isArray(rows) ? rows : []).filter((r) => r && (showAll || r.hidden !== true || (!!keepId && r.id === keepId)));

  // Static data for mock/project-next.html: ?refs=<comma statuses>&setup=<status>[&framework=<1-5>]
  // [&state=<plan_session status>]. Unknown statuses are dropped.
  const MOCK_SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
  function mockFromQuery(search) {
    const q = new URLSearchParams(search || "");
    const statuses = (q.get("refs") || "").split(",").map((x) => x.trim()).filter((x) => REF_STATUSES.includes(x));
    const style_refs = statuses.map((status, i) => ({
      index: i + 1, url: `https://ref-${i + 1}.example.com/`, added: "2026-01-01T00:00:00Z", status,
      reason: status === "failed" ? "analyst token missing" : null,
    }));
    const status = SETUP_STATUSES.includes(q.get("setup")) ? q.get("setup") : "none";
    const fw = Number(q.get("framework")) || (status === "none" ? 1 : 2);
    const ps = PLAN_STATUSES.includes(q.get("state")) ? q.get("state") : "none";
    const plan_session = ps === "none" ? { session_id: null, status: "none", brief_sha256: null, updated: null }
      : { session_id: "4b1f2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d", status: ps, brief_sha256: ps === "open" ? null : MOCK_SHA,
        updated: "2026-01-01T00:00:00Z" };
    return { style_refs, setup: { framework: fw, libs: [], status, label: frameworkLabel(fw) }, plan_session, gate: [] };
  }
  // The mock attempt's fixed tool usage; ?missing=1 adds a skipped required skill.
  function mockToolUsage(search) {
    const q = new URLSearchParams(search || "");
    return { skills: ["test-driven-development"], tools: { Read: 12, Edit: 4 }, mcp: {}, plugins: [],
      missing: q.get("missing") === "1" ? ["verification-before-completion"] : [] };
  }
  // mock/sessions-next.html: one plain session and one hidden planning chat of project `demo`.
  function mockSessions() {
    return [
      { id: "0f3c1a2b-0000-4000-8000-000000000001", title: "shell", cwd: "~/work", live: true, hidden: false, project: "", role: "" },
      { id: "4b1f2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d", title: "Planning chat", cwd: "~/.local/state/hands/demo/plan-chat/brief",
        live: false, hidden: true, project: "demo", role: "plan-chat" },
    ];
  }

  // ---- DOM builders
  function h(doc, tag, attrs, ...kids) {
    const e = doc.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
      else e.setAttribute(k, v);
    }
    for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined) e.append(typeof k === "object" ? k : String(k));
    return e;
  }

  // The style URL list: one field at start, "+" adds a row (disabled at MAX_STYLE_URLS), × removes one (the
  // last row is cleared instead). A field is checked when it loses focus, and again on every keystroke while
  // it shows an error. {root, values(), validate() -> all non-empty fields valid}.
  function styleUrlList(doc, opts) {
    const o = opts || {};
    const rows = h(doc, "div", { class: "urlrows" });
    const add = h(doc, "button", { class: "btn sm", type: "button", "data-testid": "style-url-add", "aria-label": "Add another style URL" }, "+");
    const sync = () => { add.disabled = rows.children.length >= MAX_STYLE_URLS; };
    function check(row) {
      const input = row.querySelector("input");
      const old = row.querySelector(".urlerr");
      const v = input.value.trim();
      const r = v ? validateStyleUrl(v) : { ok: true, reason: "" };
      if (old) old.remove();
      input.setAttribute("aria-invalid", r.ok ? "false" : "true");
      if (!r.ok) row.append(h(doc, "p", { class: "reason small urlerr", "data-testid": "style-url-error", role: "alert" }, reasonText(r.reason)));
      return r.ok;
    }
    function addRow(value) {
      if (rows.children.length >= MAX_STYLE_URLS) return null;
      const row = h(doc, "div", { class: "urlrow" });
      const input = h(doc, "input", { type: "text", inputmode: "url", class: "input", autocomplete: "off", spellcheck: "false",
        maxlength: String(MAX_URL + 1), placeholder: o.placeholder || "https://example.com/", "data-testid": "style-url",
        "aria-label": "Style reference URL", on: {
          blur: () => check(row),
          input: () => { if (row.querySelector(".urlerr")) check(row); },
        } });
      if (value) input.value = value;
      const rm = h(doc, "button", { class: "btn quiet sm", type: "button", "data-testid": "style-url-remove", "aria-label": "Remove this style URL",
        on: { click: () => {
          if (rows.children.length > 1) row.remove();
          else { input.value = ""; check(row); }
          sync();
        } } }, "×");
      row.append(h(doc, "div", { class: "urlfield" }, input, rm));
      rows.append(row);
      sync();
      return input;
    }
    add.addEventListener("click", () => { const i = addRow(""); if (i) i.focus(); });
    addRow("");
    const values = () => styleUrlsFromForm([...rows.querySelectorAll("input")].map((i) => i.value));
    const validate = () => [...rows.children].map(check).every(Boolean);
    const reset = () => { rows.replaceChildren(); addRow(""); };
    return { root: h(doc, "div", { class: "urllist" }, rows, h(doc, "div", { class: "row" }, add)), values, validate, reset };
  }

  // Framework select (values 1-5) and the library checkboxes (independent of the framework).
  function setupFields(doc) {
    const select = h(doc, "select", { id: "framework", class: "input", "data-testid": "framework" },
      FRAMEWORK_LABELS.map((label, i) => h(doc, "option", { value: String(i + 1) }, label)));
    const boxes = LIBS.map((lib) => {
      const box = h(doc, "input", { type: "checkbox", id: `lib-${lib.id}`, "data-testid": `lib-${lib.id}`, value: lib.id });
      return { lib, box, label: h(doc, "label", { class: "check" }, box, h(doc, "span", {}, lib.label, h(doc, "span", { class: "hint" }, lib.hint))) };
    });
    const root = h(doc, "div", { class: "setupfields" },
      h(doc, "label", { for: "framework" }, "Framework"), select,
      h(doc, "p", { class: "muted" }, "Scaffolded into the app repo before the build starts, with build, lint, typecheck and format scripts. None leaves the repo empty."),
      h(doc, "div", { class: "flabel" }, "Libraries"), boxes.map((b) => b.label));
    return { root, framework: () => Number(select.value), libs: () => boxes.filter((b) => b.box.checked).map((b) => b.lib.id) };
  }

  // The project page's style refs + setup panel from the project detail's `next` ({style_refs, setup}).
  // `addForm` (optional) is the add-URL form node app.js keeps across patches.
  function styleRefsPanel(doc, next, addForm) {
    const n = next || {};
    const refs = Array.isArray(n.style_refs) ? n.style_refs : [];
    const btn = (label, act, arg, testid, cls) => h(doc, "button", { class: cls || "btn sm", type: "button", "data-act": act,
      "data-arg": String(arg), "data-testid": testid }, label);
    const rows = refs.map((r) => {
      const failed = r.status === "failed";
      return h(doc, "li", { class: "refrow", "data-key": `ref:${r.index}`, "data-testid": "style-ref-row" },
        h(doc, "span", { class: "mono faint" }, String(r.index).padStart(2, "0")),
        h(doc, "span", { class: "mono refurl" }, r.url || ""),
        h(doc, "span", { class: `badge s-${String(r.status || "").replace(/[^a-z_]/g, "")}`, "data-testid": "style-ref-status" }, r.status || "?"),
        failed ? btn("Retry", "style-ref-retry", r.index, "style-ref-retry") : null,
        failed ? btn("Remove", "style-ref-remove", r.index, "style-ref-remove", "btn sm danger") : null,
        failed && r.reason ? h(doc, "p", { class: "reason small refreason" }, r.reason) : null);
    });
    const full = refs.length >= MAX_STYLE_URLS;
    const s = n.setup || {};
    const status = s.status || "none";
    const label = s.label || frameworkLabel(s.framework) || "None";
    const libs = Array.isArray(s.libs) && s.libs.length ? ` + ${s.libs.join(", ")}` : "";
    return h(doc, "section", { class: "stylerefs", "data-key": "style-refs", "data-testid": "style-refs-panel" },
      h(doc, "p", { class: "muted" }, "Sites whose look and motion the build follows. Each URL is captured, analysed and frozen into the spec repo before planning; text, images and logos are never copied."),
      rows.length ? h(doc, "ul", { class: "refrows" }, rows) : h(doc, "p", { class: "muted small" }, "No style references."),
      h(doc, "div", { class: "row" }, h(doc, "button", { class: "btn sm", type: "button", "data-act": "style-ref-add", "data-testid": "add-style-url",
        ...(full ? { disabled: "" } : {}) }, "Add style URL"),
      full ? h(doc, "span", { class: "muted small" }, `at most ${MAX_STYLE_URLS} per project`) : null),
      addForm || null,
      h(doc, "div", { class: "row setupline", "data-key": "setup-line" },
        h(doc, "span", { "data-testid": "setup-status" }, "Setup: ", h(doc, "strong", {}, label + libs), " · ",
          h(doc, "span", { class: `badge s-${status.replace(/[^a-z_]/g, "")}` }, status)),
        status === "failed" ? btn("Retry setup", "setup-retry", "", "setup-retry") : null));
  }

  // The project page's planning chat panel: the plan-chat button (data-act plan-chat); with a ready brief its short
  // hash and the approve button, which carries the full hash behind the short one it shows (data-act brief-approve);
  // once approved a badge instead of the button.
  function planChatPanel(doc, slug, ps) {
    const status = planStatus(ps);
    const sha = fullSha(ps && ps.brief_sha256);
    const short = sha.slice(0, 12);
    let brief = null;
    if (status === "brief_ready") {
      brief = sha
        ? h(doc, "div", { class: "row briefline", "data-key": "brief-line" },
          h(doc, "span", {}, "Brief ready · ", h(doc, "code", { class: "mono", "data-testid": "brief-sha", title: sha }, short)),
          h(doc, "button", { class: "btn sm primary", type: "button", "data-act": "brief-approve", "data-arg": sha,
            "data-testid": "brief-approve" }, "Approve brief"))
        : h(doc, "p", { class: "muted small" }, "The brief is written but its hash could not be read; refresh the page.");
    } else if (status === "approved") {
      brief = h(doc, "div", { class: "row briefline", "data-key": "brief-line" },
        h(doc, "span", { class: "badge s-done", "data-testid": "brief-approved", title: sha }, short ? `brief approved · ${short}` : "brief approved"));
    }
    const hint = {
      none: "Talk the idea through with Claude: it asks one question at a time and writes the brief.",
      open: "The chat is open. When Claude has written brief.md and coverage.md, the brief shows up here for approval.",
      brief_ready: "Read the brief in the chat, then approve exactly this version (the hash below) into the spec repo.",
      approved: "The brief is in the spec repo. Resume the chat for a change round; press Start to plan as usual.",
    }[status];
    return h(doc, "section", { class: "planchat", "data-key": "plan-chat", "data-testid": "plan-chat-panel" },
      h(doc, "p", { class: "muted" }, hint),
      h(doc, "div", { class: "row" }, h(doc, "button", { class: "btn sm", type: "button", "data-act": "plan-chat", "data-arg": slug,
        "data-testid": "plan-chat-button" }, planChatLabel(status))),
      brief);
  }

  // An attempt's tool usage line (Skills, Tools, MCP, Plugins) and a warning badge naming the required skills the
  // session did not use. Null when tools.json is absent or empty. All names are agent text: textContent only.
  function toolUsage(doc, t) {
    const u = toolUsageParts(t);
    if (!u) return null;
    return h(doc, "div", { class: "tool-usage small", "data-testid": "tool-usage" },
      u.parts.map(([k, v]) => h(doc, "span", { class: "tool-usage-part" }, h(doc, "span", { class: "muted" }, `${k}: `), v)),
      u.missing.length ? h(doc, "span", { class: "badge s-failed", title: "required skills not used", "data-testid": "missing-skills" },
        `missing: ${u.missing.join(", ")}`) : null);
  }

  // One Session manager list row. opts: {meta, when, current, onClick}; project-linked rows name the project.
  function sessionItem(doc, r, opts) {
    const o = opts || {};
    const name = r.title || "New session";
    return h(doc, "button", { class: "sitem", type: "button", role: "listitem", "data-id": r.id, "data-testid": "session-row",
      title: `${name} · ${r.cwd || ""}`, "aria-current": String(!!o.current), ...(o.onClick ? { on: { click: o.onClick } } : {}) },
      h(doc, "span", { class: "dot" + (r.live ? " live" : ""), "aria-hidden": "true" }),
      h(doc, "span", { class: "smain" }, h(doc, "span", { class: "sname" }, name,
        r.project ? h(doc, "span", { class: "badge s-project", "data-testid": "session-project", title: `planning chat of ${r.project}` }, r.project) : null,
        r.telegram ? h(doc, "span", { class: "badge s-telegram" }, "Telegram") : null),
      h(doc, "span", { class: "smeta" }, o.meta || "")),
      h(doc, "span", { class: "when" + (r.live ? " live" : "") }, o.when || ""));
  }

  const api = { FRAMEWORK_LABELS, LIBS, MAX_STYLE_URLS, MAX_URL, REF_STATUSES, SETUP_STATUSES, PLAN_STATUSES,
    validateStyleUrl, styleUrlsFromForm, reasonText, frameworkLabel, mockFromQuery, mockToolUsage, mockSessions,
    planStatus, planChatLabel, shortSha, toolUsageParts, visibleSessions,
    CREATE_PLAN_LABEL, CREATE_PLAN_HINT, shouldOpenPlanChat,
    styleUrlList, setupFields, styleRefsPanel, planChatPanel, toolUsage, sessionItem };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) {
    root.hands = root.hands || {};
    root.hands.nextform = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
