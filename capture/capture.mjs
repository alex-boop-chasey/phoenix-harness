#!/usr/bin/env node
// Capture the look and motion of a reference site with Playwright Chromium.
//
//   node capture/capture.mjs --url <url> --out <dir> [--max-steps 24] [--viewports desktop,mobile] [--nav-timeout-ms 20000]
//
// Writes <out>/capture.json (schema 1), <out>/frames/<viewport>/step-NN.png, hover-NN.png and
// <out>/capture.log. Exit 0 ok, 2 bad arguments, 3 navigation failed (capture.json still written,
// with "error"). Page content is never recorded: see extract.mjs.
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { chromium, devices } from 'playwright'
import { parseArgs, UsageError, USAGE, VIEWPORTS } from './args.mjs'
import { pageScript } from './extract.mjs'
import { cleanToken, diffSnapshots, diffStyles, splitCssList } from './paths.mjs'

const STEP_SETTLE_MS = 400
const LOAD_SETTLE_MS = 1000
const INTERACTIONS_MAX = 20
const HOVER_FRAMES_MAX = 5
const INTERACTION_PROPS = ['color', 'background-color', 'transform', 'box-shadow', 'opacity', 'border-color']
const SAMPLE_LIMIT = 400
const ANIMATIONS_PER_STEP = 100
const WEB_ANIMATIONS_MAX = 200
const CHANGED_PER_STEP = 50

class NavigationError extends Error {}

function pad(n) {
  return String(n).padStart(2, '0')
}

class Log {
  constructor(file) {
    this.file = file
    this.started = Date.now()
    fs.writeFileSync(file, '')
  }

  line(message) {
    const t = ((Date.now() - this.started) / 1000).toFixed(1).padStart(6)
    fs.appendFileSync(this.file, `${t}s ${message}\n`)
  }
}

function contextOptions(name) {
  const size = VIEWPORTS[name]
  if (name === 'mobile') {
    const phone = devices['iPhone 14'] || {}
    return {
      viewport: { width: size.w, height: size.h },
      isMobile: true,
      hasTouch: true,
      deviceScaleFactor: 2,
      userAgent: phone.userAgent,
    }
  }
  return { viewport: { width: size.w, height: size.h }, deviceScaleFactor: 1 }
}

function firstLine(err) {
  return String(err && err.message ? err.message : err).split('\n')[0].slice(0, 300)
}

async function navigate(page, url, navTimeoutMs, log, name) {
  const started = Date.now()
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: navTimeoutMs })
  } catch (err) {
    throw new NavigationError(`${name}: navigation failed: ${firstLine(err)}`)
  }
  for (const state of ['load', 'networkidle']) {
    const remaining = navTimeoutMs - (Date.now() - started)
    if (remaining <= 0) break
    try {
      await page.waitForLoadState(state, { timeout: remaining })
    } catch {
      log.line(`${name}: ${state} not reached within the ${navTimeoutMs} ms cap, continuing`)
      break
    }
  }
  log.line(`${name}: navigated in ${Date.now() - started} ms`)
  await page.waitForTimeout(LOAD_SETTLE_MS)
}

function animationKey(a) {
  return [a.type, a.id, a.name, a.target, a.pseudo, a.timeline].join('|')
}

function compactAnimation(a) {
  return { type: a.type, id: a.id, name: a.name, target: a.target, timeline: a.timeline, playState: a.playState, currentTime: a.currentTime }
}

