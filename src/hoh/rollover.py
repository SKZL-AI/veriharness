"""Context rollover, Layer A: a fresh orchestrator session takes the master
role through a capsule it re-derives -- or the old session stays master.

`hoh.succession_capsule` is the *document* (what is bound, how a successor
re-derives it). This module is the *protocol* around it:

* `evaluate_trigger` -- the only trigger of Layer A: one pure function over a
  transcript's last request. (The multi-trigger policy is Layer B.)
* `RolloverLog` -- an append-only state machine
  `ACTIVE -> ROTATION_PENDING -> DRAINING -> CAPSULE_FROZEN -> SUCCESSOR_STARTING
  -> SUCCESSOR_VALIDATING -> SUCCESSOR_ACTIVE -> PREDECESSOR_RETIRED`; an event
  that cannot follow the last one is refused, so nothing is skipped.
* `run_rollover` -- the predecessor: freeze, launch a successor with a
  *pointer prompt* only, wait for its acknowledgement while keeping the lease
  alive, hand over for a bounded window, and either see the successor hold the
  next epoch or take the program back.
* `Successor` -- the fresh session: verify, acknowledge, wait for the handover,
  **derive everything again before taking anything**, acquire, write the
  receipt, hand the token over.
* `hold_lease` -- a lease is judged by its holder's pid, and a conversation is
  not a long-lived process, so something has to stay alive and renew it.

The invariant that holds the whole thing: at no moment are two sessions
masters. The predecessor's `handoff` releases the lease before the successor's
`acquire` begins an epoch, both serialised by the program's write lock, and the
successor takes a lease only if it is *this capsule's* handover.

Nothing is deleted here, no transcript is read except by the trigger (which
reads counts, not text), and the successor path never touches one.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .durable_io import durable_append, durable_create, durable_write
from .epoch import MasterEpoch, Reason
from .store import LockBusy
from .succession import (
    ExpectedHandover, Lease, LeaseError, LeaseHeld, MasterAuthority, StaleMaster,
    pid_alive, utc_now,
)
from .succession_capsule import (
    CapsuleError, CapsuleInput, FrozenCapsule, Observer, Verification, ack_path,
    ack_problems, aborted_path, build_ack, build_receipt, check_capsule_id, freeze_capsule,
    load_capsule, new_capsule_id, plus_seconds, receipt_path, rollover_dir,
    verify_capsule,
)

__all__ = [
    "DEFAULT_THRESHOLD", "HerdrLauncher", "HoldResult", "LaunchHandle", "LauncherError",
    "Outcome", "RolloverError", "RolloverLog", "RolloverResult", "State", "Successor",
    "SuccessorLauncher", "TokenOutError", "TookOver", "TriggerError", "TriggerResult",
    "evaluate_trigger", "hand_token_over", "hold_lease", "pointer_prompt",
    "product_invocation", "run_rollover",
]


class RolloverError(RuntimeError):
    """A rollover step that is refused. The programme is left as it was."""


class TokenOutError(RolloverError):
    """The master's token could not be handed over; the lease was given back."""


# --------------------------------------------------------------------------- #
# the trigger
# --------------------------------------------------------------------------- #

#: The context, in tokens, at which a rotation is due. A value of the
#: orchestrator's own measured cost, not a model limit (see the spec's Goal).
DEFAULT_THRESHOLD = 250_000

#: Only the tail of a transcript is read: the last request is at its end, and
#: a long programme's transcript is hundreds of megabytes.
MAX_TAIL_BYTES = 8 * 1024 * 1024

#: With no usable request in the tail, the file's size stands in -- as an
#: estimate and labelled one. A transcript line carries far more than the
#: context it contributes, so this budget is deliberately generous.
DEFAULT_SIZE_BUDGET_BYTES = 16 * 1024 * 1024

_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class TriggerError(RuntimeError):
    """The transcript cannot be read. Never a pass: an unreadable transcript
    is not evidence that no rotation is due."""


@dataclass(frozen=True)
class TriggerResult:
    fire: bool
    context_tokens: int | None
    threshold: int
    reason: str            # "context_tokens" (measured) or "size_budget" (estimate)
    estimate: bool
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {"fire": self.fire, "context_tokens": self.context_tokens,
                "threshold": self.threshold, "reason": self.reason,
                "estimate": self.estimate, "size_bytes": self.size_bytes}


