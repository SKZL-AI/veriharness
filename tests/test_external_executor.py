"""P3-32 slice 4: the bounded executor of pre-authorised external actions.

`governance.resolve_routed_decisions` has answered an EXTERNAL decision "within the pre-authorisation"
since P3-31/32 slice 2, and says `executes: false`. The operator decided (2026-10-20) that VeriHarness
carries such an action out itself when, and only when, the contract names where it may act, how many
times, and that publishing there is pre-authorised (`extexec.execute_answered_decisions`, called by the
loop's pass beside the resolver). Everything else stays with a person.

What is real: `cli.main` (`hoh project drive`), the registered driver, `drive_governed`,
`ProjectController`, `governance`, `extexec`, `bind` and `Governor`. What is replaced is the process
boundary (the launcher is a fake that "runs" a node by returning a verdict) and the one thing that would
touch the outside world: the verbs. `extexec.production_actors()` registers none; a test registers a spy
that records what it was asked and does nothing -- no network, no repository, no external action. A test
reads nothing outside `tmp_path` and this directory (the driver factory is imported from here, as
`driver.json` registers it) and never removes anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hoh import cli, extexec, governance
from hoh.autonomy import (
    AutonomyContract, AutonomyMode, Escalation, Horizon, NeedsYou, ReleasePolicy,
)
from hoh.contracts import Budgets
from hoh.governance import bind
from hoh.orchestrator import (
    GateRunner, HaltClass, MergeResult, ProjectController, RunOutcome, RunVerdict,
    write_lock,
)
from hoh.project import ActionClass, GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.succession import MasterAuthority, StaleMaster

READY, MERGED = Lifecycle.READY, Lifecycle.MERGED
EXTERNAL, INTERNAL = ActionClass.EXTERNAL, ActionClass.INTERNAL
NOW = "2026-10-20T12:00:00Z"
CREATED = "2026-10-20T11:50:00Z"
HERE = Path(__file__).resolve().parent
#: spelled out here, not imported: the name is part of the contract (a project registers a file by it)
AUTONOMY_FILE = "autonomy-contract.json"
TARGET = "origin/staging"
#: the ceilings are part of the contract, spelled out here and not read from the module under test
MAX_ACT_BYTES, MAX_ACT_RECORDS, MAX_DETAIL = 16 * 1024, 4096, 200
#: the driver factory reads its world from here (the module is imported by name through `driver.json`)
CONFIG: dict = {}


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(governance, "utcnow", lambda: NOW)


# --- the world -------------------------------------------------------------------------------

def _contract(*, external=1, target=TARGET, policy=ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET,
              release=Escalation.AUTO_POLICY, epoch=0, **over) -> AutonomyContract:
    """UNATTENDED_LONG_HORIZON that pre-authorises `external` external actions and, under
    AUTO_PUBLISH_PREAUTHORIZED_TARGET, publishing to `target`. Any other field is replaced by `over`."""
    auto = policy is ReleasePolicy.AUTO_PUBLISH_PREAUTHORIZED_TARGET
    body = AutonomyContract(
        contract_id="c-exec", mode=AutonomyMode.UNATTENDED_LONG_HORIZON, needs_you=NeedsYou.OFF,
        scope=("src/hoh/",),
        horizon=Horizon(max_wallclock_seconds=3600, review_after_seconds=900, max_concurrency=4,
                        max_external_actions=external),
        budgets=Budgets(max_dispatches=50, max_wallclock_seconds=3600),
        release_policy=policy, preauthorized_target=target if auto else None,
        allowed_action_classes=(INTERNAL, EXTERNAL),
        escalation={"INTERNAL": Escalation.AUTO_POLICY, "decision": Escalation.SYSTEM_TWO,
                    "release": release if auto else Escalation.PARK_BRANCH_AND_CONTINUE,
                    "scope": Escalation.PARK_BRANCH_AND_CONTINUE,
                    "EXTERNAL": Escalation.SYSTEM_TWO},
        program_epoch=epoch).model_dump(mode="json")
    body.update(over)
    return AutonomyContract.model_validate(body)


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class Launcher:
    """The `RunLauncher` protocol without a process: every node is accepted. `launched` is the order of
    every dispatch -- an external action is never one of them."""

    wave_width = 1

    def __init__(self, root: Path, contract: AutonomyContract):
        self.root, self.contract = Path(root), contract
        self.launched: list[str] = []
        self.merged: list[str] = []

    def action_class(self, node):
        return node.action_class

    def depends_on(self, a, b):
        wa, wb = set(a.writes), set(b.writes)
        return bool(wa & wb or wa & set(b.semantic_reads) or wb & set(a.semantic_reads))

    def prepare(self, node):
        return None

    def accepted_baseline(self, node):
        return None

    def evaluate(self, node):
        return RunOutcome(RunVerdict.NOT_STARTED, "no run yet")

    def launch(self, node):
        self.launched.append(node.id)
        return RunOutcome(RunVerdict.ACCEPTED, "accepted", candidate="a" * 40, candidate_tree="t" * 16)

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, integrated="b" * 40, candidate=outcome.candidate or "")


def build_driver(store):
    return CONFIG["launcher"], Gates()


class Spy:
    """A verb that records what it was asked and does nothing."""

    def __init__(self, reply="published"):
        self.calls: list[extexec.ActionOrder] = []
        self.reply = reply

    def __call__(self, request):
        self.calls.append(request)
        return self.reply


def _register(monkeypatch, **verbs) -> dict:
    """The verbs `production_actors()` hands the pass: the product's own registers none."""
    monkeypatch.setattr(extexec, "production_actors", lambda: verbs)
    return verbs


def _node(node_id, lifecycle=READY, action=INTERNAL, **kw) -> TaskNode:
    kw.setdefault("writes", [f"{node_id}.txt"])
    return TaskNode(id=node_id, lifecycle=lifecycle, action_class=action, **kw)


def _publish(node_id="publish", target=TARGET, verb="publish", note=None) -> TaskNode:
    return _node(node_id, action=EXTERNAL, note=note if note is not None else f"external-action {verb} {target}",
                 writes=[])


def _base() -> TaskNode:
    return _node("base", MERGED, run_id="base")


def _pin(root: Path, run_id: str, contract: AutonomyContract) -> None:
    run = root / run_id
    run.mkdir(exist_ok=True)
    bind(run, contract=contract)


