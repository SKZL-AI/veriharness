# VeriHarness after v0.1.0 -- what building the harness with itself showed

*Second position paper. Status: **companion to the pre-release `v0.3.0a1`**,
prepared from the development repository at the base commit named in
`paper/NUMBERS_V2.md` (2026-10-07); the moving-present figures were taken again
on 2026-10-09 for the pre-release `v0.3.0a2`, and §15 lists what the ledger
entries O256 to O260 changed in the narrative. It describes development after the
`v0.1.0` release. It is not a release claim for phase P2, which is in progress
and not closed, and it has not yet been through the two independent
documentation reviews that `paper/AUDIT_V2.md` lists: those are `NOT_RUN` at
the time of writing, and a `NOT_RUN` is never a pass. The first paper,
`paper/POSITION_PAPER.md`, stays as it was published with `v0.1.0`; a dated
erratum was added to it, and none of its numbers was rewritten.*

## 0. Scope, sourcing, and five steps that must not be merged

**Scope.** The first paper described a campaign that produced one release.
This one describes what happened after it. P1 was completed as an internal
development milestone. No public v0.2 release was cut; the latest stable public
release remains v0.1.0 while the development line continues through P2. That
line is phase P2 (*v0.3 Durable Program Runtime + Autonomy Contract Base*),
which is **in progress**. The pre-release `v0.3.0a1` is a snapshot of it, cut at
a documentation checkpoint after the storage self-dogfood (Section P) and not
after P2 closed. P2 is not closed, and nothing below should be read as saying
it is.

**Sourcing.** Every figure in this paper carries a bracketed tag, and
`paper/NUMBERS_V2.md` lists every tag with its source -- a command, a committed
file, or an entry of the development repository's working ledger -- and how to
regenerate it; `python3 tools/docs_numbers.py check` re-derives the figures
that tools can derive and verifies that the ledger entry for every other one
contains it. A tag marked **R** there is a figure about the moving present (a
count over the history, a register status, a board row) and must be measured
again before anything is published; an unmarked tag is a dated historical
measurement. The working ledger is not part of the public export. It is cited
here by entry (`O` and a number), not by path, for the reason
`docs/LIMITATIONS.md` limit 12e gives: a citation of provenance is not a
reading instruction. A statement that rested only on the orchestrating
session's working notes, which the ledger has not recorded, would be marked
**(pending)** and would be **not yet proven** by this paper. At the base named
above two do, and say so where they stand (the two cost measurements, §2 and
§12); every other event the paper relies on is in the ledger up to its last
entry (O255) or in the repository's own history.

**Five steps, and the invariant they make.** The development after `v0.1.0`
made one distinction load-bearing, and the paper uses it in every section:

    candidate creation != product acceptance != integration acceptance != merge authority != phase closure

1. **Candidate bytes.** A product run's developer role writes a candidate in
   its own worktree. Writing it proves nothing.
2. **Acceptance.** The controller accepts a candidate only when every
   criterion passes on receipts the runner wrote and at least one criterion
   was red on the predecessor state. Acceptance is the product's verdict.
3. **Integration review.** Two independent reviewers, outside the run, read
   the accepted candidate against its specification and the merged tree, and
   release it or refuse it.
4. **Merge authority.** The orchestrating session merges a released candidate
   by importing the accepted bytes unchanged. In the self-hosted
   development the product does not merge itself; §1 counts the one
   exception.
5. **Phase closure.** A phase closes when its gate -- derived from the
   capability register, the programme checklist and an independent supervisor
   review -- says so. No merge closes a phase.

Each step has refused something the step before it passed. That is the main
empirical result of this paper, and §4 gives it.

## 1. HoH builds HoH: self-dogfooding reached the kernel

The `v0.1.0` README says, of the campaign that produced it, that **no run
wrote HoH's own logic**: runs wrote documentation, the paper, release tooling
and some tests, and every core fix was made by the orchestrating session
outside a run. That statement is true of that campaign and stays in the
README.

It is no longer true of the development line. On 2026-09-23 a product run
wrote `src/hoh/checklist.py` and its tests from a specification, in its own
clone, under a pinned autonomy contract: the loop rejected its own developer
in the first iteration and accepted the second, with no human intervention
`[V05]`. That was the first kernel capability the product built through its
own loop. Eleven product-run candidates for further P2 modules were imported
the following day `[V03]`, and from 2026-09-25 on every product run followed
one shape: an isolated clone pinned to one commit, role tabs in the operator's
Herdr workspace, the accepted candidate imported byte for byte and checked
with an import verifier, and two independent integration reviewers before any
merge. Between 2026-09-25 and 2026-10-09 there were 69 such merges into the
development mainline `[V01]`, and 55 of them change `src/hoh/` `[V02]` **R**
(46 and 34 as measured on 2026-10-07 for the first alpha, which was tagged 2026-10-08). (The count includes the merge
of the role-pane lifecycle with its tests-only follow-up, which carries a
different title and brings two runs; the tool counts it once. Since 2026-10-09
it also counts three merges whose titles the title grep had missed: the P2-17
owned contracts (`Merge p217o`), the role scratch follow-up (`Merge rbtf`) and
the second recovery slice with its two repairs (`Merge p243b`).)

**The honest ratio is not the flattering one.** The repository's attribution
check counts the development nodes after its anchor commit. Of 189 nodes
`[V06]` (141 as measured on 2026-10-07), one `[V06a]` ran through the shipped
control plane end to end, including the merge decision; 91 more `[V06b]` were
developed by the product and merged by the orchestrating session after review
(63 then), and the tool deliberately counts them in neither number; the
remaining 97 are the orchestrating session's direct work `[V06c]` **R** (77
then). Every commit but the tip
has an attribution entry at the base, the records commits included; the tip's
entry is written by the next commit, which is the price of a ledger that cannot
name its own commit. Outside that one node, the merge decision stayed with the
orchestrating session, by design: the product writes and accepts, and an
orchestrating agent, under the operator's policy, decides what enters the
mainline. Self-dogfooding of the kernel is therefore real at the level of
*candidate bytes and acceptance*, and almost absent at the level of *merge
authority*.

