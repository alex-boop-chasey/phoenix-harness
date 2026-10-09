#!/usr/bin/env python3
"""Dashboard with two persistent PTY sessions (tmux-free, Hermes-style).

- Each kind ("local", "live") owns a long-running ptyprocess.PtyProcess that
  outlives the WebSocket. A drain task reads PTY output into a bounded
  RingBuffer and fans it out to attached clients.
- Right pane in the browser is xterm.js connected to /ws/<kind>.
- "live" is prewarmed on startup so Telegram answers from boot.
"""
import asyncio, base64, json, os, re, select, signal
from pathlib import Path

import ptyprocess
from aiohttp import web, WSMsgType

HOME = Path.home()
HERE = Path(__file__).resolve().parent
PROJECTS = HOME / ".claude" / "projects"
CRED = (HERE / ".cred").read_text().strip()
PORT = int(os.environ.get("DASH_PORT", 9001))
BIND = os.environ.get("DASH_BIND", "127.0.0.1")
TELEGRAM_STATE_DIR = str(HOME / "claude-harness" / "telegram-state")
APPEND_PROMPT = (
    'When a <channel source="plugin:telegram:telegram" chat_id="..."> message arrives:\n'
    "1. Call the plugin reply tool with that chat_id — plain assistant text in the terminal "
    "never reaches the sender.\n"
    "2. After the tool call, also emit the exact reply text as a plain assistant message in "
    'the terminal, prefixed with "→ telegram: ", so the terminal log keeps a readable record '
    "of both sides of the conversation. Do this every time, even for short replies."
)

CHANNEL_FLAGS = {
    "local": [],
    "live": [
        "--channels", "plugin:telegram@claude-plugins-official",
        "--append-system-prompt", APPEND_PROMPT,
    ],
}
KINDS = tuple(CHANNEL_FLAGS)
BUFFER_CAP = 256 * 1024          # ringbuffer per session (bytes)
READ_CHUNK = 8192
READ_TIMEOUT = 0.2               # seconds select() blocks per idle tick

# ---------- PTY plumbing ------------------------------------------------------

def build_argv(kind: str, uuid: str = "") -> list[str]:
    base = ["claude", "--dangerously-skip-permissions", "--verbose"]
    if uuid:
        base = ["claude", "--resume", uuid, "--dangerously-skip-permissions", "--verbose"]
    return base + CHANNEL_FLAGS[kind]

def session_env() -> dict[str, str]:
    env = os.environ.copy()
    # Scrub inherited markers that would make the spawned claude think it's a
    # child/nested session (disables transcript saving, suppresses notices).
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_PARENT_SESSION_ID"):
            env.pop(k, None)
    env["PATH"] = ":".join([
        str(HOME / ".bun" / "bin"),
        str(HOME / ".local" / "bin"),
        "/home/linuxbrew/.linuxbrew/bin",
        "/usr/local/bin", "/usr/bin", "/bin",
    ])
    env["TELEGRAM_STATE_DIR"] = TELEGRAM_STATE_DIR
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("LANG", "C.UTF-8")
    return env


class RingBuffer:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.buf = bytearray()
    def append(self, data: bytes) -> None:
        self.buf.extend(data)
        over = len(self.buf) - self.cap
        if over > 0: del self.buf[:over]
    def snapshot(self) -> bytes:
        return bytes(self.buf)


