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

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "export_sync_probe", ROOT / "tools" / "export_sync.py")
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
    ours = es.plane({"f": A}, {"f": B}, {"f": A})
    assert ours["schreiben"] == ["f"], ours
    assert not ours["konflikt"]

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
    before = es.plane({"f": A}, {"f": A}, {"f": B})
    assert before["konflikt"]
    after = es.plane({"f": B}, {"f": B}, {"f": B})
    assert not after["konflikt"] and after["unveraendert"] == ["f"]


def test_our_work_after_integrating_is_ours_not_a_conflict():
    """The case the first draft of `record` made impossible to express.

    Base recorded at their commit, then we keep working. That must read as
    ours, or every export after an integration would stop forever.
    """
    plan = es.plane({"f": B}, {"f": C}, {"f": B})
    assert plan["schreiben"] == ["f"]
    assert not plan["konflikt"]


def test_no_path_is_exempt_from_the_comparison():
    """O174, then an independent review: the exemption list is empty now.

    It began as three paths copied from a different gate's tolerance. O174
    removed the ledger after an attack showed six public claims would have
    been reverted in silence. The review then asked why the other two were
    exempt at all, and the honest answer was that "generated" describes the
    content and not the decision -- a file being regenerable says nothing
    about whether somebody added something our regeneration will drop, which
    is exactly what O176 was.

    Both halves are pinned. Their change to a generated file must stop, and
    our own regeneration of it must still be written -- otherwise removing
    the exemption would have deadlocked every export instead of guarding it.
    """
    assert es.REPORTS == frozenset(), (
        "a path was exempted again; the kept-empty set is the record of why "
        "that is the wrong shape"
    )
    for path in ("docs/READINESS.md", "CLAIMS.md", "CLAIMS.json"):
        ihre = es.plane({path: A}, {path: A}, {path: B})
        assert [p for p, _ in ihre["konflikt"]] == [path], (
            f"a public change to {path} was not reported"
        )
        ours_ = es.plane({path: A}, {path: B}, {path: A})
        assert ours_["schreiben"] == [path], (
            f"our own regeneration of {path} was blocked, which would "
            "deadlock every export after a gate run"
        )


def test_no_force_push_anywhere_in_the_tool():
    """A guard that can be stepped over with a flag is not a guard."""
    source = (ROOT / "tools" / "export_sync.py").read_text()
    for forbidden in ("--force", "force-with-lease", "+refs/", "push -f"):
        assert forbidden not in source, f"{forbidden!r} appears in the tool"


def test_a_missing_sync_state_refuses_instead_of_assuming_one():
    """No base means no way to tell our change from theirs. Inventing an empty
    base would classify every one of their files as new and ours."""
    import pytest

    real = es.STATE
    try:
        es.STATE = Path(tempfile.gettempdir()) / "nicht-vorhanden-export-sync.json"
        with pytest.raises(es.Abort) as exc:
            es.load_state()
        assert "no sync state" in str(exc.value)
    finally:
        es.STATE = real


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
        committed_ = es.blob_digests(repo, "HEAD")["f.txt"]

        (repo / "f.txt").write_text("uncommitted edit\n")
        assert es.blob_digests(repo, "HEAD")["f.txt"] == committed_, (
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
        root = Path(tmp)
        remote_, staging, internal_ = root / "fern.git", root / "s", root / "i"

        def g(repo, *a, check=True):
            return subprocess.run(["git", "-C", str(repo), *a],
                                  capture_output=True, text=True,
                                  check=check).stdout.strip()

        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote_)],
                       check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(staging)],
                       check=True)
        g(staging, "config", "user.email", "t@example.invalid")
        g(staging, "config", "user.name", "t")
        g(staging, "remote", "add", "origin", str(remote_))
        (staging / "f.txt").write_text("eins\n")
        (staging / "EXPORT_MANIFEST.json").write_text(_json.dumps(
            {"entries": [{"path": "f.txt", "decision": "INCLUDE"},
                         {"path": "EXPORT_MANIFEST.json", "decision": "INCLUDE"}]}))
        g(staging, "add", "-A")
        g(staging, "commit", "-q", "-m", "eins")
        g(staging, "push", "-q", "origin", "main")
        g(staging, "fetch", "-q", "origin")
        head_one = g(staging, "rev-parse", "origin/main")

        internal_.mkdir()
        shutil_src = staging
        (internal_ / "f.txt").write_text("unsere aenderung\n")
        (internal_ / "EXPORT_MANIFEST.json").write_text(
            (shutil_src / "EXPORT_MANIFEST.json").read_text())

        real_hoh, real_staging, real_state = es.HOH, es.STAGING, es.STATE
        real_git = es._git   # bound before the try, or a failure
                             # inside it is masked by NameError here
        try:
            es.HOH, es.STAGING = internal_, staging
            es.STATE = root / "state.json"
            assert es.record(head_one) == 0

            # Someone else pushes while our comparison is already made. The
            # push deliberately lands *our own* content, so the comparison
            # finds nothing to object to and the run reaches the push check --
            # otherwise this test would pass on the conflict branch and say
            # nothing about the head at all.
            second_ = root / "z"
            subprocess.run(["git", "clone", "-q", str(remote_), str(second_)],
                           check=True)
            g(second_, "config", "user.email", "o@example.invalid")
            g(second_, "config", "user.name", "o")
            (second_ / "f.txt").write_text("unsere aenderung\n")
            g(second_, "add", "-A")
            g(second_, "commit", "-q", "-m", "gleiche inhalte, neuer kopf")

            # The push has to land *between* the two fetches, or both see the
            # same head and this test would assert nothing. Pushing it on the
            # second fetch is what "the head moved while this ran" means.
            state = {"fetches": 0}

            def git_with_race(repo, *args, **kw):
                if args[:1] == ("fetch",):
                    state["fetches"] += 1
                    if state["fetches"] == 2:
                        g(second_, "push", "-q", "origin", "main")
                return real_git(repo, *args, **kw)

            es._git = git_with_race

            import io
            from contextlib import redirect_stdout
            buffer_ = io.StringIO()
            with redirect_stdout(buffer_):
                rc = es.export(push=True)
            output_ = buffer_.getvalue()
            assert rc == 1, (
                "the export reported success after the public head moved "
                "under it")
            assert "the public head moved" in output_, (
                "the run stopped, but not for the head movement -- this test "
                "would then pass without ever exercising the check it names. "
                f"Output was: {output_[-300:]}")
            assert "conflict:" not in output_, (
                "the comparison objected, so the push check was never reached")
            assert state["fetches"] >= 2, "the second fetch never happened"
        finally:
            es.HOH, es.STAGING, es.STATE = real_hoh, real_staging, real_state
            es._git = real_git


