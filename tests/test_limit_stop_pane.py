"""P3-08 slice 4 (T-UL1): a usage-limit stop in a role pane is a quota park, never an
approval dialog.

The finding. On 2026-10-15 the planner and QA panes of run mgrb2 showed Claude Code's
weekly-limit banner and, under it, the menu the harness opens after it (`/rate-limit-options`,
`1. Stop and wait for limit to reset`). Herdr reports a pane that waits for a key as
`blocked`; every `blocked` branch of the dispatcher raised `WaitingForApproval` without
reading the screen, the controller classed that as NOT_INFRASTRUCTURE, and the run waited
three days for an "approval" nobody could give, until a person unblocked it by hand.

These tests drive the real `HerdrDispatcher`, the real controller, the resume loop and the
provider-health reader on fakes: the agent runtime is a Python object (`FakeControl`), so no
Herdr and no agent is ever started and nothing is written outside `tmp_path`. The pane texts
named *recorded* are those of run mgrb2 (the paths of the run removed); the permission dialog
is the shape Claude Code draws for a Bash command, and the claim made about it is only that it
is not the limit's menu.

Mutant ids in a docstring (`M..`) are the spec's table of mutations the integration review
applies; a test that names none is a negative control or pins a stated limit (`L..`).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hoh import cli, dispatchrecovery, failures, health, quota, resumeloop
from hoh.agentcontrol import AgentInfo
from hoh.contracts import Budgets, Condition, Role, RunState, Stage
from hoh.controller import (AnswerMissing, Controller, DispatchError, PaneNotClean,
                            WaitingForApproval)
from hoh.dispatchers import HerdrDispatcher, _Base
from hoh.failures import NormalizedFailure as NF
from hoh.store import RunStore
from hoh.timers import TimerKind, TimerStatus, TimerStore

TARGET = "w0:p1"
RULE = "─" * 150

# --------------------------------------------------------------------------- #
# Panes
# --------------------------------------------------------------------------- #

WEEKLY = "  ⎿  You've hit your weekly limit · resets Oct 18, 12pm (Europe/Istanbul)"
SESSION = "  ⎿  You've hit your session limit · resets 3:20am (Europe/Istanbul)"
FIVE_HOUR = "  ⎿  You've hit your 5-hour limit · resets 8pm (Europe/Istanbul)"
CLOSING = "✻ Sautéed for 0s · done 5:14 AM"
ECHO = "❯ /rate-limit-options"
#: What the role prompt leaves above the banner (the tail of the answer-file instruction).
ABOVE = [
    "  Only the JSON object, nothing else -- no code fence, no surrounding text. Reply",
    "  in the chat with a single sentence saying that the file is written. The file is",
    "  authoritative; your chat text is not evaluated.",
]


def screen(*lines: str, scrollback: int = 0) -> str:
    """A pane as `herdr agent read` writes it: some earlier output, then `lines`."""
    older = [f"older output {n}" for n in range(scrollback)]
    return "\n".join([*older, *lines]) + "\n"


def view_of(text: str) -> str:
    """What `HerdrDispatcher._pane_view` builds from it."""
    body = "\n".join(ln for ln in text.splitlines() if ln.strip())[-1200:]
    return f"--- Visible in pane ---\n{body}\n-----------------------"


def carried(exc: BaseException) -> list[str]:
    """The lines a limit error carries between its pane markers."""
    lines = str(exc).splitlines()
    return lines[2:-1]


def menu_under(resets: str) -> list[str]:
    """The menu Claude Code opens after a limit banner (recorded, run mgrb2)."""
    return [
        "  What do you want to do?",
        "  ❯ 1. Stop and wait for limit to reset",
        f"    2. Wait here, then continue automatically at {resets}",
        "    3. Switch to usage credits",
        "  Enter to confirm · Esc to cancel",
    ]


def limit_pane(banner: str = WEEKLY, resets: str = "Oct 18, 12pm", *, between=None) -> str:
    """Recorded (planner and QA panes of run mgrb2, 2026-10-15 02:14Z and 02:15Z): the
    banner, the turn's closing line, the echoed command, the rule, the menu."""
    return screen(*ABOVE, banner, *(between or [CLOSING, ECHO, RULE]), *menu_under(resets))


#: A permission dialog of Claude Code (a Bash command): a question a person answers.
PERMISSION = [
    " Bash command",
    "",
    "   git status --short",
    "   Show the working tree status",
    "",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. Yes, and don't ask again for git status commands in /work",
    "   3. No, and tell Claude what to do differently (esc)",
]
#: The pane of a limit that has passed: its banner is still in the scrollback, and the role
#: went on and now waits at a real dialog.
STALE_THEN_DIALOG = screen(WEEKLY, CLOSING, "● Continuing with the next step.", *PERMISSION)
#: The status area a limited pane draws when the turn ended on the limit (recorded, run ugt):
#: no menu. A pane Herdr calls blocked that shows only this stays what it was (L1).
STATUS_AREA = [
    "● Usage limit reached again · continuing automatically at 3:20am · esc to cancel",
    SESSION,
    "     Use your limit reset to reset it now: clau.de/reset",
    "✻ Worked for 0s · done 12:17 AM",
    RULE, "❯ ", RULE,
    "  ⚠ Usage limit reached · limit resets 3:20am",
    "    Continuing automatically at 3:20am · esc to cancel",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent",
]


# --------------------------------------------------------------------------- #
# U -- the reader of a blocked pane
# --------------------------------------------------------------------------- #

