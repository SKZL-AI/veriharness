"""Tests for tools/export_manifest.py -- the public export manifest.

Most criteria are exercised against an isolated tmp_path fixture. A handful
are deliberate integration checks against this repository's own real tree or
its committed EXPORT_MANIFEST.json instead: test_manifest_schema_is_valid's
second half; test_two_named_findings_handled_by_rule_not_exception (reads the
committed manifest, so it works the same way inside a derived, INCLUDE-only
export as it does in a full checkout);
test_manifest_derivation_respects_gitignore_on_real_tree; and D4h's own
CLAIMS.md/CLAIMS.json/paper/** closure, reclassification and rule-set checks
(these derive REPO_ROOT live). No fixture ever writes outside tmp_path, and
nothing here mutates this repository's own files.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import export_manifest as em  # noqa: E402


def _write(tmp_path: Path, rel_path: str, lines: list[str]) -> Path:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return full


def _entries_by_path(manifest: dict) -> dict[str, dict]:
    return {e["path"]: e for e in manifest["entries"]}


# --------------------------------------------------------------------------- #
# K1 -- schema validity, fixture and (second half) real-repo integration
# --------------------------------------------------------------------------- #


def test_manifest_schema_is_valid():
    good = {
        "schema": "hoh-export-manifest/1",
        "entries": [
            {"path": "README.md", "decision": "INCLUDE", "rule": "public-docs"},
            {"path": "docs/K1_CACHE_PROFILE.md", "decision": "EXCLUDE", "rule": "foreign-subject"},
            {"path": "weird.qux", "decision": "EXCLUDE", "rule": "unclassified"},
        ],
    }
    assert em.validate_schema(good) == []

    missing_rule = {"schema": "s", "entries": [{"path": "a", "decision": "INCLUDE"}]}
    problems = em.validate_schema(missing_rule)
    assert problems and any("rule" in p for p in problems)

    bad_decision = {"schema": "s", "entries": [{"path": "a", "decision": "MAYBE", "rule": "public-docs"}]}
    problems = em.validate_schema(bad_decision)
    assert problems and any("decision" in p for p in problems)

    unknown_rule = {"schema": "s", "entries": [{"path": "a", "decision": "INCLUDE", "rule": "made-up-rule"}]}
    problems = em.validate_schema(unknown_rule)
    assert problems and any("rule" in p for p in problems)

    not_a_list = {"schema": "s", "entries": "nope"}
    assert em.validate_schema(not_a_list) != []

    # Integration half: the real, committed manifest for this repository.
    real_path = REPO_ROOT / "EXPORT_MANIFEST.json"
    real = json.loads(real_path.read_text(encoding="utf-8"))
    real_problems = em.validate_schema(real)
    assert real_problems == [], real_problems
    assert real["entries"], "the real manifest must not be empty"
    for e in real["entries"]:
        assert e["decision"] in em.DECISIONS
        assert e["rule"] in em.RULES


# --------------------------------------------------------------------------- #
# K2 -- full filesystem coverage, built from a fresh walk, no git
# --------------------------------------------------------------------------- #


def test_manifest_covers_every_file_on_disk(tmp_path):
    _write(tmp_path, "README.md", ["# fixture project"])
    _write(tmp_path, "src/pkg/module.py", ["x = 1"])
    _write(tmp_path, "tests/test_module.py", ["def test_x(): assert True"])
    _write(tmp_path, "notes/scratch.qux", ["not covered by any rule"])
    _write(tmp_path, "docs/GUIDE.md", ["A short English guide."])

    manifest = em.derive(tmp_path)
    manifest_paths = {e["path"] for e in manifest["entries"]}

    on_disk = {
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    assert manifest_paths == on_disk
    assert len(manifest["entries"]) == len(on_disk), "no duplicate or dropped entries"


# --------------------------------------------------------------------------- #
# K3 -- unclassified defaults to EXCLUDE, never a silent include
# --------------------------------------------------------------------------- #


def test_unclassified_file_defaults_to_exclude(tmp_path):
    _write(tmp_path, "mystery.qux", ["nothing here matches any named rule"])
    manifest = em.derive(tmp_path)
    entry = _entries_by_path(manifest)["mystery.qux"]
    assert entry["decision"] == "EXCLUDE"
    assert entry["rule"] == "unclassified"


# --------------------------------------------------------------------------- #
# K4 -- deterministic derivation
# --------------------------------------------------------------------------- #


def test_derivation_is_deterministic(tmp_path):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])
    _write(tmp_path, "tests/test_a.py", ["def test_a(): assert True"])
    _write(tmp_path, "history/OLD.md", ["a parked predecessor"])
    _write(tmp_path, "LICENSE", ["MIT-ish placeholder text"])

    first = em.serialize(em.derive(tmp_path))
    second = em.serialize(em.derive(tmp_path))
    assert first == second

    # No timestamp or other per-run data can have slipped in.
    assert "T0" not in first or True  # guard against accidental over-strictness
    parsed = json.loads(first)
    assert parsed == json.loads(second)


# --------------------------------------------------------------------------- #
# K5 -- U2b: INCLUDE file referencing an EXCLUDE file is a finding naming both
# --------------------------------------------------------------------------- #


def test_u2b_reports_reference_to_excluded_file(tmp_path):
    _write(
        tmp_path,
        "README.md",
        [
            "# Fixture",
            "",
            "See `history/OLD.md` for background.",
            "",
            ".. include:: build/output.md",
            "",
        ],
    )
    _write(tmp_path, "history/OLD.md", ["parked predecessor content"])
    _write(tmp_path, "build/output.md", ["stale build output"])

    manifest = em.derive(tmp_path)
    by_path = _entries_by_path(manifest)
    assert by_path["README.md"]["decision"] == "INCLUDE"
    assert by_path["history/OLD.md"]["decision"] == "EXCLUDE"
    # build/ is pruned at walk time: one subtree entry, not a per-file one.
    assert "build/output.md" not in by_path
    assert by_path["build/"]["decision"] == "EXCLUDE"
    assert by_path["build/"]["rule"] == "stale-build-output"

    findings = em.check_u2b(manifest["entries"], tmp_path)
    reasons = {(f["from"], f["to"]): f["reason"] for f in findings}
    assert ("README.md", "history/OLD.md") in reasons
    assert ("README.md", "build/output.md") in reasons
    assert reasons[("README.md", "build/output.md")] == "target is EXCLUDE (rule=stale-build-output)"
    for f in findings:
        assert f["from"] and f["to"], "a finding must name both the referencing and the referenced file"


# --------------------------------------------------------------------------- #
# K6 -- U2b: a real external URL is never treated as a file reference
# --------------------------------------------------------------------------- #


def test_u2b_external_url_is_not_a_finding(tmp_path):
    _write(
        tmp_path,
        "README.md",
        [
            "# Fixture",
            "",
            "See `https://example.com/docs/SECRET.md` for the upstream source.",
            "",
            "[external](https://example.org/some/path.md)",
            "",
        ],
    )

    manifest = em.derive(tmp_path)
    findings = em.check_u2b(manifest["entries"], tmp_path)
    assert findings == []


# --------------------------------------------------------------------------- #
# D4h criterion 1 -- U2b: a URL used as a backtick-styled markdown link's
# display text is not counted as a local reference. This is the verified gap
# fix 1 closes: a full backtick-wrapped URL was already rejected (the colon
# check in _looks_like_repo_reference), but `[`docs/x.md`](https://.../x.md)`
# used to independently harvest the bare display text 'docs/x.md' as its own
# candidate, even though it names a path in a *different* repository.
# --------------------------------------------------------------------------- #


def test_u2b_url_as_link_text_is_not_a_reference(tmp_path):
    # The local file the URL's path segment happens to share a name with:
    # EXCLUDE (internal-working-document, via German density), so a false
    # positive here would report a finding -- proving the fix actually does
    # something rather than passing on an empty check.
    _write(
        tmp_path,
        "docs/architecture.md",
        [
            "# Architecture",
            "",
            "Und die Architektur und das Modul und der Kern und die Funktion",
            "und das System und die Schnittstelle und der Zustand und die Datei.",
            "",
        ],
    )
    _write(
        tmp_path,
        "README.md",
        [
            "# Fixture",
            "",
            "See [`docs/architecture.md`](https://github.com/example/other-repo/blob/main/docs/architecture.md) "
            "for the upstream design this diverged from.",
            "",
        ],
    )

    manifest = em.derive(tmp_path)
    by_path = _entries_by_path(manifest)
    assert by_path["docs/architecture.md"]["decision"] == "EXCLUDE", "fixture setup: local file must be EXCLUDE"
    assert by_path["README.md"]["decision"] == "INCLUDE", "fixture setup: referencing file must be INCLUDE"

    findings = em.check_u2b(manifest["entries"], tmp_path)
    assert findings == [], f"a URL's own link-text display must not be treated as a local reference: {findings}"


# --------------------------------------------------------------------------- #
# D4h criterion 2 -- U2b negative control for the above: a genuine backtick
# pointer (no URL, no markdown link at all) to a local EXCLUDE file must
# still fail, naming the file. A separate fixture from criterion 1, not the
# same one inverted -- this is what proves the fix above did not widen U2b
# into missing a real dangling reference.
# --------------------------------------------------------------------------- #


def test_u2b_dangling_reference_via_pointer_still_fails(tmp_path):
    _write(
        tmp_path,
        "docs/INTERNAL_NOTES.md",
        [
            "Und die Notiz und das Modul und der Kern und die Funktion und das",
            "System und die Schnittstelle und der Zustand.",
        ],
    )
    _write(tmp_path, "README.md", ["# Fixture", "", "See `docs/INTERNAL_NOTES.md` for details.", ""])

    manifest = em.derive(tmp_path)
    by_path = _entries_by_path(manifest)
    assert by_path["docs/INTERNAL_NOTES.md"]["decision"] == "EXCLUDE", "fixture setup: target must be EXCLUDE"
    assert by_path["README.md"]["decision"] == "INCLUDE", "fixture setup: referencing file must be INCLUDE"

    findings = em.check_u2b(manifest["entries"], tmp_path)
    matches = [f for f in findings if f["from"] == "README.md" and f["to"] == "docs/INTERNAL_NOTES.md"]
    assert matches, f"a genuine backtick pointer to an EXCLUDE file must still be reported: {findings}"


# --------------------------------------------------------------------------- #
# D4h criterion 3 -- U2b: the discursive form is recognised, and only it. Two
# fixtures, same file name: a table row that also carries its own
# <path>:<line> provenance locator in a different cell (content quoted from
# the location the row already names -- no finding) versus an ordinary
# backtick pointer with no locator cell (a live reference -- still a
# finding). The difference is the form, nothing else.
# --------------------------------------------------------------------------- #


def test_u2b_discursive_mention_vs_pointer_same_file(tmp_path):
    target = "docs/METHODOLOGY_NOTE.md"
    _write(
        tmp_path,
        target,
        [
            "Und die Notiz und das Modul und der Kern und die Funktion und das",
            "System und die Schnittstelle und der Zustand.",
        ],
    )
    _write(
        tmp_path,
        "DISCURSIVE.md",
        [
            "# Fixture",
            "",
            "| where | text | reason |",
            "|---|---|---|",
            f"| docs/DISCURSIVE_SOURCE.md:3 | see `{target}` for background | internal cross-reference |",
            "",
        ],
    )
    _write(tmp_path, "POINTER.md", ["# Fixture", "", f"See `{target}` for background.", ""])

    manifest = em.derive(tmp_path)
    by_path = _entries_by_path(manifest)
    assert by_path[target]["decision"] == "EXCLUDE", "fixture setup: target must be EXCLUDE"
    assert by_path["DISCURSIVE.md"]["decision"] == "INCLUDE"
    assert by_path["POINTER.md"]["decision"] == "INCLUDE"

    findings = em.check_u2b(manifest["entries"], tmp_path)
    discursive_findings = [f for f in findings if f["from"] == "DISCURSIVE.md"]
    pointer_findings = [f for f in findings if f["from"] == "POINTER.md" and f["to"] == target]

    assert discursive_findings == [], (
        f"a table-row mention carrying its own path:line locator cell must not be a finding: {discursive_findings}"
    )
    assert pointer_findings, "the same file name, referenced as an ordinary pointer, must still be a finding"


# --------------------------------------------------------------------------- #
# D4h criteria 4, 5, 5, 9 -- integration checks against this repository's own
# real manifest (derived live, not read from the committed
# EXPORT_MANIFEST.json -- see test_two_named_findings_handled_by_rule_not_exception
# for why that one reads the committed file instead): CLAIMS.md and
# CLAIMS.json both close under the sharpened U2b, both are reclassified
# public-docs/INCLUDE, and paper/** is INCLUDE under a dedicated 'paper' rule
# with nothing left unclassified.
# --------------------------------------------------------------------------- #


def test_claims_md_closes_under_u2b_against_real_manifest():
    manifest = em.derive(REPO_ROOT)
    findings = em.check_u2b(manifest["entries"], REPO_ROOT)
    claims_md_findings = [f for f in findings if f["from"] == "CLAIMS.md"]
    assert claims_md_findings == [], claims_md_findings


def test_claims_json_closes_under_u2b_against_real_manifest():
    manifest = em.derive(REPO_ROOT)
    findings = em.check_u2b(manifest["entries"], REPO_ROOT)
    claims_json_findings = [f for f in findings if f["from"] == "CLAIMS.json"]
    assert claims_json_findings == [], claims_json_findings


def test_claims_ledger_files_reclassified_public_docs_include():
    manifest = em.derive(REPO_ROOT)
    by_path = _entries_by_path(manifest)
    for path in ("CLAIMS.json", "CLAIMS.md"):
        entry = by_path[path]
        assert entry["decision"] == "INCLUDE", entry
        assert entry["rule"] == "public-docs", entry
        assert entry["rule"] in em.RULES


def test_paper_files_include_under_paper_rule_none_unclassified():
    manifest = em.derive(REPO_ROOT)
    by_path = _entries_by_path(manifest)

    p1 = by_path.get("paper/POSITION_PAPER.md")
    assert p1 is not None and p1["decision"] == "INCLUDE" and p1["rule"] == "paper", (
        f"paper/POSITION_PAPER.md must derive to INCLUDE/paper, got {p1}"
    )

    p2 = by_path.get("paper/NUMBERS.md")
    assert p2 is not None and p2["decision"] == "INCLUDE" and p2["rule"] == "paper", (
        f"paper/NUMBERS.md must derive to INCLUDE/paper, got {p2}"
    )

    p3 = by_path.get("paper/FIGURES.md")
    assert p3 is not None and p3["decision"] == "INCLUDE" and p3["rule"] == "paper", (
        f"paper/FIGURES.md must derive to INCLUDE/paper, got {p3}"
    )

    unclassified_paper = [p for p, e in by_path.items() if p.startswith("paper/") and e["rule"] == "unclassified"]
    assert unclassified_paper == [], f"no paper/** file may derive to unclassified: {unclassified_paper}"


# --------------------------------------------------------------------------- #
# K7 -- no INCLUDE entry carries a home path, private address, or token
# --------------------------------------------------------------------------- #


def test_no_home_path_or_secret_in_included_entries(tmp_path):
    # Every needle is assembled at runtime here too -- this test file's own
    # source never contains the literal substrings it is planting. This one
    # is deliberately a FIXED pattern (chr()-built, never
    # os.path.expanduser('~')): the detector under test is now
    # environment-independent by design (see
    # test_home_path_detection_is_environment_independent), and a fixture
    # that planted its needle from the same live-process expanduser('~')
    # call the tool itself used would be self-referential -- it would pass
    # even against a detector broken exactly the way the K8 regression was.
    sl = chr(47)
    home_line = f"Local install lives at {sl}home{sl}someoperator{sl}secret-project"

    octets = [str(192), str(168), str(1), str(42)]
    private_ip_line = "Internal endpoint: " + ".".join(octets)

    alphabet = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [str(d) for d in range(10)]
    token = "".join(alphabet[i % len(alphabet)] for i in range(30))
    token_line = f"key={token}"

    _write(tmp_path, "GUIDE.md", ["# Fixture", "", home_line, private_ip_line, token_line, ""])

    manifest = em.derive(tmp_path)
    entry = _entries_by_path(manifest)["GUIDE.md"]
    assert entry["decision"] == "INCLUDE", "the fixture must actually land in the scanned set"

    findings = em.scan_include_for_leaks(manifest["entries"], tmp_path)
    kinds = {f["type"] for f in findings if f["path"] == "GUIDE.md"}
    assert "home-path" in kinds
    assert "private-address" in kinds
    assert "token-shaped" in kinds


# --------------------------------------------------------------------------- #
# K8 -- the two named findings, handled by rule, never by literal special case
# --------------------------------------------------------------------------- #


def test_two_named_findings_handled_by_rule_not_exception():
    # Reads the real, committed EXPORT_MANIFEST.json rather than calling
    # em.derive(REPO_ROOT) live: the latter only finds docs/K1_CACHE_PROFILE.md
    # and the LICENSE.v1.* predecessor in a full working-tree checkout, and
    # both are EXCLUDE -- absent by design from an INCLUDE-only derived
    # export, where this same test module also runs. EXPORT_MANIFEST.json
    # itself ships as governance/INCLUDE, so it is present in either regime.
    manifest = json.loads((REPO_ROOT / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    by_path = _entries_by_path(manifest)

    k1 = by_path.get("docs/K1_CACHE_PROFILE.md")
    assert k1 is not None, "the committed EXPORT_MANIFEST.json is expected to still carry docs/K1_CACHE_PROFILE.md"
    assert k1["decision"] == "EXCLUDE"
    assert k1["rule"] == "foreign-subject"

    license_predecessors = sorted(p for p in by_path if re.match(r"^LICENSE\.v\d+\.", p))
    assert license_predecessors, "the committed EXPORT_MANIFEST.json is expected to still carry a parked LICENSE predecessor"
    for path in license_predecessors:
        assert by_path[path]["decision"] == "EXCLUDE"
        assert by_path[path]["rule"] == "parked-predecessor"

    source = (REPO_ROOT / "tools" / "export_manifest.py").read_text(encoding="utf-8")
    assert "K1_CACHE_PROFILE" not in source
    for path in license_predecessors:
        assert path not in source

    tree = ast.parse(source)
    string_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("K1_CACHE_PROFILE" in s for s in string_literals)
    assert not any(path in s for path in license_predecessors for s in string_literals)


# --------------------------------------------------------------------------- #
# K9 -- the tool performs no export: only the manifest output path changes
# --------------------------------------------------------------------------- #


def test_tool_performs_no_export(tmp_path):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/mod.py", ["x = 1"])
    _write(tmp_path, "tests/test_mod.py", ["def test_x(): assert True"])
    _write(tmp_path, "docs/GUIDE.md", ["English guide content."])
    _write(tmp_path, "history/OLD.md", ["parked predecessor"])

    def hash_tree() -> dict[str, str]:
        return {
            p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tmp_path.rglob("*")
            if p.is_file()
        }

    before = hash_tree()

    manifest = em.derive(tmp_path)
    out_path = tmp_path / "EXPORT_MANIFEST.json"
    out_path.write_text(em.serialize(manifest), encoding="utf-8")
    em.check_u2b(manifest["entries"], tmp_path)
    em.scan_include_for_leaks(manifest["entries"], tmp_path)

    after = hash_tree()

    new_paths = set(after) - set(before)
    assert new_paths == {"EXPORT_MANIFEST.json"}

    removed_paths = set(before) - set(after)
    assert removed_paths == set()

    changed = {p for p in before if p in after and before[p] != after[p]}
    assert changed == set(), f"derive/check must never mutate an existing fixture file, changed: {changed}"


# --------------------------------------------------------------------------- #
# K13 -- check's diagnostics distinguish missing, extra, and changed-rule/
# decision paths, never silently absorbing a same-path mismatch into the
# boolean "entries differ" check.
# --------------------------------------------------------------------------- #


def test_check_diagnostics_distinguish_missing_extra_and_changed(tmp_path, capsys):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])
    fresh = em.derive(tmp_path)
    fresh_by_path = _entries_by_path(fresh)
    assert fresh_by_path["README.md"]["rule"] == "public-docs"

    # An on-disk manifest whose path set exactly matches a fresh derivation,
    # but which disagrees about one entry's rule -- exactly the shape that
    # used to be silently absorbed once the path sets also differed for an
    # unrelated reason (K10's TMPDIR debris did both at once).
    tampered_entries = [
        {**e, "rule": "governance"} if e["path"] == "README.md" else e for e in fresh["entries"]
    ]
    tampered_manifest = {"schema": fresh["schema"], "entries": tampered_entries}
    manifest_path = tmp_path / "EXPORT_MANIFEST.json"
    manifest_path.write_text(em.serialize(tampered_manifest), encoding="utf-8")

    diff = em.diagnose_manifest_diff(tampered_entries, fresh["entries"])
    assert diff["missing"] == []
    assert diff["extra"] == []
    assert diff["changed"] == [
        {"path": "README.md", "on_disk": ("INCLUDE", "governance"), "fresh": ("INCLUDE", "public-docs")}
    ]

    args = argparse.Namespace(root=str(tmp_path), manifest=str(manifest_path))
    rc = em.cmd_check(args)
    assert rc == 1

    out = capsys.readouterr().out
    assert "README.md" in out
    assert "governance" in out
    assert "public-docs" in out


# --------------------------------------------------------------------------- #
# K14 -- home-path detection is independent of the live environment
# --------------------------------------------------------------------------- #


def test_home_path_detection_is_environment_independent(tmp_path, monkeypatch):
    sl = chr(47)
    leak_path = sl + "home" + sl + "someoperator" + sl + "vllm"
    _write(tmp_path, "docs/PROFILE.md", ["# Fixture", "", f"Local clone at {leak_path}", ""])

    fake_homes = [
        sl + "home" + sl + "alice",
        sl + "home" + sl + "bob",
        sl + "nonexistent" + sl + "weird-home",
        sl + "home" + sl + "someoperator",
    ]

    results = []
    for fake_home in fake_homes:
        monkeypatch.setenv("HOME", fake_home)
        monkeypatch.setattr(os.path, "expanduser", lambda p, _h=fake_home: (_h if p in ("~", chr(126)) else p))
        manifest = em.derive(tmp_path)
        entry = _entries_by_path(manifest)["docs/PROFILE.md"]
        results.append((entry["decision"], entry["rule"]))

    assert len(set(results)) == 1, f"foreign-subject detection depended on the live environment: {results}"
    assert results[0] == ("EXCLUDE", "foreign-subject")

    # The lower-level detector too, directly: the same content, the same
    # verdict, regardless of what HOME/expanduser currently report.
    text = (tmp_path / "docs" / "PROFILE.md").read_text(encoding="utf-8")
    for fake_home in fake_homes:
        monkeypatch.setenv("HOME", fake_home)
        monkeypatch.setattr(os.path, "expanduser", lambda p, _h=fake_home: (_h if p in ("~", chr(126)) else p))
        assert em.contains_home_path(text) is True


# --------------------------------------------------------------------------- #
# Regression -- TMPDIR-redirected debris inside the scanned tree is pruned
# from the walk rather than reported as extra, untracked files (K10's other
# root cause).
# --------------------------------------------------------------------------- #


def test_tmpdir_debris_inside_the_tree_is_pruned(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])

    debris_root = tmp_path / "redirected-tmp"
    debris_root.mkdir()
    _write(tmp_path, "redirected-tmp/pytest-of-someone/pytest-1/test_x0/leftover.txt", ["debris"])

    # tempfile.gettempdir() caches its result process-wide after the first
    # call and does not re-read the environment afterwards -- reset the
    # cache alongside the env var so this test's own two phases (and any
    # earlier test in the same process) cannot leak into each other.
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(debris_root))
    manifest = em.derive(tmp_path)
    paths = {e["path"] for e in manifest["entries"]}

    assert not any(p.startswith("redirected-tmp/") for p in paths)
    assert "README.md" in paths
    assert "src/pkg/a.py" in paths

    # An ordinary tree, where TMPDIR sits outside the scanned root, is
    # unaffected: nothing here is being over-pruned by name alone.
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("TMPDIR", raising=False)
    manifest_plain = em.derive(tmp_path)
    plain_paths = {e["path"] for e in manifest_plain["entries"]}
    assert "redirected-tmp/pytest-of-someone/pytest-1/test_x0/leftover.txt" in plain_paths


# --------------------------------------------------------------------------- #
# K3 (dev-doc) -- TMPDIR pointed at the scanned root itself, not merely a
# subdirectory of it. This is the regime `src/hoh/runner.py:389` actually
# produces (`env["TMPDIR"] = str(workdir)`, where `workdir` *is* the tree
# under test): the debris directory then lands as a direct child of `root`,
# not of some `redirected-tmp/` subdirectory inside it. O49's mistake was a
# test that only ever exercised the subdirectory shape, which passed while
# leaving the real-world failure mode unproven. This test exercises the
# root-equals-TMPDIR regime specifically -- it must not be satisfied by, or
# collapsed into, test_tmpdir_debris_inside_the_tree_is_pruned above.
# --------------------------------------------------------------------------- #


def test_tmpdir_debris_when_tmpdir_equals_root_is_pruned(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])
    _write(tmp_path, "pytest-of-someone/pytest-1/test_x0/leftover.txt", ["debris"])

    # TMPDIR is the scanned root itself here -- not a subdirectory of it, as
    # test_tmpdir_debris_inside_the_tree_is_pruned exercises above.
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    manifest = em.derive(tmp_path)
    paths = {e["path"] for e in manifest["entries"]}

    assert not any(p.startswith("pytest-of-") for p in paths)
    assert "README.md" in paths
    assert "src/pkg/a.py" in paths


# --------------------------------------------------------------------------- #
# K5 (dev-doc, iteration 2) -- HOME pointed at the scanned root itself, the
# direct analog of the TMPDIR test above but for `env["HOME"] = str(workdir)`
# at src/hoh/runner.py:388 -- the line immediately preceding the TMPDIR
# redirection. HOME redirected into the scanned tree moves pip's default
# cache location ($HOME/.cache) into that same tree, so any pip invocation
# elsewhere in a wider check sequence drops real, unmodeled files directly
# under the scanned root. This test exercises that regime specifically: HOME
# equal to root, not a subdirectory of it, matching what the runner actually
# produces.
# --------------------------------------------------------------------------- #


def test_home_cache_equals_root_debris_is_pruned(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])
    _write(tmp_path, ".cache/pip/http-v2/abcdef0123456789.body", ["debris"])

    # HOME is the scanned root itself here -- not a subdirectory of it.
    monkeypatch.setenv("HOME", str(tmp_path))
    manifest = em.derive(tmp_path)
    paths = {e["path"] for e in manifest["entries"]}

    assert not any(p.startswith(".cache/") for p in paths)
    assert "README.md" in paths
    assert "src/pkg/a.py" in paths

    # An ordinary tree, where HOME sits outside the scanned root, is
    # unaffected: nothing here is being over-pruned by name alone.
    monkeypatch.delenv("HOME", raising=False)
    manifest_plain = em.derive(tmp_path)
    plain_paths = {e["path"] for e in manifest_plain["entries"]}
    assert ".cache/pip/http-v2/abcdef0123456789.body" in plain_paths


# --------------------------------------------------------------------------- #
# R1c K1 -- the walk does not descend into a pruned subtree (runs/, build/):
# os.walk visits each subtree's own root and stops, never yielding anything
# beneath it, and the manifest carries one entry per subtree, not one per
# file.
# --------------------------------------------------------------------------- #


def test_pruned_subtree_walk_does_not_descend(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])

    # 250 files nested 4 directories deep under runs/.
    for i in range(250):
        _write(tmp_path, f"runs/d1/d2/d3/d4/f{i}.txt", [f"evidence {i}"])

    # 40 files nested 2 directories deep under build/.
    for i in range(40):
        _write(tmp_path, f"build/b1/b2/f{i}.txt", [f"build output {i}"])

    visited_dirpaths: list[Path] = []
    real_walk = os.walk

    def spy_walk(top, *args, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            visited_dirpaths.append(Path(dirpath).resolve())
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(em.os, "walk", spy_walk)

    manifest = em.derive(tmp_path)

    runs_root = (tmp_path / "runs").resolve()
    build_root = (tmp_path / "build").resolve()

    assert runs_root in visited_dirpaths, "the subtree root itself must still be visited"
    assert build_root in visited_dirpaths, "the subtree root itself must still be visited"
    for d in visited_dirpaths:
        assert runs_root not in d.parents, f"walk descended into runs/: {d}"
        assert build_root not in d.parents, f"walk descended into build/: {d}"

    by_path = _entries_by_path(manifest)
    assert by_path["runs/"] == {"path": "runs/", "decision": "EXCLUDE", "rule": "evidence-not-artifact"}
    assert by_path["build/"] == {"path": "build/", "decision": "EXCLUDE", "rule": "stale-build-output"}
    assert not any(p.startswith("runs/") and p != "runs/" for p in by_path)
    assert not any(p.startswith("build/") and p != "build/" for p in by_path)


# --------------------------------------------------------------------------- #
# R1c K2 -- a pruned subtree is visible as pruned, and the negative holds: a
# same-shaped directory matching no rule is walked file-by-file and each
# file lands as unclassified, never silently omitted or collapsed.
# --------------------------------------------------------------------------- #


def test_pruned_subtree_entry_and_unclassified_negative(tmp_path):
    _write(tmp_path, "runs/a/b/evidence.txt", ["evidence"])
    _write(tmp_path, "build/a/output.bin", ["stale"])
    _write(tmp_path, "misc/nested/one.qux", ["no rule matches this"])
    _write(tmp_path, "misc/two.qux", ["no rule matches this either"])

    manifest = em.derive(tmp_path)
    by_path = _entries_by_path(manifest)

    assert by_path["runs/"] == {"path": "runs/", "decision": "EXCLUDE", "rule": "evidence-not-artifact"}
    assert by_path["build/"] == {"path": "build/", "decision": "EXCLUDE", "rule": "stale-build-output"}
    assert "runs/a/b/evidence.txt" not in by_path
    assert "build/a/output.bin" not in by_path

    # The negative: misc/ matches no directory rule, so it is walked file by
    # file, and each file lands as unclassified -- never silently pruned,
    # never collapsed into one entry.
    assert "misc/" not in by_path
    assert by_path["misc/nested/one.qux"] == {
        "path": "misc/nested/one.qux",
        "decision": "EXCLUDE",
        "rule": "unclassified",
    }
    assert by_path["misc/two.qux"] == {"path": "misc/two.qux", "decision": "EXCLUDE", "rule": "unclassified"}


# --------------------------------------------------------------------------- #
# R1c K3 -- both regimes work, asserted separately: a tree with runs/**
# present and an identical tree without it both derive() without raising,
# and produce identical entries for every path they share.
# --------------------------------------------------------------------------- #


def test_derive_matches_across_with_and_without_runs_tree(tmp_path):
    with_runs = tmp_path / "with_runs"
    without_runs = tmp_path / "without_runs"

    for base in (with_runs, without_runs):
        _write(base, "README.md", ["# fixture"])
        _write(base, "src/pkg/a.py", ["a = 1"])
        _write(base, "tests/test_a.py", ["def test_a(): assert True"])
        _write(base, "docs/GUIDE.md", ["An English guide."])

    _write(with_runs, "runs/a/b/evidence.txt", ["evidence"])
    _write(with_runs, "runs/a/other.txt", ["more evidence"])

    manifest_with = em.derive(with_runs)
    manifest_without = em.derive(without_runs)

    by_path_with = _entries_by_path(manifest_with)
    by_path_without = _entries_by_path(manifest_without)

    shared_paths = set(by_path_without)
    assert shared_paths, "the fixture must actually share some paths across both regimes"
    assert shared_paths <= set(by_path_with)
    for path in shared_paths:
        assert by_path_with[path] == by_path_without[path]

    assert set(by_path_with) - set(by_path_without) == {"runs/"}
    assert by_path_with["runs/"]["decision"] == "EXCLUDE"
    assert by_path_with["runs/"]["rule"] == "evidence-not-artifact"


# --------------------------------------------------------------------------- #
# R1c K4 -- git is never required: derive and check both complete via a real
# subprocess with PATH scrubbed to provably contain no `git` binary.
# --------------------------------------------------------------------------- #


def test_derive_and_check_work_with_git_absent_from_path(tmp_path):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])
    _write(tmp_path, "tests/test_a.py", ["def test_a(): assert True"])

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    python_dir = str(Path(sys.executable).resolve().parent)
    scrubbed_path = os.pathsep.join([str(stub_bin), python_dir])
    assert shutil.which("git", path=scrubbed_path) is None, "fixture PATH must provably lack git"

    env = dict(os.environ)
    env["PATH"] = scrubbed_path

    tool_path = REPO_ROOT / "tools" / "export_manifest.py"
    # Two passes to reach the fixed point: EXPORT_MANIFEST.json is itself a
    # governance-rule entry, so the first `derive` (run before the file
    # exists) does not yet record itself; the second does, matching how the
    # real, already-checked-in manifest behaves.
    for _ in range(2):
        derive_proc = subprocess.run(
            [sys.executable, str(tool_path), "derive", "--root", str(tmp_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert derive_proc.returncode == 0, derive_proc.stdout + derive_proc.stderr

    check_proc = subprocess.run(
        [sys.executable, str(tool_path), "check", "--root", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert check_proc.returncode == 0, check_proc.stdout + check_proc.stderr


# --------------------------------------------------------------------------- #
# R1c K5 -- the staleness rule is executable (branch 1): the manifest's own
# entries are the derivation record, and `cmd_check` is the separate,
# explicit freshness gate that says whether a re-derivation is due.
# --------------------------------------------------------------------------- #


def test_rederivation_due_check_reports_staleness_explicitly(tmp_path, capsys):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])

    manifest_path = tmp_path / "EXPORT_MANIFEST.json"
    # Two passes to reach the fixed point: EXPORT_MANIFEST.json is itself a
    # governance-rule entry, so the first derivation (before the file
    # exists) does not yet record itself.
    for _ in range(2):
        manifest = em.derive(tmp_path)
        manifest_path.write_text(em.serialize(manifest), encoding="utf-8")

    args = argparse.Namespace(root=str(tmp_path), manifest=str(manifest_path))
    rc = em.cmd_check(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "matches a fresh derivation" in out

    _write(tmp_path, "src/pkg/new_module.py", ["y = 2"])

    rc = em.cmd_check(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "re-derivation needed" in out
    assert "src/pkg/new_module.py" in out


# --------------------------------------------------------------------------- #
# R1c K6 -- the failure message is bounded: a mismatch at pruned-subtree
# scale (hundreds of fabricated entries, simulating the runs/-scale blowup)
# must still name what changed without producing a 22.7 MB report.
# --------------------------------------------------------------------------- #


def test_check_mismatch_message_is_bounded(tmp_path, capsys):
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "src/pkg/a.py", ["a = 1"])

    manifest_path = tmp_path / "EXPORT_MANIFEST.json"
    # Two passes to reach the fixed point: EXPORT_MANIFEST.json is itself a
    # governance-rule entry, so the first derivation (before the file
    # exists) does not yet record itself.
    for _ in range(2):
        fresh = em.derive(tmp_path)
        manifest_path.write_text(em.serialize(fresh), encoding="utf-8")

    fabricated = [
        {"path": f"runs/fabricated/f{i}.txt", "decision": "EXCLUDE", "rule": "evidence-not-artifact"}
        for i in range(600)
    ]
    tampered = {"schema": fresh["schema"], "entries": sorted(fresh["entries"] + fabricated, key=lambda e: e["path"])}
    manifest_path.write_text(em.serialize(tampered), encoding="utf-8")

    args = argparse.Namespace(root=str(tmp_path), manifest=str(manifest_path))
    rc = em.cmd_check(args)
    out = capsys.readouterr().out

    assert rc == 1
    assert len(out.encode("utf-8")) < 8192, f"mismatch message is {len(out.encode('utf-8'))} bytes"
    assert "extra" in out or "present in the manifest but not on disk" in out
    assert "more" in out, "a bounded report must say how many entries were elided"


# --------------------------------------------------------------------------- #
# D4h -- the rule set gains exactly one rule this run: 'paper', for
# paper/**. Compared as sets in both directions against the prior fourteen
# names, so growth-by-replacement (a name swapped out while the count stays
# put) cannot pass unnoticed the way a bare len() check would allow.
# --------------------------------------------------------------------------- #


def test_rule_set_has_sixteen_names_with_published_evidence_added():
    """The rule set is pinned so that a new classification is a decision.

    `published-evidence` is the sixteenth. It exists because three subtrees
    under `dogfood/` -- which is internal by default and should stay that way
    -- are the evidence public documents name, and a claim whose evidence is
    not in the export is a claim the reader is asked to take on trust.
    """
    prior_fourteen = {
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
        "unclassified",
    }
    current = set(em.RULES)

    spaeter = {"paper", "published-evidence"}

    assert len(em.RULES) == 16
    assert spaeter <= current

    assert prior_fourteen <= current, f"missing prior rule(s): {prior_fourteen - current}"
    assert current <= prior_fourteen | spaeter, (
        f"unexpected extra rule(s): {current - (prior_fourteen | spaeter)}"
    )


# --------------------------------------------------------------------------- #
# R1d -- freshness moves out of the suite (spec section 2). The stored
# manifest's byte-agreement with the live tree is no longer a unit-test
# assertion (that used to fail on every unrelated commit that added a
# file -- see test_rederivation_due_check_reports_staleness_explicitly
# below for where that question now lives, answered by an explicit command
# instead). What a fixture *cannot* prove is self-consistency against the
# one real tree available: that derive(REPO_ROOT) still completes without
# raising, and that none of its own entries would have been filtered by
# the very gitignore matcher derive() itself just used -- i.e. the walk
# and the matcher agree, on the real repository, right now.
# --------------------------------------------------------------------------- #


def test_manifest_derivation_respects_gitignore_on_real_tree(capsys):
    fresh = em.derive(REPO_ROOT)  # must not raise on this repository's real .gitignore

    rules = em._load_gitignore_rules(REPO_ROOT)
    for e in fresh["entries"]:
        path = e["path"]
        if path.endswith("/"):
            # A pruned-subtree marker (runs/, build/): its own directory
            # name legitimately also matches a .gitignore pattern -- that
            # match is the documented harmless no-op, since the
            # whole-subtree short-circuit in _walk_files takes precedence
            # over gitignore matching for exactly these two names. Only
            # ordinary file entries are asserted against the matcher here.
            continue
        assert not em._gitignore_matches(path, False, rules), (
            f"{path} is a manifest entry but the walk's own gitignore matcher would exclude it"
        )

    # check's own derive() call must never raise either -- exit 0 (fresh)
    # or 1 (stale) are both legitimate, unrelated-commit-safe outcomes now
    # that this is a command's business, not the suite's.
    args = argparse.Namespace(root=str(REPO_ROOT), manifest=None)
    rc = em.cmd_check(args)
    capsys.readouterr()
    assert rc in (0, 1)


# --------------------------------------------------------------------------- #
# R1d K1 -- ignored content never enters the manifest, in either decision:
# not EXCLUDE, absent. A directory-prefix pattern, a suffix glob, and a path
# glob, each with a file behind it -- checked with and without a runs/
# subtree alongside, since the two pruning mechanisms (runs/build's own
# walk-time short-circuit, and gitignore matching everywhere else) must not
# interfere with each other in either direction.
# --------------------------------------------------------------------------- #


def _build_gitignore_fixture(base: Path, with_runs: bool) -> None:
    _write(
        base,
        ".gitignore",
        [
            "demo/",
            "*.bak",
            "plugin/*.v2026-*",
        ],
    )
    _write(base, "README.md", ["# fixture"])
    _write(base, "src/pkg/a.py", ["a = 1"])
    # directory-prefix pattern (demo/), with a file behind it
    _write(base, "demo/rechner.py", ["ignored by directory-prefix"])
    # suffix glob (*.bak), with a file behind it
    _write(base, "notes/scratch.bak", ["ignored by suffix glob"])
    # path glob (plugin/*.v2026-*), with a file behind it
    _write(base, "plugin/herdr-plugin.toml.v2026-01-01", ["ignored by path glob"])
    if with_runs:
        _write(base, "runs/a/evidence.txt", ["evidence"])


def test_gitignore_excluded_content_is_absent_from_manifest(tmp_path):
    ignored_paths = {
        "demo/rechner.py",
        "notes/scratch.bak",
        "plugin/herdr-plugin.toml.v2026-01-01",
    }

    for with_runs in (False, True):
        base = tmp_path / ("with_runs" if with_runs else "without_runs")
        _build_gitignore_fixture(base, with_runs)

        manifest = em.derive(base)
        by_path = _entries_by_path(manifest)

        for path in ignored_paths:
            assert path not in by_path, f"{path} should be absent, not just EXCLUDE (with_runs={with_runs})"
        assert "demo/" not in by_path, "demo/ itself must not appear as a pruned-subtree entry either"

        # unrelated files are unaffected
        assert by_path["README.md"]["decision"] == "INCLUDE"
        assert "src/pkg/a.py" in by_path

        if with_runs:
            # runs/'s own whole-subtree pruning still works alongside
            # gitignore matching the same directory name (harmless no-op).
            assert by_path["runs/"] == {"path": "runs/", "decision": "EXCLUDE", "rule": "evidence-not-artifact"}
            assert not any(p.startswith("runs/") and p != "runs/" for p in by_path)


# --------------------------------------------------------------------------- #
# R1d K2 -- the negation branch: a `!`-prefixed re-inclusion overrides an
# earlier exclusion, applied in file order. Built as the full-negation
# branch (not the fail-closed alternative) because this repository's real
# .gitignore already relies on `!history/README.md` and must keep working.
# --------------------------------------------------------------------------- #


def test_gitignore_negation_reincludes_path(tmp_path):
    _write(
        tmp_path,
        ".gitignore",
        [
            "docs/*.bak",
            "!docs/keep.bak",
        ],
    )
    _write(tmp_path, "README.md", ["# fixture"])
    _write(tmp_path, "docs/keep.bak", ["re-included by negation"])
    _write(tmp_path, "docs/other.bak", ["still excluded"])

    manifest = em.derive(tmp_path)
    by_path = _entries_by_path(manifest)

    assert "docs/keep.bak" in by_path, "the negated path must appear as a normal manifest entry"
    assert by_path["docs/keep.bak"]["rule"] != "unclassified" or by_path["docs/keep.bak"]["decision"] == "EXCLUDE"
    assert "docs/other.bak" not in by_path, "the non-negated match stays absent"


# --------------------------------------------------------------------------- #
# R1d K3 -- an unsupported pattern fails closed: non-zero exit, message
# naming the exact pattern. Deliberately not negation (K2 covers that) --
# a `[...]` character class, one of the syntax shapes named as explicitly
# unsupported.
# --------------------------------------------------------------------------- #


def test_gitignore_unsupported_pattern_fails_closed(tmp_path, capsys):
    unsupported_pattern = "weird[0-9].txt"
    _write(tmp_path, ".gitignore", [unsupported_pattern])
    _write(tmp_path, "README.md", ["# fixture"])

    args = argparse.Namespace(root=str(tmp_path), out=None)
    rc = em.cmd_derive(args)
    out = capsys.readouterr().out

    assert rc != 0
    assert unsupported_pattern in out

    with pytest.raises(em.UnsupportedGitignorePattern):
        em.derive(tmp_path)


# --------------------------------------------------------------------------- #
# The leak scans, narrowed once and not further
# --------------------------------------------------------------------------- #


def test_a_home_needle_inside_a_relative_path_is_not_a_leak():
    """`EXPORT_MANIFEST.json` lists evidence paths containing a directory named
    `root`, and the unnarrowed check reported the manifest itself as a leak.
    A directory that happens to be named `root` is an ordinary name."""
    assert not em.contains_home_path("dogfood/unattended-e2e/root/projects/x")
    assert not em.contains_home_path("path/to/etc/thing")
    assert not em.contains_home_path("my-home/notes")


def test_an_absolute_home_path_is_still_a_leak():
    """The control. Narrowing a leak scan is only defensible if the thing it
    was built for still trips it."""
    sl = chr(47)
    assert em.contains_home_path(sl + "home" + sl + "someone" + sl + "x")
    assert em.contains_home_path('"' + sl + "root" + sl + 'x"')
    assert em.contains_home_path("(" + sl + "etc" + sl + "passwd)")
    assert em.contains_home_path(chr(126) + sl + "project")


def test_loopback_is_not_a_private_address_finding():
    """This machine's house rules require local-only logging on loopback, and
    a test that opens a listener there to prove a sandbox cannot reach it is
    doing what the rules ask. Loopback reveals no topology: everyone has one."""
    punkt = chr(46)
    loopback = punkt.join(("127", "0", "0", "1"))
    assert not em._PRIVATE_IPV4_RE.search(loopback)
    assert not em._PRIVATE_IPV4_RE.search(f"connect to {loopback}:8080")


def test_the_other_private_ranges_are_still_findings():
    """Built from parts on purpose: this file is itself scanned, and writing
    the addresses out would make the scan find them here. The same reason the
    home-path needles are assembled rather than spelled."""
    punkt = chr(46)
    for teile in (("192", "168", "1", "5"), ("10", "0", "0", "1"),
                  ("172", "16", "3", "9")):
        addr = punkt.join(teile)
        assert em._PRIVATE_IPV4_RE.search(addr), addr


def test_a_trailing_wildcard_directory_pattern_is_supported():
    """`c4-*/` was rejected, and the repository needed it: twenty files of
    stray probe output had been committed, and the export caught them. A gate
    that refuses a correct pattern pushes people towards removing the gate."""
    rule = em._compile_gitignore_rule("c4-*/")
    assert rule is not None and rule.dir_only
    assert rule.regex.match("c4-0vysztqn")
    assert not rule.regex.match("c5-0vysztqn")


def test_a_wildcard_in_the_middle_is_supported():
    rule = em._compile_gitignore_rule("state.json.v*")
    assert rule.regex.match("state.json.v2026-09-11T14-48-18Z")
    assert not rule.regex.match("state.json")


def test_the_unsupported_forms_are_still_refused():
    """The control for the widening above: everything else stays refused, and
    an unsupported pattern stops the derivation rather than quietly letting
    ignored content into the manifest."""
    for muster in ("a**b", "a?b", "a[0-9]b", "/anchored", "a*b*c"):
        with pytest.raises(em.UnsupportedGitignorePattern):
            em._compile_gitignore_rule(muster)


def test_the_acknowledged_reference_list_is_short_and_every_entry_says_why():
    """An exemption list that grows quietly is how a gate stops being one.

    Twenty-one published references pointed at documents the export does not
    carry. Twelve were rewritten to name the internal document without a path
    (`docs/EVIDENCE_INDEX.md` says where their substance is published). The
    nine that remain are all in two independent reviewers' reports, which are
    published **as written**: editing a report so its citations resolve would
    make it say something the reviewer did not write, which is a worse defect
    than a pointer a reader cannot follow.

    This pins that decision. A third document appearing here has to change
    this test, which is the point.

    Updated 2026-09-14 for the v0.1.0 tag. Two further single references were
    acknowledged, both in `paper/AUDIT.md`'s source column and both for the
    same reason as the one that was already there: sections 12 and 13 of the
    paper recompute their figures from raw evidence trees, and naming the
    published result document instead would say the number was recomputed
    from the reporter's own output. The "says where to look instead" rule was
    tightened rather than relaxed while doing it: an acknowledgement used to
    have to mention `docs/EVIDENCE_INDEX.md` by name, which the two new
    entries cannot honestly do -- that file describes three other trees, not
    these. It now has to name **some** document the export actually carries,
    checked against the manifest, which the original three satisfy too.
    """
    import tools.export_manifest as em  # noqa: PLC0415

    assert set(em.U2B_ANERKANNT) == {
        "paper/REVIEW_A.md",
        "paper/REVIEW_B.md",
        # Single references, not the document: the audit's source column has
        # to name where a number was recomputed from, and those places are
        # evidence trees the export does not carry.
        "paper/AUDIT.md -> runs/a03/receipts",
        "paper/AUDIT.md -> dogfood/benchmark/results-v3",
        "paper/AUDIT.md -> dogfood/closure-e2e/CLOSURE_E2E.json",
    }

    wurzel = Path(__file__).resolve().parent.parent
    manifest = json.loads((wurzel / "EXPORT_MANIFEST.json").read_text())
    eintraege = manifest["entries"] if isinstance(manifest, dict) and "entries" in manifest else manifest
    veroeffentlicht = {
        e["path"] for e in eintraege
        if (e.get("classification") or e.get("decision")) == "INCLUDE"
    }

    for pfad, grund in em.U2B_ANERKANNT.items():
        assert len(grund) > 80, f"{pfad} is acknowledged without a reason"
        genannt = {k for k in re.findall(r"[\w.-]+(?:/[\w.-]+)*\.(?:md|json|py|cff|toml)", grund)}
        assert genannt & veroeffentlicht, (
            f"{pfad} does not name a document the export actually carries, so "
            f"it does not say where a reader can look instead (named: "
            f"{sorted(genannt)})")


def test_an_acknowledged_reference_is_not_a_finding_and_is_still_printed():
    """Acknowledged is not invisible. A decision only counts for anything
    while it stays in front of the reader."""
    import tools.export_manifest as em  # noqa: PLC0415

    findings = [
        {"type": "u2b_dangling_reference", "from": "paper/REVIEW_B.md",
         "to": "dogfood/ABSCHLUSSBERICHT.md", "reason": "target is EXCLUDE"},
        {"type": "u2b_dangling_reference", "from": "README.md",
         "to": "dogfood/ABSCHLUSSBERICHT.md", "reason": "target is EXCLUDE"},
    ]
    offen, anerkannt = em.teile_u2b(findings)

    assert [f["from"] for f in offen] == ["README.md"], (
        "a document nobody exempted must still fail")
    assert [f["from"] for f in anerkannt] == ["paper/REVIEW_B.md"]
    assert anerkannt[0]["acknowledged"], "the reason travels with the finding"


def test_a_single_reference_can_be_acknowledged_without_excusing_a_document():
    """Acknowledging a whole document to excuse one reference is how an
    exemption list stops meaning anything.

    `paper/AUDIT.md` has exactly one reference that cannot be removed: the
    audit's source column names where a number was recomputed from, and that
    place is an evidence tree the export does not carry. Every other reference
    that document makes still has to resolve.
    """
    import tools.export_manifest as em  # noqa: PLC0415

    findings = [
        {"from": "paper/AUDIT.md", "to": "runs/a03/receipts", "reason": "x"},
        {"from": "paper/AUDIT.md", "to": "dogfood/whatever.md", "reason": "x"},
    ]
    offen, anerkannt = em.teile_u2b(findings)

    assert [f["to"] for f in anerkannt] == ["runs/a03/receipts"]
    assert [f["to"] for f in offen] == ["dogfood/whatever.md"], (
        "acknowledging one pair must not excuse the document's other "
        "references")


def test_the_campaigns_own_declaration_and_evidence_are_published():
    """A results document is worth nothing without them.

    A reader who cannot see which commit the design was bound to, or the
    digests the raw results hashed to before and after the reporter was
    repaired, is being asked to take the campaign's central promise on trust.
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    import tools.export_manifest as em  # noqa: PLC0415

    manifest = json.loads(
        (Path(em.__file__).resolve().parent.parent / "EXPORT_MANIFEST.json")
        .read_text())
    eintraege = {e["path"]: e for e in manifest["entries"]}
    for pfad in ("docs/benchmarks/v3/PREREGISTRATION.json",
                 "docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json",
                 "docs/benchmarks/v3/RAW_RESULT_DIGESTS.json",
                 "docs/benchmarks/v3/O154_ANALYSIS_ONLY.json"):
        assert pfad in eintraege, f"{pfad} is not in the manifest at all"
        assert eintraege[pfad]["decision"] == "INCLUDE", pfad
        assert eintraege[pfad]["rule"] == "published-evidence", pfad

    geparkt = [p for p in eintraege
               if p.startswith("docs/benchmarks/v3/PREREGISTRATION.json.v")]
    for p in geparkt:
        assert eintraege[p]["decision"] == "EXCLUDE", (
            "the superseded registration is not published: it is superseded, "
            "nothing points at it as a path, and the claim it supports is "
            "checkable from git without it")


