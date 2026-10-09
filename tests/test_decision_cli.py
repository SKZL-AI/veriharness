"""`hoh decision check`: the decision graph fsck, executed from the product's command surface.

V3.3 P2-21. `src/hoh/decisioncheck.py` existed and was tested (46 tests), and nothing in
the product used it, so the capability matrix capped the item at PARTIAL: no entry point
names the module in an import statement. The matrix measures *text*, not execution
(O222), so every test here but the last runs the command through `hoh.cli.main` and
checks what it did; the last one checks what the matrix sees.

The inputs are `DecisionMap` JSON documents written as literals below, not built with the
models: this file is also the description of the input format. Read-only by
specification: the command writes nothing except an explicit `--receipt`, a new file.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hoh import cli, decisioncheck
from hoh.decisioncheck import check, resolution_digest
from hoh.decisions import DecisionMap

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402

NOW = "2026-09-24T12:00:00+00:00"
EARLIER = "2026-09-24T11:00:00+00:00"
LATER = "2026-09-24T13:00:00+00:00"


def _node(decision_id: str, **over) -> dict:
    node = {"decision_id": decision_id, "title": f"title of {decision_id}",
            "question": f"what about {decision_id}?", "status": "OPEN",
            "blocked_by": [], "scope_refs": ["S1"]}
    node.update(over)
    return node


def _resolved(decision_id: str, **over) -> dict:
    answer, refs = "use the existing ledger", ["doc/primary-1"]
    record = {"decision_id": decision_id, "path": "RESEARCH", "answer": answer,
              "source_refs": refs, "resolution_digest": resolution_digest(answer, refs),
              "resolved_by": "planner", "resolved_at": NOW}
    return _node(decision_id, status="RESOLVED", resolution=record, **over)


def _claimed(decision_id: str, until: str, **over) -> dict:
    return _node(decision_id, status="CLAIMED", claimed_by="worker-1",
                 claim_expires_at=until, **over)


def _map(map_id: str, nodes: list[dict], *, fog=(), generation=1) -> dict:
    return {"map_id": map_id, "destination": f"what must be clear for {map_id}",
            "generation": generation, "nodes": list(nodes), "fog": list(fog),
            "program_ref": "P-1"}


def _m1(*, nodes=None, fog=None) -> dict:
    """R resolved, Q open and blocked by R, fog F1 clearing with R."""
    fog = [{"fog_id": "F1", "title": "fog F1", "note": "not yet a question",
            "clears_with": ["R"]}] if fog is None else fog
    return _map("M1", nodes or [_resolved("R"), _node("Q", blocked_by=["R"])],
                fog=fog, generation=2)


def _m2(*, map_id="M2", extra=()) -> dict:
    """One open question T in another scope."""
    return _map(map_id, [_node("T", scope_refs=["S2"]), *extra], generation=5)


def _put(tmp_path: Path, name: str, content) -> str:
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content),
                    encoding="utf-8")
    return str(path)


def _run(tmp_path: Path, capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    rc = cli.main(["--root", str(tmp_path / "runs"), "decision", "check", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


def _refused(result: tuple[int, str, str]) -> str:
    rc, out, err = result
    assert rc == 2, result
    assert out == "", "a refusal prints nothing on stdout"
    assert err.startswith("decision check refused: "), err
    return err


def _codes(body: dict) -> dict[str, list[str]]:
    return {"errors": [f["code"] for f in body["errors"]],
            "warnings": [f["code"] for f in body["warnings"]]}


# --- the verdict ------------------------------------------------------------------


def test_a_sound_graph_passes_and_prints_the_checkers_own_receipt(tmp_path, capsys):
    first, second = _put(tmp_path, "m1.json", _m1()), _put(tmp_path, "m2.json", _m2())
    rc, out, err = _run(tmp_path, capsys, first, second)
    assert (rc, err) == (0, "")
    body = json.loads(out)
    # The command adds nothing and drops nothing: its output is the module's receipt.
    expected = check([DecisionMap.model_validate(_m1()), DecisionMap.model_validate(_m2())],
                     validator_version=decisioncheck.VALIDATOR_VERSION)
    assert body == expected.model_dump(mode="json")
    assert out == json.dumps(expected.model_dump(mode="json"), indent=2) + "\n", \
        "stdout is exactly the indented dump and a newline"
    assert body["status"] == "PASS" and body["errors"] == [] and body["warnings"] == []
    assert body["checked_generation"] == 5, "the highest generation over the maps"
    assert body["decision_graph_digest"].startswith("sha256:")
    assert body["validator_version"] == decisioncheck.VALIDATOR_VERSION
    assert body["validator_version"].strip(), "a receipt must say which rules judged it"
    # The order of the files carries no meaning.
    _, swapped, _ = _run(tmp_path, capsys, second, first)
    assert json.loads(swapped) == body


def test_an_error_finding_fails_with_exit_1(tmp_path, capsys):
    """Q is blocked by R, which is resolved in M1 but still open in M2: only the graph sees it."""
    first = _put(tmp_path, "m1.json", _m1())
    second = _put(tmp_path, "m2.json", _m2(extra=[_node("R", scope_refs=["S2"])]))
    rc, out, err = _run(tmp_path, capsys, first, second)
    assert (rc, err) == (1, "")
    body = json.loads(out)
    assert body["status"] == "FAIL"
    assert _codes(body) == {"errors": ["cross-map-blocker-unresolved"], "warnings": []}
    assert body["errors"][0]["decision_ids"] == ["Q", "R"]
    # Control: the same two files without the open duplicate pass.
    clean = _put(tmp_path, "m2-clean.json", _m2(extra=[_resolved("R", scope_refs=["S2"])]))
    assert _run(tmp_path, capsys, first, clean)[0] == 0


def test_a_warning_alone_never_fails(tmp_path, capsys):
    """An expired lease left CLAIMED is recoverable: reported, not a failure."""
    stale = _put(tmp_path, "m1.json", _m1(nodes=[
        _resolved("R"), _claimed("Q", EARLIER, blocked_by=["R"])]))
    rc, out, err = _run(tmp_path, capsys, stale, "--now", NOW)
    assert (rc, err) == (0, "")
    body = json.loads(out)
    assert body["status"] == "PASS" and body["errors"] == []
    assert _codes(body)["warnings"] == ["lease-expired-treated-active"]
    # Control: the same warning next to an error is still reported, and the error decides.
    both = _put(tmp_path, "m2.json", _m2(extra=[_node("R", scope_refs=["S2"])]))
    rc, out, _ = _run(tmp_path, capsys, stale, both, "--now", NOW)
    assert rc == 1
    assert _codes(json.loads(out)) == {"errors": ["cross-map-blocker-unresolved"],
                                       "warnings": ["lease-expired-treated-active"]}


def test_the_clock_is_only_what_now_says(tmp_path, capsys):
    stale = _put(tmp_path, "m1.json", _m1(nodes=[
        _resolved("R"), _claimed("Q", EARLIER, blocked_by=["R"])]))
    # No --now: the expiry is not evaluated (the module's rule), the wall clock is not read.
    _, out, _ = _run(tmp_path, capsys, stale)
    assert json.loads(out)["warnings"] == []
    # An instant before the expiry: a live lease.
    _, out, _ = _run(tmp_path, capsys, stale, "--now", "2026-09-24T10:00:00Z")
    assert json.loads(out)["warnings"] == []
    # An instant after it.
    _, out, _ = _run(tmp_path, capsys, stale, "--now", LATER)
    assert _codes(json.loads(out))["warnings"] == ["lease-expired-treated-active"]


def test_the_options_reach_the_checker(tmp_path, capsys):
    """--settled-ref and --program-refs are the checker's own inputs, each with a finding only it can make."""
    shaky = _put(tmp_path, "m1.json", _m1(nodes=[
        _resolved("R"), _node("Q", blocked_by=["R"]), _node("U", status="UNDERMINED")]))
    rc, out, _ = _run(tmp_path, capsys, shaky, "--settled-ref", "U")
    assert rc == 1
    assert _codes(json.loads(out))["errors"] == ["undermined-shown-as-settled"]
    # Repeatable: the failing id comes first and a benign one last, so "the last one wins" fails.
    rc, out, _ = _run(tmp_path, capsys, shaky, "--settled-ref", "U", "--settled-ref", "R")
    assert (rc, _codes(json.loads(out))["errors"]) == (1, ["undermined-shown-as-settled"])
    # A repeated id is one premise, and a premise that stands is no finding.
    rc, out, _ = _run(tmp_path, capsys, shaky, "--settled-ref", "R", "--settled-ref", "R")
    assert (rc, json.loads(out)["errors"]) == (0, [])

    graduated = _put(tmp_path, "grad.json", [{"program_id": "P-1", "map_id": "M1",
                                              "graduated": True, "scope_refs": ["S1"]}])
    rc, out, _ = _run(tmp_path, capsys, shaky, "--program-refs", graduated)
    assert rc == 1
    assert "graduated-with-blockers" in _codes(json.loads(out))["errors"]
    premise = _put(tmp_path, "premise.json", [{"program_id": "P-1", "map_id": "M1",
                                               "settled_premises": ["Q"]}])
    rc, out, _ = _run(tmp_path, capsys, shaky, "--program-refs", premise)
    assert (rc, _codes(json.loads(out))["errors"]) == (1, ["unresolved-as-settled"])
    # Control: the same programme reference with nothing graduated and nothing settled.
    quiet = _put(tmp_path, "quiet.json", [{"program_id": "P-1", "map_id": "M1"}])
    assert _run(tmp_path, capsys, shaky, "--program-refs", quiet)[0] == 0


