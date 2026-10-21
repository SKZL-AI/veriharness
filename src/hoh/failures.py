"""One normalised class for every failed role dispatch (P2-43, amendment A5).

Until this file existed a failed dispatch was whatever its text happened to
say. QA's `DispatchError` became an outage string and then, one step later, a
rejected candidate: a provider's usage limit spent an iteration, eight times
between 2026-10-02 and 2026-10-07. The same limit in the planner was found by
a phrase search over the whole message, pane included, and cost an iteration
on resume. A prompt stall and a role that outlived its time budget were
routed to the digest as decisions nobody had to take.

This module is the vocabulary and the classifier, nothing else. It decides
*what a failure was*; the controller decides what to do about it. Four
distinctions carry the design:

* **A wait is not a decision.** A usage limit, a rate limit, an unavailable
  provider, a dropped connection, a stalled prompt and a role timeout are
  cured by time: `RetryMode.AUTOMATIC`. An expired credential, an account
  action, a model that is not there and a missing local runtime are not, and
  are never woken by a timer: `RetryMode.OPERATOR_REQUIRED`.
* **A role timeout is not a provider failure.** `ROLE_TIMEOUT` is its own
  class, as A5 requires: the role did not end its turn within its budget, and
  that says nothing about the provider.
* **Evidence has a place, and an author.** A pane is a role's output as much
  as a harness's, and a failure's first line can carry a role's words. A
  phrase counts only where a harness would print it: the start of a line (in
  the first line also right after this project's own handover words, `...
  failed:` and `... exited with <n> and without output:`), ending the
  statement (the end of the line, or a banner's `·` `|` `(` trail). The same
  words in a code literal, a diff line, a test failure, or a role's sentence
  (`qa reported: quota exceeded handling is missing`, `Rate limit: cap at
  100/min`) say nothing about the dispatch. What the classifier does
  not recognise is `UNCLASSIFIED`, and that is *not* a wait: the existing
  route stays. A limit worded in a way this module does not know is therefore
  still lost as before -- a tracked gap, not a closed one.
* **A turn that ended normally is read more strictly than one that failed.**
  `classify_text` reads the screen of a dispatch that already failed, where a
  role-written `Rate limit.` costs a bounded wait. `final_screen_limit` decides
  about a developer turn that *finished* (Herdr reports a pane that ran into a
  limit as an ordinary end of turn, P2-43d), where a wrong answer parks good
  work: only a line that is exactly one of the recorded banner shapes among the
  last `FINAL_SCREEN_LINES` lines counts, and the failed dispatch carries those
  lines and nothing else of the screen. A banner counts only if it is new: one
  the pane already showed when the prompt was sent (`before`, P2-43e) belongs to
  an earlier turn -- except the harness's status-area line (`⚠`), which a pane
  draws only while a limit holds: that one is about now, and is never spent.

* **A pane that waits in the limit's own menu is a wait, not a dialog** (P3-08 slice 4,
  T-UL1). Herdr calls any pane that waits for a key `blocked`, and the dispatcher took every
  one for an approval; the menu Claude Code opens after `You've hit your weekly limit`
  (`1. Stop and wait for limit to reset`) offers nothing a person could approve.
  `blocked_screen_banners` reads that one pattern, whole, and answers the banner line; any
  other blocked pane stays a dialog.

No routing, no provider or model selection, no failover: those are P3.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .taxonomy import FailureClass, auto_resumable, classify_failure


class NormalizedFailure(StrEnum):
    """Why a role dispatch failed, in A5's words."""

    USAGE_LIMIT = "USAGE_LIMIT"
    RATE_LIMIT = "RATE_LIMIT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    NETWORK_TRANSIENT = "NETWORK_TRANSIENT"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    ACCOUNT_ACTION_REQUIRED = "ACCOUNT_ACTION_REQUIRED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    LOCAL_RUNTIME_UNAVAILABLE = "LOCAL_RUNTIME_UNAVAILABLE"
    #: Herdr's `agent_prompt_stalled`: a pane that exists and did not take the
    #: prompt, which a wait cures. A missing local runtime does not come back
    #: by waiting, so that is a different class.
    PROMPT_STALLED = "PROMPT_STALLED"
    #: The role did not end its turn within its time budget (Herdr's
    #: `timeout`). It may have been working or waiting behind a question; the
    #: pane is not inspected here. Separate from every provider class.
    ROLE_TIMEOUT = "ROLE_TIMEOUT"
    #: The taxonomy names a role-output, approval, budget, capability or state
    #: cause: not the machinery's.
    NOT_INFRASTRUCTURE = "NOT_INFRASTRUCTURE"
    #: The evidence names nothing. **Not fail-closed**: it is not in
    #: `INFRASTRUCTURE`, so the existing route stays.
    UNCLASSIFIED = "UNCLASSIFIED"


