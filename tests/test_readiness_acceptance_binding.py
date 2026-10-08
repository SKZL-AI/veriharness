"""The board judges an evidence probe exactly as the capability matrix does.

The readiness board's `acceptance` row called `evidence_status(probe)` without
the register entry, so a P2-33 line keyed `P2-35` read PROVEN on the board
while the matrix read NOT_DETERMINABLE (pkeybind integration review). And the
binding let two missing `authoritative_item`s match each other. Every case
here builds its own register, record and evidence directory under `tmp_path`;
the real register is never read, and no subprocess is started.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_P2_33 = "P2.exit-includes.unattended-test"
_P2_35 = "P2.exit-includes.controller-session-replacement"
_P2_11 = "P2.build-verify.crash-reboot-recovery"
_ABSENT = object()


def _load(name: str, alias: str, monkeypatch):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: `readiness` defines dataclasses, which look
    # their module up in sys.modules. monkeypatch removes it again.
    monkeypatch.setitem(sys.modules, alias, mod)
    spec.loader.exec_module(mod)
    return mod


class Tree:
    """A temporary tree with a durability record, its evidence directory and a
    register; the board and the test's own matrix both judge it."""

    def __init__(self, home: Path, rd, cm):
        self.home, self.rd, self.cm = home, rd, cm
        (home / "ev").mkdir(parents=True)
        (home / "ev" / "evidence.txt").write_text("preserved run\n")

    def record(self, requirements: dict | None = None, stressors: dict | None = None):
        data = {"evidence_head": "abc", "evidence_dirty": False,
                "requirements": requirements or {}, "stressors": stressors or {}}
        (self.home / "rec.json").write_text(json.dumps(data))

    def register(self, *entries: dict) -> list[dict]:
        path = self.home / "program/v3_3/REQUIREMENTS.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"requirements": list(entries)}))
        return list(entries)

    def matrix(self, entry: dict) -> tuple[str, str]:
        """The matrix's status for the same register entry."""
        return self.cm.evidence_status(entry["probe"], requirement=entry)

    def board(self):
        return self.rd.row_acceptance()


def _probe(key, **kw) -> dict:
    return {"kind": "evidence", "path": "rec.json", "evidence_dir": "ev",
            "deriver": "durability_e2e.py", "key": key, **kw}


def _entry(rid: str, probe: dict, item=_ABSENT) -> dict:
    entry = {"id": rid, "name": rid, "probe": probe}
    if item is not _ABSENT:
        entry["authoritative_item"] = item
    return entry


def _rec_entry(item=_ABSENT, satisfied=True) -> dict:
    entry = {"satisfied": satisfied}
    if item is not _ABSENT:
        entry["authoritative_item"] = item
    return entry


@pytest.fixture
def tree(tmp_path, monkeypatch) -> Tree:
    # `evidence_status` and `readiness` insert into sys.path; work on a copy.
    monkeypatch.setattr(sys, "path", list(sys.path))
    # The re-derivation always succeeds: the binding is what is under test.
    # The fresh module `row_acceptance` loads uses this same `subprocess`.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else k.get("args"), 0, "", ""))
    fake = types.ModuleType("durability_e2e")
    fake.stale_paths = lambda head: []
    monkeypatch.setitem(sys.modules, "durability_e2e", fake)
    # `row_acceptance` replaces sys.modules["capability_matrix"]; teardown
    # restores whatever was there (or nothing).
    monkeypatch.setitem(sys.modules, "capability_matrix",
                        sys.modules.get("capability_matrix",
                                        types.ModuleType("capability_matrix")))
    home = tmp_path / "tree"
    rd = _load("readiness", "kbr_readiness_probe", monkeypatch)
    monkeypatch.setattr(rd, "HOH", home)
    cm = _load("capability_matrix", "kbr_capability_matrix_probe", monkeypatch)
    monkeypatch.setattr(cm, "HOH", home)
    return Tree(home, rd, cm)


def _blocks(row, rd) -> bool:
    return (row.state, row.advisory) == (rd.FAIL, False)


def _passes(row, rd) -> bool:
    return (row.state, row.advisory) == (rd.PASS, False)


# --------------------------------------------------------------------------- #
# K1: the board refuses a mis-keyed probe the matrix refuses
# --------------------------------------------------------------------------- #

def test_mis_keyed_probe_blocks_the_board_as_the_matrix_refuses_it(tree):
    """Catches `row_acceptance` judging a probe without its register entry:
    a P2-33 line keyed `P2-35` then reads PROVEN on the board while the
    matrix reads NOT_DETERMINABLE. The control, keyed `P2-33`, is PROVEN in
    both, so the refusal is the key and nothing else."""
    tree.record({"P2-33": _rec_entry(_P2_33), "P2-35": _rec_entry(_P2_35)})

    (wrong,) = tree.register(_entry("P2-33", _probe("P2-35"), _P2_33))
    status, note = tree.matrix(wrong)
    assert status == tree.cm.NOT_DETERMINABLE, note
    row = tree.board()
    assert _blocks(row, tree.rd), row
    assert "P2-33" in row.value_, row

    (right,) = tree.register(_entry("P2-33", _probe("P2-33"), _P2_33))
    status, note = tree.matrix(right)
    assert status == tree.cm.PROVEN, note
    row = tree.board()
    assert _passes(row, tree.rd), row


