"""Tier classification of owned storage (STORAGE-D, P5-28): `hoh.storage_tiers`.

Every test builds its world under pytest's `tmp_path` (`World`): a real
`StorageRegistry` whose OS readers are injected (`FakeOS`: a process table, a
boot id, one mount), a small canonical git repository registered as the
project, owned scratch items created through the registry, an empty fake
``/proc`` unless a test needs another one, and archive targets that are
`tmp_path` directories with injected identity and health probes. HOME and git
configuration are confined to `tmp_path`; git runs with a fixed identity.

Tests inject the **sources** -- a registry, a repository, a ``/proc`` -- never
result sets: `classify_tier` takes none.

Every test name contains the selector it is run by; every docstring names the
failure (the mutant) it catches and the negative control that changes only the
condition under test. `World` and its helpers are imported by
`test_verified_relocation.py` and `test_storage_steward.py`. Nothing here
removes a file: tests change state by writing, renaming and committing.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hoh.archive_target as at
import hoh.verified_relocation as vr
from hoh import storage_tiers as st
from hoh.contracts import RunState
from hoh.storage_registry import StorageRegistry

ROOT = Path(__file__).resolve().parent.parent
BOOT = "boot-d"
PROJECT = "pd"
OTHER = "px"
SETUP_PID = 100            # the session that created the scratch items
STEWARD_PID = 200          # the session holding the receipts directory
RELOC_PID = 300            # the process that executes relocations
OTHER_PID = 400            # a second process (a competing execute)
GiB = 1024 ** 3


# --------------------------------------------------------------------------- #
# fakes and the world (also imported by the other STORAGE-D test files)
# --------------------------------------------------------------------------- #


class FakeOS:
    """Injected registry readers: a process table ``{pid: start time}``, a
    boot id, every path on mount 1 and its real ``st_dev``."""

    def __init__(self) -> None:
        self.procs = {SETUP_PID: 5000, STEWARD_PID: 6000, RELOC_PID: 7000, OTHER_PID: 8000}

    def boot_id(self) -> str:
        return BOOT

    def pid_start(self, pid: int) -> int | None:
        return self.procs.get(pid)

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs


class Probe:
    """Injected target identity and health; tests flip them between uses."""

    def __init__(self, identity: dict | None) -> None:
        self.identity = identity
        self.calls = 0
        self.after: dict | None = None      # identity from the second call on
        self.block = None                   # a threading.Event to wait on

    def read_identity(self, root):
        self.calls += 1
        if self.block is not None:
            self.block.wait()
        if self.after is not None and self.calls > 1:
            return dict(self.after)
        return None if self.identity is None else dict(self.identity)

    @staticmethod
    def read_health(root):
        return "healthy"


class Granted:
    admitted = True

    def __init__(self, op_kind: str, estimate: int) -> None:
        self.op_kind, self.estimate, self.id = op_kind, estimate, f"res-{op_kind}"


class Refused:
    admitted = False
    reason = "the reservation would cross below the reserve"


class FakeAdmission:
    """An admission with a chosen state; `reserve` admits or refuses."""

    def __init__(self, state: str = "PRESSURE", *, admit: bool = True, free: int = 40 * GiB,
                 reserve: int = 30 * GiB, seconds_to_reserve: float | None = None) -> None:
        self.state, self.admit, self.free, self.reserve_bytes = state, admit, free, reserve
        self.seconds_to_reserve = seconds_to_reserve
        self.reserved: list[tuple[str, str, int | None]] = []
        self.released: list[tuple[Granted, int]] = []

    def status(self) -> dict:
        return {"state": self.state, "effective_free_bytes": self.free,
                "reserve_bytes": self.reserve_bytes, "reserved_bytes": 0,
                "seconds_to_reserve": self.seconds_to_reserve}

    def reserve(self, project_id, op_kind, *, estimate_bytes=None):
        self.reserved.append((project_id, op_kind, estimate_bytes))
        return Granted(op_kind, estimate_bytes or 0) if self.admit else Refused()

    def release(self, reservation, actual_bytes):
        self.released.append((reservation, actual_bytes))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """HOME and git configuration confined to `tmp_path`; no seam left set."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "STORAGE-D Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "storage-d@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "STORAGE-D Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "storage-d@example.invalid")
    monkeypatch.setattr(vr, "_hooks", {})
    monkeypatch.setattr(at, "_after_copy_hook", None)


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True)
    return done.stdout.strip()


def run_state(run_id: str, *, condition: str = "ACTIVE", stage: str = "DEVELOPING") -> str:
    return RunState(run_id=run_id, repo_path="/x", project_name=PROJECT, spec_path="/x/spec",
                    spec_digest="s", policy_digest="p", profile_digest="f",
                    condition=condition, stage=stage).model_dump_json()


