#!/usr/bin/env python3
"""regression_watch.py -- the A7 regression obligation of a closed phase, made mechanical.

Amendment A7: a phase is closed at the commit of its closure record; later changes to
what its evidence is about "create regression obligations, not a reopened phase" --
re-run the eight-minute durability run at records and release checkpoints, record the
result with the commit it was taken on, and re-open the phase, by a records entry, in
the open, only if a run fails. Nothing ran those obligations: 42 merges, 0 re-runs.

    python3 tools/regression_watch.py obligation [--repo DIR] [--scratch DIR]
                                      [--timeout SECONDS] [--rerun] [--dry-run]

`obligation` decides, runs at most once, and records a terminal result:

* **NOT_NEEDED** -- the files the evidence is about (`durability_e2e.RELEVANT_PATHS`)
  are unchanged since the commit of the committed durability record. Exit 0, no run.
* **ALREADY** -- a watch record, committed or only written, exists for the same relevant
  tree (the same git object ids over `RELEVANT_PATHS`, so a records-only merge asks
  again for nothing), or an attempt marker with no record: `ALREADY:ATTEMPTED`. Exit 0
  for PASS, 1 for FAIL, 3 for ENVIRONMENT_GAP and ATTEMPTED; no run. `--rerun` is the
  one explicit way to ask again; it is never automatic.
* otherwise **one** CPU run of the existing instrument (`durability_e2e.py run` on an
  export of HEAD, then `derive`), about eight minutes, no provider, and one record
  `dogfood/<phase>-gate/watch/<UTC stamp>-<commit12>.json` with exactly one of

      PASS             every watched requirement satisfied, the harness clean
      FAIL             the run completed and a watched requirement is not satisfied:
                       the first real regression. A **reopen request** is written
                       beside it. A request is not a decision: the gate prints it, and
                       the phase is reopened only by a records entry that names it.
      ENVIRONMENT_GAP  the run could not be completed or judged (dirty tree, harness
                       failure, timeout, no git, instrument not falsifiable). Never a
                       pass, and never a regression either: no negative without a
                       proven root cause.

Exit codes: 0 PASS / NOT_NEEDED / ALREADY(PASS), 1 FAIL, 3 ENVIRONMENT_GAP (also
ALREADY:ATTEMPTED and ALREADY(GAP)), 2 refusal (usage, no --scratch, a name that would
overwrite another), 4 a write failed (disk full, permissions): visible, never a traceback.

Termination: one attempt per relevant tree, counted BEFORE it starts (a create-only
attempt marker `<gate dir>/watch-attempts/<stamp>-<commit12>-<tree16>.json`, so a second
invocation or a killed run is visible to the next call as ALREADY:ATTEMPTED), a bounded
timeout for the whole of run + derive, and no review, capsule, matrix, board or register
regeneration is triggered from here. The tool writes only the marker, the watch record
and, on FAIL, the request, create-only, into the working tree; committing them is the
records entry's act. A result that cannot be written is printed, with exit 4. The reboot
evidence (DS2/P2-11) is not re-taken at a records checkpoint (O268 section 5) and is not
judged here.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

PHASE = "P2"
WATCH_DIR = "watch"
ATTEMPT_DIR = "watch-attempts"
REQUEST_PREFIX = "REOPEN-REQUEST-"
RECORD_VERSION = 1
DURABILITY_RECORD = "dogfood/p2-durability/P2_DURABILITY_ACCEPTANCE.json"
REGISTER = "program/v3_3/REQUIREMENTS.json"
CLOSURE = "dogfood/p2-gate/CLOSURE.json"
DERIVER = "durability_e2e.py"
#: Probe keys the watch does not judge: the reboot evidence is taken again at a
#: release checkpoint with an operator-run restart, not at a records checkpoint.
NOT_RETAKEN = frozenset({"stressor:DS2"})
DEFAULT_TIMEOUT = 900
MAX_TIMEOUT = 3600
VERDICTS = ("PASS", "FAIL", "ENVIRONMENT_GAP")
EXIT = {"PASS": 0, "NOT_NEEDED": 0, "FAIL": 1, "ENVIRONMENT_GAP": 3, "ATTEMPTED": 3}
EXIT_WRITE_FAILED = 4
_SHA = re.compile(r"[0-9a-f]{40}")
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_ATTEMPT = re.compile(r"([0-9]{8}T[0-9]{6}Z-[0-9a-f]{12})-([0-9a-f]{16})\.json")
#: Ceilings on what is read back from committed watch records: the NEWEST records.
MAX_RECORDS = 256
MAX_BYTES = 1024 * 1024
MAX_REQUESTS = 64
#: The instrument unpacks the whole tree twice and starts processes; on a nearly full
#: file system a scenario fails without raising. The house floors for a run: fewer free
#: inodes or bytes than this and the run is not started, and a FAIL that came back with
#: the scratch below them is not a measurement of the product.
MIN_FREE_INODES = 2_000_000
MIN_FREE_BYTES = 60 * 1024 ** 3


def gate_dir(phase: str = PHASE) -> str:
    return f"dogfood/{phase.lower()}-gate"


def _durability():
    """`tools/durability_e2e.py` for `RELEVANT_PATHS` -- the single definition of
    what the evidence is about -- loaded without running anything."""
    name = "durability_e2e_for_watch"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / DERIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def relevant_paths() -> tuple[str, ...]:
    return tuple(_durability().RELEVANT_PATHS)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "surrogateescape")


def head_commit(repo: Path) -> str | None:
    rc, out = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    out = out.strip()
    return out if rc == 0 and _SHA.fullmatch(out) else None


def _blob(repo: Path, commit: str, path: str) -> bytes | None:
    rc, text = _git(repo, "cat-file", "blob", f"{commit}:{path}")
    if rc != 0:
        return None
    data = text.encode("utf-8", "surrogateescape")
    return data if len(data) <= MAX_BYTES else None


def _object(data: bytes | None) -> dict | None:
    if data is None:
        return None
    try:
        body = json.loads(data.decode("utf-8"))
    except (ValueError, RuntimeError):
        return None
    return body if isinstance(body, dict) else None


def relevant_tree(repo: Path, commit: str) -> str | None:
    """One digest of the git object ids of `RELEVANT_PATHS` at `commit` (a path the
    commit does not have contributes `<absent>`), or None when git cannot say. Two
    commits with the same value have the same relevant code, whatever else differs."""
    lines = []
    for path in relevant_paths():
        rc, out = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}")
        oid = out.strip() if rc == 0 else None
        if rc != 0:
            rc2, _ = _git(repo, "cat-file", "-e", commit)
            if rc2 != 0:
                return None
            oid = "<absent>"
        lines.append(f"{path}\0{oid}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def evidence_head(repo: Path, commit: str) -> str | None:
    """The commit the committed durability record's evidence was taken at."""
    record = _object(_blob(repo, commit, DURABILITY_RECORD))
    head = record.get("evidence_head") if record else None
    return head if isinstance(head, str) and _SHA.fullmatch(head) else None


