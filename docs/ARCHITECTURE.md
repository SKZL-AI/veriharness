# Architecture: four layers, and where each boundary is enforced

HoH is not a replacement for a terminal multiplexer, and it is not a
harness. It sits on top of one, deciding things a multiplexer has no opinion
about. This document draws the lines as concretely as possible: not "Herdr
does infrastructure and HoH does logic" as a slogan, but which module enforces
which half, and where you would look to check that a line is real rather than
aspirational.

There are four layers, not two. This document used to describe only the
middle two, which was accurate about the code and incomplete about the
system: the layer that decides *which runs happen at all* was missing from
it, and that is the layer this project's own development ran on.

*Read with the section at the end, "Since v0.1.0: what this description no
longer covers". It corrects two statements below by addition -- they stay as
written -- and names the parts of the package this document does not yet
describe.*

## The four layers

```
  Human policy authority
        |   policy limits, non-delegated decisions, irreversible external actions
  Agentic main orchestrator
        |   specifications, run scheduling, merges, global gates, repair, closure
  VeriHarness / HoH verification kernel
        |   one run: plan -> develop -> independent QA -> evidence -> accept/reject
  Herdr runtime
        |   sessions, panes, worktrees, restore
  Planner / developer / QA sessions, in their worktrees
```

| Layer | May decide | May not | Lives in |
|---|---|---|---|
| Human policy authority | What the orchestrator is allowed to decide; anything reserved from it; irreversible external actions | -- | Outside the repository, by configuration and instruction |
| Agentic main orchestrator | What to specify, when to run, what to merge, when a release is closed, what repair work a gate failure implies | Cannot bypass a run's verdict, cannot construct a `Receipt`, cannot mark a rejected candidate accepted | A deployment layer, **not** `src/hoh/`; drives HoH through the CLI and Herdr |
| HoH verification kernel | Plan binding, acceptance, preservation, replanning, cross-run objectives | Cannot open a pane, cannot decide which runs exist | `src/hoh/` |
| Herdr runtime | Sessions, panes, worktrees, liveness facts | Has no opinion on acceptance | Herdr itself |

The orchestrator deserves the emphasis because it is the layer most easily
mistaken for something it is not. **It is not a hidden component of `src/hoh/`,
and it is not a scheduler shipped with this repository.** It is an agent
session that holds a long-horizon objective and drives HoH through exactly the
interfaces documented here -- the same `hoh start` / `hoh run` a person would
type. Nothing in `src/hoh/` knows whether its caller is a person or an
orchestrator, and that is deliberate: the kernel's guarantees must not depend
on who invoked it.

The consequence runs the other way too, and it is the honest half. Because the
orchestrator sits *above* the kernel, **none of the kernel's guarantees apply
to it.** Its decisions are not receipt-bound. When it merges two runs, no
acceptance check attests that merge. That gap is precisely why the global
closure layer described in `docs/LIMITATIONS.md` §9 exists: the invariants
`U1`-`U5` and the post-DAG closure pass are what check the orchestrator's own
composition decisions, because the per-run loop structurally cannot.

## The cut between Herdr and HoH, in one table

| Concern | Owner | Enforced in |
|---|---|---|
| Terminal panes, tabs, workspaces | Herdr | Herdr itself; HoH never opens a pane directly |
| Starting an agent session, reading its transcript | Herdr | `src/hoh/herdr.py` calls the `herdr` CLI; HoH holds no session state of its own |
| Git worktrees for isolated development | Herdr | `herdr.worktree_create` / `hoh worktree`; HoH does not reimplement `git worktree add` |
| Deciding whether a running agent is alive, finished, or unclear | Herdr's state, HoH's decision | `herdr.liveness()` reads `herdr api snapshot`; the three-way answer (`attach`/`evaluate`/`block`) is HoH's own rule, documented in `docs/RECOVERY.md` |
| The development plan and its acceptance criteria | HoH | `src/hoh/contracts.py`'s `DevelopmentPlan`/`AcceptanceCheck`, produced by the planner role and bound to the run in `Controller._assert_plan_bound` |
| Executing an acceptance check and recording what happened | HoH | `src/hoh/runner.py`'s `run_check` -- the **only** place a `Receipt` is constructed |
| Whether a candidate is preserved across iterations | HoH | `Controller._checks_for` / `_record_preserved` in `src/hoh/controller.py` |
| Accepting or rejecting a candidate | HoH | `RoleResult.accepted()` in `src/hoh/contracts.py`, called from `Controller._run_iteration_locked` |
| Replanning after a rejection | HoH | `stages.reject_candidate` -- the only permitted `VERIFYING -> PLANNING` edge |
| Objectives that outlive a single run | HoH | `src/hoh/goalbook.py` |

## Why the cut sits where it does

