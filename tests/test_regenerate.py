"""The regeneration loop: does it ever call a moving target a fixpoint?

The loop exists because four generated documents and the board read each
other (O194). Its only real failure mode is declaring success while something
is still changing, so that is what these cases attack.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "regenerate_probe", ROOT / "tools" / "regenerate.py")
rg = importlib.util.module_from_spec(_spec)
sys.modules["regenerate_probe"] = rg
_spec.loader.exec_module(rg)


def test_a_board_that_keeps_changing_is_not_a_fixpoint(monkeypatch):
    """The case the loop exists for, and the one it must not paper over."""
    counter = {"n": 0}

    def never_settles(env):
        counter["n"] += 1
        return []

    monkeypatch.setattr(rg, "run_once", never_settles)
    monkeypatch.setattr(rg, "_digest", lambda p: f"different-{counter['n']}")
    result = rg.regenerate(max_passes=4)
    assert result["converged"] is False
    assert result["passes"] == 4
    assert "cycle rather than slow convergence" in result["why"]


def test_it_stops_as_soon_as_the_board_stops_moving(monkeypatch):
    states = ["a", "b", "b", "b"]
    seen = {"i": 0}

    def step(env):
        seen["i"] += 1
        return []

    monkeypatch.setattr(rg, "run_once", step)
    monkeypatch.setattr(rg, "_digest", lambda p: states[min(seen["i"], len(states) - 1)])
    result = rg.regenerate(max_passes=5)
    assert result["converged"] is True
    assert result["passes"] == 2, "one pass to move it, one to see it stay"


def test_a_failed_step_is_not_a_converged_cycle(monkeypatch):
    monkeypatch.setattr(rg, "run_once", lambda env: ["capability matrix: exit 2"])
    result = rg.regenerate(max_passes=3)
    assert result["converged"] is False
    assert result["passes"] == 1
    assert result["failed"] == ["capability matrix: exit 2"]


def test_the_board_is_the_last_step_of_every_pass():
    """It reads what the others write. Any other order regenerates the
    documents from a board that is about to change."""
    names = [name for name, _ in rg.STEPS]
    assert names[-1] == "readiness board", names
    assert "capability matrix" in names and "baseline documents" in names
    # And the matrix comes before the documents that render it.
    assert names.index("capability matrix") < names.index("baseline documents")


def test_the_digest_ignores_the_clock_and_not_the_content(tmp_path):
    """A timestamp in the comparison would make every pass look like a change
    and the loop would never converge."""
    board = tmp_path / "READINESS.md"
    board.write_text("Measured at `aaa` on 2026-01-01.\n\nrow: PASS\n")
    first = rg._digest(board)
    board.write_text("Measured at `bbb` on 2026-12-31.\n\nrow: PASS\n")
    assert rg._digest(board) == first
    board.write_text("Measured at `bbb` on 2026-12-31.\n\nrow: FAIL\n")
    assert rg._digest(board) != first


def test_a_red_board_is_a_verdict_and_not_a_broken_step():
    """`readiness.py --write` exits 1 when rows are open. Treating that as a
    failed step would make a red board indistinguishable from a crashed
    tool -- and would stop the loop exactly when it is most needed."""
    source = (ROOT / "tools" / "regenerate.py").read_text()
    assert 'allowed = (0, 3, 1) if name == "readiness board" else (0, 3)' in source
    assert "indistinguishable from a crashed tool" in source
