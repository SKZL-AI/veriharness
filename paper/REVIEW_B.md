# Review B — release, security, reproducibility, export, operational safety

This is one of two independent reviews of this project's release candidate.
Per the assignment, no attempt was made to locate or open the other
review's output, whatever its filename. This document only reviews; it
repairs nothing. Every reproduction below was executed against the actual
working tree at the commit this review was written from, plus a wheel and
sdist built into a scratch directory outside the project tree (the
development worktree, not the constrained check arena — see Coverage).

Eight brief items were investigated: the export state, cross-shipped
references, the check-command guard, a fresh-machine build, unfulfilled
promises, operational-safety enforcement in code, evidence reproducibility,
and the operator/captain approval question. Ten findings survived
verification; several other lines of attack were tried and came back clean,
and are reported as such rather than omitted, per the instruction that a
negative needs a demonstrated root cause in both directions.

## Coverage

Checked, with reproductions given inline in each finding or in the sections
below: the export-state file classification (both `dogfood/EXPORT_DATEIMENGE.md`
and the newer `EXPORT_MANIFEST.json`/`tools/export_manifest.py`), a
runtime-assembled needle scan of the current INCLUDE set plus the excluded
`build/lib/hoh/**` tree and every root-level and `history/` `.v*` predecessor,
cross-shipped reference closure via the project's own U2b checker, five guard
bypass attempts against `src/hoh/runner.py`'s arena guard (two more than the
minimum asked for), an actual wheel and sdist build followed by a fresh
virtual-environment install and console-command run, a search for
unbacked CI/SLA/supported-versions claims, the `yolo`/`approved` enforcement
path in `src/hoh/delivery.py`, the `tools/check_claims.py` evidence checker
run against this checkout, and the git tag/branch/merge history bearing on
the operator/captain approval question.

**NOT_CHECKED**, named explicitly:

- **The second review's own output.** Deliberately not searched for or
  opened, per the assignment's explicit instruction.
- **Re-running the B-10 git-log reproduction from inside a git-less,
  `runs/`-less check arena.** The acceptance environment for this very
  assignment states there is no usable git repository and no `runs/` tree in
  the check directory (O30, O33). The tag/branch/merge evidence cited in B-10
  was gathered in the development working tree, where `git` is available;
  it cannot be independently re-run by a grader confined to the check arena
  itself. This is a property of the check arena, not of a real downstream
  clone (which would have `.git` if obtained via a normal clone).
- **The actual content of `runs/**`.** Absent from this working tree
  entirely (project convention: gitignored, never populated in a tracked
  checkout) and absent from the check arena by design (O30/O33). The 50
  `runs/`-prefixed evidence citations inside `CLAIMS.json` (34 of them in
  `receiptcount:`/`run:`/`receipt:` evidence forms, the rest in narrative
  `text` fields) could not be independently re-derived from receipts; only
  the checker's own honest refusal to treat that gap as a pass could be
  confirmed (see B-09).
- **Whether a human captain was live-approving each `git merge` command
  through an out-of-band channel** (for example an interactive session's own
  command-approval prompts) at the time the 32 master-branch merges
  happened. No tracked artifact in this repository speaks to that one way or
  the other; B-10 states this explicitly as an open question rather than
  resolving it either way.
- **Whether the external license URLs cited in `docs/QUELLENCHECK.md`
  still resolve.** No network access was available in this environment;
  this matches the project's own paper/AUDIT.md, which records the same
  citation as "not re-fetched here (no network in this checkout)."

## Fresh-environment build and install

