#!/usr/bin/env python3
"""docs_numbers.py -- the figures of the second paper, derived and checked.

`paper/NUMBERS_V2.md` lists every figure that `paper/POSITION_PAPER_V2.md`
tags (`[V07t]` and the like) and every development figure that `README.md`,
`RELEASE_NOTES.md`, `CHANGELOG.md`, `docs/LIMITATIONS.md` and the dated errata
of `paper/POSITION_PAPER.md` state. A number in prose that nobody can
regenerate is the failure this project spends its documentation refusing, so
this tool makes the table a product of tools rather than of typing:

* a figure about the **moving present** (`R` in the table) is derived here from
  the repository's own tools -- `tools/capability_matrix.py`,
  `tools/build_plan.py`, `tools/attribution.py`, the git history, the board --
  at a named base commit;
* a **dated historical** figure is not re-measured; its ledger entry must still
  contain it. The check reads `DOGFOOD_LEDGER.md` and fails when an entry does
  not carry the text the row relies on, so the row cannot drift away from its
  source;
* the table is **rendered**, never edited: `check` renders it again and requires
  the committed file to be identical;
* every `[Vnn]` tag in the documents must name a row, every row must be used,
  and the figure a tag stands for must appear in the paragraph that carries it.

It cannot find a refusal that the registry below omits (rows V07 and V07b are
a hand-kept list whose entries are verified, not discovered), and it says so in
the table.

Usage:
    python3 tools/docs_numbers.py derive [--base REV] [--json]
    python3 tools/docs_numbers.py render [--base REV]
    python3 tools/docs_numbers.py audit  [--write]
    python3 tools/docs_numbers.py check

Exit 0 when everything verified, 1 when a figure or a tag is wrong, 3 when the
sources needed (git history, the ledger) are not in this tree: an environment
gap is not a pass.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

NUMBERS = "paper/NUMBERS_V2.md"
AUDIT = "paper/AUDIT_V2.md"
LEDGER = "DOGFOOD_LEDGER.md"

#: Documents whose `[Vnn]` tags are checked against the table.
TAGGED_DOCS = (
    "README.md",
    "RELEASE_NOTES.md",
    "CHANGELOG.md",
    "docs/LIMITATIONS.md",
    "paper/POSITION_PAPER_V2.md",
    "paper/POSITION_PAPER.md",
)

TAG_RE = re.compile(r"\[(V\d+[a-z]?)\]")
BASE_RE = re.compile(r"^Base commit: `([0-9a-f]{40})`", re.M)
AUDIT_BEGIN = "<!-- BEGIN GENERATED: tools/docs_numbers.py audit -->"
AUDIT_END = "<!-- END GENERATED: tools/docs_numbers.py audit -->"

#: The merge that carries the qa-arena-write follow-up (qaw-followup, merge
#: ab41589f, ledger O255). It closes the three findings of the reviewed
#: qa-arena-write repair (a455edc0, ledger O253) that kept the integrity gate of
#: the v0.3.0a1 pre-release open. Being an ancestor is necessary for the gate
#: and not sufficient: the closure covers the paths its reviewers replayed, and
#: the documents state its residual. (Until 2026-10-07 this constant named the
#: repair itself, a455edc0, and the documents said the gate was not met; that
#: statement is kept as history in docs/LIMITATIONS.md, limit 12d.)
INTEGRITY_GATE_COMMIT = "ab41589f"

#: The window in which the product-run merge convention exists.
FIRST_WINDOW_START = "2026-09-15 00:00:00 +0300"
FIRST_COUNT_END = "2026-10-03 00:00:00 +0300"
CONVENTION_START = "2026-09-25 00:00:00 +0300"

REFUSAL_MARKER = re.compile(r"refused|release no|no/no|release yes/no", re.I)

#: Candidates the product accepted and independent integration review refused
#: (not merged as they stood). (name, ledger entry that records it). The list is
#: kept by hand: every item is verified against its entry, none is discovered.
REFUSED = (
    ("pproof", "O236"), ("pregen", "O236"),
    ("pqagate", "O237"), ("pqagate2", "O237"), ("psupdrift", "O237"),
    ("sreg", "O240"), ("sdurw", "O240"), ("sadmit", "O240"), ("sarch", "O240"),
    ("sreg2", "O241"), ("sarch2", "O241"),
    ("sarch3", "O242"),
    ("shost", "O244"),
    ("sstew", "O249"), ("sstew2", "O249"),
    ("harness", "O251"), ("lprompt", "O251"), ("sstew4b", "O251"),
    # after the first count: O252 to O254
    ("supdrift", "O252"), ("dres2", "O252"), ("qaw5", "O252"),
    ("sss", "O253"), ("qaw7", "O253"), ("et", "O253"), ("et2", "O253"),
    # after O253: the rpl candidate, refused at integration (rplint-a/b, release
    # NO, tests only) and merged with its tests-only follow-up (ledger O254, O255)
    ("rpl", "O254"),
)
FIRST_COUNT_ENTRIES = {"O236", "O237", "O240", "O241", "O242", "O244", "O249",
                       "O251"}

A5_IDS = ("P2-43", "P3-41", "P10-17", "P11-02", "P12-06")

#: Strands the documents say are not merged. A tripwire, not a proof: it looks
#: for a product-run merge whose title names one of them, so a merge that does
#: not name its strand goes unseen. When one is found the documents are saying
#: something that is no longer true, and the check fails.
UNMERGED_STRANDS = ("failure-classes", "role-quiescence", "qa-gap-gate",
                    "criterion-fidelity", "lp-dirchain")
#: By addition, 2026-10-07: removed from this tuple because they merged:
#: prompt-threshold (33320bfc), qaw-followup and qa-arena-follow (ab41589f),
#: role-pane and role pane (caa732db, the role-pane lifecycle with rpl-tests).
#: Added because the documents now name them as open: crl-followup (the
#: context-rollover follow-up) and first-prompt (first-prompt readiness).
#: By addition, 2026-10-09: both removed again because they merged:
#: crl-followup (6091cea9, ledger O257) and first-prompt readiness (846d8e02,
#: ledger O258). "failure-classes" stays: only slice a of its core, P2-43, is
#: merged (6312e46d, ledger O258, a title that does not name this strand), and
#: the register still reads P2-43 MISSING.


class Gap(Exception):
    """A source this derivation needs is not in this tree."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _run(root: Path, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), cwd=root, capture_output=True, text=True)


def _git(root: Path, *args: str, check: bool = True) -> str:
    p = _run(root, "git", *args)
    if p.returncode != 0 and check:
        raise Gap(f"git {' '.join(args[:3])} failed: {p.stderr.strip()[:200]}")
    return p.stdout


def resolve_base(root: Path, rev: str) -> str:
    out = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}",
               check=False).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", out):
        raise Gap(f"{rev!r} is not a commit of this repository")
    return out


#: A ledger entry heading: `### O252 --`, `### O232 \u2014` or `## P2-26 --`. An
#: entry may carry lower-level or even same-level sub-headings of its own, so
#: it ends at the next entry heading, not at the next heading.
ENTRY_HEAD = re.compile(r"^#{2,3} (O\d+|P\d+-\d+)\b[^\n]*?(--|\u2014)")


def _entry_section(ledger_lines: list[str], key: str) -> tuple[int, int] | None:
    """Line range [start, end) of the ledger entry headed `### <key> --`."""
    head = re.compile(r"^#{2,3} " + re.escape(key) + r" (--|\u2014)")
    for i, line in enumerate(ledger_lines):
        if not head.match(line):
            continue
        j = i + 1
        while j < len(ledger_lines) and not ENTRY_HEAD.match(ledger_lines[j]):
            j += 1
        return i, j
    return None


def ledger_find(ledger_lines: list[str], entry: str, needle: str):
    """1-based line number of `needle` inside the ledger entry, or None."""
    span = _entry_section(ledger_lines, entry)
    if span is None:
        return None
    for i in range(span[0], span[1]):
        if needle in ledger_lines[i]:
            return i + 1
    return None


