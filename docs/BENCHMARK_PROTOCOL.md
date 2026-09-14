# Matched-budget benchmark: the protocol, frozen before any run

`docs/LIMITATIONS.md` limit 2 has said since the first release that this
project has **no baseline of any kind**. Every claim about what the harness is
worth is therefore a claim about its internals, never a comparison. This
document is the protocol that would change that, written and committed before
the first arm runs, so that the analysis cannot be chosen after the results
are visible.

Everything below is fixed at the commit that introduces this file. Any change
after the first run is an amendment, recorded as one, with the reason and the
runs it invalidates.

## The three arms

| Arm | What it is | What it is supposed to isolate |
|---|---|---|
| **A** | A plain agent in one Herdr session, given the task and the repository, no harness | the baseline anybody already has |
| **B** | `hoh run` — one HoH run: plan, develop, QA, acceptance checks, receipts | what the *verification kernel* adds to a single task |
| **C** | The full control plane — `ProjectState`, global gates over the merged result, automatic repair nodes, closure to a fixpoint | what the *orchestration layer above runs* adds |

A and B differ by verification. B and C differ by composition. Both
differences matter and neither is measurable without the other arm.

## Matched budget

"Matched" is defined on the resource that is actually scarce, and it is
declared here because defining it afterwards is how a benchmark gets the
answer its author wanted:

* **Primary: dispatches.** Each arm gets at most **9 role dispatches per
  task**. B's three roles over three iterations exhausts it exactly; A gets
  nine turns; C's dispatches include those its repair nodes make.
* **Secondary, reported but not equalised: wall-clock and tokens.** They are
  recorded per arm. They are not equalised because equalising them would make
  the comparison about model speed rather than about method.
* **A run that exceeds the dispatch budget is stopped and recorded as
  `BUDGET_EXHAUSTED`.** It is not a failure of the arm; it is a result, and it
  is reported as its own outcome rather than folded into "did not pass".

## Tasks

Five, fixed here, each small enough to finish inside the budget and each
chosen because it has a failure mode the other four do not:

1. **`to_roman`** — a pure function with a specified table and two boundary
   errors. Nothing composes; the only question is correctness.
2. **`parse_duration`** — a parser with ambiguous input ("1m" as minutes or
   metres) that the specification deliberately under-determines. Tests the
   handling of a specification that cannot be satisfied as written.
3. **`ledger_apply`** — appending entries to a running balance, where a
   plausible implementation is correct in isolation and wrong when applied
   twice. A *composition* defect: exactly what arm C claims to catch and arm B
   structurally cannot.
4. **`normalise` + `slugify`** — two functions where the second must not break
   the first. A preservation task.
5. **`retry_with_backoff`** — behaviour under a fault the test injects. The
   one task where a plausible implementation passes a weak test suite and
   fails a strong one.

Each task ships with a **hidden acceptance suite** written before any arm runs
and not visible to any arm. That suite is the measurement. An arm's own tests
are evidence about the arm, never the verdict.

## What is measured

Per task and arm:

* `final_correctness` — the hidden suite's verdict on the final state.
* `false_accepts` — the arm reported success and the hidden suite disagrees.
  The headline number: this is what verification is for.
