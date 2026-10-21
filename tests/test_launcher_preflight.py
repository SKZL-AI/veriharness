"""P3-11, first increment: the launcher refuses to start a run on the verdict of
the environment preflight.

`tools/preflight.py` measures and reports; `hoh.preflightgate` reads one report
and `HohRunLauncher.prepare` asks it before it touches anything. Every test
names what it catches. Nothing here starts Herdr or an agent, runs a real
measurement, or reads outside `tmp_path`: the reports are built with the tool's
own `report()` over synthetic checks (so the gate is tied to the producer's real
field names, not to a hand-written fixture) and the launcher's `_run_cli` is
replaced by a recorder.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import os
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import BudgetExhausted
from hoh.preflightgate import (
    MAX_AGE_CEILING_SECONDS, MAX_REPORT_BYTES, REPORT_TIME_FORMAT,
    PreflightRefused, PreflightRequirement, check_preflight, environment_identity,
)
from hoh.project import TaskNode
from hoh.reservation import LEDGER_NAME, LOCK_NAME

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("preflight_gate_probe", ROOT / "tools" / "preflight.py")
pf = importlib.util.module_from_spec(_spec)
# Registered before execution, like tests/test_preflight.py: a `@dataclass`
# resolves its annotations through `sys.modules[cls.__module__]`.
sys.modules["preflight_gate_probe"] = pf
_spec.loader.exec_module(pf)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
FRESH = NOW.strftime(REPORT_TIME_FORMAT)


# ==== reports, built by the producer ===========================================

def _checks(profile: str, level: str | None = None, **states: str) -> list:
    """Every required check passing and an isolation level that suffices (or
    `level`), with the named checks set to the given state (as
    `tests/test_preflight.py`)."""
    out = [pf.Check(c, "q", states.get(c, pf.PASS), "d", {})
           for c in pf.PROFILES[profile]["requires"]]
    out.append(pf.Check("isolation", "q", pf.PASS, "d",
                        {"level": level or pf.PROFILES[profile]["min_isolation"]}))
    return out


def _report(profile: str = "unattended", *, at: datetime | None = None,
            level: str | None = None, repo=None, root=None, **states: str) -> dict:
    body = pf.report(_checks(profile, level, **states), profile, deep=True, repo=repo, root=root)
    body["measured_at_utc"] = (at or NOW).strftime(REPORT_TIME_FORMAT)
    return body


def _put(tmp_path: Path, body, name: str = "report.json") -> Path:
    path = tmp_path / name
    # Exactly what `tools/preflight.py main` writes for `--out`.
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def _requirement(path: Path, profile: str = "unattended", age: int = 3600):
    return PreflightRequirement(path, profile, age)


def _refused(path: Path, match: str, **kw) -> None:
    with pytest.raises(PreflightRefused, match=match):
        check_preflight(_requirement(path, **kw), now=NOW)


# ==== the gate: what a report must say =========================================

def test_a_ready_report_of_the_producer_for_the_required_profile_is_accepted(tmp_path):
    """The positive control, from the tool's own `report()`: without it every
    refusal below would also pass against a gate that always refuses."""
    path = _put(tmp_path, _report(at=NOW - timedelta(seconds=90)))
    found = check_preflight(_requirement(path), now=NOW)
    assert (found.profile, found.age_seconds) == ("unattended", 90)
    assert found.measured_at == (NOW - timedelta(seconds=90)).strftime(REPORT_TIME_FORMAT)


def test_a_failed_required_check_is_refused_and_named(tmp_path):
    """Catches: the verdict not read. The tool says NOT_READY and names
    `trust_readiness`; the refusal says both."""
    body = _report(trust_readiness=pf.FAIL)
    assert body["verdict"] == "NOT_READY" and body["blocking"] == ["trust_readiness"]
    _refused(_put(tmp_path, body), r"says NOT_READY: 'trust_readiness'")


def test_an_inconclusive_report_is_not_ready(tmp_path):
    """Catches: INCONCLUSIVE rounded to READY (`!= "NOT_READY"`). A check that
    could not run has measured nothing; `worktree_creation` without `--deep` is
    the everyday case."""
    body = _report(worktree_creation=pf.INCONCLUSIVE)
    assert body["verdict"] == "INCONCLUSIVE"
    _refused(_put(tmp_path, body), r"says INCONCLUSIVE: 'worktree_creation'")


def test_a_report_for_another_profile_is_refused_in_both_directions(tmp_path):
    """Catches: the profile not compared, or compared as a prefix, or a
    stronger profile taken to cover a weaker one. A `demo` READY says nothing
    about `unattended`."""
    demo = _put(tmp_path, _report("demo"), "demo.json")
    unattended = _put(tmp_path, _report("unattended"), "unattended.json")
    _refused(demo, r"measured for profile 'demo', the launcher requires 'unattended'")
    _refused(unattended, r"measured for profile 'unattended', the launcher requires 'demo'",
             profile="demo")
    assert check_preflight(_requirement(demo, "demo"), now=NOW).profile == "demo"


@pytest.mark.parametrize("verdict", ["READY ", " READY", "ready", "Ready", "READY\n", "OK",
                                     "", True, 1, None, ["READY"], {"READY": 1}],
                         ids=lambda v: repr(v))
def test_only_the_exact_word_ready_is_a_verdict(tmp_path, verdict):
    """Catches: a case, whitespace or truthiness variant of the matched literal."""
    body = _report()
    body["verdict"] = verdict
    _refused(_put(tmp_path, body), r"verdict is")


@pytest.mark.parametrize("name", ["READY ", "unattended ", "Unattended", "unattended\n", "demo"])
def test_a_profile_is_matched_exactly(tmp_path, name):
    """Catches: `strip()`, `lower()` or `in` on the profile."""
    body = _report()
    body["profile"] = name
    _refused(_put(tmp_path, body), r"measured for profile")


@pytest.mark.parametrize("key,value", [
    ("blocking", ["trust_readiness"]),
    ("inconclusive", ["worktree_creation"]),
], ids=["blocking", "inconclusive"])
def test_a_ready_report_that_names_checks_contradicts_itself(tmp_path, key, value):
    """Catches: `verdict` trusted alone. A hand-edited verdict line over a
    report that still names what blocked is refused."""
    body = _report()
    body[key] = value
    _refused(_put(tmp_path, body), r"contradicts itself")


@pytest.mark.parametrize("key,value", [
    ("blocking", None), ("blocking", "trust_readiness"), ("blocking", [1]),
    ("blocking", [["x"]]), ("inconclusive", None), ("inconclusive", {"a": 1}),
    ("inconclusive", [None]),
], ids=lambda v: repr(v)[:24])
def test_names_that_are_not_a_list_of_strings_are_refused(tmp_path, key, value):
    body = _report()
    body[key] = value
    _refused(_put(tmp_path, body), r"is not a list of names")


def test_a_missing_key_is_refused_not_defaulted(tmp_path):
    """Catches: `.get(..., [])` standing in for a report that does not say."""
    for key in ("profile", "verdict", "blocking", "inconclusive", "measured_at_utc"):
        body = _report()
        del body[key]
        with pytest.raises(PreflightRefused):
            check_preflight(_requirement(_put(tmp_path, body, f"no-{key}.json")), now=NOW)


# ==== the gate: age ============================================================

def test_the_age_bound_is_exact(tmp_path):
    """Catches: `>=` for `>` (an age of exactly the bound is accepted), a bound
    that is a constant, and `max_age_seconds` not forwarded. 3600 s old at a
    bound of 3600 passes; 3601 does not; 60 s at a bound of 59 does not."""
    at = lambda s: _put(tmp_path, _report(at=NOW - timedelta(seconds=s)), f"a{s}.json")  # noqa: E731
    assert check_preflight(_requirement(at(3600), age=3600), now=NOW).age_seconds == 3600
    _refused(at(3601), r"3601 s old.*at most 3600 s", age=3600)
    _refused(at(60), r"60 s old.*at most 59 s", age=59)
    assert check_preflight(_requirement(at(60), age=60), now=NOW).age_seconds == 60


def test_a_clock_without_a_timezone_is_a_programming_error_not_a_pass(tmp_path):
    """Catches: a naive `now` compared against a UTC time (a TypeError that
    some caller turns into 'could not check, go on')."""
    path = _put(tmp_path, _report())
    with pytest.raises(ValueError, match="timezone-aware"):
        check_preflight(_requirement(path), now=datetime(2026, 10, 12, 12, 0, 0))


def test_a_report_from_the_future_is_refused(tmp_path):
    """Catches: a negative age read as fresh (`age <= max` alone)."""
    path = _put(tmp_path, _report(at=NOW + timedelta(seconds=1)))
    _refused(path, r"measured in the future")


@pytest.mark.parametrize("stamp", [
    "2026-10-12T12:00:00", "2026-10-12T12:00:00+00:00", "2026-10-12 12:00:00Z",
    "2026-10-12T12:00:00z", "2026-10-12T12:00:00Z\n", " 2026-10-12T12:00:00Z",
    "2026-13-12T12:00:00Z", "2026-02-30T12:00:00Z", "2026-10-12T25:00:00Z",
    "\u0662\u0660\u0662\u0666-10-12T12:00:00Z",
    "", "now", 1760270400, None, True, ["2026-10-12T12:00:00Z"],
], ids=lambda v: repr(v)[:30])
def test_a_time_that_is_not_the_producers_form_is_refused(tmp_path, stamp):
    """Catches: a lenient parse (`fromisoformat`, a timezone offset, a trailing
    newline through `$`, Arabic-Indic digits through `\\d`), and a number or a
    bool read as a time."""
    body = _report()
    body["measured_at_utc"] = stamp
    _refused(_put(tmp_path, body), r"measured_at_utc")


# ==== the gate: the file =======================================================

def test_a_report_that_is_not_there_is_a_refusal_not_an_absent_requirement(tmp_path):
    """Catches: a missing file read as 'nothing to check'."""
    _refused(tmp_path / "nothing.json", r"there is no preflight report at")


def _make_directory(p: Path) -> None:
    p.mkdir()


def _make_fifo(p: Path) -> None:
    os.mkfifo(p)


def _make_dangling(p: Path) -> None:
    p.symlink_to(p.parent / "not-there.json")


def _make_link_to_good(p: Path) -> None:
    good = _put(p.parent, _report(at=NOW), "good.json")
    p.symlink_to(good)


def _make_second_name(p: Path) -> None:
    good = _put(p.parent, _report(at=NOW), "good.json")
    os.link(good, p)


@pytest.mark.parametrize("make,match", [
    (_make_directory, "not a regular file"),
    (_make_dangling, "symbolic link"), (_make_link_to_good, "symbolic link"),
    (_make_second_name, "2 names, not one"),
], ids=["directory", "dangling-link", "link-to-a-good-report", "second-name"])
def test_a_report_that_is_not_one_regular_file_is_refused_without_hanging(tmp_path, make, match):
    """Catches: a plain `read_text` (a link is followed, a directory is an
    IsADirectoryError that escapes). Each of these stands where a good report
    would, and the good one is accepted in the same test. The FIFO has a test of
    its own that cannot hang."""
    path = tmp_path / "report.json"
    make(path)
    _refused(path, match)
    if make is _make_second_name:
        os.unlink(tmp_path / "good.json")      # a second name is gone: one name again
        assert check_preflight(_requirement(path), now=NOW).profile == "unattended"


def test_a_fifo_where_the_report_should_be_is_refused_without_blocking(tmp_path):
    """Catches: `O_NONBLOCK` dropped from the open. A FIFO opened for reading
    waits for a writer, so the gate runs in a thread and, if it has not
    answered in five seconds, a writer is opened to release it and the test
    fails (instead of the run waiting for its own timeout)."""
    path = tmp_path / "report.json"
    os.mkfifo(path)
    outcome: dict = {}

    def ask() -> None:
        try:
            check_preflight(_requirement(path), now=NOW)
            outcome["raised"] = None
        except BaseException as exc:                      # noqa: BLE001 - recorded
            outcome["raised"] = exc

    gate = threading.Thread(target=ask, daemon=True)
    gate.start()
    gate.join(5)
    if gate.is_alive():
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))   # release the open
        except OSError:
            pass
        gate.join(5)
        pytest.fail("the gate blocked on a FIFO (the open must not wait for a writer)")
    assert isinstance(outcome["raised"], PreflightRefused)
    assert "not a regular file" in str(outcome["raised"])


def test_a_path_that_cannot_be_opened_is_a_refusal_not_an_oserror(tmp_path):
    """Catches: an OSError other than ENOENT/ELOOP escaping the gate (a name
    too long, a file where a directory is due)."""
    _refused(tmp_path / ("n" * 300), r"cannot be opened")
    (tmp_path / "plain").write_text("x", encoding="utf-8")
    _refused(tmp_path / "plain" / "report.json", r"cannot be opened")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_a_report_without_read_permission_is_refused(tmp_path):
    """Catches: PermissionError read as an absent report."""
    path = _put(tmp_path, _report())
    path.chmod(0)
    try:
        _refused(path, r"cannot be opened")
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_the_report_is_read_up_to_its_bound_and_no_further(tmp_path):
    """Catches: no bound, or an off-by-one. A valid report padded to exactly
    MAX_REPORT_BYTES is accepted; one byte more is refused before it is parsed."""
    body = _report()
    pad = 0
    while True:
        raw = json.dumps({**body, "pad": "x" * pad}).encode()
        if len(raw) >= MAX_REPORT_BYTES:
            break
        pad += MAX_REPORT_BYTES - len(raw)
    assert len(raw) == MAX_REPORT_BYTES
    path = tmp_path / "report.json"
    path.write_bytes(raw)
    assert check_preflight(_requirement(path), now=NOW).profile == "unattended"
    path.write_bytes(raw + b" ")
    _refused(path, r"larger than 1048576 bytes")
    assert MAX_REPORT_BYTES == 1 << 20


_GOOD = json.dumps({"profile": "unattended", "verdict": "READY", "blocking": [],
                    "inconclusive": [], "measured_at_utc": FRESH,
                    "environment": environment_identity()})


@pytest.mark.parametrize("raw", [
    b"", b"   \n", b"\xef\xbb\xbf" + _GOOD.encode(), _GOOD.encode("utf-16"),
    _GOOD.encode("latin-1").replace(b"READY", b"R\xc9ADY"), _GOOD.encode() + b"\0",
    _GOOD.replace("READY", "RE\0ADY").encode(), b"\xff\xfe\x00\x00", b"not json",
    b"[1, 2]", b'"READY"', b"123", b"null", b"true",
    _GOOD.replace('"verdict": "READY"', '"verdict": "NOT_READY", "verdict": "READY"').encode(),
    b"[" * 200000, b'{"a": ' * 100000,
    _GOOD.replace('"profile": "unattended"', '"profile": "unattended", "profile": "unattended"').encode(),
    _GOOD.encode() + b" {}",
], ids=["empty", "blank", "bom", "utf16", "latin1", "trailing-nul", "nul-in-text",
        "utf32-bom", "not-json", "list", "string", "number", "null", "bool",
        "duplicate-verdict", "nested-lists", "nested-objects", "duplicate-profile",
        "two-documents"])
def test_content_that_is_not_one_utf8_json_object_is_refused(tmp_path, raw):
    """Catches: any input that makes the gate return, or raise something other
    than PreflightRefused (a RecursionError, a UnicodeDecodeError, a JSON key
    that is read twice and the last wins). The same text with nothing wrong
    with it is the positive control (`_GOOD`)."""
    path = tmp_path / "report.json"
    path.write_bytes(raw)
    with pytest.raises(PreflightRefused):
        check_preflight(_requirement(path), now=NOW)
    path.write_text(_GOOD, encoding="utf-8")
    assert check_preflight(_requirement(path), now=NOW).profile == "unattended"


def test_text_is_decoded_strictly_even_where_the_report_is_otherwise_good(tmp_path):
    """Catches: `decode("utf-8", "replace")` or `errors="ignore"`. A Latin-1
    byte in a field the gate does not read must still make the file not UTF-8:
    the rows above corrupt the verdict word, which is refused for another
    reason. The same text as UTF-8 is accepted in the same test."""
    text = json.dumps({**_report(at=NOW), "operator": "caf\u00e9"}, ensure_ascii=False)
    path = tmp_path / "report.json"
    path.write_bytes(text.encode("latin-1"))
    assert b"\xe9" in path.read_bytes()
    _refused(path, r"is not UTF-8 JSON")
    path.write_bytes(text.encode("utf-8"))
    assert check_preflight(_requirement(path), now=NOW).profile == "unattended"


def test_text_from_the_report_is_quoted_ascii_and_cut(tmp_path):
    """Catches: a name with a lone surrogate or a very long text reaching a
    refusal as it is (the reason is persisted in the project state, which
    cannot hold a lone surrogate, and printed on a terminal)."""
    body = _report()
    body["verdict"] = "NOT_READY"
    body["blocking"] = ["\ud800evil", "x" * 5000] + [f"c{i}" for i in range(12)]
    with pytest.raises(PreflightRefused) as err:
        check_preflight(_requirement(_put(tmp_path, body, "s.json")), now=NOW)
    text = str(err.value)
    text.encode("utf-8")                                     # no lone surrogate
    assert text.isascii() and len(text) < 600, len(text)
    assert "\\ud800evil" in text and "and 6 more" in text and "x" * 100 not in text
    assert "'" + "x" * 56 + "..." in text                    # cut at 60, quoted
    body["profile"] = "\ud83d"
    with pytest.raises(PreflightRefused) as err:
        check_preflight(_requirement(_put(tmp_path, body, "p.json")), now=NOW)
    str(err.value).encode("utf-8")


def test_a_path_is_quoted_ascii_in_every_reason(tmp_path, monkeypatch):
    """Catches: the path interpolated as it is, in the gate's reasons and in the
    launcher's hint (a name that came from bytes must not reach the project
    state or a terminal raw). The file need not exist: the reason names it."""
    path = tmp_path / "rapport-\u00e9.json"
    with pytest.raises(PreflightRefused) as err:
        check_preflight(_requirement(path), now=NOW)
    assert str(err.value).isascii() and "\\xe9" in str(err.value)
    launcher, calls = _launcher(tmp_path, monkeypatch,
                                PreflightRequirement(path, "unattended", 60))
    why = launcher.prepare(_node())
    assert why.isascii() and why.count("\\xe9") == 2 and calls == []


# ==== the requirement ==========================================================

@pytest.mark.parametrize("report", ["", "a\0b", 7, None, b"report.json", ["r.json"],
                                    "/x/\ud800.json"],
                         ids=["empty", "nul", "int", "none", "bytes", "list", "lone-surrogate"])
def test_a_report_path_that_is_not_a_path_is_refused_at_construction(report):
    """Catches: a name that no file system holds reaching `os.open`, whose
    UnicodeEncodeError is neither a refusal nor expected by any caller."""
    with pytest.raises(ValueError, match="must be a path"):
        PreflightRequirement(report, "unattended", 60)


@pytest.mark.parametrize("profile", ["", "Unattended", "unattended ", "unattended\n",
                                     "un attended", "1demo", "x" * 33, None, 3, b"demo"],
                         ids=lambda v: repr(v)[:20])
def test_a_profile_that_is_not_a_plain_name_is_refused_at_construction(profile):
    with pytest.raises(ValueError, match="profile"):
        PreflightRequirement("r.json", profile, 60)


@pytest.mark.parametrize("age", [0, -1, True, False, 1.5, "60", None,
                                 MAX_AGE_CEILING_SECONDS + 1, 10**18],
                         ids=lambda v: repr(v)[:20])
def test_an_age_that_is_not_a_whole_number_within_its_ceiling_is_refused(age):
    """Catches: a bool as 1, a float, a ceiling that is not enforced."""
    with pytest.raises(ValueError, match="max_age_seconds"):
        PreflightRequirement("r.json", "unattended", age)


def test_the_age_ceiling_is_a_week_and_the_extremes_are_accepted():
    assert MAX_AGE_CEILING_SECONDS == 7 * 24 * 3600
    assert PreflightRequirement("r.json", "unattended", 1).max_age_seconds == 1
    assert PreflightRequirement("r.json", "x" * 32, MAX_AGE_CEILING_SECONDS)


def test_a_relative_path_is_made_absolute_when_it_is_given(tmp_path, monkeypatch):
    """Catches: a requirement that reads another file after the launcher's
    working directory changed. Links are not resolved (the last component is
    opened without following one)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _put(tmp_path / "a", _report(at=NOW), "report.json")
    monkeypatch.chdir(tmp_path / "a")
    req = PreflightRequirement("report.json", "unattended", 60)
    monkeypatch.chdir(tmp_path / "b")
    assert req.report == tmp_path / "a" / "report.json"
    assert check_preflight(req, now=NOW).profile == "unattended"


