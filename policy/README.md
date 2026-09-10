# policy/ -- guard patterns as a fallback inside the repository

The runner checks check-commands against the same pattern files the operator's
shell hooks use (`~/.agents/hooks/`). The search runs in **three** stages, in
this order:

1. **Operator** -- `~/.agents/hooks/`. Authoritative: a sharpened pattern
   should take effect everywhere immediately and not be overruled by an older
   shipped copy.
2. **Package** -- `hoh/policy/` inside the installed distribution. This stage
   was missing until 2026-09-07, and that made an installed version unusable:
   `assert_command_allowed` raised `PolicyUnavailable` on **every** check, so a
   pip-installed HoH could not execute a single acceptance criterion. It never
   showed locally, because stage 1 exists on the development machine.
3. **Repository layout** -- this directory, so the tests run in a bare
   checkout.

Shipping the files in stage 2 is part of the packaging step; before that a
built wheel does **not** contain them.

**Why a copy at all:** an adversarial review pointed out that the whole guard
guarantee otherwise hangs on two unversioned files in the home directory. On
every other machine `assert_command_allowed` fails with `PolicyUnavailable`,
and the test suite is green only here. With this fallback it runs everywhere --
and whoever changes the patterns sees the change in the diff.

Keeping them in sync: after every change to `~/.agents/hooks/*.txt`, update
the copy and run `~/.agents/hooks/test-guard.sh` and `test-house-rules.sh`.

## Pattern change, 2026-09-08

The closing anchor of patterns 1 and 3 knew only whitespace, `;` and end of
line. The **bare** command substitution slipped through as a result --
`$(tool)` did not match, `$( tool )` did, even though the comment in the
pattern file names `$( )` as explicitly covered. Found by an adversarial
reviewer at the step-0 gate of the dogfood run.

The anchor now also knows `)`, `&` and `|`. Measured with `grep -E`, that is,
the engine the hooks really use, and with a placeholder instead of the real
tool name: **18 of 18** attack forms blocked, **0** false positives on
harmless commands. The operator's copy has been updated, its predecessor sits
versioned beside it, and `test-guard.sh` and `test-house-rules.sh` both report
`failed: 0` afterwards.

## Translation, 2026-09-08

`house-rules-patterns.txt` was translated into English for the release (E1).
**Not one regular expression changed** -- verified by diffing the non-comment
lines before and after, byte for byte -- and the measurement was repeated on
the translated file: again 18 of 18 blocked, 0 false positives. The German
version is parked under `history/`. The comments carry the findings knowledge
they always carried; only the language changed.