class World:
    """One registry with project `PROJECT` (canonical repository ``canon``
    holding ``data/a.txt`` and ``data/sub/b.txt``), a scratch directory of
    the setup session ``s1`` and the receipts directory of the session
    ``steward``. The setup session stays live until `end_setup`."""

    def __init__(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base
        self.fake = FakeOS()
        self.registry = StorageRegistry(
            base / "state", forbidden_roots=[base / "home"],
            boot_id_reader=self.fake.boot_id, pid_start_reader=self.fake.pid_start,
            pid_exists=self.fake.pid_exists, mount_check=lambda p: False,
            mount_id_reader=lambda p: 1)
        self.registry.init()
        self.canon = base / "canon"
        (self.canon / "data" / "sub").mkdir(parents=True)
        (self.canon / "data" / "a.txt").write_text("alpha\n", encoding="utf-8")
        (self.canon / "data" / "sub" / "b.txt").write_text("beta\n", encoding="utf-8")
        (self.canon / "README").write_text("canonical\n", encoding="utf-8")
        git(self.canon, "init", "-q", "-b", "main")
        git(self.canon, "add", "-A")
        git(self.canon, "commit", "-qm", "init")
        self.registry.register_project(PROJECT, self.canon)
        self.registry.open_session(PROJECT, "s1", pid=SETUP_PID, boot_id=BOOT)
        self.scratch = self.registry.scratch_dir(PROJECT, "s1", "scratch")
        self.registry.open_session(PROJECT, "steward", pid=STEWARD_PID, boot_id=BOOT)
        self.receipts = self.registry.scratch_dir(PROJECT, "steward", "receipts")
        self.proc = base / "proc"
        self.proc.mkdir()
        self._runs: Path | None = None

    # -- items ---------------------------------------------------------- #

    def end_setup(self) -> None:
        """The setup session's process ends: its lease is no longer live."""
        self.fake.procs.pop(SETUP_PID, None)

    def revive_setup(self) -> None:
        self.fake.procs[SETUP_PID] = 5000

    def copy(self, name: str, rel: str = "data", parent: Path | None = None) -> Path:
        target = (parent or self.scratch) / name
        shutil.copytree(self.canon / rel, target)
        return target

    def clone(self, name: str) -> Path:
        target = self.scratch / name
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.canon), str(target)],
                       check=True, capture_output=True)
        return target

    def commit(self, rel: str, text: str, message: str = "change") -> str:
        path = self.canon / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.canon, "add", "-A", "--", rel)
        git(self.canon, "commit", "-qm", message)
        return git(self.canon, "rev-parse", "HEAD")

    # -- runs base and retention ---------------------------------------- #

    def runs_base(self, parent: Path | None = None) -> Path:
        if self._runs is not None:
            return self._runs
        parent = (parent or self.tmp / "runs-parent").resolve()
        parent.mkdir(parents=True, exist_ok=True)
        config = self.registry.state_root / "CONFIG.json"
        config.write_text(json.dumps({"schema": "veriharness.storage-config/1", "sequence": 1,
                                      "admissible_external_parents": [str(parent)]}),
                          encoding="utf-8")
        entry = self.registry.create_external_root(PROJECT, parent / "runs", "runs_base")
        self._runs = Path(entry["path"])
        return self._runs

    def run(self, run_id: str, *, condition: str = "FAILED", stage: str = "VERIFYING",
            files: dict[str, str] | None = None) -> Path:
        directory = self.runs_base() / run_id
        directory.mkdir()
        (directory / "state.json").write_text(run_state(run_id, condition=condition,
                                                        stage=stage), encoding="utf-8")
        for rel, text in (files or {}).items():
            path = directory / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return directory

    def set_run(self, run_dir: Path, *, condition: str, stage: str) -> None:
        (run_dir / "state.json").write_text(run_state(run_dir.name, condition=condition,
                                                      stage=stage), encoding="utf-8")

    def retain(self, paths: list[Path], *, name: str = "20261002T000000Z.json",
               digests: dict[Path, str] | None = None) -> None:
        """Commit a retention manifest listing `paths` with their SHA-256."""
        originals = [{"path": str(p), "sha256": (digests or {}).get(p) or st.sha256_file(p)}
                     for p in paths]
        self.commit(f"{st.RETENTION_DIR}/{name}", json.dumps(
            {"schema": st.RETENTION_SCHEMA, "project_id": PROJECT, "originals": originals},
            indent=2) + "\n", message="retention manifest")

    # -- sources, targets, relocation ------------------------------------ #

    def sources(self, project: str = PROJECT, **kw) -> st.EvidenceSources:
        kw.setdefault("proc_root", str(self.proc))
        return st.EvidenceSources(self.registry, project, **kw)

    def classify(self, path, **kw) -> st.TierDecision:
        return st.classify_tier(path, registry=self.registry, sources=self.sources(**kw))

    def target(self, name: str = "t1") -> tuple[at.ArchiveTarget, Probe]:
        root = self.tmp / f"target-{name}"
        root.mkdir(exist_ok=True)
        probe = Probe({"serial": "S-" + name})
        target = at.ArchiveTarget({
            "target_id": name, "root": root, "failure_domain": "domain-" + name,
            "expected_identity": {"serial": "S-" + name},
            "identity_probe": probe.read_identity, "health_probe": probe.read_health})
        return target, probe

    def relocation(self, *, target=None, admission=None, pid: int = RELOC_PID,
                   proc_root: str | None = None, timeout: float = 120.0,
                   coordinator=None) -> vr.VerifiedRelocation:
        if target is None:
            target, _ = self.target()
        extra = {} if proc_root is None else {"proc_root": proc_root}
        return vr.VerifiedRelocation(
            self.registry, [target], None, self.receipts,
            admission=admission if admission is not None else FakeAdmission(),
            sources=lambda project: self.sources(project, **extra),
            getpid=lambda: pid, boot_id=self.fake.boot_id, timeout=timeout,
            coordinator=coordinator)

    def ledger(self, text: str) -> None:
        """Write (not commit) the canonical repository's DOGFOOD_LEDGER.md."""
        (self.canon / st.LEDGER_FILE).write_text(text, encoding="utf-8")


