"""The succession capsule (Layer A of context rollover): classed entries, a
product that derives the facts, a successor that derives them again.

`World` and `FakeObserver` here are shared with `test_rollover.py`. Nothing in
this file starts a process other than `git` on a repository it creates under
`tmp_path`, reads a transcript, or reads `~/.agents`; time is injected.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import hoh.succession_capsule as sc
from hoh.succession import MasterAuthority, StaleMaster
from hoh.succession_capsule import (
    Capsule, CapsuleError, CapsuleInput, Entry, EntryClass, LocalObserver, Section,
    ack_problems, build_ack, derive, freeze_capsule, load_capsule, sha256_text,
    verify_capsule,
)

T0 = "2026-10-07T12:00:00Z"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


class Clock:
    """An injected UTC clock that only moves when a test moves it."""

    def __init__(self, start: str = T0) -> None:
        self.now = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def __call__(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeObserver:
    """Stands in for the world: what the derivers will report."""

    def __init__(self, repo: str, runs_root: str, files: dict[str, str]) -> None:
        self.repo = repo
        self.runs_root = runs_root
        self.files = dict(files)
        self.runs: list[dict] = [
            {"run_id": "r1", "stage": "DEVELOPING", "condition": "ACTIVE", "iteration": 2,
             "attempt": 1, "retry_after": None, "candidate": "cand-1"},
            {"run_id": "r2", "stage": "VERIFYING", "condition": "BLOCKED", "iteration": 1,
             "attempt": 3, "retry_after": "2026-10-07T18:00:00Z", "candidate": None},
        ]
        self.refs: dict[tuple[str, str], dict] = {
            (repo, "cand/x"): {"sha": "c" * 40, "merged": False}}
        self.head_sha = "a" * 40
        self.branch_name = "main"
        self.boom: dict[str, Exception] = {}
        self.calls: list[str] = []

    def _maybe(self, name: str) -> None:
        self.calls.append(name)
        if name in self.boom:
            raise self.boom[name]

    def file_digest(self, path: str) -> str:
        self._maybe("file_digest")
        return self.files.get(path, "MISSING")

    def run_set(self, runs_root: str) -> list[dict]:
        self._maybe("run_set")
        return copy.deepcopy(self.runs)

    def ref_state(self, repo: str, ref: str) -> dict:
        self._maybe("ref_state")
        return copy.deepcopy(self.refs.get((repo, ref), {"sha": "MISSING", "merged": False}))

    def head(self, repo: str) -> str:
        self._maybe("head")
        return self.head_sha

    def branch(self, repo: str) -> str:
        self._maybe("branch")
        return self.branch_name


OPERATOR_TEXT = "  keep the review gate on.\n\tDo NOT merge qaw9 yet;\r\nask me first — ok?  \n"


class World:
    """One program, its master, an injected clock and a fake observer."""

    def __init__(self, tmp_path: Path, *, generation: int = 1, ttl: int = 1800) -> None:
        self.root = tmp_path
        self.dir = tmp_path / "projects" / "p"
        self.dir.mkdir(parents=True)
        self.clock = Clock()
        self.ttl = ttl
        self.repo = str(tmp_path / "repo")
        self.runs_root = str(tmp_path / "runs")
        self.plan = str(tmp_path / "PLAN.md")
        self.policy = str(tmp_path / "POLICY.md")
        self.review = str(tmp_path / "review-a.md")
        self.findings = str(tmp_path / "findings.json")
        self.observer = FakeObserver(self.repo, self.runs_root, {
            self.plan: "sha256:plan-1", self.policy: "sha256:policy-1",
            self.review: "sha256:review-1", self.findings: "sha256:findings-1"})
        self.generation = generation
        self.pred = self.authority("pred")
        self.pred.acquire(program_generation=generation)

    def authority(self, session: str, *, pid: int | None = None,
                  ttl: int | None = None) -> MasterAuthority:
        return MasterAuthority(self.dir, session, ttl_seconds=ttl or self.ttl,
                               clock=self.clock, pidns=lambda: "pidns-test", pid=pid)

    def input(self, *, decisions: bool = True, advisory: bool = True,
              model: str = "claude-opus-5-5") -> CapsuleInput:
        derive_rows = [
            {"entry_id": "plan", "section": "REGISTER", "deriver": "file_digest",
             "params": {"path": self.plan}},
            {"entry_id": "policy", "section": "REGISTER", "deriver": "file_digest",
             "params": {"path": self.policy}},
            {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
             "params": {"runs_root": self.runs_root}},
            {"entry_id": "cand", "section": "CANDIDATES", "deriver": "ref_state",
             "params": {"repo": self.repo, "ref": "cand/x"}},
            {"entry_id": "review", "section": "REVIEWS", "deriver": "file_digest",
             "params": {"path": self.review}},
            {"entry_id": "findings", "section": "REVIEWS", "deriver": "file_digest",
             "params": {"path": self.findings}},
        ]
        return CapsuleInput.model_validate({
            "successor_model": model,
            "derive": derive_rows,
            "decisions": ([{"entry_id": "d1", "text": OPERATOR_TEXT, "source": "operator",
                            "at": "2026-10-07T11:00:00Z", "status": "open"}]
                          if decisions else []),
            "advisory": ([{"entry_id": "bg", "section": "BACKGROUND",
                           "text": "the programme is on phase B"},
                          {"entry_id": "dnt", "section": "DO_NOT_TOUCH",
                           "text": "the integration checkout"}] if advisory else [])})

    def freeze(self, successor: str = "succ", *, inp: CapsuleInput | None = None,
               **kw) -> sc.FrozenCapsule:
        kw.setdefault("ack_window_s", 600)
        return freeze_capsule(self.pred, inp or self.input(), directory=self.dir,
                              observer=self.observer, repo=self.repo, project_id="p",
                              successor_session=successor, **kw)

    def verify(self, cap: Capsule, *, session: str | None = "succ", handover: bool = False,
               probe: MasterAuthority | None = None, **kw) -> sc.Verification:
        return verify_capsule(cap, probe=probe or self.authority(session or "verifier"),
                              observer=self.observer, session=session, handover=handover,
                              **kw)

    def snapshot(self) -> dict[str, tuple]:
        """Every file under the world's root, by name, with size, mtime and bytes."""
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(self.root))] = (
                    st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        return out


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def rewrite(path: Path, edit, *, redigest: bool = False) -> Capsule:
    """Tamper with a capsule file; with `redigest` the forgery is self-consistent."""
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    if redigest:
        data["capsule_digest"] = Capsule.model_validate(data).compute_digest()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return load_capsule(path)


def entry(cap: Capsule, entry_id: str) -> Entry:
    return next(e for e in cap.entries if e.entry_id == entry_id)


# --------------------------------------------------------------------------- #
# K2 -- the capsule is frozen by the product, from an observer, by the live master
# --------------------------------------------------------------------------- #


def test_a_frozen_capsule_verifies_against_the_state_it_was_derived_from(world):
    frozen = world.freeze()
    cap = load_capsule(frozen.path)
    assert frozen.path == world.dir / "rollover" / f"{cap.capsule_id}.capsule.json"
    assert (frozen.path.stat().st_mode & 0o777) == 0o600
    assert cap.schema_version == "hoh-succession-capsule/1"
    assert (cap.predecessor_session, cap.predecessor_epoch) == ("pred", 1)
    assert (cap.successor_session, cap.successor_epoch) == ("succ", 2)
    assert cap.predecessor_token_sha256 == world.pred.read_lease().token_sha256
    assert cap.head == "a" * 40 and cap.branch == "main"
    assert cap.ack_deadline == "2026-10-07T12:10:00Z"
    assert cap.capsule_digest == cap.compute_digest()
    assert cap == frozen.capsule                        # what was written is what was frozen
    ver = world.verify(cap)
    assert ver.ok and ver.differences == []
    assert ver.observed_head == "a" * 40
    # written once: the same id is never replaced
    with pytest.raises(FileExistsError):
        world.freeze(capsule_id=cap.capsule_id)
    assert load_capsule(frozen.path) == cap


def test_derived_values_come_from_the_observer_and_never_from_the_author(world):
    # the author has no field for a value
    with pytest.raises(ValidationError):
        sc.DeriveIn.model_validate({"entry_id": "x", "section": "REGISTER",
                                    "deriver": "file_digest", "params": {"path": "p"},
                                    "value": "sha256:forged"})
    with pytest.raises(ValidationError):
        CapsuleInput.model_validate({"successor_model": "m", "derive": [], "values": {}})
    world.observer.files[world.plan] = "sha256:the-observers-word"
    cap = world.freeze().capsule
    assert entry(cap, "plan").value == "sha256:the-observers-word"
    assert entry(cap, "runs").value == world.observer.runs
    assert entry(cap, "cand").value == {"sha": "c" * 40, "merged": False}
    # and a derivation that cannot be run is refused, never filled in
    world.observer.boom["file_digest"] = OSError("unreadable")
    before = sorted(p.name for p in (world.dir / "rollover").iterdir())
    with pytest.raises(OSError):
        world.freeze()
    assert sorted(p.name for p in (world.dir / "rollover").iterdir()) == before
    with pytest.raises(CapsuleError):
        derive(world.observer, "made_up", {})
    with pytest.raises(CapsuleError):
        derive(world.observer, "file_digest", {"path": "p", "extra": 1})


def test_a_capsule_without_the_run_set_or_the_register_is_not_frozen(world):
    def without(entry_id: str) -> CapsuleInput:
        inp = world.input()
        return inp.model_copy(update={"derive": tuple(d for d in inp.derive
                                                      if d.entry_id != entry_id)})

    with pytest.raises(CapsuleError, match="run_set"):
        world.freeze(inp=without("runs"))
    inp = world.input()
    no_register = inp.model_copy(update={"derive": tuple(
        d for d in inp.derive if d.section is not Section.REGISTER)})
    with pytest.raises(CapsuleError, match="REGISTER"):
        world.freeze(inp=no_register)
    dup = inp.model_copy(update={"derive": inp.derive + (inp.derive[0],)})
    with pytest.raises(CapsuleError, match="duplicate"):
        world.freeze(inp=dup)
    assert not list((world.dir / "rollover").glob("*.capsule.json"))
    # an empty run set is a fact, and is bound
    world.observer.runs = []
    cap = world.freeze().capsule
    assert entry(cap, "runs").value == []
    assert world.verify(cap).ok


def test_only_the_live_master_can_freeze_a_capsule(world):
    stranger = world.authority("stranger")
    with pytest.raises(StaleMaster):
        freeze_capsule(stranger, world.input(), directory=world.dir,
                       observer=world.observer, repo=world.repo, project_id="p",
                       successor_session="succ")
    world.pred.handoff("succ", reason="rotate")           # the old master is fenced now
    with pytest.raises(StaleMaster):
        world.freeze()
    assert not (world.dir / "rollover").exists() \
        or not list((world.dir / "rollover").iterdir())
    # the live master may not name itself as the successor either
    w2 = World(world.root / "again")
    with pytest.raises(CapsuleError):
        w2.freeze(successor="pred")


def test_entries_are_classed_and_the_classes_have_a_shape():
    fact = dict(entry_id="a", klass="DERIVED_FACT", section="REGISTER",
                deriver="file_digest", params={"path": "x"}, value="sha256:1")
    assert Entry.model_validate(fact).klass is EntryClass.DERIVED_FACT
    bad_facts = [
        {**fact, "value": None},                         # the product fills the value
        {**fact, "deriver": "run_set", "params": {"runs_root": "x"}},   # REGISTER takes files
        {**fact, "deriver": "nope"},
        {**fact, "params": {"path": "x", "more": 1}},
        {**fact, "section": "DECISIONS"},                # a fact is not a decision
        {**fact, "text": "a note"},                      # a fact carries no text
        {**fact, "klass": "OPERATOR_DECISION"},          # the section fixes the class
        {**fact, "entry_id": " a"},
    ]
    for bad in bad_facts:
        with pytest.raises(ValidationError):
            Entry.model_validate(bad)
    decision = dict(entry_id="d", klass="OPERATOR_DECISION", section="DECISIONS",
                    text="no\n", source="operator", at=T0, status="open",
                    text_sha256=sha256_text("no\n"))
    assert Entry.model_validate(decision).text == "no\n"
    for bad in ({**decision, "status": "answered"}, {**decision, "source": ""},
                {**decision, "text_sha256": None}, {**decision, "deriver": "file_digest"},
                {**decision, "section": "BACKGROUND"}, {**decision, "text": "  \n"}):
        with pytest.raises(ValidationError):
            Entry.model_validate(bad)
    advisory = dict(entry_id="n", klass="ADVISORY_CONTEXT", section="BACKGROUND",
                    text="a note")
    assert Entry.model_validate(advisory).text == "a note"
    for bad in ({**advisory, "value": 1}, {**advisory, "status": "open"},
                {**advisory, "text": None}, {**advisory, "section": "REGISTER"},
                {**advisory, "klass": "DERIVED_FACT"}):
        with pytest.raises(ValidationError):
            Entry.model_validate(bad)


# --------------------------------------------------------------------------- #
# K3 -- verification refuses, names, and reports every difference
# --------------------------------------------------------------------------- #


def test_b_a_tampered_capsule_is_refused(world):
    frozen = world.freeze()
    # a value changed in place, digest left alone
    tampered = rewrite(frozen.path, lambda d: d["entries"][0].update(value="sha256:other"))
    ver = world.verify(tampered)
    assert not ver.ok and "digest" in ver.kinds()
    # the pointer's digest is not the capsule's
    clean = load_capsule(world.freeze(capsule_id="rc-second").path)
    ver = world.verify(clean, pointer_digest="0" * 64)
    assert not ver.ok and "pointer" in ver.kinds()
    assert world.verify(clean, pointer_digest=clean.capsule_digest).ok
    # a self-consistent forgery has a good digest and is caught by re-derivation
    forged = rewrite(world.dir / "rollover" / "rc-second.capsule.json",
                     lambda d: d["entries"][0].update(value="sha256:forged"), redigest=True)
    ver = world.verify(forged)
    assert not ver.ok and "digest" not in ver.kinds() and "plan" in ver.subjects()
    # a wrong schema is named
    other = rewrite(world.dir / "rollover" / "rc-second.capsule.json",
                    lambda d: d.update(schema_version="hoh-succession-capsule/2"))
    assert "schema" in world.verify(other).kinds()


def test_c_a_moved_head_or_branch_refuses(world):
    cap = world.freeze().capsule
    world.observer.head_sha = "b" * 40
    ver = world.verify(cap)
    assert not ver.ok and ver.kinds() == {"head"}
    assert ver.observed_head == "b" * 40
    world.observer.head_sha = "a" * 40
    world.observer.branch_name = "elsewhere"
    ver = world.verify(cap)
    assert not ver.ok and ver.kinds() == {"branch"}
    world.observer.boom["head"] = OSError("no repository")      # unreadable is a difference
    assert "head" in world.verify(cap).kinds()


def test_d_a_changed_run_set_refuses_and_names_the_run_set(world):
    cap = world.freeze().capsule
    world.observer.runs[0]["stage"] = "VERIFYING"
    ver = world.verify(cap)
    assert not ver.ok and "runs" in ver.subjects()
    assert any("run_set" in d.detail for d in ver.differences if d.subject == "runs")
    world.observer.runs[0]["stage"] = "DEVELOPING"
    assert world.verify(cap).ok
    # J: only the retry time of a parked run moved
    world.observer.runs[1]["retry_after"] = "2026-10-07T19:00:00Z"
    ver = world.verify(cap)
    assert not ver.ok and ver.subjects() == {"runs"}
    world.observer.runs[1]["retry_after"] = "2026-10-07T18:00:00Z"
    # a run that appeared, and one that finished
    world.observer.runs.append({"run_id": "r3", "stage": "NEW", "condition": "ACTIVE",
                                "iteration": 0, "attempt": 0, "retry_after": None,
                                "candidate": None})
    assert "runs" in world.verify(cap).subjects()
    world.observer.runs.pop()
    world.observer.runs.pop(0)
    assert "runs" in world.verify(cap).subjects()


def test_k_a_candidate_that_moved_or_merged_and_a_changed_review_refuse(world):
    cap = world.freeze().capsule
    key = (world.repo, "cand/x")
    world.observer.refs[key] = {"sha": "d" * 40, "merged": False}      # the candidate moved
    assert world.verify(cap).subjects() == {"cand"}
    world.observer.refs[key] = {"sha": "c" * 40, "merged": True}       # accepted, now merged
    ver = world.verify(cap)
    assert ver.subjects() == {"cand"}
    assert "merged" in ver.differences[0].detail
    world.observer.refs[key] = {"sha": "c" * 40, "merged": False}
    world.observer.files[world.review] = "sha256:review-2"             # a changed review
    assert world.verify(cap).subjects() == {"review"}
    world.observer.files[world.review] = "sha256:review-1"
    world.observer.files[world.findings] = "sha256:findings-2"         # a changed finding
    assert world.verify(cap).subjects() == {"findings"}
    world.observer.files[world.findings] = "sha256:findings-1"
    assert world.verify(cap).ok


def test_a_register_file_that_changed_or_disappeared_refuses(world):
    cap = world.freeze().capsule
    world.observer.files[world.plan] = "sha256:plan-2"
    ver = world.verify(cap)
    assert not ver.ok and ver.subjects() == {"plan"}
    assert ver.freshness == ["plan"]                       # P2-20: a moved source is stale
    del world.observer.files[world.plan]                   # and one that is gone
    ver = world.verify(cap)
    assert not ver.ok and "MISSING" in ver.differences[0].detail
    assert ver.freshness == ["plan"]
    world.observer.files[world.plan] = "sha256:plan-1"
    assert world.verify(cap).ok
    # a capsule frozen while a register file was already missing binds the absence
    del world.observer.files[world.policy]
    absent = world.freeze(capsule_id="rc-absent").capsule
    assert entry(absent, "policy").value == "MISSING" and world.verify(absent).ok
    world.observer.files[world.policy] = "sha256:policy-1"
    assert "policy" in world.verify(absent).subjects()


def test_the_capsule_belongs_to_one_hand_over(world):
    cap = world.freeze().capsule
    assert world.verify(cap).ok
    # the named session only
    ver = world.verify(cap, session="someone-else")
    assert "successor" in ver.kinds()
    # the deadline
    world.clock.advance(601)
    ver = world.verify(cap)
    assert not ver.ok and "deadline" in ver.kinds()
    world.clock.advance(-601)
    # the master released and acquired again: another epoch, another token
    world.pred.release(reason="restart")
    again = world.authority("pred")
    again.acquire(program_generation=1)
    ver = world.verify(cap)
    assert not ver.ok and {"chain", "lease"} <= ver.kinds()
    assert {"epoch", "token"} <= ver.subjects()
    # and a capsule frozen for epoch 2 does not serve epoch 1's hand-over either
    w2 = World(world.root / "other")
    cap_a = w2.freeze().capsule
    w2.pred.release(reason="x")
    w3 = w2.authority("pred")
    w3.acquire(program_generation=1)
    assert not w2.verify(cap_a).ok
    # the predecessor must be live, not released, for a pre-handover verification
    w4 = World(world.root / "released")
    cap_b = w4.freeze().capsule
    w4.pred.handoff("succ", reason="rotate")
    ver = w4.verify(cap_b)
    assert "released" in ver.subjects()
    assert w4.verify(cap_b, handover=True).ok          # the successor's second look


def test_every_difference_is_reported_not_only_the_first(world):
    cap = world.freeze().capsule
    world.observer.head_sha = "b" * 40
    world.observer.branch_name = "other"
    world.observer.runs[0]["attempt"] = 9
    world.observer.refs[(world.repo, "cand/x")] = {"sha": "e" * 40, "merged": True}
    world.observer.files[world.plan] = "sha256:plan-2"
    world.observer.files[world.review] = "MISSING"
    world.clock.advance(3600)
    ver = world.verify(cap, session="someone-else", pointer_digest="f" * 64)
    assert not ver.ok
    assert {"head", "branch", "fact", "deadline", "successor", "pointer"} <= ver.kinds()
    assert {"runs", "cand", "plan", "review"} <= ver.subjects()
    assert len([d for d in ver.differences if d.kind == "fact"]) == 4
    # and a tampered decision, in the same report
    tampered = rewrite(world.dir / "rollover" / f"{cap.capsule_id}.capsule.json",
                       lambda d: next(e for e in d["entries"] if e["entry_id"] == "d1")
                       .update(text="something else"))
    ver = world.verify(tampered, session="succ")
    assert {"decision", "digest"} <= ver.kinds()


# --------------------------------------------------------------------------- #
# K4 -- the operator's words, and advisory context
# --------------------------------------------------------------------------- #


def test_i_an_operator_decision_survives_byte_for_byte_and_stays_open(world):
    frozen = world.freeze()
    cap = load_capsule(frozen.path)
    d = entry(cap, "d1")
    assert d.text == OPERATOR_TEXT                         # whitespace, CRLF, tab, unicode
    assert d.text.encode("utf-8") == OPERATOR_TEXT.encode("utf-8")
    assert d.text_sha256 == hashlib.sha256(OPERATOR_TEXT.encode("utf-8")).hexdigest()
    assert (d.klass, d.status, d.source, d.at) == (
        EntryClass.OPERATOR_DECISION, "open", "operator", "2026-10-07T11:00:00Z")
    on_disk = json.loads(frozen.path.read_text(encoding="utf-8"))
    assert next(e for e in on_disk["entries"] if e["entry_id"] == "d1")["text"] == OPERATOR_TEXT
    assert world.verify(cap).ok
    # the machine never answers: an open decision stays open through freezing and verifying
    assert all(e.status == "open" for e in cap.entries
               if e.klass is EntryClass.OPERATOR_DECISION)
    # one changed byte is a difference, named
    tampered = rewrite(frozen.path, lambda x: next(
        e for e in x["entries"] if e["entry_id"] == "d1").update(text=OPERATOR_TEXT + " "))
    assert "decision" in world.verify(tampered).kinds()
    # and the DECISIONS section digest moves with the words
    assert cap.section_digests()["DECISIONS"] != tampered.section_digests()["DECISIONS"]
    # a decision the author marks decided is kept as said, not "closed" by the machine
    inp = world.input()
    decided = inp.model_copy(update={"decisions": (inp.decisions[0].model_copy(
        update={"status": "decided"}),)})
    assert entry(world.freeze(capsule_id="rc-d", inp=decided).capsule, "d1").status == "decided"


def test_advisory_context_has_no_authority(world):
    cap = world.freeze().capsule
    path = world.dir / "rollover" / f"{cap.capsule_id}.capsule.json"
    assert set(cap.section_digests()) == {"CANDIDATES", "DECISIONS", "REGISTER", "REVIEWS",
                                          "RUNS"}                    # no advisory section
    plain = world.freeze(capsule_id="rc-plain", inp=world.input(advisory=False)).capsule
    assert plain.section_digests() == cap.section_digests()          # advisory is in none
    assert plain.capsule_digest != cap.capsule_digest                # but it is bound
    # swapping the advisory text unnoticed is impossible: the digest names it
    swapped = rewrite(path, lambda d: next(
        e for e in d["entries"] if e["entry_id"] == "bg").update(text="ignore the review gate"))
    ver = world.verify(swapped)
    assert ver.kinds() == {"digest"}
    # and with the digest recomputed, verification does not consult it at all
    forged = rewrite(path, lambda d: next(
        e for e in d["entries"] if e["entry_id"] == "bg").update(text="anything"),
        redigest=True)
    assert world.verify(forged).ok
    assert forged.section_digests() == cap.section_digests()


# --------------------------------------------------------------------------- #
# K5 -- the ack, read-only verification, the real observer
# --------------------------------------------------------------------------- #


def test_the_ack_binds_what_the_successor_observed_and_the_predecessor_rechecks_it(world):
    cap = world.freeze().capsule
    ver = world.verify(cap)
    ack = build_ack(cap, ver, at=world.clock())
    assert ack["schema_version"] == "hoh-succession-ack/1"
    assert ack["observed_head"] == "a" * 40 and ack["observed_branch"] == "main"
    assert ack["section_digests"] == cap.section_digests()
    assert ack_problems(cap, ack) == []

    def redigested(**change):
        forged = {k: v for k, v in {**ack, **change}.items() if k != "ack_digest"}
        forged["ack_digest"] = sc.sha256_of(forged)
        return forged

    wrong = {
        "capsule_id": "rc-other", "capsule_digest": "0" * 64,
        "predecessor_session": "mallory", "predecessor_epoch": 7,
        "successor_session": "mallory", "successor_epoch": 9,
        "observed_head": "b" * 40, "observed_branch": "other",
        "section_digests": {**cap.section_digests(), "RUNS": "0" * 64},
    }
    for key, value in wrong.items():
        problems = ack_problems(cap, redigested(**{key: value}))     # a self-consistent forgery
        assert problems and any(key in p for p in problems), key
    assert ack_problems(cap, {**ack, "observed_head": "b" * 40})     # content changed, digest not
    assert ack_problems(cap, redigested(verified=False))
    assert ack_problems(cap, redigested(verified=True, mismatches=["head[x]: moved"]))
    assert ack_problems(cap, redigested(schema_version="hoh-succession-ack/2"))
    assert ack_problems(cap, ["not", "an", "object"]) == ["the acknowledgement is not a JSON object"]
    # a failed verification is acknowledged as failed, with its mismatches
    world.observer.head_sha = "b" * 40
    bad = build_ack(cap, world.verify(cap), at=world.clock())
    assert bad["verified"] is False and bad["mismatches"]
    assert bad["observed_head"] == "b" * 40
    assert ack_problems(cap, bad)
    # an ack of another capsule is not this one's
    world.observer.head_sha = "a" * 40
    other = world.freeze(capsule_id="rc-other").capsule
    assert ack_problems(other, ack)


def test_verifying_is_read_only(world):
    cap = world.freeze().capsule

    class NoWrite(MasterAuthority):
        def _boom(self, *a, **k):
            raise AssertionError("verification wrote or took authority")
        acquire = renew = handoff = release = _write = _boom

    probe = NoWrite(world.dir, "succ", clock=world.clock, pidns=lambda: "pidns-test")
    lease_before = world.pred.read_lease()
    files_before = world.snapshot()
    ver = verify_capsule(cap, probe=probe, observer=world.observer, session="succ")
    assert ver.ok
    # also when it refuses
    world.observer.head_sha = "b" * 40
    assert not verify_capsule(cap, probe=probe, observer=world.observer, session="succ").ok
    assert world.snapshot() == files_before               # not a byte, not an mtime
    assert world.pred.read_lease() == lease_before
    assert world.pred.problems() == []                    # the master is still the master


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}, check=True)
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def test_the_local_observer_runs_only_read_only_git_and_reads_the_real_thing(tmp_path):
    repo = _make_repo(tmp_path)
    seen: list[list[str]] = []

    def spy(argv, **kw):
        seen.append(list(argv))
        return subprocess.run(argv, **kw)

    obs = LocalObserver(run=spy)
    head = obs.head(str(repo))
    assert head == _git(repo, "rev-parse", "HEAD")
    assert obs.branch(str(repo)) == "main"
    assert obs.ref_state(str(repo), "main") == {"sha": head, "merged": True}
    assert obs.ref_state(str(repo), "no-such-ref") == {"sha": "MISSING", "merged": False}
    assert seen, "the observer ran no git"
    for argv in seen:
        assert argv[:3] == ["git", "-C", str(repo)]
        assert argv[3] in sc.GIT_READ_ONLY, argv           # nothing else is ever run
    assert sc.GIT_READ_ONLY == {"rev-parse", "merge-base"}
    # anything that could write is refused before it is run
    count = len(seen)
    for forbidden in ("checkout", "commit", "reset", "clean", "fetch", "gc", "update-ref",
                      "branch", "merge", "status"):
        with pytest.raises(CapsuleError, match="read-only"):
            obs._git(str(repo), forbidden, "-x")
    with pytest.raises(CapsuleError):
        obs._git(str(repo))
    with pytest.raises(CapsuleError, match="not a ref"):
        obs.ref_state(str(repo), "--output=/tmp/x")        # no option injection through a ref
    assert len(seen) == count
    # and the repository is exactly as it was
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == head


