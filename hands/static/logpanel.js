// Runner log panel (0.6): appends only the new tail of the log (store.logAppend) instead of rewriting the
// whole <pre> on every update, keeps at most MAX_LINES lines in the DOM, and scrolls to the bottom only
// when it already was there (within 20 px) or when Follow is on. The panel owns its node
// (data-self-managed): the page patch never touches it. Log text is agent-influenced: textContent only.
"use strict";
(function (root) {
  const MAX_LINES = 2000;
  const NEAR_BOTTOM = 20;

  function trimLines(lines, max) {
    return lines.length > max ? lines.slice(lines.length - max) : lines;
  }
  function shouldFollow(scrollTop, clientHeight, scrollHeight, forced) {
    return !!forced || scrollTop + clientHeight >= scrollHeight - NEAR_BOTTOM;
  }

  function create(doc, logAppend) {
    let shown = [];
    let forced = false;
    const pre = doc.createElement("pre");
    pre.id = "log";
    const follow = doc.createElement("button");
    follow.type = "button"; follow.className = "btn sm"; follow.textContent = "Follow";
    follow.setAttribute("aria-pressed", "false");
    follow.title = "Keep the newest lines in view";
    const copy = doc.createElement("button");
    copy.type = "button"; copy.className = "btn sm quiet text"; copy.textContent = "Copy";
    copy.title = "Copy the shown log lines";
    const h2 = doc.createElement("h2"); h2.textContent = "Runner log";
    const head = doc.createElement("div"); head.className = "row loghead";
    head.append(h2, follow, copy);
    const section = doc.createElement("section");
    section.setAttribute("data-key", "log");
    section.setAttribute("data-self-managed", "");
    section.append(head, pre);

    follow.addEventListener("click", () => {
      forced = !forced;
      follow.setAttribute("aria-pressed", String(forced));
      if (forced) pre.scrollTop = pre.scrollHeight;
    });
    copy.addEventListener("click", () => {
      const nav = root.navigator;
      if (nav && nav.clipboard && nav.clipboard.writeText) nav.clipboard.writeText(pre.textContent).catch(() => {});
    });

    function update(lines) {
      lines = Array.isArray(lines) ? lines : [];
      const stick = shouldFollow(pre.scrollTop, pre.clientHeight, pre.scrollHeight, forced);
      const { append, reset } = logAppend(shown, lines);
      if (reset) {
        shown = trimLines(lines, MAX_LINES);
        pre.textContent = shown.join("\n");
      } else if (append.length) {
        pre.append(doc.createTextNode((shown.length ? "\n" : "") + append.join("\n")));
        shown = shown.concat(append);
        if (shown.length > MAX_LINES) { shown = trimLines(shown, MAX_LINES); pre.textContent = shown.join("\n"); }
      } else return;
      if (stick) pre.scrollTop = pre.scrollHeight;
    }
    function clear() { shown = []; pre.textContent = ""; }

    return { root: section, pre, update, clear, lines: () => shown, following: () => forced };
  }

  const api = { trimLines, shouldFollow, create, MAX_LINES };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) { root.hands = root.hands || {}; root.hands.logpanel = api; }
})(typeof window !== "undefined" ? window : globalThis);