@pytest.fixture
def w(tmp_path) -> World:
    return World(tmp_path / "w")


def tiers(world: World, *paths: Path) -> list[str | None]:
    return [world.classify(p).tier for p in paths]


# --------------------------------------------------------------------------- #
# K1 -- a foreign or UNKNOWN path never becomes a candidate
# --------------------------------------------------------------------------- #


def test_unknown_ownership_never_candidate(w, tmp_path):
    """Catches the ownership check removed: a directory outside every
    registered root (foreign data under tmp_path), the registry's own scratch
    directory and a registered runs base itself (both PROTECTED -- the runs
    base although every entry below it is OWNED) are refused
    UNKNOWN_OWNERSHIP with no tier and no eligibility. Negative control: the same content inside the
    project's owned scratch gets a tier."""
    foreign = tmp_path / "foreign"
    shutil.copytree(w.canon / "data", foreign)
    owned = w.copy("dup")
    run = w.run("20261001t0300z-held", files={"out/x.txt": "x\n"})
    w.end_setup()
    for path in (foreign, w.scratch, run.parent):
        decision = w.classify(path)
        assert decision.refused == st.UNKNOWN_OWNERSHIP and decision.tier is None
        assert not decision.eligible
        with pytest.raises(ValueError):
            decision.eligibility()
    control = w.classify(owned)
    assert control.refused is None and control.tier == st.VERIFIED_DUPLICATE


def test_foreign_hardlink_item_ineligible(w, tmp_path):
    """Catches the per-entry nlink check removed (sreg2-b N3, Revision 3
    item 12): an owned directory holding one regular file hard-linked to a
    foreign file is refused UNKNOWN_OWNERSHIP as a whole -- even though the
    directory itself is OWNED and its bytes duplicate a canonical original --
    and so is one holding a symlink with a second, foreign hard link, which
    the registry (judging where the link leads) would answer OWNED.
    Negative control: the same directory with an ordinary copy of that file
    is a VERIFIED_DUPLICATE."""
    item = w.copy("dup")
    foreign = tmp_path / "foreign-a.txt"
    (item / "a.txt").rename(tmp_path / "moved-a.txt")
    foreign.write_text("alpha\n", encoding="utf-8")
    os.link(foreign, item / "a.txt")
    w.end_setup()
    decision = w.classify(item)
    assert decision.refused == st.UNKNOWN_OWNERSHIP
    assert "hard links" in " ".join(decision.reasons)
    control = w.copy("dup2")
    assert w.classify(control).tier == st.VERIFIED_DUPLICATE
    # A hard-linked symlink: the registry judges where it leads (an owned
    # regular file, one link), only the per-entry nlink check sees its
    # second, foreign name.
    linked = w.copy("dup3")
    (linked / "alias").symlink_to("a.txt")
    os.link(linked / "alias", tmp_path / "foreign-alias", follow_symlinks=False)
    decision = w.classify(linked)
    assert decision.refused == st.UNKNOWN_OWNERSHIP and "hard links" in decision.reasons[0]


# --------------------------------------------------------------------------- #
# K2 -- age never moves an item; HOT rules; the other tiers
# --------------------------------------------------------------------------- #


def test_age_irrelevant_decades_old_mtime(w):
    """Catches any read of age (mtime/atime/ctime): an item and one whose
    every entry carries an mtime and atime decades old are classified
    identically -- for a WARM item, a COLD one and a VERIFIED_DUPLICATE. The
    module's source reads no ``st_mtime``, ``st_atime``, ``st_ctime`` or
    ``getmtime``. Negative control: changing a byte (not the age) does move
    the duplicate out of its tier."""
    dup = w.copy("dup")
    cold = w.scratch / "notes"
    cold.mkdir()
    (cold / "n.txt").write_text("not a duplicate\n", encoding="utf-8")
    run = w.run("20260101t0000z-young", condition="ACTIVE", stage="DEVELOPING",
                files={"out/x.txt": "x\n"})
    w.end_setup()
    # The run directory itself of a live run is WARM (a path inside it is HOT,
    # `test_live_run_descendant_makes_hot`).
    before = tiers(w, dup, cold, run)
    old = 315532800                                       # 1980-01-01
    for root in (dup, cold, run):
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                os.utime(os.path.join(dirpath, name), (old, old))
        os.utime(root, (old, old))
    assert tiers(w, dup, cold, run) == before == [
        st.VERIFIED_DUPLICATE, st.COLD, st.WARM]
    source = Path(st.__file__).read_text(encoding="utf-8")
    for word in ("st_mtime", "st_atime", "st_ctime", "getmtime", "getatime", "getctime"):
        assert word not in source
    (dup / "a.txt").write_text("changed\n", encoding="utf-8")
    assert w.classify(dup).tier == st.COLD


