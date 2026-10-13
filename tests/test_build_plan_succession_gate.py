"""P2's "controller/session replacement" exit is measured by the succession capsule.

F13/M9 of the preserved P2 reviews: session succession (P2-07) read VERIFIED while
`python3 tools/succession.py verify` read DRIFTED, and nothing bound the capsule's
currency to the phase exit. `build_plan.CHECKLIST_GATE_ITEMS` now maps the plan's
`P2.exit-includes.controller-session-replacement` item -- the exit condition the
reviews name succession an ingredient of -- to a fourth instrument,
`build_plan.succession_measure`, which re-derives every capsule field through the
tool's own `check` and is met only when every field is VERIFIED. The item stays
claimed by its requirement, which must be PROVEN besides (a claim never stands in for
an instrument, as for the three checklist gates).

Three kinds of case, as in `test_build_plan_checklist_gate.py`: wiring with the
answer handed in; the instrument over a constructed repository (a scratch `git`
repository carrying every source a capsule field re-derives from); parity with the
tool's own exit code over the real tree, whatever its state.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402
from test_succession_records_window import CAPSULE_REL, _board, _git, _write, seed  # noqa: E402


def _load(name: str, as_name: str):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = module
    spec.loader.exec_module(module)
    return module


bp = _load("build_plan", "build_plan_succession_probe")
su = _load("succession", "succession_gate_probe")

ITEMS = "P2.checklist-gate.all-phase-blocking-checklist-items-verified-or-validly-waive"
REVIEW = "P2.checklist-gate.current-independent-checklist-supervisor-review-complete"
FSCK = "P2.checklist-gate.checklistfsck-pass"
SUCC = "P2.exit-includes.controller-session-replacement"
CHECKLIST = {"items": ITEMS, "review": REVIEW, "fsck": FSCK}
MET_CLAUSE = "1 measured by the succession instrument (its claimant PROVEN besides)"
UNMET_CLAUSE = ("1 exit-includes condition(s) measured by the succession instrument and "
                "unmet -- succession capsule: ")


class _Blocked(BaseException):
    """Not an `OSError`: the tools under test swallow those and read on."""


@contextmanager
def _within(seconds: int):
    def stop(_signum, _frame):
        raise _Blocked("the gate blocked")
    old = signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# --------------------------------------------------------------------------- #
# wiring: phase_gates with the instruments' answers handed in
# --------------------------------------------------------------------------- #

def _item(item_id, phase="P2", kind="gate"):
    return {"id": item_id, "phase": phase, "kind": kind, "text": item_id, "capability": False}


def _scope(*items):
    return {"phases": [{"phase": p} for p in ("P1", "P2", "P3")], "items": list(items)}


def _req(rid, phase, item=None, also=()):
    return {"id": rid, "name": rid, "target_phase": phase, "authoritative_item": item,
            "also_covers": list(also), "core_required_1_0": True}


def _checklist(**changes):
    out = {kind: (True, f"{kind} is fine") for kind in CHECKLIST}
    out.update(changes)
    return lambda: out


def _gate(monkeypatch, items, *, requirements=(), rows=(), checklist=None, succession=None):
    monkeypatch.setattr(bp, "_scope", lambda: _scope(*items))
    capabilities = [{"id": rid, "target_phase": "P2", "status": status,
                     "core_required_1_0": True} for rid, status in rows]
    gates = bp.phase_gates(capabilities, list(requirements),
                           checklist_measures=checklist or _checklist(),
                           succession_measures=succession or (lambda: (True, "VERIFIED")))[1]
    return gates["P2"]


FOUR = [_item(ITEMS), _item(REVIEW), _item(FSCK), _item(SUCC)]
CLAIM = [_req("P2-35", "P2", SUCC)]
PROVEN = [("P2-35", "PROVEN")]


def test_the_map_binds_the_controller_session_replacement_item_to_the_capsule():
    assert bp.CHECKLIST_GATE_ITEMS[SUCC] == "succession"
    assert {k for k, v in bp.CHECKLIST_GATE_ITEMS.items() if v != "succession"} == set(CHECKLIST.values())
    assert bp._GATE_LABELS["succession"] == "succession capsule"
    assert bp._CHECKLIST_KINDS == ("items", "review", "fsck")
    # The gate reads the file the tool writes: one spelling, pinned equal.
    assert bp.SUCCESSION_CAPSULE == str(su.CAPSULE.relative_to(su.HOH)) == CAPSULE_REL


def test_a_verified_capsule_beside_its_proven_claim_opens_and_is_counted(monkeypatch):
    """The positive control: red on the base, where the item is met by P2-35 alone."""
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN)
    assert met is True, why
    assert why == ("4 exit condition(s) met: 0 bound to a PROVEN requirement, "
                   "3 measured by the checklist instruments, " + MET_CLAUSE), why


def test_an_unmet_capsule_holds_the_gate_with_its_reason(monkeypatch):
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN,
                     succession=lambda: (False, "REASON-S"))
    assert met is False
    assert UNMET_CLAUSE + "REASON-S" in why, why
    assert "nothing here measures yet" not in why and "checklist-gate exit" not in why, why
    assert why.count("REASON-") == 1, why
    # Beside the three checklist kinds, in the order of the clauses.
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN,
                     checklist=_checklist(fsck=(False, "REASON-F")),
                     succession=lambda: (False, "REASON-S"))
    assert met is False
    assert "1 checklist-gate exit condition(s) measured by the checklist instruments and " \
           "unmet -- ChecklistFsck: REASON-F" in why, why
    assert why.index("REASON-F") < why.index("REASON-S"), why


def test_the_claim_must_be_proven_besides_the_capsule_and_never_stands_in(monkeypatch):
    """P2-35 PROVEN does not open the item while the capsule is DRIFTED; a VERIFIED
    capsule does not open it while P2-35 is not PROVEN."""
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN,
                     succession=lambda: (False, "REASON-S"))
    assert met is False and "REASON-S" in why, why
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=[("P2-35", "MISSING")])
    assert met is False and "P2-35 MISSING" in why and "succession" not in why, why
    met, why = _gate(monkeypatch, FOUR, requirements=[_req("P2-90", "P2", "P2.other", also=[SUCC])],
                     rows=[("P2-90", "PROVEN")], succession=lambda: (False, "REASON-S"))
    assert met is False and "REASON-S" in why, why
    # Unclaimed, the item is still measured, and the message does not claim a claimant.
    met, why = _gate(monkeypatch, FOUR)
    assert met is True and why.endswith(", 1 measured by the succession instrument"), why


@pytest.mark.parametrize("answer", [
    (1, "ok"), ("True", "ok"), ("yes", "ok"), (None, "ok"), (True, 5), (True,),
    (True, "ok", "extra"), [True, "ok"], "ok", None, True,
], ids=repr)
def test_only_the_bool_true_with_a_text_reason_opens_the_succession_condition(monkeypatch, answer):
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN,
                     succession=lambda: answer)
    assert met is False, (answer, why)
    assert UNMET_CLAUSE in why and "no usable answer" in why, why


def test_a_succession_instrument_that_raises_holds_only_its_own_condition(monkeypatch):
    def boom():
        raise RuntimeError("the verifier fell over")
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN, succession=boom)
    assert met is False and why.count("RuntimeError") == 1, why
    assert UNMET_CLAUSE + "the instrument failed (RuntimeError)" in why, why
    assert "checklist-gate exit" not in why, why


def test_the_succession_instrument_is_asked_only_for_its_item_and_exactly_once(monkeypatch):
    asked, asked_checklist = [], []

    def succession():
        asked.append("asked")
        return (True, "VERIFIED")

    def checklist():
        asked_checklist.append("asked")
        return _checklist()()
    _gate(monkeypatch, FOUR[:3], checklist=checklist, succession=succession)
    assert asked == [] and asked_checklist == ["asked"]
    _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN, checklist=checklist,
          succession=succession)
    assert asked == ["asked"] and asked_checklist == ["asked"] * 2
    met, why = _gate(monkeypatch, [_item(SUCC)], requirements=CLAIM, rows=PROVEN,
                     checklist=checklist, succession=succession)
    assert met is True and asked == ["asked"] * 2 and asked_checklist == ["asked"] * 2, why
    assert why == "1 exit condition(s) met: 0 bound to a PROVEN requirement, 0 measured by " \
                  "the checklist instruments, " + MET_CLAUSE, why


def test_the_succession_reason_is_one_bounded_line(monkeypatch):
    met, why = _gate(monkeypatch, FOUR, requirements=CLAIM, rows=PROVEN,
                     succession=lambda: (False, "y" * 5000 + "\nsecond line"))
    assert met is False
    assert ("succession capsule: " + "y" * 237 + "...") in why, why
    assert "y" * 238 not in why and "second line" not in why and "\n" not in why


def test_the_checklist_measures_contract_is_unchanged():
    """`checklist_gate_measures` answers the three checklist kinds and nothing else;
    the capsule has an instrument of its own, so a provider of the three is read as
    before and a fourth key in it is ignored."""
    out = bp._gate_measurements(lambda: {**_checklist()(), "succession": (True, "x")})
    assert set(out) == set(CHECKLIST) and all(v[0] is True for v in out.values()), out


def test_the_command_line_prints_and_records_the_capsule_reason(tmp_path, monkeypatch, capsys):
    register = tmp_path / "program/v3_3/REQUIREMENTS.json"
    register.parent.mkdir(parents=True)
    register.write_text(json.dumps({"requirements": CLAIM}), encoding="utf-8")
    monkeypatch.setattr(bp, "HOH", tmp_path)
    monkeypatch.setattr(bp, "_scope", lambda: _scope(*FOUR))

    class _Missing(Exception):
        pass

    class _Matrix:
        RegisterMissing = _Missing

        @staticmethod
        def measure():
            return {"capabilities": [{"id": "P2-35", "target_phase": "P2",
                                      "status": "PROVEN", "core_required_1_0": True}]}
    monkeypatch.setattr(bp, "_matrix", lambda: _Matrix)
    monkeypatch.setattr(bp, "checklist_gate_measures", _checklist())
    monkeypatch.setattr(bp, "succession_measure", lambda: (False, "REASON-S"))
    dag = tmp_path / "dag.json"
    assert bp.main(["--dag", str(dag)]) == 0
    out = capsys.readouterr().out
    assert "gate P2: " in out and "succession capsule: REASON-S" in out, out
    entry = json.loads(dag.read_text(encoding="utf-8"))["phase_gates"]["P2"]
    assert entry["external_met"] is False and "succession capsule: REASON-S" in entry["external"]

    monkeypatch.setattr(bp, "succession_measure", lambda: (True, "VERIFIED"))
    assert bp.main(["--dag", str(dag)]) == 0
    assert "gate P2" not in capsys.readouterr().out
    entry = json.loads(dag.read_text(encoding="utf-8"))["phase_gates"]["P2"]
    assert entry["external_met"] is True and entry["external"].endswith(MET_CLAUSE)


# --------------------------------------------------------------------------- #
# the instrument over a constructed repository
# --------------------------------------------------------------------------- #

class Capsuled:
    """A scratch repository with every capsule source, a plan root in the
    environment, the gate and a fresh copy of the tool both pointed at it, and the
    capsule written at the sealing commit and committed on its own."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.seal = seed(root)
        self.su = _load("succession", "succession_for_capsuled")
        monkeypatch.setattr(self.su, "HOH", root)
        monkeypatch.setattr(self.su, "CAPSULE", root / CAPSULE_REL)
        monkeypatch.setattr(bp, "HOH", root)
        monkeypatch.setattr(bp, "_succession", lambda: self.su)
        monkeypatch.setenv(self.su.PLAN_ENV, str(root / "plan"))
        assert self.su.write_capsule(root / "plan", self.su.CAPSULE) == 0
        self.commit("capsule")

    def commit(self, message: str, *paths: str) -> str:
        for rel in paths:
            _write(self.root, rel, "x\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return _git(self.root, "rev-parse", "HEAD")

    def measure(self) -> tuple[bool, str]:
        with _within(10):
            return bp.succession_measure()


@pytest.fixture
def capsuled(tmp_path, monkeypatch):
    return Capsuled(tmp_path / "repo", monkeypatch)


def test_a_capsule_that_verifies_is_met_through_records_only_commits(capsuled):
    met, why = capsuled.measure()
    assert met is True, why
    assert why.startswith("VERIFIED: every field re-derives from this tree (internal_commit, "
                          "tree, board, sync, release, runs, open_findings, plan)"), why
    assert f"capsule for {capsuled.seal[:12]}" in why
    capsuled.commit("closure record", "DOGFOOD_HANDOFF.md", "dogfood/ATTRIBUTION.json")
    assert capsuled.measure() == (True, why)


def test_a_source_commit_after_the_capsule_is_unmet_and_names_the_field(capsuled):
    capsuled.commit("a source change", "src/hoh/b.py")
    met, why = capsuled.measure()
    assert met is False
    assert why.startswith("DRIFTED: internal_commit (HEAD has moved 2 commit(s) past "
                          f"{capsuled.seal[:12]}"), why
    assert "src/hoh/b.py is not a record path" in why, why


def test_a_dirty_tree_against_a_clean_capsule_is_unmet(capsuled):
    _write(capsuled.root, "src/hoh/a.py", "x = 2\n")
    met, why = capsuled.measure()
    assert met is False and why.startswith("DRIFTED: tree (") and "src/hoh/a.py" in why, why


def test_the_figures_regenerated_between_the_capsule_and_their_commit_keep_the_gate_met(capsuled):
    """The closure's X+1: `regenerate.py` writes the DAG, the checklist, the
    dispositions and then the board; between passes the tree is dirty in record
    paths only, and the gate must still read the capsule as met, else the figures
    it is about to commit would say "unmet"."""
    head = _git(capsuled.root, "rev-parse", "HEAD")
    _write(capsuled.root, "program/v3_3/VERIHARNESS_IMPLEMENTATION_DAG.json", "{}\n")
    _write(capsuled.root, "program/v3_3/VERIHARNESS_BUILD_CHECKLIST.json", "{}\n")
    _write(capsuled.root, "program/v3_3/PROGRAMME_DISPOSITIONS.md", "x\n")
    (capsuled.root / "docs/READINESS.md").write_text(_board(head[:7]), encoding="utf-8")
    met, why = capsuled.measure()
    assert met is True, why
    capsuled.commit("figures at X")
    assert capsuled.measure()[0] is True
    _write(capsuled.root, "dogfood/new/instrument.py", "x = 1\n")
    met, why = capsuled.measure()
    assert met is False and "dogfood/new/instrument.py" in why, why


@pytest.mark.parametrize("shape", ["HEAD", "HEAD~0", "branch", "short12", "UPPER40",
                                   "tag-object-id", "tree-id", "blob-id", "integer", "none", "list"])
def test_a_capsule_naming_a_moving_ref_never_meets_the_gate(capsuled, shape):
    """Both integration reviews' probe (B1/F1): a committed capsule whose
    `internal_commit` is `HEAD`, `HEAD~0`, the branch or an abbreviated id read
    VERIFIED after a source commit on the first candidate. Here it is unmet before
    the source commit (the capsule names no commit id) and after it; the
    unforged capsule is the control: met before, DRIFTED after."""
    sha = capsuled.seal
    _git(capsuled.root, "tag", "-a", "v-probe", "-m", "a tag object", sha)
    value = {"HEAD": "HEAD", "HEAD~0": "HEAD~0", "short12": sha[:12], "UPPER40": sha.upper(),
             "branch": _git(capsuled.root, "rev-parse", "--abbrev-ref", "HEAD"),
             "tag-object-id": _git(capsuled.root, "rev-parse", "v-probe"),
             "tree-id": _git(capsuled.root, "rev-parse", f"{sha}^{{tree}}"),
             "blob-id": _git(capsuled.root, "rev-parse", f"{sha}:README.md"),
             "integer": 12345678901234567890123456789012345678, "none": None, "list": [sha]}[shape]
    assert capsuled.measure()[0] is True
    path = capsuled.root / CAPSULE_REL
    original = path.read_bytes()
    capsule = json.loads(original)
    capsule["internal_commit"] = value
    path.write_text(json.dumps(capsule, indent=2) + "\n", encoding="utf-8")
    capsuled.commit("forged capsule")                         # a record path: the window stays open
    met, why = capsuled.measure()
    if shape == "tag-object-id":
        expected = f"DRIFTED: internal_commit ({value[:12]} is not a commit id: it peels to"
    elif shape in ("tree-id", "blob-id"):
        expected = "not checkable from here, which is not a pass: internal_commit ("
    elif shape == "none":
        expected = "DRIFTED: internal_commit (the capsule names no commit"
    else:
        expected = "DRIFTED: internal_commit (the capsule names "
    assert met is False and why.startswith(expected), (shape, why)
    if shape not in ("tag-object-id", "tree-id", "blob-id", "none"):
        assert "40-hex commit id is required" in why, (shape, why)
    capsuled.commit("a source change", "src/hoh/b.py")
    met, why = capsuled.measure()
    assert met is False and why.startswith(expected.split(" (")[0] + " ("), (shape, why)
    path.write_bytes(original)
    capsuled.commit("the real capsule again")
    met, why = capsuled.measure()
    assert met is False and "src/hoh/b.py is not a record path" in why, why


def test_the_gate_reads_the_plan_only_from_the_environment(capsuled, monkeypatch):
    """Review A, M-a: with `$VERIHARNESS_PLAN_ROOT` unset the tool walked the root the
    committed capsule recorded, and a capsule may name any directory whose files it
    digested itself. The gate's reading takes the plan from the environment or not
    at all; the command line's working-tree reading keeps the recorded root as a
    convenience (its control below)."""
    assert capsuled.measure()[0] is True
    monkeypatch.delenv(capsuled.su.PLAN_ENV)
    met, why = capsuled.measure()
    assert met is False, why
    assert why.startswith("not checkable from here, which is not a pass: plan (") and \
        capsuled.su.PLAN_ENV in why and "recorded in the capsule is not used" in why, why
    committed = json.loads(_git(capsuled.root, "show", f"HEAD:{CAPSULE_REL}"))
    working = {f: v for f, v, _ in capsuled.su.check(committed, None)}
    assert working["plan"] == capsuled.su.OK, "the command line still reads the recorded root"
    monkeypatch.setenv(capsuled.su.PLAN_ENV, str(capsuled.root / "plan"))
    assert capsuled.measure()[0] is True


def test_an_uncommitted_record_file_does_not_change_what_the_gate_reads(capsuled):
    """Review round 2 (N1): the board, the sync state, the release invariants and
    the ledger are read from the blobs at HEAD. A board regenerated and not yet
    committed is invisible to the gate (X+1 stays reachable); a board committed
    with other open rows drifts the capsule; restoring the old board in the
    working tree, uncommitted, does not undo that; the same for the sync state."""
    head = _git(capsuled.root, "rev-parse", "HEAD")
    board = capsuled.root / "docs/READINESS.md"
    original = board.read_bytes()
    board.write_text(_board(head[:7], "tests, export_sync, lint"), encoding="utf-8")
    met, why = capsuled.measure()
    assert met is True, why                                  # uncommitted: HEAD's board is read
    capsuled.commit("board with another open row")
    met, why = capsuled.measure()
    assert met is False and why.startswith("DRIFTED: board ("), why
    board.write_bytes(original)                              # the attack: restore it, uncommitted
    met, why = capsuled.measure()
    assert met is False and why.startswith("DRIFTED: board ("), why
    committed = json.loads(_git(capsuled.root, "show", f"HEAD:{CAPSULE_REL}"))
    working = {f: v for f, v, _ in capsuled.su.check(committed, None)}
    assert working["board"] == capsuled.su.OK, \
        "the working-tree reading says OK here: that is the hole, closed for the gate"
    capsuled.commit("board restored, committed")
    assert capsuled.measure()[0] is True

    sync = capsuled.root / "dogfood/export-sync/EXPORT_SYNC_STATE.json"
    original = sync.read_bytes()
    sync.write_text(json.dumps({"synced_export_commit": "f" * 40, "path_digests": {}}), encoding="utf-8")
    assert capsuled.measure()[0] is True
    capsuled.commit("export sync moved")
    met, why = capsuled.measure()
    assert met is False and why.startswith("DRIFTED: sync ("), why
    sync.write_bytes(original)
    met, why = capsuled.measure()
    assert met is False and why.startswith("DRIFTED: sync ("), why


def test_the_gate_reads_the_capsule_committed_at_head_not_the_working_tree(capsuled):
    """A gate is a statement about a commit. An edited working-tree capsule is drift
    against the committed one even when it would verify on its own; a capsule that
    HEAD does not carry does not meet the gate."""
    path = capsuled.root / CAPSULE_REL
    original = path.read_bytes()
    edited = json.loads(original)
    edited["written_at_utc"] = "2099-01-01T00:00:00Z"
    path.write_text(json.dumps(edited, indent=2) + "\n", encoding="utf-8")
    met, why = capsuled.measure()
    assert met is False and "differs from the one committed at HEAD" in why, why
    path.write_bytes(original)
    assert capsuled.measure()[0] is True
    _git(capsuled.root, "rm", "-q", "--cached", CAPSULE_REL)
    _git(capsuled.root, "commit", "-qm", "a commit without the capsule")
    met, why = capsuled.measure()
    assert met is False and "not committed at HEAD" in why, why


def test_a_capsule_recorded_on_a_dirty_tree_does_not_meet_the_gate(capsuled):
    """Written dirty, a capsule verifies only while the tree stays dirty the same
    way; that is not a state a phase closes in, so the gate requires the recorded
    cleanness even where `verify` would read the row as OK."""
    path = capsuled.root / CAPSULE_REL
    capsule = json.loads(path.read_text())
    capsule["internal_tree_clean"] = False
    path.write_text(json.dumps(capsule, indent=2) + "\n", encoding="utf-8")
    capsuled.commit("a capsule claiming a dirty tree")
    _write(capsuled.root, "src/hoh/a.py", "x = 2\n")   # dirty the same way: verify says OK
    rows = {f: v for f, v, _ in capsuled.su.check(capsule, None)}
    assert rows["tree"] == capsuled.su.OK
    met, why = capsuled.measure()
    assert met is False and "records a tree that was not clean" in why, why


def test_checklist_gate_measures_answers_exactly_three_kinds_without_a_register(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "HOH", tmp_path)
    out = bp.checklist_gate_measures()
    assert set(out) == set(bp._CHECKLIST_KINDS) == set(CHECKLIST), out
    assert all(v[0] is False and "missing" in v[1] for v in out.values()), out


def test_a_field_that_cannot_be_checked_is_unmet_and_not_a_pass(capsuled, monkeypatch):
    aside = capsuled.root.parent / "runs.aside"
    (capsuled.root / "runs").rename(aside)
    met, why = capsuled.measure()
    assert met is False
    assert why.startswith("not checkable from here, which is not a pass: runs ("), why
    aside.rename(capsuled.root / "runs")
    assert capsuled.measure()[0] is True
    # The plan: the environment names the root to read (it overrides the one the
    # capsule recorded); unreachable, the row is a gap, named with the variable to set.
    monkeypatch.setenv(capsuled.su.PLAN_ENV, str(capsuled.root.parent / "no-plan-here"))
    met, why = capsuled.measure()
    assert met is False and "plan (" in why and capsuled.su.PLAN_ENV in why, why


def _shape_missing(path: Path, outside: Path) -> None:
    path.rename(path.with_suffix(".moved"))


def _shape_directory(path: Path, outside: Path) -> None:
    path.rename(path.with_suffix(".moved"))
    path.mkdir()


def _shape_fifo(path: Path, outside: Path) -> None:
    path.rename(path.with_suffix(".moved"))
    os.mkfifo(path)


def _shape_dangling(path: Path, outside: Path) -> None:
    path.rename(path.with_suffix(".moved"))
    path.symlink_to(path.parent / "nowhere.json")


def _shape_outside(path: Path, outside: Path) -> None:
    outside.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, outside / "SUCCESSION.json")
    path.rename(path.with_suffix(".moved"))
    path.symlink_to(outside / "SUCCESSION.json")