**What orchestrator-direct work remains, and why.** A trust-boundary repair
that could not wait for a product run (§5) and the instruments that define
"invented work" were written by the orchestrating session directly, and are
attributed that way. In one case an operator caught that a whole increment had
been written orchestrator-direct with in-process reviewers, and the practice
was corrected by addition (ledger O234): from then on, product work goes
through `hoh start` / `hoh run` in Herdr, and reviewers run as visible agents
in their own panes. This checkpoint's own documents are orchestrator-direct
work too, and are attributed that way.

## 2. Durable programme state and succession

A harness that is meant to run for days has to survive the death of the
process driving it, and of the session driving that process. Before P2 the
first paper could show a control plane surviving deliberate kills at its
riskiest boundaries -- in minutes, on a fixture. P2 is the attempt to make
that a property of the programme rather than of one run.

**What exists.** Durable timers instead of sleeping processes; a sentinel that
says what is due; a never-stuck check; a master epoch with a recorded holder;
session succession, under which a returning old master's writes are not
authoritative; a leader lease (at the first alpha still only a per-project lock,
PARTIAL; since 2026-10-08 P2-09 reads PROVEN under the limits `RELEASE_NOTES.md`
states); a resume loop that acts on due timers; an unattended continuation unit that
drives a registered project on after a quota wait or a dead master; an
autonomy contract that says what a programme may decide on its own; a project
contract that says what an external project manager owns; an `EvidenceClaim`
schema linked into the digital thread; and a programme checklist whose phase
gate an independent supervisor review must pass. Of P2's 43 registered
requirements `[V10b]`, 32 read PROVEN `[V10c]` (26 as measured on 2026-10-07), and
the remainder are PARTIAL, IMPLEMENTED_NOT_PROVEN or missing **R**. The register as a whole holds 227
requirements `[V10]`: P1's 16 all read PROVEN `[V11]`, and 150 of the 168
registered for P3 to P12 read MISSING `[V11b]` **R**.

**A rollover protocol, built and not exercised (context rollover, Layer A).**
On 2026-10-07 the operator asked that the orchestrating session be able to move
into a fresh session automatically, and that the move be built as a feature of
the product. The answer that was reviewed and merged (`601168fe`, a product run)
is a protocol and not a prompt: the outgoing session freezes a *capsule* whose
entries are derived facts, which the successor must re-derive from git, files
and run states; operator decisions, kept verbatim and never answered by the
machine; and advisory context, which has no authority. The successor starts from
a pointer only, acknowledges, and takes the master epoch only if everything
re-derives, after a re-verification that sits between the acknowledgement and
the acquire; a stale predecessor is fenced, and no acknowledgement, a wrong
digest or a moved fact leaves the old session master. Two blocking findings of
the specification review shaped it: nothing re-verified the facts between the
acknowledgement and the acquire, and an author-supplied programme generation
made the generation check vacuous. The two integration reviewers released it,
and each filed minor findings that are open (§13). It is tested with fakes.
**No live succession has been run, and no register row is proven by it.** The
handoff that the operator's request itself triggered was written by hand, before
any of this existed; it is recorded as a pre-protocol rotation and as cost
evidence only. **(pending)**: the orchestrating session's notes report that the
outgoing session's requests had grown to about 310,000 context tokens on
average, against about 98,000 for the successor's first requests; that is a
local transcript scan, not re-measured here.

**The measurement that started it.** Before the resume loop was built, the
orchestrating session measured what the durable machinery had actually done on
this machine: the epoch file had never been written anywhere, so no master
epoch had existed outside a test's temporary directory; the quota block class
had never once been recorded across 13,040 run-state files `[V14]`; and the
succession module the plan required did not exist (ledger O232). The design
had been described for days; it had not happened.

**The end-to-end evidence, and how it was lost.** `tools/durability_e2e.py`
runs seven scenarios from a `git archive` of a named commit, with targeted
negative controls -- 19 at first, 20 later -- that must each be caught
`[V15]`. A first raise of P2's exit conditions to PROVEN was **withdrawn the
same day**: an evidence reviewer showed that a retry timer dropped fractional
seconds and could come due before the provider's reset, and that the evidence
itself contained the failure (ledger O237). After the fix went through its own
product run, fresh evidence was taken, reviewed by two agents, and the five
exit conditions P2-33 to P2-37 read PROVEN. They do not any more (§3). And
P2-11, crash and reboot recovery, was never proven: it rests on stressor DS2,
a real restart driven by the operator, which has never been run. What "PROVEN"
covered even while it held was stated in the register's own notes: the
orchestration layer with fixture role runs and the harness's tick loop -- not
systemd, not the plugin entry point, not a real role agent's permission
dialogs.

## 3. Evidence goes stale when the code it describes changes

The README of the `v0.1.0` release contains a sentence that turned out to be a
design principle: a measurement of a tree has to name the tree, because *the
commit that writes the numbers changes them*. P2 made the same point about
evidence, and turned it into a rule the capability matrix enforces: an
acceptance record is evidence about one real run at one commit, and it is
refused as `STALE` when any file under its relevant paths changed after that
commit, or when it was taken on a dirty tree (ledger O234).

The rule then did exactly what it was built to do, to the programme's own
headline result. The durability evidence behind P2-33 to P2-37 was taken at
one commit; the storage work that followed changed the launcher, the resume
loop, the controller, the timers, the store and the CLI. The requirements fell
back to IMPLEMENTED_NOT_PROVEN, and the entry that recorded the recovery of
the interrupted work says so by addition rather than letting the earlier
PROVEN stand (ledger O250). A later change to the durability record's own
content means the committed record no longer re-derives at all; only new
evidence, taken at the head and reviewed, can raise them again (ledger O251).

The same mechanism reaches the release evidence. The matched-budget benchmark
of the first paper is unchanged as a dataset, but its instrument freeze now
reads `DRIFTED`: files under the freeze changed after the campaign and are not
named in its drift accounting, so the board's `benchmark_v3` row reads `FAIL`
at the development head **R**. The campaign's result is still the result; what
is red is the accounting for the code that moved since.

The lesson is uncomfortable and is the point: in a codebase that changes every
day, a PROVEN status is a perishable good. A board that kept it after the code
moved would be reporting the past as the present.

## 4. Acceptance is not merge: integration review refuses what the product accepted

