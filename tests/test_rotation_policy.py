"""Context rotation, Layer B: the product decides when the orchestrating session is rotated
out, and performs it -- the policy, its record, the hold loop that applies it, the
governed record it needs, and the pointer that carries it to the successor.

Everything runs on fakes: a synthetic transcript under `tmp_path`, a fake wall clock for the
controller, a fake `perform` where the Layer-A protocol is not the subject, and -- for the one
end-to-end test -- the existing in-process launcher that runs the pointer's own command on a
thread. No Herdr, no agent and no real transcript is touched; a stub `herdr` first on PATH
logs any call and must stay silent. `hoh.rotation` is reached by attribute so that a missing
name fails the one test that needs it, not the collection.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import cli
from hoh import rollover as ro
from hoh import rotation
from hoh.epoch import EpochStore, Reason
from hoh.orchestrator import write_lock
from hoh.projectstore import ProjectStore
from hoh.rollover import Outcome, RolloverResult
from hoh.succession import MasterAuthority
from hoh.succession_capsule import load_capsule

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_800_000_000.0                                    # a fixed wall clock, 2027-01-15
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
WATCH_TAIL = ' --watch-pid "${CLAUDE_PID:-$PPID}"'


# --------------------------------------------------------------------------- #
# the scene
# --------------------------------------------------------------------------- #


def make_repo(tmp_path: Path, name: str = "gitrepo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, env=env)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "one"], check=True, env=env)
    return repo


def usage_line(tokens: int) -> str:
    return json.dumps({"type": "assistant", "isSidechain": False, "message": {
        "model": "claude-opus-5-5",
        "usage": {"input_tokens": tokens, "output_tokens": 7,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Scene:
    """A policy, the capsule input it names, a handoff path, a transcript, a project dir."""

    def __init__(self, tmp_path: Path, *, tokens: int = 20_000) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path
        self.repo = tmp_path / "gitrepo"
        self.transcript = tmp_path / "transcript.jsonl"
        self.transcript.write_text(usage_line(tokens) + "\n")
        self.handoff = tmp_path / "HANDOFF-CURRENT.md"
        self.template = tmp_path / "capsule-input.json"
        self.policy_file = tmp_path / "rotation-policy.json"
        self.dir = tmp_path / "project"
        self.dir.mkdir()
        self.write_template()
        self.write_policy()

    def template_dict(self) -> dict:
        return {
            "derive": [
                {"entry_id": "handoff", "section": "REGISTER", "deriver": "file_digest",
                 "params": {"path": str(self.handoff)}},
                {"entry_id": "repo-main", "section": "CANDIDATES", "deriver": "ref_state",
                 "params": {"repo": str(self.repo), "ref": "main"}},
                {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
                 "params": {"runs_root": str(self.tmp / "runs")}},
            ],
            "decisions": [{"entry_id": "op-1", "text": "never push without the operator",
                           "source": "operator", "at": "2026-10-04", "status": "decided"}],
            "advisory": [{"entry_id": "adv-1", "section": "CONTEXT",
                          "text": "resume the critical path"}],
        }

    def write_template(self, data: dict | None = None) -> None:
        self.template.write_text(json.dumps(data if data is not None else self.template_dict()))

    def policy_dict(self, **over) -> dict:
        body = {"schema_version": "veriharness.rotation-policy/1",
                "successor_model": "claude-opus-5-5", "capsule_input": str(self.template),
                "transcript": {"path": str(self.transcript)}, "threshold_tokens": 10_000,
                "poll_s": 0.05, "request_ttl_s": 60, "cooldown_s": 3600,
                "ack_timeout_s": 30, "takeover_s": 10}
        body.update(over)
        return body

    def write_policy(self, **over) -> None:
        self.policy_file.write_text(json.dumps(self.policy_dict(**over)))

    def last_request_id(self) -> str:
        ids = [r["request_id"] for r in self.log() if r["event"] == "rotation_requested"]
        return ids[-1] if ids else "rq-none"

    def write_handoff(self, text: str = "strand table\n", *, at: float | None = None,
                      named: bool = True) -> str:
        """The master's answer: a handoff whose first line names the latest request (or, with
        `named=False`, a file that was written after it and does not). Returns what it wrote."""
        body = f"# Handoff {self.last_request_id()}\n{text}" if named else text
        self.handoff.write_text(body)
        if at is not None:
            os.utime(self.handoff, (at, at))
        return body

    def controller(self, perform, *, clock: Clock | None = None, emit=None, on_reclaimed=None,
                   trigger=None, session: str = "pred"):
        loaded = rotation.load_policy(self.policy_file)
        self.rows: list[dict] = [] if emit is None else None  # type: ignore[assignment]
        out = self.rows if emit is None else None
        return rotation.RotationController(
            loaded, directory=self.dir, project_id="p", session=session,
            trigger=trigger or rotation.make_trigger(loaded.policy), perform=perform,
            on_reclaimed=on_reclaimed, emit=(out.append if emit is None else emit),
            wall=clock or Clock())

    def log(self) -> list[dict]:
        path = self.dir / "rotation" / "policy.log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def events(self) -> list[str]:
        return [r["event"] for r in self.log()]


class Performer:
    """A stand-in for the Layer-A protocol: records what it was given, answers what it is told."""

    def __init__(self, outcome: Outcome = Outcome.COMPLETE, *, master: bool | None = None) -> None:
        self.outcome = outcome
        self.master = (outcome is not Outcome.COMPLETE) if master is None else master
        self.calls: list[tuple] = []

    def __call__(self, inp, trigger):
        self.calls.append((inp, trigger))
        return RolloverResult(self.outcome, capsule_id="rc-test", detail=f"fake {self.outcome}",
                              predecessor_is_master=self.master, successor_epoch=2)


@pytest.fixture
def herdr_stub(tmp_path, monkeypatch):
    """A `herdr` first on PATH that logs every call and fails: no test here may reach Herdr."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    calls = tmp_path / "herdr-calls.log"
    exe = bindir / "herdr"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(calls))}\nexit 99\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HERDR_ENV", "1")
    yield calls
    assert not calls.exists() or calls.read_text() == "", "a test reached Herdr"


# --------------------------------------------------------------------------- #
# P -- the policy and the capsule input it names
# --------------------------------------------------------------------------- #

BOUNDS = [          # field, (refused below, accepted low), (accepted high, refused above)
    ("threshold_tokens", (9_999, 10_000), (10_000_000, 10_000_001)),
    ("request_ttl_s", (9, 10), (86_400, 86_401)),
    ("cooldown_s", (9, 10), (86_400, 86_401)),
    ("ack_timeout_s", (29, 30), (3_600, 3_601)),
    ("takeover_s", (9, 10), (3_600, 3_601)),
    ("poll_s", (0.009, 0.01), (60, 60.01)),
    ("prompt_delay_s", (-0.1, 0), (300, 300.1)),     # (with ack_timeout_s 3600: see below)
]


def refused(scene: Scene, **over) -> bool:
    scene.write_policy(**over)
    try:
        rotation.load_policy(scene.policy_file)
    except rotation.PolicyError:
        return True
    return False


def test_a_policy_is_a_document_with_every_number_bounded_and_nothing_unknown(tmp_path):
    scene = Scene(tmp_path)
    loaded = rotation.load_policy(scene.policy_file)
    assert loaded.policy.successor_model == "claude-opus-5-5"
    assert loaded.policy.successor_permission_mode == "auto"          # the default: unattended
    assert loaded.policy.successor_args() == ("--permission-mode", "auto")
    assert loaded.path == scene.policy_file
    assert loaded.sha256 == __import__("hashlib").sha256(scene.policy_file.read_bytes()).hexdigest()
    # defaults: only the required fields written
    scene.policy_file.write_text(json.dumps({
        "schema_version": "veriharness.rotation-policy/1", "successor_model": "m1",
        "capsule_input": str(scene.template), "transcript": {"path": str(scene.transcript)}}))
    p = rotation.load_policy(scene.policy_file).policy
    assert (p.threshold_tokens, p.request_ttl_s, p.cooldown_s) == (250_000, 1800, 1200)
    assert (p.ack_timeout_s, p.takeover_s, p.poll_s, p.prompt_delay_s) == (600, 300, 2.0, 0.0)
    for field, (below, low), (high, above) in BOUNDS:
        extra = {"ack_timeout_s": 3_600} if field == "prompt_delay_s" else {}
        assert refused(scene, **{field: below}, **extra), (field, below)
        assert not refused(scene, **{field: low}, **extra), (field, low)
        assert not refused(scene, **{field: high}, **extra), (field, high)
        assert refused(scene, **{field: above}, **extra), (field, above)
    # the type of a number is part of it: `true` is not 1, a string is not a number, NaN is none
    for field, bad in [("threshold_tokens", True), ("threshold_tokens", "20000"),
                       ("threshold_tokens", 20_000.0), ("cooldown_s", None),
                       ("request_ttl_s", 60.5), ("poll_s", True), ("poll_s", "1"),
                       ("prompt_delay_s", True), ("prompt_delay_s", float("nan")),
                       ("poll_s", float("inf"))]:
        assert refused(scene, **{field: bad}), (field, bad)
    # the delay comes out of the acknowledgement window
    assert refused(scene, ack_timeout_s=30, prompt_delay_s=30)
    assert not refused(scene, ack_timeout_s=30, prompt_delay_s=29)
    # closed vocabularies and shapes
    for over in ({"successor_permission_mode": "bypassPermissions"},
                 {"successor_permission_mode": "dontAsk"}, {"successor_effort": "extreme"},
                 {"schema_version": "veriharness.rotation-policy/2"}, {"surprise": 1},
                 {"successor_model": ""}, {"successor_model": "m m"},
                 {"successor_model": "m\n"}, {"successor_model": "\ud800"},
                 {"capsule_input": "relative.json"}, {"capsule_input": "~/in.json"},
                 {"transcript": {}}, {"transcript": {"path": "/t", "session_id_env": "SID"}},
                 {"transcript": {"path": "t.jsonl"}},
                 {"transcript": {"session_id_env": "lower"}},
                 {"transcript": {"session_id_env": "SID", "projects_dir": "relative"}},
                 {"transcript": {"path": "/t", "extra": 1}}):
        assert refused(scene, **over), over
    assert not refused(scene, successor_permission_mode=None, successor_effort="max")
    assert not refused(scene, transcript={"session_id_env": "CLAUDE_CODE_SESSION_ID"})
    assert not refused(scene, transcript={"session_id_env": "SID",
                                          "projects_dir": "~/.claude/projects"})