LIMITS = [
    ("recorded-weekly-planner", limit_pane(), WEEKLY),
    ("recorded-weekly-qa", limit_pane(between=["✻ Baked for 0s · done 5:14 AM", ECHO, RULE]),
     WEEKLY),
    ("session-limit-with-the-reset-hint",
     limit_pane(SESSION, "3:20am", between=[
         "     Use your limit reset to reset it now: clau.de/reset",
         "✻ Worked for 0s · done 12:17 AM", ECHO, RULE]), SESSION),
    ("five-hour-limit", limit_pane(FIVE_HOUR, "8pm"), FIVE_HOUR),
    ("closing-line-with-a-shell-still-running",
     limit_pane(between=["✻ Cooked for 1m 3s · done 5:14 AM · 1 shell still running",
                         ECHO, RULE]), WEEKLY),
    ("the-pointer-on-another-option",
     screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE,
            "  What do you want to do?", "    1. Stop and wait for limit to reset",
            "  ❯ 2. Wait here, then continue automatically at Oct 18, 12pm",
            "    3. Switch to usage credits", "  Enter to confirm · Esc to cancel"), WEEKLY),
    ("a-rule-of-exactly-eight", limit_pane(between=[CLOSING, ECHO, "\u2500" * 8]), WEEKLY),
    ("the-heavy-rule-is-accepted-though-unrecorded",
     limit_pane(between=[CLOSING, ECHO, "\u2501" * 150]), WEEKLY),
    ("an-older-question-above-the-banner-the-last-one-counts",
     screen("  What do you want to do?", WEEKLY, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm")), WEEKLY),
    ("crlf-line-endings", limit_pane().replace("\n", "\r\n"), WEEKLY),
    ("non-breaking-spaces-in-the-menu", limit_pane().replace("  What do you want", "\u00a0\u00a0What do you want")
     .replace("    3. Switch", "\u00a0\u00a0\u00a0\u00a03.\u00a0Switch"), WEEKLY),
    ("a-banner-with-an-api-429-body-an-unrecorded-pairing",
     limit_pane('  ⎿  API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'),
     '  ⎿  API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'),
]


@pytest.mark.parametrize("text,banner", [c[1:] for c in LIMITS], ids=[c[0] for c in LIMITS])
def test_the_limits_own_menu_under_a_banner_is_read(text, banner):
    """The recorded shapes of a blocked pane that waits in the limit's menu: the banner is
    the one line carried, in the words of the screen. Kills M8 (the date is not a banner
    segment), M9 (the qualifier stays one plain word), M39 (the closing line is not in the
    closed set between banner and menu), M40 (the echoed command), M41 (the rule), M42 (the
    hint under the menu)."""
    assert failures.blocked_screen_banners(view_of(text)) == [banner]
    assert failures.blocked_screen_banners(text) == [banner]


def test_the_banner_is_carried_alone_and_the_class_follows_from_it():
    """The error carries the banner line and nothing else of the screen, so a role's words on
    it can neither turn a usage-limit wait into an operator action nor move the retry. Kills
    M6 (the whole screen is carried)."""
    text = screen("Invalid API key.", "The limit resets at 9pm (Pacific/Kiritimati), I read.",
                  *ABOVE, WEEKLY, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm"))
    assert failures.classify_text("x\n" + view_of(text)) is NF.AUTH_EXPIRED     # the failed-turn reader
    [banner] = failures.blocked_screen_banners(view_of(text))
    assert banner == WEEKLY
    exc = DispatchError("planner is stopped by a provider's limit screen in pane w0:p1\n"
                        f"--- Visible in pane ---\n{banner}\n-----------------------")
    assert failures.classify(exc) is NF.USAGE_LIMIT


NOT_THE_LIMITS_MENU = [
    ("nothing", ""),
    ("empty-pane", "(empty)\n"),
    ("an-unreadable-pane", "(pane not readable: no such pane)\n"),
    ("a-permission-dialog-alone", screen(*ABOVE, *PERMISSION)),
    ("a-stale-banner-and-then-a-real-dialog", STALE_THEN_DIALOG),
    ("a-banner-directly-above-a-real-dialog", screen(WEEKLY, *PERMISSION)),
    ("the-status-area-without-a-menu-pinned-L1", screen(*STATUS_AREA)),
    ("the-menu-without-a-banner-in-view-pinned-L2",
     screen(*ABOVE, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm"))),
    ("a-roles-line-between-banner-and-menu",
     screen(*ABOVE, WEEKLY, "● I will wait for the reset.", CLOSING, ECHO, RULE,
            *menu_under("Oct 18, 12pm"))),
    ("a-roles-sentence-instead-of-a-banner",
     screen(*ABOVE, "  ⎿  Usage limit reached · resets soon", CLOSING, ECHO, RULE,
            *menu_under("Oct 18, 12pm"))),
    ("prose-that-starts-like-a-banner",
     screen(*ABOVE, "● Rate limit (60 rpm) added.", CLOSING, ECHO, RULE,
            *menu_under("Oct 18, 12pm"))),
    ("a-fourth-option-nobody-recorded-pinned-L3",
     screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm")[:-1],
            "    4. Upgrade your plan", "  Enter to confirm · Esc to cancel")),
    ("a-line-under-the-hint",
     screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm"),
            "  Do you want to proceed?")),
    ("a-dialog-under-the-menu",
     screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm")[:-1], *PERMISSION)),
    ("a-menu-that-offers-no-stop",
     screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE, "  What do you want to do?",
            "  ❯ 1. Wait here, then continue automatically at Oct 18, 12pm",
            "    2. Switch to usage credits")),
    ("a-wrapped-option-line-pinned-L4",
     screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE, "  What do you want to do?",
            "  ❯ 1. Stop and wait for limit to reset",
            "    2. Wait here, then continue automatically at", "       Oct 18, 12pm",
            "    3. Switch to usage credits", "  Enter to confirm · Esc to cancel")),
    ("a-rule-of-seven", limit_pane(between=[CLOSING, ECHO, "\u2500" * 7])),
    ("a-nul-in-the-question", limit_pane().replace("What do you want", "What do you\x00 want")),
    ("a-lone-surrogate-in-the-banner", limit_pane().replace("weekly", "week\ud800ly")),
    ("the-question-in-lower-case", limit_pane().replace("What do you want to do?", "what do you want to do?")),
    ("an-option-with-a-prefix-word",
     limit_pane().replace("1. Stop and wait", "1. Please Stop and wait")),
    ("a-gap-of-other-lines-between-banner-and-menu",
     screen(WEEKLY, CLOSING, ECHO, RULE, *[f"  filler {n}" for n in range(7)],
            *menu_under("Oct 18, 12pm"))),
]


@pytest.mark.parametrize("text", [c[1] for c in NOT_THE_LIMITS_MENU],
                         ids=[c[0] for c in NOT_THE_LIMITS_MENU])
def test_everything_but_the_limits_own_menu_stays_a_dialog(text):
    """A permission dialog, a stale banner above a real dialog, a status area with no menu
    (the pane the earlier slices pinned as an approval route, L6 of P2-43d), a menu without
    its banner, a line of a role in between, an option nobody recorded, anything under the
    hint: no banner is read and the pane is the dialog it always was. Kills M2 (the lines under
    the question are not examined: the fourth-option, wrapped-option, line-under-the-hint and
    dialog-under-the-menu rows), M3 (the menu's identity is not checked), M4 (any line may sit
    between banner and menu), M5 (a menu without its banner is carried), M7 (a banner above
    the whole screen is enough: the stale-banner rows)."""
    assert failures.blocked_screen_banners(view_of(text)) == []
    assert failures.blocked_screen_banners(text) == []


def test_the_menu_must_be_in_the_final_screen_only():
    """The window is the last `FINAL_SCREEN_LINES` non-blank lines, as for every reader of
    this module: an old menu further up says nothing about what the pane waits for now, and a
    banner further up than the window is not reached, even through a chain of the closed set
    (short rules here, so that the 1200 characters of the pane view do not decide). Kills M1
    (the window dropped)."""
    old = screen(*ABOVE, WEEKLY, CLOSING, ECHO, RULE, *menu_under("Oct 18, 12pm"))
    later = screen(old, *[f"\u25cf step {n}" for n in range(failures.FINAL_SCREEN_LINES)],
                   *PERMISSION)
    assert failures.blocked_screen_banners(view_of(later)) == []
    short = "\u2500" * 20
    menu = menu_under("Oct 18, 12pm")
    twelfth = screen(WEEKLY, *[short] * (failures.FINAL_SCREEN_LINES - 1 - len(menu)), *menu)
    assert len(failures.final_screen(twelfth)) == failures.FINAL_SCREEN_LINES
    assert failures.blocked_screen_banners(twelfth) == [WEEKLY]             # the 12th line from the end
    thirteenth = screen(WEEKLY, *[short] * (failures.FINAL_SCREEN_LINES - len(menu)), *menu)
    assert failures.blocked_screen_banners(thirteenth) == []                # the 13th: out of the window
    assert failures.blocked_screen_banners(view_of(thirteenth)) == []


def test_a_role_that_spells_the_whole_pattern_is_read_as_the_menu_pinned_L10():
    """Residual, stated in the spec: a role whose own output is the banner, the closing line,
    the rule, the question and the three options, as the last lines of a pane that waits for a
    key, is read as the menu. The park it earns is bounded by the stated reset and the resume
    attempts (`test_a_provider_that_stays_limited...`). If this changes, say so in the spec."""
    spelled = screen("older output", "\u25cf Here is what the screen looks like:", "",
                     *limit_pane().splitlines())
    assert failures.blocked_screen_banners(view_of(spelled)) == [WEEKLY]


def test_a_rule_wider_than_the_pane_view_pushes_the_banner_out_pinned_L9():
    """The decision reads the same pane view the message shows (`_pane_view`: the last 40 lines,
    cut to their last 1200 characters). A rule wider than the view pushes the banner out of it:
    the pane is then the dialog it always was. Kills nothing of this slice's mutants: it pins
    the stated limit."""
    wide = limit_pane(between=[CLOSING, ECHO, "\u2500" * 1100])
    assert failures.blocked_screen_banners(wide) == [WEEKLY]                # the whole screen
    assert failures.blocked_screen_banners(view_of(wide)) == []             # the framed view


def test_the_reader_of_a_finished_turn_is_unchanged_by_the_menu():
    """`final_screen_limit` (a turn that ended normally) and the older pinned residual stay
    as they were: a blocked status area with no menu is still read there as a limit."""
    assert failures.final_screen_limit(screen(*STATUS_AREA)) is NF.USAGE_LIMIT
    assert failures.final_screen_limit(limit_pane()) is NF.USAGE_LIMIT


# --- the banner shapes this slice adds ------------------------------------- #

NEW_SHAPES = [
    ("dated-weekly-banner", WEEKLY, NF.USAGE_LIMIT),
    ("dated-banner-without-the-year-or-zone-cut-at-the-day",
     "  ⎿  You've hit your weekly limit · resets Oct 18,", NF.USAGE_LIMIT),
    ("dated-banner-cut-at-the-hour",
     "  ⎿  You've hit your weekly limit · resets Oct 18, 12pm", NF.USAGE_LIMIT),
    ("full-month-and-ordinal",
     "  ⎿  You've hit your weekly limit · resets October 18th, 12pm (Europe/Istanbul)",
     NF.USAGE_LIMIT),
    ("hyphenated-qualifier", FIVE_HOUR, NF.USAGE_LIMIT),
    ("two-word-qualifier",
     "  ⎿  You've hit your Opus weekly limit · resets Oct 18, 12pm (Europe/Istanbul)",
     NF.USAGE_LIMIT),
    ("status-area-with-a-date-an-unrecorded-shape",
     "  ⚠ Usage limit reached · limit resets Oct 18, 12pm", NF.USAGE_LIMIT),
]


@pytest.mark.parametrize("line,expected", [c[1:] for c in NEW_SHAPES], ids=[c[0] for c in NEW_SHAPES])
def test_the_banner_shapes_of_a_weekly_limit_are_read_and_classified(line, expected):
    """The banner reader takes the dated reset and the hyphenated or two-word qualifier, and
    the classifier of a failed dispatch reads the same line as the same class (a shape the
    reader takes that the classifier does not know would end as UNCLASSIFIED and fall to the
    generic block). Kills M8 (the date is not a segment), M9 (the banner reader keeps one plain
    word as the qualifier), M10 (the classifier does)."""
    text = screen(line)
    assert failures.final_screen_limit(text) is expected
    exc = DispatchError("x\n" + view_of(text))
    assert failures.classify(exc) is expected


NOT_BANNERS = [
    ("not-a-month", "  ⎿  You've hit your weekly limit · resets Octopus 5, 5pm"),
    ("a-word-not-a-date", "  ⎿  You've hit your weekly limit · resets soon"),
    ("date-and-a-sentence",
     "  ⎿  You've hit your weekly limit · resets Oct 18, 12pm, so the client waits"),
    ("a-qualifier-of-three-words",
     "  ⎿  You've hit your very long named limit · resets Oct 18, 12pm"),
    ("the-other-separator-is-not-recorded-pinned-L6",
     "  ⎿  You've hit your weekly limit ∙ resets Oct 18, 12pm"),
    ("limit-reached-with-a-qualifier-is-not-recorded-pinned-L7",
     "  ⎿  Weekly limit reached · resets Oct 18, 12pm"),
]


@pytest.mark.parametrize("line", [c[1] for c in NOT_BANNERS], ids=[c[0] for c in NOT_BANNERS])
def test_what_is_not_a_recorded_banner_shape_is_not_read(line):
    """The reader stays a closed set of recorded shapes (L6, L7 are the two pinned edges)."""
    assert failures.final_screen_limit(screen(line)) is None


# --------------------------------------------------------------------------- #
# Q -- the reset a weekly banner states
# --------------------------------------------------------------------------- #

def stated(text: str, now: datetime) -> datetime:
    return quota.next_attempt_at(text, now=now)


BANNER_TEXT = f"x\n--- Visible in pane ---\n{WEEKLY}\n-----------------------"
PARKED_AT = datetime(2026, 10, 15, 2, 15, tzinfo=timezone.utc)


def test_the_dated_reset_of_a_weekly_banner_is_the_reset_in_its_zone_plus_the_margin():
    """The incident: parked 2026-10-15 02:15Z, `resets Oct 18, 12pm (Europe/Istanbul)` is
    2026-10-18 09:00Z; the margin is the one every stated time carries. Before this slice
    the text stated no time `quota` could read and the wait was the default hour, five
    attempts of it. Kills M27 (the date is ignored: the wait is `DEFAULT_WAIT`), M43 (the named
    zone is ignored), M28 (the margin is dropped)."""
    assert stated(BANNER_TEXT, PARKED_AT) == datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)
    assert stated(BANNER_TEXT, PARKED_AT) != PARKED_AT + quota.DEFAULT_WAIT


