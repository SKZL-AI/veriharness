"""Adapter onto the Herdr CLI.

Herdr is mandatory for this assignment: the backend has to be explicit and the
actual endpoint has to be demonstrable. A silent fallback to tmux does not
satisfy A01 -- which is why `require_herdr()` checks hard and reports, instead
of improvising.

Every call here is read-only or confined to HoH's own panes. Other people's
panes are never closed (a Herdr rule, and handoff §7).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


class HerdrError(RuntimeError):
    pass


class HerdrUnavailable(HerdrError):
    """No Herdr -- then HoH must not pretend that it is running through Herdr."""


@dataclass(frozen=True)
class Endpoint:
    """An agent's demonstrable Herdr identity (A01)."""

    pane_id: str
    workspace_id: str
    tab_id: str
    agent: str | None
    status: str | None
    cwd: str | None

    def evidence(self) -> str:
        return f"herdr:{self.workspace_id}/{self.tab_id}/{self.pane_id}"


def available() -> bool:
    return shutil.which("herdr") is not None and os.environ.get("HERDR_ENV") == "1"


def require_herdr() -> None:
    if shutil.which("herdr") is None:
        raise HerdrUnavailable("herdr CLI not on the PATH")
    if os.environ.get("HERDR_ENV") != "1":
        raise HerdrUnavailable(
            "HERDR_ENV != 1 -- this session is not running inside Herdr. "
            "A01 demands the real Herdr endpoint, not a tmux fallback."
        )


def _cli(*args: str, timeout: int = 30) -> dict[str, Any]:
    proc = subprocess.run(
        ["herdr", *args], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args)} exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HerdrError(f"herdr {' '.join(args)} returned no JSON: {exc}") from exc


def self_endpoint() -> Endpoint | None:
    """The identity of the running session, from the injected variables."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return None
    return Endpoint(
        pane_id=pane,
        workspace_id=os.environ.get("HERDR_WORKSPACE_ID", ""),
        tab_id=os.environ.get("HERDR_TAB_ID", ""),
        agent=None,
        status=None,
        cwd=os.getcwd(),
    )


def agents() -> list[Endpoint]:
    """Every agent Herdr recognises."""
    require_herdr()
    data = _cli("agent", "list")
    out: list[Endpoint] = []
    for row in data.get("result", {}).get("agents", []) or []:
        out.append(
            Endpoint(
                pane_id=str(row.get("pane_id", "")),
                workspace_id=str(row.get("workspace_id", "")),
                tab_id=str(row.get("tab_id", "")),
                agent=row.get("agent"),
                status=row.get("agent_status"),
                cwd=row.get("foreground_cwd") or row.get("cwd"),
            )
        )
    return out


def find_agent(*, kind: str, cwd_prefix: str | None = None, idle_only: bool = False) -> Endpoint | None:
    """Looks for a matching agent that is already running. **Diagnosis only.**

    WARNING: **never use this for dispatch.** The function returns the first
    matching agent anywhere in Herdr -- including a running session of the
    captain's. An earlier version of the HerdrDispatcher used it to take over
    "an idle session of the same harness" and sent a role prompt belonging to
    someone else into a running research session.

    HoH talks exclusively to agents it started itself.
    """
    for ep in agents():
        if ep.agent != kind:
            continue
        if idle_only and ep.status != "idle":
            continue
        if cwd_prefix and not (ep.cwd or "").startswith(cwd_prefix):
            continue
        return ep
    return None


def snapshot() -> dict[str, Any]:
    """The full state as a machine contract: `herdr api snapshot`.

    Richer than `agent list`: it carries the real agent session id
    (`agent_session.value`), `state_change_seq` and `interactive_ready`. An
    earlier version parsed `agent list` itself and thereby read every status
    other than "working"/"blocked" as "finished" -- while Herdr's own
    documentation says of `unknown`, explicitly, *"does not prove
    completion"*.
    """
    require_herdr()
    return _cli("api", "snapshot").get("result", {}).get("snapshot", {})


def liveness(pane_id: str) -> tuple[str, str]:
    """(verdict, reason) for exactly one pane.

    verdict is one of {"attach", "evaluate", "block"} -- the three cases from
    handoff §7: a running worker is re-attached, a demonstrably finished one is
    evaluated, an unclear state blocks. Never restart blindly.
    """
    try:
        data = snapshot()
    except HerdrError as exc:
        return "block", f"Herdr state not readable: {exc}"

    for row in data.get("agents", []) or []:
        if str(row.get("pane_id")) != pane_id:
            continue
        status = str(row.get("agent_status", "unknown"))
        if status in ("working", "blocked"):
            return "attach", f"running ({status})"
        if status in ("idle", "done"):
            return "evaluate", f"agent is responsive ({status})"
        return "block", f"status '{status}' does not prove completion"

    panes = {str(p.get("pane_id")) for p in (data.get("panes", []) or [])}
    if pane_id in panes:
        return "block", "the pane exists, but Herdr recognises no agent in it"
    return "evaluate", "the pane no longer exists"


# --------------------------------------------------------------------------- #
# Worktrees -- Herdr manages them itself
# --------------------------------------------------------------------------- #


def worktree_create(
    *, cwd: str, branch: str, base: str | None = None, label: str | None = None
) -> dict[str, Any]:
    """Creates an isolated git worktree through Herdr.

    Handoff §3 and §5 require the developer to work in an isolated worktree
    rather than in the primary checkout. Herdr can do this itself
    (`herdr worktree create --branch --base`) -- rebuilding it by hand with
    `git worktree add` would be exactly the duplicated work this project has
    already paid dearly for twice.
    """
    require_herdr()
    args = ["worktree", "create", "--cwd", cwd, "--branch", branch, "--no-focus"]
    if base:
        args += ["--base", base]
    if label:
        args += ["--label", label]
    return _cli(*args, timeout=120).get("result", {})


def worktree_list(cwd: str) -> list[dict[str, Any]]:
    require_herdr()
    return _cli("worktree", "list", "--cwd", cwd).get("result", {}).get("worktrees", [])


def agent_status(target: str) -> str:
    require_herdr()
    data = _cli("agent", "get", target)
    return str(data.get("result", {}).get("agent", {}).get("agent_status", "unknown"))


def read_agent(target: str, *, lines: int = 120) -> str:
    """Reads the visible transcript of an agent pane.

    Diagnosis only. Terminal text is **not** dependable run state (handoff
    §3) -- the fleet snapshot is what that is for.
    """
    require_herdr()
    proc = subprocess.run(
        ["herdr", "agent", "read", target, "--source", "recent-unwrapped", "--lines", str(lines)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise HerdrError(f"agent read {target}: {proc.stderr.strip()[:200]}")
    return proc.stdout
