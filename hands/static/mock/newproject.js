// Mock of the new-project form (static data, no server calls): the real nextform.js builders, as app.js's
// showNew() uses them. Submitting shows the request body the dashboard would send.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) if (k === "class") e.className = v; else e.setAttribute(k, v);
    e.append(...kids);
    return e;
  };
  const urls = nf.styleUrlList(document);
  const setup = nf.setupFields(document);
  const copy = nf.copyFields(document);
  const out = h("pre", { class: "out", "data-testid": "mock-out" });
  const form = h("form", {},
    h("label", { for: "slug" }, "Project slug"), h("input", { id: "slug", value: "landing", autocomplete: "off" }),
    h("label", { for: "desc" }, "Idea"), h("textarea", { id: "desc", rows: "3" }),
    h("div", { class: "flabel" }, "Style references (optional)"),
    h("p", { class: "muted" }, "Sites whose look and motion the app should follow. Up to 10 http(s) URLs."), urls.root,
    setup.root, copy.root,
    h("div", { class: "row" }, h("button", { class: "act", type: "submit", "data-testid": "create-project" }, "Create project"),
      h("button", { class: "btn primary", type: "submit", "data-testid": "plan-with-claude", value: "chat" },
        "Plan with Claude")),
    out);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!urls.validate()) { out.textContent = "Fix the style URLs first."; return; }
    if (copy.blocked()) { out.textContent = copy.blocked(); return; }
    const body = { slug: form.querySelector("#slug").value, style_refs: urls.values(), framework: setup.framework(), libs: setup.libs(), site_inputs: copy.values(),
      plan_with_claude: !!(ev.submitter && ev.submitter.value === "chat") };
    out.textContent = `mock: POST /api/new ${JSON.stringify(body)}`;
  });
  document.getElementById("main").replaceChildren(h("section", {}, h("h2", {}, "New project"), form));
});
