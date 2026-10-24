// Mock of the Auto-resolve block (static data, no server calls): ?on=1 switch on, ?self=1 self project,
// ?history=1 two history entries, ?pending=1 a scheduled retry.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const q = new URLSearchParams(location.search);
  const on = q.get("on") === "1";
  const now = Math.floor(Date.now() / 1000);
  const history = q.get("history") === "1" ? [
    { at: now - 600, step: "S03", cause: "attempts_exhausted", action: "escalate_to_alex", valid: true, executed: true, problem: "",
      reason: "The failure needs a spec change <script>alert(1)</script>", note: "Acceptance expects a page the spec never lists.",
      root_command: "sudo systemctl restart docker" },
    { at: now - 7200, step: "S02", cause: "same_failure", action: "hint_and_retry", valid: true, executed: true, problem: "",
      reason: "Same lint error three times; the hint names the rule.", note: "", root_command: "" },
  ] : [];
  const view = {
    enabled: on, self_enabled: false, is_self: q.get("self") === "1", daily_cap: 6, runs_today: history.length,
    pending: q.get("pending") === "1" ? { step: "S03", resume_at: now + 3600, reason: "rate limit" } : null, history,
  };
  const out = document.createElement("pre");
  out.className = "out";
  out.setAttribute("data-testid", "mock-out");
  const send = async (path, body) => {
    out.textContent = `POST ${path} ${JSON.stringify(body)}`;
    return { status: 200, body: { ok: true } };
  };
  const strip = document.createElement("div");
  strip.className = "row";
  const badge = nf.resolverBadge(document, on);
  if (badge) strip.append(badge);
  document.getElementById("main").replaceChildren(strip, nf.resolverBlock(document, view, { name: "demo", path: "/api/p/demo/resolver", send }), out);
});
