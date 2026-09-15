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

## Installation

Install the command-line package from [PyPI](https://pypi.org/project/hoh/):

```sh
pip install hoh
```

To pin the current stable release:

```sh
pip install hoh==0.1.0
```

The package requires Python >=3.11 and targets Linux; actual agent runs require Herdr.
Fresh PyPI installations passed the CLI, metadata, and policy checks on Python 3.11, 3.12, and 3.13.
The [distribution receipt](.github/releases/v0.1.0.json) records the immutable source, publication workflow, and artifact hashes.

For development, install an editable checkout:

```sh
git clone https://github.com/SKZL-AI/veriharness.git
cd veriharness
python -m pip install -e .
```

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
| Recorded findings | **127** |
| Human governance decisions | **7** |
| Wall-clock | five calendar days; 3.5 days elapsed |

The findings are the point of that table more than the run count is. A
campaign that produces 127 recorded findings against its own harness is not a
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

Measured over the non-merge commits reachable from the mainline **of the
development repository, at commit `30ff053`** -- a merge commit is the *taking*
of a run branch, not a direct write, so counting one as "written directly"
would invert what it shows. The published repository is an export with its own,
much shorter history; these counts do not describe it and are not reproducible
there.

| Path | Commits | Arrived via a run branch | Written directly |
|---|---|---|---|
| `src/hoh/` | 49 | 1 | 48 |
| `tests/` | 70 | 19 | 51 |
| `docs/` | 15 | 9 | 6 |
| `paper/` | 10 | 9 | 1 |
| `tools/` | 20 | 19 | 1 |

Naming that commit is not pedantry, and the first published version of this
table got three of its five rows wrong for want of it. The table counts commits
to paths the table itself lives in, so **the commit that writes these numbers
changes them**, and a count taken "now" is stale the moment it is written down.
A measurement of a tree has to name the tree.

The `paper/` row deserves its own sentence, because it changed while this
release was being prepared. Nine of its ten commits came from runs: the
position paper was written by the loop, not by hand. The tenth is the
correction that produced this release -- sections 1.1 and 1.2, written directly
by the orchestrating session, outside any run. Until that commit the column
read zero, and saying so now rather than leaving the older, tidier number in
place is the same discipline the rest of this section is about.

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

## Measured against a plain agent: the matched-budget benchmark

For most of this project's life `docs/LIMITATIONS.md` limit 2 said there was
**no baseline of any kind**, so every claim about what the harness was worth
was a claim about its own internals. There are now three campaigns, and none
was edited in the light of another. The full protocol is
`docs/BENCHMARK_PROTOCOL.md`, frozen before the first cell ran; the results
are `docs/BENCHMARK_RESULTS.md`, `_v2.md` and `_v3.md`.

Five small tasks, three arms -- **A** a plain agent with no harness, **B** one
`hoh run`, **C** the full control plane with global gates and repair nodes --
a hidden test suite per task that decides the verdict and that no arm sees,
and a budget matched on the scarce resource, role dispatches. The headline
metric is the **false accept**: the arm's own checks green, the hidden suite
red.

Campaign v3 was pre-registered in full -- 5 tasks x 3 arms x 3 repetitions,
unconditionally -- and the registration's exact bytes were bound in a git
commit 12.8 seconds before the first cell started. That is established from
git objects in `docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` and
re-derived by `tests/test_prereg_provenance.py` on every suite run, rather
than asserted.

| arm | hidden suite | false accepts | what it produced | dispatches |
|---|---|---|---|---|
| A -- plain agent | 15/15 PASS | 0 | answered | 1 per cell |
| B -- one `hoh run` | 14/15 PASS | **1** | 15/15 accepted | 6-7 |
| C -- full control plane | 14/15 PASS | 0 | **15/15 `BUDGET_EXHAUSTED`, 0/15 `CLOSED`** | 9 |

**Read this the unflattering way, because it is the correct way.** On these
five tasks the plain agent was cheaper and at least as correct. Arm C's zero
false accepts is not a correctness result: a false accept requires an arm to
claim it is finished and be wrong, and arm C never claimed it -- it spent its
nine dispatches, met a red global gate, spawned a repair node, and had the
repair refused for want of budget, fifteen times out of fifteen. In v2 the
same arm closed five of five, on eighteen dispatches per cell against a
declared nine; the budget is now enforced per cell and shared with the repair
nodes, and under that rule this control plane does not close on these tasks.

Arm B's one false accept is the finding the benchmark exists to produce -- and
it **reproduced v2's**: the same task, the same arm, a normalisation that
dropped a character. Three repetitions carry no rate; what they carry is that
it was not a one-off.

`benchmark_v3 = PASS` on the readiness board means the campaign was
pre-registered, commit-bound, complete at 45/45, matched-budget-valid, and
reproducible from the raw cell files by a second, independently written
aggregation that never imports the reporter. It does **not** mean the harness
performed better.

The positive path is measured separately, because a ceiling that refuses too
early is indistinguishable from one that refuses correctly if you only ever
measure the case where it binds. At a budget taken from what v2's arm C
actually spent -- eighteen, decided before the run, never raised afterwards --
the control plane reaches a real `CLOSED` fixpoint through a full repair
cycle: 18 of 18 declared dispatches, 9 primary and 9 repair, one repair node,
**zero human decisions**. The first attempt at that test failed with zero
provider calls, and that artifact is kept beside the result.

## What it demonstrably does

The benchmark says what the harness did not do. This is what it did, each item
bound to something you can re-run.

**It makes "green" mean something, and proves it by failing on purpose.** Five
release-critical metrics carry a falsifier -- a deliberately broken build the
instrument must catch, or the metric stays `NOT_RUN`. The dispatch-budget
instrument runs eleven controls at three ceilings (`[9, 8, 4]`, at least one
of which must force the refusal *inside* an iteration, or the tool refuses to
run) plus two falsifiers that delete the enforcement. That rule exists because
an earlier version of the same instrument passed **seven of eight** controls
against a build with the enforcement removed: five of its controls were
reading the test fixture's own loop guard rather than the product's refusal.
Two reviewers tasked to refute it found that before campaign v3 was frozen.

**It refuses states that are conventionally rounded up to a pass.** `NOT_RUN`,
`NOT_DETERMINABLE`, `UNSUPPORTED_ENVIRONMENT` and `INVALIDATED` are separate
states, and none of them collapses into a pass. The public CI's sandbox job is
green with its relevant step skipped, because GitHub's runners cannot create
the namespace -- reported as `UNSUPPORTED_ENVIRONMENT`, never derived as a
pass from the green job. A fresh clone of this repository reports its passes
and its environment-gap skips as two separate numbers that are never summed,
each skip naming the withheld artifact and why. Measured on a fresh clone of
the published `v0.1.0` tag: **30 skips** in a `--depth 1` clone, which is what
CI makes, and **28** in a full one -- two of them exist only when the history
is shallow, so the count is quoted with the clone it was taken in. The passes
beside them were 1221 and 1223 respectively; that number moves with every test
added, which is why the skip count is the one this paragraph is about. A skip is
authorised only when `EXPORT_MANIFEST.json` declares that path excluded for
the reason the test expects; missing-and-included fails, missing-and-
unclassified fails, missing-manifest fails, wrong-reason fails. Five negative
controls hold that rule in place, because the first version of it would have
turned a **lost public file** green by skipping instead of red.

**It binds acceptance to receipts it did not write.** A candidate is accepted
only when a receipt a deterministic runner produced supports every criterion,
and only when at least one criterion is proven to have been red on the state
before the change. The planner's capability boundary is enforced by digest
rather than requested in a prompt, and the instrument that measures it plants
seven violations into throwaway copies and must catch each one -- **7 of 7**.

**Every number in the public documents is anchored.** `CLAIMS.json` binds each
number-bearing sentence in `README.md` and `docs/**` to a claim with evidence,
or to an explicit `not_claims` entry with a reason. `python3
tools/check_claims.py check all` fails on an unbound sentence, on an anchor
whose content changed, and on two claims that share one resolution sentence.
This paragraph is itself subject to it.

**And it found these things in itself.** The list is the product more than the
software is: a benchmark whose reported cost was a constant written beside the
result rather than a measurement; an enforced ceiling off by one, where
`max_dispatches=9` bought eight provider calls because the counter incremented
before the budget was consulted, with an existing test that passed against it
because it only checked that *something* raised; a dispatch log counted one
line per call, wrong in both directions; a budget a restart refunded; a
capability witness that could be made to absorb a hostile write during a
retry; a published claim that told readers how to verify it and did not
survive that verification (167 of 329 failures in an export tree carried no
environment-gap marker where the document promised none would); and three
release gates that had no state in which they could fail -- one of them asking
a remote the internal tree deliberately does not have, another comparing a
tree against itself.

Two more were found while writing this release, and they are here for the same
reason the others are. A sentence in the position paper carried a correct count
with a **command beside it that computed something else** -- lines rather than
distinct ids -- so the figure could not be reproduced the way the paper told a
reader to reproduce it. And the export's reference checker, which is supposed
to catch a published document pointing at a file the export does not carry,
harvests paths only from backticked spans and markdown links: sixteen new table
rows naming internal evidence trees in bare form passed it **green**, and the
one exemption that existed before them had only ever been caught because the
same path happened to appear backticked elsewhere in its row. The first is
fixed; the second is fixed for the references at hand and its detection gap is
recorded as a tracked defect rather than patched in the hour before a tag.

The full board of what is verified, what is advisory and what is
`UNSUPPORTED_ENVIRONMENT`, with the exact command that re-derives each row, is
`docs/READINESS.md`.

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