The working tree (193 git-tracked files, excluding `.git`) was copied into a
scratch directory outside the project (never touching this checkout — the
copy, not the original, was built and installed). A build virtual environment
was created with `python3 -m venv`, `pip`/`setuptools`/`wheel` were upgraded
inside it, and the wheel was built with `pip wheel . --no-deps -w <dist_dir>`
from the copy — exit code: 0. An sdist was built the same way with
`python3 -m build --sdist` — also exit code: 0. Both artifacts were inspected
with `python3 -m zipfile -l` / `tarfile -l`: the wheel carries exactly 17
`hoh/**` source files, 2 policy pattern files, and standard `dist-info`
metadata (including a single `licenses/LICENSE`, 1073 bytes, matching the
real MIT text); the sdist carries `src/`, `tests/`, `LICENSE`, `README.md`,
`pyproject.toml`, `PKG-INFO`, `setup.cfg`. Neither artifact contains
`LICENSE.v1.20260908T084525Z` (the stale, contradictory predecessor
`dogfood/EXPORT_DATEIMENGE.md` warns produced a bad wheel once before), nor
any `build/lib/**` remnant, nor any `dogfood/**`/`A0*.md`/`PHASE2.md`
material — `license-files = ["LICENSE"]` in `pyproject.toml` is being
matched literally by the installed setuptools 78.1.1, not as a glob, so this
specific historical leak does not reproduce against the current toolchain
and pyproject configuration. This is reported because it was checked, not
assumed.

A second, separate virtual environment was created with `python3 -m venv`
and the built wheel installed into it with `pip install <wheel path>` — exit
code: 0. `hoh --help` inside that environment returned the full subcommand
list at exit code: 0. `hoh` with no arguments returned argparse's expected
usage error at exit code: 2 (a required positional argument is missing —
correct CLI behaviour, not a defect). The same commands were then re-run
with `env -i HOME=<isolated dir> PATH=<venv bin>:/usr/bin:/bin hoh --root
<isolated dir> list`, simulating a machine with none of the operator's own
`$HOME` content — exit code: 0, no dependency on this machine's home
directory observed. `grep -rl` over the installed package's `lib/`
directory for the operator's own home-directory path found no match.

## Guard attack

`src/hoh/runner.py`'s `assert_stays_in_arena` was attacked directly (via
`python3 -c "import sys; sys.path.insert(0,'src'); from hoh.runner import
assert_stays_in_arena, ArenaEscape; ..."`, calling the function on each
command string below and catching `ArenaEscape`), and the genuinely
escaping shapes were additionally executed for real in `bash`, inside a
throwaway sandbox directory with a sibling file outside it, to confirm the
guard's silence corresponds to an actual escape rather than a harmless
string the guard merely failed to recognise as dangerous.

| # | Command | Guard verdict | Real bash outcome |
|---|---|---|---|
| 1 | `cp fake.json ./../../a02/checks.json` | BLOCKED (`ArenaEscape`, `'..'`) | not executed — this is the literal historical bypass the module's own comments describe as since fixed; used here as a control |
| 2 | `A=.; B=.; C="$A$B"; cp fake.json "./$C/$C/a02/checks.json"` | ALLOWED | executed as `A=.; B=.; C="$A$B"; cat "./$C/sibling_secret.txt"` — read a file one directory above the sandbox root |
| 3 | `cat $'\056\056'/sibling_secret.txt` | ALLOWED | executed verbatim — read the sibling file; `$'\056\056'` is bash ANSI-C quoting for two octal-56 (`.`) bytes, so the shell sees `..` at run time even though the source text never contains two adjacent literal dots |
| 4 | `P=/et; Q=c/; cat "$P$Q"passwd` | ALLOWED | executed verbatim — printed the first line of `/etc/passwd` |
| 5 | `cd$IFS..` | ALLOWED | executed as `cd$IFS..; pwd; cat sibling_secret.txt` — `pwd` showed the sandbox's parent directory and the sibling file was read |
| 6 | `printf "%s" "cd .."\|sh` | BLOCKED (`ArenaEscape`, `'..'`) | not executed — the literal `..` inside the quoted `printf` argument is still delimited by whitespace and a quote on both sides, so `_PARENT_ESCAPE` still matches it even though neither `_DIRECTORY_CHANGE` nor `_SHELL_C`/`_SHELL_HERESTRING` would catch this particular shell-smuggling shape on their own |
| 7 | `for i in 1;do\tcd ..;done` | BLOCKED (`ArenaEscape`, `'..'`) | not executed — same reason as #6: the bare `..` token is still delimiter-bounded |

