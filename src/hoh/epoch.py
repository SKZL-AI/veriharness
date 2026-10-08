"""The master epoch: what the current epoch is, who holds it, and why it changed.

V3.3 P2-08. Several parts of the product already carry an epoch and refuse
work that does not match it -- `hoh.timers.Timer` holds `program_epoch` and
`session_epoch`, the sentinel sweeps by epoch, `AutonomyContract` is pinned to
one. This module is the thing they all refer to: an authority, on disk, that
answers "which epoch is current, and who holds it".

Two rules from the plan decide the design. The self-dogfood bootstrap rule:

    A stable VeriHarness controller may build a newer VeriHarness candidate,
    but may not silently replace itself mid-epoch.

and the P2 exit condition:

    kill controller/master, quota block, reboot, resume with no duplicate
    accepted work.

Put together: a master that has been replaced must not be able to keep acting
as though it were current, and the record of who held authority when must
survive the replacement. A stale master that writes one more decision is
exactly how duplicate accepted work appears. So:

* every epoch is chained to its predecessor by digest, so a **fork** -- two
  epoch 5s with different predecessors -- is something `fsck` reports rather
  than something anybody has to trust away;
* the chain is written atomically (beside, fsync, replace) because the exit
  condition includes a reboot, and a half-written epoch file is worse than none;
* history is never rewritten: beginning an epoch appends, and the file's
  previous form is copied to a versioned name before the new one replaces it;
* `authority_problems` refuses a master behind the store, ahead of it, at the
  right number with the wrong id, and anyone at all before an epoch has begun.
  Absence is not permission.

What this module deliberately does not do: the leader lease (P2-09), session
succession (P2-07), or wiring into timers, sentinel or controller. It also
never reads the wall clock -- `now` is a parameter, like everywhere a test has
to be able to say what time it is.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, ValidationError, model_validator

from .contracts import Strict
from .durable_io import durable_create, durable_write

SCHEMA = "hoh-epoch/1"
FILENAME = "epochs.json"

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class EpochError(ValueError):
    """A request against the epoch store that is refused, or a store that
    cannot be read as an epoch chain."""


class Reason(StrEnum):
    """Why an epoch began. The order is part of the contract."""

    #: The very first epoch of a program. Only epoch 1 may carry it.
    INITIAL = "INITIAL"
    #: A successor session took over from a master that ended or was lost.
    SUCCESSION = "SUCCESSION"
    #: A human operator moved authority deliberately.
    OPERATOR = "OPERATOR"
    #: A controller replacing itself with a newer candidate. A member rather
    #: than a comment because the bootstrap rule is about exactly this case:
    #: it must be said, and it must be said by beginning a new epoch.
    SELF_REPLACEMENT = "SELF_REPLACEMENT"
    #: Authority re-established after a crash, reboot or quota block.
    RECOVERY = "RECOVERY"


def epoch_digest(*, epoch: int, master_id: str, reason: Reason | str,
                 previous_digest: str, note: str = "") -> str:
    """sha256 over the canonical JSON of the content.

    Deliberately not over `began_at`: the digest names *what* this epoch is --
    its number, holder, reason, predecessor and note -- not when some process
    happened to write it down. Two records of the same epoch that differ only
    in arrival time are the same epoch; two that differ in content are a fork.
    """
    content = {"epoch": int(epoch), "master_id": str(master_id),
               "reason": str(Reason(reason).value),
               "previous_digest": str(previous_digest), "note": str(note)}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MasterEpoch(Strict):
    """One epoch of master authority. Frozen: an epoch that could be edited
    after the fact is not a record of who held authority when."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    epoch: int
    master_id: str
    reason: Reason
    began_at: str
    previous_digest: str = ""
    note: str = ""
    #: Computed when absent; a supplied value must match the content.
    epoch_digest: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "MasterEpoch":
        if self.epoch < 1:
            raise ValueError(f"epoch must be >= 1, got {self.epoch}")
        if not self.master_id:
            raise ValueError("master_id must not be empty: an epoch nobody holds "
                             "is authority nobody can be asked about")
        if self.epoch == 1:
            if self.reason is not Reason.INITIAL:
                raise ValueError(f"epoch 1 must begin with Reason.INITIAL, got {self.reason}")
            if self.previous_digest:
                raise ValueError("epoch 1 has no predecessor; previous_digest must be empty")
        else:
            if self.reason is Reason.INITIAL:
                raise ValueError(f"epoch {self.epoch} cannot be INITIAL; only epoch 1 is")
            if not self.previous_digest:
                raise ValueError(f"epoch {self.epoch} must name its predecessor's digest")
        expected = epoch_digest(epoch=self.epoch, master_id=self.master_id,
                                reason=self.reason, previous_digest=self.previous_digest,
                                note=self.note)
        if not self.epoch_digest:
            object.__setattr__(self, "epoch_digest", expected)
        elif self.epoch_digest != expected:
            raise ValueError(f"epoch {self.epoch}: epoch_digest {self.epoch_digest} does "
                             f"not match its content ({expected})")
        return self


