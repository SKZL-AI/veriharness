# Changelog

All notable changes to this project are documented here. Formatting loosely follows Keep a Changelog conventions; versioning follows SemVer once a public release process exists.

<!-- BEGIN GENERATED: tools/changelog_from_release_notes.py 0.3.0a2 -->
## [0.3.0a2] - 2026-10-09 (pre-release)

**This is a pre-release.** `v0.3.0a2` is the second alpha of the v0.3 line, a
research preview. It follows `v0.3.0a1` (tagged 2026-10-08) by the ledger
entries O256 to O260 and is prepared at a documentation checkpoint, **not**
after phase P2 closed. P2, *v0.3 Durable Program Runtime + Autonomy Contract
Base*, is in progress, its gate is not met, and every open gate below is listed
as NOT_YET_PROVEN. The latest stable release is still `v0.1.0`. The first
alpha's section further down in this file is the text of that tag with its dated
corrections, and its figures are the figures of its own cut; the `[Vnn]` figures
of this section are the ones that `paper/NUMBERS_V2.md` now carries.

`pyproject.toml` carries `0.3.0a2`. Like the first alpha it is installed from
the tagged source and is not published to PyPI: the distribution workflow of
this repository accepts only the audited `v0.1.0`. `docs/READINESS.md` is
**unchanged** by this release, and readiness is not claimed: the board still
reads `TECHNICALLY_STABLE_READY = no` `[V13]`, it is still the measurement
taken at commit `75214e31`, and it is not regenerated for this alpha because
regeneration needs the network (the `clean_install` row and the union gate's
wheel build call the package index). Its `tests` row is stale and conservative:
it predates every merge below.

**What this alpha adds, in one paragraph.** Everything in it was built by
product runs under the same discipline as the first alpha: each merge is of a
candidate the product had accepted, released by two independent integration
reviewers and bound by the merge gate (one receipt has a digest that its report
no longer has, see the limitations). The development line now holds 69
product-run merges, 55 of which change `src/hoh/` `[V01]` `[V02]`. The
user-visible changes are a NixOS-first portability patch, the command `hoh agent-setup`,
owned control of the agents HoH starts, disk-full-safe durable writes for more
of the writers, a classification of failed provider dispatches that now records
its provenance and binds every park to what it stands on, a read-only
decision check, `hoh migrate`, a compact task state for a fresh session, the
five P0 measurements, and a rollover launcher that can actually start its
successor. 32 of P2's 43 registered requirements read PROVEN `[V10c]` `[V10b]`
(26 at the first alpha), under limits that each entry below states; P2 is
**not** closed. Nothing in this paragraph was run on a real NixOS or macOS
host, in a live coding-agent session, or in a live context rollover; those are
NOT_RUN and stay listed as such under *Known limitations*.

