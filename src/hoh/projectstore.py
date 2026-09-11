"""Durable storage for `ProjectState`, with the same discipline as `RunStore`.

The run layer already solved this problem once, and solved it in a way that
survived a real power cut during this project's own campaign: atomic write,
fsync, rename, fsync the directory; an exclusive non-recursive lock; fencing
on a monotonic write sequence so a stale writer is refused rather than
allowed to overwrite; and versioned parking instead of deletion so a way back
always exists.

This module applies that same discipline one level up. It deliberately does
not invent a second, subtly different one -- two storage layers with two
robustness models means the weaker one decides how much you can trust the
system, and nobody remembers which is which.

What is genuinely new here is the reason the layer exists at all. Run state
is written by a controller that is running. Project state has to be readable
by a session that was **not** running when it was written -- a fresh
orchestrator after a crash, a restart, or a provider outage. So the one
question this module has to answer, and the reason `resume_decision()` sits
here rather than in a caller, is: holding nothing but this file, what does
the next session do?
"""

from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from .project import Lifecycle, ProjectState
from .store import LockBusy, StaleWrite, StoreError, _atomic_write

#: Directory name under the HoH root. Sibling of runs/, not inside it: a
#: project outlives the runs it schedules, and nesting it under one of them
#: would make the longest-lived state the most easily pruned.
PROJECTS_DIRNAME = "projects"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise StoreError(
            f"Invalid project id {name!r}: expected [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        )
    return name


class ProjectStore:
    """One project's durable state on disk."""

    def __init__(self, root: Path | str, project_id: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.project_id = _check_name(project_id)
        self.dir = self.root / PROJECTS_DIRNAME / self.project_id
        self.state_path = self.dir / "project.json"
        self.lock_path = self.dir / ".lock"

    # -- basics ------------------------------------------------------------ #

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.state_path.exists()

    @contextmanager
    def lock(self, *, blocking: bool = False) -> Iterator[None]:
        """Exclusive orchestrator lock. Not recursive, not forceable.

        This is what makes a double controller start impossible rather than
        merely unlikely. The lock alone does not prevent a stale overwrite --
        it only serializes -- which is why `write_state` fences separately.
        """
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
                    f"Project {self.project_id} is already held by another orchestrator"
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

    # -- versioning instead of deleting ------------------------------------ #

    def _park(self, path: Path) -> Path | None:
        """Parks the current state under a version. Nothing is ever deleted.

        The counter is derived from the highest version already present, not
        from how many files exist: pruning or archiving moves files away and a
        count-based counter then jumps backwards and collides with a name that
        is already taken. The run layer learned that the same way.
        """
        if not path.exists():
            return None
        hoechste = 0
        for f in path.parent.glob(f"{path.name}.v*"):
            m = re.match(rf"^{re.escape(path.name)}\.v(\d+)\.", f.name)
            if m:
                hoechste = max(hoechste, int(m.group(1)))
        ziel = path.with_name(f"{path.name}.v{hoechste + 1}.{_stamp()}")
        os.replace(path, ziel)
        return ziel

    def parked_states(self) -> list[Path]:
        return sorted(self.dir.glob("project.json.v*"))

    # -- read and write ---------------------------------------------------- #

    def read_state(self) -> ProjectState:
        if not self.state_path.exists():
            raise StoreError(f"Project {self.project_id} has no state at {self.state_path}")
        raw = self.state_path.read_text(encoding="utf-8")
        try:
            return ProjectState.model_validate_json(raw)
        except ValidationError as exc:
            raise StoreError(
                f"State of project {self.project_id} is unreadable: {exc}. "
                "A damaged state blocks progress rather than being silently "
                "replaced -- the parked predecessors are next to it."
            ) from exc

    def write_state(self, state: ProjectState) -> None:
        """Writes the state, refusing a writer that is behind the disk.

        Fencing is separate from locking on purpose. A crashed orchestrator
        releases its lock the moment its process dies, so the next session can
        take the lock legitimately while holding a state it read before the
        crash. Only the sequence number catches that.
        """
        self.ensure()
        if self.state_path.exists():
            try:
                aktuell = self.read_state()
            except StoreError:
                raise
            if aktuell.project_id != state.project_id:
                raise StaleWrite(
                    f"State on disk belongs to project {aktuell.project_id}, "
                    f"but the write is for {state.project_id}"
                )
            if state.write_seq < aktuell.write_seq:
                raise StaleWrite(
                    f"Stale writer for project {self.project_id}: own sequence "
                    f"{state.write_seq}, on disk {aktuell.write_seq}. Another "
                    "orchestrator has advanced the state -- reload instead of "
                    "overwriting."
                )
        state.write_seq += 1
        state.touch()
        self._park(self.state_path)
        _atomic_write(self.state_path, state.model_dump_json(indent=2))

    def create(self, state: ProjectState) -> ProjectState:
        """Idempotent start: creating a project that exists returns the
        existing state rather than replacing it.

        A start that silently overwrote would turn a retried command -- the
        most ordinary thing a resuming session does -- into data loss.
        """
        if self.exists():
            return self.read_state()
        self.write_state(state)
        return self.read_state()


#: The verdicts a resuming session may reach. Deliberately the same shape as
#: the run layer's attach/evaluate/block: an unclear state blocks rather than
#: restarts, because restarting on an unclear state is how work gets done
#: twice or lost.
RESUME = "resume"
RETRY = "retry"
EVALUATE = "evaluate"
BLOCK = "block"
REPAIR = "repair"
CLOSED = "closed"


def resume_decision(state: ProjectState) -> tuple[str, str]:
    """What a fresh session should do, derived from persisted state alone.

    Returns `(verdict, reason)`. The reason is not decoration: a verdict a
    human cannot check is a verdict they have to trust.

    The ordering below is the whole content of the function, so it is worth
    reading as a claim rather than as code. Damage and ambiguity are examined
    **before** progress, because a state that cannot be trusted must not be
    advanced; and closure is checked before readiness, so a project that is
    already finished is not restarted by a session that merely found
    something runnable.
    """
    unbekannt = state.unknown_dependencies()
    if unbekannt:
        return BLOCK, (
            "dependencies naming nodes that do not exist: "
            + "; ".join(f"{k} -> {v}" for k, v in sorted(unbekannt.items()))
            + ". An unresolvable dependency reads to a scheduler exactly like a "
            "satisfied one, so this blocks rather than proceeding."
        )

    laufend = [n for n in state.nodes if n.lifecycle is Lifecycle.RUNNING]
    if laufend:
        # RUNNING with no live controller is precisely the ambiguous case: the
        # work may have finished, may have crashed mid-merge, or may still be
        # going in a process this session cannot see. It is evaluated, never
        # assumed either way.
        return EVALUATE, (
            "nodes recorded as RUNNING: "
            + ", ".join(n.id for n in laufend)
            + ". Whether that work finished, crashed, or is still live cannot be "
            "read from this state -- it has to be established against the run "
            "record before anything else is decided."
        )

    if state.rc_closed():
        return CLOSED, (
            f"DAG terminal, every global gate green at {state.measurement_head or 'an unrecorded head'}, "
            f"no outstanding repair node; closure generation {state.closure_generation}."
        )

    blockiert = [n for n in state.nodes if n.lifecycle is Lifecycle.BLOCKED]
    bereit = state.ready()

    if bereit:
        n = bereit[0]
        if n.rejections:
            return RETRY, (
                f"node {n.id} is READY after {n.rejections} rejection(s); a rejection is "
                "input to the next iteration, not an endpoint."
            )
        return RESUME, f"node {n.id} is READY and all its dependencies have settled."

    if state.dag_terminal():
        # Terminal but not closed: either a gate is red or never ran. Both mean
        # repair, and NOT_RUN is not green.
        letzte: dict[str, object] = {}
        for g in state.gates:
            letzte[g.name] = g
        rot = [name for name, g in letzte.items() if not g.counts_as_green]  # type: ignore[union-attr]
        if not letzte:
            return REPAIR, (
                "every node has settled but no global gate has run. A closure that "
                "checked nothing has established nothing."
            )
        return REPAIR, (
            "every node has settled, but the global gates are not all green: "
            + ", ".join(sorted(rot))
            + ". DAG_TERMINAL is not RC_CLOSED."
        )

    if blockiert:
        return BLOCK, (
            "no node is runnable and these are blocked: "
            + ", ".join(n.id for n in blockiert)
            + ". A blocked node needs an authority outside the orchestrator."
        )

    return BLOCK, (
        "nothing is READY, nothing is RUNNING, and the DAG is not terminal -- the "
        "state does not say what to do next, so it is not advanced."
    )


def unblock(store: "ProjectStore", node_id: str, reason: str,
            *, actor: str = "human") -> ProjectState:
    """Returns a BLOCKED node to READY, on the record.

    A blocked node is one the orchestrator refused to decide about -- an
    accepted candidate whose merge did not land, a provider outage, a verdict
    nothing could classify. Refusing was right; leaving no way back is not.
    Without this, the only route out of a blocked project is editing the state
    file by hand, which is the one thing a durable, digest-bound state exists
    to make unnecessary.

    The reason is required and becomes a decision record, because "someone
    unblocked it" without why is exactly the kind of unrecoverable intent this
    whole layer was built to stop losing. The node's note is kept, not
    overwritten: what it was blocked for stays readable after it is running
    again.
    """
    from .project import DecisionKind, DecisionRecord

    state = store.read_state()
    node = state.node(node_id)
    if node is None:
        raise StoreError(f"project {store.project_id} has no node {node_id}")
    if node.lifecycle is not Lifecycle.BLOCKED:
        raise StoreError(
            f"node {node_id} is {node.lifecycle.value}, not BLOCKED -- unblocking "
            "something that is not blocked would hide whatever it is actually doing"
        )
    vorher = node.note
    node.lifecycle = Lifecycle.READY
    node.note = (f"{vorher} | unblocked {utc()}: {reason}" if vorher else reason)
    state.decisions.append(
        DecisionRecord(
            id=f"D{len(state.decisions) + 1:04d}",
            kind=DecisionKind.POLICY_DISPOSITION,
            actor=actor,
            reason=reason,
            payload={"node": node_id, "was_blocked_for": vorher or ""},
        )
    )
    store.write_state(state)
    return store.read_state()


def utc() -> str:
    from .contracts import utcnow
    return utcnow()


def record_external_action(
    store: "ProjectStore", *, actor: str, reason: str,
    repo_path: Path | str | None = None, node: str | None = None,
    action_class: str = "repository-mutation",
    head_before: str = "", head_after: str = "",
) -> ProjectState:
    """Writes a repository mutation made outside the loop into project state.

    The heads are measured here when the caller does not supply them, because
    a record whose numbers were typed by the person being recorded is a record
    that proves nothing. `head_after` is read now; `head_before` has to come
    from the caller, since by the time anyone thinks to record the action it
    has already happened -- and that asymmetry is worth knowing about rather
    than papering over with a plausible value.
    """
    from .project import ExternalActionRecord

    state = store.read_state()
    if node is not None and state.node(node) is None:
        raise StoreError(f"project {store.project_id} has no node {node}")

    if repo_path and not head_after:
        head_after = _head(Path(repo_path))

    state.external_actions.append(
        ExternalActionRecord(
            action_id=f"X{len(state.external_actions) + 1:04d}",
            actor=actor, reason=reason, action_class=action_class, node=node,
            head_before=head_before, head_after=head_after,
            write_seq=state.write_seq,
        )
    )
    store.write_state(state)
    return store.read_state()


def _head(repo: Path) -> str:
    import subprocess

    p = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                       cwd=str(repo), capture_output=True, text=True)
    return p.stdout.strip()


def list_projects(root: Path | str) -> list[str]:
    basis = Path(root).expanduser().resolve() / PROJECTS_DIRNAME
    if not basis.is_dir():
        return []
    return sorted(p.name for p in basis.iterdir() if (p / "project.json").exists())
