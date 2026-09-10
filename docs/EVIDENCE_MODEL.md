# The evidence model

This is the core claim of the project, so it earns the longest document.
If you read only one thing before deciding whether HoH's acceptance means
anything, read this one.

## The claim, stated plainly

A model saying "all tests pass" is not evidence that they do. HoH's entire
design is a response to that one sentence: every acceptance decision has to
trace back to a process that a deterministic runner actually started,
actually waited on, and actually recorded the exit code of -- never to a
role's self-report. This document explains the four pieces that make that
traceback possible: the candidate binding, the receipt, the four evidence
categories, and why a `PASS` receipt is still not, by itself, enough to
accept a run.

## Candidate binding: "which state, exactly, was checked?"

`src/hoh/contracts.py`'s `Candidate` binds a verification to exactly the
content that was tested -- not "commit `abc123`" but
`commit:tree_digest:clean-or-dirty`, produced by `Candidate.binding()`.
The reason a commit alone is insufficient: a dirty working tree, a changed
lock file, or edited test definitions can all leave the commit hash
unchanged while changing what actually gets executed. `tree_digest` comes
from `workspace.working_tree()`, which uses `git write-tree` over a
temporary index (so `git add -A` semantics -- respecting `.gitignore`,
picking up untracked files -- apply without touching the project's real
index). This is checked, not assumed: `stages.assert_binding_intact` is
called both before and after QA runs, and if the sources changed in between,
the verdict is discarded as invalid (`stages.FreezeError`) rather than
trusted.

## Receipts: evidence the runner produces, never the model

A `Receipt` (`src/hoh/contracts.py`) is deliberately **not fillable by the
model**: `exit_code`, `started_at`, `ended_at`, and `stdout_digest` come into
existence inside `runner.run_check` and nowhere else. The runner's own
module docstring states the design principle directly: *"the runner, not the
model, records the process exit code, the run identity and the artifact
hashes. A self-report such as 'all tests green' is not a receipt."*

Every receipt also carries `runner_identity` (host/PID/Python version) and a
digest of the full transcript, so `verify_log` can catch a manipulated log
file, and `verify_receipt` can catch a receipt that does not belong to this
run, this iteration, this attempt, or this exact candidate binding. A
receipt whose provenance does not check out is rejected regardless of what
exit code it claims.

## The four evidence categories

`src/hoh/evidence.py`'s `EvidenceStatus` sorts every claim into exactly one
of four buckets, and the mapping from a QA verdict to a bucket is
deterministic code (`evidence.normalize`), not something a model declares
about itself:

| Status | Meaning | Where it comes from |
|---|---|---|
| `VERIFIED` | Evidenced by candidate-bound records; becomes a preservation requirement for every later candidate | `Outcome.PASS` |
| `GAP` | A visible defect or an unmet requirement | `Outcome.FAIL` |
| `REGRESSION` | Was `VERIFIED` in an earlier loop and is not any more | A later `GAP`/`INSUFFICIENT` on an item whose `was_verified` flag is set |
| `INSUFFICIENT` | Not refuted, but not evidenced either -- explicitly not a success | `Outcome.INCONCLUSIVE`, or an open gap QA reported outside the checked criteria |

Two design choices here matter more than the table shows. First,
`REGRESSION` is computed from "was this item **ever** verified," not from
comparing against only the immediately previous status --
`EvidenceBundle.carry_forward`'s docstring explains why: comparing only to
the previous round let a defect quietly downgrade its own priority
(`VERIFIED -> INSUFFICIENT -> GAP` no longer registers as a regression on
the second failing round if you only look one step back). Second,
`INSUFFICIENT` is a real, named category rather than being folded into
`GAP` or silently dropped -- "not refuted" and "verified" are different
claims, and the evidence model keeps them different on purpose so a planner
cannot win by producing output that is merely unfalsified rather than
demonstrated.

## Why `PASS` alone is not enough

This is the part that is easy to state and easy to get wrong in
implementation, so HoH enforces it in three separate places rather than
once:

**A `PASS` verdict without a receipt is a contract violation, not a
possibility.** `CheckVerdict`'s model validator (`_pass_needs_receipt` in
`src/hoh/contracts.py`) raises before such an object can even be
constructed.

**A claimed `PASS` is re-checked against the actual receipt before it
counts.** `Controller._pass_supported` answers two separate questions that
an earlier version of this project conflated into one: is the evidence
*genuine* (this runner, this candidate, this iteration, this attempt), and
does it *support* the claim (does the recorded exit code match what the
check expected)? A `PASS` that fails either check is downgraded to
`INCONCLUSIVE`, not silently accepted -- as the handoff note behind this
code puts it, *"a valid schema or a hash alone proves no functional
correctness."*

**Passing every criterion is still not sufficient for acceptance.**
`RoleResult.accepted()` requires all verdicts to be `PASS` **and** at least
one to `discriminates` -- meaning the check was proven to be genuinely red
on the immediately preceding accepted state (`Controller._discriminates`
runs every new check against that predecessor state before trusting it as
proof of anything). Without this, a plan made entirely of criteria that were
already green before the developer touched anything would sail through as
an "acceptance" that demonstrates nothing changed. The reverse failure mode
was tried and rejected too: downgrading every non-discriminating criterion
punished preservation checks, whose entire job is staying green on both
states. The rule keeps the two questions -- "does what already existed still
hold?" and "was something actually added?" -- separate, and requires a real
answer to both.

## The preservation suite: evidence that outlives one iteration

A check that passes once does not get to quietly disappear from the next
plan. `Controller._checks_for` merges every previously validated check
(`self.store.read_checks()`) into every subsequent iteration's check list,
and `Controller._record_preserved` writes newly passed checks into that
persisted suite after acceptance. Critically, the **validated version wins**
over whatever the next plan proposes for the same `check_id`: if a later
plan tries to redefine an already-validated `check_id` with a weaker
command, the validated version still runs, and the attempted redefinition is
logged as a disarm attempt (`Controller._disarm_attempts`) rather than
applied. Sharpening a check is still possible -- under a *new* `check_id`,
so the discrimination logic in the previous section applies to it -- but
silently weakening one is not.
