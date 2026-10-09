"""P2-43d: a developer turn that ends on a provider's limit screen is a failed
dispatch -- never a finished turn whose candidate then goes to QA.

Amendment A5, clause 2: a usage or rate limit in any role is never a candidate
failure. For the planner and QA a limit ends the turn without the answer file
and was made a wait by P2-43a. The developer answers through the artifact in the
tree, and Herdr reports a pane that ran into the limit as an ordinary end of
turn, so `HerdrDispatcher.dispatch` returned `developer run finished (status
idle)`, the controller froze the half-done candidate, and QA could reject it.

These tests drive the real `HerdrDispatcher.dispatch` and the real controller
and resume loop on fakes: `subprocess.run` inside `hoh.dispatchers` and
`hoh.herdr.read_agent` are fake, so no Herdr and no agent is ever started and
nothing is written outside `tmp_path`.

The banner lines are recorded: the QA and planner panes of run ugt
(2026-10-04 21:16Z and 21:17Z) end exactly like `LIMIT_BOTTOM`, and the same
banner forms recur in every other recorded limit pane (the paths of the run are
removed). The reader accepts those shapes, whole, and nothing else: a developer's
own sentence that starts with the same words (`Rate limit (60 rpm) added ...`)
is not a banner. No developer pane was ever recorded on a limit; the screen it
ends on is the screen any role ends on, because the footer is the harness's. The
German phrase of the provider list is classifier *input* in a `parametrize`
decorator, never an expectation (`tests/test_source_hygiene.py`).

Mutant ids in a docstring (`D..`) are the spec's table of mutations the
integration review applies; each test names the ones it kills (a test that names
none is a negative control or pins a stated limit).

P2-43e (the follow-up of the integration finding F1 of P2-43d): a pane that
outlived a limit wait keeps the old banner in its scrollback, and a short good
re-run leaves it among the last twelve lines. The dispatcher therefore reads the
pane once more, *before* the prompt (the baseline), and a banner the pane already
showed then is not this turn's. The fake pane below models that: `before` is what
the prompt finds, `view` is what the turn leaves (a fake that shows the same screen
before and after models a banner that was there already). Mutant ids `E..` are
the spec's second table.
"""

from __future__ import annotations

import json
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import dispatchers, dispatchrecovery, failures, herdr, quota, resumeloop
from hoh.contracts import Budgets, Condition, Role, RunState, Stage
from hoh.controller import Controller, DispatchError, WaitingForApproval
from hoh.dispatchers import HarnessDispatcher, HerdrDispatcher, _Base
from hoh.failures import NormalizedFailure as NF
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore

TARGET = "w0:p1"
ANSWER = '{"ok": true}'
RULE = "─" * 88

# --------------------------------------------------------------------------- #
# Panes: what `herdr agent read` shows at the end of a turn
# --------------------------------------------------------------------------- #

#: Recorded (QA pane of run ugt, 2026-10-04 21:16Z): the bottom of Claude Code's
#: screen after a turn that ran into the session limit. The first two lines are
#: the scrollback, the last three the harness's status area.
LIMIT_BOTTOM = [
    "● Usage limit reached again · continuing automatically at 3:20am · esc to cancel",
    "  ⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)",
    "     Use your limit reset to reset it now: clau.de/reset",
    "✻ Worked for 0s · done 12:17 AM",
    RULE,
    "❯ ",
    RULE,
    "  ⚠ Usage limit reached · limit resets 3:20am",
    "    Continuing automatically at 3:20am · esc to cancel",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent",
]
#: Recorded (planner pane of run ugt, 2026-10-04 21:17Z): the same, with a shell
#: still running and a longer status line.
LIMIT_BOTTOM_PLANNER = [
    "● Usage limit reached again · continuing automatically at 3:20am · esc to cancel",
    "  ⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)",
    "     Use your limit reset to reset it now: clau.de/reset",
    "✻ Sautéed for 0s · done 12:18 AM · 1 shell still running",
    RULE,
    "❯ ",
    RULE,
    "  ⚠ Usage limit reached · limit resets 3:20am",
    "    Continuing automatically at 3:20am · esc to cancel",
    "  ⏵⏵ auto mode on · 1 shell · ← 1 agent · ↓ to manage",
]
#: Recorded (run wmig, 2026-10-04, a 48-column pane): the same screen wrapped, so the
#: session-limit line is the twelfth from the end and its zone sits on the next line.
RULE48 = "─" * 48
WRAPPED_BOTTOM = [
    "  ⎿  You've hit your session limit · resets",
    "     5:20pm (Europe/Istanbul)",
    "     Use your limit reset to reset it now:",
    "     clau.de/reset",
    "✻ Cooked for 0s · done 1:24 PM",
    RULE48,
    "❯ ",
    RULE48,
    "  ⚠ Usage limit reached · limit resets 5:20pm",
    "    Continuing automatically at 5:20pm · esc",
    "    to cancel",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 …",
]
#: The banner Claude Code prints inline when the limit stops a turn (recorded
#: 320 times in the evidence of one night, 8:20am reset).
INLINE_BANNER = ("● Usage limit reached · continuing automatically at 3:20am "
                 "· esc to cancel")
#: A finished turn: the role's closing message, then the harness's status area.
NORMAL_BOTTOM = [
    "● Done. I created done.txt and checked it against the acceptance criterion.",
    "✻ Baked for 2m 3s",
    RULE,
    "❯ ",
    RULE,
    "  ⏵⏵ auto mode on (shift+tab to cycle)",
]
CLOSE_TAIL = NORMAL_BOTTOM[1:]
#: The pane of a turn that finished normally, never having met a limit: what the
#: prompt finds unless a test says otherwise.
IDLE_PANE = "\n".join(NORMAL_BOTTOM) + "\n"
#: What a pane keeps of an old limit once it has passed (integration review
#: p243dint-a, F1): the lines of the turn that ran into it, in its scrollback,
#: without the status area. A turn that follows leaves them among its last lines.
STALE = LIMIT_BOTTOM[:3] + ["✻ Worked for 0s · done 12:17 AM"]
#: The same with the inline banner of the other recorded form.
STALE_INLINE = [INLINE_BANNER, *STALE[1:]]
#: The limit of the next day: the same screen, another reset time. Behind the old
#: banner lines (`STALE` + `FRESH_BOTTOM[1:]`: thirteen lines) both session-limit
#: lines are among the last twelve.
FRESH_BOTTOM = [ln.replace("3:20am", "8:20am") for ln in LIMIT_BOTTOM]
#: The old session-limit line as a redraw may print it (still the whole line of the
#: recorded shape): two plain spaces after the glyph instead of a space and a
#: non-breaking one, and non-breaking spaces around the dot.
STALE_RESPACED = [
    STALE[0],
    "  \u23bf  You've hit your session limit\u00a0\u00b7\u00a0resets 3:20am (Europe/Istanbul)",
    *STALE[2:],
]


def screen(*lines: str, scrollback: int = 0) -> str:
    """A pane as `herdr agent read` writes it: some earlier output, then `lines`."""
    older = [f"older output {n}" for n in range(scrollback)]
    return "\n".join([*older, *lines]) + "\n"


def with_closing(*closing: str) -> str:
    """A finished turn whose closing message holds `closing`."""
    return screen(*closing, *CLOSE_TAIL)


def short_rerun(body: int, *, stale=STALE) -> str:
    """A good turn of `body` lines (its prompt, steps, a closing line) on a pane
    that kept `stale` in its scrollback. With three lines or fewer the old banner
    is still among the last twelve non-blank lines (the finding F1)."""
    lines = (["❯ Read the prompt file and do what it says"]
             + [f"● step {n}" for n in range(body - 2)] + ["● Done. done.txt exists."])
    return screen(*stale, *lines, *CLOSE_TAIL)


def view_of(text: str) -> str:
    """What `_pane_view` builds from it, for the unit tests of the reader."""
    body = "\n".join(ln for ln in text.splitlines() if ln.strip())[-1200:]
    return f"--- Visible in pane ---\n{body}\n-----------------------"


