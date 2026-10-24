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

  // ---- design files (docs/design.md "Design files"): the same kinds, caps, kind guesses and refusal texts as
  // hands.designfiles; the server checks every file again. File names and notes are only ever set with textContent.
  const MIB = 1048576;
  const DESIGN_KINDS = ["design-md", "style-pack", "screenshot", "brand", "avoid", "font", "icon-pack", "video", "copy-tone", "feature-list"];
  const DESIGN_EXT_CLASS = { ".md": "text", ".json": "text", ".css": "text", ".txt": "text", ".png": "image", ".jpg": "image",
    ".jpeg": "image", ".webp": "image", ".svg": "image", ".woff2": "font", ".ttf": "font", ".otf": "font", ".zip": "zip",
    ".mp4": "video", ".webm": "video" };
  const DESIGN_ACCEPT = [...Object.keys(DESIGN_EXT_CLASS), "image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm"].join(",");
  const DESIGN_CAPS = { text: MIB, image: 15 * MIB, font: 5 * MIB, zip: 25 * MIB, video: 50 * MIB, submit: 200 * MIB };
  const DESIGN_HINTS = { ".heic": " (HEIC: share the photo as JPEG)", ".heif": " (HEIC: share the photo as JPEG)", ".mov": " (MOV: export as MP4)" };
  const DESIGN_TEXT_EXT = [".md", ".json", ".css", ".txt"];
  const DESIGN_THUMB_EXT = [".png", ".jpg", ".jpeg", ".webp", ".svg"];
  const MAX_NOTE = 500;
  const MAX_DESIGN_FILES = 50;
  const extOf = (name) => { const m = /\.[^./\\]*$/.exec(String(name || "")); return m ? m[0].toLowerCase() : ""; };

  function designKindGuess(name) {
    const low = String(name || "").toLowerCase();
    const ext = extOf(low);
    const cls = DESIGN_EXT_CLASS[ext];
    if (low.includes("tone") || low.includes("voice")) return "copy-tone";
    if (low.includes("feature")) return "feature-list";
    if (low.includes("avoid") || low.includes("dont")) return "avoid";
    if (ext === ".md") return "design-md";
    if (ext === ".json" || ext === ".css") return "style-pack";
    if (ext === ".txt") return "copy-tone";
    if (cls === "image" && ext !== ".svg") return "screenshot";
    if (ext === ".svg") return low.includes("logo") || low.includes("brand") ? "brand" : "icon-pack";
    if (cls === "font") return "font";
    if (ext === ".zip") return "icon-pack";
    if (cls === "video") return "video";
    return "design-md";
  }

  // null when the file may be uploaded, else the reason shown on its row.
  function designFileCheck(name, size, caps) {
    const ext = extOf(name);
    const cls = DESIGN_EXT_CLASS[ext];
    if (!cls) return "type not allowed" + (DESIGN_HINTS[ext] || "");
    const limit = { ...DESIGN_CAPS, ...(caps || {}) }[cls];
    return Number(size) > limit ? "too large" : null;
  }

  function designTotalCheck(sizes, cap) {
    const limit = cap || DESIGN_CAPS.submit;
    const total = (sizes || []).reduce((a, b) => a + (Number(b) || 0), 0);
    return total > limit ? `files total over ${Math.round(limit / MIB)} MB` : null;
  }

  function designSize(n) {
    const v = Number(n) || 0;
    if (v < 1024) return `${v} bytes`;
    if (v < MIB) return `${Math.round(v / 1024)} KB`;
    return `${(Math.round((v / MIB) * 10) / 10).toString()} MB`;
  }

  // One line per accepted plain row and one per group, as the mock pages print them.
  function designSubmitLines(ups) {
    const lines = [], groups = new Map();
    for (const u of ups || []) {
      if (u.group) { if (!groups.has(u.group)) groups.set(u.group, []); groups.get(u.group).push(u.name); }
      else lines.push(`upload ${u.name} ${u.kind} ${u.size}`);
    }
    for (const names of groups.values()) lines.push(`group ${names.join(" ")}`);
    return lines;
  }

  function designUploadUrl(slug, u, batch) {
    const q = [["name", u.name], ["kind", u.kind], ["note", u.note || ""], ["batch", batch]];
    if (u.group) q.push(["group", u.group], ["group_n", String(u.group_n)]);
    return `/api/p/${encodeURIComponent(slug)}/design-files/upload?` + q.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  }

  const changedDesignFiles = (files, ids) => (Array.isArray(files) ? files : []).filter((f) => f && (ids || []).includes(f.id));

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

  // The Design files section. opts: {caps}. rows() describes every row; uploads() lists the files to send:
  // {file, name, kind, note, size, group?, group_n?} (refused and empty rows are left out; the members of a grouped
  // row share a group marker and the server merges them into one style-pack entry). setStatus(row, text, bad).
  function designFilesSection(doc, opts) {
    const o = opts || {};
    const state = [];                       // {id, members: [File], name, kind, note, grouped, tr nodes...}
    let seq = 0, groupSeq = 0;
    const list = h(doc, "div", { class: "designrows", "data-testid": "design-files-rows" });
    const totalErr = h(doc, "p", { class: "reason small", role: "alert", "data-testid": "design-files-total-error" });
    totalErr.hidden = true;
    const input = h(doc, "input", { type: "file", multiple: "", accept: DESIGN_ACCEPT, class: "visually-hidden", tabindex: "-1",
      "data-testid": "design-files-input", "aria-label": "Design files" });
    const addButton = h(doc, "button", { class: "btn", type: "button", "data-testid": "design-files-add-button" }, "Add files");
    const plus = h(doc, "button", { class: "btn quiet", type: "button", "data-testid": "design-file-add", "aria-label": "Add an empty design file row" }, "+");
    const groupBtn = h(doc, "button", { class: "btn", type: "button", disabled: "", "data-testid": "design-files-group" }, "Group as style pack");
    const drop = h(doc, "div", { class: "designdrop", "data-testid": "design-files-drop" }, "Drop files here");

    const members = (r) => r.members.length;
    const sizeOf = (r) => r.members.reduce((a, f) => a + f.size, 0);
    function refresh() {
      const picked = state.filter((r) => r.select && r.select.checked);
      groupBtn.disabled = picked.length < 2;
      const sizes = state.filter((r) => !r.error).map(sizeOf);
      const bad = designTotalCheck(sizes, (o.caps || {}).submit);
      totalErr.hidden = !bad;
      totalErr.textContent = bad || "";
    }
    function paint(r) {
      r.nameEl.textContent = r.grouped ? `style pack (${members(r)} files)` : (r.members[0] ? r.members[0].name : "");
      r.nameEl.title = r.members.map((f) => f.name).join(" ");
      r.sizeEl.textContent = r.members.length ? designSize(sizeOf(r)) : "";
      r.pick.hidden = members(r) > 0;
      r.errBox.replaceChildren();
      if (r.error) r.errBox.append(h(doc, "p", { class: "reason small", role: "alert", "data-testid": "design-file-error" }, r.error));
      const selectable = !r.error && !r.grouped && members(r) === 1 && DESIGN_TEXT_EXT.includes(extOf(r.members[0].name));
      if (selectable) r.selWrap.append(r.select);
      else { r.select.checked = false; r.select.remove(); }
    }
    function addRow(file) {
      const r = { id: ++seq, members: [], kind: "design-md", grouped: false, error: "" };
      r.nameEl = h(doc, "span", { class: "mono designname", "data-testid": "design-file-name" });
      r.sizeEl = h(doc, "span", { class: "muted small", "data-testid": "design-file-size" });
      r.statusEl = h(doc, "span", { class: "small", "data-testid": "design-file-status" });
      r.errBox = h(doc, "div", {});
      r.kindSel = h(doc, "select", { class: "input", "data-testid": "design-file-kind", "aria-label": "Kind" },
        DESIGN_KINDS.map((k) => h(doc, "option", { value: k }, k)));
      r.noteEl = h(doc, "input", { type: "text", class: "input", maxlength: String(MAX_NOTE), placeholder: "Note (what to take from it)",
        "data-testid": "design-file-note", "aria-label": "Note" });
      r.select = h(doc, "input", { type: "checkbox", "data-testid": "design-file-select", "aria-label": "Select for style pack",
        on: { change: refresh } });
      r.selWrap = h(doc, "label", { class: "designsel" });
      r.pick = h(doc, "input", { type: "file", accept: DESIGN_ACCEPT, "data-testid": "design-file-pick", "aria-label": "Choose a file",
        on: { change: () => { const f = r.pick.files && r.pick.files[0]; if (f) setFile(r, f); } } });
      const rm = h(doc, "button", { class: "btn quiet", type: "button", "data-testid": "design-file-remove", "aria-label": "Remove this file",
        on: { click: () => { state.splice(state.indexOf(r), 1); r.row.remove(); refresh(); } } }, "✕");
      r.row = h(doc, "div", { class: "designrow", "data-testid": "design-file-row" },
        h(doc, "div", { class: "designmain" }, r.selWrap, r.nameEl, r.pick, r.sizeEl, r.statusEl, rm),
        h(doc, "div", { class: "designmeta" }, r.kindSel, r.noteEl), r.errBox);
      state.push(r);
      list.append(r.row);
      if (file) setFile(r, file); else paint(r);
      refresh();
      return r;
    }
    function setFile(r, f) {
      r.members = [f];
      r.kindSel.value = designKindGuess(f.name);
      r.error = designFileCheck(f.name, f.size, o.caps) || "";
      paint(r);
      refresh();
    }
    const addFiles = (files) => { for (const f of Array.from(files || [])) if (state.length < MAX_DESIGN_FILES) addRow(f); };
    input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
    addButton.addEventListener("click", () => input.click());
    plus.addEventListener("click", () => { if (state.length < MAX_DESIGN_FILES) addRow(null); });
    for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); addFiles(e.dataTransfer && e.dataTransfer.files); });
    groupBtn.addEventListener("click", () => {
      const picked = state.filter((r) => r.select && r.select.checked);
      if (picked.length < 2) return;
      const first = picked[0];
      first.members = picked.flatMap((r) => r.members);
      first.grouped = true;
      first.kindSel.value = "style-pack";
      for (const r of picked.slice(1)) { state.splice(state.indexOf(r), 1); r.row.remove(); }
      paint(first);
      refresh();
    });

    const root = h(doc, "section", { class: "designfiles envedit", "data-testid": "design-files" },
      h(doc, "div", { class: "flabel" }, "Design files (optional)"),
      h(doc, "p", { class: "muted" }, "Screenshots, DESIGN.md, tokens, fonts, icons, brand files and short videos the build should follow. Each file becomes a row; the kind is guessed and can be changed."),
      h(doc, "div", { class: "row" }, addButton, plus, groupBtn), input, drop, list, totalErr);
    return {
      root,
      rows: () => state.map((r) => ({ id: r.id, name: r.nameEl.textContent, kind: r.kindSel.value, note: r.noteEl.value, size: sizeOf(r),
        files: r.members.slice(), grouped: r.grouped, error: r.error })),
      uploads() {
        const out = [];
        if (!totalErr.hidden) return out;
        for (const r of state) {
          if (r.error || !r.members.length) continue;
          const g = r.grouped ? `g${Date.now().toString(36)}${++groupSeq}` : "";
          for (const f of r.members) {
            out.push({ file: f, name: f.name, kind: r.kindSel.value, note: r.noteEl.value.slice(0, MAX_NOTE), size: f.size, row: r.id,
              ...(g ? { group: g, group_n: r.members.length } : {}) });
          }
        }
        return out;
      },
      blocked: () => (totalErr.hidden ? "" : totalErr.textContent),
      setStatus(rowId, text, bad) {
        const r = state.find((x) => x.id === rowId);
        if (r) { r.statusEl.textContent = text; r.statusEl.className = bad ? "small reason" : "small muted"; }
      },
    };
  }

  // The plan panel's list of design files the PR adds or changes: PNG thumbnail (only for image files; `thumb` is the
  // PNG route and an SVG URL is never used), kind, note (text), size.
  function planDesignFiles(doc, files) {
    const rows = (Array.isArray(files) ? files : []).map((f) => {
      const thumb = typeof f.thumb === "string" && !/\.svg(\?|$)/i.test(f.thumb) && DESIGN_THUMB_EXT.includes(extOf(f.file)) ? f.thumb : "";
      return h(doc, "li", { class: "plandesign", "data-testid": "plan-design-file" },
        thumb ? h(doc, "img", { class: "designthumb", src: thumb, alt: "", width: "80", height: "80", loading: "lazy", "data-testid": "design-file-thumb" }) : null,
        h(doc, "div", { class: "plandesigntext" },
          h(doc, "div", {}, h(doc, "span", { class: "mono" }, f.file || f.id || ""), " ", h(doc, "span", { class: "badge" }, f.kind || "")),
          h(doc, "div", { class: "muted small" }, designSize(f.size)),
          f.note ? h(doc, "p", { class: "goal" }, f.note) : null));
    });
    return h(doc, "ul", { class: "plandesignlist", "data-testid": "plan-design-files" }, rows);
  }

  const api = { FRAMEWORK_LABELS, LIBS, MAX_STYLE_URLS, MAX_URL, REF_STATUSES, SETUP_STATUSES, PLAN_STATUSES,
    validateStyleUrl, styleUrlsFromForm, reasonText, frameworkLabel, mockFromQuery, mockToolUsage, mockSessions,
    planStatus, planChatLabel, shortSha, toolUsageParts, visibleSessions,
    CREATE_PLAN_LABEL, CREATE_PLAN_HINT, shouldOpenPlanChat,
    styleUrlList, setupFields, styleRefsPanel, planChatPanel, toolUsage, sessionItem,
    DESIGN_KINDS, DESIGN_ACCEPT, designKindGuess, designFileCheck, designTotalCheck, designSize, designSubmitLines, designUploadUrl,
    changedDesignFiles, designFilesSection, planDesignFiles };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) {
    root.hands = root.hands || {};
    root.hands.nextform = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
