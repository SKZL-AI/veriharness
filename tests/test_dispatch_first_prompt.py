"""The word wrap of Claude Code's input box, and the wait before a first prompt.

Two things that blocked run qfu4 (2026-10-07, 11:39Z) and the runs before it:

1. `_same_input` (the comparison behind the stalled-submit recovery) knew only
   breaks at exactly the box width. Claude Code wraps at spaces: the pointer's
   first line, 75 characters, broke as `command-line` / `argument,` in a box 78
   columns wide, the comparison found a mismatch, refused to press Enter, and
   the run blocked. The tests pin the wrap as three recorded boxes show it
   (qaw3, qaw4, qfu4), and that nothing but that wrap is accepted.
2. `herdr agent start` returns when the agent is idle, not when its input
   takes a submit; under Claude Code 2.1.292 every first prompt sent at once
   stalled. The dispatcher now waits for an idle input box that holds still
   (`HerdrDispatcher._await_ready`) and otherwise stops, sending, writing and
   pressing nothing.

No agent is started and `herdr` is never run: `_cli` is a fake, the clock is
fake, and an autouse fixture puts a stub `herdr` first on PATH whose call log
must stay empty. Everything is written below pytest's `tmp_path`.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from hoh import dispatchers
from hoh.contracts import Role, RunState
from hoh.controller import DispatchError, PaneNotClean, WaitingForApproval
from hoh.dispatchers import HerdrDispatcher, _answer_instruction, _Base, _pointer_prompt

TARGET = "w0:p1"
RULE_CHAR = "\u2500"
MARK = "\u276f"
#: Claude Code draws a no-break space after the input mark (b-review of the
#: spec); `_pending_input` strips it with the rest of the text.
NBSP = "\u00a0"


@pytest.fixture(autouse=True)
def _herdr_is_never_run(tmp_path, monkeypatch):
    """A stub `herdr` first on PATH that records its arguments and fails. A
    test that reaches the real command line instead of the fake `_cli` leaves a
    line in the log, and fails here."""
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    log = tmp_path / "herdr-calls.log"
    script = stub / "herdr"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ.get('PATH', '')}")
    yield
    assert not log.exists() or log.read_text(encoding="utf-8") == "", log.read_text()


# --------------------------------------------------------------------------- #
# 1. The word wrap of the box
# --------------------------------------------------------------------------- #

#: Run qfu4's planner pointer, verbatim from its `blocked_reason` ("whole
#: input"), with the 40-character state-root prefix replaced by another of the
#: same length, so that every soft wrap falls where it fell in the pane. The
#: pane's bottom rule was 78 columns wide: `_box_width` is 76.
QFU4_BOX_ROWS = [
    '[prompt-file]',
    '  Your instructions for this turn are too long for one command-line',
    '  argument,',
    '  so they were written to a file. That file **is** your prompt; this message',
    '  only points to it.',
    '',
    '    path:    /srv/state/projects/example-project-0001/runs/20261007T1138Z-qf',
    '  u4-runs/20261007t1138z-qfu4/answers/i1-a0-planner.prompt.md',
    '    bytes:   50036 (UTF-8)',
    '    sha256:',
    '  56ba876bdbb0031fb453574b6fb3fec69f92ce423aefab14aefdfbd02591aa57',
    '',
    '  Read the **whole** file before you do anything else. A long file comes',
    '  back',
    '  in parts: read it page by page, passing an offset each time, and keep',
    '  going',
    '  until you have reached the end of the file -- not just its beginning. Then',
    '  follow it as your instructions. Do not change, move or delete the file:',
    '  the',
    '  dispatcher checks its SHA-256 again after your turn, and a changed file',
    '  fails',
    '  the dispatch.',
    '',
    '',
    '  [answer-file]',
    '  Write your JSON answer **as a file** to:',
    '',
    '    /srv/state/projects/example-project-0001/runs/20261007T1138Z-qfu4-runs/2',
    '  0261007t1138z-qfu4/answers/i1-a0-planner.json',
    '',
    '  Only the JSON object, nothing else -- no code fence, no surrounding text.',
    '  Reply',
    '  in the chat with a single sentence saying that the file is written. The',
    '  file is',
    '  authoritative; your chat text is not evaluated.',
]
QFU4_BOX = "\n".join(QFU4_BOX_ROWS)
QFU4_WIDTH = 76
QFU4_ANSWERS = Path("/srv/state/projects/example-project-0001/runs/"
                    "20261007T1138Z-qfu4-runs/20261007t1138z-qfu4/answers")
QFU4_SIZE = 50036
QFU4_SHA = "56ba876bdbb0031fb453574b6fb3fec69f92ce423aefab14aefdfbd02591aa57"


def _qfu4_sent() -> str:
    """What the dispatcher typed in run qfu4: the pointer and the answer
    instruction, built by the dispatcher's own functions."""
    return (_pointer_prompt(QFU4_ANSWERS / "i1-a0-planner.prompt.md", QFU4_SIZE, QFU4_SHA)
            + _answer_instruction(QFU4_ANSWERS / "i1-a0-planner.json"))


