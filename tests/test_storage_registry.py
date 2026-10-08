"""The storage ownership registry (STORAGE-A, P2-38): `UNKNOWN` fails closed.

Every test creates everything under pytest's `tmp_path`: `HOME` is pointed
there, `forbidden_roots` is passed explicitly to every registry, git runs with
`GIT_CONFIG_NOSYSTEM=1` and a fixed identity, and the boot id, the pid start
times, pid existence, the mount check, mount ids and `st_dev` are **injected**
(`FakeOS`) -- no test depends on a real process, boot or mount.

Each test names the failure it catches and the mutant of
`hoh.storage_registry` it kills, and carries a negative control that changes
only the condition under test.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from hoh import storage_registry as sr
from hoh.contracts import RunState
from hoh.storage_registry import (
    OWNED,
    PROTECTED,
    UNKNOWN,
    SessionLease,
    StorageOwnershipError,
    StorageRegistry,
)

LEGACY_SCHEMA = "veriharness.storage-legacy-roots/1"
ANCHOR_FILE = "migration/legacy-roots.json"
BOOT = "boot-1"


class FakeOS:
    """Injected OS readers: a process table `{pid: start time}`, a boot id, a
    set of paths that count as mount points, a mount table `{mount point:
    mount id}` (a path lies on the mount of its nearest listed ancestor, else
    on mount 1; a value of None is an unreadable mount) and `st_dev`
    overrides (else the real `os.stat`)."""

    def __init__(self) -> None:
        self.procs: dict[int, int] = {100: 5000, 200: 6000, 300: 7000}
        self.boot = BOOT
        self.mounts: set[Path] = set()
        self.mount_ids: dict[Path, int | None] = {}
        self.st_devs: dict[Path, int] = {}

    def boot_id(self) -> str:
        return self.boot

    def pid_start(self, pid: int) -> int | None:
        return self.procs.get(pid)

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs

    def is_mount(self, path: Path) -> bool:
        return Path(path) in self.mounts

    def mount_id(self, path: Path) -> int | None:
        path = Path(path)
        for candidate in (path, *path.parents):
            if candidate in self.mount_ids:
                return self.mount_ids[candidate]
        return 1

    def st_dev(self, path: Path) -> int:
        return self.st_devs.get(Path(path), os.stat(path).st_dev)


@pytest.fixture
def fake() -> FakeOS:
    return FakeOS()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """HOME and git configuration confined to `tmp_path`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Registry Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "registry@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Registry Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "registry@example.invalid")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        HOME=os.environ["HOME"],
        GIT_AUTHOR_NAME="Registry Test",
        GIT_AUTHOR_EMAIL="registry@example.invalid",
        GIT_COMMITTER_NAME="Registry Test",
        GIT_COMMITTER_EMAIL="registry@example.invalid",
    )
    return env


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env(),
        check=True,
    )
    return out.stdout.strip()


