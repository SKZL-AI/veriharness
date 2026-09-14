"""Absence of a receipt has to mean absence of an attempt.

Two gaps a reviewer found by reading a real run's evidence rather than its
code:

* a refused *candidate* check left a receipt (exit 126); a refused *baseline*
  check left nothing at all. An iteration in which the guard rejected every
  criterion therefore looked, from the receipts alone, like an iteration where
  no baseline was ever attempted;
* the run transcript printed `Iteration 1: not accepted -- not accepted: K1,
  K2, K3` for exactly that iteration. True, and useless: nothing ran. A
  transcript that renders an infrastructure refusal as a product rejection is
  the laundering this project exists to stop.
"""

from __future__ import annotations

import json

from hoh.contracts import AcceptanceCheck, Candidate, RunState


def _kandidat(pfad) -> Candidate:
    return Candidate(
        candidate_id="c1", repo_path=str(pfad), commit="a" * 40,
        tree_clean=True, tree_digest="d" * 16,
    )


def _zustand(tmp_path) -> RunState:
    return RunState(
        run_id="r", repo_path=str(tmp_path), project_name="p",
        spec_path=str(tmp_path / "s.md"), spec_digest="d", policy_digest="d",
        profile_digest="d", iteration=1, attempt=1,
    )


# --------------------------------------------------------------------------- #
# A refused baseline check leaves evidence
# --------------------------------------------------------------------------- #


def test_a_refused_baseline_check_writes_a_receipt(tmp_path):
    from hoh.controller import Controller
    from hoh.runner import ArenaEscape
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store

    state = _zustand(tmp_path)
    check = AcceptanceCheck(
        check_id="K1", command="cd somewhere && pytest", expect_exit=0,
        description="d",
    )
    c._refusal_receipt(
        state, check, _kandidat(tmp_path),
        ArenaEscape("Check command changes the directory (cd)"),
    )

    pfad = store.dir / "receipts" / "r-i1-a1-K1-basis.json"
    assert pfad.exists(), "a refusal has to be as visible as an execution"
    d = json.loads(pfad.read_text())
    assert d["exit_code"] == 126
    assert d["runner_ok"] is False
    assert d["check_id"] == "K1"

    # And it reads as INCONCLUSIVE, never as a product verdict.
    from hoh.contracts import Receipt

    assert Receipt.model_validate(d).outcome(0).value == "INCONCLUSIVE"


def test_the_refusal_receipt_records_why(tmp_path):
    from hoh.controller import Controller
    from hoh.runner import HouseRuleViolation
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c._refusal_receipt(
        _zustand(tmp_path),
        AcceptanceCheck(check_id="K2", command="rm -rf /", expect_exit=0,
                        description="d"),
        _kandidat(tmp_path),
        HouseRuleViolation("recursive deletion at a fundamental path"),
    )
    text = (store.dir / "logs" / "r-i1-a1-K2-basis.txt").read_text()
    assert "refused before execution" in text
    assert "recursive deletion" in text


def test_a_baseline_receipt_does_not_collide_with_the_candidate_one(tmp_path):
    """The suffix is what keeps two measurements of two different states apart.
    Without it the second write would be rejected as immutable evidence."""
    from hoh.controller import Controller
    from hoh.runner import ArenaEscape
    from hoh.store import RunStore

    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c._refusal_receipt(
        _zustand(tmp_path),
        AcceptanceCheck(check_id="K1", command="x", expect_exit=0, description="d"),
        _kandidat(tmp_path),
        ArenaEscape("nope"),
    )
    namen = {p.name for p in (store.dir / "receipts").glob("*.json")}
    assert namen == {"r-i1-a1-K1-basis.json"}


# --------------------------------------------------------------------------- #
# The transcript says what happened
# --------------------------------------------------------------------------- #


class _Store:
    def __init__(self, d):
        self.dir = d


def _schreibe(verzeichnis, name, *, exit_code, runner_ok, check_id):
    verzeichnis.mkdir(parents=True, exist_ok=True)
    (verzeichnis / f"{name}.json").write_text(json.dumps({
        "receipt_id": name, "run_id": "r", "iteration": 1, "attempt": 1,
        "check_id": check_id, "candidate_binding": "b", "command": "x",
        "exit_code": exit_code, "started_at": "t", "ended_at": "t",
        "stdout_digest": "d", "runner_identity": "t", "runner_ok": runner_ok,
    }))


