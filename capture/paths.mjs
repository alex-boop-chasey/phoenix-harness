// Pure helpers shared by the capture CLI (Node) and the in-page extractor (extract.mjs injects this
// file's source into the page, so: no imports, only declarations, nothing that needs Node).
// Nothing here reads or returns page content: element paths are tag names, classes and positions.

const PATH_RE = /^[A-Za-z0-9_.:#>()\- ]{1,200}$/
const PATH_MAX = 200
const CLASS_MAX = 40
const TOKEN_MAX = 120

// Keep only characters that are safe inside an element path / token; cut to `max`.
function cleanToken(value, max = TOKEN_MAX) {
  if (value === null || value === undefined) return ''
  return String(value).replace(/[^A-Za-z0-9_.:#>()\- ]/g, '').trim().slice(0, max)
}

// One path segment: tag, up to two class names (cut to 40 chars each), :nth-child(n) among element siblings.
function pathSegment(el) {
  let seg = String(el.tagName || '').toLowerCase().replace(/[^a-z0-9-]/g, '') || 'element'
  const classes = []
  const list = el.classList ? Array.from(el.classList) : []
  for (const cls of list) {
    const clean = String(cls).replace(/[^A-Za-z0-9_-]/g, '').slice(0, CLASS_MAX)
    if (clean) classes.push(clean)
    if (classes.length === 2) break
  }
  for (const cls of classes) seg += '.' + cls
  const parent = el.parentElement
  if (parent && parent.children) {
    const index = Array.prototype.indexOf.call(parent.children, el)
    if (index >= 0) seg += ':nth-child(' + (index + 1) + ')'
  }
  return seg
}

// Element path: segments from the root down, joined by ' > ', the innermost element always kept,
// ancestors dropped from the top until the whole thing fits in 200 chars. Matches PATH_RE.
function elementPath(el) {
  if (!el || el.nodeType !== 1) return ''
  const segments = []
  let node = el
  let hops = 0
  while (node && node.nodeType === 1 && hops < 64) {
    segments.unshift(pathSegment(node))
    node = node.parentElement
    hops++
  }
  let path = segments.join(' > ')
  while (path.length > PATH_MAX && segments.length > 1) {
    segments.shift()
    path = segments.join(' > ')
  }
  if (path.length > PATH_MAX) path = path.slice(path.length - PATH_MAX)
  return path
}

// Count uses of each string value: [{value, uses}] most used first, insertion order on ties, top n.
function aggregate(values, top = 40) {
  const counts = new Map()
  for (const v of values) {
    if (v === null || v === undefined || v === '') continue
    counts.set(v, (counts.get(v) || 0) + 1)
  }
  return Array.from(counts, ([value, uses]) => ({ value, uses }))
    .sort((a, b) => b.uses - a.uses)
    .slice(0, top)
}

// Timeline kind of a Web Animation: ViewTimeline extends ScrollTimeline, so view is checked first.
function timelineKind(animation) {
  const t = animation && animation.timeline
  if (!t || typeof t !== 'object') return 'document'
  const name = t.constructor && t.constructor.name
  if (name === 'ViewTimeline' || 'subject' in t) return 'view'
  if (name === 'ScrollTimeline' || 'source' in t) return 'scroll'
  return 'document'
}

// Split a CSS comma list at top-level commas only ('cubic-bezier(0, 0, 1, 1), linear' -> 2 items).
function splitCssList(text) {
  const out = []
  let depth = 0
  let current = ''
  for (const ch of String(text || '')) {
    if (ch === '(') depth++
    if (ch === ')') depth = Math.max(0, depth - 1)
    if (ch === ',' && depth === 0) {
      out.push(current.trim())
      current = ''
    } else {
      current += ch
    }
  }
  if (current.trim() !== '') out.push(current.trim())
  return out.filter((s) => s !== '')
}

// Non-trivial transitions of one element from its computed transition-* values, paired per property
// the way CSS does (shorter lists repeat). Zero-duration transitions are dropped.
function pairTransitions(cs) {
  const props = splitCssList(cs.transitionProperty)
  const durations = splitCssList(cs.transitionDuration)
  const timings = splitCssList(cs.transitionTimingFunction)
  const delays = splitCssList(cs.transitionDelay)
  const out = []
  props.forEach((property, i) => {
    const duration = durations.length ? durations[i % durations.length] : '0s'
    if (parseFloat(duration) === 0 || !property || property === 'none') return
    out.push({
      property,
      duration,
      timingFunction: timings.length ? timings[i % timings.length] : 'ease',
      delay: delays.length ? delays[i % delays.length] : '0s',
    })
  })
  return out
}

// {prop: {from, to}} for every property whose value differs between two style maps.
function diffStyles(before, after) {
  const out = {}
  for (const key of Object.keys(after || {})) {
    const from = before ? before[key] : undefined
    if (from !== after[key]) out[key] = { from: from === undefined ? null : from, to: after[key] }
  }
  return out
}

// Elements whose sampled style changed between two step snapshots (same position and path), capped.
function diffSnapshots(prev, next, limit = 50) {
  if (!prev || !next || limit <= 0) return []
  const out = []
  const n = Math.min(prev.length, next.length)
  for (let i = 0; i < n && out.length < limit; i++) {
    const a = prev[i]
    const b = next[i]
    if (!a || !b || a.path !== b.path) continue
    const { path, ...styles } = b
    const { path: _ignored, ...before } = a
    const changed = diffStyles(before, styles)
    if (Object.keys(changed).length) out.push({ path, changed })
  }
  return out
}

function finiteOrNull(n) {
  return typeof n === 'number' && Number.isFinite(n) ? n : null
}

// A number, or a CSSNumericValue-like {value, unit} (scroll timelines report percentages), as JSON.
function cssValue(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? Math.round(v * 100) / 100 : null
  if (v && typeof v === 'object' && typeof v.value === 'number') {
    const value = Math.round(v.value * 100) / 100
    return value + (v.unit === 'percent' ? '%' : String(v.unit || ''))
  }
  return null
}

const TIMING_NUMBERS = ['delay', 'endDelay', 'iterationStart', 'duration', 'progress', 'currentIteration', 'activeDuration', 'endTime', 'localTime']
const TIMING_STRINGS = ['fill', 'direction', 'easing']

// effect.getComputedTiming() reduced to documented fields; durations of scroll timelines are {} -> null.
function summariseTiming(timing) {
  if (!timing || typeof timing !== 'object') return null
  const out = {}
  for (const key of TIMING_NUMBERS) out[key] = finiteOrNull(timing[key])
  for (const key of TIMING_STRINGS) out[key] = typeof timing[key] === 'string' ? cleanToken(timing[key]) : null
  out.iterations = timing.iterations === Infinity ? 'infinite' : finiteOrNull(timing.iterations)
  return out
}

export {
  PATH_RE, PATH_MAX, CLASS_MAX, TOKEN_MAX, cleanToken, pathSegment, elementPath, aggregate, timelineKind, splitCssList,
  pairTransitions, diffStyles, diffSnapshots, finiteOrNull, cssValue, summariseTiming,
}