def _load_ledger(root: Path) -> list[str]:
    p = root / LEDGER
    if not p.is_file():
        raise Gap(f"{LEDGER} is not in this tree (it is not part of the export)")
    return p.read_text(encoding="utf-8").split("\n")


# --------------------------------------------------------------------------
# derivation of the moving-present figures
# --------------------------------------------------------------------------

#: Product-run merges whose title does not begin "Merge VeriHarness run".
#: By addition, 2026-10-09: `a7d73b8e` ("Merge p217o: ...", the P2-17 owned
#: agent-control contracts, ledger O259) is a product-run merge by the same
#: convention (accepted candidate, integration pair, merge gate) and the old
#: title grep missed it, as it had missed `caa732db` before.
#: By addition, 2026-10-09: `b7cf790b` ("Merge rbtf: ...", the role scratch
#: follow-up) and `a4e8fbad` ("Merge p243b + p243bf + p243bf2: ...", P2-43 slice b
#: with its two repairs), both ledger O260, are product-run merges with titles
#: of the same kind. A convention for the title of a product-run merge would make
#: this list unnecessary (tracked, ledger O260).
EXTRA_MERGE_TITLES = ("^Merge role-pane-lifecycle", "^Merge p217o", "^Merge rbtf",
                      "^Merge p243b")


def _merges(root: Path, base: str, since: str, until: str | None = None):
    args = ["log", base, f"--since={since}", "--grep=^Merge VeriHarness run",
            *[f"--grep={x}" for x in EXTRA_MERGE_TITLES], "--format=%H|%cs"]
    if until:
        args.insert(3, f"--until={until}")
    out = _git(root, *args)
    return [tuple(x.split("|")) for x in out.splitlines() if x]


def derive_git(root: Path, base: str) -> dict:
    merges = _merges(root, base, FIRST_WINDOW_START)
    touching = 0
    for sha, _d in merges:
        p = _run(root, "git", "diff", "--quiet", f"{sha}^1", sha, "--",
                 "src/hoh")
        if p.returncode == 1:
            touching += 1
        elif p.returncode != 0:
            raise Gap(f"git diff failed for {sha[:8]}")
    dates = sorted(d for _s, d in merges)
    first_window = _merges(root, base, FIRST_WINDOW_START, FIRST_COUNT_END)
    subjects = _git(root, "log", base, f"--since={FIRST_WINDOW_START}",
                    f"--until={CONVENTION_START}", "--format=%s").splitlines()
    candidates = sum(1 for s in subjects
                     if "candidate as the product wrote it" in s)
    return {
        "merges": len(merges),
        "merges_first": dates[0] if dates else "",
        "merges_last": dates[-1] if dates else "",
        "merges_src": touching,
        "merges_first_window": len(first_window),
        "candidates_imported": candidates,
    }


#: The documents state dates in the commits' own offset (see the table's preface).
LINE_TZ = datetime.timezone(datetime.timedelta(hours=3))
LEDGER_ENTRY_HEADING = re.compile(r"^#{2,3} O(\d+)\b")


def derive_line(root: Path, base: str) -> dict:
    """The shape of the development line at the base: first and latest commit,
    elapsed time, calendar days with a commit, the commit-free runs and the
    longest run of consecutive days with a commit, the commit counts, and the
    ledger's numbered entry headings (added 2026-10-09, ledger O263). Every
    figure is a count over history up to the base; a day is the commit's own
    calendar day at +0300."""
    stamps = _git(root, "log", base, "--format=%cI").split()
    if not stamps:
        raise Gap("no commits up to the base")
    when = sorted(datetime.datetime.fromisoformat(x).astimezone(LINE_TZ)
                  for x in stamps)
    first, last = when[0], when[-1]
    delta = last - first
    days = sorted({w.date() for w in when})
    one = datetime.timedelta(days=1)
    gaps = [(a + one, b - one, (b - a).days - 1)
            for a, b in zip(days, days[1:]) if (b - a).days > 1]
    best_n, best_from, best_to = 0, days[0], days[0]
    run_from, run_n = days[0], 1
    for a, b in zip(days, days[1:]):
        if (b - a).days == 1:
            run_n += 1
        else:
            run_from, run_n = b, 1
        if run_n > best_n:
            best_n, best_from, best_to = run_n, run_from, b
    if best_n == 0:
        best_n, best_from, best_to = 1, days[0], days[0]
    merges = int(_git(root, "rev-list", "--count", "--merges", base).strip())
    total = int(_git(root, "rev-list", "--count", base).strip())
    out = {
        "first": first.strftime("%Y-%m-%d %H:%M"),
        "last": last.strftime("%Y-%m-%d %H:%M"),
        "first_day": str(first.date()), "last_day": str(last.date()),
        "elapsed_days": delta.days, "elapsed_hours": delta.seconds // 3600,
        "calendar_days": (days[-1] - days[0]).days + 1,
        "commit_days": len(days),
        "gaps": [(str(a), str(b), n) for a, b, n in gaps],
        "streak": (best_n, str(best_from), str(best_to)),
        "commits": total, "merges": merges, "non_merge": total - merges,
    }
    shown = _git(root, "show", f"{base}:{LEDGER}", check=False)
    nums = sorted({int(m.group(1)) for line in shown.split("\n")
                   for m in [LEDGER_ENTRY_HEADING.match(line)] if m})
    if nums:
        out["ledger_entries"] = (len(nums), nums[0], nums[-1])
    return out


def count_captain(attribution_json: str) -> tuple[int, int]:
    """(entries of category CAPTAIN, all entries) of an attribution ledger."""
    entries = json.loads(attribution_json).get("entries") or []
    return sum(1 for e in entries if e.get("category") == "CAPTAIN"), len(entries)


def derive_attribution(root: Path, base: str) -> dict:
    """The attribution figures with the ledger as committed at the base."""
    text = _git(root, "show", f"{base}:dogfood/ATTRIBUTION.json")
    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "ATTRIBUTION.json"
        led.write_text(text, encoding="utf-8")
        p = _run(root, sys.executable, "-B", "tools/attribution.py", "--json",
                 "--ledger", str(led))
    if p.returncode == 3:
        raise Gap("tools/attribution.py reports an environment gap")
    try:
        rep = json.loads(p.stdout)
    except ValueError as exc:
        raise Gap(f"tools/attribution.py gave no JSON: {exc}") from exc
    nodes = rep["development_nodes"]
    through = rep["nodes_through_the_product"]
    merged = rep["nodes_developed_by_the_product_merged_by_the_orchestrator"]
    captain, entries = count_captain(text)
    return {"nodes": nodes, "through": through, "merged": merged,
            "direct": nodes - through - merged, "captain": captain,
            "entries": entries}


def derive_matrix(root: Path) -> dict:
    p = _run(root, sys.executable, "-B", "tools/capability_matrix.py", "--json")
    try:
        d = json.loads(p.stdout)
    except ValueError as exc:
        raise Gap(f"tools/capability_matrix.py gave no JSON: {exc}") from exc
    caps = d["capabilities"]

    def count(pred):
        c: dict[str, int] = {}
        for x in caps:
            if pred(x):
                c[x["status"]] = c.get(x["status"], 0) + 1
        return c

    later = ("P0", "P1", "P2")
    by_id = {x["id"]: x["status"] for x in caps}
    return {
        "total": len(caps),
        "counts": d["counts"],
        "p1": count(lambda x: x["target_phase"] == "P1"),
        "p2": count(lambda x: x["target_phase"] == "P2"),
        "p3_p12": count(lambda x: x["target_phase"] not in later),
        "p2_42": by_id.get("P2-42", "ABSENT"),
        "a5": {i: by_id.get(i, "ABSENT") for i in A5_IDS},
    }


