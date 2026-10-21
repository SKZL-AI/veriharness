"""The wave admission's decision record: create-only, bound by its own digest (plan rows P3-17 slice 3, P3-19 slice 3).

`capacitycheck.admit_wave` takes one decision for the nodes a wave would start -- over the registry, the capacity source's
snapshot of what is held and what the providers say, the reserve for critical nodes and, with a storage policy, the
bytes and inodes of a run -- and holds what it admits in the capacity ledger. The ledger says what is **held**; this module
is where what was **decided**, and on what, is written down: one file per admission,
`<project>/capacity/capacity-<decision id>.json`, which is never replaced, carries the sha256 of its own body, and is
read back before the admission is allowed to stand.

**What is written is the caller's** (`capacitycheck.admit_wave` builds the body: the policy, what each class of node wants,
the holds the decision was handed, what the provider state held back, the provider and slot facts, the registry's digest
and the snapshot's, the plan with every node's verdict and reason, the nodes admitted and the rows held). This module
only renders, writes and reads: it knows the record's keys and its framing, not what a capacity means. It reads no clock,
takes no lock and imports nothing of the product but the durable-write primitives.

**Create-only, and read back.** `write_record` makes the directory owner-only when it is missing and refuses one that is
a link or not a directory; counts the records in it and refuses at `MAX_RECORDS` (files are inodes, nothing here removes
one, and the refusal names the remedy); publishes the bytes under a name that must not exist (`durable_io.durable_create`:
a name that exists is never replaced); and reads the file back through `read_record`, which must find the digest it
wrote. Whatever fails is `RecordError`, and the caller admits nobody on it. **Terminal states**: one attempt per
admission (there is no retry here and no counter to persist), a record that cannot be written is the admission's refusal
-- named in its reason, and in the controller's trail as a `CAPACITY_WAIT` -- and a later admission decides afresh
under a new id.

**Strict reader.** `read_record` is a regular file of one name within `MAX_RECORD_BYTES`; strict JSON (a repeated key,
`NaN` and `Infinity` are refused); exactly the keys of a record and its schema; a file name that is `capacity-<its
decision id>.json`; the very bytes the canonical rendering gives for its body; and a digest that matches that body.
A file that fails any of these is not a record.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .durable_io import durable_create, read_single_link

SCHEMA = "hoh-capacity-admission/1"
DIRNAME = "capacity"
"""The directory of a project (`<project>/capacity/`) that holds its admission records."""

MAX_RECORDS = 4096
"""Records one directory may hold before the next is refused, named: they are files and inodes, and nothing here removes
one (move old ones aside under a versioned name outside the directory)."""

MAX_RECORD_BYTES = 2 * 1024 * 1024
"""The largest record that is written or read: 64 nodes with their decision lines and at most 256 provider groups fit
in a small part of it."""

PREFIX = "capacity-"
SUFFIX = ".json"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
KEYS = frozenset({"schema", "decision_id", "decided_at", "observed_at", "registry", "snapshot", "policy", "width", "ready",
                  "offered", "wants", "reserve", "holds", "narrowed", "facts", "plan", "outcome", "held", "digest"})
"""Exactly the keys of a record."""


class RecordError(ValueError):
    """A record that was not written, or a file that is not one. `reason` is `invalid` (a value, a name, a bound or the
    file itself breaks a rule) or `io` (the file system could not be used)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"capacity record: {message}")
        self.reason = reason