def test_a_home_relative_path_is_expanded(tmp_path, monkeypatch):
    """Catches: `~` read as a directory named `~` under the working directory."""
    home = tmp_path / "home"
    home.mkdir()
    _put(home, _report(at=NOW), "report.json")
    monkeypatch.setenv("HOME", str(home))
    req = PreflightRequirement("~/report.json", "unattended", 60)
    assert req.report == home / "report.json"
    assert check_preflight(req, now=NOW).profile == "unattended"


def test_a_requirement_cannot_be_changed_after_it_was_checked():
    """Catches: a mutable requirement (the validated age or profile replaced
    after construction, past every check made in `__post_init__`)."""
    req = PreflightRequirement("r.json", "unattended", 60)
    for field, value in (("max_age_seconds", 10**9), ("profile", "demo"),
                         ("report", Path("/nowhere"))):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(req, field, value)


# ==== the launcher =============================================================

class _Grants:
    name = "test"

    def __init__(self):
        self.asked = 0

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        self.asked += 1
        return Approval(granted=True, worktree=worktree, provider=self.name)


def _launcher(tmp_path, monkeypatch, requirement=None, **kw):
    """A launcher whose CLI is a recorder; `calls` holds every argv."""
    wt = tmp_path / "worktree-home"
    wt.mkdir(exist_ok=True)
    grants = _Grants()
    launcher = HohRunLauncher(tmp_path / "root", tmp_path / "repo", approvals=grants,
                              preflight=requirement, **kw)
    calls: list[list[str]] = []

    def run_cli(argv):
        calls.append(list(argv))
        if "worktree" in argv:
            return Dispatch(0, json.dumps({"path": str(wt)}), "")
        if "start" in argv:
            return Dispatch(0, "", "")
        raise AssertionError(f"unexpected cli call {argv}")

    monkeypatch.setattr(launcher, "_run_cli", run_cli)
    launcher.grants = grants
    return launcher, calls


