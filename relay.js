'use strict';
// Single-port HTTP+WS reverse proxy with cookie auth. Everything forwards to
// the python dashboard on loopback (which owns the PTYs via ptyprocess and
// serves /ws/<kind> for xterm.js directly — no more ttyd).
const http = require('http');
const net = require('net');
const fs = require('fs');
const crypto = require('crypto');

const LISTEN_PORT = Number(process.env.RELAY_LISTEN_PORT || 8681);
const DASH_PORT = Number(process.env.RELAY_DASH_PORT || 9001);
const CRED_FILE = process.env.RELAY_CRED_FILE || '/home/node/claude-harness/.cred';
const CRED = fs.readFileSync(CRED_FILE, 'utf8').trim();
const [CRED_USER, ...CRED_PW_PARTS] = CRED.split(':');
const CRED_PW = CRED_PW_PARTS.join(':');
const SESS_TOKEN = crypto.createHash('sha256').update('claude-harness-v1:' + CRED).digest('hex');
const UPSTREAM_AUTH = 'Basic ' + Buffer.from(CRED).toString('base64');
const COOKIE_NAME = 'cha_sess';
const REMEMBER_MAX_AGE = 60 * 60 * 24 * 90; // 90 days

function routeFor(_url) { return DASH_PORT; }

