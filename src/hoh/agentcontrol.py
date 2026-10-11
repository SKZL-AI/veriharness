"""The agent-control seam: how the orchestrator starts, prompts, observes and
stops an agent session, whatever runtime hosts it (P2-17, `AgentControlAdapter`).

Before this module the dispatcher built Herdr command lines itself, in some
twenty places, and read Herdr's JSON in as many: `RoleDispatcher` (controller.py)
is the interface of a *role turn* -- "run this prompt, give me the text" -- and
says nothing about how an agent session is started, watched or ended. This is
that interface. It has three parts:

* `AgentControlAdapter`, the protocol: eleven verbs, typed values, no argument
  vector, no JSON.
* `HerdrAgentControl`, the implementation for Herdr. It builds the exact command
  lines `HerdrDispatcher` used to build and parses Herdr's answers into
  `AgentInfo` / `Surface`; the process execution, the error classification and
  the environment stay in the transport it is handed (`HerdrDispatcher._cli`).
* the fake lives with the tests (`tests/test_agent_control.py`).

What the seam itself does not do, on purpose: it does not decide anything. When
a pane may be closed, what a prompt may contain, whether a screen shows a free
input box -- that is the dispatcher's, above this seam. Ownership is the one
thing that is enforced *below* it, and only by the decorator `OwnedAgentControl`:
the durable `OwnedAgentRegistry` records which agents a run created or adopted,
under which `ProgramAgentRole`, and `OwnedAgentControl` refuses every acting
verb (start, prompt, press, close) on an id the registry does not hold. A bare
adapter still acts on the id it is given.

Failures are `controller.DispatchError`, the exception the dispatcher already
classifies (`transient`, `retry_after`); an adapter for another runtime raises
the same.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import herdr
from .contracts import Role
from .controller import DispatchError
from .durable_io import durable_append, read_single_link

#: The transport `HerdrAgentControl` runs a command through. It is called as
#: `transport(argv, purpose)` for a JSON answer, or `transport(argv, purpose,
#: text=True)` for the command's stdout as text, and raises `DispatchError`.
Transport = Callable[..., Any]


@dataclass(frozen=True)
class Surface:
    """Where an agent runs: a terminal pane and the tab that holds it."""

    pane_id: str
    tab_id: str | None


@dataclass(frozen=True)
class AgentInfo:
    """What the runtime says about one agent, as strings (never None)."""

    backend: str
    name: str
    status: str
    pane_id: str
    tab_id: str
    workspace_id: str
    cwd: str

    def evidence(self) -> str:
        """The provable identity of the run (A01): `herdr:<ws>/<tab>/<pane>`."""
        return f"{self.backend}:{self.workspace_id}/{self.tab_id}/{self.pane_id}"


@runtime_checkable
class AgentControlAdapter(Protocol):
    """Start, prompt, observe and stop an agent session.

    Every verb takes the id the runtime knows the thing by (an agent name, a
    pane id, a tab id) and acts on exactly that. `purpose` is the human reason
    for the call; it is carried into the error text and nowhere else.
    """

    #: The runtime's name; the first part of `AgentInfo.evidence()`.
    backend: str

    # -- start ---------------------------------------------------------------- #
    def open_surface(self, *, cwd: str, label: str,
                     env: Mapping[str, str] | None = None,
                     workspace: str | None = None,
                     purpose: str = "create tab") -> Surface:
        """A fresh surface (its own tab and pane) for one agent."""
        ...

    def start(self, name: str, *, kind: str, pane_id: str,
              args: Sequence[str] = (), timeout_ms: int = 240000,
              purpose: str = "") -> AgentInfo:
        """Start the agent `name` of harness `kind` in `pane_id`. One attempt;
        retrying a shell that is not ready yet is the caller's decision."""
        ...

    # -- prompt --------------------------------------------------------------- #
    def prompt(self, name: str, text: str, *, timeout_ms: int,
               purpose: str = "") -> AgentInfo:
        """Send `text` to `name` and wait for its turn to end. Returns the
        agent as the runtime reports it after the turn."""
        ...

    def press(self, pane_id: str, key: str, *, purpose: str = "") -> None:
        """Press one key in `pane_id` (the stalled prompt's Enter)."""
        ...

    # -- observe -------------------------------------------------------------- #
    def status(self, name: str, *, purpose: str = "") -> AgentInfo:
        """The agent's state (`working`, `blocked`, `idle`, `done`, ...)."""
        ...

    def screen(self, name: str, *, purpose: str = "") -> str:
        """What the agent's terminal shows right now."""
        ...

    def pane_screen(self, pane_id: str, *, source: str | None = "visible",
                    purpose: str = "") -> str:
        """What the pane shows (`source=None`: the runtime's default view);
        raises when the pane does not exist."""
        ...

    def transcript(self, name: str, *, lines: int = 120) -> str:
        """The recent scrollback of an agent, for diagnosis only."""
        ...

    def snapshot(self) -> dict:
        """The runtime's whole state: lists `panes`, `tabs` and `agents` of
        row dicts (`pane_id`, `tab_id`, `workspace_id`, `name`, `agent`,
        `agent_status`, `cwd`, `foreground_cwd`, `pane_count`)."""
        ...

    # -- stop ----------------------------------------------------------------- #
    def close_pane(self, pane_id: str, *, purpose: str = "") -> None:
        ...

    def close_tab(self, tab_id: str, *, purpose: str = "") -> None:
        ...


