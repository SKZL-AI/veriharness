"""`hoh project route-decisions` (V3.3 P3-31, slice 1): automatic eligible decision routing.

An open EXTERNAL decision of a project -- a READY node of the EXTERNAL action class that nothing
stands in front of, the decision the loop halts `BLOCKED_EXTERNAL` on without asking -- is routed
through the autonomy contract (`autonomy.route`, kind `external`). When the contract names an
automatic resolver for it (escalation SYSTEM_TWO or SYSTEM_ONE_THEN_SYSTEM_TWO) a record is created
once, never replaced, naming the decision's digest and the contract's digest and saying
`approves: false`. Everything else stays open for the operator and records nothing.

Every case goes through the product's own command (`cli.main`), the project through
`ProjectStore`, the parked branches through `Governor.decide` and the governed projects through
`MasterAuthority`. Nothing starts a process; a FIFO is guarded so that a hang is a failure.

Groups: C = the route, N = what stays open, F = refusals and failures, A = authority, D = digests
and the record, P = the production path and the module's own arguments.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from hoh import cli
from hoh.autonomy import (
    AutonomyContract,
    AutonomyMode,
    Escalation,
    Handling,
    Horizon,
    NeedsYou,
    ReleasePolicy,
)
from hoh.contracts import Budgets
from hoh.epoch import EpochStore
from hoh.governance import Governor
from hoh.project import ActionClass, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.succession import MasterAuthority

READY, BLOCKED = Lifecycle.READY, Lifecycle.BLOCKED
EXTERNAL, INTERNAL = ActionClass.EXTERNAL, ActionClass.INTERNAL


def _gov():
    import hoh.governance as module
    return module


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a test started a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


class Hang(BaseException):
    pass


class Guard:
    def __init__(self, seconds: int = 10):
        self.seconds = seconds

    def __enter__(self):
        def alarm(signum, frame):
            raise Hang(f"did not return within {self.seconds} s: it blocked")

        self.old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)
        return False


# --- builders -----------------------------------------------------------------------------

_ESCALATION = {"INTERNAL": Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
               "release": Escalation.PARK_BRANCH_AND_CONTINUE,
               "scope": Escalation.PARK_BRANCH_AND_CONTINUE}


def _contract(external=Escalation.SYSTEM_TWO, *, needs_you=NeedsYou.DIGEST_ONLY, gates=(),
              allow_external=True, contract_id="c-ext", **kw) -> AutonomyContract:
    """PLAN_AUTONOMOUS (Needs-You ON or DIGEST_ONLY) with EXTERNAL allowed and routed `external`."""
    escalation = dict(_ESCALATION)
    if allow_external and external is not None:
        escalation["EXTERNAL"] = external
    classes = (INTERNAL, EXTERNAL) if allow_external else (INTERNAL,)
    return AutonomyContract(
        contract_id=contract_id, mode=AutonomyMode.PLAN_AUTONOMOUS, needs_you=needs_you,
        release_policy=ReleasePolicy.AUTO_PREPARE_ONLY, allowed_action_classes=classes,
        escalation=escalation, hard_human_gates=tuple(gates), **kw)


def _unattended(external=Escalation.SYSTEM_TWO, **kw) -> AutonomyContract:
    """UNATTENDED_LONG_HORIZON, Needs-You OFF: a human is not expected."""
    return AutonomyContract(
        contract_id="c-un", mode=AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.OFF,
        scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900),
        budgets=Budgets(max_dispatches=10, max_wallclock_seconds=3600),
        release_policy=ReleasePolicy.NEVER_AUTO_PUBLISH,
        allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={**_ESCALATION, "EXTERNAL": external}, **kw)


def _file(tmp_path: Path, contract: AutonomyContract, name="contract.json") -> Path:
    path = tmp_path / name
    path.write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    return path


def _node(node_id, lifecycle=READY, deps=(), action=INTERNAL, **kw) -> TaskNode:
    return TaskNode(id=node_id, lifecycle=lifecycle, dependencies=list(deps),
                    action_class=action, **kw)


def _push(node_id="push", note="push the tag v1 to origin", **kw) -> TaskNode:
    return _node(node_id, action=EXTERNAL, note=note, **kw)


def _project(root: Path, *nodes, project_id="p") -> ProjectStore:
    store = ProjectStore(root, project_id)
    store.create(ProjectState(project_id=project_id, repo_path=str(root), nodes=list(nodes)))
    return store


def _run(capsys, root: Path, *argv: str, project="p"):
    """`hoh --root ROOT project route-decisions PROJECT ARGV`: (code, document or None, stderr)."""
    try:
        with Guard():
            code = cli.main(["--root", str(root), "project", "route-decisions", project, *argv])
    except SystemExit as exc:
        code = exc.code
    got = capsys.readouterr()
    return code, (json.loads(got.out) if got.out.strip() else None), got.err


def _rows(doc) -> dict:
    return {d["node"]: d for d in doc["decisions"]}


def _records(store: ProjectStore) -> list[Path]:
    d = store.dir / "decision-routings"
    return sorted(d.iterdir()) if d.is_dir() else []


def _tree(root: Path) -> dict:
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in ["", *dirs, *files]:
            path = os.path.join(base, name) if name else base
            info = os.lstat(path)
            digest = None
            if stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            seen[os.path.relpath(path, root)] = (info.st_mode, info.st_size, digest)
    return seen


def _park(root: Path, run_id: str, contract: AutonomyContract):
    """A branch parked through the product's writer: a non-delegable decision, routed."""
    return Governor(contract=contract, run_dir=root / run_id).decide(
        decision=f"{run_id} needs an answer", kind="decision", delegable=False)