def test_the_tool_carries_no_path_from_the_machine_that_wrote_it():
    """O175: this module ships in the export.

    An absolute path to one machine's home directory in a published tool is a
    small leak and a large nuisance -- nobody else can run the file without
    editing it. Nothing would have caught it: the export's home-path scan is
    scoped to documentation rules, and rightly so, because the policy pattern
    files under src/hoh/policy/ have to contain home-path patterns. So the
    check lives here, next to the tool it is about.
    """
    source = (ROOT / "tools" / "export_sync.py").read_text()
    for pattern_ in ("/home/", "/Users/", "/root/"):
        assert pattern_ not in source, (
            f"{pattern_!r} appears in a file that ships to every reader")
    assert "VERIHARNESS_EXPORT_CHECKOUT" in source


def test_without_a_checkout_it_refuses_instead_of_guessing_one():
    import pytest

    real = es.STAGING
    try:
        es.STAGING = None
        with pytest.raises(es.Abort) as exc:
            es.staging()
        assert "no export checkout" in str(exc.value)
    finally:
        es.STAGING = real


def _staging_fixture(w):
    """A bare remote, a staging clone at its head, and an internal tree that
    differs from it by one file."""
    import json as _json

    remote_, st, internal_ = w / "f.git", w / "s", w / "i"

    def g(repo, *a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                              text=True, check=True).stdout.strip()

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote_)],
                   check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(st)], check=True)
    g(st, "config", "user.email", "t@example.invalid")
    g(st, "config", "user.name", "t")
    g(st, "remote", "add", "origin", str(remote_))
    (st / "f.txt").write_text("base\n")
    (st / "EXPORT_MANIFEST.json").write_text(_json.dumps({"entries": [
        {"path": "f.txt", "decision": "INCLUDE"},
        {"path": "EXPORT_MANIFEST.json", "decision": "INCLUDE"}]}))
    g(st, "add", "-A")
    g(st, "commit", "-q", "-m", "base")
    g(st, "push", "-q", "origin", "main")
    g(st, "fetch", "-q", "origin")
    internal_.mkdir()
    (internal_ / "f.txt").write_text("internal change\n")
    (internal_ / "EXPORT_MANIFEST.json").write_text(
        (st / "EXPORT_MANIFEST.json").read_text())
    return st, internal_, g, g(st, "rev-parse", "origin/main")


def _export_in_fixture(w, prepare_):
    """Record a base, let `vorbereiten` dirty the staging tree, then export."""
    import io
    from contextlib import redirect_stdout

    st, internal_, g, head = _staging_fixture(w)
    real = es.HOH, es.STAGING, es.STATE
    try:
        es.HOH, es.STAGING, es.STATE = internal_, st, w / "state.json"
        with redirect_stdout(io.StringIO()):
            assert es.record(head) == 0
        prepare_(st, internal_, g)
        buffer_ = io.StringIO()
        with redirect_stdout(buffer_):
            rc = es.export(push=False)
        return rc, buffer_.getvalue(), st
    finally:
        es.HOH, es.STAGING, es.STATE = real