def _world(tmp_path, nodes, *, contract=None, pinned=("base",)):
    """The project `p` under `root`, its driver and its contract registered, the runs in `pinned` pinned."""
    contract = contract or _contract()
    root = tmp_path / "root"
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=list(nodes), created_at=CREATED))
    (store.dir / AUTONOMY_FILE).write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    for run_id in pinned:
        _pin(root, run_id, contract)
    launcher = Launcher(root, contract)
    CONFIG.clear()
    CONFIG["launcher"] = launcher
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_external_executor:build_driver", "path": str(HERE)}), encoding="utf-8")
    return root, store, launcher, contract


def _drive(capsys, root):
    code = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    return code, json.loads(capsys.readouterr().out)


def _add(store, *nodes) -> None:
    """New independent work arrives between two drives (another state version)."""
    state = store.read_state()
    state.nodes.extend(nodes)
    store.write_state(state)


def _lifecycles(store) -> dict:
    return {n.id: n.lifecycle for n in store.read_state().nodes}


def _acts(store) -> dict:
    """The act records of the project by file name (a regular file holding an object)."""
    directory = store.dir / extexec.ACTS_DIR
    found = {}
    for p in sorted(directory.iterdir()) if directory.is_dir() else []:
        if p.is_symlink() or not stat.S_ISREG(os.lstat(p).st_mode):
            continue
        try:
            found[p.name] = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
    return found


