"""A project id is a name, never a path: the project store and the storage registry
build a directory from it, and they refuse any id whose directory would not be a direct
child of their `projects/` directory, spelled exactly as the id (tracked follow-up
`projid`, from the run-id fix `i11b`).

The two validators had one hole, and only one: their patterns ended in `$`, which also
matches before a final newline, so `"p1\\n"` was a valid id and named the directory
`projects/p1\\n`. (`.`, `..` and names that begin with `_` or `.` were never accepted: the
first character class is alphanumeric.) Every node below runs in `tmp_path` with nothing
started: no process, no Herdr, no network, no real state root. The registry's boot, pid and
mount readers and its repository probe are replaced by stand-ins.
"""

from __future__ import annotations

import itertools
import os
import re

import pytest

from hoh import cli
from hoh import projectstore
from hoh import storage_coordinator as coordinator
from hoh import storage_registry as sr
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore, list_projects
from hoh.store import StoreError

#: the two patterns of the baseline, spelled out: what the new rules are measured against.
OLD_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OLD_REGISTRY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: names that must never be accepted as a project id, whatever the pattern says
REFUSED = [
    ".", "..", "...", "_x", ".hidden", "-x", "p1\n", "\n", "ab\n", "p1\r", "p1\n\n", "", " p1", "p1 ",
    "a/b", "../x", "./x", "/abs", "a\x00", "x\u00e9", "a" * 65,
]
ACCEPTED = ["p", "p1", "A.b-c_d", "x-1_2", "a.b", "projects", "backups", "0", "a" * 64]


def _tree(root) -> list:
    """Every entry under `root` with what a change would alter."""
    out = []
    for base, dirs, files in os.walk(root):
        for name in sorted(dirs) + sorted(files):
            path = os.path.join(base, name)
            st = os.lstat(path)
            out.append((os.path.relpath(path, root), st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino))
    return sorted(out)


def test_a_project_id_must_name_a_direct_child_of_the_projects_dir(tmp_path):
    """Catches: `"p1\\n"` (and any other name that is not exactly an id) addressing a directory.

    Each refused id raises `UnsafeProjectId` (a `StoreError`, so every caller that catches
    `StoreError` still does) with the old message, and nothing is created: the root has no
    `projects/` afterwards. Each accepted id keeps `dir == root/projects/<id>`. Kills: the
    pattern ends in `$` again; the error is not a `StoreError`; a non-string id is a
    `TypeError` instead of a refusal."""
    root = tmp_path / "root"
    root.mkdir()
    for pid in REFUSED:
        with pytest.raises(StoreError, match="Invalid project id") as seen:
            ProjectStore(root, pid)
        assert isinstance(seen.value, projectstore.UnsafeProjectId), repr(pid)
    for pid in (None, 5, b"p1", ["p1"]):
        with pytest.raises(projectstore.UnsafeProjectId):
            ProjectStore(root, pid)
    assert os.listdir(root) == [], "a refused id must not create anything"
    for pid in ACCEPTED:
        store = ProjectStore(root, pid)
        assert store.project_id == pid
        assert store.dir == root.resolve() / "projects" / pid
        assert store.state_path.parent == store.dir and store.lock_path.parent == store.dir
    assert os.listdir(root) == [], "building a store creates nothing"
    # the id of a directory that the old store made is not addressable any more, and
    # `list_projects` (which only lists) does not hide it
    odd = root / "projects" / "p1\n"
    odd.mkdir(parents=True)
    (odd / "project.json").write_text("{}", encoding="utf-8")
    assert list_projects(root) == ["p1\n"]
    with pytest.raises(projectstore.UnsafeProjectId):
        ProjectStore(root, "p1\n")


