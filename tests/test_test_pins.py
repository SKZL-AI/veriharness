"""FID1: the test files a specification pins are the bytes the candidate is accepted with.

`CRITERION_FIDELITY` proves the plan carries the criteria's commands. A command is a node id, and a
node id names a test, not its bytes: in the P3-02s2rf run the developer wrote the implementation and
the tests under the same node ids, QA accepted against tests nobody had reviewed, and only a byte
comparison made by hand after the import stopped it. A specification can now pin its test contract in
a `test-pins` block, and the product refuses -- by name, with the file and both digests -- at the
verification (before any check runs and before QA is asked), at the acceptance (the working tree as
it is then) and at `hoh import` (the accepted candidate's tree).

Every test builds its own repositories and files under `tmp_path`, drives the product's own
controller or `hoh run` / `hoh import` with a fake role, and starts no Herdr and no agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hoh import spec_criteria  # the attributes are read in each test: a tree without them fails each on its own
from hoh.contracts import Budgets, Role, Stage
from hoh.controller import Controller, DispatchError
from hoh.store import RunStore

# The fake dispatcher and the builders of the controller tests are the ones used here.
from test_controller import FakeDispatcher, build, plan_for, qa_pass

FENCE = "`" * 3
TEST_FILE = "tests/test_add.py"
GOOD = "import sys\nsys.path.insert(0, '.')\nfrom app import add\nassert add(2, 2) == 4\n"
#: What a developer who re-wrote the tests under the same node ids would leave.
REWRITTEN = GOOD + "# re-written: the same file name, other bytes\n"
CRITERION = f"python3 -B {TEST_FILE}"


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode()).hexdigest()


def block(info: str, *lines: str) -> str:
    return "\n".join([f"{FENCE}{info}", *lines, FENCE])


def spec_text(*, pins=None, criteria=(f"K1 {CRITERION}",), prose="# Spec\n\nAdd must add.\n") -> str:
    parts = [prose, block("criteria", *criteria)] if criteria else [prose]
    if pins is not None:
        parts.append(block("test-pins", *[f"{d}  {p}" for p, d in pins.items()]))
    return "\n\n".join(parts) + "\n"


def _git_quiet(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """The project of the controller tests, with a tests directory of its own in the base."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git_quiet(repo, "init", "-q")
    _git_quiet(repo, "config", "user.email", "t@example.invalid")
    _git_quiet(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "marker.txt").write_text("start\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "helpers.py").write_text("HELP = 1\n", encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text("assert True\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "real.py").write_text(GOOD, encoding="utf-8")
    _git_quiet(repo, "add", "-A")
    _git_quiet(repo, "commit", "-qm", "init")
    return repo


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    return tmp_path / "spec.md"


def writes(**files):
    """A developer that writes `{path: text}` (a dict key with `/` as a keyword needs `**`)."""
    def develop(r: Path) -> None:
        for rel, text in files.items():
            path = r / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding="utf-8")
    return develop


