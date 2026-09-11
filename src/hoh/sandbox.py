"""OS-level isolation for acceptance-check commands.

`runner.py` says outright what it is: the guard around check commands is a
tripwire against accidents, not a security boundary. A pattern denylist on a
string a shell reinterprets afterwards is not watertight in principle, and
this document has never claimed otherwise. What narrows the blast radius
today -- no login shell, a reduced environment, `setrlimit`, an isolated
arena, size-limited output -- is real and is not a sandbox.

This module is the sandbox, and the point of writing the contract before any
backend is that the three limits it is supposed to address are **not** equally
addressable. Claiming one mechanism closes all three would be the same kind of
overreach this project keeps having to correct in public.

Measured against `docs/LIMITATIONS.md`:

**Limit 6 -- the arena is nested inside the project's git working tree.**
Directly closable. `git rev-parse --show-toplevel`, run inside an arena, does
not fail: git climbs past the arena boundary and answers with the *ancestor*
repository's root, so `git status` and `git log` silently report on the live
working tree instead of the frozen candidate. A mount namespace that does not
carry the parent `.git` into the sandbox removes the thing git climbs to.
This one is falsifiable before and after, and `verify_limit_6` does exactly
that.

**Limit 4 -- the guard is a tripwire, not a boundary.** Mitigated, not closed.
A sandbox constrains what a command can reach; it does not make a denylist
sound. A hostile plan still gets to run *something*, and the honest claim is
that the blast radius shrinks to the sandbox, not that the guard became a
security boundary.

**Limit 5 -- `{ARENA}` substitution diverges between guard and runner.** Not
closed at all. The guard reasons about the command with the placeholder
replaced by the literal string "ARENA"; the runner substitutes the real
absolute path. That is a defect in string handling inside HoH, and no amount
of isolation makes the two strings the same. A sandbox reduces what the
divergence can cost. It does not remove it.

Fail-closed is the other half of the contract. If a caller asks for isolation
and the backend cannot provide it, the check does not quietly run unsandboxed:
`unavailable()` explains why and the caller refuses. A sandbox that silently
degrades is worse than none, because the whole reason to ask for one is the
assumption that it is there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Isolation(StrEnum):
    """What a caller is asking for. Named, so a refusal can name it back."""

    #: No isolation beyond what runner.py already does. Explicit, never a
    #: silent fallback: a caller has to say this word.
    NONE = "none"
    #: Filesystem and network isolation: the candidate read-only, a separate
    #: writable scratch area, no parent `.git`, no network.
    STRICT = "strict"


@dataclass(frozen=True)
class SandboxSpec:
    """What one sandboxed execution is allowed to see and do."""

    #: The tree under test. Mounted read-only: a check that rewrites the thing
    #: it is checking has already invalidated its own result.
    candidate: Path
    #: Writable scratch, outside the candidate. A check that needs to write
    #: writes here, where it cannot be mistaken for the candidate's content.
    scratch: Path
    isolation: Isolation = Isolation.STRICT
    network: bool = False
    #: Environment passed through. Everything else is dropped -- an inherited
    #: environment is an inherited capability.
    env_passthrough: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ")
    #: Extra read-only paths (an interpreter, a toolchain).
    ro_binds: tuple[Path, ...] = ()
    #: Wall-clock ceiling, seconds.
    timeout: int = 600
    #: Address-space ceiling, bytes. None leaves it to the backend default.
    memory_bytes: int | None = None


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    #: The isolation actually applied. Never inferred by the caller: a result
    #: that does not say how it ran is a result you cannot reason about.
    isolation: Isolation
    detail: str = ""


class SandboxUnavailable(RuntimeError):
    """The requested isolation cannot be provided. Never caught into a
    fallback by this module: the caller decides, explicitly."""


class SandboxBackend(Protocol):
    name: str

    def unavailable(self) -> str | None:
        """None when usable, otherwise a reason a human can act on."""
        ...

    def run(self, argv: list[str], spec: SandboxSpec) -> SandboxResult: ...


def _env_for(spec: SandboxSpec) -> dict[str, str]:
    env = {k: os.environ[k] for k in spec.env_passthrough if k in os.environ}
    env["HOME"] = str(spec.scratch)
    env["TMPDIR"] = str(spec.scratch)
    # Deliberately absent: anything that would let a check reach the outer
    # project. GIT_DIR and GIT_WORK_TREE in particular, which would re-open
    # exactly the boundary limit 6 is about.
    return env


@dataclass
class BubblewrapSandbox(SandboxBackend):
    """Linux namespaces via `bwrap`.

    Chosen over hand-rolled `unshare` calls because bubblewrap is a single
    reviewed program whose whole purpose is getting this right, and because a
    sandbox assembled out of shell primitives is a sandbox nobody audits.
    """

    name: str = "bubblewrap"
    bwrap: str = field(default_factory=lambda: shutil.which("bwrap") or "")

    def unavailable(self) -> str | None:
        if not self.bwrap:
            return (
                "bwrap is not installed; install bubblewrap, or ask for "
                "Isolation.NONE explicitly if you accept running unsandboxed"
            )
        p = subprocess.run(
            [self.bwrap, "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent",
             "true"],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            return (
                f"bwrap is installed but cannot create a namespace here: "
                f"{(p.stderr or p.stdout).strip()[:200]}"
            )
        return None

    def _argv(self, argv: list[str], spec: SandboxSpec) -> list[str]:
        cmd = [
            self.bwrap,
            "--die-with-parent",
            "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
            "--new-session",
            "--proc", "/proc",
            "--dev", "/dev",
            # A minimal read-only system. Not `--ro-bind / /`: that would carry
            # the whole machine in, including the ancestor checkout whose .git
            # is the thing limit 6 is about.
            "--ro-bind", "/usr", "/usr",
            "--ro-bind-try", "/bin", "/bin",
            "--ro-bind-try", "/lib", "/lib",
            "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/etc/ssl", "/etc/ssl",
            # The candidate, read-only. A check that rewrites what it checks
            # has invalidated its own result before it finished.
            "--ro-bind", str(spec.candidate), str(spec.candidate),
            "--bind", str(spec.scratch), str(spec.scratch),
            "--chdir", str(spec.candidate),
        ]
        if not spec.network:
            cmd.append("--unshare-net")
        for p in spec.ro_binds:
            cmd += ["--ro-bind-try", str(p), str(p)]
        for k, v in _env_for(spec).items():
            cmd += ["--setenv", k, v]
        return cmd + ["--"] + argv

    def run(self, argv: list[str], spec: SandboxSpec) -> SandboxResult:
        grund = self.unavailable()
        if grund:
            raise SandboxUnavailable(grund)
        p = subprocess.run(
            self._argv(argv, spec), capture_output=True, text=True,
            timeout=spec.timeout, env={},
        )
        return SandboxResult(p.returncode, p.stdout, p.stderr,
                             isolation=spec.isolation, detail=self.name)


@dataclass
class NoSandbox(SandboxBackend):
    """Runs the command as an ordinary subprocess.

    Exists so "unsandboxed" is a thing a caller names rather than a thing that
    happens when something else fails. It refuses `Isolation.STRICT` outright
    -- returning a STRICT-labelled result it did not provide is the precise
    failure this whole module is built to prevent.
    """

    name: str = "none"

    def unavailable(self) -> str | None:
        return None

    def run(self, argv: list[str], spec: SandboxSpec) -> SandboxResult:
        if spec.isolation is not Isolation.NONE:
            raise SandboxUnavailable(
                f"NoSandbox cannot provide {spec.isolation.value} isolation; "
                "asking it to is a caller error, not something to degrade into"
            )
        p = subprocess.run(
            argv, capture_output=True, text=True, cwd=str(spec.candidate),
            env=_env_for(spec), timeout=spec.timeout,
        )
        return SandboxResult(p.returncode, p.stdout, p.stderr,
                             isolation=Isolation.NONE, detail="no isolation applied")


def select(isolation: Isolation, backends: list[SandboxBackend] | None = None) -> SandboxBackend:
    """The backend for a requested isolation, or a refusal explaining why.

    Fail-closed: there is no path from STRICT to an unsandboxed run. A caller
    that wants to proceed anyway has to ask for `Isolation.NONE` by name, which
    makes the decision visible in the code that made it.
    """
    if isolation is Isolation.NONE:
        return NoSandbox()
    kandidaten = backends if backends is not None else [BubblewrapSandbox()]
    gruende = []
    for b in kandidaten:
        grund = b.unavailable()
        if grund is None:
            return b
        gruende.append(f"{b.name}: {grund}")
    raise SandboxUnavailable(
        f"no backend can provide {isolation.value} isolation. " + "; ".join(gruende)
    )


# --------------------------------------------------------------------------- #
# Falsifying the limits, one at a time
# --------------------------------------------------------------------------- #

def verify_limit_6(arena: Path, backend: SandboxBackend, spec: SandboxSpec) -> dict:
    """Does `git rev-parse --show-toplevel` still climb out of the arena?

    Returns both measurements rather than a verdict, because the claim worth
    making is comparative: the same command, the same directory, answering
    differently inside and outside the sandbox. A single "it is fixed" would
    not be checkable.

    Outside the sandbox this is expected to succeed and name the *ancestor*
    repository -- that is the limit. Inside, with no parent `.git` reachable,
    git should find no repository at all.
    """
    argv = ["git", "rev-parse", "--show-toplevel"]
    draussen = subprocess.run(argv, cwd=str(arena), capture_output=True, text=True)
    drinnen = backend.run(argv, spec)
    ausserhalb = draussen.stdout.strip()
    klettert = bool(ausserhalb) and Path(ausserhalb) != arena and draussen.returncode == 0
    return {
        "limit": 6,
        "outside_exit": draussen.returncode,
        "outside_toplevel": ausserhalb,
        "outside_climbs_out": klettert,
        "inside_exit": drinnen.exit_code,
        "inside_toplevel": drinnen.stdout.strip(),
        "inside_climbs_out": (
            drinnen.exit_code == 0
            and bool(drinnen.stdout.strip())
            and Path(drinnen.stdout.strip()) != arena
        ),
        "isolation": drinnen.isolation.value,
    }