# --- C: the route --------------------------------------------------------------------------

def test_the_command_routes_an_external_decision_the_contract_hands_to_system_two(
        tmp_path, capsys):
    store = _project(tmp_path, _push(), _node("i1"))
    contract = _contract()
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract)))
    assert (code, err) == (0, "")
    row = _rows(doc)["push"]
    assert (row["outcome"], row["reason"], row["handling"], row["disposition"], row["resolver"],
            row["recorded"]) == ("ROUTED", "", "", "HANDED_TO_RESOLVER", "SYSTEM_TWO", True)
    assert doc["contract"] == {"id": "c-ext", "digest": contract.digest(),
                               "mode": "PLAN_AUTONOMOUS", "program_epoch": 0}
    (path,) = _records(store)
    assert path.name == f"{row['decision_digest']}-{contract.digest()}.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    record = json.loads(path.read_text())
    assert record == {
        "schema": "hoh-decision-routing/1", "project_id": "p", "node_id": "push",
        "decision_digest": row["decision_digest"], "kind": "external",
        "disposition": "HANDED_TO_RESOLVER", "escalation": "SYSTEM_TWO", "resolver": "SYSTEM_TWO",
        "approves": False, "contract_id": "c-ext", "contract_digest": contract.digest(),
        "mode": "PLAN_AUTONOMOUS", "program_epoch": 0}
    assert path.read_bytes() == (json.dumps(record, sort_keys=True, indent=1,
                                            ensure_ascii=True) + "\n").encode()
    # nothing else of the project moved: the command routes, it does not act
    assert store.read_state().node("push").lifecycle is READY


def test_the_record_carries_no_word_an_existing_reader_takes_as_approval(tmp_path, capsys):
    """`route` answers RESOLVED_BY_POLICY for SYSTEM_TWO and `may_release` "refuses unless resolved
    by policy": the record must not say that word, nor any other `Handling`, nor be loadable as a
    routing. The one word that reads as approval in it is `approves`, which is false."""
    from hoh.autonomy import Routing

    store = _project(tmp_path, _push())
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0
    (path,) = _records(store)
    text = path.read_text()
    record = json.loads(text)
    for handling in Handling:
        assert handling.value not in text, handling
    assert "handling" not in record and record["approves"] is False
    assert record["disposition"] == "HANDED_TO_RESOLVER"
    with pytest.raises(ValueError):
        Routing.model_validate(record)
    row = _rows(doc)["push"]
    assert "RESOLVED_BY_POLICY" not in json.dumps(row)


def test_a_second_call_records_nothing_more_and_changes_nothing(tmp_path, capsys):
    store = _project(tmp_path, _push())
    argv = ("--autonomy", str(_file(tmp_path, _contract())))
    assert _run(capsys, tmp_path, *argv)[0] == 0
    before = _tree(tmp_path)
    code, doc, _ = _run(capsys, tmp_path, *argv)
    assert code == 0
    row = _rows(doc)["push"]
    assert (row["outcome"], row["recorded"]) == ("ROUTED", False)
    assert _tree(tmp_path) == before
    assert len(_records(store)) == 1


def test_system_one_then_system_two_is_named_as_such(tmp_path, capsys):
    store = _project(tmp_path, _push())
    contract = _contract(Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO)
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract)))
    assert code == 0
    assert _rows(doc)["push"]["resolver"] == "SYSTEM_ONE_THEN_SYSTEM_TWO"
    assert json.loads(_records(store)[0].read_text())["escalation"] == \
        "SYSTEM_ONE_THEN_SYSTEM_TWO"


