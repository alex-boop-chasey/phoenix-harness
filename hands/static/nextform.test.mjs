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