def changed_paths(repo: Path, since: str, commit: str) -> list[str] | None:
    """Relevant files that differ between `since` and `commit` (committed state only,
    which is what a watch record is a statement about), or None if git cannot say."""
    rc, out = _git(repo, "diff", "--name-only", "--no-renames", since, commit,
                   "--", *relevant_paths())
    if rc != 0:
        return None
    return sorted(p for p in out.splitlines() if p.strip())


def stale_since(repo: Path, since: str, commit: str) -> str | None:
    """The first commit after `since` on the way to `commit` that touched a relevant
    path (what the dashboard names as "stale since"), or None."""
    rc, out = _git(repo, "log", "--reverse", "--format=%H", f"{since}..{commit}",
                   "--", *relevant_paths())
    first = out.split()[0] if rc == 0 and out.split() else None
    return first if first and _SHA.fullmatch(first) else None


def _tree_names(repo: Path, commit: str, directory: str) -> list[str]:
    """Names directly inside `directory` at `commit` (committed state), or []."""
    rc, out = _git(repo, "ls-tree", "-z", "--name-only", commit, "--", directory + "/")
    if rc != 0:
        return []
    return [n for n in out.split("\0") if n and n.rsplit("/", 1)[0] == directory]


def _disk_names(repo: Path, directory: str) -> list[str]:
    """Names directly inside `directory` in the working tree, whatever their kind."""
    try:
        return [f"{directory}/{e.name}" for e in os.scandir(repo / directory)]
    except OSError:
        return []