class RetryMode(StrEnum):
    AUTOMATIC = "AUTOMATIC"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


_N = NormalizedFailure

RETRY_MODE: dict[NormalizedFailure, RetryMode] = {
    _N.USAGE_LIMIT: RetryMode.AUTOMATIC,
    _N.RATE_LIMIT: RetryMode.AUTOMATIC,
    _N.PROVIDER_UNAVAILABLE: RetryMode.AUTOMATIC,
    _N.NETWORK_TRANSIENT: RetryMode.AUTOMATIC,
    _N.PROMPT_STALLED: RetryMode.AUTOMATIC,
    _N.ROLE_TIMEOUT: RetryMode.AUTOMATIC,
    _N.AUTH_EXPIRED: RetryMode.OPERATOR_REQUIRED,
    _N.ACCOUNT_ACTION_REQUIRED: RetryMode.OPERATOR_REQUIRED,
    _N.MODEL_UNAVAILABLE: RetryMode.OPERATOR_REQUIRED,
    _N.LOCAL_RUNTIME_UNAVAILABLE: RetryMode.OPERATOR_REQUIRED,
    _N.NOT_INFRASTRUCTURE: RetryMode.NOT_APPLICABLE,
    _N.UNCLASSIFIED: RetryMode.NOT_APPLICABLE,
}

#: The ten classes that are a wait or an operator action (A5's eight,
#: `PROMPT_STALLED`, `ROLE_TIMEOUT`).
INFRASTRUCTURE: frozenset[NormalizedFailure] = frozenset(
    c for c, mode in RETRY_MODE.items() if mode is not RetryMode.NOT_APPLICABLE
)

#: What telemetry's `failure_class` says for each. `auto_resumable` of the
#: taxonomy class equals "this class is AUTOMATIC" for every entry.
TAXONOMY: dict[NormalizedFailure, FailureClass] = {
    _N.USAGE_LIMIT: FailureClass.PROVIDER_QUOTA,
    _N.RATE_LIMIT: FailureClass.PROVIDER_RATE_LIMIT,
    _N.PROVIDER_UNAVAILABLE: FailureClass.PROVIDER_TRANSIENT,
    _N.NETWORK_TRANSIENT: FailureClass.PROVIDER_TRANSIENT,
    _N.ROLE_TIMEOUT: FailureClass.PROVIDER_TRANSIENT,
    _N.PROMPT_STALLED: FailureClass.INFRASTRUCTURE,
    _N.AUTH_EXPIRED: FailureClass.PROVIDER_AUTH,
    _N.ACCOUNT_ACTION_REQUIRED: FailureClass.PROVIDER_AUTH,
    _N.MODEL_UNAVAILABLE: FailureClass.UNKNOWN,
    _N.LOCAL_RUNTIME_UNAVAILABLE: FailureClass.UNKNOWN,
}

#: The `blocked_kind` of an automatic wait: the lower-cased class name.
AUTOMATIC_KINDS: frozenset[str] = frozenset(
    c.value.lower() for c, mode in RETRY_MODE.items() if mode is RetryMode.AUTOMATIC
)

#: How long an automatic class waits when the text names no time. A usage
#: limit reads its reset time (`quota.next_attempt_at`) instead.
_WAIT = {
    _N.RATE_LIMIT: timedelta(minutes=5),
    _N.PROVIDER_UNAVAILABLE: timedelta(minutes=5),
    _N.PROMPT_STALLED: timedelta(minutes=5),
    _N.NETWORK_TRANSIENT: timedelta(minutes=2),
    # Long enough for a role that was still working to end its turn.
    _N.ROLE_TIMEOUT: timedelta(minutes=15),
}


def is_infrastructure(failure: NormalizedFailure) -> bool:
    return failure in INFRASTRUCTURE


def is_automatic(failure: NormalizedFailure) -> bool:
    return RETRY_MODE[failure] is RetryMode.AUTOMATIC


def blocked_kind(failure: NormalizedFailure) -> str:
    """The `blocked_kind` an automatic class is parked under."""
    if not is_automatic(failure):
        raise ValueError(f"{failure.value} is not an automatic wait; it has no blocked_kind")
    return failure.value.lower()