@pytest.mark.parametrize("hour,expected_utc", [("12am", (2026, 10, 17, 21, 1)),
                                               ("12pm", (2026, 10, 18, 9, 1)),
                                               ("1pm", (2026, 10, 18, 10, 1)),
                                               ("11pm", (2026, 10, 18, 20, 1))])
def test_the_twelve_hour_clock_is_read_as_it_is_printed(hour, expected_utc):
    """`12am` is midnight and `12pm` is noon. Kills M31 (`% 12` dropped)."""
    text = BANNER_TEXT.replace("12pm", hour)
    assert stated(text, PARKED_AT) == datetime(*expected_utc, tzinfo=timezone.utc)


def test_a_minute_is_read_and_a_bad_minute_is_no_evidence():
    ok = BANNER_TEXT.replace("12pm", "12:30pm")
    assert stated(ok, PARKED_AT) == datetime(2026, 10, 18, 9, 31, tzinfo=timezone.utc)
    bad = BANNER_TEXT.replace("12pm", "12:75pm")
    assert stated(bad, PARKED_AT) == PARKED_AT + quota.DEFAULT_WAIT


def test_the_reset_must_lie_ahead_and_within_a_week_and_a_day_exactly():
    """The bound of the park: at most `MAX_STATED_DATE_WAIT` ahead (8 days), else the stamp is
    no evidence and the default wait applies -- so a typo or a role's date can never park a
    run for good. Pinned on both sides of the bound, and for a reset already past. Kills M29
    (the bound is dropped), M30 (the bound is 9 days), M34 (a past reset is taken)."""
    reset = datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)
    assert quota.MAX_STATED_DATE_WAIT == timedelta(days=8)
    assert stated(BANNER_TEXT, reset - timedelta(days=8)) == reset
    just_over = reset - timedelta(days=8) - timedelta(seconds=1)
    assert stated(BANNER_TEXT, just_over) == just_over + quota.DEFAULT_WAIT
    assert stated(BANNER_TEXT, reset) == reset + quota.DEFAULT_WAIT
    late = reset + timedelta(hours=5)
    assert stated(BANNER_TEXT, late) == late + quota.DEFAULT_WAIT


