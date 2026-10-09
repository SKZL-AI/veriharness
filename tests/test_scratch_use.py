"""A caller's scratch (`runner.run_check(scratch=)`) is judged by the directory, held by the
kernel, and remembered by nobody (P2-42, the vet leaves the process's memory).

Every test names, in its docstring, the failure it catches and the negative control that changes
only the condition under test. Hermetic: every tree is under pytest's ``tmp_path``; the only
other process is a short python child that holds a ``flock`` and exits when its stdin closes. No
test starts Herdr, an agent or a sandbox (the sandboxed regime is a recording backend that runs
``/bin/true``).

Linux only where it says so: U3 counts ``/proc/self/fd``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hoh import runner
from hoh.contracts import AcceptanceCheck, Candidate
from hoh.sandbox import Isolation, LaunchPlan

# The lock protocol between HoH processes, written out independently of the runner: an exclusive
# flock on the scratch, a shared one on every ancestor that is owned by this user and has no group
# or other permission bits (the only ancestors that could themselves be scratches).
_HOLDER = (
    "import fcntl, os, stat, sys, time\n"
    "deadline = time.time() + float(sys.argv[1])\n"
    "keep = []\n"
    "def take(fd, how):                      # never blocks for ever: a busy lock is retried until\n"
    "    while True:                         # the deadline, then the holder gives up (exit 3)\n"
    "        try:\n"
    "            fcntl.flock(fd, how | fcntl.LOCK_NB)\n"
    "            return\n"
    "        except BlockingIOError:\n"
    "            if time.time() > deadline:\n"
    "                sys.exit(3)\n"
    "            time.sleep(0.05)\n"
    "for spec in sys.argv[2:]:\n"
    "    mode, path = spec.split(':', 1)\n"
    "    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)\n"
    "    take(fd, fcntl.LOCK_EX)\n"
    "    keep.append(fd)\n"
    "    if mode == 'scratch':\n"
    "        for up in __import__('pathlib').Path(os.path.realpath(path)).parents:\n"
    "            info = os.lstat(up)\n"
    "            if info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077:\n"
    "                fd = os.open(up, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)\n"
    "                take(fd, fcntl.LOCK_SH)\n"
    "                keep.append(fd)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


@contextlib.contextmanager
def held(*specs: str, deadline: float = 15.0):
    """Another process holds a directory: ``"scratch:<dir>"`` as a second HoH process holds the
    scratch of a check (the protocol above), ``"ex:<dir>"`` as a plain exclusive ``flock`` of
    some other program. **Bounded:** if a lock stays busy for ``deadline`` seconds (a harness
    that runs this test with its temporary directory inside a scratch it holds, say) the child
    gives up and the helper fails with a message, instead of the test hanging."""
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(deadline), *specs],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held", \
            f"the holder could not take {specs} within {deadline} s (a lock is busy)"
        yield child
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:       # pragma: no cover - the stdin close ends it
            child.kill()
            child.wait()
        child.stdout.close()


class Recording:
    """A sandbox backend that records and runs ``/bin/true``; ``during`` runs inside ``plan``,
    i.e. while the runner holds the scratch for the sandboxed check."""

    name = "recording"

    def __init__(self, during=None):
        self.during = during
        self.scratch: list[Path] = []

    def unavailable(self):
        return None

    def plan(self, argv, spec):
        self.scratch.append(Path(spec.scratch))
        if self.during is not None:
            self.during()
        return LaunchPlan(argv=["/bin/true"], env={"PATH": "/usr/bin:/bin"},
                          cwd=str(spec.candidate), proves_isolation=False)


@pytest.fixture
def arena(tmp_path) -> Path:
    path = tmp_path / "runs" / "_arenas" / "r1" / "cand"
    path.mkdir(parents=True)
    return path


def scratch_dir(parent: Path, name: str) -> Path:
    directory = parent / name
    directory.mkdir(mode=0o700, parents=True)
    return directory


_counter = iter(range(1, 10_000))


def run(arena: Path, command: str, scratch, *, backend=None):
    kw = {"isolation": Isolation.STRICT, "sandbox": backend} if backend is not None else {}
    return runner.run_check(
        AcceptanceCheck(check_id="c1", description="d", command=command, expect_exit=0),
        Candidate(candidate_id="c", repo_path=str(arena), commit="0" * 40, tree_clean=True,
                  tree_digest="t"),
        run_id="r1", iteration=1, attempt=next(_counter), scratch=scratch, **kw)


def marker(arena: Path, name: str) -> Path:
    return arena / name


def test_scratch_use_is_judged_by_the_directory_not_remembered(tmp_path, arena):
    """U1. Catches a runner that remembers a scratch it has used and then accepts it non-empty
    (the in-memory vet): what another process, or a sandboxed check, wrote into the directory
    between two uses is found in the directory and the second use is refused, in either regime,
    and the command never runs. Three uses: unsandboxed, then a file appears (what a second
    process wrote), then unsandboxed again: ``ValueError``; the same for a sandboxed second use.
    Negative control: emptied, the next use runs."""
    ws = scratch_dir(tmp_path, "ws")
    assert run(arena, "touch m1", ws)[0].runner_ok and marker(arena, "m1").exists()
    (ws / ".gitconfig").write_text("[alias]\n\tx = !touch planted\n")      # written by another process
    with pytest.raises(ValueError, match="not empty"):
        run(arena, "touch m2", ws)
    with pytest.raises(ValueError, match="not empty"):
        run(arena, "true", ws, backend=Recording())
    assert not marker(arena, "m2").exists()
    (ws / ".gitconfig").unlink()
    assert run(arena, "touch m3", ws)[0].runner_ok and marker(arena, "m3").exists()


def test_a_scratch_in_use_by_another_process_is_refused_not_waited_for(tmp_path, arena):
    """U2. Catches a runner that does not hold its scratch across processes (two live processes
    on one scratch: one writes while the other's unsandboxed check starts from an 'empty'
    directory), and one that *waits* for it (a blocking lock would hang the caller's whole run):
    while another process holds the directory, an unsandboxed and a sandboxed use are each a
    ``ValueError`` ("in use") that returns promptly and runs nothing. Negative control: once the
    holder is gone, the same call runs."""
    ws = scratch_dir(tmp_path, "ws")
    outcome: dict[str, object] = {}

    def attempt(kind: str) -> None:
        try:
            run(arena, f"touch {kind}", ws, backend=Recording() if kind == "sandboxed" else None)
            outcome[kind] = "ran"
        except ValueError as exc:
            outcome[kind] = str(exc)

    with held(f"scratch:{ws}"):
        threads = [threading.Thread(target=attempt, args=(kind,), daemon=True)
                   for kind in ("unsandboxed", "sandboxed")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "the use waited for the holder instead of refusing"
    assert all("in use" in str(text) for text in outcome.values()), outcome
    assert not marker(arena, "unsandboxed").exists()
    assert run(arena, "touch after", ws)[0].runner_ok and marker(arena, "after").exists()


def test_overlapping_uses_of_one_scratch_in_one_process_are_refused(tmp_path, arena):
    """U3. Catches overlapping mixed-regime calls on one scratch (a sandboxed check still
    running while an unsandboxed one starts from a directory that is still empty): inside the
    sandboxed use, a second use of the same scratch -- unsandboxed, then sandboxed -- is a
    ``ValueError`` ("in use") and runs nothing. Negative control: after the sandboxed use has
    ended, the directory (still empty) is used again; and every use ends with the scratch free,
    also one that raised, and with no descriptor left open."""
    ws = scratch_dir(tmp_path, "ws")
    seen: list[str] = []

    def inner() -> None:
        for backend in (None, Recording()):
            with pytest.raises(ValueError, match="in use") as raised:
                run(arena, "touch inner", ws, backend=backend)
            seen.append(str(raised.value))

    before = len(os.listdir("/proc/self/fd"))
    run(arena, "true", ws, backend=Recording(during=inner))
    assert len(seen) == 2 and not marker(arena, "inner").exists()
    (ws / "x").write_text("x")
    with pytest.raises(ValueError, match="not empty"):         # a refusal after the lock was taken
        run(arena, "true", ws)
    (ws / "x").unlink()
    assert run(arena, "touch again", ws)[0].runner_ok and marker(arena, "again").exists()
    assert len(os.listdir("/proc/self/fd")) == before


def test_a_scratch_inside_or_holding_a_scratch_in_use_is_refused(tmp_path, arena):
    """U4. Catches nested scratches used at the same time (a sandboxed check writing
    `S1/.config/...` while an unsandboxed check runs with `S1/.config` as its ``$HOME``, or the
    other way round, at any depth): another process holds S1, and a use of `S1/a/b` is refused;
    another process holds `S1/a/b`, and a use of S1 is refused; the same nested path reached
    **through a symlinked parent** (`alias -> S1`, so `alias/a/b`) is refused too (the ancestors are those of the real
    path). Negative controls: a *sibling*
    of the held scratch is used (two workspaces of one root run side by side), and `S1/a/b`
    runs once the holder is gone."""
    s1 = scratch_dir(tmp_path, "root/s1")
    deep = scratch_dir(s1, "a/b")
    s1.chmod(0o700)
    sibling = scratch_dir(tmp_path, "root/s2")
    with held(f"scratch:{s1}"):
        with pytest.raises(ValueError, match="in use"):
            run(arena, "touch nested", deep)
        assert run(arena, "touch sibling", sibling)[0].runner_ok
    with held(f"scratch:{deep}"):
        with pytest.raises(ValueError, match="in use"):
            run(arena, "touch outer", s1)
    (tmp_path / "alias").symlink_to(s1)           # the same scratch reached through a symlinked parent
    with held(f"scratch:{s1}"):
        with pytest.raises(ValueError, match="in use"):
            run(arena, "touch aliased", tmp_path / "alias" / "a" / "b")
    assert not marker(arena, "aliased").exists()
    assert not marker(arena, "nested").exists() and not marker(arena, "outer").exists()
    assert marker(arena, "sibling").exists()
    assert run(arena, "touch deep", deep)[0].runner_ok and marker(arena, "deep").exists()


def test_only_an_ancestor_that_could_be_a_scratch_is_locked(tmp_path, arena):
    """U5. Catches a lock taken on every ancestor up to ``/`` (a directory that is not 0700 and
    ours cannot be a scratch, and a lock held on it by some other program must not refuse the
    check): a 0755 ancestor held exclusively by another process does not block a use of a scratch
    below it. Negative control: the same scratch under a 0700 ancestor held the same way is
    refused (U4)."""
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    ws = scratch_dir(open_dir, "ws")
    with held(f"ex:{open_dir}"):
        assert run(arena, "touch below", ws)[0].runner_ok and marker(arena, "below").exists()


def test_a_scratch_that_cannot_be_locked_is_not_used(tmp_path, arena, monkeypatch):
    """U6. Catches a lock failure taken for success (a filesystem without ``flock``): the use is a
    ``ValueError`` ("cannot be locked") and the command never runs; so is an ancestor whose
    state cannot be read (it could be a scratch in use), and so is a lock descriptor that cannot be
    opened (``EMFILE``: only a missing path, a link or a non-directory yields "no lock", and
    `_caller_scratch` refuses those). Negative control: with the real
    ``flock`` and ``lstat`` the same call runs."""
    ws = scratch_dir(tmp_path, "ws")
    with monkeypatch.context() as patched:
        patched.setattr(fcntl, "flock", lambda fd, how: (_ for _ in ()).throw(
            OSError(errno.ENOLCK, "No locks available")))
        with pytest.raises(ValueError, match="cannot be locked"):
            run(arena, "touch locked", ws)
    assert not marker(arena, "locked").exists()
    real_lstat = os.lstat                       # an ancestor whose state cannot be read is not skipped
    with monkeypatch.context() as patched:
        patched.setattr(os, "lstat", lambda p, **kw: (_ for _ in ()).throw(
            PermissionError(errno.EACCES, "denied")) if str(p) == str(tmp_path)
            else real_lstat(p, **kw))
        with pytest.raises(ValueError, match="cannot be locked"):
            run(arena, "touch unreadable", ws)
    assert not marker(arena, "unreadable").exists()
    real_open = os.open                         # a lock descriptor that cannot be opened is not "no lock"
    with monkeypatch.context() as patched:
        patched.setattr(os, "open", lambda p, *a, **kw: (_ for _ in ()).throw(
            OSError(errno.EMFILE, "Too many open files")) if str(p) == str(ws)
            else real_open(p, *a, **kw))
        with pytest.raises(ValueError, match="cannot be locked"):
            run(arena, "touch exhausted", ws)
    assert not marker(arena, "exhausted").exists()
    assert run(arena, "touch free", ws)[0].runner_ok and marker(arena, "free").exists()


@pytest.mark.parametrize("name", [".hoh-scratch-r1-i1-a0-c1", "cand.scratch", "other.scratch"])
def test_a_callers_scratch_never_has_the_shape_of_the_runners_own(tmp_path, arena, name):
    """U7. Catches the runner's own scratch named through ``scratch=`` (a sandboxed check writes
    into `<arena>.scratch` or `.hoh-scratch-<receipt>`; the owner of that scratch reuses it from
    its own memory and runs what was written): an empty 0700 directory of either shape --
    also one that belongs to another arena -- is a ``ValueError`` and runs nothing. Negative
    control: the same empty directory under another name runs."""
    own = scratch_dir(arena.parent, name)
    with pytest.raises(ValueError, match="runner's own scratch"):
        run(arena, "touch own", own)
    assert not marker(arena, "own").exists()
    assert run(arena, "touch ok", scratch_dir(arena.parent, "r1-abc123"))[0].runner_ok


def test_a_scratch_replaced_while_it_is_being_locked_is_not_used(tmp_path, arena, monkeypatch):
    """U8. Catches a lock that protects a directory the check then does not use (the name is
    renamed away and another directory takes it between the open that is locked and the path the
    check is given): the lock is taken on a descriptor, so the runner compares the locked
    directory with the one the name leads to now, and a mismatch is a ``ValueError`` ("replaced")
    that runs nothing. Negative control: an undisturbed use runs."""
    ws = scratch_dir(tmp_path, "ws")
    real_flock = fcntl.flock
    swapped: list[bool] = []

    def swapping_flock(fd, how):
        real_flock(fd, how)
        if not swapped:
            swapped.append(True)
            ws.rename(tmp_path / "ws-moved")
            ws.mkdir(mode=0o700)

    with monkeypatch.context() as patched:
        patched.setattr(fcntl, "flock", swapping_flock)
        with pytest.raises(ValueError, match="replaced"):
            run(arena, "touch swapped", ws)
    assert not marker(arena, "swapped").exists()
    assert run(arena, "touch plain", ws)[0].runner_ok and marker(arena, "plain").exists()


@pytest.mark.parametrize("failure", [errno.ENOENT, errno.ENOTDIR, errno.ELOOP])
def test_a_path_that_looks_unusable_when_the_lock_is_taken_is_not_used_unlocked(
        tmp_path, arena, monkeypatch, failure):
    """U9. Catches "no lock" for a path that is missing, a link or not a directory *at the moment the
    lock is opened*: such a path may be a real, empty directory a moment later (a same-uid actor
    swaps one in before the runner judges it), and the check would then run without the lock the
    others rely on. The open of the scratch for the lock fails with each of the three errors while
    the directory is a good one: the use is a ``ValueError`` ("cannot be locked") and runs nothing.
    Negative control: undisturbed, the same call runs."""
    ws = scratch_dir(tmp_path, "ws")
    real_open = os.open
    with monkeypatch.context() as patched:
        patched.setattr(os, "open", lambda p, *a, **kw: (_ for _ in ()).throw(
            OSError(failure, os.strerror(failure))) if str(p) == str(ws)
            else real_open(p, *a, **kw))
        with pytest.raises(ValueError, match="cannot be locked"):
            run(arena, "touch unheld", ws)
    assert not marker(arena, "unheld").exists()
    assert run(arena, "touch held", ws)[0].runner_ok and marker(arena, "held").exists()


def test_a_callers_scratch_is_never_inside_the_runners_own_scratch(tmp_path, arena):
    """U10. Catches a caller's scratch nested **inside** the runner's own scratch (`<arena>.scratch/.config`,
    which has an allowed name, is empty, 0700 and outside the arena): the owner reuses its scratch
    without a look, so what a sandboxed check wrote there (`$HOME/.config/git/config` is the owner's
    `$HOME/.config/...`) runs in the owner's next unsandboxed check. Three cases, each a ``ValueError``
    ("runner's own scratch") that runs nothing: (a) an own scratch this process made, nominated from
    **another arena** whose parent directory is elsewhere (only the process's memory of its own
    scratches knows it); (b) a directory with the own shape beside the arena that this process never
    made (another process's; only the shape beside the arena knows it) and (c) the same with the
    sandboxed shape `.hoh-scratch-<receipt>`, one level deeper; and, still another process's, with
    the arena reached through a symlink -- (d) a symlinked cwd, (f) that and a symlinked parent
    directory, (g) the owner used the real path and the caller the link, (h) the mirror: the owner's
    arena was the link and the caller was given the target or a symlinked parent, (i) the link and
    its target have different names (the runner makes its scratch
    beside the arena **as it was given**, so "beside" is judged by the real path of that parent and
    by the parent of the real arena). Negative control, which a rule of
    "any ancestor with an own shape" would break: a scratch inside the own-shaped scratch of an
    **outer** check -- not beside this arena, not made by this process -- runs (this very test runs
    under such a scratch when the product's runner runs it)."""
    own = arena.parent / "cand.scratch"
    runner.run_check(
        AcceptanceCheck(check_id="c0", description="d", command="true", expect_exit=0),
        Candidate(candidate_id="c", repo_path=str(arena), commit="0" * 40, tree_clean=True,
                  tree_digest="t"),
        run_id="r1", iteration=1, attempt=0)                    # the runner makes <arena>.scratch
    assert own.is_dir()
    nested = scratch_dir(own, ".config")
    other = tmp_path / "elsewhere" / "cand2"
    other.mkdir(parents=True)
    with pytest.raises(ValueError, match="runner's own scratch"):          # (a) memory
        run(other, "touch inside-a", nested)
    foreign = scratch_dir(arena.parent, "never-made-here.scratch")        # (b) shape beside the arena
    with pytest.raises(ValueError, match="runner's own scratch"):
        run(arena, "touch inside-b", scratch_dir(foreign, ".config"))
    sandboxed = scratch_dir(arena.parent, ".hoh-scratch-r9-i1-a0-c1")     # (c)
    with pytest.raises(ValueError, match="runner's own scratch"):
        run(arena, "touch inside-c", scratch_dir(sandboxed, "deeper/.config"))
    assert not [name for name in ("inside-a", "inside-b", "inside-c") if marker(arena, name).exists()
                ] and not (other / "inside-a").exists()
    # (d) an arena reached through a **symlinked cwd** (`link/cand2 -> real/cand2`): the owner's scratch
    # is `link/cand2.scratch`, beside the link, not beside the real arena; (f) the same under a
    # symlinked parent directory as well (`L -> R`, `R/cand4 -> X/cand4`, cwd `L/cand4`: the
    # scratch is really `R/cand4.scratch`); (g) the owner used the real path and the caller was
    # given the link (`real/cand5.scratch` is nominated for the arena `link/cand5`).
    spelled = tmp_path / "spelled"
    (spelled / "real" / "cand2").mkdir(parents=True)
    (spelled / "link").mkdir()
    (spelled / "link" / "cand2").symlink_to(spelled / "real" / "cand2")
    (spelled / "R").mkdir()
    (spelled / "X" / "cand4").mkdir(parents=True)
    (spelled / "L").symlink_to(spelled / "R")
    (spelled / "R" / "cand4").symlink_to(spelled / "X" / "cand4")
    (spelled / "real" / "cand5").mkdir()
    (spelled / "link" / "cand5").symlink_to(spelled / "real" / "cand5")
    for label, cwd, own_scratch in (
            ("d", spelled / "link" / "cand2", spelled / "link" / "cand2.scratch"),
            ("f", spelled / "L" / "cand4", spelled / "L" / "cand4.scratch"),
            ("g", spelled / "link" / "cand5", spelled / "real" / "cand5.scratch")):
        own_scratch.mkdir(mode=0o700)                      # made by another process: not in memory
        with pytest.raises(ValueError, match="runner's own scratch"):
            run(cwd, f"touch inside-{label}", scratch_dir(own_scratch, ".config"))
        assert not (cwd / f"inside-{label}").exists()
    # (h) the mirror of (g): the owner's arena was the symlinked cwd (`link/cand6`), so its scratch is
    # `link/cand6.scratch`; the caller was given the target, or a symlinked parent directory
    # (`pdir -> real`) -- neither spelling leads to `link`, only "the arena is also there by its own
    # name" does.
    (spelled / "real" / "cand6").mkdir()
    (spelled / "link" / "cand6").symlink_to(spelled / "real" / "cand6")
    (spelled / "pdir").symlink_to(spelled / "real")
    mirror = spelled / "link" / "cand6.scratch"
    mirror.mkdir(mode=0o700)
    for label, cwd in (("h1", spelled / "real" / "cand6"), ("h2", spelled / "pdir" / "cand6")):
        with pytest.raises(ValueError, match="runner's own scratch"):
            run(cwd, f"touch inside-{label}", scratch_dir(mirror, f".config-{label}"))
        assert not (cwd / f"inside-{label}").exists()
    # (i) the link and its target have different names (`link/cand7 -> real/target7`): the owner used
    # the target, so its scratch is `real/target7.scratch`; the name this caller was given is not in
    # that directory, only the parent of the real arena leads there.
    (spelled / "real" / "target7").mkdir()
    (spelled / "link" / "cand7").symlink_to(spelled / "real" / "target7")
    renamed = spelled / "real" / "target7.scratch"
    renamed.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="runner's own scratch"):
        run(spelled / "link" / "cand7", "touch inside-i", scratch_dir(renamed, ".config"))
    assert not (spelled / "link" / "cand7" / "inside-i").exists()
    outer = scratch_dir(tmp_path / "outer-arenas", "harness.scratch")
    assert run(arena, "touch inside-outer", scratch_dir(outer, "ws"))[0].runner_ok
    assert marker(arena, "inside-outer").exists()


def test_the_test_holder_gives_up_instead_of_hanging(tmp_path):
    """U11. Catches a holder that waits for a busy lock for ever (F2 of the integration review: a harness
    that runs this file with its temporary directory inside a scratch it holds made U2-U4 hang):
    asking for a directory that another holder has gives up within the deadline and the helper
    fails with a message, in about a second (under ten at the most). Negative control: a free directory is held."""
    ws = scratch_dir(tmp_path, "ws")
    started = time.monotonic()
    with held(f"scratch:{ws}"):
        with pytest.raises(AssertionError, match="could not take"):
            with held(f"scratch:{ws}", deadline=1.0):
                pass                                    # pragma: no cover
    assert time.monotonic() - started < 10, "the holder did not honour its deadline"
    with held(f"scratch:{ws}"):
        pass


@pytest.mark.parametrize("caller", ["real", "link", "pdir"])
@pytest.mark.parametrize("owner", ["real", "link", "pdir"])
def test_the_runners_own_scratch_is_recognised_under_every_spelling_of_the_arena(
        tmp_path, owner, caller):
    """U12. Catches an own scratch missed because the owner and the caller spell the arena differently
    (the runner builds its scratch beside the arena **as it was given**; the caller's scratch is
    judged by its real path): the arena is reached as `real` (`R/cand`), `link` (`L/cand`, a symlink
    to it) or `pdir` (`P/cand`, with `P -> R`); every combination of the owner's spelling and the
    caller's -- nine -- is a ``ValueError`` ("runner's own scratch") for a directory nominated
    inside the own-shaped scratch another process made beside the arena as the owner spelled it,
    and nothing runs. Negative control: a scratch in a directory that is **not** beside the arena
    (nor in an own-shaped one) runs from the same spelling."""
    base = tmp_path / "spellings"
    (base / "R" / "cand").mkdir(parents=True)
    (base / "L").mkdir()
    (base / "L" / "cand").symlink_to(base / "R" / "cand")
    (base / "P").symlink_to(base / "R")
    arena_as = {"real": base / "R" / "cand", "link": base / "L" / "cand", "pdir": base / "P" / "cand"}
    own = arena_as[owner].parent / "cand.scratch"
    own.mkdir(mode=0o700)                                   # another process's: not in this memory
    with pytest.raises(ValueError, match="runner's own scratch"):
        run(arena_as[caller], "touch inside", scratch_dir(own, ".config"))
    assert not (arena_as[caller] / "inside").exists()
    elsewhere = scratch_dir(tmp_path / "elsewhere", "ws")
    assert run(arena_as[caller], "touch outside", elsewhere)[0].runner_ok


def test_a_scratch_nested_in_an_own_shaped_directory_is_refused_when_the_arena_cannot_be_looked_for(
        tmp_path, monkeypatch):
    """U13. Catches a failure to look for the arena by its name in the owner's directory taken for
    "it is not there" (fail open): the entry cannot be told apart (``PermissionError``), and a
    scratch inside an own-shaped directory is refused, never admitted on a guess. A missing entry,
    on the other hand, is "not there". Negative control: the same scratch under an own-shaped
    directory that is not beside the arena, with a missing entry, runs."""
    arena = tmp_path / "a" / "cand"
    arena.mkdir(parents=True)
    own = scratch_dir(tmp_path / "b", "other.scratch")             # not beside the arena, own-shaped
    nested = scratch_dir(own, ".config")
    real_samefile = os.path.samefile
    with monkeypatch.context() as patched:
        patched.setattr(os.path, "samefile", lambda x, y: (_ for _ in ()).throw(
            PermissionError(errno.EACCES, "denied")))
        with pytest.raises(ValueError, match="runner's own scratch"):
            run(arena, "touch unsure", nested)
    assert not marker_in(arena, "unsure")
    assert run(arena, "touch sure", nested)[0].runner_ok and marker_in(arena, "sure")
    assert os.path.samefile is real_samefile


def marker_in(arena: Path, name: str) -> bool:
    return (arena / name).exists()


# --- P2-42 residuals: the runner's own scratch is known by every process through a record beside it ---

_OWNER = (
    "import sys\n"
    "from hoh import runner\n"
    "from hoh.contracts import AcceptanceCheck, Candidate\n"
    "runner.run_check(AcceptanceCheck(check_id='own', description='d', command='true', expect_exit=0),\n"
    "                 Candidate(candidate_id='c', repo_path=sys.argv[1], commit='0' * 40, tree_clean=True,\n"
    "                           tree_digest='t'), run_id='rO', iteration=1, attempt=0)\n"
)


def owner_process(arena_as_given: Path) -> None:
    """Another process of the runner makes the arena's own scratch (`<arena>.scratch`, unsandboxed check,
    no `scratch=`) and exits: nothing about it is in this process's memory."""
    src = str(Path(runner.__file__).resolve().parents[1])
    done = subprocess.run([sys.executable, "-B", "-c", _OWNER, str(arena_as_given)], capture_output=True,
                          text=True, timeout=120, env={**os.environ, "PYTHONPATH": src})
    assert done.returncode == 0, done.stderr


def record_of(own: Path) -> Path:
    return own.with_name(own.name + ".owner")


def test_a_scratch_inside_the_own_scratch_of_another_arena_and_process_is_refused(tmp_path, arena):
    """R1. Catches the cross-arena family (residual of P2-42): process A's check made `A/cand.scratch`;
    this process has **another arena** (another parent, another name) and nominates a directory inside it
    (`.config`, deeper, or through a symlinked path). No own-shaped directory is beside this arena and this
    process never made it; only the record beside the scratch (`cand.scratch.owner`: ``<dev> <ino>``, written
    by the process that made it) tells. A ``ValueError`` ("recorded as its own"), nothing runs, in the
    scratch nothing is written. Negative control: a scratch elsewhere, and one in the same directory that is
    **not** inside the scratch, run."""
    theirs = tmp_path / "A" / "cand"
    theirs.mkdir(parents=True)
    owner_process(theirs)
    own = theirs.parent / "cand.scratch"
    assert own.is_dir()
    nested = scratch_dir(own, ".config")
    deeper = scratch_dir(own, "x/y/.cache")
    (tmp_path / "alias").symlink_to(own)
    for label, target in (("a", nested), ("b", deeper), ("c", tmp_path / "alias" / ".config")):
        with pytest.raises(ValueError, match="recorded as its own"):
            run(arena, f"touch inside-{label}", target)
        assert not marker(arena, f"inside-{label}").exists()
    assert sorted(path.name for path in own.iterdir()) == [".config", "x"]
    assert record_of(own).read_text().split() == [str(own.stat().st_dev), str(own.stat().st_ino)]
    assert run(arena, "touch outside", scratch_dir(tmp_path, "ws"))[0].runner_ok
    sibling = scratch_dir(theirs.parent, "workspace")
    assert run(arena, "touch beside", sibling)[0].runner_ok


def test_a_scratch_inside_the_own_scratch_under_a_link_with_another_basename_is_refused(tmp_path):
    """R2. Catches the link family (residual of P2-42): the owner's arena is the link `P/lnk -> R/cand`, so
    its scratch is `P/lnk.scratch` (the runner builds it beside the arena **as it was given**); this
    process spells the arena by its real name and nominates `P/lnk.scratch/.config`. Neither the real
    arena's parent (`R`) nor the arena's name in `P` leads to it; the record, matched by the identity of the
    directory, does. A ``ValueError``, nothing runs. Negative control: a scratch elsewhere runs."""
    (tmp_path / "R" / "cand").mkdir(parents=True)
    (tmp_path / "P").mkdir()
    (tmp_path / "P" / "lnk").symlink_to(tmp_path / "R" / "cand")
    owner_process(tmp_path / "P" / "lnk")
    own = tmp_path / "P" / "lnk.scratch"
    assert own.is_dir()
    with pytest.raises(ValueError, match="recorded as its own"):
        run(tmp_path / "R" / "cand", "touch inside", scratch_dir(own, ".config"))
    assert not (tmp_path / "R" / "cand" / "inside").exists() and record_of(own).is_file()
    assert run(tmp_path / "R" / "cand", "touch outside", scratch_dir(tmp_path, "ws"))[0].runner_ok


def test_a_check_run_by_a_check_may_use_a_scratch_inside_the_outer_own_scratch(tmp_path):
    """R3. Catches a rule that refuses every scratch inside a recorded one (this file, run by the product's
    runner, has its temporary directory, its arena and its scratch under the outer check's own scratch): a
    caller whose **arena** is inside the recorded scratch runs, also when it is reached through a link outside
    the scratch (the real path is judged). Negative controls, each refused: an arena outside the recorded
    directory, and arenas below the owner's parent directory (the exemption is not "inside the scratch's
    parent")."""
    outer = tmp_path / "outer" / "cand"
    outer.mkdir(parents=True)
    owner_process(outer)
    own = outer.parent / "cand.scratch"
    inner = own / "runs" / "cand"
    inner.mkdir(parents=True)
    ws = scratch_dir(own, "ws")
    assert run(inner, "touch ran", ws)[0].runner_ok and marker(inner, "ran").exists()
    stranger = tmp_path / "stranger" / "cand"
    stranger.mkdir(parents=True)
    with pytest.raises(ValueError, match="recorded as its own"):
        run(stranger, "touch refused", scratch_dir(own, "ws2"))
    # the exemption is "inside the recorded directory", not "inside its parent": arenas below the owner's own
    # directory (not beside the scratch, so no older rule sees them) are strangers
    for label, where in (("below", outer.parent / "x" / "y" / "cand"), ("below1", outer.parent / "x" / "cand")):
        where.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError, match="recorded as its own"):
            run(where, f"touch refused-{label}", scratch_dir(own, f"ws-{label}"))
    # and it is judged by the **real** path of the arena: a link outside the scratch to an arena inside it runs
    (tmp_path / "arena-link").symlink_to(inner)
    assert run(tmp_path / "arena-link", "touch via-link", scratch_dir(own, "ws3"))[0].runner_ok
    assert marker(inner, "via-link").exists()