def next_attempt(failure: NormalizedFailure, text: str,
                 *, now: datetime | None = None) -> datetime:
    """The earliest moment another attempt makes sense for an automatic class."""
    now = now or datetime.now(timezone.utc)
    if failure is _N.USAGE_LIMIT:
        from . import quota

        return quota.next_attempt_at(text, now=now)
    if failure not in _WAIT:
        raise ValueError(f"{failure.value} has no automatic wait")
    return now + _WAIT[failure]


# --------------------------------------------------------------------------- #
# Reading the evidence
# --------------------------------------------------------------------------- #

#: Precedence when several classes are found: operator-required first (an
#: automatic retry against an expired credential fails identically), then the
#: cause before its symptom (a timeout or a stall with a usage limit on screen
#: is the usage limit).
_PRECEDENCE: tuple[NormalizedFailure, ...] = (
    _N.AUTH_EXPIRED, _N.ACCOUNT_ACTION_REQUIRED, _N.MODEL_UNAVAILABLE,
    _N.LOCAL_RUNTIME_UNAVAILABLE, _N.USAGE_LIMIT, _N.RATE_LIMIT,
    _N.PROVIDER_UNAVAILABLE, _N.NETWORK_TRANSIENT, _N.ROLE_TIMEOUT,
    _N.PROMPT_STALLED,
)

_APOS = "['’]"

#: Phrases a harness prints. Matched at the start of a line (or after a
#: first-line handover word, `_FIRST_LINE_HANDOVER`), ending the statement
#: (`_TERMINAL`), never inside a longer word.
_PHRASES: dict[NormalizedFailure, tuple[str, ...]] = {
    _N.AUTH_EXPIRED: (
        r"invalid api key",
        r"(?:oauth )?token (?:has )?expired",
        r"authentication (?:failed|error|required)",
        r"please run /login",
        r"not logged in",
        r"credentials? (?:expired|rejected|invalid)",
    ),
    _N.ACCOUNT_ACTION_REQUIRED: (
        r"credit balance is too low",
        r"payment required",
        r"(?:your )?(?:account|organi[sz]ation) (?:has been |is )?"
        r"(?:disabled|suspended|deactivated)",
        r"verify your (?:account|email|phone)",
    ),
    _N.MODEL_UNAVAILABLE: (
        rf"there{_APOS}s an issue with the selected model",
        r"unknown model",
        r"invalid model",
        r"(?:the )?model \S+ (?:is )?(?:not found|not available|does not exist|unavailable)",
    ),
    _N.USAGE_LIMIT: (
        r"(?:claude ai )?usage limit reached",
        rf"you(?:{_APOS})?ve hit your (?:[\w-]+ ){{0,2}}limit",
        r"quota (?:exceeded|exhausted)",
        r"kontingent (?:erschoepft|aufgebraucht)",
    ),
    _N.RATE_LIMIT: (
        r"rate[ _-]limit(?:ed|_error)?",
        r"too many requests",
    ),
    _N.PROVIDER_UNAVAILABLE: (
        r"overloaded(?:_error)?",
        r"service unavailable",
        r"bad gateway",
        r"internal server error",
    ),
    _N.NETWORK_TRANSIENT: (
        r"connection (?:error|reset|refused|closed)",
        r"network (?:error|is unreachable)",
        r"econnreset",
        r"econnrefused",
        r"etimedout",
        r"eai_again",
        r"socket hang up",
        r"fetch failed",
        r"request timed out",
        r"getaddrinfo",
    ),
}

_NOT_IN_WORD = r"(?![A-Za-z0-9_])"
#: What may follow a phrase: the end of a harness banner. The end of the line
#: (with or without a final full stop or `!`), or a banner's metadata trail
#: (`· continuing automatically ...`, `| 1759600000`, `(x)`, `— ...`, `...`).
#: **Not** `:` `;` `,` or a full stop with more text after it: those continue a
#: sentence, and a role's prose does (`Rate limit: cap at 100/min`, `Quota
#: exceeded, see the table`).
_TERMINAL = r"(?:\s*[.!]?\s*$|\s*[(|·—…])"

_PHRASE_RES: tuple[tuple[NormalizedFailure, re.Pattern], ...] = tuple(
    (cls, re.compile(r"(?:" + "|".join(alts) + r")" + _NOT_IN_WORD + _TERMINAL, re.I))
    for cls, alts in _PHRASES.items()
)

