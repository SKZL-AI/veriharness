# Release notes -- v0.4.0a1 (pre-release)

**This is a pre-release and, until its tag is cut, an unreleased draft.** `v0.4.0a1` is the first public release of the `v0.4` line:
an alpha and a research preview, published as a GitHub pre-release and to PyPI as a pre-release version (`pip install --pre hoh`).
`v0.4.0` is reserved for the closure of phase P3 (*v0.4 Capacity-Aware Autonomous Parallelism*) and is unreleased; P3 is in
progress and nothing in this section says it is closed. The facts about P3 are written from the register at the cut and not
before. The operator decided on 2026-10-20 to publish this pre-release (instead of waiting for the closure of P3) so that the
public README, the CI of the public repository and the PyPI path are brought up to date.

The slice P3-ROTD is part of it: a rotation request is typed, as a fixed-form notice, into the master's own pane when that pane is reachable, after a fresh read of Herdr's state and of the pane's visible screen and never into an open dialog; otherwise it is written to a request file and recorded as `rotation_undeliverable`; delivery is on by default and `--rotate-no-deliver` turns it off. It is tested with fakes and has not been observed delivering into a live master (taken on 2026-10-21).

This is not a readiness claim: the board, `docs/READINESS.md`, reads `TECHNICALLY_STABLE_READY = no` `[V13]`, as it did at `v0.3.0`.
The sections of the earlier releases further down in this file are the text of their tags with their dated corrections, and their
figures are the figures of their own cuts; the `[Vnn]` figures of this section are the ones that `paper/NUMBERS_V2.md` carries, taken
at the commit aefef58a (committed on 2026-10-20 at 20:24 +0300). No benchmark result exists for this line: the matched-budget campaigns of
`v0.1.0` are published as they were measured and have not been repeated on this code; the pre-registered agentic benchmark has
a protocol and no result.

## Added

* **A front for the README.** What VeriHarness is in one paragraph, the loop as a diagram, the facts of how the project has been built
  (a measured table with the method and the limit of each figure), the chain of orchestrating sessions and the storage steward as
  diagrams, the status of phases P1 to P5 from the capability register, a quick start, the benchmark explained, the limitations, the
  note on the name and the citation. The README of 2026-10-13 follows it unchanged, as the dated record. The diagrams are hand-authored
  SVG under `.github/assets/` (no external fonts or scripts), with a 1280 by 640 PNG for a social preview.
* **The measured autonomy facts.** At the base of this section the build's lease records hold 22 epochs, 17 of them
  verified successions that all ended COMPLETE, each requested by the rotation policy; 250 run
  directories since 2026-10-01; 1161 review reports with the machine-bound footer, 229 of them RELEASE_NO; and 103 of
  2,087 entries of the orchestrator's running notes marked as the operator's. The history is 1,571 commits, 291 of them
  merges `[V48]`, over 43 days 20 hours `[V46]`, and 32 of the 44 calendar days carry a commit `[V47]`; the ledger runs to O271 with 243 numbered entries `[V49]`.
* **A release table in the distribution tool.** `tools/distribution_release.py` accepts a tag only if it is listed in `RELEASES` with
  its pinned tag object and commit; a listed release whose identity is not pinned is refused. `v0.4.0a1` is declared as a pre-release and
  unpinned: the two values are added after the tag exists. The workflow compares the GitHub release's pre-release flag with the table. The checks of `v0.1.0` are unchanged.

## Changed

* **`.github/workflows/publish-pypi.yml`** takes the release tag as its input and derives the file names, the index addresses, the
  environment addresses and the artifact name from it. The file name and the two environment names (`testpypi`, `pypi`) are unchanged
  because the trusted publishers on the indexes are registered against them. The build-once rule, the full-file-set check against an
  existing index release and the refusal of an unknown tag are unchanged.
* **`tools/distribution_smoke.py`** reads the version it must find from the workflow instead of the literal `0.1.0`; a run that was
  not told what to expect fails.
* **`.github/workflows/ci.yml`** installs the project with its `test` and `archive` extras.

## Fixed

* **The public CI.** From the push of `v0.3.0a1` on 2026-10-08 the `suite` job stopped at collection: `tests/test_archive_bundle.py`
  and `tests/test_archive_verify.py` import `zstandard` directly, which `pyproject.toml` declares as the `archive` extra, and the
  workflow installed the package without it. The other four jobs were green. The cause was reproduced in a fresh virtual environment
  (collection error, exit 2, the same two files) and the workflow now names the extras instead of skipping the tests.
* **The suite behind that stop.** Collection no longer stops, and the suite shows what the stop had been hiding: in a clean one-commit
  checkout of the exported `v0.3.0` tree, with `zstandard` installed, 59 tests failed and 6,942 passed. Almost all of the failures
  are tests of the development repository that read records the export withholds (the requirement register and what is derived from
  it, the pinned scope of the planning package, one acceptance record, the preserved reviews of the P2 gate, one runbook); two need a git
  identity, which the workflow now sets, and one needs a live Herdr session. The tests that need withheld records or Herdr are listed in
  `tests/conftest.py` and skip where the record or the session is absent, with the reason named, and only when the export manifest
  declares the record withheld; `tests/test_export_gap_table.py` guards the list. In the development tree nothing is skipped.

## Known limitations

* **Everything the `v0.3.0` section below says about its limits stands.** The documentation, CI and packaging changes of this draft lift no requirement and change no source file.
* **Two sentences of the `v0.3.0` section are superseded when `v0.4.0a1` is cut.** That section says the release is "not published
  to PyPI" and that the distribution workflow "accepts only the audited `v0.1.0`". The workflow accepts a tag that is listed and
  pinned in `tools/distribution_release.py`; until `v0.4.0a1` is uploaded PyPI holds `v0.1.0` only (read on 2026-10-21), and a pre-release is installed only with `--pre`. The section is
  kept as it was written.
* **The sections of `v0.3.0a1` and `v0.3.0a2` below say that the context rollover had not run live** (`v0.3.0a1`: "no live succession has been run").
  That was true of their code on their dates and stands as written. The `rotation.md` packaged with this pre-release was corrected by the slice P3-ROTD
  and says the protocol has been run live; the account above and in the README front is the current one.
* **The count of product-run merges is by merge title** `[V01]`, in the window 2026-09-25 to 2026-10-11; the tool that counts them has not been
  changed for the slices merged since the third alpha, whose titles it does not list.
