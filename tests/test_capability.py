"""The role capability boundary, attacked the way an agent would attack it.

O125: the planner's prompt says *implement nothing, edit nothing, test
nothing*, a real run showed it doing all three, and the only thing that
stopped it was a permission gate belonging to the surrounding harness rather
than to HoH.

The correction that came out of measuring it matters for what these tests
check. The planner never could reach the repository or the candidate -- A03
gives it a copy. What it could reach was the **arena root**, where the
candidate arena of every iteration is a sibling of that copy: the tree the
acceptance checks run in.

So two properties, tested separately:

* **Placement** -- the candidate arenas are no longer where the planner is.
* **Detection** -- every way around placement is caught afterwards, because
  the check compares bytes rather than reasoning about how they got there.

Every attack below is paired with a positive control. A boundary that refuses
everything is as useless as one that refuses nothing, and the planner has real
work to do.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hoh.capability import (
    Capability,
    CapabilityViolation,
    CapabilityWitness,
    RoleExecutionPolicy,
    denied_policy,
    developer_policy,
    planner_policy,
    qa_policy,
    tree_digest,
)


@pytest.fixture
def world(tmp_path):
    """An arena root shaped like a real run's."""
    arenas = tmp_path / "_arenas" / "r"
    planner_root = arenas / "planner"
    copy_dir = planner_root / "abc123"
    candidate = arenas / "def456"
    answers = tmp_path / "r" / "answers"
    for d in (copy_dir, candidate, answers):
        d.mkdir(parents=True)
    (copy_dir / "roman.py").write_text("def to_roman(n):\n    raise NotImplementedError\n")
    (candidate / "roman.py").write_text("def to_roman(n):\n    return 'I'\n")
    (candidate / "tests").mkdir()
    (candidate / "tests" / "test_roman.py").write_text("def test_x():\n    pass\n")
    return {
        "arenas": arenas, "planner_root": planner_root, "copy": copy_dir,
        "candidate": candidate, "answer": answers / "planner.json",
    }


def planner(world, protected=None):
    return planner_policy(
        read_copy=world["planner_root"], answer=world["answer"],
        protected=protected if protected is not None else (world["candidate"],),
    )


# --------------------------------------------------------------------------- #
# Placement: the candidate is not where the planner is
# --------------------------------------------------------------------------- #


def test_the_candidate_arena_is_not_a_sibling_of_the_planners_copy(world):
    """The finding itself. The planner's copy used to sit directly under the
    arena root, next to every candidate arena."""
    siblings = {p.name for p in world["planner_root"].iterdir()}
    assert world["candidate"].name not in siblings
    assert world["candidate"].parent != world["planner_root"]


def test_the_planner_may_write_its_own_answer_and_no_other(world):
    """Not the answers directory -- the answer file.

    Every role answers into the same directory, so a write scope of
    `answers/` declared that the planner may write QA's verdict. The scope is
    the file, and a reviewer comparing the policy to the prompt now reads the
    same thing in both.
    """
    p = planner(world)
    assert p.may_write(world["answer"])
    assert not p.may_write(world["answer"].parent / "i1-a1-qa.json")
    assert not p.may_write(world["candidate"] / "roman.py")
    assert not p.may_write(world["arenas"] / "anything")


# --------------------------------------------------------------------------- #
# Detection: every route around placement
# --------------------------------------------------------------------------- #


def _attack(world, do_) -> list[str]:
    """Takes a witness, performs `tun`, returns the violations."""
    witness_ = CapabilityWitness.take(planner(world))
    do_()
    return witness_.violations()


def test_overwriting_a_source_file_is_caught(world):
    def do_():
        (world["candidate"] / "roman.py").write_text("def to_roman(n):\n    return 'X'\n")

    assert _attack(world, do_), "an overwritten source file went unnoticed"


def test_moving_a_source_file_is_caught(world):
    def do_():
        (world["candidate"] / "roman.py").rename(world["candidate"] / "roman.py.bak")

    assert _attack(world, do_)


def test_creating_an_implementation_file_is_caught(world):
    def do_():
        (world["candidate"] / "impl.py").write_text("x = 1\n")

    assert _attack(world, do_)


def test_deleting_a_file_is_caught(world):
    def do_():
        (world["candidate"] / "tests" / "test_roman.py").unlink()

    assert _attack(world, do_)


