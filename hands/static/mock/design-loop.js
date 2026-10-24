// Mock of the design gates (static data, no server calls): ?gate=directions|tweaks|prototype&n=<2-8>&stale=1&approved=1.
// Saving prints the request the dashboard would send; stale=1 makes every save answer 409.
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const nf = window.hands.nextform;
  const q = new URLSearchParams(location.search);
  const gate = ["directions", "tweaks", "prototype"].includes(q.get("gate")) ? q.get("gate") : "directions";
  const n = Math.min(8, Math.max(2, parseInt(q.get("n") || "6", 10) || 6));
  const hex = (c) => c.repeat(64);
  const shots = [];
  const add = (route) => { for (const viewport of ["desktop", "mobile"]) shots.push({ route, viewport, file: `shot-${viewport}.png` }); };
  if (gate === "directions") for (let i = 1; i <= n; i++) add(`/directions/${i}`); else add("/");
  const opt = (id, label) => ({ id, label });
  const view = { step: "G01", gate, source_step: "S01", shots_sha: hex("a"), shots, redos: 0, approved: q.get("approved") === "1" };
  if (gate === "directions") view.directions = n;
  if (gate === "tweaks") {
    view.tweaks_sha = hex("b");
    view.tweaks = { schema: 1, groups: [
      { id: "nav-style", label: "Navigation", options: [opt("bar", "Top bar"), opt("split", "Split"), opt("pill", "Floating pill")] },
      { id: "accent", label: "Accent", options: [opt("green", "Green"), opt("orange", "Orange"), opt("ink", "Ink"), opt("red", "Red")] },
    ] };
  }
  if (gate === "prototype") view.design_sha = hex("c");
  const out = document.createElement("pre");
  out.className = "out";
  out.setAttribute("data-testid", "mock-out");
  const send = async (path, body) => {
    out.textContent = `POST ${path} ${JSON.stringify(body)}`;
    return q.get("stale") === "1" ? { status: 409, body: { error: "stale" } } : { status: 200, body: { ok: true, started: true } };
  };
  const panel = nf.designPanel(document, view, { slug: "demo", send, shotSrc: (s) => `shot-${s.viewport}.png` });
  document.getElementById("main").replaceChildren(panel, out);
});
