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

## Status

    protocol_frozen_at = the commit that introduces this file
    runs_completed     = see docs/BENCHMARK_RESULTS.md, which does not exist
                         until the first cell has run

Until that file exists, the honest status of limit 2 is unchanged: **no
baseline of any kind exists**, and no comparative claim may be made.
