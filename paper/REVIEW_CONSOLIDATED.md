# REVIEW_CONSOLIDATED — the union of REVIEW_A.md and REVIEW_B.md

Both input reviews are present: `paper/REVIEW_A.md` (three findings, `A-01`
through `A-03`) and `paper/REVIEW_B.md` (ten findings, `B-01` through `B-10`).
Nothing is missing, so this document consolidates both in full rather than
falling back to a single-review report.

This document consolidates. It does not repair anything named below, and it
does not add a fourteenth finding of its own. Every id from both reviews
appears exactly once, unchanged, at its own severity or higher, never lower.
Where the amendment of 2026-09-09 required this run to execute a verification
itself — the `B-01` reproduction, and the positive control for `B-05`
through `B-08` — that verification was run fresh in this working tree and its
actual output, not the number written into either source review, is what
governs the classification below. Several numbers below differ from the
numbers printed in `REVIEW_B.md` itself: this project's own tree grew between
the two reviews and this consolidation, and the honest-controls rule means
reporting what was actually observed rather than restating the older figure.

## Findings

| id | severity | classification | location | address | overlap |
|---|---|---|---|---|---|
| A-01 | HIGH | BLOCKER | paper/POSITION_PAPER.md §9 line 378 ("the four accepted dogfood runs"), read against the §3 table at lines 172-180 | Confirmed by rerunning the review's own reproduction in this tree: the §3 table's six non-"not accepted" rows still count to 6, while line 378 still says "four." Needs a repair run touching paper/POSITION_PAPER.md to either correct the number or name the narrower subset it actually means. Which run carries that repair is not decided here. | none |
| A-02 | HIGH | BLOCKER | paper/POSITION_PAPER.md §11 preamble (454-462), §11 item 6 (495-497), body §6 (240-297), read against docs/LIMITATIONS.md limit 6 (70-120) | Confirmed by rerunning the review's own reproduction in this tree: a case-insensitive search of paper/POSITION_PAPER.md for "show-toplevel", "rev-parse" and "ancestor" still returns zero matches, while docs/LIMITATIONS.md lines 98-120 still carry the undocumented ancestor-repository failure mode. Needs a repair run touching paper/POSITION_PAPER.md to add coverage of the git-ancestor-resolution failure alongside the TMPDIR half of limit 6 already covered. Which run carries that repair is not decided here. | none |
| A-03 | MEDIUM | TRACKED | paper/POSITION_PAPER.md §11 preamble (454-462) and list item 9 (511-516), read against docs/LIMITATIONS.md in full (372 lines, 16 limits) | Confirmed by rerunning the review's own reproduction: docs/LIMITATIONS.md still has no "dialog" text anywhere, while list item 9 still discusses a blocked-dialog topic sourced to C-014/docs/RECOVERY.md, not to any of the 16 limits. Real, reproducible, but a numbering/labelling mismatch rather than a missing-content defect (the actual limit-9 content is separately and fully covered in §1). Priority: P2 — tracked for the same future paper revision as A-01/A-02, not release-blocking on its own. | none |
| B-01 | HIGH | BLOCKER | EXPORT_MANIFEST.json (on-disk); tools/export_manifest.py (`derive`, `cmd_check`) | Reproduced fresh in this tree: `python3 tools/export_manifest.py check` exits 1. A structured diff (fresh `derive()` against the on-disk manifest) shows 35 paths present on disk and absent from the manifest and 2 rule reclassifications (`CLAIMS.json` and `CLAIMS.md` recorded EXCLUDE/internal-working-document, fresh derivation says INCLUDE/public-docs) — 37 disagreements total. This is higher than REVIEW_B.md's own count of 31 (29 missing + 2 changed): more files (including this run's own spec files and the two review files) have entered the tree since REVIEW_B.md was written; the class of defect and the two rule reclassifications are identical. Needs a repair run touching EXPORT_MANIFEST.json: re-run `tools/export_manifest.py derive` and record its output as the new manifest, then keep `check` in the pre-release preservation gate. Which run carries that repair is not decided here. | none |
| B-02 | MEDIUM | TRACKED | dogfood/EXPORT_DATEIMENGE.md (dated 2026-09-08, "124 verfolgten Dateien") | Reproduced fresh: a tracked-file count via `git ls-files` (piped to `wc -l`) now reports 199, not the 193 REVIEW_B.md recorded — the tree has grown further, the staleness is worse, not better. Priority: P2 — the automated EXPORT_MANIFEST.json (see B-01) is meant to supersede this hand count once B-01 is repaired; until then this document keeps drifting and should either be retired in favor of the tool or given the same explicitly-a-snapshot framing it already applies to its own summary number. | none |
| B-03 | MEDIUM | TRACKED | docs/LIMITATIONS.md:330,334; paper/AUDIT.md:79,109,115; paper/NUMBERS.md:14; paper/POSITION_PAPER.md:33,424 (plus, newly, paper/REVIEW_A.md and paper/REVIEW_B.md themselves) | Reproduced fresh via `check_u2b` on a fresh `derive()`: 17 dangling cross-shipped references now, not REVIEW_B.md's 8 — the two review files that entered the tree after REVIEW_B.md was written each name several of the same internal dogfood/spec targets REVIEW_B.md already flagged, so the count grew rather than shrank. Priority: P2 — needs a repair run touching docs/LIMITATIONS.md and paper/AUDIT.md, paper/NUMBERS.md, paper/POSITION_PAPER.md (inline the cited internal facts, or point at a public replacement document where one exists). Which run carries that repair is not decided here. | none |
| B-04 | HIGH | BLOCKER | CLAIMS.json (`evidence` fields on C-009, C-011, C-012, C-015, C-017); tools/export_manifest.py:890 (`_U2B_EXCLUDED_EXTENSIONS`) | Reproduced fresh with the review's own filter: exactly the same 5 claim ids and the same statuses came back (C-009 SUPPORTED, C-011 INVALIDATED, C-012 INVALIDATED, C-015 INVALIDATED, C-017 UNSUPPORTED), each still citing at least one non-shipping file. No drift on this one. Needs a repair run touching CLAIMS.json: either add a "the evidence exists and is not published" evidence form (R1's own "Weg B"), or extend check_u2b's reference scan to CLAIMS.json's own evidence array specifically. Which run carries that repair is not decided here. | none |
| B-05 | HIGH | DOCUMENTED_LIMITATION | src/hoh/runner.py:234 (`_PARENT_ESCAPE`), src/hoh/runner.py:271 (`assert_stays_in_arena`) | See the "DOCUMENTED_LIMITATION verification" section below for all four required clauses. No release-blocking repair; an optional future hardening (resolve/canonicalize the command in a disposable subshell rather than pattern-matching literal source text) would touch src/hoh/runner.py, out of scope for this document. | none |
| B-06 | HIGH | DOCUMENTED_LIMITATION | src/hoh/runner.py:214 (`_ABSOLUTE_PATH`), src/hoh/runner.py:271 (`assert_stays_in_arena`) | See the "DOCUMENTED_LIMITATION verification" section below for all four required clauses. Same fix shape as B-05, same file, out of scope for this document. | B-05 (same root cause and same guard function, different regex) |
| B-07 | HIGH | DOCUMENTED_LIMITATION | src/hoh/runner.py:205 (`_DIRECTORY_CHANGE`), src/hoh/runner.py:234 (`_PARENT_ESCAPE`) | See the "DOCUMENTED_LIMITATION verification" section below for all four required clauses. Same fix shape as B-05/B-06, same file, out of scope for this document. | B-05, B-06 (same root cause and same guard function, different regex pair) |
| B-08 | HIGH | DOCUMENTED_LIMITATION | src/hoh/runner.py:234 (`_PARENT_ESCAPE`) | See the "DOCUMENTED_LIMITATION verification" section below for all four required clauses. Same fix shape as B-05 through B-07, same file, out of scope for this document. | B-05, B-06, B-07 (same root cause and same guard function, same or adjacent regex) |
| B-09 | MEDIUM | TRACKED | tools/check_claims.py (evidence forms `run:`, `receipt:`, `receiptcount:`, `discriminated:`); CLAIMS.json/CLAIMS.md (37 claims); docs/LIMITATIONS.md:209-230 (limit 11) | Reproduced fresh: `python3 tools/check_claims.py check all` still exits 1 with exactly 57 `ENVIRONMENT GAP` lines, matching REVIEW_B.md's own count exactly — no drift. Priority: P1 — this understates nothing about content, but a checker that can never assert a real "green" outside the operator's own machine undermines independent verifiability of every one of the 37 affected SUPPORTED claims; needs a repair run touching CLAIMS.md's methodology section (a one-line, prominent statement next to the SUPPORTED legend). Which run carries that repair is not decided here. | none |
| B-10 | MEDIUM | TRACKED | dogfood/ABSCHLUSSBERICHT.md ("Handarbeit, ausdrücklich deklariert" table); src/hoh/delivery.py:22,106-117; git history of branch master | Reproduced fresh: searching `git log --all --format='%H %s'` for "hoh deliver" (case-insensitive) still returns no lines, and a merge-commit count from `git log master --merges --oneline` now reports 34, not REVIEW_B.md's 32 — two more direct merges landed since that review, the same pattern continuing rather than stopping. Whether an out-of-band captain approval covered these merges remains genuinely unresolved (REVIEW_B.md's own Coverage section states this as an open question, not a resolved one, and nothing in this tree settles it either way). Priority: P2 — needs a governance decision, not a code repair: either route these merges through a captain-gated `deliver()` variant, or record an explicit per-merge captain annotation in the report table the way the one license-lookup exception already does. | none |