# --- refusals ---------------------------------------------------------------------


def test_an_input_that_is_not_a_decision_map_is_refused_not_failed(tmp_path, capsys):
    good = _put(tmp_path, "good.json", _m1())
    err = _refused(_run(tmp_path, capsys, str(tmp_path / "absent.json")))
    assert "absent.json" in err
    err = _refused(_run(tmp_path, capsys, good, _put(tmp_path, "bad.json", "{not json")))
    assert "bad.json" in err
    # A field the model does not have.
    _refused(_run(tmp_path, capsys, _put(tmp_path, "extra.json", _m1() | {"surprise": 1})))
    # Not UTF-8.
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00")
    _refused(_run(tmp_path, capsys, str(binary)))
    # A graph file where a map is expected is not a map.
    _refused(_run(tmp_path, capsys, _put(tmp_path, "list.json", [_m1()])))
    # Usage errors are argparse's own refusals: exit 2, nothing on stdout, a usage line on stderr
    # (not the "decision check refused:" line).
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["decision", "check"])
    out, err = capsys.readouterr()
    assert (stop.value.code, out) == (2, "") and "usage:" in err


def test_a_graph_the_models_cannot_construct_is_refused_with_the_reason(tmp_path, capsys):
    """decisions.py refuses these at construction (ENFORCED_AT_CONSTRUCTION): there is no map to check."""
    dangling = _m1(nodes=[_resolved("R"), _node("Q", blocked_by=["Z"])])
    err = _refused(_run(tmp_path, capsys, _put(tmp_path, "dangling.json", dangling)))
    assert "dangling.json" in err and "does not contain" in err
    resolved_bare = _m1(nodes=[_node("R", status="RESOLVED")])
    err = _refused(_run(tmp_path, capsys, _put(tmp_path, "bare.json", resolved_bare)))
    assert "without a ResolutionRecord" in err


