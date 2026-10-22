# Dashboard Render Rethink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Project builder's "rebuild the whole page every 4 s" model with a store-driven, in-place-patched UI fed by change events, so the page never loses scroll, focus, open panels or typed text, and only moves when data moved.

**Architecture:** One client store holds the last snapshot per project with a server-issued version. The server adds a cheap version (max mtime of the state files) to every project payload, honours `If-None-Match`, and streams "changed" nudges over Server-Sent Events, with the existing poll kept as a 15 s fallback. Rendering keeps the `el()` builders but a new tree is *morphed* into the live DOM (vendored morphdom, MIT, no build step) instead of `replaceChildren`, and all click handlers move to one delegated listener that reads the store, so patched nodes never hold stale closures. Sections are split into independently updated panels with their own version keys, and the page gets a real information hierarchy: a sticky overview strip, the live step rail, and collapsible panels that remember their open state.

**Tech Stack:** Python 3.12 stdlib (dashboard.py), vanilla ES2020 JS, CSS tokens from docs/design.md, vendored `morphdom` 2.7.x under `hands/static/vendor/`, Playwright Chromium in the agent image via `tools/ui-shots.sh` for browser checks, `python3 -m unittest discover -s tests`.

**Spec:** this document plus `docs/design.md` (design system, hard CSP rules) and `docs/api.md` (request rules). The complaint that started it: scrollable panels snap to the top every refresh.

## Global Constraints

- CSP stays `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'` (dashboard.py:87). No inline script, no `style` attributes, no `innerHTML` anywhere (tests/test_v052.py greps for it). Dynamic styles only via `element.style` or classes.
- Everything agent-written is rendered with `textContent`.
- Host/Origin/X-Hands/session checks unchanged for every new route. New GET routes go through `_get`; SSE must pass the same auth as `/api/p/<name>`.
- Python stdlib only; no npm, no bundler. Vendored JS goes in `hands/static/vendor/` and into `STATIC_FILES` (dashboard.py:78) with an exact version in the filename.
- Safari on Mac is the target browser (docs/design.md). `EventSource`, `CSS.escape`, `requestAnimationFrame`, `<details>` are all fine there.
- `MAX_CONN = 32` (dashboard.py:73). One SSE stream per open browser tab must leave room: cap SSE streams at 8 and refuse the 9th with 503 so the UI falls back to polling.
- Install only when the user says so and no build runs (CLAUDE.md).

## Review Focus

1. A panel the user is actively scrolling while an update lands must not jump: morphdom keeps the node, but a `<pre>` whose text changed grows; pin the log to the bottom only when it already was at the bottom (existing rule, keep it). Test in Task 6.
2. A textarea with typed feedback (`planFeedback`) or an env row being edited when an update lands must keep its value and caret. Task 4 keeps those nodes out of the morph via `onBeforeElUpdated` returning false for focused form controls. Test in Task 4.
3. An SSE connection dropped by the Cloudflare tunnel or a sleeping laptop must reconnect and resync: on `open` the client refetches with its last version. Test in Task 3.
4. A project whose spec is mid-refresh (`spec_error`) or whose state.json is being atomically replaced must not blank the page; a 304 or an exception keeps the last good snapshot. Test in Task 2.
5. Memory: a tab left open for a day on a building project must not grow without bound. The log panel keeps at most 2000 lines in the DOM and the store keeps one snapshot per project. Test in Task 6.

---

### Task 1: Server version stamps and conditional GET

**Files:**
- Modify: `hands/dashboard.py:350-385` (`detail`), `hands/dashboard.py:692-699` (`log_tail`), `hands/dashboard.py:1012-1056` (`_get`)
- Test: `tests/test_v059.py` (new; reuse the `Dash` harness from `tests/test_v04.py:109`)

**Interfaces:**
- Produces: `version(cfg) -> str` in dashboard.py: `"%x-%x" % (int(max_mtime_ns of state.json, activity.json, runner.log, metrics.jsonl, progress.jsonl, plan/plan.json, plan/plan-activity.json, round.log, specs/.git/HEAD), size of runner.log)`; missing files count as 0.
- Every `GET /api/p/<name>` and `/api/p/<name>/log` response carries `ETag: "<version>"` and the JSON gains `"version": "<version>"`.
- `If-None-Match` equal to the current ETag → `304` with no body.

- [ ] **Step 1: Write the failing tests** in `tests/test_v059.py`:

