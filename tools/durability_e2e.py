#!/usr/bin/env python3
"""durability_e2e.py -- the P2 exit conditions, as a fault test anyone can re-run.

The plan's P2 gate says:

    Exit includes:  unattended test: no human attached; controller/session
                    replacement; automatic continuation; no lost state

and its fault-test list (`specs/architecture_r1/07_ACCEPTANCE_AND_FAULT_TEST_PLAN_R1.md`)
already names the scenarios: DS1 controller kill/restart, DS2 reboot recovery,
DS3 usage limit and retry-at, DS4 terminal project disarms timer, DS5 master
rollover, DS6 stale master cannot write, AC7 old session epoch cannot issue an
authoritative command. This tool runs them against the shipped control plane.

What is real: separate OS processes for every master (`hoh project drive`, and
ticks built from the `ExecStart` of `ops/hoh-continue.service`), started with no
stdin in a session of their own -- measured from /proc after each start, not
asserted; SIGKILL and SIGSTOP delivered from outside at named points, including
the window after a git merge and before the state records it; the shipped
ProjectController, ProjectStore, epoch chain, leader lease, durable timers and
resume loop; real merges into a real git repository.

What is a fixture, and the record says so: the role runs. Each node's candidate
is produced by `tools/durability_fixture.py` with git plumbing instead of an
agent, because the question here is what the *control plane* does when its
controller dies, rolls over, goes stale or waits on a quota. Agent execution is
proven separately (P1-16). The fixture inherits `evaluate` and `merge` from the
shipped `HohRunLauncher`, and its stand-in role run is a child process started
the way the real one is -- no session of its own, a process marker for its whole
life, the run lock per iteration -- so a killed master leaves it running, exactly
as with real agents (scenario `orphaned-run`).

What the harness itself does, and the record lists per scenario under
`harness_initiated`, in step order: every process it started -- the very first
`project drive` included (starting a programme is a person's act) -- and the
rollover's handoff (issued with the master's own token); the tick loop, every
tick start listed -- an in-harness loop standing in for the systemd timer, which
runs `python -c 'from hoh.cli import main'` with this tree's `src` on the path
and the unit's arguments plus `--root`; every SIGKILL it sent, cleanup kills of
masters and of their children included; every crash injected into a started
process; every signal recorded as a step (SIGSTOP/SIGCONT of a frozen master
among them); the release of pause points, branch moves and fixture edits. The
quota-crash record concatenates its three cases' lists in case order, each
entry prefixed with its case. Two harness acts are not recorded as steps and
therefore not listed: the SIGCONT sent inside `capture()` and the
negative-control `sitecustomize` injection. Neither systemd nor
`plugin/bin/hoh` is used, and the operator's installed units are not touched.

What is NOT run by default: DS2. A reboot cannot be simulated honestly -- a boot
id is not something a test may fake without teaching the lease to accept a
faked one -- so `reboot-arm` / `reboot-verify` exist for an operator to run
around a real restart. Until that evidence exists the record says NOT_RUN.

Every scenario whose guarantee a known-bad build could break is run again under
negative controls (`sitecustomize` patches in a scratch directory, never in the
product). `derive` requires every declared control to have run and to fail at
least the checks it targets (a control may break more -- a stalled scenario
fails widely -- but never fewer, and only while the scenario's preconditions
held, unless the control exists to make it stall); otherwise nothing is
satisfied. Product checks that no caught control *targets* are listed per
scenario and per requirement as measured-but-undiscriminated.

    python3 tools/durability_e2e.py run           --evidence DIR [--base SCRATCH]
    python3 tools/durability_e2e.py derive        --evidence DIR --out RECORD [--check]
    python3 tools/durability_e2e.py reboot-arm    --evidence DIR [--base SCRATCH]
    python3 tools/durability_e2e.py reboot-verify --evidence DIR

`derive --check` exits 0 only when RECORD equals what the evidence derives to.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent
sys.path.insert(0, str(HOH / "src"))

RECORD_VERSION = 6
PROJECT = "dur"
CONTINUATION_UNIT = HOH / "ops" / "hoh-continue.service"

#: The tree whose `src`, `tools` (the fixture) and `ops` (the continuation unit)
#: the scenario processes run. By default a `git archive` of HEAD, exported
#: before the first scenario: the evidence is then about exactly the committed
#: code, and an edit to the working tree during the run cannot leak into it
#: (review B4). `--live` uses the working tree instead, for development runs;
#: such evidence never satisfies a requirement.
CODE = HOH
#: The commit exported to CODE, resolved once: the label names exactly the code
#: that ran, even if HEAD moves during the run (review B6, 3).
EXPORTED_HEAD: str | None = None

TICK_DESCRIPTION = (
    "in-harness loop standing in for the systemd timer: each tick is a fresh "
    "process `python -c 'from hoh.cli import main'` with <tree>/src on PYTHONPATH "
    "and the arguments of ops/hoh-continue.service ExecStart plus --root <scenario>; "
    "systemd and plugin/bin/hoh are not used")


def _continue_argv(unit: Path = CONTINUATION_UNIT) -> list[str]:
    """The `hoh` arguments of the canonical continuation unit's ExecStart."""
    for line in unit.read_text().splitlines():
        if line.startswith("ExecStart="):
            argv = shlex.split(line.split("=", 1)[1])
            assert argv[0].endswith("/hoh"), argv
            return argv[1:]
    raise RuntimeError(f"{CONTINUATION_UNIT} has no ExecStart")


CONTINUE_ARGV = _continue_argv()
SCENARIOS = ("controller-kill", "rollover", "lapse", "quota", "moved-branch",
             "quota-crash", "orphaned-run")

#: Where the first master of a quota-crash case is killed, relative to arming
#: the retry timer and persisting the parked node.
CRASH_POINTS = ("before-arm", "after-arm", "after-park")

#: Injected into the first master of a quota-crash case, never into the product.
CRASH_INJECTOR = """
import os, signal
import hoh.timers as T, hoh.projectstore as P
WHERE = os.environ.get("HOH_DURABILITY_CRASH", "")
_arm, _write = T.TimerStore.arm_new, P.ProjectStore.write_state
def _really_arm(self, **kw):
    return _arm(self, **kw)
def arm_new(self, **kw):
    if WHERE == "before-arm" and kw.get("node_id"):
        os.kill(os.getpid(), signal.SIGKILL)
    out = ARM(self, **kw)
    if WHERE == "after-arm" and kw.get("node_id"):
        os.kill(os.getpid(), signal.SIGKILL)
    return out
def write_state(self, state):
    _write(self, state)
    if WHERE == "after-park" and any(n.blocked_kind == "usage_limit" for n in state.nodes):
        os.kill(os.getpid(), signal.SIGKILL)
ARM = _really_arm
T.TimerStore.arm_new = arm_new
P.ProjectStore.write_state = write_state
"""

NO_FENCE = """
import hoh.succession as S, hoh.projectstore as P
S.MasterAuthority.problems = lambda self: []
def _stamp(self, state):
    try:
        state.master_epoch = P.ProjectStore(self.dir.parent.parent,
                                            self.dir.name).read_state().master_epoch
    except Exception:
        pass
    return state
S.MasterAuthority.stamp = _stamp
"""

#: Known-bad controls (IE4). Each breaks one guarantee inside every process of
#: one scenario and names the checks that must then fail. A control that runs
#: with a harness error has not shown anything and is not "caught".
CONTROLS = [
    {"name": "no-recover", "scenario": "controller-kill", "allow_stall": True,
     "targets": ["every_replacement_by_the_canonical_tick", "closed"],
     "patch": "import hoh.resumeloop as R\nR.recover_projects = lambda *a, **k: []\n"},
    {"name": "no-fence", "scenario": "rollover",
     "targets": ["ac7_old_epoch_unblock_refused", "ac7_old_epoch_handoff_refused",
                 "old_master_superseded"],
     "patch": NO_FENCE},
    {"name": "no-fence-no-writeseq", "scenario": "rollover",
     "targets": ["old_master_wrote_nothing"],
     "patch": NO_FENCE + """
_w = P.ProjectStore.write_state
def _write(self, state):
    try:
        cur = self.read_state()
        state.write_seq = max(state.write_seq, cur.write_seq)
    except Exception:
        pass
    return _w(self, state)
P.ProjectStore.write_state = _write
"""},
    {"name": "no-successor", "scenario": "rollover",
     "targets": ["successor_epoch_is_succession"],
     "patch": """
import hoh.succession as S
from hoh.store import _atomic_write
_h = S.MasterAuthority.handoff
def handoff(self, successor, *, reason):
    lease = _h(self, successor, reason=reason)
    lease = lease.model_copy(update={"successor": None})
    _atomic_write(self.lease_path, lease.model_dump_json(indent=2) + "\\n")
    return lease
S.MasterAuthority.handoff = handoff
"""},
    {"name": "no-fence", "scenario": "lapse",
     "targets": ["old_master_superseded", "old_master_wrote_nothing"], "patch": NO_FENCE},
    {"name": "all-classes", "scenario": "quota",
     "targets": ["other_class_not_resumed", "other_timer_still_armed"],
     "patch": """
import hoh.resumeloop as R
_rp, _rr = R.resume_projects, R.resume_runs
WIDE = frozenset({"usage_limit", "collected_for_digest", "parked_by_contract"})
R.resume_projects = lambda *a, **k: _rp(*a, **{**k, "classes": WIDE})
R.resume_runs = lambda *a, **k: _rr(*a, **{**k, "classes": WIDE})
"""},
    {"name": "no-disarm", "scenario": "quota",
     "targets": ["ds4_closure_disarmed_the_heartbeat"],
     "patch": "import hoh.timers as T\nT.TimerStore.disarm_all = lambda self, *a, **k: []\n"},
    {"name": "ignore-due", "scenario": "quota",
     "targets": ["not_resumed_before_due"],
     "patch": """
import hoh.resumeloop as R
_d = R.due_resumptions
R.due_resumptions = lambda timers, **k: _d(timers, **{**k, "now": "2099-01-01T00:00:00Z"})
"""},
    {"name": "any-generation", "scenario": "quota",
     "targets": ["stale_generation_refused"],
     "patch": """
import hoh.resumeloop as R
_d = R.due_resumptions
def due(timers, **k):
    e = k.get("program_epoch")
    return _d([t.model_copy(update={"program_epoch": e}) for t in timers], **k)
R.due_resumptions = due
"""},
    {"name": "no-identity-check", "scenario": "moved-branch",
     "targets": ["refused_as_candidate_mismatch", "unverified_commit_never_landed"],
     "patch": "import hoh.launcher as L\nL.accepted_identity_problem = lambda *a, **k: None\n"},
    {"name": "relaunch-instead-of-read", "scenario": "controller-kill",
     "targets": ["problems.exactly_once"],
     "patch": """
import hoh.launcher as L
from hoh.orchestrator import RunOutcome, RunVerdict
L.HohRunLauncher.evaluate = lambda self, node: RunOutcome(RunVerdict.NOT_STARTED, "patched")
"""},
    {"name": "human-decision", "scenario": "controller-kill",
     "targets": ["problems.no_human"],
     "patch": """
import hoh.orchestrator as O
_r = O.ProjectController._record
def _record(self, state, kind, reason, payload, evidence=None):
    _r(self, state, kind, reason, payload, evidence)
    state.decisions[-1] = state.decisions[-1].model_copy(update={"actor": "human"})
O.ProjectController._record = _record
"""},
    {"name": "no-park-versions", "scenario": "rollover",
     "targets": ["problems.continuity"],
     "patch": "import hoh.projectstore as P\nP.ProjectStore._park = lambda self, path: None\n"},
    {"name": "no-live-check", "scenario": "rollover",
     "targets": ["ac7_anonymous_unblock_refused_while_live"],
     "patch": "import hoh.cli as C\nC._command_authority = lambda args, store: (None, False, None)\n"},
    {"name": "no-expiry", "scenario": "lapse",
     "targets": ["recovery_after_lapse"],
     "patch": """
import hoh.succession as S
_h = S.MasterAuthority.holder_state
def holder_state(self, lease, now):
    state, why = _h(self, lease, now)
    return ("LIVE", why) if state == "LOST" and "expired" in why else (state, why)
S.MasterAuthority.holder_state = holder_state
"""},
    {"name": "no-recover", "scenario": "quota-crash", "allow_stall": True,
     "targets": ["before-arm:closed_by_ticks_alone"],
     "patch": CRASH_INJECTOR + "\nimport hoh.resumeloop as R\nR.recover_projects = lambda *a, **k: []\n"},
    {"name": "no-in-flight", "scenario": "orphaned-run",
     "targets": ["first_tick_waits_on_the_run"],
     "patch": """
import hoh.launcher as L
from hoh.orchestrator import RunOutcome, RunVerdict
_e = L.HohRunLauncher.evaluate
def evaluate(self, node):
    out = _e(self, node)
    return RunOutcome(RunVerdict.UNDETERMINED, out.detail) if out.verdict is RunVerdict.IN_FLIGHT else out
L.HohRunLauncher.evaluate = evaluate
"""},
    {"name": "no-process-marker", "scenario": "orphaned-run",
     "targets": ["first_tick_waits_on_the_run"],
     "patch": "import hoh.store as S\nS.RunStore.running_process = lambda self: None\n"},
    {"name": "timer-never-armed", "scenario": "quota-crash",
     "targets": ["after-park:invariant_parked_implies_armed_timer"],
     "patch": CRASH_INJECTOR + "\nARM = lambda self, **kw: None\n"},
    # The marker still claims a running run and the lock is still held (so the
    # tick still waits), but it names another process -- the parent, i.e. the
    # killed master -- instead of the process executing the run.
    {"name": "marker-names-other-pid", "scenario": "orphaned-run",
     "targets": ["marker_names_the_orphaned_child"],
     "patch": """
import contextlib, json, os
import hoh.store as S
_pm = S.RunStore.process_marker
@contextlib.contextmanager
def process_marker(self):
    with _pm(self):
        marker = json.loads(self.process_path.read_text(encoding="utf-8"))
        marker["pid"] = os.getppid()
        S._atomic_write(self.process_path, json.dumps(marker) + "\\n")
        yield
S.RunStore.process_marker = process_marker
"""},
]