def _qfu4_view() -> str:
    """A pane view around the recorded box: the bottom rule is the recorded
    78 columns, and the mark is followed by a no-break space as in the real
    pane. (The rows above the box were not recorded; the view is rebuilt from
    the box and two rules.) With the recorded rows of qaw3 (in the robustness
    file) and qaw4 these are the only *independent* evidence of the wrap: the
    `box-N` controls and `_box_text` draw with the same algorithm as the
    product."""
    rule = RULE_CHAR * (QFU4_WIDTH + 2)
    body = "\n".join([f"{MARK}{NBSP}{QFU4_BOX_ROWS[0]}", *QFU4_BOX_ROWS[1:]])
    return f"{rule}\n{body}\n{rule}\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n"


def _wrap_row(line: str, columns: int) -> list[str]:
    """Soft wrap of one line as the three recorded boxes show it: words at
    single spaces fill a row; a word that does not fit starts the next row and
    the spaces at the break are dropped; a hyphen is no break point; a word
    longer than a row starts on the current row and is cut exactly at
    `columns`. Written here, independently of the product's `_claude_rows`."""
    rows = [""]
    for index, word in enumerate(line.split(" ")):
        if index:
            if len(rows[-1]) >= columns:
                rows.append("")
            else:
                rows[-1] += " "
        if len(word) > columns:
            for ch in word:
                if len(rows[-1]) >= columns:
                    rows.append("")
                rows[-1] += ch
            continue
        if rows[-1] and len(rows[-1]) + len(word) > columns:
            rows.append("")
        rows[-1] += word
    return [r.rstrip(" ") for r in rows]


def _box_text(text: str, width: int) -> str:
    """`_pending_input` of a pane `width + 2` columns wide holding `text`."""
    out = []
    for n, line in enumerate(text.strip().split("\n")):
        pieces = _wrap_row(line, width - 2) if line else [""]
        out.append(pieces[0] if n == 0 else ("  " + pieces[0] if pieces[0] else ""))
        out.extend("  " + p for p in pieces[1:])
    return "\n".join(out)


def test_the_recorded_qfu4_box_is_the_pointer_dispatched():
    """Catches the run that blocked: the box of qfu4 read through the product's
    own parsers (`_pending_input`, `_box_width`) is the pointer HoH typed, so
    the stalled-submit recovery would have pressed Enter. The pointer's first
    line is 75 characters; in a box 78 columns wide (text width 74) it breaks
    as `command-line` / `argument,`, short of the width. Control: the recorded
    rows are what the independent wrap model draws for the same text."""
    view = _qfu4_view()
    assert dispatchers._box_width(view) == QFU4_WIDTH
    assert dispatchers._pending_input(view) == QFU4_BOX
    sent = _qfu4_sent()
    assert sent.strip().split("\n")[1] == (
        "Your instructions for this turn are too long for one command-line argument,")
    assert len(sent.strip().split("\n")[1]) == 75     # one over the 74 a row takes
    assert dispatchers._same_input(QFU4_BOX, sent, QFU4_WIDTH) is True
    assert _box_text(sent, QFU4_WIDTH) == QFU4_BOX


def _swap(old: str, new: str, count: int = 1):
    assert old in QFU4_BOX, old
    return lambda: QFU4_BOX.replace(old, new, count)


