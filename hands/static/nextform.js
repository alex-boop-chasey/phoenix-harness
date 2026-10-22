// Style references and project setup (Phase 2): the new-project form fields (style URL list, framework select,
// library checkboxes) and the project page's style-refs panel. Pure helpers plus DOM builders that take the
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

  // Static data for mock/project-next.html: ?refs=<comma statuses>&setup=<status>[&framework=<1-5>]. Unknown
  // statuses are dropped; other parameters (state, missing: rendered by later pages) are ignored here.
  function mockFromQuery(search) {
    const q = new URLSearchParams(search || "");
    const statuses = (q.get("refs") || "").split(",").map((x) => x.trim()).filter((x) => REF_STATUSES.includes(x));
    const style_refs = statuses.map((status, i) => ({
      index: i + 1, url: `https://ref-${i + 1}.example.com/`, added: "2026-01-01T00:00:00Z", status,
      reason: status === "failed" ? "analyst token missing" : null,
    }));
    const status = SETUP_STATUSES.includes(q.get("setup")) ? q.get("setup") : "none";
    const fw = Number(q.get("framework")) || (status === "none" ? 1 : 2);
    return { style_refs, setup: { framework: fw, libs: [], status, label: frameworkLabel(fw) }, plan_session: null, gate: [] };
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

  const api = { FRAMEWORK_LABELS, LIBS, MAX_STYLE_URLS, MAX_URL, REF_STATUSES, SETUP_STATUSES,
    validateStyleUrl, styleUrlsFromForm, reasonText, frameworkLabel, mockFromQuery, styleUrlList, setupFields, styleRefsPanel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) {
    root.hands = root.hands || {};
    root.hands.nextform = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