#: The files whose behaviour the durability evidence is about. Evidence taken
#: at a commit is stale for any later commit that changes one of them.
RELEVANT_PATHS = ("src/hoh", "ops/hoh-continue.service",
                  "tools/durability_e2e.py", "tools/durability_fixture.py")


def stale_paths(evidence_head: str | None, repo: Path = HOH) -> list[str] | None:
    """Relevant files changed between the evidence's commit and HEAD, or None
    when that cannot be told (unknown commit): never read as fresh."""
    if not evidence_head:
        return None
    # Against the working tree, not HEAD: an uncommitted edit made after the
    # evidence is as much a change as a committed one (review B2). Untracked
    # files under the relevant paths count too.
    p = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", evidence_head,
                        "--", *RELEVANT_PATHS], capture_output=True, text=True)
    u = subprocess.run(["git", "-C", str(repo), "ls-files", "--others",
                        "--exclude-standard", "--", *RELEVANT_PATHS],
                       capture_output=True, text=True)
    if p.returncode != 0 or u.returncode != 0:
        return None
    return sorted({z for z in (p.stdout + u.stdout).splitlines() if z.strip()})


assert all(c["targets"] for c in CONTROLS), "a control must name what it breaks"

#: Checks about what the harness did, not about the product: they must hold for
#: a scenario to mean anything, and they are reported apart from the product's
#: guarantees (review B3, B). A control that breaks one counts as caught only if
#: it declares `allow_stall` -- it exists to make the scenario not happen.
PRECONDITIONS = {
    "only_the_first_master_started_by_hand", "no_manual_resume", "branch_really_moved",
    "old_master_was_frozen", "master_killed_while_child_ran",
    "handoff_by_current_master_with_its_token", "four_external_sigkills",
    "kill_in_duplicate_window", "kill_with_candidate_unrecorded",
    "distinct_master_processes", "killed_by_sigkill",
}


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_start(pid) -> str | None:
    """Start time of `pid` in clock ticks since boot (field 22 of /proc/<pid>/stat),
    or None when there is no such process or the file cannot be read or parsed.
    With the pid it names one process; the fixture writes the same value into
    its pause marks (`durability_fixture._self_start`)."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError, TypeError):
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return fields[19] if len(fields) > 19 else None


def _current_boot_id() -> str:
    """This boot's id, or "" when it cannot be read."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _archive_matches_head(code: Path, head: str | None, repo: Path | None = None,
                          paths: tuple[str, ...] | None = None) -> bool:
    """Whether the tree at `code` holds exactly `head`'s files under `paths`
    (default RELEVANT_PATHS): every blob equal, no file missing, none extra
    (`__pycache__` aside). This is what `reboot-verify` runs after the restart
    (the archive `reboot-arm` exported), not the file that started the command.
    False when `head` is unknown or anything cannot be read: never a guess."""
    if not head:
        return False
    repo = HOH if repo is None else repo
    paths = RELEVANT_PATHS if paths is None else paths
    ls = _git(repo, "ls-tree", "-r", "-z", head, "--", *paths)
    if ls.returncode != 0 or not ls.stdout:
        return False
    want = {}
    for rec in ls.stdout.split("\0"):
        if not rec:
            continue
        meta, _, name = rec.partition("\t")
        mode, kind, sha = meta.split()
        if kind != "blob" or mode not in ("100644", "100755"):
            return False
        want[name] = sha
    code = Path(code).resolve()
    files = [code / n for n in want]
    if not all(f.is_file() and not f.is_symlink() for f in files):
        return False
    hashed = subprocess.run(["git", "-C", str(repo), "hash-object", "--stdin-paths"],
                            input="\n".join(str(f) for f in files) + "\n",
                            capture_output=True, text=True, check=False)
    if hashed.returncode != 0 or hashed.stdout.split() != list(want.values()):
        return False
    for p in paths:
        root = code / p
        if root.is_dir():
            extra = {str(f.relative_to(code)) for f in root.rglob("*")
                     if f.is_file() and "__pycache__" not in f.parts}
            if extra - set(want):
                return False
    return True


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "f@localhost",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "f@localhost"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=False, env=env)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

