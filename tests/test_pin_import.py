"""`hoh pin` and `hoh import` (Amendment A6, increment I6, requirement row P5-38).

The orchestrator made a spec-only pin commit with a shell script and imported every accepted candidate by hand. These
tests run the product's commands on real git repositories made under `tmp_path` and check what the script and the
hand-typed steps let through: a short id or a ref name typed as the base, a branch or an index that moved, a pin that
replaced a file of the base or moved an existing pin, a spec that carried a home path, a scratch path or an invisible
character, an import whose tree is not the accepted one, a merge preview nobody made.

Every test builds its own repositories and files; nothing starts Herdr or an agent (a `herdr` stub first on `PATH`
logs every call, and the log must stay empty), nothing reads a real run root, `~` or the host's `/tmp`, and the only
paths written are under `tmp_path`. The modules are imported inside the tests, so a tree without them fails each test
on its own.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOME = "/" + "home" + "/tester"   # assembled at run time: a literal home path in an exported file is a leak-scan finding (records O265)
SPEC = "dogfood/zz/spec.md"
CLEAN_SPEC = "# Spec: a thing\n\nGoal: do it. See ~/hoh-state (read-only) and /tmp for the sandbox. e-acute: é → ok\n"


@pytest.fixture(autouse=True)
def _quiet_world(tmp_path, monkeypatch):
    """A fixed home, a git that reads no configuration, and a `herdr` that only logs its call."""
    monkeypatch.setenv("HOME", HOME)
    for key in list(os.environ):
        if key.startswith("GIT_") and key != "GIT_EXEC_PATH":
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    log = tmp_path / "herdr-calls.log"
    stub = stub_dir / "herdr"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 99\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    yield
    assert not log.exists(), "a test started Herdr"


def mod(name: str):
    return importlib.import_module(f"hoh.{name}")


# --- repositories -------------------------------------------------------------------------------


IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(repo, *args, stdin=None, check=True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, input=stdin,
                          env=dict(os.environ, **IDENT), check=False)
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


def commit(repo, files: dict[str, str], message: str = "c") -> str:
    for rel, text in files.items():
        path = Path(repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path, name="integ"):
    """A repository with two commits on `main`; returns `(path, first, second)`."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    first = commit(repo, {"a.txt": "one\ntwo\nthree\n", "b.txt": "b\n", "c.txt": "c\n", "dogfood/old/spec.md": "old\n"})
    second = commit(repo, {"d.txt": "d\n"})
    return repo, first, second


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def pin_args(tmp_path, repo, base, *, files=None, name="p1", spec=SPEC, message="Pin it.\n", co="Claude Opus 5.5 <noreply@example.invalid>",
             allow=(), receipt=None, root_name="files"):
    root = write_tree(tmp_path / root_name, files if files is not None else {SPEC: CLEAN_SPEC, "dogfood/zz/reference/x.txt": "x\n"})
    msg = tmp_path / ("msg-" + "".join(c if c.isalnum() else "_" for c in name) + ".txt")
    msg.write_text(message, encoding="utf-8")
    return dict(repo=str(repo), base=base, files_root=str(root), spec=spec, name=name, message_file=str(msg),
                co_author=co, receipt=str(receipt or tmp_path / f"receipt-{name}.json"), allow=list(allow))


def snapshot(repo: Path) -> dict:
    repo = Path(repo)
    worktree = {str(p.relative_to(repo)): p.read_bytes() for p in sorted(repo.rglob("*"))
                if p.is_file() and ".git" not in p.relative_to(repo).parts}
    index = repo / ".git" / "index"
    return {"head": (repo / ".git" / "HEAD").read_text(), "heads": git(repo, "for-each-ref", "refs/heads", "refs/tags"),
            "index": hashlib.sha256(index.read_bytes()).hexdigest(), "index_ns": index.stat().st_mtime_ns,
            "status": git(repo, "status", "--porcelain=v1", "-uall"), "worktree": worktree,
            "locks": sorted(p.name for p in (repo / ".git").glob("*.lock"))}


def install_hook(repo, tmp_path):
    """A `reference-transaction` hook in the repository that logs one line per run; returns a function that counts them.
    The control (our own git runs it) proves the hook would fire for the commands under test."""
    log = tmp_path / "hook.log"
    hook = Path(repo) / ".git" / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho "$1" >> {log}\n')
    hook.chmod(0o755)
    git(repo, "update-ref", "refs/control/hook", git(repo, "rev-parse", "HEAD"))
    count = lambda: len(log.read_text().splitlines()) if log.exists() else 0   # noqa: E731
    assert count() > 0, "the control: the hook does not fire for plain git"
    return count


def refs(repo) -> str:
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


def loose_objects(repo) -> int:
    return int(next(line.split()[1] for line in git(repo, "count-objects", "-v").splitlines() if line.startswith("count:")))


# --- the resolver: one place for the family -----------------------------------------------------


def test_a_commit_is_a_full_id_the_product_resolved_and_nothing_else(tmp_path):
    cr = mod("commitref")
    repo, first, second = make_repo(tmp_path)
    git(repo, "tag", "light", first)
    git(repo, "tag", "-a", "-m", "annotated", "ann", first)
    annotated = git(repo, "rev-parse", "ann")
    tree = git(repo, "rev-parse", first + "^{tree}")
    assert cr.resolve_commit(str(repo), "--base", first) == first
    assert cr.head_commit(str(repo)) == second
    for typed, words in [(first[:12], "not a full commit id"), (first.upper(), "not a full commit id"),
                         ("main", "not a full commit id"), ("HEAD", "not a full commit id"), ("light", "not a full commit id"),
                         (first + "~0", "not a full commit id"), ("", "not a full commit id"), (None, "not a full commit id"),
                         ("f" * 40, "is not a commit"), (tree, "is not a commit"), (annotated, "peels to")]:
        with pytest.raises(cr.CommitRefused) as caught:
            cr.resolve_commit(str(repo), "--base", typed)
        assert words in str(caught.value), (typed, str(caught.value))
    # the family imports the one resolver and the one git runner, it keeps no copy of them
    for name in ("pin", "importref"):
        module = mod(name)
        assert module.resolve_commit is cr.resolve_commit and module.git is cr.git
    assert cr.FULL_ID.pattern == "[0-9a-f]{40}|[0-9a-f]{64}"


