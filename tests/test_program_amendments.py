"""Operator amendments to the pinned production line (A1, 2026-09-30).

The completeness gate refuses a requirement the plan does not name. An
amendment is the one way the operator can add work to the programme, so every
case here asks whether an amendment could be used to smuggle work in, or to
make a later phase look finished early:

* an amendment is only read when its file, its authorising directive and its
  incident record match their pins;
* a file nobody pinned is a problem, not an unnoticed extra;
* an amendment item needs exactly one requirement, and a requirement that
  names an amendment item must name one that exists, in its phase;
* a pulled-forward requirement may start early but still holds its own
  phase's gate, and PROVEN early reads IMPLEMENTED EARLY, not done.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load("program_scope_amend_probe", "program_scope.py")

#: The programme files (program/v3_3/) are internal and not exported; in the public
#: export these tests have nothing to read (O238 review round 2, splan-a minor).
_needs_programme = pytest.mark.skipif(
    not (ROOT / "program/v3_3/REQUIREMENTS.json").is_file()
    or not (ROOT / "program/v3_3/amendments/AMENDMENTS.json").is_file(),
    reason="program/v3_3 is internal and not part of the public export")
bp = _load("build_plan_amend_probe", "build_plan.py")

AMENDMENT = """Header prose above the first phase is not an item.

# P1 — v0.2 Integrity

Build/verify:
- widget registry: the thing to build

# P2 — v0.3 Runtime

