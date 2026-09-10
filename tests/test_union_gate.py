"""Regression coverage for tools/union_gate.py.

This is the one exception to this campaign's `tests/` prohibition (see
DUG's specification): union_gate.py is this run's own artifact, and this is
its own regression test, distinct from -- and not a substitute for -- the
run's acceptance checks. Every fixture here is built by the test itself in
`tmp_path`; none of it touches the real repository's LICENSE, CITATION.cff,
CLAIMS.json or runs/ tree, matching the tool's own no-side-effects contract.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNION_GATE = REPO_ROOT / "tools" / "union_gate.py"
CHECK_CLAIMS = REPO_ROOT / "tools" / "check_claims.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))

import union_gate as ug  # noqa: E402

_LINE_RE = re.compile(r"^U([1-5]): (PASS|FAIL|SKIPPED)(?::\s*(.+))?$")
_U4_LINE_RE = re.compile(r"^U4: (PASS|FAIL|SKIPPED)(?::\s*(.+))?$")


def run_gate(repo_path: Path) -> tuple[subprocess.CompletedProcess, dict[str, re.Match]]:
    """Runs union_gate.py against repo_path and indexes its output by invariant number."""
    proc = subprocess.run(
        [sys.executable, str(UNION_GATE), str(repo_path)],
        capture_output=True, text=True, timeout=300,
    )
    lines: dict[str, re.Match] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        assert m, f"line does not match the output contract: {line!r}"
        lines[m.group(1)] = m
    return proc, lines


def git_init(repo_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_path), check=True)


def make_pyproject(repo_path: Path, *, license_val: str, license_files: str = '["LICENSE"]') -> None:
    (repo_path / "src" / "fx").mkdir(parents=True, exist_ok=True)
    (repo_path / "src" / "fx" / "__init__.py").write_text("", encoding="utf-8")
    (repo_path / "pyproject.toml").write_text(
        f'[project]\nname = "fx"\nversion = "0.0.1"\nlicense = "{license_val}"\n'
        f"license-files = {license_files}\n\n"
        '[build-system]\nrequires = ["setuptools>=77"]\nbuild-backend = "setuptools.build_meta"\n\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# --help and the five-line, in-order output contract
# --------------------------------------------------------------------------- #


def test_help_exits_zero_and_prints_usage():
    proc = subprocess.run(
        [sys.executable, str(UNION_GATE), "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()


def test_fixture_run_emits_exactly_five_lines_in_order(tmp_path):
    make_pyproject(tmp_path, license_val="MIT")
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "MIT"\n', encoding="utf-8")

    proc, lines = run_gate(tmp_path)
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    nums = [int(_LINE_RE.match(line).group(1)) for line in stdout_lines]
    assert nums == [1, 2, 3, 4, 5], stdout_lines
    for match in lines.values():
        if match.group(2) in ("FAIL", "SKIPPED"):
            assert match.group(3), f"{match.group(1)} {match.group(2)} carries no detail: {stdout_lines}"


# --------------------------------------------------------------------------- #
# U1 -- license statements agree (O27)
# --------------------------------------------------------------------------- #


def test_u1_passes_when_pyproject_citation_and_license_agree(tmp_path):
    make_pyproject(tmp_path, license_val="MIT")
    (tmp_path / "LICENSE").write_text("MIT License\n\nPermission is hereby granted...\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "MIT"\n', encoding="utf-8")

    _, lines = run_gate(tmp_path)
    assert lines["1"].group(2) == "PASS"


def test_u1_fails_naming_both_disagreeing_values(tmp_path):
    make_pyproject(tmp_path, license_val="Apache-2.0")
    (tmp_path / "LICENSE").write_text("All rights reserved. No license has been chosen.\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "UNVERIFIED"\n', encoding="utf-8")

    _, lines = run_gate(tmp_path)
    match = lines["1"]
    assert match.group(2) == "FAIL"
    detail = (match.group(3) or "").lower()
    assert "apache-2.0" in detail and "unverified" in detail
    assert "pyproject" in detail or "citation" in detail


def test_u1_skipped_when_citation_cff_is_missing(tmp_path):
    make_pyproject(tmp_path, license_val="MIT")
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    # deliberately no CITATION.cff

    _, lines = run_gate(tmp_path)
    match = lines["1"]
    assert match.group(2) == "SKIPPED"
    assert "citation.cff" in (match.group(3) or "").lower()


# --------------------------------------------------------------------------- #
# U2 -- no shipped file references an unshipped file (export survey)
# --------------------------------------------------------------------------- #


def test_u2_passes_when_every_link_resolves(tmp_path):
    (tmp_path / "README.md").write_text("See [notes](NOTES.md) for details.\n", encoding="utf-8")
    (tmp_path / "NOTES.md").write_text("Notes.\n", encoding="utf-8")
    git_init(tmp_path)

    _, lines = run_gate(tmp_path)
    assert lines["2"].group(2) == "PASS"


def test_u2_fails_naming_referencing_file_and_missing_target(tmp_path):
    (tmp_path / "README.md").write_text("See [internal notes](docs/INTERNAL.md) for details.\n", encoding="utf-8")
    git_init(tmp_path)  # docs/INTERNAL.md deliberately never created nor tracked

    _, lines = run_gate(tmp_path)
    match = lines["2"]
    assert match.group(2) == "FAIL"
    detail = match.group(3) or ""
    assert "README.md" in detail and "docs/INTERNAL.md" in detail


def test_u2_skipped_when_not_a_git_repository(tmp_path):
    (tmp_path / "README.md").write_text("No git repo here.\n", encoding="utf-8")
    # deliberately no git init

    _, lines = run_gate(tmp_path)
    match = lines["2"]
    assert match.group(2) == "SKIPPED"
    assert "git" in (match.group(3) or "").lower()


# --------------------------------------------------------------------------- #
# U3 -- built artifact matches the repo's own claims (O29)
# --------------------------------------------------------------------------- #


def test_u3_passes_when_wheel_ships_exactly_the_declared_files(tmp_path):
    make_pyproject(tmp_path, license_val="MIT", license_files='["LICENSE"]')
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "MIT"\n', encoding="utf-8")

    _, lines = run_gate(tmp_path)
    assert lines["3"].group(2) == "PASS"


def test_u3_fails_naming_the_undeclared_shipped_file(tmp_path):
    # The O29 shape: a glob declaration ships more than pyproject.toml names literally.
    make_pyproject(tmp_path, license_val="MIT", license_files='["LICENSE*"]')
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "LICENSE.OLD").write_text("All rights reserved.\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "MIT"\n', encoding="utf-8")

    _, lines = run_gate(tmp_path)
    match = lines["3"]
    assert match.group(2) == "FAIL"
    assert "LICENSE.OLD" in (match.group(3) or "")


def test_u3_skipped_when_no_pyproject_toml(tmp_path):
    (tmp_path / "README.md").write_text("Nothing to build here.\n", encoding="utf-8")

    _, lines = run_gate(tmp_path)
    match = lines["3"]
    assert match.group(2) == "SKIPPED"
    assert "pyproject" in (match.group(3) or "").lower()


# --------------------------------------------------------------------------- #
# U3's build input is a declared, packaging-derived set -- not the whole
# repository (DUG2: the copy was ~195x too wide and aborted on a transient
# path under runs/). These call build_wheel() directly, importing
# tools/union_gate.py as a module, rather than through the five-line CLI.
# --------------------------------------------------------------------------- #


def _make_buildable_repo(repo_path: Path) -> None:
    make_pyproject(repo_path, license_val="MIT")
    (repo_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (repo_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "MIT"\n', encoding="utf-8")


def test_build_wheel_excludes_a_decoy_directory_not_needed_by_the_package(tmp_path):
    _make_buildable_repo(tmp_path)
    decoy = tmp_path / "unrelated_stuff"
    decoy.mkdir()
    (decoy / "noise.txt").write_text("not part of the package\n", encoding="utf-8")

    work_dir = tmp_path / "_work"
    work_dir.mkdir()
    info = ug.build_wheel(tmp_path, work_dir)

    assert info.built, info.reason
    build_src = work_dir / "build_src"
    assert not (build_src / "unrelated_stuff").exists()
    # Relative to build_src, never the absolute path -- the same path-hazard
    # class as the runs/ check below: an ancestor directory name must never
    # be able to make this assertion trip.
    assert not any("unrelated_stuff" in p.relative_to(build_src).parts for p in build_src.rglob("*"))


def test_build_wheel_never_copies_a_runs_directory(tmp_path):
    _make_buildable_repo(tmp_path)
    arena = tmp_path / "runs" / "attic" / "arenas" / "pytest-of-sai"
    arena.mkdir(parents=True)
    (arena / "leftover.txt").write_text("parked arena debris\n", encoding="utf-8")

    work_dir = tmp_path / "_work"
    work_dir.mkdir()
    info = ug.build_wheel(tmp_path, work_dir)

    assert info.built, info.reason
    build_src = work_dir / "build_src"
    assert not (build_src / "runs").exists()
    # Relative to build_src, never the absolute path -- an absolute-path check
    # would false-positive whenever the harness's own scratch/arena directory
    # happens to contain a literal "runs" path segment above build_src,
    # independent of anything build_wheel() actually copied.
    assert not any("runs" in p.relative_to(build_src).parts for p in build_src.rglob("*"))


def test_build_wheel_skips_a_vanishing_dangling_symlink_and_names_it(tmp_path):
    _make_buildable_repo(tmp_path)
    dangling = tmp_path / "src" / "fx" / "extra_asset"
    dangling.symlink_to(tmp_path / "src" / "fx" / "does_not_exist")

    work_dir = tmp_path / "_work"
    work_dir.mkdir()
    info = ug.build_wheel(tmp_path, work_dir)

    assert info.built is True, info.reason
    assert "src/fx/extra_asset" in info.skipped_paths, info.skipped_paths


def test_u3_passes_when_the_repository_also_contains_noise_the_build_does_not_need(tmp_path):
    """The real-repository shape: a buildable package plus a decoy directory
    and a runs/ tree the build does not need -- U3 must still reach PASS,
    which is what could not happen before this change (SKIPPED on an
    aggregated, multi-megabyte shutil.Error instead)."""
    _make_buildable_repo(tmp_path)
    decoy = tmp_path / "unrelated_stuff"
    decoy.mkdir()
    (decoy / "noise.txt").write_text("not part of the package\n", encoding="utf-8")
    arena = tmp_path / "runs" / "attic" / "arenas" / "pytest-of-sai"
    arena.mkdir(parents=True)
    (arena / "leftover.txt").write_text("parked arena debris\n", encoding="utf-8")

    _, lines = run_gate(tmp_path)
    match = lines["3"]
    assert match.group(2) == "PASS", match.group(0)


# --------------------------------------------------------------------------- #
# every FAIL/SKIPPED reason is capped at a named budget (DUG2: an uncapped U3
# reason grew from 1.3 MB to 1.96 MB as runs/ grew)
# --------------------------------------------------------------------------- #


def test_reason_budget_is_a_named_constant_and_long_reasons_are_capped():
    assert isinstance(ug.REASON_BUDGET, int)
    assert ug.REASON_BUDGET > 0

    many_paths = [f"src/pkg/module_{i}.py" for i in range(5000)]
    forced_reason = "could not copy every declared path: " + "; ".join(many_paths)
    assert len(forced_reason) > ug.REASON_BUDGET

    failed = ug.Result.fail(forced_reason)
    assert failed.detail is not None
    assert len(failed.detail) <= ug.REASON_BUDGET
    assert re.search(r"omitted", failed.detail, re.IGNORECASE), failed.detail
    assert re.search(r"\d", failed.detail), failed.detail

    skipped = ug.Result.skipped(forced_reason)
    assert skipped.detail is not None
    assert len(skipped.detail) <= ug.REASON_BUDGET
    assert re.search(r"omitted", skipped.detail, re.IGNORECASE), skipped.detail


# --------------------------------------------------------------------------- #
# U4 -- every evidence reference in the claims ledger still resolves (O37)
# --------------------------------------------------------------------------- #


def test_u4_skipped_not_passed_when_runs_dir_absent(tmp_path):
    """The honesty check the whole tool exists for: no runs/ means SKIPPED, never PASS."""
    (tmp_path / "CLAIMS.json").write_text(
        '{"schema": "1", "claims": [], "not_claims": []}', encoding="utf-8"
    )
    # deliberately no runs/ directory anywhere under tmp_path

    proc = subprocess.run(
        [sys.executable, str(UNION_GATE), str(tmp_path)], capture_output=True, text=True
    )
    u4_lines = [line for line in proc.stdout.splitlines() if line.startswith("U4:")]
    assert u4_lines, proc.stdout
    match = _U4_LINE_RE.match(u4_lines[0])
    assert match, u4_lines[0]
    assert match.group(1) == "SKIPPED", u4_lines[0]
    reason = match.group(2) or ""
    assert reason.strip()
    assert "runs" in reason.lower()


def test_union_gate_never_imports_or_names_check_claims_internals():
    """U4 delegates only through check_claims.py's documented CLI."""
    src = UNION_GATE.read_text(encoding="utf-8")

    import_pat = re.compile(
        r"(?m)^\s*(?:import\s+check_claims\b|from\s+check_claims\s+import\b|import\s+.*\bcheck_claims\b)"
    )
    assert not import_pat.search(src)

    forbidden_names = [
        "resolve_evidence", "check_schema", "check_coverage", "check_a02",
        "check_render", "check_home_paths", "run_selftest", "CLAIMS_JSON",
        "CLAIMS_MD", "load_ledger", "run_selftest_extended",
    ]
    hits = [name for name in forbidden_names if name in src]
    assert not hits, hits

    assert "subprocess" in src
    assert "check_claims.py" in src or "check_claims" in src


