# Role and model routing: what is decided, and what is not decidable yet

## The decision

**Routing is not configured, and that is the evidence-based disposition, not a
postponement for lack of time.**

The data that would decide it does not exist. `src/hoh/telemetry.py` records
what a dispatch cost; nothing wrote a record until this phase, so there is no
history of per-role cost, latency or quality to route on. Choosing a routing
policy now would mean choosing it from the two things people usually choose it
from — intuition and price lists — and this project's whole argument is that
those are not measurements.

What is in place is the ability to *configure* it, which has been there since
before this document: `hoh run --role-model planner=opus --role-model qa=sonnet`
and `--role-effort`, both recorded in the run so a verdict is comparable with
another. What is absent is a recommendation about how to set them.

## The assumptions that are explicitly not adopted

Two are common enough to be worth refusing by name, because adopting either
without data is how a routing policy becomes folklore:

**"QA is cheap."** QA is the role whose failure mode is the most expensive one
this project has: a QA that accepts a candidate it should not have accepted
produces a green that is wrong, and every downstream measurement inherits it.
This project has already lost a campaign's QA verdicts to an expired
credential presenting as an ordinary dispatch, and has separately caught a QA
marking criteria as discriminating while its own text said one of them "did
not exercise the candidate at all". Neither is an argument for a larger model;
both are arguments against assuming the role is the one to economise on.

**"The planner must be frontier."** Planning here is mostly writing an
acceptance criterion that runs, and the observed failures have been mundane:
a `cd` into an arena the guard refuses, a hard-coded stale arena hash, a
criterion invoking a `pytest` the sandbox has no copy of. Those are contract
failures, not reasoning failures. Whether a larger model makes them less often
is an empirical question nobody here has measured.

## What would decide it

A routing recommendation needs four things, and the first three are now
buildable:

1. **Per-role cost.** `DispatchRecord.tokens_in/out`, once the controller
   writes them. Where the harness reports no usage the field stays `None` and
   the aggregate says `NOT_DETERMINABLE` rather than summing the records that
   happened to report — a partial total is the number people quote.
2. **Per-role failure taxonomy.** `DispatchRecord.failure_class`, so
   "the QA model was worse" can be separated from "the QA dispatch hit a rate
   limit". Without that separation a routing experiment measures the provider's
   load, not the model.
3. **Per-role quality, in a form that is not self-reported.** Receipts per
   dispatch, criteria that discriminate, and — since O112 — how many of those
   discriminate only because the criterion's own file was absent from the
   baseline. A role that produces many criteria and few behavioural
   discriminators is producing volume.
4. **A matched budget.** Two configurations are comparable only if they were
   allowed the same resources, which is the same requirement the benchmark in
   `docs/LIMITATIONS.md` limit 2 is blocked on. Routing and the benchmark are
   the same measurement problem seen from two sides.

## The status line

    routing_decision = DEFERRED_ON_EVIDENCE
    reason           = no telemetry history exists; the mechanism to collect
                       one was built in this phase and has not run long enough
                       to say anything
    revisit_when     = per-role cost and failure-class data exist for at least
                       one matched-budget comparison

This is a disposition, not an open question left lying: the thing that has to
happen before it can be decided is named, the mechanism for it exists, and the
two assumptions a reader would otherwise expect to see adopted are refused in
writing.
