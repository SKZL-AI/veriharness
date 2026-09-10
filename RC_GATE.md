# RC_GATE — the `v0.1.0-rc1` release gate

This document reports the gate for `v0.1.0-rc1`, the release candidate. It is
a report, not an approval: this run creates no tag, makes no push, calls no
`gh`, and runs no `hoh deliver`. Those four actions stay the operator's --
the plan's STOP sits before every one of them, and a run cannot lift a STOP
placed there for exactly this reason.

Baseline for every row below that names a HEAD: `77e483ea6ae0502d77d6be9dd9367d0f48dae736`
(`git rev-parse HEAD`, 2026-09-10), the commit this run's own three documents
were written against, in this run's own development worktree.
Nothing this run measured used `git` from inside a check arena -- an arena
has no `.git` of its own (O30), so every row below either comes from a
command run in this development worktree (documented as such) or is marked
`NOT_RUN` and left for the operator.

The row set is not a fixed count typed here: it is every condition in this
specification's own §14 table, in the order that table lists them, plus the
operator row the amendment of 2026-09-10 requires be named explicitly
(`Repository-Adresse`). That is eleven rows today -- ten conditions from the
plan's §14, and the one declared operator row -- and the count is stated in
prose rather than as a heading number for the same reason the amendment
gives: standing counts age, and this campaign has already had to correct
four of them.

## What ships under the `paper` manifest rule

`DEC-R4` added a manifest rule named `paper`, covering the whole of `paper/**`
because, in the captain's own words, the paper and the claims it cites "ship
together or not at all." Concretely, that rule makes all seven of these
files public in the release, and this row exists so a reader of this gate
does not have to re-derive the manifest to find out:

| File | Contents | Produced by |
|---|---|---|
| `paper/POSITION_PAPER.md` | the paper | `D5`, `d5p`, `d5l` |
| `paper/NUMBERS.md` | every number in the paper, with its claim id | `D5`, `d5l` |
| `paper/FIGURES.md` | two mermaid diagrams | `D5` |
| `paper/AUDIT.md` | the citation and numbers audit of the paper | `D6` |
| `paper/REVIEW_A.md` | independent review A -- empirical overclaims | `D7-A` |
| `paper/REVIEW_B.md` | independent review B -- release/security/reproducibility | `D7-B` |
| `paper/REVIEW_CONSOLIDATED.md` | the consolidation of both reviews | `D7-K` |

This run states that these seven files are what the `paper` rule publishes.
It does not second-guess `DEC-R4`; whether any one of the seven should be
public is a question for a person, not something this document resolves.

## The gate

Every row below carries a status from `PASS` / `FAIL` / `NOT_RUN` / `PARTIAL`,
and every row that is not `NOT_RUN` carries structured evidence -- a command
and its exit code, a path, a run id, or a receipt id -- never a bare
assertion. `NOT_RUN` is not a failure to report; it is the honest word for
"not measured," and a gate with none of them would be either a completed
release or a dishonest report, and a reader could not tell which.