@pytest.mark.parametrize("box, width", [
    # What a box that holds other text looks like -- one change each.
    pytest.param(_swap("sha256:\n  56ba", "sha256:\n  56bb"), 76, id="one-digit-of-the-digest"),
    pytest.param(_swap("argument,", "argumenx,"), 76, id="one-letter"),
    pytest.param(_swap("Read the **whole** file", "Read the whole file"), 76,
                 id="two-characters-dropped"),
    pytest.param(_swap("before you do anything else. ", "before you do anything. "), 76,
                 id="word-dropped"),
    pytest.param(_swap("i1-a0-planner.json", "i1-a1-planner.json"), 76, id="other-answer-path"),
    pytest.param(lambda: QFU4_BOX + " more", 76, id="extra-text"),
    pytest.param(lambda: QFU4_BOX + "\n" + QFU4_BOX, 76, id="the-pointer-twice"),
    pytest.param(lambda: "\n".join(QFU4_BOX_ROWS[:20]), 76, id="prefix-only"),
    pytest.param(lambda: "\n".join(QFU4_BOX_ROWS[1:]), 76, id="first-line-missing"),
    # Spaces and line breaks: whitespace is text.
    pytest.param(_swap("path:    /", "path:   /"), 76, id="one-space-fewer-in-a-row"),
    pytest.param(_swap("path:    /", "path:     /"), 76, id="one-space-more-in-a-row"),
    pytest.param(_swap("command-line\n  argument,", "command-lineargument,"), 76,
                 id="space-at-a-break-lost"),
    pytest.param(_swap("only points to it.\n\n", "only points to it.\n"), 76,
                 id="blank-line-dropped"),
    pytest.param(_swap("the dispatch.\n\n\n", "the dispatch.\n\n"), 76,
                 id="second-blank-line-dropped"),
    # Breaks Claude Code does not make.
    pytest.param(_swap("command-line\n  argument,", "command-\n  line argument,"), 76,
                 id="break-at-a-hyphen"),
    pytest.param(_swap("one command-line\n  argument,", "one\n  command-line argument,"), 76,
                 id="break-at-a-space-that-fits"),
    pytest.param(_swap("command-line\n  argument,", "command-line argument,"), 76,
                 id="break-missing-row-wider-than-the-box"),
    pytest.param(_swap("command-line\n  argument,", "command-line\n  argu\n  ment,"), 76,
                 id="word-cut-short-of-the-width"),
    pytest.param(_swap("Z-qf\n  u4-runs", "Z-q\n  fu4-runs"), 76, id="long-word-cut-one-short"),
    pytest.param(_swap("Z-qf\n  u4-runs", "Z-qfu\n  4-runs"), 76, id="long-word-cut-one-long"),
    pytest.param(_swap("Z-qf\n  u4-runs", "Z-qfu4-runs"), 76, id="long-word-break-joined"),
    pytest.param(_swap("path:    /srv", "path:\n  /srv"), 76, id="long-word-moved-to-its-own-row"),
    pytest.param(_swap("sha256:\n  56ba", "sha256:  56ba"), 76, id="digest-row-joined-over-the-width"),
    # Indentation.
    pytest.param(_swap("\n  argument,", "\nargument,"), 76, id="unindented-continuation"),
    pytest.param(_swap("\n  argument,", "\n   argument,"), 76, id="three-space-continuation"),
    pytest.param(_swap("\n  argument,", "\n>>argument,"), 76,
                 id="two-characters-instead-of-the-indentation"),
    pytest.param(_swap("\n  argument,", "\n\targument,"), 76, id="tab-instead-of-the-indentation"),
    # The width: the box is read under the width it was drawn at, or not at all.
    pytest.param(lambda: QFU4_BOX, 75, id="width-one-too-small"),
    pytest.param(lambda: QFU4_BOX, 77, id="width-one-too-large"),
    pytest.param(lambda: QFU4_BOX, 78, id="width-the-rule-not-the-text"),
    pytest.param(lambda: QFU4_BOX, 88, id="width-of-another-pane"),
    pytest.param(lambda: QFU4_BOX, None, id="width-unknown"),
    pytest.param(lambda: QFU4_BOX, 0, id="width-zero"),
])
def test_a_box_that_differs_in_content_or_in_a_break_claude_code_does_not_make_is_refused(
        box, width):
    """Catches a comparison that is too loose. The recorded box of run qfu4 is
    the pointer (the previous test); each of these is that box with one change,
    and Enter would submit text HoH did not send: another character, word,
    path or digest, extra text or the pointer twice, a prefix, a space more or
    fewer, a lost or added line break, a break at a hyphen or at a space that
    fits or short of the width, a word cut at another column, an unindented
    row, or the same box read under a width it was not drawn at. Only the wrap
    Claude Code makes is accepted; whitespace is not folded."""
    assert dispatchers._same_input(box(), _qfu4_sent(), width) is False


#: Run qaw4's pane was 48 columns wide (rows of 46, `_box_width`): the paragraph of the
#: pointer that reads "Read the whole file ...", verbatim from the pane view in
#: its `blocked_reason`, indentation removed from the first row as
#: `_pending_input` removes the input mark.
QAW4_PARAGRAPH_ROWS = [
    "Read the **whole** file before you do",
    "  anything else. A long file comes back",
    "  in parts: read it page by page, passing an",
    "  offset each time, and keep going",
    "  until you have reached the end of the file",
    "  -- not just its beginning. Then",
    "  follow it as your instructions. Do not",
    "  change, move or delete the file: the",
    "  dispatcher checks its SHA-256 again after",
    "  your turn, and a changed file fails",
    "  the dispatch.",
]