def odd_files(tmp: Path) -> dict[str, Path]:
    """Every shape of 'not a plain regular UTF-8 JSON object' a path can have."""
    good = tmp / "good.json"
    good.write_text("{}")
    cases: dict[str, Path] = {}
    cases["missing"] = tmp / "missing.json"
    cases["directory"] = tmp / "adir"
    cases["directory"].mkdir()
    cases["symlink"] = tmp / "link.json"
    cases["symlink"].symlink_to(good)
    cases["dangling"] = tmp / "dangling.json"
    cases["dangling"].symlink_to(tmp / "nowhere.json")
    cases["fifo"] = tmp / "fifo.json"
    os.mkfifo(cases["fifo"])
    cases["empty"] = tmp / "empty.json"
    cases["empty"].write_bytes(b"")
    cases["latin1"] = tmp / "latin1.json"
    cases["latin1"].write_bytes('{"a": "caf\xe9"}'.encode("latin-1"))
    cases["utf16"] = tmp / "utf16.json"
    cases["utf16"].write_bytes('{"a": 1}'.encode("utf-16"))
    cases["nul"] = tmp / "nul.json"
    cases["nul"].write_bytes(b'{"a": "x\x00y"}')
    cases["array"] = tmp / "array.json"
    cases["array"].write_text("[]")
    cases["scalar"] = tmp / "scalar.json"
    cases["scalar"].write_text("7")
    cases["null"] = tmp / "null.json"
    cases["null"].write_text("null")
    cases["torn"] = tmp / "torn.json"
    cases["torn"].write_text('{"a": ')
    cases["trailing"] = tmp / "trailing.json"
    cases["trailing"].write_text("{} {}")
    cases["duplicate"] = tmp / "dup.json"
    cases["duplicate"].write_text('{"a": 1, "a": 2}')
    cases["nan"] = tmp / "nan.json"
    cases["nan"].write_text('{"a": NaN}')
    cases["oversize"] = tmp / "big.json"
    cases["oversize"].write_text('{"a": "' + "x" * (rotation.MAX_POLICY_BYTES + 1) + '"}')
    cases["unreadable"] = tmp / "mode000.json"
    cases["unreadable"].write_text("{}")
    cases["unreadable"].chmod(0)
    return cases


def test_an_unusable_policy_or_capsule_input_file_is_refused_never_read_as_empty(tmp_path):
    scene = Scene(tmp_path)
    cases = odd_files(tmp_path / "odd") if (tmp_path / "odd").mkdir() is None else {}
    seen = 0
    for name, path in cases.items():
        if name == "unreadable" and os.access(path, os.R_OK):
            # root reads mode 000, so that one shape cannot exist; anyone else who can read it
            # is on a file system that ignores modes -- not a quiet pass either way
            assert os.geteuid() == 0, "a mode-000 file is readable by a user who is not root"
            warnings.warn("running as root: the mode-000 shape cannot exist here, 17 of the 18 "
                          "unusable-file shapes were checked")
            continue
        seen += 1
        with pytest.raises(rotation.PolicyError):
            rotation.load_policy(path)                  # as the policy file
        scene.write_policy(capsule_input=str(path))
        with pytest.raises(rotation.PolicyError):
            rotation.load_policy(scene.policy_file)     # as the capsule input it names
    assert seen == (17 if os.geteuid() == 0 else 18) == len(cases) - (os.geteuid() == 0)
    # the size ceilings are exact: a file of exactly the limit is read, one byte more is refused
    scene.write_policy()
    body = scene.policy_file.read_text()
    scene.policy_file.write_text(body + " " * (rotation.MAX_POLICY_BYTES - len(body.encode())))
    assert scene.policy_file.stat().st_size == rotation.MAX_POLICY_BYTES
    assert rotation.load_policy(scene.policy_file).policy.successor_model == "claude-opus-5-5"
    scene.policy_file.write_text(scene.policy_file.read_text() + " ")
    with pytest.raises(rotation.PolicyError, match="too large"):
        rotation.load_policy(scene.policy_file)
    scene.write_policy()
    tbody = scene.template.read_text()
    scene.template.write_text(tbody + " " * (rotation.MAX_TEMPLATE_BYTES - len(tbody.encode())))
    assert scene.template.stat().st_size == rotation.MAX_TEMPLATE_BYTES
    rotation.load_policy(scene.policy_file)
    scene.template.write_text(scene.template.read_text() + " ")
    with pytest.raises(rotation.PolicyError, match="too large"):
        rotation.load_policy(scene.policy_file)
    scene.write_template()
    # the control: a plain regular file of the same shape is read
    good = tmp_path / "odd" / "good.json"
    scene.write_policy()
    assert rotation.load_policy(scene.policy_file).policy.capsule_input == str(scene.template)
    assert good.read_text() == "{}"


def test_the_capsule_input_must_carry_the_masters_handoff_and_absolute_paths(tmp_path):
    scene = Scene(tmp_path)
    base = scene.template_dict()

    def check(mutate) -> bool:
        data = json.loads(json.dumps(base))
        mutate(data)
        scene.write_template(data)
        try:
            rotation.load_policy(scene.policy_file)
        except rotation.PolicyError:
            return False
        return True

    assert check(lambda d: None)
    # the vacuous default: a record, a run set and a head, and no narrative
    assert not check(lambda d: d["derive"].pop(0))
    assert not check(lambda d: d["derive"].append(dict(d["derive"][0], entry_id="handoff")))
    assert not check(lambda d: d["derive"][0].update(entry_id="handoff-2"))
    assert not check(lambda d: d["derive"][0].update(entry_id="Handoff"))
    assert check(lambda d: d["derive"][0].update(entry_id=" handoff "))   # the model strips: the same entry
    assert not check(lambda d: d["derive"][0].update(section="REVIEWS"))
    assert not check(lambda d: d["derive"][0].update(
        section="BLOCKERS", deriver="ref_state", params={"repo": str(scene.repo), "ref": "main"}))
    # every place a successor re-derives from is absolute: it starts in a directory of its own
    assert not check(lambda d: d["derive"][0]["params"].update(path="HANDOFF.md"))
    assert not check(lambda d: d["derive"][1]["params"].update(repo="gitrepo"))
    assert not check(lambda d: d["derive"][2]["params"].update(runs_root="runs"))
    # the capsule's own structure rule, said at load time and not at every freeze
    assert not check(lambda d: d["derive"].pop(2))
    assert check(lambda d: d["derive"].append(
        {"entry_id": "runs-2", "section": "RUNS", "deriver": "run_set",
         "params": {"runs_root": str(scene.tmp / "more-runs")}}))
    # the generation is the lease's, never the author's; unknown keys are refused
    assert not check(lambda d: d.update(programme_generation=7))
    assert not check(lambda d: d.update(surprise=True))
    # the successor model is the policy's, whatever the input says
    scene.write_template({**base, "successor_model": "something-else"})
    loaded = rotation.load_policy(scene.policy_file)
    assert rotation.validate_capsule_input(rotation.read_capsule_input(loaded.policy),
                                           loaded.policy).successor_model == "claude-opus-5-5"


# --------------------------------------------------------------------------- #
# T -- the master's transcript
# --------------------------------------------------------------------------- #


def test_the_masters_transcript_is_found_by_the_session_id_the_harness_exports_or_not_at_all(
        tmp_path):
    sid = "-".join(["a" * 8, "b" * 4, "4" + "c" * 3, "8" + "d" * 3, "e" * 12])   # (built, not written)
    projects = tmp_path / "projects"
    (projects / "-home").mkdir(parents=True)
    (projects / "-home" / f"{sid}.jsonl").write_text(usage_line(30_000) + "\n")
    spec = rotation.TranscriptSpec(session_id_env="SID", projects_dir=str(projects))
    found = rotation.resolve_transcript(spec, {"SID": sid})
    assert found == projects / "-home" / f"{sid}.jsonl"
    plain = projects / "-home" / f"{sid}.jsonl"
    assert rotation.resolve_transcript(rotation.TranscriptSpec(path=str(plain)), {}) == plain
    for env in ({}, {"SID": ""}, {"SID": "short"}, {"SID": "../../etc/passwd"},
                {"SID": "*"}, {"SID": sid + "\n"}, {"SID": "a/b/c/d/e/f/g/h"},
                {"SID": "-".join(["0" * 8, "0" * 4, "0" * 4, "0" * 4, "0" * 12])}):
        with pytest.raises(ro.TriggerError):
            rotation.resolve_transcript(spec, env)
    # the id's length is bounded (8 to 64 characters) and the match is on the whole name
    for n, found in ((7, False), (8, True), (64, True), (65, False)):
        odd = "x" * n
        (projects / "-len").mkdir(exist_ok=True)
        (projects / "-len" / f"{odd}.jsonl").write_text(usage_line(1) + "\n")
        if found:
            assert rotation.resolve_transcript(spec, {"SID": odd}) == projects / "-len" / f"{odd}.jsonl"
        else:
            with pytest.raises(ro.TriggerError):
                rotation.resolve_transcript(spec, {"SID": odd})
    # an explicit path that is not a regular file is no transcript -- a FIFO would block the
    # renewal inside `open` for as long as nobody writes to it
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    for bad in (fifo, tmp_path, tmp_path / "missing.jsonl"):
        with pytest.raises(ro.TriggerError):
            rotation.resolve_transcript(rotation.TranscriptSpec(path=str(bad)), {})
    link = tmp_path / "link.jsonl"
    link.symlink_to(projects / "-home" / f"{sid}.jsonl")
    assert rotation.resolve_transcript(rotation.TranscriptSpec(path=str(link)), {}) == link
    (projects / "-other").mkdir()
    (projects / "-other" / f"{sid}.jsonl").write_text(usage_line(1) + "\n")
    with pytest.raises(ro.TriggerError):                # found twice: not a guess
        rotation.resolve_transcript(spec, {"SID": sid})
    with pytest.raises(ro.TriggerError):                # a directory that is not there
        rotation.resolve_transcript(
            rotation.TranscriptSpec(session_id_env="SID", projects_dir=str(tmp_path / "no")),
            {"SID": sid})
    # the trigger measures the file it found, and fires at the threshold, not below
    (projects / "-other" / f"{sid}.jsonl").write_text("\n")
    lone = tmp_path / "lone"
    (lone / "-home").mkdir(parents=True)
    (lone / "-home" / f"{sid}.jsonl").write_text(usage_line(9_999) + "\n")
    policy_dict = {"schema_version": "veriharness.rotation-policy/1", "successor_model": "m",
                   "capsule_input": str(tmp_path / "in.json"),
                   "transcript": {"session_id_env": "SID", "projects_dir": str(lone)},
                   "threshold_tokens": 10_000}
    policy = rotation.RotationPolicy.model_validate(policy_dict)
    below = rotation.make_trigger(policy, {"SID": sid})()
    assert (below.fire, below.context_tokens, below.threshold) == (False, 9_999, 10_000)
    (lone / "-home" / f"{sid}.jsonl").write_text(usage_line(10_000) + "\n")
    assert rotation.make_trigger(policy, {"SID": sid})().fire is True