## Verification run for this consolidation

All of the following were executed fresh, read-only, in this working tree
(which has git and no network, and no `runs/` tree — confirmed with
`ls runs/`, which reports no such directory) before writing the table above:

- `python3 tools/export_manifest.py check` — exit 1 (B-01).
- `derive()`/`check_u2b()` called as plain Python functions from
  `tools/export_manifest.py` (no shell, no git) to get exact counts for
  B-01 and B-03.
- `git ls-files | wc -l` — 199 (B-02).
- Case-insensitive greps and `sed` extracts against
  `paper/POSITION_PAPER.md` and `docs/LIMITATIONS.md` for A-01, A-02, A-03.
- A structured filter over `CLAIMS.json`'s own `evidence` arrays, identical
  in method to REVIEW_B.md's own B-04 reproduction, for B-04.
- `python3 tools/check_claims.py check all` — exit 1, 57 `ENVIRONMENT GAP`
  lines (B-09).
- `git log --all --format='%H %s' | grep -i "hoh deliver"` (no match) and
  `git log master --merges --oneline | wc -l` — 34 (B-10).
- The `B-05`..`B-08` positive control, described in full in the next
  section.

None of these reproductions needed the network or a `runs/` tree, so none of
the thirteen findings is classified `NOT_CHECKABLE_HERE`.

