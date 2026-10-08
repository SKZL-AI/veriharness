"""Durable-state migrations: forward only, refused from the future, preserved.

A programme that runs for weeks outlives the shape of its own records, and the
P2 exit condition -- kill, quota block, reboot, resume with no duplicate
accepted work -- is decided by whether a resumed run reads its history the way
it was written. Two ways of getting that wrong are both silent, and this module
exists to make each of them loud:

1. **A record from the future.** A store written by a newer version and opened
   by an older one looks readable: unknown fields drop out, defaults fill in,
   and the run continues on a state that is missing what it does not
   understand. Here such a record is a refusal (`FromTheFuture`), never a best
   effort, and a record without a readable `schema_version` is refused as well
   rather than defaulted to "current".
2. **A migration nobody can audit.** Every real migration first keeps the
   previous bytes beside the original as `<name>.v<old>.<UTC>` -- the house rule
   is replace instead of delete, and the same naming `store.py` uses for parked
   states -- then writes the new form atomically, and returns a
   `MigrationReport` with the digest of every record before and after.

`migrate` is a dry run unless told otherwise: a migration that happens because
somebody forgot a flag is the wrong way round. Nothing in this module takes a
file away; the test module greps the source to keep it that way.

The version field is `schema_version`, as on `contracts.RunState`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .contracts import SCHEMA_VERSION
from .durable_io import durable_create, durable_write

VERSION_FIELD = "schema_version"

# Already-preserved copies (`state.json.v1.2026-09-24T00-00-00Z`) are history,
# not records; they are never migrated a second time.
_PRESERVED = re.compile(r"\.v\d+\.[^/]+$")


class Direction(Enum):
    """What a record, a plan or a report amounts to. `NONE` is an answer."""

    FORWARD = "forward"
    NONE = "none"
    REFUSED = "refused"


class MigrationError(Exception):
    """Base of every refusal in this module."""


class FromTheFuture(MigrationError):
    """A record or path starts above what this code knows. Never read as current."""


class BackwardsMigration(MigrationError):
    """A path that goes down. Migrations are forward only."""


class AmbiguousChain(MigrationError):
    """Two steps from the same version."""


class ChainGap(MigrationError):
    """A registration that would leave a hole in the chain."""


class NoPath(MigrationError):
    """No chain of registered steps connects the two versions."""


class PlanRefused(MigrationError):
    """A plan with refusals was asked to be applied. Nothing was touched."""

    def __init__(self, message: str, plan: "MigrationPlan") -> None:
        super().__init__(message)
        self.plan = plan


def _is_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Migration:
    """One step, exactly one version up. Anything else is refused here."""

    from_version: int
    to_version: int
    name: str
    description: str
    apply: Callable[[dict], dict] = field(compare=False)

    def __post_init__(self) -> None:
        if not (_is_version(self.from_version) and _is_version(self.to_version)):
            raise MigrationError(
                f"migration {self.name!r}: versions must be integers, got "
                f"{self.from_version!r} -> {self.to_version!r}")
        if self.to_version != self.from_version + 1:
            raise MigrationError(
                f"migration {self.name!r}: {self.from_version} -> "
                f"{self.to_version} is not one step; a step that jumps cannot be "
                "composed or audited")
        if not isinstance(self.name, str) or not self.name.strip():
            raise MigrationError("a migration needs a name")
        if not isinstance(self.description, str) or not self.description.strip():
            raise MigrationError(f"migration {self.name!r} needs a description")
        if not callable(self.apply):
            raise MigrationError(f"migration {self.name!r}: apply is not callable")


class Registry:
    """The ordered, contiguous chain of steps.

    With no steps registered, `current_version` is `base_version` (the
    contracts' `SCHEMA_VERSION` by default). Once steps exist it is the highest
    `to_version` of the chain.
    """

    def __init__(self, base_version: int = SCHEMA_VERSION) -> None:
        if not _is_version(base_version):
            raise MigrationError(f"base version must be an integer: {base_version!r}")
        self.base_version = base_version
        self._steps: dict[int, Migration] = {}

    def register(self, migration: Migration) -> Migration:
        if not isinstance(migration, Migration):
            raise MigrationError(f"not a Migration: {migration!r}")
        if migration.from_version in self._steps:
            other = self._steps[migration.from_version]
            raise AmbiguousChain(
                f"two migrations from version {migration.from_version}: "
                f"{other.name!r} and {migration.name!r}")
        if self._steps:
            low, high = min(self._steps), max(self._steps)
            if not (migration.from_version == high + 1
                    or migration.from_version == low - 1):
                raise ChainGap(
                    f"migration {migration.name!r} from version "
                    f"{migration.from_version} does not join the chain "
                    f"{low} -> {high + 1}; the chain would have a gap")
        self._steps[migration.from_version] = migration
        return migration

    @property
    def steps(self) -> tuple[Migration, ...]:
        return tuple(self._steps[v] for v in sorted(self._steps))

    @property
    def lowest_version(self) -> int:
        return min(self._steps) if self._steps else self.base_version

    @property
    def current_version(self) -> int:
        return max(self._steps) + 1 if self._steps else self.base_version

    def path(self, from_version: int, to_version: int | None = None
             ) -> tuple[Migration, ...]:
        """The steps from `from_version` up to `to_version` (default: current).

        The future is checked first: a start above `current_version` is
        `FromTheFuture` whatever the target is.
        """
        current = self.current_version
        if not _is_version(from_version):
            raise MigrationError(f"not a version: {from_version!r}")
        if from_version > current:
            raise FromTheFuture(
                f"version {from_version} is above the current version {current}; "
                "a record from the future is never migrated and never read")
        target = current if to_version is None else to_version
        if not _is_version(target):
            raise MigrationError(f"not a version: {target!r}")
        if target < from_version:
            raise BackwardsMigration(
                f"{from_version} -> {target} goes backwards; migrations are "
                "forward only")
        if target > current:
            raise FromTheFuture(
                f"target version {target} is above the current version {current}")
        if target == from_version:
            return ()
        if from_version < self.lowest_version:
            raise NoPath(
                f"no registered step starts at version {from_version}; the chain "
                f"begins at {self.lowest_version}")
        return tuple(self._steps[v] for v in range(from_version, target))


# --- plan ---------------------------------------------------------------------

@dataclass(frozen=True)
class RecordPlan:
    """What one record would go through."""

    key: Any
    from_version: int
    to_version: int
    direction: Direction
    steps: tuple[str, ...]


@dataclass(frozen=True)
class Refusal:
    key: Any
    reason: str
    found_version: Any = None


@dataclass(frozen=True)
class MigrationPlan:
    """A dry run's answer. Frozen: a plan is evidence, not a work queue."""

    direction: Direction
    steps: tuple[RecordPlan, ...]
    counts: Mapping[str, int]
    refusals: tuple[Refusal, ...]
    from_versions: tuple[int, ...]
    to_version: int

    @property
    def applicable(self) -> bool:
        return self.direction is not Direction.REFUSED

    def require_applicable(self) -> None:
        if not self.applicable:
            reasons = "; ".join(f"{r.key}: {r.reason}" for r in self.refusals)
            raise PlanRefused(f"the plan is REFUSED and cannot be applied: {reasons}",
                              self)


def _items(records: Iterable[Any] | Mapping[Any, Any]) -> list[tuple[Any, Any]]:
    if isinstance(records, Mapping):
        return list(records.items())
    return list(enumerate(records))


def plan(records: Iterable[Any] | Mapping[Any, Any], *, registry: Registry
         ) -> MigrationPlan:
    """Report what migrating `records` would do, writing nothing.

    `records` is a sequence (keys are indexes) or a mapping of key -> record.
    """
    current = registry.current_version
    record_plans: list[RecordPlan] = []
    refusals: list[Refusal] = []
    for key, record in _items(records):
        if not isinstance(record, Mapping):
            refusals.append(Refusal(key, f"not a record object: {type(record).__name__}"))
            continue
        if VERSION_FIELD not in record:
            refusals.append(Refusal(
                key, f"no {VERSION_FIELD}; a record without a version is not "
                "assumed to be current"))
            continue
        version = record[VERSION_FIELD]
        if not _is_version(version):
            refusals.append(Refusal(key, f"{VERSION_FIELD} is not an integer: "
                                         f"{version!r}", version))
            continue
        try:
            steps = registry.path(version)
        except MigrationError as exc:
            refusals.append(Refusal(key, f"{type(exc).__name__}: {exc}", version))
            continue
        record_plans.append(RecordPlan(
            key=key, from_version=version, to_version=current,
            direction=Direction.FORWARD if steps else Direction.NONE,
            steps=tuple(s.name for s in steps)))

    counts = {
        Direction.FORWARD.name: sum(p.direction is Direction.FORWARD for p in record_plans),
        Direction.NONE.name: sum(p.direction is Direction.NONE for p in record_plans),
        Direction.REFUSED.name: len(refusals),
    }
    if refusals:
        direction = Direction.REFUSED
    elif counts[Direction.FORWARD.name]:
        direction = Direction.FORWARD
    else:
        direction = Direction.NONE
    found = {p.from_version for p in record_plans}
    found |= {r.found_version for r in refusals if _is_version(r.found_version)}
    return MigrationPlan(
        direction=direction,
        steps=tuple(record_plans),
        counts=MappingProxyType(counts),
        refusals=tuple(refusals),
        from_versions=tuple(sorted(found)),
        to_version=current,
    )


def apply_steps(record: Mapping[str, Any], *, registry: Registry) -> dict:
    """Run the chain over one record, re-reading every intermediate form.

    Each step receives a fresh parse of the previous step's serialised output,
    so a two-step path is two steps over what was actually produced, not one
    function that happens to share objects. The version field is stamped by
    this function; a step that sets it to anything else is refused.
    """
    version = record.get(VERSION_FIELD)
    current = json.loads(json.dumps(record))
    for step in registry.path(version):
        result = step.apply(current)
        if not isinstance(result, Mapping):
            raise MigrationError(f"step {step.name!r} returned "
                                 f"{type(result).__name__}, not a record")
        result = dict(result)
        stamped = result.get(VERSION_FIELD, step.from_version)
        if stamped not in (step.from_version, step.to_version):
            raise MigrationError(
                f"step {step.name!r} set {VERSION_FIELD} to {stamped!r}; it moves "
                f"{step.from_version} -> {step.to_version}")
        result[VERSION_FIELD] = step.to_version
        current = json.loads(json.dumps(result))
    return current


def record_digest(record: Any) -> str:
    """sha256 of the canonical JSON form of a record."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- migrate ------------------------------------------------------------------

@dataclass(frozen=True)
class FileMigration:
    """The audit line for one file."""

    file: str
    direction: Direction
    from_version: int
    to_version: int
    steps: tuple[str, ...]
    digest_before: str
    digest_after: str
    preserved_to: str | None


@dataclass(frozen=True)
class MigrationReport:
    """What `migrate` did (or, on a dry run, would do)."""

    direction: Direction
    dry_run: bool
    root: str
    to_version: int
    files: tuple[FileMigration, ...]
    refusals: tuple[Refusal, ...]
    plan: MigrationPlan

    @property
    def changed(self) -> tuple[FileMigration, ...]:
        return tuple(f for f in self.files if f.digest_before != f.digest_after)


def _stamp(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise MigrationError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _record_files(root: Path) -> list[Path]:
    found = []
    for candidate in sorted(root.rglob("*.json")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if any(part.startswith(".") for part in candidate.relative_to(root).parts):
            continue
        if _PRESERVED.search(candidate.name):
            continue
        found.append(candidate)
    return found


def _serialise(record: Mapping[str, Any]) -> str:
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _preserve(path: Path, raw: bytes, version: int, stamp: str) -> Path:
    """Copy the previous bytes beside the original. Never over a different copy."""
    target = path.with_name(f"{path.name}.v{version}.{stamp}")
    try:
        # P2-41b W8: published under a name that must not exist, whole or not
        # at all, with the mode of the record it preserves.
        durable_create(target, raw, mode=stat.S_IMODE(path.stat().st_mode))
    except FileExistsError:
        # A migration interrupted between preserving and replacing, resumed
        # with the same `now`, finds its own copy: the same bytes are the same
        # preservation. Different bytes are someone else's history.
        if target.read_bytes() == raw:
            return target
        raise MigrationError(
            f"{target.name} already exists; a preserved copy is never "
            "overwritten") from None
    return target


def _atomic_write(path: Path, payload: str) -> None:
    """Write beside, fsync, then replace -- the old form or the new, never half.

    The mechanism: temp beside, fsync, `os.replace`, directory fsync -- in
    `durable_io.durable_write` (P2-41b W7). On ENOSPC or EIO the record keeps
    its old form and the error is raised; the temp file is abandoned, never
    left standing. `mode=None`: the umask decides, as it did before.
    """
    durable_write(path, payload, mode=None)


def migrate(path: Path | str, *, registry: Registry, now: datetime,
            dry_run: bool = True) -> MigrationReport:
    """Migrate every JSON record file under `path` to `registry.current_version`.

    A dry run unless `dry_run=False`. The plan is made over all files first;
    if it is REFUSED, `PlanRefused` is raised and nothing is touched. All new
    forms are computed before the first write, so a failing step leaves the
    directory as it was. Then, per changed file: preserve the old bytes as
    `<name>.v<old>.<UTC>`, write the new form atomically. Files already at the
    current version are not written at all.
    """
    root = Path(path)
    if not root.is_dir():
        raise MigrationError(f"not a directory: {root}")
    stamp = _stamp(now)

    raw: dict[str, bytes] = {}
    records: dict[str, Any] = {}
    parse_refusals: list[Refusal] = []
    for file in _record_files(root):
        rel = file.relative_to(root).as_posix()
        data = file.read_bytes()
        raw[rel] = data
        try:
            records[rel] = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_refusals.append(Refusal(rel, f"not readable JSON: {exc}"))

    the_plan = plan(records, registry=registry)
    if parse_refusals:
        refusals = tuple(sorted(parse_refusals + list(the_plan.refusals),
                                key=lambda r: str(r.key)))
        counts = dict(the_plan.counts)
        counts[Direction.REFUSED.name] = len(refusals)
        the_plan = MigrationPlan(
            direction=Direction.REFUSED, steps=the_plan.steps,
            counts=MappingProxyType(counts), refusals=refusals,
            from_versions=the_plan.from_versions, to_version=the_plan.to_version)
    the_plan.require_applicable()

    pending: list[tuple[RecordPlan, dict]] = []
    for record_plan in the_plan.steps:
        record = records[record_plan.key]
        new = (apply_steps(record, registry=registry)
               if record_plan.direction is Direction.FORWARD else record)
        pending.append((record_plan, new))

    entries: list[FileMigration] = []
    for record_plan, new in pending:
        rel = record_plan.key
        preserved_to = None
        if record_plan.direction is Direction.FORWARD and not dry_run:
            file = root / rel
            preserved = _preserve(file, raw[rel], record_plan.from_version, stamp)
            _atomic_write(file, _serialise(new))
            preserved_to = preserved.relative_to(root).as_posix()
        entries.append(FileMigration(
            file=rel,
            direction=record_plan.direction,
            from_version=record_plan.from_version,
            to_version=record_plan.to_version,
            steps=record_plan.steps,
            digest_before=record_digest(records[rel]),
            digest_after=record_digest(new),
            preserved_to=preserved_to,
        ))

    return MigrationReport(
        direction=the_plan.direction,
        dry_run=dry_run,
        root=str(root),
        to_version=the_plan.to_version,
        files=tuple(entries),
        refusals=the_plan.refusals,
        plan=the_plan,
    )
