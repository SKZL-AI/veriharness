"""The export guard: does it stop before writing over work that is not ours?

Every case here is driven through `plane`, which decides on three digests and
touches nothing, plus one case that builds real throwaway git repositories to
exercise the parts that read objects and the remote head. Nothing in this file
touches the real staging checkout, the public repository or any tag -- the
thing being tested is a refusal, and testing a refusal by trying the dangerous
operation for real is the shape this project refuses everywhere else.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "export_sync_probe", WURZEL / "tools" / "export_sync.py")
es = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("export_sync_probe", es)
_spec.loader.exec_module(es)

A, B, C = "a" * 64, "b" * 64, "c" * 64


def test_our_change_is_written_and_their_change_stops_the_run():
    """The two cases the whole file exists to tell apart.

    Same shape on the surface -- one side differs from the base -- and opposite
    answers. Asserting both in one test is deliberate: a change that made the
    guard permissive would satisfy the first half and break the second.
    """
    unser = es.plane({"f": A}, {"f": B}, {"f": A})
    assert unser["schreiben"] == ["f"], unser
    assert not unser["konflikt"]

    ihrer = es.plane({"f": A}, {"f": A}, {"f": B})
    assert not ihrer["schreiben"]
    assert [p for p, _ in ihrer["konflikt"]] == ["f"], (
        "a path only they changed was queued for writing, which would have "
        "silently reverted it -- the defect this guard was built for")


def test_both_sides_changed_is_a_conflict_not_a_winner():
    plan = es.plane({"f": A}, {"f": B}, {"f": C})
    assert [p for p, _ in plan["konflikt"]] == ["f"]
    assert not plan["schreiben"]


def test_a_file_only_they_have_stops_the_run():
    """Their new file has no base and nothing here. Copying would not delete
    it -- this tool never deletes -- but exporting around it would report a
    success that quietly left their work unaccounted."""
    plan = es.plane({}, {}, {"neu.md": A})
    assert plan["nur_dort"] == ["neu.md"]


def test_a_file_gone_from_here_is_reported_and_never_removed():
    plan = es.plane({"weg.md": A}, {}, {"weg.md": A})
    assert plan["verschwunden"] == ["weg.md"]
    assert not plan["konflikt"]
    assert "loeschen" not in plan and "delete" not in plan, (
        "the plan grew a deletion bucket; this tool reports and never removes")


def test_recording_a_new_base_turns_their_change_into_agreement():
    """Integrating is expressed by recording a new base, and that is the only
    way to clear a conflict. Before: they moved, we did not -- stop. After
    recording their state as the base and taking their content: agreement."""
    vorher = es.plane({"f": A}, {"f": A}, {"f": B})
    assert vorher["konflikt"]
    nachher = es.plane({"f": B}, {"f": B}, {"f": B})
    assert not nachher["konflikt"] and nachher["unveraendert"] == ["f"]


def test_our_work_after_integrating_is_ours_not_a_conflict():
    """The case the first draft of `record` made impossible to express.

    Base recorded at their commit, then we keep working. That must read as
    ours, or every export after an integration would stop forever.
    """
    plan = es.plane({"f": B}, {"f": C}, {"f": B})
    assert plan["schreiben"] == ["f"]
    assert not plan["konflikt"]


def test_the_report_files_are_exempt_and_the_exemption_is_bounded():
    """The gate rewrites three files on every run, so they differ between syncs
    for a reason that is not anyone's work. The exemption must not be wider."""
    assert es.BERICHTE == {"docs/READINESS.md", "CLAIMS.md"}
    assert "CLAIMS.json" not in es.BERICHTE, (
        "O174: the ledger is written, not generated. Exempting it means a "
        "change made to it in the public repository is overwritten in silence "
        "-- which is what happened to six claims there, and what the next "
        "assertion pins"
    )
    ihre_claims = es.plane({"CLAIMS.json": A}, {"CLAIMS.json": A},
                           {"CLAIMS.json": B})
    assert [p for p, _ in ihre_claims["konflikt"]] == ["CLAIMS.json"]
    plan = es.plane({"CLAIMS.md": A}, {"CLAIMS.md": B}, {"CLAIMS.md": C})
    assert plan["berichte"] == ["CLAIMS.md"] and not plan["konflikt"]
    # Negative control: any other path with the same shape is a conflict.
    anders = es.plane({"docs/OTHER.md": A}, {"docs/OTHER.md": B},
                      {"docs/OTHER.md": C})
    assert [p for p, _ in anders["konflikt"]] == ["docs/OTHER.md"]


def test_no_force_push_anywhere_in_the_tool():
    """A guard that can be stepped over with a flag is not a guard."""
    quelle = (WURZEL / "tools" / "export_sync.py").read_text()
    for verboten in ("--force", "force-with-lease", "+refs/", "push -f"):
        assert verboten not in quelle, f"{verboten!r} appears in the tool"


def test_a_missing_sync_state_refuses_instead_of_assuming_one():
    """No base means no way to tell our change from theirs. Inventing an empty
    base would classify every one of their files as new and ours."""
    import pytest

    echt = es.STATE
    try:
        es.STATE = Path(tempfile.gettempdir()) / "nicht-vorhanden-export-sync.json"
        with pytest.raises(es.Abbruch) as exc:
            es.lade_zustand()
        assert "no sync state" in str(exc.value)
    finally:
        es.STATE = echt