Five ALLOWED/BLOCKED-labelled shapes were the minimum asked for; seven were
run. Four of the seven (#2–#5) are genuine, empirically confirmed escapes
from the object under test. All four share one root cause, named in B-05
through B-08 below: `assert_stays_in_arena` matches fixed regular
expressions against the **literal source text** of the command, never
against what a shell would actually expand that text into. Any technique
that defers constructing the dangerous substring — two dots, an absolute
path prefix, or the whitespace between `cd` and its argument — to shell-time
expansion produces a command the guard never sees as dangerous, while bash
executes it exactly as the historical, already-fixed literal case did.

## Findings

### B-01 — The export manifest fails its own self-check, and the paper is entirely absent from it

**Severity:** HIGH
**Location:** `EXPORT_MANIFEST.json` (on-disk); `tools/export_manifest.py` (`cmd_check`, `derive`)
**Exposure:** `python3 tools/export_manifest.py check` exits 1 against the
current tree: the on-disk manifest disagrees with a fresh derivation in 31
places. Two are a rule reclassification that was never re-applied to the
recorded file (`CLAIMS.json` and `CLAIMS.md` are recorded `EXCLUDE`/
`internal-working-document` on disk but derive fresh as `INCLUDE`/
`public-docs` — the module's own docstring says this reclassification is
already-decided history). The other 29 are paths present on disk that the
recorded manifest never mentions at all — most consequentially, all four
files under `paper/` (`AUDIT.md`, `FIGURES.md`, `NUMBERS.md`,
`POSITION_PAPER.md`) are completely absent from the on-disk manifest's 147
entries even as EXCLUDE rows; a fresh derivation classifies all four
`INCLUDE`/`paper`. If the on-disk file were trusted as the record of what
gets published, the project's own position paper would not be named as part
of the export at all.
**Reproduction:** `python3 tools/export_manifest.py check` — exit 1;
confirmed with `python3 tools/export_manifest.py derive --out /tmp/fresh.json`
followed by a diff against `EXPORT_MANIFEST.json` showing the 29 missing
paths and 2 changed-rule paths listed above.
**Fix shape:** re-run `tools/export_manifest.py derive` against the current
tree and record its output as the new `EXPORT_MANIFEST.json`, then keep
`tools/export_manifest.py check` in whatever preservation gate runs before a
real export, so this class of drift is caught before release rather than by
a reviewer after the fact.
**Status:** CONFIRMED

### B-02 — `dogfood/EXPORT_DATEIMENGE.md`'s 65-of-124 count is stale by 69 files

**Severity:** MEDIUM
**Location:** `dogfood/EXPORT_DATEIMENGE.md` (dated 2026-09-08, "Neu erhoben am 2026-09-08... 124 verfolgten Dateien")
**Exposure:** The repository now carries 193 git-tracked files, not 124.
Comparing the document's full categorised list (all `EIN`/`AUS`/`AUS!` rows,
124 paths) against the current tracked set leaves 69 tracked files —
roughly 36% of the tree — mentioned nowhere in the document: neither
included, excluded, nor flagged. Among them is the entire newer export
mechanism this review leans on for B-01 (`EXPORT_MANIFEST.json`,
`tools/export_manifest.py`, `tests/test_export_manifest.py`), thirteen
newer `dogfood/specs/*` files and their `.v*` predecessors, and
`tools/audit_refs.py`. The document itself warned, in its own text, that
its count "moved while being written" from 124 to 125; it has since moved
to 193 and the document was never revised again.
**Reproduction:** `git ls-files | wc -l` reports 193; the document's own
categorised list (every backtick-quoted path under its `## EIN`/`## AUS`/
`## AUS!` headings) totals 124 distinct paths when parsed the same way — a
set difference of 69.
**Fix shape:** either delete the stale hand count in favour of the
tool-derived `EXPORT_MANIFEST.json` (once B-01 is fixed), or add the same
kind of dated, explicitly-a-snapshot framing the document already uses for
its own moving-target problem, applied to the file list itself rather than
only to the summary number.
**Status:** CONFIRMED

