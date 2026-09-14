# VeriHarness — an evidence-bound Harness-of-Harness for verifiable long-horizon agent development

## 0. Naming, scope, and where this paper's evidence comes from

The project's public name is **VeriHarness**, repository slug `veriharness`.
Its positioning sentence, as decided by the captain on 2026-09-08 and not
revisited here: *"VeriHarness -- an evidence-bound Harness-of-Harness for
verifiable long-horizon agent development."* `hoh` is not renamed: it stays
the architectural term and the CLI's own name, because a rename adds release
risk for no evidential gain. This paper says `VeriHarness` when it means the
project and `hoh` when it means the command or the architecture, and states
once, here, that they name the same thing.

The naming decision carries a binding rule for every sentence below: *"VeriHarness
claims verifiability within its documented evidence and gate boundaries --
not perfection, not complete correctness, not autonomous freedom from
error."* A sentence such as "VeriHarness verifies that a candidate is
correct" violates this rule by asserting a property of the world; a sentence
such as "VeriHarness accepts a candidate only when a receipt it did not write
supports every criterion, within the limits `docs/LIMITATIONS.md` names"
satisfies it, because it carries the boundary instead of erasing it. The two
directions of the rule pull against each other on purpose: this paper tries
to be confident about what is measured and silent about what is not, and a
paper that hedges every sentence into meaninglessness fails the rule as badly
as one that overclaims.

**A note on sourcing, because it governs everything that follows.** Every
number and every empirical statement in this paper traces to `CLAIMS.json`,
by claim id, wherever the ledger carries one. `CLAIMS.json`'s own coverage is
declared, not universal: it catalogues every number-bearing sentence in
`README.md` and `docs/**`, plus the two mandatory `a02` invalidation entries
and a fixed set of unflattering measured findings -- it does **not** extend
to the internal *Abschlussbericht*, `DOGFOOD_LEDGER.md`, or `PROVENANCE.md`.
Several of this project's strongest findings live in those documents, not in
the ledger. Where that is true below, this paper says so plainly, in the same
breath as the finding, rather than dressing an unledgered number up as a
verified one. That sentence -- "the ledger does not carry this" -- is the
honest move the specification for this draft asks for, and it appears
repeatedly in what follows, not as a hedge but as a fact about what
`CLAIMS.json` was built to cover.

Two terms recur throughout and are defined once, here, because the same word
names two different quantities in this project and they differ by up to
five:

- **receipt-derived discriminating**: among the checks in a run that have
  *both* a `-basis` receipt (the base state run through the same check) and a
  candidate receipt, how many of those pairs had differing exit codes. This
  is what `CLAIMS.json`'s `discriminated:` evidence form records, and every
  claim below that cites one states this definition in its own claim text.
- **acceptance-governing discriminates**: the controller's own `discriminates`
  flag, set only when the base run misses the expected exit code *and* the
  candidate meets it -- a stricter, directional condition decided by the
  controller from a measurement, never from a planner's prose.

## 1. Per-run evidence binding is necessary and not sufficient

This is the project's central claim, and it rests on two instances, which is
what lifts it from an anecdote to a property. Keep two levels apart by name:
**local candidate/run correctness** (a candidate is bound to its tree, every
`PASS` carries a receipt from this runner for this iteration, the
preservation suite re-runs, and at least one criterion is proven to have been
red on the predecessor), and **post-merge / union correctness** (the same
statements hold for the *combined* state after several runs are
consolidated). Nothing in the acceptance loop checks the second kind — a
statement about the loop that remains true, and that §1.1 shows is no longer
the whole picture at the level of the system.

**Instance one, found by a human.** Runs `d1` and `d2` each passed their own
independently verified acceptance criteria. Per-run, receipt-bound evidence
was, at the time, treated as sufficient to keep this project's public
statements about itself consistent -- and that treatment turned out to be
unsupported: `LICENSE` said no license had been chosen, `pyproject.toml` said
Apache-2.0, and `CITATION.cff` said `UNVERIFIED`, three simultaneously
contradictory statements about the project's own licence, left standing after
two individually honest runs merged (C-017). `CLAIMS.json` records that
sufficiency claim as unsupported rather than confirmed, because no evidence
form in its schema can positively verify a project-wide consistency property
from per-run receipts alone -- a person noticed the contradiction, not a
check.

**Instance two, found by a machine.** One run anchored its coverage
bookkeeping to line numbers in `docs/LIMITATIONS.md`; a later run edited that
document by several lines. `tools/check_claims.py`'s own checker went from
exit 0 on the candidate alone to reporting failures naming five specific
statements on the combined state, minutes after the merge that created the
break. Nobody read the diff and noticed; a tool did. Together, the campaign's
own account of this class of failure is: *"-- twice. Once the merge produced
3 contradictory statements about this project's own licence. Once it left 5
coverage anchors pointing at content that had moved, across 2 merges total.
**Neither run was wrong** on its own"* terms (C-052..C-054) -- a historical
count over merges that have since been resolved, which no evidence form in
this ledger's schema can independently re-verify, so `CLAIMS.json` marks it
unsupported rather than confirmed, even though the underlying narrative is
not disputed.

