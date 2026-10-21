"""The project's declared dispatch budget (V3.3 P3-23, the production knob of the QA reserve).

A launcher's `dispatch_budget`, `dispatch_reservation` and `qa_reserve` were arguments of
the constructor a project's own driver factory passed (P3-04, P3-22/23 slices 1-3): no
operator-facing way set them, so the budget gate, the reserve and the recovery of a dead
controller's holds were reachable only through code. This module is the operator's way:

* `hoh project budget <id> --dispatch-budget N [--dispatch-reservation M] [--qa-reserve K]`
  writes `<project>/dispatch-budget.json` (strict, one regular file, replaced whole, with who
  set it and when) and, with no value, reads it back;
* every `ProjectController` (so `hoh project drive`, the resume loop and the recovery pass:
  they all build one) reads the file once, validates it again, **binds it to its launcher**
  (`launcher.bind_budget`) and says what it bound as the step `DISPATCH_BUDGET`.

A project with no file is exactly what it was. A file that cannot be read, or that the launcher
refuses (it sets another budget itself, or cannot take one), is a refusal by name before any
node is touched: the drive halts AMBIGUOUS with the reason, never a silent fallback to "no
budget". Nothing here loops or retries: it is one read, one bind, one step.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import durable_io
from .contracts import utcnow

FILE = "dispatch-budget.json"
SCHEMA = "hoh-dispatch-budget/1"
#: Above any budget a project of this product sets, and the bound `scheduler` quotes for a share.
MAX_VALUE = 10**9
MAX_BYTES = 4096

_KEYS = frozenset({"schema", "dispatch_budget", "dispatch_reservation", "qa_reserve",
                   "set_by", "epoch", "reason", "set_at"})
_TEXT = re.compile(r"[\x20-\x7e]{0,200}")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class BudgetConfigError(ValueError):
    """A declared budget that is not exactly one, or a value no launcher may be given."""


@dataclass(frozen=True)
class BudgetConfig:
    dispatch_budget: int
    dispatch_reservation: int | None
    qa_reserve: int
    set_by: str
    epoch: int | None
    reason: str
    set_at: str

    def to_bytes(self) -> bytes:
        body = {"schema": SCHEMA, "dispatch_budget": self.dispatch_budget,
                "dispatch_reservation": self.dispatch_reservation, "qa_reserve": self.qa_reserve,
                "set_by": self.set_by, "epoch": self.epoch, "reason": self.reason,
                "set_at": self.set_at}
        return (json.dumps(body, indent=2, ensure_ascii=True) + "\n").encode("ascii")

    def describe(self) -> str:
        share = "no share" if self.dispatch_reservation is None else f"share {self.dispatch_reservation}"
        return (f"dispatch_budget={self.dispatch_budget}, {share}, qa_reserve={self.qa_reserve} "
                f"(set by {self.set_by} at {self.set_at})")


def _whole(value: object, low: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= MAX_VALUE


def check_values(budget: object, reservation: object, qa_reserve: object) -> None:
    """The launcher's own rules (`HohRunLauncher.__init__`), stated once for the file and the CLI."""
    if not _whole(budget, 1):
        raise BudgetConfigError(f"dispatch_budget must be a whole number from 1 to {MAX_VALUE}, "
                                f"not {budget!r}")
    if reservation is not None and not _whole(reservation, 1):
        raise BudgetConfigError(f"dispatch_reservation must be a whole number from 1 to "
                                f"{MAX_VALUE}, not {reservation!r}")
    if not _whole(qa_reserve, 0):
        raise BudgetConfigError(f"qa_reserve must be a whole number from 0 to {MAX_VALUE}, "
                                f"not {qa_reserve!r}")
    if qa_reserve >= budget:
        raise BudgetConfigError(f"qa_reserve {qa_reserve} must be below the dispatch_budget "
                                f"{budget}: a reserve of the whole budget refuses every node "
                                f"that is not a repair")