def test_an_iteration_where_nothing_ran_is_named_as_such(tmp_path):
    from hoh.cli import _inconclusive_checks

    q = tmp_path / "receipts"
    for cid in ("K1", "K2", "K3"):
        _schreibe(q, f"r-i1-a1-{cid}", exit_code=126, runner_ok=False, check_id=cid)

    unklar = _inconclusive_checks(_Store(tmp_path), _zustand(tmp_path), 1)
    assert [c for c, _ in unklar] == ["K1", "K2", "K3"]
    assert all(grund == "refused or not executable" for _, grund in unklar)


def test_a_genuine_product_failure_is_not_reported_as_inconclusive(tmp_path):
    """The control: a criterion that ran and found a defect must not be
    dressed up as an infrastructure problem either."""
    from hoh.cli import _inconclusive_checks

    q = tmp_path / "receipts"
    _schreibe(q, "r-i1-a1-K1", exit_code=1, runner_ok=True, check_id="K1")
    assert _inconclusive_checks(_Store(tmp_path), _zustand(tmp_path), 1) == []


def test_a_timeout_and_a_missing_command_are_told_apart(tmp_path):
    from hoh.cli import _inconclusive_checks

    q = tmp_path / "receipts"
    _schreibe(q, "r-i1-a1-K1", exit_code=124, runner_ok=False, check_id="K1")
    _schreibe(q, "r-i1-a1-K2", exit_code=127, runner_ok=False, check_id="K2")
    gruende = dict(_inconclusive_checks(_Store(tmp_path), _zustand(tmp_path), 1))
    assert gruende == {"K1": "timeout", "K2": "not found"}


def test_only_the_named_iteration_is_reported(tmp_path):
    from hoh.cli import _inconclusive_checks

    q = tmp_path / "receipts"
    _schreibe(q, "r-i1-a1-K1", exit_code=126, runner_ok=False, check_id="K1")
    _schreibe(q, "r-i2-a1-K1", exit_code=126, runner_ok=False, check_id="K1")
    assert len(_inconclusive_checks(_Store(tmp_path), _zustand(tmp_path), 2)) == 1


def test_baseline_receipts_are_not_counted_twice_in_the_transcript(tmp_path):
    """Both sides now leave a receipt, and the line is about criteria, not
    about measurements."""
    from hoh.cli import _inconclusive_checks

    q = tmp_path / "receipts"
    _schreibe(q, "r-i1-a1-K1", exit_code=126, runner_ok=False, check_id="K1")
    _schreibe(q, "r-i1-a1-K1-basis", exit_code=126, runner_ok=False, check_id="K1")
    assert len(_inconclusive_checks(_Store(tmp_path), _zustand(tmp_path), 1)) == 1


def test_no_receipts_at_all_reports_nothing_rather_than_crashing(tmp_path):
    from hoh.cli import _inconclusive_checks

    assert _inconclusive_checks(_Store(tmp_path), _zustand(tmp_path), 1) == []


# --------------------------------------------------------------------------- #
# A red baseline is not automatically a demonstrated increment
# --------------------------------------------------------------------------- #


def _transkript(ausgabe: str) -> str:
    return f"$ cmd\n# cwd=/x\n--- output ---\n{ausgabe}\n"


def test_a_baseline_that_collected_no_tests_is_artefactual():
    """Measured on this project's own STRICT acceptance run: the accepted
    candidate's only discriminating criterion was
    `python3 -m unittest discover -s tests -p test_roman.py`, and its baseline
    exited 5 with `Ran 0 tests ... NO TESTS RAN`. The increment was real; that
    criterion did not demonstrate it."""
    from hoh.runner import artefactual_reason

    grund = artefactual_reason(5, _transkript("Ran 0 tests in 0.000s\n\nNO TESTS RAN"))
    assert "collected no tests" in grund


