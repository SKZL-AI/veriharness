"""Adopting an existing project directory through a committed anchor
(STORAGE-A follow-up `project-adopt`, P2-38): `StorageRegistry.adopt_project`
and its CLI door `hoh storage adopt-project`.

Every test builds everything in its own `tmp_path`: an initialised state root,
a project directory shaped like the real one (a bootstrap marker
`.vh-owner.json` and the kind directories `receipts/`, `runs/`, `scratch/`),
and trust-root repositories with a committed adoption anchor. `HOME` and the
git configuration are confined by the autouse fixture `isolated_env`; the
mount check, mount ids and `st_dev` are injected (`FakeOS`) except in the CLI
tests, which run the real readers on `tmp_path`. No test reads or writes a
real state root or repository.

Each test names the failure it catches and the mutant it kills, and carries a
negative control that changes only the condition under test. Every refusal is
asserted with its rule's message (`match=`) and with an unchanged project
directory: the same names, the same bytes, the same inodes (the bootstrap
marker keeps its name and `st_ino`), and no `PROJECT.json`, `SEQUENCE.json`
or `.lock` that was not there before. The registry-wide `projects/.lock`
lies outside the project directory and is not part of that check.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
from pathlib import Path

import pytest

from hoh import cli
from hoh import storage_registry as sr
from hoh.contracts import RunState
from hoh.storage_registry import (
    OWNED,
    PROTECTED,
    UNKNOWN,
    StorageOwnershipError,
    StorageRegistry,
)
from test_storage_registry import (  # noqa: F401  (isolated_env is an autouse fixture)
    FakeOS,
    git,
    git_rc,
    isolated_env,
    make_repo,
    new_registry,
    read_json,
)

P = "veriharness"
ADOPT_SCHEMA = "veriharness.storage-project-adopt/1"
LEGACY_SCHEMA = "veriharness.storage-legacy-roots/1"
SCRATCH_RECORD_SCHEMA = "veriharness.storage-scratch-owner/1"
SCRATCH_INDEX_SCHEMA = "veriharness.storage-scratch-index/1"
ROOT_INDEX_SCHEMA = "veriharness.storage-root-index/1"
SEQUENCE_SCHEMA = "veriharness.storage-sequence/1"
PROJECT_SCHEMA = "veriharness.storage-project/1"
ANCHOR = "dogfood/storage-anchors/veriharness-project-adopt.json"
MARKER = ".vh-owner.json"
VERSIONED = ".vh-owner.json.v0.20261003T000000000000Z"
KINDS = ("receipts", "runs", "scratch")
BOOT = "boot-1"
SESSION = "80f56617"


@pytest.fixture
def fake() -> FakeOS:
    return FakeOS()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def marker_bytes(project: str = P) -> bytes:
    """A bootstrap marker with the real marker's field set."""
    return json.dumps({
        "project_id": project, "kind": "project",
        "created_utc": "2026-09-30T12:00:00.000000+00:00",
        "note": "bootstrap marker written before the registry existed",
    }).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, record: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def commit_adopt(repo: Path, record: object, name: str = ANCHOR) -> dict[str, str]:
    """Commit `record` as `name` in `repo`; the anchor `{"commit", "file"}`."""
    write_json(repo / name, record)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"anchor {name}")
    return {"commit": git(repo, "rev-parse", "HEAD"), "file": name}


def run_state(run_id: str) -> str:
    return RunState(
        run_id=run_id, repo_path="/repo", project_name="demo", spec_path="spec.md",
        spec_digest="s" * 8, policy_digest="p" * 8, profile_digest="f" * 8,
    ).model_dump_json()


def park(path: Path, tmp_path: Path) -> Path:
    """Move `path` out of the way by a rename (never a removal)."""
    parked = tmp_path / "parked"
    parked.mkdir(exist_ok=True)
    target = parked / f"{path.name}.{len(os.listdir(parked))}"
    os.rename(path, target)
    return target


class World:
    """An initialised registry with the project directory `projects/<p>/`
    shaped like the real one: the bootstrap marker and the kind directories.
    Trust-root repositories go under `<tmp>/outside/`."""

    def __init__(self, tmp_path: Path, fake: FakeOS, *, state: Path | None = None,
                 project: str = P, kinds: tuple[str, ...] = KINDS,
                 marker_project: str | None = None) -> None:
        self.tmp = tmp_path
        self.fake = fake
        self.reg = new_registry(tmp_path, fake, state=state)
        self.reg.init()
        self.project = project
        self.pdir = self.reg.state_root / "projects" / project
        self.pdir.mkdir()
        self.marker = self.pdir / MARKER
        self.marker.write_bytes(marker_bytes(marker_project or project))
        self.marker_sha = sha(self.marker.read_bytes())
        for kind in kinds:
            (self.pdir / kind).mkdir()
        self.kinds = sorted(kinds)

    def record(self, root: Path | str, **changes: object) -> dict:
        record = {
            "schema": ADOPT_SCHEMA,
            "project_id": self.project,
            "project_dir": str(self.pdir),
            "project_root": str(Path(root).resolve()),
            "bootstrap_marker": {"name": MARKER, "sha256": self.marker_sha},
            "kinds": list(self.kinds),
            "note": "adoption anchor of a test",
        }
        record.update(changes)
        return record

    def repo(self, name: str = "trust", **changes: object) -> tuple[Path, dict[str, str]]:
        """A clean trust root `<tmp>/outside/<name>` with a valid anchor."""
        repo = make_repo(self.tmp / "outside" / name)
        return repo, commit_adopt(repo, self.record(repo, **changes))

    def adopt(self, root: Path, anchor: dict[str, str]) -> dict:
        return self.reg.adopt_project(self.project, root, anchor)


def snapshot(pdir: Path) -> dict[str, tuple]:
    """Every name below `pdir` with its type, its bytes and its inode."""
    if not os.path.lexists(pdir):
        return {"": ("absent",)}
    out: dict[str, tuple] = {}
    for top, dirs, files in os.walk(pdir):
        for name in sorted(dirs + files):
            path = Path(top) / name
            st = os.lstat(path)
            rel = str(path.relative_to(pdir))
            if stat.S_ISREG(st.st_mode):
                try:
                    data = path.read_bytes()
                except OSError:
                    data = None
                out[rel] = ("file", data, st.st_ino, st.st_nlink)
            elif stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", st.st_ino)
            else:
                out[rel] = ("other", stat.S_IFMT(st.st_mode))
    return out


def assert_refused(reg: StorageRegistry, root: Path | str, anchor: object, match: str,
                   *, project: str = P) -> None:
    """`adopt_project` refuses with `match`, and nothing in the project
    directory changes (names, bytes, inodes; no new PROJECT.json,
    SEQUENCE.json or .lock)."""
    pdir = reg.state_root / "projects" / project
    before = snapshot(pdir)
    absent = [n for n in ("PROJECT.json", "SEQUENCE.json", ".lock")
              if not os.path.lexists(pdir / n)]
    with pytest.raises(StorageOwnershipError, match=match):
        reg.adopt_project(project, root, anchor)
    assert snapshot(pdir) == before
    for name in absent:
        assert not os.path.lexists(pdir / name)


def adopted(world: World, root: Path, anchor: dict[str, str]) -> dict:
    """Negative control: the adoption succeeds and PROJECT.json is written."""
    record = world.adopt(root, anchor)
    assert read_json(world.pdir / "PROJECT.json") == record
    assert record["adopted"]["anchor"]["file"] == anchor["file"]
    return record


def side_commit(repo: Path, record: object, name: str = ANCHOR) -> tuple[str, str]:
    """Commit `record` on a side branch `side`; return (main branch, commit)."""
    main = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    commit = commit_adopt(repo, record, name)["commit"]
    git(repo, "checkout", "-q", main)
    return main, commit


# --------------------------------------------------------------------------- #
# K1 -- the anchor and its verification (items 1, 2)
# --------------------------------------------------------------------------- #


