"""Shared fixtures, and one rule about evidence the export does not carry.

This project publishes its tools and its results, and deliberately does not
publish some of the material those tools measure. Three kinds, each for its
own reason:

* **the benchmark's task fixtures** (`dogfood/benchmark/tasks/`) -- the
  protocol's central premise is that the hidden suite is the verdict and no
  arm sees it. Publishing the hidden tests would make that premise
  unverifiable for any future campaign on the same tasks;
* **the campaign result trees** and the run evidence beneath them -- receipts
  record the absolute paths checks ran at, and redaction would break the
  digest each one carries (`docs/EVIDENCE_INDEX.md`);
* **superseded artifacts** parked beside their replacements, which are history
  rather than evidence.

A test that measures one of those has nothing to measure in a published
clone. It must say so and skip -- never pass quietly, which would report a
check that did not run as one that did, and never fail, which would tell a
reader the software is broken when the export is working as designed.

`braucht_evidenz` is that statement. It names the artifact and the reason in
the skip, so a reader of a CI log can tell the two situations apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent


def braucht_evidenz(pfad: str, warum: str) -> None:
    """Skip when an artifact this repository deliberately does not publish is
    absent. In the tree that holds it, the test runs as normal."""
    if not (WURZEL / pfad).exists():
        pytest.skip(f"{pfad} is not in this tree -- {warum}")


@pytest.fixture
def benchmark_aufgaben() -> Path:
    """The five task fixtures, or a skip naming why they are absent."""
    braucht_evidenz(
        "dogfood/benchmark/tasks",
        "the hidden suites are the benchmark's verdict and are not published, "
        "so a clone cannot re-derive a campaign's plan from them")
    return WURZEL / "dogfood/benchmark/tasks"


@pytest.fixture
def kampagne_v3() -> Path:
    """Campaign v3's result cells, or a skip naming why they are absent."""
    braucht_evidenz(
        "dogfood/benchmark/results-v3",
        "campaign result trees carry the absolute paths their checks ran at "
        "and are not published; docs/BENCHMARK_RESULTS_v3.md carries what they "
        "measured")
    return WURZEL / "dogfood/benchmark/results-v3"
