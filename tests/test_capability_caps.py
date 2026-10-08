"""The two caps that lower a measured status, and the exemptions that let it stand.

O220. The P2 gate's own independent supervisor review found twelve items PROVEN for
modules nothing consumes, and three PROVEN over a documented equivalent against this
module's own written doctrine. Both rules existed only as judgement, applied by hand,
and so were applied unevenly.

`tools/capability_matrix.py` now derives them. This file exists because a reviewer
deleted both caps and every existing test still passed -- 290 lines of
status-lowering logic with nothing holding it in place. Each case here fails if its
rule is removed, and each has the control that shows it is about the rule and not
about the fixture.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "capability_matrix", ROOT / "tools" / "capability_matrix.py")
cm = importlib.util.module_from_spec(_spec)
sys.modules["capability_matrix"] = cm
_spec.loader.exec_module(cm)


def _req(**over) -> dict:
    base = {"id": "X-01", "name": "a thing", "target_phase": "PX",
            "authoritative_item": "PX.build.a-thing",
            "probe": {"kind": "symbol", "symbol": "widget",
                      "test": "tests/test_widget.py"}}
    base.update(over)
    return base


def _collected(text: str = "tests/test_widget.py") -> tuple[str, bool]:
    return (text, True)


# --- the consumption cap ----------------------------------------------------

def test_a_module_nothing_consumes_is_capped_at_partial():
    """The finding itself: a symbol that exists and a test that is collected reach
    PROVEN, and that says the module exists, not that the product uses it."""
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": set()})
    assert row["status"] == cm.PARTIAL
    assert any("CAPPED at PARTIAL" in str(e) for e in row["evidence"])


def test_the_same_module_reached_from_a_tool_is_proven():
    """The control: only the graph changes. Without it a cap that fired on
    everything would pass the case above."""
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": {"tools/thing.py"}})
    assert row["status"] == cm.PROVEN
    # "named in an import statement by", not "consumed by": O222/B1 showed a
    # never-executing import satisfies this, so the evidence line says what was
    # measured. A test asserting the friendlier wording would re-authorise the claim.
    assert any("named in an import statement by tools/thing.py" in str(e)
               for e in row["evidence"])
    assert not any("consumed by" in str(e) for e in row["evidence"])


def test_the_product_cli_counts_as_a_consumer():
    """`src/hoh/cli.py` is the product's command surface. An earlier version of this
    rule looked only under `tools/` and capped the sentinel and session succession,
    both of which the CLI reaches."""
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": {"src/hoh/cli.py"}})
    assert row["status"] == cm.PROVEN


def test_a_chain_that_never_reaches_an_entry_point_is_still_capped():
    """`decisions` is imported by `decisioncheck`, and `decisioncheck` by nothing. A
    one-hop check would have called the first of them consumed."""
    graph = {"first": {"src/hoh/second.py"}, "second": set()}
    row = cm.classify(_req(), {"widget": ["src/hoh/first.py:10"]},
                      _collected(), {}, graph)
    assert row["status"] == cm.PARTIAL


def test_a_cycle_is_not_reachability():
    graph = {"a": {"src/hoh/b.py"}, "b": {"src/hoh/a.py"}}
    row = cm.classify(_req(), {"widget": ["src/hoh/a.py:1"]},
                      _collected(), {}, graph)
    assert row["status"] == cm.PARTIAL


def test_a_symbol_defined_in_an_entry_point_is_not_capped():
    """Asking who consumes `tools/succession.py` or `src/hoh/cli.py` is asking who
    consumes the consumer. Nothing imports the top of a graph."""
    for where in ("tools/succession.py:257", "src/hoh/cli.py:724"):
        row = cm.classify(_req(), {"widget": [where]}, _collected(), {}, {})
        assert row["status"] == cm.PROVEN, where
        assert any("entry point" in str(e) for e in row["evidence"])


def test_the_cap_needs_a_graph_and_says_nothing_without_one():
    """`graph=None` is how every pre-existing caller invokes this, and it must not
    silently mean 'consumed'. It means 'not asked', and the row is unchanged."""
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {})
    assert row["status"] == cm.PROVEN
    assert not any("CAPPED" in str(e) for e in row["evidence"])


# --- the base exemption, as data --------------------------------------------

def test_the_plan_not_the_register_grants_the_base_exemption(monkeypatch, tmp_path):
    """O220/F6 asked for the base rule as data. The plan says "MCP Gateway base", and
    for such an item the base existing is the requirement."""
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"items": [
        {"id": "PX.build.a-thing", "text": "a thing base"}]}), encoding="utf-8")
    monkeypatch.setattr(cm, "SCOPE_FILE", scope)
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": set()})
    assert row["status"] == cm.PROVEN
    assert any("scopes this item as a base" in str(e) for e in row["evidence"])


@pytest.mark.parametrize("text", ["shared-baseline health classification",
                                  "the database schema", "the codebase"])
def test_base_must_be_a_whole_word(monkeypatch, tmp_path, text):
    """A substring match let "baseline", "database" and "codebase" grant the
    exemption -- a reviewer used the first of those to bring an unconsumed module to
    PROVEN. O220/F6 asked for data so it could not be granted by accident."""
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"items": [
        {"id": "PX.build.a-thing", "text": text}]}), encoding="utf-8")
    monkeypatch.setattr(cm, "SCOPE_FILE", scope)
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": set()})
    assert row["status"] == cm.PARTIAL, text


# --- the equivalent cap -----------------------------------------------------

def test_an_equivalent_caps_a_measured_proven():
    """This module's doctrine always said an equivalent never reaches PROVEN, and the
    code only ever raised MISSING. Three P2 items reached PROVEN through that gap."""
    req = _req(probe={"kind": "symbol", "symbol": "widget",
                      "test": "tests/test_widget.py",
                      "equivalent": "a flock, not a renewable lease"})
    row = cm.classify(req, {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": {"tools/thing.py"}})
    assert row["status"] == cm.PARTIAL
    assert any("CAPPED at PARTIAL by the equivalent" in str(e)
               for e in row["evidence"])


def test_without_the_equivalent_the_same_row_is_proven():
    row = cm.classify(_req(), {"widget": ["src/hoh/lonely.py:10"]},
                      _collected(), {}, {"lonely": {"tools/thing.py"}})
    assert row["status"] == cm.PROVEN


def test_an_equivalent_still_raises_missing():
    """The half that always worked must keep working."""
    req = _req(probe={"kind": "none", "equivalent": "something stands in"})
    row = cm.classify(req, {}, _collected(), {}, {})
    assert row["status"] == cm.PARTIAL


# --- module-scoped symbol resolution ---------------------------------------

def test_a_probe_naming_a_module_ignores_hits_elsewhere():
    """`authority_for` is defined in both assurance.py and projectcontract.py, and
    the bare name found the wrong one first -- so P2-30 was measured against a
    different function of the same name."""
    req = _req(probe={"kind": "symbol", "symbol": "widget",
                      "module": "wanted.py", "test": "tests/test_widget.py"})
    row = cm.classify(req, {"widget": ["src/hoh/other.py:1", "src/hoh/wanted.py:2"]},
                      _collected(), {}, {"wanted": {"tools/t.py"}})
    assert row["status"] == cm.PROVEN
    assert any("src/hoh/wanted.py:2" in str(e) for e in row["evidence"])
    assert any("outside wanted.py ignored" in str(e) for e in row["evidence"])


def test_the_module_name_is_compared_as_a_filename_not_a_suffix():
    """`endswith` let `tools/programme_checklist.py` satisfy a probe naming
    `checklist.py`, and a mis-scoped hit landing under tools/ bypassed the
    consumption cap outright."""
    req = _req(probe={"kind": "symbol", "symbol": "widget",
                      "module": "evidence.py", "test": "tests/test_widget.py"})
    row = cm.classify(req, {"widget": ["tools/meta_evidence.py:66"]},
                      _collected(), {}, {})
    assert row["status"] != cm.PROVEN, (
        "a probe naming src/hoh/evidence.py must not be satisfied by "
        "tools/meta_evidence.py")


def test_several_hits_without_a_module_are_flagged_ambiguous():
    row = cm.classify(_req(), {"widget": ["src/hoh/a.py:1", "src/hoh/b.py:2"]},
                      _collected(), {}, {"a": {"tools/t.py"}})
    assert any("AMBIGUOUS" in str(e) for e in row["evidence"])


def test_the_live_register_has_no_ambiguous_probe():
    """The four that were ambiguous -- P1-03, P2-21, P2-28, P2-30 -- now name their
    module. A new one appearing is a probe measuring something nobody chose."""
    matrix = json.loads(
        (ROOT / "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json").read_text(
            encoding="utf-8"))
    ambiguous = [c["id"] for c in matrix["capabilities"]
                 if any("AMBIGUOUS" in str(e) for e in c.get("evidence") or [])]
    assert ambiguous == [], ambiguous


def test_exactly_these_plan_items_are_base_exempt():
    """O222/B7. The docstring named three and the whole-word match reaches four --
    `evidenceclaim-schema-linkage-base` is the fourth, and it is PARTIAL today only
    because the equivalent cap runs last. Pinning the set means a fifth exemption is a
    change someone makes deliberately, which is the whole point of asking for the rule
    as data."""
    scope = json.loads(
        (ROOT / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json").read_text(
            encoding="utf-8"))
    exempt = sorted(i["id"] for i in scope["items"]
                    if cm._plan_calls_it_a_base(i["id"]))
    assert exempt == [
        "P2.build-verify.decision-exploration-base",
        "P2.build-verify.digital-thread-identifier-projection-base",
        "P2.build-verify.evidenceclaim-schema-linkage-base",
        "P2.build-verify.mcp-gateway-base",
    ], exempt


def test_the_equivalent_cap_survives_a_not_determinable_status():
    """O222/B8, the crash I introduced. `ORDER.index` raises on NOT_DETERMINABLE,
    which is deliberately not in ORDER, and it took all 184 rows down -- exactly the
    failure `evidence_status`'s docstring says was removed. Four rows already carry an
    equivalent on a `kind: none` probe."""
    for probe in ({"kind": "row", "row": "lint", "equivalent": "x"},
                  {"kind": "bogus", "equivalent": "x"}):
        row = cm.classify({"id": "X", "name": "x", "probe": probe}, {}, _collected(),
                          {"lint": ("PASS", "")}, {})
        assert row["status"] == cm.NOT_DETERMINABLE, probe
