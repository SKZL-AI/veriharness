"""The project storage steward (STORAGE-D, P5-30).

One steward per project. It acts only on its own project's registered
namespace -- the registry's scratch, review, run and mutant directories of
that project and its registered runs bases -- and the one operation through
which it ever frees space is `VerifiedRelocation` (`hoh.verified_relocation`).
It never lists, reads or sizes a path outside that namespace; the only reads
outside are the canonical repository (read-only, through the evidence
sources), ``/proc`` (cwd and fd links of the caller's own uid) and the
archive targets it places on. Low free space caused by foreign data is
answered with owned optimisation and, when that cannot restore the reserve,
``NEEDS_YOU`` -- foreign data is never touched.

Two ways in:

* **Hooks only record.** `on_event(kind)` -- what `storage_park` (at
  ``admission_refused``, after the park is durable) and the `Controller` (at
  ``terminal_transition``, after a terminal state is persisted) call --
  appends one bounded line to the project's steward queue,
  ``<state_root>/projects/<p>/scratch/relocation/events.jsonl``, with a
  timeout of ``policy.hook_timeout_s`` (default 1 s), and returns. It never
  plans or executes in the caller's process and never creates the relocation
  scratch: until the first steward pass has created it, events are dropped
  and the answer says so (the periodic tick still runs).
* **The command pass does the work.** `run_once` (``hoh storage steward
  --project P --once``, run by the ``hoh-storage-steward@<p>`` unit) first
  resolves the intents an interrupted execute left (`VerifiedRelocation.recover`,
  which renames and removes nothing and releases the relocation lock again),
  then observes the admission, and only when that state is one of NORMAL,
  OPTIMIZE, PRESSURE, CRITICAL consumes the queue and acts by state: NORMAL
  observes only; OPTIMIZE computes candidates and plans (report only);
  PRESSURE or worse, or a forecast that crosses the reserve, plans and
  executes. Any other state -- ``status()`` raising, no state, an unknown
  string -- is ``UNOBSERVED``: nothing is executed, the queued events stay
  for the next pass, and a receipt says so with the error. Every
  optimisation writes one steward receipt ``<receipts>/steward-<UTC>.json``.

`candidate_plan(pressure)` is the coordinator's `ProjectSteward` protocol: no
side effects (no plan file, no event), `PlanItem`s with honest reclaimable
bytes and no item nested inside another. Honest means net: an item that is
archived before removal frees nothing in the pass that relocates it (C2's
placement verifies by extracting into the guest, and the bundle may be
staged there), so it is claimed at 0; an extract root released on its
committed receipt is claimed at its size. Hard-linked content is never
eligible, so it counts 0.

The report's ``bytes_reclaimed`` is the pass's net change of the project's
namespace: ``bytes_removed - guest_staging_bytes - extract_root_bytes -
bookkeeping_bytes`` (Revision 9 item 7). ``guest_staging_bytes`` are the
bundles this pass left in the guest (listed in ``staging_leftovers`` with
the target's); ``extract_root_bytes`` is the placement scratch C2's
placements left (listed in ``extract_roots_pending``); ``bookkeeping_bytes``,
its own field, is the growth of the relocation's ``intents.jsonl`` and the
event cursor in the relocation scratch (never negative) -- the journal is
append-only beside ``relocation.lock`` (Revision 8 item 4), so its growth is
namespace growth the pass caused, and the net stays honest. It may be
negative. Leftovers of earlier builds still on a target are reported apart,
in ``target_leftovers`` and ``target_leftover_bytes``: they lie on the
target, not in the guest, so they enter neither ``guest_staging_bytes`` nor
``bytes_reclaimed``. They come from one pass-level listing
(`VerifiedRelocation.target_leftovers`, Revision 10 item 3), taken on every
pass after the recovery and before the admission state is read -- so in
NORMAL, OPTIMIZE, PRESSURE, CRITICAL and UNOBSERVED passes alike -- with the
targets whose listing could not be taken (``target_leftovers_unmeasured``)
and the unlabelled names (``unknown_staging_entries``, path only); each path
is reported once (an item's own ``target_leftovers`` stay in its
ReclaimReceipt). Every report also carries ``unreadable_receipts`` (an
``UNREADABLE`` receipt is NEEDS_YOU; a ``STRANDED_TEMP`` file is listed
only) and ``target_config_problems`` (any problem of ``archive-targets.json``
is NEEDS_YOU, and the relocation then refuses every item). An execute that
``FAILED`` is NEEDS_YOU like a ``REFUSED`` one, in every state (NORMAL
included), and its finished items are counted; so is a recovery entry
``FAILED`` (a refusal that is not a live other holder, Revision 11 item 1) or
``RECOVERY_FAILED`` (one intent's write failed, item 4). Only ``BUSY`` -- a
proven live other holder -- is not NEEDS_YOU by itself. The steward receipt
is published whole and atomically (`verified_relocation.write_new`).

Nothing in this module removes a file or a directory.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hoh import storage_tiers as st
from hoh.storage_coordinator import NEEDS_YOU_PREFIX, CandidatePlan, PlanItem

EVENT_KINDS = ("admission_refused", "terminal_transition", "threshold_changed",
               "periodic", "forecast_crossing")
EVENTS_NAME = "events.jsonl"
CURSOR_NAME = "events.cursor"
RECEIPT_SCHEMA = "veriharness.steward-receipt/1"
NOT_MEASURED = "NOT_MEASURED"

OBSERVED = "OBSERVED"
PLANNED = "PLANNED"
EXECUTED = "EXECUTED"
NEEDS_YOU = "NEEDS_YOU"
UNOBSERVED = "UNOBSERVED"

_CANDIDATE_KINDS = ("scratch", "reviews", "runs", "mutants")
_SEVERITY = {"NORMAL": 0, "OPTIMIZE": 1, "PRESSURE": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class StewardPolicy:
    """How a steward runs. `forecast_horizon_s`: a forecast that reaches the
    reserve within this many seconds counts as crossing it. `target_timeout_s`
    bounds every call that touches an archive target; `hook_timeout_s`
    bounds the one append a hook makes; `max_event_bytes` bounds that line."""

    forecast_horizon_s: float = 6 * 3600.0
    target_timeout_s: float = 300.0
    hook_timeout_s: float = 1.0
    max_event_bytes: int = 512


def _utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class StorageSteward:
    """The storage steward of `project_id` (see the module docstring).

    `admission` is the `StorageAdmission` (or anything with its `status()`);
    `relocation` the project's `VerifiedRelocation` (None in a run process,
    whose steward only records events); `policy` a `StewardPolicy`. An
    optional `host_observer` (STORAGE-F) reports the VHDX allocation and the
    host volume's free space; without it both are `NOT_MEASURED`.
    """

    def __init__(self, project_id: str, registry, admission, relocation,
                 policy: StewardPolicy | None = None, *, host_observer=None) -> None:
        self.project_id = project_id
        self.registry = registry
        self.admission = admission
        self.relocation = relocation
        self.policy = policy or StewardPolicy()
        self.host_observer = host_observer
        if relocation is not None:
            relocation.timeout = self.policy.target_timeout_s

    # -- the queue --------------------------------------------------------- #

    @property
    def relocation_scratch(self) -> Path:
        """``<state_root>/projects/<p>/scratch/relocation`` -- computed, never
        created here."""
        return self.registry.state_root / "projects" / self.project_id / "scratch" / "relocation"

    def on_event(self, kind: str, **detail: Any) -> dict[str, Any]:
        """Record `kind` in the project's steward queue and return at once.

        One append of at most ``policy.max_event_bytes`` under a timeout of
        ``policy.hook_timeout_s``; on timeout or error the event is dropped
        and the answer's ``note`` says so. Never plans, never executes, never
        creates the relocation scratch."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown steward event {kind!r}; known: {list(EVENT_KINDS)}")
        directory = self.relocation_scratch
        if not directory.is_dir() or directory.is_symlink():
            return {"recorded": False, "kind": kind,
                    "note": f"storage steward: {kind} dropped, the relocation scratch "
                            f"{directory} does not exist yet (the next steward pass runs "
                            f"anyway)"}
        record = {"kind": kind, "utc": _utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                  **{k: v for k, v in detail.items() if isinstance(v, (str, int, bool))}}
        line = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        if len(line) > self.policy.max_event_bytes:
            line = (json.dumps({"kind": kind, "utc": record["utc"], "truncated": True})
                    + "\n").encode("ascii")
        path = directory / EVENTS_NAME
        try:
            st.bounded(lambda: _append_event(path, line), self.policy.hook_timeout_s,
                       "record a steward event")
        except Exception as exc:                # dropped, said, never raised
            return {"recorded": False, "kind": kind,
                    "note": f"storage steward: {kind} dropped ({type(exc).__name__}: {exc})"}
        return {"recorded": True, "kind": kind, "note": ""}

    def _consume_events(self) -> list[dict]:
        """The queued events after the cursor; the cursor moves past them.

        The cursor is itself an append-only log (one offset per line, the last
        complete line counts): nothing is replaced or overwritten."""
        directory = self.relocation_scratch
        path = directory / EVENTS_NAME
        cursor_path = directory / CURSOR_NAME
        offset = 0
        try:
            raw = cursor_path.read_bytes()
        except FileNotFoundError:
            raw = b""
        for line in reversed(raw[: raw.rfind(b"\n") + 1].splitlines()):
            if line.strip().isdigit():
                offset = int(line)
                break
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except FileNotFoundError:
            return []
        complete = data[: data.rfind(b"\n") + 1]
        events = []
        for line in complete.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("kind") in EVENT_KINDS:
                events.append(record)
        if complete:
            _append_event(cursor_path, f"{offset + len(complete)}\n".encode("ascii"))
        return events

    def _ensure_relocation_scratch(self) -> None:
        if self.relocation is None or self.relocation_scratch.is_dir():
            return
        from hoh.verified_relocation import RELOCATION_SESSION
        coordinator = self.relocation.coordinator
        lease = coordinator.acquire_session(self.project_id, RELOCATION_SESSION)
        try:
            self.registry.scratch_dir(self.project_id, RELOCATION_SESSION, "scratch")
        finally:
            coordinator.close_session(lease)

    # -- candidates -------------------------------------------------------- #

    def _sources(self) -> st.EvidenceSources:
        if self.relocation is not None:
            return self.relocation._sources(self.project_id)
        return st.EvidenceSources(self.registry, self.project_id)

    def candidates(self) -> list[Path]:
        """Directories directly inside the project's registered scratch,
        review, run and mutant directories, its registered runs bases and its
        intact REGISTERED legacy roots (STORAGE-L) -- found through the
        registry's indexes, never by scanning anything else. Quarantine leftovers and hidden names are not candidates."""
        sources = self._sources()
        parents = [d for _, _, d in sources.scratch_dirs(_CANDIDATE_KINDS)]
        parents += sources.runs_bases()
        parents += sources.legacy_roots()
        out = []
        for parent in parents:
            try:
                with os.scandir(parent) as listing:
                    children = sorted(listing, key=lambda e: e.name)
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or ".vh-quarantine." in child.name:
                    continue
                if child.is_dir(follow_symlinks=False):
                    out.append(Path(child.path))
        return out

    def _eligible(self, decisions: list[st.TierDecision]) -> list[st.TierDecision]:
        pins = (self.relocation.pinned(self._sources()) if self.relocation is not None
                else {"/"})
        out: list[st.TierDecision] = []
        for decision in decisions:
            if not decision.eligible or decision.project_id != self.project_id:
                continue
            if any(_overlaps(decision.path, d.path) for d in out):
                continue
            if any(_overlaps(pin, decision.path) for pin in pins):
                continue
            out.append(decision)
        return out

    def candidate_plan(self, pressure: Mapping[str, Any]) -> CandidatePlan:
        """The coordinator's protocol: eligible items of this project with
        their reclaimable bytes, no nested items, no side effects.

        The bytes are a claim the coordinator trusts, so they never exceed
        what an execute of the item frees in the project's namespace. An item
        that is archived first (a VERIFIED_DUPLICATE or a clone) frees
        nothing net in the pass that relocates it: C2's `place` verifies the
        bundle by extracting it into two registered extract roots in the
        guest (each the item's size, kept until their receipt is committed),
        and the bundle may be staged in the guest as well -- so its claim is
        0. An extract root whose release proof holds is removed without any
        bundle, staging or placement; its claim is its size. Hard-linked
        content is never eligible, so it counts 0."""
        sources = self._sources()
        decisions = [st.classify_tier(path, registry=self.registry, sources=sources)
                     for path in self.candidates()]
        items = tuple(PlanItem(d.path, d.bytes if _frees_without_placing(d) else 0, d.tier)
                      for d in self._eligible(decisions))
        return CandidatePlan(self.project_id, items)

    # -- the command pass -------------------------------------------------- #

    def run_once(self, *, plan_only: bool = False,
                 events: tuple[str, ...] = ("periodic",)) -> dict[str, Any]:
        """One optimisation pass (``hoh storage steward --once``); returns the
        report and writes a steward receipt for every pass that optimised."""
        for kind in events:
            if kind not in EVENT_KINDS:
                raise ValueError(f"unknown steward event {kind!r}")
        try:
            self._ensure_relocation_scratch()
        except Exception:                       # the session is busy: events wait
            pass
        bookkeeping_before = self._bookkeeping_size()
        recovered = self._recover()
        # Read in every state, before the admission is (Revision 10 items 2,
        # 3 and 6): what lies on the targets, what cannot be read among the
        # receipts, and what is wrong with archive-targets.json.
        listings = self._listings()
        # The state first: an unobserved pass consumes no queued event.
        error = ""
        try:
            status = dict(self.admission.status())
        except Exception as exc:                # unobservable: nothing is executed
            status, error = {}, f"{type(exc).__name__}: {exc}"
        state = status.get("state")
        if not isinstance(state, str) or state not in _SEVERITY:
            return self._unobserved(list(events), state, error, recovered, listings)
        queued = self._consume_events() if self.relocation_scratch.is_dir() else []
        kinds = list(events) + [e["kind"] for e in queued]
        horizon = status.get("seconds_to_reserve")
        if isinstance(horizon, (int, float)) and horizon <= self.policy.forecast_horizon_s:
            kinds.append("forecast_crossing")
        forecast = "forecast_crossing" in kinds
        report: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "project_id": self.project_id,
            "started_utc": _utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "events": kinds,
            "handled": sorted(set(kinds)),
            "state": state,
            "forecast_crossing": forecast,
            "free_before": status.get("effective_free_bytes"),
            "free_after": status.get("effective_free_bytes"),
            "vhdx_allocation": NOT_MEASURED,
            "host_volume_free": NOT_MEASURED,
            "bytes_selected": 0,
            "compressed_bytes": 0,
            "readback_status": [],
            "bytes_reclaimed": 0,
            "bytes_removed": 0,
            "guest_staging_bytes": 0,
            "extract_root_bytes": 0,
            "extract_roots_pending": [],
            "items_kept": [],
            "staging_leftovers": [],
            **listings,
            "plan": None,
            "execute": None,
            "bookkeeping_bytes": 0,
            "recovered": recovered,
            "needs_you": (any(_recovery_needs_you(r) for r in recovered)
                          or _listings_need_you(listings)),
            "message": "",
        }
        self._host(report)
        severity = _SEVERITY[state]
        if severity == 0 and not forecast:
            report["action"] = OBSERVED
            report["message"] = f"state {state}: observed only"
            if recovered:                       # what the recovery decided is recorded
                return self._receipt(report)
            return report
        if self.relocation is None:
            report["action"] = OBSERVED
            report.update(needs_you=True, message="no relocation configured for this steward")
            return self._receipt(report)
        plan = self.relocation.plan(self.project_id, self.candidates())
        report["plan"] = {"path": os.fspath(plan.path), "digest": plan.digest,
                          "items": len(plan.items), "rejected": len(plan.rejected)}
        report["bytes_selected"] = plan.bytes
        report["items_kept"] += [{"path": r["path"], "why": r["reason"]} for r in plan.rejected]
        act = (severity >= 2 or forecast) and not plan_only
        if not act:
            report["action"] = PLANNED
        else:
            report["action"] = EXECUTED
            if plan.items:
                outcome = self.relocation.execute(plan.path)
                report["execute"] = {"status": outcome.status, "detail": outcome.detail}
                for item in outcome.items:
                    leftover = item.get("staging_leftover") or {}
                    if leftover.get("where") == "guest":
                        report["guest_staging_bytes"] += int(leftover.get("bytes") or 0)
                    report["extract_roots_pending"] += list(item.get("extract_roots") or [])
                    report["compressed_bytes"] += int(item.get("compressed_bytes") or 0)
                    report["readback_status"].append(
                        {"path": item["path"], "status": item.get("readback_status")})
                    if item.get("staging_leftover"):
                        report["staging_leftovers"].append(item["staging_leftover"])
                    # An item's own target_leftovers stay in its ReclaimReceipt
                    # (why it was not built there); the report's come from the
                    # pass-level listing alone, so each path is reported once.
                    if item.get("status") == "RELOCATED":
                        report["bytes_removed"] += int(item.get("bytes") or 0)
                    else:
                        report["items_kept"].append({"path": item["path"],
                                                     "why": item.get("reason")})
                    if item.get("needs_you"):
                        report["needs_you"] = True
                report["extract_root_bytes"] = int(outcome.placement_scratch_bytes or 0)
                if outcome.status != "DONE":    # FAILED is judged like REFUSED
                    report["needs_you"] = report["needs_you"] or \
                        outcome.status in ("REFUSED", "FAILED")
        # Net: what left the project's namespace minus what the pass added to
        # it -- bundles still staged in the guest (staging_leftovers), the
        # extract roots C2's placements left (extract_roots_pending, released
        # by a later pass once their receipt is committed) and, as its own
        # field, the growth of the relocation's own journal and event cursor.
        # Target leftovers lie on the target and are not part of it. Never
        # removed here.
        report["bookkeeping_bytes"] = max(0, self._bookkeeping_size() - bookkeeping_before)
        report["bytes_reclaimed"] = (report["bytes_removed"] - report["guest_staging_bytes"]
                                     - report["extract_root_bytes"]
                                     - report["bookkeeping_bytes"])
        try:
            after = dict(self.admission.status())
        except Exception as exc:                # unobservable afterwards: said, NEEDS_YOU
            after = {"state": UNOBSERVED, "error": f"{type(exc).__name__}: {exc}"}
        report["free_after"] = after.get("effective_free_bytes", NOT_MEASURED)
        report["state_after"] = after.get("state")
        self._judge(report, status, after)
        return self._receipt(report)

    def _recover(self) -> list[dict]:
        """Resolve interrupted executes before anything else (Revision 8
        item 4); a failure is reported, never raised."""
        if self.relocation is None:
            return []
        try:
            return list(self.relocation.recover(self.project_id))
        except Exception as exc:                # noqa: BLE001 - said in the report
            return [{"status": "RECOVERY_FAILED", "needs_you": True,
                     "detail": f"{type(exc).__name__}: {exc}"}]

    def _listings(self) -> dict[str, Any]:
        """The pass-level fields read in every state (Revision 10 items 2, 3
        and 6): the targets' staging listing (`VerifiedRelocation.target_leftovers`
        with this steward's relocation scratch as the journal), the receipts
        that cannot be used and the stranded temporary files
        (`VerifiedRelocation.unreadable_receipts`), and the problems of
        ``archive-targets.json``. Read-only; a failure is reported, never
        raised. Without a relocation every field is empty."""
        out: dict[str, Any] = {"target_leftovers": [], "target_leftover_bytes": 0,
                               "target_leftovers_unmeasured": [],
                               "unknown_staging_entries": [], "unreadable_receipts": [],
                               "target_config_problems": []}
        if self.relocation is None:
            return out
        out["target_config_problems"] = list(getattr(self.relocation, "target_problems", []))
        try:
            listing = self.relocation.target_leftovers(self.project_id,
                                                       self.relocation_scratch)
        except Exception as exc:                # noqa: BLE001 - said in the report
            listing = {"found": [], "unknown": [],
                       "unmeasured": [{"target_id": None, "path": None,
                                       "detail": f"{type(exc).__name__}: {exc}"}]}
        out["target_leftovers"] = list(listing.get("found") or [])
        out["target_leftover_bytes"] = sum(int(x.get("bytes") or 0)
                                           for x in out["target_leftovers"])
        out["target_leftovers_unmeasured"] = list(listing.get("unmeasured") or [])
        out["unknown_staging_entries"] = list(listing.get("unknown") or [])
        try:
            out["unreadable_receipts"] = list(self.relocation.unreadable_receipts())
        except Exception as exc:                # noqa: BLE001 - said in the report
            out["unreadable_receipts"] = [{"status": "UNREADABLE", "path": None,
                                           "detail": f"{type(exc).__name__}: {exc}"}]
        return out

    def _bookkeeping_size(self) -> int:
        """Bytes of the relocation scratch's journal and event cursor (what a
        pass appends to the project's namespace besides staging and
        placement)."""
        total = 0
        for name in ("intents.jsonl", CURSOR_NAME):
            try:
                total += os.lstat(self.relocation_scratch / name).st_size
            except OSError:
                continue
        return total

    def _unobserved(self, events: list[str], state: Any, error: str,
                    recovered: list[dict], listings: Mapping[str, Any]) -> dict[str, Any]:
        """The receipt of a pass whose admission state is not one of the four
        known states: nothing planned or executed, no queued event consumed."""
        report: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "project_id": self.project_id,
            "started_utc": _utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "events": events,
            "handled": [],
            "state": UNOBSERVED,
            "observed_state": state if isinstance(state, (str, int, float)) else repr(state),
            "error": error or f"admission state {state!r} is not one of {sorted(_SEVERITY)}",
            "action": UNOBSERVED,
            "forecast_crossing": False,
            "free_before": NOT_MEASURED,
            "free_after": NOT_MEASURED,
            "vhdx_allocation": NOT_MEASURED,
            "host_volume_free": NOT_MEASURED,
            "bytes_selected": 0,
            "compressed_bytes": 0,
            "readback_status": [],
            "bytes_reclaimed": 0,
            "bytes_removed": 0,
            "guest_staging_bytes": 0,
            "extract_root_bytes": 0,
            "bookkeeping_bytes": 0,
            "extract_roots_pending": [],
            "items_kept": [],
            "staging_leftovers": [],
            **listings,
            "plan": None,
            "execute": None,
            "recovered": recovered,
            "needs_you": True,
            "message": (f"{NEEDS_YOU}: the admission state is unobserved; nothing executed, "
                        f"queued events kept for the next pass"),
        }
        self._host(report)
        return self._receipt(report)

    def _host(self, report: dict) -> None:
        if self.host_observer is None:
            return
        try:
            reading = self.host_observer.read()
        except Exception:                       # unobservable stays NOT_MEASURED
            return
        if isinstance(reading, Mapping):
            for key in ("vhdx_allocation", "host_volume_free"):
                if reading.get(key) is not None:
                    report[key] = reading[key]

    @staticmethod
    def _judge(report: dict, before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
        """NEEDS_YOU when owned optimisation cannot restore the reserve --
        e.g. when foreign data fills the disk."""
        effective = before.get("effective_free_bytes")
        reserve = before.get("reserve_bytes")
        reserved = before.get("reserved_bytes") or 0
        if not isinstance(effective, int) or not isinstance(reserve, int):
            if _SEVERITY[before["state"]] >= 2:
                report["needs_you"] = True
                report["message"] = (f"{NEEDS_YOU_PREFIX}: free space unobservable "
                                     f"(reclaimed {report['bytes_reclaimed']} B)")
            return
        shortfall = max(0, reserve + int(reserved) - effective)
        report["shortfall_bytes"] = shortfall
        if not isinstance(after.get("state"), str) or after["state"] not in _SEVERITY:
            report["needs_you"] = True
            report["message"] = (f"{NEEDS_YOU_PREFIX}: the admission state after the pass is "
                                 f"unobserved ({after.get('error') or after.get('state')!r}); "
                                 f"reclaimed {report['bytes_reclaimed']} B")
            return
        if shortfall > report["bytes_reclaimed"] or _SEVERITY[after["state"]] >= 2:
            missing = max(0, shortfall - report["bytes_reclaimed"])
            report["needs_you"] = True
            report["message"] = (f"{NEEDS_YOU_PREFIX}: missing {missing} bytes (shortfall "
                                 f"{shortfall} B, reclaimed {report['bytes_reclaimed']} B); "
                                 f"the rest of the pressure is not this project's to free")
        elif not report["message"]:
            report["message"] = f"owned optimisation reclaimed {report['bytes_reclaimed']} B"

    def _receipt(self, report: dict) -> dict:
        report["finished_utc"] = _utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        if report["needs_you"] and not report["message"].startswith(NEEDS_YOU_PREFIX):
            report["message"] = (report["message"] + "; " if report["message"] else "") \
                + "NEEDS_YOU"
        directory = (self.relocation.receipts_dir if self.relocation is not None else None)
        if directory is None:
            return report
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _utc().strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"steward-{stamp}.json"
        # Whole and atomic, never torn (Revision 10 item 2): the relocation's
        # writer -- a temporary file written whole, fsynced, then published.
        from hoh.verified_relocation import write_new
        write_new(path, json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True,
                                   default=str).encode("ascii"))
        report["receipt_path"] = os.fspath(path)
        return report