def test_a_git_commit_in_the_protected_tree_is_caught(world):
    subprocess.run(["git", "init", "-q"], cwd=world["candidate"], check=True)
    witness_ = CapabilityWitness.take(planner(world))
    subprocess.run(["git", "add", "-A"], cwd=world["candidate"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=world["candidate"], check=True,
    )
    assert witness_.violations(), "a commit inside the protected tree went unnoticed"


def test_shell_redirection_into_the_protected_tree_is_caught(world):
    def do_():
        subprocess.run(
            ["/bin/bash", "-c", f"echo tampered >> {world['candidate']}/roman.py"],
            check=True,
        )

    assert _attack(world, do_)


def test_a_python_script_writing_into_the_tree_is_caught(world):
    def do_():
        subprocess.run(
            ["python3", "-c",
             f"open({str(world['candidate'] / 'roman.py')!r}, 'a').write('# x\\n')"],
            check=True,
        )

    assert _attack(world, do_)


def test_a_symlink_planted_in_the_tree_is_caught(world):
    """The digest records symlinks by name and target, so planting one is a
    change even though it adds no file content."""
    def do_():
        (world["candidate"] / "shortcut").symlink_to(world["copy"] / "roman.py")

    assert _attack(world, do_)


def test_a_dotdot_escape_from_the_planners_copy_is_caught(world):
    def do_():
        target = world["copy"] / ".." / ".." / world["candidate"].name / "roman.py"
        target.write_text("# reached via ..\n")

    assert _attack(world, do_)


def test_an_absolute_path_write_is_caught(world):
    def do_():
        Path(str(world["candidate"] / "roman.py")).write_text("# absolute\n")

    assert _attack(world, do_)


def test_a_temporary_file_moved_in_afterwards_is_caught(world):
    """The one that placement alone would miss: nothing is written *into* the
    tree until the last moment."""
    def do_():
        tmp = world["copy"] / "staged.py"
        tmp.write_text("def to_roman(n):\n    return 'I' * n\n")
        tmp.rename(world["candidate"] / "staged.py")

    assert _attack(world, do_)


def test_a_read_permission_change_alone_is_not_reported(world):
    """Honest about exactly what the check sees, which changed.

    The digest records the **executable** bit, because that is the one whose
    flip changes what a tree does: a file that becomes runnable is a file that
    can be run. The rest of the mode is not recorded -- dropping a write bit or
    a group read bit changes no byte and changes nothing a check would
    execute -- so this narrows rather than closes.

    Its counterpart is `test_a_mode_change_counts_as_a_change`, which pins the
    executable bit. The two are not in tension: one says what is watched, the
    other says what is not.
    """
    def do_():
        os.chmod(world["candidate"] / "roman.py", 0o444)

    assert _attack(world, do_) == []


def test_making_a_source_file_executable_is_reported(world):
    """The half of the mode that changes what a tree can do."""
    def do_():
        os.chmod(world["candidate"] / "roman.py", 0o755)

    (message_,) = _attack(world, do_)
    assert str(world["candidate"]) in message_


# --------------------------------------------------------------------------- #
# Positive controls: the planner still has work it can do
# --------------------------------------------------------------------------- #


def test_reading_the_candidate_is_not_a_violation(world):
    def do_():
        (world["candidate"] / "roman.py").read_text()
        (world["candidate"] / "tests" / "test_roman.py").read_text()

    assert _attack(world, do_) == []


def test_writing_in_its_own_copy_is_not_a_violation(world):
    """The copy is the planner's and is discarded. Making it read-only would
    only invite a chmod; what matters is that it is not the candidate."""
    def do_():
        (world["copy"] / "scratch.py").write_text("notes\n")
        (world["copy"] / "roman.py").write_text("# my own copy\n")

    assert _attack(world, do_) == []


def test_writing_its_answer_is_not_a_violation(world):
    def do_():
        world["answer"].write_text('{"objective": "x"}')

    assert _attack(world, do_) == []


def test_running_a_process_is_not_a_violation_by_itself(world):
    def do_():
        subprocess.run(["python3", "-c", "print(1)"], capture_output=True, check=True)

    assert _attack(world, do_) == []


# --------------------------------------------------------------------------- #
# The policy, and its defaults
# --------------------------------------------------------------------------- #


def test_an_unnamed_role_gets_nothing(world):
    """Fail-closed: adding a role without deciding what it may do must stop
    the run rather than inherit the last role's rights."""
    p = denied_policy("mystery", protected=(world["candidate"],))
    for cap in Capability:
        assert not p.allows(cap), cap.value
    assert not p.may_write(world["answer"])
    assert p.write_scopes == ()


def test_a_bare_policy_permits_nothing():
    p = RoleExecutionPolicy(role="bare")
    assert [c for c in Capability if p.allows(c)] == []
    assert p.output_channel is None
    assert p.isolation == "none"


def test_the_developer_is_the_only_role_that_writes_the_artifact(world):
    developer_ = developer_policy(arena=world["arenas"], answer=world["answer"])
    checker = qa_policy(arena=world["arenas"], answer=world["answer"])
    planner_role = planner(world)

    assert developer_.may_write(world["candidate"] / "roman.py")
    assert not checker.may_write(world["candidate"] / "roman.py")
    assert not planner_role.may_write(world["candidate"] / "roman.py")


def test_no_role_may_write_git_or_reach_the_network(world):
    for p in (planner(world),
              developer_policy(arena=world["arenas"], answer=world["answer"]),
              qa_policy(arena=world["arenas"], answer=world["answer"])):
        assert not p.allows(Capability.GIT_WRITE), p.role
        assert not p.allows(Capability.NETWORK), p.role


def test_every_role_names_the_one_file_its_answer_comes_from(world):
    for p in (planner(world),
              developer_policy(arena=world["arenas"], answer=world["answer"]),
              qa_policy(arena=world["arenas"], answer=world["answer"])):
        assert p.output_channel == world["answer"], p.role


def test_the_summary_says_what_the_role_may_do(world):
    text = planner(world).summary()
    assert "planner" in text and "fs_read" in text and "git_read" in text
    assert "git_write" not in text and "network" not in text


# --------------------------------------------------------------------------- #
# The digest, which everything above rests on
# --------------------------------------------------------------------------- #


def test_an_absent_tree_and_an_empty_tree_are_told_apart(tmp_path):
    missing = tmp_path / "nope"
    empty = tmp_path / "leer"
    empty.mkdir()
    assert tree_digest(missing) == ""
    assert tree_digest(empty) != ""


def test_a_rename_changes_the_digest(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    before = tree_digest(d)
    (d / "a.py").rename(d / "b.py")
    assert tree_digest(d) != before, "a rename left the digest unchanged"


def test_bytecode_counts_as_a_change(tmp_path):
    """It did not, and that was a way to make a stub pass its own criteria.

    A `.pyc` whose header matches the source's mtime and size is preferred at
    import over the source beside it. Ignoring `__pycache__` therefore let a
    planner leave a working implementation in a tree whose source still held
    the stub, with nothing in the digest changing.

    The cost is the reason it was ignored: a role that imports anything writes
    bytecode. That is paid for by *where* protection is applied rather than by
    blinding the digest -- a role's own workspace is never in its protected
    set, and nothing else runs while it does.
    """
    d = tmp_path / "t"
    (d / "__pycache__").mkdir(parents=True)
    (d / "a.py").write_text("x = 1\n")
    before = tree_digest(d)
    (d / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x00\x01")
    assert tree_digest(d) != before


def test_an_empty_directory_counts_as_a_change(tmp_path):
    """A directory with no files in it contains no files to notice."""
    d = tmp_path / "t"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    before = tree_digest(d)
    (d / "staging").mkdir()
    assert tree_digest(d) != before


def test_a_mode_change_counts_as_a_change(tmp_path):
    """Making a file executable is not a content change."""
    d = tmp_path / "t"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    before = tree_digest(d)
    (d / "a.py").chmod(0o755)
    assert tree_digest(d) != before


def test_a_single_file_can_be_protected(tmp_path):
    """`checks.json` is a file, and protecting it must not silently do nothing.

    A digest that returned the empty string for anything that is not a
    directory made every file in a protected set a no-op -- including the
    preservation suite, whose removal an adversarial review rode all the way
    to a false checkpoint.
    """
    f = tmp_path / "checks.json"
    f.write_text('{"checks": []}')
    before = tree_digest(f)
    assert before
    f.write_text('{"checks": ["K1"]}')
    assert tree_digest(f) != before


def test_the_violation_names_the_tree_and_both_digests(world):
    witness_ = CapabilityWitness.take(planner(world))
    (world["candidate"] / "roman.py").write_text("tampered\n")
    (message_,) = witness_.violations()
    assert str(world["candidate"]) in message_
    assert "planner" in message_
    assert "->" in message_


def test_a_witness_over_nothing_reports_nothing(world):
    witness_ = CapabilityWitness.take(planner(world, protected=()))
    (world["candidate"] / "roman.py").write_text("tampered\n")
    assert witness_.violations() == [], (
        "a policy that protects nothing must not invent a violation -- the "
        "control for every test above"
    )


def test_the_violation_is_an_exception_type_the_loop_can_catch():
    assert issubclass(CapabilityViolation, RuntimeError)


# --------------------------------------------------------------------------- #
# A role answers through its output channel, not through its pane
# --------------------------------------------------------------------------- #


def _dispatcher(tmp_path):
    from hoh.contracts import Role
    from hoh.dispatchers import HerdrDispatcher

    d = HerdrDispatcher.__new__(HerdrDispatcher)
    d.answers_dir = tmp_path / "answers"
    d.answers_dir.mkdir(parents=True, exist_ok=True)
    return d, Role


def test_a_written_answer_means_the_role_answered(tmp_path):
    """Measured on a real planner-confinement run: the plan file was written,
    the transcript said so, and the run blocked anyway because the pane was
    left at an input prompt. A role answers through exactly one file, so that
    is where the question is decided -- not in a screen scrape."""
    d, Role = _dispatcher(tmp_path)
    file_path = d.answers_dir / "i1-a0-planner.json"
    file_path.write_text('{"objective": "implement fib"}')
    assert d._answered(file_path, Role.PLANNER)


def test_a_missing_or_empty_answer_means_it_did_not(tmp_path):
    """The control. A planner stopped at a trust dialog writes nothing, and
    that case must still block."""
    d, Role = _dispatcher(tmp_path)
    missing = d.answers_dir / "nope.json"
    assert not d._answered(missing, Role.PLANNER)
    empty = d.answers_dir / "empty.json"
    empty.write_text("   \n")
    assert not d._answered(empty, Role.PLANNER)


def test_an_unstructured_role_does_not_answer_through_a_file(tmp_path):
    """Only the structured roles have a declared output channel. Treating a
    developer's file as its answer would accept a half-written one."""
    d, Role = _dispatcher(tmp_path)
    file_path = d.answers_dir / "dev.json"
    file_path.write_text("something")
    assert not d._answered(file_path, Role.DEVELOPER)


def test_an_unstructured_role_delivers_by_changing_the_arena(tmp_path):
    """The developer's answer is not a file in the answers directory -- it is
    the artifact. Measured on a real confinement run: the developer wrote
    `fib.py`, checked its own hash against the acceptance criterion, said
    *"Done -- artifact left in the working tree"*, and left its pane at a
    prompt. Herdr reports such a pane as blocked, and the run stopped on a
    dialog that was not there."""
    from hoh.capability import tree_digest

    d, Role = _dispatcher(tmp_path)
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "fib.py").write_text("def fib(n):\n    raise NotImplementedError\n")
    d.role_cwd = {Role.DEVELOPER: str(arena)}
    before = tree_digest(arena)

    assert not d._delivered(Role.DEVELOPER, before), "nothing changed yet"
    (arena / "fib.py").write_text("def fib(n):\n    return n\n")
    assert d._delivered(Role.DEVELOPER, before)


def test_a_structured_role_is_not_judged_by_the_arena(tmp_path):
    """The control in the other direction: a planner that changed the arena
    has violated its boundary, not delivered. Its delivery is its file."""
    d, Role = _dispatcher(tmp_path)
    d.role_cwd = {Role.PLANNER: str(tmp_path)}
    assert not d._delivered(Role.PLANNER, "anything")


def test_without_a_witness_nothing_is_claimed(tmp_path):
    """No `before` digest means the question was never asked, and an
    unanswered question is not a yes."""
    d, Role = _dispatcher(tmp_path)
    d.role_cwd = {Role.DEVELOPER: str(tmp_path)}
    assert not d._delivered(Role.DEVELOPER, None)


def test_a_commit_in_a_linked_worktree_moves_the_git_digest(tmp_path):
    """The shape HoH actually runs in, and the one that nearly went unseen.

    A linked worktree's `.git` is a pointer file; the directory it points at
    holds `HEAD`, `index` and `logs`, but no `refs/` and no `packed-refs` --
    those live in the common directory. Reading only the pointer's target
    digests a `HEAD` that says `ref: refs/heads/<branch>` before and after a
    commit, and sees nothing.
    """
    import subprocess

    from hoh.capability import git_state_digest

    environment_ = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local",
    }

    def git(where, *args):
        subprocess.run(["git", "-C", str(where), *args], check=True,
                       capture_output=True, env=environment_)

    main_ = tmp_path / "main"
    main_.mkdir()
    (main_ / "a.py").write_text("x = 1\n")
    git(main_, "init", "-q", "-b", "main")
    git(main_, "add", "-A")
    git(main_, "commit", "-qm", "init")

    work_ = tmp_path / "wt"
    git(main_, "worktree", "add", "-q", "-b", "arbeit", str(work_))
    assert (work_ / ".git").is_file(), "not a linked worktree"

    before = git_state_digest(work_)
    assert before

    (work_ / "a.py").write_text("x = 2\n")
    git(work_, "add", "-A")
    git(work_, "commit", "-qm", "a commit in the worktree")

    assert git_state_digest(work_) != before, (
        "a commit in a linked worktree left the git digest unchanged")


def test_reading_a_repository_does_not_move_the_git_digest(tmp_path):
    """The reason `.git` is excluded from the tree digest in the first place.

    `git status` refreshes the index. A witness that fires on that is one
    nobody leaves switched on, so the index is the one thing deliberately not
    read -- and this pins that it stays that way.
    """
    import subprocess

    from hoh.capability import git_state_digest

    environment_ = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local",
    }
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-qm", "init")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=environment_)

    before = git_state_digest(repo)
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                   check=True, capture_output=True, env=environment_)
    assert git_state_digest(repo) == before


