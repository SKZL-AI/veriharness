"""P3-11 slice 4: no project that declares nothing starts a run on no report, whatever its launcher.

Slice 3 gated a project that declares nothing by a product default, but `HohRunLauncher.bind_routing`
is where the project directory reaches the launcher, and a driver factory may return a launcher that
has none: such a pair was bound by neither the default nor the project's `preflight.json`, and its
runs started on no report at all. These tests drive the production path -- `hoh project drive`
(`cli.main`) -> `resumeloop.drive_governed` -> `ProjectController` -> the real launcher with only
its process boundary replaced -- with a launcher that cannot be bound, and pin what the drive does
with it: a halt by name before anything is started, unless the project recorded a waiver; the same
halt on a requirement it cannot hold or a declaration it cannot read; quiet when repeated; and a
launcher that does bind is not touched by it. A last node shows the independent nodes of a parallel
wave start only on a report the default gate accepts. No agent, no Herdr, no network; everything
under `tmp_path`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_preflight_start_paths import Gates, RecordingLauncher, _report, _tree, _world

from hoh import cli
from hoh.launcher import unbound_preflight_refusal
from hoh.preflightgate import environment_identity

#: tests/conftest.py switches the default preflight (and the refusal of a launcher it cannot bind)
#: off for every module that does not say this
TESTS_THE_DEFAULT_PREFLIGHT = True
PREFLIGHT_FILE = "preflight.json"
WAIVER = {"schema": "hoh-preflight-waiver/1", "reason": "a development project",
          "decided_by": "operator", "decided_at": "2026-10-19T08:00:00Z"}
#: the driver factory reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}


class Unbound:
    """The real launcher behind a face that cannot be bound: everything the controller asks for is
    forwarded, `bind_routing` and `bind_preflight` are not there. This is what a driver factory's own
    launcher class looks like when it was written before the project could declare anything."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in ("bind_routing", "bind_preflight"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def build_driver(store):
    """The `driver.json` factory: the real launcher with a fake process boundary, with or without
    the face that cannot be bound."""
    inner = RecordingLauncher(CONFIG["root"], CONFIG["repo"], **CONFIG.get("kw", {}))
    CONFIG["launcher"] = inner
    CONFIG["master_calls"] = calls = []
    bind_master = inner.bind_master

    def spy(session_id, epoch):                   # the drive binds the master after the refusals
        calls.append((session_id, epoch))
        return bind_master(session_id, epoch)
    inner.bind_master = spy
    return (Unbound(inner) if CONFIG["unbound"] else inner), Gates()


class Drive:
    """`hoh project drive p --session sA` through `cli.main`, the driver registered by name."""

    def __init__(self, tmp_path, capsys, *, unbound, names=("n1",), **kw):
        self.tmp, self.capsys = tmp_path, capsys
        self.repo, self.root, self.store = _world(tmp_path, names)
        CONFIG.clear()
        CONFIG.update(root=self.root, repo=self.repo, unbound=unbound, kw=kw)
        (self.store.dir / "driver.json").write_text(json.dumps({
            "factory": "test_preflight_unbound:build_driver",
            "path": str(Path(__file__).resolve().parent)}))

    def declare(self, doc) -> Path:
        path = self.store.dir / PREFLIGHT_FILE
        path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8"))
        return path

    def run(self, session: str = "sA") -> tuple[int, dict]:
        rc = cli.main(["--root", str(self.root), "project", "drive", "p", "--session", session])
        out = self.capsys.readouterr().out
        return rc, json.loads(out) if out.strip().startswith("{") else {}

    @property
    def launcher(self) -> RecordingLauncher:
        return CONFIG["launcher"]

    def project_bytes(self) -> dict:
        return _tree(self.store.dir)

    def report_for_this_place(self, **over) -> Path:
        """A READY report of this machine for this project's repository and runs directory,
        where the default looks for it."""
        path = _report(self.store.dir / "preflight-report.json", **over)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["environment"] = environment_identity(repo=self.repo, root=self.root)
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path


@pytest.fixture
def unbound(tmp_path, capsys):
    return Drive(tmp_path, capsys, unbound=True)