def _tree(path: Path) -> dict:
    seen = {}
    for base, dirs, files in os.walk(path):
        for name in ["", *dirs, *files]:
            p = os.path.join(base, name) if name else base
            info = os.lstat(p)
            try:
                digest = hashlib.sha256(Path(p).read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            except OSError:                       # a record a test made unreadable on purpose
                digest = None
            seen[os.path.relpath(p, path)] = (info.st_mode, info.st_size, digest)
    return seen


def _answered_world(tmp_path, nodes, contract=None):
    """The project with its decisions routed and answered (nothing executed), driven by hand."""
    root, store, launcher, contract = _world(tmp_path, nodes, contract=contract, pinned=())
    governance.route_project_decisions(store, contract, root)
    governance.resolve_routed_decisions(store, contract, root)
    return root, store, contract


def _execute(store, root, contract, **verbs):
    return extexec.execute_answered_decisions(store, contract, root, actors=verbs)


def _rows(report) -> dict:
    return {d.node_id: (d.outcome, d.reason) for d in report.decisions}


def _steps(out, prefix) -> list[str]:
    return [s for s in out["steps"] if s.startswith(prefix)]


# --- E: the action that is carried out ------------------------------------------------------

def test_hoh_project_drive_executes_the_preauthorised_action_and_nothing_else(tmp_path, capsys, monkeypatch):
    """Through the product's command. The external action at the head is routed, answered (the contract
    pre-authorises one), and carried out by the verb registered for it -- once, with the attempt recorded
    before the verb is called and the outcome after. The independent node still runs; the launcher is never
    asked to dispatch the action and the action's node is not settled."""
    spy = _register(monkeypatch, publish=Spy())["publish"]
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")])
    code, out = _drive(capsys, root)
    assert launcher.launched == ["i1"] and launcher.merged == ["i1"], out
    assert _lifecycles(store) == {"base": MERGED, "publish": READY, "i1": MERGED}
    assert out["halt"] == HaltClass.BLOCKED_EXTERNAL.value and code == 1
    kinds = [s.split(":")[0] for s in out["steps"]]
    assert kinds.index("RESOLUTION_ANSWERED") < kinds.index("EXTERNAL_EXECUTED") < kinds.index("CONTINUATION"), kinds
    assert _steps(out, "EXTERNAL_EXECUTED") == ["EXTERNAL_EXECUTED:publish"]
    [request] = spy.calls
    digest = next(iter(_acts(store)))[:16]
    attempt_name, outcome_name = f"{digest}-{contract.digest()}.attempt.json", f"{digest}-{contract.digest()}.outcome.json"
    assert sorted(_acts(store)) == [attempt_name, outcome_name]
    attempt, outcome = _acts(store)[attempt_name], _acts(store)[outcome_name]
    answer = f"{digest}-{contract.digest()}.json"
    answer_bytes = (store.dir / governance.RESOLUTIONS_DIR / answer).read_bytes()
    assert attempt == {
        "schema": "hoh-external-act/1", "phase": "ATTEMPT", "project_id": "p", "node_id": "publish",
        "decision_digest": digest, "contract_id": "c-exec", "contract_digest": contract.digest(),
        "program_epoch": 0, "verb": "publish", "target": TARGET, "answer": answer,
        "answer_sha256": hashlib.sha256(answer_bytes).hexdigest(), "ordinal": 1, "pre_authorized": 1,
        "release_policy": "AUTO_PUBLISH_PREAUTHORIZED_TARGET"}
    attempt_sha = hashlib.sha256((store.dir / extexec.ACTS_DIR / attempt_name).read_bytes()).hexdigest()
    assert outcome == {
        "schema": "hoh-external-act/1", "phase": "OUTCOME", "project_id": "p", "node_id": "publish",
        "decision_digest": digest, "contract_id": "c-exec", "contract_digest": contract.digest(),
        "program_epoch": 0, "verb": "publish", "target": TARGET, "outcome": "EXECUTED",
        "detail": "published", "attempt_sha256": attempt_sha}
    assert request == extexec.ActionOrder("p", "publish", "publish", TARGET, digest, contract.digest(), attempt_sha)


def test_the_attempt_is_recorded_before_the_verb_is_called_and_the_outcome_after(tmp_path, monkeypatch):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    seen = {}

    def verb(request):
        seen["at call"] = sorted(_acts(store))
        return "ok"

    report = _execute(store, root, contract, publish=verb)
    assert _rows(report) == {"publish": (extexec.EXECUTED, "")}
    assert [n.rsplit(".", 2)[1] for n in seen["at call"]] == ["attempt"], "the attempt exists, the outcome does not"
    assert sorted(n.rsplit(".", 2)[1] for n in _acts(store)) == ["attempt", "outcome"]


def test_an_action_that_is_carried_out_changes_no_state_and_settles_nothing(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    before = (store.dir / "project.json").read_bytes()
    _execute(store, root, contract, publish=Spy())
    assert (store.dir / "project.json").read_bytes() == before
    assert _lifecycles(store)["publish"] is READY


def test_a_further_drive_over_a_finished_act_repeats_nothing_and_writes_nothing(tmp_path, capsys, monkeypatch):
    """Terminal records are quiet: the act is made once; a later drive (another state version) reads it,
    says nothing and leaves every byte of the acts as it was."""
    spy = _register(monkeypatch, publish=Spy())["publish"]
    root, store, launcher, _ = _world(tmp_path, [_base(), _publish(), _node("i1")])
    _drive(capsys, root)
    assert len(spy.calls) == 1 and len(_acts(store)) == 2
    kept = _tree(store.dir / extexec.ACTS_DIR)
    for later in ("i2", "i3"):
        _add(store, _node(later))
        code, again = _drive(capsys, root)
        assert launcher.merged[-1] == later, again
        assert len(spy.calls) == 1 and _tree(store.dir / extexec.ACTS_DIR) == kept
        assert not _steps(again, "EXTERNAL"), again["steps"]


def test_a_failed_act_is_recorded_said_and_never_repeated(tmp_path, capsys, monkeypatch):
    def fails(request):
        raise RuntimeError("the remote said no")

    verbs = _register(monkeypatch, publish=fails)
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")])
    code, out = _drive(capsys, root)
    assert launcher.merged == ["i1"] and _steps(out, "EXTERNAL_FAILED") == ["EXTERNAL_FAILED:publish"]
    [outcome] = [b for b in _acts(store).values() if b["phase"] == "OUTCOME"]
    assert (outcome["outcome"], outcome["detail"]) == ("FAILED", "RuntimeError: the remote said no")
    calls = []
    verbs["publish"] = lambda request: calls.append(request)
    _add(store, _node("i2"))
    code, again = _drive(capsys, root)
    assert launcher.merged == ["i1", "i2"] and not _steps(again, "EXTERNAL")
    assert calls == [] and len(_acts(store)) == 2, "a failed act is a result, not a reason to try again"


def test_a_verb_that_crashes_the_process_leaves_an_attempt_that_is_never_repeated(tmp_path, monkeypatch):
    """The attempt is the counter and exists before the act: a crash inside it still counts, and the next
    drive does not know what the verb did, so it does not do it again -- it says so and leaves it to a person.
    (The controller is run directly: a crashed session's lease is held until it expires.)"""
    class Crash(BaseException):
        pass

    def crashes(request):
        raise Crash()

    verbs = _register(monkeypatch, publish=crashes)
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")])
    with pytest.raises(Crash):
        ProjectController(store, launcher, Gates(), autonomy=contract).run()
    assert [n.rsplit(".", 2)[1] for n in _acts(store)] == ["attempt"]
    spy = Spy()
    verbs["publish"] = spy
    _add(store, _node("i2"))
    result = ProjectController(store, launcher, Gates(), autonomy=contract).run()
    assert spy.calls == [] and "EXTERNAL_WITH_PERSON" in [s.kind for s in result.steps]
    assert launcher.merged == ["i1", "i2"], "the independent work is not held back"
    assert [n.rsplit(".", 2)[1] for n in _acts(store)] == ["attempt"]


@pytest.mark.parametrize("first", ["finished", "failed", "crashed"])
def test_a_decision_is_attempted_once_even_under_another_contract(tmp_path, first):
    """A new contract digest (another epoch's contract, an amended one) is a new authorisation, not a new decision:
    the node is not settled, so the same action must not be made a second time -- nor after a crash, nor after a failure."""
    class Crash(BaseException):
        pass

    def crashes(request):
        raise Crash()

    def fails(request):
        raise RuntimeError("no")

    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")], _contract(external=2))
    verb = {"finished": Spy(), "failed": fails, "crashed": crashes}[first]
    if first == "crashed":
        with pytest.raises(Crash):
            _execute(store, root, contract, publish=verb)
    else:
        _execute(store, root, contract, publish=verb)
    other = _contract(external=2, contract_id="c-exec-2")
    assert other.digest() != contract.digest()
    governance.route_project_decisions(store, other, root)
    governance.resolve_routed_decisions(store, other, root)
    assert [a.node_id for a in governance.answered_external_decisions(store, other, root)] == ["publish"]
    spy = Spy()
    expected = {"finished": (extexec.DONE_BEFORE, extexec.ALREADY_EXECUTED),
                "failed": (extexec.DONE_BEFORE, extexec.ALREADY_FAILED),
                "crashed": (extexec.LEFT_WITH_PERSON, extexec.ATTEMPT_UNFINISHED)}[first]
    assert _rows(_execute(store, root, other, publish=spy)) == {"publish": expected}
    assert spy.calls == [] and len([n for n in _acts(store) if n.endswith(".attempt.json")]) == 1


def test_an_unfinished_attempt_is_a_reason_a_person_is_told(tmp_path, monkeypatch):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    class Crash(BaseException):
        pass

    def crashes(request):
        raise Crash()

    with pytest.raises(Crash):
        _execute(store, root, contract, publish=crashes)
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {"publish": (extexec.LEFT_WITH_PERSON, extexec.ATTEMPT_UNFINISHED)}
    assert spy.calls == []


# --- R: the release policy and the target ---------------------------------------------------

@pytest.mark.parametrize("policy", [ReleasePolicy.NEVER_AUTO_PUBLISH, ReleasePolicy.HUMAN_APPROVAL,
                                    ReleasePolicy.AUTO_PREPARE_ONLY])
def test_a_release_policy_that_does_not_name_the_target_executes_nothing_and_looks_at_nothing(
        tmp_path, capsys, monkeypatch, policy):
    """NEVER_AUTO_PUBLISH takes precedence over everything the node says: the action is answered (the
    resolver's job) and stays with a person -- no decision is looked at, no act directory, no call, no step."""
    spy = _register(monkeypatch, publish=Spy())["publish"]
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")],
                                             contract=_contract(policy=policy))
    code, out = _drive(capsys, root)
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()
    assert not _steps(out, "EXTERNAL"), out["steps"]
    assert _steps(out, "RESOLUTION_ANSWERED"), "the answer is the resolver's and is unchanged"

    def looked(*args, **kwargs):
        raise AssertionError("a decision was looked at under a policy that names no target")

    monkeypatch.setattr(governance, "answered_external_decisions", looked)
    report = _execute(store, root, contract, publish=spy)
    assert report.decisions == () and spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()


def test_a_policy_that_does_not_name_the_target_wins_over_a_contract_that_says_otherwise_everywhere_else(
        tmp_path, monkeypatch):
    """`model_copy` does not validate (a reviewer built an unattended contract with Needs-You ON that way):
    NEVER_AUTO_PUBLISH beside a target, a release escalation by policy and a declared action still executes
    nothing -- the policy is the first thing asked, not the last."""
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    loose = _contract().model_copy(update={"release_policy": ReleasePolicy.NEVER_AUTO_PUBLISH})
    assert loose.preauthorized_target == TARGET and loose.escalation["release"] is Escalation.AUTO_POLICY
    monkeypatch.setattr(governance, "answered_external_decisions", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("looked")))
    spy = Spy()
    assert _execute(store, root, loose, publish=spy).decisions == () and spy.calls == []


