"""The capsule's commit rule: HEAD behind by records-only commits is not drift.

A capsule names the commit it describes; the commit that carries it, the closure
record, the regenerated board and the attribution entry that follows every commit
come after it. The old rule accepted exactly one commit of distance touching only
the capsule, so a successor read DRIFTED at the second commit of every closure and
learned to read past red. The rule is now: behind by commits that touched nothing
but `RECORD_PATHS` is VERIFIED, with every derived field still re-derived; anything
else that moved is drift, named. Exercised in scratch repositories, because the
cases are about what commits contain and this history is not a fixture.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "succession_window_probe", ROOT / "tools" / "succession.py")
su = importlib.util.module_from_spec(_spec)
sys.modules["succession_window_probe"] = su
_spec.loader.exec_module(su)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
CAPSULE_REL = "dogfood/succession/SUCCESSION.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True, env={**os.environ, **GIT_ENV}).stdout.strip()


def _board(reference: str, rows: str = "tests, export_sync") -> str:
    return ("# Readiness\n\n"
            f"Measured at `{reference}` on 2026-10-11.\n\n"
            "    TECHNICALLY_STABLE_READY = no\n\n"
            f"Open, and each one blocking: {rows}.\n\n| condition | state |\n")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def seed(root: Path) -> str:
    """A repository carrying every source a capsule field re-derives from, plus a
    copy of the tool (which finds its tree from its own path), committed once."""
    _write(root, "src/hoh/a.py", "x = 1\n")
    _write(root, "tools/t.py", "y = 1\n")
    _write(root, "tests/test_a.py", "def test_a(): pass\n")
    _write(root, "docs/READINESS.md", _board("abc1234"))
    _write(root, "dogfood/export-sync/EXPORT_SYNC_STATE.json",
           json.dumps({"synced_export_commit": "e" * 40, "path_digests": {"a": "1"}}))
    _write(root, ".github/releases/v0.1.0.json", json.dumps(
        {"source_tag": "v0.1.0", "tag_object": "f" * 40, "source_commit": "a" * 40,
         "sha256": {"w.whl": "0" * 64}}))
    _write(root, "runs/r1/state.json", json.dumps({"stage": "DONE", "condition": "ACCEPTED"}))
    _write(root, "DOGFOOD_LEDGER.md",
           "# ledger\n\n## O1 · one\n\n**Getracktes Todo, Priorität: hoch.** later\n")
    _write(root, "README.md", "readme\n")
    _write(root, "plan/01_goal.md", "one\n")
    _write(root, ".gitignore", "runs/\n__pycache__/\n")
    (root / "tools").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "tools" / "succession.py", root / "tools" / "succession.py")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seal")
    return _git(root, "rev-parse", "HEAD")


class Repo:
    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.seal = seed(root)
        monkeypatch.setattr(su, "HOH", root)
        monkeypatch.setattr(su, "CAPSULE", root / CAPSULE_REL)
        monkeypatch.setenv(su.PLAN_ENV, str(root / "plan"))
        assert su.write_capsule(root / "plan", su.CAPSULE) == 0
        self.capsule = json.loads(su.CAPSULE.read_text())
        assert self.capsule["internal_commit"] == self.seal
        assert self.capsule["internal_tree_clean"] is True
        self.commit("capsule", CAPSULE_REL)

    def commit(self, message: str, *paths: str, content: str = "x\n") -> str:
        for rel in paths:
            if not (self.root / rel).exists():
                _write(self.root, rel, content)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return _git(self.root, "rev-parse", "HEAD")

    def rows(self) -> dict[str, tuple[str, str]]:
        return {f: (v, d) for f, v, d in su.check(self.capsule, None)}

    def verify_cli(self) -> tuple[int, str]:
        done = subprocess.run([sys.executable, "-B", str(self.root / "tools/succession.py"),
                               "verify"], capture_output=True, text=True, cwd=self.root,
                              env={"PATH": os.environ.get("PATH", ""),
                                   su.PLAN_ENV: str(self.root / "plan")})
        return done.returncode, done.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path / "repo", monkeypatch)


RECORD_COMMITS = [
    ("closure record", ("DOGFOOD_LEDGER.md",)),
    ("preserved review", ("dogfood/p2-gate/20261011T120000Z/findings.json",)),
    ("board and docs", ("docs/LIMITATIONS.md",)),
    ("public figures", ("paper/NUMBERS_V2.md", "README.md", "CLAIMS.md", "CLAIMS.json",
                        "EXPORT_MANIFEST.json")),
    ("regenerated artefacts", ("program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json",
                               "program/v3_3/VERIHARNESS_IMPLEMENTATION_DAG.json",
                               "program/v3_3/PROGRAMME_DISPOSITIONS.md")),
    ("handoff note", ("DOGFOOD_HANDOFF.md",)),
    ("release documents", ("CHANGELOG.md", "RELEASE_NOTES.md", "CITATION.cff",
                           ".github/releases/v0.3.0.json")),
    ("attribution", ("dogfood/ATTRIBUTION.json",)),
]


def test_the_capsule_commit_and_records_only_commits_behind_head_are_not_drift(repo):
    """The positive control, and the closure's own shape: the capsule, then the
    record, the preserved review, the regenerated documents and figures, the
    attribution entry -- VERIFIED at every step, exit 0 at the command line."""
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.OK and "1 records-only commit(s)" in detail, detail
    for n, (message, paths) in enumerate(RECORD_COMMITS, start=2):
        if paths == ("DOGFOOD_LEDGER.md",):
            with (repo.root / "DOGFOOD_LEDGER.md").open("a", encoding="utf-8") as f:
                f.write("\n## O2 · closure record\n\nP2 closed at this commit.\n")
        repo.commit(message, *paths)
        rows = repo.rows()
        drifted = {f: d for f, (v, d) in rows.items() if v == su.DRIFT}
        assert not drifted, (message, drifted)
        verdict, detail = rows["internal_commit"]
        assert verdict == su.OK and f"{n} records-only commit(s)" in detail, (message, detail)
        assert repo.seal[:12] in detail
    assert {v for v, _ in repo.rows().values()} == {su.OK}, repo.rows()
    code, out = repo.verify_cli()
    assert code == 0, out
    assert "VERIFIED: every field re-derives from this tree." in out


@pytest.mark.parametrize("path", [
    "src/hoh/b.py", "tools/u.py", "tests/test_b.py", "ops/hoh-continue.service",
    "policy/deny.txt", "pyproject.toml", "flake.nix", "plugin/x.toml",
    "program/v3_3/REQUIREMENTS.json", "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json",
    "program/v3_3/PROGRAMME_AMENDMENTS.json", "program/v3_3/amendments/A9_X.md",
    "program/v3_3/VERIHARNESS_X/inner.json", "program/v3_3/veriharness_x.json",
    "program/v3_3/other.json", "dogfoodx/a.md", "Docs/a.md", "docsx", "README.md.bak",
    "src/dogfood/a.md", ".github/workflows/x.yml", ".archiv/old.v1.json", "newtop.txt",
    "DOGFOOD_X/inner.md", "papers/a.md", "CLAIMS.yaml",
])
def test_a_commit_that_moves_anything_but_a_record_is_drift(repo, path):
    """One records commit first, so the window exists; then one commit that adds
    `path`. Drift, naming the path, and the distance."""
    repo.commit("attribution", "dogfood/ATTRIBUTION.json")
    repo.commit("something else", path)
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.DRIFT, (path, detail)
    assert "moved 3 commit(s)" in detail and f"{path} is not a record path" in detail, detail
    code, out = repo.verify_cli()
    assert code == 1 and "DRIFTED" in out and path in out, out


def test_removing_or_renaming_a_source_file_is_drift(repo):
    """A diff lists what went away, too -- and a rename is read as a deletion plus
    an addition (`--no-renames`), so the source path is named either way."""
    _git(repo.root, "rm", "-q", "src/hoh/a.py")
    _git(repo.root, "commit", "-qm", "remove a source file")
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.DRIFT and "src/hoh/a.py is not a record path" in detail, detail
    _git(repo.root, "mv", "tools/t.py", "tools/t2.py")
    _git(repo.root, "commit", "-qm", "rename a tool")
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.DRIFT and "tools/t.py" in detail and "tools/t2.py" in detail, detail


@pytest.mark.parametrize("source, destination", [
    ("src/hoh/a.py", "docs/a.py"), ("src/hoh/a.py", "docs/a.md"), ("tools/t.py", "dogfood/t.py"),
    ("tools/t.py", "dogfood/notes/t.txt"), ("tests/test_a.py", "paper/test_a.py"),
])
def test_a_source_file_moved_into_a_record_path_is_drift_naming_the_source(repo, source, destination):
    """The reviewers' blocker: with rename detection, `git mv src/hoh/a.py docs/a.py`
    printed only `docs/a.py` and the move read as records-only. The diff is taken
    without rename folding, so the source path is a deletion and is named."""
    repo.commit("attribution", "dogfood/ATTRIBUTION.json")
    (repo.root / destination).parent.mkdir(parents=True, exist_ok=True)
    _git(repo.root, "mv", source, destination)
    _git(repo.root, "commit", "-qm", f"move {source} into a record directory")
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.DRIFT and source in detail, (source, destination, detail)
    code, out = repo.verify_cli()
    assert code == 1 and source in out, out


def test_a_failed_git_command_is_a_gap_and_never_a_pass(repo):
    """The other blocker: `_git` answered "" for a failed command, which the window
    rule read as "nothing changed" and the tree row as "clean". A broken diff config
    makes `git diff` fail (exit 128) over a source commit; a broken status config
    makes `git status` fail. Each is an ENVIRONMENT_GAP, and the command line does
    not exit 0."""
    repo.commit("a source change", "src/hoh/b.py")
    assert repo.rows()["internal_commit"][0] == su.DRIFT
    _git(repo.root, "config", "diff.renames", "bogus")
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.GAP and "diff exit" in detail, detail
    code, out = repo.verify_cli()
    assert code != 0 and "VERIFIED: every field" not in out, out
    _git(repo.root, "config", "--unset", "diff.renames")
    assert repo.rows()["internal_commit"][0] == su.DRIFT

    _git(repo.root, "config", "status.showUntrackedFiles", "bogus")
    verdict, detail = repo.rows()["tree"]
    assert verdict == su.GAP and "git status failed" in detail, detail
    # And `write` refuses rather than record a cleanness it did not measure.
    assert su.write_capsule(repo.root / "plan", repo.root / "elsewhere.json") == 2
    assert not (repo.root / "elsewhere.json").exists()
    _git(repo.root, "config", "--unset", "status.showUntrackedFiles")
    assert repo.rows()["tree"][0] == su.OK


def test_the_tree_row_ignores_dirt_on_record_paths_and_not_on_the_ground(repo):
    """The records entry after the capsule regenerates the DAG, the board and the
    other generated documents between two passes (`regenerate.py` writes the build
    plan before the readiness board), so a tree dirty only in record paths must
    still verify; a modified source file, a module under `dogfood/` or a changed
    register must not."""
    capsule_commit = _git(repo.root, "rev-parse", "HEAD")
    _write(repo.root, "program/v3_3/VERIHARNESS_IMPLEMENTATION_DAG.json", "{}\n")
    _write(repo.root, "program/v3_3/VERIHARNESS_BUILD_CHECKLIST.md", "x\n")
    (repo.root / "docs/READINESS.md").write_text(_board(capsule_commit[:7]), encoding="utf-8")
    _write(repo.root, "dogfood/p2-gate/CHECKLIST_SNAPSHOT.json", "{}\n")
    rows = repo.rows()
    assert rows["tree"] == (su.OK, "clean outside the record paths"), rows["tree"]
    assert rows["board"][0] == su.OK and rows["internal_commit"][0] == su.OK, rows
    assert repo.verify_cli()[0] == 0
    repo.commit("the figures")
    assert {v for v, _ in repo.rows().values()} == {su.OK}

    for ground, text in (("dogfood/new/module.py", "x = 1\n"), ("docs/helper.sh", "true\n"),
                         ("program/v3_3/REQUIREMENTS.json", "{}\n"), ("newtop.txt", "x\n")):
        _write(repo.root, ground, text)
        verdict, detail = repo.rows()["tree"]
        assert verdict == su.DRIFT and ground in detail, (ground, detail)
        (repo.root / ground).rename(repo.root.parent / ground.replace("/", "_"))
    (repo.root / "src/hoh/a.py").write_text("x = 2\n")
    verdict, detail = repo.rows()["tree"]
    assert verdict == su.DRIFT and "src/hoh/a.py" in detail, detail
    (repo.root / "src/hoh/a.py").write_text("x = 1\n")
    assert repo.rows()["tree"][0] == su.OK


@pytest.mark.parametrize("shape", [
    "HEAD", "HEAD~0", "HEAD^{commit}", "@", "branch", "refs/heads/branch", "short12", "UPPER40",
    "sha-with-newline", "sha-with-space", "tag-name", "tag-object-id", "integer", "none",
])
def test_a_commit_that_is_not_a_full_commit_id_is_drift_never_a_pass(repo, shape):
    """Integration reviews of the first candidate (B1/F1): a capsule naming `HEAD`
    (or a branch, `HEAD~0`, an abbreviated id) resolved to the current tip every
    time, so a committed capsule never drifted and the gate was defeated. Only a
    full lower-case 40-hex commit id that peels to itself is a commit here; every
    other spelling is DRIFT before and after a source change, and the command
    line exits 1. The real id is the control: OK before, DRIFT after."""
    sha = repo.seal
    branch = _git(repo.root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo.root, "tag", "-a", "v-probe", "-m", "a tag object", sha)
    value = {
        "HEAD": "HEAD", "HEAD~0": "HEAD~0", "HEAD^{commit}": "HEAD^{commit}", "@": "@",
        "branch": branch, "refs/heads/branch": f"refs/heads/{branch}", "short12": sha[:12],
        "UPPER40": sha.upper(), "sha-with-newline": sha + "\n", "sha-with-space": " " + sha,
        "tag-name": "v-probe", "tag-object-id": _git(repo.root, "rev-parse", "v-probe"),
        "integer": 12345678901234567890123456789012345678, "none": None,
    }[shape]
    forged = json.loads(json.dumps(repo.capsule))
    forged["internal_commit"] = value
    assert {f: v for f, v, _ in su.check(repo.capsule, None)}["internal_commit"] == su.OK
    verdict, detail = {f: (v, d) for f, v, d in su.check(forged, None)}["internal_commit"]
    assert verdict == su.DRIFT, (shape, verdict, detail)
    assert "40-hex commit id is required" in detail or "peels to" in detail or "names no commit" in detail, detail

    repo.commit("a source change", "src/hoh/b.py")
    verdict, detail = {f: (v, d) for f, v, d in su.check(forged, None)}["internal_commit"]
    assert verdict == su.DRIFT, (shape, verdict, detail)
    assert {f: v for f, v, _ in su.check(repo.capsule, None)}["internal_commit"] == su.DRIFT
    # The command line, with the forged capsule read from outside the tree.
    path = repo.root.parent / f"forged-{shape.replace('/', '_')}.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    done = subprocess.run([sys.executable, "-B", str(repo.root / "tools/succession.py"), "verify",
                           "--capsule", str(path)], capture_output=True, text=True, cwd=repo.root,
                          env={"PATH": os.environ.get("PATH", ""), su.PLAN_ENV: str(repo.root / "plan")})
    assert done.returncode == 1 and "DRIFTED" in done.stdout and "internal_commit" in done.stdout, done.stdout


def test_write_records_the_full_commit_id(repo):
    """What `write` records is what `verify` requires, by construction."""
    assert su._COMMIT_ID.fullmatch(repo.capsule["internal_commit"])
    assert repo.capsule["internal_commit"] == repo.seal


def test_write_refuses_when_head_is_not_a_full_commit_id(tmp_path, monkeypatch):
    """The refusal in `write` is reachable: in a repository whose object format is
    sha256, `rev-parse HEAD` is 64 hex, not an id `verify` would accept. `write`
    exits 2 and writes nothing, instead of recording an id its own verifier would
    read as drift. Skips only where this git cannot make such a repository."""
    root = tmp_path / "sha256repo"
    done = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(root)],
                          capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if done.returncode != 0:
        pytest.skip(f"this git cannot init a sha256 repository: {done.stderr.strip()[:80]}")
    (root / "README.md").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seal")
    head = _git(root, "rev-parse", "HEAD")
    assert len(head) == 64 and not su._COMMIT_ID.fullmatch(head)
    monkeypatch.setattr(su, "HOH", root)
    monkeypatch.setattr(su, "CAPSULE", root / CAPSULE_REL)
    out = root / "elsewhere.json"
    assert su.write_capsule(None, out) == 2
    assert not out.exists()
    assert not (root / CAPSULE_REL).exists()


@pytest.mark.parametrize("root_value", [5, [], {}, "", None, 0.5, True])
def test_a_non_string_recorded_plan_root_is_never_a_traceback(repo, root_value):
    """Review A, N1: `recorded = Path(plan_then["root"])` was computed before it was
    needed, so a capsule with a non-string `plan.root` and an explicit `--plan-root`
    ended in a TypeError on the prototype where the base printed VERIFIED. The
    recorded root is read only when used and only as a non-empty string: with an
    explicit root the row is judged against that root; without one, at the command
    line, the row is a gap; in gate mode the recorded value is never touched."""
    forged = json.loads(json.dumps(repo.capsule))
    forged["plan"] = {"root": root_value, "files": forged["plan"]["files"]}
    with_root = {f: (v, d) for f, v, d in su.check(forged, repo.root / "plan")}
    assert with_root["plan"][0] == su.OK, with_root["plan"]
    without = {f: (v, d) for f, v, d in su.check(forged, None)}
    assert without["plan"][0] == su.GAP and su.PLAN_ENV in without["plan"][1], without["plan"]
    gate = {f: (v, d) for f, v, d in su.check(forged, None, at_commit="HEAD")}
    assert gate["plan"][0] == su.GAP and su.PLAN_ENV in gate["plan"][1], gate["plan"]
    # The command line with --plan-root, as the reviewer ran it.
    path = repo.root.parent / "forged-root.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    done = subprocess.run([sys.executable, "-B", str(repo.root / "tools/succession.py"), "--plan-root",
                           str(repo.root / "plan"), "verify", "--capsule", str(path)],
                          capture_output=True, text=True, cwd=repo.root,
                          env={"PATH": os.environ.get("PATH", "")})
    assert "Traceback" not in done.stderr and done.returncode in (0, 1, 3), done.stderr[-300:]
    assert "plan" in done.stdout and "file(s) unchanged" in done.stdout, done.stdout


def test_a_commit_not_on_this_line_is_drift(repo):
    """A commit of this repository that HEAD does not descend from describes a
    tree this one is not on -- even with an identical tree. Made with
    `commit-tree`, so no checkout is needed."""
    tree = _git(repo.root, "rev-parse", "HEAD^{tree}")
    side = _git(repo.root, "commit-tree", tree, "-p", repo.seal, "-m", "side")
    repo.capsule["internal_commit"] = side
    verdict, detail = repo.rows()["internal_commit"]
    assert verdict == su.DRIFT and "not an ancestor of HEAD" in detail, detail


def test_a_tree_that_is_not_a_repository_of_its_own_is_a_gap(repo, monkeypatch):
    """`git -C` walks up to the first `.git` above the tree: an archive copy under
    another repository would be judged by that repository's HEAD and status. That
    is an environment gap, named with the repository git answered for, and
    neither the commit nor the tree row is derived."""
    inner = repo.root / "inner"
    inner.mkdir()
    (inner / "dogfood/succession").mkdir(parents=True)
    shutil.copyfile(repo.root / CAPSULE_REL, inner / CAPSULE_REL)
    monkeypatch.setattr(su, "HOH", inner)
    monkeypatch.setattr(su, "CAPSULE", inner / CAPSULE_REL)
    rows = repo.rows()
    for field in ("internal_commit", "tree"):
        verdict, detail = rows[field]
        assert verdict == su.GAP, (field, verdict, detail)
        assert "not a git repository of its own" in detail and str(repo.root) in detail, detail


def test_the_board_re_measured_inside_the_window_is_not_drift(repo):
    """The records entry after the capsule regenerates the board, which names the
    commit it was measured at. Re-measured at one of the records-only commits
    behind HEAD, saying the same thing, it is not drift; a reference commit from
    anywhere else, or a board that says something else, still is."""
    capsule_commit = _git(repo.root, "rev-parse", "HEAD")
    (repo.root / "docs/READINESS.md").write_text(_board(capsule_commit[:7]), encoding="utf-8")
    repo.commit("board regenerated at the capsule commit")
    verdict, detail = repo.rows()["board"]
    assert verdict == su.OK and f"re-measured at {capsule_commit[:7]}" in detail, detail
    assert "records-only commit behind HEAD" in detail

    tree = _git(repo.root, "rev-parse", "HEAD^{tree}")
    outside = _git(repo.root, "commit-tree", tree, "-p", repo.seal, "-m", "outside")
    for reference in (outside[:7], "zzzzzzz", repo.seal[:7] + "0"):
        (repo.root / "docs/READINESS.md").write_text(_board(reference), encoding="utf-8")
        repo.commit(f"board claiming {reference}")
        verdict, detail = repo.rows()["board"]
        assert verdict == su.DRIFT and "reference_commit" in detail, (reference, detail)

    head = _git(repo.root, "rev-parse", "HEAD")
    (repo.root / "docs/READINESS.md").write_text(
        _board(head[:7], "tests, export_sync, lint"), encoding="utf-8")
    repo.commit("board re-measured and saying something else")
    verdict, detail = repo.rows()["board"]
    assert verdict == su.DRIFT and "open_rows_excluding_self" in detail, detail


def test_an_unchanged_board_is_still_compared_when_nothing_moved(repo):
    """The window rule adds a tolerance for one key; it removes no comparison."""
    before = repo.rows()["board"]
    assert before[0] == su.OK and "identically" in before[1]
    k = json.loads(json.dumps(repo.capsule))
    k["board"]["verdict_excluding_self"] = "not-a-value-this-tree-can-derive"
    assert {f: v for f, v, _ in su.check(k, None)}["board"] == su.DRIFT


def test_the_record_paths_are_an_explicit_list_and_the_ground_is_not_on_it():
    """Fail closed: only the spellings git prints for the listed paths; the capsule
    itself is one; nothing a matrix row or a capsule field derives from as ground."""
    assert su._is_record(CAPSULE_REL)
    assert str(su.CAPSULE.relative_to(su.HOH)) == CAPSULE_REL
    for record in ("dogfood/ATTRIBUTION.json", "dogfood/p2-gate/REVIEW.json",
                   "docs/READINESS.md", "DOGFOOD_LEDGER.md", "DOGFOOD_SUCCESSION.md",
                   "README.md", "CLAIMS.md", "CLAIMS.json", "EXPORT_MANIFEST.json",
                   "paper/NUMBERS_V2.md", "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json",
                   "program/v3_3/VERIHARNESS_BUILD_CHECKLIST.md",
                   "program/v3_3/PROGRAMME_DISPOSITIONS.md", "CHANGELOG.md", "RELEASE_NOTES.md",
                   "RELEASE_NOTES_v0.3.0.md", "CITATION.cff", ".github/releases/v0.3.0.json",
                   "dogfood/gb/fix.diff", "dogfood/benchmark/tasks/t1/README.md"):
        assert su._is_record(record), record
    # Executed code is ground wherever it sits, and so is the one hand-written
    # document under the generated-artefact prefix.
    for code in ("dogfood/benchmark/reference/solve.py", "dogfood/launch_self_hosted.sh",
                 "dogfood/x/hidden_test.py", "docs/helper.py", "paper/make.sh",
                 "program/v3_3/VERIHARNESS_STATE_OWNERSHIP_ADR.md",
                 ".github/releases/sub/x.json", "CHANGELOG/x.md", "RELEASE_NOTES.md/x",
                 "dogfood/\x01odd.md", "dogfood/\udcff.md",
                 # review round 2: case, other executable and configuring types,
                 # extensionless scripts, archived copies of code
                 "docs/x.PY", "dogfood/x.pyw", "dogfood/x.pth", "docs/x.bash", "dogfood/pytest.ini",
                 "dogfood/benchmark/tox.ini", "dogfood/run", "dogfood/x.toml", "dogfood/x.service",
                 "dogfood/x.js", "dogfood/Makefile", "dogfood/drafts/proofclass.py.v1.20260925T174038Z",
                 "dogfood/reference/cli.py.diff.txt", "dogfood/x.unknownsuffix",
                 "program/v3_3/VERIHARNESS_X.py", "DOGFOOD_X.sh", ".github/releases/x.py"):
        assert not su._is_record(code), code
    # Data under a record directory: evidence as the records entries write it.
    for data in ("dogfood/x/.process.lock", "dogfood/x/.lock", "dogfood/x/.hoh-out-run.log",
                 "dogfood/x/server.stderr", "dogfood/x/drive.console", "dogfood/x/log.1",
                 "dogfood/x/state.json.v2026-09-25T10-31-49Z-001",
                 "dogfood/x/i1.json.dispatch.json.v2026-10-01T10:26:27Z",
                 "dogfood/x/REVIEW.v1.20260924T014958Z.json", "dogfood/x/NOTES.md.v1.20261011T120000Z",
                 "dogfood/x/evidence.bundle", "dogfood/x/README", "docs/X.MD", "paper/figure.svg"):
        assert su._is_record(data), data
    assert "py" in su.EXEC_SUFFIXES and "ini" in su.EXEC_SUFFIXES and "md" in su.DATA_SUFFIXES
    for ground in ("src/hoh/x.py", "tools/succession.py", "tests/test_x.py",
                   "ops/hoh-continue.service", "policy/x", "pyproject.toml", "flake.nix",
                   "program/v3_3/REQUIREMENTS.json",
                   "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json",
                   "program/v3_3/PROGRAMME_AMENDMENTS.json",
                   "program/v3_3/amendments/A7_PHASE_CLOSURE_AT_COMMIT.md",
                   "program/v3_3/VERIHARNESS_X/y.json", "program/v3_3/VERIHARNESS_",
                   "DOGFOOD_X/y.md", "DOGFOOD_", "Dogfood/x", "dogfood", "docs", "/docs/x.md",
                   "docs/../src/x.py", '"docs/\\303\\244.md"', "", "README.md ", " README.md",
                   "README.MD", ".archiv/x.v1", "runs/r/state.json"):
        assert not su._is_record(ground), ground
    assert su.RECORD_PATHS == tuple(su.RECORD_PATHS), "a tuple, pinned"
