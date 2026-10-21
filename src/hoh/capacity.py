"""The capacity registry: one durable record of what the scheduler may count on (plan row P3-16, slice 1).

The scheduler, the broker and the router that follow (P3-17, P3-19, P3-41) all ask the same question before
they start work: *how much of each thing is there?* Today the answers live in four places -- a per-run budget sum
(`launcher.spent_budget`), a per-run quota parse (`quota.py`), a storage ledger (`storage_registry.py`) and the
operator's head. This module is the one place they are written down, as three kinds of capacity that are
measured separately and never added to each other:

* `dependency` -- how much independent work the plan can offer (the width of the ready frontier);
* `machine`    -- what this host can carry (sessions, bytes);
* `provider`   -- what a provider account may be asked for (sessions, requests, tokens).

**What this slice is.** The schema (a closed vocabulary), one canonical rendering, a strict parse, a read that
refuses every odd input, and a durable write; since slice 2 also the record's name under a state root and the pure
helpers of the operator's command (`hoh capacity set` / `show`, which owns the state-root check, the lock and the
clock). **What it is not.** It measures nothing, reserves nothing, decides nothing and is called only by that
command -- no scheduler reads it yet: a registry is a *record*, not a gate, and not a trust boundary (a process
that can write the file can write any limit; the files of a run's roles are not where it lives, but this module
does not enforce that). Reservations are P3-04, brokering is P3-17.

**Fail closed.** An absent fact is never zero and never unlimited: `read` and `parse` raise `CapacityError`
(with a stable `reason`) for a missing, unreadable, oversize, malformed, wrongly versioned, invalid or
non-canonical record, and `Registry.get` raises for a capacity nobody recorded. There is no default.

**Canonical.** A record is accepted only if it is exactly the bytes `render` would produce for its entries, so one
set of entries has one file and one digest (the sha256 of those bytes, which a later decision can quote to say
which registry it read). The module never reads a clock: the time of a fact (`as_of`) is the caller's.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .durable_io import durable_write, read_single_link

SCHEMA = "veriharness.capacity-registry/1"
"""The only schema version this module reads or writes."""

KINDS = ("dependency", "machine", "provider")
"""The three capacities the scheduler keeps apart."""

SOURCES = ("declared", "measured")
"""Who stated a limit: the operator (`declared`) or a probe (`measured`). Nothing else may."""

UNITS = ("bytes", "items", "requests", "sessions", "tokens")
"""What a limit counts. A unit nobody listed is refused, so `session` is not silently a second unit."""

MAX_ENTRIES = 256
"""Entries one registry may hold."""

MAX_LIMIT = 10 ** 15
"""The largest limit (a petabyte of bytes); a limit is an integer from 0 to this."""

MAX_BYTES = 256 * 1024
"""The largest record `read` or `parse` looks at (a registry of `MAX_ENTRIES` is a small fraction of it)."""

_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_AS_OF = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_FIELDS = ("kind", "name", "limit", "unit", "source", "as_of")


class CapacityError(ValueError):
    """A capacity record or request that is refused. `reason` is one of: `missing` (no record), `unreadable` (a
    record the file system or the record-file rules refuse), `oversize`, `malformed` (not one JSON object of
    ASCII text), `schema` (wrong or missing schema or keys), `invalid` (an entry or the entry list breaks a
    rule), `noncanonical` (valid content in a spelling `render` would not produce), `unknown` (no such
    capacity)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"capacity registry: {message}")
        self.reason = reason


def _refuse(message: str) -> CapacityError:
    return CapacityError("invalid", message)


def _shown(value: object) -> str:
    """A bounded, always-printable spelling of a value for a message. Never `repr` of an integer (Python refuses
    one of more than 4300 digits with a plain `ValueError`, which would leave a refusal as something else) and
    never an unbounded string."""
    if type(value) is int:
        return repr(value) if value.bit_length() <= 64 else f"an integer of {value.bit_length()} bits"
    if type(value) in (str, bytes):
        return repr(value[:40]) + (f" ... ({len(value)} long)" if len(value) > 40 else "")
    return f"a {type(value).__name__}"