def canonical(body: object) -> str:
    """The canonical JSON of `body`: sorted keys, no spaces, ASCII."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot_digest(registry: str, taken_at: str, judged_at: str, facts: list) -> str:
    """The sha256 (hex) of what a capacity-source snapshot says at `judged_at`: the digest of the registry it holds, when it
    was taken and every fact judged then (`capacitysource.Fact.as_dict()`), in canonical JSON. A decision quotes it to
    say what it was made on; the same snapshot judged at another time has another digest (the ages differ). Pure."""
    return hashlib.sha256(canonical({"registry": registry, "taken_at": taken_at, "judged_at": judged_at,
                                     "facts": facts}).encode("ascii")).hexdigest()


def _rendered(body: dict) -> tuple[str, bytes]:
    """`(digest, file bytes)` of a record body: the digest is the sha256 of its canonical JSON, the file is the body and
    that digest, in the one rendering `read_record` accepts."""
    digest = hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
    return digest, (json.dumps({**body, "digest": digest}, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def _no_duplicates(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is repeated")
    return dict(pairs)


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def read_record(path: str | os.PathLike) -> dict:
    """The admission record at `path`, exactly as `write_record` wrote it (see the module text). `RecordError` otherwise;
    reads nothing else and writes nothing."""
    file = Path(os.fspath(path))
    try:
        info = os.lstat(file)                      # the size is judged before a byte is read
    except FileNotFoundError:
        raise RecordError("io", f"{file.name!r} does not exist") from None
    except (OSError, ValueError) as exc:
        raise RecordError("io", f"{file.name!r} cannot be examined: {getattr(exc, 'strerror', None) or type(exc).__name__}") from exc
    if stat.S_ISREG(info.st_mode) and info.st_size > MAX_RECORD_BYTES:
        raise RecordError("invalid", f"{file.name!r} is larger than {MAX_RECORD_BYTES} bytes")
    try:
        raw = read_single_link(file)
    except (OSError, ValueError) as exc:
        raise RecordError("io", f"{file.name!r} cannot be read: {getattr(exc, 'strerror', None) or type(exc).__name__}") from exc
    if raw is None:
        raise RecordError("io", f"{file.name!r} does not exist")
    if len(raw) > MAX_RECORD_BYTES:
        raise RecordError("invalid", f"{file.name!r} is larger than {MAX_RECORD_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecordError("invalid", f"{file.name!r} is not JSON") from exc
    if type(doc) is not dict or set(doc) != KEYS or doc["schema"] != SCHEMA:
        raise RecordError("invalid", f"{file.name!r} is not an admission record ({SCHEMA})")
    identity = doc["decision_id"]
    if type(identity) is not str or _ID.fullmatch(identity) is None or file.name != f"{PREFIX}{identity}{SUFFIX}":
        raise RecordError("invalid", f"{file.name!r} names another decision")
    body = {key: value for key, value in doc.items() if key != "digest"}
    try:
        digest, rendered = _rendered(body)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecordError("invalid", f"{file.name!r} cannot be rendered") from exc
    if doc["digest"] != digest or raw != rendered:
        raise RecordError("invalid", f"{file.name!r} does not match its own digest")
    return doc


def _records_in(directory: Path) -> int:
    """How many records the directory holds, counted up to `MAX_RECORDS`."""
    found = 0
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if entry.name.startswith(PREFIX) and entry.name.endswith(SUFFIX):
                    found += 1
                    if found >= MAX_RECORDS:
                        break
    except OSError as exc:
        raise RecordError("io", f"the records cannot be counted: {exc.strerror or type(exc).__name__}") from exc
    return found


def write_record(directory: str | os.PathLike, decision_id: str, body: dict) -> tuple[str, str]:
    """`(file name, digest)` of the record `body` written to `directory` and read back (see the module text). `body` is
    the record without its `digest`; it must carry this `decision_id` and the schema. `RecordError` for anything else."""
    if type(decision_id) is not str or _ID.fullmatch(decision_id) is None:
        raise RecordError("invalid", "a decision id is [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if type(body) is not dict or set(body) != KEYS - {"digest"} or body["decision_id"] != decision_id \
            or body["schema"] != SCHEMA:
        raise RecordError("invalid", "the body is not this decision's record without its digest")
    target = Path(os.fspath(directory))
    try:
        os.mkdir(target, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RecordError("io", f"the directory {target.name!r}: {exc.strerror or type(exc).__name__}") from exc
    try:
        info = os.lstat(target)
    except OSError as exc:
        raise RecordError("io", f"the directory {target.name!r}: {exc.strerror or type(exc).__name__}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RecordError("invalid", f"{target.name!r} is not a directory of its own")
    if _records_in(target) >= MAX_RECORDS:
        raise RecordError("invalid", f"{target.name!r} holds {MAX_RECORDS} records: move the old ones aside (a versioned name "
                                     f"outside the directory) before a wave is admitted again")
    try:
        digest, payload = _rendered(body)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecordError("invalid", f"the body cannot be rendered: {type(exc).__name__}") from exc
    if len(payload) > MAX_RECORD_BYTES:
        raise RecordError("invalid", f"the record would be larger than {MAX_RECORD_BYTES} bytes")
    name = f"{PREFIX}{decision_id}{SUFFIX}"
    try:
        durable_create(target / name, payload, mode=0o600)
    except FileExistsError as exc:
        raise RecordError("invalid", f"{name} exists: a record is never replaced") from exc
    except OSError as exc:
        raise RecordError("io", f"{name} could not be written: {getattr(exc, 'strerror', None) or type(exc).__name__}") from exc
    if read_record(target / name)["digest"] != digest:
        raise RecordError("io", f"{name} reads back as another record than the one written")
    return name, digest