# --- hoh pin ------------------------------------------------------------------------------------


def test_a_pin_needs_a_full_base_id_and_a_refusal_creates_nothing(tmp_path):
    pin = mod("pin")
    repo, first, second = make_repo(tmp_path)
    git(repo, "tag", "light", first)
    git(repo, "tag", "-a", "-m", "annotated", "ann", first)
    annotated = git(repo, "rev-parse", "ann")
    before_refs, before_loose = refs(repo), loose_objects(repo)
    for n, typed in enumerate([first[:12], first.upper(), "main", "HEAD", "light", "ann", annotated, "f" * 40, ""]):
        args = pin_args(tmp_path, repo, typed, name=f"r{n}")
        out = pin.run_pin(**args)
        assert out.code == 2 and out.out == "", (typed, out)
        assert out.err.startswith("pin refused: ") and out.err.count("\n") == 1, out.err
        assert not Path(args["receipt"]).exists()
    assert refs(repo) == before_refs and loose_objects(repo) == before_loose
    ok = pin.run_pin(**pin_args(tmp_path, repo, first, name="good"))
    assert ok.code == 0, ok
    assert git(repo, "rev-parse", "refs/pins/good^") == first


def test_a_pin_is_plumbing_only_and_touches_no_branch_index_or_worktree(tmp_path):
    pin = mod("pin")
    repo, first, second = make_repo(tmp_path)
    (repo / "a.txt").write_text("staged\n")
    git(repo, "add", "a.txt")
    (repo / "b.txt").write_text("unstaged\n")
    (repo / "junk.txt").write_text("untracked\n")
    hook_runs = install_hook(repo, tmp_path)
    runs_before = hook_runs()
    before = snapshot(repo)
    before_refs = refs(repo)
    args = pin_args(tmp_path, repo, first)
    out = pin.run_pin(**args)
    assert out.code == 0 and out.err == "", out
    assert hook_runs() == runs_before, "the product's git ran a hook of the repository"
    assert snapshot(repo) == before, "a branch, HEAD, the index or the working tree changed"
    receipt = json.loads(Path(args["receipt"]).read_text())
    new_refs = set(refs(repo).splitlines()) - set(before_refs.splitlines())
    assert new_refs == {f"refs/pins/p1 {receipt['pin']}"}
    pin_id = receipt["pin"]
    # every entry of the base is carried over unchanged (the same tree, plus the two added files), also in a directory it extends
    assert git(repo, "ls-tree", "-r", first) in git(repo, "ls-tree", "-r", pin_id)
    assert git(repo, "rev-parse", pin_id + "^") == first != second, "the pin is built on the base, not on HEAD"
    assert git(repo, "rev-parse", pin_id + "^{tree}") == receipt["tree"]
    changes = git(repo, "diff-tree", "-r", "--no-renames", "--name-status", first, pin_id).splitlines()
    assert sorted(changes) == sorted(["A\tdogfood/zz/reference/x.txt", "A\tdogfood/zz/spec.md"])
    assert git(repo, "ls-tree", "-r", pin_id, "--", "dogfood/zz").count("100644 blob") == 2
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", pin_id) == "hoh <hoh@localhost>|hoh <hoh@localhost>"
    assert out.out.splitlines()[0] == f"PIN: {pin_id}"