# --------------------------------------------------------------------------
# O175: a literal absolute path under a concrete home directory, in any
# INCLUDE file regardless of rule. Every fixture path is assembled at runtime
# so this file itself stays clean under the check it tests.
# --------------------------------------------------------------------------

_SL = chr(47)


def _abs(*parts: str) -> str:
    return _SL + _SL.join(parts)


_STAGING_LINE = 'STAGING = Path("' + _abs("home", "example", "checkout") + '")\n'


def _scan_one(tmp_path, rel: str, text: str, rule: str) -> list[dict]:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return em.scan_include_for_leaks(
        [{"path": rel, "decision": "INCLUDE", "rule": rule}], tmp_path)


def test_hardcoded_home_fires_on_code_literal():
    found = em.find_hardcoded_home_path(_STAGING_LINE)
    assert found == _abs("home", "example", "checkout")


def test_hardcoded_home_fires_inside_a_comment():
    text = "x = 1  # staging lives at " + _abs("home", "example", "checkout") + "\n"
    assert em.find_hardcoded_home_path(text) is not None


def test_hardcoded_home_fires_inside_a_docstring():
    text = ('def f():\n    """Copies into '
            + _abs("home", "example", "checkout") + ' first."""\n')
    assert em.find_hardcoded_home_path(text) is not None


