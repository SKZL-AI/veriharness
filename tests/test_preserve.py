"""Preserve instead of delete, as an invariant rather than a habit.

On 2026-09-23 the orchestrator ran `rm -rf` on a parked duplicate of a run's
evidence because it judged the copy redundant. Nothing unique was lost, which is
luck, not a result. The global denylist did not stop it and was right not to by
its own design rule -- a duplicate under a dated run directory is
indistinguishable from `node_modules` to a regular expression over a command
line. So the cases here are about the thing that was missing: a definition of
"project- and evidence-owned" that a guard can read, and a safe operation for
every destructive one it refuses.

The central case is `test_the_2026_09_23_workflow_cannot_silently_happen_again`,
which rebuilds the incident from its parts.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "preserve", ROOT / "tools" / "preserve.py")
pv = importlib.util.module_from_spec(_spec)
sys.modules["preserve"] = pv
_spec.loader.exec_module(pv)


def run_dir(base: Path, name: str = "20260923t1451z-p227") -> Path:
    """A directory shaped like a real run's evidence: dated name, markers inside."""
    made = base / name
    (made / "receipts").mkdir(parents=True)
    (made / "state.json").write_text('{"run_id": "x"}', encoding="utf-8")
    (made / "checks.json").write_text("[]", encoding="utf-8")
    (made / "receipts" / "k1.json").write_text('{"k": 1}', encoding="utf-8")
    return made


# --- the incident -----------------------------------------------------------

def test_the_2026_09_23_workflow_cannot_silently_happen_again(tmp_path):
    """The incident, rebuilt: evidence is copied somewhere, the copy is judged
    redundant, and `rm -rf` is reached for. Every step is allowed except the
    last, and the refusal names the operation that does the same job."""
    original = run_dir(tmp_path / "runs")
    parked = tmp_path / "parked" / original.name
    parked.parent.mkdir()
    import shutil
    shutil.copytree(original, parked)

    problems = pv.command_problems(["rm", "-rf", str(parked)])
    assert problems, "the copy is evidence; removing it is what the rule forbids"
    assert "dedupe" in problems[0], \
        "a refusal that does not name the safe operation is a refusal that gets " \
        "worked around"

    # And the safe operation does the job, with the claim proven rather than
    # believed.
    record = pv.dedupe(parked, original)
    assert record["status"] == "ARCHIVED"
    assert Path(record["archived_to"]).is_dir(), "the bytes are moved, not removed"
    assert (Path(record["archived_to"]).parent
            / f"{Path(record['archived_to']).name}.identical.json").is_file()
    assert pv.content_digest(Path(record["archived_to"])) == record["digest"]


def test_ordinary_local_cleanup_stays_free(tmp_path):
    """The negative control, and the reason the denylist was right: blocking
    `rm -rf node_modules` would make the guard something people disable."""
    junk = tmp_path / "project" / "node_modules"
    junk.mkdir(parents=True)
    (junk / "left.js").write_text("x", encoding="utf-8")
    assert pv.command_problems(["rm", "-rf", str(junk)]) == []
    assert pv.command_problems(["rm", "-rf", str(tmp_path / "project" / "build")]) == []


# --- what makes a path protected --------------------------------------------

def test_a_run_directory_is_protected_by_what_it_holds_not_by_its_name(tmp_path):
    """The case that was missed: a run directory is named after its run, so the
    name says nothing. What it contains says everything."""
    anonymous = tmp_path / "scratch"
    anonymous.mkdir()
    assert pv.classify(anonymous) == []

    (anonymous / "evidence.json").write_text("{}", encoding="utf-8")
    reasons = pv.classify(anonymous)
    assert any("record of a run" in r for r in reasons)


def test_a_runs_root_is_protected_by_the_run_directories_inside_it(tmp_path):
    """Removing the root removes every record in it, which is worse than the
    incident, not better."""
    root = tmp_path / "some-runs"
    run_dir(root)
    reasons = pv.classify(root)
    assert any("whose records would go with it" in r for r in reasons)


def test_a_dated_name_alone_is_enough_to_ask_twice(tmp_path):
    """The shape the destroyed path had. A directory that no longer exists cannot
    be inspected, so the name has to carry weight on its own."""
    reasons = pv.classify(tmp_path / "20260923T1451Z-p227-runs")
    assert any("date stamp" in r for r in reasons)
    assert pv.classify(tmp_path / "plain-directory") == []