def test_the_year_is_found_across_new_year():
    text = BANNER_TEXT.replace("Oct 18", "Jan 3")
    now = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
    assert stated(text, now) == datetime(2027, 1, 3, 9, 1, tzinfo=timezone.utc)


SPELLINGS = [
    ("abbreviation-with-a-full-stop", "Oct. 18", PARKED_AT, datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)),
    ("whole-month-name", "October 18", PARKED_AT, datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)),
    ("ordinal-th", "Oct 18th", PARKED_AT, datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)),
    ("ordinal-st-and-the-next-month", "Nov 1st", datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 11, 1, 9, 1, tzinfo=timezone.utc)),
    ("sept", "Sept 30", datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 9, 30, 9, 1, tzinfo=timezone.utc)),
    ("sep", "Sep 30", datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 9, 30, 9, 1, tzinfo=timezone.utc)),
    ("whole-september", "September 30th", datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 9, 30, 9, 1, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize("spelling,now,expected", [c[1:] for c in SPELLINGS], ids=[c[0] for c in SPELLINGS])
def test_the_spellings_of_a_date_are_read(spelling, now, expected):
    """The banner's day may be spelled with a full stop, a whole month name, `Sept` and an
    ordinal; each reads the same moment. Kills M56 (no ordinal), M57 (no whole month name),
    M58 (no `Sept`)."""
    assert stated(BANNER_TEXT.replace("Oct 18", spelling), now) == expected


def test_the_moment_comes_back_in_the_zone_of_now():
    """`next_attempt_at` answers in the zone `now` was given in (the same instant): a caller
    that compares or prints it keeps its own offset. Kills M59 (always UTC)."""
    offset = timezone(timedelta(hours=-5))
    answer = stated(BANNER_TEXT, PARKED_AT.astimezone(offset))
    assert answer == datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)
    assert answer.utcoffset() == timedelta(hours=-5)


def test_the_zone_must_follow_the_date_directly_pinned_L16():
    """Stated limit: the zone is read only when it stands directly after the date's hour on
    the same line. A zone further along the line, or on another banner line, is not the date's
    (L16); the older reader then decides -- here the time of day of the second banner line
    (3:20am Istanbul is 2026-10-16 00:21Z from the park). Kills M60 (the zone is searched
    for further along the line), M61 (a zone from any banner line)."""
    far = BANNER_TEXT.replace("12pm (Europe/Istanbul)", "12pm so wait (Europe/Istanbul)")
    assert stated(far, PARKED_AT) == PARKED_AT + quota.DEFAULT_WAIT
    other_line = ("x\n--- Visible in pane ---\n"
                  "  ⎿  You've hit your weekly limit · resets Oct 18, 12pm\n"
                  f"{SESSION}\n-----------------------")
    assert stated(other_line, PARKED_AT) == datetime(2026, 10, 16, 0, 21, tzinfo=timezone.utc)


def test_a_wall_time_the_clock_change_repeats_is_read_at_its_first_occurrence():
    """1:30am happens twice on the night the clocks go back (2026-11-01 in New York); the
    earlier occurrence is the reset (the same choice `_stated_clock` makes: the earliest
    moment ahead). Kills M48 (the later occurrence)."""
    text = BANNER_TEXT.replace("Oct 18, 12pm", "Nov 1, 1:30am").replace("Europe/Istanbul", "America/New_York")
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    assert stated(text, now) == datetime(2026, 11, 1, 5, 31, tzinfo=timezone.utc)      # 01:30 EDT + the margin


@pytest.mark.parametrize("text", [
    BANNER_TEXT.replace(" (Europe/Istanbul)", ""),
    BANNER_TEXT.replace("Europe/Istanbul", "Nowhere/Atlantis"),
    BANNER_TEXT.replace("Oct 18", "Feb 30"),
    BANNER_TEXT.replace("Oct 18", "Oct 0"),
    BANNER_TEXT.replace("12pm", "13pm"),
    BANNER_TEXT.replace("12pm", "0pm"),
], ids=["no-zone", "unknown-zone", "no-such-day", "day-zero", "hour-13", "hour-0"])
def test_a_dated_stamp_that_cannot_be_read_is_no_evidence(text):
    """Fail closed to the existing backoff, never to a guess. Kills M32 (a missing zone is
    read in UTC), M36 (an impossible day raises)."""
    assert stated(text, PARKED_AT) == PARKED_AT + quota.DEFAULT_WAIT


def test_a_role_line_that_looks_like_a_date_does_not_move_the_wait():
    """Only lines that open like a provider banner are read (the rule `_stated_clock`
    already keeps): a role's own sentence about a reset, with its own zone, is not. Kills
    M33 (every line is read)."""
    text = ("x\n--- Visible in pane ---\n"
            "The limit resets Oct 20, 5pm (Pacific/Kiritimati), I read.\n"
            f"{WEEKLY}\n-----------------------")
    assert stated(text, PARKED_AT) == datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)


def test_the_banners_stated_time_of_day_is_still_read_as_before():
    """The negative control of the dated reader: the recorded session banner."""
    text = f"x\n--- Visible in pane ---\n{SESSION}\n-----------------------"
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert stated(text, now).isoformat() == "2026-10-10T00:21:00+00:00"


# --------------------------------------------------------------------------- #
# D -- every blocked branch of the dispatcher, on a fake runtime
# --------------------------------------------------------------------------- #

class FakeControl:
    """Stands in for the agent runtime (`dispatcher.control`). `statuses` is what the next
    status reads say (the last one repeats), `prompt_status` what a prompt that returned
    says, `view` the pane the diagnosis reads, `box` what the input-box reads show; a
    `prompt_error` is raised by the next prompt only."""

    backend = "fake"

    def __init__(self, view: str, *, statuses=("idle",), prompt_status: str = "idle",
                 box: str = "", rows=None) -> None:
        self.view = view
        self.statuses = list(statuses)
        self.prompt_status = prompt_status
        self.box = box
        self.rows = rows or []
        self.prompt_error: BaseException | None = None
        self.calls: list[str] = []
        self.pressed: list[str] = []

    def info(self, status: str, name: str = TARGET) -> AgentInfo:
        return AgentInfo("fake", name, status, TARGET, "w0:t1", "w0", "/x")

    def status(self, name, *, purpose=""):
        self.calls.append("status")
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return self.info(status, name)

    def screen(self, name, *, purpose=""):
        self.calls.append("screen")
        return self.box

    def pane_screen(self, pane_id, *, source="visible", purpose=""):
        self.calls.append("pane_screen")
        return self.box

    def transcript(self, name, *, lines=120):
        self.calls.append("transcript")
        if isinstance(self.view, BaseException):
            raise self.view
        return self.view

    def prompt(self, name, text, *, timeout_ms, purpose=""):
        self.calls.append("prompt")
        error, self.prompt_error = self.prompt_error, None
        if error is not None:
            raise error
        return self.info(self.prompt_status, name)

    def press(self, pane_id, key, *, purpose=""):
        self.calls.append("press")
        self.pressed.append(key)

    def snapshot(self):
        return {"agents": self.rows}


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


