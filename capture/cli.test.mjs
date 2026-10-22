// node --test capture/. Argument parsing of the capture CLI (capture/args.mjs; exit 2 cases raise UsageError).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseArgs } from './args.mjs'

test('parseArgs: defaults and every flag', () => {
  assert.deepEqual(parseArgs(['--url', 'http://127.0.0.1:1/', '--out', '/tmp/x']), {
    url: 'http://127.0.0.1:1/', out: '/tmp/x', maxSteps: 24, viewports: ['desktop', 'mobile'], navTimeoutMs: 20000,
  })
  assert.deepEqual(parseArgs(['--url', 'https://example.com', '--out', 'o', '--max-steps', '3', '--viewports', 'mobile', '--nav-timeout-ms', '5000']), {
    url: 'https://example.com', out: 'o', maxSteps: 3, viewports: ['mobile'], navTimeoutMs: 5000,
  })
})

test('parseArgs: bad arguments throw a usage error', () => {
  for (const argv of [
    [], ['--out', 'o'], ['--url', 'http://a/'], ['--url'], ['--url', 'ftp://a/', '--out', 'o'], ['--url', 'nota url', '--out', 'o'],
    ['--url', 'http://a/', '--out', 'o', '--max-steps', '0'], ['--url', 'http://a/', '--out', 'o', '--max-steps', 'x'],
    ['--url', 'http://a/', '--out', 'o', '--max-steps', '25'],
    ['--url', 'http://a/', '--out', 'o', '--viewports', 'tablet'], ['--url', 'http://a/', '--out', 'o', '--bogus'],
  ]) {
    assert.throws(() => parseArgs(argv), { name: 'UsageError' }, JSON.stringify(argv))
  }
})