Both instances share the same structure, and it is worth naming why that
structure recurs: each run was checked against its own base, its own scope,
and its own evidence, and each check passed honestly. A reader who treats
"every run in this history was individually verified" as "the current merged
state is verified" is making a claim this project's own evidence does not
support (C-017). A failure mode that only a careful human catches is a
different risk from one a check catches automatically after the fact, and
this project has now measured one instance of each.

### 1.1 What the project did about it, and what that changed

The two instances above are the historical discovery, and they are left
standing in that form because the sequence matters more than the conclusion.
What followed is the part a reader evaluating VeriHarness today needs, because
the sentence "nothing in the acceptance loop checks the second kind" is still
true of the *acceptance loop* and no longer describes the *system*.

The sequence was:

1. **Failure observation.** Two merges of individually correct runs left the
   combined state inconsistent (C-017, C-052..C-054).
2. **Hypothesis.** Per-run evidence does not compose. Acceptance is a property
   of a candidate against its base; consolidation is a different state, and no
   check in the loop ranges over it.
3. **Union gate.** Five invariants (`U1`-`U5`) evaluated over the merged tree
   rather than any single run's base -- licence agreement, no shipped file
   referencing an unshipped one, built artifact matching repository claims,
   every evidence reference resolving, and suite plus linter green on the
   combined state. Two of the five are the two failures above, added because
   they happened.
4. **Semantic dependency layer.** Before two runs are allowed to proceed in
   parallel, their declared scopes are measured against each other rather than
   assumed independent. Serialisation is imposed where a dependency is
   measured, not as a blanket policy.
5. **Post-DAG closure.** After the last planned run merges, the global gates
   run over the whole state. A failure there does not halt the release and
   does not get patched by hand: it produces a new repair run, which goes
   through the ordinary loop and must itself be accepted.
6. **A fixpoint rule instead of a completion rule.** Release is gated on

   ```
   DAG_TERMINAL  !=  RC_CLOSED
   ```

   A terminal DAG means every planned run merged. `RC_CLOSED` additionally
   requires every global gate green *and* that the pass which checked them
   created no new repair node. Closure is a fixpoint, not the end of a plan.

**The prospective instance.** That distinction did work during this release
rather than describing work. The DAG went terminal after run `D8`. The global
gates then found two real defects in the merged state -- a composition failure
and an export failure -- neither of which any individual run had been wrong
about. They became repair runs `d8b` and `d8c`; only after both were accepted
and closure re-ran clean did the candidate qualify. Unlike instance one, no
human noticed. Unlike instance two, the finding arrived *before* publication
rather than minutes after a merge. That is the third position in the sequence
this paper has been tracking: a human found the first, a tool found the second
after the fact, and a gate found the third before it could ship.

The general statement, which is what this section is actually for:

> **Per-run evidence binding is necessary but insufficient for composition.
> VeriHarness therefore adds global composition closure above the per-run
> acceptance loop.**

**What this does not become.** `U1`-`U5` and the semantic invariants are an
explicit, extensible set of *known* properties, each added because a specific
failure taught it. They do not prove that an unknown cross-run interaction
would be detected. The acceptance loop is still not closed under composition,
and no quantity of global gating closes it; what the global layer changes is
whether the resulting inconsistency reaches a release. `docs/LIMITATIONS.md`
§9 states that limit in the form a reader should hold this project to.

### 1.2 The operating model this evidence came from

One clarification belongs here rather than in an appendix, because every
number in this paper depends on it. The campaign that produced this evidence
did not run as a person invoking `hoh run` repeatedly. It ran as a composed
stack:

- a **human policy authority**, who set the limits and took the decisions
  reserved from delegation -- seven of them across the campaign, including
  whether to publish at all;
- an **agentic main orchestrator**: a long-running agent session that wrote
  and revised specifications, started runs, read verdicts and receipts,
  decided merges, executed the global gates, turned gate failures into repair
  runs, and repeated until closure;
- the **HoH verification kernel**, which ran each individual run's
  plan/develop/QA/evidence/accept loop;
- the **Herdr runtime**, providing the sessions and worktrees.

The orchestrator is a deployment layer, not a component of `src/hoh/`; it
drives the kernel through the same CLI any operator would use, and the kernel
cannot tell the difference. Naming it precisely matters in both directions.
Describing the campaign as hand-driven would understate what ran
automatically. Describing it as **autonomous self-repair** would be worse, and
this paper does not: the kernel did not decide which runs existed, and the
repairs to HoH's own source were made above the run boundary, not by a run.
The accurate term is **policy-delegated agentic orchestration with
evidence-bound repair loops** -- decisions delegated in advance by a human,
executed by an agent, with every individual change still required to earn
receipts through the ordinary loop.

## 2. The verification channel is the attack surface

The mechanism that establishes what is true about a candidate -- the
acceptance-check run inside its arena -- is also the mechanism most worth
attacking, because subverting it subverts every downstream statement of
correctness. This project's own adversarial review found exactly that shape
of finding: a check command could overwrite a *foreign* run's preservation
suite from inside the object nominally under verification, reproduced at
severity high (tracked internally as B-G1), while a source comment covering
that exact code path had called the class "structurally impossible." The
operator closed this gap, alongside four related guard gaps in the same
review pass, none of it feature work.

