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

`docs/ARCHITECTURE.md` draws the exact line between what Herdr owns
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

## There is no automatism, and there should not be

HoH does not switch itself on: there is no background service that
intercepts agent work automatically, watching for something to plan and
verify. A human runs `hoh start` and then `hoh run`, naming a specific
repository, a specific specification, and a specific run.

Springing into action automatically would be **wrong, not merely
inconvenient**: HoH costs three role runs per iteration plus one control run
per new acceptance criterion, and something that quietly spends money in the
background is exactly the surprise the rest of this design guards against.
Herdr exposes event hooks one *could* wire this to; the decision whether a
given piece of work is worth that recurring cost belongs to the person
paying for it, not to HoH deciding on its own that a task looks worth
checking.

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
| `docs/ARCHITECTURE.md` | The Herdr/HoH boundary, and where each side of it is actually enforced in code |
| `docs/EVIDENCE_MODEL.md` | Candidate binding, receipts, the four evidence categories, and why `PASS` alone never suffices |
| `docs/RECOVERY.md` | Crashes, blocked dialogs, exhausted quotas -- the `attach`/`evaluate`/`block` verdicts and why an unclear state blocks rather than restarts |
| `docs/GOALBOOK.md` | Objectives across runs: `PROPOSED` does nothing, approval is a human decision, the frame gate |
| `docs/HERDR_INTEGRATION.md` | The Herdr plugin, its actions, and three properties of Herdr's plugin interface measured against a real Herdr 0.8.0 |
| `docs/OPERATIONS.md` | Prerequisites, the usual operational path, intervening in a running loop, the file layout, troubleshooting |
| `docs/LIMITATIONS.md` | What is not proven yet, stated specifically and without hedging |

Read `docs/LIMITATIONS.md` before deciding this is production-ready for
anything you care about. It names, among other things, that the guard around
acceptance-check commands is a tripwire against accidents rather than a
security boundary, and that this project's own dogfood evidence covers
documentation and governance work, never a change to `src/` or `tests/`.

## Existing internal documents

This project's original internal working documents -- planning and
operational notes written before this repository took its public form, in
German -- remain exactly as they are and stay on record. `docs/OPERATIONS.md`
is their English successor for operational purposes, not a replacement --
both stay on record.
