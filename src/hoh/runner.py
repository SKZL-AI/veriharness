"""Deterministic check runner.

Handoff §6: *"The runner, not the model, records the process exit code, the
run identity and the artifact hashes."* A self-report such as "all tests
green" is not a receipt. That is why every `Receipt` comes into existence
here and nowhere else, out of a real process execution.

## How solid is the execution boundary?

Named honestly, because Handoff §5 and §8 require it: the check command comes
out of a model plan and is interpreted by a shell. A pattern denylist on a
string that the shell resolves again afterwards is **not watertight in
principle**. The guard here is a tripwire against accidents, not a security
boundary against a deliberately hostile plan. It is no substitute for a real
OS sandbox (namespaces, seccomp, network isolation).

What actually constrains:

* **No login profile** -- `bash -c`, not `bash -lc`.
* **Reduced environment** -- allowlist; `HOME` and `TMPDIR` point at a fresh
  empty directory **beside** the arena (`<arena>.scratch`), not into the real
  home and not into the arena itself. Credentials under `~/.ssh`, `~/.config`,
  `~/.claude` cannot be reached via `$HOME`; and what a check writes through
  them does not appear inside the object under test (O48, see
  `_scratch_dir`).
* **Output is streamed**, not buffered: stdout and stderr go straight into a
  file whose size is watched; when it is exceeded, the process dies.
* **Time and size limits** via `setrlimit` in the child, plus a process group
  kill.
* **The arena** -- a run happens on an isolated copy, never in the live
  workspace.

### What is deliberately NOT limited here

`RLIMIT_NPROC` is deliberately not set. On Linux it counts **all tasks of the
real UID**, not the children of this run. An earlier version set it to 512 and
thereby made every check unusable that forks at all -- every pipeline, every
`pytest`, every `make`. Worse: the failure came back as an ordinary exit code
and was recorded as **FAIL**, so the loop would have "repaired" product code
against a runner defect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import sandbox as sandbox_mod

import datetime
import os
import platform
import re
import resource
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from .contracts import (
    INFRA_EXIT_CODES,
    AcceptanceCheck,
    Candidate,
    ISOLATION_REFUSED,
    ISOLATION_UNVERIFIED,
    IsolationRecord,
    Receipt,
    digest,
    utcnow,
)

#: Pattern files, searched in this order.
#:
#: 1. The operator. The canonical version on this machine wins, so that a
#:    sharpened pattern takes effect everywhere immediately and is not
#:    overruled by an older shipped copy.
#: 2. The **package**. Until 2026-09-07 this step was missing, and that made
#:    an installed version unusable: `parent.parent.parent / "policy"` points
#:    at `<venv>/lib/python3.13/policy` for a wheel -- and nothing is there.
#:    `assert_command_allowed` therefore raised `PolicyUnavailable` on
#:    **every** check, so a pip-installed HoH could not run a single
#:    acceptance criterion. It never showed up locally, because step 1 exists
#:    on this machine.
#: 3. The repo layout, so that the tests also run in a plain checkout.
_HOME_PATTERNS = Path.home() / ".agents/hooks"
_PACKAGE_PATTERNS = Path(__file__).resolve().parent / "policy"
_REPO_PATTERNS = Path(__file__).resolve().parent.parent.parent / "policy"


def _pattern_search_path() -> tuple[Path, ...]:
    return (_HOME_PATTERNS, _PACKAGE_PATTERNS, _REPO_PATTERNS)


def _pattern_file(name: str) -> Path:
    for base in _pattern_search_path():
        candidate = base / name
        if candidate.exists():
            return candidate
    #: No hit: return the place the file belongs to, so that the error message
    #: points at the package and not at some accidental path.
    return _PACKAGE_PATTERNS / name


DEFAULT_TIMEOUT = 600
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
KILL_GRACE_SECONDS = 5
POLL_INTERVAL = 0.05

ENV_ALLOWLIST = (
    "PATH", "LANG", "LC_ALL", "TERM", "TZ", "SHELL", "USER", "LOGNAME",
    "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "CI",
)

#: The one sentence with which a receipt from a different runner is rejected.
#: It used to be typed out independently here and in `controller.py`, one
#: module apart -- two authors of the same claim, and a test pinning only one
#: of them. During the translation to English the two halves drifted for
#: exactly as long as it took someone to notice. Same class as the prose
#: coupling that carried `WaitingForApproval`: whoever changes the wording
#: changes it here, and both sides follow.
FOREIGN_RUNNER = "Receipt {receipt_id} does not come from this runner"

RLIMIT_CPU_SECONDS = 600
RLIMIT_ADDRESS_SPACE = 4 * 1024 * 1024 * 1024
RLIMIT_FILE_SIZE = 512 * 1024 * 1024
#: Open file descriptors. Only the sandboxed path sets this one: the
#: unsandboxed path never did, and lowering a ceiling for existing runs is a
#: behaviour change nobody asked for. Generous on purpose -- a ceiling that
#: ordinary test suites hit is a ceiling people disable.
RLIMIT_OPEN_FILES = 4096


class HouseRuleViolation(RuntimeError):
    """A check command violates the house rules and is not run."""


class PolicyUnavailable(RuntimeError):
    """The pattern files are missing. The runner then runs **nothing**.

    Deliberately different from the shell hooks: those fail open, so that a
    broken setup does not block every tool call the captain makes. Here it is
    about model-generated commands -- without a policy there is no reason to
    trust them.
    """


# --------------------------------------------------------------------------- #
# POSIX-ERE -> Python
# --------------------------------------------------------------------------- #

_POSIX_CLASSES = {
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "alnum": "a-zA-Z0-9",
    "space": r" \t\n\r\f\v",
    "upper": "A-Z",
    "lower": "a-z",
    "punct": re.escape("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"),
    "xdigit": "0-9A-Fa-f",
}
_CLASS_RX = re.compile(r"\[:([a-z]+):\]")


def ere_to_python(pattern: str) -> str:
    """Translates POSIX character classes **inside** the bracket expressions.

    The earlier version replaced only the string `[[:space:]]` as a whole.
    That left `[^[:space:]]` in place, and in Python it meant something
    completely different: exactly one character outside of `{[:space]}`,
    followed by a literal `]`. Patterns with negation were practically
    ineffective because of it, and Python itself warned with
    `FutureWarning: Possible nested set`.
    """
    return _CLASS_RX.sub(
        lambda m: _POSIX_CLASSES.get(m.group(1), m.group(0)), pattern
    )


def _load_patterns(
    path: Path,
) -> tuple[list[tuple[str, re.Pattern[str]]], list[tuple[str, str]]]:
    """Compiles a pattern file and reports what it could **not** compile.

    Returns the usable patterns and the lines that failed, each with the
    compiler's message.

    Earlier this function swallowed a `re.error` with `continue` and the
    comment *"skip a broken line, never abort the check"*. The intent was
    sound -- one typo should not take the whole guard down -- but the effect
    was that the guard silently protected **less** than its file said, and
    nothing anywhere reported the difference. Measured: a file with three
    rules, one of them malformed, loaded two and returned no indication that
    a rule had gone missing. That is precisely the failure this project is
    built against: a protection that looks present and is not.

    Which of the two properties survives is decided by the caller, not here.
    This function only makes the loss **visible**.
    """
    out: list[tuple[str, re.Pattern[str]]] = []
    broken: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append((line, re.compile(ere_to_python(line))))
        except re.error as exc:
            broken.append((line, str(exc)))
    return out, broken


#: The placeholder a plan uses for the path of the object under test.
#: The runner substitutes it -- the plan does not know the arena path at all.
ARENA_PLACEHOLDER = "{ARENA}"

# Command position is more than line start and `;&|`: a subshell `(`, a block
# `{`, a newline and `eval`/`bash -c` open one just as well. A reviewer
# demonstrated `(cd .. && ls)`, `{ cd ..; ls; }`, `x=1 cd ..` and the bare
# newline as bypasses.
# 2026-09-08, found at the step 0 gate: the command position knew only the
# separators `;&|(){}` and a newline. A shell keyword opens one too, so
# `if true; then cd x; fi` and `for i in 1; do cd x; done` walked past --
# after `then` and `do` there is only a space. `builtin cd x` and `\cd x`
# (the backslash suppresses an alias) were missing from the wrapper list.
_DIRECTORY_CHANGE = re.compile(
    r"(^|[;&|(){}\n]|&&|\|\||\b(?:then|do|else|elif)\s)\s*"
    r"([A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+)*"
    r"((eval|exec|command|builtin|env|nohup|time)\s+[\"']?)*"
    r"\\?(cd|pushd|popd|chdir)(\s|$)"
)
# `//home/...` is identical to `/home/...` on Linux; the lookbehind fell for
# that. Multiple slashes are therefore normalized before the check (see
# `assert_stays_in_arena`).
_ABSOLUTE_PATH = re.compile(r"(?<![\w/])(/home/|/root/|/etc/|/mnt/[a-z]/|~/)")
# A relative ascent leads out of the object under test -- and the arena sits
# next to `runs/<id>/`, so `../../checks.json`, `../../state.json` and
# `../../receipts/` are reachable from there. A reviewer used it to empty the
# preservation suite from inside the object under test; the next iteration ran
# without regression protection and accepted the regression.
#
# 2026-09-08, found by an adversarial reviewer at the step 0 gate: the first
# version demanded a delimiter from `[\s;&|(){}="']` **before** the `..` and
# did not list `/`. One leading path component was therefore enough to walk
# straight past it -- `cat ./../../a02/state.json` and
# `cp fake.json ./../../a02/checks.json` were both ALLOWED, and the path
# resolves to a file that exists. The end anchor had the mirror-image gap: it
# knew `/`, whitespace, end and quotes, so `ls ..;true` passed as well.
#
# `..` is now refused as a path component **wherever** it appears. That also
# refuses the harmless-looking `tests/../src`, which normalizes back inside
# the arena -- deliberately: a guard that has to decide whether an ascent
# comes back is a guard that can be argued with. The message names the way
# around it.
_PARENT_ESCAPE = re.compile(
    r"""(^|[\s;&|(){}=\n"'/])\.\.($|[/\s;&|(){}"'])"""
)
# A shell smuggled in underneath bypasses every pattern check on the outer
# command.
#
# 2026-09-08, same reviewer: `-c` had to follow the shell name immediately, so
# `bash --norc -c ls`, `bash -o pipefail -c ls` and the herestring form
# `bash<<<"ls"` all passed. Any tokens in between are now skipped -- as long as
# they carry no command separator, because past a `;` a new command begins and
# the other patterns are responsible for it.
_SHELL_C = re.compile(
    r"\b(sh|bash|zsh|dash|ksh)\b"
    r"(?:\s+(?!-[A-Za-z]*c\b)[^\s;|&]+)*"
    r"\s+-[A-Za-z]*c\b"
)
#: The herestring form feeds a shell without ever writing `-c`.
_SHELL_HERESTRING = re.compile(r"\b(sh|bash|zsh|dash|ksh)\s*<<<")


class ArenaEscape(RuntimeError):
    """A check command leaves the object under test.

    The runner sets `cwd` to the isolated arena -- that is not enough, because
    the command comes out of a model plan and is interpreted by a shell. In the
    first successful Herdr run, four out of five acceptance checks ran in the
    **live tree** via `cd <live-worktree> && ...`, and the receipts still
    carried the candidate binding: that binding is a label from the candidate
    object, not a measurement. The QA agent noticed it and wrote it into a
    note; the controller accepted all five as PASS.

    Afterwards exactly these commands with their absolute paths moved into the
    preservation suite and from then on ran against a directory that had
    nothing to do with the object under test any more.
    """


def assert_stays_in_arena(command: str) -> None:
    """Rejects every escape from the object under test.

    No directory change, no absolute path into the home. If a check needs the
    candidate's path, it writes `{ARENA}` -- the runner substitutes it. System
    paths (`/usr/bin/python3`) stay allowed.
    """
    # `//home` == `/home`, `a/./b` == `a/b`: normalize before the check,
    # otherwise you check a different string than the one the shell runs.
    normalized = command.replace(ARENA_PLACEHOLDER, "ARENA")
    normalized = re.sub(r"/{2,}", "/", normalized)
    normalized = re.sub(r"/\.(?=/)", "", normalized)

    ascent = _PARENT_ESCAPE.search(normalized)
    if ascent:
        raise ArenaEscape(
            f"Check command ascends out of the object under test ('..'): {command!r}. "
            f"The arena sits in the run directory; above it lie the evidence, the "
            f"state and the preservation suite. Use paths inside the object under "
            f"test or {ARENA_PLACEHOLDER}."
        )

    shell = _SHELL_C.search(normalized) or _SHELL_HERESTRING.search(normalized)
    if shell:
        raise ArenaEscape(
            f"Check command starts a shell of its own ({shell.group(1)}): "
            f"{command!r}. Its content cannot be checked from the outside; a "
            f"criterion has to be readable as a command."
        )

    hit = _DIRECTORY_CHANGE.search(normalized)
    if hit:
        raise ArenaEscape(
            f"Check command changes the directory ({hit.group(5)}): {command!r}. "
            f"The runner already works in the object under test; a change would make "
            f"the binding worthless. If you need the path, write {ARENA_PLACEHOLDER}."
        )

    path = _ABSOLUTE_PATH.search(normalized)
    if path:
        raise ArenaEscape(
            f"Check command names an absolute path outside the object under "
            f"test ({path.group(1)}...): {command!r}. "
            f"Use relative paths or {ARENA_PLACEHOLDER}."
        )


def assert_command_allowed(command: str) -> None:
    """Fail loud before anything runs."""
    sources = [
        ("house rule", _pattern_file("house-rules-patterns.txt")),
        ("denylist", _pattern_file("dangerous-patterns.txt")),
    ]
    missing = [str(p) for _, p in sources if not p.exists()]
    if missing:
        searched = ", ".join(str(b) for b in _pattern_search_path())
        raise PolicyUnavailable(
            "Guard pattern files are missing: "
            + ", ".join(missing)
            + f" -- searched in: {searched}"
            + " -- without a policy no model-generated command is run."
        )

    # A pattern file that does not compile completely makes the guard weaker
    # than it reads, so this fails closed rather than running with a reduced
    # denylist. The message names file, line and compiler error, because the
    # repair is a few seconds once one knows where to look -- and because the
    # alternative, carrying on quietly, is the very thing being guarded
    # against. A missing policy already refuses to run; a *half* policy has
    # no better claim to being trusted.
    loaded: list[tuple[str, str, re.Pattern[str]]] = []
    unusable: list[str] = []
    for source, path in sources:
        patterns, broken = _load_patterns(path)
        loaded.extend((source, pattern, rx) for pattern, rx in patterns)
        unusable.extend(
            f"{path.name}: {line!r} ({source}) -- {error}" for line, error in broken
        )
    if unusable:
        raise PolicyUnavailable(
            "Guard pattern files contain lines that do not compile: "
            + "; ".join(unusable)
            + " -- a partially loaded guard protects less than it states,"
            " so no model-generated command is run until this is fixed."
        )

    for source, pattern, rx in loaded:
        if rx.search(command):
            raise HouseRuleViolation(
                f"Check command violates {source} '{pattern}': {command!r}. "
                f"An acceptance criterion must not bypass the guards."
            )


def runner_identity() -> str:
    return f"{socket.gethostname()}/pid{os.getpid()}/py{platform.python_version()}"


def _unsandboxed_limits() -> list[tuple[int, int]]:
    """The ceilings this path applies, already clamped, as (resource, value).

    Clamped against the **current soft** limit as well as the hard one. The
    previous form took `min(limit, hard)` and therefore *raised* an operator's
    ceilings: a runner started under `ulimit -t 60` handed the check 600
    CPU-seconds. The sandboxed path was corrected for this (O107) and this one
    was left behind -- the same defect in the twin, which is how a fix stops
    being a fix.
    """
    raus = []
    for res, limit in (
        (resource.RLIMIT_CPU, RLIMIT_CPU_SECONDS),
        (resource.RLIMIT_AS, RLIMIT_ADDRESS_SPACE),
        (resource.RLIMIT_FSIZE, RLIMIT_FILE_SIZE),
    ):
        try:
            weich, hart = resource.getrlimit(res)
        except (ValueError, OSError):            # pragma: no cover - exotic
            continue
        wert = limit
        for grenze in (weich, hart):
            if grenze != resource.RLIM_INFINITY:
                wert = min(wert, grenze)
        raus.append((res, wert))
    return raus


def _limits() -> None:
    """Runs in the child process between fork and exec.

    No `RLIMIT_NPROC` -- see the module docstring.
    """
    for res, wert in _unsandboxed_limits():
        try:
            _, hard = resource.getrlimit(res)
            resource.setrlimit(res, (wert, hard))
        except (ValueError, OSError):
            continue
    os.setsid()  # own process group, so that a timeout reaches the whole family


#: What the policy fields say when nothing was applied because nothing ran.
NICHT_ANGEWANDT = "not applied: isolation refused"


def _sandbox_preexec(spec):
    """The sandboxed launch's `preexec_fn`: the spec's ceilings **and** a new
    session.

    `os.setsid()` is not optional here and its absence was not theoretical.
    `_supervise` ends an overrunning check with `os.killpg(os.getpgid(pid))`.
    Without a session of its own the sandboxed process shares the *runner's*
    process group -- so the first timeout in the sandboxed path sent SIGTERM
    to the runner, its parent, and everything else in that group. The test
    suite killed itself on the first run after the supervisor was wired in,
    which is the loudest possible way to find out and still an accident away
    from being found in production instead.

    bwrap's own `--new-session` does not help: it creates a session for the
    process *inside* the sandbox, while the one that needs its own group is
    bwrap itself.
    """
    from .sandbox import _limits_for

    grenzen = _limits_for(spec)

    def anwenden() -> None:                      # pragma: no cover - runs post-fork
        if grenzen is not None:
            grenzen()
        os.setsid()

    return anwenden


#: Signatures of a baseline run that executed **nothing**. Each is a way a
#: test runner says "there was no test here", which is what happens when the
#: criterion's own file is part of the candidate and therefore absent from the
#: state it is measured against.
ARTEFACTUAL_SIGNATUREN = (
    "NO TESTS RAN",
    "no tests ran",
    "FileNotFoundError",
    "ModuleNotFoundError",
    "ImportError: cannot import name",
    "error: file or directory not found",
    "collected 0 items",
)


def artefactual_reason(exit_code: int, transcript: str) -> str:
    """Why a red baseline may not demonstrate an increment, or "".

    A criterion that is red on the predecessor and green on the candidate is
    the project's definition of demonstrating an increment. That definition
    has a hole: if the criterion *is* a file the candidate added, the
    predecessor is red because the file is missing, and every new test file
    satisfies it without saying anything about behaviour.

    Measured on this project's own STRICT acceptance run: the accepted
    candidate's only discriminating criterion was
    `python3 -m unittest discover -s tests -p test_roman.py`, whose baseline
    exited 5 with `Ran 0 tests ... NO TESTS RAN`. The increment was real; the
    criterion did not demonstrate it.

    This does not decide acceptance -- it names the weakness so the evidence
    carries it. Deliberately conservative: it looks for a runner saying
    outright that nothing executed, not for a heuristic about what a failure
    means.
    """
    if exit_code == 5:
        # pytest's and unittest's convention for "no tests were collected".
        return "the baseline collected no tests at all (exit 5)"
    ausgabe = transcript.split("--- output ---", 1)[-1]
    for sig in ARTEFACTUAL_SIGNATUREN:
        if sig in ausgabe:
            return (
                f"the baseline did not execute the criterion ({sig!r} in its "
                "output): it is red because the candidate's own file is not "
                "there yet, not because the behaviour is absent"
            )
    return ""


def _own_ns() -> dict[str, str]:
    from .sandbox import own_namespaces

    return own_namespaces()


def _drain_proof(fd: int, hoechstens: int = 4096) -> list[str]:
    """Collects what came out of the sandbox, in a thread, until the pipe closes.

    Drained rather than read-once for a specific reason: the write end is open
    inside the sandbox and the check command inherits it. A hostile or merely
    chatty check could fill the pipe and block on its own write, which would
    hang the check rather than fail it. So everything is read and only the
    first `hoechstens` bytes are kept -- and of those, only the first three
    lines are ever parsed.

    Decoding is `errors="replace"`. A `UnicodeDecodeError` here used to escape
    `run_check` altogether -- the exception is a `ValueError`, not an
    `OSError`, so the guard missed it -- and a run ended with no receipt at
    all, which is the one outcome this module exists to prevent.
    """
    stuecke: list[bytes] = []
    behalten = 0
    with os.fdopen(fd, "rb", closefd=True) as fh:
        while True:
            block = fh.read(65536)
            if not block:
                break
            if behalten < hoechstens:
                stuecke.append(block[: hoechstens - behalten])
                behalten += len(stuecke[-1])
    return "".join(
        b.decode("utf-8", "replace") for b in stuecke
    ).splitlines()


def _isolation_measured(requested, beweis: dict[str, str], spec):
    """What isolation the run *demonstrably* had.

    Returns `(effective, mount_mode, network_policy, complaint)`. The
    complaint is empty when the measurement supports the request.

    This function is the answer to a specific defect. bubblewrap 0.9 exits 1
    both when its own setup fails and when the command it launched exits 1, and
    `subprocess` cannot tell those apart. So a bind that could not be
    established -- an exhausted namespace, an EPERM inside a container, an
    arena renamed between plan and launch -- produced exit 1, `runner_ok=True`
    and a receipt saying `effective=strict`, while nothing had executed. A
    check written to expect a non-zero exit would have *passed* off a sandbox
    that never started.

    The marker closes it, and closes more than that: it is written from inside
    the sandbox before the real command runs, and it carries the namespace ids
    and the candidate's writability as observed there. Comparing them with the
    runner's own namespaces distinguishes all four cases -- never started, ran
    in the runner's namespaces, ran isolated, ran isolated but writable.
    """
    from .sandbox import Isolation as _I, own_namespaces

    eigen = own_namespaces()
    if not eigen:
        # Without the runner's own namespaces there is nothing to compare
        # against. The previous form skipped the comparison silently, which
        # made the check fail *open*: a run in the runner's own namespaces
        # would have passed unnoticed.
        return ISOLATION_UNVERIFIED, NICHT_ANGEWANDT, NICHT_ANGEWANDT, (
            "the runner could not read its own namespaces, so the isolation "
            "it asked for cannot be compared against anything"
        )
    if not beweis:
        return ISOLATION_UNVERIFIED, NICHT_ANGEWANDT, NICHT_ANGEWANDT, (
            f"isolation {requested.value} was requested and the command left no "
            "proof it ever started inside a sandbox; bubblewrap reports its own "
            "setup failures with the same exit code a failing check uses, so "
            "this is treated as a refusal and not as a verdict"
        )
    gleich = [
        name for name in ("mnt_ns", "net_ns")
        if name in eigen and beweis.get(name) == eigen[name]
    ]
    if gleich:
        return _I.NONE, "read-write", "allowed", (
            f"isolation {requested.value} was requested but the command ran in "
            f"the runner's own {', '.join(gleich)}: it was not isolated"
        )
    mount = {
        "ro": "read-only", "rw": "read-write", "absent": "not present",
    }.get(beweis.get("candidate_writable", ""), "unknown")
    # Derived from the measurement, not from the spec the runner itself wrote.
    # `"denied" if not spec.network` was an echo of the request dressed as an
    # observation -- the same defect in the same field, one layer down.
    netz = (
        "denied"
        if beweis.get("net_ns") and beweis["net_ns"] != eigen.get("net_ns")
        else "allowed"
    )
    if mount == "not present":
        # `[ -w path ]` is false for a path that is not there, so two-valued
        # logic reported a candidate that had never been mounted as read-only:
        # the precise failure this proof exists to catch produced a clean
        # receipt, with no adversarial check involved.
        return requested, mount, netz, (
            f"isolation {requested.value} was requested and the candidate is "
            "not present inside the sandbox at all; the check measured "
            "something other than the object under test"
        )
    if mount != "read-only":
        return requested, mount, netz, (
            f"isolation {requested.value} was requested but the candidate was "
            f"{mount} inside the sandbox; a check that can rewrite what it "
            "checks has invalidated its own result"
        )
    if netz != "denied":
        return requested, mount, netz, (
            f"isolation {requested.value} was requested and the command shared "
            "the runner's network namespace"
        )
    return requested, mount, netz, ""


def _limit_policy() -> str:
    """The unsandboxed path's ceilings **as applied**, rendered for the receipt.

    Written out rather than left blank because "no record" and "no limits" are
    different claims. It used to print the constants, so a receipt promised 600
    CPU-seconds to a check that a lowered operator limit killed at 60 -- which
    `applied_limits`' own docstring calls worse than no receipt, on the same
    contract field.
    """
    namen = {
        resource.RLIMIT_CPU: "cpu",
        resource.RLIMIT_AS: "as",
        resource.RLIMIT_FSIZE: "fsize",
    }
    return ",".join(f"{namen[res]}={wert}" for res, wert in _unsandboxed_limits())


def _scratch_dir(workdir: Path) -> Path:
    """Where a check command's `HOME` and `TMPDIR` point: **beside** the arena.

    O48, measured 2026-09-08. Both used to point at `workdir` itself, so
    everything a check command wrote through them landed **inside the object
    under test**: `pytest` created `pytest-of-<user>/` there (28 of 52 arenas in
    this project's own history carry it, with an mtime about 1.6 s after the tree
    was materialized), and `pip` created `.cache/pip/`. All checks of one
    candidate share one arena, so a criterion that enumerates the check
    directory sees what an *earlier* criterion of the same iteration left
    behind -- and the answer to "which files are here" became
    order-dependent without any specification saying so.

    That cost three consecutive rejections whose candidates were correct:
    `r1` i1 and i2 (a manifest criterion counting 5719 files instead of 148),
    and `dugf` i1 (an unchanged-files criterion over the same inflated set).

    A sibling keeps the property the redirection exists for -- `$HOME` is a
    fresh empty directory, so credentials under the real `~/.ssh`, `~/.config`
    or `~/.claude` stay unreachable -- and removes the pollution of the
    enumerated tree. It is deliberately **not** a subdirectory of the arena:
    a reserved name inside the tree would only move the burden to every future
    criterion, which is the growing-exclusion-list shape this project rejects
    elsewhere.
    """
    scratch = workdir.with_name(workdir.name + ".scratch")
    try:
        # A scratch directory this process did not create is **parked**, not
        # reused. Its name is derivable from the run's own state, and `HOME`
        # points at it: a directory planted there before the check starts can
        # carry a `usercustomize.py`, which Python imports before the
        # command's first line runs. The arena does the same thing for the
        # same reason; this was the half that did not.
        #
        # Parked *once* rather than per check. All the checks of one candidate
        # share an arena and therefore a scratch name, so parking on every call
        # would leave one directory per check -- measured at up to 118 MB each
        # on this project's own runs -- and would throw away the pip and pytest
        # caches between two checks of the same candidate.
        _frisches_scratch(scratch)
    except OSError:
        # Falling back to the old behaviour is worse than a sibling and better
        # than failing the run: a check that cannot start produces no verdict
        # at all, which is the one outcome this project treats as the most
        # expensive. The pollution is visible; a missing verdict is not.
        return workdir
    return scratch


def _frisches_scratch(scratch: Path) -> Path:
    """Create a scratch directory this process owns, parking any it finds.

    Shared by both regimes. The sandboxed path had this hole open after the
    unsandboxed one was closed, which is the half-fixed shape this project
    keeps finding in its own work.
    """
    # Keyed on the path **and its inode**. The registry outlives the
    # directory, so a path we created once and something else replaced in the
    # meantime was adopted as ours -- a reviewer planted a `usercustomize.py`
    # between two checks of the same candidate and it survived, which is
    # exactly the pre-plant the parking exists to stop.
    unser = _EIGENE_SCRATCHES.get(str(scratch))
    jetzt = scratch.stat().st_ino if scratch.exists() else None
    if unser is None or jetzt != unser:
        if scratch.exists():
            stempel = datetime.datetime.now(
                datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
            scratch.rename(scratch.with_name(f"{scratch.name}.v{stempel}"))
        scratch.mkdir(parents=True)
        _EIGENE_SCRATCHES[str(scratch)] = scratch.stat().st_ino
    return scratch


#: Scratch directories this process created, path -> inode. A directory in
#: here was made by this runner and is safe to reuse; one that is not, or one
#: whose inode no longer matches, was put there by something else -- which is
#: the situation the parking exists for.
_EIGENE_SCRATCHES: dict[str, int] = {}


def _env(workdir: Path, extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    scratch = _scratch_dir(workdir)
    env["HOME"] = str(scratch)
    env["TMPDIR"] = str(scratch)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # `HOME` points at the scratch directory, and a user site directory under
    # it would be imported before the command runs. Parking an existing
    # scratch directory closes the pre-plant; this closes the route itself.
    env["PYTHONNOUSERSITE"] = "1"
    # O30: a frozen candidate is materialized with `git archive` and therefore
    # has **no `.git` of its own**. Without this, git's discovery walk climbs
    # out of the arena and binds silently to the ancestor repository -- and
    # that repository's `.gitignore` excludes the whole `runs/` subtree the
    # arena lives under, so files in the arena come back as "untracked". A
    # criterion that diffed the candidate against its base therefore compared
    # against something else entirely and failed while reporting nothing about
    # its real subject. Measured 2026-09-08: `git rev-parse --show-toplevel`
    # in an arena directory answered with the project checkout.
    #
    # The ceiling stops the walk *above* the arena, so a real repository at
    # `workdir` is still found, while an arena without one now yields
    # `fatal: not a git repository` -- loudly wrong instead of quietly wrong.
    # This is K11's family one level up: K11 hit the candidate binding, this
    # hits the criteria.
    env["GIT_CEILING_DIRECTORIES"] = str(workdir.parent)
    env.update(extra or {})
    return env


def _terminate(proc: subprocess.Popen) -> None:
    """Ends the whole process group, by force if need be.

    An earlier version broke out of the signal loop on the first `OSError`,
    without having tried SIGKILL.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def run_check(
    check: AcceptanceCheck,
    candidate: Candidate,
    *,
    run_id: str,
    iteration: int,
    attempt: int,
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    receipt_suffix: str = "",
    isolation: "sandbox_mod.Isolation | None" = None,
    sandbox: "sandbox_mod.SandboxBackend | None" = None,
) -> tuple[Receipt, str]:
    """Runs a check and returns (Receipt, transcript text).

    Output is **streamed**: stdout and stderr go straight into a file whose
    size is watched. An earlier version used `communicate()` and truncated
    only afterwards -- so the controller went on buffering the full output in
    memory, measured at 430 MB for 150 MB of check output.

    A timeout, exceeding the output limit or a process that cannot be started
    is an **infrastructure error**: `runner_ok=False`, and `Receipt.outcome()`
    then returns `INCONCLUSIVE` -- independently of `expect_exit`.

    **Isolation.** `isolation` selects how the command is executed:

    * `NONE` -- the historical path. A bash process with a reduced environment
      and `setrlimit`, which the module docstring above is careful to call a
      tripwire rather than a boundary. It stays the default so that nothing
      about adding a sandbox silently changes what existing runs do.
    * `STRICT` -- executed inside a `SandboxBackend`. If no backend can provide
      it, the check does **not** fall back: it fails closed with
      `runner_ok=False`, which reaches the controller as `INCONCLUSIVE` rather
      than as a verdict. A sandbox that silently degrades is worse than none,
      because the reason to ask for one is the assumption that it is there.

    A sandboxed run is not merely a differently-spawned process, and the
    receipt says which it was: `note` records the isolation applied, so a
    receipt read six months later does not have to be guessed at.
    """
    from .sandbox import (
        Isolation as _Isolation,
        SandboxSpec,
        SandboxUnavailable,
        applied_limits,
        select as _select,
    )

    if isolation is None:
        isolation = _Isolation.NONE
    elif not isinstance(isolation, _Isolation):
        # A plain string used to slip past every `is` comparison in here: the
        # NONE path was never taken, the command ran sandboxed, and the
        # function then raised AttributeError while building the record -- an
        # execution with no receipt, which is the one outcome this module is
        # built to make impossible.
        try:
            isolation = _Isolation(isolation)
        except ValueError:
            raise ValueError(
                f"unknown isolation {isolation!r}; expected one of: "
                + ", ".join(i.value for i in _Isolation)
            ) from None

    assert_command_allowed(check.command)
    assert_stays_in_arena(check.command)

    workdir = Path(cwd or candidate.repo_path)
    resolved_command = check.command.replace(ARENA_PLACEHOLDER, str(workdir))
    # `receipt_suffix` separates evidence that belongs to the same iteration
    # and the same criterion but measures different states: the candidate run
    # carries no suffix, the baseline check carries "basis" (that value stays
    # German for compatibility -- it goes into persisted `receipt_id`s and into
    # file names on disk). Without it the two collided, and `_write_immutable`
    # would have rejected the second -- which is why the evidence of the
    # baseline check used not to be written at all.
    receipt_id = f"{run_id}-i{iteration}-a{attempt}-{check.check_id}"
    if receipt_suffix:
        receipt_id = f"{receipt_id}-{receipt_suffix}"
    started = utcnow()
    t0 = time.monotonic()
    runner_ok = True
    truncated = False
    note = ""

    # The raw buffer lives **beside** the object under test, never inside it.
    #
    # Until 2026-09-08 it was `workdir / f".hoh-out-{receipt_id}.log"` -- that
    # is, in the very directory the measured command runs in, under a name
    # derived from run, iteration, attempt and check and therefore entirely
    # predictable. A check could overwrite or truncate its own raw transcript
    # while running, and the runner then read it back into `stdout_digest` and
    # into the immutable log. An adversarial reviewer found it at the step 0
    # gate. It could not fake a PASS -- `exit_code` is measured independently
    # via `proc.poll()`, and acceptance hangs on the exit code alone -- but a
    # misleading transcript is what a human and the QA role read, and this
    # project exists to stop exactly that.
    sink = workdir.parent / f".hoh-out-{receipt_id}.log"

    # O14: creating the directories and opening the sink used to sit *outside*
    # every `try`. An `OSError` there -- a read-only parent, an exhausted
    # inode table, and above all **a full disk** -- left `run_check` through
    # the exception instead of through a receipt. The controller then saw a
    # raw `PermissionError` or `ENOSPC` where the contract promises an
    # infrastructure error: `runner_ok=False`, hence `INCONCLUSIVE`, hence a
    # visible non-verdict rather than a crash. Reproduced 2026-09-08 by
    # chmod-ing the sink's parent to read-only. This is also the shape A08
    # (disk full) takes, which is why it is worth more than its size suggests:
    # the difference between "the run stopped" and "the run says why".
    # Resolve the backend before anything is created. Failing closed here,
    # before a sink or a directory exists, keeps a refused sandbox from leaving
    # half a run's worth of artifacts behind.
    backend_probe = ""
    abgelehnt = ""
    if isolation is not _Isolation.NONE and sandbox is None:
        try:
            sandbox = _select(isolation)
        except SandboxUnavailable as exc:
            sandbox = None
            isolation_error = str(exc)
            backend_probe = isolation_error
        else:
            isolation_error = ""
    else:
        isolation_error = ""
    if sandbox is not None and not backend_probe:
        # Asked once, recorded. The probe is what turns "bwrap is installed"
        # into "bwrap can create the namespace here", and its answer belongs
        # in the evidence rather than only in the decision it caused.
        backend_probe = sandbox.unavailable() or ""
        # O106: an *injected* backend used to skip the probe entirely. `select()`
        # is fail-closed, so the path that resolves a backend itself refused an
        # unavailable one -- but a caller passing `sandbox=` went straight to
        # `run()`, and whether that failed closed then depended on the backend
        # choosing to re-probe inside its own `run`. BubblewrapSandbox does;
        # nothing required it to. That made fail-closed a property of one
        # implementation rather than of the contract. Found by writing the
        # negative control for the isolation record, which is the whole reason
        # the falsifier rule exists.
        if backend_probe and isolation is not _Isolation.NONE:
            isolation_error = (
                f"the supplied backend {sandbox.name!r} reports itself "
                f"unusable: {backend_probe}"
            )
            abgelehnt = sandbox.name
            sandbox = None

    # What actually happened, filled in as it happens. Every field below is
    # either measured after the fact or says outright that it was not applied.
    # The previous version set `network_policy` and `candidate_mount_mode`
    # from the spec the runner had just written -- an echo of the request
    # dressed as a measurement, which is the exact inference this record was
    # added to remove.
    angewandt = _Isolation.NONE
    eigen_ns: dict[str, str] = {}
    isolation_grund = ""
    backend_name = sandbox.name if sandbox is not None else abgelehnt
    netz = "allowed"
    mount = "read-write"
    grenzen = _limit_policy()
    beweis: dict[str, str] = {}
    if isolation_error:
        # Nothing ran, so nothing was applied. Reporting the unsandboxed
        # regime's values here would describe a process that never existed --
        # a receipt has to be readable without knowing which branch produced
        # it.
        netz = mount = grenzen = NICHT_ANGEWANDT

    buffer: object | None = None
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        sink.parent.mkdir(parents=True, exist_ok=True)
        buffer = open(sink, "wb")
    except OSError as exc:
        exit_code, runner_ok = 127, False
        note = f"Output buffer could not be created: {exc}"
    if isolation_error:
        # Asked for isolation, could not have it. Not a product verdict: a
        # check that did not run in the regime it was supposed to has measured
        # nothing, and saying so is the whole point of failing closed.
        exit_code, runner_ok = 126, False
        note = f"Isolation {isolation.value} was requested and is unavailable: {isolation_error}"
        isolation_grund = note
    elif buffer is not None and sandbox is not None and isolation is not _Isolation.NONE:
        with buffer as fh:
            spec = None
            lese_ende = schreib_ende = None
            beweis_zeilen: list[str] = []
            gestartet = False
            try:
                # Parked exactly as the unsandboxed path's scratch is, and
                # for a reason a reviewer demonstrated rather than argued: the
                # name is derivable from the run's own state, this directory is
                # the check's `$HOME`, and a `usercustomize.py` planted there
                # beforehand executed **inside bubblewrap**, before the
                # command's first line, under a receipt that says "verified
                # from inside". The strongest-looking receipt was the
                # vulnerable one.
                scratch = _frisches_scratch(sink.parent / f".hoh-scratch-{receipt_id}")
                # The proof travels out over a pipe this process holds. It used
                # to be a file in the scratch directory -- which is
                # bind-mounted read-write and is also the check's $HOME and
                # $TMPDIR -- and it was read after the check finished, so the
                # untrusted command owned the evidence about itself. A
                # three-line echo was enough to make a bare unsandboxed bash
                # report honoured isolation.
                lese_ende, schreib_ende = os.pipe()
                os.set_inheritable(schreib_ende, True)
                spec = SandboxSpec(
                    candidate=workdir, scratch=scratch, isolation=isolation,
                    timeout=timeout,
                    # O107: these were left at None, so requesting STRICT
                    # removed the address-space, CPU and file-size ceilings the
                    # *weaker* path applies. Stronger isolation must not mean
                    # weaker limits; the same constants govern both paths.
                    memory_bytes=RLIMIT_ADDRESS_SPACE,
                    max_open_files=RLIMIT_OPEN_FILES,
                    cpu_seconds=RLIMIT_CPU_SECONDS,
                    file_size_bytes=RLIMIT_FILE_SIZE,
                    # `env=` used to be dropped silently on this path, so the
                    # same call produced different environments depending on
                    # isolation. No in-tree caller passed it, which is how it
                    # stayed unnoticed.
                    extra_env=dict(env or {}),
                    proof_fd=schreib_ende,
                )
                plan = sandbox.plan(["/bin/bash", "-c", resolved_command], spec)
                proc = subprocess.Popen(
                    plan.argv,
                    cwd=plan.cwd,
                    env=plan.env,
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=_sandbox_preexec(spec),
                    pass_fds=(schreib_ende,),
                )
                # Closed here so the pipe reaches EOF when the sandbox exits.
                # Held open in the parent, the drain would never finish.
                os.close(schreib_ende)
                schreib_ende = None
                sammler = threading.Thread(
                    target=lambda: beweis_zeilen.extend(_drain_proof(lese_ende)),
                    daemon=True,
                )
                sammler.start()
                lese_ende = None      # the thread owns it now
                gestartet = True
            except SandboxUnavailable as exc:
                exit_code, runner_ok = 126, False
                note = f"Isolation {isolation.value} became unavailable: {exc}"
                isolation_grund = note
                netz = mount = grenzen = NICHT_ANGEWANDT
                angewandt = None
            except OSError as exc:
                # `scratch.mkdir` used to sit outside every try. An ENOSPC or
                # an EEXIST there left `run_check` through the exception
                # instead of through a receipt -- the O14 lesson, reintroduced
                # in the branch that was supposed to be the stricter one.
                exit_code, runner_ok = 127, False
                note = f"Sandboxed process could not be started: {exc}"
                isolation_grund = note
                netz = mount = grenzen = NICHT_ANGEWANDT
                angewandt = None
            finally:
                for fd in (lese_ende, schreib_ende):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:          # pragma: no cover
                            pass
            if gestartet:
                # The same supervisor as the unsandboxed path. The sandboxed
                # branch used to call `subprocess.run(capture_output=True)`,
                # which silently dropped the output ceiling and the streaming
                # the module docstring promises: 64 MiB of check output
                # returned PASS under STRICT and INCONCLUSIVE without it.
                exit_code, runner_ok, truncated, note = _supervise(
                    proc, sink, timeout=timeout, max_output_bytes=max_output_bytes
                )
                grenzen = applied_limits(spec)
                sammler.join(timeout=10)
                from .sandbox import marker_reading

                beweis = (
                    marker_reading("\n".join(beweis_zeilen))
                    if plan.proves_isolation else {}
                )
                eigen_ns = _own_ns()
                angewandt, mount, netz, grund = _isolation_measured(
                    isolation, beweis, spec
                )
                isolation_grund = grund
                if grund:
                    # Requested isolation, and the measurement does not show
                    # it. Not a product verdict under any circumstances: a
                    # check that did not run in the regime it was supposed to
                    # has measured nothing about the product.
                    runner_ok = False
                    if exit_code not in INFRA_EXIT_CODES:
                        exit_code = 126
                    note = f"{grund} (backend {sandbox.name})"
                elif not note:
                    note = (
                        f"executed under {angewandt.value} isolation "
                        f"({sandbox.name}), verified from inside: "
                        f"mnt/net namespaces differ from the runner's, "
                        f"candidate {mount}"
                    )
    elif buffer is not None:
        with buffer as fh:
            try:
                proc = subprocess.Popen(
                    ["/bin/bash", "-c", resolved_command],  # no -l: no login profile
                    cwd=workdir,
                    env=_env(workdir, env),
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=_limits,
                )
            except OSError as exc:
                exit_code, runner_ok = 127, False
                note = f"Process could not be started: {exc}"
            else:
                exit_code, runner_ok, truncated, note = _supervise(
                    proc, sink, timeout=timeout, max_output_bytes=max_output_bytes
                )

    raw = _tail(sink, max_output_bytes)
    # The raw buffer is **kept**. The comment that used to sit here said "no
    # deleting of payload data" and the next line called `sink.unlink()` -- a
    # comment that forbade what it was introducing, the same class as the
    # message that lied about its own content (K8). It mattered materially:
    # above `max_output_bytes` the transcript keeps only the tail, and the
    # beginning was destroyed with the file. Nothing is deleted here now; the
    # file stays next to the arenas, where the retention limit parks whole
    # arenas into `attic/` rather than removing them.

    ended = utcnow()
    elapsed = time.monotonic() - t0
    combined = (
        f"$ {resolved_command}\n"
        f"# cwd={workdir}\n"
        f"# started={started} ended={ended} elapsed={elapsed:.2f}s "
        f"exit={exit_code} runner_ok={runner_ok} truncated={truncated}\n"
        + (f"# {note}\n" if note else "")
        + f"--- output ---\n{raw}\n"
    )

    receipt = Receipt(
        receipt_id=receipt_id,
        run_id=run_id,
        iteration=iteration,
        attempt=attempt,
        check_id=check.check_id,
        candidate_binding=candidate.binding(),
        command=check.command,
        exit_code=exit_code,
        started_at=started,
        ended_at=ended,
        stdout_digest=digest(combined),
        stdout_path=f"logs/{receipt_id}.txt",
        runner_identity=runner_identity(),
        runner_ok=runner_ok,
        truncated=truncated,
        isolation=IsolationRecord(
            requested=isolation.value,
            # A refusal is its own value. "none" would read like a
            # successful unsandboxed run and the requested value like a
            # successful sandboxed one; the command ran neither way, because
            # it never started. `angewandt is None` marks the branches where
            # the launch itself failed.
            effective=(
                ISOLATION_REFUSED
                if (isolation_error or angewandt is None)
                else getattr(angewandt, "value", angewandt)
            ),
            backend=backend_name,
            backend_probe=backend_probe,
            fallback_to_none=(
                isolation is not _Isolation.NONE
                and not isolation_error
                and angewandt is _Isolation.NONE
            ),
            verified_from_inside=bool(beweis),
            complaint=isolation_grund,
            observed_namespaces={
                k: v for k, v in beweis.items() if k.endswith("_ns")
            },
            runner_namespaces=eigen_ns,
            network_policy=netz,
            candidate_mount_mode=mount,
            resource_limit_policy=grenzen,
        ),
    )
    return receipt, combined


def _supervise(
    proc: subprocess.Popen, sink: Path, *, timeout: int, max_output_bytes: int
) -> tuple[int, bool, bool, str]:
    """Watches runtime and output size. Returns (exit, runner_ok, truncated, note)."""
    deadline = time.monotonic() + timeout
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc, True, False, ""

        if time.monotonic() > deadline:
            _terminate(proc)
            return 124, False, False, f"TIMEOUT after {timeout}s; process group killed."

        try:
            if sink.stat().st_size > max_output_bytes:
                _terminate(proc)
                return (
                    126,
                    False,
                    True,
                    f"output limit of {max_output_bytes} bytes exceeded; "
                    "process group killed.",
                )
        except OSError:
            pass

        time.sleep(POLL_INTERVAL)


def _tail(path: Path, limit: int) -> str:
    """Reads at most `limit` bytes from the end -- never the whole file."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                raw = b"(... beginning truncated ...)\n" + fh.read()
            else:
                raw = fh.read()
        return raw.decode("utf-8", "replace")
    except OSError as exc:
        return f"(output not readable: {exc})"


def verify_receipt(
    receipt: Receipt,
    *,
    run_id: str,
    iteration: int,
    attempt: int,
    binding: str,
    known_ids: set[str],
) -> str | None:
    """Checks the **provenance** of a receipt. Returns the rejection reason.

    Whether the evidence also *supports* the claim is a second question and is
    answered in the controller (`_pass_supported`).
    """
    if receipt.receipt_id not in known_ids:
        return FOREIGN_RUNNER.format(receipt_id=receipt.receipt_id)
    if receipt.run_id != run_id:
        return f"Receipt belongs to run {receipt.run_id}, not {run_id}"
    if receipt.iteration != iteration:
        return f"Receipt comes from iteration {receipt.iteration}, not {iteration}"
    if receipt.attempt != attempt:
        return f"Receipt comes from attempt {receipt.attempt}, not {attempt}"
    if receipt.candidate_binding != binding:
        return (
            f"Receipt is bound to candidate '{receipt.candidate_binding}', "
            f"but '{binding}' is being checked"
        )
    if not receipt.runner_ok:
        return "Receipt documents an infrastructure error, not a product verdict"
    return None


def verify_log(receipt: Receipt, log_text: str) -> str | None:
    """Recomputes the digest against the stored log.

    Before, `stdout_digest` was never read back: a manipulated
    `logs/<id>.txt` went unnoticed, even though A05 names "manipulated log"
    explicitly.
    """
    actual = digest(log_text)
    if actual != receipt.stdout_digest:
        return (
            f"Log for {receipt.receipt_id} deviates from the evidence: digest "
            f"{actual}, expected {receipt.stdout_digest}"
        )
    return None
