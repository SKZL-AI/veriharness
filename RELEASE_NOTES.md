# Release notes -- v0.3.0a2 (pre-release)

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

## Added

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

## Changed

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

## Fixed

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

## Known limitations at this pre-release, and what is not claimed

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

## Check this pre-release yourself

Some of these commands need the development repository and not only the exported
tree; for each, what it does in the exported tree is said.

* `python3 tools/changelog_from_release_notes.py check` -- the `CHANGELOG.md`
  section of this release is generated from this file, not written twice. Works
  in the exported tree.
* `python3 -m pytest --collect-only -q` -- the exported tests collect without an
  error. Works in the exported tree.
* `python3 tools/docs_numbers.py check` -- every tagged figure in these
  documents against the command or ledger entry that produced it. It reads the
  development repository's git history and working ledger, which the export does
  not carry: in the exported tree it reports an environment gap and exits 3.
* `python3 tools/check_claims.py check all` and
  `python3 tools/export_manifest.py check` -- the claims ledger and the export
  classification. They check the whole development tree, including its run
  evidence and the files the export leaves out: in the exported tree both exit 1.
* `python3 tools/capability_matrix.py` and `python3 tools/build_plan.py` --
  the register and the P2 gate whose figures this section states. They read
  the programme's requirement register, which is derived from an internal
  planning package and is not exported: in the exported tree both exit 3.

---

# Release notes -- v0.3.0a1 (pre-release)

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

## Added

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

## Changed

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

## Fixed

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

## Known limitations at this pre-release, and what is not claimed

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

## Check this pre-release yourself

* `python3 tools/docs_numbers.py check` -- every tagged figure in these
  documents against the command or ledger entry that produced it.
* `python3 tools/changelog_from_release_notes.py check` -- the `CHANGELOG.md`
  section of this release is generated from this file, not written twice.
* `python3 tools/check_claims.py check all` and
  `python3 tools/export_manifest.py check` -- the claims ledger and the export
  classification.
* `python3 tools/capability_matrix.py` and `python3 tools/build_plan.py` --
  the register and the P2 gate this file refers to.

---

# Release notes -- v0.1.0

`v0.1.0` is the first stable tag. It supersedes `v0.1.0-rc3`; all three release
candidates stay published, tagged and unmoved, because they are the record of
what this project said about itself on the way here.

**Read this first.** VeriHarness is a research preview of an evidence and
verification harness. It is good at producing a record that survives scrutiny.
It is expensive at the task level, and on the five tasks it has been measured
against, a plain agent was cheaper and at least as correct. Both halves of that
sentence are in this release because both are measured.

## The headline: a baseline exists now, and it does not favour the harness

For most of this project's life its own `docs/LIMITATIONS.md` limit 2 said
there was **no baseline of any kind**. There are now three matched-budget
campaigns against a plain agent, none of them edited in the light of another.

Campaign **v3** was pre-registered in full -- 5 tasks x 3 arms x 3 repetitions,
unconditionally -- and the registration's exact bytes were bound in a git
commit **12.8 seconds before the first cell started**. That ordering is
established from git objects in
`docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` and re-derived by
`tests/test_prereg_provenance.py` on every suite run, not asserted.

| arm | hidden suite | false accepts | what it produced | dispatches |
|---|---|---|---|---|
| A -- plain agent | 15/15 PASS | 0 | answered | 1 per cell |
| B -- one `hoh run` | 14/15 PASS | **1** | 15/15 accepted | 6-7 |
| C -- full control plane | 14/15 PASS | 0 | **15/15 `BUDGET_EXHAUSTED`, 0/15 `CLOSED`** | 9 |

* **Arm A, the plain agent, passed everything** -- on one dispatch per cell, a
  ninth of what the harnessed arms were allowed.
* **Arm B produced the one false accept**: its own acceptance criteria green,
  the hidden suite red, on a normalisation that dropped a character. It is the
  defect class this benchmark exists to detect, and it **reproduced campaign
  v2's** -- same task, same arm. Three repetitions carry no rate; they carry
  that it was not a one-off.