def _agent_row(data: Any) -> dict:
    result = data.get("result") if isinstance(data, dict) else None
    agent = result.get("agent") if isinstance(result, dict) else None
    return agent if isinstance(agent, dict) else {}


class HerdrAgentControl:
    """`AgentControlAdapter` over the Herdr CLI.

    `transport` runs one command and returns Herdr's JSON (or stdout with
    `text=True`); `HerdrDispatcher` hands it its own `_cli`, which owns the
    process, the environment, the deadline and the error classification.
    """

    backend = "herdr"

    def __init__(self, transport: Transport) -> None:
        self._run = transport

    @staticmethod
    def info(row: dict, name: str = "") -> AgentInfo:
        return AgentInfo(
            backend="herdr",
            name=str(row.get("name") or name),
            status=str(row.get("agent_status") or ""),
            pane_id=str(row.get("pane_id") or ""),
            tab_id=str(row.get("tab_id") or ""),
            workspace_id=str(row.get("workspace_id") or ""),
            cwd=str(row.get("foreground_cwd") or row.get("cwd") or ""),
        )

    def open_surface(self, *, cwd, label, env=None, workspace=None,
                     purpose="create tab") -> Surface:
        argv = ["herdr", "tab", "create", "--cwd", cwd, "--label", label, "--no-focus"]
        for key, value in (env or {}).items():
            argv += ["--env", f"{key}={value}"]
        if workspace:
            argv += ["--workspace", workspace]
        data = self._run(argv, purpose)
        result = data.get("result", {}) if isinstance(data, dict) else {}
        pane = (result.get("root_pane") or {}).get("pane_id")
        tab = (result.get("tab") or {}).get("tab_id")
        if not pane:
            raise DispatchError(f"Herdr returned no pane ID for the tab: {data}")
        return Surface(pane_id=str(pane), tab_id=str(tab) if tab else None)

    def start(self, name, *, kind, pane_id, args=(), timeout_ms=240000,
              purpose="") -> AgentInfo:
        argv = ["herdr", "agent", "start", name, "--kind", kind, "--pane", pane_id,
                "--timeout", str(timeout_ms)]
        if args:
            argv += ["--", *args]
        data = self._run(argv, purpose or f"start agent {name}")
        return self.info(_agent_row(data), name)

    def prompt(self, name, text, *, timeout_ms, purpose="") -> AgentInfo:
        data = self._run(["herdr", "agent", "prompt", name, text, "--wait",
                          "--timeout", str(timeout_ms)], purpose or f"prompt {name}")
        return self.info(_agent_row(data), name)

    def press(self, pane_id, key, *, purpose="") -> None:
        self._run(["herdr", "pane", "send-keys", pane_id, key],
                  purpose or f"press {key} in {pane_id}", text=True)

    def status(self, name, *, purpose="") -> AgentInfo:
        data = self._run(["herdr", "agent", "get", name], purpose or f"status of {name}")
        return self.info(_agent_row(data), name)

    def screen(self, name, *, purpose="") -> str:
        return self._run(["herdr", "agent", "read", name, "--source", "visible"],
                         purpose or f"read {name}", text=True)

    def pane_screen(self, pane_id, *, source="visible", purpose="") -> str:
        argv = ["herdr", "pane", "read", pane_id]
        if source:
            argv += ["--source", source]
        return self._run(argv, purpose or f"read pane {pane_id}", text=True)

    def transcript(self, name, *, lines=120) -> str:
        return herdr.read_agent(name, lines=lines)

    def snapshot(self) -> dict:
        return herdr.snapshot()

    def close_pane(self, pane_id, *, purpose="") -> None:
        self._run(["herdr", "pane", "close", pane_id], purpose or f"close pane {pane_id}")

    def close_tab(self, tab_id, *, purpose="") -> None:
        self._run(["herdr", "tab", "close", tab_id], purpose or f"close tab {tab_id}")

    def run_in_pane(self, pane_id, command, *, purpose="") -> None:
        """Type `command` and Enter into the shell of `pane_id` (`herdr pane run`). One verb
        beyond the protocol: a run host is a plain shell that runs one command, not an agent."""
        self._run(["herdr", "pane", "run", pane_id, command],
                  purpose or f"run a command in {pane_id}", text=True)