# --------------------------------------------------------------------------- #
# U -- the reader of a final screen
# --------------------------------------------------------------------------- #

API_429 = '⎿  API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'

READ = [
    ("recorded-qa-pane", screen(*LIMIT_BOTTOM), NF.USAGE_LIMIT),
    ("recorded-planner-pane", screen(*LIMIT_BOTTOM_PLANNER), NF.USAGE_LIMIT),
    ("recorded-wrapped-pane", screen(*WRAPPED_BOTTOM), NF.USAGE_LIMIT),
    ("status-area-alone", screen(*LIMIT_BOTTOM[-3:]), NF.USAGE_LIMIT),
    ("session-limit-line-alone", screen(LIMIT_BOTTOM[1]), NF.USAGE_LIMIT),
    ("continuing-automatically-line-alone", screen(INLINE_BANNER), NF.USAGE_LIMIT),
    ("wrap-up-segment",
     screen("  ⚠ Usage limit reached · limit resets 10:20pm · clau.de/wrap-up"),
     NF.USAGE_LIMIT),
    ("wrapped-after-continuing",
     screen("● Usage limit reached · continuing"), NF.USAGE_LIMIT),
    ("wrapped-after-esc",
     screen("● Usage limit reached · continuing automatically at 5:20pm · esc to"),
     NF.USAGE_LIMIT),
    ("wrapped-after-resets",
     screen("  ⎿  You've hit your session limit · resets"), NF.USAGE_LIMIT),
    ("weekly-limit-wording",
     screen("  ⎿  You've hit your weekly limit · resets 9am (Europe/Istanbul)"), NF.USAGE_LIMIT),
    ("api-error-429-body", screen(API_429), NF.RATE_LIMIT),
    ("usage-beats-rate", screen(API_429, INLINE_BANNER), NF.USAGE_LIMIT),
]


@pytest.mark.parametrize("text,expected", [c[1:] for c in READ], ids=[c[0] for c in READ])
def test_the_recorded_limit_screens_are_read(text, expected):
    """Kills D1 (the reader never finds a banner), D2 (usage limit only: the
    rate-limit row)."""
    assert failures.final_screen_limit(text) is expected
    assert failures.final_screen_limit(view_of(text)) is expected


@pytest.mark.parametrize("text,expected", [c[1:] for c in READ], ids=[c[0] for c in READ])
def test_every_banner_the_reader_accepts_is_read_by_the_classifier(text, expected):
    """The error carries only the lines the reader accepted, and the controller
    classifies it with the existing `failures.classify`: a shape the reader took
    that the classifier does not know would end as UNCLASSIFIED and fall to the
    generic block. So the class of the error is the class of the decision."""
    view = view_of(text)
    carried = "\n".join(failures.final_screen_banners(view))
    exc = DispatchError("developer ended its turn on a provider's limit screen; it is not "
                        f"counted as a finished turn\n--- Visible in pane ---\n{carried}\n"
                        "-----------------------")
    assert failures.classify(exc) is expected


@pytest.mark.parametrize("banner", ["Kontingent erschoepft | 1759600000",
                                    "● Kontingent erschoepft · resets 3:20am"],
                         ids=["bare-with-pipe", "with-glyph-and-segment"])
def test_a_german_phrase_of_the_provider_list_has_no_recorded_banner_shape(banner):
    """The phrase is in the provider list on purpose (a provider CLI may print
    it) and is classifier *input* here, passed through a decorator; the
    expectation is None, an English value. No banner shape of it was ever
    recorded, so the reader of a finished turn does not read it (L1). Kills D4."""
    assert failures.final_screen_limit(screen(banner)) is None


NOT_A_LIMIT = [
    ("normal-finish", screen(*NORMAL_BOTTOM)),
    ("empty-pane", "(empty)\n"),
    ("nothing", ""),
    ("prose-rate-limiting", with_closing("● Rate limiting is not implemented in app.py")),
    ("prose-usage-limit-sentence",
     with_closing("● Usage limit reached handling is missing in the client")),
    ("prose-with-colon", with_closing("● Rate limit: cap at 100/min")),
    ("prose-with-comma", with_closing("Quota exceeded, see the table below")),
    ("prose-with-clause", with_closing("Usage limit reached, the build is red")),
    # A developer's own sentences that start with a phrase and go on with what the
    # old trail `(|·—…` accepted (spec review p243dspec-a, B1).
    ("prose-rate-limit-parenthesis",
     with_closing("● Rate limit (60 rpm) added to the API client and tested.")),
    ("prose-rate-limit-dash", with_closing("● Rate limit — token bucket implemented")),
    ("prose-too-many-requests-http",
     with_closing("  Too many requests (HTTP 429) is now returned")),
    ("prose-quota-dash", with_closing("  Quota exceeded — now raises QuotaError")),
    ("prose-rate-limited-ellipsis", with_closing("  Rate limited… retried 3 times")),
    ("prose-rate-limit-pipe", with_closing("  Rate limit | window | cap")),
    ("prose-usage-dash", with_closing("● Usage limit reached — try later")),
    ("bare-rate-limited-ellipsis", with_closing("Rate limited…")),
    ("bare-banner-phrase", with_closing("● Usage limit reached")),
    ("bare-rate-limit", with_closing("Rate limit.")),
    ("bare-quota-exceeded", with_closing("quota exceeded")),
    ("bare-too-many-requests", with_closing("Too many requests")),
    ("bare-rate-limited", with_closing("rate limited")),
    # A banner's own words followed by a role's: the line is not the banner.
    ("banner-then-comma", with_closing("● Usage limit reached · resets 5pm, so the client waits")),
    ("banner-then-sentence", with_closing("● Usage limit reached · continuing to retry")),
    ("banner-then-parenthesis",
     with_closing("● Usage limit reached · resets 5pm (the zone is Pacific/Kiritimati)")),
    ("banner-then-word", with_closing("● Usage limit reached · resets soon")),
    # What does not lead a recorded banner: an indented code block, a bullet, a quote
    # bar, a box edge, a handover (spec review p243dspec-a, M1).
    ("code-block-indent",
     with_closing("    Usage limit reached · continuing automatically at 3:20am · esc to cancel")),
    ("deep-indent", with_closing("        ● Usage limit reached · resets 5pm")),
    ("bullet", with_closing("  • Usage limit reached · resets 5pm")),
    ("quote-bar", with_closing("  ▎ Usage limit reached · resets 5pm")),
    ("box-edge", with_closing("│ Usage limit reached · resets 5pm")),
    ("handover", with_closing("Build failed: Usage limit reached · resets 5pm")),
    ("code-literal", with_closing('    re.compile(r"usage limit reached · x", re.I),')),
    ("diff-line", with_closing("+ Usage limit reached · resets 5pm")),
    ("quoted-banner", with_closing('"Usage limit reached · resets 5pm" is what the client shows')),
    ("list-item", with_closing("- Usage limit reached · resets 5pm is handled")),
    ("pytest-assertion", with_closing("E   AssertionError: quota exceeded · x")),
    # Phrases of the classifier's list that have no recorded banner shape.
    ("stdout-form-without-glyph", screen("Claude AI usage limit reached|1759600000")),
    ("quota-exceeded-segment", screen("⎿  Quota exceeded · resets 5pm")),
    ("rate-limited-segment", screen("⎿  Rate limited · retrying in 5s…")),
    ("too-many-requests-parenthesis", screen("Too many requests (429)")),
    # A recorded banner the classifier does not read either (L10): not taken, so the
    # error can never be UNCLASSIFIED.
    ("again-form-alone",
     screen("● Usage limit reached again · continuing automatically at 3:20am · esc to cancel")),
    # API errors.
    ("api-error-429-prose", with_closing("⎿  API Error: 429 is not retried by our client")),
    ("api-error-429-bare", screen("⎿  API Error: 429")),
    ("api-error-prose-mid-line", with_closing("● the API Error: 429 handler is missing")),
    ("api-error-503", screen("⎿  API Error: 503 overloaded_error")),
    ("expired-credential", screen("⎿  Invalid API key · Please run /login")),
    ("overloaded", screen("⎿  Overloaded · retrying")),
    ("connection-error", screen("⎿  API Error: Connection error.")),
]


