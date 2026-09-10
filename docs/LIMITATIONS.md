# Limitations

This document exists because a project that explains its evidence model well
and hides its limits has not actually told the truth. Every item below is a
real, current limit of HoH, not a hedge. Where a limit lives in one specific
module, this says so, so a reader can go look rather than take the claim on
faith.

## 1. No proof of long unattended operation

The longest continuous evidence this project has of itself is a handful of
iterations inside one run (see the dogfood runs under `runs/`, and the
recorded example in `examples/minimal/recorded-rejection/`, which comes from
one such run). There is no multi-month unattended-operation record: nobody
has pointed HoH at a long-lived project, walked away, and come back weeks
later to a run that kept planning, developing and verifying correctly the
whole time. `Budgets.max_wallclock_seconds` and `max_iterations` exist and
are enforced (`RunState.budget_exhausted` in `src/hoh/contracts.py`), but an
enforced ceiling is not the same claim as demonstrated multi-month reliability
under it. Anyone evaluating HoH for long-horizon use should treat that gap as
open, not as covered by the budget mechanism.

## 2. No baseline, no matched-budget comparison

There is no matched-budget comparison against a plain agent working the same
specification without HoH's plan/develop/verify loop around it, and no
baseline run of any kind exists in this repository's evidence. It is
therefore not demonstrated that the same number of role-run dollars spent
without HoH's overhead would produce a worse (or better) outcome. The
project's claim is about what gets caught before acceptance, not about being
more cost-effective than an unsupervised agent -- that comparison has not
been run.

## 3. Provider cost is not measured

HoH counts role dispatches (`RunState.usage.dispatches`) and can report
whether a captain's cost approval is on file, but it does not measure actual
token or dollar cost anywhere. `RunState.cost_disclosure()` in
`src/hoh/contracts.py` says this plainly: without an approval on record it
reports usage as "actual token and money cost unknown" rather than inventing
a number or booking unmeasured usage as zero. If you need real cost figures,
get them from your provider's own billing, not from `cost_disclosure`.

## 4. The check-command guard is a tripwire, not a security boundary

`src/hoh/runner.py`'s own module docstring says this outright: the guard
around acceptance-check commands is a tripwire against accidents, not a
security boundary against a deliberately hostile plan. A pattern denylist on
a string that a shell interprets again afterwards is not watertight in
principle. What actually constrains a check command -- no login shell, a
reduced environment, `setrlimit`, an isolated arena, streamed and
size-limited output -- narrows the blast radius; none of it is a substitute
for a real OS sandbox (namespaces, seccomp, network isolation). Do not point
HoH's acceptance checks at a plan you do not already trust.

## 5. The `{ARENA}` placeholder and the runner's real path can diverge

A check command that needs the path of the object under test writes the
literal placeholder `{ARENA}`. The **guard** that decides whether a command
stays inside the arena (`assert_stays_in_arena` in `src/hoh/runner.py`)
checks the command after substituting the placeholder with the literal "ARENA",
a stand-in string, not a real filesystem path. The **runner**
that actually executes the command substitutes the real, absolute arena
path. The string the guard reasoned about is therefore never quite the
string that gets executed. In the cases this project has exercised, that gap
has not produced an escape, but the guard's own reasoning and the runner's
own behavior are not, and are not claimed to be, verifying the identical
string.

## 6. The arena is nested inside the project's own git working tree

The arena that isolates a check run sits under this project's own `runs/`
directory, inside the same git working tree the acceptance checks and the
preservation suite live in -- not on a separate filesystem or outside the
repository entirely. Until 2026-09-08, this limit said instead: "The runner
additionally redirects `TMPDIR` for the child process into that same arena
(`_env` in `src/hoh/runner.py`)." That was true through 2026-09-08 and
stays quoted here rather than silently removed, because a corrected limit
whose own former wording has vanished is indistinguishable from a limit
that was never there. As of 2026-09-08, `_scratch_dir` in
`src/hoh/runner.py` points the child process's `HOME` and `TMPDIR` at a
freshly created directory beside the arena -- a sibling, not a
subdirectory of the tree a check command enumerates. The change closes a
consequence this document had not named until now: all of one candidate's
checks share one arena, so a criterion enumerating the check directory
could see whatever an earlier criterion of the same iteration had left
behind there. `_scratch_dir`'s own docstring in `src/hoh/runner.py`
records the measurement: 28 of 52 candidate arenas in this project's
history carry a `pytest-of-<user>/` directory left by an earlier check.
This structural nesting -- object-under-test, evidence, and scratch space
sharing one working tree -- has already produced one real defect in the
candidate binding: `src/hoh/workspace.py`'s `is_git_repo` once answered a
question about the *outer* repository instead of the arena subdirectory
precisely because pytest's own temp-directory redirection landed inside
this nested arena. The nesting is a structural fact of how this project
runs itself today, not something the arena isolation removes.