* **Arm C reached no fixpoint at all.** Every one of its fifteen cells spent
  the nine dispatches on the primary node, met a red global gate, spawned a
  repair node, and had the repair refused for want of budget. In v2 the same
  arm closed five of five -- on eighteen dispatches per cell against a declared
  nine, because each run drew its own ceiling. The budget is now enforced per
  **cell** and shared with the repair nodes, and under the rule the protocol
  actually writes down, this control plane does not close on these tasks.
* **Arm C's zero false accepts is therefore not a correctness result.** A false
  accept requires an arm to claim it is finished and be wrong; arm C never
  claimed it. Quoting `0` for arm C beside arm B's `1` compares *answered
  wrongly once* with *never answered*.

`benchmark_v3 = PASS` on the readiness board means the campaign was
pre-registered, commit-bound, complete at 45/45, matched-budget-valid, and
reproducible from the raw cell files by a second, independently written
aggregation that never imports the reporter. It does **not** mean the harness
performed better. `docs/LIMITATIONS.md` section 19 says so in the document a
reader is most likely to quote from.

### The other half, measured separately

A ceiling that refuses too early is indistinguishable from one that refuses
correctly, if the only case you measure is the one where it binds. So the
positive path has its own evidence, at a budget **taken from v2** -- eighteen,
what v2's arm C actually spent reaching closure -- declared before the run and
never raised after a failure: the control plane reaches a real `CLOSED`
fixpoint through a full repair cycle, unattended. 18 of 18 declared dispatches,
9 primary and 9 repair, one repair node, **zero human decisions**.

The first attempt at that test failed with zero provider calls, because the
launcher had been built without an approval authority. The declared budget did
not move between attempts, and both artifacts are on disk.

## What is new in the software since rc3

* **The dispatch budget is enforced, not described.** `Budgets.max_dispatches`
  is checked *before* the counter is charged, persisted to run state before the
  provider call, shared by a node and its repair nodes, and survives a restart.
  Exhaustion is its own verdict (`BUDGET_EXHAUSTED`), its own halt class, and
  its own exception type -- never a generic failure.
* **A capability witness that a retry cannot launder.** Re-witnessing after a
  retry re-takes the baseline for named paths only, so a hostile write during
  the retry window can no longer be absorbed into the baseline.
* **Fail-closed environment-gap skips.** A test may skip for a missing artifact
  only when `EXPORT_MANIFEST.json` declares that path excluded for the reason
  the test expects. Missing-and-included fails. Missing-and-unclassified fails.
  Missing manifest fails. Wrong reason fails. Five negative controls hold it.
* **Telemetry that counts calls, not lines.** A retried role writes one log line
  for three calls and a refused role writes one for none, so the dispatch count
  now comes from a `provider_calls` field rather than from counting lines.

## The findings this release is actually made of

Each of these would have shipped green. The harness found them in itself.

* A benchmark whose reported cost was a **constant written beside the result**
  rather than a measurement.
* An enforced ceiling **off by one**: `max_dispatches=9` bought eight provider
  calls, because the counter incremented before the budget was consulted. The
  existing test passed against it, because it only checked that *something*
  raised.
* A dispatch log counted one line per call -- wrong **in both directions**.
* A budget a restart **refunded**, because the counter lived in a process.
* A capability witness that could be made to **absorb a hostile write** during a
  retry, found by an adversarial review tasked to refute rather than confirm.
* A published claim that **told readers how to verify it** and did not survive
  that verification: 167 of 329 failures in an export tree carried no
  environment-gap marker where the document promised none would.
* **Three release gates with no state in which they could fail** -- one asking a
  remote the internal tree deliberately does not have, one comparing a tree
  against itself.
* An export-skip rule that would have turned a **lost public file** green by
  skipping instead of red.
* A readiness row that counted **its own success message as a finding**: the
  export check prints "passes U2b + the leak scan", the row counted every line
  mentioning U2b, and a green export therefore reported "1 dangling
  reference(s)" while saying nothing about the ten acknowledged ones.
* Three documents that said a fresh clone reports 30 environment-gap skips.
  That holds for a `--depth 1` clone and not a full one, where two of the skips
  do not exist -- found by running this export's own suite before pushing it,
  and corrected in all three rather than in the one that was quoted most.
* A sentence in the position paper that carried a correct number and **a
  command beside it that computed something else** -- lines rather than
  distinct ids -- so a reader following the printed recipe would not get the
  printed figure.
