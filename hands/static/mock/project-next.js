// Mock of the project page's style refs + setup panel (static data, no server calls).
//   project-next.html?refs=queued,frozen,failed&setup=failed[&framework=4]
// The panel is built by the real nextform.js and patched into #main with the real render.js, as app.js does.
// Other parameters (state, missing) are accepted and ignored here.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const next = nf.mockFromQuery(location.search);
  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.textContent = "landing";
  head.append(title);
  const panelTitle = document.createElement("h2");
  panelTitle.textContent = "Style refs & setup";
  const tree = document.createElement("div");
  tree.className = "layout";
  tree.append(head, panelTitle, nf.styleRefsPanel(document, next));
  const main = document.getElementById("main");
  const live = document.createElement("div");
  main.replaceChildren(live);
  window.hands.render.patch(live, tree);
});