def _make_check_claims_fixture(tmp_path: Path) -> None:
    """Scaffolding the two O37 checker-delegation tests below share: a
    private copy of tools/check_claims.py (never the real repo's, which
    other runs are actively changing), an empty runs/ tree so
    union_gate.py's U4 never SKIPs, and a tests/test_claims_anchors.py
    placeholder -- check_home_paths() now reads that path unconditionally as
    part of `check all`, and without it every run fails closed on a
    missing-file problem regardless of the anchor, which is the second
    historical break these tests exist to stop coupling to.
    """
    (tmp_path / "tools").mkdir(parents=True)
    shutil.copy(CHECK_CLAIMS, tmp_path / "tools" / "check_claims.py")
    (tmp_path / "runs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_claims_anchors.py").write_text(
        '"""Placeholder fixture file; check_home_paths() reads this unconditionally."""\n',
        encoding="utf-8",
    )


_ANCHOR_DIGEST_C001 = "96265d500064eecf8d22ac0086bf9b62c598bf29abbb2fbc044c0ed1644a7dfa"


def test_o37_shaped_moved_anchor_makes_u4_fail_not_skip(tmp_path):
    """O37: asserts the *difference* a merge-shaped edit makes to
    check_claims.py's reported problem set, plus the negative control that
    the difference is not there before the edit. A fixture that instead had
    to be checker-clean before the edit coupled the test to every input the
    checker happens to require today -- exactly what broke this test twice
    already (see the module docstring's history). Whatever else the checker
    reports, before or after, is irrelevant by construction here: only the
    set difference between the two runs is asserted on.
    """
    assert CHECK_CLAIMS.is_file(), "tools/check_claims.py must exist for this fixture to be meaningful"

    _make_check_claims_fixture(tmp_path)

    (tmp_path / "README.md").write_text("# fx\n\nThis project ships 42 tools today.\n", encoding="utf-8")
    claims = (
        '{"schema": "1", "claims": ['
        '{"id": "C-001", "text": "fixture claim", "where": "README.md:3", '
        '"status": "INTENT", "note": "anchors the number-bearing sentence on line 3", '
        f'"anchor_digest": "{_ANCHOR_DIGEST_C001}"}}'
        '], "not_claims": []}'
    )
    (tmp_path / "CLAIMS.json").write_text(claims, encoding="utf-8")

    render = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "render"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert render.returncode == 0, render.stdout + render.stderr

    before_all = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "check", "all"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    before_lines = {line for line in before_all.stdout.splitlines() if line.strip()}

    # Negative control: nothing has been duplicated yet, so U4 must not
    # report the merge-shaped failure this test exists to catch.
    _, lines = run_gate(tmp_path)
    assert lines["4"].group(2) != "FAIL", (
        "U4 must not FAIL before the merge-shaped edit -- the anchor has not been duplicated yet"
    )

    # The merge-shaped edit: an unrelated sentence lands above the anchored
    # one, and the anchored sentence is *duplicated* rather than relocated.
    # A pure position shift is not enough to reproduce O37: resolve_anchor()
    # matches by content digest against every line in the file, so a
    # statement that only shifted line numbers still resolves as PASS -- see
    # check_claims.py's own reconcile_surface docstring. Keeping a second
    # copy of the anchored sentence in place, instead of deleting the
    # original, is what makes the anchor genuinely AMBIGUOUS.
    (tmp_path / "README.md").write_text(
        "# fx\n\nThis project ships 42 tools today.\n\n"
        "An unrelated new sentence lands here first.\n\n"
        "This project ships 42 tools today.\n",
        encoding="utf-8",
    )

    after_all = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "check", "all"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert after_all.returncode != 0, "the fixture must reproduce the O37 defect: " + after_all.stdout
    after_lines = {line for line in after_all.stdout.splitlines() if line.strip()}

    new_problems = after_lines - before_lines
    assert any("C-001" in p and "AMBIGUOUS" in p for p in new_problems), (
        "the duplicate-anchor edit must introduce a new AMBIGUOUS problem for C-001, "
        f"got new problems: {sorted(new_problems)}"
    )

    _, lines = run_gate(tmp_path)
    assert lines["4"].group(2) == "FAIL", (
        "U4 must FAIL (not SKIPPED) once check_claims.py genuinely fails and is available"
    )


