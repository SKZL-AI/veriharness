"""What each dispatch cost, recorded while it happens.

This project has repeatedly had to answer "how much did that take?" from git
archaeology: counting commits, reading step logs, inferring wall-clock from
file timestamps. Every one of those is a weaker source than a record written at
the moment, and `assurance.py` exists because weaker sources kept being
accepted as proof.

So this module writes one line per dispatch, at the dispatch, into an
append-only log. What it is careful about:

**A missing measurement is missing, not zero.** `tokens_in`/`tokens_out` are
`None` when the harness did not report them, and every aggregate says how many
of its inputs were unknown. A sum over Nones-treated-as-zero is a smaller
number that looks like a measurement, which is precisely the shape of this
project's fourth false green. `Summary.tokens` returns `None` rather than a
total when any contributing record is unknown -- annoying on purpose.

**A cost proxy is labelled as a proxy.** Where tokens are unavailable, the
harness may still report something monotone -- a request count, a quota
percentage. That goes in `quota_proxy` with its `quota_proxy_kind`, and it is
never silently compared with a token count.

**The record says why it ended, not just that it did.** `failure_class` comes
from `taxonomy.py`, so a reader can tell a rate limit from a broken contract
without parsing a message, and the retry policy that was applied is derivable
from the same table rather than remembered.

**Nothing here decides anything.** Telemetry that feeds back into control is
telemetry people start shaping. Routing decisions come later and separately,
from this data, in the open.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import Field

from .contracts import Strict, utcnow
from .taxonomy import FailureClass, disposition

TELEMETRY_SCHEMA_VERSION = 1

#: What an identity field says when the backend genuinely cannot report it.
#: An empty string does not distinguish "the harness does not tell us" from
#: "nobody asked", and a log full of the second wearing the first is what
#: `tools/telemetry_audit.py` was written to find.
NOT_AVAILABLE = "NOT_AVAILABLE"


class DispatchRecord(Strict):
    """One role dispatch, or one orchestration step that cost something."""

    schema_version: int = TELEMETRY_SCHEMA_VERSION

    # -- what this was ------------------------------------------------------ #
    #: The project this belongs to, where an orchestrated campaign is running.
    #: Empty for a bare `hoh run` with no project above it.
    project_id: str = ""
    node_id: str = ""
    run_id: str = ""
    iteration: int = 0
    attempt: int = 1
    #: "planner" / "developer" / "qa" / "gate" / "merge" / "closure".
    role: str

    # -- who did it --------------------------------------------------------- #
    #: The dispatch mechanism: "herdr", "subprocess", "none".
    backend: str = ""
    #: The provider behind it, where one is named.
    provider: str = ""
    #: The model, where one is named. `NOT_AVAILABLE` where the backend
    #: cannot report one -- an agent in a pane chooses its own, and this
    #: process never learns which. Empty means nobody filled the field in, and
    #: the audit treats the two differently on purpose: "whatever the harness
    #: defaults to" is a genuine and unhelpful state, but it is a *stated* one.
    model: str = ""
    effort: str = ""

    # -- when --------------------------------------------------------------- #
    started_at: str = Field(default_factory=utcnow)
    ended_at: str = ""
    wallclock_seconds: float | None = None

    # -- how it went -------------------------------------------------------- #
    #: "ok" / "failed" / "inconclusive".
    outcome: str = "ok"
    failure_class: FailureClass | None = None
    detail: str = ""
    #: Retries already spent on this dispatch when the record was written.
    retries: int = 0
    #: Calls that actually reached the provider for this record. One line is
    #: not one call in either direction: a retried dispatch is one line and
    #: several calls, and a dispatch refused at the budget is one line and
    #: none. `None` means the record predates the field -- which is why the
    #: counter that reads it reports how many lines could not answer instead
    #: of quietly scoring them as one.
    provider_calls: int | None = None
    waited_seconds: int = 0

    # -- what it cost ------------------------------------------------------- #
    #: None means the harness did not report it. Never 0 as a stand-in: a zero
    #: that means "unknown" is the same defect as an empty list that means
    #: "no actions".
    tokens_in: int | None = None
    tokens_out: int | None = None
    #: A monotone stand-in where tokens are unavailable, with its unit named.
    quota_proxy: float | None = None
    quota_proxy_kind: str = ""

    # -- what was watched while it ran --------------------------------------- #
    #: How many trees the capability witness digested around this dispatch,
    #: and how many directory listings it watched. Recorded rather than
    #: derived: the protected set is built from the directories that exist at
    #: dispatch time, so it differs per dispatch, and a reader who has to
    #: reconstruct it from the controller's source is reading the wrong thing
    #: -- twice now a property of a run was inferred from what the code does
    #: today rather than from what that run recorded. `None` means the record
    #: predates this field, which is not the same as zero.
    witnessed_trees: int | None = None
    witnessed_listings: int | None = None

    # -- what it produced --------------------------------------------------- #
    receipts: int = 0
    #: Criteria that were red on the predecessor and green on the candidate.
    discriminating: int = 0
    #: Of those, how many were red only because their own file was absent
    #: from the predecessor (O112). Kept separate: counting them as
    #: discriminating overstates what the iteration demonstrated.
    artefactual: int = 0
    #: The node this one repairs, if any, and how deep the chain is. A repair
    #: of a repair is a different situation from a first attempt, and a
    #: campaign that cannot tell them apart cannot report its own convergence.
    repair_of: str = ""
    repair_depth: int = 0

    def cost_known(self) -> bool:
        return self.tokens_in is not None or self.tokens_out is not None

    def retry_policy(self) -> str:
        """What the taxonomy says about this record's failure, in one line."""
        if self.failure_class is None:
            return "no failure"
        d = disposition(self.failure_class)
        return (
            f"{self.failure_class.value}: up to {d.max_retries} retr"
            f"{'y' if d.max_retries == 1 else 'ies'} against the "
            f"{d.budget.value} budget, "
            f"{'auto-resumable' if d.auto_resume else 'not auto-resumable'}"
            f"{', human gate' if d.human_gate else ''}"
        )