def test_the_programmes_own_record_directories_are_protected(tmp_path):
    for name in ("dogfood", "program", "history", ".archiv"):
        reasons = pv.classify(tmp_path / name / "something")
        assert any("programme's own records" in r for r in reasons), name


def test_fundamental_paths_are_protected_outright(tmp_path):
    """Independently of contents, and independently of the command-line denylist,
    because this module is also called from code."""
    assert any("fundamental path" in r for r in pv.classify(Path("/")))
    assert any("fundamental path" in r for r in pv.classify(Path("/etc")))


def test_an_evidence_file_named_directly_is_protected(tmp_path):
    made = run_dir(tmp_path)
    assert any("itself an evidence record" in r
               for r in pv.classify(made / "state.json"))


# --- the commands the guard understands -------------------------------------

@pytest.mark.parametrize("argv", [
    ["rm", "-rf"],
    ["shred", "-u"],
    ["find", "-delete"],
    ["git", "clean", "-fdx"],
])
def test_every_destructive_shape_is_covered(tmp_path, argv):
    made = run_dir(tmp_path / "runs")
    full = argv + [str(made)] if argv[0] != "find" else ["find", str(made), "-delete"]
    assert pv.command_problems(full), f"{argv} slipped through"


def test_a_harmless_command_touching_the_same_path_is_not_refused(tmp_path):
    """The control for the parametrised case: the path is identical, only the
    verb changes. A guard that refused reads would be unusable."""
    made = run_dir(tmp_path / "runs")
    assert pv.command_problems(["ls", "-la", str(made)]) == []
    assert pv.command_problems(["cp", "-a", str(made), str(tmp_path / "copy")]) == []
    assert pv.command_problems(["tar", "-cf", "x.tar", str(made)]) == []


def test_the_guard_reads_an_argument_vector_not_shell_text(tmp_path):
    """Parsing shell text to decide a safety question lost four rounds in the
    P1-16 acceptance record. A path with a space in it is where a string-based
    guard silently stops protecting."""
    made = run_dir(tmp_path / "a runs dir")
    assert pv.command_problems(["rm", "-rf", str(made)])


# --- the safe operations ----------------------------------------------------

def test_supersede_keeps_every_byte_and_leaves_no_removal(tmp_path):
    made = run_dir(tmp_path / "runs")
    before = pv.content_digest(made)
    moved = pv.supersede(made)
    assert not made.exists()
    assert moved.is_dir()
    assert pv.content_digest(moved) == before
    assert ".v1." in moved.name and moved.name.endswith("Z")


def test_supersede_numbers_upward_instead_of_colliding(tmp_path):
    base = tmp_path / "runs"
    base.mkdir()
    for expected in (1, 2, 3):
        target = base / "thing"
        target.write_text(f"generation {expected}", encoding="utf-8")
        moved = pv.supersede(target)
        assert f".v{expected}." in moved.name, moved.name
    assert len(list(base.iterdir())) == 3, "every generation is still there"


def test_archive_moves_into_the_box_and_the_box_is_itself_protected(tmp_path):
    made = run_dir(tmp_path / "runs")
    before = pv.content_digest(made)
    moved = pv.archive(made)
    assert moved.parent.name == ".archiv"
    assert pv.content_digest(moved) == before
    assert pv.classify(moved), "what lands in the archive stays protected"


def test_superseding_something_that_is_not_there_is_an_error_not_a_success(tmp_path):
    with pytest.raises(FileNotFoundError):
        pv.supersede(tmp_path / "never-existed")


# --- dedupe: the claim has to be proven -------------------------------------

def test_dedupe_refuses_when_the_copy_differs_by_one_byte(tmp_path):
    """The difference is exactly the unique data a removal would have destroyed,
    so this is the case the whole operation exists for."""
    original = run_dir(tmp_path / "runs")
    copy = tmp_path / "parked" / original.name
    copy.parent.mkdir()
    import shutil
    shutil.copytree(original, copy)
    (copy / "receipts" / "k2.json").write_text('{"k": 2}', encoding="utf-8")

    record = pv.dedupe(copy, original)
    assert record["status"] == "REFUSED"
    assert copy.is_dir(), "nothing was moved, and nothing was removed"
    assert record["candidate_digest"] != record["original_digest"]


