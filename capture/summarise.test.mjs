// node --test capture/. Summarising helpers of capture/paths.mjs used by the in-page extractor and the CLI.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cleanToken, splitCssList, diffStyles, diffSnapshots, finiteOrNull, summariseTiming, cssValue, pairTransitions,
} from './paths.mjs'

test('cleanToken keeps path-safe characters and cuts to the limit', () => {
  assert.equal(cleanToken('fade-up'), 'fade-up')
  assert.equal(cleanToken('  a<b"c"  ', 10), 'abc')
  assert.equal(cleanToken('x'.repeat(100), 40).length, 40)
  assert.equal(cleanToken(null), '')
  assert.equal(cleanToken(12), '12')
})

test('splitCssList splits on top-level commas only', () => {
  assert.deepEqual(splitCssList('color, transform'), ['color', 'transform'])
  assert.deepEqual(splitCssList('cubic-bezier(0.2, 0.8, 0.2, 1), linear'), ['cubic-bezier(0.2, 0.8, 0.2, 1)', 'linear'])
  assert.deepEqual(splitCssList(''), [])
  assert.deepEqual(splitCssList('ease'), ['ease'])
})

test('pairTransitions cycles the shorter lists like CSS does', () => {
  const t = pairTransitions({
    transitionProperty: 'transform, background-color',
    transitionDuration: '0.15s',
    transitionTimingFunction: 'cubic-bezier(0.2, 0.8, 0.2, 1), linear',
    transitionDelay: '0s',
  })
  assert.deepEqual(t, [
    { property: 'transform', duration: '0.15s', timingFunction: 'cubic-bezier(0.2, 0.8, 0.2, 1)', delay: '0s' },
    { property: 'background-color', duration: '0.15s', timingFunction: 'linear', delay: '0s' },
  ])
  assert.deepEqual(pairTransitions({ transitionProperty: 'all', transitionDuration: '0s', transitionTimingFunction: 'ease', transitionDelay: '0s' }), [])
})

test('diffStyles lists only the properties that changed', () => {
  const before = { color: 'rgb(0, 0, 0)', transform: 'none', opacity: '1' }
  const after = { color: 'rgb(0, 0, 0)', transform: 'matrix(1, 0, 0, 1, 0, -2)', opacity: '1' }
  assert.deepEqual(diffStyles(before, after), { transform: { from: 'none', to: 'matrix(1, 0, 0, 1, 0, -2)' } })
  assert.deepEqual(diffStyles(before, before), {})
})

test('diffSnapshots compares elements by position and reports changed properties, capped', () => {
  const prev = [{ path: 'div:nth-child(1)', opacity: '0', transform: 'none', clipPath: 'none', visibility: 'visible' },
    { path: 'div:nth-child(2)', opacity: '1', transform: 'none', clipPath: 'none', visibility: 'visible' }]
  const next = [{ path: 'div:nth-child(1)', opacity: '1', transform: 'matrix(1, 0, 0, 1, 0, 0)', clipPath: 'none', visibility: 'visible' },
    { path: 'div:nth-child(2)', opacity: '1', transform: 'none', clipPath: 'none', visibility: 'visible' }]
  assert.deepEqual(diffSnapshots(prev, next), [
    { path: 'div:nth-child(1)', changed: { opacity: { from: '0', to: '1' }, transform: { from: 'none', to: 'matrix(1, 0, 0, 1, 0, 0)' } } },
  ])
  assert.deepEqual(diffSnapshots(null, next), [])
  assert.deepEqual(diffSnapshots(prev, next, 0), [])
  const moved = [{ ...next[0], path: 'span:nth-child(1)' }, next[1]]
  assert.deepEqual(diffSnapshots(prev, moved), [], 'a different element at the same position is not a change')
})

test('finiteOrNull and cssValue', () => {
  assert.equal(finiteOrNull(1.5), 1.5)
  assert.equal(finiteOrNull(Infinity), null)
  assert.equal(finiteOrNull(NaN), null)
  assert.equal(finiteOrNull({}), null)
  assert.equal(finiteOrNull('3'), null)
  assert.equal(cssValue(12.345), 12.35)
  assert.equal(cssValue({ value: 42.123, unit: 'percent' }), '42.12%')
  assert.equal(cssValue({ value: 500, unit: 'ms' }), '500ms')
  assert.equal(cssValue(null), null)
  assert.equal(cssValue({}), null)
})

test('summariseTiming keeps the documented fields, nulls non-finite durations, names infinite iterations', () => {
  const t = summariseTiming({
    delay: 200, endDelay: 0, fill: 'both', iterationStart: 0, iterations: Infinity, duration: 2500, direction: 'alternate',
    easing: 'ease-in-out', progress: 0.5, currentIteration: 3, activeDuration: Infinity, endTime: Infinity, localTime: 8000, extra: 1,
  })
  assert.deepEqual(t, {
    delay: 200, endDelay: 0, fill: 'both', iterationStart: 0, iterations: 'infinite', duration: 2500, direction: 'alternate',
    easing: 'ease-in-out', progress: 0.5, currentIteration: 3, activeDuration: null, endTime: null, localTime: 8000,
  })
  assert.equal(summariseTiming({ duration: {} }).duration, null)
  assert.equal(summariseTiming({ duration: 'auto' }).duration, null)
  assert.equal(summariseTiming(null), null)
})
