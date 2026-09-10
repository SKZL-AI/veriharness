"""The goalbook: objectives that outlive a single run -- with a gate in front.

Today a `run` is the largest unit: one specification, one iteration budget,
done. Longer autonomous work needs the layer above it. Without that layer, one
level up, exactly what HoH prevents *inside* a run happens *between* runs:
run 7 does not know what run 3 established, and plans it again.

## Why a gate

The open design question was **who** writes the goalbook. A planner at the
objective level is the consistent continuation of the HoH idea -- and at the
same time the point at which a model starts handing itself assignments. The
captain's decision on this reads: **the AI may extend the frame, the human
sets it.**

Implemented as two stages. A new objective is born `PROPOSED` and does
nothing. Only an explicit approval turns it into `OPEN`. And when a proposal
widens the frame substantially, it is not the individual item that gets
confirmed but **the frame as a whole** -- otherwise a large expansion walks
through in many small steps.

## What the thresholds mean

They are not a security boundary but an attention boundary: they decide when
the captain has to *look*. Too low, and the gate becomes a click-through
nobody reads; too high, and it never engages. The values follow
`MAX_PRIORITIES = 3` from the planner contract -- more than three objectives
at once is already too many there.

As everywhere here: **nothing is deleted.** A discarded objective becomes
`DROPPED` with a reason and stays on the record.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Strict, utcnow

#: More than this many new objectives in **one** proposal counts as widening
#: the frame rather than continuing it. Mirrors MAX_PRIORITIES.
MAX_NEW_PER_PROPOSAL = 3

#: From this many simultaneously open objectives on, the frame is exhausted.
#: Whoever wants more should first finish or discard something.
MAX_OPEN = 10

#: A single objective whose description grows this long usually describes
#: several. Length is a crude but honest indicator of scope creep.
MAX_CHARS_PER_OBJECTIVE = 2000

#: Written into every file this module produces. The English field layout got
#: its own identifier because the German one had already been shipped under
#: `hoh-goalbook.v1` -- two incompatible layouts behind one identifier is not a
#: version, it is a guess. A parked file from before the rename still reads,
#: through `_LEGACY_FIELDS` below.
SCHEMA = "hoh-goalbook.v2"

#: Field names as they were written before the source was translated. Kept as
#: data rather than as a migration script: a goalbook is small, the mapping is
#: total, and an old parked state has to stay readable for the run record to
#: mean anything.
_LEGACY_FIELDS = {
    "ziel_id": "objective_id",
    "titel": "title",
    "beschreibung": "description",
    "vorgeschlagen_von": "proposed_by",
    "entschieden_am": "decided_at",
    "entschieden_von": "decided_by",
    "grund": "reason",
}


#: Every identifier this module can read. Anything else is a file from a
#: future version, and guessing at it is the failure this project exists to
#: prevent.
KNOWN_SCHEMAS = frozenset({"hoh-goalbook.v1", SCHEMA})


def _assert_known_schema(found: str | None, path: Path) -> None:
    """Fails loud on a goalbook this version cannot claim to understand.

    An adversarial reviewer pointed out at the step 0 gate that the `v2`
    identifier was pure decoration: `read()` never looked at it, `_migrate`
    ran unconditionally, and so the identifier decided nothing at all -- while
    the comment above it claimed that two layouts behind one identifier "is
    not a version, it is a guess". Now it decides something: a known
    identifier is read, an unknown one is refused by name instead of being
    silently reinterpreted.
    """
    if found is not None and found not in KNOWN_SCHEMAS:
        raise ValueError(
            f"{path} carries schema {found!r}, which this version does not "
            f"know (known: {', '.join(sorted(KNOWN_SCHEMAS))}). Refusing to "
            "guess at the layout."
        )


def _migrate(raw: dict) -> dict:
    """Maps legacy field names onto the current ones.

    `Objective` forbids unknown fields on purpose, so an unmigrated legacy
    entry would raise a ValidationError with nine complaints and no hint about
    the cause. Doing the mapping here keeps that failure from ever happening
    and keeps the reason for it in one place.
    """
    return {_LEGACY_FIELDS.get(k, k): v for k, v in raw.items()}


Status = Literal["PROPOSED", "OPEN", "ACTIVE", "DONE", "DROPPED"]


class Objective(Strict):
    objective_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: Status = "PROPOSED"
    spec_path: str | None = None
    runs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    proposed_by: str = "unknown"
    created_at: str = Field(default_factory=utcnow)
    decided_at: str | None = None
    decided_by: str | None = None
    reason: str | None = None


class FrameCheck(Strict):
    """The result of the gate check -- deliberately a type of its own.

    A bare True/False would conceal *why* the gate engages, and that is
    precisely the information the captain needs in order to decide in one
    sentence.
    """

    required: bool
    reasons: list[str] = Field(default_factory=list)
    new_count: int = 0
    open_afterwards: int = 0

    def report(self) -> str:
        if not self.required:
            return "No frame gate needed -- the proposal stays inside the existing frame."
        return (
            "FRAME GATE: this proposal widens the frame and needs your "
            "confirmation.\n  - " + "\n  - ".join(self.reasons)
        )


class Goalbook:
    """Persistence and rules. Lives next to the runs, not inside one."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "goalbook.json"

    # -- Reading and writing -------------------------------------------------- #

    def read(self) -> list[Objective]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        _assert_known_schema(raw.get("schema"), self.path)
        return [
            Objective.model_validate(_migrate(o)) for o in raw.get("objectives", [])
        ]

    def _park(self) -> Path:
        """Parks the current file under a version stamp, never overwriting.

        The first version of this used `if not parked.exists(): write` with a
        stamp of one-second resolution. Four writes inside the same second
        therefore produced **one** parked file, and the states in between were
        gone without a trace -- in a module whose own comment promised that
        nothing is overwritten. `RunStore._park` had already been through this
        exact defect and solved it: the timestamp carries the order, and on a
        name collision the version escalates instead of skipping.

        Exclusive creation ("xb") is what makes the promise hold rather than
        merely state it: a file that is already there cannot be touched, not
        even by a racing second process.
        """
        stamp = utcnow().replace(":", "-")
        for n in range(1000):
            suffix = f"-{n:03d}" if n else ""
            parked = self.path.with_name(f"goalbook.json.v{stamp}{suffix}")
            try:
                with open(parked, "xb") as fh:
                    fh.write(self.path.read_bytes())
                return parked
            except FileExistsError:
                continue
        raise RuntimeError(
            f"no free version name for {self.path} at {stamp} -- 1000 taken"
        )

    def write(self, objectives: list[Objective]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._park()
        tmp = self.path.with_name(f".goalbook.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {"schema": SCHEMA,
                 "objectives": [o.model_dump() for o in objectives]},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # -- The gate ------------------------------------------------------------- #

    def check_frame(self, new: list[Objective]) -> FrameCheck:
        """Does this proposal need a frame confirmation?"""
        existing = self.read()
        open_now = [o for o in existing if o.status in ("OPEN", "ACTIVE")]
        reasons: list[str] = []

        if not existing:
            reasons.append(
                "The goalbook is empty -- this proposal is what sets the frame "
                "in the first place. The first frame is always confirmed."
            )
        if len(new) > MAX_NEW_PER_PROPOSAL:
            reasons.append(
                f"{len(new)} new objectives at once (limit {MAX_NEW_PER_PROPOSAL}). "
                "More than three priorities at the same time is too many in the "
                "planner contract as well."
            )
        if len(open_now) + len(new) > MAX_OPEN:
            reasons.append(
                f"{len(open_now) + len(new)} open objectives afterwards "
                f"(limit {MAX_OPEN}) -- finish or discard something first."
            )
        for o in new:
            if len(o.description) > MAX_CHARS_PER_OBJECTIVE:
                reasons.append(
                    f"'{o.objective_id}' is very large at {len(o.description)} "
                    f"characters (limit {MAX_CHARS_PER_OBJECTIVE}) -- that "
                    "probably describes several objectives."
                )

        return FrameCheck(
            required=bool(reasons), reasons=reasons,
            new_count=len(new), open_afterwards=len(open_now) + len(new),
        )

    # -- Transitions ---------------------------------------------------------- #

    def propose(self, new: list[Objective]) -> FrameCheck:
        """Files objectives as PROPOSED. They do nothing until approved."""
        existing = self.read()
        known = {o.objective_id for o in existing}
        duplicates = [o.objective_id for o in new if o.objective_id in known]
        if duplicates:
            raise ValueError(f"objective IDs already exist: {', '.join(duplicates)}")
        check = self.check_frame(new)
        self.write(existing + [o.model_copy(update={"status": "PROPOSED"}) for o in new])
        return check

    def decide(
        self, objective_ids: list[str], *, status: Status, by: str, reason: str
    ) -> list[Objective]:
        """Approve, drop or complete -- always with a person and a reason."""
        if status not in ("OPEN", "DROPPED", "ACTIVE", "DONE"):
            raise ValueError(f"invalid objective status: {status}")
        existing = self.read()
        pending = set(objective_ids)
        changed: list[Objective] = []
        result: list[Objective] = []
        for o in existing:
            if o.objective_id not in pending:
                result.append(o)
                continue
            if status == "DONE" and not o.evidence_refs:
                raise ValueError(
                    f"'{o.objective_id}' has no evidence. DONE requires an "
                    "accepted candidate with receipts, not a model's assertion."
                )
            updated = o.model_copy(update={
                "status": status, "decided_at": utcnow(),
                "decided_by": by, "reason": reason,
            })
            result.append(updated)
            changed.append(updated)
            pending.discard(o.objective_id)
        if pending:
            raise ValueError(f"unknown objective IDs: {', '.join(sorted(pending))}")
        self.write(result)
        return changed

    def next_open(self) -> Objective | None:
        """The next workable objective -- only approved ones count."""
        for o in self.read():
            if o.status == "ACTIVE":
                return o
        for o in self.read():
            if o.status == "OPEN":
                return o
        return None
