"""Capability state and operational readiness must not define one another.

O197, stated as the consultant review put it: the capability layer and the
operational-readiness layer had been allowed to define one another. The
matrix read board rows, the board ran the matrix, and a capsule going stale
after an ordinary commit made an implemented capability flicker between
PROVEN and not. These tests hold the separation as a property of the code
rather than of the regeneration order.

    CAPABILITY EVIDENCE      symbols, contracts, collected tests, durable
                             evidence artefacts
    OPERATIONAL READINESS    the board, export sync, exact-head CI binding,
                             succession freshness, clean tree

The matrix may feed readiness. Readiness may not feed the matrix.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_sep_probe", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_sep_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


#: The operational artefacts. Each changes on an ordinary commit, export or
#: CI run, which is exactly why none of them may decide whether a capability
#: exists.
OPERATIONAL = (
    "docs/READINESS.md",
    "dogfood/succession/SUCCESSION.json",
    "dogfood/export-sync/EXPORT_SYNC_STATE.json",
    "dogfood/external-ci/EXACT_HEAD_CI.json",
)


def _strip_time(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "measured_at_utc"}


@pytest.fixture
def operational_state_unreadable(monkeypatch):
    """Every read of an operational artefact raises, in this process."""
    blocked = {str((ROOT / p).resolve()) for p in OPERATIONAL}
    real_text, real_bytes = pathlib.Path.read_text, pathlib.Path.read_bytes

    def guard(path):
        if str(Path(path).resolve()) in blocked:
            raise AssertionError(f"capability layer read operational state: {path}")

    def read_text(self, *a, **kw):
        guard(self)
        return real_text(self, *a, **kw)

    def read_bytes(self, *a, **kw):
        guard(self)
        return real_bytes(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)


def test_the_matrix_is_identical_when_operational_state_is_unreadable(
        operational_state_unreadable):
    """Tests 1-3 of the review in one: the board recording a regeneration, a
    capsule going stale, export sync moving -- none of them can change a
    capability status if the matrix cannot even read them."""
    needs_evidence(PROGRAM_REGISTER)
    cm = _load("capability_matrix")
    body = cm.measure()
    statuses = {r["id"]: r["status"] for r in body["capabilities"]}
    assert statuses, "no capabilities measured"
    # Durable capabilities that used to be decided by operational rows.
    assert statuses["P2-07"] == "PROVEN", "session succession exists and is tested"


def test_the_build_plan_does_not_read_operational_state(operational_state_unreadable):
    needs_evidence(PROGRAM_REGISTER)
    bp = _load("build_plan")
    cm = bp._matrix()
    body = cm.measure()
    order, gates = bp.phase_gates()
    plan = bp.build(body, order, gates)
    assert plan["ready_now"], "the frontier is computed without the board"


def test_the_programme_scope_does_not_read_the_register_or_operational_state(
        monkeypatch, operational_state_unreadable):
    """The inventory is the independent side of the completeness gate. It must
    not read the register it is compared against, either."""
    ps = _load("program_scope")
    plan = ps.plan_path()
    if plan is None:
        pytest.skip("the pinned plan is not reachable here")
    real_text = pathlib.Path.read_text

    def no_register(self, *a, **kw):
        if Path(self).name == "REQUIREMENTS.json":
            raise AssertionError("the inventory read the register")
        return real_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", no_register)
    assert ps.build(plan)["counts"]["phases"] == 13


def test_every_generator_is_idempotent_on_an_unchanged_tree():
    """Test 4: two consecutive runs over the same tree produce the same
    content. Timestamps are the only field allowed to differ, and they are
    excluded by name rather than by tolerance."""
    needs_evidence(PROGRAM_REGISTER)
    cm = _load("capability_matrix")
    assert _strip_time(cm.measure()) == _strip_time(cm.measure())

    bp = _load("build_plan")
    body = cm.measure()
    order, gates = bp.phase_gates()
    a = _strip_time(bp.build(json.loads(json.dumps(body)), order, gates))
    b = _strip_time(bp.build(json.loads(json.dumps(body)), order, gates))
    assert a == b

    ps = _load("program_scope")
    plan = ps.plan_path()
    if plan is not None:
        assert ps.build(plan) == ps.build(plan)

    bd = _load("baseline_docs")
    x, y = bd.measure(), bd.measure()
    for render in (bd.current_baseline, bd.product_baseline, bd.contract_trace):
        strip = lambda t: [z for z in t.splitlines() if not z.startswith("Measured at ")]
        assert strip(render(x)) == strip(render(y)), render.__name__


def test_no_register_probe_names_an_operational_source():
    """The register is where a future edit would reintroduce the cycle, so the
    rule is checked there too: no `row` probe, and no probe that names an
    operational artefact as its evidence."""
    needs_evidence(PROGRAM_REGISTER)
    register = json.loads((ROOT / "program/v3_3/REQUIREMENTS.json").read_text())
    for r in register["requirements"]:
        probe = r.get("probe") or {}
        assert probe.get("kind") != "row", r["id"]
        text = json.dumps(probe)
        for source in OPERATIONAL:
            assert source not in text, (r["id"], source)


def test_the_guard_actually_guards(operational_state_unreadable):
    """The negative control for every test above: if the fixture did not
    block anything, they would all pass against a matrix that read the board
    on every line."""
    for rel in OPERATIONAL:
        target = ROOT / rel
        if not target.exists():
            continue
        with pytest.raises(AssertionError, match="operational state"):
            target.read_text()
