"""Detecting an exhausted provider quota, and resuming afterwards.

Why this deserves its own module: from the outside a quota exhaustion looks
exactly like a failure, and it is not one. In run `a02` HoH once reported
`not accepted: K1..K9`, as if QA had examined nine criteria and rejected every
one of them -- in truth not a single one had been evaluated. The outage also
consumed two of three progress slots. Both are fixed; what was missing was the
**resumption**.

The construction follows the rest of the project: HoH decides *what counts*
and leaves the *doing* to whoever can do it. A controller that sleeps for hours
while holding the lock would be indistinguishable from a hung one. Instead the
run stays visibly blocked, carries the reason machine-readably in its state,
and `hoh resume-quota` picks it up again -- by hand, from cron, or as a Herdr
plugin action.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: Phrasings the harnesses use to report an exhausted quota. Deliberately a
#: list of separate patterns: a new phrasing is added here instead of nesting
#: one grown expression ever deeper.
#:
#: The German phrase stays in the list on purpose. These patterns match text
#: emitted by *provider CLIs*, not by this code -- a German-language harness
#: message must still be recognised, however English the surrounding code is.
_PATTERNS = [
    re.compile(r"usage limit reached", re.I),
    re.compile(r"rate.?limit(ed)?\b", re.I),
    re.compile(r"quota (exceeded|exhausted)", re.I),
    re.compile(r"\b429\b"),
    re.compile(r"too many requests", re.I),
    re.compile(r"kontingent (erschoepft|aufgebraucht)", re.I),
    re.compile(r"limit will reset", re.I),
]

#: "Your limit will reset at 5pm" / "resets at 17:00" / "in 2h 30m"
_CLOCK_TIME = re.compile(r"reset[s]?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
_DURATION = re.compile(r"(?:in|after)\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.I)

#: How long to wait when the text names no time at all. With the providers used
#: here a quota typically resets hourly or every five hours; one hour is the
#: best compromise between "do not wait needlessly" and "do not poll for
#: nothing".
DEFAULT_WAIT = timedelta(hours=1)

#: After this many unsuccessful resumptions the run stays blocked. Unbounded
#: automatic restarts can cost money without making progress -- the same
#: reasoning as behind the loop's progress budget.
MAX_RESUME_ATTEMPTS = 5


def is_quota_exhausted(text: str) -> bool:
    """Does this error text indicate an exhausted quota?"""
    return any(p.search(text or "") for p in _PATTERNS)


def next_attempt_at(text: str, *, now: datetime | None = None) -> datetime:
    """The earliest moment at which another attempt makes sense.

    Reads a reset time out of the text, falling back to `DEFAULT_WAIT`. A time
    of day that has already passed counts as tomorrow -- "resets at 5pm" seen
    at 11pm does not mean today.
    """
    now = now or datetime.now(timezone.utc)

    d = _DURATION.search(text or "")
    if d and (d.group(1) or d.group(2)):
        return now + timedelta(hours=int(d.group(1) or 0), minutes=int(d.group(2) or 0))

    c = _CLOCK_TIME.search(text or "")
    if c:
        hour = int(c.group(1)) % 12
        if (c.group(3) or "").lower() == "pm":
            hour += 12
        elif not c.group(3) and int(c.group(1)) < 24:
            hour = int(c.group(1))          # 24h notation without am/pm
        target = now.replace(hour=hour % 24, minute=int(c.group(2) or 0),
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return now + DEFAULT_WAIT


def is_due(retry_after: str | None, *, now: datetime | None = None) -> bool:
    """Has the recorded waiting period elapsed? With nothing recorded: yes.

    An unreadable timestamp also counts as due. Being unable to parse a date is
    not a reason to keep a run blocked forever -- the resume path has its own
    attempt limit (`MAX_RESUME_ATTEMPTS`) to stop it from spinning.
    """
    if not retry_after:
        return True
    try:
        target = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
    except ValueError:
        return True
    if target.tzinfo is None:
        # A timestamp without a zone reached this point from an older state
        # file. Comparing it raises TypeError ("can't compare offset-naive and
        # offset-aware datetimes"), which `except ValueError` does not catch --
        # so the resume path died with a traceback instead of deciding. These
        # stamps were always written as UTC, so that is what they are read as.
        target = target.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= target
