"""The environment preflight decides whether a run may be started.

V3.3 P3-11. `tools/preflight.py` measures this machine and writes a report whose
`verdict` is READY, NOT_READY or INCONCLUSIVE for a *profile*. Until now nothing
read it: the launcher started a run into an environment that the same tool, run
a minute earlier, had called unfit -- a trust dialog nobody is there to answer,
an interpreter that is not the one HoH was installed into, a disk with no room
for the arenas -- and the quota was spent finding that out.

This module is the consumer side, and deliberately only that. It does not
measure (that stays the tool's: a measurement is not a consequence), it does not
import the tool (`tools/` is not part of the package), and it does not decide
what a profile requires (the report says which verdict it reached for which
profile). It reads one report file and answers one question: **may a run be
started on the strength of this report?** It may when, and only when, the report

* is one regular file with one name that is read whole within a bound and is a
  JSON object (a link, a FIFO, a second name, a file too large, text that is not
  UTF-8 JSON, a key that appears twice: each is a refusal, not an absent
  report),
* was measured for exactly the profile the launcher requires (a `demo` verdict
  says nothing about `unattended`; the profile is what turns measurements into
  a decision),
* says READY, with no blocking and no inconclusive check named (INCONCLUSIVE is
  not READY: a check that could not run has measured nothing), and
* is no older than the age the launcher was configured with, and not from the
  future, and
* says where it was measured, and that place is the one asking (slice 3): the
  interpreter and Python environment of this process, and -- when the caller
  names them -- the repository and the runs directory it starts runs in. A
  report measured under another interpreter, for another repository or for
  another runs directory says nothing about this one.

The one producer (slice 3): for the **default** requirement only, `measure_default` runs
`tools/preflight.py` once, bounded in time, at most once per project and UTC hour, and files
what the tool printed. It still does not measure and does not import the tool.

What this is not. The report is **operator-attested, not authenticated**: the
tool's `digest` is unkeyed and the clock is outside it by design, so a same-user
process that writes a READY report passes. The gate protects against stale,
wrong-profile, unreadable and not-READY reports -- mistakes -- not against
someone who means to override it (who can equally start the launcher without a
requirement). The digest is therefore not verified when a report is read: doing so would claim
an integrity the format cannot carry. (`measure_default` compares it with the content of what
the tool printed, to refuse a garbled pipe -- not to authenticate anything.)

Fail closed. Every refusal is a `PreflightRefused`; no input makes the gate
return normally by accident. Text taken from the report is quoted with
`ascii()` and cut, so a name with a lone surrogate or a megabyte of text cannot
reach a state file or a terminal as it is.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .durable_io import DurableWriteNotSynced, durable_create, settled_stat

#: How `tools/preflight.py` writes `measured_at_utc` (`strftime` of a UTC time).
REPORT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIME_SHAPE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
#: A profile is a plain lower-case name (`demo`, `unattended`).
_PROFILE_SHAPE = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")

#: The most bytes of report that are read. A real report is a few kilobytes; a
#: file above this is not one, and reading it whole would put its size in memory.
MAX_REPORT_BYTES = 1 << 20

#: The most seconds a configured age may be. A report older than a week is not
#: a measurement of this machine as it is: the gate does not let it be
#: configured, whatever the caller asks for.
MAX_AGE_CEILING_SECONDS = 7 * 24 * 3600

#: The verdicts the tool can reach (`tools/preflight.py: verdict`).
VERDICTS = ("READY", "NOT_READY", "INCONCLUSIVE")

#: The report's own statement of where it was measured (`tools/preflight.py`
#: writes it with `environment_identity`; slice 3).
ENVIRONMENT_KEY = "environment"

#: What a project is gated by when it declares nothing (slice 3): the strict
#: profile, a report no older than a day, kept beside the project's other
#: declarations. A project that does not want this says so in a recorded waiver
#: (`launcher.PREFLIGHT_FILE`), it is never silently exempt.
DEFAULT_PROFILE = "unattended"
DEFAULT_MAX_AGE_SECONDS = 24 * 3600
DEFAULT_REPORT_NAME = "preflight-report.json"

#: How a tool that starts runs on its own names its report (slice 3).
REPORT_ENV = "HOH_PREFLIGHT_REPORT"
PROFILE_ENV = "HOH_PREFLIGHT_PROFILE"
MAX_AGE_ENV = "HOH_PREFLIGHT_MAX_AGE"

#: How much of the report's own text a refusal quotes.
_NAMES_SHOWN = 8
_TEXT_SHOWN = 60
_PLACE_SHOWN = 80


class PreflightRefused(RuntimeError):
    """The report does not allow a start. The message says why and quotes the
    report only through `ascii()`.

    `kind` (slice 3) says what a measurement could mend: `absent` (no report), `stale` (older than
    the requirement allows, whatever else it says) or `place` (measured for another interpreter,
    repository or runs directory, or not saying where). Any other refusal is `""`: a verdict, an
    unreadable or odd file -- evidence a new measurement must not silently replace."""

    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


def _shown(value) -> str:
    """A report value as it may appear in a refusal: ASCII only, cut."""
    if isinstance(value, str):
        text = ascii(value)
        return text if len(text) <= _TEXT_SHOWN else text[:_TEXT_SHOWN - 3] + "..."
    return f"<{type(value).__name__}>"


def _where(path) -> str:
    """A path as it may appear in a refusal: quoted, ASCII only (a name that
    came from bytes can carry a lone surrogate that no state file can hold)."""
    return ascii(str(path))


def _names(values: list[str]) -> str:
    shown = ", ".join(_shown(v) for v in values[:_NAMES_SHOWN])
    more = len(values) - _NAMES_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


@dataclass(frozen=True)
class PreflightRequirement:
    """What the launcher requires before it starts a run. No defaults: which
    profile and how old a report may be are decisions of whoever launches.

    `bind_places` (slice 3): whether a launcher also holds the report to the repository and the
    runs directory it starts runs in. True unless a tool says otherwise: a tool whose
    repository and runs directory are created at the moment it runs (a benchmark fixture) cannot
    have measured them beforehand and binds the interpreter only -- it says so by setting it
    False, and nothing else sets it."""

    report: Path
    profile: str
    max_age_seconds: int
    bind_places: bool = True

    def __post_init__(self) -> None:
        report = self.report
        if isinstance(report, (str, os.PathLike)):
            text = os.fspath(report)
        else:
            text = None
        if not isinstance(text, str) or not text or "\0" in text:
            raise ValueError("the preflight report must be a path (a non-empty "
                             "string without NUL)")
        try:
            os.fsencode(text)
        except UnicodeError:
            # A lone surrogate is not a name any file system holds; `os.open`
            # would raise UnicodeEncodeError, which no caller of the gate
            # expects.
            raise ValueError("the preflight report must be a path that names "
                             "a file (it cannot be encoded as a file name)") from None
        # Absolute at construction, without resolving links: the last component
        # is opened `O_NOFOLLOW`, and a launcher that changes directory must
        # not read another file by the same relative name.
        object.__setattr__(self, "report",
                           Path(os.path.abspath(os.path.expanduser(text))))
        if not isinstance(self.profile, str) or not _PROFILE_SHAPE.match(self.profile):
            raise ValueError(f"the preflight profile must be a plain lower-case "
                             f"name, not {_shown(self.profile)}")
        if not isinstance(self.bind_places, bool):
            raise ValueError(f"bind_places must be True or False, not {self.bind_places!r}")
        age = self.max_age_seconds
        if (isinstance(age, bool) or not isinstance(age, int)
                or not 1 <= age <= MAX_AGE_CEILING_SECONDS):
            raise ValueError(
                f"max_age_seconds must be a whole number from 1 to "
                f"{MAX_AGE_CEILING_SECONDS}, not {age!r}")


@dataclass(frozen=True)
class PreflightFinding:
    """What an accepted report said: the profile it was measured for, when, and
    how old it was at the time of the check."""

    profile: str
    measured_at: str
    age_seconds: int


def _place(value, what: str) -> str:
    """A path the identity is taken of: a non-empty name without NUL, resolved
    (`realpath`: two spellings of one place are one place)."""
    text = os.fsdecode(os.fspath(value))
    if not text or "\0" in text:
        raise ValueError(f"the {what} must be a path (a non-empty name without NUL)")
    try:
        os.fsencode(text)
    except UnicodeError:
        raise ValueError(f"the {what} must be a path that names a file (it cannot be "
                         "encoded as a file name)") from None
    return os.path.realpath(text)


def environment_identity(*, repo=None, root=None) -> dict:
    """Where this process runs, as `tools/preflight.py` writes it into a report and
    `check_preflight` compares it: the interpreter (`realpath` of `sys.executable`), the
    Python environment (`realpath` of `sys.prefix`; two virtual environments share a base
    interpreter and differ here), and -- when named -- the repository and the runs directory
    (both `realpath`). An unnamed place is `None`; an interpreter that cannot be named is `""`,
    which no report can say, so it is never the same place."""
    executable = sys.executable
    return {
        "interpreter": os.path.realpath(executable) if isinstance(executable, str) and executable else "",
        "prefix": os.path.realpath(sys.prefix),
        "repo": None if repo is None else _place(repo, "repository"),
        "root": None if root is None else _place(root, "runs directory"),
    }


def shown_place(value) -> str:
    """A path or place as it may appear in a refusal: quoted ASCII, at most 80 characters, cut
    from the **left** -- two places differ at the end (`.../repo-a`, `.../repo-b`), not at the
    start -- so that a reason and the command that follows it stay within the 600 characters the
    launcher and the CLI keep, whatever the length of the paths."""
    text = ascii(os.fspath(value) if isinstance(value, os.PathLike) else value)
    return text if len(text) <= _PLACE_SHOWN else "..." + text[-(_PLACE_SHOWN - 3):]


_PLACES = (("interpreter", "interpreter"), ("prefix", "Python environment"),
           ("repo", "repository"), ("root", "runs directory"))


def _same_place(doc: dict, *, repo, root, identity=None) -> None:
    """Refuse, by name, a report that does not say where it was measured or says another
    place than the one asking. Interpreter and environment are always compared; the
    repository and the runs directory when the caller names them."""
    said = doc.get(ENVIRONMENT_KEY)
    if not isinstance(said, dict):
        raise PreflightRefused(
            "the preflight report does not say where it was measured (it has no "
            f"`{ENVIRONMENT_KEY}` object); measure again with this version of tools/preflight.py",
            "place")
    here = environment_identity(repo=repo, root=root)
    if identity is not None:                  # the interpreter the typed children use (HOH_PYTHON)
        here["interpreter"], here["prefix"] = identity["interpreter"], identity["prefix"]
    for key, label in _PLACES:
        if here[key] is None:
            continue
        value = said.get(key)
        if not isinstance(value, str) or not value:
            raise PreflightRefused(
                f"the preflight report does not name the {label} it was measured for", "place")
        if here[key] == "" or value != here[key]:
            raise PreflightRefused(
                f"the preflight report was measured for another {label}: it says "
                f"{shown_place(value)}, this one is {shown_place(here[key])}", "place")


def default_requirement(project_dir) -> "PreflightRequirement":
    """What a project that declares nothing is gated by: the `unattended` profile, a report
    no older than a day, at `<project>/preflight-report.json`."""
    return PreflightRequirement(report=Path(os.fspath(project_dir)) / DEFAULT_REPORT_NAME,
                                profile=DEFAULT_PROFILE,
                                max_age_seconds=DEFAULT_MAX_AGE_SECONDS)


def requirement_from_environment(environ=None) -> "PreflightRequirement":
    """The requirement a tool that starts runs on its own is configured with:
    `HOH_PREFLIGHT_REPORT` (required: no report, no start), `HOH_PREFLIGHT_PROFILE` (default
    `unattended`) and `HOH_PREFLIGHT_MAX_AGE` (seconds, default one day). Such a tool creates
    its repository and runs directory when it runs, so the requirement binds the interpreter
    only (`bind_places=False`). Any value the requirement refuses is a `PreflightRefused` with
    its words."""
    env = os.environ if environ is None else environ
    report = env.get(REPORT_ENV)
    if not report:
        raise PreflightRefused(
            f"no preflight report is named: set {REPORT_ENV} to the report "
            f"`tools/preflight.py --profile {DEFAULT_PROFILE} --deep --repo REPO --root ROOT "
            "--out FILE` wrote; a run that spends quota is not started without one")
    age = env.get(MAX_AGE_ENV, str(DEFAULT_MAX_AGE_SECONDS))
    try:
        return PreflightRequirement(report=report, profile=env.get(PROFILE_ENV, DEFAULT_PROFILE),
                                    max_age_seconds=int(age), bind_places=False)
    except ValueError as exc:
        raise PreflightRefused(f"the preflight configuration is refused: {exc}") from None


def cli_options(requirement: "PreflightRequirement") -> list[str]:
    """The three options that hand a requirement to a typed `hoh start` / `hoh run`."""
    return ["--preflight-report", str(requirement.report),
            "--preflight-profile", requirement.profile,
            "--preflight-max-age", str(requirement.max_age_seconds)]


#: The interpreter a launcher types its commands with is asked once for who it is.
_ASK_INTERPRETER = ("import os, sys; print(os.path.realpath(sys.executable)); "
                    "print(os.path.realpath(sys.prefix))")
_ASK_SECONDS = 30


def interpreter_identity(python: str) -> dict:
    """`{"interpreter", "prefix"}` of the interpreter at `python`: this process's when it is the
    same one, else the answer of that interpreter itself (`-c`, 30 s). A `PreflightRefused`
    when it cannot be asked: a report cannot be held to an interpreter nobody can name."""
    if os.path.realpath(python) == os.path.realpath(sys.executable or ""):
        here = environment_identity()
        return {"interpreter": here["interpreter"], "prefix": here["prefix"]}
    try:
        done = subprocess.run([python, "-c", _ASK_INTERPRETER], capture_output=True, text=True,
                              timeout=_ASK_SECONDS, check=False)
        lines = done.stdout.splitlines()
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise PreflightRefused(
            f"the interpreter {shown_place(python)} the commands are typed with cannot be "
            f"asked who it is ({type(exc).__name__})", "place") from None
    if done.returncode != 0 or len(lines) != 2 or not all(lines):
        raise PreflightRefused(
            f"the interpreter {shown_place(python)} the commands are typed with did not say "
            f"who it is (exit {done.returncode})", "place")
    return {"interpreter": lines[0], "prefix": lines[1]}


# --------------------------------------------------------------------------- #
# The producer of the default (slice 3)
# --------------------------------------------------------------------------- #
# The default requirement (a day, at `<project>/preflight-report.json`) would halt every
# unattended drive about daily if nothing wrote the report. This is the one producer: it does
# not measure -- the measuring is the tool's -- it runs `tools/preflight.py` once, bounded in
# time, and files what the tool says. At most one attempt per project and UTC hour, recorded
# before it is made.

#: The most seconds the measuring tool is given. `--deep` creates and removes a real worktree
#: and asks the sandbox backend; a minute is usual.
MEASURE_TIMEOUT_SECONDS = 300
ATTEMPT_SCHEMA = "hoh-preflight-attempt/1"
ATTEMPT_PREFIX = "preflight-attempt-"
#: What the producer mends: no report, an old one, one for another place.
MENDABLE = ("absent", "stale", "place")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def tool_path() -> Path | None:
    """`tools/preflight.py` of the checkout this package runs from, or None (an installation
    that ships no tools has no producer, and says so)."""
    candidate = Path(__file__).resolve().parent.parent.parent / "tools" / "preflight.py"
    return candidate if candidate.is_file() else None


def run_tool(argv: list[str], timeout: int):
    """The process boundary of the producer: `(exit code, stdout, stderr)`. Raises
    `subprocess.TimeoutExpired` (the child is killed) or `OSError`."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return done.returncode, done.stdout, done.stderr