* **The development line reads differently from `v0.3.0`.** On it the register's durability rows of P2 read IMPLEMENTED_NOT_PROVEN
  again, because the P3 slices changed source files that the evidence is bound to (a regression obligation under amendment A7, not a
  re-opened phase); `v0.3.0` is the code the closure evidence was taken on. At this base the register holds 236
  requirements, 88 PROVEN and 11 PARTIAL `[V10]`, and the P2 gate reads 0 blocking open; 0 exit conditions bound to requirements not PROVEN; 0 exit conditions nothing measures yet; 148 open requirements, 125 behind a phase gate `[V12]`; of the requirements
  registered for P3 to P12, 131 of 177 MISSING (11 PARTIAL, 35 PROVEN) `[V11b]`.

---

# Release notes -- v0.3.0

**This is a release, not a pre-release.** `v0.3.0` is the first release of the
v0.3 line and the release at which phase P2, *v0.3 Durable Program Runtime +
Autonomy Contract Base*, is closed. It follows `v0.3.0a3` (tagged 2026-10-11) by
the ledger entries O266 to O268 and is cut at the commit of these documents, which
sits behind the closure record of P2 (records entry O268) and the regenerated
figures that follow it; everything between the third alpha and this commit is
records, tools, tests, the register notes of three P2 rows (P2-07, P2-28 and
P2-42, by addition) and the version line of `pyproject.toml`, and no file under
`src/hoh/` has changed since the third alpha. The sections of the three alphas further down in this file are the
text of their tags with their dated corrections, and their figures are the
figures of their own cuts; the `[Vnn]` figures of this section are the ones that
`paper/NUMBERS_V2.md` now carries, taken at the commit f78d1cb9.

`pyproject.toml` carries `0.3.0` (since the merge `9dacb6c5`, which landed before
the sealing commit of P2 because the version is not a record path). Like the
alphas it is installed from the tagged source and is **not** published to PyPI:
the distribution workflow of this repository accepts only the audited `v0.1.0`,
which stays the only release on PyPI. **Readiness is not claimed.** The board,
`docs/READINESS.md`, is regenerated in the closure sequence and reads
`TECHNICALLY_STABLE_READY = no` `[V13]`: a closed phase is not a stable release.
The rows of the board that do not read PASS at the sealing commit are named under
*Known limitations*.

**What this release is, in one paragraph.** Phase P2 is closed at the commit of
the records entry O268 (commit X of amendment A7), on four things that were
measured at the sealing commit S and carried to X by records-only commits: the
digests of the durability evidence, the digest of the committed capability
matrix with every one of P2's 43 registered requirements PROVEN `[V10c]` `[V10b]`,
the identity of the independent supervisor review `20261013T093422Z-S`, and the
digest of the checklist snapshot. The gate of P2 as `tools/build_plan.py`
measures it has 0 requirements blocking and open, 0 exit conditions bound to
requirements not PROVEN and 0 exit conditions that nothing measures `[V12]`; the
register holds 235 requirements, 70 PROVEN and 9 PARTIAL `[V10]`.
Since the third alpha the line gained the records of the durability freeze and
the operator's restart (P2-11 and P2-33 to P2-37 PROVEN), of the first run of a
registered project (P2-42 PROVEN), the instruments that decide the gate honestly
(tools and tests only), the attribution of every commit, and the closure sequence
R, S, C, A and X described below. Nothing in it was run on a real NixOS or macOS
host or against a real provider limit, and the role runs of the durability
evidence are a deterministic fixture; those limits are stated under *Known
limitations* and stand.

**The closure sequence (all 2026-10-13, UTC; records entry O268 names each by
its full commit id and digests).** R `204905be` regenerated the artefacts at the
merge of the records folds (`bcd75cb7`): the export manifest, the capability
matrix, the build plan, the checklist snapshot. S `c4dc643d` recorded the
independent supervisor review `20261013T093422Z-S` and regenerated the board. C
`642a4fa9` committed the succession capsule alone, written on a clean tree with S
as its internal commit. A `b36eed34` added the attribution entries for S and C. X
`d3791470` is the closure record itself: the ledger entry O268, the public
closure texts by addition, and the derived record of the eight-minute durability
run made on R. The commit after X regenerated the figures at X and added the
ledger addendum of the closure, and this commit carries the release documents.
`python3 tools/succession.py verify` reads VERIFIED at this commit: the commits
behind the capsule touch record paths only, which the capsule's rules do not read
as drift.

The central invariant of this development line is unchanged:

    candidate creation != product acceptance != integration acceptance != merge authority != phase closure

## Added

* **The closure record of P2 (records entry O268, commit X).** The record names
  the commits R, S, C and A; the checklist snapshot by its seal and its file
  digest; the committed capability matrix by its digest and its file digest; the
  register digest; the plan snapshot commit and the plan digest; the independent
  supervisor review `20261013T093422Z-S` with the digests of its capsule and its
  findings; the durability record with its evidence head and the empty diff of
  the relevant source paths since that head; the limits, verbatim; the test
  suite at R; the instrument fixes of the P2 line; and the regression
  obligations that stand after the closure, each with its trigger, its runner
  and the place its result is recorded. `README.md`, `docs/LIMITATIONS.md` and
  `paper/POSITION_PAPER_V2.md` say that P2 is closed at that commit, by addition,
  and the dated sentences in them that read P2 as open describe their dates.
* **The independent supervisor review `20261013T093422Z-S` (sealing commit S).**
  A separate session on a read-only worktree of R, given the canonical capsule
  and the repository and nothing else, re-measured every finding that the
  preserved reviews of 2026-09-24 had left open and answered each NO_ACTION in
  the resolution rule's shape (the evidence reference of each answer is the
  `resolve_with` entry of the finding it answers); none blocking, the review
  complete against the checklist snapshot seal and the matrix digest it names,
  no new finding that blocks P2. Its drift check reads NOT_RUN, because the
  baseline it would compare with carries no source digests; NOT_RUN is never a
  pass and the review's completeness does not rest on it. The previous review
  record is kept beside it, versioned, and the earlier versions are untouched.
* **The succession capsule bound to the P2 exit (merge `a2fc3eb7`, commit C).**
  `tools/build_plan.py` measures `tools/succession.py verify` for the exit
  condition "controller session replacement", fail closed, reading the capsule
  committed at HEAD and the board, sync, release and ledger from HEAD's blobs;
  records-only commits behind the capsule are not drift, a source change is. The
  capsule of C was written on a clean tree with S as its internal commit and is
  the one the gate reads at X and at this commit.
* **Durability: P2-11 and P2-33 to P2-37 PROVEN on evidence taken on `e0d9bdff`
  (records entry O266).** The durability end-to-end run was made on a `git
  archive` of `e0d9bdff`, the last commit that changed `src/hoh/`: every
  scenario ran, every negative control was caught, the reboot scenario was armed
  at that commit, the operator shut the WSL virtual machine down and started it
  again, and the verification after the restart found the lease holder on
  another boot, spent an epoch as RECOVERY and closed the fixture project with
  the canonical tick. The record re-derives from its evidence (`derive --check`
  exit 0) and the capability matrix refuses it when `src/hoh/`, the continuation
  unit, the harness or the fixture differ from that commit; they do not differ in
  this release, and the diff of those paths from `e0d9bdff` to this commit prints
  nothing.
