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


# --- O220/F7, O223/M12: the test half of a probe -----------------------------
#
# `_collected()` returned `(stdout, returncode == 0)` and the probe unpacked
# `text, ok = collection` and never read `ok`; "collected" was a substring test over
# the whole `--collect-only -q` output. pytest prints `ERROR tests/test_x.py` for a
# file that failed to import, so the file's name was "in the text" exactly when its
# tests could not be collected, and `test_a` is a substring of `test_a_extended` and
# of another file's `test_a`. Each case below is about what an unmeasured test half
# may never be: "collected".

_WIDGET = "tests/test_widget.py"


def _widget_row(collection, test=_WIDGET, **probe) -> dict:
    """A symbol probe whose module is consumed by an entry point, so that the test
    half is the only thing that can move the status."""
    req = _req(probe={"kind": "symbol", "symbol": "widget", "test": test, **probe})
    return cm.classify(req, {"widget": ["src/hoh/lonely.py:10"]}, collection, {},
                       {"lonely": {"tools/thing.py"}})


def _says_collected(row: dict) -> bool:
    return any(str(e).endswith(": collected") for e in row["evidence"])


def test_a_failed_collection_decides_per_file_and_never_shows_absence():
    """pytest exits non-zero and still lists the node ids of the files it could import.
    What it listed is collected (PM13: one broken file elsewhere must not demote every
    test-probed row); what it did not list is *not determinable* -- a failed collection
    cannot show that a test was not collected -- and the exit code must be a bool."""
    listed = f"{_WIDGET}::test_a\n"
    row = _widget_row((listed, False))
    assert row["status"] == cm.PROVEN and _says_collected(row)
    elsewhere = _widget_row((listed + "ERROR tests/test_other.py\n", False))
    assert elsewhere["status"] == cm.PROVEN

    # not listed, no error line for the file: undecidable on a failed collection, "not
    # collected" on a sound one (the control, same text)
    for text in ("", "no tests collected in 0.09s\n",
                 "tests/test_other.py::test_a\nERROR tests/test_bad.py\n"):
        row = _widget_row((text, False))
        assert row["status"] == cm.NOT_DETERMINABLE, text
        assert not _says_collected(row), text
        assert not any("NOT collected" in str(e) for e in row["evidence"]), text
        assert any("NOT DETERMINABLE" in str(e) for e in row["evidence"]), text
        sound = _widget_row((text, True))
        assert sound["status"] == cm.IMPLEMENTED_NOT_PROVEN, text

    # the file's own error line outranks a listing of it
    own = _widget_row((listed + f"ERROR {_WIDGET} - boom\n", False))
    assert own["status"] == cm.NOT_DETERMINABLE and not _says_collected(own)

    for ok in (1, None, "yes", 0, "", 2):
        row = _widget_row((listed, ok))
        assert row["status"] == cm.NOT_DETERMINABLE, ok
        assert not _says_collected(row), ok


def test_an_error_line_for_the_tests_file_is_not_a_collected_test():
    """The substring form took `ERROR tests/test_widget.py` for the file being
    collected. Both spellings pytest prints (summary line, section header), and a
    node-level error, make that file's half NOT_DETERMINABLE even with `ok` true --
    a caller can pass one. An error in another file does not touch it."""
    for text in (f"ERROR {_WIDGET}\n",
                 f"ERROR {_WIDGET} - ModuleNotFoundError: No module named 'x'\n",
                 f"_ ERROR collecting {_WIDGET} _\n",
                 f"{_WIDGET}::test_a\nERROR {_WIDGET}::test_b - boom\n"):
        row = _widget_row((text, True))
        assert row["status"] == cm.NOT_DETERMINABLE, text
        assert not _says_collected(row), text
    elsewhere = _widget_row((f"{_WIDGET}::test_a\nERROR tests/test_other.py\n", True))
    assert elsewhere["status"] == cm.PROVEN
    # a longer file name that merely starts with the reference's path is another file
    longer = _widget_row((f"{_WIDGET}::test_a\nERROR {_WIDGET}x\n", True))
    assert longer["status"] == cm.PROVEN