def _digest_of(body: dict) -> str:
    """The tool's digest of a report (`tools/preflight.py report`): sha256 of the report without
    its clock and its digest, keys sorted."""
    return hashlib.sha256(json.dumps({k: v for k, v in body.items() if k not in ("measured_at_utc", "digest")},
                                     sort_keys=True).encode()).hexdigest()


def measure_default(requirement: "PreflightRequirement", *, python: str, repo, root,
                    runner=run_tool, now: datetime | None = None) -> str | None:
    """Make one fresh report for the default requirement, or say why not.

    `None`: a report was filed at `requirement.report` (the caller asks it as usual: a NOT_READY
    one is still a halt). A string: the reason, for the halt that names it. The order:
    1. the attempt is recorded, create-only, before anything is run: one per project and UTC
       hour (`preflight-attempt-<YYYYMMDDTHH>Z.json` beside the report); an existing one means
       "already tried this hour" and nothing is run; one that cannot be written means nothing is run;
    2. the tool is run under `python` (the interpreter the typed commands use) with the profile of
       the requirement, `--deep`, the repository and the runs directory, within
       `MEASURE_TIMEOUT_SECONDS`;
    3. its output must be one JSON object for this profile whose `digest` is the digest of its own
       content; otherwise it is not filed;
    4. an old report at the path is renamed to a version (`<name>.v1.<UTC>`, never overwritten and
       never removed), then the new one is created, whole, create-only.
    Never a loop: nothing here calls itself, retries or waits."""
    clock = now if now is not None else _utcnow()
    report = requirement.report
    attempt = report.parent / f"{ATTEMPT_PREFIX}{clock.strftime('%Y%m%dT%H')}Z.json"
    record = {"schema": ATTEMPT_SCHEMA, "started_at": clock.strftime(REPORT_TIME_FORMAT), "pid": os.getpid(),
              "interpreter": python, "profile": requirement.profile, "timeout_seconds": MEASURE_TIMEOUT_SECONDS,
              "repo": str(repo), "root": str(root)}
    try:
        durable_create(attempt, json.dumps(record, indent=2) + "\n", mode=None)
    except FileExistsError:
        return (f"a measurement was already attempted in this UTC hour ({attempt.name}); the next is allowed "
                "in the next hour")
    except DurableWriteNotSynced:
        pass                                  # in place and whole; only its directory entry is unsure
    except OSError as exc:
        return f"the measurement attempt cannot be recorded ({type(exc).__name__}); nothing is measured"
    tool = tool_path()
    if tool is None:
        return ("this installation has no tools/preflight.py to measure with; write the report with "
                f"`tools/preflight.py --profile {requirement.profile} --deep --repo REPO --root ROOT --out FILE`")
    argv = [python, str(tool), "--profile", requirement.profile, "--deep", "--repo", str(repo),
            "--root", str(root), "--json"]
    try:
        code, out, err = runner(argv, MEASURE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return f"the measurement did not finish within {MEASURE_TIMEOUT_SECONDS} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the measurement could not be started ({type(exc).__name__})"
    # the tool's exit code is its verdict (0 READY, 1 NOT_READY, 3 INCONCLUSIVE); anything else is
    # a failure of the tool itself
    if code not in (0, 1, 3):
        return f"the measurement failed (exit {code}): {_shown(' '.join((err or '').split()[-12:]))}"
    try:
        body = json.loads(out, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError, TypeError):
        return "the measurement did not print one JSON report"
    if not isinstance(body, dict) or body.get("profile") != requirement.profile:
        return "the measurement is not a report for the required profile"
    if body.get("digest") != _digest_of(body):
        return "the measurement's digest is not the digest of its content; it is not filed"
    if report.exists() or os.path.islink(report):
        old = report.with_name(f"{report.name}.v1.{clock.strftime('%Y%m%dT%H%M%S')}Z")
        try:
            if os.path.lexists(old):
                raise FileExistsError(errno.EEXIST, "the version name is taken")
            os.rename(report, old)
        except OSError as exc:
            return f"the old report cannot be put aside ({type(exc).__name__}); the new one is not filed"
    try:
        durable_create(report, json.dumps(body, indent=2) + "\n", mode=None)
    except FileExistsError:
        pass                                  # another drive filed one first: it is asked as usual
    except DurableWriteNotSynced:
        pass
    except OSError as exc:
        return f"the new report cannot be filed ({type(exc).__name__})"
    return None


def _read_report(path: Path) -> bytes:
    """The bytes of the report: one regular file with one name, read within the
    bound. Opened `O_NOFOLLOW | O_NONBLOCK` (a link is refused, a FIFO cannot
    hang the open) and judged on the descriptor, not on the name."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PreflightRefused(
                f"the preflight report {_where(path)} is a symbolic link") from exc
        if exc.errno == errno.ENOENT:
            raise PreflightRefused(
                f"there is no preflight report at {_where(path)}", "absent") from exc
        raise PreflightRefused(
            f"the preflight report {_where(path)} cannot be opened: {exc.strerror}") from exc
    try:
        st = settled_stat(fd)           # a creator may be between its link and its drop
        if not stat.S_ISREG(st.st_mode):
            raise PreflightRefused(
                f"the preflight report {_where(path)} is not a regular file")
        if st.st_nlink != 1:
            raise PreflightRefused(
                f"the preflight report {_where(path)} has {st.st_nlink} names, not one")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            try:
                raw = fh.read(MAX_REPORT_BYTES + 1)
            except OSError as exc:
                raise PreflightRefused(
                    f"the preflight report {_where(path)} cannot be read: {exc.strerror}"
                ) from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_REPORT_BYTES:
        raise PreflightRefused(
            f"the preflight report {_where(path)} is larger than {MAX_REPORT_BYTES} bytes")
    return raw


def _no_duplicate_keys(pairs):
    """`json` keeps the last of two equal keys; a report that says READY after
    it said NOT_READY is not one report."""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("a key appears twice")
        seen[key] = value
    return seen


def _names_of(doc: dict, key: str) -> list[str]:
    value = doc.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PreflightRefused(f"the preflight report's `{key}` is not a list of names")
    return value


def _too_old(doc: dict, requirement: PreflightRequirement, clock: datetime) -> bool:
    """Whether the report's own time is already older than the requirement allows (a verdict
    refusal of such a report is `stale` too: a new measurement is what it needs). A time that
    cannot be read is not old."""
    measured = doc.get("measured_at_utc")
    if not isinstance(measured, str) or not _TIME_SHAPE.match(measured):
        return False
    try:
        when = datetime.strptime(measured, REPORT_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return clock - when > timedelta(seconds=requirement.max_age_seconds)


def check_preflight(requirement: PreflightRequirement, *,
                    now: datetime | None = None, repo=None,
                    root=None, identity: dict | None = None) -> PreflightFinding:
    """Raises `PreflightRefused` unless the report allows a start (see the
    module text); returns what the accepted report said.

    `repo` and `root` are the repository and the runs directory the caller starts runs in;
    given, the report must have been measured for exactly those (slice 3). The interpreter and
    Python environment are always compared: this process's, or -- `identity`, from
    `interpreter_identity` -- those of the interpreter the caller types its commands with.

    `now` is for tests; it must be timezone-aware.
    """
    clock = now if now is not None else datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    path = requirement.report
    raw = _read_report(path)
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PreflightRefused(
            f"the preflight report {_where(path)} is not UTF-8 JSON "
            f"({type(exc).__name__})") from exc
    if not isinstance(doc, dict):
        raise PreflightRefused(f"the preflight report {_where(path)} is not a JSON object")

    profile = doc.get("profile")
    if not isinstance(profile, str) or profile != requirement.profile:
        raise PreflightRefused(
            f"the preflight report was measured for profile {_shown(profile)}, "
            f"the launcher requires {requirement.profile!r}")

    verdict = doc.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise PreflightRefused(
            f"the preflight report's verdict is {_shown(verdict)}, not one of "
            f"{', '.join(VERDICTS)}")
    blocking = _names_of(doc, "blocking")
    inconclusive = _names_of(doc, "inconclusive")
    if verdict != "READY":
        named = blocking if verdict == "NOT_READY" else inconclusive
        raise PreflightRefused(
            f"the {requirement.profile!r} preflight says {verdict}"
            + (f": {_names(named)}" if named else ""),
            "stale" if _too_old(doc, requirement, clock) else "")
    if blocking or inconclusive:
        raise PreflightRefused(
            f"the preflight report says READY and also names checks "
            f"({_names(blocking + inconclusive)}); it contradicts itself")

    measured = doc.get("measured_at_utc")
    if not isinstance(measured, str) or not _TIME_SHAPE.match(measured):
        raise PreflightRefused(
            f"the preflight report's `measured_at_utc` is {_shown(measured)}, "
            f"not a UTC time of the form {REPORT_TIME_FORMAT}")
    try:
        when = datetime.strptime(measured, REPORT_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise PreflightRefused(
            f"the preflight report's `measured_at_utc` {_shown(measured)} is not "
            f"a date") from exc
    elapsed = clock - when
    if elapsed < timedelta(0):
        raise PreflightRefused(
            f"the preflight report was measured in the future ({measured}); "
            "its clock cannot be trusted")
    if elapsed > timedelta(seconds=requirement.max_age_seconds):
        raise PreflightRefused(
            f"the preflight report is {int(elapsed.total_seconds())} s old "
            f"(measured {measured}); the launcher accepts at most "
            f"{requirement.max_age_seconds} s", "stale")
    _same_place(doc, repo=repo, root=root, identity=identity)
    return PreflightFinding(profile=profile, measured_at=measured,
                            age_seconds=int(elapsed.total_seconds()))