def test_the_local_observer_reads_a_real_repository(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "cand")
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "two")
    cand = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    obs = LocalObserver()
    main = _git(repo, "rev-parse", "main")
    assert obs.ref_state(str(repo), "cand") == {"sha": cand, "merged": False}   # accepted, unmerged
    _git(repo, "merge", "-q", "--ff-only", "cand")
    assert obs.ref_state(str(repo), "cand") == {"sha": cand, "merged": True}
    assert obs.head(str(repo)) == cand != main
    # files
    f = tmp_path / "plan.md"
    f.write_text("plan\n")
    assert obs.file_digest(str(f)) == "sha256:" + hashlib.sha256(b"plan\n").hexdigest()
    assert obs.file_digest(str(tmp_path / "gone.md")) == "MISSING"
    assert obs.file_digest(str(tmp_path)) == "MISSING"            # a directory is not a file
    # runs: only ACTIVE, PAUSED and BLOCKED, read as plain JSON
    runs = tmp_path / "runs"
    for rid, cond, extra in (
            ("run-a", "ACTIVE", {"working_candidate": {"candidate_id": "c-1"}}),
            ("run-b", "BLOCKED", {"retry_after": "2026-10-08T00:00:00Z"}),
            ("run-c", "PAUSED", {}), ("run-d", "FAILED", {}), ("run-e", "CANCELLED", {})):
        (runs / rid).mkdir(parents=True)
        (runs / rid / "state.json").write_text(json.dumps({
            "run_id": rid, "stage": "DEVELOPING", "condition": cond, "iteration": 3,
            "attempt": 2, **extra}))
    (runs / "projects").mkdir()                                   # not a run: no state.json
    (runs / "run-x").mkdir()
    (runs / "run-x" / "state.json").write_text("{not json")
    before = sorted(str(p.relative_to(tmp_path)) for p in runs.rglob("*"))
    got = obs.run_set(str(runs))
    assert [r["run_id"] for r in got] == ["run-a", "run-b", "run-c", "run-x"]
    assert got[0] == {"run_id": "run-a", "stage": "DEVELOPING", "condition": "ACTIVE",
                      "iteration": 3, "attempt": 2, "retry_after": None, "candidate": "c-1"}
    assert got[1]["retry_after"] == "2026-10-08T00:00:00Z"
    assert got[3]["condition"] == "UNREADABLE"             # an unreadable run is not a gap
    assert obs.run_set(str(tmp_path / "no-runs")) == []
    assert sorted(str(p.relative_to(tmp_path)) for p in runs.rglob("*")) == before   # nothing written
    # the real observer drives a real freeze + verify
    w = World(tmp_path / "w")
    w.repo = str(repo)
    real = LocalObserver()
    inp = CapsuleInput.model_validate({"successor_model": "m", "derive": [
        {"entry_id": "plan", "section": "REGISTER", "deriver": "file_digest",
         "params": {"path": str(f)}},
        {"entry_id": "runs", "section": "RUNS", "deriver": "run_set",
         "params": {"runs_root": str(runs)}},
        {"entry_id": "cand", "section": "CANDIDATES", "deriver": "ref_state",
         "params": {"repo": str(repo), "ref": "cand"}}]})
    cap = freeze_capsule(w.pred, inp, directory=w.dir, observer=real, repo=str(repo),
                         project_id="p", successor_session="succ").capsule
    assert cap.head == cand and cap.branch == "main"
    assert verify_capsule(cap, probe=w.authority("succ"), observer=real, session="succ").ok
    f.write_text("plan, changed\n")
    ver = verify_capsule(cap, probe=w.authority("succ"), observer=real, session="succ")
    assert not ver.ok and ver.subjects() == {"plan"}