A second, independent instance of the same class of failure is directly
reproducible on any materialized arena, without needing pytest at all: an
arena is a plain directory extracted by `git archive`, so it has no `.git`
of its own -- `ls -la <arena>/.git` reports "No such file or directory".
Git's own upward directory search does not treat that as an error. Run
`git rev-parse --show-toplevel` from inside such an arena and it does not
fail; it silently climbs past the arena boundary, finds the ancestor
repository's `.git` further up the directory tree, and answers with *that*
repository's root path instead of the arena's own. Checked directly against
one of this project's own run arenas: `git rev-parse --show-toplevel`,
executed from inside the arena, resolves to the ancestor checkout's path,
not the arena's. The consequence is not cosmetic -- `git status`, `git log`,
and `git show`, run the same way, do not report on the frozen candidate
files sitting in the arena at all; they silently report on the *ancestor*
repository's live working tree and history, because that is the repository
git actually bound to. Anyone who assumes "I am inside the candidate's
arena, therefore a plain `git` command tells me about the candidate" would
be reading the wrong repository without any error saying so. This is the
same structural nesting as the `is_git_repo` case above, reproduced by a
different tool (`git`'s own directory discovery, not this project's code)
arriving at the same failure shape: a boundary that is enforced by
directory placement alone is invisible to anything that walks upward
looking for a `.git`.

## 7. A criterion can exclude tests from its own preservation suite

An acceptance check is free to define "the test suite" however its command
says to -- for example by scoping `pytest` to a subset of files or adding
`--deselect`/`-k` filters, which is a legitimate way to exclude tests that a
plan has deliberately decided are out of scope for a given iteration. Nothing
in `src/hoh/contracts.py` or `src/hoh/controller.py` currently prevents that
exclusion list from growing, silently and cumulatively, between iterations:
each iteration's plan is free to narrow what "the suite" means a little
further than the last one did, and the preservation mechanism
(`Controller._checks_for`) preserves whatever command was validated, not a
judgment about whether its scope is still appropriate. A shrinking suite that
still reports green is a real risk this project has not closed.

## 8. This project's own dogfood evidence and what it actually shows about `src/` and `tests/`

Measured directly against this repository's own commit history: no run has
ever added or edited a line inside `src/`. Inside `tests/`, the picture is
narrower still -- the loop created exactly three files there, and all three
are the test files of the three release tools the loop itself wrote:
`test_claims_anchors.py`, `test_export_manifest.py`, and
`test_union_gate.py`. Every commit to those three files came from a run;
none came from an operator's hand, and no other file under `tests/` carries
a single run-authored commit.

Until 2026-09-09, this document's position was the wider claim, quoted here
in full because it stopped being true rather than because it is still the
position:

"This project's own dogfood evidence never touched `src/` or `tests/`."

The same wording went further: "Every one of those runs was deliberately
forbidden to touch `src/hoh/` or `tests/`; production code changes were
explicitly out of scope for the runs that generated this project's own
evidence about itself." Both sentences are false on their `tests/` half:
several of this project's own run specifications list `tests/test_*.py` in
their own Scope -- what you may change section, so those runs were not
forbidden to touch `tests/` at all; they were instructed to.

What survives the correction is narrower, and it is the useful half: this
project's evidence about its own reliability speaks to documentation-,
packaging-, and governance-shaped work, and to writing the test suites of
the release tools the loop itself created. It says nothing about pointing
the loop at HoH's own production code, or at HoH's own pre-existing test
suite -- both remain untested by this project's own history.

A reader can check this directly, against this repository's own commit
history:

```
git log --format='%s' -- tests/ src/ | grep '^HoH '
```

## 9. Local run correctness does not imply correctness after a merge

