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
    #: Variables the caller wants inside, on top of `env_passthrough`. Passed
    #: explicitly, never inherited: an inherited environment is an inherited
    #: capability.
    extra_env: dict = field(default_factory=dict)
    #: The file descriptor, valid inside the sandbox, that the proof prologue
    #: writes to. The runner holds the other end. None means no proof is
    #: collected, and then no isolation claim can be verified.
    proof_fd: int | None = None
    #: Wall-clock ceiling, seconds.
    timeout: int = 600
    #: Address-space ceiling, bytes, applied with setrlimit before exec so the
    #: sandboxed process and everything it starts inherit it. None means no
    #: limit -- and it means that honestly: this field was declared before it
    #: was enforced, which is a protection that exists only in a docstring.
    memory_bytes: int | None = None
    #: Ceiling on open file descriptors. Same discipline: enforced, or None.
    max_open_files: int | None = None
    #: CPU-seconds ceiling and maximum file size a check may write. Both were
    #: absent here while the *unsandboxed* path applied them, so asking for
    #: stronger isolation silently removed two ceilings (O107). Carrying them
    #: keeps STRICT a superset: everything the weaker path enforces, plus a
    #: boundary.
    cpu_seconds: int | None = None
    file_size_bytes: int | None = None


@dataclass(frozen=True)
class LaunchPlan:
    """Everything needed to start a sandboxed command *without* the backend
    running it.

    The backend used to own the whole execution: it called `subprocess.run`,
    buffered the output in memory and handed back a finished result. That made
    the sandboxed path silently lose two things the unsandboxed path has --
    output streamed to a file under a size ceiling, and a supervisor that can
    kill an overrunning process group. Measured on 64 MiB of check output, the
    unsandboxed path stopped at the ceiling with `INCONCLUSIVE` while the
    sandboxed path buffered it all and returned `PASS`.

    So the backend now describes how to launch, and one supervisor runs it.
    """

    argv: list[str]
    env: dict[str, str]
    cwd: str | None
    #: True when the launched command was given a prologue that reports, over
    #: a pipe the runner holds, facts obtainable only from inside. False when
    #: the backend makes no isolation claim that needs proving.
    proves_isolation: bool = False


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

    def plan(self, argv: list[str], spec: SandboxSpec) -> LaunchPlan:
        """How to launch `argv` under this backend, without launching it."""
        ...

    def run(self, argv: list[str], spec: SandboxSpec) -> SandboxResult: ...


#: The script prologue the sandboxed command carries. It reports, from inside,
#: three things the outside cannot otherwise establish:
#:
#: * the mount and network namespace ids -- if they equal the runner's own,
#:   the command ran in the runner's namespaces and was not isolated at all;
#: * whether the candidate is `absent`, `ro` or `rw`. Three values, not two:
#:   `[ -w path ]` is false for a path that does not exist, so a bind that
#:   never materialised -- exactly the failure this proof exists to catch --
#:   read as "read-only" and produced a clean receipt. A reviewer demonstrated
#:   it with no adversarial check at all.
#:
#: **It writes to a pipe the runner holds, not to a file.** The first version
#: wrote into the scratch directory, which is bind-mounted read-write and is
#: also `$HOME` and `$TMPDIR`, and the runner read it *after* the check
#: finished -- so the untrusted command owned the proof about itself. A
#: three-line `echo` into `$HOME/.hoh-sandbox-proof` was enough to make a bare
#: unsandboxed `bash` report `honoured() == True`. A pipe is append-only and
#: the prologue writes first, so nothing the check does afterwards can alter
#: the bytes already sent. It also survives a check that tidies its own
#: `$TMPDIR`, which used to destroy the proof and turn a passing check into an
#: infrastructure refusal.
#:
#: Written *before* the real command, so a command that fails immediately
#: still leaves the proof. Nothing on the pipe means the command never
#: started, which is how a sandbox that fails during setup is told apart from
#: a check that ran and exited non-zero -- bubblewrap 0.9 exits 1 for both.
MARKER_PROLOGUE = (
    "{ readlink /proc/self/ns/mnt; readlink /proc/self/ns/net; "
    'if [ ! -e "$HOH_CANDIDATE" ]; then echo absent; '
    'elif [ -w "$HOH_CANDIDATE" ]; then echo rw; '
    "else echo ro; fi; } >&$HOH_PROOF_FD 2>/dev/null || true\n"
)