def test_lease_makes_hot(w):
    """Catches the lease source ignored: an item in the scratch of a session
    whose registry lease is live is HOT (reason ``lease``); an unreadable
    LEASE.json counts as live. Negative control: the same item once the
    session's process has ended is a VERIFIED_DUPLICATE."""
    item = w.copy("dup")
    decision = w.classify(item)
    assert decision.tier == st.HOT
    assert any(r.startswith("lease:") for r in decision.reasons)
    w.end_setup()
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    lease = w.registry._lease_path(PROJECT, "s1")
    lease.rename(lease.with_name("LEASE.json.moved"))
    lease.write_text("{not json", encoding="utf-8")
    assert w.classify(item).tier == st.HOT


def test_open_finding_makes_hot(w):
    """Catches the findings source ignored: an item named in an open-item
    record of DOGFOOD_LEDGER.md, a run whose id an open finding names, an
    item named in the latest non-PASS QA answer of a run, and an item named
    in a review report of the project's reviews scratch are HOT (reason
    ``open_finding``). Negative control: the same mention outside any open
    record (a closed section), and a PASS answer, leave them cold."""
    item = w.copy("dup")
    run = w.run("20261001t1644z-sdurw3", files={"out/x.txt": "x\n"})
    w.end_setup()
    w.ledger(f"# Ledger\n\n## Erledigt\n\n- fixed long ago: {item}\n- run 20261001t1644z-sdurw3\n")
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    assert w.classify(run / "out").tier == st.COLD
    w.ledger(f"# Ledger\n\n## Offen, getrackt\n\n- still open: {item}/a.txt\n"
             f"- rerun 20261001t1644z-sdurw3 later\n\n## Erledigt\n")
    for path in (item, run / "out"):
        decision = w.classify(path)
        assert decision.tier == st.HOT, decision
        assert any(r.startswith("open_finding:") for r in decision.reasons)
    w.ledger("# Ledger\n")
    answers = run / "answers"
    answers.mkdir()
    (answers / "i1-a1-qa.json").write_text(json.dumps(
        {"verdicts": [{"check_id": "K1", "outcome": "PASS"}], "open_gaps": []}))
    assert w.classify(run / "out").tier == st.COLD
    (answers / "i1-a2-qa.json").write_text(json.dumps(
        {"verdicts": [{"check_id": "K1", "outcome": "FAIL", "note": f"see {item}"}],
         "open_gaps": []}))
    assert w.classify(run / "out").tier == st.HOT
    assert w.classify(item).tier == st.HOT
    (answers / "i1-a3-qa.json").write_text(json.dumps(
        {"verdicts": [{"check_id": "K1", "outcome": "PASS"}], "open_gaps": []}))
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    w.registry.open_session(PROJECT, "rv", pid=STEWARD_PID, boot_id=BOOT)
    reviews = w.registry.scratch_dir(PROJECT, "rv", "reviews")
    (reviews / "report.md").write_text(f"release no: {item} still differs\n")
    assert w.classify(item).tier == st.HOT


def test_unreadable_source_makes_hot(w):
    """Catches an unreadable source treated as empty: when DOGFOOD_LEDGER.md
    cannot be read (it is a directory), every item is HOT with ``source
    unreadable``; so is it when /proc cannot be listed. Negative control:
    the readable ledger without a mention leaves the item a duplicate."""
    item = w.copy("dup")
    w.end_setup()
    w.ledger("# Ledger\n")
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    (w.canon / st.LEDGER_FILE).rename(w.tmp / "ledger-moved")
    (w.canon / st.LEDGER_FILE).mkdir()
    decision = w.classify(item)
    assert decision.tier == st.HOT
    assert any("source unreadable" in r for r in decision.reasons)
    (w.canon / st.LEDGER_FILE).rename(w.tmp / "ledger-dir-moved")
    decision = w.classify(item, proc_root=str(w.tmp / "no-proc-here"))
    assert decision.tier == st.HOT and any(r.startswith("process:") for r in decision.reasons)