def test_the_record_is_appended_and_a_stale_line_is_not_about_the_directory(tmp_path, arena):
    """R4. Catches a record that is replaced (a parked scratch loses its line) or that makes any
    directory of that name refuse: a second process, finding the directory not its own, parks it and
    makes a new one; the record then has **two** lines, the new identity last (also after a torn tail: no newline); the parked directory
    (`cand.scratch.v<stamp>`) is not named by the record's file name. A record whose identity is not the
    directory's (the directory was replaced by something the runner did not make) is not about it:
    a scratch inside such a directory elsewhere runs. Negative control: the live scratch refuses."""
    theirs = tmp_path / "A" / "cand"
    theirs.mkdir(parents=True)
    owner_process(theirs)
    own = theirs.parent / "cand.scratch"
    first = record_of(own).read_text()
    record_of(own).write_text(first.rstrip("\n"))        # a crash mid-line left no newline: the next line starts a new one
    own.rename(own.with_name("cand.scratch.v1"))        # something else replaced it; the next owner parks and remakes
    owner_process(theirs)
    lines = record_of(own).read_text().splitlines()
    assert len(lines) == 2 and lines[0] + "\n" == first and lines[1] == f"{own.stat().st_dev} {own.stat().st_ino}"
    with pytest.raises(ValueError, match="recorded as its own"):
        run(arena, "touch live", scratch_dir(own, ".config"))
    stale = tmp_path / "B" / "x.scratch"
    stale.mkdir(parents=True, mode=0o700)
    record_of(stale).write_text(f"{stale.stat().st_dev} {stale.stat().st_ino + 1}\n")
    assert run(arena, "touch stale", scratch_dir(stale, ".config"))[0].runner_ok and marker(arena, "stale").exists()


