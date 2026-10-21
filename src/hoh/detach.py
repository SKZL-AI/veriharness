"""`hoh run RUN --detach`: start the run in a tab of its own and return once it executes
(increment I11 slice 1, plan row P5-42, Amendment A6).

What it does, said exactly. It decides, with the functions `hoh run` itself uses and without
writing anything, whether `hoh run` would start (`preflight`); opens one Herdr tab for the run
and records it as the run's own, role `program:run-host`, in `owned-surfaces.jsonl` through
`surfaces.open_surface` (so `hoh surface list|reap` and the end of `hoh run` see it); writes the
log file and a create-only launch record beside it (`logs/detached-<UTC>.log` and `.json`);
types ONE command into that tab -- the same `hoh run` with the same options, without
`--detach`, its output copied to the log; and returns one JSON line **once the run's
process lock was taken by a process that wrote a new marker** (`process.json` differs from what
it was when the launch began; `RunStore.running_process` says whether the lock is still held).
`hoh wait RUN` right after it cannot read `idle` for a run that has not started (limit L1 of
`hoh wait`).

What it is not (stated, not built; slice list in the spec): no named run profile, no dedicated
pinned clone, no contract derived from the spec, no `--from-launch` re-entry, no storage claim
of its own, no close of the tab, no wait for the run to end. Exit codes: 0 started; 2 refused
(nothing was created, no role was started) or the command itself failed; 3 the tab exists but
the run was not seen executing within the deadline (the tab stays, owned and recorded).

Nothing here removes a file, signals a process or closes a tab.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "hoh-detach/1"
ROLE = "run-host"
STARTED, REFUSED, NOT_STARTED = 0, 2, 3

DEFAULT_START_TIMEOUT = 60.0
MAX_START_TIMEOUT = 600.0
POLL = 0.25

#: Taken from the caller's environment, when set, and given to the tab: a tab Herdr opens
#: starts from the Herdr server's environment, not from the caller's. Nothing else is passed
#: (a key or a token in the caller's environment never reaches a tab or a record).
ENV_ALLOW = ("HOH_RUNS", "HOH_STATE_ROOT", "HOH_PYTHON", "PYTHONPATH", "XDG_STATE_HOME")
ENV_FIXED = {"PYTHONUNBUFFERED": "1"}

#: The only characters an element of the typed command may hold; a word may not begin with `=`
#: (zsh expands `=cmd`). A shell never has to quote such a word, so no shell (bash, zsh, fish)
#: can read it as anything but itself.
SAFE_WORD = re.compile(r"[A-Za-z0-9_@%+:,./-][A-Za-z0-9_@%+=:,./-]*\Z")


class DetachRefused(Exception):
    """The run is not started and nothing was created."""


# --------------------------------------------------------------------------- #
# The command the tab runs
# --------------------------------------------------------------------------- #


def _abs(path) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def _opt(flag: str, value) -> str:
    """One valued option as ONE word, `--flag=value`: a value that begins with `-` is then never
    read as an option by the parser of the tab. An empty value is refused (`--flag=` would be
    typed, and the first slice refused the empty word)."""
    text = str(value)
    if text == "":
        raise DetachRefused(
            "'' cannot be typed into a shell without quoting "
            f"(an empty value for {flag}); give it a value")
    return f"{flag}={text}"


def _refuse_dash_run_id(args) -> None:
    """A run id that begins with `-` is read as an option by the parser of the tab and cannot be
    typed as a positional word; refused before anything starts."""
    if str(args.run_id).startswith("-"):
        raise DetachRefused(
            f"the run id {args.run_id!r} begins with '-' and cannot be typed into the tab as "
            "the run id; give the run another id")


def run_argv(args, hoh_prefix: list[str]) -> list[str]:
    """`hoh run` with the options `args` holds, without `--detach`. Paths are made absolute
    here: the tab's directory is the run's, not the caller's. Every option with a value is one
    word, `--flag=value` (`_opt`)."""
    argv = [*hoh_prefix, _opt("--root", _abs(args.root)), "run", args.run_id,
            _opt("--iterations", args.iterations)]
    if args.until_accepted:
        argv.append("--until-accepted")
    # a role option the caller did not type is not typed: the tab's `hoh run` then applies the
    # same rule (the routing policy's provider, else the default) to the same absence
    from .cli import profile_typed
    for flag, value in (("--planner", args.planner), ("--developer", args.developer),
                        ("--qa", args.qa)):
        if profile_typed(value):
            argv.append(_opt(flag, value))
    if args.no_herdr:
        argv.append("--no-herdr")
    if args.isolation is not None:
        argv.append(_opt("--isolation", args.isolation))
    for flag, values in (("--role-model", args.role_model), ("--role-effort", args.role_effort),
                         ("--role-account", args.role_account)):
        for value in values:
            argv.append(_opt(flag, value))
    if args.master_session is not None:
        argv.append(_opt("--master-session", args.master_session))
    if args.master_epoch is not None:
        argv.append(_opt("--master-epoch", args.master_epoch))
    argv.append(_opt("--role-timeout", args.role_timeout))
    if args.approval_policy:
        argv.append(_opt("--approval-policy", _abs(args.approval_policy)))
    if args.autonomy:
        argv.append(_opt("--autonomy", _abs(args.autonomy)))
    if args.routing_policy is not None:
        argv.append(_opt("--routing-policy", _abs(args.routing_policy)))
    # P3-INV1: the ceiling of the shared dispatch budget's hold goes into the tab, so the tab's
    # `hoh run` stops where the hold ends (an option dropped here would leave the run unbounded)
    if getattr(args, "dispatch_ceiling", None) is not None:
        argv.append(_opt("--dispatch-ceiling", args.dispatch_ceiling))
    # P3-11 slice 2: the environment requirement goes into the tab, so the tab's own `hoh run`
    # asks the report again when it starts (the caller asked once, now; a tab can start later).
    # Three together or none: `hoh run` refused a partial set before it came here.
    given = [getattr(args, name, None) for name in
             ("preflight_report", "preflight_profile", "preflight_max_age")]
    if any(value is not None for value in given):
        if any(value is None for value in given):
            raise DetachRefused("--preflight-report, --preflight-profile and --preflight-max-age "
                                "go together")
        argv.append(_opt("--preflight-report", _abs(given[0])))
        argv.append(_opt("--preflight-profile", given[1]))
        argv.append(_opt("--preflight-max-age", given[2]))
    if args.program_epoch is not None:
        argv.append(_opt("--program-epoch", args.program_epoch))
    if args.trust_worktree:
        argv.append("--trust-worktree")
    # Always typed, resolved here: a tab resolves `$HOH_STATE_ROOT` / XDG from the environment it
    # starts with, which need not be the caller's, and the preflight judged this root.
    from .cli import resolve_storage_root
    argv.append(_opt("--storage-root", _abs(resolve_storage_root(args.storage_root))))
    return argv


def environment() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOW if os.environ.get(k)}
    env.update(ENV_FIXED)
    return env


def command_text(argv: list[str], log: str) -> str:
    """The one line typed into the tab. Every word is safe (`SAFE_WORD`) or the launch is
    refused; the output of the run is shown in the tab and appended to the log."""
    for word in [*argv, log]:
        if not SAFE_WORD.match(word):
            raise DetachRefused(
                f"{word!r} cannot be typed into a shell without quoting (letters, digits and "
                "_ @ % + = : , . / - only); use a path without other characters")
    return " ".join(argv) + f" 2>&1 | tee -a {log}"


# --------------------------------------------------------------------------- #
# What `hoh run` would refuse, decided without writing anything
# --------------------------------------------------------------------------- #


def preflight(args, store):
    """Everything `hoh run` checks before its first role that can be checked without a write:
    returns the parsed state. Raises `DetachRefused` with the reason. The functions are the
    ones `_run_in` calls, in its order; what stays for the tab is named in the spec (limit L2).
    """
    from . import cli, surfaces
    from .store import RunStore

    if not store.state_exists():
        raise DetachRefused(f"there is no run {args.run_id} under {args.root}")
    try:
        state = RunStore._parse_state(store.state_path.read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001 - unreadable proves nothing
        raise DetachRefused(f"the state of run {args.run_id} cannot be read: {exc}") from exc
    over, why = surfaces.run_over(store.dir)
    if over:
        raise DetachRefused(f"the run is already over ({why})")
    held = store.running_process()
    if held is not None:
        raise DetachRefused(f"run {args.run_id} is already being executed "
                            f"(pid {held.get('pid', '?')}, since {held.get('since', '?')})")

    # Storage: the same refusal `hoh run` gives, without claiming the boot.
    try:
        cli.storage_admission_for(args.storage_root, claim=False)
    except cli.StorageRefused as exc:
        raise DetachRefused(str(exc)) from exc

    try:
        # a blank role option (`--qa "  "`) names no harness: the words `hoh run` gives. An empty
        # one (`--qa ""`) keeps its existing refusal, from `_opt` when the command is built.
        for role_, value in ((cli.Role.PLANNER, args.planner), (cli.Role.DEVELOPER, args.developer),
                             (cli.Role.QA, args.qa)):
            if value != "":
                cli._profile_of(role_, value)
        cli._per_role(args.role_model)
        cli._per_role(args.role_effort)
    except ValueError as exc:
        raise DetachRefused(str(exc)) from exc

    if args.approval_policy:
        from .approval_policy import PolicyRefused, load
        try:
            load(args.approval_policy)
        except PolicyRefused as exc:
            raise DetachRefused(f"approval policy refused: {exc}") from exc

    from . import routing
    try:
        cli._routing_decide(store.dir, args.routing_policy)
    except (cli.RoutingPinRefused, routing.RoutingRefused) as exc:
        raise DetachRefused(f"routing policy refused: {exc}") from exc

    from .governance import ContractRefused, bind
    offered = None
    if args.autonomy:
        from .autonomy import AutonomyContract
        try:
            offered = AutonomyContract.model_validate_json(
                Path(args.autonomy).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DetachRefused(f"autonomy contract refused: {exc}") from exc
    try:
        governor = bind(store.dir, contract=offered, program_epoch=args.program_epoch,
                        write=False)
    except ContractRefused as exc:
        raise DetachRefused(f"autonomy contract refused: {exc}") from exc
    except OSError as exc:
        raise DetachRefused(f"the autonomy contract could not be checked: {exc}") from exc
    if args.require_contract and governor is None:
        raise DetachRefused("--require-contract: the run has no autonomy contract, pinned or "
                            "offered (--autonomy PATH)")

    from . import dispatchrecovery
    try:
        dispatchrecovery.parse_role_accounts(args.role_account or [])
        dispatchrecovery.check_master_args(args.master_session, args.master_epoch)
    except ValueError as exc:
        raise DetachRefused(f"recovery declaration refused: {exc}") from exc

    try:
        isolation = cli._isolation_for_run(state, args.isolation)
    except ValueError as exc:
        raise DetachRefused(str(exc)) from exc
    if isolation is not None:
        from .sandbox import SandboxUnavailable, select
        try:
            select(isolation)
        except SandboxUnavailable as exc:
            raise DetachRefused(f"--isolation {args.isolation} was requested and cannot be "
                                f"provided here: {exc}") from exc

    # The tab starts in the run directory, not in the caller's: `python -m hoh.cli` has to be
    # importable from there with the environment the tab gets.
    from .launcher import hoh_command
    interpreter = hoh_command()[0]
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update(environment())
    why = import_probe(interpreter, env, str(store.dir))
    if why:
        raise DetachRefused(f"{interpreter} cannot import hoh.cli from the run directory with the "
                            f"environment the tab gets (set PYTHONPATH to the source tree, or "
                            f"install the package): {why}")
    return state


def import_probe(interpreter: str, env: dict, cwd: str) -> str:
    """'' when `interpreter -c "import hoh.cli"` succeeds in `cwd` with exactly `env`, else why.
    The one place this module starts a process; it runs no part of the run."""
    import subprocess

    try:
        done = subprocess.run([interpreter, "-c", "import hoh.cli"], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "the import did not finish within 60 s"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    if done.returncode == 0:
        return ""
    lines = [ln for ln in (done.stderr or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else f"exit {done.returncode}")[:200]


# --------------------------------------------------------------------------- #
# The start
# --------------------------------------------------------------------------- #


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _marker(store) -> dict | None:
    """What `process.json` holds now, or None when it is not there or cannot be read (the
    baseline of a launch: a run never started has none)."""
    try:
        return json.loads(store.process_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _marker_strict(store) -> dict | None:
    """The same read for the wait: a file that is not there is None; a file that is there and
    cannot be read is an error, which `detach` answers as exit 3 (the tab exists)."""
    try:
        return json.loads(store.process_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def await_start(store, before, *, timeout: float, poll: float = POLL):
    """Poll until `process.json` is not what it was when the launch began: a process held the
    lock and wrote its own marker. That file is durable, the lock is not -- a run that ends in
    less than a poll interval (a refusal inside `hoh run`, `--iterations 0`) would otherwise
    read as never started. Returns (marker or None, process, polls, waited); `process` is
    `running` (the lock is held now), `ended` (the process came and went in order), `died` (a
    `running` marker and a free lock: killed), or None. Never sleeps a non-positive time."""
    began = time.monotonic()
    polls = 0
    while True:
        polls += 1
        marker = _marker_strict(store)
        if marker is not None and marker != before:
            if marker.get("state") == "ended":
                return marker, "ended", polls, time.monotonic() - began
            if marker.get("state") == "running":
                if store.running_process() is not None:
                    return marker, "running", polls, time.monotonic() - began
                # The lock is free: the marker is final (`ended` is written before the lock is
                # released), so read it again -- the run may have ended between the two reads.
                again = _marker_strict(store)
                if again is not None and again != before and again.get("state") == "ended":
                    return again, "ended", polls, time.monotonic() - began
                return marker, "died", polls, time.monotonic() - began
        remaining = timeout - (time.monotonic() - began)
        if remaining <= 0:
            return None, None, polls, time.monotonic() - began
        time.sleep(min(poll, remaining))


def start_timeout_arg(value: float | None) -> float:
    if value is None:
        return DEFAULT_START_TIMEOUT
    if not (isinstance(value, (int, float)) and math.isfinite(value) and 1 <= value <= MAX_START_TIMEOUT):
        raise DetachRefused(f"--detach-timeout takes 1 to {int(MAX_START_TIMEOUT)} seconds")
    return float(value)


def render(answer: dict) -> str:
    return json.dumps(answer, ensure_ascii=True)


def detach(args, store) -> tuple[int, dict]:
    """The whole launch. Returns (exit code, answer); `DetachRefused` for exit 2."""
    from . import surfaces
    from .agentcontrol import OwnedAgentControl
    from .durable_io import durable_create
    from .launcher import hoh_command

    _refuse_dash_run_id(args)
    timeout = start_timeout_arg(args.detach_timeout)
    state = preflight(args, store)
    stamp = _utc_stamp()
    logs = store.dir / "logs"
    log, record = logs / f"detached-{stamp}.log", logs / f"detached-{stamp}.json"
    argv = run_argv(args, hoh_command())
    command = command_text(argv, str(log))
    env = environment()
    try:
        control = surfaces.make_control()
        surface = surfaces.open_surface(
            store.dir, role=ROLE, label=f"run-host-{args.run_id}"[:80], cwd=str(store.dir),
            program=state.project_name, control=control, env=env)
    except Exception as exc:                          # noqa: BLE001 - nothing exists yet
        raise DetachRefused(f"the run host could not be opened: {exc}") from exc
    answer = {"schema": SCHEMA, "run_id": args.run_id, "started": False, "exit_code": NOT_STARTED,
              "why": None, "process": None, "pid": None, "process_since": None,
              "process_ended": None, "pane_id": surface.pane_id,
              "tab_id": surface.tab_id, "registry": str(store.dir / surfaces.SURFACES_FILE),
              "log": str(log), "launch": str(record), "command": command,
              "argv_digest": None, "waited_s": None, "polls": 0}
    before = _marker(store)
    digest = hashlib.sha256(json.dumps({"argv": argv, "env": env}, sort_keys=True,
                                       ensure_ascii=True).encode("ascii")).hexdigest()
    try:
        logs.mkdir(exist_ok=True)
        durable_create(log, f"# detached launch {stamp} of run {args.run_id}\n")
        durable_create(record, json.dumps({
            "schema": SCHEMA, "run_id": args.run_id, "launched_at": stamp, "argv": argv,
            "env": env, "cwd": str(store.dir), "command": command, "log": str(log),
            "pane_id": surface.pane_id, "tab_id": surface.tab_id, "role": f"program:{ROLE}",
            "start_timeout_s": timeout, "argv_digest": digest,
            "marker_before": before}, indent=2, ensure_ascii=True) + "\n")
        answer["argv_digest"] = digest
        OwnedAgentControl(control, surfaces.registry_for(store.dir)).run_in_pane(
            surface.pane_id, command, purpose=f"start run {args.run_id}")
    except Exception as exc:                          # noqa: BLE001 - the tab exists: say so
        answer["why"] = f"the command was not sent: {type(exc).__name__}: {exc}"
        return NOT_STARTED, answer
    try:
        marker, process, polls, waited = await_start(store, before, timeout=timeout)
    except Exception as exc:                          # noqa: BLE001 - the tab exists: exit 3, not 2
        answer["why"] = (f"the wait for the process failed: {type(exc).__name__}: {exc}; "
                         f"the tab {surface.tab_id} stays, owned and recorded")
        return NOT_STARTED, answer
    answer["polls"], answer["waited_s"] = polls, round(waited, 3)
    if marker is None:
        answer["why"] = (f"no process wrote a new marker for the run within {timeout:g} s; "
                         f"the tab {surface.tab_id} stays, owned and recorded")
        return NOT_STARTED, answer
    answer.update(process=process, pid=marker.get("pid"), process_since=marker.get("since"),
                  process_ended=marker.get("ended"))
    if process == "died":
        answer["why"] = ("the process wrote its marker and is gone without ending it (killed); "
                         f"the tab {surface.tab_id} stays, owned and recorded")
        return NOT_STARTED, answer
    answer.update(started=True, exit_code=STARTED)
    return STARTED, answer


def cmd_detach(args) -> int:
    """`hoh run RUN --detach [run options]`: see the module docstring."""
    from .store import RunStore, StoreError

    try:
        if not getattr(args, "detach", False):
            raise DetachRefused("--detach-timeout and --require-contract need --detach")
        store = RunStore(args.root, args.run_id)
        code, answer = detach(args, store)
    except (DetachRefused, StoreError) as exc:
        print(f"run --detach refused: {exc}", file=sys.stderr)
        return REFUSED
    except Exception as exc:                          # noqa: BLE001 - exit 3 is an outcome, never a crash
        print(f"run --detach failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return REFUSED
    try:
        print(render(answer), flush=True)
    except (OSError, ValueError) as exc:
        print(f"run --detach: the answer could not be written: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return code