def derive_plan(root: Path) -> dict:
    p = _run(root, sys.executable, "-B", "tools/build_plan.py")
    text = p.stdout
    m = re.search(r"^(\d+) open requirement\(s\) in \d+ wave\(s\); (\d+) held "
                  r"behind a phase gate", text, re.M)
    g = re.search(r"^\s*gate P2: (.*)$", text, re.M)
    if not m or not g:
        raise Gap("tools/build_plan.py output has no summary or P2 gate line")
    gate = g.group(1)

    def grab(pattern):
        x = re.search(pattern, gate)
        return int(x.group(1)) if x else 0

    return {
        "open": int(m.group(1)), "held": int(m.group(2)),
        "blocking": grab(r"(\d+) blocking open"),
        "bound": grab(r"(\d+) exit condition\(s\) bound to"),
        "unmeasured": grab(r"(\d+) exit condition\(s\) nothing here measures"),
    }


def derive_docs(root: Path) -> dict:
    board = (root / "docs/READINESS.md").read_text(encoding="utf-8")
    m = re.search(r"TECHNICALLY_STABLE_READY = (yes|no)", board)
    lim = (root / "docs/LIMITATIONS.md").read_text(encoding="utf-8")
    return {
        "ready": m.group(1) if m else "UNKNOWN",
        "limit_headings": len(re.findall(r"^## \d", lim, re.M)),
    }


def derive_all(root: Path, base: str) -> dict:
    facts = {"base": base}
    facts["git"] = derive_git(root, base)
    facts["attribution"] = derive_attribution(root, base)
    facts["line"] = derive_line(root, base)
    facts["matrix"] = derive_matrix(root)
    facts["plan"] = derive_plan(root)
    facts["docs"] = derive_docs(root)
    return facts


def moved_inputs(root: Path, base: str) -> list[str]:
    """Files the tool-derived rows read that differ between the base and the
    working tree. The matrix and the plan are derived from the working tree; a
    figure measured at the base is only the same figure while these agree."""
    p = _run(root, "git", "diff", "--name-only", base, "--", "src",
             "program/v3_3/REQUIREMENTS.json", "dogfood/p2-durability",
             "tools/capability_matrix.py", "tools/build_plan.py")
    return [x for x in p.stdout.splitlines() if x]


# --------------------------------------------------------------------------
# the rows
# --------------------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _statuses(c: dict, order=("PROVEN", "PARTIAL", "IMPLEMENTED_NOT_PROVEN",
                              "MISSING")) -> str:
    return " + ".join(f"{c.get(k, 0)} {k}" for k in order)


