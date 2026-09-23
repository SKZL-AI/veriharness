"""Is the environment-gap skip earned, or merely assumed?

`path missing -> skip` is fail-open. Under it an export that **lost** a path
it was supposed to ship would turn the affected tests green-by-skipping
instead of red, and the strictest checks in this project would be the ones
that quietly stopped checking.

So a skip has to be authorised by the manifest, for the reason the test
expects, and everything else has to fail. These are the controls for that --
one per way it could go wrong, plus the two positive ones, because a rule that
refused everything would pass every negative control and be useless.
"""

from __future__ import annotations

import json

import pytest

import conftest as cf


def _with_manifest(tmp_path, monkeypatch, entries):
    """A tree whose manifest says exactly what the test wants to try."""
    manifest = tmp_path / "EXPORT_MANIFEST.json"
    manifest.write_text(json.dumps({"schema": "t", "entries": entries}),
                        encoding="utf-8")
    monkeypatch.setattr(cf, "ROOT", tmp_path)
    monkeypatch.setattr(cf, "MANIFEST", manifest)


def _art(file_path="evidence/tree"):
    return cf.Withheld(file_path, "internal-working-document",
                              "withheld for its own stated reason")


# -- A: absent, but the manifest says it should be there --------------------- #


def test_absent_while_the_manifest_says_include_is_a_failure(tmp_path, monkeypatch):
    """The case the whole guard exists for: a broken export losing a public
    path must go red, not green-by-skipping."""
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "evidence/tree/a.json", "decision": "INCLUDE", "rule": "published-evidence"},
    ])
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_evidence(_art())
    assert "INCLUDE" in str(exc.value)
    assert "lost a path" in str(exc.value)


# -- B: absent and nobody classified it -------------------------------------- #


def test_absent_and_unclassified_is_a_failure(tmp_path, monkeypatch):
    """`unclassified` is the manifest's conservative default -- the answer it
    gives when no rule claimed a path. An undecided absence is not a declared
    withholding."""
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "something/else.md", "decision": "EXCLUDE", "rule": "internal-working-document"},
    ])
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_evidence(_art())
    assert "does not classify it" in str(exc.value)


def test_absent_under_the_unclassified_rule_is_a_failure(tmp_path, monkeypatch):
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "evidence/tree/a.json", "decision": "EXCLUDE", "rule": "unclassified"},
    ])
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_evidence(_art())
    assert "unclassified" in str(exc.value)


# -- C: absent, excluded, but for a different reason ------------------------- #


def test_absent_under_the_wrong_exclude_reason_is_a_failure(tmp_path, monkeypatch):
    """A skip may only rest on the reason this test expects. Withheld for some
    other reason is a different situation and the test has not been told about
    it."""
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "evidence/tree/a.json", "decision": "EXCLUDE", "rule": "stale-build-output"},
    ])
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_evidence(_art())
    assert "stale-build-output" in str(exc.value)
    assert "expected" in str(exc.value)


# -- the manifest itself missing or broken ----------------------------------- #


def test_absent_with_no_manifest_at_all_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "ROOT", tmp_path)
    monkeypatch.setattr(cf, "MANIFEST", tmp_path / "EXPORT_MANIFEST.json")
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_evidence(_art())
    assert "nothing authorises" in str(exc.value)


def test_absent_with_an_unreadable_manifest_is_a_failure(tmp_path, monkeypatch):
    manifest = tmp_path / "EXPORT_MANIFEST.json"
    manifest.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cf, "ROOT", tmp_path)
    monkeypatch.setattr(cf, "MANIFEST", manifest)
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_evidence(_art())
    assert "does not parse" in str(exc.value)


# -- D: present -- the guard must be inert ----------------------------------- #


def test_a_present_artifact_is_never_skipped(tmp_path, monkeypatch):
    """Where the evidence lives the test runs, and the guard does nothing.
    Without this control a rule that skipped everything would pass every
    negative control above."""
    (tmp_path / "evidence" / "tree").mkdir(parents=True)
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "evidence/tree/a.json", "decision": "EXCLUDE", "rule": "internal-working-document"},
    ])
    cf.needs_evidence(_art())          # must simply return


# -- E: absent for exactly the expected reason -------------------------------- #


def test_absent_for_the_expected_reason_skips_and_says_why(tmp_path, monkeypatch):
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "evidence/tree/a.json", "decision": "EXCLUDE", "rule": "internal-working-document"},
    ])
    with pytest.raises(pytest.skip.Exception) as exc:
        cf.needs_evidence(_art())
    assert "withheld for its own stated reason" in str(exc.value)