class Scenario:
    #: Prepended to PYTHONPATH of every process when a negative control runs.
    control_path: Path | None = None

    def __init__(self, base: Path, evidence: Path, name: str, nodes: list[str],
                 *, fresh: bool = True):
        from hoh.project import ProjectState, TaskNode
        from hoh.projectstore import ProjectStore

        self.name = name
        self.dir = base / name
        self.repo = self.dir / "repo"
        self.root = self.dir / "root"
        self.marks = self.dir / "marks"
        self.out = evidence / name
        self.procs: list[subprocess.Popen] = []
        #: Set once the evidence is captured; moments taken afterwards (a
        #: liveness probe run on top of it) are marked as such.
        self.captured = False
        self.store = ProjectStore(self.root, PROJECT)
        if not fresh:
            return
        for d in (self.repo, self.root, self.marks, self.out):
            d.mkdir(parents=True, exist_ok=False)
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README").write_text("fixture\n")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.store.create(ProjectState(project_id=PROJECT, repo_path=str(self.repo),
                                       nodes=[TaskNode(id=n) for n in nodes]))
        self.register_driver(self.store.dir)

    @staticmethod
    def register_driver(project_dir: Path) -> None:
        (project_dir / "driver.json").write_text(json.dumps(
            {"factory": "durability_fixture:build", "path": str(CODE / "tools")}))

    def step(self, kind: str, **fields) -> dict:
        rec = {"kind": kind, "at": utc(), **fields}
        with open(self.out / "steps.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def _env(self, pause: list[str], block: str, inject: Path | None, crash: str) -> dict:
        path = str(CODE / "src")
        for extra in (inject, Scenario.control_path):
            if extra is not None:
                path = f"{extra}{os.pathsep}{path}"
        return {**os.environ, "PYTHONPATH": path, "HOH_DURABILITY_CRASH": crash,
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOH_DURABILITY_PAUSE": ",".join(pause),
                "HOH_DURABILITY_MARKS": str(self.marks),
                "HOH_DURABILITY_PROVIDER_BLOCK": block}

    def hoh(self, *argv: str, pause: list[str] = (), block: str = "", label: str = "",
            inject: Path | None = None, crash: str = "") -> subprocess.Popen:
        """Start one `hoh` process with no stdin in a session of its own, and
        measure both from /proc rather than asserting them."""
        n = sum(1 for _ in self.out.glob("proc-*.out"))
        log = open(self.out / f"proc-{n:02d}-{label or argv[0]}.out", "w")
        p = subprocess.Popen(
            [sys.executable, "-c", "from hoh.cli import main; raise SystemExit(main())",
             "--root", str(self.root), *argv],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env=self._env(list(pause), block, inject, crash),
            cwd=str(self.dir))
        try:
            stdin = os.readlink(f"/proc/{p.pid}/fd/0")
        except OSError:
            stdin = None
        try:
            sid_is_pid = os.getsid(p.pid) == p.pid
        except OSError:
            sid_is_pid = None
        p._log_path = log.name  # type: ignore[attr-defined]
        p._label = label  # type: ignore[attr-defined]
        self.procs.append(p)
        self.step("process-start", pid=p.pid, argv=list(argv), label=label,
                  measured={"stdin": stdin, "sid_is_pid": sid_is_pid},
                  pause=list(pause), crash=crash or None)
        return p

    def drive(self, session: str, pause: list[str] = (), ttl: int = 1800,
              block: str = "", token_out: Path | None = None) -> subprocess.Popen:
        extra = ["--token-out", str(token_out)] if token_out else []
        return self.hoh("project", "drive", PROJECT, "--session", session,
                        "--ttl", str(ttl), *extra, pause=pause, block=block,
                        label=f"drive-{session}")

    def tick(self, label: str, pause: list[str] = ()) -> subprocess.Popen:
        """One firing of the canonical continuation unit (see TICK_DESCRIPTION)."""
        return self.hoh(*CONTINUE_ARGV, pause=pause, label=label)

    def wait_reached(self, point: str, proc: subprocess.Popen, timeout: float = 60) -> dict:
        mark = self.marks / f"{point.replace(':', '__')}.reached"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if mark.exists():
                try:
                    data = json.loads(mark.read_text())
                    self.step("paused", point=point, pid=data["pid"])
                    return data
                except ValueError:
                    pass
            if proc.poll() is not None:
                raise RuntimeError(f"{self.name}: process {proc.pid} exited "
                                   f"({proc.returncode}) before reaching {point}: "
                                   + Path(proc._log_path).read_text()[-600:])
            time.sleep(0.05)
        raise RuntimeError(f"{self.name}: {point} not reached within {timeout}s")

    def release(self, point: str) -> None:
        (self.marks / f"{point.replace(':', '__')}.release").write_text("go")
        self.step("released", point=point)

    def signal(self, proc: subprocess.Popen, sig: int, point: str) -> None:
        os.kill(proc.pid, sig)
        self.step(f"signal-{signal.Signals(sig).name.lower()}", pid=proc.pid, point=point)

    def kill(self, proc: subprocess.Popen, point: str) -> None:
        os.kill(proc.pid, signal.SIGKILL)
        rc = proc.wait(timeout=30)
        self.step("sigkill", pid=proc.pid, point=point, returncode=rc)

    def finish(self, proc: subprocess.Popen, timeout: float = 120) -> dict:
        rc = proc.wait(timeout=timeout)
        text = Path(proc._log_path).read_text()
        try:
            payload = json.loads(text[text.index("{"):])
        except ValueError:
            payload = {"text": text[-800:]}
        self.step("process-exit", pid=proc.pid, returncode=rc, label=proc._label,
                  output=payload)
        return payload

    def run_cmd(self, *argv: str, label: str) -> int:
        p = self.hoh(*argv, label=label)
        rc = p.wait(timeout=120)
        self.step("process-exit", pid=p.pid, returncode=rc, label=label,
                  output={"text": Path(p._log_path).read_text()[-800:]})
        return rc

    def moment(self, tag: str, *pids: str) -> None:
        """Preserve project directories as they are *now*, so a check about a
        moment is judged from what was durable then, not from a hash the
        harness wrote down (review B, 1)."""
        for pid in pids or (PROJECT,):
            src = self.root / "projects" / pid
            shutil.copytree(src, self.out / "moments" / tag / "projects" / pid)
        self.step("moment", tag=tag, projects=list(pids or (PROJECT,)),
                  after_capture=self.captured)

    def capture(self, *, skip_marks: bool = False) -> None:
        # A paused role-run child is not in self.procs (the fixture started it);
        # its pid and its start time are in the mark it wrote. Released points
        # are left alone. A pid is signalled only if the process holding it now
        # started when the mark says: a pid reused by an unrelated process, or
        # a mark that carries no start time, or a start time that cannot be
        # read, signals nothing. `skip_marks` is for a capture after (or not
        # provably without) a reboot: no mark is read at all.
        for mark in ([] if skip_marks else self.marks.glob("*.reached")):
            if mark.with_suffix(".release").exists():
                continue
            try:
                data = json.loads(mark.read_text())
                pid = data["pid"]
                now = _pid_start(pid)
                if now is None:
                    continue                      # no such process, or unreadable
                if not data.get("start") or data["start"] != now:
                    self.step("mark-not-signalled", pid=pid, point=data.get("point"),
                              reason="the process now holding the pid is not the one "
                                     "that wrote the mark" if data.get("start")
                                     else "the mark carries no process start time")
                    continue
                os.kill(pid, signal.SIGKILL)
                self.step("sigkill", pid=pid, point="cleanup-child", returncode=None)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.kill(p.pid, signal.SIGCONT)
                except OSError:
                    pass
                os.kill(p.pid, signal.SIGKILL)
                p.wait()
                self.step("sigkill", pid=p.pid, point="cleanup", returncode=p.returncode)
        shutil.copytree(self.root / "projects", self.out / "projects")
        _git(self.repo, "bundle", "create", str(self.out / "repo.bundle"), "--all")
        self.captured = True
        self.step("captured")


def scenario_controller_kill(base: Path, evidence: Path) -> None:
    """DS1: four external SIGKILLs, each followed by an automatic replacement."""
    sc = Scenario(base, evidence, "controller-kill", ["n1", "n2", "n3"])
    points = ["post-launch:n1", "post-merge:n2", "pre-merge:n3", "pre-gate"]
    p = sc.drive("master-1", pause=[points[0]])
    try:
        for i, point in enumerate(points):
            try:
                sc.wait_reached(point, p)
            except RuntimeError as exc:
                sc.step("stalled", point=point, why=str(exc)[:300])
                break
            sc.kill(p, point)
            sc.moment(f"after-kill-{i + 1}")
            p = sc.tick(f"tick-{i + 1:02d}", pause=points[i + 1:i + 2])
        sc.finish(p)
    finally:
        sc.capture()


def scenario_rollover(base: Path, evidence: Path) -> None:
    """DS5 rollover, DS6 the old master wakes and writes nothing, AC7."""
    sc = Scenario(base, evidence, "rollover", ["n1", "n2"])
    token = sc.dir / "A.token"
    a = sc.drive("A", pause=["post-launch:n2"], token_out=token)
    try:
        sc.wait_reached("post-launch:n2", a)
        sc.run_cmd("project", "unblock", PROJECT, "n1", "--reason", "anonymous attempt",
                   label="ac7-unblock-no-credential-while-live")
        sc.run_cmd("project", "handoff", PROJECT, "--session", "A", "--epoch", "1",
                   "--token-file", str(token), "--to", "B", "--reason", "context rotation",
                   label="handoff-A-to-B")
        sc.finish(sc.drive("B"))
        sc.moment("B")
        sc.release("post-launch:n2")
        sc.finish(a)
        sc.moment("A-woke")
        cred = ["--session", "A", "--epoch", "1", "--token-file", str(token)]
        sc.run_cmd("project", "unblock", PROJECT, "n1", "--reason", "stale attempt", *cred,
                   label="ac7-unblock-old-epoch")
        sc.run_cmd("project", "record-action", PROJECT, "--actor", "operator",
                   "--reason", "stale attempt", *cred, label="ac7-record-action-old-epoch")
        sc.run_cmd("project", "handoff", PROJECT, *cred, "--to", "C",
                   "--reason", "stale rollover", label="ac7-handoff-old-epoch")
        sc.moment("AC7")
    finally:
        sc.capture()
    # Liveness, after the evidence above is captured: with no live master an
    # operator command gets an epoch of its own (review A, P5).
    sc.run_cmd("project", "record-action", PROJECT, "--actor", "operator",
               "--reason", "recorded after closure", label="operator-without-live-master")
    sc.moment("operator")


def scenario_lapse(base: Path, evidence: Path) -> None:
    """DS6 without a handoff: A is frozen (SIGSTOP) until its lease lapses."""
    sc = Scenario(base, evidence, "lapse", ["n1"])
    a = sc.drive("A", pause=["post-launch:n1"], ttl=3)
    try:
        sc.wait_reached("post-launch:n1", a)
        # A frozen process freezes its keepalive thread too; a merely paused
        # one would keep renewing and rightly stay the master.
        sc.signal(a, signal.SIGSTOP, "post-launch:n1")
        time.sleep(4.5)
        sc.finish(sc.drive("B"))
        sc.moment("B")
        sc.release("post-launch:n1")
        sc.signal(a, signal.SIGCONT, "post-launch:n1")
        sc.finish(a)
        sc.moment("A-woke")
    finally:
        sc.capture()


def scenario_quota(base: Path, evidence: Path) -> None:
    """DS3 + DS4 + O211: a usage limit parks a node on a durable timer, the
    master goes away, the canonical tick discovers the due timer, resumes only
    its own class, refuses a stale generation, and a new governed master drives
    to closure -- which disarms the programme's remaining timers."""
    from hoh.orchestrator import node_timer_key
    from hoh.project import Lifecycle, ProjectState, TaskNode
    from hoh.projectstore import ProjectStore
    from hoh.timers import TimerKind, TimerStore

    sc = Scenario(base, evidence, "quota", ["n1", "n2"])
    try:
        sc.finish(sc.drive("A", block="n1=8"))
        other = ProjectStore(sc.root, "other")
        other.create(ProjectState(project_id="other", repo_path=str(sc.repo), nodes=[
            TaskNode(id="m1", lifecycle=Lifecycle.BLOCKED,
                     blocked_kind="collected_for_digest",
                     note="question collected for the digest")]))
        Scenario.register_driver(other.dir)
        TimerStore(other.dir).arm_new(kind=TimerKind.EXTERNAL_WAIT, due_at=utc(),
                                      reason="digest review due", program_id="other",
                                      program_epoch=1, run_id=node_timer_key("m1"),
                                      node_id="m1")
        timers = TimerStore(sc.store.dir)
        timers.arm_new(kind=TimerKind.PROVIDER_RETRY, due_at=utc(),
                       reason="armed under generation 0", program_id=PROJECT,
                       program_epoch=0, run_id=node_timer_key("n2"), node_id="n2")
        timers.arm_new(kind=TimerKind.HEARTBEAT, due_at="2099-01-01T00:00:00Z",
                       reason="programme heartbeat", program_id=PROJECT, program_epoch=1)
        sc.step("fixtures", other_project="other", stale_timer_generation=0,
                heartbeat=True)
        sc.moment("fixtures", PROJECT, "other")
        deadline = time.monotonic() + 90
        ticks = 0
        while time.monotonic() < deadline:
            ticks += 1
            sc.finish(sc.tick(f"tick-{ticks:02d}"))
            if sc.store.read_state().rc_closed():
                break
            time.sleep(2)
        sc.finish(sc.tick(f"tick-{ticks + 1:02d}-after"))
        sc.moment("ticks", PROJECT, "other")
    finally:
        sc.capture()


def scenario_moved_branch(base: Path, evidence: Path) -> None:
    """The accepted object, not the branch name: A is accepted, the branch is
    advanced to an unverified B before the merge, and nothing may land."""
    sc = Scenario(base, evidence, "moved-branch", ["n1"])
    a = sc.drive("A", pause=["post-launch:n1"])
    try:
        sc.wait_reached("post-launch:n1", a)
        accepted = _git(sc.repo, "rev-parse", "hoh-n1").stdout.strip()
        env = {**os.environ, "GIT_INDEX_FILE": str(sc.dir / "b.index"),
               "GIT_AUTHOR_NAME": "other", "GIT_AUTHOR_EMAIL": "o@l",
               "GIT_COMMITTER_NAME": "other", "GIT_COMMITTER_EMAIL": "o@l"}

        def g(*args, **kw):
            return subprocess.run(["git", "-C", str(sc.repo), *args], env=env,
                                  capture_output=True, text=True, check=True,
                                  **kw).stdout.strip()
        blob = g("hash-object", "-w", "--stdin", input="unverified B\n")
        g("read-tree", accepted)
        g("update-index", "--add", "--cacheinfo", f"100644,{blob},n1.txt")
        moved = g("commit-tree", g("write-tree"), "-p", accepted, "-m", "B after acceptance")
        _git(sc.repo, "update-ref", "refs/heads/hoh-n1", moved)
        sc.step("branch-moved", accepted=accepted, moved_to=moved,
                main_before=_git(sc.repo, "rev-parse", "main").stdout.strip())
        sc.release("post-launch:n1")
        sc.finish(a)
    finally:
        sc.capture()


def scenario_quota_crash(base: Path, evidence: Path) -> None:
    """The retry timer's crash invariant, with real SIGKILL at three points."""
    for where in CRASH_POINTS:
        sc = Scenario(base / "quota-crash", evidence / "quota-crash", where, ["n1"])
        inject = sc.dir / "inject"
        inject.mkdir()
        (inject / "sitecustomize.py").write_text(CRASH_INJECTOR)
        try:
            a = sc.hoh("project", "drive", PROJECT, "--session", "A", block="n1=6",
                       label="drive-A", inject=inject, crash=where)
            rc = a.wait(timeout=60)
            sc.step("process-exit", pid=a.pid, returncode=rc, label="drive-A",
                    output={"text": Path(a._log_path).read_text()[-400:]})
            sc.moment("after-crash")
            deadline = time.monotonic() + 60
            ticks = 0
            while time.monotonic() < deadline:
                ticks += 1
                sc.finish(sc.tick(f"tick-{ticks:02d}"))
                if sc.store.read_state().rc_closed():
                    break
                time.sleep(1.5)
            sc.finish(sc.tick(f"tick-{ticks + 1:02d}-after"))
        finally:
            sc.capture()


def scenario_orphaned_run(base: Path, evidence: Path) -> None:
    """Finding #9: the master is killed while its role run -- a child with no
    session of its own -- is between two iterations: run lock free, process
    alive, process marker held. The tick must wait for that run (no epoch
    spent) and recover once it has finished."""
    sc = Scenario(base, evidence, "orphaned-run", ["n1"])
    # Between the child's two iterations: its run lock is free, its process
    # alive. Only the process marker tells a run in flight from a dead one.
    point = "between-iterations:n1"
    a = sc.drive("A", pause=[point])
    try:
        child = sc.wait_reached(point, a)
        sc.step("orphan", child_pid=child["pid"], master_pid=a.pid)
        sc.kill(a, point)
        sc.finish(sc.tick("tick-01"))
        sc.moment("while-in-flight")
        sc.release(point)
        deadline = time.monotonic() + 60
        n = 1
        while time.monotonic() < deadline:
            n += 1
            time.sleep(1)
            sc.finish(sc.tick(f"tick-{n:02d}"))
            if sc.store.read_state().rc_closed():
                break
    finally:
        sc.capture()


def _export_head(base: Path) -> Path:
    """Export HEAD's tree (not the working tree) to run the scenarios from."""
    global EXPORTED_HEAD
    code = base / "code"
    code.mkdir(parents=True)
    EXPORTED_HEAD = _git(HOH, "rev-parse", "HEAD").stdout.strip()
    archive = subprocess.run(["git", "-C", str(HOH), "archive", EXPORTED_HEAD],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(code)], input=archive, check=True)
    return code


