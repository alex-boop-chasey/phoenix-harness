// File browser page (0.5 step 14). A bounded view of the allowed roots (hands/files.py): list/grid,
// sort, preview (text, raster images), upload (button or drag and drop), download, new folder, rename,
// delete, search, and the allowed-roots settings. Every name and file body is rendered with textContent
// (or as an <img> served with its image/* type, never SVG). No colour or font literal lives here: the look
// comes from the shared classes in app.css (docs/design.md).
//
// Location lives in the hash: #/files?root=<abs root>&path=<rel folder>[&file=<name>][&q=<search>]
// so links, Back/Forward and reloads all work.
"use strict";

const Files = (() => {
  const S = {
    info: null, root: null, path: "", list: null, file: null, q: "", hits: null,
    view: "list", sort: ["name", 1], hidden: true, seq: 0, uploads: [], uploading: false,
    text: null, editing: false,
  };
  const q = (id) => document.getElementById(id);
  const HDR = { "X-Hands": "1", "Content-Type": "application/json" };

  // ---- small helpers
  class HttpError extends Error { constructor(status, msg) { super(msg); this.status = status; } }
  async function call(path, opts) {
    const r = await fetch(path, opts);
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json().catch(() => ({})) : await r.text();
    if (!r.ok) throw new HttpError(r.status, (body && body.error) || `HTTP ${r.status}`);
    return body;
  }
  const post = (path, body) => call(path, { method: "POST", headers: HDR, body: JSON.stringify(body) });
  const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
  const join = (a, b) => (a ? `${a}/${b}` : b);
  const parentOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  const baseOf = (p) => p.slice(p.lastIndexOf("/") + 1);
  function size(n) {
    if (n === null || n === undefined) return "—";
    if (n < 1024) return `${n} B`;
    const u = ["KB", "MB", "GB", "TB"];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
  }
  function when(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000), now = new Date();
    const sameYear = d.getFullYear() === now.getFullYear();
    return d.toLocaleString([], { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }), hour: "numeric", minute: "2-digit" });
  }
  function perms(type, mode) {
    const m = parseInt(mode || "0", 8), c = "rwxrwxrwx";
    let s = type === "dir" ? "d" : type === "link" ? "l" : "-";
    for (let i = 0; i < 9; i++) s += m & (1 << (8 - i)) ? c[i] : "-";
    return s;
  }
  const ext = (name) => { const b = baseOf(name || ""), i = b.lastIndexOf("."); return i > 0 ? b.slice(i + 1).toLowerCase() : ""; };
  const IMG = new Set(["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico"]);
  const CODE = new Set(["js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "sh", "bash", "go", "rs", "rb", "php", "java", "c", "h", "cpp", "css", "html", "sql", "toml", "yaml", "yml", "svelte", "vue"]);
  const DOC = new Set(["md", "txt", "log", "rst", "csv", "pdf", "doc", "docx"]);
  function kind(e) {
    if (e.type === "dir" || (e.type === "link" && e.target_type === "dir")) return "dir";
    const x = ext(e.name || e.path || "");
    if (IMG.has(x)) return "img";
    if (CODE.has(x)) return "code";
    if (DOC.has(x)) return "doc";
    if (x === "json" || x === "jsonl") return "json";
    return "";
  }
  function chip(e) {
    const k = kind(e);
    if (e.type === "link" && e.ok === false) return el("span", { class: "ftype bad", "aria-hidden": "true" }, "!");
    const glyph = { dir: "▸", img: "IM", doc: "≡", json: "{}" }[k] || (k === "code" ? ext(e.name || e.path).slice(0, 2).toUpperCase() : "·");
    return el("span", { class: `ftype ${k === "json" ? "" : k}`.trim(), "aria-hidden": "true" }, glyph);
  }
  const isDir = (e) => e.type === "dir" || (e.type === "link" && e.ok !== false && e.target_type === "dir");
  const rootName = (r) => { const x = (S.info && S.info.roots || []).find((o) => o.path === r); return x ? x.name : baseOf(r); };
  const shown = (r) => { const h = S.info && S.info.home; return h && r.startsWith(h + "/") ? "~" + r.slice(h.length) : r; };
  function toast(msg, bad) {
    const box = q("toasts");
    const t = el("div", { class: bad ? "toast bad" : "toast", role: bad ? "alert" : "status" },
      el("span", { class: "dot", "aria-hidden": "true" }), el("span", {}, msg),
      el("button", { class: "x", type: "button", "aria-label": "Dismiss", on: { click: () => t.remove() } }, "×"));
    box.append(t);
    setTimeout(() => t.remove(), bad ? 9000 : 4000);
  }
  function refusal(e, what) {
    // a clear, page-level explanation of a server refusal (403 protected, 404 missing, 400 bad path)
    const st = e && e.status;
    const title = st === 403 ? "Access refused" : st === 404 ? "Not found" : st === 400 ? "Not allowed" : "Could not load";
    const hint = st === 403 ? "The File browser never opens protected folders or places outside the allowed roots, whatever the settings say."
      : st === 404 ? "It may have been moved or deleted, or the root is not in the allowed list any more."
      : st === 400 ? "That path is not valid here." : "Try again in a moment.";
    return el("div", { class: "notice bad", role: "alert" }, el("strong", {}, `${title}: ${what}`),
      el("span", { class: "small" }, `${String(e.message).charAt(0).toUpperCase()}${String(e.message).slice(1)}. ${hint}`));
  }

  // ---- hash <-> state
  function parseHash() {
    const h = location.hash || "";
    const i = h.indexOf("?");
    const p = new URLSearchParams(i >= 0 ? h.slice(i + 1) : "");
    return { root: p.get("root") || "", path: p.get("path") || "", file: p.get("file") || "", q: p.get("q") || "" };
  }
  function href(o) {
    const s = qs({ root: o.root || S.root, path: o.path, file: o.file, q: o.q });
    return `#/files${s ? "?" + s : ""}`;
  }
  function go(o, replace) {
    const h = href(o);
    if (h === location.hash) return show();
    if (replace) { history.replaceState(null, "", h); show(); } else location.hash = h;
  }

  // ---- loading
  async function loadInfo() {
    S.info = await call("/api/files/roots");
    const sel = q("f-root");
    sel.replaceChildren(...S.info.roots.map((r) => el("option", { value: r.path, ...(r.exists ? {} : { disabled: "" }) },
      `${r.name}${r.exists ? "" : " (missing)"} · ${shown(r.path)}`)));
  }

  async function show() {
    const seq = ++S.seq;
    try { if (!S.info) await loadInfo(); } catch (e) {
      q("f-body").replaceChildren(refusal(e, "the allowed roots")); return;
    }
    const h = parseHash();
    let root = h.root;
    if (!root) {
      let last = null;
      try { last = localStorage.getItem("hands.files.root"); } catch (e) { last = null; }
      const ok = S.info.roots.filter((r) => r.exists);
      root = (ok.find((r) => r.path === last) || ok[0] || S.info.roots[0] || {}).path;
      if (!root) { q("f-body").replaceChildren(emptyState("No roots", "Add a folder in Settings to browse it here.")); return; }
      return go({ root, path: "" }, true);
    }
    const moved = root !== S.root || h.path !== S.path || h.q !== S.q || !S.list;
    S.root = root; S.path = h.path; S.q = h.q;
    q("f-root").value = root;
    if (!q("f-root").value && root) {           // a root that is not configured (typed URL): show it so the refusal makes sense
      q("f-root").append(el("option", { value: root }, `${baseOf(root)} (not allowed) · ${shown(root)}`));
      q("f-root").value = root;
    }
    q("f-q").value = S.q;
    crumbs();
    if (moved) {
      S.list = null; S.hits = null;
      q("f-body").replaceChildren(el("div", { class: "row muted small" }, el("span", { class: "spinner sm" }), "Loading…"));
      try {
        if (S.q) S.hits = await call(`/api/files/search?${qs({ root, q: S.q })}`);
        else S.list = await call(`/api/files/list?${qs({ root, path: S.path })}`);
        if (seq !== S.seq) return;
        if (!S.q) { try { localStorage.setItem("hands.files.root", root); } catch (e) { /* ignore */ } }
      } catch (e) {
        if (seq !== S.seq) return;
        S.list = null;
        q("f-count").textContent = "";
        q("f-body").replaceChildren(refusal(e, S.q ? `search in ${shown(root)}` : shown(join(root, S.path))),
          el("div", { class: "row" }, el("a", { class: "btn sm", href: href({ root: S.info.roots[0] && S.info.roots[0].path, path: "" }) }, "Go to the first root")));
        folderTools(false);
        hidePreview();
        return;
      }
    }
    render();
    if (h.file) preview(h.file); else hidePreview();
  }

  function crumbs() {
    const box = q("f-crumbs");
    const kids = [];
    const segs = S.path ? S.path.split("/") : [];
    const name = rootName(S.root);
    kids.push(segs.length || S.q ? el("a", { href: href({ path: "" }) }, name) : el("span", { class: "here", "aria-current": "page" }, name));
    segs.forEach((s, i) => {
      kids.push(el("span", { class: "sep", "aria-hidden": "true" }, "/"));
      const p = segs.slice(0, i + 1).join("/");
      kids.push(i === segs.length - 1 && !S.q ? el("span", { class: "here", "aria-current": "page" }, s) : el("a", { href: href({ path: p }) }, s));
    });
    if (S.q) kids.push(el("span", { class: "sep", "aria-hidden": "true" }, "·"), el("span", { class: "here" }, `search “${S.q}”`));
    box.replaceChildren(...kids);
  }

  function emptyState(title, hint, action) {
    return el("div", { class: "empty" }, el("span", { class: "glyph", "aria-hidden": "true" }, "▤"), el("strong", {}, title),
      el("span", { class: "small" }, hint), action || null);
  }

  function sorted(entries) {
    const [k, dir] = S.sort;
    const out = entries.filter((e) => S.hidden || !(e.name || baseOf(e.path)).startsWith("."));
    out.sort((a, b) => {
      const da = isDir(a) ? 0 : 1, db = isDir(b) ? 0 : 1;
      if (da !== db) return da - db;                       // folders first, whatever the order
      let r = 0;
      if (k === "name") r = (a.name || a.path).localeCompare(b.name || b.path, undefined, { numeric: true, sensitivity: "base" });
      else r = ((a[k] || 0) - (b[k] || 0)) || (a.name || a.path).localeCompare(b.name || b.path);
      return r * dir;
    });
    return out;
  }

  function folderTools(on) {
    q("f-rename-here").hidden = !on;
    q("f-delete-here").hidden = !on;
    q("f-mkdir").disabled = !!S.q || !S.list;
    q("f-upload").disabled = !!S.q || !S.list;
  }

  function render() {
    const body = q("f-body");
    folderTools(!!S.list && !!S.path && !S.q);
    q("f-sort").value = `${S.sort[0]}:${S.sort[1]}`;
    if (S.q) return renderHits(body);
    const all = S.list.entries;
    const rows = sorted(all);
    const hiddenN = all.length - rows.length;
    const total = all.reduce((t, e) => t + (e.type === "file" ? e.size || 0 : 0), 0);
    q("f-count").textContent = `${all.length} item${all.length === 1 ? "" : "s"} · ${size(total)}` +
      (hiddenN ? ` · ${hiddenN} hidden` : "") + (S.list.truncated ? ` · first ${all.length} shown` : "") + (S.list.git ? " · inside .git (read only)" : "");
    if (!rows.length) {
      body.replaceChildren(emptyState(all.length ? "Only hidden files here" : "This folder is empty",
        all.length ? "Tick “Hidden files” to see them." : "Drop files here or use Upload."));
      return;
    }
    body.replaceChildren(S.view === "grid" ? grid(rows) : table(rows));
    body.classList.toggle("table-wrap", S.view !== "grid");
  }

  function entryHref(e, path) {
    const rel = path !== undefined ? path : join(S.path, e.name);
    return isDir(e) ? href({ path: rel }) : href({ path: parentOf(rel), file: baseOf(rel) });
  }

  function thSort(label, key, cls) {
    const on = S.sort[0] === key;
    const b = el("button", { type: "button", on: { click: () => { S.sort = [key, on ? -S.sort[1] : key === "name" ? 1 : -1]; saveView(); render(); } } },
      label, on ? (S.sort[1] > 0 ? " ↑" : " ↓") : "");
    return el("th", { class: cls || "", "aria-sort": on ? (S.sort[1] > 0 ? "ascending" : "descending") : "none", scope: "col" }, b);
  }

  function table(rows) {
    const cur = parseHash().file;
    const tb = el("tbody", {}, rows.map((e) => {
      const name = e.name;
      const bad = e.type === "link" && e.ok === false;
      const a = el("a", { class: "fname", href: entryHref(e) }, name);
      const tr = el("tr", { "data-name": name, ...(cur === name ? { "aria-selected": "true" } : {}) },
        el("td", { class: "fn" }, el("span", { class: "fnwrap" }, chip(e), a,
          e.type === "link" ? el("span", { class: "fsub" }, bad ? "link · blocked" : "link") : null)),
        el("td", { class: "num" }, isDir(e) ? "—" : size(e.size)),
        el("td", { class: "when hide-sm" }, when(e.modified)),
        el("td", { class: "mono perm hide-sm" }, perms(e.type, e.mode)),
        el("td", { class: "ract" },
          el("button", { class: "btn sm quiet", type: "button", "aria-label": `Rename ${name}`, title: "Rename or move", on: { click: () => renameDialog(join(S.path, name), isDir(e)) } }, "✎"),
          el("button", { class: "btn sm quiet", type: "button", "aria-label": `Delete ${name}`, title: "Delete", on: { click: () => deleteFlow(join(S.path, name), e) } }, "🗑\uFE0E")));
      return tr;
    }));
    return el("table", { class: "flist" },
      el("thead", {}, el("tr", {}, thSort("Name", "name"), thSort("Size", "size", "num"), thSort("Modified", "modified", "hide-sm"),
        el("th", { class: "hide-sm", scope: "col" }, "Permissions"), el("th", { scope: "col", "aria-label": "Actions" }))),
      tb);
  }

  function grid(rows) {
    const cur = parseHash().file;
    let thumbs = 0;
    return el("div", { class: "fgrid" }, rows.map((e) => {
      const k = kind(e);
      let thumb = chip(e);
      if (k === "img" && e.type === "file" && e.size <= 2_000_000 && thumbs < 60) {
        thumbs++;
        const img = el("img", { alt: "", loading: "lazy", src: `/api/files/image?${qs({ root: S.root, path: join(S.path, e.name) })}` });
        img.addEventListener("error", () => img.replaceWith(chip(e)));
        thumb = img;
      }
      return el("a", { class: "fcard", href: entryHref(e), "data-name": e.name, ...(cur === e.name ? { "aria-selected": "true" } : {}) },
        el("span", { class: "thumb" }, thumb), el("span", { class: "fname" }, e.name),
        el("span", { class: "fmeta" }, isDir(e) ? (e.type === "link" ? "folder link" : "folder") : e.type === "link" && e.ok === false ? "blocked link" : size(e.size)));
    }));
  }

  function renderHits(body) {
    const h = S.hits;
    q("f-count").textContent = `${h.hits.length} match${h.hits.length === 1 ? "" : "es"}${h.truncated ? " (search stopped early: narrow it down)" : ""}`;
    const clear = el("a", { class: "btn sm quiet text", href: href({ path: "" }) }, "Clear search");
    if (!h.hits.length) { body.replaceChildren(emptyState(`Nothing matches “${h.q}”`, "Try a shorter search, or another root.", clear)); return; }
    const rows = sorted(h.hits.map((x) => ({ ...x, name: x.path })));
    body.classList.add("table-wrap");
    body.replaceChildren(el("table", { class: "flist" },
      el("thead", {}, el("tr", {}, thSort("Path", "name"), thSort("Size", "size", "num"), thSort("Modified", "modified", "hide-sm"))),
      el("tbody", {}, rows.map((x) => el("tr", { "data-name": x.path },
        el("td", { class: "fn" }, el("span", { class: "fnwrap" }, chip(x), el("a", { class: "fname", href: entryHref(x, x.path) }, x.path))),
        el("td", { class: "num" }, x.type === "dir" ? "—" : size(x.size)),
        el("td", { class: "when hide-sm" }, when(x.modified)))))));
  }

  // ---- preview
  function hidePreview() {
    q("f-preview").hidden = true;
    q("fbrowser").classList.remove("has-preview");
    S.file = null; S.text = null; S.editing = false;
  }

  async function preview(name) {
    const rel = join(S.path, name);
    const entry = S.list && S.list.entries.find((e) => e.name === name);
    S.file = rel; S.text = null; S.editing = false;
    const pv = q("f-preview");
    pv.hidden = false;
    q("fbrowser").classList.add("has-preview");
    q("f-p-h").textContent = name;
    q("f-p-meta").textContent = entry ? `${size(entry.size)} · ${when(entry.modified)}` : "";
    const body = q("f-p-body");
    body.replaceChildren(el("div", { class: "pnote row muted small" }, el("span", { class: "spinner sm" }), "Loading…"));
    tools(rel, entry, false);
    const seq = S.seq;
    const x = ext(name);
    if (IMG.has(x)) {
      const img = el("img", { class: "pimg", alt: name, src: `/api/files/image?${qs({ root: S.root, path: rel })}` });
      img.addEventListener("load", () => { if (seq === S.seq) q("f-p-meta").textContent += ` · ${img.naturalWidth}×${img.naturalHeight}`; });
      img.addEventListener("error", () => body.replaceChildren(el("p", { class: "pnote muted" }, "This image cannot be previewed (too large or not a picture). Download it instead.")));
      body.replaceChildren(el("div", { class: "imgwrap" }, img));
      if (window.matchMedia("(max-width: 900px)").matches) pv.scrollIntoView({ block: "start" });
      return;
    }
    try {
      const r = await call(`/api/files/read?${qs({ root: S.root, path: rel })}`);
      if (seq !== S.seq || S.file !== rel) return;
      if (r.binary) {
        body.replaceChildren(el("p", { class: "pnote muted" }, "Binary file: no preview. Download it to open it on your computer."));
      } else {
        S.text = r;
        const pre = el("pre", { class: "log", tabindex: "0", "aria-label": `Contents of ${name}` }, r.text || "");
        body.replaceChildren(...[pre, r.truncated ? el("p", { class: "pnote muted small" }, `Showing the first ${size(r.text.length)} of ${size(r.size)}.`) : null].filter(Boolean));
        tools(rel, entry, true);
      }
    } catch (e) {
      if (seq !== S.seq) return;
      body.replaceChildren(el("div", { class: "pnote" }, refusal(e, name)));
    }
  }

  function tools(rel, entry, text) {
    const name = baseOf(rel);
    const dl = el("a", { class: "btn sm", href: `/api/files/download?${qs({ root: S.root, path: rel })}`, download: name }, "↓ Download");
    const copy = el("button", { class: "btn sm quiet text", type: "button", on: { click: () => copyText(join(S.root, rel)) } }, "Copy path");
    const editable = text && S.text && !S.text.truncated && !(S.list && S.list.git && !S.list.git_writes);
    const edit = editable ? el("button", { class: "btn sm quiet text", type: "button", on: { click: () => startEdit(rel) } }, "Edit") : null;
    const ren = el("button", { class: "btn sm quiet text", type: "button", on: { click: () => renameDialog(rel, false) } }, "Rename");
    const del = el("button", { class: "btn sm danger", type: "button", on: { click: () => deleteFlow(rel, entry || { type: "file", name }) } }, "Delete");
    q("f-p-tools").replaceChildren(...[dl, copy, edit, ren, el("span", { class: "grow" }), del].filter(Boolean));
  }

  function startEdit(rel) {
    if (!S.text) return;
    S.editing = true;
    const ta = el("textarea", { class: "textarea", spellcheck: "false", "aria-label": `Edit ${baseOf(rel)}` });
    ta.value = S.text.text;
    const status = el("span", { class: "muted small", role: "status" }, "Editing: changes are saved over the file.");
    const save = el("button", { class: "btn sm primary", type: "button" }, "Save");
    const cancel = el("button", { class: "btn sm", type: "button", on: { click: () => preview(baseOf(rel)) } }, "Cancel");
    save.addEventListener("click", async () => {
      save.disabled = true; status.textContent = "Saving…";
      try {
        await uploadBlob(S.root, parentOf(rel), baseOf(rel), new Blob([ta.value], { type: "text/plain" }), true);
        toast(`Saved ${baseOf(rel)}`);
        S.list = null;
        await show();
      } catch (e) { status.textContent = `Not saved: ${e.message}`; status.className = "error"; save.disabled = false; }
    });
    q("f-p-tools").replaceChildren(save, cancel, status);
    q("f-p-body").replaceChildren(ta);
    ta.focus();
  }

  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); toast("Path copied"); return; } catch (e) { /* fall back below */ }
    const i = el("input", { class: "input", readonly: "" });
    i.value = t; document.body.append(i); i.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    i.remove();
    toast(ok ? "Path copied" : `Copy failed: ${t}`, !ok);
  }

  // ---- dialogs (same <dialog> look as confirmDialog in app.js)
  function formDialog({ title, label, value, hint, ok, okClass, onSubmit }) {
    const input = el("input", { class: "input mono", id: "f-dlg-in", autocomplete: "off", spellcheck: "false" });
    input.value = value || "";
    const err = el("div", { class: "error", id: "f-dlg-err", role: "alert" });
    const okBtn = el("button", { class: okClass || "btn primary", type: "submit" }, ok);
    const cancel = el("button", { class: "btn", type: "button" }, "Cancel");
    const form = el("form", { method: "dialog" },
      el("h2", { id: "f-dlg-h" }, title),
      el("div", { class: "field" }, el("label", { for: "f-dlg-in" }, label), input, hint ? el("span", { class: "hint muted small" }, hint) : null, err),
      el("div", { class: "row" }, cancel, okBtn));
    const dlg = el("dialog", { class: "dialog", "aria-labelledby": "f-dlg-h" }, form);
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener("click", close);
    dlg.addEventListener("close", () => dlg.remove());
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; input.removeAttribute("aria-invalid");
      okBtn.disabled = true;
      try { await onSubmit(input.value.trim()); close(); } catch (e) {
        err.textContent = e.message; input.setAttribute("aria-invalid", "true"); okBtn.disabled = false; input.focus();
      }
    });
    document.body.append(dlg);
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    input.focus();
    const dot = input.value.lastIndexOf(".");
    input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
    return dlg;
  }
  function checkName(n) {
    if (!n) throw new Error("Give a name.");
    if (n.split("/").some((s) => s === ".." || s === "." || s === "")) throw new Error("Use plain names separated by /, without . or .. parts.");
    return n;
  }

  function mkdirDialog() {
    formDialog({ title: "New folder", label: `Folder name in ${S.path || rootName(S.root)}`, ok: "Create folder",
      onSubmit: async (v) => {
        const rel = join(S.path, checkName(v));
        await post("/api/files/mkdir", { root: S.root, path: rel });
        toast(`Created ${v}`);
        S.list = null; await show();
      } });
  }

  function renameDialog(rel, dir) {
    const name = baseOf(rel);
    formDialog({ title: dir ? "Rename folder" : "Rename", label: "New name", value: name,
      hint: "A name moves it within this folder; a path with / (relative to the root) moves it elsewhere.", ok: "Rename",
      onSubmit: async (v) => {
        checkName(v);
        if (v === name) return;
        const to = v.includes("/") ? v : join(parentOf(rel), v);
        const r = await post("/api/files/rename", { root: S.root, path: rel, to });
        toast(`Renamed to ${baseOf(r.path)}`);
        S.list = null;
        if (rel === S.path) return go({ root: r.root, path: r.path });          // renamed the folder we are in
        if (S.file === rel && parentOf(r.path) === S.path && r.root === S.root) return go({ path: S.path, file: baseOf(r.path) }, true);
        if (S.file === rel) return go({ path: S.path }, true);
        await show();
      } });
  }

  async function deleteFlow(rel, e) {
    const name = baseOf(rel);
    const dir = e.type === "dir";
    const ok = await confirmDialog({ title: `Delete ${name}?`, ok: "Delete", okClass: "btn danger",
      body: e.type === "link" ? "Only the link is removed, not what it points to." : dir ? "The folder is removed. This cannot be undone." : "The file is removed. This cannot be undone." });
    if (!ok) return;
    let recursive = false;
    for (;;) {
      try {
        await post("/api/files/delete", { root: S.root, path: rel, recursive });
        break;
      } catch (err) {
        if (err.status === 409 && dir && !recursive) {
          const all = await confirmDialog({ title: `${name} is not empty`, warn: "Everything inside it is deleted too.",
            body: "Delete the folder and all its contents? This cannot be undone.", ok: "Delete everything", okClass: "btn danger" });
          if (!all) return;
          recursive = true;
          continue;
        }
        toast(`Could not delete ${name}: ${err.message}`, true);
        return;
      }
    }
    toast(`Deleted ${name}`);
    S.list = null;
    if (rel === S.path) return go({ path: parentOf(rel) });
    if (S.file === rel) return go({ path: S.path }, true);
    await show();
  }

  // ---- upload (XHR for progress; one file at a time; 409 asks before overwriting)
  function uploadBlob(root, dirRel, name, blob, overwrite, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", `/api/files/upload?${qs({ root, path: join(dirRel, name), overwrite: overwrite ? "1" : "" })}`);
      x.setRequestHeader("X-Hands", "1");
      x.setRequestHeader("Content-Type", "application/octet-stream");
      if (onProgress) x.upload.addEventListener("progress", (ev) => { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); });
      x.addEventListener("load", () => {
        let b = {};
        try { b = JSON.parse(x.responseText); } catch (e) { b = {}; }
        if (x.status >= 200 && x.status < 300) resolve(b); else reject(new HttpError(x.status, b.error || `HTTP ${x.status}`));
      });
      x.addEventListener("error", () => reject(new HttpError(0, "network error")));
      x.addEventListener("abort", () => reject(new HttpError(0, "cancelled")));
      x.send(blob);
    });
  }

  function queue(fileList, skipped) {
    if (!S.list || S.q) return;
    const max = S.info.limits.max_upload;
    for (const f of fileList) {
      const it = { file: f, dir: S.path, root: S.root, state: "waiting", pct: 0, err: "" };
      if (f.size > max) { it.state = "failed"; it.err = `too large (max ${size(max)})`; }
      S.uploads.push(it);
    }
    if (skipped) toast(`${skipped} folder${skipped === 1 ? "" : "s"} skipped: upload files, not folders`, true);
    drawUploads();
    pump();
  }

  async function pump() {
    if (S.uploading) return;
    S.uploading = true;
    try {
      for (;;) {
        const it = S.uploads.find((u) => u.state === "waiting");
        if (!it) break;
        it.state = "uploading"; drawUploads();
        const prog = (p) => { it.pct = p; const f = it.fill; if (f) f.style.width = `${Math.round(p * 100)}%`; };
        try {
          try { await uploadBlob(it.root, it.dir, it.file.name, it.file, false, prog); }
          catch (e) {
            if (e.status !== 409 || / folder/.test(e.message)) throw e;
            const yes = await confirmDialog({ title: `Replace ${it.file.name}?`, body: `A file with that name is already in ${it.dir || rootName(it.root)}. Replace it with the one you are uploading?`,
              ok: "Replace", okClass: "btn danger" });
            if (!yes) { it.state = "skipped"; continue; }
            await uploadBlob(it.root, it.dir, it.file.name, it.file, true, prog);
          }
          it.state = "done"; it.pct = 1;
        } catch (e) { it.state = "failed"; it.err = e.message; }
        finally { drawUploads(); }
        if (it.root === S.root && it.dir === S.path && it.state === "done" && !S.q) {
          try { S.list = await call(`/api/files/list?${qs({ root: S.root, path: S.path })}`); render(); } catch (e) { /* next show() reports it */ }
        }
      }
    } finally { S.uploading = false; drawUploads(); }
  }

  function drawUploads() {
    const box = q("f-uploads");
    box.hidden = !S.uploads.length;
    const done = S.uploads.filter((u) => u.state === "done").length;
    q("f-up-count").textContent = `${done} of ${S.uploads.length} done`;
    q("f-up-clear").disabled = S.uploading;
    q("f-uplist").replaceChildren(...S.uploads.map((u) => {
      const st = { waiting: ["s-idle", "waiting"], uploading: ["s-planning pulse", `${Math.round(u.pct * 100)}%`], done: ["s-done", "done"],
                   failed: ["s-error", "failed"], skipped: ["s-stopped", "skipped"] }[u.state];
      const fill = el("span", { class: "fill" });
      fill.style.width = `${Math.round(u.pct * 100)}%`;
      u.fill = u.state === "uploading" ? fill : null;
      return el("div", { class: "upitem", "data-name": u.file.name },
        chip({ type: "file", name: u.file.name }), el("span", { class: "upname" }, u.file.name, " ", el("span", { class: "faint small" }, size(u.file.size))),
        el("span", { class: `badge ${st[0]}` }, st[1]),
        u.state === "uploading" ? el("span", { class: "progress", role: "progressbar", "aria-label": `Uploading ${u.file.name}` }, fill) : null,
        u.err ? el("span", { class: "error" }, u.err) : null);
    }));
  }

  function initDrop() {
    const page = q("page-files"), zone = q("f-drop");
    let depth = 0;
    const hasFiles = (ev) => ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes("Files");
    page.addEventListener("dragenter", (ev) => {
      if (!hasFiles(ev) || !S.list || S.q) return;
      ev.preventDefault(); depth++;
      q("f-drop-t").textContent = `Drop files to upload to ${S.path || rootName(S.root)}/`;
      zone.hidden = false; zone.classList.add("active");
    });
    page.addEventListener("dragover", (ev) => { if (hasFiles(ev) && S.list && !S.q) { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; } });
    page.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) { zone.hidden = true; zone.classList.remove("active"); } });
    page.addEventListener("drop", (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault(); depth = 0; zone.hidden = true; zone.classList.remove("active");
      const items = Array.from(ev.dataTransfer.items || []);
      let skipped = 0;
      const picked = [];
      if (items.length && items[0].webkitGetAsEntry) {
        for (const it of items) {
          const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
          if (en && en.isDirectory) { skipped++; continue; }
          const f = it.getAsFile(); if (f) picked.push(f);
        }
      } else picked.push(...Array.from(ev.dataTransfer.files || []));
      queue(picked, skipped);
    });
  }

  // ---- settings
  function settingsDialog() {
    const info = S.info;
    const rows = el("div", { class: "rootrows" });
    const err = el("div", { class: "error", role: "alert" });
    const addRow = (v, exists) => {
      const input = el("input", { class: "input mono", "aria-label": "Allowed root folder", placeholder: "~/folder", spellcheck: "false", autocomplete: "off" });
      input.value = v || "";
      const rm = el("button", { class: "btn quiet sm", type: "button", "aria-label": "Remove this root", on: { click: () => row.remove() } }, "×");
      if (exists === false) { input.readOnly = true; input.dataset.missing = "1"; }
      const row = el("div", { class: "rootrow" }, input, rm,
        exists === false ? el("span", { class: "hint small" }, el("span", { class: "badge s-stopped" }, "missing"), " This folder no longer exists; it is dropped when you save.") : null);
      rows.append(row);
      return input;
    };
    for (const r of info.roots) addRow(shown(r.path), r.exists);
    const git = el("input", { type: "checkbox", id: "f-gitw" });
    git.checked = !!info.git_writes;
    const save = el("button", { class: "btn primary", type: "submit" }, "Save");
    const cancel = el("button", { class: "btn", type: "button" }, "Cancel");
    const form = el("form", { method: "dialog" },
      el("h2", { id: "f-set-h" }, "File browser settings"),
      el("p", { class: "muted" }, "The File browser shows only these folders. Each must be an existing folder inside your home folder."),
      el("div", { class: "sect" }, el("h3", {}, "Allowed roots"), rows,
        el("div", { class: "row" }, el("button", { class: "btn sm", type: "button", on: { click: () => addRow("").focus() } }, "+ Add root")), err),
      el("div", { class: "sect" }, el("label", { class: "check", for: "f-gitw" }, git,
        el("span", {}, "Allow changes inside .git folders", el("span", { class: "hint" }, "Off by default: uploads, renames and deletes inside .git are refused.")))),
      el("div", { class: "sect" }, el("h3", {}, "Always denied"),
        el("p", { class: "muted small" }, "Secrets live here. These are never shown, whatever the roots above say, including through symlinks."),
        el("ul", { class: "denied", id: "f-denied" }, info.denied.map((d) => el("li", {}, el("span", { class: "badge s-error" }, "denied"), el("span", { class: "mono" }, d))))),
      el("div", { class: "row" }, cancel, save));
    const dlg = el("dialog", { class: "dialog wide", "aria-labelledby": "f-set-h" }, form);
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener("click", close);
    dlg.addEventListener("close", () => dlg.remove());
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = "";
      const inputs = Array.from(rows.querySelectorAll("input"));
      inputs.forEach((i) => i.removeAttribute("aria-invalid"));
      const roots = inputs.filter((i) => !i.dataset.missing).map((i) => i.value.trim()).filter(Boolean);
      if (!roots.length) { err.textContent = "Keep at least one root."; return; }
      save.disabled = true;
      try {
        S.info = await post("/api/files/settings", { roots, git_writes: git.checked });
        close();
        toast("Settings saved");
        await loadInfo();
        S.list = null;
        const still = S.info.roots.some((r) => r.path === S.root);
        if (still) await show(); else go({ root: (S.info.roots[0] || {}).path, path: "" });
      } catch (e) {
        err.textContent = e.message;
        const bad = inputs.find((i) => i.value.trim() && e.message.includes(i.value.trim()));
        if (bad) { bad.setAttribute("aria-invalid", "true"); bad.focus(); }
        save.disabled = false;
      }
    });
    document.body.append(dlg);
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    cancel.focus();
  }

  // ---- view prefs
  function saveView() { try { localStorage.setItem("hands.files.view", JSON.stringify({ view: S.view, sort: S.sort, hidden: S.hidden })); } catch (e) { /* ignore */ } }
  function loadView() {
    try {
      const v = JSON.parse(localStorage.getItem("hands.files.view") || "null");
      if (v && (v.view === "list" || v.view === "grid")) S.view = v.view;
      if (v && Array.isArray(v.sort) && ["name", "size", "modified"].includes(v.sort[0]) && (v.sort[1] === 1 || v.sort[1] === -1)) S.sort = v.sort;
      if (v && typeof v.hidden === "boolean") S.hidden = v.hidden;
    } catch (e) { /* defaults */ }
  }
  function setView(v) {
    S.view = v;
    q("f-vlist").setAttribute("aria-pressed", String(v === "list"));
    q("f-vgrid").setAttribute("aria-pressed", String(v === "grid"));
    saveView();
    if (S.list || S.hits) render();
  }

  function init() {
    loadView();
    setView(S.view);
    q("f-hidden").checked = S.hidden;
    q("f-vlist").addEventListener("click", () => setView("list"));
    q("f-vgrid").addEventListener("click", () => setView("grid"));
    q("f-hidden").addEventListener("change", () => { S.hidden = q("f-hidden").checked; saveView(); if (S.list || S.hits) render(); });
    q("f-sort").addEventListener("change", () => { const [k, d] = q("f-sort").value.split(":"); S.sort = [k, Number(d)]; saveView(); if (S.list || S.hits) render(); });
    q("f-root").addEventListener("change", () => go({ root: q("f-root").value, path: "" }));
    q("f-search").addEventListener("submit", (ev) => { ev.preventDefault(); const v = q("f-q").value.trim(); go({ path: v ? "" : S.path, q: v }); });
    q("f-q").addEventListener("search", () => { if (!q("f-q").value && S.q) go({ path: "" }); });   // the clear (×) button
    q("f-mkdir").addEventListener("click", mkdirDialog);
    q("f-upload").addEventListener("click", () => q("f-file").click());
    q("f-file").addEventListener("change", () => { queue(Array.from(q("f-file").files || [])); q("f-file").value = ""; });
    q("f-settings").addEventListener("click", async () => { try { await loadInfo(); } catch (e) { toast(e.message, true); return; } settingsDialog(); });
    q("f-up-clear").addEventListener("click", () => { S.uploads = S.uploads.filter((u) => u.state === "waiting" || u.state === "uploading"); drawUploads(); });
    q("f-close").addEventListener("click", () => go({ path: S.path }));
    q("f-back").addEventListener("click", () => go({ path: S.path }));
    q("f-rename-here").addEventListener("click", () => renameDialog(S.path, true));
    q("f-delete-here").addEventListener("click", () => deleteFlow(S.path, { type: "dir", name: baseOf(S.path) }));
    document.addEventListener("keydown", (ev) => {
      if (q("page-files").hidden || ev.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (S.file && !S.editing) go({ path: S.path });
    });
    initDrop();
  }

  return { init, show, state: S };
})();

window.hands = window.hands || {};
window.hands.files = Files;