@pytest.mark.parametrize("note", [
    "external-action publish origin/staging-2", "external-action publish origin", "external-action publish ORIGIN/STAGING",
    "external-action publish origin/staging/", "external-action publish origin//staging"])
def test_a_target_the_contract_does_not_name_is_left_with_a_person(tmp_path, capsys, monkeypatch, note):
    spy = _register(monkeypatch, publish=Spy())["publish"]
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(note=note), _node("i1")])
    code, out = _drive(capsys, root)
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()
    assert _steps(out, "EXTERNAL_WITH_PERSON") == ["EXTERNAL_WITH_PERSON:publish"]
    [row] = _execute(store, root, contract, publish=spy).decisions
    assert (row.outcome, row.reason) == (extexec.LEFT_WITH_PERSON, extexec.TARGET_NOT_PREAUTHORIZED)


@pytest.mark.parametrize("note", [
    "", "push the tag v1 to origin", "external-action publish", "external-action publish origin/staging extra",
    "external-action  publish origin/staging", "External-Action publish origin/staging",
    "external-action Publish origin/staging", "external-action publish ./origin/staging",
    "external-action publish origin/staging\nexternal-action publish origin/staging",
    "external-action publish origin/stagingé"])
def test_a_note_that_is_not_exactly_one_declaration_declares_no_action(tmp_path, monkeypatch, note):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(note=note), _node("i1")])
    spy = Spy()
    assert extexec.declared_action(note) is None
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.NO_ACTION_DECLARED)}
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()


@pytest.mark.parametrize("note", [
    "external-action publish origin/staging\n", " external-action publish origin/staging",
    "external-action publish origin/staging ", "external-action publish origin/staging\r",
    "external-action\tpublish origin/staging", "external-action publish\u00a0origin/staging",
    "external-action publish origin/staging\x00", "external-action publish " + "t" * 257,
    "external-action " + "v" * 33 + " origin/staging"])
def test_a_note_with_anything_around_the_declaration_declares_none(note):
    """Whatever the graph keeps of a note (the model strips its edges), the reader takes it whole."""
    assert extexec.declared_action(note) is None


def test_a_declaration_names_a_verb_and_a_target():
    assert extexec.declared_action("external-action publish origin/staging") == ("publish", "origin/staging")
    assert extexec.declared_action("external-action git-push user@host:repo.git") == ("git-push", "user@host:repo.git")
    assert extexec.declared_action(None) is None and extexec.declared_action(b"external-action a b") is None


def test_a_verb_that_nobody_registered_is_left_with_a_person_and_the_product_registers_none(tmp_path, capsys):
    assert dict(extexec.production_actors()) == {} and extexec.production_actors() is not extexec.production_actors()
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")])
    code, out = _drive(capsys, root)
    assert launcher.merged == ["i1"] and _steps(out, "EXTERNAL_WITH_PERSON") == ["EXTERNAL_WITH_PERSON:publish"]
    assert not (store.dir / extexec.ACTS_DIR).exists()
    [row] = _execute(store, root, contract, deploy=Spy()).decisions
    assert (row.outcome, row.reason, row.verb) == (extexec.LEFT_WITH_PERSON, extexec.NO_ACTOR, "publish")
    [row] = _execute(store, root, contract, publish="not callable").decisions
    assert row.reason == extexec.NO_ACTOR


@pytest.mark.parametrize("release", [Escalation.PARK_BRANCH_AND_CONTINUE, Escalation.HUMAN_IF_AVAILABLE,
                                     Escalation.STOP_PROGRAM, Escalation.ABORT_BRANCH])
def test_a_contract_that_does_not_resolve_releases_by_policy_executes_nothing(tmp_path, release):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")],
                                            _contract(release=release))
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.RELEASE_NOT_RESOLVED_BY_POLICY)}
    assert spy.calls == []


@pytest.mark.parametrize("release", [Escalation.AUTO_POLICY, Escalation.SYSTEM_TWO, Escalation.SYSTEM_ONE_THEN_SYSTEM_TWO])
def test_a_contract_that_resolves_releases_by_policy_lets_the_action_through(tmp_path, release):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")], _contract(release=release))
    assert _rows(_execute(store, root, contract, publish=Spy())) == {"publish": (extexec.EXECUTED, "")}


def test_a_decision_nobody_answered_is_never_executed(tmp_path, capsys, monkeypatch):
    """No pre-authorised number, or an EXTERNAL escalation that asks a person: the resolver answers nothing,
    so the executor has nothing to carry out."""
    spy = _register(monkeypatch, publish=Spy())["publish"]
    for n, contract in enumerate([_contract(external=None),
                                  _contract(external=1, escalation={
                                      "INTERNAL": "AUTO_POLICY", "decision": "SYSTEM_TWO", "release": "AUTO_POLICY",
                                      "scope": "PARK_BRANCH_AND_CONTINUE", "EXTERNAL": "HUMAN_IF_AVAILABLE"})]):
        root, store, launcher, _ = _world(tmp_path / str(n), [_base(), _publish(), _node("i1")], contract=contract)
        code, out = _drive(capsys, root)
        assert launcher.merged == ["i1"] and spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists(), out


def test_a_tampered_answer_is_no_answer(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    [record] = (store.dir / governance.RESOLUTIONS_DIR).iterdir()
    body = json.loads(record.read_text(encoding="utf-8"))
    for field in ("executes", "approves"):
        record.write_text(json.dumps({**body, field: True}, sort_keys=True, indent=1) + "\n", encoding="ascii")
        spy = Spy()
        assert _rows(_execute(store, root, contract, publish=spy)) == {}, field
        assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()


def test_the_answers_are_read_without_writing_anything(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _publish("second"), _node("i1")],
                                            _contract(external=1))
    before = _tree(store.dir)
    [answered] = governance.answered_external_decisions(store, contract, root)
    assert _tree(store.dir) == before
    name = f"{answered.decision_digest}-{contract.digest()}.json"
    assert (answered.node_id, answered.note, answered.record_name) == ("publish", f"external-action publish {TARGET}", name)
    assert answered.record_sha256 == hashlib.sha256((store.dir / governance.RESOLUTIONS_DIR / name).read_bytes()).hexdigest()