def test_an_export_ignore_line_moves_the_git_digest(tmp_path):
    """One line removes a file from every future candidate, invisibly.

    `materialize()` builds each candidate with `git archive`, which honours
    `$GIT_DIR/info/attributes`. No ref moves, no tracked file changes, and the
    file is simply not in the next checkout. A reviewer demonstrated it against
    a digest that watched only HEAD and the refs.
    """
    import subprocess

    from hoh.capability import git_state_digest

    environment_ = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local",
    }
    repo = tmp_path / "r"
    (repo / "x").mkdir(parents=True)
    (repo / "app.py").write_text("x = 1\n")
    (repo / "guard.py").write_text("assert True\n")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-qm", "init")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=environment_)

    before = git_state_digest(repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("guard.py export-ignore\n")
    assert git_state_digest(repo) != before


def test_a_hook_or_a_config_change_moves_the_git_digest(tmp_path):
    """`core.hooksPath`, a clean filter and a hook all change what a checkout
    produces without any history moving."""
    import subprocess

    from hoh.capability import git_state_digest

    environment_ = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local",
    }
    repo = tmp_path / "r2"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-qm", "init")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=environment_)

    before = git_state_digest(repo)
    (repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\nexit 0\n")
    after_hook = git_state_digest(repo)
    assert after_hook != before

    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".hooks"],
                   check=True, capture_output=True, env=environment_)
    assert git_state_digest(repo) != after_hook