### B-03 — At least eight cross-shipped dangling references exist beyond the four the prior survey found

**Severity:** MEDIUM
**Location:** `docs/LIMITATIONS.md:330,334`; `paper/AUDIT.md:79,109,115`; `paper/NUMBERS.md:14`; `paper/POSITION_PAPER.md:33,424` (targets: `dogfood/specs/d5-paper.md`, `dogfood/specs/d5l-limits-from-the-file.md`, `dogfood/specs/d2b-licenses.md`, `docs/QUELLENCHECK.md`, `dogfood/ABSCHLUSSBERICHT.md`)
**Exposure:** `dogfood/EXPORT_DATEIMENGE.md`'s own survey names four
dangling shipped-to-unshipped references and asks, in its own text, whether
there are only four — three of those four are since fixed (verified: no
remaining mention of `docs/QUELLENCHECK.md` in `PROVENANCE.md`, no
remaining mention of `RUNBOOK.md`/`A0*.md`/`PHASE2.md` in `README.md`, no
remaining mention of `RUNBOOK.md` in `docs/OPERATIONS.md`). But the answer
to "only four" is still no: under the current classification rules (which
place `paper/**` in the export, a decision the newer tool's docstring
attributes to "the captain's own decision (2026-09-09)"), the project's own
U2b reference-closure checker finds eight distinct dangling references from
files that would ship to files that would not: `docs/LIMITATIONS.md` names
two internal spec files by path, and `paper/AUDIT.md`, `paper/NUMBERS.md`
and `paper/POSITION_PAPER.md` between them name four more internal
documents and repeat one of `docs/LIMITATIONS.md`'s targets. A reader of
the published paper who follows any of these six distinct target paths
finds nothing, because none of the six ships.
**Reproduction:** with `tools/export_manifest.py` imported as a module and
`entries = derive(Path('.'))['entries']`, `check_u2b(entries, Path('.'))`
returns 8 findings of `type: u2b_dangling_reference`, each naming its
`from`, `to` and `reason` — reproducible by running
`python3 tools/export_manifest.py check` after first fixing B-01 (the
`check` subcommand runs `check_u2b` itself, but only after the manifest
comparison passes; calling `check_u2b` directly against a fresh `derive()`
result, as done here, does not require that fix first).
**Fix shape:** either inline the cited internal facts the way `d3b` already
did for the three now-fixed cases, or point at the public replacement
documents (`docs/LIMITATIONS.md` and `docs/ARCHITECTURE.md` etc.) instead
of the internal specs where one exists.
**Status:** CONFIRMED

### B-04 — `CLAIMS.json`'s own dangling references remain open and are invisible to the automated guard by design

