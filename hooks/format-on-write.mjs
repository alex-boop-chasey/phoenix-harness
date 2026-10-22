#!/usr/bin/env node
// Claude Code PostToolUse hook: format and lint the file an Edit/Write just touched.
//
// stdin: {tool_name, tool_input: {file_path}, cwd}. The project root is the nearest ancestor of the
// file with a package.json; the tools are only ever <root>/node_modules/.bin/{prettier,eslint}.
// prettier --write runs on FORMAT_EXT, then eslint --fix on LINT_EXT, 60 s each, the path as one
// argv element (no shell). Exit 2 with the tool output on stderr when prettier fails or eslint still
// reports errors (Claude Code feeds that back to the agent); exit 0 in every other case, including
// malformed stdin, no file_path, no project root, missing tools and unknown extensions, so the hook
// never blocks the agent. Dependency-free: plain Node.
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const FORMAT_EXT = new Set([
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'mts', 'cts', 'astro',
  'css', 'scss', 'json', 'html', 'vue', 'svelte', 'yaml', 'yml', 'md', 'mdx',
])
export const LINT_EXT = new Set(['js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'mts', 'cts', 'astro', 'vue', 'svelte'])
export const TOOL_TIMEOUT_MS = 60_000

export function extensionOf(file) {
  return path.extname(file).slice(1).toLowerCase()
}

export function projectRoot(file) {
  let dir = path.dirname(file)
  for (;;) {
    if (fs.existsSync(path.join(dir, 'package.json'))) return dir
    const parent = path.dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

function isExecutable(p) {
  try {
    fs.accessSync(p, fs.constants.X_OK)
    return fs.statSync(p).isFile()
  } catch {
    return false
  }
}

// Returns null on success, otherwise the text to report.
function runTool(name, bin, args, cwd) {
  const r = spawnSync(bin, args, { cwd, encoding: 'utf8', timeout: TOOL_TIMEOUT_MS, stdio: ['ignore', 'pipe', 'pipe'] })
  if (r.error) {
    const why = r.error.code === 'ETIMEDOUT' ? `timed out after ${TOOL_TIMEOUT_MS / 1000} s` : String(r.error.message)
    return `${name} ${why}\n${r.stdout || ''}${r.stderr || ''}`
  }
  if (r.status !== 0) {
    return `${name} exited with ${r.status === null ? `signal ${r.signal}` : `status ${r.status}`}\n${r.stdout || ''}${r.stderr || ''}`
  }
  return null
}

export function readPayload(text) {
  try {
    const payload = JSON.parse(text)
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
    const input = payload.tool_input
    if (!input || typeof input !== 'object' || typeof input.file_path !== 'string' || !input.file_path) return null
    const cwd = typeof payload.cwd === 'string' && payload.cwd ? payload.cwd : process.cwd()
    return { file: path.resolve(cwd, input.file_path) }
  } catch {
    return null
  }
}

export function main(stdinText) {
  const payload = readPayload(stdinText)
  if (!payload) return 0
  const { file } = payload
  const ext = extensionOf(file)
  if (!FORMAT_EXT.has(ext)) return 0
  let stat
  try {
    stat = fs.statSync(file)
  } catch {
    return 0
  }
  if (!stat.isFile()) return 0
  const root = projectRoot(file)
  if (!root) return 0
  const bin = path.join(root, 'node_modules', '.bin')

  const prettier = path.join(bin, 'prettier')
  if (isExecutable(prettier)) {
    const failure = runTool('prettier', prettier, ['--write', file], root)
    if (failure) {
      process.stderr.write(failure.endsWith('\n') ? failure : failure + '\n')
      return 2
    }
  }
  const eslint = path.join(bin, 'eslint')
  if (LINT_EXT.has(ext) && isExecutable(eslint)) {
    const failure = runTool('eslint', eslint, ['--fix', file], root)
    if (failure) {
      process.stderr.write(failure.endsWith('\n') ? failure : failure + '\n')
      return 2
    }
  }
  return 0
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let text = ''
  try {
    text = fs.readFileSync(0, 'utf8')
  } catch {
    text = ''
  }
  process.exitCode = main(text)
}