# --------------------------------------------------------------------------- #
# H -- the handoff
# --------------------------------------------------------------------------- #


RID = "rq-20270115T080000Z-0a1b2c3d"                     # a request id (the shape the controller makes)


def test_a_handoff_is_ready_only_when_written_after_the_request_and_plainly(tmp_path):
    now, request = T0 + 100, T0
    good = tmp_path / "good.md"
    body = f"# Handoff {RID}\nstrand table\n"
    good.write_text(body)
    os.utime(good, (T0 + 50, T0 + 50))
    data, digest, mtime = rotation.read_fresh_handoff(str(good), not_before=request, now=now,
                                                      request_id=RID)
    assert data == body.encode() and mtime == T0 + 50
    assert digest == hashlib.sha256(body.encode()).hexdigest()

    def not_ready(path: Path, **kw) -> str:
        with pytest.raises(rotation.HandoffNotReady) as info:
            rotation.read_fresh_handoff(str(path), not_before=kw.get("not_before", request),
                                        now=kw.get("now", now),
                                        request_id=kw.get("request_id", RID))
        return info.value.reason

    assert "does not exist" in not_ready(tmp_path / "missing.md")
    (tmp_path / "adir").mkdir()
    assert "not a regular file" in not_ready(tmp_path / "adir")
    (tmp_path / "link.md").symlink_to(good)
    assert "symbolic link" in not_ready(tmp_path / "link.md")
    (tmp_path / "dangling.md").symlink_to(tmp_path / "nowhere")
    assert "symbolic link" in not_ready(tmp_path / "dangling.md")
    os.mkfifo(tmp_path / "fifo.md")                     # opening it would block: it is not opened
    assert "not a regular file" in not_ready(tmp_path / "fifo.md")
    (tmp_path / "empty.md").write_bytes(b"")
    assert "empty" in not_ready(tmp_path / "empty.md")
    for name, content, word in [("blank.md", b" \n\t\n", "blank"),
                                ("latin1.md", f"café {RID}".encode("latin-1"), "UTF-8"),
                                ("utf16.md", RID.encode("utf-16"), "UTF-8"),
                                ("nul.md", f"{RID}\0".encode(), "NUL"),
                                ("surrogate.md", f"{RID}\ud800".encode("utf-8", "surrogatepass"),
                                 "UTF-8")]:
        (tmp_path / name).write_bytes(content)
        os.utime(tmp_path / name, (T0 + 50, T0 + 50))
        assert word in not_ready(tmp_path / name), name
    # at most MAX_HANDOFF_BYTES, exactly (the first line names the request)
    head = f"{RID}\n".encode()
    (tmp_path / "big.md").write_bytes(head + b"x" * (rotation.MAX_HANDOFF_BYTES + 1 - len(head)))
    assert "too large" in not_ready(tmp_path / "big.md")
    (tmp_path / "limit.md").write_bytes(head + b"x" * (rotation.MAX_HANDOFF_BYTES - len(head)))
    assert (tmp_path / "limit.md").stat().st_size == rotation.MAX_HANDOFF_BYTES
    os.utime(tmp_path / "limit.md", (T0 + 50, T0 + 50))
    assert rotation.read_fresh_handoff(str(tmp_path / "limit.md"), not_before=request, now=now,
                                       request_id=RID)[0]
    locked = tmp_path / "locked.md"
    locked.write_text(RID)
    locked.chmod(0)
    if os.access(locked, os.R_OK):
        # root reads mode 000: the shape cannot exist; not a quiet pass either way
        assert os.geteuid() == 0, "a mode-000 file is readable by a user who is not root"
        warnings.warn("running as root: the mode-000 handoff shape cannot exist here")
    else:
        assert "cannot be opened" in not_ready(locked)
    # freshness: the previous rotation's handoff is not this one's; the boundary is inclusive
    os.utime(good, (T0 - 1, T0 - 1))
    assert "before the request" in not_ready(good)
    os.utime(good, (T0, T0))
    assert rotation.read_fresh_handoff(str(good), not_before=request, now=now,
                                       request_id=RID)[2] == T0
    # ... and not in the future: +5 s is accepted and +5.05 s is not (the slack is exactly 5 s)
    assert rotation.FUTURE_SLACK_S == 5.0
    os.utime(good, (now + 5.0, now + 5.0))
    assert rotation.read_fresh_handoff(str(good), not_before=request, now=now,
                                       request_id=RID)[0]
    os.utime(good, (now + 5.05, now + 5.05))
    assert "future" in not_ready(good)
    assert issubclass(rotation.HandoffNotReady, Exception)
    # the answer to a request names it, on its FIRST line, as a whole word: a file that was merely
    # written after the request (a log, a transcript, a note) is not that answer
    os.utime(good, (T0 + 50, T0 + 50))
    other = "rq-20270115T080000Z-ffffffff"
    for name, text in [
            ("plain", RID), ("heading", f"# Handoff {RID}\nx"), ("sentence", f"Handoff for {RID} (replaces)\nx"),
            ("crlf", f"# {RID}\r\nx"), ("bom", f"\ufeff# {RID}\nx"), ("quoted", f'{{"id": "{RID}"}}\nx')]:
        (tmp_path / f"{name}.named.md").write_text(text, encoding="utf-8")
        os.utime(tmp_path / f"{name}.named.md", (T0 + 50, T0 + 50))
        assert rotation.read_fresh_handoff(str(tmp_path / f"{name}.named.md"), not_before=request,
                                           now=now, request_id=RID)[0], name
    for name, text in [
            ("second line", f"# Handoff\n{RID}\n"), ("empty first line", f"\n{RID}\n"),
            ("another request", f"# Handoff {other}\n"), ("no id", "# Handoff\nstrand table\n"),
            ("longer id", f"# {RID}ef\n"), ("prefixed", f"# x{RID}\n"), ("dashed", f"# {RID}-2\n"),
            ("upper case", f"# {RID.upper()}\n"), ("truncated", f"# {RID[:-1]}\n")]:
        (tmp_path / f"{name}.unnamed.md").write_text(text, encoding="utf-8")
        os.utime(tmp_path / f"{name}.unnamed.md", (T0 + 50, T0 + 50))
        assert "does not name the request" in not_ready(tmp_path / f"{name}.unnamed.md"), name
    # (no request id, no answer: an empty id is not a pattern that matches between two words)
    (tmp_path / "gap.md").write_text("a b\n")
    os.utime(tmp_path / "gap.md", (T0 + 50, T0 + 50))
    assert "does not name the request" in not_ready(tmp_path / "gap.md", request_id="")


# --------------------------------------------------------------------------- #
# C -- the controller, on fakes
# --------------------------------------------------------------------------- #


def test_below_the_threshold_nothing_is_recorded_asked_or_done(tmp_path):
    scene = Scene(tmp_path, tokens=9_999)
    perform = Performer()
    ctl = scene.controller(perform)
    ctl.arm()
    for _ in range(5):
        tick = ctl.tick()
        assert (tick.next_in, tick.stop) == (None, False)
    assert perform.calls == []
    assert scene.events() == ["rotation_armed"]                  # the only row: it is armed
    assert [r["event"] for r in scene.rows] == ["rotation_armed"]
    assert not scene.handoff.exists() and not (scene.dir / "rotation" / "handoffs").exists()
    # the control: at exactly the threshold the same controller asks
    scene.transcript.write_text(usage_line(10_000) + "\n")
    assert ctl.tick().next_in == 0.05
    assert scene.events() == ["rotation_armed", "rotation_requested"]


def test_at_the_threshold_it_asks_the_master_for_a_handoff_and_records_why(tmp_path):
    scene = Scene(tmp_path, tokens=262_144)
    scene.write_policy(threshold_tokens=250_000)
    clock = Clock()
    perform = Performer()
    ctl = scene.controller(perform, clock=clock)
    ctl.arm()
    tick = ctl.tick()
    assert (tick.next_in, tick.stop) == (0.05, False)            # soon again: a request is open
    [row] = [r for r in scene.log() if r["event"] == "rotation_requested"]
    assert row["context_tokens"] == 262_144 and row["threshold"] == 250_000
    assert row["reason"] == "context_tokens" and row["project_id"] == "p"
    assert row["session"] == "pred" and row["handoff"] == str(scene.handoff)
    assert row["at"] == "2027-01-15T08:00:00Z" and row["deadline"] == "2027-01-15T08:01:00Z"
    assert row["request_id"].startswith("rq-") and "Write your handoff" in row["instruction"]
    assert str(scene.handoff) in row["instruction"]
    assert "first line" in row["instruction"] and row["request_id"] in row["instruction"]
    # the same row went to the operator-facing channel, whole
    assert scene.rows[-1] == row
    assert perform.calls == []                                   # asked; nothing done
    # while it waits: no second request, and each reason is recorded once, not every tick
    for _ in range(4):
        clock.advance(1)
        assert ctl.tick().next_in == 0.05
    assert scene.events().count("rotation_requested") == 1
    waiting = [r for r in scene.log() if r["event"] == "rotation_handoff_waiting"]
    assert len(waiting) == 1 and "does not exist" in waiting[0]["reason"]
    # a handoff from BEFORE the request (the previous rotation's) is not an answer
    scene.write_handoff(at=T0 - 5)
    clock.advance(1)
    ctl.tick()
    reasons = [r["reason"] for r in scene.log() if r["event"] == "rotation_handoff_waiting"]
    assert len(reasons) == 2 and "before the request" in reasons[1]
    assert perform.calls == []
    # a file written after the request that does not name it is not the master's answer to it
    # (the transcript, the log, a note: something else wrote it)
    scene.write_handoff("something written for another reason\n", at=T0 + 1, named=False)
    clock.advance(1)
    ctl.tick()
    reasons = [r["reason"] for r in scene.log() if r["event"] == "rotation_handoff_waiting"]
    assert len(reasons) == 3 and "does not name the request" in reasons[2]
    assert row["request_id"] in reasons[2] and perform.calls == []


