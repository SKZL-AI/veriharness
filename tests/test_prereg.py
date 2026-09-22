"""Does the freeze notice a change, and does it refuse to notice nothing?

The pre-registration exists so that "the instrument was frozen during the
campaign" is a measured statement rather than a recollection. So the tests
that matter are the ones where something moved: a frozen file edited, a hidden
test added, a task file removed. Each must come back `DRIFTED` and name the
path, because a freeze that reports only a boolean tells a reader that
something is wrong and not what.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HOH = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "prereg", HOH / "tools" / "prereg.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prereg"] = mod
    spec.loader.exec_module(mod)
    return mod


pr = _load()


@pytest.fixture
def tree_(tmp_path, monkeypatch):
    """A miniature repository with one frozen file and one frozen directory."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "P.md").write_text("protocol\n", encoding="utf-8")
    tasks_ = tmp_path / "tasks" / "slug_pair"
    tasks_.mkdir(parents=True)
    (tasks_ / "SPEC.md").write_text("spec\n", encoding="utf-8")
    (tasks_ / "hidden_test.py").write_text("assert True\n", encoding="utf-8")

    monkeypatch.setattr(pr, "HOH", tmp_path)
    monkeypatch.setattr(pr, "FROZEN", {
        "docs/P.md": "the protocol",
        "tasks": "the tasks and their hidden suites",
    })
    monkeypatch.setattr(pr, "_commit", lambda: "0" * 40)
    monkeypatch.setattr(pr, "_dirty", lambda: [])
    # The fixture has no git repository, so the two git-backed properties are
    # stubbed to their healthy answer. Each has its own test that sets them to
    # the unhealthy one -- stubbing them here and nowhere else would be a
    # fixture that quietly grants what the check exists to ask.
    monkeypatch.setattr(pr, "_tracked", lambda rel: True)
    monkeypatch.setattr(pr, "digests_in_commit", lambda c: pr.digeste()[0])
    return tmp_path


def _freeze(**kw):
    class A:
        campaign = "v3"
        note = ""
        allow_dirty = False
    a = A()
    for k, v in kw.items():
        setattr(a, k, v)
    return pr.cmd_freeze(a)


def test_a_freeze_records_every_file_under_a_frozen_directory(tree_):
    assert _freeze() == 0
    reg = json.loads(pr.path_for("v3").read_text())
    assert set(reg["digests"]) == {
        "docs/P.md", "tasks/slug_pair/SPEC.md", "tasks/slug_pair/hidden_test.py"}
    assert pr.comparison("v3")["verdict"] == "FROZEN"


def test_a_symlinked_directory_in_a_frozen_tree_is_reported_not_followed(tree_):
    """`rglob` does not descend into one, so files behind it were invisible
    to the digest set and perfectly visible to the benchmark."""
    elsewhere = tree_ / "anderswo"
    elsewhere.mkdir()
    (elsewhere / "extra_hidden.py").write_text("assert True\n", encoding="utf-8")
    (tree_ / "tasks" / "slug_pair" / "extra").symlink_to(elsewhere)

    digeste, problems = pr.digeste()
    assert any("symlinked directory" in x for x in problems)
    assert "tasks/slug_pair/extra" in digeste
    assert digeste["tasks/slug_pair/extra"].startswith("SYMLINK:")
    assert not any("extra_hidden" in k for k in digeste)

    _freeze()
    assert pr.comparison("v3")["verdict"] == "DRIFTED"


def test_a_registration_frozen_from_a_dirty_tree_does_not_read_as_frozen(
        tree_, monkeypatch):
    """`working_tree_clean_at_freeze` had no consumer anywhere.

    A registration naming a commit that never contained the instrument reads
    as evidence, which is worse than no registration at all.
    """
    monkeypatch.setattr(pr, "_dirty", lambda: [" M tools/benchmark.py"])
    assert _freeze(allow_dirty=True) == 0
    monkeypatch.setattr(pr, "_dirty", lambda: [])
    v = pr.comparison("v3")
    assert v["verdict"] == "DRIFTED"
    assert v["working_tree_clean_at_freeze"] is False


def test_an_edited_protocol_is_drift_and_is_named(tree_):
    _freeze()
    (tree_ / "docs" / "P.md").write_text("protocol, amended\n", encoding="utf-8")
    v = pr.comparison("v3")
    assert v["verdict"] == "DRIFTED"
    assert v["changed"] == ["docs/P.md"]


def test_an_added_hidden_test_is_drift(tree_):
    """The case a modification-only check would miss.

    A hidden suite that gains a file changes what the campaign measures just
    as much as one that is edited, and it is the easier mistake to make
    innocently.
    """
    _freeze()
    (tree_ / "tasks" / "slug_pair" / "hidden_extra.py").write_text(
        "assert True\n", encoding="utf-8")
    v = pr.comparison("v3")
    assert v["verdict"] == "DRIFTED"
    assert v["added"] == ["tasks/slug_pair/hidden_extra.py"]


