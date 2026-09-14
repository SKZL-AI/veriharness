# VeriHarness — Numbers

Every number that appears in `POSITION_PAPER.md`'s prose, after excluding
versions, dates, section/list ordinals, code blocks, and this project's own
identifiers (claim ids, finding/run ids). Where a number's source names a
claim id, that number appears in the claim's own `text` or `evidence` field
in `CLAIMS.json` -- checked directly against the ledger, not retyped from
memory. A number with no ledger claim is listed with a plain description in
the source column instead of a claim id, per the specification's own
instruction for that case.

`CLAIMS.json`'s own coverage is `README.md` and `docs/**`, plus the mandatory
`a02` invalidation entries and a fixed set of unflattering measured findings
(see `POSITION_PAPER.md` §0). Numbers sourced to the internal *Abschlussbericht*
or `DOGFOOD_LEDGER.md` below therefore carry a plain-text source, not a claim
id, because the ledger does not cover those documents -- listing them this
way, rather than inventing a claim id for them, is the honest gap the
specification for this draft asks for.

| number | claim id / source | note |
|---|---|---|
| 0 | C-056 | `docs/LIMITATIONS.md`: `git archive HEAD` yields 0 entries under `runs/`. |
| 0 | C-059 | `docs/LIMITATIONS.md` limit 12's own illustration: d1 iteration 1's acceptance-governing `discriminates` flag was 0, because that iteration was an outage with no QA verdict. |
| 0 | C-073 | `docs/LIMITATIONS.md` limit 16: `hoh resume-quota`, run over this project's full history, reported 0 runs identified as quota-blocked. |
| 1 | structural: iteration ordinal ("iteration 1") and section cross-reference (§1) used throughout the paper's run tables and prose | Not itself a distinct ledger measurement; the iteration-1 figures it labels are covered under their own claim ids elsewhere in this table (e.g. the row for 5, 13, 8, 9, 3, 11). |
| 2 | C-050 | a03 iteration 2: 2 of 3 checks with both a `-basis` and a candidate receipt had differing exit codes. |
| 2 | C-054 | `docs/LIMITATIONS.md` limit 9: the two O41 merge-defect instances total 2 merges. |
| 2 | C-055 | `docs/LIMITATIONS.md` limit 10: 2 runs in this project's own history lost an iteration each to a differential criterion attempted inside the loop. |
| 3 | C-050 | a03 iteration 2: 2 of 3 checks with both a `-basis` and a candidate receipt had differing exit codes. |
| 3 | C-048 | a03 iteration 1: 3 of 5 checks with both a `-basis` and a candidate receipt discriminated. |
| 3 | C-034 | d2c iteration 1: 3 of 9 checks discriminated. |
| 3 | C-052 | `docs/LIMITATIONS.md` limit 9: a merge of two individually-verified runs produced 3 contradictory statements about the project's own licence. |
| 3 | C-015 | `docs/LIMITATIONS.md` limit 8 / `CLAIMS.json`'s own corrected C-015 note: `test_union_gate.py` carries 3 of 3 run-authored commits. |
| 4 | structural: section cross-reference (§4) and this section's own list ordinal | Not a distinct ledger measurement. |
| 4 | C-015 | `docs/LIMITATIONS.md` limit 8 / `CLAIMS.json`'s own corrected C-015 note: `test_export_manifest.py` carries 4 of 4 run-authored commits. |
| 5 | C-019 | d1 iteration 1: 5 of 13 checks discriminated (receipt-derived). |
| 5 | C-048 | a03 iteration 1: 3 of 5 checks discriminated. |
| 5 | C-053 | `docs/LIMITATIONS.md` limit 9: the second O41 merge-defect instance left 5 coverage anchors pointing at content that had moved. |
| 6 | C-026 | d2 iteration 2: 6 of 9 checks discriminated, including the K7 regression from iteration 1 being fixed. |
| 7 | C-031 | d2b iteration 2: 7 of 13 checks discriminated, including K13 discriminating for the first time. |
| 8 | C-038 | d3 iteration 1: 8 of 11 checks discriminated, accepted on the first attempt. |
| 9 | C-026 | d2 iteration 2: 6 of 9 checks with both a `-basis` and a candidate receipt existed. |
| 9 | C-034 | d2c iteration 1: 3 of 9 checks existed for the discriminating ratio. |
| 9 | C-043 | d4 iteration 1: 9 of 10 checks discriminated, accepted on the first attempt. |
| 9 | C-015 | `docs/LIMITATIONS.md` limit 8 / `CLAIMS.json`'s own corrected C-015 note: `test_claims_anchors.py` carries 9 of 9 run-authored commits. |
| 10 | C-043 | d4 iteration 1: 9 of 10 checks with both a `-basis` and a candidate receipt existed. |
| 11 | C-021 | d1 iteration 2: 5 of 11 checks discriminated, accepted. |
| 11 | C-038 | d3 iteration 1: 8 of 11 checks existed for the discriminating ratio. |
| 13 | C-019 | d1 iteration 1: 5 of 13 checks existed for the discriminating ratio. |
| 13 | C-031 | d2b iteration 2: 7 of 13 checks existed for the discriminating ratio. |
| 18 | C-033 | Dogfood run d2c recorded 18 receipt files under `runs/d2c/receipts/`, all within its single iteration. |
| 21 | C-047 | Dogfood run a03 recorded 21 receipt files under `runs/a03/receipts/` across its two iterations. |
| 28 | C-061 | `docs/LIMITATIONS.md` limit 6: 28 of 52 candidate arenas in this project's history carried a leftover `pytest-of-<user>/` directory before the TMPDIR-redirection fix. |
| 32 | C-042 | Dogfood run d4 recorded 32 receipt files under `runs/d4/receipts/` across its two iterations. |
| 36 | C-023 | Dogfood run d2 recorded 36 receipt files under `runs/d2/receipts/` across its two iterations. |
| 37 | C-037 | Dogfood run d3 recorded 37 receipt files under `runs/d3/receipts/` across its two iterations. |
| 37 | C-073 | `docs/LIMITATIONS.md` limit 16: `hoh resume-quota`, run over this project's full history, reported 37 runs checked. |
| 48 | C-018 | Dogfood run d1 recorded 48 receipt files under `runs/d1/receipts/` across its two iterations. |
| 52 | C-028 | Dogfood run d2b recorded 52 receipt files under `runs/d2b/receipts/` across its two iterations. |
| 52 | C-061 | `docs/LIMITATIONS.md` limit 6: 28 of 52 candidate arenas carried the leftover directory. |