def _only_the_sessions_own_bytes_moved(before: dict, after: dict) -> None:
    changed = {k for k in before if before[k] != after.get(k)} | (set(after) - set(before))
    assert "project.json" in before and after["project.json"] == before["project.json"]
    # the session's own records: its lease and epoch, the locks it took (`.lock` holds a pid and a second)
    assert all(k.startswith(("lease.", "epochs.json", ".lock", ".authority.lock")) for k in changed), \
        sorted(changed)


# --------------------------------------------------------------------------- #
# the register's node: nothing declared and no report starts nothing, whatever the launcher
# --------------------------------------------------------------------------- #

def test_no_launcher_starts_a_project_that_declares_nothing_on_no_report_a_waiver_is_the_only_way_out(
        tmp_path, capsys):
    """Catches (P3-11's last gap) a project whose driver factory returns a launcher that cannot be
    bound being driven on no report: through `hoh project drive`, a launcher that binds is held
    to the default (no report: the drive halts and nothing is typed), a launcher that cannot bind
    is refused **by name** before anything is started, and only a recorded waiver lets either
    start -- the waiver is the one way out, for both."""
    for unbound in (False, True):
        sub = tmp_path / ("unbound" if unbound else "bound")
        sub.mkdir()
        drive = Drive(sub, capsys, unbound=unbound)
        rc, out = drive.run()
        assert rc != 0 and out["halt"] != "CLOSED", out
        assert drive.launcher.calls == [], (unbound, drive.launcher.calls)
        if unbound:
            assert out["halt"] == "AMBIGUOUS" and "no bind_routing" in out["reason"], out
            assert "Unbound" in out["reason"] and "hoh-preflight-waiver/1" in out["reason"], out
        else:
            assert out["halt"] == "BLOCKED_DEPENDENCY", out
            assert "the environment preflight does not allow a start" in out["reason"], out
        drive.declare(WAIVER)
        rc, out = drive.run("sB")
        if not unbound:
            # the waiver of a launcher that binds is slice 3's; here it is the way out of the halt
            assert cli.main(["--root", str(drive.root), "project", "unblock", "p", "n1",
                             "--reason", "waived"]) == 0
            capsys.readouterr()
            rc, out = drive.run("sC")
        assert rc == 0 and out["halt"] == "CLOSED", (unbound, out)
        assert drive.launcher.merged == ["n1"]
        assert len(drive.launcher.starts()) == len(drive.launcher.runs()) == 1


# --------------------------------------------------------------------------- #
# what is refused, and what is not
# --------------------------------------------------------------------------- #

def test_a_launcher_that_cannot_be_bound_halts_the_drive_by_name_and_starts_nothing(unbound):
    """Catches a refusal that is silent (the node starts on no report), one that names no launcher
    or does not say how to leave the halt, one that happens after a worktree or a run exists, and
    one that burns the node: the drive halts AMBIGUOUS with the launcher's class, the missing
    `bind_routing` and the waiver form in the reason; no `hoh worktree`, no `hoh start`, no
    `hoh run`; the project's state is not written."""
    before = unbound.project_bytes()
    rc, out = unbound.run()
    assert rc == 1 and out["halt"] == "AMBIGUOUS", out
    reason = out["reason"]
    assert reason.startswith("the drive raised ValueError: preflight refused: the launcher (Unbound) "
                             "has no bind_routing"), reason
    assert "hoh-preflight-waiver/1" in reason and "reason, decided_by, decided_at" in reason, reason
    assert unbound.launcher.calls == []
    assert CONFIG["master_calls"] == []           # refused before the drive binds the master (review B, N1)
    _only_the_sessions_own_bytes_moved(before, unbound.project_bytes())
    assert not (unbound.root / "n1").exists() and not list(unbound.root.glob("n1*"))


def test_a_refusal_is_terminal_and_quiet_and_a_waiver_recorded_afterwards_goes_on(unbound):
    """Termination: the refusal is one reason per drive. No counter, no timer, no retry: a second
    drive halts the same way, calls nothing and writes nothing but its own session's lease and
    epoch records; recording the waiver is all it takes (no unblock: the node was never blocked)."""
    rc, first = unbound.run("sA")
    assert first["halt"] == "AMBIGUOUS"
    frozen = unbound.project_bytes()
    rc, second = unbound.run("sB")
    assert second["halt"] == "AMBIGUOUS" and second["reason"] == first["reason"], second
    assert unbound.launcher.calls == []
    _only_the_sessions_own_bytes_moved(frozen, unbound.project_bytes())
    unbound.declare(WAIVER)
    rc, third = unbound.run("sC")
    assert rc == 0 and third["halt"] == "CLOSED", third
    assert unbound.launcher.merged == ["n1"]