def _append_event(path: Path, line: bytes) -> None:
    """One ``O_APPEND`` write of a complete line, fsynced; never follows a
    link and never creates the directory."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _recovery_needs_you(outcome: Mapping[str, Any]) -> bool:
    """A recovery entry that needs the operator: one marked so, and every
    ``FAILED`` or ``RECOVERY_FAILED`` entry whatever it says (only ``BUSY``, a
    live other holder, waits for the next pass by itself)."""
    return bool(outcome.get("needs_you")) or outcome.get("status") in (
        "FAILED", "RECOVERY_FAILED")


def _listings_need_you(listings: Mapping[str, Any]) -> bool:
    """A problem of ``archive-targets.json`` or an unreadable receipt needs
    the operator; target leftovers, unknown staging names and stranded
    temporary files do not by themselves."""
    return bool(listings.get("target_config_problems")) or any(
        r.get("status") == "UNREADABLE" for r in listings.get("unreadable_receipts") or [])


def _frees_without_placing(decision: st.TierDecision) -> bool:
    """Whether relocating `decision`'s item builds and places no bundle (an
    extract root released on its committed receipt)."""
    return (decision.tier == st.REPRODUCIBLE_SCRATCH
            and (decision.proof or {}).get("type") == "extract_root")


def _overlaps(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


__all__ = ["EVENT_KINDS", "EXECUTED", "NEEDS_YOU", "NOT_MEASURED", "OBSERVED", "PLANNED",
           "StewardPolicy", "StorageSteward"]