def _disk_object(repo: Path, name: str) -> dict | None:
    """A regular, non-symlink file of at most MAX_BYTES as a JSON object, or None."""
    path = repo / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return _object(path.read_bytes())
    except OSError:
        return None


def _is_record(body: dict | None) -> bool:
    return bool(body and body.get("record_version") == RECORD_VERSION
                and body.get("verdict") in VERDICTS
                and isinstance(body.get("commit"), str) and _SHA.fullmatch(body["commit"])
                and isinstance(body.get("relevant_tree"), str))


def watch_records(repo: Path, commit: str, phase: str = PHASE, *,
                  worktree: bool = False) -> list[dict]:
    """The watch records at `commit`, oldest first by file name, the NEWEST `MAX_RECORDS`
    of them (the ones that decide what is current). With `worktree`, records that are
    written but not yet committed count too (the obligation asks about those). A file
    that is not a readable object of the expected shape is left out (it can only make the
    watch run again, never read as a pass)."""
    directory = f"{gate_dir(phase)}/{WATCH_DIR}"
    committed = {n for n in _tree_names(repo, commit, directory) if n.endswith(".json")}
    local = ({n for n in _disk_names(repo, directory) if n.endswith(".json")}
             if worktree else set())
    out_records = []
    for name in sorted(committed | local)[-MAX_RECORDS:]:
        body = _object(_blob(repo, commit, name)) if name in committed else None
        if body is None and name in local:
            body = _disk_object(repo, name)
        if _is_record(body):
            out_records.append({**body, "file": name})
    return out_records


def attempts(repo: Path, commit: str, phase: str = PHASE, *,
             worktree: bool = False) -> list[dict]:
    """Attempt markers (committed, and with `worktree` also uncommitted), oldest first:
    `{file, stem, tree}`. Only the NAME is read: a marker counts whatever it contains,
    because its existence is the bound."""
    directory = f"{gate_dir(phase)}/{ATTEMPT_DIR}"
    names = set(_tree_names(repo, commit, directory))
    if worktree:
        names |= set(_disk_names(repo, directory))
    out = []
    for name in sorted(names):
        m = _ATTEMPT.fullmatch(name.rsplit("/", 1)[-1])
        if m:
            out.append({"file": name, "stem": m.group(1), "tree": m.group(2)})
    return out[-MAX_RECORDS:]


def last_watch(repo: Path, commit: str, phase: str = PHASE) -> dict | None:
    records = watch_records(repo, commit, phase)
    if not records:
        return None
    last = records[-1]
    return {"file": last["file"], "verdict": last["verdict"], "commit": last["commit"],
            "stamp": Path(last["file"]).name.split("-")[0],
            "relevant_tree": last["relevant_tree"]}


_PHASE_WORD = re.compile(r"[Pp]0*([0-9]{1,2})")
_COMMIT_PREFIX = re.compile(r"[0-9a-fA-F]{7,40}")


def names_closure(body: dict, phase: str, closed_at: str) -> bool:
    """Does a decision record name this closure -- the phase as a person writes it
    (`p2`, `P02`) and the closing commit as the full id or an abbreviation of at least
    seven hex digits, in either case? The same reading `build_plan` applies to a reopen
    decision, so a request is answered by exactly the records that reopen or keep."""
    word = body.get("phase")
    commit = body.get("closed_at_commit")
    m = _PHASE_WORD.fullmatch(word.strip()) if isinstance(word, str) else None
    if m is None or f"P{int(m.group(1))}" != phase or not isinstance(commit, str):
        return False
    prefix = commit.strip()
    return bool(_COMMIT_PREFIX.fullmatch(prefix)) and closed_at.startswith(prefix.lower())