def test_an_answer_that_exists_only_in_a_dry_run_is_not_an_answer(tmp_path):
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")], pinned=())
    governance.route_project_decisions(store, contract, root)
    assert governance.answered_external_decisions(store, contract, root) == ()
    assert not (store.dir / governance.RESOLUTIONS_DIR).exists()


# --- H: a held wave carries out nothing ---------------------------------------------------

def _wallclock_spent_contract() -> AutonomyContract:
    """The contract of `_contract()` whose wallclock limit (300 s) the project (created 600 s before NOW) has passed."""
    return _contract(
        horizon=Horizon(max_wallclock_seconds=300, review_after_seconds=900, max_concurrency=4,
                        max_external_actions=1).model_dump(mode="json"),
        budgets=Budgets(max_dispatches=50, max_wallclock_seconds=300).model_dump(mode="json"))


@pytest.mark.parametrize("held", ["contract not pinned by any run", "wallclock limit reached"])
def test_a_wave_the_contract_holds_carries_out_nothing_though_the_action_is_answered(tmp_path, capsys, monkeypatch, held):
    """The answer is the resolver's and is recorded; the act is the consequential step and waits for the contract's
    own decision to continue -- a limit that is reached, a contract no run is bound to: a person is called first."""
    spy = _register(monkeypatch, publish=Spy())["publish"]
    if held == "wallclock limit reached":
        root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")],
                                                 contract=_wallclock_spent_contract())
    else:
        root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1")], pinned=())
    code, out = _drive(capsys, root)
    assert _steps(out, "RESOLUTION_ANSWERED") == ["RESOLUTION_ANSWERED:publish"], out["steps"]
    assert any(s.startswith("CONTINUATION:") for s in out["steps"]), out["steps"]
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()
    assert not _steps(out, "EXTERNAL"), out["steps"]
    assert launcher.launched == [], "a held wave dispatches nothing either"


# --- N: the pre-authorised number -----------------------------------------------------------

def test_the_number_of_attempts_is_the_pre_authorised_number_and_one_decision_is_one_attempt(tmp_path, capsys, monkeypatch):
    spy = _register(monkeypatch, publish=Spy())["publish"]
    nodes = [_base(), _publish("first"), _publish("second"), _node("i1"), _node("i2", dependencies=["i1"])]
    root, store, launcher, contract = _world(tmp_path, nodes, contract=_contract(external=1))
    code, out = _drive(capsys, root)
    code, out = _drive(capsys, root)
    assert [r.node_id for r in spy.calls] == ["first"], "one number, one act: the second is not even answered"
    assert [b["ordinal"] for b in _acts(store).values() if b["phase"] == "ATTEMPT"] == [1]


def _stray_attempt(store, contract, *, epoch=0, name="a" * 16) -> None:
    """An attempt of another decision, written exactly as the executor writes one."""
    body = {"schema": extexec.ACT_SCHEMA, "project_id": "p", "node_id": "elsewhere", "decision_digest": name,
            "contract_id": "c-exec", "contract_digest": contract.digest(), "program_epoch": epoch,
            "verb": "publish", "target": TARGET, "phase": "ATTEMPT", "answer": "x.json", "answer_sha256": "0" * 64,
            "ordinal": 1, "pre_authorized": 9, "release_policy": "AUTO_PUBLISH_PREAUTHORIZED_TARGET"}
    directory = store.dir / extexec.ACTS_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / f"{name}-{contract.digest()}.attempt.json").write_bytes(extexec._record(body))


def test_the_executor_counts_the_attempts_itself_not_the_answers(tmp_path):
    """The resolver answered both decisions (the number is two); an attempt of another decision is already
    on record, so the second answered decision is the one past the number."""
    root, store, contract = _answered_world(tmp_path, [_base(), _publish("first"), _publish("second"), _node("i1")],
                                            _contract(external=2))
    _stray_attempt(store, contract)
    spy = Spy()
    report = _execute(store, root, contract, publish=spy)
    assert _rows(report) == {"first": (extexec.EXECUTED, ""),
                             "second": (extexec.LEFT_WITH_PERSON, extexec.PREAUTHORIZATION_SPENT)}
    assert len(spy.calls) == 1
    assert sorted(b["ordinal"] for b in _acts(store).values() if b["phase"] == "ATTEMPT") == [1, 2]


def test_an_attempt_without_an_outcome_counts_against_the_number(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish("first"), _node("i1")], _contract(external=1))
    _stray_attempt(store, contract)
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "first": (extexec.LEFT_WITH_PERSON, extexec.PREAUTHORIZATION_SPENT)}
    assert spy.calls == []


