"""The rename tool: does it touch only identifiers, and only the right ones?

A rename commit's whole value is that it contains no judgement calls. That
holds only if the instrument is incapable of making one, so every case here is
about something the tool must leave alone.
"""
from __future__ import annotations

import importlib.util
import keyword
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "identifiers_probe", ROOT / "tools" / "identifiers.py")
ident = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ident)


def test_a_german_word_in_a_string_or_comment_is_not_an_identifier():
    """The ledger is German and several checks read it. Rewriting `"Priorität"`
    inside a pattern would change behaviour while looking like a rename."""
    source = (
        '"""A docstring that says ziel and means it."""\n'
        '# a comment about ziel\n'
        'MARKER = "getracktes Todo"\n'
        'ziel = MARKER + "ziel"\n'
    )
    renamed, n = ident.rename_source(source, ident.DEFAULT)
    # Exactly one bare NAME token in that source; the other three occurrences
    # are a docstring, a comment and a string literal.
    assert n == 1, renamed
    assert 'MARKER = "getracktes Todo"' in renamed
    assert '# a comment about ziel' in renamed
    assert '"""A docstring that says ziel and means it."""' in renamed
    assert '"ziel"' in renamed, "the string literal is untouched"
    assert "target = MARKER" in renamed


def test_nothing_but_the_names_moves():
    """Rebuilt by slicing lines, not by untokenize: a rename commit that also
    reformats is a rename commit nobody can review."""
    source = (
        "x   =    1\n"
        "\n"
        "\n"
        "def f(  ziel ,\n"
        "        pfad = 2 ):\n"
        "    return ziel,pfad   # trailing\n"
    )
    renamed, n = ident.rename_source(source, ident.DEFAULT)
    assert n == 4
    assert renamed == (
        "x   =    1\n"
        "\n"
        "\n"
        "def f(  target ,\n"
        "        path = 2 ):\n"
        "    return target,path   # trailing\n"
    )


def test_applying_it_twice_changes_nothing_the_second_time():
    source = "ziel = 1\npfad = ziel\n"
    once, n1 = ident.rename_source(source, ident.DEFAULT)
    twice, n2 = ident.rename_source(once, ident.DEFAULT)
    assert n1 == 3 and n2 == 0 and twice == once


def test_a_keyword_argument_and_its_parameter_rename_together():
    """Both are NAME tokens with the same spelling, so a call site cannot end
    up disagreeing with the definition it calls."""
    source = "def f(ziel=None):\n    return ziel\n\nf(ziel=1)\n"
    renamed, _ = ident.rename_source(source, ident.DEFAULT)
    assert "def f(target=None)" in renamed and "f(target=1)" in renamed
    assert "ziel" not in renamed


def test_one_word_can_mean_two_things_and_the_override_decides():
    """`zeile` is a board row in readiness.py and a line of output in
    telemetry.py. A single global choice would be wrong in one of them."""
    source = "zeile = 1\n"
    assert "row = 1" in ident.rename_source(
        source, ident.mapping_for("tools/readiness.py"))[0]
    assert "line = 1" in ident.rename_source(
        source, ident.mapping_for("src/hoh/telemetry.py"))[0]
    # The default exists and is the commonest reading; the override is what
    # makes the other one sayable. (It was originally the reverse -- no
    # default at all -- which left 20 files untouched rather than decided.)
    assert ident.DEFAULT["zeile"] == "line"
    assert ident.mapping_for("tools/readiness.py")["zeile"] == "row"


def test_the_negative_control_a_file_of_german_prose_is_not_renamed():
    source = (
        '"""Ein deutscher Docstring über Ziel, Pfad und Grund."""\n'
        'TEXT = "Priorität: mittel, getracktes Todo"\n'
    )
    renamed, n = ident.rename_source(source, ident.DEFAULT)
    assert n == 0 and renamed == source


def test_no_replacement_shadows_a_keyword_or_a_builtin():
    """`all`, `is`, `open`, `new`, `no` are the obvious traps; each of them is
    in the map on the German side."""
    forbidden = set(keyword.kwlist) | {
        "all", "any", "open", "is", "type", "id", "next", "list", "dict",
        "set", "str", "int", "input", "print", "filter", "map", "hash",
    }
    for german, english in ident.DEFAULT.items():
        assert english.isidentifier(), (german, english)
        assert english not in forbidden, (
            f"{german} -> {english} shadows a builtin or keyword")
        assert english != german, german


def test_every_override_is_for_a_file_that_exists():
    """An override for a path nobody has is a decision nobody applies."""
    for rel in ident.OVERRIDES:
        assert (ROOT / rel).is_file(), rel


def test_the_cli_reports_remaining_names_and_exits_nonzero(tmp_path):
    """`check` is the gate's instrument, so its exit code carries the answer."""
    f = tmp_path / "m.py"
    f.write_text("ziel = 1\n")
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "identifiers.py"), "check", str(f)],
        capture_output=True, text=True)
    assert p.returncode == 1 and "ziel" in p.stdout

    f.write_text("target = 1\n")
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "identifiers.py"), "check", str(f)],
        capture_output=True, text=True)
    assert p.returncode == 0


def test_the_gate_is_about_the_language_and_not_about_the_vocabulary():
    """O188. A check that only knew the translated words would pass the moment
    the next function is named `zeile_sonstwas` -- the sweep would hold for
    exactly as long as nobody wrote new German."""
    assert ident.is_german("zeile_sonstwas"), "an untranslated compound"
    assert ident.is_german("ANKER_NEU")
    assert not ident.is_german("row_list")
    assert not ident.is_german("export_manifest")
    # The negative control that matters: an English word containing a German
    # stem as a substring is not German. `parts` is not `teile`.
    for harmless in ("parts", "candidate_tree", "baseline", "counter",
                     "is_running", "leader", "einmalig_not_a_name"[:8]):
        assert not ident.is_german(harmless), harmless


def test_a_pinned_name_is_not_reported_forever():
    """A foreign API's spelling and a field name in stored data are held in
    place on purpose. A gate that kept reporting them could never reach green,
    and a gate that cannot reach green gets switched off."""
    mapping = {"starte_lauf": "starte_lauf", "zeile": "line"}
    source = "starte_lauf = 1\nzeile = 2\n"
    assert list(ident.remaining(source, mapping)) == ["zeile"]
    # And the pin is not a rename either.
    assert "starte_lauf = 1" in ident.rename_source(source, mapping)[0]


def test_the_foreign_prototype_is_pinned_and_still_exists():
    """The parity suite subclasses `hoh-operator-tools/autopilot.py`, which is
    a foreign tree this repository may read and must not write. Renaming the
    override left the abstract method unimplemented and four parity cases
    raised NotImplementedError from a file the sweep must not touch."""
    pins = ident.OVERRIDES["tests/test_orchestrator_parity.py"]
    for name in ("starte_lauf", "globale_gates", "klassifiziere", "fahre",
                 "semantische_abhaengigkeit"):
        assert pins.get(name) == name, name
    sample = (ROOT / "tests" / "test_orchestrator_parity.py").read_text()
    assert "starte_lauf" in sample, (
        "the pin outlived the call it was protecting; drop the pin or restore "
        "the call, but do not leave a rule guarding nothing")