@pytest.mark.parametrize("text", [c[1] for c in NOT_A_LIMIT], ids=[c[0] for c in NOT_A_LIMIT])
def test_a_roles_own_words_are_not_a_limit_screen(text):
    """Kills D3 (the lead widened: the code-block, bullet, quote-bar and box-edge
    rows), D4 (a free trail accepted: the prose, bare and German rows), D5 (the
    end of the line not anchored: the `banner-then-*` rows)."""
    assert failures.final_screen_limit(text) is None
    assert failures.final_screen_limit(view_of(text)) is None


def test_only_the_final_screen_counts():
    """Kills D6 (the window raised to the whole pane): the same banner line is a
    limit on the last twelve non-blank lines and nothing a line further up."""
    banner = INLINE_BANNER
    filler = [f"line {n}" for n in range(failures.FINAL_SCREEN_LINES - 1)]
    inside = screen(banner, *filler)
    assert len(failures.final_screen(inside)) == failures.FINAL_SCREEN_LINES
    assert failures.final_screen_limit(inside) is NF.USAGE_LIMIT
    outside = screen(banner, *filler, "one more line")
    assert failures.final_screen_limit(outside) is None
    # Blank lines are not lines of the screen.
    assert failures.final_screen_limit(screen(banner, *[x for f in filler for x in (f, "")])) \
        is NF.USAGE_LIMIT
    # Nothing scrolled out of the window is part of what the error will carry.
    assert banner not in "\n".join(failures.final_screen(outside))


def test_a_role_line_of_dashes_does_not_end_the_pane_block():
    """A role's own line of exactly the twenty-three dashes that close a pane block
    is content: the real banner after it is still read (spec review p243dspec-a,
    M2). Kills D13 (the block is cut at the first closing rule)."""
    rule = "-" * 23
    text = screen("older output", rule, *LIMIT_BOTTOM)
    assert failures.final_screen_limit(view_of(text)) is NF.USAGE_LIMIT
    carried = failures.final_screen_banners(view_of(text))
    assert carried == [LIMIT_BOTTOM[1], LIMIT_BOTTOM[7]]
    assert rule in failures.final_screen(view_of(text))


def test_the_reader_of_a_failed_turn_is_unchanged():
    """`classify_text` keeps reading a failed turn's pane with its pinned
    residuals; the stricter reader is a separate function. A negative control
    for the file that pins the classifier (`tests/test_failure_classes.py`)."""
    bare = view_of(with_closing("Rate limit."))
    assert failures.final_screen_limit(bare) is None
    assert failures.classify_text("qa ended its turn\n" + bare) is NF.RATE_LIMIT


RESIDUALS = [
    # A role's own closing line that is *exactly* a recorded banner, glyph and all,
    # among the last lines, is read as the banner (stated in the spec, L2); so is
    # a role's line that opens like the structured body of a 429, and a banner the
    # turn went on from by itself while it is still among the last lines.
    ("role-line-shaped-like-a-banner", with_closing("● Usage limit reached · resets 5pm"),
     NF.USAGE_LIMIT),
    ("role-line-shaped-like-a-429-body",
     with_closing('● API Error: 429 {"type":"error","error":{"message":"x"}}'),
     NF.RATE_LIMIT),
    ("stale-banner-still-in-view",
     screen(INLINE_BANNER, "● Continuing. Done: done.txt written.", *CLOSE_TAIL),
     NF.USAGE_LIMIT),
]


@pytest.mark.parametrize("text,expected", [c[1:] for c in RESIDUALS], ids=[c[0] for c in RESIDUALS])
def test_the_residuals_are_pinned(text, expected):
    """If one of these changes, say so in the spec's limits."""
    assert failures.final_screen_limit(text) is expected


# --- U2: the reader against the pane as the prompt found it (P2-43e) ------- #

#: The chrome of a pane in the state the limit left it, and of a healed one.
HEALED = screen(*STALE, *CLOSE_TAIL)
LEFT_BY_THE_LIMIT = screen(*LIMIT_BOTTOM)
#: A banner far enough up that it is outside the last twelve lines of one view and
#: inside those of the other (the limit-state chrome of the baseline is taller).
FILLER = [f"line {n}" for n in range(13)]
TALL_BEFORE = screen(LIMIT_BOTTOM[1], *FILLER)             # the banner is 14th from the end
SHORT_AFTER = screen(LIMIT_BOTTOM[1], *FILLER[:10])        # ... and 11th from the end
#: Another day's limit behind a long turn: the old banner has scrolled out of the window.
LONG_TURN = [f"● step {n}" for n in range(14)]

STALE_ROWS = [
    # (id, pane before the prompt, pane after the turn, expected class, banner lines carried)
    ("old-banner-in-the-scrollback", HEALED, short_rerun(3), None, []),
    ("old-inline-banner-too", screen(*STALE_INLINE, *CLOSE_TAIL),
     short_rerun(3, stale=STALE_INLINE), None, []),
    ("pane-as-the-limit-left-it", LEFT_BY_THE_LIMIT, short_rerun(3), None, []),
    ("old-banner-and-no-body-at-all", HEALED, screen(*STALE, *CLOSE_TAIL), None, []),
    ("old-banner-four-lines-up", HEALED, short_rerun(4), None, []),
    ("indent-the-pane-view-strips", screen(LIMIT_BOTTOM[1], *CLOSE_TAIL).strip() + "\n",
     screen("older output", LIMIT_BOTTOM[1], *CLOSE_TAIL), None, []),
    ("banner-seen-twice-before", screen(*STALE, *STALE, *CLOSE_TAIL),
     screen(*STALE, *STALE, "● Done.", *CLOSE_TAIL), None, []),
    ("the-baseline-is-the-whole-pane-not-its-window", TALL_BEFORE, SHORT_AFTER, None, []),
    ("no-baseline-is-the-old-reader", None, short_rerun(3), NF.USAGE_LIMIT, [STALE[1]]),
    ("an-unreadable-baseline-is-none",
     "--- Visible in pane ---\n(pane not readable: no such pane)\n-----------------------",
     short_rerun(3), NF.USAGE_LIMIT, [STALE[1]]),
    ("a-baseline-without-a-banner", IDLE_PANE, screen(*LIMIT_BOTTOM), NF.USAGE_LIMIT,
     [LIMIT_BOTTOM[1], LIMIT_BOTTOM[7]]),
    ("another-days-limit-after-the-old-one", HEALED,
     screen(*STALE, *FRESH_BOTTOM[1:]), NF.USAGE_LIMIT,
     [FRESH_BOTTOM[1], FRESH_BOTTOM[7]]),
    ("the-same-banner-again-after-the-old-one", HEALED,
     screen(*STALE, "● Done.", *STALE[:3], *CLOSE_TAIL), NF.USAGE_LIMIT, [STALE[1]]),
    ("identical-words-after-the-old-one-scrolled-out-the-status-line-is-read",
     HEALED, screen(*LONG_TURN, *LIMIT_BOTTOM), NF.USAGE_LIMIT, [LIMIT_BOTTOM[7]]),
    # A pane that is still limited when the prompt is sent (finding F1 of the review
    # of draft v1): it shows the status-area line on the baseline, and the line is
    # about now, never spent. The `⎿` line is the baseline's and is spent.
    ("still-limited-pane-the-status-line-is-read", LEFT_BY_THE_LIMIT,
     screen(*LIMIT_BOTTOM), NF.USAGE_LIMIT, [LIMIT_BOTTOM[7]]),
    ("still-limited-pane-and-a-short-turn", LEFT_BY_THE_LIMIT,
     screen(*STALE, "\u25cf Done.", *LIMIT_BOTTOM[4:]), NF.USAGE_LIMIT, [LIMIT_BOTTOM[7]]),
    # Pinned limit (L-E7): the one shape the rule above gives up. A status-area line
    # that a healed pane left in its scrollback (nothing recorded shows one) is read
    # as a limit, as the P2-43d reader reads it: the screen is the same as the one
    # of a pane that is still limited, and a live limit must never reach QA.
    ("limit-a-status-line-left-in-the-scrollback-is-read",
     screen(*STALE, LIMIT_BOTTOM[7], *CLOSE_TAIL),
     short_rerun(3, stale=[*STALE, LIMIT_BOTTOM[7]]), NF.USAGE_LIMIT, [LIMIT_BOTTOM[7]]),
    # Pinned residual (L-E1): the old banner has scrolled out and the new one reads
    # exactly like it, with nothing else of the limit on the screen: counted as old.
    ("residual-the-same-words-after-the-old-one-scrolled-out", HEALED,
     screen(*LONG_TURN, *LIMIT_BOTTOM[:3], *CLOSE_TAIL), None, []),
]


