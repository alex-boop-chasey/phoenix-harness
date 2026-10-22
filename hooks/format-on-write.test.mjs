// hooks/format-on-write.mjs: the PostToolUse hook that formats and lints the file an agent just wrote.
// A throw-away project with fake prettier/eslint scripts records every call in calls.log.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const hook = fileURLToPath(new URL('./format-on-write.mjs', import.meta.url))

const made = []
after(() => {
  for (const d of made) fs.rmSync(d, { recursive: true, force: true })
})

function project({ tools = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hands-hook-'))
  made.push(root)
  const proj = path.join(root, 'proj')
  fs.mkdirSync(path.join(proj, 'src'), { recursive: true })
  fs.writeFileSync(path.join(proj, 'package.json'), '{"name":"proj","private":true}\n')
  const log = path.join(root, 'calls.log')
  if (tools) {
    const bin = path.join(proj, 'node_modules', '.bin')
    fs.mkdirSync(bin, { recursive: true })
    // prettier fails on paths containing "unparseable"; eslint reports an error for paths containing "bad"
    fs.writeFileSync(
      path.join(bin, 'prettier'),
      `#!/bin/bash\nprintf 'prettier %s\\n' "$*" >> "${log}"\n` +
        `for a in "$@"; do case "$a" in *unparseable*) echo "SyntaxError: Unexpected token" >&2; exit 2;; esac; done\nexit 0\n`,
      { mode: 0o755 },
    )
    fs.writeFileSync(
      path.join(bin, 'eslint'),
      `#!/bin/bash\nprintf 'eslint %s\\n' "$*" >> "${log}"\n` +
        `for a in "$@"; do case "$a" in *bad*) echo "  1:1  error  x error  no-undef"; exit 1;; esac; done\nexit 0\n`,
      { mode: 0o755 },
    )
  }
  return { root, proj, log }
}

const payload = (file, cwd, tool = 'Write') =>
  JSON.stringify({ tool_name: tool, tool_input: { file_path: file, content: '' }, cwd })
const run = (input, cwd) => spawnSync(process.execPath, [hook], { input, cwd, encoding: 'utf8', timeout: 60_000 })
const calls = (log) => (fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean) : [])
const file = (proj, rel, body = 'x\n') => {
  const f = path.join(proj, rel)
  fs.writeFileSync(f, body)
  return f
}

test('runs prettier then eslint on a ts file', () => {
  const { proj, log } = project()
  const f = file(proj, 'src/a.ts')
  const r = run(payload(f, proj), proj)
  assert.equal(r.status, 0, r.stderr)
  assert.deepEqual(calls(log), [`prettier --write ${f}`, `eslint --fix ${f}`])
})

test('lintable extensions get both tools, formatter-only extensions get prettier', () => {
  for (const ext of ['js', 'mjs', 'cjs', 'jsx', 'tsx', 'mts', 'cts', 'astro', 'vue', 'svelte']) {
    const { proj, log } = project()
    const f = file(proj, `src/a.${ext}`)
    assert.equal(run(payload(f, proj), proj).status, 0)
    assert.deepEqual(calls(log).map((l) => l.split(' ')[0]), ['prettier', 'eslint'], ext)
  }
  for (const ext of ['scss', 'json', 'html', 'yaml', 'yml', 'md', 'mdx']) {
    const { proj, log } = project()
    const f = file(proj, `src/a.${ext}`)
    assert.equal(run(payload(f, proj), proj).status, 0)
    assert.deepEqual(calls(log).map((l) => l.split(' ')[0]), ['prettier'], ext)
  }
})

test('skips eslint for css', () => {
  const { proj, log } = project()
  const f = file(proj, 'src/styles.css', 'a{}\n')
  assert.equal(run(payload(f, proj, 'Edit'), proj).status, 0)
  assert.deepEqual(calls(log), [`prettier --write ${f}`])
})