#: Leading whitespace and non-ASCII glyphs (`●`, `⎿`, `⚠`, ...).
_LEAD = re.compile(r"^[\s\u0080-\U0010ffff]+")
_API_ERROR_LEAD = re.compile(r"^api error:?\s*", re.I)
#: A status counts only on a line that *starts* with `API Error` (after the
#: leading glyphs and the registered first-line prefixes): a line that merely
#: contains the words (`the API Error: 500 handler is missing`) says nothing.
_API_STATUS = re.compile(r"^api error:?\s+(?:status\s+)?(\d{3})\b", re.I)
_STATUS_CLASS = {
    401: _N.AUTH_EXPIRED, 403: _N.AUTH_EXPIRED,
    402: _N.ACCOUNT_ACTION_REQUIRED,
    404: _N.MODEL_UNAVAILABLE,
    429: _N.RATE_LIMIT,
    500: _N.PROVIDER_UNAVAILABLE, 502: _N.PROVIDER_UNAVAILABLE,
    503: _N.PROVIDER_UNAVAILABLE, 504: _N.PROVIDER_UNAVAILABLE,
    529: _N.PROVIDER_UNAVAILABLE,
}

#: This project's own sentences and Herdr's error codes, read **only where the
#: project itself writes them**: at the start of the failure's first line or
#: right after the wrapper words (`_FIRST_LINE_HANDOVER`), never in the middle
#: of a line. A role's `timeout 10m` or `timed out` on screen says nothing about
#: the dispatch, and neither does a first line of its own that mentions
#: `"code":"timeout"` or `herdr cli not on the path`.
_ERR = r'\{"error"\s*:\s*\{"code"\s*:\s*'
_FIRST_LINE_ONLY: tuple[tuple[NormalizedFailure, re.Pattern], ...] = (
    (_N.PROMPT_STALLED, re.compile(_ERR + r'"agent_prompt_stalled"')),
    (_N.ROLE_TIMEOUT, re.compile(_ERR + r'"timeout"')),
    (_N.ROLE_TIMEOUT, re.compile(
        r"(?:planner|developer|qa) on \S+ gave no answer after \d+", re.I)),
    (_N.ROLE_TIMEOUT, re.compile(
        r"Command .{1,200}? timed out after \d+(?:\.\d+)? seconds", re.I)),
    (_N.LOCAL_RUNTIME_UNAVAILABLE, re.compile(
        r"herdr cli not on the path|herdr is not available|herdr returned no pane id"
        r"|\S+ could not be started|agent \S+ could not be started after"
        r"|agent \S+ reports no pane|[^:]{0,80}\bcommand not found", re.I)),
)

_PANE_OPEN = "--- Visible in pane ---"
#: Exactly what `HerdrDispatcher._pane_view` writes. A pane line of ten dashes
#: (a role's own rule) does not end the block.
_PANE_CLOSE = "-----------------------"

#: The words with which this project's own wrappers hand a provider's text on
#: in the failure's first line: `<purpose> failed: <herdr's text>` and
#: `<harness> exited with <n> and without output: <stderr>`. **Anchored at the
#: start of the line, and the purpose holds no colon**: provider text is read
#: right after the wrapper, never after a `failed:` or any `": "` further in,
#: which is where a role's own words
#: (`qa reported: the test failed: quota exceeded`) sit.
_FIRST_LINE_HANDOVER = re.compile(
    r"^\s*(?:[A-Za-z][\w .\-/]{0,79}? failed|[\w.\-]+ exited with \d+ and without output):\s+",
    re.I)


def _pane_lines(lines: list[str]) -> list[str]:
    """The lines of every `--- Visible in pane ---` ... `-----` block."""
    out: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside:
            inside = stripped == _PANE_OPEN
        elif stripped == _PANE_CLOSE:
            inside = False
        else:
            out.append(line)
    return out


def _statements(line: str, *, handover: bool) -> list[str]:
    """The pieces of a line a harness banner could start: the line itself (its
    leading glyphs stripped), what follows the wrapper words at its start (first
    line only), and, for each, what follows a leading `API Error:`."""
    pieces = [_LEAD.sub("", line)]
    if handover:
        m = _FIRST_LINE_HANDOVER.match(line)
        if m:
            pieces.append(_LEAD.sub("", line[m.end():]))
    for piece in list(pieces):
        lead = _API_ERROR_LEAD.match(piece)
        if lead:
            pieces.append(piece[lead.end():])
    return pieces


def _read_line(line: str, *, handover: bool) -> set[NormalizedFailure]:
    found: set[NormalizedFailure] = set()
    for piece in _statements(line, handover=handover):
        for cls, pattern in _PHRASE_RES:
            if pattern.match(piece):
                found.add(cls)
        m = _API_STATUS.match(piece)
        if m and int(m.group(1)) in _STATUS_CLASS:
            found.add(_STATUS_CLASS[int(m.group(1))])
        if handover:
            for cls, pattern in _FIRST_LINE_ONLY:
                if pattern.match(piece):
                    found.add(cls)
    return found