**Where the first integration pair said no.** Four candidates the product had
accepted were refused at integration review and merged only together with a
repair that a second pair released: the portability candidate (a literal
macOS-style home path in a comment, and README lines that no claims entry
covered), the P0 measurement candidate (the CLI section stated a false
subcommand group), the P2-42 candidate (a route from the sandbox into the
runner's own scratch that already existed on the base) and the second slice of
the recovery classes, P2-43 (a role's own words still parked a run through the
abort path). The repair of that slice was itself refused, for a source-hygiene
test that its run's criteria did not include and that the orchestrator's full
suite had found, and a test-only third run repaired it; all three are in one
merge. The repairs of the other three are in their merges.

The central invariant of this development line is unchanged:

    candidate creation != product acceptance != integration acceptance != merge authority != phase closure

### Added

* **Portability: NixOS first, macOS gaps narrowed (merge `94d26258`).** One
  absolute-path shell resolver replaces the hard-coded `/bin/bash`: it takes
  absolute `PATH` entries only, so a file inside a repository is never run
  through an empty or relative entry. The launcher runs the interpreter that is
  running it. `--isolation strict` binds the Nix store roots read-only, but only
  where they exist, knows the wrapped Herdr server name, and refuses by platform
  with an AppArmor hint. The arena guard and the approval scope cover macOS home
  directories case-insensitively (narrowed, not closed). Receipts state only the
  resource limits that took. `flake.nix` provides a package, an app and a dev
  shell, and the README carries platform notes. **Not run: a real NixOS host,
  `nix build` or an evaluation of the flake, a real macOS host, an
  AppArmor-restricted Ubuntu host.** The flake has never been evaluated, and the
  macOS guards are text patterns tested only by fakes.
* **`hoh agent-setup` -- VeriHarness usable from a coding-agent session (merge
  `04e94a2e`).** `hoh agent-setup (--print | --install DIR)` prints or installs,
  as new files in a directory you name, the text that tells an agent session how
  to use `hoh`: a Claude Code skill, an AGENTS/CLAUDE snippet, reference texts on
  the method and on rotating the orchestrating session, and templates for a
  handoff, an acknowledgement, a drafting brief and reviewer rules. It writes
  only new files (all or nothing; a file or symlink in the way is refused),
  refuses a user-level configuration directory without an explicit flag, and
  starts nothing. The texts require your explicit yes before any user-level
  configuration write, including the trust registration that `hoh start` makes,
  also under a standing delegation, and they state that rotation is not PROVEN
  until its live canary. **Not run: a live Claude Code or Codex session choosing
  `hoh`** (the operator smoke). This release has no README section for it yet,
  because that section would state what a session does, and no smoke has shown
  it.
* **Owned control of the agents HoH starts (P2-17; merges `33491c6e` and
  `a7d73b8e`).** An `AgentControlAdapter` sits between the dispatcher and Herdr,
  and the dispatcher spells no Herdr command itself any more. Every agent HoH
  creates is registered in an append-only `owned-agents.jsonl` bound to the run
  that wrote it; every acting verb on an id the registry does not hold is
  refused before Herdr is called; `close_tab` is refused unless the runtime's
  snapshot shows the tab and every pane in it as owned, and fails closed on a
  malformed snapshot. P2-17 reads **PROVEN, with limits stated in the register
  and in `docs/LIMITATIONS.md`**: the `program` scope of a role is built by two
  tests only (the type's own and the registry test that adopts one) and no
  product path starts a program-scope agent through the registry; the registry
  is a record kept by HoH's own process, not a defence against a process of the
  same user that can write the run directory; `close_tab` ignores the tab's pane
  count, so it relies on the runtime's snapshot listing every pane of the tab; a
  pane that was never released stays owned on disk, and after a Herdr restart a
  recalled id may name a pane that is not ours; restart recall needs the
  registry directory's name to equal the run id, which the benchmark's arm A
  layout does not give (it recalls nothing and fails safe). **Not run: a live
  `hoh run` and a restored-pane adoption against a real Herdr server.**
* **Disk-full-safe durable writes, two more slices (P2-41; merges `ae235948`
  and `e3390276`).** Amendments, role approval files, governance records, the
  restore command, the run records, the integrity close and the launcher's
  prepare step now write through the durable-write primitives and stay
  fail-closed under injected ENOSPC and partial writes. A failed integrity write
  parks the old record under a versioned name, so a stale `held: true` can no
  longer be read as current. `hoh restore` never replaces the process lock of a
  live run. **P2-41 stays PARTIAL**: the writers of `dispatchers.py` (the third slice) are
  not migrated, and the scan keeps them on its deferred list.
* **A classification of failed provider dispatches, its provenance and its
  park binding (P2-43, slices a and b; merges `6312e46d` and `a4e8fbad`).** A
  failed dispatch is classified from its evidence into the A5 failure classes,
  plus a stalled prompt and a role timeout; text nobody can classify stays
  unclassified and arms no timer. QA now re-raises a provider failure instead of
  booking it as a rejection, the misclassification that cost eight iterations
  `[V26]`; the normalised class is recorded in telemetry, and `hoh resume-quota`,
  the launcher and the resumption table know the new wait kinds. The second
  slice records, with every dispatch, its provenance (provider, model identity,
  the operator-registered account handle, role, route policy, capability
  requirements, data class, failure class); binds every park to the programme,
  run, iteration, attempt, stage, candidate, master and session it stands on;
  writes the two routing receipts of the one P2 policy (wait on the same route);
  takes the retry time from the provider's own stated reset where one is printed
  instead of waiting an hour; and adds three options to `hoh run`
  (`--role-account`, `--master-session`, `--master-epoch`; a value that looks
  like a credential is refused and never echoed). A role's own words no longer
  park a run through the abort path or the seal and binding blocks, which show an
  exception's text and never read it as a quota statement; a phrase of the
  classifier's list on a pane line of a failed turn is still read as provider
  text whoever wrote it. **P2-43 reads
  MISSING, still**: the binding is written and **nothing checks it on resume**;
  the check before a parked run is unblocked, the claim that keeps two resumers
  from the same park and the four A5 scenarios as one test are the third slice,
  not merged.
* **The five P0 measurements (merge `c73eea06`).** `tools/baseline_docs.py`
  measures the plan's five P0 items offline (the fresh install path, the CLI
  commands, the service and runtime model, the human intervention points, the
  long-horizon dogfood evidence), every figure naming its command or `path:line`
  and an unreadable source rendered `NOT_MEASURED`; `tools/build_plan.py` renders
  each as `measured` or `NOT MEASURED`. The P0 gate itself still opens on the
  presence of the baseline files; the measurements are present, sourced and
  resolvable, which does not make the figures current.
* **Durable program state read by a fresh session (P2-12, P2-13, P2-15 and
  P2-21).** `hoh migrate` brings run and project state to the current schema
  (a dry run unless `--apply`; the old form is parked, never deleted) and the
  stores read only states they can migrate (merges `31228b64` and `5854590c`).
  `hoh project resume` and `hoh project status` carry a compact task state, with
  a scope line and NOT ALL CLEAR when a gate is not green (`6c85d348`). The
  project controller records each dispatch as activity events in the same atomic
  write as its lifecycle transition and folds them with `reduce`, and the
  project state is schema 2 with one registered migration step (`2e97d814`).
  `hoh decision check` runs the decision checker on decision maps given as files
  (`0f33b9a2`). The register reads these four rows PROVEN, **with limits that
  matter**: the real run chain of `hoh migrate` is empty (every run state on
  the real stores is already at the current version, and no project state has
  been written there); no run loop or controller path
  consults the compact task state; `reduce` is a cross-check, and the lifecycles
  stay authoritative; the product keeps no decision graph, so `hoh decision
  check` examines files an operator supplies, for structure and not truth. Not
  run: a live `hoh project drive` with real roles.
* **Context rollover can start its successor, and the lease is conditional
  (merges `e4b4cda9`, `6091cea9` and `b0fd7614`).** The launcher starts the
  successor with an explicit permission mode (only `auto` is offered), puts a
  PATH-free, shell-quoted invocation in the pointer and binds the successor's
  accept to its own conversation process. The follow-up merge validates the
  capsule id as a plain name before anything is written, takes a last look right
  before `acquire`, resolves `--root`, refuses a non-object `--input` and a
  non-finite or non-positive interval. `MasterAuthority.acquire` can now be made
  conditional on an expected handover and judges it under the lease's own lock,
  which the successor's poll uses (P2-09, PROVEN with limits: the lease test
  injects the clock and calls `renew` directly, nothing protects against a
  hostile process of the same user, and the reclaim of a lapsed lease stays
  unconditional). **The rollover has still not run live**, so none of this is
  evidence for P2-14 or P2-20.