def _where(target: Path) -> str:
    """A path for a message, bounded: its last 100 characters (the file name is the useful end)."""
    text = str(target)
    return text if len(text) <= 100 else f"...{text[-100:]}"


def _cause(exc: Exception) -> str:
    """Why an operating-system call failed, without the file name it may carry (the name can be long)."""
    return (getattr(exc, "strerror", None) or type(exc).__name__)[:80]


@dataclass(frozen=True)
class Entry:
    """One capacity: `limit` `unit`s of `kind`/`name`, stated by `source` as of `as_of` (UTC, `...Z`)."""

    kind: str
    name: str
    limit: int
    unit: str
    source: str
    as_of: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in KINDS:
            raise _refuse(f"kind {_shown(self.kind)} is not one of {', '.join(KINDS)}")
        if type(self.name) is not str or _NAME.fullmatch(self.name) is None:
            raise _refuse(f"name {_shown(self.name)} is not [a-z][a-z0-9_.-]{{0,63}}")
        if type(self.limit) is not int or not 0 <= self.limit <= MAX_LIMIT:
            raise _refuse(f"limit {_shown(self.limit)} is not an integer from 0 to {MAX_LIMIT}")
        if type(self.unit) is not str or self.unit not in UNITS:
            raise _refuse(f"unit {_shown(self.unit)} is not one of {', '.join(UNITS)}")
        if type(self.source) is not str or self.source not in SOURCES:
            raise _refuse(f"source {_shown(self.source)} is not one of {', '.join(SOURCES)}")
        if type(self.as_of) is not str or _AS_OF.fullmatch(self.as_of) is None:
            raise _refuse(f"as_of {_shown(self.as_of)} is not YYYY-MM-DDTHH:MM:SSZ")
        try:
            datetime.strptime(self.as_of, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise _refuse(f"as_of {_shown(self.as_of)} is not a calendar time") from exc

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in _FIELDS}

    @classmethod
    def from_dict(cls, value: object) -> "Entry":
        """The entry a parsed JSON object describes: exactly the six keys, nothing missing and nothing extra."""
        if type(value) is not dict or set(value) != set(_FIELDS):
            raise _refuse(f"an entry is an object with exactly the keys {', '.join(_FIELDS)}")
        return cls(**value)


@dataclass(frozen=True)
class Registry:
    """What a record said: its entries (sorted by kind, then name) and the sha256 of its exact bytes."""

    entries: tuple[Entry, ...]
    digest: str

    def get(self, kind: str, name: str) -> Entry:
        """The entry for `kind`/`name`; `CapacityError` (`unknown`) when nobody recorded one -- never a default."""
        for entry in self.entries:
            if entry.kind == kind and entry.name == name:
                return entry
        raise CapacityError("unknown", f"no capacity recorded for {_shown(kind)} {_shown(name)}")

    def limit(self, kind: str, name: str) -> int:
        return self.get(kind, name).limit

    def of_kind(self, kind: str) -> tuple[Entry, ...]:
        """Every entry of one kind, in name order (empty if there is none, and for an unknown kind)."""
        return tuple(entry for entry in self.entries if entry.kind == kind)


def _checked(entries: Iterable[Entry]) -> list[Entry]:
    """The entries as a sorted list, or a refusal: only `Entry` objects, at most `MAX_ENTRIES`, no duplicate key."""
    try:
        items = list(entries)
    except TypeError as exc:
        raise _refuse(f"{_shown(entries)} is not a collection of entries") from exc
    if len(items) > MAX_ENTRIES:
        raise _refuse(f"{len(items)} entries; at most {MAX_ENTRIES}")
    seen: set[tuple[str, str]] = set()
    for item in items:
        if type(item) is not Entry:
            raise _refuse(f"{_shown(item)} is not an Entry")
        key = (item.kind, item.name)
        if key in seen:
            raise _refuse(f"{item.kind} {item.name!r} is recorded twice")
        seen.add(key)
    return sorted(items, key=lambda e: (e.kind, e.name))


