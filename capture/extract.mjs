// In-page extractor. The functions below run inside the captured page through page.evaluate; they are
// serialised as source together with capture/paths.mjs, so they may only use what that file declares,
// what they declare themselves, their argument, and browser globals.
//
// Content never leaves the page: no textContent, innerText, innerHTML, outerHTML, alt, title, src,
// href, data attributes, cookies or storage are read. Everything recorded is a number, a CSS value,
// a class/tag/animation name or an element path.
import { readFileSync } from 'node:fs'

const helpersSource = readFileSync(new URL('./paths.mjs', import.meta.url), 'utf8').replace(/\nexport \{[^}]*\}\s*$/, '\n')

// Tags that are never part of the look of a page.
function phxSkipTag(tag) {
  return tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK' || tag === 'META' || tag === 'NOSCRIPT' ||
    tag === 'TEMPLATE' || tag === 'TITLE' || tag === 'HEAD' || tag === 'BR' || tag === 'WBR'
}

// Elements that normally carry text: used to count text colour and fonts without reading any text.
function phxTextTag(tag) {
  return /^(P|H[1-6]|A|BUTTON|SPAN|LI|LABEL|TD|TH|BLOCKQUOTE|STRONG|EM|B|I|SMALL|INPUT|TEXTAREA|SELECT|DT|DD|FIGCAPTION|CITE|CODE|PRE|TIME|SUMMARY|LEGEND|CAPTION|Q|SUP|SUB|MARK|ADDRESS)$/.test(tag)
}

// Rendered or laid out with a size: a transform-collapsed element (scaleX(0) at the start of a
// scroll-driven animation) still counts, display:none and visibility:hidden do not.
function phxVisible(el, cs) {
  const style = cs || getComputedStyle(el)
  if (style.visibility === 'hidden' || style.display === 'none') return false
  if (el.offsetWidth > 0 && el.offsetHeight > 0) return true
  const r = el.getBoundingClientRect()
  return r.width > 0 && r.height > 0
}

// A CSS value as recorded: url(...) blanked (never a src), cut to 120 chars.
function phxCssText(value) {
  return String(value === null || value === undefined ? '' : value).replace(/url\([^)]*\)/g, 'url()').slice(0, 120)
}

function phxRound(n) {
  return Math.round(n * 10) / 10
}

function phxPageHeight() {
  const doc = document.documentElement
  return Math.max(doc ? doc.scrollHeight : 0, document.body ? document.body.scrollHeight : 0)
}

// Document-order elements under body minus the never-visual tags, capped.
function phxElements(limit) {
  const out = []
  if (!document.body) return out
  const all = document.body.getElementsByTagName('*')
  for (let i = 0; i < all.length && out.length < limit; i++) {
    if (!phxSkipTag(all[i].tagName)) out.push(all[i])
  }
  return out
}

// The ARIA role of an element, only when it is a known role name (never free text).
function phxRole(el) {
  const role = el.getAttribute('role')
  if (!role) return null
  const token = role.trim().toLowerCase().split(/\s+/)[0]
  return /^(banner|navigation|main|contentinfo|complementary|region|search|form|dialog|alertdialog|list|listitem|button|link|tablist|tab|tabpanel|menu|menubar|menuitem|presentation|none|img|figure|article|heading|group|toolbar|status|alert|feed|grid|table|row|cell|switch|checkbox|radio|slider|progressbar|separator|tooltip|marquee|note|document|application)$/.test(token) ? token : null
}

// Elements likely to move: a transition, an animation or will-change. Sampled first for step diffs.
function phxInteresting(cs) {
  return cs.animationName !== 'none' || cs.willChange !== 'auto' || parseFloat(cs.transitionDuration) > 0 ||
    (cs.transitionDuration || '').indexOf(',') >= 0
}

function phxKeyframesOf(effect) {
  if (!effect || typeof effect.getKeyframes !== 'function') return []
  let frames = []
  try { frames = effect.getKeyframes() } catch (e) { return [] }
  return frames.slice(0, 20).map((frame) => {
    const out = {}
    for (const key of Object.keys(frame)) {
      if (key === 'content') continue
      const v = frame[key]
      if (typeof v === 'number') out[key] = finiteOrNull(v)
      else if (typeof v === 'string') out[key] = phxCssText(v)
      else if (v === null) out[key] = null
    }
    return out
  })
}