def test_a_removed_task_file_is_drift(tree_):
    _freeze()
    target = tree_ / "tasks" / "slug_pair" / "SPEC.md"
    target.rename(target.with_suffix(".md.parked"))
    v = pr.comparison("v3")
    assert v["verdict"] == "DRIFTED"
    assert "tasks/slug_pair/SPEC.md" in v["removed"]


def test_an_unregistered_campaign_is_not_reported_as_frozen(tree_):
    """`NOT_REGISTERED` is not a pass. It is the same rule the readiness tool
    applies to a check that did not run."""
    v = pr.comparison("v3")
    assert v["verdict"] == "NOT_REGISTERED"
    assert not v["registered"]


def test_freezing_over_an_existing_registration_parks_it(tree_):
    """Nothing is overwritten: a second freeze of the same campaign is either
    a mistake or a new campaign, and both have to stay visible."""
    _freeze()
    first = json.loads(pr.path_for("v3").read_text())
    (tree_ / "docs" / "P.md").write_text("protocol v2\n", encoding="utf-8")
    _freeze()
    second = json.loads(pr.path_for("v3").read_text())

    parked = list(pr.path_for("v3").parent.glob("PREREGISTRATION.json.v*"))
    assert len(parked) == 1
    assert json.loads(parked[0].read_text())["digests"] == first["digests"]
    assert second["replaces"] == parked[0].name
    assert second["digests"] != first["digests"]


def test_the_named_commit_is_checked_and_not_only_the_digests(tree_, monkeypatch):
    """The registration is not inside its own digest set, and it decides the
    campaign's repetition count.

    An adversarial review rewrote one field to point at a commit whose
    protocol says `repetitions = 1`; `check` said FROZEN and a 15-run campaign
    certified as complete. So the commit is compared too: a rewritten field
    points at a tree whose files hash differently.
    """
    monkeypatch.setattr(pr, "_dirty", lambda: [])
    _freeze()
    # The commit the registration names contains something else.
    monkeypatch.setattr(pr, "digests_in_commit",
                        lambda c: {"docs/P.md": "0" * 16})
    v = pr.comparison("v3")
    assert v["verdict"] == "DRIFTED"
    assert v["differs_from_the_named_commit"] == ["docs/P.md"]


def test_a_registration_that_is_not_committed_does_not_read_as_frozen(
        tree_, monkeypatch):
    """A pre-registration that exists only in a working directory is a draft."""
    monkeypatch.setattr(pr, "_dirty", lambda: [])
    monkeypatch.setattr(pr, "_tracked", lambda rel: False)
    _freeze()
    v = pr.comparison("v3")
    assert not v["registration_is_tracked"]
    assert v["verdict"] == "DRIFTED"


def test_the_environment_is_recorded_because_it_is_not_frozen(tree_, monkeypatch):
    """Digests say the files did not change. They say nothing about the
    interpreter, the packages, or which model answered."""
    monkeypatch.setattr(pr, "_dirty", lambda: [])
    _freeze()
    reg = json.loads(pr.path_for("v3").read_text())
    u = reg["environment"]
    assert u["python"]
    assert "not pinned" in u["model"]
    assert set(u["environment_digests"]) >= {"HOH_HOUSE_RULES", "HERDR_ENV"}


def test_a_dirty_tree_is_refused_unless_the_caller_says_so(tree_, monkeypatch):
    """The commit recorded in the freeze has to describe the frozen state.

    With uncommitted changes it does not, and a registration naming a commit
    that never contained the instrument is worse than none: it reads as
    evidence.
    """
    monkeypatch.setattr(pr, "_dirty", lambda: [" M tools/benchmark.py"])
    assert _freeze() == 1
    assert not pr.path_for("v3").exists()

    assert _freeze(allow_dirty=True) == 0
    reg = json.loads(pr.path_for("v3").read_text())
    assert reg["working_tree_clean_at_freeze"] is False


def test_the_real_frozen_set_names_the_things_a_campaign_must_not_change():
    """The list is the claim, so it is worth a test of its own.

    Each of these was named by the advisory as invalidating: the runner, the
    budget semantics, the tasks with their hidden tests, and the protocol.
    """
    for path in ("docs/BENCHMARK_PROTOCOL.md", "tools/benchmark.py",
                 "dogfood/benchmark/tasks", "src/hoh",
                 "tools/repetition_plan.py", "tools/readiness.py",
                 "tools/prereg.py", "pyproject.toml"):
        assert path in pr.FROZEN, f"{path} can change without being noticed"
        assert pr.FROZEN[path], f"{path} is frozen without a stated reason"
