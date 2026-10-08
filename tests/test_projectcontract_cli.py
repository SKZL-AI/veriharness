"""`hoh project contract`: the PM boundary, executed from the product's command surface.

V3.3 P2-29, P2-30. `src/hoh/projectcontract.py` existed and was tested, and nothing in
the product used it, so the capability matrix capped both items at PARTIAL. The
matrix measures that an entry point *names* the module in an import statement -- text,
not execution (O222): a command that imports the module and then does nothing would
satisfy it. So every test here but the last runs a subcommand through `hoh.cli.main`
and checks what it did; the last one checks what the matrix sees.

Read-only by specification: the commands write no run root, no project store and no
file, and one test holds every byte under `tmp_path` to that.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from hoh import cli
from hoh.projectcontract import OWNERSHIP, Authority, ProjectContract

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402

NOT_EVALUATED = ["evidence_invalidated", "active_work_affected", "beyond_autonomy",
                 "release_affected"]


def _contract(**over) -> dict:
    raw = {"project_contract_id": "p1", "version": 1, "source_system": "some-pm",
           "source_revision": "r1", "objective": "ship the thing",
           "milestones": {"m2": "2026-11-01", "m1": "2026-10-01"},
           "work_packages": [
               {"work_package_id": "w1", "title": "first", "external_status": "DONE"},
               {"work_package_id": "w2", "title": "second", "depends_on": ["w1"],
                "milestone": "m1"}],
           "effective_at": "2026-10-01T00:00:00Z"}
    raw.update(over)
    return raw


def _put(tmp_path: Path, name: str, content) -> str:
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content),
                    encoding="utf-8")
    return str(path)


def _run(tmp_path: Path, capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    rc = cli.main(["--root", str(tmp_path / "runs"), "project", "contract", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


def _refused(result: tuple[int, str, str]) -> str:
    rc, out, err = result
    assert rc == 2, result
    assert out == "", "a refusal prints nothing on stdout"
    assert err.startswith("contract refused: "), err
    return err


# --- check ------------------------------------------------------------------------


def test_check_prints_the_digest_of_the_content(tmp_path, capsys):
    raw = _contract()
    path = _put(tmp_path, "c.json", raw)
    rc, out, _ = _run(tmp_path, capsys, "check", path)
    assert rc == 0
    summary = json.loads(out)
    module_digest = ProjectContract.model_validate_json(
        Path(path).read_text(encoding="utf-8")).digest()
    assert summary["digest"] == module_digest
    assert summary["project_contract_id"] == "p1" and summary["version"] == 1
    assert summary["work_packages"] == ["w1", "w2"]

    # When it arrived is provenance, not content.
    later = _put(tmp_path, "later.json", _contract(imported_at="2030-01-01T00:00:00Z"))
    rc, out, _ = _run(tmp_path, capsys, "check", later)
    assert rc == 0 and json.loads(out)["digest"] == module_digest

    # What it says is content.
    other = _put(tmp_path, "other.json", _contract(objective="ship another thing"))
    rc, out, _ = _run(tmp_path, capsys, "check", other)
    assert rc == 0 and json.loads(out)["digest"] != module_digest


def test_check_reports_milestones_and_source(tmp_path, capsys):
    path = _put(tmp_path, "c.json", _contract(source_system="jira", source_revision="r9"))
    rc, out, _ = _run(tmp_path, capsys, "check", path)
    assert rc == 0
    summary = json.loads(out)
    assert summary["milestones"] == ["m1", "m2"], "sorted names"
    assert summary["source_system"] == "jira"
    assert summary["source_revision"] == "r9"


def test_check_refuses_a_contract_with_a_dangling_dependency(tmp_path, capsys):
    raw = _contract(work_packages=[{"work_package_id": "w1", "title": "first",
                                    "depends_on": ["w9"]}])
    err = _refused(_run(tmp_path, capsys, "check", _put(tmp_path, "c.json", raw)))
    assert "does not contain" in err and "w9" in err


def test_check_refuses_a_contract_without_effective_at(tmp_path, capsys):
    """Its default is the moment of reading, so its digest would be a timestamp."""
    raw = _contract()
    del raw["effective_at"]
    err = _refused(_run(tmp_path, capsys, "check", _put(tmp_path, "c.json", raw)))
    assert "effective_at" in err


def test_check_refuses_a_file_that_is_not_json(tmp_path, capsys):
    _refused(_run(tmp_path, capsys, "check", _put(tmp_path, "c.json", "not json {")))


def test_check_refuses_a_field_the_contract_does_not_have(tmp_path, capsys):
    raw = _contract(colour="red")
    err = _refused(_run(tmp_path, capsys, "check", _put(tmp_path, "c.json", raw)))
    assert "colour" in err


def test_check_refuses_a_missing_file(tmp_path, capsys):
    _refused(_run(tmp_path, capsys, "check", str(tmp_path / "absent.json")))


def test_schema_is_the_models_own(tmp_path, capsys):
    rc, out, _ = _run(tmp_path, capsys, "schema")
    assert rc == 0
    assert json.loads(out) == ProjectContract.model_json_schema()


# --- impact -----------------------------------------------------------------------


def test_impact_describes_an_amendment_and_applies_nothing(tmp_path, capsys):
    previous = _put(tmp_path, "v1.json", _contract())
    proposed = _put(tmp_path, "v2.json", _contract(
        version=2, source_revision="r2",
        work_packages=[{"work_package_id": "w1", "title": "first", "external_status": "DONE"},
                       {"work_package_id": "w3", "title": "third"}]))
    before = {p: Path(p).read_bytes() for p in (previous, proposed)}
    rc, out, _ = _run(tmp_path, capsys, "impact", previous, proposed)
    assert rc == 0
    body = json.loads(out)
    assert body["previous"]["version"] == 1 and body["proposed"]["version"] == 2
    assert set(body["impact"]["scope_changes"]) == {"+w3", "-w2"}
    assert body["disturbs_anything"] is True
    # Only the two files were inputs: the fields that need more than that are named,
    # so their defaults do not read as "nothing".
    assert sorted(body["not_evaluated"]) == sorted(NOT_EVALUATED)
    assert {p: Path(p).read_bytes() for p in (previous, proposed)} == before


def test_impact_of_an_unchanged_plan_disturbs_nothing(tmp_path, capsys):
    path = _put(tmp_path, "v1.json", _contract())
    rc, out, _ = _run(tmp_path, capsys, "impact", path, path)
    assert rc == 0
    body = json.loads(out)
    assert body["disturbs_anything"] is False
    assert body["impact"]["scope_changes"] == []
    assert body["previous"]["digest"] == body["proposed"]["digest"]


def test_impact_refuses_two_different_contracts(tmp_path, capsys):
    previous = _put(tmp_path, "a.json", _contract())
    proposed = _put(tmp_path, "b.json", _contract(project_contract_id="p2", version=2))
    err = _refused(_run(tmp_path, capsys, "impact", previous, proposed))
    assert "two different contracts" in err


def test_impact_refuses_a_contract_without_effective_at(tmp_path, capsys):
    raw = _contract()
    del raw["effective_at"]
    good = _put(tmp_path, "good.json", _contract())
    bad = _put(tmp_path, "bad.json", raw)
    assert "effective_at" in _refused(_run(tmp_path, capsys, "impact", bad, good))
    assert "effective_at" in _refused(_run(tmp_path, capsys, "impact", good, bad))


def test_impact_refuses_a_version_that_does_not_go_forward(tmp_path, capsys):
    previous = _put(tmp_path, "v2.json", _contract(version=2))
    same = _put(tmp_path, "same.json", _contract(version=2, objective="changed"))
    back = _put(tmp_path, "back.json", _contract(version=1, objective="changed"))
    assert "goes forward" in _refused(_run(tmp_path, capsys, "impact", previous, same))
    assert "goes forward" in _refused(_run(tmp_path, capsys, "impact", previous, back))


# --- owner ------------------------------------------------------------------------


def test_owner_answers_from_the_ownership_table(tmp_path, capsys):
    for state_class, authority in (("external_objective", "EXTERNAL_PM"),
                                   ("run_acceptance", "VERIHARNESS"),
                                   ("process_mechanics", "RUNTIME_ADAPTER")):
        rc, out, _ = _run(tmp_path, capsys, "owner", state_class)
        assert rc == 0
        assert json.loads(out) == {"state_class": state_class, "authority": authority}


def test_owner_refuses_an_undeclared_state_class(tmp_path, capsys):
    err = _refused(_run(tmp_path, capsys, "owner", "no_such_class"))
    assert "no_such_class" in err and "no authority is declared" in err
    assert not err.startswith('contract refused: "'), "the KeyError quoting is not the message"


def test_owner_without_an_argument_names_the_projections_too(tmp_path, capsys):
    rc, out, _ = _run(tmp_path, capsys, "owner")
    assert rc == 0
    table = json.loads(out)
    assert table == {name: authority.value for name, authority in OWNERSHIP.items()}
    projections = {name for name, a in table.items() if a == Authority.NONE.value}
    assert projections == {name for name, a in OWNERSHIP.items() if a is Authority.NONE}
    assert "ui_projection" in projections


# --- conflicts --------------------------------------------------------------------


def test_external_done_with_no_verdict_here_is_a_conflict(tmp_path, capsys):
    """Absence is not agreement: without --internal, every external DONE conflicts."""
    rc, out, _ = _run(tmp_path, capsys, "conflicts", _put(tmp_path, "c.json", _contract()))
    assert rc == 1
    body = json.loads(out)
    assert body["project_contract_id"] == "p1" and body["digest"]
    [conflict] = body["conflicts"]
    assert conflict["work_package_id"] == "w1"
    assert conflict["external_status"] == "DONE"
    assert conflict["internal_status"] == "NOT_VERIFIED"
    assert conflict["authoritative"] == "VERIHARNESS"
    assert "the verdict here stands" in conflict["statement"]


def test_a_verified_package_is_not_a_conflict(tmp_path, capsys):
    path = _put(tmp_path, "c.json", _contract())
    rc, out, _ = _run(tmp_path, capsys, "conflicts", path,
                      "--internal", json.dumps({"w1": "VERIFIED"}))
    assert rc == 0
    assert json.loads(out)["conflicts"] == []


def test_conflicts_refuses_internal_statuses_that_are_not_a_map(tmp_path, capsys):
    path = _put(tmp_path, "c.json", _contract())
    _refused(_run(tmp_path, capsys, "conflicts", path, "--internal", json.dumps(["w1"])))
    _refused(_run(tmp_path, capsys, "conflicts", path, "--internal", "{not json"))


def test_conflicts_refuses_internal_statuses_that_are_not_strings(tmp_path, capsys):
    path = _put(tmp_path, "c.json", _contract())
    _refused(_run(tmp_path, capsys, "conflicts", path, "--internal", json.dumps({"w1": 1})))


def test_conflicts_refuses_internal_ids_the_contract_does_not_have(tmp_path, capsys):
    path = _put(tmp_path, "c.json", _contract())
    err = _refused(_run(tmp_path, capsys, "conflicts", path,
                        "--internal", json.dumps({"w1": "VERIFIED", "zz9": "VERIFIED"})))
    assert "zz9" in err


def test_conflicts_refuses_a_contract_without_effective_at(tmp_path, capsys):
    raw = _contract()
    del raw["effective_at"]
    err = _refused(_run(tmp_path, capsys, "conflicts", _put(tmp_path, "c.json", raw)))
    assert "effective_at" in err


# --- read-only, and reached ------------------------------------------------------


def test_the_contract_commands_write_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    previous = _put(tmp_path, "v1.json", _contract())
    proposed = _put(tmp_path, "v2.json", _contract(version=2, objective="changed"))

    def snapshot() -> dict[str, bytes | None]:
        return {str(p.relative_to(tmp_path)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(tmp_path.rglob("*"))}

    before = snapshot()
    invocations = (
        ("schema",),
        ("check", previous),
        ("impact", previous, proposed),
        ("owner",),
        ("owner", "program_dag"),
        ("conflicts", previous),
        ("conflicts", previous, "--internal", json.dumps({"w1": "VERIFIED"})),
        ("check", str(tmp_path / "absent.json")),
    )
    for argv in invocations:
        _run(tmp_path, capsys, *argv)
    assert snapshot() == before
    assert not (tmp_path / "runs").exists(), "no run root is created"


def test_the_command_surface_names_the_module_the_matrix_measures():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_contract_cli", ROOT / "tools" / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix_for_contract_cli"] = cm
    spec.loader.exec_module(cm)

    graph = cm._import_graph()
    assert "src/hoh/cli.py" in cm._reachable_from_an_entry_point("projectcontract", graph)

    needs_evidence(PROGRAM_REGISTER)
    register = json.loads((ROOT / PROGRAM_REGISTER.path).read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in register["requirements"]}
    symbols = cm._symbols()
    collection = ("tests/test_projectcontract.py", True)
    for item in ("P2-29", "P2-30"):
        row = cm.classify(rows[item], symbols, collection, {}, graph)
        assert row["status"] == cm.PROVEN, (item, row["status"], row["evidence"])