def test_adopt_with_committed_anchor(tmp_path, fake):
    """Catches: an adoption that does not record which committed anchor
    proved it, or records an abbreviated commit the ledger cannot cite.

    Kills the mutant **`adopted.anchor` not recorded, or recorded with the
    abbreviated commit**: the call passes a 10-character commit id and the
    record must hold the full 40-hex id. Positive case: `PROJECT.json` is a
    valid project record, and the project directory turns from `UNKNOWN`
    (negative control, before adoption) to `PROTECTED`.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    assert w.reg.owner_of(w.pdir).status == UNKNOWN                  # negative control
    record = w.adopt(repo, {"commit": anchor["commit"][:10], "file": anchor["file"]})

    on_disk = read_json(w.pdir / "PROJECT.json")
    assert on_disk == record
    assert w.reg._project_record(P) == on_disk
    assert on_disk["schema"] == PROJECT_SCHEMA
    assert on_disk["project_root"] == str(repo.resolve())
    assert on_disk["archive_namespace"] == P
    assert isinstance(on_disk["sequence"], int)
    assert len(anchor["commit"]) == 40
    assert on_disk["adopted"]["anchor"] == {"commit": anchor["commit"], "file": ANCHOR}
    assert on_disk["adopted"]["kinds"] == sorted(KINDS)
    assert on_disk["adopted"]["bootstrap_marker"]["sha256"] == w.marker_sha
    assert w.reg.owner_of(w.pdir).status == PROTECTED


def _drop_kinds(r: dict) -> None:
    r.pop("kinds")


MALFORMED = {
    "missing-key": (_drop_kinds, "exactly the keys"),
    "extra-key": (lambda r: r.update(extra="x"), "exactly the keys"),
    "kind-outside-scratch-kinds": (lambda r: r.update(kinds=[*KINDS, "sessions"]),
                                   "kinds must be"),
    "duplicate-kind": (lambda r: r.update(kinds=[*KINDS, "runs"]), "kinds must be"),
    "sha256-63-chars": (lambda r: r["bootstrap_marker"].update(
        sha256=r["bootstrap_marker"]["sha256"][:63]), "bootstrap_marker must be"),
    "sha256-upper-case": (lambda r: r["bootstrap_marker"].update(
        sha256=r["bootstrap_marker"]["sha256"].upper()), "bootstrap_marker must be"),
    "marker-name": (lambda r: r["bootstrap_marker"].update(name="owner.json"),
                    "bootstrap_marker must be"),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_adopt_anchor_malformed_refused(tmp_path, fake, case):
    """Catches: an adoption anchor whose shape is wrong accepted -- a key
    missing or extra, a kind outside `SCRATCH_KINDS` or listed twice, a
    digest that is not 64 lowercase hex, a marker name other than
    `.vh-owner.json`. Each is refused and writes nothing.

    Kills the mutant **the key-set check dropped (extra keys ignored)**
    (case `extra-key`), and the kinds and marker-shape checks dropped (the
    other cases). Negative control: the unmodified record, committed in the
    same repository, adopts.
    """
    change, match = MALFORMED[case]
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    record = w.record(repo)
    change(record)
    bad = commit_adopt(repo, record, "dogfood/storage-anchors/malformed.json")
    assert_refused(w.reg, repo, bad, match)

    good = commit_adopt(repo, w.record(repo))                        # negative control
    adopted(w, repo, good)


def test_adopt_anchor_wrong_schema_refused(tmp_path, fake):
    """Catches: a legacy-roots record (`veriharness.storage-legacy-roots/1`)
    accepted as an adoption anchor.

    Kills the mutant **the schema compared by prefix (`veriharness.storage-`)**:
    the record is otherwise a valid adoption anchor. Negative control: the
    same record with the adoption schema adopts.
    """
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    bad = commit_adopt(repo, w.record(repo, schema=LEGACY_SCHEMA),
                       "dogfood/storage-anchors/legacy.json")
    assert_refused(w.reg, repo, bad, re.escape(f"does not carry schema {ADOPT_SCHEMA}"))

    good = commit_adopt(repo, w.record(repo))                        # negative control
    adopted(w, repo, good)


def test_adopt_anchor_uncommitted_refused(tmp_path, fake):
    """Catches: an anchor read from the working tree (or the index) instead
    of the named commit -- the file exists and is staged, but the named
    commit does not hold it.

    Kills the mutant **the anchor read from `<project_root>/<file>`**.
    Negative control: once committed, the commit that holds it adopts.
    """
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    head = git(repo, "rev-parse", "HEAD")
    write_json(repo / ANCHOR, w.record(repo))
    git(repo, "add", ANCHOR)
    assert (repo / ANCHOR).is_file()
    assert_refused(w.reg, repo, {"commit": head, "file": ANCHOR}, "is not a committed file")

    git(repo, "commit", "-q", "-m", "anchor")                         # negative control
    adopted(w, repo, {"commit": git(repo, "rev-parse", "HEAD"), "file": ANCHOR})


def test_adopt_anchor_not_ancestor_refused(tmp_path, fake):
    """Catches: an anchor committed on a side branch -- never part of the
    history checked out in the trust root -- accepted.

    Kills the mutant **the ancestry check dropped**. Negative control: the
    same anchor (the same commit) after a merge into `HEAD` adopts.
    """
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    _, commit = side_commit(repo, w.record(repo))
    anchor = {"commit": commit, "file": ANCHOR}
    assert_refused(w.reg, repo, anchor, "is not an ancestor of HEAD")

    git(repo, "merge", "-q", "--no-edit", "side")                    # negative control
    adopted(w, repo, anchor)


@pytest.mark.parametrize("trick", ["grafts", "replace"])
def test_adopt_anchor_grafts_ignored(tmp_path, fake, monkeypatch, trick):
    """Catches: `.git/info/grafts` or a `git replace` graft making a side
    commit an "ancestor" of `HEAD` -- a plain git (the test's own
    environment) answers yes, as the assertion shows first.

    Kills the mutant **`adopt_project` runs its own `subprocess` git calls
    without `_GIT_FIXED_ENV`** (no `GIT_GRAFT_FILE=/dev/null`, no
    `GIT_NO_REPLACE_OBJECTS`). Negative control: after a real merge (made
    with grafts and replacements switched off) the same anchor adopts.
    """
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    main, side = side_commit(repo, w.record(repo))
    base = git(repo, "rev-parse", "HEAD")
    (repo / "later").write_text("later\n", encoding="utf-8")
    git(repo, "add", "later")
    git(repo, "commit", "-q", "-m", "later")
    head = git(repo, "rev-parse", "HEAD")
    if trick == "grafts":
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{head} {base} {side}\n",
                                                        encoding="utf-8")
    else:
        git(repo, "replace", "--graft", head, base, side)
    assert git_rc(repo, "merge-base", "--is-ancestor", side, "HEAD") == 0
    anchor = {"commit": side, "file": ANCHOR}
    assert_refused(w.reg, repo, anchor, "is not an ancestor of HEAD")

    monkeypatch.setenv("GIT_GRAFT_FILE", "/dev/null")                # negative control
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "1")
    git(repo, "merge", "-q", "--no-edit", "side")
    adopted(w, repo, anchor)


@pytest.mark.parametrize("case", ["other-project-dir", "other-state-root", "not-normalised"])
def test_adopt_anchor_names_other_dir_refused(tmp_path, fake, case):
    """Catches: an anchor naming another directory accepted for this one --
    `projects/veriharness2`, the same project path under another state root,
    or a non-normalised spelling (`.../projects/./veriharness`) of the right
    one. `project_dir` is compared as an exact string.

    Kills the mutant **`project_dir` compared after `resolve()`, or by
    basename** (the `not-normalised` and `other-state-root` cases).
    Negative control: the exact string adopts.
    """
    w = World(tmp_path, fake)
    projects = w.pdir.parent
    named = {
        "other-project-dir": str(projects / "veriharness2"),
        "other-state-root": str(tmp_path / "other" / "state" / "projects" / P),
        "not-normalised": f"{projects}/./{P}",
    }[case]
    repo = make_repo(tmp_path / "outside" / "trust")
    bad = commit_adopt(repo, w.record(repo, project_dir=named),
                       "dogfood/storage-anchors/other-dir.json")
    assert_refused(w.reg, repo, bad, "names project_dir")

    good = commit_adopt(repo, w.record(repo))                        # negative control
    adopted(w, repo, good)


def test_adopt_anchor_names_other_project_refused(tmp_path, fake):
    """Catches: an anchor naming project `b` accepted for `veriharness`.

    Kills the mutant **the `project_id` comparison dropped**: everything else
    (the directory, the root, the marker digest) names this project.
    Negative control: the record naming `veriharness` adopts.
    """
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    bad = commit_adopt(repo, w.record(repo, project_id="b"),
                       "dogfood/storage-anchors/b.json")
    assert_refused(w.reg, repo, bad, "names project_id 'b'")

    good = commit_adopt(repo, w.record(repo))                        # negative control
    adopted(w, repo, good)


def test_adopt_anchor_names_other_root_refused(tmp_path, fake):
    """Catches: an anchor committed in repository X that names repository Y
    as the trust root, accepted when the call passes X.

    Kills the mutant **the `project_root` comparison dropped**. Negative
    control: an anchor in X naming X adopts.
    """
    w = World(tmp_path, fake)
    x = make_repo(tmp_path / "outside" / "x")
    y = make_repo(tmp_path / "outside" / "y")
    bad = commit_adopt(x, w.record(y), "dogfood/storage-anchors/names-y.json")
    assert_refused(w.reg, x, bad, "names project_root")

    good = commit_adopt(x, w.record(x))                              # negative control
    adopted(w, x, good)


def _inside_case(w: World, case: str, inner: Path) -> tuple[Path, dict[str, str]]:
    """Build one case of `test_adopt_trust_root_inside_project_dir_refused`:
    the trust root and its committed anchor."""
    tmp, scratch, outside = w.tmp, w.pdir / "scratch", w.tmp / "outside"
    outside.mkdir(exist_ok=True)

    def plain(name: str) -> tuple[Path, dict[str, str]]:
        repo = make_repo(outside / name)
        return repo, commit_adopt(repo, w.record(repo))

    def move_in(path: Path, inside: Path) -> None:
        os.rename(path, inside)
        os.symlink(inside, path)

    if case == "a-root-inside":
        return inner, commit_adopt(inner, w.record(inner))
    if case == "a2-worktree-inside-gitdir-outside":
        wt = scratch / "wt2"
        git(tmp, "init", "-q", f"--separate-git-dir={outside / 'sep2git'}", str(wt))
        (wt / "README").write_text("x\n", encoding="utf-8")
        git(wt, "add", "README")
        git(wt, "commit", "-q", "-m", "init")
        return wt, commit_adopt(wt, w.record(wt))
    if case in ("b-linked-worktree", "c-worktree-admin-copied"):
        wt = outside / "wt"
        git(inner, "worktree", "add", "-q", str(wt))
        anchor = commit_adopt(wt, w.record(wt))
        if case == "c-worktree-admin-copied":
            admin = outside / "wt-admin"
            shutil.copytree(inner / ".git" / "worktrees" / "wt", admin)
            (admin / "commondir").write_text(f"{inner / '.git'}\n", encoding="utf-8")
            (wt / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
            assert git(wt, "rev-parse", "--absolute-git-dir") == str(admin.resolve())
        return wt, anchor
    if case == "d-separate-git-dir-inside":
        sep = outside / "sep"
        git(tmp, "init", "-q", f"--separate-git-dir={scratch / 'sepgit'}", str(sep))
        (sep / "README").write_text("x\n", encoding="utf-8")
        git(sep, "add", "README")
        git(sep, "commit", "-q", "-m", "init")
        return sep, commit_adopt(sep, w.record(sep))
    if case == "e-alternates-inside":
        repo, anchor = plain("alt")
        (repo / ".git" / "objects" / "info" / "alternates").write_text(
            f"{inner / '.git' / 'objects'}\n", encoding="utf-8")
        return repo, anchor
    if case == "f-objects-symlinked-inside":
        repo, anchor = plain("objlink")
        move_in(repo / ".git" / "objects", scratch / "objs")
        return repo, anchor
    if case == "g-pack-symlinked-inside":
        repo, anchor = plain("packed")
        git(repo, "repack", "-a", "-q")
        assert any(n.endswith(".pack") for n in os.listdir(repo / ".git" / "objects" / "pack"))
        move_in(repo / ".git" / "objects" / "pack", scratch / "pack")
        return repo, anchor
    if case == "h-fanout-symlinked-inside":
        repo, anchor = plain("fanout")
        blob = git(repo, "rev-parse", f"{anchor['commit']}:{ANCHOR}")
        move_in(repo / ".git" / "objects" / blob[:2], scratch / "fanout")
        return repo, anchor
    if case == "i-refs-symlinked-inside":
        repo, anchor = plain("refslink")
        move_in(repo / ".git" / "refs", scratch / "refs")
        return repo, anchor
    if case == "j-packed-refs-symlinked-inside":
        repo, anchor = plain("packedrefs")
        (repo / ".git" / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted \n{anchor['commit']} refs/tags/t\n",
            encoding="utf-8")
        assert git(repo, "rev-parse", "t") == anchor["commit"]
        move_in(repo / ".git" / "packed-refs", scratch / "packed-refs")
        return repo, anchor
    if case == "k-include-inside":
        repo, anchor = plain("include")
        included = scratch / "included.cfg"
        included.write_text("[user]\n\tname = included\n", encoding="utf-8")
        git(repo, "config", "include.path", str(included))
        return repo, anchor
    if case == "l-config-worktree-symlinked-inside":
        repo, anchor = plain("wtconfig")
        git(repo, "config", "extensions.worktreeConfig", "true")
        inside = scratch / "config.worktree"
        inside.write_text("[core]\n\tplanted = 1\n", encoding="utf-8")
        os.symlink(inside, repo / ".git" / "config.worktree")
        return repo, anchor
    if case == "m-objects-symlinked-outside":
        repo, anchor = plain("objelsewhere")
        (tmp / "elsewhere").mkdir()
        move_in(repo / ".git" / "objects", tmp / "elsewhere" / "objs")
        return repo, anchor
    raise AssertionError(case)


INSIDE_CASES = {
    "a-root-inside": "lies inside",
    "a2-worktree-inside-gitdir-outside": "lies inside",
    "b-linked-worktree": "lies inside",
    "c-worktree-admin-copied": "lies inside",
    "d-separate-git-dir-inside": "lies inside",
    "e-alternates-inside": "names borrowed object stores",
    "f-objects-symlinked-inside": "is a symlink",
    "g-pack-symlinked-inside": "is a symlink in the git store",
    "h-fanout-symlinked-inside": "is a symlink in the git store",
    "i-refs-symlinked-inside": "is a symlink",
    "j-packed-refs-symlinked-inside": "is a symlink",
    "k-include-inside": "lies inside",
    "l-config-worktree-symlinked-inside": "lies inside",
    "m-objects-symlinked-outside": "is a symlink",
}


@pytest.mark.parametrize("case", list(INSIDE_CASES))
def test_adopt_trust_root_inside_project_dir_refused(tmp_path, fake, case):
    """Catches (paspec-a B1, round-2 N1, Revision 3a): an anchor read from
    inside the candidate directory itself. Every case plants a repository at
    `projects/<p>/scratch/repo` (item 3 accepts it: `scratch/` is a kind
    directory) and commits a valid adoption anchor naming its trust root.

    Mutants killed, by case:

    * (a) the trust root is the planted repository -- **the ancestry rule
      dropped** (with the git-store rule it is an overlapping pair, listed in
      the module's equivalent mutants); (a2) only the working tree is inside,
      its separated git dir outside -- the working-tree part of the ancestry
      rule dropped;
    * (b) a linked worktree outside, git dir and common dir inside --
      **the rule applied to the trust root only**; (c) its admin dir copied
      outside, the common dir still inside -- **the rule not applied to the
      common dir**; (d) `--separate-git-dir` inside -- as (b);
    * (e) `objects/info/alternates` naming the planted objects --
      **alternates not checked**;
    * (f) `.git/objects` moved inside and symlinked -- **the resolved-objects
      ancestry and the top-level symlink check dropped**; (m) `.git/objects`
      symlinked to a directory outside every project -- **the top-level-name
      check dropped**;
    * (g) `objects/pack` (holding a pack) symlinked inside -- **the
      object-store walk dropped, or it follows symlinks**; (h) the loose
      fan-out directory holding the anchor's blob symlinked inside -- **the
      walk skips the fan-out directories**;
    * (i) `.git/refs` and (j) `.git/packed-refs` symlinked inside -- **`refs`
      / `packed-refs` left out of the top-level names**;
    * (k) `include.path` into the project directory and (l) a symlinked
      `config.worktree` with `extensions.worktreeConfig` -- **configuration
      origins not checked**.

    Negative control, in the same world (the planted repository and every
    moved directory still in `scratch/`): a clean repository at
    `<tmp>/outside/clean` with the same anchor content apart from
    `project_root` adopts.
    """
    w = World(tmp_path, fake)
    inner = make_repo(w.pdir / "scratch" / "repo")
    root, anchor = _inside_case(w, case, inner)
    assert_refused(w.reg, root, anchor, INSIDE_CASES[case])

    clean, good = w.repo("clean")                                    # negative control
    adopted(w, clean, good)


@pytest.mark.parametrize("case", ["directory", "unreadable", "symlink"])
def test_adopt_alternates_not_a_readable_file_refused(tmp_path, fake, case):
    """Catches (round 2 N3): an `objects/info/alternates` that is not a
    readable regular file treated as absent or empty.

    Kills the mutants **a non-regular file treated as absent** (a
    directory), **a read error treated as empty or absent** (a mode-0 file;
    skipped as root, the mode restored in a `finally`), and **symlinks
    followed in both the walk and the alternates read** (a symlink to an
    empty regular file outside; also refused by the object-store walk).
    Negative control: an empty regular `alternates` (a newline only) adopts.
    """
    if case == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    w = World(tmp_path, fake)
    repo, anchor = w.repo("alt")
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    if case == "directory":
        alternates.mkdir()
        assert_refused(w.reg, repo, anchor, "is not a regular file")
    elif case == "unreadable":
        alternates.write_text("", encoding="utf-8")
        os.chmod(alternates, 0)
        try:
            assert_refused(w.reg, repo, anchor, "cannot be read")
        finally:
            os.chmod(alternates, 0o644)
    else:
        (tmp_path / "elsewhere").mkdir()
        empty = tmp_path / "elsewhere" / "empty"
        empty.write_text("", encoding="utf-8")
        os.symlink(empty, alternates)
        assert_refused(w.reg, repo, anchor, "is a symlink")

    clean, good = w.repo("clean")                                    # negative control
    (clean / ".git" / "objects" / "info" / "alternates").write_text("\n", encoding="utf-8")
    adopted(w, clean, good)


def test_adopt_trust_root_containing_state_root_refused(tmp_path, fake):
    """Catches: a trust root whose working tree contains the state root (the
    state root's parent made a repository): the anchor's repository would
    hold the registry's own records.

    Kills the mutant **the containment direction dropped**. Negative
    control: a repository at `str(state_root) + "-sibling"` adopts, which
    kills a `str.startswith` form of the rule.
    """
    w = World(tmp_path, fake)
    parent = w.reg.state_root.parent
    git(parent, "init", "-q")
    (parent / "README").write_text("x\n", encoding="utf-8")
    git(parent, "add", "README")
    git(parent, "commit", "-q", "-m", "init")
    anchor = commit_adopt(parent, w.record(parent))
    assert_refused(w.reg, parent, anchor, "contains the state root")

    sibling = make_repo(Path(str(w.reg.state_root) + "-sibling"))    # negative control
    adopted(w, sibling, commit_adopt(sibling, w.record(sibling)))


@pytest.mark.parametrize("case", ["plain-directory", "subdirectory", "partial-clone", "bare"])
def test_adopt_trust_root_not_a_repository_refused(tmp_path, fake, case):
    """Catches: a trust root that is not a repository root with a working
    tree -- a plain directory, a subdirectory of a repository, a partial
    clone, and a bare repository holding a valid committed anchor.

    Kills the mutant **`_repo_identity` None accepted**: in the bare case
    the git part alone accepts the anchor (`merge-base --is-ancestor` 0 and
    `cat-file -t` `blob`, both through `StorageRegistry._git` with the
    ceiling), so only the repository-root rule refuses. Negative control: a
    repository with a working tree and a valid anchor adopts.
    """
    w = World(tmp_path, fake)
    outside = tmp_path / "outside"
    match = "not a repository root with a working tree"
    if case == "plain-directory":
        root = outside / "plain"
        root.mkdir(parents=True)
        anchor = {"commit": "0" * 40, "file": ANCHOR}
    elif case == "subdirectory":
        repo = make_repo(outside / "repo")
        root = repo / "sub"
        root.mkdir()
        anchor = commit_adopt(repo, w.record(root))
    elif case == "partial-clone":
        root, anchor = w.repo("partial")
        git(root, "config", "extensions.partialclone", "origin")
        match = "partial clone"
    else:
        work = make_repo(outside / "work")
        root = outside.resolve() / "bare.git"
        anchor = commit_adopt(work, w.record(root))
        git(outside, "clone", "-q", "--bare", str(work), str(root))
        ancestor = StorageRegistry._git(root, "merge-base", "--is-ancestor",
                                        anchor["commit"], "HEAD", ceiling=root.parent)
        blob = StorageRegistry._git(root, "cat-file", "-t", f"{anchor['commit']}:{ANCHOR}",
                                    ceiling=root.parent)
        assert ancestor.returncode == 0
        assert blob.stdout.strip() == "blob"
    assert_refused(w.reg, root, anchor, match)

    clean, good = w.repo("clean")                                    # negative control
    adopted(w, clean, good)


# --------------------------------------------------------------------------- #
# K2 -- the layout re-check on disk (item 3)
# --------------------------------------------------------------------------- #


def _plant_unexpected(pdir: Path, case: str) -> Path:
    path = pdir / {"file": "notes.txt", "directory": "other", "symlink": "link",
                   "fifo": "fifo", "sessions": "sessions"}[case]
    if case == "file":
        path.write_text("notes\n", encoding="utf-8")
    elif case in ("directory", "sessions"):
        path.mkdir()
    elif case == "symlink":
        os.symlink(pdir / "receipts", path)
    else:
        os.mkfifo(path)
    return path


@pytest.mark.parametrize("case", ["file", "directory", "symlink", "fifo", "sessions"])
def test_adopt_refuses_unexpected_entry(tmp_path, fake, case):
    """Catches: a project directory holding anything beyond the bootstrap
    marker and kind directories adopted -- a file `notes.txt`, a directory
    `other/`, a symlink `link -> receipts`, a FIFO, `sessions/`. The refusal
    names the entry.

    Kills the mutant **unknown names skipped instead of refused**. Negative
    control: with the entry moved out (a rename), the same anchor adopts.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    planted = _plant_unexpected(w.pdir, case)
    assert_refused(w.reg, repo, anchor, re.escape(f"holds {planted.name!r}"))

    park(planted, tmp_path)                                          # negative control
    adopted(w, repo, anchor)


def _plant_record(w: World, case: str) -> tuple[Path, Path]:
    """Plant a registry record; return (the record, an owned-looking file)."""
    directory = w.pdir / "scratch" / SESSION
    directory.mkdir()
    write_json(directory / MARKER, {
        "schema": SCRATCH_RECORD_SCHEMA, "project_id": P, "session_id": SESSION,
        "kind": "scratch", "created_utc": "2026-09-30T12:00:00+00:00", "sequence": 2,
    })
    x = directory / "x"
    x.write_bytes(b"x")
    if case == "SCRATCH_INDEX.json":
        st = os.lstat(directory)
        path = w.pdir / "SCRATCH_INDEX.json"
        write_json(path, {"schema": SCRATCH_INDEX_SCHEMA, "project_id": P, "sequence": 3,
                          "entries": [{
                              "path": f"projects/{P}/scratch/{SESSION}", "project_id": P,
                              "kind": "scratch", "session_id": SESSION,
                              "created_utc": "2026-09-30T12:00:00+00:00",
                              "st_dev": st.st_dev, "st_ino": st.st_ino,
                              "mount_id": w.reg._mount_id_reader(directory),
                          }]})
    elif case == "ROOT_INDEX.json":
        path = w.pdir / "ROOT_INDEX.json"
        write_json(path, {"schema": ROOT_INDEX_SCHEMA, "project_id": P, "sequence": 3,
                          "entries": []})
    else:
        path = w.pdir / "PROJECT.json.v1.20261003T000000000000Z"
        write_json(path, {"schema": PROJECT_SCHEMA, "project_id": P,
                          "project_root": "/elsewhere", "created_utc": "x",
                          "archive_namespace": P, "sequence": 1})
    return path, x


@pytest.mark.parametrize("case", ["SCRATCH_INDEX.json", "ROOT_INDEX.json",
                                  "PROJECT.json.v1.<UTC>"])
def test_adopt_refuses_planted_registry_record(tmp_path, fake, case):
    """Catches: a registry record planted in the project directory before
    adoption accepted as part of the layout. A planted `SCRATCH_INDEX.json`
    (valid, with the directory's real `st_dev`/`st_ino` and `mount_id`
    equal to `_mount_id_reader`, 1 under `FakeOS`) next to a valid scratch
    record would make that scratch directory `OWNED` the moment
    `PROJECT.json` exists.

    Kills the mutant **the registry's own record names accepted as "layout
    names"**. The test also shows why the rule exists: with the planted
    index back beside an adopted project (what the mutant would produce),
    `owner_of(scratch/<s>/x)` is `OWNED`; without `mount_id` in the entry it
    is `UNKNOWN`, so the demonstration is not vacuous. Negative control: with
    the planted record moved out, the same anchor adopts.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    planted, x = _plant_record(w, case)
    assert_refused(w.reg, repo, anchor, re.escape(f"holds {planted.name!r}"))

    parked = park(planted, tmp_path)                                 # negative control
    adopted(w, repo, anchor)
    assert w.reg.owner_of(x).status == UNKNOWN

    if case == "SCRATCH_INDEX.json":                                 # why the rule exists
        os.rename(parked, planted)
        assert w.reg.owner_of(x).status == OWNED
        index = read_json(planted)
        del index["entries"][0]["mount_id"]
        write_json(planted, index)
        assert w.reg.owner_of(x).status == UNKNOWN


@pytest.mark.parametrize("case", ["symlink", "mount"])
def test_adopt_project_dir_symlink_or_mount_refused(tmp_path, fake, case):
    """Catches: `projects/<p>` adopted although it is a symlink to a
    directory with a valid layout, or a mount point.

    Kills the mutant **`stat` instead of `lstat`, or the mount check
    dropped**. Negative control: the plain directory (moved back, or no
    longer a mount) adopts with the same anchor.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    if case == "symlink":
        (tmp_path / "elsewhere").mkdir()
        real = tmp_path / "elsewhere" / P
        os.rename(w.pdir, real)
        os.symlink(real, w.pdir)
        assert_refused(w.reg, repo, anchor, "is not a plain directory")
        park(w.pdir, tmp_path)                                       # negative control
        os.rename(real, w.pdir)
    else:
        fake.mounts.add(w.pdir)
        assert_refused(w.reg, repo, anchor, "is a mount point")
        fake.mounts.discard(w.pdir)                                  # negative control
    adopted(w, repo, anchor)


@pytest.mark.parametrize("case", ["symlink", "mount", "other-device"])
def test_adopt_kind_not_plain_directory_refused(tmp_path, fake, case):
    """Catches: a kind directory accepted although `runs` is a symlink to a
    directory, a mount, or on another `st_dev` than the project directory.

    Kills the mutant **the per-kind check dropped**. Negative control: the
    plain `runs/` on the project's device adopts with the same anchor.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    runs = w.pdir / "runs"
    if case == "symlink":
        (tmp_path / "elsewhere").mkdir()
        real = tmp_path / "elsewhere" / "runs"
        os.rename(runs, real)
        os.symlink(real, runs)
        assert_refused(w.reg, repo, anchor, "is not a plain directory")
        park(runs, tmp_path)                                         # negative control
        os.rename(real, runs)
    elif case == "mount":
        fake.mounts.add(runs)
        assert_refused(w.reg, repo, anchor, "is a mount")
        fake.mounts.discard(runs)                                    # negative control
    else:
        fake.st_devs[runs] = 987654321
        assert_refused(w.reg, repo, anchor, "on another device")
        fake.st_devs.pop(runs)                                       # negative control
    adopted(w, repo, anchor)


@pytest.mark.parametrize("case", ["extra-mutants", "missing-runs"])
def test_adopt_kinds_must_equal_anchor(tmp_path, fake, case):
    """Catches: the kind directories compared with the anchor's `kinds` as a
    subset -- a `mutants/` directory created after the anchor, or `runs/`
    missing although the anchor names it.

    Kills the mutant **a subset comparison** (either direction). Negative
    control: an anchor naming exactly the directories present adopts.
    """
    kinds = KINDS if case == "extra-mutants" else ("receipts", "scratch")
    w = World(tmp_path, fake, kinds=kinds)
    repo = make_repo(tmp_path / "outside" / "trust")
    bad = commit_adopt(repo, w.record(repo, kinds=sorted(KINDS)),
                       "dogfood/storage-anchors/kinds.json")
    if case == "extra-mutants":
        (w.pdir / "mutants").mkdir()
    assert_refused(w.reg, repo, bad, "not the anchor's kinds")

    present = sorted(n for n in os.listdir(w.pdir) if n in sr.SCRATCH_KINDS)
    good = commit_adopt(repo, w.record(repo, kinds=present))        # negative control
    adopted(w, repo, good)


def test_adopt_marker_digest_mismatch_refused(tmp_path, fake):
    """Catches: a bootstrap marker whose bytes changed after the anchor was
    committed (one byte) accepted.

    Kills the mutant **the digest not compared**. Negative control: an
    anchor carrying the changed marker's digest adopts.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    data = w.marker.read_bytes()
    changed = data.replace(b"bootstrap", b"bootstrbp", 1)
    assert len(changed) == len(data) and changed != data
    w.marker.write_bytes(changed)
    assert_refused(w.reg, repo, anchor, "has sha256")

    good = commit_adopt(repo, w.record(repo, bootstrap_marker={      # negative control
        "name": MARKER, "sha256": sha(changed)}), "dogfood/storage-anchors/changed.json")
    adopted(w, repo, good)


@pytest.mark.parametrize("case", ["symlink", "hard-link", "both-forms", "no-form"])
def test_adopt_marker_not_single_regular_file_refused(tmp_path, fake, case):
    """Catches: a bootstrap marker that is not exactly one regular file with
    one link -- a symlink to a file with the right bytes, a second hard link
    outside the directory, both the live and a versioned form, or no form.

    Kills the mutants **`st_nlink` not checked** (hard-link) and **the form
    count not checked** (both-forms, no-form); the symlink case kills an
    open without `O_NOFOLLOW`. Negative control: one regular marker with one
    link (moved back, or a fresh copy with the same bytes) adopts with the
    same anchor.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    (tmp_path / "elsewhere").mkdir()
    if case == "symlink":
        real = tmp_path / "elsewhere" / MARKER
        os.rename(w.marker, real)
        os.symlink(real, w.marker)
        assert_refused(w.reg, repo, anchor, "without following a symlink")
        park(w.marker, tmp_path)                                     # negative control
        os.rename(real, w.marker)
    elif case == "hard-link":
        os.link(w.marker, tmp_path / "elsewhere" / "second-name")
        assert_refused(w.reg, repo, anchor, "one link")
        park(w.marker, tmp_path)                                     # negative control
        w.marker.write_bytes(marker_bytes())
    elif case == "both-forms":
        copy = w.pdir / VERSIONED
        copy.write_bytes(w.marker.read_bytes())
        assert_refused(w.reg, repo, anchor, "exactly one form")
        park(copy, tmp_path)                                         # negative control
    else:
        parked = park(w.marker, tmp_path)
        assert_refused(w.reg, repo, anchor, "exactly one form")
        os.rename(parked, w.marker)                                  # negative control
    adopted(w, repo, anchor)


def test_adopt_marker_of_other_project_refused(tmp_path, fake):
    """Catches: a bootstrap marker naming project `b` accepted for
    `veriharness` although the anchor carries its matching digest.

    Kills the mutant **the marker's content not parsed**. Negative control:
    a marker naming `veriharness`, with its digest in the anchor, adopts.
    """
    w = World(tmp_path, fake, marker_project="b")
    repo, anchor = w.repo()
    assert json.loads(w.marker.read_bytes())["project_id"] == "b"
    assert_refused(w.reg, repo, anchor, "does not name project 'veriharness'")

    park(w.marker, tmp_path)                                         # negative control
    w.marker.write_bytes(marker_bytes(P))
    good = commit_adopt(repo, w.record(repo, bootstrap_marker={
        "name": MARKER, "sha256": sha(marker_bytes(P))}), "dogfood/storage-anchors/own.json")
    adopted(w, repo, good)


@pytest.mark.parametrize("case", ["unlistable-session-dir", "leftover-below-legacy-floor"])
def test_adopt_sequence_refusals_before_rename(tmp_path, fake, case):
    """Catches (paspec-a M1, Revision 3a item 2): a sequence refusal that
    fires after the bootstrap marker was renamed. Afterwards the listing,
    every file's bytes and the marker's name and inode are unchanged.

    * (a) `scratch/<s>` at mode 0 (restored in a `finally`; skipped as
      root): the floor cannot be read. Kills the mutant **the floor computed
      only inside `_next_sequence`, so the marker is renamed before the
      refusal**.
    * (b) the resumed state (the versioned marker, no `.lock`), a legacy
      `scratch/<s>/.vh-owner.json` with `sequence: 7` and a leftover
      `SEQUENCE.json` with `sequence: 3`: NEEDS_YOU with both values, before
      `.lock` exists. Kills the mutant **the `SEQUENCE.json` validation left
      to `_next_sequence`, which runs after `.lock` was created**.

    Negative control: the readable directory, and a leftover at or above the
    floor, adopt with the same anchor.
    """
    if case == "unlistable-session-dir" and os.geteuid() == 0:
        pytest.skip("root lists a mode-0 directory")
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    session = w.pdir / "scratch" / SESSION
    session.mkdir()
    if case == "unlistable-session-dir":
        os.chmod(session, 0)
        try:
            assert_refused(w.reg, repo, anchor, "cannot list")
        finally:
            os.chmod(session, 0o755)
        assert (w.pdir / MARKER).exists()
    else:
        os.rename(w.marker, w.pdir / VERSIONED)
        write_json(session / MARKER, {
            "schema": SCRATCH_RECORD_SCHEMA, "project_id": P, "session_id": SESSION,
            "kind": "scratch", "created_utc": "x", "sequence": 7})
        write_json(w.pdir / "SEQUENCE.json", {"schema": SEQUENCE_SCHEMA, "sequence": 3})
        assert_refused(w.reg, repo, anchor, r"NEEDS_YOU: .* sequence 3, below 7")
        assert not (w.pdir / ".lock").exists()
        park(w.pdir / "SEQUENCE.json", tmp_path)                     # negative control
        write_json(w.pdir / "SEQUENCE.json", {"schema": SEQUENCE_SCHEMA, "sequence": 9})
    record = adopted(w, repo, anchor)
    if case == "leftover-below-legacy-floor":
        assert record["sequence"] == 10


# --------------------------------------------------------------------------- #
# K3 -- writing (item 4)
# --------------------------------------------------------------------------- #


def _spy_writer(monkeypatch, *, fail_on: str | None = None) -> list[str]:
    """Wrap `sr._atomic_write_json`; record each written name, and raise
    ENOSPC for `fail_on` before anything is written."""
    original = sr._atomic_write_json
    written: list[str] = []

    def spy(path, record):
        if Path(path).name == fail_on:
            raise OSError(errno.ENOSPC, "No space left on device (injected)", str(path))
        written.append(Path(path).name)
        return original(path, record)

    monkeypatch.setattr(sr, "_atomic_write_json", spy)
    return written


@pytest.mark.parametrize("case", ["spy", "injected-failure"])
def test_adopt_writes_project_json_through_atomic_writer(tmp_path, fake, monkeypatch, case):
    """Catches: `PROJECT.json` written by anything but the module's one
    atomic writer (temp file, fsync, `os.replace`, fsync of the directory).

    Kills the mutant **`PROJECT.json` written with `Path.write_text` or
    `json.dump`**: the spy must see the write, and a failure injected into
    that writer must leave no `PROJECT.json`. Negative control (spy case):
    the record the spy saw is the one on disk.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    if case == "spy":
        written = _spy_writer(monkeypatch)
        record = w.adopt(repo, anchor)
        assert "PROJECT.json" in written
        assert read_json(w.pdir / "PROJECT.json") == record
    else:
        _spy_writer(monkeypatch, fail_on="PROJECT.json")
        with pytest.raises(OSError, match="injected"):
            w.adopt(repo, anchor)
        assert not os.path.lexists(w.pdir / "PROJECT.json")
        assert w.reg._project_record(P) is None


def test_adopt_marker_versioned_not_removed(tmp_path, fake):
    """Catches: the bootstrap marker removed, or copied and then removed,
    instead of renamed.

    Kills the mutants **the marker unlinked** and **the marker copied, then
    the original unlinked** (caught by the inode): exactly one
    `.vh-owner.json.v0.*` holds the original bytes and the original
    `st_ino`, `.vh-owner.json` is gone, and `versioned_as` names the file.
    Negative control: before adoption the live marker has those bytes and
    that inode.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    data, ino = w.marker.read_bytes(), os.lstat(w.marker).st_ino     # negative control
    record = w.adopt(repo, anchor)

    versioned = [n for n in os.listdir(w.pdir) if n.startswith(MARKER + ".v0.")]
    assert len(versioned) == 1
    kept = w.pdir / versioned[0]
    assert kept.read_bytes() == data
    assert os.lstat(kept).st_ino == ino
    assert not os.path.lexists(w.marker)
    assert record["adopted"]["bootstrap_marker"] == {"sha256": sha(data),
                                                     "versioned_as": versioned[0]}
    assert w.reg.owner_of(kept).status == UNKNOWN


def test_adopt_resumes_after_crash_before_project_json(tmp_path, fake, monkeypatch):
    """Catches: a crash before `PROJECT.json` (an injected ENOSPC) leaving a
    state no re-run can complete, or a half-registered project.

    Kills the mutants **leftovers refused on a re-run** and **the marker
    renamed after `PROJECT.json`** (the order of steps 1 and 4 swapped: the
    crash would then leave the live marker beside `.lock` and
    `SEQUENCE.json`, which a re-run refuses). After the crash:
    `register_project` still refuses and `owner_of(<pdir>/runs)` is
    `UNKNOWN`. Negative control: the re-run with the same anchor succeeds,
    and exactly one versioned marker exists, named by `versioned_as`.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    _spy_writer(monkeypatch, fail_on="PROJECT.json")
    with pytest.raises(OSError, match="injected"):
        w.adopt(repo, anchor)
    monkeypatch.undo()

    names = set(os.listdir(w.pdir))
    versioned = [n for n in names if n.startswith(MARKER + ".v0.")]
    assert len(versioned) == 1
    assert {".lock", "SEQUENCE.json"} <= names
    assert MARKER not in names and "PROJECT.json" not in names
    with pytest.raises(StorageOwnershipError, match="exists without a valid PROJECT.json"):
        w.reg.register_project(P, repo)
    assert w.reg.owner_of(w.pdir / "runs").status == UNKNOWN

    record = w.adopt(repo, anchor)                                   # negative control
    assert read_json(w.pdir / "PROJECT.json") == record
    assert [n for n in os.listdir(w.pdir) if n.startswith(MARKER + ".v0.")] == versioned
    assert record["adopted"]["bootstrap_marker"]["versioned_as"] == versioned[0]


def test_adopt_leftovers_beside_live_marker_refused(tmp_path, fake):
    """Catches: a crash leftover accepted beside the live marker, where the
    write order (marker renamed first) says none can exist.

    Kills the mutant **leftovers accepted whatever the marker's form**: a
    valid `SEQUENCE.json` beside the live marker is refused. Negative
    control: with it moved out, the same anchor adopts.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    leftover = w.pdir / "SEQUENCE.json"
    write_json(leftover, {"schema": SEQUENCE_SCHEMA, "sequence": 1})
    assert_refused(w.reg, repo, anchor, "beside the live bootstrap marker")

    park(leftover, tmp_path)                                         # negative control
    adopted(w, repo, anchor)


# --------------------------------------------------------------------------- #
# K4 -- idempotence and other projects (item 5)
# --------------------------------------------------------------------------- #


def test_adopt_idempotent(tmp_path, fake):
    """Catches: a second adoption with the same root and anchor writing
    again (a new `PROJECT.json`, a grown sequence).

    Kills the mutant **a re-adoption writes a new `PROJECT.json` (the
    sequence grows)**: the second call returns an equal record, and the
    listing, every file's bytes and inodes and `SEQUENCE.json`'s `sequence`
    are unchanged. Negative control: `register_project` with the same id
    and root returns the same record.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    first = w.adopt(repo, anchor)
    before = snapshot(w.pdir)
    sequence = read_json(w.pdir / "SEQUENCE.json")["sequence"]

    assert w.adopt(repo, anchor) == first
    assert snapshot(w.pdir) == before
    assert read_json(w.pdir / "SEQUENCE.json")["sequence"] == sequence
    assert w.reg.register_project(P, repo) == first                  # negative control


@pytest.mark.parametrize("case", ["other-root", "registered-without-adoption",
                                  "other-anchor"])
def test_adopt_registered_otherwise_refused(tmp_path, fake, case):
    """Catches: an existing registration returned as if it were this
    adoption -- registered with another root, registered through
    `register_project` (no `adopted`), or adopted with another anchor.
    `PROJECT.json` stays byte-identical.

    Kills the mutant **the existing record returned whenever the roots are
    equal** (the `registered-without-adoption` and `other-anchor` cases).
    Negative control: the call that matches the record returns it.
    """
    if case == "registered-without-adoption":
        reg = new_registry(tmp_path, fake)
        reg.init()
        x = make_repo(tmp_path / "outside" / "x")
        first = reg.register_project(P, x)
        pdir = reg.state_root / "projects" / P
        anchor = commit_adopt(x, {
            "schema": ADOPT_SCHEMA, "project_id": P, "project_dir": str(pdir),
            "project_root": str(x.resolve()),
            "bootstrap_marker": {"name": MARKER, "sha256": "0" * 64}, "kinds": []})
        data = (pdir / "PROJECT.json").read_bytes()
        assert_refused(reg, x, anchor, "registered without adoption")
        assert (pdir / "PROJECT.json").read_bytes() == data
        assert reg.register_project(P, x) == first                   # negative control
        return

    w = World(tmp_path, fake)
    x, anchor_a = w.repo("x")
    first = w.adopt(x, anchor_a)
    data = (w.pdir / "PROJECT.json").read_bytes()
    if case == "other-root":
        y, anchor_y = w.repo("y")
        assert_refused(w.reg, y, anchor_y, "is registered with root")
    else:
        anchor_b = commit_adopt(x, w.record(x), "dogfood/storage-anchors/second.json")
        assert_refused(w.reg, x, anchor_b, "was adopted with anchor")
    assert (w.pdir / "PROJECT.json").read_bytes() == data
    assert w.adopt(x, anchor_a) == first                             # negative control


@pytest.mark.parametrize("case", ["own-root", "linked-worktree", "parent-directory"])
def test_adopt_root_of_other_project_refused(tmp_path, fake, case):
    """Catches: a trust root that is another registered project's root, a
    linked worktree of its repository, or a repository containing it.
    Each carries a valid committed adoption anchor.

    Kills the mutant **`adopt_project` does not call
    `_refuse_other_projects_root`**. (`b`'s root lies at
    `<tmp>/bparent/b`, so its parent does not contain the state root and is
    refused by this rule, not by the containment rule.) Negative control: a
    separate clone of `b`'s repository with its own anchor adopts.
    """
    w = World(tmp_path, fake)
    broot = make_repo(tmp_path / "bparent" / "b")
    w.reg.register_project("b", broot)
    if case == "own-root":
        root, match = broot, "equals, lies inside or contains"
    elif case == "linked-worktree":
        root, match = tmp_path / "bwt", "is a checkout of the repository"
        git(broot, "worktree", "add", "-q", str(root))
    else:
        root, match = tmp_path / "bparent", "equals, lies inside or contains"
        git(root, "init", "-q")
        (root / "README").write_text("x\n", encoding="utf-8")
        git(root, "add", "README")
        git(root, "commit", "-q", "-m", "init")
    anchor = commit_adopt(root, w.record(root))
    assert_refused(w.reg, root, anchor, match)

    clone = tmp_path / "bclone"                                      # negative control
    git(tmp_path, "clone", "-q", str(broot), str(clone))
    adopted(w, clone, commit_adopt(clone, w.record(clone),
                                   "dogfood/storage-anchors/clone.json"))


# --------------------------------------------------------------------------- #
# K5 -- nothing OWNED by adoption (item 6)
# --------------------------------------------------------------------------- #


def test_adopt_makes_nothing_owned(tmp_path, fake):
    """Catches: adoption that makes existing content `OWNED` -- the fixture
    copies the real directory's shape with valid-looking records: a
    scratch record with matching ids and an integer `sequence`, a valid run
    state, `runs/.vh-owner.json`, a receipt, and a file deeper down.

    Kills the mutant **adoption writes `SCRATCH_INDEX.json` entries for the
    existing `<kind>/<s>` directories ("adopts the children")**: after
    adoption no index exists and `owner_of` of every path under the project
    directory is `PROTECTED` or `UNKNOWN`, the kind directories
    `PROTECTED`. Positive control: `open_session` and
    `scratch_dir(p, "steward", "receipts")` then work inside the adopted
    project, and a file in `receipts/steward/` is `OWNED` while the legacy
    receipt beside it stays `UNKNOWN`.
    """
    w = World(tmp_path, fake)
    write_json(w.pdir / "scratch" / SESSION / MARKER, {
        "schema": SCRATCH_RECORD_SCHEMA, "project_id": P, "session_id": SESSION,
        "kind": "scratch", "created_utc": "2026-09-30T12:00:00+00:00", "sequence": 4})
    (w.pdir / "runs" / "20261001t0000z-run").mkdir()
    (w.pdir / "runs" / "20261001t0000z-run" / "state.json").write_text(
        run_state("20261001t0000z-run"), encoding="utf-8")
    write_json(w.pdir / "runs" / MARKER, {
        "schema": SCRATCH_RECORD_SCHEMA, "project_id": P, "session_id": "runs",
        "kind": "runs", "created_utc": "x", "sequence": 1})
    receipt = w.pdir / "receipts" / "legacy-receipt.json"
    write_json(receipt, {"receipt": "legacy"})
    deep = w.pdir / "scratch" / SESSION / "reviews" / "r1" / "x.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("print('x')\n", encoding="utf-8")
    repo, anchor = w.repo()
    w.adopt(repo, anchor)

    assert not os.path.lexists(w.pdir / "SCRATCH_INDEX.json")
    assert not os.path.lexists(w.pdir / "ROOT_INDEX.json")
    seen = 0
    for top, dirs, files in os.walk(w.pdir):
        for name in dirs + files:
            status = w.reg.owner_of(Path(top) / name).status
            assert status in (PROTECTED, UNKNOWN), (top, name, status)
            seen += 1
    assert seen >= 10
    for kind in KINDS:
        assert w.reg.owner_of(w.pdir / kind).status == PROTECTED
    assert w.reg.owner_of(w.pdir).status == PROTECTED
    assert w.reg.owner_of(deep).status == UNKNOWN

    w.reg.open_session(P, "steward", pid=100, boot_id=BOOT)          # positive control
    steward = w.reg.scratch_dir(P, "steward", "receipts")
    fresh = steward / "receipt.json"
    write_json(fresh, {"receipt": "new"})
    assert w.reg.owner_of(fresh).status == OWNED
    assert w.reg.owner_of(receipt).status == UNKNOWN


def test_adopt_legacy_roots_still_need_own_anchor(tmp_path, fake):
    """Catches: adoption opening a back door for legacy content -- after it,
    (1) `register_external_root` of `scratch/<s>/reviews/<id>` with a valid
    committed legacy-roots anchor, and (2) `scratch_dir` over the existing
    session directory (with a live lease, so the refusal is not "holds no
    lease") must still be refused, and (3) a file inside both stays
    `UNKNOWN`.

    Kills the mutants **`_refuse_unsafe_root` exempts adopted projects** and
    **`scratch_dir` adopts existing directories of adopted projects**.
    Negative control: in the same adopted project a new scratch directory is
    created and `OWNED`, and an external runs base outside the state root
    registers with its own legacy anchor.
    """
    w = World(tmp_path, fake)
    reviews = w.pdir / "scratch" / SESSION / "reviews" / "r1"
    reviews.mkdir(parents=True)
    inside = reviews / "x.py"
    inside.write_text("x\n", encoding="utf-8")
    legacy_file = w.pdir / "scratch" / SESSION / "notes.md"
    legacy_file.write_text("notes\n", encoding="utf-8")
    repo, anchor = w.repo()
    w.adopt(repo, anchor)

    legacy = commit_adopt(repo, {"schema": LEGACY_SCHEMA, "roots": [
        {"path": str(reviews), "kind": "runs_base", "project_id": P}]},
        "dogfood/storage-anchors/legacy-roots.json")
    with pytest.raises(StorageOwnershipError, match="inside it"):
        w.reg.register_external_root(P, reviews, "runs_base", legacy)
    w.reg.open_session(P, SESSION, pid=100, boot_id=BOOT)
    with pytest.raises(StorageOwnershipError, match="exists but is not in SCRATCH_INDEX.json"):
        w.reg.scratch_dir(P, SESSION, "scratch")
    assert w.reg.owner_of(inside).status == UNKNOWN
    assert w.reg.owner_of(legacy_file).status == UNKNOWN

    w.reg.open_session(P, "fresh1", pid=200, boot_id=BOOT)           # negative control
    fresh = w.reg.scratch_dir(P, "fresh1", "scratch") / "f"
    fresh.write_bytes(b"f")
    assert w.reg.owner_of(fresh).status == OWNED
    base = tmp_path / "runs-base"
    (base / "20261001t0000z-run").mkdir(parents=True)
    (base / "20261001t0000z-run" / "state.json").write_text(
        run_state("20261001t0000z-run"), encoding="utf-8")
    outside = commit_adopt(repo, {"schema": LEGACY_SCHEMA, "roots": [
        {"path": str(base.resolve()), "kind": "runs_base", "project_id": P}]},
        "dogfood/storage-anchors/legacy-outside.json")
    assert w.reg.register_external_root(P, base, "runs_base", outside)["state"] == "REGISTERED"


def test_register_project_still_refuses_existing_dir(tmp_path, fake):
    """Catches: `register_project` taking over an existing project directory
    once an adoption anchor exists.

    Kills the mutant **`register_project` falls through to `adopt_project`,
    or adopts an existing directory that holds a marker**: with a valid,
    committed adoption anchor present it still refuses and writes nothing.
    Negative control: `adopt_project` with that anchor adopts.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    before = snapshot(w.pdir)
    with pytest.raises(StorageOwnershipError, match="exists without a valid PROJECT.json"):
        w.reg.register_project(P, repo)
    assert snapshot(w.pdir) == before

    adopted(w, repo, anchor)                                         # negative control


# --------------------------------------------------------------------------- #
# K6 -- the CLI door (item 7)
# --------------------------------------------------------------------------- #


def _cli(state: Path | None, repo: Path, anchor: dict[str, str], project: str = P) -> int:
    argv = ["storage", "adopt-project", "--project", project, "--project-root", str(repo),
            "--anchor", f"{anchor['commit']}:{anchor['file']}"]
    if state is not None:
        argv += ["--storage-root", str(state)]
    return cli.main(argv)


def test_cli_adopt_project(tmp_path, fake, capsys):
    """Catches: `hoh storage adopt-project` not adopting, or printing
    something other than the record it wrote.

    Kills the mutant **the handler calls `register_project`** (which
    refuses the existing directory, exit 2). Exit 0, and the printed
    `project` equals `PROJECT.json`. Negative control: before the call the
    project is not registered.
    """
    w = World(tmp_path, fake)
    repo, anchor = w.repo()
    assert w.reg._project_record(P) is None                         # negative control
    assert _cli(w.reg.state_root, repo, anchor) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["storage_root"] == str(w.reg.state_root)
    assert out["project"] == read_json(w.pdir / "PROJECT.json")
    assert out["project"]["adopted"]["anchor"] == anchor


def test_cli_adopt_refusal_exit_and_no_write(tmp_path, fake, capsys):
    """Catches: a refused adoption reported as success, or writing.

    Kills the mutant **a refusal exits 0**: a not-ancestor anchor gives exit
    2, `storage adopt-project refused: ...` on stderr, and an unchanged
    project directory. Negative control: the same anchor after a merge into
    `HEAD` exits 0.
    """
    w = World(tmp_path, fake)
    repo = make_repo(tmp_path / "outside" / "trust")
    _, commit = side_commit(repo, w.record(repo))
    anchor = {"commit": commit, "file": ANCHOR}
    before = snapshot(w.pdir)
    assert _cli(w.reg.state_root, repo, anchor) == 2
    err = capsys.readouterr().err
    assert err.startswith("storage adopt-project refused: ")
    assert "is not an ancestor of HEAD" in err
    assert snapshot(w.pdir) == before

    git(repo, "merge", "-q", "--no-edit", "side")                    # negative control
    assert _cli(w.reg.state_root, repo, anchor) == 0
    assert (w.pdir / "PROJECT.json").is_file()


@pytest.mark.parametrize("case", ["uninitialised-root", "no-storage-root-with-env"])
def test_cli_adopt_never_initialises(tmp_path, fake, monkeypatch, capsys, case):
    """Catches: the CLI door initialising a state root, or picking one from
    the environment -- adopting a trust root names its state root
    explicitly.

    Kills the mutants **the handler calls `init()`** (an uninitialised
    `--storage-root` must give exit 2 and no `OWNER.json`) and
    **`--storage-root` falls back to the environment** (`HOH_STATE_ROOT`
    names an initialised root with an adoptable directory and a valid
    anchor, so the mutant would adopt and exit 0; argparse must exit 2 and
    name `--storage-root`, and the directory stays unchanged). Negative
    control: with the explicit `--storage-root` the same call adopts.
    """
    if case == "uninitialised-root":
        empty = tmp_path / "uninit"
        empty.mkdir()
        repo = make_repo(tmp_path / "outside" / "trust")
        anchor = commit_adopt(repo, {"schema": ADOPT_SCHEMA})
        assert _cli(empty, repo, anchor) == 2
        assert "not an initialised registry" in capsys.readouterr().err
        assert os.listdir(empty) == []

        w = World(tmp_path, fake, state=empty)                       # negative control
        repo2, anchor2 = w.repo("trust2")
        assert _cli(empty, repo2, anchor2) == 0
        return

    w = World(tmp_path, fake, state=tmp_path / "second" / "state")
    repo, anchor = w.repo()
    monkeypatch.setenv("HOH_STATE_ROOT", str(w.reg.state_root))
    before = snapshot(w.pdir)
    with pytest.raises(SystemExit) as exc:
        _cli(None, repo, anchor)
    assert exc.value.code == 2
    assert "--storage-root" in capsys.readouterr().err
    assert snapshot(w.pdir) == before
    assert not (w.pdir / "PROJECT.json").exists() and w.marker.is_file()

    assert _cli(w.reg.state_root, repo, anchor) == 0                 # negative control


# --------------------------------------------------------------------------- #
# K7 -- no override (item 8)
# --------------------------------------------------------------------------- #


def test_adopt_no_override_parameters():
    """Catches: an override that skips a rule (`force`, `skip_*`,
    `**kwargs`).

    Kills the mutant **a `force` keyword**: `adopt_project` takes exactly
    `(self, project_id, project_root, anchor)`, none variadic. Negative
    control: the same check rejects a signature with a `force` keyword.
    """
    def plain_params(fn) -> list[str] | None:
        params = inspect.signature(fn).parameters.values()
        if any(p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL) for p in params):
            return None
        return [p.name for p in params]

    assert plain_params(StorageRegistry.adopt_project) == [
        "self", "project_id", "project_root", "anchor"]

    def with_force(self, project_id, project_root, anchor, *, force=False):
        return None

    assert plain_params(with_force) != [                             # negative control
        "self", "project_id", "project_root", "anchor"]
