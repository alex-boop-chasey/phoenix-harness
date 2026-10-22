// node --test (tools/js-test.sh). store.js is a browser IIFE that also exports for node.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { diffKeys, logAppend, createStore } = createRequire(import.meta.url)("./store.js");

test("diffKeys lists keys whose JSON changed, including added and removed", () => {
  assert.deepEqual(diffKeys({ a: 1, b: [1], c: "x" }, { a: 1, b: [2], d: 0 }).sort(), ["b", "c", "d"]);
  assert.deepEqual(diffKeys(null, { a: 1 }), ["a"]);
  assert.deepEqual(diffKeys({ a: { x: 1 } }, { a: { x: 1 } }), []);
});

test("logAppend returns only the new tail when the old tail is still there", () => {
  assert.deepEqual(logAppend(["a", "b"], ["b", "c"]), { append: ["c"], reset: false });
  assert.deepEqual(logAppend(["a", "b"], ["a", "b"]), { append: [], reset: false });
  assert.deepEqual(logAppend([], ["x"]), { append: ["x"], reset: false });
});

test("logAppend resets when the old last line is gone (log rotated or project switched)", () => {
  assert.deepEqual(logAppend(["a"], ["x", "y"]), { append: ["x", "y"], reset: true });
});

test("logAppend matches repeated lines by the longest shared overlap", () => {
  // old ends "ok, ok"; new has "ok" in several places: the overlap of two lines picks the right spot
  assert.deepEqual(logAppend(["start", "ok", "ok"], ["ok", "ok", "ok", "next"]), { append: ["next"], reset: false });
});

// ---- a store wired to fakes: fetch answers from a table, EventSource is driven by the test
function fakes() {
  const calls = [];
  const answers = {};                      // path -> {status, body, etag}
  const timers = [];
  const fetch = async (path, opts) => {
    calls.push({ path, inm: opts && opts.headers && opts.headers["If-None-Match"] });
    const a = answers[path.split("?")[0]] || { status: 404, body: { error: "not found" } };
    if (a.etag && opts && opts.headers && opts.headers["If-None-Match"] === a.etag) return { status: 304, ok: false, headers: { get: () => a.etag }, json: async () => ({}) };
    return { status: a.status || 200, ok: (a.status || 200) < 300, headers: { get: (k) => k.toLowerCase() === "content-type" ? "application/json" : a.etag }, json: async () => a.body };
  };
  const sources = [];
  class EventSource {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; sources.push(this); }
    addEventListener(ev, fn) { this.listeners[ev] = fn; }
    close() { this.closed = true; }
    fire(ev, data) { const fn = this.listeners[ev]; if (fn) return fn({ data: JSON.stringify(data) }); }
  }
  const setInterval = (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; };
  const clearInterval = (t) => { if (t) t.cleared = true; };
  return { calls, answers, timers, sources, deps: { fetch, EventSource, setInterval, clearInterval } };
}
const flush = () => new Promise((r) => setTimeout(r, 0));

test("sync fetches detail and log, publishes both, then uses If-None-Match and publishes nothing on 304", async () => {
  const f = fakes();
  f.answers["/api/p/demo"] = { body: { name: "demo", version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/log"] = { body: { lines: ["a"], version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/attempts"] = { body: [] };
  const store = createStore(f.deps);
  const seen = [];
  store.subscribe((name, patch) => seen.push([name, Object.keys(patch).sort()]));
  await store.sync("demo");
  assert.deepEqual(seen, [["demo", ["attempts", "detail", "log"]]]);
  assert.equal(store.get("demo").version, "v1");
  await store.sync("demo");
  assert.equal(seen.length, 1, "a 304 publishes nothing");
  assert.equal(f.calls.filter((c) => c.path === "/api/p/demo")[1].inm, '"v1"');
});

test("sync publishes only the keys that changed", async () => {
  const f = fakes();
  f.answers["/api/p/demo"] = { body: { name: "demo", version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/log"] = { body: { lines: ["a"], version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/attempts"] = { body: [] };
  const store = createStore(f.deps);
  await store.sync("demo");
  const seen = [];
  store.subscribe((name, patch) => seen.push(Object.keys(patch).sort()));
  f.answers["/api/p/demo"] = { body: { name: "demo", version: "v2" }, etag: '"v2"' };
  f.answers["/api/p/demo/log"] = { body: { lines: ["a", "b"], version: "v2" }, etag: '"v2"' };
  await store.sync("demo");
  assert.deepEqual(seen, [["log"]]);       // detail text identical apart from the stamp, attempts unchanged
});

test("open starts an EventSource; change events resync; error falls back to a 15 s poll until the next open", async () => {
  const f = fakes();
  f.answers["/api/p/demo"] = { body: { name: "demo", version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/log"] = { body: { lines: [], version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/attempts"] = { body: [] };
  const store = createStore(f.deps);
  store.open("demo");
  assert.equal(f.sources.length, 1);
  assert.equal(f.sources[0].url, "/api/p/demo/events");
  await flush();
  const before = f.calls.length;
  assert.ok(before >= 2, "open syncs at once");
  f.sources[0].fire("open");
  await flush();
  assert.equal(f.calls.length, before, "the first open event does not sync again");
  f.sources[0].fire("change", { version: "v2" });
  await flush();
  assert.ok(f.calls.length > before, "a change event resyncs");
  f.sources[0].fire("open");
  await flush();
  const afterFirstOpen = f.calls.length;
  f.sources[0].fire("error");
  assert.equal(f.timers.filter((t) => !t.cleared && t.ms === 15000).length, 1, "poll fallback armed");
  f.sources[0].fire("open");
  await flush();
  assert.equal(f.timers.filter((t) => !t.cleared).length, 0, "poll fallback cleared on reconnect");
  assert.ok(f.calls.length > afterFirstOpen, "a reconnect resyncs (the first open does not: open() already synced)");
  store.close("demo");
  assert.equal(f.sources[0].closed, true);
});

test("open without EventSource support polls; close stops it and keeps the snapshot", async () => {
  const f = fakes();
  f.answers["/api/p/demo"] = { body: { name: "demo", version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/log"] = { body: { lines: [], version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/attempts"] = { body: [] };
  const store = createStore({ ...f.deps, EventSource: undefined });
  store.open("demo");
  await flush();
  assert.equal(f.timers.filter((t) => !t.cleared).length, 1);
  store.close("demo");
  assert.equal(f.timers.filter((t) => !t.cleared).length, 0);
  assert.equal(store.get("demo").detail.name, "demo");
});

test("a failed fetch keeps the last snapshot and records the error", async () => {
  const f = fakes();
  f.answers["/api/p/demo"] = { body: { name: "demo", version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/log"] = { body: { lines: [], version: "v1" }, etag: '"v1"' };
  f.answers["/api/p/demo/attempts"] = { body: [] };
  const store = createStore(f.deps);
  await store.sync("demo");
  f.deps.fetch = async () => { throw new Error("Load failed"); };
  await store.sync("demo");
  assert.equal(store.get("demo").detail.name, "demo");
  assert.match(store.get("demo").error, /Load failed/);
});
