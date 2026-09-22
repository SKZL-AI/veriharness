"""O200: a strict check sees the interpreter HoH was installed into.

The first autonomous P1-16 run was rejected twice on the shipped example's K3,
`python3 -m pytest -q test_greet.py`, because inside the strict sandbox
`python3` was `/usr/bin/python3` -- the system interpreter, with no pytest --
on the candidate and on the baseline alike. These cases read the answer back
from the real runner under real bubblewrap, not from the spec object.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hoh import runner
from hoh.contracts import AcceptanceCheck, Candidate, Outcome
from hoh.sandbox import BubblewrapSandbox, Isolation

needs_bwrap = pytest.mark.skipif(
    BubblewrapSandbox().unavailable() is not None,
    reason=f"no usable sandbox: {BubblewrapSandbox().unavailable()}",
)


def _run(cmd: str, tmp_path: Path):
    check = AcceptanceCheck(check_id="T1", description="toolchain", command=cmd,
                            expect_exit=0)
    cand = Candidate(candidate_id="c", repo_path=str(tmp_path), commit="0" * 40,
                     tree_clean=True, tree_digest="d")
    return runner.run_check(check, cand, run_id="r", iteration=1, attempt=1,
                            cwd=tmp_path, isolation=Isolation.STRICT)


def test_the_toolchain_is_this_interpreter_and_nothing_under_usr():
    binds, path = runner.interpreter_toolchain()
    if Path(sys.prefix).resolve().is_relative_to(Path("/usr")):
        assert binds == () and path == ""
        return
    assert Path(sys.prefix).resolve() in binds
    assert path.split(":")[0] == str(Path(sys.prefix).resolve() / "bin")
    for b in binds:
        assert not b.is_relative_to(Path("/usr")), b


@needs_bwrap
def test_a_strict_check_runs_under_the_interpreter_hoh_runs_under(tmp_path):
    """Read back from inside the sandbox: which interpreter answered."""
    receipt, log = _run("python3 -c 'import sys; print(sys.prefix)'", tmp_path)
    assert receipt.outcome(0) is Outcome.PASS, log
    assert str(Path(sys.prefix).resolve()) in log


@needs_bwrap
def test_a_strict_check_can_use_a_package_that_interpreter_has(tmp_path):
    """The exact shape of the example's K3."""
    pytest.importorskip("pytest")
    receipt, log = _run("python3 -m pytest --version", tmp_path)
    assert receipt.outcome(0) is Outcome.PASS, log


@needs_bwrap
def test_the_negative_control_without_the_bind_the_system_interpreter_answers(
        tmp_path, monkeypatch):
    """Without this, the cases above would also pass against a sandbox that
    had always shown the operator's interpreter."""
    if Path(sys.prefix).resolve().is_relative_to(Path("/usr")):
        pytest.skip("this interpreter is the system one; nothing to distinguish")
    monkeypatch.setattr(runner, "interpreter_toolchain", lambda: ((), ""))
    receipt, log = _run("python3 -c 'import sys; print(sys.prefix)'", tmp_path)
    assert str(Path(sys.prefix).resolve()) not in log


@needs_bwrap
def test_the_home_directory_stays_invisible(tmp_path):
    """Binding the interpreter must not carry the rest of the home directory
    in: bwrap binds exactly the paths named. Probed from inside Python because
    the arena guard refuses a command that names an absolute path outside the
    arena -- correctly -- before the sandbox runs at all."""
    binds, _ = runner.interpreter_toolchain()
    home = Path.home()
    under_home = [b for b in binds if b.is_relative_to(home)]
    if not under_home:
        pytest.skip("no bound interpreter lives in the home directory")
    parts = "+".join(repr(x) for x in ("/", *home.parts[1:]))
    receipt, log = _run(
        "python3 -c \"import os; p=os.path.join(" + parts.replace("+", ",")
        + "); print(sorted(os.listdir(p)))\"", tmp_path)
    assert receipt.outcome(0) is Outcome.PASS, log
    visible = eval(log.strip().splitlines()[-1])
    expected = {b.relative_to(home).parts[0] for b in under_home}
    assert set(visible) <= expected, f"leaked into the sandbox: {set(visible) - expected}"


@needs_bwrap
def test_the_receipt_names_the_interpreter_observed_from_inside(tmp_path):
    """O203. The interpreter a strict check used is recorded on the receipt,
    as the prologue saw it before the check ran -- not inferred from a doctor
    probe taken earlier or from the runner's own prefix."""
    receipt, log = _run("true", tmp_path)
    seen = receipt.isolation.observed_python3
    assert seen and seen != "none", receipt.isolation
    if not Path(sys.prefix).resolve().is_relative_to(Path("/usr")):
        assert Path(seen).parent == Path(sys.prefix).resolve() / "bin", seen


@needs_bwrap
def test_the_negative_control_without_the_bind_the_receipt_says_so(
        tmp_path, monkeypatch):
    """Without this the case above would also pass for a field that always
    carried the runner's prefix."""
    if Path(sys.prefix).resolve().is_relative_to(Path("/usr")):
        pytest.skip("this interpreter is the system one; nothing to distinguish")
    monkeypatch.setattr(runner, "interpreter_toolchain", lambda: ((), ""))
    receipt, _ = _run("true", tmp_path)
    assert not receipt.isolation.observed_python3.startswith(
        str(Path(sys.prefix).resolve())), receipt.isolation.observed_python3


def test_a_check_cannot_forge_the_interpreter_line(tmp_path):
    """The line is read from the prologue's first four lines; whatever the
    check itself writes to the pipe afterwards is ignored."""
    from hoh.sandbox import marker_reading
    got = marker_reading("mnt:[1]\nnet:[2]\nro\n/venv/bin/python3\n/forged/bin/python3\n")
    assert got["python3"] == "/venv/bin/python3"


@needs_bwrap
def test_a_real_check_writing_to_the_proof_pipe_cannot_change_the_interpreter(tmp_path):
    """Reviewer B asked for the end-to-end form of the parser test above: a
    strict check that writes a forged interpreter line to the proof pipe."""
    receipt, log = _run('echo /forged/bin/python3 >&"$HOH_PROOF_FD"; true', tmp_path)
    assert receipt.outcome(0) is Outcome.PASS, log      # it ran, it was not refused
    assert receipt.isolation.observed_python3 != "/forged/bin/python3"
    assert receipt.isolation.observed_python3.endswith("/python3"), receipt.isolation
