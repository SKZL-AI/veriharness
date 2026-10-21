"""The surface book: who owns a Herdr surface is a record written when it is born, and when it is
finished is a lifecycle fact -- never an agent's status, never a name (strand `tabreap`, TR1).

What it does, said exactly.

* **Born registered.** `register` records a surface that exists (a reviewer, a drafter or an
  orchestrator session) in the project's *surface book* -- one `owned-surfaces.jsonl`, the I10a
  record used unchanged, with one extra object per line (`book`: kind, strand, perspective, round,
  candidate, the agent's name) -- after Herdr's own snapshot showed the pane, its tab, its agent and
  its directory. `register_workspaces` records the workspaces `hoh worktree` created (and only
  those: the ones the snapshot after the creation holds and the snapshot before it did not) in the
  record of the **runs root** they belong to. A surface that is not in a record is not touched.
  Ownership is the record; a label, a name, an age or an agent's status is never ownership.
* **Finished is a lifecycle fact.** The terminal rule is asked once per surface
  (`surfaces.reap(..., over_for=...)`): a reviewer or drafter is over when its **strand** is
  terminal -- `released` (an explicit `hoh surface release --strand`) or `merged` (a candidate
  commit one of the strand's surfaces was registered with is part of the branch it was registered
  against, `refs/heads/<name>`, asked of git now) -- and not before: an agent that is `idle` or
  `done` between two rounds of its strand is the normal case, not a reason; a strand that is
  parked keeps its reviewers until it is released. A run workspace is over when the runs root it was recorded for holds at least one run
  and **every** run directory there says `process.json` state `ended` and no process holds it.
  An orchestrator surface is never over.
* **The proofs are the reaper's own** (`surfaces.reap`, I10a): the snapshot, the identity of the
  pane, an agent that is `idle` or `done`, a plain shell alone and asleep, no stranger in the tab or
  the workspace, the `closing` / `closed` lines of the pane ledger, the second snapshot. This module
  only decides *whether the lifecycle allows a close*; it never closes anything itself.
* **A creation is one unit.** `hoh worktree` makes a target's workspace and a linked one, and
  Herdr's close of the target ends the linked one with it. The workspaces one call made are
  recorded with one group key and the number the call made; a workspace is closed only when every
  member of its group passes the same proofs (or is gone already) **and** the record holds the whole
  group -- otherwise it is kept or refused with the member named. A run is over only when
  `surfaces.run_over` says so (failed, cancelled or ready for delivery, no live process), not when
  its process has ended.
* **A line is judged where it belongs.** A reviewer's or drafter's line is honoured only in the
  project's own book (the home the sweep was given a project for); another home's line -- another
  project's book named as a runs root -- is never over, whatever the project given has released or
  merged. A pane that the project's book owns is never a run workspace.
* **`sweep`** is `hoh reap`: a dry run unless applied. Every surface of every home it reads comes
  back with an outcome and a reason, and an applied sweep leaves one line per surface in the home's
  `pane-lifecycle.jsonl` (`closing` / `closed` for a close, `kept` with the reason otherwise).

What this is not (stated, not built): role panes are born registered already
(`owned-agents.jsonl`) and closed by the dispatcher at the end of a turn; they are not in the book.
Nothing here starts an agent or opens a tab (that is `hoh review start`, TR2). A retired orchestrator
predecessor is never offered for closing. The worktree directory of a closed workspace stays. The
module removes no file and signals no process. Whether Herdr's `worktree create` answers with the
workspace ids is NOT_RUN: the workspaces are found by comparing two snapshots and checking the pane's
directory, not by reading the answer. Not measured either: whether the close of a linked workspace
ends its target (the group is judged as one unit in both directions), and the fate of a workspace
linked to the same target by another call (a second `hoh worktree` on the same clone, a
`herdr worktree create` by hand): it is in no group of ours.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import mergegate, surfaces
from .agentcontrol import OwnedAgentRegistry, ProgramAgentRole
from .durable_io import durable_append, read_single_link

KIND_REVIEWER = "reviewer"
KIND_DRAFTER = "drafter"
KIND_WORKSPACE = "run-workspace"
KIND_ORCHESTRATOR = "orchestrator"
#: The kinds whose lifecycle is a strand's.
STRAND_KINDS = (KIND_REVIEWER, KIND_DRAFTER)
KINDS = (KIND_REVIEWER, KIND_DRAFTER, KIND_WORKSPACE, KIND_ORCHESTRATOR)

#: The project's surface book: the scratch directory `projects/<p>/receipts/surfaces/` of the
#: registered session `surfaces` (the steward keeps its receipts in the same kind).
BOOK_SESSION = "surfaces"
BOOK_KIND = "receipts"
STRAND_FILE = "strand-events.jsonl"

STRAND_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PERSPECTIVE_RE = re.compile(r"[a-z][a-z0-9]{0,5}")
#: The id of the review that started a surface (`hoh review start`): 1 to 16 characters.
REVIEW_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,15}")
PANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,63}")
FULL_ID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: A branch as a full ref name, spelled conservatively (no `..`, no `@{`, no `.lock`, no empty or
#: dot-led component); the one place a strand's merge is asked: `refs/heads/<name>`.
BRANCH_RE = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*"
                       r"(/[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*)*")
MAX_ROUND = 999
#: The workspaces recorded by one `hoh worktree` call share a group key; a creation makes at most
#: a target's workspace and the linked workspace of the worktree.
GROUP_RE = re.compile(r"g[0-9]{8}t[0-9]{6}z-[0-9a-f]{10}")
MAX_GROUP_SIZE = 2
MAX_REASON = 200
#: A process.json is a few lines; one larger than this is not read.
PROCESS_JSON_LIMIT = 65536
#: A run's state.json (read for every sibling run of a runs root by `surfaces.run_over`) is a few
#: KiB; one larger than this is not read.
STATE_JSON_LIMIT = 4 << 20
#: A surface record or a strand-event file larger than this (a record of this build is a few KiB per
#: surface) is not read: the home is reported as unreadable and nothing in it is closed.
RECORD_LIMIT = 16 << 20


class Refused(Exception):
    """A registration, a release or a sweep the book will not do (exit 2, nothing written)."""


# --------------------------------------------------------------------------- #
# git: the two facts the strand rule needs
# --------------------------------------------------------------------------- #


def _git(repo: str, *args: str, allow: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """git in `repo` under the merge gate's scrubbed environment; a code outside `allow` is a
    `Refused`. (i6's `commitref.git` is the family's one runner once it is merged; the contract
    below does not change.)"""
    command = ["git", *mergegate._QUIET, "-c", "core.fsmonitor=false", "-c",
               "core.hooksPath=/dev/null", "-C", repo, *args]
    try:
        done = subprocess.run(command, capture_output=True, env=mergegate._git_env(),
                              stdin=subprocess.DEVNULL, timeout=mergegate._GIT_TIMEOUT,
                              check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refused(f"git cannot be run in {repo!r}: {exc}") from exc
    if done.returncode not in allow:
        raise Refused(f"git {args[0]} failed in {repo!r}: "
                      + done.stderr.decode("utf-8", "replace").strip()[:200])
    return done


def resolve_commit(repo: str, what: str, typed: object) -> str:
    """The commit `typed` names, resolved now in `repo`: `typed` must already be a full id (40 or
    64 lower-case hex digits) and the repository must answer with that very id (a tag object peels
    to another id and is refused; a short id, a name and `HEAD` are never expanded)."""
    if not isinstance(typed, str) or FULL_ID_RE.fullmatch(typed) is None:
        raise Refused(f"{what} {typed!r} is not a full commit id (40 or 64 lower-case hex digits): "
                      "a short id, a ref or a name is not accepted")
    done = _git(repo, "rev-parse", "--verify", "--quiet", typed + "^{commit}", allow=(0, 1))
    got = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or not got:
        raise Refused(f"{what} {typed[:12]} is not a commit of the project repository")
    if got != typed:
        raise Refused(f"{what} {typed[:12]} names an object that peels to {got[:12]}, not a "
                      "commit itself")
    return got


def branch_tip(repo: str, ref: object) -> str:
    """The commit the branch `ref` (a full `refs/heads/...` name) is on, asked now."""
    if (not isinstance(ref, str) or BRANCH_RE.fullmatch(ref) is None or ref.endswith(".lock")
            or len(ref) > 200):
        raise Refused(f"{ref!r} is not a branch as a full ref name (refs/heads/<name>)")
    done = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", allow=(0, 1))
    got = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or FULL_ID_RE.fullmatch(got) is None:
        raise Refused(f"{ref} is not a branch of the project repository")
    return got


def current_branch(repo: str) -> str:
    """The full ref name of the branch `HEAD` is on; a detached `HEAD` is a `Refused`."""
    done = _git(repo, "symbolic-ref", "--quiet", "HEAD", allow=(0, 1))
    got = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not got:
        raise Refused("HEAD of the project repository is detached: name the branch the candidate "
                      "is merged into with --into refs/heads/<name>")
    return got


def is_ancestor(repo: str, ancestor: str, tip: str) -> bool:
    """`ancestor` is `tip` or reachable from it (`git merge-base --is-ancestor`): code 0 yes, 1 no,
    anything else (an object the repository lacks) is a `Refused`."""
    return _git(repo, "merge-base", "--is-ancestor", ancestor, tip, allow=(0, 1)).returncode == 0


# --------------------------------------------------------------------------- #
# the record: the I10a registry plus one `book` object per line
# --------------------------------------------------------------------------- #


def _size_problem(path: Path) -> str:
    """'' unless the file at `path` is larger than `RECORD_LIMIT` (a missing file or one that cannot
    be examined is left to the reader, which refuses it)."""
    try:
        size = os.lstat(path).st_size
    except OSError:
        return ""
    return f"{path.name} is larger than {RECORD_LIMIT} bytes: not read" if size > RECORD_LIMIT else ""


def _clean_book(obj: object) -> dict | None:
    """The `book` object of a line if it is well formed, else None (the surface then has no
    lifecycle record and is never over)."""
    if not isinstance(obj, dict):
        return None
    kind = obj.get("kind")
    if kind not in KINDS:
        return None
    strand, perspective = obj.get("strand", ""), obj.get("perspective", "")
    rnd, candidate = obj.get("round", 1), obj.get("candidate", "")
    workspace, runs_root = obj.get("workspace_id", ""), obj.get("runs_root", "")
    into, review = obj.get("into", ""), obj.get("review", "")
    group, size = obj.get("group", ""), obj.get("group_size", 0)
    if not all(isinstance(v, str) for v in (strand, perspective, candidate, workspace, runs_root,
                                            into, review, group)):
        return None
    if not (isinstance(size, int) and not isinstance(size, bool)):
        return None
    if not (isinstance(rnd, int) and not isinstance(rnd, bool) and 1 <= rnd <= MAX_ROUND):
        return None
    if kind in STRAND_KINDS:
        if (STRAND_RE.fullmatch(strand) is None or PERSPECTIVE_RE.fullmatch(perspective) is None
                or (candidate and FULL_ID_RE.fullmatch(candidate) is None)
                or bool(candidate) != bool(into)
                or (into and (BRANCH_RE.fullmatch(into) is None or len(into) > 200))
                or (review and (kind != KIND_REVIEWER or REVIEW_RE.fullmatch(review) is None))):
            return None
    elif strand or perspective or candidate or into or review:
        return None
    if kind == KIND_WORKSPACE:
        if not (workspace and PANE_RE.fullmatch(workspace) and os.path.isabs(runs_root)
                and GROUP_RE.fullmatch(group) and 1 <= size <= MAX_GROUP_SIZE):
            return None
    elif workspace or runs_root or group or size:
        return None
    return {"kind": kind, "strand": strand, "perspective": perspective, "round": rnd,
            "candidate": candidate, "into": into, "review": review, "workspace_id": workspace,
            "runs_root": runs_root, "group": group, "group_size": size}


class BookRegistry(OwnedAgentRegistry):
    """`<home>/owned-surfaces.jsonl` (I10a's record, `run_id` = the directory's name) whose
    `adopt` lines carry the `book` object. The base fold ignores the extra object, so
    `surfaces.reap` reads the same file unchanged."""

    def __init__(self, home) -> None:
        home = Path(home)
        super().__init__(home / surfaces.SURFACES_FILE, run_id=home.name)
        self.home = home

    def register(self, role: ProgramAgentRole, book: dict, *, program: str, backend: str,
                 pane_id: str, tab_id: str, name: str, cwd: str) -> None:
        self._append("adopt", program=program, role=role.key, backend=backend, pane_id=pane_id,
                     tab_id=tab_id, name=name, cwd=cwd, book=book)

    def agents(self):
        """The registry's fold, with one more way to be unreadable: a line nested so deeply that
        `json.loads` raises `RecursionError` (the base fold catches ValueError, KeyError and
        TypeError only, and the whole sweep would die). It is the file that cannot be read."""
        try:
            return super().agents()
        except RecursionError:
            self.problem, self.skipped = "RecursionError: a line of the record is nested too deeply", 0
            return []

    def books(self) -> tuple[dict[str, dict], str]:
        """`{pane id: book}` of every pane whose latest registration has a well formed `book`,
        and why the file could not be read ('' when it could). A line of another run, a line that
        does not parse and a malformed `book` give no entry."""
        too_big = _size_problem(self.path)
        if too_big:
            return {}, too_big
        try:
            raw = read_single_link(self.path)
        except (OSError, UnicodeError) as exc:
            return {}, f"{type(exc).__name__}: {exc}"
        found: dict[str, dict | None] = {}
        for line in (raw or b"").decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if (not isinstance(rec, dict) or rec.get("run_id") != self.run_id
                        or rec.get("event") not in ("surface", "adopt")):
                    continue
                pane = str(rec["pane_id"])
            except RecursionError:
                return {}, "RecursionError: a line of the record is nested too deeply"
            except (ValueError, KeyError, TypeError):
                continue
            if pane:
                found[pane] = _clean_book(rec.get("book"))
        return {p: b for p, b in found.items() if b is not None}, ""


class StrandLedger:
    """`<book>/strand-events.jsonl`: the explicit `release` of a strand, one line, append-only."""

    def __init__(self, home) -> None:
        self.path = Path(home) / STRAND_FILE

    def release(self, strand: str, reason: str) -> None:
        line = json.dumps({"v": 1, "event": "release", "strand": strand, "reason": reason,
                           "at": _now()}, sort_keys=True)
        durable_append(self.path, line + "\n", single_link=True)

    def released(self) -> tuple[set[str], str]:
        """The strands released, and why the file could not be read ('' when it could). A line
        that does not parse or names no valid strand releases nothing."""
        too_big = _size_problem(self.path)
        if too_big:
            return set(), too_big
        try:
            raw = read_single_link(self.path)
        except (OSError, UnicodeError) as exc:
            return set(), f"{type(exc).__name__}: {exc}"
        out: set[str] = set()
        for line in (raw or b"").decode("utf-8", "replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (isinstance(rec, dict) and rec.get("event") == "release"
                    and isinstance(rec.get("strand"), str)
                    and STRAND_RE.fullmatch(rec["strand"]) is not None):
                out.add(rec["strand"])
        return out, ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# where the book is
# --------------------------------------------------------------------------- #


def open_registry(storage_root):
    """The storage registry of the state root. The seam a test replaces with an injected one."""
    from .storage_registry import StorageRegistry
    return StorageRegistry(Path(storage_root).expanduser())


def book_dir_of(registry, project_id: str) -> Path:
    return Path(registry.state_root) / "projects" / project_id / BOOK_KIND / BOOK_SESSION


def project_repo(registry, project_id: str) -> str:
    record = registry._project_record(project_id)
    if not record or not record.get("project_root"):
        raise Refused(f"project {project_id!r} is not registered")
    return str(record["project_root"])


def _book_dir(registry, project_id: str, path: Path) -> Path:
    """`path` is the registered scratch directory of the book: the registry answers `PROTECTED`
    (a scratch directory, never reclaimed as a candidate) with this project, kind and session --
    and `OWNED` for the record inside it."""
    owner = registry.owner_of(path)
    if (owner.status != "PROTECTED" or owner.project_id != project_id
            or owner.kind != BOOK_KIND or owner.session_id != BOOK_SESSION
            or owner.resolved is None or Path(owner.resolved) != path.resolve()):
        raise Refused(f"{path} is not the registered surface book of project {project_id!r} "
                      f"({owner.status}: {owner.reason})")
    return path


def existing_book(registry, project_id: str) -> Path | None:
    """The project's book if it exists and the registry owns it, None if there is none; a
    directory that exists but is not owned is a `Refused` (it is never adopted)."""
    path = book_dir_of(registry, project_id)
    if not os.path.lexists(path):
        return None
    return _book_dir(registry, project_id, path)


def ensure_book(registry, project_id: str, *, getpid=os.getpid) -> Path:
    """The project's book, registered first if it is not there yet: a session lease, the scratch
    directory (owner record and index entry written before the path is returned), the lease closed
    on every way out."""
    found = existing_book(registry, project_id)
    if found is not None:
        return found
    try:
        lease = registry.open_session(project_id, BOOK_SESSION, pid=getpid(),
                                      boot_id=registry._boot_id_reader())
    except Exception as exc:                          # noqa: BLE001 - the registry says why
        raise Refused(f"the surface book cannot be registered: {exc}") from exc
    try:
        path = Path(registry.scratch_dir(project_id, BOOK_SESSION, BOOK_KIND))
    except Exception as exc:                          # noqa: BLE001
        raise Refused(f"the surface book cannot be registered: {exc}") from exc
    finally:
        try:
            registry.close_session(project_id, BOOK_SESSION, lease)
        except Exception:                             # noqa: BLE001 - the directory is there either way
            pass
    return _book_dir(registry, project_id, path)


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


def _snapshot(control) -> dict:
    try:
        snap = control.snapshot()
    except Exception as exc:                          # noqa: BLE001 - nothing proven, nothing recorded
        raise Refused(f"Herdr's snapshot cannot be read: {exc}") from exc
    problem = surfaces._snapshot_problem(snap)
    if problem:
        raise Refused(problem)
    return snap


def _check_strand_fields(kind, strand, perspective, rnd, candidate) -> None:
    if kind in STRAND_KINDS:
        if not isinstance(strand, str) or STRAND_RE.fullmatch(strand) is None:
            raise Refused(f"--strand {strand!r} is not a strand id ([a-z0-9][a-z0-9-], 1 to 32 "
                          "characters)")
        if not isinstance(perspective, str) or PERSPECTIVE_RE.fullmatch(perspective) is None:
            raise Refused(f"--perspective {perspective!r} is not a perspective name ([a-z][a-z0-9], "
                          "1 to 6 characters)")
    elif strand or perspective or candidate:
        raise Refused(f"a {kind} surface has no strand, perspective or candidate")
    if not (isinstance(rnd, int) and not isinstance(rnd, bool) and 1 <= rnd <= MAX_ROUND):
        raise Refused(f"--round {rnd!r} is not a number from 1 to {MAX_ROUND}")


def register(*, registry, project_id: str, control, kind: str, pane_id: str, strand: str = "",
             perspective: str = "", round_: int = 1, candidate: str = "", into: str = "",
             getpid=os.getpid) -> dict:
    """Record the surface `pane_id` in the project's book. Everything is checked first
    (`Refused`, nothing written); the pane, its tab, its agent and its directory are those
    Herdr's snapshot shows now."""
    if kind not in (KIND_REVIEWER, KIND_DRAFTER, KIND_ORCHESTRATOR):
        raise Refused(f"--kind {kind!r}: a surface is registered as reviewer, drafter or "
                      "orchestrator (a run workspace is recorded by `hoh worktree`)")
    if not isinstance(pane_id, str) or PANE_RE.fullmatch(pane_id) is None:
        raise Refused(f"--pane {pane_id!r} is not a pane id")
    _check_strand_fields(kind, strand, perspective, round_, candidate)
    if into and not candidate:
        raise Refused("--into names the branch the candidate is merged into; there is no candidate")
    repo = project_repo(registry, project_id)
    if candidate:
        resolve_commit(repo, "--candidate", candidate)
        into = into or current_branch(repo)
        if is_ancestor(repo, candidate, branch_tip(repo, into)):
            raise Refused(f"--candidate {candidate[:12]} is part of {into} already: a strand "
                          "whose candidate is merged is over, not a strand to register a "
                          "surface for")
    snap = _snapshot(control)
    rows = [r for r in snap["panes"] if str(r.get("pane_id") or "") == pane_id]
    if len(rows) != 1:
        raise Refused(f"Herdr's snapshot lists pane {pane_id} {len(rows)} times, not once")
    row = rows[0]
    tab, workspace = str(row.get("tab_id") or ""), str(row.get("workspace_id") or "")
    if not tab or not workspace:
        raise Refused(f"Herdr's snapshot gives pane {pane_id} no tab or workspace")
    if kind in STRAND_KINDS:
        in_tab = [r for r in snap["panes"] if str(r.get("tab_id") or "") == tab]
        if len(in_tab) != 1:
            raise Refused(f"tab {tab} holds {len(in_tab)} panes: the reaper closes the tab of a "
                          f"{kind} surface only when that pane is alone in it")
    agents = [a for a in snap["agents"] if str(a.get("pane_id") or "") == pane_id]
    if len(agents) != 1 or not str(agents[0].get("name") or ""):
        raise Refused(f"pane {pane_id} holds {len(agents)} named agents: a {kind} surface is "
                      "one pane with one agent")
    name = str(agents[0]["name"])
    cwd = str(row.get("foreground_cwd") or row.get("cwd") or "")
    if not os.path.isabs(cwd):
        raise Refused(f"pane {pane_id} reports no absolute directory")
    if kind in STRAND_KINDS:
        broad = surfaces._breadth_problem(cwd)
        if broad:
            raise Refused(f"the directory of pane {pane_id} cannot identify it, so the reaper "
                          f"would refuse the surface for ever: {broad}")
    home = ensure_book(registry, project_id, getpid=getpid)
    reg = BookRegistry(home)
    owned = reg.owned()
    if reg.problem:
        raise Refused(f"the surface book cannot be read: {reg.problem}")
    books, why = reg.books()
    if why:
        raise Refused(f"the surface book cannot be read: {why}")
    if kind in STRAND_KINDS:
        released, why = StrandLedger(home).released()
        if why:
            raise Refused(f"the strand events cannot be read: {why}")
        state, text = strand_state(books, {a.pane_id for a in owned}, released, repo, {}, strand)
        if state != "open":
            raise Refused(f"{text}: a finished strand gets no new surface (use another strand id)")
    here = [a for a in owned if a.pane_id == pane_id]
    if here:
        # A pane is registered once. A later round of the same kind, strand and perspective may
        # raise the round and name a candidate; nothing else about it may change.
        prior = books.get(pane_id)
        if (prior is None or (prior["kind"], prior["strand"], prior["perspective"])
                != (kind, strand, perspective) or round_ <= prior["round"]):
            raise Refused(f"pane {pane_id} is registered already"
                          + (f" at round {prior['round']}" if prior else ""))
        if here[0].name != name:
            raise Refused(f"the agent in pane {pane_id} is named {name!r}, not {here[0].name!r} "
                          "as registered")
        candidate, into = candidate or prior["candidate"], into or prior["into"]
    if kind in STRAND_KINDS:
        for a in owned:
            b = books.get(a.pane_id)
            if a.pane_id != pane_id and b and (b["kind"], b["strand"],
                                               b["perspective"]) == (kind, strand, perspective):
                if a.pane_id in {str(r.get("pane_id") or "") for r in snap["panes"]}:
                    raise Refused(f"strand {strand} already has a live {kind} {perspective}: "
                                  f"pane {a.pane_id} (agent {a.name}); re-use it, or release "
                                  "the strand")
                if not surfaces._says_gone(control, a.pane_id):
                    raise Refused(f"the registered {kind} {perspective} of strand {strand} "
                                  f"(pane {a.pane_id}) is not listed, but Herdr does not say it "
                                  "is gone")
                reg.release(a.pane_id, reason="gone")
    book = {"kind": kind, "strand": strand, "perspective": perspective, "round": round_,
            "candidate": candidate, "into": into, "review": "", "workspace_id": "",
            "runs_root": ""}
    reg.register(ProgramAgentRole.program(kind), book, program=project_id,
                 backend=control.backend, pane_id=pane_id, tab_id=tab, name=name, cwd=cwd)
    return {"home": str(home), "pane_id": pane_id, "tab_id": tab, "workspace_id": workspace,
            "agent": name, "cwd": cwd, **book}