def classify_text(text: str, *, first_line: bool = True) -> NormalizedFailure | None:
    """The class the text names, or None.

    Reads the failure's **first line** and the lines of the pane block
    `HerdrDispatcher._pane_view` appends, and nothing else. With
    ``first_line=False`` there is no failure sentence: `text` is a bare pane
    view (its pane block if it has the markers, else every line) and only the
    pane rule applies.

    **Provider text, not role text.** A pane is a role's output as much as a
    harness's, and a failure's first line can carry a role's words. A phrase
    counts only where a harness would print it: at the start of a line (after
    leading glyphs and, in the first line, after `... failed:` or `... exited
    with <n> and without output:`), and only when it ends the statement
    (`_TERMINAL`). Anything else -- a phrase after another word, after a
    `": "` that is not a registered handover, followed by a clause -- is
    UNCLASSIFIED: the existing route stays, and **this classifier** parks nothing
    on a sentence a role wrote. (The existing route no longer parks one either:
    the CLI's generic abort handler and the controller's seal and binding
    blocks pass ``scan_for_quota=False`` to `stages.block`.) **Residual, pinned
    by tests, exactly:** on a pane line of a failed turn -- a developer's or a
    planner's as much as a harness's -- any phrase of the list at the start of
    the line that ends the statement is provider text, whoever wrote it:
    `quota exceeded`, `Too many requests`, `rate limited`, `Rate limit.`,
    `Kontingent erschoepft`, `Connection error.`, `Credit balance is too low`,
    `Usage limit reached · resets 5pm`, `API Error: 5xx <prose>`, and so on; and
    a first line whose wrapper is followed by exactly such a phrase, or whose
    purpose is any colon-free `<words> failed`. Bounded by the resume attempts.
    """
    lines = (text or "").splitlines()
    first = lines[0] if lines else ""
    found: set[NormalizedFailure] = set()

    if first_line:
        found |= _read_line(first, handover=True)
        pane = _pane_lines(lines[1:])
    else:
        pane = _pane_lines(lines) if any(
            ln.strip() == _PANE_OPEN for ln in lines) else lines

    for line in pane:
        found |= _read_line(line, handover=False)

    for cls in _PRECEDENCE:
        if cls in found:
            return cls
    return None


# --------------------------------------------------------------------------- #
# The final screen of a turn that ended normally (P2-43d)
# --------------------------------------------------------------------------- #

#: How many of a pane's last non-blank lines are "the final screen". A harness
#: that stops a turn on a limit prints the banner into the scrollback *and*
#: keeps it in its status area at the bottom. In the 771 recorded pane blocks
#: that carry the status-area banner (`⚠ Usage limit reached · limit resets
#: 3:20am`) it is the 3rd line from the end (725 blocks) or the 4th (46), and the
#: `⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)` line,
#: which names the zone of the reset, the 9th, 8th or 12th: twelve is the
#: smallest window that reaches all of them. A role's own words are above that
#: area, and only the last lines of a finished turn are the screen it ended on.
FINAL_SCREEN_LINES = 12

#: The two classes A5 clause 2 names for every role. Other provider classes on a
#: finished developer turn are a tracked gap, not read here.
_LIMIT_CLASSES = (_N.USAGE_LIMIT, _N.RATE_LIMIT)

#: What starts a recorded banner line: at most four columns of indent (spaces or
#: the non-breaking space the harness prints), one of the three glyphs of the
#: recorded banners -- `●` the inline banner, `⎿` its second line, `⚠` the status
#: area -- and a space. Nothing else leads a banner: a bullet, a box edge, a code
#: block's indent or a role's sentence does not (the reader of a failed turn
#: strips every non-ASCII glyph, `_LEAD`; this one must not).
_BANNER_LEAD = r"[ \u00a0]{0,4}[\u25cf\u23bf\u26a0][ \u00a0]*"
_CLOCK = r"\d{1,2}(?::\d{2})?(?:am|pm)"
_ZONE = r"\([A-Za-z][A-Za-z_]*(?:/[A-Za-z0-9_+\-]+)+\)"
#: A weekly limit names the day it resets, not only the hour: `resets Oct 18, 12pm
#: (Europe/Istanbul)` (recorded 2026-10-15, P3-08 slice 4). The month is one of the
#: twelve names, whole or in its three-letter form, so that `resets soon` and
#: `resets Octopus 5, 5pm` stay sentences.
_DATE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
         r"|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
         r"\.? \d{1,2}(?:st|nd|rd|th)?,?")
