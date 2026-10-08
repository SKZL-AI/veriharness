"""The storage ownership registry (STORAGE-A, P2-38): who owns a path, proven.

Every later storage operation (relocation, pressure admission, park/resume)
asks this module first. Its one answer that permits action is `OWNED`, and it
gives that answer only when records the registry wrote **before** the content
existed prove it. Everything it cannot prove is `UNKNOWN`, and `UNKNOWN` is
no-touch.

Three statuses:

* `OWNED` -- strictly **inside** a scratch directory the registry created
  (`<state_root>/projects/<p>/<kind>/<s>/...`) or strictly inside an external
  root it created itself or registered through a committed migration record.
  **A consumer may act only on `OWNED`.**
* `PROTECTED` -- the registry's own layout: the state root, `projects/`,
  `ledger/`, every project directory, every `<kind>/` and `sessions/<s>/`
  directory, every scratch directory itself, every registered external root
  itself, and every record file. Owned by the registry, never a candidate for
  any storage action.
* `UNKNOWN` -- everything else: a path that does not exist, a sibling or parent
  of an owned root, a path that reaches an owned root only through a symlink,
  a record in the wrong place or not in its index, an unregistered project.

**Trust root.** The anchor chain of a legacy external root ends at the project
repository the operator registered with `register_project`: a root counts
because a committed, reviewed migration record in *that* repository names it.
The registry does not prove that repository's own ownership; registering it is
the operator's act. A project directory that already exists (it predates the
registry) is registered only through `adopt_project`, the one door for it: a
committed `veriharness.storage-project-adopt/1` anchor in that trust root names
the directory, and adoption makes nothing below it `OWNED`.

**The verdict is per path.** `owner_of` judges exactly the one path it is
given, never a tree: a directory can be `OWNED` while an entry in it is
`UNKNOWN` (for example a hard link to a foreign file, a symlink pointing out,
or a bind mount below it). A recursive consumer must therefore ask `owner_of`
for every descendant it acts on, act only on `Ownership.resolved`, and
re-check that object's `st_dev`/`st_ino` on its own descriptor before acting.

**Configuration.** `<state_root>/CONFIG.json` belongs to the operator, who
edits it by hand; it is `PROTECTED`. The registry only reads it
(`read_config`): there is no method that adds an admissible parent.

Nothing here removes a file or a directory. Superseded records are renamed to
`<name>.v<N>.<UTC>`; a crash leaves records behind rather than losing them.

Equivalent mutants
------------------

The mutation review of the second candidate (sreg2-a, M4) left mutants that
no test kills alone. They are listed here, each with the reason why no
behaviour distinguishes it from this code. A test cannot kill such a mutant,
so none is claimed to.

Overlapping pairs. Two checks guard the same condition, so removing either
one alone changes nothing; removing both is killed by the named tests:

* *index / inode* (`_scratch_entry`): a scratch directory that is not in
  `SCRATCH_INDEX.json` has no recorded `st_dev`/`st_ino` to match, and a
  directory whose inode was swapped no longer matches the indexed one; each
  check refuses what the other refuses.
* *lease present / live* (`scratch_dir`): a missing lease is also not live
  (`lease_is_live` of nothing fails and answers not live).
* *exists / EEXIST* (`create_external_root`): the `lexists` refusal before
  the `PENDING` entry and the `FileExistsError` of `os.mkdir` both refuse a
  pre-existing path.
* *PENDING / intact* (`_classify_external`): a `PENDING` entry is skipped by
  state, and it also carries no `st_dev`/`st_ino`, so `_root_intact` fails.
* *identity ceiling / toplevel* (`_repo_identity`): the ceiling stops
  discovery at the root's parent, and the toplevel comparison rejects any
  discovered repository whose top is not the root itself.
* *git dir and common dir path / git-store names* (`adopt_project`): every git
  dir and common dir holds `HEAD` (git confirms no repository without it), and
  `_refuse_git_store_in_layout` runs the same `(st_dev, st_ino)` ancestry test
  on `HEAD` and the other top-level names, whose ancestors include the git dir
  and the common dir themselves. Dropping the git-dir or common-dir part of
  `_refuse_trust_root_in_layout` alone therefore still refuses (with the
  git-store message); the working-tree part has a case of its own.

Behaviourally equivalent:

* N01 (the lexical/resolved same-root comparison in `_owned_refusal` off):
  `_walk_components` checks every symlink and every `..` step against the
  same root, which refuses every path whose given and resolved forms lie in
  different roots.
* N04 (the walk's "a symlink resolves to" check off): a resolution that leaves
  the root either ends outside (refused by the classification of the resolved
  path) or comes back through a symlink that sits outside the root or a `..`
  step outside it (both refused by the remaining walk checks).
* N25 (an unreadable `CONFIG.json` read as empty instead of refused): an empty
  configuration admits no parent, so creation is refused either way. Since
  Revision 9a item 4 `test_mutant_unknown_parent_refused` asserts the refusal
  of a configuration without `sequence` itself, so its present form is killed.
* M23 (the project/session comparison in `close_session` off): the lease must
  equal the one on record, and `_read_lease` accepts a record only when its
  `project_id` and `session_id` equal its own path components.
* M50 (the filesystem-root refusal in `_refuse_unsafe_root` off): `/` is an
  ancestor of every state root and is refused by that rule.
* M53 (the session-id pattern in `_classify_internal` off): such a name then
  reads `PROTECTED` instead of `UNKNOWN`; both are no-touch, and `_check_id`
  never lets the registry create such a session.
* The inode-versus-string mutant of `adopt_project` (whether the trust root,
  its git dir, its common dir or a git-store name lies inside the state root
  or a project directory decided by `Path.is_relative_to` on resolved strings
  instead of `(st_dev, st_ino)` ancestry): without a bind mount both readings
  agree on every path, because a resolved path's components are the directories
  whose pairs are compared. Only a bind mount of a guarded directory gives one
  directory two path strings, and an unprivileged test cannot create one.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

OWNER_SCHEMA = "veriharness.storage-owner/1"
LEGACY_OWNER_SCHEMA = "veriharness.storage-owner/v0-bootstrap"
PROJECT_SCHEMA = "veriharness.storage-project/1"
LEASE_SCHEMA = "veriharness.storage-lease/1"
SCRATCH_RECORD_SCHEMA = "veriharness.storage-scratch-owner/1"
SCRATCH_INDEX_SCHEMA = "veriharness.storage-scratch-index/1"
ROOT_INDEX_SCHEMA = "veriharness.storage-root-index/1"
SEQUENCE_SCHEMA = "veriharness.storage-sequence/1"
LEGACY_ROOTS_SCHEMA = "veriharness.storage-legacy-roots/1"
PROJECT_ADOPT_SCHEMA = "veriharness.storage-project-adopt/1"
CONFIG_SCHEMA = "veriharness.storage-config/1"
OWNER_NAME = "veriharness"

OWNED = "OWNED"
PROTECTED = "PROTECTED"
UNKNOWN = "UNKNOWN"

#: `tmp` (P2-42) is the registered ephemeral temporary root of one session:
#: `<state_root>/projects/<p>/tmp/<s>/`. Its children are the workspaces
#: `hoh.storage_ephemeral` creates, each with a record beside it.
SCRATCH_KINDS = frozenset({"scratch", "runs", "reviews", "mutants", "receipts", "tmp"})
EXTERNAL_KINDS = frozenset({"runs_base", "target_clone", "linked_worktree"})

#: Index states of an external root. Only the last two are ever `OWNED`.
PENDING = "PENDING"
ABANDONED = "ABANDONED"
CREATED = "CREATED"
REGISTERED = "REGISTERED"
_ACCEPTED_ROOT_STATES = (CREATED, REGISTERED)

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")

_OWNER_FILE = "OWNER.json"
_CONFIG_FILE = "CONFIG.json"
_PROJECTS = "projects"
_LEDGER = "ledger"
_LOCK = ".lock"
_PROJECT_FILE = "PROJECT.json"
_SCRATCH_INDEX = "SCRATCH_INDEX.json"
_ROOT_INDEX = "ROOT_INDEX.json"
_SEQUENCE = "SEQUENCE.json"
_SESSIONS = "sessions"
_LEASE = "LEASE.json"
_SCRATCH_RECORD = ".vh-owner.json"
_RUNS_BASE_ARENAS = "_arenas"
_RUN_STATE = "state.json"
_PRESERVE_POLICY = "preserve-policy.json"

_LEGACY_INDEX = "LEGACY_INDEX.json"
LEGACY_INDEX_SCHEMA = "veriharness.storage-legacy-index/1"
#: Internal legacy roots (STORAGE-L): directories inside the state root that
#: predate the registry and are adopted one by one through a committed
#: `veriharness.storage-legacy-roots/1` anchor (`register_legacy_root`).
LEGACY_KINDS = frozenset({"legacy_review"})
#: The state of a legacy index entry replaced by a re-anchoring (kept, never removed).
SUPERSEDED = "SUPERSEDED"

_PROJECT_RECORDS = frozenset({_PROJECT_FILE, _SCRATCH_INDEX, _ROOT_INDEX, _SEQUENCE, _LOCK,
                              _LEGACY_INDEX})

#: The adoption anchor (`adopt_project`): exactly these top-level keys, plus an
#: optional string `note` the code ignores.
_ADOPT_KEYS = frozenset({
    "schema", "project_id", "project_dir", "project_root", "bootstrap_marker", "kinds",
})
_ADOPT_NOTE = "note"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
#: The bootstrap marker of a project directory that predates the registry, and
#: the one versioned name `_rename_versioned(..., "0")` gives it.
_BOOTSTRAP_MARKER = ".vh-owner.json"
_VERSIONED_MARKER = re.compile(r"^\.vh-owner\.json\.v0\.[0-9]{8}T[0-9]{12}Z(\.[0-9]+)?$")
#: The top-level names of a git dir or common dir that git reads objects, refs
#: or configuration through; none may be a symlink or lie in the layout.
_GIT_STORE_NAMES = (
    "objects", "refs", "packed-refs", "HEAD", "info", "worktrees", "commondir", "config",
    "reftable",
)

#: Environment variables that would point `git -C <project_root>` at another
#: repository than the one named, or substitute what it reads there. The
#: ceiling is stripped here and set by the registry itself (`_git(ceiling=)`);
#: the grafts file and the global configuration are not stripped but fixed
#: (`_GIT_FIXED_ENV`), because git falls back to defaults that read them.
_GIT_REDIRECTS = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_REPLACE_REF_BASE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_CONFIG", "GIT_SHALLOW_FILE",
})
#: Prefixes of the numbered `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` pairs.
_GIT_CONFIG_INJECTION = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
#: Always set for every git call: no system and no global (user) config --
#: `~/.gitconfig` and `$XDG_CONFIG_HOME/git/config` both -- no prompt, no
#: grafts (an unset `GIT_GRAFT_FILE` means `.git/info/grafts`, which can make a
#: side commit an "ancestor" of HEAD), no replace objects (`git replace` must
#: not swap an anchor commit or blob), and no lazy fetch from a promisor remote
#: (git 2.44+; older git ignores it, which is why partial clones are refused as
#: trust roots, see `_refuse_partial_clone`).
_GIT_FIXED_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_GRAFT_FILE": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1",
}


#: The one refusal reason that proves a live other holder (STORAGE-D Revision
#: 11 item 1): `open_session` found another live process holding the
#: session's lease. Callers may read a refusal as "busy" only for this reason.
LIVE_HOLDER = "LIVE_HOLDER"


class StorageOwnershipError(RuntimeError):
    """A registry operation was refused: ownership could not be proven.

    `reason` is an explicit attribute, never a message text: `LIVE_HOLDER`
    only where a live other holder was found; the default (None) is never
    `LIVE_HOLDER`, so every other refusal is fail-safe without naming one."""

    def __init__(self, *args: object, reason: str | None = None) -> None:
        super().__init__(*args)
        self.reason = reason


# --------------------------------------------------------------------------- #
# OS readers -- module-level so tests can inject their own
# --------------------------------------------------------------------------- #


def _read_boot_id() -> str:
    """The current boot id (`/proc/sys/kernel/random/boot_id`)."""
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _pid_start_time(pid: int) -> int | None:
    """Field 22 (`starttime`) of `/proc/<pid>/stat`, or None if unreadable."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return _parse_start_time(raw)


def _parse_start_time(raw: str) -> int | None:
    """Field 22 of a `/proc/<pid>/stat` line.

    The command name (field 2) may hold spaces and parentheses, so the fields
    are counted after the **last** `)`: what follows starts at field 3.
    """
    if ")" not in raw:
        return None
    tail = raw[raw.rfind(")") + 1:].split()
    try:
        return int(tail[22 - 3])
    except (IndexError, ValueError):
        return None


def _pid_exists(pid: int) -> bool:
    """Whether a process with this pid exists (its `/proc` entry is there)."""
    try:
        return int(pid) > 0 and os.path.isdir(f"/proc/{int(pid)}")
    except (TypeError, ValueError):
        return False


def _st_dev(path: Path) -> int:
    return os.stat(path).st_dev


def _is_mount(path: Path) -> bool:
    """True if `path` is a mount point or lies on another device than its parent."""
    path = Path(path)
    if os.path.ismount(path):
        return True
    try:
        return _st_dev(path) != _st_dev(path.parent)
    except OSError:
        return True


_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


def _mount_id_from_mountinfo(text: str, path: Path) -> int | None:
    """The mount id of `path` according to a `/proc/<pid>/mountinfo` text.

    The mount is the one with the longest mount point that `path` lies at or
    under (by path components); among equal mount points the **last** line
    wins, because a later mount over the same point hides the earlier one.
    Mount points are octal-escaped (`\\040` for a space) in that file.
    """
    best: int | None = None
    best_depth = -1
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            mount_id = int(fields[0])
        except ValueError:
            continue
        point = Path(_MOUNTINFO_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]))
        if not point.is_absolute() or not (path == point or path.is_relative_to(point)):
            continue
        if len(point.parts) >= best_depth:
            best, best_depth = mount_id, len(point.parts)
    return best