@pytest.mark.parametrize("before,after,expected,carried", [r[1:] for r in STALE_ROWS],
                         ids=[r[0] for r in STALE_ROWS])
def test_a_banner_the_pane_already_showed_before_the_prompt_is_not_this_turns(
        before, after, expected, carried):
    """P2-43e (finding F1 of the P2-43d integration review): the banner lines are
    counted against the pane as the prompt found it -- each line the baseline
    shows is spent once, oldest first, over the whole baseline block -- and only
    the rest is read, and carried. Kills E3 (the baseline ignored), E4 (a set, not
    a count: the same banner again), E5 (the baseline's last twelve lines only:
    the baseline-is-the-whole-pane row), E6 (the words are not compared by their
    words: the indent row), E11 (the status-area line is spent: the still-limited
    rows). The no-baseline and unreadable-baseline rows are the P2-43d reader and
    pin L-E2; the limit row pins L-E7 and the last row L-E1."""
    base = None if before is None else (before if before.startswith("---") else view_of(before))
    view = view_of(after)
    assert failures.final_screen_limit(view, before=base) is expected
    assert [ln.strip() for ln in failures.final_screen_banners(view, before=base)] \
        == [ln.strip() for ln in carried]


def test_the_stale_banner_is_in_the_last_twelve_lines_without_a_baseline():
    """The precondition of every row above, so that none passes vacuously: the
    short good re-run of the finding really is read as a limit by the reader of
    P2-43d (the same reader, no baseline)."""
    for body in (2, 3, 4):
        assert failures.final_screen_limit(view_of(short_rerun(body))) is NF.USAGE_LIMIT, body
    assert failures.final_screen_limit(view_of(short_rerun(5))) is None


def test_a_banner_redrawn_with_other_inner_spacing_is_the_same_banner():
    """The words of a banner line are compared, not its spacing: a redraw that
    prints the glyph with two plain spaces and the dot between non-breaking spaces
    is still the old line. Kills E12 (`_banner_key` strips the ends only)."""
    respaced = STALE_RESPACED[1]
    assert respaced != STALE[1] and respaced.strip() != STALE[1].strip()   # not just the indent
    after = view_of(short_rerun(3, stale=STALE_RESPACED))
    assert failures.final_screen_limit(after) is NF.USAGE_LIMIT              # still a banner shape
    assert failures.final_screen_limit(after, before=view_of(HEALED)) is None
    assert failures.final_screen_banners(after, before=view_of(HEALED)) == []


def test_the_cut_form_of_another_days_limit_is_spent_as_the_old_one():
    """Pins L-E1 for the 48-column form (finding F2 of the review of draft v1): the
    `session limit . resets` line carries no time -- the time sits on the next line,
    which is no banner shape -- so a limit with another reset time reads like the
    old one and is spent. The status-area line of the new limit (`limit resets
    8:20pm`, a whole line even at 48 columns) is not on the baseline and is read."""
    stale = [WRAPPED_BOTTOM[0], WRAPPED_BOTTOM[1]]
    fresh = [WRAPPED_BOTTOM[0], "     8:20pm (Europe/Istanbul)"]
    steps = [f"\u25cf step {n}" for n in range(14)]
    before = view_of(screen(*stale, *CLOSE_TAIL))
    after = view_of(screen(*steps, *fresh, *CLOSE_TAIL))
    assert failures.final_screen_limit(after) is NF.USAGE_LIMIT              # the P2-43d reader reads it
    assert failures.final_screen_limit(after, before=before) is None         # the residual: spent
    status = WRAPPED_BOTTOM[8].replace("5:20pm", "8:20pm")
    full = view_of(screen(*steps, *fresh, *WRAPPED_BOTTOM[5:8], status, *WRAPPED_BOTTOM[9:]))
    assert failures.final_screen_banners(full, before=before) == [status]


# --------------------------------------------------------------------------- #
# D -- the real dispatcher on a fake Herdr
# --------------------------------------------------------------------------- #

def _reply(status: str = "idle") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
        {"result": {"agent": {"pane_id": TARGET, "workspace_id": "w0", "tab_id": "w0:t1",
                              "agent": "claude", "agent_status": status, "cwd": "/x"}}}))


class FakeHerdr:
    """Stands in for `subprocess.run` inside `hoh.dispatchers` and for
    `hoh.herdr.read_agent`. `develop()` is what the developer does to the tree
    while its prompt runs; `view` is the pane after the turn (or an exception
    the read raises); `before` is the pane the prompt finds (an idle pane without
    a banner unless given; an exception makes the read before the prompt fail);
    the pane is `view` from the first prompt on, and stays what the last turn
    left it; `status` is what Herdr reports."""

    def __init__(self, view, *, status: str = "idle", develop=None, before=None) -> None:
        self.view = view
        self.pane = IDLE_PANE if before is None else before
        self.status = status
        self.develop = develop
        self.calls: list[list[str]] = []
        self.reads: list[tuple[str, int]] = []
        self.events: list[str] = []

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[1:3] == ["agent", "prompt"]:
            self.events.append("prompt")
            self.pane = self.view
            if self.develop is not None:
                self.develop()
        return _reply(self.status)

    def read_agent(self, target, *, lines=120):
        self.events.append("read")
        self.reads.append((target, lines))
        if isinstance(self.pane, BaseException):
            raise self.pane
        return self.pane

    def kind(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["agent", verb]]