def run_with(tmp_path, repo, spec, text, develop, *, plan_command=CRITERION):
    spec.write_text(text, encoding="utf-8")
    d = FakeDispatcher(repo=repo, develop=develop,
                       plan_json=lambda s: plan_for(s, command=plan_command), qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    return ctrl, state, store, d


PINNED = spec_text(pins={TEST_FILE: sha(GOOD)})


# --- The pins block ----------------------------------------------------------------------------


def test_a_block_gives_its_pins_and_a_specification_without_one_declares_none():
    pins = spec_criteria.declared_pins(spec_text(pins={TEST_FILE: sha(GOOD), "tests/conftest.py": "absent"},
                                   criteria=(f"K1 {CRITERION}",)))
    assert [(p.path, p.digest) for p in pins] == [(TEST_FILE, sha(GOOD)), ("tests/conftest.py", "absent")]
    assert spec_criteria.declared_pins(spec_text()) == ()
    assert spec_criteria.declared_pins("# Spec\n\n~~~test-pins\nnothing\n~~~\n") == (), "only a backtick fence at the line start is a block"


@pytest.mark.parametrize("text", [
    block("test-pins", f"{sha(GOOD)}  {TEST_FILE}") + "\n" + block("test-pins", f"{sha(GOOD)}  {TEST_FILE}"),
    f"{FENCE}test-pins\n{sha(GOOD)}  {TEST_FILE}\n",                                   # not closed
    block("test-pins"),                                                                 # empty
    block("test-pins", f"{sha(GOOD).upper()}  {TEST_FILE}"),                            # upper-case digest
    block("test-pins", f"{sha(GOOD)[:40]}  {TEST_FILE}"),                               # short digest
    block("test-pins", f"{sha(GOOD)}  /abs/{TEST_FILE}"),
    block("test-pins", f"{sha(GOOD)}  ../{TEST_FILE}"),
    block("test-pins", f"{sha(GOOD)}  tests//test_add.py"),
    block("test-pins", f"{sha(GOOD)}  tests/./test_add.py"),
    block("test-pins", f"{sha(GOOD)}  .git/hooks/x.py"),
    block("test-pins", f"{sha(GOOD)}  .GIT/hooks/x.py"),
    block("test-pins", f"{sha(GOOD)}  tests/te st.py"),
    block("test-pins", f"{sha(GOOD)}  tests/tést.py"),
    block("test-pins", f"{sha(GOOD)}  tests/t\x00st.py"),
    block("test-pins", f"{sha(GOOD)} {TEST_FILE}", f"{sha(REWRITTEN)} tests/TEST_ADD.py"),   # one path to a case-insensitive fs
    block("test-pins", f"{sha(GOOD)} {TEST_FILE}", f"{sha(GOOD)} {TEST_FILE}"),
    block("test-pins", f"{TEST_FILE}"),
])
def test_a_block_that_cannot_be_read_is_refused_by_name(text):
    with pytest.raises(spec_criteria.PinsBlockError, match="SPEC_TEST_PINS_UNREADABLE"):
        spec_criteria.declared_pins(f"{block('criteria', 'K1 ' + CRITERION)}\n\n{text}\n")


def test_more_pins_than_the_bound_is_refused():
    lines = [f"{sha(str(i))}  tests/t{i}.py" for i in range(spec_criteria.MAX_PINS + 1)]
    with pytest.raises(spec_criteria.PinsBlockError, match="more than"):
        spec_criteria.declared_pins(block("test-pins", *lines) + "\n")
    spec_criteria.declared_pins(block("test-pins", *lines[:-1]) + "\n")


def test_a_test_file_a_criterion_runs_must_be_pinned_when_others_are():
    text = spec_text(pins={TEST_FILE: sha(GOOD)},
                     criteria=(f"K1 {CRITERION}", "K2 python3 -B -m pytest -q tests/test_more.py::test_x -k 'a or b'"))
    with pytest.raises(spec_criteria.PinsBlockError, match="tests/test_more.py"):
        spec_criteria.declared_pins(text)
    for command in ("python3 -B -m pytest -q ./tests/test_more.py", "python3 -B -m unittest tests/test_more.py",
                    "pytest --deselect tests/test_more.py::t -q", "sh -c 'python3 -B tests/test_more.py && true'",
                    "python3 -B tests/more_test.py", "python3 -B tests/conftest.py",
                    "python3 -B tests/test_caf\u00e9.py", "python3 -B /tmp/x/test_more.py",
                    "python3 -B ../x/test_more.py", "python3 -B --deselect=tests/test_more.py"):
        with pytest.raises(spec_criteria.PinsBlockError):
            spec_criteria.declared_pins(spec_text(pins={TEST_FILE: sha(GOOD)}, criteria=(f"K1 {CRITERION}", f"K2 {command}")))
    # a tool the slice may change legitimately is no test file; an unsplittable command is refused
    spec_criteria.declared_pins(spec_text(pins={TEST_FILE: sha(GOOD)}, criteria=(f"K1 {CRITERION}", "K2 python3 -B tools/check.py")))
    with pytest.raises(spec_criteria.PinsBlockError, match="cannot be split"):
        spec_criteria.declared_pins(spec_text(pins={TEST_FILE: sha(GOOD)}, criteria=(f"K1 {CRITERION}", "K2 python3 -B 'tests/x")))
    with pytest.raises(spec_criteria.PinsBlockError, match="pinned absent"):
        spec_criteria.declared_pins(spec_text(pins={TEST_FILE: "absent"}))


# --- The verification: the pins are read before any check runs ----------------------------------


def test_the_pinned_bytes_are_accepted_and_the_implementation_stays_free(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(
        tmp_path, seeded, spec, PINNED,
        writes(**{TEST_FILE: GOOD, "app.py": "def add(a, b):\n    return a + b  # reworked\n"}))
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason
    assert d.calls == [Role.PLANNER, Role.DEVELOPER, Role.QA]
    assert "NOT_RUN" not in out.reason
    assert not any("test content fidelity" in h for h in state.history)


def test_a_re_written_test_under_the_same_name_is_refused_before_QA_is_asked(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, writes(**{TEST_FILE: REWRITTEN}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and out.reason.startswith("TEST_CONTENT_FIDELITY"), out.reason
    assert TEST_FILE in out.reason and sha(GOOD) in out.reason and sha(REWRITTEN) in out.reason
    assert d.calls == [Role.PLANNER, Role.DEVELOPER], "QA is not asked to assess tests nobody reviewed"
    assert state.last_accepted_candidate is None and state.stage is Stage.PLANNING
    assert list(store.receipts_dir.glob("*.json")) == [], "no check ran: no receipt for tests nobody reviewed"
    result = json.loads((store.results_dir / "qa-i1-a1.json").read_text())
    assert result["open_gaps"] and result["open_gaps"][0].startswith("TEST_CONTENT_FIDELITY: " + TEST_FILE)
    assert all(v["outcome"] == "INCONCLUSIVE" for v in result["verdicts"])


def test_a_pinned_file_that_is_missing_or_ignored_is_refused(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, writes(**{"done.txt": "x\n"}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and "missing from the candidate" in out.reason and TEST_FILE in out.reason


def test_a_pinned_file_the_candidate_ignores_is_not_in_the_candidate(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED,
                                     writes(**{".gitignore": f"{TEST_FILE}\n", TEST_FILE: GOOD}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and "missing from the candidate" in out.reason, "an ignored file is not in the candidate"


def test_a_symbolic_link_with_the_right_content_is_not_the_pinned_file(tmp_path, seeded, spec):
    def develop(r: Path) -> None:
        (r / "tests").mkdir(exist_ok=True)
        os.symlink("../data/real.py", r / TEST_FILE)
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, develop)
    out = ctrl.run_iteration(state)
    assert not out.accepted and "is not a regular file (mode 120000)" in out.reason and TEST_FILE in out.reason


@pytest.mark.parametrize("extra, expect", [
    ({"tests/conftest.py": "import pytest\n"}, "tests/conftest.py: new against the run's base and not pinned"),
    ({"conftest.py": "x = 1\n"}, "conftest.py: new against the run's base and not pinned"),
    ({"tests/__init__.py": ""}, "tests/__init__.py: new against the run's base and not pinned"),
    ({"pytest.ini": "[pytest]\naddopts = --co\n"}, "pytest.ini: new against the run's base and not pinned"),
    ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "pyproject.toml: new against the run's base and not pinned"),
    ({".gitattributes": f"{TEST_FILE} export-ignore\n"}, ".gitattributes: new against the run's base and not pinned"),
    ({"tests/helpers.py": "HELP = 2\n"}, "tests/helpers.py: changed against the run's base and not pinned"),
    ({"tests/test_shadow.py": "assert True\n"}, "tests/test_shadow.py: new against the run's base and not pinned"),
    ({"tests/add_data.json": "{}\n"}, "tests/add_data.json: new against the run's base and not pinned"),
    ({"tests/app.py": "def add(a, b):\n    return 4\n"}, "tests/app.py: new against the run's base and not pinned"),
    ({"tests/__pycache__/test_add.cpython-312.pyc": b"\x00\x01"},
     "tests/__pycache__/test_add.cpython-312.pyc: new against the run's base and not pinned"),
    ({"tests/__pycache__/deep/x.pyc": b"\x00"}, "tests/__pycache__/deep/x.pyc: new against the run's base and not pinned"),
    ({"tests/test_add/__init__.py": "x = 1\n"}, "tests/test_add/__init__.py: new against the run's base and not pinned"),
    ({"tests/test_add.cpython-312-x86_64-linux-gnu.so": b"\x7fELF"},
     "tests/test_add.cpython-312-x86_64-linux-gnu.so: new against the run's base and not pinned"),
    ({"tests/Test_Add.py": GOOD}, "tests/Test_Add.py: differs from the pinned tests/test_add.py only in case"),
    ({"tests/TEST_ADD.PY": GOOD}, "only in case"),
])
def test_what_the_tests_load_without_being_named_is_unchanged_or_pinned(tmp_path, seeded, spec, extra, expect):
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, writes(**{TEST_FILE: GOOD, **extra}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and out.reason.startswith("TEST_CONTENT_FIDELITY"), out.reason
    assert expect in out.reason, out.reason
    assert Role.QA not in d.calls


def test_a_file_the_slice_changes_on_purpose_is_pinned_and_then_accepted(tmp_path, seeded, spec):
    text = spec_text(pins={TEST_FILE: sha(GOOD), "tests/conftest.py": sha("FIXTURE = 1\n"),
                           "tests/helpers.py": sha("HELP = 2\n")})
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text, writes(**{
        TEST_FILE: GOOD, "tests/conftest.py": "FIXTURE = 1\n", "tests/helpers.py": "HELP = 2\n"}))
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


def test_a_path_pinned_absent_must_not_appear(tmp_path, seeded, spec):
    text = spec_text(pins={TEST_FILE: sha(GOOD), "tests/conftest.py": "absent"})
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text, writes(**{
        TEST_FILE: GOOD, "tests/conftest.py": "x = 1\n"}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and "tests/conftest.py: pinned absent, but the candidate has it" in out.reason


def test_a_directory_a_criterion_runs_needs_every_python_file_below_it_pinned(tmp_path, seeded, spec):
    text = spec_text(pins={TEST_FILE: sha(GOOD)}, criteria=("K1 python3 -B -m pytest -q -p no:cacheprovider tests",))
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text, writes(**{TEST_FILE: GOOD}),
                                     plan_command="python3 -B -m pytest -q -p no:cacheprovider tests")
    out = ctrl.run_iteration(state)
    assert not out.accepted
    for unpinned in ("tests/helpers.py", "tests/test_other.py"):
        assert f"{unpinned}: run by a criterion but not pinned" in out.reason


def test_a_deletion_of_a_file_the_tests_load_is_refused_too(tmp_path, seeded, spec):
    def develop(r: Path) -> None:
        (r / TEST_FILE).write_text(GOOD, encoding="utf-8")
        (r / "tests" / "helpers.py").rename(r / "tests" / "helpers_moved.py")
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, develop)
    out = ctrl.run_iteration(state)
    assert not out.accepted and "tests/helpers.py: deleted against the run's base and not pinned" in out.reason
    assert "tests/helpers_moved.py: new against the run's base and not pinned" in out.reason


# --- The acceptance: the working tree as it is then ----------------------------------------------


def test_a_test_file_edited_after_QA_is_refused_at_the_acceptance(tmp_path, seeded, spec, monkeypatch):
    real = Controller._verify

    def tampering(self, state, **kw):
        result = real(self, state, **kw)
        (Path(state.repo_path) / TEST_FILE).write_text(REWRITTEN, encoding="utf-8")   # after QA's verdict
        return result

    monkeypatch.setattr(Controller, "_verify", tampering)
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, writes(**{TEST_FILE: GOOD}))
    out = ctrl.run_iteration(state)
    assert Role.QA in d.calls, "the verification itself saw the pinned bytes"
    assert not out.accepted and out.reason.startswith("TEST_CONTENT_FIDELITY"), out.reason
    assert sha(REWRITTEN) in out.reason and "not the frozen candidate" in out.reason
    assert state.last_accepted_candidate is None


def test_the_acceptance_check_is_the_pins_not_a_second_opinion_of_the_freeze(tmp_path, seeded, spec, monkeypatch):
    """The control, for a specification that pins nothing: an edit after QA to a file is the freeze's
    business (`unchanged`), not this guard's -- the run behaves as it did."""
    real = Controller._verify

    def touches(self, state, **kw):
        result = real(self, state, **kw)
        (Path(state.repo_path) / "marker.txt").write_text("late\n", encoding="utf-8")
        return result

    monkeypatch.setattr(Controller, "_verify", touches)
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, spec_text(), writes(**{TEST_FILE: GOOD}))
    out = ctrl.run_iteration(state)
    assert out.accepted, out.reason


def test_with_pins_any_edit_after_QA_is_not_the_frozen_candidate(tmp_path, seeded, spec, monkeypatch):
    """The same edit, a file no pin names, under a specification that pins: the acceptance reads the
    working tree and refuses a tree that is not the candidate the verification measured."""
    real = Controller._verify

    def touches(self, state, **kw):
        result = real(self, state, **kw)
        (Path(state.repo_path) / "marker.txt").write_text("late\n", encoding="utf-8")
        return result

    monkeypatch.setattr(Controller, "_verify", touches)
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, writes(**{TEST_FILE: GOOD}))
    out = ctrl.run_iteration(state)
    assert Role.QA in d.calls
    assert not out.accepted and out.reason.startswith("TEST_CONTENT_FIDELITY"), out.reason
    assert "not the frozen candidate" in out.reason and state.last_accepted_candidate is None


# --- An amendment that changes the pins is acceptance-affecting (A-M2) ----------------------------


def qa_all(state):
    """PASS for K1 and G2 (what is not planned or preserved is ignored by the controller)."""
    return {"verdicts": [{"check_id": cid, "outcome": "PASS",
                          "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-{cid}",
                          "reproduction": "pytest", "note": "green"} for cid in ("K1", "G2")],
            "open_gaps": [], "summary": "all good"}


def amend(tmp_path, spec, text, aid, *, kind="clarify"):
    from hoh import cli

    new = tmp_path / f"{aid}.md"
    new.write_text(text, encoding="utf-8")
    return cli.main(["--root", str(tmp_path / "runs"), "amend", "r1", "--spec-file", str(new), "--amendment-id", aid,
                     "--kind", kind, "--actor", "tester", "--reason", "pins"])


def first_iteration_accepted(tmp_path, seeded, spec):
    spec.write_text(PINNED, encoding="utf-8")
    d = FakeDispatcher(repo=seeded, develop=writes(**{TEST_FILE: GOOD}),
                       plan_json=lambda s: plan_for(s, command=CRITERION), qa_json=qa_all)
    ctrl, state, store = build(tmp_path, seeded, spec, d)
    assert ctrl.run_iteration(state).accepted
    return ctrl, store, d


def plan_both(state):
    """K1 (the specification's command) and G2, as one plan."""
    one = plan_for(state, command=CRITERION)
    two = plan_for(state, command="test -f done2.txt", check_id="G2")
    return {**one, "acceptance_checks": one["acceptance_checks"] + two["acceptance_checks"]}


def test_a_changed_pins_block_reopens_every_criterion_even_as_a_clarify(tmp_path, seeded, spec, capsys):
    ctrl, store, d = first_iteration_accepted(tmp_path, seeded, spec)
    assert set(store.read_checks()) == {"K1"}
    assert amend(tmp_path, spec, spec_text(pins={TEST_FILE: sha(REWRITTEN)}), "A1", kind="clarify") == 0
    capsys.readouterr()                                       # a clarify names no criterion and costs nothing
    d.develop = writes(**{TEST_FILE: REWRITTEN, "done2.txt": "x\n"})
    # K1 left the preservation suite: it is demanded of the plan again, as a fresh criterion
    d.plan_json = lambda s: plan_for(s, command="test -f done2.txt", check_id="G2")
    with pytest.raises(DispatchError, match="CRITERION_FIDELITY.*K1: missing from the plan"):
        ctrl.run_iteration(store.read_state())
    state = store.read_state()
    assert any("test-pins block amended (A1)" in h for h in state.history)
    assert "K1" not in store.read_checks()
    # answered: planned again and measured -- the acceptance stands and records it per amendment
    d.plan_json = plan_both
    out = ctrl.run_iteration(store.read_state())
    assert out.accepted, out.reason
    assert "A1:K1" in store.read_state().revalidated and set(store.read_checks()) == {"K1", "G2"}


def test_an_amendment_that_leaves_the_pins_block_alone_does_not_reopen_anything(tmp_path, seeded, spec, capsys):
    ctrl, store, d = first_iteration_accepted(tmp_path, seeded, spec)
    assert amend(tmp_path, spec, PINNED + "\nA clarifying sentence.\n", "A1") == 0
    capsys.readouterr()
    d.develop = writes(**{"done2.txt": "x\n"})
    d.plan_json = lambda s: plan_for(s, command="test -f done2.txt", check_id="G2")   # K1 stays preserved
    out = ctrl.run_iteration(store.read_state())
    assert out.accepted, out.reason
    assert not any("test-pins block amended" in h for h in store.read_state().history)


def test_a_pins_amendment_is_asked_again_by_the_next_pins_amendment(tmp_path, seeded, spec, capsys):
    ctrl, store, d = first_iteration_accepted(tmp_path, seeded, spec)
    assert amend(tmp_path, spec, spec_text(pins={TEST_FILE: sha(REWRITTEN)}), "A1") == 0
    d.develop = writes(**{TEST_FILE: REWRITTEN, "done2.txt": "x\n"})
    d.plan_json = plan_both
    assert ctrl.run_iteration(store.read_state()).accepted
    assert amend(tmp_path, spec, spec_text(pins={TEST_FILE: sha(GOOD)}), "A2") == 0
    capsys.readouterr()
    d.develop = writes(**{TEST_FILE: GOOD, "done3.txt": "x\n"})
    d.plan_json = lambda s: plan_for(s, command="test -f done3.txt", check_id="G3")
    with pytest.raises(DispatchError, match="K1: missing from the plan"):
        ctrl.run_iteration(store.read_state())
    assert "A2:K1" not in store.read_state().revalidated and "A1:K1" in store.read_state().revalidated


# --- A specification that pins nothing says so ---------------------------------------------------


def test_a_specification_that_pins_nothing_is_not_compared_and_says_so(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, spec_text(), writes(**{TEST_FILE: REWRITTEN}))
    out = ctrl.run_iteration(state)
    assert out.accepted, "no pins: the run behaves as before"
    assert out.reason.endswith(spec_criteria.NO_TEST_PINS) and "NO_TEST_PINS" in out.reason
    assert any(spec_criteria.NO_TEST_PINS in h for h in state.history)


def test_a_specification_without_criteria_and_pins_adds_no_pin_line(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, "# Spec\nadd must add.\n", writes(**{TEST_FILE: GOOD}),
                                     plan_command=f"test -f {FakeDispatcher.MARKER}")
    ctrl.run_iteration(state)
    assert not any(spec_criteria.NO_TEST_PINS in h for h in state.history)


def test_an_unreadable_block_or_an_uncovered_test_file_ends_the_iteration_before_any_role_starts(tmp_path, seeded, spec):
    ctrl, state, store, d = run_with(
        tmp_path, seeded, spec, spec_text(pins={TEST_FILE: sha(GOOD)}, criteria=(f"K1 {CRITERION}", "K2 python3 -B tests/test_other.py")),
        writes(**{TEST_FILE: GOOD}))
    with pytest.raises(spec_criteria.PinsBlockError, match="tests/test_other.py"):
        ctrl.run_iteration(state)
    assert d.calls == []


# --- The findings are fail-closed ---------------------------------------------------------------


def test_an_unreadable_tree_or_a_missing_base_is_a_finding_never_a_pass(seeded):
    text = PINNED
    assert spec_criteria.pin_findings(spec_text(), repo=str(seeded), tree="0" * 40, base_tree=None) == []
    out = spec_criteria.pin_findings(text, repo=str(seeded), tree="0" * 40, base_tree="1" * 40)
    assert out and out[0].startswith("TEST_CONTENT_FIDELITY: the candidate's test files could not be read")
    head = subprocess.run(["git", "-C", str(seeded), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
    out = spec_criteria.pin_findings(text, repo=str(seeded), tree=head, base_tree=None)
    assert out and "no base tree" in out[0]
    out = spec_criteria.pin_findings(text.replace("test-pins", "test-pinz") + block("test-pins", "nonsense"), repo=str(seeded),
                       tree=head, base_tree=head)
    assert out and "SPEC_TEST_PINS_UNREADABLE" in out[0]


def test_an_oversize_pinned_file_is_refused_not_read(tmp_path, seeded, spec):
    big = "x = 1\n" * (spec_criteria.MAX_PIN_BYTES // 6 + 10)
    text = spec_text(pins={TEST_FILE: sha(big)})
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text, writes(**{TEST_FILE: big}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and "more than the" in out.reason


def test_a_file_name_cannot_forge_a_finding_or_break_the_record(tmp_path, seeded, spec):
    forged = "tests/x\nTEST_CONTENT_FIDELITY: all pinned files hold\u2028"
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, PINNED, writes(**{TEST_FILE: GOOD, forged: "x = 1\n"}))
    out = ctrl.run_iteration(state)
    assert not out.accepted and "\\n" in out.reason and "\n" not in out.reason.replace("\\n", ""), out.reason
    assert out.reason.count("TEST_CONTENT_FIDELITY") == 2, "the name once as the verdict, once inside the escaped file name"
    assert "\u2028" not in out.reason


def test_a_test_file_at_the_repository_root_watches_the_loaders_not_the_implementation(tmp_path, seeded, spec):
    root_test = "test_add.py"
    text = spec_text(pins={root_test: sha(GOOD)}, criteria=("K1 python3 -B test_add.py",))
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text, writes(**{
        root_test: GOOD, "app.py": "def add(a, b):\n    return a + b  # reworked\n"}), plan_command="python3 -B test_add.py")
    out = ctrl.run_iteration(state)
    assert out.accepted, "the implementation beside a root-level test is the slice's own work: " + out.reason


@pytest.mark.parametrize("extra, expect", [
    ({"conftest.py": "x = 1\n"}, "conftest.py: new against the run's base and not pinned"),
    ({"pytest.ini": "[pytest]\n"}, "pytest.ini: new against the run's base and not pinned"),
    ({"sitecustomize.py": "x = 1\n"}, "sitecustomize.py: new against the run's base and not pinned"),
    ({"test_add/__init__.py": ""}, "test_add/__init__.py: new against the run's base and not pinned"),
])
def test_a_root_level_test_still_watches_what_the_interpreter_loads(tmp_path, seeded, spec, extra, expect):
    text = spec_text(pins={"test_add.py": sha(GOOD)}, criteria=("K1 python3 -B test_add.py",))
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text, writes(**{"test_add.py": GOOD, **extra}),
                                     plan_command="python3 -B test_add.py")
    out = ctrl.run_iteration(state)
    assert not out.accepted and expect in out.reason, out.reason


def commit_files(repo, files):
    """Commit `{path: bytes}` on top of HEAD; returns `(tree before, tree after)`."""
    before = git(repo, "rev-parse", "HEAD^{tree}")
    for rel, data in files.items():
        path = Path(repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "files")
    return before, git(repo, "rev-parse", "HEAD^{tree}")


def git(repo, *args) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid"))
    return done.stdout.strip()


def test_the_size_a_pin_reads_is_a_bound_with_both_sides_pinned(seeded):
    limit = spec_criteria.MAX_PIN_BYTES
    at = b"#" * (limit - 1) + b"\n"
    before, tree = commit_files(seeded, {TEST_FILE: at})
    text = spec_text(pins={TEST_FILE: sha(at)})
    assert spec_criteria.pin_findings(text, repo=str(seeded), tree=tree, base_tree=before) == []
    over = at + b"#"
    before, tree = commit_files(seeded, {TEST_FILE: over})
    out = spec_criteria.pin_findings(spec_text(pins={TEST_FILE: sha(over)}), repo=str(seeded), tree=tree, base_tree=before)
    assert out and "more than the" in out[0]


def test_the_findings_listed_are_bounded_and_say_how_many_more(seeded):
    many = {f"tests/new_{i:02d}.py": b"x = 1\n" for i in range(spec_criteria.MAX_LISTED + 8)}
    before, tree = commit_files(seeded, {TEST_FILE: GOOD.encode(), **many})
    out = spec_criteria.pin_findings(PINNED, repo=str(seeded), tree=tree, base_tree=before)
    assert len(out) == spec_criteria.MAX_LISTED + 1 and out[-1] == "... and 8 more"
    assert all("not pinned" in line for line in out[:-1])


def test_a_pinned_file_changed_in_mode_only_is_still_the_pinned_bytes(seeded):
    before, tree = commit_files(seeded, {TEST_FILE: GOOD.encode()})
    os.chmod(seeded / TEST_FILE, 0o755)
    git(seeded, "add", "-A")
    git(seeded, "commit", "-qm", "mode")
    tree2 = git(seeded, "rev-parse", "HEAD^{tree}")
    assert tree2 != tree
    assert spec_criteria.pin_findings(PINNED, repo=str(seeded), tree=tree2, base_tree=before) == []


# --- The class: what the criteria's command loads from the working directory (A-B1) -------------

FAILING = "import sys\nsys.path.insert(0, '.')\nfrom app import add\n\n\ndef test_add():\n    assert add(2, 2) == 5\n"
PYTEST_CRITERION = f"python3 -B -m pytest -q -p no:cacheprovider {TEST_FILE}"
FAKE_MAIN = "import sys\nprint('1 passed in 0.01s')\nsys.exit(0)\n"


def run_bypass(tmp_path, seeded, spec, extra, *, pinned):
    """A failing pinned test and a file of the candidate that makes the pytest criterion exit 0."""
    text = spec_text(pins={TEST_FILE: sha(FAILING)} if pinned else None, criteria=(f"K1 {PYTEST_CRITERION}",))
    ctrl, state, store, d = run_with(tmp_path, seeded, spec, text,
                                     writes(**{TEST_FILE: FAILING, **extra}), plan_command=PYTEST_CRITERION)
    return ctrl.run_iteration(state), d


@pytest.mark.parametrize("name, files, finding", [
    ("pytest.toml", {"pytest.toml": '[pytest]\naddopts = ["--collect-only"]\n'}, "pytest.toml: new against the run's base and not pinned"),
    (".pytest.toml", {".pytest.toml": '[pytest]\naddopts = ["--collect-only"]\n'}, ".pytest.toml: new against the run's base and not pinned"),
    ("pytest.py", {"pytest.py": FAKE_MAIN}, "pytest.py: new against the run's base and not pinned"),
    ("package", {"pytest/__init__.py": "", "pytest/__main__.py": FAKE_MAIN},
     "pytest/__init__.py: new against the run's base and not pinned"),
])
def test_a_file_that_turns_a_failing_pinned_test_into_exit_0_is_refused_and_the_control_shows_it_works(
        tmp_path, seeded, spec, name, files, finding):
    out, d = run_bypass(tmp_path / "pinned", seeded, spec, files, pinned=True)
    assert not out.accepted and out.reason.startswith("TEST_CONTENT_FIDELITY"), out.reason
    assert finding in out.reason and Role.QA not in d.calls


@pytest.mark.parametrize("files", [
    {"pytest.toml": '[pytest]\naddopts = ["--collect-only"]\n'},
    {"pytest.py": FAKE_MAIN},
])
def test_the_control_without_pins_accepts_the_same_candidate(tmp_path, seeded, spec, files):
    """Without a `test-pins` block the same candidate is accepted (and says NO_TEST_PINS): the
    bypass is real, and what refuses it above is the guard and nothing else."""
    out, d = run_bypass(tmp_path, seeded, spec, files, pinned=False)
    assert out.accepted, out.reason
    assert spec_criteria.NO_TEST_PINS in out.reason


def findings_for(repo, extra, *, test=GOOD, text=None, base_files=None):
    """`pin_findings` for a candidate that adds `test` (at TEST_FILE) and `extra` to the base."""
    if base_files:
        commit_files(repo, base_files)
    files = {TEST_FILE: test.encode(), **{k: v if isinstance(v, bytes) else v.encode() for k, v in extra.items()}}
    before, tree = commit_files(repo, files)
    return spec_criteria.pin_findings(text or spec_text(pins={TEST_FILE: sha(test)}), repo=str(repo),
                                      tree=tree, base_tree=before)


@pytest.mark.parametrize("name", [
    "pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg",
    "conftest.py", "__init__.py", "__main__.py", "sitecustomize.py", "usercustomize.py", ".python-version",
    ".tool-versions", ".gitattributes", "plugin.pth", "x.pyc", "x.so",
])
def test_every_file_pytest_or_the_interpreter_reads_from_the_working_directory_is_a_finding(seeded, name):
    out = findings_for(seeded, {name: "x = 1\n"})
    assert any(line.startswith(f"{name}: new against the run's base and not pinned") for line in out), out


@pytest.mark.parametrize("name", ["pytest.toml", "conftest.py", "pytest.py", "pluggy/__init__.py"])
def test_a_loader_the_specification_pins_at_its_post_state_is_its_authors_decision(seeded, name):
    text = spec_text(pins={TEST_FILE: sha(GOOD), name: sha("x = 1\n")})
    assert findings_for(seeded, {name: "x = 1\n"}, text=text) == []
    # ... and any other bytes there are a finding with both digests
    other = findings_for(seeded, {name: "x = 2\n"}, text=text)
    assert any(line.startswith(f"{name}: expected sha256 {sha('x = 1' + chr(10))}, found {sha('x = 2' + chr(10))}")
               for line in other), other


@pytest.mark.parametrize("name", [
    "pytest.py", "pytest/__init__.py", "_pytest/__init__.py", "pluggy.py", "pluggy/__init__.py", "py.py",
    "iniconfig/__init__.py", "packaging.py", "json.py", "os/__init__.py", "sys.py", "argparse.py",
    "pytest.cpython-313-x86_64-linux-gnu.so", "pytest.pyc", "__pycache__/pytest.cpython-313.pyc",
    "pytest-9.0.3.dist-info/entry_points.txt", "evil.egg-info/entry_points.txt",
    ".pytest_cache/v/cache/lastfailed", ".hypothesis/examples/x",
])
def test_a_module_that_can_shadow_what_the_command_imports_or_registers_a_plugin_is_a_finding(seeded, name):
    out = findings_for(seeded, {name: "x = 1\n"})
    assert any(line.startswith(f"{name}: new against the run's base and not pinned") for line in out), out


def test_a_name_a_pinned_test_imports_is_protected_only_when_something_else_provides_it(seeded):
    importing = "import sys\nimport hoh\nimport newmod\nfrom app import add\nassert add(2, 2) == 4\n"
    # `hoh` resolves in this interpreter and the base has no `hoh` at the root: a root `hoh/` shadows it
    out = findings_for(seeded, {"hoh/__init__.py": ""}, test=importing)
    assert any(line.startswith("hoh/__init__.py: new") and "'hoh'" in line for line in out), out


def test_the_implementation_a_test_imports_is_free_to_change_and_to_appear(seeded):
    importing = "import sys\nimport newmod\nfrom app import add\nassert add(2, 2) == 4\n"
    # `app` is the base's own module; `newmod` is the slice's new one and nothing resolves it yet
    out = findings_for(seeded, {"app.py": "def add(a, b):\n    return a + b  # reworked\n", "newmod.py": "x = 1\n",
                                "notes.txt": "n\n", "docs/readme.md": "d\n", "src/pkg/impl.py": "y = 2\n"},
                       test=importing)
    assert out == [], out


def test_the_first_directory_without_an_init_above_a_test_file_is_on_sys_path_too(seeded):
    """pytest's `prepend` import mode puts `tests/` (the first directory above `tests/pkg/test_deep.py`
    that holds no `__init__.py`) first on `sys.path`: a module there shadows pytest's runtime."""
    deep = "tests/pkg/test_deep.py"
    commit_files(seeded, {"tests/pkg/__init__.py": b""})              # part of the base
    text = spec_text(pins={deep: sha(GOOD)}, criteria=(f"K1 python3 -B -m pytest -q {deep}",))
    before, tree = commit_files(seeded, {deep: GOOD.encode(), "tests/pluggy.py": b"x = 1\n"})
    out = spec_criteria.pin_findings(text, repo=str(seeded), tree=tree, base_tree=before)
    assert any(line.startswith("tests/pluggy.py: new against the run's base and not pinned") for line in out), out
    # a module of another name there is the slice's own business
    before, tree = commit_files(seeded, {"tests/helper_impl.py": b"x = 1\n"})
    assert spec_criteria.pin_findings(text, repo=str(seeded), tree=tree, base_tree=before) == []


def test_a_module_the_base_provides_at_the_root_may_change_even_when_the_interpreter_resolves_the_name(seeded):
    """The base's own `hoh/` package at the root is the implementation under test: it may change although
    `hoh` also resolves in this interpreter. (Without the base providing it, the same name is a shadow.)"""
    importing = "import sys\nimport hoh\nassert hoh\n"
    commit_files(seeded, {"hoh/__init__.py": b"x = 0\n"})                 # part of the base
    out = findings_for(seeded, {"hoh/__init__.py": "x = 1\n"}, test=importing)
    assert out == [], out
    # the control: the same change under a name the base does not provide at the root is a finding
    out = findings_for(seeded, {"pluggy/__init__.py": "x = 1\n"}, test=importing)
    assert any(line.startswith("pluggy/__init__.py: new") for line in out), out


def test_a_conftest_of_a_directory_above_the_tests_is_a_finding(seeded):
    """pytest loads the `conftest.py` of every directory from the root down to a test file."""
    deep = "a/b/test_deep.py"
    text = spec_text(pins={deep: sha(GOOD)}, criteria=(f"K1 python3 -B -m pytest -q {deep}",))
    before, tree = commit_files(seeded, {deep: GOOD.encode(), "a/conftest.py": b"x = 1\n", "a/other.py": b"y = 2\n"})
    out = spec_criteria.pin_findings(text, repo=str(seeded), tree=tree, base_tree=before)
    assert any(line.startswith("a/conftest.py: new against the run's base and not pinned") for line in out), out
    assert not any(line.startswith("a/other.py") for line in out), "only the names pytest reads, not the directory"


def test_a_directory_the_pytest_options_name_is_read_for_its_configuration(seeded):
    text = spec_text(pins={TEST_FILE: sha(GOOD)},
                     criteria=(f"K1 python3 -B -m pytest -q --rootdir=sub {TEST_FILE}",))
    out = findings_for(seeded, {"sub/pytest.ini": "[pytest]\naddopts = --collect-only\n"}, text=text)
    assert any(line.startswith("sub/pytest.ini: new against the run's base and not pinned") for line in out), out


def test_a_plugin_or_configuration_file_the_options_name_must_be_pinned(seeded):
    text = spec_text(pins={TEST_FILE: sha(GOOD)},
                     criteria=(f"K1 python3 -B -m pytest -q -p myplug -c custom.ini {TEST_FILE}",))
    out = findings_for(seeded, {"myplug.py": "x = 1\n", "custom.ini": "[pytest]\n"}, text=text)
    assert "myplug.py: run by a criterion but not pinned" in out
    assert "custom.ini: run by a criterion but not pinned" in out


def test_a_plugin_and_a_configuration_file_the_options_name_are_accepted_when_pinned(seeded):
    pinned = spec_text(pins={TEST_FILE: sha(GOOD), "myplug.py": sha("x = 1\n"), "custom.ini": sha("[pytest]\n")},
                       criteria=(f"K1 python3 -B -m pytest -q -p myplug -c custom.ini {TEST_FILE}",))
    assert findings_for(seeded, {"myplug.py": "x = 1\n", "custom.ini": "[pytest]\n"}, text=pinned) == []


def test_a_pytest_criterion_that_names_a_file_not_called_test_star_pins_it_too():
    text = spec_text(pins={TEST_FILE: sha(GOOD)},
                     criteria=(f"K1 {CRITERION}", "K2 python3 -B -m pytest -q tests/check_b.py::test_y"))
    with pytest.raises(spec_criteria.PinsBlockError, match="tests/check_b.py"):
        spec_criteria.declared_pins(text)
    # control: a command that is not pytest names a tool, not a test
    spec_criteria.declared_pins(spec_text(pins={TEST_FILE: sha(GOOD)},
                                          criteria=(f"K1 {CRITERION}", "K2 python3 -B tools/check_b.py")))


def test_a_file_a_pytest_criterion_names_is_watched_in_its_directory(seeded):
    """`tests/check_b.py` pinned, run by pytest although it is not called test_*: its directory
    is a test directory, so a conftest added there is a finding."""
    text = spec_text(pins={TEST_FILE: sha(GOOD), "other/check_b.py": sha(GOOD)},
                     criteria=(f"K1 {CRITERION}", "K2 python3 -B -m pytest -q other/check_b.py::test_y"))
    before, tree = commit_files(seeded, {TEST_FILE: GOOD.encode(), "other/check_b.py": GOOD.encode(),
                                         "other/conftest.py": b"x = 1\n"})
    out = spec_criteria.pin_findings(text, repo=str(seeded), tree=tree, base_tree=before)
    assert any(line.startswith("other/conftest.py: new against the run's base and not pinned") for line in out), out


# --- hoh run: the same refusal on the operator's own path --------------------------------------------


def test_hoh_run_refuses_a_re_written_test_through_the_real_cli(tmp_path, seeded, spec, monkeypatch, capsys):
    import types

    from hoh import cli, dispatchers, integrity
    from hoh import controller as controller_module

    spec.write_text(PINNED, encoding="utf-8")
    runs = tmp_path / "runs"
    store = RunStore(runs, "r1")
    store.write_state(Controller.new_state(run_id="r1", repo_path=seeded, project_name="project",
                                           spec_path=spec, budgets=Budgets(max_iterations=5)))
    fake = FakeDispatcher(repo=seeded, develop=writes(**{TEST_FILE: REWRITTEN}),
                          plan_json=lambda s: plan_for(s, command=CRITERION), qa_json=qa_pass)
    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        types.SimpleNamespace(ledger=tmp_path / "ledger"), tmp_path / "storage"))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))

    class NoAdmission(controller_module.Controller):
        def __init__(self, *a, **kw):
            kw["storage_admission"] = None
            super().__init__(*a, **kw)

    monkeypatch.setattr(controller_module, "Controller", NoAdmission)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: fake)
    cli.main(["--root", str(runs), "run", "r1", "--no-herdr", "--iterations", "1"])
    out = capsys.readouterr().out
    state = store.read_state()
    assert state.last_accepted_candidate is None and Role.QA not in fake.calls
    assert "TEST_CONTENT_FIDELITY" in out or any("TEST_CONTENT_FIDELITY" in h for h in state.history)