def test_each_declared_kind_names_its_own_reason():
    """"evidence missing" would be true of all of them and useful about none."""
    reasons = [cf.HIDDEN_FIXTURES.reason, cf.RUN_EVIDENCE_V2.reason,
               cf.RUN_EVIDENCE_V3.reason]
    assert len(set(reasons)) == len(reasons)
    assert "benchmark secrecy" in cf.HIDDEN_FIXTURES.reason
    assert "machine-local paths" in cf.RUN_EVIDENCE_V2.reason
    for g in reasons:
        assert "evidence missing" not in g


def test_the_parked_predecessor_guard_is_manifest_bound_too(tmp_path, monkeypatch):
    """Same rule for the superseded case: absent and unclassified must fail."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "R.json").write_text("{}", encoding="utf-8")
    _with_manifest(tmp_path, monkeypatch, [
        {"path": "docs/R.json", "decision": "INCLUDE", "rule": "published-evidence"},
    ])
    with pytest.raises(pytest.fail.Exception) as exc:
        cf.needs_parked_predecessor("docs/R.json")
    assert "none in the manifest" in str(exc.value)

    _with_manifest(tmp_path, monkeypatch, [
        {"path": "docs/R.json", "decision": "INCLUDE", "rule": "published-evidence"},
        {"path": "docs/R.json.v2026-09-13T21-57-02Z", "decision": "EXCLUDE",
         "rule": "parked-predecessor"},
    ])
    with pytest.raises(pytest.skip.Exception) as exc:
        cf.needs_parked_predecessor("docs/R.json")
    assert "excluded historical predecessor" in str(exc.value)


def test_an_unusable_git_is_not_a_foreign_history(tmp_path):
    """The attribution ledger's own fail-open edge.

    A repository that answers "no such object" demonstrably does not contain
    the anchor, and skipping there is honest. A `git` that cannot run answers
    nothing, and reading its silence as "different history" would make the
    check go quiet exactly when it cannot see.
    """
    import importlib.util
    import sys

    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "attribution", root / "tools" / "attribution.py")
    at = importlib.util.module_from_spec(spec)
    sys.modules["attribution"] = at
    spec.loader.exec_module(at)

    # tmp_path is not a git work tree.
    with pytest.raises(RuntimeError) as exc:
        at._anchor_exists(tmp_path, "0" * 40)
    assert "not a git work tree" in str(exc.value)
    assert "unknown is not an environment gap" in str(exc.value)

    # The positive control, built rather than assumed: a real repository with
    # a real commit. Asserting that *this* tree contains the ledger's anchor
    # would be circular -- in a published clone it does not, which is the
    # situation the guard exists for, so the control would fail exactly where
    # the guard is working.
    import subprocess

    real = tmp_path / "echt"
    real.mkdir()
    for argv in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(real), *argv], check=True,
                       capture_output=True)
    (real / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(real), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(real), "commit", "-qm", "one"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(real), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()

    assert at._anchor_exists(real, sha) is True
    assert at._anchor_exists(real, "0" * 40) is False


def test_stale_ci_evidence_is_refused_unless_only_the_gates_reports_moved(
        tmp_path, monkeypatch):
    """The readiness gate writes two of the files it certifies.

    `docs/READINESS.md` and the rendered `CLAIMS.md` are INCLUDE, and every run
    of the gate rewrites them -- so comparing the export against recorded CI
    evidence is red forever unless those two are named and tolerated. They are
    named. Anything else differing is stale evidence about different bytes,
    and stays a failure.
    """
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "readiness", root / "tools" / "readiness.py")
    rd = importlib.util.module_from_spec(spec)
    sys.modules["readiness"] = rd
    spec.loader.exec_module(rd)

    source = __import__("inspect").getsource(rd.row_ci)
    assert "path_digests" in source, (
        "the row must compare per-path digests, or it cannot name what moved")
    assert "beyond this gate's own reports" in source
    # Guarded as data since 2026-09-23: the set moved to module level and one
    # entry was added (see EXPORT_REPORTS). Reading it out of the row's source
    # made the last assertion below vacuous the moment the set moved, which a
    # reviewer demonstrated -- so the tolerance is now asserted against the
    # values and against the comparison's behaviour.
    assert rd.EXPORT_REPORTS == frozenset({
        "docs/READINESS.md", "CLAIMS.md", "CLAIMS.json", "dogfood/ATTRIBUTION.json"}), (
        "the tolerated set is not the gate's own bookkeeping any more")
    # A source file differing must read as stale evidence, not as a report.
    for path in ("src/hoh/runner.py", "src/hoh/cli.py", "tests/test_readiness.py"):
        _, real = rd.stale_export_paths({path: "before"}, {path: "after"})
        assert real == [path], (path, real)