def test_o37_fixture_tolerates_an_additional_unresolvable_claim(tmp_path):
    """Criterion 6 -- robustness against a checker that has grown new
    requirements: add a second claim with a reference that can never
    resolve, something this fixture does not otherwise supply, and confirm
    the set-difference technique above still isolates exactly the
    duplication-caused C-001 AMBIGUOUS problem, even though `check all` now
    fails on both the before and after runs for an unrelated, constant
    reason. That is what demonstrates the coupling to a checker-clean start
    is actually gone, not merely patched for today's set of checker inputs.
    """
    assert CHECK_CLAIMS.is_file(), "tools/check_claims.py must exist for this fixture to be meaningful"

    _make_check_claims_fixture(tmp_path)

    (tmp_path / "README.md").write_text("# fx\n\nThis project ships 42 tools today.\n", encoding="utf-8")
    claims = (
        '{"schema": "1", "claims": ['
        '{"id": "C-001", "text": "fixture claim", "where": "README.md:3", '
        '"status": "INTENT", "note": "anchors the number-bearing sentence on line 3", '
        f'"anchor_digest": "{_ANCHOR_DIGEST_C001}"}}, '
        '{"id": "C-002", "text": "a claim whose reference can never resolve", '
        '"where": "README.md:99", "status": "INTENT", "note": "deliberately unresolvable"}'
        '], "not_claims": []}'
    )
    (tmp_path / "CLAIMS.json").write_text(claims, encoding="utf-8")

    render = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "render"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert render.returncode == 0, render.stdout + render.stderr

    before_all = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "check", "all"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    before_lines = {line for line in before_all.stdout.splitlines() if line.strip()}

    (tmp_path / "README.md").write_text(
        "# fx\n\nThis project ships 42 tools today.\n\n"
        "An unrelated new sentence lands here first.\n\n"
        "This project ships 42 tools today.\n",
        encoding="utf-8",
    )

    after_all = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "check_claims.py"), "check", "all"],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    after_lines = {line for line in after_all.stdout.splitlines() if line.strip()}

    new_problems = after_lines - before_lines
    assert any("C-001" in p and "AMBIGUOUS" in p for p in new_problems), (
        "the set-difference technique must still isolate the duplication-caused C-001 "
        f"AMBIGUOUS problem even though the fixture no longer starts clean: {sorted(new_problems)}"
    )