* **The first prompt of an agent is waited for, and the wrap of the input box is
  modelled (first-prompt readiness; merge `846d8e02`).** The stalled-submit
  recovery now compares the box with the pointer text under the word wrap that
  three recorded boxes show, and before the first prompt the dispatcher waits for
  an idle input box that holds still; a timeout raises and sends nothing.
  **Whether this cures the Claude Code first-prompt stall is SUSPECTED until a
  live check.**
* **Registered ephemeral roots, more of them (P2-42; merges `35215aa8` and
  `c97a1ba2`).** A check's scratch must be empty at every use and is locked
  across processes; a check can no longer be given a directory inside the
  runner's own scratch, by the spellings the reviews found; the runner records
  its own scratch by identity, so a link with another name, or another process's
  check, cannot reach it. **P2-42 reads PARTIAL still** `[V10d]`.
* **Evidence tooling for the crash and reboot rows (P2-11, P2-33 to P2-37; merge
  `a155d052`).** The reboot path of `tools/durability_e2e.py` signals nothing
  across a boot (a mark is skipped unless the armed boot id is positively the
  same boot, and a pid is signalled only when its start time matches), and the
  archive is bound to the evidence commit. **This raises no row**: the evidence
  run and the operator-run restart have not been taken.
* **Every role's pytest stays inside the run's own scratch (role-basetemp and
  its follow-up; merges `fcb0b5db` and `b7cf790b`).** Planner, developer and QA
  panes, and the `--no-herdr` path, get a `TMPDIR` inside the arena that the
  retention step never moves, and the role prompts state the rule. The
  follow-up refuses a role directory that the user cannot read, write and
  search, at registration and before every dispatch, instead of repairing it
  silently or falling back to `/tmp`. Its length (over a hundred characters on
  the real layout) is a stated residual for `multiprocessing.Manager` and the
  `forkserver` start method. This answers an incident of this period, below.
* **New CLI surface** (development, not a stable interface): `hoh agent-setup`,
  `hoh migrate` and `hoh decision check`; `hoh project resume` and `status` show
  the compact task state.

### Changed

* **The register.** Of all 227 requirements, 59 read PROVEN, 11 PARTIAL, 6
  IMPLEMENTED_NOT_PROVEN and 151 MISSING `[V10]`; P2 is 32 PROVEN, 4 PARTIAL
  (P2-14, P2-20, P2-41, P2-42), 6 IMPLEMENTED_NOT_PROVEN (P2-11 and P2-33 to
  P2-37) and 1 MISSING (P2-43) `[V10c]`. The matrix measures an import
  statement, never execution: the weight of each PROVEN above rests on the merge
  run's criteria, its integration pair and the limits written into its note.
* **The project state is schema 2.** An older build refuses a version-2 state;
  a version-1 state is refused until `hoh migrate --apply`; mixed-version
  operation on one project directory is not supported.
* **The P2 gate.** It has 11 requirements blocking and open `[V12]`, where the
  first alpha had 17.
* **A provider limit the classifier does not read blocks for a person.** The
  abort path no longer reads an exception's text for a quota statement, so a
  limit worded in a way `hoh.failures` does not recognise, which used to be
  parked by accident of text, now blocks the run with no timer; a wording that
  recurs in real runs is added to the classifier with its banner shape. A failure
  class that needs an operator no longer goes through the abort handler: the run
  is blocked for a person and `hoh run` ends through its normal end of run, with
  exit 0 unless a capability violation holds the run, where the abort handler
  ended it with exit 1 (read in `controller._failure_wait` and `cli._cmd_run`;
  not replayed on a live run).

### Fixed

* `hoh migrate --apply` followed a symlinked run or project directory and
  rewrote a symlinked lock file outside its root, replaced a crashed run's
  `process.json` unparked, and met pathological records with a traceback. It now
  refuses links and escapes at plan time and again before each write, never
  rewrites `process.json`, and refuses a record no parser takes. The migration
  command had been merged before this was found, and was reproduced on that
  merge.
* A role's own words could park a run as a usage-limit wait: a pane line such as
  `Rate limit: cap at 100/min` in a failed developer or planner turn, or a
  role-chosen file name that contains `rate_limit` in a failed seal, reached the
  quota detector through the generic abort handler, and the run waited
  an hour with no receipt and no timer. Found and replayed by the integration
  review of the second recovery slice; the exception's text is now shown and
  never read.
* A check run through the STRICT sandbox could be given a directory inside the
  runner's own scratch and plant a git alias that a later unsandboxed check ran.
  A route that existed on the base before the strand began; closed by the
  repair, and the residual families by the next merge.
* The reboot path of the durability tooling killed marks whose boot id was empty
  or missing; it now skips them.

### Known limitations at this pre-release, and what is not claimed

Each open gate below is NOT_YET_PROVEN, and a gate that reads red stays red.

