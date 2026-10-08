"""Tier classification of owned storage (STORAGE-D, P5-28).

`classify_tier(path, *, registry)` answers one question for one path: may
VERIFIED_RELOCATION ever take it, and on what proof? The answer is a
`TierDecision` with the tier and the evidence that put it there:

* not `OWNED` by the registry (STORAGE-A) -- `UNKNOWN`, `PROTECTED`, another
  project's path seen from nowhere -- is **refused** (`UNKNOWN_OWNERSHIP`) and
  never gets a tier. The registry's verdict is per path, so every descendant
  of the item is asked as well (`inventory`): one entry that is not `OWNED` by
  the same project, one non-directory with ``st_nlink > 1`` (its other
  names may be foreign), or one entry on another device than the item
  itself (``FOREIGN_DEVICE``: a target or anything else mounted inside an
  item is never in a removal set, Revision 9 item 5) refuses the whole item.
* `HOT` -- a live (or unreadable) registry lease of the session that owns the
  item; a process of the same uid whose cwd or an open descriptor lies inside;
  a reference from an open finding, current evidence, a succession or recovery
  record, or an unmerged candidate. A run id named in an open finding (or a
  succession record, or an unmerged candidate's ref name) makes every path of
  that run `HOT`. A source that cannot be read makes the item `HOT` -- for the
  open findings that includes an unlistable runs base or runs scratch, a run
  whose ``state.json`` cannot be stat'ed or opened, and an unreadable QA
  answer (each may name any path of the project). A path strictly inside a
  run directory (runs base or ``runs`` scratch) is `HOT` while that run is
  not finished in the sense of `run_finished`; the run directory itself is
  then never given a proof (it stays `WARM`).
* `VERIFIED_DUPLICATE` -- every entry of the item is byte-identical to the
  same relative path of a retained canonical original of the same project:
  a tracked, clean file of the canonical repository (pinned by a
  ``refs/vh-retain/<id>`` ref before anything is removed), or a file in a
  registered ``runs_base`` listed with its SHA-256 in a committed retention
  manifest (``dogfood/storage-retention/<UTC>.json``) whose run is finished.
* `REPRODUCIBLE_SCRATCH` -- a clone whose working tree, refs and ``.git``
  hold nothing that the canonical repository does not, or an extract root of
  STORAGE-C2 whose bundle has a committed receipt and still verifies.
* `COLD` -- terminal and unreferenced; `WARM` -- everything else.

**Age is never read.** No mtime, ctime or atime enters any decision.

**No caller-supplied truth.** The function takes no reference, lease,
finding or proof sets; it collects them itself through `EvidenceSources`,
each from a named source that fails closed. Tests inject the *sources* (a
registry, a repository, a fake ``/proc``), never the result sets.

Named sources (all read-only):

* leases: ``<state_root>/projects/<p>/sessions/*/LEASE.json`` through the
  registry (`StorageRegistry._read_lease`, `lease_is_live`); a lease file that
  cannot be read counts as live.
* open findings: the latest QA answer of every run of the project
  (``<run>/answers/i<N>-a<M>-qa.json``: a verdict that is not PASS or a
  non-empty ``open_gaps``); the open-item records of the canonical
  repository's ``DOGFOOD_LEDGER.md`` (lines under a heading that says
  "offen"/"open", lines with the word ``OPEN`` and unchecked ``- [ ]``
  boxes); review reports in the project's registered ``reviews/`` scratch.
* current evidence: files committed at ``HEAD`` under
  ``dogfood/**/run-evidence/`` and ``runs/`` of the canonical repository.
* succession and recovery records: ``SUCCESSION.json`` files committed at
  ``HEAD`` and the names of ``refs/preserve/*``.
* unmerged candidates: ``refs/imports/*`` and ``refs/preserve/*`` not
  reachable from the canonical repository's default branch (its ``HEAD``).
* processes: ``/proc/<pid>/cwd`` and ``/proc/<pid>/fd/*`` of processes of
  the caller's own uid only (the uid is taken from the ``/proc/<pid>``
  directory's owner before any link is read); nothing else is read there.

Git runs with ``-C``, the scrubbed environment of `archive_bundle._git_env`
plus ``GIT_OPTIONAL_LOCKS=0`` (`_git_env`: judging writes nothing, not even
an index refresh), ``-c core.fsmonitor=false -c core.hooksPath=/dev/null`` and a timeout. The
canonical repository is the one permitted read-only source outside the
project's registered namespace (besides ``/proc``); this module writes
nothing anywhere and removes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from hoh import archive_bundle as ab
from hoh.storage_registry import LEGACY_KINDS, OWNED, SCRATCH_KINDS, Ownership, _bound_name

HOT = "HOT"
WARM = "WARM"
COLD = "COLD"
REPRODUCIBLE_SCRATCH = "REPRODUCIBLE_SCRATCH"
VERIFIED_DUPLICATE = "VERIFIED_DUPLICATE"
TIERS = (HOT, WARM, COLD, REPRODUCIBLE_SCRATCH, VERIFIED_DUPLICATE)
ELIGIBLE = frozenset({VERIFIED_DUPLICATE, REPRODUCIBLE_SCRATCH})

#: The refusal of a path the registry does not answer OWNED for.
UNKNOWN_OWNERSHIP = "UNKNOWN_OWNERSHIP"

#: An entry on another device than its item (Revision 9 item 5).
FOREIGN_DEVICE = "FOREIGN_DEVICE"

#: Notes a caller reports instead of relocating.
AWAITING_COMMITTED_RECEIPT = "AWAITING_COMMITTED_RECEIPT"
#: A legacy item whose name or content is not the anchor's binding (STORAGE-L).
LEGACY_UNBOUND = "LEGACY_UNBOUND"
UNVERIFIED_EXTRACT = "UNVERIFIED_EXTRACT"

RETENTION_DIR = "dogfood/storage-retention"
RETENTION_SCHEMA = "veriharness.storage-retention/1"
LEDGER_FILE = "DOGFOOD_LEDGER.md"
SUCCESSION_FILE = "SUCCESSION.json"
RETAIN_NAMESPACE = "refs/vh-retain/"
PROOF_SCHEMA = "veriharness.tier-proof/1"

_GIT_FLAGS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
_GIT_TIMEOUT = 120.0
_CHUNK = 1 << 20

#: An absolute path mentioned in a text (JSON, Markdown, logs).
_PATH_TEXT = re.compile(r"(?<![\w.~-])(/[A-Za-z0-9._@+%=,:~/-]+)")
#: A VeriHarness run id (``20261002t0300z-spark2``).
_RUN_ID = re.compile(r"(?<![0-9a-z])(\d{8}t\d{4,6}z-[a-z0-9][a-z0-9._-]*)")
_QA_ANSWER = re.compile(r"^i(\d+)-a(\d+)-qa\.json$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_OPEN_HEADING = re.compile(r"offen|open", re.I)
_OPEN_LINE = re.compile(r"\bOPEN\b|^\s*[-*]\s+\[ \]")
_REVIEW_SUFFIXES = (".md", ".json", ".txt")
_REVIEW_MAX_BYTES = 4 << 20
_REVIEW_DEPTH = 2
_STATE_FILE = "state.json"


class SourceUnreadable(Exception):
    """A named evidence source could not be read; the item is `HOT`."""


class TargetTimeout(Exception):
    """A call that touches an archive target ran longer than the timeout."""


def bounded(fn: Callable[[], Any], timeout: float, what: str) -> Any:
    """Run `fn` in a daemon thread and wait at most `timeout` seconds. A call
    that does not return in time raises `TargetTimeout`; the thread is left
    to finish on its own (a hung drvfs call cannot be cancelled), so the
    caller is never blocked by it."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:            # handed to the caller
            box["error"] = exc

    thread = threading.Thread(target=run, name=f"vh-bounded-{what}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TargetTimeout(f"{what} ran longer than {timeout} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Refs:
    """What a source references: absolute paths, run ids, commits."""

    paths: frozenset[str] = frozenset()
    run_ids: frozenset[str] = frozenset()
    commits: frozenset[str] = frozenset()

    def __or__(self, other: "Refs") -> "Refs":
        return Refs(self.paths | other.paths, self.run_ids | other.run_ids,
                    self.commits | other.commits)


@dataclass(frozen=True)
class Entry:
    """One entry of an item, judged by the registry. `rel` is POSIX, relative
    to the item; `sha256` is set for regular files only."""

    rel: str
    type: str
    mode: int
    size: int
    sha256: str | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class TierDecision:
    """The answer of `classify_tier`.

    `tier` is None exactly when `refused` is set (`UNKNOWN_OWNERSHIP`).
    `reasons` is the evidence that put the item in its tier; `notes` says
    why a stronger tier did not hold (e.g. ``untracked_files``,
    ``ignored_files``, ``stash``) and carries `AWAITING_COMMITTED_RECEIPT` or
    `UNVERIFIED_EXTRACT` where they apply. `proof` and `proof_digest` are the
    proof identity a `VERIFIED_DUPLICATE` or `REPRODUCIBLE_SCRATCH` relied
    on. `bytes` is the reclaimable size (regular files, each counted once).
    """

    path: str
    tier: str | None
    refused: str | None = None
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    project_id: str | None = None
    ownership: Ownership | None = field(default=None, repr=False, compare=False)
    proof: Mapping[str, Any] | None = field(default=None, repr=False)
    proof_digest: str | None = None
    bytes: int = 0
    entries: tuple[Entry, ...] = field(default=(), repr=False, compare=False)

    @property
    def eligible(self) -> bool:
        return self.tier in ELIGIBLE and self.refused is None

    def eligibility(self) -> dict[str, Any]:
        """The record STORAGE-C2's ``place(..., kind="reclaim-review")``
        requires. Produced only here (Draft addition 2): the class, the proof
        digest and ``unique: false``. Refused for anything not eligible."""
        if not self.eligible or not self.proof_digest:
            raise ValueError(f"{self.path} is {self.tier or self.refused}; no eligibility")
        return {"classification": self.tier, "proof_digest": self.proof_digest,
                "unique": False}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str).encode("ascii")


