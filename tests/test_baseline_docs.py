"""The baseline documents: do they ever say more than their source?

These are renderings of measurements taken elsewhere, so the failure mode is
not a wrong number -- it is a number with no source, or a question answered
that nothing here can answer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "baseline_docs_probe", ROOT / "tools" / "baseline_docs.py")
bd = importlib.util.module_from_spec(_spec)
sys.modules["baseline_docs_probe"] = bd
_spec.loader.exec_module(bd)

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


def test_the_cli_surface_is_read_from_the_parser_not_from_a_document():
    """A command list in a README is a claim about software. This one is the
    software: if a command is removed, the baseline stops naming it without
    anybody editing a file."""
    surface = bd.cli_surface()
    assert surface["source"].startswith("python3 -m hoh.cli"), surface
    assert {"run", "start", "status", "project"} <= set(surface["commands"]), surface


def test_the_unanswerable_questions_are_marked_and_not_estimated():
    """Time to first verified result depends on a provider, a machine and a
    person. A product baseline that guessed it would publish a number about
    this laptop as a number about the product."""
    needs_evidence(PROGRAM_REGISTER)
    text = bd.product_baseline(bd.measure())
    for question in ("time to first run", "time to first verified result",
                     "manual interventions per run"):
        row = [line for line in text.splitlines()
               if line.startswith("|") and question in line]
        assert row, question
        assert bd.NOT_MEASURED in row[0], row[0]
        # and the reason is in the same row, not left to the reader
        assert "depends on" in row[0] or "campaign" in row[0], row[0]


def test_every_figure_in_the_current_baseline_names_a_source():
    needs_evidence(PROGRAM_REGISTER)
    text = bd.current_baseline(bd.measure())
    figures = [line for line in text.splitlines()
               if line.startswith("* ") and any(ch.isdigit() for ch in line)]
    assert figures, "no figures rendered at all"
    for line in figures:
        assert "`" in line, f"a figure with no source: {line}"


def test_the_configuration_count_is_the_variables_it_lists():
    """A count and a list that disagree is the shape this project keeps
    finding: the number is quoted and the list is what is true."""
    cfg = bd.configuration_decisions()
    assert cfg["count"] == len(cfg["variables"])
    assert "VERIHARNESS_EXPORT_CHECKOUT" in cfg["variables"], cfg["variables"]
    for name, files in cfg["variables"].items():
        assert files and all(f.endswith(".py") for f in files), name


def test_the_contract_trace_has_a_row_for_every_requirement():
    needs_evidence(PROGRAM_REGISTER)
    body = bd.measure()
    text = bd.contract_trace(body)
    for cap in body["capabilities"]:
        assert f"`{cap['id']}`" in text, cap["id"]


def test_a_board_that_is_not_there_is_not_a_board_that_passed():
    """The rendering reads the board, and an absent board must not come out as
    a baseline with no red rows."""
    rows = bd.board_rows()
    assert rows["source"], rows
    if not (ROOT / "docs/READINESS.md").is_file():
        assert rows["source"] == bd.NOT_MEASURED
    else:
        assert rows["rows"], "a board with no rows read is a parser that broke"