def test_a_missing_file_in_the_baseline_is_artefactual():
    from hoh.runner import artefactual_reason

    grund = artefactual_reason(
        1, _transkript("FileNotFoundError: .../tests/test_roman.py")
    )
    assert "not there yet" in grund


def test_a_genuine_behavioural_failure_is_not_artefactual():
    """The control. Without it the detector could simply call everything
    artefactual and look equally diligent."""
    from hoh.runner import artefactual_reason

    assert artefactual_reason(
        1,
        _transkript(
            "FAIL: test_to_roman_1987\nNotImplementedError\n"
            "Ran 12 tests in 0.01s\n\nFAILED (errors=12)"
        ),
    ) == ""


def test_the_signature_is_looked_for_in_the_output_not_in_the_header():
    """The command line is echoed into the transcript's header. A criterion
    that merely *mentions* a signature word must not be misread as one."""
    from hoh.runner import artefactual_reason

    text = (
        "$ python3 -c \"print('NO TESTS RAN is a string')\"\n"
        "# cwd=/x\n--- output ---\nFAILED: the behaviour is missing\n"
    )
    assert artefactual_reason(1, text) == ""


# --------------------------------------------------------------------------- #
# The planner is told the rules its commands are judged by
# --------------------------------------------------------------------------- #


def _planner_text():
    from hoh.evidence import EvidenceBundle
    from hoh.roles import planner_prompt

    return planner_prompt(
        iteration=1, spec_text="a spec", evidence=EvidenceBundle(run_id="r"),
        repo_path="/repo", base_candidate_id="b", spec_digest="d", run_id="r",
    )


def test_the_planner_is_told_not_to_cd():
    """Two runs in this project's own history lost a whole iteration to a
    criterion beginning `cd <arena> && ...`. The guard refused it correctly
    both times; the planner had never been told the rule it was breaking, and
    the refusal arrives only after the iteration is spent."""
    text = _planner_text()
    assert "Do not `cd`" in text
    assert "{ARENA}" in text, "the alternative has to be named, not only the ban"


def test_the_planner_is_told_which_interpreter_it_gets():
    """The other recurring failure: a criterion invoking `python3 -m pytest`
    against a bare system interpreter that has no pytest."""
    text = _planner_text()
    assert "bare system `python3`" in text
    assert "unittest" in text


def test_the_planner_is_told_there_is_no_network_and_no_writing():
    text = _planner_text()
    assert "no network" in text
    assert "must not write into the directory under test" in text


def test_the_arena_token_survives_the_prompt_unformatted():
    """The prompt is an f-string; a bare `{ARENA}` in it would be a KeyError or
    a silently swallowed placeholder."""
    assert "{ARENA}" in _planner_text()


# --------------------------------------------------------------------------- #
# A run's own files live with the run
# --------------------------------------------------------------------------- #


def test_a_repair_specification_is_written_under_the_run_root(tmp_path):
    """It used to go to `<repo>/.hoh-repair-<id>.md`.

    That location is untracked, unprotected and in the way. This project's own
    export refused it as an unclassified path; the cleanup that followed moved
    it aside; and a repair run that was live at that moment blocked on
    "specification is missing". A file a run depends on must not sit where
    tidying the repository can remove it.
    """
    from hoh.launcher import HohRunLauncher
    from hoh.project import ActionClass, Lifecycle, TaskNode

    root = tmp_path / "root"
    repo = tmp_path / "repo"
    repo.mkdir()
    # Not a dry run: `prepare` returns before writing anything in that mode,
    # and the location of the file is the whole point here. The worktree and
    # the run will fail to be created -- there is no harness -- which is fine:
    # the specification is written before either is attempted.
    launcher = HohRunLauncher(root, repo)
    knoten = TaskNode(
        id="repair-1-1", spec_path="", spec_digest="d",
        lifecycle=Lifecycle.READY, action_class=ActionClass.INTERNAL,
        repair_of="node-a",
        note="the suite failed on the merged state",
    )

    launcher.prepare(knoten)

    geschrieben = list(root.rglob("repair-*.md"))
    assert geschrieben, "the repair specification was not written under the run root"
    assert repo not in geschrieben[0].parents, (
        "the specification was written into the repository under test"
    )
    assert not list(repo.glob(".hoh-repair-*.md"))