# --------------------------------------------------------------------------- #
# K12 -- the programme generation is the lease's
# --------------------------------------------------------------------------- #


def test_b2_the_programme_generation_is_derived_never_supplied(tmp_path, monkeypatch):
    w = World(tmp_path, generation=3)
    # the author has no field for it and cannot add one
    with pytest.raises(ValidationError):
        CapsuleInput.model_validate({**w.input().model_dump(mode="json"),
                                     "programme_generation": 9})
    with pytest.raises(ValidationError):
        CapsuleInput.model_validate({**w.input().model_dump(mode="json"),
                                     "program_generation": 9})
    assert "programme_generation" not in CapsuleInput.model_fields
    cap = w.freeze().capsule
    assert cap.programme_generation == 3                      # the lease's, not a default
    assert w.verify(cap, project_generation=3).ok
    # a project state that disagrees with the lease: no capsule is written
    n = len(list((w.dir / "rollover").glob("*.capsule.json")))
    with pytest.raises(CapsuleError, match="generation"):
        w.freeze(generation=2, capsule_id="rc-gen2")
    assert len(list((w.dir / "rollover").glob("*.capsule.json"))) == n
    assert w.freeze(generation=3, capsule_id="rc-gen3").capsule.programme_generation == 3
    # verify names a project generation that moved
    ver = w.verify(cap, project_generation=4)
    assert not ver.ok and "project_generation" in ver.subjects()
    assert (ver.programme_generation, ver.lease_generation, ver.project_generation) == (3, 3, 4)
    # a self-consistent forgery of another generation: right digest, wrong programme
    path = w.dir / "rollover" / f"{cap.capsule_id}.capsule.json"
    forged = rewrite(path, lambda d: d.update(programme_generation=5), redigest=True)
    assert forged.capsule_digest == forged.compute_digest()
    ver = w.verify(forged)
    assert not ver.ok and "digest" not in ver.kinds()
    assert "programme_generation" in ver.subjects()
    assert ver.lease_generation == 3 and ver.programme_generation == 5
    # P2-20's rule is given the *derived* generation, never the capsule's own claim
    given: list[int] = []
    real = sc.hoh_capsule.may_dispatch

    def spy(capsule, *, current, program_generation):
        given.append(program_generation)
        return real(capsule, current=current, program_generation=program_generation)

    monkeypatch.setattr(sc.hoh_capsule, "may_dispatch", spy)
    ver = w.verify(forged)
    assert given == [3]
    assert any(d.kind == "freshness" and "generation" in d.detail for d in ver.differences)
    # no lease at all: the generation cannot be derived, so nothing passes
    w2 = World(tmp_path / "second")
    cap2 = w2.freeze().capsule
    w2.pred.release(reason="done")
    lease_file = w2.dir / "lease.json"
    lease_file.rename(w2.dir / "lease.json.moved-aside")
    ver = w2.verify(cap2)
    assert not ver.ok and ver.lease_generation is None
    assert "programme_generation" in ver.subjects()
