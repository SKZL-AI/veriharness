# Evidence index: what the claims rest on, and what is not published here

Two claims in this repository rest on evidence trees that are **not** in
the public export, and this file says exactly what they contain so that
the gap is visible rather than quiet.

## Why they are not published

A receipt records where a check ran -- an arena, a worktree, a repository
root. That is what makes it evidence, and it is also an absolute path on
the machine that produced it. The export's own leak scan found such paths
in 36 files of these two trees and refused them, which is the behaviour
anyone would want from it.

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

## What this index does not give you

It does not let an outside reader verify the claims. It lets them see
the shape and the size of what they are being asked to take on trust,
and it lets anyone with the tree check that this file still describes
it. Those are different things and the difference is the point.
