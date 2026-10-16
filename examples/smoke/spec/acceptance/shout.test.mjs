import { test } from 'node:test';
import assert from 'node:assert/strict';
import { shout } from '../src/greet.js';

test('shout', () => {
  assert.equal(shout('Alex'), 'HELLO ALEX!');
  assert.equal(shout('  alex '), 'HELLO ALEX!');
});
test('shout rejects bad input', () => {
  for (const bad of ['', null]) assert.throws(() => shout(bad), TypeError);
});
