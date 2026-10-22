// Project store (0.6). One snapshot per project {detail, log, attempts, version, fetchedAt, error}, fed by the
// server's change feed (/api/p/<name>/events, Server-Sent Events) and conditional fetches (If-None-Match ->
// 304). Subscribers get (name, patch) with only the keys whose JSON changed, so the page patches what moved
// and nothing else. Without EventSource, or while it is in error, a 15 s poll stands in. Browser IIFE that
// also exports for node --test (tools/js-test.sh). Holds data only: never touches the DOM.
"use strict";
(function (root) {
  const POLL_MS = 15000;

  function diffKeys(prev, next) {
    const a = prev || {}, b = next || {};
    const out = [];
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
      if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) out.push(k);
    }
    return out;
  }

  // The new tail of a log given the lines shown so far. The old tail is found inside the new lines by the
  // longest overlap ending at the old last line; no overlap means the log was rotated or is another project's.
  function logAppend(oldLines, newLines) {
    if (!oldLines.length) return { append: newLines.slice(), reset: false };
    const last = oldLines[oldLines.length - 1];
    let best = -1, bestLen = 0;
    for (let j = newLines.length - 1; j >= 0; j--) {
      if (newLines[j] !== last) continue;
      let k = 1;
      while (k <= j && k < oldLines.length && newLines[j - k] === oldLines[oldLines.length - 1 - k]) k++;
      if (k > bestLen) { best = j; bestLen = k; }
      if (bestLen === Math.min(oldLines.length, j + 1)) break;      // cannot do better than the full overlap
    }
    if (best < 0) return { append: newLines.slice(), reset: true };
    return { append: newLines.slice(best + 1), reset: false };
  }

  // Keys compared for change notices. `version` is left out: it moves with every write, the page does not care.
  const KEYS = ["detail", "log", "attempts"];
  const strip = (o) => { if (!o || typeof o !== "object") return o; const c = { ...o }; delete c.version; return c; };

  function createStore(deps) {
    const snaps = new Map();
    const subs = new Set();
    const live = new Map();          // name -> {es, timer, syncing, dirty}

    function get(name) { return snaps.get(name) || null; }
    function subscribe(fn) { subs.add(fn); return () => subs.delete(fn); }
    function publish(name, patch) { for (const fn of subs) fn(name, patch); }

    async function request(path, etag) {
      const headers = etag ? { "If-None-Match": etag } : {};
      const r = await deps.fetch(path, { headers });
      if (r.status === 304) return { status: 304 };
      const ct = (r.headers && r.headers.get && r.headers.get("content-type")) || "";
      const body = ct.includes("json") ? await r.json() : null;
      if (!r.ok) throw new Error((body && body.error) || String(r.status));
      return { status: r.status, body };
    }

    async function sync(name) {
      const l = live.get(name);
      if (l && l.syncing) { l.dirty = true; return; }
      if (l) { l.syncing = true; l.dirty = false; }
      try {
        const prev = snaps.get(name) || { detail: null, log: null, attempts: null, version: "", fetchedAt: 0, error: "" };
        const enc = encodeURIComponent(name);
        const etag = prev.version ? `"${prev.version}"` : "";
        const [d, lg] = await Promise.all([request(`/api/p/${enc}`, etag), request(`/api/p/${enc}/log?lines=300`, etag)]);
        const next = { ...prev, fetchedAt: Date.now(), error: "" };
        if (d.status !== 304) { next.detail = d.body; next.version = d.body.version || ""; }
        if (lg.status !== 304) next.log = lg.body;
        if (d.status !== 304 && JSON.stringify(strip(prev.detail)) !== JSON.stringify(strip(d.body))) {
          const a = await request(`/api/p/${enc}/attempts`, "");
          next.attempts = a.body;
        }
        snaps.set(name, next);
        const patch = {};
        for (const k of KEYS) if (JSON.stringify(strip(prev[k])) !== JSON.stringify(strip(next[k]))) patch[k] = next[k];
        if (Object.keys(patch).length) publish(name, patch);
      } catch (e) {
        const prev = snaps.get(name);
        if (prev) snaps.set(name, { ...prev, error: e && e.message ? e.message : String(e) });
        else snaps.set(name, { detail: null, log: null, attempts: null, version: "", fetchedAt: 0, error: e && e.message ? e.message : String(e) });
        publish(name, { error: snaps.get(name).error });
      } finally {
        const l2 = live.get(name);
        if (l2) { l2.syncing = false; if (l2.dirty) { l2.dirty = false; sync(name); } }
      }
    }

    function poll(name, on) {
      const l = live.get(name);
      if (!l) return;
      if (on && !l.timer) l.timer = deps.setInterval(() => sync(name), POLL_MS);
      if (!on && l.timer) { deps.clearInterval(l.timer); l.timer = null; }
    }

    function open(name) {
      close(name);
      const l = { es: null, timer: null, syncing: false, dirty: false, opened: false };
      live.set(name, l);
      const ES = deps.EventSource;
      if (typeof ES === "function") {
        try {
          l.es = new ES(`/api/p/${encodeURIComponent(name)}/events`);
          l.es.addEventListener("change", () => sync(name));
          l.es.addEventListener("open", () => { poll(name, false); if (l.opened) sync(name); l.opened = true; });
          l.es.addEventListener("error", () => poll(name, true));
        } catch (e) { l.es = null; }
      }
      if (!l.es) poll(name, true);
      sync(name);
    }

    function close(name) {
      const l = live.get(name);
      if (!l) return;
      if (l.es) { try { l.es.close(); } catch (e) { /* already closed */ } }
      poll(name, false);
      live.delete(name);
    }

    return { get, subscribe, open, close, sync, POLL_MS };
  }

  const api = { diffKeys, logAppend, createStore };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) {
    root.hands = root.hands || {};
    root.hands.store = createStore({ fetch: (p, o) => root.fetch(p, o), EventSource: root.EventSource,
      setInterval: (f, ms) => root.setInterval(f, ms), clearInterval: (t) => root.clearInterval(t) });
    Object.assign(root.hands.store, { diffKeys, logAppend });
  }
})(typeof window !== "undefined" ? window : globalThis);
