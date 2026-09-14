# Changelog

All notable changes to this project are documented here. Formatting loosely follows Keep a Changelog conventions; versioning follows SemVer once a public release process exists.

## [0.1.0] - 2026-09-14

First stable tag. `RELEASE_NOTES.md` carries the full account, including the
measurements that do not flatter the harness; this section lists what changed.

### Added -- a baseline

- `docs/BENCHMARK_PROTOCOL.md`, frozen before the first cell of the first
  campaign ran: five tasks, three arms (plain agent / one `hoh run` / full
  control plane), a hidden test suite per task that decides the verdict, and a
  budget matched on role dispatches.
- `docs/BENCHMARK_RESULTS.md`, `docs/BENCHMARK_RESULTS_v2.md` and
  `docs/BENCHMARK_RESULTS_v3.md` -- three campaigns, none edited in the light
  of another. v2 is kept with `matched_budget_valid = NO`, which cannot be
  repaired after the fact and is not.
- `docs/benchmarks/v3/`: the pre-registration, its parked predecessor, the
  provenance record establishing from git objects that the exact registration
  bytes were committed before the first dispatch, the raw-result digests, and
  the post-campaign drift accounting.
- `tools/benchmark.py`, `tools/repetition_plan.py`, `tools/prereg.py`, and a
  second, independently written aggregation in
  `tests/test_v3_aggregation_agreement.py` that never imports the reporter and
  must agree with it on every figure.

### Added -- enforcement, where there had been description

- `Budgets.max_dispatches`, checked before the counter is charged and persisted
  before the provider call; shared by a node and its repair nodes; survives a
  restart. `RunVerdict.BUDGET_EXHAUSTED`, `HaltClass.BUDGET_EXHAUSTED` and a
  `BudgetExhausted` exception type, so exhaustion is never a generic failure.
- `tools/budget_evidence.py`: eleven controls at three ceilings, at least one of
  which must force the refusal inside an iteration, plus two falsifiers that
  delete the enforcement and must be detected.
- `tools/closure_e2e.py`: the positive path, at a budget measured from campaign
  v2 rather than chosen after a failure.
- `tools/meta_evidence.py --falsify`, `tools/confinement_evidence.py`,
  `tools/telemetry_audit.py`, `tools/exact_head_ci.py` and
  `tools/readiness.py`, which assembles the board.
- `CapabilityWitness.neu_bezeugen`, re-taking the baseline for named paths only,
  so a retry cannot launder a hostile write into it.

### Changed -- what a skip is allowed to mean

- `tests/conftest.py` now authorises an environment-gap skip only when
  `EXPORT_MANIFEST.json` declares that path excluded for the reason the test
  expects. Missing-and-included, missing-and-unclassified, missing-manifest and
  wrong-reason all fail instead of skipping.
  `tests/test_export_gap_guard.py` holds that with five negative controls.
- Dispatch counting reads a `provider_calls` field instead of counting log
  lines, which was wrong in both directions.

### Changed -- documentation

- `README.md` and `paper/POSITION_PAPER.md` gained the three campaigns in full
  and a section on what the harness demonstrably does, each item bound to
  evidence; `paper/NUMBERS.md` and `paper/AUDIT.md` gained the rows those
  numbers require.
- `docs/LIMITATIONS.md` grew rather than shrank.

## [0.1.0] - 2026-09-08

Interface changes made during this run, each replacing an earlier German-named equivalent that existed only inside this repository (never a public release):

### Changed -- CLI subcommands

- The goalbook subcommand family is now `hoh goal ...` (`list`, `propose`, `approve`, and related decisions), over a module renamed from `zielbuch.py` to `goalbook.py`.
- The quota-resume subcommand is now `resume-quota`, over a module renamed from `kontingent.py` to `quota.py`.

### Changed -- CLI flags

- Flags on the affected subcommands are now `--title`, `--description`, `--by`, and `--reason`, replacing their earlier German-named equivalents.

### Changed -- JSON output

- JSON output keys emitted by the goal and quota-resume commands were renamed to English -- for example `id`, `status`, `title`, `decided_by`, `by`, and `reason` -- so no command emits a German JSON key any more.

### Changed -- persisted schema

- The goalbook's persisted schema identifier is now `hoh-goalbook.v2`. The identifier is actually read and enforced on load: an unrecognized identifier is rejected by name instead of being guessed at, and a pre-existing `hoh-goalbook.v1` file is migrated rather than silently reinterpreted under the new field names.

### Added -- governance

- Added the seven public-repository governance files: `LICENSE`, `PROVENANCE.md`, `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md` (this file), and `CITATION.cff`. `LICENSE` documents that no license has been chosen yet rather than asserting one.
## [0.1.0] - 2026-09-10 (release candidate)

### Added -- release-candidate documentation

- `RELEASE_NOTES.md`, stating the research-preview positioning, the real hurdles to using HoH, what changed on the way to this release candidate, and what is not yet claimed.
- `RC_GATE.md`, reporting the release-candidate gate condition by condition, each with a status and either structured evidence or the explicit `NOT_RUN` marker -- including the rows only an operator can fill in, such as git status and the still-undecided repository host.
- This section of `CHANGELOG.md`, naming the release candidate without disturbing the section above it.

### Not changed

No file outside these three was created, edited, deleted, or renamed to produce this release candidate. Nothing under `src/hoh/`, `tests/`, or `tools/` changed; `pyproject.toml` and `CITATION.cff` are untouched; the section above stands exactly as it was written.
