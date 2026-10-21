"""P3-06 WorkspaceLease, slice 1: one writer per worktree, persisted.

Every test names the failure it catches. A test builds its own repository under
`tmp_path` (a main checkout and, where it matters, a linked worktree), starts no
Herdr and no agent, reads nothing outside `tmp_path`, and injects the clock, the
boot id and the set of live pids, so "the machine rebooted" and "the holder was
killed" are one line each. The module is imported **inside** each test, so that
on a tree without it every node fails for one reason of its own.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

START = "2026-10-12T10:00:00Z"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def make_repo(tmp_path, name="main"):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("print('one')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    return repo


def make_worktree(repo, name):
    target = repo.parent / name
    git(repo, "worktree", "add", "-q", "-b", name, str(target))
    return target


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class World:
    """One machine: a clock, a boot id, the live pids and their start times."""

    def __init__(self):
        self.now = START
        self.boot_id = "boot-one-0000"
        self.live = set()
        self.starts = {}

    def advance(self, seconds):
        moved = datetime.strptime(self.now, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=seconds)
        self.now = moved.strftime("%Y-%m-%dT%H:%M:%SZ")

    def lease(self, repo, holder, pid, host="h", pidns="pidns:[1]", **kw):
        from hoh.wslease import WorkspaceLease
        self.live.add(pid)
        return WorkspaceLease(
            repo, holder, clock=lambda: self.now, boot=lambda: self.boot_id, host=host,
            pid=pid, alive=lambda p: p in self.live, pidns=lambda: pidns,
            start=lambda p: self.starts.get(p, f"start-{p}"), **kw)


def lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="ascii").splitlines()]


# --- R1 ----------------------------------------------------------------------

def test_a_second_writer_is_refused_while_the_first_is_live(tmp_path):
    """Catches two handles holding one worktree: a second `acquire` that
    succeeds, one that overwrites the record, one that is refused only when the
    holder id differs, or a lease that is one-per-repository instead of
    one-per-worktree."""
    from hoh.wslease import WorkspaceHeld, WorkspaceLeaseLost
    from hoh.workspace import lease_directory

    repo = make_repo(tmp_path)
    linked = make_worktree(repo, "wt-one")
    w = World()
    a = w.lease(repo, "run-a", 101)
    first = a.acquire()
    assert first.generation == 1 and first.holder == "run-a"
    path = lease_directory(repo) / "lease.json"
    assert a.lease_path == path and path.is_file()
    before = sha(path)
    log_before = (path.parent / "lease.log.jsonl").read_bytes()

    for other, pid in (("run-b", 102), ("run-a", 103)):          # a name is not an identity
        b = w.lease(repo, other, pid)
        with pytest.raises(WorkspaceHeld) as exc:
            b.acquire()
        assert exc.value.reason == "held" and "run-a" in str(exc.value)
        with pytest.raises(WorkspaceLeaseLost):
            b.check()                                         # it never held anything
    assert sha(path) == before, "a refused acquire writes nothing"
    assert (path.parent / "lease.log.jsonl").read_bytes() == log_before

    again = a.acquire()                                       # the holder asking twice
    assert again == first and sha(path) == before
    assert len(lines(path.parent / "lease.log.jsonl")) == 1

    # One per worktree, not one per repository: the linked worktree is free.
    c = w.lease(linked, "run-c", 104)
    assert c.acquire().generation == 1
    assert lease_directory(linked) != lease_directory(repo)
    assert a.check().holder == "run-a" and c.check().holder == "run-c"

    a.release(reason="done")                                  # the control: now it is free
    assert w.lease(repo, "run-b", 105).acquire().generation == 2


# --- R2 ----------------------------------------------------------------------

def test_a_lost_holder_is_taken_over_and_a_live_one_never(tmp_path):
    """Catches a holder judged lost too early (a live one displaced) or too
    late (a dead one wedging the worktree): each way a holder is lost is
    exercised on its own, with a control that changes only that fact, and the
    expiry is pinned to the second."""
    from hoh.wslease import WorkspaceHeld

    repo = make_repo(tmp_path)
    w = World()

    def acquire(holder, pid, **kw):
        handle = w.lease(repo, holder, pid, ttl_seconds=60, **kw)
        return handle, handle.acquire()

    def why(handle):
        return lines(handle.directory / "lease.log.jsonl")[-1]["previous"]

    # Control: alive, same boot, inside its lease -> refused.
    _, first = acquire("run-a", 201)
    assert first.generation == 1
    with pytest.raises(WorkspaceHeld):
        w.lease(repo, "run-b", 301, ttl_seconds=60).acquire()

    # 1. the pid no longer exists
    w.live.discard(201)
    b, got = acquire("run-b", 301)
    assert got.generation == 2 and "no longer exists" in why(b)

    # 2. another boot (the machine restarted); the old pid may even exist again
    w.boot_id = "boot-two-0000"
    c, got = acquire("run-c", 302)
    assert got.generation == 3 and "restarted" in why(c)

    # 3. the pid now belongs to another process (a reused pid)
    w.starts[302] = "a-later-process"
    d, got = acquire("run-d", 303)
    assert got.generation == 4 and "another process" in why(d)

    # 4. a holder in another pid namespace cannot be asked: it is judged by its
    #    lease alone, and a live-looking one is never displaced before it runs out.
    w.advance(60)                                              # run-d's lease has run out
    far, got = acquire("run-f", 305, pidns="pidns:[2]")        # (taken by expiry alone)
    assert got.generation == 5 and "expired" in why(far)
    w.live.discard(305)                                        # not visible here, and must not matter
    with pytest.raises(WorkspaceHeld):
        w.lease(repo, "run-g", 306, ttl_seconds=60).acquire()
    w.advance(59)                                              # one second before expires_at
    with pytest.raises(WorkspaceHeld):
        w.lease(repo, "run-g", 306, ttl_seconds=60).acquire()
    w.advance(1)                                               # now == expires_at: lost
    g, got = acquire("run-g", 306)
    assert got.generation == 6
    assert got.acquired_at == "2026-10-12T10:02:00Z" and got.expires_at == "2026-10-12T10:03:00Z"

    # 5. released by its holder
    g.release(reason="done")
    h, got = acquire("run-h", 307)
    assert got.generation == 7 and why(h).startswith("released by 'run-g'")

    # 6. a holder on another host cannot be asked either: its pid is not ours to judge
    h.release(reason="next")
    i, got = acquire("run-i", 308, host="another-host")
    assert got.generation == 8
    w.live.discard(308)                                        # a pid of that name is not alive HERE ...
    with pytest.raises(WorkspaceHeld):                         # ... and must not matter
        w.lease(repo, "run-j", 309, ttl_seconds=60).acquire()
    w.advance(60)
    j, got = acquire("run-j", 309)
    assert got.generation == 9 and "expired" in why(j)


# --- R3 ----------------------------------------------------------------------

def test_only_the_holder_may_renew_and_release(tmp_path):
    """Catches a superseded handle that still writes: renew or release by a
    handle that lost the worktree must change nothing on disk and say it lost;
    a handle that never acquired holds nothing; an expired one must acquire
    again (a new generation)."""
    from hoh.wslease import WorkspaceLeaseLost

    repo = make_repo(tmp_path)
    w = World()
    a = w.lease(repo, "run-a", 401, ttl_seconds=100)
    stranger = w.lease(repo, "run-x", 499)
    for verb in (stranger.check, stranger.renew, lambda: stranger.release(reason="x")):
        with pytest.raises(WorkspaceLeaseLost) as exc:
            verb()
        assert exc.value.reason == "lost"
    assert not stranger.directory.exists(), "a handle that holds nothing creates nothing"

    a.acquire()
    w.advance(30)
    renewed = a.renew()
    assert renewed.generation == 1 and renewed.renewed_at == "2026-10-12T10:00:30Z"
    assert renewed.expires_at == "2026-10-12T10:02:10Z", "now + ttl, exactly"
    assert a.check() == renewed

    w.live.discard(401)                                        # a is killed ...
    b = w.lease(repo, "run-b", 402)
    taken = b.acquire()                                        # ... and replaced
    on_disk = sha(a.lease_path)
    for verb in (a.check, a.renew, lambda: a.release(reason="late")):
        with pytest.raises(WorkspaceLeaseLost) as exc:
            verb()
        assert "run-b" in str(exc.value)
    assert sha(a.lease_path) == on_disk, "the superseded handle changed nothing"
    assert b.check() == taken

    b.release(reason="finished")
    with pytest.raises(WorkspaceLeaseLost):
        b.check()
    with pytest.raises(WorkspaceLeaseLost):
        b.renew()
    assert json.loads(Path(b.lease_path).read_text())["released_reason"] == "finished"
    assert [e["event"] for e in lines(b.directory / "lease.log.jsonl")] == [
        "acquire", "acquire", "release"]

    c = w.lease(repo, "run-c", 403, ttl_seconds=10)            # expired, never taken: must acquire again
    c.acquire()
    w.advance(10)
    for verb in (c.check, c.renew):
        with pytest.raises(WorkspaceLeaseLost):
            verb()
    assert c.acquire().generation == 4


# --- R4 ----------------------------------------------------------------------

def test_an_unreadable_lease_is_never_read_as_no_lease(tmp_path):
    """Catches fail-open on odd input: every lease file that is there and is not
    a good record refuses `acquire` and `check`, with a stable reason, and
    leaves the bytes (and the log) exactly as they were."""
    from hoh.wslease import WorkspaceLeaseError, parse

    repo = make_repo(tmp_path)
    w = World()
    a = w.lease(repo, "run-a", 501)
    good = a.acquire()
    raw = a.lease_path.read_bytes()
    doc = json.loads(raw)
    w.live.discard(501)                      # from here a good record would be taken over

    def odd(name, payload):
        return name, payload if isinstance(payload, bytes) else (json.dumps(payload) + "\n").encode()

    def with_(**kw):
        return {**doc, **kw}

    cases = [
        ("empty", b"", "malformed"),
        ("latin1", raw.replace(b"run-a", b"run-\xe4"), "malformed"),
        ("utf16", raw.decode().encode("utf-16"), "malformed"),
        ("bom", b"\xef\xbb\xbf" + raw, "malformed"),
        ("nul", raw.replace(b"run-a", b"run\x00a"), "malformed"),
        ("truncated", raw[:40], "malformed"),
        ("repeated key", raw.replace(b'"generation"', b'"generation": 1, "generation"', 1), "malformed"),
        ("nan", raw.replace(b'"generation": 1', b'"generation": NaN'), "malformed"),
        ("array", b"[]\n", "malformed"),
        ("null", b"null\n", "malformed"),
        ("deep", b"[" * 16000, "malformed"),
        ("schema", json.dumps(with_(schema="hoh-workspace-lease/2")).encode(), "schema"),
        ("extra key", json.dumps(with_(extra=1)).encode(), "invalid"),
        ("missing key", json.dumps({k: v for k, v in doc.items() if k != "holder_pid"}).encode(), "invalid"),
        ("generation 0", json.dumps(with_(generation=0)).encode(), "invalid"),
        ("generation bool", json.dumps(with_(generation=True)).encode(), "invalid"),
        ("generation float", json.dumps(with_(generation=1.0)).encode(), "invalid"),
        ("generation str", json.dumps(with_(generation="1")).encode(), "invalid"),
        ("generation 5000 digits", raw.replace(b'"generation": 1', b'"generation": ' + b"9" * 5000), "malformed|invalid"),
        ("pid negative", json.dumps(with_(holder_pid=-1)).encode(), "invalid"),
        ("digest short", json.dumps(with_(token_sha256="ab")).encode(), "invalid"),
        ("digest upper", json.dumps(with_(token_sha256="A" * 64)).encode(), "invalid"),
        ("holder newline", json.dumps(with_(holder="run-a\n")).encode(), "invalid"),
        ("holder empty", json.dumps(with_(holder="")).encode(), "invalid"),
        ("text control", json.dumps(with_(holder_host="a\tb")).encode(), "invalid"),
        ("text non-ascii", json.dumps(with_(holder_host="hä")).encode(), "invalid"),
        ("text too long", json.dumps(with_(released_reason="x" * 513)).encode(), "invalid"),
        ("month 13", json.dumps(with_(expires_at="2026-13-12T10:00:00Z")).encode(), "invalid"),
        ("second 60", json.dumps(with_(expires_at="2026-10-12T10:00:60Z")).encode(), "invalid"),
        ("30 february", json.dumps(with_(expires_at="2026-02-30T10:00:00Z")).encode(), "invalid"),
        ("other script's digits", json.dumps(with_(expires_at="\u0662\u0660\u0662\u0666-10-12T10:00:00Z")).encode(), "invalid"),
        ("lowercase z", json.dumps(with_(expires_at="2026-10-12T10:00:00z")).encode(), "invalid"),
        ("trailing newline in time", json.dumps(with_(expires_at="2026-10-12T10:00:00Z\n")).encode(), "invalid"),
        ("released_at bad", json.dumps(with_(released_at="yesterday")).encode(), "invalid"),
        ("oversize", raw + b" " * (16384 - len(raw) + 1), "oversize"),
    ]
    for name, payload, reason in cases:
        a.lease_path.write_bytes(payload)
        before = sha(a.lease_path)
        for verb in (w.lease(repo, "run-b", 502).acquire, a.check, a.read):
            with pytest.raises(WorkspaceLeaseError) as exc:
                verb()
            assert exc.value.reason in reason.split("|"), (name, exc.value.reason)
        assert sha(a.lease_path) == before, name
        assert len(lines(a.directory / "lease.log.jsonl")) == 1, name

    with pytest.raises(WorkspaceLeaseError) as exc:
        parse("text, not bytes")
    assert exc.value.reason == "malformed"

    # The two integer bounds, literal: 2**40 generations and 2**22 (Linux's
    # largest pid_max) as a pid are the last values read.
    for key, last in (("generation", 2**40), ("holder_pid", 2**22)):
        a.lease_path.write_text(json.dumps({**doc, key: last}), encoding="ascii")
        assert getattr(a.read(), key) == last
        a.lease_path.write_text(json.dumps({**doc, key: last + 1}), encoding="ascii")
        with pytest.raises(WorkspaceLeaseError) as exc:
            a.read()
        assert exc.value.reason == "invalid", key

    # A holder at the last pid is judged with the real `os.kill` probe (an
    # injected one would hide an OverflowError) and is simply gone.
    from hoh.wslease import WorkspaceLease
    a.lease_path.write_text(json.dumps({**doc, "holder_pid": 2**22}), encoding="ascii")
    real = WorkspaceLease(repo, "run-b", clock=lambda: w.now, boot=lambda: w.boot_id, host="h",
                          pid=502, pidns=lambda: "pidns:[1]", start=lambda p: "")
    assert real.acquire().generation == 2
    a.lease_path.write_bytes(raw)                              # back to the good record, pid 501 (dead)

    a.lease_path.write_text(json.dumps({**doc, "generation": 2**40}), encoding="ascii")
    full = a.lease_path.read_bytes()                           # the last generation is read ...
    with pytest.raises(WorkspaceLeaseError) as exc:            # ... and is never counted past
        w.lease(repo, "run-b", 502).acquire()
    assert exc.value.reason == "invalid" and a.lease_path.read_bytes() == full
    a.lease_path.write_text(json.dumps({**doc, "released_reason": "x" * 512}), encoding="ascii")
    assert len(a.read().released_reason) == 512                # the text bound is 512, literally

    # The boundary and the control: exactly 16384 bytes is looked at, and good.
    a.lease_path.write_bytes(raw + b" " * (16384 - len(raw)))
    assert a.read() == good
    assert w.lease(repo, "run-b", 502).acquire().generation == 2

    # Files that are not regular files with one name. Whatever stood at the
    # name is renamed away (never removed) before the next odd file is put there.
    other = w.lease(repo, "run-c", 503)
    path = other.lease_path
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    parked = iter(range(100))

    def park(target):
        os.rename(target, tmp_path / f"parked-{next(parked)}")

    def refused(label):
        with pytest.raises(WorkspaceLeaseError) as exc:
            other.acquire()
        assert exc.value.reason == "unreadable", (label, exc.value.reason)
        with pytest.raises(WorkspaceLeaseError):
            other.read()

    park(path)
    os.symlink(outside, path)                                  # a link to a good file
    refused("link")
    assert outside.read_bytes() == raw
    park(path)
    os.symlink(tmp_path / "nowhere.json", path)                # a dangling link
    refused("dangling")
    park(path)
    path.mkdir()                                               # a directory
    refused("directory")
    park(path)
    os.link(outside, path)                                     # a second name
    refused("second name")
    park(path)
    os.mkfifo(path)                                            # a FIFO must not hang the open
    signal.alarm(20)
    try:
        refused("fifo")
    finally:
        signal.alarm(0)
    park(path)
    if os.geteuid() != 0:                                      # (an ordinary user runs this)
        path.write_bytes(raw)
        path.chmod(0)                                          # unreadable by its owner
        refused("mode 000")
        path.chmod(0o600)
        park(path)

    # The log beside the lease: a link (the line would leave the directory), a
    # FIFO, a directory, a second name. `acquire` and `release` refuse BEFORE
    # the lease is touched, so a lease never changes with its log unusable.
    holder = w.lease(repo, "run-d", 504)
    holder.lease_path.write_bytes(raw)                         # a dead holder's record (pid 501)
    log = holder.directory / "lease.log.jsonl"
    park(log)
    keep = sha(holder.lease_path)
    for label in ("link", "fifo", "directory", "second name"):
        if label == "link":
            os.symlink(outside, log)
        elif label == "fifo":
            os.mkfifo(log)
        elif label == "directory":
            log.mkdir()
        else:
            os.link(outside, log)
        with pytest.raises(WorkspaceLeaseError) as exc:
            holder.acquire()
        assert exc.value.reason == "unreadable", (label, exc.value.reason)
        assert sha(holder.lease_path) == keep, label
        assert outside.read_bytes() == raw, f"nothing was appended through the {label}"
        park(log)
    good = holder.acquire()                                    # the control: no log in the way
    assert good.generation == 2
    park(log)
    os.symlink(outside, log)
    with pytest.raises(WorkspaceLeaseError):
        holder.release(reason="x")                             # ... nor does `release` write the lease
    assert holder.check() == good and outside.read_bytes() == raw
    park(log)
    park(path)

    # The lease directory itself: a file, a link to another directory.
    directory = other.directory
    park(directory)
    directory.write_bytes(b"")
    refused("directory is a file")
    park(directory)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, directory)
    refused("directory is a link")
    assert list(elsewhere.iterdir()) == [], "nothing was written through the link"


# --- R5 ----------------------------------------------------------------------

def test_the_lock_is_taken_refused_when_busy_and_the_writers_exclude_each_other(tmp_path):
    """Catches an `acquire` that does not take the lock (it succeeds while
    another process holds it), one that waits without end, and a race that lets
    two threads both win."""
    import fcntl
    from hoh.wslease import WorkspaceHeld, WorkspaceLeaseError

    repo = make_repo(tmp_path)
    w = World()
    a = w.lease(repo, "run-a", 601)
    a.acquire()
    a.release(reason="free again")                             # the directory exists, the worktree is free
    fd = os.open(a.directory, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        b = w.lease(repo, "run-b", 602)
        before = sha(a.lease_path)
        signal.alarm(30)                                       # a waiter without end fails here, not hangs
        started = time.monotonic()
        try:
            for verb in (b.acquire, a.renew, lambda: a.release(reason="x")):
                with pytest.raises(WorkspaceLeaseError) as exc:
                    verb()
                assert exc.value.reason == "busy"
        finally:
            signal.alarm(0)
        assert time.monotonic() - started < 10
        assert sha(a.lease_path) == before
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert b.acquire().generation == 2                         # the lock is let go of

    winner = b                                                 # who holds the lease at the top of a round
    for _ in range(6):                                         # four racers, one winner
        handles = [w.lease(repo, f"racer-{i}", 700 + i) for i in range(4)]
        barrier = threading.Barrier(4)
        outcome = []

        def go(h):
            barrier.wait()
            try:
                h.acquire()
                outcome.append("won")
            except WorkspaceHeld:
                outcome.append("held")
            except WorkspaceLeaseError as exc:                # `busy` on a loaded disk is a refusal too
                outcome.append(exc.reason)

        winner.release(reason="next round")
        threads = [threading.Thread(target=go, args=(h,)) for h in handles]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert outcome.count("won") == 1, outcome            # never two, never none
        assert set(outcome) <= {"won", "held", "busy"}, outcome
        winner = next(h for h in handles if h.token is not None)


# --- R6 ----------------------------------------------------------------------

def test_commit_candidate_writes_only_for_the_holder_of_this_worktree(tmp_path):
    """Catches `commit_candidate` committing without the lease, with someone
    else's, with a released or expired one, with the lease of another worktree,
    or with a lease argument it forgot to look at (a falsy one is not
    "absent"); and a refusal that comes after the first `git add -A`."""
    from hoh.workspace import WorkspaceError, commit_candidate, head_commit, snapshot
    from hoh.wslease import WorkspaceLeaseError

    repo = make_repo(tmp_path)
    wt = make_worktree(repo, "wt-two")
    other = make_worktree(repo, "wt-three")
    w = World()
    (wt / "b.py").write_text("print('two')\n", encoding="utf-8")
    candidate = snapshot(wt, "c1")
    head = head_commit(wt)

    def refused(lease, reason=None):
        with pytest.raises(WorkspaceError) as exc:
            commit_candidate(wt, candidate, run_id="r", iteration=1, lease=lease)
        if reason is not None:
            assert isinstance(exc.value, WorkspaceLeaseError) and exc.value.reason == reason
        assert head_commit(wt) == head, "nothing was committed"
        assert git(wt, "diff", "--cached", "--name-only") == "", "nothing staged: refused before add -A"

    mine = w.lease(wt, "run-a", 801, ttl_seconds=60)
    refused(mine, "lost")                                      # never acquired
    mine.acquire()
    refused(w.lease(wt, "run-b", 802), "lost")                 # a handle that is not the holder
    elsewhere = w.lease(other, "run-c", 803)
    elsewhere.acquire()                                        # live, but the lease of another worktree
    refused(elsewhere, "elsewhere")
    for odd in (False, 0, "", "a lease", object()):
        refused(odd)                                           # `if lease:` would let the falsy ones through
    w.advance(60)
    refused(mine, "lost")                                      # run out
    assert mine.acquire().generation == 2
    mine.release(reason="done")
    refused(mine, "lost")                                      # released

    held = w.lease(wt, "run-e", 805)
    held.acquire()
    done = commit_candidate(wt, candidate, run_id="r", iteration=1, lease=held)
    assert done == head_commit(wt) and done != head
    assert git(wt, "rev-parse", "HEAD^{tree}") == candidate.tree_digest

    # The control: no lease at all is the behaviour of every existing caller.
    (wt / "c.py").write_text("print('three')\n", encoding="utf-8")
    second = snapshot(wt, "c2")
    plain = commit_candidate(wt, second, run_id="r", iteration=2)
    assert plain == head_commit(wt) and plain != done

    # A non-holder asking about a clean tree is refused too, not answered.
    third = snapshot(wt, "c3")
    with pytest.raises(WorkspaceLeaseError):
        commit_candidate(wt, third, run_id="r", iteration=3, lease=mine)
    assert commit_candidate(wt, third, run_id="r", iteration=3) == head_commit(wt)


# --- R7 ----------------------------------------------------------------------

def test_two_spellings_of_one_worktree_share_one_lease_and_a_subdirectory_has_none(tmp_path):
    """Catches a lease keyed by the spelling of the path (a symbolic link, `..`,
    a trailing slash would each get a lease of their own), a lease of a
    subdirectory that quietly becomes the outer repository's, and a lease file
    that `git add -A` would sweep into a candidate."""
    from hoh.workspace import WorkspaceError, lease_directory, snapshot, working_tree
    from hoh.wslease import WorkspaceHeld, WorkspaceLeaseError

    repo = make_repo(tmp_path)
    linked = make_worktree(repo, "wt-four")
    link = tmp_path / "alias"
    os.symlink(linked, link)
    w = World()
    a = w.lease(link, "run-a", 901)
    a.acquire()
    for spelling in (linked, str(linked) + "/", tmp_path / "wt-four" / ".." / "wt-four", link):
        assert lease_directory(spelling) == lease_directory(linked)
        with pytest.raises(WorkspaceHeld):
            w.lease(spelling, "run-b", 902).acquire()
    assert a.directory.parent.name == "wt-four", "the linked worktree's own git directory"
    assert ".git" in a.directory.parts and "worktrees" in a.directory.parts

    (linked / "sub").mkdir()
    for odd in (None, 7, b"/tmp", "bad\0name"):               # not a path at all
        with pytest.raises(WorkspaceLeaseError) as exc:
            w.lease(odd, "run-c", 903)
        assert exc.value.reason == "argument", odd
    for bad in (linked / "sub", tmp_path / "missing", tmp_path):
        with pytest.raises(WorkspaceError):
            lease_directory(bad)
        with pytest.raises(WorkspaceLeaseError) as exc:
            w.lease(bad, "run-c", 903)
        assert exc.value.reason == "argument"

    # The lease is not part of any candidate: the tree is the tree of HEAD.
    assert git(linked, "status", "--porcelain") == ""
    assert working_tree(linked) == git(linked, "rev-parse", "HEAD^{tree}")
    assert snapshot(linked, "c1").tree_clean is True


# --- R8 ----------------------------------------------------------------------

def test_arguments_ceilings_and_free_text_are_pinned_by_behaviour(tmp_path):
    """Catches a bound that moved or never existed: the holder grammar (a full
    match, not a prefix), the ttl range, a clock that is not a time, and
    free text that would make the lease's own record unreadable to the next
    handle."""
    from hoh.wslease import WorkspaceLeaseError

    repo = make_repo(tmp_path)
    w = World()

    def refused(**kw):
        with pytest.raises(WorkspaceLeaseError) as exc:
            holder = kw.pop("holder", "run-a")
            w.lease(repo, holder, 1001, **kw)
        assert exc.value.reason == "argument", kw

    for bad in ("", " a", "a ", "a\n", "a/b", "..", "-a", "a" * 129, "ä", None, 7):
        refused(holder=bad)
    for ok in ("a", "a" * 128, "run.a_b-1"):
        w.lease(repo, ok, 1001)
    for bad in (0, -1, True, 1.5, "60", None, 86401):
        refused(ttl_seconds=bad)
    one = w.lease(repo, "run-a", 1002, ttl_seconds=1)
    assert one.acquire().expires_at == "2026-10-12T10:00:01Z"
    one.release(reason="x")
    day = w.lease(repo, "run-a", 1003, ttl_seconds=86400)
    assert day.acquire().expires_at == "2026-10-13T10:00:00Z"
    day.release(reason="x")

    for bad_clock in ("soon", "2026-10-12 10:00:00", "2026-13-01T00:00:00Z", "2026-10-12T10:00:00Z\n", None):
        h = w.lease(repo, "run-a", 1004)
        h.clock = lambda v=bad_clock: v
        before = sha(h.lease_path)
        with pytest.raises(Exception):
            h.acquire()
        assert sha(h.lease_path) == before

    odd = w.lease(repo, "run-a", 1005, host="hä\n" + "x" * 600)
    odd.acquire()
    odd.release(reason="line one\nline two ä")
    reader = w.lease(repo, "run-b", 1006)
    got = reader.read()
    assert got is not None and got.released_at and set(got.released_reason) <= set(map(chr, range(32, 127)))
    assert reader.acquire().generation == 4


# --- R9 ----------------------------------------------------------------------

def test_the_record_and_the_log_are_exactly_the_documented_ones(tmp_path):
    """Catches a record that drifted (a field added, the mode loosened, the
    schema string changed), a temp file left behind, a renew that logs, and a
    takeover that does not say what it took over from (nothing is deleted)."""
    repo = make_repo(tmp_path)
    w = World()
    a = w.lease(repo, "run-a", 1101, ttl_seconds=100)
    a.acquire()
    doc = json.loads(a.lease_path.read_text(encoding="ascii"))
    assert sorted(doc) == sorted([
        "schema", "holder", "generation", "token_sha256", "acquired_at", "renewed_at", "expires_at",
        "holder_pid", "holder_boot_id", "holder_host", "holder_pidns", "holder_start",
        "released_at", "released_reason"])
    assert doc["schema"] == "hoh-workspace-lease/1" and doc["released_at"] is None
    assert doc["holder_pid"] == 1101 and doc["holder_host"] == "h" and doc["holder_boot_id"] == "boot-one-0000"
    assert len(doc["token_sha256"]) == 64 and a.token and doc["token_sha256"] != a.token
    assert a.lease_path.read_text(encoding="ascii").endswith("}\n")
    assert (a.lease_path.stat().st_mode & 0o777) == 0o600
    assert (a.directory.stat().st_mode & 0o777) == 0o700
    a.renew()
    w.live.discard(1101)
    w.lease(repo, "run-b", 1102).acquire()
    names = sorted(p.name for p in a.directory.iterdir())
    assert names == ["lease.json", "lease.log.jsonl"], names
    log = lines(a.directory / "lease.log.jsonl")
    assert [(e["event"], e["holder"], e["generation"]) for e in log] == [
        ("acquire", "run-a", 1), ("acquire", "run-b", 2)], "renew writes no log line"
    assert log[0]["previous"].startswith("no lease") and "no longer exists" in log[1]["previous"]
    assert (a.directory / "lease.log.jsonl").stat().st_mode & 0o777 == 0o600


# --- R10 ---------------------------------------------------------------------

def test_the_module_writes_only_through_durable_io_and_runs_nothing():
    """Catches a second way of writing (a bare `open`, `os.replace`, a rename
    or remove, `Path.write_text`), a process started from the lease module, an
    alias (`from os import unlink as u`) and an import that was not reviewed.
    The writer scan of `tests/test_durable_writers.py` classifies calls by
    kind; this pins the **shape of this prototype's module**: a module built
    differently changes the sets below in the same hunk."""
    path = Path(__file__).resolve().parent.parent / "src" / "hoh" / "wslease.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plain, names = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.asname is None for a in node.names), "an aliased import"
            plain |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert all(a.asname is None for a in node.names), "an aliased import"
            names.setdefault(("." * node.level) + (node.module or ""), set()).update(a.name for a in node.names)
    assert plain == {"errno", "fcntl", "json", "os", "re", "secrets", "socket", "stat", "time"}, sorted(plain)
    assert names == {
        "__future__": {"annotations"},
        "collections.abc": {"Callable", "Iterator"},
        "contextlib": {"contextmanager"},
        "dataclasses": {"asdict", "dataclass", "replace"},
        "datetime": {"UTC", "datetime", "timedelta"},
        "pathlib": {"Path"},
        ".": {"durable_io"},
        ".succession": {"boot_id", "pid_alive", "pid_namespace", "pid_start", "token_digest", "utc_now"},
        ".workspace": {"WorkspaceError", "is_git_repo", "lease_directory"},
    }, names
    os_names = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os"}
    assert os_names <= {"mkdir", "open", "close", "lstat", "getpid", "O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW",
                        "O_CLOEXEC"}, sorted(os_names)
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"open", "exec", "eval", "print", "compile", "__import__", "getattr", "setattr",
                         "vars", "globals", "locals"}, sorted(called)
    methods = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not methods & {"write_text", "write_bytes", "unlink", "rmdir", "rename", "touch",
                          "symlink_to", "hardlink_to", "chmod", "truncate"}, sorted(methods)