def _use_code_for_this_process() -> None:
    """Make *this* process import `hoh` from CODE too. The scenario processes
    already do; this one writes initial project states and fixture timers, and
    importing the working tree's `hoh` here let uncommitted product code write
    into evidence labelled "git archive HEAD" (review B5, B1). Every `hoh`
    import in this file is lazy, so nothing has been imported yet."""
    if "hoh" in sys.modules:
        raise RuntimeError("hoh was imported before the archive was on the path")
    live = str(HOH / "src")
    sys.path[:] = [p for p in sys.path if p != live]
    sys.path.insert(0, str(CODE / "src"))
    import hoh
    if not Path(hoh.__file__).resolve().is_relative_to(CODE.resolve()):
        raise RuntimeError(f"hoh resolves to {hoh.__file__}, not to the archive")


def _harness_matches_head() -> bool:
    """Whether this orchestrating file is HEAD's: the scenarios run from the
    archive, but this process runs from the working tree."""
    head = subprocess.run(["git", "-C", str(HOH), "show",
                           f"{EXPORTED_HEAD or 'HEAD'}:tools/durability_e2e.py"],
                          capture_output=True).stdout
    return head == Path(__file__).read_bytes()


def _meta(base: Path) -> dict:
    boot = Path("/proc/sys/kernel/random/boot_id")
    from_head = CODE != HOH
    hoh_mod = sys.modules.get("hoh")
    tree_edits = _git(HOH, "status", "--porcelain", "--", *RELEVANT_PATHS).stdout.strip()
    head = EXPORTED_HEAD if from_head else _git(HOH, "rev-parse", "HEAD").stdout.strip()
    return {"started_at": utc(), "head": head,
            "code": f"git archive {head}" if from_head else "live working tree",
            "parent_hoh": (str(Path(hoh_mod.__file__).resolve().relative_to(CODE.resolve()))
                           if from_head and hoh_mod is not None else None),
            # Dirty means: something this evidence ran is not HEAD's -- the
            # orchestrator, the code the processes import, or any edit to the
            # relevant paths of the working tree at the moment of measuring.
            "dirty": not (from_head and _harness_matches_head()) or bool(tree_edits),
            "python": sys.version.split()[0], "scratch": str(base),
            "boot_id": boot.read_text().strip() if boot.exists() else "",
            "tick": TICK_DESCRIPTION, "continue_argv": list(CONTINUE_ARGV),
            "agent_execution": "deterministic fixture (tools/durability_fixture.py); "
                               "role runs are not exercised here"}


