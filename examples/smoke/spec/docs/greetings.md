# Greetings spec

## greet

`greet(name)` returns `"hello " + name` with `name` trimmed of surrounding whitespace.
It throws `TypeError` when `name` is not a string, or is empty after trimming.

## shout

`shout(name)` returns `greet(name)` upper-cased with a trailing `!`.
Same validation as `greet` (it must reuse it, not duplicate it).

## farewell

Not part of this build.