def test_every_open_external_decision_is_routed_in_the_frontiers_order(tmp_path, capsys):
    """`a` has two nodes behind it, `b` none: the one that frees more is first."""
    store = _project(tmp_path, _push("b-push", "later"), _push("a-push", "first"),
                     _node("x", deps=["a-push"]), _node("y", deps=["a-push"]))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0
    assert [d["node"] for d in doc["decisions"]] == ["a-push", "b-push"]
    assert len(_records(store)) == 2


def test_a_mode_is_not_asked_for_the_same_route_in_unattended_mode(tmp_path, capsys):
    store = _project(tmp_path, _push())
    contract = _unattended(Escalation.SYSTEM_TWO)
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract)))
    assert code == 0
    assert _rows(doc)["push"]["outcome"] == "ROUTED"
    assert json.loads(_records(store)[0].read_text())["mode"] == "UNATTENDED_LONG_HORIZON"


# --- N: what stays open --------------------------------------------------------------------

@pytest.mark.parametrize("contract,handling", [
    pytest.param(_contract(Escalation.HUMAN_IF_AVAILABLE, needs_you=NeedsYou.ON), "ASK_NOW",
                 id="a person, one is expected"),
    pytest.param(_contract(Escalation.HUMAN_IF_AVAILABLE, needs_you=NeedsYou.DIGEST_ONLY),
                 "COLLECT_FOR_DIGEST", id="a person, in the digest"),
    pytest.param(_unattended(Escalation.HUMAN_IF_AVAILABLE), "PARK_BRANCH",
                 id="a person, none expected"),
    pytest.param(_contract(Escalation.PARK_BRANCH_AND_CONTINUE), "PARK_BRANCH", id="park"),
    pytest.param(_contract(Escalation.ABORT_BRANCH), "ABORT_BRANCH", id="abort"),
    pytest.param(_contract(Escalation.STOP_PROGRAM), "STOP_PROGRAM", id="stop"),
    pytest.param(_contract(allow_external=False), "PARK_BRANCH", id="a class the contract "
                 "does not allow"),
    pytest.param(_contract(Escalation.SYSTEM_TWO, gates=("EXTERNAL",)), "COLLECT_FOR_DIGEST",
                 id="a hard human gate, in the digest"),
    pytest.param(_contract(Escalation.SYSTEM_TWO, gates=("EXTERNAL",), needs_you=NeedsYou.ON),
                 "ASK_NOW", id="a hard human gate, a person expected"),
])
def test_a_decision_the_contract_does_not_hand_to_a_resolver_stays_open_and_records_nothing(
        tmp_path, capsys, contract, handling):
    store = _project(tmp_path, _push())
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract)))
    assert (code, err) == (0, "")
    row = _rows(doc)["push"]
    assert (row["outcome"], row["reason"], row["handling"], row["recorded"]) == (
        "LEFT_OPEN", "NOT_COVERED", handling, False)
    assert _records(store) == [] and not (store.dir / "decision-routings").exists()


def test_a_decision_a_contract_already_parked_is_not_routed_again(tmp_path, capsys):
    contract = _unattended(Escalation.SYSTEM_TWO)
    parked = _park(tmp_path, "r-parked", contract)
    assert parked.handling is Handling.PARK_BRANCH
    store = _project(tmp_path, _node("a", BLOCKED, run_id="r-parked"), _node("b", deps=["a"]))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract)))
    assert code == 0
    row = _rows(doc)["a"]
    assert (row["outcome"], row["reason"], row["handling"]) == (
        "LEFT_OPEN", "ALREADY_ROUTED", "PARK_BRANCH")
    assert _records(store) == []


def test_a_decision_a_person_blocked_is_not_the_programmes_to_route(tmp_path, capsys):
    store = _project(tmp_path, _node("a", BLOCKED, blocked_kind="human", note="legal review"))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0
    row = _rows(doc)["a"]
    assert (row["outcome"], row["reason"], row["recorded"]) == ("LEFT_OPEN", "NOT_DELEGABLE", False)
    assert _records(store) == []


def test_an_external_node_with_work_in_front_of_it_is_not_yet_a_decision(tmp_path, capsys):
    store = _project(tmp_path, _node("build"), _push(deps=["build"]))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0 and doc["decisions"] == []
    assert _records(store) == []


def test_an_internal_ready_node_is_never_an_open_decision(tmp_path, capsys):
    store = _project(tmp_path, _node("a"), _node("b"))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0 and doc["decisions"] == []
    assert not (store.dir / "decision-routings").exists()


def test_a_contract_that_does_not_hold_together_leaves_every_decision_open(tmp_path):
    store = _project(tmp_path, _push())
    broken = _contract().model_copy(update={"needs_you": NeedsYou.ON,
                                            "mode": AutonomyMode.UNATTENDED_LONG_HORIZON})
    assert _gov().coherence(broken)
    report = _gov().route_project_decisions(store, broken, tmp_path)
    (row,) = report.decisions
    assert (row.outcome, row.reason, row.handling) == (
        "LEFT_OPEN", "NOT_COVERED", "CONTRACT_INCOHERENT")
    assert _records(store) == []


