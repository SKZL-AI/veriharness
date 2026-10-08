# policy/ -- the shipped guard patterns (the product's mandatory minimum)

The runner checks check-commands against the same pattern files the operator's
shell hooks use (`~/.agents/hooks/`), **added to** the policy shipped with the
product. The shipped policy is a mandatory minimum; the operator's copy can
only add restrictions, never remove one.

The shipped copy is found in this order (first that exists):

1. **Package** -- `hoh/policy/` inside the installed distribution. This stage
   was missing until 2026-09-07, and that made an installed version unusable:
   `assert_command_allowed` raised `PolicyUnavailable` on **every** check, so a
   pip-installed HoH could not execute a single acceptance criterion. It never
   showed locally, because stage 1 used to be the operator's copy, present on the development machine.
2. **Repository layout** -- this directory, so the tests run in a bare
   checkout.

Then, if `~/.agents/hooks/` holds a copy that is not the same file, its
patterns are **added** (sources `house rule (operator)`, `denylist (operator)`),
so a pattern the operator sharpened takes effect at once.

**History of the order.** The operator's directory used to be searched first
and was *authoritative* (first found wins). When the operator relaxed a
machine-wide rule for their own sessions, the product's guard silently lost
that pattern, guard tests went red or hung, and real long-running commands
were left orphaned on the machine. A local relaxation must not weaken what the
product promises, so the operator's copy became additive. The incident, its
measurements and the regression tests are in the dogfood ledger (the guard-union entry).

Shipping the files in stage 1 is part of the packaging step; before that a
built wheel does **not** contain them.

**Why a copy at all:** an adversarial review pointed out that the whole guard
guarantee otherwise hangs on two unversioned files in the home directory. On
every other machine `assert_command_allowed` fails with `PolicyUnavailable`,
and the test suite is green only here. With the shipped copy it runs everywhere --
and whoever changes the patterns sees the change in the diff.

Keeping them in step: a pattern the operator **sharpens** in
`~/.agents/hooks/*.txt` should also be added here, so the product ships it.
A pattern the operator **relaxes** there must **not** be removed here -- the
shipped copy is the product's minimum, not a mirror of one machine's shell
hooks. Run `~/.agents/hooks/test-guard.sh` and `test-house-rules.sh` after
any change.

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