def test_current_evidence_makes_hot(w):
    """Catches the evidence collector removed: a file committed under
    ``dogfood/**/run-evidence/`` naming a path inside the item (pointer-
    carrying, e.g. a ``sandbox`` field) makes the item HOT (reason
    ``current_evidence``); so does one under ``runs/``. Negative control: the
    same file only in the working tree (not committed), or naming another
    path, leaves it a duplicate."""
    item = w.copy("dup")
    other = w.copy("other")
    w.end_setup()
    evidence = w.canon / "dogfood" / "x" / "run-evidence" / "r.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"sandbox": f"{item}/sub"}))
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE        # not committed
    git(w.canon, "add", "-A")
    git(w.canon, "commit", "-qm", "evidence")
    decision = w.classify(item)
    assert decision.tier == st.HOT
    assert any(r.startswith("current_evidence:") for r in decision.reasons)
    assert w.classify(other).tier == st.VERIFIED_DUPLICATE
    w.commit("runs/r9/receipt.json", json.dumps({"source_run_dir": str(other)}))
    assert w.classify(other).tier == st.HOT


def test_unmerged_candidate_makes_hot(w):
    """Catches the unmerged-candidate source ignored: a ``refs/imports/``
    ref not reachable from the default branch, whose name carries a run id,
    makes every path of that run HOT; a clone whose HEAD is such a candidate
    is HOT too. Negative control: once the candidate is merged (an ancestor
    of HEAD) neither is HOT."""
    item = w.copy("20261001t0900z-cand-copy")
    w.end_setup()
    git(w.canon, "checkout", "-q", "-b", "side")
    (w.canon / "side.txt").write_text("side\n", encoding="utf-8")
    git(w.canon, "add", "-A")
    git(w.canon, "commit", "-qm", "side")
    side = git(w.canon, "rev-parse", "HEAD")
    git(w.canon, "checkout", "-q", "main")
    git(w.canon, "update-ref", "refs/imports/20261001t0900z-cand/i1", side)
    decision = w.classify(item)
    assert decision.tier == st.HOT
    assert any(r.startswith("unmerged_candidate:") for r in decision.reasons)
    git(w.canon, "merge", "-q", "--ff-only", "side")
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE


def test_succession_record_makes_hot(w):
    """Catches the succession/recovery source ignored: a committed
    SUCCESSION.json naming the item, or a ``refs/preserve/`` ref naming its
    run, makes it HOT (reason ``succession_record``). Negative control: a
    SUCCESSION.json naming another path leaves it a duplicate."""
    item = w.copy("dup")
    w.end_setup()
    w.commit("dogfood/succession/SUCCESSION.json", json.dumps({"arena": "/elsewhere/x"}))
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    w.commit("dogfood/succession/SUCCESSION.json", json.dumps({"arena": str(item)}))
    decision = w.classify(item)
    assert decision.tier == st.HOT
    assert any(r.startswith("succession_record:") for r in decision.reasons)


def test_verified_duplicate_tier(w):
    """Catches the duplicate rule removed or loosened: a byte-identical copy
    of a committed, clean directory of the canonical repository is a
    VERIFIED_DUPLICATE with a proof naming every original and the commit.
    Negative controls changing one condition each: one byte different, an
    extra file, and the original modified in the working tree all leave it
    COLD."""
    item = w.copy("dup")
    extra = w.copy("extra")
    (extra / "new.txt").write_text("not committed anywhere\n", encoding="utf-8")
    differs = w.copy("differs")
    (differs / "sub" / "b.txt").write_text("BETA\n", encoding="utf-8")
    w.end_setup()
    decision = w.classify(item)
    assert decision.tier == st.VERIFIED_DUPLICATE
    assert decision.proof["source"] == "git"
    assert decision.proof["commit"] == git(w.canon, "rev-parse", "HEAD")
    assert sorted(o["item_rel"] for o in decision.proof["originals"]) == ["a.txt", "sub/b.txt"]
    assert decision.proof_digest == st.proof_digest(decision.proof)
    assert decision.eligibility() == {"classification": st.VERIFIED_DUPLICATE,
                                      "proof_digest": decision.proof_digest, "unique": False}
    assert tiers(w, extra, differs) == [st.COLD, st.COLD]
    (w.canon / "data" / "a.txt").write_text("edited\n", encoding="utf-8")
    assert w.classify(item).tier == st.COLD


def test_reproducible_scratch_tier(w):
    """Catches the reproduction rule removed: a fresh ``--no-hardlinks``
    clone of the canonical repository is REPRODUCIBLE_SCRATCH with a proof
    naming its commit. Negative control: the same clone with a commit the
    canonical repository lacks is not (COLD)."""
    clone = w.clone("clone")
    w.end_setup()
    decision = w.classify(clone)
    assert decision.tier == st.REPRODUCIBLE_SCRATCH, decision.notes
    assert decision.proof["type"] == "clone"
    assert decision.proof["commit"] == git(w.canon, "rev-parse", "HEAD")
    (clone / "README").write_text("local change\n", encoding="utf-8")
    git(clone, "commit", "-qam", "local")
    decision = w.classify(clone)
    assert decision.tier == st.COLD
    assert any("not in the canonical repository" in n for n in decision.notes)