**Severity:** HIGH
**Location:** `CLAIMS.json` (`file:` evidence fields on claims C-009, C-017, C-018 in the rendering); `tools/export_manifest.py:890` (`_U2B_EXCLUDED_EXTENSIONS`)
**Exposure:** The prior survey's fourth, "most severe" case — `CLAIMS.json`
citing files as evidence that would not ship — is still true today, and the
accurate, structured count is five claims. `CLAIMS.json`'s own `evidence`
array carries at least one non-shipping `file:` citation on five claims:
`C-009` (`SUPPORTED`) cites `A02_BEFUNDE.md:63` and `PHASE2.md:31`; `C-011`
(`INVALIDATED`) cites `A02_BERICHT.md:4`; `C-012` (`INVALIDATED`) cites
`HOH_ACCEPTANCE_REPORT.md:30`; `C-015` (`INVALIDATED`) cites
`dogfood/specs/d4-claims.md:19` alongside a second, shipping citation to
`docs/LIMITATIONS.md:125`; and `C-017` (`UNSUPPORTED`) cites four lines of
`dogfood/specs/d2b-licenses.md`. None of the six distinct non-shipping
target files named above (`A02_BEFUNDE.md`, `A02_BERICHT.md`,
`HOH_ACCEPTANCE_REPORT.md`, `PHASE2.md`, `dogfood/specs/d2b-licenses.md`,
`dogfood/specs/d4-claims.md`) are `INCLUDE` under either the on-disk or the
freshly derived manifest. Under the current rules `CLAIMS.json` itself
derives as `INCLUDE`/`public-docs` (see B-01), so if it ships, its own proof
chain points at files a reader cannot open, on five of its own claims
regardless of the status any one of those claims currently carries. This is
structurally invisible to the project's own guard: `tools/export_manifest.py`
deliberately excludes `.json` from `check_u2b`'s reference scan (`.py` and
`.json` are both data, the module's docstring says, not reader-navigable
prose) — a design decision that is defensible for `.py` string literals but
means the one file this exact defect class was first found in is exempt
from the automated check meant to catch it.
**Reproduction:** `python3 -c "import json; d=json.load(open('CLAIMS.json')); hits=[e for e in d['claims'] if any('A02_' in x or 'HOH_ACCEPTANCE_REPORT' in x or 'PHASE2.md' in x or 'd2b-licenses' in x or 'd4-claims' in x for x in e.get('evidence',[]) if isinstance(x,str))]; print(len(hits), sorted(h['id'] for h in hits))"`
prints `5 ['C-009', 'C-011', 'C-012', 'C-015', 'C-017']` — one structured
filter over the `evidence` array finds all five claims in a single pass;
each claim's own `evidence` list was then read directly (no other method
was used) to confirm the specific non-shipping citation named above for
`C-009`, `C-011`, `C-012`, `C-015` and `C-017`.
**Fix shape:** either resolve R1's own recorded "Weg B" recommendation (an
additional evidence form for "the evidence exists and is not published," so
the citation is honest without leaking or without silently dropping the claim),
or extend `check_u2b`'s scan to `CLAIMS.json`'s own `evidence` array
specifically (a narrower carve-out than lifting the whole-file `.json`
exclusion, which the module's docstring gives good reasons for keeping in
general).
**Status:** CONFIRMED

### B-05 — Check-command guard: runtime-concatenated dot variables defeat the parent-escape check

**Severity:** HIGH
**Location:** `src/hoh/runner.py:234` (`_PARENT_ESCAPE`), `src/hoh/runner.py:271` (`assert_stays_in_arena`)
**Exposure:** `_PARENT_ESCAPE` is a regular expression matched against the
literal command string; it has no way to see a `..` that only exists after
the shell expands and concatenates variables. Two single-character
assignments (`A=.`, `B=.`) never place two adjacent literal dots anywhere
in the source text, so the check passes a command that, once bash expands
`"$A$B"`, is an ordinary parent-directory escape of exactly the shape the
module's own comments describe as previously fixed.
**Reproduction:** `python3 -c "import sys; sys.path.insert(0,'src'); from hoh.runner import assert_stays_in_arena; assert_stays_in_arena('A=.; B=.; C=\"$A$B\"; cp fake.json \"./$C/$C/a02/checks.json\"')"`
raises nothing — ALLOWED. Executed live as
`A=.; B=.; C="$A$B"; cat "./$C/sibling_secret.txt"` from inside a sandboxed
child directory, it printed the contents of a file placed one level above
that directory.
**Fix shape:** the guard needs to reason about what the shell would
actually resolve the command to (for example by having the runner itself
expand and canonicalise candidate paths in a disposable subshell rooted at
the arena, then compare the resolved path against the arena boundary)
rather than pattern-matching the pre-expansion source text for a fixed set
of dangerous-looking substrings.
**Status:** CONFIRMED