def _node(run_id: str = "n1") -> TaskNode:
    return TaskNode(id=run_id, repair_of="gate-x", run_id=run_id)


def _fresh_requirement(tmp_path, body=None, age: int = 3600) -> PreflightRequirement:
    # The launcher reads the real clock: a report measured now is fresh. It is measured for the
    # repository and runs directory `_launcher` builds the launcher on (P3-11 slice 3).
    body = body if body is not None else _report(at=datetime.now(UTC), repo=tmp_path / "repo",
                                                 root=tmp_path / "root")
    return PreflightRequirement(_put(tmp_path, body), "unattended", age)


def test_a_launcher_without_a_requirement_never_reads_a_report(tmp_path, monkeypatch):
    """Catches: the gate on by default. `preflight=None` is what every launcher
    was, and `check_preflight` is not reached at all."""
    import hoh.launcher as launcher_module
    monkeypatch.setattr(launcher_module, "check_preflight",
                        lambda *a, **k: pytest.fail("the gate was asked"))
    launcher, calls = _launcher(tmp_path, monkeypatch)
    assert launcher.preflight is None
    assert launcher.prepare(_node()) is None
    assert [c for c in calls if "start" in c]


def test_a_start_is_allowed_by_a_ready_fresh_report(tmp_path, monkeypatch):
    """The positive control at the launcher: the same flow as above, with a
    requirement, starts the run."""
    launcher, calls = _launcher(tmp_path, monkeypatch, _fresh_requirement(tmp_path))
    node = _node()
    assert launcher.prepare(node) is None
    assert len([c for c in calls if "start" in c]) == 1 and node.spec_path


