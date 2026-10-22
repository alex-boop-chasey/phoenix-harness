// node --test (tools/js-test.sh). logpanel.js: the incremental runner-log panel's pure rules.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { trimLines, shouldFollow, MAX_LINES } = createRequire(import.meta.url)("./logpanel.js");

test("trimLines keeps only the newest lines past the cap", () => {
  const lines = Array.from({ length: 2500 }, (_, i) => `l${i}`);
  const kept = trimLines(lines, 2000);
  assert.equal(kept.length, 2000);
  assert.equal(kept[0], "l500");
  assert.equal(kept[1999], "l2499");
  assert.equal(trimLines(["a", "b"], 2000).length, 2);
  assert.equal(MAX_LINES, 2000);
});

test("shouldFollow: stick to the bottom only when already there (within 20 px) or forced", () => {
  assert.equal(shouldFollow(0, 100, 1000, false), false);
  assert.equal(shouldFollow(890, 100, 1000, false), true);
  assert.equal(shouldFollow(870, 100, 1000, false), false);
  assert.equal(shouldFollow(0, 100, 1000, true), true);
  assert.equal(shouldFollow(0, 100, 100, false), true);      // nothing to scroll: at the bottom by definition
});