## DOCUMENTED_LIMITATION verification (B-05, B-06, B-07, B-08)

The amendment requires all four of the following, in the row or here, for
every `DOCUMENTED_LIMITATION` row. All four are supplied below for all four
findings, since they share one guard and one disclaimer.

**Clause 1 — the disclaiming text, quoted, naming the class.**
`docs/LIMITATIONS.md`, limit 4, "The check-command guard is a tripwire, not a
security boundary" (heading at line 44), quotes at line 47: "the guard around
acceptance-check commands is a tripwire against accidents, not a security
boundary against a deliberately hostile plan." The next sentence, at line
49, adds: "A pattern denylist on a string that a shell interprets again
afterwards is not watertight in principle." This names the exact class
`B-05` through `B-08` are instances of — a pattern-matching guard defeated by
shell-time expansion — not merely a general "this project has limitations"
disclaimer.

**Clause 2 — the claim id and its actual CLAIMS.json status.**
`C-016` carries exactly this text ("the guard around acceptance-check
commands is a tripwire against accidents, not a security boundary against a
deliberately hostile plan") and its status in `CLAIMS.json`, read directly
from the file for this document, is `UNSUPPORTED` — not `SUPPORTED`. Per the
amendment, a claim marked `SUPPORTED` would not qualify as a disclaimer for
this purpose; `UNSUPPORTED` does qualify, since the project books the
tripwire-not-boundary property as asserted-but-not-proven rather than as a
proven guarantee it is now trying to claim credit for.

**Clause 3 — a positive control run fresh for this document, the converse
of the reviewer's reproduction.**
Executed as pure function calls into `assert_stays_in_arena` from
`src/hoh/runner.py` (imported with `sys.path.insert(0, 'src')`; no shell, no
subprocess), against seven naive, non-obfuscated shapes — the same seven the
operator's own O79 measurement used, reproduced independently here rather
than taken on the operator's word, per the amendment's instruction that this
consolidation's own result governs:

```
BLOCKED  cd ..
BLOCKED  cd ../..
BLOCKED  cat ../sibling.txt
BLOCKED  cp x ./../a02/checks.json
BLOCKED  cat <system password file>
BLOCKED  python3 <a path inside a user home directory>/t.py
BLOCKED  ls ../
```

All seven raised `ArenaEscape`; none was `ALLOWED`. This matches the
operator's O79 result exactly. Since every naive, accident-shaped construct
is still caught, the guard does do the job the disclaimer credits it with —
stopping an ordinary mistake — and clause 3 holds. Had even one naive shape
come back `ALLOWED`, all four findings would be `BLOCKER` instead, per the
amendment; that did not happen.

**Clause 4 — reachability: accident, or only deliberate construction.**
The four confirmed bypasses each require a specific, non-obvious shell
technique with no benign reason to appear in an ordinary check command:
`B-05` splits `..` across two single-character variable assignments and a
later concatenation; `B-06` splits an absolute system path the same way;
`B-07` relies on `$IFS` word-splitting to manufacture the whitespace after
`cd` without ever writing a literal space; `B-08` uses bash's ANSI-C octal
quoting to produce two literal dots that never appear adjacent in the source
text. None of these arise from someone writing a normal, working check
command and stumbling into an escape by accident — each is a deliberate
construction chosen specifically to defeat a literal-text pattern match, the
same way the reviewer describes finding them ("attacked directly"). Per the
amendment, a shape reachable only by deliberate construction, not by
accident, is exactly what keeps the row from being a `BLOCKER` outright.

All four clauses hold for all four findings, so `B-05` through `B-08`
carry `DOCUMENTED_LIMITATION`, with their original `HIGH` severity kept,
not lowered.

## Overlap

Every pair of the thirteen findings was checked against the others for
whether they name the same file-and-line location or the same underlying
claim. Result: **zero overlapping pairs** across the two reviews.

What was specifically checked and ruled out, rather than assumed:

- `A-02`'s location (`docs/LIMITATIONS.md` limit 6, lines 70-120) and `A-03`'s
  location (`docs/LIMITATIONS.md`, list-position mismatch around limit 9) do
  not coincide with `B-03`'s or `B-09`'s `docs/LIMITATIONS.md` citations
  (lines 330/334, limit 15's specification-immutability discussion, and
  lines 209-230, limit 11) — four different limits, four different topics,
  in the same source file.
- `B-01` (manifest completeness: which paths are `INCLUDE`/`EXCLUDE` at all)
  and `B-03` (dangling references from already-included files) both use
  `tools/export_manifest.py`, but point at different specific defects in it
  and were checked and found not to name the same location or claim.
- `B-01` (the automated manifest) and `B-02` (the stale hand-count document)
  are about the same general subject — what the export actually contains —
  but name two different files as their location and were not counted as
  one pair.
- The four guard-bypass findings `B-05` through `B-08` share one root cause
  and one guard function and are cross-referenced against each other in
  their own `overlap` cells above; they are findings from a single review,
  not a cross-review overlap, so they do not count toward the cross-review
  overlap figure this section reports.

Zero is also what the last two-reviewer run on this project measured
(disjoint findings sets); this run's own independent count arrives at the
same result rather than assuming it.

## Blocker verdict

**Blocker-relevant subset.** The findings that are severity `HIGH` and whose
own source-review text contains a reproduction are: `A-01`, `A-02`, `B-01`,
`B-04`, `B-05`, `B-06`, `B-07`, `B-08` — 8 findings.

**Arithmetic.** Of those 8: 0 are `NOT_REPRODUCED`, 0 are
`NOT_CHECKABLE_HERE`, and 4 (`B-05`, `B-06`, `B-07`, `B-08`) are
`DOCUMENTED_LIMITATION`. `BLOCKER` count = 8 − 0 − 0 − 4 = **4**
(`A-01`, `A-02`, `B-01`, `B-04`).

**Verdict, stated plainly:** **4 reproducible HIGH findings remain BLOCKER**
and the release candidate is blocked on them: `A-01`, `A-02`, `B-01`,
`B-04`. The `DOCUMENTED_LIMITATION` carve-out count is **4** (`B-05`, `B-06`,
`B-07`, `B-08`), all under the check-command-guard tripwire limitation
documented in `docs/LIMITATIONS.md` limit 4 and claim `C-016`. None of the
five `MEDIUM` findings (`A-03`, `B-02`, `B-03`, `B-09`, `B-10`) count as
`BLOCKER` under this document's classification rule, since `BLOCKER` is
defined as reproducible `HIGH` only; all five are `TRACKED` with a stated
priority instead.