# --- F: refusals and failures --------------------------------------------------------------

def _refused(capsys, tmp_path, *argv):
    code, doc, err = _run(capsys, tmp_path, *argv)
    assert code == 2 and doc is None and err.startswith("refused: "), (code, err)
    return err


def test_a_contract_file_that_is_not_a_contract_is_refused_and_nothing_is_written(
        tmp_path, capsys):
    store = _project(tmp_path, _push())
    before = _tree(tmp_path)
    bad = {
        "missing": tmp_path / "nowhere.json",
        "a directory": tmp_path,
        "empty": tmp_path / "empty.json",
        "not JSON": tmp_path / "junk.json",
        "a list": tmp_path / "list.json",
        "Latin-1": tmp_path / "latin1.json",
        "an unknown field": tmp_path / "extra.json",
        "oversize": tmp_path / "big.json",
    }
    (tmp_path / "empty.json").write_bytes(b"")
    (tmp_path / "junk.json").write_bytes(b"{nope")
    (tmp_path / "list.json").write_bytes(b"[1, 2]")
    (tmp_path / "latin1.json").write_bytes('{"contract_id": "cü"}'.encode("latin-1"))
    (tmp_path / "extra.json").write_text(
        json.dumps({**json.loads(_contract().model_dump_json()), "surprise": 1}))
    text = _contract().model_dump_json()
    (tmp_path / "big.json").write_bytes(text.encode() + b" " * ((1 << 20) + 1 - len(text)))
    before = _tree(tmp_path)
    for what, path in bad.items():
        _refused(capsys, tmp_path, "--autonomy", str(path))
        assert _tree(tmp_path) == before, what
    assert _records(store) == []


def test_the_contract_file_size_limit_is_exact(tmp_path, capsys):
    store = _project(tmp_path, _push())
    text = _contract().model_dump_json().encode()
    at = tmp_path / "at.json"
    at.write_bytes(text + b" " * ((1 << 20) - len(text)))
    assert at.stat().st_size == 1 << 20
    assert _run(capsys, tmp_path, "--autonomy", str(at))[0] == 0
    assert len(_records(store)) == 1
    over = tmp_path / "over.json"
    over.write_bytes(text + b" " * ((1 << 20) + 1 - len(text)))
    _refused(capsys, tmp_path, "--autonomy", str(over))


def test_a_fifo_in_place_of_the_contract_file_is_refused_and_does_not_block(tmp_path, capsys):
    _project(tmp_path, _push())
    fifo = tmp_path / "contract.fifo"
    os.mkfifo(fifo)
    _refused(capsys, tmp_path, "--autonomy", str(fifo))


def test_an_incoherent_contract_file_is_refused(tmp_path, capsys):
    store = _project(tmp_path, _push())
    raw = json.loads(_contract().model_dump_json())
    raw["escalation"]["EXTERNAL"] = "AUTO_POLICY"          # a programme approving a push itself
    path = tmp_path / "c.json"
    path.write_text(json.dumps(raw))
    err = _refused(capsys, tmp_path, "--autonomy", str(path))
    assert "AUTO_POLICY" in err
    assert _records(store) == []


@pytest.mark.parametrize("epoch", ["-1", str(2 ** 40 + 1), "1" + "0" * 5000], ids=[
    "negative", "above the bound", "5001 digits"])
def test_a_programme_epoch_the_record_cannot_name_is_refused(tmp_path, capsys, epoch):
    store = _project(tmp_path, _push())
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                          "--program-epoch", epoch)
    assert code == 2 and doc is None
    # the command's own refusal, not "no such subcommand": a number of 5001 digits is
    # argparse's (`int()` has a digit limit), the other two are the module's
    assert ("invalid int value" in err) if len(epoch) > 4000 else err.startswith("refused: "), err
    assert _records(store) == []


def test_the_largest_programme_epoch_the_record_can_name_is_accepted(tmp_path, capsys):
    store = _project(tmp_path, _push())
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                        "--program-epoch", str(2 ** 40))
    assert code == 0 and doc["contract"]["program_epoch"] == 2 ** 40
    assert len(_records(store)) == 1


def test_the_programme_epoch_is_pinned_into_the_digest(tmp_path, capsys):
    store = _project(tmp_path, _push())
    contract = _contract().pin(3)
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                        "--program-epoch", "3")
    assert code == 0
    assert doc["contract"]["digest"] == contract.digest() != _contract().digest()
    assert json.loads(_records(store)[0].read_text())["program_epoch"] == 3


