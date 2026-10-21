"""A closed phase stays closed: the gate reads a verified closure record (amendment A7).

`tools/build_plan.py` answered, on every tree, "would P2 close right now?" and the
dashboard read that as "is P2 closed?". The operator's ruling A7 says a phase is closed
at the commit of its closure record and later changes create regression obligations,
not a reopened phase. `build_plan.closure_record(phase)` now reads the machine-readable
twin of the records entry (`dogfood/<phase>-gate/CLOSURE.json`), re-derives every digest
it names from the blobs at the commit it names, and otherwise ignores it with its reason
(fail closed: the gate behaves as before). `phase_gate_report` / `build` turn a verified
closure into `CLOSED at <commit> (<entry>, <tag>); watch: ...` and keep every live
measurement visible as the watch.

Everything here runs over small synthetic git repositories built in a temporary
directory (never the real repository, which a git-less arena does not have). Nodes that
need the real history are in the last section and skip when it is absent.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from test_succession_records_window import _git, _write  # noqa: E402


def _load(name: str, as_name: str):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = module
    spec.loader.exec_module(module)
    return module


bp = _load("build_plan", "build_plan_closure_probe")

GATE = "dogfood/p2-gate"
MATRIX = bp.MATRIX_JSON
REGISTER = bp.REGISTER_JSON
DURABILITY = bp.DURABILITY_RECORD
CAPSULE = bp.SUCCESSION_CAPSULE
AMENDMENT = "program/v3_3/amendments/A7_PHASE_CLOSURE_AT_COMMIT.md"
RULING = "program/v3_3/amendments/A7_OPERATOR_DIRECTIVE_2026-10-10.txt"


def _json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Repo:
    """A scratch repository with the shape of the P2 closure: the instruments at the
    sealing commit S, the capsule commit C, the closing commit X (the records entry),
    a release commit T (tagged) and the commit H that adds the closure record.

    `variant` changes **an instrument** and keeps the record's claims consistent with it
    (so the clause under test, and no other, is what refuses), or puts a claim on a commit
    the history does not reach (`offline`), or edits an instrument after S / after C
    (`post_s`, `post_c`):

    row, snapshot_gate, review, durability, capsule, capsule_claim, offline (a set of
    "sealing" | "capsule" | "release" | "evidence"), post_s (a set of "matrix" |
    "snapshot" | "review"), post_c, ledger_text, no_rows, p2_rows, register_ids, pad, tag."""

    def __init__(self, root: Path, monkeypatch, **variant) -> None:
        self.root = root
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        self.su = _load("succession", "succession_for_closure_tests")
        monkeypatch.setattr(self.su, "HOH", root)
        monkeypatch.setattr(bp, "HOH", root)
        monkeypatch.setattr(bp, "_succession", lambda: self.su)
        ps = bp._programme_supervisor()
        v = variant
        offline = set(v.get("offline") or ())
        post_s = set(v.get("post_s") or ())
        rows = [] if v.get("no_rows") else [
            {"id": f"P2-{n:02d}", "target_phase": "P2", "status": "PROVEN"}
            for n in range(1, 1 + v.get("p2_rows", 3))]
        rows.append({"id": "P3-01", "target_phase": "P3", "status": "MISSING"})
        rows += [{"id": f"P3-{n:02d}", "target_phase": "P3", "status": "MISSING",
                  "evidence": ["x" * 80]} for n in range(2, 2 + v.get("pad", 0))]
        if v.get("row") and rows[0]["target_phase"] == "P2":
            rows[0] = {**rows[0], "status": v["row"]}
        p2_ids = [r["id"] for r in rows if r["target_phase"] == "P2"]
        matrix_s = {"what": "m", "measured_at_utc": "2026-10-13T11:02:20Z",
                    "capabilities": rows}
        matrix_x = ({**matrix_s, "annotation": "edited after S"}
                    if "matrix" in post_s else matrix_s)
        self.matrix_digest = ps.matrix_digest(matrix_x)
        self.snapshot_seal = "ab" * 32
        gates = {"closed": True, "derived_from_evidence": True, "blockers": []}
        gates.update(v.get("snapshot_gate") or {})
        snapshot_s = {"what": "s", "generation": 1, "seal": self.snapshot_seal,
                      "gates": {"P2": gates}, "items": [], "phase_exit": {}}
        snapshot_x = ({**snapshot_s, "annotation": "edited after S"}
                      if "snapshot" in post_s else snapshot_s)
        review_s = {"review_id": "20261013T093422Z-S", "phase": "P2", "complete": True,
                    "blocking_findings": [], "snapshot_seal": self.snapshot_seal,
                    "matrix_digest": self.matrix_digest}
        review_s.update(v.get("review") or {})
        review_x = ({**review_s, "annotation": "edited after S"}
                    if "review" in post_s else review_s)
        register_ids = v.get("register_ids")
        register = {"requirements": [
            {"id": i, "target_phase": "P2"} for i in (p2_ids if register_ids is None
                                                      else register_ids)]
            + [{"id": "P3-01", "target_phase": "P3"}]}
        texts = {MATRIX: _json(matrix_x), GATE + "/CHECKLIST_SNAPSHOT.json": _json(snapshot_x),
                 REGISTER: _json(register), AMENDMENT: "A7: closed at a commit\n",
                 RULING: "ruling\n"}
        # R: the instruments as they stand at S (the review and capsule come later)
        for rel, text in {MATRIX: _json(matrix_s),
                          GATE + "/CHECKLIST_SNAPSHOT.json": _json(snapshot_s),
                          REGISTER: texts[REGISTER], AMENDMENT: texts[AMENDMENT],
                          RULING: texts[RULING], "DOGFOOD_LEDGER.md": "# ledger\n",
                          "src/mod.py": "VALUE = 1\n"}.items():
            _write(root, rel, text)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "R: regenerated artefacts")
        self.evidence_head = _git(root, "rev-parse", "HEAD")
        r_tree = self.evidence_head

        def off_line(rev, parent=None):
            args = ["commit-tree", f"{rev}^{{tree}}", "-m", "off line"]
            return _git(root, *args, *(["-p", parent] if parent else []))
        evidence_claim = off_line(r_tree) if "evidence" in offline else self.evidence_head
        durability = {"evidence_head": evidence_claim, "requirement_satisfied": True,
                      "record_version": 6}
        durability.update(v.get("durability") or {})
        texts[DURABILITY] = _json(durability)
        _write(root, DURABILITY, texts[DURABILITY])
        _write(root, GATE + "/REVIEW.json", _json(review_s))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "S: review recorded")
        self.sealing = _git(root, "rev-parse", "HEAD")
        self.sealing_claim = off_line(self.sealing) if "sealing" in offline else self.sealing
        capsule_s = {"internal_commit": self.sealing_claim, "internal_tree_clean": True}
        capsule_s.update(v.get("capsule") or {})
        _write(root, CAPSULE, _json(capsule_s))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "C: capsule")
        self.capsule_commit = _git(root, "rev-parse", "HEAD")
        self.capsule_claim_commit = (off_line(self.capsule_commit, self.sealing)
                                     if "capsule" in offline else self.capsule_commit)
        capsule_x = ({**capsule_s, "annotation": "edited after C"} if v.get("post_c")
                     else capsule_s)
        # X: the entry, and whatever the variant edits after S / after C
        for rel, text in ((MATRIX, texts[MATRIX]),
                          (GATE + "/CHECKLIST_SNAPSHOT.json", texts[GATE + "/CHECKLIST_SNAPSHOT.json"]),
                          (GATE + "/REVIEW.json", _json(review_x)),
                          (CAPSULE, _json(capsule_x))):
            _write(root, rel, text)
        _write(root, "DOGFOOD_LEDGER.md", v.get("ledger_text") or
               "# ledger\n\n### O268 -- 2026-10-13: the closure\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "X: closure record entry")
        self.x = _git(root, "rev-parse", "HEAD")
        _write(root, "RELEASE_NOTES.md", "v0.3.0\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "T: release")
        self.release = _git(root, "rev-parse", "HEAD")
        self.release_claim = (off_line(self.x, self.x) if "release" in offline
                              else self.release)
        self.offline_tree_commit = off_line
        if v.get("tag", True) and "release" not in offline:
            _git(root, "tag", "v0.3.0", self.release)
        self.review_text = _json(review_x)
        self.capsule_text = _json(capsule_x)
        self.texts = texts
        capsule_claim = v.get("capsule_claim") or self.sealing_claim
        self.record = {
            "what": "closure record of P2", "record_version": 1, "phase": "P2",
            "closure_entry": "O268", "closure_date": "2026-10-13",
            "amendment": {"id": "A7", "file": "A7_PHASE_CLOSURE_AT_COMMIT.md",
                          "file_sha256": _sha(texts[AMENDMENT]),
                          "ruling_file": "A7_OPERATOR_DIRECTIVE_2026-10-10.txt",
                          "ruling_sha256": _sha(texts[RULING])},
            "closed_at_commit": self.x, "sealing_commit": self.sealing_claim,
            "capsule_commit": self.capsule_claim_commit,
            "release": {"tag": "v0.3.0", "commit": self.release_claim},
            "instruments": {
                "matrix": {"file_sha256": _sha(texts[MATRIX]),
                           "matrix_digest": self.matrix_digest},
                "snapshot": {"file_sha256": _sha(texts[GATE + "/CHECKLIST_SNAPSHOT.json"]),
                             "seal": self.snapshot_seal},
                "review": {"file_sha256": _sha(self.review_text),
                           "review_id": "20261013T093422Z-S",
                           "snapshot_seal": self.snapshot_seal,
                           "matrix_digest": self.matrix_digest},
                "register": {"file_sha256": _sha(texts[REGISTER])},
                "capsule": {"file_sha256": _sha(self.capsule_text),
                            "internal_commit": capsule_claim},
                "durability": {"file_sha256": _sha(texts[DURABILITY]),
                               "evidence_head": evidence_claim}},
            "rows": {"phase_rows": len(p2_ids),
                     "proven": sum(1 for r in rows if r["target_phase"] == "P2"
                                   and r["status"] == "PROVEN")}}

    def commit_record(self, mutate=None, *, commit: bool = True, name: str = "CLOSURE.json"):
        record = json.loads(json.dumps(self.record))
        if mutate is not None:
            mutate(record)
        _write(self.root, f"{GATE}/{name}", _json(record))
        if commit:
            _git(self.root, "add", "-A")
            _git(self.root, "commit", "-qm", "closure record")
        return record

    def commit(self, rel: str, text: str, message: str = "change") -> str:
        _write(self.root, rel, text)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return _git(self.root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path / "repo", monkeypatch)


def _closed(world: Repo, mutate=None):
    world.commit_record(mutate)
    return bp.closure_record("P2")


def _by(**patch):
    def apply(rec):
        for key, value in patch.items():
            rec[key] = value
    return apply


# --------------------------------------------------------------------------- #
# the record verifies, and later commits do not undo it
# --------------------------------------------------------------------------- #

def test_a_verified_closure_reads_closed(repo):
    closure, why = _closed(repo)
    assert why == "" and closure is not None, why
    assert closure["closed_at"] == repo.x and closure["entry"] == "O268"
    assert closure["tag"] == "v0.3.0" and closure["phase"] == "P2"
    assert closure["review_id"] == "20261013T093422Z-S"
    assert closure["evidence_head"] == repo.evidence_head


def test_a_source_merge_after_the_closure_does_not_undo_it(repo):
    """The defect: every merge touching `src/` flipped the live gate. The closure is a
    statement about X; commits after it (source, tests, tools, the register) change what
    the watch shows and nothing the closure says."""
    repo.commit_record()
    for rel in ("src/mod.py", "tests/test_x.py", "tools/y.py", REGISTER):
        repo.commit(rel, f"changed {rel}\n")
        closure, why = bp.closure_record("P2")
        assert why == "" and closure["closed_at"] == repo.x, (rel, why)


def test_verified_closures_names_only_phases_that_have_a_record(repo):
    repo.commit_record()
    closed, ignored = bp.verified_closures(["P0", "P1", "P2", "P3"])
    assert set(closed) == {"P2"} and ignored == {}


# --------------------------------------------------------------------------- #
# every mismatch holds, with its reason (fail closed = the gate as before)
# --------------------------------------------------------------------------- #

def test_a_record_that_is_not_committed_is_ignored(repo):
    repo.commit_record(commit=False)
    closure, why = bp.closure_record("P2")
    assert closure is None and "is not a file of" in why and "closure record" in why, why
    closed, ignored = bp.verified_closures(["P2"])
    assert closed == {} and "P2" in ignored


def test_a_record_edited_after_its_commit_is_ignored(repo):
    repo.commit_record()
    _write(repo.root, f"{GATE}/CLOSURE.json", _json(repo.record | {"closure_entry": "O999"}))
    closure, why = bp.closure_record("P2")
    assert closure is None and "differs from the one committed at HEAD" in why, why


def _later(record):
    record["closed_at_commit"] = "f" * 40


MISMATCHES = {
    "extra-key": (lambda r: r.update(extra="x"), "does not hold exactly its keys"),
    "missing-key": (lambda r: r.pop("rows"), "does not hold exactly its keys"),
    "version": (_by(record_version=2), "is version 2"),
    "phase": (_by(phase="P3"), "is about 'P3', not P2"),
    "entry-shape": (_by(closure_entry="268"), "`closure_entry` is missing or not in its shape"),
    "short-sha": (_by(closed_at_commit="d3791470"), "`closed_at_commit` is missing"),
    "unknown-commit": (_later, "is not a commit of this repository"),
    "matrix-file-digest": (
        lambda r: r["instruments"]["matrix"].update(file_sha256="0" * 64),
        "the capability matrix at"),
    "matrix-digest-moved": (
        lambda r: r["instruments"]["matrix"].update(matrix_digest="1" * 64),
        "does not re-digest"),
    "snapshot-seal": (
        lambda r: r["instruments"]["snapshot"].update(seal="2" * 64),
        "is not sealed"),
    "review-id": (
        lambda r: r["instruments"]["review"].update(review_id="20260101T000000Z-Z"),
        "is not complete, blocking-free"),
    "review-bound-elsewhere": (
        lambda r: r["instruments"]["review"].update(snapshot_seal="3" * 64),
        "is not complete, blocking-free"),
    "register-digest": (
        lambda r: r["instruments"]["register"].update(file_sha256="4" * 64),
        "the requirement register at"),
    "capsule-for-another-commit": (
        lambda r: r["instruments"]["capsule"].update(internal_commit="5" * 40),
        "does not describe the sealing commit"),
    "durability-head": (
        lambda r: r["instruments"]["durability"].update(evidence_head="6" * 40),
        "does not name the recorded evidence head"),
    "rows-count": (lambda r: r["rows"].update(proven=2), "closing lifts no row"),
    "ledger-entry": (_by(closure_entry="O999"), "has no entry O999"),
    "amendment-digest": (
        lambda r: r["amendment"].update(file_sha256="7" * 64), "the amendment at"),
    "ruling-digest": (
        lambda r: r["amendment"].update(ruling_sha256="8" * 64), "the operator ruling at"),
    "release-before-closure": (
        lambda r: r["release"].update(commit="9" * 40), "git could not relate"),
}


@pytest.mark.parametrize("name", sorted(MISMATCHES))
def test_each_mismatch_holds_with_its_reason(repo, name):
    mutate, expected = MISMATCHES[name]
    closure, why = _closed(repo, mutate)
    assert closure is None, name
    assert expected in why, (name, why)
    closed, ignored = bp.verified_closures(["P2"])
    assert closed == {} and expected in ignored["P2"], (name, ignored)


def test_a_closing_commit_that_is_not_in_the_history_is_ignored(repo):
    """A commit the repository holds but HEAD does not descend from (here: a child of X
    that no branch reaches)."""
    other = _git(repo.root, "commit-tree", f"{repo.x}^{{tree}}", "-p", repo.x, "-m", "off line")
    closure, why = _closed(repo, _by(closed_at_commit=other))
    assert closure is None, why
    assert "the closing commit is not in this tree's history" in why, why


def test_the_release_must_descend_from_the_closing_commit(repo):
    closure, why = _closed(repo, lambda r: r["release"].update(commit=repo.capsule_commit))
    assert closure is None and "does not descend from the closing commit" in why, why


def test_the_tag_is_printed_only_when_it_resolves_here_and_names_the_release(
        tmp_path, monkeypatch):
    world = Repo(tmp_path / "repo", monkeypatch, tag=False)
    world.commit_record()
    closure, why = bp.closure_record("P2")
    assert closure is not None, why
    assert closure["tag"] is None and closure["tag_claimed"] == "v0.3.0", \
        "a name nothing in this repository backs is a claim, not printed"
    _git(world.root, "tag", "v0.3.0", world.x)             # the tag names another commit
    closure, why = bp.closure_record("P2")
    assert closure is None and "the tag v0.3.0 here names" in why, why
    _git(world.root, "tag", "-f", "v0.3.0", world.release)
    closure, why = bp.closure_record("P2")
    assert why == "" and closure["tag"] == "v0.3.0"


@pytest.mark.parametrize("variant, expected", [
    ({"row": "IMPLEMENTED_NOT_PROVEN"}, "closing lifts no row"),
    ({"snapshot_gate": {"closed": False}}, "is not sealed"),
    ({"review": {"complete": False}}, "is not complete, blocking-free"),
    ({"review": {"blocking_findings": ["F1"]}}, "is not complete, blocking-free"),
    ({"review": {"phase": "P3"}}, "is not complete, blocking-free"),
    ({"capsule": {"internal_tree_clean": False}}, "does not describe the sealing commit"),
    ({"durability": {"requirement_satisfied": False}}, "does not name the recorded evidence head"),
], ids=["row-not-proven", "snapshot-gate-open", "review-incomplete", "review-blocking",
        "review-of-another-phase", "capsule-on-dirty-tree", "durability-unsatisfied"])
def test_the_content_of_the_instruments_is_judged_not_only_their_digests(
        tmp_path, monkeypatch, variant, expected):
    world = Repo(tmp_path / "repo", monkeypatch, **variant)
    closure, why = _closed(world)
    assert closure is None and expected in why, why


# --------------------------------------------------------------------------- #
# odd input is a reason, never an answer (fail closed)
# --------------------------------------------------------------------------- #

COMMITTED_SHAPES = {
    "not-json": (b"{not json", "cannot be read"),
    "empty": (b"", "cannot be read"),
    "latin-1": ('{"what": "caf\xe9"}'.encode("latin-1"), "cannot be read"),
    "utf-16": (json.dumps({"phase": "P2"}).encode("utf-16"), "cannot be read"),
    "nul": (b'{"what": "a\x00b"}', "cannot be read"),
    "list": (b"[1, 2]", "is not a JSON object"),
    "scalar": (b"3", "is not a JSON object"),
    "deeply-nested": (b"[" * 200000 + b"]" * 200000, "cannot be read"),
    "lone-surrogate": (b'{"what": "\\ud800"}', "does not hold exactly its keys"),
}


@pytest.mark.parametrize("name", sorted(COMMITTED_SHAPES))
def test_a_committed_record_of_another_shape_is_ignored_with_a_reason(repo, name):
    raw, expected = COMMITTED_SHAPES[name]
    target = repo.root / GATE / "CLOSURE.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "odd record")
    closure, why = bp.closure_record("P2")
    assert closure is None and expected in why, (name, why)
    assert "\n" not in why and len(why) <= bp._GATE_REASON_MAX


def _as_symlink_outside(path: Path, outside: Path):
    target = outside / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = path.with_name("tmp-link")
    link.symlink_to(target)
    link.replace(path)


def _as_dangling(path: Path, outside: Path):
    link = path.with_name("tmp-link")
    link.symlink_to(outside / "nowhere")
    link.replace(path)


def _as_directory(path: Path, outside: Path):
    path.rename(path.with_name("CLOSURE.json.v1.moved"))
    path.mkdir()


def _as_fifo(path: Path, outside: Path):
    import os
    fifo = path.with_name("tmp-fifo")
    os.mkfifo(fifo)
    fifo.replace(path)


def _as_unreadable(path: Path, outside: Path):
    path.chmod(0)


@pytest.mark.parametrize("shape, expected", [
    (_as_symlink_outside, "resolves outside the repository"),
    (_as_dangling, "is missing"),
    (_as_directory, "is not a regular file"),
    (_as_fifo, "is not a regular file"),
    (_as_unreadable, "cannot be read"),
], ids=["symlink-outside", "dangling-symlink", "directory", "fifo", "mode-000"])
def test_a_working_copy_that_is_not_a_regular_file_of_this_tree_is_ignored(
        repo, tmp_path, shape, expected):
    """A FIFO is never opened (reading one blocks); the others are stat-ed, not trusted."""
    repo.commit_record()
    shape(repo.root / GATE / "CLOSURE.json", tmp_path)
    closure, why = bp.closure_record("P2")
    assert closure is None and expected in why, why


def test_a_record_committed_as_a_symlink_is_not_a_record(repo):
    target = repo.root / GATE / "CLOSURE.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to("../../DOGFOOD_LEDGER.md")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "closure as a link")
    closure, why = bp.closure_record("P2")
    assert closure is None and "cannot be read" in why, why


def test_the_commit_and_digest_literals_are_matched_entirely(repo):
    for patch, key in (({"closed_at_commit": repo.x + "\n"}, "closed_at_commit"),
                       ({"closed_at_commit": repo.x.upper()}, "closed_at_commit"),
                       ({"sealing_commit": repo.sealing + " "}, "sealing_commit"),
                       ({"capsule_commit": " " + repo.capsule_commit}, "capsule_commit")):
        closure, why = _closed(repo, _by(**patch))
        assert closure is None, (patch, why)
        assert f"`{key}` is missing or not in its shape" in why, (patch, why)
    closure, why = _closed(repo, _by(phase="p2"))
    assert closure is None and "is about 'p2', not P2" in why, why
    for bad in ("P02", "P2\n", "p2", "P", "../P2", "P2/..", " P2"):
        assert bp.closure_record(bad)[0] is None, bad


def test_an_oversize_instrument_blob_is_refused_at_the_ceiling(repo, monkeypatch):
    repo.commit_record()
    sizes = [len((repo.root / rel).read_bytes()) for rel in
             (f"{GATE}/CLOSURE.json", MATRIX, REGISTER, DURABILITY, CAPSULE,
              f"{GATE}/REVIEW.json", f"{GATE}/CHECKLIST_SNAPSHOT.json", "DOGFOOD_LEDGER.md")]
    ceiling = max(sizes)
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", ceiling)
    assert bp.closure_record("P2")[0] is not None
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", ceiling - 1)
    closure, why = bp.closure_record("P2")
    assert closure is None and "is larger than" in why, why


def test_the_tool_reads_only(repo):
    """Termination is part of the contract: verifying a closure writes nothing, starts
    no review, capsule or regeneration, and leaves the tree as it found it."""
    repo.commit_record()

    def listing():
        return sorted((str(p.relative_to(repo.root)), p.stat().st_mtime_ns)
                      for p in repo.root.rglob("*")
                      if ".git" not in p.relative_to(repo.root).parts and p.is_file())
    before, status = listing(), _git(repo.root, "status", "--porcelain")
    assert bp.closure_record("P2")[0] is not None
    bp.verified_closures(["P1", "P2", "P3"])
    assert listing() == before and _git(repo.root, "status", "--porcelain") == status


# --------------------------------------------------------------------------- #
# an explicit reopen decision is the only way back (A7 rule 3)
# --------------------------------------------------------------------------- #

def _reopen(world, name="20261020T000000Z.json", **fields):
    record = {"phase": "P2", "closed_at_commit": world.x, "decision": "REOPEN",
              "entry": "O290", "reason": "a failing run"}
    record.update(fields)
    world.commit(f"{GATE}/{bp.REOPEN_DIR}/{name}", _json(record), "reopen")


def test_a_reopen_decision_naming_the_closure_ends_it(repo):
    repo.commit_record()
    assert bp.closure_record("P2")[0] is not None
    _reopen(repo)
    closure, why = bp.closure_record("P2")
    assert closure is None and why == "reopened by dogfood/p2-gate/reopen/20261020T000000Z.json (O290)", why


def test_a_reopen_decision_about_another_closure_does_not(repo):
    repo.commit_record()
    _reopen(repo, closed_at_commit="e" * 40)
    assert bp.closure_record("P2")[0] is not None
    _reopen(repo, phase="P3")
    assert bp.closure_record("P2")[0] is not None


@pytest.mark.parametrize("name, text, expected", [
    ("20261020T000000Z.json", "{not json", "cannot be read"),
    ("20261020T000001Z.json", "[1, 2]", "is not a JSON object"),
    ("20261020T000002Z.json", _json({"phase": "P2"}), "does not name a phase and a closing commit"),
    ("20261020T000003Z.txt", "reopen P2", "is not a JSON record"),
], ids=["not-json", "not-object", "no-closing-commit", "not-a-json-record"])
def test_a_reopen_record_that_cannot_be_read_holds_the_closure_fail_closed(
        repo, name, text, expected):
    """Whether it reopens this closure cannot be told, so the closure is not honoured."""
    repo.commit_record()
    repo.commit(f"{GATE}/{bp.REOPEN_DIR}/{name}", text, "unreadable reopen")
    closure, why = bp.closure_record("P2")
    assert closure is None and expected in why, why


def test_more_reopen_records_than_the_ceiling_are_not_read(repo):
    """An unbounded directory would cost one git read each; past `REOPEN_MAX` the answer
    is a refusal, not a long wait (64 are read, 65 are not)."""
    repo.commit_record()
    assert bp.REOPEN_MAX == 64
    for n in range(64):
        _write(repo.root, f"{GATE}/{bp.REOPEN_DIR}/{n:04d}.json",
               _json({"phase": "P2", "closed_at_commit": "e" * 40}))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "many records about other closures")
    assert bp.closure_record("P2")[0] is not None
    _write(repo.root, f"{GATE}/{bp.REOPEN_DIR}/9999.json",
           _json({"phase": "P2", "closed_at_commit": "e" * 40}))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "one more")
    closure, why = bp.closure_record("P2")
    assert closure is None and "more than 64" in why, why


def test_a_reopen_decision_that_is_not_reopen_holds_the_closure(repo):
    repo.commit_record()
    _reopen(repo, decision="REQUEST")
    closure, why = bp.closure_record("P2")
    assert closure is None and "neither REOPEN, KEEP_CLOSED nor RESOLVED" in why, why


def test_a_keep_closed_decision_answers_a_request_and_the_closure_stands(repo):
    repo.commit_record()
    _reopen(repo, decision="KEEP_CLOSED", request="REOPEN-REQUEST-20261020T000000Z-abc.json")
    closure, why = bp.closure_record("P2")
    assert why == "" and closure["closed_at"] == repo.x, why
    _reopen(repo)                       # the same directory, now a REOPEN: it ends it
    assert bp.closure_record("P2")[0] is None


# --------------------------------------------------------------------------- #
# the tree must be a repository of its own, judged before any git read (R4)
# --------------------------------------------------------------------------- #

def test_a_tree_without_its_own_history_is_an_environment_gap_not_a_missing_record(
        tmp_path, monkeypatch):
    world = Repo(tmp_path / "outer", monkeypatch)
    world.commit_record()
    inner = tmp_path / "outer" / "inner"
    inner.mkdir()
    for rel in (f"{GATE}/CLOSURE.json", CAPSULE):
        _write(inner, rel, (world.root / rel).read_text(encoding="utf-8"))
    monkeypatch.setattr(bp, "HOH", inner)
    monkeypatch.setattr(world.su, "HOH", inner)
    closure, why = bp.closure_record("P2")
    assert closure is None, why
    assert why.startswith("ENVIRONMENT_GAP: not a git repository of its own"), why
    assert "git show" not in why and "is not a file of" not in why, why


def test_the_succession_instrument_names_the_gap_before_it_reads_git(tmp_path, monkeypatch):
    """R4's second half: on a tree without history `git show` exits 128 and the gate used
    to print "not committed at HEAD (git show exit 128)", which misnames the cause."""
    world = Repo(tmp_path / "outer", monkeypatch)
    inner = tmp_path / "outer" / "inner"
    inner.mkdir()
    _write(inner, CAPSULE, (world.root / CAPSULE).read_text(encoding="utf-8"))
    monkeypatch.setattr(bp, "HOH", inner)
    monkeypatch.setattr(world.su, "HOH", inner)
    met, why = bp.succession_measure()
    assert met is False
    assert why.startswith("not checkable from here, which is not a pass: ENVIRONMENT_GAP: "), why
    assert "not a git repository of its own" in why and "git show exit" not in why, why


def test_a_tool_that_judges_another_tree_or_that_raises_ignores_the_record(repo, monkeypatch):
    repo.commit_record()
    monkeypatch.setattr(repo.su, "HOH", repo.root / "elsewhere")
    closure, why = bp.closure_record("P2")
    assert closure is None and "judges another tree" in why, why
    monkeypatch.setattr(repo.su, "HOH", repo.root)

    def boom():
        raise RuntimeError("no tool")
    monkeypatch.setattr(bp, "_succession", boom)
    closure, why = bp.closure_record("P2")
    assert closure is None and "could not be verified (RuntimeError)" in why, why
    closure, why = bp.closure_record("../P2")
    assert closure is None and "is not a phase name" in why


# --------------------------------------------------------------------------- #
# the gate: CLOSED, and the watch keeps every live answer
# --------------------------------------------------------------------------- #

ITEMS = "P2.checklist-gate.all-phase-blocking-checklist-items-verified-or-validly-waive"
REVIEW = "P2.checklist-gate.current-independent-checklist-supervisor-review-complete"
FSCK = "P2.checklist-gate.checklistfsck-pass"
SUCC = "P2.exit-includes.controller-session-replacement"
BOUND = "P2.exit-includes.unattended-test"


def _item(item_id, phase="P2", kind="gate"):
    return {"id": item_id, "phase": phase, "kind": kind, "text": item_id,
            "capability": False}


def _scope():
    items = [_item(i) for i in (ITEMS, REVIEW, FSCK, SUCC, BOUND)]
    items.append(_item("P3.gate.a", "P3"))
    return {"phases": [{"phase": p} for p in ("P1", "P2", "P3")], "items": items}


def _req(rid, phase, item):
    return {"id": rid, "name": rid, "target_phase": phase, "authoritative_item": item,
            "also_covers": [], "core_required_1_0": True}


REQUIREMENTS = [_req("P2-33", "P2", BOUND), _req("P2-35", "P2", SUCC),
                _req("P3-01", "P3", "P3.gate.a")]


def _rows(**status):
    base = {"P2-33": "PROVEN", "P2-35": "PROVEN", "P3-01": "MISSING"}
    base.update(status)
    return [{"id": rid, "name": rid, "target_phase": rid.split("-")[0], "status": st,
             "core_required_1_0": True} for rid, st in base.items()]


def _answers(**changes):
    out = {k: (True, f"{k} is fine") for k in ("items", "review", "fsck")}
    out.update(changes)
    return lambda: out


CLOSURE = {"phase": "P2", "closed_at": "d3791470af3ffcd96a9c19300d66243c7d0fdd11",
           "entry": "O268", "tag": "v0.3.0"}


def _report(monkeypatch, *, closures, rows=None, answers=None, succession=None):
    monkeypatch.setattr(bp, "_scope", _scope)
    asked = []

    def succession_measures():
        asked.append("asked")
        return (False, "REASON-S") if succession is None else succession
    out = bp.phase_gate_report(
        rows if rows is not None else _rows(), REQUIREMENTS,
        checklist_measures=answers or _answers(), succession_measures=succession_measures,
        closures=closures)
    return out, asked


def test_a_closed_phase_reads_closed_whatever_the_instruments_answer(monkeypatch):
    (order, gates, watch), asked = _report(
        monkeypatch, closures={"P2": CLOSURE},
        rows=_rows(**{"P2-33": "IMPLEMENTED_NOT_PROVEN"}),
        answers=_answers(review=(False, "snapshot seal changed")))
    met, why = gates["P2"]
    assert met is True and order == ["P1", "P2", "P3"]
    assert why == ("CLOSED at d3791470af3f (O268, v0.3.0); watch: 1 row(s) not PROVEN on "
                   "this tree (P2-33); live instruments: items met, review UNMET, fsck met"), why
    assert asked == [], "the capsule is the rotation instrument; it is not asked after closure"
    entry = watch["P2"]
    assert entry["live_met"] is False and "bound to requirement(s) not PROVEN: P2-33" in entry["live_reason"]
    assert entry["rows_not_proven"] == {"P2-33": "IMPLEMENTED_NOT_PROVEN"}
    assert entry["instruments"]["review"] == {"met": False, "reason": "snapshot seal changed"}
    assert entry["instruments"]["succession"]["met"] is None
    assert "P3" in gates and gates["P3"][0] is False


def test_without_a_closure_the_gate_is_what_it_was(monkeypatch):
    (_order, gates, watch), asked = _report(
        monkeypatch, closures=None, answers=_answers(review=(False, "stale")))
    met, why = gates["P2"]
    assert met is False and "supervisor review: stale" in why
    assert "succession capsule: REASON-S" in why and asked == ["asked"]
    assert watch == {}
    assert gates == bp.phase_gates(
        _rows(), REQUIREMENTS, checklist_measures=_answers(review=(False, "stale")),
        succession_measures=lambda: (False, "REASON-S"))[1]


def test_a_closure_of_one_phase_does_not_open_another(monkeypatch):
    (_order, gates, _watch), _ = _report(monkeypatch, closures={"P2": CLOSURE})
    assert gates["P2"][0] is True and gates["P3"][0] is False
    (_order, gates, _watch), _ = _report(monkeypatch, closures={"P3": {**CLOSURE, "phase": "P3"}})
    assert gates["P3"][1].startswith("CLOSED at ")
    assert gates["P2"][0] is False and gates["P2"][1] == bp.phase_gates(
        _rows(), REQUIREMENTS, checklist_measures=_answers(),
        succession_measures=lambda: (False, "REASON-S"))[1]["P2"][1]


def test_the_closed_line_is_one_bounded_line_and_names_at_most_six_rows(monkeypatch):
    rows = [{"id": f"P2-{n}", "target_phase": "P2", "status": "MISSING"}
            for n in range(40, 50)] + _rows()
    (_order, gates, watch), _ = _report(monkeypatch, closures={"P2": CLOSURE}, rows=rows)
    why = gates["P2"][1]
    assert len(why) <= bp._GATE_REASON_MAX and "\n" not in why
    assert "10 row(s) not PROVEN on this tree (P2-40, P2-41, P2-42, P2-43, P2-44, P2-45 and 4 more)" in why
    assert len(watch["P2"]["rows_not_proven"]) == 10


def test_a_phase_with_all_rows_proven_says_so(monkeypatch):
    (_order, gates, _watch), _ = _report(monkeypatch, closures={"P2": CLOSURE})
    assert gates["P2"][1] == ("CLOSED at d3791470af3f (O268, v0.3.0); watch: every row PROVEN "
                              "on this tree; live instruments: items met, review met, fsck met")


# --------------------------------------------------------------------------- #
# the plan: a closed phase holds nothing shut, and its open rows stay visible
# --------------------------------------------------------------------------- #

def _body():
    return {"capabilities": [
        {"id": "P2-33", "name": "n", "target_phase": "P2", "status": "IMPLEMENTED_NOT_PROVEN",
         "core_required_1_0": True, "depends_on": []},
        {"id": "P3-01", "name": "n", "target_phase": "P3", "status": "MISSING",
         "core_required_1_0": True, "depends_on": []}]}


def test_the_plan_of_a_closed_phase_has_no_blocker_and_keeps_the_watch(monkeypatch):
    (order, gates, watch), _ = _report(
        monkeypatch, closures={"P2": CLOSURE},
        rows=_rows(**{"P2-33": "IMPLEMENTED_NOT_PROVEN"}))
    plan = bp.build(_body(), order, gates, closures={"P2": CLOSURE}, watch=watch)
    state = plan["phase_gates"]["P2"]
    assert state["blocking_open"] == [] and state["external_met"] is True
    assert state["closed_at"] == CLOSURE["closed_at"] and state["closure_entry"] == "O268"
    assert state["release_tag"] == "v0.3.0"
    assert state["watch"]["blocking_open_live"] == ["P2-33"]
    assert state["watch"]["rows_not_proven"] == {"P2-33": "IMPLEMENTED_NOT_PROVEN"}
    assert "P2" in plan["open_gates"]
    assert "P3-01" in plan["ready_now"] and "P3-01" not in plan["held_by_phase_gate"]
    assert "P2-33" in plan["ready_now"], "the open row is still work, not hidden"
    json.dumps(plan)


def test_without_the_closure_the_same_plan_holds_the_next_phase(monkeypatch):
    (order, gates, _watch), _ = _report(
        monkeypatch, closures=None, rows=_rows(**{"P2-33": "IMPLEMENTED_NOT_PROVEN"}))
    plan = bp.build(_body(), order, gates)
    assert plan["phase_gates"]["P2"]["blocking_open"] == ["P2-33"]
    assert plan["phase_gates"]["P2"]["external_met"] is False
    assert "closed_at" not in plan["phase_gates"]["P2"]
    assert "P3-01" in plan["held_by_phase_gate"] and "P2" not in plan["open_gates"]


def test_a_record_that_was_not_honoured_is_in_the_plan_with_its_reason(monkeypatch):
    (order, gates, _watch), _ = _report(monkeypatch, closures=None)
    plan = bp.build(_body(), order, gates, closure_notes={"P2": "the record is stale"})
    assert plan["phase_gates"]["P2"]["closure_ignored"] == "the record is stale"
    assert plan["phase_gates"]["P2"]["external_met"] is False


class _Matrix:
    class RegisterMissing(Exception):
        pass

    @staticmethod
    def measure():
        return {"capabilities": _rows(**{"P2-33": "IMPLEMENTED_NOT_PROVEN"})}


def _main_over(world: Repo, monkeypatch, capsys):
    _write(world.root, "program/v3_3/REQUIREMENTS.json", _json({"requirements": REQUIREMENTS}))
    monkeypatch.setattr(bp, "_scope", _scope)
    monkeypatch.setattr(bp, "_matrix", lambda: _Matrix)
    monkeypatch.setattr(bp, "checklist_gate_measures", _answers())
    monkeypatch.setattr(bp, "succession_measure", lambda: (False, "REASON-S"))
    dag = world.root.parent / "dag.json"
    assert bp.main(["--dag", str(dag)]) == 0
    return capsys.readouterr().out, json.loads(dag.read_text(encoding="utf-8"))


def test_the_command_line_prints_the_closed_line_and_records_the_closure(
        repo, monkeypatch, capsys):
    repo.commit_record()
    # `main` reads the register from the working tree; the committed record is what counts
    out, dag = _main_over(repo, monkeypatch, capsys)
    assert f"gate P2: CLOSED at {repo.x[:12]} (O268, v0.3.0); watch: " in out, out
    assert "blocking open" not in out.split("gate P2")[1].splitlines()[0]
    state = dag["phase_gates"]["P2"]
    assert state["closed_at"] == repo.x and state["external_met"] is True
    assert state["blocking_open"] == []


def test_the_command_line_says_why_a_record_was_ignored_and_still_prints_the_gate(
        repo, monkeypatch, capsys):
    repo.commit_record(mutate=lambda r: r["instruments"]["matrix"].update(matrix_digest="1" * 64))
    out, dag = _main_over(repo, monkeypatch, capsys)
    assert "gate P2: closure record ignored: " in out and "does not re-digest" in out, out
    assert "gate P2: 1 blocking open" in out and "REASON-S" in out, out
    state = dag["phase_gates"]["P2"]
    assert "does not re-digest" in state["closure_ignored"] and state["external_met"] is False


# --------------------------------------------------------------------------- #
# the committed record of P2 (shape; its digests need the history: see below)
# --------------------------------------------------------------------------- #

def test_the_committed_closure_record_of_p2_is_well_formed():
    path = ROOT / GATE / "CLOSURE.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert set(record) == bp.CLOSURE_KEYS
    assert record["record_version"] == 1 and record["phase"] == "P2"
    assert record["closure_entry"] == "O268" and record["closure_date"] == "2026-10-13"
    assert re.fullmatch(r"[0-9a-f]{40}", record["closed_at_commit"])
    assert record["closed_at_commit"].startswith("d3791470")
    assert record["release"]["tag"] == "v0.3.0"
    assert record["rows"] == {"phase_rows": 43, "proven": 43}
    for name, group in record["instruments"].items():
        assert re.fullmatch(r"[0-9a-f]{64}", group["file_sha256"]), name
    review = record["instruments"]["review"]
    assert review["review_id"] == "20261013T093422Z-S"
    assert review["snapshot_seal"] == record["instruments"]["snapshot"]["seal"]
    assert review["matrix_digest"] == record["instruments"]["matrix"]["matrix_digest"]
    assert record["instruments"]["capsule"]["internal_commit"] == record["sealing_commit"]


def _real_history():
    import subprocess
    try:
        top = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        have = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e",
                               f"HEAD:{GATE}/CLOSURE.json"], capture_output=True).returncode
    except OSError:
        return False
    return bool(top) and Path(top).resolve() == ROOT.resolve() and have == 0


@pytest.mark.skipif(not _real_history(),
                    reason="needs a repository of its own with the closure record committed")
def test_the_committed_closure_record_of_p2_verifies_against_the_real_history():
    closure, why = bp.closure_record("P2")
    assert why == "" and closure is not None, why
    assert closure["closed_at"].startswith("d3791470") and closure["entry"] == "O268"


# --------------------------------------------------------------------------- #
# revision 1 -- one red-then-green row per clause the verifier claims to check.
# Each fixture changes the instrument (and keeps the record's claims consistent with
# it) or puts a claim on a commit the history does not reach, so that the clause
# under test, and no other, is what refuses.
# --------------------------------------------------------------------------- #

def _x12(world):
    return world.x[:12]


CLAUSES = {
    # the chain of commits (an off-line commit for each ancestry)
    "S-not-before-C": ({"offline": {"sealing"}, "capsule": {}}, None,
                       "the sealing commit does not lead to the capsule"),
    "C-not-before-X": ({"offline": {"capsule"}}, None,
                       "the capsule commit does not lead to the closing commit"),
    "release-not-in-history": ({"offline": {"release"}}, None,
                                "the release is not in this tree's history"),
    "evidence-not-before-X": ({"offline": {"evidence"}}, None,
                              "the durability evidence head"),
    # each file digest at X, alone
    "snapshot-digest-at-X": ({}, lambda r: r["instruments"]["snapshot"].update(file_sha256="0" * 64),
                             "the checklist snapshot at {x12} has sha256"),
    "review-digest-at-X": ({}, lambda r: r["instruments"]["review"].update(file_sha256="0" * 64),
                           "the supervisor review at {x12} has sha256"),
    "capsule-digest-at-X": ({}, lambda r: r["instruments"]["capsule"].update(file_sha256="0" * 64),
                            "the succession capsule at {x12} has sha256"),
    "durability-digest-at-X": ({}, lambda r: r["instruments"]["durability"].update(file_sha256="0" * 64),
                               "the durability record at {x12} has sha256"),
    "matrix-digest-at-X": ({}, lambda r: r["instruments"]["matrix"].update(file_sha256="0" * 64),
                           "the capability matrix at {x12} has sha256"),
    # carried unchanged from S (X's blob is what the claim names; S's blob differs)
    "matrix-changed-after-S": ({"post_s": {"matrix"}}, None, "the capability matrix at {s12}"),
    "snapshot-changed-after-S": ({"post_s": {"snapshot"}}, None, "the checklist snapshot at {s12}"),
    "review-changed-after-S": ({"post_s": {"review"}}, None, "the supervisor review at {s12}"),
    "capsule-changed-after-C": ({"post_c": True}, None, "the succession capsule at {c12}"),
    # the review file's own bindings, and the claims about it, one disjunct each
    "review-file-seal": ({"review": {"snapshot_seal": "e" * 64}}, None,
                         "is not complete, blocking-free"),
    "review-file-matrix-digest": ({"review": {"matrix_digest": "e" * 64}}, None,
                                  "is not complete, blocking-free"),
    "review-claim-seal": ({}, lambda r: r["instruments"]["review"].update(snapshot_seal="3" * 64),
                          "is not complete, blocking-free"),
    "review-claim-matrix-digest": (
        {}, lambda r: r["instruments"]["review"].update(matrix_digest="3" * 64),
        "is not complete, blocking-free"),
    # the capsule: file == claim and claim == S are separate clauses
    "capsule-file-is-not-the-claim": ({"capsule": {"internal_commit": "c" * 40}}, None,
                                      "does not describe the sealing commit"),
    "capsule-names-another-commit-consistently": (
        {"capsule": {"internal_commit": "a" * 40}, "capsule_claim": "a" * 40}, None,
        "does not describe the sealing commit"),
    # the snapshot gate: closed and blockers are separate clauses
    "snapshot-gate-not-closed": ({"snapshot_gate": {"closed": False}}, None, "is not sealed"),
    "snapshot-gate-has-a-blocker": ({"snapshot_gate": {"blockers": ["P2-01"]}}, None,
                                    "is not sealed"),
    # rows
    "a-phase-with-no-rows": ({"no_rows": True}, None, "has 0 of 0 P2 row(s) PROVEN"),
    "rows-claim-count": ({}, lambda r: r["rows"].update(phase_rows=4), "closing lifts no row"),
    "rows-claim-count-is-a-bool": ({"p2_rows": 1}, lambda r: r["rows"].update(phase_rows=True),
                                   "closing lifts no row"),
    "register-lacks-a-matrix-row": ({"register_ids": ["P2-01", "P2-02"]}, None,
                                    "disagree about the requirements of P2"),
    "register-has-a-row-the-matrix-lacks": (
        {"register_ids": ["P2-01", "P2-02", "P2-03", "P2-09"]}, None,
        "disagree about the requirements of P2"),
    # shapes
    "version-is-a-bool": ({}, lambda r: r.update(record_version=True), "is version True"),
    "tag-shape": ({}, lambda r: r["release"].update(tag="1.0"), "`tag` is missing or not in its shape"),
    "date-shape": ({}, lambda r: r.update(closure_date="13-10-2026"),
                   "`closure_date` is missing or not in its shape"),
    "amendment-id-shape": ({}, lambda r: r["amendment"].update(id="7"),
                           "`id` is missing or not in its shape"),
    "amendment-id-not-the-files": ({}, lambda r: r["amendment"].update(id="A8"),
                                   "is not the one its files name"),
    "amendment-file-name-shape": ({}, lambda r: r["amendment"].update(file="../A7_X.md"),
                                  "`file` is missing or not in its shape"),
    "ledger-entry-not-at-line-start": (
        {"ledger_text": "# ledger\n\nsee ### O268 -- 2026-10-13: the closure\n"}, None,
        "has no entry O268"),
    "ledger-entry-of-another-date": ({}, lambda r: r.update(closure_date="2026-10-14"),
                                      "has no entry O268 of 2026-10-14"),
}


@pytest.mark.parametrize("name", sorted(CLAUSES))
def test_each_clause_of_the_verifier_has_a_row_that_only_it_refuses(
        tmp_path, monkeypatch, name):
    variant, mutate, fragment = CLAUSES[name]
    world = Repo(tmp_path / "repo", monkeypatch, **variant)
    closure, why = _closed(world, mutate)
    want = fragment.format(x12=_x12(world), s12=world.sealing[:12],
                           c12=world.capsule_commit[:12])
    assert closure is None, name
    assert want in why, (name, why)


def test_a_phase_with_no_matrix_row_is_not_closed_vacuously(tmp_path, monkeypatch):
    world = Repo(tmp_path / "repo", monkeypatch, no_rows=True)
    closure, why = _closed(world)
    assert closure is None and "0 of 0 P2 row(s)" in why, why


def test_an_oversize_instrument_blob_is_refused_by_its_own_ceiling(tmp_path, monkeypatch):
    world = Repo(tmp_path / "repo", monkeypatch, pad=400)
    world.commit_record()
    record_size = len((world.root / GATE / "CLOSURE.json").read_bytes())
    matrix_size = len((world.root / MATRIX).read_bytes())
    assert matrix_size > record_size + 1000
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", matrix_size - 1)
    closure, why = bp.closure_record("P2")
    assert closure is None and "the capability matrix at" in why and "is larger than" in why, why
    monkeypatch.setattr(bp, "_GATE_INPUT_MAX_BYTES", matrix_size)
    assert bp.closure_record("P2")[0] is not None


def test_a_git_that_cannot_list_the_reopen_directory_is_not_a_pass(repo, monkeypatch):
    repo.commit_record()
    real = repo.su._git_rc

    def failing(*args, **kw):
        if args and args[0] == "ls-tree":
            return 128, ""
        return real(*args, **kw)
    monkeypatch.setattr(repo.su, "_git_rc", failing)
    closure, why = bp.closure_record("P2")
    assert closure is None and "git could not list" in why, why


def test_a_git_that_cannot_read_the_reopen_history_is_not_a_pass(repo, monkeypatch):
    repo.commit_record()
    real = repo.su._git_rc

    def failing(*args, **kw):
        if args and args[0] == "log":
            return 128, ""
        return real(*args, **kw)
    monkeypatch.setattr(repo.su, "_git_rc", failing)
    closure, why = bp.closure_record("P2")
    assert closure is None and "git could not read the history" in why, why


# --- an explicit reopen is read as a person writes it (A-B1) ------------------------

@pytest.mark.parametrize("spelling", [
    "full", "twelve", "seven", "upper-full", "upper-twelve", "padded"])
@pytest.mark.parametrize("phase_spelling", ["P2", "p2", "P02", " P2 "])
def test_a_reopen_decision_reopens_however_the_commit_and_the_phase_are_spelled(
        repo, spelling, phase_spelling):
    commit = {"full": repo.x, "twelve": repo.x[:12], "seven": repo.x[:7],
              "upper-full": repo.x.upper(), "upper-twelve": repo.x[:12].upper(),
              "padded": " " + repo.x[:12] + " "}[spelling]
    repo.commit_record()
    assert bp.closure_record("P2")[0] is not None
    _reopen(repo, closed_at_commit=commit, phase=phase_spelling)
    closure, why = bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), (spelling, phase_spelling, why)


@pytest.mark.parametrize("commit", ["zzzzzzz", "d37", "", "not a commit", "0x" + "a" * 10])
def test_a_reopen_decision_for_this_phase_with_a_commit_that_cannot_be_read_is_refused(
        repo, commit):
    repo.commit_record()
    _reopen(repo, closed_at_commit=commit)
    closure, why = bp.closure_record("P2")
    assert closure is None and "cannot be read" in why and "cannot be told" in why, why


def test_a_reopen_decision_naming_another_closure_of_the_phase_is_not_this_one(repo):
    repo.commit_record()
    other = ("0" if repo.x[0] != "0" else "1") + repo.x[1:]
    _reopen(repo, closed_at_commit=other[:12])
    assert bp.closure_record("P2")[0] is not None


@pytest.mark.parametrize("phase_spelling", ["", "P-2", "PX2", "phase two", 2, None])
def test_a_reopen_decision_whose_phase_cannot_be_read_is_refused(repo, phase_spelling):
    repo.commit_record()
    _reopen(repo, phase=phase_spelling)
    closure, why = bp.closure_record("P2")
    assert closure is None and "does not name a phase and a closing commit" in why, why


def test_a_reopen_decision_about_another_phase_is_ignored_whatever_its_commit(repo):
    repo.commit_record()
    _reopen(repo, phase="p3", closed_at_commit="zz")
    assert bp.closure_record("P2")[0] is not None


# --- removing a reopen record does not end it ---------------------------------------

def _remove_from_reopen_dir(world, name):
    path = world.root / GATE / bp.REOPEN_DIR / name
    path.rename(world.root / "moved-out.txt")
    _git(world.root, "add", "-A")
    _git(world.root, "commit", "-qm", "the record leaves the directory")


def test_a_reopen_record_that_is_removed_still_holds_the_closure(repo):
    repo.commit_record()
    _reopen(repo)
    assert bp.closure_record("P2")[0] is None
    _remove_from_reopen_dir(repo, "20261020T000000Z.json")
    closure, why = bp.closure_record("P2")
    assert closure is None and "no longer there" in why and "RESOLVED" in why, why


def test_a_resolved_record_naming_it_ends_a_reopen_and_a_removal(repo):
    repo.commit_record()
    _reopen(repo)
    _reopen(repo, name="20261021T000000Z.json", decision="RESOLVED",
            resolves="20261020T000000Z.json", entry="O291", reason="P2-33 repaired")
    closure, why = bp.closure_record("P2")
    assert why == "" and closure is not None, "the reopen is resolved by a records entry"
    _remove_from_reopen_dir(repo, "20261020T000000Z.json")
    assert bp.closure_record("P2")[0] is not None, "removed AND resolved"


def test_a_resolved_record_that_names_nothing_resolves_nothing(repo):
    repo.commit_record()
    _reopen(repo)
    _reopen(repo, name="20261021T000000Z.json", decision="RESOLVED", reason="x")
    closure, why = bp.closure_record("P2")
    assert closure is None and "names no record it resolves" in why, why


def test_a_resolved_record_for_another_closure_resolves_nothing_here(repo):
    repo.commit_record()
    _reopen(repo)
    _reopen(repo, name="20261021T000000Z.json", decision="RESOLVED",
            resolves="20261020T000000Z.json", closed_at_commit="0" * 7
            if repo.x[:7] != "0" * 7 else "1" * 7)
    closure, why = bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), why


def test_a_removed_keep_closed_record_also_needs_a_resolution(repo):
    repo.commit_record()
    _reopen(repo, decision="KEEP_CLOSED", request="r.json")
    assert bp.closure_record("P2")[0] is not None
    _remove_from_reopen_dir(repo, "20261020T000000Z.json")
    closure, why = bp.closure_record("P2")
    assert closure is None and "no longer there" in why, why


def test_a_history_of_more_reopen_files_than_the_ceiling_is_not_read(repo):
    """The directory holds none now; its history holds 65. Not read, not honoured."""
    repo.commit_record()
    for n in range(65):
        _write(repo.root, f"{GATE}/{bp.REOPEN_DIR}/{n:04d}.json",
               _json({"phase": "P2", "closed_at_commit": "e" * 40, "decision": "KEEP_CLOSED"}))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "many")
    moved = repo.root / "moved-out"
    moved.mkdir()
    for n in range(65):
        (repo.root / GATE / bp.REOPEN_DIR / f"{n:04d}.json").rename(moved / f"{n:04d}.txt")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "all gone")
    closure, why = bp.closure_record("P2")
    assert closure is None and "holds or held 65 files, more than 64" in why, why


def test_the_live_answer_is_unknown_not_met_while_the_capsule_is_not_asked(monkeypatch):
    """Every asked instrument met is not "met" when one instrument was not asked: the
    watch says None and says why; an unmet instrument stays unmet."""
    (_o, _g, watch), asked = _report(monkeypatch, closures={"P2": CLOSURE})
    entry = watch["P2"]
    assert entry["live_met"] is None and asked == []
    assert entry["live_reason"].endswith("the succession capsule was not asked after closure")
    (_o, _g, watch), _ = _report(monkeypatch, closures={"P2": CLOSURE},
                                 answers=_answers(fsck=(False, "x")))
    assert watch["P2"]["live_met"] is False


# --- a committed reopen is judged by the version that was committed (minor fold) ----

@pytest.mark.parametrize("edit", [
    {"decision": "KEEP_CLOSED"}, {"closed_at_commit": "0000000"}, {"phase": "P3"}],
    ids=["to-keep-closed", "to-another-commit", "to-another-phase"])
def test_an_edit_in_place_never_cancels_a_committed_reopen(repo, edit):
    repo.commit_record()
    _reopen(repo)
    assert bp.closure_record("P2")[0] is None
    _reopen(repo, **edit)                    # the same file, committed again, edited
    closure, why = bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), (edit, why)


def test_only_a_resolved_committed_after_the_edit_ends_an_edited_reopen(repo):
    repo.commit_record()
    _reopen(repo)
    _reopen(repo, decision="KEEP_CLOSED")
    assert bp.closure_record("P2")[0] is None
    _reopen(repo, name="20261021T000000Z.json", decision="RESOLVED",
            resolves="20261020T000000Z.json", entry="O291", reason="repaired")
    closure, why = bp.closure_record("P2")
    assert why == "" and closure is not None, why


def test_a_resolved_committed_before_the_reopen_it_names_is_ignored(repo):
    repo.commit_record()
    _reopen(repo, name="20261019T000000Z.json", decision="RESOLVED",
            resolves="20261020T000000Z.json", entry="O291", reason="in advance")
    assert bp.closure_record("P2")[0] is not None, "nothing is reopened yet"
    _reopen(repo)                            # the reopen it names arrives afterwards
    closure, why = bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), why


def test_a_resolved_committed_in_the_same_commit_as_the_reopen_is_not_after_it(repo):
    repo.commit_record()
    for name, body in (
            ("20261020T000000Z.json", {"phase": "P2", "closed_at_commit": repo.x,
                                       "decision": "REOPEN", "entry": "O290"}),
            ("20261021T000000Z.json", {"phase": "P2", "closed_at_commit": repo.x,
                                       "decision": "RESOLVED",
                                       "resolves": "20261020T000000Z.json"})):
        _write(repo.root, f"{GATE}/{bp.REOPEN_DIR}/{name}", _json(body))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "reopen and resolution together")
    closure, why = bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), why


def test_an_edit_that_makes_the_file_a_resolved_record_is_a_resolution_committed_after_it(repo):
    """The one edit that ends a reopen: the file becomes a RESOLVED record naming itself, in a
    commit after the one that made it a reopen -- a committed records act, in the open."""
    repo.commit_record()
    _reopen(repo)
    assert bp.closure_record("P2")[0] is None
    _reopen(repo, decision="RESOLVED", resolves="20261020T000000Z.json", reason="repaired")
    closure, why = bp.closure_record("P2")
    assert why == "" and closure is not None, why


def test_a_file_edited_into_a_reopen_is_a_reopen_from_that_edit_on(repo):
    """The reopen version is the edit, not the file's first commit: a RESOLVED committed
    between the two came before the reopen it would name."""
    repo.commit_record()
    _reopen(repo, decision="KEEP_CLOSED", request="r.json")
    _reopen(repo, name="20261021T000000Z.json", decision="RESOLVED",
            resolves="20261020T000000Z.json", reason="early")
    assert bp.closure_record("P2")[0] is not None
    _reopen(repo)                            # the same file becomes a REOPEN
    closure, why = bp.closure_record("P2")
    assert closure is None and why.startswith("reopened by "), why
    _reopen(repo, name="20261022T000000Z.json", decision="RESOLVED",
            resolves="20261020T000000Z.json", reason="after")
    assert bp.closure_record("P2")[0] is not None
