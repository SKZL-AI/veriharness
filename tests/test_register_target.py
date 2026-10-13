"""P2-42 operating practice: a fresh target clone is registered as an owned project
BEFORE `hoh start` (`dogfood/regproj/spec.md`).

Two CLI doors over the registry, `hoh storage new-target` (the registry makes an empty
`target_clone` directory: owner record first, directory second) and `hoh storage
register-project` (a FRESH project whose root is exactly such a directory, create-only), and
the end-to-end fact they exist for: a `hoh run` of a project registered this way works in a
workspace of its registered root.

Every test builds its world in pytest's ``tmp_path`` (`test_storage_tiers.World`: a registry
with injected process table, boot id and mounts); the CLI tests hand the CLI that registry
(the same patch `test_ephemeral_wiring.through_the_cli` uses), except the one that must see
the real constructor. No test reads or writes a real state root, starts Herdr or an agent,
or removes anything: the world is left to pytest's own tmp handling.

Mutant ids (``M..``) name the spec's table; each test says which it kills.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hoh import cli
from hoh import storage_registry as sr
from hoh import storage_ephemeral as se
from test_ephemeral_wiring import (Fake, beside_the_arenas, documents, records,
                                   through_the_cli)
from test_storage_tiers import PROJECT, World, isolated_env  # noqa: F401  (autouse fixture)

NEW = "hoh-t1"          # the project a fresh target is registered as


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


@pytest.fixture
def w(tmp_path) -> World:
    world = World(tmp_path / "w")
    world.fake.procs[os.getpid()] = 4242          # this process, as the registry's process table has it
    return world


@pytest.fixture
def parent(tmp_path, w) -> Path:
    """The directory the operator declared in the state root's CONFIG.json (by hand: the
    registry offers no method that adds a parent) as one that may hold target clones."""
    directory = (tmp_path / "targets").resolve()
    directory.mkdir()
    (w.registry.state_root / "CONFIG.json").write_text(json.dumps({
        "schema": "veriharness.storage-config/1", "sequence": 1,
        "admissible_external_parents": [str(directory)]}), encoding="utf-8")
    return directory


def door(w: World, monkeypatch, *argv: str) -> int:
    """`hoh storage ...` through the real parser, on the world's registry."""
    monkeypatch.setattr(sr, "StorageRegistry", lambda *a, **k: w.registry)
    return cli.main(["storage", *argv, "--storage-root", str(w.registry.state_root)])


def new_target(w, monkeypatch, path: Path, owner: str = PROJECT) -> int:
    return door(w, monkeypatch, "new-target", "--parent-project", owner, "--path", str(path))


def register(w, monkeypatch, repo: Path | str, project: str = NEW) -> int:
    return door(w, monkeypatch, "register-project", "--project", project, "--repo", str(repo))


def clone_into(w: World, root: Path, pin: str | None = None) -> Path:
    """What the launcher does between the two doors: clone into the (empty) directory."""
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(w.canon), str(root)],
                   check=True, capture_output=True)
    if pin:
        subprocess.run(["git", "-C", str(root), "checkout", "-q", pin, "-B", "loop/t1"],
                       check=True, capture_output=True)
    return root


def made(w, monkeypatch, parent: Path, name: str = "t1", capsys=None) -> Path:
    """A target the product made: new-target, then clone."""
    assert new_target(w, monkeypatch, parent / name) == 0
    return clone_into(w, parent / name)


def snapshot(*roots: Path) -> dict[str, str]:
    """Every file below the roots by relative name and content hash, every directory by name."""
    seen: dict[str, str] = {}
    for top in roots:
        for here, dirs, files in os.walk(top):
            for name in dirs:
                seen[str(Path(here, name))] = "dir"
            for name in files:
                p = Path(here, name)
                seen[str(p)] = ("link" if p.is_symlink()
                                else hashlib.sha256(p.read_bytes()).hexdigest())
    return seen


def index_of(w: World, project: str = PROJECT) -> dict:
    """The owner project's root index ({} before any root was made)."""
    path = w.registry.state_root / "projects" / project / "ROOT_INDEX.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def project_file(w: World, project: str) -> Path:
    return w.registry.state_root / "projects" / project / "PROJECT.json"