def make_state(root: Path):
    """(store, state) of a run `r1` under `root`, its repository an empty directory."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "repo").mkdir(exist_ok=True)
    (root / "spec.md").write_text("# s\n", encoding="utf-8")
    store = RunStore(root / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=root / "repo", project_name="p",
                                 spec_path=root / "spec.md", budgets=Budgets(max_dispatches=9))
    store.write_state(state)
    return store, state


def make_dispatcher(root: Path, monkeypatch, fake: FakeHerdr):
    """(store, state, dispatcher): a Herdr-free `HerdrDispatcher` over `fake`."""
    store, state = make_state(root)
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(d, answers_dir=store.dir / "answers")
    d.cwd = str(root / "repo")
    d.timeout_ms = 1000
    monkeypatch.setattr(d, "_ensure_agent", lambda role, st: TARGET)
    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=fake.run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(herdr, "read_agent", fake.read_agent)
    return store, state, d


def dispatch(tmp_path, monkeypatch, view, *, role=Role.DEVELOPER, status="idle", develop=None,
             before=None):
    fake = FakeHerdr(view, status=status, develop=develop, before=before)
    store, state, d = make_dispatcher(tmp_path, monkeypatch, fake)
    try:
        return fake, d, state, d.dispatch(role, "p", state=state), None
    except Exception as exc:                       # noqa: BLE001 - the test inspects it
        return fake, d, state, None, exc


def test_a_developer_turn_that_ends_on_a_limit_screen_is_a_failed_dispatch(tmp_path, monkeypatch):
    """Kills D7 (the check not called after a developer turn: the dispatch
    returns `developer run finished`), D8 (the error is transient: the
    controller would re-send the prompt into the limit), D11 (the message carries
    the whole final screen, not the banner lines)."""
    fake, d, state, result, exc = dispatch(
        tmp_path, monkeypatch, screen(*LIMIT_BOTTOM, scrollback=30))
    assert result is None, result
    assert type(exc) is DispatchError
    assert exc.transient is False and exc.retry_after == 0
    first, *rest = str(exc).splitlines()
    assert first == ("developer ended its turn on a provider's limit screen; "
                     "it is not counted as a finished turn")
    assert rest[0] == "--- Visible in pane ---" and rest[-1] == "-----------------------"
    carried = rest[1:-1]
    # Exactly the lines that were read as the banner: the two the classifier
    # knows from a recorded pane, and nothing else of the screen.
    assert carried == [LIMIT_BOTTOM[1], LIMIT_BOTTOM[7]]
    assert carried == failures.final_screen_banners(view_of(screen(*LIMIT_BOTTOM, scrollback=30)))
    # One prompt, no status read, two pane reads: the pane as the prompt found it
    # (the baseline, P2-43e), then the pane after the turn.
    assert len(fake.kind("prompt")) == 1 and fake.kind("get") == []
    assert fake.events == ["read", "prompt", "read"]
    assert fake.reads == [(TARGET, 40), (TARGET, 40)]
    # The classes the rest of the product reads off it.
    assert failures.classify(exc) is NF.USAGE_LIMIT
    assert failures.is_automatic(failures.classify(exc))
    stated = failures.next_attempt(NF.USAGE_LIMIT, str(exc),
                                   now=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc))
    assert stated.isoformat() == "2026-10-10T00:21:00+00:00"


def test_the_widest_recorded_shape_keeps_its_session_limit_line_in_the_window(
        tmp_path, monkeypatch):
    """The wrapped pane puts the session-limit line (the one that names the zone of
    the reset) twelfth from the end. It is carried, with the status-area banner,
    and nothing else. The zone itself sits on the continuation line of a wrapped
    banner and is not carried: the wait is the default hour, bounded by the resume
    attempts (stated in the spec, L9). Kills D12 (a window of eleven lines)."""
    assert len(failures.final_screen(screen(*WRAPPED_BOTTOM))) == failures.FINAL_SCREEN_LINES
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, screen(*WRAPPED_BOTTOM))
    assert type(exc) is DispatchError and failures.classify(exc) is NF.USAGE_LIMIT
    # (`_pane_view` strips the whole text, so the first line loses its indent.)
    assert [ln.strip() for ln in str(exc).splitlines()[2:-1]] == [
        WRAPPED_BOTTOM[0].strip(), WRAPPED_BOTTOM[8].strip()]
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert failures.next_attempt(NF.USAGE_LIMIT, str(exc), now=now) == now + quota.DEFAULT_WAIT


def test_a_rate_limit_screen_is_a_failed_dispatch_of_its_class(tmp_path, monkeypatch):
    """Kills D2 (usage limit only), D7."""
    fake, d, state, result, exc = dispatch(
        tmp_path, monkeypatch, with_closing(API_429))
    assert result is None and type(exc) is DispatchError
    assert failures.classify(exc) is NF.RATE_LIMIT


def test_a_developer_turn_that_ends_normally_is_unchanged(tmp_path, monkeypatch):
    """The negative control of every test here: the same dispatcher, the same
    fakes, a pane without a banner."""
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, screen(*NORMAL_BOTTOM))
    assert exc is None
    assert result == "developer run finished (status idle)"
    assert len(fake.kind("prompt")) == 1 and fake.kind("get") == []


def test_the_pane_is_read_once_and_after_the_turn_of_a_developer(tmp_path, monkeypatch):
    """The verdict is one `agent read` per developer turn (the same read that
    diagnoses a failed dispatch, 40 lines), made after the prompt returned and
    before the turn is booked; since P2-43e one more read of the same kind comes
    first, before the prompt (the baseline). Kills D7 (no read), E1 (no baseline
    read), E2 (the baseline is the verdict read)."""
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, screen(*NORMAL_BOTTOM))
    assert exc is None
    assert fake.events == ["read", "prompt", "read"]
    assert fake.reads == [(TARGET, 40), (TARGET, 40)]


MENTIONS = [
    ("prose-rate-limiting", "● Rate limiting is not implemented in app.py; I left a TODO."),
    ("prose-with-colon", "● Rate limit: cap at 100/min"),
    ("bare-phrase", "Usage limit reached"),
    ("sentence", "Quota exceeded, see the table below"),
    ("quoted", '● I made the client stop at "Usage limit reached · resets 5pm" and tested it.'),
    ("parenthesis", "● Rate limit (60 rpm) added to the API client and tested."),
    ("dash", "● Rate limit — token bucket implemented"),
    ("http-429", "  Too many requests (HTTP 429) is now returned"),
    ("quota-dash", "  Quota exceeded — now raises QuotaError"),
    ("ellipsis", "  Rate limited… retried 3 times"),
    ("pipe", "  Rate limit | window | cap"),
]


@pytest.mark.parametrize("closing", [c[1] for c in MENTIONS], ids=[c[0] for c in MENTIONS])
def test_a_role_that_only_mentions_a_limit_finishes_its_turn(tmp_path, monkeypatch, closing):
    """A developer's own words about limits, in the closing message of a turn that
    ended normally, are not a provider's banner (spec review p243dspec-a, B1: the
    last six lines of the table are the sentences that parked a good turn under
    the first draft). Kills D4, D5."""
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, with_closing(closing))
    assert exc is None, repr(exc)
    assert result == "developer run finished (status idle)"


def test_a_banner_above_the_final_screen_is_not_where_the_turn_ended(tmp_path, monkeypatch):
    """The developer's output quotes a banner (a limit that has since passed, or
    its own test fixture) and then goes on for more than a screen. Kills D6."""
    view = screen(INLINE_BANNER, *[f"step {n}" for n in range(failures.FINAL_SCREEN_LINES)],
                  *CLOSE_TAIL)
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, view)
    assert exc is None and result == "developer run finished (status idle)"


def test_work_in_the_tree_does_not_make_a_limit_screen_a_finished_turn(tmp_path, monkeypatch):
    """The developer wrote files, then the limit stopped it. The turn is still not
    finished: the candidate is not frozen from it. Nothing it wrote is touched.
    Kills D7 (nothing raises: the half-done tree is frozen)."""
    root = tmp_path / "work"

    def develop():
        (root / "repo" / "partial.txt").write_text("half\n", encoding="utf-8")

    fake, d, state, result, exc = dispatch(
        root, monkeypatch, screen(*LIMIT_BOTTOM), develop=develop)
    assert result is None and type(exc) is DispatchError
    assert (root / "repo" / "partial.txt").read_text(encoding="utf-8") == "half\n"


def test_a_blocked_pane_with_nothing_delivered_keeps_its_approval_route(tmp_path, monkeypatch):
    """Pins L6: a pane Herdr reports as `blocked` whose tree did not change is an
    approval dialog and is raised as one, whatever its screen shows; a stale banner
    must not hide a dialog the dispatcher never answers. The same pane with work
    in the tree is a delivery, and then the final screen decides."""
    fake, d, state, result, exc = dispatch(
        tmp_path / "nothing", monkeypatch, screen(*LIMIT_BOTTOM), status="blocked")
    assert type(exc) is WaitingForApproval, repr(exc)

    root = tmp_path / "delivered"

    def develop():
        (root / "repo" / "partial.txt").write_text("half\n", encoding="utf-8")

    fake, d, state, result, exc = dispatch(
        root, monkeypatch, screen(*LIMIT_BOTTOM), status="blocked", develop=develop)
    assert type(exc) is DispatchError, repr(exc)
    assert failures.classify(exc) is NF.USAGE_LIMIT


def test_an_unreadable_pane_changes_nothing(tmp_path, monkeypatch):
    """A pane that cannot be read proves nothing: the turn is booked as before
    (stated in the spec, L3) and noted in the run history. Kills D10 (an
    unreadable pane is a failure: every developer turn would fail when the read
    does)."""
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, OSError("no such pane"))
    assert exc is None and result == "developer run finished (status idle)"
    # ... and the run history says so, in words no classifier reads.
    [note] = [h for h in state.history if "final screen could not be read" in h]
    assert "developer: the final screen could not be read" in note
    assert "no such pane" not in note
    # The negative control: a readable pane leaves no such note.
    fake, d, state, result, exc = dispatch(tmp_path / "control", monkeypatch, screen(*NORMAL_BOTTOM))
    assert exc is None and not [h for h in state.history if "could not be read" in h]


def test_the_other_roles_are_not_second_guessed(tmp_path, monkeypatch):
    """QA answered through its file; the screen after it is not read, even when it
    ends on a banner: the answer is the answer. Kills D9 (the check applied to a
    structured role), E8 (a baseline read for a structured role)."""
    root = tmp_path / "qa"
    fake = FakeHerdr(screen(*LIMIT_BOTTOM))
    store, state, d = make_dispatcher(root, monkeypatch, fake)
    answer = d._answer_path(Role.QA, state)

    def write():
        answer.write_text(ANSWER, encoding="utf-8")

    fake.develop = write
    assert d.dispatch(Role.QA, "p", state=state) == ANSWER
    assert fake.reads == []


def test_a_roles_words_on_the_final_screen_do_not_change_the_class_or_the_reset(
        tmp_path, monkeypatch):
    """The error carries the banner lines and nothing else, so a role's closing
    lines on the same screen (a bare `Invalid API key.`, which the permissive
    classifier of a failed turn reads as an expired credential, or a sentence
    about another reset time) can neither turn a usage-limit wait into an operator
    action nor move the retry. Kills D11 (the whole final screen is carried)."""
    view = screen("Invalid API key.", "The limit resets at 9pm (Pacific/Kiritimati), I read.",
                  *LIMIT_BOTTOM)
    assert failures.classify_text("x\n" + view_of(view)) is NF.AUTH_EXPIRED   # the failed-turn reader
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, view)
    assert type(exc) is DispatchError
    assert "Invalid API key" not in str(exc) and "Kiritimati" not in str(exc)
    assert failures.classify(exc) is NF.USAGE_LIMIT
    stated = failures.next_attempt(NF.USAGE_LIMIT, str(exc),
                                   now=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc))
    assert stated.isoformat() == "2026-10-10T00:21:00+00:00"


@pytest.mark.parametrize("body", [2, 3, 4], ids=["two-lines", "three-lines", "four-lines"])
@pytest.mark.parametrize("before", [HEALED, LEFT_BY_THE_LIMIT],
                         ids=["healed-pane", "pane-as-the-limit-left-it"])
def test_a_good_short_rerun_on_a_pane_that_kept_the_old_banner_finishes_its_turn(
        tmp_path, monkeypatch, before, body):
    """The finding F1: the pane outlived the wait and keeps the old banner in its
    scrollback; the re-run is short and good. It is a finished turn, however many
    times it is asked, and the history says why it was not parked. Kills E1 (no
    baseline read), E2 (the baseline is the verdict read), E3 (nothing is spent),
    E10 (the history says nothing)."""
    after = short_rerun(body)
    assert failures.final_screen_limit(view_of(after)) is NF.USAGE_LIMIT     # the old reader parks it
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, after, before=before)
    assert exc is None, repr(exc)
    assert result == "developer run finished (status idle)"
    assert fake.events == ["read", "prompt", "read"]
    [note] = [h for h in state.history if "already on the pane before the prompt" in h]
    assert " developer: " in note
    # The negative control: a pane without a banner leaves no such note.
    fake, d, state, result, exc = dispatch(tmp_path / "control", monkeypatch, screen(*NORMAL_BOTTOM))
    assert exc is None and not [h for h in state.history if "before the prompt" in h]


def test_a_new_limit_after_an_old_banner_is_a_failed_dispatch_with_the_new_reset(
        tmp_path, monkeypatch):
    """The turn ended on the limit of the next day, on a pane that still shows the
    old one: the failed dispatch carries the new banner lines and nothing of the
    old, so the retry is the new reset (8:20am), never the old one (3:20am) that
    the first banner of an error text would give. Kills E1, E2, E3 (the old lines
    are carried and win), E7 (the error carries the unfiltered banners)."""
    after = screen(*STALE, *FRESH_BOTTOM[1:])
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, after, before=HEALED)
    assert result is None and type(exc) is DispatchError
    assert exc.transient is False
    rest = str(exc).splitlines()[2:-1]
    assert rest == [FRESH_BOTTOM[1], FRESH_BOTTOM[7]]
    assert failures.classify(exc) is NF.USAGE_LIMIT
    stated = failures.next_attempt(NF.USAGE_LIMIT, str(exc),
                                   now=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc))
    assert stated.isoformat() == "2026-10-10T05:21:00+00:00"                 # 8:20am +03:00, plus the margin
    assert not [h for h in state.history if "before the prompt" in h]


@pytest.mark.parametrize("after", [screen(*LIMIT_BOTTOM),
                                   screen(*STALE, "\u25cf Done.", *LIMIT_BOTTOM[4:])],
                         ids=["the-turn-leaves-the-screen-as-it-was", "a-short-turn-under-the-status-area"])
def test_a_pane_that_is_still_limited_when_the_prompt_is_sent_is_a_failed_dispatch(
        tmp_path, monkeypatch, after):
    """Finding F1 of the review of draft v1 (A5 clause 2: a live limit never reaches
    QA): the wait ended early, or the status area was not redrawn, so the prompt
    finds the pane as the limit left it, status-area line and all, and the turn
    leaves it limited. The `⎿` line of the baseline is spent; the status-area line
    is about now and is read: a failed dispatch of its class, not `developer run
    finished`. Kills E11 (the status-area line is spent like any other)."""
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, after, before=screen(*LIMIT_BOTTOM))
    assert result is None and type(exc) is DispatchError, result
    assert exc.transient is False and failures.classify(exc) is NF.USAGE_LIMIT
    assert str(exc).splitlines()[2:-1] == [LIMIT_BOTTOM[7]]
    assert fake.events == ["read", "prompt", "read"]
    assert not [h for h in state.history if "before the prompt" in h]


def test_the_baseline_is_read_before_the_send_time_is_taken(tmp_path, monkeypatch):
    """The role's time budget is counted from the send (`sent_at`, which a stalled
    send's mark keeps): the baseline read comes first and is not part of it. The
    clock here moves 50 s on every pane read; the prompt fails, so the dispatcher
    keeps its mark, and the mark's time is the clock at the send, after the read.
    Kills E13 (the baseline is read after `sent_at` is taken)."""
    clock = {"now": 100.0}

    class SlowRead(FakeHerdr):
        sent_clock = None

        def read_agent(self, target, *, lines=120):
            clock["now"] += 50.0
            return super().read_agent(target, lines=lines)

        def run(self, args, **kwargs):
            if args[1:3] == ["agent", "prompt"]:
                self.sent_clock = clock["now"]
                raise RuntimeError("the prompt did not return")
            return super().run(args, **kwargs)

    fake = SlowRead(screen(*NORMAL_BOTTOM))
    store, state, d = make_dispatcher(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(dispatchers, "time", types.SimpleNamespace(
        monotonic=lambda: clock["now"], sleep=lambda *_: None, time=lambda: 0.0))
    with pytest.raises(Exception):
        d.dispatch(Role.DEVELOPER, "p", state=state)
    [(_digest, sent_at, _earlier)] = d._unsettled().values()
    assert fake.reads and fake.sent_clock == 150.0                           # the baseline was read first
    assert sent_at == fake.sent_clock


@pytest.mark.parametrize("after,cls", [(screen(*LIMIT_BOTTOM, scrollback=30), NF.USAGE_LIMIT),
                                       (with_closing(API_429), NF.RATE_LIMIT)],
                         ids=["usage-limit", "rate-limit"])
def test_a_limit_after_a_normal_finish_is_still_a_failed_dispatch(
        tmp_path, monkeypatch, after, cls):
    """The property of P2-43d, with the baseline in place: the prompt finds a pane
    that finished a turn normally (no banner), the turn ends on a limit screen. The
    banner is new, so the turn is a failed dispatch of its class, carrying the
    banner lines. Kills E1 (no baseline read: the order of the reads), E2 (the
    baseline is the verdict read: everything is spent)."""
    fake, d, state, result, exc = dispatch(tmp_path, monkeypatch, after,
                                           before=screen(*NORMAL_BOTTOM, scrollback=30))
    assert result is None and type(exc) is DispatchError
    assert exc.transient is False and failures.classify(exc) is cls
    assert str(exc).splitlines()[2:-1] == failures.final_screen_banners(view_of(after))
    assert fake.events == ["read", "prompt", "read"]
    assert not [h for h in state.history if "before the prompt" in h]


def test_an_unreadable_baseline_is_no_baseline(tmp_path, monkeypatch):
    """Pins L-E2: a pane that cannot be read before the prompt gives no baseline,
    and every banner counts, as before P2-43e -- the stale banner of the finding
    parks the turn again, a real limit still parks it. Kills E9 (a baseline that
    could not be read suppresses the check)."""
    fake, d, state, result, exc = dispatch(
        tmp_path / "stale", monkeypatch, short_rerun(3), before=OSError("no such pane"))
    assert type(exc) is DispatchError and failures.classify(exc) is NF.USAGE_LIMIT
    assert fake.events == ["read", "prompt", "read"]
    fake, d, state, result, exc = dispatch(
        tmp_path / "real", monkeypatch, screen(*LIMIT_BOTTOM), before=OSError("no such pane"))
    assert type(exc) is DispatchError and failures.classify(exc) is NF.USAGE_LIMIT


# The non-Herdr adapter (`hoh run --no-herdr`, `HarnessDispatcher`): the recorded
# `claude -p` limit form is a failed dispatch since slice a; an unrecorded stdout
# form is a stated limit (L5), pinned here.

SUBPROCESS_LIMIT = "Claude AI usage limit reached|1759600000"


def harness_dispatch(root: Path, monkeypatch, *, returncode: int, stdout: str = "", stderr: str = ""):
    store, state = make_state(root)
    d = HarnessDispatcher(answers_dir=store.dir / "answers",
                          profiles={Role.DEVELOPER: "claude"}, cwd=root / "repo")
    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr),
        TimeoutExpired=subprocess.TimeoutExpired))
    try:
        return d.dispatch(Role.DEVELOPER, "p", state=state), None
    except Exception as exc:                       # noqa: BLE001 - the test inspects it
        return None, exc


def test_the_recorded_subprocess_limit_form_is_a_failed_developer_dispatch(tmp_path, monkeypatch):
    """The `claude -p` form (exit 1, empty stdout, the limit on stderr) was recorded
    and is a failed dispatch of its class for the developer too, with no help from
    this change: the non-Herdr adapter is covered for the form that was seen."""
    result, exc = harness_dispatch(tmp_path, monkeypatch, returncode=1, stderr=SUBPROCESS_LIMIT)
    assert result is None and type(exc) is DispatchError
    assert exc.transient is False
    assert failures.classify(exc) is NF.USAGE_LIMIT


@pytest.mark.parametrize("returncode,stdout", [(0, SUBPROCESS_LIMIT), (1, SUBPROCESS_LIMIT)],
                         ids=["exit-0-with-stdout", "exit-1-with-stdout"])
def test_an_unrecorded_stdout_form_of_the_subprocess_adapter_is_not_read(
        tmp_path, monkeypatch, returncode, stdout):
    """Pins the stated limit L5: a limit on a **non-empty** stdout of the
    non-Herdr adapter is booked as the developer's answer, as before. No such
    form was ever recorded; reading one needs a recorded sample."""
    result, exc = harness_dispatch(tmp_path, monkeypatch, returncode=returncode, stdout=stdout)
    assert exc is None and result == stdout


# --------------------------------------------------------------------------- #
# E -- the real controller and resume loop
# --------------------------------------------------------------------------- #

SPEC = "# Spec\nadd done.txt\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "T")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


class Roles:
    """The planner and QA the test decides; the developer is the real dispatcher."""

    def __init__(self, repo: Path, real: HerdrDispatcher) -> None:
        self.repo = repo
        self.real = real
        self.calls: list[str] = []

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role.value)
        if role is Role.DEVELOPER:
            return self.real.dispatch(role, prompt, state=state)
        if role is Role.PLANNER:
            base = state.last_accepted_candidate or state.base_candidate
            return json.dumps({
                "run_id": state.run_id, "iteration": state.iteration,
                "base_candidate_id": base.candidate_id if base else f"{state.run_id}-base",
                "spec_digest": state.spec_digest, "objective": "add done.txt",
                "targets": ["app.py"], "preserve": [],
                "acceptance_checks": [{
                    "check_id": "K1", "description": "done.txt exists",
                    "command": "test -f done.txt", "expect_exit": 0, "preserves": False}],
                "out_of_scope": [], "evidence_refs": [],
                "repair_only": False, "repair_reason": None})
        return json.dumps({
            "verdicts": [{
                "check_id": "K1", "outcome": "PASS",
                "receipt_id": f"{state.run_id}-i{state.iteration}-a{state.attempt}-K1",
                "reproduction": "pytest", "note": "green"}],
            "open_gaps": [], "summary": "all good"})

    def endpoint_evidence(self, role: Role) -> str:
        return f"fake:{role.value}"


def build(tmp_path: Path, repo: Path, monkeypatch, view):
    """A controller over the real developer dispatcher; `fake.view` is what the
    pane shows after each developer turn."""
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=9))
    store.write_state(state)

    def develop():
        (repo / "done.txt").write_text("ok\n", encoding="utf-8")

    fake = FakeHerdr(view, develop=develop)
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(d, answers_dir=store.dir / "answers")
    d.cwd = str(repo)
    d.timeout_ms = 1000
    monkeypatch.setattr(d, "_ensure_agent", lambda role, st: TARGET)
    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=fake.run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(herdr, "read_agent", fake.read_agent)
    roles = Roles(repo, d)
    return Controller(store, roles, spec_path=spec), state, store, roles, fake


def telemetry(store: RunStore) -> list[dict]:
    path = store.dir / "telemetry.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def armed(store: RunStore):
    return [t for t in TimerStore(store.dir.parent).read()
            if t.status is TimerStatus.ARMED and t.kind is TimerKind.PROVIDER_RETRY
            and t.run_id == "r1"]


def history(state) -> str:
    return "\n".join(state.history)


def test_a_developer_limit_parks_the_run_and_the_candidate_never_reaches_qa(
        tmp_path, repo, monkeypatch):
    """Kills D7 (nothing raises: the candidate is frozen and QA is dispatched),
    D8 (the failed dispatch is transient: the developer is prompted again at
    once, into the limit)."""
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, screen(*LIMIT_BOTTOM))
    out = ctrl.run_iteration(state)
    assert not out.accepted and out.reason.startswith("no verdict")
    assert roles.calls == ["planner", "developer"]          # QA was never asked
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "usage_limit", Stage.DEVELOPING)
    assert state.retry_after
    text = history(state)
    assert "candidate rejected" not in text and "unverified" not in text
    assert "failure class USAGE_LIMIT (AUTOMATIC) in developer" in text
    assert (state.iteration, state.usage.iterations, state.usage.outages,
            state.usage.loops_without_progress) == (1, 1, 0, 0)
    persisted = store.read_state()
    assert (persisted.condition, persisted.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    [timer] = armed(store)
    assert timer.reason.startswith("usage limit: ")
    # The failed dispatch is booked as one, with its class (A5 clause 1).
    [failed] = [ln for ln in telemetry(store) if ln["role"] == "developer" and ln["outcome"] != "ok"]
    assert (failed["outcome"], failed["normalized_failure"], failed["failure_class"]) == (
        "failed", "USAGE_LIMIT", "PROVIDER_QUOTA")
    # The developer's work is where it was left.
    assert (repo / "done.txt").read_text(encoding="utf-8") == "ok\n"
    # The park is receipted as a developer's: A5 clause 4.
    records, problems = dispatchrecovery.read_records(store.dir)
    assert problems == [] and len(records) == 1
    [record] = records
    assert record.kind == "ROUTING_DECISION" and record.reason == "USAGE_LIMIT"
    assert record.provenance.role == "developer"
    assert record.binding.stage == "DEVELOPING" and record.retry_at == state.retry_after
    # The reset the provider states (3:20am Istanbul), not the default hour.
    stated = datetime.fromisoformat(state.retry_after).astimezone(timezone.utc)
    assert (stated.hour, stated.minute) == (0, 21)            # 3:20am +03:00, plus the margin


def test_the_parked_developer_wait_is_bound_resumed_once_and_keeps_the_iteration(
        tmp_path, repo, monkeypatch):
    """The recovery of slice c on a developer's park: the receipt is verified
    against the live state, the resume writes its claim, and the same iteration
    carries on to QA with the screen healed. Kills D7."""
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, screen(*LIMIT_BOTTOM))
    ctrl.run_iteration(state)
    assert roles.calls == ["planner", "developer"]
    due = datetime.now(timezone.utc) + timedelta(hours=30)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=due)
    assert rec["action"] == "resumed"
    assert rec["binding"] == dispatchrecovery.ParkVerdict.BOUND.value
    assert rec["claim"] == "written"
    assert armed(store) == []
    # The screen is healed: the resumed developer's turn ends normally.
    fake.view = screen(*NORMAL_BOTTOM)
    persisted = store.read_state()
    fresh = Controller(store, roles, spec_path=tmp_path / "spec.md")
    done = fresh.run_iteration(persisted)
    assert done.accepted
    assert (persisted.iteration, persisted.usage.iterations) == (1, 1)
    assert roles.calls.count("qa") == 1 and roles.calls.count("developer") == 2
    again = resumeloop.resume_runs(tmp_path / "runs", now=due)
    assert all(r["action"] != "resumed" for r in again)


def test_a_normal_developer_finish_goes_to_qa_as_before(tmp_path, repo, monkeypatch):
    """The negative control of the two tests above: the same controller, the same
    real dispatcher, a pane without a banner."""
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, screen(*NORMAL_BOTTOM))
    out = ctrl.run_iteration(state)
    assert out.accepted
    assert roles.calls == ["planner", "developer", "qa"]
    assert state.blocked_kind is None and armed(store) == []


def test_a_developer_that_only_mentions_a_limit_goes_to_qa(tmp_path, repo, monkeypatch):
    """A developer's own words about rate limits and usage limits, at the end of a
    turn that ended normally, are not evidence about the provider. Kills D4, D5."""
    view = with_closing("● Rate limiting is not implemented in app.py",
                        "● Rate limit: cap at 100/min", "Usage limit reached")
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, view)
    out = ctrl.run_iteration(state)
    assert out.accepted
    assert roles.calls == ["planner", "developer", "qa"]


def test_the_recorded_subprocess_limit_form_parks_a_developer_through_the_non_herdr_adapter(
        tmp_path, repo, monkeypatch):
    """End to end through `HarnessDispatcher`: the developer writes `done.txt`, the
    CLI exits 1 with the limit on stderr, the run waits and QA is never asked."""
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=9))
    store.write_state(state)

    def run(*a, **k):
        (repo / "done.txt").write_text("ok\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=1, stdout="", stderr=SUBPROCESS_LIMIT)

    monkeypatch.setattr(dispatchers, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    real = HarnessDispatcher(answers_dir=store.dir / "answers",
                             profiles={Role.DEVELOPER: "claude"}, cwd=repo)
    roles = Roles(repo, real)
    out = Controller(store, roles, spec_path=spec).run_iteration(state)
    assert not out.accepted and out.reason.startswith("no verdict")
    assert roles.calls == ["planner", "developer"]
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "usage_limit", Stage.DEVELOPING)
    assert (state.iteration, state.usage.iterations) == (1, 1)
    assert len(armed(store)) == 1


def test_a_resumed_developer_on_a_pane_that_kept_the_old_banner_goes_to_qa(
        tmp_path, repo, monkeypatch):
    """The loop of the finding F1, through the real controller and resume loop: the
    developer is parked on a limit, the wait ends, and the pane that is prompted
    again is the one the limit left, its old banner in the scrollback. The re-run is
    short and good. It must go to QA on the first resume. On P2-43d the loop ran
    like this: resumed, parked again, five times, then `given_up`, six developer
    dispatches and QA never asked (the failure message of this test on that code).
    Kills E1, E2, E3, E10."""
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, screen(*LIMIT_BOTTOM))
    ctrl.run_iteration(state)
    assert roles.calls == ["planner", "developer"]
    assert (state.condition, state.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    fake.view = short_rerun(3)               # the pane stays; the re-run is short and good
    trail, persisted = [], state
    for k in range(quota.MAX_RESUME_ATTEMPTS + 2):
        due = datetime.now(timezone.utc) + timedelta(hours=30 * (k + 1))
        recs = resumeloop.resume_runs(tmp_path / "runs", now=due)
        trail.append([r["action"] for r in recs])
        if [r["action"] for r in recs] != ["resumed"]:
            break
        assert recs[0]["binding"] == dispatchrecovery.ParkVerdict.BOUND.value
        persisted = store.read_state()
        done = Controller(store, roles, spec_path=tmp_path / "spec.md").run_iteration(persisted)
        trail.append("accepted" if done.accepted else "parked again")
        if done.accepted:
            break
    assert trail == [["resumed"], "accepted"], (
        " > ".join(str(step) for step in trail)
        + f" (developer dispatched {roles.calls.count('developer')} times, QA {roles.calls.count('qa')})")
    assert roles.calls.count("qa") == 1 and roles.calls.count("developer") == 2
    assert (persisted.iteration, persisted.usage.iterations) == (1, 1)
    assert armed(store) == []
    assert [h for h in persisted.history if "already on the pane before the prompt" in h]
    again = resumeloop.resume_runs(tmp_path / "runs", now=datetime.now(timezone.utc) + timedelta(hours=99))
    assert all(r["action"] != "resumed" for r in again)


def test_a_new_limit_on_the_resumed_developer_is_parked_again_with_its_own_reset(
        tmp_path, repo, monkeypatch):
    """The control of the test above: when the re-run meets a limit of its own, the
    old banner on the pane does not hide it, and the new wait is the new reset."""
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, screen(*LIMIT_BOTTOM))
    ctrl.run_iteration(state)
    fake.view = screen(*STALE, *FRESH_BOTTOM[1:])
    due = datetime.now(timezone.utc) + timedelta(hours=30)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=due)
    assert rec["action"] == "resumed"
    persisted = store.read_state()
    again = Controller(store, roles, spec_path=tmp_path / "spec.md").run_iteration(persisted)
    assert not again.accepted and "qa" not in roles.calls
    assert (persisted.condition, persisted.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    assert len(armed(store)) == 1
    stated = datetime.fromisoformat(persisted.retry_after).astimezone(timezone.utc)
    assert (stated.hour, stated.minute) == (5, 21)                            # 8:20am +03:00, plus the margin


def test_a_resumed_developer_on_a_pane_that_is_still_limited_is_parked_again_not_sent_to_qa(
        tmp_path, repo, monkeypatch):
    """Finding F1 of the review of draft v1, through the real controller and resume
    loop (A5 clause 2: a live limit never reaches QA): the developer is parked, the
    wait ends early, and the pane that is prompted again still shows the limit's
    status area, before and after the turn. Draft v1 spent that line against the
    baseline and booked the turn as finished; the candidate went to QA. It is
    parked again, with a timer. Kills E11."""
    ctrl, state, store, roles, fake = build(tmp_path, repo, monkeypatch, screen(*LIMIT_BOTTOM))
    ctrl.run_iteration(state)
    assert roles.calls == ["planner", "developer"]
    due = datetime.now(timezone.utc) + timedelta(hours=30)
    [rec] = resumeloop.resume_runs(tmp_path / "runs", now=due)
    assert rec["action"] == "resumed"
    persisted = store.read_state()
    again = Controller(store, roles, spec_path=tmp_path / "spec.md").run_iteration(persisted)
    assert not again.accepted and "qa" not in roles.calls
    assert roles.calls.count("developer") == 2
    assert (persisted.condition, persisted.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    assert len(armed(store)) == 1
    assert not [h for h in persisted.history if "already on the pane before the prompt" in h]
