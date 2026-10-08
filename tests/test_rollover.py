"""Context rollover, Layer A: the trigger, the state machine, the predecessor's
sequence, the successor's steps, the lease holder, the Herdr launcher and the
commands.

A rotation is driven end to end with an injected clock: the predecessor runs in
the foreground and the successor acts on every tick of its `sleep`, so a test
can say exactly where in the protocol the world changes. Fakes stand in for
Herdr, the agent and the observer; nothing here starts a process other than
`git` on a repository created under `tmp_path`, reads a transcript of the real
machine, or reads `~/.agents`. Three tests (`...rolls_over_end_to_end...`, `...stays_master_and_exits_5...` and
`hold_takes_a_free_program...`) use the real clock, for a few seconds at most.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_succession_capsule import (
    OPERATOR_TEXT, World, _git, _make_repo, entry, rewrite,
)

from hoh import cli
from hoh import rollover as ro
from hoh.epoch import EpochStore, Reason
from hoh.orchestrator import write_lock
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.rollover import (
    HerdrLauncher, HoldResult, LauncherError, LaunchHandle, Outcome, RolloverError,
    RolloverLog, State, Successor, TokenOutError, TriggerError, evaluate_trigger,
    hold_lease, pointer_prompt, run_rollover,
)
from hoh.succession import LeaseError, LeaseHeld, MasterAuthority, StaleMaster
from hoh.succession_capsule import (
    CapsuleInput, LocalObserver, ack_path, aborted_path, build_ack, capsule_path,
    freeze_capsule, load_capsule, receipt_path,
)

# --------------------------------------------------------------------------- #
# K1 -- the trigger
# --------------------------------------------------------------------------- #


def rec(inp: int = 0, cc: int = 0, cr: int = 0, out: int = 7, *, side: bool = False,
        model: str = "claude-opus-5-5", kind: str = "assistant") -> str:
    return json.dumps({"type": kind, "isSidechain": side, "message": {
        "model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": cc,
                                  "cache_read_input_tokens": cr}}})


def transcript(tmp_path: Path, *lines: str, name: str = "t.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_context_is_the_last_request_input_plus_both_cache_counts(tmp_path):
    t = transcript(tmp_path,
                   rec(inp=5, cc=10, cr=20),                    # an earlier request
                   rec(inp=10, cc=200, cr=300_000, out=99_999))  # the last one
    got = evaluate_trigger(t)
    assert got.context_tokens == 300_210                          # output is not context
    assert got.fire and got.reason == "context_tokens" and not got.estimate
    # each of the three counts is part of it
    assert evaluate_trigger(transcript(tmp_path, rec(inp=7), name="a")).context_tokens == 7
    assert evaluate_trigger(transcript(tmp_path, rec(cc=8, inp=1), name="b")).context_tokens == 9
    assert evaluate_trigger(transcript(tmp_path, rec(cr=9, inp=1), name="c")).context_tokens == 10
    assert ro.DEFAULT_THRESHOLD == 250_000


def test_sidechain_synthetic_and_unmeasured_lines_are_not_the_context(tmp_path):
    real = rec(inp=100, cc=50_000, cr=60_000)                    # 110,100
    noise = [
        rec(inp=900_000, side=True),                              # a sub-agent
        rec(inp=900_000, model="<synthetic>"),                    # a synthetic record
        rec(inp=900_000, kind="user"),                            # not an assistant record
        rec(inp=0, cc=0, cr=0),                                   # unmeasured: all zero
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": "9e5"}}}),
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1.5}}}),
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": True}}}),
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": -5}}}),
        json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 9e5}}}),
        json.dumps({"type": "assistant", "message": {"usage": None}}),
        json.dumps({"type": "assistant", "message": "text"}),
    ]
    got = evaluate_trigger(transcript(tmp_path, real, *noise))
    assert got.context_tokens == 110_100 and not got.fire        # the real request, not the noise
    only_noise = evaluate_trigger(transcript(tmp_path, *noise, name="n"))
    assert only_noise.context_tokens is None and only_noise.estimate


def test_a_torn_last_line_and_garbage_are_skipped_not_guessed(tmp_path):
    t = tmp_path / "torn.jsonl"
    t.write_text("\n".join([
        rec(inp=1, cc=2, cr=3),
        rec(inp=250_000),
        "not json at all",
        "[1, 2, 3]",
        '"a string"',
        "",
        '{"type": "assistant", "message": {"usage": {"input_tokens": 9999999',   # torn tail
    ]), encoding="utf-8")
    got = evaluate_trigger(t)
    assert got.context_tokens == 250_000 and got.fire            # the last whole record


def test_only_the_tail_of_a_large_transcript_is_read(tmp_path):
    filler = ["x" * 99] * 80
    t = transcript(tmp_path, rec(inp=300_000), *filler)           # the only record is far back
    assert t.stat().st_size > 8000
    tail = evaluate_trigger(t, max_tail_bytes=1024)
    assert tail.context_tokens is None                            # it was never read ...
    assert tail.reason == "size_budget" and tail.estimate
    whole = evaluate_trigger(t, max_tail_bytes=1 << 20)
    assert whole.context_tokens == 300_000 and not whole.estimate  # ... but is there to find
    # a window that opens mid-line does not mistake the cut-off half for a record
    t2 = transcript(tmp_path, rec(inp=10), "y" * 300, rec(inp=20), name="two.jsonl")
    assert evaluate_trigger(t2, max_tail_bytes=400).context_tokens == 20
    assert ro.MAX_TAIL_BYTES == 8 * 1024 * 1024


def test_the_threshold_fires_at_it_and_not_below(tmp_path):
    at = evaluate_trigger(transcript(tmp_path, rec(inp=250_000), name="at"))
    below = evaluate_trigger(transcript(tmp_path, rec(inp=249_999), name="below"))
    assert at.fire and not below.fire
    assert (at.threshold, below.threshold) == (250_000, 250_000)
    t = transcript(tmp_path, rec(inp=1000), name="custom")
    assert evaluate_trigger(t, threshold=1000).fire
    assert not evaluate_trigger(t, threshold=1001).fire
    assert evaluate_trigger(t).as_dict()["context_tokens"] == 1000


def test_after_a_compaction_the_trigger_is_off_again(tmp_path):
    t = transcript(tmp_path, rec(inp=10, cr=400_000), rec(inp=10, cr=500_000),
                   rec(inp=10, cc=119_990))                       # the conversation was compacted
    got = evaluate_trigger(t)
    assert got.context_tokens == 120_000 and not got.fire


def test_without_a_usable_record_the_size_budget_decides_and_says_so(tmp_path):
    empty = transcript(tmp_path, "", name="empty")
    small = evaluate_trigger(empty)
    assert not small.fire and small.estimate and small.reason == "size_budget"
    assert small.context_tokens is None
    big = transcript(tmp_path, *["{}"] * 2000, name="big")
    over = evaluate_trigger(big, size_budget_bytes=1000)
    assert over.fire and over.estimate and over.reason == "size_budget"
    assert not evaluate_trigger(big, size_budget_bytes=10_000_000).fire
    assert ro.DEFAULT_SIZE_BUDGET_BYTES > ro.MAX_TAIL_BYTES


def test_the_size_budget_is_ignored_when_the_requests_can_be_read(tmp_path):
    t = transcript(tmp_path, *["{}"] * 2000, rec(inp=5), name="big")
    assert t.stat().st_size > 1000
    got = evaluate_trigger(t, size_budget_bytes=1000)
    assert not got.fire and not got.estimate and got.reason == "context_tokens"
    assert got.context_tokens == 5


def test_an_unreadable_transcript_is_an_error_and_never_a_pass(tmp_path):
    with pytest.raises(TriggerError):
        evaluate_trigger(tmp_path / "missing.jsonl")
    with pytest.raises(TriggerError):
        evaluate_trigger(tmp_path)                                # a directory
    w = World(tmp_path / "w")
    launcher = FakeLauncher()
    with pytest.raises(TriggerError):                             # and a rotation does not go on
        run_rollover(w.pred, w.dir, inp=w.input(), observer=w.observer, launcher=launcher,
                     root=w.root, repo=w.repo, project_id="p", successor_session="succ",
                     trigger=lambda: evaluate_trigger(tmp_path / "missing.jsonl"))
    assert not launcher.started


def test_below_the_threshold_nothing_is_done(tmp_path):
    w = World(tmp_path)
    t = transcript(tmp_path, rec(inp=1000, cr=2000), name="small")
    rot = Rotation(w)
    result = rot.go(trigger=lambda: evaluate_trigger(t))
    assert result.outcome is Outcome.NOT_TRIGGERED and result.predecessor_is_master
    assert not rot.launcher.started and not (w.dir / "rollover").exists()
    assert w.pred.problems() == [] and RolloverLog(w.dir).events() == []
    big = transcript(tmp_path, rec(inp=1000, cr=300_000), name="big")
    again = Rotation(w).go(trigger=lambda: evaluate_trigger(big))
    assert again.outcome is Outcome.COMPLETE                      # and over it, it goes on


# --------------------------------------------------------------------------- #
# the harness: a fake launcher and a successor that acts on every tick
# --------------------------------------------------------------------------- #


class FakeLauncher:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.started: list[dict] = []
        self.stopped: list = []

    def start(self, *, session_id, model, cwd, prompt, label):
        if self.fail:
            raise LauncherError(self.fail)
        self.started.append({"session_id": session_id, "model": model, "cwd": Path(cwd),
                             "prompt": prompt, "label": label,
                             "cwd_entries": sorted(os.listdir(cwd))})
        return LaunchHandle(agent=f"agent-{session_id}", pane_id="pane-1", tab_id="tab-1")

    def stop(self, handle) -> None:
        self.stopped.append(handle)


class Rotation:
    """One rotation under test. The predecessor runs in the foreground; every
    `sleep` advances the clock and lets the successor act. `hooks` change the
    world at a named phase ("launched": started, no ack yet; "handed": the
    lease is handed to the successor, who has not taken it)."""

    def __init__(self, w: World, *, successor: str = "succ", takeover_s: int = 120,
                 ack_timeout_s: int = 600, poll_s: float = 10.0,
                 launcher: FakeLauncher | None = None, acks: bool = True, takes: bool = True,
                 ack_after: int = 0, succ_pid: int | None = None, hooks: dict | None = None,
                 inp: CapsuleInput | None = None) -> None:
        self.w = w
        self.successor = successor
        self.takeover_s = takeover_s
        self.ack_timeout_s = ack_timeout_s
        self.poll_s = poll_s
        self.launcher = launcher or FakeLauncher()
        self.acks, self.takes, self.ack_after = acks, takes, ack_after
        self.hooks = hooks or {}
        self.fired: set[str] = set()
        self.inp = inp
        self.succ_auth = w.authority(successor, pid=succ_pid)
        self.extra: list[MasterAuthority] = []
        self.succ: Successor | None = None
        self.ack: dict | None = None
        self.taken = None
        self.take_tried = False
        self.errors: list[Exception] = []
        self.ticks = 0
        self.max_masters = 0
        self.lease_live_at_each_tick: list[bool] = []
        self.reads: list[str] = []

    def capsule_id(self) -> str:
        """The capsule this rotation's successor was started for (not merely the
        newest file: two cycles in one second have names that do not sort by time)."""
        if self.launcher.started:
            return self.launcher.started[-1]["cwd"].name.removesuffix(".cwd")
        return sorted((self.w.dir / "rollover").glob("*.capsule.json"))[-1].name.removesuffix(
            ".capsule.json")

    def capsule_file(self) -> Path:
        return capsule_path(self.w.dir, self.capsule_id())

    def phase(self) -> str:
        if not self.launcher.started:
            return "start"
        if not ack_path(self.w.dir, self.capsule_id()).exists():
            return "launched"
        lease = self.w.pred.read_lease()
        if lease is not None and lease.released_at and lease.successor == self.successor \
                and not self.take_tried:
            return "handed"
        return "other"

    def sleep(self, seconds: float) -> None:
        self.w.clock.advance(seconds)
        self.ticks += 1
        live = [h for h in [self.w.pred, self.succ_auth, *self.extra] if h.problems() == []]
        self.max_masters = max(self.max_masters, len(live))
        lease = self.w.pred.read_lease()
        self.lease_live_at_each_tick.append(
            lease is not None and not lease.released_at and lease.expires_at > self.w.clock())
        phase = self.phase()
        if phase in self.hooks and phase not in self.fired:
            self.fired.add(phase)
            self.hooks[phase](self)
            phase = self.phase()
        if phase == "launched" and self.acks and self.ticks > self.ack_after:
            self._successor().acknowledge_into(self)
        elif phase == "handed" and self.takes:
            self.take_tried = True
            try:
                self.taken = self._successor().take(self.ack, token_out=None)
            except RolloverError as exc:
                self.errors.append(exc)

    def _successor(self) -> "_Actor":
        if self.succ is None:
            cap = load_capsule(self.capsule_file())
            self.succ = Successor(self.succ_auth, self.capsule_file(), observer=self.w.observer,
                                  pointer_digest=cap.capsule_digest,
                                  sleep=self.w.clock.advance, poll_s=self.poll_s, wait_s=30)
        return _Actor(self.succ)

    def go(self, **kw):
        kw.setdefault("ack_timeout_s", self.ack_timeout_s)
        kw.setdefault("takeover_s", self.takeover_s)
        return run_rollover(
            self.w.pred, self.w.dir, inp=self.inp or self.w.input(), observer=self.w.observer,
            launcher=self.launcher, root=self.w.root, repo=self.w.repo, project_id="p",
            successor_session=self.successor, poll_s=self.poll_s, sleep=self.sleep, **kw)


class _Actor:
    def __init__(self, succ: Successor) -> None:
        self.succ = succ

    def acknowledge_into(self, rot: Rotation) -> None:
        try:
            _, rot.ack = self.succ.acknowledge()
        except RolloverError as exc:
            rot.errors.append(exc)

    def take(self, ack, *, token_out):
        return self.succ.take(ack, token_out=token_out)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def chain_reasons(world: World) -> list[Reason]:
    return [e.reason for e in EpochStore(world.dir).chain()]


def events(world: World) -> list[str]:
    return [e["event"] for e in RolloverLog(world.dir).events()]


# --------------------------------------------------------------------------- #
# K6 -- the state machine, the happy path, the pointer, no access to the old conversation
# --------------------------------------------------------------------------- #


def test_the_states_follow_the_protocol_order_and_nothing_else(w):
    log = RolloverLog(w.dir, clock=w.clock)
    assert log.state() is State.ACTIVE and log.events() == []

    def refuses(event, **fields):
        before = log.path.read_bytes() if log.path.exists() else b""
        with pytest.raises(RolloverError):
            log.record(event, **fields)
        assert (log.path.read_bytes() if log.path.exists() else b"") == before

    for event in ("frozen", "launched", "ack", "handoff", "complete", "retired",
                  "reclaimed", "abort", "no-such-event"):
        refuses(event, capsule_id="c1")                       # nothing starts in the middle
    log.record("draining", capsule_id="c1", expires_at="2026-10-07T13:00:00Z")
    assert log.state() is State.DRAINING
    for event in ("ack", "handoff", "launched", "complete", "retired", "pending", "draining"):
        refuses(event, capsule_id="c1")
    log.record("frozen", capsule_id="c1", expires_at="2026-10-07T13:00:00Z")
    assert log.state() is State.CAPSULE_FROZEN
    refuses("handoff", capsule_id="c1")                       # no handoff without an ack
    refuses("ack", capsule_id="c1")
    refuses("launched", capsule_id="other")                   # a different cycle's event
    log.record("launched", capsule_id="c1", expires_at="2026-10-07T13:00:00Z")
    assert log.state() is State.SUCCESSOR_STARTING
    refuses("handoff", capsule_id="c1")
    log.record("ack", capsule_id="c1", expires_at="2026-10-07T13:00:00Z")
    assert log.state() is State.SUCCESSOR_VALIDATING
    refuses("complete", capsule_id="c1")
    refuses("retired", capsule_id="c1")
    log.record("handoff", capsule_id="c1", expires_at="2026-10-07T13:00:00Z")
    refuses("retired", capsule_id="c1")
    log.record("complete", capsule_id="c1")
    assert log.state() is State.SUCCESSOR_ACTIVE
    log.record("retired", capsule_id="c1")
    assert log.state() is State.PREDECESSOR_RETIRED
    # append-only: the lines already there are never touched
    lines = log.path.read_text().splitlines()
    log.record("draining", capsule_id="c2", expires_at="2026-10-07T14:00:00Z")   # a new cycle
    assert log.path.read_text().splitlines()[:len(lines)] == lines
    # abort and reclaimed return to ACTIVE, from every waiting state
    for stop_at in ("frozen", "launched", "ack", "handoff"):
        local = RolloverLog(w.dir / f"x-{stop_at}", clock=w.clock)
        for ev in ("draining", "frozen", "launched", "ack", "handoff"):
            local.record(ev, capsule_id="c", expires_at="2026-10-07T13:00:00Z")
            if ev == stop_at:
                break
        local.record("abort", capsule_id="c")
        assert local.state() is State.ACTIVE
    local.record("draining", capsule_id="d", expires_at="2026-10-07T13:00:00Z")
    local.record("frozen", capsule_id="d", expires_at="2026-10-07T13:00:00Z")
    local.record("launched", capsule_id="d", expires_at="2026-10-07T13:00:00Z")
    local.record("ack", capsule_id="d", expires_at="2026-10-07T13:00:00Z")
    local.record("handoff", capsule_id="d", expires_at="2026-10-07T13:00:00Z")
    local.record("reclaimed", capsule_id="d")
    assert local.state() is State.ACTIVE
    # a torn tail is ended, never glued onto
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('{"event": "frozen", "capsule_id": "c2", "at": ')
    assert log.state() is State.DRAINING                      # the torn line is not an event
    log.record("frozen", capsule_id="c2", expires_at="2026-10-07T14:00:00Z")
    assert log.state() is State.CAPSULE_FROZEN


def test_a_the_role_moves_over_the_predecessor_is_fenced_and_the_receipt_binds_both(w):
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE, result.detail
    assert result.successor_epoch == 2 and not result.predecessor_is_master
    # F: the predecessor is fenced and must go idle
    assert w.pred.problems()
    with pytest.raises(StaleMaster):
        w.pred.check()
    with pytest.raises(StaleMaster):
        w.pred.renew()
    assert rot.succ_auth.problems() == []
    # exactly one epoch step, for SUCCESSION, held by the successor
    chain = EpochStore(w.dir).chain()
    assert [(e.epoch, e.master_id, e.reason) for e in chain] == [
        (1, "pred", Reason.INITIAL), (2, "succ", Reason.SUCCESSION)]
    # never two masters, at any tick
    assert rot.max_masters <= 1 and rot.ticks > 0
    # the log tells the protocol
    assert events(w) == ["draining", "frozen", "launched", "ack", "handoff", "complete",
                         "retired"]
    assert RolloverLog(w.dir).state() is State.PREDECESSOR_RETIRED
    # the receipt binds the capsule, the ack, both sessions and epochs and the lease token
    cid = result.capsule_id
    cap = load_capsule(capsule_path(w.dir, cid))
    ack = json.loads(ack_path(w.dir, cid).read_text())
    receipt = json.loads(receipt_path(w.dir, cid).read_text())
    lease = rot.succ_auth.read_lease()
    assert receipt["schema_version"] == "hoh-succession-receipt/1"
    assert receipt["capsule_digest"] == cap.capsule_digest == ack["capsule_digest"]
    assert receipt["ack_digest"] == ack["ack_digest"]
    assert (receipt["predecessor_session"], receipt["predecessor_epoch"]) == ("pred", 1)
    assert (receipt["successor_session"], receipt["successor_epoch"]) == ("succ", 2)
    assert receipt["lease_token_sha256"] == lease.token_sha256
    assert receipt["lease_acquired_at"] == lease.acquired_at
    assert receipt["receipt_digest"]
    # the successor lease is a different credential from the predecessor's
    assert lease.token_sha256 != cap.predecessor_token_sha256
    assert not rot.launcher.stopped                            # the successor is left running
    assert (receipt_path(w.dir, cid).stat().st_mode & 0o777) == 0o600
    assert not aborted_path(w.dir, cid).exists()


def test_the_successor_starts_with_a_pointer_outside_the_repository(w):
    repo = Path(w.repo)
    repo.mkdir()
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE
    [launch] = rot.launcher.started
    cap = load_capsule(capsule_path(w.dir, result.capsule_id))
    prompt = launch["prompt"]
    assert prompt.isascii() and len(prompt) < 2500
    assert cap.capsule_digest in prompt and str(capsule_path(w.dir, result.capsule_id)) in prompt
    assert "succ" in prompt and "Do no programme work yet" in prompt
    assert '"took over"' in prompt and "tell the operator" in prompt
    # a pointer and nothing else: no history, no decision, no advisory text
    for text in (OPERATOR_TEXT.strip(), "the programme is on phase B", "the integration checkout"):
        assert text not in prompt
    # the working directory is the product's own, empty, outside the repository
    assert launch["cwd"] == w.dir / "rollover" / f"{result.capsule_id}.cwd"
    assert launch["cwd_entries"] == []
    assert repo not in launch["cwd"].parents and launch["cwd"] != repo
    assert os.path.commonpath([str(repo), str(launch["cwd"])]) != str(repo)
    assert launch["model"] == "claude-opus-5-5" == cap.successor_model
    assert launch["session_id"] == "succ"
    # the one command is the real one
    [command] = [ln for ln in prompt.splitlines() if "rollover-accept" in ln and "hoh" in ln
                 and ln.startswith("  ")]
    argv = shlex.split(command)
    assert argv[:2] == ["hoh", "--root"] and argv[2] == str(w.root)
    parsed = cli.build_parser().parse_args(argv[1:])
    assert parsed.project_cmd == "rollover-accept" and parsed.project_id == "p"
    assert parsed.capsule == str(capsule_path(w.dir, result.capsule_id))
    assert parsed.digest == cap.capsule_digest and parsed.session == "succ"
    assert parsed.token_out == str(launch["cwd"] / "master.token")


def test_o_a_successor_with_no_access_to_the_old_conversation_continues_from_durable_state(
        w, monkeypatch):
    decoy = w.root / "old-conversation.jsonl"
    decoy.write_text(rec(inp=900_000) + "\n")
    monkeypatch.setattr(ro, "evaluate_trigger",
                        lambda *a, **k: pytest.fail("the successor path read a transcript"))
    read: list[str] = []
    real_read_text = Path.read_text

    def spying(self, *a, **k):
        read.append(str(self))
        return real_read_text(self, *a, **k)

    def watched(fn):
        """Record every file the successor reads, and only while it is acting."""
        def inner(self, *a, **k):
            with monkeypatch.context() as m:
                m.setattr(Path, "read_text", spying)
                return fn(self, *a, **k)
        return inner

    monkeypatch.setattr(Successor, "acknowledge", watched(Successor.acknowledge))
    monkeypatch.setattr(Successor, "take", watched(Successor.take))
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE
    # the successor was built from a path, a digest and its own session id: nothing of the
    # predecessor's process, and nothing of a transcript
    assert read and not any(p.endswith(".jsonl") or "transcript" in p or p == str(decoy)
                            for p in read)
    assert rot.succ is not None and rot.succ.authority is rot.succ_auth
    assert rot.succ.authority is not w.pred
    cap = load_capsule(capsule_path(w.dir, result.capsule_id))
    assert "transcript" not in cap.model_dump_json().lower()
    # everything it needs to continue is in the capsule: the facts it re-derived, the open
    # decision, the register, the runs
    assert {e.entry_id for e in cap.entries} >= {"plan", "runs", "cand", "review", "d1"}
    assert rot.succ_auth.problems() == [] and rot.taken is not None


# --------------------------------------------------------------------------- #
# K4 (rollover) -- the operator's words arrive as said, unanswered
# --------------------------------------------------------------------------- #


def test_i_the_operators_open_decision_arrives_exactly_as_said_and_unanswered(w):
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE
    cap = load_capsule(capsule_path(w.dir, result.capsule_id))        # what the successor loads
    d = entry(cap, "d1")
    assert d.text == OPERATOR_TEXT and d.status == "open"
    assert d.text.encode("utf-8") == OPERATOR_TEXT.encode("utf-8")
    # the machine added no answer anywhere: not to the entry, not to the ack, not to the receipt
    assert [e.status for e in cap.entries if e.entry_id == "d1"] == ["open"]
    ack = ack_path(w.dir, result.capsule_id).read_text()
    receipt = receipt_path(w.dir, result.capsule_id).read_text()
    assert "decided" not in ack and "decided" not in receipt
    assert OPERATOR_TEXT.strip() not in ack and OPERATOR_TEXT.strip() not in receipt
    log_text = (w.dir / "rollover" / "rollover.log.jsonl").read_text()
    assert OPERATOR_TEXT.strip() not in log_text


# --------------------------------------------------------------------------- #
# K7 -- fail closed
# --------------------------------------------------------------------------- #


def assert_still_master(w: World, rot: Rotation | None = None) -> None:
    assert w.pred.problems() == []
    lease = w.pred.read_lease()
    assert lease is not None and not lease.released_at
    assert [e.master_id for e in EpochStore(w.dir).chain()] == ["pred"]
    if rot is not None:
        assert rot.max_masters <= 1


def test_a_successor_that_does_not_start_leaves_the_predecessor_master(w):
    rot = Rotation(w, launcher=FakeLauncher(fail="herdr is not running"))
    result = rot.go()
    assert result.outcome is Outcome.LAUNCH_FAILED and result.predecessor_is_master
    assert "herdr is not running" in result.detail
    assert_still_master(w, rot)
    assert events(w) == ["draining", "frozen", "abort"]
    assert RolloverLog(w.dir).state() is State.ACTIVE
    assert aborted_path(w.dir, result.capsule_id).exists()
    # nothing was handed over, so nothing needs reclaiming: the next rotation just works
    again = Rotation(w).go()
    assert again.outcome is Outcome.COMPLETE


def test_g_a_successor_that_never_acknowledges_costs_nothing_and_is_refused_late(w):
    rot = Rotation(w, acks=False)
    result = rot.go()
    assert result.outcome is Outcome.NO_ACK and result.predecessor_is_master
    cap = load_capsule(rot.capsule_file())
    assert w.clock() >= cap.ack_deadline                       # it waited the whole window
    assert_still_master(w, rot)
    assert rot.launcher.stopped == [LaunchHandle("agent-succ", "pane-1", "tab-1")]
    assert aborted_path(w.dir, cap.capsule_id).exists()
    assert events(w) == ["draining", "frozen", "launched", "abort"]
    # the lease was renewed through the whole wait, and is still the predecessor's
    assert all(rot.lease_live_at_each_tick)
    # a late successor takes nothing and writes nothing
    late = Successor(rot.succ_auth, rot.capsule_file(), observer=w.observer, sleep=lambda s: None)
    before = w.snapshot()
    with pytest.raises(RolloverError, match="aborted"):
        late.accept()
    assert w.snapshot() == before
    assert not ack_path(w.dir, cap.capsule_id).exists()
    assert [e.master_id for e in EpochStore(w.dir).chain()] == ["pred"]


def test_c_a_head_that_moved_before_the_successor_looked_refuses_and_a_refreeze_works(w):
    def move_head(rot):
        rot.w.observer.head_sha = "b" * 40

    rot = Rotation(w, hooks={"launched": move_head})
    result = rot.go()
    assert result.outcome is Outcome.BAD_ACK and result.predecessor_is_master
    cap = load_capsule(rot.capsule_file())
    assert w.clock() < cap.ack_deadline                        # the refusal was seen at once
    assert "did not verify" in result.detail and "head" in result.detail
    ack = json.loads(ack_path(w.dir, cap.capsule_id).read_text())
    assert ack["verified"] is False and ack["observed_head"] == "b" * 40
    assert_still_master(w, rot)
    assert rot.launcher.stopped and aborted_path(w.dir, cap.capsule_id).exists()
    # a re-freeze against the ground as it is now works
    second = Rotation(w).go()
    assert second.outcome is Outcome.COMPLETE and second.capsule_id != result.capsule_id
    assert load_capsule(capsule_path(w.dir, second.capsule_id)).head == "b" * 40


def test_d_a_run_that_changed_between_freezing_and_looking_refuses_naming_the_run_set(w):
    def change_run(rot):
        rot.w.observer.runs[0]["attempt"] = 9

    rot = Rotation(w, hooks={"launched": change_run})
    result = rot.go()
    assert result.outcome is Outcome.BAD_ACK and result.predecessor_is_master
    assert "runs" in result.detail and "run_set" in result.detail
    assert_still_master(w, rot)
    ack = json.loads(ack_path(w.dir, result.capsule_id).read_text())
    assert ack["verified"] is False and any("runs" in m for m in ack["mismatches"])


def forge_ack(rot: Rotation, change) -> None:
    """Write a self-consistent acknowledgement (right digest) that says something the
    frozen capsule does not: `change` maps the real verification to altered fields."""
    cap = load_capsule(rot.capsule_file())
    ver = rot.w.verify(cap)
    assert ver.ok
    bad = dataclasses.replace(ver, **change(ver))
    ack = build_ack(cap, bad, at=rot.w.clock())
    ack_path(rot.w.dir, cap.capsule_id).write_text(json.dumps(ack))


def test_an_ack_that_differs_from_what_was_frozen_is_refused(tmp_path):
    variants = {
        "section": lambda v: {"observed_sections": {**v.observed_sections, "RUNS": "0" * 64}},
        "head": lambda v: {"observed_head": "b" * 40},
        "branch": lambda v: {"observed_branch": "other"},
    }
    for name, change in variants.items():
        world = World(tmp_path / name)
        rot = Rotation(world, acks=False,
                       hooks={"launched": lambda r, change=change: forge_ack(r, change)})
        result = rot.go()
        assert result.outcome is Outcome.BAD_ACK, (name, result.detail)
        assert_still_master(world, rot)
        assert aborted_path(world.dir, result.capsule_id).exists()
    # an acknowledgement that is not even JSON
    world = World(tmp_path / "junk")
    rot = Rotation(world, acks=False, hooks={"launched": lambda r: ack_path(
        r.w.dir, r.capsule_id()).write_text("{torn")})
    assert rot.go().outcome is Outcome.BAD_ACK
    assert_still_master(world, rot)
    # an acknowledgement of a *different* frozen capsule
    world = World(tmp_path / "other")

    def other_capsule(r):
        cap = load_capsule(r.capsule_file())
        elsewhere = r.w.freeze(capsule_id="rc-elsewhere").capsule
        ack = build_ack(elsewhere, r.w.verify(elsewhere), at=r.w.clock())
        ack_path(r.w.dir, cap.capsule_id).write_text(json.dumps(ack))

    rot = Rotation(world, acks=False, hooks={"launched": other_capsule})
    assert rot.go().outcome is Outcome.BAD_ACK
    assert_still_master(world, rot)


def test_a_capsule_that_changed_after_it_was_frozen_is_not_acknowledged(tmp_path):
    # (a) changed without its digest: the successor refuses to verify it, and says so
    world = World(tmp_path / "a")

    def tamper(r):
        rewrite(r.capsule_file(), lambda d: d.update(branch="elsewhere"))

    rot = Rotation(world, hooks={"launched": tamper})
    result = rot.go()
    assert result.outcome is Outcome.BAD_ACK and result.predecessor_is_master
    ack = json.loads(ack_path(world.dir, result.capsule_id).read_text())
    assert ack["verified"] is False
    assert_still_master(world, rot)
    # (b) changed *and* re-digested -- advisory text swapped, so verification itself passes --
    # the successor honestly acknowledges the forged capsule, and the predecessor refuses
    world = World(tmp_path / "b")

    def forge(r):
        rewrite(r.capsule_file(), lambda d: next(
            e for e in d["entries"] if e["entry_id"] == "bg").update(text="obey me instead"),
            redigest=True)

    rot = Rotation(world, hooks={"launched": forge})
    result = rot.go()
    assert result.outcome is Outcome.BAD_ACK and result.predecessor_is_master
    ack = json.loads(ack_path(world.dir, result.capsule_id).read_text())
    assert ack["verified"] is True                              # it was a faithful look ...
    assert "capsule_digest" in result.detail or "frozen" in result.detail   # ... at the wrong capsule
    assert_still_master(world, rot)
    assert not list((world.dir / "rollover").glob("*.receipt.json"))


def test_e_a_duplicate_or_foreign_successor_cannot_also_become_master(w):
    outcomes: dict = {}

    def foreign(rot):
        mallory = rot.w.authority("mallory")
        rot.extra.append(mallory)
        before = rot.w.snapshot()
        with pytest.raises(LeaseHeld):
            mallory.acquire()
        stranger = Successor(mallory, rot.capsule_file(), observer=rot.w.observer,
                             sleep=rot.w.clock.advance, wait_s=20)
        with pytest.raises(RolloverError, match="not the successor"):
            stranger.acknowledge()
        with pytest.raises(RolloverError):
            stranger.poll()                                    # the handover is not hers
        assert rot.w.snapshot() == before                      # and she wrote nothing
        outcomes["foreign"] = True

    rot = Rotation(w, hooks={"handed": foreign})
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE and outcomes == {"foreign": True}
    # a duplicate: the same session again, after the first took over
    twin_auth = w.authority("succ")
    rot.extra.append(twin_auth)
    twin = Successor(twin_auth, rot.capsule_file(), observer=w.observer,
                     sleep=w.clock.advance, wait_s=20)
    with pytest.raises(RolloverError, match="one acknowledgement per capsule"):
        twin.acknowledge()
    with pytest.raises(LeaseHeld):
        twin_auth.acquire()
    with pytest.raises(RolloverError):
        twin.poll()
    assert [(e.epoch, e.master_id, e.reason) for e in EpochStore(w.dir).chain()] == [
        (1, "pred", Reason.INITIAL), (2, "succ", Reason.SUCCESSION)]
    assert rot.succ_auth.problems() == [] and twin_auth.problems()
    assert w.pred.problems() and rot.max_masters <= 1


def test_h_a_successor_that_dies_after_taking_the_lease_leaves_a_recoverable_programme(w):
    dead_pid = 2_000_000_000                                   # no such process
    rot = Rotation(w, succ_pid=dead_pid, hooks={"handed": lambda r: None})

    def take_only(r):
        # the successor acquires, and the process dies before the receipt is written
        r.take_tried = True
        r.succ = Successor(r.succ_auth, r.capsule_file(), observer=r.w.observer,
                           sleep=r.w.clock.advance, wait_s=20)
        r.succ.poll()

    rot.hooks["handed"] = take_only
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE                  # the epoch moved: the predecessor is done
    assert not list((w.dir / "rollover").glob("*.receipt.json"))        # but nothing says more
    probe = w.authority("reader")
    state, why = probe.holder_state(probe.read_lease(), w.clock())
    assert state == "LOST" and "no longer exists" in why       # a lease needs a living holder
    # not wedged: a new session acquires at once, as recovery
    fresh = w.authority("operator-session")
    epoch = fresh.acquire(program_generation=1)
    assert epoch.reason is Reason.RECOVERY and epoch.epoch == 3
    assert fresh.problems() == [] and rot.succ_auth.problems() and w.pred.problems()
    assert chain_reasons(w) == [Reason.INITIAL, Reason.SUCCESSION, Reason.RECOVERY]


def test_a_successor_that_verifies_and_never_takes_over_costs_one_takeover_window(w):
    rot = Rotation(w, takes=False, takeover_s=120)
    result = rot.go()
    assert result.outcome is Outcome.RECLAIMED and result.predecessor_is_master
    assert result.reclaimed_epoch == 2 and result.token
    handoff = next(r for r in RolloverLog(w.dir).events() if r["event"] == "handoff")
    reclaimed = next(r for r in RolloverLog(w.dir).events() if r["event"] == "reclaimed")
    from datetime import datetime
    spent = (datetime.strptime(reclaimed["at"], "%Y-%m-%dT%H:%M:%SZ")
             - datetime.strptime(handoff["at"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
    assert 120 <= spent < 120 + 2 * rot.poll_s                 # one takeover window, not a TTL
    assert w.ttl == 1800 > spent
    # the program is the predecessor's again, at a fresh epoch, with a new token
    assert chain_reasons(w) == [Reason.INITIAL, Reason.RECOVERY]
    assert [e.master_id for e in EpochStore(w.dir).chain()] == ["pred", "pred"]
    assert w.pred.problems() == [] and rot.max_masters <= 1
    assert w.pred.read_lease().token_sha256 != load_capsule(
        rot.capsule_file()).predecessor_token_sha256
    assert "hoh project hold" in result.hint                   # and it says what to do next
    assert rot.launcher.stopped and events(w)[-2:] == ["handoff", "reclaimed"]
    assert RolloverLog(w.dir).state() is State.ACTIVE
    # a successor that wakes up late takes nothing
    with pytest.raises(RolloverError, match="aborted"):
        Successor(rot.succ_auth, rot.capsule_file(), observer=w.observer,
                  sleep=w.clock.advance, wait_s=20).poll()
    assert rot.succ_auth.problems() and rot.succ_auth.epoch is None


def test_a_write_in_flight_marks_the_rotation_pending_and_it_runs_at_the_next_safe_point(w):
    busy = {"yes": True}
    launcher = FakeLauncher()
    rot = Rotation(w, launcher=launcher)
    first = rot.go(safe=lambda: not busy["yes"])
    assert first.outcome is Outcome.DEFERRED and first.predecessor_is_master
    assert not launcher.started and not list((w.dir / "rollover").glob("*.capsule.json"))
    assert events(w) == ["pending"] and RolloverLog(w.dir).state() is State.ROTATION_PENDING
    assert_still_master(w)
    second = rot.go(safe=lambda: not busy["yes"])              # still unsafe: no second record
    assert second.outcome is Outcome.DEFERRED and events(w) == ["pending"]
    busy["yes"] = False
    third = rot.go(safe=lambda: not busy["yes"])               # the next safe point continues
    assert third.outcome is Outcome.COMPLETE
    assert events(w) == ["pending", "draining", "frozen", "launched", "ack", "handoff",
                         "complete", "retired"]


def test_a_session_that_is_no_longer_the_master_cannot_start_a_rollover(w):
    w.pred.handoff("somebody-else", reason="manual rotation")
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.REFUSED and not result.predecessor_is_master
    assert "stale master" in result.detail
    assert not rot.launcher.started and not (w.dir / "rollover").exists()


def test_a_lease_nobody_keeps_alive_is_refused_before_anything_is_frozen(w):
    # the master acquired it from a short command that has since exited
    w.pred.release(reason="the command that took it exited")
    gone = w.authority("pred", pid=2_000_000_000)                  # no such process
    gone.acquire()
    w.pred = gone
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.REFUSED and result.predecessor_is_master
    assert "not held by a living process" in result.detail and "LOST" in result.detail
    assert "hoh project hold" in result.hint
    assert not rot.launcher.started and not (w.dir / "rollover").exists()


def test_a_succession_in_flight_blocks_a_second_one_until_its_deadline_passes(w):
    log = RolloverLog(w.dir, clock=w.clock)
    log.record("draining", capsule_id="rc-old", expires_at="2026-10-07T12:05:00Z")
    log.record("frozen", capsule_id="rc-old", expires_at="2026-10-07T12:05:00Z")
    log.record("launched", capsule_id="rc-old", expires_at="2026-10-07T12:05:00Z")
    rot = Rotation(w)
    blocked = rot.go()
    assert blocked.outcome is Outcome.IN_FLIGHT and blocked.predecessor_is_master
    assert blocked.capsule_id == "rc-old" and not rot.launcher.started
    assert events(w) == ["draining", "frozen", "launched"]     # nothing was disturbed
    w.clock.advance(301)                                       # past rc-old's own deadline
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE
    ended = [r for r in RolloverLog(w.dir).events() if r["event"] == "abort"]
    assert len(ended) == 1 and ended[0]["reason"] == "expired"
    assert ended[0]["capsule_id"] == "rc-old"
    assert events(w)[3] == "abort" and events(w)[4] == "draining"


def test_a_predecessor_that_lost_the_lease_while_waiting_reports_it(w):
    def intrude(rot):
        clone = rot.w.authority("pred")
        clone.present(epoch=1, token=rot.w.pred.token)
        clone.handoff("intruder", reason="somebody else took the programme")

    rot = Rotation(w, acks=False, hooks={"launched": intrude})
    result = rot.go()
    assert result.outcome is Outcome.SUPERSEDED and not result.predecessor_is_master
    assert "lost the lease" in result.detail
    assert w.pred.problems()
    assert rot.launcher.stopped and aborted_path(w.dir, result.capsule_id).exists()
    assert RolloverLog(w.dir).state() is State.ACTIVE
    assert [e.master_id for e in EpochStore(w.dir).chain()] == ["pred"]     # nothing was begun


def test_a_handoff_that_fails_after_the_lease_was_written_still_completes(w):
    real = w.pred.handoff

    def flaky(*a, **k):
        real(*a, **k)
        raise OSError("directory fsync failed")             # the lease is written, the call raised

    w.pred.handoff = flaky
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE, result.detail
    assert chain_reasons(w) == [Reason.INITIAL, Reason.SUCCESSION]
    # and a handoff that raised *without* writing is a refusal, and the predecessor stays
    world = World(w.root / "refused")
    world.pred.handoff = lambda *a, **k: (_ for _ in ()).throw(LeaseError("not today"))
    rot2 = Rotation(world)
    refused = rot2.go()
    assert refused.outcome is Outcome.HANDOFF_REFUSED and refused.predecessor_is_master
    assert "not today" in refused.detail
    assert rot2.launcher.stopped and aborted_path(world.dir, refused.capsule_id).exists()
    assert world.pred.problems() == [] and not world.pred.read_lease().released_at


def test_a_long_wait_for_the_ack_keeps_the_lease_alive(tmp_path):
    world = World(tmp_path, ttl=60)                            # a lease that lapses in a minute
    rot = Rotation(world, ack_after=50, ack_timeout_s=600, poll_s=10.0)   # acks after ~500 s
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE, result.detail
    assert rot.ticks >= 50 and all(rot.lease_live_at_each_tick[:50])
    assert world.clock() > "2026-10-07T12:08:00Z"              # far past the 60 s TTL
    # the same wait with nobody renewing would have lost the lease: the control
    ctl = World(tmp_path / "control", ttl=60)
    ctl.clock.advance(61)
    assert ctl.pred.problems()


# --------------------------------------------------------------------------- #
# K8 -- a bounded handover, a lease needs a living holder, hold_lease
# --------------------------------------------------------------------------- #


def test_a_handover_can_be_bounded_and_then_lapses(w):
    lease = w.pred.handoff("succ", reason="rotate", expires_in=30)
    assert lease.expires_at == "2026-10-07T12:00:30Z" and lease.successor == "succ"
    other = w.authority("other")
    with pytest.raises(LeaseHeld):                              # it stands for its window
        other.acquire()
    w.clock.advance(29)
    with pytest.raises(LeaseHeld):
        other.acquire()
    w.clock.advance(1)                                          # the window is over
    epoch = other.acquire()
    assert epoch.reason is Reason.RECOVERY and epoch.epoch == 2   # not wedged for a whole TTL
    # the named successor keeps its right only while the handover stands
    w2 = World(w.root / "again")
    w2.pred.handoff("succ", reason="rotate", expires_in=30)
    assert w2.authority("succ").acquire().reason is Reason.SUCCESSION


def test_a_handoff_without_a_bound_keeps_the_handles_ttl_and_a_bad_bound_is_refused(w):
    lease = w.pred.handoff("succ", reason="rotate")
    assert lease.expires_at == "2026-10-07T12:30:00Z"            # now + the handle's 1800 s
    for bad in (0, -5, 1.5, "30", True, [30]):
        world = World(w.root / f"bad-{abs(hash(repr(bad)))}")
        before = world.snapshot()
        with pytest.raises(LeaseError, match="expires_in"):
            world.pred.handoff("succ", reason="rotate", expires_in=bad)
        assert world.pred.problems() == []                      # refused, and still the master
        assert not world.pred.read_lease().released_at
        assert world.snapshot() == before


def test_a_lease_is_judged_by_its_holders_process_so_something_must_stay_alive(tmp_path):
    from test_succession_capsule import Clock

    clock = Clock()
    alive_pids = {4242}
    d = tmp_path / "prog"

    def handle(session, pid=None):
        return MasterAuthority(d, session, clock=clock, pidns=lambda: "ns", pid=pid,
                               alive=lambda p: p in alive_pids)

    short = handle("short-command", pid=4242)
    short.acquire()
    reader = handle("reader")
    lease = short.read_lease()
    assert reader.holder_state(lease, clock())[0] == "LIVE"
    alive_pids.clear()                                          # the command that took it exited
    state, why = reader.holder_state(lease, clock())
    assert state == "LOST" and "no longer exists" in why
    assert lease.expires_at > clock()                           # nowhere near its TTL
    assert reader.acquire().reason is Reason.RECOVERY           # so the next session just takes it


def test_hold_lease_keeps_the_lease_alive_past_its_ttl_and_ends_when_fenced(tmp_path):
    world = World(tmp_path, ttl=10)
    probe = world.authority("probe")
    live: list[bool] = []
    n = {"ticks": 0}

    def sleep(seconds):
        world.clock.advance(seconds)
        n["ticks"] += 1
        lease = world.pred.read_lease()
        live.append(probe.holder_state(lease, world.clock())[0] == "LIVE")
        if n["ticks"] == 15:                                    # 45 s in: another master moves in
            world.pred.handoff("next", reason="rotate")

    result = hold_lease(world.pred, interval=3, sleep=sleep, monotonic=lambda: 0.0)
    assert isinstance(result, HoldResult)
    assert result.reason == "fenced" and not result.released
    assert result.renewals == 15 and n["ticks"] == 15
    assert world.clock() == "2026-10-07T12:00:45Z"              # four and a half TTLs
    assert all(live[:14])                                       # alive the whole time, renewed
    assert world.pred.problems()                                # and now fenced
    # the control: without a holder that renews, the same lease is LOST at its TTL
    ctl = World(tmp_path / "control", ttl=10)
    ctl.clock.advance(11)
    assert ctl.authority("p").holder_state(ctl.pred.read_lease(), ctl.clock())[0] == "LOST"


def test_hold_lease_releases_when_the_watched_session_process_is_gone(tmp_path):
    world = World(tmp_path)
    sessions = iter([True, True, True, False])
    seen_pids: list[int] = []

    def alive(pid):
        seen_pids.append(pid)
        return next(sessions)

    result = hold_lease(world.pred, interval=5, watch_pid=31337, alive=alive,
                        sleep=world.clock.advance)
    assert result.reason == "watched session ended" and result.released
    assert result.renewals == 3 and set(seen_pids) == {31337}
    lease = world.authority("reader").read_lease()
    assert lease.released_at and "watched session ended" in lease.released_reason
    assert lease.successor is None
    # a released lease can be taken at once
    assert world.authority("next").acquire().reason is Reason.RECOVERY
    # and --hold-for ends it too, also giving the lease back
    w2 = World(tmp_path / "bounded")
    ticks = {"t": 0.0}

    def sleep(s):
        ticks["t"] += s
        w2.clock.advance(s)

    done = hold_lease(w2.pred, interval=3, hold_for=10, sleep=sleep,
                      monotonic=lambda: ticks["t"])
    assert done.reason == "elapsed" and done.released and done.renewals == 4
    assert w2.authority("r").read_lease().released_at


# --------------------------------------------------------------------------- #
# K9 -- the Herdr launcher
# --------------------------------------------------------------------------- #


class FakeHerdr:
    """A fake `herdr` CLI: records every argv, answers like the real JSON."""

    def __init__(self, *, fail_on: tuple[str, str] | None = None, pane: str | None = "pane-9",
                 tab: str | None = "tab-9", agent_start_errors: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.pane, self.tab = pane, tab
        self.agent_start_errors = list(agent_start_errors or [])

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        key = (argv[1], argv[2])
        if key == ("agent", "start") and self.agent_start_errors:
            return subprocess.CompletedProcess(argv, 1, "", self.agent_start_errors.pop(0))
        if key == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, "", "boom: " + " ".join(key))
        if key == ("tab", "create"):
            result = {}
            if self.pane:
                result["root_pane"] = {"pane_id": self.pane}
            if self.tab:
                result["tab"] = {"tab_id": self.tab}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")
        if key == ("agent", "start"):
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"result": {"agent": {"pane_id": self.pane}}}), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def of(self, a: str, b: str) -> list[list[str]]:
        return [c for c in self.calls if (c[1], c[2]) == (a, b)]


def test_the_herdr_launcher_opens_its_own_tab_and_sends_the_pointer(tmp_path):
    herdr = FakeHerdr()
    launcher = HerdrLauncher(run=herdr, workspace="ws-1", settle_s=0, sleep=lambda s: None)
    cwd = tmp_path / "x.cwd"
    cwd.mkdir()
    handle = launcher.start(session_id="succ-20261007-abc", model="claude-opus-5-5", cwd=cwd,
                            prompt="THE POINTER", label="successor-rc-1")
    assert handle == LaunchHandle(agent=handle.agent, pane_id="pane-9", tab_id="tab-9")
    [tab_create] = herdr.of("tab", "create")
    assert tab_create[:3] == ["herdr", "tab", "create"]
    assert tab_create[tab_create.index("--cwd") + 1] == str(cwd)       # not the repository
    assert tab_create[tab_create.index("--label") + 1] == "successor-rc-1"
    assert "--no-focus" in tab_create
    assert tab_create[tab_create.index("--workspace") + 1] == "ws-1"
    assert "--env" not in tab_create                                    # no role-pane mark
    [start] = herdr.of("agent", "start")
    assert start[start.index("--pane") + 1] == "pane-9"
    assert start[start.index("--kind") + 1] == "claude"
    assert start[-2:] == ["--model", "claude-opus-5-5"]
    assert start[3] == handle.agent and len(handle.agent) <= 24
    [prompt] = herdr.of("agent", "prompt")
    assert prompt[3:] == [handle.agent, "THE POINTER"]                  # the pointer, exactly
    # the order is tab, agent, prompt -- and nothing was closed
    assert [(c[1], c[2]) for c in herdr.calls] == [
        ("tab", "create"), ("agent", "start"), ("agent", "prompt")]
    # stop closes the one tab this launcher opened, and only that
    launcher.stop(handle)
    assert herdr.of("tab", "close") == [["herdr", "tab", "close", "tab-9"]]
    launcher.stop(LaunchHandle("a", "p", None))                         # nothing to close
    assert len(herdr.of("tab", "close")) == 1
    # a busy shell is retried, as the dispatcher does
    herdr2 = FakeHerdr(agent_start_errors=["not an available shell", "busy"])
    ok = HerdrLauncher(run=herdr2, settle_s=0, sleep=lambda s: None).start(
        session_id="s", model="m", cwd=cwd, prompt="p", label="l")
    assert ok.tab_id == "tab-9" and len(herdr2.of("agent", "start")) == 3


def test_a_launcher_that_fails_midway_closes_only_the_tab_it_opened(tmp_path):
    for failing in (("agent", "start"), ("agent", "prompt")):
        herdr = FakeHerdr(fail_on=failing, tab="tab-mine")
        launcher = HerdrLauncher(run=herdr, settle_s=0, sleep=lambda s: None)
        with pytest.raises(LauncherError, match="boom"):
            launcher.start(session_id="s", model="m", cwd=tmp_path, prompt="p", label="l")
        assert herdr.of("tab", "close") == [["herdr", "tab", "close", "tab-mine"]], failing
        assert all(tuple(c[1:3]) != ("tab", "close") or c[3] == "tab-mine"
                   for c in herdr.calls)
    # a tab with no pane is closed too: it was ours and is useless
    herdr = FakeHerdr(pane=None, tab="tab-empty")
    with pytest.raises(LauncherError, match="no pane"):
        HerdrLauncher(run=herdr, settle_s=0, sleep=lambda s: None).start(
            session_id="s", model="m", cwd=tmp_path, prompt="p", label="l")
    assert herdr.of("tab", "close") == [["herdr", "tab", "close", "tab-empty"]]
    # a failure to create the tab opens nothing and closes nothing
    herdr = FakeHerdr(fail_on=("tab", "create"))
    with pytest.raises(LauncherError):
        HerdrLauncher(run=herdr, settle_s=0, sleep=lambda s: None).start(
            session_id="s", model="m", cwd=tmp_path, prompt="p", label="l")
    assert not herdr.of("tab", "close") and not herdr.of("agent", "start")
    # and a launcher whose Herdr is not installed fails closed, not open
    def missing(argv, **kw):
        raise FileNotFoundError("herdr")
    with pytest.raises(LauncherError):
        HerdrLauncher(run=missing, settle_s=0).start(
            session_id="s", model="m", cwd=tmp_path, prompt="p", label="l")


# --------------------------------------------------------------------------- #
# K10 -- the commands
# --------------------------------------------------------------------------- #


def make_project(tmp_path: Path):
    """A real project store, a real git repository, and a live master with a token file."""
    repo = _make_repo(tmp_path)
    root = tmp_path / "hohroot"
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="waiting")]))
    master = MasterAuthority(store.dir, "pred", serialize=lambda: write_lock(store))
    master.acquire(program_generation=store.read_state().program_generation)
    token = tmp_path / "master.token"
    token.write_text(master.token)
    return SimpleNamespace(repo=repo, root=root, store=store, master=master, token=token)


def hoh(project, *argv: str) -> int:
    return cli.main(["--root", str(project.root), "project", *argv])


def master_args(project) -> list[str]:
    return ["--session", "pred", "--epoch", "1", "--token-file", str(project.token)]


class ThreadLauncher:
    """Starts the successor by running the pointer's own command line, in this
    process on a thread, through the real command parser."""

    instances: list["ThreadLauncher"] = []

    def __init__(self, *a, **k) -> None:
        ThreadLauncher.instances.append(self)
        self.rc = None
        self.argv: list[str] = []
        self.thread: threading.Thread | None = None

    def start(self, *, session_id, model, cwd, prompt, label):
        [command] = [ln for ln in prompt.splitlines() if ln.startswith("  hoh ")]
        argv = shlex.split(command)
        cli.build_parser().parse_args(argv[1:])            # the pointer's own line is valid
        # only to keep the test short: poll fast; the hold ends when the test fences it
        self.argv = argv[1:] + ["--poll", "0.05", "--hold-for", "30", "--interval", "0.05"]
        self.thread = threading.Thread(
            target=lambda: setattr(self, "rc", cli.main(self.argv)), daemon=True)
        self.thread.start()
        return SimpleNamespace(tab_id=None)

    def stop(self, handle) -> None:
        pass


class NullLauncher:
    def __init__(self, *a, **k) -> None:
        pass

    def start(self, **kw):
        return SimpleNamespace(tab_id=None)

    def stop(self, handle) -> None:
        pass


def test_the_command_line_rolls_over_end_to_end_through_the_pointers_own_command(
        tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    ThreadLauncher.instances.clear()
    monkeypatch.setattr(ro, "HerdrLauncher", ThreadLauncher)
    rc = hoh(project, "rollover", "p", *master_args(project), "--successor", "succ-e2e",
             "--model", "claude-opus-5-5", "--poll", "0.05", "--takeover", "10",
             "--ack-timeout", "30")
    out = capsys.readouterr().out
    [launcher] = ThreadLauncher.instances
    assert rc == 0, out
    assert '"outcome": "COMPLETE"' in out
    assert launcher.thread.is_alive()                         # the successor is holding the lease
    chain = EpochStore(project.store.dir).chain()
    assert [(e.epoch, e.master_id, e.reason) for e in chain][:2] == [
        (1, "pred", Reason.INITIAL), (2, "succ-e2e", Reason.SUCCESSION)]
    assert len(chain) == 2                                       # one step, and no more
    # the predecessor sees the successor's lease the moment it is written; the receipt
    # follows it, on the successor's thread
    import time
    for _ in range(200):
        receipts = list((project.store.dir / "rollover").glob("*.receipt.json"))
        if receipts:
            break
        time.sleep(0.05)
    [receipt] = receipts
    data = json.loads(receipt.read_text())
    assert (data["predecessor_session"], data["successor_session"]) == ("pred", "succ-e2e")
    assert (data["predecessor_epoch"], data["successor_epoch"]) == (1, 2)
    # the predecessor is fenced; its old token no longer acts
    assert hoh(project, "handoff", "p", *master_args(project), "--to", "x",
               "--reason", "late") == 5
    # the successor's token went where the pointer said, owner-only, and is its own
    [token_file] = (project.store.dir / "rollover").glob("*.cwd/master.token")
    assert (token_file.stat().st_mode & 0o777) == 0o600
    import hashlib
    assert hashlib.sha256(token_file.read_text().encode()).hexdigest() == data[
        "lease_token_sha256"]
    # the successor acts with it -- and handing over ends its hold loop, as a fenced holder's does
    assert hoh(project, "handoff", "p", "--session", "succ-e2e", "--epoch", "2",
               "--token-file", str(token_file), "--to", "next", "--reason", "test over") == 0
    launcher.thread.join(15)
    assert not launcher.thread.is_alive() and launcher.rc == 0
    # (the successor's own words come from its thread, possibly after the predecessor returned)
    out += capsys.readouterr().out
    assert "took over: session succ-e2e holds epoch 2" in out
    assert "hold ended: fenced" in out
    # nothing of the old conversation travelled: the capsule is a document of facts
    [capsule] = (project.store.dir / "rollover").glob("*.capsule.json")
    cap = load_capsule(capsule)
    assert cap.head == _git(project.repo, "rev-parse", "HEAD")
    assert {e.entry_id for e in cap.entries} == {"project-state", "run-set", "repo-head"}


def test_the_command_line_verifies_read_only_and_exits_5_on_a_difference(tmp_path, capsys):
    project = make_project(tmp_path)
    inp = CapsuleInput.model_validate({"successor_model": "m", "derive": [
        {"entry_id": "state", "section": "REGISTER", "deriver": "file_digest",
         "params": {"path": str(project.store.state_path)}},
        {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
         "params": {"runs_root": str(project.root)}},
        {"entry_id": "head", "section": "CANDIDATES", "deriver": "ref_state",
         "params": {"repo": str(project.repo), "ref": "HEAD"}}]})
    frozen = freeze_capsule(project.master, inp, directory=project.store.dir,
                            observer=LocalObserver(), repo=str(project.repo),
                            project_id="p", successor_session="succ")
    cap = frozen.capsule

    def snapshot():
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
                for p in sorted(project.root.rglob("*")) if p.is_file()}

    verify = ["rollover-verify", "p", "--capsule", str(frozen.path), "--session", "succ",
              "--digest", cap.capsule_digest]
    before = snapshot()
    assert hoh(project, *verify) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["differences"] == []
    assert snapshot() == before                                      # read-only, byte for byte
    # a moved HEAD: exit 5, named
    (project.repo / "new.txt").write_text("x\n")
    _git(project.repo, "add", "new.txt")
    _git(project.repo, "commit", "-q", "-m", "moved")
    before = snapshot()
    assert hoh(project, *verify) == 5
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert {"head", "fact"} <= {d["kind"] for d in report["differences"]}
    assert snapshot() == before
    # every other way to be refused is 5 as well
    assert hoh(project, *verify[:-1], "0" * 64) == 5                  # the pointer's digest is wrong
    assert hoh(project, "rollover-verify", "p", "--capsule", str(frozen.path),
               "--session", "somebody-else") == 5                     # not the named successor
    assert hoh(project, "rollover-verify", "p", "--capsule", str(tmp_path / "nope.json"),
               "--session", "succ") == 5
    capsys.readouterr()


def test_the_command_line_stays_master_and_exits_5_when_nobody_acknowledges(
        tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    monkeypatch.setattr(ro, "HerdrLauncher", NullLauncher)
    rc = hoh(project, "rollover", "p", *master_args(project), "--model", "m",
             "--ack-timeout", "1", "--poll", "0.05")
    out = json.loads(capsys.readouterr().out)
    assert rc == 5 and out["outcome"] == "NO_ACK" and out["predecessor_is_master"] is True
    assert project.master.problems() == []                           # still the master
    assert not project.master.read_lease().released_at
    assert [e.master_id for e in EpochStore(project.store.dir).chain()] == ["pred"]
    # no safe point: pending, exit 7
    with project.store.lock():                                       # a write is in flight
        rc = hoh(project, "rollover", "p", *master_args(project), "--model", "m",
                 "--poll", "0.05", "--ack-timeout", "1")
    out = json.loads(capsys.readouterr().out)
    assert rc == 7 and out["outcome"] == "DEFERRED"
    assert RolloverLog(project.store.dir).state() is State.ROTATION_PENDING
    # nothing due (a transcript below the threshold): exit 0, nothing started
    t = transcript(tmp_path, rec(inp=10, cr=20), name="small")
    n = len(list((project.store.dir / "rollover").glob("*.capsule.json")))
    rc = hoh(project, "rollover", "p", *master_args(project), "--model", "m",
             "--transcript", str(t), "--threshold", "1000")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["outcome"] == "NOT_TRIGGERED"
    assert len(list((project.store.dir / "rollover").glob("*.capsule.json"))) == n
    # an unreadable transcript is refused (exit 2), never read as "nothing due"
    rc = hoh(project, "rollover", "p", *master_args(project), "--model", "m",
             "--transcript", str(tmp_path / "missing.jsonl"))
    assert rc == 2 and "refused" in capsys.readouterr().err
    # the model is the operator's choice: without one, nothing is started
    assert hoh(project, "rollover", "p", *master_args(project)) == 2
    capsys.readouterr()


def test_the_command_line_refuses_without_the_masters_token(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path)
    monkeypatch.setattr(ro, "HerdrLauncher", ThreadLauncher)
    ThreadLauncher.instances.clear()
    base = ["rollover", "p", "--model", "m"]
    assert hoh(project, *base, "--session", "pred", "--epoch", "1") == 5          # no token
    wrong = tmp_path / "wrong.token"
    wrong.write_text("not-the-token")
    assert hoh(project, *base, "--session", "pred", "--epoch", "1",
               "--token-file", str(wrong)) == 5                                     # wrong token
    assert hoh(project, *base, "--session", "pred", "--epoch", "7",
               "--token-file", str(project.token)) == 5                             # wrong epoch
    assert hoh(project, *base, "--session", "mallory", "--epoch", "1",
               "--token-file", str(project.token)) == 5                             # wrong session
    err = capsys.readouterr().err
    assert err.count("refused") == 4
    assert not (project.store.dir / "rollover").exists()                            # nothing frozen
    assert not ThreadLauncher.instances and project.master.problems() == []


def test_hold_takes_a_free_program_holds_it_and_refuses_a_live_one(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    root = tmp_path / "hohroot"
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="waiting")]))
    project = SimpleNamespace(root=root, store=store)
    token_out = tmp_path / "hold.token"
    rc = hoh(project, "hold", "p", "--session", "first", "--hold-for", "0.3",
             "--interval", "0.05", "--token-out", str(token_out))
    out = capsys.readouterr().out
    assert rc == 0
    assert '"held": true' in out and '"epoch": 1' in out and '"epoch_reason": "INITIAL"' in out
    assert '"ended": "elapsed"' in out and '"released": true' in out
    assert (token_out.stat().st_mode & 0o777) == 0o600
    chain = EpochStore(store.dir).chain()
    assert [(e.epoch, e.master_id, e.reason) for e in chain] == [(1, "first", Reason.INITIAL)]
    probe = MasterAuthority(store.dir, "probe")
    assert probe.read_lease().released_at and token_out.read_text()    # a token was handed out
    assert token_out.read_text() not in out                           # but never printed
    # a live master elsewhere: refused, and nothing moves
    live = MasterAuthority(store.dir, "live")
    live.acquire()
    epochs_before = len(EpochStore(store.dir).chain())
    rc = hoh(project, "hold", "p", "--session", "second", "--hold-for", "0.3")
    assert rc == 5 and "refused" in capsys.readouterr().err
    assert len(EpochStore(store.dir).chain()) == epochs_before and live.problems() == []
    # a project that does not exist is not made up
    assert cli.main(["--root", str(root), "project", "hold", "ghost",
                     "--session", "x", "--hold-for", "0.1"]) == 2


# --------------------------------------------------------------------------- #
# K11 -- the ground moves after the ack: nothing is taken (B1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("what", ["head", "run"])
def test_b1_the_ground_moving_after_the_ack_stops_the_takeover_and_the_predecessor_reclaims(
        tmp_path, what):
    world = World(tmp_path)

    def move(rot):
        if what == "head":
            rot.w.observer.head_sha = "b" * 40
        else:
            rot.w.observer.runs[0]["stage"] = "VERIFYING"

    rot = Rotation(world, hooks={"handed": move}, takeover_s=60)
    result = rot.go()
    # the acknowledgement was a faithful look at the capsule as frozen ...
    ack = json.loads(ack_path(world.dir, result.capsule_id).read_text())
    assert ack["verified"] is True
    # ... the ground moved before the handover was taken, so it was not taken
    assert result.outcome is Outcome.RECLAIMED, result.detail
    [error] = rot.errors
    assert isinstance(error, RolloverError) and "ground moved" in str(error)
    assert what in str(error) or "runs" in str(error) or "head" in str(error)
    assert rot.taken is None and rot.succ_auth.epoch is None and rot.succ_auth.problems()
    # no SUCCESSION epoch exists; the predecessor took the program back by RECOVERY
    assert chain_reasons(world) == [Reason.INITIAL, Reason.RECOVERY]
    assert [e.master_id for e in EpochStore(world.dir).chain()] == ["pred", "pred"]
    assert Reason.SUCCESSION not in chain_reasons(world)
    assert not list((world.dir / "rollover").glob("*.receipt.json"))      # no receipt
    assert world.pred.problems() == []                                     # master again
    assert rot.max_masters <= 1                                            # never two masters
    assert events(world)[-2:] == ["handoff", "reclaimed"]


# --------------------------------------------------------------------------- #
# K12 -- the programme generation is the lease's
# --------------------------------------------------------------------------- #


def test_b2_a_generation_that_differs_refuses_the_rotation_before_anything_is_frozen(tmp_path):
    world = World(tmp_path, generation=3)
    rot = Rotation(world)
    result = rot.go(generation=2)                       # the project state says 2, the lease 3
    assert result.outcome is Outcome.REFUSED and result.predecessor_is_master
    assert "generation 2" in result.detail and "lease is at 3" in result.detail
    assert not (world.dir / "rollover").exists()        # nothing frozen, nothing logged
    assert not rot.launcher.started and world.pred.problems() == []
    assert_still_master(world)
    ok = Rotation(world).go(generation=3)
    assert ok.outcome is Outcome.COMPLETE
    cap = load_capsule(capsule_path(world.dir, ok.capsule_id))
    assert cap.programme_generation == 3
    assert rot.succ_auth.read_lease().program_generation == 3          # carried to the successor


# --------------------------------------------------------------------------- #
# K13 -- minors
# --------------------------------------------------------------------------- #


def test_a_freeze_that_raises_ends_the_cycle_durably_and_the_next_rotation_can_start(w):
    w.observer.boom["file_digest"] = OSError("the register is unreadable")
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.REFUSED and result.predecessor_is_master
    assert "unreadable" in result.detail
    # durable: a fresh reader of the log sees the cycle ended, not DRAINING
    fresh = RolloverLog(w.dir)
    assert [e["event"] for e in fresh.events()] == ["draining", "abort"]
    assert fresh.state() is State.ACTIVE
    assert aborted_path(w.dir, result.capsule_id).exists()
    assert not list((w.dir / "rollover").glob("*.capsule.json"))        # capsule-less state intact
    assert_still_master(w)
    assert not rot.launcher.started
    del w.observer.boom["file_digest"]
    assert Rotation(w).go().outcome is Outcome.COMPLETE                 # nothing is wedged


def test_a_draining_that_nobody_ended_expires_like_every_other_state(w):
    log = RolloverLog(w.dir, clock=w.clock)
    log.record("draining", capsule_id="rc-dead", expires_at="2026-10-07T12:05:00Z")
    rot = Rotation(w)
    assert rot.go().outcome is Outcome.IN_FLIGHT                        # not yet
    assert not rot.launcher.started
    w.clock.advance(301)
    result = rot.go()
    assert result.outcome is Outcome.COMPLETE                           # then it is ended on record
    rows = RolloverLog(w.dir).events()
    assert [r["event"] for r in rows][:3] == ["draining", "abort", "draining"]
    assert rows[1]["reason"] == "expired" and rows[1]["capsule_id"] == "rc-dead"
    assert rows[1]["expired_at"] == "2026-10-07T12:05:00Z"
    # a DRAINING the product writes itself carries its own deadline
    again = World(w.root / "own")
    stuck = Rotation(again)
    again.observer.boom["head"] = OSError("git timed out")
    assert stuck.go().outcome is Outcome.REFUSED
    first = RolloverLog(again.dir).events()[0]
    assert first["event"] == "draining" and first["expires_at"] > first["at"]


def test_a_handoff_refused_because_this_session_is_stale_is_reported_as_superseded(w):
    def stale(*a, **k):
        raise StaleMaster("stale master: superseded between the ack and the handoff")

    w.pred.handoff = stale
    rot = Rotation(w)
    result = rot.go()
    assert result.outcome is Outcome.SUPERSEDED
    assert result.predecessor_is_master is False                        # not "master": it is not
    assert "no longer the master" in result.detail
    assert rot.launcher.stopped and aborted_path(w.dir, result.capsule_id).exists()
    assert RolloverLog(w.dir).state() is State.ACTIVE and not rot.errors
    assert result.as_dict()["predecessor_is_master"] is False


def test_every_value_in_the_pointers_command_is_shell_quoted(tmp_path):
    nasty = {
        "root": str(tmp_path / "a root with spaces" / "it's"),
        "project_id": "p; touch PWNED",
        "capsule_file": str(tmp_path / 'x" && echo hi #' / "$(touch PWNED).capsule.json"),
        "capsule_digest": "ab`id`cd",
        "session": "s'ucc $HOME",
        "token_out": str(tmp_path / "t o k" / "master.token"),
    }
    text = pointer_prompt(**nasty)
    [command] = [ln for ln in text.splitlines() if ln.startswith("  hoh ")]
    argv = shlex.split(command)
    assert argv == ["hoh", "--root", nasty["root"], "project", "rollover-accept",
                    nasty["project_id"], "--capsule", nasty["capsule_file"], "--digest",
                    nasty["capsule_digest"], "--session", nasty["session"], "--token-out",
                    nasty["token_out"]]
    # a shell would see no expansion: each value that is special is wrapped by shlex.quote
    for key, value in nasty.items():
        if shlex.quote(value) != value:
            assert shlex.quote(value) in command, key
    assert shlex.join(argv) == command.strip()                          # exactly shlex's quoting
    assert text.isascii() and len(text) < 2500
    # what cannot be quoted safely is refused rather than sent
    with pytest.raises(RolloverError, match="ASCII"):
        pointer_prompt(**{**nasty, "root": "/home/üser"})
    with pytest.raises(RolloverError, match="characters"):
        pointer_prompt(**{**nasty, "root": "/" + "x" * 3000})


def test_hold_gives_the_lease_back_when_the_token_cannot_be_written(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    root = tmp_path / "hohroot"
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="waiting")]))
    project = SimpleNamespace(root=root, store=store)
    bad = tmp_path / "no-such-dir" / "hold.token"
    rc = hoh(project, "hold", "p", "--session", "first", "--hold-for", "5",
             "--interval", "0.05", "--token-out", str(bad))
    err = capsys.readouterr()
    assert rc == 5 and "cannot write the token" in err.err
    assert '"held"' not in err.out                                      # it never went on to hold
    lease = MasterAuthority(store.dir, "probe").read_lease()
    assert lease.released_at and "--token-out failed" in lease.released_reason
    # the lease is back: another session takes it at once
    assert MasterAuthority(store.dir, "next").acquire().epoch == 2
    # and the successor's side behaves the same: the lease is given back, no receipt
    world = World(tmp_path / "w")
    rot = Rotation(world, takes=False)
    take = {}

    def take_with_bad_token(r):
        r.take_tried = True
        succ = Successor(r.succ_auth, r.capsule_file(), observer=r.w.observer,
                         sleep=r.w.clock.advance, wait_s=20)
        try:
            succ.take(r.ack, token_out=str(tmp_path / "nowhere" / "master.token"))
        except TokenOutError as exc:
            take["error"] = exc

    rot.hooks["handed"] = take_with_bad_token
    result = rot.go()
    assert isinstance(take["error"], TokenOutError)
    assert not list((world.dir / "rollover").glob("*.receipt.json"))
    log = [json.loads(ln) for ln in (world.dir / "lease.log.jsonl").read_text().splitlines()]
    gave_back = [r for r in log if r["event"] == "release" and r["session"] == "succ"]
    assert len(gave_back) == 1 and "--token-out failed" in gave_back[0]["reason"]
    # the successor took epoch 2 and gave it back: someone has to be master, so the predecessor
    # took the programme back (RECOVERY) rather than reporting a role that moved to nobody
    assert result.outcome is Outcome.RECLAIMED and "gave it back" in result.detail
    assert chain_reasons(world) == [Reason.INITIAL, Reason.SUCCESSION, Reason.RECOVERY]
    assert world.pred.problems() == []
