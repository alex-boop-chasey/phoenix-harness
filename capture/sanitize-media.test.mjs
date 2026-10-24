import { test as nodeTest } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import zlib from 'node:zlib'

// Chromium needs the installed playwright package; a copy of the tree without node_modules skips these.
const installed = await import('playwright').then(() => true, () => false)
const test = (name, fn) => nodeTest(name, { skip: installed ? false : 'playwright is not installed' }, fn)

const script = new URL('./sanitize-media.mjs', import.meta.url).pathname
const tmp = (p) => fs.mkdtempSync(path.join(os.tmpdir(), p))
const run = (args) => spawnSync(process.execPath, [script, ...args], { encoding: 'utf8', timeout: 180_000 })

function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(zlib.crc32(Buffer.concat([Buffer.from(type), data])) >>> 0)
  return Buffer.concat([len, Buffer.from(type), data, crc])
}
function png(width, height, extra = []) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8
  ihdr[9] = 2
  const row = Buffer.alloc(1 + width * 3, 90)
  row[0] = 0
  const raw = Buffer.concat(Array.from({ length: height }, () => row))
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    ...extra,
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

test('a PNG text chunk does not survive re-encoding', () => {
  const inDir = tmp('san-in-')
  const out = tmp('san-out-')
  fs.writeFileSync(path.join(inDir, 'a.png'), png(40, 30, [chunk('tEXt', Buffer.from('Comment\0SECRET-TEXT'))]))
  const r = run(['--in-dir', inDir, '--out-dir', out])
  assert.equal(r.status, 0, r.stderr)
  assert.ok(!fs.readFileSync(path.join(out, 'a.png')).includes('SECRET-TEXT'))
})

test('a 3000x20 PNG comes out at most 2560 wide', () => {
  const inDir = tmp('san-in-')
  const out = tmp('san-out-')
  fs.writeFileSync(path.join(inDir, 'w.png'), png(3000, 20))
  assert.equal(run(['--in-dir', inDir, '--out-dir', out]).status, 0)
  assert.ok(fs.readFileSync(path.join(out, 'w.png')).readUInt32BE(16) <= 2560)
})

test('an SVG gets only a thumbnail', () => {
  const inDir = tmp('san-in-')
  const out = tmp('san-out-')
  fs.writeFileSync(path.join(inDir, 's.svg'), '<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50"><rect width="50" height="50"/></svg>')
  assert.equal(run(['--in-dir', inDir, '--out-dir', out]).status, 0)
  assert.ok(!fs.existsSync(path.join(out, 's.svg')))
  assert.ok(fs.existsSync(path.join(out, 'thumbs', 's.svg.png')))
})

test('a broken PNG is listed in errors.json and exits 3', () => {
  const inDir = tmp('san-in-')
  const out = tmp('san-out-')
  fs.writeFileSync(path.join(inDir, 'b.png'), Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.alloc(30, 1)]))
  const r = run(['--in-dir', inDir, '--out-dir', out])
  assert.equal(r.status, 3)
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(out, 'errors.json'), 'utf8')).map((e) => e.file), ['b.png'])
})

test('missing arguments exit 2', () => {
  assert.equal(run([]).status, 2)
})