def test_a_start_is_refused_on_a_verdict_and_nothing_is_created(tmp_path, monkeypatch):
    """Catches: the refusal after the specification, the worktree or the trust
    approval, a refusal that still starts, and a reason that does not name the
    check. The reason comes back as a returned string, the form the
    orchestrator turns into a BLOCKED node and a named halt."""
    req = _fresh_requirement(tmp_path, _report(at=datetime.now(UTC), trust_readiness=pf.FAIL))
    launcher, calls = _launcher(tmp_path, monkeypatch, req)
    node = _node()
    why = launcher.prepare(node)
    assert isinstance(why, str)
    assert "does not allow a start" in why and "says NOT_READY: 'trust_readiness'" in why
    assert "--profile unattended --out" in why
    assert calls == [] and launcher.grants.asked == 0
    assert (node.branch, node.spec_path) == (None, None)
    assert not (tmp_path / "root").exists()


def test_a_refusal_is_before_the_hold_and_leaves_the_budget_whole(tmp_path, monkeypatch):
    """Catches (P3-04's named hazard): a refusal between the hold and the start,
    which would leave a HELD row for a run that never began. With a budget of 9
    and another run's hold of 4, the refused start writes nothing: the ledger is
    byte for byte what it was, and a launcher without a requirement is then
    granted the 5 that are left."""
    plain, plain_calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9,
                                   dispatch_reservation=4)
    assert plain.prepare(_node("other")) is None
    ledger = tmp_path / "root" / LEDGER_NAME
    before = ledger.read_bytes()
    names = sorted(p.name for p in (tmp_path / "root").iterdir())
    assert LEDGER_NAME in names and LOCK_NAME in names

    req = _fresh_requirement(tmp_path, _report(at=datetime.now(UTC), repo_clean=pf.FAIL))
    refused, refused_calls = _launcher(tmp_path, monkeypatch, req, dispatch_budget=9)
    assert "repo_clean" in refused.prepare(_node("n1"))
    assert refused_calls == []
    assert ledger.read_bytes() == before
    assert sorted(p.name for p in (tmp_path / "root").iterdir()) == names

    again, again_calls = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    assert again.prepare(_node("n1")) is None
    start = [c for c in again_calls if "start" in c][-1]
    assert start[start.index("--max-dispatches") + 1] == "5"