def test_the_number_belongs_to_the_programme_epoch(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")], _contract(external=1, epoch=2))
    _stray_attempt(store, contract, epoch=1)
    assert _rows(_execute(store, root, contract, publish=Spy())) == {"publish": (extexec.EXECUTED, "")}
    _stray_attempt(store, contract, epoch=2, name="b" * 16)
    assert sorted(b["program_epoch"] for b in _acts(store).values() if b["phase"] == "ATTEMPT") == [1, 2, 2]


@pytest.mark.parametrize("external", [0, None])
def test_a_contract_that_pre_authorises_no_number_executes_nothing_even_if_an_answer_is_handed_over(
        tmp_path, monkeypatch, external):
    """The resolver writes no answer under such a contract; the executor does not rely on that: handed a decision
    that claims to be answered, it counts the number itself."""
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")], _contract(external=1))
    [answered] = governance.answered_external_decisions(store, contract, root)
    none = _contract(external=external)
    monkeypatch.setattr(governance, "answered_external_decisions", lambda *a, **k: (answered,))
    spy = Spy()
    assert _rows(_execute(store, root, none, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.NO_PREAUTHORIZATION)}
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()


def test_two_answered_decisions_are_two_attempts_when_the_number_is_two(tmp_path, capsys, monkeypatch):
    spy = _register(monkeypatch, publish=Spy())["publish"]
    nodes = [_base(), _publish("first"), _publish("second"), _node("i1")]
    root, store, launcher, contract = _world(tmp_path, nodes, contract=_contract(external=2))
    _drive(capsys, root)
    assert sorted(r.node_id for r in spy.calls) == ["first", "second"]
    assert sorted(b["ordinal"] for b in _acts(store).values() if b["phase"] == "ATTEMPT") == [1, 2]


# --- D: what cannot be read or recorded -----------------------------------------------------

_STRAY = "a" * 16 + "-" + "b" * 16


def _damage(directory: Path, how: str) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    shape = {"schema": extexec.ACT_SCHEMA, "project_id": "p", "decision_digest": "a" * 16,
             "contract_digest": "b" * 16, "program_epoch": 0, "phase": "ATTEMPT"}
    target = directory / f"{_STRAY}.attempt.json"
    if how == "garbage":
        target.write_bytes(b"not json")
    elif how == "not an object":
        target.write_bytes(b"[1, 2]\n")
    elif how == "wrong project":
        target.write_bytes(extexec._record({**shape, "project_id": "other"}))
    elif how == "wrong schema":
        target.write_bytes(extexec._record({**shape, "schema": "hoh-external-act/2"}))
    elif how == "wrong phase":
        target.write_bytes(extexec._record({**shape, "phase": "OUTCOME"}))
    elif how == "name differs from digests":
        target.write_bytes(extexec._record({**shape, "decision_digest": "0" * 16}))
    elif how == "epoch not a number":
        target.write_bytes(extexec._record({**shape, "program_epoch": "0"}))
    elif how == "not the bytes the executor writes":
        target.write_bytes(json.dumps(shape).encode("ascii"))
    elif how == "oversize":
        target.write_bytes(b"x" * (MAX_ACT_BYTES + 1))
    elif how == "outcome without attempt":
        (directory / f"{_STRAY}.outcome.json").write_bytes(extexec._record({**shape, "phase": "OUTCOME"}))
    elif how == "outcome of another attempt":
        target.write_bytes(extexec._record(shape))
        (directory / f"{_STRAY}.outcome.json").write_bytes(
            extexec._record({**shape, "phase": "OUTCOME", "attempt_sha256": "0" * 64}))
    elif how == "link":
        (directory / "elsewhere").write_bytes(extexec._record(shape))
        target.symlink_to("elsewhere")
    elif how == "fifo":
        os.mkfifo(target)
    elif how == "hard link":
        (directory / "other-name").write_bytes(extexec._record(shape))
        os.link(directory / "other-name", target)
    elif how == "directory":
        target.mkdir()
    elif how == "dangling link":
        target.symlink_to("nowhere")
    elif how == "latin-1 bytes":
        target.write_bytes(extexec._record(shape).replace(b"project_id", "projet_\u00e9".encode("latin-1")))
    elif how == "utf-16":
        target.write_bytes(json.dumps(shape).encode("utf-16"))
    elif how == "nul byte":
        target.write_bytes(extexec._record(shape)[:-1] + b"\x00\n")
    elif how == "mode 000":
        target.write_bytes(extexec._record({**shape, "decision_digest": "a" * 16}))
        target.chmod(0o000)
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["garbage", "not an object", "wrong project", "wrong schema", "wrong phase",
                                 "name differs from digests", "epoch not a number", "not the bytes the executor writes",
                                 "oversize", "outcome without attempt", "outcome of another attempt", "link",
                                 "fifo", "hard link", "directory", "dangling link", "latin-1 bytes", "utf-16",
                                 "nul byte", "mode 000"])
def test_a_directory_of_acts_that_cannot_be_read_executes_nothing_and_says_so(tmp_path, how):
    """A count taken from part of a directory is a count damage can lower: nothing is carried out."""
    if how == "mode 000" and os.geteuid() == 0:
        pytest.skip("the read bit is not enforced for root")
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    _damage(store.dir / extexec.ACTS_DIR, how)
    kept = _tree(store.dir / extexec.ACTS_DIR)
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.ACTS_UNREADABLE)}
    assert spy.calls == [] and _tree(store.dir / extexec.ACTS_DIR) == kept


def _padded_attempt(store, contract, size: int) -> None:
    """A valid, canonical attempt record of exactly `size` bytes (a padding key, as no executor writes)."""
    directory = store.dir / extexec.ACTS_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    body = {"schema": extexec.ACT_SCHEMA, "project_id": "p", "node_id": "elsewhere", "decision_digest": "c" * 16,
            "contract_digest": contract.digest(), "program_epoch": 0, "phase": "ATTEMPT", "pad": ""}
    room = size - len(extexec._record(body))
    assert room >= 0
    data = extexec._record({**body, "pad": "x" * room})
    assert len(data) == size
    (directory / f"{'c' * 16}-{contract.digest()}.attempt.json").write_bytes(data)


@pytest.mark.parametrize("extra,reason", [(0, extexec.PREAUTHORIZATION_SPENT), (1, extexec.ACTS_UNREADABLE)])
def test_an_act_record_of_exactly_the_largest_size_is_read_and_one_byte_more_is_not(tmp_path, extra, reason):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    _padded_attempt(store, contract, MAX_ACT_BYTES + extra)
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {"publish": (extexec.LEFT_WITH_PERSON, reason)}
    assert spy.calls == []


def test_the_ceilings_are_the_contracts():
    assert (extexec.MAX_ACT_BYTES, extexec.MAX_ACT_RECORDS, extexec.MAX_DETAIL) == (MAX_ACT_BYTES, MAX_ACT_RECORDS, MAX_DETAIL)


def test_a_directory_of_more_act_records_than_the_ceiling_is_not_read(tmp_path, monkeypatch):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")], _contract(external=3))
    monkeypatch.setattr(extexec, "MAX_ACT_RECORDS", 3)
    for n in range(3):
        _stray_attempt(store, contract, name=f"{n:016x}")
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.PREAUTHORIZATION_SPENT)}, "three records are read: three attempts"
    _stray_attempt(store, contract, name=f"{4:016x}")
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.ACTS_UNREADABLE)}, "four are not"
    assert spy.calls == []


