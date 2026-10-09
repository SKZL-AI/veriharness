"""One normalised class for every failed role dispatch (P2-43, amendment A5).

Until this file existed a failed dispatch was whatever its text happened to
say. QA's `DispatchError` became an outage string and then, one step later, a
rejected candidate: a provider's usage limit spent an iteration, eight times
between 2026-10-02 and 2026-10-07. The same limit in the planner was found by
a phrase search over the whole message, pane included, and cost an iteration
on resume. A prompt stall and a role that outlived its time budget were
routed to the digest as decisions nobody had to take.

This module is the vocabulary and the classifier, nothing else. It decides
*what a failure was*; the controller decides what to do about it. Three
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

No routing, no provider or model selection, no failover: those are P3.
"""

from __future__ import annotations

import re
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
        rf"you(?:{_APOS})?ve hit your (?:\w+ )?limit",
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
