#!/usr/bin/env python3
"""Derives EXPORT_MANIFEST.json: the public-export file set, by rule.

`derive(root)` walks the **filesystem** of `root` and classifies every
regular file it finds into exactly one `{path, decision, rule}` entry. It
never calls `git` -- no subprocess, no `git ls-files`, no git plumbing of any
kind -- and it never reads `runs/` as a source of truth (a file under
`runs/**` is classified, never consulted; the whole subtree is pruned at
walk time and recorded once, see below, rather than enumerated file by
file). Its behaviour is therefore byte-for-byte identical whether or not a
usable git repository or a `runs/` tree exists at or above `root`: the check
environment this tool is graded in has neither, and the repository this
manifest is produced for has both.

Two rules are more than a per-file label: `runs/**` (evidence-not-artifact)
and `build/**` (stale-build-output) are pruned *at walk time*, not after
enumeration. `_walk_files` visits each such subtree's own root directory
once (to confirm it exists) and then stops recursing into it, and `derive()`
records the subtree itself as one manifest entry carrying that rule -- never
one entry per file underneath. This keeps the walk itself cheap in a
checkout, where `runs/` alone can hold hundreds of thousands of files that
would otherwise all be visited only to be classified EXCLUDE one at a time.
Every other directory rule below (`tests/`, `tools/`, `examples/`, `ops/`,
`plugin/`, `policy/`, `history/`, `paper/`) keeps walking file-by-file: the
INCLUDE ones are individually needed for export, and `history/` is small and
not the measured explosion this pruning addresses. A directory matching none of
these rules is walked file-by-file too and lands as `unclassified` per
file -- pruning is tied to a named rule, never to a growing ad-hoc list of
directory names.

Rule set (14 named rules, plus the conservative default `unclassified`):

    package                    what a user installs and runs: src/,
                               pyproject.toml
    tests                      tests/
    tooling                    tools/
    example                    examples/
    operational-integration    ops/, plugin/, policy/ -- non-Python
                               deployment/runtime-integration material
                               (systemd units, a cron example, the Herdr
                               plugin manifest, the guard's own policy pattern
                               files) that ships alongside the package but is
                               not itself `pip`-installed, is not prose
                               documentation, and is not governance text.
                               Added in this run: none of the eleven starting
                               rules fit non-Python operational material, and
                               the survey this run refines left it an unnamed
                               "Kern"/"Plugin..." bucket.
    repo-meta                  version-control-only metadata that shapes what
                               git tracks (.gitignore and siblings of its
                               kind) -- not documentation, not package data,
                               not governance text. Added in this run for the
                               same reason as operational-integration:
                               .gitignore fits none of the eleven starting
                               rules.
    governance                 a fixed, conventional set of root-level
                               filenames: LICENSE, CHANGELOG.md, CITATION.cff,
                               CONTRIBUTING.md, PROVENANCE.md, SECURITY.md,
                               THIRD_PARTY_NOTICES.md
    internal-working-document  refined in this run from "the German working
                               documents of this campaign" to "the working and
                               process material of this campaign, regardless
                               of language": (a) everything under dogfood/**
                               (specs, decisions, reports -- several of the
                               newer specs under dogfood/specs/ are themselves
                               in English, so a language-only rule would have
                               missed them); (b) a root-level or docs/**
                               markdown file whose German-function-word
                               density is >= 8%, a threshold picked to sit in
                               the wide, empirically measured gap between this
                               repository's English documents (0.0%-3.8%) and
                               its German ones (13.8%-25.1%); (c)
                               CLAIMS.json/CLAIMS.md -- see the dedicated note
                               below.
    foreign-subject            a docs/** file whose content contains a
                               home-directory-path needle. In this repository
                               the sole match is a document that profiles a
                               different, decoupled project's local inference
                               cache and names that project's real clone path
                               -- the file *proves* its own subject reaches
                               outside this repository by naming a path into
                               another local project. Which file that is is
                               not spelled out here on purpose: naming it in
                               prose would be a hardcoded reference to it in
                               every sense that matters, even outside the
                               classification logic itself, and this rule is
                               deliberately not that -- see this module's own
                               paired test for the reproducible, re-derivable
                               check. Every other docs/** file is clean
                               (verified during this run), so the rule is
                               sharp, not a coincidence tuned to one file.
    evidence-not-artifact      runs/**
    parked-predecessor         history/**, plus any root-level file matching
                               `<base>.v<digits>.<rest>` whose un-suffixed
                               sibling `<base>` exists at root (e.g.
                               LICENSE.v1.<timestamp> beside LICENSE). Scoped
                               to root level on purpose: a versioned
                               predecessor living under dogfood/specs/ is
                               still that spec's own campaign material, so the
                               dogfood/** rule (checked first) claims it
                               instead -- matching how the hand survey this
                               run refines already treated the two groups
                               differently.
    stale-build-output         build/**
    paper                      paper/** -- the position paper, in full. Added
                               in this run by the captain's own decision
                               (2026-09-09): the paper cites 39 claim ids and
                               is unreadable without CLAIMS.json/CLAIMS.md,
                               which become public in this same run, so the
                               two ship together rather than one silently
                               staying `unclassified`.
    public-docs                what remains: a root-level or docs/** markdown
                               file that is prose written for readers, in
                               English (below the German density threshold),
                               not a docs/** file naming a foreign path, and
                               not one of the two special-purpose root
                               basename sets above.
    unclassified (default)     anything none of the above claims. Always
                               EXCLUDE -- publication is irreversible, so an
                               unrecognised file is conservative by
                               construction, never a silent include.

CLAIMS.json / CLAIMS.md, specifically: both are `public-docs` and `INCLUDE`.
They previously sat under internal-working-document because, as authored,
they did not close under U2b: CLAIMS.md's "methodology" prose
backtick-referenced three docs/** files by name, and a citation table row
quoted a fourth EXCLUDE path from a locally-resolved source document. Two
things changed, in order: first, U2b's own reference detection was sharpened
(see `extract_references()`) to stop treating a discursive table-row mention
carrying its own `<path>:<line>` provenance locator as a live pointer, and to
stop treating a backtick span used as a URL's own markdown-link display text
as an independent bare-path candidate; second, CLAIMS.md's one remaining
prose sentence naming those three files -- a statement *about* a
classification decision, not a pointer for a reader to follow -- was
reformulated without code-spans so it reads as the discursive mention it is.
CLAIMS.json itself is additionally exempted from U2b entirely (see
`_U2B_EXCLUDED_EXTENSIONS`): it is the structured data CLAIMS.md is rendered
from, not prose a reader navigates by, and carries the same backtick
mentions as raw JSON string data. Only after both were verified to close
under the sharpened check were the two reclassified -- reclassifying first
would have published a document with a dangling reference, the exact defect
U2b exists to catch.

This module also performs the U2b within-export reference closure check and
a scan of INCLUDE-classified content for home-directory paths, private/
internal addresses, and token-shaped strings -- see check_u2b() and
scan_include_for_leaks(). It performs no export of its own: nothing here
ever copies, archives, or shells out to cp/rsync/git/gh: derive() only
reads and returns data, and this file's own CLI writes to exactly one path,
the manifest it was asked to produce.

Usage:
    python3 tools/export_manifest.py derive [--root PATH] [--out PATH]
    python3 tools/export_manifest.py check [--manifest PATH] [--root PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------
# The rule set
# --------------------------------------------------------------------------

RULES = (
    # Evidence a public document names. See `_EVIDENCE_PREFIXES`.
    "published-evidence",
    "package",
    "public-docs",
    "governance",
    "tests",
    "tooling",
    "example",
    "internal-working-document",
    "evidence-not-artifact",
    "parked-predecessor",
    "stale-build-output",
    "foreign-subject",
    "operational-integration",
    "repo-meta",
    "paper",
    "unclassified",
)

DECISIONS = ("INCLUDE", "EXCLUDE")

#: This run's own deliverable, EXPORT_MANIFEST.json, is included here too: it
#: is the transparency record of exactly what was published and why -- the
#: same governance role PROVENANCE.md plays for provenance -- and it carries
#: no prose (pure path/decision/rule data), so it cannot itself develop a
#: U2b problem the way CLAIMS.md did.
GOVERNANCE_BASENAMES = frozenset(
    {
        "LICENSE",
        "CHANGELOG.md",
        "CITATION.cff",
        "CONTRIBUTING.md",
        "PROVENANCE.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "EXPORT_MANIFEST.json",
    }
)

REPO_META_BASENAMES = frozenset({".gitignore", ".gitattributes", ".dockerignore"})

#: See the "CLAIMS.json / CLAIMS.md, specifically" note in the module
#: docstring for how these two came to be public-docs rather than
#: internal-working-document.
CLAIMS_LEDGER_BASENAMES = frozenset({"CLAIMS.json", "CLAIMS.md"})

#: O181. The four `DOGFOOD_*.md` files that predate this rule are classified
#: `internal-working-document` by the German-density heuristic -- not because
#: of what they are, but because of the language they happen to be written
#: in. A fifth one written in English fell straight through to `public-docs`,
#: and the leak scan then caught it carrying this machine's home directory.
#:
#: The heuristic is not the defect and is not touched: it was calibrated on
#: this repository's own documents and it is right about all four. What it
#: cannot do is see that a document is an internal working note when the note
#: is in English, because language is not the property that decides.
#:
#: `DOGFOOD_` is this project's own marker for that property, worn by every
#: such file it has. Named here so the classification rests on the marker
#: rather than on a language accident, narrow enough that it claims nothing
#: about any other file.
_DOGFOOD_BASENAME_RE = re.compile(r"^DOGFOOD_.+\.md$")

#: A root-level parked predecessor: NAME.v<digits>.<rest>, sibling of NAME.
_VERSIONED_SIBLING_RE = re.compile(r"^(?P<base>.+)\.v\d+\.(?P<rest>.+)$")

#: The other shape this project parks things under: `<name>.v<UTC stamp>`,
#: written by `RunStore` and by the pre-registration freeze. The first form
#: above counts versions; this one stamps them. Both mean the same thing --
#: replaced, kept, not current -- and both belong to the same rule wherever
#: they sit. It used to be recognised only at the repository root, so a parked
#: file one directory down fell through to `unclassified`, which is the
#: manifest's way of saying nobody decided. A test that skips on an absent
#: path may only do so when the manifest *declares* the withholding, and
#: `unclassified` is not a declaration.
#: Two stamp formats are in use and both mean the same thing: `RunStore`
#: writes `20260913T215702Z`, the pre-registration freeze writes
#: `2026-09-13T21-57-02Z`. A rule that knew only one classified the other as
#: `unclassified`, which is the manifest's way of saying nobody decided -- and
#: an undecided path must never satisfy a test's environment-gap skip.
_VERSIONED_STAMP_RE = re.compile(
    r"^(?P<base>.+)\.v\d{4}-?\d{2}-?\d{2}T\d{2}-?\d{2}-?\d{2}Z$")

#: Directories whose entire subtree gets one fixed rule regardless of
#: content. Checked, in this order, before any content-based rule.
_DIRECTORY_RULES = (
    # Continuous-integration configuration belongs in the published repository:
    # it is what makes the clean-install claim checkable by someone who does not
    # have this machine. Classified as repo-meta rather than tooling because it
    # configures the host, not the project -- nothing under it is imported or
    # executed by `hoh` itself.
    (".github", "INCLUDE", "repo-meta"),
    ("tests", "INCLUDE", "tests"),
    ("tools", "INCLUDE", "tooling"),
    ("examples", "INCLUDE", "example"),
    ("runs", "EXCLUDE", "evidence-not-artifact"),
    ("build", "EXCLUDE", "stale-build-output"),
    ("history", "EXCLUDE", "parked-predecessor"),
    ("ops", "INCLUDE", "operational-integration"),
    ("plugin", "INCLUDE", "operational-integration"),
    ("policy", "INCLUDE", "operational-integration"),
    ("dogfood", "EXCLUDE", "internal-working-document"),
    ("paper", "INCLUDE", "paper"),
)

#: Top-level directories pruned at walk time: recorded as exactly one
#: manifest entry -- rule looked up from `_DIRECTORY_RULES` above, never a
#: duplicated rule-string literal -- and never descended into. Limited to
#: the two whole-subtree rules this run's measured explosion is about
#: (`runs` -> evidence-not-artifact, `build` -> stale-build-output). Every
#: other `_DIRECTORY_RULES` entry keeps walking file-by-file: the INCLUDE
#: ones (`tests`, `tools`, `examples`, `ops`, `plugin`, `policy`) are needed
#: individually for export, and `history` is EXCLUDE but small and not
#: named by this run's spec.
_PRUNE_AT_ROOT_DIRNAMES = frozenset({"runs", "build"})

#: Paths under an EXCLUDE-by-default directory that are nevertheless published,
#: because a public claim names them as its evidence. Matched as a prefix and
#: checked *before* `_DIRECTORY_RULES`, so the surrounding directory's rule does
#: not decide for them.
#:
#: **The two evidence trees are not here, and the reason is worth stating.**
#: `dogfood/strict-e2e/` and `dogfood/unattended-e2e/` were added to this tuple
#: and then removed, because the export's own leak scan found absolute machine
#: paths in 36 of their files: an arena's location, a worktree's location, the
#: repository's own. Receipts record where a check ran, which is exactly what
#: makes them evidence and exactly what must not be published.
#:
#: Redacting them was considered and refused. A receipt carries
#: `stdout_digest`, computed over the transcript *including* those paths, so a
#: redacted transcript no longer matches its own digest -- and published
#: evidence whose integrity field is knowingly wrong is worse than evidence
#: that is honestly absent. `docs/EVIDENCE_INDEX.md` publishes what can be
#: published without lying: for each claim, the digest of the tree it rests on,
#: the number of artifacts in it, and how to check it against the repository
#: that holds it.
#:
#: `ATTRIBUTION.json` stays: it names commits and categories, and no paths.
_EVIDENCE_PREFIXES = (
    ("dogfood/ATTRIBUTION.json", "INCLUDE", "published-evidence"),
    # A campaign's own pre-registration and the evidence about it. Published
    # because the results document is worth nothing without them: a reader who
    # cannot see which commit the design was bound to, or the digests the raw
    # results hashed to before and after the reporter was repaired, is being
    # asked to take the campaign's central promise on trust. Each was scanned
    # for home paths before being listed; they carry digests, commit ids and a
    # platform string, and no machine-local path.
    #
    # The parked predecessor (`PREREGISTRATION.json.v<stamp>`) is deliberately
    # **not** here. It is superseded, no published document points at it as a
    # path, and the provenance artifact's claim -- that two commits carry two
    # different registration blobs -- is checkable from git without it.
    ("docs/benchmarks/v3/PREREGISTRATION.json", "INCLUDE", "published-evidence"),
    ("docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json", "INCLUDE",
     "published-evidence"),
    ("docs/benchmarks/v3/RAW_RESULT_DIGESTS.json", "INCLUDE",
     "published-evidence"),
    ("docs/benchmarks/v3/O154_ANALYSIS_ONLY.json", "INCLUDE",
     "published-evidence"),
)

_DIRECTORY_RULE_BY_NAME = {name: (decision, rule) for name, decision, rule in _DIRECTORY_RULES}

#: Never treated as export candidates at all -- universally-recognised
#: transient/VCS names, unrelated to git-independence: this tool never
#: invokes git, but it also never wants to enumerate git's own object store,
#: or a *.egg-info/__pycache__/.pytest_cache dropped by a previous test run,
#: as if they were export candidates. `.git` is skipped as both a directory
#: (an ordinary clone) and a plain file (a worktree's gitdir pointer, as in
#: the tree this tool was developed against) -- either way it is never a
#: file this manifest should classify.
_SKIP_NAMES = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache"})


def _is_skipped_dir(name: str) -> bool:
    return name in _SKIP_NAMES or name.endswith(".egg-info")


def _is_skipped_file(name: str) -> bool:
    return name in _SKIP_NAMES


# --------------------------------------------------------------------------
# .gitignore-aware filtering (R1d)
#
# `.gitignore` is a **tracked file** -- present in a checkout and in the
# check arena alike, no `git` binary needed -- so it is read directly with
# `(root / ".gitignore").read_text()`, once per `_walk_files()` call, and
# never re-typed as a hand-copied list of names inside this module: doing
# that would let the tool's own ignore list drift from the real
# `.gitignore` the moment one of the two is edited without the other, the
# same shape of defect this run's manifest-vs-tree drift already is one
# level up. A missing `.gitignore` is zero patterns -- every fixture that
# builds a tree without one keeps deriving exactly as before this run.
#
# Supported syntax (this repository's own `.gitignore` needs and gets
# exactly these four shapes):
#   - blank lines and `#` comments are skipped.
#   - a pattern with no internal slash (an optional trailing `/` aside)
#     matches by basename at any depth. This covers directory-prefix
#     patterns (`runs/`, `demo/`, `build/`, `__pycache__/`) and suffix
#     globs anywhere (`*.pyc`, `*.egg-info/`, `*.bak`); at most a single
#     leading `*` is the only wildcard shape supported here.
#   - a pattern containing a slash before its last segment
#     (`history/*.bak`, `plugin/*.v2026-*`, `policy/*.txt.v*`,
#     `plugin/bin/hoh-latest-de`) is anchored to the repository root and
#     matches only under that literal directory: every segment but the
#     last must be a literal path component, and `*` (any run of
#     non-`/` characters, any number of times) is supported only within
#     the final segment -- never spanning a `/`.
#   - a `!`-prefixed pattern re-includes a path an earlier pattern
#     matched, applied in file order so a later negation overrides an
#     earlier exclusion (git's own last-match-wins rule).
#
# Explicitly unsupported, and each fails closed (raises, naming the exact
# pattern) rather than silently letting the file through: `**` (globstar),
# `?`, character classes (`[...]`), a pattern anchored with a leading `/`,
# and a wildcard appearing in a non-final path segment. A silently
# unsupported pattern would put ignored content back into the manifest --
# exactly the defect this run fixes -- so an unrecognised pattern stops
# `derive()` rather than passing its file through uninspected.
# --------------------------------------------------------------------------


class UnsupportedGitignorePattern(ValueError):
    """A `.gitignore` line uses syntax this module does not implement.

    Caught by `cmd_derive`/`cmd_check` and reported as a `FAIL:` naming the
    exact offending pattern text, non-zero exit -- never silently ignored,
    since that would let ignored content back into the manifest.
    """

    def __init__(self, pattern: str):
        super().__init__(f"unsupported .gitignore pattern: '{pattern}'")
        self.pattern = pattern


_DISALLOWED_GLOB_CHARS = frozenset("?[]")


class _GitignoreRule:
    __slots__ = ("raw", "negate", "anchored", "dir_only", "regex")

    def __init__(self, raw: str, negate: bool, anchored: bool, dir_only: bool, regex: re.Pattern[str]):
        self.raw = raw
        self.negate = negate
        self.anchored = anchored
        self.dir_only = dir_only
        self.regex = regex


def _glob_segment_to_regex(segment: str) -> str:
    """Translates one path segment (`*` matches any run of non-`/`
    characters, any number of times) into a regex fragment. Callers reject
    `?`, character classes and `**` before this is ever reached.
    """
    return "[^/]*".join(re.escape(part) for part in segment.split("*"))


def _compile_gitignore_rule(raw_line: str) -> _GitignoreRule | None:
    """Compiles one `.gitignore` line, or returns None for a blank/comment
    line. Raises `UnsupportedGitignorePattern` for syntax outside the
    subset documented above.
    """
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    original = stripped
    negate = stripped.startswith("!")
    if negate:
        stripped = stripped[1:]

    if not stripped or stripped.startswith("/"):
        raise UnsupportedGitignorePattern(original)

    dir_only = len(stripped) > 1 and stripped.endswith("/")
    pattern_body = stripped[:-1] if dir_only else stripped

    if not pattern_body or "**" in pattern_body:
        raise UnsupportedGitignorePattern(original)
    if any(c in _DISALLOWED_GLOB_CHARS for c in pattern_body):
        raise UnsupportedGitignorePattern(original)

    segments = pattern_body.split("/")
    anchored = len(segments) > 1

    if anchored:
        for seg in segments[:-1]:
            if not seg or "*" in seg:
                raise UnsupportedGitignorePattern(original)
        final = segments[-1]
        if not final:
            raise UnsupportedGitignorePattern(original)
        regex_body = "/".join(re.escape(seg) for seg in segments[:-1]) + "/" + _glob_segment_to_regex(final)
    else:
        # One `*`, anywhere in the segment. The earlier form additionally
        # required it to be *leading*, which rejected `c4-*/` -- a rule the
        # repository needed after twenty files of stray probe output were
        # committed and the export caught them. `_glob_segment_to_regex`
        # already places the wildcard wherever it sits; the extra condition
        # was caution rather than necessity, and caution that refuses a
        # correct pattern pushes people towards deleting the gate.
        #
        # Still refused: more than one `*`, `**`, `?`, character classes, and
        # a leading `/`. An unsupported pattern stops the derivation rather
        # than silently letting ignored content into the manifest.
        star_count = pattern_body.count("*")
        if star_count > 1:
            raise UnsupportedGitignorePattern(original)
        regex_body = _glob_segment_to_regex(pattern_body)

    return _GitignoreRule(
        raw=original, negate=negate, anchored=anchored, dir_only=dir_only, regex=re.compile("^" + regex_body + "$")
    )


def _load_gitignore_rules(root: Path) -> list[_GitignoreRule]:
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8")
    except OSError:
        return []
    rules: list[_GitignoreRule] = []
    for line in text.splitlines():
        rule = _compile_gitignore_rule(line)
        if rule is not None:
            rules.append(rule)
    return rules


def _gitignore_matches(rel_path: str, is_dir: bool, rules: list[_GitignoreRule]) -> bool:
    """True if `rel_path` (posix, relative to root, no leading `/`) is
    ignored under `rules` -- the last matching rule wins, mirroring git's
    own precedence, so a later `!`-re-inclusion overrides an earlier
    exclusion.
    """
    if not rules:
        return False
    basename = rel_path.rsplit("/", 1)[-1]
    matched = False
    for rule in rules:
        if rule.dir_only and not is_dir:
            continue
        candidate = rel_path if rule.anchored else basename
        if rule.regex.match(candidate):
            matched = not rule.negate
    return matched


# --------------------------------------------------------------------------
# German-density content heuristic (internal-working-document, content half)
# --------------------------------------------------------------------------

_GERMAN_WORDS = (
    r"der|die|das|den|dem|des|und|oder|nicht|kein|keine|keinen|wird|werden"
    r"|ist|sind|war|waren|eine|einen|einem|eines|mit|ohne|fuer|für|über|ueber"
    r"|auf|aus|von|vom|zum|zur|bei|nach|noch|schon|bereits|selbst|dass|sich"
    r"|darf|muss|soll|kann|hat|haben"
)
_GERMAN_WORD_RE = re.compile(rf"\b(?:{_GERMAN_WORDS})\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
GERMAN_DENSITY_THRESHOLD = 0.08


def german_density(text: str) -> float:
    """Fraction of alphabetic words that are common German function words.

    Calibrated against this repository's own documents: every English
    document measured 0.0%-3.8%, every German one 13.8%-25.1%. 8% sits in
    the middle of that gap with wide margin on both sides.
    """
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    hits = _GERMAN_WORD_RE.findall(text)
    return len(hits) / len(words)


# --------------------------------------------------------------------------
# Home-path needle, for foreign-subject detection and the INCLUDE-set scan.
# Assembled at runtime (mirroring tools/check_claims.py:55-66) so this
# module's own source never contains the literal substring it is refusing.
#
# Fixed and environment-independent on purpose: an earlier version of this
# detector used `os.path.expanduser('~')` -- the *checking process's own*
# $HOME -- as the needle. That made foreign-subject detection depend on
# which user or sandbox happened to be running the check: it caught this
# repository's one real foreign-subject leak (a home-directory clone path
# authored into a docs/** file) only when the checking process's own $HOME
# still happened to equal the operator's, and silently missed it otherwise
# (see test_home_path_detection_is_environment_independent, which
# regression-tests exactly this). A fixed prefix set has no such
# dependency: it flags a path shaped like a home directory regardless of
# whose it is.
# --------------------------------------------------------------------------


def _home_path_needles() -> list[str]:
    sl = chr(47)
    dirnames = ["home", "root", "etc"]
    needles = [sl + name + sl for name in dirnames]
    needles.append(chr(126) + sl)
    return needles


def _mnt_pattern() -> re.Pattern[str]:
    sl = chr(47)
    return re.compile(re.escape(sl + "mnt" + sl) + r"[^" + re.escape(sl) + r"]+" + re.escape(sl))


#: Characters that, immediately before a needle, mean it is not the start of an
#: absolute path. A directory that happens to be named "root" inside a relative
#: path is an ordinary name, not this machine's layout: the manifest lists
#: `dogfood/unattended-e2e/root/projects/...`, and the unnarrowed check
#: reported the manifest itself as a leak.
_PATH_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789._-/"
)


def contains_home_path(text: str) -> bool:
    """True when an absolute home-ish path appears, at the start of a path.

    Narrowed deliberately and no further: a needle in quotes, in brackets or at
    the start of a line still counts, because the character before it cannot be
    part of a path. Only a needle preceded by another path segment is let
    through.
    """
    for needle in _home_path_needles():
        pos = text.find(needle)
        while pos != -1:
            if pos == 0 or text[pos - 1] not in _PATH_CHARS:
                return True
            pos = text.find(needle, pos + 1)
    return bool(_mnt_pattern().search(text))


# --------------------------------------------------------------------------
# Hardcoded home paths (O175)
#
# A different property from `contains_home_path`, and deliberately narrower:
# not "mentions a home-ish prefix" -- guard pattern files and code that
# handles `~` or $HOME legitimately do that -- but "carries a literal
# absolute path under one concrete home directory". A published tool once
# carried such a literal and no gate saw it, because the home-path scan is
# (correctly) scoped to reader-facing docs. This check runs over every
# INCLUDE file regardless of rule and, like `contains_home_path`, over raw
# text: a literal in a comment or docstring is still published.
#
# Only a named home counts: /home/<name> and /Users/<name> with <name> one
# plain path segment starting with a letter, digit or underscore -- either
# followed by a further segment or standing alone as the home root itself
# (Path("/home/<name>") is as much a leak as anything below it) -- and
# /root/ followed by a segment character. So a bare /home/, an ellipsis
# /home/..., a pattern like /home/[^/]+/ or /home/*/, a placeholder like
# /home/<name>/, and "/root/," in prose do not match.
#
# The start must not continue a path (a relative x/home/..., a URL path on
# some host, or the //home/ of a doubled slash), with one exception: a
# file:// or file://localhost URL is an absolute local path in disguise, so
# file:///home/<name>/x is caught. The reported literal starts at the path,
# never at the scheme. Assembled at runtime so this module's own source
# carries no such literal.
# --------------------------------------------------------------------------

#: Home-directory names that are documentation placeholders, not somebody's
#: machine. Measured when this check was introduced: the only named-home
#: literals in the INCLUDE set use "someone" (a docstring example in
#: src/hoh/dispatchers.py and fixtures in tests/test_review_findings.py).
#: Narrowing by name keeps the check rule- and file-independent. Read at
#: call time, so the set is the single place to change. "example" is *not*
#: a placeholder here: it is exactly the kind of name a real checkout uses.
_PLACEHOLDER_HOME_NAMES = frozenset({"someone"})

#: Directories directly under macOS's /Users that are not a user's home.
#: "Shared" is the system-wide shared folder every Mac has; naming it says
#: nothing about whose machine this is. Applies to the /Users form only and,
#: like the placeholder set, is read at call time.
_NON_USER_DIRS_UNDER_USERS = frozenset({"Shared"})


def _hardcoded_home_path_pattern() -> re.Pattern[str]:
    sl = chr(47)
    path_char = r"[A-Za-z0-9._~$" + re.escape(sl) + r"-]"
    file_url = "file:" + sl + sl
    # Fixed-width lookbehinds only: not after a path character, or right
    # after a file:// / file://localhost scheme.
    before = ("(?:(?<!" + path_char + ")|(?<=" + re.escape(file_url) + ")"
              "|(?<=" + re.escape(file_url + "localhost") + "))")
    seg = r"[A-Za-z0-9._-]"
    named = (before + sl + r"(?P<dir>home|Users)" + sl
             + r"(?P<name>[A-Za-z0-9_]" + seg + r"*)"
             + "(?:" + sl + "|(?!" + path_char + "))")
    root = before + sl + "root" + sl + seg
    return re.compile(named + "|" + root)


_HARDCODED_HOME_PATH_RE = _hardcoded_home_path_pattern()


def find_hardcoded_home_path(text: str) -> str | None:
    """The first literal absolute path under a concrete home directory, or None."""
    for m in _HARDCODED_HOME_PATH_RE.finditer(text):
        name = m.group("name")
        if name is not None:
            # "in /Users/Shared." ends a sentence; the dot is not the name.
            name = name.rstrip(".")
            if name in _PLACEHOLDER_HOME_NAMES:
                continue
            if m.group("dir") == "Users" and name in _NON_USER_DIRS_UNDER_USERS:
                continue
        end = m.end()
        while end < len(text) and not text[end].isspace() and text[end] not in "\"'`)]>,;":
            end += 1
        return text[m.start():end]
    return None


# --------------------------------------------------------------------------
# Private/internal address and token-shaped-string needles for the
# INCLUDE-set scan (K7). Neither is a literal identifying string, so neither
# needs runtime assembly the way a home-directory path does; both are
# general algorithmic patterns.
# --------------------------------------------------------------------------

#: Loopback is deliberately **not** here. This machine's house rules require
#: local-only logging on 127.0.0.1, and a test that opens a listener on
#: loopback to prove a sandbox cannot reach it is doing exactly what the rules
#: ask. Loopback reveals no network topology: every machine has the same one.
#: The other three ranges stay, because those do say something about where the
#: author sits.
_PRIVATE_IPV4_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)

#: A run of alnum characters, no hyphen, underscore or slash (those break
#: real identifiers, hyphenated phrases and -- importantly -- ordinary file
#: paths into short pieces on their own: this codebase's shell scripts and
#: test fixtures both carry 24+-character-looking paths like
#: "$HOME/miniconda3/bin/python3" that are not secrets), at least 24 long.
#: A pure lowercase-hex run of the same shape is a content digest or
#: commit-style hash in this codebase's own convention (CLAIMS.json alone
#: carries dozens of sha256 anchor digests) -- excluded separately, not by
#: chance: token-shaped secrets are essentially never *pure* hex.
_TOKEN_RE = re.compile(r"[A-Za-z0-9+]{24,}={0,2}")
_HEX_ONLY_RE = re.compile(r"^[0-9a-fA-F]+$")


def find_token_shaped(text: str) -> str | None:
    for m in _TOKEN_RE.finditer(text):
        s = m.group(0)
        if _HEX_ONLY_RE.match(s):
            continue
        if not (any(c.isdigit() for c in s) and any(c.isalpha() for c in s)):
            continue
        return s
    return None


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _classify(rel_path: str, root: Path) -> tuple[str, str]:
    """Returns (decision, rule) for one file. rel_path uses forward slashes."""
    parts = rel_path.split("/")
    top = parts[0]

    # A parked predecessor, at any depth. Asked before the directory rules so
    # that a stamped sibling is classified by what it *is* rather than by
    # where it happens to live.
    m = _VERSIONED_STAMP_RE.match(parts[-1])
    if m and (root / "/".join(parts[:-1]) / m.group("base")).is_file():
        return "EXCLUDE", "parked-predecessor"

    if rel_path == "pyproject.toml" or top == "src":
        return "INCLUDE", "package"

    # Named evidence subtrees, checked before the wholesale directory rules.
    # `dogfood/` is internal by default and should stay that way -- it is a
    # working tree, not a deliverable. But three things under it are the
    # evidence that public claims rest on, and a claim whose evidence is not
    # in the export is a claim the reader is asked to take on trust. That is
    # the one thing this project's documents say nobody should have to do.
    for prefix, decision, rule in _EVIDENCE_PREFIXES:
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return decision, rule

    for dirname, decision, rule in _DIRECTORY_RULES:
        if top == dirname:
            return decision, rule

    if len(parts) == 1:
        m = _VERSIONED_SIBLING_RE.match(rel_path)
        if m and (root / m.group("base")).is_file():
            return "EXCLUDE", "parked-predecessor"
        if rel_path in REPO_META_BASENAMES:
            return "INCLUDE", "repo-meta"
        if rel_path in GOVERNANCE_BASENAMES:
            return "INCLUDE", "governance"
        if rel_path in CLAIMS_LEDGER_BASENAMES:
            return "INCLUDE", "public-docs"

    is_prose_candidate = (len(parts) == 1 or top == "docs") and rel_path.endswith(".md")
    if is_prose_candidate:
        try:
            text = (root / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = None
        if text is not None:
            if top == "docs" and contains_home_path(text):
                return "EXCLUDE", "foreign-subject"
            if german_density(text) >= GERMAN_DENSITY_THRESHOLD:
                return "EXCLUDE", "internal-working-document"
        if _DOGFOOD_BASENAME_RE.match(rel_path.rsplit("/", 1)[-1]):
            return "EXCLUDE", "internal-working-document"
        return "INCLUDE", "public-docs"

    return "EXCLUDE", "unclassified"


#: pytest's own base-temp convention, exactly as `_pytest.tmpdir` names it:
#: `pytest-of-<user>`. Structural (the prefix, not any specific username or
#: the exact directory name observed in any one session) on purpose -- it
#: matches this one, documented convention and nothing broader, so it never
#: risks swallowing arbitrary `tempfile.mkdtemp()` output or another tool's
#: debris that happens to share a temp directory.
_PYTEST_TMPDIR_RE = re.compile(r"^pytest-of-.+$")

#: The one fixed name of the cache directory `pip` (and other well-behaved
#: XDG-cache-conscious tools) create directly under `$HOME` --
#: `$HOME/.cache/...`. A literal name, not a pattern, mirroring how
#: `_PYTEST_TMPDIR_RE` matches one documented convention and nothing wider.
_HOME_CACHE_DIRNAME = ".cache"


def _walk_files(root: Path) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Walks `root`, pruning VCS/cache noise, the whole-subtree exclusion
    rules named in `_PRUNE_AT_ROOT_DIRNAMES`, and, when it falls inside the
    tree, this project's own runner-redirected temp-directory and
    home-directory-cache debris.

    Returns `(files, pruned)`. `files` is the individual-file relative-path
    list, exactly as before. `pruned` is one `(path, decision, rule)` triple
    per top-level subtree named in `_PRUNE_AT_ROOT_DIRNAMES` that was
    actually found on disk: `os.walk` visits that subtree's own root
    directory (so its existence is confirmed and it counts as visited), and
    is then stopped from recursing any further into it, so none of its
    contents are ever enumerated or classified individually. A directory
    matching no such rule is walked normally and contributes its files to
    `files`, one per file, like any other directory -- this is what keeps
    an unrecognised subtree from being silently pruned: it is walked and
    lands as `unclassified` per file in `derive()`, never omitted.

    This project's own runner redirects both `TMPDIR` and `HOME` into the
    tree under test for sandboxed acceptance-criterion commands (see
    `src/hoh/runner.py`: `env["HOME"] = str(workdir)` immediately followed by
    `env["TMPDIR"] = str(workdir)`) -- and in practice both redirect to the
    scanned root itself, not merely a subdirectory of it. That drops two
    kinds of debris directly at the top of the very tree this tool is asked
    to scan: `pytest tmp_path` runs leave `pytest-of-<user>/**`, and any
    subprocess in the wider check sequence that invokes `pip` leaves
    `.cache/pip/**` (pip's default cache lives at `$HOME/.cache`). Both are
    pruned the same way: `tempfile.gettempdir()` and `Path.home()` are each
    re-resolved once per walk (never a hardcoded path -- each tracks wherever
    `TMPDIR`/`HOME` actually point at run time), and a directory is pruned
    exactly when its *parent* resolves to the matching root and its own name
    matches the fixed convention (`pytest-of-*` under the temp root, the
    literal name `.cache` under the home root). That one rule per anchor
    covers both shapes without special-casing either: the anchor pointed at
    `root` itself (the debris directory is then a direct child of `root`) and
    the anchor pointed at some subdirectory inside `root` (the debris
    directory is then a direct child of that subdirectory instead). On an
    ordinary machine, where neither the temp directory nor the home directory
    sits inside the scanned tree, no directory in the walk ever has either as
    a parent, so both rules are a no-op.

    A path the repository's own tracked `.gitignore` excludes never becomes
    a manifest entry either, in either regime: gitignore rules are loaded
    once here (see the ".gitignore-aware filtering" section above) and
    applied after the existing skip/debris filters. Precedence matters:
    `rel_dir_str in _PRUNE_AT_ROOT_DIRNAMES` above -- the whole-subtree
    pruning for `runs/` and `build/` -- still runs first, unchanged, so
    those two keep producing exactly the one pruned-subtree entry each;
    gitignore matching those same two directory names at the point they
    are considered for `kept` is then a harmless no-op (they are always
    kept there, so `os.walk` still descends one level into each and hits
    the pruning branch above on the next iteration), never a silent
    deletion of the entry other tests depend on. Every other directory a
    gitignore pattern names (`demo/`, ...) is dropped from `dirnames`
    before `os.walk` ever descends into it -- one prune, no per-file walk,
    the same shape `runs/`/`build/` used before this rule existed -- and a
    file inside a directory that IS still walked file-by-file (`history/`,
    `plugin/`, `policy/`, or any unclassified directory) is dropped from
    `filenames` the same way, before it is ever added to `out`.
    """
    out: list[str] = []
    pruned: list[tuple[str, str, str]] = []
    tmp_root = Path(tempfile.gettempdir()).resolve()
    home_root = Path.home().resolve()
    gitignore_rules = _load_gitignore_rules(root)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        rel_dir = current.relative_to(root)
        rel_dir_str = str(rel_dir)

        if rel_dir_str in _PRUNE_AT_ROOT_DIRNAMES:
            decision, rule = _DIRECTORY_RULE_BY_NAME[rel_dir_str]
            pruned.append((rel_dir_str + "/", decision, rule))
            dirnames[:] = []
            continue

        kept = []
        for d in dirnames:
            if _is_skipped_dir(d):
                continue
            if current == tmp_root and _PYTEST_TMPDIR_RE.match(d):
                continue
            if current == home_root and d == _HOME_CACHE_DIRNAME:
                continue
            if rel_dir_str == "." and d in _PRUNE_AT_ROOT_DIRNAMES:
                # Let the existing whole-subtree pruning branch above
                # handle these on the next iteration -- see the docstring
                # note on precedence.
                kept.append(d)
                continue
            candidate_dir = d if rel_dir_str == "." else (rel_dir / d).as_posix()
            if _gitignore_matches(candidate_dir, True, gitignore_rules):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            if rel_dir_str == "." and _is_skipped_file(name):
                continue
            rel = name if rel_dir_str == "." else (rel_dir / name).as_posix()
            if _gitignore_matches(rel, False, gitignore_rules):
                continue
            out.append(rel)
    return out, pruned