function phxAnimationRecord(a, full) {
  const effect = a.effect
  let target = null
  try { target = effect && effect.target } catch (e) { target = null }
  const out = {
    type: cleanToken(a.constructor && a.constructor.name, 30) || 'Animation',
    id: cleanToken(a.id, 60) || null,
    name: cleanToken(a.animationName || a.transitionProperty || '', 60) || null,
    target: target && target.nodeType === 1 ? elementPath(target) : '',
    pseudo: effect && effect.pseudoElement ? cleanToken(effect.pseudoElement, 20) : null,
    timeline: timelineKind(a),
    playState: cleanToken(a.playState, 20),
    currentTime: cssValue(a.currentTime),
  }
  if (full) {
    out.keyframes = phxKeyframesOf(effect)
    let timing = null
    try { timing = effect ? summariseTiming(effect.getComputedTiming()) : null } catch (e) { timing = null }
    out.timing = timing
  }
  return out
}

function phxAnimations(limit, full) {
  let list = []
  try { list = document.getAnimations() } catch (e) { return [] }
  return list.slice(0, limit).map((a) => phxAnimationRecord(a, full))
}

// One scroll step: scroll position, page height, the scroller transforms (smooth-scroll detection),
// a style sample of up to `sampleLimit` elements (diffed in Node) and the live web animations.
function snapshotStep(opts) {
  const all = phxElements(5000)
  const interesting = []
  const rest = []
  for (const el of all) {
    const cs = getComputedStyle(el)
    if (interesting.length < opts.sampleLimit && phxInteresting(cs)) interesting.push(el)
    else rest.push(el)
  }
  const sample = interesting.concat(rest).slice(0, opts.sampleLimit)
  const styles = sample.map((el) => {
    const cs = getComputedStyle(el)
    return { path: elementPath(el), opacity: cs.opacity, transform: cs.transform, clipPath: cs.clipPath, visibility: cs.visibility }
  })
  const body = document.body
  const wrapper = body && body.firstElementChild
  return {
    scrollY: Math.round(window.scrollY),
    pageHeight: phxPageHeight(),
    scrollerTransforms: [body ? getComputedStyle(body).transform : 'none', wrapper ? getComputedStyle(wrapper).transform : 'none'],
    styles,
    animations: phxAnimations(opts.animationLimit, true),
  }
}

// Visible links and buttons in document order: their index in querySelectorAll('a, button') (so Node
// can find the same handles), tag and path. Up to opts.max.
function listInteractive(opts) {
  const all = document.querySelectorAll('a, button')
  const out = []
  for (let i = 0; i < all.length && out.length < opts.max; i++) {
    const el = all[i]
    const cs = getComputedStyle(el)
    if (!phxVisible(el, cs) || cs.pointerEvents === 'none') continue
    out.push({ index: i, tag: el.tagName.toLowerCase(), path: elementPath(el) })
  }
  return out
}

function phxStructure(limit, vw, vh) {
  const out = []
  let visited = 0
  const walk = (el, depth) => {
    for (const child of Array.from(el.children)) {
      if (out.length >= limit || visited++ > 8000) return
      const tag = child.tagName
      if (phxSkipTag(tag)) continue
      const r = child.getBoundingClientRect()
      const role = phxRole(child)
      const landmark = /^(HEADER|NAV|MAIN|SECTION|ARTICLE|ASIDE|FOOTER|FORM|FIGURE|DIALOG|UL|OL|TABLE)$/.test(tag) || role !== null
      const big = r.width >= vw * 0.5 && r.height >= vh * 0.25
      let next = depth
      if ((landmark || big) && r.width > 0 && r.height > 0) {
        next = depth + 1
        out.push({
          tag: tag.toLowerCase(),
          role,
          depth: next,
          box: { x: Math.round(r.left + window.scrollX), y: Math.round(r.top + window.scrollY), w: Math.round(r.width), h: Math.round(r.height) },
          children: child.children.length,
          path: elementPath(child),
        })
      }
      if (next < 3) walk(child, next)
    }
  }
  if (document.body) walk(document.body, 0)
  return out
}

