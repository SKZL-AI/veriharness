# REVIEW_A — empirical-honesty review of paper/POSITION_PAPER.md

This is an independent review of `paper/POSITION_PAPER.md`'s empirical
statements against `CLAIMS.json`'s own evidence, `docs/LIMITATIONS.md`'s own
wording, and, where this checkout's own git history could settle a question
directly, that history. It reports findings; it repairs nothing, and no file
other than this one was created, edited, deleted, or renamed to produce it.

Method: every claim id the paper cites (`C-011` through `C-074`, the range
actually used) was looked up in `CLAIMS.json` and its `text`, `status`, and
`evidence` fields compared against the sentence the paper attaches the id to
— not against `paper/NUMBERS.md` or `paper/AUDIT.md`, which this review did
not use as a source of agreement for any number. `tools/check_claims.py check
all` was run once, read-only, to re-validate every claim's evidence form
against this checkout's actual files; it reported 57 problems, all of the
form `ENVIRONMENT GAP -- this check directory has no top-level runs/
directory`, none of them a content or anchor-digest defect. `git log`,
scoped to specific paths, was used to recompute two counts the paper states
about its own commit history. The word "discriminating"/"discriminates" and
every appearance of `a02` were checked across the whole document by exhaustive
grep, not by sampling.

## Findings

### A-01