Build/verify:
- gadget lifecycle
"""
DIRECTIVE = "The operator authorises A7.\n"
RECORD = "What happened.\n"

SCOPE = {
    "plan_sha256": "a" * 64,
    "phases": [
        {"phase": "P0", "items": 1, "capabilities": 0, "phase_kind": "baseline"},
        {"phase": "P1", "items": 1, "capabilities": 1, "phase_kind": "capability"},
        {"phase": "P2", "items": 1, "capabilities": 0, "phase_kind": "non-capability"},
    ],
    "items": [
        {"id": "P0.deliver.baseline", "phase": "P0", "kind": "baseline_deliverable",
         "section": "Deliver:", "capability": False},
        {"id": "P1.build.alpha", "phase": "P1", "kind": "capability",
         "section": "Build/verify:", "capability": True},
        {"id": "P2.purpose.why", "phase": "P2", "kind": "rationale",
         "section": "Purpose:", "capability": False},
    ],
}
W_ITEM = "P1.a7.build-verify.widget-registry-the-thing-to-build"
G_ITEM = "P2.a7.build-verify.gadget-lifecycle"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write(directory: Path, *, amendment=AMENDMENT, directive=DIRECTIVE,
           record=RECORD, pins: dict | None = None, extra: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "A7.md").write_text(amendment)
    (directory / "A7_DIRECTIVE.txt").write_text(directive)
    (directory / "A7_RECORD.md").write_text(record)
    entry = {"id": "A7", "file": "A7.md", "sha256": _sha(AMENDMENT),
             "authority": {"file": "A7_DIRECTIVE.txt", "sha256": _sha(DIRECTIVE)},
             "record": {"file": "A7_RECORD.md", "sha256": _sha(RECORD)},
             "authorised_by": "operator", "authorised_on": "2026-09-30"}
    entry.update(pins or {})
    (directory / "AMENDMENTS.json").write_text(json.dumps({"amendments": [entry]}))
    for name, text in (extra or {}).items():
        (directory / name).write_text(text)
    return directory


def _register(*extra: dict) -> dict:
    return {"requirements": [
        {"id": "P1-01", "target_phase": "P1", "authoritative_item": "P1.build.alpha"},
        {"id": "P1-02", "target_phase": "P1", "authoritative_item": W_ITEM},
        {"id": "P2-01", "target_phase": "P2", "authoritative_item": G_ITEM},
        *extra,
    ]}


# --------------------------------------------------------------------------- #
# Parsing and pins
# --------------------------------------------------------------------------- #

def test_amendment_items_carry_the_phase_first_and_the_amendment_name(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    assert a["problems"] == []
    ids = [i["id"] for i in a["items"]]
    assert ids == [W_ITEM, G_ITEM]
    assert all(i["amendment"] == "A7" and i["kind"] == "capability" for i in a["items"])


def test_an_edited_amendment_is_not_read(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", amendment=AMENDMENT + "- sneaked in\n"))
    assert a["items"] == []
    assert any("unpinned edit" in p for p in a["problems"])


def test_an_edited_directive_voids_the_amendment(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", directive="The operator did not.\n"))
    assert a["items"] == []
    assert any("authority file" in p for p in a["problems"])


def test_an_amendment_without_a_pinned_authority_is_refused(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", pins={"authority": {}}))
    assert a["items"] == []
    assert any("authority is not pinned" in p for p in a["problems"])


def test_a_file_nobody_pinned_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", extra={"A8.md": AMENDMENT}))
    assert any("A8.md is not named" in p for p in a["problems"])


def test_amendment_files_without_an_index_are_a_problem(tmp_path):
    d = tmp_path / "am"
    d.mkdir()
    (d / "A7.md").write_text(AMENDMENT)
    a = ps.load_amendments(d)
    assert a["items"] == [] and a["problems"]


def test_no_amendments_directory_is_no_amendments(tmp_path):
    a = ps.load_amendments(tmp_path / "absent")
    assert a == {"amendments": [], "items": [], "problems": [], "pulled_forward": {}}


def test_a_pin_may_not_point_outside_the_directory(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", pins={"file": "../A7.md"}))
    assert a["items"] == []
    assert any("bare name" in p for p in a["problems"])


# --------------------------------------------------------------------------- #
# Mapping into the register
# --------------------------------------------------------------------------- #

def test_a_consistent_amendment_passes_both_gates(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    reg = _register()
    assert ps.amendment_problems(SCOPE, a, reg) == []
    assert ps.completeness(ps.merged(SCOPE, a), reg, "a" * 64) == []


def test_without_the_merge_an_amendment_requirement_is_still_invented_work(tmp_path):
    """The invented-work guard is unchanged: it is the merge of a *pinned*
    amendment that makes the item exist, nothing about the requirement."""
    reg = _register()
    problems = ps.completeness(SCOPE, reg, "a" * 64)
    assert any("P1-02" in p and "not an item of the plan" in p for p in problems)


def test_an_unmapped_amendment_capability_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    reg = _register()
    reg["requirements"] = [r for r in reg["requirements"] if r["id"] != "P2-01"]
    assert any(G_ITEM in p and "no requirement" in p
               for p in ps.amendment_problems(SCOPE, a, reg))


def test_an_amendment_item_claimed_twice_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    reg = _register({"id": "P2-02", "target_phase": "P2", "authoritative_item": G_ITEM})
    assert any("claimed by 2" in p for p in ps.amendment_problems(SCOPE, a, reg))


def test_a_requirement_naming_a_missing_amendment_item_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    reg = _register({"id": "P2-03", "target_phase": "P2",
                     "authoritative_item": "P2.a7.build-verify.never-written"})
    assert any("P2-03" in p and "no pinned amendment" in p
               for p in ps.amendment_problems(SCOPE, a, reg))


def test_a_requirement_in_the_wrong_phase_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    reg = _register()
    reg["requirements"][2]["target_phase"] = "P1"
    assert any("P2-01" in p and "amendment item is in P2" in p
               for p in ps.amendment_problems(SCOPE, a, reg))


def test_an_amendment_may_not_create_a_phase(tmp_path):
    text = AMENDMENT + "\n# P9 — invented\n\nBuild/verify:\n- new thing\n"
    d = _write(tmp_path / "am", amendment=text, pins={"sha256": _sha(text)})
    a = ps.load_amendments(d)
    reg = _register({"id": "P9-01", "target_phase": "P9",
                     "authoritative_item": "P9.a7.build-verify.new-thing"})
    assert any("not a phase of the plan" in p for p in ps.amendment_problems(SCOPE, a, reg))


PF_TEXT = "pull-forward: " + G_ITEM + "\n" + AMENDMENT


def _write_pf(directory: Path) -> Path:
    return _write(directory, amendment=PF_TEXT, pins={"sha256": _sha(PF_TEXT)})


def test_pulled_forward_must_name_a_pinned_amendment_and_say_why(tmp_path):
    a = ps.load_amendments(_write_pf(tmp_path / "am"))
    reg = _register()
    reg["requirements"][2]["pulled_forward"] = {"amendment": "A9", "why": "x"}
    assert any("pinned amendment" in p for p in ps.amendment_problems(SCOPE, a, reg))
    reg["requirements"][2]["pulled_forward"] = {"amendment": "A7", "why": " "}
    assert any("no reason" in p for p in ps.amendment_problems(SCOPE, a, reg))
    reg["requirements"][2]["pulled_forward"] = {"amendment": "A7", "why": "x"}
    assert ps.amendment_problems(SCOPE, a, reg) == []


def test_only_the_pinned_amendment_text_can_pull_an_item_forward(tmp_path):
    """O238 review (splan-a B1): the register alone could mark any plan item
    pulled_forward -- e.g. P9-01 -- and the build plan released it while
    P2..P8 were shut. Now the item must be listed in the amendment's own
    pinned text."""
    a = ps.load_amendments(_write(tmp_path / "am"))           # no pull-forward lines
    reg = _register()
    reg["requirements"][2]["pulled_forward"] = {"amendment": "A7", "why": "x"}
    assert any("does not pull forward" in p for p in ps.amendment_problems(SCOPE, a, reg))
    a = ps.load_amendments(_write_pf(tmp_path / "am2"))
    reg = _register()
    reg["requirements"][0]["pulled_forward"] = {"amendment": "A7", "why": "x"}   # a plan item
    assert any("P1-01" in p and "does not pull forward" in p
               for p in ps.amendment_problems(SCOPE, a, reg))


def test_a_listed_pull_forward_needs_its_requirement_to_say_so(tmp_path):
    a = ps.load_amendments(_write_pf(tmp_path / "am"))
    assert any("carries no pulled_forward" in p
               for p in ps.amendment_problems(SCOPE, a, _register()))


def test_a_pull_forward_line_must_name_an_item_of_the_amendment(tmp_path):
    text = "pull-forward: P9.a7.build-verify.nothing\n" + AMENDMENT
    a = ps.load_amendments(_write(tmp_path / "am", amendment=text, pins={"sha256": _sha(text)}))
    assert any("not an item of this amendment" in p for p in a["problems"])


def test_an_amendment_cannot_be_its_own_authority(tmp_path):
    """O238 review (splan-a B3): authority.file == file was accepted."""
    a = ps.load_amendments(_write(tmp_path / "am", pins={
        "authority": {"file": "A7.md", "sha256": _sha(AMENDMENT)}}))
    assert a["items"] == []
    assert any("cannot authorise itself" in p for p in a["problems"])


def test_an_empty_directive_authorises_nothing(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", directive="  \n",
                                  pins={"authority": {"file": "A7_DIRECTIVE.txt",
                                                      "sha256": _sha("  \n")}}))
    assert a["items"] == [] and any("is empty" in p for p in a["problems"])


def test_one_directive_cannot_authorise_two_amendments(tmp_path):
    d = _write(tmp_path / "am")
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    (d / "A8.md").write_text(AMENDMENT.replace("widget", "sprocket").replace("gadget", "cog"))
    second = dict(idx["amendments"][0], id="A8", file="A8.md",
                  sha256=_sha((d / "A8.md").read_text()), record={})
    idx["amendments"].append(second)
    (d / "AMENDMENTS.json").write_text(json.dumps(idx))
    a = ps.load_amendments(d)
    assert any("already belongs to A7" in p for p in a["problems"])
    assert [x["id"] for x in a["amendments"]] == ["A7"]


def test_a_record_pinned_without_a_file_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", pins={"record": {"sha256": "b" * 64}}))
    assert any("record is not pinned" in p for p in a["problems"])


def test_a_malformed_index_entry_is_a_problem_not_a_crash(tmp_path):
    d = _write(tmp_path / "am")
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    idx["amendments"].append("A9")
    (d / "AMENDMENTS.json").write_text(json.dumps(idx))
    assert any("is not an object" in p for p in ps.load_amendments(d)["problems"])


def test_a_subdirectory_in_the_amendments_directory_is_a_problem(tmp_path):
    d = _write(tmp_path / "am")
    (d / "drafts").mkdir()
    (d / "drafts" / "A9.md").write_text(AMENDMENT)
    assert any("drafts is not named" in p and "subdirectory" in p
               for p in ps.load_amendments(d)["problems"])


def test_a_duplicate_amendment_id_is_a_problem(tmp_path):
    d = _write(tmp_path / "am")
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    idx["amendments"].append(dict(idx["amendments"][0]))
    (d / "AMENDMENTS.json").write_text(json.dumps(idx))
    assert any("listed twice" in p for p in ps.load_amendments(d)["problems"])


def test_an_amendment_without_authorised_by_is_refused(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am", pins={"authorised_by": " "}))
    assert a["items"] == [] and any("authorised_by" in p for p in a["problems"])


def test_the_index_is_append_only_against_its_history(tmp_path):
    """O238 review (splan-a B3): "never edited in place" was a sentence, not a
    check. A committed entry that is changed or removed is now a problem."""
    d = _write(tmp_path / "am")
    committed = json.loads((d / "AMENDMENTS.json").read_text())
    assert ps.load_amendments(d, history=[committed])["problems"] == []
    changed = json.loads(json.dumps(committed))
    changed["amendments"][0]["authorised_on"] = "2026-01-01"
    assert any("append-only" in p
               for p in ps.load_amendments(d, history=[changed])["problems"])
    assert any("append-only" in p
               for p in ps.load_amendments(d, history=[{"amendments": [
                   dict(committed["amendments"][0], id="A6")]}])["problems"])


def test_an_amendment_item_colliding_with_a_plan_item_is_a_problem(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    scope = json.loads(json.dumps(SCOPE))
    scope["items"].append({"id": W_ITEM, "phase": "P1", "kind": "capability",
                           "section": "Build/verify:", "capability": True})
    assert any("collides with an item of the plan" in p
               for p in ps.amendment_problems(scope, a, _register()))


def test_a_broken_amendments_document_is_not_merged(tmp_path):
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(SCOPE))
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"amendments": [], "items": [], "problems": ["x"]}))
    with pytest.raises(ValueError):
        ps.load_merged_scope(scope_file, doc)


def test_check_reports_a_broken_amendment_without_the_plan(tmp_path, monkeypatch):
    """O238 review (splan-b B1): `--check` returned 3 ("plan not reachable")
    before looking at the amendments, so the board row read NOT_RUN while an
    amendment was broken."""
    d = _write(tmp_path / "am", amendment=AMENDMENT + "- sneaked in\n")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(_register()))
    monkeypatch.setattr(ps, "AMENDMENTS_DIR", d)
    monkeypatch.setattr(ps, "REGISTER", reg)
    monkeypatch.setattr(ps, "SCOPE", tmp_path / "absent.json")
    monkeypatch.delenv("VERIHARNESS_PLAN_ROOT", raising=False)
    assert ps.main(["--check"]) == 1


def test_merge_recounts_the_phases_an_amendment_adds_capabilities_to(tmp_path):
    a = ps.load_amendments(_write(tmp_path / "am"))
    m = ps.merged(SCOPE, a)
    p2 = next(p for p in m["phases"] if p["phase"] == "P2")
    assert p2["capabilities"] == 1 and p2["phase_kind"] == "capability"
    assert m["plan_sha256"] == SCOPE["plan_sha256"], "the plan's pin is not replaced"
    assert [x["id"] for x in m["amendments"]] == ["A7"]
    assert len(SCOPE["items"]) == 3, "the plan's inventory is not mutated"


# --------------------------------------------------------------------------- #
# Pull-forward in the build plan
# --------------------------------------------------------------------------- #

PF = {"amendment": "A7", "why": "the incident needs it now"}


def _body(pulled: bool) -> dict:
    later = {"id": "P2-09", "name": "later", "target_phase": "P2", "status": "MISSING",
             "core_required_1_0": True, "depends_on": []}
    if pulled:
        later["pulled_forward"] = PF
    return {"capabilities": [
        {"id": "P1-09", "name": "open", "target_phase": "P1", "status": "MISSING",
         "core_required_1_0": True, "depends_on": []},
        later]}


UNMET = {"P1": (False, "an exit condition nothing here measures yet")}


def test_a_later_phase_requirement_waits_for_the_gate_in_front_of_it():
    plan = bp.build(_body(pulled=False), ["P1", "P2"], {})
    assert plan["ready_now"] == ["P1-09"]
    held = bp.build(_body(pulled=False), ["P1", "P2"], UNMET)
    assert [i["id"] for i in held["held_items"]] == ["P2-09"]


def test_a_pulled_forward_requirement_may_start_before_the_gate_opens():
    plan = bp.build(_body(pulled=True), ["P1", "P2"], {})
    assert sorted(plan["ready_now"]) == ["P1-09", "P2-09"]
    held = bp.build(_body(pulled=True), ["P1", "P2"], UNMET)
    assert held["held_items"] == [], "not even an unmeasured gate holds it"
    item = next(i for w in plan["waves"] for i in w["items"] if i["id"] == "P2-09")
    assert item["pulled_forward"] == PF


def test_a_pulled_forward_requirement_still_holds_its_own_phase_gate():
    """Pulling forward is about *when it may start*. Its phase still does not
    close until it is satisfied."""
    plan = bp.build(_body(pulled=True), ["P1", "P2"], {})
    assert "P2-09" in plan["phase_gates"]["P2"]["blocking_open"]
    assert "P2" not in plan["open_gates"]


def test_a_proven_requirement_reads_implemented_early_while_its_gate_is_shut():
    """O238 review (splan-a B2): only pulled-forward rows were labelled; P5-17,
    PROVEN with only P0/P1 open, read "done"."""
    label = bp.start_label("P5-17", "PROVEN", "P4", ["P0", "P1"], set(), set(), set())
    assert label.startswith("IMPLEMENTED EARLY")
    assert bp.start_label("P5-17", "PROVEN", "P4", ["P0", "P1", "P2", "P3", "P4"],
                          set(), set(), set()) == "done"
    assert bp.start_label("P1-03", "PROVEN", "P0", ["P0"], set(), set(), set()) == "done"


def test_proven_early_is_not_the_phase_being_satisfied():
    body = _body(pulled=True)
    body["capabilities"][1]["status"] = "PROVEN"
    plan = bp.build(body, ["P1", "P2"], {})
    assert "P1" not in plan["open_gates"], "P1-09 is open, so P1's gate is shut"
    assert "P2" not in plan["open_gates"], (
        "every P2 requirement is PROVEN, but P2 cannot count as satisfied while "
        "the gate in front of it is shut")
    body["capabilities"][0]["status"] = "PROVEN"
    plan = bp.build(body, ["P1", "P2"], {})
    assert plan["open_gates"] == ["P1", "P2"]


# --------------------------------------------------------------------------- #
# This repository
# --------------------------------------------------------------------------- #

@_needs_programme
def test_this_repositorys_amendments_are_pinned_and_mapped():
    a = ps.load_amendments()
    assert a["problems"] == []
    # O252: A5 (provider / model routing) registered; the exact set grows by one
    # and must equal the authoritative pin index, entry for entry.
    # O262: A6 (the agent-session orchestration surface) registered; the set grows by one again.
    # O265: A7 (a phase is closed at a commit) registered; the set grows by one again.
    assert [x["id"] for x in a["amendments"]] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    pinned = json.loads((ROOT / "program/v3_3/amendments/AMENDMENTS.json").read_text())
    assert [x["id"] for x in pinned["amendments"]] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    assert a["pulled_forward"]["A5"] == []
    # A7 states a rule about the closure of a phase and adds no capability, no pull-forward and no
    # exit condition: three constraint items under P2, which the build plan counts for no gate and
    # the register for no requirement (O265, A7_OVERLAP_RECORD.md section 2).
    a7 = [i for i in a["items"] if i["amendment"] == "A7"]
    assert len(a7) == 3 and {i["phase"] for i in a7} == {"P2"} and {i["kind"] for i in a7} == {"constraint"}
    assert not [i for i in a7 if i["kind"] in bp._GATE_KINDS or i["capability"]]
    assert a["pulled_forward"]["A7"] == []
    # A6 pulls its eight capabilities of P5 forward, adds no exit condition, and is claimed
    # by exactly the requirements P5-36 .. P5-43 (O262).
    a6 = [i for i in a["items"] if i["amendment"] == "A6"]
    assert len(a6) == 8 and {i["phase"] for i in a6} == {"P5"}
    assert not [i for i in a6 if i["kind"] in bp._GATE_KINDS]
    assert sorted(a["pulled_forward"]["A6"]) == sorted(i["id"] for i in a6)
    a6_reqs = sorted(r["id"] for r in json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())["requirements"]
                     if ".a6." in str(r.get("authoritative_item", "")))
    assert a6_reqs == [f"P5-{n}" for n in range(36, 44)]
    # A5 adds no exit condition either: one capability in each of P2, P3, P10,
    # P11 and P12, each claimed by exactly the requirement O252 registered.
    a5 = [i for i in a["items"] if i["amendment"] == "A5"]
    assert a5 and not [i for i in a5 if i["kind"] in bp._GATE_KINDS]
    a5_reqs = {str(r.get("authoritative_item")).split(".")[0]: r["id"]
               for r in json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())["requirements"]
               if ".a5." in str(r.get("authoritative_item", ""))}
    assert a5_reqs == {"P2": "P2-43", "P3": "P3-41", "P10": "P10-17", "P11": "P11-02", "P12": "P12-06"}
    # A2, A3 and A4 are non-blocking by their directives (2026-10-01 and
    # 2026-10-02): they pull nothing forward, so every pulled-forward item
    # still belongs to A1.
    assert a["pulled_forward"]["A2"] == [] and a["pulled_forward"]["A3"] == []
    assert a["pulled_forward"]["A4"] == []
    # A4 is non-blocking (2026-10-02): the build plan counts a phase's
    # invariants, validations and gates as its exit conditions, so A4 may add
    # none of them -- only the two optional-provider capabilities in P10.
    a4 = [i for i in a["items"] if i["amendment"] == "A4"]
    assert a4 and not [i for i in a4 if i["kind"] in bp._GATE_KINDS]
    a4_reqs = [r for r in json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())["requirements"]
               if str(r.get("authoritative_item", "")).startswith("P10.a4.")]
    assert sorted(r["id"] for r in a4_reqs) == ["P10-15", "P10-16"]
    assert all(r["optional_provider"] and not r["core_required_1_0"] for r in a4_reqs)
    register = json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())
    scope_file = ROOT / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"
    scope = json.loads(scope_file.read_text()) if scope_file.is_file() else None
    assert ps.amendment_problems(scope, a, register) == []


def test_the_committed_amendments_document_matches_a_fresh_parse():
    """PROGRAMME_AMENDMENTS.json is derived; a stale one would let the build
    plan read yesterday's amendment."""
    doc_file = ROOT / "program/v3_3/PROGRAMME_AMENDMENTS.json"
    if not doc_file.is_file():
        pytest.skip("no amendments document in this tree")
    doc = json.loads(doc_file.read_text())
    fresh = ps.load_amendments()
    assert doc["items"] == fresh["items"]
    assert doc["amendments"] == fresh["amendments"]
    assert doc.get("pulled_forward") == fresh["pulled_forward"]
    assert doc["problems"] == []


