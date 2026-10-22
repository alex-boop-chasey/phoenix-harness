// Mock of the Session manager list (static data, no server calls): one plain session and one hidden planning
// chat of project `demo`, built with the same nextform.js helpers sessions.js uses; "Show all" lists the chat.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const rows = nf.mockSessions();
  const toggle = document.getElementById("s-show-all");
  function render() {
    const shown = nf.visibleSessions(rows, toggle.checked);
    const hidden = rows.length - shown.length;
    document.getElementById("s-count").textContent = `${rows.filter((r) => r.live).length} running${hidden ? ` · ${hidden} hidden` : ""}`;
    document.getElementById("slist").replaceChildren(...shown.map((r) => nf.sessionItem(document, r, {
      meta: `${r.cwd}${r.live ? "" : " · stopped"}`, when: r.live ? "live" : "1 d ago" })));
  }
  toggle.addEventListener("change", render);
  render();
});