def release_strand(*, registry, project_id: str, strand: str, reason: str, getpid=os.getpid) -> dict:
    """Write the explicit terminal fact of a strand: its reviewers and drafters are over. Idempotent:
    a second release changes nothing."""
    if not isinstance(strand, str) or STRAND_RE.fullmatch(strand) is None:
        raise Refused(f"--strand {strand!r} is not a strand id")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON
            or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
        raise Refused(f"--reason must be 1 to {MAX_REASON} characters without a control character")
    project_repo(registry, project_id)                    # an unregistered project is refused
    home = ensure_book(registry, project_id, getpid=getpid)
    ledger = StrandLedger(home)
    done, why = ledger.released()
    if why:
        raise Refused(f"the strand events cannot be read: {why}")
    if strand in done:
        return {"home": str(home), "strand": strand, "released": True, "already": True}
    try:
        ledger.release(strand, reason)
    except Exception as exc:                          # noqa: BLE001
        raise Refused(f"the release cannot be written: {exc}") from exc
    return {"home": str(home), "strand": strand, "released": True, "already": False}


# --------------------------------------------------------------------------- #
# run workspaces: recorded when `hoh worktree` creates them
# --------------------------------------------------------------------------- #


def register_workspaces(*, root, repo, worktree, before: dict, after: dict, backend: str,
                        group: str = "", caller_pane: str = "",
                        caller_tab: str = "") -> tuple[list[dict], list[str]]:
    """Record the workspaces that `after` holds and `before` did not, in the record of the runs
    root `root`, **as one group** (one `group` key, and `group_size` = the number of new workspaces
    the creation made, recorded or not): Herdr's close of a target workspace ends its linked
    workspace with it, so the reaper closes one only when every member of its group passes.

    A workspace is recorded only if it is new, holds exactly one pane, and that pane sits at or
    below the worktree (the linked workspace) or the repository (the target's); every other new
    workspace gets a note and no record. A creation makes at most two workspaces, one of each: more
    than `MAX_GROUP_SIZE` new workspaces, or two of one class, is a concurrent creation (somebody
    else's workspace is among them) and **nothing** is recorded. One concurrent workspace that
    stands exactly where the target's would -- while the target existed before the call -- cannot
    be told from the target by what the snapshots show; it is recorded, and the reaper's own proofs
    (alone, idle, in its place) are all that stands between it and a close. A new workspace that
    holds the **caller's own pane or tab** (`caller_pane`, `caller_tab`: the process that creates
    the worktree is never a surface it makes) is declined like a workspace of two panes. Returns
    `(records, notes)`; never raises."""
    notes: list[str] = []
    try:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            return [], [f"runs root {root} is not a plain directory: nothing was recorded"]
        for snap, what in ((before, "before"), (after, "after")):
            problem = surfaces._snapshot_problem(snap, need_pane=False)
            if problem:
                return [], [f"the snapshot {what} the creation is unusable ({problem}): nothing "
                            "was recorded"]
        old_ws = {str(r.get("workspace_id") or "") for r in before["panes"]}
        old_panes = {str(r.get("pane_id") or "") for r in before["panes"]}
        fresh: dict[str, list[dict]] = {}
        for r in after["panes"]:
            ws, pane = str(r.get("workspace_id") or ""), str(r.get("pane_id") or "")
            if ws and ws not in old_ws and pane not in old_panes:
                fresh.setdefault(ws, []).append(r)
        if not fresh:
            return [], ["no new workspace appeared in Herdr's snapshot: nothing was recorded"]
        if len(fresh) > MAX_GROUP_SIZE:
            return [], [f"{len(fresh)} new workspaces appeared in Herdr's snapshot, more than one "
                        "creation makes (a target's and a linked one): a concurrent creation? "
                        "nothing was recorded"]
        wanted: list[tuple[str, str, str, str, str]] = []
        for ws, rows in sorted(fresh.items()):
            if any((caller_pane and str(r.get("pane_id") or "") == caller_pane)
                   or (caller_tab and str(r.get("tab_id") or "") == caller_tab) for r in rows):
                notes.append(f"workspace {ws} holds the caller's own pane or tab: not recorded")
                continue
            if len(rows) != 1:
                notes.append(f"workspace {ws} holds {len(rows)} panes: not recorded")
                continue
            row = rows[0]
            pane, tab = str(row.get("pane_id") or ""), str(row.get("tab_id") or "")
            cwd = str(row.get("foreground_cwd") or row.get("cwd") or "")
            if not pane or not tab or PANE_RE.fullmatch(ws) is None:
                notes.append(f"workspace {ws}: its pane or tab is not named: not recorded")
                continue
            linked = surfaces._at_or_below(cwd, str(worktree or ""))
            if not (linked or surfaces._at_or_below(cwd, str(repo))):
                notes.append(f"workspace {ws}: its pane is at {cwd!r}, not at or below the "
                             "worktree or the repository: not recorded")
                continue
            wanted.append((ws, pane, tab, cwd, "linked" if linked else "target"))
        classes = [w[4] for w in wanted]
        if classes.count("linked") > 1 or classes.count("target") > 1:
            return [], [f"two new workspaces stand in the place of the same one "
                        f"({', '.join(sorted(w[0] for w in wanted))}): a concurrent creation? "
                        "nothing was recorded"]
        group = group or ("g" + datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz") + "-"
                          + os.urandom(5).hex())
        reg = BookRegistry(root)
        records: list[dict] = []
        for ws, pane, tab, cwd, _klass in wanted:
            book = {"kind": KIND_WORKSPACE, "strand": "", "perspective": "", "round": 1,
                    "candidate": "", "into": "", "review": "", "workspace_id": ws,
                    "runs_root": str(root.resolve()), "group": group,
                    "group_size": len(fresh)}
            reg.register(ProgramAgentRole.program(KIND_WORKSPACE), book, program="hoh-worktree",
                         backend=backend, pane_id=pane, tab_id=tab, name="", cwd=cwd)
            records.append({"workspace_id": ws, "pane_id": pane, "tab_id": tab, "cwd": cwd,
                            "home": str(root), "group": group})
        return records, notes
    except Exception as exc:                          # noqa: BLE001 - the worktree exists either way
        return [], [f"the workspaces were not recorded ({type(exc).__name__}: {exc})"]