def build_rows(facts: dict | None) -> list[dict]:
    """The table. `facts` is None while only the static shape is needed."""
    f = facts or {}
    g = f.get("git", {})
    a = f.get("attribution", {})
    mx = f.get("matrix", {})
    pl = f.get("plan", {})
    dc = f.get("docs", {})
    ln = f.get("line", {})

    def d(label, fn):
        try:
            return fn()
        except (KeyError, TypeError):
            return f"<{label}: not derived>"

    first = sum(1 for _n, e in REFUSED if e in FIRST_COUNT_ENTRIES)
    later = len(REFUSED) - first

    p3 = mx.get("p3_p12", {})
    p3_total = sum(p3.values()) if p3 else 0

    rows: list[dict] = [
        dict(tag="V01", r=True, needles=[str(g.get("merges", ""))],
             figure=d("git", lambda: str(g["merges"])),
             what="product-run merges into the development mainline, "
                  + d("git", lambda: g["merges_first"]) + " to "
                  + d("git", lambda: g["merges_last"]),
             source="git history",
             regenerate="`git log <base> --since='2026-09-15 00:00:00 +0300' "
                        "--grep='^Merge VeriHarness run' "
                        "--grep='^Merge role-pane-lifecycle' "
                        "--grep='^Merge p217o' --grep='^Merge rbtf' "
                        "--grep='^Merge p243b' --format=%h | wc -l`"),
        dict(tag="V02", r=True, needles=[str(g.get("merges_src", ""))],
             figure=d("git", lambda: str(g["merges_src"])),
             what="of the V01 merges, those that change anything under "
                  "`src/hoh/`",
             source="git history",
             regenerate="for each V01 merge `m`: `git diff --quiet m^1 m -- "
                        "src/hoh` is false; count"),
        dict(tag="V03", r=False, needles=["11", "Eleven"],
             figure=d("git", lambda: str(g["candidates_imported"])),
             what="product-run candidates for P2 modules imported \"as the "
                  "product wrote it\" on 2026-09-24, before the merge-commit "
                  "convention",
             source="git history",
             regenerate="`git log <base> --since='2026-09-15 00:00:00 +0300' "
                        "--until='2026-09-25 00:00:00 +0300' --format=%s | "
                        "grep -c 'candidate as the product wrote it'`"),
        dict(tag="V05", r=False, needles=[],
             figure="2 iterations; 7 dispatches; 0 human interventions; 46 "
                    "tests; 416 lines",
             what="the first kernel capability built through the product's own "
                  "loop (P2-26, `src/hoh/checklist.py`): iteration 1 rejected, "
                  "iteration 2 accepted",
             source="ledger P2-26 entry", regenerate="read the entry; its "
             "evidence tree is named there",
             ledger=[("P2-26", "iteration 2   K1..K8 PASS, accepted, 7 "
                      "dispatches, 0 human interventions"),
                     ("P2-26", "46 tests pass, ruff clean, 416 lines")]),
        dict(tag="V06", r=True, needles=[str(a.get("nodes", ""))],
             figure=d("attribution", lambda: str(a["nodes"])),
             what="development nodes after the attribution anchor, with the "
                  "ledger as committed at the base",
             source="`tools/attribution.py`",
             regenerate="`python3 tools/attribution.py --json --ledger "
                        "<dogfood/ATTRIBUTION.json at the base>` -> "
                        "`development_nodes`"),
        dict(tag="V06a", r=True, needles=[str(a.get("through", ""))],
             figure=d("attribution", lambda: str(a["through"])),
             what="of V06, nodes executed through the shipped control plane "
                  "end to end, merge decision included",
             source="as V06", regenerate="-> `nodes_through_the_product`"),
        dict(tag="V06b", r=True, needles=[str(a.get("merged", ""))],
             figure=d("attribution", lambda: str(a["merged"])),
             what="of V06, nodes the product developed and the orchestrating "
                  "session merged after review (counted in neither V06a nor "
                  "V06c)",
             source="as V06", regenerate="-> "
             "`nodes_developed_by_the_product_merged_by_the_orchestrator`"),
        dict(tag="V06c", r=True, needles=[str(a.get("direct", ""))],
             figure=d("attribution", lambda: str(a["direct"])),
             what="of V06, the orchestrating session's direct work",
             source="as V06", regenerate="V06 minus V06a minus V06b"),
        dict(tag="V07", r=False, needles=[str(first)],
             figure=str(first),
             what="candidates accepted by the product and refused by "
                  "independent integration review, ledger entries O236 to "
                  "O251: " + _names(lambda e: e in FIRST_COUNT_ENTRIES),
             source="ledger (hand-kept list, each item verified)",
             regenerate="`python3 tools/docs_numbers.py check` verifies that "
                        "each entry names the candidate and records a refusal; "
                        "it cannot find a refusal the list omits, so a "
                        "reviewer recounts",
             refused=lambda e: e in FIRST_COUNT_ENTRIES),
        dict(tag="V07b", r=False, needles=[str(later)],
             figure=str(later),
             what="the same, recorded after the first count, in O252 to "
                  "O254: " + _names(lambda e: e not in FIRST_COUNT_ENTRIES),
             source="ledger (hand-kept list, each item verified)",
             regenerate="as V07",
             refused=lambda e: e not in FIRST_COUNT_ENTRIES),
        dict(tag="V07t", r=False, needles=[str(first + later)],
             figure=str(first + later),
             what="V07 plus V07b",
             source="as V07 and V07b", regenerate="V07 + V07b"),
        dict(tag="V08", r=False, needles=[str(g.get("merges_first_window", ""))],
             figure=d("git", lambda: str(g["merges_first_window"])),
             what="product-run merges in the window of V07's first count "
                  "(2026-09-25 to 2026-10-02)",
             source="git history",
             regenerate="as V01 with `--until='2026-10-03 00:00:00 +0300'` (a "
                        "bare date takes the current time of day and counts "
                        "more)"),
        dict(tag="V09", r=False, needles=[],
             figure="3",
             what="product acceptances over a defect the run's own QA had "
                  "recorded as an open gap: pproof, pregen, sarch",
             source="ledger O236, O240", regenerate="read the entries",
             ledger=[("O236", "QA had recorded the fragment escape as a HIGH "
                      "open"),
                     ("O236", "the **second** acceptance in this wave over a "
                      "QA-recorded defect"),
                     ("O240", "the third acceptance over a QA-recorded "
                      "defect")]),
        dict(tag="V10", r=True, needles=[str(mx.get("total", ""))],
             figure=d("matrix", lambda: f"{mx['total']} = "
                      f"{_statuses(mx['counts'])}"),
             what="requirements in the capability register, by derived status "
                  "(the board's row omits IMPLEMENTED_NOT_PROVEN)",
             source="`tools/capability_matrix.py`",
             regenerate="`python3 tools/capability_matrix.py --json` -> "
                        "`counts`"),
        dict(tag="V10b", r=True,
             needles=[str(sum(mx.get("p2", {}).values()) or "")],
             figure=d("matrix", lambda: str(sum(mx["p2"].values()))),
             what="P2 requirements in the register", source="as V10",
             regenerate="count `capabilities` with `target_phase == \"P2\"`"),
        dict(tag="V10c", r=True, needles=[str(mx.get("p2", {}).get("PROVEN", ""))],
             figure=d("matrix", lambda: f"{mx['p2'].get('PROVEN', 0)} PROVEN "
                      f"({mx['p2'].get('PARTIAL', 0)} PARTIAL, "
                      f"{mx['p2'].get('IMPLEMENTED_NOT_PROVEN', 0)} "
                      f"IMPLEMENTED_NOT_PROVEN, "
                      f"{mx['p2'].get('MISSING', 0)} MISSING)"),
             what="of V10b, by status", source="as V10",
             regenerate="as V10b, by status"),
        dict(tag="V10d", r=True, needles=[],
             figure=d("matrix", lambda: f"P2-42: {mx['p2_42']}"),
             what="the register status of P2-42 (registered ephemeral "
                  "temporary roots); the code is merged and the register "
                  "names its symbol with an equivalent, which holds the row "
                  "below PROVEN",
             source="as V10",
             regenerate="`python3 tools/capability_matrix.py --json` -> "
                        "`capabilities`, id `P2-42`"),
        dict(tag="V11", r=True, needles=[str(mx.get("p1", {}).get("PROVEN", ""))],
             figure=d("matrix", lambda: f"{mx['p1'].get('PROVEN', 0)} of "
                      f"{sum(mx['p1'].values())}"),
             what="P1 requirements PROVEN", source="as V10",
             regenerate="as V10b with `\"P1\"`"),
        dict(tag="V11b", r=True, needles=[str(p3.get("MISSING", ""))],
             figure=d("matrix", lambda: f"{p3.get('MISSING', 0)} of {p3_total} "
                      f"MISSING ({p3.get('PARTIAL', 0)} PARTIAL, "
                      f"{p3.get('PROVEN', 0)} PROVEN)"),
             what="requirements registered for P3 to P12, by status", source="as V10",
             regenerate="count `capabilities` whose `target_phase` is not P0, "
                        "P1 or P2, by status"),
        dict(tag="V12", r=True, needles=[str(pl.get("blocking", ""))],
             figure=d("plan", lambda: f"{pl['blocking']} blocking open; "
                      f"{pl['bound']} exit conditions bound to requirements "
                      f"not PROVEN; {pl['unmeasured']} exit conditions nothing "
                      f"measures yet; {pl['open']} open requirements, "
                      f"{pl['held']} behind a phase gate"),
             what="the P2 gate and the build plan", source="`tools/build_plan.py`",
             regenerate="`python3 tools/build_plan.py`"),
        dict(tag="V13", r=True, needles=["TECHNICALLY_STABLE_READY = "],
             figure=d("docs", lambda: f"TECHNICALLY_STABLE_READY = "
                      f"{dc['ready']}"),
             what="the committed board's verdict", source="`docs/READINESS.md`",
             regenerate="`python3 tools/readiness.py --write` (or "
                        "`tools/regenerate.py`) at the published commit, at "
                        "low load"),
        dict(tag="V14", r=False, needles=["13,040"],
             figure="13,040 run-state files; 90,934 `resume_attempts` "
                    "occurrences, all 0",
             what="the measurement that showed the quota block class had "
                  "never been recorded",
             source="ledger O232", regenerate="historical; read the entry",
             ledger=[("O232", "13,040"), ("O232", "90,934")]),
        dict(tag="V15", r=False, needles=["19", "20"],
             figure="7 scenarios; 19 targeted negative controls (at "
                    "`274cc27`); 20 of 20 caught (at `7c85d8f` and at "
                    "`0aa4d1f`); 44 retry timers all due at or after "
                    "`retry_after`",
             what="the end-to-end durability evidence",
             source="ledger O234, O237",
             regenerate="historical; the evidence directories are named in the "
                        "entries",
             ledger=[("O234", "274cc27"), ("O234", "all 19 targeted negative "
                      "controls caught"), ("O237", "20 of 20"),
                     ("O237", "7c85d8f"), ("O237", "0aa4d1f"),
                     ("O237", "44 ")]),
        dict(tag="V16", r=False, needles=["977", "74 GiB"],
             figure="977 GiB of a 1 TiB volume (the operator's copy of the WSL "
                    "disk); about 74 GiB of 653 GiB in the guest were "
                    "VeriHarness's; 52 GiB reviewer scratch",
             what="the storage incident of 2026-09-25", source="ledger O238",
             regenerate="historical",
             ledger=[("O238", "977 GiB"), ("O238", "74 GiB"),
                     ("O238", "653 GiB"), ("O238", "52 GiB")]),
        dict(tag="V17", r=False, needles=["3,861", "3,382"],
             figure="3,861 `git status` entries; 3,382 files of single-copy "
                    "evidence",
             what="the interrupted working tree that was preserved",
             source="ledger O238", regenerate="historical",
             ledger=[("O238", "3,861"), ("O238", "3,382")]),
        dict(tag="V18", r=False, needles=["17"],
             figure="17",
             what="files that conflicted when the preserved state was merged "
                  "with the storage line",
             source="ledger O250", regenerate="historical",
             ledger=[("O250", "Seventeen files")]),
        dict(tag="V19", r=False, needles=[],
             figure="124 paths, 29.07 GiB; guest free 434.82 -> 463.78 GiB",
             what="the operator-approved, pre-product reclamation of list A",
             source="ledger O239", regenerate="historical",
             ledger=[("O239", "124 paths"), ("O239", "29.07"),
                     ("O239", "434.82"), ("O239", "463.78")]),
        dict(tag="V20", r=False, needles=[],
             figure="3 runs; 3500 passed, 5 failed on the merge (the same five "
                    "fail on its base)",
             what="the storage steward (STORAGE-D)", source="ledger O249",
             regenerate="historical",
             ledger=[("O249", "3500 passed, 5 failed")]),
        dict(tag="V21a", r=False, needles=["116,592"],
             figure="116,592 bytes",
             what="the steward specification carried by the prompt that hit "
                  "the argument limit", source="ledger O251",
             regenerate="historical", ledger=[("O251", "116,592")]),
        dict(tag="V21b", r=False, needles=["154,480"], figure="154,480 bytes",
             what="a developer prompt delivered through a prompt file, live",
             source="ledger O251", regenerate="historical",
             ledger=[("O251", "154,480")]),
        dict(tag="V21c", r=False, needles=["122,734"], figure="122,734 bytes",
             what="a planner prompt delivered through a prompt file, live",
             source="ledger O251", regenerate="historical",
             ledger=[("O251", "122,734")]),
        dict(tag="V22", r=False, needles=["two iterations"],
             figure="2 iterations lost, accepted in iteration 3",
             what="the project-adopt run's dispatch losses (stall, then "
                  "leftover input read as a slash command)",
             source="ledger O252", regenerate="historical",
             ledger=[("O252", "iterations 1 and 2 were lost to a dispatch "
                      "stall"), ("O252", "iteration 3")]),
        dict(tag="V23", r=False, needles=["six"],
             figure="6",
             what="reproducible scratch files with a non-UTF-8 name moved out "
                  "of an arena, with a manifest",
             source="ledger O249", regenerate="historical",
             ledger=[("O249", "The six reproducible scratch files")]),
        dict(tag="V25", r=False, needles=["145 MB"],
             figure="Node 24, pnpm 11, 499 packages; 145 MB written into the "
                    "review clone's `.git`; one 9 KB pack fetched into the "
                    "study clone",
             what="the Whiteboard shadow pilot", source="ledger O251",
             regenerate="historical",
             ledger=[("O251", "Node 24"), ("O251", "pnpm 11"),
                     ("O251", "499 packages"), ("O251", "145 MB")]),
        dict(tag="V26", r=False, needles=["eight", "8"],
             figure="8 iterations (wmig, kbr, ugt, pcon, dres2, ecl and rpl in "
                    "iteration 1; lgs in iteration 2); first recorded as 5",
             what="iterations lost to a provider usage limit that stopped QA "
                  "and was booked as a rejection",
             source="ledger O252 and its addition", regenerate="historical",
             ledger=[("O252", "seven instances, not five"),
                     ("O252", "dres2 iteration 1"),
                     ("O255", "rpl iteration 1"),
                     ("O255", "count is **eight**")]),
        dict(tag="V28", r=False, needles=["four", "4"],
             figure="4 invisible characters (U+200E twice, U+200B, U+00AD); a "
                    "57,578-byte planner prompt",
             what="the specification that stalled two planner dispatches",
             source="ledger O252", regenerate="historical",
             ledger=[("O252", "four invisible characters"),
                     ("O252", "57,578-byte")]),
        dict(tag="V29", r=False, needles=["2700"],
             figure="2700 s default role timeout; relaunched with 5400 s",
             what="the run blocked while its developer still worked",
             source="ledger O252", regenerate="historical",
             ledger=[("O252", "2700 s"), ("O252", "--role-timeout 5400")]),
        dict(tag="V30", r=False, needles=["120"],
             figure="120-second limit of the agent's shell tool",
             what="the limit a developer's mutation command exceeded before "
                  "its turn was taken as ended",
             source="ledger O252", regenerate="historical",
             ledger=[("O252", "120-s Bash limit")]),
        dict(tag="V31", r=False, needles=["seven"],
             figure="7 first prompts stalled in 4 runs, each recovered",
             what="first prompts that stalled in the runs pinned on or after "
                  "the stalled-submit merge, none ending blocked",
             source="ledger O253", regenerate="historical",
             ledger=[("O253", "seven first prompts stalled")]),
        dict(tag="V32", r=False, needles=[],
             figure="5 s after `herdr pane close`: the agent and its shell "
                    "gone, the background process alive",
             what="the live smoke of closing a pane (the orchestrator's own "
                  "tab)",
             source="ledger O253", regenerate="historical",
             ledger=[("O253", "after five seconds")]),
        dict(tag="V33", r=False, needles=["188"],
             figure="0 of 188 roots eligible (survey about 3.4 h, detached; "
                    "two earlier attempts cut off at the 2-h background limit)",
             what="the legacy-scratch survey of Section P",
             source="ledger O252", regenerate="historical",
             ledger=[("O252", "0 of 188"), ("O252", "3.4 h")]),
        dict(tag="V33b", r=False, needles=["157", "98"],
             figure="157 committed briefs designate 98 of the 188 roots",
             what="roots designated by accident by committed briefs under "
                  "`dogfood/`",
             source="ledger O252", regenerate="historical",
             ledger=[("O252", "157 pre-existing committed briefs"),
                     ("O252", "98 of the 188")]),
        dict(tag="V34", r=False, needles=["53", "195"],
             figure="53 planner answers replayed; 195 deviating checks in 42 "
                    "answers",
             what="the criterion-fidelity replay", source="ledger O252",
             regenerate="historical",
             ledger=[("O252", "a replay of 53 planner answers"),
                     ("O252", "195 deviating checks in 42 answers")]),
        dict(tag="V35", r=False, needles=["67,107,099"],
             figure="67,107,099 of 67,108,864 inodes used",
             what="the inode exhaustion of 2026-10-05", source="ledger O252",
             regenerate="historical",
             ledger=[("O252", "67,107,099 of 67,108,864")]),
        dict(tag="V36", r=False, needles=["4,211", "15,558,933"],
             figure="4,211 trees; 15,558,933 entries",
             what="the first cleanup of the orchestrator's own reproducible "
                  "scratch", source="ledger O252", regenerate="historical",
             ledger=[("O252", "4,211"), ("O252", "15,558,933")]),
        dict(tag="V37", r=False, needles=["6,562,367"],
             figure="2,003 copies inventoried; 342 proven reproducible; 341 "
                    "removed; 6,562,367 entries; 37,598,417,431 bytes; 1,661 "
                    "kept",
             what="the second cleanup", source="ledger O253",
             regenerate="historical",
             ledger=[("O253", "2,003 copies inventoried"),
                     ("O253", "342 proven reproducible"),
                     ("O253", "341 removed"), ("O253", "6,562,367"),
                     ("O253", "37,598,417,431"), ("O253", "1,661")]),
        dict(tag="V38", r=False, needles=["P2-42"],
             figure="3 runs (et, et2, et3), 2 refused; merged as `b7899bc8`",
             what="the P2-42 strand", source="ledger O253; git history",
             regenerate="`git log -1 --format=%s b7899bc8`",
             ledger=[("O253", "Run `20261006t1628z-et`")],
             commit=("b7899bc8", "P2-42")),
        dict(tag="V39", r=False, needles=["10", "5", "4"],
             figure="about 10 (2026-10-04), then about 5 (2026-10-05), "
                    "retired (2026-10-06); work-item first: an upper bound of "
                    "4 strands and 2 agents per strand; restart at 4 to 6 "
                    "weighted loads",
             what="the provider-parallelism policy",
             source="ledger O252, O253", regenerate="historical",
             ledger=[("O252", "soft target of about 10"),
                     ("O252", "4-6 weighted workloads"),
                     ("O252", "soft target of about 5"),
                     ("O253", "are retired"),
                     ("O253", "an upper bound of 4 independent strands and 2 "
                      "agents per strand")]),
        dict(tag="V40", r=True, needles=["MISSING"],
             figure=d("matrix", lambda: f"{len(mx['a5'])} requirements: "
                      + ", ".join(f"{i} {s}" for i, s in mx["a5"].items())),
             what="amendment A5's requirements, one per phase",
             source="as V10",
             regenerate="`python3 tools/capability_matrix.py --json` -> "
                        "`capabilities`, ids P2-43, P3-41, P10-17, P11-02, "
                        "P12-06",
             expects=("all MISSING", lambda fx: all(
                 s == "MISSING" for s in fx["matrix"]["a5"].values()))),
        dict(tag="V43", r=False, needles=["75"],
             figure="75% of the weekly limit, 2026-10-05",
             what="the provider burn signal", source="ledger O252",
             regenerate="historical",
             ledger=[("O252", "75% of your weekly limit")]),
        dict(tag="V44", r=False, needles=["15", "12"],
             figure="15 writers migrated (W1-W15); 12 remain on the list",
             what="disk-full-safe durable writes (P2-41)", source="ledger O252",
             regenerate="historical",
             ledger=[("O252", "W1-W15"), ("O252", "twelve writers")]),
        dict(tag="V45", r=True, needles=[str(dc.get("limit_headings", ""))],
             figure=d("docs", lambda: f"{dc['limit_headings']} numbered "
                      f"headings"),
             what="numbered headings of `docs/LIMITATIONS.md`",
             source="`docs/LIMITATIONS.md`",
             regenerate="count lines matching `^## [0-9]`"),
        # By addition, 2026-10-09 (ledger O263): the duration, the autonomy and
        # the scale of the whole development line, which the v0.1.0 campaign
        # figures ("five calendar days; 3.5 days elapsed") do not describe.
        dict(tag="V46", r=True,
             needles=[f"{ln['elapsed_days']} days"] if ln else [],
             figure=d("line", lambda: f"{ln['first']} to {ln['last']} (+0300): "
                      f"{ln['elapsed_days']} days {ln['elapsed_hours']} h "
                      f"({_plural(ln['elapsed_days'] // 7, 'week')} "
                      f"{_plural(ln['elapsed_days'] % 7, 'day')})"),
             what="first and latest commit of the development mainline, and "
                  "the time between them",
             source="git history",
             regenerate="`git log <base> --format=%cI` (earliest and latest "
                        "commit date, read at +0300); the difference in days "
                        "and whole hours"),
        dict(tag="V47", r=True,
             needles=[f"{ln['commit_days']} of the {ln['calendar_days']}",
                      f"{ln['commit_days']} of {ln['calendar_days']}"]
             if ln else [],
             figure=d("line", lambda: f"{ln['commit_days']} of "
                      f"{ln['calendar_days']} calendar days carry a commit; "
                      + (("none on " + " or ".join(
                          f"{a} to {b} ({_plural(n, 'day')})"
                          for a, b, n in ln["gaps"])) if ln["gaps"]
                         else "no day without a commit")
                      + f"; longest run of days with a commit: "
                      f"{ln['streak'][0]} ({ln['streak'][1]} to "
                      f"{ln['streak'][2]})"),
             what="calendar days (commit date at +0300, first to latest day "
                  "inclusive) with at least one commit, the commit-free runs "
                  "and the longest run of consecutive days with a commit",
             source="git history",
             regenerate="`git log <base> --format=%cI`; the distinct calendar "
                        "days, the gaps between them and the longest run of "
                        "consecutive days"),
        dict(tag="V48", r=True, needles=[f"{ln['commits']:,}"] if ln else [],
             figure=d("line", lambda: f"{ln['commits']:,} commits = "
                      f"{ln['non_merge']} non-merge + {ln['merges']} merges"),
             what="commits reachable from the base",
             source="git history",
             regenerate="`git rev-list --count <base>`; with `--merges` for "
                        "the merges"),
        dict(tag="V49", r=True,
             needles=[str(ln["ledger_entries"][0])]
             if ln.get("ledger_entries") else [],
             figure=d("line", lambda: f"{ln['ledger_entries'][0]} numbered "
                      f"entries, O{ln['ledger_entries'][1]} to "
                      f"O{ln['ledger_entries'][2]}"),
             what="distinct ledger entry numbers that head an entry "
                  "(`## On` or `### On`) in the working ledger as committed "
                  "at the base; O1 to O22 are numbered open items inside the "
                  "first entries, not entry headings",
             source="`DOGFOOD_LEDGER.md` at the base",
             regenerate="`git show <base>:DOGFOOD_LEDGER.md`; count the "
                        "distinct numbers in lines matching `^#{2,3} O[0-9]+`"),
        dict(tag="V50", r=False, needles=["117 run directories"],
             figure="117 run directories, 2026-10-01 to 2026-10-09",
             what="run directories under the project's runs directory, from "
                  "the first (2026-10-01 10:22Z) to the latest (2026-10-09 "
                  "07:29Z); runs of earlier days were kept elsewhere and are "
                  "not counted (taken on 2026-10-09 at 16:40 +0300; the count "
                  "grows with every run)",
             source="ledger O263",
             regenerate="`ls <state root>/runs` and count the names ending in "
                        "`-runs`; historical once taken",
             ledger=[("O263", "117 `*-runs` directories")]),
        dict(tag="V51", r=False, needles=["459 first-round"],
             figure="459 first-round reviewer reports (771 report files in "
                    "all, including re-review rounds), 2026-09-30 to "
                    "2026-10-09",
             what="reports of the spec and integration reviewers of the build: "
                  "first round = files named `*-report.md` minus the "
                  "re-review files named `*-rN-report.md`; all = files whose "
                  "name contains `report` and ends in `.md`, re-review rounds "
                  "(`-rN-report.md`, `-report-rN.md`) and parked copies "
                  "included (taken on 2026-10-09 at 17:57 +0300, the third "
                  "taking of this count; the counts grow with every review)",
             source="ledger O263",
             regenerate="count the files named `*-report.md`, subtract those "
                        "named `*-rN-report.md`; and count the files matching "
                        "`*report*.md`, in the build's review directory; "
                        "historical once taken",
             ledger=[("O263", "459 first-round reviewer reports (771 report "
                      "files in all, including re-review rounds)")]),
        dict(tag="V52", r=True, needles=["CAPTAIN"],
             figure=d("attribution", lambda: f"{a['captain']} of "
                      f"{a['entries']} attribution entries are of category "
                      "CAPTAIN (a human decision or a human-authored change)"),
             what="entries of `dogfood/ATTRIBUTION.json` at the base booked to "
                  "a human; the file is declared by the orchestrating session "
                  "and checked for tiling and for the run states it cites, "
                  "not for authorship",
             source="`dogfood/ATTRIBUTION.json` at the base",
             regenerate="`git show <base>:dogfood/ATTRIBUTION.json`; count "
                        "the entries whose `category` is `CAPTAIN`"),
        dict(tag="V53", r=False,
             needles=["6 operator directives", "6 directives"],
             figure="6 operator directives stored verbatim, 2026-09-30 to "
                    "2026-10-09",
             what="amendments A1 to A6 of the programme, each with the "
                  "operator's own words in a file of its own",
             source="ledger O263",
             regenerate="`git ls-tree -r --name-only <base> "
                        "program/v3_3/amendments` and count the files named "
                        "`*_OPERATOR_DIRECTIVE_*`",
             ledger=[("O263", "6 verbatim operator directives")]),
        dict(tag="V54", r=False,
             needles=["3 acknowledged hand-overs", "handed over 3 times"],
             figure="4 orchestrator sessions in a row; 3 acknowledged "
                    "hand-overs (2026-10-07, 2026-10-08, 2026-10-09); a fourth "
                    "hand-over file (2026-10-08) was written before a machine "
                    "shutdown and not used",
             what="orchestrator sessions since the written hand-over protocol "
                  "began on 2026-10-07; earlier sessions are not itemised",
             source="ledger O263",
             regenerate="list the hand-over and acknowledgement files of the "
                        "build's hand-over directory; historical",
             ledger=[("O263", "4 orchestrator sessions"),
                     ("O263", "3 acknowledged hand-overs")]),
        dict(tag="V55", r=False, needles=["13 stops", "13 quota stops"],
             figure="13 quota stops (15 markers), 2026-10-03 to 2026-10-07",
             what="stops of provider work at the account's usage limit, as "
                  "the orchestrator's running notes mark them; two pairs of "
                  "markers describe one stop each",
             source="ledger O263",
             regenerate="count the lines of the running notes that open with "
                        "`STATE AT USAGE LIMIT` or `STATE AT WEEKLY LIMIT`; "
                        "historical",
             ledger=[("O263", "13 quota stops (15 markers)")]),
        dict(tag="V56", r=False, needles=["power cut"],
             figure="power cut 2026-09-10; disk full 2026-09-25; inode "
                    "exhaustion 2026-10-05; operator-requested WSL shutdown "
                    "2026-10-08 (about one hour); inode pressure 2026-10-09 "
                    "(the storage guard at CRITICAL on the inode axis parked "
                    "two runs until 11:36Z)",
             what="the other interruptions the ledger and the notes record",
             source="ledger O263, O88, O238, O252, O258, O262",
             regenerate="read the entries; historical",
             ledger=[("O263", "power cut of 2026-09-10"),
                     ("O263", "operator-requested WSL shutdown of 2026-10-08"),
                     ("O263", "two runs parked on storage pressure until "
                      "11:36Z")]),
    ]
    for row in rows:
        row["needles"] = [n for n in row["needles"] if n]
        row.setdefault("ledger", [])
    return rows