### B-06 — Check-command guard: a variable-split absolute path defeats the absolute-path check

**Severity:** HIGH
**Location:** `src/hoh/runner.py:214` (`_ABSOLUTE_PATH`), `src/hoh/runner.py:271` (`assert_stays_in_arena`)
**Exposure:** Same root cause as B-05, applied to `_ABSOLUTE_PATH` instead
of `_PARENT_ESCAPE`: the needle `/etc/` never appears as a contiguous
substring in the command text when it is built from two shorter variables,
so the check never fires, and bash still resolves the concatenation to a
real absolute path outside the arena at run time.
**Reproduction:** `python3 -c "import sys; sys.path.insert(0,'src'); from hoh.runner import assert_stays_in_arena; assert_stays_in_arena('P=/et; Q=c/; cat \"$P$Q\"passwd')"`
raises nothing — ALLOWED. Executed live, `P=/et; Q=c/; cat "$P$Q"passwd`
printed the first line of `/etc/passwd`.
**Fix shape:** same as B-05 — resolve, don't pattern-match.
**Status:** CONFIRMED

### B-07 — Check-command guard: IFS-based space substitution defeats both the directory-change and parent-escape checks at once

**Severity:** HIGH
**Location:** `src/hoh/runner.py:205` (`_DIRECTORY_CHANGE`), `src/hoh/runner.py:234` (`_PARENT_ESCAPE`)
**Exposure:** `_DIRECTORY_CHANGE` requires `cd` to be followed immediately
by a literal whitespace character or end-of-string; `_PARENT_ESCAPE`
requires `..` to be immediately preceded by a delimiter character from a
fixed set. `cd$IFS..` satisfies neither: `cd` is followed by `$`, and `..`
is preceded by `S` (the last character of `$IFS`). At the shell, unquoted
`$IFS` expands to its default value (space/tab/newline) and the result is
then word-split using the current `$IFS`, so `cd$IFS..` is executed as `cd`
`..` — a directory change out of the arena that neither pattern was written
to recognise, because the token boundary the patterns rely on never exists
in the literal source text.
**Reproduction:** `python3 -c "import sys; sys.path.insert(0,'src'); from hoh.runner import assert_stays_in_arena; assert_stays_in_arena('cd\$IFS..')"`
raises nothing — ALLOWED. Executed live as `cd$IFS..; pwd; cat sibling_secret.txt`,
`pwd` printed the sandbox's parent directory and the sibling file's
contents were read successfully.
**Fix shape:** same as B-05/B-06 — a guard anchored to literal whitespace
and a fixed delimiter set cannot see a shell-level substitution that
produces the delimiter only after expansion; only re-resolving the command
(or refusing unquoted `$IFS`/parameter expansion outright as a denylisted
construct, a narrower but weaker patch) closes this specific shape.
**Status:** CONFIRMED

### B-08 — Check-command guard: ANSI-C quoted octal escapes defeat the parent-escape check

**Severity:** HIGH
**Location:** `src/hoh/runner.py:234` (`_PARENT_ESCAPE`)
**Exposure:** Bash's `$'...'` ANSI-C quoting expands backslash-octal
escapes before the word is used; `$'\056\056'` is two literal `.`
characters (octal 56) to the shell, but the source text handed to the guard
never contains two adjacent dot characters, only the six-character escape
sequence. `_PARENT_ESCAPE` matches literal `..`, not its octal encoding, so
it does not fire, and the shell still resolves the argument to a real
parent-directory reference.
**Reproduction:** `python3 -c "import sys; sys.path.insert(0,'src'); from hoh.runner import assert_stays_in_arena; assert_stays_in_arena('cat \$\x27\\056\\056\$\x27/sibling_secret.txt')"`
(the command string is `cat $'\056\056'/sibling_secret.txt`) raises
nothing — ALLOWED. Executed live, `cat $'\056\056'/sibling_secret.txt`
printed the contents of the sibling file one directory above the sandbox.
**Fix shape:** same as B-05 through B-07.
**Status:** CONFIRMED