def test_a_contract_id_the_record_cannot_name_is_refused(tmp_path, capsys):
    store = _project(tmp_path, _push())
    for name in ("über", "c" * 129):
        _refused(capsys, tmp_path, "--autonomy",
                 str(_file(tmp_path, _contract(contract_id=name), "c.json")))
    assert _records(store) == []


def test_the_expected_digest_must_be_the_contracts(tmp_path, capsys):
    store = _project(tmp_path, _push())
    contract = _contract()
    path = _file(tmp_path, contract)
    err = _refused(capsys, tmp_path, "--autonomy", str(path), "--expect-digest", "0" * 16)
    assert contract.digest() in err and _records(store) == []
    for spelled in (contract.digest().upper(), contract.digest() + " ", ""):
        _refused(capsys, tmp_path, "--autonomy", str(path), "--expect-digest", spelled)
    code, _, _ = _run(capsys, tmp_path, "--autonomy", str(path),
                      "--expect-digest", contract.digest())
    assert code == 0 and len(_records(store)) == 1


def test_a_log_that_cannot_be_read_is_reported_and_fails_the_command(tmp_path, capsys):
    log = tmp_path / "r-torn" / "autonomy" / "routings.jsonl"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"{torn\n")
    store = _project(tmp_path, _node("a", BLOCKED, run_id="r-torn"), _push())
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 1
    assert _rows(doc)["a"]["reason"] == "LOG_UNREADABLE"
    assert _rows(doc)["push"]["outcome"] == "ROUTED", "an unreadable log holds only its branch"
    assert len(_records(store)) == 1


def test_a_record_that_differs_is_never_replaced(tmp_path, capsys):
    store = _project(tmp_path, _push())
    argv = ("--autonomy", str(_file(tmp_path, _contract())))
    assert _run(capsys, tmp_path, *argv)[0] == 0
    (path,) = _records(store)
    path.write_bytes(path.read_bytes() + b" ")
    tampered = path.read_bytes()
    code, doc, _ = _run(capsys, tmp_path, *argv)
    assert code == 1
    row = _rows(doc)["push"]
    assert (row["outcome"], row["reason"], row["recorded"]) == ("LEFT_OPEN", "RECORD_CONFLICT",
                                                                False)
    assert path.read_bytes() == tampered


def test_a_record_that_appears_between_the_look_and_the_create_is_not_replaced(
        tmp_path, capsys, monkeypatch):
    """The look says "no record"; the file is there by the time of the create. The create
    never replaces: the answer is a conflict, and the other file is as it was."""
    store = _project(tmp_path, _push())
    contract = _contract()
    argv = ("--autonomy", str(_file(tmp_path, contract)))
    digest = _rows(_run(capsys, tmp_path, "--dry-run", *argv)[1])["push"]["decision_digest"]
    d = store.dir / "decision-routings"
    d.mkdir()
    path = d / f"{digest}-{contract.digest()}.json"
    path.write_bytes(b"somebody else's bytes")
    monkeypatch.setattr(_gov().durable_io, "read_single_link", lambda p: None)
    code, doc, _ = _run(capsys, tmp_path, *argv)
    assert code == 1
    assert _rows(doc)["push"]["reason"] == "RECORD_CONFLICT"
    assert path.read_bytes() == b"somebody else's bytes"


@pytest.mark.parametrize("how", ["a link", "a dangling link", "a FIFO", "a directory",
                                 "a second name", "oversize"])
def test_a_record_name_that_is_not_a_plain_record_fails_the_command_and_is_left_alone(
        tmp_path, capsys, how):
    store = _project(tmp_path, _push())
    contract = _contract()
    argv = ("--autonomy", str(_file(tmp_path, contract)))
    assert _run(capsys, tmp_path, "--dry-run", *argv)[0] == 0
    digest = _rows(_run(capsys, tmp_path, "--dry-run", *argv)[1])["push"]["decision_digest"]
    d = store.dir / "decision-routings"
    d.mkdir()
    path = d / f"{digest}-{contract.digest()}.json"
    other = tmp_path / "elsewhere"
    other.write_bytes(b"{}")
    if how == "a link":
        path.symlink_to(other)
    elif how == "a dangling link":
        path.symlink_to(tmp_path / "nowhere")
    elif how == "a FIFO":
        os.mkfifo(path)
    elif how == "a directory":
        path.mkdir()
    elif how == "a second name":
        os.link(other, path)
    else:
        path.write_bytes(b"x" * ((1 << 14) + 1))        # one byte over what is looked at
    before = _tree(tmp_path)
    code, doc, _ = _run(capsys, tmp_path, *argv)
    assert code == 1
    expected = "RECORD_UNREADABLE"
    assert _rows(doc)["push"]["reason"] == expected, how
    assert _tree(tmp_path) == before
    assert other.read_bytes() == b"{}"