def _names(pred) -> str:
    by: dict[str, list[str]] = {}
    for name, entry in REFUSED:
        if pred(entry):
            by.setdefault(entry, []).append(name)
    return "; ".join(f"{', '.join(v)} ({k})" for k, v in by.items())


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _source_cell(row: dict, ledger_lines: list[str] | None) -> str:
    src = row["source"]
    if ledger_lines is None or not row["ledger"]:
        return src
    where: dict[str, int] = {}
    for entry, needle in row["ledger"]:
        ln = ledger_find(ledger_lines, entry, needle)
        if ln is not None and entry not in where:
            where[entry] = ln
    if not where:
        return src
    pos = "; ".join(f"{e} line {n}" for e, n in where.items())
    return f"{src} ({pos})"


def render(root: Path, base: str, facts: dict | None = None) -> str:
    facts = facts if facts is not None else derive_all(root, base)
    rows = build_rows(facts)
    try:
        ledger_lines: list[str] | None = _load_ledger(root)
    except Gap:
        ledger_lines = None
    subject = _git(root, "log", "-1", "--format=%s", base).strip()
    cdate = _git(root, "log", "-1", "--format=%ci", base).strip()
    out = [
        "# VeriHarness -- Numbers, second edition (pre-release v0.3.0a2)",
        "",
        "<!-- Generated by `python3 tools/docs_numbers.py render`; "
        "`python3 tools/docs_numbers.py check` compares this file with a "
        "fresh derivation. Do not edit it by hand. -->",
        "",
        "Every figure that `paper/POSITION_PAPER_V2.md` tags, and every "
        "development figure that `README.md`, `RELEASE_NOTES.md` (and the "
        "`CHANGELOG.md` section generated from it), `docs/LIMITATIONS.md` and "
        "the dated errata of `paper/POSITION_PAPER.md` state, with its source "
        "and the way to take it again. The first edition, "
        "`paper/NUMBERS.md`, covers `paper/POSITION_PAPER.md` and the "
        "`v0.1.0` campaign and keeps its own dated erratum.",
        "",
        f"Base commit: `{base}`",
        "",
        f"The base is the commit named above ({subject}; committed {cdate}). "
        "Dates are the commits' own (`+0300`); give `git log` explicit times "
        "with that offset, because a bare date is read with the current time "
        "of day. A figure derived by a tool is derived from the working tree "
        "except where the row says it reads the base: the tool refuses to "
        "check a table whose base differs from the working tree in the files "
        "those derivations read.",
        "",
        "**The R column.** `R` marks a figure about the moving present: a "
        "count over history up to the base, a status in the capability "
        "register, a row of the board. Every `R` row must be regenerated at "
        "the publication checkpoint, against the exact commit being "
        "published (`python3 tools/docs_numbers.py render --base <rev>`, then "
        "adjust the prose that `check` names). An unmarked row is a dated "
        "historical measurement: it is checked against its source, not "
        "re-measured.",
        "",
        "**Sources.** `ledger Onnn, line L` is the development repository's "
        "working ledger, which the public export does not carry; the line "
        "number is read from the ledger when this table is rendered, so the "
        "entry number is the stable part. A command is run from the "
        "repository root.",
        "",
        "| tag | figure | what it is | source | regenerate | R |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        def esc(x: str) -> str:
            return x.replace("|", "\\|")

        out.append("| {tag} | {figure} | {what} | {source} | {regen} | {r} |"
                   .format(tag=row["tag"], figure=esc(row["figure"]),
                           what=esc(row["what"]),
                           source=esc(_source_cell(row, ledger_lines)),
                           regen=esc(row["regenerate"]),
                           r="R" if row["r"] else ""))
    out += [
        "",
        "**Figures outside the paper.** The development figures in "
        "`README.md`, `RELEASE_NOTES.md`, `CHANGELOG.md` and "
        "`docs/LIMITATIONS.md` carry the same `[Vnn]` tags as the paper and "
        "resolve to the rows above. Identifiers that carry digits -- phase "
        "and requirement ids such as P2-41, ledger entry numbers, stressor "
        "ids such as DS2 -- are names, not measurements, and are not listed.",
        "",
        "**Known gaps in this table.** Rows V07 and V07b are a hand-kept "
        "list: the tool verifies that each listed entry names its candidate "
        "and records a refusal, and that the stated counts equal the list, "
        "but it cannot find a refusal the list omits. A reviewer recounts "
        "them. Rows that carry a ledger source are verified against the "
        "ledger entry's text, not re-measured. V13 reads the committed "
        "board, which is regenerated separately and may be older than the "
        "base.",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def check_ledger_rows(rows: list[dict], ledger_lines: list[str]) -> list[str]:
    problems = []
    for row in rows:
        for entry, needle in row["ledger"]:
            if ledger_find(ledger_lines, entry, needle) is None:
                problems.append(f"{row['tag']}: ledger entry {entry} does not "
                                f"contain {needle!r}")
    return problems


def check_refusals(ledger_lines: list[str]) -> list[str]:
    problems = []
    seen = set()
    for name, entry in REFUSED:
        if (name, entry) in seen:
            problems.append(f"refusal list: {name} ({entry}) is listed twice")
        seen.add((name, entry))
        span = _entry_section(ledger_lines, entry)
        if span is None:
            problems.append(f"refusal list: no ledger entry {entry}")
            continue
        word = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w])")
        ok = any(word.search(ledger_lines[i]) and
                 REFUSAL_MARKER.search(ledger_lines[i])
                 for i in range(span[0], span[1]))
        if not ok:
            problems.append(f"refusal list: ledger entry {entry} has no line "
                            f"that names {name} and records a refusal")
    return problems


