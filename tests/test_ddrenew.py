"""The renewal of a parked bound (V3.3 P3-42, slice 2): `doubledown.renew_bound`,
`hoh double-down renew`.

Amendment A8: an RND_NEGATIVE block before its fifth round that reaches a bound is PARKED and "is
resumed when the bound is renewed". Slice 1 parked it and showed it as a decision; this slice gives
the person the act. A renewal is one create-only record naming who and why; it grants a fixed
amount; the bound is reached again after that much and parks again, visibly.

Every test builds real runs and projects under `tmp_path` through the product's own stores and
drives the analysts with a scripted fake: no agent, no provider, no process. The grants and the
limits are written out as literals on purpose: they are the contract.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import types
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hoh import cli, doubledown
from hoh.contracts import Budgets, Condition, RunState, Usage
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


# --- builders ------------------------------------------------------------------------------ #

def _run(root, rid, condition=Condition.BLOCKED, **kw) -> RunStore:
    store = RunStore(root, rid)
    store.write_state(RunState(run_id=rid, repo_path="/x", project_name="p", spec_path="/x/s.md",
                               spec_digest="a" * 16, policy_digest="b" * 16,
                               profile_digest="c" * 16, condition=condition, **kw))
    return store


def _project(root, nodes, pid="p") -> ProjectStore:
    store = ProjectStore(root, pid)
    store.write_state(ProjectState(project_id=pid, repo_path="/x", nodes=list(nodes)))
    return store


def _loops_exhausted():
    return dict(blocked_reason="3 loops without traceable progress (limit 3)",
                usage=Usage(iterations=3, loops_without_progress=3),
                budgets=Budgets(max_loops_without_progress=3))


def _hyp(path, outcome="open", evidence=("saw it",)):
    return {"path_id": path, "statement": "s", "tested": True, "outcome": outcome,
            "evidence": list(evidence)}


def _report(perspective, hyps, proposal=None, unsolvable=None):
    return json.dumps({"perspective": perspective, "hypotheses": hyps, "proposal": proposal,
                       "unsolvable": unsolvable})


def _open_hyps(round_no, perspective):
    return _report(perspective, [_hyp(f"open-{round_no}-{perspective}", "open", evidence=())])


class Fake:
    """Scripted analysts: `script[(round, perspective)]` is the answer text, or an exception."""

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default
        self.calls: list[tuple[int, str]] = []

    @contextmanager
    def factory(self, halt):
        yield self

    def analyse(self, perspective, prompt, *, round_no):
        self.calls.append((round_no, perspective))
        value = self.script.get((round_no, perspective), self.default)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(round_no, perspective)
        return value


class _Health:
    def __call__(self, root, *, now=None):
        return types.SimpleNamespace(complete=True, providers=())


def _dossiers(owner: Path) -> list[Path]:
    dd = owner / "dd"
    return sorted(p for p in dd.iterdir() if p.is_dir() and len(p.name) == 16) if dd.is_dir() else []


def _only_dossier(owner: Path) -> Path:
    [d] = _dossiers(owner)
    return d


def _pass(root, fake=None, **kw):
    return doubledown.double_down_pass(root, analysts=fake.factory if fake else None,
                                       now=kw.pop("now", NOW), **kw)


def _drive(tmp_path, fake, monkeypatch, *, start=0, passes=40):
    """Passes 700 s apart (a provider park is looked at again after 600 s)."""
    monkeypatch.setattr(doubledown, "read_health", _Health())
    actions = []
    for k in range(start, start + passes):
        rows = _pass(tmp_path, fake, now=NOW + timedelta(seconds=700 * k))
        actions.extend(r["action"] for r in rows)
    return actions


def _bytes(path: Path) -> dict:
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def _state_bytes(root) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file() and p.name in
            ("state.json", "project.json")}


def _parked_on_a_bound(tmp_path, monkeypatch, rid="r"):
    """An RND_NEGATIVE run whose analyst never answers: three provider parks, then the bound."""
    run = _run(tmp_path, rid, **_loops_exhausted())
    _project(tmp_path, [TaskNode(id="n1", lifecycle=Lifecycle.BLOCKED, run_id=rid, note="x")])
    fake = Fake({(1, "root_cause"): RuntimeError("down")}, default=_open_hyps)
    _drive(tmp_path, fake, monkeypatch)
    d = doubledown.read_dossier(_only_dossier(run.dir))
    assert [p["kind"] for p in d.parks] == ["provider", "provider", "provider", "bound"]
    return run, fake, d


# --- the numbers --------------------------------------------------------------------------- #

def test_the_renewal_grants_and_the_limits_are_the_documented_numbers():
    assert doubledown.RENEW_ROUNDS == 8
    assert doubledown.RENEW_BYTES == 8 << 20
    assert doubledown.MAX_RENEW_TEXT == 1000
    assert doubledown.MAX_PARKS == 3
    assert doubledown.SCHEMA_RENEW == "hoh-doubledown-renew/1"


# --- what a renewal does ------------------------------------------------------------------- #

def test_a_person_renews_a_bound_and_the_double_down_dispatches_again(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    calls = len(fake.calls)
    assert _pass(tmp_path, fake, now=NOW + timedelta(days=30))[0]["action"] == "parked_bound"
    assert len(fake.calls) == calls                       # nothing resumes a bound by itself
    row = doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="more rounds",
                                 now=NOW)
    assert row["action"] == "renewed" and row["n"] == 4 and row["by"] == "sai"
    assert row["grants"] == {"rounds": 8, "bytes": 8 << 20, "provider_parks": 3}
    rec = json.loads((parked.path / "renew-4.json").read_text())
    assert (rec["schema"], rec["n"], rec["by"], rec["reason"]) == (
        "hoh-doubledown-renew/1", 4, "sai", "more rounds")
    assert rec["renewed_at"] == "2026-10-14T12:00:00Z"
    assert rec["block_digest"] == parked.block["digest"]
    assert rec["grants"] == {"rounds": 8, "bytes": 8 << 20, "provider_parks": 3}
    again = doubledown.read_dossier(parked.path)
    assert again.parked is None and again.outcome is None and len(again.renewals) == 1
    ok = Fake(default=_open_hyps)
    [row] = _pass(tmp_path, ok, now=NOW + timedelta(days=30))
    assert row["action"] == "round_1_continue" and len(ok.calls) == 2


def test_the_failures_before_a_renewal_are_not_counted_again(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="again", now=NOW)
    actions = [r["action"] for k in range(2)
               for r in _pass(tmp_path, fake, now=NOW + timedelta(days=30, seconds=k))]
    assert actions == ["dispatch_failed", "dispatch_failed"]      # the third parks, not the first
    assert doubledown.read_dossier(parked.path).parked is None
    assert _pass(tmp_path, fake, now=NOW + timedelta(days=30, seconds=9))[0]["action"] == "parked"


def test_a_renewal_grants_three_more_provider_parks_and_the_bound_parks_again(
        tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="again", now=NOW)
    before = len(fake.calls)
    _drive(tmp_path, fake, monkeypatch, start=100)
    d = doubledown.read_dossier(parked.path)
    assert [p["kind"] for p in d.parks] == ["provider"] * 3 + ["bound"] + ["provider"] * 3 + ["bound"]
    assert d.outcome is None and d.rounds == []                   # still parked, never ended
    assert d.provider_parks_since_renewal == 3 and d.parked["kind"] == "bound"
    assert len(fake.calls) - before == 12
    second = doubledown.renew_bound(run.dir, d.block["halt_id"], by="sai", reason="and again",
                                    now=NOW)
    assert second["action"] == "renewed" and second["n"] == 8
    assert doubledown.read_dossier(parked.path).provider_parks_since_renewal == 0


def test_a_renewal_raises_the_owners_allowance_by_the_fixed_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(doubledown, "OWNER_MAX_ROUNDS", 1)
    monkeypatch.setattr(doubledown, "RENEW_ROUNDS", 2)
    run = _run(tmp_path, "r", **_loops_exhausted())
    _project(tmp_path, [TaskNode(id="n1", lifecycle=Lifecycle.BLOCKED, run_id="r", note="x")])
    fake = Fake(default=_open_hyps)
    assert [_pass(tmp_path, fake)[0]["action"] for _ in range(2)] == [
        "round_1_continue", "parked_bound"]                       # the allowance of 1 is spent
    d = doubledown.read_dossier(_only_dossier(run.dir))
    assert d.parked["kind"] == "bound" and len(d.rounds) == 1
    doubledown.renew_bound(run.dir, d.block["halt_id"], by="sai", reason="allowance", now=NOW)
    acts = [_pass(tmp_path, fake)[0]["action"] for _ in range(3)]
    assert acts == ["round_2_continue", "round_3_continue", "parked_bound"]    # 1 + 2 = 3 rounds
    assert len(doubledown.read_dossier(d.path).rounds) == 3


def test_a_renewal_raises_the_owners_byte_allowance_by_the_fixed_grant(tmp_path, monkeypatch):
    run = _run(tmp_path, "r", **_loops_exhausted())
    _project(tmp_path, [TaskNode(id="n1", lifecycle=Lifecycle.BLOCKED, run_id="r", note="x")])
    fake = Fake(default=_open_hyps)
    _pass(tmp_path, fake)
    d = doubledown.read_dossier(_only_dossier(run.dir))
    used = doubledown._owner_usage(run.dir / "dd")["bytes"]
    monkeypatch.setattr(doubledown, "OWNER_MAX_BYTES", used)
    assert _pass(tmp_path, fake)[0]["action"] == "parked_bound"
    monkeypatch.setattr(doubledown, "RENEW_BYTES", 1 << 20)
    doubledown.renew_bound(run.dir, d.block["halt_id"], by="sai", reason="bytes", now=NOW)
    usage = doubledown._owner_usage(run.dir / "dd")
    assert (usage["granted_rounds"], usage["granted_bytes"]) == (8, 1 << 20)
    assert _pass(tmp_path, fake)[0]["action"] == "round_2_continue"


def test_a_renewal_that_does_not_read_back_grants_nothing(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="x", now=NOW)
    assert doubledown._owner_usage(run.dir / "dd")["granted_rounds"] == 8
    rec = parked.path / "renew-4.json"
    body = json.loads(rec.read_text())
    body["by"] = "somebody else"                                  # the seal no longer matches
    rec.chmod(0o600)
    rec.write_text(json.dumps(body))
    usage = doubledown._owner_usage(run.dir / "dd")
    assert (usage["granted_rounds"], usage["granted_bytes"]) == (0, 0)
    [row] = _pass(tmp_path, fake, now=NOW + timedelta(days=30))
    assert row["action"] == "refused" and "digest" in row["why"]


# --- who may renew, and what ------------------------------------------------------------------ #

def _listing(path: Path) -> list[str]:
    return sorted(os.listdir(path))


def test_a_renewal_is_refused_unless_the_dossier_is_parked_on_a_bound(tmp_path, monkeypatch):
    run = _run(tmp_path, "h", blocked_reason="stuck")
    fake = Fake({(1, "root_cause"): RuntimeError("down")}, default=_open_hyps)
    other = _run(tmp_path, "t", blocked_reason="stuck", budgets=Budgets(max_iterations=1),
                 usage=Usage(iterations=1))
    monkeypatch.setattr(doubledown, "read_health", _Health())
    for _ in range(3):
        _pass(tmp_path, fake)
    d = doubledown.read_dossier(_only_dossier(run.dir))
    assert d.parked["kind"] == "provider"
    halt_id = d.block["halt_id"]
    before = _listing(d.path)
    row = doubledown.renew_bound(run.dir, halt_id, by="sai", reason="x", now=NOW)
    assert row["action"] == "refused" and "not parked on a bound" in row["why"]
    assert _listing(d.path) == before
    closed = doubledown.read_dossier(_only_dossier(other.dir))
    assert closed.outcome["outcome"] == "PROPOSAL"                # a TRIVIAL one, ended at once
    row = doubledown.renew_bound(other.dir, closed.block["halt_id"], by="sai", reason="x", now=NOW)
    assert row["action"] == "refused" and "PROPOSAL" in row["why"]
    row = doubledown.renew_bound(run.dir, "0" * 16, by="sai", reason="x", now=NOW)
    assert row["action"] == "refused"                             # no such dossier


@pytest.mark.parametrize("halt_id", ["", "x", "0" * 15, "0" * 17, "G" * 16, "A" * 16, "\u0660" * 16,
                                     "../" + "0" * 13, "0" * 16 + "\n", None, 7])
def test_a_dossier_is_named_by_sixteen_hex_characters_and_nothing_else(tmp_path, halt_id):
    run = _run(tmp_path, "h", blocked_reason="stuck")
    row = doubledown.renew_bound(run.dir, halt_id, by="sai", reason="x", now=NOW)
    assert row["action"] == "refused"
    assert not (run.dir / "dd").exists()


@pytest.mark.parametrize("by,reason", [
    ("", "why"), ("   ", "why"), (" sai", "why"), ("sai ", "why"), ("sa\ni", "why"),
    ("sai", ""), ("sai", " why"), ("sai", "why "), ("sai", "line\none"), ("sai", "a\x00b"),
    ("sai", "x" * 1001), ("x" * 1001, "why"), (None, "why"), ("sai", None), (7, "why")])
def test_a_renewal_names_who_and_why_in_one_printable_line(tmp_path, monkeypatch, by, reason):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    before = _listing(parked.path)
    row = doubledown.renew_bound(run.dir, parked.block["halt_id"], by=by, reason=reason, now=NOW)
    assert row["action"] == "refused" and _listing(parked.path) == before


def test_a_name_and_a_reason_of_exactly_the_longest_length_are_accepted(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    row = doubledown.renew_bound(run.dir, parked.block["halt_id"], by="b" * 1000,
                                 reason="r" * 1000, now=NOW)
    assert row["action"] == "renewed"


def test_a_bound_is_renewed_once_and_the_record_is_create_only(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    halt_id = parked.block["halt_id"]
    assert doubledown.renew_bound(run.dir, halt_id, by="sai", reason="one", now=NOW)["action"] \
        == "renewed"
    kept = (parked.path / "renew-4.json").read_bytes()
    again = doubledown.renew_bound(run.dir, halt_id, by="other", reason="two", now=NOW)
    assert again["action"] == "refused" and "not parked on a bound" in again["why"]
    assert (parked.path / "renew-4.json").read_bytes() == kept
    with pytest.raises(OSError):
        doubledown._create(parked.path / "renew-4.json", {"schema": "hoh-doubledown-renew/1"})


def test_a_renewal_answers_a_bound_and_a_resume_a_provider_park_nothing_else(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    block = parked.block["digest"]
    forged = doubledown._seal({"schema": "hoh-doubledown-resume/1", "n": 4, "block_digest": block,
                               "resumed_at": "2026-10-14T12:00:00Z", "failures": {}})
    (parked.path / "resume-4.json").write_text(json.dumps(forged))     # a resume of the bound
    with pytest.raises(doubledown.DossierError, match="a renewal answers a bound"):
        doubledown.read_dossier(parked.path)
    (parked.path / "resume-4.json").rename(parked.path / "resume-4.json.v1")
    forged = doubledown._seal({"schema": "hoh-doubledown-renew/1", "n": 1, "block_digest": block,
                               "by": "x", "reason": "y", "renewed_at": "2026-10-14T12:00:00Z",
                               "grants": {}, "failures": {}})
    (parked.path / "renew-1.json").write_text(json.dumps(forged))      # a renewal of a provider park
    with pytest.raises(doubledown.DossierError, match="a renewal answers a bound"):
        doubledown.read_dossier(parked.path)


def test_a_renewal_of_another_dossiers_block_or_a_wrong_number_is_refused_on_read(
        tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    for n, digest in ((4, "0" * 64), (5, parked.block["digest"]), (9, parked.block["digest"])):
        forged = doubledown._seal({"schema": "hoh-doubledown-renew/1", "n": n,
                                   "block_digest": digest, "by": "x", "reason": "y",
                                   "renewed_at": "2026-10-14T12:00:00Z", "grants": {},
                                   "failures": {}})
        (parked.path / "renew-4.json").write_text(json.dumps(forged))
        with pytest.raises(doubledown.DossierError, match="does not belong to this block"):
            doubledown.read_dossier(parked.path)
        (parked.path / "renew-4.json").rename(parked.path / f"renew-4.json.v{n}")


def test_a_renewal_is_refused_through_a_link_or_a_dossier_that_is_not_a_directory(
        tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    halt_id = parked.block["halt_id"]
    link = tmp_path / "link"
    link.symlink_to(run.dir)
    row = doubledown.renew_bound(link, halt_id, by="sai", reason="x", now=NOW)
    assert row["action"] == "refused" and "link" in row["why"]
    aside = run.dir / "dd" / (halt_id + ".v1")
    parked.path.rename(aside)
    (run.dir / "dd" / halt_id).symlink_to(aside)
    row = doubledown.renew_bound(run.dir, halt_id, by="sai", reason="x", now=NOW)
    assert row["action"] == "refused"
    assert not list(aside.glob("renew-*"))
    (run.dir / "dd" / halt_id).rename(run.dir / "dd" / (halt_id + ".link"))
    aside.rename(run.dir / "dd" / halt_id)
    (run.dir / "dd").rename(run.dir / "dd.v1")                   # the whole dd/ is a link now
    (run.dir / "dd").symlink_to(run.dir / "dd.v1")
    row = doubledown.renew_bound(run.dir, halt_id, by="sai", reason="x", now=NOW)
    assert row["action"] == "refused" and not list((run.dir / "dd.v1" / halt_id).glob("renew-*"))


def test_a_second_pass_that_holds_the_dossier_leaves_a_renewal_refused(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    with doubledown._locked(parked.path):
        row = doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="x",
                                     now=NOW)
    assert row["action"] == "refused" and "another pass holds this dossier" in row["why"]
    assert not (parked.path / "renew-4.json").exists()


# --- termination: bounded, terminal, quiet ------------------------------------------------------ #

def test_a_parked_bound_is_quiet_until_a_person_renews_it(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    calls = len(fake.calls)
    kept = _bytes(parked.path)
    state = _state_bytes(tmp_path)
    for k in range(3):
        [row] = _pass(tmp_path, fake, now=NOW + timedelta(days=60 + k))
        assert row["action"] == "parked_bound"
    assert _bytes(parked.path) == kept and len(fake.calls) == calls
    assert _state_bytes(tmp_path) == state
    from hoh import missioncontrol

    doc = missioncontrol.project_view(str(tmp_path), "p", "needs-you")
    assert [r["outcome"] for r in doc["double_down"]] == ["PARKED_BOUND"]
    doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="go on", now=NOW)
    assert "double_down" not in missioncontrol.project_view(str(tmp_path), "p", "needs-you")


def test_a_renewal_that_cannot_be_written_leaves_the_bound_parked_and_visible(
        tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    real = doubledown._create
    monkeypatch.setattr(doubledown, "_create", lambda path, record: (_ for _ in ()).throw(
        OSError(28, "No space left on device")) if Path(path).name.startswith("renew-")
        else real(path, record))
    row = doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="x", now=NOW)
    assert row["action"] == "refused" and "No space left" in row["why"]
    assert doubledown.read_dossier(parked.path).parked["kind"] == "bound"
    [again] = _pass(tmp_path, fake, now=NOW + timedelta(days=30))
    assert again["action"] == "parked_bound"                      # still parked, still a decision
    from hoh import missioncontrol

    doc = missioncontrol.project_view(str(tmp_path), "p", "needs-you")
    assert [r["outcome"] for r in doc["double_down"]] == ["PARKED_BOUND"]


def test_the_renewal_never_writes_a_state_or_lifts_a_block(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    state = _state_bytes(tmp_path)
    doubledown.renew_bound(run.dir, parked.block["halt_id"], by="sai", reason="x", now=NOW)
    assert _state_bytes(tmp_path) == state
    tree = ast.parse(Path(doubledown.__file__).read_text(encoding="utf-8"))
    [fn] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "renew_bound"]
    called = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)}
    assert not ({"write_state", "unblock", "resume", "arm_new", "disarm", "acquire", "release",
                 "transition"} & called)


# --- the command ------------------------------------------------------------------------------ #

def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def test_hoh_double_down_renew_renews_a_run_and_a_project_dossier(tmp_path, monkeypatch):
    run, fake, parked = _parked_on_a_bound(tmp_path, monkeypatch)
    halt_id = parked.block["halt_id"]
    root = str(tmp_path)
    rc, out, err = _main(["--root", root, "double-down", "renew", halt_id, "--run", "r",
                          "--by", "sai", "--reason", "more rounds"])
    assert rc == 0, err
    row = json.loads(out)
    assert (row["action"], row["n"], row["by"], row["halt_id"]) == ("renewed", 4, "sai", halt_id)
    rc, out, _ = _main(["--root", root, "double-down", "renew", halt_id, "--run", "r",
                        "--by", "sai", "--reason", "again"])
    assert rc == 2 and json.loads(out)["action"] == "refused"      # no longer parked on a bound
    proj_dd = ProjectStore(root, "p").dir / "dd"
    rc, out, _ = _main(["--root", root, "double-down", "renew", "0" * 16, "--project", "p",
                        "--by", "sai", "--reason", "x"])
    assert rc == 2 and json.loads(out)["action"] == "refused" and not proj_dd.exists()


def test_the_renew_command_refuses_what_it_cannot_name(tmp_path):
    root = str(tmp_path)
    for argv in (["double-down", "renew", "0" * 16, "--by", "sai", "--reason", "x"],       # no owner
                 ["double-down", "renew", "0" * 16, "--run", "r", "--project", "p",
                  "--by", "sai", "--reason", "x"],                                          # two
                 ["double-down", "renew", "0" * 16, "--run", "r", "--reason", "x"],         # no --by
                 ["double-down", "renew", "0" * 16, "--run", "r", "--by", "sai"]):          # no why
        rc, _, _ = _main(["--root", root, *argv])
        assert rc == 2
    rc, out, err = _main(["--root", root, "double-down", "renew", "0" * 16, "--run", "../x",
                          "--by", "sai", "--reason", "x"])
    assert rc == 2 and "refused" in err
    rc, out, err = _main(["--root", root, "double-down", "renew", "0" * 16, "--run=../x",
                          "--by=sai", "--reason=x"])                    # the other spelling
    assert rc == 2 and "refused" in err
    rc, out, err = _main(["--root", root, "double-down", "renew", "0" * 16, "--run", "nope",
                          "--by", "sai", "--reason", "x"])
    assert rc == 2 and json.loads(out)["action"] == "refused"
    assert not any(tmp_path.iterdir())                            # nothing was created
