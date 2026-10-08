"""Independent verification of the guard's union semantics (O235).

An operator-side relaxation of `~/.agents/hooks/*.txt` once silently removed
refusals from the product guard. The repair makes the shipped policy a
mandatory minimum and the operator's copy additive only. These tests check that
from the outside, without reusing the orchestrator's own guard tests:

1. a pattern only in the shipped copy is refused although the operator's copy
   exists and lacks it;
2. a pattern only in the operator's copy is refused (additive);
3. when the operator's and the shipped copy are the same file (or repeat the
   same line), the refusal is attributed to the shipped source;
4. a missing shipped file raises `PolicyUnavailable`, even if the operator's
   copy exists;
5. an operator line that does not compile, or an unreadable operator file,
   fails closed with `PolicyUnavailable`, naming the operator source;
6. `run_check` with a refused sentinel never starts a process.

Every case has a negative control that changes only the condition under test.

Isolation: all three pattern locations of `hoh.runner` are redirected into
`tmp_path` with `monkeypatch.setattr`; no test reads or writes the operator's
hook directory or any real `policy/` directory. The sentinels are made-up words
refused only by pattern files these tests write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hoh import runner
from hoh.contracts import AcceptanceCheck, Candidate
from hoh.runner import (
    HouseRuleViolation,
    PolicyUnavailable,
    assert_command_allowed,
    run_check,
)

HOUSE = "house-rules-patterns.txt"
DENY = "dangerous-patterns.txt"

#: (file name, shipped source label, operator source label)
KINDS = [
    pytest.param(HOUSE, "house rule", "house rule (operator)", id="house-rules"),
    pytest.param(DENY, "denylist", "denylist (operator)", id="denylist"),
]

SHIPPED_SENTINEL = "zqxshippedsentinel"
OPERATOR_SENTINEL = "zqxoperatorsentinel"
HARMLESS = "echo zqxharmlessword"
PLACEHOLDER = "# placeholder: no patterns\n"


def _write(folder: Path, lines: dict[str, list[str]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in (HOUSE, DENY):
        body = PLACEHOLDER + "".join(line + "\n" for line in lines.get(name, []))
        (folder / name).write_text(body, encoding="utf-8")


def _policy(tmp_path: Path, monkeypatch, *,
            shipped: dict[str, list[str]] | None = None,
            operator: dict[str, list[str]] | None = None,
            with_operator: bool = True) -> tuple[Path, Path]:
    """Build a shipped and an operator pattern directory under `tmp_path` and
    point `hoh.runner` at them. The repository location points at a directory
    that does not exist, so no real `policy/` can be found."""
    shipped_dir = tmp_path / "shipped"
    operator_dir = tmp_path / "operator"
    _write(shipped_dir, shipped or {})
    if with_operator:
        _write(operator_dir, operator or {})
    monkeypatch.setattr(runner, "_PACKAGE_PATTERNS", shipped_dir)
    monkeypatch.setattr(runner, "_HOME_PATTERNS", operator_dir)
    monkeypatch.setattr(runner, "_REPO_PATTERNS", tmp_path / "no-repo")
    _assert_isolated(tmp_path)
    return shipped_dir, operator_dir


def _assert_isolated(tmp_path: Path) -> None:
    """Every location the guard can read lies under this test's tmp_path."""
    for base in (runner._PACKAGE_PATTERNS, runner._HOME_PATTERNS,
                 runner._REPO_PATTERNS):
        assert Path(base).is_relative_to(tmp_path), base
    for name in (HOUSE, DENY):
        assert runner._pattern_file(name).is_relative_to(tmp_path)


def _violation(command: str) -> str:
    with pytest.raises(HouseRuleViolation) as exc:
        assert_command_allowed(command)
    return str(exc.value)