def make_dispatcher(root: Path, monkeypatch, control: FakeControl, *, profiles=None):
    """(store, state, dispatcher): a Herdr-free `HerdrDispatcher` over `control`."""
    store, state = make_state(root)
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(d, answers_dir=store.dir / "answers", profiles=profiles or {})
    d.cwd = str(root / "repo")
    d.timeout_ms = 1000
    d.__dict__["_control"] = control
    monkeypatch.setattr(d, "_ensure_agent", lambda role, st: TARGET)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return store, state, d


def attempt(call):
    try:
        return call(), None
    except Exception as exc:                       # noqa: BLE001 - the test inspects it
        return None, exc


ROLES = [Role.PLANNER, Role.DEVELOPER, Role.QA]
STALLED_BOX = ("✻ Baked for 0s\n" + "─" * 88 + "\n❯ p\n" + "─" * 88
               + "\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")


def drive_turn_ends_blocked(root, monkeypatch, view, role):
    """The incident: the prompt returned and the pane waits for a key (`_dispatch`)."""
    control = FakeControl(view, prompt_status="blocked")
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    return attempt(lambda: d.dispatch(role, "p", state=state)), control


def drive_earlier_prompt_did_not_return(root, monkeypatch, view, role):
    """The pane was left with an unreturned prompt and now waits for a key (`_settle`)."""
    control = FakeControl(view, statuses=("blocked",))
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    control.prompt_error = DispatchError("the prompt did not return")
    _, first = attempt(lambda: d.dispatch(role, "p", state=state))
    assert type(first) is DispatchError
    return attempt(lambda: d.dispatch(role, "p", state=state)), control


def drive_stalled_prompt_before_enter(root, monkeypatch, view, role):
    """The prompt stalled, stayed typed in the box, and the pane now waits for a key
    (`_submit_stalled`, before Enter)."""
    control = FakeControl(view, statuses=("idle", "blocked"), box=STALLED_BOX)
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    d.own_panes = [TARGET]
    control.prompt_error = DispatchError("agent_prompt_stalled", retry_after=5.0)
    _, first = attempt(lambda: d.dispatch(Role.DEVELOPER, "p", state=state))
    assert first is not None and first.retry_after == 5.0
    return attempt(lambda: d.dispatch(Role.DEVELOPER, "p", state=state)), control


def drive_new_agent_before_its_first_prompt(root, monkeypatch, view, role):
    """A new agent waits for a key before it was ever prompted (`_await_ready`)."""
    control = FakeControl(view, statuses=("blocked",))
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    d._fresh().add(TARGET)
    return attempt(lambda: d.dispatch(role, "p", state=state)), control


def drive_restored_agent(root, monkeypatch, view, role):
    """A pane Herdr restored waits for a key (`_check_restored`)."""
    from hoh import dispatchers

    control = FakeControl(view, statuses=("blocked",))
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    d._unsettled()[TARGET] = (dispatchers.RESTORED, 0.0, None)
    return attempt(lambda: d.dispatch(role, "p", state=state)), control


def drive_agent_name_taken(root, monkeypatch, view, role):
    """The agent's name is taken by a restored agent of this run that waits for a key
    (`_reusable_agent`)."""
    row = {"name": "hb-planner", "agent_status": "blocked", "pane_id": TARGET,
           "tab_id": "w0:t1", "workspace_id": "w0", "foreground_cwd": str(root / "repo")}
    control = FakeControl(view, rows=[row])
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    return attempt(lambda: d._reusable_agent("hb-planner", state)), control


def missing_answer(d, state, role):
    return AnswerMissing("the turn ended without the answer file",
                         answer_path=d._answer_path(role, state), target=TARGET,
                         prompt_sha256="0" * 64, prompt_bytes=1, prompt_file=None,
                         sent_at=time.monotonic())


def drive_follow_up_status(root, monkeypatch, view, role):
    """The turn ended without its answer and the pane waits for a key (`follow_up`, status)."""
    control = FakeControl(view, statuses=("blocked",))
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    missing = missing_answer(d, state, role)
    return attempt(lambda: d.follow_up(role, state, missing, charge=lambda: None)), control


def drive_follow_up_turn(root, monkeypatch, view, role):
    """The follow-up itself ended on a pane that waits for a key (`follow_up`, turn)."""
    control = FakeControl(view, statuses=("idle",), prompt_status="blocked")
    _store, state, d = make_dispatcher(root, monkeypatch, control)
    missing = missing_answer(d, state, role)
    return attempt(lambda: d.follow_up(role, state, missing, charge=lambda: None)), control


SITES = [
    ("the-turn-ends-blocked", drive_turn_ends_blocked, ROLES),
    ("an-earlier-prompt-did-not-return", drive_earlier_prompt_did_not_return, ROLES),
    ("a-stalled-prompt-before-enter", drive_stalled_prompt_before_enter, [Role.DEVELOPER]),
    ("a-new-agent-before-its-first-prompt", drive_new_agent_before_its_first_prompt, ROLES),
    ("a-restored-agent", drive_restored_agent, ROLES),
    ("an-agent-name-taken", drive_agent_name_taken, [Role.PLANNER]),
    ("the-follow-up-status", drive_follow_up_status, [Role.PLANNER, Role.QA]),
    ("the-follow-up-turn", drive_follow_up_turn, [Role.PLANNER, Role.QA]),
]
CASES = [pytest.param(drive, role, id=f"{name}-{role.value}")
         for name, drive, roles in SITES for role in roles]


@pytest.mark.parametrize("drive,role", CASES)
def test_a_blocked_pane_that_shows_the_limits_menu_is_a_wait_not_a_dialog(
        tmp_path, monkeypatch, drive, role):
    """All eight places the dispatcher meets a pane Herdr calls `blocked`, for the planner,
    the developer and QA (the analyst of a double-down runs in the planner's slot, so it takes
    the planner rows): the error is not a `WaitingForApproval`, it is of the class the banner
    names, it carries that banner and nothing else, it is not transient, and nothing was
    sent into the menu. Kills M16..M23 (each site left as it was, one mutant per site), M11 (`_blocked` never finds a
    limit), M24 (`_await_ready` counts the stop as a failed read), M25 (the stop is a kind of
    approval wait), M26 (it is transient), M6 and M13 (more than the banner is carried)."""
    (result, exc), control = drive(tmp_path, monkeypatch, limit_pane(), role)
    assert result is None
    assert isinstance(exc, DispatchError), repr(exc)
    assert not isinstance(exc, (WaitingForApproval, PaneNotClean, AnswerMissing)), repr(exc)
    assert exc.transient is False and exc.retry_after == 0 and exc.pane_kept_open is False
    assert cli._waiting_for_approval(exc) is False          # no tab is kept open for it
    assert carried(exc) == [WEEKLY]
    assert str(exc).splitlines()[1] == "--- Visible in pane ---"
    assert failures.classify(exc) is NF.USAGE_LIMIT
    assert failures.is_automatic(failures.classify(exc))
    assert control.pressed == []