def proof_digest(proof: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of a proof."""
    return hashlib.sha256(_canonical(proof)).hexdigest()


def _git_env() -> dict:
    """The environment of every git subprocess this module starts:
    `archive_bundle`'s scrubbed one plus ``GIT_OPTIONAL_LOCKS=0``.

    Judging takes no optional lock and so writes nothing: the only write to
    the canonical repository is the retain ref (Revision 2 item 5), and
    judging an item must not change the inventory its plan recorded and its
    quarantine is compared with (Revision 2 item 8, Revision 8 item 1) -- a
    ``git status`` or ``git diff`` would otherwise refresh a clone's
    ``.git/index`` opportunistically."""
    env = ab._git_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(repo: Path | str, *args: str, timeout: float = _GIT_TIMEOUT
         ) -> subprocess.CompletedProcess:
    """``git -C <repo>`` with the scrubbed environment and no optional locks
    (`_git_env`), no fsmonitor, no hooks and a timeout. Raises
    `SourceUnreadable` when git cannot run at all."""
    command = ["git", *ab._QUIET, *_GIT_FLAGS, "-C", os.fspath(repo), *args]
    try:
        return subprocess.run(command, capture_output=True, env=_git_env(),
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceUnreadable(f"git {' '.join(args[:2])} in {repo}: {exc}") from exc


def _git_ok(repo: Path | str, *args: str) -> bytes:
    done = _git(repo, *args)
    if done.returncode != 0:
        raise SourceUnreadable(f"git {' '.join(args[:2])} in {repo} failed: "
                               + done.stderr.decode("utf-8", "replace").strip()[:300])
    return done.stdout


def sha256_file(path: str | os.PathLike[str] | bytes) -> str:
    """SHA-256 of a regular file, read without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(fd)


def _blob_id(data: bytes, algo: str) -> str:
    header = b"blob " + str(len(data)).encode() + b"\0"
    return hashlib.new(algo, header + data).hexdigest()


def _read_bytes(path: str | os.PathLike[str]) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def text_refs(text: str) -> Refs:
    """The absolute paths and run ids a text mentions."""
    paths = set()
    for match in _PATH_TEXT.finditer(text):
        raw = match.group(1).rstrip(".,:;")
        if raw.count("/") >= 2:
            paths.add(os.path.normpath(raw))
    runs = {m.group(1).rstrip("._-") for m in _RUN_ID.finditer(text.lower())}
    return Refs(frozenset(paths), frozenset(runs))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def run_components(resolved: Path, root: Path | None) -> tuple[str, ...]:
    """The path components a run id is matched against: the owned root's
    own name and everything below it."""
    if root is None:
        return resolved.parts
    try:
        rel = resolved.relative_to(root.parent)
    except ValueError:
        return resolved.parts
    return rel.parts


def _names_run(components: Iterable[str], run_id: str) -> bool:
    for part in components:
        part = part.lower()
        if part == run_id or (part.startswith(run_id) and part[len(run_id)] in "-_."):
            return True
    return False


def _hits(refs: Refs, forms: Iterable[str], root: str | None,
          components: tuple[str, ...]) -> list[str]:
    """Which of `refs` reference the item: a path equal to it or inside it,
    a path it lies inside (only inside its own owned root -- a mention of
    ``/home`` references nothing), or a run id naming one of its components.
    """
    found = []
    forms = [f for f in forms if f]
    for ref in sorted(refs.paths):
        for item in forms:
            if _inside(ref, item) or (root and _inside(item, ref) and _inside(ref, root)):
                found.append(ref)
                break
    for run_id in sorted(refs.run_ids):
        if _names_run(components, run_id):
            found.append("run " + run_id)
    return found


# --------------------------------------------------------------------------- #
# Inventory: every descendant asked of the registry
# --------------------------------------------------------------------------- #


class _NotOwned(Exception):
    pass


#: The ``lstat`` of `inventory` (a seam: a test makes one entry report
#: another ``st_dev``).
_lstat = os.lstat


def inventory(path: Path, registry, project_id: str, *,
              inodes: dict[str, tuple[int, int]] | None = None) -> tuple[Entry, ...]:
    """Every entry below `path` (not following symlinks), each judged by
    `registry.owner_of`: `OWNED` by `project_id`, no non-directory with more
    than one hard link, and on the same device as `path` itself (an entry
    with another ``st_dev`` is ``FOREIGN_DEVICE``: something mounted inside
    the item). Raises `_NotOwned` on the first that is not.

    With `inodes`, the same walk records ``(st_dev, st_ino)`` of every entry
    under its ``rel`` (and of `path` itself under ``""``) -- beside the
    entries, never in them, so `entries_digest` keeps its form."""
    entries: list[Entry] = []
    stack = [""]
    base = os.fspath(path)
    try:
        top = _lstat(base)
    except OSError as exc:
        raise _NotOwned(f"cannot stat {base}: {exc}") from exc
    if inodes is not None:
        inodes[""] = (top.st_dev, top.st_ino)
    while stack:
        rel_dir = stack.pop()
        full_dir = os.path.join(base, rel_dir) if rel_dir else base
        try:
            with os.scandir(full_dir) as listing:
                names = sorted(item.name for item in listing)
        except OSError as exc:
            raise _NotOwned(f"cannot list {full_dir}: {exc}") from exc
        for name in names:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(base, rel)
            try:
                st = _lstat(full)
            except OSError as exc:
                raise _NotOwned(f"cannot stat {full}: {exc}") from exc
            if st.st_dev != top.st_dev:
                raise _NotOwned(f"{FOREIGN_DEVICE}: {full} lies on device {st.st_dev}, not on "
                                f"the item's device {top.st_dev} (something is mounted inside "
                                f"the item)")
            if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
                raise _NotOwned(f"{full} has {st.st_nlink} hard links; its other names "
                                f"may be foreign (UNKNOWN)")
            owner = registry.owner_of(full)
            if owner.status != OWNED or owner.project_id != project_id:
                raise _NotOwned(f"{full} is {owner.status} "
                                f"({owner.project_id or '-'}): {owner.reason}")
            if inodes is not None:
                inodes[rel] = (st.st_dev, st.st_ino)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISREG(st.st_mode):
                entries.append(Entry(rel, "file", mode, st.st_size, sha256_file(full)))
            elif stat.S_ISDIR(st.st_mode):
                entries.append(Entry(rel, "dir", mode, 0))
                stack.append(rel)
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(full)
                entries.append(Entry(rel, "symlink", mode, len(os.fsencode(target)),
                                     link_target=target))
            else:
                raise _NotOwned(f"{full} is not a file, directory or symlink")
    return tuple(sorted(entries, key=lambda e: e.rel))


def entries_digest(entries: Iterable[Entry]) -> str:
    return hashlib.sha256(_canonical([e.__dict__ for e in entries])).hexdigest()


# --------------------------------------------------------------------------- #
# Process probe (/proc, same uid only)
# --------------------------------------------------------------------------- #


def _proc_dir_uid(pid_dir: str) -> int:
    return os.stat(pid_dir).st_uid


def processes_inside(path: str, *, proc_root: str = "/proc",
                     getuid: Callable[[], int] = os.getuid,
                     proc_uid: Callable[[str], int] = _proc_dir_uid) -> list[str]:
    """Processes of the caller's own uid whose cwd or an open descriptor lies
    inside `path` (``"<pid> cwd -> <target>"`` / ``"<pid> fd/<n> -> ..."``).

    Only ``/proc/<pid>/cwd`` and ``/proc/<pid>/fd/*`` are read, and only for
    a pid whose ``/proc/<pid>`` directory belongs to the caller's uid -- the
    uid is decided before any link of that process is read. A link that
    cannot be read proves nothing and is skipped (the process may have
    ended). Raises `SourceUnreadable` if ``/proc`` itself cannot be listed.
    """
    own = getuid()
    root = os.path.normpath(path)
    try:
        pids = [name for name in os.listdir(proc_root) if name.isdigit()]
    except OSError as exc:
        raise SourceUnreadable(f"{proc_root} cannot be listed: {exc}") from exc
    found = []
    for pid in sorted(pids, key=int):
        pid_dir = os.path.join(proc_root, pid)
        try:
            if proc_uid(pid_dir) != own:
                continue
        except OSError:
            continue
        links = [("cwd", os.path.join(pid_dir, "cwd"))]
        try:
            fds = os.listdir(os.path.join(pid_dir, "fd"))
        except OSError:
            fds = []
        links += [(f"fd/{n}", os.path.join(pid_dir, "fd", n)) for n in sorted(fds)]
        for label, link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if target.endswith(" (deleted)"):
                target = target[: -len(" (deleted)")]
            if _inside(os.path.normpath(target), root):
                found.append(f"{pid} {label} -> {target}")
    return found


# --------------------------------------------------------------------------- #
# Run states
# --------------------------------------------------------------------------- #


def run_finished(run_dir: Path) -> tuple[bool, str]:
    """Revision 5a item 1: a run counts as finished when its condition is
    FAILED or CANCELLED, or its stage is CHECKPOINTED or READY_FOR_DELIVERY
    -- and, in every branch, no live process holds it (``process.json``
    absent or ``ended``, and no live holder of ``.process.lock`` as
    `RunStore.running_process` probes it). An unreadable state is not
    finished."""
    from hoh.contracts import Condition, Stage
    from hoh.store import RunStore

    try:
        state = RunStore._parse_state((run_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except Exception as exc:                    # unparseable proves nothing
        return False, f"run state unreadable: {exc}"
    terminal = (state.condition in (Condition.FAILED, Condition.CANCELLED)
                or state.stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY))
    if not terminal:
        return False, f"run {state.run_id} is {state.condition}/{state.stage}"
    marker = run_dir / "process.json"
    if os.path.lexists(marker):
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"process.json unreadable: {exc}"
        if not isinstance(record, dict) or record.get("state") != "ended":
            return False, "process.json does not say ended"
    try:
        holder = RunStore(run_dir.parent, run_dir.name).running_process()
    except Exception as exc:                    # a probe that fails proves nothing
        return False, f"process probe failed: {exc}"
    if holder is not None:
        return False, f"a live process holds the run ({holder.get('pid', '?')})"
    return True, f"run {state.run_id} is {state.condition}/{state.stage}, no live process"


# --------------------------------------------------------------------------- #
# Evidence sources
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Original:
    """A retained original one file of a duplicate may equal."""

    path: str
    source: str                     # "git" or "retention"
    digest: str                     # git blob id, or SHA-256
    mode: str = ""                  # git mode for git originals


class EvidenceSources:
    """The named sources `classify_tier` reads, for one project.

    Every reader caches its answer for the lifetime of the instance; `fresh()`
    returns a new instance with the same configuration and an empty cache, so
    a plan and every re-check of an execute read the sources anew.

    Injectable for tests: `canonical_repo` (default: the registry's project
    root), `proc_root`, `getuid` and `proc_uid` (the process probe), `git_timeout`
    and `target_timeout` (the payload re-hash of an extract root's bundle on
    its archive target). A reader raises `SourceUnreadable`; the caller makes the
    item `HOT`.
    """

    def __init__(self, registry, project_id: str, *, canonical_repo: Path | str | None = None,
                 proc_root: str = "/proc", getuid: Callable[[], int] = os.getuid,
                 proc_uid: Callable[[str], int] = _proc_dir_uid,
                 git_timeout: float = _GIT_TIMEOUT,
                 target_timeout: float = 300.0) -> None:
        self.registry = registry
        self.project_id = project_id
        self._canonical = canonical_repo
        self.proc_root = proc_root
        self.getuid = getuid
        self.proc_uid = proc_uid
        self.git_timeout = git_timeout
        self.target_timeout = target_timeout
        self._cache: dict[str, Any] = {}

    def fresh(self) -> "EvidenceSources":
        return EvidenceSources(self.registry, self.project_id,
                               canonical_repo=self._canonical, proc_root=self.proc_root,
                               getuid=self.getuid, proc_uid=self.proc_uid,
                               git_timeout=self.git_timeout,
                               target_timeout=self.target_timeout)

    def _cached(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self._cache:
            try:
                self._cache[key] = ("ok", compute())
            except SourceUnreadable as exc:
                self._cache[key] = ("err", exc)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self._cache[key] = ("err", SourceUnreadable(f"{key}: {exc}"))
        kind, value = self._cache[key]
        if kind == "err":
            raise value
        return value

    # -- the project and its namespace -------------------------------------- #

    def canonical_root(self) -> Path:
        def compute() -> Path:
            if self._canonical is not None:
                return Path(self._canonical).resolve()
            record = self.registry._project_record(self.project_id)
            if not record:
                raise SourceUnreadable(f"project {self.project_id!r} is not registered")
            return Path(record["project_root"]).resolve()
        return self._cached("canonical_root", compute)

    def scratch_dirs(self, kinds: Iterable[str] = SCRATCH_KINDS) -> list[tuple[str, str, Path]]:
        """``(kind, session_id, directory)`` of every indexed scratch dir of the
        project whose record and inode still match (the registry's index)."""
        wanted = set(kinds)
        out = []
        for entry in self.registry._scratch_entries(self.project_id):
            kind, session = entry.get("kind"), entry.get("session_id")
            if kind not in wanted or not isinstance(session, str):
                continue
            if self.registry._scratch_entry(self.project_id, kind, session) is None:
                continue
            out.append((kind, session, self.registry.state_root / entry["path"]))
        return sorted(out)

    def runs_bases(self) -> list[Path]:
        return sorted(Path(e["path"]) for e in
                      self.registry._accepted_roots(self.project_id, "runs_base"))

    def run_dirs(self) -> list[Path]:
        """Run directories of the project: children of registered runs bases
        and of ``runs`` scratch that hold a ``state.json``.

        Fails closed: a runs base or runs scratch that cannot be listed, or a
        child whose ``state.json`` cannot be stat'ed or opened, raises
        `SourceUnreadable` -- an unreadable run may hold an open finding about
        any path of the project, so it is never skipped as if it were empty.
        A child without a ``state.json`` (``_arenas``, a file) is no run."""
        out = []
        parents = self.runs_bases() + [d for _, _, d in self.scratch_dirs(("runs",))]
        for parent in parents:
            try:
                with os.scandir(parent) as listing:
                    children = sorted(item.name for item in listing)
            except OSError as exc:
                raise SourceUnreadable(f"runs directory {parent} cannot be listed: "
                                       f"{exc}") from exc
            for name in children:
                child = parent / name
                state = child / _STATE_FILE
                try:
                    if stat.S_ISLNK(os.lstat(child).st_mode):
                        continue
                    st = os.lstat(state)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as exc:
                    raise SourceUnreadable(f"run {child}: {state.name} cannot be "
                                           f"stat'ed: {exc}") from exc
                if not stat.S_ISREG(st.st_mode):
                    raise SourceUnreadable(f"run {child}: {state.name} is not a regular file")
                try:
                    os.close(os.open(state, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC))
                except OSError as exc:
                    raise SourceUnreadable(f"run {child}: {state.name} cannot be read: "
                                           f"{exc}") from exc
                out.append(child)
        return out

    # -- leases ---------------------------------------------------------- #

    def live_sessions(self) -> frozenset[str]:
        """Sessions of the project whose registry lease is live -- or cannot
        be read (an unreadable lease cannot be proven dead)."""
        def compute() -> frozenset[str]:
            sessions = self.registry._project_dir(self.project_id) / "sessions"
            live = set()
            try:
                with os.scandir(sessions) as listing:
                    names = [item.name for item in listing]
            except FileNotFoundError:
                return frozenset()
            for name in names:
                if not os.path.lexists(sessions / name / "LEASE.json"):
                    continue
                lease = self.registry._read_lease(self.project_id, name)
                if lease is None or self.registry.lease_is_live(lease):
                    live.add(name)
            return frozenset(live)
        return self._cached("live_sessions", compute)

    # -- open findings ------------------------------------------------------ #

    def open_findings(self) -> Refs:
        return self._cached("open_findings", lambda: (
            self._qa_findings() | self._ledger_findings() | self._review_findings()
            | self._legacy_findings()))

    def legacy_roots(self) -> list[Path]:
        """The intact REGISTERED legacy roots of the project (LEGACY_INDEX)."""
        reader = getattr(self.registry, "legacy_roots", None)
        if reader is None:
            return []
        return sorted(Path(e["path"]) for e in reader(self.project_id))

    def committed_dogfood_blobs(self) -> frozenset[str]:
        """The blob ids of every file committed at HEAD under ``dogfood/``."""
        def compute() -> frozenset[str]:
            out = _git_ok(self.canonical_root(), "ls-tree", "-r", "-z", "--full-tree",
                          "HEAD", "--", "dogfood")
            ids = set()
            for record in out.split(b"\0"):
                meta = record.partition(b"\t")[0].split()
                if len(meta) == 3 and meta[1] == b"blob":
                    ids.add(meta[2].decode())
            return frozenset(ids)
        return self._cached("committed_dogfood_blobs", compute)

    def legacy_naming(self, root: Path) -> list[tuple[str, str]]:
        """``(file, blob)`` of every file committed at HEAD under ``dogfood/``
        whose text names `root` in any spelling (`legacy_root_pattern`)."""
        def compute() -> list[tuple[str, str]]:
            repo = self.canonical_root()
            forms = _root_spellings(root)
            args = ["grep", "-l", "-z", "-F"]
            for form in forms:
                args += ["-e", form]
            done = _git(repo, *args, "HEAD", "--", "dogfood")
            if done.returncode not in (0, 1):
                raise SourceUnreadable(f"git grep in {repo} failed")
            pattern = legacy_root_pattern(root)
            out = []
            for name in done.stdout.split(b"\0"):
                if not name:
                    continue
                file = os.fsdecode(name).partition(":")[2]
                blob = _git_ok(repo, "rev-parse", f"HEAD:{file}").decode().strip()
                data = _git_ok(repo, "cat-file", "blob", blob)
                if pattern.search(data.decode("utf-8", "replace")):
                    out.append((file, blob))
            return sorted(out)
        return self._cached(f"legacy_naming:{root}", compute)

    def _legacy_findings(self) -> Refs:
        """Briefs, reports and notes not yet preserved, near a registered
        legacy root: every regular ``*.md``/``*.json``/``*.txt`` file
        (1) directly in the root's parent ``reviews/``, (2) directly in the
        session dir above it, and (3) directly in each directory of
        ``reviews/`` (every reviewer dir, adopted or not) -- each unless it is
        byte-identical to a blob committed at HEAD under ``dogfood/``. Each
        contributes its `text_refs` and every registered root its
        `legacy_root_pattern` matches. A file directly inside a registered
        root R contributes nothing about R itself (its own bound notes), but
        everything about the others. An unlistable directory or unreadable
        file raises (HOT). Deeper files are not read."""
        refs = Refs()
        roots = self.legacy_roots()
        if not roots:
            return refs
        committed = self.committed_dogfood_blobs()
        algo = self.object_format()
        parents = {root.parent for root in roots}
        pending = sorted(parents | {parent.parent for parent in parents})
        done: set[Path] = set()
        while pending:
            directory = pending.pop()
            if directory in done:
                continue
            done.add(directory)
            try:
                with os.scandir(directory) as listing:
                    items = sorted(listing, key=lambda e: e.name)
            except OSError as exc:
                raise SourceUnreadable(f"legacy dir {directory} cannot be listed: {exc}") from exc
            if directory in parents:
                pending += [Path(e.path) for e in items if e.is_dir(follow_symlinks=False)]
            own = directory if directory in roots else None
            for item in items:
                if not item.name.endswith(_REVIEW_SUFFIXES) or \
                        not item.is_file(follow_symlinks=False):
                    continue
                try:
                    data = _read_bytes(item.path)
                except OSError as exc:
                    raise SourceUnreadable(f"legacy brief {item.path} unreadable: {exc}") from exc
                if _blob_id(data, algo) in committed:
                    continue
                text = data.decode("utf-8", "replace")
                found = text_refs(text)
                exempt = own is not None and _bound_unchanged_file(
                    self.registry, Path(item.path), data)
                if exempt:
                    found = Refs(paths=frozenset(p for p in found.paths
                                                 if not _inside(p, os.fspath(own))),
                                 run_ids=found.run_ids, commits=found.commits)
                refs |= found
                for root in roots:
                    if (root != own or not exempt) and legacy_root_pattern(root).search(text):
                        refs |= Refs(paths=frozenset({os.fspath(root)}))
        return refs

    def commit_trees(self) -> dict[str, str]:
        """``{tree id: commit}`` of every commit reachable from the canonical
        repository's refs (``log --all``; reflog entries do not count)."""
        def compute() -> dict[str, str]:
            out = _git_ok(self.canonical_root(), "log", "--all", "--format=%H %T")
            trees: dict[str, str] = {}
            for line in out.decode().splitlines():
                commit, _, tree = line.partition(" ")
                if commit and tree:
                    trees.setdefault(tree, commit)
            return trees
        return self._cached("commit_trees", compute)

    def _qa_findings(self) -> Refs:
        refs = Refs()
        for run_dir in self.run_dirs():
            answers = run_dir / "answers"
            try:
                names = os.listdir(answers)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SourceUnreadable(f"QA answers of {run_dir} cannot be listed: "
                                       f"{exc}") from exc
            ranked = sorted((int(m.group(1)), int(m.group(2)), name) for name in names
                            if (m := _QA_ANSWER.match(name)))
            if not ranked:
                continue
            latest = answers / ranked[-1][2]
            try:
                text = latest.read_text(encoding="utf-8")
                answer = json.loads(text)
                verdicts = answer.get("verdicts") or []
                gaps = answer.get("open_gaps") or []
                is_open = bool(gaps) or any(
                    not isinstance(v, dict) or v.get("outcome") != "PASS" for v in verdicts)
            except (OSError, ValueError, AttributeError) as exc:
                # An unreadable answer may name any path of the project.
                raise SourceUnreadable(f"QA answer {latest} unreadable: {exc}") from exc
            if is_open:
                refs |= text_refs(text) | Refs(run_ids=frozenset({run_dir.name.lower()}))
        return refs

    def _ledger_findings(self) -> Refs:
        ledger = self.canonical_root() / LEDGER_FILE
        if not os.path.lexists(ledger):
            return Refs()
        try:
            text = ledger.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SourceUnreadable(f"{ledger} unreadable: {exc}") from exc
        records = []
        open_level = 0
        for line in text.splitlines():
            heading = _HEADING.match(line)
            if heading:
                level = len(heading.group(1))
                if open_level and level <= open_level:
                    open_level = 0
                if _OPEN_HEADING.search(heading.group(2)):
                    open_level = level
                    records.append(line)
                continue
            if open_level or _OPEN_LINE.search(line):
                records.append(line)
        return text_refs("\n".join(records))

    def _review_findings(self) -> Refs:
        refs = Refs()
        for _, _, directory in self.scratch_dirs(("reviews",)):
            for report in _report_files(directory):
                try:
                    if os.lstat(report).st_size > _REVIEW_MAX_BYTES:
                        continue
                    refs |= text_refs(report.read_text(encoding="utf-8", errors="replace"))
                except OSError as exc:
                    raise SourceUnreadable(f"review report {report} unreadable: {exc}") from exc
        return refs

    # -- current evidence, succession, unmerged candidates ------------------ #

    def _committed(self, pathspecs: Iterable[str]) -> list[tuple[str, bytes]]:
        repo = self.canonical_root()
        listed = _git_ok(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", *pathspecs)
        out = []
        for record in listed.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            parts = meta.split()
            if len(parts) != 3 or parts[1] != b"blob":
                continue
            out.append((os.fsdecode(name), parts[2].decode()))
        if not out:
            return []
        batch = subprocess.run(
            ["git", *ab._QUIET, *_GIT_FLAGS, "-C", os.fspath(repo), "cat-file", "--batch"],
            input=b"".join(oid.encode() + b"\n" for _, oid in out), capture_output=True,
            env=_git_env(), timeout=self.git_timeout, check=False)
        if batch.returncode != 0:
            raise SourceUnreadable("git cat-file --batch failed in " + os.fspath(repo))
        blobs, data, pos = [], batch.stdout, 0
        for name, oid in out:
            end = data.index(b"\n", pos)
            header = data[pos:end].split()
            if len(header) != 3 or header[0].decode() != oid:
                raise SourceUnreadable(f"git cat-file answered {header!r} for {name}")
            size = int(header[2])
            blobs.append((name, data[end + 1:end + 1 + size]))
            pos = end + 1 + size + 1
        return blobs

    def current_evidence(self) -> Refs:
        def compute() -> Refs:
            refs = Refs()
            for name, blob in self._committed(["runs", "dogfood"]):
                parts = name.split("/")
                if parts[0] == "runs" or (parts[0] == "dogfood" and "run-evidence" in parts):
                    refs |= Refs(paths=text_refs(blob.decode("utf-8", "replace")).paths)
            return refs
        return self._cached("current_evidence", compute)

    def succession_records(self) -> Refs:
        def compute() -> Refs:
            refs = Refs()
            for name, blob in self._committed(["."]):
                if name.split("/")[-1] == SUCCESSION_FILE:
                    refs |= text_refs(blob.decode("utf-8", "replace"))
            for ref, _ in self._refs("refs/preserve/"):
                refs |= Refs(run_ids=text_refs(ref).run_ids)
            return refs
        return self._cached("succession_records", compute)

    def _refs(self, prefix: str) -> list[tuple[str, str]]:
        out = _git_ok(self.canonical_root(), "for-each-ref",
                      "--format=%(refname) %(objectname)", prefix)
        rows = []
        for line in out.decode("utf-8", "replace").splitlines():
            name, _, oid = line.partition(" ")
            if name and oid:
                rows.append((name, oid))
        return rows

    def unmerged_candidates(self) -> Refs:
        def compute() -> Refs:
            repo = self.canonical_root()
            refs = Refs()
            for ref, oid in self._refs("refs/imports/") + self._refs("refs/preserve/"):
                merged = _git(repo, "merge-base", "--is-ancestor", oid, "HEAD")
                if merged.returncode == 0:
                    continue
                if merged.returncode != 1:
                    raise SourceUnreadable(f"cannot judge whether {ref} is merged")
                commit = _git_ok(repo, "rev-parse", "--verify", "-q",
                                 oid + "^{commit}").decode().strip()
                refs |= Refs(run_ids=text_refs(ref).run_ids, commits=frozenset({commit}))
            return refs
        return self._cached("unmerged_candidates", compute)

    # -- processes --------------------------------------------------------- #

    def processes_inside(self, path: str) -> list[str]:
        return processes_inside(path, proc_root=self.proc_root, getuid=self.getuid,
                                proc_uid=self.proc_uid)

    # -- originals --------------------------------------------------------- #

    def object_format(self) -> str:
        def compute() -> str:
            done = _git(self.canonical_root(), "rev-parse", "--show-object-format")
            name = done.stdout.decode().strip() if done.returncode == 0 else "sha1"
            return name if name in ("sha1", "sha256") else "sha1"
        return self._cached("object_format", compute)

    def head_commit(self) -> str:
        return self._cached("head_commit", lambda: _git_ok(
            self.canonical_root(), "rev-parse", "--verify", "HEAD^{commit}").decode().strip())

    def git_originals(self) -> dict[str, Original]:
        """Tracked blobs at ``HEAD`` of the canonical repository whose index
        entry equals the ``HEAD`` blob, by absolute path. Whether the
        working-tree file is clean is checked per original (`original_ok`)."""
        def compute() -> dict[str, Original]:
            repo = self.canonical_root()
            head = _git_ok(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
            index = _git_ok(repo, "ls-files", "-s", "-z")
            staged = {}
            for record in index.split(b"\0"):
                if not record:
                    continue
                meta, _, name = record.partition(b"\t")
                parts = meta.split()
                if len(parts) == 3 and parts[2] == b"0":
                    staged[os.fsdecode(name)] = parts[1].decode()
            out = {}
            for record in head.split(b"\0"):
                if not record:
                    continue
                meta, _, name = record.partition(b"\t")
                parts = meta.split()
                if len(parts) != 3 or parts[1] != b"blob":
                    continue
                rel = os.fsdecode(name)
                oid = parts[2].decode()
                if staged.get(rel) != oid:
                    continue
                path = os.path.join(os.fspath(repo), rel)
                out[path] = Original(path, "git", oid, parts[0].decode())
            return out
        return self._cached("git_originals", compute)

    def retention(self) -> dict[str, Original]:
        """Originals listed in the retention manifests committed at ``HEAD``
        under `RETENTION_DIR`, by absolute path. A listed path twice with
        different digests is listed under neither (fail closed)."""
        def compute() -> dict[str, Original]:
            out: dict[str, Original] = {}
            conflicts = set()
            for name, blob in self._committed([RETENTION_DIR]):
                if not name.endswith(".json"):
                    continue
                try:
                    record = json.loads(blob.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SourceUnreadable(f"retention manifest {name}: {exc}") from exc
                if not isinstance(record, dict) or record.get("schema") != RETENTION_SCHEMA:
                    continue
                if record.get("project_id") != self.project_id:
                    continue
                for item in record.get("originals") or []:
                    if not isinstance(item, dict):
                        continue
                    path, digest = item.get("path"), item.get("sha256")
                    if not (isinstance(path, str) and os.path.isabs(path)
                            and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
                        continue
                    path = os.path.normpath(path)
                    if path in out and out[path].digest != digest:
                        conflicts.add(path)
                    out[path] = Original(path, "retention", digest)
            for path in conflicts:
                out.pop(path, None)
            return out
        return self._cached("retention", compute)

    # -- extract roots (STORAGE-C2) ----------------------------------------- #

    def extract_index(self) -> dict[str, list[dict]]:
        """Every extract-root line of every session index of the project
        (``<session scratch>/archive-target/EXTRACT_ROOTS.jsonl``), by path."""
        def compute() -> dict[str, list[dict]]:
            from hoh import archive_target as at
            out: dict[str, list[dict]] = {}
            for _, _, directory in self.scratch_dirs(("scratch",)):
                index = at.extract_index_path(directory)
                if not os.path.lexists(index):
                    continue
                for record in at._read_jsonl(index):
                    if isinstance(record, dict) and isinstance(record.get("path"), str):
                        out.setdefault(os.path.normpath(record["path"]), []).append(record)
            return out
        return self._cached("extract_index", compute)

    def committed_receipts(self) -> list[dict]:
        """Receipts committed at ``HEAD`` (STORAGE-C2's
        `rehydration.committed_receipt_file_digests` and
        `committed_content_digests`), each with its file digest."""
        def compute() -> list[dict]:
            from hoh import rehydration
            repo = self.canonical_root()
            files = rehydration.committed_receipt_file_digests(repo)
            contents = rehydration.committed_content_digests(repo)
            out = []
            for _, data in rehydration._committed_receipt_blobs(repo):
                digest = hashlib.sha256(data).hexdigest()
                record = rehydration._parse_receipt(data)
                if record is None or digest not in files:
                    continue
                if record.get("content_digest") not in contents:
                    continue
                out.append({**record, "_file_sha256": digest})
            return out
        return self._cached("committed_receipts", compute)


def _report_files(directory: Path) -> list[Path]:
    out = []
    stack = [(directory, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as listing:
                items = sorted(listing, key=lambda e: e.name)
        except OSError as exc:
            raise SourceUnreadable(f"reviews scratch {current} unreadable: {exc}") from exc
        for item in items:
            if item.name.startswith(".vh-owner") or item.name == ".git":
                continue
            if item.is_dir(follow_symlinks=False):
                if depth + 1 < _REVIEW_DEPTH:
                    stack.append((Path(item.path), depth + 1))
            elif item.is_file(follow_symlinks=False) and item.name.endswith(_REVIEW_SUFFIXES):
                out.append(Path(item.path))
    return out


# --------------------------------------------------------------------------- #
# Proofs
# --------------------------------------------------------------------------- #


def _original_ok(original: Original, sources: EvidenceSources, registry,
                 project_id: str) -> str:
    """Why `original` is not a retained canonical original now, or ""."""
    if original.source == "git":
        try:
            st = os.lstat(original.path)
        except OSError:
            return f"{original.path} is missing"
        algo = sources.object_format()
        if original.mode == "120000":
            if not stat.S_ISLNK(st.st_mode):
                return f"{original.path} is not the committed symlink"
            data = os.fsencode(os.readlink(original.path))
        else:
            if not stat.S_ISREG(st.st_mode):
                return f"{original.path} is not a regular file"
            data = _read_bytes(original.path)
        if _blob_id(data, algo) != original.digest:
            return f"{original.path} is modified (not the committed blob)"
        return ""
    owner = registry.owner_of(original.path)
    if owner.status != OWNED or owner.project_id != project_id or owner.kind != "runs_base":
        return f"{original.path} is not OWNED in a runs_base of {project_id}: {owner.reason}"
    try:
        if sha256_file(original.path) != original.digest:
            return f"{original.path} differs from its retention manifest entry"
    except OSError as exc:
        return f"{original.path} unreadable: {exc}"
    run_dir = owner.root / Path(original.path).relative_to(owner.root).parts[0]
    finished, why = run_finished(run_dir)
    if not finished:
        return f"the run of {original.path} is not finished: {why}"
    return ""


def _item_digest(entry: Entry, full: str, original: Original, algo: str) -> bool:
    if original.source == "retention":
        return entry.type == "file" and entry.sha256 == original.digest
    if entry.type == "file":
        return original.mode in ("100644", "100755") and \
            _blob_id(_read_bytes(full), algo) == original.digest
    if entry.type == "symlink":
        return original.mode == "120000" and \
            _blob_id(os.fsencode(entry.link_target or ""), algo) == original.digest
    return False


def duplicate_proof(item: Path, entries: tuple[Entry, ...], registry,
                    sources: EvidenceSources, project_id: str) -> tuple[dict | None, list[str]]:
    """The VERIFIED_DUPLICATE proof of `item`, or None and why not.

    Every entry must equal the same relative path below one prefix of one
    source of originals: the canonical repository's tracked, clean files
    (`EvidenceSources.git_originals`) or a committed retention manifest's
    listed ``runs_base`` files (`EvidenceSources.retention`). Directories
    must exist there as directories; at least one file is required.
    """
    notes: list[str] = []
    files = [e for e in entries if e.type == "file"]
    if not files:
        return None, ["duplicate: the item holds no file"]
    resolved = os.fspath(item)
    for source_name, reader in (("git", sources.git_originals), ("retention", sources.retention)):
        try:
            originals = reader()
        except SourceUnreadable as exc:
            notes.append(f"duplicate: {source_name} originals unreadable: {exc}")
            continue
        if not originals:
            continue
        first = files[0].rel
        prefixes = sorted({p[: -len(first) - 1] for p in originals
                           if p.endswith("/" + first)})
        for prefix in prefixes:
            if _inside(prefix, resolved) or _inside(resolved, prefix):
                continue
            relied: list[dict] = []
            why = ""
            algo = sources.object_format() if source_name == "git" else "sha256"
            for entry in entries:
                target = prefix + "/" + entry.rel
                if entry.type == "dir":
                    if not any(p.startswith(target + "/") for p in originals):
                        why = f"{entry.rel}: no such directory among the originals"
                        break
                    continue
                original = originals.get(target)
                if original is None:
                    why = f"{entry.rel}: no original at {target}"
                    break
                if not _item_digest(entry, os.path.join(resolved, entry.rel), original, algo):
                    why = f"{entry.rel}: bytes differ from {target}"
                    break
                problem = _original_ok(original, sources, registry, project_id)
                if problem:
                    why = f"{entry.rel}: {problem}"
                    break
                relied.append({"path": target, "source": original.source,
                               "digest": original.digest, "item_rel": entry.rel,
                               "item_sha256": entry.sha256})
            if why:
                notes.append(f"duplicate ({source_name}, {prefix}): {why}")
                continue
            proof = {"schema": PROOF_SCHEMA, "classification": VERIFIED_DUPLICATE,
                     "source": source_name, "prefix": prefix,
                     "entries_digest": entries_digest(entries), "originals": relied}
            if source_name == "git":
                proof["canonical"] = os.fspath(sources.canonical_root())
                proof["commit"] = sources.head_commit()
            return proof, notes
        if not prefixes:
            notes.append(f"duplicate ({source_name}): no original ends with {first}")
    return None, notes


def _owner_home(uid: int) -> str | None:
    """The home directory of `uid` from the password database (a seam), or
    None when it cannot be read."""
    import pwd
    try:
        return pwd.getpwuid(uid).pw_dir
    except (KeyError, OSError):
        return None


def _root_spellings(root: Path) -> list[str]:
    """`root` as a text may spell it: absolute, and -- for each home it lies
    under, the steward process's ``$HOME`` and the home of the root's owner
    (`_owner_home`, independent of the steward's environment) -- ``~/...``,
    ``$HOME/...`` and ``${HOME}/...``."""
    forms = [os.fspath(root)]
    homes = {os.environ.get("HOME")}
    try:
        homes.add(_owner_home(os.lstat(root).st_uid))
    except OSError:
        pass
    for home in sorted(h for h in homes if h and os.path.isabs(h)):
        try:
            rel = Path(root).relative_to(Path(home).resolve()).as_posix()
        except ValueError:
            continue
        if rel and rel != ".":
            forms += [f"~/{rel}", f"$HOME/{rel}", "${HOME}/" + rel]
    return list(dict.fromkeys(forms))


def legacy_root_pattern(root: Path) -> "re.Pattern[str]":
    """Any spelling of `root` not continued by a name: ``r1``, ``r1/``,
    ``r1)``, a sentence-ending ``r1.``, ``r1-`` and ``r1--see`` match;
    ``r12``, ``r1-b`` and ``r1.old`` (a sibling name) do not. Used for liveness (fail closed),
    never for identification (which needs ``<root>/`` exactly)."""
    alternatives = "|".join(re.escape(f) for f in _root_spellings(root))
    return re.compile(rf"(?:{alternatives})(?!\w|[.-]\w)")


def _tree_id(node: dict, algo: str) -> bytes | None:
    """The raw id of the git tree `node` (``{name: (mode, raw id) | dict}``),
    computed in memory; None for an empty directory."""
    rows = []
    for name, value in node.items():
        if isinstance(value, dict):
            sub = _tree_id(value, algo)
            if sub is None:
                return None
            rows.append((os.fsencode(name), b"40000", sub, True))
        else:
            rows.append((os.fsencode(name), value[0], value[1], False))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0] + (b"/" if r[3] else b""))
    body = b"".join(m + b" " + n + b"\0" + i for n, m, i, _ in rows)
    return hashlib.new(algo, b"tree " + str(len(body)).encode() + b"\0" + body).digest()


def git_tree_proof(item: Path, entries: tuple[Entry, ...], sources: EvidenceSources
                   ) -> tuple[dict | None, list[str]]:
    """The REPRODUCIBLE_SCRATCH proof of an exact tree copy (``git archive``
    extract), or None and why not: no entry named ``.git`` at any depth, no
    empty directory, and the tree id computed in memory from every file's
    mode (``100755`` iff owner-executable) and blob id and every symlink's
    ``120000`` blob equals the tree of a commit reachable from the canonical
    repository's refs. Nothing is written to any object store; no time
    stamp is read."""
    if any(".git" in e.rel.split("/") for e in entries):
        return None, ["git_tree: the item holds a .git entry"]
    if not any(e.type == "file" for e in entries):
        return None, ["git_tree: the item holds no file"]
    algo = sources.object_format()
    root: dict = {}
    for entry in entries:
        parts = entry.rel.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if entry.type == "dir":
            node.setdefault(parts[-1], {})
        elif entry.type == "file":
            mode = b"100755" if entry.mode & stat.S_IXUSR else b"100644"
            data = _read_bytes(os.path.join(os.fspath(item), entry.rel))
            if hashlib.sha256(data).hexdigest() != entry.sha256:
                return None, [f"git_tree: {entry.rel} changed while it was judged"]
            node[parts[-1]] = (mode, bytes.fromhex(_blob_id(data, algo)))
        elif entry.type == "symlink":
            target = os.fsencode(entry.link_target or "")
            node[parts[-1]] = (b"120000", bytes.fromhex(_blob_id(target, algo)))
        else:
            return None, [f"git_tree: {entry.rel} is not a file, dir or symlink"]
    raw = _tree_id(root, algo)
    if raw is None:
        return None, ["git_tree: the item holds an empty directory"]
    tree = raw.hex()
    commit = sources.commit_trees().get(tree)
    if commit is None:
        return None, [f"git_tree: tree {tree} is no commit's tree reachable from the "
                      f"canonical repository's refs"]
    proof = {"schema": PROOF_SCHEMA, "classification": REPRODUCIBLE_SCRATCH,
             "type": "git_tree", "canonical": os.fspath(sources.canonical_root()),
             "commit": commit, "tree": tree, "entries_digest": entries_digest(entries)}
    return proof, []


_FRESH_CLONE_KEYS = {"core.repositoryformatversion", "core.filemode", "core.bare",
                     "core.logallrefupdates", "remote.origin.url", "remote.origin.fetch"}


def _clone_config_problem(item: Path) -> str:
    done = _git(item, "config", "--file", os.fspath(item / ".git" / "config"), "--list", "-z")
    if done.returncode != 0:
        return "config unreadable"
    pairs = []
    for record in done.stdout.split(b"\0"):
        if record:
            key, _, value = record.decode("utf-8", "replace").partition("\n")
            pairs.append((key.lower(), value))
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        return "config repeats a key"
    values = dict(pairs)
    expected = {"core.filemode": "true", "core.bare": "false",
                "core.logallrefupdates": "true",
                "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*"}
    for key, value in expected.items():
        if values.get(key) != value:
            return f"config {key} is {values.get(key)!r}, not a fresh clone's {value!r}"
    version = values.get("core.repositoryformatversion")
    extra = set(values) - _FRESH_CLONE_KEYS
    if version == "1" and values.get("extensions.objectformat") == "sha256":
        extra.discard("extensions.objectformat")
    elif version != "0":
        return f"config core.repositoryformatversion is {version!r}"
    branches = {k.split(".")[1] for k in extra if k.startswith("branch.") and k.count(".") >= 2}
    for branch in branches:
        if values.get(f"branch.{branch}.remote") == "origin" and \
                values.get(f"branch.{branch}.merge") == f"refs/heads/{branch}":
            extra -= {f"branch.{branch}.remote", f"branch.{branch}.merge"}
    if extra:
        return "config has keys a fresh clone does not: " + ", ".join(sorted(extra))
    if "remote.origin.url" not in values:
        return "config names no remote origin"
    return ""


def _non_empty_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode) and bool(os.listdir(path))
    except FileNotFoundError:
        return False


def clone_proof(item: Path, sources: EvidenceSources) -> tuple[dict | None, list[str]]:
    """The REPRODUCIBLE_SCRATCH proof of a git clone, or None and why not.

    Gates (each failing one is named in the notes): ``.git`` a real
    directory; no linked worktree (``.git/worktrees/`` empty), no submodule
    repository (``.git/modules/`` empty), no hook (a file in ``.git/hooks``
    that is not a ``*.sample``), no alternates, a configuration equal to a
    fresh clone's apart from the remote URL; no untracked and no ignored file
    (``ls-files --others`` without and with ``--ignored``); the facts of
    `archive_bundle.git_facts` (``worktree_clean``, ``stash_empty``,
    ``all_refs_included``, ``no_gitlinks``, ``single_worktree``) over every
    ref of the clone; ``HEAD``'s commit present in the canonical repository
    and reachable from its refs; every ref of the clone present there and
    its commit an ancestor of ``HEAD``; every reflog entry an ancestor of
    ``HEAD``.
    """
    notes: list[str] = []
    git_dir = item / ".git"
    try:
        if not stat.S_ISDIR(os.lstat(git_dir).st_mode):
            return None, ["clone: .git is not a directory"]
    except FileNotFoundError:
        return None, ["clone: not a git clone"]
    canonical = sources.canonical_root()
    if _non_empty_dir(git_dir / "worktrees"):
        notes.append("linked_worktree: .git/worktrees is not empty")
    if _non_empty_dir(git_dir / "modules"):
        notes.append("submodule: .git/modules is not empty")
    try:
        hooks = [n for n in os.listdir(git_dir / "hooks") if not n.endswith(".sample")]
    except FileNotFoundError:
        hooks = []
    if hooks:
        notes.append("hook: " + ", ".join(sorted(hooks)))
    if os.path.lexists(git_dir / "objects" / "info" / "alternates"):
        notes.append("alternates: the object store reaches outside the clone")
    problem = _clone_config_problem(item)
    if problem:
        notes.append("config: " + problem)
    untracked = _git(item, "ls-files", "-z", "--others", "--exclude-standard")
    if untracked.returncode != 0 or untracked.stdout:
        notes.append("untracked_files")
    ignored = _git(item, "ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    if ignored.returncode != 0 or ignored.stdout:
        notes.append("ignored_files")
    refs = _git(item, "for-each-ref", "--format=%(refname) %(objectname)")
    rows = [line.split(" ", 1) for line in refs.stdout.decode("utf-8", "replace").splitlines()
            if " " in line]
    head = _git(item, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    commit = head.stdout.decode().strip() if head.returncode == 0 else ""
    if not commit:
        notes.append("clone: HEAD names no commit")
        return None, notes
    recorded = []
    for name, oid in rows:
        peeled = _git(item, "rev-parse", "--verify", "-q", name + "^{commit}")
        tree = _git(item, "rev-parse", "--verify", "-q", name + "^{tree}")
        recorded.append({"ref": name, "name": name, "full_name": name, "unpeeled": oid,
                         "commit": peeled.stdout.decode().strip(),
                         "tree": tree.stdout.decode().strip()})
    if recorded:
        try:
            measured = ab.git_facts(item, {"status": OWNED, "root": os.fspath(item)}, recorded)
        except ab.ArchiveError as exc:
            notes.append(f"git_facts refused: {exc}")
            measured = {"facts": {}}
        facts = measured["facts"]
        if not facts.get("stash_empty", False):
            notes.append("stash")
        for fact in ("worktree_clean", "all_refs_included", "no_gitlinks", "single_worktree"):
            if not facts.get(fact, False):
                notes.append(f"fact {fact} does not hold")
    else:
        notes.append("clone: no refs")
        facts = {}
    present = _git(canonical, "cat-file", "-e", commit + "^{commit}")
    if present.returncode != 0:
        notes.append(f"commit {commit} is not in the canonical repository")
    else:
        unreached = _git(canonical, "rev-list", "-n", "1", commit, "--not", "--all")
        if unreached.returncode != 0 or unreached.stdout.strip():
            notes.append(f"commit {commit} is not reachable from the canonical repository's refs")
    for row in recorded:
        if _git(canonical, "cat-file", "-e", row["unpeeled"]).returncode != 0:
            notes.append(f"ref {row['ref']} names an object the canonical repository lacks")
        elif not row["commit"] or _git(item, "merge-base", "--is-ancestor", row["commit"],
                                       commit).returncode != 0:
            notes.append(f"ref {row['ref']} is not an ancestor of HEAD")
    for logfile in _reflog_files(git_dir / "logs"):
        try:
            lines = Path(logfile).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            notes.append(f"unreachable_reflog: {logfile} unreadable")
            continue
        for line in lines:
            fields_ = line.split(" ", 2)
            for oid in fields_[:2]:
                if not oid or set(oid) == {"0"}:
                    continue
                if _git(item, "merge-base", "--is-ancestor", oid, commit).returncode != 0:
                    notes.append(f"unreachable_reflog: {oid} in {os.path.basename(logfile)}")
                    break
    if notes:
        return None, notes
    proof = {"schema": PROOF_SCHEMA, "classification": REPRODUCIBLE_SCRATCH, "type": "clone",
             "canonical": os.fspath(canonical), "commit": commit,
             "refs": sorted((r["ref"], r["unpeeled"]) for r in recorded),
             "facts": dict(sorted(facts.items()))}
    return proof, notes


def _reflog_files(logs: Path) -> list[str]:
    out = []
    for dirpath, _, filenames in os.walk(logs):
        out += [os.path.join(dirpath, name) for name in sorted(filenames)]
    return sorted(out)


def extract_root_proof(item: Path, entries: tuple[Entry, ...], ownership: Ownership,
                       sources: EvidenceSources) -> tuple[dict | None, list[str]]:
    """The release proof of a STORAGE-C2 extract root (Revisions 3, 4, 7).

    Eligible only when the item is listed in the union of the project's
    session indexes, is a direct child of a scratch directory, holds nothing
    but ``EXTRACT_ROOT.json`` and ``tree/``, its marker names a bundle whose
    receipt is committed at ``HEAD`` (else `AWAITING_COMMITTED_RECEIPT`),
    ``tree/``'s recomputed content digest equals that receipt's
    ``content_digest``, and a re-hash of the payload at the receipt's bundle
    location equals the payload SHA-256 **in the receipt**. A marker or an
    index outcome ``FAILED`` is `UNVERIFIED_EXTRACT` (NEEDS_YOU).
    """
    from hoh import archive_target as at
    from hoh import rehydration

    resolved = os.fspath(item)
    index = sources.extract_index().get(resolved)
    marker_path = item / at.EXTRACT_ROOT_MARKER
    if index is None:
        if os.path.lexists(marker_path):
            return None, ["extract root: a marker without an index entry is never enough"]
        return None, []
    if ownership.kind != "scratch" or item.parent != ownership.root:
        return None, ["extract root: not a direct child of a scratch directory"]
    names = sorted(e.rel for e in entries if "/" not in e.rel)
    if names != [at.EXTRACT_ROOT_MARKER, "tree"] or \
            next(e for e in entries if e.rel == "tree").type != "dir":
        return None, [f"extract root: holds {names}, not only EXTRACT_ROOT.json and tree/"]
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"extract root: marker unreadable: {exc}"]
    outcomes = {r.get("outcome") for r in index}
    if marker.get("outcome") == at.EXTRACT_FAILED or at.EXTRACT_FAILED in outcomes:
        return None, [UNVERIFIED_EXTRACT + ": the verification of this extract root failed"]
    bundle_id = marker.get("bundle_id")
    receipts = [r for r in sources.committed_receipts()
                if r.get("bundle_id") == bundle_id and r.get("status") == ab.ARCHIVED_VERIFIED
                and r.get("payload_kind") == "tree"]
    if not bundle_id or not receipts:
        return None, [AWAITING_COMMITTED_RECEIPT + f": no committed receipt for {bundle_id}"]
    receipt = receipts[0]
    tree_digest = rehydration.tree_content_digest(item / "tree")
    if tree_digest is None or tree_digest != receipt.get("content_digest"):
        return None, ["extract root: tree digest mismatch with the committed receipt"]
    location = receipt.get("bundle_realpath")
    payload = os.path.join(location, ab.TREE_PAYLOAD) if isinstance(location, str) else None
    try:
        payload_sha = (bounded(lambda: sha256_file(payload), sources.target_timeout,
                               "re-hash the payload on the target") if payload else None)
    except (OSError, TargetTimeout) as exc:
        return None, [f"extract root: payload unreadable at {payload}: {exc}"]
    if payload_sha is None or payload_sha != receipt.get("payload_sha256"):
        return None, ["extract root: payload mismatch with the committed receipt"]
    proof = {"schema": PROOF_SCHEMA, "classification": REPRODUCIBLE_SCRATCH,
             "type": "extract_root", "bundle_id": bundle_id,
             "receipt_file_sha256": receipt["_file_sha256"],
             "content_digest": tree_digest, "payload_sha256": payload_sha,
             "bundle_location": location}
    return proof, []


# --------------------------------------------------------------------------- #
# classify_tier
# --------------------------------------------------------------------------- #


def reference_reasons(path: str, *, root: Path | None, sources: EvidenceSources,
                      head_commit: str | None = None) -> list[str]:
    """The `HOT` reasons that come from references (open findings, current
    evidence, succession records, unmerged candidates) for `path` -- which
    need not exist (`VerifiedRelocation.user_reclaimed` asks it of a removed
    item). An unreadable source is a reason of its own."""
    resolved = Path(path)
    forms = {os.path.normpath(os.fspath(path))}
    components = run_components(resolved, root)
    root_s = os.fspath(root) if root is not None else None
    reasons = []
    for label, reader in (("open_finding", sources.open_findings),
                          ("current_evidence", sources.current_evidence),
                          ("succession_record", sources.succession_records),
                          ("unmerged_candidate", sources.unmerged_candidates)):
        try:
            refs = reader()
        except SourceUnreadable as exc:
            reasons.append(f"{label}: source unreadable ({exc})")
            continue
        for hit in _hits(refs, forms, root_s, components):
            reasons.append(f"{label}: {hit}")
        if head_commit and head_commit in refs.commits:
            reasons.append(f"{label}: HEAD {head_commit} is an unmerged candidate")
    return reasons


def run_dir_of(resolved: Path, ownership: Ownership) -> Path | None:
    """The run directory `resolved` is in or is: a child of a registered runs
    base (not ``_arenas``) or of a ``runs`` scratch directory that holds a
    ``state.json`` -- or whose ``state.json`` cannot be stat'ed (judged
    unfinished by `run_finished`). None outside any run."""
    if ownership.root is None or ownership.kind not in ("runs_base", "runs"):
        return None
    try:
        parts = resolved.relative_to(ownership.root).parts
    except ValueError:
        return None
    if not parts or (ownership.kind == "runs_base" and parts[0] == "_arenas"):
        return None
    run_dir = ownership.root / parts[0]
    try:
        os.lstat(run_dir / _STATE_FILE)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        pass
    return run_dir


def _legacy_reports_missing(ownership: Ownership, sources: EvidenceSources) -> str:
    """Why the anchor reports of the legacy root holding the item are not all
    committed at HEAD under ``dogfood/`` (by blob id), or ""."""
    entries = [e for e in sources.registry.legacy_roots(sources.project_id)
               if e["path"] == os.fspath(ownership.root)]
    if not entries:
        return "legacy root: not registered (or no longer intact)"
    try:
        committed = sources.committed_dogfood_blobs()
    except SourceUnreadable as exc:
        return f"legacy root: committed blobs unreadable ({exc})"
    for report in entries[0].get("reports") or []:
        if report.get("blob") not in committed:
            return f"legacy root: report {report.get('file')} is not committed at HEAD"
    return ""


def _bound_unchanged_file(registry, path: Path, data: bytes) -> bool:
    """`path`, a regular file directly in a legacy root, is still bound by
    name, type, inode (`legacy_child`) and content digest (in the form
    `classify_tier` measures a file item)."""
    bound = registry.legacy_child(path)
    if not bound or bound.get("type") != "file":
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    entry = Entry(".", "file", stat.S_IMODE(st.st_mode), len(data),
                  hashlib.sha256(data).hexdigest())
    return bound.get("entries_digest") == entries_digest([entry])


def _legacy_root_drift(ownership: Ownership, sources: EvidenceSources) -> list[str]:
    """HOT reasons: the legacy root holding the item no longer looks as its
    anchor bound it -- a direct entry that is not bound (a new note, a new
    round's directory), a bound entry that is another inode now, or a bound
    regular file whose content changed. The relocation's own quarantine of a
    bound child (same inode, child absent) is not drift. An unlistable root
    is a reason of its own."""
    entries = [e for e in sources.registry.legacy_roots(sources.project_id)
               if e["path"] == os.fspath(ownership.root)]
    if not entries:
        return ["legacy_drift: the root is not registered (or no longer intact)"]
    entry = entries[0]
    root = Path(entry["path"])
    try:
        with os.scandir(root) as listing:
            names = sorted(item.name for item in listing)
    except OSError as exc:
        return [f"legacy_drift: {root} cannot be listed ({exc})"]
    reasons = []
    for name in names:
        path = root / name
        bound_name = _bound_name(name)
        if bound_name != name and (bound_name not in entry["children"]
                                   or os.path.lexists(root / bound_name)):
            bound_name = name
        child = entry["children"].get(bound_name)
        recorded = (entry.get("child_ids") or {}).get(bound_name)
        try:
            st = os.lstat(path)
        except OSError:
            reasons.append(f"legacy_drift: {name} cannot be stat'ed")
            continue
        if child is None or recorded != [st.st_dev, st.st_ino]:
            reasons.append(f"legacy_drift: {name} is not bound by the root's anchor")
            continue
        try:
            digest = sources.registry.legacy_entry_digest(root, path)
        except Exception as exc:                # a refusal (hard link, foreign) is drift
            reasons.append(f"legacy_drift: {name} cannot be measured as bound ({exc})")
            continue
        if digest != child.get("entries_digest"):
            reasons.append(f"legacy_drift: {name} changed since the anchor")
    return reasons


def legacy_liveness(root: Path, sources: EvidenceSources) -> list[str]:
    """The legacy HOT reasons of `root` -- reports, reuse, drift -- from
    `sources` (the relocation passes fresh ones in quarantine, so a change
    after the plan keeps the item)."""
    holder = Ownership(status=OWNED, project_id=sources.project_id, kind="legacy_review",
                       root=Path(root))
    reasons = []
    missing = _legacy_reports_missing(holder, sources)
    if missing:
        reasons.append(f"legacy_report: {missing}")
    reasons += _legacy_reuse(holder, sources)
    reasons += _legacy_root_drift(holder, sources)
    return reasons


def _legacy_reuse(ownership: Ownership, sources: EvidenceSources) -> list[str]:
    """HOT reasons: a file committed at HEAD under ``dogfood/`` that names the
    legacy root (any spelling) but is not cited by its anchor -- a later
    designation or report: the root may be in use again."""
    entries = [e for e in sources.registry.legacy_roots(sources.project_id)
               if e["path"] == os.fspath(ownership.root)]
    if not entries:
        return ["legacy_reuse: the root is not registered (or no longer intact)"]
    cited = {c.get("blob") for c in entries[0].get("designations", []) +
             entries[0].get("reports", [])} | sources.registry.legacy_anchor_blobs(
                 sources.project_id, ownership.root)
    try:
        naming = sources.legacy_naming(Path(ownership.root))
    except SourceUnreadable as exc:
        return [f"legacy_reuse: source unreadable ({exc})"]
    return [f"legacy_reuse: {file} names the root and is not cited by its anchor"
            for file, blob in naming if blob not in cited]


def _hot_reasons(resolved: Path, given: str, ownership: Ownership,
                 sources: EvidenceSources) -> list[str]:
    reasons = []
    if ownership.kind in LEGACY_KINDS:
        missing = _legacy_reports_missing(ownership, sources)
        if missing:
            reasons.append(f"legacy_report: {missing}")
        reasons += _legacy_reuse(ownership, sources)
        reasons += _legacy_root_drift(ownership, sources)
    run_dir = run_dir_of(resolved, ownership)
    if run_dir is not None and resolved != run_dir:
        # Anything inside a run that may still write it is in use (Rev 5a).
        finished, why = run_finished(run_dir)
        if not finished:
            reasons.append(f"live_run: inside {run_dir.name}, which is not finished ({why})")
    if ownership.kind in SCRATCH_KINDS and ownership.session_id:
        try:
            if ownership.session_id in sources.live_sessions():
                reasons.append(f"lease: session {ownership.session_id} holds a live lease")
        except SourceUnreadable as exc:
            reasons.append(f"lease: source unreadable ({exc})")
    try:
        for holder in sources.processes_inside(os.fspath(resolved)):
            reasons.append(f"process: {holder}")
    except SourceUnreadable as exc:
        reasons.append(f"process: source unreadable ({exc})")
    head = None
    if os.path.isdir(resolved / ".git"):
        done = _git(resolved, "rev-parse", "--verify", "-q", "HEAD^{commit}")
        head = done.stdout.decode().strip() if done.returncode == 0 else None
    for reason in reference_reasons(os.fspath(resolved), root=ownership.root,
                                    sources=sources, head_commit=head):
        reasons.append(reason)
    given_n = os.path.normpath(os.path.abspath(given))
    if given_n != os.fspath(resolved):
        reasons += [r for r in reference_reasons(given_n, root=ownership.root, sources=sources)
                    if r not in reasons]
    return reasons


def classify_tier(path: str | os.PathLike[str], *, registry,
                  sources: EvidenceSources | None = None) -> TierDecision:
    """Classify `path` now. See the module docstring for the rules; the
    registry's `owner_of` decides ownership, `sources` (default: fresh
    `EvidenceSources` of the owning project) supplies every other fact."""
    shown = os.fspath(path)
    ownership = registry.owner_of(path)
    if ownership.status != OWNED or ownership.resolved is None or not ownership.project_id:
        return TierDecision(path=shown, tier=None, refused=UNKNOWN_OWNERSHIP,
                            reasons=(f"{ownership.status}: {ownership.reason}",),
                            project_id=ownership.project_id, ownership=ownership)
    project = ownership.project_id
    resolved = ownership.resolved
    if sources is None:
        sources = EvidenceSources(registry, project)
    elif sources.project_id != project:
        return TierDecision(path=os.fspath(resolved), tier=None, refused=UNKNOWN_OWNERSHIP,
                            reasons=(f"owned by {project}, not by the sources' project "
                                     f"{sources.project_id}",),
                            project_id=project, ownership=ownership)
    try:
        st = os.lstat(resolved)
        if (st.st_dev, st.st_ino) != (ownership.st_dev, ownership.st_ino):
            raise _NotOwned("the path changed while it was judged")
        if stat.S_ISDIR(st.st_mode):
            entries = inventory(resolved, registry, project)
        elif stat.S_ISREG(st.st_mode):
            entries = (Entry(".", "file", stat.S_IMODE(st.st_mode), st.st_size,
                             sha256_file(resolved)),)
        else:
            entries = ()
    except _NotOwned as exc:
        return TierDecision(path=os.fspath(resolved), tier=None, refused=UNKNOWN_OWNERSHIP,
                            reasons=(f"an entry is not owned: {exc}",), project_id=project,
                            ownership=ownership)
    if ownership.kind in LEGACY_KINDS:
        bound = registry.legacy_child(resolved)
        if (bound is None or ownership.root is None or resolved.parent != ownership.root
                or bound.get("entries_digest") != entries_digest(entries)):
            return TierDecision(path=os.fspath(resolved), tier=None, refused=UNKNOWN_OWNERSHIP,
                                reasons=(f"{LEGACY_UNBOUND}: not a direct child of a legacy root "
                                         f"whose content equals the anchor's binding",),
                                project_id=project, ownership=ownership)
    size = sum(e.size for e in entries if e.type == "file")
    base = dict(path=os.fspath(resolved), project_id=project, ownership=ownership,
                bytes=size, entries=entries)

    hot = _hot_reasons(resolved, shown, ownership, sources)
    if hot:
        return TierDecision(tier=HOT, reasons=tuple(hot), **base)

    notes: list[str] = []
    run_dir = run_dir_of(resolved, ownership)
    live_run = None
    if run_dir is not None:                     # the run directory itself
        finished, why_run = run_finished(run_dir)
        if not finished:
            live_run = f"live_run: {run_dir.name} is not finished ({why_run}); no proof"
            notes.append(live_run)
    if stat.S_ISDIR(st.st_mode) and live_run is None:
        proof, why = duplicate_proof(resolved, entries, registry, sources, project)
        notes += why
        if proof is not None:
            return TierDecision(tier=VERIFIED_DUPLICATE, reasons=(
                f"verified duplicate of {len(proof['originals'])} retained "
                f"{proof['source']} original(s) below {proof['prefix']}",),
                notes=tuple(notes), proof=proof, proof_digest=proof_digest(proof), **base)
        for builder in (lambda: extract_root_proof(resolved, entries, ownership, sources),
                        lambda: (git_tree_proof(resolved, entries, sources)
                                 if ownership.kind in LEGACY_KINDS
                                 else (None, [])),
                        lambda: clone_proof(resolved, sources)):
            try:
                proof, why = builder()
            except SourceUnreadable as exc:
                proof, why = None, [f"reproduction: source unreadable ({exc})"]
            notes += why
            if proof is not None:
                return TierDecision(tier=REPRODUCIBLE_SCRATCH, reasons=(
                    f"reproducible ({proof['type']})",), notes=tuple(notes), proof=proof,
                    proof_digest=proof_digest(proof), **base)

    terminal, why = _terminal(resolved, ownership, sources)
    if terminal:
        return TierDecision(tier=COLD, reasons=(f"terminal and unreferenced: {why}",),
                            notes=tuple(notes), **base)
    return TierDecision(tier=WARM, reasons=(f"not terminal: {why}",), notes=tuple(notes),
                        **base)


def _terminal(resolved: Path, ownership: Ownership, sources: EvidenceSources
              ) -> tuple[bool, str]:
    run_dir = run_dir_of(resolved, ownership)
    if run_dir is not None:
        return run_finished(run_dir)
    if ownership.kind in SCRATCH_KINDS:
        return True, f"session {ownership.session_id} holds no live lease"
    if ownership.kind in LEGACY_KINDS:
        missing = _legacy_reports_missing(ownership, sources)
        if missing:
            return False, missing
        return True, "legacy root: every anchor report is committed at HEAD"
    if ownership.kind == "runs_base" and ownership.root is not None:
        first = resolved.relative_to(ownership.root).parts[0]
        if first == "_arenas":
            return False, "an arena of a runs base"
        return run_finished(ownership.root / first)
    return False, f"a {ownership.kind} root has no terminal state"


__all__ = [
    "COLD", "ELIGIBLE", "HOT", "REPRODUCIBLE_SCRATCH", "TIERS", "UNKNOWN_OWNERSHIP",
    "VERIFIED_DUPLICATE", "WARM", "AWAITING_COMMITTED_RECEIPT", "UNVERIFIED_EXTRACT",
    "Entry", "EvidenceSources", "Refs", "SourceUnreadable", "TargetTimeout", "TierDecision",
    "bounded",
    "classify_tier", "clone_proof", "duplicate_proof", "extract_root_proof",
    "git_tree_proof", "legacy_liveness", "legacy_root_pattern",
    "inventory", "processes_inside", "proof_digest", "reference_reasons", "run_dir_of",
    "run_finished",
]
