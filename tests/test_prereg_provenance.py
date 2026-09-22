"""Were the exact registration bytes commit-bound before the first v3 dispatch?

A pre-registration that is written before a campaign and committed after it is
not a pre-registration -- it is a file that could still have been edited while
the campaign was deciding what it would say. The distinction is invisible in a
log, which records only the order in which things were *printed*, so it is
settled here from git objects and from the campaign's own result files.

The sequence this checks is a real one and it was close: the first registration
was committed, then immediately replaced because it had recorded the
interactive shell's environment rather than the campaign's, then the
replacement was committed, and the campaign started twelve seconds later. Two
registrations, two blobs; only one of them can be the one that binds.

`docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json` is the stored answer.
These tests re-derive it rather than reading it back.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import (
    RUN_EVIDENCE_V3,
    needs_evidence,
    needs_parked_predecessor,
)

HOH = Path(__file__).resolve().parent.parent
PATH = "docs/benchmarks/v3/PREREGISTRATION.json"
EVIDENCE = HOH / "docs/benchmarks/v3/PREREGISTRATION_PROVENANCE.json"


def git(*args: str) -> str:
    p = subprocess.run(["git", "-C", str(HOH), *args],
                       capture_output=True, text=True)
    if p.returncode:
        pytest.skip(f"git said: {(p.stderr or p.stdout).strip()[:120]}")
    return p.stdout.strip()


@pytest.fixture(scope="module")
def binding_() -> tuple[str, datetime]:
    """(the commit carrying the exact registration bytes, its commit time)."""
    if not (HOH / ".git").exists():
        pytest.skip("not a git checkout")
    blob = git("hash-object", PATH)
    hits_ = []
    for line in git("log", "--reverse", "--format=%H %cI", "--", PATH).splitlines():
        h, when_ = line.split(" ", 1)
        if git("rev-parse", f"{h}:{PATH}") == blob:
            hits_.append((h, datetime.fromisoformat(when_).astimezone(UTC)))
    assert hits_, (
        "no commit carries the exact bytes of the registration in the working "
        "tree -- the campaign ran under something that was never committed")
    return hits_[0]


def first_cell_start() -> datetime:
    cells = sorted((HOH / "dogfood/benchmark/results-v3").glob("*.json"))
    if not cells:
        needs_evidence(RUN_EVIDENCE_V3)
    return datetime.fromtimestamp(
        min(json.loads(f.read_text())["started_at"] for f in cells), UTC)


def test_the_registration_in_the_tree_is_the_one_that_was_committed():
    """An uncommitted or edited registration is a draft, whatever it says."""
    if not (HOH / ".git").exists():
        pytest.skip("not a git checkout")
    assert not git("status", "--porcelain", "--", PATH), (
        "the registration differs from its last commit")


def test_the_exact_bytes_were_committed_before_the_first_cell_ran(binding_):
    """The question the log cannot answer.

    Not "a registration existed" and not "a freeze was printed" -- these exact
    bytes, in a commit, before the first cell of the campaign started.
    """
    commit, when_ = binding_
    first = first_cell_start()
    assert when_ < first, (
        f"the binding commit {commit[:12]} is dated {when_.isoformat()}, the "
        f"first v3 cell started {first.isoformat()}")
    assert subprocess.run(
        ["git", "-C", str(HOH), "merge-base", "--is-ancestor", commit, "HEAD"]
    ).returncode == 0, "the binding commit is not an ancestor of HEAD"


def test_no_provider_dispatch_in_a_v3_run_tree_predates_the_binding_commit(
        binding_):
    """The stronger form of the same question.

    A cell's `started_at` is written before its arm runs, so it already bounds
    the first dispatch from below. This checks the dispatches themselves,
    where a record of them survives: arm A dispatches through a dispatcher
    that writes no telemetry, so its cells are covered by the test above.
    """
    _, when_ = binding_
    bases = set()
    for f in sorted((HOH / "dogfood/benchmark/results-v3").glob("*.json")):
        d = json.loads(f.read_text()).get("arm_detail") or {}
        for k in ("root", "worktree"):
            if d.get(k):
                bases.add(str(d[k]))
    seen_ = []
    for w in bases:
        for t in glob.glob(w + "/**/telemetry.jsonl", recursive=True):
            for line in open(t):
                try:
                    s = json.loads(line).get("started_at")
                except ValueError:                 # pragma: no cover - partial
                    continue
                if s:
                    seen_.append(s)
    if not seen_:
        needs_evidence(RUN_EVIDENCE_V3)
        # O173. The recorded cells are here -- that is what `braucht_evidenz`
        # asks about, and why it does not skip -- but the run trees they name
        # are scratch directories under /tmp that the campaign wrote and the
        # machine later reclaimed. Three states, not two, and only the middle
        # one was modelled: the cells can be present while the trees they point
        # into are gone. `min([])` then raised ValueError, so the check ended in
        # a crash rather than in either of the answers it exists to give.
        #
        # The distinction that decides it is whether a single named tree still
        # stands. None standing is an environment gap: there is nothing left to
        # read, and saying so is the honest answer. One standing and carrying no
        # telemetry is a finding, and still fails below -- which is the negative
        # control this branch would otherwise destroy.
        # Decided on the run roots alone. A candidate worktree never carries
        # telemetry -- the controller writes it into the run root -- so
        # counting surviving worktrees here would report trees that could not
        # answer the question even when they were new.
        roots = set()
        for f in sorted((HOH / "dogfood/benchmark/results-v3").glob("*.json")):
            d = json.loads(f.read_text()).get("arm_detail") or {}
            if d.get("root"):
                roots.add(str(d["root"]))
        present = sorted(w for w in roots if os.path.isdir(w))
        if not present:
            pytest.skip(
                f"none of the {len(roots)} run root(s) campaign v3 recorded "
                "still exists -- they are scratch directories the machine "
                "reclaimed, and the telemetry lived inside them. The recorded "
                "cells and their bound digests are unaffected and are checked "
                "by the tests around this one; this particular question cannot "
                "be asked again without re-running the campaign, which would "
                "make it a different campaign"
            )
        pytest.fail(
            f"{len(present)} of {len(roots)} recorded run root(s) still "
            "exist and not one carries a telemetry record: "
            + ", ".join(present[:3])
        )
    earliest = datetime.fromisoformat(min(seen_).replace("Z", "+00:00"))
    assert when_ < earliest, (
        f"a provider was dispatched at {min(seen_)}, before the registration "
        f"was committed at {when_.isoformat()}")


def test_the_stored_answer_matches_a_fresh_derivation(binding_):
    """The artifact is a record, not a source. If it drifts from what git and
    the result files say, the record is the thing that is wrong."""
    needs_evidence(RUN_EVIDENCE_V3)
    if not EVIDENCE.is_file():
        pytest.skip("no provenance artifact installed")
    stored = json.loads(EVIDENCE.read_text())
    commit, when_ = binding_

    assert stored["binding_commit"] == commit
    assert stored["binding_commit_committed_at_utc"] == when_.strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert stored["registration_sha256"] == hashlib.sha256(
        (HOH / PATH).read_bytes()).hexdigest()
    assert stored["registration_git_blob"] == git("hash-object", PATH)
    assert stored[
        "registration_exact_digest_committed_before_first_dispatch"] == (
        "YES" if when_ < first_cell_start() else "NO")


def test_the_earlier_registration_is_a_different_blob_and_is_kept(binding_):
    """Two registrations, two blobs. The parked one is not deleted, and it is
    not the one that binds -- both halves matter, and a check that only said
    "a registration was committed" would be satisfied by the wrong one."""
    needs_parked_predecessor("docs/benchmarks/v3/PREREGISTRATION.json")
    commit, _ = binding_
    all_ = [z.split(" ", 1)[0] for z in
            git("log", "--reverse", "--format=%H", "--", PATH).splitlines()]
    blobs = {h: git("rev-parse", f"{h}:{PATH}") for h in all_}
    assert len(set(blobs.values())) == len(blobs), (
        "two commits carry identical registration bytes -- then 'which one "
        "binds' is not a question this test can answer")
    parked = sorted((HOH / "docs/benchmarks/v3").glob(
        "PREREGISTRATION.json.v*"))
    assert parked, "the superseded registration was not kept"