def parse(data: bytes) -> BudgetConfig:
    """The declaration these bytes are, or `BudgetConfigError` saying why not."""
    if len(data) > MAX_BYTES:
        raise BudgetConfigError(f"{FILE} is {len(data)} bytes, above the {MAX_BYTES} of a declaration")

    def no_duplicates(pairs):
        seen: dict = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError(f"the key {key!r} is repeated")
            seen[key] = value
        return seen

    def no_constant(name):
        raise ValueError(f"{name} is not a number")

    try:
        doc = json.loads(data.decode("ascii"), object_pairs_hook=no_duplicates,
                         parse_constant=no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BudgetConfigError(f"{FILE} is not one JSON document: {exc}"[:200]) from exc
    if not isinstance(doc, dict) or set(doc) != _KEYS:
        raise BudgetConfigError(f"{FILE} is not exactly the keys {sorted(_KEYS)}")
    if doc["schema"] != SCHEMA:
        raise BudgetConfigError(f"{FILE} is not a declaration of this version ({SCHEMA})")
    check_values(doc["dispatch_budget"], doc["dispatch_reservation"], doc["qa_reserve"])
    epoch = doc["epoch"]
    if epoch is not None and not _whole(epoch, 0):
        raise BudgetConfigError("epoch is a whole number or null")
    if not isinstance(doc["set_by"], str) or not _SESSION.fullmatch(doc["set_by"]):
        raise BudgetConfigError("set_by is a session or operator name")
    if not isinstance(doc["reason"], str) or not _TEXT.fullmatch(doc["reason"]):
        raise BudgetConfigError("reason is printable ASCII, at most 200 characters")
    if not isinstance(doc["set_at"], str) or not _TIME.fullmatch(doc["set_at"]):
        raise BudgetConfigError("set_at is a UTC time")
    try:
        datetime.strptime(doc["set_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise BudgetConfigError("set_at is a real calendar time") from exc
    return BudgetConfig(doc["dispatch_budget"], doc["dispatch_reservation"], doc["qa_reserve"],
                        doc["set_by"], epoch, doc["reason"], doc["set_at"])


def read_config(project_dir: Path | str) -> BudgetConfig | None:
    """The project's declaration, or None when it declares none. Anything else that is at the
    path -- a link, a FIFO, a directory, an unreadable or oversize file, bytes that are not a
    declaration -- is `BudgetConfigError`: never "no budget"."""
    path = Path(project_dir) / FILE
    try:
        kind = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BudgetConfigError(f"{FILE} cannot be examined: {exc}"[:200]) from exc
    if not stat.S_ISREG(kind.st_mode):
        raise BudgetConfigError(f"{FILE} is a link or not one regular file")
    if kind.st_size > MAX_BYTES:
        raise BudgetConfigError(f"{FILE} is {kind.st_size} bytes, above the {MAX_BYTES} of a declaration")
    try:
        data = durable_io.read_single_link(path)
    except OSError as exc:
        raise BudgetConfigError(f"{FILE} cannot be read: {exc}"[:200]) from exc
    if data is None:
        return None
    return parse(data)


def write_config(project_dir: Path | str, *, dispatch_budget: int,
                 dispatch_reservation: int | None, qa_reserve: int, set_by: str,
                 epoch: int | None, reason: str) -> BudgetConfig:
    """Validates, replaces the declaration whole (a crash leaves the old one or the new one) and
    reads it back: what is returned is what is on disk."""
    check_values(dispatch_budget, dispatch_reservation, qa_reserve)
    cfg = BudgetConfig(dispatch_budget, dispatch_reservation, qa_reserve,
                       set_by, epoch, "".join(c if " " <= c <= "~" else "?" for c in reason)[:200],
                       utcnow())
    parse(cfg.to_bytes())                         # what is written is what is read back
    path = Path(project_dir) / FILE
    try:
        durable_io.durable_write(path, cfg.to_bytes(), mode=0o644)
    except OSError as exc:
        raise BudgetConfigError(f"{FILE} could not be written: {exc}"[:200]) from exc
    back = read_config(project_dir)
    if back != cfg:
        raise BudgetConfigError(f"{FILE} does not read back as written")
    return back


def bind(project_dir: Path | str, launcher: object) -> BudgetConfig | None:
    """Binds the project's declaration to `launcher`; None when the project declares none.

    A launcher that cannot take one (no `bind_budget`) refuses it by name rather than running
    unbudgeted under a file that says otherwise."""
    cfg = read_config(project_dir)
    if cfg is None:
        return None
    take = getattr(launcher, "bind_budget", None)
    if not callable(take):
        raise BudgetConfigError(
            f"this project declares a dispatch budget ({FILE}) and its launcher "
            f"({type(launcher).__name__}) cannot take one")
    take(cfg.dispatch_budget, cfg.dispatch_reservation, cfg.qa_reserve)
    return cfg