def test_the_record_size_limit_is_exact(tmp_path, capsys):
    """Exactly the limit is read and found different (a conflict); one byte more is not looked
    at (unreadable). Both leave the file alone."""
    store = _project(tmp_path, _push())
    contract = _contract()
    argv = ("--autonomy", str(_file(tmp_path, contract)))
    digest = _rows(_run(capsys, tmp_path, "--dry-run", *argv)[1])["push"]["decision_digest"]
    d = store.dir / "decision-routings"
    d.mkdir()
    path = d / f"{digest}-{contract.digest()}.json"
    path.write_bytes(b"x" * (1 << 14))
    code, doc, _ = _run(capsys, tmp_path, *argv)
    assert (code, _rows(doc)["push"]["reason"]) == (1, "RECORD_CONFLICT")
    path.write_bytes(b"x" * ((1 << 14) + 1))
    code, doc, _ = _run(capsys, tmp_path, *argv)
    assert (code, _rows(doc)["push"]["reason"]) == (1, "RECORD_UNREADABLE")


def test_a_routings_directory_that_is_not_one_fails_the_command(tmp_path, capsys):
    store = _project(tmp_path, _push())
    (store.dir / "decision-routings").write_text("not a directory")
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 1
    assert _rows(doc)["push"]["reason"] == "RECORD_NOT_WRITTEN"
    link = tmp_path / "away"
    link.mkdir()
    other = _project(tmp_path, _push(), project_id="q")
    (other.dir / "decision-routings").symlink_to(link)
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                        project="q")
    assert code == 1
    assert _rows(doc)["push"]["reason"] == "RECORD_NOT_WRITTEN"
    assert list(link.iterdir()) == []


@pytest.mark.parametrize("name", ["a b", "../x", ".hidden", "n" * 129, "\u00fc"])
def test_a_node_id_the_record_cannot_name_fails_the_command(tmp_path, capsys, name):
    store = _project(tmp_path, _push(name))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 1
    assert _rows(doc)[name]["reason"] == "NODE_NOT_NAMEABLE"
    assert _records(store) == []


def test_a_node_id_of_exactly_128_characters_is_routed(tmp_path, capsys):
    store = _project(tmp_path, _push("n" * 128))
    code, doc, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0 and _rows(doc)["n" * 128]["outcome"] == "ROUTED"
    assert len(_records(store)) == 1


def test_a_graph_without_a_frontier_is_refused_through_the_command_and_nothing_changes(
        tmp_path, capsys):
    store = ProjectStore(tmp_path, "p")
    store.create(ProjectState(project_id="p", repo_path=str(tmp_path),
                              nodes=[_push("twin"), _push("twin")]))
    path = _file(tmp_path, _contract())
    before = _tree(tmp_path)
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(path))
    assert code == 2 and doc is None
    assert err.startswith("refused: ") and "no frontier" in err, err
    assert _tree(tmp_path) == before
    assert _records(store) == []


def test_a_project_that_is_not_there_is_refused(tmp_path, capsys):
    path = _file(tmp_path, _contract())
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(path), project="nope")
    assert code == 2 and doc is None and "no state" in err


# --- A: authority --------------------------------------------------------------------------

def _governed(tmp_path, *nodes, live: bool):
    store = _project(tmp_path, *nodes)
    authority = MasterAuthority(store.dir, "sA", serialize=store.lock)
    authority.acquire()
    if not live:
        authority.release(reason="orderly halt")
    return store, authority


def test_a_live_master_refuses_an_anonymous_command_and_nothing_is_recorded(tmp_path, capsys):
    store, _ = _governed(tmp_path, _push(), live=True)
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 5 and doc is None and "a live master holds this project" in err
    assert _records(store) == []


def test_the_live_masters_credential_authorises_the_command(tmp_path, capsys):
    store, authority = _governed(tmp_path, _push(), live=True)
    code, _, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                        "--session", "sA", "--epoch", "1", "--token", authority.token)
    assert (code, err) == (0, "")
    assert len(_records(store)) == 1
    assert authority.problems() == [], "the master is untouched"