class Summary(Strict):
    """An aggregate that says what it does not know."""

    dispatches: int = 0
    by_role: dict[str, int] = Field(default_factory=dict)
    by_outcome: dict[str, int] = Field(default_factory=dict)
    by_failure_class: dict[str, int] = Field(default_factory=dict)
    wallclock_seconds: float = 0.0
    wallclock_unknown: int = 0
    #: The token total, or None when any contributing record did not report
    #: one. Deliberately all-or-nothing: a partial total is a number a reader
    #: will quote as if it were complete.
    tokens: int | None = None
    tokens_unknown: int = 0
    retries: int = 0
    receipts: int = 0
    discriminating: int = 0
    artefactual: int = 0

    def line(self) -> str:
        parts = [
            f"{self.dispatches} dispatch(es)",
            f"{self.wallclock_seconds:.1f}s wallclock"
            + (f" ({self.wallclock_unknown} unknown)" if self.wallclock_unknown else ""),
            (
                f"{self.tokens} tokens"
                if self.tokens is not None
                else f"tokens NOT_DETERMINABLE ({self.tokens_unknown} of "
                     f"{self.dispatches} unreported)"
            ),
            f"{self.retries} retr{'y' if self.retries == 1 else 'ies'}",
            f"{self.receipts} receipt(s)",
            f"{self.discriminating} discriminating"
            + (f" ({self.artefactual} artefactual)" if self.artefactual else ""),
        ]
        return " · ".join(parts)