def render(entries: Iterable[Entry]) -> bytes:
    """The one record for these entries: ASCII JSON, sorted keys, entries sorted by kind and name, one newline."""
    document = {"schema": SCHEMA, "entries": [e.as_dict() for e in _checked(entries)]}
    return (json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice in one object")
    return dict(pairs)


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def parse(data: bytes) -> Registry:
    """The registry a record's bytes describe, or a `CapacityError`. Pure: no file is touched."""
    if type(data) is not bytes:
        raise CapacityError("malformed", "a record is bytes")
    if len(data) > MAX_BYTES:
        raise CapacityError("oversize", f"{len(data)} bytes; at most {MAX_BYTES}")
    if not data.isascii():
        raise CapacityError("malformed", "a record is ASCII text")
    try:
        document = json.loads(data.decode("ascii"), object_pairs_hook=_no_duplicate_keys,
                              parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise CapacityError("malformed", f"not JSON: {exc}") from exc
    if type(document) is not dict:
        raise CapacityError("malformed", "a record is one JSON object")
    if set(document) != {"schema", "entries"} or document["schema"] != SCHEMA:
        raise CapacityError("schema", f"a record is an object with exactly the keys schema ({SCHEMA}) and entries")
    listed = document["entries"]
    if type(listed) is not list:
        raise _refuse("entries is a list")
    if len(listed) > MAX_ENTRIES:
        raise _refuse(f"{len(listed)} entries; at most {MAX_ENTRIES}")
    entries = _checked(Entry.from_dict(item) for item in listed)
    if render(entries) != data:
        raise CapacityError("noncanonical", "valid content, but not the bytes render() produces for it")
    return Registry(entries=tuple(entries), digest=hashlib.sha256(data).hexdigest())


def read(path: str | os.PathLike) -> Registry:
    """The registry in the file `path`. Every failure is a `CapacityError` -- the reader has no facts then.

    Checked in this order: the name does not exist -> `missing`; not a regular file with exactly one name (a
    dangling link, a link, a directory, a FIFO, a second name), a file the user cannot read, a name with a NUL
    and a `path` that is not a path -> `unreadable` (`durable_io.read_single_link` rules, no link followed);
    then a size over `MAX_BYTES` -> `oversize`; then `parse`.
    """
    try:
        target = Path(path)
    except TypeError as exc:
        raise CapacityError("unreadable", f"{_shown(path)} is not a path") from exc
    try:
        status = os.lstat(target)
    except FileNotFoundError as exc:
        raise CapacityError("missing", f"{_where(target)} does not exist") from exc
    except (OSError, ValueError) as exc:               # ValueError: a NUL in the name
        raise CapacityError("unreadable", f"{_where(target)}: {_cause(exc)}") from exc
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise CapacityError("unreadable", f"{_where(target)} is not a regular file with exactly one name")
    if status.st_size > MAX_BYTES:
        raise CapacityError("oversize", f"{_where(target)} is {status.st_size} bytes; at most {MAX_BYTES}")
    try:
        data = read_single_link(target)
    except (OSError, ValueError) as exc:
        raise CapacityError("unreadable", f"{_where(target)}: {_cause(exc)}") from exc
    if data is None:
        raise CapacityError("missing", f"{_where(target)} does not exist")
    return parse(data)


def write(path: str | os.PathLike, entries: Iterable[Entry]) -> Registry:
    """Replace the record at `path` with the one for `entries`, or leave it exactly as it was.

    The entries are checked and rendered first, and the bytes must parse back, so an invalid set never reaches
    the disk. The file is written through `durable_io.durable_write` (mode 0600). A `CapacityError` leaves the
    old record. An `OSError` of the write leaves **either** record, and the exception says which: before the
    replace (`DiskFullError`, any other `OSError`) the old record stays; `DurableWriteNotSynced` is raised
    **after** the replace -- the new record is in place and only its durability is uncertain. A caller that
    must know calls `read` and compares the digest; it never assumes the old record. The whole record is
    replaced: two writers that each read and then write lose one update -- who may write, and in what order, is
    the broker's.
    """
    data = render(entries)
    registry = parse(data)
    durable_write(Path(path), data, mode=0o600)
    return registry


# --- slice 2: where the record lives, and what the operator's command needs of it ---------------------------------
# Everything below is as pure as the rest of the module: the state root check, the lock, the clock and the printing
# belong to `hoh capacity` (`cli.cmd_capacity_set`, `cli.cmd_capacity_show`), which is the one caller.

FILE_NAME = "capacity-registry.json"
"""The record's name under a state root: one file per host, flat, beside `storage-policy.json` and
`steward-policy.json`. The storage registry classifies it `UNKNOWN` (`storage_registry._classify_internal`: not part
of its layout), which every storage action reads as no-touch -- nothing cleans it, nothing adopts it."""

_LIMIT = re.compile(r"0|[1-9][0-9]{0,15}")
_SHOWN = 240


def location(state_root: str | os.PathLike) -> Path:
    """The file of the registry of the state root `state_root`: `<state root>/capacity-registry.json`.

    Pure: nothing is checked and nothing is touched. Whether the directory is a registered, initialised state root
    is the caller's to ask (`hoh capacity` does, through `StorageRegistry`, before it reads or writes)."""
    return Path(os.fspath(state_root)) / FILE_NAME


def parse_limit(text: str) -> int:
    """The limit a command line spells: ASCII decimal digits only, at most 16 of them, no sign, no space, no
    leading zero, no exponent or underscore (`int()` would accept every one of those, and the Unicode digits too).
    A value above `MAX_LIMIT` is `Entry`'s refusal, not this one's. `CapacityError` (`invalid`) otherwise."""
    if type(text) is not str or _LIMIT.fullmatch(text) is None:
        raise _refuse(f"limit {_shown(text)} is not a plain decimal integer (digits 0-9, at most 16, no sign, "
                      "space or leading zero)")
    return int(text)


def set_entry(current: Registry | None, entry: Entry) -> tuple[tuple[Entry, ...], Entry | None]:
    """`(entries, previous)`: the entries of `current` (none for `None`) with `entry` in place of the one that has
    its kind and name, and that previous entry (`None` if there was none). Sorted as `render` sorts them; more than
    `MAX_ENTRIES` is `CapacityError` (`invalid`). Pure: nothing is written."""
    if current is not None and type(current) is not Registry:
        raise _refuse(f"{_shown(current)} is not a Registry")
    old = () if current is None else current.entries
    previous = next((e for e in old if (e.kind, e.name) == (entry.kind, entry.name)), None)
    kept = [e for e in old if (e.kind, e.name) != (entry.kind, entry.name)]
    return tuple(_checked([*kept, entry])), previous


def view(registry: Registry) -> dict:
    """What a person is shown of a registry: its digest and its entries in the record's order. Every string in it
    already obeys the grammar (a closed vocabulary, `[a-z][a-z0-9_.-]{0,63}` names, an ASCII time), so it is
    printable as it is; at most `MAX_ENTRIES` entries, because `read` refuses more."""
    return {"digest": registry.digest, "entries": [entry.as_dict() for entry in registry.entries]}


def printable(text: object, limit: int = _SHOWN) -> str:
    """A bounded, escaped spelling of anything for a terminal line: at most `limit` characters of it, with every
    control character, escape sequence, newline and non-ASCII character (a lone surrogate included) written as a
    backslash escape, and the length said when it was cut. Never raises for any `str`."""
    whole = text if type(text) is str else _shown(text)
    spelled = ascii(whole[:limit])[1:-1]
    return spelled + (f"... ({len(whole)} characters)" if len(whole) > limit else "")