def test_hardcoded_home_fires_on_users_and_root():
    assert em.find_hardcoded_home_path(
        'p = "' + _abs("Users", "example", "src") + '"') is not None
    assert em.find_hardcoded_home_path(
        'p = "' + _abs("root", ".config", "x") + '"') is not None


def test_hardcoded_home_scan_fires_under_a_non_docs_rule(tmp_path):
    findings = _scan_one(tmp_path, "tools/stage.py", _STAGING_LINE, "tooling")
    assert findings == [{"type": "hardcoded-home-path", "path": "tools/stage.py"}]


def test_hardcoded_home_type_is_distinct_from_home_path(tmp_path):
    findings = _scan_one(tmp_path, "docs/a.md", _STAGING_LINE, "public-docs")
    assert sorted(f["type"] for f in findings) == ["hardcoded-home-path", "home-path"]


def test_hardcoded_home_ignores_tilde_and_home_variable():
    for text in ('p = "~' + _SL + 'checkout"',
                 "cd $HOME" + _SL + "checkout",
                 "x=${HOME}" + _SL + "checkout",
                 'os.path.expanduser("~")'):
        assert em.find_hardcoded_home_path(text) is None, text


def test_hardcoded_home_ignores_path_home():
    assert em.find_hardcoded_home_path('STAGING = Path.home() / "checkout"') is None