#: The provider's own metadata after the phrase, a closed set and never a free
#: sentence: `continuing automatically at 3:20am`, `esc to cancel`, `resets
#: 3:20am (Europe/Istanbul)`, `limit resets 3:20am`, `resets Oct 18, 12pm
#: (Europe/Istanbul)`, `clau.de/wrap-up`, each also as far as a narrow pane
#: wrapped it (`continuing`, `esc to`, `resets`).
_SEGMENT = (
    r"(?:continuing(?: automatically(?: at " + _CLOCK + r")?)?"
    r"|esc(?: to(?: cancel)?)?"
    r"|(?:limit )?resets(?: (?:" + _DATE + r"(?: " + _CLOCK + r"(?: " + _ZONE + r")?)?"
    r"|" + _CLOCK + r"(?: " + _ZONE + r")?))?"
    r"|clau\.de/[\w\-/]+)"
)
#: The whole line: lead, one of the two recorded phrases, then one or more
#: ` · <segment>` and nothing after (a role's `Usage limit reached · resets 5pm,
#: so ...` is a sentence, not a banner).
_USAGE_BANNER = re.compile(
    r"^" + _BANNER_LEAD
    + r"(?:usage limit reached|you['\u2019]?ve hit your (?:[\w-]+ ){0,2}limit)"
    + r"(?:\s*\u00b7\s*" + _SEGMENT + r")+\s*$", re.I)
#: The structured error body Claude Code prints for an HTTP error (the shape
#: pinned for a 529 in `tests/test_failure_classes.py`: `API Error: 529
#: {"type":"error",...}`), here for a 429. Not recorded for a 429 itself.
_RATE_BANNER = re.compile(
    r"^" + _BANNER_LEAD + r'API Error:\s*429\s+\{\s*"type"\s*:\s*"error"', re.I)


def _pane_block(lines: list[str]) -> list[str]:
    """The lines between the first `--- Visible in pane ---` and the **last**
    closing rule: a role's own line of the same twenty-three dashes inside the
    block is content, not the end of it."""
    start = next(i for i, ln in enumerate(lines) if ln.strip() == _PANE_OPEN) + 1
    ends = [i for i in range(start, len(lines)) if lines[i].strip() == _PANE_CLOSE]
    return lines[start:ends[-1]] if ends else lines[start:]


def _screen_lines(view: str | None) -> list[str]:
    """The non-blank lines of a pane view's `--- Visible in pane ---` block (the
    whole text if it has no markers)."""
    lines = (view or "").splitlines()
    if any(ln.strip() == _PANE_OPEN for ln in lines):
        lines = _pane_block(lines)
    return [ln for ln in lines if ln.strip()]


def final_screen(view: str) -> list[str]:
    """The final screen of a pane view: the last `FINAL_SCREEN_LINES` non-blank
    lines of its `--- Visible in pane ---` block (the whole text if it has no
    markers)."""
    return _screen_lines(view)[-FINAL_SCREEN_LINES:]


def _limit_banner(line: str) -> NormalizedFailure | None:
    if _USAGE_BANNER.match(line):
        return _N.USAGE_LIMIT
    if _RATE_BANNER.match(line):
        return _N.RATE_LIMIT
    return None


def _banner_key(line: str) -> str:
    """A banner line by its words: the indent a pane view strips from its first
    line, and the spacing a redraw may change, are not part of it."""
    return " ".join(line.split())


def _status_line(line: str) -> bool:
    """A banner line of the harness's status area (`⚠ Usage limit reached ...`).
    The harness draws it at the bottom of the screen only while a limit holds, so
    it says something about the pane now and never about an earlier turn."""
    return _banner_key(line).startswith("\u26a0")


def final_screen_banners(view: str, *, before: str | None = None) -> list[str]:
    """The lines of the final screen that `final_screen_limit` read as a limit
    banner, in order and nothing else. This is what a failed dispatch carries as
    its evidence: the lines the decision was made on, so that no other word of a
    role on the screen can change the class the rest of the product reads off the
    message, or the reset time it takes from it.

    `before` is the pane view read just before the prompt was sent. A banner the
    pane already showed then is the last limit's, not this turn's (a pane that
    outlived the wait keeps it in its scrollback, and a short good turn leaves it
    among the last lines): every banner line of the **whole** `before` block is
    spent once against an equal line of the final screen, oldest first, and only
    what is left is read. Counted, not positioned: a banner shown more often than
    before is new. The status-area line (`⚠`) is never spent: it is drawn only
    while a limit holds, so a pane that still shows it when the prompt is sent is
    still limited and is not a healed pane that kept an old banner. Without
    `before` (None, or a view with no banner) every banner counts, as in P2-43d."""
    spent = Counter(_banner_key(ln) for ln in _screen_lines(before)
                    if _limit_banner(ln) is not None and not _status_line(ln))
    fresh = []
    for ln in final_screen(view):
        if _limit_banner(ln) is None:
            continue
        key = _banner_key(ln)
        if spent[key] > 0:
            spent[key] -= 1
            continue
        fresh.append(ln)
    return fresh