def test_a_pin_ref_is_created_never_moved_and_the_receipt_is_create_only(tmp_path):
    pin = mod("pin")
    repo, first, second = make_repo(tmp_path)
    one = pin.run_pin(**pin_args(tmp_path, repo, first, name="p1"))
    assert one.code == 0
    held = git(repo, "rev-parse", "refs/pins/p1")
    # the same name again, from another base: refused, the ref stays where it is
    again = pin.run_pin(**pin_args(tmp_path, repo, second, name="p1", receipt=tmp_path / "other.json", root_name="files2"))
    assert again.code == 2 and "exists" in again.err and git(repo, "rev-parse", "refs/pins/p1") == held
    assert not (tmp_path / "other.json").exists()
    # an existing receipt is never replaced and nothing is published before that is known
    taken = tmp_path / "taken.json"
    taken.write_text("mine\n")
    before = refs(repo)
    refused = pin.run_pin(**pin_args(tmp_path, repo, first, name="p2", receipt=taken, root_name="files3"))
    assert refused.code == 2 and taken.read_text() == "mine\n" and refs(repo) == before
    missing_dir = pin.run_pin(**pin_args(tmp_path, repo, first, name="p3", receipt=tmp_path / "no" / "r.json", root_name="files4"))
    assert missing_dir.code == 2 and refs(repo) == before
    for bad in ["", "a b", "../x", "a..b", "x.lock", "-x", ".x", "a/b", "x" * 65]:
        out = pin.run_pin(**pin_args(tmp_path, repo, first, name=bad, root_name="files5"))
        assert out.code == 2 and refs(repo) == before, bad
    # the ref is created only if absent at the moment of creation too: a pin of that name made in between stays
    # as it is, and the late command makes no pin
    real = pin.git

    def racing(repo_, *a, **kw):
        if a and a[0] == "update-ref":
            real(repo_, "update-ref", a[1], first, "")
        return real(repo_, *a, **kw)
    pin.git = racing
    try:
        raced = pin.run_pin(**pin_args(tmp_path, repo, second, name="p5", receipt=tmp_path / "raced.json", root_name="files7"))
    finally:
        pin.git = real
    assert raced.code == 2 and git(repo, "rev-parse", "refs/pins/p5") == first and not (tmp_path / "raced.json").exists()
    # a ref under the name (refs/pins/NAME/x) would make the creation fail after the objects were written: refused up front
    git(repo, "update-ref", "refs/pins/dir/x", first)
    before_df, loose_df = refs(repo), loose_objects(repo)
    clash = pin.run_pin(**pin_args(tmp_path, repo, first, name="dir", receipt=tmp_path / "df.json", root_name="files8"))
    assert clash.code == 2 and "collide" in clash.err and refs(repo) == before_df and loose_objects(repo) == loose_df
    assert not (tmp_path / "df.json").exists()
    # a receipt whose read-back differs from what was written is reported, not trusted (exit 3)
    real_create = pin.durable_create
    pin.durable_create = lambda path, payload, **kw: real_create(path, "tampered\n", **kw)
    try:
        altered = pin.run_pin(**pin_args(tmp_path, repo, first, name="p6", receipt=tmp_path / "alt.json", root_name="files9"))
    finally:
        pin.durable_create = real_create
    assert altered.code == 3 and "read back" in altered.err and "PIN: " in altered.out
    # a receipt that cannot be written after the ref exists is exit 3 with the pin named
    def broken(path, payload, **kw):
        raise OSError(5, "io error")
    original = pin.durable_create
    pin.durable_create = broken
    try:
        late = pin.run_pin(**pin_args(tmp_path, repo, first, name="p4", root_name="files6"))
    finally:
        pin.durable_create = original
    assert late.code == 3 and "PIN: " in late.out and "NOT written" in late.err
    assert git(repo, "rev-parse", "--verify", "refs/pins/p4")


def test_a_pin_only_adds_bounded_regular_files_at_plain_paths(tmp_path):
    pin = mod("pin")
    repo, first, second = make_repo(tmp_path)
    before_refs, before_loose = refs(repo), loose_objects(repo)

    def refused(n, files=None, **kw):
        root = tmp_path / f"root{n}"
        root.mkdir(parents=True, exist_ok=True)
        write_tree(root, files if files is not None else {SPEC: CLEAN_SPEC})
        for hook in kw.pop("hooks", []):
            hook(root)
        args = pin_args(tmp_path, repo, first, name=f"q{n}", root_name=f"unused{n}", **kw)
        args["files_root"] = str(root)
        out = pin.run_pin(**args)
        assert out.code == 2 and out.out == "" and out.err.startswith("pin refused: "), (n, out)
        assert refs(repo) == before_refs and loose_objects(repo) == before_loose, n
        return out.err

    assert "under dogfood/ only" in refused(1, {SPEC: CLEAN_SPEC, "a.txt": "replaced\n"})
    assert "already exists in the base" in refused(2, {SPEC: CLEAN_SPEC, "dogfood/old/spec.md": "replaced\n"})
    assert "already exists in the base" in refused(3, {SPEC: CLEAN_SPEC, "dogfood/old": "a file where a tree is\n"})
    assert "which the base has as a file" in refused(4, {SPEC: CLEAN_SPEC, "dogfood/old/spec.md/under.txt": "x\n"})
    assert "neither a regular file nor a directory" in refused(5, {SPEC: CLEAN_SPEC, "target.md": "t\n"}, hooks=[lambda r: (r / "link.md").symlink_to("target.md")])
    assert "neither a regular file nor a directory" in refused(6, hooks=[lambda r: os.mkfifo(r / "fifo")])
    refused(7, {SPEC: CLEAN_SPEC, "has space.md": "x\n"})
    refused(8, {SPEC: CLEAN_SPEC, ".hidden": "x\n"})
    refused(9, {SPEC: CLEAN_SPEC, "-dash.md": "x\n"})
    refused(10, {SPEC: CLEAN_SPEC, "café.md": "x\n"})
    assert "under dogfood/ only" in refused(18, {SPEC: CLEAN_SPEC, "src/hoh/evil.py": "x = 1\n"})
    assert "under dogfood/ only" in refused(19, {"spec.md": CLEAN_SPEC}, spec="spec.md")
    refused(11, {}, spec=SPEC)
    assert "not one of the files" in refused(12, {SPEC: CLEAN_SPEC}, spec="dogfood/zz/other.md")
    assert "not one of the files" in refused(13, {SPEC: CLEAN_SPEC}, spec="../spec.md")
    missing = pin.run_pin(**dict(pin_args(tmp_path, repo, first, name="q16"), files_root=str(tmp_path / "nowhere")))
    assert missing.code == 2 and refs(repo) == before_refs
    # a nested path with every component new is added; a directory the base has is extended, not replaced
    ok = pin.run_pin(**pin_args(tmp_path, repo, first, name="q17", files={SPEC: CLEAN_SPEC, "dogfood/old/new.md": "n\n"}, root_name="rootok"))
    assert ok.code == 0 and git(repo, "cat-file", "-p", "refs/pins/q17:dogfood/old/spec.md") == "old"