function phxKeyframeRules(limit) {
  const out = []
  let skipped = 0
  const visit = (rules) => {
    for (const rule of Array.from(rules || [])) {
      if (out.length >= limit) return
      if (typeof CSSKeyframesRule !== 'undefined' && rule instanceof CSSKeyframesRule) {
        out.push({
          name: cleanToken(rule.name, 60),
          frames: Array.from(rule.cssRules).slice(0, 20).map((k) => {
            const properties = {}
            for (let i = 0; i < k.style.length && i < 20; i++) {
              const prop = k.style[i]
              if (prop === 'content') continue
              properties[prop] = phxCssText(k.style.getPropertyValue(prop))
            }
            return { offset: phxCssText(k.keyText).slice(0, 40), properties }
          }),
        })
      } else if (typeof CSSImportRule !== 'undefined' && rule instanceof CSSImportRule) {
        try { visit(rule.styleSheet && rule.styleSheet.cssRules) } catch (e) { skipped++ }
      } else if (rule.cssRules) {
        visit(rule.cssRules)
      }
    }
  }
  const sheets = Array.from(document.styleSheets).concat(Array.from(document.adoptedStyleSheets || []))
  for (const sheet of sheets) {
    let rules
    try { rules = sheet.cssRules } catch (e) { skipped++; continue }
    visit(rules)
  }
  return { keyframes: out, crossOriginSheetsSkipped: skipped }
}

function phxGraphics(limit) {
  const out = { svg: 0, img: 0, video: 0, canvas: 0, backgroundImages: 0, sizes: [] }
  for (const kind of ['svg', 'img', 'video', 'canvas']) {
    const nodes = document.getElementsByTagName(kind)
    out[kind] = nodes.length
    for (let i = 0; i < nodes.length && out.sizes.length < limit; i++) {
      const r = nodes[i].getBoundingClientRect()
      if (r.width > 0 && r.height > 0) out.sizes.push({ kind, w: Math.round(r.width), h: Math.round(r.height) })
    }
  }
  return out
}

function phxValueOrNull(v) {
  if (typeof v === 'boolean') return v
  if (typeof v === 'number') return finiteOrNull(v)
  if (typeof v === 'string') return phxCssText(v).slice(0, 60)
  if (typeof v === 'function') return 'function'
  if (v && typeof v === 'object' && v.nodeType === 1) return elementPath(v)
  return v === null || v === undefined ? null : typeof v
}

function phxScrollTrigger(st) {
  const vars = st.vars && typeof st.vars === 'object' ? st.vars : {}
  let animationDuration = null
  let animationTargets = 0
  try { animationDuration = st.animation && typeof st.animation.duration === 'function' ? finiteOrNull(st.animation.duration()) : null } catch (e) { animationDuration = null }
  try { animationTargets = st.animation && typeof st.animation.targets === 'function' ? st.animation.targets().length : 0 } catch (e) { animationTargets = 0 }
  const scrub = vars.scrub !== undefined ? vars.scrub : st.scrub
  return {
    trigger: st.trigger && st.trigger.nodeType === 1 ? elementPath(st.trigger) : '',
    start: phxValueOrNull(st.start),
    end: phxValueOrNull(st.end),
    startVar: phxValueOrNull(vars.start),
    endVar: phxValueOrNull(vars.end),
    scrub: scrub === undefined || scrub === null ? false : phxValueOrNull(scrub),
    pin: Boolean(st.pin || vars.pin),
    toggleActions: typeof vars.toggleActions === 'string' ? cleanToken(vars.toggleActions, 60) : null,
    markers: Boolean(vars.markers),
    animationDuration,
    animationTargets,
  }
}

function phxTween(child) {
  let duration = null
  let targets = 0
  let children = 0
  try { duration = typeof child.duration === 'function' ? finiteOrNull(child.duration()) : null } catch (e) { duration = null }
  try { targets = typeof child.targets === 'function' ? child.targets().length : 0 } catch (e) { targets = 0 }
  try { children = typeof child.getChildren === 'function' ? child.getChildren().length : 0 } catch (e) { children = 0 }
  const keys = child.vars && typeof child.vars === 'object' ? Object.keys(child.vars) : []
  return {
    kind: typeof child.getChildren === 'function' ? 'timeline' : 'tween',
    duration,
    targets,
    children,
    varsKeys: keys.map((k) => cleanToken(k, 40)).filter(Boolean).slice(0, 20),
  }
}

