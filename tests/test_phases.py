"""`hoh phase close | status | reopen`: a phase is closed at a commit, for any project (amendment A7).

A phase whose gate is re-measured on the live tree reads open again at the first merge after it
closed, and a project whose verification restarts at every merge never converges. `hoh.phases`
gives every repository the three acts the build tools gave VeriHarness alone: `close` verifies a
closure entry against the blobs at its closing commit and writes a receipt, `status` re-derives
the committed receipt and reads CLOSED with the watch beside it (or the reason it is ignored),
`reopen` records an explicit decision. `checklist.phase_gate(closure=)` is the verdict they share.

Everything runs over small synthetic git repositories in `tmp_path` (the repository this file
lives in is never read: a git-less arena has no history). The nodes drive `hoh.cli.main`, the
way an operator does; the few that call a function say why.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from hoh import checklist, cli
from hoh.checklist import (
    ChecklistItem,
    Criticality,
    GateVerdict,
    ItemEvidence,
    phase_gate,
    reconcile,
)


class _Phases:
    """`hoh.phases`, imported when a node first uses it: on a tree without the module every node
    fails by its own name instead of the file failing to collect."""

    @staticmethod
    def _module():
        import importlib

        return importlib.import_module("hoh.phases")

    def __getattr__(self, name):
        return getattr(self._module(), name)

    def __setattr__(self, name, value):             # monkeypatch must reach the module itself
        setattr(self._module(), name, value)


phases = _Phases()

ROOT = Path(__file__).resolve().parent.parent
GATE = ".hoh/phases/p2"
SNAP = "checklists/p2.json"
EVIDENCE = ("evidence/matrix.json", "evidence/review.json")
X40 = "ab" * 20
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def git(root: Path, *args: str, stdin: str | None = None) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env=_ENV, input=stdin, timeout=60, check=False)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def text(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _items(open_ids=(), verified_ids=("P2-1", "P2-2")) -> list[ChecklistItem]:
    ids = [*verified_ids, *open_ids]
    rows = [ChecklistItem(item_id=i, phase="P2", title=f"title {i}", requirement_ref="R",
                          source_digest="d", criticality=Criticality.P1_PHASE_BLOCKING,
                          evidence_refs=["e"])
            for i in ids]
    rows.append(ChecklistItem(item_id="P3-1", phase="P3", title="later", requirement_ref="R",
                              source_digest="d", criticality=Criticality.P1_PHASE_BLOCKING,
                              evidence_refs=["e"]))
    return rows


def _snapshot(open_ids=(), verified_ids=("P2-1", "P2-2"), generation=1):
    evidence = {i: ItemEvidence(present_refs=["e"], source_digest="d") for i in verified_ids}
    return reconcile(_items(open_ids, verified_ids), evidence=evidence, generation=generation)


def snapshot_text(open_ids=(), verified_ids=("P2-1", "P2-2"), generation=1, **extra) -> str:
    """A complete dump of a sealed snapshot (what the product reads); extra keys ride along."""
    body = json.loads(_snapshot(open_ids, verified_ids, generation).model_dump_json(by_alias=True))
    return text({**body, **extra})


class World:
    """A repository with the shape of a closure: evidence commit E, the records entry X (the
    snapshot and the evidence files), and later commits H1.. . The entry file lives outside the
    repository, as an operator would keep it."""

    def __init__(self, tmp_path: Path, *, snapshot: str | None = None, extra=None,
                 open_ids=(), watch=True) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        self.root.mkdir(parents=True)
        git(self.root, "init", "-q", "-b", "main")
        self.write("src/app.py", "VALUE = 1\n")
        self.write("docs/notes.md", "notes\n")
        self.e = self.commit("E: the evidence was taken here")
        self.write(SNAP, snapshot if snapshot is not None else snapshot_text(open_ids))
        for n, name in enumerate(EVIDENCE):
            self.write(name, text({"n": n}))
        for path, what in (extra or {}).items():
            self.write(path, what)
        self.x = self.commit("X: the records entry")
        self.entry_path = tmp_path / "entry.json"
        self.watch = watch

    def write(self, rel: str, what) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(what, tuple):                       # ("link", target)
            os.symlink(what[1], path)
        else:
            path.write_text(what, encoding="utf-8")
        return path

    def commit(self, message: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def advance(self, n: int = 1, *, path: str = "src/app.py") -> str:
        head = ""
        for k in range(n):
            self.write(path, f"VALUE = {k + 2}\n# {hashlib.sha256(os.urandom(8)).hexdigest()}\n")
            head = self.commit(f"H{k + 1}: later work")
        return head

    def blob(self, rel: str) -> bytes:
        return subprocess.run(["git", "-C", str(self.root), "show", f"{self.x}:{rel}"],
                              capture_output=True, env=_ENV, check=True).stdout

    def entry(self, **over) -> dict:
        snap = self.blob(SNAP)
        body = {"schema": phases.ENTRY_SCHEMA, "phase": "P2", "ref": "O268",
                "closed_at_commit": self.x,
                "snapshot": {"path": SNAP, "file_sha256": sha(snap),
                             "seal": json.loads(snap)["seal"]},
                "evidence": [{"path": p, "file_sha256": sha(self.blob(p))} for p in EVIDENCE],
                "watch": {"evidence_commit": self.e, "paths": ["src"]} if self.watch else None}
        body.update(over)
        return body

    def entry_file(self, body: dict | None = None, **over) -> str:
        """Write the entry outside the repository: the default one with `over` applied, or `body`
        exactly as given."""
        self.entry_path.write_text(text(body if body is not None else self.entry(**over)),
                                   encoding="utf-8")
        return str(self.entry_path)

    def tree(self) -> dict[str, bytes]:
        """Every file of the working tree outside .git, by path (links by their target)."""
        found = {}
        for base, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                p = Path(base) / name
                found[str(p.relative_to(self.root))] = (os.readlink(p).encode() if p.is_symlink()
                                                        else p.read_bytes())
        return found


def drop(w: World, rel: str) -> None:
    """Take a tracked file out of the working tree and the index (git records it)."""
    git(w.root, "rm", "-q", "-f", "--", rel)


def run(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def phase(capsys, w: World, verb: str, *argv: str) -> tuple[int, str, str]:
    return run(capsys, "phase", verb, *argv, "--repo", str(w.root))


def closed_world(capsys, tmp_path, **kw) -> World:
    """close, commit the receipt: the closure stands at HEAD."""
    w = World(tmp_path, **kw)
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert code == 0, err
    w.commit("R: the closure receipt")
    return w


# --------------------------------------------------------------------------- #
# the verdict: checklist.phase_gate(closure=)
# --------------------------------------------------------------------------- #

def _ok() -> ItemEvidence:
    return ItemEvidence(present_refs=["e"], source_digest="d")


def test_a_closure_reads_the_phase_closed_whatever_the_live_snapshot_says():
    """The point of the whole slice at the level of the verdict: with a verified closure the phase
    is CLOSED at its commit although the live snapshot has open blockers, and those blockers are
    the watch -- visible, never a wall. Catches a closure that is ignored, and one that hides the
    live state."""
    live = reconcile(_items(open_ids=("P2-3", "P2-4")), evidence={"P2-1": _ok(), "P2-2": _ok()},
                     generation=2)
    assert phase_gate(live, "P2").closed is False
    closure = checklist.Closure(phase="P2", closed_at=X40, ref="O268", watch=["evidence stale since abc"])
    verdict = phase_gate(live, "P2", closure=closure)
    assert isinstance(verdict, GateVerdict)
    assert verdict.closed is True and verdict.blockers == []
    assert (verdict.closed_at, verdict.closure_ref) == (X40, "O268")
    assert verdict.derived_from_evidence is False
    handed = phase_gate(live, "P2", closure=closure, evidence={"P2-1": _ok(), "P2-2": _ok()})
    assert handed.closed is True and handed.derived_from_evidence is False, "the closure closed it"
    assert verdict.watch[0] == "evidence stale since abc"
    assert [w.split(":")[1].strip() for w in verdict.watch[1:]] == ["P2-3", "P2-4"]
    assert all(w.startswith("live: ") for w in verdict.watch[1:])
    assert verdict.reading() == f"CLOSED at {X40[:12]} (O268); watch: " + "; ".join(verdict.watch)


def test_without_a_closure_the_verdict_is_what_it_was():
    """Compatibility: a caller that hands in no closure gets the old verdict, field for field, and
    the new fields stay empty. Catches a default that changes every existing gate."""
    live = _snapshot(open_ids=("P2-3",))
    plain = phase_gate(live, "P2")
    assert plain.closed is False and [b.item_id for b in plain.blockers] == ["P2-3"]
    assert (plain.closed_at, plain.closure_ref, plain.watch) == (None, None, [])
    assert plain.reading() == "OPEN: 1 blocker(s)"
    assert phase_gate(_snapshot(), "P2").reading() == "closed (live gate, no closure record)"


def test_a_closure_of_another_phase_closes_nothing():
    """A caller that hands in the wrong phase's closure has not verified the one it needs: the
    verdict is not closed and says so in a blocker of its own."""
    closure = checklist.Closure(phase="P3", closed_at=X40, ref="O1")
    verdict = phase_gate(_snapshot(open_ids=("P2-3",)), "P2", closure=closure)
    assert verdict.closed is False and verdict.closed_at is None
    assert "(closure)" in [b.item_id for b in verdict.blockers]
    clean = phase_gate(_snapshot(), "P2", closure=closure)
    assert clean.closed is False and [b.item_id for b in clean.blockers] == ["(closure)"]


@pytest.mark.parametrize("closed_at", [
    "ab" * 19, "ab" * 21, X40.upper(), "xy" * 20, "", X40[:12]],
    ids=["39-digits", "41-digits", "upper-case", "not-hex", "empty", "abbreviated"])
def test_a_closure_names_one_full_lower_case_commit(closed_at):
    """The commit a verdict says CLOSED at is one full id: an abbreviation, another case or a
    trailing newline is a different spelling of nothing."""
    with pytest.raises(ValidationError):
        checklist.Closure(phase="P2", closed_at=closed_at, ref="O1")
    checklist.Closure(phase="P2", closed_at=X40, ref="O1")


def test_the_watch_lines_are_bounded_at_every_hand():
    """A closure carries at most 8 lines of its own watch (the 9th is refused), and the live
    blockers are cut at 8 with the rest counted -- exactly at the boundary, not one off."""
    checklist.Closure(phase="P2", closed_at=X40, ref="O1", watch=["w"] * 8)
    with pytest.raises(ValidationError):
        checklist.Closure(phase="P2", closed_at=X40, ref="O1", watch=["w"] * 9)
    closure = checklist.Closure(phase="P2", closed_at=X40, ref="O1")
    eight = phase_gate(_snapshot(open_ids=tuple(f"B{n}" for n in range(8))), "P2", closure=closure)
    assert len(eight.watch) == 8 and not any("more" in w for w in eight.watch)
    twelve = phase_gate(_snapshot(open_ids=tuple(f"B{n:02d}" for n in range(12))), "P2",
                        closure=closure)
    assert len(twelve.watch) == 9 and twelve.watch[-1] == "live: and 4 more"


# --------------------------------------------------------------------------- #
# close: the receipt is written only for an entry the blobs at X bear out
# --------------------------------------------------------------------------- #

def test_close_writes_a_receipt_and_status_reads_it_once_committed(capsys, tmp_path):
    """The whole path an operator takes, through the command: close writes the receipt (not
    committed: the closure does not stand yet), commit it, status reads CLOSED at X with the
    watch. Catches a receipt that is trusted from the working tree and a status that cannot read
    what close wrote."""
    w = World(tmp_path)
    entry = w.entry_file()
    code, out, err = phase(capsys, w, "close", "P2", "--record", entry)
    assert code == 0 and err == "", err
    assert f"P2 closed at {w.x[:12]} (O268)" in out and "commit the receipt" in out
    receipt = json.loads((w.root / GATE / "CLOSURE.json").read_text())
    expected = {**w.entry(), "schema": phases.RECEIPT_SCHEMA}
    by = receipt.pop("closed_by")
    assert receipt == expected
    assert by["entry_sha256"] == sha(Path(entry).read_bytes())
    assert by["head"] == w.x and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", by["at_utc"])
    code, out, err = phase(capsys, w, "status", "P2")
    assert code == 1 and "not committed" in out and "OPEN" in out
    w.commit("R: the closure receipt")
    code, out, err = phase(capsys, w, "status", "P2")
    assert code == 0 and err == ""
    assert out == (f"P2 CLOSED at {w.x[:12]} (O268); watch: evidence unchanged since "
                   f"{w.e[:12]}\n")
    code, out, _ = phase(capsys, w, "status", "P2", "--json")
    state = json.loads(out)["phases"][0]
    assert (code, state["state"], state["closed_at"], state["closure_ref"]) == (
        0, "CLOSED", w.x, "O268")


def test_a_closed_phase_stays_closed_through_later_commits(capsys, tmp_path):
    """The convergence the operator asked for, through the command: after the closure the source
    moves, the live snapshot is re-reconciled with open blockers, and the phase still reads CLOSED
    at X -- with the stale evidence and the live blockers as the watch, never as a reopening.
    Catches a status that re-measures the phase instead of reading the closure."""
    w = closed_world(capsys, tmp_path)
    head = w.advance(3)
    w.write(SNAP, snapshot_text(open_ids=("P2-3",), generation=2))
    w.commit("H4: the checklist is reconciled again, one new blocker")
    code, out, err = phase(capsys, w, "status", "P2")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"P2 CLOSED at {w.x[:12]} (O268); watch: ")
    first = git(w.root, "log", "--reverse", "--format=%H", f"{w.e}..HEAD", "--", "src").split()[0]
    assert f"evidence stale since {first[:12]}, re-run pending" in lines[0]
    assert "live: P2-3: " in lines[0] and head != first
    assert phase(capsys, w, "status")[:2] == (0, out)           # no phase argument: same phase


def test_close_reads_the_blobs_at_x_not_at_head(capsys, tmp_path):
    """The receipt is bound to the closing commit: a snapshot edited after X changes nothing, and
    an entry that carries the digest of the edited file is refused. Catches a reader of HEAD."""
    w = World(tmp_path)
    entry = w.entry_file()                           # the digests of the files AT X
    edited = snapshot_text(open_ids=("P2-9",), generation=7)
    w.write(SNAP, edited)
    w.commit("H1: the snapshot is edited after X")
    code, _, err = phase(capsys, w, "close", "P2", "--record", entry)
    assert code == 0, err
    other = World(tmp_path / "other")
    other.write(SNAP, edited)
    other.commit("H1: the snapshot is edited after X")
    claim = {"path": SNAP, "file_sha256": sha(edited), "seal": json.loads(edited)["seal"]}
    code, out, err = phase(capsys, other, "close", "P2", "--record", other.entry_file(snapshot=claim))
    assert (code, out) == (1, "") and "the checklist snapshot at" in err and "has sha256" in err
    assert not (other.root / GATE).exists()


def _short(w, e):
    e["closed_at_commit"] = w.x[:12]


def _upper(w, e):
    e["closed_at_commit"] = w.x.upper()


def _tag_object(w, e):
    git(w.root, "tag", "-a", "-m", "t", "v1", w.x)
    e["closed_at_commit"] = git(w.root, "rev-parse", "v1")


def _off_line(w, e):
    side = git(w.root, "commit-tree", f"{w.x}^{{tree}}", "-p", w.e, "-m", "off line")
    e["closed_at_commit"] = side


def _unknown_commit(w, e):
    e["closed_at_commit"] = "0" * 40


def _snapshot_digest(w, e):
    e["snapshot"]["file_sha256"] = "0" * 64


def _seal(w, e):
    e["snapshot"]["seal"] = "1" * 64


def _evidence_digest(w, e):
    e["evidence"][0]["file_sha256"] = "2" * 64


def _evidence_twice(w, e):
    e["evidence"].append(dict(e["evidence"][0]))


def _evidence_is_the_snapshot(w, e):
    e["evidence"][0] = {"path": SNAP, "file_sha256": e["snapshot"]["file_sha256"]}


def _evidence_missing(w, e):
    e["evidence"][0] = {"path": "evidence/absent.json", "file_sha256": "3" * 64}


def _evidence_directory(w, e):
    e["evidence"][0] = {"path": "evidence", "file_sha256": "4" * 64}


def _unknown_key(w, e):
    e["note"] = "x"


def _missing_key(w, e):
    del e["ref"]


def _other_schema(w, e):
    e["schema"] = phases.RECEIPT_SCHEMA


def _other_phase(w, e):
    e["phase"] = "P3"


def _bad_ref(w, e):
    e["ref"] = "O 268"


def _watch_not_leading_to_x(w, e):
    e["watch"]["evidence_commit"] = git(w.root, "commit-tree", f"{w.e}^{{tree}}", "-m", "elsewhere")


def _watch_path_dotdot(w, e):
    e["watch"]["paths"] = ["src/../etc"]


def _watch_twice(w, e):
    e["watch"]["paths"] = ["src", "src"]


def _watch_empty(w, e):
    e["watch"]["paths"] = []


def _watch_shape(w, e):
    e["watch"] = {"evidence_commit": w.e}


@pytest.mark.parametrize("mutate,reason", [
    (_short, "`closed_at_commit` is missing or not in its shape"),
    (_upper, "`closed_at_commit` is missing or not in its shape"),
    (_tag_object, "is not one full commit id"),
    (_off_line, "is not an ancestor of"),
    (_unknown_commit, "is not a commit of this repository"),
    (_snapshot_digest, "the checklist snapshot at"),
    (_seal, "is not sealed"),
    (_evidence_digest, "the evidence file evidence/matrix.json at"),
    (_evidence_twice, "not all different files"),
    (_evidence_is_the_snapshot, "not all different files"),
    (_evidence_missing, "is not a file of"),
    (_evidence_directory, "is not a regular file"),
    (_unknown_key, "does not hold exactly"),
    (_missing_key, "does not hold exactly"),
    (_other_schema, "not hoh-phase-closure-entry/1"),
    (_other_phase, "is about 'P3', not P2"),
    (_bad_ref, "`ref` is missing or not in its shape"),
    (_watch_not_leading_to_x, "does not lead to the closing commit"),
    (_watch_path_dotdot, "is not a repository path"),
    (_watch_empty, "`watch.paths` is not a list of 1 to 64 paths"),
    (_watch_twice, "`watch.paths` names a path twice"),
    (_watch_shape, "`watch` does not hold exactly"),
])
def test_close_refuses_an_entry_the_blobs_at_x_do_not_bear_out(capsys, tmp_path, mutate, reason):
    """Every claim of the entry is re-derived from the blobs at X; one that is not borne out
    writes nothing and says which. The reasons are pinned, so a check that is dropped fails by
    its own name instead of by the next check along."""
    w = World(tmp_path)
    entry = w.entry()
    mutate(w, entry)
    before = w.tree()
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(body=entry))
    assert (code, out) == (1, "") and err.startswith("phase close refused: ") and reason in err, err
    after = w.tree()
    assert after == before and not (w.root / GATE).exists()


def test_close_refuses_a_snapshot_that_does_not_close_the_phase(capsys, tmp_path):
    """The snapshot at X must close the phase by `checklist.phase_gate`: one blocking item that is
    not VERIFIED or WAIVED, a snapshot with the phase absent, or one edited after it was sealed,
    is refused with the blocker named. Closing lifts no item (A7 rule 2)."""
    for snapshot, reason in ((snapshot_text(open_ids=("P2-3",)), "P2-3"),
                             (snapshot_text(verified_ids=()), "not in this snapshot at all"),
                             (snapshot_text().replace('"title P2-1"', '"edited"'),
                              "is not sealed")):
        w = World(tmp_path / sha(snapshot)[:8], snapshot=snapshot)
        code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
        assert code == 1 and reason in err, (reason, err)
        assert not (w.root / GATE).exists()
    w = World(tmp_path / "other", snapshot=snapshot_text())
    code, _, err = phase(capsys, w, "close", "P3", "--record", w.entry_file(phase="P3"))
    assert code == 1 and "phase P3 is not closed by the snapshot" in err and "P3-1" in err


def test_close_reads_a_snapshot_file_that_carries_more_than_a_snapshot(capsys, tmp_path):
    """A file that holds the snapshot's `items`, `generation` and `seal` plus other keys (a
    programme's own report around it) is read for those three; the seal is still computed from the
    snapshot alone. Catches a reader that refuses a wrapper, and one that trusts it."""
    w = World(tmp_path, snapshot=snapshot_text(what="a report", gates={"P2": {"closed": True}}))
    assert phase(capsys, w, "close", "P2", "--record", w.entry_file())[0] == 0


def test_close_refuses_an_entry_it_cannot_read(capsys, tmp_path):
    """Fail closed on the entry file: missing, a directory, a FIFO, a link, non-UTF-8, UTF-16,
    a lone surrogate, NUL, deep nesting, not an object, empty -- each refuses, none raises."""
    w = World(tmp_path)
    good = text(w.entry())
    cases = {
        "missing": None, "directory": "dir", "fifo": "fifo", "link": "link",
        "latin1": good.replace("O268", "\xe9").encode("latin-1"),
        "utf16": good.encode("utf-16"),
        "surrogate": good.replace('"O268"', '"\\ud800"').encode(),
        "nul": good.encode().replace(b"O268", b"O\x00268"),
        "deep": b"[" * 200000 + b"]" * 200000,
        "list": b"[]", "empty": b"", "string": b'"x"',
    }
    for name, content in cases.items():
        path = tmp_path / f"e-{name}"
        if content is None:
            pass
        elif content == "dir":
            path.mkdir()
        elif content == "fifo":
            os.mkfifo(path)
        elif content == "link":
            (tmp_path / "real.json").write_text(good)
            os.symlink(tmp_path / "real.json", path)
        else:
            path.write_bytes(content)
        code, out, err = phase(capsys, w, "close", "P2", "--record", str(path))
        assert (code, out) == (1, ""), name
        assert err.startswith("phase close refused: "), (name, err)
        assert not (w.root / GATE).exists(), name
    code, _, err = phase(capsys, w, "close", "P2", "--record", str(tmp_path / "e\x00x"))
    assert code == 1 and "cannot be opened" in err


def test_the_entry_is_read_up_to_the_ceiling_and_not_one_byte_more(capsys, tmp_path):
    """MAX_BYTES is exact: an entry padded with blanks to exactly that many bytes is read and
    closes the phase, one byte more is refused. A mutant of the bound fails one side."""
    w = World(tmp_path)
    body = text(w.entry())
    exact = body + " " * (phases.MAX_BYTES - len(body.encode()))
    assert len(exact.encode()) == phases.MAX_BYTES
    w.entry_path.write_text(exact)
    assert phase(capsys, w, "close", "P2", "--record", str(w.entry_path))[0] == 0
    big = World(tmp_path / "big")
    big.entry_path.write_text(exact + " ")
    code, _, err = phase(capsys, big, "close", "P2", "--record", str(big.entry_path))
    assert code == 1 and f"larger than {phases.MAX_BYTES} bytes" in err


def test_a_blob_is_read_up_to_the_ceiling_and_not_one_byte_more(capsys, tmp_path):
    """The same ceiling on a blob at X: a snapshot padded to exactly MAX_BYTES is read, one byte
    more is refused before it is read."""
    body = snapshot_text()
    exact = body + " " * (phases.MAX_BYTES - len(body.encode()))
    w = World(tmp_path / "at", snapshot=exact)
    assert phase(capsys, w, "close", "P2", "--record", w.entry_file())[0] == 0
    w = World(tmp_path / "over", snapshot=exact + " ")
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert code == 1 and f"larger than {phases.MAX_BYTES} bytes" in err


@pytest.mark.parametrize("count,ok", [(64, True), (65, False)])
def test_the_evidence_files_of_an_entry_are_bounded_exactly(capsys, tmp_path, count, ok):
    """MAX_FILES is exact for evidence files: 64 close, 65 are refused (never cut to 64)."""
    extra = {f"evidence/f{n:02d}.json": text({"n": n}) for n in range(count)}
    w = World(tmp_path, extra=extra)
    rows = [{"path": n, "file_sha256": sha(w.blob(n))} for n in sorted(extra)]
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(evidence=rows))
    assert (code == 0) is ok, err
    assert ok or "`evidence` is not a list of at most 64 files" in err


@pytest.mark.parametrize("count,ok", [(64, True), (65, False)])
def test_the_watch_paths_of_an_entry_are_bounded_exactly(capsys, tmp_path, count, ok):
    """MAX_FILES is exact for the watched paths of an entry."""
    w = World(tmp_path)
    watch = {"evidence_commit": w.e, "paths": [f"src{n}" for n in range(count)]}
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(watch=watch))
    assert (code == 0) is ok, err
    assert ok or "`watch.paths` is not a list of 1 to 64 paths" in err


def test_a_receipt_is_never_replaced_and_a_second_close_is_quiet(capsys, tmp_path):
    """The terminal record is quiet: a second close refuses and leaves every byte as it was --
    with the receipt only on disk, committed, or committed and gone from the working tree.
    Catches a replace, and a re-write of the same bytes with a new clock."""
    w = World(tmp_path)
    entry = w.entry_file()
    assert phase(capsys, w, "close", "P2", "--record", entry)[0] == 0
    before = w.tree()
    for stage in ("on disk", "committed", "committed, gone from the working tree"):
        if stage == "committed":
            w.commit("R: the closure receipt")
        elif stage != "on disk":
            drop(w, GATE + "/CLOSURE.json")
            before = w.tree()
        code, out, err = phase(capsys, w, "close", "P2", "--record", entry)
        assert (code, out) == (1, "") and "already exists: a receipt is never replaced" in err, stage
        assert w.tree() == before, stage


def test_close_judges_a_repository_of_its_own_only(capsys, tmp_path):
    """`git` answers for whatever repository lies above a directory: a tree copied without its
    .git, or a subdirectory, is not judged by the history above it."""
    w = World(tmp_path)
    entry = w.entry_file()
    inner = w.root / "copy"
    shutil.copytree(w.root, inner, ignore=shutil.ignore_patterns(".git", "copy"))
    sub = w.root / "src"
    for where in (inner, sub):
        for argv in (("close", "P2", "--record", entry), ("reopen", "P2", "--request", "x", "--reason", "r")):
            code, out, err = run(capsys, "phase", *argv, "--repo", str(where))
            assert (code, out) == (1, ""), argv
            assert "is not the top of a git repository of its own" in err
        code, out, err = run(capsys, "phase", "status", "P2", "--repo", str(where))
        assert (code, out) == (2, "") and "git repository of its own" in err
    code, _, err = run(capsys, "phase", "status", "P2", "--repo", str(tmp_path / "absent"))
    assert code == 2 and "is not a directory" in err


@pytest.mark.parametrize("gate", ["../x", "/abs", ".git/x", "a//b", "a/./b", "-x", "a b", "a\\b",
                                  "p2/", "é", "a:b", "a*b", "", "a/" + "b" * 121])
def test_a_gate_directory_is_a_plain_repository_path(capsys, tmp_path, gate):
    """--gate-dir is a path inside the repository in one shape; nothing else is written to or
    read from, whatever the spelling (a leading dash, a glob, a dot part, a .git part)."""
    w = World(tmp_path)
    before = w.tree()
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(),
                           f"--gate-dir={gate}")
    assert (code, out) == (1, "") and "--gate-dir" in err, err
    assert w.tree() == before
    assert phase(capsys, w, "status", "P2", f"--gate-dir={gate}")[0] == 2


def test_the_gate_directory_is_never_followed_through_a_link(capsys, tmp_path):
    """A link on the way to the receipt would put a record outside the repository: refused, and
    nothing lands beyond the link."""
    w = World(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, w.root / ".hoh")
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert (code, out) == (1, "") and "is a link or not a directory" in err
    assert list(outside.iterdir()) == []


def test_a_gate_directory_other_than_the_default_is_used_where_it_is_named(capsys, tmp_path):
    """--gate-dir moves the records: the receipt is written there, status finds it there and
    nowhere else, and the default directory is not touched."""
    w = World(tmp_path)
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(),
                         "--gate-dir", "records/p2-gate")
    assert code == 0, err
    assert (w.root / "records/p2-gate/CLOSURE.json").is_file() and not (w.root / ".hoh").exists()
    w.commit("R")
    assert phase(capsys, w, "status", "P2", "--gate-dir", "records/p2-gate")[0] == 0
    code, out, _ = phase(capsys, w, "status", "P2")
    assert code == 1 and out.startswith("P2 OPEN: no closure record")
    assert phase(capsys, w, "status", "--gate-dir", "records/p2-gate")[0] == 2


@pytest.mark.parametrize("spelling", ["P2", "p2", "P02", " p02 "])
def test_a_phase_is_spelt_as_a_person_writes_it(capsys, tmp_path, spelling):
    """P2, p2 and P02 are one phase and one directory; a name that is not a phase refuses."""
    w = World(tmp_path)
    assert phase(capsys, w, "close", spelling, "--record", w.entry_file())[0] == 0
    assert (w.root / GATE / "CLOSURE.json").is_file()


@pytest.mark.parametrize("name", ["P100", "P", "Q2", "2", "P-1", "P2x", "P2٣"])
def test_a_name_that_is_not_a_phase_refuses(capsys, tmp_path, name):
    w = World(tmp_path)
    code, _, err = phase(capsys, w, "close", name, "--record", w.entry_file())
    assert code == 1 and "is not a phase name" in err
    assert phase(capsys, w, "status", name)[0] == 2


def test_a_receipt_that_cannot_be_written_is_refused_and_leaves_nothing(capsys, tmp_path,
                                                                        monkeypatch):
    """The receipt is the terminal record: when it cannot be written the command says so and
    exits 1 with nothing in place; when only the directory sync failed it is in place and the
    command warns (the closure stands, the operator is told)."""
    import errno

    from hoh import durable_io

    w = World(tmp_path)

    def full(path, payload, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(phases, "durable_create", full)
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert (code, out) == (1, "") and "cannot be created" in err
    assert not (w.root / GATE / "CLOSURE.json").exists()

    def unsynced(path, payload, **kw):
        durable_io.durable_create(path, payload, **kw)
        raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "I/O error"))
    monkeypatch.setattr(phases, "durable_create", unsynced)
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert code == 0 and "directory entry may not be durable" in err
    assert (w.root / GATE / "CLOSURE.json").is_file()


def test_every_git_call_is_bounded_and_a_hang_is_a_refusal(capsys, tmp_path, monkeypatch):
    """GIT_TIMEOUT is passed to every git call (pinned at 60) and a call that does not return is
    a refusal, never a wait and never a traceback."""
    import types

    w = World(tmp_path)
    entry = w.entry_file()
    seen = []
    real = subprocess.run

    def spy(argv, **kw):
        seen.append(kw.get("timeout"))
        return real(argv, **kw)

    def shim(run):
        return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                     DEVNULL=subprocess.DEVNULL)
    monkeypatch.setattr(phases, "subprocess", shim(spy))
    assert phases.GIT_TIMEOUT == 60
    assert phase(capsys, w, "status", "P2")[0] == 1
    assert seen and set(seen) == {60}

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    monkeypatch.setattr(phases, "subprocess", shim(hang))
    code, out, err = phase(capsys, w, "status", "P2")
    assert (code, out) == (2, "") and "git could not be run (TimeoutExpired)" in err
    code, out, err = phase(capsys, w, "close", "P2", "--record", entry)
    assert (code, out) == (1, "") and "git could not be run" in err


# --------------------------------------------------------------------------- #
# status: a receipt is believed only after it is re-derived, every time
# --------------------------------------------------------------------------- #

def _receipt_edit(w, fn):
    path = w.root / GATE / "CLOSURE.json"
    body = json.loads(path.read_text())
    fn(body)
    path.write_text(text(body))
    w.commit("edit the receipt")


def _x_moved_to_the_evidence_commit(w):
    _receipt_edit(w, lambda b: b.update(closed_at_commit=w.e))


def _digest_moved(w):
    _receipt_edit(w, lambda b: b["evidence"][0].update(file_sha256="5" * 64))


def _extra_key(w):
    _receipt_edit(w, lambda b: b.update(extra=1))


def _not_an_object(w):
    (w.root / GATE / "CLOSURE.json").write_text("[]\n")
    w.commit("receipt is a list")


def _no_closed_by(w):
    _receipt_edit(w, lambda b: b.pop("closed_by"))


def _entry_sha_shape(w):
    _receipt_edit(w, lambda b: b["closed_by"].update(entry_sha256="xyz"))


def _head_shape(w):
    _receipt_edit(w, lambda b: b["closed_by"].update(head=b["closed_by"]["head"][:12]))


def _closed_by_shape(w):
    _receipt_edit(w, lambda b: b["closed_by"].update(at_utc="yesterday"))


def _seal_edit(w):
    _receipt_edit(w, lambda b: b["snapshot"].update(seal="6" * 64))


def _working_tree_differs(w):
    (w.root / GATE / "CLOSURE.json").write_text("{}\n")


def _tools_record(w):
    # the record tools/build_plan.py reads for VeriHarness's own phase: another schema, not ours
    (w.root / GATE / "CLOSURE.json").write_text(text({
        "what": "Closure record of phase P2", "record_version": 1, "phase": "P2",
        "closure_entry": "O268", "closed_at_commit": w.x, "instruments": {}}))
    w.commit("a record in the tools' format")


def _receipt_is_a_link(w):
    drop(w, GATE + "/CLOSURE.json")
    w.write(GATE + "/CLOSURE.json", ("link", "../../../src/app.py"))
    w.commit("receipt is a link")


def _receipt_is_not_json(w):
    (w.root / GATE / "CLOSURE.json").write_bytes(b"\xff\xfe not json")
    w.commit("garbage")


def _history_rewritten(w):
    # HEAD moves to a line that does not contain X: the receipt is committed there unchanged
    git(w.root, "checkout", "-q", "--orphan", "other")
    git(w.root, "commit", "-q", "--allow-empty", "-m", "an unrelated root with the same files")


@pytest.mark.parametrize("break_it,reason", [
    (_x_moved_to_the_evidence_commit, "is not a file of"),
    (_digest_moved, "the evidence file evidence/matrix.json at"),
    (_extra_key, "does not hold exactly"),
    (_not_an_object, "is not a JSON object"),
    (_no_closed_by, "does not hold exactly"),
    (_closed_by_shape, "`closed_by.at_utc` is missing or not in its shape"),
    (_entry_sha_shape, "`closed_by.entry_sha256` is missing or not in its shape"),
    (_head_shape, "`closed_by.head` is missing or not in its shape"),
    (_seal_edit, "is not sealed"),
    (_working_tree_differs, "differs from the one committed"),
    (_tools_record, "the record carries schema 'None', not hoh-phase-closure/1"),
    (_receipt_is_a_link, "is not a regular file"),
    (_receipt_is_not_json, "is not UTF-8 JSON"),
    (_history_rewritten, "is not an ancestor of"),
])
def test_a_receipt_that_does_not_verify_is_ignored_with_its_reason(capsys, tmp_path, break_it,
                                                                   reason):
    """Fail closed: whatever is wrong with the receipt -- a digest, a seal, a key, a link, a
    working-tree edit, a history that no longer holds X -- the phase is not closed by it, the
    command exits 1, and the line says why. Nothing is repaired or written."""
    w = closed_world(capsys, tmp_path)
    break_it(w)
    before = w.tree()
    code, out, err = phase(capsys, w, "status", "P2")
    assert code == 1 and err == ""
    assert out.startswith("P2 OPEN: closure record ignored: ") and reason in out, out
    assert w.tree() == before


def test_a_phase_with_no_receipt_is_open_and_a_receipt_only_on_disk_is_not_a_closure(capsys,
                                                                                     tmp_path):
    """No record: OPEN. A record that exists only in the working tree: OPEN, and the line says to
    commit it -- a closure is read from HEAD."""
    w = World(tmp_path)
    assert phase(capsys, w, "status", "P2") == (1, "P2 OPEN: no closure record\n", "")
    phase(capsys, w, "close", "P2", "--record", w.entry_file())
    code, out, _ = phase(capsys, w, "status", "P2")
    assert code == 1 and "exists in the working tree but is not committed" in out


def test_status_without_a_phase_lists_every_recorded_phase(capsys, tmp_path):
    """Without an argument every phase with a directory under .hoh/phases is read, in phase
    order, and the exit is 0 only if there is at least one and all are closed; no record at all
    says so and exits 1 (a repository with none has no closed phase)."""
    w = World(tmp_path)
    assert phase(capsys, w, "status") == (1, "no phase has a closure record\n", "")
    phase(capsys, w, "close", "P2", "--record", w.entry_file())
    w.write(".hoh/phases/p10/NOTE", "x\n")
    w.write(".hoh/phases/notaphase/NOTE", "x\n")
    w.write(".hoh/phases/p3/NOTE", "x\n")
    w.commit("R")
    code, out, _ = phase(capsys, w, "status")
    assert code == 1
    assert [ln.split(" ")[0] for ln in out.splitlines()] == ["P2", "P3", "P10"]
    assert out.splitlines()[0].startswith("P2 CLOSED at")
    assert out.splitlines()[1] == "P3 OPEN: no closure record"


def test_status_writes_nothing(capsys, tmp_path):
    """status is a read: the working tree and the index are byte for byte as they were, whatever
    it found. Catches a status that repairs, refreshes or records."""
    w = closed_world(capsys, tmp_path)
    w.advance(2)
    before = (w.tree(), git(w.root, "status", "--porcelain=v1"), git(w.root, "rev-parse", "HEAD"))
    for argv in (("P2",), (), ("P2", "--json"), ("P3",)):
        phase(capsys, w, "status", *argv)
    assert (w.tree(), git(w.root, "status", "--porcelain=v1"), git(w.root, "rev-parse", "HEAD")) == before


def _decision(w, name="d1.json", **fields):
    body = {"schema": phases.DECISION_SCHEMA, "decision": "REOPEN", "phase": "P2",
            "closed_at_commit": w.x, "reason": "why"}
    body.update(fields)
    w.write(f"{GATE}/reopen/{name}", text(body))
    return w.commit(f"decision {name}")


def _status_line(capsys, w):
    code, out, _ = phase(capsys, w, "status", "P2")
    return code, out.strip()


def test_a_committed_reopen_decision_reopens_and_nothing_cancels_it(capsys, tmp_path):
    """The decision is read from the commit that added it: deleting the file, editing it to
    KEEP_CLOSED or emptying it later does not undo it. Only a new closure at a new commit does.
    Catches a reader of the current file."""
    w = closed_world(capsys, tmp_path)
    _decision(w, reason="the durability run failed")
    code, line = _status_line(capsys, w)
    assert code == 1 and line == ("P2 OPEN: closure reopened by .hoh/phases/p2/reopen/d1.json: "
                                  "the durability run failed")
    w.write(f"{GATE}/reopen/d1.json", text({"decision": "KEEP_CLOSED", "phase": "P2",
                                           "closed_at_commit": w.x}))
    w.commit("edit in place")
    assert _status_line(capsys, w)[1].startswith("P2 OPEN: closure reopened by")
    drop(w, GATE + "/reopen/d1.json")
    w.commit("remove it")
    code, line = _status_line(capsys, w)
    assert code == 1 and line.startswith("P2 OPEN: closure reopened by")
    code, out, _ = phase(capsys, w, "status", "P2", "--json")
    assert json.loads(out)["phases"][0]["state"] == "REOPENED"


def test_a_decision_about_another_closure_or_phase_or_an_answer_does_not_reopen(capsys, tmp_path):
    """A decision is about one phase and one closing commit (the full id or seven or more hex
    digits, in any case); one for another commit, another phase, or a KEEP_CLOSED answer leaves
    the closure standing. A decision that names this closure by a prefix reopens it."""
    w = closed_world(capsys, tmp_path)
    _decision(w, "other-commit.json", closed_at_commit="0" * 40)
    _decision(w, "other-phase.json", phase="P3")
    _decision(w, "keep.json", decision="KEEP_CLOSED")
    assert _status_line(capsys, w)[1].startswith("P2 CLOSED at")
    _decision(w, "short.json", closed_at_commit=w.x[:7].upper(), phase="p02")
    assert _status_line(capsys, w)[1].startswith("P2 OPEN: closure reopened by")


@pytest.mark.parametrize("name,content,reason", [
    ("bad.json", "{", "is not UTF-8 JSON"),
    ("list.json", "[]", "is not a JSON object"),
    ("nophase.json", '{"decision": "REOPEN"}', "does not name a phase and a closing commit"),
    ("badcommit.json", '{"phase": "P2", "closed_at_commit": "zzzzzzzz", "decision": "REOPEN"}',
     "a closing commit that cannot be read"),
    ("shortcommit.json", '{"phase": "P2", "closed_at_commit": "abc", "decision": "REOPEN"}',
     "a closing commit that cannot be read"),
    ("resolved.json", '{"phase": "P2", "closed_at_commit": "@X", "decision": "RESOLVED"}',
     "neither REOPEN nor KEEP_CLOSED"),
    ("nodecision.json", '{"phase": "P2", "closed_at_commit": "@X"}',
     "neither REOPEN nor KEEP_CLOSED"),
    ("note.txt", "a note", "is not a JSON record"),
    ("lower.json", '{"phase": "P2", "closed_at_commit": "@X", "decision": "reopen"}',
     "neither REOPEN nor KEEP_CLOSED"),
])
def test_a_record_in_the_reopen_directory_that_cannot_be_read_ignores_the_closure(
        capsys, tmp_path, name, content, reason):
    """Whether the closure was reopened cannot be told when a record names this phase and cannot
    be read (or is no JSON record at all): the closure is not honoured, the reason is printed. A
    RESOLVED record is not understood by this slice and refuses the same way."""
    w = closed_world(capsys, tmp_path)
    w.write(f"{GATE}/reopen/{name}", content.replace("@X", w.x))
    w.commit("a record")
    code, line = _status_line(capsys, w)
    assert code == 1 and line.startswith("P2 OPEN: closure record ignored: "), line
    assert reason in line, line


def test_the_reopen_directory_is_bounded_exactly(capsys, tmp_path):
    """MAX_FILES records are read; one more ignores the closure (never a cut that could hide the
    one that matters). A record that was removed still counts: the history is read."""
    w = closed_world(capsys, tmp_path)
    for n in range(63):
        w.write(f"{GATE}/reopen/k{n:02d}.json", text({"phase": "P2", "closed_at_commit": "0" * 40,
                                                       "decision": "KEEP_CLOSED"}))
    w.commit("63 answers")
    assert _status_line(capsys, w)[1].startswith("P2 CLOSED at")
    w.write(f"{GATE}/reopen/k63.json", text({"phase": "P2", "closed_at_commit": "0" * 40,
                                              "decision": "KEEP_CLOSED"}))
    w.commit("the 64th")
    assert _status_line(capsys, w)[1].startswith("P2 CLOSED at")
    w.write(f"{GATE}/reopen/k64.json", text({"phase": "P2", "closed_at_commit": "0" * 40,
                                              "decision": "KEEP_CLOSED"}))
    w.commit("the 65th")
    code, line = _status_line(capsys, w)
    assert code == 1 and "holds or held 65 files, more than 64" in line
    for n in range(65):
        drop(w, f"{GATE}/reopen/k{n:02d}.json")
    w.commit("all removed")
    assert "holds or held 65 files" in _status_line(capsys, w)[1]


def _watch_record(w, stamp, verdict, commit, paths=("src",), tree=None):
    if tree is None:
        oids = [git(w.root, "rev-parse", f"{commit}:{p}") for p in paths]
        tree = sha("".join(f"{p}\0{o}\n" for p, o in zip(paths, oids)))
    w.write(f"{GATE}/watch/{stamp}-{commit[:12]}.json",
            text({"record_version": 1, "verdict": verdict, "commit": commit,
                  "relevant_tree": tree}))


def _request(w, name="REOPEN-REQUEST-20261018T130000Z-abc.json", commit=None, x=None):
    w.write(f"{GATE}/{name}", text({"phase": "P2", "closed_at_commit": x or w.x,
                                    "commit": commit or git(w.root, "rev-parse", "HEAD"),
                                    "failed_requirements": ["P2-33"]}))
    return name


def test_the_watch_says_what_changed_since_the_evidence_and_what_ran_last(capsys, tmp_path):
    """The watch is information beside the closure: unchanged, stale since the first commit that
    touched the watched paths, the last re-run and whether it is current. Each state, once."""
    w = closed_world(capsys, tmp_path)
    assert "evidence unchanged since " + w.e[:12] in _status_line(capsys, w)[1]
    w.write("docs/notes.md", "other\n")
    w.commit("an unwatched change")
    assert "evidence unchanged" in _status_line(capsys, w)[1]
    first = w.advance(1)
    w.advance(1)
    line = _status_line(capsys, w)[1]
    assert f"evidence stale since {first[:12]}, re-run pending" in line and "last re-run" not in line
    head = git(w.root, "rev-parse", "HEAD")
    _watch_record(w, "20261018T120000Z", "PASS", head)
    w.commit("a re-run on this tree")
    line = _status_line(capsys, w)[1]
    assert f"last re-run 20261018T120000Z PASS at {head[:12]}" in line
    assert "stale" not in line, line
    w.advance(1)
    line = _status_line(capsys, w)[1]
    assert "last re-run 20261018T120000Z PASS" in line and "re-run pending" in line
    _watch_record(w, "20261018T140000Z", "FAIL", git(w.root, "rev-parse", "HEAD"))
    w.commit("a failing re-run")
    assert "last re-run 20261018T140000Z FAIL" in _status_line(capsys, w)[1]
    assert _status_line(capsys, w)[1].startswith("P2 CLOSED at"), "a failed run is not a reopening"


def test_a_closure_without_a_watch_says_so(capsys, tmp_path):
    w = closed_world(capsys, tmp_path, watch=False)
    assert _status_line(capsys, w)[1].endswith("watch: no regression watch declared")


def test_an_open_reopen_request_is_the_first_watch_line_and_a_decision_answers_it(capsys, tmp_path):
    """A request is not a decision: the phase stays CLOSED and the line names the regression. It
    is answered only by a committed REOPEN or KEEP_CLOSED decision that names the closure and the
    request; removing the request file answers nothing."""
    w = closed_world(capsys, tmp_path)
    head = w.advance(1)
    name = _request(w, commit=head)
    w.commit("a regression")
    line = _status_line(capsys, w)[1]
    assert line.startswith(f"P2 CLOSED at {w.x[:12]} (O268); watch: REGRESSION at {head[:12]}: "
                           f"P2-33 FAILED, reopen decision pending ({name})")
    drop(w, f"{GATE}/{name}")
    w.commit("the file is removed")
    assert "REGRESSION" in _status_line(capsys, w)[1]
    _decision(w, "answer.json", decision="KEEP_CLOSED", request=name)
    line = _status_line(capsys, w)[1]
    assert "REGRESSION" not in line and line.startswith("P2 CLOSED at")
    other = _request(w, "REOPEN-REQUEST-20261018T150000Z-def.json", x="0" * 40)
    _request(w, other, x="0" * 40)
    w.commit("a request about another closure")
    assert "REGRESSION" not in _status_line(capsys, w)[1]


def test_a_watch_that_cannot_be_read_is_said_and_never_a_pass(capsys, tmp_path):
    """An unreadable request does not reopen and does not vanish: the closure stands, the watch
    line says the watch is unreadable. The decision files were already read by the closure."""
    w = closed_world(capsys, tmp_path)
    w.write(f"{GATE}/REOPEN-REQUEST-20261018T130000Z-bad.json", "{")
    w.commit("garbage request")
    assert _status_line(capsys, w)[1].startswith(f"P2 CLOSED at {w.x[:12]} (O268)")


def test_status_shows_the_snapshot_at_x_when_the_live_one_cannot_be_read(capsys, tmp_path):
    """The live snapshot is the file at HEAD under the receipt's path; when it is gone or not a
    snapshot the closure still stands and the line says which snapshot is shown."""
    w = closed_world(capsys, tmp_path)
    (w.root / SNAP).write_text("{")
    w.commit("the live snapshot is broken")
    line = _status_line(capsys, w)[1]
    assert line.startswith("P2 CLOSED at") and f"live snapshot {SNAP} at HEAD cannot be read" in line
    drop(w, SNAP)
    w.commit("the live snapshot is gone")
    assert "cannot be read" in _status_line(capsys, w)[1]


# --------------------------------------------------------------------------- #
# reopen: an explicit, recorded decision -- and nothing else reopens
# --------------------------------------------------------------------------- #

def _reopen(capsys, w, name, reason="the run failed twice", *extra):
    return phase(capsys, w, "reopen", "P2", "--request", str(w.root / GATE / name),
                 "--reason", reason, *extra)


def test_reopen_records_the_decision_and_status_reads_it_once_committed(capsys, tmp_path):
    """The act through the command: a committed request, an explicit reason, a decision file that
    names the closure and the request. Uncommitted it changes nothing; committed the phase reads
    OPEN with the reason. A second reopen is quiet at every stage (bytes compared)."""
    w = closed_world(capsys, tmp_path)
    head = w.advance(1)
    name = _request(w, commit=head)
    w.commit("a regression")
    code, out, err = _reopen(capsys, w, name)
    assert code == 0 and err == "", err
    written = sorted((w.root / GATE / "reopen").iterdir())
    assert len(written) == 1 and re.fullmatch(rf"[0-9]{{8}}T[0-9]{{6}}Z-{w.x[:12]}\.json",
                                              written[0].name)
    body = json.loads(written[0].read_text())
    assert {k: v for k, v in body.items() if k != "decided_at_utc"} == {
        "schema": phases.DECISION_SCHEMA, "decision": "REOPEN", "phase": "P2",
        "closed_at_commit": w.x, "request": name, "reason": "the run failed twice"}
    line = _status_line(capsys, w)[1]
    assert line.startswith("P2 CLOSED at"), "uncommitted: not read"
    assert (f"reopen decision {written[0].name} is written but not committed: it counts once "
            "committed") in line
    before = w.tree()
    code, out, err = _reopen(capsys, w, name)
    assert (code, out) == (1, "") and "already has a reopen decision" in err and w.tree() == before
    w.commit("the decision")
    code, line = _status_line(capsys, w)
    assert code == 1 and line == (f"P2 OPEN: closure reopened by .hoh/phases/p2/reopen/"
                                  f"{written[0].name}: the run failed twice")
    before = w.tree()
    code, out, err = _reopen(capsys, w, name)
    assert (code, out) == (1, "") and "there is no standing closure of P2 to reopen" in err
    assert w.tree() == before


def test_reopen_refuses_what_is_not_a_decision_about_the_standing_closure(capsys, tmp_path):
    """Nothing is written unless the closure stands, the request is committed as it is on disk and
    is about this closure, the file is a request in the gate directory, and the reason is text."""
    w = World(tmp_path)
    assert _reopen(capsys, w, "REOPEN-REQUEST-x.json")[0] == 1                # no closure at all
    phase(capsys, w, "close", "P2", "--record", w.entry_file())
    w.commit("R")
    w.advance(1)
    good = _request(w)
    other = _request(w, "REOPEN-REQUEST-other.json", x="0" * 40)
    w.write(f"{GATE}/REOPEN-REQUEST-edited.json", text({"phase": "P2", "closed_at_commit": w.x}))
    w.commit("requests")
    (w.root / GATE / "REOPEN-REQUEST-edited.json").write_text("{}")          # differs from HEAD
    w.write(f"{GATE}/REOPEN-REQUEST-new.json", text({"phase": "P2", "closed_at_commit": w.x}))
    elsewhere = tmp_path / "elsewhere" / good
    elsewhere.parent.mkdir()
    elsewhere.write_bytes((w.root / GATE / good).read_bytes())
    before = w.tree()
    def inside(name):
        return str(w.root / GATE / name)

    cases = [
        (inside(other), "r", "is not about the closure of P2 at"),
        (inside("REOPEN-REQUEST-edited.json"), "r", "is not committed"),
        (inside("REOPEN-REQUEST-new.json"), "r", "is not a file of"),
        (inside("REOPEN-REQUEST-absent.json"), "r", "cannot be opened"),
        (inside("notes.json"), "r", "is not a reopen request file"),
        (str(elsewhere), "r", "is not the file"),
        (inside(good), "", "--reason must be 1 to 2000 characters"),
        (inside(good), " \t ", "--reason must be 1 to 2000 characters"),
        (inside(good), "x" * 2001, "--reason must be 1 to 2000 characters"),
        (inside(good), "a\nb", "without control characters"),
        (inside(good), "a\x00b", "without control characters"),
        (inside(good), "a\x7fb", "without control characters"),
        (inside(good), "\udcff", "is not valid UTF-8"),
    ]
    for request, reason, expected in cases:
        code, out, err = phase(capsys, w, "reopen", "P2", "--request", request, "--reason", reason)
        assert (code, out) == (1, ""), (request, reason)
        assert err.startswith("phase reopen refused: ") and expected in err, (request, err)
        assert w.tree() == before
    assert not (w.root / GATE / "reopen").exists()
    code, _, err = _reopen(capsys, w, good, "x" * 2000)                      # the ceiling itself
    assert code == 0, err


def test_a_request_that_is_a_link_or_not_a_file_is_not_read(capsys, tmp_path):
    w = closed_world(capsys, tmp_path)
    name = "REOPEN-REQUEST-link.json"
    os.symlink(w.root / SNAP, w.root / GATE / name)
    w.commit("a link as a request")
    code, out, err = _reopen(capsys, w, name)
    assert (code, out) == (1, "") and "cannot be opened" in err
    fifo = "REOPEN-REQUEST-fifo.json"
    os.mkfifo(w.root / GATE / fifo)
    code, out, err = _reopen(capsys, w, fifo)
    assert (code, out) == (1, "") and "is not a regular file" in err


def test_nothing_but_the_command_reopens():
    """A phase is never reopened by anything automatic: no module of the product but the command
    and the module itself refers to the functions that write or read a decision as an act, and
    only the command imports the module. A loop that wants to reopen fails this node, by design;
    a loop that parks a reopen request as a decision for a person adds its own reading, not this."""
    src = Path(phases.__file__).parent
    importers, reopeners = set(), set()
    for path in sorted(src.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        if re.search(r"(from\s+\.phases\s+import|from\s+\.\s+import[^\n]*\bphases\b|"
                     r"from\s+hoh\.phases\b|import\s+hoh\.phases\b)", body):
            importers.add(path.name)
        if re.search(r"\b(reopen_phase|run_reopen)\b", body):
            reopeners.add(path.name)
    assert importers == {"cli.py"}, importers
    assert reopeners == {"cli.py", "phases.py"}, reopeners


# --------------------------------------------------------------------------- #
# the product and the build tools read the same records
# --------------------------------------------------------------------------- #

def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_for_phases_tests",
                                                  ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_product_and_the_regression_watch_tool_read_the_same_records(tmp_path, monkeypatch):
    """tools/regression_watch.py writes the watch records and the reopen requests of VeriHarness
    and reads them for the gate; the product reads the same layout for any project. Over one
    repository in that layout the two agree on the open requests, the changed paths, the first
    stale commit, the relevant-tree digest, the last record and the way a decision names a
    closure -- so an agreed record means the same to both."""
    rw = _load_tool("regression_watch")
    paths = ("src", "ops/unit.service")
    monkeypatch.setattr(rw, "relevant_paths", lambda: paths)
    w = World(tmp_path)
    gate = "dogfood/p2-gate"
    w.write("ops/unit.service", "u1\n")
    ev = w.commit("evidence")
    first = w.advance(1)
    head = w.advance(1)
    for stamp, verdict in (("20261018T100000Z", "PASS"), ("20261018T110000Z", "FAIL")):
        tree = rw.relevant_tree(w.root, head)
        w.write(f"{gate}/watch/{stamp}-{head[:12]}.json",
                text({"record_version": 1, "verdict": verdict, "commit": head,
                      "relevant_tree": tree}))
    w.write(f"{gate}/watch/20261018T120000Z-bad.json", "{")
    w.write(f"{gate}/watch/20261018T130000Z-odd.json", text({"record_version": 2}))
    for n, (xx, fail) in enumerate(((w.x, ["P2-33"]), (w.x, []), ("0" * 40, ["P2-1"]))):
        w.write(f"{gate}/REOPEN-REQUEST-2026101{n}T000000Z-aa.json",
                text({"phase": "P2", "closed_at_commit": xx, "commit": head,
                      "failed_requirements": fail}))
    w.write(f"{gate}/reopen/answer.json", text({"decision": "KEEP_CLOSED", "phase": "p02",
                                                "closed_at_commit": w.x[:9].upper(),
                                                "request": f"{gate}/REOPEN-REQUEST-20261011T000000Z-aa.json"}))
    w.write(f"{gate}/reopen/other.json", text({"decision": "REOPEN", "phase": "P3",
                                               "closed_at_commit": w.x,
                                               "request": "REOPEN-REQUEST-20261010T000000Z-aa.json"}))
    w.commit("records")
    head = git(w.root, "rev-parse", "HEAD")
    assert phases.open_requests(w.root, head, "P2", gate, w.x) == rw.open_requests(
        w.root, head, w.x, "P2") and len(rw.open_requests(w.root, head, w.x, "P2")) == 1
    assert phases.changed_paths(w.root, ev, head, paths) == rw.changed_paths(w.root, ev, head) \
        == ["src/app.py"]
    assert phases.stale_since(w.root, ev, head, paths) == rw.stale_since(w.root, ev, head) == first
    assert phases._relevant_tree(w.root, head, paths) == rw.relevant_tree(w.root, head)
    last = rw.last_watch(w.root, head, "P2")
    mine = phases._watch_records(w.root, head, gate)[-1]
    assert (mine["verdict"], mine["commit"], mine["relevant_tree"]) == (
        last["verdict"], last["commit"], last["relevant_tree"]) and last["verdict"] == "FAIL"
    spellings = [("p2", w.x), ("P02", w.x[:7]), (" P2 ", w.x[:7].upper()), ("P2", w.x[:6]),
                 ("P3", w.x), ("P2", "0" * 40), ("P2", 7), (7, w.x), ("P2x", w.x), ("P2", " " + w.x),
                 ("P2", w.x + "\n"), ("P2", ""), (None, None)]
    for phase_word, commit in spellings:
        body = {"phase": phase_word, "closed_at_commit": commit}
        assert phases.names_closure(body, "P2", w.x) == rw.names_closure(body, "P2", w.x), body
    assert phases.phase_of("P02") == "P2" and phases.phase_of("x") is None


def test_close_refuses_evidence_that_is_a_link_at_x(capsys, tmp_path):
    """A link at X is no file: the digest of a link is the digest of the name it points to, and
    binding it would bind nothing. Refused, with the mode named."""
    w = World(tmp_path, extra={"evidence/link.json": ("link", "matrix.json")})
    rows = [{"path": "evidence/link.json", "file_sha256": sha(w.blob("evidence/link.json"))}]
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(evidence=rows))
    assert (code, out) == (1, "") and "is not a regular file (mode 120000)" in err
    assert not (w.root / GATE).exists()


def test_an_object_that_repeats_a_key_is_refused(capsys, tmp_path):
    """JSON keeps the last of two equal keys, so a reader of the text sees the first: an entry or
    a record that repeats a key says two things and is read as neither."""
    w = World(tmp_path)
    liar = text(w.entry()).replace('"phase": "P2",', '"phase": "P3",\n  "phase": "P2",')
    assert liar != text(w.entry())
    (tmp_path / "dup.json").write_text(liar)
    code, out, err = phase(capsys, w, "close", "P2", "--record", str(tmp_path / "dup.json"))
    assert (code, out) == (1, "") and "repeats a key" in err
    closed = closed_world(capsys, tmp_path / "second")
    closed.write(f"{GATE}/reopen/d.json", '{"decision": "KEEP_CLOSED", "decision": "REOPEN"}')
    closed.commit("a decision that says two things")
    assert "repeats a key" in _status_line(capsys, closed)[1]


@pytest.mark.parametrize("value,ok", [("O" + "1" * 63, True), ("O" + "1" * 64, False)])
def test_a_ref_has_its_edge(capsys, tmp_path, value, ok):
    """`ref` is at most 64 characters: 64 close, 65 are refused."""
    w = World(tmp_path)
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(ref=value))
    assert (code == 0) is ok, err


@pytest.mark.parametrize("path,ok", [
    pytest.param("a" * 120, True, id="part-120"), pytest.param("a" * 121, False, id="part-121"),
    pytest.param("/".join(["b" * 80, "b" * 80, "b" * 78]), True, id="path-240"),
    pytest.param("/".join(["b" * 80, "b" * 80, "b" * 79]), False, id="path-241"),
])
def test_a_path_has_its_edges(capsys, tmp_path, path, ok):
    """A path of the entry is at most 240 characters in parts of at most 120: the edges close, one
    over is refused."""
    assert len(path) in (120, 121, 240, 241)
    w = World(tmp_path)
    watch = {"evidence_commit": w.e, "paths": [path]}
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file(watch=watch))
    assert (code == 0) is ok, err
    assert ok or "a repository path in the shape the product reads" in err


@pytest.mark.parametrize("count,expected", [
    (3, ["REGRESSION"] * 3), (5, ["REGRESSION"] * 3 + ["and 2 more reopen request(s) pending"]),
    (64, ["REGRESSION"] * 3 + ["and 61 more reopen request(s) pending"]),
    (65, ["regression watch unreadable (65 reopen requests were added, more than 64"]),
])
def test_the_open_reopen_requests_are_listed_three_at_a_time_and_counted(capsys, tmp_path,
                                                                         count, expected):
    """Three requests are named, the rest counted; more than MAX_FILES are not listed at all and
    the watch says it cannot read them (never a cut that undercounts). The phase stays CLOSED."""
    w = closed_world(capsys, tmp_path)
    for n in range(count):
        _request(w, f"REOPEN-REQUEST-20261018T1300{n:02d}Z-a{n:02d}.json")
    w.commit("regressions")
    code, line = _status_line(capsys, w)
    assert code == 0 and line.startswith("P2 CLOSED at")
    watch = line.split("; watch: ", 1)[1].split("; ")
    assert [seen[:len(want)] for seen, want in zip(watch, expected)] == expected


def test_only_the_newest_sixty_four_watch_records_are_read(capsys, tmp_path):
    """The last record is looked for among the newest MAX_FILES files: an older valid one is not
    reached (the line says a re-run is pending), one more recent is. Pins the cut exactly."""
    w = closed_world(capsys, tmp_path)
    head = w.advance(1)
    _watch_record(w, "20260101T000000Z", "PASS", head)
    for n in range(64):
        w.write(f"{GATE}/watch/20261018T1{n:05d}Z-junk.json", "{")
    w.commit("a valid record, then 64 that are not records")
    assert "last re-run" not in _status_line(capsys, w)[1]
    drop(w, f"{GATE}/watch/20261018T100000Z-junk.json")
    w.commit("one junk file fewer: the valid record is the 64th newest")
    assert "last re-run 20260101T000000Z PASS" in _status_line(capsys, w)[1]


def test_a_decision_that_cannot_be_written_is_refused_and_nothing_counts(capsys, tmp_path,
                                                                        monkeypatch):
    """The decision is the terminal record of a reopen: when it cannot be written the command says
    so and exits 1, no decision file exists, and the closure still stands."""
    import errno

    w = closed_world(capsys, tmp_path)
    name = _request(w, commit=w.advance(1))
    w.commit("a regression")

    def full(path, payload, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(phases, "durable_create", full)
    code, out, err = _reopen(capsys, w, name)
    assert (code, out) == (1, "") and "cannot be created" in err
    assert not list((w.root / GATE / "reopen").glob("*.json"))
    assert _status_line(capsys, w)[1].startswith("P2 CLOSED at")


@pytest.mark.parametrize("argv", [
    ["phase"], ["phase", "close", "P2"], ["phase", "close", "--record", "e"],
    ["phase", "reopen", "P2", "--request", "REOPEN-REQUEST-x.json"],
    ["phase", "reopen", "P2", "--reason", "r"], ["phase", "status", "P2", "P3"],
    ["phase", "status", "--gate-dir"], ["phase", "frobnicate"],
])
def test_the_command_line_has_three_acts_and_asks_for_what_each_needs(argv, capsys):
    """argparse refuses an incomplete or unknown act (exit 2) before anything is read."""
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == 2
    capsys.readouterr()
    parsed = cli.build_parser().parse_args(["phase", "status"])
    assert (parsed.repo, parsed.gate_dir, parsed.phase, parsed.json) == (".", None, None, False)


def test_what_the_repository_says_reaches_the_terminal_without_control_characters(capsys, tmp_path):
    """A request, a decision and a file name come out of the repository: an escape sequence in a
    failed requirement or a reason, or a file name that is not UTF-8, must not reach the terminal
    as itself, and must not make the command fail."""
    w = closed_world(capsys, tmp_path)
    head = w.advance(1)
    w.write(f"{GATE}/REOPEN-REQUEST-20261018T130000Z-a.json",
            text({"phase": "P2", "closed_at_commit": w.x, "commit": head,
                  "failed_requirements": ["\x1b[31mred\x1b[0m"]}))
    odd = os.fsencode(w.root / GATE / "reopen") + b"/\xff-keep.json"
    (w.root / GATE / "reopen").mkdir(parents=True, exist_ok=True)
    with open(odd, "wb") as fh:
        fh.write(json.dumps({"phase": "P2", "closed_at_commit": "0" * 40,
                             "decision": "KEEP_CLOSED"}).encode())
    w.commit("a request with an escape sequence, a decision file with a name that is not UTF-8")
    code, out, err = phase(capsys, w, "status", "P2")
    assert (code, err) == (0, "") and "\x1b" not in out and "red" in out, out
    code, out, _ = phase(capsys, w, "status", "P2", "--json")
    assert code == 0 and json.loads(out)["phases"][0]["state"] == "CLOSED"
    _decision(w, "reason.json", reason="\x1b[2Jgone\x07")
    code, out, err = phase(capsys, w, "status", "P2")
    assert (code, err) == (1, "") and "\x1b" not in out and "\x07" not in out and "gone" in out, out


def test_a_committed_reopen_survives_a_merge_that_leaves_it_out(capsys, tmp_path):
    """A reopen is permanent also against history simplification: a line that never had the
    decision is merged over the line that did (`-s ours`) and fast-forwarded to, so the decision
    is in no tree on the first-parent line -- and the phase still reads reopened. Catches a reader
    of the simplified history (the endless-reset concern: a closure must not come back by a merge)."""
    w = closed_world(capsys, tmp_path)
    before = git(w.root, "rev-parse", "HEAD")
    _decision(w, reason="the durability run failed")
    git(w.root, "checkout", "-q", "-b", "side", before)
    git(w.root, "merge", "-q", "-s", "ours", "-m", "a merge that leaves the decision out", "main")
    git(w.root, "checkout", "-q", "main")
    git(w.root, "merge", "-q", "--ff-only", "side")
    assert not (w.root / GATE / "reopen").exists(), "the decision is in no tree of this line"
    code, line = _status_line(capsys, w)
    assert code == 1 and line.startswith("P2 OPEN: closure reopened by "), line


def test_a_receipt_edited_in_place_does_not_close_again(capsys, tmp_path):
    """After a reopen, writing a new closing commit into the old CLOSURE.json (a valid one: its
    digests are borne out at the new commit) is not a closure -- nobody ran `close`, no decision
    stands behind it. The receipt was changed in place and the line says so; the phase stays
    open. Catches a status that re-reads the edited file as a fresh closure."""
    w = closed_world(capsys, tmp_path)
    _decision(w)
    x2 = w.advance(1)
    _receipt_edit(w, lambda b: b.update(closed_at_commit=x2))
    code, line = _status_line(capsys, w)
    assert code == 1 and line.startswith("P2 OPEN: closure record ignored: "), line
    assert "was changed in place" in line and "close again" in line, line


def test_a_phase_closes_again_after_a_reopen_by_a_new_receipt_at_a_new_commit(capsys, tmp_path):
    """The one way back: move the old receipt aside under a versioned name, commit, and run
    `close` at a new closing commit. The decision names the old commit and does not follow the
    phase; the history of the path (added, moved away, added) is not an edit in place."""
    w = closed_world(capsys, tmp_path)
    _decision(w)
    assert _status_line(capsys, w)[1].startswith("P2 OPEN: closure reopened by")
    git(w.root, "mv", GATE + "/CLOSURE.json", GATE + "/CLOSURE.v1.20261018T000000Z.json")
    w.commit("the old receipt is moved aside")
    old_x = w.x
    w.x = w.advance(1)
    code, _, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert code == 0, err
    w.commit("R2: the new closure receipt")
    code, line = _status_line(capsys, w)
    assert code == 0 and line.startswith(f"P2 CLOSED at {w.x[:12]} (O268)") and w.x != old_x, line


def test_close_refuses_a_closing_commit_a_committed_decision_reopened(capsys, tmp_path):
    """`close` consults the reopen decisions too: with the old receipt moved aside, closing again
    at the very commit a committed decision reopened is refused, naming the decision."""
    w = closed_world(capsys, tmp_path)
    _decision(w, reason="failed twice")
    git(w.root, "mv", GATE + "/CLOSURE.json", GATE + "/CLOSURE.v1.20261018T000000Z.json")
    w.commit("the old receipt is moved aside")
    before = w.tree()
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert (code, out) == (1, "")
    assert f"{w.x[:12]} was reopened by {GATE}/reopen/d1.json: close at a later commit" in err
    assert not (w.root / GATE / "CLOSURE.json").exists() and w.tree() == before


def test_a_keep_closed_decision_does_not_block_a_reopen(capsys, tmp_path):
    """Only an earlier REOPEN decision for the closure blocks another: a KEEP_CLOSED answer
    (written by hand, committed or not) is an answer to a request, and the operator may still
    reopen later."""
    w = closed_world(capsys, tmp_path)
    name = _request(w, commit=w.advance(1))
    w.commit("a regression")
    _decision(w, "keep.json", decision="KEEP_CLOSED", request="REOPEN-REQUEST-older.json")
    w.write(f"{GATE}/reopen/keep-uncommitted.json",
            text({"phase": "P2", "closed_at_commit": w.x, "decision": "KEEP_CLOSED"}))
    code, _, err = _reopen(capsys, w, name)
    assert code == 0, err


def test_a_tab_in_a_reason_is_text(capsys, tmp_path):
    """The reason is text without control characters; a TAB is allowed (it is the one control
    character a person types into a sentence)."""
    w = closed_world(capsys, tmp_path)
    name = _request(w, commit=w.advance(1))
    w.commit("a regression")
    code, _, err = _reopen(capsys, w, name, "two\twords")
    assert code == 0, err
    written = next((w.root / GATE / "reopen").glob("*.json"))
    assert json.loads(written.read_text())["reason"] == "two\twords"


def test_the_receipt_that_was_written_is_read_back(capsys, tmp_path, monkeypatch):
    """After the write the file is read back and compared; a file that differs from what was
    written is reported as such (and the operator told not to use it), not announced as done."""
    from hoh import durable_io

    w = World(tmp_path)

    def damaged(path, payload, **kw):
        return durable_io.durable_create(path, payload + " ", **kw)
    monkeypatch.setattr(phases, "durable_create", damaged)
    code, out, err = phase(capsys, w, "close", "P2", "--record", w.entry_file())
    assert (code, out) == (1, "")
    assert "what was read back is not what was written; do not use it" in err


def test_a_phase_directory_is_listed_only_in_its_canonical_spelling(capsys, tmp_path):
    """`status` without a phase lists `p<N>` directories only (the spelling the product writes):
    `p02`, `P4` and `p2x` are other names, not other spellings of a phase."""
    w = closed_world(capsys, tmp_path)
    for name in ("p02", "P4", "p2x", "p", "p-3"):
        w.write(f".hoh/phases/{name}/NOTE", "x\n")
    w.commit("directories with other names")
    code, out, _ = phase(capsys, w, "status")
    assert code == 0 and [ln.split(" ")[0] for ln in out.splitlines()] == ["P2"]


def _uncommitted_decisions_files(w, count):
    for n in range(count):
        w.write(f"{GATE}/reopen/u{n:02d}.json", text({"phase": "P2", "closed_at_commit": "0" * 40,
                                                         "decision": "KEEP_CLOSED"}))


@pytest.mark.parametrize("count,ok", [(64, True), (65, False)])
def test_the_working_tree_reopen_directory_is_listed_up_to_64_entries(capsys, tmp_path, count, ok):
    """Decisions written and not committed are named in the watch (two, by name) while the
    directory holds at most MAX_FILES entries; one more and the watch says it cannot list them
    (never a cut). The phase stays CLOSED either way."""
    w = closed_world(capsys, tmp_path)
    _uncommitted_decisions_files(w, count)
    code, line = _status_line(capsys, w)
    assert code == 0 and line.startswith("P2 CLOSED at")
    if ok:
        assert "reopen decision u00.json is written but not committed" in line
    else:
        assert "regression watch unreadable (" in line and "holds more than 64 entries" in line


@pytest.mark.parametrize("count,ok", [(64, True), (65, False)])
def test_reopen_reads_up_to_64_files_of_the_working_tree_directory(capsys, tmp_path, count, ok):
    """`reopen` reads the decision files of the working-tree directory to be sure there is no
    earlier REOPEN: 64 files are read, one more refuses (an unread file cannot be told from a
    decision)."""
    w = closed_world(capsys, tmp_path)
    name = _request(w, commit=w.advance(1))
    w.commit("a regression")
    _uncommitted_decisions_files(w, count)
    code, _, err = _reopen(capsys, w, name)
    assert (code == 0) is ok, err
    assert ok or "holds more than 64 files: not read" in err


def test_the_branches_git_can_fail_on_are_refusals(capsys, tmp_path, monkeypatch):
    """`merge-base` answers 0 (ancestor), 1 (not) or something else (git could not say): the
    third is a refusal of its own, never a pass; and a crash of the product itself in `status`
    is exit 2 with the exception named, not 'open' and not a traceback."""
    w = closed_world(capsys, tmp_path)
    real = phases._git

    def failing(repo, *args):
        if args[:1] == ("merge-base",):
            return 128, b""
        return real(repo, *args)
    monkeypatch.setattr(phases, "_git", failing)
    code, line = _status_line(capsys, w)
    assert code == 1 and "git could not relate" in line and "(exit 128)" in line, line
    monkeypatch.setattr(phases, "_git", real)

    def crash(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(phases, "phase_status", crash)
    code, out, err = phase(capsys, w, "status", "P2")
    assert (code, out) == (2, "") and "failed itself: RuntimeError: boom" in err


def test_only_phases_mints_a_closure():
    """A `Closure` is what a caller hands in after verifying; the only code that verifies is
    `hoh.phases`, so it is the only code that makes one (a node like the reopen scan: a later
    caller that constructs a Closure itself fails here, by design)."""
    src = Path(phases.__file__).parent
    minters = {path.name for path in sorted(src.glob("*.py"))
               if re.search(r"\bClosure\(", path.read_text(encoding="utf-8"))}
    assert minters == {"checklist.py", "phases.py"}, minters      # checklist.py defines the class
