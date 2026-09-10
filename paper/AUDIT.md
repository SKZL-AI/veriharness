# VeriHarness -- independent citation and numbers audit

This is the audit the D6 specification asks for, not a corrected paper: every
row below reports what was checked, how, and what came out; nothing in
`paper/POSITION_PAPER.md`, `paper/NUMBERS.md`, `paper/FIGURES.md` or
`CLAIMS.json` was edited to produce it. `tools/audit_refs.py` mechanically
checks this document's own coverage, verdict shape, and internal count
consistency -- it does not re-derive every verdict below by itself (that
would require touching `runs/` and `.git`, which it is built not to do); the
investigative work behind each row is recorded here so a reader can dispute
it.

## Methodology

**Whitespace normalisation.** `paper/POSITION_PAPER.md` is markdown prose
that wraps; a multi-word needle can break across a line wrap while reading as
one sentence on the page. Every search below (claim ids, `a02` mentions,
`source Q<n>` tokens, overclaim vocabulary) first collapses every run of
whitespace in the file to a single space, then searches the collapsed text --
per the specification's own 2026-09-09 warning about a `grep` that missed a
quotation broken after its fourth word.

**Claim id extraction, including ranges.** The paper cites some claim ids as
a range, e.g. `(C-052..C-054)` and `(C-068..C-072)`. Read as prose this means
every id in the range is being cited, not only the two endpoints a literal
`C-\d+` regex would catch. This audit expands both ranges (adding C-053,
C-069, C-070 and C-071 to the 43 ids a literal-token regex finds), because an
audit that only checked the two endpoints of a five-id range and called that
complete would itself be the kind of shallow pass this document exists to
rule out. `tools/audit_refs.py`'s own `coverage` check performs the same
expansion, so the two stay in lock-step.

**Claim-id resolution.** Each id is looked up in `CLAIMS.json`; the row is
`RESOLVED` once the id exists there, and the ledger's own `status` value
(`SUPPORTED` / `UNSUPPORTED` / `INVALIDATED`) is quoted in the row -- an
`INVALIDATED` claim still resolves as a *citation* (the id exists and its
status is stated correctly), which is a different question from whether the
underlying finding is corroborated. All 47 claim ids cited in the paper exist
in `CLAIMS.json`; none are `MISSING`.