function phxGsap() {
  const gsap = window.gsap
  if (!gsap || (typeof gsap !== 'object' && typeof gsap !== 'function')) return { present: false }
  const out = { present: true, version: cleanToken(gsap.version, 20) || null, scrollTriggers: [], timelines: [] }
  let st = window.ScrollTrigger
  try { if (!st && gsap.core && gsap.core.globals) st = gsap.core.globals().ScrollTrigger } catch (e) { st = null }
  try {
    if (st && typeof st.getAll === 'function') out.scrollTriggers = Array.from(st.getAll()).slice(0, 100).map(phxScrollTrigger)
  } catch (e) { out.scrollTriggers = [] }
  try {
    const tl = gsap.globalTimeline
    if (tl && typeof tl.getChildren === 'function') out.timelines = Array.from(tl.getChildren(true, true, true)).slice(0, 100).map(phxTween)
  } catch (e) { out.timelines = [] }
  return out
}

// Plain options of a smooth-scroll library instance: numbers, booleans, short strings, nothing else.
function phxLibraryOptions(candidates) {
  for (const c of candidates) {
    if (!c || typeof c !== 'object' || !c.options || typeof c.options !== 'object') continue
    const out = {}
    for (const key of Object.keys(c.options).slice(0, 40)) {
      const name = cleanToken(key, 40)
      const v = c.options[key]
      if (!name) continue
      if (typeof v === 'number') out[name] = finiteOrNull(v)
      else if (typeof v === 'boolean') out[name] = v
      else if (typeof v === 'string') out[name] = cleanToken(v, 60)
      else if (typeof v === 'function') out[name] = 'function'
      else if (v && typeof v === 'object' && v.nodeType === 1) out[name] = elementPath(v)
    }
    return out
  }
  return null
}

function phxSmoothScroll() {
  const html = cleanToken(getComputedStyle(document.documentElement).scrollBehavior, 20)
  const body = document.body ? cleanToken(getComputedStyle(document.body).scrollBehavior, 20) : 'auto'
  const scroll = typeof window.scroll === 'object' ? window.scroll : null
  return {
    lenis: phxLibraryOptions([window.lenis, window.__lenis, window.Lenis]),
    locomotive: phxLibraryOptions([window.locomotive, window.locomotiveScroll, scroll]),
    scrollBehavior: { html, body },
    cssScrollBehavior: html !== 'auto' ? html : body,
  }
}