`CLAIMS.json` does not carry a claim id for the B-G1 finding itself: the
finding lives in the internal *Abschlussbericht*'s point D and in the
non-numeric half of `docs/LIMITATIONS.md`'s discussion of arena isolation,
neither of which the ledger's own coverage extends to (see §0). What the
ledger does carry, and what motivates taking B-G1 seriously rather than as an
isolated incident, is the project's own standing position on the guard that
sits beside it, a claim `CLAIMS.json` marks unsupported rather than confirmed
because no evidence form in its schema can positively prove a negative about
a hostile plan: *"the guard around acceptance-check commands is a tripwire
against accidents, not a security boundary against a deliberately hostile
plan"* (C-016) -- explicitly disclaimed, not merely unproven, and this
project's own evidence forms record it as such precisely because a pattern
denylist on a string a shell reinterprets afterward is not watertight in
principle. B-G1 is a demonstration of exactly the class of failure that
disclaimer warns about, found in the one channel whose compromise would make
every other statement in this paper unreliable: the channel that decides
what counts as a receipt.

## 3. An earned, receipt-derived `discriminates` versus a stamped, acceptance-governing one

The same word, "discriminates," carries two entirely different epistemic
values across this project's own history, and the clearest demonstration of
the gap is a single run corrected by a later one.

**`a02`, invalidated, appears here only as the counter-example it is.** Its
original report claimed nine of ten criteria demonstrated a genuine increment
against the predecessor state. That claim is invalidated: for the iteration
in question, only one criterion actually ran against the base, under an
acceptance-governing `discriminates` default that had been *applied* rather
than *earned*, with zero base receipts on disk to support the other nine
(C-011). A dependent summary elsewhere in the project's own acceptance
report repeated the stale nine-of-ten figure without the correction attached
and is invalidated for the same reason, left stale when the row above it was
fixed (C-012). Both citations above are invalidated, and the correction --
not the original figure -- is what this paper treats as informative: a
`discriminates` flag that is stamped rather than measured is not evidence,
regardless of how confident the surrounding prose sounds.

**`a03` and every dogfood run after it make the controller run the base
check itself and keep the `-basis` receipt**, so the acceptance-governing
flag is earned from a real second execution, not defaulted. `a03` -- the
corrected replacement for `a02`'s invalidated evidence -- recorded 21 receipt
files across two iterations (C-047). Its first iteration: 3 of 5 checks with
both a `-basis` and a candidate receipt had receipt-derived differing exit
codes, and the candidate was accepted (C-048, C-049). Its second iteration:
2 of 3 such checks differed, the candidate was accepted again, and the run
reached two consecutive accepted iterations -- the exact pair this project's
"two consecutive increments" claim now rests on, in place of `a02`'s
invalidated version (C-050, C-051).

The same earned pattern holds across the whole dogfood campaign, not just
`a03`. Every one of the seven runs below recorded both a `-basis` and a
candidate receipt for its accepted (or, for `d2c`, its sole) iteration, so
every receipt-derived count below is a measurement, not a default:

| run | receipts recorded | receipt-derived discriminating (relevant iteration) | outcome |
|---|---|---|---|
| `d1` | 48 (C-018) | 5 of 11, iteration 2 (C-021) | accepted, iteration 2 (C-022) |
| `d2` | 36 (C-023) | 6 of 9, iteration 2 (C-026) | accepted, iteration 2 (C-027) |
| `d2b` | 52 (C-028) | 7 of 13, iteration 2 (C-031) | accepted, iteration 2 (C-032) |
| `d2c` | 18 (C-033) | 3 of 9, iteration 1 (C-034) | not accepted -- the one dogfood run of the seven that was not (C-036) |
| `d3` | 37 (C-037) | 8 of 11, iteration 1 (C-038) | accepted on the first attempt (C-039) |
| `d4` | 32 (C-042) | 9 of 10, iteration 1 (C-043) | accepted on the first attempt (C-044) |
| `a03` | 21 (C-047) | 3 of 5, iteration 1 (C-048) | accepted, both iterations (C-049, C-051) |

`d1`'s own first iteration is the clean illustration of why the two
quantities named in §0 must never be quoted interchangeably: its
receipt-derived discriminating count was 5 of 13 (C-019), while its
acceptance-governing `discriminates` flag was 0 (C-059) -- a historical count
the ledger itself marks unsupported, since this checkout has no populated
`runs/` tree to re-derive it from -- because that iteration was an outage
with no QA verdict at all: QA never returned a verdict because the QA role
stalled on a blocked approval dialog, so no single check is at fault for the
rejection (C-020). "5 of 13 criteria discriminated" is a true sentence about
that iteration and a misleading one if read as "5 of 13 criteria governed
acceptance," because none of them did.

## 4. A document that refuses to invent

Run `d2`'s specification instructed the loop not to invent a license fact:
every license statement must carry either a locally verifiable source or the
literal marker `UNVERIFIED`, with a sentence naming what would resolve it.
The plan the specification argued against had asserted "Herdr: Apache-2.0"
and "HarnessOfHarness: MIT" as settled facts, neither of which was locally
verifiable on the machine the loop ran on at the time -- `herdr` shipped as a
bare binary with no bundled license file. The loop's developer role wrote a
`LICENSE` file that states the legally correct default position instead of
guessing: with no grant recorded, the software is "all rights reserved,"
and the file names the two plan assertions it could not verify locally and
what would resolve each one, rather than repeating them as fact. `CLAIMS.json`
does not carry a claim id for this specific document's wording -- it is
recorded in `DOGFOOD_LEDGER.md`, outside the ledger's declared coverage (see
§0) -- but the surrounding license-consistency problem it responds to is the
same one instance one of §1 measures, a sufficiency claim the ledger itself
marks unsupported rather than confirmed (C-017).

