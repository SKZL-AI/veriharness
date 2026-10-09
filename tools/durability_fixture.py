"""The role run, replaced -- and nothing else.

`tools/durability_e2e.py` proves what the control plane does under faults. The
one thing it must not need is an agent writing code: that is the role runs'
job, proven by P1-16. So this module replaces exactly that and inherits
everything else from the shipped `hoh.launcher.HohRunLauncher`:

* `evaluate` -- reading a run's state back after a crash, the live-lock check
  that says a run is still IN_FLIGHT, the baseline comparison that makes a
  resume exactly-once: the product's;
* `merge` -- the accepted-identity check and the "already contained" guard
  that keeps a merge from landing twice: the product's;
* `accepted_baseline`, `action_class`, `depends_on`: the product's.

Only `launch` (and `prepare`, which would create a Herdr worktree) is
overridden. `launch` runs the stand-in role run in a **child process started
the way `HohRunLauncher._run_cli` starts `hoh run`** -- `subprocess.run`, no
session of its own. Like `hoh run`, that child holds the run's process marker
(`RunStore.process_marker`) for its whole life and the run lock per iteration
only, with two iterations and the lock free in between; it writes a real
`RunState`: a candidate commit on branch `hoh-<node>` recorded as
`last_accepted_candidate`, or a quota block recorded by `hoh.stages.block`
itself. The verdict is then the product's own reading of that state. A master
killed while the child works leaves the child running -- the real launcher's
shape (finding #9).

Fault injection lives here, never in the product. A named *pause point*
listed in ``HOH_DURABILITY_PAUSE`` makes the process write
``<marks>/<point>.reached`` (with its pid) and wait until
``<marks>/<point>.release`` exists: ``pre-launch:<n>``, ``in-run:<n>`` (inside
the child, holding the run lock), ``between-iterations:<n>`` (inside the child,
lock free, process alive), ``post-launch:<n>``, ``pre-merge:<n>``,
``post-merge:<n>``, ``pre-gate``. A node listed in
``HOH_DURABILITY_PROVIDER_BLOCK`` as ``node=seconds`` is quota-blocked on its
first run for that many seconds.

Registered for a project by writing its ``driver.json``::

    {"factory": "durability_fixture:build", "path": "<repo>/tools"}
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __name__ == "__main__":                       # the child: find the package first
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hoh.contracts import Candidate, RunState, Stage  # noqa: E402
from hoh.launcher import HohRunLauncher  # noqa: E402
from hoh.orchestrator import GateRunner, MergeResult, RunOutcome  # noqa: E402
from hoh.project import GateOutcome, GateResult  # noqa: E402
from hoh.store import RunStore  # noqa: E402

PAUSE_ENV = "HOH_DURABILITY_PAUSE"
MARKS_ENV = "HOH_DURABILITY_MARKS"
BLOCK_ENV = "HOH_DURABILITY_PROVIDER_BLOCK"


def _git(repo: Path, *args: str, env: dict | None = None,
         input_: str | None = None) -> subprocess.CompletedProcess:
    # Real dates: two launches of one node must be two commits, or a duplicate
    # launch would be invisible (review B, 3).
    base = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, input=input_, check=False,
                          env={**os.environ, **base, **(env or {})})


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _self_start() -> str:
    """This process's start time in clock ticks since boot (field 22 of
    /proc/self/stat), or "" when it cannot be read. Written into the pause mark
    so the harness signals the pid later only if the same process still holds it."""
    try:
        stat = Path("/proc/self/stat").read_text()
    except OSError:
        return ""
    fields = stat[stat.rfind(")") + 2:].split()
    return fields[19] if len(fields) > 19 else ""


def pause(point: str) -> None:
    """Stop at a named point if the scenario asked for it; see the module doc."""
    wanted = {p.strip() for p in os.environ.get(PAUSE_ENV, "").split(",") if p.strip()}
    marks = os.environ.get(MARKS_ENV)
    if point not in wanted or not marks:
        return
    d = Path(marks)
    reached = d / f"{point.replace(':', '__')}.reached"
    release = d / f"{point.replace(':', '__')}.release"
    try:
        with open(reached, "x", encoding="utf-8") as fh:
            json.dump({"point": point, "pid": os.getpid(), "start": _self_start(),
                       "at": utc()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        return                                  # fired once already: pass through
    while not release.exists():
        time.sleep(0.05)


def expected_content(node_id: str) -> str:
    return f"{node_id}\n"


def _blocks() -> dict[str, int]:
    out = {}
    for item in os.environ.get(BLOCK_ENV, "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = int(v)
    return out


def _log(records: Path, node: str, result: str, sha: str = "") -> None:
    """Append-only: every role run leaves a line, so derive can count them."""
    with open(records / "launches.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"node": node, "result": result, "sha": sha,
                             "pid": os.getpid(), "at": utc()}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------- #
# The stand-in role run (child process)
# --------------------------------------------------------------------------- #

def role_run(*, root: Path, run_id: str, repo: Path, node: str, records: Path) -> int:
    """What `hoh run` would do for one dispatch, minus the agent: under the
    run's lock, either record a quota block or produce and record a candidate."""
    store = RunStore(root, run_id)
    # Like `hoh run`: a process marker for the whole run, and the run lock
    # held per iteration only -- so between the two iterations below the run
    # is unlocked while its process still works (review B3, C).
    with store.process_marker():
        with store.lock():
            pause(f"in-run:{node}")
            state = store.read_state()
            state.note(f"fixture iteration 1 of {node}")
            store.write_state(state)
        pause(f"between-iterations:{node}")
        return _second_iteration(store, repo=repo, node=node, records=records)