* **The eight-minute run at R, recorded (records entry O268; amendment A7's
  first records checkpoint after the evidence).** `tools/durability_e2e.py run`
  on a `git archive` of R: no harness failure, a clean tree at start and end,
  every negative control ran; the derived record reads P2-33 to P2-37 satisfied
  and P2-11 not satisfied because that run armed no reboot (the reboot evidence
  is the committed record above and is not re-taken at a records checkpoint).
  The derived record is copied byte-identically into the tree as a data file
  under the record directory of the closure, named with its digest in the ledger
  entry O268; its evidence directory is not copied, so `derive --check` against
  that copy is NOT_RUN from the repository alone.
* **P2-42 PROVEN on the first run of a registered project (records entry
  O266).** The strand that completed the export leak scan (merged as
  `f956b119`, which the third alpha contained) was run live as a registered
  project with real Herdr panes, and a readback found every temporary workspace
  of the run under the registered root. The hold on the row named three
  conditions; the lift rests on the first of them under the operator's ruling,
  with the approval-resume of a registered run not exercised and the temporary
  files of the other role harnesses NOT_RUN.
* **Instruments that decide the gate honestly (merges `a00e3270`, `cdc4159f`,
  `9c487146`; tools and tests only).** A probe's test half reads `collected`
  only when pytest collected it, and a broken collection is NOT_DETERMINABLE per
  file; a later review resolves a finding only by naming the review that raised
  it; the board's lint, identifiers and tests rows are measured on the
  repository side; `tools/docs_numbers.py` reads the status of P2-42 from the
  register and a met P2 gate from `tools/build_plan.py`; the three P2 state tests
  assert the post-O266 truth (capability coverage closed, the gate held only by
  its named exit conditions).
* **The records before the sealing commit (merges `9dacb6c5`, `8ad54273`,
  `bcd75cb7`).** The version `0.3.0` and the edition heading of the figures
  table; the attribution of the commits that no entry had claimed, with the
  run evidence of five product runs copied into the tree; the folds of the
  review minors of the records entry O266 and of the two slices before, the
  register decision on P2-07 (the capsule's currency is a P2 exit condition,
  measured by the build plan, fail closed), and the obligations of the closure
  written as obligations before the capsule commit (records entry O267), so that
  nothing committed after the capsule opens an item of the ledger.
* **Regression obligations under amendment A7 (records entry O268, section 9).**
  "The durability end-to-end run of about eight minutes is re-run at every
  records checkpoint and every release checkpoint after X, and the reboot
  evidence of the durability test is taken again at every release checkpoint;
  the result of each is recorded with the commit it was taken on." And, from the
  operator directive of 2026-10-10: "Later src/hoh changes would then not reopen
  P2 but only create regression obligations: re-run the 8-minute durability run
  at records and release checkpoints and re-take the reboot evidence at release
  checkpoints." The tag `v0.3.0` carries no source change against `e0d9bdff`,
  and the operator ruled on 2026-10-12 that it therefore needs no new reboot;
  the eight-minute run at the tag is the orchestrating session's and is recorded
  in the ledger entry of the tag, not in this package. An obligation that fails
  re-opens P2 by a records entry, in the open, that names the failing run and the
  commit it was taken on.

## Changed

* **The register.** Of all 235 requirements, 70 read PROVEN, 9 PARTIAL, 0
  IMPLEMENTED_NOT_PROVEN and 156 MISSING `[V10]` (63 PROVEN, 10 PARTIAL and 6
  IMPLEMENTED_NOT_PROVEN at the third alpha); all 43 requirements of P2 read
  PROVEN `[V10c]` `[V10b]` (36 at the third alpha). The matrix measures an import
  statement, never execution: the weight of each PROVEN rests on the merge run's
  criteria, its integration pair and the limits written into its note. The later
  phases hold 156 of 176 MISSING (9 PARTIAL, 11 PROVEN) `[V11b]`.
* **The P2 gate.** 0 blocking open; 0 exit conditions bound to requirements not PROVEN; 0 exit conditions nothing measures yet; 165 open requirements, 125 behind a phase gate `[V12]`, where the third alpha had 7 blocking requirements and the two checklist-gate
  conditions of the records entry O266 unmet. The closure is a statement about
  the commit of the closure record and about nothing later.
* **The attribution ratio.** Of 320 development nodes after the attribution anchor
  `[V06]`, 1 went through the shipped control plane end to end `[V06a]`, 131 were
  developed by a product run and merged by the orchestrating session after
  review `[V06b]`, and 188 were written by the orchestrating session directly
  `[V06c]` (294, 1, 126 and 167 at the third alpha). Every commit of the line
  belongs to an entry of `dogfood/ATTRIBUTION.json` apart from the tip of the
  moment, which the check tolerates, and 0 of 367 entries are of category
  CAPTAIN, a human decision or a human-authored change `[V52]`.
* **The history.** 1,260 commits, 182 of them merges `[V48]`; the first and the latest
  commit of the mainline are 36 days 19 hours apart `[V46]`; 27 of the 37 calendar days carry a
  commit `[V47]`; the ledger runs to O268 with 240 numbered entries `[V49]`.
* **The count of product-run merges stands at the third alpha's values.** The
  line has 97 product-run merges since 2026-09-25 `[V01]`, 78 of which change
  `src/hoh/` `[V02]`, as at the third alpha: the four product runs merged since
  (the two instrument slices, the succession binding with its repair, and the
  re-stated state tests) carry titles that the counting tool does not list, and
  this release changes no tool, because after the sealing commit nothing outside
  the record paths lands until the tag. The count is corrected when the tool is
  next changed, after the tag; none of the four changes `src/hoh/`.
* **The v0.3.0a3 pre-release was exported and tagged** on 2026-10-11 (export
  commit `d98092cc`), as an annotated tag and a GitHub pre-release whose text is
  the a3 section of this file at the tag; `v0.1.0` stayed the release that
  GitHub marks as latest. This release is cut the same way, from the merge of
  the release documents after their review; it is not marked as a pre-release,
  and it is the release GitHub marks as latest (the orchestrating session's
  decision at the cut; `v0.1.0` stays the only release on PyPI).
* **The version and the dates.** `pyproject.toml` has carried `0.3.0` since
  the merge `9dacb6c5`; `CITATION.cff` carries `0.3.0` with the date of the tag;
  the edition heading of `paper/NUMBERS_V2.md` names the release.

