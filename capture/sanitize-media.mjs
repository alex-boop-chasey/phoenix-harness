// Decode every PNG/JPEG/WebP under --in-dir in Chromium and re-encode it (drops EXIF/XMP/text chunks),
// at most --max px on the longest side; write 320 px PNG thumbnails of rasters and SVGs to OUT/thumbs/<rel>.png.
// Undecodable files are listed in OUT/errors.json and the exit code is 3; 2 for bad arguments.
import fs from 'node:fs'
import path from 'node:path'
import { chromium } from 'playwright'

const USAGE = 'usage: node capture/sanitize-media.mjs --in-dir <dir> --out-dir <dir> [--max 2560] [--thumb 320]'
const MIME = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' }

function parseArgs(argv) {
  const o = { inDir: null, outDir: null, max: 2560, thumb: 320 }
  for (let i = 0; i < argv.length; i += 2) {
    const v = argv[i + 1]
    if (v === undefined) throw new Error(`${argv[i]} needs a value`)
    if (argv[i] === '--in-dir') o.inDir = v
    else if (argv[i] === '--out-dir') o.outDir = v
    else if (argv[i] === '--max' || argv[i] === '--thumb') {
      const n = Number(v)
      if (!Number.isInteger(n) || n < 1 || n > 16384) throw new Error(`${argv[i]} needs an integer from 1 to 16384`)
      o[argv[i].slice(2)] = n
    } else throw new Error(`unknown argument ${argv[i]}`)
  }
  if (!o.inDir || !o.outDir) throw new Error('--in-dir and --out-dir are required')
  return o
}

function* walk(dir, rel = '') {
  for (const e of fs.readdirSync(path.join(dir, rel), { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const r = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) yield* walk(dir, r)
    else if (e.isFile()) yield r
  }
}

// Runs inside the page: bytes in as base64, PNG/JPEG/WebP bytes out as base64.
async function reencode({ b64, mime, max, thumb }) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const bmp = await createImageBitmap(new Blob([bytes], { type: mime }))
  const draw = async (limit, byWidth, type, quality) => {
    const s = Math.min(1, limit / (byWidth ? bmp.width : Math.max(bmp.width, bmp.height)))
    const w = Math.max(1, Math.round(bmp.width * s))
    const h = Math.max(1, Math.round(bmp.height * s))
    const canvas = new OffscreenCanvas(w, h)
    canvas.getContext('2d').drawImage(bmp, 0, 0, w, h)
    const blob = await canvas.convertToBlob({ type, quality })
    if (blob.type !== type) throw new Error(`cannot encode ${type}`)
    const buf = new Uint8Array(await blob.arrayBuffer())
    let bin = ''
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
    return btoa(bin)
  }
  return { main: await draw(max, false, mime, 0.9), thumb: await draw(thumb, true, 'image/png') }
}

// Runs inside the page: an SVG loaded as an <img> never runs scripts or fetches.
async function svgThumb({ b64, thumb }) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
  const url = URL.createObjectURL(new Blob([bytes], { type: 'image/svg+xml' }))
  try {
    const img = new Image()
    img.src = url
    await img.decode()
    const nw = img.naturalWidth || thumb
    const nh = img.naturalHeight || thumb
    const s = Math.min(1, thumb / nw)
    const w = Math.max(1, Math.round(nw * s))
    const h = Math.max(1, Math.round(nh * s))
    const canvas = new OffscreenCanvas(w, h)
    canvas.getContext('2d').drawImage(img, 0, 0, w, h)
    const buf = new Uint8Array(await (await canvas.convertToBlob({ type: 'image/png' })).arrayBuffer())
    let bin = ''
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
    return btoa(bin)
  } finally {
    URL.revokeObjectURL(url)
  }
}

function put(out, rel, b64) {
  const target = path.join(out, rel)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.writeFileSync(target, Buffer.from(b64, 'base64'))
}

async function main() {
  let opts
  try {
    opts = parseArgs(process.argv.slice(2))
  } catch (err) {
    console.error(`${err.message}\n${USAGE}`)
    return 2
  }
  fs.mkdirSync(opts.outDir, { recursive: true })
  const errors = []
  const browser = await chromium.launch()
  try {
    const context = await browser.newContext({ offline: true, javaScriptEnabled: true })
    const page = await context.newPage()
    for (const rel of walk(opts.inDir)) {
      const ext = path.extname(rel).toLowerCase()
      const mime = MIME[ext]
      if (!mime && ext !== '.svg') continue
      try {
        const b64 = fs.readFileSync(path.join(opts.inDir, rel)).toString('base64')
        if (mime) {
          const r = await page.evaluate(reencode, { b64, mime, max: opts.max, thumb: opts.thumb })
          put(opts.outDir, rel, r.main)
          put(opts.outDir, `thumbs/${rel}.png`, r.thumb)
        } else {
          put(opts.outDir, `thumbs/${rel}.png`, await page.evaluate(svgThumb, { b64, thumb: opts.thumb }))
        }
      } catch (err) {
        errors.push({ file: rel, error: String(err.message || err).split('\n')[0].slice(0, 200) })
      }
    }
  } finally {
    await browser.close()
  }
  if (errors.length) {
    fs.writeFileSync(path.join(opts.outDir, 'errors.json'), JSON.stringify(errors, null, 1) + '\n')
    return 3
  }
  return 0
}

process.exitCode = await main()