def _pointer_paragraph() -> str:
    """The paragraph of the pointer that qaw4's rows show, from the dispatcher's
    own text."""
    text = _pointer_prompt(QFU4_ANSWERS / "i1-a0-planner.prompt.md", QFU4_SIZE, QFU4_SHA)
    start = text.index("Read the **whole** file")
    return text[start:].strip()


def test_the_recorded_qaw4_paragraph_wraps_at_spaces_and_not_at_hyphens():
    """Run qaw4's narrow pane (46 text columns) pins what a wider one cannot:
    `-- not just its beginning.` moved whole to the next row although a `-`
    would still have fitted on the row above (44 of 46 columns used), so a
    hyphen is no break point; words fill a row up to its last column. The
    paragraph, read from the pointer the dispatcher builds, is exactly the
    recorded rows; the same rows with the break made at the hyphen are not."""
    paragraph = _pointer_paragraph()
    recorded = "\n".join(QAW4_PARAGRAPH_ROWS)
    rule = RULE_CHAR * 48
    view = (f"{rule}\n{MARK}{NBSP}{QAW4_PARAGRAPH_ROWS[0]}\n"
            + "\n".join(QAW4_PARAGRAPH_ROWS[1:]) + f"\n{rule}\n")
    assert dispatchers._box_width(view) == 46
    assert dispatchers._pending_input(view) == recorded
    assert dispatchers._same_input(recorded, paragraph, 46) is True
    at_hyphen = recorded.replace("the file\n  -- not", "the file -\n  - not")
    assert at_hyphen != recorded
    assert dispatchers._same_input(at_hyphen, paragraph, 46) is False
    assert _box_text(paragraph, 46) == recorded


@pytest.mark.parametrize("width", [46, 60, 76, 88, 100, 140], ids=lambda w: f"box-{w}")
def test_the_same_text_in_a_pane_of_another_width_is_the_same_text(width):
    """Control, and the other side of the previous test: the pointer drawn by
    the independent wrap model in a pane of another width is accepted at that
    width (qaw4's pane was 46 columns of text, qaw3's 86, qfu4's 76)."""
    sent = _qfu4_sent()
    view = (RULE_CHAR * (width + 2) + "\n" + MARK + " " + _box_text(sent, width)
            + "\n" + RULE_CHAR * (width + 2) + "\n")
    assert dispatchers._box_width(view) == width
    assert dispatchers._same_input(dispatchers._pending_input(view), sent, width) is True


@pytest.mark.parametrize("text, width, expected", [
    pytest.param("A" * 74, 76, True, id="a-line-of-exactly-the-text-width"),
    pytest.param("A" * 75, 76, False, id="a-line-one-over-in-one-row"),
    pytest.param("one two three", 20, True, id="short-line-no-wrap"),
    pytest.param("aaaa bbbb cccc", 12, True, id="wrap-at-a-space"),
    pytest.param("x-y " + "w" * 20, 12, True, id="word-cut-after-a-hyphen-in-the-line"),
])
def test_small_boxes_by_the_wrap_model(text, width, expected):
    """Edges of `_same_input` on small texts, each drawn by the independent
    model, except the one-row-too-wide case, which is a single row of one
    character more than the box takes. Catches an off-by-two in the text width, and a model that breaks
    at hyphens."""
    box = _box_text(text, width)
    if text == "A" * 75:
        box = "A" * 75
    assert dispatchers._same_input(box, text, width) is expected


def test_a_hyphen_is_no_break_point_in_a_long_word():
    """Run qfu4 cut `20261007T1138Z-qf` / `u4-runs` mid-token at the full width
    although hyphens lay earlier in the word. A box that cuts at the hyphen is
    refused; the one cut at the width is accepted (control)."""
    sent = "path:  " + "ab-" * 40
    assert dispatchers._same_input(_box_text(sent, 20), sent, 20) is True
    short = "path:  " + "ab-" * 11
    at_hyphen = "path:  ab-ab-ab-ab-ab-\n  ab-ab-ab-ab-ab-ab-"
    assert dispatchers._same_input(_box_text(short, 20), short, 20) is True
    assert dispatchers._same_input(at_hyphen, short, 20) is False


# --------------------------------------------------------------------------- #
# 2. The wait before the first prompt of an agent HoH started
# --------------------------------------------------------------------------- #

RULE = RULE_CHAR * 88
FOOT = "  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n"
EMPTY_BOX = f"\u273b Baked for 0s\n{RULE}\n{MARK} \n{RULE}\n{FOOT}"
#: A fresh Claude Code session's empty box (pane view of run sstew4).
PLACEHOLDER_BOX = f"{RULE}\n{MARK} Try \"fix lint errors\"\n{RULE}\n{FOOT}"
#: Another agent kind's screen: no Claude input box at all.
OTHER_SCREEN = "kimi> working on it\n  [esc] cancel   [tab] mode\n> \n"


