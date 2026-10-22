// Session manager page (0.5 step 11). Persistent Claude terminals: list from /api/sessions, actions via
// POST /api/sessions/..., terminal over the WebSocket /ws/sessions/<id>?t=<ticket> (ticket = CSRF proof,
// minted by a POST). xterm.js is vendored under /vendor/. Everything the session prints is written
// into xterm (which escapes it); everything else is built with textContent, never innerHTML.
//
// CSP: the dashboard allows no inline styles, and xterm's DOM renderer injects two <style> elements
// (docs/console-review.md §5). Instead of a nonce (a CSP change) the terminal is opened with a
// documentOverride whose createElement("style") returns a hidden holder element: the CSS xterm writes
// into its textContent goes into a constructed CSSStyleSheet adopted by the document, which CSP does
// not govern. No colour or font literal lives here: the xterm theme is read from the --term-* tokens.
"use strict";

const Sessions = (() => {
  const S = {
    rows: [], selected: null, defaults: null, term: null, fit: null, ws: null, wsSid: null,
    closing: false, retry: 0, retryTimer: null, state: "idle", ended: false, opened: false, shimOk: true,
  };
  const q = (id) => document.getElementById(id);

  // ---- helpers
  function agoTs(ts) {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function shortCwd(p) {
    const home = S.defaults && S.defaults.home;
    return home && p.startsWith(home + "/") ? "~" + p.slice(home.length) : p;
  }
  const title = (r) => r.title || "New session";
  const post = (path, body) => api(path, { method: "POST", headers: { "X-Hands": "1", "Content-Type": "application/json" },
                                          body: JSON.stringify(body || {}) });

  // ---- xterm: theme from the design tokens, styles through a constructed stylesheet (no CSP change)
  function token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function theme() {
    const t = (n) => token(`--term-${n}`);
    const fg = t("fg"), bg = t("bg"), dim = t("dim");
    return {
      background: bg, foreground: fg, cursor: t("cursor"), cursorAccent: bg,
      selectionBackground: token("--accent-soft"), selectionForeground: fg, selectionInactiveBackground: token("--surface-2"),
      black: fg, red: t("red"), green: t("green"), yellow: t("yellow"), blue: t("blue"), magenta: t("magenta"), cyan: t("cyan"), white: bg,
      brightBlack: dim, brightRed: t("red"), brightGreen: t("green"), brightYellow: t("yellow"), brightBlue: t("blue"),
      brightMagenta: t("magenta"), brightCyan: t("cyan"), brightWhite: fg,
    };
  }
  function styleShim() {
    const ok = "adoptedStyleSheets" in document && typeof CSSStyleSheet === "function" && !!CSSStyleSheet.prototype.replaceSync;
    S.shimOk = ok;
    if (!ok) return null;                          // old browser: xterm's <style> elements get blocked (text still readable)
    return new Proxy(document, {
      get(target, key) {
        if (key === "createElement") {
          return (tag, opts) => {
            if (String(tag).toLowerCase() !== "style") return target.createElement(tag, opts);
            const holder = target.createElement("span");
            holder.hidden = true;
            holder.className = "xterm-style-shim";
            const sheet = new CSSStyleSheet();
            let css = "", attached = false;
            Object.defineProperty(holder, "textContent", {
              get: () => css,
              set: (v) => {
                css = String(v);
                try { sheet.replaceSync(css); } catch (e) { /* keep the previous rules */ }
                if (!attached) { document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet]; attached = true; }
              },
            });
            holder.remove = () => {
              HTMLElement.prototype.remove.call(holder);
              document.adoptedStyleSheets = document.adoptedStyleSheets.filter((s) => s !== sheet);
              attached = false;
            };
            return holder;
          };
        }
        const v = target[key];
        return typeof v === "function" ? v.bind(target) : v;
      },
    });
  }
  function ensureTerm() {
    if (S.term) return S.term;
    const host = q("xterm");
    const fontSize = parseInt(token("--fs-1"), 10) || 13;
    S.term = new Terminal({ cursorBlink: true, scrollback: 5000, fontSize, fontFamily: token("--mono"), theme: theme(),
                            minimumContrastRatio: 1, documentOverride: styleShim() || undefined });
    S.fit = new FitAddon.FitAddon();
    S.term.loadAddon(S.fit);
    S.term.open(host);
    S.opened = true;
    S.term.onData((d) => { if (S.ws && S.ws.readyState === 1) S.ws.send(new TextEncoder().encode(d)); });
    S.term.onResize(({ cols, rows }) => { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "resize", cols, rows })); });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onTheme = () => { S.term.options.theme = theme(); };
    if (mq.addEventListener) mq.addEventListener("change", onTheme); else if (mq.addListener) mq.addListener(onTheme);
    if (typeof ResizeObserver === "function") new ResizeObserver(() => refit()).observe(host);
    window.addEventListener("resize", refit);
    return S.term;
  }
  function refit() {
    if (!S.fit || q("page-sessions").hidden) return;
    try { S.fit.fit(); } catch (e) { /* not laid out yet */ }
  }

  // ---- connection state
  function setConn(state, text) {
    S.state = state;
    const c = q("s-conn");
    c.textContent = text;
    c.className = "conn " + ({ live: "ok", reconnecting: "warn", connecting: "", error: "bad", ended: "warn", stopped: "", idle: "" }[state] || "");
    renderHead();
  }
  function selectedRow() { return S.rows.find((r) => r.id === S.selected) || null; }
  function renderHead() {
    const r = selectedRow();
    const dot = q("s-dot"), badge = q("s-badge"), tg = q("s-tg");
    q("s-term-h").textContent = r ? title(r) : "No session selected";
    q("s-meta").textContent = r ? `${shortCwd(r.cwd)} · started ${new Date(r.created * 1000).toLocaleString([], { hour: "numeric", minute: "2-digit", day: "numeric", month: "short" })}` : "";
    const live = !!(r && r.live);
    dot.className = "dot" + (live ? (S.state === "reconnecting" ? " wait" : " live") : "");
    const label = !r ? "idle" : !live ? "stopped" : S.state === "live" ? "live" : S.state === "reconnecting" ? "reconnecting" : "connecting";
    badge.textContent = label;
    badge.className = "badge " + (label === "live" ? "s-running pulse" : label === "stopped" ? "s-stopped" : label === "idle" ? "s-idle" : "s-planning");
    const onTg = !!(r && r.telegram);
    tg.hidden = !onTg;
    tg.textContent = onTg && live ? "live on Telegram" : "Telegram";
    tg.title = onTg ? (live ? "Messages to your Telegram bot reach this session." : "Resumes connected to your Telegram bot.") : "";
    q("s-resume").disabled = !r || live;
    q("s-stop").disabled = !r || !live;
    q("s-telegram").disabled = !r;
    q("s-telegram").textContent = onTg ? "Leave Telegram" : "Go live on Telegram";
    q("s-delete").disabled = !r;
  }

  // ---- list
  // Hidden sessions (project planning chats, opened from the project page) are listed only with "Show all" on.
  function renderList() {
    const list = q("slist");
    const live = S.rows.filter((r) => r.live).length;
    const onTg = S.rows.find((r) => r.live && r.telegram);
    const shown = hands.nextform.visibleSessions(S.rows, q("s-show-all").checked, S.selected);
    const hidden = S.rows.length - shown.length;
    q("s-count").textContent = S.rows.length ? `${live} running · ${S.rows.length - live} stopped${onTg ? " · 1 on Telegram" : ""}${hidden ? ` · ${hidden} hidden` : ""}` : "";
    if (!shown.length) {
      list.replaceChildren(el("div", { class: "empty" }, el("span", { class: "glyph" }, "▣"), el("strong", {}, "No sessions yet"),
        el("span", { class: "small" }, "Start a persistent Claude terminal in a folder under your home. It keeps running when you close the browser."),
        el("button", { class: "btn primary sm", type: "button", on: { click: newSession } }, "New session")));
      return;
    }
    list.replaceChildren(...shown.map((r) => hands.nextform.sessionItem(document, r, {
      meta: `${shortCwd(r.cwd)}${r.live ? "" : " · stopped"}${r.viewers ? ` · ${r.viewers} viewer${r.viewers > 1 ? "s" : ""}` : ""}`,
      when: r.live ? "live" : agoTs(r.last_active), current: r.id === S.selected, onClick: () => select(r.id) })));
  }
  let loading = null;
  function load() { return loading || (loading = loadNow().finally(() => { loading = null; })); }
  async function loadNow() {
    const known = new Set(S.rows.map((r) => r.id));
    let rows;
    try { rows = await api("/api/sessions"); }
    catch (e) { q("s-count").textContent = `update failed: ${e.message}`; return; }
    for (const r of S.rows) if (!rows.some((x) => x.id === r.id) && !known.has(r.id)) rows.unshift(r);   // created while this request ran
    S.rows = rows;
    if (S.selected && !selectedRow() && known.has(S.selected)) { S.selected = null; disconnect(); setConn("idle", "That session was deleted."); }
    const r = selectedRow();
    if (r && !r.live && (S.state === "live" || S.state === "connecting" || S.state === "reconnecting")) {
      disconnect(); setConn("stopped", "Session stopped. Resume to start it again (its history is kept).");
    }
    renderList();
    renderHead();
  }

  // ---- terminal connection, with reconnect after a blip (fresh ticket every time)
  function disconnect() {
    clearTimeout(S.retryTimer);
    S.retryTimer = null;
    S.retry = 0;
    if (S.ws) { S.closing = true; try { S.ws.close(); } catch (e) { /* ignore */ } S.ws = null; }
    S.wsSid = null;
  }
  async function connect(id, isRetry) {
    disconnect();
    S.closing = false;
    S.ended = false;
    const term = ensureTerm();
    if (!isRetry) term.reset();
    setConn(isRetry ? "reconnecting" : "connecting", isRetry ? `Reconnecting (try ${S.retry})…` : "Connecting…");
    let t;
    try { t = await post(`/api/sessions/${encodeURIComponent(id)}/ticket`); }
    catch (e) {
      if (S.selected !== id) return;
      setConn("error", `Cannot connect: ${e.message}`);
      return scheduleRetry(id);
    }
    if (S.selected !== id) return;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    const ws = new WebSocket(`${proto}${location.host}/ws/sessions/${encodeURIComponent(id)}?t=${encodeURIComponent(t.ticket)}`);
    ws.binaryType = "arraybuffer";
    S.ws = ws;
    S.wsSid = id;
    ws.onopen = () => {
      if (S.ws !== ws) return;
      S.retry = 0;
      setConn("live", "Connected. Keys go to the session while the terminal is focused.");
      refit();
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      term.focus();
    };
    ws.onmessage = (ev) => {
      if (S.ws !== ws) return;
      if (typeof ev.data === "string") {
        let m = null;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m && m.type === "exit") { S.ended = true; setConn("ended", "The session ended."); load(); }
        return;
      }
      term.write(new Uint8Array(ev.data));
    };
    ws.onclose = (ev) => {
      if (S.ws !== ws) return;
      S.ws = null;
      if (S.closing) return;
      if (S.ended) { setConn("stopped", "Session stopped. Resume to start it again (its history is kept)."); load(); return; }
      if (ev.code === 1008 || ev.code === 4003) { setConn("error", "Connection refused by the server."); return; }
      scheduleRetry(id);
    };
    ws.onerror = () => { /* onclose follows */ };
  }
  function scheduleRetry(id) {
    if (S.selected !== id) return;
    S.retry += 1;
    if (S.retry > 8) { setConn("error", "Lost the connection. Click the session to try again."); return; }
    const wait = Math.min(10000, 500 * 2 ** (S.retry - 1));
    setConn("reconnecting", `Connection lost. Reconnecting in ${Math.round(wait / 1000) || 1} s (try ${S.retry} of 8)…`);
    S.retryTimer = setTimeout(() => { if (S.selected === id) connect(id, true); }, wait);
  }

  // ---- selection and actions
  async function select(id, keepHash) {
    S.selected = id;
    if (!keepHash && location.hash !== `#/sessions/${id}`) history.replaceState(null, "", `#/sessions/${id}`);
    q("sessions").classList.add("show-term");
    let r = selectedRow();
    if (!r) { await load(); r = selectedRow(); }
    renderList();
    renderHead();
    if (!r) { setConn("idle", "That session no longer exists."); return; }
    try {
      if (r.live) await connect(id);
      else { disconnect(); ensureTerm().reset(); setConn("stopped", "Session stopped. Resume to start it again (its history is kept)."); }
    } catch (e) { setConn("error", `Terminal failed: ${e.message}`); throw e; }
    setTimeout(refit, 0);
  }
  function back() {
    q("sessions").classList.remove("show-term");
    history.replaceState(null, "", "#/sessions");
  }
  async function newSession() {
    if (!S.defaults) { try { S.defaults = await api("/api/sessions/defaults"); } catch (e) { toast(`Could not load the session defaults: ${e.message}`, true); return; } }
    let row;
    try { row = await folderDialog(S.defaults.cwd); }
    catch (e) { setConn("error", `New session failed: ${e.message}`); return; }
    if (!row) { if (!S.selected) setConn("idle", "New session cancelled."); return; }
    S.rows.unshift(row);
    select(row.id).catch(() => {});
    load();
  }
  const start = (cwd, telegram) => post("/api/sessions/new", telegram ? { cwd, telegram: true } : { cwd });
  const tgTaken = (r) => S.rows.find((x) => x.live && x.telegram && (!r || x.id !== r.id));
  const tgBusy = (holder) => `another session is already live on Telegram (${title(holder)}); stop it or leave Telegram there first`;
  function folderDialog(def) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (done) return; done = true; resolve(v); if (typeof dlg.close === "function") dlg.close(); dlg.remove(); };
      const input = el("input", { class: "input mono", id: "s-cwd", type: "text", value: def, spellcheck: "false", autocomplete: "off", "aria-describedby": "s-cwd-hint" });
      const err = el("div", { class: "error", id: "s-cwd-err" });
      const tg = el("input", { type: "checkbox", id: "s-tg-new" });
      const taken = tgTaken(null);
      const tgErr = el("div", { class: "error", id: "s-tg-err" });
      const go = el("button", { class: "btn primary", type: "submit" }, "Start session");
      const form = el("form", { method: "dialog", on: { submit: async (ev) => {
        ev.preventDefault();
        const v = input.value.trim();
        const fail = (msg, where) => {
          (where || err).textContent = msg; go.disabled = false;
          if (!where) { input.setAttribute("aria-invalid", "true"); input.focus(); } else tg.focus();
        };
        if (!v) return fail("Enter a folder.");
        const holder = tg.checked ? tgTaken(null) : null;          // client pre-check; the server refuses too (409)
        if (holder) return fail(tgBusy(holder), tgErr);
        go.disabled = true;
        err.textContent = "";
        tgErr.textContent = "";
        input.removeAttribute("aria-invalid");
        try { finish(await start(v, tg.checked)); }
        catch (e) { fail(e.message, /Telegram/.test(e.message) ? tgErr : null); }
      } } },
        el("h2", { id: "s-dlg-title" }, "New session"),
        el("p", { class: "muted" }, "Starts Claude Code in a folder on the VPS. The session keeps running when you close this page (it lives as long as the dashboard); stop it from here when you are done."),
        el("div", { class: "field" }, el("label", { for: "s-cwd" }, "Working folder"), input,
          el("span", { class: "muted small", id: "s-cwd-hint" }, "Your home folder (the default) or a folder inside it; hidden folders such as ~/.config are refused."), err),
        el("div", { class: "field" }, el("label", { class: "check", for: "s-tg-new" }, tg,
          el("span", {}, "Live on Telegram", el("span", { class: "hint" },
            taken ? `Only one session can be live at a time; “${title(taken)}” is live now. Stop it or leave Telegram there first.`
                  : "Messages to your Telegram bot reach this session and Claude answers there. Only one session can be live at a time."))), tgErr),
        el("div", { class: "row" }, el("button", { class: "btn", type: "button", on: { click: () => finish(null) } }, "Cancel"), go));
      const dlg = el("dialog", { class: "dialog", "aria-labelledby": "s-dlg-title", on: { close: () => finish(null) } }, form);
      document.body.append(dlg);
      openDialog(dlg);
      input.focus();
      input.select();
    });
  }
  async function resume() {
    const r = selectedRow();
    if (!r || r.live) return;
    try { Object.assign(r, await post(`/api/sessions/${encodeURIComponent(r.id)}/resume`)); }
    catch (e) { setConn("error", `Could not resume: ${e.message}`); toast(`Could not resume: ${e.message}`, true); return; }
    renderList();
    connect(r.id);
  }
  // Telegram on/off = a restart with or without `--channels` (stop first when running); the server
  // refuses a second live session (409) and the message names the one that holds the slot.
  async function toggleTelegram() {
    const r = selectedRow();
    if (!r) return;
    const on = !r.telegram;
    const other = on ? tgTaken(r) : null;
    const ok = await confirmDialog({
      title: on ? `Go live on Telegram with ${title(r)}?` : `Leave Telegram with ${title(r)}?`,
      warn: other ? `“${title(other)}” is live on Telegram now. Only one session can be live at a time; stop it or leave Telegram there first.`
                  : r.live ? "This restarts the session (its conversation is kept). Anything Claude is doing right now is interrupted." : "",
      body: on ? "Messages to your Telegram bot reach this session and Claude answers there. Permission prompts are relayed to Telegram with Allow / Deny buttons; only paired accounts can send or approve."
               : "The session restarts without the Telegram channel. Messages to the bot are no longer delivered to any session.",
      ok: on ? (r.live ? "Restart live on Telegram" : "Resume live on Telegram") : (r.live ? "Restart without Telegram" : "Resume without Telegram"),
      okClass: "btn primary" });
    if (!ok) return;
    const holder = on ? tgTaken(r) : null;                          // client pre-check; the server refuses too (409)
    if (holder) { setConn("error", `Could not go live on Telegram: ${tgBusy(holder)}.`); return; }
    try {
      if (r.live) { disconnect(); Object.assign(r, await post(`/api/sessions/${encodeURIComponent(r.id)}/stop`)); }
      Object.assign(r, await post(`/api/sessions/${encodeURIComponent(r.id)}/resume`, { telegram: on }));
    } catch (e) {
      load();
      setConn("error", `Could not ${on ? "go live on" : "leave"} Telegram: ${e.message}`);
      renderHead();
      return;
    }
    renderList();
    connect(r.id);
  }
  async function stop() {
    const r = selectedRow();
    if (!r || !r.live) return;
    const ok = await confirmDialog({ title: `Stop ${title(r)}?`, body: "Ends Claude's process. Its conversation is kept and you can resume it later from this list.", ok: "Stop session", okClass: "btn primary" });
    if (!ok) return;
    try { Object.assign(r, await post(`/api/sessions/${encodeURIComponent(r.id)}/stop`)); }
    catch (e) { setConn("error", `Could not stop: ${e.message}`); toast(`Could not stop: ${e.message}`, true); return; }
    disconnect();
    setConn("stopped", "Session stopped. Resume to start it again (its history is kept).");
    renderList();
  }
  async function del() {
    const r = selectedRow();
    if (!r) return;
    const ok = await confirmDialog({ title: `Delete ${title(r)}?`, warn: r.live ? "This session is running; deleting stops it first." : "",
      body: "Removes the session from this list and deletes its conversation transcript on the VPS. This cannot be undone.", ok: "Delete session" });
    if (!ok) return;
    try { await post(`/api/sessions/${encodeURIComponent(r.id)}/delete`); }
    catch (e) { setConn("error", `Could not delete: ${e.message}`); toast(`Could not delete: ${e.message}`, true); return; }
    disconnect();
    S.rows = S.rows.filter((x) => x.id !== r.id);
    S.selected = null;
    if (S.term) S.term.reset();
    setConn("idle", "Session deleted.");
    history.replaceState(null, "", "#/sessions");
    q("sessions").classList.remove("show-term");
    renderList();
    renderHead();
  }

  // ---- page lifecycle (called by app.js: init once, show on every route to #/sessions[/<id>], poll on ticks)
  function init() {
    q("s-new").addEventListener("click", newSession);
    q("s-back").addEventListener("click", back);
    q("s-resume").addEventListener("click", resume);
    q("s-stop").addEventListener("click", stop);
    q("s-telegram").addEventListener("click", toggleTelegram);
    q("s-delete").addEventListener("click", del);
    q("s-show-all").addEventListener("change", renderList);
    api("/api/sessions/defaults").then((d) => { S.defaults = d; renderList(); renderHead(); }).catch(() => {});
  }
  async function show(id) {
    await load();
    const exists = !!id && S.rows.some((r) => r.id === id);
    if (exists && id !== S.selected) select(id, true).catch(() => {});
    else if (exists) { q("sessions").classList.add("show-term"); renderHead(); }
    else if (!id) q("sessions").classList.remove("show-term");
    else setConn("idle", "That session no longer exists.");
    setTimeout(refit, 0);
    if (!S.shimOk && S.opened) setConn(S.state, `${q("s-conn").textContent} (this browser cannot style the terminal; colours are off)`);
  }
  function poll() { load(); }
  return { init, show, poll, state: S, select, connect, refit };
})();

window.hands = window.hands || {};
window.hands.sessions = Sessions;