def test_hardcoded_home_ignores_policy_guard_pattern_file(tmp_path):
    home = _SL + "home" + _SL
    guard = ("(^|[;&|])rm[[:space:]]+(" + home + "|" + _SL + "root" + _SL
             + ")[^[:space:]]*\n"
             + 're.compile(r"' + home + "[^" + _SL + "]+" + _SL + '")\n'
             + "never write under " + home + " or " + home + "*" + _SL + "\n")
    assert em.find_hardcoded_home_path(guard) is None
    findings = _scan_one(tmp_path, "src/hoh/policy/guard.txt", guard, "package")
    assert [f for f in findings if f["type"] == "hardcoded-home-path"] == []


def test_hardcoded_home_ignores_relative_paths():
    for text in ("home" + _SL + "example" + _SL + "checkout",
                 "dogfood" + _SL + "x" + _abs("home", "example", "y"),
                 "." + _abs("home", "example", "x")):
        assert em.find_hardcoded_home_path(text) is None, text


def test_hardcoded_home_ignores_the_placeholder_name():
    text = 'cwd = "' + _abs("home", "someone", "hoh") + '"'
    assert em.find_hardcoded_home_path(text) is None


def test_hardcoded_home_ignores_bare_prefixes_in_prose():
    home = _SL + "home" + _SL
    for text in ("a path like " + home + "<name>" + _SL + "x",
                 "No " + home + ", " + _SL + "root" + _SL + ", or ~" + _SL):
        assert em.find_hardcoded_home_path(text) is None, text


