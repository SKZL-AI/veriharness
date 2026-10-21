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
#: At most five digits a side: a longer number is no duration (and `int()` of
#: thousands of digits raises, which the receipt path must never meet).
_DURATION = re.compile(r"\b(?:in|after)\s+(?:(\d{1,5})\s*h)?\s*(?:(\d{1,5})\s*m)?", re.I)

#: What the provider prints on a limit (2026-10-04, four runs):
#: `Usage limit reached · continuing automatically at 3:20am · esc to cancel`
#: and `You've hit your session limit · resets 3:20am (Europe/Istanbul)`.
#: The clock time comes without `at`, so the old pattern read neither -- every
#: wait was `now + 1 h` and spent up to four of five resume attempts on a
#: five-hour reset (P2-43 Q4). Without an `at`, a bare number is not a time:
#: it needs minutes or am/pm (`resets 3 requests` is not 03:00).
_STATED_CLOCK = re.compile(
    r"(?:\bresets?(?:\s+at)?|\bcontinuing automatically at|\btry again at)\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?![\w:])", re.I)
#: The zone the provider names after its time, `(Europe/Istanbul)`.
_STATED_ZONE = re.compile(r"\s*\(\s*([A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\s*\)")

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

    # The provider's own banner first: its stated reset (with its zone) is the
    # best evidence there is, and a role's `finished in 5m` elsewhere must not
    # outrank it. A weekly limit states a day as well as an hour.
    stated = _stated_date(text or "", now)
    if stated is None:
        stated = _stated_clock(text or "", now)
    if stated is not None:
        return stated if stated is not _UNTRUSTED else now + DEFAULT_WAIT

    wait = _stated_duration(text or "")
    if wait is not None:
        return now + wait

    c = _CLOCK_TIME.search(text or "")
    if c and int(c.group(2) or 0) < 60:           # `resets at 99:99` is no time
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


#: Added to a reset time the provider states: the banner names the minute, the
#: reset happens somewhere inside it, and a run resumed one second early meets
#: the same banner -- whose reset time, read just after the minute, is then
#: tomorrow's.
STATED_RESET_MARGIN = timedelta(minutes=1)

#: A line that says something about a limit: the only lines a duration
#: (`retry in 2h 30m`) is read from. A role's `finished in 5m` is not one.
_LIMIT_WORDS = re.compile(r"\b(?:resets?|limit|quota|retry|try again)\b", re.I)
#: No provider waits longer than this for a duration; a larger number is a typo
#: or a role's, and reading it would park a run for good.
_MAX_STATED_WAIT = timedelta(hours=72)

#: How a provider's own banner starts (after its leading glyphs): the only
#: lines a stated clock time and zone are read from, so a role's own
#: `resets 5pm (Pacific/Kiritimati)` cannot supply the zone of a real banner.
_BANNER_OPENER = re.compile(
    r"^(?:(?:claude ai )?usage limit reached|you['’]?ve hit your|(?:your )?limit (?:will )?resets?)",
    re.I)
_LEAD = re.compile(r"^[\s\u0080-\U0010ffff]+")


def _stated_duration(text: str) -> timedelta | None:
    """The first `in 2h 30m` / `after 5m` on a line that talks about a limit."""
    for ln in text.splitlines():
        if not _LIMIT_WORDS.search(ln):
            continue
        for d in _DURATION.finditer(ln):
            if d.group(1) or d.group(2):
                hours, minutes = int(d.group(1) or 0), int(d.group(2) or 0)
                if hours <= 72 and minutes < 24 * 60:        # before timedelta can overflow
                    wait = timedelta(hours=hours, minutes=minutes)
                    if wait <= _MAX_STATED_WAIT:
                        return wait
    return None


#: A stated time whose zone this machine does not know: no evidence.
_UNTRUSTED = object()

#: `resets Oct 18, 12pm (Europe/Istanbul)`: the banner of a weekly limit (recorded
#: 2026-10-15, P3-08 slice 4). The day without a year, the hour with am/pm.
_STATED_DATE = re.compile(
    r"\bresets?(?:\s+at)?\s+(?:on\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?![\w:])", re.I)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")

#: The longest a dated reset may lie ahead: a week and a day. A weekly limit resets
#: within seven days; a stamp further out is a typo or a role's, and reading it
#: would park a run for good. Outside it (or already past) the stamp is no evidence
#: and the default wait applies.
MAX_STATED_DATE_WAIT = timedelta(days=8)


def _month_of(name: str) -> int | None:
    word = name.lower()
    for number, month in enumerate(_MONTHS, start=1):
        if word == month or word == month[:3] or (word == "sept" and month == "september"):
            return number
    return None