def test_mis_keyed_stressor_without_stressor_for_blocks_the_board(tree):
    """Catches the board reading a `stressor:` key without the register entry:
    a stressor with no `stressor_for` then stands in for P2-11 on the board,
    while the matrix refuses it. The control adds `stressor_for: "P2-11"`
    and nothing else."""
    tree.record(stressors={"DS1": "PASS"})

    (bare,) = tree.register(_entry("P2-11", _probe("stressor:DS1"), _P2_11))
    status, note = tree.matrix(bare)
    assert status == tree.cm.NOT_DETERMINABLE, note
    assert "stressor_for" in note, note
    row = tree.board()
    assert _blocks(row, tree.rd), row
    # The row cuts each note at 70 characters, so only the id is asserted.
    assert "P2-11:" in row.value_, row

    (declared,) = tree.register(
        _entry("P2-11", _probe("stressor:DS1", stressor_for="P2-11"), _P2_11))
    status, note = tree.matrix(declared)
    assert status == tree.cm.PROVEN, note
    row = tree.board()
    assert _passes(row, tree.rd), row


def test_mis_keyed_each_probe_is_bound_to_its_own_register_entry(tree):
    """Catches `row_acceptance` passing one register entry (say the first) for
    every probe, or looking the wrong one up: two lines each keyed by their own
    id pass, and swapping the keys blocks the board on both ids."""
    tree.record({"P2-33": _rec_entry(_P2_33), "P2-35": _rec_entry(_P2_35)})

    own = tree.register(_entry("P2-33", _probe("P2-33"), _P2_33),
                        _entry("P2-35", _probe("P2-35"), _P2_35))
    assert [tree.matrix(e)[0] for e in own] == [tree.cm.PROVEN] * 2
    row = tree.board()
    assert _passes(row, tree.rd), row

    swapped = tree.register(_entry("P2-33", _probe("P2-35"), _P2_33),
                            _entry("P2-35", _probe("P2-33"), _P2_35))
    assert [tree.matrix(e)[0] for e in swapped] == [tree.cm.NOT_DETERMINABLE] * 2
    row = tree.board()
    assert _blocks(row, tree.rd), row
    assert "P2-33:" in row.value_ and "P2-35:" in row.value_, row


# --------------------------------------------------------------------------- #
# K2: a missing authoritative_item never binds, and the note names the side
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("record_item,register_item", [
    (_ABSENT, _ABSENT), ("", ""), (_ABSENT, ""), ("", _ABSENT),
], ids=["absent-absent", "empty-empty", "absent-empty", "empty-absent"])
def test_missing_item_on_both_sides_is_not_determinable(tree, record_item, register_item):
    """Catches two missing items matching each other (`None == None` or
    `"" == ""` binding -- the pkeybind review's surviving mutant): the probe
    must be NOT_DETERMINABLE, the note must name both sides, and the board
    must block."""
    tree.record({"P2-33": _rec_entry(record_item)})
    (entry,) = tree.register(_entry("P2-33", _probe("P2-33"), register_item))
    status, note = tree.matrix(entry)
    assert status == tree.cm.NOT_DETERMINABLE, note
    assert "record entry" in note and "register entry" in note, note
    row = tree.board()
    assert _blocks(row, tree.rd), row
    assert "P2-33" in row.value_, row


@pytest.mark.parametrize("record_item,key", [
    (_ABSENT, "P2-33"), ("", "P2-33"), (_P2_33, "P2-99"),
], ids=["absent", "empty", "no-record-entry"])
def test_missing_item_in_the_record_entry_names_only_the_record_entry(tree, record_item, key):
    """Catches a refusal that blames the wrong side, or both, when only the
    record entry lacks its item (including a key the record has no entry
    for): the register entry names its item, so the note must name the
    record entry and not the register entry."""
    tree.record({"P2-33": _rec_entry(record_item)})
    (entry,) = tree.register(_entry("P2-33", _probe(key), _P2_33))
    status, note = tree.matrix(entry)
    assert status == tree.cm.NOT_DETERMINABLE, note
    assert "record entry" in note, note
    assert "register entry" not in note, note
    assert _blocks(tree.board(), tree.rd)


@pytest.mark.parametrize("register_item", [_ABSENT, ""], ids=["absent", "empty"])
def test_missing_item_in_the_register_entry_names_only_the_register_entry(tree, register_item):
    """Catches a refusal that blames the wrong side, or both, when only the
    register entry lacks its item: the record entry names it, so the note
    must name the register entry and not the record entry."""
    tree.record({"P2-33": _rec_entry(_P2_33)})
    (entry,) = tree.register(_entry("P2-33", _probe("P2-33"), register_item))
    status, note = tree.matrix(entry)
    assert status == tree.cm.NOT_DETERMINABLE, note
    assert "register entry" in note, note
    assert "record entry" not in note, note
    assert _blocks(tree.board(), tree.rd)


def test_missing_item_control_both_present_and_equal_binds(tree):
    """Catches a missing-item check so broad that it refuses every
    requirement-id key: with both items present and equal the probe binds,
    PROVEN in the matrix and a passing row on the board."""
    tree.record({"P2-33": _rec_entry(_P2_33)})
    (entry,) = tree.register(_entry("P2-33", _probe("P2-33"), _P2_33))
    status, note = tree.matrix(entry)
    assert status == tree.cm.PROVEN, note
    row = tree.board()
    assert _passes(row, tree.rd), row