# --- hoh import: the accepted candidate's tree against the pins of its specification ------------------


class Accepted:
    """An integration repository, a development worktree whose commit is the accepted candidate, a
    specification and a run state that names them (spec_path, spec_digest, base_candidate)."""

    def __init__(self, tmp_path, *, test_text=GOOD, spec_body=PINNED, with_spec=True):
        self.tmp = tmp_path
        self.integ = tmp_path / "integ"
        self.integ.mkdir()
        git(self.integ, "init", "-q", "-b", "main")
        (self.integ / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.integ / "tests").mkdir()
        (self.integ / "tests" / "helpers.py").write_text("HELP = 1\n", encoding="utf-8")
        git(self.integ, "add", "-A")
        git(self.integ, "commit", "-qm", "base")
        self.base = git(self.integ, "rev-parse", "HEAD")
        self.dev = tmp_path / "dev"
        git(self.integ, "worktree", "add", "-q", "--detach", str(self.dev), self.base)
        (self.dev / TEST_FILE).write_text(test_text, encoding="utf-8")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", "candidate")
        self.cand = git(self.dev, "rev-parse", "HEAD")
        self.tree = git(self.dev, "rev-parse", "HEAD^{tree}")
        self.base_tree = git(self.dev, "rev-parse", self.base + "^{tree}")
        self.spec = tmp_path / "spec.md"
        self.spec.write_text(spec_body, encoding="utf-8")
        self.state = tmp_path / "state.json"
        body = {"run_id": "r1",
                "base_candidate": {"candidate_id": "r1-base", "repo_path": str(self.dev), "commit": self.base,
                                   "tree_clean": True, "tree_digest": self.base_tree},
                "last_accepted_candidate": {"candidate_id": "r1-i1-a1", "repo_path": str(self.dev),
                                            "commit": self.base, "tree_clean": False, "tree_digest": self.tree}}
        if with_spec:
            body["spec_path"] = str(self.spec)
            body["spec_digest"] = hashlib.sha256(spec_body.encode()).hexdigest()[:16]
        self.state.write_text(json.dumps(body), encoding="utf-8")

    def args(self, **over):
        base = dict(repo=str(self.integ), run_state=str(self.state), commit=self.cand, name="x",
                    receipt=str(self.tmp / "import-x.json"))
        base.update(over)
        return base