# --------------------------------------------------------------------------- #
# U5 -- test suite and linter are green on the combined state
# --------------------------------------------------------------------------- #


def make_test_repo(tmp_path: Path, test_body: str) -> Path:
    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "test_x.py").write_text(test_body, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "tools").mkdir()
    return tmp_path


def test_u5_passes_when_pytest_and_ruff_are_green(tmp_path):
    make_test_repo(tmp_path, "def test_ok():\n    assert 1 + 1 == 2\n")

    _, lines = run_gate(tmp_path)
    assert lines["5"].group(2) == "PASS"


def test_u5_fails_naming_the_broken_test_suite(tmp_path):
    make_test_repo(tmp_path, "def test_broken():\n    assert 1 + 1 == 3\n")

    _, lines = run_gate(tmp_path)
    match = lines["5"]
    assert match.group(2) == "FAIL"
    detail = (match.group(3) or "").lower()
    assert "pytest" in detail or "test" in detail


def test_u5_skipped_when_no_tests_directory(tmp_path):
    (tmp_path / "README.md").write_text("Nothing to test here.\n", encoding="utf-8")

    _, lines = run_gate(tmp_path)
    match = lines["5"]
    assert match.group(2) == "SKIPPED"
    assert "tests" in (match.group(3) or "").lower()