# --------------------------------------------------------------------------- #
# the lifecycle facts
# --------------------------------------------------------------------------- #


def _read_small(path: Path) -> bytes:
    """At most `PROCESS_JSON_LIMIT` bytes of a regular file, opened without blocking or following
    a link; anything else raises `OSError`."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        data = os.read(fd, PROCESS_JSON_LIMIT + 1)
        if len(data) > PROCESS_JSON_LIMIT:
            raise OSError(f"{path} is larger than {PROCESS_JSON_LIMIT} bytes")
        return data
    finally:
        os.close(fd)


def _plain_file_problem(path: Path, limit: int | None = None) -> str:
    """'' when `path` is absent or a regular file (not a link) of at most `limit` bytes that can be
    opened for reading; else why not. Looked at with `lstat` first: a FIFO is never opened here,
    and the open that proves readability is `O_NONBLOCK | O_NOFOLLOW`."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"cannot be examined ({type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if limit is not None and info.st_size > limit:
        return f"is larger than {limit} bytes"
    try:                       # readable at all? (`RunStore.running_process` reads an unopenable
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:     # lock as "no holder": that is a fail-open, not an answer)
        return f"cannot be opened ({type(exc).__name__})"
    os.close(fd)
    return ""


def runs_over(root) -> tuple[bool, str]:
    """`runs_state` without the anomaly flag."""
    over, why, _odd = runs_state(root)
    return over, why


def runs_state(root) -> tuple[bool, str, bool]:
    """A runs root is over when it holds at least one run directory and **every** run directory
    in it says `process.json` state `ended`, has no live holder of its process lock **and is over
    by `surfaces.run_over`** (failed, cancelled, or ready for delivery; a paused, blocked or
    checkpointed run, or one whose state cannot be read, is not). A run directory is a plain
    subdirectory that holds `state.json` or `process.json`; a link, a directory that cannot be read
    and a process marker that is missing, unreadable or not ended make the root not over.
    Returns `(over, why, odd)`: `odd` is True when a run directory holds something no run of this
    product writes -- a `state.json` or `.process.lock` that is a link, a FIFO or another
    non-regular file, that cannot be opened (mode 000: `RunStore.running_process` would read an
    unopenable lock as "no holder"), or a `state.json` larger than `STATE_JSON_LIMIT` -- which the sweep reports
    as `refused`, not as a run that is merely not over. Every file of a sibling run is `lstat`ed
    before anything opens it (`surfaces.run_over` and `RunStore.running_process` open them without
    a guard: a FIFO would block them). Never raises."""
    from .store import RunStore
    root = Path(root)
    try:
        if root.is_symlink() or not root.is_dir():
            return False, f"{root} is not a plain directory", False
        names = sorted(os.listdir(root))
    except OSError as exc:
        return False, f"the runs root cannot be read: {exc}", False
    runs = 0
    for name in names:
        path = root / name
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            return False, f"{name}: cannot be examined ({exc})", False
        if stat.S_ISLNK(mode):
            return False, f"{name} is a link: it cannot be proven a finished run", False
        if not stat.S_ISDIR(mode):
            continue
        if not (os.path.lexists(path / "state.json") or os.path.lexists(path / "process.json")):
            continue
        runs += 1
        try:
            marker = json.loads(_read_small(path / "process.json").decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            return False, f"run {name}: process.json cannot be read ({type(exc).__name__})", False
        if not isinstance(marker, dict) or marker.get("state") != "ended":
            return False, f"run {name}: process.json does not say ended", False
        for what, limit in (("state.json", STATE_JSON_LIMIT), (".process.lock", None)):
            problem = _plain_file_problem(path / what, limit)
            if problem:
                return False, f"run {name}: {what} {problem}: it is not read", True
        try:
            holder = RunStore(root, name).running_process()
        except Exception as exc:                      # noqa: BLE001 - a probe that fails proves nothing
            return False, f"run {name}: the process probe failed ({type(exc).__name__})", False
        if holder is not None:
            return False, f"run {name}: a live process holds it", False
        # An ended *process* is not an ended *run*: a run that stopped at a checkpoint, is paused
        # or is blocked has no process either, and waits for the next `hoh run`. I10a's own rule
        # (`surfaces.run_over`: failed, cancelled, or ready for delivery, and no live process)
        # is the one answer of both reapers for a run directory.
        over, why = surfaces.run_over(path)
        if not over:
            return False, f"run {name}: the run is not over: {why}", False
    if not runs:
        return False, "no run has been recorded under this runs root yet", False
    return True, f"all {runs} run(s) under the runs root have ended", False


def strand_state(books: dict, owned: set, released: set, repo: str, tips: dict,
                 strand: str) -> tuple[str, str]:
    """`('released' | 'merged' | 'open', why)` for a strand. **Released**: a valid release line names it.
    **Merged**: the strand's surfaces that are owned now carry at least one candidate and **every**
    candidate is part of the branch it was registered against (`git merge-base --is-ancestor`, the tip
    asked now, cached in `tips`); a candidate of a surface that was released (replaced, gone) does not
    count, and a fact that cannot be had (an unknown branch, an unknown commit, no repository) makes
    the strand **open**, never merged. Anything else is open."""
    if strand in released:
        return "released", f"strand {strand} is released"
    pairs = sorted({(b["candidate"], b["into"]) for pane, b in books.items()
                    if pane in owned and b["kind"] in STRAND_KINDS and b["strand"] == strand
                    and b["candidate"]})
    problem = "" if repo else "no repository is known"
    if pairs and repo:
        parts = []
        for cand, into in pairs:
            try:
                tip = tips.get(into) or tips.setdefault(into, branch_tip(repo, into))
                if is_ancestor(repo, cand, tip):
                    parts.append(f"its candidate {cand[:12]} is part of {into} ({tip[:12]})")
            except Refused as exc:
                problem = str(exc)
        if len(parts) == len(pairs):
            return "merged", f"strand {strand} is merged: " + "; ".join(parts)
    extra = f" (a merge cannot be judged: {problem})" if problem and pairs else ""
    return "open", f"strand {strand} is open: neither merged nor released{extra}"


def strand_terminal(registry, project_id: str, strand: str) -> str:
    """'' when the strand is open or has no book yet, else why it is finished -- a finished strand gets
    no new surface (`register` and `hoh review start` refuse one). A book or an event file that cannot
    be read is a `Refused`: it cannot be shown that the strand is open."""
    home = existing_book(registry, project_id)
    if home is None:
        return ""
    reg = BookRegistry(home)
    owned = {a.pane_id for a in reg.owned()}
    if reg.problem:
        raise Refused(f"the surface book cannot be read: {reg.problem}")
    books, why = reg.books()
    if why:
        raise Refused(f"the surface book cannot be read: {why}")
    released, why = StrandLedger(home).released()
    if why:
        raise Refused(f"the strand events cannot be read: {why}")
    state, text = strand_state(books, owned, released, project_repo(registry, project_id), {},
                               strand)
    return text if state != "open" else ""


#: The events the registry folds (`agentcontrol._EVENTS`); a line with another event is dropped.
_BOOK_EVENTS = frozenset({"surface", "agent", "adopt", "release"})


def book_ownership(home) -> tuple[dict[str, str], str, str, dict[str, str]]:
    """What the project's surface book says about who owns which pane, **and whether it can be
    trusted to say a pane is NOT its own** -- `(kinds, unreadable, damage, dropped)`:

    * `kinds`: `{pane id: "kind K" | "an unreadable line"}` for every pane the registry's fold
      holds as owned (the line's `book` may not parse: still the book's);
    * `unreadable`: why the file cannot be read at all ('' when it can; then `kinds` is empty);
    * `damage`: '' unless the file holds something the fold **dropped** that cannot be attributed
      to another pane -- bytes that are not UTF-8, a line that is not JSON (truncated, garbage), a
      line that is no object or whose `pane_id` is no string. `OwnedAgentRegistry.agents()` drops
      such a line silently (it counts it in `skipped`) and a book that is entirely non-UTF-8
      reads as "owns nothing" without an error: the registry cannot show that a pane is not its
      own, whatever pane it is asked about;
    * `dropped`: `{pane id: why}` for a pane that a line the fold dropped names -- an event of the
      registry that is not one (also an unhashable one), another `run_id`, a role that does not
      parse, or an `agent` / `release` event of a pane no accepted claim registered before it --
      the pane may well be the book's, and is not shown not to be.

    A line the fold accepted is never in `damage` or `dropped`; a line that names another pane,
    by a string, is not about this pane (the book may carry old lines of other runs and events of
    panes nobody registered: `skipped` alone is no reason to refuse every run workspace).
    The scan reads every line of the file, always: the fold does not drop every line it should not
    trust (a `pane_id` that is a list or a number is **accepted** as the pane `"['w9:p1']"` or `"7"`,
    not counted in `skipped`), so `skipped == 0` proves nothing about the pane being asked for."""
    reg = BookRegistry(home)
    agents = reg.agents()
    if reg.problem:
        return {}, reg.problem, "", {}
    books, why = reg.books()
    if why:
        return {}, why, "", {}
    kinds = {a.pane_id: ("kind " + books[a.pane_id]["kind"]) if a.pane_id in books
             else "an unreadable line" for a in agents if a.state == "owned"}
    try:
        text = (read_single_link(reg.path) or b"").decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return kinds, "", f"the record is not UTF-8 text ({type(exc).__name__})", {}
    damage, dropped = "", {}
    registered: set[str] = set()      # panes an accepted claim named EARLIER in the file (the fold's `panes`)
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            damage = damage or f"line {number} is not JSON (truncated or garbage)"
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("pane_id"), str):
            damage = damage or f"line {number} is no object with a pane id"
            continue
        pane, event = rec["pane_id"], rec.get("event")
        if not pane:
            damage = damage or f"line {number} names no pane"
            continue
        # Every line the fold drops that names a pane is doubt about THAT pane, whatever its event:
        # the fold drops an `agent` / `release` of a pane no accepted claim registered before it
        # ("an event for a pane nobody registered"), and HoH writes both only after a claim -- an
        # orphan one is evidence that the pane's registration was lost.
        why_dropped = ""
        if not isinstance(event, str) or event not in _BOOK_EVENTS:
            why_dropped = f"line {number}: event {event!r} is not an event of the registry"
        elif rec.get("run_id") != reg.run_id:
            why_dropped = f"line {number}: it belongs to run {rec.get('run_id')!r}, not {reg.run_id!r}"
        elif event in ("agent", "release"):
            if pane not in registered:
                why_dropped = f"line {number}: an {event} event of a pane no claim registered before it"
        else:
            try:
                ProgramAgentRole.parse(rec["role"])
            except (ValueError, KeyError, TypeError):
                why_dropped = f"line {number}: its role does not parse"
            else:
                registered.add(pane)
        if why_dropped:
            dropped.setdefault(pane, why_dropped)
    return kinds, "", damage, dropped


@dataclass
class Context:
    """What the strand rule asks the project: its repository, the tips of the branches strands were
    registered against and the released strands. Read lazily and once; a fact that cannot be had is
    not a reason to close."""

    registry: object = None
    project_id: str = ""
    repo: str = ""
    tips: dict = field(default_factory=dict)
    tip_problem: str = ""
    released: set = field(default_factory=set)
    released_problem: str = ""
    _loaded: bool = False

    def load(self, book_home: Path | None) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.registry is not None and self.project_id:
            try:
                self.repo = project_repo(self.registry, self.project_id)
            except Refused as exc:
                self.tip_problem = str(exc)
        else:
            self.tip_problem = "no project was given: a merge cannot be judged"
        if book_home is not None:
            self.released, self.released_problem = StrandLedger(book_home).released()


class Rule:
    """The terminal rule of `surfaces.reap` for the surfaces of homes: `(home, surface) -> (over,
    why)`. `strand` restricts a sweep to one strand (every other strand's surface is not over)."""

    def __init__(self, context: Context, *, book_home: Path | None, strand: str = "") -> None:
        self.context, self.book_home, self.strand = context, book_home, strand
        self._books: dict[str, tuple[dict, str]] = {}
        self._owned: dict[str, set] = {}
        #: `(home, pane id)` -> why, for a run workspace whose runs root holds something no run
        #: writes (`runs_state`'s `odd`): `refuse_for` turns its `pending` into `refused`.
        self.anomalies: dict[tuple[str, str], str] = {}
        self._book_kinds: dict[str, str] | None = None
        self._book_problem = ""       # the book cannot be read at all
        self._book_damage = ""        # it can, and holds a line nobody can attribute (book_ownership)
        self._book_dropped: dict[str, str] = {}

    def book_of(self, home, pane_id: str) -> dict | None:
        key = str(home)
        if key not in self._books:
            reg = BookRegistry(home)
            self._books[key] = reg.books()
            self._owned[key] = {a.pane_id for a in reg.owned()}
        return self._books[key][0].get(pane_id)

    def _is_book(self, home) -> bool:
        """`home` is the project's own surface book (the directory, not a name or a spelling)."""
        return (self.book_home is not None
                and os.path.realpath(home) == os.path.realpath(self.book_home))

    def _book_owner(self, pane_id: str) -> str:
        """How the project's book owns `pane_id` now ("kind K", or "an unreadable line"), '' when it
        does not.
        A book that cannot be read cannot show that the pane is not its own: its reason is kept
        in `_book_problem`; one that is damaged (`book_ownership`) in `_book_damage` and
        `_book_dropped`; the caller asks `_book_doubt` and says "not over"."""
        if self._book_kinds is None:
            self._book_kinds = {}
            if self.book_home is not None:
                (self._book_kinds, self._book_problem, self._book_damage,
                 self._book_dropped) = book_ownership(self.book_home)
        return self._book_kinds.get(pane_id, "")

    def _book_doubt(self, pane_id: str) -> str:
        """Why the project's book cannot show that `pane_id` is not its own ('' when it can): it
        is damaged (a dropped line that no pane can be told from), or a dropped line names the pane."""
        self._book_owner(pane_id)
        if self._book_damage:
            return self._book_damage
        return self._book_dropped.get(pane_id, "")

    def refuse_for(self, home, surface) -> str:
        """The reason a surface that is not over is `refused` rather than `pending` ('' for the
        ordinary case): its runs root holds a file no run of this product writes."""
        return self.anomalies.get((str(home), surface.pane_id), "")

    def _strand_state(self, home, book: dict) -> tuple[bool, str]:
        ctx = self.context
        ctx.load(self.book_home)
        state, text = strand_state(self._books[str(home)][0], self._owned[str(home)], ctx.released,
                                   ctx.repo, ctx.tips, book["strand"])
        if state != "open":
            return True, text
        if ctx.released_problem:
            text += f" (the strand events cannot be read: {ctx.released_problem})"
        if ctx.tip_problem:
            text += f" (a merge cannot be judged: {ctx.tip_problem})"
        return False, text

    def __call__(self, home, surface) -> tuple[bool, str]:
        book = self.book_of(home, surface.pane_id)
        if book is None:
            return False, ("no lifecycle record: the surface was not registered with a kind, so "
                           "nothing says it is finished")
        kind = book["kind"]
        if kind == KIND_ORCHESTRATOR:
            return False, "an orchestrator surface is never reaped automatically"
        if self.strand and book["strand"] != self.strand:
            return False, f"not in strand {self.strand}"
        if kind in STRAND_KINDS:
            if not self._is_book(home):
                return False, ("a reviewer's or drafter's strand is judged in the project's own "
                               "surface book only: this home is not that book (another "
                               "project's, or none was given), so no release or merge applies "
                               "to it")
            return self._strand_state(home, book)
        if kind == KIND_WORKSPACE:
            if os.path.realpath(book["runs_root"]) != os.path.realpath(home):
                return False, (f"recorded for the runs root {book['runs_root']}, which is not "
                               f"{home}: the runs that decide are not these")
            owner = self._book_owner(surface.pane_id)
            if owner:
                return False, (f"the pane is recorded in the project's surface book ({owner}): "
                               "it is not a run workspace")
            if self._book_problem:
                return False, ("the project's surface book cannot be read, so it is not shown "
                               f"that the pane is not its own: {self._book_problem}")
            doubt = self._book_doubt(surface.pane_id)
            if doubt:
                return False, ("the project's surface book is damaged: it cannot show that the "
                               f"pane is not its own ({doubt})")
            over, why, odd = runs_state(home)
            if odd:
                self.anomalies[(str(home), surface.pane_id)] = why
            return over, why
        return False, f"kind {kind!r} has no terminal rule"

    def workspace_of(self, home, surface) -> str:
        book = self.book_of(home, surface.pane_id)
        return book["workspace_id"] if book and book["kind"] == KIND_WORKSPACE else ""

    def group_of(self, home, surface) -> tuple[list[tuple[str, str]], str]:
        """`(mates, problem)`: the other recorded workspaces of the surface's group, as
        `(pane id, workspace id)`, and a reason when the record does not hold the whole group
        (a member that was never recorded would be closed with it, unexamined) or holds more than
        the creation made. `([], "")` for a surface that is not a run workspace."""
        book = self.book_of(home, surface.pane_id)
        if book is None or book["kind"] != KIND_WORKSPACE:
            return [], ""
        books = self._books[str(home)][0]
        mates = sorted((pane, b["workspace_id"]) for pane, b in books.items()
                       if pane != surface.pane_id and b["kind"] == KIND_WORKSPACE
                       and b["group"] == book["group"])
        if 1 + len(mates) != book["group_size"]:
            return [], (f"workspace {book['workspace_id']} was created together with "
                        f"{book['group_size'] - 1} other workspace(s) of which "
                        f"{len(mates)} are in the record: a close of it may end one that was "
                        "never examined")
        return mates, ""


# --------------------------------------------------------------------------- #
# sweep: `hoh reap`
# --------------------------------------------------------------------------- #


def discover_homes(roots) -> list[Path]:
    """Each directory in `roots` and its direct plain subdirectories that hold a surface record
    with at least one `book` line, or a record that cannot be read at all (the sweep reports it).
    A record is found by its content, never by a directory's name. A link is not followed."""
    found: list[Path] = []
    for base in roots:
        base = Path(base)
        try:
            if base.is_symlink() or not base.is_dir():
                raise Refused(f"--root {base} is not a plain directory")
            children = [base / n for n in sorted(os.listdir(base))]
        except OSError as exc:
            raise Refused(f"--root {base} cannot be read: {exc}") from exc
        for cand in [base, *children]:
            try:
                mode = os.lstat(cand).st_mode
            except OSError:
                continue
            if not stat.S_ISDIR(mode) or not os.path.lexists(cand / surfaces.SURFACES_FILE):
                continue
            books, why = BookRegistry(cand).books()
            if (books or why) and cand not in found:
                found.append(cand)
    return found


def _same_dir(a, b) -> bool:
    return b is not None and os.path.realpath(a) == os.path.realpath(b)


def sweep(*, registry=None, project_id: str = "", roots=(), strand: str = "", apply: bool = False,
          self_pane: str = "", self_tab: str = "", self_inside: bool = False) -> dict:
    """Judge every surface of the project's book and of the runs roots, close those whose lifecycle
    is over and that the reaper proves safe (`apply`), and say what happened to each. Raises
    `Refused` (nothing done) for input that cannot be used and the reaper's own exceptions for a
    Herdr that cannot be reached."""
    if strand and STRAND_RE.fullmatch(strand) is None:
        raise Refused(f"--strand {strand!r} is not a strand id")
    homes: list[Path] = []
    book_home: Path | None = None
    if project_id:
        if registry is None:
            raise Refused("no registry")
        project_repo(registry, project_id)               # an unregistered project is refused
        book_home = existing_book(registry, project_id)
        if book_home is not None:
            homes.append(book_home)
    known = {os.path.realpath(h) for h in homes}
    for found in discover_homes(roots):
        if os.path.realpath(found) not in known:
            known.add(os.path.realpath(found))
            homes.append(found)
    ctx = Context(registry=registry, project_id=project_id)
    rule = Rule(ctx, book_home=book_home, strand=strand)
    rows: list[dict] = []
    for home in homes:
        _, unreadable = BookRegistry(home).books()
        if unreadable:                                # the reaper would read it whole: it does not
            if not strand or _same_dir(home, book_home):   # no strand is known of a foreign home:
                rows.append({"home": str(home), "kind": "", "strand": "", "perspective": "",
                             "round": 0, "workspace_id": "", "pane_id": "", "tab_id": "",
                             "outcome": "refused",
                             "why": f"the surfaces record cannot be read: {unreadable}"})
            continue                                  # not print it; `homes` still names it
                                                      # (the project's own book is always printed)
        entries = surfaces.reap(home, close=apply, self_pane=self_pane, self_tab=self_tab,
                                self_inside=self_inside, over_for=rule,
                                workspace_of=rule.workspace_of, group_of=rule.group_of,
                                refuse_for=rule.refuse_for)
        for e in entries:
            book = rule.book_of(home, e.get("pane_id", "")) or {}
            if strand and book.get("strand") != strand:
                continue
            rows.append({"home": str(home), "kind": book.get("kind", ""),
                         "strand": book.get("strand", ""),
                         "perspective": book.get("perspective", ""),
                         "round": book.get("round", 0),
                         "workspace_id": book.get("workspace_id", ""),
                         **{k: e[k] for k in ("pane_id", "tab_id", "outcome", "why")}})
    return {"apply": bool(apply), "tips": dict(sorted(ctx.tips.items())),
            "homes": [str(h) for h in homes], "surfaces": rows}


def reap_root_after_run(root) -> None:
    """The end of `hoh run`, after the process marker is released: the workspaces recorded for the
    runs root of the run, if every run there has ended. Never raises, never changes the exit code,
    never writes to stdout; a root with no record does nothing."""
    try:
        root = Path(root)
        if not os.path.lexists(root / surfaces.SURFACES_FILE):
            return
        books, why = BookRegistry(root).books()
        if not books and not why:
            return
        if why:
            surfaces._note(f"workspaces not reaped: the surfaces record cannot be read: {why}")
            return
        pane, tab, inside = surfaces.own_location()
        rule = Rule(Context(), book_home=None)
        for r in surfaces.reap(root, close=True, self_pane=pane, self_tab=tab, self_inside=inside,
                               over_for=rule, workspace_of=rule.workspace_of,
                               group_of=rule.group_of, refuse_for=rule.refuse_for):
            if r["outcome"] in ("kept", "refused"):
                surfaces._note(f"workspace {r['pane_id'] or r['run_id']} stays: {r['why']}")
    except Exception as exc:                          # noqa: BLE001 - the end of a run must not fail here
        try:
            text = f"surfaces: workspaces not reaped ({type(exc).__name__}: {exc})"
        except Exception:                             # noqa: BLE001
            text = "surfaces: workspaces not reaped"
        surfaces._note(text)