def test_cold_when_terminal_and_unreferenced(w):
    """Catches the COLD rule removed: an unreferenced item without a proof
    in the scratch of an ended session, one in a FAILED run, and that FAILED
    run's directory are COLD. Negative control: the same run directory while
    ACTIVE/DEVELOPING is WARM (and a path inside it HOT)."""
    notes = w.scratch / "notes"
    notes.mkdir()
    (notes / "n.txt").write_text("scratch notes\n", encoding="utf-8")
    run = w.run("20261001t0100z-done", condition="FAILED", files={"out/x.txt": "x\n"})
    w.end_setup()
    assert tiers(w, notes, run / "out", run) == [st.COLD, st.COLD, st.COLD]
    w.set_run(run, condition="ACTIVE", stage="DEVELOPING")
    assert w.classify(run).tier == st.WARM
    assert w.classify(run / "out").tier == st.HOT


def test_warm_otherwise(w):
    """Catches the WARM fallback replaced by COLD: an unreferenced run
    directory that is still ACTIVE/DEVELOPING, and a CHECKPOINTED one whose
    process is still live, are WARM (a path inside either is HOT). Negative
    control: the same CHECKPOINTED run once its process has ended is COLD."""
    from hoh.store import RunStore

    run = w.run("20261001t0200z-busy", condition="ACTIVE", stage="DEVELOPING",
                files={"out/x.txt": "x\n"})
    w.end_setup()
    assert w.classify(run).tier == st.WARM
    assert w.classify(run / "out").tier == st.HOT
    w.set_run(run, condition="ACTIVE", stage="CHECKPOINTED")
    with RunStore(run.parent, run.name).process_marker():
        assert w.classify(run).tier == st.WARM
    assert w.classify(run).tier == st.COLD
    assert w.classify(run / "out").tier == st.COLD


def _raise_for(real, *paths):
    """A stand-in for an `os` function that raises PermissionError for the
    given paths and behaves as `real` everywhere else (no chmod, so the
    tests never depend on the uid)."""
    blocked = {os.fspath(p) for p in paths}

    def fake(path, *args, **kwargs):
        if not isinstance(path, int) and os.fsdecode(os.fspath(path)) in blocked:
            raise PermissionError(13, "Permission denied (injected)", os.fspath(path))
        return real(path, *args, **kwargs)
    return fake


def _runs_scratch(world: World, session: str = "rs") -> Path:
    """A ``runs`` scratch directory of a session whose process has ended."""
    world.fake.procs[500] = 9000
    world.registry.open_session(PROJECT, session, pid=500, boot_id=BOOT)
    runs = world.registry.scratch_dir(PROJECT, session, "runs")
    world.fake.procs.pop(500)
    return runs


def _scratch_run(runs: Path, run_id: str, *, condition: str = "FAILED",
                 stage: str = "VERIFYING", files: dict[str, str] | None = None) -> Path:
    run = runs / run_id
    run.mkdir()
    (run / "state.json").write_text(run_state(run_id, condition=condition, stage=stage),
                                    encoding="utf-8")
    for rel, text in (files or {}).items():
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        (run / rel).write_text(text, encoding="utf-8")
    return run


def test_unreadable_runs_base_makes_hot(w, monkeypatch):
    """Catches an OSError on the runs-base listing skipped (the open-findings
    source failing open, E-K2): when the registered runs base -- or a
    ``runs`` scratch directory -- cannot be listed, its runs' QA answers
    cannot be read, so every item of the project is HOT with ``open_finding:
    source unreadable`` naming that directory. Negative control: the same
    world with both readable leaves the duplicate VERIFIED_DUPLICATE."""
    item = w.copy("dup")
    run = w.run("20261001t0400z-done", files={"out/x.txt": "x\n"})
    runs = _runs_scratch(w)
    _scratch_run(runs, "20261001t0401z-done")
    w.end_setup()
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    real = os.scandir
    for unreadable in (run.parent, runs):
        monkeypatch.setattr(os, "scandir", _raise_for(real, unreadable))
        decision = w.classify(item)
        assert decision.tier == st.HOT, decision
        assert any(r.startswith("open_finding: source unreadable") and str(unreadable) in r
                   for r in decision.reasons), decision.reasons
        monkeypatch.setattr(os, "scandir", real)
        assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    assert not w.relocation().plan(PROJECT, []).items