Between the development ledger's entries O236 and O254, 26 candidates that the
product had accepted were refused by independent integration review `[V07t]`:
18 in O236 to O251 `[V07]` and 8 in O252 to O254 `[V07b]` (the last is the
role-pane lifecycle: its reviewers found the code correct and refused its
tests). In the same
period there were 46 merges, the count measured on 2026-10-07 (`[V01]` reads 69
at the base of the second alpha, which also counts the entries after O254)
**R**, 22 of them in the window of the first count `[V08]`. Some refusals were repeated runs of one specification, so
the ratio describes attempts, not features. The refusals were not about style.
Examples, each proven by a reviewer before it was recorded:

* A proof-class rule meant to stop evidence-removing classes being chosen
  freely accepted a quote cut out of a longer word (`"base"` out of
  "database") -- the escape hatch the specification was written to close.
* An archive verifier certified a **link-only archive** and issued a receipt
  for it, on the verification side of the very failure the storage work exists
  to prevent.
* The first storage steward re-read its removal set from a staged manifest at
  removal time, so an entry appended after placement could make it unlink a
  file outside the item.
* A long-prompt fix wrote prompt files at a predictable name, so a symlink
  planted there redirected the write.
* A durability-record change collapsed three distinct crash cases into one
  sorted set.
* A supervisor-drift tool read a link outside the repository through a
  symlinked parent because it called `os.readlink` before the inside-the-
  repository check.
* A dispatch-residue run was accepted by the product although the criteria its
  specification named K16 to K22 were never written: the run claimed all
  criteria demonstrated, and the node ids existed only in the drafter's
  prototype. The orchestrator ruled the reviewer who refused it right.
* Two accepted candidates of the QA-arena strand were each refused by both
  reviewers. In the first refusal a hard link let the seal change the mode of a
  foreign file, and a sticky approval wait let the run be reported ACCEPTED. In
  the second, a contract's stop routing overwrote the one field that carried
  the violation hold, so `hoh deliver` ran the git fast-forward of a
  violation-held candidate; the reviewer's test showed the main branch equal to
  the candidate, and the command returned an error only because the transition
  afterwards was refused. The candidate that finally merged was released by both
  reviewers, and still left three findings of one reviewer open (§13): the
  integration review of a merge is a process, and the merge message that said
  the false-ACCEPTED paths were closed said more than that reviewer had proven.
  A follow-up strand closed those findings (merge `ab41589f`, §13), after four
  runs of it had been blocked at the first dispatch for reasons that were not
  its own (§5).
* Two accepted candidates of the temporary-root strand were refused. In the
  first, cleanup crossed a bind mount on the same filesystem and deleted a
  foreign file, proven in a private user namespace. In the second, a scratch
  vetted by an unsandboxed use stayed
  vetted after a sandboxed check had planted a configuration alias in it, and
  the alias then ran outside the sandbox.

Three times the product accepted a candidate over a defect its own QA role had
recorded as an open gap `[V09]`: the controller's acceptance reads only the
criteria's verdicts, and an open gap decides nothing. A gate for this was
specified, built through the product, and refused at integration twice. A
later attempt was blocked at planning for a reason unrelated to the gate (§5).
It is not merged.

A refusal does not always mean the code was wrong. For one run both reviewers
found the code correct and one still refused it, because two criteria did not
bind the behaviour: a test rig made the pane id equal to the agent target, so
mutants that pressed Enter through the wrong target survived. The repair was a
tests-only follow-up, pinned on the accepted candidate and merged together with
it. The pattern recurs: a follow-up whose criteria bind the content of the
candidate by digest and are checked against a mutant table.

What follows is not that acceptance is worthless. Acceptance is a necessary
condition for a merge, it is cheap to re-check, and it rejected iterations
that never needed a reviewer. What follows is that **acceptance is not
sufficient**, and the first paper's claim -- per-run evidence binding is
necessary and not sufficient for composition -- has a second half that the
development line measured: per-run acceptance is necessary and not sufficient
for integration either.

## 5. Iterations lost to the machinery, not to the candidate

The five steps were also tested from the other side: what the product's own
machinery did to candidates that were not at fault. Several failures of this
kind are on the record.

**A provider usage limit booked as a rejection.** When the account reached its
usage limit while a QA role was working, the product recorded the iteration as
"candidate rejected: unverified, no QA verdict" and, within about two minutes,
the planner's own dispatch hit the same limit and was classified as a resumable
quota block. The same limit in a planner is resumable; in QA it consumed an
iteration. It happened eight times `[V26]`: seven runs in iteration 1 and one in
iteration 2. The first count in the ledger was five; an integration review of
the records found two more with the identical signature, and the count was
corrected by addition. The merge records of two of the first seven runs did not say
that their iteration 1 had been lost this way. The eighth, in the
role-pane-lifecycle run, is read from its run state: the QA turn ended without
an answer file, the pane showed the usage-limit text, the iteration was booked
as rejected, and the planner of the next iteration was blocked by the same limit
within two minutes (ledger O255). The repair -- normalised
failure classes in which a usage or rate limit is never a candidate failure and
never an iteration -- is the core of the P2 item of amendment A5 (provider and
model routing with durable failover, registered by the operator on
2026-10-05). A5 registers five requirements, one per phase: minimal durable
semantics in P2, runtime routing in P3, adapters in P10, a calibrated
recommendation in P11 that is never an authorisation, and host placement in
P12. All five read MISSING `[V40]`; nothing of A5 is built, and the repair is
**not merged**.

**A stalled prompt, and the residue it left.** One run lost two iterations to a
dispatch whose first prompt was typed and never submitted; at the next
iteration the new pointer was appended to that residue and read by the agent
interface as an unknown slash command `[V22]`. Root cause was read from QA's own
session transcript. The repair came in three pieces: a stalled prompt is a
transient (§10), prompts no longer begin with `/` and a pane holding unsent
input is refused, and after a stalled submit Enter is pressed once on the
dispatch's own prompt. Until the last piece merged, the operator granted one
narrow manual exception, logged per case; it was used once. In the runs that had
the recovery, seven first prompts stalled and each was recovered `[V31]`. The
startup race itself is not fixed.