async function captureSteps(page, name, size, opts, outDir, log) {
  const steps = []
  const seen = new Map()
  const scroller = []
  let prevStyles = null
  let pageHeight = 0
  for (let i = 0; i < opts.maxSteps; i++) {
    const top = i * size.h
    await page.evaluate((y) => window.scrollTo({ top: y, left: 0, behavior: 'instant' }), top)
    await page.waitForTimeout(STEP_SETTLE_MS)
    const frame = path.posix.join('frames', name, `step-${pad(i)}.png`)
    await page.screenshot({ path: path.join(outDir, frame) })
    const snap = await page.evaluate(pageScript('snapshotStep', { sampleLimit: SAMPLE_LIMIT, animationLimit: ANIMATIONS_PER_STEP }))
    const last = scroller[scroller.length - 1]
    if (last && snap.scrollY <= last.scrollY && last.transforms.join('|') === snap.scrollerTransforms.join('|')) {
      fs.rmSync(path.join(outDir, frame), { force: true })
      log.line(`${name}: step ${i} dropped, the page stops scrolling at ${snap.scrollY} px`)
      break
    }
    const changed = diffSnapshots(prevStyles, snap.styles, CHANGED_PER_STEP)
    prevStyles = snap.styles
    pageHeight = snap.pageHeight
    for (const a of snap.animations) {
      const key = animationKey(a)
      const known = seen.get(key)
      if (known) {
        known.steps++
        known.lastPlayState = a.playState
      } else if (seen.size < WEB_ANIMATIONS_MAX) {
        seen.set(key, { ...a, firstStep: i, steps: 1, lastPlayState: a.playState })
      }
    }
    steps.push({ index: i, scrollY: snap.scrollY, frame, animations: snap.animations.map(compactAnimation), changed })
    scroller.push({ scrollY: snap.scrollY, transforms: snap.scrollerTransforms })
    log.line(`${name}: step ${i} at scrollY ${snap.scrollY} (page ${pageHeight} px, ${snap.animations.length} animations, ${changed.length} changed)`)
    if (top + size.h >= pageHeight) break
  }
  let transformScroller = false
  for (let i = 1; i < scroller.length; i++) {
    const a = scroller[i - 1]
    const b = scroller[i]
    if (a.scrollY === b.scrollY && (a.transforms[0] !== b.transforms[0] || a.transforms[1] !== b.transforms[1])) transformScroller = true
  }
  return { steps, pageHeight, webAnimations: Array.from(seen.values()), transformScroller }
}

function readElementState(handle) {
  return handle.evaluate((el, props) => {
    const cs = getComputedStyle(el)
    const styles = {}
    for (const p of props) styles[p] = cs.getPropertyValue(p)
    let animations = []
    try {
      animations = el.getAnimations().map((a) => a.id || a.animationName || a.transitionProperty || a.constructor.name)
    } catch { animations = [] }
    return { styles, animations, transitionDuration: cs.transitionDuration, borderWidth: parseFloat(cs.borderTopWidth) || 0 }
  }, INTERACTION_PROPS)
}

// Wait long enough for the element's own transitions (at least 250 ms, at most 1.2 s).
function settleMs(transitionDuration) {
  let max = 0
  for (const part of splitCssList(transitionDuration)) {
    const n = parseFloat(part)
    if (!Number.isFinite(n)) continue
    max = Math.max(max, /ms$/.test(part) ? n : n * 1000)
  }
  return Math.min(1200, Math.max(250, max + 100))
}

function interactionRecord(kind, item, before, after) {
  const started = after.animations.filter((a) => !before.animations.includes(a)).map((a) => cleanToken(a, 60)).filter(Boolean)
  const diff = diffStyles(before.styles, after.styles)
  if (before.borderWidth === 0 && after.borderWidth === 0) delete diff['border-color']
  return { kind, tag: item.tag, target: item.path, diff, started: started.slice(0, 10) }
}

// Hover, focus and (buttons only) pressed states of the first visible links and buttons. Links are
// never clicked; the press is released with click count 0 so no click event fires.
async function captureInteractions(page, name, outDir, log) {
  const records = []
  let items = []
  try {
    items = await page.evaluate(pageScript('listInteractive', { max: INTERACTIONS_MAX }))
  } catch (err) {
    log.line(`${name}: listing links and buttons failed: ${firstLine(err)}`)
    return records
  }
  const handles = await page.$$('a, button')
  let hovered = 0
  for (const item of items) {
    const handle = handles[item.index]
    if (!handle) continue
    try {
      await handle.scrollIntoViewIfNeeded({ timeout: 2000 })
      await page.waitForTimeout(150)
      const before = await readElementState(handle)
      const wait = settleMs(before.transitionDuration)

      await handle.hover({ timeout: 2000 })
      await page.waitForTimeout(wait)
      const hoverState = await readElementState(handle)
      const hover = interactionRecord('hover', item, before, hoverState)
      if (hovered < HOVER_FRAMES_MAX) {
        hover.frame = path.posix.join('frames', name, `hover-${pad(hovered)}.png`)
        await page.screenshot({ path: path.join(outDir, hover.frame) })
      }
      hovered++
      records.push(hover)

      if (item.tag === 'button') {
        await page.mouse.down({ clickCount: 0 })
        await page.waitForTimeout(wait)
        const pressedState = await readElementState(handle)
        await page.mouse.up({ clickCount: 0 })
        records.push(interactionRecord('pressed', item, hoverState, pressedState))
      }
      await page.mouse.move(0, 0)
      await page.waitForTimeout(wait)

      await handle.evaluate((el) => el.focus({ preventScroll: true }))
      await page.waitForTimeout(wait)
      const focusState = await readElementState(handle)
      records.push(interactionRecord('focus', item, before, focusState))
      await handle.evaluate((el) => el.blur())
      await page.waitForTimeout(100)
    } catch (err) {
      log.line(`${name}: interaction on ${item.path} skipped: ${firstLine(err)}`)
    }
  }
  log.line(`${name}: ${records.length} interaction records on ${items.length} elements`)
  return records
}