def refs(repo) -> str:
    return git(repo, "for-each-ref", "refs/imports/")


def test_an_import_of_the_pinned_bytes_says_VERIFIED_in_its_receipt(tmp_path):
    from hoh import importref

    run = Accepted(tmp_path)
    out = importref.run_import(**run.args())
    assert out.code == 0 and "TEST-PINS: VERIFIED" in out.out, (out.out, out.err)
    receipt = json.loads((tmp_path / "import-x.json").read_text())
    assert receipt["test_pins"] == {"status": "VERIFIED", "pins": 1, "spec_digest": json.loads(run.state.read_text())["spec_digest"]}


def test_an_import_of_re_written_tests_is_refused_and_creates_no_ref(tmp_path):
    from hoh import importref

    run = Accepted(tmp_path, test_text=REWRITTEN)
    out = importref.run_import(**run.args())
    assert out.code == 2 and out.out == ""
    assert "TEST_CONTENT_FIDELITY" in out.err and sha(GOOD) in out.err and sha(REWRITTEN) in out.err
    assert refs(run.integ) == "" and not (tmp_path / "import-x.json").exists()


def test_an_import_of_a_candidate_with_an_unpinned_conftest_is_refused(tmp_path):
    from hoh import importref

    run = Accepted(tmp_path)
    (run.dev / "tests" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    git(run.dev, "add", "-A")
    git(run.dev, "commit", "-qm", "more")
    tree = git(run.dev, "rev-parse", "HEAD^{tree}")
    state = json.loads(run.state.read_text())
    state["last_accepted_candidate"]["tree_digest"] = tree
    run.state.write_text(json.dumps(state), encoding="utf-8")
    out = importref.run_import(**run.args(commit=git(run.dev, "rev-parse", "HEAD")))
    assert out.code == 2 and "tests/conftest.py: new against the run's base and not pinned" in out.err
    assert refs(run.integ) == ""


def test_a_specification_that_is_not_the_runs_or_cannot_be_read_refuses_the_import(tmp_path):
    from hoh import importref

    run = Accepted(tmp_path)
    run.spec.write_text(spec_text(pins={TEST_FILE: sha(REWRITTEN)}), encoding="utf-8")      # another text
    out = importref.run_import(**run.args())
    assert out.code == 2 and "the pins of another text are not the run's" in out.err and refs(run.integ) == ""
    copy = tmp_path / "copy.md"
    copy.write_text(PINNED, encoding="utf-8")                                              # the run's own text
    out = importref.run_import(**run.args(spec=str(copy)))
    assert out.code == 0 and "TEST-PINS: VERIFIED" in out.out
    (tmp_path / "second").mkdir()
    run2 = Accepted(tmp_path / "second")  # a missing specification
    (tmp_path / "second" / "spec.md").rename(tmp_path / "second" / "spec.moved")
    out = importref.run_import(**run2.args())
    assert out.code == 2 and "cannot be read" in out.err and refs(run2.integ) == ""


def test_an_import_under_a_specification_that_pins_nothing_says_NO_TEST_PINS(tmp_path):
    from hoh import importref

    run = Accepted(tmp_path, spec_body=spec_text(), test_text=REWRITTEN)
    out = importref.run_import(**run.args())
    assert out.code == 0 and "TEST-PINS: NO_TEST_PINS" in out.out
    receipt = json.loads((tmp_path / "import-x.json").read_text())
    assert receipt["test_pins"]["status"] == "NO_TEST_PINS" and receipt["test_pins"]["reason"] == spec_criteria.NO_TEST_PINS
    (tmp_path / "old").mkdir()
    old = Accepted(tmp_path / "old", with_spec=False)
    out = importref.run_import(**old.args())
    assert out.code == 0 and "TEST-PINS: NOT_RUN" in out.out and "names no specification" in out.out
    # a state that names a specification but no usable digest is not "no specification": refused
    for number, patch in enumerate(({"spec_digest": 5}, {"spec_digest": ""}, {"spec_digest": None}, {"spec_path": 7})):
        sub = tmp_path / f"half{number}"
        sub.mkdir()
        half = Accepted(sub)
        state = json.loads(half.state.read_text())
        state.update(patch)
        if patch == {"spec_digest": None}:
            state.pop("spec_digest")
        half.state.write_text(json.dumps(state), encoding="utf-8")
        out = importref.run_import(**half.args())
        assert out.code == 2 and refs(half.integ) == "", (patch, out.err[:200])


def test_hoh_import_forwards_the_spec_option_and_refuses_through_the_real_cli(tmp_path, capsys):
    from hoh import cli

    run = Accepted(tmp_path)
    copy = tmp_path / "copy.md"
    copy.write_text(PINNED, encoding="utf-8")
    run.spec.rename(tmp_path / "spec.moved")                      # the run's spec_path is gone
    argv = ["import", "--repo", str(run.integ), "--run-state", str(run.state), "--commit", run.cand,
            "--receipt", str(tmp_path / "ri.json")]
    assert cli.main(argv + ["--id", "nospec"]) == 2
    assert "cannot be read" in capsys.readouterr().err and refs(run.integ) == ""
    assert cli.main(argv + ["--id", "viacli", "--spec", str(copy)]) == 0
    assert "TEST-PINS: VERIFIED" in capsys.readouterr().out
    (tmp_path / "bad").mkdir()
    bad = Accepted(tmp_path / "bad", test_text=REWRITTEN)
    code = cli.main(["import", "--repo", str(bad.integ), "--run-state", str(bad.state), "--commit", bad.cand,
                     "--id", "bad", "--receipt", str(tmp_path / "rb.json"), "--spec", str(bad.spec)])
    assert code == 2 and "TEST_CONTENT_FIDELITY" in capsys.readouterr().err and refs(bad.integ) == ""


def test_an_odd_specification_or_run_state_refuses_the_import_and_never_hangs(tmp_path):
    import signal

    from hoh import importref

    class Blocked(BaseException):          # not an Exception: `run_import` must not be able to swallow it
        pass

    def alarm(*_):
        raise Blocked("the import blocked on a file that is not a regular file")

    signal.signal(signal.SIGALRM, alarm)
    cases = {}
    for name in ("fifo", "dir", "latin1", "utf16", "dangling", "nul", "mode0"):
        sub = tmp_path / name
        sub.mkdir()
        cases[name] = Accepted(sub)
    os.mkfifo(cases["fifo"].tmp / "fifo.md")
    (cases["dir"].tmp / "adir").mkdir()
    (cases["latin1"].tmp / "l.md").write_bytes("caf\u00e9".encode("latin-1"))
    (cases["utf16"].tmp / "u.md").write_bytes(PINNED.encode("utf-16"))
    os.symlink(cases["dangling"].tmp / "nowhere", cases["dangling"].tmp / "d.md")
    (cases["nul"].tmp / "n.md").write_bytes(b"\x00" + PINNED.encode())
    (cases["mode0"].tmp / "m.md").write_bytes(PINNED.encode())
    os.chmod(cases["mode0"].tmp / "m.md", 0)
    names = {"fifo": "fifo.md", "dir": "adir", "latin1": "l.md", "utf16": "u.md",
             "dangling": "d.md", "nul": "n.md", "mode0": "m.md"}
    for name, run in cases.items():
        state = json.loads(run.state.read_text())
        state["spec_path"] = str(run.tmp / names[name])
        run.state.write_text(json.dumps(state), encoding="utf-8")
        signal.alarm(30)
        try:
            out = importref.run_import(**run.args())
        finally:
            signal.alarm(0)
        if name == "mode0" and os.access(run.tmp / "m.md", os.R_OK):
            continue                                           # a user who reads mode 000 (root): not this case
        assert out.code == 2 and refs(run.integ) == "", (name, out)
    # a run state whose base is not a tree, or is missing, cannot be compared: refused when pins exist
    for number, patch in enumerate(({"base_candidate": None}, {"base_candidate": {"tree_digest": "nope"}},
                                    {"base_candidate": {"tree_digest": "0" * 40}})):
        sub = tmp_path / f"base{number}"
        sub.mkdir()
        run = Accepted(sub)
        state = json.loads(run.state.read_text())
        state.update(patch)
        run.state.write_text(json.dumps(state), encoding="utf-8")
        out = importref.run_import(**run.args())
        assert out.code == 2 and "TEST_CONTENT_FIDELITY" in out.err and refs(run.integ) == "", (patch, out)


def test_the_size_a_specification_is_read_at_is_a_bound_with_both_sides(tmp_path):
    from hoh import importref

    def padded(total: int) -> str:
        body = PINNED + "\n<!--"
        filler = total - len(body.encode()) - len("-->")
        return body + "x" * filler + "-->"

    at = padded(importref.MAX_SPEC_BYTES)
    assert len(at.encode()) == importref.MAX_SPEC_BYTES
    (tmp_path / "at").mkdir()
    run = Accepted(tmp_path / "at", spec_body=at)
    out = importref.run_import(**run.args())
    assert out.code == 0 and "TEST-PINS: VERIFIED" in out.out, (out.out[:300], out.err[:300])
    over = padded(importref.MAX_SPEC_BYTES + 1)
    (tmp_path / "over").mkdir()
    run = Accepted(tmp_path / "over", spec_body=over)
    out = importref.run_import(**run.args())
    assert out.code == 2 and "cannot be read" in out.err and refs(run.integ) == ""


def test_a_wrapped_pytest_command_is_opened_for_its_directory_too(seeded):
    before, tree = commit_files(seeded, {TEST_FILE: GOOD.encode()})
    wrapped = "sh -c 'python3 -B -m pytest -q -p no:cacheprovider tests'"
    out = spec_criteria.pin_findings(spec_text(pins={TEST_FILE: sha(GOOD)}, criteria=(f"K1 {wrapped}",)),
                                     repo=str(seeded), tree=tree, base_tree=before)
    assert "tests/helpers.py: run by a criterion but not pinned" in out
    assert "tests/test_other.py: run by a criterion but not pinned" in out
