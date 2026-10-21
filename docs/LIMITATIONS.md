# Limitations

This document exists because a project that explains its evidence model well
and hides its limits has not actually told the truth. Every item below is a
real, current limit of HoH, not a hedge. Where a limit lives in one specific
module, this says so, so a reader can go look rather than take the claim on
faith.

## Status at the v0.3.0a1 pre-release -- read this first

The numbered entries further down were written up to and for `v0.1.0`, and
they stay as they were written: an entry is not deleted because it was fixed,
and a fixed entry's old wording is part of the record. This section was added
for the pre-release `v0.3.0a1` that follows the release. It sorts those
entries, and the limits found since, into three groups: what is open now, what
is resolved or materially changed since `v0.1.0`, and what is a historical
release reference. The pre-release is a snapshot of phase P2 (*v0.3 Durable
Program Runtime*), which is **in progress and not closed**; every open gate
below is **NOT_YET_PROVEN**, and a gate that reads red stays red here. The
first row of the table is the integrity gate, which held the cut back until the
qaw-followup strand merged and now reads met for the replayed paths, with one
residual stated.

The distinction this whole section depends on, in one line:

    candidate creation != product acceptance != integration acceptance != merge authority != phase closure

Where a row rests on the development repository's working ledger, which this
export does not carry, it names the ledger entry (`O` and a number) without a
path. A figure tagged `[Vnn]` is resolved to its source and to the command
that regenerates it in `paper/NUMBERS_V2.md`.

### Changed since the first alpha (added 2026-10-09, for the pre-release v0.3.0a2)

The rows below stay as they were written for `v0.3.0a1`. This subsection says what
the ledger entries O256 to O260 changed in them; `RELEASE_NOTES.md` has the full
account and each limit it states.

* **A provider usage limit that stops a QA role.** The first two slices of P2-43
  are merged (merges `6312e46d` and `a4e8fbad`): a failed provider dispatch is
  classified, QA re-raises a provider failure instead of booking it as a rejected
  iteration, every dispatch records its provenance, and every park is bound to what
  it stands on. The row's sentence that the repair is not merged is superseded for
  those slices only. P2-43 still reads MISSING `[V40]`: nothing checks the binding
  on resume yet, and the third slice is not merged. A role's own words no longer
  park a run through the abort path; a pane line of a failed turn that is exactly
  a phrase of the classifier's list is still read as provider text whoever wrote
  it, and a limit worded in a way the classifier does not read now blocks for a
  person with no timer.
* **The first prompt after an agent starts.** First-prompt readiness is merged
  (merge `846d8e02`): the recovery models the word wrap of the box as three
  recorded boxes show it, and the dispatcher waits for an idle input box that
  holds still before the first prompt. The row's sentence that the readiness fix
  is the next P2 strand is superseded. Whether the wait cures the stall is
  SUSPECTED until a live check.
* **Context rollover (Layer A).** The follow-up items of the row are fixed in code
  (merge `6091cea9`): the capsule id is validated as a plain name before anything
  is written, a relative `--root` is resolved, a non-object `--input` and a
  non-finite or non-positive interval are refused, and the successor takes a last
  look right before `acquire`. A conditional acquire judges the handover under the
  lease's own lock (merge `b0fd7614`, P2-09), and the launcher starts the successor
  in an explicit permission mode (merge `e4b4cda9`). Still open: no live
  succession has been run, so no row is proven by one.
* **P2 is not closed.** The gate has fewer requirements blocking and open; the row
  below states the figure and keeps the earlier one.
* **New limits.** `hoh agent-setup` has not been tried by a live agent session;
  owned agent control (P2-17) has not run against a real Herdr server; the
  portability patch has not run on a real NixOS, macOS or AppArmor-restricted
  host; and four QA roles ran pytest in the shared default basetemp before the role
  scratch merge (`fcb0b5db`). `RELEASE_NOTES.md` states each.

### Current open limitations

