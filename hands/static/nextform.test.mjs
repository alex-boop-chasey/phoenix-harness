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

// ---- design files (C1-05)
test("DESIGN_KINDS and DESIGN_ACCEPT: ten kinds in order, 15 extensions plus the image/video types, no HEIC", () => {
  assert.deepEqual(nf.DESIGN_KINDS, ["design-md", "style-pack", "screenshot", "brand", "avoid", "font", "icon-pack", "video", "copy-tone", "feature-list"]);
  const parts = nf.DESIGN_ACCEPT.split(",");
  for (const e of [".md", ".json", ".css", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".woff2", ".ttf", ".otf", ".zip", ".mp4", ".webm"]) assert.ok(parts.includes(e), e);
  for (const m of ["image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm"]) assert.ok(parts.includes(m), m);
  assert.equal(parts.length, 20);
  assert.ok(!/heic|heif|mov|quicktime/i.test(nf.DESIGN_ACCEPT));
});

test("designKindGuess follows the server's table", () => {
  const g = nf.designKindGuess;
  assert.equal(g("DESIGN.md"), "design-md");
  assert.equal(g("tokens.json"), "style-pack");
  assert.equal(g("theme.CSS"), "style-pack");
  assert.equal(g("notes.txt"), "copy-tone");
  assert.equal(g("brand-voice.md"), "copy-tone");
  assert.equal(g("features.md"), "feature-list");
  assert.equal(g("avoid-this.md"), "avoid");
  assert.equal(g("hero.png"), "screenshot");
  assert.equal(g("shot.JPEG"), "screenshot");
  assert.equal(g("logo.svg"), "brand");
  assert.equal(g("icons.svg"), "icon-pack");
  assert.equal(g("Inter.woff2"), "font");
  assert.equal(g("pack.zip"), "icon-pack");
  assert.equal(g("demo.mp4"), "video");
  assert.equal(g("mystery"), "design-md");
});

test("designFileCheck refuses wrong types (HEIC and MOV with a hint) and oversize files", () => {
  const c = nf.designFileCheck;
  assert.equal(c("a.md", 10), null);
  assert.equal(c("run.exe", 10), "type not allowed");
  assert.equal(c("IMG_1.HEIC", 10), "type not allowed (HEIC: share the photo as JPEG)");
  assert.equal(c("x.heif", 10), "type not allowed (HEIC: share the photo as JPEG)");
  assert.equal(c("clip.mov", 10), "type not allowed (MOV: export as MP4)");
  assert.equal(c("a.md", 1048576), null);
  assert.equal(c("a.md", 1048577), "too large");
  assert.equal(c("a.woff2", 5 * 1048576 + 1), "too large");
  assert.equal(c("a.png", 15 * 1048576), null);
  assert.equal(c("a.mp4", 50 * 1048576 + 1), "too large");
  assert.equal(c("a.zip", 25 * 1048576 + 1), "too large");
  assert.equal(c("a.md", 2000, { text: 1000 }), "too large", "caps override");
  assert.equal(c("a.png", 2000, { text: 1000 }), null, "other classes keep their default");
});

test("designTotalCheck refuses a submit over the cap", () => {
  assert.equal(nf.designTotalCheck([100, 200], 1000), null);
  assert.match(nf.designTotalCheck([600, 500], 1000), /^files total over /);
  assert.match(nf.designTotalCheck([150 * 1048576, 60 * 1048576]), /over 200 MB/);
  assert.equal(nf.designTotalCheck([], 1), null);
});

test("designSize prints bytes, KB and MB", () => {
  assert.equal(nf.designSize(12), "12 bytes");
  assert.equal(nf.designSize(12 * 1024), "12 KB");
  assert.equal(nf.designSize(Math.round(1.2 * 1048576)), "1.2 MB");
});

test("designSubmitLines: one upload line per plain row, one group line per group, refused rows absent", () => {
  const lines = nf.designSubmitLines([
    { name: "a.md", kind: "design-md", size: 3 },
    { name: "x.md", kind: "style-pack", size: 1, group: "g1", group_n: 2 },
    { name: "y.css", kind: "style-pack", size: 1, group: "g1", group_n: 2 },
  ]);
  assert.deepEqual(lines, ["upload a.md design-md 3", "group x.md y.css"]);
});

test("designUploadUrl carries name, kind, note, batch and the group marker", () => {
  const u = nf.designUploadUrl("demo", { name: "a b.md", kind: "design-md", note: "n&t", group: "g1", group_n: 2 }, "b1");
  assert.equal(u, "/api/p/demo/design-files/upload?name=a%20b.md&kind=design-md&note=n%26t&batch=b1&group=g1&group_n=2");
  assert.equal(nf.designUploadUrl("demo", { name: "a.md", kind: "design-md", note: "" }, "b1"),
    "/api/p/demo/design-files/upload?name=a.md&kind=design-md&note=&batch=b1");
});

test("changedDesignFiles keeps only the index entries the plan names", () => {
  const files = [{ id: "d001" }, { id: "d002" }, { id: "d003" }];
  assert.deepEqual(nf.changedDesignFiles(files, ["d003", "d001"]).map((f) => f.id), ["d001", "d003"]);
  assert.deepEqual(nf.changedDesignFiles(files, []), []);
  assert.deepEqual(nf.changedDesignFiles(null, ["d001"]), []);
});
