"""The table of tests that need withheld records must not rot (PUB1, 2026-10-14).

`tests/conftest.py` names, per test, the records the export withholds and the Herdr tests that a clone cannot run. A table like that
is only worth having if it can neither list a test that no longer exists nor authorise a skip over an artifact that the export
actually ships. These are the controls: the entries resolve to real test functions, and every artifact they name is declared
withheld by the manifest -- `EXCLUDE`, for no path under it `INCLUDE`, under the one rule the entry expects. The runtime half (skip
only when absent and declared) is `needs_evidence`, covered by `test_export_gap_guard.py`.
"""

from __future__ import annotations

import ast

import pytest

import conftest as cf


def _defined(path: str) -> dict[str, set[str]]:
    """Top-level test functions and, per class, its methods, by syntax and not by import."""
    tree = ast.parse((cf.ROOT / path).read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {"": set()}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[""].add(node.name)
        elif isinstance(node, ast.ClassDef):
            out[node.name] = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return out


def _keys() -> list[str]:
    return sorted(set(cf.NEEDS_WITHHELD) | set(cf.NEEDS_HERDR))


def test_the_table_is_not_empty_and_its_entries_have_the_expected_shape():
    assert cf.NEEDS_WITHHELD and cf.NEEDS_HERDR
    for key in _keys():
        parts = key.split("::")
        assert parts[0].startswith("tests/") and parts[0].endswith(".py") and len(parts) in (2, 3), key
        assert "[" not in key, f"{key}: list the test, not one of its parametrisations"


@pytest.mark.parametrize("key", _keys())
def test_every_listed_test_exists(key):
    """Catches a table entry that outlived its test: a stale entry would make a future test of the same name skip."""
    parts = key.split("::")
    where = cf.ROOT / parts[0]
    assert where.is_file(), f"{key}: {parts[0]} is not in this tree"
    defined = _defined(parts[0])
    cls, name = ("", parts[1]) if len(parts) == 2 else (parts[1], parts[2])
    assert name in defined.get(cls, set()), f"{key}: no such test"


def test_every_named_artifact_is_declared_withheld_by_the_manifest():
    """Catches a skip authorised over a path the export ships: every artifact the table names is, in the manifest, absent from the
    INCLUDE set, and carries exactly the rule the table expects (the same condition `needs_evidence` enforces at run time)."""
    entries = cf._manifest()
    arts = {a.path: a for arts_ in cf.NEEDS_WITHHELD.values() for a in arts_}
    assert arts
    for path, art in sorted(arts.items()):
        affected = {p: v for p, v in entries.items() if p == path or p.startswith(path + "/")}
        assert affected, f"{path}: the manifest does not classify it, so a skip over it is not authorised"
        shipped = sorted(p for p, (d, _) in affected.items() if d == "INCLUDE")
        assert not shipped, f"{path}: the export ships {shipped[:3]}"
        rules = {r for _, (d, r) in affected.items() if d == "EXCLUDE"}
        assert rules == {art.rule_}, f"{path}: withheld under {sorted(rules)}, the table expects {art.rule_!r}"
