// Test entry for `node --test capture/`: Node's test runner takes that positional as one test file
// and loads the directory through package.json "main", so this file registers every unit test.
import { readdirSync } from 'node:fs'

const dir = new URL('./', import.meta.url)
for (const name of readdirSync(dir).filter((f) => f.endsWith('.test.mjs')).sort()) {
  await import(new URL(name, dir))
}