def test_uncommitted_work_in_the_staging_checkout_is_not_written_over():
    """The comparison is about the public head. The files land in a working
    tree, and those are not the same thing.

    The first version of this tool compared against `origin/main` and then
    wrote into the staging checkout without ever asking what state it was in
    -- so uncommitted work there was replaced by our version and the run
    reported success. Found by an independent review, not by this suite.

    All three kinds of dirt count, and each is asserted separately: unstaged,
    staged, and untracked. The assertion that matters in every case is not
    the exit code but the file: it must come back byte-for-byte.
    """
    cases_ = {
        "unstaged": lambda st, internal_, g: (st / "f.txt").write_text("THEIRS\n"),
        "staged": lambda st, internal_, g: ((st / "f.txt").write_text("THEIRS\n"),
                                         g(st, "add", "f.txt")),
    }
    for name, prepare_ in cases_.items():
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, st = _export_in_fixture(Path(tmp), prepare_)
            assert rc == 1, f"{name}: the export reported success"
            assert (st / "f.txt").read_text() == "THEIRS\n", (
                f"{name}: uncommitted work in the staging checkout was "
                "overwritten -- the defect this test exists for"
            )
            assert "not committed in the staging checkout" in out

    # Untracked, and a path we would newly create rather than replace.
    def untracked(st, internal_, g):
        import json as _json
        (internal_ / "neu.md").write_text("ours\n")
        (internal_ / "EXPORT_MANIFEST.json").write_text(_json.dumps({"entries": [
            {"path": "f.txt", "decision": "INCLUDE"},
            {"path": "neu.md", "decision": "INCLUDE"},
            {"path": "EXPORT_MANIFEST.json", "decision": "INCLUDE"}]}))
        (st / "neu.md").write_text("THEIRS UNTRACKED\n")

    with tempfile.TemporaryDirectory() as tmp:
        rc, out, st = _export_in_fixture(Path(tmp), untracked)
        assert rc == 1
        assert (st / "neu.md").read_text() == "THEIRS UNTRACKED\n", (
            "an untracked file in the staging checkout was overwritten"
        )


def test_a_staging_checkout_at_another_head_is_refused():
    """Writing into a tree that is not at the commit the comparison was made
    against produces a mixture of two states that nobody reviewed."""
    def back(st, internal_, g):
        (st / "andere.txt").write_text("x\n")
        g(st, "add", "-A")
        g(st, "commit", "-q", "-m", "staging moved on its own")

    with tempfile.TemporaryDirectory() as tmp:
        rc, out, st = _export_in_fixture(Path(tmp), back)
        assert rc == 1
        assert "not at the head this comparison was made against" in out
        assert (st / "f.txt").read_text() == "base\n", (
            "the export wrote into a checkout at a different head"
        )


def test_a_clean_staging_checkout_is_still_written_to():
    """The negative control for the two refusals above. A guard that refuses
    everything is not a guard, and this is the case the tool exists to do."""
    with tempfile.TemporaryDirectory() as tmp:
        rc, out, st = _export_in_fixture(Path(tmp), lambda st, i, g: None)
        assert rc == 0, out
        assert (st / "f.txt").read_text() == "internal change\n"


def test_nothing_here_resets_stashes_or_removes_foreign_work():
    """The refusal is the whole mechanism. A tool that tidied the staging
    checkout to get past its own guard would be the defect with a extra step."""
    source = (ROOT / "tools" / "export_sync.py").read_text()
    for forbidden in ("reset --hard", "git stash", "clean -", "checkout --",
                     "shutil.rmtree", "os.remove", "unlink("):
        assert forbidden not in source, (
            f"{forbidden!r} appears in a tool whose contract is that it never "
            "decides whose work survives"
        )


def test_the_timestamp_comes_from_the_standard_library():
    """`date -u` is not a command on Windows, and shelling out for something
    the standard library produces buys nothing. Found by a review running
    this on another platform.

    Asked of the syntax tree, not of the text. The first version of this test
    searched for the string `"date"` and failed on the comment explaining why
    the call was removed -- a check that fires on prose about the thing it
    forbids is the shape this project keeps finding in its own gates.
    """
    import ast

    source = (ROOT / "tools" / "export_sync.py").read_text()
    tree_ = ast.parse(source)
    for nodes in ast.walk(tree_):
        if not isinstance(nodes, ast.Call) or not nodes.args:
            continue
        first_item = nodes.args[0]
        if not isinstance(first_item, ast.List) or not first_item.elts:
            continue
        head = first_item.elts[0]
        if isinstance(head, ast.Constant) and head.value == "date":
            raise AssertionError(
                f"a subprocess call to `date` survives at line {nodes.lineno}"
            )
    assert "datetime.now(UTC)" in source