def _second_iteration(store: RunStore, *, repo: Path, node: str, records: Path) -> int:
    from hoh import stages

    with store.lock():
        state = store.read_state()
        blocked = records / f"{node}.provider-blocked"
        delay = _blocks().get(node)
        if delay is not None and not blocked.exists():
            due = datetime.now(UTC) + timedelta(seconds=delay)
            blocked.write_text(json.dumps({"retry_after": due.isoformat()}))
        if blocked.exists():
            due = datetime.fromisoformat(json.loads(blocked.read_text())["retry_after"])
            if due > datetime.now(UTC):
                # Recorded by the product's own quota path; the fixture only
                # supplies the reset time the provider would have named.
                stages.block(state, "usage limit reached (fixture)")
                state.retry_after = due.isoformat()
                store.write_state(state)
                _log(records, node, "provider_blocked")
                return 0
        base = _git(repo, "rev-parse", "main").stdout.strip()
        blob = _git(repo, "hash-object", "-w", "--stdin",
                    input_=expected_content(node)).stdout.strip()
        env = {"GIT_INDEX_FILE": str(records / f"{node}.index")}
        _git(repo, "read-tree", base, env=env)
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{node}.txt",
             env=env)
        tree = _git(repo, "write-tree", env=env).stdout.strip()
        sha = _git(repo, "commit-tree", tree, "-p", base, "-m",
                   f"candidate for {node} (role run pid {os.getpid()}, "
                   f"{time.time_ns()})").stdout.strip()
        _git(repo, "update-ref", f"refs/heads/hoh-{node}", sha)
        if state.condition.value == "BLOCKED":
            stages.unblock(state, "fixture: the quota came back")
        state.stage = Stage.CHECKPOINTED
        state.last_accepted_candidate = Candidate(
            candidate_id=f"{node}-{time.time_ns()}", repo_path=str(repo), commit=sha,
            tree_clean=True, tree_digest=tree)
        store.write_state(state)
        _log(records, node, "accepted", sha)
    return 0


# --------------------------------------------------------------------------- #
# The launcher: the product's, with the agent replaced
# --------------------------------------------------------------------------- #

class FixtureLauncher(HohRunLauncher):
    def __init__(self, store) -> None:
        state = store.read_state()
        # Run states live beside the project, not under the HoH root, so the
        # run-level resume pass never mistakes them for real runs.
        super().__init__(store.dir / "fixture-runs", state.repo_path, mainline="main")
        self.store = store
        self.records = store.dir / "fixture"
        self.records.mkdir(exist_ok=True)

    def prepare(self, node):
        return None                     # no Herdr worktree: the child writes the branch

    def _ensure_run(self, node) -> RunStore:
        store = RunStore(self.root, node.run_id or node.id)
        if not store.state_exists():
            store.write_state(RunState(
                run_id=node.run_id or node.id, repo_path=str(self.repo_path),
                project_name=self.store.project_id, spec_path="(fixture)",
                spec_digest="0" * 16, policy_digest="0" * 16, profile_digest="0" * 16))
        return store

    def launch(self, node) -> RunOutcome:
        pause(f"pre-launch:{node.id}")
        store = self._ensure_run(node)
        # Started the way `_run_cli` starts `hoh run`: no session of its own.
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "role-run",
             "--root", str(self.root), "--run-id", store.run_id,
             "--repo", str(self.repo_path), "--node", node.id,
             "--records", str(self.records)],
            check=False, env=os.environ.copy())
        outcome = self._verdict_from_baseline(store.read_state(), node.accepted_before)
        pause(f"post-launch:{node.id}")
        return outcome

    def merge(self, node, outcome) -> MergeResult:
        pause(f"pre-merge:{node.id}")
        result = super().merge(node, outcome)
        if result.landed and result.detail != "already contained in the mainline":
            pause(f"post-merge:{node.id}")
        return result


class FixtureGates(GateRunner):
    def __init__(self, store) -> None:
        self.store = store
        self.repo = Path(store.read_state().repo_path)

    def subject(self) -> str:
        return _git(self.repo, "rev-parse", "main").stdout.strip()[:12]

    def run(self, subject: str) -> list[GateResult]:
        pause("pre-gate")
        state = self.store.read_state()
        missing = [n.id for n in state.nodes
                   if _git(self.repo, "show", f"main:{n.id}.txt").stdout
                   != expected_content(n.id)]
        return [GateResult(
            name="every-node-landed", subject=subject,
            outcome=GateOutcome.RED if missing else GateOutcome.GREEN,
            exit_code=1 if missing else 0,
            detail=("missing: " + ", ".join(missing)) if missing else "all nodes on main")]


def build(store):
    return FixtureLauncher(store), FixtureGates(store)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rr = sub.add_parser("role-run")
    for flag in ("--root", "--run-id", "--repo", "--node", "--records"):
        rr.add_argument(flag, required=True)
    a = ap.parse_args()
    raise SystemExit(role_run(root=Path(a.root), run_id=a.run_id, repo=Path(a.repo),
                              node=a.node, records=Path(a.records)))