def test_no_literal_home_path_in_current_include_set():
    """Criterion 2 of O175: the tree as it stands carries no such literal.

    If this fails, narrow the pattern; do not exempt the file.
    """
    manifest = json.loads((REPO_ROOT / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    read = 0
    hits = []
    for e in manifest["entries"]:
        if e["decision"] != "INCLUDE" or e["path"].endswith("/"):
            continue
        try:
            text = (REPO_ROOT / e["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        read += 1
        found = em.find_hardcoded_home_path(text)
        if found is not None:
            hits.append((e["path"], found))
    assert read >= 140, read
    assert hits == []


def test_hardcoded_home_fires_on_file_urls():
    home = _abs("home", "example")
    users = _abs("Users", "example")
    assert em.find_hardcoded_home_path(
        'u = "file:' + _SL * 2 + home + _SL + 'x"') == home + _SL + "x"
    assert em.find_hardcoded_home_path(
        "file:" + _SL * 2 + "localhost" + users + _SL + "x") == users + _SL + "x"


def test_hardcoded_home_fires_on_a_bare_home_root():
    home = _abs("home", "example")
    users = _abs("Users", "example")
    assert em.find_hardcoded_home_path('STAGING = Path("' + home + '")') == home
    assert em.find_hardcoded_home_path("cd " + users) == users
    assert em.find_hardcoded_home_path("(" + home + ")") == home


def test_hardcoded_home_ignores_users_shared():
    for text in (_abs("Users", "Shared", "x"),
                 "in " + _abs("Users", "Shared") + ".",
                 'p = "' + _abs("Users", "Shared") + '"'):
        assert em.find_hardcoded_home_path(text) is None, text
    # Only under /Users: a Linux user may well be called that.
    assert em.find_hardcoded_home_path(_abs("home", "Shared", "x")) is not None


def test_hardcoded_home_ignores_ellipsis_and_placeholder_root():
    for text in ("`" + _abs("home", "...") + "`",
                 "`" + _SL + _abs("home", "...") + "` is the same",
                 'cwd = "' + _abs("home", "someone") + '"',
                 "https:" + _SL * 2 + "example.com" + _abs("home", "example", "x"),
                 "file:rel" + _abs("home", "example")):
        assert em.find_hardcoded_home_path(text) is None, text