**Claims against code.** For every claim id whose `CLAIMS.json` evidence
names a `file:` or `test:` location, this audit confirmed the named file
exists, and, where the ledger records an `anchor_digest` (a normalised
content hash of the cited line -- `docs/LIMITATIONS.md` §6/§8's own
content-anchoring mechanism, the one `d4c` built after the coverage-anchor
break this paper's own §1 instance two describes), that the digest still
resolves to exactly one line in the current file. It independently
reimplements that digest check (`sha256` of the line with internal
whitespace collapsed) rather than trusting `tools/check_claims.py`'s
`check evidence` subcommand, because that subcommand's own `file:` resolution
is a bare line-count range check (`resolve_evidence` in `tools/check_claims.py`,
around line 328-343: "does the line number fall inside the file", nothing
about content) -- it would not have caught a citation whose line drifted to
different content. The stronger, content-digest check does the real work here.

Result: **every** cited claim's `file:`/`test:` evidence resolves. Several
(all under `docs/LIMITATIONS.md`, which grew when limits 13-16 were merged in
after these claims were anchored) carry a *stale line number* in their
`evidence`/`where` field -- the recorded line no longer holds the cited
sentence -- but the content-digest still resolves uniquely elsewhere in the
same file, so the citation is intact; only the human-readable line pointer
drifted. This audit reports that drift per-row rather than silently
correcting it, and treats it as `RESOLVED` (the named thing exists and
matches), not `MISMATCH`, because the specification's own MISMATCH example is
a *renamed function* -- content genuinely gone -- not a cosmetic line-number
offset with the content still present and uniquely identified. Also ran
`python3 tools/check_claims.py check all` directly: 57 problems, every one an
`ENVIRONMENT GAP` on `receipt:`/`run:`/`receiptcount:`/`discriminated:`
evidence (the runs/-dependent forms); zero `file:`/`test:` evidence problems
reported, consistent with this audit's own finding.

**Numbers.** Recomputed, not compared to `paper/NUMBERS.md` -- this project
has already produced one false number by reading a `du -sh` block count as a
byte size (see `paper/POSITION_PAPER.md`'s own history), and two documents
agreeing on a wrong number is exactly the failure mode that survives a
compare-the-documents check. Of the 38 numbers `paper/NUMBERS.md`'s own table
catalogues, 3 are independently recomputable inside this checkout (the two
structural ordinals, and the "3 contradictory license statements" figure,
recomputed by counting the distinct rows of `dogfood/specs/d2b-licenses.md`'s
own table rather than by re-reading `docs/LIMITATIONS.md`'s restatement of
the same figure). The remaining 35 need either `runs/` (gitignored, absent
from this checkout -- confirmed: `runs/` does not exist here, O33) or `.git`
history (absent from an arena by construction, O31; also not invoked here by
design even though this particular checkout happens to have `.git`, because
the resolver must behave identically inside an arena that has neither). Those
35 are `NOT_CHECKED`, each row naming which of the two is missing and what
command or file would resolve it once available.

One incidental finding, not a row (out of `coverage`'s scope, which is
limited to what `paper/NUMBERS.md`'s table already catalogues): that table
does not carry a separate entry for the digit "3" in the paper's own §3
sentence "Its second iteration: 2 of 3 such checks differed, the candidate
was accepted again..." (a03 iteration 2, C-050) -- it catalogues the "2" but
not the "3" of that same ratio, even though both digits appear in
`POSITION_PAPER.md`'s prose. Flagged for the operator below as a
`paper/NUMBERS.md` maintenance item; it does not create a missing row in this
audit because criterion 1 scopes coverage to what `NUMBERS.md` already lists,
not to an independent re-derivation of `NUMBERS.md`'s own completeness.

**a02 mentions.** Re-parsed independently (whitespace-normalised,
`\ba02\b`): 5 mentions, matching the plan's stated count exactly (the plan
said not to trust that figure and to recount -- recounted, and it holds).
Every one sits in language that marks it invalidated or names it as the
counter-example the section is making (`RESOLVED`); none reads as unqualified
support for `a02`'s original claim. `a03` (`A03_NACHWEIS.md`) is consistently
named as the corrected replacement.

**External citations.** One `source Q<n>` token in the paper: `source Q2`,
citing `docs/QUELLENCHECK.md` row Q2. Marked `EXTERNAL` per the
specification: the URL and read-date are well-formed and quoted, but this is
a dated read of a third party's repository, not something this checkout can
re-fetch (no network access here) or otherwise reproduce.

**Overclaim vocabulary.** Searched the whitespace-normalised paper for the
exact vocabulary `dogfood/specs/d5-paper.md`'s own criterion 10 names:
unqualified superlative, "proves", "guarantees", "fully autonomous",
"production-ready", "solves", "verifies that", "ensures correctness",
"cannot fail" (word-boundary matches, so "solves" does not fire on
"re**solves**"). Three hits, all three sitting inside a negation or a
quotation the paper explicitly marks as violating its own rule -- reported
with the full sentence, not a bare count, per the specification's own
instruction that a count is not auditable.

**Commit references and run/receipt references.** Searched
`paper/POSITION_PAPER.md` for hex-looking commit shas (7-40 hex characters)
and for specific `runs/...` receipt-style path references. Found **none** --
the paper discusses `runs/` only as a directory/concept (11 mentions, all
about its absence or its role, none naming a specific run's receipt file),
and never quotes a commit sha in its own prose (several `CLAIMS.json` claim
*texts* do carry commit shas, e.g. C-049's `e348be3fab0b`, but those live in
the ledger, not in the paper's own words, so they are not a paper citation to
audit). `tools/audit_refs.py`'s `coverage` check does not gate on these two
categories for exactly this reason -- there is nothing of this shape to find
in this specific paper -- and this document says so plainly rather than
manufacturing rows for citations that do not exist.

## Claim id citations (47 rows)

| Ref | Verdict | Recomputation source / method | Detail |
|---|---|---|---|
| CLAIM C-011 | RESOLVED | CLAIMS.json lookup | ledger status: INVALIDATED; text: "**Ergebnis (urspruengliche Fassung):** A02 erfüllt — zwei aufeinanderfolgende akzeptierte Inkremente mit Warm-Start und evidenzbasiertem Rep...". evidence file:A02_BERICHT.md exists; content-digest resolves uniquely (currently at line 13, recorded at line 4 -- drift, content intact) |
| CLAIM C-012 | RESOLVED | CLAIMS.json lookup | ledger status: INVALIDATED; text: "\\| **H2** Echter HoH-Pfad \\| **fertig** — drei getrennte Rollenkontexte, Kandidatenbindung, Runner-Receipts, Warm-Start, Replanning. Zwei au...". evidence file:HOH_ACCEPTANCE_REPORT.md exists; content-digest resolves uniquely (currently at line 61, recorded at line 30 -- drift, content intact) |
| CLAIM C-013 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "There is no matched-budget comparison against a plain agent working the same specification without HoH's plan/develop/verify loop around it,...". evidence file:docs/LIMITATIONS.md exists; content-digest resolves uniquely at the recorded line (25 == 25, no drift) |
| CLAIM C-014 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "when the failure is specifically WaitingForApproval, it leaves the role tabs open instead, because closing them would remove the one place a...". evidence test:tests/test_controller.py::test_a_blocked_qa_dialog_is_reported_route_independently -- function confirmed present (grep, line 523) |
| CLAIM C-015 | RESOLVED | CLAIMS.json lookup | ledger status: INVALIDATED; text: "This project's own dogfood evidence never touched `src/` or `tests/`....". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 151, recorded 125 -- drift, content intact); second evidence file:dogfood/specs/d4-claims.md:19 exists and its line 19 reads the cited Scope text |
| CLAIM C-016 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "the guard around acceptance-check commands is a tripwire against accidents, not a security boundary against a deliberately hostile plan...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 47, recorded 44 -- drift, content intact); second evidence file:src/hoh/runner.py:13 exists and its line 13 reads the tripwire sentence quoted by the claim |
| CLAIM C-017 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "Per-run, receipt-bound evidence verification is sufficient to keep this project's public statements about itself consistent....". evidence file:dogfood/specs/d2b-licenses.md exists; content-digest for the primary line resolves (currently line 17, recorded 18 -- drift, content intact); the three table-row evidence lines 22-24 exist and match the LICENSE/pyproject.toml/CITATION.cff table quoted by the claim |
| CLAIM C-018 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run d1 (packaging) recorded 48 receipt files under `runs/d1/receipts/` across its two iterations....". receiptcount evidence only (runs/) -- not independently checked here, see Numbers section |
| CLAIM C-019 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d1 iteration 1 reached VERIFYING; of the checks for which both a `-basis` and a candidate receipt exist for that iteration, 5 of 13 discrimi...". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-020 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d1 iteration 1's candidate was rejected unverified, not failed: QA never returned a verdict because the QA role stalled on a blocked approva...". run evidence only (runs/) -- not independently checked here |
| CLAIM C-021 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d1 iteration 2 reached VERIFYING; of the checks for which both a `-basis` and a candidate receipt exist for that iteration, 5 of 11 discrimi...". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-022 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d1 iteration 2's candidate (committed as `5a2368c86e3b`) was accepted; d1 reached CHECKPOINTED after 2 iterations....". run evidence only (runs/) -- not independently checked here |
| CLAIM C-023 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run d2 (governance files) recorded 36 receipt files under `runs/d2/receipts/` across its two iterations....". receiptcount evidence only (runs/) -- see Numbers section |
| CLAIM C-026 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d2 iteration 2 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 6 of 9 discriminated, inclu...". discriminated/run/receipt evidence only (runs/) -- see Numbers section |
| CLAIM C-027 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d2 iteration 2's candidate (committed as `1e7cdb5a2e91`) was accepted; d2 reached CHECKPOINTED after 2 iterations....". run evidence only (runs/) -- not independently checked here |
| CLAIM C-028 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run d2b (resolving the license markers) recorded 52 receipt files under `runs/d2b/receipts/` across its two iterations....". receiptcount evidence only (runs/) -- see Numbers section |
| CLAIM C-031 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d2b iteration 2 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 7 of 13 discriminated, inc...". discriminated/run/receipt evidence only (runs/) -- see Numbers section |
| CLAIM C-032 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d2b iteration 2's candidate (committed as `979dbc731904`) was accepted; d2b reached CHECKPOINTED after 2 iterations....". run evidence only (runs/) -- not independently checked here |
| CLAIM C-033 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run d2c (the license-file selection) recorded 18 receipt files under `runs/d2c/receipts/`, all within its single iteration that reac...". receiptcount evidence only (runs/) -- see Numbers section |
| CLAIM C-034 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d2c iteration 1 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 3 of 9 discriminated....". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-036 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d2c never reached CHECKPOINTED: after iteration 1's rejection, its iteration budget (2/2, lower than the other dogfood runs' budget of 3) wa...". run evidence only (runs/) -- not independently checked here |
| CLAIM C-037 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run d3 (README, docs, the minimal example) recorded 37 receipt files under `runs/d3/receipts/` across its two iterations....". receiptcount evidence only (runs/) -- see Numbers section |
| CLAIM C-038 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d3 iteration 1 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 8 of 11 discriminated....". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-039 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d3 iteration 1's candidate (committed as `e3c896ac5dc9`) was accepted on the very first verification attempt. That is named here explicitly ...". run evidence only (runs/) -- not independently checked here |
| CLAIM C-042 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run d4 (this claims ledger's own first iteration pair) recorded 32 receipt files under `runs/d4/receipts/` across its two iterations...". receiptcount evidence only (runs/) -- see Numbers section |
| CLAIM C-043 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d4 iteration 1 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 9 of 10 discriminated....". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-044 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "d4 iteration 1's candidate (committed as `cc3cb9b857af`) was accepted on the first attempt....". run evidence only (runs/) -- not independently checked here |
| CLAIM C-047 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "Dogfood run a03 (the prior core evidence, two consecutive acceptances) recorded 21 receipt files under `runs/a03/receipts/` across its two i...". receiptcount evidence only (runs/) -- see Numbers section |
| CLAIM C-048 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "a03 iteration 1 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 3 of 5 discriminated....". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-049 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "a03 iteration 1's candidate (committed as `e348be3fab0b`) was accepted; a03 is the run this project's A03_NACHWEIS.md corrected-evidence rep...". run evidence only (runs/) -- not independently checked here |
| CLAIM C-050 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "a03 iteration 2 reached VERIFYING; of the checks with both a `-basis` and a candidate receipt for that iteration, 2 of 3 discriminated....". discriminated/run evidence only (runs/) -- see Numbers section |
| CLAIM C-051 | RESOLVED | CLAIMS.json lookup | ledger status: SUPPORTED; text: "a03 iteration 2's candidate (committed as `86ad5fff829e`) was accepted; a03 reached CHECKPOINTED with 2 consecutive accepted iterations, the...". run evidence only (runs/) -- not independently checked here |
| CLAIM C-052 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "-- twice. Once the merge produced 3 contradictory statements about this...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 182, recorded 142 -- drift, content intact) |
| CLAIM C-053 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "project's own licence. Once it left 5 coverage anchors pointing at content...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 183, recorded 143 -- drift, content intact); second evidence file:tests/test_claims_anchors.py:4 exists and its line 4 is inside the cited docstring paragraph |
| CLAIM C-054 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "that had moved, across 2 merges total. **Neither run was wrong** on its own...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 184, recorded 144 -- drift, content intact) |
| CLAIM C-055 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "Measured: 2 runs in this project's own history lost an iteration each to...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 202, recorded 162 -- drift, content intact) |
| CLAIM C-056 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "`git archive HEAD` run against this project yields 0 entries under `runs/`....". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 212, recorded 172 -- drift, content intact); second evidence file:.gitignore:5 exists and its line 5 literally reads `runs/` |
| CLAIM C-057 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "is not there to read. Measured: 6 of 15 acceptance criteria in one run...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 215, recorded 175 -- drift, content intact) |
| CLAIM C-059 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "acceptance-governing `discriminates` flag was 0, because that iteration was...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 245, recorded 205 -- drift, content intact) |
| CLAIM C-061 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "records the measurement: 28 of 52 candidate arenas in this project's...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves at the recorded line (88 == 88, no drift); second evidence file:src/hoh/runner.py:393 exists and its line 393 is inside the cited `_scratch_dir` docstring paragraph |
| CLAIM C-068 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "On 2026-09-08, around 21:15Z, two independent runs failed in the same shape...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 341, recorded 314 -- drift, content intact) |
| CLAIM C-069 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "within one window, across four role sessions in total: `d5`'s QA left no answer...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 342, recorded 315 -- drift, content intact) |
| CLAIM C-070 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "file behind for iteration 1, so the iteration was not accepted for lack of a...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 343, recorded 316 -- drift, content intact) |
| CLAIM C-071 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "verdict, and `d5`'s planner then left no answer file behind for iteration 2,...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 344, recorded 317 -- drift, content intact) |
| CLAIM C-072 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "blocking the run outright. `d4h` failed the same way, in the same window,...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 345, recorded 318 -- drift, content intact) |
| CLAIM C-073 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "`{"checked": 37, "results": []}`: 37 runs checked, 0 identified as...". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 351, recorded 324 -- drift, content intact) |
| CLAIM C-074 | RESOLVED | CLAIMS.json lookup | ledger status: UNSUPPORTED; text: "quota-blocked -- while two of the runs it checked, `d5` and `d4h`, were....". evidence file:docs/LIMITATIONS.md exists, content-digest resolves (currently line 352, recorded 325 -- drift, content intact) |

## Numbers (38 rows)

| Ref | Verdict | Recomputation source / method | Detail |
|---|---|---|---|
| NUMBER 0 (C-056) | NOT_CHECKED | N/A -- recomputation would require running `git archive HEAD` and counting entries under runs/ | O31: this resolver never invokes git, regardless of whether `.git` happens to be reachable from this checkout's cwd (an arena a criterion runs inside is not guaranteed repository history, and the resolver must behave identically whether or not this particular checkout has one); recomputing 'git archive HEAD yields 0 entries under runs/' requires a git command this tool does not run. Claimed: 0. |
| NUMBER 0 (C-059) | NOT_CHECKED | N/A -- would require runs/d1's own persisted run state (the controller's `discriminates` flag for iteration 1) | O33: runs/ is gitignored and absent from this checkout; `ls runs/` confirms no such directory exists here either. Claimed: 0. |
| NUMBER 0 (C-073) | NOT_CHECKED | N/A -- would require running `hoh resume-quota` against this project's full runs/ history | O33: runs/ is gitignored and absent; the command's own claimed output ('checked': 37, 'results': []) cannot be reproduced without the full runs/ tree. Claimed: 0 (of 37 checked). |
| NUMBER 1 (structural) | RESOLVED | paper/POSITION_PAPER.md | Structural: iteration ordinal ("iteration 1") and the paper's own §1 cross-reference, per paper/NUMBERS.md's own note ("not itself a distinct ledger measurement"). Recomputed trivially by confirming §1 exists and the label is used consistently; not a measurement. |
| NUMBER 2 (C-050) | NOT_CHECKED | N/A -- would require runs/a03's own persisted receipts for iteration 2 | O33: runs/ absent. Claimed: 2 (of 3) checks with differing exit codes, a03 iteration 2. |
| NUMBER 2 (C-054) | NOT_CHECKED | N/A -- would require git merge history (which of this project's commits are merge commits, and which two produced the license and coverage-anchor incidents) | O31: this resolver never invokes git, regardless of whether `.git` happens to be reachable from this checkout's cwd; docs/LIMITATIONS.md's own prose narrates the same '2 merges total' figure but re-reading that narration is not independent recomputation (this project's own numbers-must-be-recomputed rule exists precisely to rule that move out). Claimed: 2. |
| NUMBER 2 (C-055) | NOT_CHECKED | N/A -- would require identifying, by name, the 2 runs that lost an iteration to a differential criterion, from runs/ history | O33/O31: neither docs/LIMITATIONS.md nor DOGFOOD_LEDGER.md nor dogfood/ABSCHLUSSBERICHT.md names the 2 runs explicitly (checked: `grep` for the figure in both finds only the same restated number, not an enumerable list). Claimed: 2. |
| NUMBER 3 (C-048) | NOT_CHECKED | N/A -- would require runs/a03's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 3 (of 5) checks discriminated, a03 iteration 1. |
| NUMBER 3 (C-034) | NOT_CHECKED | N/A -- would require runs/d2c's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 3 (of 9) checks discriminated, d2c iteration 1. |
| NUMBER 3 (C-052) | RESOLVED | dogfood/specs/d2b-licenses.md | Recomputed by counting the distinct license statements in dogfood/specs/d2b-licenses.md's own table (lines 22-24): LICENSE says "all rights reserved" (run d2), pyproject.toml says Apache-2.0 (run d1), CITATION.cff says UNVERIFIED (run d2) -- 3 distinct statements. Recomputed value 3 == printed value 3. |
| NUMBER 3 (C-015) | NOT_CHECKED | N/A -- would require `git log --format='%s' -- tests/ src/ \| grep '^HoH '` (the exact command docs/LIMITATIONS.md:172 itself names) to count test_union_gate.py's run-authored commits | O31: this resolver never invokes git, regardless of whether `.git` happens to be reachable from this checkout's cwd; the command docs/LIMITATIONS.md itself prescribes for this figure is a git command this tool does not run. Claimed: 3 (of 3). |
| NUMBER 4 (structural) | RESOLVED | paper/POSITION_PAPER.md | Structural: §4 cross-reference and this section's own list ordinal, per paper/NUMBERS.md's own note ("not a distinct ledger measurement"). Recomputed trivially by confirming §4 exists; not a measurement. |
| NUMBER 4 (C-015) | NOT_CHECKED | N/A -- would require the same `git log` commit count as the row above, for test_export_manifest.py | O31: this resolver never invokes git, regardless of whether `.git` happens to be reachable from this checkout's cwd. Claimed: 4 (of 4). |
| NUMBER 5 (C-019) | NOT_CHECKED | N/A -- would require runs/d1's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 5 (of 13) checks discriminated, d1 iteration 1. |
| NUMBER 5 (C-048) | NOT_CHECKED | N/A -- would require runs/a03's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 5 total checks with both receipts, a03 iteration 1 (denominator of 3 of 5). |
| NUMBER 5 (C-053) | NOT_CHECKED | N/A -- would require the pre-merge and post-merge content of docs/OPERATIONS.md's line-anchored entries (git history of the d4b/d3b merge) | O31: this resolver never invokes git, regardless of whether `.git` happens to be reachable from this checkout's cwd; tests/test_claims_anchors.py's docstring restates '5' as a bare description, not an enumerable list of the five entries, so re-reading it is not independent recomputation. Claimed: 5. |
| NUMBER 6 (C-026) | NOT_CHECKED | N/A -- would require runs/d2's own persisted receipts for iteration 2 | O33: runs/ absent. Claimed: 6 (of 9) checks discriminated, d2 iteration 2. |
| NUMBER 7 (C-031) | NOT_CHECKED | N/A -- would require runs/d2b's own persisted receipts for iteration 2 | O33: runs/ absent. Claimed: 7 (of 13) checks discriminated, d2b iteration 2. |
| NUMBER 8 (C-038) | NOT_CHECKED | N/A -- would require runs/d3's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 8 (of 11) checks discriminated, d3 iteration 1. |
| NUMBER 9 (C-026) | NOT_CHECKED | N/A -- would require runs/d2's own persisted receipts for iteration 2 | O33: runs/ absent. Claimed: 9 total checks with both receipts, d2 iteration 2 (denominator of 6 of 9). |
| NUMBER 9 (C-034) | NOT_CHECKED | N/A -- would require runs/d2c's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 9 total checks with both receipts, d2c iteration 1 (denominator of 3 of 9). |
| NUMBER 9 (C-043) | NOT_CHECKED | N/A -- would require runs/d4's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 9 (of 10) checks discriminated, d4 iteration 1. |
| NUMBER 9 (C-015) | NOT_CHECKED | N/A -- would require the same `git log` commit count as above, for test_claims_anchors.py | O31: this resolver never invokes git, regardless of whether `.git` happens to be reachable from this checkout's cwd. Claimed: 9 (of 9). |
| NUMBER 10 (C-043) | NOT_CHECKED | N/A -- would require runs/d4's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 10 total checks with both receipts, d4 iteration 1 (denominator of 9 of 10). |
| NUMBER 11 (C-021) | NOT_CHECKED | N/A -- would require runs/d1's own persisted receipts for iteration 2 | O33: runs/ absent. Claimed: 11 total checks with both receipts, d1 iteration 2 (denominator of 5 of 11). |
| NUMBER 11 (C-038) | NOT_CHECKED | N/A -- would require runs/d3's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 11 total checks with both receipts, d3 iteration 1 (denominator of 8 of 11). |
| NUMBER 13 (C-019) | NOT_CHECKED | N/A -- would require runs/d1's own persisted receipts for iteration 1 | O33: runs/ absent. Claimed: 13 total checks with both receipts, d1 iteration 1 (denominator of 5 of 13). |
| NUMBER 13 (C-031) | NOT_CHECKED | N/A -- would require runs/d2b's own persisted receipts for iteration 2 | O33: runs/ absent. Claimed: 13 total checks with both receipts, d2b iteration 2 (denominator of 7 of 13). |
| NUMBER 18 (C-033) | NOT_CHECKED | N/A -- would require counting files under runs/d2c/receipts/ | O33: runs/ absent (confirmed: `runs/` does not exist in this checkout). Claimed: 18. |
| NUMBER 21 (C-047) | NOT_CHECKED | N/A -- would require counting files under runs/a03/receipts/ | O33: runs/ absent. Claimed: 21. |
| NUMBER 28 (C-061) | NOT_CHECKED | N/A -- would require enumerating this project's historical candidate arenas (ephemeral, materialized per run under runs/) for a leftover pytest-of-<user>/ directory | O33: runs/ absent, and the arenas themselves no longer exist even where runs/ is present (they are ephemeral per-check working directories, not preserved artifacts). Claimed: 28 (of 52). |
| NUMBER 32 (C-042) | NOT_CHECKED | N/A -- would require counting files under runs/d4/receipts/ | O33: runs/ absent. Claimed: 32. |
| NUMBER 36 (C-023) | NOT_CHECKED | N/A -- would require counting files under runs/d2/receipts/ | O33: runs/ absent. Claimed: 36. |
| NUMBER 37 (C-037) | NOT_CHECKED | N/A -- would require counting files under runs/d3/receipts/ | O33: runs/ absent. Claimed: 37. |
| NUMBER 37 (C-073) | NOT_CHECKED | N/A -- would require running `hoh resume-quota` against this project's full runs/ history | O33: runs/ absent. Claimed: 37 (runs checked). |
| NUMBER 48 (C-018) | NOT_CHECKED | N/A -- would require counting files under runs/d1/receipts/ | O33: runs/ absent. Claimed: 48. |
| NUMBER 52 (C-028) | NOT_CHECKED | N/A -- would require counting files under runs/d2b/receipts/ | O33: runs/ absent. Claimed: 52. |
| NUMBER 52 (C-061) | NOT_CHECKED | N/A -- would require the same historical arena enumeration as the row above (denominator of 28 of 52) | O33: runs/ absent, and arenas are ephemeral. Claimed: 52. |

