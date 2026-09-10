# Contributing

This is a one-person research preview. There is no CI pipeline configured (no workflow files exist in this repository) and no external contribution process is being actively solicited yet. This document exists so the bar for a change is written down instead of implied, including for future work on this same codebase.

## Running the tests

From the repository root:

```
python3 -m pytest -q
```

All tests must pass before a change is considered complete. There is no separate integration or slow-test suite; `pytest -q` is the whole thing, and the count it collects changes as the codebase grows -- run it yourself rather than trusting a number written down here.

## Linting

```
ruff check --select F,E9 src tests
```

This must exit 0. The selected rule set is deliberately narrow -- undefined names and syntax errors, not a full style pass -- so do not widen it as a side effect of an unrelated change without saying so explicitly.

## What a change has to satisfy

- The two commands above stay green. A change that turns either red is not done, not "done except for CI" -- there is no CI here to catch it later.
- No claim in a docstring, comment, commit message, or one of the governance files at the repository root should describe something as verified, tested, or sourced unless it actually is; mark what is not with the literal word `UNVERIFIED` rather than dropping the caveat silently.
- New behavior gets a new test under `tests/`; a bug fix gets a regression test that fails without the fix and passes with it.
- A user-visible or interface change (CLI subcommands, flags, JSON output keys, persisted schema identifiers) gets a `CHANGELOG.md` entry describing it.
- Nothing is deleted or silently overwritten to make a change look cleaner than it is; a file that has to move aside is renamed with a version stamp, not removed.

## What this project does not have

No CI pipeline, no mandatory review process, no supported-versions table, and no response-time commitment on issues -- see `SECURITY.md`. If any of those get set up later, this file should be updated to describe them, not assumed to already cover them.