# --------------------------------------------------------------------------- #
# R1 -- new-target: the owner record exists before the directory
# --------------------------------------------------------------------------- #


def test_new_target_makes_an_empty_registered_directory_whose_content_is_owned(
        w, parent, monkeypatch, capsys):
    """R1a. Catches a door that makes the directory without the registry (M9: a plain
    `mkdir`: no index entry, the directory stays UNKNOWN) or that reports a directory it did
    not make. The registry made `<parent>/t1` and nothing else: it is empty, the index of the
    owner project holds exactly one entry for it, state CREATED and origin `created`, with the
    inode that is on disk; the directory itself is PROTECTED as a `target_clone` root of the
    owner project, and what is cloned into it afterwards is OWNED by that project. The printed
    document is the entry. Negative control: before the call the path is UNKNOWN (it does not
    exist) and the index has no entry."""
    target = parent / "t1"
    assert w.registry.owner_of(target).status == sr.UNKNOWN
    assert [e for e in index_of(w).get("entries", []) if e["path"] == str(target)] == []
    assert new_target(w, monkeypatch, target) == 0
    out = json.loads(capsys.readouterr().out)
    assert os.listdir(target) == []
    entries = [e for e in index_of(w)["entries"] if e["path"] == str(target)]
    assert len(entries) == 1 and out == {"storage_root": str(w.registry.state_root),
                                         "root": entries[0]}
    st = os.stat(target)
    assert (entries[0]["state"], entries[0]["origin"], entries[0]["kind"], entries[0]["project_id"]) \
        == ("CREATED", "created", "target_clone", PROJECT)
    assert (entries[0]["st_dev"], entries[0]["st_ino"]) == (st.st_dev, st.st_ino)
    verdict = w.registry.owner_of(target)
    assert (verdict.status, verdict.kind, verdict.project_id, verdict.root) \
        == (sr.PROTECTED, "target_clone", PROJECT, target)
    clone_into(w, target)
    inside = w.registry.owner_of(target / "README")
    assert (inside.status, inside.kind, inside.project_id) == (sr.OWNED, "target_clone", PROJECT)


@pytest.mark.parametrize("case", ["parent-not-declared", "path-exists", "relative-path",
                                  "unregistered-owner"])
def test_new_target_refuses_what_it_cannot_prove_and_writes_nothing(
        case, tmp_path, w, parent, monkeypatch, capsys):
    """R1b. Catches a door that creates anywhere (a parent that is not checked against
    CONFIG.json), over a directory that already exists, with a
    relative path (run from the declared parent, where the resolved path would be a legal new
    directory: M18), or for a project nobody registered. Each case exits 2 with
    `storage new-target refused: ` on stderr and changes neither the state root nor the
    declared parent nor the stranger's directory. Negative control: the first case's call, with
    the declared parent, succeeds."""
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine\n", encoding="utf-8")
    existing = parent / "taken"
    existing.mkdir()
    (existing / "keep.txt").write_text("mine\n", encoding="utf-8")
    path, owner = {"parent-not-declared": (stranger / "t1", PROJECT),
                   "path-exists": (existing, PROJECT),
                   "relative-path": (Path("t1"), PROJECT),
                   "unregistered-owner": (parent / "t1", "nobody")}[case]
    if case == "relative-path":
        monkeypatch.chdir(parent)               # so a resolved `t1` would be a legal new directory
    before = snapshot(w.registry.state_root, parent, stranger)
    assert new_target(w, monkeypatch, path, owner) == 2
    err = capsys.readouterr().err
    assert err.startswith("storage new-target refused: ")
    if case == "relative-path":
        assert "not an absolute path" in err
    assert snapshot(w.registry.state_root, parent, stranger) == before
    assert new_target(w, monkeypatch, parent / "t1") == 0                    # negative control


# --------------------------------------------------------------------------- #
# R2 -- register-project: a fresh project, by construction
# --------------------------------------------------------------------------- #