def test_malformed_options_are_refused(tmp_path, capsys):
    first = _put(tmp_path, "m1.json", _m1())
    _refused(_run(tmp_path, capsys, first, "--now", "yesterday"))
    err = _refused(_run(tmp_path, capsys, first, "--now", "2026-09-24T12:00:00"))
    assert "timezone" in err, "an instant without a zone is not an instant"
    _refused(_run(tmp_path, capsys, first, "--program-refs", str(tmp_path / "absent.json")))
    _refused(_run(tmp_path, capsys, first, "--program-refs",
                  _put(tmp_path, "refs.json", {"program_id": "P-1"})))
    _refused(_run(tmp_path, capsys, first, "--program-refs",
                  _put(tmp_path, "refs2.json", [{"program_id": "P-1", "map_id": "M1", "x": 1}])))


def test_a_path_that_cannot_be_used_is_a_refusal_and_never_a_crash(tmp_path, capsys):
    """An uncaught exception exits 1, which reads as FAIL. `Path.expanduser` raises RuntimeError
    for an unknown ~user, and a NUL byte makes the open call raise ValueError."""
    good = _put(tmp_path, "m1.json", _m1())
    ghost = "~no_such_user_zz9/m.json"
    err = _refused(_run(tmp_path, capsys, ghost))
    assert "no_such_user_zz9" in err
    _refused(_run(tmp_path, capsys, good, "--program-refs", ghost))
    _refused(_run(tmp_path, capsys, good, "--receipt", "~no_such_user_zz9/r.json"))
    _refused(_run(tmp_path, capsys, str(tmp_path / "a\0b.json")))
    _refused(_run(tmp_path, capsys, good, "--receipt", str(tmp_path / "a\0b.json")))
    assert not list(tmp_path.glob("a*")), "nothing was written"


def test_a_checker_that_raises_is_a_refusal_and_never_a_fail(tmp_path, capsys, monkeypatch):
    """An uncaught exception exits 1, which would read as FAIL: a crash must not look like a finding."""
    def boom(*args, **kwargs):
        raise RuntimeError("the checker broke")

    monkeypatch.setattr(decisioncheck, "check", boom)
    err = _refused(_run(tmp_path, capsys, _put(tmp_path, "m1.json", _m1())))
    assert "the checker itself failed" in err and "the checker broke" in err