def _box(text: str) -> str:
    return f"{RULE}\n{MARK} {text}\n{RULE}\n{FOOT}"


def _agent(status: str) -> dict:
    return {"result": {"agent": {"pane_id": TARGET, "workspace_id": "w0",
                                 "tab_id": "w0:t1", "agent": "claude",
                                 "agent_status": status, "cwd": "/tmp"}}}


class _Script:
    """A sequence whose last item repeats; an exception in it is raised."""

    def __init__(self, items) -> None:
        self.items = list(items)

    def next(self):
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


class _ReadyCli:
    """Herdr as the dispatcher sees it through `_cli`: `agent get` answers the
    next status, `agent read` the next screen, `agent prompt` is recorded (and
    a structured role's answer is written as the role would). Anything else --
    above all an Enter -- fails the test."""

    def __init__(self, statuses=("idle",), screens=(EMPTY_BOX,)) -> None:
        self.calls: list[list[str]] = []
        self.statuses = _Script(statuses)
        self.screens = _Script(screens)
        self.answer: Path | None = None

    def kinds(self) -> list[str]:
        return [c[2] for c in self.calls]

    def __call__(self, args, purpose, *, text=False):
        self.calls.append(list(args))
        verb = args[1:3]
        if verb == ["agent", "get"]:
            return _agent(self.statuses.next())
        if verb in (["agent", "read"], ["pane", "read"]):
            assert text and args[-2:] == ["--source", "visible"]
            return self.screens.next()
        if verb == ["agent", "prompt"]:
            if self.answer is not None:
                self.answer.write_text('{"ok": true}\n', encoding="utf-8")
            return _agent("idle")
        pytest.fail(f"unexpected herdr call {args}")


