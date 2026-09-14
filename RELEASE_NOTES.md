# Release notes -- v0.1.0

`v0.1.0` is the first stable tag. It supersedes `v0.1.0-rc3`; all three release
candidates stay published, tagged and unmoved, because they are the record of
what this project said about itself on the way here.

**Read this first.** VeriHarness is a research preview of an evidence and
verification harness. It is good at producing a record that survives scrutiny.
It is expensive at the task level, and on the five tasks it has been measured
against, a plain agent was cheaper and at least as correct. Both halves of that
sentence are in this release because both are measured.

## The headline: a baseline exists now, and it does not favour the harness

For most of this project's life its own `docs/LIMITATIONS.md` limit 2 said
there was **no baseline of any kind**. There are now three matched-budget
campaigns against a plain agent, none of them edited in the light of another.

Campaign **v3** was pre-registered in full -- 5 tasks x 3 arms x 3 repetitions,
unconditionally -- and the registration's exact bytes were bound in a git
commit **12.8 seconds before the first cell started**. That ordering is
established from git objects in
`docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` and re-derived by
`tests/test_prereg_provenance.py` on every suite run, not asserted.

| arm | hidden suite | false accepts | what it produced | dispatches |
|---|---|---|---|---|
| A -- plain agent | 15/15 PASS | 0 | answered | 1 per cell |
| B -- one `hoh run` | 14/15 PASS | **1** | 15/15 accepted | 6-7 |
| C -- full control plane | 14/15 PASS | 0 | **15/15 `BUDGET_EXHAUSTED`, 0/15 `CLOSED`** | 9 |

* **Arm A, the plain agent, passed everything** -- on one dispatch per cell, a
  ninth of what the harnessed arms were allowed.
* **Arm B produced the one false accept**: its own acceptance criteria green,
  the hidden suite red, on a normalisation that dropped a character. It is the
  defect class this benchmark exists to detect, and it **reproduced campaign
  v2's** -- same task, same arm. Three repetitions carry no rate; they carry
  that it was not a one-off.
* **Arm C reached no fixpoint at all.** Every one of its fifteen cells spent
  the nine dispatches on the primary node, met a red global gate, spawned a
  repair node, and had the repair refused for want of budget. In v2 the same
  arm closed five of five -- on eighteen dispatches per cell against a declared
  nine, because each run drew its own ceiling. The budget is now enforced per
  **cell** and shared with the repair nodes, and under the rule the protocol
  actually writes down, this control plane does not close on these tasks.
* **Arm C's zero false accepts is therefore not a correctness result.** A false
  accept requires an arm to claim it is finished and be wrong; arm C never
  claimed it. Quoting `0` for arm C beside arm B's `1` compares *answered
  wrongly once* with *never answered*.

`benchmark_v3 = PASS` on the readiness board means the campaign was
pre-registered, commit-bound, complete at 45/45, matched-budget-valid, and
reproducible from the raw cell files by a second, independently written
aggregation that never imports the reporter. It does **not** mean the harness
performed better. `docs/LIMITATIONS.md` section 19 says so in the document a
reader is most likely to quote from.

### The other half, measured separately

A ceiling that refuses too early is indistinguishable from one that refuses
correctly, if the only case you measure is the one where it binds. So the
positive path has its own evidence, at a budget **taken from v2** -- eighteen,
what v2's arm C actually spent reaching closure -- declared before the run and
never raised after a failure: the control plane reaches a real `CLOSED`
fixpoint through a full repair cycle, unattended. 18 of 18 declared dispatches,
9 primary and 9 repair, one repair node, **zero human decisions**.

The first attempt at that test failed with zero provider calls, because the
launcher had been built without an approval authority. The declared budget did
not move between attempts, and both artifacts are on disk.

## What is new in the software since rc3

* **The dispatch budget is enforced, not described.** `Budgets.max_dispatches`
  is checked *before* the counter is charged, persisted to run state before the
  provider call, shared by a node and its repair nodes, and survives a restart.
  Exhaustion is its own verdict (`BUDGET_EXHAUSTED`), its own halt class, and
  its own exception type -- never a generic failure.
* **A capability witness that a retry cannot launder.** Re-witnessing after a
  retry re-takes the baseline for named paths only, so a hostile write during
  the retry window can no longer be absorbed into the baseline.
* **Fail-closed environment-gap skips.** A test may skip for a missing artifact
  only when `EXPORT_MANIFEST.json` declares that path excluded for the reason
  the test expects. Missing-and-included fails. Missing-and-unclassified fails.
  Missing manifest fails. Wrong reason fails. Five negative controls hold it.
* **Telemetry that counts calls, not lines.** A retried role writes one log line
  for three calls and a refused role writes one for none, so the dispatch count
  now comes from a `provider_calls` field rather than from counting lines.

## The findings this release is actually made of

Each of these would have shipped green. The harness found them in itself.

* A benchmark whose reported cost was a **constant written beside the result**
  rather than a measurement.