Herdr's job is to make an agent session **demonstrable**: a real pane, a real
workspace, a real endpoint you can point at and say "this is the session that
did the work." That is exactly what handoff §1 (the founding design note for
this project) calls A01: no silent fallback to some other multiplexer, no
claimed session that cannot be pointed at. `src/hoh/herdr.py`'s
`require_herdr()` enforces this at the boundary -- if `HERDR_ENV` is not `1`
or the `herdr` binary is not on `PATH`, HoH refuses to claim a Herdr-backed
run rather than quietly falling back to a plain subprocess.

HoH's job starts on the other side of that boundary: given that a role ran
somewhere real, was what it produced actually checked, and does the checked
result deserve to be called accepted? Herdr has no opinion on any of that --
it does not know what a `DevelopmentPlan` is, and it is not supposed to.
That is deliberate: coupling the two would mean every project that wants
Herdr's session management would also inherit HoH's acceptance machinery,
and every project that wants HoH's acceptance discipline would be forced
into Herdr specifically. `src/hoh/dispatchers.py` documents the seam
directly: `HerdrDispatcher` starts each role in its own Herdr pane;
`HarnessDispatcher` runs the same harness CLIs as plain subprocesses,
"more robust and runnable without Herdr -- but without a Herdr endpoint and
therefore no A01 evidence." Swapping one for the other changes nothing
about how a plan is checked or a candidate accepted.

## Where the boundary is actually enforced, not just described

A description of a boundary is not the same as code that holds it. Three
concrete enforcement points, because this is the part that is easy to get
wrong in prose and hard to fake in code:

**The object under test is never the live workspace.** `Controller._verify`
in `src/hoh/controller.py` never runs an acceptance check against
`state.repo_path` directly. It calls `workspace.materialize()` to extract an
isolated copy of the exact `git`-tree that was frozen, into an arena
directory that `Controller._arena` places *next to* the run directory, never
inside it. `runner.assert_stays_in_arena` additionally refuses any check
command that tries to `cd` out, use an absolute path outside the arena, or
start a shell of its own. Herdr has no equivalent concept -- it does not
isolate what an agent's shell commands can reach; that is HoH's guard, and
its known limits are named plainly in `docs/LIMITATIONS.md`.

**The developer is the only writer to the artifact.** `workspace.py`'s
`commit_candidate` refuses to commit unless the target is a linked git
worktree (`.git` is a file, not a directory) created for this run, on a
branch that is not `main`/`master`/`develop`/`trunk`/`release`. Herdr creates
that worktree; HoH refuses to write anywhere else. The planner and QA roles
get a **read-only copy** of the candidate (`materialize` into their own
arena), never the worktree the developer writes in.

**One controller per run, and no writer works on a stale state.**
`RunStore.lock()` (in `src/hoh/store.py`) is a cross-process `flock`; two
`hoh run` invocations against the same run cannot interleave writes. On top
of the lock, `RunState.write_seq` is a fencing token: a controller holding an
old `RunState` object that tries to write after someone else has already
advanced the state is rejected, not merged. Herdr's own locking (if any)
protects panes and worktrees; it says nothing about two HoH controllers
racing to accept the same candidate, which is why HoH keeps this guarantee
itself instead of assuming Herdr provides it.

## What crosses the boundary, explicitly

Two objects cross from one side to the other on every role dispatch:

- **The prompt**, built by `src/hoh/roles.py` from the specification, the
  frozen plan, and the evidence bundle -- HoH's, handed to Herdr only as
  text to inject into a pane it creates.
