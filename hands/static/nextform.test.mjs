// node --test (tools/js-test.sh). nextform.js is a browser IIFE that also exports for node.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const nf = createRequire(import.meta.url)("./nextform.js");
const { validateStyleUrl, styleUrlsFromForm, frameworkLabel, reasonText, mockFromQuery, FRAMEWORK_LABELS, MAX_STYLE_URLS } = nf;

test("validateStyleUrl accepts http and https URLs", () => {
  assert.deepEqual(validateStyleUrl("https://example.com/a"), { ok: true, reason: "" });
  assert.deepEqual(validateStyleUrl("http://example.com"), { ok: true, reason: "" });
  assert.deepEqual(validateStyleUrl("  https://example.com/a?b=1  "), { ok: true, reason: "" });
});

test("validateStyleUrl rejects credentials in the URL", () => {
  assert.deepEqual(validateStyleUrl("https://u:p@x.io"), { ok: false, reason: "credentials" });
  assert.equal(validateStyleUrl("https://user@x.io/").reason, "credentials");
});

test("validateStyleUrl rejects schemes other than http(s)", () => {
  assert.deepEqual(validateStyleUrl("javascript:1"), { ok: false, reason: "scheme" });
  assert.equal(validateStyleUrl("javascript:alert(1)").reason, "scheme");
  assert.equal(validateStyleUrl("ftp://x.io/").reason, "scheme");
  assert.equal(validateStyleUrl("data:text/html,hi").reason, "scheme");
  assert.equal(validateStyleUrl("example.com").reason, "scheme");
});

test("validateStyleUrl rejects URLs longer than 2048 characters", () => {
  const base = "https://example.com/";
  const long = base + "a".repeat(2049 - base.length);
  assert.equal(long.length, 2049);
  assert.deepEqual(validateStyleUrl(long), { ok: false, reason: "too long" });
  assert.equal(validateStyleUrl(long.slice(0, 2048)).ok, true);
});

test("validateStyleUrl rejects empty, non-string, whitespace and host-less input", () => {
  assert.equal(validateStyleUrl("").reason, "empty");
  assert.equal(validateStyleUrl("   ").reason, "empty");
  assert.equal(validateStyleUrl(undefined).reason, "empty");
  assert.equal(validateStyleUrl("https://exa mple.com/").reason, "whitespace");
  assert.equal(validateStyleUrl("https:///path").ok, false);
});

test("styleUrlsFromForm trims values and drops empties", () => {
  assert.deepEqual(styleUrlsFromForm([" https://a.io ", "", "   ", "https://b.io/x"]), ["https://a.io", "https://b.io/x"]);
  assert.deepEqual(styleUrlsFromForm([]), []);
  assert.deepEqual(styleUrlsFromForm([null, undefined, 3]), []);
});

test("framework labels are the five setup choices in order", () => {
  assert.deepEqual(FRAMEWORK_LABELS, ["None", "Astro + Tailwind", "Astro + Tailwind + AI kit", "Next.js + Tailwind", "Next.js + Tailwind + AI kit"]);
  assert.equal(frameworkLabel(1), "None");
  assert.equal(frameworkLabel("4"), "Next.js + Tailwind");
  assert.equal(frameworkLabel(9), "");
  assert.equal(MAX_STYLE_URLS, 10);
});

test("reasonText has a message for every reason", () => {
  for (const r of ["empty", "too long", "whitespace", "scheme", "credentials", "host", "invalid"]) assert.ok(reasonText(r).length > 0);
});

test("mockFromQuery reads refs and setup from the query string", () => {
  const m = mockFromQuery("?refs=queued,frozen,failed&setup=failed&state=open&missing=1");
  assert.deepEqual(m.style_refs.map((r) => r.status), ["queued", "frozen", "failed"]);
  assert.deepEqual(m.style_refs.map((r) => r.index), [1, 2, 3]);
  assert.equal(m.style_refs[2].reason.length > 0, true);
  assert.equal(m.setup.status, "failed");
  assert.match(m.setup.label, /Astro \+ Tailwind|Next\.js \+ Tailwind/);
  assert.deepEqual(mockFromQuery("?refs=&setup=done").style_refs, []);
  assert.equal(mockFromQuery("").setup.status, "none");
  assert.equal(mockFromQuery("").setup.label, "None");
  assert.deepEqual(mockFromQuery("?refs=bogus,frozen&setup=bogus").style_refs.map((r) => r.status), ["frozen"]);
});