This is the clearest evidence this project has that evidence pressure changes
what a machine writes where it does not know, rather than merely changes
whether it later gets corrected: the loop had every incentive to fill in a
plausible-sounding answer and did not.

## 5. The loop corrected a wrong human instruction, traceably

A specification for run `d1` embedded a sha256 digest of a guard pattern file
as a pinned fact. The digest went stale: the operator changed the pinned file
for an unrelated guard fix after the specification was written, and the
specification did not follow. That staleness was first the operator's own
mistake, found and named by the operator mid-run. What happened next is the
stronger finding, and it belongs to the loop, not the operator: in a later
iteration, without anyone pointing it out, the loop's planner role
independently found the same stale pin, read the project's own git history,
and named the exact commit that had superseded it -- a hardening fix for a
real command-substitution bypass, landed one day after the specification's
own measurement. The planner then argued a consequence the operator had not
written down: pinning the stale digest would require shipping the weaker,
since-fixed guard as the criterion's own reference state, a security
regression, and rewrote the criterion as a content relation against the live
source instead of a frozen number -- independently arriving at the same
correction the operator had separately concluded was the better form. As with
§4, `CLAIMS.json`'s coverage does not extend to `DOGFOOD_LEDGER.md`, where
this account is recorded, so this paper reports it as sourced narrative
rather than as a ledger-verified figure.

## 6. The arena's isolation and the evidence base are mutually invisible, by design

Three limits in this project trace to one cause: a candidate arena is
materialized with `git archive <tree>` and therefore holds exactly one tree
and nothing else -- no history, no `.git` of its own, and no untracked file.

First, a criterion can assert any property *of* the candidate and none of the
*difference* to its predecessor, because there is no earlier tree inside the
arena to compare against. This has already cost the project real iterations,
not just a theoretical gap: measured, 2 runs in this project's own history
lost an iteration each to acceptance criteria that tried to express a
differential property inside the loop anyway, and the second loss came
*after* the cause of the first had already been diagnosed -- naming the
mechanism did not, by itself, stop the mistake from recurring, a historical
count over past runs that `CLAIMS.json` itself marks unsupported rather than
confirmed, since no evidence form in its schema re-derives it from this
checkout alone (C-055). That class of assertion has to live outside the
acceptance-check loop entirely, expressed by whoever compares runs after the
fact -- a limit of the arena's design, stated plainly rather than apologized
for.

Second, and more consequential: a criterion cannot verify a statement about
the evidence itself, because `runs/` is gitignored and therefore absent from
any candidate's own snapshot -- `git archive HEAD` run against this project
yields 0 entries under `runs/`, a claim `CLAIMS.json` records as unsupported
because its schema has no evidence form that runs a git command and checks
its output (C-056). Six of fifteen acceptance criteria in one run failed for
exactly this reason, each trying to assert something about a run's own
evidence trail that its own arena simply did not contain to read -- again a
historical per-run count this checkout's evidence forms cannot positively
re-verify, so the ledger marks it unsupported rather than confirmed (C-057).
This is deliberate on one side and open on the other. Direct
live access by a candidate under test to `runs/` must stay forbidden, full
stop, because the verification channel that judges a candidate cannot also be
reachable *from* the candidate -- §2's B-G1 is exactly the failure that
relaxing this boundary would reopen. What remains open is that a claims
ledger citing receipts genuinely cannot be verified by a criterion running
inside the loop it is meant to check; that verification has to happen
outside the loop, as this paper's own construction (developer writes,
independent QA executes its own criteria, neither on the ledger's own say-so)
is itself an instance of.

Third, and the concrete, measured consequence of nesting the arena inside
this project's own working tree rather than a separate filesystem: until
2026-09-08, the runner also redirected a check's `TMPDIR` into that same
arena, and that redirection has already produced one real defect in the
candidate binding -- `is_git_repo` in `src/hoh/workspace.py` once answered a
question about the *outer* repository instead of the arena subdirectory,
precisely because pytest's own temp-directory redirection landed inside the
nested arena. The measurement behind the fix: 28 of 52 candidate arenas in
this project's own history carried a leftover `pytest-of-<user>/` directory
from an earlier check before the redirection was corrected -- a count over
past, mostly no-longer-existing arenas that `CLAIMS.json` itself records as
unsupported rather than confirmed, since this checkout cannot positively
re-derive a historical population it does not still hold (C-061). This is
the same defect the project's own bug ledger tracks as K11, fixed by the
operator with two accompanying tests, under the project's narrow exception
for repairing a reproduced defect in `src/hoh/`.

## 7. An iteration that changes nothing cannot be accepted