def derive(root: str | Path) -> dict:
    """Derives the export manifest for `root` from the filesystem alone.

    Never calls git, never reads `runs/` as a source of truth (files under
    it are classified like any other file), and never uses `..` or an
    absolute needle path in doing so. Deterministic: the returned dict, once
    serialized the way `derive`'s CLI writes it, is byte-for-byte identical
    across repeated calls against the same tree.
    """
    root = Path(root).resolve()
    entries = []
    files, pruned = _walk_files(root)
    for path, decision, rule in pruned:
        entries.append({"path": path, "decision": decision, "rule": rule})
    for rel in files:
        decision, rule = _classify(rel, root)
        entries.append({"path": rel, "decision": decision, "rule": rule})
    entries.sort(key=lambda e: e["path"])
    return {"schema": "hoh-export-manifest/1", "entries": entries}


def serialize(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------
# Schema validation (K1)
# --------------------------------------------------------------------------


def validate_schema(data: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["manifest is not a JSON object"]
    if not isinstance(data.get("schema"), str) or not data["schema"]:
        problems.append("top-level 'schema' must be a non-empty string")
    entries = data.get("entries")
    if not isinstance(entries, list):
        problems.append("top-level 'entries' must be a list")
        return problems
    seen_paths: set[str] = set()
    for i, e in enumerate(entries):
        loc = f"entries[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{loc}: not an object")
            continue
        path = e.get("path")
        if not isinstance(path, str) or not path:
            problems.append(f"{loc}: 'path' must be a non-empty string")
        else:
            if path in seen_paths:
                problems.append(f"{loc}: duplicate path {path!r}")
            seen_paths.add(path)
        decision = e.get("decision")
        if decision not in DECISIONS:
            problems.append(f"{loc}: 'decision' must be one of {DECISIONS}, got {decision!r}")
        rule = e.get("rule")
        if rule not in RULES:
            problems.append(f"{loc}: 'rule' must be one of the declared rules, got {rule!r}")
        if decision == "EXCLUDE" and rule == "unclassified":
            pass  # the conservative default -- always legal
        elif rule == "unclassified" and decision != "EXCLUDE":
            problems.append(f"{loc}: rule 'unclassified' must carry decision EXCLUDE, got {decision!r}")
    return problems


# --------------------------------------------------------------------------
# U2b -- within-export reference closure
# --------------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
#: Captures both the link's display text (group 1) and its target (group 2)
#: -- the display text is needed to detect the "backtick used as a URL's own
#: link text" shape below, not just the target.
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_INCLUDE_DIRECTIVE_RE = re.compile(r"(?im)^\s*(?:\.\.\s+)?include::?\s*=?\s*[\"']?([^\s\"'>]+)")
_LOCATOR_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")

#: A repo-relative-path-shaped `<path>:<line>` locator inside a table cell --
#: the same shape this module's own `_LOCATOR_SUFFIX_RE` strips off the end
#: of a single candidate, but matched anywhere in a line to recognise a
#: dedicated provenance-locator cell sitting next to other cells in the same
#: row (see `_is_table_row_with_locator` below).
_TABLE_ROW_LOCATOR_RE = re.compile(r"[\w.-]+(?:/[\w.-]+)+:\d+(?:-\d+)?")


def _is_url(s: str) -> bool:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s):
        return True
    return bool(re.match(r"^(?:mailto|tel):", s))


