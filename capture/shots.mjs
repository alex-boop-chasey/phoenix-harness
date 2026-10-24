// Start the built site, screenshot each route full-page at desktop (1440x900) and mobile (390x844 @2x) with reduced
// motion into OUT/<slug>-desktop|mobile.png and OUT/shots.json. Exit 0 ok; 3 (OUT/errors.json) when the server never
// came up or a route answered >= 400; 2 for bad arguments.
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { chromium } from 'playwright'

const USAGE = 'usage: node capture/shots.mjs --start CMD --port P --routes R... --out DIR [--wait-ms 500] [--timeout-s 60]'
const MAX_HEIGHT = 6000
const VIEWPORTS = {
  desktop: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, isMobile: false },
  mobile: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true },
}

function parseArgs(argv) {
  const o = { start: null, port: null, routes: [], out: null, waitMs: 500, timeoutS: 60 }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--routes') {
      while (i + 1 < argv.length && !argv[i + 1].startsWith('--')) o.routes.push(argv[++i])
      continue
    }
    const v = argv[++i]
    if (v === undefined) throw new Error(`${a} needs a value`)
    if (a === '--start') o.start = v
    else if (a === '--out') o.out = v
    else if (a === '--port' || a === '--wait-ms' || a === '--timeout-s') {
      const n = Number(v)
      if (!Number.isInteger(n) || n < 0 || n > 65535) throw new Error(`${a} needs an integer from 0 to 65535`)
      o[a === '--port' ? 'port' : a === '--wait-ms' ? 'waitMs' : 'timeoutS'] = n
    } else throw new Error(`unknown argument ${a}`)
  }
  if (!o.start || !o.port || !o.out || !o.routes.length) throw new Error('--start, --port, --routes and --out are required')
  if (o.routes.some((r) => !r.startsWith('/'))) throw new Error('routes must start with /')
  return o
}

export function slug(route) {
  return route.replace(/^\/+|\/+$/g, '').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase() || 'home'
}

async function waitForServer(base, timeoutS) {
  const end = Date.now() + timeoutS * 1000
  while (Date.now() < end) {
    try {
      const r = await fetch(base + '/', { signal: AbortSignal.timeout(2000) })
      if (r.status < 500) return true
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250))
  }
  return false
}

async function main() {
  let o
  try {
    o = parseArgs(process.argv.slice(2))
  } catch (e) {
    console.error(`${e.message}\n${USAGE}`)
    return 2
  }
  fs.mkdirSync(o.out, { recursive: true })
  const base = `http://127.0.0.1:${o.port}`
  const server = spawn('sh', ['-c', o.start], { detached: true, stdio: 'ignore', env: { ...process.env, PORT: String(o.port) } })
  const errors = []
  const shots = []
  try {
    if (!(await waitForServer(base, o.timeoutS))) errors.push({ route: '*', error: 'server did not start' })
    else {
      const browser = await chromium.launch()
      try {
        for (const [name, dev] of Object.entries(VIEWPORTS)) {
          const ctx = await browser.newContext({ ...dev, reducedMotion: 'reduce', serviceWorkers: 'block' })
          for (const route of o.routes) {
            const page = await ctx.newPage()
            try {
              const resp = await page.goto(base + route, { waitUntil: 'load', timeout: 30000 })
              const status = resp ? resp.status() : 0
              if (status >= 400) {
                if (!errors.some((e) => e.route === route)) errors.push({ route, status })
                continue
              }
              await page.waitForTimeout(o.waitMs)
              const height = await page.evaluate(() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0))
              const file = `${slug(route)}-${name}.png`
              await page.screenshot({
                path: path.join(o.out, file),
                clip: { x: 0, y: 0, width: dev.viewport.width, height: Math.min(Math.max(height, 1), MAX_HEIGHT) },
                fullPage: true,
              })
              shots.push({ route, viewport: name, file })
            } catch (e) {
              if (!errors.some((x) => x.route === route)) errors.push({ route, error: String(e.message).slice(0, 200) })
            } finally {
              await page.close()
            }
          }
          await ctx.close()
        }
      } finally {
        await browser.close()
      }
    }
  } finally {
    try {
      process.kill(-server.pid, 'SIGKILL')
    } catch {
      // already gone
    }
  }
  if (shots.length) fs.writeFileSync(path.join(o.out, 'shots.json'), JSON.stringify(shots, null, 2))
  if (errors.length) fs.writeFileSync(path.join(o.out, 'errors.json'), JSON.stringify(errors, null, 2))
  return errors.length ? 3 : 0
}

if (import.meta.url === `file://${process.argv[1]}`) process.exit(await main())
