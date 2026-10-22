// node --test capture/. Pure helpers of capture/paths.mjs: element paths, aggregation, timeline kinds.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { elementPath, aggregate, timelineKind, PATH_RE } from './paths.mjs'

function fakeEl(tagName, classes = [], parentElement = null) {
  return { tagName, classList: classes, parentElement, nodeType: 1 }
}

test('elementPath: tag, up to two classes cut to 40 chars, nth-child, matching the path regex', () => {
  const parent = fakeEl('SECTION', ['grid'])
  const sibling = fakeEl('ARTICLE', ['card'], parent)
  const el = fakeEl('DIV', ['card', 'reveal', 'x'.repeat(60)], parent)
  parent.children = [sibling, el]
  const p = elementPath(el)
  assert.equal(p, 'section.grid > div.card.reveal:nth-child(2)')
  assert.ok(p.length <= 200)
  assert.match(p, PATH_RE)
})

test('elementPath: the third class is dropped, long classes are cut to 40 chars', () => {
  const parent = fakeEl('BODY')
  const el = fakeEl('DIV', ['y'.repeat(60), 'z', 'third'], parent)
  parent.children = [el]
  assert.equal(elementPath(el), `body > div.${'y'.repeat(40)}.z:nth-child(1)`)
})

test('elementPath: class names keep only path-safe characters, never longer than 200 chars', () => {
  const parent = fakeEl('HTML')
  const el = fakeEl('DIV', ['w-1/2', 'md:flex', '[data-x]', 'a b'], parent)
  parent.children = [el]
  const p = elementPath(el)
  assert.equal(p, 'html > div.w-12.mdflex:nth-child(1)')
  let deep = fakeEl('HTML')
  for (let i = 0; i < 60; i++) {
    const child = fakeEl('DIV', ['level' + i], deep)
    deep.children = [child]
    deep = child
  }
  const long = elementPath(deep)
  assert.ok(long.length <= 200, `length ${long.length}`)
  assert.match(long, PATH_RE)
  assert.ok(long.endsWith('div.level59:nth-child(1)'), 'the innermost element is kept')
})

test('elementPath: nothing, a non-element and an element without a parent', () => {
  assert.equal(elementPath(null), '')
  assert.equal(elementPath({ nodeType: 3 }), '')
  assert.equal(elementPath(fakeEl('HTML')), 'html')
})

test('aggregate counts uses, most used first, insertion order on ties, top n', () => {
  assert.deepEqual(aggregate(['a', 'b', 'a']), [{ value: 'a', uses: 2 }, { value: 'b', uses: 1 }])
  assert.deepEqual(aggregate(['b', 'a', 'a', 'c', 'c']), [{ value: 'a', uses: 2 }, { value: 'c', uses: 2 }, { value: 'b', uses: 1 }])
  assert.deepEqual(aggregate(['a', 'b', 'c', 'c'], 2), [{ value: 'c', uses: 2 }, { value: 'a', uses: 1 }])
  assert.deepEqual(aggregate([]), [])
  assert.deepEqual(aggregate(['', null, undefined, 'x']), [{ value: 'x', uses: 1 }])
})

test('timelineKind: document, scroll and view timelines', () => {
  assert.equal(timelineKind({ timeline: null }), 'document')
  assert.equal(timelineKind({ timeline: { constructor: { name: 'DocumentTimeline' } } }), 'document')
  assert.equal(timelineKind({ timeline: { constructor: { name: 'ScrollTimeline' }, source: {} } }), 'scroll')
  assert.equal(timelineKind({ timeline: { constructor: { name: 'ViewTimeline' }, source: {}, subject: {} } }), 'view')
  assert.equal(timelineKind({ timeline: { source: {}, subject: {} } }), 'view')
  assert.equal(timelineKind({}), 'document')
  assert.equal(timelineKind(null), 'document')
})