def test_register_project_registers_the_made_clone_and_a_run_finds_its_project(
        tmp_path, w, parent, monkeypatch, capsys):
    """R2a. Catches a door that registers without the provenance datum (M7) or under another
    id or root. After new-target, clone,
    register: exit 0; the printed project is `PROJECT.json`; it names the id, the clone as
    `project_root` and a `registered_target` of exactly the owner project, kind, path, origin
    `created` and the inode of the root; `register_project` of the registry's own world
    (the canonical repository) is unchanged; the owner project's index and the clone's
    ownership verdict are byte-for-byte what they were. The product's own rule for a run
    (`storage_ephemeral.project_for_run`) finds the project from a linked worktree of the
    clone, by the name and by the repository alone. Negative control: before the call the
    project is not registered and a worktree of the same clone belongs to no project."""
    root = made(w, monkeypatch, parent)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "wtx", str(wt)],
                   check=True, capture_output=True)
    assert w.registry._project_record(NEW) is None
    assert se.project_for_run(w.registry, "wt", wt)[0] is None
    canon_record = project_file(w, PROJECT).read_bytes()
    index_before = (w.registry.state_root / "projects" / PROJECT / "ROOT_INDEX.json").read_bytes()
    capsys.readouterr()
    assert register(w, monkeypatch, root) == 0
    out = json.loads(capsys.readouterr().out)
    record = json.loads(project_file(w, NEW).read_text(encoding="utf-8"))
    st = os.stat(root)
    assert out == {"storage_root": str(w.registry.state_root), "project": record}
    assert record["project_id"] == NEW and record["project_root"] == str(root)
    assert record["registered_target"] == {"owner_project": PROJECT, "kind": "target_clone",
                                           "path": str(root), "origin": "created",
                                           "st_dev": st.st_dev, "st_ino": st.st_ino}
    assert project_file(w, PROJECT).read_bytes() == canon_record
    assert (w.registry.state_root / "projects" / PROJECT / "ROOT_INDEX.json").read_bytes() \
        == index_before
    assert w.registry.owner_of(root).status == sr.PROTECTED
    assert se.project_for_run(w.registry, NEW, wt) == (NEW, "name")
    assert se.project_for_run(w.registry, "wt", wt) == (NEW, "repository")