def test_the_message_gets_the_trailers_the_product_writes_and_the_digest_is_the_gates(tmp_path):
    pin, mg = mod("pin"), mod("mergegate")
    repo, first, second = make_repo(tmp_path)
    args = pin_args(tmp_path, repo, first, message="Pin the thing.\n\nA second paragraph.\n\n\n")
    out = pin.run_pin(**args)
    assert out.code == 0, out
    receipt = json.loads(Path(args["receipt"]).read_text())
    digest = hashlib.sha256(CLEAN_SPEC.encode()).hexdigest()
    assert receipt["spec_sha256"] == digest == mg._spec_sha256(str(repo), receipt["pin"], SPEC)
    assert f"SPEC_SHA256: {digest}" in out.out
    body = git(repo, "log", "-1", "--format=%B", receipt["pin"])
    assert body.startswith("Pin the thing.\n\nA second paragraph.\n\nSpec: " + SPEC)
    trailers = git(repo, "interpret-trailers", "--parse", stdin=body + "\n")
    assert trailers.splitlines() == [f"Spec: {SPEC}", f"Spec-SHA256: {digest}", f"Base: {first}",
                                     "Co-Authored-By: Claude Opus 5.5 <noreply@example.invalid>"]
    assert receipt["base"] == first and receipt["co_author"] == "Claude Opus 5.5 <noreply@example.invalid>"
    assert [f["path"] for f in receipt["files"]] == ["dogfood/zz/reference/x.txt", "dogfood/zz/spec.md"]
    assert {f["path"]: f["blob"] for f in receipt["files"]}["dogfood/zz/spec.md"] == git(repo, "rev-parse", receipt["pin"] + ":" + SPEC)
    before = refs(repo)
    for n, (message, co) in enumerate([("x\nSpec-SHA256: " + "0" * 64 + "\n", "A B <a@b.c>"), ("x\nco-authored-by: A <a@b.c>\n", "A B <a@b.c>"),
                                       ("x\nBase: " + first + "\n", "A B <a@b.c>"), ("x\nSpec: y\n", "A B <a@b.c>"), ("", "A B <a@b.c>"),
                                       ("   \n\n", "A B <a@b.c>"), ("ok\n", ""), ("ok\n", "A B"), ("ok\n", "<a@b.c>"),
                                       ("ok\n", "A B <a@b.c>\nCo-Authored-By: X <x@y.z>"),
                                       ("ok\n", "Ann\u202e <a@b.c>"), ("ok\n", "Ann\u200b B <a@b.c>"), ("ok\n", f"{HOME}/x <a@b.c>")]):
        refusal = pin.run_pin(**pin_args(tmp_path, repo, first, name=f"t{n}", message=message, co=co, root_name=f"mroot{n}"))
        assert refusal.code == 2 and refs(repo) == before, (message, co, refusal)


@pytest.mark.parametrize("spec_text, kind", [
    (f"see {HOME}/hoh-state/x\n", "home-path"), (f"at {HOME}\n", "home-path"),
    ("see /x/scratch/0123abcd/specs here\n", "scratch-path"), ("in /tmp/claude-1000/agent\n", "scratch-path"),
    ("run specs-draft/i6/x\n", "scratch-path"), ("in .herdr/worktrees/w\n", "scratch-path"),
    ("zero​width\n", "invisible"), ("bidi ‮ text\n", "invisible"), ("nbsp here\n", "invisible"),
    ("sep here\n", "invisible"), ("emoji ️\n", "invisible"), ("soft­hyphen\n", "invisible"),
    ("crlf\r\nline\r\n", "invisible"), ("bell \x07\n", "invisible"), ("﻿bom\n", "invisible"),
])
def test_the_spec_is_checked_for_a_home_path_a_scratch_path_and_invisible_characters(tmp_path, spec_text, kind):
    pin = mod("pin")
    repo, first, second = make_repo(tmp_path)
    before = refs(repo)
    args = pin_args(tmp_path, repo, first, files={SPEC: CLEAN_SPEC + spec_text, "dogfood/zz/reference/x.txt": "x\n"})
    out = pin.run_pin(**args)
    assert out.code == 2 and kind in out.err and "line" in out.err and refs(repo) == before, out
    assert not Path(args["receipt"]).exists()
    # an explicit, named allowance publishes it and the receipt says so
    allowed = pin.run_pin(**dict(args, allow=[kind], name="allowed"))
    assert allowed.code == 0, allowed
    assert json.loads(Path(args["receipt"]).read_text())["allowed"] == [kind]


def test_what_the_check_does_not_flag_and_what_it_never_lets_through(tmp_path):
    pin = mod("pin")
    repo, first, second = make_repo(tmp_path)
    harmless = (f"{HOME}x/other and {HOME[:-1]}/y and /home/someone/else and ~/hoh-state and /tmp/x and scratch/0123abcdef/ "
                "and scratch/<id>/ and specs-draftee and tab\there é → ü\n")
    assert pin.scan_text(harmless.encode(), home=HOME) == []
    out = pin.run_pin(**pin_args(tmp_path, repo, first, files={SPEC: CLEAN_SPEC + harmless, "dogfood/zz/reference/x.txt": f"{HOME}/note​\n"}))
    assert out.code == 0, out          # only the spec and the message are read for these kinds, not the reference files
    # bytes that are not text can never be allowed
    for n, data in enumerate([b"\xff\xfe bad", b"nul\x00here"]):
        root = tmp_path / f"bin{n}"
        (root / "dogfood" / "zz").mkdir(parents=True)
        (root / SPEC).write_bytes(data)
        args = dict(pin_args(tmp_path, repo, first, name=f"b{n}", root_name=f"unused{n}"), files_root=str(root))
        refusal = pin.run_pin(**dict(args, allow=["invisible", "home-path", "scratch-path"]))
        assert refusal.code == 2 and "encoding" in refusal.err
        unknown = pin.run_pin(**dict(args, allow=["encoding"]))
        assert unknown.code == 2 and "not a kind that can be allowed" in unknown.err
    # the message is read like the spec
    bad_message = pin.run_pin(**pin_args(tmp_path, repo, first, name="m", message=f"see {HOME}/x\n", root_name="mroot"))
    assert bad_message.code == 2 and "message" in bad_message.err and "home-path" in bad_message.err