## Fixed

* The gate of the succession capsule could be passed by a capsule whose
  `internal_commit` was a symbolic name (`HEAD`, a branch), which never drifts:
  both integration reviewers of the binding found it independently and refused
  the candidate; the repair requires one full commit id that resolves to itself,
  reads the plan root from the environment only and refuses to write a capsule
  at a HEAD that is not a full commit id (merge `a2fc3eb7`).
* The two instruments that decided P2's open supervisor findings read a probe's
  test half as `collected` whether or not pytest had collected it, and a later
  review could resolve a finding without naming the review that raised it; both
  fail honestly now (merge `a00e3270`).
* The figures tool stated the status of P2-42 as a fixed text that contradicted
  the register after the lift, and the P2 gate as unmet by its own reading; it
  now derives both. The re-anchoring of one claim that the rewrite had orphaned
  was found at integration review and repaired as records before the merge
  (merge `cdc4159f`).
* Three state tests asserted that P2 was the open frontier and were red after
  the O266 records; they assert the post-O266 truth now (merge `9c487146`). The
  test nodes that pin "unmet" for the P2 exit turn red at the closure by design
  and are re-stated as the first test change after the tag (see the
  limitations).
* The closure record's README paragraph cited an internal amendment file by its
  path from an exported line, which the export's closure rule refuses; the
  paragraph names the amendment without the path (the first revision of X,
  found by the full suite on the candidate).
* Records corrections, each by addition: the rotation count of the orchestrating
  session in `README.md` counted one rotation twice; three present-tense rows of
  `docs/LIMITATIONS.md` that the records entry O266 had superseded point to the
  lift; the register notes of P2-07, P2-28 and P2-42 carry the current reading
  (merge `bcd75cb7`, records entry O267).

## Known limitations at this release, and what is not claimed

* **What the closure is not.** It is not a release claim: the board reads
  `TECHNICALLY_STABLE_READY = no` `[V13]`. It is not a statement about any commit
  after the closure record. It lifts no limit of the evidence it rests on. A
  change to `src/hoh/`, the continuation unit, the harness or the fixture after
  the closure makes the six durability rows read stale until the eight-minute
  run is repeated, and creates the regression obligations above; it does not
  re-open the phase, and a failed obligation does.
* **The durability evidence, with its limits (records entries O266 and O268).**
  The restart was `wsl --shutdown`, the shutdown of a WSL2 virtual machine and a
  new start with another kernel boot id, not a power cut or a hardware reset,
  and a write under an unclean power loss was not exercised. One reboot, on one
  host, of one fixture project; role runs are the deterministic fixture and the
  tick is the harness's own loop running the arguments of the continuation unit,
  so systemd, the timer unit and the plugin binary are bypassed and a real role
  agent's dialogs are not covered. The record lists as undiscriminated every
  check of the reboot scenario and the problem classes of the three scenarios
  P2-11 needs: no negative control targets them, so the instrument cannot show
  that a build without reboot recovery would fail them. The evidence is about
  `e0d9bdff`. The operator compacted the WSL virtual disk while it was down, an
  operator act after which the record still re-derives. The paused master was
  ended by the shutdown, not by a harness signal.
* **The scope of the other lifted rows.** The no-human reading of P2-34 covers
  the orchestration layer: a real role agent can still wait on an operator. A
  resume service on by default (OP-16) is a P5 item and is not read into P2-36.
  The lift of P2-42 rests on one run of one registered project on one host with
  the Claude role harness only; the approval-resume of a registered run and the
  refusal paths were not exercised live. The rotation of the orchestrating
  session has the limits of the third alpha: one project on one host; the
  expiry, withdrawal and error paths not exercised live; the item-level rotation
  without a caller outside the tests; the capsule input derived by the
  orchestrating session's script before each rotation, not by the product.
* **The test suite at R, and the nodes the closure turns red by design.** Two
  measurements of the full suite exist at R and differ: the orchestrating
  session's run and the board's `tests` row, taken between the passes of the
  regeneration with the tree dirty in record paths; both are recorded in the
  closure record with the cause of each named failure, neither is re-run for
  this release, and which nodes the board's extra failures are is left open
  there. Six test nodes pin "unmet" for the P2 exit and turn red at the commit
  at which the exit turns; a test change is not a record path and would break
  the succession window, so they are re-stated to the closed state as the first
  test change after the tag. One test of the program scope builds the scope
  from the plan without the operator amendments and fails at every commit of
  this period while the scope check with amendments reads COMPLETE; it is
  brought to the amendments after the tag.
* **The board.** The rows that do not read PASS at the sealing commit, with the
  reasons the board gives: the test suite and the union invariants (the full
  suite at R in the real repository read 7082 passed, 4 failed, 2 skipped, with
  the cause of each failure named in the closure record; the board's own `tests`
  row, taken between the passes of the regeneration, read 7078 passed, 8 failed
  at the sealing commit and 7079 passed, 7 failed at the figures commit after X;
  records entries O267 and O268); the benchmark freeze, which reads DRIFTED because files moved
  after the freeze and the later modules are not named in its artefact, and both
  benchmark rows with it (one advisory); the export manifest, which was out of
  date at S, X and the commit after X by design and is re-derived in this
  commit; the export sync, which has no export checkout set; the succession row
  as it stood before the capsule commit, re-measured after X; the consistency
  check, which reads the working tree against the base of the figures table and
  is re-rendered in this commit; and the external CI, whose recorded run tested
  a different export. Lint, identifiers, attribution, claims, the capability
  matrix, the program scope, durability, acceptance, the checklist and the
  supervisor review read PASS.
* **The product-run count does not include the four runs merged since the third
  alpha** (above); the figure is right for the titles it lists and is corrected
  with the tool after the tag.
* **The earlier limitations stand where they are not named above.** Nothing of
  `hoh wait`, `hoh surface` and `hoh run --detach` has run against a live Herdr
  session; a provider usage limit is read from recorded shapes only; the NixOS
  portability patch has not run on a host and `hoh agent-setup` has not been
  tried by a live agent session; no merge step of the product calls the merge
  gate; the integrity gate is met for the replayed paths and not for every path;
  the storage steward has not reclaimed anything of VeriHarness's own data.
* **One self-test of the audit tooling reads red at this commit, and the ledger
  is not what is wrong.** `python3 tools/audit_refs.py selftest` uses the claim id
  C-999 as its fixture of an id that must not exist; the claims ledger of this
  release reaches that id, so the self-test reports its fixture as wrongly
  resolved. The fixture is corrected with the tool after the tag (no tool changes
  in the records window); the other seven audit checks and
  `python3 tools/check_claims.py check all` read green at this commit.