* The export's own reference checker harvesting paths only from backticked
  spans and markdown links, so sixteen new audit rows naming internal evidence
  trees in bare form **passed it green**. The one exemption that existed before
  them had only ever been caught because the same path happened to appear
  backticked elsewhere in its row. Fixed for the references at hand; the
  detection gap is a tracked defect, not a patch written in the hour before a
  tag.
* And, before campaign v3 was frozen: an earlier version of the budget
  instrument **passed seven of eight controls against a build with the
  enforcement removed**. Five of its controls were reading the test fixture's
  own loop guard rather than the product's refusal. Two reviewers tasked to
  refute it found that, which is why the instrument now runs at three ceilings
  of which at least one must force the refusal inside an iteration -- or it
  refuses to run at all.

## What has not changed

* The positioning. This is a research preview, not a production system.
* The four hurdles in `README.md`: a written specification, a running Herdr,
  three agent dispatches per iteration, and JSON output.
* `docs/LIMITATIONS.md` remains the document to read before deciding this is
  ready for anything you care about. It has grown, not shrunk.
* No measurement left this machine. The public repository is an export: every
  file in it is `INCLUDE`-classified in `EXPORT_MANIFEST.json`, and the run
  evidence, raw receipts and internal working documents deliberately stay out.
  That is why a fresh clone reports explicit environment-gap skips, each naming
  the withheld artifact and why -- counted separately from its passes, and
  never summed with them. Measured on a fresh clone of the published tag:
  **30 skips** in a `--depth 1` clone and **28** in a full one, two of them
  existing only when the history is shallow. The passes beside them were 1221
  and 1223. The tagged tree's own copy of this paragraph says 1220 and 1222 --
  it was written before the last regression test was added, and the tag is not
  being moved to hide that. See the note at the end of these notes.

## Verifying this release yourself

```sh
git clone --depth 1 https://github.com/SKZL-AI/veriharness
cd veriharness
python3 -m pytest -q                        # passes and environment-gap skips, separately
python3 tools/check_claims.py check all     # every number bound to a claim or a reason
python3 tools/export_manifest.py check      # the export's own classification and leak scan
ruff check --select F,E9 src tests tools
```

`docs/READINESS.md` is the board: every row, its verdict, and the exact command
that re-derives it -- including the rows that are advisory and the one that is
`UNSUPPORTED_ENVIRONMENT` because GitHub's runners cannot create the namespace
it needs.


## Corrected after the tag was cut, and the tag is not being moved

`v0.1.0` was published, and then verified the way these notes tell a reader to
verify it: a fresh clone of the published tag, suite run, numbers compared.
Two of them were one short.

The tagged tree's own copy of this file, of `README.md` and of the position
paper says a `--depth 1` clone reports **1220 passed** and a full clone
**1222**. Measured on the published tag: **1221** and **1223**. The skip
counts -- 30 and 28 -- were right, and they are the figure those sentences are
about. The passes moved because the last regression test added before the tag
(for finding O169, the attribution check that had no reachable green state)
landed after the sentence was written.

This is the third instance of one class in a single release, and the class is
the subject of the section it keeps damaging: a number and the state it was
taken at drifting apart. The first was a heading tally with a command beside it
that counted something else; the second was a skip count true of one clone
shape and not the other; this is the third. The wording in all three places now
leads with the figure that does not move and names the tag and the clone the
others were taken in.

`v0.1.0` stays published, tagged and unmoved, exactly as `v0.1.0-rc1` through
`rc3` did when the same thing happened to them. A tag that is silently
corrected is worse than a tag with a known, stated, one-off-by-one error, and
this project has said so in print three times now.

---

# Release notes -- v0.1.0-rc3

`v0.1.0-rc3` supersedes `v0.1.0-rc2`, published the same day. Both earlier tags
stay published and unmoved. No behaviour of `hoh` changed in either step.

rc3 exists because verifying rc2 *after* publishing it found a defect that
every gate before publication had missed, and the way it was missed is the
interesting part.

## What rc3 corrects