Run `d2c2`'s second iteration passed all ten of ten criteria, and was
rejected anyway: the controller's own reasoning, recorded verbatim in the
project's dogfood accounting, is that no criterion demonstrated the
increment, because every one of them was already green on the predecessor
state. `CLAIMS.json` does not carry a claim id for `d2c2`: it is recorded in
the internal *Abschlussbericht*'s run table, not in `README.md` or `docs/**`,
so this paper reports its numbers -- ten of ten criteria passing, zero of
them discriminating in the acceptance-governing sense -- as sourced to that
document rather than as ledger-verified (see §0).

The same mechanism is independently visible inside a ledger-covered run,
which is the closest this paper can come to corroborating it by claim id:
`d1`'s second iteration carried a criterion the plan had listed as new work,
`K11`, which the controller itself flagged as not discriminating, in its own
words, because it "was already green on the last accepted state and
therefore does not demonstrate the increment" -- the plan's own framing lost
to the measurement. `CLAIMS.json` does not carry a claim id for this `K11`
detail: it is recorded in the internal *Abschlussbericht*, not in `README.md`
or `docs/**`, so this paper reports it as sourced to that document rather
than as ledger-verified (see §0). `d2c2`'s rejection is the same rule
applied to an entire iteration rather than a single criterion inside one: an
iteration that changes nothing measurable cannot be accepted, regardless of
how many of its criteria pass. This is the direct countermeasure to the
failure mode behind §3's invalidated `a02` evidence -- a stamped
`discriminates` flag with no measurement behind it -- observed here in
ordinary operation rather than argued for in the abstract.

## 8. The loop repeatedly produced better criteria than the specification it was given -- and that is not evidence of autonomy