def _requests_added(repo: Path, commit: str, gd: str) -> list[tuple[str, str]]:
    """`(file name, the oldest commit that added it)` for every reopen request ever
    ADDED to the gate directory on the way to `commit` -- not only those still present
    at `commit`: a request is answered by a decision, never by removing it. Raises when
    git cannot say (a silent empty answer would hide a regression)."""
    spec = f":(glob){gd}/{REQUEST_PREFIX}*.json"
    rc, out = _git(repo, "-c", "core.quotepath=off", "log", "--reverse", "--full-history", "-m",
                   "--diff-filter=A", "--no-renames", "--name-only", "--format=%x01%H",
                   commit, "--", spec)
    if rc != 0:
        raise RuntimeError("git cannot list the reopen requests")
    added: dict[str, str] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("\x01"):
            current = line[1:].strip()
        elif line.strip() and current and _SHA.fullmatch(current):
            added.setdefault(line.strip(), current)
    return sorted(added.items())[:MAX_REQUESTS]


def open_requests(repo: Path, commit: str, closed_at: str, phase: str = PHASE) -> list[dict]:
    """Reopen requests for the closure at `closed_at` that no decision answers. A request
    is read as it was ADDED (history), so removing the file, or a newer record, does not
    answer it. Only a committed `<gate dir>/reopen/*.json` whose `decision` is `REOPEN` or
    `KEEP_CLOSED`, that names the closure (`names_closure`) and carries `"request": <file>`
    answers it; any other record, a RESOLVED one included, does not. Unreadable files are
    not requests; a decision that cannot be read answers nothing. Raises when git cannot
    say."""
    gd = gate_dir(phase)
    answered = set()
    for name in _tree_names(repo, commit, f"{gd}/reopen"):
        if name.endswith(".json"):
            body = _object(_blob(repo, commit, name))
            if (body and body.get("decision") in ("REOPEN", "KEEP_CLOSED")
                    and isinstance(body.get("request"), str)
                    and names_closure(body, phase, closed_at)):
                answered.add(body["request"].strip().rsplit("/", 1)[-1])
    found = []
    for name, added_at in _requests_added(repo, commit, gd):
        base = name.rsplit("/", 1)[-1]
        if name != f"{gd}/{base}" or not base.endswith(".json") or base in answered:
            continue
        body = _object(_blob(repo, added_at, name))
        if not body or body.get("closed_at_commit") != closed_at or body.get("phase") != phase:
            continue
        failed = body.get("failed_requirements")
        found.append({"file": base, "commit": body.get("commit"),
                      "failed": [x[:40] for x in failed if isinstance(x, str)][:20]
                      if isinstance(failed, list) else []})
    return found


# --------------------------------------------------------------------------- #
# deciding, running, judging
# --------------------------------------------------------------------------- #

class RunResult:
    """What a runner hands back: nothing is judged by the runner."""

    def __init__(self, *, derived=None, meta=None, run_rc=None, derive_rc=None,
                 timed_out=False, note=""):
        self.derived, self.meta = derived, meta
        self.run_rc, self.derive_rc = run_rc, derive_rc
        self.timed_out, self.note = timed_out, note


def watched_requirements(repo: Path, commit: str) -> list[str]:
    """The requirement ids whose register probe reads the durability record, minus
    the reboot evidence (`NOT_RETAKEN`)."""
    register = _object(_blob(repo, commit, REGISTER)) or {}
    ids = []
    for r in register.get("requirements") or []:
        probe = r.get("probe") if isinstance(r, dict) else None
        if (isinstance(probe, dict) and probe.get("deriver") == DERIVER
                and probe.get("kind") == "evidence"
                and str(probe.get("key") or "") not in NOT_RETAKEN
                and isinstance(r.get("id"), str)):
            ids.append(r["id"])
    return sorted(set(ids))