## a02 mentions (5 rows)

| Ref | Verdict | Recomputation source / method | Detail |
|---|---|---|---|
| A02 mention 1 | RESOLVED | independent re-parse of paper/POSITION_PAPER.md (whitespace-normalised) | "...it catalogues every number-bearing sentence in README.md and docs/**, plus the two mandatory `a02` invalidation entries and a fixed set of unflattering measured findings..." (§0) -- a02 named as one of the ledger's mandatory *invalidation* entries. Sits in invalidated/counter-example context, not used as unqualified support. |
| A02 mention 2 | RESOLVED | independent re-parse of paper/POSITION_PAPER.md (whitespace-normalised) | "**`a02`, invalidated, appears here only as the counter-example it is.** Its original report claimed nine of ten criteria..." (§3) -- explicitly labelled invalidated and a counter-example. Sits in invalidated/counter-example context, not used as unqualified support. |
| A02 mention 3 | RESOLVED | independent re-parse of paper/POSITION_PAPER.md (whitespace-normalised) | "`a03` -- the corrected replacement for `a02`'s invalidated evidence -- recorded 21 receipt files..." (§3) -- explicitly "invalidated evidence", a03 named as its replacement. Sits in invalidated/counter-example context, not used as unqualified support. |
| A02 mention 4 | RESOLVED | independent re-parse of paper/POSITION_PAPER.md (whitespace-normalised) | "...the exact pair this project's 'two consecutive increments' claim now rests on, in place of `a02`'s invalidated version (C-050, C-051)." (§3) -- explicitly "invalidated version", superseded by a03. Sits in invalidated/counter-example context, not used as unqualified support. |
| A02 mention 5 | RESOLVED | independent re-parse of paper/POSITION_PAPER.md (whitespace-normalised) | "This is the direct countermeasure to the failure mode behind §3's invalidated `a02` evidence -- a stamped `discriminates` flag with no measurement behind it..." (§8) -- explicitly "invalidated ... evidence". Sits in invalidated/counter-example context, not used as unqualified support. |