```python
def test_detail_has_version_and_etag(self):
    code, hdrs, body = self.d.req("GET", "/api/p/demo")
    self.assertEqual(code, 200); self.assertEqual(hdrs["ETag"], f'"{body["version"]}"')

def test_if_none_match_gives_304(self):
    _, hdrs, _ = self.d.req("GET", "/api/p/demo")
    code, _, body = self.d.req("GET", "/api/p/demo", headers={"If-None-Match": hdrs["ETag"]})
    self.assertEqual(code, 304); self.assertEqual(body, b"")

def test_version_changes_when_state_written(self):
    v1 = self.d.req("GET", "/api/p/demo")[2]["version"]
    (self.state_dir / "state.json").write_text('{"status":"running","steps":{}}')
    os.utime(self.state_dir / "state.json", ns=(1, 10**18))   # mtime strictly later
    self.assertNotEqual(v1, self.d.req("GET", "/api/p/demo")[2]["version"])
```

(`Dash.req` needs a `headers=` kwarg and must return parsed headers; extend the helper in `tests/test_v04.py` and keep its old call sites working.)

- [ ] **Step 2: Run** `python3 -m unittest tests.test_v059 -v` → FAIL (no ETag / no `headers` kwarg).
- [ ] **Step 3: Implement** `version(cfg)`, add `"version"` to `detail()` and `log_tail()`, and in `_get` compute the ETag before building the body for those two routes; send 304 via `_send(304, b"", "application/json", {"ETag": tag})`.
- [ ] **Step 4: Run** the new tests and the whole suite → PASS.
- [ ] **Step 5: Commit** `feat(dash): version stamp + conditional GET on project detail and log`.

### Task 2: Change feed over Server-Sent Events

**Files:**
- Modify: `hands/dashboard.py` (`_get`, `Server.__init__`, new `_events`), `docs/api.md` (new route section)
- Test: `tests/test_v059.py`

**Interfaces:**
- Produces: `GET /api/p/<name>/events` → `200 text/event-stream`, headers `Cache-Control: no-store`, `X-Accel-Buffering: no`. Body: `retry: 5000\n\n`, then every time `version(cfg)` changes (checked every 1.0 s in the handler thread) `event: change\ndata: {"version": "<v>"}\n\n`; a comment line `: ping\n\n` every 15 s of silence; stream ends after 10 minutes (client reconnects). `Server.sse_open` counter; 9th concurrent stream → `503 {"error": "too many streams"}`.
- Auth, Host and public-access checks are the ones every GET already passes in `_dispatch`.

- [ ] **Step 1: Write the failing tests**: `test_events_stream_sends_change_on_write` (open a raw socket with the session cookie, read until `event: change` after touching state.json; assert the `data` version equals the next `GET /api/p/demo` version), `test_events_ninth_stream_refused` (open 8 streams, 9th gets 503), `test_events_ping_keeps_socket_alive` (set `PING_INTERVAL` via monkeypatch to 0.2 s, assert a `: ping` arrives).
- [ ] **Step 2: Run** → FAIL 404.
- [ ] **Step 3: Implement** `_events(name)` in `Handler`: writes headers with `_send`-style helpers but keeps the connection, loops with `time.sleep(1.0)`, releases the counter in `finally`; disables the socket timeout for the duration (`self.connection.settimeout(None)`) exactly like `_terminal` does for the WebSocket.
- [ ] **Step 4: Run** suite → PASS. Document the route in `docs/api.md`.
- [ ] **Step 5: Commit** `feat(dash): SSE change feed per project`.

### Task 3: Client store and transport (`store.js`)

**Files:**
- Create: `hands/static/store.js`
- Modify: `hands/static/index.html` (script tag before app.js), `hands/dashboard.py:78` (`STATIC_FILES`), `tests/test_v052.py:22` (add to the lint list)
- Test: `hands/static/store.test.mjs` run with `node --test` inside the agent image (`docker run --rm --network none -v $PWD:/src:ro hands:cc2.1.301-pw1.64.1-dns2 node --test /src/hands/static/store.test.mjs`); add that command to `tools/js-test.sh`.

**Interfaces:**
- Produces global `window.hands.store` with:
  - `get(name) -> {detail, log, attempts, version, fetchedAt} | null`
  - `subscribe(fn: (name, patch: {detail?, log?, attempts?}) => void) -> unsubscribe`
  - `open(name)`: starts `EventSource('/api/p/<name>/events')`; on `change` or on `open` calls `sync(name)`; on `error` falls back to `setInterval(sync, 15000)` until the next successful `open`.
  - `close(name)`: tears down the stream and timer.
  - `sync(name)`: fetches detail and log with `If-None-Match`; 304 → no publish; 200 → publish only the keys whose JSON text changed.
  - Pure helpers exported for tests: `diffKeys(oldSnap, newSnap) -> string[]`, `logAppend(oldLines, newLines) -> {append: string[], reset: boolean}` (reset when the new tail does not contain the old last line).