def test_with_a_fresh_handoff_it_binds_a_copy_and_performs_the_rotation(tmp_path):
    scene = Scene(tmp_path)
    clock = Clock()
    perform = Performer(Outcome.COMPLETE)
    ctl = scene.controller(perform, clock=clock)
    ctl.arm()
    ctl.tick()                                                   # the request
    [req] = [r for r in scene.log() if r["event"] == "rotation_requested"]
    clock.advance(3)
    written = scene.write_handoff("the strand table\n", at=T0 + 2).encode()
    tick = ctl.tick()
    assert tick.stop is True                                     # the lease is the successor's now
    [(inp, trigger)] = perform.calls
    assert trigger().fire is True                                # the protocol re-checks the trigger
    # ... but a size estimate (no usable request in the transcript) never decides: after the
    # consent the protocol would otherwise read "do not know" as "rotate" (the file is large)
    scene.transcript.write_text("x" * (16 * 1024 * 1024 + 1) + "\n")
    estimated = trigger()
    assert estimated.estimate is True and estimated.fire is False
    scene.transcript.write_text(usage_line(20_000) + "\n")
    assert inp.successor_model == "claude-opus-5-5"
    [handoff] = [d for d in inp.derive if d.entry_id == "handoff"]
    copy = Path(handoff.params["path"])
    assert copy == scene.dir / "rotation" / "handoffs" / f"{req['request_id']}.handoff.md"
    assert copy.read_bytes() == written
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert [d.entry_id for d in inp.derive] == ["handoff", "repo-main", "runs"]   # as written
    assert [d.entry_id for d in inp.decisions] == ["op-1"]
    # whatever the spelling of the entry's name in the input (the model strips blanks), what
    # is bound is the copy and never the living file
    spelled = Scene(tmp_path / "spelled")
    data = spelled.template_dict()
    data["derive"][0]["entry_id"] = " handoff "
    spelled.write_template(data)
    clock2 = Clock()
    perform2 = Performer(Outcome.COMPLETE)
    ctl2 = spelled.controller(perform2, clock=clock2)
    ctl2.arm()
    ctl2.tick()
    spelled.write_handoff("handoff 2\n", at=T0 + 1)
    clock2.advance(2)
    assert ctl2.tick().stop is True
    [(inp2, _t)] = perform2.calls
    [bound] = [d for d in inp2.derive if d.entry_id == "handoff"]
    assert Path(bound.params["path"]).parent == spelled.dir / "rotation" / "handoffs"
    # the master may keep writing: what the capsule binds is the copy
    scene.write_handoff("changed after the freeze\n")
    assert copy.read_bytes() == written
    assert scene.events() == ["rotation_armed", "rotation_requested", "rotation_handoff_ready",
                              "rotation_started", "rotation_finished"]
    ready = scene.log()[2]
    assert ready["sha256"] == __import__("hashlib").sha256(copy.read_bytes()).hexdigest()
    done = scene.log()[-1]
    assert (done["outcome"], done["capsule_id"], done["context_tokens"], done["threshold"]) == (
        "COMPLETE", "rc-test", 20_000, 10_000)
    # every transition is shown to the operator, whole and in order (the quiet bookkeeping
    # rows -- a handoff that is ready -- are in the log only)
    assert [r["event"] for r in scene.rows] == [e for e in scene.events()
                                                 if e != "rotation_handoff_ready"]
    assert scene.rows == [r for r in scene.log() if r["event"] != "rotation_handoff_ready"]
    assert all(r["project_id"] == "p" for r in scene.rows)


@pytest.mark.parametrize("outcome", [Outcome.REFUSED, Outcome.LAUNCH_FAILED, Outcome.NO_ACK,
                                     Outcome.BAD_ACK, Outcome.HANDOFF_REFUSED,
                                     Outcome.SUPERSEDED, Outcome.RECLAIMED])
def test_an_outcome_that_is_not_a_completion_ends_the_request_on_record_and_cools_down(
        tmp_path, outcome):
    scene = Scene(tmp_path)
    scene.write_policy(cooldown_s=100)
    clock = Clock()
    reclaimed: list[int] = []
    perform = Performer(outcome)
    ctl = scene.controller(perform, clock=clock, on_reclaimed=lambda: reclaimed.append(1))
    ctl.arm()
    ctl.tick()
    scene.write_handoff(at=T0 + 1)
    clock.advance(2)
    tick = ctl.tick()
    assert tick.stop is False                                    # the master stays the master
    done = [r for r in scene.log() if r["event"] == "rotation_finished"]
    assert [r["outcome"] for r in done] == [outcome.value]
    assert reclaimed == ([1] if outcome is Outcome.RECLAIMED else [])
    # cool-down: no request for cooldown_s, however far over the threshold the context is
    calls = len(perform.calls)
    clock.advance(99)                                            # 99 s after the outcome
    assert ctl.tick().next_in is None
    assert scene.events().count("rotation_requested") == 1
    clock.advance(1)                                             # 100 s: over
    assert ctl.tick().next_in == 0.05
    assert scene.events().count("rotation_requested") == 2
    assert len(perform.calls) == calls


@pytest.mark.parametrize("outcome", [Outcome.DEFERRED, Outcome.IN_FLIGHT])
def test_a_rotation_that_has_no_safe_point_yet_is_retried_until_the_request_expires(
        tmp_path, outcome):
    scene = Scene(tmp_path)
    scene.write_policy(request_ttl_s=60, cooldown_s=100)
    clock = Clock()
    perform = Performer(outcome, master=True)
    ctl = scene.controller(perform, clock=clock)
    ctl.arm()
    ctl.tick()
    scene.write_handoff(at=T0 + 1)
    for _ in range(5):
        clock.advance(2)
        assert ctl.tick().next_in == 0.05
    assert len(perform.calls) == 5                               # tried at every tick
    assert scene.events().count("rotation_deferred") == 1        # said once
    assert "rotation_finished" not in scene.events()
    clock.advance(60)                                            # past the deadline
    assert ctl.tick().next_in is None
    expired = [r for r in scene.log() if r["event"] == "rotation_request_expired"]
    assert len(expired) == 1 and expired[0]["reason"] == "the program's write lock stayed held"
    assert len(perform.calls) == 5                               # not tried again after expiry
    # and the master keeps its handoff copy: nothing is deleted
    assert len(list((scene.dir / "rotation" / "handoffs").iterdir())) == 1


def test_a_context_that_dropped_again_withdraws_the_request_without_a_cooldown(tmp_path):
    scene = Scene(tmp_path)
    clock = Clock()
    perform = Performer(Outcome.NOT_TRIGGERED, master=True)
    ctl = scene.controller(perform, clock=clock)
    ctl.arm()
    ctl.tick()
    scene.write_handoff(at=T0 + 1)
    clock.advance(2)
    assert ctl.tick().stop is False
    assert "rotation_withdrawn" in scene.events() and "rotation_finished" not in scene.events()
    scene.transcript.write_text(usage_line(20_000) + "\n")       # grows again: asks again at once
    ctl.tick()
    assert scene.events().count("rotation_requested") == 2


def test_a_request_without_a_handoff_expires_on_record_and_nothing_rotates(tmp_path):
    scene = Scene(tmp_path)
    scene.write_policy(request_ttl_s=60, cooldown_s=300)
    clock = Clock()
    perform = Performer()
    ctl = scene.controller(perform, clock=clock)
    ctl.arm()
    ctl.tick()
    clock.advance(59)
    assert ctl.tick().next_in == 0.05                            # one second before the deadline
    clock.advance(1)
    assert ctl.tick().next_in is None
    [expired] = [r for r in scene.log() if r["event"] == "rotation_request_expired"]
    assert expired["reason"].startswith("the handoff does not exist")
    assert expired["cooldown_until"] == "2027-01-15T08:06:00Z"   # now + cooldown_s
    assert perform.calls == []
    clock.advance(299)
    assert ctl.tick().next_in is None                            # cooling down
    clock.advance(1)
    assert ctl.tick().next_in == 0.05                            # asks again, once
    assert scene.events().count("rotation_requested") == 2
    # a handoff that arrives only after the deadline is not an answer to that request
    clock.advance(60)
    ctl.tick()
    scene.write_handoff(at=clock() + 1)
    clock.advance(2)
    assert ctl.tick().next_in is None
    assert perform.calls == []