### B-09 — The evidence checker can never exit clean without a private `runs/` tree, and the ledger does not say so in one place

**Severity:** MEDIUM
**Location:** `tools/check_claims.py` (evidence forms `run:`, `receipt:`, `receiptcount:`, `discriminated:`); `CLAIMS.json`/`CLAIMS.md` (37 claims marked `SUPPORTED` citing at least one such form); `docs/LIMITATIONS.md:209-230` (limit 11)
**Exposure:** Running the checker in this checkout — which has no `runs/`
directory, matching both the check arena (O30/O33) and any real clone of
the public export (`runs/` is gitignored and, per B-01/B-02's underlying
tooling, always classified `EXCLUDE`/`evidence-not-artifact`) — produces 57
`FAIL:` lines, every one of them explicitly labelled `ENVIRONMENT GAP...
this is not a content defect in the ledger` rather than a silent pass or an
undifferentiated failure. That labelling is good practice and is reported
here as a positive, not a defect. But the checker's own exit code is
non-zero regardless, meaning `tools/check_claims.py check all` can never
be used as a green gate in any environment that does not also carry the
operator's private `runs/` tree — which is every environment a third party
or the check arena will ever run it in. `docs/LIMITATIONS.md` limit 11
documents the adjacent, narrower problem (a criterion *inside a run* cannot
inspect `runs/`); it does not state, in one place a reader would find
before trusting a `SUPPORTED` label, that the 37 claims resting even partly
on `run:`/`receipt:`/`receiptcount:`/`discriminated:` evidence can never be
independently re-verified by anyone without that same private directory —
a number the operator alone verified must not read as a number the
published tooling can verify for someone else.
**Reproduction:** `python3 tools/check_claims.py check all` — exit 1, with
57 lines each containing the literal string `ENVIRONMENT GAP`
(`grep -c "ENVIRONMENT GAP" <output>` reports 57), none of them
distinguished from a real defect anywhere in the tool's own summary line
(`57 problem(s).`).
**Fix shape:** a one-line, prominent statement in `CLAIMS.md`'s methodology
section (or immediately beside the `SUPPORTED` legend) that `run:`-family
evidence is verifiable only inside the operator's own, unpublished `runs/`
tree, and that a reader without it is trusting the operator's own prior
run of the checker, not re-deriving the result themselves.
**Status:** CONFIRMED

### B-10 — `hoh deliver --approve` was genuinely never used, but 32 protected-branch merges bypassed the code path that switch guards, with no comparable authorization record