- Consumes: Task 1 and Task 2 routes.

- [ ] **Step 1: Write failing tests** for `diffKeys` and `logAppend` (`logAppend(["a","b"], ["b","c"])` → `{append:["c"], reset:false}`; `logAppend(["a"], ["x","y"])` → `{reset:true, append:["x","y"]}`), and a fake-`EventSource` test that an `error` schedules a 15 s poll and an `open` cancels it.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `store.js` as an IIFE that also sets `module.exports` when `typeof module !== "undefined"` so `node --test` can import it.
- [ ] **Step 4: Run** JS tests and `python3 -m unittest tests.test_v052` → PASS.
- [ ] **Step 5: Commit** `feat(dash): client store with SSE + conditional fetch`.

### Task 4: In-place rendering with morphdom and delegated actions

**Files:**
- Create: `hands/static/vendor/morphdom-2.7.4.min.js` (verbatim from the npm tarball; record the sha256 in `docs/design.md`), `hands/static/render.js`
- Modify: `hands/static/app.js` (`refresh`, `statusSection`, `planSection`, `roundsSection`, `attemptsSection`, `stepBar`), `hands/static/index.html`, `hands/dashboard.py:78`
- Test: `tests/test_v059.py` (static checks), browser check via `tools/ui-shots.sh`

**Interfaces:**
- Produces `window.hands.render.patch(root: Element, next: Element)`: `morphdom(root, next, { getNodeKey: n => n.dataset?.key || n.id || undefined, onBeforeElUpdated: (from, to) => !(from === document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(from.tagName)) && !(from.tagName === "DETAILS" && (to.open = from.open, false)) })`. Details keep their open state; a focused control is never touched.
- Every clickable control in the builder page carries `data-act="<verb>"` plus `data-arg` and is handled by one `document.getElementById("main").addEventListener("click", dispatch)`; `dispatch` looks up the current snapshot in the store and calls the existing functions (`action`, `planAction`, `roundAction`, `showFile`, `showPlanFile`, `closeTab`, `reopen`). The inline `on: { click }` handlers inside `#main` are removed. Tabs and the "+" menu keep their own code (already reconciled in place).
- Every section builder gives its root a stable `data-key` (`status`, `plan`, `rounds`, `setup`, `env`, `steps`, `metrics`, `log`, `attempts`, `progress`, `viewer`, `stepbar`), and every list row a key (`step:<id>`, `attempt:<name>`, `metric:<ts>:<role>`).

