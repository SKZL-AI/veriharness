# Release notes -- v0.1.0-rc3

`v0.1.0-rc3` supersedes `v0.1.0-rc2`, published the same day. Both earlier tags
stay published and unmoved. No behaviour of `hoh` changed in either step.

rc3 exists because verifying rc2 *after* publishing it found a defect that
every gate before publication had missed, and the way it was missed is the
interesting part.

## What rc3 corrects

The attribution table in `README.md` counts commits to `src/hoh/`, `tests/`,
`docs/`, `paper/` and `tools/`. That table lives in `README.md`, and it is
written in the same commit that touches those very paths -- so **the commit
that writes the numbers changes the numbers.** Measured at `HEAD`, three of the
table's five rows were off by one the moment they were committed.

A second, independent defect sat next to it: the table said "measured over the
non-merge commits reachable from the mainline" without saying *which* mainline.
Read against this published repository, whose mainline is two commits long,
every one of those figures is wrong. They only ever described the development
repository.

rc3 therefore:

- names the repository and the exact commit the figures are measured at, and
  states plainly that they are not reproducible in this export;
- corrects the three rows: `tests/` 70/19/51, `docs/` 15/9/6, `paper/` 10/9/1;
- measures the attribution at that named commit rather than at `HEAD`, so the
  figure stops moving.