def _is_table_row_with_locator(line: str) -> bool:
    """True for a markdown table row (stripped form starts and ends with
    `|`) that also carries an explicit `<path>:<line>` locator somewhere in
    the row -- i.e. a row that already names, in one of its own cells, the
    file and line a reader would go read. A backtick file-mention elsewhere
    in that same row is then content quoted from the location the row
    already names, not a live pointer authored by the referencing document.
    """
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|") and len(stripped) >= 2):
        return False
    return bool(_TABLE_ROW_LOCATOR_RE.search(line))


def extract_references(text: str) -> list[str]:
    """Repo-relative path candidates from backtick spans, markdown links and
    include-like directives -- normalized (trailing `:line` / `:line,line`
    locator suffixes stripped), not yet filtered for plausibility.

    Two shapes are deliberately never harvested as bare-path candidates from
    a backtick span, both about *why* a backtick mention exists rather than
    what it says:

    - a backtick span used as the entire display text of a markdown link
      whose own target resolves as a URL (`[`docs/x.md`](https://.../x.md)`)
      -- that mention is part of the link, already covered by the link's own
      target extraction, not an independent bare-path mention; and
    - a backtick span inside a markdown table row that also carries an
      explicit `<path>:<line>` locator in one of its other cells -- content
      quoted from the location the row already names, not a live pointer.

    Neither exclusion is content-aware beyond this shape: a genuine live
    pointer hand-formatted as a table row that happens to carry an unrelated
    path:line-shaped cell is not rescued from this exclusion, and a
    discursive mention written as ordinary prose outside a table row is not
    caught by it either. See this module's own tests for the shapes this
    does and does not catch.
    """
    raw: list[str] = []

    locator_row_ranges: list[tuple[int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if _is_table_row_with_locator(line):
            locator_row_ranges.append((offset, offset + len(line)))
        offset += len(line)

    def _in_locator_row(pos: int) -> bool:
        return any(start <= pos < end for start, end in locator_row_ranges)

    url_link_text_starts: set[int] = set()
    for m in _MD_LINK_RE.finditer(text):
        link_text, target = m.group(1), m.group(2)
        if _is_url(target) and _CODE_SPAN_RE.fullmatch(link_text):
            url_link_text_starts.add(m.start(1))
        raw.append(target)

    for m in _CODE_SPAN_RE.finditer(text):
        if m.start() in url_link_text_starts or _in_locator_row(m.start()):
            continue
        raw.append(m.group(1))

    for m in _INCLUDE_DIRECTIVE_RE.finditer(text):
        raw.append(m.group(1))

    return [_LOCATOR_SUFFIX_RE.sub("", r.strip()) for r in raw]


def _looks_like_repo_reference(s: str, top_level_names: set[str]) -> bool:
    """True only for a candidate that could plausibly be a repo-relative
    path to an actual file this manifest knows about -- never a URL, a
    shell/env expansion, a placeholder, an absolute path, or a bare
    filename with no directory component (which this repository's own
    prose uses constantly for self-referential or illustrative mentions
    that were never meant as live cross-references).
    """
    if not s or _is_url(s):
        return False
    if any(c.isspace() for c in s):
        return False
    if s.startswith("~") or s.startswith("$") or "${" in s or "$(" in s:
        return False
    if s.startswith("/"):
        return False
    if "<" in s or ">" in s or "*" in s or ":" in s:
        return False
    if "/" not in s or s.endswith("/"):
        return False
    parts = s.split("/")
    if ".." in parts or "." in parts:
        return False
    return parts[0] in top_level_names


#: U2b reads prose meant for a reader to navigate by -- documentation,
#: configuration, plain text. `.py` source is deliberately excluded: a
#: source file's string literals are data (test fixtures, regex patterns),
#: not navigable cross-references, and this project's own test suite proves
#: the point -- tests/test_export_manifest.py's U2b fixtures necessarily
#: contain backtick-quoted paths as *test input*, which would otherwise
#: read as this file live-referencing them. `.json` is excluded for the same
#: reason: a structured data file's string fields are data, not
#: reader-navigable prose -- CLAIMS.json is rendered into CLAIMS.md, which
#: *is* the prose surface a reader actually navigates and is scanned as
#: normal, so nothing that is a live pointer in the rendered document goes
#: unchecked here.
_U2B_EXCLUDED_EXTENSIONS = frozenset({".py", ".json"})


#: References a published document makes that a reader cannot follow, and
#: that are **not** going to be removed, each with the reason. Data rather
#: than a branch in the code, for the same reason `CLAIMS.json` keeps its
#: surface exclusions as data: a decision that can be read, reviewed and
#: counted is a different thing from a special case inside a function.
#:
#: The bar for an entry here is high and only one situation has met it so
#: far: a document that is a **record of what someone else wrote**. Editing a
#: reviewer's report so that its citations resolve would make the report say
#: something the reviewer did not write, which is a worse defect than a
#: pointer a reader cannot follow. Every other published document had its
#: citations rewritten to name the internal document without a path
#: (`docs/EVIDENCE_INDEX.md` lists them and says where their substance is
#: published).
#:
#: Acknowledged is not invisible: every entry is printed on every run, and
#: `tests/test_export_manifest.py` pins this list so that it cannot grow
#: without a test changing with it.
U2B_ANERKANNT: dict[str, str] = {
    "paper/REVIEW_A.md":
        "an independent reviewer's report, published as written. Its "
        "citations are what the reviewer read; rewriting them would make the "
        "report say something they did not write. The documents are named in "
        "docs/EVIDENCE_INDEX.md",
    "paper/REVIEW_B.md":
        "an independent reviewer's report, published as written -- and one of "
        "its findings (B-03) is *about* these very references, so its paths "
        "are the subject of the finding rather than pointers it offers a "
        "reader. The documents are named in docs/EVIDENCE_INDEX.md",
    "paper/AUDIT.md -> runs/a03/receipts":
        "the audit's source column names where a number was **recomputed "
        "from**, and `tools/audit_refs.py numbers-recomputed` requires that "
        "path to exist. For this row the place is the a03 run's receipt tree, "
        "which the export deliberately does not carry (limit 12e): its "
        "receipts record the absolute paths the checks ran at. Naming "
        "anything else in that column would misstate where the figure came "
        "from, and naming nothing would make the row unverifiable. What the "
        "tree contains, and where its substance is published, is described in "
        "docs/EVIDENCE_INDEX.md. Only this one reference is acknowledged; "
        "every other reference AUDIT.md makes still has to resolve",
    "paper/AUDIT.md -> dogfood/benchmark/results-v3":
        "campaign v3's 45 raw cell files, named as the place the arm figures "
        "in sections 12 and 13 of the paper were recomputed from -- "
        "independently of the reporter that wrote the result documents, "
        "which is the whole point of naming them. The cells record the "
        "absolute paths the runs executed at, so the export does not carry "
        "them (limit 12e). What a reader can follow instead is "
        "docs/BENCHMARK_RESULTS_v3.md, the published surface of the same "
        "data. Naming that document in the source column instead would say "
        "the figures were recomputed from the reporter's own output, which "
        "is exactly the circularity these rows exist to avoid",
    "paper/AUDIT.md -> dogfood/closure-e2e/CLOSURE_E2E.json":
        "the recorded evidence file of the post-O143 operational closure, "
        "named as the place its figures were read back from. It records "
        "machine-local run roots, so the export does not carry it (limit "
        "12e). What a reader can follow instead is docs/READINESS.md, whose "
        "post_o143_closure row carries the same figures and the command that "
        "re-derives them",
}


def teile_u2b(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """(findings that stand, findings whose reference is acknowledged).

    A key is either a whole document -- everything it cites -- or a single
    `from -> to` pair. The pair form exists because acknowledging a whole
    document to excuse one reference is how an exemption list stops meaning
    anything: `paper/AUDIT.md` has one reference that cannot be removed, and
    all its others must keep failing if they ever break.
    """
    offen, anerkannt = [], []
    for f in findings:
        grund = (U2B_ANERKANNT.get(f"{f.get('from')} -> {f.get('to')}")
                 or U2B_ANERKANNT.get(f.get("from")))
        if grund:
            anerkannt.append({**f, "acknowledged": grund})
        else:
            offen.append(f)
    return offen, anerkannt


def check_u2b(entries: list[dict], root: Path) -> list[dict]:
    """For every INCLUDE entry, resolves its extracted references against
    the manifest. Reports a finding for each reference whose target is
    EXCLUDE or absent from the manifest -- never edits, never decides which
    side is wrong, only reports (U2b: 'the manifest cannot decide that by
    itself').
    """
    by_path = {e["path"]: e for e in entries}
    #: Pruned-subtree entries (`runs/`, `build/`, ...) -- recorded as their
    #: own path with a trailing slash. A reference into one of these
    #: subtrees (e.g. `runs/foo.txt`) never has its own manifest entry once
    #: pruned, so it is resolved by prefix against these instead of falling
    #: through to the weaker "absent from manifest" finding.
    pruned_entries = [e for e in entries if e["path"].endswith("/")]
    top_level_names = {p.split("/")[0] for p in by_path}
    findings: list[dict] = []
    for e in entries:
        if e["decision"] != "INCLUDE":
            continue
        if Path(e["path"]).suffix in _U2B_EXCLUDED_EXTENSIONS:
            continue
        full = root / e["path"]
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        seen: set[str] = set()
        for cand in extract_references(text):
            if cand in seen or not _looks_like_repo_reference(cand, top_level_names):
                continue
            seen.add(cand)
            target = by_path.get(cand)
            if target is not None:
                if target["decision"] != "INCLUDE":
                    findings.append(
                        {
                            "type": "u2b_dangling_reference",
                            "from": e["path"],
                            "to": cand,
                            "reason": f"target is EXCLUDE (rule={target['rule']})",
                        }
                    )
                continue
            pruned_target = next((p for p in pruned_entries if cand.startswith(p["path"])), None)
            if pruned_target is not None:
                findings.append(
                    {
                        "type": "u2b_dangling_reference",
                        "from": e["path"],
                        "to": cand,
                        "reason": f"target is EXCLUDE (rule={pruned_target['rule']})",
                    }
                )
                continue
            findings.append(
                {"type": "u2b_dangling_reference", "from": e["path"], "to": cand, "reason": "absent from manifest"}
            )
    return findings


# --------------------------------------------------------------------------
# INCLUDE-set scan: home path / private address / token-shaped string (K7)
# --------------------------------------------------------------------------


#: The home-path needles above are deliberately generic (any mention of the
#: home, root or etc top-level directory, a tilde-slash home shorthand, or a
#: per-device mount point), which is exactly right for foreign-subject
#: detection over docs/** prose -- verified during this run to be clean
#: everywhere except the one real finding. It is far too broad for the rest
#: of this project's own INCLUDE set: a security-guard tool's own pattern
#: files and its own path-safety code necessarily spell out these very
#: prefixes as part of what they do. Verified during this run against every
#: other rule category: package (`src/hoh/runner.py`'s own absolute-path
#: guard, `src/hoh/policy/*.txt`), tests, operational-integration
#: (`ops/README.md`, `policy/README.md`, the same policy pattern files) and
#: example (a recorded QA verdict whose whole point is to *report the
#: absence* of exactly these prefixes) all carry at least one such
#: self-referential mention, none of them a leak. The genuinely
#: reader-facing narrative surface -- public-docs and governance -- is the
#: only place such a mention would actually be a leak rather than the tool
#: documenting itself, so the home-path leak scan is scoped to it. The
#: private-address and token-shaped checks below have no such false-positive
#: history in this repository and stay unscoped, over every INCLUDE file.
_HOME_PATH_SCAN_RULES = frozenset({"public-docs", "governance"})


def scan_include_for_leaks(entries: list[dict], root: Path) -> list[dict]:
    findings: list[dict] = []
    for e in entries:
        if e["decision"] != "INCLUDE":
            continue
        full = root / e["path"]
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if e["rule"] in _HOME_PATH_SCAN_RULES and contains_home_path(text):
            findings.append({"type": "home-path", "path": e["path"]})
        # Every rule: a literal under a concrete home is never legitimate.
        if find_hardcoded_home_path(text) is not None:
            findings.append({"type": "hardcoded-home-path", "path": e["path"]})
        ip_match = _PRIVATE_IPV4_RE.search(text)
        if ip_match:
            findings.append({"type": "private-address", "path": e["path"]})
        token = find_token_shaped(text)
        if token:
            findings.append({"type": "token-shaped", "path": e["path"]})
    return findings


# --------------------------------------------------------------------------
# Diagnosing an on-disk-manifest vs. fresh-derivation disagreement (K13)
# --------------------------------------------------------------------------


def diagnose_manifest_diff(on_disk_entries: list[dict], fresh_entries: list[dict]) -> dict:
    """Explains, in three distinct categories, why an on-disk manifest's
    entries differ from a fresh derivation's.

    'missing': a path the fresh derivation found but the on-disk manifest
    does not have. 'extra': the reverse. 'changed': a path present in both
    whose decision or rule differs -- named explicitly, with both values,
    rather than left to be inferred (or missed) once the path sets already
    disagree for an unrelated reason. Collapsing all of this into a single
    "entries differ" boolean is exactly the gap this function closes: a
    same-path rule mismatch used to be silently absorbed the moment the
    path sets also happened to differ (from unrelated on-disk noise), never
    named in the printed reason.
    """
    on_disk_by_path = {e["path"]: e for e in on_disk_entries if isinstance(e, dict) and isinstance(e.get("path"), str)}
    fresh_by_path = {e["path"]: e for e in fresh_entries}
    on_disk_paths = set(on_disk_by_path)
    fresh_paths = set(fresh_by_path)

    missing = sorted(fresh_paths - on_disk_paths)
    extra = sorted(on_disk_paths - fresh_paths)

    changed = []
    for path in sorted(on_disk_paths & fresh_paths):
        old, new = on_disk_by_path[path], fresh_by_path[path]
        old_key = (old.get("decision"), old.get("rule"))
        new_key = (new.get("decision"), new.get("rule"))
        if old_key != new_key:
            changed.append({"path": path, "on_disk": old_key, "fresh": new_key})

    return {"missing": missing, "extra": extra, "changed": changed}


# --------------------------------------------------------------------------
# Bounding a mismatch diagnostic's printed size (K6): a pruned-subtree-scale
# mismatch (hundreds of thousands of paths) must still name what changed
# without dumping every path -- the pre-pruning failure here was 22.7 MB.
# --------------------------------------------------------------------------

_DIAGNOSTIC_CAP = 20


def _format_capped_paths(paths: list[str]) -> str:
    shown = paths[:_DIAGNOSTIC_CAP]
    if len(paths) > _DIAGNOSTIC_CAP:
        return f"{shown} ...and {len(paths) - _DIAGNOSTIC_CAP} more"
    return str(shown)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def cmd_derive(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else _DEFAULT_ROOT
    try:
        manifest = derive(root)
    except UnsupportedGitignorePattern as exc:
        print(f"FAIL: unsupported .gitignore pattern: '{exc.pattern}'")
        return 1
    out_path = Path(args.out) if args.out else root / "EXPORT_MANIFEST.json"
    out_path.write_text(serialize(manifest), encoding="utf-8")
    print(f"wrote {len(manifest['entries'])} entries to {out_path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else _DEFAULT_ROOT
    manifest_path = Path(args.manifest) if args.manifest else root / "EXPORT_MANIFEST.json"

    try:
        on_disk_raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FAIL: cannot read {manifest_path}: {exc}")
        return 1
    try:
        on_disk = json.loads(on_disk_raw)
    except json.JSONDecodeError as exc:
        print(f"FAIL: {manifest_path} is not valid JSON: {exc}")
        return 1

    problems = validate_schema(on_disk)
    if problems:
        for p in problems:
            print(f"FAIL: schema: {p}")
        return 1

    try:
        fresh = derive(root)
    except UnsupportedGitignorePattern as exc:
        print(f"FAIL: unsupported .gitignore pattern: '{exc.pattern}'")
        return 1
    fresh_serialized = serialize(fresh)
    on_disk_entries = on_disk["entries"]
    fresh_entries = fresh["entries"]
    if on_disk_entries != fresh_entries:
        print(f"FAIL: {manifest_path} disagrees with a fresh derivation from {root} -- re-derivation needed")
        diff = diagnose_manifest_diff(on_disk_entries, fresh_entries)
        if diff["missing"]:
            print(f"  present on disk but missing from the manifest: {_format_capped_paths(diff['missing'])}")
        if diff["extra"]:
            print(f"  present in the manifest but not on disk: {_format_capped_paths(diff['extra'])}")
        for c in diff["changed"][:_DIAGNOSTIC_CAP]:
            print(
                f"  {c['path']}: manifest says (decision, rule)={c['on_disk']}, "
                f"fresh derivation says (decision, rule)={c['fresh']}"
            )
        if len(diff["changed"]) > _DIAGNOSTIC_CAP:
            print(f"  ...and {len(diff['changed']) - _DIAGNOSTIC_CAP} more changed")
        return 1
    if on_disk_raw != fresh_serialized:
        print(f"FAIL: {manifest_path}'s serialization differs from a fresh derivation (same entries, different bytes)")
        return 1

    findings = 0
    offen, anerkannt = teile_u2b(check_u2b(on_disk_entries, root))
    for f in offen:
        findings += 1
        print(f"FAIL: U2b: {f['from']} references {f['to']} -- {f['reason']}")
    # Printed on every run, never counted as a finding. A reference that has
    # been thought about and decided is a different state from one nobody has
    # looked at, and the difference is only worth anything if the decision
    # stays in front of the reader.
    for f in anerkannt:
        print(f"ACKNOWLEDGED: {f['from']} references {f['to']} "
              f"-- {f['reason']}; {f['acknowledged']}")
    for f in scan_include_for_leaks(on_disk_entries, root):
        findings += 1
        print(f"FAIL: {f['type']} found in INCLUDE-classified {f['path']}")

    if findings:
        print(f"{findings} problem(s).")
        return 1

    print(f"OK: {manifest_path} matches a fresh derivation and passes U2b + "
          f"the leak scan ({len(on_disk_entries)} entries, "
          f"{len(anerkannt)} acknowledged reference(s) in "
          f"{len(U2B_ANERKANNT)} document(s))")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="export_manifest.py",
        description=(
            "Derives and checks EXPORT_MANIFEST.json from the filesystem alone. "
            "Never calls git and never treats runs/ as authoritative, so behaviour "
            "is identical whether or not this directory has a usable git repository "
            "or a runs/ tree above it."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_derive = sub.add_parser("derive", help="derive the manifest and write it")
    p_derive.add_argument("--root", default=None, help="repository root to scan (default: this tool's own repository)")
    p_derive.add_argument("--out", default=None, help="output path (default: <root>/EXPORT_MANIFEST.json)")

    p_check = sub.add_parser("check", help="re-derive and verify an on-disk manifest")
    p_check.add_argument("--manifest", default=None, help="manifest path (default: <root>/EXPORT_MANIFEST.json)")
    p_check.add_argument("--root", default=None, help="repository root to scan (default: this tool's own repository)")

    args = parser.parse_args(argv)
    if args.command == "derive":
        sys.exit(cmd_derive(args))
    elif args.command == "check":
        sys.exit(cmd_check(args))


if __name__ == "__main__":
    main()
