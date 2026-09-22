"""Persistence: locked, atomic, without deleting.

Handoff §7 demands three properties, and they come together here:

1. **Exactly one controller/writer per run.** A file lock (flock) prevents a
   second start or a stale controller instance from mutating concurrently.
2. **Result first, pointer second.** Result files are written in full,
   fsynced and validated; only afterwards does the state atomically point
   at them. A crash may leave unreferenced artifacts behind, but **never an
   accepted candidate without evidence**.
3. **No deleting.** Replaced states are parked with a version
   (`state.json.v3.2026-09-07T12-00-00Z`), not overwritten-and-gone.
   That is at the same time a house rule (correction provenance).

Layout under `runs/<run_id>/`:

    state.json              current run state (the only pointer)
    state.json.v<n>.<ts>    earlier states, ascending, never deleted
    evidence.json           current E_t
    evidence.json.v<n>.<ts> earlier bundles
    results/<name>.json     immutable role results
    receipts/<id>.json      immutable runner receipts
    logs/<id>.txt           raw stdout of the checks
    .lock                   controller lock
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ValidationError

from .contracts import AcceptanceCheck, RunState, digest
from .evidence import EvidenceBundle


class StoreError(RuntimeError):
    pass


class LockBusy(StoreError):
    """Another controller holds the run. Do not force it -- that would be
    exactly the concurrent mutation A07 rules out."""


class StaleWrite(StoreError):
    """A writer is working on a stale state.

    Not forceable: the correct response is to reload, not to overwrite.
    """


class ImmutableViolation(StoreError):
    """Attempt to overwrite an immutable result file."""


_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _check_name(name: str) -> str:
    if not _SAFE.match(name):
        raise StoreError(f"unsafe file name: {name!r}")
    return name


def _atomic_write(path: Path, payload: str) -> None:
    """Write, fsync, rename -- plus an fsync on the directory.

    Without the directory fsync the rename can be missing after a power
    failure even though the data is there. That is exactly the gap A06 checks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class RunStore:
    """Every write access of a run passes through here."""

    def __init__(self, root: Path | str, run_id: str) -> None:
        self.run_id = _check_name(run_id)
        self.root = Path(root).expanduser().resolve()
        self.dir = self.root / self.run_id
        self.results_dir = self.dir / "results"
        self.receipts_dir = self.dir / "receipts"
        self.logs_dir = self.dir / "logs"
        self.state_path = self.dir / "state.json"
        self.evidence_path = self.dir / "evidence.json"
        self.checks_path = self.dir / "checks.json"
        self.stop_path = self.dir / "stop-request.json"
        self.lock_path = self.dir / ".lock"

    # -- Directory / lock -------------------------------------------------- #

    def ensure(self) -> None:
        for d in (self.dir, self.results_dir, self.receipts_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, *, blocking: bool = False) -> Iterator[None]:
        """Exclusive controller lock. Not recursive, not forceable."""
        self.ensure()
        fh = open(self.lock_path, "a+")
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fh.fileno(), flags)
            except OSError as exc:
                holder = ""
                try:
                    fh.seek(0)
                    holder = fh.read().strip()
                except OSError:
                    pass
                raise LockBusy(
                    f"Run {self.run_id} is already held by another controller"
                    + (f" ({holder})" if holder else "")
                ) from exc
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} since={_stamp()}\n")
            fh.flush()
            os.fsync(fh.fileno())
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    # -- Versioning instead of deleting ------------------------------------ #

    def _park(self, path: Path) -> Path | None:
        """Parks the existing state with a version. Returns the parked path.

        Two bugs of the first version, both documented by reviewers:

        1. The number was `1 + number of existing .v*`. A `prune()` moves
           parked states into `attic/`, which makes the counter **jump back**
           -- in the real a02 directory there were five files
           `state.json.v21.*` and four `v22.*`, and the *newest* timestamps
           carried the *lowest* numbers. The promised "reconstructible
           ladder" did not exist.

        2. If the name collided anyway, `os.link` raised `FileExistsError`,
           and the `except OSError` branch wrote **over** it with
           `write_bytes`. A silent loss at exactly the spot where "nothing is
           ever deleted" is promised.

        Both are fixed: the timestamp carries the ordering (it does not jump
        back), and on an identical name **another version is created right
        away** instead of overwriting. A file that is already there is not
        touched under any circumstances.
        """
        if not path.exists():
            return None

        base = path.with_name(f"{path.name}.v{_stamp()}")
        parked = base
        counter = 0
        while parked.exists():
            # An identical name means: create another version right away. The
            # previous state stays untouched.
            counter += 1
            parked = path.with_name(f"{base.name}-{counter:03d}")
            if counter > 999:
                raise StoreError(
                    f"Too many identically named parked states for {path.name}. "
                    f"Nothing is overwritten -- please check {path.parent}."
                )

        # Hard link instead of a copy: identical content, no second write.
        # `os.link` **never** creates over an existing file; if it fails for
        # another reason (a different file system) we copy -- and even that
        # only onto a path the loop above has proven to be free.
        #
        # **The condition that carries this:** every writer of this store
        # replaces the file atomically (`_atomic_write` -> `os.replace`) and
        # therefore creates a new inode. If someone wrote *in place*, the
        # parked state would change along with it -- a hard link shares the
        # inode. This condition holds throughout the store and is named here
        # so that it does not get lost silently.
        try:
            os.link(path, parked)
        except FileExistsError:                        # race: do not overwrite
            raise StoreError(
                f"Parked state {parked.name} appeared between the check and "
                "the creation. Nothing is overwritten."
            ) from None
        except OSError:
            with open(parked, "xb") as fh:      # x = exclusive, never overwrite
                fh.write(path.read_bytes())
        return parked

    # -- State -------------------------------------------------------------- #

    def write_state(self, state: RunState) -> None:
        """Writes the state -- but only if the writer is up to date.

        The state is the only pointer; the old state is parked beforehand so
        that a way back exists (A10).

        **Fencing:** if a higher `write_seq` lies on disk, someone else has
        written in the meantime and this writer is working on a stale state.
        It is rejected instead of overwriting the other party's progress. The
        lock alone did not catch that -- it only serializes.
        """
        self.ensure()

        if self.state_path.exists():
            try:
                # The same migration-aware view as `read_state`. Without
                # it the fencing failed on a state that `read_state` read
                # without trouble -- after a schema change the run would have
                # been readable but not writable.
                current = self._read_state_file()
            except (ValidationError, OSError) as exc:
                # Formerly: current = None, the fencing failed silently and
                # the stale writer overwrote. Handoff §7 demands the opposite
                # -- a damaged state blocks progress visibly.
                raise StoreError(
                    f"State of run {self.run_id} is unreadable; there is no way "
                    f"to check whether this writer is up to date: {exc}"
                ) from exc
            if current is not None:
                if current.run_id != state.run_id:
                    raise StaleWrite(
                        f"State on disk belongs to run {current.run_id}, "
                        f"but the write is for {state.run_id}"
                    )
                if state.write_seq < current.write_seq:
                    raise StaleWrite(
                        f"Stale writer for run {self.run_id}: own "
                        f"sequence {state.write_seq}, on disk "
                        f"{current.write_seq}. The state has been advanced by "
                        "another controller in the meantime -- reload "
                        "instead of overwriting."
                    )

        state.write_seq += 1
        self._park(self.state_path)
        _atomic_write(self.state_path, state.model_dump_json(indent=2))

    def read_parked_state(self, path: Path | str) -> RunState:
        """Reads one of the parked states -- migration included.

        There was no public way to do this. `parked_states()` handed back
        paths, and the migration sat exclusively inside `_read_state_file`,
        which reads `self.state_path` and nothing else. So anyone wanting to
        look at an earlier state had to reimplement `_migrate`, and 23 of the
        119 parked files on this machine need it.

        An adversarial reviewer called that out at the step 0 gate, and the
        objection lands: a system whose central promise is that nothing
        disappears silently must not make its own history reachable only
        through a private function. No data was lost -- but the way back was
        undocumented, which for a run record is nearly as bad.
        """
        return self._parse_state(Path(path).read_text(encoding="utf-8"))

    def _read_state_file(self) -> RunState:
        """Reads the state file, with field migration where needed.

        One place for both readers: `read_state` and the fencing check in
        `write_state`. Two views on the same file were exactly the reason why
        a migrated run was readable but not writable.
        """
        return self._parse_state(self.state_path.read_text(encoding="utf-8"))

    @staticmethod
    def _parse_state(raw: str) -> RunState:
        """The one parser, so a parked state reads exactly like a current one."""
        try:
            return RunState.model_validate_json(raw)
        except ValidationError as exc:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise exc from None
            migrated = _migrate(parsed) if isinstance(parsed, dict) else None
            if migrated is None:
                raise
            return RunState.model_validate(migrated)

    def read_state(self) -> RunState:
        """Reads the state -- with field migration, but without guessing.

        The contract is deliberately strict (`extra=forbid`) so that a
        hallucinated field does not slip through. But exactly that strictness
        makes every existing run unreadable as soon as a field is renamed --
        and over weeks of work the code inevitably changes. Without a
        migration path every schema change would be a data loss.

        Only what is listed in `MIGRATIONS` **by name** gets migrated. An
        unknown field stays an error: to discard it silently would be the same
        negligence the strict contract is built against. The previous state is
        parked before every migration.
        """
        if not self.state_path.exists():
            raise StoreError(f"no state for run {self.run_id} at {self.state_path}")
        try:
            fresh = RunState.model_validate_json(
                self.state_path.read_text(encoding="utf-8")
            )
            return fresh
        except ValidationError:
            pass                                # once more in a moment, migrating

        try:
            state = self._read_state_file()
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            # A corrupt or schema-foreign state blocks visibly instead of
            # quietly running on.
            raise StoreError(
                f"State of run {self.run_id} is damaged or schema-foreign: {exc}"
            ) from exc

        self._park(self.state_path)
        state.note(
            "State migrated to the current schema; the previous state lies "
            "next to it, versioned."
        )
        return state

    def state_exists(self) -> bool:
        return self.state_path.exists()

    # -- Evidence ----------------------------------------------------------- #

    def write_evidence(self, bundle: EvidenceBundle) -> str:
        """Write and validate first, only then may the state point at it."""
        self.ensure()
        payload = bundle.model_dump_json(indent=2)
        self._park(self.evidence_path)
        _atomic_write(self.evidence_path, payload)
        # Read back: a pointer to something unreadable is worse than none.
        EvidenceBundle.model_validate_json(self.evidence_path.read_text(encoding="utf-8"))
        return digest(payload)

    def read_evidence(self, *, expect_digest: str | None = None) -> EvidenceBundle:
        """Reads E_t and checks it against the digest recorded in the state.

        Before, it was only read and never compared: `evidence_ref` was a
        constant path to a mutable file, and `evidence_digest` sat next to it
        without ever being checked. An `evidence.json` rewritten from the
        outside got through without comment.
        """
        if not self.evidence_path.exists():
            if expect_digest:
                raise StoreError(
                    f"State points at evidence (digest {expect_digest}), "
                    f"but {self.evidence_path} is missing"
                )
            return EvidenceBundle(run_id=self.run_id)

        raw = self.evidence_path.read_text(encoding="utf-8")
        if expect_digest and digest(raw) != expect_digest:
            raise StoreError(
                f"Evidence of run {self.run_id} deviates from the state: expected "
                f"{expect_digest}, found {digest(raw)}. The file was changed "
                "outside the controller."
            )
        return EvidenceBundle.model_validate_json(raw)

    # -- Backup and restore ------------------------------------------------- #

    def backup(self, dest: Path | str | None = None) -> Path:
        """Backs up the complete run state as a tar.gz.

        Handoff §7 demands: *"test backup and restore of the run state
        once."* Everything is backed up except `arena/` and `attic/` -- the
        arenas are reproducible from the candidate bindings at any time
        (`git archive`), and they are the large part.
        """
        import tarfile

        out_dir = Path(dest) if dest else self.root / "backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.run_id}-{_stamp()}.tar.gz"

        with tarfile.open(path, "w:gz") as tar:
            for entry in sorted(self.dir.iterdir()):
                if entry.name in ("arena", "attic", ".lock"):
                    continue
                tar.add(entry, arcname=f"{self.run_id}/{entry.name}")
        return path

    def restore(self, archive: Path | str, *, overwrite: bool = False) -> Path:
        """Restores a backed-up run.

        Without `overwrite` an existing run is not touched -- the restored one
        lands next to it under `<run_id>-restored-<time>`. That is the house
        rule "delete nothing" in operational form.
        """
        import tarfile

        archive = Path(archive)
        if not archive.exists():
            raise StoreError(f"backup not found: {archive}")

        if self.dir.exists() and any(self.dir.iterdir()) and not overwrite:
            target_run_id = f"{self.run_id}-restored-{_stamp()}"
        else:
            target_run_id = self.run_id

        target = self.root / _check_name(target_run_id)
        target.mkdir(parents=True, exist_ok=True)
        target_root = target.resolve()

        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                # The first path component is the original run id; it is replaced.
                parts = Path(member.name).parts[1:]
                if not parts:
                    continue
                out = (target / Path(*parts)).resolve()
                if not str(out).startswith(str(target_root)):
                    raise StoreError(f"path points out of the target: {member.name!r}")
                member.name = str(Path(*parts))
            tar.extractall(target, filter="tar")

        # Checked by reading back: a backup that cannot be loaded is not one.
        restored = RunStore(self.root, target_run_id)
        restored.read_state()
        return target

    # -- Retention ---------------------------------------------------------- #

    @property
    def arenas_dir(self) -> Path:
        """Check directories -- **next to** the run directory, not inside it.

        A check command runs in an arena with a free shell. If it lay under
        `runs/<id>/`, receipts, state and the preservation suite would be
        reachable via `../..`; a reviewer exploited exactly that.
        """
        d = self.root / "_arenas" / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prune(self, *, keep_states: int = 20, keep_arenas: int = 3) -> dict[str, list[str]]:
        """Limits what piles up -- without losing evidence.

        Handoff §7 demands "rotation and retention limit" by name and at the
        same time that "evidence of accepted checkpoints must not rotate away
        unnoticed". Both together mean: parked intermediate states and old
        arenas can be limited, **receipts, logs, role results and
        evidence.json never**.

        A review measured that one arena for a real research repo is 394 MB
        large -- per iteration and per attempt. And a single short test run
        left 13 parked `state.json` behind.

        Things are moved into `attic/`, not deleted: the house rule of this
        machine says "delete nothing, file it with a version".
        """
        attic = self.dir / "attic"
        moved: dict[str, list[str]] = {"states": [], "arenas": []}

        parked = sorted(self.dir.glob("state.json.v*"), key=lambda p: p.stat().st_mtime)
        if len(parked) > keep_states:
            target = attic / "states"
            target.mkdir(parents=True, exist_ok=True)
            for path in parked[: len(parked) - keep_states]:
                new_path = target / path.name
                if not new_path.exists():
                    path.rename(new_path)
                    moved["states"].append(path.name)

        arena = self.arenas_dir
        if arena.is_dir():
            # `<digest>.scratch` is not an arena: it is the sibling a check
            # command's HOME and TMPDIR point at (O48, `runner._scratch_dir`).
            # Counting it as one would halve the effective `keep_arenas`; and
            # it must travel with its arena, because a scratch directory whose
            # arena has been filed away is orphaned evidence of nothing.
            # `.hoh-scratch-<receipt_id>` is the sandboxed path's equivalent,
            # and `prune` did not know the name: `Path(".hoh-scratch-r-i1-a1-K1")`
            # has no suffix, so three of them reduced a `keep_arenas=3` to
            # zero and filed every real arena into `attic/`. Nothing was lost,
            # but "keep the last three arenas" was not what happened.
            # `.suffix` was the test and it broke again the moment a parked
            # scratch directory gained a timestamp: for
            # `cand.scratch.v20260913T145602Z` the suffix is the timestamp, so
            # three of those reduced a `keep_arenas=3` to zero all over again
            # and filed every real arena into `attic/`. The name is the test now.
            arena_runs = sorted(
                (
                    d for d in arena.iterdir()
                    if d.is_dir()
                    and ".scratch" not in d.name
                    and not d.name.startswith(".hoh-scratch-")
                    # The planner's read copies live under `planner/`, one
                    # directory that exists for the whole run. Counting it as
                    # an arena let `prune` file the planner's own root into
                    # the attic and take the next dispatch's read copy with it.
                    and d.name != "planner"
                ),
                key=lambda d: d.stat().st_mtime,
            )
            # A sandbox scratch directory is named after the **receipt**
            # (`.hoh-scratch-<run>-i<n>-a<m>-<check>`), which shares nothing
            # with an arena's `digest(...)[:12]` name -- so the glob that tried
            # to file them alongside their arena could never match, and they
            # accumulated forever. They are filed on their own age instead,
            # keeping as many as there are arenas.
            sandbox_ = sorted(
                (d for d in arena.iterdir()
                 if d.is_dir() and d.name.startswith(".hoh-scratch-")),
                key=lambda d: d.stat().st_mtime,
            )
            # `run_check` writes one of these beside the arena per check, and
            # nothing ever removed them: the loop below only considers
            # directories. They are transcripts, they are small, and they are
            # kept -- but they are kept in the attic, not in the tree the next
            # run's roles work in.
            logs_ = sorted(
                (f for f in arena.glob(".hoh-out-*.log") if f.is_file()),
                key=lambda f: f.stat().st_mtime,
            )
            if len(logs_) > keep_arenas:
                target = attic / "arenas"
                target.mkdir(parents=True, exist_ok=True)
                for f in logs_[: len(logs_) - keep_arenas]:
                    destination = target / f.name
                    if not destination.exists():
                        f.rename(destination)
                        moved["arenas"].append(f.name)
            if len(sandbox_) > keep_arenas:
                target = attic / "arenas"
                target.mkdir(parents=True, exist_ok=True)
                for d in sandbox_[: len(sandbox_) - keep_arenas]:
                    destination = target / d.name
                    if not destination.exists():
                        d.rename(destination)
                        moved["arenas"].append(d.name)

            if len(arena_runs) > keep_arenas:
                target = attic / "arenas"
                target.mkdir(parents=True, exist_ok=True)
                for d in arena_runs[: len(arena_runs) - keep_arenas]:
                    new_path = target / d.name
                    if not new_path.exists():
                        d.rename(new_path)
                        moved["arenas"].append(d.name)
                        for scratch in (
                            d.with_name(d.name + ".scratch"),
                            *arena.glob(f"{d.name}.scratch.v*"),
                        ):
                            if not scratch.is_dir():
                                continue
                            scratch_target = target / scratch.name
                            if not scratch_target.exists():
                                scratch.rename(scratch_target)
                                moved["arenas"].append(scratch.name)

        return moved

    def usage_bytes(self) -> dict[str, int]:
        """What this run occupies on disk, by category."""

        def size(path: Path) -> int:
            if not path.exists():
                return 0
            if path.is_file():
                return path.stat().st_size
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

        return {
            # The arenas lie **next to** the run directory, not inside it
            # (see arenas_dir). Until 2026-09-07 this line read
            # `self.dir / "arena"` and therefore always reported 0, however
            # much the arenas actually held -- a report stating one of its own
            # categories as empty while it is not, which is exactly the silent
            # false statement the rest is built against.
            #
            # Correction provenance, 2026-09-08: this comment used to say
            # "even with 216 KB of arenas actually occupied" and called the
            # arenas "the largest source of usage". An adversarial reviewer
            # refuted both. 216 KB is what `du -sh` reports (4K block
            # allocation over 29 small files); this function sums `st_size`,
            # which came to **35,633 bytes** -- the number was six times too
            # high for the quantity it was about. And the arenas were the
            # largest category in neither existing run: `a02` had `attic` at
            # 3.6 MB with `arena` at 0, `a03` had `states` at 124,670 against
            # `arena` at 33,351. What the fix demonstrably did is turn `a03`'s
            # `arena` from 0 into 33,351. That is the whole claim, and it is
            # enough.
            "arena": size(self.arenas_dir),
            "attic": size(self.dir / "attic"),
            "receipts": size(self.receipts_dir) + size(self.logs_dir),
            "results": size(self.results_dir),
            "states": sum(
                f.stat().st_size for f in self.dir.glob("state.json*") if f.is_file()
            ),
        }

    # -- Stop request (without lock) ----------------------------------------- #

    def request_stop(self, kind: str, reason: str) -> None:
        """Records a stop request without needing the controller lock.

        The controller holds the lock for a whole iteration -- planner,
        developer and QA dispatch plus all checks. `pause` and `cancel`
        therefore hit nothing and only reported "is already held": a fix had
        taken exactly the capability away from the operator that Handoff §7
        demands.

        The request is a file of its own. The controller reads it at every
        documented safe boundary between two phases -- never in the middle of
        a running check.
        """
        if kind not in ("pause", "cancel"):
            raise StoreError(f"unknown stop request: {kind!r}")
        self.ensure()
        _atomic_write(
            self.stop_path,
            json.dumps({"kind": kind, "reason": reason, "at": _stamp()}, indent=2),
        )

    def read_stop_request(self) -> dict | None:
        if not self.stop_path.exists():
            return None
        try:
            return json.loads(self.stop_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"kind": "pause", "reason": "unreadable stop request", "at": ""}

    def clear_stop_request(self) -> None:
        """Park a handled request with a version instead of deleting it."""
        if self.stop_path.exists():
            self._park(self.stop_path)
            self.stop_path.unlink()

    # -- Preservation suite -------------------------------------------------- #

    def write_checks(self, checks: dict[str, AcceptanceCheck]) -> None:
        """The checks that have to run again on every further candidate.

        Handoff §6: *"Old passing checks are preservation requirements ...,
        not automatically valid proof for new code. For the first production
        version, run the defined acceptance/regression suite again on every
        candidate."* Without this persistence that was pure decoration: a
        planner who leaves out an inconvenient check got a real regression
        through as a checkpoint.
        """
        self.ensure()
        self._park(self.checks_path)
        payload = json.dumps(
            {cid: c.model_dump() for cid, c in sorted(checks.items())}, indent=2
        )
        _atomic_write(self.checks_path, payload)

    # -- Amendments --------------------------------------------------------- #

    @property
    def amendments_path(self) -> Path:
        return self.dir / "amendments.json"

    def write_amendments(self, ledger) -> None:
        """The run's amendment chain, parked before every rewrite.

        Kept beside the state rather than inside it: an amendment outlives the
        iteration that prompted it, and a reader asking "what did this run
        promise when iteration 3 was accepted" needs the chain, not a field
        that only holds the latest value.
        """
        self.ensure()
        self._park(self.amendments_path)
        _atomic_write(self.amendments_path, ledger.model_dump_json(indent=2))

    def read_amendments(self, *, origin_digest: str = ""):
        """The chain, or an empty one anchored at `origin_digest`."""
        from .amendment import AmendmentLedger

        if not self.amendments_path.exists():
            return AmendmentLedger(run_id=self.run_id, origin_digest=origin_digest)
        try:
            raw = self.amendments_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StoreError(
                f"amendment chain of run {self.run_id} unreadable: {exc}"
            ) from exc
        return AmendmentLedger.model_validate_json(raw)

    def read_checks(self) -> dict[str, AcceptanceCheck]:
        if not self.checks_path.exists():
            return {}
        try:
            raw = json.loads(self.checks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise StoreError(
                f"preservation suite of run {self.run_id} unreadable: {exc}"
            ) from exc
        return {cid: AcceptanceCheck.model_validate(data) for cid, data in raw.items()}

    # -- Immutable artifacts ------------------------------------------------ #

    def write_result(self, name: str, model: BaseModel) -> Path:
        return self._write_immutable(self.results_dir / f"{_check_name(name)}.json", model)

    def write_receipt(self, name: str, model: BaseModel) -> Path:
        return self._write_immutable(self.receipts_dir / f"{_check_name(name)}.json", model)

    def _write_immutable(self, path: Path, model: BaseModel) -> Path:
        self.ensure()
        if path.exists():
            raise ImmutableViolation(
                f"{path.name} already exists; results and receipts are never overwritten"
            )
        _atomic_write(path, model.model_dump_json(indent=2))
        return path

    def write_log(self, name: str, text: str) -> Path:
        self.ensure()
        path = self.logs_dir / f"{_check_name(name)}.txt"
        if path.exists():
            raise ImmutableViolation(f"{path.name} already exists")
        _atomic_write(path, text)
        return path

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    # -- Inventory ----------------------------------------------------------- #

    def receipts(self) -> list[Path]:
        return sorted(self.receipts_dir.glob("*.json"))

    def results(self) -> list[Path]:
        return sorted(self.results_dir.glob("*.json"))

    def parked_states(self) -> list[Path]:
        return sorted(self.dir.glob("state.json.v*"), key=lambda p: p.stat().st_mtime)


#: Fields that once existed and today are named differently or dropped.
#: Every line is a deliberate decision with a rationale, not a heuristic.
MIGRATIONS: dict[str, str | None] = {
    # `time.monotonic()` counts from boot and was persisted anyway; after a
    # restart the value was meaningless and the runtime limit silently dead.
    # Replaced by `started_at_wall` (UTC ISO). The old value cannot be
    # converted -- it is discarded, and the budget starts over.
    "started_monotonic": None,
}


def _migrate(raw_state: dict) -> dict | None:
    """Applies known field migrations. None = not migratable."""
    unknown = set(raw_state) - set(RunState.model_fields) - set(MIGRATIONS)
    if unknown:
        return None                      # do not guess
    changed = False
    for old, new in MIGRATIONS.items():
        if old in raw_state:
            value = raw_state.pop(old)
            changed = True
            if new is not None:
                raw_state.setdefault(new, value)
    return raw_state if changed else None


def list_runs(root: Path | str) -> list[str]:
    root = Path(root).expanduser()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "state.json").exists())