# --------------------------------------------------------------------------- #
# Ownership: which agents a run started, and in what role
# --------------------------------------------------------------------------- #

RUN_SCOPE = "run"
PROGRAM_SCOPE = "program"
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class ProgramAgentRole:
    """The role an owned agent plays in a program, one level above `Role`.

    `Role` names the three roles of a run's pipeline. A `ProgramAgentRole`
    refines it by a scope: `run` is a pipeline role of one run (`planner`,
    `developer`, `qa`); `program` is a specialist the program itself starts
    beside the pipeline (an orchestrator, a reviewer) and whose name may be any
    slug that is not a pipeline role. Nothing else is a role: an agent without
    one is never registered.
    """

    scope: str
    name: str

    def __post_init__(self) -> None:
        pipeline = {r.value for r in Role}
        if self.scope == RUN_SCOPE:
            if self.name not in pipeline:
                raise ValueError(f"{self.name!r} is not a pipeline role "
                                 f"({', '.join(sorted(pipeline))})")
        elif self.scope == PROGRAM_SCOPE:
            if not _NAME.match(self.name) or self.name in pipeline:
                raise ValueError(f"{self.name!r} is not a program role name: a "
                                 "lowercase slug that is not a pipeline role")
        else:
            raise ValueError(f"unknown role scope {self.scope!r}")

    @classmethod
    def pipeline(cls, role: Role | str) -> "ProgramAgentRole":
        return cls(RUN_SCOPE, str(getattr(role, "value", role)))

    @classmethod
    def program(cls, name: str) -> "ProgramAgentRole":
        return cls(PROGRAM_SCOPE, name)

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.name}"

    @classmethod
    def parse(cls, key: str) -> "ProgramAgentRole":
        scope, _, name = str(key).partition(":")
        return cls(scope, name)


class NotOwned(DispatchError):
    """An acting verb was asked for an id the registry does not hold as owned.
    Never transient: asking again does not make a foreign agent ours."""

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)


OWNED_FILE = "owned-agents.jsonl"
_EVENTS = frozenset({"surface", "agent", "adopt", "release"})


@dataclass(frozen=True)
class OwnedAgent:
    """One pane HoH owns (or owned): who owns it, in what role, what runs in it."""

    program: str
    run_id: str
    role: ProgramAgentRole
    backend: str
    pane_id: str
    tab_id: str
    name: str
    cwd: str
    origin: str          # "created" (open_surface) or "adopted" (a restored agent)
    state: str           # "owned" or "released"