# --------------------------------------------------------------------------- #
# Item 1: the shipped policy is a mandatory minimum
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_1_a_shipped_pattern_is_refused_although_the_operator_copy_lacks_it(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _, operator_dir = _policy(tmp_path, monkeypatch,
                              shipped={name: [SHIPPED_SENTINEL]})
    # The operator's copy exists and does not carry the pattern (a relaxation).
    assert (operator_dir / name).is_file()
    assert SHIPPED_SENTINEL not in (operator_dir / name).read_text(encoding="utf-8")

    message = _violation(f"echo start; {SHIPPED_SENTINEL}")
    assert f"violates {shipped_label} '{SHIPPED_SENTINEL}'" in message
    assert "(operator)" not in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_1_control_without_the_shipped_pattern_the_command_passes(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _policy(tmp_path, monkeypatch, shipped={})
    assert_command_allowed(f"echo start; {SHIPPED_SENTINEL}")


# --------------------------------------------------------------------------- #
# Item 2: the operator's copy is additive
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_2_a_pattern_only_in_the_operator_copy_is_refused(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    shipped_dir, _ = _policy(tmp_path, monkeypatch,
                             operator={name: [OPERATOR_SENTINEL]})
    assert OPERATOR_SENTINEL not in (shipped_dir / name).read_text(encoding="utf-8")

    message = _violation(f"echo start; {OPERATOR_SENTINEL}")
    assert f"violates {operator_label} '{OPERATOR_SENTINEL}'" in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_2_control_without_the_operator_line_the_command_passes(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _policy(tmp_path, monkeypatch, operator={})
    assert_command_allowed(f"echo start; {OPERATOR_SENTINEL}")


# --------------------------------------------------------------------------- #
# Item 3: a pattern in both copies is attributed to the shipped source
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_3_operator_and_shipped_being_the_same_file_attributes_to_shipped(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    shipped_dir, _ = _policy(tmp_path, monkeypatch,
                             shipped={name: [SHIPPED_SENTINEL]},
                             with_operator=False)
    monkeypatch.setattr(runner, "_HOME_PATTERNS", shipped_dir)
    # Both lookups resolve to one and the same file.
    assert runner._operator_pattern_file(name) == runner._pattern_file(name)

    message = _violation(f"echo start; {SHIPPED_SENTINEL}")
    assert f"violates {shipped_label} '{SHIPPED_SENTINEL}'" in message
    assert "(operator)" not in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_3_identical_lines_in_distinct_files_attribute_to_shipped(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _policy(tmp_path, monkeypatch,
            shipped={name: [SHIPPED_SENTINEL]},
            operator={name: [SHIPPED_SENTINEL]})
    message = _violation(f"echo start; {SHIPPED_SENTINEL}")
    assert f"violates {shipped_label} '{SHIPPED_SENTINEL}'" in message
    assert "(operator)" not in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_3_control_the_line_only_in_the_operator_copy_names_the_operator(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    """Rev. 3a.2: the same setup with the line removed from the shipped copy;
    the refusal is then attributed to the operator source."""
    _policy(tmp_path, monkeypatch,
            shipped={},
            operator={name: [SHIPPED_SENTINEL]})
    message = _violation(f"echo start; {SHIPPED_SENTINEL}")
    assert f"violates {operator_label} '{SHIPPED_SENTINEL}'" in message


# --------------------------------------------------------------------------- #
# Item 4: a missing shipped file fails closed, the operator copy cannot stand in
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_4_a_missing_shipped_file_raises_although_the_operator_copy_exists(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    shipped_dir, operator_dir = _policy(tmp_path, monkeypatch)
    # Move the shipped file aside (nothing is deleted) so it is missing.
    (shipped_dir / name).rename(tmp_path / f"{name}.parked")
    assert not (shipped_dir / name).exists()
    assert (operator_dir / HOUSE).is_file() and (operator_dir / DENY).is_file()

    with pytest.raises(PolicyUnavailable) as exc:
        assert_command_allowed(HARMLESS)
    message = str(exc.value)
    assert "Guard pattern files are missing" in message
    assert str(shipped_dir / name) in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_4_control_with_the_shipped_file_present_the_command_passes(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    shipped_dir, _ = _policy(tmp_path, monkeypatch)
    assert (shipped_dir / name).is_file()
    assert_command_allowed(HARMLESS)


# --------------------------------------------------------------------------- #
# Item 5: a broken or unreadable operator file fails closed
# --------------------------------------------------------------------------- #

BROKEN_LINE = "zqx(unclosed"
FIXED_LINE = r"zqx\(unclosed"


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_5_an_operator_line_that_does_not_compile_fails_closed(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _policy(tmp_path, monkeypatch, operator={name: [OPERATOR_SENTINEL, BROKEN_LINE]})
    with pytest.raises(PolicyUnavailable) as exc:
        assert_command_allowed(HARMLESS)
    message = str(exc.value)
    assert f"{name}: {BROKEN_LINE!r} ({operator_label})" in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_5_control_the_same_line_made_compilable_loads(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _policy(tmp_path, monkeypatch, operator={name: [OPERATOR_SENTINEL, FIXED_LINE]})
    assert_command_allowed(HARMLESS)
    # The repaired operator file is really in force.
    message = _violation(f"echo start; {OPERATOR_SENTINEL}")
    assert f"violates {operator_label} '{OPERATOR_SENTINEL}'" in message


def _make_unreadable(operator_dir: Path, name: str, how: str, tmp_path: Path) -> None:
    target = operator_dir / name
    target.rename(tmp_path / f"{name}.operator.parked")   # moved aside, not deleted
    if how == "directory":
        target.mkdir()
    else:
        dangling = tmp_path / "nowhere" / "missing.txt"
        assert not dangling.exists()
        target.symlink_to(dangling)
        assert target.is_symlink() and not target.exists()


@pytest.mark.parametrize("how", ["directory", "dangling-symlink"])
@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_5_an_unreadable_operator_file_fails_closed(
        tmp_path, monkeypatch, name, shipped_label, operator_label, how):
    _, operator_dir = _policy(tmp_path, monkeypatch)
    _make_unreadable(operator_dir, name, how, tmp_path)

    with pytest.raises(PolicyUnavailable) as exc:
        assert_command_allowed(HARMLESS)
    message = str(exc.value)
    assert f"{name}: '<file unreadable>' ({operator_label})" in message


@pytest.mark.parametrize("name, shipped_label, operator_label", KINDS)
def test_5_control_a_readable_operator_file_in_the_same_place_loads(
        tmp_path, monkeypatch, name, shipped_label, operator_label):
    _, operator_dir = _policy(tmp_path, monkeypatch)
    assert (operator_dir / name).is_file()
    assert_command_allowed(HARMLESS)


# --------------------------------------------------------------------------- #
# Item 6: run_check never starts a refused command
# --------------------------------------------------------------------------- #

MARKER = "marker-must-not-exist"
#: Relative marker, creating command before the sentinel (Rev. 2, 3a.5).
COMMAND = f"touch {MARKER}; {SHIPPED_SENTINEL}"


def _arena(tmp_path: Path) -> Path:
    arena = tmp_path / "arena"
    arena.mkdir()
    return arena


def _run(arena: Path):
    check = AcceptanceCheck(check_id="K1", description="t", command=COMMAND,
                            expect_exit=0)
    candidate = Candidate(candidate_id="c1", repo_path=str(arena), commit="a" * 40,
                          tree_clean=True, tree_digest="d1")
    return run_check(check, candidate, run_id="r1", iteration=1, attempt=1,
                     cwd=arena)


def test_6_run_check_with_a_refused_sentinel_starts_no_process(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, shipped={DENY: [SHIPPED_SENTINEL]})
    arena = _arena(tmp_path)
    with pytest.raises(HouseRuleViolation) as exc:
        _run(arena)
    assert f"violates denylist '{SHIPPED_SENTINEL}'" in str(exc.value)
    assert not (arena / MARKER).exists(), "a process was started"
    assert not list(tmp_path.glob(".hoh-out-*.log")), "a transcript sink was opened"


def test_6_control_without_the_pattern_the_same_command_runs(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, shipped={})
    arena = _arena(tmp_path)
    receipt, _ = _run(arena)
    # The process really ran: the marker exists. The sentinel is not a
    # command on any machine, so bash reports "command not found" (127).
    assert (arena / MARKER).exists()
    assert receipt.runner_ok
    assert receipt.exit_code == 127