# --- the receipt and the read-only guarantee --------------------------------------


def test_receipt_is_a_new_file_holding_what_was_printed(tmp_path, capsys):
    first = _put(tmp_path, "m1.json", _m1())
    receipt = tmp_path / "receipt.json"
    rc, out, _ = _run(tmp_path, capsys, first, "--receipt", str(receipt))
    assert rc == 0
    assert receipt.read_text(encoding="utf-8") == out
    assert json.loads(out)["decision_graph_digest"] in receipt.read_text(encoding="utf-8")
    # A FAIL is the verdict most worth keeping: its receipt is written too.
    failing = _put(tmp_path, "m2.json", _m2(extra=[_node("R", scope_refs=["S2"])]))
    failed = tmp_path / "failed.json"
    rc, out, _ = _run(tmp_path, capsys, first, failing, "--receipt", str(failed))
    assert rc == 1 and failed.read_text(encoding="utf-8") == out
    assert json.loads(failed.read_text(encoding="utf-8"))["status"] == "FAIL"


def test_receipt_never_replaces_a_file_and_a_refusal_writes_none(tmp_path, capsys):
    first = _put(tmp_path, "m1.json", _m1())
    existing = tmp_path / "receipt.json"
    existing.write_text("an earlier receipt", encoding="utf-8")
    _refused(_run(tmp_path, capsys, first, "--receipt", str(existing)))
    assert existing.read_text(encoding="utf-8") == "an earlier receipt"
    # A dangling symlink is a name too.
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "nowhere.json")
    _refused(_run(tmp_path, capsys, first, "--receipt", str(link)))
    assert not (tmp_path / "nowhere.json").exists()
    # No directory is created for it.
    _refused(_run(tmp_path, capsys, first, "--receipt", str(tmp_path / "no" / "dir" / "r.json")))
    assert not (tmp_path / "no").exists()
    # An input that is refused leaves no receipt behind.
    wanted = tmp_path / "wanted.json"
    _refused(_run(tmp_path, capsys, str(tmp_path / "absent.json"), "--receipt", str(wanted)))
    assert not wanted.exists()


def test_the_command_writes_nothing_but_the_receipt(tmp_path, capsys, monkeypatch):
    first = _put(tmp_path, "m1.json", _m1())
    failing = _put(tmp_path, "m2.json", _m2(extra=[_node("R", scope_refs=["S2"])]))
    refs = _put(tmp_path, "refs.json", [{"program_id": "P-1", "map_id": "M1"}])
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    def snapshot() -> dict[str, bytes | None]:
        return {str(p.relative_to(tmp_path)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(tmp_path.rglob("*"))}

    before = snapshot()
    for argv in ((first,), (first, failing), (first, "--program-refs", refs, "--now", NOW),
                 (first, "--settled-ref", "Q"), (str(tmp_path / "absent.json"),),
                 (first, "--now", "yesterday")):
        _run(tmp_path, capsys, *argv)
    assert snapshot() == before
    assert not (tmp_path / "runs").exists(), "no run root and no project store is opened"
    # With a receipt, exactly one new entry appears: no temporary file is left beside it.
    _run(tmp_path, capsys, first, "--receipt", str(tmp_path / "r.json"))
    after = snapshot()
    assert set(after) - set(before) == {"r.json"}
    assert {k: v for k, v in after.items() if k != "r.json"} == before


# --- what the matrix sees ---------------------------------------------------------


def test_the_command_surface_names_the_module_the_matrix_measures():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_decision_cli", ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_for_decision_cli"] = cm
    spec.loader.exec_module(cm)

    graph = cm._import_graph()
    assert "src/hoh/cli.py" in cm._reachable_from_an_entry_point("decisioncheck", graph)

    needs_evidence(PROGRAM_REGISTER)
    register = json.loads((ROOT / PROGRAM_REGISTER.path).read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in register["requirements"]}
    # The status half is deliberately tied to the matrix, as in test_projectcontract_cli: the
    # point of this command is the PROVEN lift, so a change to the matrix's rule that moves
    # P2-21 must be seen here and decided, not absorbed. The reachability half above is the
    # part that stays true whatever the matrix does with statuses.
    row = cm.classify(rows["P2-21"], cm._symbols(), ("tests/test_decisioncheck.py", True), {}, graph)
    assert row["status"] == cm.PROVEN, (row["status"], row["evidence"])