**One claim flipped, and it is worth naming rather than quietly restating.**
rc2 reported `paper/` as 9/9/**0** -- nothing written to the position paper
outside a run. That was true until sections 1.1 and 1.2 were written directly
by the orchestrating session to produce rc2 itself. The column now reads 1, and
the README names which commit it is.

## How it was found, and why that matters more than the fix

Every gate was green. The ten-step closure chain: green. The export
verification: green. The post-publish audit: green. The tag was pushed and the
release published.

The defect surfaced only when the number checker was pointed at a **fresh clone
of the published repository** instead of the tree it was written in. Every gate
ran inside the same tree the figure was derived from, so every gate agreed with
it. That is not a gap in any individual check -- it is a property of checking a
measurement against the thing it measured.

This is the same shape as two limits this project already documents:
`subject_head` is not `attestation_commit`, and `RC_CLOSED` is not
`POST_PUBLISH_CONSISTENT`. The new instance adds self-reference: here, writing
the measurement down changes it. A measurement of a tree has to name the tree.

---

# Release notes -- v0.1.0-rc2

`v0.1.0-rc2` supersedes `v0.1.0-rc1`. It is a **documentation and positioning
correction**, not new functionality: no behaviour of `hoh` changed between the
two tags. `v0.1.0-rc1` stays published, tagged and unmoved -- it is the
historical artifact of what this project said about itself on 2026-09-10
before the corrections below, and deleting or retagging it would destroy
exactly the kind of record this project claims to keep.

## What rc2 corrects, and why there was anything to correct

Four claims in the rc1 documents were wrong or misleading. All four were
found after publication, three of them by a reader's objection rather than by
a gate, which is itself worth stating plainly.

| Where | What rc1 said | Why it was wrong |
|---|---|---|
| `README.md` | "There is no automatism, and there should not be" | True of the kernel, false as a description of the product. HoH does not self-start, but this project's own campaign drove it automatically from an orchestrating agent session across five days. The heading read as "this thing cannot be automated". |
| `README.md`, `RELEASE_NOTES.md` | The dogfood evidence "covers documentation and governance work, never a change to `src/` or `tests/`" | False on both halves. 19 of 70 commits under `tests/` arrived via run branches, and one run added two files under `src/hoh/policy/`. |
| `docs/LIMITATIONS.md` limit 1 | Described the campaign as *attended*, with "an operator" writing each specification | Misattributed the system's own top layer as external supervision. The specifications, merges and repairs came from an agent session, not a person typing. |
| `docs/LIMITATIONS.md` limit 8 | "No run has ever added or edited a line inside `src/`" | Overstated. Run `d1` added `src/hoh/policy/dangerous-patterns.txt` and `src/hoh/policy/house-rules-patterns.txt`, merged as accepted candidate `d1-i2`. Recorded as finding `O94`. |

None of these were code defects, and none of them changed what the software
does. They were claims about the project's own evidence -- which is the class
of statement this project holds itself to most strictly, so getting them
wrong in the first public release is worth naming rather than quietly fixing.

rc2 additionally adds what rc1 omitted entirely: a `What has actually been
exercised` section in `README.md` reporting the campaign in measured numbers,
an attribution table naming which authority wrote which paths, the four-layer
architecture in `docs/ARCHITECTURE.md`, and §1.1/§1.2 of the position paper on
global composition closure and the operating model the evidence came from.

Every number added is bound: `kampagne-zahlen.py` re-derives all of them from
the run records and the commit history and fails the release gate on any
disagreement, with a negative control that falsifies each number in turn and
requires the checker to catch it.

## The merge problem, restated

rc1 stated the composition limit in a form that had already stopped being
complete: *"nothing in the loop checks the union of two runs' changes"*. The
corrected two-level statement, now in `docs/LIMITATIONS.md` §9 and the paper's
§1.1:

- **Per run:** `Accept(A)` and `Accept(B)` still do not imply `Accept(A ∪ B)`.
  The acceptance loop is not closed under composition, and no amount of global
  gating closes it.
- **System level:** above that loop sit the union invariants `U1`-`U5`,
  semantic dependency measurement between parallel candidates, and a post-DAG
  closure pass that turns any global failure into a new repair run and re-runs
  closure until it reaches a fixpoint -- `DAG_TERMINAL != RC_CLOSED`.

This is not a claim that composition is solved. `U1`-`U5` are a named,
extensible list of failure shapes that have actually occurred; an unknown
cross-run interaction would pass all of them. What the global layer changes is
whether such an inconsistency reaches a release -- during this very release it
caught two, after the run graph was already terminal, which became repair runs
`d8b` and `d8c`.

---

# Release notes -- v0.1.0-rc1

*Preserved below exactly as published, apart from the four corrections marked
`[corrected in rc2]`. This is the record of what was claimed at rc1.*

## What this is, plainly, before anything else

This is a research preview, and it is a tool for someone who already wants
this discipline -- a written specification, evidence-bound acceptance,
independent verification before anything is trusted -- and is willing to pay
its running cost. It is not a tool that makes that discipline easy or cheap
to adopt, and this release does not claim otherwise. `hoh` is the
command-line package, module, and CLI that implement the Harness-of-Harness
pattern; VeriHarness is the project and repository built on that pattern.

## The four real hurdles

Adopting HoH costs something before it produces anything, and pretending
otherwise would undercut an evidence-first project's own point:

| Hurdle | Why it counts |
|---|---|
| A written specification | The planner role has nothing to bind acceptance criteria to without one, and few projects walk in with one already written. |
| Herdr running, with `HERDR_ENV=1` | HoH refuses to start otherwise, on purpose -- a real prerequisite, not a formality. |
| Three agent sessions of quota per iteration | Planner, developer, and QA each run as their own dispatch, before counting the per-criterion control runs this loop also spends. |
| JSON output | `hoh` commands print structured JSON, operable by scripts and other tools -- not designed to be inviting to read on its own. |

## What changed on the way to `0.1.0-rc1`

- This run (`hoh-d8`) added three release-candidate documents -- this file,
  `RC_GATE.md`, and a new section in `CHANGELOG.md` -- and nothing else: no
  file under `src/hoh/`, `tests/`, or `tools/` changed to produce them, and
  no file this release candidate reports on (including all seven files the
  `paper` manifest rule publishes) was edited by this run.
- The position paper (`paper/POSITION_PAPER.md`) and its citation-and-numbers
  audit (`paper/AUDIT.md`) each passed their own QA gate: `D5` accepted the
  paper at its second iteration, `14` of `14` criteria; `D6` accepted the
  audit at its second iteration, `10` of `10` criteria.
- Two independent reviews of the release candidate -- `paper/REVIEW_A.md` and
  `paper/REVIEW_B.md`, run as separate sessions that never saw each other's
  findings before both finished -- found eight HIGH-severity findings between
  them, consolidated without repair in `paper/REVIEW_CONSOLIDATED.md`. The
  four classified BLOCKER are now closed: `A-01` and `A-02` were corrected by
  loop run `d7r`, `B-04` by loop run `d7w`, and `B-01` by loop run `d7x` --
  none of them by the operator. The remaining four are classified
  DOCUMENTED_LIMITATION and were not fixed by either the loop or the
  operator: they are a disclosed, disclaimed property of the check-command
  guard, not a silent gap (see `docs/LIMITATIONS.md`, limit 4).
- Eleven core HoH defects and five guard gaps found during this campaign's
  own dogfooding were fixed **at the orchestrator level** -- by the
  orchestrating agent session working outside a run, not by a person typing
  and not by an HoH developer role -- under a narrow, declared exception to
  the rule that the loop may not touch `src/hoh/` or `tests/`; the full
  accounting, defect by defect, is the internal *Abschlussbericht*
  point D. Outside that declared exception, the loop never touched HoH's own
  production code to produce this project's evidence about itself.
  **[corrected in rc2.** rc1 wrote "by the operator" here, which reads as a
  person typing; the fixes were made by the orchestrating agent session
  working above the run boundary. The sentence about production code is
  corrected separately below.**]**

## The double review, stated precisely

`paper/POSITION_PAPER.md` went through two genuinely independent reviews --
`paper/REVIEW_A.md` (empirical honesty against `CLAIMS.json`) and
`paper/REVIEW_B.md` (release, security, export, and operational safety) --
each a separate `hoh run`, staggered, with no shared output directory and no
access to the other's context or findings before both finished.
`paper/REVIEW_CONSOLIDATED.md` then merged both, repairing nothing itself.
This release candidate does not claim there is no open HIGH finding: it
claims that the reproducible, blocking findings are closed, names which loop
run closed each one, and reports that the rest are documented rather than
silent. `RC_GATE.md` carries the full severity-and-disposition breakdown.

**A separate, smaller reduction, named here because it is about this very
document.** The house rules on this machine normally call for two
independent adversarial subagents to review any verdict before it is
committed. For the run that produced these three files, that gate was
replaced by a single external QA pass instead: the role structure for this
run names one independent QA reviewer, not two, and the developer role that
wrote `RELEASE_NOTES.md`, `RC_GATE.md`, and the `CHANGELOG.md` addition was
explicitly told not to spawn review subagents of its own, on the grounds
that the external QA role already sits outside and above it. That is a real
reduction in coverage of unknown size, not an equivalent, and it is named
here rather than left implicit.

## What this dogfood evidence does and does not cover

~~The evidence behind this release candidate covers documentation, packaging,
and governance work -- not a single change to HoH's own production code.~~
**[corrected in rc2]** That claim is too wide on both halves. Measured over
the non-merge commits reachable from the mainline: `tests/` has 70 commits, 19 of them
arriving via run branches across three run-authored files; `src/hoh/` has 49
commits, one of which arrived via a run branch -- run `d1` adding
`src/hoh/policy/dangerous-patterns.txt` and `src/hoh/policy/house-rules-patterns.txt` so an installed wheel
could execute an acceptance check at all. What survives, and is worth saying:
**no run has ever added or edited a line of Python under `src/`.** HoH's own
logic was never written by a run. See `docs/LIMITATIONS.md` limit 8, and
finding `O94` for how the overstatement was found.

## What is not claimed

This document does not restate `docs/LIMITATIONS.md`; read it before
deciding this is production-ready for anything you care about. It names,
among other things, that the check-command guard is a tripwire against
accidents rather than a security boundary, that there is no matched-budget
comparison against a plain agent working the same specification, and that
local run correctness does not imply correctness after a merge of several
runs' work.

No part of this project fixed itself: every repair named in this document
was made either by the operator, under a narrow, declared exception, or by
the loop working an ordinary, scoped `hoh run` iteration against
documentation or release tooling -- never by HoH observing its own failure
and correcting it unsupervised. This release makes no claim of autonomous
self-repair, self-healing, or anything "fixing itself": every fix credited
above names the loop or the operator, by run id or by role, because that is
the distinction this project is built to keep honest.

## What this run did, and what the operator did after it

**Run `D8`, which produced this document, published nothing.** It created no
tag, made no push, added no remote and never called `hoh deliver` -- those
were outside its declared scope, and `RC_GATE.md`'s "What this run did not do"
records that boundary as it stood when the gate was measured.

**Publication happened afterwards**, once the release closure was complete:
`v0.1.0-rc1` was tagged and pushed on a human decision that this run neither
made nor was permitted to make. The orchestrating agent session prepared and
verified the export; the decision to publish it was reserved from delegation
and taken by a person. `v0.1.0-rc2` follows the same split. Those two sentences are deliberately kept apart, because
one is a statement about a run and the other about a person, and collapsing
them is the exact class of claim this project exists to keep separate.
`RC_GATE.md` reports the gate condition by condition, including the rows
only the operator can fill in.