def test_a_run_that_exists_is_refused_too_and_nothing_is_called(tmp_path, monkeypatch):
    """Catches: the gate after the `state_exists` branch (a resumed run goes to
    `_ensure_runnable` and is dispatched next, which is what spends quota)."""
    run = tmp_path / "root" / "n1"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"run_id": "n1"}), encoding="utf-8")
    req = _fresh_requirement(tmp_path, _report(at=datetime.now(UTC), role_permissions=pf.FAIL))
    launcher, calls = _launcher(tmp_path, monkeypatch, req)
    assert "role_permissions" in launcher.prepare(_node("n1"))
    assert calls == []


def test_the_environment_is_asked_before_the_budget(tmp_path, monkeypatch):
    """Catches: the gate after `budget_argumente`. With a spent budget and an
    unfit environment the answer is the environment's, as a returned reason,
    not a BudgetExhausted raised: it is the more fundamental refusal, and the
    one an operator fixes first."""
    run = tmp_path / "root" / "old"
    run.mkdir(parents=True)
    (run / "state.json").write_text(
        json.dumps({"run_id": "old", "usage": {"dispatches": 9}}), encoding="utf-8")
    req = _fresh_requirement(tmp_path, _report(at=datetime.now(UTC), level=pf.NONE))
    launcher, _ = _launcher(tmp_path, monkeypatch, req, dispatch_budget=9)
    why = launcher.prepare(_node("n1"))
    assert "does not allow a start" in why and "says NOT_READY: 'isolation'" in why
    plain, _ = _launcher(tmp_path, monkeypatch, dispatch_budget=9)
    with pytest.raises(BudgetExhausted):
        plain.prepare(_node("n1"))