@_needs_programme
def test_every_directive_test_of_a1_is_carried_by_a_requirement():
    """O238 review (splan-b M1): the directive's section S lists S1..S23; each
    must be carried by at least one A1 requirement (`directive_tests`)."""
    import re as _re
    directive = (ROOT / "program/v3_3/amendments/A1_OPERATOR_DIRECTIVE_2026-09-30.txt").read_text()
    section = directive.split("S. TEST REQUIREMENTS", 1)[1].split("T. COMPLETION REPORT", 1)[0]
    numbered = [int(m) for m in _re.findall(r"^(\d+)\. ", section, _re.M)]
    assert numbered == list(range(1, 24))
    register = json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())
    carried = {t for r in register["requirements"] for t in r.get("directive_tests") or []}
    assert carried == {f"S{n}" for n in numbered}


@_needs_programme
def test_the_repositorys_pull_forward_set_is_pinned_and_complete():
    a = ps.load_amendments()
    register = json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())
    marked = sorted(r["authoritative_item"] for r in register["requirements"]
                    if r.get("pulled_forward"))
    # O262: A6 pulls eight further items forward; A1's ten are unchanged.
    assert marked == sorted(a["pulled_forward"]["A1"] + a["pulled_forward"]["A6"])
    assert len(a["pulled_forward"]["A1"]) == 10 and len(a["pulled_forward"]["A6"]) == 8
    assert len(marked) == 18