@pytest.mark.parametrize("ref,text,collected", [
    # the reference is a file: some node id of that file
    pytest.param(_WIDGET, f"{_WIDGET}::test_a\n", True, id="file-listed"),
    pytest.param(_WIDGET, "tests/test_other.py::test_a\n", False, id="file-not-listed"),
    pytest.param(_WIDGET, f"other/{_WIDGET}::test_a\n", False,
                 id="file-path-suffix-is-not-the-path"),
    pytest.param(_WIDGET, f"{_WIDGET}.bak::test_a\n", False, id="file-longer-name"),
    pytest.param("tests/test_widget", f"{_WIDGET}::test_a\n", False,
                 id="file-prefix-of-a-name"),
    pytest.param("tests", f"{_WIDGET}::test_a\n", False, id="file-a-directory"),
    pytest.param(" " + _WIDGET, f"{_WIDGET}::test_a\n", False, id="file-leading-space"),
    pytest.param(_WIDGET.upper(), f"{_WIDGET}::test_a\n", False, id="file-upper-case"),
    # the reference is a node: that line, its parametrized ids, or what is inside it
    pytest.param(f"{_WIDGET}::test_a", f"{_WIDGET}::test_a\n", True, id="node-listed"),
    pytest.param(f"{_WIDGET}::test_a", f"{_WIDGET}::test_a_extended\n", False,
                 id="node-is-a-prefix-of-another-name"),
    pytest.param(f"{_WIDGET}::test_a", "tests/test_other.py::test_a\n", False,
                 id="node-same-name-in-another-file"),
    pytest.param(f"{_WIDGET}::test_a", f"{_WIDGET}::TestK::test_a\n", False,
                 id="node-same-name-in-a-class"),
    pytest.param(f"{_WIDGET}::test_a", f"{_WIDGET}::test_a[1]\n{_WIDGET}::test_a[2]\n",
                 True, id="node-parametrized-ids"),
    pytest.param(f"{_WIDGET}::test_a[2]", f"{_WIDGET}::test_a[1]\n{_WIDGET}::test_a[2]\n",
                 True, id="node-one-parameter"),
    pytest.param(f"{_WIDGET}::test_a[3]", f"{_WIDGET}::test_a[1]\n{_WIDGET}::test_a[2]\n",
                 False, id="node-parameter-not-listed"),
    pytest.param(f"{_WIDGET}::TestK", f"{_WIDGET}::TestK::test_m\n", True,
                 id="node-class-with-a-test"),
    pytest.param(f"{_WIDGET}::TestK::test_m", f"{_WIDGET}::TestK::test_m\n", True,
                 id="node-method-listed"),
    pytest.param(f"{_WIDGET}::TestK::test_m", f"{_WIDGET}::TestK::test_mm\n", False,
                 id="node-method-prefix"),
    pytest.param(f"{_WIDGET}::test_a", f"{_WIDGET}::test_a\r\n", True,
                 id="node-crlf-line"),
    pytest.param(f"{_WIDGET}::test_a\n", f"{_WIDGET}::test_a\n", False,
                 id="node-reference-holds-a-newline"),
])
def test_collected_means_the_exact_node_id(ref, text, collected):
    """With `ok` true and nothing listed an error, only the exact node id counts:
    IMPLEMENTED_NOT_PROVEN is the word for a symbol whose test was *not* collected."""
    row = _widget_row((text, True), test=ref)
    assert _says_collected(row) is collected, (ref, text)
    assert row["status"] == (cm.PROVEN if collected else cm.IMPLEMENTED_NOT_PROVEN)
    if not collected:
        assert any("NOT collected" in str(e) for e in row["evidence"])


def test_a_test_probe_that_could_not_be_measured_is_not_missing():
    """`kind: test` answered MISSING for a collection it could not read -- a statement
    about the software from a probe that never ran. NOT_DETERMINABLE, as for the symbol
    kinds; and MISSING stays the answer for a collection that is sound and lacks it."""
    probe = {"kind": "test", "test": f"{_WIDGET}::test_a"}
    for collection in (None, ("", False), (f"ERROR {_WIDGET}\n", True), "not a tuple"):
        row = cm.classify({"id": "X", "name": "x", "probe": probe}, {}, collection, {})
        assert row["status"] == cm.NOT_DETERMINABLE, collection
    sound = cm.classify({"id": "X", "name": "x", "probe": probe}, {},
                        ("tests/test_other.py::test_b\n", True), {})
    assert sound["status"] == cm.MISSING
    there = cm.classify({"id": "X", "name": "x", "probe": probe}, {},
                        (f"{_WIDGET}::test_a\n", True), {})
    assert there["status"] == cm.PROVEN