class OwnedAgentRegistry:
    """The durable record of the agents one run owns: `<run dir>/owned-agents.jsonl`.

    One JSON line per event (`surface`, `agent`, `adopt`, `release`), appended
    through `durable_io.durable_append(single_link=True)` before the verb that
    depends on it is sent. The state is the fold of the lines of **this run**
    (`run_id`): a line of another run, a line that does not parse and a line of
    an unknown event are skipped and counted, never trusted. A file that cannot
    be read (a symbolic link, a second name, an I/O error) owns **nothing**:
    every query is False, `problem` says why. A record is a claim by HoH's own
    process, not a proof against a process of the same user that can write the
    run directory.
    """

    def __init__(self, path: Path | str, *, run_id: str) -> None:
        if not run_id:
            raise ValueError("an owned-agent registry belongs to one run")
        self.path = Path(path)
        #: The run this registry answers for. `run_id` here is the default (the run
        #: directory's name, which is what a restarted process has before it has
        #: dispatched anything); the first `bind` replaces it with the id of the
        #: `RunState` actually being served, and from then on it cannot change.
        self.run_id = run_id
        self._bound = False
        self.problem = ""
        self.skipped = 0

    def bind(self, run_id: str) -> None:
        """Make `run_id` -- the id of the state being served -- the run this
        registry answers for. Once bound, a different id is a `ValueError`: a
        registry never changes owner in the middle of a run."""
        if not run_id:
            raise ValueError("an owned-agent registry belongs to one run")
        if self._bound and run_id != self.run_id:
            raise ValueError(f"the registry is bound to run {self.run_id!r}, "
                             f"not to {run_id!r}")
        self.run_id, self._bound = run_id, True

    # -- write ----------------------------------------------------------------- #
    def _append(self, event: str, **fields) -> None:
        record = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "v": 1, **fields, "event": event, "run_id": self.run_id}
        line = json.dumps(record, sort_keys=True) + "\n"
        durable_append(self.path, line, single_link=True)

    def record_surface(self, role: ProgramAgentRole, surface: Surface, *, program: str,
                       backend: str, cwd: str) -> None:
        self._append("surface", program=program, role=role.key, backend=backend,
                     pane_id=surface.pane_id, tab_id=surface.tab_id or "", cwd=cwd)

    def record_agent(self, pane_id: str, name: str) -> None:
        self._append("agent", pane_id=pane_id, name=name)

    def adopt(self, role: ProgramAgentRole, *, program: str, backend: str, pane_id: str,
              tab_id: str, name: str, cwd: str) -> None:
        """A pane the runtime restored, proven this run's by the dispatcher
        (name and directory); from now on it is owned like a created one."""
        self._append("adopt", program=program, role=role.key, backend=backend,
                     pane_id=pane_id, tab_id=tab_id, name=name, cwd=cwd)

    def release(self, pane_id: str, *, reason: str) -> None:
        """The pane is gone (a confirmed close, or the runtime says so): it is
        no longer closable, promptable or startable."""
        self._append("release", pane_id=pane_id, reason=reason)

    # -- read ------------------------------------------------------------------ #
    def agents(self) -> list[OwnedAgent]:
        """Every pane this run ever owned, folded (latest state), in the order
        first recorded. `[]` when the file is unreadable (see `problem`)."""
        self.problem, self.skipped = "", 0
        try:
            raw = read_single_link(self.path)
        except (OSError, UnicodeError) as exc:
            self.problem = f"{type(exc).__name__}: {exc}"
            return []
        panes: dict[str, dict] = {}
        for line in (raw or b"").decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                event, pane = rec["event"], str(rec["pane_id"])
                if (not isinstance(rec, dict) or event not in _EVENTS
                        or rec["run_id"] != self.run_id or not pane):
                    raise ValueError
                if event in ("surface", "adopt"):
                    ProgramAgentRole.parse(rec["role"])
            except (ValueError, KeyError, TypeError):
                self.skipped += 1
                continue
            if event in ("surface", "adopt"):
                panes[pane] = {
                    "program": str(rec.get("program") or ""), "role": rec["role"],
                    "backend": str(rec.get("backend") or ""), "tab_id": str(rec.get("tab_id") or ""),
                    "name": str(rec.get("name") or ""), "cwd": str(rec.get("cwd") or ""),
                    "origin": "created" if event == "surface" else "adopted", "state": "owned"}
            elif pane in panes:
                if event == "agent":
                    panes[pane]["name"] = str(rec.get("name") or "")
                else:
                    panes[pane]["state"] = "released"
            else:
                self.skipped += 1          # an event for a pane nobody registered
        return [OwnedAgent(run_id=self.run_id, role=ProgramAgentRole.parse(v.pop("role")),
                           pane_id=pane, **v) for pane, v in panes.items()]

    def owned(self) -> list[OwnedAgent]:
        return [a for a in self.agents() if a.state == "owned"]

    def owns_pane(self, pane_id: str) -> bool:
        return any(a.pane_id == str(pane_id) for a in self.owned())

    def owns_tab(self, tab_id: str) -> bool:
        return bool(tab_id) and any(a.tab_id == str(tab_id) for a in self.owned())

    def owns_agent(self, name: str) -> bool:
        return bool(name) and any(a.name == str(name) for a in self.owned())

    def for_role(self, role: ProgramAgentRole) -> list[OwnedAgent]:
        return [a for a in self.owned() if a.role == role]