@pytest.mark.parametrize("drive,role", CASES)
@pytest.mark.parametrize("view", [STALE_THEN_DIALOG, screen(*ABOVE, *PERMISSION),
                                  screen(*STATUS_AREA)],
                         ids=["stale-banner-then-a-real-dialog", "a-real-dialog",
                              "status-area-no-menu"])
def test_every_other_blocked_pane_is_the_dialog_it_always_was(
        tmp_path, monkeypatch, drive, role, view):
    """The negative control of the test above, site by site: the same fake, the same status,
    a pane that is not the limit's menu. A `WaitingForApproval`, with the sentence every
    caller keys on and the pane view under it, as before -- **byte for byte**: the one-line
    approval sentence of the site, a newline, the pane view `_pane_view` builds (and, for an
    agent whose name is taken, the sentence alone: that site never printed the pane). Kills M12
    (every blocked pane is a limit), M7 (a banner above the whole screen is enough), M49 (the
    view is cut), M50 (the newline is a space), M51 (a line is appended), M52 (the name-taken
    site prints the pane)."""
    (result, exc), control = drive(tmp_path, monkeypatch, view, role)
    assert result is None
    assert type(exc) is WaitingForApproval, repr(exc)
    assert exc.pane_kept_open is True and cli._waiting_for_approval(exc) is True
    assert "waits for an approval" in str(exc).splitlines()[0]
    assert failures.classify(exc) is NF.NOT_INFRASTRUCTURE
    text = str(exc)
    if drive is drive_agent_name_taken:
        assert "\n" not in text and "Visible in pane" not in text
    else:
        shown = view_of(view.strip())
        assert text.endswith("\n" + shown), text
        sentence = text[:-len(shown) - 1]
        assert sentence and "\n" not in sentence


@pytest.mark.parametrize("drive,role", CASES)
def test_an_unreadable_pane_is_the_dialog_it_always_was(tmp_path, monkeypatch, drive, role):
    """Fail closed (L8): a pane that cannot be read proves nothing, and the old route stays.
    Kills M12."""
    (result, exc), control = drive(tmp_path, monkeypatch, OSError("no such pane"), role)
    assert type(exc) is WaitingForApproval, repr(exc)
    assert failures.classify(exc) is NF.NOT_INFRASTRUCTURE


def test_the_pane_is_read_once_for_the_decision_and_the_message(tmp_path, monkeypatch):
    """One `agent read` where there was one: the screen that decides is the screen that is
    shown. Kills M14 (a second read for the decision)."""
    (_result, exc), control = drive_turn_ends_blocked(tmp_path / "a", monkeypatch,
                                                      limit_pane(), Role.PLANNER)
    assert control.calls.count("transcript") == 1
    (_result, exc), control = drive_turn_ends_blocked(tmp_path / "b", monkeypatch,
                                                      screen(*PERMISSION), Role.PLANNER)
    assert control.calls.count("transcript") == 1


def test_the_error_of_a_limit_stop_has_none_of_the_sentences_of_a_dialog(tmp_path, monkeypatch):
    """`failures.classify` keys on this project's own sentences before it reads a pane: a limit
    error that said `waits for an approval` or `needs approval` would be NOT_INFRASTRUCTURE
    whatever it carried. Kills M15 (the first line reuses the approval sentence)."""
    (_result, exc), _control = drive_turn_ends_blocked(tmp_path, monkeypatch, limit_pane(),
                                                       Role.PLANNER)
    first = str(exc).splitlines()[0].lower()
    for sentence in failures._OWN_SENTENCES:
        assert sentence not in first
    assert "approval" not in first
    assert "blocked" not in str(exc)       # `cli._waiting_for_approval` keeps the tabs on that word


def test_an_analyst_in_the_planners_slot_is_stopped_the_same_way(tmp_path, monkeypatch):
    """The analyst of a double-down is dispatched as the planner of a dossier
    (`doubledown.RoleAnalyst`); the class of its failed dispatch is the class of the stop."""
    from hoh.doubledown import RoleAnalyst

    control = FakeControl(limit_pane(), prompt_status="blocked")
    _store, _state, d = make_dispatcher(tmp_path, monkeypatch, control)
    _result, exc = attempt(lambda: RoleAnalyst(d).analyse("root_cause", "p", round_no=1))
    assert isinstance(exc, DispatchError) and failures.classify(exc) is NF.USAGE_LIMIT


def test_a_rate_limit_banner_over_the_menu_is_a_rate_limit_wait(tmp_path, monkeypatch):
    """The banner decides the class: an `API Error: 429` body over the same menu is a RATE_LIMIT
    wait (the five minutes of its class), not a usage limit. That pairing was never recorded
    (the body is not a recorded 429 either, `failures.py`); it is accepted because the menu's
    identity gates it (L17). Kills M11."""
    body = '  \u23bf  API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'
    (result, exc), _control = drive_turn_ends_blocked(tmp_path, monkeypatch,
                                                      limit_pane(body), Role.PLANNER)
    assert isinstance(exc, DispatchError) and not isinstance(exc, WaitingForApproval)
    assert carried(exc) == [body] and failures.classify(exc) is NF.RATE_LIMIT


def test_a_developer_that_delivered_before_the_menu_is_parked_too(tmp_path, monkeypatch):
    """A developer whose tree changed before the pane stopped is a delivery (the answer is the
    artifact), so the final screen decides (`_refuse_a_limit_screen`, P2-43d): the dated banner
    of a weekly limit is a failed dispatch of its class there too. Kills M8 (the date is not a
    banner segment)."""
    # The pane the prompt finds is healthy (a banner it already showed would be an earlier
    # limit's, P2-43e); the turn leaves it in the menu.
    control = FakeControl(screen("\u25cf Ready.", "\u276f "), prompt_status="blocked")
    _store, state, d = make_dispatcher(tmp_path, monkeypatch, control)
    original = control.prompt

    def prompt(name, text, *, timeout_ms, purpose=""):
        (tmp_path / "repo" / "partial.txt").write_text("half\n", encoding="utf-8")
        control.view = limit_pane()
        return original(name, text, timeout_ms=timeout_ms, purpose=purpose)

    control.prompt = prompt
    _result, exc = attempt(lambda: d.dispatch(Role.DEVELOPER, "p", state=state))
    assert type(exc) is DispatchError, repr(exc)
    assert carried(exc) == [WEEKLY] and failures.classify(exc) is NF.USAGE_LIMIT
    assert (tmp_path / "repo" / "partial.txt").read_text(encoding="utf-8") == "half\n"


def test_a_turn_that_was_answered_is_not_second_guessed(tmp_path, monkeypatch):
    """A role that wrote its answer and left its pane at the menu is finished: the answer is
    the answer (the rule `_note_blocked_but_answered` keeps). Kills M44 (the screen is read
    before the answer)."""
    control = FakeControl(limit_pane(), prompt_status="blocked")
    _store, state, d = make_dispatcher(tmp_path, monkeypatch, control)
    answer = d._answer_path(Role.PLANNER, state)
    original = control.prompt

    def prompt(name, text, *, timeout_ms, purpose=""):
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text('{"ok": true}', encoding="utf-8")
        return original(name, text, timeout_ms=timeout_ms, purpose=purpose)

    control.prompt = prompt
    assert d.dispatch(Role.PLANNER, "p", state=state) == '{"ok": true}'