def test_a_fault_never_escapes_a_tick_and_nothing_rotates_unrecorded(tmp_path):
    # (a) an unreadable transcript is no measurement: said once, however often it is asked
    scene = Scene(tmp_path)
    scene.transcript.chmod(0o600)
    missing = tmp_path / "gone.jsonl"
    scene.write_policy(transcript={"path": str(missing)})
    perform = Performer()
    ctl = scene.controller(perform)
    ctl.arm()
    for _ in range(4):
        assert ctl.tick().stop is False
    blind = [r for r in scene.log() if r["event"] == "rotation_blind"]
    assert len(blind) == 1 and "cannot read the transcript" in blind[0]["detail"]
    assert perform.calls == [] and "rotation_requested" not in scene.events()
    missing.write_text(usage_line(20_000) + "\n")                # readable again: it measures
    ctl.tick()
    assert "rotation_requested" in scene.events()

    # (b) an estimate (no usable request in the transcript) never starts a rotation
    est = Scene(tmp_path / "est")
    est.transcript.write_text("x" * (16 * 1024 * 1024 + 1) + "\n")
    p2 = Performer()
    c2 = est.controller(p2)
    c2.arm()
    for _ in range(3):
        c2.tick()
    assert est.events().count("rotation_estimate_ignored") == 1
    assert "rotation_requested" not in est.events() and p2.calls == []

    # (c) a request that cannot be recorded is not made: the log directory is a link out
    lk = Scene(tmp_path / "lk")
    outside = tmp_path / "outside"
    outside.mkdir()
    (lk.dir / "rotation").symlink_to(outside)
    p3 = Performer()
    c3 = lk.controller(p3)
    c3.arm()                                                     # cannot record; says so
    assert c3.tick().stop is False
    assert list(outside.iterdir()) == [] and p3.calls == []
    assert any(r["event"] == "rotation_error" for r in lk.rows)

    # (c2) the log itself a link to somebody else's file: not appended to, nothing rotates
    l2 = Scene(tmp_path / "l2")
    (l2.dir / "rotation").mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    (l2.dir / "rotation" / "policy.log.jsonl").symlink_to(victim)
    p32 = Performer()
    c32 = l2.controller(p32)
    c32.arm()
    assert c32.tick().stop is False
    assert victim.read_text() == "precious\n" and p32.calls == []

    # (c2b) the log file a FIFO: arming must not hang reading it, and nothing is appended
    l2b = Scene(tmp_path / "l2b")
    (l2b.dir / "rotation").mkdir()
    os.mkfifo(l2b.dir / "rotation" / "policy.log.jsonl")
    p32b = Performer()
    c32b = l2b.controller(p32b)
    th = threading.Thread(target=lambda: (c32b.arm(), c32b.tick()), daemon=True)
    th.start()
    th.join(5)
    hung = th.is_alive()
    if hung:                                            # let the blocked open go before failing
        os.close(os.open(l2b.dir / "rotation" / "policy.log.jsonl", os.O_WRONLY))
        th.join(5)
    assert not hung, "arming blocked on a FIFO log"
    assert p32b.calls == [] and any(r["event"] == "rotation_error" for r in l2b.rows)

    # (c3) a handoff ready, but the directory its copy goes to is a link out: no rotation
    l3 = Scene(tmp_path / "l3")
    clock3 = Clock()
    p33 = Performer()
    c33 = l3.controller(p33, clock=clock3)
    c33.arm()
    c33.tick()
    (l3.dir / "rotation" / "handoffs").symlink_to(outside)
    l3.write_handoff(at=T0 + 1)
    clock3.advance(2)
    assert c33.tick().stop is False
    assert list(outside.iterdir()) == [] and p33.calls == []
    assert "rotation_error" in l3.events() and "rotation_finished" not in l3.events()

    # (c4) a transcript that is a FIFO is no measurement and does not block the tick
    ff = Scene(tmp_path / "ff")
    fifo = tmp_path / "ff" / "fifo.jsonl"
    os.mkfifo(fifo)
    ff.write_policy(transcript={"path": str(fifo)})
    c34 = ff.controller(Performer())
    c34.arm()
    t = threading.Thread(target=c34.tick, daemon=True)
    t.start()
    t.join(5)
    stuck = t.is_alive()
    if stuck:                                           # let the blocked open go before failing
        fd = os.open(fifo, os.O_WRONLY)
        os.close(fd)
        t.join(5)
    assert not stuck, "the tick blocked on a FIFO"
    assert [r["event"] for r in ff.log()] == ["rotation_armed", "rotation_blind"]

    # (d) the protocol raising is a fault, recorded, with a cool-down; the tick returns
    ex = Scene(tmp_path / "ex")
    clock = Clock()

    def explode(inp, trigger):
        raise RuntimeError("the launcher fell over")

    c4 = ex.controller(explode, clock=clock)
    c4.arm()
    c4.tick()
    ex.write_handoff(at=T0 + 1)
    clock.advance(2)
    tick = c4.tick()
    assert tick.stop is False
    err = [r for r in ex.log() if r["event"] == "rotation_error"]
    assert len(err) == 1 and "RuntimeError: the launcher fell over" in err[0]["detail"]
    clock.advance(100)
    assert c4.tick().next_in is None                             # cooling down after a fault

    # (e) the capsule input changing for the worse between the arming and the request
    bad = Scene(tmp_path / "bad")
    p5 = Performer()
    c5 = bad.controller(p5)
    c5.arm()
    bad.template.write_text("not json")
    assert c5.tick().stop is False
    assert p5.calls == [] and "rotation_requested" not in bad.events()
    assert any(r["event"] == "rotation_error" for r in bad.log())


def test_the_cooldown_survives_a_restart_and_a_fresh_successor_is_not_rotated_at_once(tmp_path):
    scene = Scene(tmp_path)
    scene.write_policy(cooldown_s=1000)
    clock = Clock()
    first = scene.controller(Performer(Outcome.COMPLETE), clock=clock)
    first.arm()
    first.tick()
    scene.write_handoff(at=T0 + 1)
    clock.advance(2)
    assert first.tick().stop is True
    # the successor's controller: a new process, the same log, the same (huge) transcript
    clock.advance(500)
    perform = Performer()
    second = scene.controller(perform, clock=clock, session="succ")
    second.arm()
    armed = [r for r in scene.log() if r["event"] == "rotation_armed"]
    assert [r["session"] for r in armed] == ["pred", "succ"]
    assert armed[1]["cooldown_until"] == "2027-01-15T08:16:42Z"  # 8:00:02 + 1000 s
    assert second.tick().next_in is None
    clock.advance(499)
    assert second.tick().next_in is None
    clock.advance(2)
    assert second.tick().next_in == 0.05
    assert scene.events().count("rotation_requested") == 2
    # the log is read up to MAX_LOG_BYTES, exactly: a log of that size still gives the cool-down
    # back, one byte more is not read at all (a log of any size is never slurped); the file is
    # sparse, its tail is NUL and no row
    assert rotation.MAX_LOG_BYTES == 32 * 1024 * 1024
    for extra, restored in ((0, True), (1, False)):
        big = Scene(tmp_path / f"big{extra}")
        big.write_policy(cooldown_s=1000)
        (big.dir / "rotation").mkdir()
        log = big.dir / "rotation" / "policy.log.jsonl"
        log.write_text(json.dumps({"at": "2027-01-15T07:59:00Z", "event": "rotation_finished",
                                   "project_id": "p", "session": "pred"}) + "\n")
        os.truncate(log, rotation.MAX_LOG_BYTES + extra)
        ctl = big.controller(Performer(), clock=Clock())
        ctl.arm()
        assert (ctl.cooldown_until == T0 - 60 + 1000) is restored, extra


def test_a_session_that_has_just_taken_over_starts_in_a_cooldown_of_its_own(tmp_path):
    scene = Scene(tmp_path)                   # (no rotation on record at all: a bare log)
    scene.write_policy(cooldown_s=1000)
    clock = Clock()
    perform = Performer()
    ctl = scene.controller(perform, clock=clock, session="succ")
    ctl.arm(fresh=True)
    [armed] = scene.log()
    assert armed["cooldown_until"] == "2027-01-15T08:16:40Z"                 # now + 1000 s
    clock.advance(999)
    assert ctl.tick().next_in is None and "rotation_requested" not in scene.events()
    clock.advance(1)
    assert ctl.tick().next_in == 0.05
    # the control: a controller that is not fresh asks at once
    plain = Scene(tmp_path / "plain")
    again = plain.controller(Performer())
    again.arm()
    assert again.tick().next_in == 0.05


def test_a_handoff_the_product_or_the_harness_writes_anyway_is_no_consent(tmp_path, monkeypatch):
    """The master's writing of the handoff is its consent; the product infers it from a file
    that changed after the request. So the file may not be one something else writes."""
    scene = Scene(tmp_path)
    home = scene.dir                                             # the project's directory
    (home / "rotation" / "handoffs").mkdir(parents=True)
    into = tmp_path / "into-project"
    into.symlink_to(home)
    hard = tmp_path / "hardlink-of-the-transcript.md"
    os.link(scene.transcript, hard)
    soft = tmp_path / "symlink-to-the-transcript.md"
    soft.symlink_to(scene.transcript)
    places = {
        "the decision log": home / "rotation" / "policy.log.jsonl",
        "the project state": home / "project.json",
        "a handoff copy": home / "rotation" / "handoffs" / "rq-x.handoff.md",
        "the project directory": home,
        "dot-dot into the project": tmp_path / "nowhere" / ".." / "project" / "x.md",
        "a link to the project": into / "rotation" / "policy.log.jsonl",
        "the transcript": scene.transcript,
        "a hard link of the transcript": hard,
        "a link to the transcript": soft,
        "the policy file": scene.policy_file,
        "the capsule input": scene.template,
    }
    controls = {
        "the default place": scene.handoff,
        "a sibling whose name starts like the project's": tmp_path / "project-notes" / "HANDOFF.md",
        "a file named like the log in another directory": tmp_path / "other" / "policy.log.jsonl",
    }

    def point_at(place: Path) -> None:
        data = scene.template_dict()
        data["derive"][0]["params"]["path"] = str(place)
        scene.write_template(data)

    for what, place in places.items():
        point_at(place)                                          # at the load, with the directory
        with pytest.raises(rotation.PolicyError, match="handoff path"):
            rotation.load_policy(scene.policy_file, directory=home)
        rotation.load_policy(scene.policy_file)                  # (no directory: nothing to compare)
    for what, place in controls.items():
        point_at(place)
        rotation.load_policy(scene.policy_file, directory=home)
    # ... and again at every request, which is where the transcript the environment names is
    # known: the controller refuses, nothing is asked of the master, nothing rotates
    sid = "-".join(["b" * 8, "b" * 4, "4" + "b" * 3, "8" + "b" * 3, "b" * 12])
    projects = tmp_path / "harness-projects"
    (projects / "-home").mkdir(parents=True)
    env_transcript = projects / "-home" / f"{sid}.jsonl"
    env_transcript.write_text(usage_line(20_000) + "\n")
    env_hard = tmp_path / "hardlink-of-the-env-transcript.md"
    os.link(env_transcript, env_hard)
    monkeypatch.setenv("SID", sid)
    by_id = {"transcript": {"session_id_env": "SID", "projects_dir": str(projects)}}
    for what, place, policy_over in [
            ("the decision log", places["the decision log"], {}),
            ("the project's own state", places["the project state"], {}),
            ("the transcript by path", scene.transcript, {}),
            ("the transcript by session id", env_transcript, by_id),
            ("a hard link of it", env_hard, by_id)]:
        point_at(place)
        scene.write_policy(**policy_over)
        perform = Performer()
        ctl = scene.controller(perform, clock=Clock())          # (no load-time check: a bare
        ctl.arm()                                               #  controller, as the unit tests use)
        assert ctl.tick().stop is False, what
        assert [r["event"] for r in scene.log()] == ["rotation_armed", "rotation_error"], what
        assert "handoff path" in scene.log()[1]["detail"], what
        assert perform.calls == [], what
        # (the consent attack, concretely: with the product's own log named as the handoff the
        #  request row itself "answers" the request -- it was written after it)
        (home / "rotation" / "policy.log.jsonl").rename(tmp_path / f"log-{len(what)}.v1")


def test_a_completed_rotation_stops_the_loop_even_when_its_closing_row_cannot_be_written(tmp_path):
    """COMPLETE means the lease is the successor's: whatever happens to the record afterwards,
    this loop has nothing left to renew. Every other outcome keeps the master in its loop."""
    for outcome, stops in ((Outcome.COMPLETE, True), (Outcome.NO_ACK, False)):
        scene = Scene(tmp_path / outcome.value)
        clock = Clock()
        log = scene.dir / "rotation" / "policy.log.jsonl"

        def perform(inp, trigger, scene=scene, log=log, outcome=outcome):
            os.link(log, scene.tmp / "alias.jsonl")              # the log is no longer a file of
            return RolloverResult(outcome, capsule_id="rc-1",    # its own: appending is refused
                                  predecessor_is_master=outcome is not Outcome.COMPLETE,
                                  successor_epoch=2)

        ctl = scene.controller(perform, clock=clock)
        ctl.arm()
        ctl.tick()
        scene.write_handoff(at=T0 + 1)
        clock.advance(2)
        tick = ctl.tick()
        assert tick.stop is stops, outcome
        assert "rotation_finished" not in scene.events()         # it could not be written ...
        [err] = [r for r in scene.rows if r["event"] == "rotation_error"]
        assert err["unrecorded"] is True                         # ... and the channel says so
        clock.advance(10)
        assert ctl.tick().next_in is None                        # the cool-down runs either way


