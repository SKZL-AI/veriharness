"""Context rotation, Layer B: a rotation after N compactions of the master's conversation.

The policy field `rotate_after_compactions`, the count of compaction boundaries in the master's
own transcript, the trigger that fires at the first of the ceiling (`threshold_tokens`) and the
count, and the session id of the rotated-out session carried into the decision log and the
capsule. Everything runs on synthetic transcripts written here under `tmp_path`: no real
transcript, Herdr, agent or `~/.agents` is touched, and the tests need no git repository of
their own.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from test_succession_capsule import World, entry

from hoh import rotation
from hoh.rollover import Outcome, RolloverResult, TriggerError, TriggerResult
from hoh.succession_capsule import CapsuleInput, EntryClass, Section

T0 = 1_800_000_000.0                                   # a fixed wall clock, 2027-01-15
SID = "0000000a-0000-4000-8000-00000000000b"          # a session id (the harness names files so)
SID_ENV = "synthetic-session-1"                        # the other spelling: whatever the env names


def usage_line(tokens: int) -> str:
    return json.dumps({"type": "assistant", "isSidechain": False, "message": {
        "model": "claude-opus-5-5",
        "usage": {"input_tokens": tokens, "output_tokens": 7,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})


def boundary(**over) -> str:
    row = {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
           "isMeta": False, "timestamp": "2027-01-15T07:00:00.000Z",
           "compactMetadata": {"trigger": "auto", "preTokens": 167_000}, "uuid": "u-1"}
    row.update(over)
    return json.dumps(row)


def write(path: Path, *lines: str | bytes) -> Path:
    data = b"".join((ln if isinstance(ln, bytes) else ln.encode("utf-8")) + b"\n"
                    for ln in lines)
    path.write_bytes(data)
    return path


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Performer:
    def __init__(self, outcome: Outcome = Outcome.COMPLETE) -> None:
        self.outcome = outcome
        self.calls: list[tuple] = []

    def __call__(self, inp, trigger):
        self.calls.append((inp, trigger))
        return RolloverResult(self.outcome, capsule_id="rc-test", detail="fake",
                              predecessor_is_master=False, successor_epoch=2)


class Scene:
    """A policy, the capsule input it names, a handoff path, a transcript, a project dir."""

    def __init__(self, tmp: Path, *, transcript_name: str = f"{SID}.jsonl", **policy) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.transcript = tmp / transcript_name
        self.handoff = tmp / "HANDOFF-CURRENT.md"
        self.template = tmp / "capsule-input.json"
        self.policy_file = tmp / "rotation-policy.json"
        self.dir = tmp / "project"
        self.dir.mkdir()
        self.template.write_text(json.dumps({
            "derive": [
                {"entry_id": "handoff", "section": "REGISTER", "deriver": "file_digest",
                 "params": {"path": str(self.handoff)}},
                {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
                 "params": {"runs_root": str(tmp / "runs")}}],
            "advisory": [{"entry_id": "adv-1", "section": "CONTEXT",
                          "text": "resume the critical path"}]}))
        self.write_policy(**policy)

    def write_policy(self, **over) -> None:
        body = {"schema_version": "veriharness.rotation-policy/1",
                "successor_model": "claude-opus-5-5", "capsule_input": str(self.template),
                "transcript": {"path": str(self.transcript)}, "threshold_tokens": 100_000,
                "poll_s": 0.05, "request_ttl_s": 60, "cooldown_s": 3600,
                "ack_timeout_s": 30, "takeover_s": 10}
        body.update(over)
        self.policy_file.write_text(json.dumps(body))

    def loaded(self):
        return rotation.load_policy(self.policy_file)

    def controller(self, perform, clock: Clock, *, trigger=None):
        loaded = self.loaded()
        return rotation.RotationController(
            loaded, directory=self.dir, project_id="p", session="pred",
            trigger=trigger or rotation.make_trigger(loaded.policy), perform=perform,
            emit=lambda row: None, wall=clock)

    def log(self) -> list[dict]:
        path = self.dir / "rotation" / "policy.log.jsonl"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []

    def write_handoff(self) -> None:
        ids = [r["request_id"] for r in self.log() if r["event"] == "rotation_requested"]
        self.handoff.write_text(f"# Handoff {ids[-1]}\nstrand table\n")
        os.utime(self.handoff, (T0 + 2, T0 + 2))


def policy_of(scene: Scene) -> rotation.RotationPolicy:
    return scene.loaded().policy


# --------------------------------------------------------------------------- #
# P -- the policy field
# --------------------------------------------------------------------------- #


def test_rotate_after_compactions_is_an_optional_bounded_integer(tmp_path):
    scene = Scene(tmp_path)
    assert policy_of(scene).rotate_after_compactions is None          # absent: today's behaviour
    assert rotation.MAX_ROTATE_AFTER_COMPACTIONS == 100
    for ok in (1, 5, 100):
        scene.write_policy(rotate_after_compactions=ok)
        assert policy_of(scene).rotate_after_compactions == ok
    scene.write_policy(rotate_after_compactions=None)
    assert policy_of(scene).rotate_after_compactions is None
    for bad in (0, -1, 101, 10_000, True, False, "5", 5.0, 4.5, [5], {"n": 5},
                float("nan")):
        scene.write_policy(rotate_after_compactions=bad)
        with pytest.raises(rotation.PolicyError):
            scene.loaded()
        assert bad is not None
    # the ceiling is still there and still bounded, and a policy that sets both loads
    scene.write_policy(rotate_after_compactions=5, threshold_tokens=1_000_000)
    both = policy_of(scene)
    assert (both.rotate_after_compactions, both.threshold_tokens) == (5, 1_000_000)
    scene.write_policy(rotate_after_compactions=5, threshold_tokens=10_000_001)
    with pytest.raises(rotation.PolicyError):
        scene.loaded()
    # the sha pin is the digest of the file's bytes, whatever the new field says
    scene.write_policy(rotate_after_compactions=5)
    loaded = scene.loaded()
    assert loaded.sha256 == hashlib.sha256(scene.policy_file.read_bytes()).hexdigest()
    before = loaded.sha256
    scene.write_policy(rotate_after_compactions=6)
    assert scene.loaded().sha256 != before


def test_a_capsule_input_may_not_use_the_entry_id_the_product_reserves(tmp_path):
    scene = Scene(tmp_path)
    assert rotation.ROTATING_SESSION_ENTRY == "rotating-session"
    base = json.loads(scene.template.read_text())
    for where, row in (
            ("advisory", {"entry_id": "rotating-session", "section": "CONTEXT", "text": "x"}),
            ("advisory", {"entry_id": "rotating-session", "section": "BACKGROUND",
                          "text": "x"}),
            ("decisions", {"entry_id": "rotating-session", "text": "t", "source": "operator",
                           "at": "2026-10-04", "status": "decided"}),
            ("derive", {"entry_id": "rotating-session", "section": "REGISTER",
                        "deriver": "file_digest", "params": {"path": str(scene.handoff)}}),
            ("derive", {"entry_id": " rotating-session ", "section": "REGISTER",   # (stripped)
                        "deriver": "file_digest", "params": {"path": str(scene.handoff)}})):
        data = json.loads(json.dumps(base))
        data.setdefault(where, []).append(row)
        scene.template.write_text(json.dumps(data))
        with pytest.raises(rotation.PolicyError, match="reserves"):
            scene.loaded()
    scene.template.write_text(json.dumps(base))
    assert scene.loaded().policy.successor_model == "claude-opus-5-5"


# --------------------------------------------------------------------------- #
# C -- counting compactions
# --------------------------------------------------------------------------- #


def test_compactions_are_the_top_level_boundary_records_and_nothing_that_mentions_one(tmp_path):
    t = tmp_path / "t.jsonl"
    mention = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": boundary()}]}}
    write(t,
          usage_line(20_000),
          boundary(),                                                    # 1
          boundary(isSidechain=True),                                    # a sub-agent: no
          json.dumps(mention),                                           # a mention: no
          boundary(type="user"),                                         # wrong type: no
          boundary(subtype="compact_boundary_x"),                        # wrong subtype: no
          boundary(subtype="Compact_Boundary"),                          # (case): no
          json.dumps({"type": "system", "subtype": "init"}),
          json.dumps([{"type": "system", "subtype": "compact_boundary"}]),   # not an object
          '{"type": "system", "subtype": "compact_boundary"',            # torn: skipped
          "plain text mentioning compact_boundary",
          boundary(compactMetadata={"trigger": "manual", "preTokens": 90_000}),   # 2: manual too
          b'{"type": "system", "subtype": "compact_boundary", "content": "\xff\xfe"}',  # not UTF-8
          '{"type": "system", "subtype": "compact_boundary", "content": "a\x00b"}',    # raw NUL
          b"[" * 300_000 + b'"compact_boundary"' + b"]" * 300_000,      # nesting too deep
          usage_line(30_000),
          boundary(),                                                    # 3
          )
    assert rotation.count_compactions(t) == 3
    # a complete record whose line has no newline yet is counted; a torn one never is
    with open(t, "ab") as fh:
        fh.write(boundary().encode("utf-8"))
    assert rotation.count_compactions(t) == 4
    with open(t, "ab") as fh:
        fh.write(b'\n{"type": "system", "subtype": "compact_boun')
    assert rotation.count_compactions(t) == 4
    write(tmp_path / "none.jsonl")
    assert rotation.count_compactions(tmp_path / "none.jsonl") == 0       # none were written


def test_only_the_literal_true_marks_a_boundary_as_a_sub_agents(tmp_path):
    t = tmp_path / "t.jsonl"
    write(t,
          boundary(isSidechain=True),                           # a sub-agent: no
          boundary(),                                           # 1 (the key absent)
          boundary(isSidechain=False), boundary(isSidechain=None),       # 2, 3
          boundary(isSidechain=0), boundary(isSidechain=1),              # 4, 5: not the literal
          boundary(isSidechain="true"), boundary(isSidechain="false"),   # 6, 7
          boundary(isSidechain=[True]), boundary(isSidechain={}))        # 8, 9
    assert rotation.count_compactions(t) == 9


def test_the_count_is_taken_up_to_the_size_the_file_had_when_it_was_opened(tmp_path,
                                                                            monkeypatch):
    lines = [boundary(), boundary(), boundary()]
    t = write(tmp_path / "t.jsonl", *lines)
    full = t.stat().st_size
    real_fstat = os.fstat

    def opened_smaller(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size == full:
            fields = list(st)
            fields[6] = 2 * (len(lines[0]) + 1)               # st_size: two lines were there
            return os.stat_result(fields)
        return st

    with monkeypatch.context() as m:
        m.setattr(rotation.os, "fstat", opened_smaller)
        assert rotation.count_compactions(t) == 2             # what came after is not read
    assert rotation.count_compactions(t) == 3


def test_a_line_over_the_cap_is_skipped_and_the_next_line_is_read(tmp_path):
    cap = rotation.MAX_BOUNDARY_LINE_BYTES
    assert cap == 1024 * 1024

    def padded(size: int) -> bytes:
        row = json.loads(boundary())
        row["content"] = ""
        row["pad"] = ""
        base = len(json.dumps(row).encode("utf-8"))
        row["pad"] = "x" * (size - base)
        raw = json.dumps(row).encode("utf-8")
        assert len(raw) == size
        return raw

    t = tmp_path / "t.jsonl"
    write(t, padded(cap), boundary())                       # exactly the cap: still a record
    assert rotation.count_compactions(t) == 2
    write(t, padded(cap + 1), boundary())                   # one over: not one, and not parsed
    assert rotation.count_compactions(t) == 1
    write(t, padded(3 * cap + 17), boundary(), padded(cap + 5), boundary())
    assert rotation.count_compactions(t) == 2


def test_a_transcript_that_cannot_be_counted_is_an_error_and_never_zero(tmp_path):
    good = write(tmp_path / "good.jsonl", usage_line(5), boundary())
    assert rotation.count_compactions(good) == 1
    size = good.stat().st_size
    assert rotation.count_compactions(good, max_bytes=size) == 1
    with pytest.raises(TriggerError, match="over the"):
        rotation.count_compactions(good, max_bytes=size - 1)
    assert rotation.MAX_COMPACTION_SCAN_BYTES == 4 * 1024 ** 3
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    dangling = tmp_path / "dangling.jsonl"
    dangling.symlink_to(tmp_path / "nowhere.jsonl")
    for bad in (tmp_path / "missing.jsonl", tmp_path, fifo, dangling):
        with pytest.raises(TriggerError):
            rotation.count_compactions(bad)
    with pytest.raises(TriggerError):
        rotation.count_compactions("bad\x00name.jsonl")
    if os.geteuid() != 0:
        locked = write(tmp_path / "locked.jsonl", boundary())
        locked.chmod(0)
        try:
            with pytest.raises(TriggerError):
                rotation.count_compactions(locked)
        finally:
            locked.chmod(0o600)
    link = tmp_path / "link.jsonl"
    link.symlink_to(good)
    assert rotation.count_compactions(link) == 1              # a link to a transcript is read


# --------------------------------------------------------------------------- #
# T -- the trigger
# --------------------------------------------------------------------------- #


def fired(scene: Scene, environ=None):
    return rotation.make_trigger(policy_of(scene), environ)()


def test_the_nth_compaction_makes_a_rotation_due_below_the_ceiling(tmp_path):
    scene = Scene(tmp_path, rotate_after_compactions=3)
    write(scene.transcript, usage_line(20_000), boundary(), boundary())
    two = fired(scene)
    assert (two.fire, two.reason, two.estimate) == (False, "context_tokens", False)
    assert (two.compactions, two.compaction_limit, two.context_tokens) == (2, 3, 20_000)
    write(scene.transcript, usage_line(20_000), boundary(), boundary(), boundary())
    three = fired(scene)
    assert (three.fire, three.reason, three.estimate) == (True, "compactions", False)
    assert (three.compactions, three.compaction_limit) == (3, 3)
    assert (three.context_tokens, three.threshold) == (20_000, 100_000)
    size = scene.transcript.stat().st_size
    assert three.size_bytes == size                      # the base's measurement is kept
    assert three.as_dict() == {
        "fire": True, "context_tokens": 20_000, "threshold": 100_000, "reason": "compactions",
        "estimate": False, "size_bytes": size, "compactions": 3, "compaction_limit": 3,
        "transcript_session_id": SID}
    # more than the limit: still due
    write(scene.transcript, usage_line(20_000), *([boundary()] * 9))
    assert fired(scene).fire is True and fired(scene).compactions == 9


def test_the_ceiling_stays_the_ceiling_and_is_decided_without_the_count(tmp_path, monkeypatch):
    scene = Scene(tmp_path, rotate_after_compactions=5)
    write(scene.transcript, usage_line(100_000))                       # at the ceiling, 0 compactions
    at = fired(scene)
    assert (at.fire, at.reason, at.compactions) == (True, "context_tokens", 0)
    write(scene.transcript, usage_line(99_999))
    assert fired(scene).fire is False
    # both at once: the ceiling names itself, the count is recorded
    write(scene.transcript, *([boundary()] * 5), usage_line(100_000))
    both = fired(scene)
    assert (both.fire, both.reason, both.compactions) == (True, "context_tokens", 5)
    # the count could not be taken: at the ceiling the rotation is still due ...
    monkeypatch.setattr(rotation, "MAX_COMPACTION_SCAN_BYTES", 10)
    over = fired(scene)
    assert (over.fire, over.reason, over.compactions) == (True, "context_tokens", None)
    # ... and below it nothing is guessed: a blind tick, not a "no"
    write(scene.transcript, *([boundary()] * 5), usage_line(5_000))
    with pytest.raises(TriggerError):
        fired(scene)


def test_without_the_field_the_compactions_decide_nothing_and_are_not_counted(tmp_path,
                                                                              monkeypatch):
    scene = Scene(tmp_path)                                            # no rotate_after_compactions
    write(scene.transcript, *([boundary()] * 50), usage_line(20_000))
    got = fired(scene)
    assert (got.fire, got.reason, got.compactions, got.compaction_limit) == (
        False, "context_tokens", None, None)
    # not even read: a transcript too large to count is no matter without the field
    monkeypatch.setattr(rotation, "MAX_COMPACTION_SCAN_BYTES", 1)
    assert fired(scene).fire is False
    write(scene.transcript, usage_line(100_000))
    assert fired(scene).fire is True


def test_a_count_is_a_measurement_and_a_size_estimate_is_not(tmp_path):
    scene = Scene(tmp_path, rotate_after_compactions=2)
    # no usable request in the transcript: the size estimate stands in, labelled one
    write(scene.transcript, boundary())
    one = fired(scene)
    assert (one.fire, one.estimate, one.reason, one.context_tokens) == (
        False, True, "size_budget", None)
    assert one.compactions == 1
    write(scene.transcript, boundary(), boundary())
    two = fired(scene)
    assert (two.fire, two.estimate, two.reason, two.context_tokens) == (
        True, False, "compactions", None)
    # a transcript over the size budget with no request in it, and too few compactions, is
    # the estimate it was (the controller never starts on one) ...
    huge = b"x" * (16 * 1024 * 1024 + 1)
    write(scene.transcript, huge, boundary())
    est = fired(scene)
    assert (est.fire, est.estimate, est.reason, est.compactions) == (True, True, "size_budget", 1)
    # ... and with the limit reached it is a measured rotation, not an estimated one
    write(scene.transcript, huge, boundary(), boundary())
    got = fired(scene)
    assert (got.fire, got.estimate, got.reason, got.compactions) == (
        True, False, "compactions", 2)


# --------------------------------------------------------------------------- #
# S -- the session id
# --------------------------------------------------------------------------- #


def test_the_session_id_is_the_name_of_the_transcript_file_or_nothing(tmp_path):
    spec_path = rotation.TranscriptSpec(path=str(tmp_path / f"{SID}.jsonl"))
    assert rotation.transcript_session_id(spec_path, tmp_path / f"{SID}.jsonl") == SID
    for name in ("transcript.jsonl", f"{SID.upper()}.jsonl", f"{SID}.json", f"{SID}.jsonl.bak",
                 "0000000a-0000-4000-8000-00000000000.jsonl", f"{SID}x.jsonl", ".jsonl",
                 f"{SID}\n.jsonl"):
        assert rotation.transcript_session_id(spec_path, tmp_path / name) is None, name
    projects = tmp_path / "projects" / "-home"
    projects.mkdir(parents=True)
    spec_env = rotation.TranscriptSpec(session_id_env="SID", projects_dir=str(tmp_path / "projects"))
    write(projects / f"{SID_ENV}.jsonl", usage_line(1))
    assert rotation.transcript_session_id(spec_env, projects / f"{SID_ENV}.jsonl") == SID_ENV
    assert rotation.transcript_session_id(spec_env, projects / "short.jsonl") is None
    # through the trigger, for both spellings of the transcript
    scene = Scene(tmp_path / "a", rotate_after_compactions=2)
    write(scene.transcript, usage_line(1))
    assert fired(scene).transcript_session_id == SID
    named = Scene(tmp_path / "b", transcript_name="transcript.jsonl")
    write(named.transcript, usage_line(1))
    assert fired(named).transcript_session_id is None
    env_policy = rotation.RotationPolicy.model_validate({
        "schema_version": "veriharness.rotation-policy/1", "successor_model": "m",
        "capsule_input": str(scene.template),
        "transcript": {"session_id_env": "SID", "projects_dir": str(tmp_path / "projects")},
        "rotate_after_compactions": 1})
    got = rotation.make_trigger(env_policy, {"SID": SID_ENV})()
    assert got.transcript_session_id == SID_ENV and got.compactions == 0
    # a link is named by its own name, never by what it points at (both directions)
    links = tmp_path / "links"
    links.mkdir()
    sid2 = "0000000b-0000-4000-8000-00000000000c"
    opaque = write(links / "opaque.jsonl", usage_line(1))
    session_named = links / f"{sid2}.jsonl"
    session_named.symlink_to(opaque)                         # a session name -> a plain file
    elsewhere = links / "elsewhere.jsonl"
    elsewhere.symlink_to(write(links / f"{SID}.jsonl", usage_line(1)))   # a plain name -> a session

    def through_trigger(link: Path):
        policy = rotation.RotationPolicy.model_validate({
            "schema_version": "veriharness.rotation-policy/1", "successor_model": "m",
            "capsule_input": str(scene.template), "transcript": {"path": str(link)}})
        return rotation.make_trigger(policy)().transcript_session_id

    assert rotation.transcript_session_id(spec_path, session_named) == sid2
    assert rotation.transcript_session_id(spec_path, elsewhere) is None
    assert through_trigger(session_named) == sid2
    assert through_trigger(elsewhere) is None


# --------------------------------------------------------------------------- #
# R -- the record and the capsule
# --------------------------------------------------------------------------- #


def test_the_request_and_the_closing_row_carry_the_compactions_and_the_session_id(tmp_path):
    scene = Scene(tmp_path, rotate_after_compactions=2)
    write(scene.transcript, usage_line(20_000), boundary(), boundary())
    clock, perform = Clock(), Performer()
    ctl = scene.controller(perform, clock)
    ctl.arm()
    [armed] = [r for r in scene.log() if r["event"] == "rotation_armed"]
    assert armed["rotate_after_compactions"] == 2 and armed["threshold_tokens"] == 100_000
    ctl.tick()
    [req] = [r for r in scene.log() if r["event"] == "rotation_requested"]
    assert req["reason"] == "compactions"
    assert (req["compactions"], req["rotate_after_compactions"]) == (2, 2)
    assert (req["context_tokens"], req["threshold"]) == (20_000, 100_000)
    assert req["transcript_session_id"] == SID
    assert perform.calls == []
    clock.advance(3)
    scene.write_handoff()
    assert ctl.tick().stop is True
    [(inp, trigger)] = perform.calls
    assert trigger().fire is True and trigger().reason == "compactions"   # the re-check agrees
    # the capsule input the protocol freezes names the session being rotated out ...
    [mine] = [a for a in inp.advisory if a.entry_id == "rotating-session"]
    assert mine.section is Section.CONTEXT
    assert rotation.rotating_session_id(mine.text) == SID
    assert req["request_id"] in mine.text
    assert "compactions counted in its transcript: 2" in mine.text
    # ... beside the operator's own advisory entries, which are untouched and first
    assert [a.entry_id for a in inp.advisory] == ["adv-1", "rotating-session"]
    assert inp.advisory[0].text == "resume the critical path"
    [fin] = [r for r in scene.log() if r["event"] == "rotation_finished"]
    assert (fin["compactions"], fin["transcript_session_id"]) == (2, SID)
    assert (fin["context_tokens"], fin["threshold"], fin["outcome"]) == (
        20_000, 100_000, "COMPLETE")


def test_a_transcript_that_names_no_session_adds_no_entry_and_a_null_to_the_rows(tmp_path):
    scene = Scene(tmp_path, transcript_name="transcript.jsonl")           # no field: ceiling only
    write(scene.transcript, usage_line(100_000))
    clock, perform = Clock(), Performer()
    ctl = scene.controller(perform, clock)
    ctl.arm()
    [armed] = [r for r in scene.log() if r["event"] == "rotation_armed"]
    assert armed["rotate_after_compactions"] is None            # absent: recorded as absent
    ctl.tick()
    [req] = [r for r in scene.log() if r["event"] == "rotation_requested"]
    assert req["reason"] == "context_tokens"
    assert (req["compactions"], req["rotate_after_compactions"],
            req["transcript_session_id"]) == (None, None, None)
    clock.advance(3)
    scene.write_handoff()
    assert ctl.tick().stop is True
    [(inp, _t)] = perform.calls
    assert [a.entry_id for a in inp.advisory] == ["adv-1"]
    # a trigger that is not this module's (a plain TriggerResult) is as before
    plain = Scene(tmp_path / "plain")
    write(plain.transcript, usage_line(1))
    ctl2 = plain.controller(
        Performer(), Clock(),
        trigger=lambda: TriggerResult(True, 5, 100_000, "context_tokens", False, 10))
    ctl2.arm()
    ctl2.tick()
    [row] = [r for r in plain.log() if r["event"] == "rotation_requested"]
    assert (row["compactions"], row["transcript_session_id"]) == (None, None)


def test_the_rotating_session_entry_freezes_and_verifies_as_advisory_context(tmp_path):
    w = World(tmp_path)
    text = rotation.rotating_session_text(SID, request_id="rq-20270115T080000Z-00000000",
                                          compactions=5)
    assert text.split("\n")[0] == f"transcript_session_id: {SID}"
    assert "compactions counted in its transcript: 5" in text
    assert "not counted" in rotation.rotating_session_text(SID, request_id="rq-x",
                                                           compactions=None)
    # the first line reads back; nothing else does
    assert rotation.rotating_session_id(text) == SID
    assert rotation.rotating_session_id(f"transcript_session_id: {SID_ENV}\nx") == SID_ENV
    for hostile in (None, 5, "", f"transcript_session_id:{SID}", f" transcript_session_id: {SID}",
                    f"transcript_session_id: {SID} ", f"transcript_session_id: {SID}\r\nx",
                    f"x\ntranscript_session_id: {SID}", "transcript_session_id: ",
                    "transcript_session_id: short", "transcript_session_id: ../../x",
                    f"Transcript_session_id: {SID}"):
        assert rotation.rotating_session_id(hostile) is None, repr(hostile)
    body = w.input().model_dump(mode="json")
    body["advisory"].append({"entry_id": rotation.ROTATING_SESSION_ENTRY,
                             "section": "CONTEXT", "text": text})
    plain = w.freeze("heir-a")
    frozen = w.freeze("heir-b", inp=CapsuleInput.model_validate(body))
    e = entry(frozen.capsule, "rotating-session")
    assert e.klass is EntryClass.ADVISORY_CONTEXT and e.section is Section.CONTEXT
    assert e.text == text
    # advisory context is bound by the capsule digest, in no section digest
    assert frozen.capsule.section_digests() == plain.capsule.section_digests()
    assert frozen.capsule.compute_digest() == frozen.capsule.capsule_digest
    assert w.verify(frozen.capsule, session="heir-b").ok