def marker_reading(text: str) -> dict[str, str]:
    """Parses what came back from the sandbox. Missing lines stay missing.

    Only the **first three** lines are read. The pipe is shared with the check
    command's own file descriptors, so a later writer can append; it cannot
    alter what the prologue already wrote. Defaulting a missing key was a
    surviving mutation in review: a short marker that silently became "ro" is
    the same weaker-source-as-proof class this whole area is about.
    """
    zeilen = [z.strip() for z in text.splitlines() if z.strip()][:3]
    schluessel = ("mnt_ns", "net_ns", "candidate_writable")
    return dict(zip(schluessel, zeilen))


def own_namespaces() -> dict[str, str]:
    """The runner's own namespaces, for comparison with a marker."""
    raus = {}
    for kurz, pfad in (("mnt_ns", "/proc/self/ns/mnt"), ("net_ns", "/proc/self/ns/net")):
        try:
            raus[kurz] = os.readlink(pfad)
        except OSError:
            pass
    return raus


def _limits_for(spec: SandboxSpec):
    """A `preexec_fn` applying the spec's resource ceilings, or None.

    Applied between fork and exec so the sandboxed process and everything it
    starts inherit them -- a limit set on the parent only would be a limit the
    check command escapes by starting a child.

    Returns None when nothing is limited, so no hook is installed at all rather
    than one that does nothing.
    """
    gesetzt = (spec.memory_bytes, spec.max_open_files, spec.cpu_seconds,
               spec.file_size_bytes)
    if all(g is None for g in gesetzt):
        return None

    def anwenden() -> None:                      # pragma: no cover - runs post-fork
        import resource

        for kennung, wert in _rlimit_paare(spec):
            try:
                _, hart = resource.getrlimit(kennung)
                resource.setrlimit(kennung, (wert, hart))
            except (ValueError, OSError):
                # The unsandboxed twin has always tolerated this. Without the
                # guard a setrlimit failure surfaces in the parent as an
                # exception from the fork, which `run_check` does not catch --
                # so a machine with an unusual limit configuration would lose
                # the receipt entirely.
                continue

    return anwenden


def _rlimit_paare(spec: SandboxSpec):
    """The (resource, value) pairs actually applied, already clamped.

    Clamped against the **current soft limit** as well as the hard one. An
    earlier version took `min(requested, hard)`, which on a machine whose
    operator had lowered the soft limits *raised* them: a runner started under
    `ulimit -n 1024` handed the check 4096 descriptors, and a 60-second CPU
    ceiling became 600. A ceiling that can lift an existing ceiling is not a
    ceiling.
    """
    import resource

    raus = []
    for kennung, wert in (
        (resource.RLIMIT_AS, spec.memory_bytes),
        (resource.RLIMIT_NOFILE, spec.max_open_files),
        (resource.RLIMIT_CPU, spec.cpu_seconds),
        (resource.RLIMIT_FSIZE, spec.file_size_bytes),
    ):
        if wert is None:
            continue
        try:
            weich_jetzt, hart = resource.getrlimit(kennung)
        except (ValueError, OSError):        # pragma: no cover - exotic platform
            continue
        for grenze in (weich_jetzt, hart):
            if grenze != resource.RLIM_INFINITY:
                wert = min(wert, grenze)
        raus.append((kennung, wert))
    return raus


def applied_limits(spec: SandboxSpec) -> str:
    """The ceilings that will actually be in force, rendered for a receipt.

    Computed from the same clamping the `preexec_fn` performs, so the receipt
    reports what was applied rather than what was asked for. A receipt
    promising a 600-second ceiling to a check killed by SIGXCPU after 60 is
    worse than no receipt.
    """
    import resource

    namen = {
        resource.RLIMIT_AS: "as",
        resource.RLIMIT_NOFILE: "nofile",
        resource.RLIMIT_CPU: "cpu",
        resource.RLIMIT_FSIZE: "fsize",
    }
    teile = [f"{namen[k]}={v}" for k, v in _rlimit_paare(spec)]
    teile.append(f"timeout={spec.timeout}s")
    return ",".join(teile) if teile else "none"