// Everything that does not depend on the scroll position.
function extractStatic(opts) {
  const elements = phxElements(5000)
  const colorUses = new Map()
  const addColor = (value, property) => {
    if (!value || value === 'rgba(0, 0, 0, 0)' || value === 'transparent') return
    const entry = colorUses.get(value) || { value, uses: 0, properties: [] }
    entry.uses++
    if (entry.properties.indexOf(property) < 0) entry.properties.push(property)
    colorUses.set(value, entry)
  }
  const families = new Map()
  const sizes = []
  const lineHeights = new Map()
  const paddings = []
  const margins = []
  const gaps = []
  const radii = []
  const shadows = []
  const transitionKeys = new Map()
  const animationTimelines = []
  let backgroundImages = 0

  for (const el of elements) {
    const cs = getComputedStyle(el)
    const timeline = cs.animationTimeline
    if (typeof timeline === 'string' && timeline !== '' && timeline !== 'auto' && animationTimelines.length < 50) {
      animationTimelines.push({ path: elementPath(el), value: phxCssText(timeline), animationName: cleanToken(cs.animationName, 60) })
    }
    if (!phxVisible(el, cs)) continue
    const textish = phxTextTag(el.tagName) || (el.children.length === 0 && el.childNodes.length > 0)
    if (textish) {
      addColor(cs.color, 'color')
      const stack = phxCssText(cs.fontFamily)
      const family = stack.split(',')[0].replace(/["']/g, '').trim()
      if (family) {
        const entry = families.get(family) || { family, stack, uses: 0, weights: new Map() }
        entry.uses++
        entry.weights.set(cs.fontWeight, (entry.weights.get(cs.fontWeight) || 0) + 1)
        families.set(family, entry)
      }
      const px = phxRound(parseFloat(cs.fontSize))
      if (Number.isFinite(px)) sizes.push(px)
      const lh = parseFloat(cs.lineHeight)
      const lhValue = Number.isFinite(lh) ? phxRound(lh) + 'px' : cleanToken(cs.lineHeight, 20)
      const lhEntry = lineHeights.get(lhValue) || { value: lhValue, ratio: Number.isFinite(lh) && px > 0 ? Math.round((lh / px) * 100) / 100 : null, uses: 0 }
      lhEntry.uses++
      lineHeights.set(lhValue, lhEntry)
    }
    addColor(cs.backgroundColor, 'background-color')
    if (parseFloat(cs.borderTopWidth) > 0 || parseFloat(cs.borderBottomWidth) > 0 || parseFloat(cs.borderLeftWidth) > 0 || parseFloat(cs.borderRightWidth) > 0) {
      addColor(cs.borderTopColor, 'border-color')
    }
    for (const v of new Set([cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft])) if (parseFloat(v) > 0) paddings.push(v)
    for (const v of new Set([cs.marginTop, cs.marginRight, cs.marginBottom, cs.marginLeft])) if (parseFloat(v) !== 0 && v !== 'auto') margins.push(v)
    if (/flex|grid/.test(cs.display)) {
      for (const v of new Set([cs.rowGap, cs.columnGap])) if (v && v !== 'normal' && parseFloat(v) > 0) gaps.push(v)
    }
    if (cs.borderRadius && parseFloat(cs.borderRadius) > 0) radii.push(phxCssText(cs.borderRadius))
    if (cs.boxShadow && cs.boxShadow !== 'none') shadows.push(phxCssText(cs.boxShadow))
    if (cs.backgroundImage && cs.backgroundImage.indexOf('url(') >= 0) backgroundImages++
    for (const t of pairTransitions(cs)) {
      const key = [t.property, t.duration, t.timingFunction, t.delay].join('|')
      const entry = transitionKeys.get(key) || Object.assign({}, t, { uses: 0 })
      entry.uses++
      transitionKeys.set(key, entry)
    }
  }

  const byUses = (a, b) => b.uses - a.uses
  const keyframeRules = phxKeyframeRules(100)
  const graphics = phxGraphics(100)
  graphics.backgroundImages = backgroundImages
  return {
    pageHeight: phxPageHeight(),
    structure: phxStructure(200, opts.vw, opts.vh),
    colors: Array.from(colorUses.values()).sort(byUses).slice(0, 40),
    fonts: {
      families: Array.from(families.values()).sort(byUses).slice(0, 10).map((f) => ({
        family: f.family,
        stack: f.stack,
        uses: f.uses,
        weights: Array.from(f.weights, ([weight, uses]) => ({ weight: cleanToken(weight, 10), uses })).sort(byUses),
      })),
      sizes: aggregate(sizes, 20).map((s) => ({ px: s.value, uses: s.uses })),
      lineHeights: Array.from(lineHeights.values()).sort(byUses).slice(0, 20),
    },
    spacing: { paddings: aggregate(paddings), margins: aggregate(margins), gaps: aggregate(gaps) },
    radii: aggregate(radii),
    shadows: aggregate(shadows),
    graphics,
    keyframes: keyframeRules.keyframes,
    crossOriginSheetsSkipped: keyframeRules.crossOriginSheetsSkipped,
    transitions: Array.from(transitionKeys.values()).sort(byUses).slice(0, 40),
    animationTimelines,
    gsap: phxGsap(),
    smoothScroll: phxSmoothScroll(),
  }
}

const PAGE_FUNCTIONS = [
  phxSkipTag, phxTextTag, phxVisible, phxCssText, phxRound, phxPageHeight, phxElements, phxRole, phxInteresting,
  phxKeyframesOf, phxAnimationRecord, phxAnimations, snapshotStep, listInteractive, phxStructure, phxKeyframeRules,
  phxGraphics, phxValueOrNull, phxScrollTrigger, phxTween, phxGsap, phxLibraryOptions, phxSmoothScroll, extractStatic,
]
const pageFunctionsSource = PAGE_FUNCTIONS.map((fn) => fn.toString()).join('\n')

// Source of one expression that runs `name(arg)` in the page with every helper in scope.
export function pageScript(name, arg) {
  if (!PAGE_FUNCTIONS.some((fn) => fn.name === name)) throw new Error(`unknown page function ${name}`)
  return `(() => {\n${helpersSource}\n${pageFunctionsSource}\nreturn ${name}(${JSON.stringify(arg === undefined ? null : arg)});\n})()`
}