class EpochStore:
    """The epoch chain of one root, in one file, replaced atomically.

    Nothing is ever taken away: `begin` appends, and before the file is
    replaced its previous form is copied to `epochs.json.v<N>.<UTC>` beside
    it. Reading is split in two on purpose -- `_records()` returns the raw
    rows so that `fsck` can diagnose a deliberately broken file, while
    `chain()` and `current()` refuse to hand out epochs from a chain that
    does not validate.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.path = self.root / FILENAME

    # --- reading -----------------------------------------------------------

    def _records(self) -> list[Any]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EpochError(f"{self.path} is not readable as an epoch chain: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("epochs"), list):
            raise EpochError(f"{self.path} does not hold an epoch chain")
        return raw["epochs"]

    def chain(self) -> list[MasterEpoch]:
        """Every epoch, in epoch order. Refuses a chain `fsck` finds broken:
        handing out one row of a forked history is choosing a side silently."""
        problems = self.fsck()
        if problems:
            raise EpochError(f"{self.path} fails fsck: " + "; ".join(problems))
        return sorted((MasterEpoch.model_validate(r) for r in self._records()),
                      key=lambda e: e.epoch)

    def current(self) -> MasterEpoch | None:
        """The latest epoch, or None when nothing has begun yet."""
        chain = self.chain()
        return chain[-1] if chain else None

    def fsck(self) -> list[str]:
        """The structural problems of the chain on disk, or []."""
        problems: list[str] = []
        valid: list[MasterEpoch] = []
        for i, rec in enumerate(self._records()):
            # A first epoch that is not INITIAL is refused by MasterEpoch itself;
            # say it in the words fsck promises rather than as a validation error.
            if isinstance(rec, dict) and rec.get("epoch") == 1 \
                    and rec.get("reason") != Reason.INITIAL.value:
                problems.append(f"first epoch is not INITIAL (reason {rec.get('reason')!r})")
                rec = {**rec, "reason": Reason.INITIAL.value, "epoch_digest": ""}
            try:
                valid.append(MasterEpoch.model_validate(rec))
            except ValidationError as exc:
                msgs = "; ".join(e["msg"] for e in exc.errors())
                problems.append(f"record {i} is not a valid epoch: {msgs}")
        if not valid:
            return problems
        by_number: dict[int, list[MasterEpoch]] = {}
        for e in valid:
            by_number.setdefault(e.epoch, []).append(e)
        numbers = sorted(by_number)
        if numbers[0] != 1:
            problems.append(f"gap: chain starts at epoch {numbers[0]}, not 1")
        for lo, hi in zip(numbers, numbers[1:]):
            if hi != lo + 1:
                problems.append(f"gap: epoch {lo} is followed by epoch {hi}")
        for n in numbers:
            if len(by_number[n]) > 1:
                problems.append(f"duplicate epoch number {n} ({len(by_number[n])} records): "
                                "a fork")
        for n in numbers:
            if n == 1 or (n - 1) not in by_number:
                continue
            predecessors = {p.epoch_digest for p in by_number[n - 1]}
            for e in by_number[n]:
                if e.previous_digest not in predecessors:
                    problems.append(f"broken link: epoch {n} names previous_digest "
                                    f"{e.previous_digest[:16]}, which is not the digest of "
                                    f"the recorded epoch {n - 1}")
        return problems

    # --- writing -----------------------------------------------------------

    def begin(self, master_id: str, *, reason: Reason, now: str,
              note: str = "") -> MasterEpoch:
        """Begins the next epoch, chained to the current one, and writes it."""
        if not isinstance(now, str) or not _TIME.match(now):
            raise EpochError(f"now must be a UTC timestamp like 2026-09-24T00:00:00Z: {now!r}")
        reason = Reason(reason)
        current = self.current()   # refuses a broken chain before appending to it
        if current is None:
            if reason is not Reason.INITIAL:
                raise EpochError(f"the first epoch must be INITIAL, not {reason}")
            new = MasterEpoch(epoch=1, master_id=master_id, reason=reason, began_at=now,
                              previous_digest="", note=note)
        else:
            if reason is Reason.INITIAL:
                raise EpochError(f"Reason.INITIAL is only for the first epoch; epoch "
                                 f"{current.epoch} has already begun")
            new = MasterEpoch(epoch=current.epoch + 1, master_id=master_id, reason=reason,
                              began_at=now, previous_digest=current.epoch_digest, note=note)
        records = self._records()
        records.append(json.loads(new.model_dump_json()))
        self._write(records, now=now)
        return new

    def _versioned_name(self, now: str) -> Path:
        stamp = now.replace("-", "").replace(":", "")
        n = 1 + sum(1 for _ in self.root.glob(f"{FILENAME}.v*"))
        while True:
            candidate = self.root / f"{FILENAME}.v{n}.{stamp}"
            if not candidate.exists():
                return candidate
            n += 1

    def _preserve_previous(self, now: str) -> None:
        """Copies the current file to a versioned name before it is replaced.
        Exclusive creation, so an existing version is never overwritten: the
        copy is published through `durable_io.durable_create` (P2-41b W2),
        which never replaces a name and never leaves a half version. It keeps
        the mode of the current file."""
        if not self.path.is_file():
            return
        while True:
            target = self._versioned_name(now)
            try:
                durable_create(target, self.path.read_bytes(),
                               mode=stat.S_IMODE(self.path.stat().st_mode))
                return
            except FileExistsError:
                continue

    def _write(self, records: list[Any], *, now: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema_version": SCHEMA, "written_at": now,
                              "epochs": records}, indent=2) + "\n"
        self._preserve_previous(now)
        # The mechanism (P2-41b W1): temp beside, fsync, `os.replace`,
        # directory fsync -- inside `durable_io.durable_write`. On ENOSPC or
        # EIO the chain keeps its bytes and the error is raised; a failed
        # directory fsync is `DurableWriteNotSynced`, no longer swallowed.
        # 0o600, the mode the earlier `mkstemp` temp gave the chain.
        durable_write(self.path, payload, mode=0o600)


def authority_problems(store: EpochStore, *, master_id: str, claimed_epoch: int) -> list[str]:
    """Why this master may not act, or []. [] only for the holder of the
    current epoch, claiming exactly that epoch."""
    try:
        broken = store.fsck()
    except EpochError as exc:
        return [f"the epoch store cannot be read: {exc}"]
    if broken:
        return ["the epoch chain fails fsck, so nobody holds authority over it: "
                + "; ".join(broken)]
    current = store.current()
    if current is None:
        return [f"no epoch has begun; nothing has authority yet, and a claim to epoch "
                f"{claimed_epoch} by {master_id!r} is not one"]
    if claimed_epoch < current.epoch:
        return [f"superseded: {master_id!r} claims epoch {claimed_epoch}, but the current "
                f"epoch is {current.epoch} (held by {current.master_id!r})"]
    if claimed_epoch > current.epoch:
        return [f"epoch {claimed_epoch} has not begun: {master_id!r} claims it, but the "
                f"current epoch is {current.epoch}; a claim ahead of the store is reading "
                f"state nobody wrote"]
    if master_id != current.master_id:
        return [f"{master_id!r} is not the holder of epoch {current.epoch}; "
                f"{current.master_id!r} is -- a second master at one epoch"]
    return []


def may_replace_self(store: EpochStore, *, master_id: str, candidate_id: str,
                     claimed_epoch: int) -> list[str]:
    """The bootstrap rule as a refusal: a controller may build a newer
    candidate and may not replace itself mid-epoch. [] only once a fresh
    epoch has begun with Reason.SELF_REPLACEMENT and is held by `master_id`."""
    problems = authority_problems(store, master_id=master_id, claimed_epoch=claimed_epoch)
    if problems:
        return problems
    if candidate_id == master_id:
        return [f"candidate {candidate_id!r} is the master itself: replacing yourself with "
                f"yourself is a no-op dressed as a transition"]
    current = store.current()
    assert current is not None  # authority_problems refused an empty store
    if current.reason is not Reason.SELF_REPLACEMENT:
        return [f"replacing self within epoch {current.epoch} (begun for {current.reason}) is "
                f"refused: the replacement needs a new epoch begun with "
                f"Reason.SELF_REPLACEMENT"]
    return []