def summarise(records: list[DispatchRecord]) -> Summary:
    """Aggregates, and reports what was not measurable rather than rounding it.

    Note what is *not* here: no averages over a mixed population, no cost per
    accepted candidate, no savings. Those are comparisons, and a comparison
    needs a matched baseline that does not exist yet. Producing one from this
    data would be the number-before-the-measurement this project keeps warning
    about in other people's work.
    """
    s = Summary(dispatches=len(records))
    unknown_ = 0
    total_ = 0
    for r in records:
        s.by_role[r.role] = s.by_role.get(r.role, 0) + 1
        s.by_outcome[r.outcome] = s.by_outcome.get(r.outcome, 0) + 1
        if r.failure_class is not None:
            k = r.failure_class.value
            s.by_failure_class[k] = s.by_failure_class.get(k, 0) + 1
        if r.wallclock_seconds is None:
            s.wallclock_unknown += 1
        else:
            s.wallclock_seconds += r.wallclock_seconds
        if r.cost_known():
            total_ += (r.tokens_in or 0) + (r.tokens_out or 0)
        else:
            unknown_ += 1
        s.retries += r.retries
        s.receipts += r.receipts
        s.discriminating += r.discriminating
        s.artefactual += r.artefactual
    s.tokens_unknown = unknown_
    # All or nothing. A total over the records that happened to report is a
    # smaller number that reads like a complete one.
    s.tokens = None if unknown_ else total_
    return s


class TelemetryLog:
    """An append-only JSONL log, written the way the stores write state.

    Append-only because a telemetry record that can be revised is a telemetry
    record someone will revise. One line per dispatch, fsynced, so a killed
    process loses at most the line it was writing -- and the reader tolerates
    exactly that: a trailing partial line is skipped rather than raising,
    because losing the whole history to one truncated write would be the worse
    failure.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def append(self, record: DispatchRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text_line = record.model_dump_json() + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(text_line)
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> list[DispatchRecord]:
        if not self.path.exists():
            return []
        out_list: list[DispatchRecord] = []
        for text_line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            text_line = text_line.strip()
            if not text_line:
                continue
            try:
                out_list.append(DispatchRecord.model_validate_json(text_line))
            except ValueError:
                # A truncated last line from a killed writer. Skipped, not
                # raised: the rest of the history is still evidence.
                continue
        return out_list

    def summary(self) -> Summary:
        return summarise(self.read())

    def rewrite_sorted(self) -> None:
        """Rewrites the log in start order, atomically, keeping every record.

        Concurrent appenders interleave, which is fine for storage and awkward
        for reading. This does not drop, merge or edit anything -- it is a sort
        -- and it writes through a temporary file in the same directory so a
        crash leaves either the old log or the new one.
        """
        sentence = sorted(self.read(), key=lambda r: (r.started_at, r.run_id, r.role))
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".telemetry-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for r in sentence:
                    fh.write(r.model_dump_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).replace(self.path.with_name(self.path.name + ".partial"))
            raise


def record_from_role(
    *,
    role: str,
    run_id: str,
    iteration: int,
    attempt: int,
    started_at: str,
    ended_at: str,
    backend: str = "",
    model: str = "",
    outcome: str = "ok",
    failure_class: FailureClass | None = None,
    detail: str = "",
    usage: dict | None = None,
    **rest,
) -> DispatchRecord:
    """Builds a record from what a dispatcher actually returned.

    `usage` is whatever the harness handed back, which differs per provider and
    is frequently absent. Nothing is invented: a key that is not there leaves
    the field `None`, and `quota_proxy` is filled only when the harness offered
    something monotone to fill it with.
    """
    usage = usage or {}
    duration_ = None
    try:
        from datetime import datetime

        a = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        duration_ = max((b - a).total_seconds(), 0.0)
    except (ValueError, AttributeError):
        duration_ = None
    proxy = usage.get("requests") if "requests" in usage else None
    return DispatchRecord(
        role=role, run_id=run_id, iteration=iteration, attempt=attempt,
        started_at=started_at, ended_at=ended_at, wallclock_seconds=duration_,
        backend=backend, model=model, outcome=outcome,
        failure_class=failure_class, detail=detail,
        tokens_in=usage.get("input_tokens"),
        tokens_out=usage.get("output_tokens"),
        quota_proxy=float(proxy) if proxy is not None else None,
        quota_proxy_kind="requests" if proxy is not None else "",
        **rest,
    )
