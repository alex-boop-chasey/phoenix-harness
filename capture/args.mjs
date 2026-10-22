// Command-line parsing for capture.mjs. Dependency-free so it is unit-testable without Playwright.

export const VIEWPORTS = {
  desktop: { w: 1440, h: 900 },
  mobile: { w: 390, h: 844 },
}
export const MAX_STEPS = 24
export const DEFAULT_NAV_TIMEOUT_MS = 20000
export const USAGE = `usage: node capture/capture.mjs --url <url> --out <dir> [--max-steps ${MAX_STEPS}] [--viewports desktop,mobile] [--nav-timeout-ms ${DEFAULT_NAV_TIMEOUT_MS}]`

export class UsageError extends Error {
  constructor(message) {
    super(message)
    this.name = 'UsageError'
  }
}

function positiveInt(flag, value, max = Infinity) {
  const n = Number(value)
  if (!Number.isInteger(n) || n <= 0 || n > max) throw new UsageError(`${flag} needs an integer from 1 to ${max}`)
  return n
}

// Throws UsageError (exit 2) on anything unexpected; the url must be http(s).
export function parseArgs(argv) {
  const opts = { url: null, out: null, maxSteps: MAX_STEPS, viewports: ['desktop', 'mobile'], navTimeoutMs: DEFAULT_NAV_TIMEOUT_MS }
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    const next = () => {
      if (i + 1 >= argv.length) throw new UsageError(`${arg} needs a value`)
      return argv[++i]
    }
    if (arg === '--url') opts.url = next()
    else if (arg === '--out') opts.out = next()
    else if (arg === '--max-steps') opts.maxSteps = positiveInt(arg, next(), MAX_STEPS)
    else if (arg === '--nav-timeout-ms') opts.navTimeoutMs = positiveInt(arg, next())
    else if (arg === '--viewports') {
      opts.viewports = next().split(',').map((v) => v.trim()).filter(Boolean)
      if (!opts.viewports.length || opts.viewports.some((v) => !VIEWPORTS[v])) {
        throw new UsageError(`--viewports takes a comma list of ${Object.keys(VIEWPORTS).join(', ')}`)
      }
    } else throw new UsageError(`unknown argument ${arg}`)
  }
  if (!opts.url) throw new UsageError('--url is required')
  if (!opts.out) throw new UsageError('--out is required')
  let parsed
  try { parsed = new URL(opts.url) } catch { throw new UsageError('--url is not a valid URL') }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new UsageError('--url must be http(s)')
  return opts
}