test('ignores txt and files without an extension', () => {
  const { proj, log } = project()
  for (const rel of ['notes.txt', 'Makefile', 'src/a.TS.bak']) {
    assert.equal(run(payload(file(proj, rel), proj), proj).status, 0)
  }
  assert.deepEqual(calls(log), [])
})

test('extension match is case-insensitive', () => {
  const { proj, log } = project()
  const f = file(proj, 'src/A.JSON')
  assert.equal(run(payload(f, proj), proj).status, 0)
  assert.deepEqual(calls(log), [`prettier --write ${f}`])
})

test('exit 2 with eslint output', () => {
  const { proj, log } = project()
  const f = file(proj, 'src/bad.ts')
  const r = run(payload(f, proj), proj)
  assert.equal(r.status, 2)
  assert.match(r.stderr, /x error/)
  assert.equal(calls(log).length, 2, 'prettier ran before eslint')
})

test('exit 2 with prettier output, eslint not run', () => {
  const { proj, log } = project()
  const f = file(proj, 'src/unparseable.ts')
  const r = run(payload(f, proj), proj)
  assert.equal(r.status, 2)
  assert.match(r.stderr, /SyntaxError/)
  assert.deepEqual(calls(log), [`prettier --write ${f}`])
})

test('project root is the nearest ancestor with package.json', () => {
  const { proj, log } = project()
  const inner = path.join(proj, 'packages', 'inner')
  fs.mkdirSync(path.join(inner, 'node_modules', '.bin'), { recursive: true })
  fs.writeFileSync(path.join(inner, 'package.json'), '{}\n')
  // the inner package has no tools of its own: nothing runs, the outer project's tools are not used
  const f = file(proj, 'packages/inner/a.ts')
  assert.equal(run(payload(f, proj), proj).status, 0)
  assert.deepEqual(calls(log), [])
})

test('exit 0 outside a project', () => {
  const { root, log } = project()
  const f = file(root, 'loose.ts')
  assert.equal(run(payload(f, root), root).status, 0)
  assert.deepEqual(calls(log), [])
})

test('exit 0 when the tools are missing', () => {
  const { proj, log } = project({ tools: false })
  const f = file(proj, 'src/a.ts')
  const r = run(payload(f, proj), proj)
  assert.equal(r.status, 0)
  assert.equal(r.stderr, '')
  assert.deepEqual(calls(log), [])
})

test('exit 0 on malformed stdin, empty stdin and missing file_path', () => {
  const { proj, log } = project()
  assert.equal(run('this is not json', proj).status, 0)
  assert.equal(run('', proj).status, 0)
  assert.equal(run('[1,2]', proj).status, 0)
  assert.equal(run(JSON.stringify({ tool_name: 'Write', tool_input: {} }), proj).status, 0)
  assert.equal(run(JSON.stringify({ tool_name: 'Write', tool_input: { file_path: 42 } }), proj).status, 0)
  assert.equal(run(JSON.stringify({ tool_name: 'Write' }), proj).status, 0)
  assert.deepEqual(calls(log), [])
})

test('exit 0 when the named file does not exist', () => {
  const { proj, log } = project()
  assert.equal(run(payload(path.join(proj, 'src', 'gone.ts'), proj), proj).status, 0)
  assert.deepEqual(calls(log), [])
})

test('path with spaces passed intact', () => {
  const { proj, log } = project()
  const f = file(proj, 'src/my file.ts')
  assert.equal(run(payload(f, proj), proj).status, 0)
  assert.deepEqual(calls(log), [`prettier --write ${f}`, `eslint --fix ${f}`])
})

test('relative file_path is resolved against the payload cwd', () => {
  const { root, proj, log } = project()
  const f = file(proj, 'src/rel.ts')
  assert.equal(run(payload('src/rel.ts', proj), root).status, 0)
  assert.deepEqual(calls(log), [`prettier --write ${f}`, `eslint --fix ${f}`])
})