def _paragraphs(text: str):
    para: list[str] = []
    start = 1
    for i, line in enumerate(text.split("\n"), start=1):
        if line.strip() == "":
            if para:
                yield start, "\n".join(para)
                para = []
            continue
        if not para:
            start = i
        para.append(line)
    if para:
        yield start, "\n".join(para)


def check_tags(root: Path, rows: list[dict], facts: dict | None) -> list[str]:
    """Every tag names a row; the figure a tag stands for appears in at least
    one paragraph of each document that uses the tag; every row is used."""
    problems = []
    by_tag = {r["tag"]: r for r in rows}
    used: set[str] = set()
    for rel in TAGGED_DOCS:
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        if rel == "RELEASE_NOTES.md":
            # only the section of the pre-release carries tags
            cut = text.find("\n# Release notes -- v0.1.0\n")
            text = text[:cut] if cut != -1 else text
        seen: dict[str, tuple[int, bool]] = {}
        for start, para in _paragraphs(text):
            tags = TAG_RE.findall(para)
            if not tags:
                continue
            flat = re.sub(r"\s+", " ", para)
            for tag in tags:
                row = by_tag.get(tag)
                if row is None:
                    problems.append(f"{rel}:{start}: tag [{tag}] names no row "
                                    "of the table")
                    continue
                used.add(tag)
                first, ok = seen.get(tag, (start, False))
                needles = row["needles"]
                ok = ok or not needles or any(n in flat for n in needles)
                seen[tag] = (first, ok)
                exp = row.get("expects")
                if exp and facts is not None:
                    phrase, holds = exp
                    if phrase in flat and not holds(facts):
                        problems.append(
                            f"{rel}:{start}: the text says {phrase!r} for "
                            f"[{tag}] but the derived figure is "
                            f"{row['figure']!r}")
        for tag, (first, ok) in seen.items():
            if not ok:
                problems.append(
                    f"{rel}:{first}: tag [{tag}] stands for "
                    f"{by_tag[tag]['figure']!r} but no paragraph of this "
                    f"document that carries it has any of "
                    f"{by_tag[tag]['needles']}")
    for tag in by_tag:
        if tag not in used:
            problems.append(f"row {tag} is used by no document")
    return problems