def _case(name: str, w: World, parent: Path, monkeypatch, tmp_path: Path) -> tuple[Path | str, str]:
    """(the path handed to register-project, a fragment of the refusal) for a directory the
    registry did not make as a target clone -- or made in a way that proves nothing."""
    if name == "plain-clone-under-the-declared-parent":
        return clone_into(w, parent / "hand"), "not the root of a target clone this registry created"
    if name == "clone-in-an-owned-scratch-directory":
        return w.clone("c1"), "not the root of a target clone this registry created"
    if name == "clone-below-a-created-root":
        assert new_target(w, monkeypatch, parent / "t1") == 0
        return clone_into(w, parent / "t1" / "sub"), "not the root of a target clone this registry created"
    if name == "another-projects-repository":
        return w.canon, "not the root of a target clone this registry created"
    if name == "symlink-to-a-created-root":
        real = made(w, monkeypatch, parent, "t1")
        (parent / "link").symlink_to(real)
        return parent / "link", "not an exact resolved path"
    if name == "created-root-that-is-not-a-repository":
        assert new_target(w, monkeypatch, parent / "t1") == 0
        return parent / "t1", "not a repository root"
    if name == "created-root-holding-a-linked-worktree":
        assert new_target(w, monkeypatch, parent / "t1") == 0
        subprocess.run(["git", "-C", str(w.canon), "worktree", "add", "-q", "--detach",
                        str(parent / "t1")], check=True, capture_output=True)
        return parent / "t1", "not its own plain .git directory"
    if name == "created-root-of-another-kind":
        entry = w.registry.create_external_root(PROJECT, parent / "rb", "runs_base")
        return clone_into(w, Path(entry["path"])), "not the root of a target clone this registry created"
    if name == "entry-registered-by-an-anchor":
        root = made(w, monkeypatch, parent, "t1")
        _rewrite_entry(w, root, origin="legacy_anchor")
        return root, "single CREATED entry made by"
    if name == "symlinked-dot-git":
        assert new_target(w, monkeypatch, parent / "t1") == 0
        outside = clone_into(w, tmp_path / "outside-store")
        (parent / "t1" / ".git").symlink_to(outside / ".git")
        return parent / "t1", "not its own plain .git directory"
    if name == "dot-git-with-another-common-dir":
        root = made(w, monkeypatch, parent, "t1")
        outside = clone_into(w, tmp_path / "outside-store")
        (root / ".git" / "commondir").write_text(str(outside / ".git") + "\n", encoding="utf-8")
        return root, "not its own plain .git directory"
    if name == "shared-clone":
        assert new_target(w, monkeypatch, parent / "t1") == 0
        subprocess.run(["git", "clone", "-q", "--shared", str(w.canon), str(parent / "t1")],
                       check=True, capture_output=True)
        return parent / "t1", "borrows objects"
    if name == "two-entries-for-the-path":
        root = made(w, monkeypatch, parent, "t1")
        entries = w.registry._root_entries(PROJECT)
        entries.append(dict(next(e for e in entries if e["path"] == str(root))))
        w.registry._write_index(PROJECT, "ROOT_INDEX.json", sr.ROOT_INDEX_SCHEMA, entries)
        return root, "single CREATED entry"
    if name == "created-entry-in-registered-state":
        root = made(w, monkeypatch, parent, "t1")
        _rewrite_entry(w, root, state="REGISTERED")
        return root, "single CREATED entry"
    if name == "crashed-create-beside-a-registered-entry":
        root = made(w, monkeypatch, parent, "t1")
        entries = w.registry._root_entries(PROJECT)
        proven = next(e for e in entries if e["path"] == str(root))
        crashed = {"path": str(root), "project_id": PROJECT, "kind": "target_clone",
                   "state": "ABANDONED", "origin": "created", "created_utc": proven["created_utc"]}
        proven.update(origin="legacy_anchor", state="REGISTERED")   # what proves PROTECTED
        w.registry._write_index(PROJECT, "ROOT_INDEX.json", sr.ROOT_INDEX_SCHEMA,
                                [crashed, proven])
        return root, "single CREATED entry"
    if name == "entry-still-pending":
        root = made(w, monkeypatch, parent, "t1")
        _rewrite_entry(w, root, state="PENDING")
        return root, "not the root of a target clone this registry created"
    if name == "path-does-not-exist":
        return parent / "ghost", "cannot be resolved"
    raise AssertionError(name)


def _rewrite_entry(w: World, root: Path, **changes) -> None:
    """Rewrite the index entry of `root` as a damaged or foreign registry might have it (the
    registry's own writer, so the file stays a valid index)."""
    entries = w.registry._root_entries(PROJECT)
    for entry in entries:
        if entry["path"] == str(root):
            entry.update(changes)
    w.registry._write_index(PROJECT, "ROOT_INDEX.json", sr.ROOT_INDEX_SCHEMA, entries)


CASES = ["plain-clone-under-the-declared-parent", "clone-in-an-owned-scratch-directory",
         "clone-below-a-created-root", "another-projects-repository",
         "symlink-to-a-created-root", "created-root-that-is-not-a-repository",
         "created-root-holding-a-linked-worktree", "entry-registered-by-an-anchor",
         "entry-still-pending", "created-root-of-another-kind", "path-does-not-exist",
         "symlinked-dot-git", "dot-git-with-another-common-dir", "shared-clone",
         "two-entries-for-the-path", "created-entry-in-registered-state",
         "crashed-create-beside-a-registered-entry"]


