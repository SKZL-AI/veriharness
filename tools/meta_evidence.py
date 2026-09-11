#!/usr/bin/env python3
"""meta_evidence.py -- the release-critical metrics, each with its own provenance.

`src/hoh/assurance.py` states the rules. This is what applies them, and it
exists because a rule layer nothing calls is itself the failure mode it
describes: a module that makes a project *look* like it has a meta-evidence
gate, while the numbers that decide a release are still read wherever they
were read before. An adversarial reviewer put it exactly that way, and was
right.

So each metric here:

* names the file or command it reads, and reads it -- through the `from_*`
  builders, which digest the source's actual bytes;
* carries a **falsifier**: a deliberate break, applied to a *copy* of the
  source, that the metric has to notice. Run with `--falsify` the falsifiers
  execute and their outcome goes into the record. Without it every critical
  metric is `FalsifierState.NOT_RUN`, which is not a pass -- so the gate
  cannot go green on a run that skipped them;
* ends in one closure, which is green only when every record is authoritative
  and nothing required is missing.

Usage:
    python3 tools/meta_evidence.py [--repo PATH] [--falsify] [--json]

Exit code 0 only when the closure is green.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent / "src"))

from hoh.assurance import (  # noqa: E402
    AssuranceClosure,
    AssuranceRecord,
    EvidenceSource,
    FalsifierState,
    Provenance,
    ResultState,
    SourceKind,
    from_git,
    from_json_state,
    git_head,
)

#: The unattended run's evidence, kept in the repository rather than in a
#: scratch directory. A release-critical number whose source lives in a
#: session-scoped temp directory is a number that cannot be re-checked.
UNATT = Path("dogfood/unattended-e2e")


# --------------------------------------------------------------------------- #
# Falsifiers: break a copy, and see whether the metric notices
# --------------------------------------------------------------------------- #


def _falsify_json(pfad: Path, mutate, messen) -> tuple[FalsifierState, str]:
    """Applies `mutate` to a copy of a JSON file and re-measures.

    Returns KILLED when the measurement changed, ESCAPED when it did not.
    Nothing is written next to the original: the copy lives in a fresh temp
    directory that is a sibling of nothing.
    """
    if not pfad.exists():
        return FalsifierState.NOT_RUN, f"{pfad} does not exist, so nothing was broken"
    vorher = messen(json.loads(pfad.read_text()))
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-") as d:
        kopie = Path(d) / pfad.name
        daten = json.loads(pfad.read_text())
        beschreibung = mutate(daten)
        kopie.write_text(json.dumps(daten))
        nachher = messen(json.loads(kopie.read_text()))
    if nachher != vorher:
        return FalsifierState.KILLED, (
            f"{beschreibung}; the measurement moved from {vorher!r} to {nachher!r}"
        )
    return FalsifierState.ESCAPED, (
        f"{beschreibung}; the measurement stayed at {vorher!r} and noticed nothing"
    )


def _falsify_git(repo: Path, mutate, messen) -> tuple[FalsifierState, str]:
    """Same idea against a repository: clone it, break the clone, re-measure."""
    if not (repo / ".git").exists() and not repo.exists():
        return FalsifierState.NOT_RUN, f"{repo} is not a repository"
    vorher = messen(repo)
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-git-") as d:
        klon = Path(d) / "klon"
        p = subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(repo), str(klon)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            return FalsifierState.NOT_RUN, f"the clone failed: {p.stderr.strip()[:120]}"
        beschreibung = mutate(klon)
        nachher = messen(klon)
    if nachher != vorher:
        return FalsifierState.KILLED, (
            f"{beschreibung}; the measurement moved from {vorher!r} to {nachher!r}"
        )
    return FalsifierState.ESCAPED, (
        f"{beschreibung}; the measurement stayed at {vorher!r} and noticed nothing"
    )


# --------------------------------------------------------------------------- #
# The metrics
# --------------------------------------------------------------------------- #


def _interventions(zustand: dict) -> int:
    """Decisions a human made, plus repository mutations nothing accounts for.

    Both halves matter and the project has got each of them wrong once. The
    decision list alone missed commits nobody recorded; the record list alone
    was empty on a run that predated the mechanism, and that emptiness was
    read as a measured zero.
    """
    menschlich = [
        e for e in zustand.get("decisions", [])
        if e.get("actor") not in ("orchestrator", None)
    ]
    return len(menschlich) + len(zustand.get("external_actions", []))


def _geschlossen(zustand: dict) -> bool:
    knoten = zustand.get("nodes", [])
    return bool(knoten) and all(k.get("lifecycle") == "MERGED" for k in knoten)


def _kandidaten_einmal(repo: Path) -> bool:
    """Did every accepted candidate land exactly once?

    Read from git, never from a step log. A process killed between "merged"
    and "logged" writes the commit and no line, so the log is short in exactly
    the case the check is for -- which is how this passed on a list of length
    one.
    """
    p = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return False
    genommen = [z for z in p.stdout.splitlines() if z.startswith("Take accepted ")]
    return len(genommen) == len(set(genommen))


def _strict_quittungen(wurzel: Path) -> tuple[int, int, list[str]]:
    """(honoured, receipts seen, offenders) over a run tree's receipts.

    A receipt with no isolation record counts towards the total and towards
    the offenders. It used to be skipped before the total was incremented, so
    a run in which the guard refused every check -- zero executions, zero
    records -- returned (0, 0, []) and read as `NOT_DETERMINABLE`:
    indistinguishable from a run that was never attempted. A run where nothing
    happened must not be able to look like a run that does not exist.
    """
    gut, alle, schlecht = 0, 0, []
    for f in sorted(wurzel.rglob("receipts/*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        alle += 1
        iso = d.get("isolation")
        if iso is None:
            schlecht.append(f"{d.get('receipt_id', f.name)} (no isolation record)")
            continue
        geehrt = (
            iso.get("effective") == iso.get("requested")
            and not iso.get("fallback_to_none")
            and (iso.get("requested") == "none" or iso.get("verified_from_inside"))
        )
        if geehrt and iso.get("requested") == "strict":
            gut += 1
        elif iso.get("requested") == "strict":
            schlecht.append(d.get("receipt_id", f.name))
    return gut, alle, schlecht


def sammeln(repo: Path, *, falsify: bool) -> AssuranceClosure:
    kopf = git_head(repo)
    saetze: list[AssuranceRecord] = []

    zustand = repo / UNATT / "root/projects/unattended/project.json"

    # -- 1. the unattended run took no intervention --------------------------- #
    f_zustand, f_detail = (
        _falsify_json(
            zustand,
            lambda d: d["decisions"].append(
                {"kind": "POLICY_DISPOSITION", "actor": "a person"}
            ) or "a human policy disposition was appended to a copy of the state",
            _interventions,
        )
        if falsify else (FalsifierState.NOT_RUN, "")
    )
    saetze.append(
        from_json_state(
            "unattended_interventions",
            path=zustand,
            kind=SourceKind.PROJECT_STATE,
            derivation=(
                "decisions whose actor is not the orchestrator, plus "
                "external_actions -- on a state written after the record "
                "mechanism existed, so an empty list here is a measured zero"
            ),
            interpret=lambda d: (
                _interventions(d),
                ResultState.VERIFIED if _interventions(d) == 0 else ResultState.FAILED,
            ),
            subject_head=kopf,
            falsifier=f_zustand,
            falsifier_detail=f_detail,
        )
    )

    # -- 2. and it reached a fixpoint ---------------------------------------- #
    f2, f2d = (
        _falsify_json(
            zustand,
            lambda d: d["nodes"].append(
                {"id": "offen", "lifecycle": "READY", "spec_digest": "x"}
            ) or "an unmerged node was appended to a copy of the state",
            _geschlossen,
        )
        if falsify else (FalsifierState.NOT_RUN, "")
    )
    saetze.append(
        from_json_state(
            "unattended_fixpoint",
            path=zustand,
            kind=SourceKind.PROJECT_STATE,
            derivation="every node in the project state is MERGED",
            interpret=lambda d: (
                _geschlossen(d),
                ResultState.VERIFIED if _geschlossen(d) else ResultState.FAILED,
            ),
            subject_head=kopf,
            falsifier=f2,
            falsifier_detail=f2d,
        )
    )

    # -- 3. no candidate landed twice, read from git ------------------------- #
    bundle = repo / UNATT / "fixture.bundle"
    with tempfile.TemporaryDirectory(prefix="hoh-bundle-") as d:
        ziel = Path(d) / "fixture"
        p = subprocess.run(
            ["git", "clone", "-q", str(bundle), str(ziel)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            saetze.append(
                AssuranceRecord(
                    metric_id="unattended_no_duplicate_merge",
                    source=EvidenceSource(kind=SourceKind.ABSENT),
                    derivation=f"the evidence bundle could not be opened: "
                               f"{p.stderr.strip()[:120]}",
                    state=ResultState.NOT_DETERMINABLE,
                    provenance=Provenance.MEASURED,
                )
            )
        else:
            f3, f3d = (
                _falsify_git(
                    ziel,
                    lambda k: subprocess.run(
                        ["git", "-C", str(k), "commit", "-q", "--allow-empty",
                         "-m", "Take accepted candidate slugify (accepted slugify-i1)"],
                        capture_output=True,
                        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                             "PATH": "/usr/bin:/bin"},
                    ) and "a duplicate take-commit was added to a clone"
                    or "a duplicate take-commit was added to a clone",
                    _kandidaten_einmal,
                )
                if falsify else (FalsifierState.NOT_RUN, "")
            )
            saetze.append(
                from_git(
                    "unattended_no_duplicate_merge",
                    repo=ziel,
                    argv=["log", "--format=%s"],
                    derivation=(
                        "every 'Take accepted candidate ...' subject in the "
                        "fixture's own history appears exactly once"
                    ),
                    interpret=lambda p: (
                        _kandidaten_einmal(ziel),
                        ResultState.VERIFIED if _kandidaten_einmal(ziel)
                        else ResultState.FAILED,
                    ),
                    falsifier=f3,
                    falsifier_detail=f3d,
                    # The fixture is a repository of its own, pinned inside
                    # this one by the commit that carries the bundle.
                    subject_head=kopf,
                )
            )

    # -- 4. the STRICT acceptance run ---------------------------------------- #
    strikt = repo / "dogfood/strict-e2e"
    gut, alle, schlecht = _strict_quittungen(strikt)
    if not strikt.exists():
        saetze.append(
            AssuranceRecord(
                metric_id="strict_real_agent_e2e",
                source=EvidenceSource(kind=SourceKind.ABSENT),
                derivation="no STRICT acceptance evidence is present in the repository",
                state=ResultState.NOT_DETERMINABLE,
                provenance=Provenance.MEASURED,
            )
        )
    else:
        f4, f4d = (
            _falsifiziere_quittung(strikt)
            if falsify else (FalsifierState.NOT_RUN, "")
        )
        saetze.append(
            AssuranceRecord(
                metric_id="strict_real_agent_e2e",
                source=EvidenceSource(
                    kind=SourceKind.RECEIPT,
                    identity=str(strikt),
                    subject_head=kopf,
                ),
                derivation=(
                    f"{gut} of {alle} receipts report "
                    "requested=strict, effective=strict, no fallback, and "
                    "verified_from_inside -- the last of which is the namespace "
                    "comparison the launched command made from inside the sandbox"
                ),
                measured={"honoured": gut, "receipts": alle, "offenders": schlecht},
                state=(
                    ResultState.VERIFIED if gut and not schlecht
                    else ResultState.FAILED if schlecht
                    else ResultState.NOT_DETERMINABLE
                ),
                provenance=Provenance.MEASURED,
                falsifier=f4,
                falsifier_detail=f4d,
            )
        )

    return AssuranceClosure(
        subject_head=kopf,
        records=saetze,
        required=[
            "unattended_interventions",
            "unattended_fixpoint",
            "unattended_no_duplicate_merge",
            "strict_real_agent_e2e",
        ],
    )


def _falsifiziere_quittung(wurzel: Path) -> tuple[FalsifierState, str]:
    """Breaks one receipt in a copy and checks the metric notices."""
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-r-") as d:
        kopie = Path(d) / "strict"
        shutil.copytree(wurzel, kopie)
        ziel = next(iter(sorted(kopie.rglob("receipts/*.json"))), None)
        if ziel is None:
            return FalsifierState.NOT_RUN, "there was no receipt to break"
        daten = json.loads(ziel.read_text())
        if not daten.get("isolation"):
            return FalsifierState.NOT_RUN, "the receipt carries no isolation record"
        daten["isolation"]["verified_from_inside"] = False
        ziel.write_text(json.dumps(daten))
        gut, alle, schlecht = _strict_quittungen(kopie)
    if schlecht:
        return FalsifierState.KILLED, (
            "verified_from_inside was set to false on one receipt; the metric "
            f"reported it as an offender ({schlecht[0]})"
        )
    return FalsifierState.ESCAPED, (
        "verified_from_inside was set to false on one receipt and the metric "
        "reported nothing"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument(
        "--falsify", action="store_true",
        help="run each metric's negative control. Without this every critical "
             "metric stays NOT_RUN, which is not a pass, so the closure cannot "
             "go green -- that is deliberate.",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    schluss = sammeln(repo, falsify=args.falsify)
    if args.json:
        print(schluss.model_dump_json(indent=2))
    else:
        print(schluss.report())
        print()
        print("  states:", schluss.by_state())
    return 0 if schluss.green() else 1


if __name__ == "__main__":
    raise SystemExit(main())