* `latent_defects` — the hidden suite passes and a specified property is
  nevertheless unmet (checked by reading, recorded with the reader's name).
* `composition_defects` — each artefact is individually correct, the combined
  state is not. Only measurable where an arm produces more than one.
* `repairs` — repair cycles the arm ran by itself.
* `dispatches`, `wallclock_seconds`, `tokens` (or `NOT_DETERMINABLE`).
* `provider_failures` by `FailureClass`.
* `ambiguity` — outcomes the arm could not classify: `UNKNOWN`,
  `INCONCLUSIVE`, `UNDETERMINED`.

## Analysis, fixed in advance

* The primary comparison is `false_accepts` per task, A vs B vs C.
* The secondary comparison is `composition_defects`, B vs C, on tasks 3 and 4
  only — the two where composition exists. Reporting it on task 1 would be
  reporting noise.
* **No significance test.** Five tasks and few repetitions cannot support one,
  and computing a p-value over six data points would be decoration. The result
  is a table of counts, with every run listed.
* **Every run is kept**, including the ones that fail for reasons that have
  nothing to do with the arm — a provider outage, a rate limit, a machine
  problem. They are reported under their `FailureClass` and excluded from the
  correctness comparison **only** when the arm never produced a final state,
  and each such exclusion is listed by name.

## Stopping rules, also fixed in advance

* Repetitions: **3 per (task, arm)** where the budget allows, 1 where it does
  not. Whichever is run is reported per cell; a cell with one repetition is
  labelled as such and is not averaged with a cell that has three.
* The benchmark stops when every cell has been attempted once. Attempting more
  is allowed; reporting a subset of cells is not.
* **A negative or null result is the result.** If B and C show no advantage
  over A on these tasks, that is what gets published, in the same detail. This
  sentence is the reason the protocol is committed before the runs.

## What this cannot establish, stated now

* **Generalisation.** Five small tasks in one language on one machine with one
  provider. The arms differ in method; they are not a sample of software work.
* **Cost-effectiveness.** Tokens are recorded where the harness reports them
  and are `NOT_DETERMINABLE` where it does not. A cost ratio computed over the
  runs that happened to report usage would be exactly the partial total this
  project keeps refusing elsewhere.
* **Anything about long-duration operation.** The tasks are minutes long. Limit
  1 is untouched by this benchmark and stays open.
* **That arm A is the strongest baseline available.** It is *a* baseline: a
  competent agent with no harness. A human-built harness, or a different
  agent framework, would be a different and more demanding comparison, and is
  not attempted here. Where this is written up, it must not be called
  "state of the art".

## Campaign v2: a replication, declared before it runs

Campaign `v1` ran before three defects in this harness were found and fixed.
Four of its five arm-C cells stopped for the same reason, and that reason was
one of them. A campaign whose failures are dominated by a defect measures the
defect, not the method — so v1 stays as it is, marked `PRE-O125-CLOSURE`, and
this section fixes the shape of the replication **before** it runs, for the
same reason the original protocol was frozen before the first arm.

    campaign_id       = v2
    protocol_version  = 2
    status            = DECLARED, not yet run

**What stays identical.** The same five tasks, the same hidden test suites, the
same matched-budget rule, the same outcome metrics, the same analysis, the same
stopping rules, the same exclusion rule. None of the sections above is
reopened. A replication that changes what it measures is a new experiment
wearing a replication's name.

**The one named intervention.** The planner capability boundary (O125/O126),
together with the two defects found while closing it: the blocked-pane
misreading (O127) and the telemetry wiring (O128). These are named here, in
advance, as the difference between v1 and v2. Nothing else about the harness
may be changed between the campaigns for the purpose of improving the result,
and any change that does happen is listed in the v2 results document whether or
not it looks relevant.

**No result-dependent analysis change.** The comparison, the exclusions and the
reporting are those already fixed above. If v2 looks worse than v1, that is the
finding and it is published in the same detail. If v2's arm C still stops on
approvals, the fix did not work, and that is a harness defect to report, not a
cell to drop.

**Repetitions.** Three per (task, arm) where quota allows, as above. Where it
does not, the order of priority is fixed now and not renegotiated later:
**first the four arm-C cells that O127 stopped in v1** (`parse_duration`,
`retry_backoff`, `slug_pair`, `to_roman`), then the remaining C cells, then B,
then A. A partial v2 reports exactly which cells ran and compares only those
against their v1 counterparts.

**What a green v2 would and would not license.** It would license the sentence
"in campaign v2, arm C produced *n* final states where v1 produced one". It
would not license "VeriHarness performs better than a plain agent": five tasks
on one machine with one provider cannot support that sentence, and §"What this
cannot establish" above applies to v2 unchanged.

## Changes made to the instrument after v2 began, declared

The protocol forbids a result-dependent *analysis* change. It does not forbid
fixing a broken measurement, and it would be worse to leave one in place --
but a fix made after a result is visible has to be named, or the distinction
is one nobody can check.

**2026-09-13, after cell 1 of v2 (`parse_duration/C`).** The `dispatches`
figure was `min(iterations * 3, DISPATCH_BUDGET)`: a constant in arm C, a
ceiling-clipped estimate in arm B. Counted from the run trees' own dispatch
logs, that cell spent 18 dispatches and reported 9 -- arm C runs a further
full run per repair node, and none of it was counted. The figure is now
counted, with `dispatch_budget` and `over_budget` beside it.

What this changes and what it does not: it changes a *reported cost*, in the
direction unfavourable to arms B and C. It changes no outcome metric --
`hidden_suite`, `false_accept` and `produced_final_state` are untouched -- and
it changes no comparison. Campaign v1's cells keep the constant they were
measured with; `docs/BENCHMARK_RESULTS.md` says what that column means there.

Also after cell 1: the orchestrator's unclassifiable-verdict branch now
carries the launcher's detail into the halt reason. That is a change to what a
*log* says, not to what an arm does, and it was made because cell 1 could not
be diagnosed without it.

## Campaign v3: pre-registered in full, before the first dispatch

v2 measured what it measured and keeps its findings. This section fixes the
shape of a third campaign **before** any of its cells runs, and it exists
because v2 exposed two things this document could not deliver: a repetition
rule nothing could decide, and a matched budget nothing could enforce.

    campaign_id       = v3
    protocol_version  = 3
    status            = DECLARED, not yet run

It is a pre-registration, so it is written to be checkable rather than
readable: every quantity below is a number or a rule that a tool can apply
without asking anyone what was meant.

### The design, in full

    tasks                 = to_roman, parse_duration, ledger_apply,
                            slug_pair, retry_backoff               (5, unchanged)
    arms                  = A, B, C                                (3, unchanged)
    repetitions           = 3 per (task, arm), without exception
    planned_runs          = 5 x 3 x 3 = 45
    dispatch_budget       = 9 per cell, hard, enforced
    max_campaign_cost     = 45 x 9 = 405 role dispatches (a ceiling, not a
                            forecast: arm A spends 1 by construction)
    hidden_suite          = unchanged; it is the verdict and no arm sees it
    primary_metric        = false_accept (an arm's own checks green, hidden
                            suite red)
    secondary_metrics     = final outcome, hidden-suite result, dispatches
                            measured, budget exhausted, repairs, human
                            decisions, wallclock, provider failures,
                            ambiguity, final correctness
    stopping_rule         = a cell stops at BUDGET_EXHAUSTED or at its arm's
                            own terminal state, whichever comes first
    analysis              = per (task, arm) aggregation over the 3 repetitions

Not every secondary metric exists for every arm. Repairs, human decisions and
closure are properties of arm C's control plane; arm B has iterations and
receipts but no repair nodes; arm A has neither. A metric that does not exist
for an arm is **absent** from that cell's record, never written as zero -- a
zero that means "not applicable" is indistinguishable from a zero that was
measured, and this project has already been caught reporting one of those.

`dispatch_budget = 9` is enforced rather than described. The ninth call
reaches the provider, the tenth is refused with the budget named, the count is
written to disk before each call so a crash cannot refund it, and a second
process reads the same spend; removing the enforcement turns every one of
those controls red. `python3 tools/budget_evidence.py` measures it and
reproduces the record, which this project keeps in its internal evidence tree
and the export does not carry (limitation 12e).

**The arms do not spend the budget symmetrically, and that is stated here
rather than discovered.** B and C are given the ceiling and enforced against
it. **Arm A is single-shot**: it is one agent turn with the specification and
the repository, and the instrument makes exactly one provider call for it
(counted in `arm_a`, not asserted). It is therefore *under* the ceiling, not
at it, and §"Matched budget"'s sentence "A gets nine turns" describes a design
that was never built -- in v1 and in v2 arm A spent one dispatch per cell.

The direction of that asymmetry is named too, because it does not point the
convenient way: more turns could only help arm A, so a campaign in which A
matches or beats the harnessed arms is **robust** to it, and one in which A
loses is not evidence that the harness is better. v3 does not repair the
asymmetry -- rebuilding arm A now would break comparability with v1 and v2,
which is a worse trade -- it declares it, and any v3 comparison carries it.

The task names are the directory names under `dogfood/benchmark/tasks/`, so
that a tool can check the list rather than interpret it. §"Tasks" above
describes the same five in prose: `slug_pair` is the `normalise` + `slugify`
pair, and `retry_backoff` is the one called `retry_with_backoff` there.

**`repetitions = 3` is unconditional.** Not "three where the budget allows":
that phrasing is what made v2's repetition requirement undecidable, and it is
not repeated here in any form. A campaign that cannot afford 45 runs reports
the cells it ran and names the ones it did not. It is **partial**, and it is
never re-declared afterwards as a design with fewer repetitions.

**No superiority claim from three repetitions.** n=3 per cell is enough to
show a false accept happening more than once and not enough to compare means.
A better average in one arm is reported as what it is -- three runs -- and
§"What this cannot establish" applies to v3 unchanged.

### What counts as a dispatch

The budget is meaningless without this, and v2's was:

* A dispatch is **one call that reaches a planner, developer or QA provider**.
  The initial node, every further iteration, every repair node, every schema
  repair, and every retry that actually re-calls the provider each count one.
* Deterministic local work counts nothing: acceptance checks, gates, digests,
  merges, closure, and any refusal that never reached a provider.
* **The nine are spent by the whole cell** -- one (task, arm, repetition) --
  not by each run inside it. A node and the repair nodes it spawns draw on one
  shared ceiling of nine. The design block above says `per cell` for that
  reason: it read `per run`, which is the same number for arms A and B and
  three times the number for arm C, and two lines of one pre-registration
  giving different budgets is exactly the ambiguity this campaign exists to
  remove.
* On spending dispatch 9 there is no tenth. Further work that would need one
  ends the cell in `BUDGET_EXHAUSTED` -- a measured final state, and
  explicitly not `PROVIDER_UNAVAILABLE`, not an ambiguous halt, and not a
  quiet close.

The figure is read from the runs' own records (`state.json` usage and the
`provider_calls` field of `telemetry.jsonl`), never asserted beside the
result. v2's cost figure was asserted, and it was wrong by a factor of two in
three cells.