def test_blob_digests_read_the_commit_and_not_the_worktree():
    """What the public repository has is what its commit says. A digest taken
    off the worktree would fold in an uncommitted edit and compare against a
    tree nobody published."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "r"
        repo.mkdir()

        def g(*a):
            return subprocess.run(["git", "-C", str(repo), *a],
                                  capture_output=True, text=True,
                                  check=True).stdout.strip()

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.invalid")
        g("config", "user.name", "t")
        (repo / "f.txt").write_text("committed\n")
        g("add", "-A")
        g("commit", "-q", "-m", "one")
        festgeschrieben = es.blob_digests(repo, "HEAD")["f.txt"]

        (repo / "f.txt").write_text("uncommitted edit\n")
        assert es.blob_digests(repo, "HEAD")["f.txt"] == festgeschrieben, (
            "an uncommitted edit changed the digest, so the comparison would "
            "be against a tree the public repository never published")


def test_a_head_that_moves_between_check_and_push_stops_the_run():
    """The comparison is a statement about one tree. If the public head moves
    after it and before the push, the statement is about a tree nobody is
    pushing to -- so the push must not happen on the strength of it.

    Driven against throwaway repositories with the module's own paths
    redirected. The real staging checkout, the real remote and every tag are
    untouched; a guard is tested by whether it refuses, not by performing the
    thing it refuses.
    """
    import json as _json

    with tempfile.TemporaryDirectory() as tmp:
        wurzel = Path(tmp)
        fern, staging, intern = wurzel / "fern.git", wurzel / "s", wurzel / "i"

        def g(repo, *a, check=True):
            return subprocess.run(["git", "-C", str(repo), *a],
                                  capture_output=True, text=True,
                                  check=check).stdout.strip()

        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(fern)],
                       check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(staging)],
                       check=True)
        g(staging, "config", "user.email", "t@example.invalid")
        g(staging, "config", "user.name", "t")
        g(staging, "remote", "add", "origin", str(fern))
        (staging / "f.txt").write_text("eins\n")
        (staging / "EXPORT_MANIFEST.json").write_text(_json.dumps(
            {"entries": [{"path": "f.txt", "decision": "INCLUDE"},
                         {"path": "EXPORT_MANIFEST.json", "decision": "INCLUDE"}]}))
        g(staging, "add", "-A")
        g(staging, "commit", "-q", "-m", "eins")
        g(staging, "push", "-q", "origin", "main")
        g(staging, "fetch", "-q", "origin")
        kopf_eins = g(staging, "rev-parse", "origin/main")

        intern.mkdir()
        shutil_src = staging
        (intern / "f.txt").write_text("unsere aenderung\n")
        (intern / "EXPORT_MANIFEST.json").write_text(
            (shutil_src / "EXPORT_MANIFEST.json").read_text())

        echt_hoh, echt_staging, echt_state = es.HOH, es.STAGING, es.STATE
        echt_git = es._git   # bound before the try, or a failure
                             # inside it is masked by NameError here
        try:
            es.HOH, es.STAGING = intern, staging
            es.STATE = wurzel / "state.json"
            assert es.record(kopf_eins) == 0

            # Someone else pushes while our comparison is already made. The
            # push deliberately lands *our own* content, so the comparison
            # finds nothing to object to and the run reaches the push check --
            # otherwise this test would pass on the conflict branch and say
            # nothing about the head at all.
            zweit = wurzel / "z"
            subprocess.run(["git", "clone", "-q", str(fern), str(zweit)],
                           check=True)
            g(zweit, "config", "user.email", "o@example.invalid")
            g(zweit, "config", "user.name", "o")
            (zweit / "f.txt").write_text("unsere aenderung\n")
            g(zweit, "add", "-A")
            g(zweit, "commit", "-q", "-m", "gleiche inhalte, neuer kopf")

            # The push has to land *between* the two fetches, or both see the
            # same head and this test would assert nothing. Pushing it on the
            # second fetch is what "the head moved while this ran" means.
            zustand = {"fetches": 0}

            def git_mit_rennen(repo, *args, **kw):
                if args[:1] == ("fetch",):
                    zustand["fetches"] += 1
                    if zustand["fetches"] == 2:
                        g(zweit, "push", "-q", "origin", "main")
                return echt_git(repo, *args, **kw)

            es._git = git_mit_rennen

            import io
            from contextlib import redirect_stdout
            puffer = io.StringIO()
            with redirect_stdout(puffer):
                rc = es.export(push=True)
            ausgabe = puffer.getvalue()
            assert rc == 1, (
                "the export reported success after the public head moved "
                "under it")
            assert "the public head moved" in ausgabe, (
                "the run stopped, but not for the head movement -- this test "
                "would then pass without ever exercising the check it names. "
                f"Output was: {ausgabe[-300:]}")
            assert "conflict:" not in ausgabe, (
                "the comparison objected, so the push check was never reached")
            assert zustand["fetches"] >= 2, "the second fetch never happened"
        finally:
            es.HOH, es.STAGING, es.STATE = echt_hoh, echt_staging, echt_state
            es._git = echt_git


def test_the_tool_carries_no_path_from_the_machine_that_wrote_it():
    """O175: this module ships in the export.

    An absolute path to one machine's home directory in a published tool is a
    small leak and a large nuisance -- nobody else can run the file without
    editing it. Nothing would have caught it: the export's home-path scan is
    scoped to documentation rules, and rightly so, because the policy pattern
    files under src/hoh/policy/ have to contain home-path patterns. So the
    check lives here, next to the tool it is about.
    """
    quelle = (WURZEL / "tools" / "export_sync.py").read_text()
    for muster in ("/home/", "/Users/", "/root/"):
        assert muster not in quelle, (
            f"{muster!r} appears in a file that ships to every reader")
    assert "VERIHARNESS_EXPORT_CHECKOUT" in quelle


def test_without_a_checkout_it_refuses_instead_of_guessing_one():
    import pytest

    echt = es.STAGING
    try:
        es.STAGING = None
        with pytest.raises(es.Abbruch) as exc:
            es.staging()
        assert "no export checkout" in str(exc.value)
    finally:
        es.STAGING = echt