@pytest.mark.parametrize("case", CASES)
def test_register_project_refuses_everything_the_registry_did_not_make_as_a_target_clone(
        case, tmp_path, w, parent, monkeypatch, capsys):
    """R2b. Catches a door that registers a foreign, UNKNOWN or merely similar directory.
    Mutants: M1 (the status is not read: a path inside a created root counts), M2 (the kind is not
    read: the root of a `runs_base`), M3 (the origin of the entry is not read: the
    anchor-registered entry), M5 (the whole git-directory rule: the linked worktree), M12 (the
    symlink clause: a symlinked `.git`), M13 (the common-dir clause: a `.git/commondir` naming
    another store), M14 (the entry count: two entries for the path), M15 (the entry state: a
    `REGISTERED` entry), M16 (the entry not bound to the entry that proves PROTECTED: a crashed
    create beside a registered entry dies with a `KeyError` instead of a refusal), M17 (the
    alternates rule: a `git clone --shared`), M6 (the exact-path rule: the symlink to a created
    root) and M8 (the handler does not ask for a fresh target: the plain clone, the scratch clone
    and the canonical repository register). Each case
    exits 2 with `storage register-project refused: ` and the fragment of its own rule on
    stderr, and the state root, the declared parent and the other projects' roots are
    byte-for-byte what they were: no `projects/hoh-t1/` was made. Negative control: a clone the
    registry made registers, in the same world, right after the refusal."""
    path, fragment = _case(case, w, parent, monkeypatch, tmp_path)
    canon_before = snapshot(w.canon)
    before = snapshot(w.registry.state_root, parent)
    capsys.readouterr()
    assert register(w, monkeypatch, path) == 2
    err = capsys.readouterr().err
    assert err.startswith("storage register-project refused: ") and fragment in err, err
    assert snapshot(w.registry.state_root, parent) == before
    assert snapshot(w.canon) == canon_before
    assert w.registry._project_record(NEW) is None
    good = made(w, monkeypatch, parent, "good")                                # negative control
    assert register(w, monkeypatch, good, "hoh-good") == 0


def test_register_project_refuses_a_git_store_that_leads_into_another_repository(
        tmp_path, w, parent, monkeypatch, capsys):
    """R2d. Catches a fresh-target path that checks only `objects/info/alternates` (M20: the
    store rule of `adopt_project`, `_refuse_git_store_in_layout`, is not applied). A made clone
    whose `.git/objects`, `.git/refs` or `.git/objects/pack` was replaced by a symlink into the
    store of another clone (the original is renamed beside it, nothing is removed) is not a
    whole clone this registry created: a later git write in it would land in the other store.
    Each variant exits 2 with `storage register-project refused: ` and `is a symlink`, and the
    state root, the declared parent and the other clone are byte-for-byte unchanged. Negative
    control: an untouched made clone registers right after the three refusals."""
    other = clone_into(w, tmp_path / "other-store")                   # a repository that is not ours
    for n, (rel, project) in enumerate((("objects", "hoh-a"), ("refs", "hoh-b"),
                                         ("objects/pack", "hoh-c"))):
        root = made(w, monkeypatch, parent, f"t{n}")
        inside = root / ".git" / rel
        inside.rename(inside.with_name(inside.name + ".moved"))      # kept, not removed
        inside.symlink_to(other / ".git" / rel)
        before = snapshot(w.registry.state_root, parent, other)
        capsys.readouterr()
        assert register(w, monkeypatch, root, project) == 2, rel
        err = capsys.readouterr().err
        assert err.startswith("storage register-project refused: ") and "is a symlink" in err, err
        assert snapshot(w.registry.state_root, parent, other) == before
        assert w.registry._project_record(project) is None
    good = made(w, monkeypatch, parent, "good")                      # negative control
    assert register(w, monkeypatch, good, "hoh-good") == 0


def test_register_project_refuses_a_partial_clone(tmp_path, w, parent, monkeypatch, capsys):
    """R2e. Catches a fresh-target path that skips the partial-clone rule (M21: `register_project`
    runs `_refuse_partial_clone` for the fresh path too). A made clone with
    `remote.origin.promisor true` is refused with exit 2, `storage register-project refused: `
    and `partial clone`, state unchanged. Negative control: an ordinary made clone registers right
    after the refusal."""
    root = made(w, monkeypatch, parent)
    subprocess.run(["git", "-C", str(root), "config", "remote.origin.promisor", "true"],
                   check=True, capture_output=True)
    before = snapshot(w.registry.state_root, parent)
    capsys.readouterr()
    assert register(w, monkeypatch, root) == 2
    err = capsys.readouterr().err
    assert err.startswith("storage register-project refused: ") and "partial clone" in err, err
    assert snapshot(w.registry.state_root, parent) == before
    good = made(w, monkeypatch, parent, "good")                      # negative control
    assert register(w, monkeypatch, good, "hoh-good") == 0