* **Phase P3 is the next phase, and nothing of it is lifted.** Of the
  requirements registered for P3 to P12, 156 of 176 MISSING (9 PARTIAL, 11 PROVEN) read as stated `[V11b]`; the P3 candidates that exist were produced
  during the closure window and are reviewed as candidates by their integration
  pairs after the tag; none is merged by this release.
* **Not claimed:** that any figure measured for `v0.1.0` describes this line;
  that the exact-head external CI has run on these bytes (it has not); that the
  durability evidence covers a power loss, a second host, a real role agent or
  more than one reboot; that a rotation of the orchestrating session is reliable
  beyond the recorded rotations; that the portability patch works on a real
  NixOS or macOS host; that a coding agent given the output of `hoh agent-setup`
  uses `hoh`; that long-duration unattended operation is demonstrated (it is
  not); or that this release is ready in the sense of the board (it is not).

## Check this release yourself

Some of these commands need the development repository and not only the exported
tree. For each, what it does in the exported tree is said; the exported-tree exit
codes were measured for the third alpha and are not measured again here.

* `python3 tools/changelog_from_release_notes.py check` -- the `CHANGELOG.md`
  section of this release is generated from this file and not written twice. It
  works in the exported tree.
* `python3 -m pytest --collect-only -q` -- the exported tests collect with no
  collection error; the command works in the exported tree.
* `python3 tools/docs_numbers.py check` -- every tagged figure of these
  documents against the command or the ledger entry that produced it. It reads
  the development repository's git history and working ledger, which the export
  does not carry; in the exported tree it reports an environment gap, exit 3.
* `python3 tools/check_claims.py check all` and
  `python3 tools/export_manifest.py check` -- the claims ledger and the export
  classification. They check the whole development tree, with its run evidence
  and the files the export leaves out; in the exported tree each of them exits 1.
* `python3 tools/capability_matrix.py` and `python3 tools/build_plan.py` --
  the register and the P2 gate, whose figures this section states. Both read
  the programme's requirement register, which is derived from an internal
  planning package and not exported; in the exported tree each of them exits 3.
* `python3 tools/succession.py verify` -- the succession capsule of the closure
  against the tree. It needs the development repository with its history, its
  run evidence and the plan root the capsule names; what it does in the exported
  tree was not measured for this release.
* `git diff e0d9bdff..v0.3.0 -- src/hoh ops/hoh-continue.service tools/durability_e2e.py tools/durability_fixture.py`
  -- prints nothing: the durability evidence of the closure is about the code
  this release ships.

---

# Release notes -- v0.3.0a3 (pre-release)

**This is a pre-release.** `v0.3.0a3` is the third alpha of the v0.3 line, a
research preview. It follows `v0.3.0a2` (tagged 2026-10-09) by the ledger
entries O261 to O265. It is prepared at a documentation checkpoint and **not**
after phase P2 closed: P2, *v0.3 Durable Program Runtime + Autonomy Contract
Base*, is in progress, its gate is not met, and every open gate below is listed
as NOT_YET_PROVEN. The latest stable release is still `v0.1.0`. The sections of
the two earlier alphas further down in this file are the text of their tags with
their dated corrections, and their figures are the figures of their own cuts;
the `[Vnn]` figures of this section are the ones that `paper/NUMBERS_V2.md` now
carries.

`pyproject.toml` carries `0.3.0a3`. Like the earlier alphas it is installed from
the tagged source and is not published to PyPI: the distribution workflow of
this repository accepts only the audited `v0.1.0`. `docs/READINESS.md` is
**unchanged** by this release, and readiness is not claimed. The board still
reads `TECHNICALLY_STABLE_READY = no` `[V13]`; that is the measurement taken at
commit `75214e31`, and it is not regenerated for this alpha either, because
regeneration needs the network (the `clean_install` row and the union gate's
wheel build call the package index). The `tests` row of the board is stale and
conservative: it predates every merge below.

**What this alpha adds, in one paragraph.** Everything in it was built by
product runs under the same discipline as the earlier alphas: each merge is of a
candidate the product had accepted, released by two independent integration
reviewers and bound by the merge gate; since `1e633e46` the gate of record is
the product's own `hoh merge-gate check`. The development line now holds 97
product-run merges, 78 of which change `src/hoh/` `[V01]` `[V02]` (69 and 55 at
the second alpha; the merges since then carry titles of their own, which the
count now lists, see *Changed*). The user-visible changes are commands:
`hoh merge-gate`, `hoh wait`, `hoh surface`, `hoh run --detach`, `hoh pin`,
`hoh import`, `hoh storage account`, `hoh storage new-target`,
`hoh storage register-project` and `hoh project create`, with a rotation policy
under which the product hands the orchestrating session over by itself. On the
register, disk-full-safe writes (P2-41), the classification and the park
binding of provider usage limits (P2-43), and the two rows that the first live
rotation of the orchestrating session bears on (P2-14, P2-20) now read PROVEN,
each under limits that its entry below states: 36 of P2's 43 registered
requirements read PROVEN `[V10c]` `[V10b]` (32 at the second alpha); P2 is
**not** closed. Apart from the one live rotation of the orchestrating session
described below, nothing in this paragraph was run in a live Herdr session, on a
real NixOS or macOS host, or against a real provider limit; those are NOT_RUN
and stay listed as such under *Known limitations*.

**Where the first integration pair said no.** Several candidates the product
had accepted were refused at the first round of integration review and merged
only together with a repair that a second pair released: the wiring of the
ephemeral temporary roots (a stale or duplicate run could write `PAUSED` into a
live master's run, and an unpark and re-park loop), the surface reaper (it
raised, and wrote to standard output, when standard error was broken), the
export leak scan (a UTF-16 text next to a non-ASCII character failed open), the
per-run storage account (an unproven aggregate read 0 while it was UNMEASURED),
the registration of a target clone (a symlinked `.git/objects` or refs
registered while the adoption path refuses it), the pins of the hygiene
allowlist (exactness and breadth unpinned) and the residue of the rollover
(a rule of `hold_lease` pinned for `sleep()` only). The repair of each is in its
merge. The first live dress rehearsal of the automatic rotation was refused by
Herdr itself, which is the last entry under *Fixed*.

The central invariant of this development line is unchanged:

    candidate creation != product acceptance != integration acceptance != merge authority != phase closure

## Added