@pytest.mark.parametrize("how", ["a file", "a link to a directory"])
def test_an_acts_path_that_is_not_a_directory_of_its_own_executes_nothing(tmp_path, capsys, monkeypatch, how):
    spy = _register(monkeypatch, publish=Spy())["publish"]
    root, store, launcher, contract = _world(tmp_path, [_base(), _publish(), _node("i1"), _node("i2", dependencies=["i1"])])
    path = store.dir / extexec.ACTS_DIR
    if how == "a file":
        path.write_text("not a directory", encoding="utf-8")
    else:
        (tmp_path / "elsewhere").mkdir()
        path.symlink_to(tmp_path / "elsewhere")
    code, out = _drive(capsys, root)
    assert spy.calls == [] and launcher.merged == ["i1", "i2"], "the independent work is not held back"
    assert set(_steps(out, "EXTERNAL_WITH_PERSON")) == {"EXTERNAL_WITH_PERSON:publish"}
    if how == "a file":
        assert path.read_text(encoding="utf-8") == "not a directory"
    else:
        assert not list((tmp_path / "elsewhere").iterdir()), "nothing is written through a link"


def test_an_attempt_that_cannot_be_recorded_means_the_verb_is_not_called(tmp_path, monkeypatch):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    (store.dir / extexec.ACTS_DIR).mkdir(mode=0o500)
    spy = Spy()
    try:
        report = _execute(store, root, contract, publish=spy)
    finally:
        (store.dir / extexec.ACTS_DIR).chmod(0o700)
    if os.geteuid() == 0:                      # a superuser writes into a directory with no write bit
        pytest.skip("the write bit is not enforced for root")
    assert _rows(report) == {"publish": (extexec.LEFT_WITH_PERSON, extexec.ATTEMPT_NOT_RECORDED)}
    assert spy.calls == [] and _acts(store) == {}


def test_an_outcome_that_cannot_be_recorded_is_said_and_the_act_is_not_repeated(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])

    def blocks_its_own_outcome(request):
        (store.dir / extexec.ACTS_DIR / f"{request.decision_digest}-{request.contract_digest}.outcome.json").mkdir()
        return "done"

    report = _execute(store, root, contract, publish=blocks_its_own_outcome)
    assert _rows(report) == {"publish": (extexec.EXECUTED, extexec.OUTCOME_NOT_RECORDED)}
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.ACTS_UNREADABLE)}
    assert spy.calls == []


def test_what_a_verb_says_is_stored_as_short_printable_ascii(tmp_path):
    for n, (reply, expected) in enumerate([("café \x00\x1b[31m ok\n", "caf\\xe9   [31m ok "), ("x" * 1000, "x" * MAX_DETAIL),
                                            (None, ""), (b"bytes", ""), (12, "")]):
        root, store, contract = _answered_world(tmp_path / str(n), [_base(), _publish(), _node("i1")])
        assert _rows(_execute(store, root, contract, publish=Spy(reply))) == {"publish": (extexec.EXECUTED, "")}
        [outcome] = [b for b in _acts(store).values() if b["phase"] == "OUTCOME"]
        assert outcome["detail"] == expected


def test_a_request_cannot_be_written_through_and_an_executor_refuses_what_it_cannot_use(tmp_path):
    request = extexec.ActionOrder("p", "n", "publish", TARGET, "d", "c", "a")
    with pytest.raises(FrozenInstanceError):
        request.target = "elsewhere"
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    with pytest.raises(governance.RoutingRefused):
        extexec.execute_answered_decisions(store, contract, root, actors=["publish"])
    with pytest.raises(governance.RoutingRefused):
        extexec.execute_answered_decisions(store, "not a contract", root, actors={})
    with pytest.raises(governance.RoutingRefused):
        extexec.execute_answered_decisions(store, contract, tmp_path / "no such directory", actors={})
    assert not (store.dir / extexec.ACTS_DIR).exists()


# --- A: authority ---------------------------------------------------------------------------

def test_a_master_that_lost_the_lease_before_a_pass_executes_nothing(tmp_path, monkeypatch):
    spy = _register(monkeypatch, publish=Spy())["publish"]
    root, store, launcher, _ = _world(tmp_path, [_base(), _publish(), _node("i1")])

    class Superseded(ProjectController):
        def _drive(self):
            result = super()._drive()
            if self._allowed is None:                 # a successor takes the lease once the drive ended
                self.authority.release(reason="test: handed over")
                MasterAuthority(store.dir, "sB", serialize=lambda: write_lock(store)).acquire(
                    program_generation=0, note="successor")
            return result

    authority = MasterAuthority(store.dir, "sA", serialize=lambda: write_lock(store))
    authority.acquire(program_generation=0, note="the old master")
    result = Superseded(store, launcher, Gates(), authority=authority).run()
    assert result.halt is HaltClass.SUPERSEDED and launcher.launched == [] and spy.calls == []
    assert not (store.dir / extexec.ACTS_DIR).exists()
    with pytest.raises(StaleMaster):
        authority.check()


# --- S: the programme stop, the durable directory, the digest, the modes ---------------------

def _stopped_world(tmp_path):
    """The answered world with a node blocked `contract_stop` beside it: the one durable stop the programme has."""
    halt = _node("halt", lifecycle=Lifecycle.BLOCKED, blocked_kind="contract_stop", writes=[])
    return _answered_world(tmp_path, [_base(), halt, _publish(), _node("i1")])


def test_a_stopped_programme_executes_nothing_and_says_why(tmp_path):
    """The contract's `kill_switch` declares channels and cannot be set; the stop the running programme has is the
    programme stop (`PROGRAMME_STOPPED`). The executor checks it itself, whatever the pass decided."""
    root, store, contract = _stopped_world(tmp_path)
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.PROGRAMME_STOPPED)}
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()
    assert extexec.PROGRAMME_STOPPED in extexec.VISIBLE_REASONS


def test_the_stop_the_executor_reads_is_the_stop_the_continuation_holds_for(tmp_path):
    root, store, contract = _stopped_world(tmp_path)
    state = store.read_state()
    plan = governance.plan_continuation(state, contract, wave_id="w1", runs_dir=root, depends_on=lambda a, b: False)
    assert (plan.outcome, plan.reason) == ("HOLD", governance.PROGRAMME_STOPPED)
    assert governance.programme_stoppers(store, root) == (("halt",), ())
    clean_root, clean_store, _ = _answered_world(tmp_path / "clean", [_base(), _publish(), _node("i1")])
    assert governance.programme_stoppers(clean_store, clean_root) == ((), ())