def _stated_date(text: str, now: datetime) -> datetime | None:
    """The moment a weekly limit's banner says it resets (`resets Oct 18, 12pm
    (Europe/Istanbul)`), plus the margin -- or None when the text states no such
    time, names no zone, names one this machine does not know, names a day that
    does not exist, or names a moment that is not within `MAX_STATED_DATE_WAIT`
    ahead (the default wait then applies, as for every unreadable stamp).

    Read only from lines that open like a provider banner (`_BANNER_OPENER`). The
    zone is the one **directly after the date's hour on the same line**
    (`12pm (Europe/Istanbul)`): a zone further along the line, or on another banner
    line, is not read (unlike `_stated_clock`, which takes one from any banner line).
    The year is not printed: the day is taken in the year of `now` in the zone and the
    year after, and the earliest of those that lies ahead decides (the year before is
    tried too, defensively, and can never win: that wall time is always past)."""
    from zoneinfo import ZoneInfo

    lines = [ln for ln in text.splitlines() if _BANNER_OPENER.match(_LEAD.sub("", ln))]
    first = None
    zone_name = None
    for ln in lines:
        for m in _STATED_DATE.finditer(ln):
            if first is None and _month_of(m.group(1)) is not None:
                first = m
                z = _STATED_ZONE.match(ln, m.end())
                if z is not None:
                    zone_name = z.group(1)
    if first is None or zone_name is None:
        return None
    month, day = _month_of(first.group(1)), int(first.group(2))
    hour, minute = int(first.group(3)), int(first.group(4) or 0)
    if not (1 <= hour <= 12 and minute < 60):
        return None
    hour = hour % 12 + (12 if first.group(5).lower() == "pm" else 0)
    try:
        zone = ZoneInfo(zone_name)
    except (KeyError, ValueError, OSError):
        return None
    year = now.astimezone(zone).year
    best = None
    for candidate in (year - 1, year, year + 1):
        try:
            wall = datetime(candidate, month, day, hour, minute, tzinfo=zone)
        except ValueError:                       # Feb 30, or Feb 29 in a common year
            continue
        moment = wall.astimezone(timezone.utc) + STATED_RESET_MARGIN
        if now < moment <= now + MAX_STATED_DATE_WAIT and (best is None or moment < best):
            best = moment
    return None if best is None else best.astimezone(now.tzinfo or timezone.utc)


def _stated_clock(text: str, now: datetime):
    """The next occurrence of the clock time the provider states, in the zone it
    states. None when the text states none in the provider's form (the older
    patterns then decide); `_UNTRUSTED` when it names a zone that does not
    resolve (the time is then not evidence, and the default wait applies).

    A zone named anywhere in the text applies to every stated time in it: the
    banner's first line carries the time without it (`continuing automatically
    at 3:20am`), its second the zone (`resets 3:20am (Europe/Istanbul)`).
    Read only from lines that open like a provider banner (`Usage limit
    reached ...`, `You've hit your ... limit ...`, `limit resets ...`), so a
    role's own `resets 5pm (Pacific/Kiritimati)` or `finished in 5m` elsewhere
    on the screen says nothing; a time without a zone keeps the older rule (the
    clock read in `now`'s own zone).
    """
    from zoneinfo import ZoneInfo

    lines = [ln for ln in text.splitlines() if _BANNER_OPENER.match(_LEAD.sub("", ln))]
    first = None
    zone_name = None
    for ln in lines:
        for m in _STATED_CLOCK.finditer(ln):
            has_at = bool(re.search(r"\bat\s*$", ln[:m.start(1)], re.I))
            if not (has_at or m.group(2) or m.group(3)):
                continue                      # a bare number is not a time
            if first is None:
                first = m
            z = _STATED_ZONE.match(ln, m.end())
            if z is not None and zone_name is None:
                zone_name = z.group(1)
    if first is None:
        return None
    hour = int(first.group(1))
    ampm = (first.group(3) or "").lower()
    if ampm:
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    minute = int(first.group(2) or 0)
    if minute > 59:
        return None
    if zone_name is None:
        return None                           # no zone named: the older rule decides
    try:
        zone = ZoneInfo(zone_name)
    except (KeyError, ValueError, OSError):
        return _UNTRUSTED
    local = now.astimezone(zone)
    # The earliest moment after `now` at which the zone's clock shows the time
    # (plus the margin), today or tomorrow, **in either fold**: on the autumn
    # change `1:30am` happens twice, and taking only the first reading waited
    # a day for a reset that was an hour away. A fold counts only if the clock
    # really shows that time then (`wall` survives the round trip); a time the
    # spring-forward change skips (`2:30am`) has neither, and is read as the
    # clock reads it after the gap (fold 0: 03:30 daylight time), never as an
    # instant before the change.
    best = None
    for day in (0, 1):
        wall = (local.replace(tzinfo=None, hour=hour, minute=minute, second=0,
                              microsecond=0) + timedelta(days=day))
        readings = [wall.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        real = [r for r in readings
                if r.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall]
        for reading in real or readings[:1]:
            moment = reading.astimezone(timezone.utc) + STATED_RESET_MARGIN
            if moment > now and (best is None or moment < best):
                best = moment
    return best.astimezone(now.tzinfo or timezone.utc)


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
