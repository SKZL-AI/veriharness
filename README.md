# VeriHarness -- Harness-of-Harness

VeriHarness — an evidence-bound Harness-of-Harness for verifiable
long-horizon agent development. `hoh` is the command-line package, module,
and CLI that implement the Harness-of-Harness pattern; VeriHarness is the
project and repository built on that pattern, not a second name for the
same thing. VeriHarness sits on top of Herdr, a terminal multiplexer for
coding agents, and drives a plan → develop → independent QA → evidence →
accept/reject → replan loop
around agent-written changes, so that acceptance rests on receipts a
deterministic runner produced, not on a model's own report that its work is
done. `docs/EVIDENCE_MODEL.md` makes the full case for why that distinction
matters; this file is the map of the rest.

## What this actually is

A specification goes in. A planner role turns it into a development plan
with machine-checkable acceptance criteria. A developer role implements
against that plan, in an isolated worktree it is the only writer to. An
independent QA role re-executes the criteria against a frozen, isolated copy
of the result and reports verdicts -- verdicts that HoH itself re-checks
against the runner's own receipts before trusting them. A candidate is
accepted only if every criterion passes *and* at least one of them is proven
to have been genuinely red on the state before the change. A rejected
candidate goes back into planning with its evidence attached, not into the
trash.

`docs/ARCHITECTURE.md` draws the exact lines: what Herdr owns
(sessions, panes, worktrees, restore) and what HoH owns (the plan, the
evidence, acceptance, replanning, and objectives that span multiple runs).

## Running it: two commands, on purpose

```sh
hoh start --repo <worktree> --spec <spec.md> --run-id <id>
hoh run <id> --iterations 2 --planner claude --developer claude --qa claude
```

`hoh start` only creates the run record. `hoh run` is the command that
actually dispatches roles and spends quota -- and it is a separate,
deliberate invocation for a reason, not an accident of the CLI's design.

## Automation is policy-controlled, not implicit

HoH does not switch itself on. There is no background service that
intercepts agent work, watches for something worth planning, and starts
spending quota on it. Nothing happens until something explicitly calls
`hoh start` and then `hoh run`, naming a specific repository, a specific
specification, and a specific run.

That is a statement about *implicit* automation, not about automation as
such -- and the distinction matters, because this project's own development
depended on it. There are two ways to operate the loop, and both are
intended:

**Manual CLI mode.** A person types the two commands. This is what the
examples document and the right way to start.

**Orchestrated mode.** A long-running agent session drives the loop: it
writes and revises specifications, starts runs, reads the resulting verdicts
and receipts, decides what to merge, executes the global gates, turns gate
failures into new repair runs, and repeats until the whole set closes. The
kernel cannot tell the difference -- it is the same CLI either way -- but the
*horizon* is, because a session that can read a rejection can also plan the
next run from it.

Implicit automation is refused while orchestrated automation is not, and the
reason is consent rather than capability. HoH costs three role runs per
iteration plus one control run per new acceptance criterion. Something that
quietly spends money in the background is exactly the surprise the rest of
this design guards against. An orchestrator spends the same money -- but it
does so because someone delegated that authority for a scope they named.
Herdr exposes event hooks one *could* wire a trigger to; whether a given
piece of work is worth the recurring cost is a policy decision, and this
design's position is that the decision must be *somewhere explicit*, not
that it must be re-made by hand every time.

Which decisions an orchestrator may take on its own is itself policy, not
architecture. In the campaign described below, a small number were reserved
for a human, and irreversible external actions -- publishing to a public
remote, for one -- still are. Nothing in the design requires that particular
split. A different deployment can delegate more, or less.

### Four layers, and what each one decides

| Layer | Decides | Exercised here |
|---|---|---|
| Human policy authority | Policy limits, non-delegated decisions, irreversible external actions | 7 governance decisions; the public release |
| Agentic main orchestrator | Specifications, when to run, what to merge, global gates, repair work, closure | The campaign below |
| VeriHarness / HoH verification kernel | One run: plan → develop → independent QA → evidence → accept/reject → replan | 65 runs |
| Herdr runtime | Sessions, panes, worktrees, restore | Every run |

The orchestrator is not hidden inside `src/hoh/`. It is a deployment layer
that drives HoH through the same CLI and Herdr interfaces anyone else would.
`docs/ARCHITECTURE.md` sets out what each layer may and may not do.

## What has actually been exercised

VeriHarness was developed and released using VeriHarness, in orchestrated
mode. Not as a demonstration assembled afterwards: the loop below produced
this project's documentation, its position paper, its claims ledger, its
export and release tooling, and the repair work that followed its own gate
failures.

Measured on the campaign that produced this release, derived from the run
records in the development repository. Those records are deliberately not
part of this export -- they carry working material, not product -- so the
figures are stated here rather than linked:

| | |
|---|---|
| Runs | **65** |
| Receipts | **1,988**, of which **880** are control runs |
| Iterations that produced receipts | **100** (98 of them criterion receipts) |
| Distinct specifications | **51** |
| Merges into the mainline | **45** |
| Recorded findings | **96** |
| Human governance decisions | **7** |
| Wall-clock | five calendar days; 3.5 days elapsed |

The findings are the point of that table more than the run count is. A
campaign that produces 96 recorded findings against its own harness is not a
campaign that went smoothly. The findings themselves live in the development
repository's working ledger, which does not ship; what ships is the subset
that became a standing limit, written out in full in `docs/LIMITATIONS.md`
rather than summarised. Limits 8 and 9 there are both findings from this
campaign that were never fully closed.