# --------------------------------------------------------------------------- #
# E -- the real controller, the resume loop and the provider-health reader
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
    """The roles the test decides; `real` is the real dispatcher for the roles in `live`."""

    def __init__(self, repo: Path, real: HerdrDispatcher, live) -> None:
        self.repo, self.real, self.live = repo, real, set(live)
        self.calls: list[str] = []
        # What the controller reads of its dispatcher to name the provider in a receipt.
        self.profiles, self.models, self.efforts = real.profiles, real.models, real.efforts

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        self.calls.append(role.value)
        if role in self.live:
            return self.real.dispatch(role, prompt, state=state)
        if role is Role.DEVELOPER:
            (self.repo / "done.txt").write_text("ok\n", encoding="utf-8")
            return "developer run finished (status idle)"
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


def build(tmp_path: Path, repo: Path, monkeypatch, view, *, live, prompt_status="blocked"):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    store = RunStore(tmp_path / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=9))
    store.write_state(state)
    control = FakeControl(view, prompt_status=prompt_status)
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(d, answers_dir=store.dir / "answers",
                   profiles={Role.PLANNER: "claude", Role.QA: "claude", Role.DEVELOPER: "claude"})
    d.cwd = str(repo)
    d.timeout_ms = 1000
    d.__dict__["_control"] = control
    monkeypatch.setattr(d, "_ensure_agent", lambda role, st: TARGET)
    roles = Roles(repo, d, live)
    return Controller(store, roles, spec_path=spec), state, store, roles, control


def armed(store: RunStore):
    return [t for t in TimerStore(store.dir.parent).read()
            if t.status is TimerStatus.ARMED and t.kind is TimerKind.PROVIDER_RETRY
            and t.run_id == "r1"]


def history(state) -> str:
    return "\n".join(state.history)


RESET = datetime(2026, 10, 18, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def parked_at_the_incident(monkeypatch):
    """The clock of the park: 2026-10-15 02:15Z, when the planner of run mgrb2 met the limit.
    `failures.next_attempt` reads the clock at the moment of the park; fixing its `now` keeps
    the stated reset (2026-10-18 09:01Z) independent of the day the test runs."""
    real = failures.next_attempt
    monkeypatch.setattr(failures, "next_attempt",
                        lambda failure, text, now=None: real(failure, text, now=PARKED_AT))


def test_a_planner_stopped_by_the_weekly_limit_is_parked_until_the_reset(
        tmp_path, repo, monkeypatch, parked_at_the_incident):
    """The incident, end to end through the real controller: the planner's pane waits in the
    limit's menu. The run is parked as a quota wait -- `usage_limit`, the reset the banner
    states, a durable timer, a receipt of the class -- and not as a block that collects for the
    digest; no iteration is spent and no verdict is given. Kills M21 (the dispatch site as it
    was: `blocked_kind` is not `usage_limit`), M27 (the wait is the default hour)."""
    ctrl, state, store, roles, control = build(tmp_path, repo, monkeypatch, limit_pane(),
                                               live={Role.PLANNER})
    out = ctrl.run_iteration(state)
    assert not out.accepted and out.reason.startswith("no verdict")
    assert roles.calls == ["planner"]
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "usage_limit", Stage.PLANNING)
    assert (state.usage.iterations, state.usage.outages, state.usage.loops_without_progress) == (0, 0, 0)
    assert state.retry_after == RESET.isoformat()
    text = history(state)
    assert "failure class USAGE_LIMIT (AUTOMATIC) in planner" in text
    assert "waits for an approval" not in text and "collected_for_digest" not in text
    assert "candidate rejected" not in text
    [timer] = armed(store)
    assert timer.reason.startswith("usage limit: ")
    [record] = dispatchrecovery.read_records(store.dir)[0]
    assert record.kind == "ROUTING_DECISION" and record.reason == "USAGE_LIMIT"
    assert record.provenance.role == "planner" and record.retry_at == state.retry_after


def test_the_stated_reset_is_the_wait_and_the_run_wakes_without_a_person(
        tmp_path, repo, monkeypatch, parked_at_the_incident):
    """The reset the banner states, relative to when the run parked (the incident's own
    clock, `parked_at_the_incident`), is the retry: before it the
    resume loop leaves the run alone, after it the loop resumes it, bound to its receipt, and
    the next drive starts the planner again. Kills M21, M27."""
    ctrl, state, store, roles, control = build(tmp_path, repo, monkeypatch, limit_pane(),
                                               live={Role.PLANNER})
    ctrl.run_iteration(state)
    assert state.retry_after == RESET.isoformat()
    runs = tmp_path / "runs"
    early = resumeloop.resume_runs(runs, now=RESET - timedelta(hours=1))
    assert all(r["action"] != "resumed" for r in early)
    assert store.read_state().blocked_kind == "usage_limit"
    [rec] = resumeloop.resume_runs(runs, now=RESET + timedelta(minutes=1))
    assert rec["action"] == "resumed"
    assert rec["binding"] == dispatchrecovery.ParkVerdict.BOUND.value and rec["claim"] == "written"
    assert armed(store) == []
    persisted = store.read_state()
    assert persisted.condition is Condition.ACTIVE and persisted.resume_attempts == 1
    # The pane has healed; the same iteration goes on to the planner and the rest.
    control.view, control.prompt_status = screen(*ABOVE, "● Done."), "idle"
    again = resumeloop.resume_runs(runs, now=RESET + timedelta(minutes=2))
    assert all(r["action"] != "resumed" for r in again)


def test_the_provider_health_reader_sees_the_stop_as_a_limit_of_the_provider(
        tmp_path, repo, monkeypatch, parked_at_the_incident):
    """P3-08's reader (and the sentinel built on it) sees what the receipt says: the run is a
    wait on the provider's limit, attributed to the provider the dispatcher was given, with
    the stated reset. Before this slice the same stop was a block no health reader looks at."""
    ctrl, state, store, roles, control = build(tmp_path, repo, monkeypatch, limit_pane(),
                                               live={Role.PLANNER})
    ctrl.run_iteration(state)
    report = health.read_health(tmp_path / "runs", now=PARKED_AT + timedelta(hours=1))
    group = report.for_provider("claude")
    assert group is not None, json.dumps(report.as_dict())
    assert group.state is health.HealthState.LIMITED and group.runs == ("r1",)
    assert group.not_before == RESET.isoformat() and group.failures == (("USAGE_LIMIT", 1),)
    assert report.unattributed == () and report.complete


def test_qa_stopped_by_the_weekly_limit_is_a_wait_and_not_a_rejection(
        tmp_path, repo, monkeypatch, parked_at_the_incident):
    """QA's pane waits in the limit's menu after the candidate was frozen: no verdict, no
    outage, no rejected candidate, the stage stays VERIFYING, the candidate is not touched.
    Kills M21 (the dispatch site as it was: the stop is booked as 'no QA verdict')."""
    ctrl, state, store, roles, control = build(tmp_path, repo, monkeypatch, limit_pane(),
                                               live={Role.QA})
    out = ctrl.run_iteration(state)
    assert not out.accepted and out.reason.startswith("no verdict")
    assert roles.calls == ["planner", "developer", "qa"]
    assert (state.condition, state.blocked_kind, state.stage) == (
        Condition.BLOCKED, "usage_limit", Stage.VERIFYING)
    assert state.usage.outages == 0
    # No tab is kept open for it: the run's end closes its panes as for any failed dispatch
    # (`_finish_run`); only a dialog sets the flag that keeps them.
    assert not getattr(ctrl, "_waiting_for_approval", False) and not out.waiting_for_approval
    text = history(state)
    assert "candidate rejected" not in text and "NO QA VERDICT" not in text
    assert "failure class USAGE_LIMIT (AUTOMATIC) in qa" in text
    assert len(armed(store)) == 1
    assert (repo / "done.txt").read_text(encoding="utf-8") == "ok\n"