* **P2 is not closed.** Its gate has 11 requirements blocking and open, and 3
  exit conditions that no instrument measures yet `[V12]`. Of the eleven, P2-33
  to P2-37 read IMPLEMENTED_NOT_PROVEN because their durability evidence is stale and the
  evidence tooling has not been run at the freeze commit; P2-11 needs a real
  reboot (DS2) that has never been run; P2-14 and P2-20 wait for a live
  rollover; P2-41 is PARTIAL; P2-42 reads PARTIAL `[V10d]`: the controller does
  not yet open a registry session or pass a scratch, the workspace's own
  temporary files are not covered, and the P2-41 dependency is open; P2-43 reads
  MISSING.
* **A provider usage limit is classified and its park is bound, and the repair
  is not complete.** The first two slices are merged (above); the third, which
  checks the binding on resume, is not. A park that is stale, foreign or
  forged is still resumed exactly as before. Tracked: the classifier reads a
  phrase of its list on a pane line whoever wrote it, a banner-shaped line of a
  role before the real banner supplies the retry time, a limit worded in a way
  the classifier does not read now blocks for a person and arms no timer (changed
  on purpose), an expired authentication is parked by the contract, a token-like
  string without a known prefix is accepted as `--master-session` and printed,
  and developer-side limit detection is not built.
* **The first prompt after an agent starts can still stall.** First-prompt
  readiness is merged; whether it cures the stall is SUSPECTED until a live
  check, and the recovery still fails closed when its model of the wrap does not
  apply. One later observation under Claude Code 2.1.294 saw a first prompt
  submitted without a stall; it is one observation.
* **Context rollover has not run live.** The launch blockers found for the
  canary are closed in code; the canary itself has not run, so no row is proven
  by a succession. An automatic rotation policy (Layer B) is not built, and a
  refusal in the lapse window of the lease can leave no master until
  `hoh project hold`.
* **`hoh agent-setup` has not been tried by a live agent session.** Its tests
  and its integration review use fakes and temporary directories. An unusable
  path can still produce a traceback.
* **Owned agent control has not run against a real Herdr server** (P2-17, above),
  and the benchmark's arm A layout recalls nothing after a restart. A restored
  Herdr pane carries no role `TMPDIR`; only the role prompt tells it.
* **Portability is NixOS first, and unverified on a host.** See the first entry
  under *Added*.
* **Four QA roles of this period ran pytest in the shared default basetemp**
  (their own `open_gaps` say so): a directory that belongs to no run, where
  pytest's retention rule can remove the numbered directories of other pytest
  processes. No reviewer could assess whether anything of another session was
  removed. The role scratch merge is meant to prevent it for the runs started
  after it; only the QA results were scanned for it, not the planner and
  developer results, and panes restored by Herdr cannot receive an environment
  and rely on the prompt rule.
* **One merge-gate receipt binds a digest its report no longer has.** The
  role-basetemp merge's receipt records a digest of the first integration report
  and the report was modified after the gate ran; its footer lines are identical
  to the receipt's, and what changed in the body is NOT_MEASURED.
* **The earlier limitations stand where they are not named above.** The
  integrity gate is met for the replayed paths and not for every path; the role
  pane lifecycle ends a role's own processes with the limits the first alpha
  states; the planner re-types the specification's criteria; the QA-gap gate is
  not merged; the storage steward has not reclaimed anything of VeriHarness's
  own data.
* **Smaller tracked items.** Lint and the identifier check are red at this
  commit, as they were before the merges of this period: `ruff --select F,E9`
  reads 34 findings, 17 of them in the merged `tests/test_dispatch_provenance.py`
  (a fixture imported and redefined as a test argument, and one unused import)
  and 17 that predate it; the identifier check lists 7 files, one of them that
  test file (the word "echoed" in two test names) and 6 that predate it. Two
  merge messages of this period under-report their reviews; the board's `tests`
  row is stale.
* **Not claimed:** that any figure measured for `v0.1.0` describes this line;
  that the exact-head external CI has run on these bytes (it has not); that the
  portability patch works on a real NixOS or macOS host (it was not run on one);
  that a coding agent given the output of `hoh agent-setup` uses `hoh` (not
  run); that long-duration unattended operation is demonstrated (it is not).

<!-- END GENERATED: tools/changelog_from_release_notes.py -->

<!-- BEGIN GENERATED: tools/changelog_from_release_notes.py 0.3.0a1 -->
## [0.3.0a1] - 2026-10-07 (pre-release)

**This is a pre-release.** `v0.3.0a1` is the first alpha of the v0.3 line, a
research preview. It is prepared at the documentation checkpoint that follows
Section P and the ledger entries O252 to O255 -- **not** after phase P2
closed. P2, *v0.3 Durable Program Runtime + Autonomy Contract
Base*, is in progress, its gate is not met, and every open gate below is listed
as NOT_YET_PROVEN. The latest stable release is still `v0.1.0`. P1 was completed
as an internal development milestone. No public v0.2 release was cut; the latest
stable public release remains v0.1.0 while the development line continues
through P2.