def test_dedupe_notices_a_rename_even_when_the_bytes_match(tmp_path):
    """Two trees holding the same bytes under different names are not the same
    evidence: which receipt is which is part of the record."""
    original = run_dir(tmp_path / "runs")
    copy = tmp_path / "parked" / original.name
    copy.parent.mkdir()
    import shutil
    shutil.copytree(original, copy)
    (copy / "receipts" / "k1.json").rename(copy / "receipts" / "k9.json")
    assert pv.dedupe(copy, original)["status"] == "REFUSED"


def test_dedupe_will_not_invent_an_original(tmp_path):
    copy = run_dir(tmp_path / "parked")
    with pytest.raises(FileNotFoundError):
        pv.dedupe(copy, tmp_path / "no-original-here")


# --- the module itself ------------------------------------------------------

def test_this_module_contains_no_path_that_removes_anything():
    """The strongest statement available about a tool whose whole point is not to
    delete: not "it is careful", but "there is no code that could".

    `shutil.move` and `os.rename` are moves. `rmtree`, `unlink`, `os.remove`,
    `rmdir` and `os.removedirs` are not, and none of them is here.
    """
    source = (ROOT / "tools" / "preserve.py").read_text(encoding="utf-8")
    for forbidden in ("rmtree", ".unlink(", "os.remove", "os.rmdir",
                      "removedirs", "shutil.rmtree"):
        # The names appear in this test and in prose; what matters is that they
        # are not called in the tool.
        assert f"{forbidden}" not in source.replace("`rmtree`", "").replace(
            "`unlink`", "").replace("`os.remove`", "").replace(
            "`rmdir` and `os.removedirs`", ""), forbidden


def test_the_command_returns_two_so_a_hook_can_block_on_it(tmp_path, capsys):
    """Exit 2 is what the PreToolUse guard on this machine treats as "block",
    so the tool is usable as the redirect the denylist is missing."""
    made = run_dir(tmp_path / "runs")
    assert pv.main(["check", "--", "rm", "-rf", str(made)]) == 2
    assert "protected" in capsys.readouterr().err
    assert pv.main(["check", "--", "ls", str(made)]) == 0


def test_classify_reports_rather_than_refusing(tmp_path, capsys):
    made = run_dir(tmp_path / "runs")
    assert pv.main(["classify", str(made)]) == 0
    assert "protected" in capsys.readouterr().out


# --- the policy a run claims to have run under ------------------------------

def test_the_policy_digest_covers_every_file_the_invariant_rests_on():
    """A run's evidence has to name a specific policy, or "this ran under the
    no-delete invariant" is a good intention rather than a claim. All three
    files are in this repository on purpose: the programme's correctness must not
    depend on machine-global configuration that other projects share."""
    found = pv.policy_digest(ROOT)
    assert found["complete"]
    assert set(found["files"]) == {"tools/preserve.py", "tools/preserve_hook.py",
                                   "policy/role_approval.default.json"}
    assert all(not v.startswith("ABSENT") for v in found["files"].values())


def test_a_changed_policy_changes_the_digest(tmp_path):
    """The negative control: if the digest did not move, recording it would prove
    nothing about which policy was in force."""
    for relative in pv.POLICY_FILES:
        spot = tmp_path / relative
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.write_text("original", encoding="utf-8")
    before = pv.policy_digest(tmp_path)["policy_digest"]
    (tmp_path / "tools" / "preserve.py").write_text("weakened", encoding="utf-8")
    assert pv.policy_digest(tmp_path)["policy_digest"] != before


def test_an_incomplete_policy_tree_cannot_claim_the_invariant(tmp_path):
    found = pv.policy_digest(tmp_path)
    assert not found["complete"]
    assert pv.main(["digest", "--root", str(tmp_path)]) == 2


def test_a_run_under_a_different_policy_than_the_one_claimed_is_refused(tmp_path):
    """Point 5 of the correction: fail closed when the run did not actually load
    the expected policy."""
    assert pv.main(["digest", "--root", str(ROOT), "--expect", "deadbeef"]) == 2
    good = pv.policy_digest(ROOT)["policy_digest"]
    assert pv.main(["digest", "--root", str(ROOT), "--expect", good]) == 0
