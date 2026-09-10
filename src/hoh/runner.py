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

import os
import platform
import re
import resource
import signal
import socket
import subprocess
import time
from pathlib import Path

from .contracts import AcceptanceCheck, Candidate, Receipt, digest, utcnow

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


def _limits() -> None:
    """Runs in the child process between fork and exec.

    No `RLIMIT_NPROC` -- see the module docstring.
    """
    for res, limit in (
        (resource.RLIMIT_CPU, RLIMIT_CPU_SECONDS),
        (resource.RLIMIT_AS, RLIMIT_ADDRESS_SPACE),
        (resource.RLIMIT_FSIZE, RLIMIT_FILE_SIZE),
    ):
        try:
            _, hard = resource.getrlimit(res)
            soft = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            continue
    os.setsid()  # own process group, so that a timeout reaches the whole family


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
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Falling back to the old behaviour is worse than a sibling and better
        # than failing the run: a check that cannot start produces no verdict
        # at all, which is the one outcome this project treats as the most
        # expensive. The pollution is visible; a missing verdict is not.
        return workdir
    return scratch


def _env(workdir: Path, extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    scratch = _scratch_dir(workdir)
    env["HOME"] = str(scratch)
    env["TMPDIR"] = str(scratch)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
) -> tuple[Receipt, str]:
    """Runs a check and returns (Receipt, transcript text).

    Output is **streamed**: stdout and stderr go straight into a file whose
    size is watched. An earlier version used `communicate()` and truncated
    only afterwards -- so the controller went on buffering the full output in
    memory, measured at 430 MB for 150 MB of check output.

    A timeout, exceeding the output limit or a process that cannot be started
    is an **infrastructure error**: `runner_ok=False`, and `Receipt.outcome()`
    then returns `INCONCLUSIVE` -- independently of `expect_exit`.
    """
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
    buffer: object | None = None
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        sink.parent.mkdir(parents=True, exist_ok=True)
        buffer = open(sink, "wb")
    except OSError as exc:
        exit_code, runner_ok = 127, False
        note = f"Output buffer could not be created: {exc}"
    if buffer is not None:
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