| Limit | State at the pre-release | Evidence |
|---|---|---|
| **The integrity gate is met for the replayed paths; one residual and the tracked minors stay** | The qa-arena-write repair (merge `a455edc0`) seals QA's candidate arena and holds a run that was blocked for a capability violation, for every state shape the current build writes. Its integration reviewer proved three findings open, and this pre-release was held for them (the text of that state is kept in the dated correction at the end of limit 12d): (1) a violation state written before the hold existed could be unblocked by `resume_runs` and the launcher then reported the run ACCEPTED; (2) `tools/acceptance_record.py` did not count an open seal as a problem; (3) a `seals.jsonl` replaced by a symlink made the controller append ledger lines to the foreign file, and a forged `planned` line for a sibling arena of the same run was honoured. **The qaw-followup merge (`ab41589f`, 2026-10-07) closes them for the replayed paths:** the resume loop, its fallback and `hoh resume-storage` refuse a run the hold covers (`test_l1`: the run stays BLOCKED and the launcher verdict is UNDETERMINED); an open QA seal is not an acceptance (`test_n1`); the seal ledger is read and appended only as a single regular link (`O_NOFOLLOW`, checked on the descriptor), so a symlink, a FIFO, a dangling link or a second name is refused before a byte is written (`test_m4`); and a `planned` line is honoured only for an arena this run sealed (`test_m3`). Both integration reviewers replayed each path on the merged tree, the strand's 13 criteria pass under NONE and STRICT and its 54 mutant rows are killed. **Not claimed:** that no other false-ACCEPTED or foreign-write path exists. **Stated and pinned by the strand (P6):** a role pane that has read the run's `arena_secret` and copies iteration and attempt gets a forged plan honoured -- the ledger is a record and a restore aid, not an access control against a process of the same user. Tracked minor: `acceptance_record.open_seals` does not count link names (not reachable from the drive, because evidence is copied without hard links), and a held run under storage pressure is booked as `storage_unavailable` (the strand's own reading says the flow is unreachable). | `docs/LIMITATIONS.md` limit 12d; ledger O253, O254, O255 |
| P2 is not closed, and the board is red | `TECHNICALLY_STABLE_READY = no`. The P2 gate has 11 requirements blocking and open (17 as measured on 2026-10-07), 5 exit conditions bound to requirements that are not PROVEN, and 3 exit conditions no instrument measures yet `[V12]`. Amendment A5 added a P2 requirement, P2-43, which delays closure further. | `docs/READINESS.md`; the build plan's gate report; ledger O252 |
| **A provider usage limit that stops a QA role is booked as a rejected iteration** | Eight iterations were lost this way `[V26]`: wmig, kbr, ugt, pcon, dres2, ecl and rpl in iteration 1, lgs in iteration 2. Each state history reads "candidate rejected: unverified, no QA verdict", followed within about two minutes by the planner's quota block. The same limit in a planner is classified as a resumable block. The orchestrator parked and resumed each run and did not treat a lost iteration as a verdict on the candidate; the dres2 and ecl acceptances in iteration 2 also followed an iteration lost this way. **The repair (failure-classes, the core of amendment A5's P2 item P2-43) is not merged**; P2-43 reads MISSING `[V40]`. The eighth (rpl) is read from its run state, committed with the ledger. `state.json` also keeps a stale `blocked_kind` after `resume-quota` (medium). | ledger O252 and its addition, O253 |
| End-to-end durability evidence is stale | P2-33 to P2-37 were proven once, on evidence taken at an earlier commit. Every later change under `src/hoh/` made that evidence stale, and the durability record no longer re-derives under the current deriver. They read IMPLEMENTED_NOT_PROVEN until fresh evidence is taken at the head and reviewed. (Superseded, 2026-10-11, by records entry O266: the evidence was taken again on `e0d9bdff`, re-derives from its copy, and P2-33 to P2-37 read PROVEN, with the limits stated in the section "Update, 2026-10-11, after the operator's restart" at the end of this file. Pointer added 2026-10-12 by records entry O267; the row's text stays as the state of 2026-10-07.) | ledger O237, O250, O251; `docs/READINESS.md` rows `durability` and `acceptance`; superseded by O266 |
| No real reboot has been run | P2-11, crash and reboot recovery, rests on stressor DS2, a real restart driven by the operator. It has never been run, so P2-11 has never been proven. (Superseded, 2026-10-11, by records entry O266: the operator-run real restart was made, stressor DS2 reads PASS and P2-11 reads PROVEN, with the limits stated in the section "Update, 2026-10-11, after the operator's restart" at the end of this file. Pointer added 2026-10-12 by records entry O267; the row's text stays as the state of 2026-10-07.) | ledger O234, O237; superseded by O266 |
| What "unattended" covers | Where the durability evidence held, it covered the orchestration layer with fixture role runs and the harness's own tick loop -- not systemd, not the plugin entry point, and not a real role agent's permission dialogs. Limit 1 below still holds in full. | ledger O237 |
| The first prompt after an agent starts can be typed and not submitted, and the recovery has a width limit | A startup race: the first prompt is typed, and `herdr agent prompt --wait` reports a stall. Dispatch recovers: after a stalled submit it presses Enter once, only when the box holds exactly this dispatch's own text, and types nothing again. In the four runs that had the recovery before the prompt-threshold merge, seven first prompts stalled and each was recovered `[V31]`. The prompt-threshold merge (`33320bfc`) sends every role prompt above 8,192 bytes, and every prompt holding a paste-unsafe character, through the pointer file, because a long inline paste is rendered collapsed and the box could not be verified (qfu, qfu2 and qfu3 blocked this way). That is a recovery made verifiable, **not a fix of the race**, and it does not prove the dialog boundary, which only the tests pin: a dialog that opens between the screen read and the key press would receive the Enter. **Width limit (run qfu4, PROVEN from the recorded box):** the recovery models a break in the box only mid-word at exactly the box width; Claude Code breaks at a word boundary when the line does not fit. In a 79-column pane (box width 76) the pointer's first line, 77 columns with its indentation, wrapped before its last word, the recovery refused as designed, and the run was blocked at its first planner dispatch before any candidate; the same pointer text passed in a 106-column pane. Residuals the prompt-threshold spec records: recovery works only for prompts the pointer carries; a short inline prompt taller than the pane is refused (T8); a long path can defeat the comparison (T7, SUSPECTED); the pointer's wording "too long for one command-line argument" is now incomplete (T2). Readiness of the first prompt and the wrap model are the next P2 strand, not started. | ledger O253, O255; `src/hoh/dispatchers.py` |
| A role can be re-woken by its own background task, and what the lifecycle does about it | A role turn is taken as ended while the role still has a background task that can wake it and write. One run saw a developer edit source files while QA ran; the candidate-binding check failed the run closed, as designed. A live smoke showed that closing the pane stops the agent from waking and does not end its background descendants `[V32]`. **The role-pane lifecycle is merged (`caa732db`, with its tests-only follow-up):** HoH closes its own role panes and ends the processes a role left running before the freeze, never touching a foreign pane or process (a process is ended only when it carries the pane's minted mark and is anchored to the pane's own shell; a signal goes to one process, never a group; a marked process outside the pane is named and not signalled). Its first live use was the rpl-tests run (its `pane-lifecycle.jsonl`): the planner and developer panes were closed at the turn's end; the QA pane's release was refused because two marked processes had escaped the pane (named, never signalled), and the pane was gone without anyone closing it by hand (the orchestrator found it already gone) (Correction, 2026-10-08: the lifecycle log of that run, `pane-lifecycle.jsonl`, also holds a `closing` and a `closed` line for the QA pane, in the same second as the `refused` line; the earlier "closed three panes and refused one" counted that pane twice, and the text above replaced it.) (Further correction, 2026-10-08: the `closing` line is written by HoH before its own close command and the `closed` line only after a second snapshot no longer lists the pane, and a pane found already gone is written with an `already_gone` field, which this line does not carry (`_close_own_pane` in `src/hoh/dispatchers.py`); so HoH itself closed the QA pane's tab, nobody closed it by hand, and the `refused` line that follows names the two escaped processes, not a failed close.). Not claimed: that no role can still change the tree after the freeze by a route the lifecycle does not see (the numbered limit 21, written by that run, says what a close proves and does not: a shared daemon is named and never ended and blocks the developer's release; that the mark reaches every command an agent's own shell tool starts has not been shown by a test). Tracked minor: extra mutants of the pane-count guard in `_close_own_pane` and three accounting mutants survive the tests (fail closed); a detached daemon adopted by a subreaper is documented as escaping; the run state keeps a stale iteration-1 QA task and `blocked_kind` after acceptance (controller, low). The pre-existing whole-tab close of a dead agent in `dispatchers.py` is the lifecycle's own tracked area. | ledger O252, O253, O254, O255 |
| A long role timeout is booked as an unclassified block | The default role timeout is 2700 seconds; one run was blocked at that limit while its developer still worked `[V29]`. A role timeout and a fresh-agent startup stall are routed to the digest as unclassified, non-delegable decisions; the classes (ROLE_TIMEOUT, PROMPT_STALLED) belong to the failure-classes strand. The timeout is not recorded in the run. | ledger O252 |
| Invisible characters stopped an inline prompt paste (merged: prompt-threshold) | A specification carrying four invisible characters stalled two planner dispatches `[V28]`: the agent interface announced the removal and waited, so the inline paste was never submitted. The remedy (every role prompt above 8,192 bytes, and any prompt with control, format, private-use characters, tab or carriage return, through the file pointer) was merged as `33320bfc`. It was built by a product run under one declared deviation, a bootstrap harness (the previous source plus only the reviewed dispatcher change; the operator delegated the choice), retired at the merge. The residuals are in the first-prompt row above. | ledger O252, O255; `src/hoh/dispatchers.py` |
| Disk-full-safe writes are partial | P2-41 reads PARTIAL: the run store, the timer store and 15 further writers (W1 to W15) use the durable write; 12 writers remain on the migration list `[V44]`, and the second migration strand has not run. | ledger O242, O252 |
| Registered temporary roots are merged; the register holds the row at PARTIAL | P2-42 (a registered project-owned temporary root, creator-cleaned workspaces, admitted writes, an inode signal) was merged after two runs that integration review refused `[V38]`. The EPHEMERAL_TMP tier, the release proof and the relocation of temporary paths (P5-35) are deferred. The capability register's row for P2-42 reads PARTIAL `[V10d]`: the register edit that followed the merge names the symbol with an equivalent, which holds the row below PROVEN. The vet that admits a scratch is the memory of one process; a second process is outside the single-creator design (minor, tracked). (Superseded, 2026-10-11, by records entry O266: the hold was lifted on the first run of a registered project, the register's row for P2-42 reads PROVEN, and the row V10d of the numbers table says so since its re-rendering at `cc79b2ae`; the limits of that lift, among them the approval-resume of a registered run (L2) not exercised, are stated in the section "Update, 2026-10-11, after the operator's restart" at the end of this file. Pointer added 2026-10-12 by records entry O267; the row's text stays as the state of 2026-10-07.) | `src/hoh/storage_ephemeral.py`; ledger O253; superseded by O266 |
| The storage steward has not acted on VeriHarness's own data | Section P ran on the real project: it was adopted as its own storage trust root, the legacy survey found 0 of 188 roots eligible `[V33]`, `adopt-legacy` ended with an empty outcome list and exit code 2, and the steward's real pass reported state NORMAL, observed only, with 0 bytes reclaimed. Section P produced no `VERIFIED_RELOCATION` receipt. 157 committed briefs under `dogfood/` already designate 98 of the 188 roots `[V33b]`, and no committed report names any, so every root stays refused. The survey is not incremental: it needed about 3.4 hours detached, after two attempts were cut off by the background limit (medium, tracked). | ledger O252 |
| Inode exhaustion has happened twice, and the product is not what handled it | The development machine ran out of inodes twice on 2026-10-05, at 67,107,099 of 67,108,864 `[V35]`, from accumulated small-file scratch of reviewers and drafters; the process that made the final push is not identified. The orchestrator's two cleanups removed their own reproducible scratch under the operator's temporary-file rule: 15,558,933 entries `[V36]`, then 6,562,367 entries and 37,598,417,431 bytes `[V37]`. The steward's inode axis (P2-42) is merged, and no record shows it meeting a real pressure event. | ledger O252, O253 |
| A gap QA records as blocking does not block acceptance | `RoleResult.accepted()` reads only the criteria's verdicts; the QA answer's `open_gaps` never decides anything. The product accepted candidates over a QA-recorded defect at least three times `[V09]`. The QA-gap gate was refused at integration twice; a later attempt was blocked at planning by the invisible characters above. It is not merged. | `src/hoh/contracts.py`; ledger O236, O237, O240, O252 |
| A candidate's changed paths are not compared with its contract's scope | The product checks that a scope exists, not that the candidate stayed inside it; out-of-scope files were merged as recorded deviations. | ledger O244, O245, O249 |
| The planner re-types the specification's criteria | Nothing compares a planned criterion with the specification's text. The planner transcribed criteria with a stray quote (one iteration lost), added a `--deselect`, wrapped criteria, added or dropped criteria and replaced them. A replay of 53 planner answers found 195 deviating checks in 42 answers `[V34]`. The reviewers ran the specification verbatim each time; the product did not. The criterion-fidelity strand is released in review and not built. | ledger O252 |
| Long prompts: the directory chain is trusted | Prompts longer than one argument travel as prompt files with link-safe leaf names and an in-memory digest. The directory chain up to those files is assumed not to be swapped by a same-user role (T-LP5, high; its strand is released in review and not built); approval and answer files can still follow planted links (T-LP1, T-LP2, medium). | ledger O251, O252 |
| The clean-tree check decodes `git status` strictly | The witness, the integrity snapshots and the run state handle names that are not UTF-8. The clean-tree check still decodes `git status` strictly (medium). | ledger O251, O252 |
| A role's deletion guard is a list of literal prefixes | The mandatory deny rule that never matched is fixed. Quoted `-delete`, absolute binary paths and `command` or `xargs` wrappers are still not matched by a role's own rules (medium, tracked); the machine-wide deny hook still applies to every session. | `src/hoh/approval_policy.py`; ledger O252 |
| The runner puts pytest's basetemp under the arena | `tools/preserve.py` classifies `_arenas/` as an evidence container, so `tests/test_preserve.py` fails in any run whose criteria include it (high, tracked). | ledger O252 |
| QA's arena seal is detection plus mode bits | Besides the residual in the first row: a role running as root is detected, not prevented; an agent with a shell can `chmod` first and is detected by the change time; a forged `released` line between dispatches hides a touch; after a plain role timeout the arena is unsealed while the pane may still work. The numbered limit 12d records each. | `docs/LIMITATIONS.md` limit 12d; ledger O253 |
| No capacity planner | Project-native parallelism is MISSING in the board's baseline row, and numeric provider-concurrency targets raised provider burn without raising completed work, which the development ledger records as an input to P3 (P3-41 reads MISSING `[V40]`). | `docs/READINESS.md` row `parallelism_baseline`; ledger O253 |
| A dialog-free role answer is not yet observed in the run bytes | Roles may write their own answer files without a permission prompt (merged), but no run record shows a live session doing so without one; the claim is "rule written", not "dialog removed". Not re-checked since. | ledger O239, O240 |
| Context rollover (Layer A) is merged and has not run live | `hoh project hold`, `rollover`, `rollover-verify` and `rollover-accept` (merge `601168fe`) are tested with fakes; **no live succession has been run, and no P2 row is proven by it** (the one handoff of 2026-10-07 was written by hand before the protocol existed and is not that evidence). The draining states between the freeze and the validated successor are **recorded, not enforced**: nothing refuses `hoh start` or `hoh run` in that window. The first-prompt stall can lose the successor's pointer; the acknowledgement deadline then fails closed (the predecessor stays master) and the pointer is sent again. A capsule's derived facts are only as good as the code that derives them (git, files, run states); a fact nobody derives is not checked, and an operator decision's `decided` status is author-set. **Open follow-up items (tracked HIGH, a prerequisite of the live canary):** the capsule id is not pattern-validated, so an unverified capsule can place its create-only, owner-only acknowledgement file outside the rollover directory; `acquire` is not atomic with the last verification, so a master gap is possible (never two masters); a relative `--root` never rolls over (fail closed); a non-object `--input` raises instead of refusing; `hold --interval` accepts zero or a negative value; nine guard sites have no test that kills their removal. An automatic rotation policy (Layer B) is not built. *Correction by addition, 2026-10-20 (the row stands as written on its date): "no live succession has been run" is no longer true. The build's own rotation policy (`src/hoh/rotation.py`) has requested, and the succession code has completed, live successions of the orchestrating session since 2026-10-11, each recorded by a `rotation_finished` row; the README front gives the counts and the method. That log is the build's working record and is not exported. The correction changes no P2 row, and the other limits of this row are unchanged.* | ledger O255; `src/hoh/rollover.py`, `src/hoh/succession_capsule.py` |
| The developer role can leave a filesystem-wide search running | Twice on 2026-10-07 (qfu5, crl) a developer ended its turn with a foreground-detached `find /` running for minutes; each was stopped by the orchestrator, which signalled only those processes. The specs had named scratch paths the role cannot reach (a rule against that is in place); the role prompt does not yet forbid searching outside the worktree and the arena (medium, tracked). | ledger O255 |
| The board's `tests` row and this environment's failures | `docs/READINESS.md` measures the suite in a checkout with the git history and the runs directory, and reads that environment's failures. A history-less copy of the same tree, as the integration reviewers and the orchestrator use for base-against-candidate comparisons, fails 18 tests that need the git history, the runs directory or the host's units (`test_attribution`, `test_isolation_record`, `test_preflight`, `test_readiness`, `test_repetition_plan`, `test_sandbox_toolchain`, `test_succession`). They are the same on base and candidate in every comparison recorded; they are two environments, not two versions of the code. | ledger O255 |
| The remaining release gates | Besides the integrity gate above: the exact-head external CI, the export synchronisation, the benchmark freeze, the succession capsule, the installed-unit check, the durability and acceptance rows, the programme checklist and supervisor review, the lint and identifier checks, the union gate's U5 and the test suite all read red or `NOT_RUN` on the board, each for a reason the board states. | `docs/READINESS.md` |

Every other numbered limit below still holds as written, as far as this
snapshot re-checked it -- not every one was re-measured: limits 3, 5, 7, 10,
11, 12, 12c, 12e, 12f, 12g, 13, 13a, 13b, 13c, 14, 17, 18 and 20. Limits 2,
12b and 15 already record their own resolution or result and are unchanged
since `v0.1.0`. Limit 21 was written by the role-pane-lifecycle run itself and
is not re-measured here.

### Resolved or materially changed since v0.1.0

The entry itself stays below, unedited; this is what changed and the evidence
for it.

| Limit | What changed | State now | Evidence |
|---|---|---|---|
| 1 -- long-duration unattended operation | A durable programme runtime now exists: durable timers, a sentinel, session succession with a master epoch, a resume loop, and an optional unattended continuation unit. | **Open.** Built and partly proven on fixtures; not demonstrated over days without intervention, and not across a real reboot. | ledger O233, O234, O237; P2 rows of the capability matrix |
| 4 -- the check guard is a tripwire | Three changes. The self-hosted runs have executed their checks under `--isolation strict` since 2026-09-24, so the entry's sentence that no check in this project's history ran inside the sandbox no longer holds. An operator-side policy file can now only sharpen the shipped command policy, never relax it. And the mandatory deny rule that never matched was fixed. | **Open** as a limit: a denylist is still not a security boundary. | ledger O235, O252; the guard-union and deny-rule product runs' merges |
| 6 -- the arena inside the project's git tree | The deployment this project uses for itself places each run's clone and arenas under a registered run root outside the canonical repository. | **Unchanged for a default `hoh start`**; changed for the self-hosted deployment. | ledger O212, O239 |
| 8 -- what the dogfood evidence shows about `src/` | Product runs have written kernel logic under `src/hoh/` since 2026-09-23: 55 of the 69 product-run merges since 2026-09-25 change it `[V02]` `[V01]` (34 of 46 as measured on 2026-10-07 for the first alpha). The entry's "no run has ever added or edited a line of Python inside `src/`" is true of the v0.1.0 history and false of the development after it. | **Superseded** for the development line. | ledger P2-26 entry, O236 to O255; the merge history |
| 9 -- composition | A second independent layer now stands between acceptance and merge: two integration reviewers per accepted candidate, who refused 26 candidates the product had accepted `[V07t]`. | **Open**: review is a process, not a proof, and the union is still checked only against known failure shapes. | ledger O236 to O255 |
| 12a -- trivially discriminating criteria | Detection now also recognises pytest's own "nothing executed" messages at exit 4, case-insensitively. | **Open**: nothing is subtracted from acceptance, and QA was recorded marking criteria the harness had flagged as artefactual as discriminating (not re-checked since). | ledger O236, O237; the artefactual-signature product run's merge |
| 12d -- the witness compares two moments | The witness digests names that are not UTF-8 instead of crashing on them and the run state records such text losslessly; it stopped a run whose check wrote bytecode into a protected arena (root cause proven); and QA's candidate arena is now sealed and frozen in QA's witness, with a hold on capability violations that `hoh resume` and `hoh deliver` refuse for the states the current build writes (the three findings that were open at the first draft of this checkpoint are closed for the replayed paths by the qaw-followup merge; first row of the open table). | **Open**: detection, not prevention; see the open row on the seal. | ledger O241, O249, O251, O253 |
| 16 -- quota exhaustion and a broken contract look alike | A resume loop now acts on due quota waits, a retry timer is never due before the provider's reset, and an agent that ends its turn without an answer gets one follow-up before the dispatch counts as unanswered. A provider limit in QA is still booked as a rejection (first open row). | **Not yet proven** on a real provider quota exhaustion in a live role run, and **open** for the QA case. | ledger O232, O233, O237, O252; the dispatch-robustness product run's merge |
| 19 -- `benchmark_v3 = PASS` | The board's row reads `FAIL` at the current head: files under the campaign's freeze changed after the campaign without being named in its drift accounting. | The campaign's statement stays historical and correct; the present-tense "the row reads `PASS`" is **stale**. | `docs/READINESS.md` rows `benchmark_v3` and `benchmark_v3_preregistration` |
| Union gate timeout (found after v0.1.0) | A fixed 600-second limit on the whole suite was suspected of the non-converging regeneration; the mechanism proved to be the timeout text, which began with an interpreter home path, entering the board and the claims ledger. A timeout is now a fixed `NOT_RUN` text, process groups are killed safely and the suite budget is 1800 seconds. | **Resolved**; the row `union_invariants` still reads red for the test failures of U5. | ledger O250, O251, O252; the union-gate-timeout merge |
| Dispatch residue and stalled submits (found after v0.1.0) | A retry typed on top of unsent input; a pointer prompt starting with `/` was read as an unknown command and cost a run two iterations `[V22]`; a resumed iteration reused its candidate id and failed the run. Prompts no longer start with `/`, a pane with unsent input is refused, a stalled submit is recovered once, and a candidate id is fresh. | **Resolved** for these paths; the startup race behind the stalls is open (above). | ledger O252, O253 |
| Long run paths (found after v0.1.0) | `roles._SAFE_PATH` bounds a path at 200 characters and a long arena's pytest temporary path exceeded it. The house-rules naming test no longer depends on arena depth. | **Resolved** for the test; the bound itself is unchanged. | `src/hoh/roles.py`; ledger O252 |

### Historical release references

The figures in limits 1, 2, 8, 12e and 19 -- the campaign's runs, receipts,
merges and findings, the matched-budget benchmark, the reference counts at the
`v0.1.0` tag -- are measurements of the `v0.1.0` campaign. They are correct
for that campaign and are not current-development numbers. The development
after it is measured in `paper/NUMBERS_V2.md`.

## 1. Multi-day orchestrated operation is demonstrated; long-duration operation without intervention is not

This entry has now been wrong twice, in opposite directions, and both
corrections stay visible, because how a project describes its own evidence is
itself evidence.

**The first version undersold what exists.** It read "a handful of iterations
inside one run".

**The second version misdescribed who produced it.** It said an operator
"wrote each specification, merged each accepted candidate, decided each open
question", and called those days *attended*. That is not the architecture:
the specifications, merges, repairs and findings came from an **orchestrating
agent session** -- Herdr runs it, it drives HoH, HoH runs the roles. Calling
that layer "an operator" reads as a person typing, and it describes the
system's own top layer as if it were external supervision.

Measured on the campaign that produced this release: **65 runs carrying 1,988
receipts (880 of them control runs), 100 iterations that produced receipts,
51 specifications, 45 merges and 127 recorded findings**, across five calendar
days and 3.5 days of elapsed wall-clock -- and the position paper, the claims
ledger, the release documents and the export machinery among the artifacts
produced.

Three statements have to be kept apart here, because collapsing them is what
made both earlier versions wrong.

**Empirically demonstrated.** A multi-day, agent-orchestrated campaign that
continued on its own between policy, safety and release gates: writing
specifications, starting runs, reading verdicts, merging, running the global
gates, generating repair runs from gate failures, and re-closing until the
whole set reached a fixpoint.

**Architecturally available, not exercised here.** Most of what was escalated
in this campaign was escalated **by rule, not by necessity**. A human decided
seven things -- `DEC-R1`, `DEC-R1a`, `DEC-R2`, `DEC-R3`, `DEC-R4`, `DEC-R5`,
`DEC-R6`: the project's public name, which files the export carries, how one
class of reference is dispositioned, and whether to publish. Seven governance
decisions against 65 runs, 51 specifications, 45 merges and 127 findings. The
same orchestration configured with those policies delegated in advance would
have resolved them itself. That is a statement about the design, not a
measurement: this deployment did not run that way, so it is listed here as
untested rather than as a result.

**Not demonstrated, and the real gap.** Weeks or months of operation with no
human intervention at all. Five days is not months. Nothing here shows how the
failure modes this campaign actually hit -- budget exhaustion, a role harness
whose credential had expired, a lost tool directory after a power cut, a
composition failure between two individually correct runs -- behave when nobody
is reachable for a week. A human *was* reachable throughout and answered those
seven times.

**One half of that sentence has since been measured, and it is worth separating
from the half that has not.** Until 2026-09-11 this paragraph also said
"nothing here shows an orchestrator surviving its own restart". That is no
longer true. A real run was driven by the control plane and killed twice with
`os._exit(9)` -- once after a candidate was accepted but before the merge, once
after the merge but before global closure -- and a fresh process, holding
nothing but the persisted state, resumed correctly both times: it read the run
record rather than re-dispatching, merged exactly once, and reached a fixpoint.
Five distinct process ids, and the run's own write sequence unchanged across
the resume, so nothing was re-run.

**A second measurement, made on 2026-09-11, goes further and is worth stating
exactly.** A run reached its fixpoint with **no human action at all** after it
was started: a real dispatch, a candidate accepted, four deliberate process
kills at the boundaries where a resume can lose or repeat work, a global gate
deliberately red on the merged state, a repair node created and specified
automatically, a real repair dispatch, and a second closure that reached
`RC_CLOSED`. Six processes, four kills, zero interventions -- and checked
against the repository rather than the harness's own log: five commits, every
candidate landed exactly once.

What that still does **not** establish is the rest of this paragraph, and the
gap is one of scale rather than kind. The run took minutes, not days. It had
one planned node, one repair, and a five-file fixture. A provider outage across
a long idle period, an orchestrator restarted many times, and the accumulation
of small ambiguities over weeks all remain untested. The approval authority
that let it proceed unattended was configured in advance; a deployment without
one still blocks at a trust prompt, deliberately.

And one thing is worth saying because the first attempt at that run failed:
the deadlock it hit was found by the fault injection, not by reasoning. Killing
the process between marking a node as running and dispatching it left a run
that existed and had never begun, which the controller called unclassifiable
and halted on -- every round, indefinitely. That is the shape of failure this
kind of test exists to find, and it is a reason to treat "it worked once
unattended" as a beginning rather than a result.

`Budgets.max_wallclock_seconds` and `max_iterations` exist and are enforced
(`RunState.budget_exhausted` in `src/hoh/contracts.py`), but an enforced ceiling
is not the same claim as demonstrated multi-month reliability under it. Anyone
evaluating HoH for long-horizon use should treat that gap as open, and should
not read "five days orchestrated" as evidence for the month that follows.

### Addition, 2026-10-09: the duration is now measured on the whole development line

Everything above in this entry is the text of the `v0.1.0` campaign -- five calendar days and three and a half days of
elapsed wall-clock -- and stays as written. "Five days" was never the age of the project, and after the tag it stopped
being its size. Measured on the development line at the base commit of `paper/NUMBERS_V2.md` (the O262 records commit): the first
commit is of 2026-09-07 and the latest of 2026-10-09, 32 days 15 hours apart `[V46]`; 23 of the 33 calendar days carry
a commit, none on 2026-09-15 to 2026-09-20 and none on 2026-09-26 to 2026-09-29, and each of the last ten days does
`[V47]`; there are 1,088 commits `[V48]`, 69 product-run merges `[V01]` and 234 numbered ledger entries `[V49]`. For the
line as a whole the multi-day statement in this entry's title is therefore a multi-week one. Three things stay as they
were, and the sentence "long-duration operation without intervention is not" keeps its truth:

* *Not without a person.* The orchestrating session is an agent session. The operator directed it (6 directives stored
  verbatim `[V53]`), added work, approved every publication and decided the storage and policy questions. The operator
  was reachable and was asked. Operation with no person involved is not shown.
* *Not without stopping.* The line stopped: 13 quota stops between 2026-10-03 and 2026-10-07 `[V55]`, a full disk on
  2026-09-25 (977 GiB of a 1 TiB volume) `[V16]` that left four days without a commit, inode exhaustion on 2026-10-05
  (67,107,099 of 67,108,864 inodes used) `[V35]`, inode pressure on 2026-10-09 (the product's storage guard reached
  CRITICAL on the inode axis and parked two runs), a power cut, and an operator-requested machine shutdown of about one
  hour on 2026-10-08 `[V56]`. The six days after the tag, 2026-09-15 to 2026-09-20, have no commit and no recorded
  reason.
* *Not by the product's own runtime.* The orchestrating session was handed over 3 times `[V54]` by files the outgoing
  session wrote, not by the product's rollover protocol, which has not run live (P2-14 reads PARTIAL). The five exit
  conditions of phase P2 that a long unattended run would prove, P2-33 to P2-37, read IMPLEMENTED_NOT_PROVEN, and crash
  and reboot recovery (P2-11) has never been exercised on a real reboot. Of the development nodes after the attribution
  anchor, 109 were written by the orchestrating session directly `[V06c]`, and 94 were developed by product runs and
  merged by the orchestrating session after review `[V06b]`.

## 2. A baseline now exists, and it does not favour the harness

This limitation used to read: no baseline of any kind exists. One does now,
under a protocol committed before the first arm ran
(`docs/BENCHMARK_PROTOCOL.md`), and the result is reported here rather than
somewhere more flattering.

**Arm A -- a plain agent, no harness -- passed all five hidden suites.**
Including the composition task, where a plausible implementation is correct
once and wrong when applied twice, and the task where a weak test suite lets a
wrong implementation through. Between 20 and 114 seconds each.

Arm B, one HoH run, passed four of five; the fifth produced no final state.
Arm C, the full control plane, produced a final state once in five.

Two readings, and the honest one is the first:

* **These five tasks do not discriminate between the arms.** That is a finding
  about the benchmark's design, not a verdict on any arm. A task a competent
  agent finishes in twenty seconds cannot show what verification adds.
* **The B-vs-C comparison is not supported at all.** Four arm-C cells and one
  arm-B cell stopped at an interactive permission prompt, five attempts each --
  the planner running shell commands it is not supposed to run (limitation
  12b). One data point is not a comparison and is not offered as one.

So what is now demonstrated is narrower than the limitation's old wording
suggested was missing, and in the opposite direction: on tasks of this size,
an unsupervised agent did the work correctly every time. Nothing here shows
the harness catching something the plain agent got wrong, because on these
tasks the plain agent got nothing wrong.

What remains untested is the case the project is actually about: work large
enough that a single agent's self-report is not trustworthy, and composition
across several accepted increments. Designing tasks that reach it is open, and
`docs/BENCHMARK_RESULTS.md` carries the full table, every excluded cell and
the reason it stopped.

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

**Since 2026-09-11 there is an OS sandbox, and it does not close this limit.**
`src/hoh/sandbox.py` provides filesystem and network isolation through Linux
namespaces: the candidate mounted read-only, a separate writable scratch area,
no network, an environment that is not inherited. Measured, not asserted -- a
command inside it cannot read a file outside the declared binds, and
`/proc/net/route` shows zero routes. What that changes is *how far a command
can reach*. It does not make a pattern denylist sound, and a command the
denylist does not name is still allowed to do whatever it does. The honest
statement is that the blast radius shrinks to the sandbox, not that the guard
became a security boundary.

Two further qualifications, because "we added a sandbox" is exactly the kind
of sentence that gets over-read. The sandbox is **not yet the path acceptance
checks take**: it exists, is tested, and is selectable, and `runner.py` does
not use it, so no check in this project's own history has run inside one. And
it fails closed by construction -- asking for isolation the backend cannot
provide raises rather than quietly running unsandboxed, because a sandbox that
silently degrades is worse than none.

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

**The sandbox added in 2026-09-11 does not touch this one at all**, and it is
worth saying so next to the sentence rather than leaving a reader to infer it.
The divergence is two different strings inside HoH's own code -- the guard
reasons about the command with the placeholder replaced by the literal
`"ARENA"`, the runner substitutes the real absolute path -- and no amount of
isolation makes two different strings equal. Isolation reduces what the
divergence can cost. It does not remove the divergence.

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

**What the sandbox added here, stated precisely, because the first version of
this paragraph was wrong in both directions.**

It said the sandbox closed this failure. It did not, because the common case
was already closed: since 2026-09-08 `_env` sets `GIT_CEILING_DIRECTORIES` to
the arena's parent, and with that in place `git rev-parse --show-toplevel`
inside an arena exits `128` instead of naming the ancestor repository. A
sandbox was not needed for that and is not what fixed it.

What *was* still open is narrower and sharper than this limit's own wording
suggests. The mitigation is an environment variable, and the thing it
constrains is a shell command that can remove it. Measured through `run_check`
itself:

| Regime | Exit | Answer |
|---|---|---|
| Ceiling set, no sandbox | `128` | prevented |
| **The check removes the ceiling**, no sandbox | `0` | **names the ancestor repository** |
| The check removes the ceiling, sandboxed | `128` | prevented |

`env -u GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel` climbs straight
back out, and the command guard does not catch it: there is no absolute path
and no `..` in that command for it to object to.

So the sandbox's contribution is to make the boundary **structural rather than
configured** -- there is no parent `.git` to find, so removing a variable
accomplishes nothing. That is worth having, and it is a smaller claim than
"closed".

The general lesson is worth more than the instance. A mitigation that consists
of an environment variable is not enforceable against the thing it constrains,
because the constrained process inherits the environment meant to limit it.
That is the same family as limit 4, with the difference that this one read like
a boundary.

Two things this still does not mean. `STRICT` isolation is available and is not
the default, so most checks continue to run the historical way. And the nesting
itself is unchanged: the arena still sits inside the project's working tree.
What the sandbox removes is the parent `.git` that git climbs to, not the
structure that makes climbing possible.

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

Measured directly against this repository's own commit history: **no run has
ever added or edited a line of Python inside `src/`.** That wording is
narrower than this entry carried until 2026-09-10, and the narrowing is a
correction, not a hedge. The earlier form was "no run has ever added or
edited a line inside `src/`", and it is false: run `d1` added
`src/hoh/policy/dangerous-patterns.txt` and
`src/hoh/policy/house-rules-patterns.txt` -- packaged copies of the guard
pattern lists, so that an installed wheel could run an acceptance check at
all -- and that commit reached the mainline through the ordinary merge of the
accepted candidate `d1-i2`. It is one commit against 49 non-merge commits
touching `src/hoh/`; the other 48 were written outside any run. It contains no `.py` file, which
is why the corrected claim still says something worth saying. But "a run
wrote nothing under `src/`" was not true, and a reader checking it against
the commit history would have caught this document in an overstatement about
its own discipline.

Inside `tests/`, the picture is
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

## 9. Composition is guarded globally, not proven complete

This is the sharpest limit in this document, because it says the project's
own verification discipline is insufficient for the thing it is most likely
to be used for: composing the output of several runs. Two runs, each verified
against its own base with candidate-bound evidence and each respecting its
own declared scope, left the combined state inconsistent after being merged
-- twice. Once the merge produced 3 contradictory statements about this
project's own licence. Once it left 5 coverage anchors pointing at content
that had moved, across 2 merges total. **Neither run was wrong** on its own
terms: each was checked against its own base, its own scope, and its own
evidence, and each check passed honestly. Per-run evidence binding is therefore
necessary and not sufficient: a reader who treats "every run in this history
was individually verified" as "the current merged state is verified" is making
a claim per-run checking does not support.

**Since this entry was first written, that gap has been partly closed, and
saying so is part of stating it honestly.** An earlier version ended "nothing
in the loop checks the union of two runs' changes against each other". That is
no longer true. `tools/union_gate.py` runs five invariants over the *combined*
state rather than over any single run's base, and two of them are exactly the
two failures named above:

```
U1  every licence statement in the repository agrees          <- the first failure
U2  no shipped file references a file that is not shipped
U3  what the built artifact ships matches what the repo claims
U4  every evidence reference in the claims ledger still resolves <- the second
U5  suite and linter are green on the combined state
```

A third instance was caught by this discipline during the release closure and
is worth naming because it is the mechanism working rather than a story about
it: one run introduced the invariant "every exported path is classified", a
later run added two exported documents, **neither run was wrong on its own
terms**, and the whole-state check refused the combined state until a repair
run classified them.

**The invariants are one of two layers, and the second one is what actually
caught the last two failures.** `U1`-`U5` check the merged tree. Above them
sits a closure layer that checks the *process*: semantic dependency measurement
between runs that are candidates to proceed in parallel, the claims/export/
global-state invariants, and a post-DAG pass that runs after every planned run
has been merged. When any of those fail, the failure does not stop the release
-- it generates a new repair run, which goes through the ordinary loop and must
itself be accepted, after which closure is attempted again. Release is gated on
a *fixpoint*, not on the plan being finished:

```
DAG_TERMINAL  !=  RC_CLOSED
```

A terminal DAG means every planned run has been merged. `RC_CLOSED` additionally
requires every global gate green *and* no new repair node created by the pass
that checked them. The distinction was not theoretical during this release. The
DAG went terminal after run `D8`; the global gates then found two real
composition and export defects in the merged state, which became repair runs
`d8b` and `d8c`; only after those were accepted and closure re-ran clean did
the candidate qualify. Both defects were prospective -- found before publication
by the layer above the acceptance loop, not by a reader afterwards.

The general form:

> **Per-run evidence binding is necessary but insufficient for composition.
> VeriHarness therefore adds global composition closure above the per-run
> acceptance loop.**

**What remains open is the part that cannot be closed by adding invariants.**
`U1`-`U5` are a *named list*, extended each time a composition failure taught
this project a new one. A cross-run inconsistency of a kind no invariant names
would still pass every gate here. So the honest form of this limit is no longer
"the union is unchecked" but: **the union is checked, at two levels, against a
growing list of known failure shapes -- and that list is evidence of what has
been learned, not a proof that the next composition is sound.** The per-run
acceptance loop is still not closed under composition, and no amount of global
gating changes that; what the global layer changes is whether the resulting
inconsistency reaches a release.

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

## 12a. A criterion whose own file the candidate added discriminates trivially

The acceptance-governing condition in limitation 12 -- red on the predecessor,
green on the candidate -- has a hole that this project's own STRICT acceptance
run walked straight into.

The accepted candidate had exactly one discriminating criterion:

    python3 -m unittest discover -s tests -p 'test_roman.py'

Exit 0 on the candidate. On the baseline, exit 5:

    Ran 0 tests in 0.000s

    NO TESTS RAN

The baseline is red **because the test file is not there** -- it is part of the
candidate. Every newly written test file discriminates in that sense, so the
criterion demonstrates that the developer created a file, not that behaviour
changed. In that particular run the increment was real; the evidence offered
for it was not.

`hoh.runner.artefactual_reason()` detects the signatures with which a test
runner says it executed nothing (exit 5, `NO TESTS RAN`, `FileNotFoundError`,
`collected 0 items`) and the controller records them on the run state, so the
weakness is visible in the evidence and to any gate that reads it.

**Nothing is subtracted.** What counts as a demonstrated increment is a
semantic property of acceptance, and changing it belongs in a specification
rather than in a defect fix -- a run whose only new criterion is a new test
file would stop being acceptable, which may well be right and is not a change
to make silently. So this stays open, tracked, and priority **high**: it
weakens the single mechanism this project offers against a candidate that
passes without changing anything.

What works today, and what a spec author should do until it is closed: write
the failing test *before* the run, in the baseline. Then the baseline fails
with the behaviour's absence -- `NotImplementedError`, a wrong value, an
assertion -- and the criterion demonstrates what it claims to. This project's
third STRICT acceptance run is built that way.

## 12b. The planner's read-only contract is a sentence, not a boundary

The planner prompt says, in those words: *"This is a pure planning invocation:
implement nothing, edit nothing, test nothing. You may **read** the project
directory in order to ground the plan in the actual project -- you may not
write there."*

Nothing enforces it. The developer role gets an arena and a candidate binding;
the planner gets a request.

Measured, not inferred. Five benchmark cells stopped at an interactive
permission prompt, each attempted five times, and the transcripts show the
planner moving the file under test, writing a reference implementation, and
executing the acceptance criteria it was drafting:

    Bash(cd .../scratchpad && mv duration.py durati...)
    K1 exit= 1 / K2 exit= 1 / K3 exit= 1
    Confirmed: all three checks pass (exit 0) against a correct reference
    implementation and correctly fail (exit 1) against the current stub

What stopped it was the surrounding harness's permission gate, which is not
part of HoH and is not present in every deployment.

Two costs, and the second is the one that matters:

* the criteria a planner has already tested against its own reference are
  criteria it believes are green, which is precisely the mixing that keeping
  planning and verification apart is supposed to prevent;
* a planner that can write into the project can change the thing a later
  criterion measures, and no binding in the run would show it.

**Corrected while building the countermeasure.** The planner does *not* have
write access to the repository or the candidate: A03 has given it a copy of its
own since 2026-09-08, and the `mv` in the transcript hit that copy. What it
does have is write access to the directory the copy sits in -- the **arena
root** -- and the candidate arena for every iteration and attempt is a sibling
there. That is the tree the acceptance checks run in.

The placement has a documented reason: one directory up, the harness asks for
trust once per run instead of once per iteration. The cost of it was not
written down next to the reason.

**Priority: high, and open.** The fix is structural -- the planner needs a
directory that contains its copy and nothing else, and the roles need a
capability policy that is enforced rather than requested.

### What was built, and what two adversarial reviews then found (2026-09-13)

`src/hoh/capability.py` gives every role a `RoleExecutionPolicy`: read and
write scopes, git and process and network rights, an output channel, and a set
of trees that must be byte-identical before and after the role runs. The
planner got a directory of its own, so the candidate arenas stopped being
siblings of its copy.

Two independent reviewers were asked to refute the resulting claim. Both did.

**The measurement did not measure what its metric names said.** Sixteen
violations planted into a copy of a real run; thirteen came back clean.
`planner_repo_mutations` never ran `git status`, so an implementation written
straight into the working tree and left uncommitted scored a perfect run. The
candidate arenas -- the trees the acceptance checks execute in, which is the
whole subject of this limitation -- were compared only by directory *name*.
`__pycache__` was excluded from every digest, and a `.pyc` whose header matches
the source wins at import, so a stub could be made to pass its own criteria
with nothing in the source changing.

**The policy was not installed where it mattered.** Only the candidate arenas
and `receipts/` were protected. The run's own directory was not, so a planner
that emptied `checks.json` during its own dispatch removed the preservation
suite and a real regression was accepted as a checkpoint -- reproduced end to
end, with nothing noted anywhere. The repository was not, so a file the planner
wrote into the live worktree landed inside the accepted candidate, because
`snapshot()` binds the working tree rather than the commit.

**Nothing tested the installation.** Setting `protected=()` for the planner --
switching the detection half off entirely -- left the whole suite green at 840
tests. `tests/test_capability.py` attacks the instrument and builds its
protected trees by hand; no test called `Controller._policy_for`.

All three are fixed: the digest excludes nothing but `.git` (and git's own
history is watched separately through HEAD and refs, not the index); a single
file can be protected, which `checks.json` needs and silently did not have;
the run directory, the repository and the other roles' arenas are in the
policies; and `tests/test_policy_wiring.py` pins the installation, including
both reproductions and the positive controls that keep the boundary from
simply forbidding everything.

### Measured under the fixed wiring (2026-09-13)

Run `cfnfib7a`, a real three-agent Herdr+HoH dispatch against the adversarial
specification above, reports `planner_capability_boundary = VERIFIED`:
`planner_repo_mutations`, `planner_git_mutations` and
`planner_generated_implementation` all zero, the planner's plan valid, the
developer's candidate landed and acceptance working. The instrument that says
so catches seven planted violations, each on its own.

The number that took two reviews to get right is the last one: **the witness
was armed for 2 of 2 planner dispatches**, read out of the dispatch records
rather than derived from the controller's source. How much a witness covered is
a property of the dispatch, because the protected set is built from what exists
when it starts; an earlier run accepted in an iteration where that set was
empty, and its `0 violations` was a true statement about nothing. A run whose
records do not carry the number is now reported as unreadable, never as zero.
The evidence is in `dogfood/planner-confinement/`.

**What stays open regardless.** This is detection, not prevention. An agent
running as the same user can reach any path it can name. What the witness
guarantees is that a run in which that happened cannot quietly produce a
verdict -- and that is one run, one task, one machine, not a claim about a
hostile planner.

## 12c. A criterion that never executed in either state is reported as failed

A criterion is red on the predecessor and green on the candidate: that is this
project's definition of demonstrating an increment. Limit 12a covers the case
where the predecessor was red only because the criterion's own file was not
there yet. This is the other end of it.

Measured on this project's own confinement run `cfnfib6a`. The criterion was
`python3 -m unittest discover -s tests -t . -v`, and **both** receipts -- the
baseline and the candidate -- end identically:

    ImportError: Start directory is not importable: '.../tests'

The interpreter on this machine carries a namespace package called `tests` in
its own `site-packages`, which shadows the project-local directory. Nothing
about `fib.py` could change that outcome. What the run reported was:

    not accepted: K1

True, and misleading. A criterion that fails at the same import boundary in
both states has said nothing about the product. It is not red; it is
**inconclusive**. HoH already has that state for the infrastructure exit codes
`{124, 126, 127}`; here the exit code is 1 and the distinction is unavailable.

The outcome was still safe -- an unproven criterion blocks acceptance, which is
the direction this project errs in deliberately -- and the run's QA role
diagnosed the collision independently and recorded it. But a reader of the
result sees a product verdict where there was none.

**Not silently folded into `artefactual_reason()`.** That function describes a
*baseline* that executed nothing while the candidate was green; here neither
side executed. Stretching it to cover both would make one number mean two
things, which is exactly limit 12's complaint one level down. Naming the class
and leaving it open is the honest option until the verdict machinery can carry
a third state through `_parse_qa`.

**Priority: medium, and open.** Related: the check environment is reduced, not
empty. `ENV_ALLOWLIST` and `PYTHONNOUSERSITE=1` keep the operator's home and
the user site directory out; the interpreter's own `site-packages` stay, and a
module name collision there reaches the check.

## 12d. The witness compares two moments, and QA works where it watches

`CapabilityWitness` digests a role's protected trees before the dispatch and
again after. Three things follow, and they are stated here rather than left to
be found:

**A change and its undo are one moment apart.** Measured: a commit inside a
dispatch moves the git digest, and `git reset --hard HEAD~1` in the same
dispatch moves it back. The witness sees the two endpoints and reports nothing.
The unreachable object stays in the repository, so
`tools/confinement_evidence.py` finds it afterwards with `git fsck` -- but that
is a post-hoc measurement, not the fail-closed check.

**QA's working directory is the arena root, which is also where the arenas
are.** That is the shape limit 12b objects to for the planner, and it has not
been fixed for QA. What was fixed is a false positive it caused: the arena root
was watched for new children, and `pytest` run against the candidate -- the
work QA is there to do -- left `.pytest_cache` beside the arenas and failed the
iteration. The listing watch is therefore off for QA. Every arena except the
one QA is reviewing is still digested in full; what is given up is noticing a
*new* directory created during QA's dispatch. The alternative considered and
rejected was a list of tool names to ignore, which is the growing exclusion
list this project rejects in the arena-placement argument.

**A full digest is cheap here and is not cheap everywhere.** Measured on this
project's own tree at 979,952 files and 5.2 GB: 121 s cold, 51 s warm, per
call, twice per planner and QA dispatch. The runs this harness has actually
executed measure 0.001 s, because a candidate arena is small. A repository of
research size in `repo_path` would make the witness the dominant cost of a
dispatch, and over a 51-second window any concurrent writer in the tree is a
finding. Nothing caps this yet.

**Two runs over one repository interfere.** The lock is per run
(`runs/<run_id>/.lock`), and the repository is in the protected set of the
planner and QA. A second run's developer writing the shared worktree during
the first run's planner dispatch therefore fails the first run. The message is
honest -- "a protected tree changed *during* planner's dispatch", not "the
planner changed it" -- but a run is blocked for something a role did not do.
Nothing in HoH claims to support two concurrent runs over one repository, and
now nothing pretends to either.

**Priority: medium, and open.** The structural fix for the second point is the
one the planner already got: a working directory containing what the role may
see and nothing else.

**Addendum 2026-10-05 (qa-arena-write): QA's candidate arena is now sealed and
frozen.** A dogfood run on 2026-10-03 showed the gap this section left open
for the arena QA reviews: QA created `.qa_probe` there and moved it out again,
and nothing noticed. What now holds:

* The arena is **sealed** for QA's dispatch: every write bit under it is
  cleared, with the plan (each entry's mode and inode identity) on the seal
  ledger `runs/<id>/seals.jsonl` before the first `chmod`. A seal that fails is
  rolled back and blocks the run before QA starts.
* It is **frozen** in QA's witness: contents plus each entry's mode, size,
  mtime and ctime. So the change-and-undo of the first point above is seen
  there -- a write moved out again, a `chmod` and back, a content write whose
  mtime was set back all leave a change time.
* **Every `Exception` way out** of the dispatch reads the witness: an answer,
  the final `DispatchError`, a crash.
* **Interrupts and kept-open panes keep the seal**: a `KeyboardInterrupt` or
  `SystemExit`, a `WaitingForApproval` and a `PaneNotClean` leave the arena
  sealed, because the pane may still act.
* A sealed arena is **checked at every later verification** and never unsealed
  there; it is **unsealed only after the tabs close** (`close_own` returned,
  nothing unconfirmed, and the run's QA agent gone from Herdr), comparing once
  more. A touch found then makes `hoh run` exit non-zero. A QA agent that is
  still present does not hide a touch: it keeps the seal, and the arena is
  compared all the same.
* **Every way a `hoh run` ends compares the seals it leaves in place**: tabs
  kept open for an approval, a tab not confirmed closed, a `close_own` that
  raised, a release that raised. A touch found there blocks the run and makes
  `hoh run` exit non-zero, instead of waiting for a verification that may never
  come. While a seal is open, the launcher reports the run UNDETERMINED (nothing
  is merged) and `hoh deliver` refuses it; a ledger that cannot be read counts
  as an open seal.
* **Every `chmod` is identity-verified**: opened inside the arena component by
  component without following a link, checked against the planned
  `(st_dev, st_ino)`, and changed through the pinned descriptor. A swapped
  symlink is refused, not changed.
* **A file with more than one name is never sealed and never changed.** One of
  its names may lie outside the arena, where a `chmod` would reach a file that is
  not ours, and the link count cannot tell the two cases apart. The seal refuses
  such a file before any `chmod` (the run blocks and QA does not start), and a
  later `chmod` of one is refused too, so a forged plan cannot aim at it.
* The ledger is **honoured only for this run's arenas**; a line naming any
  other path is refused as tampering and blocks the run. Only the first
  `planned` line of an open seal is its plan and only the first usable `sealed`
  line is its digest; a `touched` line is a record and never the baseline, so a
  touch is reported at every comparison and the ledger holds one `touched` line
  per distinct state found. Forged `planned`, `sealed` and `touched` lines
  therefore cannot hide a touch.
* Every capability violation now sets `blocked_kind = "capability_violation"`,
  which a re-block and a pause keep. While it is set -- or, for a run blocked
  before the kind existed, while a BLOCKED, PAUSED, CANCELLED or FAILED run's
  block reason names the violation -- the launcher never reports the run ACCEPTED (so nothing is
  merged), `hoh deliver` refuses it, `hoh resume` refuses it (even with
  `--force`) and returns a paused one to BLOCKED, the launcher never
  auto-unblocks it, and `tools/acceptance_record.py` does not count its
  checkpoint. **`hoh unblock` is the only command that clears the hold.**

What remains, stated rather than left to be found:

* **Root** ignores the cleared write bits; a role running as root is not
  prevented, only detected.
* An agent with a shell can **`chmod` first** and then write. That is not
  prevented; it is detected, because the `chmod` itself moves the change time.
* The ledger is protected during dispatches only, as `state.json` is. **A forged
  `released` line appended between dispatches hides a touch** of the arena it
  names, and a forged `releasing` line both hides a touch and gets the arena
  unsealed at the next verification without a comparison.
* **A run that is never run again keeps its arena sealed, and is neither merged
  nor delivered.** Its touch, if any, is found by the next `hoh run` that
  closes its tabs. `prune()` stops at such an arena: it cannot move a directory
  without its write bit.
* **The link count has a price.** It cannot tell a second name inside the arena
  from one outside it, so a criterion that makes hard links inside the arena (a
  package manager's installs, say) makes every QA dispatch of that run fail
  closed before QA starts. That is availability, not safety. Counting the names
  the walk finds inside the arena would admit such pairs, but it needs a count
  that holds at plan, seal and release time and is a new attack surface; it is a
  separate strand.
* The seal waits `SEAL_SETTLE_S` (0.05 s) so that a `chmod` right after it lands
  in a later clock tick; on a kernel with fine-grained timestamps the wait is
  merely unnecessary.
* The frozen digest reads the whole arena twice per QA dispatch, plus once at
  each later verification and at the release while it stays sealed: the digest
  cost of the third point above, now also paid for QA's own arena.
* A contract whose routing for the violation is STOP or ABORT sets its own kind
  (`contract_stop`, `contract_abort`) but keeps the block reason, and `hoh
  cancel` keeps it too, so such a run stays held: the CANCELLED gap is closed.
  `tools/benchmark.py` still counts a violation-held run as accepted; that is
  tracked LOW.

**Correction, after the integration review of the previous run (qa-arena-write,
what holds now).** The last point above, that a contract's STOP or ABORT sets its
own kind and the run "stays held" through the block reason, was true only until a
second block rewrote the reason; the claim that the CANCELLED gap is closed was
overbroad. The old text stays; what is true since this correction:

* The hold has one carrier, `blocked_kind`, and one writer rule. A contract that
  stops the programme or abandons the branch still cancels the run and records
  its routing, but it **keeps the violation kind** and notes that the run stays
  held; the park, collect and ask routings never replaced it. Only a person's
  `hoh unblock` clears the kind.
* **`hoh resume` refuses a held run in every condition but PAUSED**, with or
  without `--force`, and writes nothing. A held PAUSED run returns to BLOCKED
  with no reconciliation (it could only re-block the run and rewrite the reason
  that names the violation). **`hoh deliver` reads the hold first**, before the
  stage, the open seals, the contract and any git operation, in every condition.
* **A held CANCELLED run has no automatic way back.** `hoh unblock` takes a
  BLOCKED run only, so nothing lifts it by itself and nothing of it is merged or
  delivered; the way on is a new run. A later block with no contract in force
  turns it into BLOCKED, and a person's `hoh unblock` then lifts the hold.
* **The release compares the arena with the digest it was sealed with**, before it
  unseals. Every dispatch takes its witness when it starts, so a touch before
  the first dispatch, between two of them, or after the last one's check was in
  no witness; the comparison finds it, restores the modes, records it and blocks
  the run with the kind. A digest that cannot be read is taken as a touch. The
  price is one more digest of the arena per iteration.

What this correction does not close, stated:

* After a plain role-timeout `DispatchError` the arena is unsealed while the pane
  may still be working; a write after that unseal and before the tabs close is
  not witnessed. It accepts nothing (that attempt has no QA verdict, and the next
  one uses a fresh arena), but the touch is not seen. Fixing it needs the agent's
  status inside the dispatch code; it is tracked.
* `tools/attribution.py`, `tools/self_hosted_topology.py` and `tools/benchmark.py`
  read the accepted candidate as provenance and do not read the hold; the release
  gates do. Tracked.
* An ACTIVE run that carries the kind (no product writer makes one) can lose it
  to a capacity refusal in `storage_park`, which is outside this strand's scope.
  Tracked, with the guard that would close it.
* The legacy fallback of the hold reads the block reason's first line, which can
  hold an agent's own words; the effect is denial only, and a person's `hoh
  unblock` lifts it. Tracked.

**Addendum (qaw-followup, after the integration review of qa-arena-write).** What
that review found is closed or narrowed here, and what stays is stated:

* **Every automatic way back asks for the hold itself.** The `retry_after`
  fallback of `hoh resume-quota` (`resumeloop._legacy_run`) had no check, so a run
  whose kind was `usage_limit` and whose reason named the violation (a state an
  earlier build wrote), or a caller that named `capability_violation` among the
  classes it owns, was unblocked by its own `retry_after`. The timer path, the
  fallback and `hoh resume-storage` now each refuse a run `stages.violation_unresolved`
  holds, with the verdict NEEDS_OVERRIDE and the timer left armed. `park_for_storage_pressure`
  no longer parks a held run (the capacity-refusal gap listed above): it keeps the
  kind, keeps the reason, arms no timer and reports the run not parked. Project
  nodes are not covered: a node is `usage_limit` only through the launcher's
  provider verdict, which a held run never gives, and the launcher refuses a held
  run at launch.
* **`tools/acceptance_record.py` reads `seals.jsonl`.** A captured run with an open
  seal (a `planned` line with no `released` line, the rule of `open_seal_arenas`)
  is not an acceptance, and a ledger the tool cannot read as text counts as
  one. The evidence copy of a symbolic-linked ledger is a regular file
  (`copytree(symlinks=False)`), so the tool cannot see the link; the run it came
  from is held by the controller the next time it reads the ledger.
* **The seal ledger is written and read without following a link.** It is
  opened with `O_NOFOLLOW` and checked on the descriptor: a regular file with exactly
  one name. A symbolic link, a FIFO, a file with a second name (an append would
  write to a file outside the run) is refused: `durable_append(single_link=True)`
  raises before a byte is written, the gate (`open_seal_arenas`) counts the ledger
  itself as one open seal, and the controller blocks the run with the violation
  kind. A link to an empty file can no longer hide an open seal. **The price:** a
  ledger with a second name (a `cp -al` snapshot of the run directory) is refused
  too, which is availability and not safety. The directories above the ledger are
  not checked; the run directory's own identity is the store's concern.
  Telemetry and the succession log share `durable_append` and keep following links.
  HoH never repairs a ledger: the way out is a person's (move the link aside,
  which deletes nothing, put the real ledger back, then `hoh unblock`), and until
  then `hoh deliver` says the arena is still open.
* **A plan binds to its own arena.** A `planned` line that opens a seal is honoured
  only for the directory whose name this run's secret and the iteration and attempt
  on the line give (`controller.arena_name`, the function `_arena` uses). A forged
  line for another directory of the arena root (a sibling, the planner's copy, an
  arena of another run under a shared `scratch_dir`) is refused as tampering and
  blocks the run. **What this does not do:** the secret is in `state.json`, which a
  pane can read, so a pane that computes the name of a real arena of this run
  still gets its forged plan honoured (a mode change inside this run's own arena,
  which the pane could make itself). The ledger is a record and a restore aid, not
  an access control against a process of the same user.

**Correction by addition, 2026-10-07 (the review of the merge that carries this
addendum).** The last point above, and the sentence that `hoh unblock` is the
only command that clears the hold, hold for the states the current build writes
and not for a state written before the hold existed. Three findings that the
strand's own integration reviewer proved were not carried into the merge and are
open: `resume_runs` unblocks a legacy violation state to ACTIVE, so the launcher
can report the run ACCEPTED; `tools/acceptance_record.py` ignores the seal
ledger, so an open seal is not a problem there; and a `seals.jsonl` replaced by
a symlink gets ledger lines appended to the foreign file, with a forged
`planned` line for a sibling arena of the same run honoured. A follow-up strand
is being drafted; until it merges, the legacy fallback is **not** denial only,
and the first row of *Current open limitations* above is the standing statement.

**Closed for the replayed paths, 2026-10-07 (by addition; the correction above
stays as the record of what was open).** The follow-up strand merged as
`ab41589f` (run qfu5, accepted in iteration 1; both integration reviewers
released it yes/yes). The legacy-state resume, the ignored open seal, the
symlinked seal ledger and the forged sibling-arena plan line are refused on the
merged tree, each replayed by both reviewers; the qaw-followup addendum above
states how. The correction above was right when it was written and is wrong now
for those four paths: the legacy fallback no longer unblocks a held run. What
the strand states and pins itself stays open: a role pane that has read the
run's `arena_secret` and copies iteration and attempt gets a forged plan
honoured.

## 12e. Twenty-one published references point at documents the export does not carry

The export gate checks something most projects do not: that a document which
*is* published does not send a reader to a path that is *not*. Running it here
reports 21 such references, and they are old rather than new:

The five documents under `paper/` carry most of them, citing the internal
working documents they were written against: a closing report, several run
specifications, a source check. The release notes and the RC gate document do
the same, and this file does it twice itself, in the entry about the
specification that could not be corrected in place (limit 15).

The exact list is not reproduced here, and that is not coyness: naming those
paths in this entry would *add* dangling references to an entry about dangling
references, which is what the first version of it did. `python3
tools/export_manifest.py check` prints them, and it is the same command the
count above comes from.

None of these is a false claim. Each is a citation of provenance: *this
statement came from that document*, and the document exists in the repository
that produced the export. What a reader of the export gets is a reference they
cannot follow, which is a different defect from a wrong number and a smaller
one -- but it is the defect the gate is for, and it is reported rather than
suppressed.

Two honest options and neither is free: publish the cited documents, which
means publishing working notes written for an audience of one and, for the
`dogfood/specs/` files, absolute machine paths; or rewrite the citations as
prose that names the document without linking it, which costs the reader the
ability to ask for it. The second is what `docs/EVIDENCE_INDEX.md` now does
about the confinement README, and it is what this entry recommends for the
rest.

**Priority: medium, and open.** It blocks nothing technically and it is the
kind of thing that quietly stays broken, so it is written down with a count
that a re-run can check: `python3 tools/export_manifest.py check`.

### Closed on 2026-09-14, by the second option this entry recommended

The twelve references in living documents -- the RC gate, the release notes,
this file, and three files under `paper/` -- now name the internal document
without a path. `docs/EVIDENCE_INDEX.md` carries a table of them: what each
one is, and where its substance *is* published. A reader loses the ability to
ask for the file by path and keeps the credit and the trail.

The remaining nine are all in `paper/REVIEW_A.md` and `paper/REVIEW_B.md`, and
they are **not** being rewritten. Those are two independent reviewers' reports,
published as written. Editing a report so that its citations resolve would
make it say something the reviewer did not write, which is a worse defect than
a pointer that cannot be followed -- and one of B's own findings is *about*
these very references, so its paths are the subject of the finding rather than
pointers it offers. `tools/export_manifest.py` records the two documents in
`U2B_ANERKANNT` with the reason, prints every acknowledged reference on every
run, and `tests/test_export_manifest.py` pins the list so it cannot grow
without a test changing with it.

So: **0 unacknowledged, 9 acknowledged in 2 documents.** The count above is
what the gate reported before this, and it is left standing rather than
edited, because the entry is the record of what was found.

### Recount on 2026-09-14, at the v0.1.0 tag

The line above reads **0 unacknowledged, 9 acknowledged in 2 documents**. The
gate now reports **0 unacknowledged, 12 acknowledged in 5 documents**, and
neither number replaces the other: the first is what was true when that
paragraph was written, and this is what is true now.

Two of the three additions came with this release: `paper/AUDIT.md`'s source
column names the two evidence trees section 12 and 13's figures were
recomputed from, and neither is in the export (limit 12g says how they got
past the gate in the first place, which is the more interesting half). The
third was already there when the paragraph above was written and the paragraph
missed it -- `paper/AUDIT.md -> runs/a03/receipts`, acknowledged for the same
reason, in a third document. So the "9 in 2" was one document behind on the
day it was written. That is recorded here rather than corrected in place, for
the same reason the rest of this entry is.

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

## 12f. The local no-online-sync rule protects measurements, and does not gate the sanitised public export

This project is developed under an operator house rule that says: no cloud or
online sync, logging local, no measurements leave the machine. A session read
that as a blanket block on pushing anything to a public repository, and
reported the release sequence's push step as a hard gate needing a decision.
**That reading was wrong and is corrected here rather than quietly dropped.**

The rule protects *internal measurements and machine-local paths*: receipt
trees carrying absolute home paths, run states naming worktrees, raw telemetry.
It is the same concern that keeps those trees out of the export (limit 12e) and
the reason `tools/export_manifest.py` scans every INCLUDE file for home paths,
private addresses and token-shaped strings.

It is not a block on the public artifact. The captain has separately authorised
updating the public repository once `TECHNICALLY_STABLE_READY` is `YES` and the
public closure is green. Those two things do not conflict, and the way to keep
both is procedural rather than a judgement call:

* never synchronise the internal working tree;
* produce the **public export** and run the leak, path, manifest and claims
  audits over it;
* carry only INCLUDE-classified, public-safe artifacts;
* do it from a separate public staging checkout that has the remote, not from
  the tree that holds the run evidence;
* and keep the stable tag, the GitHub release and any package-index upload as
  their own separate approval, which they remain.

What the rule forbids is pushing the measurements. What it does not forbid is
publishing the product the measurements were about.

## 19. `benchmark_v3 = PASS` is a statement about the campaign, not about the product

The readiness row reads `PASS`. It is worth saying plainly what that does and
does not assert, because the two are easy to run together and the difference
is the whole point of having run the campaign.

**What it means, and only this:** the campaign was pre-registered before the
first dispatch; the exact registration bytes were bound in a commit before it;
all 45 runs completed; the matched budget was valid, meaning no cell exceeded
the ceiling the protocol declares; the raw data is complete; and the analysis
is reproducible from the cell files by a second, independently written
aggregation.

**What it does not mean.** It says nothing about VeriHarness being better than
a plain agent, nothing about it being more efficient, and nothing about the
control plane reaching a fixpoint. The campaign's own numbers say the
opposite of the last one.

### What the campaign actually measured

| arm | hidden suite | false accepts | end state |
|---|---|---|---|
| A -- plain agent | 15/15 PASS | 0 | answered |
| B -- one `hoh run` | 14/15 PASS | 1 | 15/15 accepted |
| C -- full control plane | 14/15 PASS | 0 | 15/15 `BUDGET_EXHAUSTED`, 0/15 `CLOSED` |

**Arm C's zero false accepts is not a correctness result.** A false accept
requires an arm to claim it is finished and be wrong. Arm C never claimed it:
under the budget the protocol declares, every one of its fifteen cells ran out
of dispatches before closing. `slug_pair/C/3` is the case that shows the
difference -- its hidden suite is red and no claim of success was made, which
is a miss rather than a false pass. Quoting `0 false accepts` for arm C beside
arm B's `1` compares *answered wrongly once* with *never answered*.

**Arm A, with one dispatch per cell, passed everything.** That is not a
rounding detail. Arm A is single-shot by construction (limit 17), so it spent
a ninth of what the harnessed arms were allowed, and it still produced the
most passing cells and no false accept.

**What the harness did demonstrate** is narrower and is worth stating on its
own: arm B's verification kernel produced one measurable false accept on five
small tasks, reproducing the same task and arm campaign v2 found, and arm C's
control plane detected a red global gate and dispatched a repair node in every
single cell -- it simply had no budget left to finish. Whether that repair
path still reaches closure when the budget is sufficient is a separate
question with separate evidence (`post_o143_closure`), because a ceiling that
refuses too early is indistinguishable from one that refuses correctly.

## 13a. A dispatch is charged before the provider answers, and a crash does not refund it

The dispatch counter is incremented and **written to disk before the call goes
out**, not after it returns. That is deliberate and it is the only order under
which the budget survives a crash: a count that reaches disk only on a clean
return is a count a killed process refunds, and a node that died after eight
dispatches would come back with nine more.

The cost of that order is stated here rather than discovered: a call that
never reached the provider at all -- the process was killed in the moment
between the charge and the call, the machine lost power, the harness failed to
start the agent -- is charged anyway. Under a nine-dispatch ceiling a run that
is interrupted twice that way has seven left, not nine. There is no automatic
correction, because distinguishing "charged and never dialled" from "dialled
and the answer was lost" would require a record written by the far side, and
this project does not have one.

What exists instead is visibility: `telemetry.jsonl` records
`provider_calls` per dispatch record, so a charge that produced no call is
`provider_calls: 0` on the line and can be counted by a reader.
`tools/budget_evidence.py` control K3 measures the order itself.

## 13b. One shared budget assumes the runs under it do not overlap

`HohRunLauncher(dispatch_budget=N)` derives what a new run may spend by
reading the persisted spend of every run under its root at the moment the run
is started. `ProjectController` starts nodes one at a time, so under this
project's own control plane the figure is exact.

It is **not** a lock. Two launcher processes over the same root, or a caller
that starts two runs concurrently, would each read the same remainder and each
hand it out in full -- the classic read-then-write race, and the reason it is
harmless today is a property of the caller rather than of the budget. Making
it safe under concurrency would need the charge to be taken at start time
rather than derived at prepare time, which is a different design and is not in
v0.1.0. Anyone composing `HohRunLauncher` into a parallel scheduler has to
serialise the starts.

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
the internal *d5-paper* specification -- at a point when `docs/LIMITATIONS.md` held 12
limits. By the time that run actually ran, run `d3e` had already merged limit
13 and limit 14, and the file held 14; that mismatch, and this same digest
mechanism as its cause, is recorded in
the internal *d5l-limits-from-the-file* specification. The running specification could
not be corrected in place -- the same immutability this entry describes applied
to it too -- so the fix did not happen inside that run; it became a separate
one.

### There is a supported path now (2026-09-13), and it is not a shortcut

`hoh amend <run-id> --spec-file <new> --kind <k> --actor <who> --reason <why>
[--affects K1,K2]` records a change instead of forbidding one. What it does:

* the old text is **parked and stays readable** -- `park_and_amend` renames it
  with its own digest in the name, so "what did this run promise when
  iteration 3 was accepted" has an answer;
* the chain is stored beside the state in `amendments.json`, and it validates
  continuously: an amendment whose `from_digest` is not where the chain left
  off is refused, because a chain with a gap cannot say what was promised at
  any point in it;
* the run's own `spec_digest` moves with the chain, so a *recorded* amendment
  does not block -- and an unrecorded edit still does, which is the protection
  this entry was about;
* an acceptance-affecting amendment names the criteria it touches, their
  receipts are marked as belonging to a superseded specification, and **the
  controller withholds acceptance** until each of them has been planned *and*
  measured again. Planned and measured, not one or the other: a plan that
  drops the criterion while QA mentions it anyway would otherwise satisfy the
  gate by talking about it.

`tests/test_amendment_e2e.py` drives that path on a real controller: an
accepted candidate on the old text, an amendment naming its criterion, a
second candidate refused for exactly that reason, and a third accepted once
the criterion is re-measured. Its negative control is the refusal, and there
is a positive control beside it -- a `CLARIFY` that affects nothing withholds
nothing, because a feature that stopped every run would be one people route
around.

**What is still true.** Nothing here judges whether an amendment is honest.
A person with commit access can always edit a file, and an amendment that
narrows a criterion until a failing candidate passes is *recorded*, not
prevented. The record names the author, the kind, the reason, the criteria and
whether the change came after an acceptance; reading it is a person's job.

And one thing the first version of this section claimed and could not deliver:
that the amended criterion is measured "against the new text". It was not. A
preserved criterion is frozen against redefinition -- correctly, it is how a
plan is stopped from watering one down -- and an amendment's affected criteria
are by construction ones that already passed, so all of them were frozen. The
planner's new definition was silently discarded and the receipt recorded the
pre-amendment command. An amendment now **reopens** its criteria: they leave
the preservation suite, become fresh criteria that have to discriminate again,
and the new definition is the one that counts. What remains a person's job is
noticing an amendment whose "new" criterion is the old one retyped.


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

The checks that **do** hold in a published clone are `check schema` and `check
ids`, and `check all`'s own shape rule: every failure it reports carries an
environment-gap marker, **except the rendering difference this entry is
about**. That rule, not an exit code of zero, is what a reader can verify
here. `check coverage` exits 1 in a clone as well; what holds there is the
same shape rule, every one of its failures marked.

### Corrected 2026-09-14, by measurement rather than by re-reading

The sentence above used to say there is *no* unmarked failure, and it invited
a reader to check exactly that. Run against a real export tree it was wrong:
167 of 329 failures carried no marker. Two different things had been reported
as one. An **anchor** that cannot resolve because its file is absent was
marked; an **evidence reference** to the very same absent file was not, and
163 of the unmarked failures were that.

The asymmetry had no justification, so the tool was changed rather than the
claim weakened to fit it: `check_claims.py` now asks `EXPORT_MANIFEST.json`
whether an absent path is classified `EXCLUDE`. If it is, the file was never
shipped and the failure is an environment gap; if it is not, the evidence is
genuinely missing and it stays a content defect, which is the direction that
fails safe. Measured again afterwards: 4 unmarked lines, all four the single
rendering difference described above.

Worth stating plainly, because a claim that tells a reader how to verify it
and then does not survive being verified is the worst kind to publish: this
was found by building the export and running the check in it, which is the
only place the claim was ever about.

## 17. The benchmark's arms do not spend the matched budget symmetrically

`docs/BENCHMARK_PROTOCOL.md` §"Matched budget" says each arm gets at most nine
role dispatches per task and that "A gets nine turns". Arms B and C are given
that ceiling and, since O144, enforced against it. **Arm A is single-shot.**
`tools/benchmark.py`'s `arm_a` makes exactly one provider call: one agent turn
with the specification and the repository, no loop above it. Every arm-A cell
in campaigns v1 and v2 records one dispatch.

So the budget is matched as a ceiling and not as an allocation, and the arm
that is supposed to be the baseline anybody already has is given a ninth of
what the harnessed arms may spend.

The direction matters and is not the convenient one: more turns could only
help arm A. A campaign in which A matches or beats B and C is therefore robust
to the asymmetry -- which is what v1 and v2 found, A passing 5 of 5 in both.
A campaign in which A lost would **not** be evidence that the harness is
better, because A was not given the resource the protocol promised it.

Campaign v3 declares the asymmetry rather than repairing it: rebuilding arm A
into a nine-turn loop would make v3's arm A incomparable with v1's and v2's,
and a benchmark that changes its baseline between campaigns cannot replicate
anything. The declaration is in the v3 pre-registration, and any comparison
drawn from v3 carries it.

## 13c. A retry's own bookkeeping is the one write the capability witness cannot attribute

`Controller._dispatch` takes a capability witness before a role runs and
compares it afterwards. A transient retry has to charge a dispatch before
calling the provider again, charging means persisting `state.json`, and
`state.json` is one of the trees the witness protects -- so the controller's
own bookkeeping would read as a violation by the role.

The first attempt re-took the **whole** witness at that point, and an
adversarial review broke it: a write into `checks.json` landing in the same
window was folded into the new baseline, the run continued with a hijacked
acceptance suite, and nothing was recorded. That is the defect class
(O125/O129) the witness exists to catch.

What exists now is `CapabilityWitness.neu_bezeugen`, which re-takes exactly
the paths it is given. The controller gives it one: `state.json`. Every other
protected tree keeps the baseline it was taken with, so a write anywhere else
during a retry is still caught.

**What is still uncovered**, stated here rather than left to be found: a write
to `state.json` *itself*, by anyone, between the controller's write and the
re-witness a few microseconds later. No digest can attribute that one -- the
controller is rewriting the same file -- and the honest reading of a
`state.json` digest during a retry is therefore "not attributable", not
"unchanged". This is detection, not prevention, and `capability.py` says the
same thing about itself one level down.

## 18. The benchmark's instrument is frozen by digest; what it runs on is not

`tools/prereg.py freeze` records a digest for every file that can change what
a campaign measures, and `check` reports anything that moved. That establishes
that the **files** were the same on the day of the first cell and the day of
the last.

It does not establish that the **instrument** was. Not frozen, and not
freezable by this mechanism:

* **The model.** `tools/benchmark.py` dispatches the harness profile
  `claude`, which resolves to whatever that CLI chooses at run time. There is
  no model pin anywhere in the benchmark path. A campaign spanning a provider
  upgrade would measure two instruments and report one.
* **The agent CLI itself**, its version and its defaults.
* **Installed package versions.** `pyproject.toml` carries floors
  (`pydantic>=2.0`), not a lockfile.
* **Environment variables interpolated into role prompts**, most importantly
  `HOH_HOUSE_RULES`: present or absent, it changes every prompt the arms
  receive.

The registration records all of these as they stood at the freeze --
interpreter version, platform, package versions, `claude --version`, and a
digest per prompt-relevant variable -- so that a reader can *see* whether they
moved. Seeing is what this offers; guaranteeing is not. A campaign whose
result matters should be run in one sitting, and one that was not should say
so.

## 12g. The export's reference check only sees a path that is quoted

Limit 12e is about references the gate *finds*. This one is about the ones it
does not.

`check_u2b` harvests path candidates from backtick spans, markdown links and
include-like directives. That is a deliberate choice and the module says why:
a bare word in prose is usually not a pointer, and treating every
slash-shaped string as one would make the check unusable. The consequence is
that **a path written into a table cell without backticks is invisible to
it**, and a table cell is exactly where a provenance column lives.

This was found while preparing v0.1.0 and is recorded as O167. Sixteen rows
were added to `paper/AUDIT.md` whose source column names the evidence tree a
number was recomputed from; two of those trees are not in the export; the gate
ran green. Worse for the gate's credibility than the miss itself: the one
acknowledged exception that existed before them, `paper/AUDIT.md ->
runs/a03/receipts`, had only ever been caught because the same path also
appears *backticked* in that row's detail column. The precedent was not
evidence that the detection worked.

Two gates also want different things from that one cell.
`tools/audit_refs.py numbers-recomputed` requires the source column to be a
real path it can `stat`, so backticks there would break it; `check_u2b` only
sees the path if it is backticked. The references at hand are now named in the
*detail* column in backticks, so the gate sees them, and both are
acknowledged with reasons naming a published document a reader can follow
instead. The acknowledgement rule was tightened rather than relaxed while
doing it: an exemption used to have to mention `docs/EVIDENCE_INDEX.md` by
name, which the two new ones cannot honestly do -- that file describes three
other evidence trees, not these -- and now has to name **some** document the
export actually carries, checked against the manifest.

**What stays open, deliberately.** The detection itself is unchanged: a bare
path in a table cell is still invisible, and the next person to add such a row
will not be warned. Widening it means touching a gate with its own fixture
suite, and the hour before a release tag is the worst possible time to do
that -- a gate written then is a gate whose negative control nobody runs.
Priority: high, for the release after this one. Second, smaller, open point:
`docs/EVIDENCE_INDEX.md` does not describe the benchmark and closure evidence
trees, so an acknowledgement for those two can only point a reader at the
published result document, not at a description of the withheld tree.
Priority: medium.

## 20. The export is guarded now, and the guard is narrower than it sounds

For most of this project's life the export was a copy: every `INCLUDE` path
from the internal tree into the public checkout. That held while the internal
tree was the only place anything was written. It stopped holding when the
distribution work was done **in the public repository** -- three merged pull
requests touching fourteen files, nine of which the internal tree also
carries. A copy would have written the older internal version over six of them
and reported success, because it compared nothing.

`tools/export_sync.py` compares three states per path before it writes
anything: the digest recorded the last time the two sides were deliberately
synchronised, the internal file now, and the public file at the current head.
Only "ours moved, theirs did not" is written. "Theirs moved, ours did not" and
"both moved and disagree" stop the run **before the first write**, name the
paths, and say that integrating them and recording a new base is the only way
forward. The public head is read again immediately before a push, because a
head that moved in between makes the comparison a statement about a tree
nobody is pushing to.

**What it does not do, and none of these is an oversight.**

It does not merge. It stops and reports; a human or a later session decides
whose version survives. A tool that picked a winner would be the thing this
entry is about, with an extra step.

It does not delete. A path the base knows and the internal tree no longer
carries is reported and left in place. That is the house rule, and it also
means a genuine removal has to be done deliberately somewhere else.

It does not use timestamps. An mtime survives a copy, a checkout and a
restore, and the question here is provenance, not age.

It exempts nothing, and that took two corrections to arrive at. The first
draft carried three paths copied out of a different gate's tolerance list:
`CLAIMS.md`, `docs/READINESS.md` and `CLAIMS.json`. The ledger came out after
an attack on the guard showed that exempting it would have silently reverted
six claims the distribution work added in the public repository -- the defect
the file exists to prevent, reintroduced by its own exemption list (O174). An
independent review then asked why the other two were exempt at all, and the
answer did not survive the question: "generated" describes the content, not
the decision, and a file being regenerable says nothing about whether someone
added something to it that our regeneration will drop -- which is precisely
what happened (O176). The exemption was also unnecessary. The case it was
written for, this project rewriting those files on every gate run, already
reads as "ours moved, theirs did not" and is already written.

It checks the tree it writes into, which the first version did not. The
comparison is about the public head; the files land in a working checkout,
and those are not the same thing. Uncommitted work there -- staged, unstaged
or untracked -- was replaced by our version and the run reported success,
until a review reproduced it. The checkout must now be at the head the
comparison was made against, and nothing we would write may collide with work
that is present and uncommitted. Neither is resolved automatically: no reset,
no stash, no removal, and a test asserts that none of those appears in the
file at all.

It cannot tell a deliberate divergence from an accidental one. "Recording a
new base" is a human act, and the tool believes it.



## 21. Role panes are closed by HoH, and the close proves less than it sounds

HoH now ends a role's session when the role's turn ends, and closes whatever it
opened when the run ends. Both rest on the same closing rule, and the rule
proves less than "closed" suggests. This entry says what it proves and what it
does not.

**What a close proves.** After a close the Herdr snapshot no longer lists the
pane or its agent, and Herdr itself answers that the pane is not found. That
session cannot take another prompt and cannot wake up: the developer that ended
its turn "waiting for the notification" (run qaw) can no longer edit the tree
after the freeze, because it is not there.

**What a close does not prove.**

- *That what the agent started has ended.* Closing a pane ends the agent and its
  shell, not the commands the agent started in the background: measured live, a
  background `sleep` stayed alive, orphaned, after `herdr pane close` returned
  ok, and Herdr's documentation is silent on it. That is why the processes of a
  closed pane are looked for and ended separately (below).
- *That the ledger is evidence.* `pane-lifecycle.jsonl` is a record, not a gate.
  It sits in the run directory beside protected evidence but is not in the
  capability witness's set, because the dispatcher appends to it during a
  dispatch, so a role that can write there can falsify it. Nothing decides from
  it: ownership comes from the creation record and the live snapshot.
- *That a crashed `hoh run` is cleaned up.* A run that dies leaves its panes
  standing, and `hoh cancel` on a run with no live process closes nothing. A
  later run adopts a pane by agent name and directory, and the turn end of an
  adopted pane cannot say what its role left running (it has no mark).
- *That the transcript of an accepted turn survives.* It goes with its pane.
  The answer files and the receipts are the evidence; the pane is a place.
- *That a dialog raised after the answer was seen.* A role that answered and
  then sat at an input prompt is reported `blocked` by Herdr. The turn-end
  release closes that pane: nothing HoH asked is waiting for the dialog. A dialog
  raised during the turn keeps the pane open instead.
- *That a pane id still names the pane HoH made.* Herdr documents that closed
  ids are not reused for a running server, and says nothing across a restart. So
  every close re-checks the live pane: an agent in it must be one HoH recorded
  there, and with no agent the pane's directory must lie under the directory HoH
  made it in. **That check is not a proof**: an operator's shell in the very same
  directory, holding a recorded id after an id reuse, passes it. Reuse across a
  restart is suspected, not measured.
- *That a tab is only ever closed whole when it is all ours.* A whole tab closes
  only when the snapshot proves every pane in it is HoH's own, passes the
  identity check and is not kept open. A window of milliseconds remains between
  that snapshot and the command: a pane the operator opens in the tab in that
  window is closed with it. A single pane close has no such window.
- *That there is one path.* Exactly one other place closes a pane: the
  replacement of a restored pane whose input box is not provably empty. It keeps
  its own, stricter proof and raises where the closing rule returns, but writes
  the same ledger lines. A source scan pins that no third function spells a close
  command; it is a tripwire for the literal spelling, and code review is the
  other half.

**The role's processes.** Every role pane is created with a mark in its
environment, and every process the role starts inherits it. Before the pane is
closed HoH takes the pane's root (its own shell, a session leader directly below
the Herdr server) and the role's members: the root, everything in the root's
session, and every descendant of those, by structure and not by mark. After the
pane is closed the members are signalled one process at a time, with SIGTERM, a
grace period, and SIGKILL, in rounds, and a final look at a fresh snapshot names
any member still alive, including one born during the last round. Never a group,
never a pid without the start time that was seen, never pid one, this process, its
ancestors or what it started.

- *What it does not find.* A command that scrubbed its mark, left the session
  and was already detached from its parent when the pane was released. Unmarked
  processes that merely hold files in the work tree are not touched or counted.
  A pane that is already gone has no session to anchor on, so a command whose
  mark cannot be read is not found there at all.
- *A shared daemon is named and never ended.* A server that a role started first
  (the lavish and chrome-devtools helpers are examples) inherits the mark and
  detaches. It is reported as escaped, recorded, never signalled, and it blocks
  the developer's release: the developer is not frozen while one exists. That
  may be costly on a machine where such daemons are common.
- *The root rule depends on the server's name and on the server not adopting
  orphans.* Orphans are adopted by a subreaper (on the machine this was
  measured on, the relay above Herdr), not by init, so a detached marked daemon
  is also a session leader with an unmarked parent. The root is therefore
  accepted only below a process named exactly `herdr` that leads its own
  session, and never merely "the candidate that is left". If the server is not
  named that on some install, every developer release is refused, visibly, with
  nothing signalled. If a future Herdr became a subreaper, the release would be
  refused without ending the role's command, which fails closed.
- *A pane that is already gone* is not given a root: its marked leftovers are
  named and the release is refused, and nothing is signalled.
- *The interpreter may have no pidfd.* Where `os.pidfd_open` and
  `signal.pidfd_send_signal` exist the signal goes through a pidfd opened before
  the start time is read, so a reused pid cannot receive it. Where they do not
  (the default interpreter of the machine this was measured on has none) the start time is re-read and
  `kill` follows, and a window of microseconds remains in which a pid could be
  reused.
- *What the live smoke has not shown.* That the mark given to `tab create`
  reaches the commands an agent's own shell tool starts, and that a closed pane
  answers `pane_not_found`, are argued from the same channel and from Herdr's
  schema, and are checked by the orchestrator against a live Herdr, not by any
  test in this repository.
- *The owned-agent registry is HoH's own record, not a barrier.* The file
  `owned-agents.jsonl` in a run's directory is written by HoH's own process; a
  process of the same user that can write that directory can forge or remove a
  line. A pane that was never released (a crash, a release line that could not
  be written, a restarted Herdr) stays owned on disk and is recalled by the next
  process's close. Herdr does not reuse the id of a closed pane within one
  server's life, but may after a restart, so across a restart the older identity
  check (the agent name recorded for the pane, the directory it was made in) is
  the only line left. Restart recall needs the registry directory's name to
  equal the run id, so a layout that names it differently recalls nothing (it
  fails safe: the panes stay open). The program scope of the registry's role
  type is exercised by one test only: no product path starts a program-scope
  agent yet. A live run and a restored pane against a real Herdr are NOT_RUN.

## Update after v0.3.0a2, 2026-10-09: disk-full-safe writes read PROVEN, on text-level evidence and with limits

The row "Disk-full-safe writes are partial" in the table at the top of this file
and the sentence of the first alpha that only part of the writers are disk-full
safe are the text of the first alpha and of `v0.3.0a2`; they stay as written. On
the development line the third and last slice of the P2-41 closure is merged and
the capability matrix reads P2-41 PROVEN (ledger entry O261). What that rests on,
and what it does not:

* *What the evidence is.* The writer scan (`tests/test_durable_writers.py`) fails
  on any raw write call that it does not classify, and the fault-injecting tests
  of the durable primitives and of the migrated writers inject a full disk, an I/O
  error and short or zero-byte writes. The scan is lexical: it does not see an
  aliased call, `getattr`, `codecs.open` or `io.FileIO`. The matrix itself
  measures an import statement and a collected test, not an execution.
* *Helpers that keep their own code.* The storage modules write with their own
  temp-fsync-publish helpers (the scan's `OWN_DURABLE` class), not with
  `durable_write`. The host helper and the bundle helper have the full-disk tests
  of this closure; the host helper has no zero-byte test, and the bundle helper
  has no fsync-error test (removing its fsync goes unnoticed). The coordinator and
  the relocation helpers have an ENOSPC injected at their own write function. The
  registry writer has a test of a failed replace and none of a full disk. The copy
  helper of the archive target, the tar stream and the no-replace link of the
  bundle code have no test of a full disk at all. A failed directory fsync in the
  bundle code is silent by design (best effort).
* *Not claimed.* A long unattended run that survives a full disk end to end (park
  and resume on storage pressure belongs to P2-40). A backup that holds a
  hard-link member cannot be restored (restore refuses it). The leaf write, the
  audit append and the recorded answer of the dispatcher are now refused under the
  one-mebibyte free-space reserve, which the recorded answer did not obey before.
  Some behaviours of the new code are unpinned by a test and tracked in the ledger
  entry.

## Update after v0.3.0a2, 2026-10-09 (ledger O262): P2-43 reads PROVEN, with limits that matter

The rows and paragraphs above that say P2-43 reads MISSING, that the third slice is not merged, that nothing checks the
binding on resume, and that developer-side limit detection is not built are the text of the first alpha and of
`v0.3.0a2`; they stay as written. On the development line the third and fourth slices of P2-43 are merged and the
capability matrix reads P2-43 PROVEN (ledger entry O262). What that rests on, and what it does not:

* *What the evidence is.* One test file drives the real controller and resume loop, on fakes, through the four scenarios
  of amendment A5 (a limit in QA, an expired credential, a stale master, two masters); a second drives the real
  dispatcher, controller and resume loop on a recorded real limit screen and on a developer whose own words mention
  limits. The matrix itself measures a symbol, a collected test file and an import statement, not an execution.
* *What the detection reads.* Only the recorded shapes of the provider's banner, whole. A banner in another wording (a
  hyphenated hour limit, a weekly limit, a bare phrase, German text, the form with the word again alone) is not read, and
  the turn reaches QA as it did before. A line of a role's own words that is exactly a recorded banner, glyph and all,
  among the last twelve lines is read as the banner.
* *The stale-banner re-park (open).* The pane of a developer is reused and keeps the old limit line in its scrollback. A
  short good re-run after the wait leaves that line among the last twelve lines, is read as a limit again and is parked
  again and again until the resume limit, and is then handed to a person. It fails safe: nothing is accepted wrongly. The
  fix (ignore a banner that was already on the pane before the prompt was sent) is specified and in a run, and not merged.
* *Never recorded.* No developer pane has been recorded on a real limit; the status area is the harness's, so the screen
  is inferred to be the same. No real rate-limit body was recorded, so that branch is driven by synthetic text. The
  non-Herdr adapter reads a limit on an empty standard output only.
* *A person's escape hatch.* `hoh unblock` and `hoh resume --force` lift a park without the verification and without a
  claim, by design.
* *NOT_RUN.* A live developer turn that meets a real provider limit, a live rate-limit body, a live resume against a pane
  that continued by itself, a live race of two resuming processes, and a live run that meets a real limit with receipts.

## Update, later on 2026-10-09 (ledger O264): what the merges after the first update change, and what they leave

Nothing above is edited. Where a sentence above says that something is not merged and it is merged now, this section is the
correction; where it says that something does not exist and it exists now, this section says how far.

* *The stale-banner re-park of P2-43 (supersedes the bullet "The stale-banner re-park (open)" of the section above).* A
  limit banner that the developer's pane already showed when the prompt was sent is not read as this turn's. A pane that is
  still limited when the prompt is sent stays a failed dispatch. What stays: a banner with the same words after a turn longer
  than the window is missed unless its status-area line is read; that backstop exists for usage limits and not for a rate-limit
  body, so a repeated byte-identical body is missed; a status-area line that a healed pane left in its scrollback is read, and
  the run is parked again until the resume limit and then handed to a person (reproduced in review); an unreadable baseline is
  none, and so is the baseline of a turn awaited after a stalled send; no developer pane that survived a limit wait has been
  recorded, so how often any of this happens is SUSPECTED. The register still reads P2-43 PROVEN, with these limits.
* *Ephemeral temporary roots (P2-42, still PARTIAL).* `hoh run` for a run of a registered project registers, admits and
  releases the scratch of its acceptance checks and of each iteration, and no longer makes the runner's sibling scratch. The
  role scratch is not a registered root, a run of an unregistered project keeps the old scratch, a workspace whose creator
  died is not reclaimed, and arenas and output sinks are run-store artefacts outside the row. `hoh resume-storage` unparks a
  run only when the role and the iteration's workspace would be admitted together. A live run of the product, a really full
  disk and a race of two processes for one session are NOT_RUN.
* *The merge gate as a command (P5-36, PARTIAL).* `hoh merge-gate check` aggregates the required review reports of one
  candidate and `verify` re-decides a receipt. A report counts only with a complete six-line footer that the reviewer
  declares complete, bound to the candidate, base and spec digest, and not modified in the last minute; a partial or cut-off
  report is no verdict. It is not wired into any merge step, the receipt is not signed, the merge message is not generated
  from it, and the build's own merges still use the orchestrator's script. The command has not decided a merge.
* *Waiting on a run (P5-43, MISSING).* `hoh wait RUN` is read-only and answers one JSON line and an exit code. It does not wait
  for a report or a process id, keeps no list of what it reported, and does not close anything. `idle` is a verdict about the
  moment of the look, and `parked` says that a resume loop will lift the block, not that one is running. Nothing ran against a
  live Herdr session.
* *Owned surfaces and their reaper (P5-39, PARTIAL).* `hoh surface open` records a tab as a run's own; `hoh surface reap`
  closes it when the run is over (failed, cancelled or ready for delivery, with no live process) and the shell in it is proven
  idle by a scan of every user's processes. A checkpoint is not the end of a run, a foreign tab is never closed, and anything
  that cannot be proven is kept. Reviewer and drafter tabs are not covered, closing a tab is expected to hang up its shell, and
  what a real Herdr does is NOT_RUN.
* *Criterion fidelity (supersedes the row "The planner re-types the specification's criteria" above, for specifications that
  declare a block).* A specification that states its criteria in one fenced block is held to them: a plan that drops, re-types,
  wraps or deselects one is refused. A specification without the block is accepted as before, with "criterion fidelity
  NOT_RUN" in the reason; the older specifications are not back-filled. The preflight of every criterion in the run's own
  isolation, before quota is spent, is outlined and not built.

* *The figures of the development line, taken again.* At the commit of the records entry O264 (`dff02f08`, committed on
  2026-10-09 at 22:14 +0300; the figures quoted above for the O262 records commit stay as written) the history is 1,130 commits
  `[V48]` and 236 numbered ledger entries, O23 to O264 `[V49]`. Of the development nodes after the attribution anchor, 104 were
  developed by a product run and merged by the orchestrating session after review `[V06b]` and 128 were written by the
  orchestrating session directly `[V06c]`. The seven product-run merges described in this section were decided by the orchestrator's own script,
  not by the product's `hoh merge-gate`.

## Update, 2026-10-11 (ledger O265): what the merges after the second update change, and what they leave

Nothing above is edited. Where a sentence above says that something does not exist or is not merged and it is merged now, this
section is the correction; where it says that something is true and it has changed, this section says how far. No requirement is
lifted by this entry.

* *Waiting (supersedes the bullet "Waiting on a run (P5-43, MISSING)" of the section above; the row still reads MISSING).*
  `hoh wait` waits on a run, on a review report (`--report PATH`) and on a process id (`--pid N`). A report counts as final only
  after the stable age of the merge gate, a late footer starts the age again, and a FIFO, a directory or an oversize file end the
  wait at once as unreadable. A process is identified by its id and its start time, never by a name; a reused id or a zombie ends
  the wait, and no signal is sent. A candidate-less final stage and a storage park that no timer will lift read `held`, an accepted
  run whose process died says so, and a closed standard output exits with a refusal. Still not built: the durable list of what
  was reported (clause C7) and the trigger of the reaper (clause C9). Nothing ran against a live Herdr session.
* *Temporary roots (P2-42, still PARTIAL; supersedes the bullet "The role scratch is not a registered root" of the section
  above, for a run of a registered project).* The role scratch of such a run lives below the iteration's registered workspace, a
  pane whose temporary directory is not the bound scratch and an adopted pane are refused, and a refusal keeps the pane open.
  The operator ruled that the row reads "for a run of a registered project". What still holds the row: no run of a registered
  project has been observed, a live run with real panes and the other harnesses' temporary files are NOT_RUN, and the
  approval-resume of a registered run cannot adopt the kept pane. A run of an unregistered project keeps the old scratch.
* *Handing the orchestrating session over (P2-14, P2-20, still PARTIAL).* The rotation policy, its controller and the creation of
  the governed project record are merged and tested with fakes, and fail closed. No rotation has been run by the product; the
  register holds P2-14 at PARTIAL on purpose, because without the hold the import of the module alone would read PROVEN. The
  dress rehearsal on a synthetic transcript is not evidence. A handoff within the file-system timestamp granularity of its request
  waits until the request expires, and a policy that names a transcript path is inherited by the successor.
* *The detached run (P5-42, MISSING).* `hoh run --detach` opens an owned run-host tab, types one command of safe words and
  returns when a new process marker appears. A refusal that only the tab can make reads "started"; the named profile, the
  timeout from the specification, the dedicated pinned clone and the derived contract are not built; whether Herdr behaves as
  documented is NOT_RUN. A run id is refused unless it is a plain direct child of the runs root.
* *Pin and import (P5-38, MISSING).* `hoh pin` builds a specification-only pin commit by plumbing on a base the product resolved,
  create-only, with a digest trailer; `hoh import` verifies an accepted candidate and writes a receipt. Both write git objects to
  the repository they are given. The topology check, the scope-of-diff check and registered receipts are not built, and a live
  import of a real run is NOT_RUN.
* *The per-run storage account (P5-40, MISSING).* `hoh storage account` reads bytes and entries over registry-owned paths, counts
  a hard link once and never answers zero for what it could not measure. It does not enter `hoh status`, produces no receipt and
  admits nothing on entries; its command pre-checks are unbounded and a file can change under the walk.
* *The merge gate (P5-36, still PARTIAL; supersedes the sentence "the build's own merges still use the orchestrator's script"
  of the section above).* The report read is guarded: a FIFO, a socket, a device, a directory, a symlink or an oversize file is
  unreadable, never a hang. The orchestrator's script and the product command disagreed on more than half of a probe set; since
  `1e633e46` the product command has been the gate of record, every merge since has carried both receipts, and a replay of the older
  receipts found that many merges had been made on a reviewed base that was no longer the first parent (the product refuses that).
  No merge step of the product calls the command.
* *Owned surfaces (P5-39, still PARTIAL).* The reaper refuses a recorded directory that is a mount point or too broad, and an
  unreadable mount table refuses every listed surface. Reviewer and drafter tabs are not covered; a cleanup script
  closed sixty reviewer and run tabs on a rule that read an idle agent as finished, which is the case the product feature of the
  surface book is for (drafted, not merged).
* *The export leak scan.* It reads the bytes of every exported file, a UTF-16 reading of them and the name, refuses a private
  session or scratch identifier by digest and any session-shaped UUID that is not on a short list of synthetic ones, and treats an
  unreadable file as a finding. A name carrier prints its path (limit L8) and a UTF-16 text at an odd offset is not read (limit
  L9). The list of digests names only the identifiers known when it was last extended.
* *A phase is closed at a commit (amendment A7).* The rule that later changes to the source create regression obligations and do
  not re-open a closed phase is recorded as an amendment of the production line. No phase is closed by it: the gate of P2 reads
  open.
* *The figures of the development line, taken again.* At the commit of the records entry O265 (`d88281da`, committed on 2026-10-11 at 03:44 +0300; the figures quoted above for the O264
  records commit stay as written) the history is 1,197 commits `[V48]` and 237 numbered ledger entries, O23 to O265 `[V49]`. The first
  and the latest commit of the mainline are 34 days 3 hours apart `[V46]`, and 25 of the 35 calendar days carry a commit `[V47]`. Of
  the development nodes after the attribution anchor, 125 were developed by a product run and merged by the orchestrating session
  after review `[V06b]` and 155 were written by the orchestrating session directly `[V06c]`. The product's `hoh merge-gate`
  decided the merges recorded in this section from `1e633e46` on, as the gate of record, with the orchestrator's script beside it.

## Update, 2026-10-11, later (ledger O265, second revision): the first rotation by the product has run

Nothing above is edited. This section supersedes, for P2-14 and P2-20, the table row "Context rollover (Layer A) is merged and has not
run live", the sentence of the first limit that the product's rollover protocol "has not run live", and the bullet "Handing the
orchestrating session over (P2-14, P2-20, still PARTIAL)" of the section above. The record is under `dogfood/canary-a7/`.

* *Handing the orchestrating session over (P2-14 and P2-20 now read PROVEN).* On 2026-10-11 the product rotated the
  orchestrating session of the build by itself: `hoh project hold --rotate-policy` requested the rotation because the measured context
  was above the threshold (not lowered for the canary), the master wrote its handoff as consent, the controller froze a capsule of
  derived facts, operator decisions and advisory context, started a successor, and `rollover-accept` re-measured every derived fact,
  acknowledged, took the lease as the second epoch with the reason SUCCESSION, and the decision log ends COMPLETE with the
  predecessor no longer the master. A write with the old token was refused. The orchestrator checked ten acceptance items and found them
  met. The two rehearsals before it are not evidence; the first found a real defect (the successor's agent name), which the strand
  `rlname` fixed.
* *What that does not show.* One rotation, on one host, of the orchestrating role of one project, to one successor model in one
  permission mode. The expiry, the withdrawal and the error paths of the controller were not run live, no second rotation (the
  cool-down) has run, and the capsule input of the canary was written before the newest run existed and did not bind its run root
  (nothing was lost; tracked: derive the input again before each rotation). A policy that names a transcript path is inherited by the
  successor; the canary used the session id from the environment. The digests of the record chain can be re-checked on the committed
  copy only where no private session id enters them; the manifest says which.
* *The context capsule (P2-20).* The module is used live by the verification of the succession capsule: the capsule's derived facts
  are bound as sources and judged by the freshness rule before an acknowledgement is written, and the acknowledgement of the canary
  is verified with no mismatch. Only that path ran live (the assembly with a zero budget, the source binding, the freshness rule);
  hierarchical loading, the role-aware views and the budget squeeze are exercised by tests only.
* *Controller and session replacement (P2-35, status unchanged).* The canary is one replacement of one role observed live; it is
  not the durability record, which is taken again at the freeze, and it binds no gate.
* *Not changed.* P2-42 still reads PARTIAL; P2-11 and P2-33 to P2-37 are unchanged; the gate of P2 is shut.
* *The figures of the development line, taken again.* At the commit of the second revision of the records entry O265 (`29852843`, committed on 2026-10-11 at 04:21 +0300; the
  figures quoted above for the first revision stay as written) the history is 1,204 commits `[V48]` and 237 numbered ledger entries, O23 to
  O265 `[V49]`, with 126 development nodes developed by a product run and merged by the orchestrating session after review `[V06b]` and 158
  written by the orchestrating session directly `[V06c]`. The first and the latest commit of the mainline are 34 days 4 hours apart `[V46]`, and
  25 of the 35 calendar days carry a commit `[V47]`. The register holds 235 requirements, 63 PROVEN + 10 PARTIAL + 6 IMPLEMENTED_NOT_PROVEN + 156 MISSING `[V10]`.

## Update, 2026-10-11, third revision of the records entry O265 (review findings folded by addition)

Nothing above is edited.

* *A limit of the PROVEN reading of P2-14 (added).* The canary exercised the rotation of the session of the orchestrator (the policy, the
  controller and the rollover). The item-level context rotation that the probe of the row names, `rotate` in `src/hoh/rotation.py`
  and `reachable`, has no caller outside the tests; the register note's sentence that nothing rotates items yet stays true. The status
  is PROVEN as set; it is a statement about the rotation of the session.
* *"No second rotation has run" (dated).* The bullet above that says that a second rotation was not run live was true when it was
  written. A second rotation by the product completed later on 2026-10-11 (successor epoch three, outcome COMPLETE); it is recorded in
  the closure records and is not counted here.
* *The figures of the development line, taken again.* At the commit of the third revision of the records entry O265 (`84384e1d`, committed on 2026-10-11 at 05:06 +0300) the history is
  1,207 commits `[V48]`, with 126 development nodes developed by a product run and merged by the orchestrating session after review `[V06b]` and 160 written
  by the orchestrating session directly `[V06c]`.
* *Correction of the bullet "No second rotation has run" above (2026-10-11, third revision of the records entry O265, second part).* The bullet says that the second
  rotation "is recorded in the closure records". That was wrong when it was set: no closure record exists yet. It is replaced by: the second rotation is to
  be recorded in the closure records, none of which exists yet; it is observed in the canary root and in the orchestrator's ledger and is not counted here.
* *The figures of the development line, taken again.* At the commit of the second part of the third revision of the records entry O265 (`d70a1add`, committed on 2026-10-11 at 05:26 +0300) the
  history is 1,210 commits `[V48]`, with 126 development nodes developed by a product run and merged by the orchestrating session after review `[V06b]` and 162
  written by the orchestrating session directly `[V06c]`.

## Update, 2026-10-11, for v0.3.0a3 (release documentation)

Nothing above is edited. The limits of the third alpha are those of the sections above and of the section of `v0.3.0a3` in
`RELEASE_NOTES.md`; the preparation of the release changes no source file and lifts no requirement.

* *What the release notes add.* They collect under one heading the limits that the sections above state for P2-41, P2-43, P2-42,
  P2-14 and P2-20 and for the commands merged since the second alpha, and they say that none of `hoh wait`, `hoh surface` and
  `hoh run --detach` has run against a live Herdr session. The durability evidence is not taken at this commit, and the reboot
  proof (P2-11) stays NOT_RUN until the operator's restart.
* *The product-run merges are counted by title (tracked, LOW).* The count, 97 product-run merges since 2026-09-25 `[V01]`, 78 of
  which change `src/hoh/` `[V02]`, lists the titles of the merges since the second alpha one by one in `tools/docs_numbers.py`; a
  figure that depends on a list of titles is brittle, and a convention for the title of a product-run merge would end the list.
* *The figures of the development line, taken again.* At the commit `3517ce6f` (committed on 2026-10-11 at 07:24 +0300; the figures quoted above
  for the earlier records commits stay as written) the history is 1,217 commits `[V48]`. Of the development nodes after the
  attribution anchor, 126 were developed by a product run and merged by the orchestrating session after review `[V06b]` and 167
  were written by the orchestrating session directly `[V06c]`.

## Update, 2026-10-11, after the operator's restart (records entry O266)

Nothing above is edited. This section supersedes, for P2-11, P2-33 to P2-37 and P2-42, the statements of the sections above that these rows read
IMPLEMENTED_NOT_PROVEN or PARTIAL, that the reboot has never been run, that the durability evidence is stale, and that no run of a registered project has been
observed. The evidence is under `dogfood/p2-durability/20261011T0108Z/` (with the run's logs and a manifest of digests in `dogfood/p2-durability/FREEZE-20261011T0108Z/`)
and `dogfood/leakids2/20261011T0319Z/`; the rotations are under `dogfood/canary-a7/20261011T0124Z-rotations/`.

* *Crash and reboot recovery (P2-11 now reads PROVEN).* The durability record was re-taken on a `git archive` of `e0d9bdff` (clean tree, no harness failure), and the
  reboot scenario ran across a real restart of the machine: the master was armed and paused on one boot, the operator ran `wsl --shutdown` and restarted, and the
  verification after the restart found the lease holder on another boot, spent an epoch as RECOVERY and closed the project with the canonical tick. The five exit
  conditions P2-33 to P2-37 read the same record and read PROVEN again, because the new evidence is fresh: the relevant paths (`src/hoh/`, the continuation unit,
  the harness and the fixture) have no difference since `e0d9bdff`.
* *What that does not show.* A restart of the WSL virtual machine is not a power cut; the behaviour of a write under an unclean power loss was not exercised. One reboot,
  on one host, of one fixture project, with deterministic fixture role runs and the harness's own loop in place of systemd and `plugin/bin/hoh`. The record itself lists
  the seven checks of the reboot scenario, and the problem classes of the scenarios the row needs, as undiscriminated: no negative control targets them, so the
  instrument cannot show that a build without reboot recovery would fail them. The evidence is about one commit: a change under the relevant paths makes the six
  rows stale until the run is repeated (amendment A7: a regression obligation at records and release checkpoints, and the reboot evidence again at a release
  checkpoint).
* *Registered ephemeral temporary roots (P2-42 now reads PROVEN).* The hold said that the row waited for the first run of a registered project. The strand `leakids2`
  was run as one: `hoh storage new-target` and `hoh storage register-project` made and registered the target clone, `hoh run` ran the planner, the developer and the QA
  pane under Herdr, the iteration was accepted, and the readback found a registered temporary root with its owner record, the role scratch inside the iteration's
  workspace, every workspace record released, nothing created beside the arenas, and the run's output naming the project and the root with `matched_by name`.
* *What that does not show.* The approval-resume of a registered run (limit L2) did not occur, so the kept pane that it cannot adopt is not exercised; one run, one project,
  one host, with the Claude role harness only (the temporary files of the pi, codex and kimi harnesses are NOT_RUN, limit L10); the refusal paths (a pane whose
  `TMPDIR` is not the bound scratch, an adopted pane, a held session) were not exercised live because the first iteration was accepted; arenas, the planner read copy and
  the output sinks are outside the row by amendment A3; the reclaim of a dead creator's workspace is P5-35.
* *Rotation of the orchestrating session (P2-14, P2-20, P2-35: statuses unchanged by this entry).* After the canary the product rotated the session six more times by
  the same policy, each ending COMPLETE with the predecessor no longer the master and no error in the decision log, and after the restart the lease was taken again
  as epoch eight with the reason RECOVERY and the next rotation ran from it (epoch nine). This answers the two earlier sentences of this file that a second rotation "is to be recorded in the closure records":
  it is recorded in the evidence copy named above, and no closure record exists. The limits stand: one project on one host; the expiry, withdrawal and error paths
  not exercised live; the item-level rotation (`rotate`, `reachable`) without a caller outside the tests; the capsule input derived by the orchestrator's script
  before each rotation and not by the product (tracked).
* *The gate of P2 is not met.* No blocking requirement of P2 is open, and two checklist-gate conditions measured by the programme's own instruments are unmet: a
  current independent supervisor review (the preserved record is stale against the checklist snapshot and the matrix and holds blocking findings) and the consistency
  check, which does not close a phase over the release-blocking findings of the preserved reviews (nineteen are unresolved). The condition that every blocking
  checklist item is verified is met. No closure record exists (amendment A7 says what one names). The supervisor review is a separate, independent act.
* *Smaller corrections of the third alpha's texts (by addition).* `RELEASE_NOTES.md` and `CHANGELOG.md` of `v0.3.0a3` say that no run of a registered project has been
  observed; the leakids2 run was made before the tag was cut and was not yet counted: understatement, corrected by a bullet at the end of the limitations of that
  section. The sentence that the durability freeze is armed at `e0d9bdff` is backed in the tree by the evidence record, which names that commit as its head and which
  the matrix refuses when the relevant paths differ from it. The phrase check of the changelog generator reads only the first generated section: a tracked
  tools item, not changed here.
* *The figures of the development line, taken again.* At the commit of the records entry O266 (`cc79b2ae`, committed on 2026-10-11 at 16:01 +0300; the figures quoted above stay as
  written) the register holds 235 requirements, 70 PROVEN and 9 PARTIAL `[V10]`; of the requirements of P2 43 PROVEN `[V10c]`,
  and the P2 gate reads 0 blocking open; 0 exit conditions bound to requirements not PROVEN; 0 exit conditions nothing measures yet; 165 open requirements, 157 behind a phase gate `[V12]` (a count of requirements and of the exit conditions bound to them; the two checklist-gate conditions are unmet and the gate is shut).
  The history is 1,226 commits `[V48]` and 238 numbered ledger entries, O23 to O266 `[V49]`, with 126 development nodes developed by a product run and
  merged by the orchestrating session after review `[V06b]` and 173 written by the orchestrating session directly `[V06c]`. The first and the latest commit of the mainline
  are 34 days 15 hours apart `[V46]`, and 25 of the 35 calendar days carry a commit `[V47]`.

## Update, 2026-10-13, at the closure record of P2 (records entry O268)

Nothing above is edited. Phase P2 is closed at the commit of the records entry O268, under amendment A7: a phase is closed at the commit of its closure record,
which names the evidence digests, the digest of the committed capability matrix, the identity of the independent supervisor review and the digest of the
checklist snapshot, all measured at that commit; after it the live matrix may read rows of the phase as stale while the closure stands. What that commit holds:
the sealing commit `c4dc643d` with the independent supervisor review `20261013T093422Z-S` (a separate session on a read-only worktree of the regenerated records
commit `204905be`, given the canonical capsule and the repository; every inherited finding answered NO_ACTION in the resolution rule's shape, none blocking, the
review complete against the snapshot seal and the matrix digest it names); the succession capsule written on a clean tree and committed alone at `642a4fa9`,
which `tools/succession.py verify` reads VERIFIED through the records-only commits behind it; the committed matrix with every requirement of P2 PROVEN; and the
durability record taken on `e0d9bdff` with an empty diff of the relevant paths since, re-checked at the records commit by the eight-minute run without a reboot.

* *What the closure does not say.* Every limit stated in the sections above stands. The reboot evidence is one restart of a WSL virtual machine on one host with
  fixture role runs and the harness's own tick loop, not a power cut; the checks of the reboot scenario and the problem classes of the scenarios P2-11 needs are
  listed by the record as undiscriminated; the no-human reading covers the orchestration layer and not a real role agent's dialogs; OP-16, a resume service on
  by default, is a P5 item and is not read into P2-36; the lift of P2-42 rests on the first run of a registered project, with the approval-resume of such a run
  not exercised; the drift check of the supervisor review reads NOT_RUN because its baseline carries no source digests. The readiness board stays red
  (`TECHNICALLY_STABLE_READY = no`): a closed phase is not a stable release, and the rows that do not read PASS at the sealing commit (the test suite, the union
  invariants, the benchmark freeze and both benchmark rows, the export manifest, the export sync, the succession capsule as it stood before the capsule
  commit, the consistency check, the external CI) are named in the record with the reasons the board gives.
* *What follows.* A change to the source after the closure creates regression obligations and does not re-open the phase: the durability run is re-run at every
  records and release checkpoint and the reboot evidence is taken again at every release checkpoint, each recorded with its commit; an obligation that fails
  re-opens P2 by a records entry that names the failing run. The tag `v0.3.0` carries no source change against `e0d9bdff`, and the operator ruled on 2026-10-12
  that it needs no new reboot. The figures of this file and the release documents are re-taken in the commits that follow the closure record; until then the
  counts stated in the sections above are those of their dates, and the sentences above that read P2 as open or not closed are superseded by this section.
## Update, 2026-10-13, for v0.3.0 (release documents)

Nothing above is edited. The release `v0.3.0` is cut behind the closure record of P2 (the section above) and changes no source file: the
limits of the release are the limits the sections above state, and the section of `v0.3.0` in `RELEASE_NOTES.md` collects them under one
heading with the rows of the board that do not read PASS at the sealing commit. The release documents lift no requirement and change no tool.

* *What the release notes add.* The closure sequence by its commits, the independent supervisor review and the capsule, the eight-minute run
  on R recorded as a data file, the regression obligations of amendment A7 with the operator's ruling of 2026-10-12 that the tag needs no new
  reboot, and the statement that readiness is not claimed (`TECHNICALLY_STABLE_READY = no`).
* *The count of product-run merges stands at the third alpha's values.* 97 product-run merges since 2026-09-25 `[V01]`, 78 of which change
  `src/hoh/` `[V02]`: the four product runs merged since the third alpha (the two instrument slices, the succession binding with its repair,
  the re-stated state tests; none changes `src/hoh/`) carry titles that `tools/docs_numbers.py` does not list, and after the sealing commit no
  tool changes until the tag. The figure is right for the titles it lists and is corrected with the tool after the tag.
* *The figures of the development line, taken again.* At the commit `f78d1cb9` (committed on 2026-10-13 at 19:29 +0300; the figures quoted above for the
  earlier commits stay as written) the history is 1,260 commits, 182 of them merges `[V48]`; the first and the latest commit of the
  mainline are 36 days 19 hours apart `[V46]`, 27 of the 37 calendar days carry a commit `[V47]`, and the ledger runs to O268 with 240 numbered entries
  `[V49]`. Of the development nodes after the attribution anchor, 131 were developed by a product run and merged by the orchestrating
  session after review `[V06b]` and 188 were written by the orchestrating session directly `[V06c]`.

## Update, 2026-10-14, public documentation, CI and packaging ahead of v0.4.0a1

Nothing above is edited. This update changes documents, the continuous-integration workflow, the distribution workflow and its tools,
and no source file; the limits of `v0.3.0` are the limits the sections above state, and `v0.4.0a1` is not cut yet.

* *What the new front of the README adds, and what it does not.* It states the measured autonomy facts of the build with the method and
  the limit of each (the records before 2026-10-11 are not itemised there, the operator's share is a lower bound, a QA result with every
  check PASS is not an accepted merge). It lifts no requirement and measures no performance claim: the benchmark section says that the
  matched-budget campaign has not been repeated on the `v0.3` code and that the agentic benchmark in preparation has no figures yet.
* *The statement of the shipped agent documents that no live succession has run was out of date, and is corrected on the development line.*
  `src/hoh/agent_docs/references/rotation.md` (shipped in the package) read "No live succession has been run" at `v0.3.0`; it had been
  written before the first product rotation (the canary of amendment A7, 2026-10-11). The file now says that the protocol has been run live
  and where the log is, and it describes how a rotation request is typed into the master's pane. The `v0.3.0` text and the row of the
  table above stand as written on their date and are corrected by addition (the row carries a dated correction).
* *The public CI.* It had been red from the push of `v0.3.0a1` on 2026-10-08 on every push, because two test files import `zstandard`
  (the `archive` extra) and the workflow installed the package without its extras. The workflow installs `.[test,archive]` now and gives
  the tests a git identity; behind the collection error the suite had further failures in a published clone (tests that read records
  the export withholds, or need a live Herdr session), which skip there with the reason named, only when the manifest declares the record
  withheld.
* *The figures of the development line, taken again.* At the commit `aefef58a` (committed on 2026-10-20 at 20:24 +0300; the figures quoted above for the
  earlier commits stay as written) the history is 1,571 commits, 291 of them merges `[V48]`; the first and the latest commit of the
  mainline are 43 days 20 hours apart `[V46]`, 32 of the 44 calendar days carry a commit `[V47]`, and the ledger runs to O271 with 243 numbered entries
  `[V49]`. The register holds 236 requirements, 88 PROVEN and 11 PARTIAL `[V10]`.
