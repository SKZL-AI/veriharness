"""Regression tests for CLAIMS.json's content-digest coverage anchors, and
(d4d) for the declared claim_surfaces manifest that replaced the coverage
check's docs/** glob.

d4b anchored coverage bookkeeping to absolute line numbers in
docs/OPERATIONS.md; d3b then edited that same file, and the union broke five
entries even though neither run individually did anything wrong. d4c (the
tests up through test_failure_reports_a_line_number below) exercises the
content-digest anchor identity in tools/check_claims.py that replaces line
numbers as the covered-statement identity, and the re-anchoring of the five
broken docs/OPERATIONS.md entries.

d4d is the same class of defect one layer up (O41): the coverage check
globbed docs/** to find its own scope, so two runs that never touched a
common file still collided inside that glob. The tests from
test_claim_surfaces_manifest_has_required_fields onward exercise the
declared claim_surfaces manifest that replaced the glob, the read-only
reconciliation mode, and the reconciliation of docs/LIMITATIONS.md's eleven
sentences d3c left uncovered.

Every fixture-based test below points the resolver (or the reconciliation
functions, which take the same explicit repo_root parameter) at an isolated
tmp_path directory -- never at this repository's own runs/ tree, never via
git, never with an absolute /home-style path.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import random
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_claims as cc


def _write_fixture(tmp_path: Path, rel_path: str, lines: list[str]) -> Path:
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return full


# A generic fixture statement used across B/C/D/E -- distinct from any real
# docs/OPERATIONS.md wording, so it can never collide with G's substring scan.
_FIXTURE_STATEMENT = "This step retries the request up to 3 times before giving up."


def test_repro_five_old_anchors_fail_and_new_resolver_passes():
    """Criterion A: the five historical stale (path, line) anchors, built here
    as literal fixture data local to this test, no longer find their
    statement in the CURRENT docs/OPERATIONS.md via a plain line-number
    lookup -- and the new digest-based resolver reports zero coverage
    problems for the real repo ledger.
    """
    stale_anchors = [
        (
            "docs/OPERATIONS.md",
            15,
            "| Herdr | A session must be running; `HERDR_ENV=1` is required, "
            "and there is deliberately no tmux fallback |",
        ),
        (
            "docs/OPERATIONS.md",
            149,
            "recent 20 states and 3 arenas stay in place; older ones move to `attic/`.",
        ),
        (
            "docs/OPERATIONS.md",
            25,
            "becomes available directly rather than needing `python3 -m hoh.cli`.",
        ),
        (
            "docs/OPERATIONS.md",
            55,
            "Step 2 is harness-specific (this project's own operators use a companion",
        ),
        (
            "docs/OPERATIONS.md",
            62,
            "automatically**. Without step 4, the run sits in stage `NEW` indefinitely.",
        ),
    ]

    for rel_path, stale_line, statement in stale_anchors:
        full = cc.REPO_ROOT / rel_path
        current_lines = full.read_text(encoding="utf-8").splitlines()
        assert 0 < stale_line <= len(current_lines)
        actual_at_stale_line = current_lines[stale_line - 1].strip()
        assert actual_at_stale_line != statement, (
            f"expected the old line-number anchor {rel_path}:{stale_line} to have "
            f"drifted off the statement, but it still reads {actual_at_stale_line!r}"
        )

    problems = cc.check_coverage(cc.load_ledger())
    unmarked = [p for p in problems if cc.ENVIRONMENT_GAP_MARKER not in p]
    assert not unmarked, (
        f"{len(unmarked)} of {len(problems)} coverage problem(s) lack the "
        f"{cc.ENVIRONMENT_GAP_MARKER} marker; first: {unmarked[0]!r}"
    )


def test_pure_line_shift_still_resolves(tmp_path):
    """Criterion B: inserting text above a covered statement still resolves
    as PASS -- the anchor followed the content, not the old line number.
    """
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)

    _write_fixture(
        tmp_path,
        "fixture.md",
        ["# Fixture", "", "Some unrelated preface text.", _FIXTURE_STATEMENT, ""],
    )
    result = cc.resolve_anchor("fixture.md", digest, None, tmp_path)
    assert result.verdict == "PASS"
    assert result.line == 4

    _write_fixture(
        tmp_path,
        "fixture.md",
        [
            "# Fixture",
            "",
            "A brand new paragraph inserted above the statement.",
            "It spans a couple of lines to genuinely shift things down.",
            "",
            "Some unrelated preface text.",
            _FIXTURE_STATEMENT,
            "",
        ],
    )
    result = cc.resolve_anchor("fixture.md", digest, None, tmp_path)
    assert result.verdict == "PASS"
    assert result.line == 7


def test_content_mutation_is_rejected(tmp_path):
    """Criterion C: changing the covered statement's own wording is rejected
    as FAIL, even though the line number is untouched.
    """
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)
    _write_fixture(
        tmp_path,
        "fixture.md",
        [
            "# Fixture",
            "",
            "This step retries the request up to 5 times before giving up.",
            "",
        ],
    )
    result = cc.resolve_anchor("fixture.md", digest, None, tmp_path)
    assert result.verdict == "FAIL"


def test_deletion_is_rejected(tmp_path):
    """Criterion D: removing the covered statement entirely is rejected as FAIL."""
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)
    _write_fixture(
        tmp_path,
        "fixture.md",
        ["# Fixture", "", "This paragraph no longer mentions retries at all.", ""],
    )
    result = cc.resolve_anchor("fixture.md", digest, None, tmp_path)
    assert result.verdict == "FAIL"


def test_duplicate_statement_is_ambiguous_not_missing(tmp_path):
    """Criterion E: the identical statement twice, with no distinguishing
    heading context, is reported as AMBIGUOUS -- its own verdict, never
    folded into "missing" -- and the resolver refuses to pick one arbitrarily.
    """
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)
    _write_fixture(
        tmp_path,
        "fixture.md",
        ["# Fixture", "", _FIXTURE_STATEMENT, "", _FIXTURE_STATEMENT, ""],
    )

    result = cc.resolve_anchor("fixture.md", digest, None, tmp_path)
    assert result.verdict == "AMBIGUOUS"
    assert sorted(result.candidates) == [3, 5]
    assert result.line is None

    message = cc._anchor_problem_message("claim", "C-TEST", "fixture.md:3", "fixture.md", result)
    lowered = message.lower()
    assert "ambig" in lowered, f"expected the reason to name ambiguity: {message!r}"
    assert "missing" not in lowered
    assert "not found" not in lowered
    assert "not covered" not in lowered


def test_resolver_has_no_hardcoded_site_list():
    """Criterion G: the resolver's own source contains no literal list of the
    five affected statements or their old/new line numbers -- the fix must be
    general, not five special cases in a trench coat.

    Text substrings are checked with a plain string search (they are
    distinctive enough not to collide with anything legitimate in the
    resolver's own source). Line numbers are checked as Python integer
    *literals* via the AST, not as substrings of the source text -- a naive
    substring search would false-positive on this file's own numbered
    docstring comments (e.g. "# 17. cite: reference ..." in
    run_selftest_extended, which has nothing to do with line-anchor logic).
    """
    source = (REPO_ROOT / "tools" / "check_claims.py").read_text(encoding="utf-8")

    distinguishing_substrings = [
        "is deliberately no tmux fallback",
        "3 arenas stay in place",
        "python3 -m hoh.cli",
        "operators use a companion",
        "sits in stage `NEW` indefinitely",
    ]
    for needle in distinguishing_substrings:
        assert needle not in source, f"found hardcoded statement text in the resolver: {needle!r}"

    forbidden_line_numbers = {15, 149, 25, 55, 62, 17, 27, 57, 64, 151}
    tree = ast.parse(source, filename="check_claims.py")
    int_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int)
    }
    hardcoded = forbidden_line_numbers & int_literals
    assert not hardcoded, f"found the affected line numbers as literal constants: {hardcoded}"


def test_failure_reports_a_line_number(tmp_path):
    """Criterion H: a FAIL verdict's reported message still names an
    informative location (the recorded where-line) so an operator can find
    it -- informative, never part of the identity.
    """
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)
    _write_fixture(
        tmp_path,
        "fixture.md",
        ["# Fixture", "", "Nothing here mentions retries anymore.", ""],
    )
    result = cc.resolve_anchor("fixture.md", digest, None, tmp_path)
    assert result.verdict == "FAIL"

    recorded_where = "fixture.md:3"
    message = cc._anchor_problem_message("claim", "C-TEST", recorded_where, "fixture.md", result)
    assert recorded_where in message


# --------------------------------------------------------------------------
# d4d: CLAIMS.json declares its claim surfaces instead of the coverage check
# globbing docs/** -- O41's own class of defect (two disjoint runs colliding
# only inside a glob neither of them could see). The tests below exercise the
# manifest, the declaration-driven coverage check, the read-only
# reconciliation mode, and the reconciled docs/LIMITATIONS.md entries.
# --------------------------------------------------------------------------


def test_claim_surfaces_manifest_has_required_fields():
    """Criterion K1: CLAIMS.json declares its claim surfaces, each with
    `path`, `scope`, and `why` -- parseable and non-empty.
    """
    data = cc.load_ledger()
    surfaces = data.get("claim_surfaces")
    assert isinstance(surfaces, list) and surfaces, "claim_surfaces must be a non-empty list"

    for surface in surfaces:
        assert isinstance(surface, dict), f"claim_surfaces entry is not an object: {surface!r}"
        path = surface.get("path")
        assert isinstance(path, str) and path.strip(), f"surface missing non-empty 'path': {surface!r}"
        scope = surface.get("scope")
        assert scope == "whole file" or (isinstance(scope, list) and scope), (
            f"surface {path!r} has an unrecognized 'scope': {scope!r}"
        )
        why = surface.get("why")
        assert isinstance(why, str) and why.strip(), f"surface {path!r} missing non-empty 'why'"


def test_coverage_uses_declared_surfaces_not_glob():
    """Criterion K2: the coverage check reads the claim_surfaces declaration,
    not a directory glob. Two halves:

    (a) `coverage_targets` -- the function `check_coverage` calls to decide
        what to examine -- contains no directory glob at all in its own
        source (checked with `inspect.getsource`, not a whole-file search,
        so the unrelated glob inside `find_undeclared_claim_surfaces`, which
        exists for a different purpose -- warning about undeclared documents,
        never deciding coverage -- does not make this assertion vacuous).
    (b) Removing a surface from a *temporary copy* of CLAIMS.json (written to
        tmp_path, never touching the real ledger) changes the target set
        `coverage_targets` returns for that copy.
    """
    source = inspect.getsource(cc.coverage_targets)
    tree = ast.parse(source)
    glob_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("glob", "rglob", "walk", "listdir", "iterdir", "scandir")
    ]
    assert not glob_calls, f"coverage_targets still walks a directory via {glob_calls}: {source!r}"


def test_coverage_uses_declared_surfaces_not_glob_removal_demo(tmp_path):
    """Criterion K2, half (b): a temporary copy of CLAIMS.json with one
    surface removed changes what `coverage_targets` examines. The real
    CLAIMS.json is read once (to build a faithful copy) and never written to;
    the copy lives entirely under tmp_path.
    """
    real_data = cc.load_ledger()
    surfaces = real_data.get("claim_surfaces") or []
    removed_path = "docs/RECOVERY.md"
    assert any(s.get("path") == removed_path for s in surfaces), (
        f"fixture assumption broken: {removed_path!r} is not currently declared"
    )

    full_copy = dict(real_data)
    reduced_copy = dict(real_data)
    reduced_copy["claim_surfaces"] = [s for s in surfaces if s.get("path") != removed_path]

    full_copy_path = tmp_path / "CLAIMS.full.json"
    reduced_copy_path = tmp_path / "CLAIMS.reduced.json"
    full_copy_path.write_text(json.dumps(full_copy), encoding="utf-8")
    reduced_copy_path.write_text(json.dumps(reduced_copy), encoding="utf-8")

    full_loaded = cc.load_ledger(full_copy_path)
    reduced_loaded = cc.load_ledger(reduced_copy_path)

    full_targets = {p.relative_to(cc.REPO_ROOT).as_posix() for p, _ in cc.coverage_targets(full_loaded)}
    reduced_targets = {p.relative_to(cc.REPO_ROOT).as_posix() for p, _ in cc.coverage_targets(reduced_loaded)}

    assert removed_path in full_targets
    assert removed_path not in reduced_targets
    assert full_targets - reduced_targets == {removed_path}


def test_all_previously_globbed_files_are_declared():
    """Criterion K3: every file the old `docs/**` glob covered -- README.md
    plus the ten docs/*.md files -- is present in claim_surfaces. Named
    explicitly here (per the development document) rather than re-derived
    from a fresh glob, since re-deriving it from the filesystem would just
    smuggle the old glob back in as the test's own oracle.
    """
    previously_globbed = {
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/EVIDENCE_MODEL.md",
        "docs/GOALBOOK.md",
        "docs/HERDR_HOH_IMPLEMENTATION_HANDOFF.md",
        "docs/HERDR_INTEGRATION.md",
        "docs/K1_CACHE_PROFILE.md",
        "docs/LIMITATIONS.md",
        "docs/OPERATIONS.md",
        "docs/QUELLENCHECK.md",
        "docs/RECOVERY.md",
    }
    data = cc.load_ledger()
    declared = {s.get("path") for s in (data.get("claim_surfaces") or []) if isinstance(s, dict)}
    missing = previously_globbed - declared
    assert not missing, f"previously-globbed file(s) missing from claim_surfaces: {sorted(missing)}"


def test_no_hardcoded_limitations_sentences_in_checker():
    """Criterion K5: no hardcoded list of the eleven LIMITATIONS.md sentences
    this run reconciled, or their line numbers, anywhere in
    tools/check_claims.py -- mirroring test_resolver_has_no_hardcoded_site_list
    above: text via substring search, line numbers via an AST int-literal
    walk so a legitimate unrelated integer (a timeout, a loop bound) is never
    mistaken for a smuggled site list.
    """
    source = (REPO_ROOT / "tools" / "check_claims.py").read_text(encoding="utf-8")

    distinguishing_substrings = [
        "Once the merge produced 3 contradictory statements",
        "Once it left 5 coverage anchors pointing at content",
        "across 2 merges total",
        "lost an iteration each to",
        "yields 0 entries under",
        "6 of 15 acceptance criteria",
        "the receipt-derived count was",
        "discriminates` flag was 0",
        "Quoting \"5 of 13\"",
        "carry an inflated counter for",
    ]
    for needle in distinguishing_substrings:
        assert needle not in source, f"found hardcoded LIMITATIONS.md sentence text in the checker: {needle!r}"

    forbidden_line_numbers = {142, 143, 144, 157, 162, 172, 175, 204, 205, 208, 226}
    tree = ast.parse(source, filename="check_claims.py")
    int_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
    }
    hardcoded = forbidden_line_numbers & int_literals
    assert not hardcoded, f"found reconciled LIMITATIONS.md line numbers as literal constants: {hardcoded}"


def test_reconciliation_reports_both_directions(tmp_path):
    """Criterion K6: reconciliation reports both directions, against two
    independent fixtures.

    Fixture 1 (unbound): a surface file carries a number-bearing sentence
    with no covering claims/not_claims entry at all -- it must show up in
    "unbound".
    Fixture 2 (vanished): a claims entry's anchor_digest was recorded against
    a sentence that has since been edited out of the surface file -- it must
    show up in "vanished", and the edited file's own (digit-free) replacement
    text must not leak into "unbound" too.
    """
    # Fixture 1: unbound.
    unbound_root = tmp_path / "unbound"
    _write_fixture(
        unbound_root,
        "fixture.md",
        ["# Fixture", "", "This step retries 4 times before giving up.", ""],
    )
    unbound_data = {"claims": [], "not_claims": []}
    unbound_surface = {"path": "fixture.md", "scope": "whole file", "why": "test fixture"}
    unbound_result = cc.reconcile_surface(unbound_data, unbound_surface, repo_root=unbound_root)
    assert unbound_result["vanished"] == []
    assert any(
        u["line"] == 3 and "retries 4 times" in u["text"] for u in unbound_result["unbound"]
    ), f"expected the retries sentence to be reported unbound: {unbound_result['unbound']!r}"

    # Fixture 2: vanished.
    vanished_root = tmp_path / "vanished"
    original_sentence = "This step retries 5 times before giving up."
    original_digest = cc.compute_anchor_digest(original_sentence)
    _write_fixture(vanished_root, "fixture.md", ["# Fixture", "", original_sentence, ""])
    # The statement is edited away -- content changed, no digit left behind,
    # so it must not also register as a fresh "unbound" sentence.
    _write_fixture(
        vanished_root,
        "fixture.md",
        ["# Fixture", "", "This step no longer mentions retries at all.", ""],
    )
    vanished_data = {
        "claims": [
            {
                "id": "C-TEST",
                "text": original_sentence,
                "where": "fixture.md:3",
                "status": "INTENT",
                "evidence": [],
                "note": "fixture",
                "anchor_digest": original_digest,
            }
        ],
        "not_claims": [],
    }
    vanished_surface = {"path": "fixture.md", "scope": "whole file", "why": "test fixture"}
    vanished_result = cc.reconcile_surface(vanished_data, vanished_surface, repo_root=vanished_root)
    assert vanished_result["unbound"] == []
    assert any(
        v["id"] == "C-TEST" and v["kind"] == "claim" for v in vanished_result["vanished"]
    ), f"expected C-TEST to be reported vanished: {vanished_result['vanished']!r}"


def test_reconciliation_writes_nothing(tmp_path):
    """Criterion K7: reconciliation is read-only. Hashing a fixture surface's
    files before and after running the full reconciliation report against it
    yields identical hashes.
    """
    root = tmp_path / "readonly"
    _write_fixture(
        root,
        "fixture.md",
        ["# Fixture", "", "This step retries 3 times before giving up.", "", "Some other prose."],
    )
    data = {
        "claim_surfaces": [{"path": "fixture.md", "scope": "whole file", "why": "test fixture"}],
        "claims": [],
        "not_claims": [],
    }

    def _hash_tree() -> dict[str, str]:
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = _hash_tree()
    report = cc.reconcile_report(data, repo_root=root)
    after = _hash_tree()

    assert before == after, "reconciliation modified a file under the fixture surface's directory"
    assert report  # sanity: the report actually examined the declared surface


def test_claims_json_declares_reconciliation_requirement():
    """Criterion K9: a consumer can determine, from CLAIMS.json's own data
    alone, which files require reconciliation when changed -- no prose
    parsing needed. O41 already had a prose note saying as much and it did
    not prevent the defect (docs/LIMITATIONS.md limit 9); this is the same
    statement made machine-readable.
    """
    data = cc.load_ledger()
    surfaces = data.get("claim_surfaces") or []
    assert surfaces, "no claim_surfaces to check the dependency against"

    requires_reconciliation = [
        s["path"] for s in surfaces if isinstance(s, dict) and s.get("requires_reconciliation_on_change") is True
    ]
    assert requires_reconciliation, (
        "no surface declares requires_reconciliation_on_change -- the dependency is not "
        "machine-readable from claim_surfaces alone"
    )
    assert set(requires_reconciliation) == {s["path"] for s in surfaces}, (
        "every declared claim surface must require reconciliation on change -- a surface "
        "silently exempted from that field would only ever be found by reading prose"
    )

    policy = data.get("claim_surface_policy")
    assert isinstance(policy, dict) and policy.get("on_change"), (
        "claim_surface_policy must independently name the dependency at the manifest level"
    )


def test_undeclared_claim_surface_heuristic_detects_new_document(tmp_path):
    """Criterion K14: the undeclared-claim-surface heuristic detects a
    genuinely undeclared, number-bearing document, and that detection is
    advisory only.

    Three things are shown against one fixture repo: the heuristic reports
    the undeclared document; the same document can never leak into
    check_coverage's FAIL-level problems (coverage_targets only iterates
    *declared* claim_surfaces, so an undeclared file is never even examined);
    and the `check undeclared-surfaces` CLI subcommand still exits 0 with
    that same undeclared document standing in for the fixture's ledger.
    """
    root = tmp_path / "repo"
    _write_fixture(root, "README.md", ["# Fixture repo", "", "Nothing notable here."])
    _write_fixture(
        root,
        "docs/UNDECLARED.md",
        ["# Undeclared doc", "", "This project ships 9 undocumented widgets."],
    )
    data = {
        "claim_surfaces": [
            {"path": "README.md", "scope": "whole file", "why": "fixture root doc"}
        ],
        "claims": [],
        "not_claims": [],
    }

    warnings = cc.find_undeclared_claim_surfaces(data, repo_root=root)
    assert "docs/UNDECLARED.md" in warnings, f"expected the undeclared doc to be warned about: {warnings!r}"

    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = root
        coverage_problems = cc.check_coverage(data)
    finally:
        cc.REPO_ROOT = original_repo_root
    assert not any("UNDECLARED.md" in p for p in coverage_problems), (
        f"the undeclared document leaked into check_coverage's FAIL-level problems: {coverage_problems!r}"
    )

    fixture_claims_json = root / "CLAIMS.json"
    fixture_claims_json.write_text(json.dumps(data), encoding="utf-8")
    original_claims_json = cc.CLAIMS_JSON
    try:
        cc.REPO_ROOT = root
        cc.CLAIMS_JSON = fixture_claims_json
        with pytest.raises(SystemExit) as excinfo:
            cc.cmd_check("undeclared-surfaces")
        assert excinfo.value.code == 0, (
            "`check undeclared-surfaces` must exit 0 even when it has something to warn about"
        )
    finally:
        cc.REPO_ROOT = original_repo_root
        cc.CLAIMS_JSON = original_claims_json


def test_undeclared_claim_surface_heuristic_no_false_positive(tmp_path):
    """Criterion K15: the heuristic stays silent for (a) a document already
    declared in claim_surfaces even though it carries digits, and (b) a
    document with no number-bearing sentence at all -- both must produce
    zero warnings, or the warning channel would be noisy enough to be
    ignored.
    """
    root = tmp_path / "repo"
    _write_fixture(root, "README.md", ["# Fixture repo", "", "Nothing notable here."])
    _write_fixture(
        root,
        "docs/ALREADY_DECLARED.md",
        ["# Already declared", "", "This project ships 9 undocumented widgets."],
    )
    _write_fixture(
        root,
        "docs/NO_DIGITS.md",
        ["# No digits here", "", "A perfectly ordinary sentence with no numbers at all."],
    )
    data = {
        "claim_surfaces": [
            {"path": "README.md", "scope": "whole file", "why": "fixture root doc"},
            {"path": "docs/ALREADY_DECLARED.md", "scope": "whole file", "why": "fixture doc"},
        ],
        "claims": [],
        "not_claims": [],
    }

    warnings = cc.find_undeclared_claim_surfaces(data, repo_root=root)
    assert "docs/ALREADY_DECLARED.md" not in warnings, (
        f"a declared surface must never be warned about even though it carries digits: {warnings!r}"
    )
    assert "docs/NO_DIGITS.md" not in warnings, (
        f"a digit-free document must never be warned about: {warnings!r}"
    )


def test_home_paths_guard_scans_claims_test_file(tmp_path):
    """Criterion K16: check_home_paths()'s scanned-file set now includes
    tests/test_claims_anchors.py -- the new test file this run owns -- in
    addition to CLAIMS.json, CLAIMS.md and tools/check_claims.py.

    Structural half: the guard's own source references the new constant.
    Behavioural half: a needle planted in a fixture standing in for
    CLAIMS_ANCHORS_TEST is actually caught by check_home_paths(), proving the
    file is genuinely read and checked, not merely named in a list nobody
    iterates. REPO_ROOT is monkeypatched only for the duration of the call,
    and only so the fixture path resolves cleanly through the guard's own
    `path.relative_to(REPO_ROOT)` formatting -- CLAIMS_JSON, CLAIMS_MD and
    this module's own __file__ are left pointing at their real locations,
    since none of them trip the home-path check (`check home-paths` already
    passes on the real ledger) and so never reach that relative_to() call.
    """
    assert cc.CLAIMS_ANCHORS_TEST == REPO_ROOT / "tests" / "test_claims_anchors.py"
    source = inspect.getsource(cc.check_home_paths)
    assert "CLAIMS_ANCHORS_TEST" in source, (
        "check_home_paths() no longer references the claims-anchors test file constant"
    )

    needle = chr(47) + "home" + chr(47)
    fixture_path = tmp_path / "planted_test_claims_anchors.py"
    fixture_path.write_text(f'PLANTED = "{needle}fixture-user/project"\n', encoding="utf-8")

    original_repo_root = cc.REPO_ROOT
    original_test_const = cc.CLAIMS_ANCHORS_TEST
    try:
        cc.REPO_ROOT = tmp_path
        cc.CLAIMS_ANCHORS_TEST = fixture_path
        problems = cc.check_home_paths()
    finally:
        cc.REPO_ROOT = original_repo_root
        cc.CLAIMS_ANCHORS_TEST = original_test_const

    assert any("planted_test_claims_anchors.py" in p for p in problems), (
        f"check_home_paths() did not catch a planted home-path needle in CLAIMS_ANCHORS_TEST: {problems!r}"
    )


def test_home_paths_guard_passes_on_real_repository_files():
    """Criterion K17: the home-paths guard passes directly against the real
    repository's own four scanned files -- CLAIMS.json, CLAIMS.md,
    tools/check_claims.py and tests/test_claims_anchors.py -- not only
    against the K16 fixture. K16 proves the mechanism catches an injected
    needle; this proves the real files are actually clean, turning what was
    a hand-verified claim (re-run and re-reported every iteration) into a
    standing, repeatable check. A failure here names a real, in-scope
    literal to fix -- never a reason to narrow check_home_paths()'s scanned
    set.
    """
    problems = cc.check_home_paths()
    assert problems == [], f"home-path pattern(s) found in the real repository: {problems!r}"


def test_reconcile_cli_writes_nothing_against_real_ledger():
    """Criterion K18: running the reconcile CLI against the real repository
    modifies none of CLAIMS.json, CLAIMS.md, docs/LIMITATIONS.md or
    tests/test_claims_anchors.py. K7 proves this against a synthetic
    fixture; this proves it against the actual checked-in ledger and
    documents, turning a one-off manual hash comparison (repeated in every
    iteration's evidence report) into a standing check.

    Runs the CLI as a real subprocess -- the same interface an operator or a
    scheduler would use -- rather than calling reconcile_report() in-process,
    so a write introduced anywhere between argument parsing and the report
    functions would still be caught.
    """
    import subprocess

    watched = [
        REPO_ROOT / "CLAIMS.json",
        REPO_ROOT / "CLAIMS.md",
        REPO_ROOT / "docs" / "LIMITATIONS.md",
        REPO_ROOT / "tests" / "test_claims_anchors.py",
    ]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "check_claims.py"), "reconcile"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # 0 (fully reconciled) or 1 (something to reconcile) are both legitimate
    # report outcomes -- what this test asserts is that neither writes.
    assert proc.returncode in (0, 1), (
        f"reconcile crashed instead of reporting: rc={proc.returncode} stderr={proc.stderr!r}"
    )

    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    assert before == after, "reconcile modified a real, checked-in file -- it must be read-only"


# --------------------------------------------------------------------------
# R1a: resolvability classification (PUBLIC/LOCAL_ONLY/EXTERNAL). runs/** is
# never exported, so every receipt:/run:/receiptcount:/discriminated:
# reference is unresolvable by a reader of the public repository -- that is
# not a defect, but it must be visible, not silently indistinguishable from a
# file: reference a reader really can check. The tests below exercise
# classify_reference()'s derivation (never a per-reference hand annotation),
# the who/when requirement on LOCAL_ONLY references, the CLAIMS.md rendering,
# the `check resolvability` CLI subcommand, and the additive shape of the
# real ledger after this run's changes.
# --------------------------------------------------------------------------


def test_every_evidence_reference_has_a_resolvability_class():
    """K1: every evidence reference in the real CLAIMS.json parses to a
    resolvability class from exactly {PUBLIC, LOCAL_ONLY, EXTERNAL}.
    """
    data = cc.load_ledger()
    seen_any = False
    for c in data.get("claims") or []:
        for e in c.get("evidence") or []:
            seen_any = True
            cls = cc.classify_reference(e)
            assert cls in cc.VALID_RESOLVABILITY_CLASSES, (
                f"{c.get('id')}: {e} classified as {cls!r}, not one of {cc.VALID_RESOLVABILITY_CLASSES}"
            )
    assert seen_any, "fixture assumption broken: no evidence references found in the real ledger"


def test_resolvability_class_changes_when_export_rule_changes(tmp_path):
    """K2: the class is derived, not typed. On a temporary fixture ledger
    (never the real CLAIMS.json), changing which export rule applies to a
    path changes the derived class of a reference into it -- in both
    directions: adding a new exclusion rule turns a PUBLIC reference
    LOCAL_ONLY, and removing one turns a LOCAL_ONLY reference PUBLIC.
    """
    fixture = {
        "schema": "hoh-claims/1",
        "claims": [
            {
                "id": "C-001",
                "text": "fixture claim",
                "where": "README.md:1",
                "status": "SUPPORTED",
                "evidence": ["file:custom/notes.md:1"],
            }
        ],
        "not_claims": [],
    }
    fixture_path = tmp_path / "CLAIMS.fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    loaded = cc.load_ledger(fixture_path)
    ref = loaded["claims"][0]["evidence"][0]

    # Default rule table: "custom/**" is not excluded by any rule -> PUBLIC.
    assert cc.classify_reference(ref) == cc.RESOLVABILITY_PUBLIC

    # Adding a new export-exclusion rule matching "custom/**" flips the SAME
    # reference to LOCAL_ONLY, with no other code touched.
    rules_with_new_exclusion = cc.DEFAULT_EXPORT_EXCLUSION_RULES + [
        ("fixture-custom-exclusion", lambda p: p.startswith("custom/"))
    ]
    assert cc.classify_reference(ref, export_exclusion_rules=rules_with_new_exclusion) == (
        cc.RESOLVABILITY_LOCAL_ONLY
    )

    # The reverse direction: a path normally excluded (dogfood/**, via the
    # 'internal-working-document' rule) becomes PUBLIC once that rule is
    # removed from the table -- proving the class tracks the rule table, not
    # a hardcoded judgment about this particular path.
    dogfood_ref = "file:dogfood/specs/example.md:1"
    assert cc.classify_reference(dogfood_ref) == cc.RESOLVABILITY_LOCAL_ONLY
    rules_without_dogfood_rule = [
        (name, predicate)
        for name, predicate in cc.DEFAULT_EXPORT_EXCLUSION_RULES
        if name != "internal-working-document"
    ]
    assert cc.classify_reference(dogfood_ref, export_exclusion_rules=rules_without_dogfood_rule) == (
        cc.RESOLVABILITY_PUBLIC
    )


def test_run_evidence_forms_are_always_local_only():
    """K3: every receipt:/run:/receiptcount:/discriminated: reference in the
    real CLAIMS.json classifies as LOCAL_ONLY, with zero exceptions --
    runs/** is never exported.
    """
    data = cc.load_ledger()
    checked = 0
    for c in data.get("claims") or []:
        for e in c.get("evidence") or []:
            form = e.split(":", 1)[0]
            if form in cc.LOCAL_ONLY_EVIDENCE_FORMS:
                checked += 1
                assert cc.classify_reference(e) == cc.RESOLVABILITY_LOCAL_ONLY, (
                    f"{c.get('id')}: {e} must classify LOCAL_ONLY"
                )
    assert checked > 0, (
        "fixture assumption broken: no receipt:/run:/receiptcount:/discriminated: references found"
    )


def test_local_only_references_state_who_and_when():
    """K4: every LOCAL_ONLY reference states what resolved it, by whom, and
    when. A fixture claim whose resolution text only says evidence exists is
    rejected by check_resolvability(); one with a real 'by <actor> on <date>'
    statement is accepted. Also asserts this holds for the real ledger's
    claims carrying LOCAL_ONLY evidence.
    """
    valid_claim = {
        "id": "C-TEST-VALID",
        "text": "fixture",
        "where": "README.md:1",
        "status": "SUPPORTED",
        "evidence": ["receipt:x/y"],
        "local_only_resolution": "Resolved by the fixture operator on 2026-01-01 against runs/x/receipts/.",
    }
    invalid_claim = {
        "id": "C-TEST-INVALID",
        "text": "fixture",
        "where": "README.md:1",
        "status": "SUPPORTED",
        "evidence": ["receipt:x/y"],
        "local_only_resolution": "Evidence exists.",
    }

    problems, _ = cc.check_resolvability({"claims": [valid_claim], "not_claims": []})
    assert not any("C-TEST-VALID" in p for p in problems), problems

    problems, _ = cc.check_resolvability({"claims": [invalid_claim], "not_claims": []})
    assert any("C-TEST-INVALID" in p and "who/when" in p for p in problems), problems

    # A claim with no local_only_resolution at all (and no who/when embedded
    # in its evidence strings either) is rejected the same way.
    missing_claim = {
        "id": "C-TEST-MISSING",
        "text": "fixture",
        "where": "README.md:1",
        "status": "SUPPORTED",
        "evidence": ["receipt:x/y"],
    }
    problems, _ = cc.check_resolvability({"claims": [missing_claim], "not_claims": []})
    assert any("C-TEST-MISSING" in p and "who/when" in p for p in problems), problems

    # Real ledger: every claim carrying LOCAL_ONLY evidence states who/when
    # (the 39 claims this run populated with local_only_resolution).
    data = cc.load_ledger()
    real_problems, _ = cc.check_resolvability(data)
    who_when_problems = [p for p in real_problems if "who/when" in p]
    assert who_when_problems == [], who_when_problems


def test_claims_md_shows_class_within_claim_block():
    """K5: CLAIMS.md shows each evidence reference's resolvability class
    inside that claim's own rendered block, not in a separate legend or
    summary table.
    """
    data = cc.load_ledger()
    rendered = cc.render_markdown(data)

    lowered = rendered.lower()
    assert "## resolvability" not in lowered
    assert "## legend" not in lowered
    assert "resolvability legend" not in lowered

    def _claim_block(cid: str) -> str:
        marker = f"**{cid}**"
        start = rendered.index(marker)
        rest = rendered[start + len(marker):]
        candidates = [i for i in (rest.find("\n**C-"), rest.find("\n## Not claims")) if i != -1]
        end = min(candidates) if candidates else len(rest)
        return rest[:end]

    # C-018 (receiptcount:d1=48) is LOCAL_ONLY and shows both the class and
    # the who/when resolution right next to its own evidence bullet.
    c018_block = _claim_block("C-018")
    assert "LOCAL_ONLY" in c018_block
    assert "resolved by" in c018_block

    # C-001 (file:src/hoh/herdr.py:52) is PUBLIC and shows that too, inside
    # its own block.
    c001_block = _claim_block("C-001")
    assert "PUBLIC" in c001_block


def test_resolvability_report_shows_counts_and_non_public_claims():
    """K6: `check resolvability` reports per-class reference counts and
    lists every claim id whose evidence rests entirely on non-PUBLIC
    references. Checked both in-process and via the actual CLI subcommand,
    so a report that exists but was never wired to the CLI cannot pass this.
    """
    data = cc.load_ledger()
    problems, report_lines = cc.check_resolvability(data)
    assert problems == [], problems

    report_text = "\n".join(report_lines)
    for cls in (cc.RESOLVABILITY_PUBLIC, cc.RESOLVABILITY_LOCAL_ONLY, cc.RESOLVABILITY_EXTERNAL):
        assert cls in report_text, report_text

    non_public_section = report_text.split("Claims resting entirely")[-1]
    # C-018 rests entirely on receiptcount: (LOCAL_ONLY) evidence.
    assert "C-018" in non_public_section
    # C-001 rests on file:src/hoh/herdr.py:52 (PUBLIC) -- not entirely non-PUBLIC.
    assert "C-001" not in non_public_section

    import subprocess

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "check_claims.py"), "check", "resolvability"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Resolvability reference counts" in proc.stdout
    assert "Claims resting entirely on non-PUBLIC evidence" in proc.stdout


def test_all_local_only_fixture_passes_resolvability_check(tmp_path):
    """K7: a LOCAL_ONLY reference does not make the checker fail -- a
    fixture ledger whose every evidence reference is LOCAL_ONLY (with valid
    who/when statements) exits 0 under `check resolvability`, both in
    process and through the real CLI entry point.
    """
    resolution = "Resolved by the fixture operator on 2026-01-01 against runs/x/receipts/."
    fixture = {
        "schema": "hoh-claims/1",
        "claims": [
            {
                "id": "C-001",
                "text": "fixture claim one",
                "where": "README.md:1",
                "status": "SUPPORTED",
                "evidence": ["receiptcount:x=3"],
                "local_only_resolution": resolution,
            },
            {
                "id": "C-002",
                "text": "fixture claim two",
                "where": "README.md:2",
                "status": "SUPPORTED",
                "evidence": ["run:x/1", "discriminated:x/1=1/2"],
                "local_only_resolution": resolution,
            },
            {
                "id": "C-003",
                "text": "fixture claim three",
                "where": "README.md:3",
                "status": "SUPPORTED",
                "evidence": ["file:A02_BEFUNDE.md:1"],
                "local_only_resolution": resolution,
            },
        ],
        "not_claims": [],
    }
    for c in fixture["claims"]:
        for e in c["evidence"]:
            assert cc.classify_reference(e) == cc.RESOLVABILITY_LOCAL_ONLY, e

    problems, report_lines = cc.check_resolvability(fixture)
    assert problems == [], problems
    assert any("LOCAL_ONLY: 4" in line for line in report_lines), report_lines

    import shutil
    import subprocess

    (tmp_path / "tools").mkdir()
    shutil.copy(REPO_ROOT / "tools" / "check_claims.py", tmp_path / "tools" / "check_claims.py")
    (tmp_path / "CLAIMS.json").write_text(json.dumps(fixture), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "check", "resolvability"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ledger_shape_stays_additive():
    """K8: the change is additive in shape on the real ledger, asserted
    without a predecessor (an arena holds exactly one tree and no history --
    O31). Every claim still has a status from the allowed set, every claim
    that carries evidence still carries a non-empty evidence list, and every
    evidence entry still carries its original form marker in addition to its
    now-derivable resolvability class.
    """
    data = cc.load_ledger()
    forms = ("test:", "file:", "receipt:", "run:", "cite:", "receiptcount:", "discriminated:")
    checked = 0
    for c in data.get("claims") or []:
        cid = c.get("id")
        assert c.get("status") in cc.VALID_STATUSES, cid
        evidence = c.get("evidence")
        if evidence is not None:
            assert isinstance(evidence, list) and evidence, (
                f"{cid}: 'evidence' present but empty -- lost its non-empty evidence list"
            )
            for e in evidence:
                checked += 1
                assert isinstance(e, str) and e.startswith(forms), (
                    f"{cid}: {e!r} lost its original evidence form marker"
                )
                cls = cc.classify_reference(e)
                assert cls in cc.VALID_RESOLVABILITY_CLASSES, f"{cid}: {e!r} -> {cls!r}"
    assert checked > 0, "fixture assumption broken: no evidence entries found on the real ledger"


# --------------------------------------------------------------------------
# D4i: an anchor whose target file was never exported is an environment gap,
# not a defect. d4h reclassified CLAIMS.json/CLAIMS.md as public-docs, and on
# its candidate's derived (71-file) export every one of 53 coverage problems
# plus the one remaining pytest failure had the same cause: the anchor's
# target file -- one of six internal working documents, EXCLUDE by rule --
# was never given to this check at all. resolve_anchor() folded that into
# the same plain FAIL as a real "statement moved or changed" defect. The
# tests below exercise the new ENVIRONMENT_GAP verdict this run adds, always
# through isolated tmp_path fixtures with cc.REPO_ROOT monkeypatched (never
# against this checkout's own runs/ tree or its six real, present docs --
# every declared claim_surfaces target in this checkout still exists, so the
# new path is never exercised by the real ledger here).
# --------------------------------------------------------------------------


def test_absent_anchor_target_yields_environment_gap(tmp_path):
    """K1: an anchor whose target file is entirely absent from the tree is
    reported carrying the ENVIRONMENT GAP marker and naming the missing
    file, not folded into the plain unresolved-anchor FAIL.
    """
    root = tmp_path / "repo"
    root.mkdir()
    missing_path = "docs/NEVER_EXPORTED.md"
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)

    # Direct resolver check: no file at all under repo_root for this path.
    result = cc.resolve_anchor(missing_path, digest, None, root)
    assert result.verdict == "ENVIRONMENT_GAP"
    assert result.line is None
    assert result.candidates == []

    data = {
        "claim_surfaces": [],
        "claims": [
            {
                "id": "C-GAP",
                "text": _FIXTURE_STATEMENT,
                "where": f"{missing_path}:3",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": digest,
            }
        ],
        "not_claims": [],
    }
    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = root
        problems = cc.check_coverage(data)
    finally:
        cc.REPO_ROOT = original_repo_root

    assert len(problems) == 1
    assert cc.ENVIRONMENT_GAP_MARKER in problems[0], problems
    assert missing_path in problems[0], problems
    assert "C-GAP" in problems[0], problems


def test_changed_content_anchor_still_plain_fail(tmp_path):
    """K2: an anchor whose target file exists but whose content genuinely
    changed is still reported as a plain FAIL, with no ENVIRONMENT GAP
    marker -- the criterion that separates a real repair from a switch-off.
    Without this test, K1's repair would be indistinguishable from turning
    the anchor check off altogether.
    """
    root = tmp_path / "repo"
    present_path = "docs/PRESENT_BUT_CHANGED.md"
    digest = cc.compute_anchor_digest(_FIXTURE_STATEMENT)
    _write_fixture(
        root,
        present_path,
        ["# Present", "", "This step retries the request up to 9 times before giving up.", ""],
    )

    result = cc.resolve_anchor(present_path, digest, None, root)
    assert result.verdict == "FAIL"

    data = {
        "claim_surfaces": [],
        "claims": [
            {
                "id": "C-CHANGED",
                "text": _FIXTURE_STATEMENT,
                "where": f"{present_path}:3",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": digest,
            }
        ],
        "not_claims": [],
    }
    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = root
        problems = cc.check_coverage(data)
    finally:
        cc.REPO_ROOT = original_repo_root

    assert len(problems) == 1
    assert cc.ENVIRONMENT_GAP_MARKER not in problems[0], problems
    assert "no longer resolves" in problems[0], problems
    assert "C-CHANGED" in problems[0], problems


def test_absent_and_changed_anchor_counted_separately_in_one_run(tmp_path):
    """K3: one check_coverage() invocation over a fixture carrying one
    absent-target anchor and one changed-content anchor reports exactly one
    ENVIRONMENT-GAP-marked problem and exactly one unmarked FAIL -- proving
    the code does not take the same branch for both. Two fixtures checked
    separately could both pass while a single (wrong) branch handled both
    cases; this is the one-invocation proof the spec calls for.
    """
    root = tmp_path / "repo"
    present_path = "docs/PRESENT_TOGETHER.md"
    missing_path = "docs/MISSING_TOGETHER.md"
    statement_present = "This step retries the request up to 3 times before giving up."
    statement_missing = "This step retries the request up to 4 times before giving up."
    digest_present = cc.compute_anchor_digest(statement_present)
    digest_missing = cc.compute_anchor_digest(statement_missing)
    _write_fixture(
        root,
        present_path,
        ["# Present", "", "This step retries the request up to 9 times before giving up.", ""],
    )
    assert not (root / missing_path).exists()

    data = {
        "claim_surfaces": [],
        "claims": [
            {
                "id": "C-TOGETHER-CHANGED",
                "text": statement_present,
                "where": f"{present_path}:3",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": digest_present,
            },
        ],
        "not_claims": [
            {
                "id": "N-TOGETHER-GAP",
                "text": statement_missing,
                "where": f"{missing_path}:5",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": digest_missing,
            }
        ],
    }
    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = root
        problems = cc.check_coverage(data)
    finally:
        cc.REPO_ROOT = original_repo_root

    assert len(problems) == 2, problems
    marked = [p for p in problems if cc.ENVIRONMENT_GAP_MARKER in p]
    unmarked = [p for p in problems if cc.ENVIRONMENT_GAP_MARKER not in p]
    assert len(marked) == 1, problems
    assert len(unmarked) == 1, problems
    assert missing_path in marked[0] and "N-TOGETHER-GAP" in marked[0], marked
    assert "C-TOGETHER-CHANGED" in unmarked[0], unmarked


def test_environment_gap_anchor_count_is_derived_and_not_literal_53(tmp_path):
    """K4: the emitted count of absent-target anchor problems equals a count
    independently derived from the fixture data (never a stored literal),
    and tools/check_claims.py's source carries no literal integer 53 -- the
    number that happened to be this run's motivating measurement on d4h's
    candidate export, and which this run must never bake in as a constant.
    """
    root = tmp_path / "repo"
    present_path = "docs/PRESENT_COUNT.md"
    _write_fixture(root, present_path, ["# Present", "", "Nothing here matches any anchor.", ""])

    # Three distinct absent-target anchors -- distinct from 53 on purpose, so
    # a hardcoded 53 could never accidentally satisfy this fixture's count.
    absent_specs = [
        ("docs/GONE_A.md", "Statement A retries 11 times before giving up."),
        ("docs/GONE_B.md", "Statement B retries 12 times before giving up."),
        ("docs/GONE_C.md", "Statement C retries 13 times before giving up."),
    ]
    changed_text = "Statement D retries 14 times before giving up."

    claims = [
        {
            "id": f"C-GONE-{i}",
            "text": text,
            "where": f"{path}:1",
            "status": "INTENT",
            "evidence": [],
            "anchor_digest": cc.compute_anchor_digest(text),
        }
        for i, (path, text) in enumerate(absent_specs)
    ]
    claims.append(
        {
            "id": "C-CHANGED-D",
            "text": changed_text,
            "where": f"{present_path}:3",
            "status": "INTENT",
            "evidence": [],
            "anchor_digest": cc.compute_anchor_digest(changed_text),
        }
    )
    data = {"claim_surfaces": [], "claims": claims, "not_claims": []}

    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = root
        problems = cc.check_coverage(data)
    finally:
        cc.REPO_ROOT = original_repo_root

    marked = [p for p in problems if cc.ENVIRONMENT_GAP_MARKER in p]
    unmarked = [p for p in problems if cc.ENVIRONMENT_GAP_MARKER not in p]

    # The independently derived count: distinct absent target paths this
    # fixture's own construction data does not create on disk under root.
    expected_gap_count = len({path for path, _ in absent_specs if not (root / path).is_file()})
    assert expected_gap_count == len(absent_specs) == 3
    assert len(marked) == expected_gap_count, problems
    assert len(unmarked) == 1, problems

    source = (REPO_ROOT / "tools" / "check_claims.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="check_claims.py")
    int_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
    }
    assert 53 not in int_literals, "found the literal 53 as an int constant in check_claims.py"


def test_runs_root_environment_gap_mechanism_is_untouched(tmp_path):
    """K5: the pre-existing runs/ environment-gap mechanism
    (_runs_root_missing() and the receipt:/run:/receiptcount:/discriminated:
    ENVIRONMENT GAP behavior) is untouched by this run's anchor fix. A
    fixture repo root with no runs/ directory at all still marks every one
    of those four evidence forms with ENVIRONMENT GAP, exactly as before --
    a change that merged the two mechanisms into one and lost this would be
    a regression, not a simplification.
    """
    assert callable(cc._runs_root_missing)
    assert callable(cc._environment_gap_failure)

    root = tmp_path / "repo"
    root.mkdir()
    assert not (root / "runs").is_dir()

    data = {
        "claims": [
            {
                "id": "C-RUNS-GAP",
                "text": "fixture claim resting on runs/-evidence",
                "where": "README.md:1",
                "status": "SUPPORTED",
                "evidence": ["receipt:x/y", "run:x/1", "receiptcount:x=3", "discriminated:x/1=1/2"],
            }
        ],
        "not_claims": [],
    }

    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = root
        assert cc._runs_root_missing() is True
        problems = cc.check_evidence(data, need_pytest=False)
    finally:
        cc.REPO_ROOT = original_repo_root

    assert len(problems) == 4, problems
    assert all(cc.ENVIRONMENT_GAP_MARKER in p for p in problems), problems


def test_check_claims_has_no_anchor_target_allowlist():
    """K6: tools/check_claims.py contains no allowlist of the six affected
    filenames (docs/HERDR_HOH_IMPLEMENTATION_HANDOFF.md, docs/QUELLENCHECK.md,
    docs/K1_CACHE_PROFILE.md, dogfood/specs/d2b-licenses.md,
    HOH_ACCEPTANCE_REPORT.md, A02_BERICHT.md) or any anchor ids introduced by
    this run's anchor-resolution code, and the ENVIRONMENT_GAP decision is
    proven file-presence-derived, never a lookup.

    How this is checked, and why it would catch a list spelled differently
    than expected:

    1. A source scan scoped to exactly the functions this run adds or
       touches (AnchorResolution, resolve_anchor, _anchor_problem_message,
       cmd_check) -- not the whole module -- because three of the six real
       filenames already appear elsewhere in check_claims.py, legitimately,
       in the pre-existing (out-of-scope, untouched by this run)
       DEFAULT_EXPORT_EXCLUSION_RULES table that R1a added for a different
       purpose (export-resolvability classification of `file:` evidence
       references, not anchor targets). A whole-file scan would therefore
       false-positive on code this run never touches; scoping to this run's
       own functions keeps the assertion meaningful without being vacuous.
    2. The same scoped source is checked for any literal C-NNN/N-NNN anchor
       id pattern.
    3. A behavioral genericity proof: a fixture target filename built at
       test-run time from the test process's own pid and a fresh random
       integer -- a string no list written before this test executed could
       possibly contain -- still resolves to ENVIRONMENT_GAP. This is the
       part that would catch a list "spelled differently than expected": no
       matter how such a list were spelled (a different case, a regex, a
       hash of the name, a set of prefixes), it could not have anticipated
       this specific, only-just-generated name, so only a genuine
       Path.is_file() check can pass it.
    """
    six_affected_filenames = [
        "HERDR_HOH_IMPLEMENTATION_HANDOFF.md",
        "QUELLENCHECK.md",
        "K1_CACHE_PROFILE.md",
        "d2b-licenses.md",
        "HOH_ACCEPTANCE_REPORT.md",
        "A02_BERICHT.md",
    ]

    anchor_related_source = "\n".join(
        inspect.getsource(fn)
        for fn in (cc.AnchorResolution, cc.resolve_anchor, cc._anchor_problem_message, cc.cmd_check)
    )
    for name in six_affected_filenames:
        assert name not in anchor_related_source, (
            f"found the affected filename {name!r} hardcoded in this run's anchor-resolution code"
        )

    anchor_id_re = re.compile(r"\b[CN]-\d{2,4}\b")
    ids_in_scope = set(anchor_id_re.findall(anchor_related_source))
    assert not ids_in_scope, (
        f"found literal claim/not_claim ids hardcoded in this run's anchor-resolution code: {ids_in_scope}"
    )

    novel_name = f"docs/NEVER_SEEN_{os.getpid()}_{random.randint(0, 10**9)}.md"
    for name in six_affected_filenames:
        assert name not in novel_name
    digest = cc.compute_anchor_digest("An entirely unrelated fixture sentence about widgets.")
    never_created_root = REPO_ROOT / "tools" / "__d4i_never_created_fixture_root__"
    assert not never_created_root.exists()
    result = cc.resolve_anchor(novel_name, digest, None, never_created_root)
    assert result.verdict == "ENVIRONMENT_GAP", (
        f"a freshly generated, never-hardcoded filename did not resolve to ENVIRONMENT_GAP: {result.verdict!r}"
    )


# --------------------------------------------------------------------------
# d4i, iteration 2, added a test here for its own criterion 7 -- "CLAIMS.json
# and CLAIMS.md are unchanged -- record both sha256 values in the plan and
# assert they are unchanged. This run changes the checker, never the thing
# checked." -- as test_claims_ledger_files_are_byte_identical_to_recorded_sha256,
# hardcoding the two sha256 digests of CLAIMS.json and CLAIMS.md as measured
# at d4i's start. That sentence was true for the duration of d4i and nothing
# past it, but a hardcoded pytest function does not expire: it turned a
# per-run scope statement into a standing lock on a shared, cross-run
# artifact. d3f ran in the same batch, added its own limits with their own
# claims, and legitimately edited CLAIMS.json -- so the union of two
# individually-correct candidates was red, on a digest that could never
# again equal both runs' output at once. Updating the digests would only
# re-close the lock for d3g, which is specified to edit CLAIMS.json next;
# that is why the function is removed here rather than repaired.
#
# Nothing in this file replaces it. Scope -- "did this run touch the claims
# ledger" -- is a statement about one run's diff, and belongs in that run's
# own acceptance criteria, checked against digests recorded in that run's
# own plan -- not as a standing test in this repository.
# --------------------------------------------------------------------------


def _write_check_all_fixture_repo(root: Path, *, broken: bool, with_runs: bool) -> None:
    """Builds a minimal, runnable check_claims.py repo under root: a verbatim
    copy of the real tool, a schema-valid CLAIMS.json with no claim_surfaces
    (so coverage never needs a declared docs tree), a CLAIMS.md that agrees
    with a fresh regeneration, and a placeholder tests/test_claims_anchors.py
    so check_home_paths() finds something readable at every path it scans.

    `broken=True` adds a second claim with no 'note' -- a genuine schema
    defect that `check all` reports as a plain FAIL, never carrying the
    ENVIRONMENT_GAP_MARKER, in either regime. `with_runs=True` creates a real
    runs/<run>/receipts/<id>.json backing the first claim's receipt:
    evidence, so that reference resolves instead of reporting a gap.
    """
    import shutil

    (root / "tools").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "tools" / "check_claims.py", root / "tools" / "check_claims.py")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_claims_anchors.py").write_text(
        "# fixture placeholder for check_home_paths() -- not a real test file\n",
        encoding="utf-8",
    )

    claims = [
        {
            "id": "C-001",
            "text": "The fixture ledger cites one receipt as its only evidence.",
            "where": "runs/fixture-run/receipts",
            "status": "SUPPORTED",
            "evidence": ["receipt:fixture-run/basis"],
        }
    ]
    if broken:
        claims.append(
            {
                "id": "C-002",
                "text": "This claim is deliberately left without a note.",
                "where": "runs/fixture-run/receipts",
                "status": "INTENT",
                "evidence": [],
            }
        )
    data = {"schema": "hoh-claims/1", "claim_surfaces": [], "claims": claims, "not_claims": []}
    (root / "CLAIMS.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "CLAIMS.md").write_text(cc.render_markdown(data), encoding="utf-8")

    if with_runs:
        receipts_dir = root / "runs" / "fixture-run" / "receipts"
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "basis.json").write_text("{}", encoding="utf-8")


def _run_check_all(root: Path):
    import subprocess

    return subprocess.run(
        [sys.executable, str(root / "tools" / "check_claims.py"), "check", "all"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_check_all_reports_no_unmarked_failure_in_this_environment(tmp_path):
    """K8: the spanning invariant -- `check all` reports no FAIL: line
    without the ENVIRONMENT_GAP_MARKER -- holds in either regime, because the
    expectation is derived from whether this check directory has a
    top-level runs/ tree, never hardcoded as a literal exit code. d4i's
    version of this test asserted `check all` always exits 1, which is only
    true in a runs/-absent git-archive export; inside a checkout that
    carries runs/ and all six anchor targets, `check all` correctly exits 0
    -- nothing is unresolvable there, which is the criterion being
    satisfied, not violated.

    Builds two clean fixture repos under this one tmp_path -- one with a
    top-level runs/ directory, one without -- plus two negative-control
    fixtures that layer a genuine, unmarked schema defect (a claim missing
    its required 'note') onto each, and runs `check_claims.py check all`
    against all four as real subprocesses, in this one test invocation.
    """

    def _assert_no_unmarked_failure(fail_lines: list[str], *, runs_present: bool) -> None:
        unmarked = [line for line in fail_lines if cc.ENVIRONMENT_GAP_MARKER not in line]
        assert not unmarked, (
            f"found FAIL: line(s) without the {cc.ENVIRONMENT_GAP_MARKER} marker in the "
            f"runs/ {'present' if runs_present else 'absent'} regime (runs_present={runs_present}): "
            f"{unmarked!r}"
        )

    absent_root = tmp_path / "clean_absent"
    present_root = tmp_path / "clean_present"
    neg_absent_root = tmp_path / "broken_absent"
    neg_present_root = tmp_path / "broken_present"

    _write_check_all_fixture_repo(absent_root, broken=False, with_runs=False)
    _write_check_all_fixture_repo(present_root, broken=False, with_runs=True)
    _write_check_all_fixture_repo(neg_absent_root, broken=True, with_runs=False)
    _write_check_all_fixture_repo(neg_present_root, broken=True, with_runs=True)

    absent_proc = _run_check_all(absent_root)
    present_proc = _run_check_all(present_root)
    neg_absent_proc = _run_check_all(neg_absent_root)
    neg_present_proc = _run_check_all(neg_present_root)

    absent_fail_lines = [ln for ln in absent_proc.stdout.splitlines() if ln.startswith("FAIL:")]
    present_fail_lines = [ln for ln in present_proc.stdout.splitlines() if ln.startswith("FAIL:")]
    neg_absent_fail_lines = [ln for ln in neg_absent_proc.stdout.splitlines() if ln.startswith("FAIL:")]
    neg_present_fail_lines = [ln for ln in neg_present_proc.stdout.splitlines() if ln.startswith("FAIL:")]

    # Regime table, absent branch: `check all` must exit non-zero and report
    # at least one FAIL: line -- the environment-gap receipt: evidence above.
    assert absent_proc.returncode != 0, (
        f"expected `check all` to exit non-zero with runs/ absent (runs_present=False): "
        f"rc={absent_proc.returncode} stdout={absent_proc.stdout!r} stderr={absent_proc.stderr!r}"
    )
    assert absent_fail_lines, (
        f"expected at least one FAIL: line with runs/ absent (runs_present=False): "
        f"stdout={absent_proc.stdout!r}"
    )

    # The spanning invariant itself, both regimes, on the clean fixtures --
    # the "yields a pass in both" proof, from this one test invocation.
    _assert_no_unmarked_failure(absent_fail_lines, runs_present=False)
    _assert_no_unmarked_failure(present_fail_lines, runs_present=True)

    # Negative control: a FAIL without the marker must make the invariant
    # check itself fail -- proving this is a real check, not a no-op --
    # in both regimes.
    with pytest.raises(AssertionError):
        _assert_no_unmarked_failure(neg_absent_fail_lines, runs_present=False)
    with pytest.raises(AssertionError):
        _assert_no_unmarked_failure(neg_present_fail_lines, runs_present=True)


# --------------------------------------------------------------------------
# d4e: the public claim surface is the public export, not a glob.
#
# check_export_classification() is a *separate* check from
# find_undeclared_claim_surfaces() above, over a *separate* reference set --
# EXPORT_MANIFEST.json's own INCLUDE set, not a README.md/docs/** glob -- and
# it fails closed where the glob-based heuristic only ever warns. The tests
# below exercise that new check and its own regression test (an injected,
# undeclared INCLUDE path is reported by name), plus check_local_only_diversity(),
# which repairs O52 (r1a's 39 identical local_only_resolution sentences).
# --------------------------------------------------------------------------


def test_export_classification_passes_on_the_real_ledger():
    """Positive control: every real EXPORT_MANIFEST.json INCLUDE path is
    classified exactly once in the real CLAIMS.json -- either a declared
    claim_surfaces entry or a declared claim_surface_exclusions entry with a
    closed-vocabulary reason, never both, never neither.
    """
    manifest = json.loads((REPO_ROOT / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    data = cc.load_ledger()
    problems = cc.check_export_classification(manifest, data)
    assert problems == [], problems


def test_export_classification_reports_injected_unclassified_path_by_name():
    """Criterion 5 / this run's own regression test: an INCLUDE path added to
    a *fixture copy* of the manifest (the real manifest is only read, never
    mutated) that names no claim_surfaces entry and no claim_surface_exclusions
    entry is reported, by its exact path, as a problem. This is the check
    that keeps criterion 1 closed after this run ends: a future INCLUDE
    addition nobody declared must fail, not silently pass.
    """
    manifest = json.loads((REPO_ROOT / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    fixture_manifest = json.loads(json.dumps(manifest))  # deep copy, never touches the real dict
    fixture_manifest["entries"].append(
        {"path": "docs/ZZ_FIXTURE_NOT_REAL.md", "decision": "INCLUDE", "rule": "fixture"}
    )
    data = cc.load_ledger()
    problems = cc.check_export_classification(fixture_manifest, data)
    assert problems, "expected the injected unclassified path to be reported"
    assert any("docs/ZZ_FIXTURE_NOT_REAL.md" in p for p in problems), problems


def test_export_classification_rejects_a_path_declared_both_ways():
    """A path that is simultaneously a claim_surfaces entry and a
    claim_surface_exclusions entry is reported, not silently accepted as
    "covered enough" -- criterion 1 says "never both".
    """
    manifest = {"entries": [{"path": "README.md", "decision": "INCLUDE", "rule": "fixture"}]}
    data = {
        "claim_surfaces": [{"path": "README.md", "scope": "whole file", "why": "fixture"}],
        "claim_surface_exclusions": [{"path": "README.md", "reason": "ledger-itself"}],
    }
    problems = cc.check_export_classification(manifest, data)
    assert any("README.md" in p and "BOTH" in p for p in problems), problems


def test_export_classification_rejects_reason_outside_closed_vocabulary():
    """Criterion 2: a free-text or otherwise non-closed-vocabulary exclusion
    reason (including the specifically-forbidden 'prose') is rejected.
    """
    manifest = {"entries": [{"path": "docs/FIXTURE.md", "decision": "INCLUDE", "rule": "fixture"}]}
    for bad_reason in ("prose", "informational", "low-signal", ""):
        data = {
            "claim_surfaces": [],
            "claim_surface_exclusions": [{"path": "docs/FIXTURE.md", "reason": bad_reason}],
        }
        problems = cc.check_export_classification(manifest, data)
        assert any("docs/FIXTURE.md" in p for p in problems), (bad_reason, problems)


def test_export_classification_rejects_prose_md_excluded_instead_of_declared():
    """Criterion 3, exercised directly against check_export_classification():
    a prose `.md` INCLUDE path outside examples/minimal/recorded-rejection/
    that is excluded rather than declared a surface is reported -- and a
    `.md` path *inside* recorded-rejection/ is exempt (there are none on
    disk today, but the copied-evidence JSON/txt files there establish the
    exemption is for the whole directory, not just non-.md files in it).
    """
    manifest = {
        "entries": [
            {"path": "docs/PROSE_FIXTURE.md", "decision": "INCLUDE", "rule": "fixture"},
            {
                "path": "examples/minimal/recorded-rejection/FIXTURE.md",
                "decision": "INCLUDE",
                "rule": "fixture",
            },
        ]
    }
    data = {
        "claim_surfaces": [],
        "claim_surface_exclusions": [
            {"path": "docs/PROSE_FIXTURE.md", "reason": "pattern-data"},
            {"path": "examples/minimal/recorded-rejection/FIXTURE.md", "reason": "copied-evidence"},
        ],
    }
    problems = cc.check_export_classification(manifest, data)
    assert any("docs/PROSE_FIXTURE.md" in p for p in problems), problems
    assert not any("recorded-rejection/FIXTURE.md" in p for p in problems), problems


def test_local_only_diversity_passes_on_the_real_ledger():
    """Positive control: after this run's rewrite of the 39 claims that used
    to share r1a's one identical local_only_resolution sentence (O52), no
    two LOCAL_ONLY claims in the real ledger share a statement.
    """
    data = cc.load_ledger()
    problems = cc.check_local_only_diversity(data)
    assert problems == [], problems


def test_local_only_diversity_rejects_uniform_sentence_fixture():
    """This run's own regression test for criterion 7's negative control: a
    fixture ledger where every LOCAL_ONLY claim shares one identical
    local_only_resolution sentence -- exactly r1a's original defect -- must
    be rejected by check_local_only_diversity(). Without this fixture the
    diversity check would have the same defect it repairs: nothing proves it
    can ever fail.
    """
    same = "Resolved by the fixture operator on 2026-01-01 against runs/x/receipts/."
    claims = [
        {
            "id": f"C-F{i}",
            "text": "x",
            "where": "README.md:1",
            "status": "SUPPORTED",
            "evidence": [f"receiptcount:xfixture{i}={i + 1}"],
            "local_only_resolution": same,
        }
        for i in range(3)
    ]
    fixture = {"claims": claims, "not_claims": []}
    problems = cc.check_local_only_diversity(fixture)
    assert problems, "expected the uniform-sentence fixture to be rejected"
    assert all(cid in problems[0] for cid in ("C-F0", "C-F1", "C-F2")), problems


def test_local_only_diversity_ignores_claims_without_local_only_evidence():
    """A claim resting entirely on PUBLIC/EXTERNAL evidence needs no
    local_only_resolution at all, and sharing (or lacking) one is not a
    diversity problem -- check_resolvability() is what would require the
    field to exist in the first place; this check only polices duplication
    among claims that do carry one.
    """
    claims = [
        {
            "id": "C-P1",
            "text": "x",
            "where": "README.md:1",
            "status": "SUPPORTED",
            "evidence": ["file:README.md:1"],
        },
        {
            "id": "C-P2",
            "text": "y",
            "where": "README.md:1",
            "status": "SUPPORTED",
            "evidence": ["file:README.md:1"],
        },
    ]
    fixture = {"claims": claims, "not_claims": []}
    problems = cc.check_local_only_diversity(fixture)
    assert problems == [], problems


# --------------------------------------------------------------------------
# d4f -- a rendered claim reads as the sentence (or table row) it belongs to,
# reconstructed at render time via the existing digest-based resolve_anchor()
# -- never the stale `where` line -- with the anchored line's own raw
# content kept identifiable, and an honest fallback when the source is
# absent. See reconstruct_claim_text() in tools/check_claims.py.
# --------------------------------------------------------------------------


def test_reconstruct_claim_text_resolves_via_digest_and_pads_one_sentence_on_straddle(tmp_path):
    """reconstruct_claim_text() resolves through the digest -- never through
    a stale line number recorded in `where` -- and, for a line whose own
    content straddles a sentence boundary, pads the rendered window by
    exactly one more sentence on each side (clamped to the paragraph), not
    the whole paragraph.
    """
    para = [
        "Zero sentence begins the paragraph and stands alone quite firmly.",
        "Intro sentence stands alone completely here today for context.",
        "Second sentence opens right here and keeps going without any pause",
        "until it finally ends now. Third sentence begins immediately after",
        "that and finishes shortly. Fourth sentence closes the paragraph now.",
    ]
    _write_fixture(tmp_path, "docs/FIXTURE_STRADDLE.md", para)
    anchor_line = para[3]
    digest = cc.compute_anchor_digest(anchor_line)

    # `where` names a deliberately wrong line number -- resolution must
    # ignore it and go through the digest instead.
    rendered = cc.reconstruct_claim_text(
        anchor_line, "docs/FIXTURE_STRADDLE.md:999", digest, None, tmp_path
    )

    assert (
        "Second sentence opens right here and keeps going without any pause "
        "until it finally ends now." in rendered
    )
    assert (
        "Third sentence begins immediately after that and finishes shortly." in rendered
    )
    assert "Intro sentence stands alone completely here today for context." in rendered
    assert "Fourth sentence closes the paragraph now." in rendered
    assert "Zero sentence begins the paragraph" not in rendered, (
        "padding must stop at one sentence beyond the straddle, not reach the "
        f"whole paragraph: {rendered!r}"
    )
    assert cc.ANCHOR_OPEN in rendered and cc.ANCHOR_CLOSE in rendered
    inner = rendered.split(cc.ANCHOR_OPEN, 1)[1].split(cc.ANCHOR_CLOSE, 1)[0]
    assert inner == anchor_line


def test_reconstruct_claim_text_single_sentence_overlap_gets_no_padding(tmp_path):
    """The common case: a line entirely inside one sentence gets exactly
    that sentence, nothing more -- no padding, since there is no straddle.
    """
    para = [
        "Alpha sentence opens the paragraph and finishes right here today.",
        "Beta sentence is the only one that actually anchors this fixture.",
        "Gamma sentence closes the paragraph out on its own terms finally.",
    ]
    _write_fixture(tmp_path, "docs/FIXTURE_SINGLE.md", para)
    anchor_line = para[1]
    digest = cc.compute_anchor_digest(anchor_line)

    rendered = cc.reconstruct_claim_text(
        anchor_line, "docs/FIXTURE_SINGLE.md:1", digest, None, tmp_path
    )

    assert "Beta sentence is the only one that actually anchors this fixture." in rendered
    assert "Alpha sentence opens the paragraph" not in rendered
    assert "Gamma sentence closes the paragraph" not in rendered


def test_reconstruct_claim_text_table_row_strips_pipes_and_joins_with_em_dash(tmp_path):
    """A resolved line shaped like a markdown table row renders as its cells
    joined by an em dash -- a table row is a complete unit on one line, not
    a sentence to join across the other rows of the same table.
    """
    lines_ = [
        "| Header A | Header B |",
        "|---|---|",
        "| Fixture cell one | Fixture cell two spans more words here |",
    ]
    _write_fixture(tmp_path, "docs/FIXTURE_TABLE.md", lines_)
    row = lines_[2]
    digest = cc.compute_anchor_digest(row)

    rendered = cc.reconstruct_claim_text(row, "docs/FIXTURE_TABLE.md:1", digest, None, tmp_path)
    assert rendered == "Fixture cell one — Fixture cell two spans more words here"


def test_reconstruct_claim_text_missing_source_falls_back_honestly(tmp_path):
    """Criterion 6: a claim whose source file is absent (the export case, or
    a claim pointing into runs/) renders the raw line plus an explicit
    RECONSTRUCTION_FALLBACK_MARKER, never invents a sentence, and
    render_markdown() does not raise.
    """
    raw = "a fixture line whose source file will not exist on disk"
    digest = cc.compute_anchor_digest(raw)

    rendered = cc.reconstruct_claim_text(
        raw, "docs/DOES_NOT_EXIST.md:7", digest, None, tmp_path
    )
    assert raw in rendered
    assert cc.RECONSTRUCTION_FALLBACK_MARKER in rendered

    fixture_ledger = {
        "claims": [
            {
                "id": "C-FIX",
                "text": raw,
                "where": "docs/DOES_NOT_EXIST.md:7",
                "status": "UNSUPPORTED",
                "evidence": [],
                "anchor_digest": digest,
            }
        ],
        "not_claims": [],
    }
    original_repo_root = cc.REPO_ROOT
    try:
        cc.REPO_ROOT = tmp_path
        md = cc.render_markdown(fixture_ledger)
    finally:
        cc.REPO_ROOT = original_repo_root
    assert raw in md and cc.RECONSTRUCTION_FALLBACK_MARKER in md


def test_reconstruct_claim_text_no_digest_or_unparseable_where_falls_back(tmp_path):
    """A runs/-pointing claim (no anchor_digest, `where` with no trailing
    `:<line>`) is exactly the class criterion 6 names -- it must degrade to
    the raw line plus the fallback marker too, never raise.
    """
    raw = "a runs/-evidence claim with no line-level anchor at all"
    rendered = cc.reconstruct_claim_text(raw, "runs/d1/receipts", None, None, tmp_path)
    assert raw in rendered
    assert cc.RECONSTRUCTION_FALLBACK_MARKER in rendered


def test_render_markdown_real_ledger_no_claim_begins_mid_sentence(tmp_path):
    """Criterion 1, exercised directly as a pytest test (not only via the
    external acceptance script): render_markdown() produces zero
    claims-table rows whose displayed text starts with a continuation marker
    (`--`, a lowercase non-identifier word, a closing `**`, `)`, or `|`) --
    unless that row carries cc.RECONSTRUCTION_FALLBACK_MARKER, d4f's own
    honest signal that the source file was absent and no reconstruction was
    attempted.

    d8c: a plain `flagged == []` here goes red the moment a source file is
    missing (the export regime), because d4f's criterion 6 requires exactly
    that marked degradation. The invariant that holds across both regimes is
    checked below via _unmarked_mid_sentence(), in one invocation, against:
    the real ledger (no fallback expected anywhere today), an isolated
    present-source fixture (no fallback), an isolated fixture where one
    claim's source is absent from the tree (a marked fallback -- the export
    case), and a self-verifying negative control (source present, a genuine
    mid-sentence reconstruction, no marker) whose own check is wrapped in
    pytest.raises so a future no-op regression is itself caught failing to
    raise.
    """
    data = cc.load_ledger()
    md = cc.render_markdown(data)

    def _extract_texts(rendered_md: str) -> list[str]:
        rendered_lines = rendered_md.splitlines()
        row_start = rendered_lines.index("| id | status | text | where |") + 2
        extracted = []
        for ln in rendered_lines[row_start:]:
            if not ln.startswith("|"):
                break
            cells = re.split(r"(?<!\\)\|", ln)
            if len(cells) < 5:
                continue
            extracted.append(cells[3].strip().replace("\\|", "|"))
        return extracted

    texts = _extract_texts(md)

    def is_identifier_token(word: str) -> bool:
        w = word.rstrip(".,;:'\")")
        if re.search(r"\d", w):
            return True
        if "/" in w:
            return True
        if re.search(r"\.[A-Za-z]{1,4}(\b|\$)", w):
            return True
        return False

    def starts_mid_sentence(text: str) -> bool:
        if not text:
            return False
        if text.startswith("--") or text.startswith(")") or text.startswith("|"):
            return True
        if text.startswith("**"):
            # Nach ** entscheidet, ob Leerraum folgt. Ein schliessendes **
            # steht am Ende eines Fettbereichs und wird von Leerraum gefolgt;
            # ein oeffnendes wird von dem gefolgt, was fett wird.
            #
            # Die Vorfassung fragte stattdessen, ob innerhalb der naechsten 80
            # Zeichen ein zweites ** steht, und hielt damit jede Fettung, deren
            # Einleitung laenger als 80 Zeichen ist, faelschlich fuer eine
            # Fortsetzung -- eingetreten am 2026-09-10 an C-142, dessen
            # Listenpunkt mit einer 102 Zeichen langen fetten Ueberschrift
            # beginnt. Der Rohtext war korrekt rekonstruiert, die Pruefung
            # falsch.
            #
            # Die erste Reparatur ersetzte das Zeichenfenster durch eine
            # Positivliste von acht Zeichen und handelte sich damit dieselbe
            # Fehlerklasse andersherum ein: `**„Zitat"**`, `***fett-kursiv***`,
            # `**~~weg~~**`, `**$HOME**` und ein Emoji wurden zu Fortsetzungen
            # erklaert. Ein adversariales Review fand zehn solche Stellen im
            # Repository -- darunter `„`, das deutsche oeffnende
            # Anfuehrungszeichen, das in der Liste schlicht fehlte. Eine
            # Positivliste faengt, was jemand aufgeschrieben hat.
            #
            # GRENZE, ausdruecklich: ein Fragment, das mit `**` beginnt, traegt
            # das Zeichen LINKS vom Delimiter nicht mehr -- genau das, was die
            # Frage entscheiden wuerde. `**bold**s of the counter ...` ist
            # deshalb hier nicht von einem Satzanfang zu unterscheiden und
            # wird durchgelassen. Das ist bewusst in Kauf genommen: die Klasse
            # ist im heutigen Renderer nicht erreichbar, weil die Satzgrenze
            # fast immer hinter Leerraum liegt. Wer sie schliessen will, muss
            # die Quellzeile ueber den Anker nachschlagen statt aus dem
            # Fragment zu raten.
            # Ein leeres ** am Textende bleibt eine Fortsetzung.
            rest = text[2:]
            if not rest:
                return True
            return rest[0].isspace()
        m = re.match(r"^([A-Za-z][A-Za-z0-9./'_-]*)", text)
        if not m:
            return False
        word = m.group(1)
        if word[0].isupper():
            return False
        return not is_identifier_token(word)

    def _unmarked_mid_sentence(items: list[str]) -> list[str]:
        return [
            t[:70]
            for t in items
            if starts_mid_sentence(t) and cc.RECONSTRUCTION_FALLBACK_MARKER not in t
        ]

    # ABGELEITET, nicht getippt. Diese Zeile stand als `== 122` und ist beim
    # naechsten katalogisierten Claim gefallen -- die fuenfte stehende Zahl, die
    # dieses Repository entfernt. d8cs Kriterium 6 hatte beide Wege erlaubt und
    # genau diese Gefahr benannt; hier ist sie eingetreten.
    assert len(texts) == len(cc.load_ledger()["claims"]), (
        f"rendered claim rows ({len(texts)}) must match the ledger's claim count "
        f"({len(cc.load_ledger()['claims'])})"
    )
    assert _unmarked_mid_sentence(texts) == []

    # Beide Richtungen der **-Unterscheidung, damit die Reparatur von 2026-09-10
    # nicht stillschweigend zurueckfaellt: ein oeffnendes ** mit langer
    # Einleitung darf NICHT als Fortsetzung gelten, ein schliessendes ** schon.
    _oeffnend = (
        "**Multi-day orchestrated operation is demonstrated; long-duration "
        "operation without intervention is not.** The campaign behind this "
        "paper ran 65 runs."
    )
    assert len(_oeffnend.split("**")[1]) > 80, (
        "die Einleitung muss laenger als das alte 80-Zeichen-Fenster sein, "
        "sonst prueft dieser Fall die Reparatur gar nicht"
    )
    assert not starts_mid_sentence(_oeffnend), (
        "ein oeffnendes ** mit Einleitung ueber 80 Zeichen wurde wieder als "
        "Fortsetzung gewertet -- die Regression von C-142 ist zurueck"
    )
    # Nur Leerraum nach ** heisst Fortsetzung. Jeder Fall hier war unter der
    # verworfenen Positivliste ein Falsch-Positiv; ein adversariales Review
    # fand am 2026-09-10 zehn solche Stellen im Repository selbst, `„` voran.
    for _anfang in (
        "**\u201eZitat\u201c am Satzanfang** und der Rest des Satzes.",
        "***fett-kursiv*** beginnt hier einen Satz.",
        "**~~ueberholt~~ ersetzt** durch die neue Fassung.",
        "**$HOME wird vom Guard abgelehnt**, mit Absicht.",
        "**\U0001F680 Startliste** deckt die letzten drei Schritte ab.",
        "**\u00c4nderungen am Exportpfad** muessen neu abgeleitet werden.",
        "**65 Laeufe** trugen 1.988 Belege.",
        "**\\_wortwoertlich\\_ gesetzt** von einem der Pruefer.",
    ):
        assert not starts_mid_sentence(_anfang), (
            f"{_anfang[:40]!r} beginnt einen Satz, wurde aber als Fortsetzung "
            "gewertet -- die verworfene Positivliste ist zurueck"
        )
    for _fortsetzung in (
        "** and the remainder of a sentence that began on an earlier line.",
        "**\tnach einem Tabulator geht derselbe Satz weiter.",
        "**\nund nach einem Zeilenumbruch ebenso.",
        "**",
    ):
        assert starts_mid_sentence(_fortsetzung), (
            f"{_fortsetzung[:30]!r} ist eine Fortsetzung und muss gemeldet werden"
        )

    def _render_with_root(ledger: dict, root: Path) -> str:
        original_repo_root = cc.REPO_ROOT
        try:
            cc.REPO_ROOT = root
            return cc.render_markdown(ledger)
        finally:
            cc.REPO_ROOT = original_repo_root

    # Regime: every claim's source is present -- no fallback anywhere.
    present_root = tmp_path / "present"
    present_root.mkdir()
    present_line = "Widgets ship with a five year warranty covering parts and labor."
    _write_fixture(present_root, "docs/PRESENT.md", ["# Present", "", present_line, ""])
    present_ledger = {
        "claims": [
            {
                "id": "C-D8C-PRESENT",
                "text": present_line,
                "where": "docs/PRESENT.md:1",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": cc.compute_anchor_digest(present_line),
            }
        ],
        "not_claims": [],
    }
    present_texts = _extract_texts(_render_with_root(present_ledger, present_root))
    assert _unmarked_mid_sentence(present_texts) == []

    # Regime: one claim's source is present, one is absent from the tree --
    # the export case. The absent one must degrade with the fallback marker,
    # and must not be reported as an unmarked mid-sentence begin even though
    # its raw text alone looks mid-sentence.
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    ok_line = "Batteries are replaced under warranty within the first two years."
    _write_fixture(missing_root, "docs/PRESENT_TOO.md", ["# Present too", "", ok_line, ""])
    gap_raw = "banana peel warnings were dropped from the shipped export entirely."
    missing_ledger = {
        "claims": [
            {
                "id": "C-D8C-OK",
                "text": ok_line,
                "where": "docs/PRESENT_TOO.md:1",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": cc.compute_anchor_digest(ok_line),
            },
            {
                "id": "C-D8C-GAP",
                "text": gap_raw,
                "where": "docs/ABSENT_FROM_EXPORT.md:9",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": cc.compute_anchor_digest(gap_raw),
            },
        ],
        "not_claims": [],
    }
    missing_texts = _extract_texts(_render_with_root(missing_ledger, missing_root))
    assert any(cc.RECONSTRUCTION_FALLBACK_MARKER in t for t in missing_texts), (
        "expected the missing-source claim to degrade with the fallback "
        f"marker: {missing_texts!r}"
    )
    assert _unmarked_mid_sentence(missing_texts) == []

    # Negative control: source present, reconstruction genuinely begins
    # mid-sentence, no marker -- a real misreconstruction. This must be
    # reported, not waved through -- pytest.raises below is what makes a
    # future reversion to a no-op check fail this test instead of passing it
    # silently.
    neg_root = tmp_path / "negative"
    neg_root.mkdir()
    neg_line = "banana crates were never mentioned anywhere in the final specification."
    _write_fixture(neg_root, "docs/NEG.md", ["# Neg", "", neg_line, ""])
    neg_ledger = {
        "claims": [
            {
                "id": "C-D8C-NEG",
                "text": neg_line,
                "where": "docs/NEG.md:1",
                "status": "INTENT",
                "evidence": [],
                "anchor_digest": cc.compute_anchor_digest(neg_line),
            }
        ],
        "not_claims": [],
    }
    neg_texts = _extract_texts(_render_with_root(neg_ledger, neg_root))
    with pytest.raises(AssertionError, match="C-D8C-NEG|banana crates"):
        neg_result = _unmarked_mid_sentence(neg_texts)
        assert neg_result == [], f"unmarked mid-sentence claim C-D8C-NEG: {neg_result}"


def test_render_markdown_never_mutates_claims_json():
    """Criterion 5: reconstruction is derived, not stored -- render_markdown()
    must never write anything back into the ledger it reads. Compares
    CLAIMS.json's own sha256 before and after an in-memory render_markdown()
    call (and a check_render() call, which also only reads).
    """
    before = hashlib.sha256(cc.CLAIMS_JSON.read_bytes()).hexdigest()
    data = cc.load_ledger()
    cc.render_markdown(data)
    cc.check_render(data)
    after = hashlib.sha256(cc.CLAIMS_JSON.read_bytes()).hexdigest()
    assert before == after


def test_sentences_in_scope_docstring_is_accurate_and_negative_control_rejected():
    """Criterion 10 + 10a (d4f amendment): _sentences_in_scope()'s docstring
    no longer claims 'no exerciser' or that no declared surface uses
    section-level scoping -- both false now (2 of the ledger's current
    claim_surfaces, CLAIMS.md and paper/AUDIT.md, use it) -- carries no
    standing literal equal to the current claim_surfaces count (derived
    here, not hardcoded: a corrected literal ages exactly like 'eleven'
    did), and still describes what the branch does. The negative control
    feeds the ORIGINAL stale wording through the same check and confirms it
    is rejected -- otherwise this test would pass vacuously for a docstring
    nobody touched.
    """
    data = cc.load_ledger()
    surfaces_count = len(data.get("claim_surfaces") or [])

    def problems_in(doc: str) -> list[str]:
        p = []
        if "no exerciser" in doc:
            p.append("still claims no exerciser")
        if re.search(r"no\s+(?:currently-declared|declared)\s+surface\s+does", doc):
            p.append("still claims no declared surface uses section-level scoping")
        if re.search(r"\b" + str(surfaces_count) + r"\b", doc):
            p.append(f"carries the current surface count {surfaces_count} as a standing literal")
        if "section-level scoping" not in doc:
            p.append("no longer describes section-level scoping")
        if "whole file" not in doc and "whole-file" not in doc:
            p.append("no longer describes the whole-file fallback")
        return p

    live_doc = cc._sentences_in_scope.__doc__ or ""
    live_problems = problems_in(live_doc)
    assert not live_problems, f"live docstring still has issues: {live_problems}"

    stale_doc = (
        "Number-bearing sentences in `path`, restricted to a surface's `scope`.\n\n"
        "`scope == \"whole file\"` is every qualifying line, unchanged from before\n"
        "surfaces existed. A `scope` given as a list of heading strings restricts\n"
        "the result to lines whose nearest preceding markdown heading is one of\n"
        "those -- section-level scoping, for a future surface that needs it; no\n"
        "currently-declared surface does, so this branch has no exerciser among\n"
        "today's eleven entries. Any other shape falls back to whole-file\n"
        "treatment rather than silently under-covering the surface.\n"
    )
    stale_problems = problems_in(stale_doc)
    assert stale_problems, (
        "negative control: the original no-exerciser/eleven-entries wording "
        "must be rejected by this same check, or criterion 10 is vacuous"
    )


def test_a_claim_quoting_a_source_line_is_still_checked_against_it():
    """The narrowing must not have removed the rule it narrowed.

    `audit_refs claims-against-code` demands a claim's anchor line appear in
    its first cited file. That is right for a claim that *quotes* source -- a
    moved source makes the quotation stale -- and a category error for a prose
    claim whose evidence is the program that produced a number. Only the second
    case was excused, and this pins that the first still fires.
    """
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    wurzel = _Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "audit_refs_probe", wurzel / "tools" / "audit_refs.py")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules.setdefault("audit_refs_probe", mod)
    spec.loader.exec_module(mod)

    quelle = __import__("inspect").getsource(mod.cmd_claims_against_code)
    assert "hier_verankert" in quelle, "the rule lost its discriminator"
    assert 'claim.get("where", "").split(":")[0] == path' in quelle, (
        "the rule no longer asks whether the claim is anchored in the file it "
        "cites, so it either fires on everything or on nothing")
    assert "anchor_digest no longer resolves anywhere in" in quelle, (
        "the rule itself is gone, not narrowed")