# --------------------------------------------------------------------------- #
# L -- the hold loop
# --------------------------------------------------------------------------- #


def test_the_hold_loop_applies_the_policy_after_each_good_renewal_and_obeys_its_answer(tmp_path):
    def run(on_tick, *, renew_fails=(), limit=6, interval=60.0):
        auth = MasterAuthority(tmp_path / f"p{len(list(tmp_path.iterdir()))}", "pred")
        auth.acquire()
        calls = {"renew": 0}
        real = auth.renew

        def renew():
            calls["renew"] += 1
            if calls["renew"] in renew_fails:
                raise OSError("a hiccup")
            return real()

        auth.renew = renew
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= limit:
                raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            ro.hold_lease(auth, interval=interval, sleep=sleep, monotonic=lambda: 0.0,
                          on_tick=on_tick)
        return auth, sleeps, calls

    seen: list[int] = []
    _auth, sleeps, _calls = run(lambda: seen.append(1))
    assert len(seen) == 6 and sleeps == [60.0] * 6               # a tick per renewal; no hint
    # not after a renewal that failed (the lease is not known to be held)
    seen.clear()
    run(lambda: seen.append(1), renew_fails=(2, 3))
    assert len(seen) == 4
    # the answer shortens the next wait, never lengthens it, never below the floor
    hints = iter([5.0, 1e9, 0.001, 0.0, -3.0, float("nan"), True, "7", None, float("inf")])
    _a, sleeps, _c = run(lambda: ro.Tick(next_in=next(hints)), limit=10)
    assert sleeps == [5.0, 60.0, ro.MIN_PERIOD_S, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0]
    # a fault of the policy does not end the hold; the next renewal ticks again
    count = {"n": 0}

    def flaky():
        count["n"] += 1
        raise RuntimeError("policy bug")

    _a, sleeps, _c = run(flaky, limit=4)
    assert count["n"] == 4 and len(sleeps) == 4
    # stop: the lease went to a successor; it is neither renewed again nor given back
    auth = MasterAuthority(tmp_path / "stop", "pred")
    auth.acquire()
    result = ro.hold_lease(auth, interval=60.0, sleep=lambda s: 1 / 0, monotonic=lambda: 0.0,
                           on_tick=lambda: ro.Tick(stop=True))
    assert (result.reason, result.renewals, result.released) == ("rotated", 1, False)
    assert auth.read_lease().released_at is None
    # the policy is optional: the loop without it is the one that was
    auth = MasterAuthority(tmp_path / "plain", "pred")
    auth.acquire()
    ticks = iter([False, True])
    done = ro.hold_lease(auth, interval=60.0, watch_pid=1, alive=lambda pid: next(ticks),
                         sleep=lambda s: None, monotonic=lambda: 0.0)
    assert done.reason == "watched session ended" and done.released


# --------------------------------------------------------------------------- #
# G -- the pointer carries the policy
# --------------------------------------------------------------------------- #


def test_the_pointer_carries_extra_words_quoted_before_the_watch_pid(tmp_path):
    base = dict(root="/r", project_id="p", capsule_file="/r/c.json", capsule_digest="d" * 64,
                session="succ-1", token_out="/r/t")
    plain = ro.pointer_prompt(**base)
    assert "--rotate-policy" not in plain
    prompt = ro.pointer_prompt(**base, extra_args=("--rotate-policy", "/odd dir/policy $x.json"))
    [line] = [ln for ln in prompt.splitlines() if ln.startswith("  ") and "rollover-accept" in ln]
    command = line.strip()
    assert command.endswith(WATCH_TAIL)
    argv = shlex.split(command.removesuffix(WATCH_TAIL)) + ["--watch-pid", "4242"]
    assert argv[-4:-2] == ["--rotate-policy", "/odd dir/policy $x.json"]   # one word, intact
    assert argv.index("--token-out") < argv.index("--rotate-policy") < argv.index("--watch-pid")
    args = cli.build_parser().parse_args(argv[argv.index("--root"):])
    assert args.rotate_policy == "/odd dir/policy $x.json" and args.watch_pid == 4242
    # still a pointer: ASCII, short
    with pytest.raises(ro.RolloverError):
        ro.pointer_prompt(**base, extra_args=("--rotate-policy", "/café/p.json"))
    with pytest.raises(ro.RolloverError):
        ro.pointer_prompt(**base, extra_args=("--rotate-policy", "/" + "x" * 2600))
    # run_rollover hands them to the pointer it sends
    sources = SimpleNamespace(prompt=None)

    class Capture:
        def start(self, *, session_id, model, cwd, prompt, label):
            sources.prompt = prompt
            raise ro.LauncherError("stop here")

        def stop(self, handle):
            pass

    scene = Scene(tmp_path)
    repo = scene.repo = make_repo(tmp_path)
    store = __import__("hoh.projectstore", fromlist=["x"]).ProjectStore(tmp_path / "root", "p")
    from hoh.project import ProjectState
    from hoh.succession_capsule import CapsuleInput, LocalObserver
    store.create(ProjectState(project_id="p", repo_path=str(repo)))
    master = MasterAuthority(store.dir, "pred")
    master.acquire()
    inp = CapsuleInput.model_validate({"successor_model": "m", "derive": [
        {"entry_id": "state", "section": "REGISTER", "deriver": "file_digest",
         "params": {"path": str(store.state_path)}},
        {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
         "params": {"runs_root": str(tmp_path / "runs")}},
        {"entry_id": "head", "section": "CANDIDATES", "deriver": "ref_state",
         "params": {"repo": str(repo), "ref": "main"}}]})
    result = ro.run_rollover(master, store.dir, inp=inp, observer=LocalObserver(),
                             launcher=Capture(), root=str(store.root), repo=str(repo),
                             project_id="p", successor_session="succ-x",
                             accept_args=("--rotate-policy", "/pol.json"))
    assert result.outcome is Outcome.LAUNCH_FAILED and "--rotate-policy /pol.json" in sources.prompt


# --------------------------------------------------------------------------- #
# Q -- the governed record is a product command
# --------------------------------------------------------------------------- #


def test_project_create_makes_the_governed_record_once_and_refuses_what_it_should_not(
        tmp_path, capsys):
    repo = make_repo(tmp_path)
    root = tmp_path / "hohroot"

    def create(*a) -> int:
        return cli.main(["--root", str(root), "project", "create", *a])

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert create("p", "--repo", str(repo)) == 0
    first = json.loads(capsys.readouterr().out)
    state_path = root / "projects" / "p" / "project.json"
    assert first["created"] is True and Path(first["state"]) == state_path
    assert (first["nodes"], first["program_generation"]) == (0, 1)
    assert first["repo_path"] == str(repo.resolve())
    st = ProjectStore(root, "p").read_state()
    assert (st.project_id, st.repo_path, st.master_epoch) == ("p", str(repo.resolve()), 0)
    # only the record and the lock beside it were made; the repository is untouched
    made = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert set(made) <= {"projects", "projects/p", "projects/p/project.json",
                         "projects/p/.lock"}, made
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True, check=True).stdout == ""
    # idempotent: reported, never replaced
    before = state_path.read_bytes()
    assert create("p", "--repo", str(repo)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] is False and second["write_seq"] == first["write_seq"]
    assert state_path.read_bytes() == before
    # another repository for an existing project: refused, nothing changed
    other = make_repo(tmp_path, "other")
    assert create("p", "--repo", str(other)) == 2
    err = capsys.readouterr().err
    assert "already exists for the repository" in err and str(repo.resolve()) in err
    assert state_path.read_bytes() == before
    # not a checkout: a plain directory, a missing one, a file -- and nothing is created
    plain = tmp_path / "plain"
    plain.mkdir()
    afile = tmp_path / "afile"
    afile.write_text("x")
    for bad in (plain, tmp_path / "missing", afile):
        assert create("q", "--repo", str(bad)) == 2, bad
        assert "refused:" in capsys.readouterr().err
    assert not (root / "projects" / "q").exists()
    # a project id is a name
    for bad_id in ("", ".", "..", "a/b", "../x", "/abs", "_x", ".hidden", "x" * 65,
                   "x\n", "p\n", "x\r", "x\u2028", "\u0661", "x y", "x\x00y"):
        assert create(bad_id, "--repo", str(repo)) == 2, bad_id
        assert capsys.readouterr().err.startswith("refused:"), bad_id
    assert sorted(p.name for p in (root / "projects").iterdir()) == ["p"]
    # a damaged record is reported as unreadable, not replaced
    state_path.write_text("{ torn")
    assert create("p", "--repo", str(repo)) == 3
    assert state_path.read_text() == "{ torn"
    capsys.readouterr()
    state_path.write_bytes(before)
    # and it is what `hold` takes: epoch 1, the first generation
    assert cli.main(["--root", str(root), "project", "hold", "p", "--session", "s1",
                     "--hold-for", "0.2", "--interval", "0.05"]) == 0
    assert '"epoch": 1' in capsys.readouterr().out
    assert [(e.epoch, e.master_id, e.reason) for e in EpochStore(root / "projects" / "p").chain()
            ] == [(1, "s1", Reason.INITIAL)]
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip() == head


# --------------------------------------------------------------------------- #
# W -- the cli's own wiring of the controller (the Layer-A protocol is a spy here)
# --------------------------------------------------------------------------- #