function parseCookies(h) {
  const out = {};
  const c = h && h.cookie;
  if (!c) return out;
  for (const part of c.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function checkAuth(req) {
  const c = parseCookies(req.headers);
  if (!c[COOKIE_NAME]) return false;
  try {
    const a = Buffer.from(c[COOKIE_NAME], 'utf8');
    const b = Buffer.from(SESS_TOKEN, 'utf8');
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  } catch { return false; }
}

function isApiPath(url) {
  return url.startsWith('/api/') || url.startsWith('/ws/');
}

function unauth(req, res) {
  if (isApiPath(req.url)) {
    res.writeHead(401, { 'Content-Length': '0' });
    res.end();
  } else {
    const to = '/login?next=' + encodeURIComponent(req.url || '/');
    res.writeHead(302, { Location: to, 'Content-Length': '0' });
    res.end();
  }
}

const LOGIN_HTML = (err, next, user) => `<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Claude Harness · Sign in</title>
<style>
 html,body{margin:0;height:100%;font:14px/1.4 -apple-system,system-ui,sans-serif;background:#0b0d10;color:#d6dadf;display:flex;align-items:center;justify-content:center}
 .card{background:#161a20;border:1px solid #262c33;border-radius:10px;padding:22px 24px;width:320px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
 h1{margin:0 0 14px;font-size:16px}
 label{display:block;font-size:12px;color:#8a93a0;margin:10px 0 4px}
 input[type=text],input[type=password]{width:100%;box-sizing:border-box;background:#0b0d10;border:1px solid #2a3038;border-radius:6px;color:#eaecef;padding:9px 10px;font:inherit}
 input[type=text]:focus,input[type=password]:focus{outline:0;border-color:#1f6feb}
 .row{display:flex;align-items:center;gap:6px;margin-top:12px;color:#aab;font-size:12.5px}
 .row input{accent-color:#1f6feb}
 button{margin-top:16px;width:100%;background:#1f6feb;color:#fff;border:0;padding:10px;border-radius:6px;cursor:pointer;font:inherit;font-weight:500}
 button:hover{background:#2b7ff5}
 .err{margin-top:10px;color:#f1a9a9;font-size:12px;min-height:14px}
</style></head><body>
<form class=card method=POST action="/login" autocomplete=on>
 <h1>Claude Harness</h1>
 <input type=hidden name=next value="${(next||'/').replace(/"/g,'&quot;')}">
 <label for=u>Username</label>
 <input id=u name=username type=text autocomplete=username autocapitalize=none autocorrect=off spellcheck=false value="${(user||'').replace(/"/g,'&quot;')}" required>
 <label for=p>Password</label>
 <input id=p name=password type=password autocomplete=current-password required>
 <label class=row><input type=checkbox name=remember value=1 checked> Remember password</label>
 <button type=submit>Sign in</button>
 <div class=err>${err||''}</div>
</form></body></html>`;

function readBody(req, cb) {
  const chunks = [];
  let total = 0;
  req.on('data', (b) => { total += b.length; if (total > 4096) { req.destroy(); return; } chunks.push(b); });
  req.on('end', () => cb(Buffer.concat(chunks).toString('utf8')));
  req.on('error', () => cb(''));
}

function parseForm(s) {
  const out = {};
  for (const pair of s.split('&')) {
    if (!pair) continue;
    const i = pair.indexOf('=');
    const k = decodeURIComponent((i < 0 ? pair : pair.slice(0, i)).replace(/\+/g, ' '));
    const v = i < 0 ? '' : decodeURIComponent(pair.slice(i + 1).replace(/\+/g, ' '));
    out[k] = v;
  }
  return out;
}

function sendLogin(res, code, err, next, user) {
  const body = LOGIN_HTML(err, next, user);
  res.writeHead(code, { 'Content-Type': 'text/html; charset=utf-8', 'Content-Length': Buffer.byteLength(body), 'Cache-Control': 'no-store' });
  res.end(body);
}

function safeNext(n) {
  if (!n || typeof n !== 'string') return '/';
  if (!n.startsWith('/') || n.startsWith('//')) return '/';
  if (n.startsWith('/login') || n.startsWith('/logout')) return '/';
  return n;
}

function cleanHeaders(h) {
  const out = { ...h };
  for (const k of Object.keys(out)) {
    if (k === 'forwarded' || k === 'x-real-ip' || k.startsWith('x-forwarded-') || k.startsWith('tailscale-')) delete out[k];
  }
  return out;
}

const server = http.createServer((req, res) => {
  const url = req.url || '/';
  if (url === '/login' || url.startsWith('/login?')) {
    const q = url.includes('?') ? url.slice(url.indexOf('?') + 1) : '';
    const nextVal = safeNext(parseForm(q).next);
    if (req.method === 'POST') {
      return readBody(req, (body) => {
        const f = parseForm(body);
        if (f.username === CRED_USER && f.password === CRED_PW) {
          const parts = [`${COOKIE_NAME}=${SESS_TOKEN}`, 'Path=/', 'HttpOnly', 'SameSite=Lax'];
          if (f.remember) parts.push(`Max-Age=${REMEMBER_MAX_AGE}`);
          res.writeHead(302, { 'Set-Cookie': parts.join('; '), Location: safeNext(f.next), 'Content-Length': '0' });
          return res.end();
        }
        sendLogin(res, 401, 'Invalid username or password.', safeNext(f.next), f.username);
      });
    }
    return sendLogin(res, 200, '', nextVal, CRED_USER);
  }
  if (url === '/logout') {
    res.writeHead(302, {
      'Set-Cookie': `${COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`,
      Location: '/login',
      'Content-Length': '0',
    });
    return res.end();
  }
  if (!checkAuth(req)) return unauth(req, res);
  const port = routeFor(url);
  const headers = cleanHeaders(req.headers);
  headers.host = `127.0.0.1:${port}`;
  headers.authorization = UPSTREAM_AUTH;
  const upstream = http.request({ host: '127.0.0.1', port, method: req.method, path: url, headers }, (ures) => {
    res.writeHead(ures.statusCode || 502, ures.headers);
    ures.pipe(res);
  });
  upstream.on('error', (e) => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain' });
    res.end('relay upstream error: ' + e.code);
  });
  req.pipe(upstream);
});

// WebSocket upgrade: enforce auth, splice to the right loopback service.
server.on('upgrade', (req, socket, head) => {
  if (!checkAuth(req)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }
  const port = routeFor(req.url);
  const upstream = net.connect(port, '127.0.0.1', () => {
    const headers = cleanHeaders(req.headers);
    headers.host = `127.0.0.1:${port}`;
    headers.authorization = UPSTREAM_AUTH;
    const lines = [`${req.method} ${req.url} HTTP/1.1`];
    for (const [k, v] of Object.entries(headers)) {
      if (Array.isArray(v)) for (const vv of v) lines.push(`${k}: ${vv}`);
      else lines.push(`${k}: ${v}`);
    }
    upstream.write(lines.join('\r\n') + '\r\n\r\n');
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on('error', () => socket.destroy());
  socket.on('error', () => upstream.destroy());
});

server.listen(LISTEN_PORT, '0.0.0.0', () => {
  console.log(new Date().toISOString(), `[relay] :${LISTEN_PORT} -> dash :${DASH_PORT} (HTTP + /ws/*)`);
});