## External citations (1 row)

| Ref | Verdict | Recomputation source / method | Detail |
|---|---|---|---|
| EXTERNAL source Q2 | EXTERNAL | docs/QUELLENCHECK.md row Q2 | URL https://github.com/Flesymeb/HarnessOfHarness/blob/main/README.md, checked blob `4142d6aa7b931fd4d8644d55ebc6f0b343ddadc1`, read-date 2026-09-06 (docs/QUELLENCHECK.md's own "Pruefdatum"). Rests on a dated read of an external repository, not a reproducible local source -- not re-fetched here (no network in this checkout). |

## Overclaim vocabulary (3 rows)

| Ref | Verdict | Recomputation source / method | Detail |
|---|---|---|---|
| OVERCLAIM production-ready | RESOLVED | whitespace-normalised regex scan of paper/POSITION_PAPER.md for the criterion-10 vocabulary | Full sentence (§12): "This paper does not claim VeriHarness is production-ready, does not claim it solves evidence composition across merged runs..." -- negated ("does not claim"). |
| OVERCLAIM solves | RESOLVED | whitespace-normalised regex scan of paper/POSITION_PAPER.md for the criterion-10 vocabulary | Same sentence (§12): "...does not claim it solves evidence composition across merged runs (§1's structural gap stays open even where its one concrete instance is closed)..." -- negated. |
| OVERCLAIM verifies that | RESOLVED | whitespace-normalised regex scan of paper/POSITION_PAPER.md for the criterion-10 vocabulary | Full sentence (§0): "A sentence such as 'VeriHarness verifies that a candidate is correct' violates this rule by asserting a property of the world..." -- the phrase sits inside a quotation that the surrounding sentence explicitly says would violate the paper's own naming rule (the d5-paper.md criterion-10 negative-control case, quoted and negated in the paper's own voice). |