class Wired:
    """`cli._rotation_tick` around a real project store and a real master, on the real clock, with
    `run_rollover` replaced by a spy that records what the cli handed it and answers `answer`
    (or runs `act`)."""

    def __init__(self, tmp_path: Path, monkeypatch, *, outcome: Outcome = Outcome.COMPLETE,
                 ttl: int = 1800, act=None) -> None:
        self.scene = Scene(tmp_path, tokens=20_000)
        self.scene.repo = make_repo(tmp_path)
        self.root = tmp_path / "hohroot"
        assert cli.main(["--root", str(self.root), "project", "create", "p", "--repo",
                         str(self.scene.repo)]) == 0
        self.store = ProjectStore(self.root, "p")
        self.scene.dir = self.store.dir
        self.scene.write_policy()
        self.loaded = rotation.load_policy(self.scene.policy_file, directory=self.store.dir)
        self.authority = MasterAuthority(self.store.dir, "pred", ttl_seconds=ttl,
                                         serialize=lambda: write_lock(self.store))
        self.authority.acquire(program_generation=1)
        self.token_out = tmp_path / "pred.token"
        self.forwarded: dict = {}
        self.outcome = outcome
        self.act = act
        monkeypatch.setattr(ro, "run_rollover", self._run)

    def _run(self, authority, directory, **kw):
        self.forwarded.update(kw)
        if self.act is not None:
            self.act(authority)
        return RolloverResult(self.outcome, capsule_id="rc-wired", predecessor_is_master=False,
                              detail="a spy", successor_epoch=2)

    def drive(self, **kw):
        """Ask, answer as the master would, and let the controller perform; returns the last tick."""
        tick = cli._rotation_tick(self.store, self.authority, self.loaded, str(self.token_out), **kw)
        assert tick().next_in == self.loaded.policy.poll_s                 # the request
        self.scene.write_handoff(at=time.time() + 1)                       # the master's answer
        return tick()


def test_the_cli_gives_the_protocol_the_programs_real_safe_point(tmp_path, monkeypatch, herdr_stub):
    """A rotation must not start while a write of the program is in flight: the protocol asks
    `safe()` and the cli's answer is the program's write lock."""
    wired = Wired(tmp_path, monkeypatch)
    assert wired.drive().stop is True and wired.scene.events()[-1] == "rotation_finished"
    safe = wired.forwarded["safe"]
    assert safe() is True
    with ProjectStore(wired.root, "p").lock():                       # a write in flight
        assert safe() is False
    assert safe() is True


def test_the_cli_hands_the_masters_token_back_when_a_rotation_ends_in_a_reclaim(
        tmp_path, monkeypatch, herdr_stub):
    """RECLAIMED means this session took the program back at a fresh epoch, and its token file
    still holds the old token: the master must be able to act, so the file is rewritten (the old
    one kept under a versioned name). Any other outcome leaves it alone."""
    def reclaim(authority):
        authority.token = "token-of-the-reclaimed-epoch"

    wired = Wired(tmp_path / "reclaimed", monkeypatch, outcome=Outcome.RECLAIMED, act=reclaim)
    wired.token_out.write_text("token-of-the-old-epoch\n")
    assert wired.drive().stop is False
    assert wired.token_out.read_text() == "token-of-the-reclaimed-epoch"
    assert stat.S_IMODE(wired.token_out.stat().st_mode) == 0o600
    kept = sorted(wired.token_out.parent.glob("pred.token.v*"))
    assert [k.read_text() for k in kept] == ["token-of-the-old-epoch\n"]
    assert wired.scene.events()[-1] == "rotation_finished"
    # the control: a refused rotation does not touch the file
    other = Wired(tmp_path / "refused", monkeypatch, outcome=Outcome.REFUSED, act=reclaim)
    other.token_out.write_text("token-of-the-old-epoch\n")
    assert other.drive().stop is False
    assert other.token_out.read_text() == "token-of-the-old-epoch\n"


def test_the_master_keeps_its_lease_while_the_successor_is_being_started(
        tmp_path, monkeypatch, herdr_stub):
    """Starting the successor (`herdr` calls that retry, a prompt delay) can take longer than the
    lease lasts, and the hold loop cannot renew while it waits: the rotation runs under the
    lease's keepalive, or the master would lose its lease to its own rotation (SUPERSEDED)."""
    slept = []

    def slow_launch(authority):
        time.sleep(3.7)                    # longer than the whole lease (ttl 3 s)
        slept.append(authority.renew())    # StaleMaster here is the lease having lapsed meanwhile

    wired = Wired(tmp_path, monkeypatch, ttl=3, act=slow_launch)
    tick = wired.drive(keepalive_s=0.1)
    assert tick.stop is True and len(slept) == 1
    assert "rotation_error" not in wired.scene.events()
    assert wired.scene.events()[-1] == "rotation_finished"


def test_a_closed_stdout_never_fails_a_request_or_ends_a_hold(tmp_path, monkeypatch):
    """The operator-facing channel is best effort: the row is in the log before it is shown,
    and a pipe whose reader went away must not turn every request into a fault."""
    row = {"event": "rotation_armed", "at": "2027-01-15T08:00:00Z"}
    closed = open(os.devnull, "w")
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)
    cli._rotation_event(row)                                         # ValueError: closed file
    reader, writer = os.pipe()
    os.close(reader)
    broken = os.fdopen(writer, "w")
    monkeypatch.setattr(sys, "stdout", broken)
    cli._rotation_event(row)                                         # BrokenPipeError
    scene = Scene(tmp_path)
    ctl = scene.controller(Performer(), emit=cli._rotation_event)
    ctl.arm()
    assert ctl.tick().next_in == 0.05
    assert scene.events() == ["rotation_armed", "rotation_requested"]    # no rotation_error
    monkeypatch.undo()
    try:
        broken.close()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# E -- the real caller: `hoh project hold` rotates the session
# --------------------------------------------------------------------------- #


class ThreadLauncher:
    """Starts the successor by running the pointer's own command line, in this process on a
    thread, through the real command parser (as `tests/test_rollover.py` does)."""

    instances: list["ThreadLauncher"] = []

    def __init__(self, *a, **k) -> None:
        ThreadLauncher.instances.append(self)
        self.kwargs = k
        self.rc = None
        self.argv: list[str] = []
        self.prompt = ""
        self.thread: threading.Thread | None = None

    def start(self, *, session_id, model, cwd, prompt, label):
        self.prompt = prompt
        [line] = [ln for ln in prompt.splitlines() if ln.startswith("  ") and "rollover-accept" in ln]
        command = line.strip()
        assert command.endswith(WATCH_TAIL), command
        argv = shlex.split(command.removesuffix(WATCH_TAIL)) + ["--watch-pid", str(os.getpid())]
        argv = argv[argv.index("--root"):]
        cli.build_parser().parse_args(argv)                    # the pointer's own line is valid
        self.argv = argv + ["--poll", "0.05", "--hold-for", "60", "--interval", "0.05"]
        self.thread = threading.Thread(
            target=lambda: setattr(self, "rc", cli.main(self.argv)), daemon=True)
        self.thread.start()
        return SimpleNamespace(tab_id=None)

    def stop(self, handle) -> None:
        pass


