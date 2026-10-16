import { test } from 'node:test';
import assert from 'node:assert/strict';
import { greet } from '../src/greet.js';

test('greet trims and greets', () => {
  assert.equal(greet('Alex'), 'hello Alex');
  assert.equal(greet('  Alex '), 'hello Alex');
});
test('greet rejects bad input', () => {
  for (const bad of ['', '   ', 42, null, undefined]) assert.throws(() => greet(bad), TypeError);
});