def test_a_dry_run_reads_no_report(tmp_path, monkeypatch):
    """Catches: the gate before the dry-run return (a dry run touches nothing,
    and a report that is not there must not make it refuse)."""
    import hoh.launcher as launcher_module
    monkeypatch.setattr(launcher_module, "check_preflight",
                        lambda *a, **k: pytest.fail("the gate was asked"))
    req = PreflightRequirement(tmp_path / "nothing.json", "unattended", 60)
    launcher, calls = _launcher(tmp_path, monkeypatch, req, dry_run=True)
    assert launcher.prepare(_node()) == "dry run: nothing is prepared"
    assert calls == []


def test_a_report_that_goes_stale_or_is_replaced_is_seen_by_the_next_start(tmp_path, monkeypatch):
    """Catches: the report cached in the launcher, in either direction. The
    same launcher refuses a start on a two-hour-old report, starts after a new
    report is written, and refuses again when that report is replaced by one
    that says NOT_READY."""
    old = _report(at=datetime.now(UTC) - timedelta(hours=2))
    req = PreflightRequirement(_put(tmp_path, old), "unattended", 3600)
    launcher, calls = _launcher(tmp_path, monkeypatch, req)
    why = launcher.prepare(_node("n1"))
    assert "s old" in why and "at most 3600 s" in why and calls == []
    _put(tmp_path, _report(at=datetime.now(UTC), repo=tmp_path / "repo", root=tmp_path / "root"))
    assert launcher.prepare(_node("n1")) is None
    assert [c for c in calls if "start" in c]
    # ... and the other way round: a report that was fine and then is not.
    _put(tmp_path, _report(at=datetime.now(UTC), trust_readiness=pf.FAIL))
    assert "'trust_readiness'" in launcher.prepare(_node("n2"))
    assert len([c for c in calls if "start" in c]) == 1


