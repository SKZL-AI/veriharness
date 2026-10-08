"""The topology of a self-hosted run, checked against real repositories.

O177 established the failure: a run whose development worktree shares a
repository with the tree the orchestrator is committing to gets its witnesses
invalidated by that orchestrator, and cannot tell that apart from the agent
having done it. The prose repair -- "isolated clones, not worktrees" -- survived
exactly until a stronger attribution category looked reachable from a worktree,
at which point the orchestrator proposed going back. So these cases are about
the check that refuses instead of the document that asks.

Everything here builds real git repositories and reads the answers back out of
them. A test double would decide the one thing under test: which checkouts can
move each other's history.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "self_hosted_topology", ROOT / "tools" / "self_hosted_topology.py")
st = importlib.util.module_from_spec(_spec)
sys.modules["self_hosted_topology"] = st
_spec.loader.exec_module(st)


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def repo_with_a_commit(path: Path, *, text: str = "one") -> Path:
    """A real repository with one real commit, identity set locally."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "file.txt").write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "first")
    return path


def clone_at(source: Path, target: Path, commit: str) -> Path:
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks",
                    str(source), str(target)], check=True,
                   capture_output=True, text=True)
    git(target, "config", "user.email", "test@example.invalid")
    git(target, "config", "user.name", "Test")
    git(target, "checkout", "--quiet", commit, "-B", "work")
    return target


def worktree_of(repo: Path, where: Path, branch: str) -> Path:
    git(repo, "worktree", "add", "--quiet", "-b", branch, str(where))
    return where


# --- the falsified arrangement itself ---------------------------------------

def test_a_worktree_of_the_integration_tree_is_refused(tmp_path):
    """The O177 arrangement. A commit in the integration tree moves history under
    the run, and no amount of care in the run can compensate for that."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    dev = worktree_of(integration, tmp_path / "dev", "dev")

    problems = st.check(dev, pin, integration=integration, runs=None)
    assert problems
    assert any("same repository" in p and "O177" in p for p in problems)


def test_the_same_run_in_a_dedicated_clone_is_sound(tmp_path):
    """The negative control for the case above: only the topology changes. The
    run is over the same commit, doing the same work."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    dev = worktree_of(target, tmp_path / "dev", "dev")

    assert st.check(dev, pin, integration=integration, runs=None) == []


def test_repository_identity_is_the_repository_not_the_checkout(tmp_path):
    """The discrimination the whole check rests on. `--git-dir` differs per
    worktree, which would make every worktree look independent -- the exact
    confusion that made the O177 topology look safe."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    dev = worktree_of(integration, tmp_path / "dev", "dev")
    target = clone_at(integration, tmp_path / "target", pin)

    assert st.repository_identity(dev) == st.repository_identity(integration)
    assert st.repository_identity(target) != st.repository_identity(integration)


# --- the pin ----------------------------------------------------------------

def test_a_clone_over_a_different_base_cannot_bind_and_is_refused(tmp_path):
    """A candidate produced over a different base has a tree that cannot be
    reproduced on the pin, so the import would not bind -- and an attribution
    claim would then cover contents this run never authored."""
    integration = repo_with_a_commit(tmp_path / "integration")
    first = git(integration, "rev-parse", "HEAD")
    (integration / "file.txt").write_text("two", encoding="utf-8")
    git(integration, "commit", "--quiet", "-am", "second")
    second = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", first)
    dev = worktree_of(target, tmp_path / "dev", "dev")

    problems = st.check(dev, second, integration=integration, runs=None)
    assert any("not at the pin" in p for p in problems)
    # The control: the same clone, pinned to what it is actually on.
    assert st.check(dev, first, integration=integration, runs=None) == []


def test_no_pin_is_not_a_pass(tmp_path):
    """"Pinned to the exact source commit" has to be checkable to mean
    anything. An empty pin is an unasked question, not a satisfied one."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    dev = worktree_of(target, tmp_path / "dev", "dev")

    problems = st.check(dev, "", integration=integration, runs=None)
    assert any("not a checkable statement" in p for p in problems)