Three runs in a row improved on a human-written specification, each in a
different way, and `CLAIMS.json` does not carry claim ids for any of the
three (they are recorded in `DOGFOOD_LEDGER.md`, outside the ledger's
declared coverage): run `d4c` replaced a defective differential criterion --
one that had been anchoring coverage to line numbers, the exact mechanism
behind instance two of §1 -- with a comparison against a per-claim content
digest computed at plan time; this content-anchoring field, `anchor_digest`,
is now a required field on every entry in `CLAIMS.json` itself, independently
verifiable by reading the ledger's own schema. Run `d4b` turned a criterion
the operator had specified imprecisely ("these checks cannot run without
`runs/`") into an asserted precondition instead: in a check directory that
genuinely has no `runs/` subdirectory, every evidence-resolution failure must
carry a distinct `ENVIRONMENT GAP` marker, and no unmarked failure is
permitted -- a statement that is machine-checkable where the operator's
original framing was not, and it is now `tools/check_claims.py`'s own
`ENVIRONMENT_GAP_MARKER` constant. Run `d3b` added two precisions its
specification had not asked for: `PROVENANCE.md` names that the Homebrew
formula one version ahead of the installed Herdr binary independently states
Apache-2.0, while the installed 0.8.0 binary itself ships with no local
license file to read; and it frames this project's own choice of MIT over
Apache-2.0 as "a stated trade-off, not a claim that MIT is 'the standard'
license," rather than presenting the choice as self-evidently correct.

**Report this with its limit attached, in the same breath, because the limit
is the more important half of the finding.** All three runs worked on
specifications a human wrote; two of them repaired that human's own mistakes
rather than inventing new scope; none of the three touched `src/hoh/` or
`tests/`; and every one of them was judged by criteria a human could read and
that an independent QA executed itself. The honest claim available from this
evidence is about the effect of a well-posed task with checkable criteria on
what a capable role produces, not about a system that repairs itself.
Nothing in this project's own repair history supports a claim of autonomous
self-repair, and this paper does not derive one: the paper explicitly does
not claim self-repair or self-healing, and nothing in this project's history
shows software that "fixed itself" or "autonomously corrected" its own
defects without a role dispatch or an operator making the change -- every
fix named anywhere in this paper traces to a named actor, the loop's planner,
developer, or QA role, or the operator directly, never to VeriHarness acting
on itself.

## 9. The forbidden inference, and the boundary that keeps it honest

The captain's instruction for this draft, given 2026-09-08, is factual and
checkable, and this paper keeps the two halves visibly separate rather than
letting them blur into one undifferentiated "the system improved":

**The loop's own work** -- actions taken by a planner, developer, or QA role
dispatch, inside its declared scope, judged by criteria an independent QA
executed itself: the six of the seven accepted dogfood runs and their
deliverables, per the §3 table above (`d2c` being the one not accepted); the
`LICENSE` file of §4 that refused to invent a fact;
the planner of §5 that read the project's own git history and rejected a
stale specification as a security regression; the three specification
improvements of §8; and a QA role that, in a separate run, traced a
criterion's failure to a structurally unsatisfiable regex assertion in the
check script itself (O40) rather than guessing, and marked its verdict
`INCONCLUSIVE` with the sentence "this is a bug in the K10 check script, not
a candidate defect" instead of returning a plain `FAIL`. Every one of these
is a loop action and is cited as such.

**Operator fixes** -- work a human found, fixed, and tested, never a loop
role dispatch: eleven core defects (K1 through K11, one of them K11 of §6,
fixed with two tests), five guard gaps including B-G1 of §2, six named
observations resolved by diagnosis rather than by code change, the
translation of this project's source text that made the loop's own
documentation-only scope possible in the first place, and the git merges
that consolidated accepted candidates into `master`. `HoH` -- `VeriHarness`
under its architectural name -- was forbidden `src/hoh/` and `tests/`
throughout every dogfood run recorded in this project; every line of
production code the loop is credited with above is documentation, packaging,
or governance prose, never `src/` or `tests/`. the internal *Abschlussbericht*'s
point D carries the complete, itemized list of these interventions,
including two that were mistakes of the operator's own -- a specification
edited mid-verification and then reverted, and an early accounting tool that
misreported its own run count before being corrected -- and this paper cites
that document as the source of the boundary rather than blurring it.

A sentence like "the system repaired itself" is false here by construction:
every defect named in this paper was fixed by a specific role or a specific
human, never by VeriHarness acting without one. A sentence like "evidence
pressure changed what the loop wrote where it did not know" is true, per §4,
and is the stronger and more defensible claim of the two.

## 10. Prior work and attribution

**Harness-of-Harness** is prior work this project adapts, not a dependency it
ships. Its repository license is MIT, with the copyright line "Copyright (c)
2026 Hyoung Yan," read directly from the repository's own `LICENSE` file.
VeriHarness's own workflow -- repeated planning, development, and independent
QA over persisted, evidence-bound state -- is adapted from the published
Harness-of-Harness paper and repository; this is an **independent
adaptation**, not an official Harness-of-Harness release, not an installable
HoH package, and not a full replication of the paper. `HoH-lite`, a
lightweight variant announced in that repository's own README, remains only
announced there and was not shipped as of the check this project performed
(the internal *Quellencheck*, source Q2); this project therefore claims no
dependency on an installable HoH package, because at the time of that check
there was none to depend on.

**Herdr** is the runtime this project drives, invoked only as a subprocess
and never imported or vendored as a library. Its upstream project license is
Apache-2.0. The distinction that matters for anyone installing this project
is version-specific: the `herdr` binary this project's own worktree runs
against is 0.8.0, and that installed binary ships with no bundled license
file of its own to read -- the Apache-2.0 statement above covers the
upstream Herdr *project*, one version ahead at 0.9.0 in its own Homebrew
formula, not a license file shipped alongside the 0.8.0 binary itself.
Because this project only shells out to `herdr` as a subprocess and never
links or vendors its code, Herdr's license does not attach to this project's
own MIT-licensed source; that is an observation about how the code calls
out, not a legal opinion.

**pydantic and pydantic-core**, the only third-party packages this project's
own `src/hoh/**` imports at runtime, are both MIT-licensed, verified locally
against the installed distributions' own license metadata and bundled
`LICENSE` files rather than assumed from the package name.

No affiliation with, or endorsement by, the authors or maintainers of any of
the above is implied by this project's use of their work.

## 11. Limitations, in full, here rather than in an appendix

At least these hold today, several of them measured rather than merely
disclaimed:

This list corresponds to `docs/LIMITATIONS.md` and is complete against it: as
of 2026-09-10, that file held sixteen numbered limits, and this section
carries a statement for every one of them -- fourteen of the sixteen as the
items below, and the remaining two (limits 9 and 12) in the body of this
paper itself, at length, where they are already treated (limit 9 in §1 and
§1.1, on per-run versus merged state and the closure layer above it, limit 12
in §0's and §3's
`discriminates`-terminology definitions). The list's positions below do not
track `docs/LIMITATIONS.md`'s own limit numbers position-for-position: limit
6's content below spans list positions 6 and 7, offsetting every position
after it, so list position 9, for instance, is not that file's limit 9. A
reader who counts a different number of headings in `docs/LIMITATIONS.md`
than are named here knows this list has fallen behind, not that a gap was
deliberate.

1. **Multi-day orchestrated operation is demonstrated; long-duration
   operation without intervention is not.** The campaign behind this paper ran
   **65 runs, 1,988 receipts (880 of them control runs) and 100 iterations
   that produced receipts, across five calendar days and 3.5 days of elapsed
   wall-clock**, producing this paper, the claims ledger and the release
   machinery among its artifacts. It was driven by an **orchestrating agent
   session** rather than by a person writing specifications: a human decided
   **seven** governance questions (`DEC-R1` … `DEC-R6` and the decision to
   publish), and those were escalated by rule rather than by necessity — the
   same orchestration with those policies delegated in advance would have
   resolved them itself, which is a property of the design and not a measured
   result. What is therefore open is duration, not the automation:
   `Budgets.max_wallclock_seconds` and `max_iterations` are enforced, but an
   enforced ceiling is not a demonstration of multi-month reliability, and
   nothing here shows how this campaign's own failure modes — budget
   exhaustion, an expired role credential, a lost tool directory after a power
   cut, a composition failure between two correct runs — behave when nobody is
   reachable for a week, or how an orchestrator survives its own restart.
2. **No matched-budget comparison, and no baseline at all.** There is no
   matched-budget comparison against a plain agent working the same
   specification without VeriHarness's plan/develop/verify loop around it,
   and no baseline run of any kind exists in this repository's own evidence
   -- an absence claim `CLAIMS.json` records as unsupported rather than
   confirmed, because no evidence form in its schema can positively verify
   the absence of something (C-013). It is therefore not demonstrated that
   the same role-run budget spent without this loop's overhead would produce
   a worse, or better, outcome.
3. **Provider cost is not measured.** VeriHarness counts role dispatches and
   can report whether a cost approval is on file, but it does not measure
   actual token or dollar cost anywhere in this project's own accounting.
4. **The check-command guard is a tripwire, not a security boundary**, per
   §2's C-016 -- a claim about the absence of a hostile-plan defense that
   `CLAIMS.json` itself marks unsupported rather than confirmed, since no
   evidence form in its schema can positively prove such a negative. A
   pattern denylist on a string a shell reinterprets afterward is not
   watertight in principle, and the guard's own mitigations narrow the blast
   radius without substituting for a real OS sandbox.
5. **The checked string and the executed string can diverge.** A check
   command reasons about the placeholder `{ARENA}` after the guard
   substitutes a stand-in string for it, while the runner later substitutes
   the real, absolute arena path when the command actually executes; the
   checked string the guard evaluated is therefore never quite the string
   the shell runs. This project has not observed that gap produce an
   escape, and does not claim it closed.
6. **The arena is nested inside this project's own working tree**, and until
   2026-09-08 the runner additionally redirected each check's `TMPDIR` into
   that same arena rather than a directory outside it (§6). A second,
   independent instance of the same nesting is reproducible on any
   materialized arena without needing `TMPDIR` at all: an arena is a plain
   `git archive` extract with no `.git` of its own, so `git rev-parse
   --show-toplevel` run from inside it does not fail -- it climbs past the
   arena boundary and answers with the ancestor repository's root path
   instead of the arena's own (§6).
7. **That TMPDIR nesting already produced a real defect in the candidate
   binding** -- 28 of 52 candidate arenas in this project's own history
   carried a leftover scratch directory from an earlier check before the
   operator corrected the redirection, a historical arena count
   `CLAIMS.json` marks unsupported rather than confirmed since it cannot be
   re-derived from this checkout alone (C-061, §6).
8. **A plan can shrink its own preservation suite, and nothing currently
   stops the shrinkage from growing between iterations.** An acceptance
   check is free to scope `pytest` to a subset of files, which is a
   legitimate way to exclude tests a plan has deliberately put out of scope
   for one iteration; nothing in the controller today prevents that
   exclusion list from narrowing further and further across later
   iterations while still reporting the preservation suite as green.
9. **No real observation of a blocked dialog keeping its pane open in
   production.** The claim is not demonstrated end-to-end (C-014): only a
   mocked unit test exercises the path where a `WaitingForApproval` failure
   leaves a role's tab open instead of closing it, and no run in this
   project's own evidence drives a real approval dialog and watches its pane
   stay open.
10. **This project's own dogfood evidence covers documentation, packaging and governance only.**
    Until 2026-09-09, this paragraph read: "`src/hoh/` and `tests/` were
    forbidden to every dogfood run recorded in this project's own history --
    an absence-shaped scope claim `CLAIMS.json` marks unsupported rather than
    confirmed, since its evidence forms cannot positively prove that no run
    ever touched those directories (C-015). VeriHarness's evidence about its
    own reliability therefore speaks to documentation- and governance-shaped
    work, not to using the loop to develop or modify a nontrivial production
    code path." That claim's `src/` half stands: no run in this project's own
    commit history has ever added or edited a line inside `src/`. Its `tests/`
    half does not: the loop wrote exactly three files under `tests/`, and all
    three are the test files of the release tools the loop itself created --
    `test_claims_anchors.py` (9 of 9 commits), `test_export_manifest.py` (4 of
    4), and `test_union_gate.py` (3 of 3), every one of those commits coming
    from a run, sourced from `CLAIMS.json`'s own corrected C-015 note. Several
    of this project's own run specifications list `tests/test_*.py` in their
    own Scope -- what you may change section, so those runs were not
    forbidden to touch `tests/` at all; they were instructed to. `CLAIMS.json`'s
    own C-015 entry was moved from status `unsupported` to status
    `invalidated` by node `d3g` on 2026-09-09, on exactly this evidence. What
    survives, narrower than the retired sentence above: this project's own
    dogfood evidence still speaks only to documentation-, packaging-, and
    governance-shaped work, and to writing the test suites of the release
    tools the loop itself created -- it says nothing about pointing the loop
    at `src/hoh/` itself, or at HoH's own pre-existing test suite, both of
    which remain untested by this project's own history.
11. **A criterion cannot express a property of the difference to the
    predecessor state.** An arena holds exactly one tree, with no earlier
    tree inside it to compare against; this cost the project two iterations
    in its own history, the second after the cause of the first had already
    been diagnosed -- a historical iteration count `CLAIMS.json` marks
    unsupported rather than confirmed, not re-derivable from this checkout
    alone (C-055, §6).
12. **A criterion cannot verify a statement about the evidence.** `runs/` is
    gitignored and absent from every candidate arena; six of fifteen
    acceptance criteria in one run failed for exactly this reason -- again a
    historical per-run count the ledger's own evidence forms cannot
    positively re-verify, so `CLAIMS.json` marks both figures unsupported
    rather than confirmed (C-056, C-057, §6). This is deliberate on one side
    (a candidate must never reach the evidence that judges it, per §2's
    B-G1) and open on the other (a claims ledger citing receipts cannot
    today be verified by a criterion running inside the same loop it
    checks).
13. **An iteration budget is charged when an iteration begins, and a state
    written by an older version keeps what that version charged.** The
    iteration counter budgets check against increments when an iteration
    starts, not when it resolves, and that increment is persisted into run
    state immediately, before a verdict. Cross-version resume of that counter
    is not supported: a newer version has no way to tell which of an inherited
    counter's charges reflect iterations that actually ran to a verdict versus
    ones that were charged and then interrupted, before resolving, by
    something version-specific. Two runs in this project's own history carry
    an inflated counter for exactly this reason and stay blocked on resume.
14. **A preservation promise over a shared artifact turns that artifact into a global lock.**
    Every run's preservation criteria include the whole test
    suite, and that suite is one shared artifact, checked the same way, by
    every run that starts from this project's current history -- which makes
    its redness global: while one test in it is red, any newly started run
    inherits the failure the moment its arena is materialized, no matter what
    that run's own subject is, and cannot be accepted while the failure
    stands, regardless of whether that run's own scope ever touched the file
    that broke it. This project has paid for that twice. Once, run `dug2` was
    rejected repeatedly across three iterations, ten of eleven of its own
    criteria green each time, at a failure two other, already-merged runs had
    jointly produced entirely outside `dug2`'s own declared scope -- which its
    scope forbade it to touch -- and the run was abandoned rather than fixed,
    because fixing it was never its job. Once, after a later merge had landed
    with every dependency satisfied, a run that should have been immediately
    startable was not, because materializing its arena would have inherited
    that same still-red shared test, and usable parallelism across the
    project collapsed to exactly the one run already working to repair it.
    Both halves have to be read together: the mechanism is correct -- a
    preservation criterion that tolerated "except somebody else's failure"
    would be exactly the disarming this whole design exists to prevent -- and
    the cost above is real, the price of keeping that promise honest rather
    than a defect to route around.
15. **A specification is immutable for the life of a run, and there is no
    supported path to amend one.** The specification-digest check sits at the
    top of every iteration in `src/hoh/controller.py`, before that iteration's
    planning begins, and blocks the run outright the moment the on-disk
    specification text no longer matches the digest recorded when the run
    itself started. `unblock`, in `src/hoh/stages.py`, is the only function
    that lifts such a block, and it clears the block reason but never touches
    the recorded digest, so the very next iteration reads the same edited
    specification, recomputes the same mismatched digest, and blocks again;
    `resume` covers a paused run only, not one blocked this way. This is a
    deliberate trade, and the reasoning is the code's own: the specification
    must not change silently underneath a running run, "otherwise two
    iterations plan and verify against different truths." The limit sits one
    level up -- there is no supported amendment path once a run has begun,
    only detection and a re-block, so sharpening a specification after the
    first dispatch costs a run rather than a quick correction inside one.
    This run's own history is the cost made concrete: run `D5`'s criterion 6
    required, verbatim, "all twelve limitations," true when it was written and
    false by the time `D5` actually ran, because run `d3e` had by then already
    merged limit 13 and limit 14 into `docs/LIMITATIONS.md`; the running
    specification could not be corrected in place -- the same immutability
    this entry describes applied to it too -- so the fix did not happen
    inside that run, it became this separate one.
16. **A usage-quota exhaustion is indistinguishable from a role that broke
    its contract.** On 2026-09-08, two independent runs, `d5` and `d4h`,
    failed in the same shape within one window: a role session left no answer
    file behind, blocking each run in turn -- an operator's own cross-run
    reading of session logs, and one this project's evidence forms cannot
    independently re-verify, so `CLAIMS.json` records the joint-timing account
    as unsupported rather than confirmed (C-068..C-072). The command built
    for exactly this cause did not recognise it: `hoh resume-quota`, run over
    this project's full history, reported `{"checked": 37, "results": []}` --
    37 runs checked, 0 identified as quota-blocked -- even though two of the
    runs it checked, `d5` and `d4h`, were (C-073, C-074). "Left no answer file
    behind" names two different causes and only one has a resume path: a
    role's own broken output contract, where the session ran, produced no
    verdict, and there is nothing to resume from except re-dispatching the
    role; or a spent usage quota, where the session was cut off before the
    role process could write anything at all, including the marker
    `resume-quota` looks for. HoH never detects a quota exhaustion by itself
    and never automatically resumes a run blocked for that reason; the
    recovery here is an operator action, not an automated one -- `hoh
    unblock <run> --reason "..."` lifts the block by hand, once the cause is
    confirmed, and only then does the role run again.

## 12. What this paper does not claim

This paper does not claim VeriHarness is production-ready, does not claim it
solves evidence composition across merged runs (§1's structural gap stays
open even where its one concrete instance is closed), and does not claim
autonomous self-repair (§8). What it does claim, within the boundary the
naming rule of §0 sets: that per-run evidence binding is a real, demonstrated
property with two independent instances of the composition gap it does not
close (§1); that the verification channel is worth defending as an attack
surface because it has already been attacked successfully once (§2); that an
earned acceptance verdict and a stamped one are distinguishable in this
project's own receipts, and the project has both kinds on record (§3); and
that, within the documented evidence and gate boundaries this paper has named
throughout, VeriHarness accepts a candidate only when a receipt it did not
write supports every criterion -- nothing broader than that.