class PtySession:
    """Persistent PTY for a kind. Clients attach/detach freely; buffer is replayed."""
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.proc: ptyprocess.PtyProcess | None = None
        self.buffer = RingBuffer(BUFFER_CAP)
        self.clients: set[web.WebSocketResponse] = set()
        self._drain_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.cols = 160
        self.rows = 50
        self.last_data_at: float = 0.0

    async def spawn(self, uuid: str = "") -> None:
        async with self._lock:
            await self._kill_locked()
            argv = build_argv(self.kind, uuid)
            loop = asyncio.get_running_loop()
            self.proc = await loop.run_in_executor(
                None,
                lambda: ptyprocess.PtyProcess.spawn(
                    argv, cwd=str(HOME), env=session_env(),
                    dimensions=(self.rows, self.cols)),
            )
            self.buffer = RingBuffer(BUFFER_CAP)
            self._drain_task = asyncio.create_task(self._drain())
            print(f"[spawn] kind={self.kind} pid={self.proc.pid} uuid={uuid or '-'}", flush=True)

    async def kill(self) -> None:
        async with self._lock:
            await self._kill_locked()

    async def _kill_locked(self) -> None:
        # Boot any current viewers so the new drain doesn't paint on top of the
        # old frame. They auto-reconnect and get a clean replay of the new buffer.
        clients, self.clients = self.clients, set()
        for ws in clients:
            try:
                await ws.send_str(json.dumps({"type": "respawn"}))
                await ws.close(code=4001, message=b"respawn")
            except Exception: pass
        # Null first so the drain loop's `while p is self.proc` exits naturally.
        p, self.proc = self.proc, None
        if self._drain_task:
            self._drain_task.cancel()
            try: await self._drain_task
            except BaseException: pass
            self._drain_task = None
        if p and p.isalive():
            print(f"[kill] kind={self.kind} pid={p.pid}", flush=True)
            try:
                pgid = os.getpgid(p.pid)
            except Exception:
                pgid = None
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                if not p.isalive(): break
                try:
                    if pgid is not None: os.killpg(pgid, sig)
                    else: p.kill(sig)
                except Exception: pass
                for _ in range(10):
                    if not p.isalive(): break
                    await asyncio.sleep(0.05)

    def alive(self) -> bool:
        return bool(self.proc and self.proc.isalive())

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        p = self.proc
        while p is self.proc and p is not None:
            try:
                chunk = await loop.run_in_executor(None, self._read_blocking, p)
            except Exception:
                break
            if chunk is None:                       # EOF
                break
            if not chunk:                           # idle tick
                continue
            self.last_data_at = asyncio.get_running_loop().time()
            self.buffer.append(chunk)
            for ws in list(self.clients):
                if ws.closed: self.clients.discard(ws); continue
                try: await ws.send_bytes(chunk)
                except Exception: self.clients.discard(ws)
        # process exited; notify viewers
        for ws in list(self.clients):
            try: await ws.send_str(json.dumps({"type": "exit"}))
            except Exception: pass

    def _read_blocking(self, p) -> bytes | None:
        try:
            fd = p.fd
        except Exception:
            return None
        try:
            r, _, _ = select.select([fd], [], [], READ_TIMEOUT)
        except (OSError, ValueError):
            return None
        if not r: return b""
        try:
            return os.read(fd, READ_CHUNK) or None
        except OSError:
            return None

    async def write(self, data: bytes) -> None:
        p = self.proc
        if not p or not p.isalive(): return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: os.write(p.fd, data))
        except Exception: pass

    async def resize(self, cols: int, rows: int) -> None:
        cols = max(20, min(cols, 500)); rows = max(5, min(rows, 300))
        self.cols, self.rows = cols, rows
        p = self.proc
        if not p or not p.isalive(): return
        try: p.setwinsize(rows, cols)
        except Exception: pass

    def busy(self) -> tuple[bool, str]:
        # Streaming bytes within the last ~1s = actually working; stale buffer
        # content (an old token/timer line in scrollback) doesn't count.
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return False, "no loop"
        if self.last_data_at and (now - self.last_data_at) < 1.0:
            return True, "streaming"
        return False, "idle"


SESSIONS: dict[str, PtySession] = {k: PtySession(k) for k in KINDS}

# ---------- session listing (unchanged behavior) ------------------------------

def list_sessions() -> list[dict]:
    rows = []
    if not PROJECTS.exists(): return rows
    for proj in PROJECTS.iterdir():
        if not proj.is_dir(): continue
        for f in proj.glob("*.jsonl"):
            try:
                title = None
                with f.open("rb") as fh:
                    for line in fh:
                        if not line.strip(): continue
                        try: o = json.loads(line)
                        except Exception: continue
                        if o.get("type") == "user" and not o.get("isMeta"):
                            m = o.get("message", {}); c = m.get("content")
                            if isinstance(c, list):
                                c = next((p.get("text","") for p in c
                                          if isinstance(p, dict) and p.get("type")=="text"), "")
                            if isinstance(c, str) and c.strip() and \
                               "<local-command" not in c and not c.startswith("<"):
                                title = c.strip().splitlines()[0][:80]; break
                rows.append({
                    "uuid": f.stem,
                    "title": title or "(no prompt)",
                    "project": proj.name.lstrip("-").replace("-", "/"),
                    "mtime": int(f.stat().st_mtime),
                })
            except Exception: pass
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:200]

