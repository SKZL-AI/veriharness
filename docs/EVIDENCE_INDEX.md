# Evidence index: what the claims rest on, and what is not published here

Three claims in this repository rest on evidence trees that are **not**
in the public export, and this file says exactly what they contain so
that the gap is visible rather than quiet.

## Why they are not published

A receipt records where a check ran -- an arena, a worktree, a repository
root. That is what makes it evidence, and it is also an absolute path on
the machine that produced it. The export's own leak scan found such paths
in 36 files of the first two trees and refused them, which is the
behaviour anyone would want from it. A run state names the worktree it
ran in for the same reason and with the same consequence.

Redaction was considered and refused. Each receipt carries a
`stdout_digest` computed over the transcript *including* those paths; a
redacted transcript no longer matches its own digest. Evidence whose
integrity field is knowingly wrong is worse than evidence that is
honestly absent.

What follows is therefore everything that can be stated without a path:
the digest of each tree, its size, and the semantic fields. Anyone
holding the repository that contains these trees can re-derive every
number below with `python3 tools/evidence_index.py`, and a mismatch
means the tree has changed since this file was written.

## STRICT acceptance run

* tree digest: `838017219d7599a9` over 27 file(s), 36776 bytes
* receipts carrying an isolation record: 10
* receipts where isolation was **shown** from inside -- namespaces
  differing from the runner's, candidate read-only, network denied:
  **10 of 10**

| receipt | exit | runner_ok | effective | verified inside | namespaces differ | mount | network |
|---|---|---|---|---|---|---|---|
| `strictroman4-i1-a1-K1-basis` | 1 | True | strict | True | True | read-only | denied |
| `strictroman4-i1-a1-K1` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i1-a1-K2-basis` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i1-a1-K2` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i2-a1-K1` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i2-a1-K2` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i3-a1-K1` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i3-a1-K2` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i3-a1-K3-basis` | 0 | True | strict | True | True | read-only | denied |
| `strictroman4-i3-a1-K3` | 0 | True | strict | True | True | read-only | denied |

The namespace ids themselves are in the tree and are not reproduced
here: they are kernel inode numbers for this machine's namespaces, and
they are only meaningful in comparison with the runner's own, which is
the comparison the `namespaces differ` column already reports.

## Unattended fixpoint

* tree digest: `414bb7958722afa2` over 82 file(s), 149759 bytes
* nodes and their final lifecycle: `repair-1-1` = MERGED, `slugify` = MERGED
* repair nodes the run created by itself: `repair-1-1`
* closure generations: 2
* decisions recorded: 4, of which by a person: **0**
* repository mutations nothing in the state accounts for: **0**

| gate | outcome |
|---|---|
| `release-notes-present` | RED |
| `suite` | GREEN |
| `release-notes-present` | GREEN |
| `suite` | GREEN |

Two gate results for the same gate name at different generations is the
point, not a duplicate: the first closure found a red gate, a repair
node was created and merged, and the second closure found it green.

## Planner capability boundary

* tree digest: `186d452708fb6cfe` over 26 file(s), 67896 bytes
* verdict recorded by the measurement: **VERIFIED**

| metric | value |
|---|---|
| `planner_capability_violations` | 0 |
| `planner_repo_mutations` | 0 |
| `planner_git_mutations` | 0 |
| `planner_generated_implementation` | 0 |
| `planner_output_valid` | True |
| `developer_can_write` | True |
| `qa_answered` | True |
| `acceptance_functions` | True |

The last four are positive controls. Without them a boundary that
forbade everything would score perfectly on the first four, which is
the failure mode a confinement measurement is most likely to have.

* the planner's copies, each against the tree it was materialised from: `601e8468f428`, `f24e440e7df5`
* files the accepted candidate touched: `fib.py`, with 13 receipt(s)
* instrument control -- seven violations planted into throwaway copies, each detected on its own: **7 of 7**
* the controller's witness was armed for **2 of 2** planner dispatch(es), read from the dispatch records themselves rather than derived from what the controller does today

How much the witness covered is a property of each dispatch: the
protected set is built from what exists when it starts. A run whose
records do not carry that number is reported as not readable, never
as zero -- an earlier run accepted in an iteration where the set was
empty, and its violation count was a true statement about nothing.

The tree digests and the copy digests are the parts an outside reader
cannot check. The reasoning they support travels with the evidence
tree, in a README beside these files, and is therefore not exported
either -- naming its path here would be a reference nobody could
follow. What *is* exported is limitation 12b, which carries the same
argument in `docs/LIMITATIONS.md`.

## Internal working documents that published files cite

The documents below drove this project's own development and are
**not** in the public export: they are working material in German,
full of machine-local paths and of process detail that is provenance
rather than product. Published documents used to cite them by path,
which gave a reader twenty-one pointers that resolve to nothing in a
clone. The citations now name the document without a path and point
here, so that the source is still credited and nothing looks like a
broken link.

| internal document | what it is | where its substance is published |
|---|---|---|
| *Abschlussbericht* | the closing report of this project's own dogfood phase: what was built, what was measured, what was left open | `DOGFOOD_LEDGER.md` (the findings, entry by entry) and `docs/LIMITATIONS.md` (what is still true) |
| *Quellencheck* | a source-by-source check of the position paper's external citations | `paper/AUDIT.md`, which reports the same checks as verdicts |
| *d2b-licenses* | the internal specification for the licence and third-party review | `THIRD_PARTY_NOTICES.md` and `LICENSE` |
| *d4-claims* | the internal specification for the claims ledger | `CLAIMS.md` and the ledger's own methodology section in `CLAIMS.json` |
| *d5-paper* and *d5l-limits-from-the-file* | the internal specifications for the position paper and for deriving its limitations from measured files | `paper/POSITION_PAPER.md` and `docs/LIMITATIONS.md` |
| *Export-Dateimenge* | the working note that decided which paths the export carries | `EXPORT_MANIFEST.json` and its rules, which are the decision itself rather than a description of it |
| *D7-Review-Auftrag* | the brief given to the independent reviewers of the release candidate | `paper/REVIEW_A.md` and `paper/REVIEW_B.md`, which are their reports |
| the run evidence trees | receipts, run states and answers from the acceptance runs | the three sections above, and `docs/LIMITATIONS.md` limit 12e |

None of them is a source for a number. Every number a published
document states is carried by `CLAIMS.json`, which names the file and
line it was read from, and by the tools that recompute it.

## What this index does not give you

It does not let an outside reader verify the claims. It lets them see
the shape and the size of what they are being asked to take on trust,
and it lets anyone with the tree check that this file still describes
it. Those are different things and the difference is the point.
