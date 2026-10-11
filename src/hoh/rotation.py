"""Context rotation (V3.3 P2-14): moving Layer 1 material to Layer 2.

A programme that runs for weeks accumulates more context than any brief can
carry. Hierarchical loading answers *what to include*; rotation answers *what
to do with what no longer fits*:

    Layer 0  compact low-resolution context        (hoh.taskstate)
    Layer 1  relevant decisions / evidence         <- rotated out of here
    Layer 2  full source available on demand       <- ... to here

The rule this module exists for:

    Rotation may reduce what is *carried*. It may never reduce what is
    *reachable*, and it may never happen silently.

So every rotation leaves a `RotationRecord` that is addressable (`record_id`),
digest-bound (`record_digest`, and each item's content `digest`) and complete
about what it moved (`item_ids`, `source_refs` and `digests` are refused unless
they have the same length). `reachable` turns "Layer 2 still holds it" from an
assertion into a check.

No model or provider call is made here. A rotated item carries its *reference*
(`source_ref`, `digest`) and a caller-supplied one-line `gist`; summarising a
rotated group into new prose would be a provider call, and this module invents
no prose. The item tiering above reads nothing: items are handed in, nothing
touches the run store, the filesystem, git or the network, and there is no wall
clock -- `now` is a parameter. Inputs are never mutated; equal inputs give equal
results.

The last section, "session rotation", is the one place this module is effectful,
and it is the *policy* of the same doctrine at the largest scale: when the
orchestrating session's own context no longer fits, the whole session is rotated
out and a fresh one carries only the capsule, every entry of which stays
reachable (re-derivable) and digest-bound. `RotationPolicy` says when, the
`RotationController` decides at the renewal tick of `hoh project hold` (and of
`rollover-accept`) and records every decision, and the move itself is the
existing Layer-A protocol (`hoh.rollover.run_rollover`), which the controller
calls and never re-implements. Its files, clock and the rollover itself arrive
through seams, so the controller is driven by fakes in tests.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from hoh.contracts import Strict
from hoh.durable_io import durable_append, durable_create
from hoh.rollover import (
    DEFAULT_THRESHOLD,
    Outcome,
    RolloverResult,
    Tick,
    TriggerError,
    TriggerResult,
    evaluate_trigger,
)
from hoh.succession_capsule import (
    CapsuleInput,
    Section,
    check_capsule_id,
)


class Tier(StrEnum):
    """Where a piece of context currently sits.

    PINNED is the escape hatch with teeth: the North Star digest, the work
    package under execution, an open blocker. `rotate` never moves a pinned
    item, and raises rather than drop one when the budget cannot hold them.
    """

    CARRIED = "CARRIED"
    ROTATED = "ROTATED"
    PINNED = "PINNED"


def content_digest(content: str | bytes) -> str:
    """sha256 hex of `content` -- the digest a caller stores in `ContextItem.digest`.

    `reachable` recomputes exactly this over what the resolver returns, so an
    item built with any other digest function will be reported as changed.
    """
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(data).hexdigest()


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; a naive time cannot be ordered safely")
    return value


class ContextItem(Strict):
    """One piece of context and the pointer that makes it reachable again.

    An empty `source_ref` or `digest` is refused at construction: an item that
    cannot be reached again after rotation is an item rotation would destroy.
    """

    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1)
    tier: Tier
    title: str
    #: Caller-supplied one-line gist. Never generated here.
    gist: str
    source_ref: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    cost: int = Field(ge=0)
    last_touched_at: datetime
    epoch: int

    @field_validator("last_touched_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "last_touched_at")


def record_digest(*, record_id: str, rotated_at: datetime, epoch: int,
                  item_ids: Iterable[str], source_refs: Iterable[str],
                  digests: Iterable[str], reason: str, freed: int) -> str:
    """sha256 over canonical JSON of everything a `RotationRecord` claims.

    Same idiom as `taskstate.state_digest`: sorted keys, compact separators,
    utf-8. Every field is covered -- editing the reason after the fact is as
    much a falsification as editing which items moved.
    """
    body = {
        "record_id": record_id,
        "rotated_at": rotated_at.isoformat(),
        "epoch": epoch,
        "item_ids": list(item_ids),
        "source_refs": list(source_refs),
        "digests": list(digests),
        "reason": reason,
        "freed": freed,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RotationRecord(Strict):
    """What one rotation did. Frozen and digest-bound.

    Refused at construction: `item_ids`, `source_refs` and `digests` of unequal
    length (a rotation naming three items and two sources has lost one), an
    empty rotation (no rotation is `None`, not an empty record), a blank
    reason (rotation never happens silently) and a `record_digest` that is not
    the digest of what the record shows.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    rotated_at: datetime
    epoch: int
    item_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    digests: tuple[str, ...]
    reason: str = Field(min_length=1)
    freed: int = Field(ge=0)
    record_digest: str

    @field_validator("rotated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "rotated_at")

    @model_validator(mode="after")
    def _complete_and_bound(self) -> RotationRecord:
        lengths = (len(self.item_ids), len(self.source_refs), len(self.digests))
        if len(set(lengths)) != 1:
            raise ValueError(
                f"item_ids, source_refs and digests must have the same length, got {lengths}; "
                "a record that names fewer sources than items has lost one")
        if not self.item_ids:
            raise ValueError("a rotation record must name at least one item; "
                             "nothing rotated is None, not an empty record")
        if any(not s.strip() for s in self.source_refs + self.digests):
            raise ValueError("every rotated item needs a non-empty source_ref and digest")
        if self.record_digest != self.compute_digest():
            raise ValueError("record_digest does not match the record's content")
        return self

    def compute_digest(self) -> str:
        return record_digest(
            record_id=self.record_id, rotated_at=self.rotated_at, epoch=self.epoch,
            item_ids=self.item_ids, source_refs=self.source_refs, digests=self.digests,
            reason=self.reason, freed=self.freed)


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #


def carried_cost(items: Iterable[ContextItem]) -> int:
    """Total cost of what a brief carries: CARRIED and PINNED, never ROTATED."""
    return sum(i.cost for i in items if i.tier is not Tier.ROTATED)


def rotation_pressure(items: Iterable[ContextItem], *, budget: int) -> float:
    """`carried_cost / budget`; above 1.0 the budget is already exceeded."""
    if budget <= 0:
        raise ValueError(f"budget must be at least 1, got {budget}")
    return carried_cost(items) / budget


# --------------------------------------------------------------------------- #
# rotate
# --------------------------------------------------------------------------- #


def _coldness(item: ContextItem) -> tuple[int, datetime, str]:
    # Rule 6 before rule 2: an older epoch goes first whatever its timestamp;
    # item_id makes ties deterministic regardless of input order.
    return (item.epoch, item.last_touched_at, item.item_id)


def rotate(
    items: Iterable[ContextItem],
    *,
    budget: int,
    now: datetime,
    keep_touched_within: timedelta | None = None,
    reason: str,
) -> tuple[list[ContextItem], RotationRecord | None]:
    """Rotate CARRIED items to ROTATED until the carried cost fits `budget`.

    Returns the new item list (input order, rotated items with tier ROTATED)
    and the record, or `(items, None)` when nothing needed to move.

    1. PINNED never rotates; pinned cost alone over `budget` raises.
    2. Coldest first: by `last_touched_at`, oldest first, then `item_id`.
    3. Every rotated item is in the record with its `source_ref` and `digest`.
    4. Idempotent: rotating the result again with the same budget returns None.
    5. `keep_touched_within` protects items touched at or after
       `now - keep_touched_within` -- unless the budget is otherwise
       unreachable; then the coldest protected items rotate and the record's
       reason says the protection was overridden.
    6. An older `epoch` rotates before any item of a newer one.
    7. `budget <= 0` raises.

    Duplicate item ids, a blank reason, a naive `now` and a negative
    `keep_touched_within` raise ValueError.
    """
    if budget <= 0:
        raise ValueError(f"budget must be at least 1, got {budget}")
    if not reason or not reason.strip():
        raise ValueError("reason must not be blank; rotation never happens silently")
    _require_aware(now, "now")
    if keep_touched_within is not None and keep_touched_within < timedelta(0):
        raise ValueError("keep_touched_within must not be negative")

    items = [i if isinstance(i, ContextItem) else ContextItem.model_validate(i) for i in items]
    duplicates = sorted(k for k, n in Counter(i.item_id for i in items).items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate item_id(s): {', '.join(duplicates)}")

    pinned = sum(i.cost for i in items if i.tier is Tier.PINNED)
    if pinned > budget:
        raise ValueError(
            f"pinned items alone cost {pinned}, over budget {budget}; "
            "a budget that cannot hold what must be held is a caller error")

    excess = carried_cost(items) - budget
    if excess <= 0:
        return items, None

    candidates = sorted((i for i in items if i.tier is Tier.CARRIED), key=_coldness)
    if keep_touched_within is None:
        free, protected = candidates, []
    else:
        since = now - keep_touched_within
        free = [i for i in candidates if i.last_touched_at < since]
        protected = [i for i in candidates if i.last_touched_at >= since]

    chosen: list[ContextItem] = []
    overridden: list[str] = []
    for group, is_protected in ((free, False), (protected, True)):
        for item in group:
            if excess <= 0:
                break
            chosen.append(item)
            excess -= item.cost
            if is_protected:
                overridden.append(item.item_id)

    text = reason.strip()
    if overridden:
        text += (f"; keep_touched_within={keep_touched_within} protection overridden for "
                 f"{', '.join(overridden)}: budget {budget} unreachable otherwise")

    moved = {i.item_id for i in chosen}
    out = [i.model_copy(update={"tier": Tier.ROTATED}) if i.item_id in moved else i
           for i in items]

    fields = dict(
        rotated_at=now,
        epoch=max(i.epoch for i in items),
        item_ids=tuple(i.item_id for i in chosen),
        source_refs=tuple(i.source_ref for i in chosen),
        digests=tuple(i.digest for i in chosen),
        reason=text,
        freed=sum(i.cost for i in chosen),
    )
    # Addressable by content: the same rotation of the same state has the same id.
    record_id = "rot-" + record_digest(record_id="", **fields)[:16]
    record = RotationRecord(record_id=record_id, **fields,
                            record_digest=record_digest(record_id=record_id, **fields))
    return out, record


# --------------------------------------------------------------------------- #
# reachable
# --------------------------------------------------------------------------- #


def reachable(record: RotationRecord, *,
              resolve: Callable[[str], str | bytes | None]) -> list[str]:
    """The refs of `record` that Layer 2 no longer holds; `[]` when it holds all.

    A ref is reported when `resolve` raises, returns None, or returns content
    whose `content_digest` differs from the recorded digest -- a stale pointer
    is not a working one. Order follows the record.
    """
    lost: list[str] = []
    for ref, want in zip(record.source_refs, record.digests):
        try:
            content = resolve(ref)
        except Exception:  # noqa: BLE001 -- any failure to resolve is unreachability
            lost.append(ref)
            continue
        if content is None or content_digest(content) != want:
            lost.append(ref)
    return lost


# --------------------------------------------------------------------------- #
# session rotation (Layer B): when the orchestrating session itself is replaced
# --------------------------------------------------------------------------- #

POLICY_SCHEMA = "veriharness.rotation-policy/1"

#: The capsule-input entry that carries the master's own handoff. The product cannot
#: derive a strand table; the master writes it when asked, and nothing rotates without it.
HANDOFF_ENTRY = "handoff"

ROTATION_DIRNAME = "rotation"
LOG_NAME = "policy.log.jsonl"

MAX_POLICY_BYTES = 64 * 1024
MAX_TEMPLATE_BYTES = 256 * 1024
MAX_HANDOFF_BYTES = 1024 * 1024
MAX_LOG_BYTES = 32 * 1024 * 1024

#: A handoff modified further in the future than this (seconds) is not a fresh one.
FUTURE_SLACK_S = 5.0

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class PolicyError(ValueError):
    """A rotation policy (or its capsule input) that cannot be used. Never a pass:
    the hold refuses before it takes a lease."""


class HandoffNotReady(Exception):
    """The master's handoff is not there (yet) in a form that may be bound."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Unusable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _plain_path(value: str, *, what: str, tilde: bool = False) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ValueError(f"{what} must be a printable path")
    if tilde and value.startswith("~/"):
        return value
    if not os.path.isabs(value):
        raise ValueError(f"{what} must be an absolute path, got {value!r}: the successor "
                         "starts in a directory of its own")
    return value


class TranscriptSpec(Strict):
    """Where the master's own transcript is: a file, or the session id the
    harness exports (found as `<projects_dir>/<anything>/<id>.jsonl`)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    path: str | None = None
    session_id_env: str | None = None
    projects_dir: str = "~/.claude/projects"

    @model_validator(mode="after")
    def _one_form(self) -> TranscriptSpec:
        if (self.path is None) == (self.session_id_env is None):
            raise ValueError("a transcript is named by exactly one of `path` and "
                             "`session_id_env`")
        if self.path is not None:
            _plain_path(self.path, what="transcript.path")
        if self.session_id_env is not None and not _ENV_NAME.match(self.session_id_env):
            raise ValueError("transcript.session_id_env must be an environment variable "
                             "name (upper case, digits, '_')")
        _plain_path(self.projects_dir, what="transcript.projects_dir", tilde=True)
        return self


class RotationPolicy(Strict):
    """When the orchestrating session is rotated out, and how the successor starts.

    Every number has a floor and a ceiling and every one is refused outside them;
    an integer is an integer (`true` is not 1), a float is finite. The policy is
    a document the operator writes once (`hoh project hold --rotate-policy FILE`);
    nothing in it is a value the product guesses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    schema_version: str
    successor_model: str
    #: JSON, `CapsuleInput`-shaped: what to derive, the operator's decisions, advisory
    #: context. It must contain the `handoff` entry (see `HANDOFF_ENTRY`).
    capsule_input: str
    transcript: TranscriptSpec
    threshold_tokens: int = Field(DEFAULT_THRESHOLD, strict=True, ge=10_000, le=10_000_000)
    successor_permission_mode: Literal["auto"] | None = "auto"
    successor_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    prompt_delay_s: float = Field(0.0, strict=True, ge=0, le=300, allow_inf_nan=False)
    #: From the request for a handoff to the start of the rotation, at most.
    request_ttl_s: int = Field(1800, strict=True, ge=10, le=86_400)
    #: After a rotation that did not happen, no new request for this long.
    cooldown_s: int = Field(1200, strict=True, ge=10, le=86_400)
    poll_s: float = Field(2.0, strict=True, ge=0.01, le=60, allow_inf_nan=False)
    ack_timeout_s: int = Field(600, strict=True, ge=30, le=3_600)
    takeover_s: int = Field(300, strict=True, ge=10, le=3_600)

    @field_validator("successor_model")
    @classmethod
    def _a_model_name(cls, value: str) -> str:
        if not _MODEL_NAME.fullmatch(value):
            raise ValueError(f"successor_model must be a model name (letters, digits, "
                             f"'.', '_', ':', '-'), got {value!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RotationPolicy:
        if self.schema_version != POLICY_SCHEMA:
            raise ValueError(f"schema_version must be {POLICY_SCHEMA!r}, "
                             f"got {self.schema_version!r}")
        _plain_path(self.capsule_input, what="capsule_input")
        if self.prompt_delay_s >= self.ack_timeout_s:
            raise ValueError("prompt_delay_s must be shorter than ack_timeout_s: the delay "
                             "comes out of the acknowledgement window")
        return self

    def successor_args(self) -> tuple[str, ...]:
        args: list[str] = []
        if self.successor_permission_mode:
            args += ["--permission-mode", self.successor_permission_mode]
        if self.successor_effort:
            args += ["--effort", self.successor_effort]
        return tuple(args)


@dataclass(frozen=True)
class LoadedPolicy:
    policy: RotationPolicy
    path: Path
    sha256: str


def _read_regular(path: str | os.PathLike[str], *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    """The bytes of a regular, non-empty, readable file of at most `max_bytes`; never a
    link, a FIFO, a directory or a device (a FIFO is not opened at all)."""
    p = os.fspath(path)
    if "\0" in p:
        raise _Unusable("has a NUL in its name")
    try:
        before = os.lstat(p)
    except FileNotFoundError:
        raise _Unusable("does not exist") from None
    except OSError as exc:
        raise _Unusable(f"cannot be examined: {exc.strerror or exc}") from None
    if stat.S_ISLNK(before.st_mode):
        raise _Unusable("is a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise _Unusable("is not a regular file")
    if before.st_size == 0:
        raise _Unusable("is empty")
    if before.st_size > max_bytes:
        raise _Unusable(f"is too large ({before.st_size} bytes, the limit is {max_bytes})")
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise _Unusable(f"cannot be opened: {exc.strerror or exc}") from None
    try:
        now = os.fstat(fd)
        if (not stat.S_ISREG(now.st_mode)
                or (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino)):
            raise _Unusable("changed while it was being read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _Unusable(f"is too large (more than {max_bytes} bytes)")
    except OSError as exc:
        raise _Unusable(f"cannot be read: {exc.strerror or exc}") from None
    finally:
        os.close(fd)
    return b"".join(chunks), now


def _text(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unusable("is not valid UTF-8") from None
    if "\0" in text:
        raise _Unusable("contains a NUL")
    return text


def _json_object(text: str, what: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = value
        return out

    def no_constants(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    try:
        data = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=no_constants)
    except ValueError as exc:
        raise PolicyError(f"{what} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise PolicyError(f"{what} must be a JSON object, got {type(data).__name__}")
    return data


def _read_json_file(path: str | os.PathLike[str], *, what: str, max_bytes: int) -> tuple[
        dict[str, Any], bytes]:
    try:
        data, _st = _read_regular(path, max_bytes=max_bytes)
        text = _text(data)
    except _Unusable as exc:
        raise PolicyError(f"{what} {os.fspath(path)!r} {exc.reason}") from None
    return _json_object(text, f"{what} {os.fspath(path)!r}"), data


def validate_capsule_input(data: Mapping[str, Any], policy: RotationPolicy) -> CapsuleInput:
    """The capsule input the policy rotates with, or `PolicyError`.

    The successor model is the policy's. What the default rollover input lacks and an
    unattended rotation cannot do without: the master's own narrative. So the input
    must name exactly one `handoff` entry, a file digest, bind a run set (the capsule's
    own structure rule), and every path it derives from must be absolute (a relative one would be re-derived in the successor's own
    directory and name another place).
    """
    body = dict(data)
    body["successor_model"] = policy.successor_model
    try:
        inp = CapsuleInput.model_validate(body)
    except ValidationError as exc:
        raise PolicyError(f"the capsule input is not usable: {exc}") from None
    handoffs = [d for d in inp.derive if d.entry_id == HANDOFF_ENTRY]
    if len(handoffs) != 1:
        raise PolicyError(f"the capsule input must name exactly one entry {HANDOFF_ENTRY!r} "
                          f"(the master's handoff), found {len(handoffs)}")
    [entry] = handoffs
    if entry.section is not Section.REGISTER or entry.deriver != "file_digest":
        raise PolicyError(f"the entry {HANDOFF_ENTRY!r} must be a REGISTER entry derived by "
                          "file_digest")
    if not any(d.section is Section.RUNS and d.deriver == "run_set" for d in inp.derive):
        raise PolicyError("the capsule input binds no run_set in RUNS: a capsule needs one "
                          "(an empty run set is a fact, an absent one is not), and every "
                          "rotation would otherwise be refused at the freeze")
    for d in inp.derive:
        key = {"file_digest": "path", "run_set": "runs_root", "ref_state": "repo"}.get(d.deriver)
        value = d.params.get(key) if key else None
        if key and (not isinstance(value, str) or not os.path.isabs(value)
                    or not value.isprintable()):
            raise PolicyError(f"the entry {d.entry_id!r} derives from {value!r}: the path "
                              "must be absolute")
    return inp


def read_capsule_input(policy: RotationPolicy) -> dict[str, Any]:
    data, _raw = _read_json_file(policy.capsule_input, what="the capsule input",
                                 max_bytes=MAX_TEMPLATE_BYTES)
    return data


def load_policy(path: str | os.PathLike[str], *,
                directory: str | os.PathLike[str] | None = None) -> LoadedPolicy:
    """Read a policy file and the capsule input it names, or raise `PolicyError`.

    Everything that is not a plain regular UTF-8 JSON object of the right shape is
    refused: a missing file, a link, a FIFO, a directory, mode 000, NUL, a duplicate
    key, `NaN`, a number that is out of range or of the wrong type. With `directory` (the
    project's), the handoff the input names must also be a place only the master writes
    (`check_handoff_location`); the controller checks it again at every request, with the
    transcript the environment names.
    """
    data, raw = _read_json_file(path, what="the rotation policy", max_bytes=MAX_POLICY_BYTES)
    try:
        policy = RotationPolicy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(f"the rotation policy is not usable: {exc}") from None
    inp = validate_capsule_input(read_capsule_input(policy), policy)
    loaded = LoadedPolicy(policy=policy, path=Path(os.path.abspath(os.fspath(path))),
                          sha256=hashlib.sha256(raw).hexdigest())
    if directory is not None:
        check_handoff_location(handoff_path(inp), directory=directory,
                               others=_not_the_handoff(loaded, directory,
                                                       policy.transcript.path))
    return loaded


def resolve_transcript(spec: TranscriptSpec, environ: Mapping[str, str] | None = None) -> Path:
    """The transcript file, or `TriggerError` (never a guess: not found, found twice, or
    not named by the environment all mean "no measurement")."""
    if spec.path is not None:
        # a FIFO would block the hold loop's renewal in `open`: only a regular file is read
        try:
            kind = os.stat(spec.path).st_mode
        except OSError as exc:
            raise TriggerError(f"cannot read the transcript {spec.path}: "
                               f"{exc.strerror or exc}") from exc
        if not stat.S_ISREG(kind):
            raise TriggerError(f"the transcript {spec.path} is not a regular file")
        return Path(spec.path)
    env = os.environ if environ is None else environ
    sid = env.get(spec.session_id_env or "", "")
    if not _SESSION_ID.match(sid):
        raise TriggerError(f"${spec.session_id_env} does not name a session id here "
                           f"({sid[:12]!r}), so the master's transcript cannot be found")
    base = Path(spec.projects_dir).expanduser()
    try:
        found = sorted(p for p in base.glob(f"*/{sid}.jsonl") if p.is_file())
    except OSError as exc:
        raise TriggerError(f"cannot search {base} for the transcript: {exc}") from exc
    if len(found) != 1:
        raise TriggerError(f"expected exactly one transcript {sid}.jsonl under {base}, "
                           f"found {len(found)}")
    return found[0]


def make_trigger(policy: RotationPolicy,
                 environ: Mapping[str, str] | None = None) -> Callable[[], TriggerResult]:
    """`evaluate_trigger` over the master's own transcript, at the policy's threshold."""
    def trigger() -> TriggerResult:
        return evaluate_trigger(resolve_transcript(policy.transcript, environ),
                                threshold=policy.threshold_tokens)
    return trigger


def _names_request(text: str, request_id: str) -> bool:
    """Does the first line of `text` name `request_id` as a whole word? (`rq-...-ab12cd34`
    is not named by a line that carries `rq-...-ab12cd34ef`.)"""
    first = text.split("\n", 1)[0]
    return bool(request_id) and re.search(rf"(?<![A-Za-z0-9_-]){re.escape(request_id)}(?![A-Za-z0-9_-])",
                     first) is not None


def read_fresh_handoff(path: str, *, not_before: float, now: float,
                       request_id: str) -> tuple[bytes, str, float]:
    """(bytes, sha256, mtime) of the master's handoff, or `HandoffNotReady`.

    Ready means: a regular, non-empty, UTF-8 file without NUL, at most
    `MAX_HANDOFF_BYTES`, modified at or after `not_before` (the request) and not in the
    future, **whose first line names `request_id`**. A handoff from before the request is
    the previous rotation's, not this one's; a file that merely was touched after the
    request (a log, a transcript, a note the master happens to write) is not an answer to
    it: the master answers a request by naming it.
    """
    try:
        data, st = _read_regular(path, max_bytes=MAX_HANDOFF_BYTES)
        text = _text(data)
    except _Unusable as exc:
        raise HandoffNotReady(f"the handoff {exc.reason}") from None
    if not text.strip():
        raise HandoffNotReady("the handoff is blank")
    if st.st_mtime < not_before:
        raise HandoffNotReady("the handoff was last written before the request")
    if st.st_mtime > now + FUTURE_SLACK_S:
        raise HandoffNotReady("the handoff is dated in the future")
    if not _names_request(text, request_id):
        raise HandoffNotReady(f"the first line of the handoff does not name the request "
                              f"{request_id}")
    return data, hashlib.sha256(data).hexdigest(), st.st_mtime


def handoff_path(inp: CapsuleInput) -> str:
    """The path of the `handoff` entry of a validated capsule input."""
    [entry] = [d for d in inp.derive if d.entry_id == HANDOFF_ENTRY]
    return str(entry.params["path"])


def _same_file(a: str, b: str | os.PathLike[str]) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def check_handoff_location(path: str, *, directory: str | os.PathLike[str],
                           others: Iterable[tuple[str, str | os.PathLike[str]]] = ()) -> None:
    """`PolicyError` unless `path` is a place only the master writes.

    The master's writing of the handoff is its consent to be rotated out, and the product
    infers that from a file that changed after the request. So the file must be one the
    product does not itself write: not inside the project's directory (its state, its
    decision log, its capsules, its handoff copies) and not any of `others` -- the master's
    own transcript (written on every turn), the policy file, the capsule input. Every
    spelling is compared resolved (`..`, a link to a directory, a hard link).
    """
    real = os.path.realpath(path)
    home = os.path.realpath(directory)
    if real == home or real.startswith(home + os.sep):
        raise PolicyError(f"the handoff path {path!r} is inside the project directory "
                          f"{directory}, where the product writes its own files: a file there "
                          "could answer a request without the master's consent")
    for what, other in others:
        if _same_file(real, other):
            raise PolicyError(f"the handoff path {path!r} is the {what} ({other}): a file "
                              "that is written for another reason cannot be the master's "
                              "answer to a request")


def _not_the_handoff(loaded: LoadedPolicy, directory: str | os.PathLike[str],
                     transcript: str | os.PathLike[str] | None
                     ) -> list[tuple[str, str | os.PathLike[str]]]:
    out: list[tuple[str, str | os.PathLike[str]]] = [
        ("rotation policy", loaded.path), ("capsule input", loaded.policy.capsule_input),
        ("decision log", Path(directory) / ROTATION_DIRNAME / LOG_NAME)]
    if transcript is not None:
        out.append(("master's transcript", transcript))
    return out


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime(_TIME_FORMAT)


def _epoch(iso: object) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(iso), _TIME_FORMAT)))
    except ValueError:
        return None


def _own_dir(path: Path) -> Path:
    """Create `path` if missing; it must be a real directory, not a link to one."""
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a directory of its own (a link or a file)")
    return path


#: How each outcome of `run_rollover` ends a request. `None` keeps it open (retried).
_KEEP_OPEN = frozenset({Outcome.DEFERRED, Outcome.IN_FLIGHT})


class RotationController:
    """Decides, at every tick, whether the orchestrating session is rotated out, and does
    it by calling the Layer-A protocol. One controller per hold loop.

    A tick is cheap while nothing is due: it reads the master's transcript, and logs
    nothing. When the context is at the threshold it makes **a request** -- one durable
    row (`rotation_requested`: the measured value, the threshold, the reason) and one line on
    stdout asking the master to write its handoff -- and then waits, at most
    `request_ttl_s`, for a handoff modified after the request. Writing it is the master's
    consent: it is at a point where it can stop. With it, and with the program's write
    lock free, the controller copies the handoff beside the log (the copy is what the
    capsule binds, so the master may keep writing), and calls `perform`. Every other
    path ends the request, on record, and starts a cool-down of `cooldown_s`: the
    master stays master. Nothing here raises into the hold loop.
    """

    def __init__(self, loaded: LoadedPolicy, *, directory: Path | str, project_id: str,
                 session: str, trigger: Callable[[], TriggerResult],
                 perform: Callable[[CapsuleInput, Callable[[], TriggerResult]], RolloverResult],
                 on_reclaimed: Callable[[], None] | None = None,
                 emit: Callable[[dict[str, Any]], None] | None = None,
                 wall: Callable[[], float] = time.time) -> None:
        self.loaded = loaded
        self.policy = loaded.policy
        self.directory = Path(directory)
        self.project_id = project_id
        self.session = session
        self.trigger = trigger
        self.perform = perform
        self.on_reclaimed = on_reclaimed
        self.emit = emit or (lambda row: None)
        self.wall = wall
        self.log_path = self.directory / ROTATION_DIRNAME / LOG_NAME
        self.cooldown_until = 0.0
        self._request: dict[str, Any] | None = None
        self._said: set[str] = set()

    # --- the record ----------------------------------------------------------------

    def _row(self, event: str, **fields: Any) -> dict[str, Any]:
        return {"at": _iso(self.wall()), "event": event, "project_id": self.project_id,
                "session": self.session, **fields}

    def _record(self, event: str, *, show: bool = False, **fields: Any) -> dict[str, Any]:
        """Append the row, durably, **before** anything is done about it; a row that cannot
        be written raises, and the tick ends in a fault (nothing rotates unrecorded)."""
        row = self._row(event, **fields)
        _own_dir(self.log_path.parent)
        durable_append(self.log_path, json.dumps(row, sort_keys=True), single_link=True,
                       create_mode=0o600)
        if show:
            self.emit(row)
        return row

    def _record_once(self, key: str, event: str, **fields: Any) -> None:
        if key not in self._said:
            self._said.add(key)
            self._record(event, show=True, **fields)

    def events(self) -> list[dict[str, Any]]:
        """The rows of the log; `[]` when it is missing or is not a plain regular file of
        a sane size (a FIFO or a link is never opened for reading: the hold must not hang)."""
        out: list[dict[str, Any]] = []
        try:
            data, _st = _read_regular(self.log_path, max_bytes=MAX_LOG_BYTES)
            raw = data.decode("utf-8")
        except (_Unusable, ValueError):
            return out
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "event" in row:
                out.append(row)
        return out

    def arm(self, *, fresh: bool = False) -> None:
        """Record that this process applies the policy, and restore the cool-down from the
        record: a rotation that ended less than `cooldown_s` ago (for whatever reason,
        completion included) is not followed by a request.

        `fresh` is the successor's side (`rollover-accept`): a session that has just taken
        over starts in a cool-down of its own. Its predecessor writes the closing row of
        the rotation only after the successor holds the lease, so the record alone cannot
        be relied on to say, in time, that a rotation has just happened."""
        if fresh:
            self.cooldown_until = self.wall() + self.policy.cooldown_s
        for row in self.events():
            if row.get("event") in ("rotation_finished", "rotation_request_expired",
                                    "rotation_error"):
                at = _epoch(row.get("at"))
                if at is not None:
                    self.cooldown_until = max(self.cooldown_until, at + self.policy.cooldown_s)
        try:
            self._record("rotation_armed", show=True, policy_sha256=self.loaded.sha256,
                         policy=str(self.loaded.path),
                         threshold_tokens=self.policy.threshold_tokens,
                         cooldown_until=_iso(self.cooldown_until) if self.cooldown_until else None)
        except Exception as exc:                                # noqa: BLE001 - reported
            self.emit(self._row("rotation_error", where="arm", detail=str(exc)[:300]))

    # --- the tick --------------------------------------------------------------------

    def tick(self) -> Tick:
        try:
            return self._tick()
        except Exception as exc:                                # noqa: BLE001 - fail closed
            return self._fault(exc)

    def _fault(self, exc: BaseException) -> Tick:
        now = self.wall()
        self._request = None
        self.cooldown_until = now + self.policy.cooldown_s
        detail = f"{type(exc).__name__}: {exc}"[:300]
        try:
            self._record("rotation_error", show=True, detail=detail)
        except Exception:                                       # noqa: BLE001 - best effort
            self.emit(self._row("rotation_error", detail=detail, unrecorded=True))
        return Tick()

    def _tick(self) -> Tick:
        now = self.wall()
        if now < self.cooldown_until:
            return Tick()
        if self._request is None:
            return self._maybe_request(now)
        return self._advance(now)

    def _maybe_request(self, now: float) -> Tick:
        try:
            fired = self.trigger()
        except TriggerError as exc:
            self._record_once(f"blind:{exc}", "rotation_blind", detail=str(exc)[:300])
            return Tick()
        self._said = {k for k in self._said if not k.startswith("blind:")}
        if not fired.estimate:
            self._said.discard("estimate")
        if not fired.fire:
            return Tick()
        if fired.estimate:
            self._record_once("estimate", "rotation_estimate_ignored", size_bytes=fired.size_bytes,
                              detail="the context could not be measured; a size estimate "
                                     "never starts a rotation")
            return Tick()
        template = validate_capsule_input(read_capsule_input(self.policy), self.policy)
        path = handoff_path(template)
        try:
            transcript: Path | None = resolve_transcript(self.policy.transcript)
        except TriggerError:
            transcript = None           # (it was just measured; whatever it is, the path check
            #                              below still has the log, the policy and the input)
        check_handoff_location(path, directory=self.directory,
                               others=_not_the_handoff(self.loaded, self.directory, transcript))
        request_id = f"rq-{_iso(now).replace('-', '').replace(':', '')}-{secrets.token_hex(4)}"
        check_capsule_id(request_id)
        deadline = now + self.policy.request_ttl_s
        row = self._record(
            "rotation_requested", show=True, request_id=request_id, reason=fired.reason,
            context_tokens=fired.context_tokens, threshold=fired.threshold,
            size_bytes=fired.size_bytes, handoff=path, deadline=_iso(deadline),
            instruction=(f"Write your handoff to {path} now (replace its content), and make "
                         f"its first line name this request ({request_id}): writing it is "
                         "your consent to be rotated out. From then on start no run, merge or "
                         "commit until a rotation_finished, rotation_request_expired, "
                         "rotation_withdrawn or rotation_error line follows. If you cannot "
                         "stop now, do not write it: the request expires and nothing rotates."))
        self._request = {"id": request_id, "started": now, "deadline": deadline,
                         "handoff": path, "waiting": None, "ready": None, "deferred": False,
                         "row": row}
        return Tick(next_in=self.policy.poll_s)

    def _advance(self, now: float) -> Tick:
        req = self._request
        assert req is not None
        if now >= req["deadline"]:
            return self._expire(req, now)
        if req["ready"] is None:
            try:
                data, digest, _mtime = read_fresh_handoff(
                    req["handoff"], not_before=req["started"], now=now, request_id=req["id"])
            except HandoffNotReady as exc:
                if exc.reason != req["waiting"]:
                    req["waiting"] = exc.reason
                    self._record("rotation_handoff_waiting", request_id=req["id"], reason=exc.reason)
                return Tick(next_in=self.policy.poll_s)
            handoffs = _own_dir(self.directory / ROTATION_DIRNAME / "handoffs")
            copy = handoffs / f"{req['id']}.handoff.md"
            durable_create(copy, data, mode=0o600)
            # the entry is found in the validated input (the model normalises names), never in
            # the raw text, so no spelling of "handoff" keeps the living file bound
            checked = validate_capsule_input(read_capsule_input(self.policy), self.policy)
            body = checked.model_dump(mode="json")
            body["derive"] = [({**d, "params": {"path": str(copy)}}
                               if d["entry_id"] == HANDOFF_ENTRY else d)
                              for d in body["derive"]]
            inp = validate_capsule_input(body, self.policy)
            req["ready"] = (inp, digest, copy)
            self._record("rotation_handoff_ready", request_id=req["id"], handoff=req["handoff"],
                         sha256=digest, copy=str(copy), bytes=len(data))
        inp, digest, copy = req["ready"]
        if not req.get("started_said"):
            req["started_said"] = True
            self._record("rotation_started", show=True, request_id=req["id"], handoff_sha256=digest,
                         copy=str(copy))
        result = self.perform(inp, self._decisive)
        return self._finish(req, result, now)

    def _decisive(self) -> TriggerResult:
        """The trigger the protocol re-checks once the master has consented. A size estimate
        (no usable request in the transcript) reads "do not rotate" here whatever the
        transcript's size says: the controller never starts on a guess, and neither does
        the protocol it calls."""
        fired = self.trigger()
        return replace(fired, fire=False) if fired.estimate else fired

    def _expire(self, req: dict[str, Any], now: float) -> Tick:
        why = req["waiting"] or ("the program's write lock stayed held" if req["deferred"]
                                 else "no handoff arrived")
        self._request = None
        self.cooldown_until = now + self.policy.cooldown_s
        self._record("rotation_request_expired", show=True, request_id=req["id"], reason=why,
                     cooldown_until=_iso(self.cooldown_until))
        return Tick()

    def _finish(self, req: dict[str, Any], result: RolloverResult, now: float) -> Tick:
        stop = result.outcome is Outcome.COMPLETE
        outcome = result.outcome
        try:
            if outcome in _KEEP_OPEN:
                if not req["deferred"]:
                    req["deferred"] = True
                    self._record("rotation_deferred", request_id=req["id"], outcome=outcome.value,
                                 detail=result.detail[:300])
                return Tick(next_in=self.policy.poll_s)
            self._request = None
            if outcome is Outcome.NOT_TRIGGERED:
                self._record("rotation_withdrawn", show=True, request_id=req["id"], detail=result.detail)
                return Tick()
            self.cooldown_until = self.wall() + self.policy.cooldown_s
            if outcome is Outcome.RECLAIMED and self.on_reclaimed is not None:
                self.on_reclaimed()
            self._record("rotation_finished", show=True, request_id=req["id"], outcome=outcome.value,
                         capsule_id=result.capsule_id, detail=result.detail[:300],
                         predecessor_is_master=result.predecessor_is_master,
                         successor_epoch=result.successor_epoch,
                         context_tokens=req["row"].get("context_tokens"),
                         threshold=req["row"].get("threshold"))
        except Exception as exc:                                # noqa: BLE001 - fail closed
            fault = self._fault(exc)
            return Tick(stop=True) if stop else fault
        return Tick(stop=True) if stop else Tick()