# --------------------------------------------------------------------------- #
# SKIPPED never masks a FAIL, and never forces a red exit on its own
# --------------------------------------------------------------------------- #


def _make_skip_u4_repo(tmp_path: Path, readme_body: str) -> Path:
    # No runs/ anywhere -> U4 is always SKIPPED in this fixture.
    (tmp_path / "README.md").write_text(readme_body, encoding="utf-8")
    git_init(tmp_path)
    return tmp_path


def test_skipped_invariant_does_not_mask_a_real_fail(tmp_path):
    _make_skip_u4_repo(tmp_path, "See [gone](docs/GONE.md).\n")

    proc, lines = run_gate(tmp_path)
    assert lines["4"].group(2) == "SKIPPED"
    assert lines["2"].group(2) == "FAIL"
    assert proc.returncode != 0, "a FAIL must make the exit code non-zero even with another SKIPPED"


def test_skipped_invariant_does_not_force_a_red_exit(tmp_path):
    _make_skip_u4_repo(tmp_path, "Nothing unusual here.\n")

    proc, lines = run_gate(tmp_path)
    assert lines["4"].group(2) == "SKIPPED"
    assert not any(match.group(2) == "FAIL" for match in lines.values())
    assert proc.returncode == 0, "a lone SKIPPED must not turn the exit code red"
    assert "SKIPPED" in proc.stdout, "a SKIPPED invariant must still be visible on a green exit"


# --------------------------------------------------------------------------- #
# no side effects: the tool writes nothing outside its own stdout
# --------------------------------------------------------------------------- #


def _snapshot(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_running_the_gate_leaves_the_fixture_byte_identical(tmp_path):
    make_pyproject(tmp_path, license_val="MIT")
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text('cff-version: 1.2.0\nlicense: "MIT"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("Nothing unusual.\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    git_init(tmp_path)

    before = _snapshot(tmp_path)
    subprocess.run(
        [sys.executable, str(UNION_GATE), str(tmp_path)], capture_output=True, text=True, timeout=300
    )
    after = _snapshot(tmp_path)

    diff = set(before) ^ set(after) | {k for k in before if before.get(k) != after.get(k)}
    assert before == after, f"the fixture's files changed after running union_gate.py: {diff}"