* An enforced ceiling **off by one**: `max_dispatches=9` bought eight provider
  calls, because the counter incremented before the budget was consulted. The
  existing test passed against it, because it only checked that *something*
  raised.
* A dispatch log counted one line per call -- wrong **in both directions**.
* A budget a restart **refunded**, because the counter lived in a process.
* A capability witness that could be made to **absorb a hostile write** during a
  retry, found by an adversarial review tasked to refute rather than confirm.
* A published claim that **told readers how to verify it** and did not survive
  that verification: 167 of 329 failures in an export tree carried no
  environment-gap marker where the document promised none would.
* **Three release gates with no state in which they could fail** -- one asking a
  remote the internal tree deliberately does not have, one comparing a tree
  against itself.
* An export-skip rule that would have turned a **lost public file** green by
  skipping instead of red.
* A readiness row that counted **its own success message as a finding**: the
  export check prints "passes U2b + the leak scan", the row counted every line
  mentioning U2b, and a green export therefore reported "1 dangling
  reference(s)" while saying nothing about the ten acknowledged ones.
* Three documents that said a fresh clone reports 30 environment-gap skips.
  That holds for a `--depth 1` clone and not a full one, where two of the skips
  do not exist -- found by running this export's own suite before pushing it,
  and corrected in all three rather than in the one that was quoted most.
* A sentence in the position paper that carried a correct number and **a
  command beside it that computed something else** -- lines rather than
  distinct ids -- so a reader following the printed recipe would not get the
  printed figure.
* The export's own reference checker harvesting paths only from backticked
  spans and markdown links, so sixteen new audit rows naming internal evidence
  trees in bare form **passed it green**. The one exemption that existed before
  them had only ever been caught because the same path happened to appear
  backticked elsewhere in its row. Fixed for the references at hand; the
  detection gap is a tracked defect, not a patch written in the hour before a
  tag.
* And, before campaign v3 was frozen: an earlier version of the budget
  instrument **passed seven of eight controls against a build with the
  enforcement removed**. Five of its controls were reading the test fixture's
  own loop guard rather than the product's refusal. Two reviewers tasked to
  refute it found that, which is why the instrument now runs at three ceilings
  of which at least one must force the refusal inside an iteration -- or it
  refuses to run at all.

## What has not changed

* The positioning. This is a research preview, not a production system.
* The four hurdles in `README.md`: a written specification, a running Herdr,
  three agent dispatches per iteration, and JSON output.
* `docs/LIMITATIONS.md` remains the document to read before deciding this is
  ready for anything you care about. It has grown, not shrunk.
* No measurement left this machine. The public repository is an export: every
  file in it is `INCLUDE`-classified in `EXPORT_MANIFEST.json`, and the run
  evidence, raw receipts and internal working documents deliberately stay out.
  That is why a fresh clone reports explicit environment-gap skips, each naming
  the withheld artifact and why -- counted separately from its passes, and
  never summed with them. Measured on a fresh clone of the published tag:
  **30 skips** in a `--depth 1` clone and **28** in a full one, two of them
  existing only when the history is shallow. The passes beside them were 1221
  and 1223. The tagged tree's own copy of this paragraph says 1220 and 1222 --
  it was written before the last regression test was added, and the tag is not
  being moved to hide that. See the note at the end of these notes.

## Verifying this release yourself

```sh
git clone --depth 1 https://github.com/SKZL-AI/veriharness
cd veriharness
python3 -m pytest -q                        # passes and environment-gap skips, separately
python3 tools/check_claims.py check all     # every number bound to a claim or a reason
python3 tools/export_manifest.py check      # the export's own classification and leak scan
ruff check --select F,E9 src tests tools
```

`docs/READINESS.md` is the board: every row, its verdict, and the exact command
that re-derives it -- including the rows that are advisory and the one that is
`UNSUPPORTED_ENVIRONMENT` because GitHub's runners cannot create the namespace
it needs.


## Corrected after the tag was cut, and the tag is not being moved

`v0.1.0` was published, and then verified the way these notes tell a reader to
verify it: a fresh clone of the published tag, suite run, numbers compared.
Two of them were one short.

The tagged tree's own copy of this file, of `README.md` and of the position
paper says a `--depth 1` clone reports **1220 passed** and a full clone
**1222**. Measured on the published tag: **1221** and **1223**. The skip
counts -- 30 and 28 -- were right, and they are the figure those sentences are
about. The passes moved because the last regression test added before the tag
(for finding O169, the attribution check that had no reachable green state)
landed after the sentence was written.

This is the third instance of one class in a single release, and the class is
the subject of the section it keeps damaging: a number and the state it was
taken at drifting apart. The first was a heading tally with a command beside it
that counted something else; the second was a skip count true of one clone
shape and not the other; this is the third. The wording in all three places now
leads with the figure that does not move and names the tag and the clone the
others were taken in.

`v0.1.0` stays published, tagged and unmoved, exactly as `v0.1.0-rc1` through
`rc3` did when the same thing happened to them. A tag that is silently
corrected is worse than a tag with a known, stated, one-off-by-one error, and
this project has said so in print three times now.

---

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