def test_a_pin_absent_from_the_integration_repository_is_refused(tmp_path):
    """There would be nothing to parent the import commit on, so the product's
    bytes could never be brought in unchanged."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    (target / "file.txt").write_text("local", encoding="utf-8")
    git(target, "commit", "--quiet", "-am", "only in the clone")
    stranger = git(target, "rev-parse", "HEAD")
    dev = worktree_of(target, tmp_path / "dev", "dev")

    problems = st.check(dev, stranger, integration=integration, runs=None)
    assert any("not an object in the integration repository" in p for p in problems)


def test_a_dirty_run_repository_is_refused_before_the_run_starts(tmp_path):
    """Then the candidate's tree carries contents nobody authored in the run,
    and the attribution claim would cover them."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    dev = worktree_of(target, tmp_path / "dev", "dev")
    assert st.check(dev, pin, integration=integration, runs=None) == []

    (target / "stray.txt").write_text("not from the run", encoding="utf-8")
    problems = st.check(dev, pin, integration=integration, runs=None)
    assert any("dirty before the run starts" in p for p in problems)


# --- two runs on one history ------------------------------------------------

def _state(path: Path, *, run_id: str, repo: Path, condition: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "repo_path": str(repo),
                                "condition": condition}), encoding="utf-8")


def test_another_live_run_on_the_same_history_is_refused(tmp_path):
    """Whichever commits first moves the ground under the other -- O177 with two
    agents instead of an agent and an orchestrator."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    mine = worktree_of(target, tmp_path / "mine", "mine")
    theirs = worktree_of(target, tmp_path / "theirs", "theirs")
    runs = tmp_path / "runs"
    _state(runs / "other" / "state.json", run_id="other", repo=theirs,
           condition="ACTIVE")

    problems = st.check(mine, pin, integration=integration, runs=runs)
    assert any("another live run holds the same repository" in p for p in problems)


def test_a_finished_run_on_the_same_history_is_not_a_problem(tmp_path):
    """The negative control: only the condition changes. A run that is over has
    no witness left to break, so refusing here would be superstition."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    mine = worktree_of(target, tmp_path / "mine", "mine")
    theirs = worktree_of(target, tmp_path / "theirs", "theirs")
    runs = tmp_path / "runs"
    _state(runs / "other" / "state.json", run_id="other", repo=theirs,
           condition="ACCEPTED")

    assert st.check(mine, pin, integration=integration, runs=runs) == []


def test_the_run_being_checked_does_not_flag_itself(tmp_path):
    """`hoh start` writes a state before `hoh run` is called, so a check that
    matched on repository alone would refuse every sound topology there is."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    dev = worktree_of(target, tmp_path / "dev", "dev")
    runs = tmp_path / "runs"
    _state(runs / "mine" / "state.json", run_id="mine", repo=dev,
           condition="ACTIVE")

    assert st.check(dev, pin, integration=integration, runs=runs) == []


# --- the narrow exception ---------------------------------------------------

def test_the_exclusive_exception_does_not_excuse_a_live_integration_tree(tmp_path):
    """A worktree may be used only where no concurrent history movement is
    possible. Uncommitted changes in the integration tree are what a tree being
    worked in looks like, so the exception does not apply there."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    dev = worktree_of(integration, tmp_path / "dev", "dev")

    assert st.check(dev, pin, integration=integration, runs=None,
                    exclusive="serialized: nothing else runs") == []

    (integration / "file.txt").write_text("being worked in", encoding="utf-8")
    problems = st.check(dev, pin, integration=integration, runs=None,
                        exclusive="serialized: nothing else runs")
    assert any("uncommitted changes" in p for p in problems)


# --- the import binding -----------------------------------------------------

def _accepted(path: Path, *, tree: str, candidate_id: str = "c1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": "r1",
        "last_accepted_candidate": {"candidate_id": candidate_id,
                                    "tree_digest": tree},
    }), encoding="utf-8")
    return path