def test_an_unreadable_report_stops_a_start_as_a_reason_not_a_crash(tmp_path, monkeypatch):
    """Catches: an exception of the gate escaping `prepare` (the orchestrator
    catches BudgetExhausted only; anything else ends the session)."""
    path = tmp_path / "report.json"
    path.mkdir()
    launcher, calls = _launcher(tmp_path, monkeypatch,
                                PreflightRequirement(path, "unattended", 3600))
    why = launcher.prepare(_node())
    assert isinstance(why, str) and "not a regular file" in why and calls == []


def test_the_reason_is_kept_to_six_hundred_characters(tmp_path, monkeypatch):
    """Catches: a reason that grows with the report's path (quoted once for
    the missing file and once in the hint) and is persisted whole in the
    node's note."""
    deep = tmp_path
    for _ in range(8):
        deep = deep / ("d" * 80)
    launcher, calls = _launcher(tmp_path, monkeypatch,
                                PreflightRequirement(deep / "report.json", "unattended", 60))
    why = launcher.prepare(_node())
    assert isinstance(why, str) and len(why) == 600 and calls == []


@pytest.mark.parametrize("exc", [RuntimeError("a bug of the gate"), KeyError("k"), OSError("disk")],
                         ids=["runtime", "key", "os"])
def test_a_failure_of_the_gate_itself_is_not_turned_into_a_reason(tmp_path, monkeypatch, exc):
    """Catches: `except Exception` around the gate. Only a `PreflightRefused` is
    a reason; anything else is the gate's own defect, which stops loudly with
    nothing started (and is not a report that passed)."""
    import hoh.launcher as launcher_module

    def broken(*args, **kwargs):
        raise exc

    monkeypatch.setattr(launcher_module, "check_preflight", broken)
    launcher, calls = _launcher(tmp_path, monkeypatch, _fresh_requirement(tmp_path))
    with pytest.raises(type(exc)):
        launcher.prepare(_node())
    assert calls == []