def test_an_unreadable_collection_or_reference_fails_closed_per_row():
    """Nothing here may raise out of `classify` -- one malformed value took all 184
    rows down before -- and nothing here may read as collected."""
    for collection in ("tests/test_widget.py", (None, True), (b"tests/x", True),
                       ("only one",), ("a", True, "extra"), 5, {"a": 1, "b": 2}):
        row = _widget_row(collection)
        assert row["status"] == cm.NOT_DETERMINABLE, collection
    for ref in (["tests/test_widget.py"], {"x": 1}, 5, "\x00", "::"):
        row = _widget_row((f"{_WIDGET}::test_a\n", True), test=ref)
        assert row["status"] in (cm.NOT_DETERMINABLE, cm.IMPLEMENTED_NOT_PROVEN), ref
        assert not _says_collected(row), ref


def _project(path: Path, **files: str) -> Path:
    """A throwaway pytest project: `files` maps a name under `tests/` to its source."""
    (path / "tests").mkdir(parents=True)
    (path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    for name, source in files.items():
        (path / "tests" / name).write_text(source, encoding="utf-8")
    return path


def test_collection_that_pytest_itself_calls_failed_is_not_a_collected_test(
        tmp_path, monkeypatch):
    """The whole path with a real pytest: one test file that cannot be imported makes
    pytest exit non-zero while still listing the other file's tests. The broken file's
    half is NOT_DETERMINABLE (it is not even mentioned as "collected"), the good file's
    stays PROVEN -- one broken file does not demote the rest -- and a node the failed
    collection does not list is undecidable, not "not collected". Control: the same
    project without the broken file is sound, and then absence is a finding."""
    broken = _project(tmp_path / "broken", **{
        "test_good.py": "def test_one():\n    pass\n",
        "test_bad.py": "import not_a_module_for_this_test\n\ndef test_two():\n    pass\n"})
    monkeypatch.setattr(cm, "HOH", broken)
    text, ok = cm._collected()
    assert ok is False
    assert "tests/test_good.py::test_one" in text          # listed
    assert "ERROR tests/test_bad.py" in text
    for ref in ("tests/test_good.py", "tests/test_good.py::test_one"):
        row = _widget_row((text, ok), test=ref)
        assert row["status"] == cm.PROVEN and _says_collected(row), ref
    for ref in ("tests/test_bad.py", "tests/test_bad.py::test_two",
                "tests/test_good.py::test_two", "tests/test_missing.py"):
        row = _widget_row((text, ok), test=ref)
        assert row["status"] == cm.NOT_DETERMINABLE, ref
        assert not _says_collected(row), ref

    sound = _project(tmp_path / "sound", **{"test_good.py": "def test_one():\n    pass\n"})
    monkeypatch.setattr(cm, "HOH", sound)
    text, ok = cm._collected()
    assert ok is True
    row = _widget_row((text, ok), test="tests/test_good.py::test_one")
    assert row["status"] == cm.PROVEN and _says_collected(row)
    absent = _widget_row((text, ok), test="tests/test_good.py::test_two")
    assert absent["status"] == cm.IMPLEMENTED_NOT_PROVEN


def test_pytest_collecting_nothing_is_not_a_sound_collection(tmp_path, monkeypatch):
    """Exit code 5 -- no tests collected -- is not 0: an empty `tests/` is not a suite
    that holds the named test, and it is not one that lacks it either."""
    monkeypatch.setattr(cm, "HOH", _project(tmp_path / "empty", **{"test_x.py": "x = 1\n"}))
    text, ok = cm._collected()
    assert ok is False
    assert _widget_row((text, ok), test="tests/test_x.py")["status"] == cm.NOT_DETERMINABLE


def test_collection_output_that_is_not_utf8_does_not_take_the_matrix_down(monkeypatch):
    """`text=True` raised UnicodeDecodeError out of `measure()` and with it every row.
    The output is decoded with replacement characters, which cannot spell a node id."""
    real = cm.subprocess.run
    script = ("import sys; sys.stdout.buffer.write("
              "b'tests/test_widget.py::test_a\\n\\xff\\xfe\\n')")

    def run(args, **kw):
        return real([sys.executable, "-c", script], **{
            k: v for k, v in kw.items() if k in ("capture_output", "text", "errors")})

    monkeypatch.setattr(cm.subprocess, "run", run)
    text, ok = cm._collected()
    assert ok is True
    assert "tests/test_widget.py::test_a" in text
    assert _widget_row((text, ok), test=f"{_WIDGET}::test_a")["status"] == cm.PROVEN