def _shape_unreadable(path: Path, outside: Path) -> None:
    path.chmod(0)
    if os.access(path, os.R_OK):
        path.chmod(0o644)
        pytest.skip("a mode-000 file stays readable for this user")


def _text(content: bytes):
    def shape(path: Path, outside: Path) -> None:
        path.write_bytes(content)
    return shape


SHAPES = {
    "missing": _shape_missing, "directory": _shape_directory, "fifo": _shape_fifo,
    "dangling": _shape_dangling, "outside": _shape_outside, "unreadable": _shape_unreadable,
    "corrupt": _text(b"{"), "list": _text(b"[]"), "scalar": _text(b"1"), "empty": _text(b""),
    "utf16": _text("{}".encode("utf-16")), "latin1": _text(b'{"what": "\xe4"}'),
    "nul": _text(b'{"what": "a\x00b"}'), "deep": _text(b"[" * 200000 + b"]" * 200000),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_capsule_the_gate_cannot_read_is_unmet_never_a_traceback(capsuled, tmp_path, shape):
    SHAPES[shape](capsuled.root / CAPSULE_REL, tmp_path / "outside")
    met, why = capsuled.measure()
    assert met is False, (shape, why)
    assert "the succession capsule" in why, (shape, why)
    assert "Traceback" not in why


def test_an_oversize_capsule_is_refused_at_the_ceiling(capsuled, monkeypatch):
    size = (capsuled.root / CAPSULE_REL).stat().st_size
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", size)
    assert capsuled.measure()[0] is True
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", size - 1)
    met, why = capsuled.measure()
    assert met is False and "larger than" in why, why


def test_a_tool_that_judges_another_tree_or_has_no_tree_holds_the_gate(capsuled, tmp_path, monkeypatch):
    monkeypatch.setattr(capsuled.su, "HOH", tmp_path / "elsewhere")
    met, why = capsuled.measure()
    assert met is False and "judges another tree" in why, why
    monkeypatch.delattr(capsuled.su, "HOH")
    met, why = capsuled.measure()
    assert met is False and "judges another tree" in why, why


def test_a_tree_that_is_not_a_repository_of_its_own_is_unmet(capsuled, monkeypatch):
    """An archive copy under another repository: the tool's own gap, read as unmet."""
    inner = capsuled.root / "inner"
    shutil.copytree(capsuled.root, inner, ignore=shutil.ignore_patterns(".git", "inner"))
    monkeypatch.setattr(bp, "HOH", inner)
    monkeypatch.setattr(capsuled.su, "HOH", inner)
    monkeypatch.setattr(capsuled.su, "CAPSULE", inner / CAPSULE_REL)
    met, why = capsuled.measure()
    assert met is False and "not a git repository of its own" in why, why
    assert why.startswith("not checkable from here"), why


@pytest.mark.parametrize("rows", [
    [], None, "VERIFIED", [("a", "WEIRD", "b")], [("a", "VERIFIED")], [("a", "VERIFIED", 3)],
    [("a", "VERIFIED", "b"), ("c", "DRIFTED")], {"a": ("VERIFIED", "b")},
], ids=repr)
def test_a_verifier_that_answers_in_another_shape_is_unmet(capsuled, monkeypatch, rows):
    monkeypatch.setattr(capsuled.su, "check", lambda capsule, plan_root, **kw: rows)
    assert capsuled.measure() == (False, "the succession verifier returned no usable answer")


def test_a_verifier_that_raises_is_unmet_with_the_type(capsuled, monkeypatch):
    def boom(capsule, plan_root, **kw):
        raise KeyError("internal_commit")
    monkeypatch.setattr(capsuled.su, "check", boom)
    met, why = capsuled.measure()
    assert met is False and why.startswith("the succession capsule could not be verified (KeyError)"), why


def test_a_drifted_capsule_names_every_drifted_field_in_the_tools_order(capsuled):
    (capsuled.root / "docs/READINESS.md").write_text(_board("abc1234", "tests"), encoding="utf-8")
    capsuled.commit("board says something else and a source moved", "tools/u.py")
    met, why = capsuled.measure()
    assert met is False and why.startswith("DRIFTED: internal_commit ("), why
    assert why.index("internal_commit (") < why.index("board ("), why


# --------------------------------------------------------------------------- #
# parity over the real tree
# --------------------------------------------------------------------------- #

def test_the_gate_agrees_with_the_tools_exit_code_whatever_the_trees_state():
    """Time-invariant: red tomorrow only if the gate and `verify` disagree. Here and
    in a published clone alike (there the run tree and the plan are gaps: exit 3,
    unmet)."""
    needs_evidence(PROGRAM_REGISTER)
    done = subprocess.run([sys.executable, "-B", str(ROOT / "tools/succession.py"), "verify"],
                          capture_output=True, text=True, cwd=ROOT, timeout=300)
    met, why = bp.succession_measure()
    assert why and "Traceback" not in why, why
    # Parity with the tool's own `check` evaluated at HEAD (the gate's reading), and
    # never laxer than the command line's working-tree reading except where the two
    # readings differ by design (an uncommitted record file).
    rc, blob = su._git_rc("show", f"HEAD:{CAPSULE_REL}")
    if rc != 0:
        assert met is False and "not committed at HEAD" in why, why
        return
    committed = json.loads(blob)
    rows = su.check(committed, su._plan_root(None), at_commit="HEAD")
    expected = (all(v == su.OK for _, v, _ in rows) and committed.get("internal_tree_clean") is True
                and (ROOT / CAPSULE_REL).read_bytes() == blob.encode("utf-8", "surrogateescape"))
    assert met is expected, (met, why, rows)
    if met:
        assert why.startswith("VERIFIED: "), why
        dirty_records = [p for p in su._git("status", "--porcelain", "--no-renames",
                                            "--untracked-files=all").splitlines()
                         if su._is_record(p[3:])]
        if not dirty_records:
            assert done.returncode == 0, (done.returncode, done.stdout[-400:])