def test_the_preflight_keyword_is_the_last_of_the_signature():
    """Catches: a keyword inserted before the others (the textual overlap with
    the strands that edit the same signature is why it is last)."""
    last = list(inspect.signature(HohRunLauncher.__init__).parameters.values())[-1]
    assert (last.name, last.kind, last.default) == (
        "preflight", inspect.Parameter.KEYWORD_ONLY, None)


@pytest.mark.parametrize("bad", ["report.json", Path("report.json"), 3, {"report": "r"}],
                         ids=["str", "path", "int", "dict"])
def test_a_requirement_that_is_not_one_is_refused_at_construction(tmp_path, bad):
    """Catches: a path accepted where a requirement (profile and age) is due,
    which would be a gate with no profile and no age."""
    with pytest.raises(ValueError, match="must be a PreflightRequirement"):
        HohRunLauncher(tmp_path / "root", tmp_path / "repo", preflight=bad)


def test_the_orchestrator_books_a_refusal_as_a_blocked_node_and_a_named_halt(tmp_path, monkeypatch):
    """Catches: a refusal that ends the session instead of halting it. The real
    launcher under the real controller: the node is BLOCKED with the reason on
    it, the halt is BLOCKED_DEPENDENCY and names the check, nothing was
    dispatched. `hoh project unblock` is what returns the node to READY once
    the environment has been measured again."""
    from hoh.orchestrator import GateRunner, HaltClass, ProjectController
    from hoh.project import Lifecycle, ProjectState
    from hoh.projectstore import ProjectStore

    class _Gates(GateRunner):
        def subject(self) -> str:
            return "abc1234"

        def run(self, subject):
            raise AssertionError("no gate runs while a node is blocked")

    store = ProjectStore(tmp_path / "projects", "p")
    state = ProjectState(project_id="p", repo_path=str(tmp_path))
    state.nodes = [_node("n1")]
    store.create(state)
    req = _fresh_requirement(tmp_path, _report(at=datetime.now(UTC), trust_readiness=pf.FAIL))
    launcher, calls = _launcher(tmp_path, monkeypatch, req)
    result = ProjectController(store, launcher, _Gates()).run()
    assert result.halt is HaltClass.BLOCKED_DEPENDENCY
    assert "cannot be made runnable" in result.reason and "'trust_readiness'" in result.reason
    node = store.read_state().node("n1")
    assert node.lifecycle is Lifecycle.BLOCKED
    assert "says NOT_READY: 'trust_readiness'" in node.note
    assert calls == []


def test_the_default_stays_off_for_other_features_even_after_a_tests_own_undo(monkeypatch, tmp_path):
    """Catches (P3-11 slice 3): the conftest's switch for the default requirement being lost. This
    module is not about the default, so `launcher.default_requirement` is off here -- and stays
    off when a test puts its own faults back with `monkeypatch.undo()` (the fixture holds a patch
    of its own). Without it every drive of another feature would be gated by a report it has none of."""
    import hoh.launcher as launcher_module

    assert launcher_module.default_requirement(tmp_path) is None
    monkeypatch.setattr(launcher_module, "hoh_command", launcher_module.hoh_command)
    monkeypatch.undo()
    assert launcher_module.default_requirement(tmp_path) is None
