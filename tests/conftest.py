"""Shared fixtures, and one fail-closed rule about evidence the export withholds.

This project publishes its tools and its results and deliberately does not
publish some of the material those tools measure. A test that measures one of
those has nothing to measure in a published clone, and must say so and skip.

The dangerous version of that is `path missing -> skip`. Under it, an export
that **lost** a path it was supposed to ship would turn the affected tests
green-by-skipping instead of red, and the check would be fail-open exactly
where it is meant to be strict. So a skip is earned, not assumed, and all of
these have to hold:

1. the path really is absent;
2. `EXPORT_MANIFEST.json` is present and parses;
3. that path -- or, for a directory, the files under it -- is in the manifest;
4. the manifest says `EXCLUDE`, with the rule this kind of artifact is
   expected to carry;
5. the skip names the artifact and why it is withheld, in this artifact's own
   words rather than a generic "evidence missing".

Anything else fails:

    missing + expected EXCLUDE rule   -> skip, environment gap
    missing + INCLUDE                 -> fail, the export lost a public path
    missing + unclassified            -> fail, nobody decided about it
    missing + manifest absent/broken  -> fail, nothing authorises the skip
    missing + a different EXCLUDE rule-> fail, withheld for a reason that is
                                         not the one this test expects
    present                           -> no skip; the test runs

`unclassified` failing matters: it is the manifest's conservative default, the
answer it gives when no rule claimed a path. An undecided path is not a
declared withholding and must never buy a green test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
MANIFEST = WURZEL / "EXPORT_MANIFEST.json"


class Zurueckgehalten:
    """One kind of artifact the export withholds, and why."""

    def __init__(self, pfad: str, regel: str, grund: str) -> None:
        self.pfad, self.regel, self.grund = pfad, regel, grund


#: Each kind names its own reason. "evidence missing" would be true of all of
#: them and useful about none.
HIDDEN_FIXTURES = Zurueckgehalten(
    "dogfood/benchmark/tasks", "internal-working-document",
    "hidden benchmark fixtures intentionally withheld to preserve future "
    "benchmark secrecy -- the protocol's premise is that the hidden suite is "
    "the verdict and no arm sees it")
RUN_EVIDENCE_V2 = Zurueckgehalten(
    "dogfood/benchmark/results-v2", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS_v2.md carries "
    "what they measured")
RUN_EVIDENCE_V1 = Zurueckgehalten(
    "dogfood/benchmark/results", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS.md carries "
    "what they measured")
CONFINEMENT_EVIDENCE = Zurueckgehalten(
    "dogfood/planner-confinement", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/EVIDENCE_INDEX.md publishes the "
    "tree digests and every path-free field")
SELFHOST_EVIDENCE = Zurueckgehalten(
    "dogfood/selfhost-e2e", "internal-working-document",
    "excluded because the run tree records the worktree each dispatch ran in; "
    "dogfood/ATTRIBUTION.json names what it established")
RUN_EVIDENCE_V3 = Zurueckgehalten(
    "dogfood/benchmark/results-v3", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS_v3.md carries "
    "what they measured")


def _manifest() -> dict[str, tuple[str, str]]:
    """path -> (decision, rule). Raises if the manifest cannot authorise anything."""
    if not MANIFEST.is_file():
        pytest.fail(
            f"{MANIFEST.name} is absent, so nothing authorises an "
            f"environment-gap skip. A missing path with no manifest is a "
            f"broken tree, not a withheld artifact.")
    try:
        daten = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except ValueError as exc:
        pytest.fail(f"{MANIFEST.name} does not parse ({exc}), so nothing "
                    f"authorises an environment-gap skip")
    eintraege = daten.get("entries") if isinstance(daten, dict) else daten
    return {e["path"]: (e["decision"], e["rule"]) for e in eintraege or []}


def braucht_evidenz(art: Zurueckgehalten) -> None:
    """Skip only if the manifest declares this artifact withheld, for its
    expected reason. Fail in every other absent case; do nothing if present."""
    ziel = WURZEL / art.pfad
    if ziel.exists():
        return

    eintraege = _manifest()
    betroffen = {p: v for p, v in eintraege.items()
                 if p == art.pfad or p.startswith(art.pfad + "/")}
    if not betroffen:
        pytest.fail(
            f"{art.pfad} is absent and the manifest does not classify it. An "
            f"undecided path is not a declared withholding, so this is a "
            f"broken tree rather than an environment gap.")

    eingeschlossen = sorted(p for p, (d, _) in betroffen.items() if d == "INCLUDE")
    if eingeschlossen:
        pytest.fail(
            f"{art.pfad} is absent but the manifest says INCLUDE for "
            f"{len(eingeschlossen)} path(s) under it (e.g. "
            f"{eingeschlossen[0]}). The export lost a path it was supposed to "
            f"ship; that is a failure, not a gap.")

    falsche = sorted({r for _, (d, r) in betroffen.items()
                      if d == "EXCLUDE" and r != art.regel})
    if falsche:
        pytest.fail(
            f"{art.pfad} is absent and excluded, but under rule(s) "
            f"{', '.join(falsche)} rather than the expected {art.regel!r}. "
            f"A skip may only rest on the reason this test expects.")

    pytest.skip(f"{art.pfad} is not in this tree -- {art.grund}")


def braucht_geparkten_vorgaenger(basis: str) -> None:
    """The superseded-artifact case: a `<name>.v<stamp>` sibling of `basis`."""
    ordner = (WURZEL / basis).parent
    if ordner.is_dir() and sorted(ordner.glob(Path(basis).name + ".v*")):
        return
    eintraege = _manifest()
    geparkt = {p: v for p, v in eintraege.items()
               if p.startswith(basis + ".v")}
    if not geparkt:
        pytest.fail(
            f"no parked predecessor of {basis} and none in the manifest "
            f"either: an undecided absence is not a declared withholding")
    falsch = sorted({r for _, (d, r) in geparkt.items()
                     if d != "EXCLUDE" or r != "parked-predecessor"})
    if falsch:
        pytest.fail(
            f"a parked predecessor of {basis} is classified {falsch} rather "
            f"than EXCLUDE/parked-predecessor")
    pytest.skip(
        f"no parked predecessor of {basis} in this tree -- excluded "
        f"historical predecessor; the binding registration is published, and "
        f"that the two carry different blobs is checkable from git in the "
        f"repository that produced them")


@pytest.fixture
def benchmark_aufgaben() -> Path:
    braucht_evidenz(HIDDEN_FIXTURES)
    return WURZEL / HIDDEN_FIXTURES.pfad
