// Mock of the Design files section (static data, no server calls): ?form=new|amend. Submit prints the requests
// the dashboard would send: one line per accepted row and one per style-pack group.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) if (k === "class") e.className = v; else e.setAttribute(k, v);
    e.append(...kids);
    return e;
  };
  const amend = new URLSearchParams(location.search).get("form") === "amend";
  const section = nf.designFilesSection(document);
  const copy = nf.copyFields(document);
  const out = h("pre", { class: "out", "data-testid": "mock-out" });
  const submit = h("button", { class: "act", type: "button", "data-testid": "design-files-submit" }, amend ? "Send change request" : "Create project");
  submit.addEventListener("click", () => {
    const blocked = copy.blocked() || section.blocked();
    if (blocked) { out.textContent = blocked; return; }
    const lines = nf.designSubmitLines(section.uploads());
    const si = amend && copy.empty() ? [] : [`POST /api/${amend ? "action/amend" : "new"} ${JSON.stringify({ site_inputs: copy.values() })}`];
    out.textContent = [...si, ...lines].join("\n") || "no design files";
  });
  const kids = [h("h2", {}, amend ? "Change request" : "New project")];
  if (amend) kids.push(h("div", { class: "flabel" }, "Style references (optional)"), nf.styleUrlList(document).root);
  kids.push(copy.root, section.root, h("div", { class: "row" }, submit), out);
  document.getElementById("main").replaceChildren(h("section", {}, ...kids));
});