## Sections 12 and 13, added 2026-09-14

`POSITION_PAPER.md` gained two sections -- the three benchmark campaigns, and
what the harness demonstrably does -- and every number they carry is listed
here under the same rule as the table above: a claim id where the ledger
carries the figure, and a plain description of the source where it does not.
Three sources recur in the second column and are named rather than dressed up:
`docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` (a JSON evidence file
outside this ledger's declared prose coverage, whose figures
`tests/test_prereg_provenance.py` re-derives from git objects on every suite
run), `DOGFOOD_LEDGER.md` (this project's internal working ledger, which
`CLAIMS.json` does not cover, as `POSITION_PAPER.md` section 0 states), and the
three benchmark result documents, which the ledger does cover.

| number | claim id / source | note |
|---|---|---|
| 5 | `docs/benchmarks/v3/PREREGISTRATION.json` | tasks per campaign, frozen before campaign v3's first cell ran. |
| 3 | `docs/benchmarks/v3/PREREGISTRATION.json` | arms per campaign (A plain agent, B one `hoh run`, C the full control plane). |
| 3 | pre-registered repetition count, `docs/BENCHMARK_PROTOCOL.md` | repetitions per cell, declared unconditionally before the campaign ran. |
| 45 | C-259 | campaign v3: 45 of 45 runs complete (15 cells x 3 repetitions), COMPLETE, matched_budget_valid = YES. |
| 9 | `docs/BENCHMARK_PROTOCOL.md` | dispatches per **cell** in campaign v3, shared by a node and its repair nodes rather than granted per run. |
| 12.8 | `docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` | seconds between the commit binding the registration's exact bytes and the first v3 cell. The ledger does not cover this JSON evidence file; `tests/test_prereg_provenance.py` re-derives the figure from git objects on every suite run. |
| 50 | `docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` | seconds between that same commit and the earliest provider dispatch recorded in any v3 run tree. Same honest gap as the row above. |
| 15 | C-455 | campaign v3, arm A: 15 of 15 hidden suites PASS. |
| 0 | C-455 | campaign v3, arm A: 0 false accepts. |
| 1 | C-455 | campaign v3, arm A: 1 dispatch per cell. |
| 14 | C-456 | campaign v3, arm B: 14 of 15 hidden suites PASS. |
| 1 | C-456 | campaign v3, arm B: 1 false accept -- `slug_pair`, the metric the benchmark exists to produce. |
| 14 | C-457 | campaign v3, arm C: 14 of 15 hidden suites PASS. |
| 0 | C-457 | campaign v3, arm C: 0 false accepts -- and the paper states in the same passage why that is not a correctness result. |
| 15 | C-457 | campaign v3, arm C: 15 of 15 cells ended `BUDGET_EXHAUSTED`. |
| 18 | C-217 | campaign v2: an arm C cell spent 18 dispatches against a declared 9, because a node and its repair node each drew a full ceiling. |
| 5 | `docs/BENCHMARK_RESULTS_v2.md` | campaign v2: arm C closed 5 of 5 cells -- on the doubled budget the row above records. |
| 1 | `docs/BENCHMARK_RESULTS.md` | campaign v1: arm C produced a final state in 1 cell of 5. |
| 4 | `docs/BENCHMARK_RESULTS.md` | campaign v1: 4 of the 5 arm C cells stopped for the same reason, O127 -- a finished planner pane read as waiting for a human approval. |
| 6 | C-239 | campaign v1: one of the four acceptance-criteria counts (5, 6, 4, 3) still held in the run trees of the cells O127 stopped. |
| 5 | C-239 | campaign v1: the first of those four acceptance-criteria counts. |
| 4 | C-239 | campaign v1: the third of those four acceptance-criteria counts. |
| 3 | C-239 | campaign v1: the fourth of those four acceptance-criteria counts. |
| 18 | C-458 | post-O143 operational closure: 18 of 18 declared dispatches spent, the budget taken from what v2's arm C actually spent and never raised after a failure. |
| 9 | C-458 | post-O143 operational closure: 9 primary and 9 repair dispatches. |
| 1 | C-458 | post-O143 operational closure: 1 repair node. |
| 0 | C-458 | post-O143 operational closure: 0 human decisions. |
| 2 | structural: `docs/LIMITATIONS.md` limit heading number (limit 2, the missing baseline) | Not itself a measurement; the limit's own text is the recorded statement. |
| 17 | structural: `docs/LIMITATIONS.md` limit heading number (limit 17, arm A is single-shot by construction) | Not itself a measurement. |
| 5 | C-252 | 5 release-critical metrics carry a falsifier; without it the metric stays `NOT_RUN`. |
| 11 | C-459 | 11 controls in the dispatch-budget instrument. |
| 9 | C-459 | the first of the instrument's three ceilings. |
| 8 | C-459 | the second of the instrument's three ceilings -- and the one that forces the refusal inside an iteration. |
| 4 | C-459 | the third of the instrument's three ceilings. |
| 2 | C-254 | 2 falsifiers, each deleting the enforcement, both required to be detected. |
| 7 | `DOGFOOD_LEDGER.md` finding O147 -- an internal working document this ledger does not cover | an earlier version of the budget instrument passed 7 of 8 controls against a build with the enforcement removed. The paper names the finding id in the same sentence. |
| 8 | `DOGFOOD_LEDGER.md` finding O147 -- an internal working document this ledger does not cover | the denominator of that 7 of 8. |
| 5 | `DOGFOOD_LEDGER.md` finding O147 -- an internal working document this ledger does not cover | 5 of those 8 controls were reading the test fixture's own loop guard rather than the product's refusal. |
| 2 | `DOGFOOD_LEDGER.md` finding O147 -- an internal working document this ledger does not cover | 2 independent reviewers were tasked to refute the instrument before campaign v3 was frozen; both returned release: NO. |
| 9 | `DOGFOOD_LEDGER.md` finding O144 -- an internal working document this ledger does not cover | `max_dispatches=9`, the ceiling that bought eight provider calls before the off-by-one was found. |
| 8 | `DOGFOOD_LEDGER.md` finding O144 -- an internal working document this ledger does not cover | the eight provider calls that ceiling actually bought. |
| 167 | C-462 | failures in an export tree that carried no environment-gap marker where the published document promised none would. |
| 329 | C-462 | the denominator of that ratio. |
| 142 | `DOGFOOD_LEDGER.md` | **Superseded the day it was written, and kept to show why.** An earlier draft of section 13 froze the count of findings carrying their own heading in the internal ledger at 142, with a line-counting command beside it that did not reproduce it (finding O166). Recording that finding moved the count; recording the next one (O167) moved it again. Section 13 now prints the recomputation command and no answer, so the paper carries no such figure. |
| 30 | C-460 | explicit environment-gap skips a fresh clone of the public export reports, never summed with its passes. |
| 7 | C-461 | violations planted into throwaway copies by the capability-boundary instrument, each detected on its own: 7 of 7. |
| 5 | `tests/test_export_gap_guard.py` | negative controls holding the manifest-bound, fail-closed export-skip rule in place. |

## Numbers this table intentionally does not carry a ledger claim for

These appear in `POSITION_PAPER.md`'s prose but are not measurements this
paper makes: they are versions (such as the installed Herdr binary's own
version number), ISO dates, and identifiers this project names rather than
measures -- claim ids, internal finding ids, guard-finding ids, and run ids
such as `d2c2` and `a03`. None of these are excluded from the table above by
accident: `POSITION_PAPER.md`'s own acceptance check strips exactly this
category before extracting numbers, and this table follows the same rule
rather than padding itself with non-measurements.