def test_a_real_dialog_in_the_planners_pane_keeps_its_approval_route(
        tmp_path, repo, monkeypatch):
    """The negative control of the three tests above, end to end: the same controller and
    dispatcher, a pane that waits at a permission dialog. The run blocks for a person, no
    timer is armed, nothing is classed as a provider wait. Kills M12."""
    ctrl, state, store, roles, control = build(
        tmp_path, repo, monkeypatch, screen(*ABOVE, *PERMISSION), live={Role.PLANNER})
    with pytest.raises(WaitingForApproval):
        ctrl.run_iteration(state)
    assert roles.calls == ["planner"]
    assert state.blocked_kind not in failures.AUTOMATIC_KINDS | {"usage_limit"}
    assert armed(store) == []


def test_a_provider_that_stays_limited_is_tried_five_times_and_then_left_to_a_person(
        tmp_path, repo, monkeypatch):
    """Termination of the park. The bound is the resume loop's own (`quota.
    MAX_RESUME_ATTEMPTS`), counted in `resume_attempts` and written with the state before
    the run is started again, so an attempt that crashes still counts. With the pane limited
    on every attempt the trail is: parked, then five resumes each followed by a new park, then
    `given_up` with an escalation receipt -- and a further pass changes no byte. Kills M37
    (the counter is not advanced: no bound), M38 (the bound is off by one), M21 (the stop does
    not park)."""
    ctrl, state, store, roles, control = build(tmp_path, repo, monkeypatch, limit_pane(),
                                               live={Role.PLANNER})
    clock = [PARKED_AT]                    # the moment of the next park: the pass that resumed it
    real = failures.next_attempt
    monkeypatch.setattr(failures, "next_attempt",
                        lambda failure, text, now=None: real(failure, text, now=clock[0]))
    ticks = iter(range(10_000))

    def utcnow() -> str:
        n = next(ticks)
        return f"2026-10-18T17:{n // 60 % 60:02d}:{n % 60:02d}Z"

    monkeypatch.setattr("hoh.controller.utcnow", utcnow)      # distinct seconds: arena names
    ctrl.run_iteration(state)
    runs = tmp_path / "runs"
    trail = []
    for k in range(quota.MAX_RESUME_ATTEMPTS + 3):
        due = RESET + timedelta(days=10 * (k + 1))
        clock[0] = due
        [rec] = resumeloop.resume_runs(runs, now=due)
        trail.append(rec["action"])
        if rec["action"] != "resumed":
            break
        assert store.read_state().resume_attempts == k + 1          # written before the attempt
        persisted = store.read_state()
        again = Controller(store, roles, spec_path=tmp_path / "spec.md").run_iteration(persisted)
        assert not again.accepted
        assert (persisted.condition, persisted.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    assert trail == ["resumed"] * quota.MAX_RESUME_ATTEMPTS + ["given_up"], trail
    [escalation] = [r for r in dispatchrecovery.read_records(store.dir)[0]
                    if r.kind == "ROUTING_ESCALATION"]
    assert escalation.reason == "USAGE_LIMIT"
    def record() -> dict[str, bytes]:
        """The bytes of the state and of every receipt file beside it."""
        files = {"state.json": (store.dir / "state.json").read_bytes()}
        files.update({f"recovery/{p.name}": p.read_bytes()
                      for p in sorted((store.dir / "recovery").glob("*")) if p.is_file()})
        return files

    settled = record()
    assert len(settled) > 1                                   # the receipts are really compared
    far = RESET + timedelta(days=900)
    [quiet] = resumeloop.resume_runs(runs, now=far)
    assert quiet["action"] == "given_up"
    assert record() == settled
    assert roles.calls.count("planner") == quota.MAX_RESUME_ATTEMPTS + 1


def run_through_the_cli(tmp_path, monkeypatch, roles) -> int:
    """`hoh run` itself -- the real parser, `cmd_run`, its controller -- over `roles` (the
    real dispatcher for the live roles): no Herdr, no agent, no host storage root."""
    import types

    from hoh import dispatchers, integrity
    from hoh import controller as controller_module

    monkeypatch.setattr(cli, "storage_admission_for", lambda option, **kw: (
        types.SimpleNamespace(ledger=tmp_path / "ledger"), tmp_path / "storage"))
    monkeypatch.setattr(cli, "_hook_steward", lambda *a, **kw: None)
    monkeypatch.setattr(integrity, "snapshot",
                        lambda repo_path: (_ for _ in ()).throw(RuntimeError("not measured")))

    class NoAdmission(controller_module.Controller):
        def __init__(self, *a, **kw):
            kw["storage_admission"] = None
            super().__init__(*a, **kw)

    monkeypatch.setattr(controller_module, "Controller", NoAdmission)
    monkeypatch.setattr(dispatchers, "build_dispatcher", lambda **kw: roles)
    return cli.main(["--root", str(tmp_path / "runs"), "run", "r1", "--no-herdr",
                     "--iterations", "1"])


def test_the_production_path_parks_the_stop_and_hoh_resume_quota_wakes_it(
        tmp_path, repo, monkeypatch, capsys, parked_at_the_incident):
    """The path production executes, from the two entry points. `hoh run` (`cli.main`, the
    real `cmd_run`) meets the planner's pane in the limit's menu and parks the run as a
    quota wait with a receipt and a timer; `hoh resume-quota` (`cli.main`, `cmd_resume_quota`)
    does nothing before the stated reset and resumes the run, bound to its receipt, after it.
    Nobody answers anything. Kills M21 (the stop is an approval wait again), M27 (the wait is
    the default hour)."""
    ctrl, state, store, roles, control = build(tmp_path, repo, monkeypatch, limit_pane(),
                                               live={Role.PLANNER})
    run_through_the_cli(tmp_path, monkeypatch, roles)
    capsys.readouterr()
    parked = store.read_state()
    assert (parked.condition, parked.blocked_kind) == (Condition.BLOCKED, "usage_limit")
    assert parked.retry_after == RESET.isoformat() and len(armed(store)) == 1
    assert "waits for an approval" not in history(parked)
    assert (store.dir / "recovery" / "routing.jsonl").is_file()

    class Clock(datetime):
        at = RESET - timedelta(hours=1)

        @classmethod
        def now(cls, tz=None):
            return cls.at if tz is None else cls.at.astimezone(tz)

    monkeypatch.setattr(resumeloop, "datetime", Clock)
    assert cli.main(["--root", str(tmp_path / "runs"), "resume-quota"]) == 0
    before = json.loads(capsys.readouterr().out)
    assert all(r.get("action") != "resumed" for r in before["results"])
    Clock.at = RESET + timedelta(minutes=1)
    assert cli.main(["--root", str(tmp_path / "runs"), "resume-quota"]) == 0
    after = json.loads(capsys.readouterr().out)
    [rec] = [r for r in after["results"] if r.get("action") == "resumed"]
    assert rec["target"] == "r1" and rec["binding"] == dispatchrecovery.ParkVerdict.BOUND.value
    assert store.read_state().condition is Condition.ACTIVE and armed(store) == []