This is the sharpest limit in this document, because it says the project's
own verification discipline is insufficient for the thing it is most likely
to be used for: composing the output of several runs. Two runs, each verified
against its own base with candidate-bound evidence and each respecting its
own declared scope, left the combined state inconsistent after being merged
-- twice. Once the merge produced 3 contradictory statements about this
project's own licence. Once it left 5 coverage anchors pointing at content
that had moved, across 2 merges total. **Neither run was wrong** on its own
terms: each was checked against its own base, its own scope, and its own
evidence, and each check passed honestly. Nothing in the loop checks the
union of two runs' changes against each other, only each run against the
base it started from. Per-run evidence binding is therefore necessary and
not sufficient for correctness of a merged, multi-run history; a reader who
treats "every run in this history was individually verified" as "the current
merged state is verified" is making a claim this project does not support.

## 10. A criterion cannot express a property of the difference to the predecessor state

An arena is materialized with `git archive <tree>` and holds exactly one
tree: the files as they stand at that commit, nothing before it. There is no
`.git` inside the arena (see limit 6) and no predecessor tree to diff
against, so a check command has no way to ask "is this smaller than it was",
"did this only add lines", or any other question phrased as a comparison to
what came before -- that class of assertion has to be expressed outside the
acceptance-check loop entirely, by whoever is comparing runs after the fact.
Measured: 2 runs in this project's own history lost an iteration each to
acceptance criteria that tried to express a differential property inside the
loop anyway. The second of the two was written *after* the cause of the
first had already been diagnosed, which is the part worth naming plainly --
knowing the mechanism did not, by itself, stop the same mistake from being
made again in the very next run.

## 11. A criterion cannot verify a statement about the evidence

`runs/` is gitignored, so it is absent from any candidate's own snapshot --
`git archive HEAD` run against this project yields 0 entries under `runs/`.
A check command inside a run therefore cannot inspect that run's own
evidence trail, or any other run's, because the directory holding it simply
is not there to read. Measured: 6 of 15 acceptance criteria in one run
failed for this single reason, each one trying to assert something about
evidence that its own arena did not contain.

This is deliberate, and it has two halves that both have to be named for the
limit to be read correctly. First: direct live access by the candidate under
test to `runs/` must stay forbidden, full stop -- not as an oversight to be
lifted, but because a reviewer once emptied a *foreign* run's preservation
suite from inside the very object that was supposed to be under
verification, which is exactly the failure mode unrestricted access to
`runs/` would reopen. Second: a future design -- a read-only,
controller-generated, digest-bound evidence capsule handed to the check
command instead of live filesystem access -- could preserve both properties
at once, isolation and the ability to verify a statement about the evidence.
That capsule does not exist in this project today.
This document does not promise it either -- it names the shape of a
solution without claiming the solution has been built.

## 12. "Discriminating" names two different quantities

The word shows up in two places in this project and they are not the same
measurement, so any number a reader meets must say which one is meant. The
first is receipt-derived: among the checks in a run that have both a
`-basis` receipt and a candidate receipt, how many of those pairs had
differing exit codes between base and candidate. The second is
acceptance-governing: the controller's own `discriminates` flag, which is
`True` only when the base run failed to meet the expected exit code *and*
the candidate met it -- a stricter, directional condition, not a mere
difference. For one iteration in this project's history the two diverge
outright: the receipt-derived count was **5 of 13**, while the
acceptance-governing `discriminates` flag was 0, because that iteration was
an outage with no QA verdict at all -- there was no acceptance decision for
the flag to be true about, regardless of how many receipt pairs happened to
differ. Quoting "5 of 13" as if it answered the same question as the
controller's flag would misstate what that iteration's evidence actually
supports.

## 13. An iteration budget is charged when an iteration begins, and a state written by an older version keeps what that version charged

The iteration counter that budgets check against is incremented when an
iteration starts, not when it resolves, and that increment is persisted into
run state immediately. Cross-version resume of that counter is **not
supported** in v0.1.0: if a state file was written by an older version of
this project and is then resumed under a newer one, the newer version has no
way to tell which of the charges in that counter reflect iterations that
actually ran to a verdict versus iterations that were charged and then
interrupted by something version-specific -- a crash, a schema change, a
killed process -- before they could resolve. Healing this would mean
decreasing a persisted counter after the fact, and the true count of
iterations that should have been charged is not reliably recoverable once
the version boundary has been crossed, so this project does not attempt it.
Measured: 2 runs in this project's own history carry an inflated counter for
exactly this reason and stay blocked on resume -- their state files were
written by a version whose charges the current version cannot safely
reinterpret.