def test_a_relative_repo_is_refused_before_the_registry_is_asked(tmp_path, w, parent, monkeypatch, capsys):
    """R2c. Catches a door that resolves a relative `--repo` against the caller's working
    directory (M19) (a directory that happens to be the created root would register). Exit 2,
    `storage register-project refused: ... not an absolute path`, nothing written."""
    root = made(w, monkeypatch, parent)
    monkeypatch.chdir(parent)
    before = snapshot(w.registry.state_root)
    capsys.readouterr()
    assert register(w, monkeypatch, "t1") == 2
    err = capsys.readouterr().err
    assert err.startswith("storage register-project refused: ") and "not an absolute path" in err
    assert snapshot(w.registry.state_root) == before
    assert register(w, monkeypatch, root) == 0                                 # negative control


def test_register_project_is_create_only(tmp_path, w, parent, monkeypatch, capsys):
    """R3. Catches a door that returns an existing record as if it had just registered
    (M4: `register_project` is idempotent by design and would answer exit 0): the same id and
    the same root again, the same root under another id, and another created root under the
    same id are all refused, with `PROJECT.json` of the first registration byte-for-byte
    unchanged and no second project directory. Negative control: the first registration, and
    a second created root under a second id, succeed."""
    first = made(w, monkeypatch, parent, "t1")
    second = made(w, monkeypatch, parent, "t2")
    assert register(w, monkeypatch, first) == 0
    keep = project_file(w, NEW).read_bytes()
    before = snapshot(w.registry.state_root)
    capsys.readouterr()
    for repo, project, fragment in ((first, NEW, "already registered"),
                                    (first, "hoh-other", "equals, lies inside or contains"),
                                    (second, NEW, "already registered")):
        assert register(w, monkeypatch, repo, project) == 2
        err = capsys.readouterr().err
        assert err.startswith("storage register-project refused: ") and fragment in err, err
        assert project_file(w, NEW).read_bytes() == keep
        assert snapshot(w.registry.state_root) == before
    assert w.registry._project_record("hoh-other") is None
    assert register(w, monkeypatch, second, "hoh-t2") == 0                     # negative control


@pytest.mark.parametrize("door_name,extra", [
    ("new-target", ["--parent-project", PROJECT, "--path", "/x/y"]),
    ("register-project", ["--project", NEW, "--repo", "/x/y"]),
])
def test_the_doors_name_their_state_root_and_never_initialise_one(
        door_name, extra, tmp_path, monkeypatch, capsys):
    """R4. Catches a door that falls back to `$HOH_STATE_ROOT` or the default root (M10: the
    environment names an initialised root; the mutant would use it) or that initialises a
    root (a state root is made by `hoh storage init`, by hand). Without `--storage-root` the
    parser exits 2 and names it; with an uninitialised directory the door exits 2 and leaves
    the directory empty. The real registry constructor is used here."""
    empty = tmp_path / "uninit"
    empty.mkdir()
    monkeypatch.setenv("HOH_STATE_ROOT", str(empty))
    with pytest.raises(SystemExit) as stop:
        cli.main(["storage", door_name, *extra])
    assert stop.value.code == 2
    assert "--storage-root" in capsys.readouterr().err
    assert cli.main(["storage", door_name, *extra, "--storage-root", str(empty)]) == 2
    assert capsys.readouterr().err.startswith(f"storage {door_name} refused: ")
    assert os.listdir(empty) == []


# --------------------------------------------------------------------------- #
# R5 -- the point: a run of a project registered this way works under its root
# --------------------------------------------------------------------------- #


