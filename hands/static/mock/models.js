// Mock of the Models block (static data, no server calls): ?override=1 gives the coder an active auto-override,
// ?running=1&pending=1 a pending change while a build runs, ?error=probe makes saving fail like a failed probe.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const q = new URLSearchParams(location.search);
  const ALLOW = [
    ["claude-fable-5-1", "Most capable; best for planning, hard builds and review; highest cost."],
    ["claude-opus-5-5", "Strong all-round model; good fallback for demanding work."],
    ["claude-sonnet-5-5", "Balanced speed and cost; good for subagents and routine steps."],
    ["claude-haiku-5-5", "Fastest and cheapest; for light, well-bounded tasks."],
  ].map(([id, note]) => ({ id, note }));
  const note = (id) => ALLOW.find((m) => m.id === id).note;
  const row = (role, model, source, effort) => ({ role, model, effort: effort || "", source, note: note(model), override: null, pending: false });
  const roles = [
    row("planner", "claude-fable-5-1", "default", "high"), row("coder", "claude-fable-5-1", "toml"),
    row("evaluator", "claude-opus-5-5", "manual", "medium"), row("subagents", "claude-sonnet-5-5", "default"),
    row("resolver", "claude-opus-5-5", "default"), row("fallback", "claude-opus-5-5", "default"),
  ];
  const coder = roles[1];
  if (q.get("override") === "1") {
    Object.assign(coder, { model: "claude-haiku-5-5", source: "auto-override", note: note("claude-haiku-5-5"),
      override: { model: "claude-haiku-5-5", reason: "model access", until: null } });
  }
  if (q.get("running") === "1" && q.get("pending") === "1") coder.pending = true;
  const view = {
    roles, allowlist: ALLOW, efforts: ["low", "medium", "high", "xhigh", "max"], effort_roles: ["planner", "coder", "evaluator", "resolver"],
    usage: [{ model: "claude-fable-5-1", sessions: 12, cost_usd: 18.4 }, { model: "claude-opus-5-5", sessions: 5, cost_usd: 3.07 }],
    alive: q.get("running") === "1",
  };
  const out = document.createElement("pre");
  out.className = "out";
  out.setAttribute("data-testid", "mock-out");
  const print = (line) => { out.textContent = out.textContent ? `${out.textContent}\n${line}` : line; };
  const send = async (path, body) => {
    if (q.get("error") === "probe") return { status: 400, body: { error: `probe failed for ${body.model}: model_access` } };
    print(`POST ${path} ${JSON.stringify(body)}`);
    return { status: 200, body: { ok: true } };
  };
  const block = nf.modelsBlock(document, view, {
    path: "/api/p/demo/models", send, clearOverride: () => print("POST /api/p/demo/action/ack-clear-model {}"),
  });
  document.getElementById("main").replaceChildren(block, out);
});