**Invisible characters.** A specification that carried four invisible
characters in its own test cases stalled two planner dispatches `[V28]`: the
agent interface announced it had removed them and waited for a keypress, so the
inline paste of the prompt was never submitted, and every transient retry pasted
it again. Size alone was not the cause. The remedy -- send every long prompt,
and any prompt with control or format characters, through the file pointer --
was merged (`33320bfc`) after the same finding recurred from another side: a
long inline paste is rendered collapsed, so the stalled-submit recovery could
not prove what the box held and refused, and three runs of the qa-arena follow-up strand
blocked at their first dispatch before any candidate. The strand that fixes
this had the same problem itself, and was built by a product run under one
declared deviation, a bootstrap harness (the previous source plus only the
reviewed dispatcher change; the operator delegated the choice in the words
"mach das beste hinsichtlich dem mindset vom veriharness und optimiere passend
zum veriharness"); the harness was retired at the merge and the candidate was
written independently of it.

**A pointer line, word-wrapped.** The recovery models the input box: a line
that does not fit breaks mid-word at exactly the box width. A fourth blocked run
showed that Claude Code breaks at a word boundary instead: in a 79-column pane
the pointer's first line (77 columns with its indentation, against a box width
of 76) wrapped before its last word, the recovery refused as designed, and the
run was blocked before any candidate. The recorded box makes the cause provable;
in a 106-column pane the same text passed. Nothing was pressed by hand, and the
operator widened the window. The repair -- readiness of the first prompt and a
model of the wrap -- is the next P2 strand, not started.

**A role timeout.** A run was blocked at the default role timeout of 2700
seconds while its developer was still working `[V29]`, and the block was
routed to the operator's digest as an unclassified decision. The run was
relaunched with a larger timeout through a copy of the launcher; the
repository's launcher is unchanged.