def test_a_run_of_a_project_registered_this_way_works_in_workspaces_of_its_registered_root(
        tmp_path, w, parent, monkeypatch, capsys):
    """R5. Catches a registration that does not engage the run wiring (M11: the handler
    answers success and registers nothing), the practice the operator ruled
    on 2026-10-10 (P2-42 reads "of a run of a registered project"; dogfood target clones are
    registered BEFORE a run). One real `hoh run` iteration (the real parser and
    controller, a fake dispatcher; no Herdr) in a linked worktree of a clone that went through
    new-target, clone, register: the output names the project, the session and the project's own
    `tmp` root (`<state>/projects/hoh-t1/tmp/r1`, PROTECTED, a root of the registry), matched by
    name; the admission was asked on behalf of that project for three workspaces, each RELEASED
    and removed; nothing was made beside the arenas. The `ephemeral_roots` document is the
    value `cli._run_in` reads to decide `defer_role_scratch=ephemeral is not None` (strand
    p242r2), so the roles' scratch of this run is what that strand puts in the same
    workspace; this test asserts nothing about role scratch itself. Negative control: the same
    run in a worktree of a clone nobody registered has no root and uses the sibling scratch."""
    root = made(w, monkeypatch, parent, "t1")
    assert register(w, monkeypatch, root) == 0
    capsys.readouterr()
    wt = tmp_path / "wt-registered"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "wtx", str(wt)],
                   check=True, capture_output=True)
    base = tmp_path / "run-registered"
    base.mkdir()
    rc, store, admission, fake = through_the_cli(base, wt, w, monkeypatch, project=NEW,
                                                 fake=Fake(wt))
    out = capsys.readouterr().out
    assert rc == 0, out
    shown = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
    tmp_root = w.registry.state_root / "projects" / NEW / "tmp" / "r1"
    assert shown == [{"project": NEW, "session": "r1", "root": str(tmp_root),
                      "matched_by": "name"}]
    verdict = w.registry.owner_of(tmp_root)
    assert (verdict.status, verdict.project_id) == (sr.PROTECTED, NEW)
    found = records(tmp_root)
    assert len(found) == 3
    assert all(r["state"] == se.RELEASED and r["removed"] is True and r["project_id"] == NEW
               for r in found)
    assert {p for kind, p in admission.projects if kind == "tmp_workspace"} == {NEW}
    assert admission.kinds("tmp_workspace") == 3 and admission.open == {}
    assert beside_the_arenas(store) == []
    assert store.read_state().last_accepted_candidate is not None

    plain = clone_into(w, parent / "unregistered")                              # negative control
    wt2 = tmp_path / "wt-plain"
    subprocess.run(["git", "-C", str(plain), "worktree", "add", "-q", "-b", "wty", str(wt2)],
                   check=True, capture_output=True)
    base2 = tmp_path / "run-plain"
    base2.mkdir()
    rc, store2, admission2, _ = through_the_cli(base2, wt2, w, monkeypatch, project="hoh-t9",
                                                fake=Fake(wt2), run_id="r2")
    out = capsys.readouterr().out
    shown = [d["ephemeral_roots"] for d in documents(out) if "ephemeral_roots" in d]
    assert rc == 0 and shown[0]["state"] == "not registered", out
    assert admission2.kinds("tmp_workspace") == 0


# --------------------------------------------------------------------------- #
# R6 -- an OSError of the registry is a refusal at both doors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("door_name", ["new-target", "register-project"])
def test_an_oserror_of_the_registry_is_a_refusal_at_both_doors(
        door_name, w, parent, monkeypatch, capsys):
    """R6. Catches a door that lets an `OSError` of the registry (a permission or a disk error
    while it writes) end as a traceback: M22 (`register-project` catches only
    `StorageOwnershipError`), M23 (`new-target` does). The registry method is made to raise
    `PermissionError`; the door exits 2 with `storage <door> refused: ` and the error on stderr,
    and the state root and the declared parent are unchanged. Negative control: the same call
    without the fault succeeds."""
    root = made(w, monkeypatch, parent, "t1") if door_name == "register-project" else parent / "t1"

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied (simulated)")

    name = "register_project" if door_name == "register-project" else "create_external_root"
    before = snapshot(w.registry.state_root, parent)
    real = getattr(w.registry, name)
    monkeypatch.setattr(w.registry, name, boom)
    capsys.readouterr()
    rc = register(w, monkeypatch, root) if door_name == "register-project" \
        else new_target(w, monkeypatch, root)
    err = capsys.readouterr().err
    assert rc == 2 and err.startswith(f"storage {door_name} refused: ") \
        and "Permission denied" in err, err
    assert snapshot(w.registry.state_root, parent) == before
    monkeypatch.setattr(w.registry, name, real)                                # negative control
    assert (register(w, monkeypatch, root) if door_name == "register-project"
            else new_target(w, monkeypatch, root)) == 0