The attribution table in `README.md` counts commits to `src/hoh/`, `tests/`,
`docs/`, `paper/` and `tools/`. That table lives in `README.md`, and it is
written in the same commit that touches those very paths -- so **the commit
that writes the numbers changes the numbers.** Measured at `HEAD`, three of the
table's five rows were off by one the moment they were committed.

A second, independent defect sat next to it: the table said "measured over the
non-merge commits reachable from the mainline" without saying *which* mainline.
Read against this published repository, whose mainline is two commits long,
every one of those figures is wrong. They only ever described the development
repository.

rc3 therefore:

- names the repository and the exact commit the figures are measured at, and
  states plainly that they are not reproducible in this export;
- corrects the three rows: `tests/` 70/19/51, `docs/` 15/9/6, `paper/` 10/9/1;
- measures the attribution at that named commit rather than at `HEAD`, so the
  figure stops moving.

**One claim flipped, and it is worth naming rather than quietly restating.**
rc2 reported `paper/` as 9/9/**0** -- nothing written to the position paper
outside a run. That was true until sections 1.1 and 1.2 were written directly
by the orchestrating session to produce rc2 itself. The column now reads 1, and
the README names which commit it is.

## How it was found, and why that matters more than the fix

Every gate was green. The ten-step closure chain: green. The export
verification: green. The post-publish audit: green. The tag was pushed and the
release published.

The defect surfaced only when the number checker was pointed at a **fresh clone
of the published repository** instead of the tree it was written in. Every gate
ran inside the same tree the figure was derived from, so every gate agreed with
it. That is not a gap in any individual check -- it is a property of checking a
measurement against the thing it measured.

This is the same shape as two limits this project already documents:
`subject_head` is not `attestation_commit`, and `RC_CLOSED` is not
`POST_PUBLISH_CONSISTENT`. The new instance adds self-reference: here, writing
the measurement down changes it. A measurement of a tree has to name the tree.

---

# Release notes -- v0.1.0-rc2

`v0.1.0-rc2` supersedes `v0.1.0-rc1`. It is a **documentation and positioning
correction**, not new functionality: no behaviour of `hoh` changed between the
two tags. `v0.1.0-rc1` stays published, tagged and unmoved -- it is the
historical artifact of what this project said about itself on 2026-09-10
before the corrections below, and deleting or retagging it would destroy
exactly the kind of record this project claims to keep.

## What rc2 corrects, and why there was anything to correct

Four claims in the rc1 documents were wrong or misleading. All four were
found after publication, three of them by a reader's objection rather than by
a gate, which is itself worth stating plainly.

| Where | What rc1 said | Why it was wrong |
|---|---|---|
| `README.md` | "There is no automatism, and there should not be" | True of the kernel, false as a description of the product. HoH does not self-start, but this project's own campaign drove it automatically from an orchestrating agent session across five days. The heading read as "this thing cannot be automated". |
| `README.md`, `RELEASE_NOTES.md` | The dogfood evidence "covers documentation and governance work, never a change to `src/` or `tests/`" | False on both halves. 19 of 70 commits under `tests/` arrived via run branches, and one run added two files under `src/hoh/policy/`. |
| `docs/LIMITATIONS.md` limit 1 | Described the campaign as *attended*, with "an operator" writing each specification | Misattributed the system's own top layer as external supervision. The specifications, merges and repairs came from an agent session, not a person typing. |
| `docs/LIMITATIONS.md` limit 8 | "No run has ever added or edited a line inside `src/`" | Overstated. Run `d1` added `src/hoh/policy/dangerous-patterns.txt` and `src/hoh/policy/house-rules-patterns.txt`, merged as accepted candidate `d1-i2`. Recorded as finding `O94`. |

None of these were code defects, and none of them changed what the software
does. They were claims about the project's own evidence -- which is the class
of statement this project holds itself to most strictly, so getting them
wrong in the first public release is worth naming rather than quietly fixing.

rc2 additionally adds what rc1 omitted entirely: a `What has actually been
exercised` section in `README.md` reporting the campaign in measured numbers,
an attribution table naming which authority wrote which paths, the four-layer
architecture in `docs/ARCHITECTURE.md`, and §1.1/§1.2 of the position paper on
global composition closure and the operating model the evidence came from.

Every number added is bound: `kampagne-zahlen.py` re-derives all of them from
the run records and the commit history and fails the release gate on any
disagreement, with a negative control that falsifies each number in turn and
requires the checker to catch it.

## The merge problem, restated

rc1 stated the composition limit in a form that had already stopped being
complete: *"nothing in the loop checks the union of two runs' changes"*. The
corrected two-level statement, now in `docs/LIMITATIONS.md` §9 and the paper's
§1.1:

- **Per run:** `Accept(A)` and `Accept(B)` still do not imply `Accept(A ∪ B)`.
  The acceptance loop is not closed under composition, and no amount of global
  gating closes it.
- **System level:** above that loop sit the union invariants `U1`-`U5`,
  semantic dependency measurement between parallel candidates, and a post-DAG
  closure pass that turns any global failure into a new repair run and re-runs
  closure until it reaches a fixpoint -- `DAG_TERMINAL != RC_CLOSED`.

This is not a claim that composition is solved. `U1`-`U5` are a named,
extensible list of failure shapes that have actually occurred; an unknown
cross-run interaction would pass all of them. What the global layer changes is
whether such an inconsistency reaches a release -- during this very release it
caught two, after the run graph was already terminal, which became repair runs
`d8b` and `d8c`.

---

# Release notes -- v0.1.0-rc1

*Preserved below exactly as published, apart from the four corrections marked
`[corrected in rc2]`. This is the record of what was claimed at rc1.*

## What this is, plainly, before anything else

This is a research preview, and it is a tool for someone who already wants
this discipline -- a written specification, evidence-bound acceptance,
independent verification before anything is trusted -- and is willing to pay
its running cost. It is not a tool that makes that discipline easy or cheap
to adopt, and this release does not claim otherwise. `hoh` is the
command-line package, module, and CLI that implement the Harness-of-Harness
pattern; VeriHarness is the project and repository built on that pattern.

## The four real hurdles

Adopting HoH costs something before it produces anything, and pretending
otherwise would undercut an evidence-first project's own point:

| Hurdle | Why it counts |
|---|---|
| A written specification | The planner role has nothing to bind acceptance criteria to without one, and few projects walk in with one already written. |
| Herdr running, with `HERDR_ENV=1` | HoH refuses to start otherwise, on purpose -- a real prerequisite, not a formality. |
| Three agent sessions of quota per iteration | Planner, developer, and QA each run as their own dispatch, before counting the per-criterion control runs this loop also spends. |
| JSON output | `hoh` commands print structured JSON, operable by scripts and other tools -- not designed to be inviting to read on its own. |

## What changed on the way to `0.1.0-rc1`

- This run (`hoh-d8`) added three release-candidate documents -- this file,
  `RC_GATE.md`, and a new section in `CHANGELOG.md` -- and nothing else: no
  file under `src/hoh/`, `tests/`, or `tools/` changed to produce them, and
  no file this release candidate reports on (including all seven files the
  `paper` manifest rule publishes) was edited by this run.
- The position paper (`paper/POSITION_PAPER.md`) and its citation-and-numbers
  audit (`paper/AUDIT.md`) each passed their own QA gate: `D5` accepted the
  paper at its second iteration, `14` of `14` criteria; `D6` accepted the
  audit at its second iteration, `10` of `10` criteria.
- Two independent reviews of the release candidate -- `paper/REVIEW_A.md` and
  `paper/REVIEW_B.md`, run as separate sessions that never saw each other's
  findings before both finished -- found eight HIGH-severity findings between
  them, consolidated without repair in `paper/REVIEW_CONSOLIDATED.md`. The
  four classified BLOCKER are now closed: `A-01` and `A-02` were corrected by
  loop run `d7r`, `B-04` by loop run `d7w`, and `B-01` by loop run `d7x` --
  none of them by the operator. The remaining four are classified
  DOCUMENTED_LIMITATION and were not fixed by either the loop or the
  operator: they are a disclosed, disclaimed property of the check-command
  guard, not a silent gap (see `docs/LIMITATIONS.md`, limit 4).
- Eleven core HoH defects and five guard gaps found during this campaign's
  own dogfooding were fixed **at the orchestrator level** -- by the
  orchestrating agent session working outside a run, not by a person typing
  and not by an HoH developer role -- under a narrow, declared exception to
  the rule that the loop may not touch `src/hoh/` or `tests/`; the full
  accounting, defect by defect, is the internal *Abschlussbericht*
  point D. Outside that declared exception, the loop never touched HoH's own
  production code to produce this project's evidence about itself.
  **[corrected in rc2.** rc1 wrote "by the operator" here, which reads as a
  person typing; the fixes were made by the orchestrating agent session
  working above the run boundary. The sentence about production code is
  corrected separately below.**]**

## The double review, stated precisely

`paper/POSITION_PAPER.md` went through two genuinely independent reviews --
`paper/REVIEW_A.md` (empirical honesty against `CLAIMS.json`) and
`paper/REVIEW_B.md` (release, security, export, and operational safety) --
each a separate `hoh run`, staggered, with no shared output directory and no
access to the other's context or findings before both finished.
`paper/REVIEW_CONSOLIDATED.md` then merged both, repairing nothing itself.
This release candidate does not claim there is no open HIGH finding: it
claims that the reproducible, blocking findings are closed, names which loop
run closed each one, and reports that the rest are documented rather than
silent. `RC_GATE.md` carries the full severity-and-disposition breakdown.

**A separate, smaller reduction, named here because it is about this very
document.** The house rules on this machine normally call for two
independent adversarial subagents to review any verdict before it is
committed. For the run that produced these three files, that gate was
replaced by a single external QA pass instead: the role structure for this
run names one independent QA reviewer, not two, and the developer role that
wrote `RELEASE_NOTES.md`, `RC_GATE.md`, and the `CHANGELOG.md` addition was
explicitly told not to spawn review subagents of its own, on the grounds
that the external QA role already sits outside and above it. That is a real
reduction in coverage of unknown size, not an equivalent, and it is named
here rather than left implicit.

## What this dogfood evidence does and does not cover

~~The evidence behind this release candidate covers documentation, packaging,
and governance work -- not a single change to HoH's own production code.~~
**[corrected in rc2]** That claim is too wide on both halves. Measured over
the non-merge commits reachable from the mainline: `tests/` has 70 commits, 19 of them
arriving via run branches across three run-authored files; `src/hoh/` has 49
commits, one of which arrived via a run branch -- run `d1` adding
`src/hoh/policy/dangerous-patterns.txt` and `src/hoh/policy/house-rules-patterns.txt` so an installed wheel
could execute an acceptance check at all. What survives, and is worth saying:
**no run has ever added or edited a line of Python under `src/`.** HoH's own
logic was never written by a run. See `docs/LIMITATIONS.md` limit 8, and
finding `O94` for how the overstatement was found.

## What is not claimed

This document does not restate `docs/LIMITATIONS.md`; read it before
deciding this is production-ready for anything you care about. It names,
among other things, that the check-command guard is a tripwire against
accidents rather than a security boundary, that there is no matched-budget
comparison against a plain agent working the same specification, and that
local run correctness does not imply correctness after a merge of several
runs' work.

No part of this project fixed itself: every repair named in this document
was made either by the operator, under a narrow, declared exception, or by
the loop working an ordinary, scoped `hoh run` iteration against
documentation or release tooling -- never by HoH observing its own failure
and correcting it unsupervised. This release makes no claim of autonomous
self-repair, self-healing, or anything "fixing itself": every fix credited
above names the loop or the operator, by run id or by role, because that is
the distinction this project is built to keep honest.

## What this run did, and what the operator did after it

**Run `D8`, which produced this document, published nothing.** It created no
tag, made no push, added no remote and never called `hoh deliver` -- those
were outside its declared scope, and `RC_GATE.md`'s "What this run did not do"
records that boundary as it stood when the gate was measured.

**Publication happened afterwards**, once the release closure was complete:
`v0.1.0-rc1` was tagged and pushed on a human decision that this run neither
made nor was permitted to make. The orchestrating agent session prepared and
verified the export; the decision to publish it was reserved from delegation
and taken by a person. `v0.1.0-rc2` follows the same split. Those two sentences are deliberately kept apart, because
one is a statement about a run and the other about a person, and collapsing
them is the exact class of claim this project exists to keep separate.
`RC_GATE.md` reports the gate condition by condition, including the rows
only the operator can fill in.