def test_a_routing_log_that_cannot_be_read_stops_the_executor_too(tmp_path):
    waiting = _node("w", lifecycle=Lifecycle.BLOCKED, writes=[])
    root, store, contract = _answered_world(tmp_path, [_base(), waiting, _publish(), _node("i1")])
    log = root / "w" / "autonomy"
    log.mkdir(parents=True)
    (log / "routings.jsonl").write_bytes(b"not json\n")
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.ROUTING_LOG_UNREADABLE)}
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()


def test_hoh_project_drive_executes_nothing_while_the_programme_is_stopped(tmp_path, capsys, monkeypatch):
    spy = _register(monkeypatch, publish=Spy())["publish"]
    halt = _node("halt", lifecycle=Lifecycle.BLOCKED, blocked_kind="contract_stop", writes=[])
    root, store, launcher, _ = _world(tmp_path, [_base(), halt, _publish(), _node("i1")])
    code, out = _drive(capsys, root)
    assert spy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists() and not _steps(out, "EXTERNAL"), out["steps"]
    assert launcher.launched == [], "a stopped programme dispatches nothing either"


def test_the_first_act_makes_its_directory_entry_durable_before_the_verb_is_called(tmp_path, monkeypatch):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish("first"), _publish("second"), _node("i1")],
                                            _contract(external=2))
    real, synced = extexec._fsync_directory, []

    def spy_sync(path):
        synced.append((Path(path), (store.dir / extexec.ACTS_DIR).is_dir(),
                       sorted(p.name for p in (store.dir / extexec.ACTS_DIR).iterdir())))
        real(path)

    monkeypatch.setattr(extexec, "_fsync_directory", spy_sync)
    seen = []

    def verb(request):
        seen.append(list(synced))
        return "ok"

    assert _rows(_execute(store, root, contract, publish=verb)) == {"first": (extexec.EXECUTED, ""),
                                                                     "second": (extexec.EXECUTED, "")}
    assert [(p, made, files) for p, made, files in synced] == [(store.dir, True, [])], "once, for the directory made, before any record"
    assert seen[0] == synced and seen[1] == synced


def test_a_directory_entry_that_cannot_be_made_durable_means_the_verb_is_not_called(tmp_path, monkeypatch):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])

    def refuses(path):
        raise OSError("no")

    monkeypatch.setattr(extexec, "_fsync_directory", refuses)
    spy = Spy()
    assert _rows(_execute(store, root, contract, publish=spy)) == {
        "publish": (extexec.LEFT_WITH_PERSON, extexec.ATTEMPT_NOT_RECORDED)}
    assert spy.calls == [] and _acts(store) == {}


def test_the_act_records_and_their_directory_are_private_to_the_owner(tmp_path):
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    _execute(store, root, contract, publish=Spy())
    assert stat.S_IMODE(os.stat(store.dir / extexec.ACTS_DIR).st_mode) == 0o700
    names = sorted(p.name for p in (store.dir / extexec.ACTS_DIR).iterdir())
    assert len(names) == 2
    for name in names:
        assert stat.S_IMODE(os.stat(store.dir / extexec.ACTS_DIR / name).st_mode) == 0o600, name


def test_a_note_that_changed_between_the_answer_and_the_reading_is_not_acted_on(tmp_path, monkeypatch):
    """The decision digest is recomputed from the note that is about to be acted on: an answer binds the note it
    was given for, not another declaration that arrived with the second read of the state."""
    root, store, contract = _answered_world(tmp_path, [_base(), _publish(), _node("i1")])
    real, reads = store.read_state, []

    def second_read_differs():
        state = real()
        reads.append(1)
        if len(reads) == 2:                      # the resolver's read was the first
            state.node("publish").note = f"external-action deploy {TARGET}"
        return state

    monkeypatch.setattr(store, "read_state", second_read_differs)
    publish, deploy = Spy(), Spy()
    assert _execute(store, root, contract, publish=publish, deploy=deploy).decisions == ()
    assert publish.calls == [] and deploy.calls == [] and not (store.dir / extexec.ACTS_DIR).exists()


# --- G: the register row --------------------------------------------------------------------

def test_the_register_row_names_the_probe_the_matrix_reads():
    """P3-32 as this slice leaves it (R-1): the executor is the probe, the row stays PARTIAL with the one thing that
    is left as its exact equivalent (no production actor is registered), and the earlier probes are kept by addition."""
    register = json.loads((HERE.parent / "program" / "v3_3" / "REQUIREMENTS.json").read_text(encoding="utf-8"))
    row = {r["id"]: r for r in register["requirements"]}["P3-32"]
    probe = row["probe"]
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "execute_answered_decisions", "extexec.py")
    path, node = probe["test"].split("::")
    assert path == "tests/test_external_executor.py" and hasattr(sys.modules[__name__], node)
    assert callable(extexec.execute_answered_decisions)
    equivalent = probe["equivalent"]
    for said in ("built and gated", "pre-authorised target", "NEVER_AUTO_PUBLISH", "programme stop honoured",
                 "no production actor is registered yet", "operator decision"):
        assert said in equivalent, said
    assert "recorded, not executed yet" not in equivalent, "the executor exists: the old equivalent is replaced"
    for said in ("production_actors", "NEVER_AUTO_PUBLISH", "preauthorized_target", "ATTEMPT_UNFINISHED", "programme_stoppers"):
        assert said in probe["note"], said
    previous = row["previous_probe"]
    assert (previous["symbol"], previous["replaced_on"]) == ("act_on_limit", "2026-10-20")
    assert "slice 4" in previous["equivalent"] and "previous_probe" in previous


def test_the_matrix_reads_the_row_partial_with_the_exact_equivalent():
    """The register edit is read by the tool that grades the gate: symbol and module, a collected node that pins the
    behaviour, the module reached from an entry point -- and the equivalent caps the row at PARTIAL, because the
    product registers no actor, so nothing is carried out for real."""
    import importlib.util

    from conftest import PROGRAM_REGISTER, needs_evidence
    needs_evidence(PROGRAM_REGISTER)
    spec = importlib.util.spec_from_file_location("capability_probe_executor", HERE.parent / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_probe_executor"] = cm
    spec.loader.exec_module(cm)
    row = {r["id"]: r for r in cm.measure()["capabilities"]}["P3-32"]
    assert row["status"] == cm.PARTIAL, row["evidence"]
    assert any("extexec.py" in e for e in row["evidence"]) and any("CAPPED" in e for e in row["evidence"])
    assert any("the bounded executor is built and gated" in e for e in row["evidence"])
