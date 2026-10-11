# The working method behind VeriHarness, as a checklist

This is the method the project itself was built with. It is a method, not a guarantee: each part
says what it protects and what it costs. Fill the placeholders in `templates/`; keep nothing
machine-specific in a shared copy.

## 1. Independent work items as parallel strands

- The unit of parallelism is an independent, ready work item, never the number of agents.
  Start an item only if its inputs exist and it shares no write path with a running one.
- Give each strand its own worktree or copy and its own scratch directory. Two strands that edit the
  same file are not independent: name the overlap in both specifications and merge in a stated order.
- The upper bound is a limit, not a target. Nothing is started to fill slots. Quota and disk are
  capacities too: watch them, and park work instead of starting more when either is under pressure.
- A quota error is never a verdict about the work. Park the item and resume it later.

## 2. Specification drafting

- One drafter per strand writes `spec.md` (goal, exact scope, what to build, acceptance criteria,
  mutants, a note to the roles, overlaps with other strands), plus notes with the evidence and a
  list of open questions, each with a recommendation. See `templates/drafting-brief.template.md`.
- Every criterion is a test id or a plain command with its measured baseline: red on the pin for the
  stated reason, green on a prototype. Measure it the way the product will run it (through
  `runner.run_check`, once with no isolation and once with strict isolation), not only with a bare
  pytest. A criterion that is red for the wrong reason, or green already, proves nothing.
- Name the mutants that a reviewer should run (at most ten): the one-line breakages that each
  criterion must kill. A criterion no mutant fails is decoration.
- The specification never names a scratch path the roles cannot reach. Everything a role needs is in
  the specification, the pinned files or the repository.
- A spec-only pin commit (the specification added to a base commit by plumbing, no working-tree
  change) is what a product run is pinned to. Record the pin, the base and the specification digest.

## 3. Two independent adversarial reviewers per gate

- Each gate (a specification before a run, an accepted candidate before a merge) gets two reviewers
  with different perspectives, each in its own Herdr tab and its own scratch directory, on a copy made
  from the committed bytes (an archive, never a clone, never the live tree).
  Perspective A attacks correctness and safety; perspective B attacks testability and integration.
- Their job is to refute, not to confirm. Every finding carries a location, a harmless repro, a
  label PROVEN or SUSPECTED, and a class BLOCKING or MINOR. BLOCKING means the requirement is not met,
  a rule is unenforced, a trust boundary is weaker, or a status is claimed without evidence.
- Reviewers are read-only on the integration repository, run no destructive commands and write only
  their report. See `templates/reviewer-rules.template.md`.
- A new review round starts only for BLOCKING findings. MINORs are folded into the pinned revision.
- Independent means a different perspective and a fresh context. Two copies of the same reviewer are
  one source of evidence, not two.

## 4. The merge gate, bound to a footer

- Every required report ends with five lines: `VERDICT`, `CANDIDATE`, `BASE`, `SPEC_SHA256`,
  `BLOCKING_OPEN`, and a sixth line, `COMPLETENESS`. A merge is allowed only if every required
  review is present, unique, complete (`COMPLETENESS: COMPLETE`), stable (not modified in the last
  minute), `RELEASE_YES` with zero open BLOCKING, and its candidate, base and specification digest
  equal the values recomputed now. Missing, stale, ambiguous, partial, unstable or colliding means
  not eligible; a partial or unstable report is no verdict, never a release yes or a release no.
- Run the gate as its own step (`hoh merge-gate check`), read its output, write its receipt to a
  fresh name (an existing receipt is never replaced). The receipt digest goes into the merge
  message. Run `hoh merge-gate verify` on the receipt right before the merge, with `--candidate`
  and `--base HEAD` naming the pair about to be merged: it refuses a report that changed, a base
  that moved, or a receipt for another pair.
- Acceptance is not merge: candidate creation, product acceptance, integration acceptance, merge
  authority and phase closure are five different things, held by different parties.

## 5. Lean verification tiers

- Drafting: source analysis, targeted tests, reproduction of known findings. No full mutant tables,
  no full-suite comparison.
- Spec review: one pair, one round, targeted checks of the contract.
- Integration review: the specification's named mutants only, an independent replay of the critical
  criteria, the interaction with the current head. The full-suite comparison against the base is run
  once by the orchestrator, locally, and given to both reviewers.
- Release or phase gate: the authoritative final suites, once.
- Reuse expensive evidence that is bound to content and pin, fresh, and untouched by the change.

## 6. Records by addition

- A record is appended, never rewritten. A corrected claim keeps the old text, with when it was set
  and what replaced it. Nothing is deleted: a superseded file is renamed with a version suffix.
- Every slip (a command that failed unnoticed, a file in the wrong place, a rule nearly broken) is
  recorded with its effect, even when there was none.
- A status is claimed only with evidence: PROVEN is measured or read at a location; NOT_RUN is
  written as NOT_RUN, never as passed.

## 7. Fail-closed safety rules (put them verbatim into every brief)

- No destructive commands: no recursive removal, no `dd`, no `shred`, no hard reset, no `git clean`,
  no stash. Never write such a command as a string into a file either.
- Foreign data is no-touch. Ownership is never inferred from a name or an age. Unknown means no-touch.
- Read-only means read-only: reviewers and drafters work on an archive copy in their own scratch.
- No push, tag, release or publication without the operator's explicit yes, and show the exact
  commit before asking.
- No network in drafting and review.

## 8. Storage and inode hygiene

- One archive copy per strand; mutants are hard-linked copies with only the mutated file replaced.
- Every test run uses a base temporary directory inside a self-removing temporary directory.
- Keep each scratch directory small (a byte budget and an entry budget) and check free space and
  free inodes before heavy steps. Stop and report if either is low.

## 9. Monitors and dashboards

- Anything that waits for a process waits on its process id, not on a name match.
- A monitor dies with the session that started it; a successor re-arms its own.
- Close your own finished tabs through the list of tabs you opened, and never a tab you did not open.