def git_rc(repo: Path, *args: str) -> int:
    """The exit code of a plain git call (the test's own environment): what a
    git that does *not* run with the registry's fixed environment answers."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env(),
        check=False,
    ).returncode


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "README").write_text("project\n", encoding="utf-8")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit_anchor(repo: Path, record: object, *, name: str = ANCHOR_FILE) -> dict[str, str]:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2), encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", "legacy roots")
    return {"commit": git(repo, "rev-parse", "HEAD"), "file": name}


def legacy(*roots: tuple[Path, str], project: str | None = "a") -> dict:
    """A legacy-roots migration record; every entry names `project` (None:
    the entry names no project)."""
    entry = {} if project is None else {"project_id": project}
    return {"schema": LEGACY_SCHEMA,
            "roots": [{"path": str(p), "kind": k, **entry} for p, k in roots]}


def run_state(run_id: str) -> str:
    return RunState(
        run_id=run_id, repo_path="/repo", project_name="demo", spec_path="spec.md",
        spec_digest="s" * 8, policy_digest="p" * 8, profile_digest="f" * 8,
    ).model_dump_json()


def make_runs_base(path: Path, *run_ids: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for run_id in run_ids:
        (path / run_id).mkdir()
        (path / run_id / "state.json").write_text(run_state(run_id), encoding="utf-8")
    return path


def new_registry(tmp_path: Path, fake: FakeOS, *, state: Path | None = None,
                 forbidden: list[Path] | None = None) -> StorageRegistry:
    state = state or tmp_path / "vh" / "state"
    state.parent.mkdir(parents=True, exist_ok=True)
    return StorageRegistry(
        state,
        forbidden_roots=forbidden if forbidden is not None else [tmp_path / "home" / "user"],
        boot_id_reader=fake.boot_id,
        pid_start_reader=fake.pid_start,
        pid_exists=fake.pid_exists,
        mount_check=fake.is_mount,
        mount_id_reader=fake.mount_id,
        st_dev_reader=fake.st_dev,
    )


@pytest.fixture
def reg(tmp_path, fake) -> StorageRegistry:
    """An initialised registry with project `a` (repo `proj-a`)."""
    registry = new_registry(tmp_path, fake)
    registry.init()
    registry.register_project("a", make_repo(tmp_path / "proj-a"))
    return registry


def scratch(reg: StorageRegistry, project: str = "a", session: str = "s1",
            kind: str = "scratch", pid: int = 100) -> Path:
    reg.open_session(project, session, pid=pid, boot_id=BOOT)
    return reg.scratch_dir(project, session, kind)


def owned_file(directory: Path, name: str = "data.bin") -> Path:
    path = directory / name
    path.write_bytes(b"x")
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, record: object) -> None:
    path.write_text(json.dumps(record), encoding="utf-8")


def index_scratch(reg: StorageRegistry, project: str, kind: str, session: str) -> None:
    """Test-only: add a scratch dir to SCRATCH_INDEX.json as the registry would."""
    index_path = reg.state_root / "projects" / project / "SCRATCH_INDEX.json"
    index = read_json(index_path)
    directory = reg.state_root / "projects" / project / kind / session
    st = os.lstat(directory)
    index["entries"].append({
        "path": f"projects/{project}/{kind}/{session}", "project_id": project, "kind": kind,
        "session_id": session, "created_utc": "x", "st_dev": st.st_dev, "st_ino": st.st_ino,
        "mount_id": reg._mount_id_reader(directory),
    })
    write_json(index_path, index)


CONFIG_SCHEMA = "veriharness.storage-config/1"


def write_config(reg: StorageRegistry, record: object) -> None:
    """Write `<state_root>/CONFIG.json` directly, as the operator edits it by
    hand (Rev. 9 item 4): a temp file in the same directory, then
    `os.replace`. The registry has no method that writes it."""
    path = reg.state_root / "CONFIG.json"
    tmp = reg.state_root / ".CONFIG.json.tmp.operator"
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def admit(reg: StorageRegistry, *parents: Path) -> None:
    """Add `parents` to the admissible parents for `create_external_root` in
    `CONFIG.json` (Rev. 7 item 1, Rev. 9 item 4) -- the operator act every
    creation test needs, written directly with a growing `sequence`."""
    path = reg.state_root / "CONFIG.json"
    config = read_json(path) if path.exists() else {
        "schema": CONFIG_SCHEMA, "sequence": 0, "admissible_external_parents": []}
    for parent in parents:
        if str(parent) not in config["admissible_external_parents"]:
            config["admissible_external_parents"].append(str(parent))
    config["sequence"] += 1
    write_config(reg, config)


def forge_scratch(reg: StorageRegistry, project: str, kind: str, session: str,
                  record: dict) -> Path:
    directory = reg.state_root / "projects" / project / kind / session
    directory.mkdir(parents=True)
    write_json(directory / ".vh-owner.json", record)
    return directory


# --------------------------------------------------------------------------- #
# K1 -- UNKNOWN fails closed
# --------------------------------------------------------------------------- #


def test_unknown_nonexistent_path(reg):
    """Catches: a path that does not exist reported as owned.

    Kills the mutant `resolve(strict=False)` in `owner_of`: non-strict
    resolution folds `ghost/../data.bin` into the existing `data.bin`, so the
    mutant reports a path the kernel cannot open as OWNED. Negative control:
    the same shape through an existing `sub/` is OWNED, and so is the plain
    file once it exists.
    """
    directory = scratch(reg)
    ghost = directory / "never-created"
    assert reg.owner_of(ghost).status == UNKNOWN
    owned_file(directory)
    assert reg.owner_of(directory / "ghost" / ".." / "data.bin").status == UNKNOWN
    # negative control: only existence changes
    (directory / "sub").mkdir()
    assert reg.owner_of(directory / "sub" / ".." / "data.bin").status == OWNED
    ghost.write_bytes(b"now")
    assert reg.owner_of(ghost).status == OWNED


def test_unknown_unindexed_record(reg):
    """Catches: a well-formed `.vh-owner.json` in the right place accepted
    without its `SCRATCH_INDEX.json` entry.

    Kills the mutant that skips the index lookup. Negative control: adding
    exactly the index entry (and nothing else) makes the same path OWNED.
    """
    real = scratch(reg)
    record = read_json(real / ".vh-owner.json")
    record["session_id"] = "s2"
    forged = forge_scratch(reg, "a", "scratch", "s2", record)
    target = owned_file(forged)
    assert reg.owner_of(target).status == UNKNOWN
    assert reg.owner_of(forged).status == UNKNOWN
    # negative control: only the index entry changes
    index_scratch(reg, "a", "scratch", "s2")
    assert reg.owner_of(target).status == OWNED


def test_unknown_record_in_wrong_place(reg, tmp_path):
    """Catches: a record honoured outside
    `<state_root>/projects/<p>/<kind>/<s>/.vh-owner.json`, or one whose fields
    name another project, kind or session than its path.

    Kills the mutants that accept any ancestor `.vh-owner.json` and that skip
    the field-equality check. Negative control: the indexed directory with a
    record whose fields match its path is OWNED.
    """
    real = scratch(reg)
    good = read_json(real / ".vh-owner.json")

    foreign = tmp_path / "foreign" / "deep"
    foreign.mkdir(parents=True)
    write_json(foreign.parent / ".vh-owner.json", good)
    write_json(foreign / ".vh-owner.json", good)
    assert reg.owner_of(owned_file(foreign)).status == UNKNOWN

    nested = real / "nested"
    nested.mkdir()
    write_json(nested / ".vh-owner.json", dict(good, session_id="nested"))
    assert reg.owner_of(owned_file(nested)).status == OWNED      # owned via s1, not its own

    for session, fields in (
        ("s3", {"project_id": "b"}),
        ("s4", {"session_id": "s1"}),
        ("s5", {"kind": "reviews"}),
    ):
        directory = forge_scratch(reg, "a", "scratch", session,
                                  {**good, "session_id": session, **fields})
        index_scratch(reg, "a", "scratch", session)
        assert reg.owner_of(owned_file(directory)).status == UNKNOWN, session

    # negative control: matching fields, indexed, same place shape
    control = forge_scratch(reg, "a", "scratch", "s6", dict(good, session_id="s6"))
    index_scratch(reg, "a", "scratch", "s6")
    assert reg.owner_of(owned_file(control)).status == OWNED


# --------------------------------------------------------------------------- #
# K2 -- foreign paths are never owned next to owned ones
# --------------------------------------------------------------------------- #


def test_foreign_sibling_and_parent_unknown(reg, tmp_path):
    """Catches: the containment test inverted (root inside path) or an
    ancestor accepted, so a parent or sibling of an owned root reads OWNED.

    Kills the mutant that checks `root.is_relative_to(path)` or accepts
    ancestors. Negative control: a file inside the same root is OWNED.
    """
    base = tmp_path / "ext"
    base.mkdir()
    admit(reg, base)
    root = base / "root"
    reg.create_external_root("a", root, "target_clone")
    inside = owned_file(root)
    sibling = base / "sibling"
    sibling.mkdir()
    sibling_file = owned_file(base, "next-to-root.bin")

    assert reg.owner_of(inside).status == OWNED          # negative control
    assert reg.owner_of(sibling).status == UNKNOWN
    assert reg.owner_of(owned_file(sibling)).status == UNKNOWN
    assert reg.owner_of(sibling_file).status == UNKNOWN
    assert reg.owner_of(base).status == UNKNOWN
    assert reg.owner_of(tmp_path).status == UNKNOWN

    directory = scratch(reg)
    kind_sibling = reg.state_root / "projects" / "a" / "scratch" / "loose"
    kind_sibling.mkdir()
    assert reg.owner_of(owned_file(directory)).status == OWNED   # negative control
    assert reg.owner_of(owned_file(kind_sibling)).status == UNKNOWN


def test_foreign_prefix_sibling_unknown(reg, tmp_path):
    """Catches: containment decided by string prefix, so `<root>-x` and
    `<root>x` count as inside `<root>`.

    Kills the mutant that uses `str.startswith` instead of
    `Path.is_relative_to`. Negative control: `<root>/f` is OWNED.
    """
    root = tmp_path / "ext" / "root"
    root.parent.mkdir()
    admit(reg, root.parent)
    reg.create_external_root("a", root, "target_clone")
    for name in ("root-x", "rootx", "root.v1"):
        (root.parent / name).mkdir()
        assert reg.owner_of(owned_file(root.parent / name)).status == UNKNOWN, name
    assert reg.owner_of(owned_file(root)).status == OWNED        # negative control


def test_foreign_symlink_out_of_owned_unknown(reg, tmp_path):
    """Catches: a symlink inside owned scratch pointing to a foreign directory
    judged by the name it was given.

    Kills the mutant that judges only the lexical path. Negative control: a
    symlink inside the same scratch dir pointing within it is OWNED.
    """
    directory = scratch(reg)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_file = owned_file(foreign)
    (directory / "out").symlink_to(foreign, target_is_directory=True)
    (directory / "out-file").symlink_to(foreign_file)

    assert reg.owner_of(directory / "out").status == UNKNOWN
    assert reg.owner_of(directory / "out" / "data.bin").status == UNKNOWN
    assert reg.owner_of(directory / "out-file").status == UNKNOWN
    assert reg.owner_of(foreign_file).status == UNKNOWN

    # negative control: same shape, target inside the same owned root
    (directory / "real").mkdir()
    owned_file(directory / "real")
    (directory / "in").symlink_to(directory / "real", target_is_directory=True)
    assert reg.owner_of(directory / "in" / "data.bin").status == OWNED


def test_foreign_symlink_into_owned_unknown(reg, tmp_path):
    """Catches: a symlink outside every owned root pointing into owned scratch
    judged by where it resolves to alone.

    Kills the mutant that judges only the resolved path. Covers a link to the
    scratch dir, a link to a file in it, and a symlinked ancestor component
    (an alias of the state root). Negative control: the same file by its own
    name is OWNED.
    """
    directory = scratch(reg)
    target = owned_file(directory)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link").symlink_to(directory, target_is_directory=True)
    (outside / "file-link").symlink_to(target)
    (tmp_path / "alias").symlink_to(reg.state_root, target_is_directory=True)

    assert reg.owner_of(outside / "link" / "data.bin").status == UNKNOWN
    assert reg.owner_of(outside / "file-link").status == UNKNOWN
    alias = tmp_path / "alias" / "projects" / "a" / "scratch" / "s1" / "data.bin"
    assert reg.owner_of(alias).status == UNKNOWN
    assert reg.owner_of(target).status == OWNED                   # negative control


# --------------------------------------------------------------------------- #
# K3 -- external roots are proven, not asserted
# --------------------------------------------------------------------------- #


def test_foreign_external_root_ancestor_refused(tmp_path, fake):
    """Catches: an ancestor of the state root, a forbidden root, an ancestor of
    a forbidden root, or the filesystem root registered as an external root.

    Kills the mutant that removes the ancestor-of-state-root / forbidden-root
    check: every refused candidate here has a valid committed anchor naming
    project `a` and a valid run structure, so only that rule refuses it, and
    the refusal's reason is asserted. Negative control: the same structure at
    a neutral path, and below a forbidden root, registers.
    """
    state = tmp_path / "base" / "state"
    home = tmp_path / "fb" / "home"
    reg = new_registry(tmp_path, fake, state=state, forbidden=[home])
    reg.init()
    repo = make_repo(tmp_path / "proj-a")
    reg.register_project("a", repo)

    make_runs_base(tmp_path / "base", "r1")
    make_runs_base(home, "r2")
    make_runs_base(home / "below", "r3")
    make_runs_base(tmp_path / "neutral", "r4")
    candidates = [tmp_path / "base", tmp_path / "fb", home]
    anchor = commit_anchor(repo, legacy(*[(p, "runs_base") for p in candidates + [
        tmp_path / "neutral", home / "below"]]))

    reasons = ["the state root, an ancestor of it", "forbidden root", "forbidden root"]
    for candidate, reason in zip(candidates, reasons):
        with pytest.raises(StorageOwnershipError, match=reason):
            reg.register_external_root("a", candidate, "runs_base", anchor)
    with pytest.raises(StorageOwnershipError, match="filesystem root"):
        reg.register_external_root("a", Path(tmp_path.anchor), "runs_base", anchor)
    with pytest.raises(StorageOwnershipError, match="or inside it"):
        reg.create_external_root("a", state / "projects" / "inner", "target_clone")

    # negative control: same structure, only the position changes
    assert reg.register_external_root("a", tmp_path / "neutral", "runs_base",
                                      anchor)["state"] == "REGISTERED"
    assert reg.register_external_root("a", home / "below", "runs_base",
                                      anchor)["state"] == "REGISTERED"


def test_foreign_external_root_overlap_refused(tmp_path, fake):
    """Catches: project B registering a root that is an ancestor or a
    descendant of a root project A already holds.

    Kills the mutant that removes the cross-project overlap check or limits it
    to the caller's own project. Negative control: B creates a disjoint root.
    """
    reg = new_registry(tmp_path, fake)
    reg.init()
    reg.register_project("a", make_repo(tmp_path / "proj-a"))
    repo_b = make_repo(tmp_path / "proj-b")
    reg.register_project("b", repo_b)

    root_a = tmp_path / "ext" / "root"
    root_a.parent.mkdir()
    (tmp_path / "ext2").mkdir()
    admit(reg, root_a.parent, tmp_path / "ext2")
    reg.create_external_root("a", root_a, "target_clone")
    admit(reg, root_a)               # so only the overlap rule refuses `root_a/inner`
    make_runs_base(tmp_path / "ext", "r1")
    anchor = commit_anchor(repo_b, legacy((tmp_path / "ext", "runs_base"), project="b"))

    with pytest.raises(StorageOwnershipError, match="overlaps"):
        reg.register_external_root("b", tmp_path / "ext", "runs_base", anchor)   # ancestor
    with pytest.raises(StorageOwnershipError, match="overlaps"):
        reg.create_external_root("b", root_a / "inner", "target_clone")         # descendant
    with pytest.raises(StorageOwnershipError, match="overlaps"):
        reg.create_external_root("a", root_a / "inner", "target_clone")         # own, too
    assert not (root_a / "inner").exists()

    # negative control: a disjoint path
    assert reg.create_external_root("b", tmp_path / "ext2" / "r", "target_clone")[
        "state"] == "CREATED"


def test_external_root_proof_foreign_selfclaim_refused(reg, tmp_path):
    """Catches: a foreign directory that claims itself -- its own valid
    `state.json` with a matching `run_id`, its own committed legacy-roots
    record, and a caller-supplied anchor naming it -- accepted.

    Kills the mutant that skips anchor verification and trusts the candidate's
    content. The self-claimed record names project `a` and is otherwise the
    good record; only where it is committed differs, and the reason (not on
    the project's HEAD line) is asserted. Negative control: the same record
    committed in the registered project's repository registers the same
    directory.
    """
    foreign = make_runs_base(tmp_path / "foreign-runs", "r1")
    record = legacy((foreign, "runs_base"))
    git(foreign, "init", "-q")
    self_anchor = commit_anchor(foreign, record)

    with pytest.raises(StorageOwnershipError, match="is not an ancestor of HEAD"):
        reg.register_external_root("a", foreign, "runs_base", self_anchor)
    assert reg.owner_of(foreign / "r1" / "state.json").status == UNKNOWN
    with pytest.raises(StorageOwnershipError, match="is not a repository path"):
        reg.register_external_root("a", foreign, "runs_base",
                                   {"commit": self_anchor["commit"], "file": "../x.json"})

    # negative control: the anchor lives in the registered project repository
    repo = tmp_path / "proj-a"
    anchor = commit_anchor(repo, record)
    reg.register_external_root("a", foreign, "runs_base", anchor)
    assert reg.owner_of(foreign / "r1" / "state.json").status == OWNED


def test_external_root_anchor_must_be_committed(reg, tmp_path):
    """Catches: an anchor read from the working tree, or from a commit that is
    not an ancestor of the project's HEAD.

    Kills the mutants that read the file from disk instead of `git cat-file`
    and that skip `merge-base --is-ancestor`. The record is the good one
    (naming project `a`); only where it lives differs, and each refusal's
    reason is asserted. Negative control: the same file committed on HEAD's
    line registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    record = legacy((runs, "runs_base"))

    head = git(repo, "rev-parse", "HEAD")
    (repo / "migration").mkdir()
    write_json(repo / ANCHOR_FILE, record)                         # uncommitted
    with pytest.raises(StorageOwnershipError, match="is not a committed file"):
        reg.register_external_root("a", runs, "runs_base", {"commit": head, "file": ANCHOR_FILE})

    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_anchor(repo, record)
    git(repo, "checkout", "-q", branch)
    assert not (repo / ANCHOR_FILE).exists() or read_json(repo / ANCHOR_FILE) == record
    with pytest.raises(StorageOwnershipError, match="is not an ancestor of HEAD"):
        reg.register_external_root("a", runs, "runs_base", side)    # not an ancestor
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    # negative control: committed on HEAD's line
    git(repo, "merge", "-q", "--ff-only", "side")
    reg.register_external_root("a", runs, "runs_base", side)
    assert reg.owner_of(runs / "r1" / "state.json").status == OWNED


SCHEMA_REASON = re.escape(f"does not carry schema {LEGACY_SCHEMA}")
PATH_REASON = "lists no root with path"
KIND_REASON = "only with kind"
PROJECT_REASON = "for another project"


def refused_with(reg: StorageRegistry, path: Path, kind: str, anchor: dict,
                 reason: str) -> str:
    """Register `path` for project `a` and assert the refusal's reason --
    and that it is not the project-id refusal, which would mask the rule
    under test (Rev. 9 item 1)."""
    with pytest.raises(StorageOwnershipError, match=reason) as info:
        reg.register_external_root("a", path, kind, anchor)
    assert PROJECT_REASON not in str(info.value), info.value
    return str(info.value)


def one_change(good: dict, change) -> dict:
    """A deep copy of `good` with `change` applied to it (one property)."""
    record = copy.deepcopy(good)
    change(record)
    return record


def test_external_root_anchor_wrong_schema_refused(reg, tmp_path):
    """Catches: an anchor accepted because the path appears *somewhere* in the
    committed JSON rather than as a `roots[]` entry of schema
    `veriharness.storage-legacy-roots/1` with the matching kind.

    Every bad record is the good record (`legacy((runs, 'runs_base'))`, whose
    entry names project `a`) with exactly one property changed (Rev. 9 item
    1): schema `/0`, schema missing, the path only in a `note` field, kind
    `target_clone`, the entry key `root` instead of `path`, the top-level
    `roots` renamed. So the project-id check cannot refuse them first, and
    each refusal's reason is asserted. Kills the mutants that drop the schema
    comparison, the path match or the kind match in `_verify_anchor` (see also
    `test_mutant_anchor_*`), and the one that replaces the field check with
    "any string value equals the path". Negative control: the good record
    itself registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    good = legacy((runs, "runs_base"))
    assert good["roots"][0]["project_id"] == "a"

    def to_note(r: dict) -> None:
        r["note"] = r["roots"][0].pop("path")

    def key_root(r: dict) -> None:
        r["roots"][0]["root"] = r["roots"][0].pop("path")

    bad_records = [
        (one_change(good, lambda r: r.update(schema=LEGACY_SCHEMA[:-1] + "0")), SCHEMA_REASON),
        (one_change(good, lambda r: r.pop("schema")), SCHEMA_REASON),
        (one_change(good, to_note), PATH_REASON),
        (one_change(good, lambda r: r["roots"][0].update(kind="target_clone")), KIND_REASON),
        (one_change(good, key_root), PATH_REASON),
        (one_change(good, lambda r: r.update(entries=r.pop("roots"))), PATH_REASON),
    ]
    for number, (bad, reason) in enumerate(bad_records):
        anchor = commit_anchor(repo, bad, name=f"migration/bad-{number}.json")
        refused_with(reg, runs, "runs_base", anchor, reason)
        assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN
    anchor = commit_anchor(repo, good)                               # negative control
    assert reg.register_external_root("a", runs, "runs_base", anchor)["state"] == "REGISTERED"


def test_external_root_mount_refused(reg, tmp_path, fake):
    """Catches: a mount point registered or created as an external root.

    A real mount cannot be made under `tmp_path`, so the mount check is
    **injected** (`FakeOS.is_mount`, passed as `mount_check`); the test marks
    the candidate as a mount there. Kills the mutant that removes the mount
    check. The anchor is the good record naming project `a`; the refusal's
    reason is asserted. Negative control: the same calls with the path not
    marked.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    anchor = commit_anchor(repo, legacy((runs, "runs_base")))
    admit(reg, tmp_path)
    fake.mounts.add(runs)
    refused_with(reg, runs, "runs_base", anchor, "is a mount point")
    created = tmp_path / "mnt-created"
    fake.mounts.add(created)
    with pytest.raises(StorageOwnershipError, match="is a mount point"):
        reg.create_external_root("a", created, "target_clone")
    assert reg.owner_of(owned_file(created)).status == UNKNOWN

    # negative control: not a mount
    fake.mounts.clear()
    reg.register_external_root("a", runs, "runs_base", anchor)
    assert reg.owner_of(runs / "r1" / "state.json").status == OWNED
    other = tmp_path / "plain-created"
    reg.create_external_root("a", other, "target_clone")
    assert reg.owner_of(owned_file(other)).status == OWNED


def test_created_root_owned(reg, tmp_path):
    """Catches: a root the registry created itself never becoming OWNED below
    it (the positive path the fail-closed tests are controlled against).

    Kills the mutant that never sets `CREATED`. The root itself is PROTECTED,
    its contents are OWNED, and the index records the inode.
    """
    root = tmp_path / "created"
    admit(reg, tmp_path)
    entry = reg.create_external_root("a", root, "target_clone")
    assert entry["state"] == "CREATED"
    st = os.lstat(root)
    assert (entry["st_dev"], entry["st_ino"]) == (st.st_dev, st.st_ino)
    index = read_json(reg.state_root / "projects" / "a" / "ROOT_INDEX.json")
    assert [e["state"] for e in index["entries"]] == ["CREATED"]
    result = reg.owner_of(owned_file(root))
    assert (result.status, result.project_id, result.kind, result.root) == (
        OWNED, "a", "target_clone", root)
    assert result.st_ino == os.stat(root / "data.bin").st_ino
    assert str(reg.state_root / "projects" / "a" / "ROOT_INDEX.json") in result.proof
    assert reg.owner_of(root).status == PROTECTED


def test_reserved_root_created_by_other_unknown(reg, tmp_path):
    """Catches: a path somebody else created before the registry's `mkdir`
    adopted as a created root.

    Kills the mutant `mkdir(exist_ok=True)` / pre-existing path adopted.
    Negative control: a fresh path is created and OWNED.
    """
    pre = tmp_path / "pre"
    pre.mkdir()
    victim = owned_file(pre)
    admit(reg, tmp_path)
    with pytest.raises(StorageOwnershipError):
        reg.create_external_root("a", pre, "target_clone")
    assert reg.owner_of(victim).status == UNKNOWN
    assert reg.owner_of(pre).status == UNKNOWN

    fresh = tmp_path / "fresh"
    reg.create_external_root("a", fresh, "target_clone")             # negative control
    assert reg.owner_of(owned_file(fresh)).status == OWNED


def test_created_root_replaced_inode_unknown(reg, tmp_path):
    """Catches: a created root renamed away and replaced by another directory
    at the same path still counted as owned.

    Kills the mutant that drops the `st_dev`/`st_ino` re-check. The original is
    renamed aside (never removed). Negative control: before the swap the same
    path is OWNED.
    """
    root = tmp_path / "created"
    admit(reg, tmp_path)
    reg.create_external_root("a", root, "target_clone")
    assert reg.owner_of(owned_file(root)).status == OWNED            # negative control
    aside = tmp_path / "created.v1.20261001T000000Z"
    os.rename(root, aside)
    root.mkdir()
    assert reg.owner_of(owned_file(root)).status == UNKNOWN
    assert reg.owner_of(root).status == UNKNOWN
    assert reg.owner_of(aside / "data.bin").status == UNKNOWN


# --------------------------------------------------------------------------- #
# K4 -- isolation and leases
# --------------------------------------------------------------------------- #


def test_isolation_project_a_never_b(tmp_path, fake):
    """Catches: project A's scratch reported as project B, and B claiming A's
    project root or a root overlapping A's.

    Kills the mutant whose owner lookup returns the first registered project
    (B is registered first, A's id sorts first: either constant answer fails),
    and the one that drops the cross-project overlap check. Negative control:
    B's own paths are B's, and B can register a disjoint root.
    """
    reg = new_registry(tmp_path, fake)
    reg.init()
    repo_a = make_repo(tmp_path / "proj-a")
    reg.register_project("b", make_repo(tmp_path / "proj-b"))
    reg.register_project("a", repo_a)
    file_a = owned_file(scratch(reg, "a", "s1", pid=100))
    file_b = owned_file(scratch(reg, "b", "s1", pid=200))
    assert reg.owner_of(file_a).project_id == "a"
    assert reg.owner_of(file_b).project_id == "b"

    root_a = tmp_path / "root-a"
    admit(reg, tmp_path)
    reg.create_external_root("a", root_a, "target_clone")
    assert reg.owner_of(owned_file(root_a)).project_id == "a"

    with pytest.raises(StorageOwnershipError):
        reg.register_project("b2", repo_a)
    with pytest.raises(StorageOwnershipError):
        reg.register_project("b", repo_a)
    with pytest.raises(StorageOwnershipError):
        reg.create_external_root("b", root_a / "nested", "target_clone")
    assert reg.owner_of(file_a).project_id == "a"

    root_b = tmp_path / "root-b"                                     # negative control
    reg.create_external_root("b", root_b, "target_clone")
    assert reg.owner_of(owned_file(root_b)).project_id == "b"


def test_isolation_id_regex(reg, tmp_path):
    """Catches: ids that escape their directory (`..`, `/`, upper case, too
    long) accepted as project or session ids. Negative control: valid ids."""
    root = tmp_path / "proj-a"
    for bad in ("..", "a/b", "A", "-a", "", "a" * 65, ".hidden"):
        with pytest.raises(StorageOwnershipError):
            reg.register_project(bad, root)
        with pytest.raises(StorageOwnershipError):
            reg.open_session("a", bad, pid=100, boot_id=BOOT)
    assert reg.register_project("a", root)["project_id"] == "a"       # idempotent
    reg.register_project("a.b-c_9", make_repo(tmp_path / "proj-x"))
    reg.open_session("a", "s" * 64, pid=100, boot_id=BOOT)


def test_isolation_register_project_idempotent(reg, tmp_path):
    """Catches: re-registering an id with another root silently rebinding it.
    Negative control: the same id with the same root returns the same record."""
    first = read_json(reg.state_root / "projects" / "a" / "PROJECT.json")
    again = reg.register_project("a", tmp_path / "proj-a")
    assert again == first
    assert first["archive_namespace"] == "a"
    assert first["project_root"] == str((tmp_path / "proj-a").resolve())
    with pytest.raises(StorageOwnershipError):
        reg.register_project("a", make_repo(tmp_path / "proj-other"))
    assert read_json(reg.state_root / "projects" / "a" / "PROJECT.json") == first


def test_lease_live_blocks_other_session(reg, fake):
    """Catches: a live lease of pid X superseded by another pid, and a lease
    closed by a different session.

    Kills the mutant that skips the liveness check (always supersedes).
    Negative control: once pid X is gone, the same `open_session` supersedes
    it and keeps the old record as `LEASE.json.v1.<UTC>`.
    """
    lease = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    assert reg.lease_is_live(lease)
    path = reg.state_root / "projects" / "a" / "sessions" / "s1" / "LEASE.json"
    before = path.read_bytes()
    with pytest.raises(StorageOwnershipError):
        reg.open_session("a", "s1", pid=200, boot_id=BOOT)
    assert path.read_bytes() == before
    assert reg.open_session("a", "s1", pid=100, boot_id=BOOT) == lease   # same process

    other = reg.open_session("a", "s2", pid=200, boot_id=BOOT)
    with pytest.raises(StorageOwnershipError):
        reg.close_session("a", "s1", other)
    forged = SessionLease(**{**other.__dict__, "session_id": "s1"})
    with pytest.raises(StorageOwnershipError):
        reg.close_session("a", "s1", forged)
    assert path.read_bytes() == before

    # negative control: pid 100 gone -> superseded, old record kept
    del fake.procs[100]
    assert not reg.lease_is_live(lease)
    new = reg.open_session("a", "s1", pid=300, boot_id=BOOT)
    assert new.pid == 300 and new.sequence > lease.sequence
    kept = list(path.parent.glob("LEASE.json.v1.*"))
    assert len(kept) == 1 and kept[0].read_bytes() == before


def test_lease_reused_pid_not_live(reg, fake):
    """Catches: a reused pid (same number, different start time) taken for the
    original process.

    Kills the mutant that drops the start-time comparison. Also: a different
    boot id is not live. Negative control: with the start time unchanged the
    lease is live and blocks.
    """
    lease = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    assert reg.lease_is_live(lease)                                  # negative control
    with pytest.raises(StorageOwnershipError):
        reg.open_session("a", "s1", pid=200, boot_id=BOOT)
    path = reg.state_root / "projects" / "a" / "sessions" / "s1" / "LEASE.json"
    before = path.read_bytes()

    fake.procs[100] = 5001                                           # pid reused
    assert not reg.lease_is_live(lease)
    second = reg.open_session("a", "s1", pid=200, boot_id=BOOT)
    kept = sorted(path.parent.glob("LEASE.json.v*"))
    assert [p.read_bytes() for p in kept] == [before]
    assert kept[0].name.startswith("LEASE.json.v1.")

    fake.boot = "boot-2"                                             # rebooted
    assert not reg.lease_is_live(second)
    reg.open_session("a", "s1", pid=200, boot_id="boot-2")
    assert len(list(path.parent.glob("LEASE.json.v*"))) == 2
    assert any(p.name.startswith("LEASE.json.v2.") for p in path.parent.iterdir())


def test_lease_close_by_other_session_refused(reg):
    """Catches: `close_session` accepting a lease that is not the one on
    record. Negative control: the session's own lease closes, and the closed
    record is kept under a versioned name."""
    own = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    stale = SessionLease(**{**own.__dict__, "sequence": own.sequence + 99})
    with pytest.raises(StorageOwnershipError):
        reg.close_session("a", "s1", stale)
    kept = reg.close_session("a", "s1", own)
    assert kept.exists() and kept.name.startswith("LEASE.json.v1.")
    with pytest.raises(StorageOwnershipError):
        reg.scratch_dir("a", "s1", "scratch")                        # no lease now


# --------------------------------------------------------------------------- #
# K5 -- layout PROTECTED, records before content, legacy record kept
# --------------------------------------------------------------------------- #


def test_registry_dirs_protected_not_owned(reg, tmp_path):
    """Catches: the registry's own layout reported OWNED and so offered to a
    storage action.

    Kills the mutant that removes the PROTECTED branch. Negative control: a
    file strictly inside the scratch dir and the external roots is OWNED.
    """
    directory = scratch(reg)
    created = tmp_path / "created"
    admit(reg, tmp_path)
    reg.create_external_root("a", created, "target_clone")
    runs = make_runs_base(tmp_path / "runs", "r1")
    reg.register_external_root("a", runs, "runs_base",
                               commit_anchor(tmp_path / "proj-a", legacy((runs, "runs_base"))))
    state = reg.state_root
    project = state / "projects" / "a"
    protected = [
        state, state / "OWNER.json", state / "projects", state / "ledger",
        state / "projects" / ".lock", project, project / "PROJECT.json", project / ".lock",
        project / "SEQUENCE.json", project / "SCRATCH_INDEX.json", project / "ROOT_INDEX.json",
        project / "sessions", project / "sessions" / "s1",
        project / "sessions" / "s1" / "LEASE.json", project / "scratch", directory,
        directory / ".vh-owner.json", created, runs,
    ]
    for path in protected:
        assert path.exists(), path
        assert reg.owner_of(path).status == PROTECTED, path
    assert reg.owner_of(state).kind == "registry"
    for path in (owned_file(directory), owned_file(created), runs / "r1" / "state.json"):
        assert reg.owner_of(path).status == OWNED, path            # negative control


def test_before_content_record_exists(tmp_path, fake, monkeypatch):
    """Catches: content created before its ownership record -- `OWNER.json`
    after the first child of the state root, `.vh-owner.json` or the index
    entry after `scratch_dir` returned -- and `scratch_dir` without a lease.

    Kills the mutants that write `.vh-owner.json` after returning and that
    remove the lease check. Negative control: with a lease the call succeeds
    and the directory holds nothing but its record.
    """
    reg = new_registry(tmp_path, fake)
    made: list[tuple[Path, bool]] = []
    real_mkdir = os.mkdir

    def spy(path, *args, **kwargs):
        made.append((Path(path), (reg.state_root / "OWNER.json").exists()))
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", spy)
    reg.init()
    children = [(p, had) for p, had in made if p.parent == reg.state_root]
    assert children and all(had for _, had in children)
    monkeypatch.setattr(os, "mkdir", real_mkdir)

    reg.register_project("a", make_repo(tmp_path / "proj-a"))
    target = reg.state_root / "projects" / "a" / "scratch" / "s1"
    with pytest.raises(StorageOwnershipError):
        reg.scratch_dir("a", "s1", "scratch")                        # no lease
    assert not target.exists()

    reg.open_session("a", "s1", pid=100, boot_id=BOOT)              # negative control
    directory = reg.scratch_dir("a", "s1", "scratch")
    assert directory == target
    assert sorted(os.listdir(directory)) == [".vh-owner.json"]
    record = read_json(directory / ".vh-owner.json")
    assert {k: record[k] for k in ("project_id", "session_id", "kind")} == {
        "project_id": "a", "session_id": "s1", "kind": "scratch"}
    assert "created_utc" in record
    index = read_json(reg.state_root / "projects" / "a" / "SCRATCH_INDEX.json")
    assert [e["path"] for e in index["entries"]] == ["projects/a/scratch/s1"]
    assert reg.scratch_dir("a", "s1", "scratch") == directory        # idempotent


def test_before_content_scratch_refuses_unindexed_directory(reg):
    """Catches: `scratch_dir` adopting a directory that already exists without
    an index entry. Negative control: another session's fresh dir is made."""
    legacy_dir = reg.state_root / "projects" / "a" / "runs" / "s1"
    legacy_dir.mkdir(parents=True)
    owned_file(legacy_dir)
    reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    with pytest.raises(StorageOwnershipError):
        reg.scratch_dir("a", "s1", "runs")
    assert not (legacy_dir / ".vh-owner.json").exists()
    with pytest.raises(StorageOwnershipError):
        reg.scratch_dir("a", "s1", "not-a-kind")
    reg.open_session("a", "s2", pid=200, boot_id=BOOT)
    assert reg.scratch_dir("a", "s2", "runs").is_dir()


def test_bootstrap_record_kept(tmp_path, fake):
    """Catches: the legacy `v0-bootstrap` OWNER.json overwritten instead of
    kept, and a non-empty directory without OWNER.json adopted.

    Kills the mutant that overwrites the v0 record instead of renaming it.
    Negative control: an empty directory initialises without any `.v0`.
    """
    state = tmp_path / "state-v0"
    state.mkdir()
    v0 = b'{"schema": "veriharness.storage-owner/v0-bootstrap",\n "owner":"veriharness"}  \n'
    (state / "OWNER.json").write_bytes(v0)
    (state / "legacy-content").mkdir()
    reg = new_registry(tmp_path, fake, state=state)
    record = reg.init()
    kept = list(state.glob("OWNER.json.v0.*"))
    assert len(kept) == 1 and kept[0].read_bytes() == v0
    assert read_json(state / "OWNER.json")["schema"] == "veriharness.storage-owner/1"
    assert record["supersedes"] == kept[0].name
    assert reg.owner_of(kept[0]).status == PROTECTED
    assert (state / "legacy-content").is_dir()

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    owned_file(occupied)
    with pytest.raises(StorageOwnershipError):
        new_registry(tmp_path, fake, state=occupied).init()
    assert not (occupied / "OWNER.json").exists()

    empty = tmp_path / "empty"                                       # negative control
    empty.mkdir()
    new_registry(tmp_path, fake, state=empty).init()
    assert (empty / "OWNER.json").exists() and not list(empty.glob("OWNER.json.v0.*"))


def test_bootstrap_foreign_owner_refused_and_init_idempotent(tmp_path, fake):
    """Catches: an OWNER.json of another schema or owner adopted, and a second
    `init()` rewriting the `/1` record. Negative control: init twice on our own
    record returns the same record."""
    for number, foreign in enumerate((
        {"schema": "other.owner/1", "owner": "someone"},
        {"schema": "veriharness.storage-owner/1", "owner": "someone-else"},
        "not json at all",
    )):
        state = tmp_path / f"foreign-{number}"
        state.mkdir()
        raw = foreign if isinstance(foreign, str) else json.dumps(foreign)
        (state / "OWNER.json").write_text(raw, encoding="utf-8")
        with pytest.raises(StorageOwnershipError):
            new_registry(tmp_path, fake, state=state).init()
        assert (state / "OWNER.json").read_text(encoding="utf-8") == raw
        assert sorted(os.listdir(state)) == ["OWNER.json"]

    reg = new_registry(tmp_path, fake)
    first = reg.init()
    raw = (reg.state_root / "OWNER.json").read_bytes()
    assert reg.init() == first
    assert (reg.state_root / "OWNER.json").read_bytes() == raw
    assert first["layout"]["projects"] == "projects/"


# --------------------------------------------------------------------------- #
# K6 -- remaining coverage
# --------------------------------------------------------------------------- #


def test_unknown_malformed_record(reg):
    """Catches: a malformed or unreadable `.vh-owner.json` still counted.
    The original record is renamed aside, never removed. Negative control:
    putting the original bytes back restores OWNED."""
    directory = scratch(reg)
    target = owned_file(directory)
    record_path = directory / ".vh-owner.json"
    original = record_path.read_bytes()
    assert reg.owner_of(target).status == OWNED

    os.rename(record_path, directory / "record.v1")
    record_path.write_text("{not json", encoding="utf-8")
    assert reg.owner_of(target).status == UNKNOWN
    os.rename(record_path, directory / "record.v2")
    record_path.mkdir()                                              # unreadable as a file
    assert reg.owner_of(target).status == UNKNOWN
    os.rename(record_path, directory / "record.v3")
    record_path.symlink_to(directory / "record.v1")                  # not a regular file
    assert reg.owner_of(target).status == UNKNOWN
    os.rename(record_path, directory / "record.v4")
    record_path.write_bytes(original)                                # negative control
    assert reg.owner_of(target).status == OWNED


def test_unknown_unregistered_project(reg):
    """Catches: a path under a project directory with no valid PROJECT.json
    counted, even with a record and an index of its own. Negative control:
    the registered project's path is OWNED."""
    good = scratch(reg)
    good_record = read_json(good / ".vh-owner.json")
    pdir = reg.state_root / "projects" / "zz"
    directory = forge_scratch(reg, "zz", "scratch", "s1", dict(good_record, project_id="zz"))
    st = os.lstat(directory)
    write_json(pdir / "SCRATCH_INDEX.json", {
        "schema": "veriharness.storage-scratch-index/1", "project_id": "zz",
        "entries": [{"path": "projects/zz/scratch/s1", "kind": "scratch",
                     "session_id": "s1", "st_dev": st.st_dev, "st_ino": st.st_ino}]})
    assert reg.owner_of(owned_file(directory)).status == UNKNOWN
    assert reg.owner_of(pdir).status == UNKNOWN
    write_json(pdir / "PROJECT.json", {"schema": "veriharness.storage-project/1",
                                       "project_id": "other", "project_root": "/x"})
    assert reg.owner_of(directory / "data.bin").status == UNKNOWN
    assert reg.owner_of(owned_file(good)).status == OWNED            # negative control


def test_unknown_runs_base_child_without_state(reg, tmp_path):
    """Catches: any child of a runs base counted as owned. Only `_arenas/` and
    children whose `state.json` validates with `run_id` equal to the directory
    name are OWNED. Negative control: those two are OWNED."""
    runs = make_runs_base(tmp_path / "runs", "r1")
    reg.register_external_root("a", runs, "runs_base",
                               commit_anchor(tmp_path / "proj-a", legacy((runs, "runs_base"))))
    (runs / "_arenas").mkdir()
    (runs / "junk").mkdir()
    (runs / "misnamed").mkdir()
    (runs / "misnamed" / "state.json").write_text(run_state("r1"), encoding="utf-8")
    (runs / "broken").mkdir()
    (runs / "broken" / "state.json").write_text("{}", encoding="utf-8")
    for name in ("junk", "misnamed", "broken"):
        assert reg.owner_of(owned_file(runs / name)).status == UNKNOWN, name
    assert reg.owner_of(owned_file(runs, "stray.txt")).status == UNKNOWN
    assert reg.owner_of(owned_file(runs / "_arenas")).status == OWNED
    assert reg.owner_of(owned_file(runs / "r1")).status == OWNED


def test_unknown_runs_base_registration_requires_valid_states(reg, tmp_path):
    """Catches: a runs base registered with no run state, or with one that does
    not validate or names another run. Negative control: a valid base."""
    repo = tmp_path / "proj-a"
    empty = tmp_path / "empty-runs"
    empty.mkdir()
    mismatch = make_runs_base(tmp_path / "mismatch", "r1")
    (mismatch / "r2").mkdir()
    (mismatch / "r2" / "state.json").write_text(run_state("r1"), encoding="utf-8")
    anchor = commit_anchor(repo, legacy((empty, "runs_base"), (mismatch, "runs_base")))
    for candidate in (empty, mismatch):
        with pytest.raises(StorageOwnershipError):
            reg.register_external_root("a", candidate, "runs_base", anchor)
    good = make_runs_base(tmp_path / "good", "r1", "r2")
    reg.register_external_root("a", good, "runs_base",
                               commit_anchor(repo, legacy((good, "runs_base"))))


def test_unknown_pending_entry(reg, tmp_path, fake):
    """Catches: a `PENDING` entry (a creation that never reached `CREATED`)
    counted as owned. Recovery through a committed legacy anchor works.
    Negative control: after that re-registration the path is OWNED."""
    runs = tmp_path / "runs"
    admit(reg, tmp_path)
    fake.mounts.add(runs)                                            # aborts after mkdir
    with pytest.raises(StorageOwnershipError):
        reg.create_external_root("a", runs, "runs_base")
    index = read_json(reg.state_root / "projects" / "a" / "ROOT_INDEX.json")
    assert [e["state"] for e in index["entries"]] == ["PENDING"]
    make_runs_base(runs, "r1")
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN
    assert reg.owner_of(runs).status == UNKNOWN

    fake.mounts.clear()                                              # negative control
    reg.register_external_root("a", runs, "runs_base",
                               commit_anchor(tmp_path / "proj-a", legacy((runs, "runs_base"))))
    index = read_json(reg.state_root / "projects" / "a" / "ROOT_INDEX.json")
    assert [e["state"] for e in index["entries"]] == ["ABANDONED", "REGISTERED"]
    assert reg.owner_of(runs / "r1" / "state.json").status == OWNED


def test_external_root_target_clone_and_worktree(reg, tmp_path):
    """Catches: a target clone accepted without a registered runs base whose
    `preserve-policy.json` names it, and a linked worktree whose gitdir does
    not point into a registered clone. Negative control: the real chain
    (runs base -> clone -> `git worktree add`) registers and is OWNED."""
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    clone = make_repo(tmp_path / "clone")
    stray = make_repo(tmp_path / "stray-clone")
    worktree = tmp_path / "wt"
    git(clone, "worktree", "add", "-q", str(worktree))
    stray_wt = tmp_path / "stray-wt"
    git(stray, "worktree", "add", "-q", str(stray_wt))
    anchor = commit_anchor(repo, legacy(
        (runs, "runs_base"), (clone, "target_clone"), (stray, "target_clone"),
        (worktree, "linked_worktree"), (stray_wt, "linked_worktree")))

    with pytest.raises(StorageOwnershipError):                       # no runs base yet
        reg.register_external_root("a", clone, "target_clone", anchor)
    write_json(runs / "preserve-policy.json", {"root": str(clone), "files": {}})
    reg.register_external_root("a", runs, "runs_base", anchor)
    with pytest.raises(StorageOwnershipError):                       # not named by policy
        reg.register_external_root("a", stray, "target_clone", anchor)
    with pytest.raises(StorageOwnershipError):                       # clone not registered
        reg.register_external_root("a", worktree, "linked_worktree", anchor)
    reg.register_external_root("a", clone, "target_clone", anchor)
    with pytest.raises(StorageOwnershipError):                       # points into stray
        reg.register_external_root("a", stray_wt, "linked_worktree", anchor)
    with pytest.raises(StorageOwnershipError):                       # wrong kind for a dir .git
        reg.register_external_root("a", stray, "linked_worktree", anchor)
    reg.register_external_root("a", worktree, "linked_worktree", anchor)
    assert reg.owner_of(worktree / "README").kind == "linked_worktree"
    assert reg.owner_of(worktree / "README").status == OWNED
    assert reg.owner_of(clone / "README").status == OWNED
    assert reg.owner_of(stray / "README").status == UNKNOWN


def test_registry_sequence_grows_and_writes_are_atomic(reg, tmp_path, monkeypatch):
    """Catches: a record sequence that repeats or shrinks, and a failed write
    that leaves a half-written record. Negative control: the record from
    before the failed write is intact."""
    project = reg.state_root / "projects" / "a"
    sequences = [read_json(project / "PROJECT.json")["sequence"]]
    lease = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    sequences.append(lease.sequence)
    directory = reg.scratch_dir("a", "s1", "scratch")
    sequences.append(read_json(directory / ".vh-owner.json")["sequence"])
    sequences.append(read_json(project / "SCRATCH_INDEX.json")["sequence"])
    admit(reg, tmp_path)
    reg.create_external_root("a", tmp_path / "created", "target_clone")
    sequences.append(read_json(project / "ROOT_INDEX.json")["sequence"])
    assert sequences == sorted(set(sequences)), sequences
    assert read_json(project / "SEQUENCE.json")["sequence"] == sequences[-1]
    assert not [p for p in project.rglob("*") if ".tmp." in p.name]

    before = (project / "SCRATCH_INDEX.json").read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        sr._atomic_write_json(project / "SCRATCH_INDEX.json", {"half": True})
    monkeypatch.undo()
    assert (project / "SCRATCH_INDEX.json").read_bytes() == before


def test_registry_reader_helpers(tmp_path):
    """Catches: start time parsed from the wrong field when the command name
    holds spaces or parentheses, and a plain directory taken for a mount.
    Negative control: a well-formed line parses to field 22."""
    fields = "R " + " ".join(str(n) for n in range(4, 53))      # field n holds n
    assert sr._parse_start_time(f"42 (a) b) c) {fields}") == 22
    assert sr._parse_start_time(f"42 (plain) {fields}") == 22
    assert sr._parse_start_time("garbage") is None
    assert sr._parse_start_time("42 (short) R 1 2") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sr._is_mount(plain) is False
    assert sr._pid_exists(-1) is False


def test_registry_unknown_without_owner_record(tmp_path, fake):
    """Catches: `owner_of` answering OWNED for a state root whose OWNER.json is
    gone or foreign. Negative control: with the record in place it is OWNED."""
    reg = new_registry(tmp_path, fake)
    reg.init()
    reg.register_project("a", make_repo(tmp_path / "proj-a"))
    target = owned_file(scratch(reg))
    assert reg.owner_of(target).status == OWNED
    owner = reg.state_root / "OWNER.json"
    os.rename(owner, reg.state_root / "OWNER.json.aside")
    assert reg.owner_of(target).status == UNKNOWN
    os.rename(reg.state_root / "OWNER.json.aside", owner)
    assert reg.owner_of(target).status == OWNED


def test_registry_source_has_no_removal():
    """Catches: any code path in the module that removes a file or directory
    (`unlink`, `rmdir`, `rmtree`, `remove`, `removedirs`), as a name or an
    attribute, anywhere in its AST. Negative control: the scanner finds such a
    call in a snippet that has one."""
    banned = {"unlink", "rmdir", "rmtree", "remove", "removedirs"}

    def hits(source: str) -> list[str]:
        found = []
        for node in ast.walk(ast.parse(source)):
            name = (node.attr if isinstance(node, ast.Attribute)
                    else node.id if isinstance(node, ast.Name) else None)
            if name in banned:
                found.append(name)
        return found

    source = Path(sr.__file__).read_text(encoding="utf-8")
    assert hits(source) == []
    assert hits("obj.unlink(x)\nobj.rmtree(y)\n") == ["unlink", "rmtree"]


def test_registry_mountinfo_parser():
    """Catches: the default mount-id reader picking the wrong mount -- the
    shortest instead of the longest mount point, a string-prefix match
    (`/data-x` under `/data`), the first instead of the last of two mounts
    over one point, or an escaped space not decoded. Negative control: a path
    on no listed mount gets the root mount."""
    text = "\n".join([
        "20 1 8:1 / / rw - ext4 /dev/sda rw",
        "30 20 8:2 / /data rw - ext4 /dev/sdb rw",
        "31 30 8:2 /x /data/bind rw - ext4 /dev/sdb rw",
        "32 30 8:2 /y /data/bind rw - ext4 /dev/sdb rw",
        "40 20 0:5 / /with\\040space rw - tmpfs none rw",
        "garbage",
    ])
    parse = sr._mount_id_from_mountinfo
    assert parse(text, Path("/data/bind/f")) == 32
    assert parse(text, Path("/data/other")) == 30
    assert parse(text, Path("/data-x/f")) == 20
    assert parse(text, Path("/with space/f")) == 40
    assert parse(text, Path("/elsewhere")) == 20                    # negative control
    assert parse("", Path("/x")) is None


# --------------------------------------------------------------------------- #
# K10 -- Revisions 6-8: the integration-review blockers
# --------------------------------------------------------------------------- #


def test_detour_through_foreign_symlink_unknown(reg, tmp_path):
    """Catches (sreg-a B1/A1): a path that leaves its owned root through a
    symlink and comes back -- `s1/out -> foreign`, `foreign/f -> s1/target`
    -- reported OWNED because its given and its resolved form both lie in
    `s1`. A consumer renaming the given name would move the foreign entry.

    Kills the mutant **endpoint-only comparison** (only `lex` and `real` are
    compared; the component walk is dropped). Also: a symlink chain hopping
    through a foreign link, and a `..` that steps out of the root and back.
    Negative control: the same target by its own name, and through a link
    that stays inside `s1`, is OWNED and reports `resolved == target`.
    """
    directory = scratch(reg)
    target = owned_file(directory, "target")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (directory / "out").symlink_to(foreign, target_is_directory=True)
    (foreign / "f").symlink_to(target)
    (directory / "hop").symlink_to(foreign / "f")

    detour = reg.owner_of(directory / "out" / "f")
    assert detour.status == UNKNOWN and detour.resolved is None
    assert reg.owner_of(directory / "hop").status == UNKNOWN
    assert reg.owner_of(Path(f"{directory}/../s1/target")).status == UNKNOWN

    # negative control: no detour
    plain = reg.owner_of(target)
    assert (plain.status, plain.resolved) == (OWNED, target)
    (directory / "in").symlink_to(target)
    inside = reg.owner_of(directory / "in")
    assert (inside.status, inside.resolved) == (OWNED, target)


def test_close_session_requires_token(reg):
    """Catches (sreg-a B2/A4): another session ending a live lease by
    rebuilding a `SessionLease` from the public `LEASE.json` and then taking
    the session (and its scratch) over.

    Kills the mutant **close by public record** (the token check removed:
    any lease equal to the record closes it). The record holds only the
    token's SHA-256; the token is in neither the record nor `repr`. Negative
    control: the holder's own lease, with its token, closes.
    """
    lease = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    data = owned_file(reg.scratch_dir("a", "s1", "scratch"))
    path = reg.state_root / "projects" / "a" / "sessions" / "s1" / "LEASE.json"
    before = path.read_bytes()
    record = read_json(path)
    assert lease.token and lease.token not in before.decode() and lease.token not in repr(lease)
    assert record["token_sha256"] == hashlib.sha256(lease.token.encode()).hexdigest()

    # pid 200 rebuilds the lease from the public record
    fields = ("project_id", "session_id", "pid", "pid_start", "boot_id", "since_utc", "sequence")
    public = SessionLease(**{k: record[k] for k in fields})
    assert public == lease                                           # equal in every public field
    for forged in (public, SessionLease(**{k: record[k] for k in fields}, token="0" * 64)):
        with pytest.raises(StorageOwnershipError):
            reg.close_session("a", "s1", forged)
    assert path.read_bytes() == before
    with pytest.raises(StorageOwnershipError):
        reg.open_session("a", "s1", pid=200, boot_id=BOOT)
    assert reg.owner_of(data).status == OWNED

    kept = reg.close_session("a", "s1", lease)                       # negative control
    assert kept.name.startswith("LEASE.json.v1.") and kept.read_bytes() == before


def test_project_cannot_claim_root_of_another(reg, tmp_path):
    """Catches (sreg-a B3/A2): project B registered at a directory inside A's
    root (git then resolves it to A's repository, so A's records anchor B's
    claims), at a directory containing A's root, or at a linked worktree of
    A's repository (same `--git-common-dir`).

    Kills the mutant **byte-equal-only root check** (only an identical root
    string is refused). Negative control: an unrelated repository registers
    as B.
    """
    repo_a = tmp_path / "proj-a"
    (repo_a / "sub").mkdir()
    worktree = tmp_path / "wt-a"
    git(repo_a, "worktree", "add", "-q", str(worktree))

    for candidate in (repo_a / "sub", tmp_path, worktree):
        with pytest.raises(StorageOwnershipError):
            reg.register_project("b", candidate)
    assert not (reg.state_root / "projects" / "b").exists()

    other = make_repo(tmp_path / "proj-b")                           # negative control
    assert reg.register_project("b", other)["project_root"] == str(other)


def test_hardlink_is_unknown(reg, tmp_path):
    """Catches (sreg-a M5, sreg-b M1): a hard link to a foreign file placed in
    scratch reported OWNED with the foreign inode -- truncating or rewriting
    it would change foreign data.

    Kills the mutant **nlink ignored**. Negative control: a single-link file
    in the same directory is OWNED.
    """
    directory = scratch(reg)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    victim = owned_file(foreign, "victim")
    os.link(victim, directory / "link")
    assert os.stat(directory / "link").st_nlink == 2
    assert reg.owner_of(directory / "link").status == UNKNOWN
    assert reg.owner_of(victim).status == UNKNOWN

    single = owned_file(directory, "single")                         # negative control
    assert os.stat(single).st_nlink == 1
    assert reg.owner_of(single).status == OWNED


def test_bind_mount_boundary_unknown(reg, tmp_path, fake):
    """Catches (sreg-a M2): a foreign directory bind-mounted inside scratch
    (same `st_dev`, not detected by `ismount`) reported OWNED.

    A real bind mount cannot be made under `tmp_path`, so the mount-id reader
    is **injected** (`FakeOS.mount_id`, passed as `mount_id_reader`). The
    scratch dir's own mount id (7) differs from its parent's (1), as the
    arena's `$TMPDIR` mount does; only components strictly below the root
    are compared with it. Kills the mutant **mount id ignored**. Negative
    control: a sibling directory on the root's mount stays OWNED.
    """
    expected = reg.state_root / "projects" / "a" / "scratch" / "s1"
    fake.mount_ids[expected] = 7
    directory = scratch(reg)
    assert directory == expected
    index = read_json(reg.state_root / "projects" / "a" / "SCRATCH_INDEX.json")
    assert index["entries"][0]["mount_id"] == 7

    bind = directory / "bind"
    (bind / "deep").mkdir(parents=True)
    inner, deeper = owned_file(bind), owned_file(bind / "deep")
    sibling = directory / "sibling"
    sibling.mkdir()
    sibling_file = owned_file(sibling)
    assert reg.owner_of(inner).status == OWNED                       # before the mount
    fake.mount_ids[bind] = 9
    for path in (bind, inner, bind / "deep", deeper):
        assert reg.owner_of(path).status == UNKNOWN, path
    assert reg.owner_of(sibling_file).status == OWNED                # negative control
    fake.mount_ids[sibling] = None                                   # unreadable mount id
    assert reg.owner_of(sibling_file).status == UNKNOWN

    admit(reg, tmp_path)
    root = tmp_path / "created"
    reg.create_external_root("a", root, "target_clone")
    (root / "m").mkdir()
    fake.mount_ids[root / "m"] = 11
    assert reg.owner_of(owned_file(root / "m")).status == UNKNOWN
    assert reg.owner_of(owned_file(root)).status == OWNED


def test_clone_of_other_project_registrable(reg, tmp_path):
    """Catches: the one-root-one-project rule over-reaching, so that B's own
    `git clone` of A's repository -- a different repository with its own
    `.git` -- is refused as if it were A's.

    Kills the mutant that identifies a repository by its history (root
    commit, remote URL) instead of its resolved common dir. Negative control
    (the condition flipped): a linked worktree of A's repository, which
    shares A's common dir, is refused.
    """
    repo_a = tmp_path / "proj-a"
    clone = tmp_path / "proj-b"
    git(tmp_path, "clone", "-q", str(repo_a), str(clone))
    assert git(clone, "rev-list", "--max-parents=0", "HEAD") == \
        git(repo_a, "rev-list", "--max-parents=0", "HEAD")
    assert reg.register_project("b", clone)["project_root"] == str(clone)

    worktree = tmp_path / "wt-a"                                      # negative control
    git(repo_a, "worktree", "add", "-q", str(worktree))
    with pytest.raises(StorageOwnershipError):
        reg.register_project("c", worktree)


def test_anchor_of_other_project_refused(reg, tmp_path):
    """Catches (sreg-a B3/A2b): project B, registered at its own clone of A's
    repository, registering a root through a record that names project A --
    A's own commit (an ancestor of B's HEAD, since B cloned it) or a copy of
    it committed in B's repository -- or through a record that names no
    project at all; and B registering A's anchored root.

    Kills the mutant **anchor project_id ignored**. Negative control: B's
    own record naming `b` registers the same root, for B.
    """
    repo_a = tmp_path / "proj-a"
    runs_a = make_runs_base(tmp_path / "runs-a", "r1")
    runs_x = make_runs_base(tmp_path / "runs-x", "r2")
    runs_y = make_runs_base(tmp_path / "runs-y", "r3")
    anchor_a = commit_anchor(repo_a, legacy((runs_a, "runs_base"), (runs_x, "runs_base")))
    reg.register_external_root("a", runs_a, "runs_base", anchor_a)
    clone = tmp_path / "proj-b"
    git(tmp_path, "clone", "-q", str(repo_a), str(clone))
    reg.register_project("b", clone)

    with pytest.raises(StorageOwnershipError):
        reg.register_external_root("b", runs_a, "runs_base", anchor_a)    # A's root
    with pytest.raises(StorageOwnershipError):
        reg.register_external_root("b", runs_x, "runs_base", anchor_a)    # A's record
    copied = commit_anchor(clone, legacy((runs_y, "runs_base")), name="migration/copied.json")
    nameless = commit_anchor(clone, legacy((runs_y, "runs_base"), project=None),
                             name="migration/nameless.json")
    for anchor in (copied, nameless):
        with pytest.raises(StorageOwnershipError):
            reg.register_external_root("b", runs_y, "runs_base", anchor)
    assert reg.owner_of(runs_x / "r2" / "state.json").status == UNKNOWN
    assert reg.owner_of(runs_y / "r3" / "state.json").status == UNKNOWN
    assert reg.owner_of(runs_a / "r1" / "state.json").project_id == "a"

    own = commit_anchor(clone, legacy((runs_y, "runs_base"), project="b"),  # negative control
                        name="migration/own.json")
    assert reg.register_external_root("b", runs_y, "runs_base", own)["state"] == "REGISTERED"
    assert reg.owner_of(runs_y / "r3" / "state.json").project_id == "b"


def test_partial_clone_refused(reg, tmp_path):
    """Catches (sreg-a r2 minor, sreg-b M3): a partial clone accepted as a
    trust root -- `cat-file` on a missing object could fetch it from a
    promisor remote, and git 2.43 ignores `GIT_NO_LAZY_FETCH`. The settings
    are made locally with `git config`; nothing is fetched.

    Kills the mutant **partial-clone check removed**. Covers
    `extensions.partialclone`, `remote.<name>.promisor`, and a registered
    project that turns into a partial clone afterwards (its anchors are then
    refused). Negative control: the same repositories without the setting.
    """
    partial = make_repo(tmp_path / "proj-p")
    git(partial, "config", "extensions.partialclone", "origin")
    promisor = make_repo(tmp_path / "proj-q")
    git(promisor, "config", "remote.origin.promisor", "true")
    for project, repo in (("p", partial), ("q", promisor)):
        with pytest.raises(StorageOwnershipError):
            reg.register_project(project, repo)
    assert not (reg.state_root / "projects" / "p").exists()

    plain = make_repo(tmp_path / "proj-r")                           # negative control
    reg.register_project("r", plain)
    runs = make_runs_base(tmp_path / "runs", "r1")
    anchor = commit_anchor(plain, legacy((runs, "runs_base"), project="r"))
    git(plain, "config", "remote.origin.promisor", "true")
    with pytest.raises(StorageOwnershipError):
        reg.register_external_root("r", runs, "runs_base", anchor)
    git(plain, "config", "remote.origin.promisor", "false")
    with pytest.raises(StorageOwnershipError):                       # set at all: refused
        reg.register_external_root("r", runs, "runs_base", anchor)
    git(plain, "config", "--unset", "remote.origin.promisor")
    assert reg.register_external_root("r", runs, "runs_base", anchor)["state"] == "REGISTERED"


def test_plain_root_inside_enclosing_repo_compared_by_path(reg, tmp_path):
    """Catches (sreg-b r2 B1, sreg-a r4 minor): git discovery reaching an
    enclosing repository -- two plain sibling directories inside one outer
    repository taking its identity (the second then refused as a "checkout"
    of the first), and a plain project root using the outer repository's
    commits as anchors.

    Kills the mutants **ceiling not set** (identity from `--git-common-dir`
    without the toplevel/ceiling check: the second sibling is refused) and
    **ceiling stripped** in `_git` (the outer repository's commit anchors a
    root). Negative control: once the project root is its own repository,
    the same record committed there anchors the root.
    """
    outer = make_repo(tmp_path / "outer")
    first, second = outer / "p1", outer / "p2"
    first.mkdir()
    second.mkdir()
    reg.register_project("p1", first)
    reg.register_project("p2", second)
    assert {read_json(reg.state_root / "projects" / p / "PROJECT.json")["project_root"]
            for p in ("p1", "p2")} == {str(first), str(second)}

    runs = make_runs_base(tmp_path / "runs", "r1")
    record = legacy((runs, "runs_base"), project="p1")
    outer_anchor = commit_anchor(outer, record, name="migration/outer.json")
    with pytest.raises(StorageOwnershipError):
        reg.register_external_root("p1", runs, "runs_base", outer_anchor)
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    git(first, "init", "-q")                                         # negative control
    own = commit_anchor(first, record, name="migration/own.json")
    assert reg.register_external_root("p1", runs, "runs_base", own)["state"] == "REGISTERED"


# --------------------------------------------------------------------------- #
# K11 -- one test per mutant that survived sreg-a M3, plus Rev. 7 rules
# --------------------------------------------------------------------------- #


def test_mutant_st_dev_check(reg, tmp_path, fake):
    """Catches: an object inside an owned root that lies on another device
    than the root (a different filesystem mounted or moved in) reported
    OWNED.

    The device is read through the injected `st_dev_reader`
    (`FakeOS.st_dev`), which reports a different `st_dev` for the one path
    under test. Kills mutant **M02** (the `st_dev` comparison in `owner_of`
    removed). Negative control: the same path with its real `st_dev` is
    OWNED.
    """
    target = owned_file(scratch(reg))
    assert reg.owner_of(target).status == OWNED                      # negative control
    fake.st_devs[target] = os.stat(target).st_dev + 1
    assert reg.owner_of(target).status == UNKNOWN

    admit(reg, tmp_path)
    root = tmp_path / "created"
    reg.create_external_root("a", root, "target_clone")
    inside = owned_file(root)
    assert reg.owner_of(inside).status == OWNED                      # negative control
    fake.st_devs[inside] = os.stat(inside).st_dev + 1
    assert reg.owner_of(inside).status == UNKNOWN


def test_mutant_repo_inside_target(tmp_path, fake):
    """Catches: a legacy root registered although the project repository --
    the anchor's trust root -- lies inside the candidate directory, so the
    "committed record" is content of the very directory it claims.

    Kills mutant **M18** (the repo-inside-target refusal in
    `_verify_anchor` removed): everything else here is valid -- a committed
    anchor on HEAD's line naming project `w`, a runs base with a valid run.
    Negative control: the same record for a runs base the repository does
    not lie in registers.
    """
    reg = new_registry(tmp_path, fake)
    reg.init()
    wrap = make_runs_base(tmp_path / "wrap", "r1")
    repo = make_repo(wrap / "repo")
    reg.register_project("w", repo)
    outside = make_runs_base(tmp_path / "outside", "r1")
    anchor = commit_anchor(repo, legacy((wrap, "runs_base"), (outside, "runs_base"),
                                        project="w"))
    with pytest.raises(StorageOwnershipError):
        reg.register_external_root("w", wrap, "runs_base", anchor)
    assert reg.owner_of(wrap / "r1" / "state.json").status == UNKNOWN

    assert reg.register_external_root("w", outside, "runs_base", anchor)[    # negative control
        "state"] == "REGISTERED"


def test_mutant_resolve_exact(reg, tmp_path):
    """Catches: an external root registered or created through a name that
    is not its exact resolved path -- a symlink to the root, or a symlinked
    parent -- so the index records a path the caller never named.

    Kills mutant **M40** (the `lex != real` refusal in `_resolve_exact`
    removed). Negative control: the canonical paths register and create.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    alias = tmp_path / "alias"
    alias.symlink_to(runs, target_is_directory=True)
    anchor = commit_anchor(repo, legacy((runs, "runs_base")))
    with pytest.raises(StorageOwnershipError):
        reg.register_external_root("a", alias, "runs_base", anchor)
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    parent = tmp_path / "parent"
    parent.mkdir()
    (tmp_path / "parent-link").symlink_to(parent, target_is_directory=True)
    admit(reg, parent)
    with pytest.raises(StorageOwnershipError):
        reg.create_external_root("a", tmp_path / "parent-link" / "new", "target_clone")
    assert not (parent / "new").exists()

    reg.register_external_root("a", runs, "runs_base", anchor)       # negative control
    assert reg.create_external_root("a", parent / "new", "target_clone")["state"] == "CREATED"


def test_mutant_malformed_sequence(reg):
    """Catches: a sequence counter that is not a non-negative integer, or
    that went backwards below a project record's sequence, silently
    restarted -- sequences would then repeat.

    Kills mutant **M41** (the counter refusal replaced by a restart at 0).
    Each bad counter is renamed aside (never removed) before the next one.
    Negative control: the original counter bytes put back, the same writer
    succeeds with a larger sequence.
    """
    scratch(reg)                                                     # index records exist
    project = reg.state_root / "projects" / "a"
    counter = project / "SEQUENCE.json"
    good = counter.read_bytes()
    highest = read_json(counter)["sequence"]
    schema = "veriharness.storage-sequence/1"
    bad_counters = [
        '{"schema": "%s", "sequence": "7"}' % schema,
        '{"schema": "%s", "sequence": -1}' % schema,
        '{"schema": "%s", "sequence": true}' % schema,
        "{not json",
        '{"schema": "%s", "sequence": 1}' % schema,                    # went backwards
    ]
    for number, bad in enumerate(bad_counters, start=1):
        os.rename(counter, project / f"counter-aside-{number}.json")
        counter.write_text(bad, encoding="utf-8")
        with pytest.raises(StorageOwnershipError):
            reg.open_session("a", f"x{number}", pid=200, boot_id=BOOT)
        assert not (project / "sessions" / f"x{number}" / "LEASE.json").exists()
        assert counter.read_text(encoding="utf-8") == bad

    os.rename(counter, project / "counter-aside-last.json")          # negative control
    counter.write_bytes(good)
    assert reg.open_session("a", "x9", pid=200, boot_id=BOOT).sequence == highest + 1


def test_mutant_external_kind_set(reg, tmp_path):
    """Catches: an external root of an unknown kind accepted -- for a kind
    outside `{runs_base, target_clone, linked_worktree}` no structure check
    runs at all, so the kind set is what keeps them mandatory.

    Kills mutant **M49** (the kind-set check removed): the anchor names the
    root with exactly that kind. Negative control: the valid kinds register
    and create.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    anchor = commit_anchor(repo, legacy((runs, "run_root"), (runs, "anything"),
                                        (runs, "runs_base")))
    for kind in ("run_root", "anything"):
        with pytest.raises(StorageOwnershipError):
            reg.register_external_root("a", runs, kind, anchor)
    admit(reg, tmp_path)
    with pytest.raises(StorageOwnershipError):
        reg.create_external_root("a", tmp_path / "new", "anything")
    assert not (tmp_path / "new").exists()
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    assert reg.register_external_root("a", runs, "runs_base", anchor)[    # negative control
        "state"] == "REGISTERED"
    assert reg.create_external_root("a", tmp_path / "new", "target_clone")["state"] == "CREATED"


def test_mutant_scratch_record_schema(reg):
    """Catches: a `.vh-owner.json` in the right place, indexed, with the
    right project, kind and session, but another or no `schema`, honoured.

    Kills mutant **M52** (the schema comparison in `_scratch_entry`
    removed). Negative control: the same forged directory with the right
    schema is OWNED.
    """
    good = read_json(scratch(reg) / ".vh-owner.json")
    for session, schema in (("s2", "veriharness.storage-scratch-owner/0"), ("s3", None)):
        record = {k: v for k, v in good.items() if k != "schema"}
        record["session_id"] = session
        if schema is not None:
            record["schema"] = schema
        directory = forge_scratch(reg, "a", "scratch", session, record)
        index_scratch(reg, "a", "scratch", session)
        assert reg.owner_of(owned_file(directory)).status == UNKNOWN, session

    control = forge_scratch(reg, "a", "scratch", "s4", dict(good, session_id="s4"))
    index_scratch(reg, "a", "scratch", "s4")
    assert reg.owner_of(owned_file(control)).status == OWNED          # negative control


def test_mutant_unreadable_lease(reg, fake):
    """Catches: a `LEASE.json` that cannot be read (malformed JSON, missing
    fields) treated as dead and superseded -- an unreadable lease cannot be
    proven dead, so adopting the session would be a guess.

    Kills mutant **M51** (an unreadable lease superseded instead of
    refused). The holder's pid is gone, so only readability decides. Each
    bad record is renamed aside, never removed. Negative control: the
    original, readable (dead) record put back is superseded and kept as
    `LEASE.json.v1.*`.
    """
    reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    path = reg.state_root / "projects" / "a" / "sessions" / "s1" / "LEASE.json"
    original = path.read_bytes()
    del fake.procs[100]
    for number, bad in enumerate((
        "{not json",
        json.dumps({"schema": "veriharness.storage-lease/1", "pid": 100}),
    ), start=1):
        os.rename(path, path.parent / f"aside-{number}.json")
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(StorageOwnershipError):
            reg.open_session("a", "s1", pid=200, boot_id=BOOT)
        assert path.read_text(encoding="utf-8") == bad
        assert not list(path.parent.glob("LEASE.json.v*"))

    os.rename(path, path.parent / "aside-last.json")                 # negative control
    path.write_bytes(original)
    assert reg.open_session("a", "s1", pid=200, boot_id=BOOT).pid == 200
    kept = list(path.parent.glob("LEASE.json.v1.*"))
    assert len(kept) == 1 and kept[0].read_bytes() == original


def test_mutant_git_env_no_replace(reg, tmp_path):
    """Catches (sreg-a M1/A3): `git replace` getting an anchor past "must be
    committed". The anchor names HEAD's own commit -- trivially an ancestor
    of HEAD -- and a file that exists only on a side commit; after `git
    replace <HEAD> <side-commit>` git reads the side commit's tree under
    HEAD's id, so the file "is" committed at HEAD unless git runs with
    `GIT_NO_REPLACE_OBJECTS=1`.

    Kills mutant **GIT_NO_REPLACE_OBJECTS dropped** from the registry's git
    environment. The replace ref is made in `tmp_path`'s repository only.
    Negative control: the same record really committed on HEAD's line
    registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    record = legacy((runs, "runs_base"))
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_anchor(repo, record)
    git(repo, "checkout", "-q", branch)
    head = git(repo, "rev-parse", "HEAD")
    forged = {"commit": head, "file": ANCHOR_FILE}
    with pytest.raises(StorageOwnershipError):                       # not at HEAD
        reg.register_external_root("a", runs, "runs_base", forged)
    git(repo, "replace", head, side["commit"])
    assert json.loads(git(repo, "show", f"{head}:{ANCHOR_FILE}")) == record
    with pytest.raises(StorageOwnershipError):                       # still not at HEAD
        reg.register_external_root("a", runs, "runs_base", forged)
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    on_head = commit_anchor(repo, record, name="migration/on-head.json")  # negative control
    assert reg.register_external_root("a", runs, "runs_base", on_head)["state"] == "REGISTERED"


def test_mutant_stray_tmp_record_protected(reg):
    """Catches (sreg-a M4/A6): a stray temp file of a crashed record write
    (`.vh-owner.json.tmp.*`, and the `..vh-owner.json.tmp.*` name the
    module's atomic writer uses) inside a scratch dir reported OWNED, and
    stray temps of project and configuration records left UNKNOWN instead
    of PROTECTED.

    Kills the mutant that drops the temp-name branch for scratch records.
    Negative control: content whose name merely resembles a record (no
    leading dot, or another stem) is OWNED.
    """
    directory = scratch(reg)
    for name in (".vh-owner.json.tmp.123.abcd", "..vh-owner.json.tmp.123.abcd"):
        assert reg.owner_of(owned_file(directory, name)).status == PROTECTED, name
    project = reg.state_root / "projects" / "a"
    for path in (project / ".SCRATCH_INDEX.json.tmp.1.ab",
                 reg.state_root / ".CONFIG.json.tmp.1.ab",
                 project / "sessions" / "s1" / ".LEASE.json.tmp.1.ab"):
        path.write_text("{}", encoding="utf-8")
        assert reg.owner_of(path).status == PROTECTED, path

    for name in ("vh-owner.json.tmp.notes", "data.tmp.1", ".vh-owner.jsonx.tmp.1"):
        assert reg.owner_of(owned_file(directory, name)).status == OWNED, name


def test_mutant_unknown_parent_refused(reg, tmp_path):
    """Catches (sreg-a M6/A8, Rev. 7 item 1, Rev. 9a items 2, 4, 5):
    `create_external_root` making a directory inside a foreign (`UNKNOWN`)
    parent that the operator's `CONFIG.json` does not validly list.

    `CONFIG.json` is written directly, as the operator edits it. Kills:
    **N21** as the development document names it (the admissible-parent check
    in `create_external_root` removed: the first creation under a foreign
    parent then succeeds); the reader's state-root refusal removed (the entry
    inside the state root would then be admitted); and N25 in its present form
    (a configuration without `sequence` read as empty instead of refused).
    Every invalid entry -- a symlink to the foreign parent, a path inside the
    state root, `/`, a non-normalised spelling, a relative path -- is listed
    in `invalid_entries` with its reason and never admitted; a creation under
    the parent such an entry resolves to is refused with the entry named.
    Refusals come before the `PENDING` entry: no index entry, no directory.
    Negative control: the same creation under the one valid parent is
    CREATED and OWNED below.
    """
    foreign = tmp_path / "foreign-parent"
    foreign.mkdir()
    assert reg.owner_of(foreign).status == UNKNOWN
    with pytest.raises(StorageOwnershipError, match="not a declared admissible parent"):
        reg.create_external_root("a", foreign / "root", "target_clone")
    assert not (foreign / "root").exists()
    assert not (reg.state_root / "projects" / "a" / "ROOT_INDEX.json").exists()

    declared = tmp_path / "declared"
    declared.mkdir()
    declared2 = tmp_path / "declared2"
    declared2.mkdir()
    link = tmp_path / "declared-link"
    link.symlink_to(foreign, target_is_directory=True)
    inside = reg.state_root / "projects"
    non_normal = f"{tmp_path}/x/../declared2"
    listed = [str(declared), str(link), str(inside), "/", non_normal, "relative/dir"]
    config_path = reg.state_root / "CONFIG.json"
    write_config(reg, {"schema": CONFIG_SCHEMA, "sequence": 1,
                       "admissible_external_parents": listed})

    config = reg.read_config()
    assert config.admissible_external_parents == (declared,)
    assert reg.admissible_external_parents() == (declared,)
    invalid = {e["path"]: e["reason"] for e in config.invalid_entries}
    assert set(invalid) == {str(link), str(inside), "/", non_normal, "relative/dir"}
    assert "not an exact resolved path" in invalid[str(link)]
    assert "registry's own layout" in invalid[str(inside)]
    assert "filesystem root" in invalid["/"]
    assert "not normalised" in invalid[non_normal]
    assert "not an absolute path" in invalid["relative/dir"]

    with pytest.raises(StorageOwnershipError, match=re.escape(repr(non_normal))) as info:
        reg.create_external_root("a", declared2 / "root", "target_clone")
    assert "not normalised" in str(info.value)
    with pytest.raises(StorageOwnershipError, match=re.escape(repr(str(link)))):
        reg.create_external_root("a", foreign / "root", "target_clone")
    assert not (declared2 / "root").exists() and not (foreign / "root").exists()
    assert not (reg.state_root / "projects" / "a" / "ROOT_INDEX.json").exists()

    os.rename(config_path, reg.state_root / "CONFIG.json.v1.20261001T000000Z")
    write_config(reg, {"schema": CONFIG_SCHEMA, "admissible_external_parents": [str(declared)]})
    with pytest.raises(StorageOwnershipError, match="sequence"):
        reg.read_config()
    with pytest.raises(StorageOwnershipError, match="sequence"):
        reg.create_external_root("a", declared / "root", "target_clone")
    assert not (declared / "root").exists()

    os.rename(config_path, reg.state_root / "CONFIG.json.v2.20261001T000000Z")
    write_config(reg, {"schema": CONFIG_SCHEMA, "sequence": 2,         # negative control
                       "admissible_external_parents": listed})
    assert reg.owner_of(config_path).status == PROTECTED
    assert reg.create_external_root("a", declared / "root", "target_clone")["state"] == "CREATED"
    assert reg.owner_of(owned_file(declared / "root")).status == OWNED


# --------------------------------------------------------------------------- #
# K12 -- Revision 9 / 9a: anchor reasons, git environment, sequence floor,
# operator-only configuration, N23 and N26
# --------------------------------------------------------------------------- #


def test_mutant_anchor_schema(reg, tmp_path):
    """Catches: an anchor record accepted whatever its top-level `schema`.

    Kills the mutant that drops the schema comparison in `_verify_anchor`:
    each bad record is the good record naming project `a`, with only the
    schema changed (another version, a near miss with trailing space, a
    non-string); the refusal's reason is the schema rule, not the project-id
    rule. Negative control: the good record registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    good = legacy((runs, "runs_base"))
    for number, schema in enumerate(("veriharness.storage-legacy-roots/2",
                                     LEGACY_SCHEMA + " ", ["veriharness.storage-legacy-roots/1"])):
        bad = one_change(good, lambda r: r.update(schema=schema))
        anchor = commit_anchor(repo, bad, name=f"migration/schema-{number}.json")
        refused_with(reg, runs, "runs_base", anchor, SCHEMA_REASON)
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    anchor = commit_anchor(repo, good)                               # negative control
    assert reg.register_external_root("a", runs, "runs_base", anchor)["state"] == "REGISTERED"


def test_mutant_anchor_kind(reg, tmp_path):
    """Catches: an anchor whose `roots[]` entry names the path with another
    kind accepted for the kind the caller asks for -- the structure check of
    that kind would then run on a root the migration record never vouched for
    as such.

    Kills the mutant that drops `r.get('kind') == kind` in `_verify_anchor`:
    each bad record is the good record naming project `a`, with only the
    entry's kind changed (`target_clone`, `linked_worktree`, missing); the
    refusal's reason is the kind rule. Negative control: kind `runs_base`
    registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    good = legacy((runs, "runs_base"))
    changes = [lambda r: r["roots"][0].update(kind="target_clone"),
               lambda r: r["roots"][0].update(kind="linked_worktree"),
               lambda r: r["roots"][0].pop("kind")]
    for number, change in enumerate(changes):
        anchor = commit_anchor(repo, one_change(good, change), name=f"migration/kind-{number}.json")
        refused_with(reg, runs, "runs_base", anchor, KIND_REASON)
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    anchor = commit_anchor(repo, good)                               # negative control
    assert reg.register_external_root("a", runs, "runs_base", anchor)["state"] == "REGISTERED"


def test_mutant_anchor_path(reg, tmp_path):
    """Catches: an anchor whose `roots[]` entry names another path accepted
    for the path being registered.

    Kills the mutant that drops `r.get('path') == str(target)` in
    `_verify_anchor`: each bad record is the good record naming project `a`,
    with only the entry's path changed -- a sibling directory's resolved path
    (itself a valid runs base), and a non-normalised spelling of the very same
    directory (`<tmp>/x/../runs`; only the exact resolved string counts). The
    refusal's reason is the path rule. Negative control: the exact resolved
    path registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    sibling = make_runs_base(tmp_path / "runs-sibling", "r2")
    good = legacy((runs, "runs_base"))
    for number, other in enumerate((str(sibling), f"{tmp_path}/x/../runs")):
        bad = one_change(good, lambda r: r["roots"][0].update(path=other))
        anchor = commit_anchor(repo, bad, name=f"migration/path-{number}.json")
        refused_with(reg, runs, "runs_base", anchor, PATH_REASON)
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    anchor = commit_anchor(repo, good)                               # negative control
    assert reg.register_external_root("a", runs, "runs_base", anchor)["state"] == "REGISTERED"


def test_grafts_file_cannot_make_side_commit_ancestor(reg, tmp_path):
    """Catches (sreg2-a M1): `.git/info/grafts` in the project repository
    making a side-branch anchor commit an "ancestor" of HEAD, so a record that
    was never on HEAD's line anchors a root.

    Kills the mutant that strips `GIT_GRAFT_FILE` instead of fixing it to
    `/dev/null` (git then reads `.git/info/grafts`). The test shows the attack
    is real: with the grafts line `<HEAD> <HEAD^> <side>`, a plain git says
    the side commit is an ancestor. Controls: before the grafts line the side
    anchor is refused for the same reason; a record really committed on
    HEAD's line registers with the grafts file still present.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    record = legacy((runs, "runs_base"))
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_anchor(repo, record)
    git(repo, "checkout", "-q", branch)
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    git(repo, "add", "main.txt")
    git(repo, "commit", "-q", "-m", "main")
    head = git(repo, "rev-parse", "HEAD")

    refused_with(reg, runs, "runs_base", side, "is not an ancestor of HEAD")   # control
    assert git_rc(repo, "merge-base", "--is-ancestor", side["commit"], "HEAD") == 1

    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text(
        f"{head} {base} {side['commit']}\n", encoding="utf-8")
    assert git_rc(repo, "merge-base", "--is-ancestor", side["commit"], "HEAD") == 0
    refused_with(reg, runs, "runs_base", side, "is not an ancestor of HEAD")
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    on_head = commit_anchor(repo, record, name="migration/on-head.json")  # negative control
    assert reg.register_external_root("a", runs, "runs_base", on_head)["state"] == "REGISTERED"


def test_git_ignores_global_config(reg, tmp_path, monkeypatch):
    """Catches (sreg2-b N2): the registry's git reading the user's global
    configuration -- `~/.gitconfig` and `$XDG_CONFIG_HOME/git/config` -- so a
    setting there changes what a project repository is. Here a global
    `remote.origin.promisor = true` makes every clean repository look like a
    partial clone and unregistrable (the same lever could add settings that
    matter more).

    Kills the mutant without `GIT_CONFIG_GLOBAL=/dev/null` in the fixed git
    environment. `HOME` and `XDG_CONFIG_HOME` point at a fake home under
    `tmp_path` only around the registry calls; the test shows a plain git
    there does read the setting. Negative control: the same setting in the
    repository's own (local) config is refused as a partial clone.
    """
    clean = make_repo(tmp_path / "proj-c")
    local = make_repo(tmp_path / "proj-d")
    git(local, "config", "remote.origin.promisor", "true")
    fake_home = tmp_path / "fake-home"
    xdg = fake_home / "xdg"
    (xdg / "git").mkdir(parents=True)
    promisor = '[remote "origin"]\n\tpromisor = true\n'
    (fake_home / ".gitconfig").write_text(promisor, encoding="utf-8")
    (xdg / "git" / "config").write_text(promisor, encoding="utf-8")

    with monkeypatch.context() as m:
        m.setenv("HOME", str(fake_home))
        m.setenv("XDG_CONFIG_HOME", str(xdg))
        assert "promisor" in git(clean, "config", "--get-regexp", r"remote\..*\.promisor")
        assert reg.register_project("c", clean)["project_root"] == str(clean)
        with pytest.raises(StorageOwnershipError, match="partial clone"):  # negative control
            reg.register_project("d", local)
    assert not (reg.state_root / "projects" / "d").exists()


def test_sequence_never_shrinks_after_counter_renamed(reg):
    """Catches (sreg2-a M2, Rev. 9a item 1): once `SEQUENCE.json` is renamed
    away, the next sequence restarting low -- below sequences already on
    record, which then repeat.

    The calls are ordered so that the maximum sits in `SCRATCH_INDEX.json`,
    above every lease (open_session, then two scratch_dir calls). Kills the
    mutants that restart the counter at 0/1 (the candidate's behaviour) and
    that take the floor from lease records only. The counter is renamed aside
    with `os.rename` (never removed). Negative control: with the counter in
    place the same call also continues above the maximum.
    """
    project = reg.state_root / "projects" / "a"
    lease = reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    reg.scratch_dir("a", "s1", "scratch")
    reg.scratch_dir("a", "s1", "runs")

    def sequences() -> dict[str, int]:
        found = {}
        for path in project.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.name != ".lock" \
                    and "aside" not in path.name:
                value = json.loads(path.read_text(encoding="utf-8")).get("sequence")
                if isinstance(value, int):
                    found[str(path.relative_to(project))] = value
        return found

    on_record = sequences()
    highest = max(v for k, v in on_record.items() if k != "SEQUENCE.json")
    assert read_json(project / "SCRATCH_INDEX.json")["sequence"] == highest
    assert highest > lease.sequence
    assert highest > max(v for k, v in on_record.items() if "LEASE" in k)

    os.rename(project / "SEQUENCE.json", project / "counter-renamed-aside.json")
    second = reg.open_session("a", "s2", pid=200, boot_id=BOOT)
    assert second.sequence == highest + 1
    assert read_json(project / "SEQUENCE.json")["sequence"] == second.sequence

    third = reg.open_session("a", "s3", pid=300, boot_id=BOOT)      # negative control
    assert third.sequence == second.sequence + 1


def test_sequence_floor_counts_versioned_copies(reg):
    """Catches: the sequence floor read from current records only, while the
    highest sequence on record sits in a versioned copy -- a closed lease is
    renamed to `LEASE.json.v1.<UTC>` without a new sequence, so after the
    counter is renamed away its sequence would be handed out again.

    Kills the mutant whose floor skips `<name>.v<N>.<UTC>` copies. Negative
    control: the current records alone are below the closed lease.
    """
    project = reg.state_root / "projects" / "a"
    reg.open_session("a", "s1", pid=100, boot_id=BOOT)
    reg.scratch_dir("a", "s1", "scratch")
    closing = reg.open_session("a", "s2", pid=200, boot_id=BOOT)
    kept = reg.close_session("a", "s2", closing)
    assert read_json(kept)["sequence"] == closing.sequence
    current = [read_json(p)["sequence"] for p in (
        project / "PROJECT.json", project / "SCRATCH_INDEX.json",
        project / "sessions" / "s1" / "LEASE.json",
        project / "scratch" / "s1" / ".vh-owner.json")]
    assert max(current) < closing.sequence                           # negative control

    os.rename(project / "SEQUENCE.json", project / "counter-renamed-aside.json")
    assert reg.open_session("a", "s3", pid=300, boot_id=BOOT).sequence == closing.sequence + 1


def test_no_api_extends_admissible_parents(reg, tmp_path):
    """Catches (sreg2-a M3, Rev. 9 item 4): any consumer of the registry
    admitting a foreign parent for new external roots through the public API
    -- the list must be the operator's alone, edited by hand in `CONFIG.json`.

    Kills the mutant that keeps (or re-adds) a declare/add method. Introspects
    the public API of the class and of an instance: no callable other than
    the read-only readers mentions admissible/parent, and none is named
    declare/add/admit/extend. Then a consumer call that creates a root under
    an undeclared foreign parent is refused and leaves `CONFIG.json` byte for
    byte as it was; so do the readers. Negative control: the same call under
    the declared parent creates the root, and `CONFIG.json` is still
    unchanged.
    """
    readers = {"admissible_external_parents", "read_config"}
    for owner in (StorageRegistry, reg):
        public = [n for n in dir(owner) if not n.startswith("_")]
        methods = [n for n in public if callable(getattr(owner, n))]
        assert {"init", "create_external_root", "owner_of"} <= set(methods)
        assert [n for n in methods if ("admissible" in n or "parent" in n)
                and n not in readers] == []
        assert [n for n in public
                if any(word in n for word in ("declare", "add", "admit", "extend"))] == []

    declared = tmp_path / "declared"
    declared.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    admit(reg, declared)
    config_path = reg.state_root / "CONFIG.json"
    before = config_path.read_bytes()
    assert reg.read_config().admissible_external_parents == (declared,)
    assert reg.admissible_external_parents() == (declared,)
    with pytest.raises(StorageOwnershipError, match="not a declared admissible parent"):
        reg.create_external_root("a", foreign / "root", "target_clone")
    assert not (foreign / "root").exists()
    assert config_path.read_bytes() == before

    assert reg.create_external_root("a", declared / "root", "target_clone")[   # control
        "state"] == "CREATED"
    assert config_path.read_bytes() == before


def test_mutant_classify_external_names_project(reg, tmp_path):
    """Catches (sreg2-a M4, N23): an external-root entry in project B's
    `ROOT_INDEX.json` that names another project (or none) still making paths
    below its root OWNED -- for B.

    Kills **N23** (the `entry.get('project_id') != project_id` skip in
    `_classify_external` removed). The forged entries keep the genuine
    `st_dev`/`st_ino`/`mount_id`, so only the named project differs. The
    superseded index bytes are kept by `os.rename` to a versioned name; the
    forged index is written under `tmp_path`. Negative control: the genuine
    entry naming `b` gives OWNED for `b`, before and after.
    """
    reg.register_project("b", make_repo(tmp_path / "proj-b"))
    (tmp_path / "ext").mkdir()
    admit(reg, tmp_path / "ext")
    root = tmp_path / "ext" / "root-b"
    reg.create_external_root("b", root, "target_clone")
    target = owned_file(root)
    index_path = reg.state_root / "projects" / "b" / "ROOT_INDEX.json"
    genuine = index_path.read_bytes()
    result = reg.owner_of(target)
    assert (result.status, result.project_id) == (OWNED, "b")       # negative control

    record = json.loads(genuine)
    for number, named in enumerate(("a", None), start=1):
        entry = dict(record["entries"][0])
        if named is None:
            entry.pop("project_id")
        else:
            entry["project_id"] = named
        os.rename(index_path, index_path.with_name(f"ROOT_INDEX.json.v{number}.20261001T000000Z"))
        write_json(index_path, {**record, "entries": [entry]})
        assert {k: entry[k] for k in ("st_dev", "st_ino", "mount_id")} == {
            k: record["entries"][0][k] for k in ("st_dev", "st_ino", "mount_id")}
        assert reg.owner_of(target).status == UNKNOWN, named
        assert reg.owner_of(root).status == UNKNOWN, named

    os.rename(index_path, index_path.with_name("ROOT_INDEX.json.v3.20261001T000000Z"))
    index_path.write_bytes(genuine)                                   # negative control
    result = reg.owner_of(target)
    assert (result.status, result.project_id) == (OWNED, "b")


def test_mutant_git_redirect_vars_stripped(reg, tmp_path, monkeypatch):
    """Catches (sreg2-a M4, N26): the caller's `GIT_DIR` redirecting the
    registry's `git -C <project_root>` to another repository, in which a
    commit that the project repository does not have is an ancestor of HEAD
    -- so a record from that other repository anchors a root.

    Kills **N26** (the comprehension in `_git` that strips redirecting
    variables removed). `GIT_DIR` is set (absolute) only after all commits
    are made. Negative control: with `GIT_DIR` still set, a real anchor
    committed in the project repository registers.
    """
    repo = tmp_path / "proj-a"
    runs = make_runs_base(tmp_path / "runs", "r1")
    record = legacy((runs, "runs_base"))
    other = make_repo(tmp_path / "other-repo")
    foreign = commit_anchor(other, record)
    own = commit_anchor(repo, record, name="migration/own.json")
    assert git_rc(repo, "cat-file", "-e", f"{foreign['commit']}^{{commit}}") != 0

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert git_rc(repo, "merge-base", "--is-ancestor", foreign["commit"], "HEAD") == 0
    refused_with(reg, runs, "runs_base", foreign, "is not an ancestor of HEAD")
    assert reg.owner_of(runs / "r1" / "state.json").status == UNKNOWN

    assert reg.register_external_root("a", runs, "runs_base", own)[       # negative control
        "state"] == "REGISTERED"