def do_delete(uuid: str) -> dict:
    if not re.match(r"^[0-9a-f-]{36}$", uuid):
        return {"ok": False, "err": "bad uuid"}
    if not PROJECTS.exists():
        return {"ok": False, "err": "no projects dir"}
    removed = 0
    for proj in PROJECTS.iterdir():
        if not proj.is_dir(): continue
        f = proj / f"{uuid}.jsonl"
        if f.exists():
            try: f.unlink(); removed += 1
            except Exception as e: return {"ok": False, "err": f"unlink failed: {e}"}
    return {"ok": removed > 0, "removed": removed}

# ---------- aiohttp app -------------------------------------------------------

INDEX = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Claude Harness</title>
<link rel="stylesheet" href="/static/xterm.min.css">
<script src="/static/xterm.min.js"></script>
<script src="/static/addon-fit.min.js"></script>
<style>
 html,body{margin:0;height:100%;font:13px/1.4 system-ui,sans-serif;background:#0b0d10;color:#d6dadf}
 #app{display:grid;grid-template-columns:340px 1fr;height:100%}
 #side{border-right:1px solid #222;display:flex;flex-direction:column;min-height:0}
 #side h1{font-size:12px;letter-spacing:.1em;color:#888;margin:0;text-transform:uppercase}
 .btn{display:inline-block;background:#1f6feb;color:#fff;border:0;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:12px}
 .btn:hover{background:#2b7ff5}
 .btn.g{background:#1b2027;color:#aab}
 .btn.live{background:#9b59b6}
 .btn.live:hover{background:#b270d4}
 .btn.del{background:#5a2026;color:#f1a9a9}
 .btn.del:hover{background:#8a2a33;color:#fff}
 .btn.sm{padding:3px 7px;font-size:11px}
 #head{padding:10px;border-bottom:1px solid #222;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 #live-status{font-size:11px;color:#888;flex:1 1 100%}
 #live-status b{color:#c89cde}
 #list{flex:1;overflow:auto;padding:6px}
 .row{padding:7px 8px;border-radius:6px;margin-bottom:3px;border:1px solid transparent;display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center}
 .row:hover{background:#161a20;border-color:#262c33}
 .row .t{color:#eaecef;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
 .row .m{color:#777;font-size:11px;margin-top:1px;grid-column:1;display:flex;justify-content:space-between;gap:8px}
 .row .m span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .row .a{grid-row:1 / span 2;display:flex;gap:4px}
 #main{display:flex;flex-direction:column;min-width:0}
 #tabs{display:flex;background:#0f1216;border-bottom:1px solid #222}
 .tab{padding:9px 16px;cursor:pointer;color:#6a7280;border-right:1px solid #222;font-size:12px;user-select:none;border-bottom:3px solid transparent;background:#0f1216}
 .tab:hover{color:#d6dadf}
 .tab.active{background:#1b2027;color:#fff;border-bottom-color:#1f6feb;font-weight:600}
 .tab[data-k=live].active{border-bottom-color:#9b59b6}
 .tab .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#9b59b6;margin-right:6px;vertical-align:middle}
 .frame{display:none;flex:1;background:#000;min-height:0;position:relative}
 .frame.active{display:block}
 .term{position:absolute;inset:0;padding:4px 0 0 6px;background:#000}
 .term .xterm{height:100%}
 .conn{position:absolute;top:6px;right:10px;font-size:10px;color:#e88;z-index:2;pointer-events:none;display:none}
 .conn.bad{display:block}
</style></head><body><div id=app>
<div id=side>
 <div id=head>
   <h1>Sessions</h1>
   <button class=btn onclick="act('local','new')">New (local)</button>
   <button class="btn live" onclick="restartLive()">Restart Live</button>
   <div id=live-status>Live tab = Telegram-connected · click <b>→Live</b> on a row to promote it.</div>
 </div>
 <div id=list>loading…</div>
</div>
<div id=main>
 <div id=tabs>
   <div class="tab active" data-k=local onclick="pick('local')">Local</div>
   <div class="tab" data-k=live onclick="pick('live')"><span class=dot></span>Live (Telegram)</div>
 </div>
 <div class="frame active" data-k=local><div class="term" id="term-local"></div><div class="conn" id="conn-local">connecting…</div></div>
 <div class="frame" data-k=live><div class="term" id="term-live"></div><div class="conn" id="conn-live">connecting…</div></div>
</div>
</div>
<script>
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(t){const now=Date.now()/1000,a=now-t;if(a<3600)return Math.round(a/60)+'m';if(a<86400)return Math.round(a/3600)+'h';return new Date(t*1000).toLocaleDateString()}
const TERMS = {};
function mkTerm(kind){
  const host = document.getElementById('term-'+kind);
  const conn = document.getElementById('conn-'+kind);
  const term = new Terminal({cursorBlink:true, fontSize:13, scrollback:20000,
    allowProposedApi:true, theme:{background:'#000'}, convertEol:false});
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(host);
  requestAnimationFrame(()=>{ try{ fit.fit(); }catch(e){} });
  let ws, inflightOpen = false;
  const refit = () => { try{ fit.fit(); sendResize(); }catch(e){} };
  const sendResize = () => {
    if (ws && ws.readyState===1) ws.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
  };
  const connect = () => {
    if (inflightOpen) return;
    inflightOpen = true;
    conn.textContent = 'connecting…'; conn.classList.add('bad');
    const proto = location.protocol==='https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/' + kind);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      inflightOpen = false;
      conn.classList.remove('bad');
      sendResize();
    };
    let respawning = false;
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        try {
          const o = JSON.parse(e.data);
          if (o.type==='exit')    conn.textContent='process exited';
          if (o.type==='respawn') { respawning = true; term.reset(); }
        } catch(_) {}
        return;
      }
      term.write(new Uint8Array(e.data));
    };
    ws.onclose = () => {
      inflightOpen = false;
      if (respawning) {
        conn.textContent = 'reconnecting…'; conn.classList.add('bad');
        setTimeout(connect, 150);
      } else {
        conn.textContent = 'disconnected · retrying'; conn.classList.add('bad');
        setTimeout(connect, 1200);
      }
    };
    ws.onerror = () => { try{ ws.close(); }catch(_){} };
  };
  term.onData((d) => { if (ws && ws.readyState===1) ws.send(new TextEncoder().encode(d)); });
  term.onResize(sendResize);
  window.addEventListener('resize', refit);
  connect();
  return { term, fit, refit, reset(){ try{ ws && ws.close(); }catch(_){} setTimeout(()=>{ term.reset(); connect(); }, 150); } };
}
function pick(k){
  document.querySelectorAll('.tab,.frame').forEach(el=>el.classList.toggle('active', el.dataset.k===k));
  const t = TERMS[k]; if(t){ t.refit(); setTimeout(()=>{ try{ t.term.focus(); }catch(e){} }, 50); }
}
async function load(){
 const rows=await (await fetch('api/sessions')).json();
 document.getElementById('list').innerHTML = rows.map(s=>`
  <div class=row>
   <div class=t onclick="act('local','load','${s.uuid}')">${esc(s.title)}</div>
   <div class=a>
     <button class="btn live sm" title="Promote to Live (Telegram)" onclick="act('live','load','${s.uuid}')">→Live</button>
     <button class="btn del sm" title="Delete session" onclick="del(event,'${s.uuid}','${esc(s.title).replace(/'/g,"&#39;")}')">✕</button>
   </div>
   <div class=m><span>${esc(s.project)}</span><span>${fmt(s.mtime)}</span></div>
  </div>`).join('') || '<div style="padding:8px;color:#666">no sessions</div>';
}
async function act(kind,action,uuid){
 const st=await (await fetch('api/status?kind='+kind)).json();
 if(st.busy && !confirm(kind+' is busy ('+st.reason+'). Replace it?')) return;
 pick(kind);
 // Server will send {"type":"respawn"} + close our WS; the client auto-resets and reconnects.
 await fetch('api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,action,uuid})});
}
function restartLive(){ act('live','new'); }
async function del(e,uuid,title){
 e.stopPropagation();
 if(!confirm('Delete session "'+title+'"? This removes its .jsonl file.')) return;
 const r=await (await fetch('api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uuid})})).json();
 if(!r.ok) alert('Delete failed: '+(r.err||'not found'));
 load();
}
window.addEventListener('load', ()=>{
  TERMS.local = mkTerm('local');
  TERMS.live  = mkTerm('live');
  setTimeout(()=>{ TERMS.local.refit(); try{ TERMS.local.term.focus(); }catch(e){} }, 300);
});
load(); setInterval(load, 15000);
</script></body></html>"""

@web.middleware
async def auth_mw(request: web.Request, handler):
    a = request.headers.get("Authorization", "")
    if a.startswith("Basic "):
        try:
            if base64.b64decode(a[6:]).decode() == CRED:
                return await handler(request)
        except Exception: pass
    return web.Response(status=401,
                        headers={"WWW-Authenticate": 'Basic realm="claude-harness"'})

async def h_index(_req):
    return web.Response(text=INDEX, content_type="text/html",
                        headers={"Cache-Control": "no-store"})

async def h_sessions(_req):
    return web.json_response(list_sessions())

async def h_status(request):
    kind = request.query.get("kind", "local")
    if kind not in KINDS:
        return web.json_response({"busy": False, "reason": "bad kind", "kind": kind})
    b, r = SESSIONS[kind].busy()
    return web.json_response({"busy": b, "reason": r, "kind": kind})

async def h_action(request):
    try: body = await request.json()
    except Exception: body = {}
    kind = body.get("kind", "local"); act = body.get("action"); uuid = body.get("uuid", "")
    if kind not in KINDS: return web.json_response({"ok": False, "err": "bad kind"})
    if act not in ("new", "load"): return web.json_response({"ok": False, "err": "bad action"})
    if act == "load" and not re.match(r"^[0-9a-f-]{36}$", uuid):
        return web.json_response({"ok": False, "err": "bad uuid"})
    try:
        await SESSIONS[kind].spawn(uuid if act == "load" else "")
    except Exception as e:
        import traceback; traceback.print_exc()
        return web.json_response({"ok": False, "err": f"spawn: {e}"})
    return web.json_response({"ok": True, "kind": kind})

async def h_delete(request):
    try: body = await request.json()
    except Exception: body = {}
    return web.json_response(do_delete(body.get("uuid", "")))

async def h_ws(request):
    kind = request.match_info["kind"]
    if kind not in KINDS: return web.Response(status=404)
    sess = SESSIONS[kind]
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    if not sess.alive():
        await sess.spawn()
    sess.clients.add(ws)
    try:
        snap = sess.buffer.snapshot()
        if snap: await ws.send_bytes(snap)
        # Nudge TUI to repaint for the new viewer (Ctrl-L).
        await sess.write(b"\x0c")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await sess.write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try: d = json.loads(msg.data)
                except Exception: continue
                if d.get("type") == "resize":
                    try:
                        await sess.resize(int(d.get("cols", 160)), int(d.get("rows", 50)))
                    except Exception: pass
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
    finally:
        sess.clients.discard(ws)
    return ws

async def on_startup(_app):
    # Prewarm live so Telegram is up without a browser.
    try: await SESSIONS["live"].spawn()
    except Exception as e: print(f"prewarm live failed: {e}", flush=True)

async def on_cleanup(_app):
    for s in SESSIONS.values():
        try: await s.kill()
        except Exception: pass

def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=4 * 1024 * 1024)
    app.router.add_get("/", h_index)
    app.router.add_get("/index.html", h_index)
    app.router.add_get("/api/sessions", h_sessions)
    app.router.add_get("/api/status", h_status)
    app.router.add_post("/api/action", h_action)
    app.router.add_post("/api/delete", h_delete)
    app.router.add_get("/ws/{kind}", h_ws)
    app.router.add_static("/static/", path=str(HERE / "static"), show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    print(f"dashboard on {BIND}:{PORT}", flush=True)
    web.run_app(make_app(), host=BIND, port=PORT, access_log=None)