def _mount_id(path: Path) -> int | None:
    """The id of the mount `path` lies on (`/proc/self/mountinfo`), or None if
    it cannot be read -- and None proves nothing (`owner_of` answers UNKNOWN).

    A bind mount of a foreign directory onto the same filesystem keeps
    `st_dev` but gets a mount id of its own, so this catches what `st_dev`
    cannot. `path` is expected in resolved form.
    """
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _mount_id_from_mountinfo(text, Path(path))


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    """The one record writer: temp file in the same directory, fsync,
    `os.replace`, fsync of the directory.

    The temp name is created exclusively, so two writers never share one. A
    crash leaves a stray `.<name>.tmp.*` behind (never removed here); it is
    not a record and `owner_of` never reads it.
    """
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _read_record(path: Path) -> dict[str, Any] | None:
    """A JSON object from a regular file, or None (missing, symlink, unreadable,
    malformed). Never raises: an unreadable record proves nothing."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _versioned_target(path: Path, version: str) -> Path:
    """A name `<path>.v<version>.<UTC>` that does not exist yet."""
    stamp = _utc_stamp()
    candidate = path.with_name(f"{path.name}.v{version}.{stamp}")
    k = 1
    while os.path.lexists(candidate):
        candidate = path.with_name(f"{path.name}.v{version}.{stamp}.{k}")
        k += 1
    return candidate


def _rename_versioned(path: Path, version: str) -> Path:
    """Keep `path` byte-for-byte under a versioned name (a rename, never a removal)."""
    target = _versioned_target(path, version)
    os.rename(path, target)
    _fsync_dir(path.parent)
    return target


def _next_version(path: Path) -> str:
    pattern = re.compile(re.escape(path.name) + r"\.v(\d+)\.")
    used = [0]
    try:
        for name in os.listdir(path.parent):
            hit = pattern.match(name)
            if hit:
                used.append(int(hit.group(1)))
    except OSError:
        pass
    return str(max(used) + 1)


#: The `lstat` of the legacy walk (a seam: a test makes one entry report another uid).
_legacy_lstat = os.lstat


def _entry(rel: str, kind: str, mode: int, size: int, sha256: str | None = None, *,
           link: str | None = None) -> dict[str, Any]:
    """One entry in the form of `storage_tiers.Entry.__dict__`."""
    return {"rel": rel, "type": kind, "mode": mode, "size": size, "sha256": sha256,
            "link_target": link}


def _entries_digest(entries: list[dict[str, Any]]) -> str:
    """`storage_tiers.entries_digest` of `entries` (the same canonical JSON)."""
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, default=str).encode("ascii")).hexdigest()


def _sha256_path(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        digest = hashlib.sha256()
        for chunk in iter(lambda: os.read(fd, 1 << 20), b""):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


#: `verified_relocation`'s quarantine name `<name>.vh-quarantine.<UTC>[...]`.
_QUARANTINE = re.compile(r"^(?P<name>.+)\.vh-quarantine\.[0-9]{8}T[0-9]{6}Z.*$")


def _bound_name(name: str) -> str:
    """The legacy child a direct entry of a legacy root stands for: itself,
    or the child that `VerifiedRelocation` renamed into quarantine (the
    quarantine of a bound child is that child, mid-relocation)."""
    match = _QUARANTINE.match(name)
    return match.group("name") if match else name


def _same_inode(path: Path, recorded: Any) -> bool:
    """`path` (not followed) is still the `[st_dev, st_ino]` recorded for it."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return isinstance(recorded, list) and [st.st_dev, st.st_ino] == recorded


def _non_empty(directory: Path) -> bool:
    with os.scandir(directory) as entries:
        return any(True for _ in entries)


def _lexical(path: os.PathLike[str] | str) -> Path:
    """Absolute and normalised, **without** resolving symlinks."""
    return Path(os.path.normpath(os.path.abspath(os.fspath(path))))


def _is_temp_of(name: str, record: str) -> bool:
    """`name` is a temp file `_atomic_write_json` makes for `record` (or the
    bare `<record>.tmp.*` form): a leftover of a crashed write, never content."""
    return name.startswith(f".{record}.tmp.") or name.startswith(f"{record}.tmp.")


def _is_record_or_version(name: str, record: str) -> bool:
    """The record itself, a versioned copy (`<record>.v<N>.<UTC>`) or a temp."""
    return name == record or name.startswith(record + ".v") or _is_temp_of(name, record)


def _resolves_to(raw: str, target: Path) -> bool:
    """Whether the path string `raw` resolves (symlinks followed, `..`
    folded, missing components kept) to `target`. Used only to name an
    invalid configuration entry in a refusal; it never admits anything."""
    try:
        return os.path.isabs(raw) and Path(raw).resolve(strict=False) == target
    except (OSError, RuntimeError, ValueError):
        return False


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _LeftOwnedRoot(Exception):
    """Raised by `_walk_components` when a path leaves the owned root."""


_LINK_DONE = object()
_MAX_LINK_HOPS = 40