## Commit references

None found in `paper/POSITION_PAPER.md` (searched for 7-40 character hex
sequences in the whitespace-normalised text). No rows in this category.

## Run and receipt references

None found as specific, resolvable-in-principle identifiers (e.g. a
`runs/<run>/receipts/<id>` path or a bare receipt filename) in
`paper/POSITION_PAPER.md`'s own prose. The paper discusses the `runs/`
directory as a concept repeatedly (see the Numbers section above for the
individual run/iteration figures, which are audited there as Numbers backed
by `receiptcount:`/`discriminated:`/`run:` evidence). No rows in this
category.

## For the operator

This audit could not check 35 of the 38 catalogued numbers inside
this arena/checkout. They fall into two groups:

**Needs `runs/` only (O33 -- gitignored, absent from the candidate snapshot), 28 rows:**
NUMBER 0 (C-059), NUMBER 0 (C-073), NUMBER 2 (C-050), NUMBER 3 (C-048), NUMBER 3 (C-034), NUMBER 5 (C-019), NUMBER 5 (C-048), NUMBER 6 (C-026), NUMBER 7 (C-031), NUMBER 8 (C-038), NUMBER 9 (C-026), NUMBER 9 (C-034), NUMBER 9 (C-043), NUMBER 10 (C-043), NUMBER 11 (C-021), NUMBER 11 (C-038), NUMBER 13 (C-019), NUMBER 13 (C-031), NUMBER 18 (C-033), NUMBER 21 (C-047), NUMBER 28 (C-061), NUMBER 32 (C-042), NUMBER 36 (C-023), NUMBER 37 (C-037), NUMBER 37 (C-073), NUMBER 48 (C-018), NUMBER 52 (C-028), NUMBER 52 (C-061).
Confirm by running `python3 tools/check_claims.py check all` against a checkout
that *does* have the full `runs/` tree (the same command reports
`ENVIRONMENT GAP`, not a content defect, here) -- and by the operator's own
`hoh resume-quota` run for the two rows sourced to that command's output.

