// In-place rendering (0.6). The builders in app.js still produce a whole new tree with el(); instead of
// replacing #main's subtree (which threw away scroll positions, open <details>, focus and typed text every
// refresh) the new tree is morphed into the live one with morphdom: nodes with the same key are kept and
// only their attributes and text change. Nodes marked data-self-managed (the environment editor, the file
// viewers, the action output) own their own state and are never touched; a focused form control is left
// alone so a keystroke is never lost to an update. Click handlers live in app.js's delegated dispatcher,
// never on the nodes, so a kept node can never hold stale data.
"use strict";
(function (root) {
  function keyOf(node) {
    if (node.nodeType !== 1) return undefined;
    return node.getAttribute("data-key") || node.id || undefined;
  }
  function beforeUpdate(from, to) {
    if (from.hasAttribute("data-self-managed")) return false;
    if (from === root.document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(from.tagName)) return false;
    if (from.tagName === "DETAILS") { if (from.open) to.setAttribute("open", ""); else to.removeAttribute("open"); }
    return true;
  }
  function patch(live, next) {
    return root.morphdom(live, next, { getNodeKey: keyOf, onBeforeElUpdated: beforeUpdate });
  }
  root.hands = root.hands || {};
  root.hands.render = { patch, keyOf, beforeUpdate };
})(window);