**A role woken by its own background task.** The candidate is frozen when the
developer is recorded as done, and the binding check compares the working tree
during QA. In one run a developer's mutation command exceeded the 120-second
limit of the agent's shell tool `[V30]` and was moved to the background; the
developer ended its turn
"waiting for the completion notification"; the controller saw an idle pane,
recorded the developer as done and froze the candidate; and minutes later the
notification re-woke the developer, which edited source and test files while QA
was running. The binding check failed closed, as designed, and the run was
blocked. The product defect is a class: **a role turn is taken as ended while
the role still has work pending**. In a later run the same pattern appeared
again and the orchestrator stopped the developer's process by hand before it
could wake, an interim stand-in recorded as such. The repair is a strand of its
own (role-pane lifecycle): close the panes the product no longer needs, and
make sure a role cannot resume before the freeze. A live smoke on the real
agent runtime taught what closing a pane does and does not do: a background
command the agent had started, then an ended turn, then `herdr pane close`; after
five seconds the agent process and its shell were gone and the background
process was still alive, orphaned `[V32]`. Closing the pane prevents the agent
from waking and does **not** end its background descendants, so a surviving
child that writes could still change the worktree after the freeze. The
specification therefore ends the role's processes and fails closed if any
survives, and it is merged (`caa732db`) together with a tests-only follow-up:
its integration reviewers found the production code correct and refused the
tests, because one test assumed a process layout that does not hold when the
check runs the way the product runs it, and nothing bound the rule that a whole
tab closes only when every pane in it is HoH's own. The follow-up rewrote one
test file. The lifecycle closes the role's own panes and ends only processes
that carry the pane's minted mark and are anchored to the pane's own shell; one
signal to one process, never a group; a marked process outside the pane is
named and not signalled. Its first live use closed the planner and developer panes at turn end; the
QA pane's release was refused because two marked processes had escaped it, and
the pane was gone without anyone closing it by hand (Correction, 2026-10-08: the lifecycle log of that run, `pane-lifecycle.jsonl`, also holds a `closing` and a `closed` line for the QA pane, in the same second as the `refused` line; the earlier "closed three panes and refused one" counted that pane twice, and the text above replaced it.) (Further correction, 2026-10-08: the `closing` line is written by HoH before its own close command and the `closed` line only after a second snapshot no longer lists the pane, and a pane found already gone is written with an `already_gone` field, which this line does not carry (`_close_own_pane` in `src/hoh/dispatchers.py`); so HoH itself closed the QA pane's tab, nobody closed it by hand, and the `refused` line that follows names the two escaped processes, not a failed close.) What stays open is
in §13.

**The planner re-types the criteria.** A planner copies the specification's
criterion commands, and nothing compares the planned command with the
specification's text. A replay of 53 planner answers found 195 deviating checks
in 42 answers `[V34]`: transcription errors that cost an iteration, a
`--deselect`, wrappers, added and dropped criteria, replaced criteria. For every
merge of that wave the integration reviewers ran the specification's own text
verbatim on the merged tree; the product's acceptance did not. The fix is
specified and waits.

**The orchestrator's own tooling failed the same way.** Prompts for the
orchestrator's reviewers stayed unsent in their input boxes for about an hour,
more than once; the orchestrator's helper that presses Enter missed two because
the pane soft-wrapped the phrase it matched. Enter was pressed for those
reviewers, never for a product run.

## 6. STORAGE-D, Section P, and an operation that is allowed to remove

**The incident.** On 2026-09-25 the disk under the development machine filled:
the operator's copy of the WSL disk image stood at 977 GiB of 1 TiB, and about
74 GiB of the guest's 653 GiB were VeriHarness's `[V16]`. VeriHarness was not
the main consumer, but it had no measurement of the space it was about to use,
its reviewer scratch had no owner record and no lifecycle, and the house rule
"never delete, version instead" made its growth monotonic by construction
(ledger O238). Whether the full disk caused the failed regeneration recorded
that evening is **not established**.

**The response was an amendment, not an exception.** The operator's directive
entered the programme as amendment A1, pinned by digest and parsed by the
plan's own parser, with its requirements placed in the phases where they
belong -- four of them in P2, so P2 cannot close without them. The work was
built through the product, in two waves of specifications, each reviewed
before it was pinned.

**VERIFIED_RELOCATION.** The storage steward (STORAGE-D) frees space through
exactly one operation, `VERIFIED_RELOCATION`: a candidate is re-classified,
archived to a target, the archive is verified, a receipt is made durable, and
only then is the local copy removed -- for two classes only, a verified
duplicate of a kept original and reproducible scratch. A test scans the
storage modules, the controller and the CLI for any removal call outside that
operation's `execute`. The steward took three product runs of one
specification; the first two were refused at integration (§4), the third
merged with both reviewers releasing it `[V20]`.

Two precise limits belong here. First, the relocation module's docstring says
that nothing else in VeriHarness removes a file or a directory; the scan that
backs it covers a named set of modules, and the durable-write primitive does
remove its own temporary file, as the workspace module removes a temporary
index -- removals of an operation's own scratch, not of project data, but the
docstring says more than its test proves. Second, one entry in the mandatory
deny list that every role's permission policy must carry, `Bash(find *
-delete:*)`, was read by the agent's permission system as a literal prefix and
never matched; that was proven for one version of the agent's matcher and
repaired in a product run, which also checks permission-rule forms at load. What
remains is narrower: quoted `-delete`, absolute binary paths and `command` or
`xargs` wrappers are not matched by a role's own rules, and the machine-wide
deny hook still covers every session.

The pre-product reclamation that did happen -- a frozen list of verified
duplicates and reproducible scratch, 124 paths and 29.07 GiB, approved by the
operator and re-checked path by path before each removal `[V19]` -- was
orchestrator tooling at the operator's request, not the product's operation, and
is recorded as such.

**Section P: the first real self-dogfood.** The project was adopted as its own
storage trust root through a committed anchor, as a product operation (`hoh
storage adopt-project`), and a second adoption run was idempotent. The survey of
legacy reviewer scratch, run detached at idle priority, took about 3.4 hours
after two earlier attempts had been cut off by the two-hour limit on background
commands and left no output; it found 0 of 188 roots eligible `[V33]`. Every
refusal was fail-closed: the reasons were that no committed report names the
root, that no committed designation names it, or that its content cannot be
bound (hard links from git clones, FIFOs left by tests, symlinks). The yield was
zero, as the specification of the legacy path had predicted, and no anchor was
built by hand. The steward's real pass reported state NORMAL and **observed
only**; no byte was selected, reclaimed or removed. `adopt-legacy` ended with an
empty outcome list and exit code 2, by design, and Section P produced no
`VERIFIED_RELOCATION` receipt. A first reading of the records was corrected by
addition: the survey's "vanished" entries were dangling or looping symlinks, not
paths that vanished, and 157 committed briefs under `dogfood/` already designate
98 of the 188 roots by accident `[V33b]` -- a committed non-report file naming a
root is a designation by the product's own rule -- while no committed report
names any, so every root stays refused. A near-zero reclaim was accepted as a
valid result of Section P; whether committing reports to make legacy roots
eligible is worth the effort is open.

**Inode exhaustion.** On 2026-10-05 every command on the root filesystem failed
with `ENOSPC` while about a hundred gigabytes were free, twice: the inode table
stood at 67,107,099 of 67,108,864 `[V35]`. The cause was structural --
accumulated small-file scratch, pytest temporary trees of reviewers and drafters
-- and the process that made the final push was not identified. Reviewer results
of that window were to be checked for `ENOSPC` traces before being accepted, a
check still open when the ledger entry was cut. The steward measured bytes only.
The operator directed a cleanup of the orchestrator's own reproducible scratch
under the temporary-file rule: a manifest written before the first removal,
4,211 trees and 15,558,933 entries `[V36]`, none in use or referenced by a
record. A second cleanup the next day inventoried 2,003 repository copies,
proved 342 reproducible because every file in them is a git blob of the
canonical repository, and removed 341 (one was in use): 6,562,367 entries and
37,598,417,431 bytes, with 1,661 kept because they hold files that are not blobs
`[V37]`. No relocation was possible through the product's allowed path, because
the steward at NORMAL only observes and the survey had zero eligible roots.
Prevention was written into briefs (inode checks beside byte checks; scratch
caps; no full clones; self-removing basetemps), and the cap was exceeded and
recorded again in the days that followed -- the lesson being that a rule in a
brief is not a product feature. The product's own answer is P2-42: a registered,
project-owned temporary root, workspaces cleaned by their creator, admitted
writes and an inode signal next to the byte signal. It took three runs, two of
them refused at integration (§4), and is merged `[V38]`; the register reads PARTIAL
`[V10d]` for it at the base: its edit names the merged symbol with an equivalent.
No record yet shows the signal meeting a real pressure event.

## 7. A file name that is not UTF-8

A planner dispatch blocked with `'utf-8' codec can't encode character
'\udce9'`. The cause, reproduced read-only at the same position: the
capability witness folded witnessed file names with a strict encode, and an
existing test of the archive module legitimately left a file with a Latin-1
byte in its name in a check's temporary directory inside the arena (ledger
O249). The witness did not fail open -- it stopped the run -- but it stopped
it for the witness's own defect, not for anything a role did. The six
reproducible scratch files were moved out of the arena with a manifest
(nothing deleted) `[V23]`, the run resumed, and the root cause went through
its own product run: names are folded with `os.fsencode`, git is read as
bytes, and the digests of UTF-8 names are unchanged (ledger O251).

The second half was closed by a later run: non-UTF-8 text is now recorded in the
run state losslessly and injectively, and existing state files serialise
byte-identically (ledger O252). Open: the clean-tree check still decodes `git
status` strictly (tracked medium). A failure mode that only appears when a test
happens to write an unusual byte is the kind a harness has to meet in
production before it is found.

## 8. A prompt longer than one argument

A steward follow-up run blocked at its developer dispatch with `[Errno 7]
Argument list too long: 'herdr'`: the dispatcher passed the whole prompt as
one command-line argument, beyond the kernel's per-argument limit
(`MAX_ARG_STRLEN`); the steward's specification alone, which the prompt
carries, was 116,592 bytes `[V21a]`. The first fix was accepted by the product
and refused at integration (§4: a planted link at a predictable prompt-file
name redirected the write, and the binding was lost on an aborted or resumed
dispatch). The second, after five specification-review rounds, writes
link-safe prompt files, compares them only against a digest held in memory,
and appends an audit record before the dispatch. It was then shown live: a
developer prompt of 154,480 bytes `[V21b]` and a planner prompt of 122,734
bytes `[V21c]` went through prompt files, each with its audit record, and were
answered (ledger O251).

The stated boundary is that the directory chain up to those files is trusted
not to be swapped by a role running as the same user; hardening it is tracked
as high (T-LP5), with a released and unbuilt specification. And the fix produced
a new failure one layer up, recorded in §5: the pointer prompt began with a
`/`, which an agent interface parsed, together with leftover unsent input, as an
unknown slash command. That is repaired: prompts no longer begin with `/`, and a
pane that holds unsent input is refused rather than typed into. Since the
prompt-threshold merge every role prompt above 8 KiB, and every prompt holding a
character the input box treats specially, travels the same way (§5).

## 9. Recovering interrupted state without rewriting it

When the disk filled, the orchestrating session's working tree of the
development repository held uncommitted work -- 3,861 entries in `git status`,
among them 3,382 files that were the only copy of an evidence directory
`[V17]`. It was not committed in a hurry and not cleaned up. It was
snapshotted as a commit object on a preserve reference, without changing the
working tree, and left alone while the storage work was built on a separate
branch in a registered state root (ledger O238).

Recovery, days later, was a merge: a new branch at the preserved commit, the
storage line merged into it, the conflicts resolved as unions where both sides
only appended (the ledger, the attribution record) and by regeneration where
the files are derived; 17 files conflicted `[V18]`. No history was rewritten
and nothing was removed (ledger O250). The recovery's own review then
corrected the recovery entry twice -- it had misquoted which requirement was
raised before the incident, and stated the full disk as a cause where the
incident record leaves it open -- and both corrections were made by addition.

The same discipline applied to the records of the next waves. When integration
review found that the first count of quota losses in the ledger was five, not
seven, the series of commits that had carried it had not yet left the
drafter's clone; it was rebuilt rather than patched, the earlier objects were
kept on a local reference, and the correction was recorded by addition (ledger
O252). The general point: *interrupted* is a state, not a failure to be
tidied. A preserve reference costs nothing; a "cleanup" of a working tree that
held a single copy of evidence would have cost the evidence.

## 10. Idle is not done: dispatch under real agents

Four runs lost an iteration in the same way: QA started a long suite in the
background, ended its turn saying it would write its answer when the suite
finished, and the dispatcher read the idle pane as a finished dispatch with no
answer (ledger O237, O242, O244). In one case the answer file was written
after the iteration had already been rejected, into the closed iteration's
path. A planner blocked twice on a permission dialog for writing its own
answer file, and the product -- correctly -- does not answer dialogs (ledger
O237); a scoped allow rule for a role's own answer directory was then built
through the product, but no run record yet shows a live session writing
without a prompt, so the claim is "rule written", not "dialog removed".

The dispatch-robustness run makes a stalled prompt transient and gives an agent
that ended its turn without an answer exactly one follow-up before the dispatch
counts as unanswered. Its integration reviewers ran the specification's full
criterion on the merge and on the base with a short temporary path, because the
planner had narrowed it by a `--deselect` after a house-rules test failed in a
long arena path; the test itself was repaired by a later test-only run. The
residue follow-up -- a pane that still holds unsent input is refused rather than
typed into, and prompts no longer begin with `/` -- merged after its first form
was refused at integration (§4). Two findings sit next to it:

* **Idle is not quiescent, and the lifecycle answers it only in part.** A role
  can end its turn while a background task that can wake it is pending (§5). The
  dispatcher recovers an unanswered turn and a stalled prompt, and the merged
  role-pane lifecycle ends the processes it can tie to the role's pane before the
  freeze; it still does not know that a role has nothing left that can write by
  a route it does not see (a command that scrubbed its mark and detached, a
  shared daemon, which it names and never ends).
* **Criterion transcription.** The planner re-types the specification's
  criterion commands (§5). The product accepted a candidate whose planner had
  narrowed a criterion, too: disclosed, shown harmless afterwards, and still a
  change the specification did not make.

## 11. A shadow pilot of an external review tool

The operator asked for one non-blocking shadow pilot of an external,
MIT-licensed deep-review tool, local-only and read-only, on one accepted
historical candidate, as exploratory evidence for a future, optional
review-provider contract (amendment A4, phase P10) -- not as P10 implemented.
Its pinned source and toolchain were downloaded first, into the pilot's own
directory; the build and the runs were then offline, in a user namespace, with
telemetry off (ledger O251) `[V25]`.

What it taught is mostly about the word *read-only*. The review clone was
dedicated, and still not immutable: the tool moved its `HEAD`, ran `git
worktree add/remove/prune`, and wrote 145 MB into the clone's `.git` `[V25]`;
the clone's `origin` still named the integration worktree; and a lazy fetch
wrote a pack into a study clone that was meant to stay untouched. None of this
reached a canonical repository, and all of it is recorded. It is a concrete
case for what the P10 contract, registered before the pilot, already requires:
review clones at commit pins of one candidate, classified as derived review
material and never a standing duplicate, an immutable pin rather than a
version that changes in place, and a local-only profile that only an explicit
operator authorisation may relax. Tool output is derived, never independent
support, and never part of the acceptance kernel.

## 12. Capacity: a number of agents is not a measure of work

The programme ran its own agents under a provider quota, and the operator's
policy for that quota changed several times in three days. On 2026-10-04 an
adaptive, provider-aware parallelism set a soft target of about 10 active
provider sessions `[V39]`, after a first instruction for maximum parallelism
the same day; an addendum then asked, after a real limit, to keep useful work,
start nothing new and restart at 4 to 6 weighted loads `[V39]`. On 2026-10-05
the soft target became about 5, still not a hard bound `[V39]`, and the same
day the weekly burn signal read 75 percent `[V43]`. On 2026-10-06 both numeric
targets were retired. The default became **work-item first**: an upper bound of
4 independent strands and 2 agents per strand as the normal profile `[V39]` --
a bound, not a target -- with roles sequential inside a strand and two
concurrent agents only at a dual-review gate, dual review kept, targeted
verification for a bounded change, minor findings folded in without a new
round, new rounds only for blocking findings, and foreign sessions observed and
never a target.

The empirical input, recorded as such: numeric provider-concurrency targets
raised provider burn without raising completed work. A capacity planner (the
P3 item P3-41, MISSING) should reason over independent work items, the
dependency graph, the value on the critical path, the role, the cost of
verification, provider burn, and machine pressure in bytes and inodes. One
incident belongs here as input to P3 scheduler-lease design: two instances of the
orchestrator's paced re-prompt scheduler ran at once and could prompt the same
reviewers twice; a single-writer lock was added.

On 2026-10-07 the operator reported that the weekly limit had again been used
in one day and asked to return to the way the programme had worked the week
before, in particular with subagents, audit and evals. The decision (in the
house rules, as LEAN_VERIFICATION) bounded the *depth* of verification, not its
existence: two strands until the pre-release, drafting by source analysis and
targeted tests, one round of specification review, an integration pair that runs
only the mutants the specification names, the full-suite comparison run once by
the orchestrator, and a merge only when a gate tool finds every required review
report bound to the candidate, the base and the specification with no blocking
finding open. **(pending)**: the orchestrating session's notes measured the
cost on local transcripts as a price proxy, not an official quota account:
drafting subagents were the largest item since 2026-10-04 and merged work per
unit had fallen several-fold.

The rule that survived every version of the policy: **a quota error is never a
product verdict**. Every usage-limit stop was parked and resumed; none was
counted as a result. The product's own booking of a QA usage limit as a
rejection (§5) is the one place where that rule is not yet true of the code.

## 13. What is not proven, and would change this position

* **The pre-release's integrity gate is met for the paths its reviewers
  replayed, and the paper claims no more.** The qa-arena-write repair closes
  the false-ACCEPTED and foreign-write paths for every state shape the current
  build writes. Its reviewer proved three findings open -- a violation state
  written before the hold existed could be unblocked by the resume loop and
  then reported ACCEPTED; the acceptance record ignored an open seal; a
  symlinked seal ledger got ledger lines appended to the foreign file -- and
  the first draft of this paper said the gate was not met and the release was
  held. The qaw-followup merge (`ab41589f`) closes them, and a forged
  sibling-arena plan line, for the replayed paths: both integration reviewers
  replayed each on the merged tree and saw it refused. Not shown: that no other
  such path exists. Stated and pinned by the strand itself: a role pane that
  has read the run's `arena_secret` and copies iteration and attempt gets a
  forged plan honoured inside its own arena. The sentence of an earlier entry
  of the ledger that the legacy fallback of the hold only denies was wrong, and
  stays corrected.
* **P2 is not closed.** Its gate has 11 requirements blocking and open (17 as
  measured on 2026-10-07), and 3
  exit conditions no instrument measures yet `[V12]` **R**; the board reads
  `TECHNICALLY_STABLE_READY = no` `[V13]` **R**. The pre-release is planned
  anyway, by the operator's decision, and says so.
* **No real reboot** has been run (DS2), so crash-and-reboot recovery is not
  proven.
* **The durability evidence is stale**; P2-33 to P2-37 must be re-evidenced at
  the head and reviewed.
* **The quota misclassification is not repaired**, nor is anything of
  amendment A5 built `[V40]`.
* **The startup race behind the stalled first prompt is not fixed**; dispatch
  recovers for every prompt, and the recovery has a width limit (§5): a pointer
  line that Claude Code word-wraps is refused, as designed.
* **A role's own processes are ended before the freeze, with limits**: the
  role-pane lifecycle is merged and has had one live use; a few extra mutants
  of its pane-count guard survive its tests (fail closed), and a detached daemon
  adopted by a subreaper is documented as escaping.
* **Context rollover has not run live**, so nothing in the register is proven
  by it. Its draining states are recorded and not enforced; a stalled first
  prompt can lose the successor's pointer (fail closed); and its follow-up items
  are open: an unvalidated capsule id, an acquire that is not atomic with the
  last verification (a master gap is possible, never two masters), a relative
  root that never rolls over, and two input checks.
* **Disk-full safety is partial** (P2-41): 12 writers remain `[V44]`.
* **Registered temporary roots** (P2-42) are merged; the register edit and the
  deferred EPHEMERAL_TMP tier and temporary-path relocation (P5-35) are not
  done.
* **The storage steward** has not acted on VeriHarness's own data: its real
  pass observed only `[V33]`.
* **Acceptance ignores QA-recorded blocking gaps**, **does not compare a
  candidate's changed paths with its contract's scope**, and **does not compare
  the planner's criteria with the specification's text** `[V34]`.
* **The exact-head external CI** has not run on these bytes, and the export
  has not been synchronised.
* **Long-duration unattended operation** -- days without intervention, across
  provider outages -- is not demonstrated. The first paper's limit stands.

What would change the position: a live succession under the rollover protocol,
after its follow-up items are closed, shown by a record that re-derives; the
quota repair merged and shown on live runs; the first prompt accepted without a
recovery, at any pane width; fresh durability evidence at the head that two
reviewers release; an operator-run DS2; the steward's first real pass that
relocates something of VeriHarness's own registered data, with receipts; a
QA-gap gate that survives integration review; and a P2 gate that reads met on a
current, independent supervisor review.

## 14. Numbers, audit, and how to check this paper

`paper/NUMBERS_V2.md` lists every tagged figure, its source and its
regeneration. `python3 tools/docs_numbers.py check` verifies it.
`paper/AUDIT_V2.md` records the mechanical checks that were run and holds the
checklist for the two independent documentation reviews that must happen before
this paper is described as reviewed; until they have happened and been answered,
this paper is not reviewed. The first paper and its audit,
`paper/POSITION_PAPER.md` and `paper/AUDIT.md`, are the record of `v0.1.0`; they
carry dated errata added by this checkpoint, and none of their figures was
changed.

## 15. Update for the pre-release v0.3.0a2 (added 2026-10-09)

The narrative above is the text of the first alpha, and it stays as written. The
figures marked **R** were taken again at the base named in `paper/NUMBERS_V2.md`
and carry their earlier value in parentheses. These statements of the narrative
no longer hold, and what holds now is as follows; `RELEASE_NOTES.md` has the account
and every limit.

* **The quota repair (§5 and §13).** "It is not merged" is superseded for
  the first two slices: a failed provider dispatch is classified, every dispatch
  records its provenance, every park is bound to what it stands on, QA re-raises a
  provider failure instead of booking it as a rejection, and a role's own words
  no longer park a run through the abort path. The binding is written and nothing
  checks it on resume. The register still reads P2-43 MISSING `[V40]`, the third
  slice is not merged at the base of this text, and the place where §12 found the rule that a quota error is never a
  product verdict not yet true of the code (QA) is changed in the merged code,
  shown by the merge run's tests and its integration review and not on a live
  run.
* **The wrapped pointer line (§5).** "The next P2 strand, not started" is
  superseded: first-prompt readiness is merged. Whether the wait before the first
  prompt cures the stall is SUSPECTED until a live check; the recovery still fails
  closed when its model of the wrap does not apply.
* **The rollover (§5 and §13).** The follow-up items listed in §13
  are fixed in code, the launcher starts its successor in an explicit permission
  mode, and the acquire can be conditional on an expected handover. No live
  succession has been run, so nothing in the register is proven by one.
* **The leader lease and the register.** P2-09, P2-12, P2-13, P2-15, P2-17 and
  P2-21 read PROVEN, each with limits that its register note and `RELEASE_NOTES.md`
  state; the matrix measures an import statement and never execution, so the weight
  of each rests on the merge run's criteria and its integration pair.
* **Disk-full safety (§13).** "12 writers remain" `[V44]` is the figure of
  the first alpha. Two more slices are merged (P2-41), which leave the row PARTIAL
  and the writers of the dispatcher unmigrated; the figure is not re-derived.
* **Integration review.** The count of candidates refused at integration review
  `[V07t]` is dated to the entries O236 to O254. Four more candidates were refused
  and merged only with a repair after a second review: the portability patch, the P0
  measurements, the P2-42 increment and the second recovery slice (whose first repair
  was refused in turn, for a test it had not run, and repaired by a test-only run).
* **What did not change.** P2 is not closed, the board still reads `TECHNICALLY_STABLE_READY = no` `[V13]`
  and was not regenerated, no real reboot (DS2) has been run, no coding-agent session
  has been shown to use `hoh agent-setup`, and nothing was run on a real NixOS or
  macOS host.

## 16. Update after v0.3.0a2 (added 2026-10-09, ledger O261)

The preceding section is the update for the cut of `v0.3.0a2` and stays as written.
Since the cut one more run has been merged, the third slice of the disk-full-safe-writes
closure, and the register has been read again. The figures marked **R** were taken
again at the base named in `paper/NUMBERS_V2.md`; the value of section 15 or of
the narrative stands in parentheses.

* **Disk-full safety (§13 and §15).** The statement of section 15 that two more
  slices are merged and leave the row PARTIAL no longer holds: the third and last
  slice is merged and the register reads P2-41 PROVEN. Read it as narrowly as the
  register note scopes it: the matrix measures an import statement, the writer scan
  is lexical, the storage modules keep their own temp-fsync-publish helpers, which
  are tested for a full disk to different degrees, and a long unattended run through
  a full disk is not claimed. "12 writers remain" `[V44]` is the figure of the first
  alpha and is not re-derived.
* **The register and the gate.** The register holds 227 requirements, 60 of them
  PROVEN and 10 PARTIAL `[V10]` **R** (59 and 11 at the cut); 33 of P2's
  requirements read PROVEN `[V10c]` **R** (32 at the cut; 26 as measured on
  2026-10-07); the P2 gate has 10 requirements blocking and open `[V12]` **R** (11
  at the cut).
* **The honest ratio.** The attribution check counts 195 nodes after its anchor
  `[V06]` **R** (189 at the cut), of which one `[V06a]` ran through the shipped
  control plane end to end, 92 `[V06b]` **R** were developed by the product and merged
  by the orchestrating session after review (91 at the cut), and 102 `[V06c]` **R**
  are the orchestrating session's direct work (97 at the cut). The ratio is
  unchanged in kind: the merge decision stays with the orchestrating session.

## 17. Update after v0.3.0a2, second part (added 2026-10-09, ledger O262)

The preceding section and the sections before it stay as written. Since that section two more product runs have been merged
(the park binding of a failed provider dispatch is enforced on every automatic resume, and a developer turn that ends
on a provider's limit screen is a failed dispatch of its class), a sixth amendment has been registered, and the
register has been read again. The figures marked **R** were taken again at the base named in
`paper/NUMBERS_V2.md`; the value of the preceding section stands in parentheses.

* **Provider usage limits (§12 and §13).** The statement of section 15 that P2-43 reads MISSING and that its third
  slice is not merged no longer holds: the register reads P2-43 PROVEN `[V40]` **R**. Read it as narrowly as the
  register note scopes it. The matrix measures a symbol, a collected test and an import statement, not an execution.
  Only the recorded shapes of the provider's banner are read, whole, and no developer pane has ever been recorded on a
  real limit; no real rate-limit body was recorded either, so that branch is driven by synthetic text. A short good
  re-run after a wait is parked again and again until the resume limit and is then handed to a person; the fix is
  specified and in a run, and not merged. A live run that meets a real provider limit is NOT_RUN.
* **The register and the gate.** The register holds 235 requirements, 61 of them PROVEN and 10 PARTIAL `[V10]` **R**
  (227, 60 and 10 in section 16; the difference in the total is the eight requirements of the new amendment, registered
  and MISSING, planned and not built); 34 of P2's requirements read PROVEN `[V10c]` **R** (33); the P2 gate has 9
  requirements blocking and open `[V12]` **R** (10), with 174 open requirements, 157 of them behind a phase gate; of
  the requirements registered for P3 to P12, 158 of 176 read MISSING `[V11b]` **R** (150 of 168). The new amendment adds
  none to the P2 gate; its eight requirements belong to the gate of P5.
* **The honest ratio.** The attribution check counts 204 nodes after its anchor `[V06]` **R** (195), of which one
  `[V06a]` ran through the shipped control plane end to end, 94 `[V06b]` **R** were developed by the product and merged
  by the orchestrating session after review (92), and 109 `[V06c]` **R** are the orchestrating session's direct work
  (102). The ratio is unchanged in kind: the merge decision stays with the orchestrating session.
* **What the new amendment is.** It registers eight capabilities of the orchestration layer that this build ran by hand
  (a merge gate, the start of review pairs and drafters, pins and imports, closing a run's own terminal tabs, a per-run
  storage account, a scoped reclaim, a detached run, an event wait) as planned work. None is built. The finding that
  prompted it is a limit of the harness as used: finished runs read as active while idle shells of their launcher tabs
  stayed open, and storage had to be scanned because nothing accounted it per run.
