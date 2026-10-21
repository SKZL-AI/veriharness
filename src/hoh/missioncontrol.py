"""`hoh mission`: read-only views of what the product already writes under a runs root.

The thin Mission Control (rows P3-33 to P3-38 of the plan). Six views:

* `overview` (P3-33): "which runs are there and what is each one doing?" -- one row per run.
* `board` (P3-35, Running / Ready / Blocked) and `needs-you` (P3-36): "what runs, what can run,
  what waits, and what waits for a person?" for one project -- the project's graph
  (`projects/<id>/project.json`) read through `decisionfrontier.derive_frontier`, with the
  routing log of each BLOCKED node's run (`governance.read_branch_stops`) as the evidence of why
  it waits.
* `map` (P3-34, Mission Map): the same graph as a structure -- every node with the place the
  board gives it, its level, what it depends on, what depends on it, the dependencies that name
  no node, and the cycles. It reads the same project, the same logs and the same frontier as the
  board and places nothing of its own.
* `timeline` (P3-37): the dated records of the project file -- dispatch events, decisions, gate
  results and external actions -- in one list, oldest first.
* `evidence` (P3-38, Evidence health): the gates of the latest closure pass and whether each
  measured the commit the closure attests, and, per run a node names, the counts of its
  evidence bundle (`evidence.json`) by status.

Each is a *view*: nothing here decides, resumes, unblocks, answers, cancels or closes anything,
and nothing here is authoritative -- the state files are. Nothing here is a scheduler: the board
says what the frontier says, and what the loop dispatches next is not decided here.

It writes nothing: no state, no lock file, no marker, no parked copy, no log, no directory. A run
is looked at the way `hoh wait RUN --timeout 0` looks at it (`runwait.observe_run`: the process
lock first, then `state.json` parsed in memory, never `RunStore.read_state`, which parks and
rewrites a state that needs a migration) and the row's `phase` is `runwait.phase_of` of that look,
so the two commands cannot disagree about a run. The product's reader trusts what it opens; this
view does not: before a run is looked at, `state.json`, `process.json` and `.process.lock` must be
absent (the last two) or regular files that are not symbolic links, `state.json` at most
`MAX_STATE_BYTES`, and the marker a JSON object -- a FIFO would block the reader, an unreadable
lock would read as "nothing executes this run".

The project views read `project.json` with the same discipline (`_read_project`): the project
directory and the file are not links, the file is a regular file of at most `MAX_PROJECT_BYTES`
(a FIFO would block `ProjectStore.read_state`, which is not called), UTF-8, at the current schema
version, a `ProjectState` whose `project_id` is the directory's; anything else is a refusal
(exit 2, nothing printed). A routing log that cannot be read is never skipped: the node shows as
an `UNREADABLE` decision (when its kind says it waits for an answer) and the log is listed under
`unreadable`, which makes the exit code 1.

Never silent: every entry of the root is either shown as a row or *proven* not to be a run --
a name the root keeps for itself, a plain file, or a directory whose `state.json` entry is
absent (`ENOENT`/`ENOTDIR`). An entry that cannot be examined (no search permission on the run
directory or on the root, an I/O error) is a row with `readable: false` and the reason; so is a
run that cannot be looked at. A row is never left out and never given a phase it did not earn.
Exit codes: 0 every run looked at, 1 at least one row is unreadable, 2 the root is not usable
(the command itself failed).

Limits:
  L1  A view of one moment: a run that changes while the rows are collected is read once, each
      row at its own moment.
  L2  The checks that keep the reader from blocking are made on the path before the reader opens
      it; a file swapped for a FIFO in between is not caught (a same-user race).
  L3  A directory whose `state.json` is proven absent is not a run and is not listed; a symbolic
      link in the root is listed as unreadable, never followed; a directory that cannot be
      searched is listed as unreadable even if it was never a run (it cannot be told).
  L4  Timers and the runs' own histories (`state.json` `history`, receipts, routing logs of
      nodes that are not BLOCKED) are later slices; `board`, `needs-you` and `map` read the
      project graph and the routing logs of its BLOCKED nodes only, `timeline` the project file
      only, `evidence` the project file and the `evidence.json` of the runs its nodes name; none
      says anything of a node's run beyond that (the Overview is the look at a run).
  L5  The project views are a view of one moment: the project state is read once, each routing
      log and each evidence bundle at its own moment, and a log read while a run appends to it
      may be refused as torn.
  L6  The map is the graph the project file declares (`dependencies`); it does not read
      `writes`, `semantic_reads` or `produces`, so two nodes that conflict on a path without a
      dependency between them are two unrelated nodes here.
  L7  The timeline orders by the stamp each record carries, as written (a stamp of the form
      `YYYY-MM-DDTHH:MM:SSZ`, compared as text); records with the same stamp keep the order of
      their source (dispatch events, decisions, external actions, gates) and of their list. A
      record whose stamp is not of that form is listed first and counted as undated, never
      dropped and never cut by `TIMELINE_ROWS` (which applies to the dated records, so a
      list may hold more than `TIMELINE_ROWS` rows by the number of undated ones); the stamp
      is not checked against a calendar or against the machine's clock.
  L8  `current` of a gate says only that its `subject` is the project's `measurement_head`;
      nothing here re-measures anything, and an evidence bundle's items are not checked against
      the tree (the product's evidence items carry no read set, so "stale because X changed" is
      not derivable).
  L9  The size of an answer follows the size of the project file (at most `MAX_PROJECT_BYTES`):
      the map and the evidence runs are bounded by `MAX_NODES`, the timeline by `TIMELINE_ROWS`,
      but the gates of the latest pass are all listed (24 000 gates of one pass were answered in
      0.2 s as a 1.2 MB document). Nothing is dropped to keep an answer short.
      The worst accepted evidence case (2000 bundles of 1 MiB) is not timed here (a reviewer
      measured 7 ms for one such bundle: about 15 to 25 s in all, and nothing is printed until the
      look is done).
  L10 `evidence` opens the run directory of every run a node names (the board opens only the
      logs of BLOCKED nodes): a project whose runs were moved out of the runs root (a
      relocated or archived run) answers exit 1 with one `UNREADABLE ... the run directory does
      not exist` row per such run until they are back. Honest, and a standing alarm for such a
      project.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, TextIO

from . import doubledown, migrations, runwait
from .decisionfrontier import TIMED_KINDS, DecisionFrontier, FrontierError, derive_frontier
from .evidence import EvidenceBundle
from .governance import read_branch_stops
from .project import Lifecycle, ProjectState
from .projectstore import ProjectStore
from .store import RESERVED_PREFIXES, RESERVED_RUN_IDS, RunStore, StoreError

SCHEMA = "hoh-mission/1"
VIEWS = ("overview", "board", "needs-you", "map", "timeline", "evidence")
#: The views of one project (`--project ID`); the overview takes none.
PROJECT_VIEWS = ("board", "needs-you", "map", "timeline", "evidence")

#: The largest `state.json` that is looked at; a larger file is a row that says so.
MAX_STATE_BYTES = 8 * 1024 * 1024
#: The largest `project.json` that is read; a larger file is a refusal.
MAX_PROJECT_BYTES = 8 * 1024 * 1024
#: The most nodes, and the most dependency entries in all, a project view is computed for. The
#: frontier is `O(C * (N + E))` for `C` causes: 2000 nodes in a chain took 0.5 s, 2000 nodes of 300
#: dependencies each (600 000 entries) 4 to 7 s, and an 8 MiB file holds some 490 000 minimal nodes,
#: which is refused in 0.15 s (the count is made on the parsed record, before the model is
#: built). Real graphs are tens to hundreds of nodes with a few dependencies each; a larger one is
#: a refusal that says so, not a view that seems to hang.
MAX_NODES = 2000
MAX_DEPENDENCIES = 20000
#: The largest `process.json` that is parsed.
MAX_MARKER_BYTES = 64 * 1024
#: The largest `evidence.json` that is read; a larger one is listed as unreadable.
MAX_EVIDENCE_BYTES = 1024 * 1024
#: The most dated records the timeline lists (the newest); the rest are counted as `omitted`.
#: A record without a stamp of the product's form is never cut by it.
TIMELINE_ROWS = 500
#: Bounds that keep one text row one short line, and one error message short.
NAME_WIDTH = 80
KIND_WIDTH = 40
TEXT_REASON_WIDTH = 120
ERROR_LIMIT = 240

#: exit codes
OK, SOME_UNREADABLE, REFUSED = 0, 1, 2


class MissionRefused(Exception):
    """The root cannot be listed: nothing was looked at."""


class _Unfit(Exception):
    """A run that is not looked at, and why (the text is the row's `error`)."""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _plain(text: object, limit: int | None = None) -> str:
    """One line of printable ASCII: `ascii()` escapes control characters, escape sequences, lone
    surrogates and every non-ASCII character, so that no name or reason can drive a terminal or
    break an encoder."""
    plain = ascii(str(text))[1:-1]
    return plain if limit is None else _clip(plain, limit)


def _open_regular(path: str, limit: int) -> int:
    """A descriptor for the regular file at `path`, or an `OSError`/`_Unfit` that names why not.
    Never blocks (`O_NONBLOCK`), never follows a link (`O_NOFOLLOW`); the kind is read from the
    descriptor, not from the path a moment earlier."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Unfit("is not a regular file")
        if st.st_size > limit:
            raise _Unfit(f"is larger than {limit} bytes (not read)")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _problem(run_dir: str) -> str | None:
    """Why this run directory cannot be looked at without risk, or None."""
    for name, limit, required in (("state.json", MAX_STATE_BYTES, True),
                                  ("process.json", MAX_MARKER_BYTES, False),
                                  (".process.lock", MAX_MARKER_BYTES, False)):
        path = os.path.join(run_dir, name)
        try:
            os.lstat(path)
        except FileNotFoundError:
            if required:
                return f"{name} is missing"
            continue
        except OSError as exc:
            return f"{name} cannot be examined: {exc.strerror or type(exc).__name__}"
        try:
            fd = _open_regular(path, limit)
        except OSError as exc:
            why = ("is a symbolic link (not followed)" if exc.errno == errno.ELOOP
                   else f"cannot be opened: {exc.strerror or type(exc).__name__}")
            return f"{name} {why}"
        except _Unfit as exc:
            return f"{name} {exc}"
        try:
            if name == "process.json":
                try:
                    marker = json.loads(os.read(fd, MAX_MARKER_BYTES + 1).decode("utf-8"))
                except (ValueError, RecursionError):
                    marker = None
                if not isinstance(marker, dict):
                    return "process.json is not a JSON object: liveness cannot be told"
        finally:
            os.close(fd)
    return None


def _row(root: str, name: str) -> dict[str, Any]:
    try:
        run_dir = os.path.join(root, name)
        if stat.S_ISLNK(os.lstat(run_dir).st_mode):
            raise _Unfit("is a symbolic link (not followed)")
        problem = _problem(run_dir)
        if problem is not None:
            raise _Unfit(problem)
        obs = runwait.observe_run(RunStore(root, name))
    except _Unfit as exc:
        return {"run_id": name, "readable": False, "error": _plain(exc, ERROR_LIMIT)}
    except Exception as exc:                          # noqa: BLE001 - a row, never a crash
        return {"run_id": name, "readable": False,
                "error": _plain(f"{type(exc).__name__}: {exc}", ERROR_LIMIT)}
    return {"run_id": name, "readable": True, "phase": runwait.phase_of(obs),
            **dataclasses.asdict(obs)}


def _is_run(child: str) -> tuple[bool, str | None]:
    """`(is_row, why_not_examinable)`: a row when the entry is a link or a directory holding a
    `state.json` entry of any kind, or when the entry cannot be examined; not a row only when it
    is proven not to be a run (`ENOENT`/`ENOTDIR`, a plain file)."""
    try:
        kind = os.lstat(child).st_mode
    except FileNotFoundError:
        return False, None                             # gone since the listing
    except OSError as exc:
        return True, f"cannot be examined: {exc.strerror or type(exc).__name__}"
    if stat.S_ISLNK(kind):
        return True, None
    if not stat.S_ISDIR(kind):
        return False, None
    try:
        os.lstat(os.path.join(child, "state.json"))
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError as exc:
        return True, f"state.json cannot be examined: {exc.strerror or type(exc).__name__}"
    return True, None


def _resolve_root(root: str) -> str:
    try:
        return str(Path(os.path.expanduser(str(root))).resolve())
    except (OSError, RuntimeError) as exc:
        raise MissionRefused(f"the runs root {str(root)!r} cannot be resolved: {exc}") from None


def overview(root: str) -> dict[str, Any]:
    """The rows of every run under `root`, in the order of their names. Writes nothing."""
    base = _resolve_root(root)
    try:
        entries = sorted(os.listdir(base))
    except OSError as exc:
        raise MissionRefused(f"the runs root {base!r} cannot be listed: "
                             f"{exc.strerror or type(exc).__name__}") from None
    rows: list[dict[str, Any]] = []
    for name in entries:
        if name.startswith(RESERVED_PREFIXES) or name in RESERVED_RUN_IDS:
            continue
        is_row, unexaminable = _is_run(os.path.join(base, name))
        if unexaminable is not None:
            rows.append({"run_id": name, "readable": False, "error": _plain(unexaminable, ERROR_LIMIT)})
        elif is_row:
            rows.append(_row(base, name))
    phases: dict[str, int] = {}
    for row in rows:
        if row["readable"]:
            phases[row["phase"]] = phases.get(row["phase"], 0) + 1
    unreadable = sum(1 for row in rows if not row["readable"])
    return {"schema": SCHEMA, "view": "overview", "root": base,
            "totals": {"runs": len(rows), "readable": len(rows) - unreadable,
                       "unreadable": unreadable, "by_phase": dict(sorted(phases.items()))},
            "runs": rows}


# --------------------------------------------------------------------------- #
# The project views: board (P3-35) and needs-you (P3-36)
# --------------------------------------------------------------------------- #


def _read_all(fd: int, limit: int) -> bytes:
    """At most `limit + 1` bytes of the descriptor: one more than allowed says "too large"."""
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(1 << 20, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _refuse(project_id: str, why: object) -> MissionRefused:
    return MissionRefused(f"project {_plain(project_id, NAME_WIDTH)}: {_plain(why, ERROR_LIMIT)}")


def _read_project(base: str, project_id: str) -> ProjectState:
    """The state of the project, read in memory the way `ProjectStore.read_state` reads it but
    without its hazards (a FIFO blocks it, a link is followed, a size is not bounded), or a
    `MissionRefused` that says why not. Writes nothing."""
    try:
        store = ProjectStore(base, project_id)           # the id's name check; creates nothing
    except StoreError as exc:
        raise _refuse(project_id, f"is not a project id: {exc}") from None
    for path, what in ((store.dir.parent, "the projects directory"),
                       (store.dir, "the project directory")):
        try:
            kind = os.lstat(path).st_mode
        except FileNotFoundError:
            raise _refuse(project_id, f"{what} does not exist") from None
        except OSError as exc:
            raise _refuse(project_id, f"{what} cannot be examined: "
                                      f"{exc.strerror or type(exc).__name__}") from None
        if stat.S_ISLNK(kind):
            raise _refuse(project_id, f"{what} is a symbolic link (not followed)")
        if not stat.S_ISDIR(kind):
            raise _refuse(project_id, f"{what} is not a directory")
    try:
        fd = _open_regular(str(store.state_path), MAX_PROJECT_BYTES)
    except FileNotFoundError:
        raise _refuse(project_id, "has no project.json") from None
    except OSError as exc:
        why = ("is a symbolic link (not followed)" if exc.errno == errno.ELOOP
               else f"cannot be opened: {exc.strerror or type(exc).__name__}")
        raise _refuse(project_id, f"project.json {why}") from None
    except _Unfit as exc:
        raise _refuse(project_id, f"project.json {exc}") from None
    try:
        raw = _read_all(fd, MAX_PROJECT_BYTES)
    except OSError as exc:
        raise _refuse(project_id, f"project.json cannot be read: "
                                  f"{exc.strerror or type(exc).__name__}") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_PROJECT_BYTES:
        raise _refuse(project_id, f"project.json is larger than {MAX_PROJECT_BYTES} bytes (not read)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _refuse(project_id, f"project.json is not UTF-8: {exc}") from None
    try:
        record = json.loads(text)
    except migrations.UNPARSEABLE:
        record = None
    if isinstance(record, dict):
        try:
            migrations.require_readable(record, registry=migrations.STATE_REGISTRIES["project"],
                                        what="project.json")
        except migrations.MigrationError as exc:
            raise _refuse(project_id, exc) from None
    nodes = record.get("nodes") if isinstance(record, dict) else None
    if isinstance(nodes, list):                           # counted before the model is built
        if len(nodes) > MAX_NODES:
            raise _refuse(project_id, f"has {len(nodes)} nodes, more than the {MAX_NODES} the "
                                      f"view is computed for")
        edges = sum(len(n["dependencies"]) for n in nodes
                    if isinstance(n, dict) and isinstance(n.get("dependencies"), list))
        if edges > MAX_DEPENDENCIES:
            raise _refuse(project_id, f"has {edges} dependency entries, more than the "
                                      f"{MAX_DEPENDENCIES} the view is computed for")
    try:
        state = ProjectState.model_validate_json(text)
    except (ValueError, RecursionError) as exc:
        raise _refuse(project_id, f"project.json is not a project state: {exc}") from None
    if state.project_id != project_id:
        raise _refuse(project_id, f"project.json says it is project "
                                  f"{_plain(state.project_id, NAME_WIDTH)}: not read as this one")
    return state


def _log_of(base: str, run_id: str) -> list:
    """The stopping routings of the log of run `run_id`, or an exception that says why not. A
    run id that is not a run directory's name, a run directory that is absent or a link, and a
    log `read_branch_stops` refuses are all refusals here, never an empty log."""
    run_dir = RunStore(base, run_id).dir
    try:
        linked = stat.S_ISLNK(os.lstat(run_dir).st_mode)
    except FileNotFoundError:
        raise _Unfit("the run directory does not exist: its log cannot be read") from None
    if linked:
        raise _Unfit("the run directory is a symbolic link (not followed)")
    return read_branch_stops(run_dir)


def _routings(base: str, state: ProjectState) -> tuple[dict[str, list], dict[str, str]]:
    """`(stops, unreadable)` for `derive_frontier`: for every BLOCKED node that names a run, the
    routings of that run's log that stopped its branch, or why the log cannot be read. Each
    distinct run id is read once, whatever number of nodes name it."""
    stops: dict[str, list] = {}
    unreadable: dict[str, str] = {}
    seen: dict[str, tuple[list | None, str | None]] = {}
    for node in state.nodes:
        if node.lifecycle is not Lifecycle.BLOCKED or node.run_id is None:
            continue
        if node.run_id not in seen:
            try:
                seen[node.run_id] = (_log_of(base, node.run_id), None)
            except _Unfit as exc:
                seen[node.run_id] = (None, _plain(exc, ERROR_LIMIT))
            except Exception as exc:                      # noqa: BLE001 - listed, never a crash
                seen[node.run_id] = (
                    None, _plain(f"{type(exc).__name__}: {exc}", ERROR_LIMIT))
        routings, why = seen[node.run_id]
        if why is None:
            stops[node.id] = list(routings)
        else:
            unreadable[node.id] = why
    return stops, unreadable


def board(state: ProjectState, frontier: DecisionFrontier) -> dict[str, Any]:
    """Every node once: running, ready, queued, blocked, contingent, or settled (counted)."""
    by_id = {n.id: n for n in state.nodes}
    decisions = {d.node_id: d for d in frontier.decisions}
    runnable, other = set(frontier.runnable), set(frontier.other_blocked)
    unrunnable = set(frontier.unrunnable)
    held_by: dict[str, list[str]] = {}
    for n in state.nodes:                                 # graph order, one pass over the holds
        if n.id in decisions:
            for held in decisions[n.id].holds:
                held_by.setdefault(held, []).append(n.id)
    running: list[dict] = []
    ready: list[dict] = []
    queued: list[dict] = []
    blocked: list[dict] = []
    contingent: list[dict] = []
    merged = abandoned = 0
    for n in state.nodes:
        if n.lifecycle is Lifecycle.MERGED:
            merged += 1
        elif n.lifecycle is Lifecycle.ABANDONED:
            abandoned += 1
        elif n.lifecycle is Lifecycle.CONTINGENT:
            contingent.append({"node_id": n.id})
        elif n.lifecycle is Lifecycle.RUNNING:
            running.append({"node_id": n.id, "run_id": n.run_id})
        elif n.id in decisions:
            d = decisions[n.id]
            blocked.append({"node_id": n.id, "waits_for": "decision" if d.resolvable else "nobody",
                            "kind": d.kind, "handling": d.handling, "retry_after": d.retry_after})
        elif n.id in other:
            blocked.append({"node_id": n.id,
                            "waits_for": "time" if n.blocked_kind in TIMED_KINDS else "other",
                            "kind": n.blocked_kind or "", "handling": None,
                            "retry_after": n.retry_after})
        elif n.id in runnable:
            ready.append({"node_id": n.id})
        elif n.lifecycle is Lifecycle.READY:
            queued.append({
                "node_id": n.id,
                "waiting_on": [d for d in dict.fromkeys(n.dependencies)
                               if d not in by_id or not by_id[d].settled],
                "held_by": held_by.get(n.id, []),
                "unrunnable": n.id in unrunnable})
        else:                                             # a BLOCKED node the frontier left out
            raise MissionRefused(f"node {_plain(n.id, NAME_WIDTH)} is blocked and the frontier "
                                 f"does not place it: the board is not shown")
    return {"totals": {"nodes": len(state.nodes), "running": len(running), "ready": len(ready),
                       "queued": len(queued), "blocked": len(blocked),
                       "contingent": len(contingent), "merged": merged, "abandoned": abandoned},
            "running": running, "ready": ready, "queued": queued, "blocked": blocked,
            "contingent": contingent}


def needs_you(frontier: DecisionFrontier) -> dict[str, Any]:
    """The open decisions in the frontier's own order (most freed work first), and apart from
    them the branches the contract ended, which nobody answers."""
    open_: list[dict] = []
    ended: list[dict] = []
    for d in frontier.decisions:
        row = {"node_id": d.node_id, "handling": d.handling, "kind": d.kind,
               "decision": d.decision, "why": d.why, "retry_after": d.retry_after,
               "holds": list(d.holds)}
        if d.resolvable:
            open_.append({"rank": len(open_) + 1, **row, "frees": list(d.frees)})
        else:
            ended.append(row)
    return {"totals": {"open": len(open_), "ended": len(ended)},
            "decisions": open_, "ended": ended}


def _cycles(ids: list[str], needs: dict[str, list[str]]) -> list[list[str]]:
    """The strongly connected components of the dependency graph that are cycles (two or more
    nodes, or a node that depends on itself), each in graph order, ordered by their first
    member. Iterative (Tarjan): a chain of 2000 nodes does not reach the recursion limit."""
    order = {i: k for k, i in enumerate(ids)}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []
    counter = 0
    for root in ids:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(needs[root]))]
        while work:
            node, it = work[-1]
            advanced = False
            for dep in it:
                if dep not in index:
                    index[dep] = low[dep] = counter
                    counter += 1
                    stack.append(dep)
                    on_stack.add(dep)
                    work.append((dep, iter(needs[dep])))
                    advanced = True
                    break
                if dep in on_stack:
                    low[node] = min(low[node], index[dep])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    members.append(top)
                    if top == node:
                        break
                if len(members) > 1 or node in needs[node]:
                    found.append(sorted(members, key=order.__getitem__))
    return sorted(found, key=lambda c: order[c[0]])


def mission_map(state: ProjectState, placed: dict[str, Any]) -> dict[str, Any]:
    """Every node once, with the place the board gives it (`placed` is `board()`'s answer), its
    dependencies split into the ones that name a node and the ones that do not, its dependents,
    and its level: 0 for a node with no dependency, else one more than the highest level of its
    dependencies; no level for a node on a cycle, behind one, or with a dependency that names
    no node (the same nodes the frontier cannot run). Writes nothing."""
    where: dict[str, str] = {}
    for bucket in ("running", "ready", "queued", "blocked", "contingent"):
        for row in placed[bucket]:
            where[row["node_id"]] = bucket
    for n in state.nodes:
        if n.lifecycle in (Lifecycle.MERGED, Lifecycle.ABANDONED):
            where[n.id] = n.lifecycle.value.lower()
    ids = [n.id for n in state.nodes]
    known = set(ids)
    needs = {n.id: list(dict.fromkeys(d for d in n.dependencies if d in known)) for n in state.nodes}
    unknown = {n.id: list(dict.fromkeys(d for d in n.dependencies if d not in known))
               for n in state.nodes}
    dependents: dict[str, list[str]] = {i: [] for i in ids}
    for i in ids:
        for d in needs[i]:
            dependents[d].append(i)
    pending = {i: len(needs[i]) + len(unknown[i]) for i in ids}
    level: dict[str, int | None] = {i: None for i in ids}
    wave = [i for i in ids if pending[i] == 0]
    depth = 0
    while wave:
        nxt: list[str] = []
        for i in wave:
            level[i] = depth
            for j in dependents[i]:
                pending[j] -= 1
                if pending[j] == 0:
                    nxt.append(j)
        wave = nxt
        depth += 1
    cycles = _cycles(ids, needs)
    on_cycle = {i for c in cycles for i in c}
    rows: list[dict] = []
    for n in state.nodes:
        if level[n.id] is not None:
            shape = "ok"
        elif n.id in on_cycle:
            shape = "cycle"
        elif unknown[n.id]:
            shape = "unknown"
        else:
            shape = "behind"
        rows.append({"node_id": n.id, "place": where[n.id], "level": level[n.id], "shape": shape,
                     "depends_on": needs[n.id], "unknown": unknown[n.id],
                     "dependents": dependents[n.id]})
    return {"totals": {"nodes": len(rows), "edges": sum(len(v) for v in needs.values()),
                       "unknown": sum(len(v) for v in unknown.values()),
                       "cycles": len(cycles), "unplaced": sum(1 for r in rows if r["shape"] != "ok")},
            "nodes": rows, "cycles": cycles}


def _stamped(at: str) -> bool:
    """`YYYY-MM-DDTHH:MM:SSZ` with ASCII digits: the form `contracts.utcnow` writes."""
    return (len(at) == 20 and at.isascii() and at[4] == at[7] == "-" and at[10] == "T"
            and at[13] == at[16] == ":" and at[19] == "Z"
            and all(c.isdigit() for k, c in enumerate(at) if k not in (4, 7, 10, 13, 16, 19)))


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def timeline(state: ProjectState) -> dict[str, Any]:
    """The dated records of the project file in one list, oldest first (see L7). Records
    without a stamp of the product's form come first, are all listed and are counted as
    `undated`: the cap never cuts one. Of the dated records at most `TIMELINE_ROWS` are listed,
    the newest; the others are counted as `omitted`."""
    entries: list[dict] = []
    for index, ev in enumerate(state.activity_events):
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        entries.append({"at": ev.recorded_at, "source": "dispatch", "index": index,
                        "what": ev.kind.value, "node_id": _as_text(payload.get("node")),
                        "detail": f"sequence={ev.sequence}" + "".join(
                            f"  {key}={payload[key]}" for key in ("reason", "failure")
                            if _as_text(payload.get(key)))})
    for index, dec in enumerate(state.decisions):
        entries.append({"at": dec.at, "source": "decision", "index": index,
                        "what": dec.kind.value, "node_id": _as_text(dec.payload.get("node")),
                        "detail": f"{dec.actor}: {dec.reason}"})
    for index, act in enumerate(state.external_actions):
        entries.append({"at": act.at, "source": "external", "index": index,
                        "what": act.action_class, "node_id": act.node,
                        "detail": f"{act.actor}: {act.reason}"})
    for index, gate in enumerate(state.gates):
        entries.append({"at": gate.at, "source": "gate", "index": index,
                        "what": gate.outcome.value, "node_id": None,
                        "detail": f"{gate.name}  generation={gate.generation}  "
                                  f"subject={gate.subject}"})
    for entry in entries:
        entry["dated"] = _stamped(entry["at"])
    undated = [e for e in entries if not e["dated"]]
    dated = sorted((e for e in entries if e["dated"]), key=lambda e: e["at"])
    shown = undated + dated[-TIMELINE_ROWS:]              # the cap is on the dated records only
    counts = {"dispatch": len(state.activity_events), "decision": len(state.decisions),
              "external": len(state.external_actions), "gate": len(state.gates)}
    return {"totals": {"events": len(entries), **counts, "undated": len(undated),
                       "shown": len(shown), "omitted": len(entries) - len(shown)},
            "events": shown}


def _evidence_of(base: str, run_id: str) -> dict[str, Any] | None:
    """The counts of the evidence bundle of run `run_id` by status, `None` when the run
    directory exists and holds no `evidence.json`, or an `_Unfit` that says why it cannot be
    read: an id that is not a run directory's name, a run directory that is absent or a link, a
    bundle that is a link, not a regular file, larger than `MAX_EVIDENCE_BYTES`, not UTF-8, not
    an evidence bundle, or the bundle of another run."""
    run_dir = RunStore(base, run_id).dir
    try:
        kind = os.lstat(run_dir).st_mode
    except FileNotFoundError:
        raise _Unfit("the run directory does not exist: its evidence cannot be read") from None
    if stat.S_ISLNK(kind):
        raise _Unfit("the run directory is a symbolic link (not followed)")
    if not stat.S_ISDIR(kind):
        raise _Unfit("the run directory is not a directory")
    try:
        fd = _open_regular(os.path.join(run_dir, "evidence.json"), MAX_EVIDENCE_BYTES)
    except FileNotFoundError:
        return None
    except OSError as exc:
        why = ("is a symbolic link (not followed)" if exc.errno == errno.ELOOP
               else f"cannot be opened: {exc.strerror or type(exc).__name__}")
        raise _Unfit(f"evidence.json {why}") from None
    except _Unfit as exc:
        raise _Unfit(f"evidence.json {exc}") from None
    try:
        raw = _read_all(fd, MAX_EVIDENCE_BYTES)
    except OSError as exc:
        raise _Unfit(f"evidence.json cannot be read: {exc.strerror or type(exc).__name__}") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise _Unfit(f"evidence.json is larger than {MAX_EVIDENCE_BYTES} bytes (not read)")
    try:
        bundle = EvidenceBundle.model_validate_json(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:       # UnicodeDecodeError is a ValueError
        raise _Unfit(f"evidence.json is not an evidence bundle: {exc}") from None
    if bundle.run_id != run_id:
        raise _Unfit(f"evidence.json says it is the bundle of run "
                     f"{_plain(bundle.run_id, NAME_WIDTH)}: not read as this one")
    counts = {"VERIFIED": 0, "GAP": 0, "REGRESSION": 0, "INSUFFICIENT": 0}
    for item in bundle.items:
        counts[item.status.value] += 1
    worst = next((w for w in ("REGRESSION", "GAP", "INSUFFICIENT") if counts[w]), None)
    return {"items": len(bundle.items), "verified": counts["VERIFIED"], "gap": counts["GAP"],
            "regression": counts["REGRESSION"], "insufficient": counts["INSUFFICIENT"],
            "worst": worst or ("VERIFIED" if bundle.items else "EMPTY")}


def evidence_health(base: str, state: ProjectState) -> tuple[dict[str, Any], dict[str, str]]:
    """`(document body, unreadable)`: the gates of the latest closure pass, what closure says,
    and the evidence bundle of every run a node names, each distinct run read once. A bundle that
    cannot be read is never an empty one: it is listed under `unreadable` by run id."""
    now = state.latest_generation()
    latest = [g for g in state.gates if g.generation == now] or list(state.gates)
    last: dict[str, Any] = {}
    for g in latest:
        last[g.name] = g
    head = state.measurement_head
    gates: list[dict] = []
    for g in last.values():
        current = None if head is None else g.subject == head
        gates.append({"name": g.name, "outcome": g.outcome.value, "subject": g.subject,
                      "current": current, "stale": g.outcome.value == "GREEN" and current is False,
                      "generation": g.generation, "at": g.at, "detail": g.detail})
    nodes_of: dict[str, list[str]] = {}
    for n in state.nodes:
        if n.run_id is not None:
            nodes_of.setdefault(n.run_id, []).append(n.id)
    runs: list[dict] = []
    unreadable: dict[str, str] = {}
    for run_id, node_ids in nodes_of.items():
        try:
            counts = _evidence_of(base, run_id)
        except _Unfit as exc:
            unreadable[run_id] = _plain(exc, ERROR_LIMIT)
            continue
        except Exception as exc:                      # noqa: BLE001 - listed, never a crash
            unreadable[run_id] = _plain(f"{type(exc).__name__}: {exc}", ERROR_LIMIT)
            continue
        runs.append({"run_id": run_id, "nodes": node_ids,
                     "bundle": counts is not None,
                     **(counts or {"items": 0, "verified": 0, "gap": 0, "regression": 0,
                                   "insufficient": 0, "worst": "NONE"})})
    closure = {"generation": state.closure_generation, "measurement_head": head,
               "dag_terminal": state.dag_terminal(), "gates_green": state.gates_green(),
               "open_repairs": [n.id for n in state.nodes if n.repair_of and not n.settled],
               "rc_closed": state.rc_closed(), "retired_gates": state.retired_gates()}
    sums = {k: sum(r[k] for r in runs) for k in ("verified", "gap", "regression", "insufficient")}
    totals = {"gates": len(gates), "gates_green": sum(1 for g in gates if g["outcome"] == "GREEN"),
              "gates_stale": sum(1 for g in gates if g["stale"]),
              "runs": len(runs) + len(unreadable), "no_bundle": sum(1 for r in runs if not r["bundle"]),
              **sums, "nodes_without_run": sum(1 for n in state.nodes if n.run_id is None)}
    return {"totals": totals, "closure": closure, "gates": gates, "runs": runs}, unreadable


def project_view(root: str, project_id: str, view: str) -> dict[str, Any]:
    """The document of one project view (`PROJECT_VIEWS`) under `root`. Writes nothing; a
    `MissionRefused` when the project cannot be read or described."""
    if view not in PROJECT_VIEWS:
        raise MissionRefused(f"{view!r} is not a project view")
    base = _resolve_root(root)
    state = _read_project(base, project_id)
    if view in ("timeline", "evidence"):                  # the project file (and the bundles) only
        body, unreadable = (timeline(state), {}) if view == "timeline" else evidence_health(
            base, state)
        body["totals"]["unreadable"] = len(unreadable)
        return {"schema": SCHEMA, "view": view, "root": base, "project": project_id,
                "write_seq": state.write_seq, **body, "unreadable": unreadable}
    stops, unreadable = _routings(base, state)
    # P3-42: a block the double-down ended in a proposal or an escalation is a decision for a
    # person. First in a node's list, so the contract's own routing, when there is one, still
    # reads last; a dossier that cannot be read is shown as unreadable, never skipped.
    dd_stops, dd_unreadable, dd_rows = doubledown.decisions_for_project(base, state)
    for node_id, found in dd_stops.items():
        stops[node_id] = found + stops.get(node_id, [])
    for node_id, why in dd_unreadable.items():
        unreadable.setdefault(node_id, why)
    try:
        frontier = derive_frontier(state, stops=stops, unreadable=unreadable)
    except FrontierError as exc:
        raise _refuse(project_id, f"is not a graph with a frontier: {exc}") from None
    if view == "needs-you":
        body = needs_you(frontier)
    else:                                                 # `board`, and `map` over the same board
        body = board(state, frontier)
        if view == "map":
            body = mission_map(state, body)
    if view == "needs-you" and dd_rows:
        body["double_down"] = dd_rows
    body["totals"]["unreadable"] = len(unreadable)
    return {"schema": SCHEMA, "view": view, "root": base, "project": project_id,
            "write_seq": state.write_seq, **body, "unreadable": unreadable}


def render_json(doc: dict[str, Any]) -> str:
    """ASCII JSON: a name that is not UTF-8 is written as an escape, never fails the write."""
    return json.dumps(doc, indent=2, ensure_ascii=True)


def _ids(ids: list[str]) -> str:
    return _plain(",".join(ids), TEXT_REASON_WIDTH) if ids else "-"


def _render_board(doc: dict[str, Any]) -> str:
    t = doc["totals"]
    lines = [f"mission board  project {_plain(doc['project'], NAME_WIDTH)}  "
             f"root {_plain(doc['root'])}",
             "totals: " + "  ".join(f"{k}={t[k]}" for k in (
                 "nodes", "running", "ready", "queued", "blocked", "contingent", "merged",
                 "abandoned", "unreadable"))]
    for row in doc["running"]:
        run_id = _plain(row["run_id"], NAME_WIDTH) if row["run_id"] else "-"
        lines.append(f"RUNNING  {_plain(row['node_id'], NAME_WIDTH)}  run={run_id}")
    for row in doc["ready"]:
        lines.append(f"READY  {_plain(row['node_id'], NAME_WIDTH)}")
    for row in doc["queued"]:
        lines.append(f"QUEUED  {_plain(row['node_id'], NAME_WIDTH)}  "
                     f"waiting_on={_ids(row['waiting_on'])}  held_by={_ids(row['held_by'])}"
                     + ("  UNRUNNABLE" if row["unrunnable"] else ""))
    for row in doc["blocked"]:
        lines.append(f"BLOCKED  {_plain(row['node_id'], NAME_WIDTH)}  waits_for={row['waits_for']}"
                     + (f"  {_plain(row['kind'], KIND_WIDTH)}" if row["kind"] else "")
                     + (f"  {_plain(row['handling'], KIND_WIDTH)}" if row["handling"] else "")
                     + (f"  retry_after={_plain(row['retry_after'], KIND_WIDTH)}"
                        if row["retry_after"] else ""))
    for row in doc["contingent"]:
        lines.append(f"CONTINGENT  {_plain(row['node_id'], NAME_WIDTH)}")
    for node_id, why in doc["unreadable"].items():
        lines.append(f"UNREADABLE  {_plain(node_id, NAME_WIDTH)}  {_plain(why, TEXT_REASON_WIDTH)}")
    return "\n".join(lines)


def _render_needs_you(doc: dict[str, Any]) -> str:
    t = doc["totals"]
    lines = [f"mission needs-you  project {_plain(doc['project'], NAME_WIDTH)}  "
             f"root {_plain(doc['root'])}",
             f"open: {t['open']}  ended: {t['ended']}  unreadable: {t['unreadable']}"]
    for row in doc["decisions"]:
        lines.append(f"{row['rank']}  {_plain(row['node_id'], NAME_WIDTH)}  "
                     f"{_plain(row['handling'], KIND_WIDTH)}  {_plain(row['kind'], KIND_WIDTH)}  "
                     f"frees={_ids(row['frees'])}  holds={_ids(row['holds'])}")
        lines.append(f"   decision: {_plain(row['decision'], TEXT_REASON_WIDTH) or '-'}")
        lines.append(f"   why: {_plain(row['why'], TEXT_REASON_WIDTH)}")
    for row in doc["ended"]:
        lines.append(f"ENDED  {_plain(row['node_id'], NAME_WIDTH)}  "
                     f"{_plain(row['handling'], KIND_WIDTH)}  {_plain(row['kind'], KIND_WIDTH)}  "
                     f"holds={_ids(row['holds'])}")
        lines.append(f"   why: {_plain(row['why'], TEXT_REASON_WIDTH)}")
    for row in doc.get("double_down", ()):                # P3-42: only when a dossier ended
        lines.append(f"DOUBLE-DOWN  {_plain(row['target'], NAME_WIDTH)}  "
                     f"{_plain(row['tier'], KIND_WIDTH)}  {_plain(row['outcome'], KIND_WIDTH)}  "
                     f"{_plain(row['summary'], TEXT_REASON_WIDTH)}")
    for node_id, why in doc["unreadable"].items():
        lines.append(f"UNREADABLE  {_plain(node_id, NAME_WIDTH)}  {_plain(why, TEXT_REASON_WIDTH)}")
    return "\n".join(lines)


def _render_map(doc: dict[str, Any]) -> str:
    t = doc["totals"]
    lines = [f"mission map  project {_plain(doc['project'], NAME_WIDTH)}  "
             f"root {_plain(doc['root'])}",
             "totals: " + "  ".join(f"{k}={t[k]}" for k in (
                 "nodes", "edges", "unknown", "cycles", "unplaced", "unreadable"))]
    placed = [r for r in doc["nodes"] if r["level"] is not None]
    for row in sorted(placed, key=lambda r: r["level"]):          # stable: graph order in a level
        lines.append(f"L{row['level']}  {_plain(row['node_id'], NAME_WIDTH)}  "
                     f"{row['place'].upper()}  needs={_ids(row['depends_on'])}")
    for row in doc["nodes"]:
        if row["level"] is None:
            lines.append(f"UNPLACED  {_plain(row['node_id'], NAME_WIDTH)}  "
                         f"{row['place'].upper()}  shape={row['shape']}  "
                         f"needs={_ids(row['depends_on'])}  unknown={_ids(row['unknown'])}")
    for members in doc["cycles"]:
        lines.append(f"CYCLE  {_ids(members)}")
    for node_id, why in doc["unreadable"].items():
        lines.append(f"UNREADABLE  {_plain(node_id, NAME_WIDTH)}  {_plain(why, TEXT_REASON_WIDTH)}")
    return "\n".join(lines)


def _render_timeline(doc: dict[str, Any]) -> str:
    t = doc["totals"]
    lines = [f"mission timeline  project {_plain(doc['project'], NAME_WIDTH)}  "
             f"root {_plain(doc['root'])}",
             f"events: {t['events']}  shown: {t['shown']}  omitted: {t['omitted']}  "
             f"undated: {t['undated']}"]
    for row in doc["events"]:
        node = f"  node={_plain(row['node_id'], NAME_WIDTH)}" if row["node_id"] else ""
        lines.append(f"{_plain(row['at'], KIND_WIDTH)}  {row['source'].upper()}  "
                     f"{_plain(row['what'], KIND_WIDTH)}{node}  "
                     f"{_plain(row['detail'], TEXT_REASON_WIDTH)}"
                     + ("" if row["dated"] else "  UNDATED"))
    return "\n".join(lines)


def _render_evidence(doc: dict[str, Any]) -> str:
    t, c = doc["totals"], doc["closure"]
    lines = [f"mission evidence  project {_plain(doc['project'], NAME_WIDTH)}  "
             f"root {_plain(doc['root'])}",
             "totals: " + "  ".join(f"{k}={t[k]}" for k in (
                 "gates", "gates_green", "gates_stale", "runs", "no_bundle", "verified", "gap",
                 "regression", "insufficient", "nodes_without_run", "unreadable")),
             f"closure: generation={c['generation']}  "
             f"measured_at={_plain(c['measurement_head'] or '-', NAME_WIDTH)}  "
             f"dag_terminal={str(c['dag_terminal']).lower()}  "
             f"gates_green={str(c['gates_green']).lower()}  "
             f"rc_closed={str(c['rc_closed']).lower()}  open_repairs={_ids(c['open_repairs'])}  "
             f"retired_gates={_ids(c['retired_gates'])}"]
    for g in doc["gates"]:
        flag = {True: "current", False: "NOT_CURRENT", None: "no_head"}[g["current"]]
        lines.append(f"GATE  {_plain(g['name'], NAME_WIDTH)}  {g['outcome']}  {flag}"
                     + ("  STALE" if g["stale"] else "")
                     + f"  generation={g['generation']}  subject={_plain(g['subject'], NAME_WIDTH)}")
    for r in doc["runs"]:
        lines.append(f"RUN  {_plain(r['run_id'], NAME_WIDTH)}  {r['worst']}  "
                     f"items={r['items']}  verified={r['verified']}  gap={r['gap']}  "
                     f"regression={r['regression']}  insufficient={r['insufficient']}  "
                     f"nodes={_ids(r['nodes'])}")
    for run_id, why in doc["unreadable"].items():
        lines.append(f"UNREADABLE  {_plain(run_id, NAME_WIDTH)}  {_plain(why, TEXT_REASON_WIDTH)}")
    return "\n".join(lines)


def render_text(doc: dict[str, Any]) -> str:
    if doc["view"] == "board":
        return _render_board(doc)
    if doc["view"] == "needs-you":
        return _render_needs_you(doc)
    if doc["view"] == "map":
        return _render_map(doc)
    if doc["view"] == "timeline":
        return _render_timeline(doc)
    if doc["view"] == "evidence":
        return _render_evidence(doc)
    t = doc["totals"]
    lines = [f"mission overview  root {_plain(doc['root'])}",
             f"runs: {t['runs']} ({t['readable']} readable, {t['unreadable']} unreadable)"
             + "".join(f"  {phase}={n}" for phase, n in t["by_phase"].items())]
    for row in doc["runs"]:
        name = _plain(row["run_id"], NAME_WIDTH)
        if not row["readable"]:
            lines.append(f"{name}  UNREADABLE  {_plain(row['error'], TEXT_REASON_WIDTH)}")
            continue
        extra = row["blocked_reason"] or row["stop_reason"] or ""
        lines.append(f"{name}  {row['phase']}  {row['stage']}/{row['condition']}  "
                     f"it={row['iteration']}  process={row['process']}"
                     + (f"  {_plain(row['blocked_kind'], KIND_WIDTH)}" if row["blocked_kind"] else "")
                     + (f"  {_plain(extra, TEXT_REASON_WIDTH)}" if extra else ""))
    return "\n".join(lines)


def run(view: str, root: str, *, project: str | None = None, as_json: bool = False,
        out: TextIO | None = None, err: TextIO | None = None) -> int:
    """The command: prints the view and returns the exit code (0, 1 or 2). `project` names the
    project of the project views (`PROJECT_VIEWS`) and is refused for the overview."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if out is None:                                    # `>&-`: there is nowhere to answer
        print("mission refused: stdout is closed, the answer could not be written", file=err)
        return REFUSED
    if view not in VIEWS:
        print(f"mission refused: unknown view {view!r}", file=err)
        return REFUSED
    if view in PROJECT_VIEWS and project is None:
        print(f"mission refused: the {view} view needs a project (--project ID)", file=err)
        return REFUSED
    if view not in PROJECT_VIEWS and project is not None:
        print(f"mission refused: the {view} view takes no project", file=err)
        return REFUSED
    try:
        doc = overview(root) if project is None else project_view(root, project, view)
    except MissionRefused as exc:
        print(f"mission refused: {exc}", file=err)
        return REFUSED
    text = render_json(doc) if as_json else render_text(doc)
    try:
        out.write(text + "\n")
        out.flush()
    except (OSError, ValueError) as exc:               # a closed or full stdout: nothing was answered
        print(f"mission failed: the answer could not be written: {type(exc).__name__}: {exc}",
              file=err)
        return REFUSED
    return SOME_UNREADABLE if doc["totals"]["unreadable"] else OK