# --------------------------------------------------------------------------- #
# Round 2 (O238 review): history, self-authority by content, header-only lines
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    import subprocess
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_append_only_check_reads_real_git_history(tmp_path):
    """splan-a R1 / splan-b B1: the history lookup ran `git log` from the
    amendments directory with a top-relative pathspec and so never found a
    commit. Here the index lives two levels down in a real repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    d = _write(repo / "program" / "amendments")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    assert ps.load_amendments(d)["problems"] == []
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    idx["amendments"][0]["authorised_on"] = "2026-01-01"
    (d / "AMENDMENTS.json").write_text(json.dumps(idx))
    assert any("append-only" in p for p in ps.load_amendments(d)["problems"])
    _git(repo, "commit", "-q", "-am", "c2 edits the entry in place")
    assert any("append-only" in p for p in ps.load_amendments(d)["problems"]), \
        "committing the edit does not make it legitimate"


def test_removing_a_committed_index_is_a_problem(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    d = _write(repo / "am")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    (d / "AMENDMENTS.json").rename(tmp_path / "parked-index.json")
    assert any("now missing" in p for p in ps.load_amendments(d)["problems"])


def test_an_authority_with_the_amendments_own_content_is_refused(tmp_path):
    """splan-a round 2: a byte-identical copy under another name passed."""
    a = ps.load_amendments(_write(tmp_path / "am", directive=AMENDMENT,
                                  pins={"authority": {"file": "A7_DIRECTIVE.txt",
                                                      "sha256": _sha(AMENDMENT)}}))
    assert a["items"] == [] and any("same content" in p for p in a["problems"])


def test_a_symlinked_authority_is_refused(tmp_path):
    d = _write(tmp_path / "am")
    (d / "A7_DIRECTIVE.txt").rename(tmp_path / "real-directive.txt")
    (d / "A7_DIRECTIVE.txt").symlink_to(tmp_path / "real-directive.txt")
    a = ps.load_amendments(d)
    assert a["items"] == [] and any("is a symlink" in p for p in a["problems"])


def test_a_pull_forward_line_below_a_phase_heading_does_not_count(tmp_path):
    """splan-b round 2 (surviving mutant): only the header above the first
    phase heading can pull an item forward."""
    text = AMENDMENT.replace("- gadget lifecycle\n", "- gadget lifecycle\npull-forward: " + G_ITEM + "\n")
    assert ps.pulled_forward_items(text) == []
    assert ps.pulled_forward_items("pull-forward: " + G_ITEM + "\n" + AMENDMENT) == [G_ITEM]


def test_regenerate_stops_at_the_first_failed_step(monkeypatch):
    """splan-a/b round 2 (surviving mutant): the `break` was untested."""
    rg = _load("regenerate_amend_probe", "regenerate.py")
    calls = []

    class Proc:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "boom"

    def fake_run(argv, **kw):
        calls.append(argv)
        return Proc(1 if len(calls) == 1 else 0)

    monkeypatch.setattr(rg.subprocess, "run", fake_run)
    failed = rg.run_once({})
    assert len(calls) == 1 and len(failed) == 1


# --------------------------------------------------------------------------- #
# Round 3 (O238 review): the directory moved away, symlinks, shared authority
# --------------------------------------------------------------------------- #

def _committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    d = _write(repo / "program" / "amendments")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    return repo, d


def test_renaming_the_amendments_directory_away_is_a_problem(tmp_path):
    """splan-b R3-1 / splan-a B1: parking the whole directory (the house-rule
    way to withdraw something) skipped the history lookup, because git needed
    the directory as its working directory."""
    repo, d = _committed(tmp_path)
    d.rename(d.parent / "amendments.v1.20261001T120000Z")
    problems = ps.load_amendments(d)["problems"]
    assert any("now missing" in p for p in problems)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c2 parks the directory")
    assert any("now missing" in p for p in ps.load_amendments(d)["problems"])


def test_a_symlinked_index_is_refused(tmp_path):
    repo, d = _committed(tmp_path)
    edited = tmp_path / "edited.json"
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    idx["amendments"][0]["authorised_on"] = "2026-01-01"
    edited.write_text(json.dumps(idx))
    (d / "AMENDMENTS.json").rename(tmp_path / "parked.json")
    (d / "AMENDMENTS.json").symlink_to(edited)
    a = ps.load_amendments(d)
    assert a["items"] == [] and any("is a symlink" in p for p in a["problems"])


def test_a_symlinked_amendments_directory_is_refused(tmp_path):
    repo, d = _committed(tmp_path)
    copy = tmp_path / "elsewhere"
    d.rename(copy)
    d.symlink_to(copy)
    a = ps.load_amendments(d)
    assert a["items"] == [] and any("is a symlink" in p for p in a["problems"])


def test_one_directive_text_cannot_authorise_two_amendments_under_two_names(tmp_path):
    """splan-b round 3: the content check compared digests only within one
    amendment; a byte copy of A7's directive under a new name authorised A8."""
    d = _write(tmp_path / "am")
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    (d / "A8.md").write_text(AMENDMENT.replace("widget", "sprocket").replace("gadget", "cog"))
    (d / "A8_DIRECTIVE.txt").write_text(DIRECTIVE)
    idx["amendments"].append(dict(idx["amendments"][0], id="A8", file="A8.md",
                                  sha256=_sha((d / "A8.md").read_text()),
                                  authority={"file": "A8_DIRECTIVE.txt", "sha256": _sha(DIRECTIVE)},
                                  record={}))
    (d / "AMENDMENTS.json").write_text(json.dumps(idx))
    a = ps.load_amendments(d)
    assert any("same content as a file of A7" in p for p in a["problems"])
    assert [x["id"] for x in a["amendments"]] == ["A7"]


def test_a_symlinked_ancestor_of_the_amendments_is_refused(tmp_path):
    """splan-b R4-1 / splan-a round 4: `program/v3_3` as a symlink to an edited
    copy outside the repository made the history lookup run outside the
    repository and return nothing."""
    repo, d = _committed(tmp_path)
    program = repo / "program"
    outside = tmp_path / "alt"
    program.rename(outside)
    program.symlink_to(outside)
    idx = json.loads((d / "AMENDMENTS.json").read_text())
    idx["amendments"][0]["authorised_on"] = "2026-01-01"
    (d / "AMENDMENTS.json").write_text(json.dumps(idx))
    a = ps.load_amendments(d)
    assert a["items"] == [] and any("through a symlink" in p for p in a["problems"])