**Needs `.git` only (O31 -- no `.git` inside an arena, and this tool does not
invoke git even where `.git` happens to be present), 6 rows:**
NUMBER 0 (C-056), NUMBER 2 (C-054), NUMBER 3 (C-015), NUMBER 4 (C-015), NUMBER 5 (C-053), NUMBER 9 (C-015).
Confirm with `git log --format='%s' -- tests/ src/ | grep '^HoH '` (the
commit-count rows; this is the exact command `docs/LIMITATIONS.md:172`
itself names) and by inspecting the two merge commits behind the license and
coverage-anchor incidents (the `C-054`/`C-055` rows) directly in `git log`.

**Needs both `runs/` and `.git`, 1 row:** NUMBER 2 (C-055).

(28 + 6 + 1 = 35, the full `NOT_CHECKED` count.)

**Commit references named in the paper:** none (see the Commit references
section above) -- nothing to confirm.

**`paper/NUMBERS.md` maintenance item (not a `paper/POSITION_PAPER.md`
defect, and not a row in this audit -- see Methodology):** `NUMBERS.md`'s own
table does not carry a row for the digit "3" in "2 of 3" (a03 iteration 2,
C-050's denominator), even though that digit appears in the paper's prose
alongside the "2" the table does catalogue.

## Summary

| Verdict | Count |
|---|---|
| RESOLVED | 58 |
| MISMATCH | 0 |
| MISSING | 0 |
| EXTERNAL | 1 |
| NOT_CHECKED | 35 |
| **Total rows** | **94** |

58 rows resolved cleanly (47 claim-id lookups, all of them
confirmed against current code/docs where their evidence names a file or
test; 3 recomputed numbers; 5 `a02` mentions, all correctly marked
invalidated; 3 overclaim-vocabulary hits, all hedged or quoted-and-negated).
35 rows are honestly `NOT_CHECKED` -- every one of the 35 remaining
numbers, each because recomputing it needs `runs/` and/or `.git`, neither of
which this checkout's own resolver touches. 1 row is `EXTERNAL` (the one
dated external citation). Zero `MISMATCH`, zero `MISSING`: nothing this audit
checked came back wrong, and no cited claim id was absent from the ledger.
That is the honest result, reported plainly rather than manufactured: this
audit looked at all 94 citations it could extract and is reporting exactly
what came back, including the 35 it could not check and why.