* **Disk-full-safe durable writes, the closure slice (P2-41; merge `0985ecb9`,
  records O261).** The dispatcher's leaf write, its audit append and the
  recorded answer go through the durable-write primitives, with a directory
  file-descriptor form, and the restore filters lock files; the host helper and
  the bundle helper, which keep their own temp-fsync-publish code, have the
  full-disk tests of this closure. **P2-41 reads PROVEN, on text-level evidence and with
  limits:** the writer scan is lexical (an aliased call, `getattr`,
  `codecs.open` or `io.FileIO` is not seen); the host helper has no zero-byte
  test and the bundle helper no fsync-error test; the registry writer has a test
  of a failed replace and none of a full disk; the leaf write, the audit append
  and the recorded answer are now refused under the one-mebibyte free-space
  reserve, which the recorded answer did not obey before. Not claimed: a long
  unattended run that survives a full disk end to end (P2-40).
* **Provider usage limits: the park binding is enforced, and a developer's
  limit screen is a failed dispatch (P2-43; merges `b86b09e9`, `39348042` and
  `8d7f8c04`, records O262 and O264).** The binding that the second recovery
  slice wrote is now checked before a parked run is unblocked: a stale, foreign
  or restored park, two masters and a declaration over a park are refused, the
  park gate fails closed, and a resume that cannot be taken escalates. A
  developer turn that ends normally on a recorded provider usage or rate-limit
  screen is a failed dispatch and never a candidate for QA; a limit banner that
  was already on the developer's pane before the prompt was sent is not read as
  this turn's, and a pane that is still limited at the prompt stays a failed
  dispatch. **P2-43 reads PROVEN, with limits that matter:** the detection reads
  only the recorded shapes of the provider's banner, whole (a hyphenated hour
  limit, a weekly limit, a bare phrase or another language is not read); a line
  of a role's own words that is exactly a recorded banner among the last twelve
  lines is read as the banner; a repeated byte-identical rate-limit body after a
  long turn is missed; a status-area line that a healed pane left in its
  scrollback is read and the run is parked again until the resume limit and then
  handed to a person; no developer pane has been recorded on a real limit.
  `hoh unblock` and `hoh resume --force` lift a park without the verification,
  by design.
* **The product rotates the orchestrating session by itself (P2-14, P2-20;
  merges `3199ac32`, `b4a3fe8f` and `e0d9bdff`, records O265).** A rotation
  policy and its controller run under `hoh project hold --rotate-policy` and
  `hoh project rollover-accept --rotate-policy`, `hoh project create` makes the
  governed project record that they need, and every step fails closed. The
  residue of the rollover and of `hoh migrate` that the review found (an
  interval and a poll validated before anything acts, a dying hold giving its
  lease back, a migration refusing an unparseable record) is closed, with the
  lease-renewal rules of the hold pinned by tests. The first live rotation was run on 2026-10-11 by the
  product itself: the policy asked for it at its threshold, the master wrote its
  handoff, a capsule of derived facts was frozen, the successor re-derived every
  fact against the world, took the lease as the second epoch and the old session
  was fenced. **P2-14 and P2-20 read PROVEN, with the limits of that evidence:**
  one rotation, on one host, of the orchestrating role of one project, to one
  successor model in one permission mode; the paths of expiry, withdrawal and
  error were not run live; the capsule input of the canary was written before
  the newest run existed (tracked: derive it again before each rotation); a
  policy that names a transcript path is inherited by the successor; for P2-20
  only the verification path of the capsule ran live. The item-level context
  rotation that the probe of P2-14 names (`rotate` in `src/hoh/rotation.py` and
  `reachable`) has no caller outside the tests, so the status is a statement
  about the rotation of the session. A second rotation by the product completed
  later the same day; it is to be recorded in the closure records, none of which
  exists yet, and it is not counted here.
* **The merge gate as a product command (P5-36; merges `1be5ac70` and
  `1e633e46`).** `hoh merge-gate check` judges the review reports a candidate
  requires and writes the decision as a new receipt; `hoh merge-gate verify`
  decides a receipt's inputs again. A report counts only with a complete
  six-line footer that its reviewer declares complete, bound to the candidate,
  the base and the specification's digest, and not modified in the last minute;
  a partial or cut-off report is no verdict. The reading of a report is guarded:
  a FIFO, a socket, a device, a directory, a symlink or an oversize file is
  unreadable and never a hang. **P5-36 reads PARTIAL:** no merge step of the
  product calls the command, the receipt is not signed and the merge message is
  not generated from it.
* **`hoh wait` (P5-43; merges `577185d1`, `997a7dd3` and `7240719f`).** A
  read-only wait on a run's state, on a review report (`--report`, with the
  footer check and the stable-age rule of the merge gate) and on a process id
  (`--pid`, identified by its id and its start time, never signalled). It
  answers one JSON line and an exit code per outcome, never writes, never polls
  faster than once a second and never waits unbounded. A candidate-less final
  stage and a storage park that no timer will lift read `held`. **P5-43 reads
  MISSING, still:** the durable list of what was reported and the trigger of the
  reaper are not built.
* **A run's own terminal tabs are closed when the run is over (P5-39; merges
  `fb473c13` and `9fd95ff1`).** `hoh surface open|list|reap` records a tab as a
  run's own in an owned registry and closes it when the run is over (failed,
  cancelled, or ready for delivery with no live process; a checkpoint is not the
  end) and the shell in it is proven idle by a scan of every user's processes.
  A foreign tab, a running run and an unproven shell are never closed, the
  reaper never closes its own tab, a recorded directory that is a mount point is
  refused, and one `closed` line is written per surface under a run-directory
  lock. **P5-39 reads PARTIAL:** reviewer and drafter tabs of a strand without a
  run are not covered, and what a real Herdr does on the close is NOT_RUN.
* **A detached run (P5-42; merges `cf7c26c2` and `2b7978d5`).**
  `hoh run --detach` (with `--detach-timeout` and `--require-contract`) refuses
  in a read-only preflight before any tab or role exists, then opens an owned
  run-host tab, types one command of safe words, keeps a log and a create-only
  launch record, and returns once a new process marker appears. **P5-42 reads
  MISSING, still:** a refusal that only the tab can make reads "started", the
  named profile, the timeout from the specification, the dedicated pinned clone
  and the derived contract are not built, and whether Herdr behaves as documented
  is NOT_RUN.
* **Pin and import (P5-38; merge `91871a5a`).** `hoh pin` builds a
  specification-only pin commit by plumbing on a base the product resolved,
  create-only, with a digest trailer; `hoh import` fetches an accepted candidate
  into `refs/imports/<id>`, proves that its tree is the accepted one, previews
  the merge and writes a receipt. **P5-38 reads MISSING, still:** the topology
  check, the scope-of-diff check and registered receipts are not built, and an
  import of a real run through the command is NOT_RUN.