### The instrument is frozen with the protocol

    benchmark_v3_protocol_commit    = recorded in
                                      docs/benchmarks/v3/PREREGISTRATION.json
    benchmark_v3_instrument_commit  = recorded in the same file

That file is written and committed **before the first dispatch**. It names
both commits, carries a digest for every frozen file, and records what the
campaign is running *on* -- interpreter, platform, package versions, agent CLI
version, and a digest per environment variable that reaches a role prompt.

`tools/prereg.py check` asks five questions, and it asks all five because an
adversarial review got past the first one alone:

1. does every frozen file still hash to what was recorded?
2. does the **commit** the registration names contain those same files? The
   registration cannot be inside its own digest set, and the repetition count
   is read out of that commit -- so without this, rewriting one field turned a
   45-run design into a 15-run design with no drift signal;
3. is the registration itself committed and unmodified?
4. was it frozen from a clean working tree? A registration naming a commit
   that never contained the instrument reads as evidence;
5. does any frozen tree contain a symlinked directory? Its contents are not
   covered by any digest, so it is reported rather than walked.

**Frozen for the duration of the campaign** -- changing any of these
invalidates the campaign rather than improving it:

* the benchmark runner (`tools/benchmark.py`),
* the budget semantics (`Budgets.max_dispatches`, `RunState.budget_exhausted`,
  `Controller._budget_oder_absage`, `HohRunLauncher`'s shared ceiling),
* the task definitions, their visible tests and their hidden suites,
* the evaluator, the definition of a false accept, and the definition of an
  outcome.

**Not frozen**, because they cannot influence what is measured: documentation
outside this file, the ledger, the readiness tool, and anything under
`docs/` that no arm reads.

**A pre-flight cell is allowed and is not a campaign cell.** Running one task
through one arm end to end -- trust helper, worktree, real dispatches, budget,
hidden suite -- before the campaign's first cell is spent finding out that the
pipeline is broken is ordinary care. It is run under the campaign id `smoke`,
into `dogfood/benchmark/results-smoke/`, which `tools/repetition_plan.py` does
not know and therefore cannot aggregate. A pre-flight result is never reported
as a v3 result, and a v3 cell is never satisfied by one.

**If the instrument turns out to be broken mid-campaign**, it is not repaired
mid-campaign. The runs that exist are preserved and marked `INVALIDATED` or
`PARTIAL` with the finding recorded, the repair is made afterwards, and a new
campaign (`v3.1` or `v4`) is pre-registered and started from zero. A dataset
half-measured with one instrument and half with another is not a dataset.

### No leakage from v2's result

v2 found exactly one false accept: `slug_pair`, arm B, a normalisation that
dropped a character (`'ahnlich-ja' != 'hnlich-ja'`). That finding must not be
allowed to shape the campaign that is supposed to reproduce it:

* **No task-specific change to `slug_pair`** -- not to its specification, its
  visible tests, its hidden suite, or its acceptance criteria -- for the
  purpose of making that false accept appear or disappear.
* Only generic product changes that were independently justified and frozen
  before v3 started may take effect during it.
* If v3 does not reproduce the false accept, that is a result about n=3, not
  evidence that it was fixed.

### What a completed v3 would license, and what it would not

It would license a comparison of false accepts per task across A, B and C
under a budget that was actually matched -- the sentence this benchmark was
built to say and has not been able to say yet. It would not license
"VeriHarness performs better": five small tasks on one machine with one
provider cannot support that.

## What the frozen text does not decide, found while enforcing it

Two findings from reading the frozen commit against the campaigns, recorded
here because a protocol's gaps belong in the protocol:

**`where the budget allows` is not decidable as written.** The repetition rule
asks for three per cell "where the budget allows", and the only budget this
document defines governs a single run: nine role dispatches per task per arm.
A per-run ceiling cannot say whether a *second run of the same cell* is
affordable, and no campaign, quota or wall-clock allowance appears anywhere in
this file. The bullet immediately after it sets the stopping rule at one
attempt per cell and calls more "allowed". Both readings are convenient in
opposite directions, so neither is adopted:

    protocol_repetition_requirement = NOT_DETERMINABLE

A campaign measured under it may be reported as what it measured -- one
repetition per cell -- and not as fulfilment of the three-repeat design this
document claims. `tools/repetition_plan.py` derives that table from the frozen
commit rather than from this file as it stands today.

**The dispatch budget was defined and never enforced.** This document says a
run that exceeds it "is stopped and recorded as `BUDGET_EXHAUSTED`", and that
arm C's repair-node dispatches count. Neither happened: three of campaign v2's
five arm-C cells spent 18 against 9 and halted normally. That is an instrument
defect, not a protocol gap -- but it means those cells did not have the matched
resource this benchmark is named after, and a future campaign has to enforce
the rule rather than report it.

**A v3 may define both properly.** Neither is retro-specified here. Writing a
sharper rule into this file now and treating it as though it had been
pre-registered is exactly the move freezing a protocol exists to prevent.

## Status

    protocol_frozen_at = the commit that introduces this file
    runs_completed     = see docs/BENCHMARK_RESULTS.md

Campaign v1 is complete at 15 of 15 cells and is marked `PRE-O125-CLOSURE`.
Campaign v2 is declared in the section above and has not run.

Until that file exists, the honest status of limit 2 is unchanged: **no
baseline of any kind exists**, and no comparative claim may be made.

*Superseded on 2026-09-13, and kept because this project corrects by addition
rather than by overwriting.* That file now exists. Limit 2's current status is
that a baseline exists and it does **not** favour this harness: arm A, a plain
agent with no control plane, produced the most passing cells. No comparative
claim in this project's favour may be made from it either.