def _request_context(record: Any) -> int | None:
    """The context a transcript record measures, or None when it is not a
    measurement of *the conversation's own* last request: a sub-agent
    (`isSidechain`) or synthetic record, a missing, non-integer or zero count."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    if record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("model") == "<synthetic>" or record.get("model") == "<synthetic>":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return None
    total = 0
    for key in _USAGE_KEYS:
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        total += value
    return total or None


def evaluate_trigger(transcript: Path | str, *, threshold: int = DEFAULT_THRESHOLD,
                     max_tail_bytes: int = MAX_TAIL_BYTES,
                     size_budget_bytes: int = DEFAULT_SIZE_BUDGET_BYTES) -> TriggerResult:
    """Is a rotation due? Pure over the transcript's last request.

    The context of the *last* request of the conversation itself is
    `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of
    the last `assistant` record that has usage. It fires at `>= threshold`.
    """
    path = Path(transcript)
    try:
        size = path.stat().st_size
        start = max(0, size - int(max_tail_bytes))
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError as exc:
        raise TriggerError(f"cannot read the transcript {path}: {exc}") from exc
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]               # the window opens mid-line
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue                    # torn or foreign: skipped, never guessed
        tokens = _request_context(record)
        if tokens is not None:
            return TriggerResult(fire=tokens >= threshold, context_tokens=tokens,
                                 threshold=threshold, reason="context_tokens",
                                 estimate=False, size_bytes=size)
    return TriggerResult(fire=size >= size_budget_bytes, context_tokens=None,
                         threshold=threshold, reason="size_budget", estimate=True,
                         size_bytes=size)


# --------------------------------------------------------------------------- #
# the state machine
# --------------------------------------------------------------------------- #


class State(StrEnum):
    ACTIVE = "ACTIVE"
    ROTATION_PENDING = "ROTATION_PENDING"
    DRAINING = "DRAINING"
    CAPSULE_FROZEN = "CAPSULE_FROZEN"
    SUCCESSOR_STARTING = "SUCCESSOR_STARTING"
    SUCCESSOR_VALIDATING = "SUCCESSOR_VALIDATING"
    SUCCESSOR_ACTIVE = "SUCCESSOR_ACTIVE"
    PREDECESSOR_RETIRED = "PREDECESSOR_RETIRED"


#: The state an event leaves the log in. `abort` and `reclaimed` return to
#: ACTIVE: the predecessor is master again.
_STATE_AFTER: dict[str | None, State] = {
    None: State.ACTIVE,
    "pending": State.ROTATION_PENDING,
    "draining": State.DRAINING,
    "frozen": State.CAPSULE_FROZEN,
    "launched": State.SUCCESSOR_STARTING,
    "ack": State.SUCCESSOR_VALIDATING,
    "handoff": State.SUCCESSOR_VALIDATING,
    "complete": State.SUCCESSOR_ACTIVE,
    "retired": State.PREDECESSOR_RETIRED,
    "abort": State.ACTIVE,
    "reclaimed": State.ACTIVE,
}

#: What may follow what. Nothing is skipped: no handoff without a frozen
#: capsule and an acknowledgement. A cycle may *start* from any state in which
#: no cycle is in flight; `complete` is one of them -- the successor holds the
#: epoch, and `retired` is only the predecessor's own bookkeeping.
_START = frozenset({"pending", "draining"})
_NEXT: dict[str | None, frozenset[str]] = {
    None: _START,
    "pending": frozenset({"draining", "abort"}),
    "draining": frozenset({"frozen", "abort"}),
    "frozen": frozenset({"launched", "abort"}),
    "launched": frozenset({"ack", "abort"}),
    "ack": frozenset({"handoff", "abort"}),
    "handoff": frozenset({"complete", "reclaimed", "abort"}),
    "complete": frozenset({"retired"}) | _START,
    "retired": _START,
    "abort": _START,
    "reclaimed": _START,
}

_IDLE = frozenset({State.ACTIVE, State.ROTATION_PENDING, State.PREDECESSOR_RETIRED,
                   State.SUCCESSOR_ACTIVE})

#: The slack added to a waiting state's own deadline, so a predecessor that is
#: still working when the deadline falls is not pre-empted by the next start.
_GRACE_S = 60


class RolloverLog:
    """`rollover/rollover.log.jsonl`: append-only, one JSON object per line,
    the state read off the last event."""

    def __init__(self, directory: Path | str, *,
                 clock: Callable[[], str] = utc_now) -> None:
        self.dir = rollover_dir(directory)
        self.path = self.dir / "rollover.log.jsonl"
        self.clock = clock

    def events(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue                  # a torn tail: durable_append terminates it
            if isinstance(row, dict) and "event" in row:
                out.append(row)
        return out

    def last(self) -> dict[str, Any] | None:
        events = self.events()
        return events[-1] if events else None

    def state(self) -> State:
        last = self.last()
        return _STATE_AFTER[last["event"] if last else None]

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append `event`, or raise `RolloverError` when it cannot follow the
        last one. Read and append are one step under a lock of their own."""
        if event not in _STATE_AFTER:
            raise RolloverError(f"unknown rollover event {event!r}")
        self.dir.mkdir(parents=True, exist_ok=True)
        # The flock target is the directory itself: a lock needs no file of its own.
        fd = os.open(str(self.dir), os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            last = self.last()
            last_event = last["event"] if last else None
            if event not in _NEXT[last_event]:
                raise RolloverError(
                    f"{event!r} cannot follow {last_event or 'the start'!r} "
                    f"(state {self.state().value}); allowed: "
                    f"{sorted(_NEXT[last_event])}")
            cycle = (last or {}).get("capsule_id")
            if (event not in _START and event != "abort" and last_event in _CYCLE_EVENTS
                    and fields.get("capsule_id") != cycle):
                raise RolloverError(f"{event!r} belongs to capsule "
                                    f"{fields.get('capsule_id')!r}, the cycle in flight "
                                    f"is {cycle!r}")
            row = {"at": self.clock(), "event": event,
                   "state": _STATE_AFTER[event].value, **fields}
            durable_append(self.path, json.dumps(row, sort_keys=True))
            return row
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


_CYCLE_EVENTS = frozenset({"draining", "frozen", "launched", "ack", "handoff", "complete"})


def expire_stale(log: RolloverLog, now: str) -> dict[str, Any] | None:
    """End, on record, a cycle whose own deadline passed -- its predecessor
    never came back. Every waiting state carries one, DRAINING included."""
    last = log.last()
    if last is None:
        return None
    if last["event"] in ("draining", "frozen", "launched", "ack", "handoff"):
        expires = last.get("expires_at")
        if isinstance(expires, str) and expires <= now:
            return log.record("abort", capsule_id=last.get("capsule_id"),
                              reason="expired", expired_at=expires)
    return None


# --------------------------------------------------------------------------- #
# the pointer prompt
# --------------------------------------------------------------------------- #

POINTER_MAX_CHARS = 2500

#: The one piece of the pointer's command that a shell expands: the process the
#: lease is bound to. `$CLAUDE_PID` is the Claude Code process, which Claude
#: Code exports to its Bash tool; `$PPID` is the same process when it is not
#: set (the Bash tool's shell is Claude's child, in the foreground and in the
#: background). A constant of the product, never an input, so it is appended
#: after the quoted values and not itself quoted.
SESSION_PID_EXPR = '"${CLAUDE_PID:-$PPID}"'


def product_invocation(*, python: str | None = None,
                       package_root: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """The words that start THIS product from any shell, with no `hoh` on PATH:
    `env PYTHONPATH=<import root> <this interpreter> -m hoh.cli`.

    Derived from the running process, so the successor runs the same
    interpreter and the same code as the predecessor. `env` carries the
    assignment because a quoted `PYTHONPATH=...` word is not an assignment to
    a shell. Refuses (RolloverError) rather than print a command that cannot
    run: no interpreter, an import root that does not hold `hoh/cli.py`, or a
    path with a character that is not printable (a newline would split the
    pointer's one command line in two).

    `-P` (Python 3.11, the product's floor) keeps the working directory off
    `sys.path`: `-m` would otherwise put it ahead of `PYTHONPATH`, and a
    `hoh/cli.py` in the successor's directory would shadow the product.
    """
    interpreter = python if python is not None else sys.executable
    if not interpreter or not os.path.isabs(interpreter):
        raise RolloverError("cannot tell which Python interpreter runs this product "
                            f"({interpreter!r}); the successor could not be pointed at it")
    root = Path(package_root if package_root is not None
                else Path(__file__).resolve().parent.parent)
    if not (root / "hoh" / "cli.py").is_file():
        raise RolloverError(f"{root} is not an import root of this product (no hoh/cli.py "
                            "in it); the successor could not be pointed at it")
    if not (str(root).isprintable() and interpreter.isprintable()):
        raise RolloverError("the interpreter or the import root has a character that is "
                            "not printable; the pointer's command could not be one line")
    return ("env", f"PYTHONPATH={root}", interpreter, "-P", "-m", "hoh.cli")


def pointer_prompt(*, root: str, project_id: str, capsule_file: str,
                   capsule_digest: str, session: str, token_out: str,
                   invocation: Sequence[str] = ("hoh",)) -> str:
    """What the successor is told: a pointer and nothing else. It carries no
    history -- not a decision, not a note -- only where the capsule is and the
    one command to run. ASCII, under 2,500 characters, every value of the
    command shell-quoted. `invocation` is how the command starts the product
    (`product_invocation()` in a real rotation; the bare `hoh` only where it is
    known to be on PATH). The command ends with the lease bound to the
    successor's own conversation process (`SESSION_PID_EXPR`)."""
    argv = [*invocation, "--root", root, "project", "rollover-accept", project_id,
            "--capsule", capsule_file, "--digest", capsule_digest,
            "--session", session, "--token-out", token_out]
    command = " ".join(shlex.quote(a) for a in argv) + f" --watch-pid {SESSION_PID_EXPR}"
    text = (
        f"You are the successor orchestrator session {shlex.quote(session)} for project "
        f"{shlex.quote(project_id)}.\n"
        "This message is a pointer, not a briefing, and you carry no history. "
        "Do no programme work yet.\n"
        "\n"
        "1. Run this command now, in the background, and leave it running:\n"
        f"  {command}\n"
        "2. Act as the master only after it prints \"took over\". Until then do "
        "nothing.\n"
        "3. If it prints \"refused\" or exits, do nothing and tell the operator.\n"
        "\n"
        f"Capsule: {capsule_file}\n"
        f"Capsule digest: {capsule_digest}\n"
        f"Session id: {session}\n")
    if not text.isascii():
        raise RolloverError("the pointer prompt must be ASCII; a value in it is not")
    if len(text) >= POINTER_MAX_CHARS:
        raise RolloverError(f"the pointer prompt is {len(text)} characters, the limit "
                            f"is {POINTER_MAX_CHARS}")
    return text


# --------------------------------------------------------------------------- #
# the launcher seam
# --------------------------------------------------------------------------- #


class LauncherError(RuntimeError):
    """The successor could not be started."""


@dataclass(frozen=True)
class LaunchHandle:
    agent: str
    pane_id: str
    tab_id: str | None


class SuccessorLauncher(Protocol):
    """How a successor session is started. The tests use a fake; the real one
    is `HerdrLauncher`."""

    def start(self, *, session_id: str, model: str, cwd: Path, prompt: str,
              label: str) -> Any: ...

    def stop(self, handle: Any) -> None: ...


class HerdrLauncher:
    """Starts the successor in a tab of its own and sends it the pointer.

    Mirrors -- and does not import -- the dispatcher's Herdr calls. Its tab is
    in no dispatcher's `own_tabs` and its panes carry no role mark, so neither
    the dispatcher's `close_own` nor the role-pane lifecycle touches the
    successor; `stop` closes only the tab this launcher opened.
    """

    def __init__(self, *, run: Callable[..., Any] | None = None,
                 workspace: str | None = None, agent_kind: str = "claude",
                 timeout_s: float = 300.0, settle_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep,
                 agent_args: Sequence[str] = (), prompt_delay_s: float = 0.0) -> None:
        if not (math.isfinite(prompt_delay_s) and prompt_delay_s >= 0):
            raise ValueError(f"prompt_delay_s must be finite and not negative, "
                             f"got {prompt_delay_s}")
        self._run = run or subprocess.run
        self.workspace = workspace if workspace is not None \
            else os.environ.get("HERDR_WORKSPACE_ID")
        self.agent_kind = agent_kind
        self.timeout_s = timeout_s
        self.settle_s = settle_s
        self._sleep = sleep
        #: Arguments for the successor's own `claude`, after `--model` (e.g.
        #: `--permission-mode auto`): without them it starts in the default
        #: permission mode and waits at its first approval, before it can
        #: acknowledge.
        self.agent_args = tuple(agent_args)
        #: Seconds to idle between `agent start` and the pointer. 0 sends it
        #: at once. Claude Code 2.1.292 left a first prompt typed but unsent; a
        #: stall that survives any delay still fails closed (no ack, NO_ACK).
        self.prompt_delay_s = float(prompt_delay_s)

    def _cli(self, argv: list[str], purpose: str) -> dict[str, Any]:
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=self.timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LauncherError(f"{purpose} failed: {exc}") from exc
        if proc.returncode != 0:
            raise LauncherError(f"{purpose} failed: {(proc.stderr or '').strip()[:300]}")
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError as exc:
            raise LauncherError(f"{purpose} returned no JSON: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _close(self, tab_id: str | None) -> None:
        if not tab_id:
            return
        try:
            self._cli(["herdr", "tab", "close", tab_id], "close the successor's tab")
        except LauncherError:
            pass                           # best effort: never mask the real failure

    def start(self, *, session_id: str, model: str, cwd: Path, prompt: str,
              label: str) -> LaunchHandle:
        tab_args = ["herdr", "tab", "create", "--cwd", str(cwd), "--label", label,
                    "--no-focus"]
        if self.workspace:
            tab_args += ["--workspace", self.workspace]
        data = self._cli(tab_args, "create the successor's tab")
        result = data.get("result", {}) if isinstance(data.get("result"), dict) else {}
        pane = (result.get("root_pane") or {}).get("pane_id")
        tab = (result.get("tab") or {}).get("tab_id")
        tab = str(tab) if tab else None
        try:
            if not pane:
                raise LauncherError(f"Herdr returned no pane for the tab: {data}")
            pane = str(pane)
            self._sleep(self.settle_s)     # the new tab's shell needs a moment
            # Random part first: Herdr cuts agent names at about 24 characters.
            name = f"{secrets.token_hex(3)}-{session_id}"[:24]
            started = None
            for attempt in range(5):
                try:
                    started = self._cli(
                        ["herdr", "agent", "start", name, "--kind", self.agent_kind,
                         "--pane", pane, "--timeout", "240000", "--", "--model", model,
                         *self.agent_args],
                        f"start the successor agent {name}")
                    break
                except LauncherError as exc:
                    if "not an available shell" not in str(exc) and "busy" not in str(exc):
                        raise
                    self._sleep(1.0 + attempt * 0.5)
            if started is None:
                raise LauncherError(f"the successor agent {name} could not be started")
            if self.prompt_delay_s > 0:
                self._sleep(self.prompt_delay_s)
            self._cli(["herdr", "agent", "prompt", name, prompt],
                      f"send the pointer to {name}")
        except BaseException:
            self._close(tab)               # only the tab this launcher opened
            raise
        return LaunchHandle(agent=name, pane_id=pane, tab_id=tab)

    def stop(self, handle: LaunchHandle) -> None:
        self._close(getattr(handle, "tab_id", None))


# --------------------------------------------------------------------------- #
# the token
# --------------------------------------------------------------------------- #


def _preserve_existing(path: Path) -> None:
    """A token file that already exists is copied to a versioned name before it
    is overwritten: nothing is replaced unseen."""
    if not path.is_file():
        return
    stamp = utc_now().replace("-", "").replace(":", "")
    n = 1 + sum(1 for _ in path.parent.glob(f"{path.name}.v*"))
    while True:
        target = path.with_name(f"{path.name}.v{n}.{stamp}")
        try:
            durable_create(target, path.read_bytes(), mode=0o600)
            return
        except FileExistsError:
            n += 1


def hand_token_over(authority: MasterAuthority, token_out: str | None) -> None:
    """Write the master's token, owner-only, to `token_out`.

    If that fails *after the lease was taken*, the lease is given back and the
    caller is refused: a master nobody can act through is a lease that reads
    LOST and a drive that believes it holds a program it does not.
    """
    if not token_out:
        return
    try:
        out = Path(token_out).expanduser()
        _preserve_existing(out)
        # Whole or not at all, owner-only (a half-written token is no token).
        durable_write(out, authority.token or "", mode=0o600)
    except OSError as exc:
        try:
            authority.release(reason=f"--token-out failed: {exc}")
        except Exception:                                   # noqa: BLE001 - best effort
            pass
        raise TokenOutError(f"cannot write the token to {token_out}: {exc}") from exc


# --------------------------------------------------------------------------- #
# holding a lease
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldResult:
    reason: str          # "fenced" | "watched session ended" | "elapsed"
    renewals: int
    released: bool


def hold_lease(authority: MasterAuthority, *, interval: float | None = None,
               watch_pid: int | None = None, hold_for: float | None = None,
               sleep: Callable[[float], None] = time.sleep,
               monotonic: Callable[[], float] = time.monotonic,
               alive: Callable[[int], bool] = pid_alive) -> HoldResult:
    """Renew the lease until it is lost, or the session is gone, or time is up.

    A lease is judged by its holder's pid; a conversation is not a long-lived
    process, so the session that holds a program needs a process that stays.
    Ends when fenced (a handoff, or another master: `StaleMaster`), or when
    `watch_pid` -- the session's own process -- no longer exists (the lease is
    then given back at once), or after `hold_for` seconds (also given back).
    """
    period = interval if interval is not None else max(1.0, authority.ttl / 3)
    started = monotonic()
    renewals = 0

    def give_back(why: str) -> HoldResult:
        try:
            authority.release(reason=why)
        except StaleMaster:
            return HoldResult("fenced", renewals, False)
        return HoldResult(why, renewals, True)

    while True:
        if watch_pid is not None and not alive(watch_pid):
            return give_back("watched session ended")
        if hold_for is not None and monotonic() - started >= hold_for:
            return give_back("elapsed")
        try:
            authority.renew()
            renewals += 1
        except StaleMaster:
            return HoldResult("fenced", renewals, False)
        except Exception:                                   # noqa: BLE001 - retried
            pass            # a transient fault (a full disk, a lock hiccup): next period
        sleep(period)


# --------------------------------------------------------------------------- #
# the successor
# --------------------------------------------------------------------------- #


@dataclass
class TookOver:
    epoch: int
    session: str
    receipt_file: Path
    authority: MasterAuthority = field(repr=False)


class Successor:
    """The fresh session's side of the protocol.

    `authority` is the handle of the *successor's own* session; it is used to
    read (verify) and, only at the end, to acquire. Nothing authoritative is
    written before that `acquire`.
    """

    def __init__(self, authority: MasterAuthority, capsule_file: Path | str, *,
                 observer: Observer, pointer_digest: str | None = None,
                 project_generation: int | Callable[[], int | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 1.0,
                 wait_s: float = 900.0) -> None:
        self.authority = authority
        self.session = authority.session_id
        self.directory = authority.dir
        self.capsule_file = Path(capsule_file)
        self.observer = observer
        self.pointer_digest = pointer_digest
        self._project_generation = project_generation
        self.sleep = sleep
        self.poll_s = poll_s
        self.wait_s = wait_s
        self._cap = None

    @property
    def cap(self):
        if self._cap is None:
            try:
                self._cap = load_capsule(self.capsule_file)
            except CapsuleError as exc:
                raise RolloverError(str(exc)) from exc
        return self._cap

    def _generation(self) -> int | None:
        value = self._project_generation
        return value() if callable(value) else value

    def verify(self, *, handover: bool = False) -> Verification:
        return verify_capsule(self.cap, probe=self.authority, observer=self.observer,
                              session=self.session, pointer_digest=self.pointer_digest,
                              handover=handover, project_generation=self._generation())

    def _refuse_if_aborted(self) -> None:
        marker = aborted_path(self.directory, self.cap.capsule_id)
        if marker.exists():
            try:
                why = json.loads(marker.read_text(encoding="utf-8")).get("outcome", "")
            except (OSError, ValueError):
                why = ""
            raise RolloverError("the predecessor aborted this rollover "
                                f"({why or 'no reason given'}); nothing is taken")

    def acknowledge(self) -> tuple[Verification, dict[str, Any]]:
        """Verify, and write the acknowledgement -- also when verification
        failed, so the predecessor sees the refusal at once. A session the
        capsule does not name has no standing and writes nothing."""
        cap = self.cap
        if self.session != cap.successor_session:
            raise RolloverError(f"{self.session!r} is not the successor this capsule "
                                f"names ({cap.successor_session!r}); it writes nothing")
        self._refuse_if_aborted()
        ver = self.verify()
        ack = build_ack(cap, ver, at=self.authority.clock())
        try:
            durable_create(ack_path(self.directory, cap.capsule_id),
                           json.dumps(ack, indent=2, sort_keys=True) + "\n", mode=0o600)
        except FileExistsError as exc:
            raise RolloverError("an acknowledgement of this capsule already exists: "
                                "one acknowledgement per capsule") from exc
        return ver, ack

    def poll(self) -> MasterEpoch:
        """Wait for the handover; **derive everything again**; only then take it.

        The predecessor is not made to stop working while it waits (DRAINING is
        not enforced), so the ground can move between the acknowledgement and
        the handover. Any difference -- HEAD, branch, a fact, the epoch, the
        lease, the generation, a source -- and nothing is acquired: the
        predecessor's bounded handover lapses and it takes the program back.
        """
        cap = self.cap
        deadline = plus_seconds(self.authority.clock(), self.wait_s)
        while True:
            self._refuse_if_aborted()
            lease = self.authority.read_lease()
            if lease is not None and lease.released_at \
                    and lease.successor == self.session \
                    and lease.token_sha256 == cap.predecessor_token_sha256:
                break
            if self.authority.clock() >= deadline:
                raise RolloverError("no handover reached this session in time; "
                                    "nothing is taken")
            self.sleep(self.poll_s)
        again = self.verify(handover=True)
        if not again.ok:
            raise RolloverError("the ground moved after the acknowledgement; nothing is "
                                "taken: " + "; ".join(str(d) for d in again.differences))
        # The last look, immediately before the acquire: verifying takes time, and
        # in that time the handover may have lapsed and the predecessor taken the
        # program back (it marks the cycle aborted *before* it acquires). Acquiring
        # then would take a lease that is the predecessor's again.
        self._refuse_if_aborted()
        if not self._handover_stands(self.authority.read_lease()):
            raise RolloverError("the handover no longer stands (it lapsed or was taken "
                                "back); nothing is taken")
        try:
            epoch = self.authority.acquire(
                program_generation=cap.programme_generation, reason=Reason.SUCCESSION,
                note=f"rollover {cap.capsule_id}",
                only_handover=ExpectedHandover(
                    master_epoch=cap.predecessor_epoch,
                    token_sha256=cap.predecessor_token_sha256))
        except (LeaseHeld, LeaseError, LockBusy) as exc:
            raise RolloverError(f"the lease is not this session's to take: {exc}") from exc
        if epoch.epoch != cap.successor_epoch:
            try:
                self.authority.release(reason=f"acquired epoch {epoch.epoch}, the capsule "
                                              f"intends {cap.successor_epoch}")
            except Exception:                               # noqa: BLE001 - best effort
                pass
            raise RolloverError(f"acquired epoch {epoch.epoch}, not the intended "
                                f"{cap.successor_epoch}; the lease was given back and the "
                                f"program has no master until a session runs "
                                f"`hoh project hold`")
        return epoch

    def _handover_stands(self, lease: Lease | None) -> bool:
        """Is `lease` still the predecessor's handover of this capsule to this
        session, and not yet lapsed?"""
        cap = self.cap
        return (lease is not None and bool(lease.released_at)
                and lease.successor == self.session
                and lease.token_sha256 == cap.predecessor_token_sha256
                and lease.master_epoch == cap.predecessor_epoch
                and lease.expires_at > self.authority.clock())

    def take(self, ack: dict[str, Any], *, token_out: str | None = None) -> TookOver:
        """After the acknowledgement: wait for the handover, derive again, take
        the lease, hand the token over, write the receipt. If the token or the
        receipt cannot be written the lease is given back and this refuses."""
        cap = self.cap
        epoch = self.poll()
        try:
            hand_token_over(self.authority, token_out)
            lease: Lease | None = self.authority.read_lease()
            assert lease is not None
            receipt = build_receipt(cap, ack, lease=lease, at=self.authority.clock())
            target = receipt_path(self.directory, cap.capsule_id)
            durable_create(target, json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                           mode=0o600)
        except TokenOutError:
            raise
        except Exception as exc:                            # noqa: BLE001 - give it back
            try:
                self.authority.release(reason=f"receipt failed: {exc}")
            except Exception:                               # noqa: BLE001 - best effort
                pass
            raise RolloverError(f"the receipt could not be written ({exc}); the lease "
                                f"was given back") from exc
        return TookOver(epoch=epoch.epoch, session=self.session, receipt_file=target,
                        authority=self.authority)

    def accept(self, *, token_out: str | None = None) -> TookOver:
        """The whole of the successor's side: verify, acknowledge, wait, derive
        again, acquire, hand the token over, write the receipt."""
        ver, ack = self.acknowledge()
        if not ver.ok:
            raise RolloverError("verification failed; the acknowledgement records it and "
                                "nothing is taken: "
                                + "; ".join(str(d) for d in ver.differences))
        return self.take(ack, token_out=token_out)


# --------------------------------------------------------------------------- #
# the predecessor
# --------------------------------------------------------------------------- #


class Outcome(StrEnum):
    COMPLETE = "COMPLETE"                  # the successor holds the next epoch
    RECLAIMED = "RECLAIMED"                # the handover lapsed; this session took it back
    SUPERSEDED = "SUPERSEDED"              # someone else is master; this session is not
    DEFERRED = "DEFERRED"                  # no safe point; recorded as pending
    NOT_TRIGGERED = "NOT_TRIGGERED"
    IN_FLIGHT = "IN_FLIGHT"                # another cycle is in flight
    REFUSED = "REFUSED"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    NO_ACK = "NO_ACK"
    BAD_ACK = "BAD_ACK"
    HANDOFF_REFUSED = "HANDOFF_REFUSED"


@dataclass
class RolloverResult:
    outcome: Outcome
    capsule_id: str | None = None
    detail: str = ""
    predecessor_is_master: bool = True
    successor_epoch: int | None = None
    capsule_file: Path | None = None
    hint: str = ""
    reclaimed_epoch: int | None = None
    token: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome.value, "capsule_id": self.capsule_id,
                "detail": self.detail,
                "predecessor_is_master": self.predecessor_is_master,
                "successor_epoch": self.successor_epoch,
                "capsule": str(self.capsule_file) if self.capsule_file else None,
                "reclaimed_epoch": self.reclaimed_epoch, "hint": self.hint}


def _mark_aborted(directory: Path, capsule_id: str, outcome: Outcome | str, detail: str,
                  at: str) -> None:
    """The abort marker, for the paths that end a cycle after a handover. A
    successor that comes late finds it and takes nothing. It is written once and
    never replaced, so what it says must stay true whatever happens next."""
    payload = json.dumps({"outcome": getattr(outcome, "value", outcome), "detail": detail,
                          "at": at}, sort_keys=True) + "\n"
    try:
        durable_create(aborted_path(directory, capsule_id), payload, mode=0o600)
    except OSError:
        pass            # best effort: the lease, not this marker, is what protects


def _read_json(path: Path) -> Any:
    """The parsed file, or None when it does not exist; a file that exists and
    cannot be read is returned as a string explaining why (never as a pass)."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable: {exc}"


def _file_problems(frozen: FrozenCapsule) -> list[str]:
    """The capsule file on disk must still be the frozen one, byte for byte."""
    import hashlib

    try:
        on_disk = hashlib.sha256(frozen.path.read_bytes()).hexdigest()
    except OSError as exc:
        return [f"the capsule file cannot be read any more: {exc}"]
    if on_disk != frozen.file_sha256:
        return ["the capsule file on disk is not the one that was frozen"]
    return []


def _lease_or_none(authority: MasterAuthority) -> Lease | None:
    try:
        return authority.read_lease()
    except Exception:                                       # noqa: BLE001 - reported as None
        return None


def run_rollover(authority: MasterAuthority, directory: Path | str, *,
                 inp: CapsuleInput, observer: Observer, launcher: SuccessorLauncher,
                 root: Path | str, repo: str, project_id: str, successor_session: str,
                 trigger: Callable[[], TriggerResult] | None = None,
                 safe: Callable[[], bool] = lambda: True,
                 generation: int | None = None, ack_timeout_s: int = 600,
                 takeover_s: int = 300, drain_s: int = 300, poll_s: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep,
                 capsule_id: str | None = None,
                 reclaim_linger_s: float = 5.0,
                 invocation: Sequence[str] | None = None) -> RolloverResult:
    """The predecessor's whole rotation. See the module docstring; the steps:

    1. check; the trigger (optional) must fire; a cycle in flight refuses; no
       safe point -> pending / DEFERRED, nothing frozen, nothing launched.
    2. draining; freeze (any failure ends the cycle at once).
    3. launch the successor with the pointer prompt, in a working directory of
       its own under `rollover/<id>.cwd`.
    4. wait for the acknowledgement, renewing the lease on every poll; check it
       against what was frozen.
    5. hand over, bounded by `takeover_s`.
    6. see the successor hold exactly the intended epoch -> COMPLETE; else the
       handover lapses and this session acquires again -> RECLAIMED.

    `invocation` is how the pointer's command starts the product; None (the
    default) is `product_invocation()`, resolved first, so a product that cannot
    name itself refuses (RolloverError) before anything is frozen or launched.
    """
    invocation = tuple(invocation) if invocation is not None else product_invocation()
    directory = Path(directory)
    log = RolloverLog(directory, clock=authority.clock)

    try:
        authority.check()
    except StaleMaster as exc:
        return RolloverResult(Outcome.REFUSED, detail=str(exc), predecessor_is_master=False)
    lease = _lease_or_none(authority)
    if lease is not None:
        # A lease is judged by its holder's process. One nobody keeps alive reads
        # LOST, and the successor refuses to verify a capsule against it -- so the
        # rotation could only end in NO_ACK after the whole window. Say so now.
        holder, why = authority.holder_state(lease, authority.clock())
        if holder != "LIVE":
            return RolloverResult(
                Outcome.REFUSED,
                detail=f"this session's lease is not held by a living process "
                       f"({holder}: {why}); nothing is frozen",
                hint="Run `hoh project hold` in the background first: a conversation "
                     "is not a long-lived process, and its lease needs one.")
    if generation is not None and (lease is None
                                   or generation != lease.program_generation):
        return RolloverResult(
            Outcome.REFUSED,
            detail=f"the project state is at programme generation {generation} but the "
                   f"lease is at {lease.program_generation if lease else None}: "
                   f"nothing is frozen")

    if trigger is not None:
        fired = trigger()
        if not fired.fire:
            return RolloverResult(Outcome.NOT_TRIGGERED,
                                  detail=f"{fired.reason}: context "
                                         f"{fired.context_tokens} < {fired.threshold}")

    expire_stale(log, authority.clock())
    if log.state() not in _IDLE:
        last = log.last() or {}
        return RolloverResult(Outcome.IN_FLIGHT, capsule_id=last.get("capsule_id"),
                              detail=f"a rotation is in flight ({log.state().value}) "
                                     f"until {last.get('expires_at')}")
    if not safe():
        if log.state() is not State.ROTATION_PENDING:
            log.record("pending", reason="the program's write lock is held")
        return RolloverResult(Outcome.DEFERRED,
                              detail="no safe point: the program's write lock is held; "
                                     "recorded as pending, the next start continues")

    capsule_id = capsule_id or new_capsule_id(authority.clock())
    try:
        check_capsule_id(capsule_id)          # a name, before any path is built from it
    except CapsuleError as exc:
        return RolloverResult(Outcome.REFUSED, detail=str(exc))
    successor = str(successor_session).strip()
    handle: Any = None

    def abort(outcome: Outcome, detail: str, *, stop: bool = True,
              master: bool = True) -> RolloverResult:
        try:
            durable_create(aborted_path(directory, capsule_id),
                           json.dumps({"outcome": outcome.value,
                                       "detail": detail, "at": authority.clock()},
                                      sort_keys=True) + "\n", mode=0o600)
        except OSError:
            pass
        try:
            log.record("abort", capsule_id=capsule_id, outcome=outcome.value, detail=detail[:300])
        except RolloverError:
            pass
        if stop and handle is not None:
            try:
                launcher.stop(handle)
            except Exception:                               # noqa: BLE001 - best effort
                pass
        return RolloverResult(outcome, capsule_id=capsule_id, detail=detail,
                              predecessor_is_master=master)

    try:
        log.record("draining", capsule_id=capsule_id,
                   expires_at=plus_seconds(authority.clock(), drain_s))
    except RolloverError as exc:
        return RolloverResult(Outcome.IN_FLIGHT, detail=str(exc))

    try:
        frozen = freeze_capsule(
            authority, inp, directory=directory, observer=observer, repo=repo,
            project_id=project_id, successor_session=successor, generation=generation,
            ack_window_s=ack_timeout_s, capsule_id=capsule_id)
    except Exception as exc:                                # noqa: BLE001 - ends the cycle
        return abort(Outcome.REFUSED, f"the capsule was not frozen: {exc}",
                     master=not isinstance(exc, StaleMaster))
    cap = frozen.capsule
    log.record("frozen", capsule_id=capsule_id, expires_at=cap.ack_deadline,
               capsule_digest=cap.capsule_digest)

    cwd = rollover_dir(directory) / f"{capsule_id}.cwd"
    try:
        cwd.mkdir(parents=True, exist_ok=False)
        prompt = pointer_prompt(root=str(root), project_id=project_id,
                                capsule_file=str(frozen.path),
                                capsule_digest=cap.capsule_digest, session=successor,
                                token_out=str(cwd / "master.token"),
                                invocation=invocation)
        handle = launcher.start(session_id=successor, model=cap.successor_model,
                                cwd=cwd, prompt=prompt, label=f"successor-{capsule_id}")
    except Exception as exc:                                # noqa: BLE001 - fail closed
        return abort(Outcome.LAUNCH_FAILED, f"the successor did not start: {exc}",
                     stop=False)
    log.record("launched", capsule_id=capsule_id, expires_at=cap.ack_deadline)

    # --- wait for the acknowledgement, keeping the lease alive ---------------
    ack: Any = None
    while True:
        try:
            authority.renew()
        except StaleMaster as exc:
            return abort(Outcome.SUPERSEDED, f"this session lost the lease while "
                                             f"waiting: {exc}", master=False)
        ack = _read_json(ack_path(directory, capsule_id))
        if ack is not None:
            break
        if authority.clock() >= cap.ack_deadline:
            return abort(Outcome.NO_ACK, f"no acknowledgement by {cap.ack_deadline}")
        sleep(poll_s)

    problems = ack_problems(cap, ack) + _file_problems(frozen)
    if problems:
        return abort(Outcome.BAD_ACK, "; ".join(problems))
    log.record("ack", capsule_id=capsule_id,
               expires_at=plus_seconds(authority.clock(), takeover_s + _GRACE_S))

    # --- the bounded handover ---------------------------------------------------
    def hand_over() -> RolloverResult | None:
        successor = cap.successor_session
        try:
            authority.handoff(successor, expires_in=int(takeover_s),
                              reason=f"verified rollover {capsule_id}")
            return None
        except Exception as exc:                            # noqa: BLE001 - classified
            written = _lease_or_none(authority)
            if (written is not None and written.released_at
                    and written.successor == successor
                    and written.master_epoch == cap.predecessor_epoch):
                return None        # it raised after the lease was written: it stands
            if isinstance(exc, StaleMaster):
                return abort(Outcome.SUPERSEDED, f"this session is no longer the master: "
                                                 f"{exc}", master=False)
            return abort(Outcome.HANDOFF_REFUSED, f"the handoff was refused: {exc}")

    refused = hand_over()
    if refused is not None:
        return refused
    handed_at = authority.clock()
    deadline = plus_seconds(handed_at, takeover_s)
    log.record("handoff", capsule_id=capsule_id, expires_at=plus_seconds(deadline, _GRACE_S))

    def successor_holds() -> str:
        """Where the handover stands, from the epoch chain and the lease:
        "holds" -- the successor holds exactly the intended epoch; "waiting" --
        nobody has taken it yet (an epoch begins *before* its lease is written,
        so the intended epoch without its lease is still waiting); "gave_back"
        -- the successor took the intended epoch and released it again;
        "other" -- some other session moved the epoch."""
        try:
            current = authority.epochs.current()
            held = authority.read_lease()
        except Exception:                                   # noqa: BLE001 - not yet
            return "waiting"
        if current is None or current.epoch <= cap.predecessor_epoch:
            return "waiting"
        if current.epoch != cap.successor_epoch or current.master_id != successor:
            return "other"
        if held is None or held.leader_session_id != successor \
                or held.master_epoch != cap.successor_epoch:
            return "waiting"
        return "gave_back" if held.released_at else "holds"

    while True:
        seen = successor_holds()
        if seen != "waiting" or authority.clock() >= deadline:
            break
        sleep(poll_s)
    if seen == "other":
        return abort(Outcome.SUPERSEDED, "another session took the program while the "
                                         "handover stood", master=False, stop=False)

    if seen == "holds":
        log.record("complete", capsule_id=capsule_id)
        log.record("retired", capsule_id=capsule_id)
        fenced = bool(authority.problems())
        return RolloverResult(
            Outcome.COMPLETE, capsule_id=capsule_id, capsule_file=frozen.path,
            successor_epoch=cap.successor_epoch, predecessor_is_master=not fenced,
            detail=f"{successor!r} holds epoch {cap.successor_epoch}; this session is "
                   f"fenced and must go idle")

    # The handover lapsed: take the program back (a fresh epoch, RECOVERY). The
    # cycle is marked aborted *first*: a successor that is about to acquire looks
    # at the marker last of all, and one that looks later still finds this
    # session's live lease and is refused (`LeaseHeld`). The marker is written once,
    # before the outcome is known, so it says only that a reclaim was attempted; what
    # came of it is in the log (`reclaimed`, or `abort` with the outcome).
    _mark_aborted(directory, capsule_id, "RECLAIM_ATTEMPTED",
                  "the handover window passed and this session is taking the program back",
                  authority.clock())
    try:
        epoch = authority.acquire(program_generation=cap.programme_generation,
                                  reason=Reason.RECOVERY,
                                  note=f"rollover {capsule_id}: the handover lapsed")
    except (LeaseHeld, LeaseError, LockBusy, StaleMaster) as exc:
        return abort(Outcome.SUPERSEDED, f"the handover lapsed and the lease was not "
                                         f"this session's to take back: {exc}",
                     master=False)
    log.record("reclaimed", capsule_id=capsule_id, epoch=epoch.epoch)
    if handle is not None:
        try:
            launcher.stop(handle)
        except Exception:                                   # noqa: BLE001 - best effort
            pass
    # Linger, renewing: this command is about to exit and a lease is judged by its
    # holder's process, so for a moment longer the reclaimed lease is held by a
    # living one. Then say what is true now, not what was true at the acquire.
    until = plus_seconds(authority.clock(), reclaim_linger_s)
    while authority.clock() < until:
        try:
            authority.renew()
        except StaleMaster as exc:
            return RolloverResult(
                Outcome.SUPERSEDED, capsule_id=capsule_id, capsule_file=frozen.path,
                predecessor_is_master=False,
                detail=f"this session took the program back and then lost it again: {exc}")
        except (LockBusy, LeaseError, OSError):
            pass            # transient, as in `hold_lease`: the result line is still printed
        sleep(poll_s)
    return RolloverResult(
        Outcome.RECLAIMED, capsule_id=capsule_id, capsule_file=frozen.path,
        reclaimed_epoch=epoch.epoch, predecessor_is_master=not authority.problems(),
        token=authority.token,
        detail=("the successor took the program and gave it back" if seen == "gave_back"
                else "the successor did not take over within the handover window")
               + "; this session took the program back at a fresh epoch",
        hint="This command is about to exit, and a lease is judged by its holder's "
             "process: nothing holds the reclaimed lease. Run `hoh project hold` in "
             "the background now (it begins a fresh epoch and keeps it).")
