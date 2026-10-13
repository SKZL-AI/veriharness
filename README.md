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

**State on 2026-10-09 (added; the opening paragraph above is unchanged).** The latest stable release is `v0.1.0`. The
current pre-release is `v0.3.0a3` (it was `v0.3.0a2` on 2026-10-09; superseded on 2026-10-13 by the release `v0.3.0`, see the row *Release* below and *State on 2026-10-13, release documents*), an alpha and a research preview, installed from the tagged
source and not published to PyPI. The development line (phase P2) is in progress: 34 of its 43 registered requirements read PROVEN `[V10c]` `[V10b]`,
and 9 blocking items of its gate are open `[V12]`. The line has been developed under its own orchestration since
2026-09-07, with 69 product-run merges since 2026-09-25 `[V01]`, by an agent-orchestrated loop that an operator directs
and that stopped several times, over 32 days 15 hours `[V46]`; the account, the figures and the interruptions are under
*Duration, autonomy and scale of the build* below, and the parts and their states are under *The parts, and what each
has and has not shown*.

**State on 2026-10-13 (added; the paragraphs above are unchanged).** Phase P2 is closed at the commit of the records entry O268 of `DOGFOOD_LEDGER.md`,
the closure record that amendment A7 (the phase-closure amendment of the production line, pinned by digest in the programme's amendments file) names. That record names the sealing commit `c4dc643d`, the
succession capsule committed alone at `642a4fa9`, the independent supervisor review `20261013T093422Z-S` (a separate session on a read-only worktree; every
finding it inherited answered NO_ACTION, none blocking, the review complete), the checklist snapshot by its seal, the committed capability matrix by its digest
with every requirement of P2 PROVEN, and the durability evidence taken on `e0d9bdff` with an empty diff of the relevant source paths since. The closure is a
statement about that commit and about nothing later: a later change to the source creates regression obligations under A7 (the eight-minute durability run at
every records and release checkpoint, the reboot evidence at release checkpoints), not a re-opened phase, and an obligation that fails re-opens P2 by a records
entry. The limits of the evidence stand as `docs/LIMITATIONS.md` states them: one restart of a virtual machine on one host with fixture role runs, the checks of
the reboot scenario undiscriminated by any negative control, the approval-resume of a registered run not exercised. The readiness board still reads
`TECHNICALLY_STABLE_READY = no`: a closed phase is not a stable release. The release documents and the figures of this file are re-taken in the commits that
follow the closure record, before the tag `v0.3.0`; until then the dated sentences below that say P2 is open, in progress or not closed describe the state at
their dates and are superseded by this paragraph.

**State on 2026-10-13, release documents of `v0.3.0` (added; the paragraphs above are unchanged).** The release `v0.3.0` is cut
from the merge of its release documents after their review, behind the closure record of P2 (the paragraph above) and the regenerated figures
that follow it: `pyproject.toml` and `CITATION.cff` carry `0.3.0`, `RELEASE_NOTES.md` opens with its account, `CHANGELOG.md` carries the
section generated from it, and `.github/releases/v0.3.0.json` names the title and the source of the release text. It is a release, not a
pre-release, and it is not a readiness claim: the board reads `TECHNICALLY_STABLE_READY = no`, a closed phase is not a stable release, and
`v0.1.0` stays the only release on PyPI. The release changes no source file: every merge since the third alpha is records, tools, tests, the
register notes of three P2 rows (P2-07, P2-28 and P2-42, by addition) or the version line of `pyproject.toml`, and the diff of `src/hoh/`, the continuation unit, the durability harness and its fixture from `e0d9bdff` to the release commit prints nothing,
so the durability evidence of the closure is about the code the release ships; the operator ruled on 2026-10-12 that the tag therefore needs
no new reboot, and the eight-minute durability run at the tag is the orchestrating session's regression obligation under amendment A7, recorded
in the ledger entry of the tag. The sentences of this file that name `v0.3.0a3` as the current pre-release describe their dates.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again for the release `v0.3.0` at the commit `f78d1cb9`
(2026-10-13 at 19:29 +0300; the figures quoted in the updates above stay as written). At that base the register holds 235 requirements, 70 PROVEN
and 9 PARTIAL `[V10]`; all 43 requirements of P2 read PROVEN `[V10c]` `[V10b]`, and the P2 gate reads 0 blocking open; 0 exit conditions bound to requirements not PROVEN; 0 exit conditions nothing measures yet; 165 open requirements, 125 behind a phase gate `[V12]`; of
those registered for P3 to P12, 156 of 176 MISSING (9 PARTIAL, 11 PROVEN) `[V11b]`. The history is 1,260 commits, 182 of them merges `[V48]`; the first and the
latest commit of the mainline are 36 days 19 hours apart `[V46]` and 27 of the 37 calendar days carry a commit `[V47]`; the ledger runs to O268 with 240
numbered entries `[V49]`. The development line has 97 product-run merges since 2026-09-25 `[V01]`, 78 of which change `src/hoh/` `[V02]`, as
at the third alpha: the four product runs merged since carry titles that the counting tool does not list, and the release changes no tool. Of
320 development nodes after the attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 131 were developed by a
product run and merged by the orchestrating session after review `[V06b]`, and 188 were written by the orchestrating session directly `[V06c]`.

## Where the project stands: release, pre-release, development, roadmap

Four things are easy to run together here, and this repository keeps them
apart on purpose.

| Track | State | What it means for a reader |
|---|---|---|
| **Latest stable release** | `v0.1.0`, tagged 2026-09-14, on PyPI since 2026-09-15 | The only stable release. The campaign figures, the benchmark and the findings further down this file describe the work that produced it, and they stay as they were measured. |
| **Pre-release** | `v0.3.0a1` -- **PRE-RELEASE**, an alpha and a research preview, **prepared, not yet cut** | Prepared at a documentation checkpoint, not at the end of a phase. Its integrity gate is met for the paths its reviewers replayed (the qaw-followup merge `ab41589f` closed the three findings that had held it; one residual is stated in `docs/LIMITATIONS.md`), and the cut waits for the operator's decision on the exact commit. Once cut, it is installed from the tagged source and is not published to PyPI: the distribution workflow of this repository accepts only the audited `v0.1.0`. `pyproject.toml` carries `0.3.0a1`. `RELEASE_NOTES.md` opens with its account; its open gates are listed as NOT_YET_PROVEN there and in `docs/LIMITATIONS.md`. (Correction, 2026-10-09: `v0.3.0a1` was cut on 2026-10-08; "prepared, not yet cut" and "the cut waits for the operator's decision" are the text of the checkpoint before the cut and are kept here. `pyproject.toml` now carries the next pre-release, in the next row.) |
| **Newer pre-release** | `v0.3.0a2` -- **PRE-RELEASE**, an alpha and a research preview, tagged `v0.3.0a2` | Prepared at a documentation checkpoint after the ledger entries O256 to O260, not at the end of a phase. It adds a NixOS-first portability patch, `hoh agent-setup`, owned control of the agents HoH starts, more disk-full-safe writers, a classification of failed provider dispatches with its provenance and park binding, `hoh migrate` and `hoh decision check`, and the five P0 measurements; none of it was run on a real NixOS or macOS host or in a live coding-agent session. It is installed from the tagged source and is not published to PyPI. `pyproject.toml` carries `0.3.0a2`. `RELEASE_NOTES.md` opens with its account; its open gates are listed as NOT_YET_PROVEN there and in `docs/LIMITATIONS.md`. |
| **Newest pre-release** | `v0.3.0a3` -- **PRE-RELEASE**, an alpha and a research preview, tagged `v0.3.0a3` | Prepared at a documentation checkpoint after the ledger entries O261 to O265, not at the end of a phase. It adds the merge gate as a command, `hoh wait`, `hoh surface`, `hoh run --detach`, `hoh pin` and `hoh import`, the per-run storage account, target registration, and a rotation policy under which the product hands the orchestrating session over by itself; P2-41, P2-43, P2-14 and P2-20 read PROVEN, each with limits that `RELEASE_NOTES.md` states. It is installed from the tagged source and is not published to PyPI. `pyproject.toml` carries `0.3.0a3`. P2 is not closed; its open gates are listed as NOT_YET_PROVEN in `RELEASE_NOTES.md` and in `docs/LIMITATIONS.md`. |
| **Release** | `v0.3.0` -- the release at which phase P2 is closed, tagged `v0.3.0` | Cut from the merge of its release documents after their review, behind the closure record of P2 (records entry O268, 2026-10-13) and the regenerated figures that follow it. It is not a pre-release. It changes no source file against the third alpha: every merge since `v0.3.0a3` is records, tools, tests, the register notes of three P2 rows or the version line of `pyproject.toml`, and the durability evidence of the closure is about the code it ships. It is installed from the tagged source and is not published to PyPI (the distribution workflow accepts only `v0.1.0`). `pyproject.toml` carries `0.3.0`. Readiness is not claimed: the board reads `TECHNICALLY_STABLE_READY = no`, and `RELEASE_NOTES.md` opens with the account of the closure, its limits and the rows of the board that do not read PASS. |
| **Current development** | Phase P2, *v0.3 Durable Program Runtime + Autonomy Contract Base* -- **IN PROGRESS** | Not closed. The pre-release is a snapshot of this line, and a clone of the default branch may be ahead of the tag. (Closed at the commit of records entry O268, 2026-10-13; see *State on 2026-10-13* above. The earlier text of this row stays as written.) |
| **Long-term roadmap** | Phases P0 to P12, v0.2 through v2.0 | A registered plan, not a promise. P1 was completed as an internal development milestone. No public v0.2 release was cut; the latest stable public release remains v0.1.0 while the development line continues through P2. Of the requirements registered for P3 to P12, 150 of 168 read MISSING `[V11b]`; ten of the 11 that read PROVEN are storage-lifecycle items that amendment A1 placed there and that were built early. (Updated 2026-10-09 at the O262 base: 158 of 176 read MISSING `[V11b]`, because amendment A6 registered eight more requirements for phase P5; see *The parts, and what each has and has not shown*.) |

The board that says what holds today is `docs/READINESS.md`; at its last
committed measurement it reads `TECHNICALLY_STABLE_READY = no`, and that holds
for the pre-release too: `v0.3.0a1` is not a stable release and does not claim
the board's conjunction. `docs/READINESS.md` is measured at commit `75214e31`, not
at the commit that carries the pre-release: it is conservative (its `attribution` row
and its `tests` row predate the entries and the run that now close them), and it is
not regenerated at the cut, because regenerating it needs the network (the
`clean_install` row and the union gate's wheel build call the package index).
(Correction, 2026-10-08, of the text published with the pre-release tag: "close them" is inexact for the `tests` row. The last suite run of the checkpoint still had failed tests (two environment failures of the scratch placement and one artefact of that run's own PATH), so the row is stale and conservative, not closed. The earlier wording is kept and corrected here; the newest ledger entry has the figures.)
Figures below that carry a bracketed tag are listed,
with their source and the command that regenerates them, in
`paper/NUMBERS_V2.md`.

### Duration, autonomy and scale of the build, measured on 2026-10-09

*Added 2026-10-09; nothing above or below this block was edited.* The campaign figures further down this file,
including their wall-clock row (five calendar days, three and a half days elapsed), are the measurement of the
`v0.1.0` campaign, taken when that release was tagged on 2026-09-14. They are correct for that campaign, they stay
where they are, and they are not the age or the size of the project. The same kinds of figure for the whole
development line follow, at the base commit of `paper/NUMBERS_V2.md` (the O262 records commit, committed on 2026-10-09)
unless a row says it was counted later.

| What | Figure |
|---|---|
| Span | First commit 2026-09-07, latest commit 2026-10-09: 32 days 15 hours, four weeks and four days `[V46]` |
| Days with a commit | 23 of the 33 calendar days; no commit on 2026-09-15 to 2026-09-20 or on 2026-09-26 to 2026-09-29; a commit on each of the 10 days from 2026-09-30 to 2026-10-09 `[V47]` |
| History | 1,088 commits, 144 of them merges `[V48]`; 234 numbered ledger entries, O23 to O262 `[V49]` |
| Product runs | 69 product-run merges since 2026-09-25 `[V01]`; 117 run directories from 2026-10-01 on, counted on 2026-10-09 `[V50]` |
| Independent review | 459 first-round reviewer reports (771 report files in all, including re-review rounds) since 2026-09-30, counted on 2026-10-09 `[V51]` |
| Who wrote the code | Of 204 development nodes after the attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 94 were developed by a product run and merged by the orchestrating session after review `[V06b]`, and 109 were written by the orchestrating session directly `[V06c]`. The attribution file books none to a human, category `CAPTAIN` `[V52]`; it is declared by the orchestrating session and checked for tiling and for the run states it cites, not for authorship |
| Direction | 6 operator directives stored verbatim, 2026-09-30 to 2026-10-09 `[V53]` |
| Orchestrator sessions | 4 in a row since the written hand-over began on 2026-10-07, with 3 acknowledged hand-overs `[V54]` |

**What "autonomous" means here, and what it does not.** The orchestrating session is an agent session (a Claude Code
session) that drives VeriHarness through its own command line. In this build it wrote the specifications, started the
product runs, had every product-run candidate reviewed by two independent reviewer agents before its merge (from
2026-09-25 on), ran the global gates, and kept the ledger and these documents. The operator set the direction and
added work as it went (the directives above and the day-to-day items), approved, or delegated for a stated window,
every irreversible external action such as publishing a release, decided the storage and policy questions that only a
person may decide (which folders may be deleted, for one), and shut WSL down once on purpose to compact the disk. That
is *orchestrated*, not *unattended*: the operator was reachable and was asked, and the line also stopped, as the next
list names. The product's own runs carried the first two groups of the *Who wrote the code* row; the third, the
larger group by count of nodes, the orchestrating session wrote directly.

**Interruptions, named.** The line did not run without stopping:

* No commit from 2026-09-15 to 2026-09-20, after the `v0.1.0` tag (the publication to PyPI on 2026-09-15 is not a
  commit); the ledger gives no reason for the pause.
* A power cut on 2026-09-10 (ledger entry O88): the project content was re-measured and found intact, and some
  operator tools kept in volatile storage were lost `[V56]`.
* The disk under the development machine filled on 2026-09-25 (977 GiB of a 1 TiB volume) `[V16]`; no commit from
  2026-09-26 to 2026-09-29, and work resumed on 2026-09-30 under the storage lifecycle that the operator directed.
* Provider quota: 13 stops between 2026-10-03 and 2026-10-07 `[V55]`, one of them the weekly limit on 2026-10-06, which
  the operator reset. The product now classifies a provider limit it can read as a failed dispatch rather than a
  rejected iteration (P2-43, whose limits are stated in `docs/LIMITATIONS.md`).
* Inode exhaustion on 2026-10-05 (67,107,099 of 67,108,864 inodes used) `[V35]`. On 2026-10-09 it was inode pressure, not
  exhaustion: the product's storage guard reached CRITICAL on the inode axis and parked two runs until the operator's
  deletions allowed the product's own resume `[V56]`.
* An operator-requested WSL shutdown and disk compaction on 2026-10-08, about one hour `[V56]`.

**What this does and does not show.** It shows that an agent-orchestrated build, with independent review of its
product-run candidates before their merge, kept a repository moving for more than four weeks of calendar time, through
quota stops and storage blocks, and that there was a commit on each of the last ten days of the measurement. It does
not show unattended operation: a person directed the work, was reachable and answered. It does not show continuous
running: there was no commit on ten of the 33 calendar days `[V47]`. It does not show that the product's own durable
runtime kept the build alive: the five exit conditions P2-33 to P2-37 read IMPLEMENTED_NOT_PROVEN, crash and reboot
recovery (P2-11) has never been exercised on a real reboot, and the orchestrating session was handed over by files the
outgoing session wrote, not by the product's rollover protocol, which has not run live (P2-14 reads PARTIAL).
`docs/LIMITATIONS.md`, limit 1, keeps the full account.

## What changed after v0.1.0

Stated against the development repository at the commit the checkpoint was
prepared from. `paper/POSITION_PAPER_V2.md` gives the full account.

* **HoH now builds HoH's own kernel.** The v0.1.0 statement further down --
  no run wrote HoH's own logic -- is true of that campaign and stays in place.
  Since 2026-09-23, product runs have written kernel modules under `src/hoh/`.
  From 2026-09-25 on, every such run worked in an isolated clone pinned to one
  commit, its accepted candidate was imported byte for byte, and two
  independent integration reviewers released it before it was merged: 55 of
  the 69 product-run merges between 2026-09-25 and 2026-10-09 change
  `src/hoh/` `[V02]` `[V01]` (34 of 46 as measured on 2026-10-07 for the first alpha, which was tagged 2026-10-08).
* **Acceptance is not merge.** A product run writes candidate bytes; the
  product's own QA and receipts decide acceptance; independent integration
  review decides whether accepted bytes may be merged; the orchestrating
  session holds the merge authority; and a phase closes only when its gate,
  derived from the capability register, says so. The invariant, in one line:
  `candidate creation != product acceptance != integration acceptance != merge authority != phase closure`.
  Each step has refused what the step before it passed: in the development
  ledger's entries O236 to O254, 26 candidates the product had accepted were
  refused at integration review `[V07t]`.
* **A durable programme runtime (P2).** Durable timers, a sentinel and a
  never-stuck check; session succession with a master epoch and a leader lease
  (partial at the first alpha; P2-09 reads PROVEN since 2026-10-08, under the
  limits `RELEASE_NOTES.md` states); a resume loop that acts on due timers; an
  autonomy contract; and a programme checklist whose phase gate an independent
  supervisor review has to pass. 32 of P2's 43 registered requirements read
  PROVEN `[V10c]` `[V10b]` (26 as measured on 2026-10-07) [dated figures, added 2026-10-09: 26 of 43 on 2026-10-07 for the
  first alpha, 32 at the `v0.3.0a2` checkpoint, 34 at the O262 base on 2026-10-09, after the lifts of P2-41 and P2-43],
  and P2 is **not** closed.
* **The orchestrating session can be rolled over under a protocol (Layer A,
  merged, not yet exercised live).** Four commands (`hoh project hold`,
  `rollover`, `rollover-verify`, `rollover-accept`) freeze a capsule whose
  entries are derived facts the successor must re-derive, the operator's
  decisions kept verbatim and never answered by the machine, and advisory
  context with no authority; a successor takes the master epoch only if
  everything re-derives, and a stale predecessor is fenced. It has been tested
  with fakes; no live succession has been run, so **no P2 row is proven by it**,
  and the one handoff of 2026-10-07 (by hand, before the protocol existed) is
  not that evidence. See `docs/OPERATIONS.md` and `docs/LIMITATIONS.md`.
* **HoH closes its own role panes and ends a role's processes before the
  freeze (merged).** The role-pane lifecycle never touches a foreign pane or
  process, and refuses to close a pane whose role left marked processes outside
  it; its first live use closed the planner and developer panes at turn end and,
  for the QA pane, refused the release because two marked processes had escaped
  it (named, never signalled); the QA pane was gone without anyone closing it by hand.
  (Correction, 2026-10-08: the lifecycle log of that run, `pane-lifecycle.jsonl`, also holds a `closing` and a `closed` line for the QA pane, in the same second as the `refused` line; the earlier "closed three panes and refused one" counted that pane twice, and the text above replaced it.) (Further correction, 2026-10-08: the `closing` line is written by HoH before its own close command and the `closed` line only after a second snapshot no longer lists the pane, and a pane found already gone is written with an `already_gone` field, which this line does not carry (`_close_own_pane` in `src/hoh/dispatchers.py`); so HoH itself closed the QA pane's tab, nobody closed it by hand, and the `refused` line that follows names the two escaped processes, not a failed close.)
* **Every long role prompt goes through a pointer file (merged).** A prompt
  above 8 KiB, or one holding a character an input box treats specially, is
  written to a file and the agent gets a short pointer, so the stalled-submit
  recovery can verify what is in the box for every prompt. The first prompt
  into a fresh pane still stalls; only its recovery works for every prompt.
* **A storage lifecycle, after a real storage incident.** An ownership
  registry, admission by reservation, a durable park and resume under storage
  pressure, disk-full-safe durable writes (partial), verified archive bundles,
  registered ephemeral temporary roots, and `VERIFIED_RELOCATION` -- the one
  operation in VeriHarness that may remove a local copy of project data, and
  only after the archive holding it has been verified. Its first real
  self-dogfood ran on the project itself: the project was adopted, the survey
  of legacy reviewer scratch found 0 of 188 roots eligible `[V33]`, and the
  steward's real pass observed only and reclaimed nothing.
* **Failures the product found in itself.** A capability witness that crashed
  on a file name that is not UTF-8; role prompts longer than one command-line
  argument may be (`MAX_ARG_STRLEN`); an operator-side relaxation that
  silently weakened the shipped command guard; a dispatcher that read an agent
  ending its turn, while its own background job still ran, as finished; a QA
  role whose provider usage limit was booked as a rejected iteration, eight
  times `[V26]`; a run held for a capability violation that could be reported
  ACCEPTED, repaired by the reviewed qa-arena-write run for the states the
  current build writes and, for the three paths that run's reviewer then proved
  open (a legacy state, an open seal, a symlinked seal ledger) and a forged
  sibling-arena plan line, by the qaw-followup run; and evidence that went
  stale when the code it described changed.
  What was repaired, how, and what is still open is in `docs/LIMITATIONS.md`.

**What is not true yet, stated as plainly as the rest.** P2 is open. Its exit
conditions P2-33 to P2-37 were once proven on end-to-end durability evidence,
and that evidence went stale when the code changed; they read
IMPLEMENTED_NOT_PROVEN until it is taken again. P2-11, crash and reboot
recovery, has never been proven: a real reboot (stressor DS2) has not been
run. Disk-full-safe writes cover only part of the writers (P2-41, PARTIAL).
**The pre-release's integrity gate is met for the paths its reviewers
replayed, and not claimed beyond them**: the qaw-followup merge closed the
legacy-state, open-seal, symlinked-ledger and sibling-plan paths, each replayed
by both integration reviewers on the merged tree, and the strand states one
residual itself -- a role pane that has read the run's `arena_secret` and
copies iteration and attempt. **The repair of the quota misclassification is
not merged**: at this pre-release a provider usage limit that stops a QA role
is still booked as a rejected iteration, and the repair (failure-classes, the
core of amendment A5's P2 item P2-43) reads MISSING `[V40]`. **The first prompt
after an agent starts can still be typed and not submitted**; dispatch
recovers from it for every prompt, which is not a fix of the race, and the
recovery refuses (fails closed) when Claude Code word-wraps the pointer line in
a narrow pane: a 79-column pane blocked one run before any candidate, and the
readiness fix is the next P2 strand. A role's own background task is now ended
before the freeze by the merged role-pane lifecycle, which has had one live use
and carries tracked minor survivors. **Context rollover is built and not
exercised**: no live succession has run, the draining state is recorded and not
enforced, and five follow-up items are open (and nine guard rules have no test that kills their removal; corrected 2026-10-08 from "three", the count RELEASE_NOTES and LIMITATIONS list). The storage steward has not reclaimed anything of
VeriHarness's own data. The exact-head external CI, the benchmark freeze and
the programme supervisor's review of P2 all read red on the board. The full
list is *Current open limitations* at the top of `docs/LIMITATIONS.md`.

**Update, 2026-10-09, for `v0.3.0a2`.** The paragraph above is the text of the
first alpha. Since then the ledger entries O256 to O260 changed these
statements, and `RELEASE_NOTES.md` has the account and every limit:

* The first two slices of the quota repair are merged: a failed provider dispatch
  is classified, QA re-raises a provider failure instead of booking it as a
  rejected iteration, every dispatch records its provenance, and every park is
  bound to what it stands on. The binding is written and not yet checked on
  resume. P2-43 still reads MISSING `[V40]`; the third slice is not merged.
* First-prompt readiness is merged (a bounded wait before the first prompt, and a
  model of the box's word wrap); whether it cures the stall is SUSPECTED until a
  live check, so "the readiness fix is the next P2 strand" no longer holds.
* The follow-up items of the context rollover are fixed in code, the launcher can
  start its successor, and the acquire can be conditional (P2-09 reads PROVEN).
  It has still not run live.
* More writers are disk-full safe; P2-41 is PARTIAL still.
* New: `hoh agent-setup`, `hoh migrate`, `hoh decision check`, owned agent control
  (P2-17 reads PROVEN, with limits), the NixOS-first portability patch (never run
  on a real NixOS or macOS host), and the five P0 measurements.

**Update, 2026-10-09, after `v0.3.0a2` (ledger entry O261).** Two statements
above no longer hold on the development line: "Disk-full-safe writes cover only
part of the writers (P2-41, PARTIAL)" and "More writers are disk-full safe; P2-41
is PARTIAL still". The third and last slice of the P2-41 closure is merged, and
the capability matrix reads P2-41 PROVEN. Read that as narrowly as it is scoped:
the writers of run, project and receipt state that the writer scan lists as
migrated go through one disk-full-safe write primitive (`durable_write`,
`durable_create`, `durable_append`), and the scan fails on any raw write call it
does not classify; the storage modules keep their own temp-fsync-publish helpers
(the scan's `OWN_DURABLE` class), which are tested for a full disk to different
degrees, three of the eight functions not at all; the scan does not see aliased
calls; the matrix measures an import statement, not an execution; and a long
unattended run that survives a full disk end to end is not claimed. The limits
are in the register note of P2-41 and in the ledger entry O261. Nothing above
this paragraph was edited.

**Update, 2026-10-09, after `v0.3.0a2` (ledger entry O262).** Two statements above no longer hold on the
development line, or were worded too narrowly; nothing above this paragraph was edited. First, the previous paragraph says
that three of the eight functions that keep their own temp-fsync-publish helper are not tested for a full disk at all;
the count is four, because the registry writer has a test of a failed replace and none of a full disk, as
`docs/LIMITATIONS.md` already says. Second, the list of the pre-release says that P2-43 still reads MISSING and that its
third slice is not merged. The third slice (the park binding is enforced on every automatic resume) and a fourth (a
developer turn that ends on a provider's usage- or rate-limit screen is a failed dispatch of its class, not a finished
turn) are merged, and the capability matrix reads P2-43 PROVEN. Read that as narrowly as it is scoped: the matrix
measures a symbol, a collected test and an import statement, not an execution; only the recorded shapes of the provider's
banner are read, whole, and no developer pane has ever been recorded on a real limit; a short good re-run after a wait is
parked again and again until the resume limit and is then handed to a person, and the fix is specified and not merged;
no real rate-limit body was ever recorded; a live run that meets a real provider limit is NOT_RUN. The limits are in the
register note of P2-43 and in the ledger entry O262.

**Update, later on 2026-10-09 (ledger entry O264).** The previous update says that the fix for the stale-banner re-park of P2-43 is
specified and not merged. It is merged: a limit banner that was already on the developer's pane when the prompt was sent is not
read as this turn's. That bounds the problem and does not remove it everywhere: a status-area line that a healed pane left in
its scrollback is still read, a repeated rate-limit body after a long turn is missed, and no developer pane has been recorded on
a real limit. Five further pieces are merged on the development line and are not in `v0.3.0a2`. Ephemeral temporary roots are used
by `hoh run` for a run of a registered project (P2-42 stays PARTIAL, because the role scratch is not a registered root).
`hoh merge-gate` checks a set of review reports and verifies a receipt; no merge step of the product calls it. `hoh wait` waits
on a run and on nothing else. `hoh surface` records the tabs of a run and closes the ones of a run that is over once the shell
in them is proven idle. Criterion fidelity refuses a plan that drops, re-types, wraps or deselects a declared criterion, and
holds only for a specification that declares its criteria in one block. The capability matrix reads P5-36 and P5-39 PARTIAL and
P5-43 MISSING, and lifts no requirement to PROVEN. None of this has run against a live Herdr session. The limits are in the
register notes and in `docs/LIMITATIONS.md`.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit of the records entry O264
(`dff02f08`, committed on 2026-10-09 at 22:14 +0300); the table in the section above, which names the O262 records commit as its
base, keeps its figures as written. At the new base the history is 1,130 commits, 153 of them merges `[V48]`, and 236 numbered
ledger entries, O23 to O264 `[V49]`. Of 233 development nodes after the attribution anchor `[V06]`, 1 went through the shipped
control plane end to end `[V06a]`, 104 were developed by a product run and merged by the orchestrating session after review
`[V06b]`, and 128 were written by the orchestrating session directly `[V06c]`. Of the requirements registered for P3 to P12, 156 of
176 read MISSING, 9 read PARTIAL and 11 read PROVEN `[V11b]`.

**Update, 2026-10-11 (ledger entry O265).** Sixteen further merges are on the development line after the update above and are not in
`v0.3.0a2`. Two sentences of that update are out of date and are corrected here by addition. `hoh wait` no longer waits on a run and
on nothing else: it also waits on a review report (`--report`, with the footer check and the stable-age rule of the merge gate) and on
a process id (`--pid`); a durable list of what it reported and a trigger of the reaper are not built, and the register still reads
P5-43 MISSING. The scratch of the roles of a run of a registered project now lives below the iteration's registered workspace, and
`hoh storage new-target` and `hoh storage register-project` register a target clone; P2-42 still reads PARTIAL, because no run of a
registered project has been observed and nothing has run against a live Herdr session. The product now rotates the orchestrating
session by itself under a policy and creates the governed project record (`hoh project create`); that was tested with fakes, P2-14
still reads PARTIAL and is held until a live rotation has run. Also merged: `hoh run --detach`, `hoh pin` and `hoh import`, the
per-run storage account `hoh storage account`, a guarded read of the review reports for the merge gate (the product command has been
the gate of record since `1e633e46`), the export leak scan for private identifiers, the refusal of run ids and project ids that are
not plain names, and the amendment A7, which records that a phase is closed at a commit and that later changes to the source create
regression obligations. No requirement was lifted to PROVEN. The limits are in the register notes and in `docs/LIMITATIONS.md`.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit of the records entry O265
(`d88281da`, committed on 2026-10-11 at 03:44 +0300); the paragraph above that names the O264 records commit as its base keeps its figures as written. At the new base the
history is 1,197 commits, 170 of them merges `[V48]`, and 237 numbered ledger entries, O23 to O265 `[V49]`; the first and the
latest commit of the mainline are 34 days 3 hours apart `[V46]` and 25 of the 35 calendar days carry a commit `[V47]`. Of 281 development
nodes after the attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 125 were developed by a
product run and merged by the orchestrating session after review `[V06b]`, and 155 were written by the orchestrating session
directly `[V06c]`.

**Update, 2026-10-11, later (ledger entry O265, second revision).** The first rotation of the orchestrating session by the product has run, as
the canary of amendment A7: the policy asked for the rotation at its threshold, the master wrote its handoff, the successor verified the
frozen capsule against the world and took the lease as the second epoch, and the old session was fenced. On that evidence P2-14 and
P2-20 read PROVEN, and three sentences above are out of date and are corrected here by addition: the sentence in "Duration, autonomy
and scale of the build" that the session was handed over by files and not by the product's rollover protocol, "which has not run
live"; the row "Handing the orchestrating session over" of the table, which says "tested with fakes; no live succession has run";
and the paragraph of this entry that holds P2-14 at PARTIAL until a live rotation has run. What the evidence is not: one rotation, on
one host, of one role, with one successor model; the paths of expiry and error and a second rotation were not run live, the
capsule input of the canary was static and missed the newest run root (tracked), P2-11 and P2-33 to P2-37 are unchanged, and the gate
of P2 is still shut. P2-42 still reads PARTIAL. The strand `rlname`, merged before the canary, fixed the defect that the first
rehearsal found (the name of the successor's agent was refused by Herdr); it lifts no requirement.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit of the second revision of the records entry
O265 (`29852843`, committed on 2026-10-11 at 04:21 +0300). At that base the register holds 235 requirements, 63 PROVEN and
10 PARTIAL `[V10]`; of the requirements of P2, 36 PROVEN `[V10c]`; of those registered for P3 to P12, 156 of 176 MISSING (9 PARTIAL, 11 PROVEN) `[V11b]`. The history is
1,204 commits, 171 of them merges `[V48]`, and 237 numbered ledger entries, O23 to O265 `[V49]`. Of 285 development nodes after
the attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 126 were developed by a product run and merged by the
orchestrating session after review `[V06b]`, and 158 were written by the orchestrating session directly `[V06c]`.

**Update, 2026-10-11, third revision of the records entry O265 (review findings folded by addition).** Three limits and three dated facts
qualify the paragraph above. First, the canary exercised the rotation of the session (the policy, the controller, the rollover); the item-level
context rotation that the probe of P2-14 names (`rotate` in `src/hoh/rotation.py` and `reachable`) has no caller outside the tests, so
the status PROVEN of P2-14 is a statement about the rotation of the session and the sentence of the register note that nothing rotates items
yet stays true. Second, where this README says that a second rotation was not run live, that was true when it was written; a second
rotation by the product completed later the same day (successor epoch three, outcome COMPLETE); it is recorded in the closure records and
is not counted here. Third, three further sentences of this README say that no succession has run and are out of date in the same way as
the three named above, all three in the section "What changed after v0.1.0": the paragraph on the rollover ("It has been tested with
fakes; no live succession has been run, so no P2 row is proven by it"), the sentence on what is built and not exercised ("Context
rollover is built and not exercised: no live succession has run") and the list item on its follow-up fixes ("It has still not run
live"). They are kept as written; the canary paragraph above and the limits of this paragraph supersede them.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit of the third revision of the records entry O265
(`84384e1d`, committed on 2026-10-11 at 05:06 +0300). At that base the history is 1,207 commits, 171 of them merges `[V48]`; of 287 development nodes after the
attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 126 were developed by a product run and merged by the orchestrating
session after review `[V06b]`, and 160 were written by the orchestrating session directly `[V06c]`.

**Correction, 2026-10-11 (third revision of the records entry O265, second part).** The paragraph on the third revision above says of the second
rotation that it "is recorded in the closure records". That was wrong when it was set: no closure record exists yet (amendment A7 states
it, and the gate of P2 is shut). It is replaced by: the second rotation is to be recorded in the closure records, none of which exists
yet; it is observed in the canary root and in the orchestrator's ledger, and it is not counted here.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit of the second part of the third revision of the records
entry O265 (`d70a1add`, committed on 2026-10-11 at 05:26 +0300). At that base the history is 1,210 commits, 171 of them merges `[V48]`; of 289 development nodes
after the attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 126 were developed by a product run and merged by the
orchestrating session after review `[V06b]`, and 162 were written by the orchestrating session directly `[V06c]`.

**Update, 2026-10-11, for `v0.3.0a3` (release documentation).** The documentation of the third alpha is prepared for the
tag `v0.3.0a3`, which is cut from the merge of this package after its review: `pyproject.toml` and `CITATION.cff` carry `0.3.0a3`,
`RELEASE_NOTES.md` opens with its account, and `CHANGELOG.md` carries the section generated from it. The statement at the top of
this file that the current pre-release is `v0.3.0a3`, and the row of the table above, hold once that tag exists. The preparation
lifts no requirement and changes no source file; the lifts since the second alpha are those of the records entries O261, O262 and O265 (P2-41, P2-43,
P2-14 and P2-20 read PROVEN, each with the limits stated in `docs/LIMITATIONS.md`). P2 is not closed: P2-11 needs a real reboot
that has not been run, the durability evidence is not taken at this commit, and the hold on P2-42 stays. The statement of an
earlier update that no closure record exists yet is still true.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit `3517ce6f` (2026-10-11 at 07:24 +0300; the
figures quoted in the updates above stay as written). At that base the register holds 235 requirements, 63 PROVEN and 10 PARTIAL
`[V10]`; of the requirements of P2, 36 PROVEN `[V10c]`, and 7 blocking items of its gate are open `[V12]`; of those registered
for P3 to P12, 156 of 176 MISSING (9 PARTIAL, 11 PROVEN) `[V11b]`. The history is 1,217 commits, 173 of them merges `[V48]`;
the first and the latest commit of the mainline are 34 days 7 hours apart `[V46]` and 25 of the 35 calendar days carry a commit
`[V47]`. The development line has 97 product-run merges since 2026-09-25 `[V01]`, 78 of which change `src/hoh/` `[V02]` (69 and 55
before): the count lists the titles of the merges since the second alpha one by one. Of 294 development nodes after the
attribution anchor `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 126 were developed by a product run and
merged by the orchestrating session after review `[V06b]`, and 167 were written by the orchestrating session directly `[V06c]`.

**Update, 2026-10-11, after the operator's restart (records entry O266).** The durability freeze and the restart that the paragraphs above say are still to come
have taken place. The durability end-to-end run was made on a `git archive` of `e0d9bdff` (the last commit that changed `src/hoh/`): all seven scenarios ran, all
twenty negative controls were caught, and the reboot scenario was armed, the operator ran `wsl --shutdown` and started Herdr again, and the verification after the
restart found the lease holder on another boot, spent an epoch as RECOVERY and closed the project with the canonical tick. On that evidence P2-11 and P2-33 to P2-37
read PROVEN. The limits are in `docs/LIMITATIONS.md` and the note of each row: the restart was the shutdown of a virtual machine and not a power cut; one reboot,
one host, fixture role runs and the harness's own tick loop (no systemd); the record lists every check of the reboot scenario as one that no negative control
targets; and the evidence is about one commit, so any change under `src/hoh/` makes these rows read stale until the eight-minute run is repeated (amendment A7 makes
that a regression obligation). P2-42 reads PROVEN as well: the first run of a registered project was made (the strand `leakids2`, merged as `f956b119`, real Herdr
panes, every temporary workspace of the run found under the registered root by the readback); the approval-resume of a registered run (limit L2) did not occur in
it and is still not shown, and the other harnesses' temporary files are NOT_RUN. The product has rotated the orchestrating session six more times by its own
policy since the canary, took the lease again after the restart as a recovery and rotated once more from it. (Correction, 2026-10-12, records entry O267: the six rotations after the canary already include the one made from the recovered lease, so this sentence counts that rotation twice; at the commit of these figures the product's own log in the evidence copy holds seven finished rotations in all, the canary and six more, the last of them from epoch eight.) **P2 is still not closed.** The blocking requirements of its gate are all PROVEN,
and two exit conditions that the programme's own instruments measure are unmet: a current independent supervisor review (the preserved record is stale against the
matrix and the checklist snapshot and carries blocking findings) and the checklist consistency check, which refuses to close a phase over the nineteen
release-blocking findings that the preserved reviews of 2026-09-24 left unresolved. No closure record exists; one needs the review and that check first.

Sentences of this README that this update supersedes, kept as written: that the five exit conditions P2-33 to P2-37 read IMPLEMENTED_NOT_PROVEN and that
crash and reboot recovery (P2-11) has never been exercised on a real reboot (the paragraph on the build, and the section on what changed after v0.1.0); that P2-11
"has never been proven"; the row of the table on the exit conditions of P2; every statement that P2-42 reads PARTIAL or has no observed run of a registered
project; and, in the update for `v0.3.0a3`, "P2 is not closed: P2-11 needs a real reboot that has not been run". The dated paragraph headed "State on 2026-10-09"
names `v0.3.0a3` beside figures of that date (thirty-four PROVEN, nine blocking): the figures of that paragraph are those of 2026-10-09, and the figures of the day are in
the paragraphs below. The install note under *Installation* names the tags `v0.3.0a1` and `v0.3.0a2`; the same holds for the tag `v0.3.0a3`.

The figures of the moving present that `paper/NUMBERS_V2.md` derives were taken again at the commit of the records entry O266 (`cc79b2ae`, committed on 2026-10-11 at 16:01 +0300;
the figures quoted in the updates above stay as written). At that base the register holds 235 requirements, 70 PROVEN and
9 PARTIAL `[V10]`; of the requirements of P2, 43 PROVEN `[V10c]`, and the P2 gate reads 0 blocking open; 0 exit conditions bound to requirements not PROVEN; 0 exit conditions nothing measures yet; 165 open requirements, 157 behind a phase gate `[V12]` -- that line counts requirements and
exit conditions bound to them, not the two checklist-gate conditions named above, which are unmet: the gate is shut; of those registered for P3 to P12, 156 of 176 MISSING (9 PARTIAL, 11 PROVEN) `[V11b]`.
The history is 1,226 commits, 174 of them merges `[V48]`, and 238 numbered ledger entries, O23 to O266 `[V49]`. The attribution check
counts 300 development nodes `[V06]`: 1 went through the shipped control plane end to end `[V06a]`, 126 were developed by a product run and merged by
the orchestrating session after review `[V06b]`, and 173 were written by the orchestrating session directly `[V06c]`.

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

### The parts, and what each has and has not shown

*Added 2026-10-09, from the capability register (`tools/capability_matrix.py`) at the base commit of
`paper/NUMBERS_V2.md`.* The register holds 235 requirements `[V10]`. `PROVEN` is the register's word for an
implementation, a test, positive evidence and a discriminating control where one applies, each found by a probe the
repository can run (a symbol, a collected test, a readiness row). It is not a claim that the capability has run in a
live session, and `docs/LIMITATIONS.md` states the limits row by row.

| Part | What it is | State in the register |
|---|---|---|
| The per-run loop | Plan, develop in an isolated worktree, independent QA against a frozen copy, receipts from a deterministic runner, acceptance only when every criterion passes and one was red before the change, replanning from a rejection with its evidence. Exercised on the `v0.1.0` campaign and in the product runs since | The 16 requirements of phase P1: 16 of 16 PROVEN `[V11]` |
| Durable programme runtime | An event ledger, materialized programme state, run, session, provider and evidence state, durable timers, a sentinel, a never-stuck check, migrations and compact task state; session succession with a master epoch and a leader lease | PROVEN: P2-01 to P2-09, P2-12, P2-13. Crash and reboot recovery, P2-11, is IMPLEMENTED_NOT_PROVEN: a real reboot has not been run |
| Handing the orchestrating session over | Four commands (hold, rollover, rollover-verify, rollover-accept) that freeze a capsule the successor must re-derive, with a fence for a stale predecessor | PARTIAL: P2-14 and P2-20. Tested with fakes; no live succession has run |
| Recovery of failed provider dispatches | A failed dispatch is classified with its provenance, a park is bound to what it stands on and the binding is enforced on resume, and a developer turn that ends on a provider limit screen is a failed dispatch | P2-43 PROVEN with stated limits; a live run that meets a real provider limit is NOT_RUN |
| Autonomy governance | A user intent ledger, an autonomy contract and its profile compiler, a needs-you handling policy, a programme horizon and budget guard | PROVEN: P2-10, P2-22 to P2-25 |
| Programme control and checklist | The separation of programme logic from activities, a controller transition gate, a programme checklist with a reconciler, a supervisor agent with findings, an fsck and the phase-gate binding | PROVEN: P2-15, P2-16, P2-26 to P2-28 |
| Adapters and interfaces | Owned control of the agents HoH starts, an MCP gateway base, a decision exploration base with a decision fsck, the project contract, the state ownership contract, digital-thread identifiers and the evidence-claim schema | PROVEN: P2-17 to P2-19, P2-21, P2-29 to P2-32 |
| Storage lifecycle | An ownership registry, admission by reservation, a durable park and resume under pressure, disk-full-safe durable writes for the migrated writers, verified archive bundles, `VERIFIED_RELOCATION`, a steward, registered ephemeral temporary roots | PROVEN: P2-38 to P2-41, P3-40, P5-25 to P5-33. P2-42 is PARTIAL |
| The exit conditions of phase P2 | The unattended test, no human attached, controller and session replacement, automatic continuation, no lost state | P2-33 to P2-37 are IMPLEMENTED_NOT_PROVEN: the end-to-end evidence went stale when the code changed |
| Everything after P2 | Phases P3 to P12 of the registered plan | 158 of the 176 requirements read MISSING `[V11b]` |

Phase P2 is **not** closed. Of its 43 requirements, 34 read PROVEN `[V10c]` `[V10b]` and the gate has 9 blocking items
open `[V12]`. The counts of P2 and of the later phases that are stated further up are those of the `v0.3.0a2`
checkpoint and stay where they were stated; the lifts of P2-41 and P2-43 and the eight requirements that amendment A6
registered for phase P5 have changed them since.

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

An editable checkout of the default branch is development code. Once the tag
`v0.3.0a1` is cut, checking it out before installing gives the pre-release (see
*Where the project stands* above); `pip install hoh==0.1.0` is the only package published
to PyPI. The same holds for the tag `v0.3.0a2`.

### Platform notes

HoH is developed and tested on Ubuntu (including WSL2). Other platforms:

- **NixOS and other Nix hosts.** `flake.nix` provides a package, an app and a
  dev shell (`nix develop`). It was written without a Nix binary and has
  **never been evaluated or built** (NOT_RUN). Checks run under the `bash`
  found in an absolute `PATH` entry when `/bin/bash` does not exist, and
  `--isolation strict` binds `/nix/store`, `/run/current-system/sw`, and the
  `static` and `profiles` directories of `/etc` read-only, only when the store
  exists (wording corrected 2026-10-08: the two paths were first
  written out in full, which the export leak scan reads as a system path).
  **Everything in those roots is readable by a strict check**, including
  source copies of projects and any secret a derivation put in the store;
  closure-only binds are deferred. Both behaviours are covered by tests that
  fake a Nix host; neither has been run on a real one. If your Herdr server
  runs under a wrapper with another process name, set `HOH_HERDR_COMM` to that
  name (`herdr` and `.herdr-wrapped` are recognised; the second is expected from
  nixpkgs wrappers and not yet observed). The wrapped package puts `git`,
  `bash` and `bubblewrap` on `PATH`; `herdr` and the agent CLIs you bring
  yourself.
- **macOS: partially supported.** Only `--isolation none` exists (there is no
  bubblewrap); `--isolation strict` is refused with a message saying so. The
  arena guard and the approval scope were narrowed for macOS paths (text
  patterns only, not tested on a Mac). Herdr-driven role runs need `/proc` and
  read-only sealing needs `O_PATH`, neither of which macOS has, so a full run is
  not expected to work. This README does not claim macOS support; a complete
  port is a separate task.
- **Ubuntu 24.04 and `--isolation strict`.** `bubblewrap` needs unprivileged
  user namespaces, which Ubuntu 24.04 restricts by default
  (`kernel.apparmor_restrict_unprivileged_userns=1`). When bubblewrap cannot
  create a namespace, HoH refuses `strict` before dispatching anything and
  says so. The preferred remedy is an AppArmor profile that grants `userns,`
  to the `bwrap` path; setting
  `kernel.apparmor_restrict_unprivileged_userns=0` removes that hardening
  system-wide and is a last resort. `--isolation none` is unaffected.

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

*Added after v0.1.0, so that the paragraph above is not over-read.* The
repository now ships optional systemd user units under `ops/`: a supervised
quota resume, an unattended continuation, a storage-pressure resume and a
per-project storage-steward pass. Nothing in the package installs or enables
them; that is the operator's act. They act only on runs and projects that
already exist -- the unattended continuation drives on a project that
registered a driver, after a quota wait or after its previous master died --
and none of them creates a run or a project. Implicit *start* of new work is
still refused; implicit *continuation* of delegated work is now available, by
an operator's explicit choice. `ops/README.md` describes each unit.

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

*The "Exercised here" column describes the v0.1.0 campaign.* The
package itself also carries a control plane, `src/hoh/orchestrator.py`, which
decides run order, global gates and repair nodes for a project; it was added
on 2026-09-11, before the tag, and it is what the benchmark's arm C and the
closure test exercise. The agentic orchestrator described next is a separate,
higher layer that drives the package from outside, and it still holds the
merge authority. `docs/ARCHITECTURE.md` records the correction.

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

*Label added 2026-10-09: the table above is the `v0.1.0` campaign, measured when that release was tagged on
2026-09-14. The same measurement for the whole development line, from 2026-09-07 to 2026-10-09, is under
"Duration, autonomy and scale of the build" near the top of this file.*

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

**After v0.1.0 this changed, and the paragraph above stays as the record of
the campaign it describes.** From 2026-09-23 on, product runs wrote kernel
logic under `src/hoh/` -- the programme checklist was the first -- and from
2026-09-25 on every such candidate came from an isolated clone pinned to one
commit and passed two independent integration reviewers before the
orchestrating session merged it. Core fixes written directly by the
orchestrating session still exist and are attributed as such. *What changed
after v0.1.0* above and `paper/POSITION_PAPER_V2.md` give the account.

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

*At the current development head, the readiness row reads `FAIL`, and the
reason is not the campaign.* Code under the campaign's freeze changed after
the campaign, and the drift accounting does not yet name all of it. The row still
reads the campaign as complete and matched-budget-valid; what fails is the
accounting for the code that moved since. Until that accounting is written,
the sentence above describes the board at v0.1.0, not the board today.

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
| `docs/READINESS.md` | The board: every release condition, its state at the last measurement, and the command that re-derives it |
| `paper/POSITION_PAPER_V2.md` | The development after v0.1.0 -- durable programme state, HoH building its own kernel, storage safety, and the failures found on the way. Ships with the pre-release, with its own figures (`paper/NUMBERS_V2.md`) and audit (`paper/AUDIT_V2.md`) |
| `CHANGELOG.md` | User-visible changes by version; the `0.3.0a1` section is generated from `RELEASE_NOTES.md` by `tools/changelog_from_release_notes.py` |

Read `docs/LIMITATIONS.md` before deciding this is production-ready for
anything you care about. It names, among other things, that the guard around
acceptance-check commands is a tripwire against accidents rather than a
security boundary, and that multi-day orchestrated operation is
demonstrated while long-duration operation without any human intervention
is not.

*Added 2026-10-09: "multi-day" in the sentence above is the `v0.1.0` campaign. The development line has since been
measured at 32 days 15 hours `[V46]`, driven by an agent session with the operator directing it, and with named
interruptions (see "Duration, autonomy and scale of the build" near the top of this file). Operation with no
person involved at all is still not shown; `docs/LIMITATIONS.md`, limit 1, keeps the account.*

## Existing internal documents

This project's original internal working documents -- planning and
operational notes written before this repository took its public form, in
German -- remain exactly as they are and stay on record. `docs/OPERATIONS.md`
is their English successor for operational purposes, not a replacement --
both stay on record.
