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

## Numbers this table intentionally does not carry a ledger claim for

These appear in `POSITION_PAPER.md`'s prose but are not measurements this
paper makes: they are versions (such as the installed Herdr binary's own
version number), ISO dates, and identifiers this project names rather than
measures -- claim ids, internal finding ids, guard-finding ids, and run ids
such as `d2c2` and `a03`. None of these are excluded from the table above by
accident: `POSITION_PAPER.md`'s own acceptance check strips exactly this
category before extracting numbers, and this table follows the same rule
rather than padding itself with non-measurements.