def test_unreadable_run_state_makes_hot(w, monkeypatch):
    """Catches an unreadable state.json skipped: a run directory whose
    ``state.json`` cannot be stat'ed, or cannot be opened, or whose latest QA
    answer cannot be read, makes the findings source unreadable -- every
    item of the project is HOT (``source unreadable``) and the run's own
    output is never eligible (HOT, or refused where the registry cannot
    validate the run directory either), instead
    of the run being skipped as if it held no finding. Negative control:
    the same run readable leaves the duplicate VERIFIED_DUPLICATE and the
    finished run's output COLD."""
    item = w.copy("dup")
    run = w.run("20261001t0410z-done", files={"out/x.txt": "x\n"})
    w.end_setup()
    assert [w.classify(p).tier for p in (item, run / "out")] == [
        st.VERIFIED_DUPLICATE, st.COLD]
    state = run / "state.json"
    for name in ("lstat", "open"):
        real = getattr(os, name)
        monkeypatch.setattr(os, name, _raise_for(real, state))
        decision = w.classify(item)
        assert decision.tier == st.HOT, (name, decision)
        assert any("source unreadable" in r and str(state) in r
                   for r in decision.reasons), decision.reasons
        # The run's own output: HOT, or refused outright where the registry
        # itself cannot validate the run directory -- never eligible.
        inside = w.classify(run / "out")
        assert not inside.eligible
        assert inside.tier == st.HOT or inside.refused == st.UNKNOWN_OWNERSHIP, inside
        monkeypatch.setattr(os, name, real)
        assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    answers = run / "answers"
    (answers / "i1-a1-qa.json").mkdir(parents=True)        # an answer that cannot be read
    decision = w.classify(item)
    assert decision.tier == st.HOT and any("source unreadable" in r for r in decision.reasons)


def test_live_run_descendant_makes_hot(w):
    """Catches only the run directory itself judged (E-gap-a3663e73): a
    directory inside a run of the registered runs base that is byte-for-byte
    a VERIFIED_DUPLICATE of the canonical repository is HOT (``live_run``)
    and never planned while its run is ACTIVE/DEVELOPING, and while a FAILED
    run's process still holds it; the same for a run in ``runs`` scratch.
    Negative control: once the run is FAILED with no live process the same
    directory is a VERIFIED_DUPLICATE and is planned."""
    from hoh.store import RunStore

    files = {"out/a.txt": "alpha\n", "out/sub/b.txt": "beta\n"}
    run = w.run("20261001t0500z-live", condition="ACTIVE", stage="DEVELOPING", files=files)
    scratch_run = _scratch_run(_runs_scratch(w), "20261001t0501z-live",
                               condition="ACTIVE", stage="DEVELOPING", files=files)
    w.end_setup()
    for live in (run, scratch_run):
        decision = w.classify(live / "out")
        assert decision.tier == st.HOT
        assert any(r.startswith("live_run:") for r in decision.reasons), decision.reasons
        assert not w.relocation().plan(PROJECT, [live / "out"]).items
        assert w.classify(live).tier == st.WARM          # the run directory itself
        w.set_run(live, condition="FAILED", stage="DEVELOPING")
        with RunStore(live.parent, live.name).process_marker():
            assert w.classify(live / "out").tier == st.HOT
        assert w.classify(live / "out").tier == st.VERIFIED_DUPLICATE
        assert [i["path"] for i in w.relocation().plan(PROJECT, [live / "out"]).items] == [
            str(live / "out")]


# --------------------------------------------------------------------------- #
# K9 -- the /proc probe reads only processes of the same uid
# --------------------------------------------------------------------------- #


def test_proc_probe_same_uid_only(w, monkeypatch):
    """Catches other uids read: in a fake ``/proc``, pid 11 (own uid) and
    pid 22 (another uid) both have a cwd inside the item; the probe reports
    pid 11 only and never reads a link of pid 22. Negative control: without
    pid 11 the item is not HOT although pid 22 still points inside."""
    item = w.copy("dup")
    w.end_setup()
    own = os.getuid()
    for pid in ("11", "22"):
        (w.proc / pid / "fd").mkdir(parents=True)
        (w.proc / pid / "cwd").symlink_to(item / "sub")
    (w.proc / "22" / "fd" / "3").symlink_to(item / "a.txt")
    uids = {str(w.proc / "11"): own, str(w.proc / "22"): own + 1}
    read: list[str] = []
    real = os.readlink

    def readlink(path, *a, **k):
        read.append(os.fspath(path))
        return real(path, *a, **k)

    monkeypatch.setattr(os, "readlink", readlink)
    probe = dict(proc_uid=lambda d: uids[d])
    decision = w.classify(item, **probe)
    assert decision.tier == st.HOT
    assert [r for r in decision.reasons if r.startswith("process:")] == [
        f"process: 11 cwd -> {item / 'sub'}"]
    assert not [p for p in read if p.startswith(str(w.proc / "22"))]
    (w.proc / "11").rename(w.tmp / "proc-11-gone")
    assert w.classify(item, **probe).tier == st.VERIFIED_DUPLICATE


# --------------------------------------------------------------------------- #
# K7 -- the rest of the module
# --------------------------------------------------------------------------- #


def test_inventory_asks_registry_for_every_entry(w):
    """Catches the inventory asking the registry only about the item: a
    symlink inside the item pointing out of the namespace makes the item
    UNKNOWN_OWNERSHIP although the item itself is OWNED. Negative control:
    a symlink pointing to a sibling inside the item is fine."""
    item = w.copy("dup")
    (item / "inside").symlink_to("a.txt")
    w.end_setup()
    assert w.classify(item).refused is None
    (item / "outside").symlink_to(w.tmp / "home")
    decision = w.classify(item)
    assert decision.refused == st.UNKNOWN_OWNERSHIP