### Who wrote what

Three different authorities wrote to this repository, and collapsing them
into "the loop wrote itself" would be false in both directions -- it would
overstate the kernel and erase the work that happened above it.

| Author | What it wrote |
|---|---|
| HoH developer roles, inside scoped runs | Documentation, the position paper, examples, and -- where a specification named them -- test files |
| The orchestrating agent session, outside run write-boundaries | Specifications, core fixes to `src/hoh/`, gate and release tooling, merges |
| A human, as policy authority | Seven governance decisions, and every irreversible external action |

Measured over the non-merge commits reachable from the mainline -- a merge
commit is the *taking* of a run branch, not a direct write, so counting one as
"written directly" would invert what it shows:

| Path | Commits | Arrived via a run branch | Written directly |
|---|---|---|---|
| `src/hoh/` | 49 | 1 | 48 |
| `tests/` | 69 | 19 | 50 |
| `docs/` | 14 | 9 | 5 |
| `paper/` | 9 | 9 | **0** |
| `tools/` | 20 | 19 | 1 |

The `tools/` row is the one worth pausing on: all three of this project's
release-gate tools -- `check_claims.py`, `export_manifest.py` and
`union_gate.py` -- were created by runs, not written by hand, and so were the
three test files that guard them (`tests/test_claims_anchors.py`,
`tests/test_export_manifest.py`, `tests/test_union_gate.py`). The machinery
that decides whether this release may ship was itself produced through the
loop it gates.

The single `src/hoh/` commit that arrived via a run branch added packaged
policy data -- the guard pattern lists under `src/hoh/policy/` -- and no
Python. **No run wrote HoH's own logic.** A normal run's specification named
the paths it was allowed to touch, and HoH's source was excluded from that
set. Core fixes were therefore made deliberately *outside* the run boundary,
at the orchestrator level. Where those fixes repaired a defect this campaign
found in HoH itself -- eleven core defects and five guard gaps -- each was
declared as an exception and carries a test; the remainder of the 48 direct
commits is ordinary development that predates or sits outside that
accounting, and this README does not claim otherwise. All of it is a
considerably weaker claim than "the system repaired itself", and that is the
point of stating it this way.

## The four real hurdles to using this

Adopting HoH is not free, and pretending otherwise would undercut the whole
point of an evidence-first project. Four things stand between "I have an
agent" and "I have HoH running":

| Hurdle | Why it counts |
|---|---|
| A written specification | Few projects have one going in. Without it, the planner has nothing to bind acceptance criteria to. |
| Herdr must be running (`HERDR_ENV=1`) | HoH refuses to start otherwise -- deliberately, but it is a real prerequisite, not a formality. |
| Three agent sessions of quota per iteration | Planner, developer, and QA each run as their own dispatch. That is the recurring price of the loop, before counting the per-criterion control runs. |
| Output is JSON | `hoh` commands print structured JSON, not prose. Operable by scripts and other tools; not designed to be inviting on its own. |

Honestly stated: **HoH is currently a tool for someone who already wants
this discipline -- specification-first, evidence-bound acceptance -- and is
willing to pay its running cost, not one that makes that discipline easy or
cheap to adopt.** For a research preview, that is the right thing to say
about it. Claiming general, low-friction usability would not be.

## See it work -- including a rejection

`examples/minimal/` is a complete, small specification you can run through
the real loop yourself, plus a **recorded rejection** copied unmodified from
this project's own dogfood evidence: a real candidate, a real failing
preservation check, and the receipts that show why it was rejected rather
than accepted. Read `examples/minimal/README.md` first if you only have a
few minutes -- watching the loop refuse a candidate, with the evidence to
back that refusal up, explains more about what HoH is for than a paragraph
of prose can.

## The documents

| Document | What it covers |
|---|---|
| `docs/ARCHITECTURE.md` | The four layers -- policy, orchestrator, kernel, runtime -- and where each boundary is actually enforced in code |
| `docs/EVIDENCE_MODEL.md` | Candidate binding, receipts, the four evidence categories, and why `PASS` alone never suffices |
| `docs/RECOVERY.md` | Crashes, blocked dialogs, exhausted quotas -- the `attach`/`evaluate`/`block` verdicts and why an unclear state blocks rather than restarts |
| `docs/GOALBOOK.md` | Objectives across runs: `PROPOSED` does nothing, approval is a human decision, the frame gate |
| `docs/HERDR_INTEGRATION.md` | The Herdr plugin, its actions, and three properties of Herdr's plugin interface measured against a real Herdr 0.8.0 |
| `docs/OPERATIONS.md` | Prerequisites, the usual operational path, intervening in a running loop, the file layout, troubleshooting |
| `docs/LIMITATIONS.md` | What is not proven yet, stated specifically and without hedging |

Read `docs/LIMITATIONS.md` before deciding this is production-ready for
anything you care about. It names, among other things, that the guard around
acceptance-check commands is a tripwire against accidents rather than a
security boundary, and that multi-day orchestrated operation is
demonstrated while long-duration operation without any human intervention
is not.

## Existing internal documents

This project's original internal working documents -- planning and
operational notes written before this repository took its public form, in
German -- remain exactly as they are and stay on record. `docs/OPERATIONS.md`
is their English successor for operational purposes, not a replacement --
both stay on record.