def test_a_master_that_goes_stale_under_the_lock_is_refused_and_nothing_is_recorded(
        tmp_path, capsys, monkeypatch):
    """The credential is checked when it is presented and again inside the program lock
    (`authority.check()`); a master that is superseded in between writes nothing and the
    command says exit 5, not 0."""
    from hoh.succession import StaleMaster

    store, authority = _governed(tmp_path, _push(), live=True)
    calls = []

    def stale(self, *args, **kwargs):
        calls.append(1)
        raise StaleMaster("superseded while the command was waiting for the lock")

    monkeypatch.setattr(MasterAuthority, "check", stale)
    code, doc, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                          "--session", "sA", "--epoch", "1", "--token", authority.token)
    assert calls, "the section did not ask the authority again"
    assert code == 5 and doc is None and err.startswith("refused: "), (code, err)
    assert _records(store) == []


def test_session_and_epoch_without_the_token_are_refused(tmp_path, capsys):
    store, _ = _governed(tmp_path, _push(), live=True)
    code, _, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                        "--session", "sA", "--epoch", "1")
    assert code == 5 and "token" in err and _records(store) == []


def test_an_old_epoch_with_its_old_token_is_refused_after_a_takeover(tmp_path, capsys):
    store, old = _governed(tmp_path, _push(), live=False)
    token = old.token
    MasterAuthority(store.dir, "sB", serialize=store.lock).acquire()
    code, _, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())),
                        "--session", "sA", "--epoch", "1", "--token", token)
    assert code == 5 and "stale master" in err
    assert _records(store) == []


def test_without_a_live_master_an_operator_epoch_is_taken_and_released(tmp_path, capsys):
    store, _ = _governed(tmp_path, _push(), live=False)
    code, _, err = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert (code, err) == (0, "")
    assert len(_records(store)) == 1
    assert [e.reason.value for e in EpochStore(store.dir).chain()] == ["INITIAL", "OPERATOR"]
    assert store.read_state().master_epoch == 0, "routing is not a state write: no epoch stamped"


def test_a_dry_run_takes_no_authority_and_writes_nothing(tmp_path, capsys):
    store, _ = _governed(tmp_path, _push(), live=True)
    path = _file(tmp_path, _contract())
    before = _tree(tmp_path)
    code, doc, err = _run(capsys, tmp_path, "--dry-run", "--autonomy", str(path))
    assert (code, err) == (0, "")
    assert doc["dry_run"] is True
    row = _rows(doc)["push"]
    assert (row["outcome"], row["recorded"]) == ("ROUTED", False)
    assert _tree(tmp_path) == before
    assert _records(store) == []


def test_a_dry_run_report_is_the_report_of_the_real_run_but_for_recorded(tmp_path, capsys):
    _project(tmp_path, _push(), _node("i"))
    argv = ("--autonomy", str(_file(tmp_path, _contract())))
    dry = _run(capsys, tmp_path, "--dry-run", *argv)[1]
    real = _run(capsys, tmp_path, *argv)[1]
    for d in (dry, real):
        d["decisions"][0].pop("recorded")
    dry["dry_run"] = real["dry_run"] = None
    assert dry == real


# --- D: digests and the record -------------------------------------------------------------

def test_the_record_is_bound_to_the_decision_and_to_the_contract(tmp_path, capsys):
    store = _project(tmp_path, _push(note="push v1"))
    first = _contract()
    other = _contract(Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO)
    assert first.digest() != other.digest()
    for contract, name in ((first, "a.json"), (other, "b.json")):
        assert _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract, name)))[0] == 0
    names = [p.name for p in _records(store)]
    assert len(names) == 2 and len({n.split("-")[0] for n in names}) == 1
    for path in _records(store):
        record = json.loads(path.read_text())
        assert path.name == f"{record['decision_digest']}-{record['contract_digest']}.json"


def test_the_run_and_the_dependencies_of_a_node_are_part_of_its_decision(tmp_path, capsys):
    argv = ("--dry-run", "--autonomy", str(_file(tmp_path, _contract())))
    seen = {}
    for label, extra in (("plain", {}), ("run", {"run_id": "r-1"}),
                         ("run again", {"run_id": "r-2"}), ("dep", {"deps": ["done"]}),
                         ("two deps", {"deps": ["done", "done-too"]})):
        root = tmp_path / label.replace(" ", "-")
        root.mkdir()
        nodes = [_node("done", Lifecycle.MERGED), _node("done-too", Lifecycle.MERGED),
                 _push(**({"run_id": extra["run_id"]} if "run_id" in extra else {}),
                       **({"deps": extra["deps"]} if "deps" in extra else {}))]
        _project(root, *nodes)
        code, doc, _ = _run(capsys, root, *argv)
        assert code == 0, label
        seen[label] = _rows(doc)["push"]["decision_digest"]
    assert len(set(seen.values())) == len(seen), seen


