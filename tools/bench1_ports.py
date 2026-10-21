#!/usr/bin/env python3
"""bench1_ports.py -- the production ports of the BENCH1 runner (`tools/bench1.py`).

`tools/bench1.py` decides; this file touches the world, always through an object a test can replace:

* **storage** -- `StorageGuard` (3 000 000 free inodes, 92 % bytes) before every pull and every cell, and a
  per-pull inode measurement that refuses the next pull when the margin would not survive it;
* **images** -- `GuardedDocker` (pull one listed ref, inspect, export `/testbed`, run the task's tests through the
  pinned `bench-test` vector; release stays `bench1.release_images`), `prepare_snapshot`, the `bench-test` script;
* **the entry gate and the grader** -- `parse_test_results`, `arms_path_counts`, `entry_gate_for_task` (gold and
  base, twice, through the arms' path and the official harness), `OfficialGrader` (pinned argv, no cleanup);
* **arm B** -- `BArmPorts`: a real `hoh worktree` / `hoh start --routing-policy` / `hoh run` with the registered
  flags, read back from the pinned routing record, the run state, the policy file the product will load and the
  product's own parsed command line, then a post-start read-back before the first provider call can matter;
* **arms A0 / A1+** -- `AArmPorts` and `arm_worker_main`: the plain agent in a child process of its own, a
  handshake (the worker writes what its built dispatcher says, the parent verifies and releases it), the registered
  prompts and the reviewer loop of `bench1.run_a1plus`;
* **transcripts, patches, deadlines** -- `locate_transcripts`, `filter_patch`, the I14 reader (the parsed command
  line of the process for B, the built dispatcher for A).

Nothing here is executed by the tests against a real runtime, Herdr, a provider or the network: every external
call is an injected runner, popen, clock or dispatcher factory. Readers whose record shape the pinned product does
not publish are marked ASSUMED in their docstrings and fail closed: a missing read-back is a finding, which makes
the cell an INSTRUMENT_FAILURE, never a pass.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path


def _load_tool():
    """The tool (`tools/bench1.py`), shared with whoever loaded this file (one set of classes and exceptions)."""
    mod = sys.modules.get("bench1")
    if mod is not None and hasattr(mod, "run_cell"):
        return mod
    path = Path(__file__).resolve().with_name("bench1.py")
    spec = importlib.util.spec_from_file_location("bench1", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench1"] = mod
    spec.loader.exec_module(mod)
    return mod


b1 = _load_tool()

MIN_FREE_INODES = 3_000_000
MAX_USED_FRACTION = 0.92
#: A pull is refused when the free inodes minus this multiple of the largest inode cost seen so far would fall
#: below the floor (an unpacked image is tens of thousands of inodes; the campaign pulls them one at a time).
PROJECTION_FACTOR = 1.5
HANDSHAKE_WAIT_S = 120
POST_START_WAIT_S = 30
ROLE_TIMEOUT_S = b1.ROLE_TIMEOUT_S

# --------------------------------------------------------------------------- #
# Storage guard
# --------------------------------------------------------------------------- #


class StorageStop(b1.Bench1Error):
    """AB6: the storage guard says stop (free inodes below the floor, or bytes above the ceiling)."""


@dataclasses.dataclass
class StorageGuard:
    """Read-only (`statvfs`) guard over one or more paths; `check` raises `StorageStop`, never changes anything."""

    paths: tuple[str, ...]
    min_free_inodes: int = MIN_FREE_INODES
    max_used_fraction: float = MAX_USED_FRACTION
    statvfs: Callable = os.statvfs

    def readings(self) -> list[dict]:
        out = []
        for p in self.paths:
            st = self.statvfs(p)
            used = 1 - (st.f_bavail / st.f_blocks) if st.f_blocks else 1.0
            out.append({"path": p, "free_inodes": st.f_favail, "used_fraction": round(used, 4)})
        return out

    def check(self, extra_inodes: int = 0) -> list[dict]:
        rows = self.readings()
        problems = []
        for r in rows:
            if r["free_inodes"] - extra_inodes < self.min_free_inodes:
                problems.append(f"{r['path']}: free inodes {r['free_inodes']} (less {extra_inodes} projected) "
                                f"below {self.min_free_inodes}")
            if r["used_fraction"] > self.max_used_fraction:
                problems.append(f"{r['path']}: bytes used {r['used_fraction']:.1%} above "
                                f"{self.max_used_fraction:.0%}")
        if problems:
            raise StorageStop("; ".join(problems))
        return rows


# --------------------------------------------------------------------------- #
# Container runtime, images, snapshots
# --------------------------------------------------------------------------- #


def export_argv(image: str, uid: int, gid: int) -> list[str]:
    """The streaming export of an image's own `/testbed` (an ephemeral container, no mounts, network off). The
    image's own `/testbed/.git` is **not** exported: `make_snapshot` builds the one-commit snapshot itself and
    refuses a top-level `.git` (the history of the base commit is not part of what an arm sees)."""
    if not b1._REF.fullmatch(image):
        raise b1.Bench1Error("export: not an image reference")
    argv = ["docker", "run", b1._EPHEMERAL, "--network", "none", "--user", f"{uid}:{gid}", image,
            "tar", "-C", "/testbed", "--exclude=./.git", "-cf", "-", "."]
    if not b1.docker_argv_allowed(argv):
        raise b1.Bench1Error("export vector is not on the allow-list")
    return argv


def load_deltas(delta_dir: Path) -> list[int]:
    """The inode costs of the pulls recorded in `delta_dir` (create-only files); a record that is not
    `{"inodes": non-negative int}` makes the projection unknowable and is refused."""
    out: list[int] = []
    delta_dir = Path(delta_dir)
    if not delta_dir.is_dir():
        return out
    for f in sorted(delta_dir.glob("*.json")):
        n = b1.read_json_object(f, single_link=True).get("inodes")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise b1.Bench1Error("a pull record has no inode count")
        out.append(n)
    return out


class GuardedDocker(b1.SubprocessDocker):
    """`SubprocessDocker` with the storage guard before every pull and a measurement of what each pull cost."""

    def __init__(self, guard: StorageGuard, runner: Callable | None = None,
                 uid: int | None = None, gid: int | None = None, delta_dir: Path | None = None):
        super().__init__(runner=runner)
        self.guard = guard
        self.delta_dir = Path(delta_dir) if delta_dir is not None else None
        self.deltas: list[int] = load_deltas(self.delta_dir) if self.delta_dir is not None else []
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid

    def pull(self, ref: str) -> None:
        projected = int(PROJECTION_FACTOR * max(self.deltas, default=0))
        before = self.guard.check(extra_inodes=projected)[0]["free_inodes"]
        super().pull(ref)
        after = self.guard.readings()[0]["free_inodes"]
        delta = max(0, before - after)
        self.deltas.append(delta)
        if self.delta_dir is not None:                    # one create-only record per pull: the next process
            self.delta_dir.mkdir(parents=True, exist_ok=True)   # (a fresh CLI call) projects from it
            b1.write_create_only(self.delta_dir / f"{time.time_ns()}.json", {"ref": ref, "inodes": delta})

    def export_testbed(self, image: str, dest: Path, scratch_dir: Path) -> int:
        """Stream the image's `/testbed` into a tar inside `scratch_dir`, extract it into `dest` (new, empty);
        refuses members that are absolute, contain `..`, or are devices/FIFOs; returns the member count."""
        self.guard.check()                                # the export unpacks tens of thousands of files
        argv = export_argv(image, self.uid, self.gid)
        tar_path = Path(scratch_dir) / "testbed.tar"
        with open(tar_path, "xb") as out:
            done = self.runner(argv, stdout=out, stderr=subprocess.PIPE, timeout=3600, check=False)
        if done.returncode != 0:
            raise b1.Bench1Error("testbed export failed")
        Path(dest).mkdir(parents=True, exist_ok=False)
        count = 0
        with tarfile.open(tar_path) as tar:
            for m in tar:
                name = m.name
                if name.startswith("/") or ".." in Path(name).parts or m.isdev() or m.isfifo():
                    raise b1.Bench1Error("testbed export holds an unsafe member")
                count += 1
            try:
                tar.extractall(dest, filter="data")          # also refuses a link that leaves `dest`
            except tarfile.TarError as exc:
                raise b1.Bench1Error(f"testbed export holds an unsafe member ({type(exc).__name__})") from exc
        return count


def prepare_snapshot(docker: GuardedDocker, image: str, task_dir: Path, tmp_root: Path) -> dict:
    """The task's single-commit snapshot from the image's own `/testbed` (build artefacts included), and its
    I17 report. The export directory is a self-releasing temporary directory."""
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix="bench1-export-") as t:
        src = Path(t) / "src"
        members = docker.export_testbed(image, src, Path(t))
        commit = b1.make_snapshot(src, task_dir / "snap")
    return {"commit": commit, "members": members, "report": b1.snapshot_report(task_dir / "snap")}


_BENCH_TEST_TEMPLATE = '''#!/usr/bin/env python3
"""bench-test: run a command in the task's own image (generated by tools/bench1_ports.py; do not edit)."""
import importlib.util
import os
import shlex
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("bench1", {tool!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["bench1"] = mod
spec.loader.exec_module(mod)
argv = mod.bench_test_argv({image!r}, Path.cwd(), " ".join(shlex.quote(a) for a in sys.argv[1:]))
os.execvp(argv[0], argv)
'''


def bench_test_script(image: str, tool_path: Path) -> str:
    if not b1._REF.fullmatch(image):
        raise b1.Bench1Error("bench-test: not an image reference")
    return _BENCH_TEST_TEMPLATE.format(tool=str(Path(tool_path).resolve()), image=image)


def write_bench_test(task_dir: Path, image: str, tool_path: Path) -> Path:
    path = Path(task_dir) / "bench-test"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
    try:
        os.write(fd, bench_test_script(image, tool_path).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def image_ref(instance_id: str) -> str:
    """The official per-instance image reference, `swebench/sweb.eval.x86_64.<owner>_1776_<rest>:latest`."""
    if "__" not in instance_id:
        raise b1.Bench1Error("not a SWE-bench instance id")
    owner, rest = instance_id.split("__", 1)
    ref = f"swebench/sweb.eval.x86_64.{owner}_1776_{rest}:latest".lower()
    if not b1._REF.fullmatch(ref):
        raise b1.Bench1Error("instance id does not make an image reference")
    return ref


def stage_tasks(sample: dict, stage: str) -> list[dict]:
    """The tasks of a stage from the sample file: tuning rounds 1 and 2 (T), or the four of stage 0."""
    if stage == "T":
        rows = list(sample.get("round1", [])) + list(sample.get("round2", []))
    elif stage == "0":
        rows = list(sample.get("stage0", []))
    else:
        raise b1.Bench1Error("only stages T and 0 are prepared here")
    if not rows:
        raise b1.Bench1Error("the sample file has no task for this stage")
    return rows


# --------------------------------------------------------------------------- #
# Test output parsers, the arms' path and the entry gate
# --------------------------------------------------------------------------- #

_PYTEST = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)")
_DJANGO = re.compile(r"^(\S+ \([\w.]+\))(?:\n.*)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
_DJANGO_ONE = re.compile(r"^(\S+ \([\w.]+\)).*? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
_SYMPY = re.compile(r"^(test_\w+)\s+(ok|F|E|f|s)\b")


def parse_test_results(repo_slug: str, output: str) -> dict[str, str]:
    """test id -> `PASSED` | `FAILED` for the three repositories of the pool. ASSUMED output formats (pytest
    `-rA`, Django verbose runner, sympy `bin/test --verbose`); a test the output does not mention is absent,
    and `arms_path_counts` counts an absent test as failed."""
    results: dict[str, str] = {}
    for line in output.splitlines():
        line = line.rstrip()
        if repo_slug == "pytest-dev/pytest":
            m = _PYTEST.match(line)
            if m:
                results[m.group(2)] = "PASSED" if m.group(1) in ("PASSED", "XFAIL") else "FAILED"
        elif repo_slug == "django/django":
            m = _DJANGO_ONE.match(line)
            if m:
                results[m.group(1)] = "PASSED" if m.group(2) in ("ok", "expected failure") \
                    or m.group(2).startswith("skipped") else "FAILED"
        elif repo_slug == "sympy/sympy":
            m = _SYMPY.match(line)
            if m:
                results[m.group(1)] = "PASSED" if m.group(2) in ("ok", "f", "s") else "FAILED"
        else:
            raise b1.Bench1Error(f"no output parser for {repo_slug!r}")
    return results


def arms_path_counts(repo_slug: str, output: str, f2p: list[str], p2p: list[str]) -> dict:
    """The entry gate's per-path result `{"f2p_failed", "p2p_failed"}`; an id the output does not show failed."""
    res = parse_test_results(repo_slug, output)
    return {"f2p_failed": sum(1 for t in f2p if res.get(t) != "PASSED"),
            "p2p_failed": sum(1 for t in p2p if res.get(t) != "PASSED")}


def entry_gate_for_task(arms_path_run: Callable[[str | None], dict], official_run: Callable[[str | None], dict],
                        gold_patch: str, repeats: int = 2) -> dict:
    """Gold and base, `repeats` times each, through both paths; the verdict is `bench1.entry_gate`'s. A result
    that is not a dict of two non-negative integers makes the whole gate refuse (never a pass)."""
    def ok(r) -> bool:
        return isinstance(r, dict) and all(isinstance(r.get(k), int) and not isinstance(r.get(k), bool)
                                           and r[k] >= 0 for k in ("f2p_failed", "p2p_failed"))
    lists: dict[str, list[dict]] = {"gold_arms": [], "gold_official": [], "base_arms": [], "base_official": []}
    for _ in range(repeats):
        lists["gold_arms"].append(arms_path_run(gold_patch))
        lists["gold_official"].append(official_run(gold_patch))
        lists["base_arms"].append(arms_path_run(None))
        lists["base_official"].append(official_run(None))
    bad = [k for k, v in lists.items() if not all(ok(r) for r in v)]
    if bad:
        return {"holds": False, "reasons": [f"{k}: a result is not two counts" for k in bad], "lists": lists}
    verdict = b1.entry_gate(lists["gold_arms"], lists["gold_official"], lists["base_arms"],
                            lists["base_official"])
    return {**verdict, "lists": lists}


class OfficialGrader:
    """The official evaluation of one patch, argument vector pinned: one worker, instance cache level, **no
    cleanup**, the `swebench` image namespace. Reads the per-instance report the harness writes.

    ASSUMED (the harness is not installed where this was written): the flag names and the report layout
    (`tests_status.FAIL_TO_PASS.failure`). A report that cannot be read raises; the entry gate then refuses."""

    DATASET = "princeton-nlp/SWE-bench_Verified"

    def __init__(self, runner: Callable | None = None, python: str = sys.executable,
                 agent_roots: Iterable[Path] = ()):
        self.runner = runner or subprocess.run
        self.python = python
        self.agent_roots = tuple(Path(r) for r in agent_roots)

    def argv(self, predictions: Path, task: str, run_id: str) -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", task) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id):
            raise b1.Bench1Error("grader: task or run id is not a plain name")
        return [self.python, "-m", "swebench.harness.run_evaluation", "--dataset_name", self.DATASET,
                "--predictions_path", str(predictions), "--instance_ids", task, "--max_workers", "1",
                "--run_id", run_id, "--cache_level", "instance", "--clean", "False",
                "--namespace", "swebench"]

    def cache_env(self, scratch: Path) -> dict[str, str]:
        """The environment of the harness: every cache the dataset library or the harness may write -- which
        will hold the gold and test patches of **every** instance of the dataset -- is a directory below the
        grading scratch (self-releasing, outside every agent-visible path), never `~/.cache`."""
        cache = Path(scratch) / "cache"
        env = {**os.environ, "XDG_CACHE_HOME": str(cache / "xdg"), "HF_HOME": str(cache / "hf"),
               "HF_DATASETS_CACHE": str(cache / "hf" / "datasets"), "HF_HUB_CACHE": str(cache / "hf" / "hub"),
               "HUGGINGFACE_HUB_CACHE": str(cache / "hf" / "hub"), "TRANSFORMERS_CACHE": str(cache / "hf" / "tf")}
        return env

    def evaluate(self, task: str, patch_path: Path, row: dict, scratch: Path) -> dict:
        scratch = Path(scratch)
        roots = list(self.agent_roots)
        if patch_path is not None and Path(patch_path).resolve().parent != scratch.resolve():
            roots.append(Path(patch_path).parent)         # a cell's patch lives in the cell's own directory
            #   (the entry gate's own patches are written straight into the scratch, which is not a cell)
        assert_outside(scratch, roots)
        pred = scratch / "predictions.jsonl"
        text = b1.read_regular_bytes(Path(patch_path)).decode("utf-8", errors="replace") \
            if patch_path is not None else ""
        pred.write_text(json.dumps({"instance_id": task, "model_name_or_path": "bench1", "model_patch": text})
                        + "\n", encoding="utf-8")
        run_id = f"bench1-{hashlib.sha256((task + b1.utcstamp() + str(time.time_ns())).encode()).hexdigest()[:12]}"
        done = self.runner(self.argv(pred, task, run_id), cwd=str(scratch), capture_output=True, text=True,
                           timeout=7200, check=False, env=self.cache_env(scratch))
        reports = sorted(scratch.glob(f"logs/run_evaluation/{run_id}/*/{task}/report.json"))
        if done.returncode != 0 or not reports:
            raise b1.Bench1Error("official evaluation produced no report")
        return parse_official_report(b1.read_json_object(reports[0]), task)


def assert_outside(scratch: Path, agent_roots: Iterable[Path]) -> None:
    """The hidden-data scratch is neither inside nor around any path an arm works in (its cell, its worktree,
    the campaign's work, roots, store and temp directories, the transcripts directory). Resolved paths."""
    s = Path(scratch).resolve()
    for r in agent_roots:
        a = Path(r).resolve()
        if s == a or s.is_relative_to(a) or a.is_relative_to(s):
            raise b1.Bench1Error("the grading scratch overlaps a path an arm can read")


def agent_visible_roots(paths: CampaignPaths) -> tuple[Path, ...]:
    return (paths.work, paths.roots, paths.store, paths.tmp, paths.claude_projects)


def build_grader(agent_roots: Iterable[Path] = (), runner: Callable | None = None) -> OfficialGrader:
    """The one grading instrument: the entry gate, `grade` and `regrade` all take it from here."""
    return OfficialGrader(runner=runner, agent_roots=agent_roots)


def official_run(grader, task: str, row: dict, scratch: Path) -> Callable[[str | None], dict]:
    """`official_run(patch | None)` for `entry_gate_for_task`: the gold patch, or the base (no patch), graded by
    the same instrument that grades the cells."""
    def run(patch: str | None) -> dict:
        path = Path(scratch) / f"entry-{'gold' if patch else 'base'}-{time.time_ns()}.diff"
        path.write_text(patch or "", encoding="utf-8")
        out = grader.evaluate(task, path, row, Path(scratch))
        return {"f2p_failed": out["f2p_failed"], "p2p_failed": out["p2p_failed"]}
    return run


def parse_official_report(report: dict, task: str) -> dict:
    """`{"resolved", "f2p_failed", "p2p_failed"}` from a per-instance report; anything else raises."""
    inst = report.get(task)
    if not isinstance(inst, dict) or not isinstance(inst.get("resolved"), bool):
        raise b1.Bench1Error("official report has no verdict for the instance")
    status = inst.get("tests_status")
    counts = {}
    for key, name in (("FAIL_TO_PASS", "f2p_failed"), ("PASS_TO_PASS", "p2p_failed")):
        part = status.get(key) if isinstance(status, dict) else None
        fail = part.get("failure") if isinstance(part, dict) else None
        if not isinstance(fail, list):
            raise b1.Bench1Error("official report has no per-test status")
        counts[name] = len(fail)
    return {"resolved": inst["resolved"], **counts}


# --------------------------------------------------------------------------- #
# Liveness: the registry the dispatchers write
# --------------------------------------------------------------------------- #

OWNED_AGENTS_FILE = "owned-agents.jsonl"           # hoh.agentcontrol.OWNED_FILE: <run dir>/owned-agents.jsonl
_REGISTRY_EVENTS = frozenset({"surface", "agent", "adopt", "release"})


class OwnedAgentsPort:
    """`surface_list()` for one hoh root, from the **dispatchers' own registry** (`owned-agents.jsonl` of every
    run directory below the root, and of the root itself: an A arm's worker keeps its registry in its cell).
    `hoh surface list` reads only `owned-surfaces.jsonl` and is blind to the panes a dispatcher opened (reviewer
    A1); this port is asked beside it. The answer is the tool's documented shape; a file that is not a plain
    single-link regular file, a line that is not a registry event, a missing root or a root that is not a
    directory is a `problem` row, which makes `parse_surface_list` raise -- UNKNOWN is alive."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def surface_list(self) -> str:
        rows: list[dict] = []
        if not self.root.is_dir():
            return json.dumps({"surfaces": [{"problem": "the hoh root does not exist"}]})
        files = [self.root / OWNED_AGENTS_FILE]
        try:
            for d in sorted(self.root.iterdir()):
                if d.is_symlink():
                    rows.append({"problem": "a run directory is a symbolic link", "file": d.name})
                elif d.is_dir():
                    files.append(d / OWNED_AGENTS_FILE)
        except OSError:
            return json.dumps({"surfaces": [{"problem": "the hoh root cannot be listed"}]})
        for f in files:
            if not os.path.lexists(f):
                continue
            try:
                rows.extend(self._fold(f))
            except b1.Bench1Error as exc:
                rows.append({"problem": str(exc)[:120], "file": f.parent.name})
        return json.dumps({"surfaces": rows})

    @staticmethod
    def _fold(path: Path) -> list[dict]:
        text = b1.read_regular_bytes(path, single_link=True).decode("utf-8", errors="replace")
        panes: dict[tuple[str, str], str] = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                event, pane, run = rec["event"], str(rec["pane_id"]), str(rec["run_id"])
            except (ValueError, KeyError, TypeError):
                raise b1.Bench1Error("a registry line is not an event") from None
            if event not in _REGISTRY_EVENTS or not pane:
                raise b1.Bench1Error("a registry line has an unknown event")
            if event in ("surface", "adopt"):
                panes[(run, pane)] = "owned"
            elif event == "release" and (run, pane) in panes:
                panes[(run, pane)] = "released"
        return [{"run_id": run, "pane_id": pane, "state": state} for (run, pane), state in panes.items()]


def liveness_ports(hoh_ports: Iterable) -> list:
    """For `surface_liveness`: each root asked twice -- the product's surface list and the dispatchers' registry.
    `hoh_ports` are the `HohCli` objects of the roots the arms used."""
    out: list = []
    for p in hoh_ports:
        out.extend([p, OwnedAgentsPort(p.root)])
    return out


# --------------------------------------------------------------------------- #
# Patches, transcripts, prompts
# --------------------------------------------------------------------------- #

_VERSIONED = re.compile(r"\.v\d+\.\d{8}T\d{6}Z$")


def filter_patch(diff_text: str) -> tuple[str, int]:
    """Drop the file sections of a unified diff whose path is an `.archiv/` path or a `<name>.v<N>.<UTC>` copy
    (the house-rules backups of PREREG 5.2); returns the patch and how many sections were dropped."""
    sections = re.split(r"(?m)^(?=diff --git )", diff_text)
    kept, dropped = [], 0
    for sec in sections:
        m = re.match(r"diff --git a/(\S+) b/(\S+)", sec)
        if m and (".archiv/" in m.group(2) or m.group(2).startswith(".archiv/") or _VERSIONED.search(m.group(2))):
            dropped += 1
            continue
        kept.append(sec)
    return "".join(kept), dropped


def claude_slug(cwd: str | Path) -> str:
    """The directory name Claude Code gives a working directory under `~/.claude/projects` (ASSUMED: every
    character that is not a letter, digit or dash becomes a dash)."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(cwd))


def locate_transcripts(projects_dir: Path, cwd_prefixes: Iterable[str | Path], since: float) -> list[Path]:
    """Session transcripts (`*.jsonl`) of sessions whose working directory starts with one of the prefixes and
    that were modified at or after `since` (epoch seconds). The listing is the cell's scan list: a transcript
    that was missed makes the cell's usage a lower bound, which the tool flags only if none was found."""
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    slugs = [claude_slug(p) for p in cwd_prefixes]
    found = []
    for d in sorted(projects_dir.iterdir()):
        if d.is_dir() and any(d.name.startswith(s) for s in slugs):
            for f in sorted(d.glob("*.jsonl")):
                if f.is_file() and not f.is_symlink() and f.stat().st_mtime >= since:
                    found.append(f)
    return found


def transcript_lines(paths: Iterable[Path]) -> list[str]:
    out: list[str] = []
    for p in paths:
        out.extend(b1.read_regular_bytes(Path(p)).decode("utf-8", errors="replace").splitlines())
    return out


def render_prompt(template: str, **values: str) -> str:
    """`{name}` placeholders replaced by plain `str.replace`; a placeholder left over is an error."""
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    left = re.findall(r"\{[a-z_]+\}", out)
    if left:
        raise b1.Bench1Error(f"prompt placeholders left unfilled: {sorted(set(left))}")
    return out


def without_fix_request(wrapper: str) -> str:
    """The wrapper minus its 'Fix the issue below ...' paragraph (the reviewer's copy of the task text)."""
    paras = wrapper.split("\n\n")
    return "\n\n".join(p for p in paras if not p.lstrip().startswith("Fix the issue below"))


# --------------------------------------------------------------------------- #
# Campaign paths and the shared cell layout
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class CampaignPaths:
    root: Path
    store: Path
    work: Path
    roots: Path
    tmp: Path
    policy_file: Path             # the approval policy of the pinned product (policy/role_approval.default.json)
    routing_policy: Path          # hoh-routing-policy/1: claude-sonnet-5-5 / high for every role
    arms_dir: Path                # frozen arms/*.md
    tool_path: Path
    hoh_src: Path | None = None
    claude_projects: Path = Path.home() / ".claude" / "projects"

    @classmethod
    def under(cls, root: Path, policy_file: Path, arms_dir: Path, tool_path: Path,
              hoh_src: Path | None = None) -> CampaignPaths:
        root = Path(root)
        return cls(root=root, store=root / "store", work=root / "work", roots=root / "roots", tmp=root / "tmp",
                   policy_file=Path(policy_file), routing_policy=root / "routing-policy.json",
                   arms_dir=Path(arms_dir), tool_path=Path(tool_path), hoh_src=hoh_src)

    def cell_root(self, key: str) -> Path:
        return self.roots / key

    def task_dir(self, task: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", task) or ".." in task:
            raise b1.Bench1Error("task id is not a plain name")
        return self.work / task


def routing_policy_doc() -> dict:
    return {"schema": "hoh-routing-policy/1",
            "default": {"model": b1.REGISTERED_MODEL, "effort": b1.REGISTERED_EFFORT}}


def run_id_for(key: str, task: str) -> str:
    """Herdr truncates names at ~24 characters: the random-looking part first, the task after it."""
    return hashlib.sha256(key.encode()).hexdigest()[:8] + "-" + re.sub(r"[^A-Za-z0-9]", "", task)[-12:]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(b1.read_regular_bytes(Path(path))).hexdigest()


def policy_readback(policy_file: Path) -> dict:
    """`policy_digest`, `house_text_digest` and `denied_tools` of the policy file the product will load."""
    raw = b1.read_regular_bytes(Path(policy_file))
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("house_rules"), str) \
            or not isinstance(doc.get("deny"), list):
        raise b1.Bench1Error("approval policy file has no house_rules text or deny list")
    return {"policy_digest": hashlib.sha256(raw).hexdigest(),
            "house_text_digest": hashlib.sha256(doc["house_rules"].encode("utf-8")).hexdigest(),
            "denied_tools": [str(t) for t in doc["deny"]]}


def routing_readback(run_dir: Path, roles: Iterable[str]) -> dict:
    """models and efforts per role from the run's pinned routing record: `routing.json` names the policy file
    and its digest; the policy is read, its canonical digest compared with the pin, and the default route of each
    role returned. A pin, a policy or a digest that does not hold raises."""
    pin = b1.read_json_object(Path(run_dir) / "routing.json", single_link=True)
    source, digest = pin.get("source"), pin.get("digest")
    if pin.get("schema") != "hoh-routing-pin/1" or not isinstance(source, str) or not isinstance(digest, str):
        raise b1.Bench1Error("routing pin has an unexpected shape")
    policy = b1.read_json_object(Path(source))
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if hashlib.sha256(canonical.encode("ascii")).hexdigest() != digest:
        raise b1.Bench1Error("routing policy differs from the digest the run is pinned to")
    default = policy.get("default") if isinstance(policy.get("default"), dict) else {}
    roles_doc = policy.get("roles") if isinstance(policy.get("roles"), dict) else {}
    models, efforts = {}, {}
    for role in roles:
        entry = roles_doc.get(role) if isinstance(roles_doc.get(role), dict) else {}
        models[role] = entry.get("model", default.get("model"))
        efforts[role] = entry.get("effort", default.get("effort"))
    return {"models": models, "efforts": efforts}


# --------------------------------------------------------------------------- #
# Arm B: a real `hoh run`
# --------------------------------------------------------------------------- #


def b_run_argv(python: str, root: Path, run_id: str, paths: CampaignPaths, isolation: str,
               preflight_options: tuple[str, ...] = ()) -> list[str]:
    """The registered `hoh run` of PREREG 13 (every flag the environment contract needs), then -- P3-11
    slice 4 -- the three environment-preflight options the campaign was given (`preflight_options`; none
    leaves the registered vector byte for byte as it was)."""
    if isolation not in ("none", "strict"):
        raise b1.Bench1Error("isolation must be none or strict")
    return [python, "-m", "hoh.cli", "--root", str(root), "run", run_id, "--iterations", "3",
            "--until-accepted", "--planner", "claude", "--developer", "claude", "--qa", "claude",
            "--role-model", f"all={b1.REGISTERED_MODEL}", "--role-effort", f"all={b1.REGISTERED_EFFORT}",
            "--role-timeout", str(ROLE_TIMEOUT_S), "--approval-policy", str(paths.policy_file),
            "--trust-worktree", "--storage-root", str(paths.store), "--isolation", isolation,
            *preflight_options]


def preflight_options_from_environment(paths: CampaignPaths) -> tuple[str, ...]:
    """P3-11 slice 4: the three options with which arm B's typed `hoh start` and `hoh run` ask the product's
    environment preflight, read from `HOH_PREFLIGHT_REPORT` (and `_PROFILE`, `_MAX_AGE`) by the product's own
    `requirement_from_environment`. The product's own gate (`check_preflight`, for this interpreter, which is the
    one arm B types its commands with) is asked here as well, **before any cell exists**: no variable, a report
    that is missing, unreadable, stale, for another profile or interpreter, or not READY -- a `Bench1Error`
    before a cell directory, a worktree or a ledger entry is made (the typed commands ask again, in the
    processes that spend). The gate is the product's (`paths.hoh_src`): a product without it is a finding, not a
    cell run ungated."""
    if paths.hoh_src is not None and str(paths.hoh_src) not in sys.path:
        sys.path.insert(0, str(paths.hoh_src))
    try:
        from hoh.preflightgate import (PreflightRefused, check_preflight, cli_options,
                                       requirement_from_environment)
    except ImportError as exc:
        raise b1.Bench1Error(f"refused: the product under test has no environment preflight gate "
                             f"({type(exc).__name__}); arm B would start runs on no report") from None
    try:
        requirement = requirement_from_environment()
        check_preflight(requirement)
        return tuple(cli_options(requirement))
    except PreflightRefused as exc:
        raise b1.Bench1Error(f"refused: {exc}") from None


def process_argv(pid: int, proc_root: Path = Path("/proc")) -> list[str]:
    """The argument vector of a running process, read from the kernel's record (Linux)."""
    raw = b1.read_regular_bytes(Path(proc_root) / str(pid) / "cmdline", 1 << 20)
    return [a.decode("utf-8", errors="replace") for a in raw.split(b"\x00") if a]


def verify_process_argv(actual: list[str], expected: list[str]) -> None:
    """The running process carries exactly the registered command line (`--role-timeout 2700` first, since the
    deadline is I14); any other difference is as much a refusal."""
    if option_value(actual, "--role-timeout") != str(ROLE_TIMEOUT_S):
        raise b1.Bench1Error("the running process does not carry --role-timeout 2700")
    if actual != expected:
        raise b1.Bench1Error("the running process does not carry the registered command line")


def worktree_trust_recorded(run_dir: Path, worktree: Path) -> bool:
    """`hoh run --trust-worktree` writes `<run dir>/approval/worktree_trust.json` before any role starts
    (`{"worktree", "registered", ...}`); the record names this run's worktree and a registration, or trust is not
    recorded. (The text `hoh start` prints is about the arena and answers directories, not the worktree.)"""
    try:
        doc = b1.read_json_object(Path(run_dir) / "approval" / "worktree_trust.json", single_link=True)
    except b1.Bench1Error:
        return False
    return doc.get("worktree") == str(Path(worktree).resolve()) and bool(doc.get("registered"))


def option_value(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


class BArmPorts:
    """Arm B as a `CellPorts` object for `bench1.run_cell`. Every collaborator is injected."""

    ROLES = ("planner", "developer", "qa")

    def __init__(self, paths: CampaignPaths, hoh_factory: Callable[[Path], object], procs, *,
                 isolation: str, task_meta: Callable[[str], dict], guard: StorageGuard,
                 popen: Callable = subprocess.Popen, python: str = sys.executable,
                 parse_run_argv: Callable[[list[str]], object] | None = None,
                 proc_root: Path = Path("/proc"), sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, git_runner: Callable | None = None,
                 preflight_options: tuple[str, ...] = ()):
        self.preflight_options = tuple(preflight_options)   # P3-11 slice 4: typed after the other flags
        self.paths, self.hoh_factory, self.procs = paths, hoh_factory, procs
        self.isolation, self.task_meta, self.guard = isolation, task_meta, guard
        self.popen, self.python, self.parse_run_argv = popen, python, parse_run_argv
        self.proc_root, self.sleep, self.clock = proc_root, sleep, clock
        self.git = git_runner
        self.hoh = None
        self._state: dict[str, dict] = {}

    # -- before the start ------------------------------------------------------------------------------------

    def build_and_readback(self, spec) -> dict:
        self.guard.check()
        key = spec.key
        cell_root = self.paths.cell_root(key)
        cell_root.mkdir(parents=True, exist_ok=False)
        meta = self.task_meta(spec.task)
        run_id = run_id_for(key, spec.task)
        hoh = self.hoh_factory(cell_root)
        self.hoh = hoh
        snap = self.paths.task_dir(spec.task) / "snap"
        out = json.loads(hoh.run("worktree", "--repo", str(snap), "--branch", f"bench-{run_id}").stdout)
        wt = out.get("worktree") if isinstance(out, dict) else None
        if not isinstance(wt, str) or not wt:
            raise b1.Bench1Error("hoh worktree did not name the worktree")
        wrapper = b1.read_regular_bytes(self.paths.arms_dir / "task_wrapper.md").decode("utf-8")
        issue = b1.read_regular_bytes(Path(spec.issue_path)).decode("utf-8")
        spec_file = cell_root / "SPEC.md"
        spec_file.write_text(render_prompt(wrapper, problem_statement=issue), encoding="utf-8")
        json.loads(hoh.run(
            "start", "--repo", wt, "--spec", str(spec_file), "--project", f"bench1-{spec.task}"[:60],
            "--run-id", run_id, "--max-iterations", "3", "--max-dispatches", str(b1.DISPATCH_CAP["B"]),
            "--routing-policy", str(self.paths.routing_policy), *self.preflight_options).stdout)
        argv = b_run_argv(self.python, cell_root, run_id, self.paths, self.isolation, self.preflight_options)
        ns = self.parse_run_argv(argv[3:]) if self.parse_run_argv else None   # the product's own parse
        parsed_timeout = int(ns.role_timeout) if ns is not None else None
        parsed_iso = ns.isolation if ns is not None else None
        run_dir = cell_root / run_id
        state = b1.read_json_object(run_dir / "state.json", single_link=True)
        pol = policy_readback(Path(ns.approval_policy) if ns is not None else self.paths.policy_file)
        observed = {**routing_readback(run_dir, self.ROLES), **pol}
        observed["role_timeout_s"] = {r: parsed_timeout for r in self.ROLES}
        observed["dispatch_cap"] = (state.get("budgets") or {}).get("max_dispatches")
        observed["isolation"] = parsed_iso
        observed["storage_root_initialised"] = (self.paths.store / "OWNER.json").is_file()
        observed["storage_root"] = str(self.paths.store)
        observed["herdr_env"] = os.environ.get("HERDR_ENV") == "1"
        observed["hidden_material_in_worktree"] = not worktree_matches_snapshot(Path(wt), snap, self.git)
        observed["snapshot"] = b1.snapshot_report(snap)
        observed["deferred_checks"] = ["project_resolved", "session_open", "scratch_under_storage_root",
                                       "trust_recorded"]
        self._state[key] = {"run_id": run_id, "cell_root": cell_root, "argv": argv, "wt": Path(wt),
                            "snap": snap, "meta": meta, "base": state.get("base_candidate"), "t0": self.clock()}
        return observed

    # -- start, post-start, wait, collect --------------------------------------------------------------------

    def start(self, spec) -> object:
        self.guard.check()
        st = self._state[spec.key]
        env = dict(os.environ, TMPDIR=str(self.paths.tmp))
        if self.paths.hoh_src is not None:
            env["PYTHONPATH"] = str(self.paths.hoh_src)
        log_fd = _create_log(st["cell_root"] / "hoh-run.log")
        try:
            proc = self.popen(st["argv"], stdout=log_fd, stderr=subprocess.STDOUT, env=env,
                              start_new_session=True, cwd=str(st["cell_root"]))
        finally:
            os.close(log_fd)
        st["proc"] = proc
        return b1.Handle(spec.key, spec.arm, pgid=proc.pid, run_id=st["run_id"], leader_pid=proc.pid)

    def post_start_readback(self, spec, handle) -> dict:
        """After the process exists, before the first provider call can matter: the parsed command line of the
        process (what it will use) and the product's own registrations. ASSUMED record shapes are named."""
        st = self._state[spec.key]
        verify_process_argv(process_argv(handle.leader_pid, self.proc_root), st["argv"])
        deadline = self.clock() + POST_START_WAIT_S
        session = scratch = trust = False
        run_dir = st["cell_root"] / st["run_id"]
        while self.clock() < deadline and not (session and scratch and trust):
            session = find_session(self.paths.store, st["run_id"])
            scratch = scratch_under(self.paths.store, run_dir)
            trust = worktree_trust_recorded(run_dir, st["wt"])
            if not (session and scratch and trust):
                self.sleep(1)
        state = {}
        try:
            state = b1.read_json_object(st["cell_root"] / st["run_id"] / "state.json", single_link=True)
        except b1.Bench1Error:
            pass
        return {"project_resolved": session, "session_open": session, "scratch_under_storage_root": scratch,
                "trust_recorded": trust, "provider_calls_before_check": (state.get("usage") or {}).get("dispatches")}

    def wait(self, handle, seconds: float) -> bool:
        proc = self._state[handle.cell_key]["proc"]
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return True

    def collect(self, handle) -> dict:
        st = self._state[handle.cell_key]
        run_dir = st["cell_root"] / st["run_id"]
        state = b1.read_json_object(run_dir / "state.json", single_link=True)
        ended, reason, commit = b1.ending_from_state(state)
        patch_path = None
        if commit is None:
            wc = state.get("working_candidate")
            commit = wc.get("commit") if isinstance(wc, dict) else None
        if commit:
            base = st["base"].get("commit") if isinstance(st["base"], dict) else None
            base = base or run_git(st["snap"], "rev-parse", "HEAD", runner=self.git).strip()
            raw = run_git(st["wt"], "diff", f"{base}..{commit}", runner=self.git)
            patch, _dropped = filter_patch(raw)
            patch_path = st["cell_root"] / "patch.diff"
            patch_path.write_text(patch, encoding="utf-8")
        files = locate_transcripts(self.paths.claude_projects, [st["wt"], run_dir, st["cell_root"]], st["t0"])
        artefactual: set[str] = set()
        tel = run_dir / "telemetry.jsonl"
        if tel.is_file():
            for line in b1.read_regular_bytes(tel).decode("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("artefactual"), list):
                    artefactual |= {str(x) for x in row["artefactual"]}
        usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
        return {"ended": ended, "stop_reason": reason, "claimed_tree": commit,
                "patch_path": str(patch_path) if patch_path else None, "dispatches": usage.get("dispatches"),
                "transcript_lines": transcript_lines(files), "transcripts": [str(p) for p in files],
                "artefactual": sorted(artefactual), "rounds": []}


def trust_registered(worktree: str, registry: Path | None = None) -> bool:
    """ASSUMED (as `tools/benchmark.py` reads it): the harness's `~/.claude.json` holds a `projects[<path>]`
    entry with `hasTrustDialogAccepted` true for the worktree."""
    reg = Path(registry) if registry is not None else Path.home() / ".claude.json"
    try:
        doc = b1.read_json_object(reg)
    except b1.Bench1Error:
        return False
    proj = doc.get("projects")
    ent = proj.get(str(worktree)) if isinstance(proj, dict) else None
    return isinstance(ent, dict) and ent.get("hasTrustDialogAccepted") is True


def _create_log(path: Path) -> int:
    """A new log file (never replaced, no symlink follow) as a file descriptor for a child's stdout."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def find_session(store: Path, run_id: str, limit: int = 20000) -> bool:
    """ASSUMED: the product's registered ephemeral session for a run leaves a record under the storage root whose
    name mentions the run id. Bounded walk; False when none is found (the cell then fails closed)."""
    n = 0
    for dirpath, _dirs, files in os.walk(store):
        for name in files:
            n += 1
            if n > limit:
                return False
            if run_id in name or run_id in dirpath:
                return True
    return False


def scratch_under(store: Path, run_dir: Path) -> bool:
    """ASSUMED: the run's owned-agent registry lists working directories; they must be under the storage root or
    the run directory. Read from `owned-agents.jsonl` (name taken from `agentcontrol.OWNED_FILE`)."""
    f = Path(run_dir) / "owned-agents.jsonl"
    if not f.is_file():
        return False
    cwds = []
    for line in b1.read_regular_bytes(f).decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("cwd"), str):
            cwds.append(row["cwd"])
    roots = (str(store), str(run_dir.parent))
    return bool(cwds) and all(c.startswith(roots) for c in cwds)


# --------------------------------------------------------------------------- #
# Local git (read-only except the snapshot's own repository)
# --------------------------------------------------------------------------- #

PORTS_GIT_VERBS = ("rev-parse", "diff", "status", "ls-files", "add")


def run_git(repo: Path, *args: str, runner: Callable | None = None) -> str:
    """One allow-listed git verb in `repo`. `add` is used only as `add -A` on an arm's own worktree to make
    untracked files visible to `diff --cached` (the worker's patch extraction)."""
    verb = args[0] if args else None
    if verb not in PORTS_GIT_VERBS:
        raise b1.Bench1Error("git verb is not on the allow-list")
    run = runner or subprocess.run
    done = run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300, check=False,
               env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)})
    if done.returncode != 0:
        raise b1.Bench1Error(f"git {verb} failed: {(done.stderr or '').strip()[:160]}")
    return done.stdout


def worktree_matches_snapshot(wt: Path, snap: Path, runner: Callable | None = None) -> bool:
    """The arm's worktree is exactly the snapshot commit with no change: nothing else was put there."""
    a = run_git(wt, "rev-parse", "HEAD", runner=runner).strip()
    b = run_git(snap, "rev-parse", "HEAD", runner=runner).strip()
    dirty = run_git(wt, "status", "--porcelain", runner=runner).strip()
    return a == b and not dirty


def worktree_patch(wt: Path, runner: Callable | None = None) -> str:
    """The arm's current change as a patch against HEAD, new files included (stages with `add -A` first)."""
    run_git(wt, "add", "-A", runner=runner)
    return run_git(wt, "diff", "--cached", "HEAD", runner=runner)


def worktree_digest(wt: Path, runner: Callable | None = None) -> str:
    return hashlib.sha256(worktree_patch(wt, runner).encode("utf-8", errors="replace")).hexdigest()


# --------------------------------------------------------------------------- #
# Arms A0 / A1+: the plain agent in a worker process
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class WorkerPlan:
    """What the parent hands the worker (a JSON file): everything but secrets."""

    key: str
    arm: str
    cell_root: str
    worktree: str
    spec_issue: str
    arms_dir: str
    policy_file: str
    hoh_src: str | None
    model: str
    effort: str
    timeout_s: int
    dispatch_cap: int
    handshake_wait_s: int = HANDSHAKE_WAIT_S

    def dump(self, path: Path) -> None:
        b1.write_create_only(Path(path), dataclasses.asdict(self))

    @classmethod
    def load(cls, path: Path) -> WorkerPlan:
        doc = b1.read_json_object(Path(path), single_link=True)
        try:
            return cls(**doc)
        except TypeError as exc:
            raise b1.Bench1Error("worker plan has unexpected fields") from exc


def worker_prompts(plan: WorkerPlan) -> dict:
    arms = Path(plan.arms_dir)
    read = lambda n: b1.read_regular_bytes(arms / n).decode("utf-8")
    issue = b1.read_regular_bytes(Path(plan.spec_issue)).decode("utf-8")
    wrapper = render_prompt(read("task_wrapper.md"), problem_statement=issue)
    return {
        "wrapper": wrapper,
        "A0": render_prompt(read("A0.md"), task_wrapper=wrapper),
        "dev": read("A1plus_dev.md"),
        "review": read("A1plus_review.md"),
        "digests": {n: hashlib.sha256((arms / n).read_bytes()).hexdigest()
                    for n in ("task_wrapper.md", "A0.md", "A1plus_dev.md", "A1plus_review.md")},
    }


def arm_worker_main(plan: WorkerPlan, dispatcher_factory: Callable, *, sleep: Callable = time.sleep,
                    clock: Callable[[], float] = time.time, runner: Callable | None = None,
                    set_signal: Callable = signal.signal) -> int:
    """The A-arm worker. Builds the dispatcher (the only place a provider can be reached), writes
    `readback.json` from the **built** object before the first dispatch, waits for the parent's `go` file, then
    runs the arm. `dispatcher_factory(plan)` returns an object with `models`, `efforts`, `timeout_ms`,
    `approval_policy_digest`, `dispatch(role_key, prompt) -> str` and `close_own()`.

    SIGTERM (the kill path's group signal) ends the worker through `SystemExit(143)`, and whatever way the worker
    ends -- finished, handshake timeout, SIGTERM, an exception -- the panes its dispatcher opened are closed in
    the `finally` (the tool's `close_owned` cannot close an A arm's panes: they are the worker's own)."""
    cell = Path(plan.cell_root)

    def on_term(signum, frame):
        raise SystemExit(143)
    set_signal(signal.SIGTERM, on_term)
    d = None
    try:
        d = dispatcher_factory(plan)
        return _worker_body(plan, d, cell, sleep, clock, runner)
    finally:
        if d is not None:
            try:
                d.close_own()
            except Exception as exc:                        # noqa: BLE001 - recorded, the group kill follows
                (cell / "close-own-error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")


def _worker_body(plan: WorkerPlan, d, cell: Path, sleep: Callable, clock: Callable, runner: Callable | None) -> int:
    readback = {
        "models": {k: v for k, v in dict(d.models).items()},
        "efforts": {k: v for k, v in dict(d.efforts).items()},
        "role_timeout_s": int(d.timeout_ms) // 1000,
        "policy_digest": d.approval_policy_digest,
        "dispatch_cap": plan.dispatch_cap,
        "trust_recorded": bool(getattr(d, "trust_recorded", False)),
    }
    b1.write_create_only(cell / "readback.json", readback)
    deadline = clock() + plan.handshake_wait_s
    go = cell / "go"
    while not go.exists():
        if clock() > deadline:
            return 3
        sleep(0.5)
    prompts = worker_prompts(plan)
    wt = Path(plan.worktree)
    result: dict
    if plan.arm == "A0":
        answer = d.dispatch("developer", prompts["A0"])
        marker = b1.parse_claim(answer)
        result = {"ended": "claim" if marker == "COMPLETE" else "no_claim", "dispatches": 1,
                  "rounds": [], "reason": "single session"}
        patch = worktree_patch(wt, runner)
        if marker == "COMPLETE":
            result["claimed_tree"] = hashlib.sha256(patch.encode("utf-8", errors="replace")).hexdigest()
    else:
        def dispatch(role: str, findings: str) -> str:
            if role == "developer":
                text = render_prompt(prompts["dev"], task_wrapper=prompts["wrapper"],
                                     previous_review_findings=findings or "")
            else:
                text = render_prompt(prompts["review"],
                                     task_wrapper_without_fix_request=without_fix_request(prompts["wrapper"]))
            return d.dispatch(role, text)

        def snapshot(label: str) -> str:
            patch_text = worktree_patch(wt, runner)
            rounds = cell / "rounds"
            rounds.mkdir(exist_ok=True)
            b1.write_create_only(rounds / f"{label}.json", {"patch": patch_text})
            return hashlib.sha256(patch_text.encode("utf-8", errors="replace")).hexdigest()

        result = b1.run_a1plus(dispatch, lambda: worktree_digest(wt, runner), snapshot, plan.dispatch_cap)
        patch = worktree_patch(wt, runner)
    filtered, dropped = filter_patch(patch)
    (cell / "patch.diff").write_text(filtered, encoding="utf-8")
    result.update(prompt_digests=prompts["digests"], dropped_sections=dropped)
    b1.write_create_only(cell / "result.json", result)
    return 0


class AArmPorts:
    """Arms A0 and A1+ as a `CellPorts` object: the worker is a child process in a session of its own."""

    def __init__(self, paths: CampaignPaths, procs, *, guard: StorageGuard, task_meta: Callable[[str], dict],
                 hoh_factory: Callable[[Path], object],
                 popen: Callable = subprocess.Popen, python: str = sys.executable,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 git_runner: Callable | None = None, worktree_maker: Callable | None = None):
        self.paths, self.procs, self.guard, self.task_meta = paths, procs, guard, task_meta
        self.hoh_factory, self.popen, self.python = hoh_factory, popen, python
        self.sleep, self.clock, self.git = sleep, clock, git_runner
        self.worktree_maker = worktree_maker
        self.hoh = None
        self._st: dict[str, dict] = {}

    def build_and_readback(self, spec) -> dict:
        self.guard.check()
        cell = self.paths.cell_root(spec.key)
        cell.mkdir(parents=True, exist_ok=False)
        self.hoh = self.hoh_factory(cell)
        snap = self.paths.task_dir(spec.task) / "snap"
        wt = self.worktree_maker(snap, spec) if self.worktree_maker else None
        if wt is None:
            out = json.loads(self.hoh.run("worktree", "--repo", str(snap), "--branch",
                                          f"bench-{run_id_for(spec.key, spec.task)}").stdout)
            wt = out.get("worktree") if isinstance(out, dict) else None
        if not isinstance(wt, str) or not wt:
            raise b1.Bench1Error("the worktree was not created")
        pol = policy_readback(self.paths.policy_file)
        plan = WorkerPlan(key=spec.key, arm=spec.arm, cell_root=str(cell), worktree=wt,
                          spec_issue=spec.issue_path, arms_dir=str(self.paths.arms_dir),
                          policy_file=str(self.paths.policy_file),
                          hoh_src=str(self.paths.hoh_src) if self.paths.hoh_src else None,
                          model=b1.REGISTERED_MODEL, effort=b1.REGISTERED_EFFORT, timeout_s=ROLE_TIMEOUT_S,
                          dispatch_cap=b1.DISPATCH_CAP[spec.arm])
        plan_path = cell / "plan.json"
        plan.dump(plan_path)
        self._st[spec.key] = {"cell": cell, "plan": plan_path, "wt": Path(wt), "snap": snap, "t0": self.clock()}
        observed = dict(pol)
        observed["dispatch_cap"] = plan.dispatch_cap
        observed["storage_root_initialised"] = (self.paths.store / "OWNER.json").is_file()
        observed["storage_root"] = str(self.paths.store)
        observed["herdr_env"] = os.environ.get("HERDR_ENV") == "1"
        observed["hidden_material_in_worktree"] = not worktree_matches_snapshot(Path(wt), snap, self.git)
        observed["snapshot"] = b1.snapshot_report(snap)
        # what only the built dispatcher can say, and the registrations of the process, come after the start
        observed["not_applicable"] = ["project_resolved", "session_open"]
        observed["deferred_checks"] = ["models", "efforts", "role_timeout_s", "trust_recorded",
                                       "scratch_under_storage_root", "policy_digest"]
        return observed

    def start(self, spec) -> object:
        self.guard.check()
        st = self._st[spec.key]
        env = dict(os.environ, TMPDIR=str(self.paths.tmp))
        if self.paths.hoh_src is not None:
            env["PYTHONPATH"] = str(self.paths.hoh_src)
        log_fd = _create_log(st["cell"] / "worker.log")
        try:
            proc = self.popen([self.python, str(self.paths.tool_path), "arm-worker", "--plan", str(st["plan"])],
                              stdout=log_fd, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                              cwd=str(st["wt"]))
        finally:
            os.close(log_fd)
        st["proc"] = proc
        return b1.Handle(spec.key, spec.arm, pgid=proc.pid, run_id=None, leader_pid=proc.pid)

    def post_start_readback(self, spec, handle) -> dict:
        """The handshake: wait for the worker's `readback.json` (written from its built dispatcher), return it
        in the tool's keys, and release the worker by creating `go` **only after** this method returned
        (`run_cell` compares first; on a mismatch the group is killed and `go` never appears)."""
        st = self._st[spec.key]
        rb = st["cell"] / "readback.json"
        deadline = self.clock() + HANDSHAKE_WAIT_S
        while not rb.exists():
            if self.clock() > deadline:
                raise b1.Bench1Error("the worker did not write its read-back")
            self.sleep(0.5)
        doc = b1.read_json_object(rb, single_link=True)
        roles = b1.ARM_ROLES[spec.arm]
        key_for = {"developer": "developer", "reviewer": "qa"}
        models = {r: doc["models"].get(key_for.get(r, r)) for r in roles}
        efforts = {r: doc["efforts"].get(key_for.get(r, r)) for r in roles}
        wt = str(st["wt"])
        return {"models": models, "efforts": efforts, "policy_digest": doc.get("policy_digest"),
                "role_timeout_s": {r: doc.get("role_timeout_s") for r in roles},
                "trust_recorded": doc.get("trust_recorded") is True,
                # the plain agent's worktree is under the campaign root (the registered contract said the storage
                # root; the tool's I13 is applied to A arms through this narrower, truthful reading -- QUESTIONS)
                "scratch_under_storage_root": wt.startswith(str(self.paths.root) + os.sep),
                "provider_calls_before_check": 0}

    def release(self, spec) -> None:
        """Called by the CLI after `run_cell`'s comparison passed: lets the worker dispatch."""
        st = self._st[spec.key]
        go = st["cell"] / "go"
        fd = os.open(go, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)

    def wait(self, handle, seconds: float) -> bool:
        proc = self._st[handle.cell_key]["proc"]
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return True

    def collect(self, handle) -> dict:
        st = self._st[handle.cell_key]
        cell = st["cell"]
        res_path = cell / "result.json"
        result = b1.read_json_object(res_path, single_link=True) if res_path.exists() else {}
        files = locate_transcripts(self.paths.claude_projects, [st["wt"], cell], st["t0"])
        patch = cell / "patch.diff"
        released = (cell / "go").exists()
        return {"ended": result.get("ended", "instrument_failure"),
                "stop_reason": result.get("reason", "the worker wrote no result"),
                "claimed_tree": result.get("claimed_tree"),
                "patch_path": str(patch) if patch.exists() else None,
                # a worker that was never released (`go` absent) cannot have dispatched: zero, not unknown
                "dispatches": result.get("dispatches", None if released else 0), "rounds": result.get("rounds", []),
                "transcript_lines": transcript_lines(files), "transcripts": [str(p) for p in files]}


# --------------------------------------------------------------------------- #
# Subcommand mains (called by tools/bench1.py; `tool` is that module)
# --------------------------------------------------------------------------- #


def _paths(args) -> CampaignPaths:
    root = Path(args.campaign_root)
    here = Path(b1.__file__).resolve().parent
    return CampaignPaths.under(root, policy_file=here.parent / "policy" / "role_approval.default.json",
                               arms_dir=root / "arms", tool_path=here / "bench1.py",
                               hoh_src=here.parent / "src")


def _sample_meta(sample_path: Path) -> Callable[[str], dict]:
    sample = b1.read_json_object(Path(sample_path))
    rows = {}
    for key in ("round1", "round2", "spares", "stage0", "stage1a", "stage1b"):
        for r in sample.get(key, []):
            rows[r["instance_id"]] = r
    def meta(task: str) -> dict:
        if task not in rows:
            raise b1.Bench1Error("task is not in the sample file")
        return rows[task]
    return meta


class PublicDatasetHF(b1.DatasetHF):  # pragma: no cover - network
    """The public columns only (`repo`, `base_commit`, `problem_statement`) of an instance, read into memory by
    the tool's dataset port (never written to disk); every hidden column is dropped on the spot."""

    def fetch(self, ids: list[str]) -> dict:
        rows = super().fetch(ids)
        return {k: {"repo": v["repo"], "base_commit": v["base_commit"],
                    "problem_statement": v["problem_statement"]} for k, v in rows.items()}


def write_issue(task_dir: Path, task: str, sample_row: dict, public_rows: dict,
                meta_rows: dict | None = None) -> int:
    """`work/<task>/issue.txt` = the dataset row's `problem_statement`, verbatim (PREREG 5: the wrapper plus the
    problem statement). The public row must be the sample's own instance (same repository and base commit); when
    the frozen metadata is given its `problem_chars` must equal the text's length. Create-only; a second call with
    the same text is a no-op, with another text a refusal."""
    pub = public_rows.get(task)
    if not isinstance(pub, dict) or not isinstance(pub.get("problem_statement"), str) \
            or not pub["problem_statement"].strip():
        raise b1.Bench1Error("the dataset has no problem statement for the task")
    if pub.get("repo") != sample_row.get("repo") or pub.get("base_commit") != sample_row.get("base_commit"):
        raise b1.Bench1Error("the dataset row is not the sample's instance (repository or base commit differ)")
    text = pub["problem_statement"]
    if meta_rows is not None:
        chars = (meta_rows.get(task) or {}).get("problem_chars")
        if chars != len(text):
            raise b1.Bench1Error("the problem statement is not the one the frozen metadata describes")
    path = Path(task_dir) / "issue.txt"
    Path(task_dir).mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        if b1.read_regular_bytes(path).decode("utf-8") != text:
            raise b1.Bench1Error("issue.txt exists with another text") from None
        return len(text)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(text)


def _meta_rows(meta_path: str | None) -> dict | None:
    if not meta_path:
        return None
    raw = json.loads(b1.read_regular_bytes(Path(meta_path)).decode("utf-8"))
    if not isinstance(raw, list):
        raise b1.Bench1Error("the frozen metadata is not a list of rows")
    return {r["instance_id"]: r for r in raw if isinstance(r, dict) and "instance_id" in r}


def main_prepare(args, tool, *, docker_factory: Callable | None = None, public_source=None,
                 statvfs: Callable = os.statvfs) -> int:
    """Steps (one per call): `manifest` (stage's refs, sealed, before any pull), `pull` (one listed ref, guarded,
    measured, the measurement kept), `snapshot` (the issue from the dataset row, the export without `.git`, one
    commit, the `bench-test`); `validate` is a CLI shape only: it needs the hidden rows and the official grader and
    is driven by the Stage 0 session (`entry_gate_for_task`, `official_run`)."""
    paths = _paths(args)
    freeze = b1.Freeze.load(Path(args.freeze))
    guard = StorageGuard((str(paths.root), "/"), statvfs=statvfs)
    sample = b1.read_json_object(Path(args.sample))
    tasks = stage_tasks(sample, args.stage)
    img = paths.root / "images"
    img.mkdir(parents=True, exist_ok=True)
    (img / "receipts").mkdir(exist_ok=True)
    manifest, chain = img / "manifest.json", img / "CHAIN.jsonl"
    if args.step == "manifest":
        refs = [{"instance_id": t["instance_id"], "ref": image_ref(t["instance_id"])} for t in tasks]
        b1.write_manifest(manifest, "b1", refs, chain)
        print(json.dumps({"manifest": str(manifest), "refs": len(refs)}))
        return 0
    if not args.task:
        raise b1.Bench1Error(f"--task is required for step {args.step}")
    row = {t["instance_id"]: t for t in tasks}.get(args.task)
    if row is None:
        raise b1.Bench1Error("task is not in this stage's sample")
    ref = image_ref(args.task)
    if args.step == "snapshot":
        guard.check()                                 # before the issue is fetched and before any export
    make_docker = lambda: (docker_factory or GuardedDocker)(guard, delta_dir=img / "inode-deltas")
    if args.step == "pull":
        docker = make_docker()
        rec = b1.pull_one(docker, manifest, ref, img / "receipts", chain)
        print(json.dumps({"ref": ref, "preexisting": rec["preexisting"], "inode_deltas": docker.deltas}))
        return 0
    if args.step == "snapshot":
        docker = make_docker()
        paths.tmp.mkdir(parents=True, exist_ok=True)
        chars = write_issue(paths.task_dir(args.task), args.task, row,
                            (public_source or PublicDatasetHF()).fetch([args.task]), _meta_rows(args.meta))
        info = prepare_snapshot(docker, ref, paths.task_dir(args.task), paths.tmp)
        write_bench_test(paths.task_dir(args.task), ref, paths.tool_path)
        print(json.dumps({"task": args.task, **info, "issue_chars": chars,
                          "frozen_patterns": freeze.wiring_patterns_digest[:12]}))
        return 0
    raise b1.Bench1Error("the validate step is driven by the Stage 0 session with the hidden rows loaded "
                         "(entry_gate_for_task); it has no standalone command")


def main_run_cell(args, tool, *, make_ports: Callable | None = None, statvfs: Callable = os.statvfs) -> int:
    paths = _paths(args)
    freeze = b1.Freeze.load(Path(args.freeze))
    approvals = b1.StageApprovals.load(Path(args.approval))
    patterns = b1.WiringPatterns.load(Path(args.patterns))
    sample = b1.read_json_object(Path(args.sample))
    if args.task not in {t["instance_id"] for t in stage_tasks(sample, args.stage)}:
        raise b1.Bench1Error("task is not in this stage's sample")
    meta_fn = _sample_meta(Path(args.sample))
    meta = meta_fn(args.task)
    guard = StorageGuard((str(paths.root), "/"), statvfs=statvfs)
    guard.check()
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    cell_dir = paths.task_dir(args.task)
    spec = b1.CellSpec(stage=args.stage, task=args.task, arm=args.arm, rep=args.rep, attempt=args.attempt,
                       repo_slug=meta["repo"], issue_path=str(cell_dir / "issue.txt"),
                       snapshot_path=str(cell_dir / "snap"),
                       hoh_root=str(paths.cell_root(b1.cell_key(args.task, args.arm, args.rep, args.attempt))),
                       base_commit=str(meta.get("base_commit", "")), supersedes=args.supersedes)
    ports = (make_ports or _production_ports)(args.arm, paths, freeze, guard, meta_fn)
    ledger = b1.BudgetLedger(Path(args.ledger))
    doc = b1.run_cell(spec, ports, results, ledger, patterns, freeze, approvals)
    print(json.dumps({k: doc.get(k) for k in ("key", "ended", "stop_reason", "spend_wtu", "path")}))
    return 0 if doc.get("ended") not in ("instrument_failure",) else 1


def _production_ports(arm: str, paths: CampaignPaths, freeze, guard: StorageGuard, meta_fn: Callable):
    procs = b1.ProcessGroups()
    hoh_factory = lambda root: b1.HohCli(root, src=paths.hoh_src)
    if arm == "B":
        def parse_run(argv):
            sys.path.insert(0, str(paths.hoh_src))
            from hoh.cli import build_parser  # the product's own parser
            return build_parser().parse_args(argv)
        return BArmPorts(paths, hoh_factory, procs, isolation=freeze.isolation, task_meta=meta_fn, guard=guard,
                         parse_run_argv=parse_run, preflight_options=preflight_options_from_environment(paths))
    return AArmPorts(paths, procs, guard=guard, task_meta=meta_fn, hoh_factory=hoh_factory)


def main_regrade(args, tool, *, hoh_factory: Callable | None = None, dataset=None, grader=None) -> int:
    results = Path(args.results)
    cell = b1.read_json_object(results / f"{args.key}.cell.json", single_link=True)
    run = b1.read_json_object(results / f"{args.key}.run.json", single_link=True)
    patch = run.get("patch_path")
    if not patch:
        raise b1.Bench1Error("the run record has no patch to regrade")
    make = hoh_factory or b1.HohCli
    roots = [Path(r) for r in args.hoh_root]
    if Path(run.get("hoh_root", "")) not in roots:
        raise b1.Bench1Error("the run record names a root that liveness was not asked of")
    ports = liveness_ports([make(r) for r in roots])
    hidden = b1.HiddenRows(dataset or b1.DatasetHF(), Path(args.scratch_root), b1.surface_liveness(ports))
    task_dir = [Path(run["snapshot_path"]).parent] if isinstance(run.get("snapshot_path"), str) else []
    rec = b1.regrade_cell(cell, Path(patch), hidden, grader or build_grader(agent_roots=roots + task_dir), results)
    print(json.dumps(rec))
    return 0


def main_arm_worker(args, tool) -> int:
    plan = WorkerPlan.load(Path(args.plan))

    def factory(p: WorkerPlan):                                 # pragma: no cover - needs hoh, Herdr, a provider
        if p.hoh_src:
            sys.path.insert(0, p.hoh_src)
        from hoh import herdr
        from hoh.contracts import Role
        from hoh.controller import Controller
        from hoh.dispatchers import build_dispatcher
        herdr.require_herdr()
        cell = Path(p.cell_root)
        answers = cell / "answers"
        answers.mkdir(exist_ok=True)
        pol_raw = b1.read_regular_bytes(Path(p.policy_file))
        model = {Role.DEVELOPER: p.model, Role.QA: p.model}
        effort = {Role.DEVELOPER: p.effort, Role.QA: p.effort}
        state = Controller.new_state(run_id=f"{p.key}"[:60], repo_path=p.worktree, project_name="bench1",
                                     spec_path=p.spec_issue)
        d = build_dispatcher(answers_dir=answers, profiles={Role.DEVELOPER: "claude", Role.QA: "claude"},
                             cwd=p.worktree, prefer_herdr=True, timeout_ms=p.timeout_s * 1000,
                             models=model, efforts=effort, approval_policy=json.loads(pol_raw))

        class Wrapped:
            """What the product's own dispatcher object holds -- never what the plan says it should hold."""

            def __init__(self):
                self.models = {str(k): v for k, v in dict(d.models).items()}
                self.efforts = {str(k): v for k, v in dict(d.efforts).items()}
                self.timeout_ms = d.timeout_ms
                held = d.approval_policy
                self.approval_policy_digest = hashlib.sha256(pol_raw).hexdigest() \
                    if held == json.loads(pol_raw) else \
                    hashlib.sha256(json.dumps(held, sort_keys=True).encode("utf-8")).hexdigest()
                self.trust_recorded = trust_registered(p.worktree)

            def dispatch(self, role_key, prompt):
                role = Role.DEVELOPER if role_key == "developer" else Role.QA
                return d.dispatch(role, prompt, state=state)

            def close_own(self):
                d.close_own()
        return Wrapped()
    return arm_worker_main(plan, factory)