def test_the_direct_child_check_does_not_depend_on_the_pattern(tmp_path, monkeypatch):
    """Catches: a later change of the character set (or a pattern that is only `.*`) turning an id
    into a path outside `<root>/projects/`.

    With the pattern widened to anything, `.`, `..`, `a/b`, `../x`, `/abs` and the empty id are
    still refused, with "not a direct child", before anything is read or made (`./x` and `x/`
    spell a child, but not as the id says); `p1` and `a.b` pass. Kills: the constructor does
    not compare the directory with `root/projects/<id>`; it compares the parent only."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(projectstore, "_NAME_RE", re.compile(r".*", re.DOTALL))
    for pid in (".", "..", "a/b", "../x", "/abs", "", "a/..", "./x", "x/", "x//"):
        with pytest.raises(projectstore.UnsafeProjectId, match="not a direct child"):
            ProjectStore(root, pid)
    assert os.listdir(root) == []
    assert ProjectStore(root, "p1").dir == root.resolve() / "projects" / "p1"
    assert ProjectStore(root, "a.b").dir == root.resolve() / "projects" / "a.b"


def test_the_new_rules_refuse_nothing_the_old_patterns_accepted_but_a_final_newline(tmp_path):
    """Catches: a validator that is stricter than the one line it was meant to change, so that a
    project that exists becomes unreachable.

    Over every string of up to four characters from an alphabet that holds the letters, digits,
    the three punctuation characters, a space, a slash, a backslash and the newline (all 11,111
    of them), and over the long and boundary lengths: the project store accepts a string if and
    only if the old pattern did and it does not end in a newline; the registry id rule
    likewise. The real ids measured read-only on 2026-10-10 (the registry's project
    `veriharness`, its two session ids, `p`, `a`) are in the corpus. Kills: any change of the
    character class or of the length bound."""
    alphabet = ["a", "Z", "0", ".", "_", "-", " ", "/", "\\", "\n"]
    corpus = {"".join(t) for n in range(0, 5) for t in itertools.product(alphabet, repeat=n)}
    corpus |= {"veriharness", "p", "a", "A.b-c_d", "s-1.x", "a" * 63, "a" * 64, "a" * 65, "a" * 63 + "\n",
               "a" * 64 + "\n", "0" + "." * 63, "9" * 64}
    assert len(corpus) > 4000
    for s in sorted(corpus):
        want_project = bool(OLD_PROJECT.match(s)) and not s.endswith("\n")
        assert bool(projectstore._NAME_RE.match(s)) is want_project, repr(s)
        want_registry = bool(OLD_REGISTRY.match(s)) and not s.endswith("\n")
        assert bool(sr._ID.match(s)) is want_registry, repr(s)
    for s in ("veriharness", "p", "a", "A.b-c_d"):
        assert s in corpus


def test_hoh_project_answers_an_id_the_store_refuses_with_a_refusal_not_a_traceback(tmp_path, capsys):
    """Catches: `hoh project status ..` raising out of `main()` (a traceback), and `hoh project
    status "p1\\n"` answering "has no state" as if `p1\\n` were a project.

    For `status`, `resume`, `authority`, `unblock` and `record-action` each of `..`, `.`, `p1\\n`,
    `_x`, `a/b`: exit 2, nothing on stdout, exactly one line on stderr that starts `refused:
    Invalid project id`, and the tree (root, the project `p` that exists) byte-for-byte
    unchanged. A project that exists still answers (`status p` is 0), and an id the store
    accepts that has no state still says so ("has no state", exit 2). Kills: the CLI does not
    catch the refusal; it returns 1; it catches `StoreError` for the whole command."""
    root = tmp_path / "root"
    root.mkdir()
    ProjectStore(root, "p").create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="merge conflict")]))

    def run(*argv):
        code = cli.main(["--root", str(root), "project", *argv])
        out, err = capsys.readouterr()
        return code, out, err

    code, out, err = run("status", "p")
    assert code == 0 and "p" in out
    before = _tree(tmp_path)
    for pid in ("..", ".", "p1\n", "_x", "a/b"):
        for argv in (("status", pid), ("resume", pid), ("authority", pid),
                     ("unblock", pid, "a", "--reason", "x"),
                     ("record-action", pid, "--actor", "human", "--reason", "x")):
            code, out, err = run(*argv)
            assert code == 2, (argv, code, err)
            assert out == "", (argv, out)
            lines = err.splitlines()
            assert len(lines) == 1 and lines[0].startswith("refused: Invalid project id"), (argv, err)
            assert _tree(tmp_path) == before, argv
    code, out, err = run("status", "q")
    assert code == 2 and "has no state" in err and "refused" not in err
    assert _tree(tmp_path) == before


def _registry(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    registry = sr.StorageRegistry(
        state, forbidden_roots=[tmp_path / "home"], boot_id_reader=lambda: "boot-1",
        pid_start_reader=lambda pid: 5000, pid_exists=lambda pid: True,
        mount_check=lambda path: False, mount_id_reader=lambda path: 1,
        st_dev_reader=lambda path: os.stat(path).st_dev)
    registry._repo_identity = lambda root: None        # no git process: a plain directory
    registry.init()
    return registry


def test_the_registry_id_rule_refuses_a_trailing_newline_for_projects_and_sessions(tmp_path):
    """Catches: the storage registry (`<state>/projects/<p>/...`, `.../sessions/<s>/`) building a
    directory from `"p1\\n"`, and treating a hand-made directory of that name as a registered
    project.

    `_check_id` (project and session), `register_project` and the coordinator's own project check
    refuse an id that ends in a newline; the refusal creates nothing under `projects/`; a
    directory `projects/q1\\n` that carries a well-formed `PROJECT.json` is not a registered
    project (`_project_record` is None, `_registered_projects` does not list it); the ids that
    were valid still are. Kills: the registry pattern ends in `$` again."""
    registry = _registry(tmp_path)
    plain = tmp_path / "proj"
    plain.mkdir()
    for bad in ("p1\n", "\n", "a\x00", ".", "..", "_x", "Veri", "", "a" * 65, "a/b", None, 5):
        with pytest.raises(sr.StorageOwnershipError, match="invalid project id"):
            sr._check_id(bad, "project id")
    with pytest.raises(sr.StorageOwnershipError, match="invalid session id"):
        sr._check_id("s1\n", "session id")
    with pytest.raises(coordinator.StorageCoordinatorError):
        coordinator._check_project_id("p1\n")
    for good in ("veriharness", "p1", "a.b-c_d", "0", "a" * 64):
        assert sr._check_id(good, "project id") == good
    before = sorted(os.listdir(registry._projects))
    with pytest.raises(sr.StorageOwnershipError, match="invalid project id"):
        registry.register_project("p1\n", plain)
    assert sorted(os.listdir(registry._projects)) == before, "a refused id creates no directory"
    record = registry.register_project("p1", plain)
    assert record["project_id"] == "p1"
    # a directory the old registry could have made, with a record that names itself
    odd = registry._projects / "q1\n"
    odd.mkdir()
    (odd / "PROJECT.json").write_text(
        (registry._projects / "p1" / "PROJECT.json").read_text(encoding="utf-8").replace(
            '"p1"', '"q1\\n"'), encoding="utf-8")
    assert registry._project_record("q1\n") is None
    assert registry._registered_projects() == ["p1"]
    assert registry._project_record("p1")["project_id"] == "p1"