- **The raw answer**, written by the agent to a file path HoH gave it
  (`dispatchers.py`'s `_answer_instruction`) rather than scraped from
  terminal text -- because, as that module's docstring puts it, "a pane
  contains TUI frames, status lines and wrapped lines; fishing JSON out of
  that is brittle."

Everything else -- which panes exist, which agent is idle, which workspace
a worktree lives in -- stays on Herdr's side of the line and is read, never
assumed, through `herdr.py`'s thin CLI wrapper.

## Since v0.1.0: what this description no longer covers

Added for the v0.3.0a1 pre-release. Nothing above is rewritten; where a
statement above no longer holds, it is corrected here, with the evidence.

**Correction 1 -- the package does carry a control plane.** The layer table
says the orchestrator lives in "a deployment layer, **not** `src/hoh/`", and
the paragraph after it says it is "not a scheduler shipped with this
repository". Both were already inaccurate at the `v0.1.0` tag:
`src/hoh/orchestrator.py` (with `src/hoh/launcher.py`) has been package code
since 2026-09-11. Its `ProjectController` decides which runs of a project
exist and in what order, runs the global gates over the merged result and
creates repair nodes when they fail; the benchmark's arm C and the closure
test exercise it. What the paragraphs above describe is still true of the
**agentic** orchestrator -- the agent session that writes specifications,
starts product runs, reads verdicts and holds the merge authority -- which
drives the package from outside through the CLI and Herdr. There are
therefore two orchestration layers, one in the package and one above it, and
"the kernel cannot decide which runs exist" is true of `controller.py`, which
drives one run, not of the package as a whole.

**Correction 2 -- `run_check` is not the only constructor of a `Receipt`.**
The cut table calls `runner.run_check` "the **only** place a `Receipt` is
constructed". The controller also constructs two receipts itself, both for a
check that was refused before it ran: one when the baseline's command is
refused, one when the guard refuses a candidate's check. Both record exit
code 126 and `runner_ok=False`, and both are written by HoH, never by a model.
The property that matters -- a model cannot produce a receipt -- holds; the
sentence that said exactly one function produces them does not.

**Who decides what, now that HoH builds HoH.** Five steps, each with its own
authority, and none of them implies the next:

| Step | Decided by | Can refuse what the step before it passed |
|---|---|---|
| A product run writes candidate bytes | the developer role, in its own worktree | -- |
| Acceptance | the controller, from the runner's receipts and QA's re-checked verdicts | yes: a candidate whose criteria are not all demonstrated |
| Integration review | two independent reviewers, outside the run | yes: candidates the product accepted have been refused at this step |
| Merge | the orchestrating session, which holds the merge authority, by byte-identical import of the accepted candidate | yes: one accepted and reviewed run stayed unmerged until its attribution could be recorded honestly (ledger O179) |
| Phase closure | the phase gate, derived from the capability register and an independent supervisor review | yes: a phase does not close over an open finding |

**What the package now holds that this document does not describe.** The
modules below were added after `v0.1.0` (the list is the difference between
`src/hoh/` at that tag's commit and now); grouped by concern, each module's own
docstring is the description until this document is extended in a later
revision:

* *Durable programme runtime (P2):* `timers.py`, `sentinel.py`,
  `resumption.py`, `resumeloop.py`, `epoch.py`, `succession.py`,
  `autonomy.py`, `governance.py`, `checklist.py`, `supervisor.py`,
  `capsule.py`, `programlogic.py`, `taskstate.py`, `rotation.py`,
  `migrations.py`, `decisions.py`, `decisioncheck.py`, `thread.py`,
  `projectcontract.py`, `gateway.py`, and, merged at `601168fe` and not yet
  exercised live, `succession_capsule.py` and `rollover.py` (context rollover,
  Layer A: a capsule of re-derivable facts and the protocol that hands the
  master epoch to a fresh orchestrating session).
* *Storage lifecycle (amendment A1, and P2-42):* `storage_registry.py`,
  `storage_pressure.py`, `storage_park.py`, `storage_ephemeral.py`,
  `durable_io.py`, `archive_bundle.py`, `archive_target.py`,
  `rehydration.py`, `storage_coordinator.py`, `storage_host.py`,
  `storage_tiers.py`, `verified_relocation.py`, `storage_steward.py`,
  `legacy_scratch.py`.
* *Role panes and processes:* `pane_ledger.py` and `procs.py` (the role-pane
  lifecycle, merged at `caa732db`: HoH closes its own role panes and ends the
  processes a role left running before the freeze, by a minted mark anchored to
  the pane's own shell, and names, never signals, what it cannot tie to the pane).
* *Isolation and integrity:* `integrity.py` (inventories before and after a
  run) and `approval_policy.py` (what a role may do without asking). The
  role capability witness, `capability.py`, and the namespace sandbox,
  `sandbox.py`, are older and are extended: the witness now also seals and
  freezes QA's candidate arena.
* *Evidence and proof:* `proofclass.py` and `evidenceclaim.py`.

**What crosses the boundary, revised.** The prompt no longer always crosses
as one piece of text. A role prompt longer than one command-line argument may
be is written to a prompt file in the run's `answers/` directory, and Herdr
receives a short pointer to it (since the prompt-threshold merge, every role
prompt above 8 KiB, or holding a character the input box treats specially,
goes this way, so that the recovery after a stalled submit can verify the box); the dispatcher checks the file's SHA-256 after
the turn against a digest it kept in memory, and records an audit line before
the dispatch. The answer still comes back through a file path HoH gave the
role. Known limit: the directory chain up to those files is trusted
(`docs/LIMITATIONS.md`, *Current open limitations*).

**Where HoH's own state now lives in the self-hosted deployment.** The
project develops itself with each product run in an isolated clone pinned to
one commit, under a registered run root outside the canonical repository; the
canonical repository is written only by the orchestrating session's merges.
This is a deployment choice, not a property of `hoh start`, whose default
layout is unchanged (limit 6).