def test_a_waived_project_starts_exactly_what_it_did_and_types_no_preflight_option(unbound):
    """Catches a waiver that still gates (no report exists: the node would halt) and one that
    types an option nothing configured: the waiver is the recorded decision not to be gated; the
    launcher that cannot bind was never given a requirement."""
    unbound.declare(WAIVER)
    rc, out = unbound.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert len(CONFIG["master_calls"]) == 1       # and the master was bound once the refusals were past
    (start,) = unbound.launcher.starts()
    (run,) = unbound.launcher.runs()
    for argv in (start, run):
        assert not [w for w in argv if w.startswith("--preflight")]


def test_a_project_that_declares_a_routing_policy_sees_the_routing_words_first(unbound):
    """Catches the new refusal taking the place of the one that was there (review B, N8): a project that
    declares a routing policy and whose launcher cannot be bound halts with the routing words, unchanged;
    the preflight words come only once routing is not the obstacle -- so the existing words and their
    tests keep their meaning."""
    (unbound.store.dir / "routing-policy.json").write_text("{}", encoding="utf-8")
    rc, out = unbound.run()
    assert rc == 1 and out["halt"] == "AMBIGUOUS", out
    assert out["reason"].startswith("the drive raised ValueError: routing policy refused: this project "
                                    "declares a routing policy"), out["reason"]
    assert "preflight" not in out["reason"].split("cannot route the runs it starts")[0]
    assert unbound.launcher.calls == [] and CONFIG["master_calls"] == []


DECLARATIONS = {
    "a-requirement": (json.dumps({"schema": "hoh-preflight-requirement/1", "report": "/nowhere/r.json",
                                  "profile": "unattended", "max_age_seconds": 3600}).encode(),
                      "declares a requirement (preflight.json, profile 'unattended') and the launcher "
                      "(Unbound) has no bind_routing"),
    "waiver-extra-key": (json.dumps({**WAIVER, "until": "never"}).encode(),
                         "preflight requirement refused: preflight.json (a waiver) must have exactly the keys"),
    "waiver-no-reason": (json.dumps({**WAIVER, "reason": " "}).encode(),
                         "preflight requirement refused: preflight.json (a waiver) needs a printable `reason`"),
    "waiver-bad-time": (json.dumps({**WAIVER, "decided_at": "2026-02-30T08:00:00Z"}).encode(),
                        "(a waiver) needs `decided_at` as a UTC time"),
    "not-json": (b"{", "preflight requirement refused: preflight.json is not one UTF-8 JSON object"),
    "not-utf8": (b"\xff\xfe{}", "preflight requirement refused: preflight.json is not one UTF-8 JSON object"),
    "other-schema": (json.dumps({"schema": "x/1"}).encode(),
                     "preflight requirement refused: preflight.json is not a preflight requirement or waiver"),
    "oversize": (b" " * 5000, "preflight requirement refused: preflight.json is larger than 4096 bytes"),
    "list": (b"[]", "preflight requirement refused: preflight.json is not a preflight requirement or waiver"),
    "duplicate-key": (b'{"schema": "hoh-preflight-waiver/1", "schema": "hoh-preflight-waiver/1"}',
                      "preflight requirement refused: preflight.json is not one UTF-8 JSON object"),
    "directory": (None, "preflight requirement refused: preflight.json is a link or not one regular file"),
    "fifo": ("fifo", "preflight requirement refused: preflight.json is a link or not one regular file"),
    "dangling-link": ("dangling", "preflight requirement refused: preflight.json is a link or not one regular file"),
    "live-link": ("link", "preflight requirement refused: preflight.json is a link or not one regular file"),
}