test("planChatLabel: Plan with Claude without a session, Resume afterwards", () => {
  assert.equal(nf.planChatLabel("none"), "Plan with Claude");
  assert.equal(nf.planChatLabel(undefined), "Plan with Claude");
  for (const s of ["open", "brief_ready", "approved"]) assert.equal(nf.planChatLabel(s), "Resume planning chat");
});

test("planStatus needs a session id and a known status", () => {
  assert.equal(nf.planStatus(null), "none");
  assert.equal(nf.planStatus({ session_id: null, status: "open" }), "none");
  assert.equal(nf.planStatus({ session_id: "x", status: "brief_ready" }), "brief_ready");
  assert.equal(nf.planStatus({ session_id: "x", status: "bogus" }), "none");
});

test("shortSha is the first 12 hex chars of a sha256, empty otherwise", () => {
  const sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
  assert.equal(nf.shortSha(sha), "9f86d081884c");
  assert.equal(nf.shortSha(` ${sha.toUpperCase()} `), "9f86d081884c");
  assert.equal(nf.shortSha("abc"), "");
  assert.equal(nf.shortSha(null), "");
});

test("mockFromQuery maps state to plan_session", () => {
  assert.equal(nf.planStatus(mockFromQuery("?state=none").plan_session), "none");
  assert.equal(nf.planStatus(mockFromQuery("?state=open").plan_session), "open");
  assert.equal(mockFromQuery("?state=open").plan_session.brief_sha256, null);
  assert.equal(nf.shortSha(mockFromQuery("?state=brief_ready").plan_session.brief_sha256).length, 12);
  assert.equal(nf.planStatus(mockFromQuery("?state=approved").plan_session), "approved");
  assert.equal(nf.planStatus(mockFromQuery("?state=bogus").plan_session), "none");
});

test("toolUsageParts lists skills, tools with counts, MCP and plugins; null when empty", () => {
  const u = nf.toolUsageParts({ skills: ["test-driven-development"], tools: { Read: 12, Edit: 4 }, mcp: { github: 2 }, plugins: ["superpowers"], missing: [] });
  assert.deepEqual(u.parts, [["Skills", "test-driven-development"], ["Tools", "Read×12, Edit×4"], ["MCP", "github×2"], ["Plugins", "superpowers"]]);
  assert.deepEqual(u.missing, []);
  assert.deepEqual(nf.toolUsageParts({ missing: ["verification-before-completion"] }).missing, ["verification-before-completion"]);
  assert.equal(nf.toolUsageParts({ skills: [], tools: {} }), null);
  assert.equal(nf.toolUsageParts(null), null);
});

test("mockToolUsage: fixed data, missing=1 adds verification-before-completion", () => {
  assert.deepEqual(nf.mockToolUsage("").missing, []);
  assert.deepEqual(nf.mockToolUsage("?missing=1").missing, ["verification-before-completion"]);
  assert.deepEqual(nf.mockToolUsage("").tools, { Read: 12, Edit: 4 });
});

test("visibleSessions hides hidden sessions unless show all is on or the row is selected", () => {
  const rows = nf.mockSessions();
  assert.deepEqual(nf.visibleSessions(rows, false).map((r) => r.title), ["shell"]);
  assert.equal(nf.visibleSessions(rows, true).length, 2);
  assert.equal(nf.visibleSessions(rows, false, rows[1].id).length, 2);
  assert.deepEqual(nf.visibleSessions(null, true), []);
  assert.equal(rows[1].project, "demo");
});

test("the create form's chat button is labelled Create and plan with Claude", () => {
  assert.equal(nf.CREATE_PLAN_LABEL, "Create and plan with Claude");
  assert.equal(nf.CREATE_PLAN_HINT, "Creates the project, then opens the planning chat");
});

test("shouldOpenPlanChat: once, and only once the project exists", () => {
  assert.equal(nf.shouldOpenPlanChat({ chat: true, done: true, started: false }), true);
  assert.equal(nf.shouldOpenPlanChat({ chat: true, done: true, started: true }), false, "no second start");
  assert.equal(nf.shouldOpenPlanChat({ chat: true, done: false, started: false }), false, "still creating");
  assert.equal(nf.shouldOpenPlanChat({ chat: false, done: true, started: false }), false, "Create project only");
  assert.equal(nf.shouldOpenPlanChat(null), false);
  assert.equal(nf.shouldOpenPlanChat({}), false);
});