def unmerged_check(root: Path, base: str) -> list[str]:
    out = _git(root, "log", base, f"--since={FIRST_WINDOW_START}",
               "--grep=^Merge VeriHarness run", "--format=%h%x09%s")
    problems = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        low = subject.lower()
        for strand in UNMERGED_STRANDS:
            if strand in low:
                problems.append(
                    f"the documents say the {strand} strand is not merged, "
                    f"but {sha} ({subject[:70]}) is a product-run merge that "
                    "names it")
    return problems


def gate_check(root: Path, base: str) -> list[str]:
    p = _run(root, "git", "merge-base", "--is-ancestor", INTEGRITY_GATE_COMMIT,
             base)
    if p.returncode != 0:
        return [f"integrity gate: {INTEGRITY_GATE_COMMIT} (the merge of the "
                f"qaw-followup strand) is not an ancestor of {base[:12]}; "
                "the follow-up is necessary for the gate and does not meet it "
                "alone"]
    return []


def audit_block(rows: list[dict], verdicts: dict[str, tuple[str, str]]) -> str:
    out = [AUDIT_BEGIN, "",
           "### Mechanical audit of every figure (generated)", "",
           "One row per tag of `paper/NUMBERS_V2.md`. `RESOLVED` means a "
           "tool-derived figure equals the table at the base, or every ledger "
           "text the row relies on is present in its entry. A row that cannot "
           "be checked in a tree without the ledger or the git history reads "
           "`NOT_RUN`, which is never a pass.", "",
           "| tag | kind | verdict | method |", "|---|---|---|---|"]
    for row in rows:
        verdict, method = verdicts.get(row["tag"], ("NOT_RUN", "not checked"))
        kind = ("derived by a tool" if row["r"] else
                "ledger text" if row["ledger"] else "git or table")
        out.append(f"| {row['tag']} | {kind} | {verdict} | {method} |")
    out += ["", AUDIT_END]
    return "\n".join(out)