@pytest.mark.parametrize("content", [
    pytest.param("junk\n", id="junk"), pytest.param("1 2 3\n", id="three-fields"),
    pytest.param("\n", id="empty-line"), pytest.param("1 2\n\n", id="empty-last-line"),
    # well-formed lines, only too many; the first is one byte longer so that the limit plus one byte ends on a newline
    pytest.param("1 22\n" + "1 2\n" * ((1 << 20) // 4 + 8), id="over-the-limit"),
    pytest.param("1 2", id="torn-last-line"),                            # no newline: cut by a crash, closed by the owner's next check
    pytest.param("1 2\n3", id="torn-inside-a-line")])
def test_a_record_that_cannot_be_read_refuses_the_caller(tmp_path, arena, content):
    """R5. Catches a record that is not understood taken for "not recorded" (fail open): a line that is not
    ``<dev> <ino>``, an empty line, a record over the limit -- the scratch is **not used** ("cannot be
    read"). (R5b: a link, ``EACCES``, and the negative controls.)"""
    own = scratch_dir(tmp_path / "elsewhere", "o.scratch")
    ws = scratch_dir(own, ".config")
    record_of(own).write_text(content)
    with pytest.raises(ValueError, match="cannot be read"):
        run(arena, "touch unsure", ws)
    assert not marker(arena, "unsure").exists()


def test_a_record_that_is_a_link_or_unopenable_refuses_the_caller(tmp_path, arena, monkeypatch):
    """R5b. The other two ways a record cannot be read: a symbolic link (not followed) and an ``OSError`` on open.
    Negative control: no record, then a well-formed one of another identity, run."""
    own = scratch_dir(tmp_path / "elsewhere", "o.scratch")
    ws = scratch_dir(own, ".config")
    assert run(arena, "touch none", ws)[0].runner_ok                         # no record
    target = tmp_path / "target"
    target.write_text("1 2\n")
    record_of(own).symlink_to(target)
    with pytest.raises(ValueError, match="cannot be read"):
        run(arena, "touch linked", ws)
    record_of(own).replace(tmp_path / "moved-away-link")                      # (moved, not removed)
    record_of(own).write_text("1 2\n")                                      # well-formed, not this directory
    assert run(arena, "touch other", ws)[0].runner_ok
    real_open = os.open
    def refusing(path, *args, **kwargs):
        if str(path).endswith(".owner"):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, *args, **kwargs)
    with monkeypatch.context() as patched:
        patched.setattr(os, "open", refusing)
        with pytest.raises(ValueError, match="cannot be read"):
            run(arena, "touch denied", ws)
    assert not marker(arena, "linked").exists() and not marker(arena, "denied").exists()


def test_a_scratch_that_cannot_be_recorded_is_not_used_until_it_is(tmp_path, arena, monkeypatch):
    """R6. Catches an own scratch used as a `HOME` without a record, or with a record that went missing (the
    record is what other processes know it by): when the record cannot be written (``ENOSPC``) the unsandboxed check does not use the
    scratch (its `HOME` is the arena, the runner's existing fall-back when the scratch cannot be made),
    and the next check, when the record can be written, records it and uses it. Negative control:
    the second check's `HOME` is `<arena>.scratch` and the record is there; a record removed in between
    is written again by the next check, once."""
    real_open = os.open
    def full(path, *args, **kwargs):
        if str(path).endswith(".owner"):
            raise OSError(errno.ENOSPC, "no space left")
        return real_open(path, *args, **kwargs)
    with monkeypatch.context() as patched:
        patched.setattr(os, "open", full)
        assert run(arena, "printf %s \"$HOME\" > home1", None)[0].runner_ok
    own = arena.parent / "cand.scratch"
    assert (arena / "home1").read_text() == str(arena) and not record_of(own).exists()
    assert run(arena, "printf %s \"$HOME\" > home2", None)[0].runner_ok
    assert (arena / "home2").read_text() == str(own)
    assert record_of(own).read_text().split() == [str(own.stat().st_dev), str(own.stat().st_ino)]
    record_of(own).replace(tmp_path / "moved-away-record")                    # a record that went missing is written again
    assert run(arena, "printf %s \"$HOME\" > home3", None)[0].runner_ok
    assert record_of(own).read_text().split() == [str(own.stat().st_dev), str(own.stat().st_ino)]
    assert run(arena, "printf %s \"$HOME\" > home4", None)[0].runner_ok     # and not twice
    assert len(record_of(own).read_text().splitlines()) == 1


def test_a_record_that_is_not_a_regular_file_never_blocks_the_runner(tmp_path, arena):
    """R7. Catches a runner that hangs on a FIFO planted as the record (`<dir>.owner`), on either side: the
    caller's check opens it with ``O_NONBLOCK`` and requires a regular file ("cannot be read", the scratch is
    not used); the owner's recording does the same and falls back to the arena as `HOME` (R6). A watchdog
    (``SIGALRM``, 20 s) turns a hang into a failure. Negative control: a regular record is read."""
    def watchdog(signum, frame):
        raise AssertionError("the runner blocked on a planted FIFO")
    previous = signal.signal(signal.SIGALRM, watchdog)
    signal.alarm(20)
    try:
        own = scratch_dir(tmp_path / "elsewhere", "o.scratch")
        ws = scratch_dir(own, ".config")
        os.mkfifo(record_of(own))
        with pytest.raises(ValueError, match="cannot be read"):
            run(arena, "touch fifo", ws)
        assert not marker(arena, "fifo").exists()
        os.mkfifo(record_of(arena.parent / "cand.scratch"))                   # the owner's side
        assert run(arena, "printf %s \"$HOME\" > home", None)[0].runner_ok
        assert (arena / "home").read_text() == str(arena)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_the_owners_record_is_written_with_care(tmp_path):
    """R8. Catches a record written without the guards the reader assumes: (a) a record the runner would
    have to read past the limit (well-formed lines, only too many) is not extended and the scratch is not
    used (`HOME` is the arena); (b) a record that is a symbolic link is not followed (its target is
    untouched) and the scratch is not used; (c) a record the runner creates is mode 0600 (the open's mode,
    under the usual umask). Negative control: (c) is a used scratch with its line."""
    def home_after(arena_dir: Path) -> str:
        arena_dir.mkdir(parents=True)
        assert run(arena_dir, "printf %s \"$HOME\" > home", None)[0].runner_ok
        return (arena_dir / "home").read_text()
    big = tmp_path / "a" / "cand"
    big.parent.mkdir()
    record_of(big.parent / "cand.scratch").write_text("1 2\n" * ((1 << 20) // 4 + 8))
    assert home_after(big) == str(big)
    size = record_of(big.parent / "cand.scratch").stat().st_size
    assert size == 4 * ((1 << 20) // 4 + 8)                                   # not extended
    linked = tmp_path / "b" / "cand"
    linked.parent.mkdir()
    target = tmp_path / "target"
    target.write_text("1 2\n")
    record_of(linked.parent / "cand.scratch").symlink_to(target)
    assert home_after(linked) == str(linked) and target.read_text() == "1 2\n"
    fine = tmp_path / "c" / "cand"
    fine.parent.mkdir()
    old = os.umask(0o022)
    try:
        assert home_after(fine) == str(fine.parent / "cand.scratch")
    finally:
        os.umask(old)
    assert stat_mode(record_of(fine.parent / "cand.scratch")) == 0o600


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