* **The per-run storage account (P5-40; merge `79ea2a90`).** `hoh storage
  account` reads bytes and entries over registry-owned paths, counts a hard link
  once, never answers 0 for what it could not measure (UNMEASURED, with a lower
  bound) and bounds every walk; it names a root that is not intact. **P5-40
  reads MISSING, still:** it does not enter `hoh status`, produces no receipt and
  admits nothing on entries.
* **Ephemeral temporary roots in the controller, and the role scratch of a
  registered project (P2-42; merges `485b6251`, `b3554d15` and `01fc9a09`).**
  For a run of a registered project, `hoh run` registers, admits and releases
  the scratch of its acceptance checks and of each iteration, the resume gate
  asks for the role and the iteration's workspace together, and a session held
  by another live process is refused with nothing written. The role scratch of
  such a run lives below the iteration's registered workspace; a pane whose
  `TMPDIR` is not the bound scratch and an adopted pane are refused and kept
  open. `hoh storage new-target` creates an owned, registered directory for a
  target clone (its owner record first) and `hoh storage register-project`
  registers a fresh clone, create-only, applying the git-store rule of the
  adoption path. The role prompt gets one bullet: helper files go to the
  role's scratch, not beside it. **P2-42 reads PARTIAL, still** `[V10d]`: no run
  of a registered project has been observed, a live run with real Herdr panes
  and the other harnesses' temporary files are NOT_RUN, and the approval-resume
  of a registered run cannot adopt the kept pane.
* **Criterion fidelity (merge `f44f90df`).** A specification that declares its
  criteria in one criteria block is held to them: the controller refuses a plan
  that drops, re-types, wraps or deselects one (`CRITERION_FIDELITY`). A
  specification without the block is accepted as before, with "criterion
  fidelity NOT_RUN" in the reason; older specifications are not back-filled.
  The merge also repairs a test-isolation defect that it exposed (a test
  reloaded the real `hoh.roles` and left a stale exception base; the product
  never reloads).
* **The export leak scan refuses private identifiers (merges `1869a46a` and
  `f956b119`).** The scan reads the bytes of every exported file, a UTF-16
  reading of them and the file name, refuses a private session or scratch
  identifier by digest and any session-shaped UUID that is not on a short list
  of synthetic ones, and treats an unreadable file as a finding; the export
  scans the exact bytes it is about to write, before the first write. The list
  of digests is complete for the six session prefixes named when it was
  extended, and an ordered character-set literal is no longer taken for a secret
  (a content-based rule, with no path or literal allowlist). Its limits are in
  `docs/LIMITATIONS.md` (a name carrier prints its path, a UTF-16 text at an odd
  offset is not read, the list names only the identifiers known when it was
  last extended).
* **The P2 checklist-gate binding (merge `74772dc1`).** `tools/build_plan.py`
  measures the three P2 checklist-gate items (the items, the fsck and the
  review) from the reconciled checklist and the supervisor board, fail closed;
  the exit conditions that nothing measures read 0 now `[V12]`, where the
  second alpha had 3. It is tools-only and lifts no row; the gate stays shut for
  measured reasons.
* **Identifiers and hygiene (merges `0c3037c1`, `addb7e0f` and `c618a0d2`).**
  The project store and the storage registry refuse a project id that is not
  exactly a name (a trailing newline was the only bad id accepted) and
  `hoh project` answers one with a refusal; the test hygiene rules for legacy
  scratch are pinned, falsy evidence no longer binds and a duplicate id fails,
  and the hygiene allowlist is pinned to the file and the literal in both
  directions and a stale entry is refused (test-only).
* **New CLI surface** (development, not a stable interface): `hoh merge-gate`,
  `hoh wait`, `hoh surface`, `hoh pin`, `hoh import`; `hoh run --detach`;
  `hoh storage account`, `hoh storage new-target`,
  `hoh storage register-project`; `hoh project create` and the option
  `--rotate-policy` of `hoh project hold` and `hoh project rollover-accept`.

## Changed

* **The register.** Of all 235 requirements, 63 read PROVEN, 10 PARTIAL, 6
  IMPLEMENTED_NOT_PROVEN and 156 MISSING `[V10]` (227 requirements and 59
  PROVEN at the second alpha; amendment A6 pulled eight rows forward, P5-36 to
  P5-43); P2 is 36 PROVEN, 1 PARTIAL (P2-42), 6 IMPLEMENTED_NOT_PROVEN (P2-11
  and P2-33 to P2-37) and none MISSING `[V10c]`. The matrix measures an import
  statement, never execution: the weight of each PROVEN above rests on the merge
  run's criteria, its integration pair and the limits written into its note. The
  later phases hold 156 of 176 requirements MISSING, 9 PARTIAL and 11 PROVEN
  `[V11b]`.
* **The P2 gate.** It has 7 requirements blocking and open `[V12]`, where the
  second alpha had 11: P2-11, P2-33 to P2-37 and P2-42. The three exit
  conditions that no instrument measured at the second alpha are measured now
  (above), and the gate is still shut.
* **P2-14 is held, and then lifted on a live record.** The merge of the
  rotation policy carried an `equivalent` that held P2-14 at PARTIAL in the same
  commit, because without it the matrix would have read PROVEN from an import
  and fakes alone. The hold was lifted by the records entry of the live
  rotation, on that evidence and with the limits above, not by the merge.
* **Amendments A6 and A7.** A6 (the agent-session orchestration surface) pulled
  eight rows into the plan (P5-36 to P5-43: the merge gate as a command, review
  start, pin and import, the reaper, the storage account, scoped reclaim, the
  detached run, the wait) and dispositioned the mined to-do items. A7 records
  that a phase is closed at a commit by a closure record, and that later changes
  to the source create regression obligations and do not re-open a closed phase.
  No phase is closed by it.
* **The public description states the measured reality.** The README and the
  documents it points to say what the build is (orchestrated development with product
  runs since 2026-09-25, not an unattended or continuous run) with its figures
  derived by `tools/docs_numbers.py` (merge `b352c667`, ledger O263).
* **How product-run merges are counted.** `tools/docs_numbers.py` counts a
  product-run merge by its title. The merges since the second alpha carry
  titles of their own, and the count lists them one by one (a test pins them and
  a lookalike that must not count); a convention for the title of a product-run
  merge would end the list and has not been made. The table heading names this
  pre-release.
* **The v0.3.0a2 pre-release was exported and tagged** on 2026-10-09 (export
  commit `e95e09a0`); a later export (`a84e4030`) carried the refreshed public
  description only and is not a release.

## Fixed

* A zero-byte write made the archive bundle writer spin. A criterion of the
  P2-41 closure found it; a three-line guard fixes it.
