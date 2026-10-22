// Mock of the project page's Phase 2 panels (static data, no server calls).
//   project-next.html?refs=queued,frozen,failed&setup=failed[&framework=4][&state=none|open|brief_ready|approved][&missing=1]
// The panels are built by the real nextform.js and patched into #main with the real render.js, as app.js does:
// planning chat (state), style refs + setup (refs, setup, framework) and one attempt with tool usage (missing).
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const next = nf.mockFromQuery(location.search);
  const h = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  };
  const head = h("div", "page-head");
  head.append(h("h1", "", "landing"));
  const line = h("div", "row");
  line.append(h("strong", "", "step-03-a1"), h("span", "badge s-passed", "passed"));
  const attempt = h("div", "attempt");
  attempt.append(line);
  const usage = nf.toolUsage(document, nf.mockToolUsage(location.search));
  if (usage) attempt.append(usage);
  const tree = h("div", "layout");
  tree.append(head, h("h2", "", "Plan with Claude"), nf.planChatPanel(document, "landing", next.plan_session),
    h("h2", "", "Style refs & setup"), nf.styleRefsPanel(document, next), h("h2", "", "Attempts"), attempt);
  const main = document.getElementById("main");
  const live = document.createElement("div");
  main.replaceChildren(live);
  window.hands.render.patch(live, tree);
});