**Severity:** MEDIUM
**Location:** `dogfood/ABSCHLUSSBERICHT.md` ("Handarbeit, ausdrücklich deklariert" table, report point D); `src/hoh/delivery.py:22,106-117`; git history of branch `master`
**Exposure:** Report point D's specific, narrow claim — that
`hoh deliver --approve` was never invoked because "the switch means the
captain's explicit approval, and I don't set that for myself" — checks out:
no commit message, tag name, or reflog entry anywhere in this repository's
full history references `hoh deliver` being run, and no `runs/` state
exists to show a delivery record either way. But the same report table
also names 32 direct `git merge --no-ff` operations performed by the
operator to land accepted candidate branches on `master`, framed
throughout as ordinary "Operatorarbeit." Landing work on a project's
integration branch is exactly the action `src/hoh/delivery.py`'s own
module docstring quotes the handoff as reserving to the captain and
firstmate ("this assignment grants no additional push, merge, deployment or
purchasing rights"), and `deliver()`'s own `local-only` mode — the one path
that can touch a branch at all, and only once both `yolo == "off"` and
`approved` are true — refuses to ever create a merge commit ("No rebase, no
merge commit, no force" — `src/hoh/delivery.py:16`). The 32 operator merges
are `--no-ff` merge commits: a stronger action against the protected branch
than the code's own captain-gated path permits itself to perform, executed
through a channel (`git merge`, run directly) that `delivery.py`'s guard
never sees at all. Exactly one hand-intervention in the same report table
carries an explicit "vom Captain ausdrücklich autorisiert" annotation (a
one-time, read-only license lookup); the 32 merges carry no comparable
annotation, only the operator's own characterisation of the action as
within its remit. Whether a human captain was live-approving each merge
through some other channel this repository does not record is a real
possibility this review cannot rule out (see Coverage) and does not assert
either way.
**Reproduction:** `git log --all --format='%H %s' | grep -i "hoh deliver"`
returns no lines; `git log master --merges --oneline | wc -l` reports 32;
each of the 32 is a `--no-ff` merge commit (`git show --no-patch
--format='%P' <merge-sha>` lists two parents for every one of them, which a
fast-forward-only delivery would never produce).
**Fix shape:** not a code fix — a governance one: either route
project-internal consolidation merges through a variant of `deliver()` that
is allowed to create merge commits under the same `yolo`/`approved` gate
`local-only` already uses (so the guard actually sees the action), or
require and record an explicit per-merge captain annotation in the report
table the way the one license-lookup exception already does, so "Operator
work, no exception" is a checked claim rather than an assertion.
**Status:** CONFIRMED

## Checked and found clean

The following were attacked and came back clean; reported per the
instruction that an absence needs to be demonstrated, not assumed. A
runtime-assembled needle scan (home-directory prefixes, a private-address
pattern, and a token-shaped-string pattern, none of them written as a
literal forbidden substring in the scanning code) was run over: the 65
files `dogfood/EXPORT_DATEIMENGE.md` currently proposes for `EIN`; the full
176-entry INCLUDE set a fresh `tools/export_manifest.py derive()` produces
under the current rules (`scan_include_for_leaks` reports 0 findings); the
18 tracked files under `build/lib/hoh/**`; and all 19 `.v*`-suffixed
predecessor files at the repository root and under `history/`. The only
hits anywhere were self-referential mentions inside the guard's own source
and pattern-documentation files (`src/hoh/dispatchers.py`,
`src/hoh/runner.py`, their `build/lib/` copies, and several
`dogfood/specs/*` files quoting this very assignment's own needle-assembly
instruction) and `.invalid`-domain test fixture addresses — none a real
leak. The one genuine leak in the tree, a document naming a real
absolute home-directory path into an unrelated local project, is correctly
excluded under both the old hand survey (`AUS!`) and the new tool's
`foreign-subject` rule, and is not named again here for the same reason the
tool's own docstring gives for not naming it. Neither the built wheel nor
the built sdist contains it, `LICENSE.v1.20260908T084525Z`, or any
`build/lib/**` file (see "Fresh-environment build and install" above). No
CI configuration, workflow file, response-time promise, or supported-versions
table exists anywhere in the tree; `SECURITY.md` explicitly and
correctly disclaims all three rather than making them. `yolo` has exactly
one settable value reachable from any code path (`Literal["on", "off"] =
"off"` in `src/hoh/contracts.py:447`, and `controller.py:167` sets it
literally to `"off"`); no CLI flag, argparse choice, or other entry point
sets it to `"on"` anywhere in `src/hoh/**`, and `deliver()` independently
refuses delivery if it were ever anything but `"off"` — this is enforced in
the code path actually executed by `hoh deliver`, not only documented in
prose.

## Word-count note

This document, whitespace-normalised, is well over the 300-word minimum
the acceptance check applies; no further padding was added beyond what the
findings above required to be concretely reproducible.