## 14. A preservation promise over a shared artifact turns that artifact into a global lock

Every run's preservation criteria include the whole test suite, and that
suite is one shared artifact: the same file tree, checked the same way, by
every run that starts from this project's current history. That makes its
redness global. While one test in it is red, any newly started run
inherits the failure the moment its arena is materialized, no matter what
that run's own subject is, and cannot be accepted while the failure stands
-- regardless of whether the run's own scope ever touched the file that
broke.

This project has paid for that twice. Once, a run whose acceptance
criteria were nearly all green was rejected repeatedly at a failure that
two other, already-merged runs had jointly produced -- a failure entirely
outside the rejected run's own declared scope, which its scope forbade it
to touch -- and the run was abandoned rather than fixed, because fixing it
was never its job. Once, after a later merge had landed with every
dependency satisfied, a run that should have been immediately startable was
not, because materializing its arena would have inherited that same
still-red shared test; usable parallelism across the project collapsed to
exactly the one run already working to repair it.

Both halves have to be said, because either one alone misreads what
happened. The mechanism is correct: a preservation criterion that
tolerated "except somebody else's failure" would be exactly the disarming
this whole design exists to prevent -- a shared suite that quietly excused
shared failures would stop catching the regressions it exists to catch.
And the cost is real: whoever wants to keep working in parallel while a
shared test is red must either narrow their own preservation promise down
to their own scope -- which gives up regression protection for everything
outside it -- or accept that, for as long as the shared test stays red,
usable parallelism across this project drops to one: whoever is repairing
it.

## 15. A specification is immutable for the life of a run, and there is no supported path to amend one

The specification digest check sits at the top of every iteration, not
somewhere further down the loop where a plan or a check could still slip past
it. `src/hoh/controller.py`, line 207, reads the check
`if digest(spec_text) != state.spec_digest:` before that iteration's planning
begins, and blocks the run outright the moment the text on disk stops matching
the digest recorded when the run itself started. `unblock`, in
`src/hoh/stages.py`, line 226, is the only function that lifts such a block,
and its own body clears `blocked_reason` and `stop_reason` but never touches
`state.spec_digest` -- so the very next iteration reads the same edited
specification, recomputes the same mismatched digest, and blocks again.
`resume`, by its own docstring, covers a `PAUSED` run only, not a `BLOCKED` one
stuck this way; nothing in either module clears a stale `spec_digest`.

This is a deliberate design, and the code says why at the check site itself:
the specification must not change silently underneath a running run --
"otherwise two iterations plan and verify against different truths." That is
the right trade, and this entry is not a complaint about making it. The limit
sits one level up: there is no *supported* amendment path once a run has begun,
only detection and a re-block. Sharpening a specification after the first
dispatch costs a run, not a quick correction inside one.

The cost is this project's own. Run `D5`'s criterion 6 required, verbatim,
"all twelve limitations" -- the line stands recorded today in
`dogfood/specs/d5-paper.md` -- at a point when `docs/LIMITATIONS.md` held 12
limits. By the time that run actually ran, run `d3e` had already merged limit
13 and limit 14, and the file held 14; that mismatch, and this same digest
mechanism as its cause, is recorded in
`dogfood/specs/d5l-limits-from-the-file.md`. The running specification could
not be corrected in place -- the same immutability this entry describes applied
to it too -- so the fix did not happen inside that run; it became a separate
one.

## 16. A usage-quota exhaustion is indistinguishable from a role that broke its contract

On 2026-09-08, around 21:15Z, two independent runs failed in the same shape
within one window, across four role sessions in total: `d5`'s QA left no answer
file behind for iteration 1, so the iteration was not accepted for lack of a
verdict, and `d5`'s planner then left no answer file behind for iteration 2,
blocking the run outright. `d4h` failed the same way, in the same window,
session for session. Every one of those four sessions produced the same
message: "left no answer file behind."

The command that exists for exactly this cause did not recognise it.
`hoh resume-quota`, run across this project's full run history, reported
`{"checked": 37, "results": []}`: 37 runs checked, 0 identified as
quota-blocked -- while two of the runs it checked, `d5` and `d4h`, were.