def test_a_decision_edited_since_is_another_decision(tmp_path, capsys):
    store = _project(tmp_path, _push(note="push v1"))
    argv = ("--autonomy", str(_file(tmp_path, _contract())))
    assert _run(capsys, tmp_path, *argv)[0] == 0
    state = store.read_state()
    state.node("push").note = "push v2 to a different remote"
    store.write_state(state)
    assert _run(capsys, tmp_path, *argv)[0] == 0
    digests = {json.loads(p.read_text())["decision_digest"] for p in _records(store)}
    assert len(digests) == 2


def test_the_digest_of_a_decision_is_the_same_for_the_same_node_in_any_project_state(
        tmp_path, capsys):
    a = _project(tmp_path, _push(note="x"), _node("noise-a"))
    b = _project(tmp_path, _push(note="x"), _node("noise-b"), _node("noise-c"), project_id="q")
    argv = ("--autonomy", str(_file(tmp_path, _contract())))
    da = _rows(_run(capsys, tmp_path, "--dry-run", *argv)[1])["push"]["decision_digest"]
    db = _rows(_run(capsys, tmp_path, "--dry-run", *argv, project="q")[1])["push"]["decision_digest"]
    assert da != db, "the project is part of what identifies the decision"
    assert a.project_id == "p" and b.project_id == "q"


def test_the_record_names_no_free_text_of_the_node_or_the_contract(tmp_path, capsys):
    store = _project(tmp_path, _push(note="push ü\u0000secret-token-123 to origin"))
    contract = _contract(decision_policy="the captain's private words")
    assert _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, contract)))[0] == 0
    text = _records(store)[0].read_text()
    assert "secret-token" not in text and "private words" not in text and text.isascii()


# --- P: the production path and the module's own arguments ---------------------------------

def test_the_command_is_what_the_module_is_reached_through(tmp_path, capsys, monkeypatch):
    """`hoh project route-decisions` -> `cmd_project` -> `_project_route_decisions` ->
    `governance.route_project_decisions`: the module's function is called by the command and
    by nothing else a test arranged."""
    from hoh import governance

    store = _project(tmp_path, _push())
    seen = []
    real = governance.route_project_decisions

    def spy(*args, **kwargs):
        seen.append((args[0].project_id, kwargs.get("dry_run")))
        return real(*args, **kwargs)

    monkeypatch.setattr(governance, "route_project_decisions", spy)
    code, _, _ = _run(capsys, tmp_path, "--autonomy", str(_file(tmp_path, _contract())))
    assert code == 0 and seen == [("p", False)]
    assert len(_records(store)) == 1


def test_the_module_refuses_arguments_that_are_not_what_they_must_be(tmp_path):
    g = _gov()
    store = _project(tmp_path, _push())
    ok = _contract()
    for contract in (None, {}, "c"):
        with pytest.raises(g.RoutingRefused):
            g.route_project_decisions(store, contract, tmp_path)
    for runs in (None, 7, ["x"], b"x", str(tmp_path / "nowhere"), str(tmp_path / "contract.json"),
                 "a\x00b"):
        with pytest.raises(g.RoutingRefused):
            g.route_project_decisions(store, ok, runs)
    for digest in ("", "x", 7, b"0" * 16, ok.digest().upper()):
        with pytest.raises(g.RoutingRefused):
            g.route_project_decisions(store, ok, tmp_path, expect_digest=digest)
    assert _records(store) == []


def test_a_graph_without_a_frontier_is_refused(tmp_path):
    g = _gov()
    store = ProjectStore(tmp_path, "p")
    store.create(ProjectState(project_id="p", repo_path=str(tmp_path),
                              nodes=[_push("twin"), _push("twin")]))
    with pytest.raises(g.RoutingRefused):
        g.route_project_decisions(store, _contract(), tmp_path)


def test_the_routing_reader_of_the_wave_is_unchanged_by_the_shared_log_helper(tmp_path):
    """`plan_continuation` and the router read the logs through one helper now: a RUNNING node's
    stop and an unreadable log still hold a wave (the candidate's own cases, once more)."""
    g = _gov()
    contract = _unattended()
    stop = _unattended(Escalation.SYSTEM_TWO).model_copy(
        update={"escalation": {**_ESCALATION, "EXTERNAL": Escalation.SYSTEM_TWO,
                               "decision": Escalation.STOP_PROGRAM}})
    assert Governor(contract=stop, run_dir=tmp_path / "rp").decide(
        decision="d", kind="decision", delegable=False).handling is Handling.STOP_PROGRAM
    state = ProjectState(project_id="p", repo_path="/x", nodes=[
        _node("P", Lifecycle.RUNNING, run_id="rp"), _node("I1")])
    plan = g.plan_continuation(state, contract, wave_id="w1", runs_dir=tmp_path,
                               depends_on=lambda a, b: False)
    assert plan.outcome == "HOLD" and plan.reason == "PROGRAMME_STOPPED"