def test_the_shallow_watch_actually_reports_a_new_child(tmp_path):
    """The firing path was untested: tests asserted the policy carried a
    shallow entry, never that a change in it is reported."""
    from hoh.capability import CapabilityWitness, RoleExecutionPolicy

    root = tmp_path / "arenas"
    (root / "aaaa1111").mkdir(parents=True)
    policy_ = RoleExecutionPolicy(role="planner", protected_shallow=(root,))
    witness_ = CapabilityWitness.take(policy_)

    assert witness_.violations() == []
    (root / "staging").mkdir()

    (message_,) = witness_.violations()
    assert "gained or lost a child" in message_
    assert str(root) in message_
    assert "planner" in message_


def test_a_repository_is_measured_the_way_the_freeze_check_measures_it(tmp_path):
    """A gitignored file is not a difference, and a tracked change is.

    The witness must not be stricter than `unchanged()`, which binds through
    `git write-tree` and therefore honours `.gitignore`. It was, and a
    background process writing bytecode into the repository failed an
    iteration the freeze semantics consider untouched.
    """
    import subprocess

    from hoh.capability import repo_digest

    environment_ = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@local",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@local",
    }
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-qm", "init")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=environment_)

    before = repo_digest(repo)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")
    assert repo_digest(repo) == before, "a gitignored file is not a difference"

    (repo / "app.py").write_text("x = 2\n")
    assert repo_digest(repo) != before, "a tracked change is"


def test_a_directory_that_is_not_a_repository_is_still_digested_whole(tmp_path):
    """An arena has no `.git`, and everything in it counts."""
    from hoh.capability import repo_digest

    d = tmp_path / "arena"
    (d / "__pycache__").mkdir(parents=True)
    (d / "a.py").write_text("x = 1\n")
    before = repo_digest(d)
    (d / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\x00")
    assert repo_digest(d) != before