"Left no answer file behind" names two different causes, and only one of them
has a resume path. It can mean a role's own broken output contract -- the
session ran, produced no verdict, and there is nothing to resume from except
re-dispatching the role. Or it can mean a spent usage quota -- the session was
cut off before the role process could write anything at all, including the
marker `resume-quota` looks for. What distinguished the two here was
simultaneity across two independent runs' specifications, which no single run's
own state can observe on its own.

HoH never detects a quota exhaustion by itself, and it never automatically
resumes a blocked run for that reason; left to its own recorded state, it never
recognises the quota -- or, spelled the other way, never recognizes the quota
-- that caused the block in the first place. `resume-quota`'s own output above
is exactly that failure, not a partial success: it checked every run in this
project's history and came back with zero identified as quota-blocked, though
two of the ones it checked were. The recovery here is an operator action, not
an automated one: `hoh unblock <run> --reason "…"` lifts the block by hand,
once the operator has confirmed the cause, and only then does the role run
again.

### Citations that are provenance, not a reading instruction

Several citations in this document, and in the papers, reviews, audits and
release documents that examine this project, name an internal, unpublished
document by path: one that does not ship, and that a reader of the published
tree cannot open. **The class is defined by the shape of the sentence, not by a
list of files** -- an earlier version of this paragraph enumerated five
filenames and was out of date within a day, when the release candidate added
two more documents of exactly the same kind. A member of this class is any
sentence in a shipped document that names an unpublished path in order to say
*where* a fact is recorded, while stating the fact itself inline. This project's own export-manifest reference checker reports
every one of them as a dangling reference. None of them gets fixed by
editing the citation, because editing it would repair nothing: each
sentence already narrates inline what was found, and the path exists to
show that the check happened and where it is recorded -- not to send a
reader somewhere they can navigate to. That is provenance, not a navigation
instruction, and this project's own governance carries the class as a
named, reported, but accepted finding rather than quieting the checker --
so the same mechanism keeps catching a citation that really is broken
instead of one that merely proves its own homework. A citation that instead
promises a reader can inspect an unpublished path themselves is the
opposite shape, and is a defect rather than a member of this class -- the
minimal walkthrough example in this repository carried two citations shaped
exactly that way until they were repaired alongside this note.

This paragraph's reading of the class is wider than the decision that first
named it, which spoke only of citations inside a review or an audit: it
also covers this document's own dogfood/specs citations in the entry above
about a specification's immutability once a run has begun. Those two are
not inside a review or an audit, yet they share the identical shape -- an
inline-narrated fact, a path that shows where the check is recorded, and no
promise that a reader can open it themselves. Widening the class to cover
them is this run's operator's own judgment, not a settled question, and it
is named here precisely so a human reviewer reading this candidate can
overrule it -- in which case those two citations become a repair target of
their own, to be addressed separately, rather than something this
paragraph should be read as having already resolved.

### `check render` compares against the tree it runs in

`CLAIMS.md` is generated from `CLAIMS.json`, and `tools/check_claims.py check
render` verifies that the file on disk matches a fresh regeneration. **In a
clone of the published repository that check fails, and the failure is
expected.**

The reason is the rendering itself. A claim's displayed text is reconstructed
from the source line its `where` field names, so that a row reads as a whole
sentence rather than as the fragment the ledger stored. Where the source file
is present, the renderer reconstructs. Where it is absent, it degrades
honestly: it shows the recorded line and appends a reconstruction-fallback
marker, and it never invents a sentence it cannot read.

Several of the documents those `where` fields point at are internal working
documents that this project does not publish. The shipped `CLAIMS.md` was
therefore rendered in a tree where they exist, and re-rendering it in a tree
where they do not produces the marked fallback for exactly those rows. The two
renderings differ, `check render` reports the difference, and both renderings
are correct for the tree they were made in.

**Shipping the export-regime rendering instead would be worse**, not better: it
would replace reconstructed sentences with recorded fragments for a reader who
has no way to reconstruct them, which is the defect the reconstruction was
built to remove. What is published is therefore the more readable of the two,
and this entry is the disclosure that it cannot be reproduced from the
published tree alone.

The checks that **do** hold in a published clone are `check coverage`, `check
schema` and `check ids`, and `check all`'s own shape rule: every failure it
reports carries an environment-gap marker, and there is no unmarked one. That
rule, not an exit code of zero, is what a reader can verify here.