def _env_for(spec: SandboxSpec) -> dict[str, str]:
    env = {k: os.environ[k] for k in spec.env_passthrough if k in os.environ}
    env.update({str(k): str(v) for k, v in spec.extra_env.items()})
    env["HOME"] = str(spec.scratch)
    env["TMPDIR"] = str(spec.scratch)
    if spec.proof_fd is not None:
        # Passed as variables rather than interpolated into the prologue. A
        # candidate path containing `$(...)` or a backtick was expanded by the
        # shell before the check ran, so the writability test measured a
        # substituted string instead of the candidate.
        env["HOH_PROOF_FD"] = str(spec.proof_fd)
        env["HOH_CANDIDATE"] = str(spec.candidate)
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

    #: The probe's answer, remembered. It used to run three times per check
    #: (`select`, the runner's record, and `run` itself), and it forks a
    #: process each time. Cached per instance, not per class: a long-lived
    #: process should not be pinned to a stale answer forever.
    _probe: str | None = field(default=None, repr=False, compare=False)

    def unavailable(self) -> str | None:
        if self._probe is not None:
            return self._probe or None
        self._probe = self._probe_now() or ""
        return self._probe or None

    def _probe_now(self) -> str | None:
        if not self.bwrap:
            return (
                "bwrap is not installed; install bubblewrap, or ask for "
                "Isolation.NONE explicitly if you accept running unsandboxed"
            )
        try:
            p = subprocess.run(
                [self.bwrap, "--ro-bind", "/", "/", "--unshare-all",
                 "--die-with-parent", "true"],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            # A wedged bwrap used to hang the probe with no deadline at all,
            # and the probe runs before anything is dispatched -- so the whole
            # run hung before doing any work.
            return "bwrap did not answer the availability probe within 30s"
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

    def plan(self, argv: list[str], spec: SandboxSpec) -> LaunchPlan:
        """The launch, with the proof prologue spliced into the script.

        Only a `bash -c SCRIPT` invocation can carry a shell prologue, which is
        every call this runner makes. Anything else is launched unchanged and
        proves nothing -- and a plan that proves nothing cannot be used to
        claim isolation was verified, which `run_check` enforces.
        """
        gebaut = list(argv)
        # Only a *shell* script can carry a shell prologue. An earlier version
        # keyed on `-c` alone and spliced bash into `python3 -c`, which turned
        # a working check into a SyntaxError.
        ist_shell = (
            len(gebaut) == 3
            and gebaut[1] == "-c"
            and Path(gebaut[0]).name in ("bash", "sh", "dash", "zsh")
        )
        beweist = ist_shell and spec.proof_fd is not None
        if beweist:
            gebaut[2] = MARKER_PROLOGUE + gebaut[2]
        return LaunchPlan(
            argv=self._argv(gebaut, spec), env={}, cwd=None,
            proves_isolation=beweist,
        )

    def run(self, argv: list[str], spec: SandboxSpec) -> SandboxResult:
        grund = self.unavailable()
        if grund:
            raise SandboxUnavailable(grund)
        plan = self.plan(argv, spec)
        p = subprocess.run(
            plan.argv, capture_output=True, text=True,
            timeout=spec.timeout, env=plan.env, preexec_fn=_limits_for(spec),
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

    def plan(self, argv: list[str], spec: SandboxSpec) -> LaunchPlan:
        if spec.isolation is not Isolation.NONE:
            raise SandboxUnavailable(
                f"NoSandbox cannot provide {spec.isolation.value} isolation; "
                "asking it to is a caller error, not something to degrade into"
            )
        # No proof: this backend claims no isolation, so there is nothing to
        # prove. The False is what stops a caller from presenting an
        # unsandboxed run as a verified one.
        return LaunchPlan(
            argv=list(argv), env=_env_for(spec), cwd=str(spec.candidate),
            proves_isolation=False,
        )

    def run(self, argv: list[str], spec: SandboxSpec) -> SandboxResult:
        plan = self.plan(argv, spec)
        p = subprocess.run(
            plan.argv, capture_output=True, text=True, cwd=plan.cwd,
            env=plan.env, timeout=spec.timeout, preexec_fn=_limits_for(spec),
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