def test_a_byte_preserving_import_binds_from_a_foreign_clone(tmp_path):
    """The reason isolation never has to be weakened for attribution: a git tree
    is content-addressed, so the candidate's tree hash is reproduced exactly by
    an import commit carrying the same contents -- in a different repository,
    with a different commit sha."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    (target / "built.txt").write_text("what the product wrote", encoding="utf-8")
    git(target, "add", "-A")
    tree = git(target, "write-tree")

    # The same contents, brought into the integration repository.
    (integration / "built.txt").write_text("what the product wrote", encoding="utf-8")
    git(integration, "add", "-A")
    assert git(integration, "write-tree") == tree, \
        "identical contents must hash to the identical tree, or the whole " \
        "argument for the clone topology fails"
    git(integration, "commit", "--quiet", "-m", "import")
    imported = git(integration, "rev-parse", "HEAD")

    state = _accepted(tmp_path / "runs" / "r1" / "state.json", tree=tree)
    assert st.verify_import(state, imported, integration=integration) == []


def test_an_import_that_changed_a_byte_is_refused(tmp_path):
    """The negative control, and the case that matters: a merge carrying review
    fixes alongside the product's bytes cannot be claimed as the product's
    work, and this is what catches it before the claim is written."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    (target / "built.txt").write_text("what the product wrote", encoding="utf-8")
    git(target, "add", "-A")
    tree = git(target, "write-tree")

    (integration / "built.txt").write_text("what the product wrote, edited",
                                           encoding="utf-8")
    git(integration, "add", "-A")
    git(integration, "commit", "--quiet", "-m", "import with a fix folded in")
    imported = git(integration, "rev-parse", "HEAD")

    state = _accepted(tmp_path / "runs" / "r1" / "state.json", tree=tree)
    problems = st.verify_import(state, imported, integration=integration)
    assert any("did not preserve the product's bytes" in p for p in problems)


def test_a_run_state_with_nothing_accepted_cannot_bind(tmp_path):
    integration = repo_with_a_commit(tmp_path / "integration")
    head = git(integration, "rev-parse", "HEAD")
    state = tmp_path / "runs" / "r1" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")

    problems = st.verify_import(state, head, integration=integration)
    assert any("no accepted candidate" in p for p in problems)


def test_a_candidate_without_a_tree_digest_cannot_bind(tmp_path):
    integration = repo_with_a_commit(tmp_path / "integration")
    head = git(integration, "rev-parse", "HEAD")
    state = _accepted(tmp_path / "runs" / "r1" / "state.json", tree="")
    problems = st.verify_import(state, head, integration=integration)
    assert any("no tree_digest" in p for p in problems)


# --- the command surface ----------------------------------------------------

def test_the_command_returns_codes_rather_than_raising(tmp_path, capsys):
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    dev = worktree_of(integration, tmp_path / "dev", "dev")
    assert st.main(["check", "--repo", str(dev), "--pin", pin,
                    "--integration", str(integration)]) == 1

    target = clone_at(integration, tmp_path / "target", pin)
    good = worktree_of(target, tmp_path / "good", "good")
    assert st.main(["check", "--repo", str(good), "--pin", pin,
                    "--integration", str(integration)]) == 0
    assert "sound" in capsys.readouterr().out


def test_a_path_that_is_not_a_checkout_is_an_unaskable_question(tmp_path):
    integration = repo_with_a_commit(tmp_path / "integration")
    nothing = tmp_path / "not-a-repo"
    nothing.mkdir()
    problems = st.check(nothing, "abc", integration=integration, runs=None)
    assert any("not a git checkout" in p for p in problems)


def test_a_missing_run_worktree_is_named(tmp_path):
    integration = repo_with_a_commit(tmp_path / "integration")
    problems = st.check(tmp_path / "gone", "abc", integration=integration,
                        runs=None)
    assert problems == [f"the run worktree is not there: {tmp_path / 'gone'}"]


@pytest.mark.parametrize("condition", sorted(st.LIVE_CONDITIONS))
def test_every_live_condition_counts_as_holding_the_history(tmp_path, condition):
    """A parked or blocked run still owns its worktree and its witnesses; only a
    finished one does not."""
    integration = repo_with_a_commit(tmp_path / "integration")
    pin = git(integration, "rev-parse", "HEAD")
    target = clone_at(integration, tmp_path / "target", pin)
    mine = worktree_of(target, tmp_path / "mine", "mine")
    theirs = worktree_of(target, tmp_path / "theirs", "theirs")
    runs = tmp_path / "runs"
    _state(runs / "other" / "state.json", run_id="other", repo=theirs,
           condition=condition)
    assert st.live_runs_sharing(runs, st.repository_identity(mine), own=mine)