class _Clock:
    """The dispatcher's `time`: `sleep` only records, `monotonic()` steps a
    second, `time()` is fixed in the past. The wait is counted in reads, not
    by this clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.fixed = time.time() - 10.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def time(self) -> float:
        return self.fixed


def _state() -> RunState:
    return RunState(run_id="20261008t0000z-firstprompt-test", repo_path="/x",
                    project_name="p", spec_path="/x/s.md", spec_digest="a" * 16,
                    policy_digest="b" * 16, profile_digest="c" * 16,
                    iteration=1, attempt=0)


def _rig(tmp_path, monkeypatch, cli, *, kind: str = "claude", fresh: bool = True):
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(d, answers_dir=tmp_path / "run" / "answers",
                   profiles={r: kind for r in Role})
    arena = tmp_path / "arena"
    arena.mkdir(parents=True, exist_ok=True)
    d.cwd = str(arena)
    d.timeout_ms = 1000
    d.agents, d.own_panes, d.own_tabs = {}, [], []
    monkeypatch.setattr(d, "_ensure_agent", lambda role, state: TARGET)
    monkeypatch.setattr(d, "_pane_view", lambda target, lines=40: "(stubbed)")
    monkeypatch.setattr(d, "_cli", cli)
    clock = _Clock()
    monkeypatch.setattr(dispatchers, "time", clock)
    if fresh:
        d._fresh().add(TARGET)
    return d, clock


def _dispatch(d, state, prompt: str = "do the task", role: Role = Role.DEVELOPER):
    return d.dispatch(role, prompt, state=state)


def test_the_wait_is_a_second_a_read_three_quiet_reads_sixty_at_most():
    """The numbers of the contract, named so that a change shows. Not read from
    a Claude Code version: no version is consulted anywhere."""
    assert dispatchers.READY_POLL_SECONDS == 1.0
    assert dispatchers.READY_QUIET_POLLS == 3
    assert dispatchers.READY_MAX_POLLS == 60


def test_first_prompt_waits_for_an_idle_box_that_holds_still(tmp_path, monkeypatch):
    """Catches a first prompt sent as soon as `agent start` returned (the stall
    of runs qgg to qfu4), and a wait that is satisfied too early. The agent is
    idle throughout; its screen shows no box, then a fresh session's
    placeholder, then an empty box, which then holds still. The prompt is the
    last call, after exactly the reads that include `READY_QUIET_POLLS`
    agreeing ones after the last change, one second apart; the note says so,
    and the agent is no longer fresh. Nothing is pressed."""
    noise = [OTHER_SCREEN, PLACEHOLDER_BOX]
    cli = _ReadyCli(screens=[*noise, EMPTY_BOX])
    d, clock = _rig(tmp_path, monkeypatch, cli)
    state = _state()
    _dispatch(d, state)
    polls = len(noise) + 1 + dispatchers.READY_QUIET_POLLS
    assert cli.kinds() == ["get", "read"] * polls + ["prompt"]
    assert clock.slept == [dispatchers.READY_POLL_SECONDS] * (polls - 1)
    assert d._fresh() == set()
    assert "held still" in "\n".join(state.history) and TARGET in "\n".join(state.history)


def test_a_placeholder_that_holds_still_is_an_empty_box(tmp_path, monkeypatch):
    """Catches a wait that never ends on a fresh session: Claude Code shows
    `Try "..."` in an empty box (run sstew4), which `_pending_input` returns as
    text. Control: the same text that is not that placeholder is not an empty
    box (the timeout test below)."""
    cli = _ReadyCli(screens=[PLACEHOLDER_BOX])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    _dispatch(d, _state())
    assert cli.kinds() == ["get", "read"] * (dispatchers.READY_QUIET_POLLS + 1) + ["prompt"]


def test_first_prompt_waits_while_the_agent_is_working(tmp_path, monkeypatch):
    """Catches a wait that looks at the box alone: a box drawn while the agent
    is still `working` (starting up) is not ready."""
    cli = _ReadyCli(statuses=["working", "working", "idle"])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    _dispatch(d, _state())
    polls = 2 + dispatchers.READY_QUIET_POLLS + 1
    assert cli.kinds().count("get") == polls and cli.kinds()[-1] == "prompt"
    # The two working reads stop before the box is read at all.
    assert cli.kinds().count("read") == polls - 2


def test_a_dialog_before_the_first_prompt_is_not_answered_or_written_into(tmp_path, monkeypatch):
    """Catches a prompt sent into a dialog of a new agent. `blocked` raises
    `WaitingForApproval` at once; nothing is sent, and the agent stays fresh."""
    cli = _ReadyCli(statuses=["idle", "blocked"])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    with pytest.raises(WaitingForApproval):
        _dispatch(d, _state())
    assert "prompt" not in cli.kinds() and d._fresh() == {TARGET}


@pytest.mark.parametrize("statuses, screens, why", [
    pytest.param(["idle"], [OTHER_SCREEN], "no input box", id="no-box-ever-drawn"),
    pytest.param(["idle"], [_box("hello")], "already holds text", id="text-in-the-box"),
    pytest.param(["idle"], [_box('Try "x" and more')], "already holds text",
                 id="text-that-only-begins-like-the-placeholder"),
    pytest.param(["working"], [EMPTY_BOX], "status is 'working'", id="always-working"),
    pytest.param(["idle"], [DispatchError("boom")], "a read failed", id="reads-fail"),
    pytest.param([DispatchError("boom")], [EMPTY_BOX], "a read failed", id="status-reads-fail"),
    pytest.param(["idle"], [_box('Try "a\n  b"')], "already holds text",
                 id="a-placeholder-over-two-lines"),
    pytest.param(["idle"], [_box('Try "a"'), _box('Try "b"')] * 40, "kept changing",
                 id="the-box-never-holds-still"),
])
def test_without_an_idle_box_that_holds_still_nothing_is_sent_written_or_pressed(
        tmp_path, monkeypatch, statuses, screens, why):
    """Catches a first prompt (or an Enter) after a wait that gave up, a wait
    without a bound, and a prompt file or marker written before the wait ended.
    After exactly `READY_MAX_POLLS` reads the dispatch raises `PaneNotClean`
    with the reason in it. There is no `agent prompt` and no key sent (the rig
    fails on one), the answers directory is empty although a prompt above 8 KiB
    would go through a file, and the agent is still fresh, so a retry waits
    again."""
    cli = _ReadyCli(statuses, screens)
    d, clock = _rig(tmp_path, monkeypatch, cli)
    with pytest.raises(PaneNotClean) as stopped:
        _dispatch(d, _state(), prompt="x" * 9000)
    assert why in str(stopped.value)
    assert "prompt" not in cli.kinds()
    assert cli.kinds().count("get") == dispatchers.READY_MAX_POLLS
    assert len(clock.slept) == dispatchers.READY_MAX_POLLS - 1
    assert list(d.answers_dir.iterdir()) == []
    assert d._fresh() == {TARGET}


def test_a_failed_read_among_good_ones_does_not_end_the_wait(tmp_path, monkeypatch):
    """Control of the previous test: one failed read resets the quiet count and
    the wait goes on; the prompt follows the quiet reads that come after."""
    cli = _ReadyCli(screens=[EMPTY_BOX, DispatchError("blip"), EMPTY_BOX])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    _dispatch(d, _state())
    polls = 2 + dispatchers.READY_QUIET_POLLS + 1
    assert cli.kinds() == ["get", "read"] * polls + ["prompt"]


def test_an_agent_that_is_not_fresh_is_prompted_without_a_read(tmp_path, monkeypatch):
    """Control: only the first prompt of an agent HoH started waits. A target
    that is not marked fresh is prompted at once, with no Herdr call before the
    prompt and no sleep."""
    cli = _ReadyCli(screens=[OTHER_SCREEN])
    d, clock = _rig(tmp_path, monkeypatch, cli, fresh=False)
    _dispatch(d, _state())
    assert cli.kinds() == ["prompt"] and clock.slept == []


def test_only_the_first_prompt_waits(tmp_path, monkeypatch):
    """Catches a wait repeated before every prompt of the same agent: once the
    first prompt is sent, the next one goes out with no read in front of it."""
    cli = _ReadyCli()
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    state = _state()
    _dispatch(d, state, "first task")
    before = len(cli.calls)
    _dispatch(d, state, "second task")
    assert [c[2] for c in cli.calls[before:]] == ["prompt"]


def test_another_agent_kind_waits_on_its_status_alone(tmp_path, monkeypatch):
    """Claude Code's box is the only box `_pending_input` knows. For another
    kind the wait holds the status (`idle` for the quiet reads) and reads no
    screen, so it cannot block on a box that does not exist."""
    cli = _ReadyCli(screens=[OTHER_SCREEN])
    d, _clock = _rig(tmp_path, monkeypatch, cli, kind="kimi")
    _dispatch(d, _state())
    assert cli.kinds() == ["get"] * (dispatchers.READY_QUIET_POLLS + 1) + ["prompt"]


def test_a_started_agent_is_fresh_and_a_restored_one_is_not(tmp_path, monkeypatch):
    """Catches the mark set in the wrong place. `_ensure_agent` marks the agent
    it starts (a pane created in its place after a restored one was replaced
    goes through the same call) and does not mark one Herdr restored, which has
    had its turns and is checked by `_check_restored`. Both through the real
    `_ensure_agent`, with the Herdr calls it makes faked."""
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    store = tmp_path / "runs" / "r1"
    store.mkdir(parents=True)
    _Base.__init__(d, answers_dir=store / "answers", profiles={r: "claude" for r in Role})
    d.store_dir = store
    d.cwd = str(store)
    d.timeout_ms = 1000
    d.agents, d.own_panes, d.own_tabs = {}, [], []
    started: list[str] = []
    monkeypatch.setattr(d, "_new_tab_pane", lambda label, *, cwd=None: "w0:p9")
    monkeypatch.setattr(d, "_agent_args", lambda role: [])
    monkeypatch.setattr(d, "_start_agent",
                        lambda name, kind, pane, extra=None: started.append(name))
    state = _state()
    name = d._agent_name(Role.QA, state)
    monkeypatch.setattr(dispatchers.herdr, "snapshot", lambda: {"agents": []})
    assert d._ensure_agent(Role.QA, state) == name
    assert started == [name] and d._fresh() == {name}

    restored = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(restored, answers_dir=store / "answers", profiles={r: "claude" for r in Role})
    restored.store_dir = store
    restored.cwd = str(store)
    restored.agents, restored.own_panes, restored.own_tabs = {}, [], []
    monkeypatch.setattr(restored, "_start_agent",
                        lambda *a, **k: pytest.fail("a restored agent is not started again"))
    row = {"name": name, "pane_id": "w0:p7", "workspace_id": "w0", "tab_id": "w0:t7",
           "agent": "claude", "agent_status": "idle", "cwd": str(store),
           "foreground_cwd": str(store)}
    monkeypatch.setattr(dispatchers.herdr, "snapshot", lambda: {"agents": [row]})
    assert restored._ensure_agent(Role.QA, state) == name
    assert restored._fresh() == set()


@pytest.mark.parametrize("sent, box, expected", [
    pytest.param("x" * 74 + " tail", "x" * 74 + "\n  tail", True, id="space-after-a-full-row"),
    pytest.param("x" * 74 + "tail", "x" * 74 + "\n  tail", True, id="no-space-the-word-is-cut"),
    pytest.param("aaaa bbbb cccc", "aaaa bbbb\n  cccc", True, id="one-space-at-a-break"),
    pytest.param("aaaa bbbb  cccc", "aaaa bbbb\n  cccc", True, id="two-spaces-at-a-break"),
    pytest.param("aaaa bbbb   cccc", "aaaa bbbb\n  cccc", False,
                 id="a-run-that-fills-the-row-stays-closed"),
    pytest.param("aaaa bbbb   cccc", "aaaa bbbb\n   cccc", True,
                 id="a-run-that-fills-the-row-leaves-one-space-less"),
    pytest.param("aaaa bbbb cccc", "aaaa bbbb\n  cccc ", False, id="a-space-kept-at-the-end-of-a-box-row"),
    pytest.param("x" * 74 + "  tail", "x" * 74 + "\n   tail", True, id="full-row-second-space-kept"),
    pytest.param("x" * 74 + "  tail", "x" * 74 + "\n  tail", False,
                 id="full-row-second-space-dropped-stays-closed"),
    pytest.param("aaaa bbbb cccc", "aaaa bbbb cccc", False, id="no-break-where-one-is-made"),
])
def test_what_the_comparison_forgives_about_spaces_at_a_break_and_what_it_leaves_closed(
        sent, box, expected):
    """Documents the whole of the whitespace `_same_input` forgives: the
    screen cannot show a space dropped at a break, so a box that breaks after
    a full row (74 text columns at width 76) or at a space that does not fit
    is the same box whether `sent` had a space, none (a word cut at the width)
    or a short run there. The model keeps the second and later spaces of a run
    that fills the row, or follows an exactly full one, at the head of the next
    row; a box that drops them is refused (the closed side; never recorded). A space kept at
    the end of a box row, or a break where none is made, is refused."""
    width = 76 if sent.startswith("x") else 12
    assert dispatchers._same_input(box, sent, width) is expected


class _Ticks:
    """A monotonic clock that is read once per call and moves `step` each time."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_the_wait_has_a_ceiling_on_its_own_clock(tmp_path, monkeypatch):
    """Catches a wait with no wall-clock bound (a Herdr read can hang for its
    wrapper timeout, so 60 reads are not a time bound). The ceiling is read from
    the dispatcher's own injectable clock (`ready_clock`, default the real
    monotonic clock bound at import), never from the module's `time`, which the
    fakes of other tests step by 1000 s a call. With a clock stepping 100 s a
    read and the default 15 minutes the wait ends after the 9th poll with
    `PaneNotClean`, nothing sent. Control: the module-level fake clock
    stepping 1000 s a call does not end a wait that holds still."""
    cli = _ReadyCli(screens=[_box('Try "a"'), _box('Try "b"')] * 40)
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    d.ready_clock = _Ticks(100.0)
    assert dispatchers.READY_CEILING_SECONDS == 15 * 60
    with pytest.raises(PaneNotClean) as stopped:
        _dispatch(d, _state())
    polls = cli.kinds().count("get")
    assert polls == 9 and "prompt" not in cli.kinds()
    assert "900 s" in str(stopped.value)
    assert d._fresh() == {TARGET}

    steady = _ReadyCli(screens=[EMPTY_BOX])
    d2, clock2 = _rig(tmp_path / "steady", monkeypatch, steady)
    clock2.step = 1000.0
    clock2.monotonic = lambda: setattr(clock2, "now", clock2.now + clock2.step) or clock2.now
    _dispatch(d2, _state())
    assert steady.kinds()[-1] == "prompt"