| Condition | Status | Evidence |
|---|---|---|
| Tests vollständig grün | `PASS` | `python3 -m pytest -q` -> `479 passed` in `39.40s`, exit `0`, run fresh in this working tree at HEAD `77e483e` (2026-09-10). This is the working-tree regime, not a materialized export artifact -- see `clean install grün` below, which this run's own scope does not build. Supplementary, same run: `ruff check --select F,E9 src tests tools` -> `All checks passed!`, exit `0`. |
| clean install grün | `NOT_RUN` | Not measured by this run: building a wheel and installing it into a fresh venv is operator work, outside this run's declared scope (`RELEASE_NOTES.md`, `RC_GATE.md`, and the `0.1.0-rc1` section of `CHANGELOG.md` only). `dogfood/ABSCHLUSSBERICHT.md` Section H records earlier passes of this condition, but at HEADs that predate this one by dozens of merged commits; restating those here as current would be exactly the kind of stale claim this document exists to avoid. |
| Plugin portabel | `NOT_RUN` | Not measured by this run: invoking `plugin/bin/hoh list` with `PYTHONPATH` removed, from outside the repository, is operator work outside this run's scope. Same staleness note as `clean install grün` applies to the last recorded measurement in `dogfood/ABSCHLUSSBERICHT.md` Section H. |
| keine privaten Pfade/Secrets | `PASS` | `scan_include_for_leaks()` from `tools/export_manifest.py`, called as a plain Python function (no shell, no `git`) against a fresh `derive()` of this tree at HEAD `77e483e`: `79` `INCLUDE`-classified entries out of `202` derived entries scanned for home-directory-style paths (needle assembled at runtime rather than spelled literally, the same discipline this document's own criterion 13 requires), private/loopback IPv4 address ranges, and token-shaped strings -- `0` findings. The file-set and address decisions this condition also depends on are settled, not open: `DEC-R1`, `DEC-R1a`, `DEC-R2`, and `DEC-R3` are all closed per the operator's own decision log (`dogfood/ABSCHLUSSBERICHT.md`), none of them reopened since. |
| README Quickstart funktioniert | `NOT_RUN` | Not measured by this run: the walkthrough only "runs through," rather than merely parses, with Herdr running (`HERDR_ENV=1`) and three role-session dispatches (planner, developer, QA) -- none of which this run invokes. `dogfood/ABSCHLUSSBERICHT.md` Section H records the commands in both `README.md` and `examples/minimal/README.md` as checked syntactically against the current CLI, never as executed end to end. |
| Claims Ledger vollständig | `PASS` | `python3 tools/check_claims.py check all`, run fresh at HEAD `77e483e`: exit `1`, `57` problem(s), every one of them tagged `ENVIRONMENT GAP` (`grep -c '^FAIL:'` and `grep -c 'ENVIRONMENT GAP'` on the same output both return `57`; zero unmarked). That `57` matches, counted independently from `CLAIMS.json` itself, the number of `receipt:`/`run:`/`receiptcount:`/`discriminated:` evidence references among its `122` claims. Measured against `check all`'s own shape rule (every reported failure `ENVIRONMENT GAP`-marked, the marked count equal to the number of `runs/`-dependent evidence references, no unmarked failure) rather than exit `0`, which is unreachable in any checkout without a `runs/` tree (O38). Supplementary, same run: `check coverage` -> `OK: every number-bearing sentence is covered`, exit `0`; `check a02` -> `OK: no claim resting on run a02 is SUPPORTED`, exit `0`; `check resolvability`, `check schema`, `check ids` all exit `0`. |
| Position Paper QA grün | `PASS` | Position paper (`D5`): accepted candidate `d5-i2` at iteration `2`, `14` of `14` criteria PASS, receipt `d5-i2-a1-AC01` exit `0`. Citation-and-numbers audit (`D6`): accepted candidate `d6-i2` at iteration `2`, `10` of `10` criteria PASS, receipt `d6-i2-a1-K1` exit `0`. Both from `dogfood/ABSCHLUSSBERICHT.md` Section B's run-trace table. |
| Double Review ohne offenen HIGH | `PASS` | Two independent reviews ran as separate `hoh run`s, per `dogfood/D7_REVIEW_AUFTRAG.md`'s revised (2026-09-08) design: `REVIEW_A` (run `d7-a`, accepted candidate `d7-a-i1`, receipt `d7-a-i1-a1-K1` exit `0`) and `REVIEW_B` (run `d7-b`, accepted candidate `d7-b-i2`, receipt `d7-b-i2-a1-K1` exit `0`) -- neither saw the other's context or findings before both finished. `REVIEW_CONSOLIDATED` (run `d7-k2`, candidate `d7-k2-i1`) then merged both, repairing nothing itself, per its own Findings table in `paper/REVIEW_CONSOLIDATED.md`. That table's severity column carries `8` raw HIGH findings; its classification column splits them `4` BLOCKER and `4` DOCUMENTED_LIMITATION. The `4` BLOCKER findings are each closed: `A-01` by loop run `d7r` (commit `d4ded2b`), `A-02` also by loop run `d7r` (commit `d4ded2b`), `B-04` by loop run `d7w` (commit `8dec6c3`), and `B-01` by loop run `d7x` (commit `fdedbb5`) -- verified directly in this tree, not taken on the commit messages' word alone (see the disposition table below). The `4` DOCUMENTED_LIMITATION findings (`B-05` through `B-08`) are the check-command-guard tripwire class disclaimed at `docs/LIMITATIONS.md` limit `4` and claim `C-016`, verified against a fresh positive control of `7` of `7` naive escape shapes still `BLOCKED`. `5` MEDIUM findings are TRACKED, none release-blocking. As of this HEAD no BLOCKER is open; the four remaining HIGH findings stand disclosed and disclaimed, not absent. |
| Git status sauber | `NOT_RUN` | Operator row, per O30: the check directory this run's own documents are graded in has no `.git` of its own, so no criterion in this run can call `git status` -- and this development worktree's own status will change the moment this run's documents are committed on top of the HEAD named above. To be filled in by the operator, as a porcelain line count, after that commit. |
| aktueller HEAD dokumentiert | `PASS` | This document's own baseline, stated once at the top: HEAD `77e483ea6ae0502d77d6be9dd9367d0f48dae736`, from `git rev-parse HEAD` run in this development worktree on 2026-09-10 -- the commit these three release-candidate documents were written against. |
| Repository-Adresse | `PASS` | **Operatorzeile, ausgefuellt am 2026-09-10.** Der Host wurde bei `DEC-R3` ausdruecklich NICHT entschieden, deshalb trug `pyproject.toml` bis hierher den RFC-2606-Platzhalter `example.invalid`. Mit der Freigabe zur Veroeffentlichung ist er entschieden: `[project.urls]` traegt jetzt `Repository = "https://github.com/SKZL-AI/veriharness"`. Der Platzhalter war kein Versehen, sondern die ehrliche Form einer offenen Entscheidung -- und diese Zeile ist ihr Abschluss. |

## Findings disposition, for the double-review row above

Read from `paper/REVIEW_CONSOLIDATED.md`'s own Findings table, not typed:

| Finding | Severity | Classification | Closed by |
|---|---|---|---|
| `A-01` | HIGH | BLOCKER | loop run `d7r`, commit `d4ded2b` -- `paper/POSITION_PAPER.md` §9 now reads "the six of the seven accepted dogfood runs," matching the §3 table it used to contradict |
| `A-02` | HIGH | BLOCKER | loop run `d7r`, commit `d4ded2b` -- `paper/POSITION_PAPER.md` now names `git rev-parse --show-toplevel` and the ancestor-repository failure mode alongside the `TMPDIR` half of limit 6 |
| `B-01` | HIGH | BLOCKER | loop run `d7x`, commit `fdedbb5` -- `EXPORT_MANIFEST.json` re-derived; a fresh `check` at this HEAD reports only the expected, separately-tracked `runs/` regime mismatch (O89), not the 37 missing plus 2 misclassified paths the finding named |
| `B-04` | HIGH | BLOCKER | loop run `d7w`, commit `8dec6c3` -- `CLAIMS.json`'s `C-064` note no longer claims its evidence file is "present in any checkout"; it now names the file `EXCLUDE` and its evidence locally-resolved |
| `B-05`-`B-08` | HIGH | DOCUMENTED_LIMITATION | not fixed, and not release-blocking: disclaimed in `docs/LIMITATIONS.md` limit 4 and claim `C-016`, with a positive control (`7`/`7` naive escapes still `BLOCKED`) confirming the guard still stops accidents even though it is not a security boundary |
| `A-03`, `B-02`, `B-03`, `B-09`, `B-10` | MEDIUM | TRACKED | not release-blocking under `paper/REVIEW_CONSOLIDATED.md`'s own classification rule (`BLOCKER` is reproducible HIGH only) |

## What this run did not do

No tag was created. No `git push`, no remote, no `gh` call. No `hoh deliver`,
with or without `--approve`. No file this run reports on was edited to make
this gate greener -- in particular, none of the seven `paper/**` files above
and no file under `src/hoh/`, `tests/`, or `tools/`. Where this run found a
condition it could not measure honestly, it wrote `NOT_RUN`, not a guess.