def final_screen_limit(view: str, *, before: str | None = None) -> NormalizedFailure | None:
    """`USAGE_LIMIT` or `RATE_LIMIT` when the **final screen** of a pane whose
    turn ended normally shows a provider's limit banner, else None.

    Stricter than `classify_text`, on purpose. That reads the screen of a turn
    that already *failed*, where a role-written `Rate limit.` costs a bounded
    wait; this one decides about a turn that *finished*, where a wrong answer
    parks good work (a missed banner only sends the candidate to QA, as before).
    A line counts only when (1) it is among the last `FINAL_SCREEN_LINES`
    non-blank lines and (2) it **is** one of the recorded banner shapes, whole:
    a recorded glyph, the phrase `usage limit reached` or `you've hit your ...
    limit`, and only the provider's own ` · <metadata>` segments after it
    (`_USAGE_BANNER`); or it opens with `API Error: 429 {"type":"error"` (the
    structured body of a rate limit). Prose that merely starts with the same
    words (`Rate limit (60 rpm) added`, `Quota exceeded -- now raises`, `Rate
    limited... retried`), a bare phrase, a bullet, a code block's indent, a
    quotation and everything above the final screen say nothing. Residual, pinned
    by tests: a role's own line that is *exactly* a banner (`● Usage limit
    reached · resets 5pm`) among the last lines is read as the banner, and so is
    a banner that stays in view after the turn went on by itself during the turn.
    A banner the pane already showed before the prompt (`before`, see
    `final_screen_banners`) is none of these: it says nothing about this turn.
    """
    found = {cls for line in final_screen_banners(view, before=before)
             if (cls := _limit_banner(line)) is not None}
    return next((cls for cls in _LIMIT_CLASSES if cls in found), None)


# --------------------------------------------------------------------------- #
# A blocked pane that shows the limit's own menu (P3-08 slice 4, T-UL1)
# --------------------------------------------------------------------------- #

#: What Claude Code draws between a limit banner and the menu it opens
#: (`/rate-limit-options`), recorded on the planner and QA panes of run mgrb2
#: (2026-10-15 02:14Z and 02:15Z): the turn's closing line, the command it echoed,
#: the rule above the menu, and the reset hint of a session limit. A closed set;
#: any other line between the banner and the menu is somebody's words.
_BETWEEN = (
    re.compile(r"^\u273b \S+ for \d[\dhms ]*(?: \u00b7 .+)?$"),
    re.compile(r"^\u276f /rate-limit-options$"),
    re.compile(r"^[\u2500\u2501]{8,}$"),
    re.compile(r"^Use your limit reset to reset it now:?(?: clau\.de/reset)?$", re.I),
    re.compile(r"^clau\.de/[\w\-/]+$", re.I),
)
#: The menu itself: its question, its three options (the pointer `\u276f` marks the
#: chosen one), and the hint under it. `Stop and wait for limit to reset` is what
#: makes a menu the limit's: no approval dialog offers it.
_MENU_QUESTION = re.compile(r"^What do you want to do\?$")
_MENU_STOP = "Stop and wait for limit to reset"
_MENU_OPTION = re.compile(
    r"^(?:\u276f )?\d\. (?:" + re.escape(_MENU_STOP)
    + r"|Wait here, then continue automatically(?: at .+)?|Switch to usage credits)$")
_MENU_HINT = re.compile(r"^Enter to confirm \u00b7 Esc to cancel$")