- severity: HIGH
- location: `paper/POSITION_PAPER.md`, §9 ("The forbidden inference, and the
  boundary that keeps it honest"), the sentence beginning "executed itself:
  the four accepted dogfood runs" (around line 378), read against the §3
  table two sections above it (lines 172–180).
- claim: The paper credits "the loop's own work" to, among other things,
  "the four accepted dogfood runs and their deliverables and the §3 table
  above" — naming the §3 table as the source of the count "four."
- why: The §3 table it cites lists seven dogfood runs (`d1`, `d2`, `d2b`,
  `d2c`, `d3`, `d4`, `a03`) and marks six of them "accepted" (`d1`, `d2`,
  `d2b`, `d3`, `d4`, `a03`); `d2c`'s own row is explicitly labelled "not
  accepted -- the one dogfood run of the seven that was not (C-036)," which
  only holds arithmetically if six of the seven were accepted. "Four" is not
  a subset the surrounding text defines (it is not "the four accepted on the
  first attempt" -- that set is `d3`, `d4`, and `a03`'s first iteration,
  three runs, not four; it is not any other named partition either), and no
  reference set narrower than "the §3 table above" is stated for it. This is
  exactly an aggregate figure without a closed reference set matching what it
  is computed over: the number understates the paper's own tabulated
  evidence for its central claim about the loop's accepted output by exactly
  two runs.
- reproduction: REPRO: `sed -n '174,180p' paper/POSITION_PAPER.md | grep -v
  'not accepted' | grep -c accepted` returns `6` against the same table
  `grep -n "the four accepted dogfood runs" paper/POSITION_PAPER.md` (line
  378) calls "four."

### A-02

- severity: HIGH
- location: `paper/POSITION_PAPER.md` §11 preamble (lines 454–462) and §11
  item 6 (lines 495–497), together with body §6 (lines 240–297), read against
  `docs/LIMITATIONS.md`'s own limit 6 (lines 70–120, especially 98–120).
- claim: The §11 preamble states plainly that the Limitations section
  "corresponds to `docs/LIMITATIONS.md` and is complete against it... this
  section carries a statement for every one of them." Its restatement of
  limit 6 covers only the arena's nesting inside the project's own working
  tree and the now-fixed (2026-09-08) `TMPDIR` redirection that once caused
  the `is_git_repo` defect; that is the entire content of both §11 item 6 and
  body §6 on this topic.
- why: `docs/LIMITATIONS.md`'s own limit 6 names a second, independent
  failure mode that the paper never mentions anywhere: an arena has no `.git`
  of its own, but git's own upward directory search does not treat that as
  an error -- `git rev-parse --show-toplevel`, run from inside a materialized
  arena, silently climbs past the arena boundary and resolves to the
  ancestor checkout's path instead. The source document is explicit about
  the consequence: "`git status`, `git log`, and `git show`, run the same
  way, do not report on the frozen candidate files sitting in the arena at
  all; they silently report on the *ancestor* repository's live working tree
  and history," and "[a]nyone who assumes 'I am inside the candidate's
  arena, therefore a plain `git` command tells me about the candidate' would
  be reading the wrong repository without any error saying so." Unlike the
  `TMPDIR`/`is_git_repo` half of the same limit, this document gives no date
  and no fix for this second instance -- it reads as currently open. A
  completeness claim ("a statement for every one of them") that omits an
  open, silently-wrong-repository failure mode of the very isolation
  mechanism the paper spends all of §6 defending is an understatement of
  `docs/LIMITATIONS.md`'s own limit 6, not a restatement of it.
- reproduction: REPRO: `grep -in "show-toplevel\|rev-parse\|ancestor"
  paper/POSITION_PAPER.md` returns no matches anywhere in the paper, while
  `sed -n '98,120p' docs/LIMITATIONS.md` contains the failure mode and its
  own reproduction instruction (materialize an arena, run `git rev-parse
  --show-toplevel` inside it, observe the ancestor checkout's path).

### A-03

- severity: MEDIUM
- location: `paper/POSITION_PAPER.md` §11 preamble (lines 454–462) and list
  item 9 (lines 511–516), read against `docs/LIMITATIONS.md` in full (372
  lines, 16 numbered limits).
- claim: The preamble states the numbered list "carries a statement for
  every one of them -- fourteen of the sixteen as the items below, and the
  remaining two (limits 9 and 12) in the body of this paper itself, at
  length, where they are already treated (limit 9 in §1's
  per-run-versus-merged-state discussion, limit 12 in §0's and §3's
  `discriminates`-terminology definitions)." This sentence, placed directly
  before a list numbered 1 through 16, invites reading list position N as
  `docs/LIMITATIONS.md`'s limit N, with positions 9 and 12 the two
  deliberately skipped.
- why: List position 9, "No real observation of a blocked dialog keeping its
  pane open in production," sourced to `C-014` / `docs/RECOVERY.md:74`, is
  not content from `docs/LIMITATIONS.md` at all -- that file contains no
  "dialog" or "WaitingForApproval" limitation anywhere among its 16.
  `docs/LIMITATIONS.md`'s actual limit 9, "Local run correctness does not
  imply correctness after a merge," is correctly and fully treated in §1,
  exactly as promised -- but it never appears at list position 9 or anywhere
  in the numbered list, and something unrelated fills that slot instead.
  The list's positions are also not aligned with `docs/LIMITATIONS.md`'s
  numbers from limit 6 onward for an unrelated reason: limit 6's own content
  is split across list positions 6 and 7 (arena nesting, then the `TMPDIR`
  defect that is part of the *same* source limit), so every later position
  is offset by one relative to the source document's own numbering even
  where the content itself is accurate. The aggregate claim "fourteen of the
  sixteen" is defensible in total count, but the specific position-9
  correspondence the surrounding sentence sets up for a reader is false, and
  nothing in the text warns a reader that positions do not track numbers.
- reproduction: REPRO: `grep -in "dialog" docs/LIMITATIONS.md` returns no
  matches, while `sed -n '511,516p' paper/POSITION_PAPER.md` shows list item
  9 discussing exactly that (non-existent-in-the-source) topic.

## Also examined, no finding

Three of the specification's named traps produced no violation anywhere in
`paper/POSITION_PAPER.md`, checked by exhaustive grep across the whole
document rather than by sampling a few sections, and are reported here
rather than silently passed over:

- **"discriminating"/"discriminates."** Every one of the fourteen
  occurrences (`grep -c -i discriminat paper/POSITION_PAPER.md`) either
  appears in the §0 definitions themselves, uses the qualified compound
  "receipt-derived discriminating" / "acceptance-governing `discriminates`,"
  or is embedded in a sentence that names which of the two quantities is
  meant (e.g. "not discriminating in the acceptance-governing sense," "the
  controller itself flagged as not discriminating, in its own words"). No
  bare, unqualified use was found.
- **`a02` as support.** All five occurrences of `a02` (excluding `a03`,
  which is unrelated text matching the same substring) explicitly frame it as
  invalidated or as a counter-example (e.g. "`a02`, invalidated, appears
  here only as the counter-example it is," "in place of `a02`'s invalidated
  version"). No occurrence cites `a02` as support for a positive claim.
- **Overclaim vocabulary.** `grep -inE
  "proves|guarantee|fully autonomous|production-ready|solves|solved"
  paper/POSITION_PAPER.md` matches only the explicit negation in §12 ("does
  not claim VeriHarness is production-ready... does not claim it solves
  evidence composition"). No unqualified superlative, "proves," or
  "guarantees" was found in the document.
- **Self-repair vs. autonomy.** §8's own title states the finding the
  specification asks reviewers to hunt for ("...and that is not evidence of
  autonomy"), and every fix or improvement named anywhere in the paper is
  attributed to a specific role (planner, developer, QA) or to the operator
  by name, never to the system acting on itself; §9 states this rule
  explicitly and §8's closing paragraph states the limit of the "better
  criteria than the spec" observation in the same breath as the observation
  itself.

This review recomputed the §3 table (runs `d1` through `a03`, all seven
rows, both the receipt count and the receipt-derived discriminating
fraction) against the `text` field of each cited claim id in `CLAIMS.json`
directly; every cell matched. It also recomputed two commit counts the paper
states about its own history (§11 item 10's "9 of 9," "4 of 4," and "3 of 3"
commits for the three loop-authored test files, and the claim that no
`HoH `-prefixed commit ever touched `src/`) with `git log --format='%s' --
tests/test_claims_anchors.py|test_export_manifest.py|test_union_gate.py` and
`git log --format='%s' -- src/ | grep -c '^HoH '`; all matched exactly.

## Coverage

**Examined directly, in full:** `paper/POSITION_PAPER.md` (all 652 lines);
`CLAIMS.json` (all 74 claim entries, its `methodology`, `not_claims`, and
`claim_surfaces` fields); `docs/LIMITATIONS.md` (all 372 lines, all 16
numbered limits); `dogfood/specs/d2b-licenses.md` (to check the §1
"instance one" license-contradiction table); `dogfood/ABSCHLUSSBERICHT.md`
point D (to check the "eleven core defects," "five guard gaps," and
`K1`–`K11` figures cited in §9); this checkout's own `git log` output for
`tests/test_claims_anchors.py`, `tests/test_export_manifest.py`,
`tests/test_union_gate.py`, and `src/` (to recompute §11 item 10's commit
counts). `tools/check_claims.py check all` was run once, read-only.

**Could not be examined, and why:** this checkout has no `runs/` directory
at all (`ls runs/` fails; confirmed independently by `tools/check_claims.py
check all`'s own 57 `ENVIRONMENT GAP` results, all for `receiptcount:`,
`discriminated:`, `run:`, and `receipt:` evidence forms), so no
receipt-level number in the §3 table -- the 48/36/52/18/37/32/21 receipt
counts and every "N of M discriminated" fraction -- could be independently
re-derived from raw receipts here; this review verified them only against
`CLAIMS.json`'s own recorded `text` for each claim id, which is the level
`CLAIMS.json` itself is built to certify, and is silent about whether the
number was, upstream of the ledger, correctly read off the original receipt
files. `paper/NUMBERS.md`, `paper/AUDIT.md`, and `paper/FIGURES.md` were not
used as sources of agreement for any number in this review (the paper does
not cite any of the three), consistent with the brief's instruction not to
compare two documents to each other and call that a recomputation. This
review did not look for, and did not open, any file that might be a second
reviewer's output.

(Unlike the check directory the acceptance criteria describe, this working
tree does have a usable git repository and full commit history; where that
history could settle a specific numeric claim about this project's own
commits, it was used, and is cited above by the exact command run.)