async function captureViewport(browser, name, opts, outDir, log, withInteractions) {
  const size = VIEWPORTS[name]
  const context = await browser.newContext(contextOptions(name))
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  page.on('dialog', (dialog) => { dialog.dismiss().catch(() => {}) })
  page.on('popup', (popup) => { popup.close().catch(() => {}) })
  fs.mkdirSync(path.join(outDir, 'frames', name), { recursive: true })
  try {
    await navigate(page, opts.url, opts.navTimeoutMs, log, name)
    const motion = await captureSteps(page, name, size, opts, outDir, log)
    const interactions = withInteractions ? await captureInteractions(page, name, outDir, log) : []
    await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }))
    await page.waitForTimeout(STEP_SETTLE_MS)
    const fixed = await page.evaluate(pageScript('extractStatic', { vw: size.w, vh: size.h }))
    log.line(`${name}: static extraction done (${fixed.structure.length} structure entries, ${fixed.keyframes.length} keyframes)`)
    return {
      size: { w: size.w, h: size.h },
      pageHeight: Math.max(motion.pageHeight, fixed.pageHeight),
      steps: motion.steps,
      structure: fixed.structure,
      colors: fixed.colors,
      fonts: fixed.fonts,
      spacing: fixed.spacing,
      radii: fixed.radii,
      shadows: fixed.shadows,
      graphics: fixed.graphics,
      keyframes: fixed.keyframes,
      crossOriginSheetsSkipped: fixed.crossOriginSheetsSkipped,
      transitions: fixed.transitions,
      animationTimelines: fixed.animationTimelines,
      webAnimations: motion.webAnimations,
      gsap: fixed.gsap,
      smoothScroll: { ...fixed.smoothScroll, transformScroller: motion.transformScroller },
      interactions,
    }
  } finally {
    await context.close().catch(() => {})
  }
}

function writeJson(outDir, result) {
  fs.writeFileSync(path.join(outDir, 'capture.json'), JSON.stringify(result, null, 2) + '\n')
}

async function main(argv) {
  let opts
  try {
    opts = parseArgs(argv)
  } catch (err) {
    if (!(err instanceof UsageError)) throw err
    process.stderr.write(`capture: ${err.message}\n${USAGE}\n`)
    return 2
  }
  const outDir = path.resolve(opts.out)
  fs.mkdirSync(outDir, { recursive: true })
  const log = new Log(path.join(outDir, 'capture.log'))
  const result = { schema: 1, url: opts.url, capturedAt: new Date().toISOString(), viewports: {} }
  log.line(`capture of ${opts.url} (viewports ${opts.viewports.join(', ')}, max ${opts.maxSteps} steps, nav cap ${opts.navTimeoutMs} ms)`)
  let browser = null
  let code = 0
  try {
    browser = await chromium.launch()
    for (const [index, name] of opts.viewports.entries()) {
      result.viewports[name] = await captureViewport(browser, name, opts, outDir, log, index === 0)
    }
  } catch (err) {
    result.error = firstLine(err)
    code = err instanceof NavigationError ? 3 : 1
    log.line(`error: ${result.error}`)
  } finally {
    if (browser) await browser.close().catch(() => {})
  }
  writeJson(outDir, result)
  log.line(`done, exit ${code}`)
  return code
}

if (process.argv[1] && path.resolve(process.argv[1]) === new URL(import.meta.url).pathname) {
  main(process.argv.slice(2)).then(
    (code) => { process.exitCode = code },
    (err) => {
      process.stderr.write(`capture: ${firstLine(err)}\n`)
      process.exitCode = 1
    },
  )
}