* A run id of `..` escaped the runs root through `hoh run` and `--detach`
  (HIGH). A run id is refused unless it is a plain direct child of the runs
  root, a name with a `_` or `.` prefix and the names `backups` and `projects`
  are refused, and `hoh` answers with a refusal and exit 2. Detach types its
  options as `--flag=value` and refuses empty values and leading dashes before
  anything starts.
* A developer turn that ended on a recorded limit screen reached QA, and a
  good re-run after a limit wait was parked again by a banner already on the
  reused pane (both P2-43, above).
* The export leak scan failed open on a UTF-16 text next to a non-ASCII
  character; the account of a storage root read 0 for what it could not
  measure; the reaper raised on a broken standard error; and the registration of
  a target clone accepted a symlinked `.git/objects` or refs. All four were
  found at integration review and repaired before their merge.
* The successor of a rotation was started under an agent name that Herdr
  refuses (a digit-leading token with upper-case letters). The first dress
  rehearsal of the automatic rotation ended `LAUNCH_FAILED` on it with the
  predecessor still master and its lease intact; every earlier test used a fake
  Herdr that accepted any name. The name now obeys Herdr's rule (a fixed letter,
  the random part, the session id lower-cased with every other character mapped
  to `-`, at most 24 characters) and a strict fake refuses names exactly as
  Herdr does.

## Known limitations at this pre-release, and what is not claimed

Each open gate below is NOT_YET_PROVEN, and a gate that reads red stays red.

* **P2 is not closed.** Its gate has 7 requirements blocking and open `[V12]`.
  P2-11 needs a real reboot that has never been run, and the reboot proof stays
  NOT_RUN until the operator's restart. P2-33 to P2-37 read
  IMPLEMENTED_NOT_PROVEN because their durability evidence is stale and the
  evidence tooling has not been run at the freeze commit (the durability freeze
  is armed at `e0d9bdff`). P2-42 reads PARTIAL `[V10d]`
  (above) and its hold stays until the closure records exist. P2-14 and P2-20
  read PROVEN on one live rotation with the limits stated above, and no closure
  record exists yet.
* **Durability evidence is not taken at this commit.** `src/hoh/` is identical
  in this release to the tip it is cut from; the durability run, the reboot
  evidence and the full suite are not part of this package (the suite was run
  by the orchestrator at each merge, with the same environment failures as the
  base each time; this package does not repeat it).
* **A provider usage limit is read from recorded shapes only.** See P2-43
  above: other wordings are not read, and no developer pane has been recorded on
  a real limit. Whether first-prompt readiness cures the stall of an agent's
  first prompt stays SUSPECTED until a live check; nothing in this alpha
  changes it.
* **Rotation has one live record.** The expiry, withdrawal and error paths of
  the controller have not run live as evidence, the second rotation of the same
  day is not yet recorded, and the item-level rotation has no caller outside the
  tests.
* **The new commands have not run against a live Herdr session.** `hoh wait`,
  `hoh surface` and `hoh run --detach` were tested with fakes and temporary
  directories and reviewed; what a real Herdr does on a pane close or a tab
  create with an environment is NOT_RUN.
* **Owned agent control, portability and agent-setup are as at the second
  alpha.** The limits of P2-17, of the NixOS-first portability patch (never run
  on a host) and of `hoh agent-setup` (not tried by a live agent session) are
  not changed by anything in this alpha.
* **No merge step of the product calls the merge gate.** The orchestrator's
  script ran beside the command at every merge; the two disagreed on more than
  half of a probe set, and a replay of the older receipts found that many merges
  had been made on a reviewed base that was no longer the first parent (the
  product command refuses that).
* **The earlier limitations stand where they are not named above.** The
  integrity gate is met for the replayed paths and not for every path; the role
  pane lifecycle ends a role's own processes with the limits the first alpha
  states; the QA-gap gate is not merged; the storage steward has not reclaimed
  anything of VeriHarness's own data.
* **Smaller tracked items.** Lint and the identifier check are red at this
  commit, as they were at the second alpha, and with more findings than then:
  the findings are in the test files and the modules merged since, and this
  release does not fix them (it changes no source or test file apart from the
  tests of `tools/docs_numbers.py`). Two merge messages of the second alpha's
  period under-report their reviews, and the board's `tests` row is stale.
* **Not claimed:** that any figure measured for `v0.1.0` describes this line;
  that the exact-head external CI has run on these bytes (it has not); that a
  rotation of the orchestrating session is reliable beyond the one live record
  (it is not shown); that the portability patch works on a real NixOS or macOS
  host; that a coding agent given the output of `hoh agent-setup` uses `hoh`; or
  that long-duration unattended operation is demonstrated (it is not).
* **Corrections after the tag (records entry O266, 2026-10-11; the text above is kept as published).** (a) The statements above that no run of a registered
  project has been observed are out of date: the live run of a registered project (the strand that completed the export leak scan, merge `f956b119`, which this release contains) was made
  before the tag was cut and was not counted; P2-42 now reads PROVEN on it, with its limits (the approval-resume limit not met, one run, one harness) in the
  register note and in `docs/LIMITATIONS.md`. (b) The sentence that the durability freeze is armed at `e0d9bdff` is backed by the durability record, which
  names that commit as its evidence head and which the capability matrix refuses when `src/hoh/`, the continuation unit, the harness or the fixture differ from
  it (they do not differ in this release). (c) After the tag: the operator's restart and the freeze evidence were taken, P2-11 and P2-33 to P2-37 read PROVEN on
  evidence taken on `e0d9bdff` (with the limits above and in `docs/LIMITATIONS.md`), the P2 gate has no blocking requirement open, and two checklist-gate
  conditions (a current independent supervisor review and the consistency check) are unmet: P2 is not closed.

## Check this pre-release yourself

Some of these commands need the development repository and not only the exported
tree. For each, what it does in the exported tree is said.

* `python3 tools/changelog_from_release_notes.py check` -- the `CHANGELOG.md`
  section of this release is generated from this file and not written twice. It
  works in the exported tree.
* `python3 -m pytest --collect-only -q` -- the exported tests collect without
  an error. It works in the exported tree.
* `python3 tools/docs_numbers.py check` -- every tagged figure of these
  documents against the command or the ledger entry that produced it. It reads
  the development repository's git history and working ledger, which the export
  does not carry; in the exported tree it reports an environment gap and exits 3.
* `python3 tools/check_claims.py check all` and
  `python3 tools/export_manifest.py check` -- the claims ledger and the export
  classification. They check the whole development tree, with its run evidence
  and the files the export leaves out; in the exported tree both exit 1.
* `python3 tools/capability_matrix.py` and `python3 tools/build_plan.py` --
  the register and the P2 gate, whose figures this section states. They read
  the programme's requirement register, which is derived from an internal
  planning package and not exported; in the exported tree both exit 3.

---

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