def cmd_run(args) -> int:
    # Absolute: `git -C <scratch repo> bundle create <path>` resolves a relative
    # path against the scratch repository, and the first evidence run wrote its
    # bundles there instead of into the evidence (derive failed closed).
    evidence = Path(args.evidence).resolve()
    if evidence.exists() and any(evidence.iterdir()):
        print(f"refused: {evidence} is not empty; evidence is never overwritten",
              file=sys.stderr)
        return 2
    evidence.mkdir(parents=True, exist_ok=True)
    base = Path(args.base or tempfile.mkdtemp(prefix="hoh-durability-")).resolve()
    global CODE, CONTINUE_ARGV
    if not args.live:
        CODE = _export_head(base)
        CONTINUE_ARGV = _continue_argv(CODE / "ops" / "hoh-continue.service")
        _use_code_for_this_process()
    meta = _meta(base)
    failures = {}
    for name in args.only or SCENARIOS:
        try:
            globals()[f"scenario_{name.replace('-', '_')}"](base, evidence)
        except Exception as exc:                      # noqa: BLE001 - recorded
            failures[name] = f"{type(exc).__name__}: {exc}"[:800]
    controls = {}
    for c in CONTROLS:
        if args.only and c["scenario"] not in args.only:
            continue
        key = f"{c['name']}@{c['scenario']}"
        cdir = base / "controls" / key
        cdir.mkdir(parents=True)
        (cdir / "sitecustomize.py").write_text(c["patch"])
        Scenario.control_path = cdir
        try:
            globals()[f"scenario_{c['scenario'].replace('-', '_')}"](
                base / "negative-control" / key, evidence / "negative-control" / key)
            controls[key] = "ran"
        except Exception as exc:                      # noqa: BLE001 - recorded
            controls[key] = f"harness error: {type(exc).__name__}: {exc}"[:300]
        finally:
            Scenario.control_path = None
    meta["negative_controls"] = controls
    meta["finished_at"] = utc()
    # Measured again at the end: an edit made during the run and reverted
    # before readiness would otherwise be invisible (review B3, 8).
    meta["dirty_at_end"] = _meta(base)["dirty"]
    meta["harness_failures"] = failures
    (evidence / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    return 1 if failures else 0


def cmd_reboot_arm(args) -> int:
    """Start a master that will not survive a reboot, and leave it paused."""
    evidence = Path(args.evidence).resolve()
    if (evidence / "reboot").exists():
        print(f"refused: {evidence / 'reboot'} exists", file=sys.stderr)
        return 2
    base = Path(args.base or tempfile.mkdtemp(prefix="hoh-durability-reboot-",
                                              dir=str(Path.home()))).resolve()
    global CODE, CONTINUE_ARGV
    if not args.live:
        CODE = _export_head(base)
        CONTINUE_ARGV = _continue_argv(CODE / "ops" / "hoh-continue.service")
        _use_code_for_this_process()
    sc = Scenario(base, evidence, "reboot", ["n1", "n2"])
    a = sc.drive("master-1", pause=["post-launch:n1"])
    sc.wait_reached("post-launch:n1", a)
    meta = _meta(base)
    sc.step("reboot-arm", boot_id=meta["boot_id"], pid=a.pid)
    (sc.out / "ARMED.json").write_text(json.dumps(
        {"base": str(base), "pid": a.pid, "code_root": str(CODE), **meta}, indent=2) + "\n")
    print(f"armed: master pid {a.pid} is paused on boot {meta['boot_id'][:8]}.\n"
          f"Now restart the machine (on WSL: `wsl --shutdown` from Windows), then run:\n"
          f"  python3 tools/durability_e2e.py reboot-verify --evidence {evidence}")
    return 0


def cmd_reboot_verify(args) -> int:
    evidence = Path(args.evidence).resolve()
    armed = json.loads((evidence / "reboot" / "ARMED.json").read_text())
    if any(s.get("kind") == "reboot-verify" for s in _steps(evidence / "reboot")):
        print(f"refused: {evidence / 'reboot'} was verified already; evidence is "
              "never appended to", file=sys.stderr)
        return 2
    global CODE, CONTINUE_ARGV, EXPORTED_HEAD
    CODE = Path(armed.get("code_root") or HOH)
    CONTINUE_ARGV = _continue_argv(CODE / "ops" / "hoh-continue.service")
    if CODE != HOH:
        _use_code_for_this_process()
    EXPORTED_HEAD = armed.get("head") or None
    sc = Scenario(Path(armed["base"]), evidence, "reboot", [], fresh=False)
    boot = _current_boot_id() or ""
    # Pause marks name pids of the previous boot. They are read only when the
    # boot is positively the same one: a missing, empty or unreadable boot id
    # on either side is "not provably the same" and signals nothing.
    same_boot = bool(boot) and armed.get("boot_id") == boot
    sc.step("reboot-verify", boot_id=boot, marks_skipped=not same_boot,
            tool_matches_armed_head=bool(EXPORTED_HEAD) and _harness_matches_head(),
            code_matches_armed_head=_archive_matches_head(CODE, EXPORTED_HEAD))
    try:
        deadline = time.monotonic() + 90
        ticks = 0
        while time.monotonic() < deadline:
            ticks += 1
            sc.finish(sc.tick(f"tick-{ticks:02d}"))
            if sc.store.read_state().rc_closed():
                break
            time.sleep(2)
    finally:
        sc.capture(skip_marks=not same_boot)
    print(f"verify done after {ticks} tick(s); boot "
          f"{str(armed.get('boot_id') or '')[:8]} -> {boot[:8]}")
    return 0


# --------------------------------------------------------------------------- #
# Deriving -- from the raw evidence only
# --------------------------------------------------------------------------- #

def _sanitize(msg: str, work: Path, evidence: Path) -> str:
    """A message independent of where the evidence lives and which temporary
    directory derive used (review B3, 10)."""
    msg = msg.replace(str(evidence), "<evidence>")
    msg = re.sub(re.escape(str(work)) + r"/[^/\s'\"]+", "<work>", msg)
    msg = msg.replace(str(work), "<work>")
    msg = re.sub(r"/tmp/hoh-durability-derive-[^/\s'\"]+(/[^/\s'\"]+)*", "<work>", msg)
    return re.sub(r"https://errors\.pydantic\.dev/\S+", "<pydantic-doc>", msg)


def _steps(d: Path) -> list[dict]:
    f = d / "steps.jsonl"
    return [json.loads(z) for z in f.read_text().splitlines()] if f.is_file() else []


def _repo_facts(d: Path, work: Path) -> dict:
    """Merge facts read from the bundle itself."""
    clone = Path(tempfile.mkdtemp(prefix=f"{d.name}-", dir=work)) / "clone"
    subprocess.run(["git", "clone", "-q", str(d / "repo.bundle"), str(clone)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], capture_output=True)

    def g(*a):
        return subprocess.run(["git", "-C", str(clone), *a], capture_output=True,
                              text=True).stdout
    first_parent = []
    for z in g("log", "--first-parent", "--format=%H %P", "main").splitlines():
        parts = z.split()
        first_parent.append((parts[0], parts[1:]))
    return {"first_parent": first_parent,
            "files": sorted(g("ls-tree", "--name-only", "main").split()),
            "contents": {f: g("show", f"main:{f}") for f in
                         g("ls-tree", "--name-only", "main").split()},
            "head": g("rev-parse", "main").strip()}


def _merged_count(repo: dict, sha: str) -> int:
    return sum(1 for _, ps in repo["first_parent"] if len(ps) > 1 and sha in ps[1:])


def _project_facts(d: Path, pid: str = PROJECT) -> dict:
    from hoh.epoch import EpochStore
    from hoh.project import ProjectState
    from hoh.timers import TimerStore

    pdir = d / "projects" / pid
    raw = (pdir / "project.json").read_bytes()
    state = ProjectState.model_validate_json(raw)
    seqs = []
    for f in sorted(pdir.glob("project.json.v*")):
        try:
            seqs.append(json.loads(f.read_text())["write_seq"])
        except (ValueError, KeyError):
            seqs.append(None)
    seqs.append(state.write_seq)
    store = EpochStore(pdir)
    fsck = store.fsck()
    chain = [] if fsck else store.chain()
    candidates = {}
    for n in state.nodes:
        rec = pdir / "fixture-runs" / (n.run_id or n.id) / "state.json"
        if rec.is_file():
            last = json.loads(rec.read_text()).get("last_accepted_candidate")
            if last:
                candidates[n.id] = last["commit"]
    launches_f = pdir / "fixture" / "launches.jsonl"
    launches = [json.loads(z) for z in launches_f.read_text().splitlines()] \
        if launches_f.is_file() else []
    log_f = pdir / "lease.log.jsonl"
    log = [json.loads(z) for z in log_f.read_text().splitlines()] if log_f.is_file() else []
    return {"state": state, "sha256": _authoritative_digest(pdir),
            "chain_problems": _independent_chain_problems(pdir),
            "write_seqs": sorted(s for s in seqs if s is not None),
            "epochs": [(e.epoch, e.master_id, e.reason.value) for e in chain],
            "fsck": fsck, "candidates": candidates, "launches": launches,
            "lease_log": log, "timers": TimerStore(pdir).read()}


#: Lock bookkeeping, not state: a process that takes a lock and is then refused
#: rewrites the holder note in these files, and that is not a write of state.
_NOT_STATE = {".lock", ".authority.lock", ".timers.lock"}


def _authoritative_digest(pdir: Path) -> str:
    """sha256 over every file of a project directory except lock notes: the
    state, its parked versions, the epoch chain, the lease and its log, the
    timers. A stale master that rewrote the lease passed a check that hashed
    project.json alone (review B2, 5)."""
    h = hashlib.sha256()
    for f in sorted(pdir.rglob("*")):
        if f.is_file() and f.name not in _NOT_STATE:
            h.update(str(f.relative_to(pdir)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _independent_chain_problems(pdir: Path) -> list[str]:
    """The epoch chain re-checked here, not by the product's own fsck: each
    epoch's digest recomputed from its content (the documented canonical form
    of `hoh.epoch.epoch_digest`) and linked to its predecessor."""
    f = pdir / "epochs.json"
    if not f.is_file():
        return ["no epochs.json"]
    rows = json.loads(f.read_text()).get("epochs", [])
    problems, prev = [], ""
    for i, r in enumerate(sorted(rows, key=lambda r: r.get("epoch", 0)), 1):
        content = {"epoch": r.get("epoch"), "master_id": r.get("master_id"),
                   "reason": r.get("reason"), "previous_digest": r.get("previous_digest"),
                   "note": r.get("note", "")}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode()).hexdigest()
        if r.get("epoch") != i:
            problems.append(f"epoch numbers not 1..n at {i}")
        if r.get("previous_digest") != prev:
            problems.append(f"epoch {i} does not link to its predecessor")
        if r.get("epoch_digest") != digest:
            problems.append(f"epoch {i} digest does not match its content")
        prev = digest
    # Append-only: every preserved earlier form of the chain must be a prefix
    # of the final one. A chain rewritten consistently from the start passes
    # the digest check above; it does not pass this (review B3, 13).
    final = [r.get("epoch_digest") for r in sorted(rows, key=lambda r: r.get("epoch", 0))]
    versions = sorted(pdir.glob("epochs.json.v*"))
    # One preserved form per epoch after the first -- the chain's own history,
    # checked for completeness like the write_seq chain is (review B4).
    if len(versions) != max(len(final) - 1, 0):
        problems.append(f"{len(versions)} preserved epoch-chain versions for "
                        f"{len(final)} epochs; expected {max(len(final) - 1, 0)}")
    lengths = []
    for v in versions:
        try:
            old = [r.get("epoch_digest") for r in sorted(
                json.loads(v.read_text()).get("epochs", []), key=lambda r: r.get("epoch", 0))]
        except ValueError:
            problems.append(f"{v.name} is unreadable")
            continue
        if final[:len(old)] != old:
            problems.append(f"{v.name} is not a prefix of the current chain")
        lengths.append(len(old))
    if sorted(lengths) != list(range(1, len(final))):
        problems.append(f"preserved chain lengths {sorted(lengths)} are not 1..{len(final) - 1}")
    return problems


def _moment(d: Path, tag: str, pid: str = PROJECT) -> dict | None:
    m = d / "moments" / tag
    return _project_facts(m, pid) if (m / "projects" / pid / "project.json").is_file() else None


def _exits(steps: list[dict]) -> dict[str, dict]:
    labels = [s["label"] for s in steps if s["kind"] == "process-exit"]
    dup = {x for x in labels if labels.count(x) > 1}
    if dup:
        raise ValueError(f"process labels are not unique: {sorted(dup)}")
    return {s["label"]: s for s in steps if s["kind"] == "process-exit"}


def _no_human(steps: list[dict], facts: dict) -> list[str]:
    problems = []
    if not any(s["kind"] == "process-start" for s in steps):
        # An absence of measurements is not an absence of humans (review B2, 8).
        problems.append("no process was measured")
    started = {s["pid"] for s in steps if s["kind"] == "process-start"}
    for s in steps:
        # A SIGKILL ends a process as much as an exit does (review B4).
        ended = s["kind"] == "process-exit" or (
            s["kind"] == "sigkill" and s.get("point") != "cleanup-child")
        if ended and s["pid"] not in started:
            problems.append(f"process {s['pid']} ({s.get('label') or s.get('point')}) "
                            f"ended with no measured start")
    for s in steps:
        if s["kind"] != "process-start":
            continue
        m = s.get("measured") or {}
        if m.get("stdin") != "/dev/null" or m.get("sid_is_pid") is not True:
            problems.append(f"process {s['pid']} ({s.get('label')}): measured {m}")
    st = facts["state"]
    humans = [d.id for d in st.decisions if d.actor != "orchestrator"]
    if humans:
        problems.append(f"decisions by a non-orchestrator actor: {humans}")
    if st.external_actions:
        problems.append(f"{len(st.external_actions)} external action(s) recorded")
    return problems


def _exactly_once(facts: dict, repo: dict, *, landed: list[str] | None = None) -> list[str]:
    """Main is exactly: the initial commit plus one merge per landed node, each
    taking in that node's accepted candidate, and nothing else; each node was
    launched to acceptance exactly once."""
    nodes = [n.id for n in facts["state"].nodes]
    landed = nodes if landed is None else landed
    problems = []
    fp = repo["first_parent"]
    non_merges = [c for c, ps in fp if len(ps) < 2]
    if len(non_merges) != 1:
        problems.append(f"main's first-parent chain has {len(non_merges)} non-merge "
                        f"commits, expected only the initial one")
    merged_in = Counter(ps[1] for _, ps in fp if len(ps) > 1)
    expected = Counter(facts["candidates"][n] for n in landed if n in facts["candidates"])
    if merged_in != expected:
        problems.append(f"merges on main {dict(merged_in)} != accepted candidates "
                        f"{dict(expected)}")
    want_files = sorted(["README"] + [f"{n}.txt" for n in landed])
    if repo["files"] != want_files:
        problems.append(f"files on main {repo['files']} != {want_files}")
    for n in landed:
        if repo["contents"].get(f"{n}.txt") != f"{n}\n":
            problems.append(f"{n}.txt on main is not the accepted content")
        if n not in facts["candidates"]:
            problems.append(f"node {n} has no candidate record")
    accepted = Counter(x["node"] for x in facts["launches"] if x["result"] == "accepted")
    for n in facts["candidates"]:
        if accepted.get(n, 0) != 1:
            problems.append(f"node {n} was launched to acceptance {accepted.get(n, 0)} times")
    return problems


def _continuity(facts: dict) -> list[str]:
    seqs = facts["write_seqs"]
    problems = []
    # From the first write on: every state the store ever wrote is preserved.
    if not (seqs and seqs == list(range(1, len(seqs) + 1))):
        problems.append(f"write_seq chain not 1..n and contiguous: {seqs}")
    if facts["fsck"]:
        problems.append("epoch chain fails fsck: " + "; ".join(facts["fsck"]))
    problems += [f"independent chain check: {p}" for p in facts["chain_problems"]]
    return problems


def _harness_initiated(steps: list[dict]) -> list[str]:
    """Everything the harness did itself, in step order: the processes it
    started, the tick loop (every tick start), every SIGKILL it sent (cleanup
    and cleanup-child included), every crash it injected into a started
    process, every signal recorded as a `signal-<name>` step, and the
    releases, branch moves and fixtures it applied."""
    out = []
    ticks = 0
    for s in steps:
        if s["kind"] == "process-start":
            if s["argv"] == CONTINUE_ARGV:
                ticks += 1
                out.append(f"tick: {s.get('label') or ticks}")
            else:
                out.append(f"process: {s.get('label') or ' '.join(s['argv'][:2])}")
            if s.get("crash"):
                out.append(f"crash: {s['crash']}")
        elif s["kind"] == "sigkill":
            out.append(f"sigkill: {s.get('point', '')}".rstrip(": "))
        elif s["kind"] in ("released", "branch-moved", "fixtures") \
                or s["kind"].startswith("signal-"):
            out.append(f"{s['kind']}: {s.get('point', '')}".rstrip(": "))
    return out


#: The moments each scenario takes, declared here and required by derive: a
#: moment that was never taken, or whose copy is missing, would otherwise
#: leave the cross-check below with nothing to compare (review B7).
#: The value is the number of epochs the governed project's chain must hold at
#: that moment -- an empty or truncated chain is a prefix of anything and
#: would compare nothing (review B7). Forging every moment consistently with a
#: forged final chain remains undetectable from the evidence alone; that limit
#: is inherent to evidence the run itself writes.
EXPECTED_MOMENTS = {
    "controller-kill": {"after-kill-1": 1, "after-kill-2": 2, "after-kill-3": 3,
                        "after-kill-4": 4},
    "rollover": {"B": 2, "A-woke": 2, "AC7": 2, "operator": 3},
    "lapse": {"B": 2, "A-woke": 2},
    "quota": {"fixtures": 1, "ticks": 2},
    "moved-branch": {},
    "orphaned-run": {"while-in-flight": 1},
    "before-arm": {"after-crash": 1}, "after-arm": {"after-crash": 1},
    "after-park": {"after-crash": 1},
    "reboot": {},
}


def _missing_moments(d: Path) -> list[str]:
    """Every declared or logged moment, with its content: the project state of
    every project its step names, and the governed project's epoch chain. A
    folder with nothing in it compares nothing (review B7)."""
    logged = {s.get("tag"): s.get("projects") or [PROJECT]
              for s in _steps(d) if s["kind"] == "moment"}
    counts = EXPECTED_MOMENTS.get(d.name, {})
    want = {tag: logged.get(tag, [PROJECT]) for tag in counts}
    want.update(logged)
    problems = []
    for tag, pids in sorted(want.items()):
        for pid in pids:
            base = d / "moments" / tag / "projects" / pid
            need = ["project.json"] + (["epochs.json"] if pid == PROJECT else [])
            for f in need:
                if not (base / f).is_file():
                    problems.append(f"moment {tag}: {pid}/{f} is missing")
        chain = d / "moments" / tag / "projects" / PROJECT / "epochs.json"
        if tag in counts and chain.is_file():
            try:
                n = len(json.loads(chain.read_text()).get("epochs", []))
            except ValueError:
                n = -1
            if n != counts[tag]:
                problems.append(f"moment {tag}: {n} epochs, expected {counts[tag]}")
    return problems


def _moment_chain_problems(d: Path) -> list[str]:
    """Every preserved moment's epoch chain must be a prefix of the final one:
    the moments are copies taken during the run, so a history rewritten
    afterwards -- even consistently, versions included -- disagrees with them
    (review B5, M1)."""
    final_f = d / "projects" / PROJECT / "epochs.json"
    if not final_f.is_file():
        return []
    final = [r.get("epoch_digest") for r in sorted(
        json.loads(final_f.read_text()).get("epochs", []), key=lambda r: r.get("epoch", 0))]
    problems = []
    # Which moments came after the capture is read from the order of the step
    # log, not from a flag a moment carries (review B6, 1). Those extend the
    # history on purpose -- the operator liveness probe -- and must extend it
    # by OPERATOR epochs only, on top of exactly the captured chain.
    steps = _steps(d)
    cut = next((i for i, s in enumerate(steps) if s["kind"] == "captured"), len(steps))
    later = {s.get("tag") for i, s in enumerate(steps) if s["kind"] == "moment" and i > cut}
    for m in sorted((d / "moments").glob(f"*/projects/{PROJECT}/epochs.json")):
        rows = sorted(json.loads(m.read_text()).get("epochs", []),
                      key=lambda r: r.get("epoch", 0))
        seen = [r.get("epoch_digest") for r in rows]
        if m.parts[-4] in later:
            extra = rows[len(final):]
            if seen[:len(final)] != final or not extra \
                    or any(r.get("reason") != "OPERATOR" for r in extra):
                problems.append(f"moment {m.parts[-4]} (after capture) does not extend the "
                                f"captured chain by OPERATOR epochs only")
            continue
        if final[:len(seen)] != seen:
            problems.append(f"moment {m.parts[-4]}: its epoch chain is not a prefix "
                            f"of the final chain")
    return problems


def _result(steps, facts, repo, checks, stressors, landed=None, d=None) -> dict:
    problems = {"exactly_once": _exactly_once(facts, repo, landed=landed),
                "continuity": _continuity(facts) + (
                    _moment_chain_problems(d) + _missing_moments(d) if d else []),
                "no_human": _no_human(steps, facts)}
    return {"stressors": stressors, "checks": checks, "problems": problems,
            "harness_initiated": _harness_initiated(steps),
            "passed": bool(checks) and all(checks.values())
                      and not any(problems.values())}


def derive_controller_kill(d: Path, work: Path) -> dict:
    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    kills = [s for s in steps if s["kind"] == "sigkill" and s.get("point") != "cleanup"]
    starts = [s for s in steps if s["kind"] == "process-start"]
    ticks = [s for s in steps if s["kind"] == "process-exit"
             and str(s.get("label", "")).startswith("tick-")]
    recovered = [r for t in ticks for r in t["output"].get("projects", [])
                 if r.get("action") == "recovered"]
    holders = [m for _, m, _ in facts["epochs"]]
    checks = {
        "four_external_sigkills": len(kills) == 4 and all(k["returncode"] == -9 for k in kills),
        "kill_in_duplicate_window": any(k["point"].startswith("post-merge:") for k in kills),
        "kill_with_candidate_unrecorded": any(k["point"].startswith("post-launch:")
                                              for k in kills),
        "distinct_master_processes": len({s["pid"] for s in starts}) == len(starts) == 5,
        "only_the_first_master_started_by_hand": bool(starts)
                                                 and starts[0]["argv"][:2] == ["project", "drive"]
                                                 and all(s["argv"] == CONTINUE_ARGV
                                                         for s in starts[1:]),
        "every_replacement_by_the_canonical_tick": len(holders) == 5 and all(
            h.startswith("recover-") for h in holders[1:]) and len(recovered) >= 1,
        "closed": bool(recovered) and recovered[-1].get("halt") == "CLOSED"
                  and facts["state"].rc_closed(),
        "no_ambiguous_halt": bool(recovered) and all(r.get("halt") != "AMBIGUOUS"
                                                     for r in recovered),
        "epochs_initial_then_recovery": [r for _, _, r in facts["epochs"]]
                                        == ["INITIAL"] + ["RECOVERY"] * 4,
        "first_holder_is_the_started_master": holders[:1] == ["master-1"],
    }
    return _result(steps, facts, repo, checks, ["DS1"], d=d)


def _same_state(a: dict | None, b: dict | None) -> bool:
    """Both moments preserved, and byte-identical project state."""
    return a is not None and b is not None and a["sha256"] == b["sha256"]


def derive_rollover(d: Path, work: Path) -> dict:
    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    out = _exits(steps)
    at_b, woke, ac7 = _moment(d, "B"), _moment(d, "A-woke"), _moment(d, "AC7")
    op = _moment(d, "operator")
    a, b = out.get("drive-A", {}), out.get("drive-B", {})

    def rc(label):
        return out.get(label, {}).get("returncode")
    checks = {
        "ac7_anonymous_unblock_refused_while_live":
            rc("ac7-unblock-no-credential-while-live") == 5,
        "handoff_by_current_master_with_its_token": rc("handoff-A-to-B") == 0,
        "successor_epoch_is_succession": facts["epochs"][:2] == [
            (1, "A", "INITIAL"), (2, "B", "SUCCESSION")],
        "successor_closed": b.get("output", {}).get("halt") == "CLOSED",
        "old_master_superseded": a.get("returncode") == 6
                                 and a.get("output", {}).get("halt") == "SUPERSEDED",
        "old_master_wrote_nothing": _same_state(at_b, woke),
        "ac7_old_epoch_unblock_refused": rc("ac7-unblock-old-epoch") == 5,
        "ac7_old_epoch_record_action_refused": rc("ac7-record-action-old-epoch") == 5,
        "ac7_old_epoch_handoff_refused": rc("ac7-handoff-old-epoch") == 5,
        "ac7_nothing_changed": _same_state(woke, ac7),
        "operator_command_possible_without_live_master":
            rc("operator-without-live-master") == 0 and op is not None
            and op["epochs"][-1][2] == "OPERATOR" and len(op["state"].external_actions) == 1,
    }
    return _result(steps, facts, repo, checks, ["DS5", "DS6", "AC7"], d=d)


def derive_lapse(d: Path, work: Path) -> dict:
    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    out = _exits(steps)
    kinds = [s["kind"] for s in steps]
    checks = {
        "old_master_was_frozen": "signal-sigstop" in kinds and "signal-sigcont" in kinds,
        "recovery_after_lapse": facts["epochs"][:2] == [(1, "A", "INITIAL"),
                                                         (2, "B", "RECOVERY")],
        "lapse_named_in_log": any(e.get("event") == "acquire" and "expired"
                                  in str(e.get("previous", "")) for e in facts["lease_log"]),
        "successor_closed": out.get("drive-B", {}).get("output", {}).get("halt") == "CLOSED",
        "old_master_superseded": out.get("drive-A", {}).get("returncode") == 6,
        "old_master_wrote_nothing": _same_state(_moment(d, "B"), _moment(d, "A-woke")),
    }
    return _result(steps, facts, repo, checks, ["DS6"], d=d)


def derive_quota(d: Path, work: Path) -> dict:
    from hoh.orchestrator import node_timer_key
    from hoh.timers import TimerKind, TimerStatus

    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    other_before, other_after = _moment(d, "fixtures", "other"), _moment(d, "ticks", "other")
    out = _exits(steps)
    starts = [s for s in steps if s["kind"] == "process-start"]
    ticks = [s for s in steps if s["kind"] == "process-exit"
             and str(s.get("label", "")).startswith("tick-")]
    first = out.get("drive-A", {}).get("output", {})
    n1 = [t for t in facts["timers"] if t.node_id == "n1"
          and t.program_epoch == facts["state"].program_generation]
    stale = [t for t in facts["timers"] if t.program_epoch == 0]
    beats = [t for t in facts["timers"] if t.kind is TimerKind.HEARTBEAT]
    recs = [r for t in ticks for r in t["output"].get("projects", [])]
    resumed = [r for r in recs if r.get("action") == "resumed"]
    drove = [r for r in recs if r.get("action") == "drove"]
    before_due = [r for r in (ticks[0]["output"].get("projects", []) if ticks else [])
                  if r.get("node") == "n1"]
    checks = {
        "usage_limit_parked_the_node": first.get("halt") == "BLOCKED_PROVIDER",
        "durable_timer_armed": len(n1) == 1,
        "not_resumed_before_due": bool(before_due)
                                  and all(r.get("verdict") == "NOT_DUE" for r in before_due),
        "resumed_exactly_once_by_a_tick": len(resumed) == 1
                                          and resumed[0].get("target") == PROJECT,
        "timer_consumed_once": len(n1) == 1 and n1[0].status is TimerStatus.FIRED,
        "new_governed_master_drove_to_closure": len(drove) == 1
                                                and drove[0].get("halt") == "CLOSED"
                                                and facts["state"].rc_closed(),
        "successor_epoch_is_recovery": [r for _, _, r in facts["epochs"]][:2]
                                       == ["INITIAL", "RECOVERY"],
        "stale_generation_refused": any(r.get("verdict") == "SUPERSEDED_EPOCH"
                                        and r.get("node") == "n2" for r in recs)
                                    and all(t.run_id == node_timer_key("n2") for t in stale)
                                    and bool(stale),
        "other_class_not_resumed": _same_state(other_before, other_after)
                                   and other_after is not None
                                   and other_after["state"].node("m1").blocked_kind
                                   == "collected_for_digest"
                                   and any(r.get("action") == "class_not_owned"
                                           and r.get("target") == "other" for r in recs),
        "other_timer_still_armed": other_after is not None
                                   and [t.status for t in other_after["timers"]]
                                   == [TimerStatus.ARMED],
        "ds4_closure_disarmed_the_heartbeat": len(beats) == 1
                                              and beats[0].status is TimerStatus.DISARMED
                                              and "closed" in beats[0].disarmed_reason,
        "no_manual_resume": bool(starts) and starts[0]["argv"][:2] == ["project", "drive"]
                            and all(s["argv"] == CONTINUE_ARGV for s in starts[1:]),
    }
    return _result(steps, facts, repo, checks, ["DS3", "DS4", "O211"], d=d)


def derive_moved_branch(d: Path, work: Path) -> dict:
    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    moved = next((s for s in steps if s["kind"] == "branch-moved"), {})
    a = _exits(steps).get("drive-A", {}).get("output", {})
    checks = {
        "branch_really_moved": bool(moved)
                               and moved.get("accepted") != moved.get("moved_to"),
        "refused_as_candidate_mismatch": a.get("halt") == "CANDIDATE_MISMATCH",
        "unverified_commit_never_landed": bool(moved)
                                          and _merged_count(repo, moved["moved_to"]) == 0,
        "node_blocked_not_merged": facts["state"].node("n1").lifecycle.value == "BLOCKED",
    }
    return _result(steps, facts, repo, checks, ["ACCEPTED-IDENTITY"], landed=[], d=d)


def derive_quota_crash(d: Path, work: Path) -> dict:
    from hoh.timers import TimerStatus

    cases = {}
    for where in CRASH_POINTS:
        c = d / where
        steps, facts, repo = _steps(c), _project_facts(c), _repo_facts(c, work)
        m = _moment(c, "after-crash")
        node = m["state"].node("n1") if m else None
        armed = [t for t in (m["timers"] if m else []) if t.node_id == "n1"
                 and t.status is TimerStatus.ARMED]
        first = [s for s in steps if s["kind"] == "process-exit" and s["label"] == "drive-A"]
        ticks = [s for s in steps if s["kind"] == "process-exit"
                 and str(s.get("label", "")).startswith("tick-")]
        first_tick = [r for r in (ticks[0]["output"].get("projects", []) if ticks else [])
                      if r.get("node") == "n1"]
        expected = {"before-arm": ("RUNNING", 0), "after-arm": ("RUNNING", 1),
                    "after-park": ("BLOCKED", 1)}[where]
        checks = {
            "killed_by_sigkill": bool(first) and first[0]["returncode"] == -9,
            "durable_moment_is_the_expected_one": node is not None
                                                  and (node.lifecycle.value, len(armed))
                                                  == expected,
            "invariant_parked_implies_armed_timer": node is not None and not (
                node.lifecycle.value == "BLOCKED" and node.blocked_kind == "usage_limit"
                and not armed),
            "closed_by_ticks_alone": facts["state"].rc_closed(),
            "no_armed_timer_left": all(t.status is not TimerStatus.ARMED
                                       for t in facts["timers"]),
        }
        if where == "after-arm":
            checks["orphan_timer_did_not_resume_a_running_node"] = bool(first_tick) and all(
                r.get("action") != "resumed" for r in first_tick) and any(
                r.get("verdict") == "ALREADY_RUNNING" for r in first_tick)
        cases[where] = _result(steps, facts, repo, checks, [], d=c)
    flat = {f"{w}:{k}": v for w, c in cases.items() for k, v in c["checks"].items()}
    merged = {k: [f"{w}: {p}" for w, c in cases.items() for p in c["problems"][k]]
              for k in ("exactly_once", "continuity", "no_human")}
    return {"stressors": ["DS3-CRASH"], "checks": flat, "problems": merged,
            "harness_initiated": [f"{w}: {h}" for w, c in cases.items()
                                  for h in c["harness_initiated"]],
            "passed": all(c["passed"] for c in cases.values())}


def _marker_names_child(d: Path, child_pid) -> bool:
    """The process marker preserved while the run was in flight names the
    orphaned child as running -- not some other process, not an ended run."""
    if child_pid is None:
        return False
    f = (d / "moments" / "while-in-flight" / "projects" / PROJECT / "fixture-runs" / "n1"
         / "process.json")
    try:
        marker = json.loads(f.read_text())
    except (OSError, ValueError):
        return False
    return (isinstance(marker, dict) and marker.get("state") == "running"
            and marker.get("pid") == child_pid)


def derive_orphaned_run(d: Path, work: Path) -> dict:
    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    kills = [s for s in steps if s["kind"] == "sigkill"
             and s.get("point") == "between-iterations:n1"]
    orphan = next((s for s in steps if s["kind"] == "orphan"), {})
    out = _exits(steps)
    first = out.get("tick-01", {}).get("output", {}).get("projects", [])
    during = _moment(d, "while-in-flight")
    recs = [r for s in steps if s["kind"] == "process-exit"
            and str(s.get("label", "")).startswith("tick-")
            for r in s["output"].get("projects", [])]
    recovered = [r for r in recs if r.get("action") == "recovered"]
    checks = {
        "master_killed_while_child_ran": len(kills) == 1 and kills[0]["returncode"] == -9
                                         and bool(orphan)
                                         and orphan.get("child_pid") != orphan.get("master_pid"),
        "first_tick_waits_on_the_run": any(r.get("action") == "waiting_on_run" for r in first),
        "no_epoch_spent_while_in_flight": during is not None and len(during["epochs"]) == 1,
        "recovered_after_the_run_finished": len(recovered) == 1
                                            and recovered[0].get("halt") == "CLOSED",
        "epochs_initial_then_one_recovery": [r for _, _, r in facts["epochs"]]
                                            == ["INITIAL", "RECOVERY"],
        "marker_names_the_orphaned_child": _marker_names_child(d, orphan.get("child_pid")),
    }
    return _result(steps, facts, repo, checks, ["DS1-ORPHAN"], d=d)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def derive_reboot(d: Path, work: Path) -> dict:
    steps, facts, repo = _steps(d), _project_facts(d), _repo_facts(d, work)
    arms = [s for s in steps if s["kind"] == "reboot-arm"]
    vers = [s for s in steps if s["kind"] == "reboot-verify"]
    arm, ver = (arms or [{}])[0], (vers or [{}])[0]
    ticks = [s for s in steps if s["kind"] == "process-exit"
             and str(s.get("label", "")).startswith("tick-")]
    recovered = [r for t in ticks for r in t["output"].get("projects", [])
                 if r.get("action") == "recovered"]
    armed, meta = _read_json(d / "ARMED.json"), _read_json(d.parent / "meta.json")
    checks = {
        # One arm and one verify: a second verify would append ticks to a
        # finished record, and only the first is read below.
        "armed_and_verified_once": len(arms) == 1 and len(vers) == 1,
        # The restart is evidence about one commit's code: the arm must have run
        # from the very commit the other scenarios ran from, on a clean tree, and
        # the verify with that commit's own harness, fixture, unit and src/hoh
        # (the archive that actually ran after the restart, not only the file
        # that started the command). The arm's record and the steps are the
        # harness's own statements, at the trust level of meta.json.
        "armed_at_the_evidence_commit": bool(armed.get("head"))
                                        and armed.get("head") == meta.get("head")
                                        and armed.get("dirty") is False
                                        and str(armed.get("code", "")).startswith("git archive "),
        "verified_with_the_armed_harness": ver.get("tool_matches_armed_head") is True
                                           and ver.get("code_matches_armed_head") is True,
        "boot_changed": bool(arm.get("boot_id")) and bool(ver.get("boot_id"))
                        and arm["boot_id"] != ver["boot_id"],
        "recovered_after_reboot": [r for _, _, r in facts["epochs"]][:2]
                                  == ["INITIAL", "RECOVERY"],
        "restart_named_in_log": any("restarted" in str(e.get("previous", ""))
                                    for e in facts["lease_log"]),
        "closed_by_the_canonical_tick": bool(recovered)
                                        and recovered[-1].get("halt") == "CLOSED",
    }
    return _result(steps, facts, repo, checks, ["DS2"], d=d)


#: Which scenarios each registered exit condition rests on. Written down, not
#: inferred: a reader can disagree with one line without re-reading the tool.
REQUIREMENTS = {
    # Crash AND reboot recovery. Standalone: a record without the operator's
    # restart still speaks for the five exit conditions, so this row is read by
    # its own key and is not part of the record's top-level conjunction.
    "P2-11": {"item": "P2.build-verify.crash-reboot-recovery",
              "needs": ["controller-kill", "orphaned-run", "reboot"],
              "standalone": True,
              "note": "crash recovery is DS1 (external SIGKILL, automatic replacement) and "
                      "DS1-ORPHAN; reboot recovery is DS2, an operator-run restart"},
    # The umbrella line: the four lines under it are its content, so it holds
    # only when they all hold (and_requirements), evaluated after them.
    "P2-33": {"item": "P2.exit-includes.unattended-test",
              "needs": ["controller-kill", "quota"],
              "and_requirements": ["P2-34", "P2-35", "P2-36", "P2-37"],
              "note": "rests on the product scenarios passing and on harness "
                      "preconditions (only the first master started by hand; no manual "
                      "resume) that no control can break"},
    "P2-34": {"item": "P2.exit-includes.no-human-attached",
              "needs": list(SCENARIOS), "only": "no_human",
              "note": "the decision/external-action half is a product property (control "
                      "human-decision); the stdin/session half measures how the harness "
                      "started processes and is a precondition"},
    "P2-35": {"item": "P2.exit-includes.controller-session-replacement",
              "needs": ["controller-kill", "rollover", "lapse", "orphaned-run"]},
    "P2-36": {"item": "P2.exit-includes.automatic-continuation",
              "needs": ["quota", "quota-crash", "controller-kill"]},
    "P2-37": {"item": "P2.exit-includes.no-lost-state",
              "needs": list(SCENARIOS), "only": "exactly_once+continuity",
              "and_pass": ["moved-branch", "quota-crash"]},
}


def _controls(evidence: Path, meta: dict, work: Path) -> tuple[dict, bool]:
    ran = meta.get("negative_controls") or {}
    out = {}
    for c in CONTROLS:
        key = f"{c['name']}@{c['scenario']}"
        status = ran.get(key)
        entry = {"targets": c["targets"], "status": status or "NOT_RUN"}
        if status != "ran":
            entry.update(caught=False, failed_checks=[])
        else:
            try:
                r = globals()[f"derive_{c['scenario'].replace('-', '_')}"](
                    evidence / "negative-control" / key / c["scenario"], work)
                failed = sorted(k for k, v in r["checks"].items() if not v)
                failed += sorted(f"problems.{k}" for k, v in r.get("problems", {}).items()
                                 if v)

                def hit(target):
                    if target.startswith("problems."):
                        return bool(r.get("problems", {}).get(target[9:]))
                    return r["checks"].get(target) is False
                broken_pre = sorted(k for k, v in r["checks"].items()
                                    if not v and k.split(":", 1)[-1] in PRECONDITIONS)
                entry.update(failed_checks=failed, broken_preconditions=broken_pre,
                             # A control whose scenario did not even happen as
                             # set up has shown nothing about its target -- unless
                             # the control exists to make it stall (review B4).
                             caught=bool(c["targets"])
                             and all(hit(tg) for tg in c["targets"])
                             and (not broken_pre or c.get("allow_stall", False)))
            except Exception as exc:                  # noqa: BLE001 - recorded
                entry.update(caught=False, failed_checks=[
                    "derive error: " + _sanitize(str(exc), work, evidence)[:200]])
        out[key] = entry
    return out, bool(out) and all(e["caught"] for e in out.values())


def _evaluate_requirements(scenarios: dict, meta: dict, instrument_ok: bool,
                           meta_problems: list[str]) -> dict:
    """One row per REQUIREMENTS entry, from the derived scenario results.

    Requirements without `and_requirements` first, each from its own scenarios;
    then those with it, which hold only if their own scenarios do *and* every
    listed requirement is satisfied. A listed id that is not a requirement
    fails closed."""
    reqs = {}
    order = sorted(REQUIREMENTS, key=lambda rid: bool(REQUIREMENTS[rid].get("and_requirements")))
    for rid in order:
        spec = REQUIREMENTS[rid]
        rows = [scenarios.get(n, {"status": "NOT_RUN"}) for n in spec["needs"]]
        ran = all(r.get("status") in ("PASS", "FAIL") and "problems" in r for r in rows)
        if spec.get("only") == "no_human":
            ok = ran and all(not r["problems"]["no_human"] for r in rows)
        elif spec.get("only") == "exactly_once+continuity":
            ok = ran and all(not r["problems"]["exactly_once"]
                             and not r["problems"]["continuity"] for r in rows) and all(
                scenarios.get(n, {}).get("status") == "PASS"
                for n in spec.get("and_pass", []))
        else:
            ok = all(r.get("status") == "PASS" for r in rows)
        deps = list(spec.get("and_requirements", []))
        # Only requirements without dependencies of their own are evaluated
        # before this one; anything else is not (yet) a satisfied row here.
        deps_ok = all(reqs.get(dep, {}).get("satisfied") is True for dep in deps)
        undisc = {n: {"checks": scenarios.get(n, {}).get("undiscriminated_checks", []),
                      "problems": scenarios.get(n, {}).get("undiscriminated_problems", [])}
                  for n in spec["needs"]}
        reqs[rid] = {"authoritative_item": spec["item"], "scenarios": spec["needs"],
                     "and_requirements": deps,
                     "note": spec.get("note", ""),
                     # Measured but not shown falsifiable by any targeted control:
                     # stated with the requirement, not only per scenario (B4).
                     "undiscriminated": undisc,
                     "satisfied": bool(ok) and deps_ok and instrument_ok and not meta_problems
                                  and not meta.get("harness_failures")
                                  and meta.get("dirty") is False
                                  and meta.get("dirty_at_end") is False}
    return {rid: reqs[rid] for rid in REQUIREMENTS}


def _exit_conditions_satisfied(reqs: dict) -> bool:
    """The record's top-level flag: every requirement except the standalone
    ones (P2-11 needs an operator's restart; a record without it still speaks
    for the five exit conditions, each of which is read by its own key)."""
    return all(r["satisfied"] for rid, r in reqs.items()
               if not REQUIREMENTS[rid].get("standalone"))


def derive(evidence: Path) -> dict:
    meta = json.loads((evidence / "meta.json").read_text())
    # Judge against the command that actually ran (it came from the code the
    # run used), not against whatever the working tree's unit says today.
    global CONTINUE_ARGV
    CONTINUE_ARGV = meta.get("continue_argv") or _continue_argv()
    meta_problems = []
    unit_at_head = subprocess.run(
        ["git", "-C", str(HOH), "show", f"{meta.get('head')}:ops/hoh-continue.service"],
        capture_output=True, text=True)
    if unit_at_head.returncode == 0:
        for line in unit_at_head.stdout.splitlines():
            if line.startswith("ExecStart=") and \
                    shlex.split(line.split("=", 1)[1])[1:] != CONTINUE_ARGV:
                meta_problems.append("the recorded tick command is not the evidence "
                                     "commit's continuation unit")
    else:
        meta_problems.append("the evidence commit's continuation unit cannot be read")
    with tempfile.TemporaryDirectory(prefix="hoh-durability-derive-") as tmp:
        work = Path(tmp)
        scenarios = {}
        for name in SCENARIOS + ("reboot",):
            d = evidence / name
            if not d.is_dir():
                scenarios[name] = {"status": "NOT_RUN"}
                continue
            try:
                r = globals()[f"derive_{name.replace('-', '_')}"](d, work)
                r["status"] = "PASS" if r["passed"] else "FAIL"
            except Exception as exc:                          # noqa: BLE001 - recorded
                # Paths out of the message: the record must not depend on where
                # the evidence lives or which temp directory derive used.
                msg = _sanitize(str(exc), work, evidence)
                r = {"status": "FAIL", "passed": False,
                     "error": f"{type(exc).__name__}: {msg}"[:400]}
            scenarios[name] = r
        controls, instrument_ok = _controls(evidence, meta, work)
    for name, r in scenarios.items():
        # Discriminated means: a control *targeting* it was caught. A check
        # that only ever fails as collateral damage of a stalled scenario has
        # not been shown to detect anything (review B3, A).
        targeted = {tg for c in CONTROLS if c["scenario"] == name
                    and controls.get(f"{c['name']}@{name}", {}).get("caught")
                    for tg in c["targets"]}
        if "checks" in r:
            def base(k):
                return k.split(":", 1)[-1]
            r["preconditions"] = sorted(k for k in r["checks"] if base(k) in PRECONDITIONS)
            r["undiscriminated_checks"] = sorted(
                k for k in r["checks"] if base(k) not in PRECONDITIONS and k not in targeted)
            r["undiscriminated_problems"] = sorted(
                p for p in ("exactly_once", "continuity", "no_human")
                if f"problems.{p}" not in targeted)
    reqs = _evaluate_requirements(scenarios, meta, instrument_ok, meta_problems)
    stressors = {}
    for r in scenarios.values():
        for s in r.get("stressors", []):
            # A stressor is PASS only when the instrument that measured it has
            # shown it can fail (review B3, E).
            stressors[s] = ("PASS" if r["status"] == "PASS" and instrument_ok
                            else "UNPROVEN" if r["status"] == "PASS" else r["status"])
    stressors.setdefault("DS2", scenarios["reboot"]["status"])
    for c in CONTROLS:
        controls.get(f"{c['name']}@{c['scenario']}", {}).setdefault("scenario", c["scenario"])
    return {
        "what": "P2 exit conditions under faults: governed masters, SIGKILL/SIGSTOP, "
                "rollover, stale epochs, usage-limit timers, derived from raw evidence",
        "record_version": RECORD_VERSION,
        "evidence_head": meta.get("head"),
        # Anything but an explicit False is dirty: a record without the key is
        # not evidence of a clean tree (review B2).
        "evidence_dirty": not (meta.get("dirty") is False
                               and meta.get("dirty_at_end") is False),
        "agent_execution": meta.get("agent_execution"), "tick": meta.get("tick"),
        "harness_failures": meta.get("harness_failures"),
        "meta_problems": meta_problems,
        "stressors": dict(sorted(stressors.items())),
        "scenarios": scenarios,
        "negative_controls": controls,
        "instrument_falsifiable": instrument_ok,
        "requirements": reqs,
        "requirement_satisfied": _exit_conditions_satisfied(reqs),
    }


def cmd_derive(args) -> int:
    record = derive(Path(args.evidence))
    text = json.dumps(record, indent=2, sort_keys=True, default=str) + "\n"
    out = Path(args.out)
    if args.check:
        same = out.is_file() and out.read_text() == text
        print("record re-derives from its evidence" if same
              else "record does NOT equal its derivation")
        return 0 if same else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out.rename(out.with_name(f"{out.name}.v{stamp}"))
    out.write_text(text)
    print(json.dumps({k: record[k] for k in ("stressors", "instrument_falsifiable",
                                             "requirement_satisfied")}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--evidence", required=True)
    r.add_argument("--base", default=None, help="scratch directory for repos and roots")
    r.add_argument("--only", nargs="*", choices=SCENARIOS)
    r.add_argument("--live", action="store_true",
                   help="run the working tree instead of a git archive of HEAD "
                        "(development only: such evidence satisfies nothing)")
    r.set_defaults(func=cmd_run)
    d = sub.add_parser("derive")
    d.add_argument("--evidence", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--check", action="store_true")
    d.set_defaults(func=cmd_derive)
    ra = sub.add_parser("reboot-arm")
    ra.add_argument("--evidence", required=True)
    ra.add_argument("--base", default=None)
    ra.add_argument("--live", action="store_true")
    ra.set_defaults(func=cmd_reboot_arm)
    rv = sub.add_parser("reboot-verify")
    rv.add_argument("--evidence", required=True)
    rv.set_defaults(func=cmd_reboot_verify)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