def _descendants(root_pid: int) -> list[int]:
    """Every process below `root_pid` right now (Linux /proc parent links), whatever
    session it has -- the instrument starts its masters in sessions of their own."""
    parent: dict[int, list[int]] = {}
    try:
        entries = [e.name for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return []
    for entry in entries:
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                data = fh.read()
            ppid = int(data[data.rindex(b")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        parent.setdefault(ppid, []).append(int(entry))
    found, stack = [], [root_pid]
    while stack:
        for child in parent.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def _bounded(argv: list[str], cwd: Path, timeout: float) -> tuple[int | None, bool]:
    """Run `argv` in a session of its own and, at the timeout, kill what it started:
    its process group, and every descendant found below it (the instrument starts its
    masters with `start_new_session`, outside the group). Only processes below the one
    this call started are signalled. `(exit code, timed_out)`."""
    proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        below = _descendants(proc.pid)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pid in below:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.wait()
        return None, True


def default_runner(repo: Path, commit: str, scratch: Path, timeout: int) -> RunResult:
    """The existing instrument: `durability_e2e.py run` (it exports HEAD itself) and
    `derive`, in the repository, on a clean tree, under one timeout."""
    tool = repo / "tools" / DERIVER
    evidence = scratch / "evidence"
    base = scratch / "base"
    derived_path = scratch / "derived.json"
    started = time.monotonic()
    run_rc, timed_out = _bounded(
        [sys.executable, "-B", str(tool), "run", "--evidence", str(evidence),
         "--base", str(base)], repo, timeout)
    if timed_out:
        return RunResult(timed_out=True, note=f"the run exceeded {timeout} s")
    meta = _object((evidence / "meta.json").read_bytes()
                   if (evidence / "meta.json").is_file() else None)
    left = timeout - (time.monotonic() - started)
    if left < 1:
        return RunResult(meta=meta, run_rc=run_rc, timed_out=True,
                         note=f"no time left of the {timeout} s to derive")
    derive_rc, timed_out = _bounded(
        [sys.executable, "-B", str(tool), "derive", "--evidence", str(evidence),
         "--out", str(derived_path)], repo, left)
    if timed_out:
        return RunResult(meta=meta, run_rc=run_rc, timed_out=True,
                         note=f"derive exceeded what was left of the {timeout} s")
    derived = _object(derived_path.read_bytes() if derived_path.is_file() else None)
    return RunResult(derived=derived, meta=meta, run_rc=run_rc, derive_rc=derive_rc)


def judge(result: RunResult, commit: str, watched: list[str]) -> tuple[str, str, dict]:
    """`(verdict, reason, facts)` from what the runner returned. FAIL only for a run
    that completed cleanly and whose derivation says a watched requirement is not
    satisfied; everything that prevents that reading is ENVIRONMENT_GAP."""
    facts: dict = {}

    def gap(why: str):
        return "ENVIRONMENT_GAP", why, facts
    if result.timed_out:
        return gap(result.note or "timed out")
    if result.derived is None or result.meta is None:
        return gap("no derivable evidence: the run or the derivation produced none "
                   f"(run exit {result.run_rc}, derive exit {result.derive_rc})"
                   + (f"; {result.note}" if result.note else ""))
    meta, derived = result.meta, result.derived
    failures = meta.get("harness_failures")
    facts["harness_failures"] = failures if isinstance(failures, dict) else None
    if failures != {}:
        return gap("the harness reported failures (a scenario did not complete), "
                   "which is not a measurement of the product: "
                   + ", ".join(sorted(failures)[:5] if isinstance(failures, dict) else ["?"]))
    if meta.get("dirty") is not False or meta.get("dirty_at_end") is not False:
        return gap("the run was taken on a tree that was not clean, so its commit is "
                   "not what it measured")
    if derived.get("evidence_head") != commit:
        return gap(f"the evidence is about {str(derived.get('evidence_head'))[:12]}, "
                   f"not {commit[:12]}")
    facts["instrument_falsifiable"] = derived.get("instrument_falsifiable")
    facts["meta_problems"] = derived.get("meta_problems")
    if derived.get("instrument_falsifiable") is not True or derived.get("meta_problems"):
        return gap("the instrument is not falsifiable or lists meta problems; "
                   "nothing it says is read as a pass or a regression")
    requirements = derived.get("requirements")
    if not isinstance(requirements, dict) or not watched:
        return gap("the derivation names no requirements to judge")
    satisfied = {}
    for rid in watched:
        entry = requirements.get(rid)
        satisfied[rid] = bool(isinstance(entry, dict) and entry.get("satisfied") is True)
    facts["requirements"] = satisfied
    stressors = derived.get("stressors")
    facts["stressors"] = (dict(sorted(stressors.items()))
                          if isinstance(stressors, dict) else None)
    failed = sorted(rid for rid, ok in satisfied.items() if not ok)
    if failed:
        return "FAIL", f"{', '.join(failed)} not satisfied by the completed run", facts
    return "PASS", f"{len(satisfied)} watched requirement(s) satisfied", facts


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _write_new(path: Path, text: str) -> None:
    """Create-only: never overwrites another record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(text)


def _free(path: Path) -> tuple[int, int]:
    """`(free inodes, free bytes)` of the file system holding `path`."""
    st = os.statvfs(path)
    return st.f_favail, st.f_bavail * st.f_frsize


def _pressure(free: tuple[int, int]) -> str:
    """Why this much free space is too little for a run, or ""."""
    inodes, size = free
    if inodes < MIN_FREE_INODES:
        return f"{inodes} free inodes (floor {MIN_FREE_INODES})"
    if size < MIN_FREE_BYTES:
        return f"{size // 1024 ** 3} GiB free (floor {MIN_FREE_BYTES // 1024 ** 3} GiB)"
    return ""


def _standing(repo: Path, commit: str, tree: str) -> tuple[str, dict] | None:
    """The newest thing known about `tree`, committed or not: `("record", record)` or
    `("attempt", marker)` (an attempt no record has finished), or None. A record of the
    same stem finishes its marker."""
    items = [(Path(r["file"]).stem, 1, "record", r)
             for r in watch_records(repo, commit, worktree=True) if r["relevant_tree"] == tree]
    items += [(a["stem"], 0, "attempt", a)
              for a in attempts(repo, commit, worktree=True) if tree.startswith(a["tree"])]
    if not items:
        return None
    _, _, kind, body = max(items, key=lambda t: (t[0], t[1]))
    return kind, body


def obligation(repo: Path, *, runner=None, scratch: Path | None = None,
               timeout: int = DEFAULT_TIMEOUT, rerun: bool = False,
               dry_run: bool = False, now=None) -> tuple[str, str, int]:
    """Decide, run at most once, record. `(outcome, reason, exit_code)`; outcome is
    NOT_NEEDED, ALREADY:<verdict|ATTEMPTED>, RUN-not-made (dry run), a verdict, REFUSED
    (2) or WRITE_FAILED (4)."""
    repo = Path(repo)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
        return "REFUSED", f"timeout must be 1..{MAX_TIMEOUT} seconds", 2
    commit = head_commit(repo)
    if commit is None:
        return "ENVIRONMENT_GAP", "not a git repository with a HEAD", 3
    rc, top = _git(repo, "rev-parse", "--show-toplevel")
    if rc != 0 or Path(top.strip()).resolve() != repo.resolve():
        return "ENVIRONMENT_GAP", "not the top of a git repository of its own", 3
    since = evidence_head(repo, commit)
    if since is None:
        return "ENVIRONMENT_GAP", ("the committed durability record names no evidence "
                                   "commit, so staleness cannot be told"), 3
    changed = changed_paths(repo, since, commit)
    if changed is None:
        return "ENVIRONMENT_GAP", f"git cannot relate {since[:12]} to HEAD", 3
    if not changed:
        return "NOT_NEEDED", (f"the relevant paths are unchanged since the evidence "
                              f"commit {since[:12]}"), 0
    tree = relevant_tree(repo, commit)
    if tree is None:
        return "ENVIRONMENT_GAP", "git cannot read the relevant paths of HEAD", 3
    if not rerun:
        standing = _standing(repo, commit, tree)
        if standing and standing[0] == "record":
            v = standing[1]["verdict"]
            return f"ALREADY:{v}", (f"{standing[1]['file']} already holds {v} for this "
                                    "relevant tree; --rerun asks again"), EXIT[v]
        if standing:
            return "ALREADY:ATTEMPTED", (
                f"{standing[1]['file']} counts an attempt for this relevant tree that has no "
                "result record (the run was killed, is still running, or its result could "
                "not be written); a gap, never a pass. --rerun asks again"), EXIT["ATTEMPTED"]
    dirty_rc, dirty = _git(repo, "status", "--porcelain", "--untracked-files=all",
                           "--", *relevant_paths())
    if dry_run:
        return "RUN-not-made", (f"would run: {len(changed)} relevant path(s) changed "
                                f"since {since[:12]}; tree "
                                f"{'dirty' if dirty.strip() or dirty_rc else 'clean'}"), 0
    if dirty_rc != 0 or dirty.strip():
        # Refused before any run, and not recorded: a record is terminal for its
        # relevant tree, and a tree that is merely dirty today is not a result.
        return "ENVIRONMENT_GAP", ("the working tree has uncommitted changes under the "
                                   "relevant paths; the instrument would label its "
                                   "evidence dirty"), 3
    if scratch is None:
        return "REFUSED", ("a run needs --scratch DIR: a directory under a registered "
                           "scratch root (never /tmp); each run unpacks the whole tree"), 2
    scratch = Path(scratch)
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        free_before = _free(scratch)
    except OSError as exc:
        return "WRITE_FAILED", f"the scratch directory is not usable: {exc}", EXIT_WRITE_FAILED
    why = _pressure(free_before)
    if why:
        # Not recorded and not counted, like a dirty tree: the pressure is today's.
        return "ENVIRONMENT_GAP", (f"the scratch file system has {why}; the run is not "
                                   "started, because a failure under pressure is not a "
                                   "measurement of the product"), 3
    started = _stamp(now)
    stem = f"{started}-{commit[:12]}"
    watch_path = repo / gate_dir() / WATCH_DIR / f"{stem}.json"
    marker = repo / gate_dir() / ATTEMPT_DIR / f"{stem}-{tree[:16]}.json"
    if watch_path.exists() or marker.exists():
        return "REFUSED", f"{stem} exists; a record or an attempt is never overwritten", 2
    # The attempt is counted BEFORE it starts: a second invocation, or a run that is
    # killed, finds the marker and does not run again without --rerun.
    try:
        _write_new(marker, _json_text({
            "record_version": RECORD_VERSION, "kind": "regression-watch-attempt",
            "phase": PHASE, "commit": commit, "relevant_tree": tree, "started_at": started,
            "pid": os.getpid(), "timeout_s": timeout,
            "note": ("An attempt with no watch record of the same stem was killed, is "
                     "running, or lost its result: ALREADY:ATTEMPTED until --rerun.")}))
    except FileExistsError:
        return "REFUSED", f"{marker.name} exists; an attempt is never overwritten", 2
    except OSError as exc:
        return "WRITE_FAILED", (f"the attempt marker cannot be written ({exc}); "
                                "nothing was run"), EXIT_WRITE_FAILED
    t0 = time.monotonic()
    runner = runner or default_runner
    try:
        with tempfile.TemporaryDirectory(dir=str(scratch), prefix="regression-watch-",
                                         ignore_cleanup_errors=True) as td:
            try:
                result = runner(repo, commit, Path(td), timeout)
            except Exception as exc:  # noqa: BLE001 - a runner that fails is a gap
                result = RunResult(note=f"the runner failed ({type(exc).__name__}): {exc}")
            free_after = _free(scratch)
    except OSError as exc:
        return "WRITE_FAILED", (f"the scratch directory could not be used ({exc}); the "
                                "attempt is counted: ALREADY:ATTEMPTED until --rerun"), EXIT_WRITE_FAILED
    verdict, reason, facts = judge(result, commit, watched_requirements(repo, commit))
    squeezed = _pressure(free_after)
    if verdict == "FAIL" and squeezed:
        verdict = "ENVIRONMENT_GAP"
        reason = (f"{reason}; but the scratch file system ended with {squeezed}: a failure "
                  "under resource pressure is not a measurement of the product")
    facts["scratch_free"] = {"inodes_before": free_before[0], "bytes_before": free_before[1],
                             "inodes_after": free_after[0], "bytes_after": free_after[1]}
    record = {
        "record_version": RECORD_VERSION, "kind": "regression-watch", "phase": PHASE,
        "verdict": verdict, "reason": reason, "commit": commit, "relevant_tree": tree,
        "evidence_head_before": since, "relevant_paths_changed": changed[:40],
        "attempt": marker.name,
        "started_at": started, "finished_at": _stamp(), "duration_s": round(time.monotonic() - t0),
        "runner": "tools/durability_e2e.py run + derive (one run per relevant tree)",
        "agent_execution": "deterministic fixture; role runs are not exercised here",
        "limits": ["the reboot evidence (DS2, P2-11) is not re-taken at a records "
                   "checkpoint; it is taken at a release checkpoint (A7)",
                   "a PASS is a statement about this commit's relevant paths; it does "
                   "not re-point the register's probe"],
        **facts}
    if result.derived is not None:
        record["derived_sha256"] = hashlib.sha256(
            json.dumps(result.derived, sort_keys=True).encode("utf-8")).hexdigest()
    text = _json_text(record)

    def lost(what: str, exc: OSError) -> tuple[str, str, int]:
        # The eight-minute result must not be lost with the write: it is printed whole.
        return "WRITE_FAILED", (f"{what} could not be written ({exc}); the result is NOT "
                                f"saved: {verdict}: {reason}\n{text}"), EXIT_WRITE_FAILED
    if verdict == "FAIL":
        # The request first: once it exists the gate shows the regression, whatever
        # happens to the record.
        closed_at = _object(_blob(repo, commit, CLOSURE) or None) or {}
        request = {
            "record_version": RECORD_VERSION, "kind": "reopen-request", "phase": PHASE,
            "decision": "PENDING", "closed_at_commit": closed_at.get("closed_at_commit"),
            "commit": commit, "failed_requirements": [
                rid for rid, ok in (facts.get("requirements") or {}).items() if not ok],
            "failing_run": f"{gate_dir()}/{WATCH_DIR}/{stem}.json",
            "note": ("A request is not a decision (A7 rule 3). The phase is reopened "
                     f"only by a committed {gate_dir()}/reopen/<name>.json with decision "
                     "REOPEN naming closed_at_commit; one with decision KEEP_CLOSED and "
                     "\"request\": this file's name answers it without reopening.")}
        try:
            _write_new(repo / gate_dir() / f"{REQUEST_PREFIX}{stem}.json", _json_text(request))
        except FileExistsError:
            return "REFUSED", f"{REQUEST_PREFIX}{stem}.json exists; never overwritten", 2
        except OSError as exc:
            return lost("the reopen request", exc)
    try:
        _write_new(watch_path, text)
    except FileExistsError:
        return "REFUSED", f"{watch_path.name} exists; a record is never overwritten", 2
    except OSError as exc:
        return lost("the watch record", exc)
    return verdict, reason, EXIT[verdict]


def _json_text(body: dict) -> str:
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("obligation")
    o.add_argument("--repo", default=str(HOH))
    o.add_argument("--scratch", default=None,
                   help="REQUIRED for a run: a directory under a registered scratch root "
                        "(never /tmp) for the run's temporary files")
    o.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    o.add_argument("--rerun", action="store_true",
                   help="ask again although a record exists for this relevant tree")
    o.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        outcome, reason, code = obligation(
            Path(a.repo).resolve(), scratch=Path(a.scratch) if a.scratch else None,
            timeout=a.timeout, rerun=a.rerun, dry_run=a.dry_run)
    except OSError as exc:  # never a traceback with the FAIL code
        outcome, reason, code = "WRITE_FAILED", f"{type(exc).__name__}: {exc}", EXIT_WRITE_FAILED
    print(f"{outcome}: {reason}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