def blocked_screen_banners(view: str) -> list[str]:
    """The limit banner of a pane whose agent Herdr reports as `blocked` when the
    dialog it waits in is the limit's own menu -- else an empty list.

    Herdr calls a pane that waits for a keypress `blocked`, whatever it waits for.
    A permission dialog is a person's to answer; the menu Claude Code opens after
    `You've hit your weekly limit` (`1. Stop and wait for limit to reset`) is not a
    question at all: the provider has said no until a time it names, and no answer
    changes that. Told apart by the screen, and only by a screen that is the whole
    pattern, in this order (the last `FINAL_SCREEN_LINES` non-blank lines):

    * the last `What do you want to do?` line, with only the recorded options and
      the hint below it, among them `Stop and wait for limit to reset`;
    * above it, going up through the closed set of `_BETWEEN` (closing line,
      echoed command, rule, reset hint) and nothing else, a limit banner
      (`_limit_banner`).

    Anything else -- a banner with a permission dialog under it, a stale banner
    above work the role did, a menu without the banner in view, an option nobody
    recorded, a line of a role between banner and menu -- is `[]`, and the pane
    stays what it was: a dialog a person looks at. The result is the banner line
    and nothing else of the screen, so no word of a role can change the class the
    rest of the product reads off the error, or the reset time it takes from it.
    Residual, pinned by tests: a role whose own output spells the whole pattern
    (banner, closing line, rule, question, the three options) as the last lines of a
    pane that waits for a key is read as the menu; the park it earns is bounded by
    the stated reset and the resume attempts.
    """
    screen = final_screen(view)
    keys = [_banner_key(ln) for ln in screen]
    asks = [i for i, k in enumerate(keys) if _MENU_QUESTION.match(k)]
    if not asks:
        return []
    ask = asks[-1]
    menu = keys[ask + 1:]
    if not all(_MENU_OPTION.match(k) or _MENU_HINT.match(k) for k in menu):
        return []
    if not any(k.endswith(_MENU_STOP) for k in menu):
        return []
    for i in range(ask - 1, -1, -1):
        if _limit_banner(screen[i]) is not None:
            return [screen[i]]
        if not any(pattern.match(keys[i]) for pattern in _BETWEEN):
            return []
    return []


#: This project's own sentences in a failure's first line. Checked before any
#: phrase search: the output-contract failure is a plain `DispatchError` whose
#: first line embeds the role's own text after the marker, e.g. `...the repair
#: budget is exhausted: quota exceeded`.
_OWN_SENTENCES = (
    "violates the output contract", "dispatch refused",
    "dispatch budget exhausted", "capability violation",
    "waits for an approval", "needs approval",
)

#: By exception type name in the MRO, as `taxonomy.classify_failure` does; the
#: types are this project's own, so nothing is imported. `PaneNotClean` and its
#: subclass `RoleNotQuiescent` are the controller's: a pane that is not provably
#: clean keeps its own route -- tabs open, a person looks -- whatever its text
#: shows.
_BY_TYPE = {
    "WaitingForApproval": _N.NOT_INFRASTRUCTURE,
    "CapabilityViolation": _N.NOT_INFRASTRUCTURE,
    "RoleOutputError": _N.NOT_INFRASTRUCTURE,
    "StoreError": _N.NOT_INFRASTRUCTURE,
    "FreezeError": _N.NOT_INFRASTRUCTURE,
    "StorageParked": _N.NOT_INFRASTRUCTURE,
    "PaneNotClean": _N.NOT_INFRASTRUCTURE,
    "HerdrUnavailable": _N.LOCAL_RUNTIME_UNAVAILABLE,
}

_NOT_INFRA_TAXONOMY = frozenset({
    FailureClass.CONTRACT_INVALID, FailureClass.NEEDS_APPROVAL,
    FailureClass.CAPABILITY_VIOLATION, FailureClass.BUDGET_EXHAUSTED,
    FailureClass.CORRUPT_STATE, FailureClass.ROLE_FAILED,
})


def classify(exc: BaseException | None, *, detail: str = "") -> NormalizedFailure:
    """The class of a failed dispatch. Never None."""
    text = detail or str(exc or "")
    first = (text.strip().splitlines() or [""])[0].lower()
    if any(s in first for s in _OWN_SENTENCES):
        return _N.NOT_INFRASTRUCTURE
    for k in type(exc).__mro__ if exc is not None else ():
        by_type = _BY_TYPE.get(k.__name__)
        if by_type is not None:
            return by_type
    found = classify_text(text)
    if found is not None:
        return found
    if classify_failure(exc, detail=detail) in _NOT_INFRA_TAXONOMY:
        return _N.NOT_INFRASTRUCTURE
    return _N.UNCLASSIFIED


# The parity the controller and telemetry rely on, checked where it is defined
# rather than hoped for.
assert set(TAXONOMY) == INFRASTRUCTURE
assert all(auto_resumable(TAXONOMY[c]) == is_automatic(c) for c in INFRASTRUCTURE)
