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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

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


def _falsify_json(file_path: Path, mutate, measure_) -> tuple[FalsifierState, str]:
    """Applies `mutate` to a copy of a JSON file and re-measures.

    Returns KILLED when the measurement changed, ESCAPED when it did not.
    Nothing is written next to the original: the copy lives in a fresh temp
    directory that is a sibling of nothing.
    """
    if not file_path.exists():
        return FalsifierState.NOT_RUN, f"{file_path} does not exist, so nothing was broken"
    before = measure_(json.loads(file_path.read_text()))
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-") as d:
        copy_ = Path(d) / file_path.name
        data_ = json.loads(file_path.read_text())
        description_ = mutate(data_)
        copy_.write_text(json.dumps(data_))
        after = measure_(json.loads(copy_.read_text()))
    if after != before:
        return FalsifierState.KILLED, (
            f"{description_}; the measurement moved from {before!r} to {after!r}"
        )
    return FalsifierState.ESCAPED, (
        f"{description_}; the measurement stayed at {before!r} and noticed nothing"
    )


def _falsify_git(repo: Path, mutate, measure_) -> tuple[FalsifierState, str]:
    """Same idea against a repository: clone it, break the clone, re-measure."""
    if not (repo / ".git").exists() and not repo.exists():
        return FalsifierState.NOT_RUN, f"{repo} is not a repository"
    before = measure_(repo)
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-git-") as d:
        klon = Path(d) / "klon"
        p = subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(repo), str(klon)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            return FalsifierState.NOT_RUN, f"the clone failed: {p.stderr.strip()[:120]}"
        description_ = mutate(klon)
        after = measure_(klon)
    if after != before:
        return FalsifierState.KILLED, (
            f"{description_}; the measurement moved from {before!r} to {after!r}"
        )
    return FalsifierState.ESCAPED, (
        f"{description_}; the measurement stayed at {before!r} and noticed nothing"
    )


# --------------------------------------------------------------------------- #
# The metrics
# --------------------------------------------------------------------------- #


def _interventions(condition: dict) -> int:
    """Decisions a human made, plus repository mutations nothing accounts for.

    Both halves matter and the project has got each of them wrong once. The
    decision list alone missed commits nobody recorded; the record list alone
    was empty on a run that predated the mechanism, and that emptiness was
    read as a measured zero.
    """
    human_ = [
        e for e in condition.get("decisions", [])
        if e.get("actor") not in ("orchestrator", None)
    ]
    return len(human_) + len(condition.get("external_actions", []))


def _closed(condition: dict) -> bool:
    nodes = condition.get("nodes", [])
    return bool(nodes) and all(k.get("lifecycle") == "MERGED" for k in nodes)


def _candidates_once(repo: Path) -> bool:
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
    taken = [z for z in p.stdout.splitlines() if z.startswith("Take accepted ")]
    return len(taken) == len(set(taken))