def test_classify_tier_takes_no_result_sets():
    """Catches an override parameter: `classify_tier` accepts only the path,
    the registry and the sources -- no reference, lease, finding or proof
    set. Negative control: the scan sees the parameters that are there."""
    import inspect

    names = list(inspect.signature(st.classify_tier).parameters)
    assert names == ["path", "registry", "sources"]
    for word in ("reference", "lease", "finding", "proof", "override", "force"):
        assert not [n for n in names if word in n]


def test_storage_tiers_removes_nothing():
    """Catches a removal call in `hoh.storage_tiers`: no remove, unlink,
    rmdir, rmtree or truncate call and no shutil import. Negative control:
    the same scanner finds ``os.unlink`` in a probe."""
    def hits(tree):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in {"remove", "unlink", "rmdir", "removedirs", "rmtree", "truncate"}:
                    out.append(name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if any(n.split(".")[0] == "shutil" for n in names):
                    out.append("shutil")
        return out
    assert hits(ast.parse(Path(st.__file__).read_text(encoding="utf-8"))) == []
    assert hits(ast.parse("import os\nos.unlink(p)\n")) == ["unlink"]


def test_git_judging_takes_no_optional_locks(w, monkeypatch):
    """Catches ``GIT_OPTIONAL_LOCKS=0`` dropped from this module's git
    environment (Revision 2 item 5: the only write to the canonical
    repository is the retain ref; Revision 2 item 8 and Revision 8 item 1:
    judging must not change the inventory a plan records and the quarantine
    is compared with). A clone whose tracked file is touched -- same bytes,
    new mtime, so its ``.git/index`` is stat-stale and an optional-lock git
    command would refresh it -- is judged by `classify_tier`: the index stays
    byte-identical, and every git subprocess `storage_tiers` itself starts
    carries ``GIT_OPTIONAL_LOCKS=0``. Mutant: the variable dropped from
    `storage_tiers._git_env`. Negative control: a plain ``git status`` in the
    same clone (optional locks allowed) does rewrite the index, so the
    staleness the test relies on is real."""
    clone = w.clone("clone")
    w.end_setup()
    readme = clone / "README"
    before_mtime = os.stat(readme).st_mtime_ns
    os.utime(readme, ns=(before_mtime + 7_000_000_000, before_mtime + 7_000_000_000))
    index = clone / ".git" / "index"
    index_bytes = index.read_bytes()

    started: list[tuple[list, dict | None]] = []
    real_run = subprocess.run

    def recording_run(args, *a, **kw):
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller == st.__name__ and args and os.path.basename(str(args[0])) == "git":
            started.append((list(args), kw.get("env")))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(st.subprocess, "run", recording_run)
    decision = w.classify(clone)
    monkeypatch.setattr(st.subprocess, "run", real_run)
    assert decision.tier == st.REPRODUCIBLE_SCRATCH, decision.notes
    assert started, "classify_tier started no git subprocess of its own"
    lacking = [args for args, env in started
               if env is None or env.get("GIT_OPTIONAL_LOCKS") != "0"]
    assert lacking == [], lacking
    assert index.read_bytes() == index_bytes
    # the negative control: optional locks allowed, git refreshes the index
    git(clone, "status", "--porcelain")
    assert index.read_bytes() != index_bytes


# --------------------------------------------------------------------------- #
# K18 -- Revision 9 item 5: nothing mounted inside an item is in it
# --------------------------------------------------------------------------- #


def test_inventory_refuses_other_device(w, monkeypatch):
    """Catches the device check dropped from `inventory` (Revision 9 item 5):
    one entry of a duplicate (``sub/b.txt``) reports another ``st_dev`` than
    the item itself -- as an archive target or anything else mounted inside
    the item would -- through a monkeypatched ``storage_tiers._lstat``; the
    inventory refuses it (FOREIGN_DEVICE), so the item is UNKNOWN_OWNERSHIP
    and never in a removal set. Mutant: the device check dropped (the item
    stays a VERIFIED_DUPLICATE). Negative control: without the patch the same
    item is a VERIFIED_DUPLICATE and its inventory lists ``sub/b.txt``."""
    item = w.copy("dup")
    w.end_setup()
    assert w.classify(item).tier == st.VERIFIED_DUPLICATE
    assert "sub/b.txt" in [e.rel for e in st.inventory(item, w.registry, PROJECT)]
    real = st._lstat

    class OtherDevice:
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return getattr(self._info, name)

        @property
        def st_dev(self):
            return self._info.st_dev + 1

    def lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if os.fspath(path).endswith(os.sep + os.path.join("dup", "sub", "b.txt")):
            return OtherDevice(info)
        return info

    monkeypatch.setattr(st, "_lstat", lstat)
    decision = w.classify(item)
    assert decision.refused == st.UNKNOWN_OWNERSHIP and decision.tier is None
    assert st.FOREIGN_DEVICE in decision.reasons[0]
    with pytest.raises(st._NotOwned, match=st.FOREIGN_DEVICE):
        st.inventory(item, w.registry, PROJECT, inodes={})
