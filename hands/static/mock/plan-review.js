// Mock of the plan panel's design files list and its separate tick (static data, no server calls):
// ?design=changed|none. Approve needs the tick only when design files changed.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) if (k === "class") e.className = v; else e.setAttribute(k, v);
    e.append(...kids);
    return e;
  };
  const changed = new URLSearchParams(location.search).get("design") === "changed";
  const files = [
    { id: "d001", kind: "screenshot", file: "refs/design/d001/hero.png", note: "Take the layout of the hero", size: 12 * 1024, thumb: "thumb-1.png" },
    { id: "d002", kind: "icon-pack", file: "refs/design/d002/icons.svg", note: "Line icons", size: Math.round(1.2 * 1048576), thumb: "thumb-1.png" },
    { id: "d003", kind: "copy-tone", file: "refs/design/d003/tone.md", note: "<b>not bold</b>", size: 900 },
  ];
  const approve = h("button", { class: "act", type: "button", "data-testid": "plan-approve" }, "Approve & build");
  const kids = [h("h2", {}, "Plan"), h("p", {}, "12 steps")];
  if (changed) {
    const box = h("input", { type: "checkbox", id: "designack", "data-testid": "plan-design-ack" });
    const sync = () => { approve.disabled = !box.checked; };
    box.addEventListener("change", sync);
    approve.disabled = true;
    kids.push(h("div", { class: "warnbox" }, h("strong", {}, "This PR adds or changes design files."), nf.planDesignFiles(document, files),
      h("label", { for: "designack", class: "inline" }, box, " I reviewed the design files")));
  }
  const out = h("pre", { class: "out", "data-testid": "mock-out" });
  approve.addEventListener("click", () => { out.textContent = `POST /api/p/demo/action/approve ${JSON.stringify({ design_ack: changed })}`; });
  kids.push(h("div", { class: "row out" }, approve), out);
  document.getElementById("main").replaceChildren(h("section", {}, ...kids));
});