- [ ] **Step 1: Write failing static tests**: `test_app_js_has_no_inline_click_in_main_builders` (grep `app.js` between `function statusSection` and `function tick` for `on: {` → must be 0 occurrences outside `envEditor` and `showNew`), `test_morphdom_vendored_and_served` (`GET /vendor/morphdom-2.7.4.min.js` → 200, `index.html` references it before `app.js`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `render.js`, vendor morphdom, convert `refresh()` to build the same `layout` tree and call `patch($("#main").firstElementChild, layout)` (first render still uses `replaceChildren`). Delete the `saveScrolls`/`restoreScrolls` helpers added on 2026-10-22 (uncommitted in the working tree) and the `keepOut`/`attempts-wrap` retention hacks: the morph keeps those nodes. Keep `envPanels` (its editor owns local state).
- [ ] **Step 4: Verify in a browser** with the rig: `bash tools/ui-shots.sh 26-morph bookings crm landing cafe florist`; add a `scroll-keep` page to `tools/shots.js` that scrolls `.scroll` inside the Steps panel to 200 px, waits 9 s (two updates), and asserts `scrollTop === 200`. Expected: assertion passes, no console errors (the rig fails the run on any).
- [ ] **Step 5: Run** the Python suite → PASS. **Commit** `feat(dash): morph-patched builder page, delegated actions`.

### Task 5: Subscribe the page to the store (remove the 4 s rebuild)

**Files:**
- Modify: `hands/static/app.js` (`tick`, `refresh`, `select`, `closeTab`, `loadProjects`, DOMContentLoaded)

**Interfaces:**
- Consumes: `store.open/close/subscribe/get` (Task 3), `render.patch` (Task 4).
- `select(name)` → `store.close(previous); store.open(name)`; the subscriber calls `renderProject(name, snapshot)`; `tick` keeps only `loadProjects()` (every 10 s) and `loadUsage()` (every 60 s). `refresh(full)` becomes `store.sync(current)` and is still called after every action so the result shows immediately.

- [ ] **Step 1: Write failing test** in `tests/test_v059.py`: `test_no_four_second_interval` (grep app.js: `setInterval(tick, 4000)` absent; `store.open(` present).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; keep the `document.hidden` and "typing" guards in `tick` for the two remaining polls.
- [ ] **Step 4: Verify**: rig page `bookings` with `UI_SHOTS_PROJECTS=6`; in `tools/shots.js` count network requests to `/api/p/` during 20 s on an idle fixture → expect ≤ 2 (initial pair), and one `events` stream.
- [ ] **Step 5: Commit** `feat(dash): builder page driven by store events, polling only as fallback`.

### Task 6: Incremental log panel

**Files:**
- Create: `hands/static/logpanel.js`
- Modify: `hands/static/app.js` (log section), `hands/static/app.css`

**Interfaces:**
- Produces `logPanel(name)` → element `<section data-key="log">` owning a `<pre id="log">` that it updates itself from the store's `log` patches using `logAppend` (Task 3): appends `append` lines as text nodes, trims to the newest 2000 lines, and scrolls to the bottom only if it was within 20 px of it before the append. Header gets a "Follow" toggle (`data-act="log-follow"`, aria-pressed) that forces bottom-follow on; a "Copy" button copies `textContent`.
- The morph skips this node (`onBeforeElUpdated` returns false for `[data-self-managed]`), as it does for the env editor.

- [ ] **Step 1: Write failing JS tests** for the panel's pure part `trimLines(lines, 2000)` and the follow rule `shouldFollow(scrollTop, clientHeight, scrollHeight, forced)` (`shouldFollow(0, 100, 1000, false) === false`, `shouldFollow(890, 100, 1000, false) === true`).
- [ ] **Step 2: Run** `node --test` in the image → FAIL.
- [ ] **Step 3: Implement** `logpanel.js`; wire into `refresh`'s layout in place of `logSec`.
- [ ] **Step 4: Verify** with the rig: page `bookings`, scroll the log to the top, wait 9 s → `scrollTop` unchanged; then press Follow → bottom.
- [ ] **Step 5: Commit** `feat(dash): incremental runner log with follow toggle`.

### Task 7: Information architecture: overview strip and panels

**Files:**
- Modify: `hands/static/app.js` (layout in `refresh`, `statusSection`), `hands/static/app.css`, `docs/design.md` (new components: `.overview`, `.panel`)
- Test: rig screenshots

**Interfaces:**
- Layout becomes: sticky `.overview` strip (project name, status badge, runner alive dot, current step + phase + live activity line, `passed/total` progress bar, cost and sessions, Start/Stop/Ack buttons) → two columns: left `.panels` (Plan, Change rounds, Setup, Environment, Steps table, Sessions, Attempts + file viewer, Progress notes, Runner log) as `<details class="panel" data-key=…>`, right the existing `.stepbar`. Panel open state is remembered per project in `localStorage` key `hands.panels.<name>` (wrapped in try/catch). Defaults open: Plan when `plan.state` is `ready`/`planning`/`failed`; Runner log when `alive`; Change rounds when a live round exists; Steps always.
- Phone (≤ 900 px): single column, step rail collapses into the overview strip's progress bar; nothing else changes.

- [ ] **Step 1: Build** the strip and panels; keep every existing section builder, only re-parent them.
- [ ] **Step 2: Verify** rig pages `bookings crm landing cafe florist new sessions` in light/dark at 1440 and 390; no console errors; `scroll-keep` page still passes.
- [ ] **Step 3: Update** `docs/design.md` components list and `README.md` dashboard paragraph; bump `VERSION` to 0.6.0 and add a CHANGELOG entry.
- [ ] **Step 4: Commit** `feat(dash): overview strip + remembered panels`.

### Task 8: Hardening and rollout

- [ ] **Step 1:** Run the full Python suite and the JS tests; run `bash tools/ui-shots.sh 26-final` for every default page.
- [ ] **Step 2:** Manual Safari check on the live dashboard after the user installs (`bash install.sh`, only when `hands railway-hotel status` shows runner alive: False): scroll a panel during a build, type feedback during an update, sleep the laptop and wake it (SSE reconnect), open 3 tabs.
- [ ] **Step 3:** Commit any fixes; tag `0.6.0`.

## Self-review notes

- Spec coverage: scroll loss (T4, T6), needless 4 s rebuild (T1, T2, T3, T5), hierarchy (T7), Safari and CSP constraints (global).
- Interfaces are consistent: `store.sync/open/close/subscribe`, `render.patch`, `logAppend`, `version(cfg)`, route `/api/p/<name>/events`.
- Proportion: 8 tasks for roughly 600 changed lines of JS/Python; bodies left to the implementer except the morphdom options and the SSE frame format, which are the decisions.
- Not covered on purpose: the Session manager page (already event-driven over its WebSocket) and the New-project form (local state, rebuilt only on navigation).
