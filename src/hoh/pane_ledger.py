"""The pane lifecycle ledger: what HoH closed, kept, refused and ended.

One JSON line per event, appended to `<run dir>/pane-lifecycle.jsonl` through
`durable_io.durable_append`. It is **a record, not a gate**: it sits in the run
directory beside protected evidence but is not in the capability witness's set
(the dispatcher appends during dispatches), so a role that can write there can
falsify it. Nothing decides from it -- ownership comes from the creation
record and the live Herdr snapshot (`HerdrDispatcher._close_own_pane`).
`hoh status` shows `summary()`.

Events:

* `closing`  -- written **before** a close command is sent; with no later
  `closed` for the same pane it is an unconfirmed close.
* `closed`   -- the close was confirmed (or the pane was already gone).
* `kept`     -- a pane was deliberately left open, with the reason.
* `refused`  -- a close or a release was refused, with the reason.
* `ended`    -- processes the role left running that HoH ended (`pids`,
  `count`); it changes no pane's state.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .durable_io import durable_append

LEDGER = "pane-lifecycle.jsonl"
"""The ledger's file name inside the run directory."""

EVENTS = frozenset({"closing", "closed", "kept", "refused", "ended"})
"""The only events a line may carry; anything else is a `ValueError`."""

#: The events that put a pane into a list of `summary()`, and the list.
_OPEN_LISTS = {"kept": "kept_open", "refused": "refused", "closing": "unconfirmed"}

#: The fields a summary entry carries, when the line has them.
_ENTRY_FIELDS = ("role", "pane", "tab", "why", "at")

GATE_WAIT_S = 5.0
"""How long `gate` polls for the lock before it answers `False`."""

GATE_PAUSE_S = 0.01
"""The pause between two polls of the lock."""


def append(run_dir: Path | str, event: str, **fields) -> bool:
    """Append one event; `True` if it is on disk, `False` if the write failed.

    An unknown event is a programming error and raises `ValueError`. A write
    that fails (no space, unwritable directory, a field that is not JSON) is
    `False` and never raised: a close or a refusal must not be turned into
    something else by its own record.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown pane ledger event {event!r}; known: "
                         f"{', '.join(sorted(EVENTS))}")
    record = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              **fields, "event": event}
    try:
        line = json.dumps(record, sort_keys=True, default=str)
        durable_append(Path(run_dir) / LEDGER, line + "\n")
    except Exception:  # noqa: BLE001 - a record must never raise out of a close
        return False
    return True


@contextmanager
def gate(run_dir: Path | str):
    """Hold the run's ledger gate: yields `True` while this process holds an exclusive
    `flock` on the run directory itself (a lock needs no file of its own), `False`
    when it could not be had within `GATE_WAIT_S` or the directory cannot be opened
    (missing, a symbolic link, a file). Never raises for either; the descriptor is
    closed on exit and the kernel drops the lock with it.

    It makes a check of the ledger and the append that follows one step for the
    reapers of a run (`surfaces.reap`); `append` itself does not take it. Where
    `flock` fails (a file system without it) or the run directory is a link, the
    gate is always `False`: the caller keeps what it would have changed.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(os.fspath(run_dir), flags)
    except OSError:
        fd = None
    held = False
    try:
        if fd is not None:
            deadline = time.monotonic() + GATE_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(GATE_PAUSE_S)
                except OSError:               # no flock here: waiting would not help
                    break
        yield held
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def summary(run_dir: Path | str) -> dict:
    """What the ledger says, for `hoh status`; never raises.

    `closed` counts `closed` lines and `processes_ended` sums the `count` of
    the `ended` lines. Every pane (or tab, for a line without a pane) whose
    **latest** line is `kept`, `refused` or `closing` is listed in `kept_open`,
    `refused` or `unconfirmed`; a later `closed` for the same pane removes it.
    A line that does not parse, or has an unknown event, is counted in
    `skipped_lines`. No ledger is zero and empty; an unreadable one is
    `{"unreadable": "<error>"}`, never an empty-looking summary.
    """
    path = Path(run_dir) / LEDGER
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeError) as exc:
        return {"unreadable": f"{type(exc).__name__}: {exc}"}

    closed = 0
    processes_ended = 0
    skipped = 0
    latest: dict[str, tuple[str, dict]] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(rec, dict) or rec.get("event") not in EVENTS:
            skipped += 1
            continue
        event = rec["event"]
        if event == "ended":
            count = rec.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                processes_ended += count
            continue
        if event == "closed":
            closed += 1
        key = str(rec.get("pane") or rec.get("tab") or "")
        if not key:
            continue
        latest[key] = (event, rec)

    lists: dict[str, list[dict]] = {name: [] for name in _OPEN_LISTS.values()}
    for event, rec in latest.values():
        name = _OPEN_LISTS.get(event)
        if name is None:
            continue
        lists[name].append({k: rec[k] for k in _ENTRY_FIELDS if k in rec})
    return {"closed": closed, "processes_ended": processes_ended,
            **lists, "skipped_lines": skipped}