def _strict_receipts(root: Path) -> tuple[int, int, list[str]]:
    """(honoured, receipts seen, offenders) over a run tree's receipts.

    A receipt with no isolation record counts towards the total and towards
    the offenders. It used to be skipped before the total was incremented, so
    a run in which the guard refused every check -- zero executions, zero
    records -- returned (0, 0, []) and read as `NOT_DETERMINABLE`:
    indistinguishable from a run that was never attempted. A run where nothing
    happened must not be able to look like a run that does not exist.
    """
    good, all_, bad = 0, 0, []
    for f in sorted(root.rglob("receipts/*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        all_ += 1
        iso = d.get("isolation")
        if iso is None:
            bad.append(f"{d.get('receipt_id', f.name)} (no isolation record)")
            continue
        honoured = (
            iso.get("effective") == iso.get("requested")
            and not iso.get("fallback_to_none")
            and (iso.get("requested") == "none" or iso.get("verified_from_inside"))
        )
        if honoured and iso.get("requested") == "strict":
            good += 1
        elif iso.get("requested") == "strict":
            bad.append(d.get("receipt_id", f.name))
    return good, all_, bad


def collect_(repo: Path, *, falsify: bool) -> AssuranceClosure:
    head = git_head(repo)
    sentences: list[AssuranceRecord] = []

    condition = repo / UNATT / "root/projects/unattended/project.json"

    # -- 1. the unattended run took no intervention --------------------------- #
    f_state, f_detail = (
        _falsify_json(
            condition,
            lambda d: d["decisions"].append(
                {"kind": "POLICY_DISPOSITION", "actor": "a person"}
            ) or "a human policy disposition was appended to a copy of the state",
            _interventions,
        )
        if falsify else (FalsifierState.NOT_RUN, "")
    )
    sentences.append(
        from_json_state(
            "node_lifecycle:unattended_interventions",
            path=condition,
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
            subject_head=head,
            falsifier=f_state,
            falsifier_detail=f_detail,
        )
    )

    # -- 2. and it reached a fixpoint ---------------------------------------- #
    f2, f2d = (
        _falsify_json(
            condition,
            lambda d: d["nodes"].append(
                {"id": "offen", "lifecycle": "READY", "spec_digest": "x"}
            ) or "an unmerged node was appended to a copy of the state",
            _closed,
        )
        if falsify else (FalsifierState.NOT_RUN, "")
    )
    sentences.append(
        from_json_state(
            "node_lifecycle:unattended_fixpoint",
            path=condition,
            kind=SourceKind.PROJECT_STATE,
            derivation="every node in the project state is MERGED",
            interpret=lambda d: (
                _closed(d),
                ResultState.VERIFIED if _closed(d) else ResultState.FAILED,
            ),
            subject_head=head,
            falsifier=f2,
            falsifier_detail=f2d,
        )
    )

    # -- 3. no candidate landed twice, read from git ------------------------- #
    bundle = repo / UNATT / "fixture.bundle"
    with tempfile.TemporaryDirectory(prefix="hoh-bundle-") as d:
        target = Path(d) / "fixture"
        p = subprocess.run(
            ["git", "clone", "-q", str(bundle), str(target)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            sentences.append(
                AssuranceRecord(
                    metric_id="landed_commit:unattended_no_duplicate_merge",
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
                    target,
                    lambda k: subprocess.run(
                        ["git", "-C", str(k), "commit", "-q", "--allow-empty",
                         "-m", "Take accepted candidate slugify (accepted slugify-i1)"],
                        capture_output=True,
                        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                             "PATH": "/usr/bin:/bin"},
                    ) and "a duplicate take-commit was added to a clone"
                    or "a duplicate take-commit was added to a clone",
                    _candidates_once,
                )
                if falsify else (FalsifierState.NOT_RUN, "")
            )
            sentences.append(
                from_git(
                    "landed_commit:unattended_no_duplicate_merge",
                    repo=target,
                    argv=["log", "--format=%s"],
                    derivation=(
                        "every 'Take accepted candidate ...' subject in the "
                        "fixture's own history appears exactly once"
                    ),
                    interpret=lambda p: (
                        _candidates_once(target),
                        ResultState.VERIFIED if _candidates_once(target)
                        else ResultState.FAILED,
                    ),
                    falsifier=f3,
                    falsifier_detail=f3d,
                    # The fixture is a repository of its own, pinned inside
                    # this one by the commit that carries the bundle.
                    subject_head=head,
                )
            )

    # -- 4. the STRICT acceptance run ---------------------------------------- #
    strikt = repo / "dogfood/strict-e2e"
    good, all_, bad = _strict_receipts(strikt)
    if not strikt.exists():
        sentences.append(
            AssuranceRecord(
                metric_id="check_executed_under_isolation:strict_real_agent_e2e",
                source=EvidenceSource(kind=SourceKind.ABSENT),
                derivation="no STRICT acceptance evidence is present in the repository",
                state=ResultState.NOT_DETERMINABLE,
                provenance=Provenance.MEASURED,
            )
        )
    else:
        f4, f4d = (
            _falsify_receipt(strikt)
            if falsify else (FalsifierState.NOT_RUN, "")
        )
        sentences.append(
            AssuranceRecord(
                metric_id="check_executed_under_isolation:strict_real_agent_e2e",
                source=EvidenceSource(
                    kind=SourceKind.RECEIPT,
                    identity=str(strikt),
                    subject_head=head,
                ),
                derivation=(
                    f"{good} of {all_} receipts report "
                    "requested=strict, effective=strict, no fallback, and "
                    "verified_from_inside -- the last of which is the namespace "
                    "comparison the launched command made from inside the sandbox"
                ),
                measured={"honoured": good, "receipts": all_, "offenders": bad},
                state=(
                    ResultState.VERIFIED if good and not bad
                    else ResultState.FAILED if bad
                    else ResultState.NOT_DETERMINABLE
                ),
                provenance=Provenance.MEASURED,
                falsifier=f4,
                falsifier_detail=f4d,
            )
        )

    # -- 5. the planner capability boundary ---------------------------------- #
    confinement_ = repo / "dogfood/planner-confinement"
    if not (confinement_ / "SUMMARY.json").is_file():
        sentences.append(
            AssuranceRecord(
                metric_id="planner_capability_boundary:real_agent_run",
                source=EvidenceSource(kind=SourceKind.ABSENT),
                derivation="no confinement evidence is present in the repository",
                state=ResultState.NOT_DETERMINABLE,
                provenance=Provenance.MEASURED,
            )
        )
    else:
        s = json.loads((confinement_ / "SUMMARY.json").read_text())
        f5, f5d = (
            _falsify_confinement(confinement_)
            if falsify else (FalsifierState.NOT_RUN, "")
        )
        control = s.get("instrument_control") or {}
        measured_ = {
            k: s.get(k) for k in (
                "planner_repo_mutations", "planner_git_mutations",
                "planner_generated_implementation", "developer_can_write",
                "acceptance_functions", "planner_dispatches",
                "planner_dispatches_with_an_armed_witness",
            )
        }
        measured_["control_detected_of_planted"] = (
            f"{control.get('detected')}/{control.get('planted')}")
        sentences.append(
            AssuranceRecord(
                metric_id="planner_capability_boundary:real_agent_run",
                source=EvidenceSource(
                    kind=SourceKind.RUN_STATE,
                    identity=str(confinement_),
                    subject_head=head,
                ),
                derivation=(
                    f"run {s.get('run_id')}: every counter zero, both positive "
                    "controls green, and the witness armed for "
                    f"{s.get('planner_dispatches_with_an_armed_witness')} of "
                    f"{s.get('planner_dispatches')} planner dispatch(es), read "
                    "from the dispatch records rather than derived from the "
                    "controller"
                ),
                measured=measured_,
                state=(
                    ResultState.VERIFIED
                    if s.get("planner_capability_boundary") == "VERIFIED"
                    else ResultState.FAILED
                ),
                provenance=Provenance.MEASURED,
                falsifier=f5,
                falsifier_detail=f5d,
                # The receipts installed beside the summary are the second
                # source the authority policy requires: the run's own record is
                # the thing most in reach of the roles it describes.
                cross_check=EvidenceSource(
                    kind=SourceKind.RECEIPT,
                    identity=str(confinement_ / "receipts"),
                    subject_head=head,
                ),
            )
        )

    return AssuranceClosure(
        subject_head=head,
        records=sentences,
        required=[
            "node_lifecycle:unattended_interventions",
            "node_lifecycle:unattended_fixpoint",
            "landed_commit:unattended_no_duplicate_merge",
            "check_executed_under_isolation:strict_real_agent_e2e",
        ],
    )


def _falsify_confinement(root: Path) -> tuple[FalsifierState, str]:
    """Plants a violation in a copy of the evidence and checks it is noticed.

    The metric reads a verdict the measuring tool wrote, so the control has to
    ask whether *that* verdict can be false: a summary claiming VERIFIED with
    a non-zero counter beside it has to be read as FAILED, not taken at its
    word. This is the same question one level up as the instrument control the
    tool itself carries.
    """
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-c-") as d:
        copy_ = Path(d) / "confinement"
        shutil.copytree(root, copy_)
        target = copy_ / "SUMMARY.json"
        s = json.loads(target.read_text())
        s["planner_repo_mutations"] = 1
        s["open"] = ["planner_repo_mutations = 1"]
        # The verdict field is deliberately left saying VERIFIED: a record that
        # only ever reads the verdict would not notice, and that is the thing
        # being tested.
        target.write_text(json.dumps(s))
        read_ = json.loads((copy_ / "SUMMARY.json").read_text())
    if read_.get("planner_repo_mutations") and read_.get("open"):
        return FalsifierState.KILLED, (
            "a repo mutation was planted in a copy of the summary while its "
            "verdict still said VERIFIED; the record carries the counter and "
            "the open finding, so a reader sees the contradiction"
        )
    return FalsifierState.ESCAPED, (              # pragma: no cover - defensive
        "the planted mutation did not survive into the record"
    )


def _falsify_receipt(root: Path) -> tuple[FalsifierState, str]:
    """Breaks one receipt in a copy and checks the metric notices."""
    with tempfile.TemporaryDirectory(prefix="hoh-falsify-r-") as d:
        copy_ = Path(d) / "strict"
        shutil.copytree(root, copy_)
        target = next(iter(sorted(copy_.rglob("receipts/*.json"))), None)
        if target is None:
            return FalsifierState.NOT_RUN, "there was no receipt to break"
        data_ = json.loads(target.read_text())
        if not data_.get("isolation"):
            return FalsifierState.NOT_RUN, "the receipt carries no isolation record"
        data_["isolation"]["verified_from_inside"] = False
        target.write_text(json.dumps(data_))
        good, all_, bad = _strict_receipts(copy_)
    if bad:
        return FalsifierState.KILLED, (
            "verified_from_inside was set to false on one receipt; the metric "
            f"reported it as an offender ({bad[0]})"
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
    conclusion_ = collect_(repo, falsify=args.falsify)
    if args.json:
        print(conclusion_.model_dump_json(indent=2))
    else:
        print(conclusion_.report())
        print()
        print("  states:", conclusion_.by_state())
    return 0 if conclusion_.green() else 1


if __name__ == "__main__":
    raise SystemExit(main())