def test_the_width_of_the_box_is_part_of_what_must_hold_still(tmp_path, monkeypatch):
    """Catches a signature without the pane width: a box that is the same text
    but drawn at another width (the pane is being resized, the TUI redrawn)
    has not held still."""
    narrow = f"{RULE_CHAR * 80}\n{MARK} \n{RULE_CHAR * 80}\n{FOOT}"
    cli = _ReadyCli(screens=[EMPTY_BOX, narrow])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    _dispatch(d, _state())
    assert cli.kinds().count("get") == 1 + dispatchers.READY_QUIET_POLLS + 1


def test_done_is_as_good_as_idle(tmp_path, monkeypatch):
    """Catches `done` taken as not ready (`_settle` and `_check_restored` treat
    both as free)."""
    cli = _ReadyCli(statuses=["done"])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    _dispatch(d, _state())
    assert cli.kinds().count("get") == dispatchers.READY_QUIET_POLLS + 1
    assert cli.kinds()[-1] == "prompt"


def test_a_failed_status_read_does_not_end_the_wait(tmp_path, monkeypatch):
    """Catches `agent get` read outside the guard: an error from it is "not
    ready" like an error from `agent read`, and the wait goes on."""
    cli = _ReadyCli(statuses=[DispatchError("blip"), "idle"])
    d, _clock = _rig(tmp_path, monkeypatch, cli)
    _dispatch(d, _state())
    assert cli.kinds().count("get") == 1 + dispatchers.READY_QUIET_POLLS + 1
    assert cli.kinds()[-1] == "prompt"
