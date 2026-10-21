"""The parallelism modes on the wave's admission path (V3.3 P3-25 and P3-26, the wiring slice).

`scheduler.profile_width` (P3-25 slice 1) says how many of the ready candidates a profile of the ladder LINEAR,
CONSERVATIVE, MODERATE, HIGH, MAX_SAFE offers to admission, and `scheduler.adapt_profile` (P3-26 slice 1) moves
along that ladder on the signals of P3-07. Both are pure and nothing called them. This module is their caller on the
production path: `ProjectController._backpressure_admission` hands it the nodes a wave would prepare (after the
capacity gate) and it answers which of them may be prepared, in the caller's order -- a prefix, never empty.

**The declaration.** `<project>/parallelism.json` (schema `hoh-parallelism/1`) is the operator's: `mode` (one of the
five profiles or `AUTO_ADAPTIVE`), `slots` (1 to 64, the authorized safe concurrent slots), `reserve` (0 to 64, slots
held back for QA, integration and recovery), `ceiling` (1 to 64, the most active work packages) and, for
`AUTO_ADAPTIVE` only, `top` (the widest profile the adaptation may reach) and `up_after` (1 to 16, the stable waves
in a row for one step up). Nothing has a default. The file is strict and bounded: a missing key, an extra key, a
duplicate key, a wrong type (a bool is no number), a value out of range, a spelling that is not exactly one of the
words, a file above `MAX_BYTES`, a link, a FIFO, a directory or a second name is `ParallelismError`, never a guess.
**No file is no declaration**: the project runs exactly as before and says nothing (the opt-in is the file, as the
capacity registry's is).

**The wave a declaration makes.** With a declaration the controller's width (`HOH_WAVE_WIDTH`, `width=` and the
launcher's `wave_width` still win when given; they are then the outer bound) is the widest wave the declaration can
offer: the width of the mode, or of `top` under `AUTO_ADAPTIVE`, at most `wave.MAX_WAVE_WIDTH`. Each wave round then
offers at most `profile_width(level, Lanes(slots, reserve, ceiling, active), ready)` of the candidates, `active`
being the project's nodes that are RUNNING. A fixed mode's level is the mode. The offer goes to
the gate the caller passes (`backpressure.admit_wave`, P3-07: this module does not import it), which can only narrow
it further and writes its decision record as before.

**AUTO_ADAPTIVE.** The level in force and its stable count are the state `<project>/parallelism-state.json`
(schema `hoh-parallelism-state/1`: `level`, `stable`, `last_change`, the `decision_id` of the round that wrote it,
`at`), replaced atomically by this module after each wave's decision and before any node is prepared. The
first wave starts at `scheduler.adaptive_start(bounds)`. The reason of the backpressure decision of the round is the
signal for `adapt_profile`; a decision that could not be taken is a signal that cannot be read, one step down. The
wave is `wave_width(profile_width(adapted level), decision.width)`: never wider than the profile in force when the
round began and never wider than the decision just made, so a step down narrows at once and a step up widens the
next wave, not this one.

**Fail closed, and never an empty round.** Whatever cannot be known -- a declaration that was readable when the
controller was built and is not now, a state file that is unreadable, a width that cannot be computed, a state that
cannot be written -- is the floor: the first node alone, a `PARALLELISM_UNJUDGED` step with the reason, and the
nodes past it wait READY, unprepared, offered again next round (the rounds are `max_rounds`-bounded and each one
runs at least its head, as for P3-07). An unreadable state file is left exactly as found (the operator's evidence,
not rewritten); the floor repeats, named, until it is fixed. A project that is closed runs no wave round, so a later
drive writes nothing here. **The liveness floor is not the ceiling**: when the profile has no free lane (`ceiling` or
`slots - reserve` reached, or nodes already RUNNING) the scheduler's width is 0 and the round still offers the head, so
the one node the loop must run may exceed the declared `ceiling` by one; that is the price of a round that always
advances. `active` is the project's RUNNING nodes in the persisted state; the controller settles RUNNING nodes before it
plans a wave, so on the production path it is normally 0 and a lane is taken only by a state a person or another process
left behind. The state file is a shared, authoritative record: its write goes through `fenced` (the controller's program
lock and master-epoch check), so a superseded master writes nothing.

Not here: the command that writes the declaration, deriving `slots` from the capacity registry and the machine,
dissolving the reserve when no QA work is waiting, a persisted reason for the nodes a mode cuts, and any use of
the modes outside the wave round.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import durable_io
from .contracts import utcnow
from .scheduler import (
    MAX_LANES, MAX_UP_AFTER, PROFILES, Adaptive, Bounds, Lanes, SchedulerError, adapt_profile,
    adaptive_start, profile_width, wave_width,
)
from .store import StaleWrite, StoreError
from .wave import MAX_WAVE_WIDTH

if TYPE_CHECKING:  # pragma: no cover - typing only; this module never imports the gate it is handed
    from .backpressure import WaveGate

FILE = "parallelism.json"
STATE_FILE = "parallelism-state.json"
SCHEMA = "hoh-parallelism/1"
STATE_SCHEMA = "hoh-parallelism-state/1"
ADAPTIVE = "AUTO_ADAPTIVE"
MODES = PROFILES + (ADAPTIVE,)
"""The five profiles of the ladder, narrowest first, and the rule that moves along it."""

MAX_BYTES = 4096
"""A declaration or a state file is a dozen short fields; a file above this is not ours."""

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_KEYS = ("schema", "mode", "slots", "reserve", "ceiling")
_KEYS_ADAPTIVE = _KEYS + ("top", "up_after")
_STATE_KEYS = ("schema", "level", "stable", "last_change", "decision_id", "at")
_CHANGE_KEYS = ("at", "from", "to", "reason")


class ParallelismError(RuntimeError):
    """A declaration or a state file that is not acceptable. Never a width."""


def _whole(value: object, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ParallelismError(f"{what} is a whole number from {low} to {high}")
    return value


def _word(value: object, allowed: tuple[str, ...], what: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ParallelismError(f"{what} is exactly one of {', '.join(allowed)}")
    return value


@dataclass(frozen=True)
class Declaration:
    """What the project declares (see the module text). `top` and `up_after` are None unless the mode is
    `AUTO_ADAPTIVE`."""

    mode: str
    slots: int
    reserve: int
    ceiling: int
    top: str | None = None
    up_after: int | None = None

    @property
    def bounds(self) -> Bounds | None:
        return Bounds(self.top, self.up_after) if self.mode == ADAPTIVE else None

    def lanes(self, active: int) -> Lanes:
        return Lanes(self.slots, self.reserve, self.ceiling, min(max(active, 0), MAX_LANES))


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not a number")


def _pairs(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ParallelismError("a key is given twice")
    return dict(pairs)


def _object(path: Path, what: str) -> dict | None:
    """The JSON object of one of the project's own record files, None when the file is not there. Anything else
    that is not a small regular file with one name holding one UTF-8 JSON object is `ParallelismError`."""
    try:
        size = os.lstat(path).st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ParallelismError(f"{what} cannot be examined: {type(exc).__name__}") from exc
    if size > MAX_BYTES:
        raise ParallelismError(f"{what} is larger than {MAX_BYTES} bytes")
    try:
        raw = durable_io.read_single_link(path)
        if raw is None:
            return None
        if len(raw) > MAX_BYTES:
            raise ParallelismError(f"{what} is larger than {MAX_BYTES} bytes")
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_no_constant)
    except ParallelismError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ParallelismError(f"{what} cannot be read: {type(exc).__name__}") from exc
    if type(doc) is not dict:
        raise ParallelismError(f"{what} is not one JSON object")
    return doc


def parse(doc: object) -> Declaration:
    """The `Declaration` of a decoded document, or `ParallelismError`."""
    if type(doc) is not dict:
        raise ParallelismError("a declaration is one JSON object")
    mode = _word(doc.get("mode"), MODES, "mode")
    keys = _KEYS_ADAPTIVE if mode == ADAPTIVE else _KEYS
    if set(doc) != set(keys):
        raise ParallelismError("the declaration has exactly the keys " + ", ".join(keys))
    if doc["schema"] != SCHEMA:
        raise ParallelismError(f"schema is {SCHEMA}")
    slots = _whole(doc["slots"], 1, MAX_LANES, "slots")
    reserve = _whole(doc["reserve"], 0, MAX_LANES, "reserve")
    ceiling = _whole(doc["ceiling"], 1, MAX_LANES, "ceiling")
    if mode != ADAPTIVE:
        return Declaration(mode, slots, reserve, ceiling)
    return Declaration(mode, slots, reserve, ceiling, _word(doc["top"], PROFILES, "top"),
                       _whole(doc["up_after"], 1, MAX_UP_AFTER, "up_after"))


def read_declaration(project_dir: Path | str) -> Declaration | None:
    """The project's declaration; None only when `parallelism.json` does not exist (ENOENT -- every other way
    the name can fail is a refusal). Raises `ParallelismError`."""
    doc = _object(Path(project_dir) / FILE, FILE)
    return None if doc is None else parse(doc)


def outer_width(project_dir: Path | str) -> int | None:
    """The controller's width a declaration makes: the widest wave it can offer (the width of the mode, or of
    `top`, with nothing active and every candidate ready), 1 to `wave.MAX_WAVE_WIDTH`; None without a
    declaration. Raises `ParallelismError`."""
    declared = read_declaration(project_dir)
    if declared is None:
        return None
    widest = declared.top if declared.mode == ADAPTIVE else declared.mode
    try:
        width = profile_width(widest, declared.lanes(0), MAX_LANES).width
    except SchedulerError as exc:
        raise ParallelismError(f"the declaration gives no width: {exc}") from exc
    return min(max(width, 1), MAX_WAVE_WIDTH)


# --------------------------------------------------------------------------- #
# The adaptation's state
# --------------------------------------------------------------------------- #

def _change(doc: object) -> dict | None:
    if doc is None:
        return None
    if type(doc) is not dict or set(doc) != set(_CHANGE_KEYS):
        raise ParallelismError("last_change is null or exactly " + ", ".join(_CHANGE_KEYS))
    if type(doc["at"]) is not str or _TIME.fullmatch(doc["at"]) is None:
        raise ParallelismError("last_change.at is a UTC time YYYY-MM-DDTHH:MM:SSZ")
    _word(doc["from"], PROFILES, "last_change.from")
    _word(doc["to"], PROFILES, "last_change.to")
    if type(doc["reason"]) is not str or re.fullmatch(r"[a-z_]{1,16}", doc["reason"]) is None:
        raise ParallelismError("last_change.reason is a short lower-case word")
    return dict(doc)


def read_state(project_dir: Path | str, bounds: Bounds) -> Adaptive | None:
    """The adaptation's state kept for the project; None only when `parallelism-state.json` does not exist.
    A file that is not exactly what `write_state` produces is `ParallelismError`. The `stable` count the file
    holds may be anything `write_state` could have written under an earlier declaration (0 to `MAX_UP_AFTER`); it is
    **clamped to the declaration's `bounds.up_after`** when read, so lowering `up_after` never strands the project at
    the floor."""
    doc = _object(Path(project_dir) / STATE_FILE, STATE_FILE)
    if doc is None:
        return None
    if set(doc) != set(_STATE_KEYS) or doc["schema"] != STATE_SCHEMA:
        raise ParallelismError(f"{STATE_FILE} is {STATE_SCHEMA} with exactly the keys " + ", ".join(_STATE_KEYS))
    level = _word(doc["level"], PROFILES, "level")
    stable = _whole(doc["stable"], 0, MAX_UP_AFTER, "stable")
    _change(doc["last_change"])
    if type(doc["decision_id"]) is not str or _ID.fullmatch(doc["decision_id"]) is None:
        raise ParallelismError("decision_id is not an id")
    if type(doc["at"]) is not str or _TIME.fullmatch(doc["at"]) is None:
        raise ParallelismError("at is a UTC time YYYY-MM-DDTHH:MM:SSZ")
    return Adaptive(level, min(stable, bounds.up_after))


def write_state(project_dir: Path | str, before: Adaptive, after, decision_id: str) -> None:
    """Replaces the state with what `after` (an `Adaptation`) says, atomically. `last_change` is the step this
    reading made when it changed the level, else what the file said before. Raises `OSError` or
    `ParallelismError` (the old file is then untouched)."""
    path = Path(project_dir) / STATE_FILE
    previous = _object(path, STATE_FILE)
    kept = None if previous is None else _change(previous.get("last_change"))
    stamp = utcnow()
    change = ({"at": stamp, "from": before.level, "to": after.level, "reason": after.reason}
              if after.changed else kept)
    body = {"schema": STATE_SCHEMA, "level": after.level, "stable": after.stable, "last_change": change,
            "decision_id": decision_id, "at": stamp}
    durable_io.durable_write(path, json.dumps(body, indent=2, sort_keys=True) + "\n", mode=0o600)


# --------------------------------------------------------------------------- #
# The wave's admission
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ModeGate:
    """What the admission answered. `admitted` are the node ids that may be prepared (a prefix of the ids
    offered, never empty when any was offered). `gate` is the backpressure answer for the nodes the mode offered
    (None when the mode could not offer: the floor). `notes` are the steps to say, `(kind, node id or None, text)`:
    `PARALLELISM` (the mode and the width it gave), `PARALLELISM_WAIT` (a node the mode cut) and
    `PARALLELISM_UNJUDGED` (the floor, with its reason). `declared` is False when the project has no
    declaration (the answer is then the backpressure gate's, unchanged)."""

    admitted: tuple[str, ...]
    gate: "WaveGate | None"
    notes: tuple[tuple[str, str | None, str], ...]
    declared: bool


def _floor(ids: tuple[str, ...], why: str) -> ModeGate:
    notes = [("PARALLELISM_UNJUDGED", None, f"the parallelism mode cannot be judged, width 1: {why}"[:400])]
    notes += [("PARALLELISM_WAIT", node, f"PARALLELISM/unjudged: {why}"[:300]) for node in ids[1:]]
    return ModeGate(ids[:1], None, tuple(notes), True)


def admit_wave(node_ids, *, project_dir: Path | str, gate: Callable[[tuple[str, ...], str], object],
               fenced: Callable[[Callable[[], None]], None], decision_id: str, active: int) -> ModeGate:
    """Which of `node_ids` (the wave's candidates, in priority order) may be prepared under the project's
    declared mode? See the module text. `active` is the number of the project's nodes that are RUNNING. `gate` is
    called as `gate(offered_ids, decision_id)` and answers like `backpressure.admit_wave` (a `WaveGate`: `admitted`,
    `waits`, `why`, `judged`, `decision`); the controller passes that function, bound to its launcher and its
    record directory. `fenced` is called as `fenced(write)` with the closure that replaces the state file and must
    run it exactly once, under whatever makes the write authoritative (the controller: the program lock and the
    master's epoch check); a `StaleWrite` it raises (a superseded master) is **not** caught here.

    **Never raises (but for a `StaleWrite` of `fenced`) and never admits nobody of a list it was given.** Without a declaration it is the gate over
    every candidate, as before. `decision_id` names the backpressure record
    (`<project>/backpressure/backpressure-<id>.json`) and the state this call may write."""
    ids = tuple(node_ids)
    directory = Path(project_dir)

    def gate_over(offered: tuple[str, ...]):
        return gate(offered, decision_id)

    try:
        declared = read_declaration(directory)
    except ParallelismError as exc:
        return _floor(ids, f"{FILE}: {exc}") if ids else ModeGate((), None, (), True)
    if declared is None:
        answer = gate_over(ids)
        return ModeGate(answer.admitted, answer, (), False)
    if not ids or any(type(node) is not str for node in ids):
        answer = gate_over(ids)
        return ModeGate(answer.admitted, answer, (), True)
    try:
        held, bounds = None, declared.bounds
        if bounds is not None:
            try:
                held = read_state(directory, bounds) or adaptive_start(bounds)
            except ParallelismError as exc:
                return _floor(ids, f"{STATE_FILE}: {exc}; left as found")
        level = held.level if held is not None else declared.mode
        lanes = declared.lanes(active)
        width = profile_width(level, lanes, len(ids))
        offered = ids[:max(width.width, 1)]
        answer = gate_over(offered)
        final, adapted = len(answer.admitted), None
        if held is not None:
            signal = answer.decision.reason.value if answer.judged and answer.decision is not None else None
            adapted = adapt_profile(held, signal, bounds)
            allowed = wave_width(profile_width(adapted.level, lanes, len(ids)), answer.decision.width) \
                if signal is not None else 1
            final = min(final, max(allowed, 1))
            try:
                fenced(lambda: write_state(directory, held, adapted, decision_id))
            except StaleWrite:
                raise
            except (OSError, ParallelismError, StoreError) as exc:
                return _floor(ids, f"{STATE_FILE} cannot be written ({type(exc).__name__}); the adaptation "
                                   f"is not applied")
    except StaleWrite:                                         # a superseded master writes nothing and says so
        raise
    except Exception as exc:                                   # noqa: BLE001 - fail closed: the floor
        return _floor(ids, f"{type(exc).__name__}: {str(exc)[:200]}")
    admitted = answer.admitted[:final]
    mode_cut = tuple(ids[len(offered):]) + tuple(answer.admitted[final:])
    named = declared.mode if adapted is None else f"{declared.mode} {level}"
    detail = (f"mode {named}: profile width {width.width} of {len(ids)} ready (slots {declared.slots}, "
              f"reserve {declared.reserve}, ceiling {declared.ceiling}, active {lanes.active})")
    if adapted is not None:
        detail += (f"; {level} -> {adapted.level} ({adapted.reason}, stable {adapted.stable} of "
                   f"{declared.up_after}); wave {len(admitted)}")
    notes = [("PARALLELISM", None, detail[:400])]
    why = (f"PARALLELISM/{named}: width {len(admitted)} of {len(ids)} ready")[:300]
    notes += [("PARALLELISM_WAIT", node, why) for node in mode_cut]
    return ModeGate(admitted, answer, tuple(notes), True)


__all__ = ["ADAPTIVE", "FILE", "MAX_BYTES", "MODES", "SCHEMA", "STATE_FILE", "STATE_SCHEMA", "Declaration",
           "ModeGate", "ParallelismError", "admit_wave", "outer_width", "parse", "read_declaration",
           "read_state", "write_state"]