# --- hoh import ---------------------------------------------------------------------------------


class Run:
    """An integration repository, a development worktree of a run that holds one accepted candidate, and the run state."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.integ, self.first, self.second = make_repo(tmp_path)
        # the run's development worktree is a linked worktree of the run's own repository, as in a real run
        self.target = tmp_path / "target"
        self.dev = tmp_path / "dev"
        subprocess.run(["git", "clone", "-q", str(self.integ), str(self.target)], check=True, capture_output=True)
        git(self.target, "worktree", "add", "-q", "--detach", str(self.dev), self.second)
        self.cand = commit(self.dev, {"a.txt": "one\nTWO-CAND\nthree\n"}, "candidate")
        git(self.dev, "checkout", "-q", "--detach", self.second)    # the candidate is on no branch and no head
        self.tree = git(self.dev, "rev-parse", self.cand + "^{tree}")
        self.state = self.write_state(self.tree)

    def write_state(self, tree, name="state.json", **extra):
        path = self.tmp / name
        candidate = {"candidate_id": "r1-i1-a1", "repo_path": str(self.dev), "commit": self.first, "tree_clean": False,
                     "tree_digest": tree, "created_at": "2026-10-10T00:00:00Z", "note": None}
        path.write_text(json.dumps({"run_id": "r1", "last_accepted_candidate": candidate, **extra}))
        return path

    def other(self, name, files, parent=None):
        """An import ref in the integration repository for a commit that adds `files` to `parent`."""
        parent = parent or self.first
        git(self.integ, "checkout", "-q", "--detach", parent)
        oid = commit(self.integ, files, name)
        git(self.integ, "checkout", "-q", "main")
        git(self.integ, "update-ref", f"refs/imports/{name}", oid)
        return oid

    def args(self, **over):
        base = dict(repo=str(self.integ), run_state=str(self.state), commit=self.cand, name="x", receipt=str(self.tmp / "import-x.json"))
        base.update(over)
        return base


def topology_tool():
    spec = importlib.util.spec_from_file_location("self_hosted_topology", ROOT / "tools" / "self_hosted_topology.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def test_an_accepted_candidate_is_fetched_by_its_full_id_and_its_tree_is_the_accepted_one(tmp_path):
    imp = mod("importref")
    run = Run(tmp_path)
    hook_runs = install_hook(run.integ, tmp_path)
    runs_before = hook_runs()
    before = snapshot(run.integ)
    before_refs = refs(run.integ)
    out = imp.run_import(**run.args())
    assert out.code == 0 and out.err == "", out
    assert hook_runs() == runs_before, "the product's git ran a hook of the repository"
    after = snapshot(run.integ)
    assert after == before, "a branch, HEAD, the index or the working tree of the integration repository changed"
    assert not (run.integ / ".git" / "FETCH_HEAD").exists()
    assert set(refs(run.integ).splitlines()) - set(before_refs.splitlines()) == {f"refs/imports/x {run.cand}"}
    receipt = json.loads(Path(run.args()["receipt"]).read_text())
    assert (receipt["ref"], receipt["commit"], receipt["tree"], receipt["tree_digest"]) == ("refs/imports/x", run.cand, run.tree, run.tree)
    assert (receipt["run_id"], receipt["candidate_id"], receipt["source"], receipt["fetched"]) == ("r1", "r1-i1-a1", str(run.dev), True)
    assert receipt["parents"] == [run.second] and receipt["base"] == run.second
    assert [p["against"] for p in receipt["previews"]] == ["base"] and receipt["all_clean"] is True
    assert out.out.splitlines()[0] == f"IMPORT: refs/imports/x {run.cand}"
    # the same arithmetic the tool performs, on the same inputs
    assert topology_tool().verify_import(run.state, run.cand, integration=run.integ) == []
    assert imp.verify_tree(str(run.integ), run.cand, imp.accepted_candidate(str(run.state))) == run.tree


def test_a_commit_that_is_not_the_accepted_tree_is_refused_and_no_ref_is_made(tmp_path):
    imp = mod("importref")
    run = Run(tmp_path)
    git(run.dev, "checkout", "-q", "--detach", run.first)
    other = commit(run.dev, {"a.txt": "one\nSOMETHING-ELSE\nthree\n", "extra.txt": "e\n"}, "not accepted")
    git(run.dev, "checkout", "-q", "--detach", run.second)
    before = refs(run.integ)
    out = imp.run_import(**run.args(commit=other))
    assert out.code == 2 and out.out == "" and "did not preserve the accepted bytes" in out.err, out
    assert run.tree[:12] in out.err and git(run.dev, "rev-parse", other + "^{tree}")[:12] in out.err
    assert refs(run.integ) == before and not Path(run.args()["receipt"]).exists()
    # the tool agrees on the same inputs (the commit is in the repository now, as the fetch put it there)
    assert topology_tool().verify_import(run.state, other, integration=run.integ) != []
    # objects are checked on the way in: a commit whose tree has a `.git` entry carries the digest it claims and is still refused
    blob = git(run.dev, "hash-object", "-w", "--stdin", stdin="x")
    bad_tree = git(run.dev, "mktree", stdin=f"100644 blob {blob}\t.git\n")
    bad_commit = git(run.dev, "commit-tree", bad_tree, "-p", run.second, "-m", "dot git")
    fsck_state = run.write_state(bad_tree, name="fsck.json")
    fscked = imp.run_import(**run.args(commit=bad_commit, run_state=str(fsck_state), name="fsck", receipt=str(run.tmp / "fsck-r.json")))
    assert fscked.code == 2 and fscked.out == "" and refs(run.integ) == before and not (run.tmp / "fsck-r.json").exists()
    # a tree digest the state does not carry as a full tree id, and a state without an accepted candidate
    for n, state in enumerate([run.write_state("abc123", name="s1.json"), run.write_state("", name="s2.json")]):
        refused = imp.run_import(**run.args(run_state=str(state)))
        assert refused.code == 2 and refs(run.integ) == before, n
    (run.tmp / "s3.json").write_text(json.dumps({"run_id": "r1", "last_accepted_candidate": None}))
    (run.tmp / "s4.json").write_text("not json")
    for name in ("s3.json", "s4.json", "missing.json"):
        refused = imp.run_import(**run.args(run_state=str(run.tmp / name)))
        assert refused.code == 2 and refs(run.integ) == before, name
    # the worktree the state names is gone: nothing to fetch from, no ref
    # (or a URL, or a relative path that happens to exist: nothing is fetched from anything but an absolute local directory)
    for n, where in enumerate([str(run.tmp / "no-such-worktree"), "https://example.invalid/r.git", "file:///nonexistent", "dev",
                               "-dev", ""]):
        gone = json.loads(run.state.read_text())
        gone["last_accepted_candidate"]["repo_path"] = where
        (run.tmp / f"g{n}.json").write_text(json.dumps(gone))
        out = imp.run_import(**run.args(run_state=str(run.tmp / f"g{n}.json")))
        assert out.code == 2 and out.out == "" and refs(run.integ) == before, where


def test_the_commit_and_the_base_are_full_ids_and_an_import_ref_is_never_moved(tmp_path):
    imp = mod("importref")
    run = Run(tmp_path)
    git(run.dev, "tag", "cand-tag", run.cand)
    before = refs(run.integ)
    for n, typed in enumerate([run.cand[:12], run.cand.upper(), "cand-tag", "main", "HEAD", "f" * 40, ""]):
        out = imp.run_import(**run.args(commit=typed, name=f"c{n}", receipt=str(run.tmp / f"rc{n}.json")))
        assert out.code == 2 and out.out == "", typed
        assert refs(run.integ) == before
    for n, typed in enumerate([run.first[:12], "main", "HEAD", "f" * 40]):
        out = imp.run_import(**run.args(base=typed, name=f"b{n}", receipt=str(run.tmp / f"rb{n}.json")))
        assert out.code == 2 and refs(run.integ) == before, typed
    for n, bad in enumerate(["", "a b", "../x", "x.lock", "-x", "x" * 33, "a/b"]):
        out = imp.run_import(**run.args(name=bad, receipt=str(run.tmp / f"ri{n}.json")))
        assert out.code == 2 and refs(run.integ) == before, bad
    # the product resolves HEAD itself when no base is typed, and records the full id; a typed full id is recorded too
    ok = imp.run_import(**run.args())
    assert ok.code == 0 and json.loads(Path(run.args()["receipt"]).read_text())["base"] == run.second
    git(run.integ, "update-ref", "refs/imports/d/x", run.first)
    loose_before = loose_objects(run.integ)
    clash = imp.run_import(**run.args(name="d", receipt=str(run.tmp / "d.json")))
    assert clash.code == 2 and "collide" in clash.err and not (run.tmp / "d.json").exists()
    assert loose_objects(run.integ) == loose_before
    assert subprocess.run(["git", "-C", str(run.integ), "rev-parse", "--verify", "--quiet", "refs/imports/d"]).returncode != 0
    held = git(run.integ, "rev-parse", "refs/imports/x")
    again = imp.run_import(**run.args(receipt=str(run.tmp / "again.json"), base=run.first))
    assert again.code == 2 and "exists" in again.err and git(run.integ, "rev-parse", "refs/imports/x") == held
    typed_base = imp.run_import(**run.args(name="y", receipt=str(run.tmp / "y.json"), base=run.first))
    assert typed_base.code == 0 and json.loads((run.tmp / "y.json").read_text())["base"] == run.first
    real = imp.git

    def racing(repo_, *a, **kw):
        if a and a[0] == "update-ref":
            real(repo_, "update-ref", a[1], run.first, "")        # an import of that name made in between
        return real(repo_, *a, **kw)
    imp.git = racing
    try:
        raced = imp.run_import(**run.args(name="race", receipt=str(run.tmp / "race.json")))
    finally:
        imp.git = real
    assert raced.code == 2 and git(run.integ, "rev-parse", "refs/imports/race") == run.first and not (run.tmp / "race.json").exists()
    taken = run.tmp / "taken.json"
    taken.write_text("mine\n")
    refused = imp.run_import(**run.args(name="z", receipt=str(taken)))
    assert refused.code == 2 and taken.read_text() == "mine\n"
    assert subprocess.run(["git", "-C", str(run.integ), "rev-parse", "--verify", "--quiet", "refs/imports/z"]).returncode != 0


def test_the_merge_preview_against_the_base_and_the_other_imports_says_clean_conflict_or_error(tmp_path):
    imp = mod("importref")
    run = Run(tmp_path)
    run.other("conflicting", {"a.txt": "one\nTWO-OTHER\nthree\n"})
    clean = run.other("clean", {"b.txt": "b changed\n"})
    merged = run.other("merged", {"m.txt": "m\n"}, parent=run.second)
    git(run.integ, "merge", "-q", "--ff-only", merged)          # main now contains the import `merged`
    tip = git(run.integ, "rev-parse", "HEAD")
    orphan = git(run.integ, "commit-tree", git(run.integ, "hash-object", "-t", "tree", "-w", "--stdin", stdin=""), "-m", "orphan")
    git(run.integ, "update-ref", "refs/imports/orphan", orphan)
    before = snapshot(run.integ)
    refs_before = set(refs(run.integ).splitlines())
    out = imp.run_import(**run.args(against=["conflicting", "clean"]))
    assert out.code == 1, out
    receipt = json.loads(Path(run.args()["receipt"]).read_text())
    by = {p["against"]: p for p in receipt["previews"]}
    assert list(by) == ["base", "import:clean", "import:conflicting"] and receipt["base"] == tip
    assert (by["base"]["status"], by["base"]["relation"]) == ("clean", "diverged")
    assert (by["import:clean"]["status"], by["import:clean"]["relation"]) == ("clean", "diverged")
    assert by["import:conflicting"]["status"] == "conflict" and by["import:conflicting"]["conflicts"] == ["a.txt"]
    assert by["import:conflicting"]["conflict_count"] == 1 and receipt["all_clean"] is False
    assert (by["base"]["left"], by["base"]["right"]) == (tip, run.cand) and by["import:clean"]["left"] == clean
    assert "PREVIEW import:conflicting: CONFLICT diverged (1 path(s))" in out.out and "PREVIEW base: CLEAN diverged" in out.out
    after = snapshot(run.integ)
    assert {k: after[k] for k in before} == before, "the preview changed a branch, HEAD, the index or the working tree"
    assert set(refs(run.integ).splitlines()) - refs_before == {f"refs/imports/x {run.cand}"}
    # --against-unmerged: every other import that the base does not contain yet (not the merged one, not itself)
    auto = imp.run_import(**run.args(name="y", receipt=str(run.tmp / "y.json"), against_unmerged=True))
    receipt_y = json.loads((run.tmp / "y.json").read_text())
    assert sorted(p["against"] for p in receipt_y["previews"]) == ["base", "import:clean", "import:conflicting", "import:orphan", "import:x"]
    status = {p["against"]: p["status"] for p in receipt_y["previews"]}
    assert status["import:orphan"] == "error" and status["import:x"] == "clean" and auto.code == 1
    # an unknown, a missing or the own name is refused; so is a list that is too long
    for wrong in (["nope"], ["w"], ["merged", "nope"]):
        refused = imp.run_import(**run.args(name="w", receipt=str(run.tmp / "w.json"), against=wrong))
        assert refused.code == 2 and not (run.tmp / "w.json").exists()
    # when every preview is clean the exit is 0
    ok = imp.run_import(**run.args(name="u", receipt=str(run.tmp / "u.json"), against=["clean"]))
    assert ok.code == 0 and json.loads((run.tmp / "u.json").read_text())["all_clean"] is True
    # the relations
    assert imp.preview(str(run.integ), tip, tip)["relation"] == "same"
    assert imp.preview(str(run.integ), tip, run.first)["relation"] == "contained"
    assert imp.preview(str(run.integ), run.first, tip)["relation"] == "fast-forward"


def test_the_import_receipt_is_create_only_and_a_failure_after_the_ref_is_exit_3(tmp_path):
    imp = mod("importref")
    run = Run(tmp_path)
    def broken(path, payload, **kw):
        raise OSError(5, "io error")
    original = imp.durable_create
    imp.durable_create = broken
    try:
        late = imp.run_import(**run.args())
    finally:
        imp.durable_create = original
    assert late.code == 3 and "IMPORT: refs/imports/x" in late.out and "NOT written" in late.err
    assert git(run.integ, "rev-parse", "refs/imports/x") == run.cand
    # a receipt whose read-back differs from what was written is exit 3, not trusted
    real_create = imp.durable_create
    imp.durable_create = lambda path, payload, **kw: real_create(path, "tampered\n", **kw)
    try:
        altered = imp.run_import(**run.args(name="altered", receipt=str(run.tmp / "alt.json")))
    finally:
        imp.durable_create = real_create
    assert altered.code == 3 and "read back" in altered.err and "IMPORT: refs/imports/altered" in altered.out
    # a second import of the same commit under a new name works, and its receipt is never replaced by a third
    receipt = run.tmp / "second.json"
    assert imp.run_import(**run.args(name="second", receipt=str(receipt))).code == 0
    text = receipt.read_text()
    third = imp.run_import(**run.args(name="third", receipt=str(receipt)))
    assert third.code == 2 and receipt.read_text() == text


def test_the_commands_are_wired_into_the_cli_and_the_modules_remove_and_move_nothing(tmp_path, capsys):
    from hoh import cli

    repo, first, second = make_repo(tmp_path)
    args = pin_args(tmp_path, repo, first)
    code = cli.main(["pin", "--repo", args["repo"], "--base", first, "--files-root", args["files_root"], "--spec", SPEC,
                     "--ref", "viacli", "--message-file", args["message_file"], "--co-author", args["co_author"],
                     "--receipt", args["receipt"], "--allow", "invisible"])
    seen = capsys.readouterr()
    assert code == 0 and seen.out.startswith("PIN: ") and git(repo, "rev-parse", "refs/pins/viacli")
    code = cli.main(["pin", "--repo", args["repo"], "--base", "main", "--files-root", args["files_root"], "--spec", SPEC,
                     "--ref", "viacli2", "--message-file", args["message_file"], "--co-author", args["co_author"],
                     "--receipt", str(tmp_path / "r2.json")])
    seen = capsys.readouterr()
    assert code == 2 and seen.out == "" and seen.err.startswith("pin refused: ")
    (tmp_path / "run").mkdir()
    run = Run(tmp_path / "run")
    code = cli.main(["import", "--repo", str(run.integ), "--run-state", str(run.state), "--commit", run.cand, "--id", "viacli",
                     "--receipt", str(tmp_path / "ri.json")])
    seen = capsys.readouterr()
    assert code == 0 and seen.out.startswith("IMPORT: refs/imports/viacli ") and git(run.integ, "rev-parse", "refs/imports/viacli") == run.cand
    with pytest.raises(SystemExit) as caught:
        cli.main(["import", "--repo", str(run.integ)])
    assert caught.value.code == 2
    capsys.readouterr()
    # the three modules remove nothing and run no git command that moves a branch, the index or the worktree
    forbidden_calls = {"unlink", "rmdir", "rmtree", "remove", "removedirs", "truncate", "rename", "kill", "killpg", "system", "Popen"}
    forbidden_git = {"checkout", "switch", "reset", "clean", "branch", "stash", "merge", "rebase", "commit", "add", "rm", "mv",
                     "restore", "push", "pull", "gc", "prune", "worktree", "tag", "config"}
    for name in ("commitref", "pin", "importref"):
        source = (ROOT / "src" / "hoh" / f"{name}.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert called not in forbidden_calls, (name, called, node.lineno)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "git":
                words = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                assert not forbidden_git & set(words), (name, words, node.lineno)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                assert not {"shutil", "signal", "tempfile"} & set(modules), (name, node.lineno)


def test_the_limits_hold_by_behaviour_not_by_constant(tmp_path):
    pin, imp = mod("pin"), mod("importref")
    repo, first, second = make_repo(tmp_path)

    def attempt(n, files, message="m\n"):
        return pin.run_pin(**pin_args(tmp_path, repo, first, name=f"l{n}", files=files, message=message, root_name=f"lroot{n}"))

    def refused(n, files, message="m\n"):
        before = (refs(repo), loose_objects(repo))
        out = attempt(n, files, message)
        assert out.code == 2 and out.out == "" and (refs(repo), loose_objects(repo)) == before, (n, out.err)
        return out.err

    two_hundred = {f"dogfood/zz/f{i:03d}.txt": "x\n" for i in range(199)}
    two_hundred[SPEC] = CLEAN_SPEC
    assert attempt(1, two_hundred).code == 0                      # exactly 200 files
    assert "over the bounds" in refused(2, {**two_hundred, "dogfood/zz/extra.txt": "x\n"})
    megabyte = 1024 * 1024
    assert attempt(3, {SPEC: CLEAN_SPEC, "dogfood/zz/big.txt": "a" * (4 * megabyte)}).code == 0   # exactly 4 MiB
    assert "over the bounds" in refused(4, {SPEC: CLEAN_SPEC, "dogfood/zz/big.txt": "a" * (4 * megabyte + 1)})
    assert attempt(5, {SPEC: CLEAN_SPEC, **{f"dogfood/zz/b{i}.txt": "b" * (4 * megabyte) for i in range(3)}}).code == 0
    assert "over the bounds" in refused(6, {SPEC: CLEAN_SPEC, **{f"dogfood/zz/b{i}.txt": "b" * (4 * megabyte) for i in range(4)}})
    assert attempt(7, {SPEC: CLEAN_SPEC}, message="m" * 8191 + "\n").code == 0          # exactly 8192 bytes
    refused(8, {SPEC: CLEAN_SPEC}, message="m" * 8192 + "\n")
    # 32 other imports to compare against are taken, 33 are refused with no ref
    (tmp_path / "r2").mkdir()
    run = Run(tmp_path / "r2")
    unmerged = run.other("m00", {"zz.txt": "z\n"})
    for i in range(1, 32):
        git(run.integ, "update-ref", f"refs/imports/m{i:02d}", unmerged)
    first_import = imp.run_import(**run.args(name="n1", receipt=str(run.tmp / "n1.json"), against_unmerged=True))
    assert first_import.code == 0 and len(json.loads((run.tmp / "n1.json").read_text())["previews"]) == 33
    before = refs(run.integ)
    over = imp.run_import(**run.args(name="n2", receipt=str(run.tmp / "n2.json"), against_unmerged=True))
    assert over.code == 2 and refs(run.integ) == before and not (run.tmp / "n2.json").exists()


def test_neither_command_runs_maintenance_that_could_remove_an_object(tmp_path):
    pin, imp = mod("pin"), mod("importref")
    run = Run(tmp_path)
    repo = run.integ
    git(repo, "config", "gc.auto", "1")
    git(repo, "config", "gc.autoDetach", "false")
    git(repo, "config", "gc.pruneExpire", "now")
    loose = tmp_path / "loose"
    loose.mkdir()
    names = []
    for i in range(1500):
        (loose / f"o{i}").write_text(f"dangling {i}\n")
        names.append(str(loose / f"o{i}"))
    ids = git(repo, "hash-object", "-w", "--stdin-paths", stdin="\n".join(names) + "\n").split()
    assert len(ids) == 1500
    # control: plain `git gc --auto` of this repository does prune them (so the setup is a real trap)
    probe_ids = ids[:1]
    out = pin_args(tmp_path, repo, run.first, name="gcpin")
    assert pin.run_pin(**out).code == 0
    assert imp.run_import(**run.args()).code == 0                   # the fetch is where git starts auto maintenance
    survivors = git(repo, "cat-file", "--batch-check", stdin="\n".join(ids) + "\n").splitlines()
    assert not [row for row in survivors if row.endswith("missing")], "an unreachable object was pruned"
    control = subprocess.run(["git", "-C", str(repo), "gc", "--auto", "--quiet"], capture_output=True, text=True, env=dict(os.environ, **IDENT))
    gone = git(repo, "cat-file", "--batch-check", stdin="\n".join(probe_ids + ids[-5:]) + "\n")
    assert control.returncode == 0 and "missing" in gone, "the control: the trap was not set (gc --auto does not prune here)"