class OwnedAgentControl:
    """An `AgentControlAdapter` that acts only on agents its registry owns.

    It wraps another adapter. `open_surface` registers what it created (under
    the role set by `acting_as`, and refuses without one); `start` and the
    acting verbs (`prompt`, `press`, `close_pane`, `close_tab`) are refused with
    `NotOwned` -- before the wrapped adapter is called -- for any id the
    registry does not hold as owned. `close_tab` also requires that the runtime
    lists no pane in that tab that the registry does not own. Observing
    (`status`, `screen`, `pane_screen`, `transcript`, `snapshot`) is not
    acting and passes through for any id.
    """

    def __init__(self, base: AgentControlAdapter, registry: OwnedAgentRegistry) -> None:
        self._base = base
        self.registry = registry
        self._role: ProgramAgentRole | None = None
        self._program = ""

    @property
    def backend(self) -> str:
        return self._base.backend

    @contextmanager
    def acting_as(self, role: ProgramAgentRole, *, program: str,
                  run_id: str) -> Iterator[None]:
        try:
            self.registry.bind(run_id)
        except ValueError as exc:
            raise NotOwned(str(exc)) from exc
        before = (self._role, self._program)
        self._role, self._program = role, program
        try:
            yield
        finally:
            self._role, self._program = before

    def adopt(self, *, pane_id: str, tab_id: str, name: str, cwd: str) -> None:
        """Register a restored agent the dispatcher proved this run's."""
        if self._role is None:
            raise NotOwned("an adoption needs the role it is adopted for")
        self.registry.adopt(self._role, program=self._program, backend=self.backend,
                            pane_id=pane_id, tab_id=tab_id, name=name, cwd=cwd)

    def _require(self, owned: bool, what: str, verb: str) -> None:
        if not owned:
            why = f" ({self.registry.problem})" if self.registry.problem else ""
            raise NotOwned(f"{verb}: {what} is not an agent this run owns{why}; "
                           "nothing was sent")

    def open_surface(self, *, cwd, label, env=None, workspace=None,
                     purpose="create tab") -> Surface:
        if self._role is None:
            raise NotOwned("open_surface: no role to own the new surface under; "
                           "nothing was created")
        surface = self._base.open_surface(cwd=cwd, label=label, env=env,
                                          workspace=workspace, purpose=purpose)
        try:
            self.registry.record_surface(self._role, surface, program=self._program,
                                         backend=self.backend, cwd=cwd)
        except Exception as exc:          # noqa: BLE001 - what is not recorded is not left
            try:
                if surface.tab_id:
                    self._base.close_tab(surface.tab_id, purpose="close an unregistered tab")
                else:
                    self._base.close_pane(surface.pane_id, purpose="close an unregistered pane")
            except Exception:             # noqa: BLE001 - reported below either way
                pass
            raise DispatchError(f"the new surface {surface.pane_id} could not be "
                                f"recorded as owned ({exc}); it was closed") from exc
        return surface

    def start(self, name, *, kind, pane_id, args=(), timeout_ms=240000,
              purpose="") -> AgentInfo:
        self._require(self.registry.owns_pane(pane_id), f"pane {pane_id}", "start")
        info = self._base.start(name, kind=kind, pane_id=pane_id, args=args,
                                timeout_ms=timeout_ms, purpose=purpose)
        try:
            self.registry.record_agent(pane_id, name)
        except Exception as exc:          # noqa: BLE001
            raise DispatchError(f"agent {name} was started in {pane_id} but could "
                                f"not be recorded as owned ({exc})") from exc
        return info

    def prompt(self, name, text, *, timeout_ms, purpose="") -> AgentInfo:
        self._require(self.registry.owns_agent(name), f"agent {name}", "prompt")
        return self._base.prompt(name, text, timeout_ms=timeout_ms, purpose=purpose)

    def press(self, pane_id, key, *, purpose="") -> None:
        self._require(self.registry.owns_pane(pane_id), f"pane {pane_id}", "press")
        self._base.press(pane_id, key, purpose=purpose)

    def close_pane(self, pane_id, *, purpose="") -> None:
        self._require(self.registry.owns_pane(pane_id), f"pane {pane_id}", "close_pane")
        self._base.close_pane(pane_id, purpose=purpose)

    def close_tab(self, tab_id, *, purpose="") -> None:
        self._require(self.registry.owns_tab(tab_id), f"tab {tab_id}", "close_tab")
        # The runtime must show the tab, row by row, or nothing is closed: a
        # snapshot that lists no pane (or no list) proves nobody is in the tab
        # no more than it proves the tab is gone.
        snapshot = self._base.snapshot()
        listed = snapshot.get("panes") if isinstance(snapshot, dict) else None
        rows = listed if isinstance(listed, list) else []
        self._require(bool(rows) and all(isinstance(r, dict) for r in rows)
                      and any(str(r.get("tab_id") or "") == str(tab_id) for r in rows),
                      f"tab {tab_id} (the runtime's snapshot does not show it)", "close_tab")
        strangers = [str(r.get("pane_id")) for r in rows
                     if str(r.get("tab_id") or "") == str(tab_id)
                     and not self.registry.owns_pane(str(r.get("pane_id") or ""))]
        self._require(not strangers, f"tab {tab_id} (it holds {', '.join(strangers)})",
                      "close_tab")
        self._base.close_tab(tab_id, purpose=purpose)

    # -- observing is not acting ------------------------------------------------ #
    def status(self, name, *, purpose="") -> AgentInfo:
        return self._base.status(name, purpose=purpose)

    def screen(self, name, *, purpose="") -> str:
        return self._base.screen(name, purpose=purpose)

    def pane_screen(self, pane_id, *, source="visible", purpose="") -> str:
        return self._base.pane_screen(pane_id, source=source, purpose=purpose)

    def transcript(self, name, *, lines=120) -> str:
        return self._base.transcript(name, lines=lines)

    def snapshot(self) -> dict:
        return self._base.snapshot()

    def run_in_pane(self, pane_id, command, *, purpose="") -> None:
        """`HerdrAgentControl.run_in_pane` for a pane this run owns (an acting verb, like
        `press`); a runtime without the verb is refused, nothing is sent."""
        self._require(self.registry.owns_pane(pane_id), f"pane {pane_id}", "run_in_pane")
        runner = getattr(self._base, "run_in_pane", None)
        if runner is None:
            raise DispatchError("run_in_pane: this runtime cannot run a command in a pane; "
                                "nothing was sent")
        runner(pane_id, command, purpose=purpose)