def wait_for(condition, what: str, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def log_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_hold_with_a_policy_rotates_the_session_end_to_end_through_the_real_command(
        tmp_path, monkeypatch, capsys, herdr_stub):
    scene = Scene(tmp_path, tokens=262_144)
    scene.repo = make_repo(tmp_path)
    root = tmp_path / "hohroot"
    assert cli.main(["--root", str(root), "project", "create", "p", "--repo",
                     str(scene.repo)]) == 0
    store = ProjectStore(root, "p")
    scene.dir = store.dir                           # (where this scene's log is read, and its request id)
    ThreadLauncher.instances.clear()
    monkeypatch.setattr(ro, "HerdrLauncher", ThreadLauncher)
    ticks: list[str] = []
    real_tick = rotation.RotationController.tick

    def spy(self):
        ticks.append(self.session)
        return real_tick(self)

    monkeypatch.setattr(rotation.RotationController, "tick", spy)
    # numbers that are not the defaults, so that a value not forwarded shows
    scene.write_policy(threshold_tokens=250_000, ack_timeout_s=45, takeover_s=30,
                       prompt_delay_s=7.0, successor_effort="high")
    forwarded: dict = {}
    real_run = ro.run_rollover
    log = store.dir / "rotation" / "policy.log.jsonl"
    held_back: dict = {}

    def armed_rows() -> int:
        return sum(r["event"] == "rotation_armed" for r in log_events(log))

    def spy_run(*a, **k):
        forwarded.update(k)
        result = real_run(*a, **k)
        if result.outcome is Outcome.COMPLETE:
            # The predecessor learns that the rotation is complete only after the successor holds
            # the lease, and writes the closing row of the rotation after that -- and the successor
            # arms its own controller at about the same time. Which comes first is a race; this
            # forces the order in which the successor must NOT rely on the log: the closing row is
            # held back until the successor's controller has armed.
            wait_for(lambda: armed_rows() == 2, "the successor's controller to arm")
            held_back["rows"] = log_events(log)
        return result

    monkeypatch.setattr(ro, "run_rollover", spy_run)
    out: dict = {}
    token = tmp_path / "pred.token"
    hold = threading.Thread(target=lambda: out.update(rc=cli.main([
        "--root", str(root), "project", "hold", "p", "--session", "pred", "--interval", "0.05",
        "--hold-for", "120", "--token-out", str(token), "--watch-pid", str(os.getpid()),
        f"--rotate-policy={scene.policy_file}"])), daemon=True)
    hold.start()
    wait_for(lambda: any(r["event"] == "rotation_requested" for r in log_events(log)),
             "the request for a handoff")
    time.sleep(0.2)                         # the master takes a moment, then writes it
    written = scene.write_handoff("strand table: LB next\n").encode()
    hold.join(60)
    assert not hold.is_alive() and out["rc"] == 0
    text = capsys.readouterr().out
    # the operator-facing channel: one event per line, whole
    shown = [json.loads(ln) for ln in text.splitlines() if ln.startswith('{"')]
    mine = [r["event"] for r in shown if r["session"] == "pred"]
    assert mine == ["rotation_armed", "rotation_requested", "rotation_started",
                    "rotation_finished"], text
    [req] = [r for r in shown if r["event"] == "rotation_requested" and r["session"] == "pred"]
    assert (req["context_tokens"], req["threshold"]) == (262_144, 250_000)
    assert req["handoff"] == str(scene.handoff) and "Write your handoff" in req["instruction"]
    [fin] = [r for r in shown if r["event"] == "rotation_finished" and r["session"] == "pred"]
    assert fin["outcome"] == "COMPLETE" and fin["successor_epoch"] == 2
    assert '"ended": "rotated"' in text and '"released": false' in text
    assert all(r["event"] != "rotation_error" for r in shown)
    # the protocol ran: the epoch chain, the capsule, the pointer that carried the policy
    chain = EpochStore(store.dir).chain()
    assert [(e.epoch, e.reason) for e in chain] == [(1, Reason.INITIAL), (2, Reason.SUCCESSION)]
    assert chain[0].master_id == "pred" and chain[1].master_id.startswith("succ-")
    [launcher] = ThreadLauncher.instances
    assert launcher.kwargs == {"agent_args": ("--permission-mode", "auto", "--effort", "high"),
                               "prompt_delay_s": 7.0}
    assert launcher.argv[launcher.argv.index("--rotate-policy") + 1] == str(scene.policy_file)
    policy_sha = hashlib.sha256(scene.policy_file.read_bytes()).hexdigest()
    assert launcher.argv[launcher.argv.index("--rotate-policy-sha256") + 1] == policy_sha
    assert len(launcher.prompt) < 2500                      # (the pointer stays a pointer)
    assert forwarded["poll_s"] == 0.05 and forwarded["generation"] == 1
    assert (forwarded["ack_timeout_s"], forwarded["takeover_s"]) == (45, 30)
    assert forwarded["accept_args"] == ("--rotate-policy", str(scene.policy_file),
                                        "--rotate-policy-sha256", policy_sha)
    assert callable(forwarded["trigger"]) and callable(forwarded["safe"])
    assert forwarded["successor_session"].startswith("succ-")
    assert forwarded["project_id"] == "p" and forwarded["repo"] == str(scene.repo.resolve())
    [capsule] = (store.dir / "rollover").glob("*.capsule.json")
    cap = load_capsule(capsule)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    assert (time.mktime(time.strptime(cap.ack_deadline, fmt))
            - time.mktime(time.strptime(cap.created_at, fmt))) == 45
    [acked] = [r for r in ro.RolloverLog(store.dir).events() if r["event"] == "ack"]
    assert (time.mktime(time.strptime(acked["expires_at"], fmt))
            - time.mktime(time.strptime(acked["at"], fmt))) == 30 + ro._GRACE_S
    [handoff] = [e for e in cap.entries if e.entry_id == "handoff"]
    copy = Path(handoff.params["path"])
    assert copy.parent == store.dir / "rotation" / "handoffs" and copy.name.startswith("rq-")
    assert copy.read_bytes() == written
    assert handoff.value == "sha256:" + hashlib.sha256(copy.read_bytes()).hexdigest()
    scene.write_handoff("the master kept writing\n", named=False)              # (after the freeze)
    assert copy.read_bytes() == written
    # the successor applied the same policy, in its own turn, and did not rotate again at once --
    # although the predecessor's closing row was not yet in the log when the successor armed, so
    # that only a cool-down of the successor's own can have held it back (M13)
    assert [r["event"] for r in held_back["rows"] if r["event"] != "rotation_handoff_waiting"] == [
        "rotation_armed", "rotation_requested", "rotation_handoff_ready", "rotation_started",
        "rotation_armed"]
    rows = log_events(log)
    armed = [r for r in rows if r["event"] == "rotation_armed"]
    assert [r["session"] for r in armed] == ["pred", chain[1].master_id]
    assert armed[0]["cooldown_until"] is None
    assert armed[1]["cooldown_until"], "the successor armed without a cool-down of its own"
    cooldown = (rotation._epoch(armed[1]["cooldown_until"]) - rotation._epoch(armed[1]["at"]))
    assert 3599 <= cooldown <= 3601, armed[1]                    # cooldown_s of the policy: 3600
    time.sleep(0.5)                                              # many ticks of the successor
    rows = log_events(log)
    assert [r["event"] for r in rows].count("rotation_requested") == 1
    assert [r["session"] for r in rows if r["event"] == "rotation_requested"] == ["pred"]
    assert "pred" in ticks and chain[1].master_id in ticks
    # fence the successor so its thread ends
    wait_for(lambda: list((store.dir / "rollover").glob("*.cwd/master.token")), "its token")
    [token_file] = (store.dir / "rollover").glob("*.cwd/master.token")
    assert cli.main(["--root", str(root), "project", "handoff", "p", "--session",
                     chain[1].master_id, "--epoch", "2", "--token-file", str(token_file),
                     "--to", "next", "--reason", "test over"]) == 0
    launcher.thread.join(30)
    assert not launcher.thread.is_alive() and launcher.rc == 0


def test_a_policy_that_cannot_be_applied_is_refused_before_any_lease_is_taken(
        tmp_path, monkeypatch, capsys, herdr_stub):
    scene = Scene(tmp_path, tokens=9_999)
    scene.repo = make_repo(tmp_path)
    root = tmp_path / "hohroot"
    assert cli.main(["--root", str(root), "project", "create", "p", "--repo",
                     str(scene.repo)]) == 0
    capsys.readouterr()
    store = ProjectStore(root, "p")

    def hold(policy: Path, *, watch: bool = True) -> int:
        return cli.main(["--root", str(root), "project", "hold", "p", "--session", "s1",
                         "--hold-for", "0.3", "--interval", "0.05",
                         *(["--watch-pid", str(os.getpid())] if watch else []),
                         "--rotate-policy", str(policy)])

    def untouched() -> bool:
        return not (store.dir / "epochs.json").exists() and not (store.dir / "lease.json").exists()

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    for name, content in [("torn", "{ torn"), ("array", "[]"), ("empty", ""),
                          ("unknown", json.dumps({**scene.policy_dict(), "surprise": 1})),
                          ("threshold", json.dumps(scene.policy_dict(threshold_tokens=1)))]:
        f = bad_dir / f"{name}.json"
        f.write_text(content)
        assert hold(f) == 2, name
        assert capsys.readouterr().err.startswith("refused:"), name
        assert untouched(), name
    assert hold(bad_dir) == 2                               # a directory
    assert hold(tmp_path / "missing.json") == 2             # nothing there
    no_handoff = scene.template_dict()
    no_handoff["derive"].pop(0)
    scene.write_template(no_handoff)                        # the vacuous input: no narrative
    assert hold(scene.policy_file) == 2
    assert "handoff" in capsys.readouterr().err and untouched()
    # the handoff is a place only the master writes: the project's own directory is refused, and the
    # master's transcript (the cli compares them before it takes a lease)
    for place in (store.dir / "rotation" / "policy.log.jsonl", store.dir / "project.json",
                  scene.transcript):
        in_project = scene.template_dict()
        in_project["derive"][0]["params"]["path"] = str(place)
        scene.write_template(in_project)
        assert hold(scene.policy_file) == 2, place
        assert "handoff path" in capsys.readouterr().err and untouched(), place
    scene.write_template()
    # a hold that does not watch its session cannot know when the master is gone
    assert hold(scene.policy_file, watch=False) == 2
    assert "--watch-pid" in capsys.readouterr().err and untouched()
    # outside Herdr the successor cannot be started: the policy is not armed
    monkeypatch.delenv("HERDR_ENV")
    assert hold(scene.policy_file) == 2
    assert "Herdr" in capsys.readouterr().err and untouched()
    # the successor's side: a policy it cannot apply is refused before the capsule is even read
    assert cli.main(["--root", str(root), "project", "rollover-accept", "p", "--capsule",
                     str(tmp_path / "no-capsule.json"), "--digest", "d" * 64, "--session",
                     "succ-x", "--watch-pid", str(os.getpid()),
                     "--rotate-policy", str(bad_dir / "torn.json")]) == 5
    err = capsys.readouterr().err
    assert err.startswith("refused:") and "rotation policy" in err and untouched()
    # ... and a policy file that is not the one that started the rotation (the pointer carries
    # the digest of that one): refused before the capsule is read as well
    accept = ["--root", str(root), "project", "rollover-accept", "p", "--capsule",
              str(tmp_path / "no-capsule.json"), "--digest", "d" * 64, "--session", "succ-x",
              "--watch-pid", str(os.getpid()), "--rotate-policy", str(scene.policy_file)]
    sha = hashlib.sha256(scene.policy_file.read_bytes()).hexdigest()
    other_sha = ("0" if sha[0] != "0" else "1") + sha[1:]
    assert cli.main([*accept, "--rotate-policy-sha256", other_sha]) == 5
    err = capsys.readouterr().err
    assert "not the file that started this rotation" in err and sha in err and untouched()
    for bad in (sha.upper(), sha[:63], sha + "0", "", "x" * 64, sha + "\n", " " + sha):
        assert cli.main([*accept, "--rotate-policy-sha256", bad]) == 5, bad
        assert "64 lower-case hex" in capsys.readouterr().err
    assert cli.main(["--root", str(root), "project", "rollover-accept", "p", "--capsule",
                     str(tmp_path / "no-capsule.json"), "--digest", "d" * 64, "--session",
                     "succ-x", "--rotate-policy-sha256", sha]) == 5
    assert "needs --rotate-policy" in capsys.readouterr().err
    # the control: the same policy, armed and below its threshold, holds and records that it is armed
    monkeypatch.setenv("HERDR_ENV", "1")
    # the right digest passes the policy stage of the accept (it then fails on the missing capsule)
    assert cli.main([*accept, "--rotate-policy-sha256", sha]) == 5
    err = capsys.readouterr().err
    assert "rotation policy" not in err and "capsule" in err and untouched()
    assert hold(scene.policy_file) == 0
    assert [e.epoch for e in EpochStore(store.dir).chain()] == [1]
    assert [r["event"] for r in log_events(store.dir / "rotation" / "policy.log.jsonl")] == [
        "rotation_armed"]
    # and without a policy nothing of this exists
    other = ProjectStore(root, "q")
    assert cli.main(["--root", str(root), "project", "create", "q", "--repo",
                     str(scene.repo)]) == 0
    assert cli.main(["--root", str(root), "project", "hold", "q", "--session", "s2",
                     "--hold-for", "0.2", "--interval", "0.05"]) == 0
    assert not (other.dir / "rotation").exists()


# --------------------------------------------------------------------------- #
# M, D -- the import the matrix reads, and the text that ships
# --------------------------------------------------------------------------- #


def test_the_cli_names_rotation_in_an_import_statement_and_the_matrix_reads_it():
    """The end-to-end test above is what shows the import is *called*; this is the one fact
    the capability matrix measures (an import statement in an entry point)."""
    spec = importlib.util.spec_from_file_location("capability_probe_lb",
                                                  ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_probe_lb"] = cm
    spec.loader.exec_module(cm)
    assert Path(cm.HOH).resolve() == ROOT
    graph = cm._import_graph()
    assert "src/hoh/cli.py" in graph["rotation"], sorted(graph["rotation"])


def test_the_shipped_rotation_text_names_the_record_command_and_the_automatic_protocol():
    text = (ROOT / "src" / "hoh" / "agent_docs" / "references" / "rotation.md").read_text(
        encoding="utf-8")
    flat = " ".join(text.split())
    assert "hoh project create" in flat
    assert "has no command that creates one" not in flat
    for word in ("--rotate-policy", "--watch-pid", "rotation_requested", "rotation_finished",
                 "rotation_request_expired", "rotation_withdrawn", "rotation_error",
                 "handoff", "cool-down", "first line names the request id", "digest",
                 "never one the product or the harness writes"):
        assert word in flat, word
    assert "No live succession has been run" in flat            # still true, and still said
