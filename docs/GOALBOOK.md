# The goalbook: objectives that outlive a single run

A `run` is HoH's largest unit today: one specification, one iteration
budget, one accepted-or-not outcome. That is deliberately small. The
goalbook (`src/hoh/goalbook.py`) exists for the layer above it -- work that
is supposed to span multiple runs -- and it exists specifically so that,
without it, run 7 would not know what run 3 already established and would
plan it again from nothing.

## The open question this answers: who gets to decide what HoH works on next?

A planner role operating at the objective level, deciding for itself what
the next run should be about, is the natural continuation of HoH's own
design -- and it is also the exact point at which a model would start
handing itself assignments rather than a human deciding what is worth the
cost. The goalbook's design resolves that tension with one rule, stated in
the module's own docstring: **the AI may extend the frame, the human sets
it.**

## `PROPOSED` does nothing

`Goalbook.propose()` files new objectives with `status="PROPOSED"` and stops
there. A `PROPOSED` objective is inert: `Goalbook.next_open()`, which is what
decides what gets worked on next, only ever returns an objective whose
status is `ACTIVE` or `OPEN` -- a `PROPOSED` entry is invisible to it. An
agent, including HoH's own planner role, can call `propose`; nothing in this
module lets an agent call the next step itself.

## Approval is a human decision

`Goalbook.decide()` is the only way an objective moves out of `PROPOSED`,
and it requires a `by` (who decided) and a `reason` -- both persisted, both
required arguments, not optional ones. This is not merely a UI convention:
`decide()` is also where `DONE` is refused outright unless the objective
already carries `evidence_refs` -- "DONE requires an accepted candidate with
receipts, not a model's assertion," in the code's own words. An objective
cannot mark its own completion by asserting it finished; it needs the kind
of receipt-backed acceptance described in `docs/EVIDENCE_MODEL.md`.

Nothing is ever deleted from the goalbook either. A rejected proposal
becomes `DROPPED`, with the reason and who decided it kept on the record --
the same "nothing is deleted, only re-labeled with a reason" discipline this
project applies to its evidence and its parked state files.

## The frame gate

Not every new objective needs a human to stop and look -- that would make
the gate a click-through nobody reads. But some proposals genuinely change
what HoH is being asked to pursue, and those need attention rather than a
rubber stamp. `Goalbook.check_frame()` decides which is which, and returns a
`FrameCheck` that names its reasons explicitly rather than a bare
true/false, specifically so a human can see *why* the gate engaged in one
glance instead of having to infer it.

The gate engages when any of these hold:

- **The goalbook is currently empty.** The first objective ever proposed
  necessarily sets the frame from nothing, so it is always gated.
- **More than `MAX_NEW_PER_PROPOSAL` (3) new objectives arrive in one
  proposal.** This mirrors `MAX_PRIORITIES = 3` from the planner's own
  contract -- more than three priorities at once is already considered too
  many there.
- **More than `MAX_OPEN` (10) objectives would be open afterwards.** Past
  this point the goalbook's own docstring calls the frame "exhausted" --
  whoever wants more should finish or discard something first.
- **A single objective's description exceeds `MAX_CHARS_PER_OBJECTIVE`
  (2000) characters.** Length is a crude but honest proxy here: an
  objective description this long usually describes several objectives
  bundled into one.

When the gate is required, confirmation is asked for **the frame as a
whole**, not objective by objective -- the module's own reasoning is that
gating each new item individually would let a large expansion walk through
in a series of small, individually-unremarkable steps. The thresholds
themselves are explicitly not a security boundary; they are what the code
calls an *attention* boundary -- a line that decides when a human has to
look, not a line that anything is prevented from crossing outright.

## Where this leaves a reader

If you are deciding whether to let HoH propose its own follow-up work: it
can, but only as an inert suggestion sitting in `PROPOSED` until you (or
whoever the `--by` name identifies) explicitly approve it, and any proposal
that meaningfully widens what has already been approved will surface a
frame-gate report asking you to look at it as a whole before it becomes
workable.