def row_verdicts(rows: list[dict], problems: list[str],
                 have_ledger: bool, have_git: bool) -> dict:
    bad = set(re.findall(r"\bV\d+[a-z]?\b", " ".join(problems)))
    out = {}
    for row in rows:
        tag = row["tag"]
        if tag in bad:
            out[tag] = ("MISMATCH", "see `python3 tools/docs_numbers.py check`")
        elif row["r"]:
            out[tag] = (("RESOLVED", "re-derived by the tool at the base")
                        if have_git else ("NOT_RUN", "no git history"))
        elif row["ledger"]:
            out[tag] = (("RESOLVED", "ledger text present in the entry")
                        if have_ledger else ("NOT_RUN", "no ledger in this tree"))
        else:
            out[tag] = (("RESOLVED", "derived from git history and compared")
                        if have_git else ("NOT_RUN", "no git history"))
    return out


def splice_audit(text: str, block: str) -> str:
    if AUDIT_BEGIN in text and AUDIT_END in text:
        i = text.index(AUDIT_BEGIN)
        j = text.index(AUDIT_END) + len(AUDIT_END)
        return text[:i] + block + text[j:]
    return text.rstrip("\n") + "\n\n" + block + "\n"


def recorded_base(root: Path) -> str | None:
    p = root / NUMBERS
    if not p.is_file():
        return None
    m = BASE_RE.search(p.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _blank(text: str, tags) -> str:
    for tag in tags:
        text = re.sub(r"(?m)^\| " + re.escape(tag) + r" \|.*$",
                      f"| {tag} | (skipped) |", text)
    return text


def run_check(root: Path, skip=frozenset()) -> tuple[int, list[str], dict]:
    """Verify the table, the tags and the audit block. `skip` names rows that
    are left out of the comparisons: the board's own run of this check skips
    the row that reads the board, which would otherwise make the board's verdict
    depend on a check the board itself runs."""
    base = recorded_base(root)
    if base is None:
        return 1, [f"{NUMBERS} is missing or names no base commit"], {}
    problems: list[str] = []
    notes: list[str] = []
    try:
        base = resolve_base(root, base)
    except Gap as exc:
        return 3, [f"environment gap: {exc}"], {}
    moved = moved_inputs(root, base)
    if moved:
        problems.append(
            "the working tree differs from the base in files the derived rows "
            f"read ({', '.join(moved[:4])}...); render the table with a new "
            "--base")
    try:
        facts = derive_all(root, base)
    except Gap as exc:
        return 3, [f"environment gap: {exc}"], {}
    rows = build_rows(facts)
    have_ledger = True
    try:
        ledger_lines = _load_ledger(root)
    except Gap as exc:
        have_ledger = False
        ledger_lines = []
        notes.append(f"NOT_RUN: ledger rows ({exc})")
    if have_ledger:
        problems += check_ledger_rows(rows, ledger_lines)
        problems += check_refusals(ledger_lines)
    try:
        fresh = render(root, base, facts)
        current = (root / NUMBERS).read_text(encoding="utf-8")
        if have_ledger and _blank(fresh, skip) != _blank(current, skip):
            problems.append(f"{NUMBERS} is not what `render` produces at the "
                            "recorded base; run `python3 tools/docs_numbers.py "
                            "render`")
    except Gap as exc:
        notes.append(f"NOT_RUN: render comparison ({exc})")
    problems += check_tags(root, rows, facts)
    problems += gate_check(root, base)
    problems += unmerged_check(root, base)
    verdicts = row_verdicts(rows, problems, have_ledger, True)
    audit_path = root / AUDIT
    if audit_path.is_file():
        block = audit_block(rows, verdicts)
        text = audit_path.read_text(encoding="utf-8")
        if AUDIT_BEGIN not in text or _blank(splice_audit(text, block),
                                              skip) != _blank(text, skip):
            if have_ledger:
                problems.append(f"{AUDIT} carries a stale generated block; run "
                                "`python3 tools/docs_numbers.py audit --write`")
    return (1 if problems else 0), problems + notes, facts


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("derive", "render"):
        s = sub.add_parser(name)
        s.add_argument("--base", default=None,
                       help="commit the figures are measured at (default: the "
                            "base recorded in the table)")
        if name == "derive":
            s.add_argument("--json", action="store_true")
    s = sub.add_parser("audit")
    s.add_argument("--write", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--skip", action="append", default=[], metavar="TAG",
                   help="leave a row out of the comparisons (the board's own "
                        "run skips the row that reads the board)")
    ap.add_argument("--root", type=Path, default=HOH, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = args.root.resolve()

    try:
        if args.cmd == "check":
            code, lines, _facts = run_check(root, frozenset(args.skip))
            for line in lines:
                print(line)
            if code == 0:
                print("OK: every figure re-derived or found in its ledger "
                      "entry; every tag names a row and its figure")
            return code
        rev = getattr(args, "base", None) or recorded_base(root)
        if args.cmd in ("derive", "render"):
            if rev is None:
                print("no base: pass --base REV", file=sys.stderr)
                return 2
            base = resolve_base(root, rev)
            if args.cmd == "derive":
                facts = derive_all(root, base)
                if args.json:
                    print(json.dumps(facts, indent=2, sort_keys=True))
                else:
                    for row in build_rows(facts):
                        print(f"{row['tag']:<5} {row['figure']}")
                return 0
            text = render(root, base)
            (root / NUMBERS).write_text(text, encoding="utf-8")
            print(f"wrote {NUMBERS} at base {base[:12]}")
            return 0
        if args.cmd == "audit":
            base = recorded_base(root)
            if base is None:
                print(f"{NUMBERS} names no base", file=sys.stderr)
                return 2
            base = resolve_base(root, base)
            facts = derive_all(root, base)
            rows = build_rows(facts)
            problems = check_tags(root, rows, facts)
            have_ledger = True
            try:
                lines = _load_ledger(root)
                problems += check_ledger_rows(rows, lines)
                problems += check_refusals(lines)
            except Gap:
                have_ledger = False
            verdicts = row_verdicts(rows, problems, have_ledger, True)
            block = audit_block(rows, verdicts)
            path = root / AUDIT
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            new = splice_audit(text, block)
            if args.write:
                path.write_text(new, encoding="utf-8")
                print(f"wrote the generated block of {AUDIT}")
            else:
                print(block)
            return 0
    except Gap as exc:
        print(f"environment gap: {exc}", file=sys.stderr)
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