@pytest.mark.parametrize("label", list(DECLARATIONS))
def test_only_a_well_formed_waiver_lets_a_launcher_that_cannot_be_bound_through(unbound, label):
    """Fail closed on the declaration: a requirement the launcher cannot hold, a waiver with a key
    too many, an empty reason, a date that does not exist, a file that is not JSON, not UTF-8, of
    another schema, over the size limit, a directory or a link to a perfectly good waiver -- each
    halts the drive by name before anything is started. Nothing is read leniently."""
    content, said = DECLARATIONS[label]
    path = unbound.store.dir / PREFLIGHT_FILE
    if content is None:
        path.mkdir()
    elif content == "link":
        good = unbound.tmp / "good-waiver.json"
        good.write_text(json.dumps(WAIVER), encoding="utf-8")
        path.symlink_to(good)
    elif content == "dangling":
        path.symlink_to(unbound.tmp / "nothing-here.json")
    elif content == "fifo":
        os.mkfifo(path)
    else:
        unbound.declare(content)
    rc, out = unbound.run()
    assert rc == 1 and out["halt"] == "AMBIGUOUS", out
    assert said in out["reason"], out["reason"]
    assert unbound.launcher.calls == []


def test_a_launcher_that_binds_is_not_asked_and_the_refusal_is_the_launchers_alone(tmp_path):
    """Catches a refusal that reaches a launcher the default already binds (every project of the
    product would halt) and one that reads the project for a launcher it does not concern: for a
    launcher with `bind_routing` the answer is None whatever the project holds -- no file is
    read -- and for one without, a missing project directory is the same refusal, not a crash."""
    launcher = RecordingLauncher(tmp_path / "root", tmp_path / "repo")
    hostile = tmp_path / "project-that-does-not-exist"
    assert unbound_preflight_refusal(launcher, hostile) is None
    refusal = unbound_preflight_refusal(Unbound(launcher), hostile)
    assert refusal is not None and refusal.startswith("preflight refused: the launcher (Unbound)")
    assert not hostile.exists()
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / PREFLIGHT_FILE).write_text(json.dumps(WAIVER), encoding="utf-8")
    assert unbound_preflight_refusal(Unbound(launcher), tmp_path / "project") is None
    assert unbound_preflight_refusal(Unbound(launcher), str(tmp_path / "project") + os.sep) is None


def test_the_size_limit_of_the_declaration_is_the_one_the_bind_reads(tmp_path):
    """Catches a second reader of `preflight.json` with a limit of its own: a waiver padded to exactly
    the 4096 bytes the bind reads is a waiver (the launcher that cannot be bound goes through), one byte
    more is refused by the same words."""
    project = tmp_path / "project"
    project.mkdir()
    who = Unbound(RecordingLauncher(tmp_path / "root", tmp_path / "repo"))
    body = json.dumps(WAIVER)
    path = project / PREFLIGHT_FILE
    path.write_text(body + " " * (4096 - len(body)), encoding="utf-8")
    assert path.stat().st_size == 4096 and unbound_preflight_refusal(who, project) is None
    path.write_text(body + " " * (4097 - len(body)), encoding="utf-8")
    assert "preflight.json is larger than 4096 bytes" in unbound_preflight_refusal(who, project)


# --------------------------------------------------------------------------- #
# unattended parallel waves
# --------------------------------------------------------------------------- #

def test_a_parallel_wave_starts_only_on_a_report_the_default_gate_accepts(tmp_path, capsys):
    """The requirement is readiness for unattended **parallel waves**: the independent nodes of a
    wave are each prepared by the same launcher, so the default gate stands in front of all of them.
    With no report (and the measuring tool absent) the head is refused and the drive halts
    BLOCKED_DEPENDENCY with nothing typed, no sibling started; with a report of this place both nodes
    start in one wave and the project closes."""
    refused = tmp_path / "refused"
    refused.mkdir()
    drive = Drive(refused, capsys, unbound=False, names=("n1", "n2"), wave_width=2)
    rc, out = drive.run()
    assert out["halt"] == "BLOCKED_DEPENDENCY", out
    assert "the environment preflight does not allow a start" in out["reason"], out
    assert drive.launcher.calls == []

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    drive = Drive(allowed, capsys, unbound=False, names=("n1", "n2"), wave_width=2)
    drive.report_for_this_place()
    rc, out = drive.run()
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert sorted(drive.launcher.merged) == ["n1", "n2"]
    assert len(drive.launcher.starts()) == len(drive.launcher.runs()) == 2
    assert [s for s in out["steps"] if s.startswith("WAVE")], out["steps"]