`pyproject.toml` carries `0.3.0a1`. The pre-release is installed from the tagged
source and is not published to PyPI: the distribution workflow of this
repository accepts only the audited `v0.1.0`. `docs/READINESS.md` reads
`TECHNICALLY_STABLE_READY = no`, and this pre-release does not claim otherwise.
The board is measured at commit `75214e31`, not at the commit that carries this
release: it is conservative (its `attribution` and `tests` rows predate the
entries and the run that now close them), and it is not regenerated at the cut
because regeneration needs the network (the `clean_install` row and the union
gate's wheel build call the package index).
(Correction, 2026-10-08, of the text published with the pre-release tag: "close them" is inexact for the `tests` row. The last suite run of the checkpoint still had failed tests (two environment failures of the scratch placement and one artefact of that run's own PATH), so the row is stale and conservative, not closed. The earlier wording is kept and corrected here; the newest ledger entry has the figures.)

**Integrity gate: met for the paths its reviewers replayed, with one residual
stated.** The condition for cutting this alpha is that its runtime cannot turn
a run that was blocked for a capability violation into an ACCEPTED one. Two
merges carry it. The qa-arena-write repair (merge `a455edc0`) seals QA's
candidate arena, freezes it in QA's witness, and holds a run that was blocked
for a capability violation: not reported ACCEPTED, not delivered and not
resumed by anything but `hoh unblock`, for every state shape the current build
writes. The qaw-followup merge (`ab41589f`) closes the three findings that the
repair's integration reviewer had proved and that kept this gate open when this
checkpoint was first drafted: a violation state written before the hold
existed is no longer unblocked by the resume loop (it is refused and the
launcher verdict is UNDETERMINED, not ACCEPTED); the acceptance record counts
an open QA seal as a problem; a `seals.jsonl` that is a symlink, a FIFO or has
a second name is refused before a byte is written, so no ledger line reaches a
foreign file; and a forged `planned` line for a sibling arena is honoured only
when it is bound to an arena this run sealed. Both integration reviewers
replayed each of these paths on the merged tree and saw it refused. **What this
does not claim:** that no other false-ACCEPTED or foreign-write path exists --
it covers the replayed paths and what the strand's 13 criteria and 54 mutant
rows pin -- and the residual the strand states and pins itself: a role pane that
has read the run's `arena_secret` can compute the name of a real arena of that
run and have a forged `planned` line for it honoured (a mode change inside the
run's own arena, which the pane could make itself), because the secret is in
`state.json` and the ledger is a record and a restore aid, not an access
control against a process of the same user. Earlier text of this paragraph,
written before the strand merged, said the gate was not met and the release was
held for it; that was correct at the time, and the correction is dated in
`docs/LIMITATIONS.md` (limit 12d). **The repair of the quota misclassification
(failure-classes) is not merged.** The first entries under *Known limitations*
say what that costs.

The central invariant of this development line, which every section below
assumes:

    candidate creation != product acceptance != integration acceptance != merge authority != phase closure

### Added

* **HoH builds HoH's own kernel.** Since 2026-09-23 product runs have written
  modules under `src/hoh/`. From 2026-09-25 on, every such run worked in an
  isolated clone pinned to one commit, its accepted candidate was imported byte
  for byte, and two independent integration reviewers released it before the
  orchestrating session merged it: 46 product-run merges `[V01]`, 34 of which
  change `src/hoh/` `[V02]`. In the ledger entries O236 to O254, 26 candidates
  the product had accepted were refused at integration review `[V07t]`.
* **Durable programme runtime (P2).** Durable timers, a sentinel, a never-stuck
  check, session succession with a master epoch, a partial leader lease, a
  resume loop that acts on due timers, an autonomy contract, a programme
  checklist bound to the phase gate, and an independent supervisor review of
  the gate. 26 of P2's 43 registered requirements read PROVEN `[V10c]`
  `[V10b]`; P2 is not closed.
* **Project contract and state ownership.** Read-only `hoh project contract`
  commands (`schema`, `check`, `impact`, `owner`, `conflicts`) beside
  `hoh project authority`, `drive` and `handoff`.
* **Evidence model.** The `EvidenceClaim` schema and its entry point
  `tools/evidence_claims.py`; proof classes that keep "built", "tested",
  "integrated", "behaviourally proven" and "gate satisfied" apart; and an
  acceptance record that is refused as `STALE` when the code it describes
  changed after it was taken.
* **Programme gate machinery.** Phase gates bound to the register (a gate item
  that is not bound can no longer read closed), supervisor drift measured
  against a preserved baseline, each acceptance probe bound to its own register
  entry, and a union-gate timeout that reports a stable `NOT_RUN` instead of
  copying an interpreter path into the board.
* **Storage lifecycle (operator amendment A1), and its first self-dogfood.**
  Ownership registry, admission by reservation, durable park and resume under
  storage pressure, verified archive bundles and targets, host adapters, and
  `VERIFIED_RELOCATION` with a per-project storage steward -- the only operation
  that may remove a local copy of project data. `hoh storage init`, `steward`,
  `adopt-project`, `survey-legacy` and `adopt-legacy`, and `hoh resume-storage`.
  Section P ran on the real project: it was adopted, the survey of legacy
  reviewer scratch found 0 of 188 roots eligible `[V33]`, and the steward's real
  pass observed and reclaimed nothing.
* **Registered ephemeral temporary roots (P2-42).** A project-owned temporary
  root; a scratch that is not owned by the user, not private to them or not
  empty is refused; cleanup compares mount ids and removes through directory
  descriptors; and an inode pressure signal sits next to the byte signal. The
  strand took three runs, two of which integration review refused `[V38]`.
* **Every long role prompt goes through a pointer file (prompt-threshold).** A
  role prompt above 8,192 bytes, or holding a character an input box treats
  specially (control, format and private-use characters, tab, carriage
  return), is written to a link-safe file and the agent receives a short
  pointer, so the stalled-submit recovery can verify the box text of every
  first prompt. Claude Code 2.1.292 renders a long inline paste collapsed,
  which had made that recovery fail closed and had blocked three runs of the
  qaw-followup strand before any candidate. The strand was built by a product
  run under one declared deviation, a bootstrap harness (the previous source
  plus only the reviewed dispatcher change), retired at its merge.
* **HoH closes its own role panes and ends a role's processes before the
  freeze (role-pane lifecycle).** It never signals or closes a foreign pane or
  process: a process is ended only when it carries the pane's minted mark and
  is anchored to the pane's own shell (its session or its descendants), a
  signal goes to one process and never a group, and a pane whose role left
  marked processes outside it is refused and named rather than closed. A tests-only follow-up
  (rpl-tests) closed two blocking findings of its integration review.
* **The orchestrating session can be rolled over under a protocol (context
  rollover, Layer A).** `hoh project hold`, `rollover`, `rollover-verify` and
  `rollover-accept` freeze a succession capsule whose entries are derived facts
  that the successor must re-derive, operator decisions kept verbatim and never
  answered by the machine, and advisory context with no authority; the
  successor takes the master epoch only if everything re-derives and
  acknowledges, after a re-verification that sits between the acknowledgement
  and the acquire; a stale predecessor is fenced. It is tested with fakes and
  is **not evidence for any P2 row before its live canary**: the one handoff of
  2026-10-07 was written by hand, before the protocol existed, and does not
  count as one.
* **QA's candidate arena is sealed.** Every write bit under it is cleared for
  QA's dispatch, the plan is on a ledger before the first `chmod`, the arena is
  frozen in QA's witness, and the ways a `hoh run` ends compare the seals they
  leave in place. A capability violation sets a hold that only `hoh unblock`
  lifts, for the states the current build writes, and for the legacy-state,
  open-seal, symlinked-ledger and sibling-plan paths of the follow-up strand.
* **Amendment A5, registered and not built.** Provider and model routing with
  durable failover: five requirements, one per phase, all MISSING `[V40]`.
* **Operator units.** Optional systemd user units under `ops/` for a
  supervised quota resume, an unattended continuation, a storage-pressure
  resume and a per-project steward pass. Nothing installs or enables them.
* **New CLI surface** (development, not a stable interface): `hoh storage`
  (`init`, `steward`, `adopt-project`, `survey-legacy`, `adopt-legacy`),
  `hoh resume-storage`, `hoh timers`, `hoh sentinel`, `hoh stuck`,
  `hoh project` (`authority`, `contract`, `drive`, `handoff`, `hold`,
  `rollover`, `rollover-verify`, `rollover-accept`), and
  `--drive`/`--recover` on `hoh resume-quota`.

### Changed

* **A stalled prompt is a transient, and idle is not done.** A stalled prompt is
  retried; an agent that ends its turn without an answer gets one follow-up;
  after a stalled prompt submit, Enter is pressed once, on the dispatch's own
  prompt, bound to the stall record of that send. Prompts no longer start with
  `/`, and a pane that still holds unsent input is refused instead of typed
  into.
* **Long prompts travel as files.** A role prompt longer than one command-line
  argument is written to a link-safe prompt file and Herdr receives a pointer;
  the dispatcher compares the file against a digest it kept in memory. Since
  the prompt-threshold merge the same path serves every role prompt above 8,192
  bytes and every prompt holding a paste-unsafe character.
* **Candidate ids and anchors.** A resumed iteration gets a fresh candidate id
  (`-i<n>-a<m>`), and the anchor of a candidate never moves a ref.
* **The command guard.** An operator-side policy can sharpen the shipped command
  policy and can never relax it; one mandatory deny rule that never matched is
  fixed and permission-rule forms are checked at load.
* **Disk-full-safe durable writes** cover the first fifteen writers of the
  migration list (P2-41 stays PARTIAL).
* **Artefactual discrimination.** pytest's exit-4 "nothing ran" messages count
  as artefactual, case-insensitively and only at exit 4.

### Fixed

* The capability witness crashed on a file name that is not UTF-8; names are
  now folded with `os.fsencode`, non-UTF-8 text is recorded losslessly in the
  run state, and the digests of UTF-8 names are unchanged.
* A role prompt longer than one argument failed with `Argument list too long`.
* A retry timer could come due before the provider's reset.
* A run held for a capability violation could be reported ACCEPTED, delivered
  or resumed through a pause, and QA could write into its candidate arena
  without the write being noticed: fixed for the states the current build
  writes, and by the follow-up for a legacy violation state, an open seal, a
  symlinked seal ledger and a forged sibling-arena plan line.
* The board's regeneration did not converge because a timeout text carrying a
  home path entered the board; the cause was proven and removed.

### Known limitations at this pre-release, and what is not claimed

Each open gate below is NOT_YET_PROVEN, and a gate that reads red stays red.

* **The integrity gate is met for the replayed paths, not for every path.** The
  qaw-followup merge (`ab41589f`) closed the legacy-state resume, the open
  seal, the symlinked seal ledger and the forged sibling-arena plan line, each
  replayed by both integration reviewers (see the gate paragraph at the top).
  Stated and pinned by the strand itself: a role pane that has read the run's
  `arena_secret` and copies iteration and attempt gets a forged plan honoured
  inside its own run's arena. Tracked minor: the acceptance tool does not count
  link names in a seal ledger (not reachable from the drive, because evidence
  is copied without hard links); a held run under storage pressure is booked as
  `storage_unavailable` (unreachable by the strand's own reading). The earlier
  statement that the legacy fallback of the hold only denies was wrong and is
  corrected in `docs/LIMITATIONS.md`.
* **A provider usage limit that stops a QA role is booked as a rejected
  iteration.** It cost eight iterations in the runs recorded so far `[V26]`; the
  same limit in a planner is classified as a resumable block. The repair
  (failure-classes, the core of amendment A5's P2 item P2-43) is specified and
  not merged, so P2-43 reads MISSING `[V40]`. The orchestrator parked and
  resumed each affected run and did not treat the lost iteration as a verdict
  on the candidate. The eighth, the role-pane-lifecycle run's first iteration,
  is read from its run state (ledger O255).
* **The first prompt after an agent starts still stalls, and the recovery has a
  width limit.** Dispatch recovers (an Enter on its own prompt, once) instead of
  blocking, seven times in the runs that had the recovery `[V31]`, and since the
  prompt-threshold merge every role prompt goes through a pointer file so that
  the box can be verified. That is a recovery, not a fix of the startup race.
  The recovery compares the box with the pointer text and models a break only
  mid-word at exactly the box width, while Claude Code breaks at a word boundary
  when a line does not fit: in a 79-column pane (box width 76) the pointer's
  first line, 77 columns with its indentation, wrapped short of the width, the
  recovery refused as designed, and the run qfu4 was blocked at its first
  planner dispatch before any candidate; the same pointer text passed in a
  106-column pane. The strand that waits for the first prompt to be accepted
  and models the wrap is the next P2 strand, not started.
* **A role's own processes are ended before the freeze, with limits.** The
  role-pane lifecycle is merged together with a tests-only follow-up. Closing a
  pane still does not end its background descendants `[V32]`; HoH ends them
  itself, by mark and anchor, or names what it did not signal. Its first live
  use closed the planner and developer panes at turn end; the QA pane's release
  was refused because two marked processes had escaped the pane (named, never
  signalled), and the pane was gone without anyone closing it by hand (Correction, 2026-10-08: the lifecycle log of that run, `pane-lifecycle.jsonl`, also holds a `closing` and a `closed` line for the QA pane, in the same second as the `refused` line; the earlier "closed three panes and refused one" counted that pane twice, and the text above replaced it.) (Further correction, 2026-10-08: the `closing` line is written by HoH before its own close command and the `closed` line only after a second snapshot no longer lists the pane, and a pane found already gone is written with an `already_gone` field, which this line does not carry (`_close_own_pane` in `src/hoh/dispatchers.py`); so HoH itself closed the QA pane's tab, nobody closed it by hand, and the `refused` line that follows names the two escaped processes, not a failed close.). Tracked minor:
  a few extra mutants of the pane-count guard survive the tests (fail closed),
  a detached daemon adopted by a subreaper is documented as escaping, and a run
  state keeps a stale iteration-1 task after acceptance.
* **Context rollover has not run live.** The capsule, the state machine and the
  four commands are tested with fakes; no live succession has been run, so no P2
  row is proven by it. The draining states between the freeze and the validated
  successor are recorded and **not enforced**: nothing refuses `hoh start` or
  `hoh run` in that window, and before the handoff the predecessor is trusted
  to launch nothing. The first-prompt stall can lose the successor's pointer; the
  acknowledgement deadline then fails closed, the predecessor stays master, and
  the pointer is sent again or the command re-run. Derived facts are only as
  good as the code that derives them: a fact nobody derives is not checked.
  Open follow-up items (tracked HIGH, a prerequisite of the live canary): the
  capsule id is not validated, so an unverified capsule can place its
  create-only, owner-only acknowledgement file outside the rollover directory;
  the acquire is not atomic with the last verification, so a master gap is
  possible, never two masters; a relative `--root` never rolls over (it fails
  closed); a non-object input file raises instead of refusing; `hold` accepts a
  zero or negative interval. An automatic rotation policy (Layer B) is not
  built.
* **P2 is not closed.** Its gate has 17 requirements blocking and open, and 3
  exit conditions that no instrument measures yet `[V12]`. P2-33 to P2-37 read
  IMPLEMENTED_NOT_PROVEN because their durability evidence is stale; P2-11 needs
  a real reboot (DS2) that has never been run; P2-41 is PARTIAL; P2-42 reads
  PARTIAL `[V10d]` after the register edit that follows its merge, not PROVEN.
* **The planner re-types the specification's criteria** and nothing compares
  the planned command with the specification's text; a replay of 53 planner
  answers found 195 deviating checks in 42 answers `[V34]`.
* **The QA-gap gate is not merged**: a gap QA records as blocking does not block
  acceptance.
* **The storage steward has not reclaimed anything of VeriHarness's own**; at
  the real pass its state was NORMAL and it observed only `[V33]`.
* **Smaller tracked items.** The developer role has twice left a
  filesystem-wide `find /` running when its turn ended, each time stopped by the
  orchestrator (the specs had named scratch paths the role cannot reach; a rule
  against that is in place and the role prompt does not yet forbid the search);
  lint (15 findings) and the identifier check (4 files) are red at this commit;
  and the `tests` row of `docs/READINESS.md` is measured in a checkout with
  history and the runs directory, so it reads a different failure count than a
  history-less copy of the same tree, which fails 18 tests that need the git
  history, the runs directory or the host's units: two environments, not two
  versions of the code.
* **The board is red** (`TECHNICALLY_STABLE_READY = no`): tests, lint, the
  external CI, the export synchronisation, the benchmark freeze, the programme
  supervisor and others, each for a reason `docs/READINESS.md` states.
* **Not claimed:** that any figure measured for `v0.1.0` -- campaign,
  benchmark, skip counts -- describes this development line (they stay
  historical); that the exact-head external CI has run on these bytes (it has
  not); that long-duration unattended operation is demonstrated (it is not).

`docs/LIMITATIONS.md` opens with the current open limitations, the entries
resolved or changed since `v0.1.0`, and the historical references, in that
order. `paper/POSITION_PAPER_V2.md` is the account of the programme since the
tag, with its own figures in `paper/NUMBERS_V2.md` and its audit in
`paper/AUDIT_V2.md`; `paper/POSITION_PAPER.md` stays as published with
`v0.1.0`, with dated errata added.

<!-- END GENERATED: tools/changelog_from_release_notes.py -->

## [0.1.0] - 2026-09-14

First stable tag. `RELEASE_NOTES.md` carries the full account, including the
measurements that do not flatter the harness; this section lists what changed.

### Added -- a baseline

- `docs/BENCHMARK_PROTOCOL.md`, frozen before the first cell of the first
  campaign ran: five tasks, three arms (plain agent / one `hoh run` / full
  control plane), a hidden test suite per task that decides the verdict, and a
  budget matched on role dispatches.
- `docs/BENCHMARK_RESULTS.md`, `docs/BENCHMARK_RESULTS_v2.md` and
  `docs/BENCHMARK_RESULTS_v3.md` -- three campaigns, none edited in the light
  of another. v2 is kept with `matched_budget_valid = NO`, which cannot be
  repaired after the fact and is not.
- `docs/benchmarks/v3/`: the pre-registration, its parked predecessor, the
  provenance record establishing from git objects that the exact registration
  bytes were committed before the first dispatch, the raw-result digests, and
  the post-campaign drift accounting.
- `tools/benchmark.py`, `tools/repetition_plan.py`, `tools/prereg.py`, and a
  second, independently written aggregation in
  `tests/test_v3_aggregation_agreement.py` that never imports the reporter and
  must agree with it on every figure.

### Added -- enforcement, where there had been description

- `Budgets.max_dispatches`, checked before the counter is charged and persisted
  before the provider call; shared by a node and its repair nodes; survives a
  restart. `RunVerdict.BUDGET_EXHAUSTED`, `HaltClass.BUDGET_EXHAUSTED` and a
  `BudgetExhausted` exception type, so exhaustion is never a generic failure.
- `tools/budget_evidence.py`: eleven controls at three ceilings, at least one of
  which must force the refusal inside an iteration, plus two falsifiers that
  delete the enforcement and must be detected.
- `tools/closure_e2e.py`: the positive path, at a budget measured from campaign
  v2 rather than chosen after a failure.
- `tools/meta_evidence.py --falsify`, `tools/confinement_evidence.py`,
  `tools/telemetry_audit.py`, `tools/exact_head_ci.py` and
  `tools/readiness.py`, which assembles the board.
- `CapabilityWitness.neu_bezeugen`, re-taking the baseline for named paths only,
  so a retry cannot launder a hostile write into it.

### Changed -- what a skip is allowed to mean

- `tests/conftest.py` now authorises an environment-gap skip only when
  `EXPORT_MANIFEST.json` declares that path excluded for the reason the test
  expects. Missing-and-included, missing-and-unclassified, missing-manifest and
  wrong-reason all fail instead of skipping.
  `tests/test_export_gap_guard.py` holds that with five negative controls.
- Dispatch counting reads a `provider_calls` field instead of counting log
  lines, which was wrong in both directions.

### Changed -- documentation

- `README.md` and `paper/POSITION_PAPER.md` gained the three campaigns in full
  and a section on what the harness demonstrably does, each item bound to
  evidence; `paper/NUMBERS.md` and `paper/AUDIT.md` gained the rows those
  numbers require.
- `docs/LIMITATIONS.md` grew rather than shrank.

## [0.1.0] - 2026-09-08

Interface changes made during this run, each replacing an earlier German-named equivalent that existed only inside this repository (never a public release):

### Changed -- CLI subcommands

- The goalbook subcommand family is now `hoh goal ...` (`list`, `propose`, `approve`, and related decisions), over a module renamed from `zielbuch.py` to `goalbook.py`.
- The quota-resume subcommand is now `resume-quota`, over a module renamed from `kontingent.py` to `quota.py`.

### Changed -- CLI flags

- Flags on the affected subcommands are now `--title`, `--description`, `--by`, and `--reason`, replacing their earlier German-named equivalents.

### Changed -- JSON output

- JSON output keys emitted by the goal and quota-resume commands were renamed to English -- for example `id`, `status`, `title`, `decided_by`, `by`, and `reason` -- so no command emits a German JSON key any more.

### Changed -- persisted schema

- The goalbook's persisted schema identifier is now `hoh-goalbook.v2`. The identifier is actually read and enforced on load: an unrecognized identifier is rejected by name instead of being guessed at, and a pre-existing `hoh-goalbook.v1` file is migrated rather than silently reinterpreted under the new field names.

### Added -- governance

- Added the seven public-repository governance files: `LICENSE`, `PROVENANCE.md`, `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md` (this file), and `CITATION.cff`. `LICENSE` documents that no license has been chosen yet rather than asserting one.
## [0.1.0] - 2026-09-10 (release candidate)

### Added -- release-candidate documentation

- `RELEASE_NOTES.md`, stating the research-preview positioning, the real hurdles to using HoH, what changed on the way to this release candidate, and what is not yet claimed.
- `RC_GATE.md`, reporting the release-candidate gate condition by condition, each with a status and either structured evidence or the explicit `NOT_RUN` marker -- including the rows only an operator can fill in, such as git status and the still-undecided repository host.
- This section of `CHANGELOG.md`, naming the release candidate without disturbing the section above it.

### Not changed

No file outside these three was created, edited, deleted, or renamed to produce this release candidate. Nothing under `src/hoh/`, `tests/`, or `tools/` changed; `pyproject.toml` and `CITATION.cff` are untouched; the section above stands exactly as it was written.