def _walk_components(path: os.PathLike[str] | str, root: Path) -> Path:
    """Resolve `path` the way the kernel does, one component at a time, and
    raise `_LeftOwnedRoot` if the walk leaves `root` on the way.

    Checked for **every** step, not only for the end points: where each
    symlink met on the way sits (its resolved parent plus its name), where
    each symlink resolves to, and -- once the walk has been inside `root` --
    every `..` step. Each of them must lie at or below `root`. A path that
    reaches its target through a foreign directory and comes back (`out ->
    /foreign`, `/foreign/f -> <root>/target`) therefore fails even though
    both its given and its resolved form lie inside `root`. Returns the
    resolved path; OS errors propagate.
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    queue: deque[object] = deque(raw.split("/"))
    current = Path("/")
    entered = False
    hops = 0

    def require_inside(where: Path, what: str) -> None:
        if not (where == root or where.is_relative_to(root)):
            raise _LeftOwnedRoot(f"{what} {where} lies outside the owned root {root}")

    while queue:
        part = queue.popleft()
        if part is _LINK_DONE:
            require_inside(current, "a symlink on the way resolves to")
            continue
        assert isinstance(part, str)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            if entered:
                require_inside(current, "a '..' step leads to")
            continue
        candidate = current / part
        st = os.lstat(candidate)
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > _MAX_LINK_HOPS:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", str(candidate))
            require_inside(candidate, "a symlink on the way sits at")
            target = os.readlink(candidate)
            if target.startswith("/"):
                current = Path("/")
            queue.extendleft(reversed([*target.split("/"), _LINK_DONE]))
            continue
        current = candidate
        if current == root or current.is_relative_to(root):
            entered = True
    return current


def _check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise StorageOwnershipError(f"invalid {what} {value!r}: must match {_ID.pattern}")
    return value


# --------------------------------------------------------------------------- #
# Public value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionLease:
    """A session's claim on its project, recorded at
    `projects/<p>/sessions/<s>/LEASE.json`.

    Live only while the pid exists, the pid's start time equals the recorded
    one (a reused pid is a different process) and the boot id is the current
    boot's.

    `token` is the holder's secret: `open_session` draws it, returns it in
    this object and stores only its SHA-256 (`token_sha256`) in `LEASE.json`.
    Only the holder can therefore close the lease; a `SessionLease` rebuilt
    from the public record has no token and is refused. The token takes no
    part in `repr` or in equality.
    """

    project_id: str
    session_id: str
    pid: int
    pid_start: int
    boot_id: str
    since_utc: str
    sequence: int
    token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Ownership:
    """The answer of `owner_of`. Act only when `status == "OWNED"`.

    `proof` is the chain of record files that proves the answer. `resolved`
    is the resolved path that was judged, and **a consumer acts only on
    `resolved`**, never on the path it passed in: the given name may run
    through symlinks whose directory entries are not the registry's. `st_dev`
    and `st_ino` identify the resolved object, so a later operation re-checks
    (`fstat` on its own descriptor of `resolved`) that it acts on the same
    object and not on something swapped in since.
    """

    status: str
    project_id: str | None = None
    session_id: str | None = None
    kind: str | None = None
    root: Path | None = None
    proof: tuple[str, ...] = ()
    st_dev: int | None = None
    st_ino: int | None = None
    reason: str = ""
    resolved: Path | None = None


@dataclass(frozen=True)
class StorageConfig:
    """What `StorageRegistry.read_config` read from the operator's
    `CONFIG.json`: the valid admissible parents, and every listed entry that
    was not admitted with the reason (`{"path", "reason"}`)."""

    sequence: int
    admissible_external_parents: tuple[Path, ...] = ()
    invalid_entries: tuple[dict[str, str], ...] = ()


def _unknown(reason: str) -> Ownership:
    return Ownership(status=UNKNOWN, reason=reason)


@dataclass(frozen=True)
class _Verdict:
    ownership: Ownership
    root_dev: int | None = None
    root_mount: int | None = None


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


class StorageRegistry:
    """Fail-closed ownership registry rooted at `state_root`.

    Layout::

        <state_root>/OWNER.json                       written before any child
        <state_root>/CONFIG.json                      operator configuration (read only)
        <state_root>/ledger/                          admission ledger (PROTECTED)
        <state_root>/projects/.lock                   registry-wide writer lock
        <state_root>/projects/<p>/PROJECT.json
        <state_root>/projects/<p>/.lock               project writer lock
        <state_root>/projects/<p>/SEQUENCE.json       last sequence handed out
        <state_root>/projects/<p>/SCRATCH_INDEX.json  every scratch dir created
        <state_root>/projects/<p>/ROOT_INDEX.json     every external root
        <state_root>/projects/<p>/sessions/<s>/LEASE.json
        <state_root>/projects/<p>/<kind>/<s>/.vh-owner.json

    **Consumers may act only on `OWNED`.** `PROTECTED` and `UNKNOWN` are both
    no-touch. The anchor chain for legacy external roots ends at the project
    repository the operator registered (`register_project`); the registry does
    not prove that repository's own ownership. An existing project directory
    is registered only through `adopt_project`, never by `register_project`.

    The OS readers are injectable so tests never depend on real processes,
    boots or mounts: `boot_id_reader() -> str`, `pid_start_reader(pid) -> int
    | None`, `pid_exists(pid) -> bool`, `mount_check(path) -> bool`,
    `mount_id_reader(path) -> int | None` (None is UNKNOWN) and
    `st_dev_reader(path) -> int`.
    """

    def __init__(
        self,
        state_root: os.PathLike[str] | str,
        *,
        forbidden_roots: Iterable[os.PathLike[str] | str] | None = None,
        boot_id_reader: Callable[[], str] = _read_boot_id,
        pid_start_reader: Callable[[int], int | None] = _pid_start_time,
        pid_exists: Callable[[int], bool] = _pid_exists,
        mount_check: Callable[[Path], bool] = _is_mount,
        mount_id_reader: Callable[[Path], int | None] = _mount_id,
        st_dev_reader: Callable[[Path], int] = _st_dev,
    ) -> None:
        self.state_root = Path(state_root).resolve(strict=False)
        if forbidden_roots is None:
            home = os.environ.get("HOME")
            forbidden_roots = [home] if home and os.path.isabs(home) else []
        self.forbidden_roots = tuple(Path(p).resolve(strict=False) for p in forbidden_roots)
        self._boot_id_reader = boot_id_reader
        self._pid_start_reader = pid_start_reader
        self._pid_exists = pid_exists
        self._mount_check = mount_check
        self._mount_id_reader = mount_id_reader
        self._st_dev_reader = st_dev_reader

    # ------------------------------------------------------------------ paths

    @property
    def _owner_path(self) -> Path:
        return self.state_root / _OWNER_FILE

    @property
    def _config_path(self) -> Path:
        return self.state_root / _CONFIG_FILE

    @property
    def _projects(self) -> Path:
        return self.state_root / _PROJECTS

    def _project_dir(self, project_id: str) -> Path:
        return self._projects / project_id

    def _lease_path(self, project_id: str, session_id: str) -> Path:
        return self._project_dir(project_id) / _SESSIONS / session_id / _LEASE

    # ------------------------------------------------------------------ init

    def init(self) -> dict[str, Any]:
        """Create the state root and write `OWNER.json` before any child.

        Accepted: an absent or empty directory; an existing `/1` record of
        this owner (idempotent); the legacy `v0-bootstrap` record, which is
        kept byte-for-byte as `OWNER.json.v0.<UTC>` (a rename) before the `/1`
        record is written. Refused, never adopted: any other `OWNER.json`, and
        a non-empty directory without one.
        """
        root = self.state_root
        if not root.exists():
            try:
                os.mkdir(root)
            except OSError as exc:
                raise StorageOwnershipError(f"cannot create state root {root}: {exc}") from exc
        if root.is_symlink() or not root.is_dir():
            raise StorageOwnershipError(f"state root {root} is not a plain directory")

        superseded = None
        if os.path.lexists(self._owner_path):
            existing = _read_record(self._owner_path)
            schema = existing.get("schema") if existing else None
            if existing and schema == OWNER_SCHEMA and existing.get("owner") == OWNER_NAME:
                self._ensure_layout()
                return existing
            if existing and schema == LEGACY_OWNER_SCHEMA:
                superseded = _rename_versioned(self._owner_path, "0").name
            else:
                raise StorageOwnershipError(
                    f"{self._owner_path} is not a VeriHarness owner record this "
                    f"registry may adopt (schema {schema!r}); refusing"
                )
        elif _non_empty(root):
            raise StorageOwnershipError(
                f"state root {root} is not empty and has no {_OWNER_FILE}; refusing to adopt it"
            )

        record: dict[str, Any] = {
            "schema": OWNER_SCHEMA,
            "owner": OWNER_NAME,
            "created_utc": _utc_iso(),
            "layout": {
                "owner": _OWNER_FILE,
                "config": _CONFIG_FILE,
                "projects": _PROJECTS + "/",
                "ledger": _LEDGER + "/",
                "project": f"{_PROJECTS}/<project_id>/{_PROJECT_FILE}",
                "lease": f"{_PROJECTS}/<project_id>/{_SESSIONS}/<session_id>/{_LEASE}",
                "scratch": f"{_PROJECTS}/<project_id>/<kind>/<session_id>/{_SCRATCH_RECORD}",
            },
            "sequence": 1,
        }
        if superseded:
            record["supersedes"] = superseded
        _atomic_write_json(self._owner_path, record)
        self._ensure_layout()
        return record

    def _ensure_layout(self) -> None:
        for name in (_PROJECTS, _LEDGER):
            path = self.state_root / name
            if os.path.lexists(path):
                if path.is_symlink() or not path.is_dir():
                    raise StorageOwnershipError(f"{path} is not a plain directory; refusing")
                continue
            os.mkdir(path)
        _fsync_dir(self.state_root)

    def _owner_valid(self) -> bool:
        record = _read_record(self._owner_path)
        return bool(
            record
            and record.get("schema") == OWNER_SCHEMA
            and record.get("owner") == OWNER_NAME
        )

    def _require_init(self) -> None:
        if not self._owner_valid():
            raise StorageOwnershipError(f"{self.state_root} is not an initialised registry")
        for name in (_PROJECTS, _LEDGER):
            path = self.state_root / name
            if path.is_symlink() or not path.is_dir():
                raise StorageOwnershipError(f"{path} is missing or not a plain directory")

    # ------------------------------------------------------------------ locks

    @contextmanager
    def _locked(self, project_id: str | None = None) -> Iterator[None]:
        """Registry-wide lock, then the project lock: one order, no deadlock.

        The registry-wide lock serialises the cross-project overlap check; the
        project lock (`projects/<p>/.lock`) serialises the project's writers.
        """
        fds: list[int] = []
        try:
            for directory in [self._projects] + (
                [self._project_dir(project_id)] if project_id else []
            ):
                fd = os.open(directory / _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
                fds.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            for fd in reversed(fds):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _sequence_floor(self, project_id: str) -> int:
        """The highest integer `sequence` in any record of the project, 0 if
        there is none.

        Read from every record the project keeps and every versioned copy of
        each (`<name>.v<N>.<UTC>`): `PROJECT.json`, `SCRATCH_INDEX.json`,
        `ROOT_INDEX.json`, `SEQUENCE.json`, each `sessions/<s>/LEASE.json` and
        each scratch record `<kind>/<s>/.vh-owner.json`. (External roots keep
        no record of their own; their entries live in `ROOT_INDEX.json`.)
        Temp files are not records and are skipped; so are symlinks. A
        directory that exists but cannot be listed is refused: a floor read
        from part of the records could be too low.
        """
        pdir = self._project_dir(project_id)
        highest = 0

        def scan(directory: Path, names: Iterable[str]) -> None:
            nonlocal highest
            try:
                listing = os.listdir(directory)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StorageOwnershipError(
                    f"cannot list {directory} to find the highest sequence: {exc}"
                ) from exc
            for entry in listing:
                if not any(_is_record_or_version(entry, n) and not _is_temp_of(entry, n)
                           for n in names):
                    continue
                seen = (_read_record(directory / entry) or {}).get("sequence")
                if isinstance(seen, int) and not isinstance(seen, bool):
                    highest = max(highest, seen)

        def plain_subdirs(directory: Path) -> list[Path]:
            try:
                with os.scandir(directory) as entries:
                    return [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise StorageOwnershipError(
                    f"cannot list {directory} to find the highest sequence: {exc}"
                ) from exc

        scan(pdir, (_PROJECT_FILE, _SCRATCH_INDEX, _ROOT_INDEX, _SEQUENCE))
        for session in plain_subdirs(pdir / _SESSIONS):
            scan(session, (_LEASE,))
        for kind in sorted(SCRATCH_KINDS):
            for directory in plain_subdirs(pdir / kind):
                scan(directory, (_SCRATCH_RECORD,))
        return highest

    def _next_sequence(self, project_id: str) -> int:
        """Hand out the next sequence of this project. Caller holds the lock.

        The sequence never goes backwards. Without `SEQUENCE.json` (a new
        project, or a counter renamed away) the next sequence starts above the
        highest sequence in any record of the project (`_sequence_floor`). A
        counter that exists but is malformed -- unreadable, not an integer,
        negative, or below that floor (it went backwards) -- is refused rather
        than restarted: restarting would let a sequence shrink, which is the
        one thing it must never do.
        """
        pdir = self._project_dir(project_id)
        path = pdir / _SEQUENCE
        floor = self._sequence_floor(project_id)
        current = floor
        if os.path.lexists(path):
            record = _read_record(path)
            value = record.get("sequence") if record else None
            if (
                not isinstance(value, int) or isinstance(value, bool)
                or value < 0 or value < floor
            ):
                raise StorageOwnershipError(
                    f"sequence counter {path} is unreadable or went backwards "
                    f"(below {floor}, the highest sequence on record); refusing"
                )
            current = value
        nxt = current + 1
        _atomic_write_json(path, {"schema": SEQUENCE_SCHEMA, "sequence": nxt})
        return nxt

    # ------------------------------------------------------------------ projects

    def _project_record(self, project_id: str) -> dict[str, Any] | None:
        if not _ID.match(project_id):
            return None
        pdir = self._project_dir(project_id)
        if pdir.is_symlink():
            return None
        record = _read_record(pdir / _PROJECT_FILE)
        if (
            not record
            or record.get("schema") != PROJECT_SCHEMA
            or record.get("project_id") != project_id
            or not isinstance(record.get("project_root"), str)
        ):
            return None
        return record

    def _registered_projects(self) -> list[str]:
        try:
            names = sorted(os.listdir(self._projects))
        except OSError:
            return []
        return [name for name in names if self._project_record(name) is not None]

    def _require_project(self, project_id: str) -> dict[str, Any]:
        _check_id(project_id, "project id")
        record = self._project_record(project_id)
        if record is None:
            raise StorageOwnershipError(f"project {project_id!r} is not registered")
        return record

    def register_project(
        self, project_id: str, project_root: os.PathLike[str] | str
    ) -> dict[str, Any]:
        """Record `projects/<p>/PROJECT.json` for an existing directory.

        Idempotent for the same id and root; a different root for the same id
        is refused. **One root, one project:** a root that equals, lies inside
        or contains another project's root (by resolved path) is refused, and
        so is a repository root whose repository is another project's -- the
        same resolved `git rev-parse --git-common-dir`, e.g. a linked worktree
        of it. A repository identity counts only for a repository root:
        `--show-toplevel`, run with `GIT_CEILING_DIRECTORIES` at the root's
        parent, must be the root itself; any other directory is compared by
        path alone and git discovery never reaches an enclosing repository. A
        separate clone is a different repository and may be registered. A
        partial clone is refused as a trust root (`_refuse_partial_clone`).
        """
        self._require_init()
        _check_id(project_id, "project id")
        try:
            root = Path(project_root).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise StorageOwnershipError(f"project root {project_root} does not exist") from exc
        if not root.is_dir():
            raise StorageOwnershipError(f"project root {root} is not a directory")
        identity = self._repo_identity(root)
        if identity is not None:
            self._refuse_partial_clone(root)

        with self._locked():
            self._refuse_other_projects_root(project_id, root, identity)
            pdir = self._project_dir(project_id)
            if os.path.lexists(pdir):
                existing = self._project_record(project_id)
                if existing is None:
                    raise StorageOwnershipError(
                        f"{pdir} exists without a valid {_PROJECT_FILE}; refusing to adopt it"
                    )
                if existing["project_root"] != str(root):
                    raise StorageOwnershipError(
                        f"project {project_id!r} is registered with root "
                        f"{existing['project_root']}, not {root}"
                    )
                return existing
            os.mkdir(pdir)
            _fsync_dir(self._projects)
            with self._locked_project_only(project_id):
                record = {
                    "schema": PROJECT_SCHEMA,
                    "project_id": project_id,
                    "project_root": str(root),
                    "created_utc": _utc_iso(),
                    "archive_namespace": project_id,
                    "sequence": self._next_sequence(project_id),
                }
                _atomic_write_json(pdir / _PROJECT_FILE, record)
            return record

    def _refuse_other_projects_root(self, project_id: str, root: Path,
                                    identity: Path | None) -> None:
        """One root, one project: refuse `root` if it equals, lies inside or
        contains another registered project's root, or if it is a repository
        root (`identity` not None) of another project's repository. The caller
        holds `_locked()`. Extracted unchanged from `register_project`."""
        for other in self._registered_projects():
            record = self._project_record(other)
            if other == project_id or record is None:
                continue
            other_root = Path(record["project_root"])
            if root == other_root or root.is_relative_to(other_root) \
                    or other_root.is_relative_to(root):
                raise StorageOwnershipError(
                    f"{root} equals, lies inside or contains {other_root}, the root of "
                    f"project {other!r}; refusing to register it for {project_id!r}"
                )
            if identity is not None and os.path.isdir(other_root) \
                    and self._repo_identity(other_root) == identity:
                raise StorageOwnershipError(
                    f"{root} is a checkout of the repository {identity} of project "
                    f"{other!r}; refusing to register it for {project_id!r}"
                )

    @contextmanager
    def _locked_project_only(self, project_id: str) -> Iterator[None]:
        fd = os.open(
            self._project_dir(project_id) / _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ------------------------------------------------------------------ adoption

    def adopt_project(self, project_id: str, project_root: os.PathLike[str] | str,
                      anchor: dict[str, str]) -> dict[str, Any]:
        """Register the **existing** directory `projects/<p>/` -- one that
        predates the registry and holds only its bootstrap marker and kind
        directories -- through a committed adoption anchor. The only door for
        an existing project directory: `register_project` refuses it.

        `anchor = {"commit", "file"}` names a JSON object committed in the
        trust root `project_root` at an ancestor of its `HEAD`, with schema
        `veriharness.storage-project-adopt/1` and exactly the keys `schema`,
        `project_id`, `project_dir`, `project_root`, `bootstrap_marker`
        (`{"name": ".vh-owner.json", "sha256": <hex>}`) and `kinds` (plus an
        optional string `note`). Under the registry-wide lock, in this order,
        the first failing rule refuses:

        * the registry is initialised and `project_id` is valid;
        * `project_root` is a repository root with a working tree;
        * the trust root, its git dir and its common dir lie outside the state
          root and every project directory, and do not contain the state root,
          by `(st_dev, st_ino)` ancestry (`_refuse_trust_root_in_layout`);
        * the git store is not reached through a symlink or a guarded
          directory, and borrows no objects (`_refuse_git_store_in_layout`);
        * every configuration file git reads for the trust root
          (`git config --list --show-origin --includes`) is readable and lies
          outside the layout by the same ancestry test;
        * it is not a partial clone, and not another project's root
          (`_refuse_other_projects_root`);
        * the anchor is committed (`_read_committed_json`) and names exactly
          this project, this directory (`project_dir`, compared as a string)
          and this trust root (`project_root`, resolved).

        A valid `PROJECT.json` already present is returned unchanged if it
        records this root and this anchor (idempotent), else refused; nothing
        is written either way. Otherwise the directory is re-checked on disk
        (`_adoptable_layout`), the marker is renamed to `.vh-owner.json.v0.<UTC>`
        (never removed), and `PROJECT.json` is written last, through
        `_atomic_write_json`: it is the commit point, and a crash before it
        leaves only leftovers a re-run accepts. Nothing below the directory
        becomes `OWNED`: no index is written and no kind directory is entered.
        """
        self._require_init()
        _check_id(project_id, "project id")
        with self._locked():
            self._require_init()
            try:
                root = Path(project_root).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise StorageOwnershipError(
                    f"trust root {project_root} does not exist") from exc
            if not root.is_dir():
                raise StorageOwnershipError(f"trust root {root} is not a directory")
            identity = self._repo_identity(root)
            if identity is None:
                raise StorageOwnershipError(
                    f"trust root {root} is not a repository root with a working tree; "
                    f"refusing it as a trust root"
                )
            found = self._git(root, "rev-parse", "--absolute-git-dir", ceiling=root.parent)
            try:
                if found.returncode != 0 or not found.stdout.strip():
                    raise OSError(found.stderr.strip())
                git_dir = Path(found.stdout.strip()).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise StorageOwnershipError(
                    f"cannot read the git dir of {root} ({exc}); refusing") from exc
            self._refuse_trust_root_in_layout(root, git_dir, identity)
            self._refuse_git_store_in_layout(git_dir, identity)

            listed = self._git(root, "config", "--list", "--show-origin", "--includes", "-z",
                               ceiling=root.parent)
            if listed.returncode != 0:
                raise StorageOwnershipError(
                    f"cannot list the configuration of {root} "
                    f"({listed.stderr.strip()}); refusing")
            for origin in sorted(set(listed.stdout.split("\0")[0::2]) - {""}):
                if not origin.startswith("file:"):
                    raise StorageOwnershipError(
                        f"configuration of {root} comes from {origin!r}, not from a file; "
                        f"refusing")
                try:
                    source = (root / origin[len("file:"):]).resolve(strict=True)
                    os.close(os.open(source, os.O_RDONLY | os.O_NONBLOCK))
                except (OSError, RuntimeError) as exc:
                    raise StorageOwnershipError(
                        f"configuration file {origin[len('file:'):]} of {root} cannot be "
                        f"read ({exc}); refusing") from exc
                self._refuse_trust_root_in_layout(source, source, source)

            self._refuse_partial_clone(root)
            self._refuse_other_projects_root(project_id, root, identity)
            record, full_commit, file = self._read_committed_json(root, anchor)
            verified = {"commit": full_commit, "file": file}
            spec = f"{full_commit}:{file}"
            pdir = self._project_dir(project_id)

            if not isinstance(record, dict):
                raise StorageOwnershipError(f"anchor {spec} is not a JSON object")
            keys = set(record) - {_ADOPT_NOTE}
            if keys != _ADOPT_KEYS:
                raise StorageOwnershipError(
                    f"anchor {spec} must carry exactly the keys {sorted(_ADOPT_KEYS)} (and "
                    f"an optional note); missing {sorted(_ADOPT_KEYS - keys)}, extra "
                    f"{sorted(keys - _ADOPT_KEYS)}")
            if not isinstance(record.get(_ADOPT_NOTE, ""), str):
                raise StorageOwnershipError(f"anchor {spec}: note is not a string")
            if record["schema"] != PROJECT_ADOPT_SCHEMA:
                raise StorageOwnershipError(
                    f"anchor {spec} does not carry schema {PROJECT_ADOPT_SCHEMA} "
                    f"(found {record['schema']!r})")
            if record["project_id"] != project_id:
                raise StorageOwnershipError(
                    f"anchor {spec} names project_id {record['project_id']!r}, not "
                    f"{project_id!r}; an anchor belongs to one project")
            if record["project_dir"] != str(pdir):
                raise StorageOwnershipError(
                    f"anchor {spec} names project_dir {record['project_dir']!r}, not exactly "
                    f"{str(pdir)!r}")
            if record["project_root"] != str(root):
                raise StorageOwnershipError(
                    f"anchor {spec} names project_root {record['project_root']!r}, not the "
                    f"trust root {str(root)!r}")
            marker = record["bootstrap_marker"]
            if (
                not isinstance(marker, dict) or set(marker) != {"name", "sha256"}
                or marker["name"] != _BOOTSTRAP_MARKER
                or not isinstance(marker["sha256"], str)
                or not _SHA256_HEX.match(marker["sha256"])
            ):
                raise StorageOwnershipError(
                    f"anchor {spec}: bootstrap_marker must be exactly {{'name': "
                    f"'{_BOOTSTRAP_MARKER}', 'sha256': <64 lowercase hex>}}, found {marker!r}")
            kinds = record["kinds"]
            if (
                not isinstance(kinds, list)
                or not all(isinstance(k, str) and k in SCRATCH_KINDS for k in kinds)
                or len(set(kinds)) != len(kinds)
            ):
                raise StorageOwnershipError(
                    f"anchor {spec}: kinds must be a list of distinct names from "
                    f"{sorted(SCRATCH_KINDS)}, found {kinds!r}")

            existing = self._project_record(project_id)
            if existing is not None:
                if existing["project_root"] != str(root):
                    raise StorageOwnershipError(
                        f"project {project_id!r} is registered with root "
                        f"{existing['project_root']}, not {root}; refusing to adopt it")
                adopted = existing.get("adopted")
                if not isinstance(adopted, dict):
                    raise StorageOwnershipError(
                        f"project {project_id!r} is registered without adoption; refusing "
                        f"to adopt it")
                if adopted.get("anchor") != verified:
                    raise StorageOwnershipError(
                        f"project {project_id!r} was adopted with anchor "
                        f"{adopted.get('anchor')!r}, not {verified!r}; refusing")
                return existing

            found_marker = self._adoptable_layout(project_id, record)
            if found_marker.name == _BOOTSTRAP_MARKER:
                versioned = _rename_versioned(found_marker, "0").name
            else:
                versioned = found_marker.name
            with self._locked_project_only(project_id):
                adopted_record = {
                    "schema": PROJECT_SCHEMA,
                    "project_id": project_id,
                    "project_root": str(root),
                    "created_utc": _utc_iso(),
                    "archive_namespace": project_id,
                    "sequence": self._next_sequence(project_id),
                    "adopted": {
                        "anchor": verified,
                        "bootstrap_marker": {"sha256": marker["sha256"],
                                             "versioned_as": versioned},
                        "kinds": sorted(kinds),
                    },
                }
                _atomic_write_json(pdir / _PROJECT_FILE, adopted_record)
            return adopted_record

    def _refuse_trust_root_in_layout(self, root: Path, git_dir: Path, common_dir: Path
                                     ) -> None:
        """Refuse unless each of the three resolved paths lies outside the
        registry's layout and does not contain the state root -- decided by
        `(st_dev, st_ino)` ancestry, never by comparing path strings.

        The guarded set is the pair of the state root and of every entry of
        `projects/` that `lstat`s as a directory (registered or not). A path
        lies inside when the pair of the path or of any of its ancestors is
        guarded; it contains the state root when its own pair is the state
        root's or one of the state root's ancestors'. Any `lstat` failure
        refuses. (The git-store and configuration rules apply this same test
        to a single path by passing it three times.)
        """
        def pair(path: Path, what: Path) -> tuple[int, int]:
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise StorageOwnershipError(
                    f"cannot lstat {path} to place {what} outside the registry's layout "
                    f"({exc}); refusing") from exc
            return st.st_dev, st.st_ino

        guarded = {pair(self.state_root, self.state_root): self.state_root}
        try:
            with os.scandir(self._projects) as entries:
                names = [Path(e.path) for e in entries]
        except OSError as exc:
            raise StorageOwnershipError(
                f"cannot list {self._projects} to guard the registry's layout ({exc}); "
                f"refusing") from exc
        for entry in names:
            try:
                st = os.lstat(entry)
            except OSError as exc:
                raise StorageOwnershipError(
                    f"cannot lstat {entry} to guard the registry's layout ({exc}); "
                    f"refusing") from exc
            if stat.S_ISDIR(st.st_mode):
                guarded[(st.st_dev, st.st_ino)] = entry
        around = {pair(a, self.state_root) for a in (self.state_root, *self.state_root.parents)}

        for path in dict.fromkeys((root, git_dir, common_dir)):
            own = pair(path, path)
            if own in around:
                raise StorageOwnershipError(
                    f"{path} contains the state root {self.state_root} (by (st_dev, st_ino) "
                    f"ancestry); refusing it as a trust root")
            for step in (path, *path.parents):
                hit = guarded.get(pair(step, path))
                if hit is not None:
                    raise StorageOwnershipError(
                        f"{path} lies inside {hit} (the state root or a project directory, by "
                        f"(st_dev, st_ino) ancestry): the trust root and everything git reads "
                        f"for the anchor must lie outside the registry's layout; refusing")

    def _refuse_git_store_in_layout(self, git_dir: Path, common_dir: Path) -> None:
        """Refuse unless the git store of the trust root is reached through no
        symlink and no guarded directory, and borrows no objects. Fails closed:
        any `lstat`, `scandir` or `resolve` error other than a missing
        optional name refuses.

        For the git dir and the common dir (once if they are one): each
        present top-level name of `_GIT_STORE_NAMES` is no symlink and passes
        the `(st_dev, st_ino)` ancestry test of `_refuse_trust_root_in_layout`
        (a bind mount of a guarded directory carries its pair). The resolved
        `<common dir>/objects` passes that test, and a walk that follows no
        symlink covers all of it (fan-out directories, loose objects, `pack/`,
        `info/`), each `refs/` and each `reftable/`: any symlink refuses, and
        so does any directory that fails the same ancestry test (its own pair
        or an ancestor's guarded). Hard links are out of
        model (the operator's registration decision is the trust root).

        `<common dir>/objects/info/alternates`: absent passes; anything but a
        regular file refuses; a read error refuses (never read as empty); a
        regular file with any non-blank content refuses, wherever it points.
        """
        stores = list(dict.fromkeys((git_dir, common_dir)))
        for store in stores:
            for name in _GIT_STORE_NAMES:
                path = store / name
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise StorageOwnershipError(
                        f"cannot lstat {path} in the git store ({exc}); refusing") from exc
                if stat.S_ISLNK(st.st_mode):
                    raise StorageOwnershipError(
                        f"{path} is a symlink: a name of the git store may not lead "
                        f"elsewhere; refusing")
                self._refuse_trust_root_in_layout(path, path, path)

        try:
            objects = (common_dir / "objects").resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise StorageOwnershipError(
                f"cannot resolve {common_dir / 'objects'} ({exc}); refusing") from exc
        self._refuse_trust_root_in_layout(objects, objects, objects)

        def walk(top: Path) -> None:
            pending = [top]
            while pending:
                directory = pending.pop()
                try:
                    with os.scandir(directory) as entries:
                        names = [Path(e.path) for e in entries]
                except OSError as exc:
                    raise StorageOwnershipError(
                        f"cannot list {directory} in the git store ({exc}); refusing"
                    ) from exc
                for path in names:
                    try:
                        st = os.lstat(path)
                    except OSError as exc:
                        raise StorageOwnershipError(
                            f"cannot lstat {path} in the git store ({exc}); refusing"
                        ) from exc
                    if stat.S_ISLNK(st.st_mode):
                        raise StorageOwnershipError(
                            f"{path} is a symlink in the git store; refusing")
                    if stat.S_ISDIR(st.st_mode):
                        self._refuse_trust_root_in_layout(path, path, path)
                        pending.append(path)

        walk(objects)
        for store in stores:
            for name in ("refs", "reftable"):
                try:
                    os.lstat(store / name)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise StorageOwnershipError(
                        f"cannot lstat {store / name} in the git store ({exc}); refusing"
                    ) from exc
                walk(store / name)

        alternates = objects / "info" / "alternates"
        try:
            st = os.lstat(alternates)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StorageOwnershipError(f"cannot lstat {alternates} ({exc}); refusing") from exc
        if not stat.S_ISREG(st.st_mode):
            raise StorageOwnershipError(
                f"{alternates} is not a regular file; borrowed objects are never read for an "
                f"anchor; refusing")
        try:
            fd = os.open(alternates, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            raise StorageOwnershipError(
                f"{alternates} cannot be read ({exc}); refusing") from exc
        if content.strip():
            raise StorageOwnershipError(
                f"{alternates} names borrowed object stores; an anchor is read only from the "
                f"trust root's own objects; refusing")

    def _adoptable_layout(self, project_id: str, anchor: dict[str, Any]) -> Path:
        """Re-check `projects/<p>/` on disk for `adopt_project`; return the
        bootstrap marker (live or versioned). Read-only; refuses unless:

        * the directory exists, `lstat`s as a directory (no symlink) and is not
          a mount;
        * its entries (one `scandir`, no symlink followed) are exactly one
          marker form -- `.vh-owner.json` or one `.vh-owner.json.v0.<UTC>`, a
          regular file with one link, opened with `O_NOFOLLOW`, whose SHA-256
          is the anchor's and whose JSON names this `project_id` and `kind
          "project"` --, kind directories (plain, no mount, on the
          directory's device) whose set equals the anchor's `kinds`, and,
          only beside the versioned marker, this step's own crash leftovers
          (`.lock`, a valid `SEQUENCE.json`, temp files of `PROJECT.json` or
          `SEQUENCE.json`). Any other entry is refused by name.

        It ends with the sequence checks `_next_sequence` would make, so none
        of them can refuse after the marker rename: the floor (unlistable
        directories refuse) and a leftover `SEQUENCE.json` at or above it. A
        leftover below the floor (a legacy writer raised it) is NEEDS_YOU.
        """
        pdir = self._project_dir(project_id)
        try:
            st = os.lstat(pdir)
        except OSError as exc:
            raise StorageOwnershipError(f"{pdir} cannot be read ({exc}); refusing") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise StorageOwnershipError(f"{pdir} is not a plain directory; refusing to adopt it")
        if self._mount_check(pdir):
            raise StorageOwnershipError(f"{pdir} is a mount point; refusing to adopt it")
        try:
            with os.scandir(pdir) as entries:
                listing = sorted((e.name, os.lstat(e.path)) for e in entries)
        except OSError as exc:
            raise StorageOwnershipError(f"cannot list {pdir} ({exc}); refusing") from exc

        markers: list[str] = []
        kinds: set[str] = set()
        leftovers: list[str] = []
        for name, est in listing:
            path = pdir / name
            if name == _BOOTSTRAP_MARKER or _VERSIONED_MARKER.match(name):
                markers.append(name)
            elif name in SCRATCH_KINDS:
                if stat.S_ISLNK(est.st_mode) or not stat.S_ISDIR(est.st_mode):
                    raise StorageOwnershipError(
                        f"kind directory {path} is not a plain directory; refusing")
                if self._mount_check(path):
                    raise StorageOwnershipError(f"kind directory {path} is a mount; refusing")
                if self._st_dev_reader(path) != self._st_dev_reader(pdir):
                    raise StorageOwnershipError(
                        f"kind directory {path} lies on another device than {pdir}; refusing")
                kinds.add(name)
            elif name == _LOCK:
                if not stat.S_ISREG(est.st_mode):
                    raise StorageOwnershipError(f"{path} is not a regular file; refusing")
                leftovers.append(name)
            elif name == _SEQUENCE:
                record = _read_record(path)
                value = record.get("sequence") if record else None
                if (
                    not record or record.get("schema") != SEQUENCE_SCHEMA
                    or not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    raise StorageOwnershipError(
                        f"{path} is not a valid {SEQUENCE_SCHEMA} record; refusing")
                leftovers.append(name)
            elif _is_temp_of(name, _PROJECT_FILE) or _is_temp_of(name, _SEQUENCE):
                if not stat.S_ISREG(est.st_mode):
                    raise StorageOwnershipError(f"{path} is not a regular file; refusing")
                leftovers.append(name)
            else:
                raise StorageOwnershipError(
                    f"{pdir} holds {name!r}, which is not part of an adoptable layout (the "
                    f"bootstrap marker, kind directories and this step's leftovers only); "
                    f"refusing")

        if len(markers) != 1:
            raise StorageOwnershipError(
                f"{pdir} must hold exactly one form of its bootstrap marker "
                f"({_BOOTSTRAP_MARKER} or one {_BOOTSTRAP_MARKER}.v0.<UTC>), found {markers}; "
                f"refusing")
        marker = pdir / markers[0]
        if leftovers and markers[0] == _BOOTSTRAP_MARKER:
            raise StorageOwnershipError(
                f"{pdir} holds leftovers {leftovers} beside the live bootstrap marker; they "
                f"can only follow its rename; refusing")
        expected = anchor["bootstrap_marker"]["sha256"]
        try:
            fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise StorageOwnershipError(
                f"bootstrap marker {marker} cannot be opened without following a symlink "
                f"({exc}); refusing") from exc
        with os.fdopen(fd, "rb") as fh:
            mst = os.fstat(fh.fileno())
            if not stat.S_ISREG(mst.st_mode) or mst.st_nlink != 1:
                raise StorageOwnershipError(
                    f"bootstrap marker {marker} is not a regular file with one link "
                    f"(st_nlink {mst.st_nlink}); refusing")
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise StorageOwnershipError(
                f"bootstrap marker {marker} has sha256 {hashlib.sha256(data).hexdigest()}, "
                f"not the anchor's {expected}; refusing")
        try:
            content = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise StorageOwnershipError(
                f"bootstrap marker {marker} is not JSON; refusing") from exc
        if (
            not isinstance(content, dict) or content.get("project_id") != project_id
            or content.get("kind") != "project"
        ):
            raise StorageOwnershipError(
                f"bootstrap marker {marker} does not name project {project_id!r} with kind "
                f"'project'; refusing")
        if kinds != set(anchor["kinds"]):
            raise StorageOwnershipError(
                f"the kind directories of {pdir} are {sorted(kinds)}, not the anchor's "
                f"kinds {sorted(anchor['kinds'])}; refusing")

        floor = self._sequence_floor(project_id)
        if _SEQUENCE in leftovers:
            value = (_read_record(pdir / _SEQUENCE) or {}).get("sequence")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StorageOwnershipError(
                    f"{pdir / _SEQUENCE} is unreadable; refusing")
            if value < floor:
                raise StorageOwnershipError(
                    f"NEEDS_YOU: the leftover {pdir / _SEQUENCE} holds sequence {value}, below "
                    f"{floor}, the highest sequence on record (a legacy writer raised it); a "
                    f"re-run cannot recover this; nothing was written")
        return marker

    # ------------------------------------------------------------------ configuration

    def read_config(self) -> StorageConfig:
        """Read the operator's `<state_root>/CONFIG.json`; never writes it.

        Format: `{"schema": "veriharness.storage-config/1", "sequence": <int>,
        "admissible_external_parents": [<absolute resolved path>, ...]}`. The
        operator edits this file by hand; the registry offers no method that
        adds to it. No file: an empty configuration (no parent is admissible).

        Refused (`StorageOwnershipError`): a file that is not a JSON object, a
        wrong `schema`, a missing or invalid `sequence` (the message names
        `sequence`), and an `admissible_external_parents` that is not a list.

        Each entry of the list must be an absolute path equal to its own
        `resolve(strict=True)` -- exact, normalised, no symlink component --
        naming an existing directory that is neither the filesystem root nor
        the state root nor inside it. An entry that fails is never admitted:
        it is reported in `invalid_entries` (`{"path", "reason"}`) and the
        rest of the list stands.
        """
        if not os.path.lexists(self._config_path):
            return StorageConfig(sequence=0)
        record = _read_record(self._config_path)
        if record is None:
            raise StorageOwnershipError(
                f"{self._config_path} is not a readable JSON object; refusing"
            )
        if record.get("schema") != CONFIG_SCHEMA:
            raise StorageOwnershipError(
                f"{self._config_path} has schema {record.get('schema')!r}, not "
                f"{CONFIG_SCHEMA}; refusing"
            )
        sequence = record.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise StorageOwnershipError(
                f"{self._config_path} lacks a valid `sequence` (a non-negative integer, "
                f"found {sequence!r}); refusing"
            )
        listed = record.get("admissible_external_parents", [])
        if not isinstance(listed, list):
            raise StorageOwnershipError(
                f"{self._config_path}: admissible_external_parents is not a list; refusing"
            )
        valid: list[Path] = []
        invalid: list[dict[str, str]] = []
        for raw in listed:
            reason = self._admissible_parent_refusal(raw)
            if reason:
                invalid.append({"path": raw if isinstance(raw, str) else repr(raw),
                                "reason": reason})
            elif Path(raw) not in valid:
                valid.append(Path(raw))
        return StorageConfig(sequence=sequence, admissible_external_parents=tuple(valid),
                             invalid_entries=tuple(invalid))

    def _admissible_parent_refusal(self, raw: object) -> str:
        """Why a listed parent is not admissible, or "" if it is."""
        if not isinstance(raw, str) or not raw:
            return "not a non-empty path string"
        if not os.path.isabs(raw):
            return "not an absolute path"
        if os.path.normpath(raw) != raw:
            return f"not normalised (normalises to {os.path.normpath(raw)})"
        try:
            real = Path(raw).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return f"cannot be resolved: {exc}"
        if real != Path(raw):
            return f"not an exact resolved path (resolves to {real})"
        if not real.is_dir():
            return "not a directory"
        if real == Path(real.anchor):
            return "the filesystem root is never an admissible parent"
        if real == self.state_root or real.is_relative_to(self.state_root):
            return "lies in the registry's own layout (the state root or inside it)"
        return ""

    def admissible_external_parents(self) -> tuple[Path, ...]:
        """Read-only: the valid operator-declared parents under which
        `create_external_root` may create a directory (`read_config`)."""
        return self.read_config().admissible_external_parents

    # ------------------------------------------------------------------ leases

    def lease_is_live(self, lease: SessionLease) -> bool:
        """Live only if the pid exists, its start time equals the recorded one,
        and the boot id is the current boot's. Any reader failure: not live."""
        try:
            return bool(
                self._pid_exists(lease.pid)
                and self._pid_start_reader(lease.pid) == lease.pid_start
                and self._boot_id_reader() == lease.boot_id
            )
        except Exception:                       # a reader that fails proves nothing
            return False

    def _read_lease(self, project_id: str, session_id: str) -> SessionLease | None:
        """The public part of `LEASE.json` (never a token: only its digest is
        on disk), or None if the record cannot be read."""
        record = _read_record(self._lease_path(project_id, session_id))
        if not record or record.get("schema") != LEASE_SCHEMA:
            return None
        try:
            lease = SessionLease(
                project_id=record["project_id"],
                session_id=record["session_id"],
                pid=record["pid"],
                pid_start=record["pid_start"],
                boot_id=record["boot_id"],
                since_utc=record["since_utc"],
                sequence=record["sequence"],
            )
        except (KeyError, TypeError):
            return None
        if lease.project_id != project_id or lease.session_id != session_id:
            return None
        if not all(isinstance(v, int) and not isinstance(v, bool)
                   for v in (lease.pid, lease.pid_start, lease.sequence)):
            return None
        return lease

    def open_session(
        self, project_id: str, session_id: str, *, pid: int, boot_id: str
    ) -> SessionLease:
        """Record a lease for `pid` on the current boot.

        The returned lease carries a fresh secret `token`; `LEASE.json` holds
        only its SHA-256, so only this holder can close the lease. An existing
        live lease is superseded by no one: another process (pid, start time
        or boot differ) is refused, and the same live process gets the public
        part of its lease back (without the token, which it already holds). A
        dead lease is superseded: `LEASE.json` is renamed to
        `LEASE.json.v<N>.<UTC>`, never removed. A lease that cannot be read is
        refused -- it cannot be proven dead.
        """
        self._require_init()
        self._require_project(project_id)
        _check_id(session_id, "session id")
        if self._boot_id_reader() != boot_id:
            raise StorageOwnershipError(f"boot id {boot_id!r} is not the current boot")
        if not self._pid_exists(pid):
            raise StorageOwnershipError(f"pid {pid} does not exist")
        pid_start = self._pid_start_reader(pid)
        if not isinstance(pid_start, int):
            raise StorageOwnershipError(f"start time of pid {pid} is unreadable")

        with self._locked(project_id):
            path = self._lease_path(project_id, session_id)
            sessions = path.parent.parent
            for directory in (sessions, path.parent):
                if os.path.lexists(directory):
                    if directory.is_symlink() or not directory.is_dir():
                        raise StorageOwnershipError(f"{directory} is not a plain directory")
                else:
                    os.mkdir(directory)
            if os.path.lexists(path):
                existing = self._read_lease(project_id, session_id)
                if existing is None:
                    raise StorageOwnershipError(
                        f"{path} is unreadable; it cannot be proven dead, refusing"
                    )
                if self.lease_is_live(existing):
                    if (existing.pid, existing.pid_start, existing.boot_id) == (
                        pid, pid_start, boot_id
                    ):
                        return existing
                    raise StorageOwnershipError(
                        f"session {session_id!r} of project {project_id!r} holds a live "
                        f"lease of pid {existing.pid}; refusing",
                        reason=LIVE_HOLDER,
                    )
                _rename_versioned(path, _next_version(path))
            token = secrets.token_hex(32)
            lease = SessionLease(
                project_id=project_id,
                session_id=session_id,
                pid=int(pid),
                pid_start=pid_start,
                boot_id=boot_id,
                since_utc=_utc_iso(),
                sequence=self._next_sequence(project_id),
                token=token,
            )
            _atomic_write_json(path, {
                "schema": LEASE_SCHEMA,
                "project_id": lease.project_id,
                "session_id": lease.session_id,
                "pid": lease.pid,
                "pid_start": lease.pid_start,
                "boot_id": lease.boot_id,
                "since_utc": lease.since_utc,
                "sequence": lease.sequence,
                "token_sha256": _token_digest(token),
            })
            return lease

    def close_session(self, project_id: str, session_id: str, lease: SessionLease) -> Path:
        """Close the session's own lease: it is kept as `LEASE.json.v<N>.<UTC>`.

        Only the holder can end a lease: refused unless `lease` is exactly the
        lease on record for this project and session **and** carries the
        secret token whose SHA-256 the record holds (compared in constant
        time). A lease rebuilt from the public `LEASE.json` has no token and
        is refused -- one session cannot close another's.
        """
        self._require_init()
        self._require_project(project_id)
        _check_id(session_id, "session id")
        if lease.project_id != project_id or lease.session_id != session_id:
            raise StorageOwnershipError(
                f"lease of session {lease.session_id!r} cannot close session {session_id!r}"
            )
        if not isinstance(lease.token, str) or not lease.token:
            raise StorageOwnershipError(
                "this lease carries no token: only the holder that opened it can close it"
            )
        with self._locked(project_id):
            path = self._lease_path(project_id, session_id)
            current = self._read_lease(project_id, session_id)
            if current != lease:
                raise StorageOwnershipError(
                    f"the lease on record for session {session_id!r} is not the one given"
                )
            digest = (_read_record(path) or {}).get("token_sha256")
            if not isinstance(digest, str) or not hmac.compare_digest(
                digest, _token_digest(lease.token)
            ):
                raise StorageOwnershipError(
                    f"the token does not match the lease of session {session_id!r}; refusing"
                )
            return _rename_versioned(path, _next_version(path))

    # ------------------------------------------------------------------ scratch

    def _scratch_entries(self, project_id: str) -> list[dict[str, Any]]:
        record = _read_record(self._project_dir(project_id) / _SCRATCH_INDEX)
        if not record or record.get("schema") != SCRATCH_INDEX_SCHEMA:
            return []
        entries = record.get("entries")
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def _root_entries(self, project_id: str) -> list[dict[str, Any]]:
        record = _read_record(self._project_dir(project_id) / _ROOT_INDEX)
        if not record or record.get("schema") != ROOT_INDEX_SCHEMA:
            return []
        entries = record.get("entries")
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def _write_index(self, project_id: str, name: str, schema: str,
                     entries: list[dict[str, Any]]) -> None:
        _atomic_write_json(
            self._project_dir(project_id) / name,
            {"schema": schema, "project_id": project_id, "entries": entries,
             "sequence": self._next_sequence(project_id)},
        )

    def _scratch_rel(self, project_id: str, kind: str, session_id: str) -> str:
        return f"{_PROJECTS}/{project_id}/{kind}/{session_id}"

    def _scratch_entry(self, project_id: str, kind: str, session_id: str
                       ) -> dict[str, Any] | None:
        """The index entry of a scratch dir, if its record is valid **in place**.

        The record counts only at exactly
        `<state_root>/projects/<p>/<kind>/<s>/.vh-owner.json`, only when its
        `project_id`, `kind` and `session_id` equal those path components, only
        when the directory is in `SCRATCH_INDEX.json`, and only while the
        directory is still the inode the index recorded.
        """
        if kind not in SCRATCH_KINDS or not _ID.match(session_id):
            return None
        rel = self._scratch_rel(project_id, kind, session_id)
        entry = next(
            (e for e in self._scratch_entries(project_id)
             if e.get("path") == rel and e.get("kind") == kind
             and e.get("session_id") == session_id),
            None,
        )
        if entry is None:
            return None
        directory = self.state_root / rel
        record = _read_record(directory / _SCRATCH_RECORD)
        if (
            not record
            or record.get("schema") != SCRATCH_RECORD_SCHEMA
            or record.get("project_id") != project_id
            or record.get("kind") != kind
            or record.get("session_id") != session_id
        ):
            return None
        try:
            st = os.lstat(directory)
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        if (st.st_dev, st.st_ino) != (entry.get("st_dev"), entry.get("st_ino")):
            return None
        return entry

    def scratch_dir(self, project_id: str, session_id: str, kind: str) -> Path:
        """Create `projects/<p>/<kind>/<s>/` with its `.vh-owner.json` and its
        `SCRATCH_INDEX.json` entry -- all before the path is returned.

        The session must hold a live lease. A directory that already exists
        without being in the index is refused, never adopted (legacy
        directories are registered by a migration with its own proof).
        """
        self._require_init()
        self._require_project(project_id)
        _check_id(session_id, "session id")
        if kind not in SCRATCH_KINDS:
            raise StorageOwnershipError(f"unknown scratch kind {kind!r}")

        with self._locked(project_id):
            lease = self._read_lease(project_id, session_id)
            if lease is None:
                raise StorageOwnershipError(
                    f"session {session_id!r} of project {project_id!r} holds no lease"
                )
            if not self.lease_is_live(lease):
                raise StorageOwnershipError(
                    f"the lease of session {session_id!r} is not live"
                )
            rel = self._scratch_rel(project_id, kind, session_id)
            directory = self.state_root / rel
            entries = self._scratch_entries(project_id)
            if any(e.get("path") == rel for e in entries):
                if self._scratch_entry(project_id, kind, session_id) is None:
                    raise StorageOwnershipError(
                        f"{directory} is indexed but its record or inode no longer matches"
                    )
                return directory
            if os.path.lexists(directory):
                raise StorageOwnershipError(
                    f"{directory} exists but is not in {_SCRATCH_INDEX}; refusing to adopt it"
                )
            kind_dir = directory.parent
            if os.path.lexists(kind_dir):
                if kind_dir.is_symlink() or not kind_dir.is_dir():
                    raise StorageOwnershipError(f"{kind_dir} is not a plain directory")
            else:
                os.mkdir(kind_dir)
            os.mkdir(directory)
            _fsync_dir(kind_dir)
            st = os.lstat(directory)
            created = _utc_iso()
            _atomic_write_json(directory / _SCRATCH_RECORD, {
                "schema": SCRATCH_RECORD_SCHEMA,
                "project_id": project_id,
                "session_id": session_id,
                "kind": kind,
                "created_utc": created,
                "sequence": self._next_sequence(project_id),
            })
            entries.append({
                "path": rel,
                "project_id": project_id,
                "kind": kind,
                "session_id": session_id,
                "created_utc": created,
                "st_dev": st.st_dev,
                "st_ino": st.st_ino,
                "mount_id": self._mount_id_reader(directory),
            })
            self._write_index(project_id, _SCRATCH_INDEX, SCRATCH_INDEX_SCHEMA, entries)
            return directory

    # ------------------------------------------------------------------ external roots

    def _all_roots(self) -> list[tuple[str, Path, dict[str, Any] | None]]:
        """Every external root (in any state) and every scratch dir, of every project."""
        out: list[tuple[str, Path, dict[str, Any] | None]] = []
        for project_id in self._registered_projects():
            for entry in self._root_entries(project_id):
                if isinstance(entry.get("path"), str):
                    out.append((project_id, Path(entry["path"]), entry))
            for entry in self._scratch_entries(project_id):
                if isinstance(entry.get("path"), str):
                    out.append((project_id, self.state_root / entry["path"], None))
        return out

    def _refuse_unsafe_root(self, project_id: str, path: Path, kind: str, *,
                            must_exist: bool, replaces_pending: bool = False) -> None:
        """The one safety check for both `register_external_root` and
        `create_external_root`. Raises on the first rule that refuses."""
        if kind not in EXTERNAL_KINDS:
            raise StorageOwnershipError(f"unknown external root kind {kind!r}")
        if path == Path(path.anchor):
            raise StorageOwnershipError("the filesystem root is never an external root")
        state = self.state_root
        if path == state or state.is_relative_to(path) or path.is_relative_to(state):
            raise StorageOwnershipError(
                f"{path} is the state root, an ancestor of it, or inside it; refusing"
            )
        for forbidden in self.forbidden_roots:
            if forbidden.is_relative_to(path):
                raise StorageOwnershipError(
                    f"{path} is a forbidden root or an ancestor of one ({forbidden}); refusing"
                )
        for owner, other, entry in self._all_roots():
            if not (path == other or path.is_relative_to(other) or other.is_relative_to(path)):
                continue
            if (
                replaces_pending and entry is not None and owner == project_id
                and path == other and entry.get("state") in (PENDING, ABANDONED)
            ):
                continue
            raise StorageOwnershipError(
                f"{path} overlaps {other}, already registered for project {owner!r}; refusing"
            )
        if must_exist:
            if not path.is_dir() or path.is_symlink():
                raise StorageOwnershipError(f"{path} is not a directory")
            if self._mount_check(path):
                raise StorageOwnershipError(f"{path} is a mount point; refusing")

    def _resolve_exact(self, path: os.PathLike[str] | str, *, exists: bool) -> Path:
        """The exact path, which must already be its own resolved form."""
        lex = _lexical(path)
        try:
            if exists:
                real = Path(path).resolve(strict=True)
            else:
                real = Path(path).parent.resolve(strict=True) / Path(path).name
        except (OSError, RuntimeError) as exc:
            raise StorageOwnershipError(f"{path} cannot be resolved: {exc}") from exc
        if lex != real:
            raise StorageOwnershipError(
                f"{path} is not an exact resolved path (resolves to {real}); refusing"
            )
        return real

    def create_external_root(self, project_id: str, path: os.PathLike[str] | str,
                             kind: str) -> dict[str, Any]:
        """Create an external root and record it before it exists.

        Under the lock: an index entry `PENDING`, then `os.mkdir(path)` by the
        registry itself (a path that already exists is refused), then the new
        inode's `st_dev`/`st_ino` and `CREATED`. Only `CREATED` entries whose
        directory is still that inode are ever `OWNED`; a crash between
        `mkdir` and `CREATED` leaves the path `UNKNOWN`.

        **Admissible parents.** The new directory's parent must be exactly one
        of the valid `admissible_external_parents` the operator listed in
        `CONFIG.json` (`read_config`); the registry itself never adds to that
        list. Any other parent -- in particular one that is `UNKNOWN`, foreign
        territory -- is refused before the `PENDING` entry is written. If the
        parent is the resolved form of a listed entry that is invalid (for
        example a non-normalised spelling or a symlink), the refusal names
        that entry and why it is invalid. Overlap with any registered root is
        refused first and wins: no root is ever created inside another.
        """
        self._require_init()
        self._require_project(project_id)
        target = self._resolve_exact(path, exists=False)
        with self._locked(project_id):
            self._refuse_unsafe_root(project_id, target, kind, must_exist=False)
            config = self.read_config()
            if target.parent not in config.admissible_external_parents:
                named = [
                    f"{e['path']!r} ({e['reason']})" for e in config.invalid_entries
                    if _resolves_to(e["path"], target.parent)
                ]
                hint = (f"; the listed entry {', '.join(named)} resolves to it but is not "
                        f"admissible") if named else ""
                raise StorageOwnershipError(
                    f"{target.parent} is not a declared admissible parent for external "
                    f"roots{hint}; refusing to create {target}"
                )
            if os.path.lexists(target):
                raise StorageOwnershipError(f"{target} already exists; refusing to adopt it")
            if target.parent.is_symlink() or not target.parent.is_dir():
                raise StorageOwnershipError(f"parent of {target} is not a plain directory")
            entries = self._root_entries(project_id)
            entry: dict[str, Any] = {
                "path": str(target),
                "project_id": project_id,
                "kind": kind,
                "state": PENDING,
                "origin": "created",
                "created_utc": _utc_iso(),
            }
            entries.append(entry)
            self._write_index(project_id, _ROOT_INDEX, ROOT_INDEX_SCHEMA, entries)
            try:
                os.mkdir(target)
            except FileExistsError as exc:
                raise StorageOwnershipError(
                    f"{target} was created by someone else; it stays {UNKNOWN}"
                ) from exc
            except OSError as exc:
                raise StorageOwnershipError(f"cannot create {target}: {exc}") from exc
            _fsync_dir(target.parent)
            st = os.lstat(target)
            if self._mount_check(target):
                raise StorageOwnershipError(f"{target} is a mount point; it stays {PENDING}")
            entry.update(state=CREATED, st_dev=st.st_dev, st_ino=st.st_ino,
                         mount_id=self._mount_id_reader(target))
            self._write_index(project_id, _ROOT_INDEX, ROOT_INDEX_SCHEMA, entries)
            return dict(entry)

    def register_external_root(self, project_id: str, path: os.PathLike[str] | str,
                               kind: str, anchor: dict[str, str]) -> dict[str, Any]:
        """Register a root VeriHarness created before the registry existed.

        Proven, not asserted: `anchor = {"commit", "file"}` names a JSON file
        committed in the registered project's repository at a commit that is
        an ancestor of its `HEAD`; its top-level `schema` is exactly
        `veriharness.storage-legacy-roots/1` and its `roots` list holds
        `{"path": <exact resolved path>, "kind": <kind>, "project_id":
        <caller>}`. An anchor belongs to one project: an entry naming another
        project, or none, is refused. Nothing inside the candidate directory
        is trusted as an anchor. Per kind, the structure is re-checked on disk
        (see `_check_structure`).
        """
        self._require_init()
        project = self._require_project(project_id)
        target = self._resolve_exact(path, exists=True)
        with self._locked(project_id):
            self._refuse_unsafe_root(project_id, target, kind, must_exist=True,
                                     replaces_pending=True)
            verified = self._verify_anchor(project, target, kind, anchor)
            self._check_structure(project_id, target, kind)
            st = os.lstat(target)
            entries = self._root_entries(project_id)
            for old in entries:
                if old.get("path") == str(target) and old.get("state") == PENDING:
                    old["state"] = ABANDONED
            entry = {
                "path": str(target),
                "project_id": project_id,
                "kind": kind,
                "state": REGISTERED,
                "origin": "legacy_anchor",
                "anchor": verified,
                "created_utc": _utc_iso(),
                "st_dev": st.st_dev,
                "st_ino": st.st_ino,
                "mount_id": self._mount_id_reader(target),
            }
            entries.append(entry)
            self._write_index(project_id, _ROOT_INDEX, ROOT_INDEX_SCHEMA, entries)
            return dict(entry)

    @staticmethod
    def _git(repo: Path, *args: str, ceiling: Path | None = None
             ) -> subprocess.CompletedProcess[str]:
        """Run `git -C <repo>` without substitutions.

        Redirecting and config-injecting variables of the caller are dropped
        and `_GIT_FIXED_ENV` is set (no system or global config, no grafts, no
        replace objects, no lazy fetch). With
        `ceiling`, `GIT_CEILING_DIRECTORIES` is set to it, so discovery stops
        before an enclosing repository; every call about a project repository
        passes the repository root's parent here.
        """
        env = {
            k: v for k, v in os.environ.items()
            if k not in _GIT_REDIRECTS and not k.startswith(_GIT_CONFIG_INJECTION)
        }
        env.update(_GIT_FIXED_ENV)
        if ceiling is not None:
            env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, env=env, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StorageOwnershipError(f"git failed in {repo}: {exc}") from exc

    def _repo_identity(self, root: Path) -> Path | None:
        """The resolved `--git-common-dir` of `root` if `root` is itself a
        repository root, else None (a plain directory: compared by path only).

        `--show-toplevel` runs with the ceiling at `root.parent`, so a plain
        directory inside an enclosing repository never takes that
        repository's identity. A `.git` that git cannot confirm is refused.
        """
        top = self._git(root, "rev-parse", "--show-toplevel", ceiling=root.parent)
        is_root = top.returncode == 0 and top.stdout.strip() != "" and \
            Path(top.stdout.strip()).resolve(strict=False) == root
        if not is_root:
            if os.path.lexists(root / ".git"):
                raise StorageOwnershipError(
                    f"{root}/.git exists but git does not confirm {root} as a repository "
                    f"root; refusing"
                )
            return None
        common = self._git(root, "rev-parse", "--git-common-dir", ceiling=root.parent)
        if common.returncode != 0 or not common.stdout.strip():
            raise StorageOwnershipError(f"cannot read the git common dir of {root}; refusing")
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = root / common_dir
        return common_dir.resolve(strict=False)

    def _refuse_partial_clone(self, repo: Path) -> None:
        """A partial clone is never a trust root: `cat-file` on a missing
        object could fetch it from a promisor remote, and git 2.43 ignores
        `GIT_NO_LAZY_FETCH`. Refused if `extensions.partialclone` or any
        `remote.<name>.promisor` is set, or if the config cannot be read."""
        for pattern in (r"^extensions\.partialclone$", r"^remote\..*\.promisor$"):
            found = self._git(repo, "config", "--get-regexp", pattern, ceiling=repo.parent)
            if found.returncode == 1 and not found.stdout.strip():
                continue
            raise StorageOwnershipError(
                f"{repo} is a partial clone or its config cannot be read "
                f"({pattern}: {found.stdout.strip() or found.stderr.strip()}); refusing it "
                f"as a trust root"
            )

    def _read_committed_json(self, repo: Path, anchor: dict[str, str]
                             ) -> tuple[Any, str, str]:
        """Read the JSON file `anchor = {"commit", "file"}` names from the git
        objects of `repo`: `(record, full_commit, file)`.

        Every git call runs through `_git` (`_GIT_FIXED_ENV`, ceiling at the
        repository's parent). Refused: a commit that is not a hex id, a file
        that is not a relative repository path without `..` or starts with
        `-`, a partial clone, a commit that is not an ancestor of `HEAD`, a
        `<commit>:<file>` that is not a blob, and a blob that is not JSON. The
        working tree is never read. The parsed value is returned as it is;
        each caller checks that it is an object with its own message (so
        `_verify_anchor`'s refusals stay what they were).
        """
        if not isinstance(anchor, dict):
            raise StorageOwnershipError("anchor must be {'commit', 'file'}")
        commit, file = anchor.get("commit"), anchor.get("file")
        if not isinstance(commit, str) or not _COMMIT.match(commit):
            raise StorageOwnershipError(f"anchor commit {commit!r} is not a hex commit id")
        if (
            not isinstance(file, str) or not file or os.path.isabs(file)
            or ".." in Path(file).parts or file.startswith("-")
        ):
            raise StorageOwnershipError(f"anchor file {file!r} is not a repository path")
        ceiling = repo.parent
        self._refuse_partial_clone(repo)
        ancestor = self._git(repo, "merge-base", "--is-ancestor", commit, "HEAD",
                             ceiling=ceiling)
        if ancestor.returncode != 0:
            raise StorageOwnershipError(
                f"anchor commit {commit} is not an ancestor of HEAD of {repo}"
            )
        full = self._git(repo, "rev-parse", "--verify", "--quiet", commit, ceiling=ceiling)
        spec = f"{commit}:{file}"
        kind_of = self._git(repo, "cat-file", "-t", spec, ceiling=ceiling)
        if full.returncode != 0 or kind_of.returncode != 0 or kind_of.stdout.strip() != "blob":
            raise StorageOwnershipError(f"anchor {spec} is not a committed file")
        blob = self._git(repo, "cat-file", "-p", spec, ceiling=ceiling)
        if blob.returncode != 0:
            raise StorageOwnershipError(f"anchor {spec} cannot be read from git")
        try:
            record = json.loads(blob.stdout)
        except ValueError as exc:
            raise StorageOwnershipError(f"anchor {spec} is not JSON") from exc
        return record, full.stdout.strip(), file

    def _verify_anchor(self, project: dict[str, Any], target: Path, kind: str,
                       anchor: dict[str, str]) -> dict[str, str]:
        repo = Path(project["project_root"])
        if repo == target or repo.is_relative_to(target):
            raise StorageOwnershipError(
                f"the project repository {repo} lies inside {target}; a file inside the "
                f"candidate is never an anchor"
            )
        record, full_commit, file = self._read_committed_json(repo, anchor)
        spec = f"{anchor['commit']}:{file}"
        if not isinstance(record, dict) or record.get("schema") != LEGACY_ROOTS_SCHEMA:
            raise StorageOwnershipError(
                f"anchor {spec} does not carry schema {LEGACY_ROOTS_SCHEMA}"
            )
        roots = record.get("roots")
        entries = [r for r in (roots if isinstance(roots, list) else []) if isinstance(r, dict)]
        at_path = [r for r in entries if r.get("path") == str(target)]
        if not at_path:
            raise StorageOwnershipError(
                f"anchor {spec} lists no root with path {target} (a `roots[]` entry whose "
                f"`path` is exactly that string)"
            )
        named = [r for r in at_path if r.get("kind") == kind]
        if not named:
            raise StorageOwnershipError(
                f"anchor {spec} lists {target} only with kind "
                f"{sorted({str(r.get('kind')) for r in at_path})}, not with kind {kind}"
            )
        if not any(r.get("project_id") == project["project_id"] for r in named):
            raise StorageOwnershipError(
                f"anchor {spec} names {target} for another project (or for none), not for "
                f"{project['project_id']!r}; an anchor belongs to one project"
            )
        return {"commit": full_commit, "file": file}

    # ------------------------------------------------------------------ legacy roots

    def legacy_roots(self, project_id: str, *, intact_only: bool = True
                     ) -> list[dict[str, Any]]:
        """The REGISTERED entries of `LEGACY_INDEX.json` of `project_id` (only
        those whose directory is still the recorded inode, unless
        `intact_only` is False). SUPERSEDED entries are never returned."""
        record = _read_record(self._project_dir(project_id) / _LEGACY_INDEX)
        if not record or record.get("schema") != LEGACY_INDEX_SCHEMA:
            return []
        entries = record.get("entries")
        out = []
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or not isinstance(e.get("path"), str):
                continue
            if e.get("state") != REGISTERED or e.get("kind") not in LEGACY_KINDS:
                continue
            if e.get("project_id") != project_id or not isinstance(e.get("children"), dict):
                continue
            if intact_only and not self._root_intact(e):
                continue
            out.append(e)
        return out

    def legacy_anchor_blobs(self, project_id: str, path: os.PathLike[str] | str) -> set[str]:
        """The blob ids of every anchor that registered `path` (REGISTERED or
        SUPERSEDED entries): the registry's own records, never usage evidence."""
        record = _read_record(self._project_dir(project_id) / _LEGACY_INDEX)
        if not record or record.get("schema") != LEGACY_INDEX_SCHEMA:
            return set()
        return {e["anchor_blob"] for e in record.get("entries") or []
                if isinstance(e, dict) and e.get("path") == os.fspath(path)
                and isinstance(e.get("anchor_blob"), str)}

    def legacy_child(self, path: os.PathLike[str] | str) -> dict[str, Any] | None:
        """The anchor-bound record `{"type", "entries_digest"}` of `path` when
        it is a direct child of an intact REGISTERED legacy root, else None.
        The binding is by name under the root's recorded inode; the caller
        compares the content digest it measures with `entries_digest`."""
        p = Path(path)
        try:
            parts = p.relative_to(self.state_root).parts
        except ValueError:
            return None
        if len(parts) < 2 or parts[0] != _PROJECTS:
            return None
        for entry in self.legacy_roots(parts[1]):
            if p.parent == Path(entry["path"]):
                child = entry["children"].get(p.name)
                if not isinstance(child, dict) or not _same_inode(
                        p, (entry.get("child_ids") or {}).get(p.name)):
                    return None
                return dict(child)
        return None

    def _committed_blob(self, repo: Path, cited: Any, what: str) -> tuple[dict[str, str], bytes]:
        """`cited = {"commit", "file", "blob"}`: the commit is an ancestor of
        HEAD, `file` lies under `dogfood/`, and `<commit>:<file>` is a blob
        whose id is `blob`. Returns the citation and the blob's bytes."""
        if not isinstance(cited, dict) or set(cited) != {"commit", "file", "blob"}:
            raise StorageOwnershipError(f"{what} must be exactly {{commit, file, blob}}: {cited!r}")
        commit, file, blob = cited["commit"], cited["file"], cited["blob"]
        if not all(isinstance(v, str) for v in (commit, file, blob)) or \
                not _COMMIT.match(commit) or not _COMMIT.match(blob):
            raise StorageOwnershipError(f"{what}: commit and blob must be hex ids: {cited!r}")
        if (not file or os.path.isabs(file) or ".." in Path(file).parts or file.startswith("-")
                or Path(file).parts[:1] != ("dogfood",)):
            raise StorageOwnershipError(f"{what}: {file!r} is not a path under dogfood/")
        ceiling = repo.parent
        if self._git(repo, "merge-base", "--is-ancestor", commit, "HEAD",
                     ceiling=ceiling).returncode != 0:
            raise StorageOwnershipError(f"{what}: {commit} is not an ancestor of HEAD")
        spec = f"{commit}:{file}"
        kind_of = self._git(repo, "cat-file", "-t", spec, ceiling=ceiling)
        oid = self._git(repo, "rev-parse", "--verify", "--quiet", spec, ceiling=ceiling)
        if kind_of.returncode != 0 or kind_of.stdout.strip() != "blob" or \
                oid.returncode != 0 or oid.stdout.strip() != blob:
            raise StorageOwnershipError(f"{what}: {spec} is not the committed blob {blob}")
        env = {k: v for k, v in os.environ.items()
               if k not in _GIT_REDIRECTS and not k.startswith(_GIT_CONFIG_INJECTION)}
        env.update(_GIT_FIXED_ENV, GIT_CEILING_DIRECTORIES=str(ceiling))
        data = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob],
                              capture_output=True, env=env, timeout=60, check=False)
        if data.returncode != 0:
            raise StorageOwnershipError(f"{what}: blob {blob} cannot be read")
        return {"commit": commit, "file": file, "blob": blob}, data.stdout

    def _legacy_entries(self, root: Path, base: Path, uid: int, mount: int | None
                        ) -> tuple[list[dict[str, Any]], int, int]:
        """Every entry below `base` (inside the legacy `root`), `lstat` only,
        with the per-entry refusals of `register_legacy_root`: another uid
        than the state root's, another `st_dev` or mount id than the root's,
        a non-directory with more than one link, a symlink resolving outside
        `root`. Returns the entries in `storage_tiers.Entry` form (relative to
        `base`), the apparent bytes and the allocated bytes (each inode once)."""
        root_dev = self._st_dev_reader(root)
        entries: list[dict[str, Any]] = []
        apparent = allocated = 0
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            full_dir = base / rel_dir if rel_dir else base
            with os.scandir(full_dir) as listing:
                names = sorted(item.name for item in listing)
            for name in names:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                full = base / rel
                st = _legacy_lstat(full)
                if st.st_uid != uid:
                    raise StorageOwnershipError(f"{full} belongs to uid {st.st_uid}; refusing")
                if self._st_dev_reader(full) != root_dev:
                    raise StorageOwnershipError(f"{full} lies on another device; refusing")
                if mount is None or self._mount_id_reader(full) != mount:
                    raise StorageOwnershipError(f"{full} lies on another mount; refusing")
                mode = stat.S_IMODE(st.st_mode)
                allocated += st.st_blocks * 512
                if stat.S_ISDIR(st.st_mode):
                    entries.append(_entry(rel, "dir", mode, 0))
                    stack.append(rel)
                    continue
                if st.st_nlink > 1:
                    raise StorageOwnershipError(f"{full} has {st.st_nlink} hard links; refusing")
                if stat.S_ISREG(st.st_mode):
                    entries.append(_entry(rel, "file", mode, st.st_size, _sha256_path(full)))
                    apparent += st.st_size
                elif stat.S_ISLNK(st.st_mode):
                    link = os.readlink(full)
                    resolved = Path(os.path.normpath(full.parent / link))
                    if not (resolved == root or resolved.is_relative_to(root)):
                        raise StorageOwnershipError(f"{full} is a symlink leaving the root; refusing")
                    entries.append(_entry(rel, "symlink", mode, len(os.fsencode(link)),
                                          link=link))
                else:
                    raise StorageOwnershipError(f"{full} is not a file, dir or symlink")
        return sorted(entries, key=lambda e: e["rel"]), apparent, allocated

    def legacy_survey_root(self, target: Path) -> dict[str, Any]:
        """The content binding of a legacy root, read-only: `children` (each
        direct entry's type and entries digest, in the form `classify_tier`
        measures it), `files`, `bytes_apparent`, `bytes_allocated`. Raises
        `StorageOwnershipError` on the first per-entry refusal."""
        uid = _legacy_lstat(self.state_root).st_uid
        mount = self._mount_id_reader(target)
        if mount is None:
            raise StorageOwnershipError(f"the mount of {target} cannot be read; refusing")
        children: dict[str, dict[str, str]] = {}
        files = apparent = allocated = 0
        with os.scandir(target) as listing:
            names = sorted(item.name for item in listing)
        for name in names:
            child = target / name
            st = _legacy_lstat(child)
            if stat.S_ISDIR(st.st_mode):
                if st.st_uid != uid or self._st_dev_reader(child) != \
                        self._st_dev_reader(target) or self._mount_id_reader(child) != mount:
                    raise StorageOwnershipError(f"{child} is foreign or on another mount")
                entries, a, b = self._legacy_entries(target, child, uid, mount)
                allocated += st.st_blocks * 512 + b
                kind = "dir"
            else:
                single, a, b = self._legacy_single(target, child, uid, mount)
                entries = single
                allocated += b
                kind = entries[0]["type"] if entries else "symlink"
            apparent += a
            files += sum(1 for e in entries if e["type"] == "file")
            children[name] = {"type": kind, "entries_digest": _entries_digest(entries)}
        return {"children": children, "files": files, "bytes_apparent": apparent,
                "bytes_allocated": allocated}

    def legacy_entry_digest(self, root: os.PathLike[str] | str,
                            path: os.PathLike[str] | str) -> str:
        """The entries digest of the direct entry `path` of the legacy `root`,
        in the form its binding has (a directory: its entries; a regular file:
        the single entry ``"."``; a symlink: no entries). Read-only; raises
        `StorageOwnershipError` on every per-entry refusal of the survey."""
        root, path = Path(root), Path(path)
        uid = _legacy_lstat(self.state_root).st_uid
        mount = self._mount_id_reader(root)
        if mount is None:
            raise StorageOwnershipError(f"the mount of {root} cannot be read; refusing")
        st = _legacy_lstat(path)
        if stat.S_ISDIR(st.st_mode):
            if st.st_uid != uid or self._st_dev_reader(path) != self._st_dev_reader(root) \
                    or self._mount_id_reader(path) != mount:
                raise StorageOwnershipError(f"{path} is foreign or on another mount")
            return _entries_digest(self._legacy_entries(root, path, uid, mount)[0])
        return _entries_digest(self._legacy_single(root, path, uid, mount)[0])

    def _legacy_single(self, root: Path, path: Path, uid: int, mount: int | None
                       ) -> tuple[list[dict[str, Any]], int, int]:
        """A non-directory direct child of a legacy root, in the form
        `classify_tier` measures it: a regular file is one entry ``"."``; a
        symlink has no entries (it never gets a proof)."""
        st = _legacy_lstat(path)
        if st.st_uid != uid or self._st_dev_reader(path) != self._st_dev_reader(root) \
                or mount is None or self._mount_id_reader(path) != mount:
            raise StorageOwnershipError(f"{path} is foreign or on another device or mount")
        if stat.S_ISLNK(st.st_mode):
            resolved = Path(os.path.normpath(path.parent / os.readlink(path)))
            if not (resolved == root or resolved.is_relative_to(root)):
                raise StorageOwnershipError(f"{path} is a symlink leaving the root; refusing")
            return [], 0, 0
        if not stat.S_ISREG(st.st_mode):
            raise StorageOwnershipError(f"{path} is not a file, dir or symlink")
        if st.st_nlink > 1:
            raise StorageOwnershipError(f"{path} has {st.st_nlink} hard links; refusing")
        return ([_entry(".", "file", stat.S_IMODE(st.st_mode), st.st_size, _sha256_path(path))],
                st.st_size, st.st_blocks * 512)

    def register_legacy_root(self, project_id: str, path: os.PathLike[str] | str,
                             kind: str, anchor: dict[str, str]) -> dict[str, Any]:
        """Adopt one legacy directory **inside** the state root (STORAGE-L).

        The one door for it. `kind` is in `LEGACY_KINDS`; for `legacy_review`
        the path is exactly `<state_root>/projects/<p>/scratch/<s>/reviews/<id>`
        (a plain directory, no mount), overlapping no indexed scratch dir,
        external root or other legacy root; `anchor` passes `_verify_anchor`;
        its entry carries `designations[]` and `reports[]` (each committed
        under `dogfood/` at an ancestor of HEAD; one non-report designation
        contains `<path>/`) and `children` -- the type and entries digest of
        **every** direct entry, which the content measured now must equal
        name for name (`legacy_survey_root`). A root that is already
        REGISTERED is re-anchored only by an anchor whose commit descends
        from the recorded one; the old entry is then marked SUPERSEDED (kept).
        Nothing is written inside the root.
        """
        self._require_init()
        project = self._require_project(project_id)
        if kind not in LEGACY_KINDS:
            raise StorageOwnershipError(f"unknown legacy root kind {kind!r}")
        target = self._resolve_exact(path, exists=True)
        with self._locked(project_id):
            try:
                rel = target.relative_to(self.state_root).parts
            except ValueError:
                rel = ()
            if not (len(rel) == 6 and rel[0] == _PROJECTS and rel[1] == project_id
                    and rel[2] == "scratch" and rel[4] == "reviews"
                    and _ID.match(rel[3]) and _ID.match(rel[5])):
                raise StorageOwnershipError(
                    f"{target} is not <state_root>/projects/{project_id}/scratch/<s>/reviews/<id>")
            st = os.lstat(target)
            if not stat.S_ISDIR(st.st_mode) or self._mount_check(target):
                raise StorageOwnershipError(f"{target} is not a plain directory or is a mount")
            for _, other, _ in self._all_roots():
                if target == other or target.is_relative_to(other) or other.is_relative_to(target):
                    raise StorageOwnershipError(f"{target} overlaps {other}; refusing")
            previous = None
            for e in self.legacy_roots(project_id, intact_only=False):
                other = Path(e["path"])
                if target == other and self._root_intact(e):
                    previous = e
                elif target == other or target.is_relative_to(other) or \
                        other.is_relative_to(target):
                    raise StorageOwnershipError(
                        f"{target} overlaps the legacy root {other}; refusing")
            verified = self._verify_anchor(project, target, kind, anchor)
            repo = Path(project["project_root"])
            if previous is not None:
                old = previous["anchor"]["commit"]
                if old == verified["commit"] or self._git(
                        repo, "merge-base", "--is-ancestor", old, verified["commit"],
                        ceiling=repo.parent).returncode != 0:
                    raise StorageOwnershipError(
                        f"{target} is already registered with anchor {old}; only an anchor "
                        f"that descends from it re-anchors it")
            record, _, _ = self._read_committed_json(repo, anchor)
            root = next(r for r in record["roots"] if isinstance(r, dict)
                        and r.get("path") == str(target) and r.get("kind") == kind
                        and r.get("project_id") == project_id)
            designations, reports = root.get("designations"), root.get("reports")
            if not isinstance(designations, list) or not designations:
                raise StorageOwnershipError(f"anchor entry for {target} has no designations")
            if not isinstance(reports, list) or not reports:
                raise StorageOwnershipError(f"anchor entry for {target} has no reports")
            needle = (str(target) + "/").encode()
            designated = False
            cited_d = []
            for cited in designations:
                rec, data = self._committed_blob(repo, cited, "designation")
                cited_d.append(rec)
                if "-report" not in Path(rec["file"]).name and needle in data:
                    designated = True
            if not designated:
                raise StorageOwnershipError(
                    f"no committed designation (a non-report brief under dogfood/) names "
                    f"{target}/ exactly; refusing")
            cited_r = [self._committed_blob(repo, cited, "report")[0] for cited in reports]
            try:
                measured = self.legacy_survey_root(target)
            except OSError as exc:
                raise StorageOwnershipError(f"{target} cannot be walked: {exc}") from exc
            if root.get("children") != measured["children"]:
                raise StorageOwnershipError(
                    f"{target}: its children differ from the anchor's binding (names, types or "
                    f"entries digests); refusing")
            anchor_blob = self._git(repo, "rev-parse", "--verify", "--quiet",
                                    f"{verified['commit']}:{verified['file']}",
                                    ceiling=repo.parent).stdout.strip()
            child_ids = {}
            for name in measured["children"]:
                cst = _legacy_lstat(target / name)
                child_ids[name] = [cst.st_dev, cst.st_ino]
            st = os.lstat(target)
            entry = {
                "child_ids": child_ids,
                "path": str(target), "project_id": project_id, "kind": kind,
                "state": REGISTERED, "origin": "legacy_anchor", "anchor": verified,
                "anchor_blob": anchor_blob,
                "designations": cited_d, "reports": cited_r, **measured,
                "created_utc": _utc_iso(), "st_dev": st.st_dev, "st_ino": st.st_ino,
                "mount_id": self._mount_id_reader(target),
            }
            index = _read_record(self._project_dir(project_id) / _LEGACY_INDEX) or {}
            entries = index.get("entries") if index.get("schema") == LEGACY_INDEX_SCHEMA else []
            entries = [e for e in (entries or []) if isinstance(e, dict)]
            for e in entries:
                if previous is not None and e.get("path") == str(target) and \
                        e.get("state") == REGISTERED:
                    e["state"] = SUPERSEDED
            entries.append(entry)
            self._write_index(project_id, _LEGACY_INDEX, LEGACY_INDEX_SCHEMA, entries)
            return dict(entry)

    @staticmethod
    def _valid_run_dir(child: Path) -> bool:
        """A direct child of a runs base holding a run state whose `run_id`
        is the directory's name (validated by the product's own parser)."""
        from hoh.store import RunStore

        try:
            if not stat.S_ISDIR(os.lstat(child).st_mode):
                return False
            state_file = child / _RUN_STATE
            if not stat.S_ISREG(os.lstat(state_file).st_mode):
                return False
            state = RunStore._parse_state(state_file.read_text(encoding="utf-8"))
        except Exception:                       # unparseable proves nothing
            return False
        return state.run_id == child.name

    def _accepted_roots(self, project_id: str, kind: str) -> list[dict[str, Any]]:
        return [
            e for e in self._root_entries(project_id)
            if e.get("kind") == kind and e.get("state") in _ACCEPTED_ROOT_STATES
            and e.get("project_id") == project_id and self._root_intact(e)
        ]

    def _check_structure(self, project_id: str, target: Path, kind: str) -> None:
        if kind == "runs_base":
            states = []
            for child in sorted(target.iterdir()):
                if (child / _RUN_STATE).exists() or (child / _RUN_STATE).is_symlink():
                    states.append(child)
            if not states:
                raise StorageOwnershipError(f"{target} holds no <run_id>/{_RUN_STATE}")
            bad = [c.name for c in states if not self._valid_run_dir(c)]
            if bad:
                raise StorageOwnershipError(
                    f"{target}: run states that do not validate or name another run: {bad}"
                )
        elif kind == "target_clone":
            git_dir = target / ".git"
            if git_dir.is_symlink() or not git_dir.is_dir():
                raise StorageOwnershipError(f"{target}/.git is not a directory")
            for base in self._accepted_roots(project_id, "runs_base"):
                policy = _read_record(Path(base["path"]) / _PRESERVE_POLICY)
                named = policy.get("root") if policy else None
                if isinstance(named, str) and Path(named).resolve(strict=False) == target:
                    return
            raise StorageOwnershipError(
                f"no registered runs_base of {project_id!r} names {target} in its "
                f"{_PRESERVE_POLICY}"
            )
        elif kind == "linked_worktree":
            git_file = target / ".git"
            if git_file.is_symlink() or not git_file.is_file():
                raise StorageOwnershipError(f"{target}/.git is not a file")
            line = git_file.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                raise StorageOwnershipError(f"{target}/.git holds no gitdir line")
            gitdir = Path(line[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = target / gitdir
            gitdir = gitdir.resolve(strict=False)
            for clone in self._accepted_roots(project_id, "target_clone"):
                worktrees = Path(clone["path"]) / ".git" / "worktrees"
                if gitdir.parent == worktrees and gitdir.is_dir():
                    return
            raise StorageOwnershipError(
                f"{target}/.git does not point into a registered target_clone's "
                f".git/worktrees/<name>"
            )

    def _root_intact(self, entry: dict[str, Any]) -> bool:
        """The root directory is still the inode recorded for it."""
        try:
            st = os.lstat(entry["path"])
        except (OSError, KeyError, TypeError):
            return False
        return stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (
            entry.get("st_dev"), entry.get("st_ino")
        )

    # ------------------------------------------------------------------ owner_of

    def owner_of(self, path: os.PathLike[str] | str) -> Ownership:
        """Who owns `path`: `OWNED`, `PROTECTED` or `UNKNOWN`.

        **Consumers may act only on `OWNED`, and only on `Ownership.resolved`**
        (re-checked against `st_dev`/`st_ino` on their own descriptor), never
        on the path they passed in.

        The path is judged by its lexical form (absolute, normalised, symlinks
        *not* resolved), by its resolved form (`resolve(strict=True)`) and by
        every step in between. `OWNED` requires:

        * both forms strictly inside the **same** owned root;
        * no detour: every symlink met on the way sits inside that root and
          resolves inside it, and no `..` step leaves it once inside
          (`_walk_components`) -- a path that leaves the root and comes back
          is `UNKNOWN`;
        * the resolved object on the device recorded for the root, and every
          component strictly below the root on the root's recorded mount (a
          bind mount is a boundary even on the same `st_dev`);
        * not a regular file with more than one hard link (the other names
          may be foreign).

        A symlink inside an owned directory pointing out, a symlink outside
        pointing in, and a path that does not exist are all `UNKNOWN`. The
        trust root is the operator-registered project repository; see the
        module docstring.
        """
        try:
            lex = _lexical(path)
            real = Path(path).resolve(strict=True)
            st = os.stat(real)
        except (OSError, RuntimeError, ValueError, TypeError):
            return _unknown("does not exist or cannot be resolved")
        if not self._owner_valid():
            return _unknown(f"{self.state_root} carries no valid owner record")

        by_real = self._classify(real)
        verdict = by_real.ownership
        if verdict.status == UNKNOWN:
            return verdict
        if verdict.status == OWNED:
            refusal = self._owned_refusal(path, lex, real, st, by_real)
            if refusal:
                return _unknown(refusal)
        return Ownership(
            status=verdict.status, project_id=verdict.project_id,
            session_id=verdict.session_id, kind=verdict.kind, root=verdict.root,
            proof=verdict.proof, st_dev=st.st_dev, st_ino=st.st_ino, reason=verdict.reason,
            resolved=real,
        )

    def _owned_refusal(self, given: os.PathLike[str] | str, lex: Path, real: Path,
                       st: os.stat_result, by_real: _Verdict) -> str:
        """Why an `OWNED` verdict on `real` does not hold for the path as
        given, or "" if it holds. Each check is one rule of `owner_of`."""
        owned = by_real.ownership
        root = owned.root
        assert root is not None
        if lex != real:
            o_lex = self._classify(lex).ownership
            if not (o_lex.status == OWNED and o_lex.root == root
                    and o_lex.project_id == owned.project_id):
                return "the given path and its resolved path are not inside the same owned root"
        try:
            walked = _walk_components(given, root)
        except _LeftOwnedRoot as exc:
            return f"the path leaves its owned root on the way: {exc}"
        except (OSError, RuntimeError, ValueError):
            return "the path cannot be walked component by component"
        if walked != real:
            return "the path resolved differently on a second walk"
        try:
            if self._st_dev_reader(real) != by_real.root_dev:
                return "the resolved object lies on another device than its root"
            below = real.relative_to(root).parts
            for depth in range(1, len(below) + 1):
                mount = self._mount_id_reader(root.joinpath(*below[:depth]))
                if mount is None or by_real.root_mount is None or mount != by_real.root_mount:
                    return (f"{root.joinpath(*below[:depth])} lies on another mount than its "
                            f"root (a mount boundary below the owned root)")
        except Exception:                       # a reader that fails proves nothing
            return "the device or mount of the path cannot be read"
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            return "a regular file with more than one hard link; its other names may be foreign"
        return ""

    def _classify(self, p: Path) -> _Verdict:
        if p == self.state_root or p.is_relative_to(self.state_root):
            return self._classify_internal(p)
        return self._classify_external(p)

    def _protected(self, reason: str, *, project_id: str | None = None,
                   session_id: str | None = None, kind: str = "registry",
                   root: Path | None = None, proof: tuple[str, ...] = ()) -> _Verdict:
        return _Verdict(Ownership(
            status=PROTECTED, project_id=project_id, session_id=session_id, kind=kind,
            root=root or self.state_root, proof=proof or (str(self._owner_path),),
            reason=reason,
        ))

    def _classify_internal(self, p: Path) -> _Verdict:
        parts = p.relative_to(self.state_root).parts
        n = len(parts)
        if n == 0:
            return self._protected("the state root")
        head = parts[0]
        if head == _LEDGER:
            return self._protected("the registry ledger")
        if n == 1 and any(_is_record_or_version(head, r) for r in (_OWNER_FILE, _CONFIG_FILE)):
            return self._protected("an owner or configuration record")
        if head != _PROJECTS:
            return _Verdict(_unknown("not part of the registry layout"))
        if n == 1:
            return self._protected("the projects directory")
        project_id = parts[1]
        if n == 2 and project_id == _LOCK:
            return self._protected("the registry lock")
        if self._project_record(project_id) is None:
            return _Verdict(_unknown(f"project {project_id!r} is not registered"))
        pdir = self._project_dir(project_id)
        chain = (str(self._owner_path), str(pdir / _PROJECT_FILE))
        if n == 2:
            return self._protected("a project directory", project_id=project_id, proof=chain)
        sub = parts[2]
        if n == 3 and any(_is_record_or_version(sub, r) for r in _PROJECT_RECORDS):
            return self._protected("a project record", project_id=project_id, proof=chain)
        if sub == _SESSIONS:
            if n == 3:
                return self._protected("the sessions directory", project_id=project_id,
                                       proof=chain)
            if not _ID.match(parts[3]):
                return _Verdict(_unknown("not a session directory"))
            if n == 4 or (n == 5 and _is_record_or_version(parts[4], _LEASE)):
                return self._protected("a session directory or lease record",
                                       project_id=project_id, session_id=parts[3], proof=chain)
            return _Verdict(_unknown("not a registry record"))
        if sub in SCRATCH_KINDS:
            if n == 3:
                return self._protected("a scratch kind directory", project_id=project_id,
                                       proof=chain)
            session_id = parts[3]
            entry = self._scratch_entry(project_id, sub, session_id)
            if entry is None:
                legacy = self._legacy_verdict(project_id, p, chain)
                if legacy is not None:
                    return legacy
                return _Verdict(_unknown(
                    "no valid, indexed owner record at this scratch directory"
                ))
            root = pdir / sub / session_id
            proof = chain + (str(pdir / _SCRATCH_INDEX), str(root / _SCRATCH_RECORD))
            if n == 4 or (n == 5 and (parts[4] == _SCRATCH_RECORD
                                      or _is_temp_of(parts[4], _SCRATCH_RECORD))):
                return self._protected("a scratch directory, its record or a leftover "
                                       "temp file of it",
                                       project_id=project_id, session_id=session_id,
                                       kind=sub, root=root, proof=proof)
            return _Verdict(Ownership(
                status=OWNED, project_id=project_id, session_id=session_id, kind=sub,
                root=root, proof=proof, reason="inside an indexed scratch directory",
            ), root_dev=entry["st_dev"], root_mount=entry.get("mount_id"))
        return _Verdict(_unknown("not part of the registry layout"))

    def _legacy_verdict(self, project_id: str, p: Path, chain: tuple[str, ...]
                        ) -> _Verdict | None:
        """The verdict for `p` from the REGISTERED legacy roots of `project_id`,
        or None when no legacy root contains it. The root itself is PROTECTED,
        a path strictly inside an intact root is OWNED (session_id None), and
        a root that is no longer its recorded inode makes everything UNKNOWN."""
        for entry in self.legacy_roots(project_id, intact_only=False):
            root = Path(entry["path"])
            if not (p == root or p.is_relative_to(root)):
                continue
            if not self._root_intact(entry):
                return _Verdict(_unknown("the legacy root is no longer the inode recorded for it"))
            proof = chain + (str(self._project_dir(project_id) / _LEGACY_INDEX),)
            if p != root:
                first = p.relative_to(root).parts[0]
                bound = _bound_name(first)
                if bound not in entry["children"] or (
                        bound != first and os.path.lexists(root / bound)) or \
                        not _same_inode(root / first, (entry.get("child_ids") or {}).get(bound)):
                    return _Verdict(_unknown(
                        "not bound by the legacy anchor (a child added after the anchor's "
                        "survey, or a quarantine name beside its still-present child)"))
            if p == root:
                return self._protected("a registered legacy root", project_id=project_id,
                                       kind=entry["kind"], root=root, proof=proof)
            return _Verdict(Ownership(
                status=OWNED, project_id=project_id, session_id=None, kind=entry["kind"],
                root=root, proof=proof, reason="inside a registered legacy root",
            ), root_dev=entry["st_dev"], root_mount=entry.get("mount_id"))
        return None

    def _classify_external(self, p: Path) -> _Verdict:
        for project_id in self._registered_projects():
            for entry in self._root_entries(project_id):
                if entry.get("state") not in _ACCEPTED_ROOT_STATES:
                    continue
                if not isinstance(entry.get("path"), str):
                    continue
                if entry.get("project_id") != project_id:
                    continue                    # an entry names its project, or it is void
                root = Path(entry["path"])
                if not (p == root or p.is_relative_to(root)):
                    continue
                if not self._root_intact(entry):
                    return _Verdict(_unknown("the root is no longer the inode recorded for it"))
                pdir = self._project_dir(project_id)
                proof = (str(self._owner_path), str(pdir / _PROJECT_FILE),
                         str(pdir / _ROOT_INDEX))
                kind = entry.get("kind")
                if p == root:
                    return self._protected("a registered external root", project_id=project_id,
                                           kind=kind, root=root, proof=proof)
                if kind == "runs_base":
                    first = p.relative_to(root).parts[0]
                    if first != _RUNS_BASE_ARENAS and not self._valid_run_dir(root / first):
                        return _Verdict(_unknown(
                            "below a runs base, only _arenas/ and validated run directories "
                            "are owned"
                        ))
                return _Verdict(Ownership(
                    status=OWNED, project_id=project_id, session_id=None, kind=kind,
                    root=root, proof=proof, reason="inside a registered external root",
                ), root_dev=entry["st_dev"], root_mount=entry.get("mount_id"))
        return _Verdict(_unknown("not under any registered root"))


__all__ = [
    "LIVE_HOLDER",
    "OWNED",
    "PROTECTED",
    "UNKNOWN",
    "Ownership",
    "SessionLease",
    "StorageConfig",
    "StorageOwnershipError",
    "StorageRegistry",
]
