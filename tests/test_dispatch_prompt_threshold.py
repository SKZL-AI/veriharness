"""Routing of role prompts through the prompt file, and what it does to a stall.

A role prompt above `PROMPT_FILE_THRESHOLD` (8 KiB of UTF-8), or one holding a
character a paste into Claude Code's input box may drop, is not typed inline:
the dispatcher writes it to a file and types a short pointer. Why:

* runs qgg and qgg2 (2026-10-04) blocked at their first planner dispatch with a
  57,970-byte prompt, below the old 64 KiB threshold, that stayed unsent in the
  box ("Removed 4 invisible characters");
* runs qfu, qfu2 and qfu3 (2026-10-07) blocked the same way with a ~50 KB
  prompt: Claude Code 2.1.292 showed the stalled box collapsed to its first and
  last 500 characters, and the stalled-submit recovery refused, correctly,
  because the screen could not prove that the box held HoH's prompt.

The pointer is about 1.2 KiB, so the box shows it whole and the recovery can
press Enter once. These tests pin the routing, its edges, and that consequence.
Nothing here measures what a terminal does with a paste: the collapse rule is
Claude Code 2.1.292's, read off its binary and off the recorded box of run qfu.

No agent is started and `herdr` is never run: `_cli` is a fake that records its
arguments, the dispatcher is built without `require_herdr()`, and the clock is
fake. Everything is written below pytest's `tmp_path`. Characters under test
are built with `chr()` or written as `\\u` escapes -- this file holds none.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from hoh import dispatchers
from hoh.contracts import Role, RunState
from hoh.controller import DispatchError, PaneNotClean
from hoh.dispatchers import HerdrDispatcher, _answer_instruction, _Base

ROOT = Path(__file__).resolve().parent.parent
TARGET = "w0:p1"
ROLES = [Role.PLANNER, Role.DEVELOPER, Role.QA]
QGG_SIZES = (57_970, 57_578)

# Claude Code 2.1.292: an input box longer than this many characters (UTF-16
# units) is kept as its first and last KEEP characters around a marker.
CLAUDE_COLLAPSE_CHARS = 10_000
CLAUDE_COLLAPSE_KEEP = 500
PATH_MAX = 4096

# Default_Ignorable_Code_Point, inclusive; declared here independently of the
# product's table so that a slip in one of them is seen by the other.
DEFAULT_IGNORABLE = [
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
]

# 23 characters, one per class, built with chr().
UNSAFE = {
    "lrm": chr(0x200E), "zwsp": chr(0x200B), "shy": chr(0x00AD),
    "bom": chr(0xFEFF), "lsep": chr(0x2028), "psep": chr(0x2029),
    "cr": chr(0x0D), "tab": chr(0x09), "esc": chr(0x1B), "bel": chr(0x07),
    "del": chr(0x7F), "pua": chr(0xE000),
    "unassigned": chr(0x0378), "nbsp": chr(0x00A0),
    "ideographic-space": chr(0x3000), "vs16": chr(0xFE0F),
    "vs17": chr(0xE0100), "cgj": chr(0x034F), "hangul-filler": chr(0x3164),
    "halfwidth-filler": chr(0xFFA0), "mongolian-fvs1": chr(0x180B),
    "khmer-aq": chr(0x17B4), "braille-blank": chr(0x2800),
}


def _state() -> RunState:
    return RunState(run_id="20261007t0000z-pthresh-test", repo_path="/x",
                    project_name="p", spec_path="/x/s.md", spec_digest="a" * 16,
                    policy_digest="b" * 16, profile_digest="c" * 16,
                    iteration=1, attempt=0)


class _FakeCli:
    """Records every call; an `answer` path, if set, is written as the role
    would write it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answer: Path | None = None

    def __call__(self, args, purpose, *, text=False):
        self.calls.append(list(args))
        if self.answer is not None:
            self.answer.write_text('{"ok": true}\n', encoding="utf-8")
        return {"result": {"agent": {"pane_id": TARGET, "workspace_id": "w0",
                                     "tab_id": "w0:t1", "agent": "claude",
                                     "agent_status": "idle", "cwd": "/tmp"}}}


def _dispatcher(tmp_path: Path, monkeypatch) -> tuple[HerdrDispatcher, _FakeCli]:
    d = HerdrDispatcher.__new__(HerdrDispatcher)
    _Base.__init__(d, answers_dir=tmp_path / "run" / "answers",
                   profiles={r: "claude" for r in Role})
    arena = tmp_path / "arena"
    arena.mkdir(parents=True, exist_ok=True)
    d.cwd = str(arena)
    d.timeout_ms = 1000
    d.agents = {}
    d.own_panes = []
    d.own_tabs = []
    monkeypatch.setattr(d, "_ensure_agent", lambda role, state: TARGET)
    monkeypatch.setattr(d, "_pane_view", lambda target, lines=40: "(stubbed)")
    cli = _FakeCli()
    monkeypatch.setattr(d, "_cli", cli)
    return d, cli


def _full(d: HerdrDispatcher, role: Role, prompt: str, state: RunState) -> str:
    """What the dispatcher binds: the prompt plus, for the structured roles,
    the answer instruction."""
    if role in dispatchers.STRUCTURED_ROLES:
        return prompt + _answer_instruction(d._answer_path(role, state))
    return prompt


def _pad(d: HerdrDispatcher, role: Role, size: int, tail: str = "") -> str:
    """A plain ASCII prompt (ending in `tail`) whose full prompt, with the
    answer instruction, is exactly `size` UTF-8 bytes."""
    overhead = (len(_full(d, role, "", _state()).encode("utf-8"))
                + len(tail.encode("utf-8")))
    body = ("Plain line of a role prompt.\n" * (size // 29 + 1))[:size - overhead]
    return body + tail


def _send(tmp_path: Path, monkeypatch, role: Role, size: int | None = None,
          prompt: str | None = None, tail: str = ""):
    d, cli = _dispatcher(tmp_path, monkeypatch)
    state = _state()
    if prompt is None:
        prompt = _pad(d, role, size, tail)
    full = _full(d, role, prompt, state)
    if size is not None:
        assert len(full.encode("utf-8")) == size
    if role in dispatchers.STRUCTURED_ROLES:
        cli.answer = d._answer_path(role, state)
    d.dispatch(role, prompt, state=state)
    return d, cli, full


def _prompt_files(d: HerdrDispatcher) -> list[str]:
    return sorted(p.name for p in d.answers_dir.iterdir() if ".prompt." in p.name)


def _assert_inline(d, cli, full) -> None:
    assert len(cli.calls) == 1
    assert cli.calls[0][4] == full
    assert _prompt_files(d) == []


def _assert_pointer(d, cli, full, role) -> None:
    """The prompt went through the file: it is in the file byte for byte, and
    the one `agent prompt` names the file without holding the prompt. Does not
    depend on the pointer's wording."""
    assert len(cli.calls) == 1
    typed = cli.calls[0][4]
    assert full not in typed
    name = f"i1-a0-{role.value}.prompt.md"
    assert (d.answers_dir / name).read_bytes() == full.encode("utf-8")
    assert str(d.answers_dir.resolve() / name) in typed


def _independently_unsafe(ch: str) -> bool:
    """The oracle of the source scan: not printable (False for Cc, Cf, Cs, Co,
    Cn, Zl, Zp and every Zs but U+0020), a Default_Ignorable code point, or
    U+2800."""
    cp = ord(ch)
    return (not ch.isprintable() or cp == 0x2800
            or any(lo <= cp <= hi for lo, hi in DEFAULT_IGNORABLE))


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_threshold_is_8_kib_and_argv_limit_unchanged():
    assert dispatchers.PROMPT_FILE_THRESHOLD == 8 * 1024
    assert dispatchers.ARGV_ELEMENT_LIMIT == 64 * 1024
    # Whatever stays inline is shown whole by Claude Code.
    assert dispatchers.PROMPT_FILE_THRESHOLD < CLAUDE_COLLAPSE_CHARS


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_boundary_at_8_kib(tmp_path, monkeypatch, role):
    """Longer than the threshold goes through the file; exactly the threshold
    stays inline."""
    d, cli, full = _send(tmp_path / "above", monkeypatch, role, 8 * 1024 + 1)
    _assert_pointer(d, cli, full, role)
    d, cli, full = _send(tmp_path / "at", monkeypatch, role, 8 * 1024)
    _assert_inline(d, cli, full)


@pytest.mark.parametrize("size", QGG_SIZES, ids=str)
def test_qa_gap_gate_sized_prompt_uses_pointer(tmp_path, monkeypatch, size):
    """The size that blocked qgg and qgg2 inline goes through the file even as
    plain ASCII."""
    d, cli, full = _send(tmp_path, monkeypatch, Role.PLANNER, size)
    _assert_pointer(d, cli, full, Role.PLANNER)


@pytest.mark.parametrize("char", list(UNSAFE.values()), ids=list(UNSAFE))
@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_paste_unsafe_character_uses_pointer(tmp_path, monkeypatch, role, char):
    """A short prompt with one such character goes through the file, for every
    role, and the file keeps the character byte for byte."""
    prompt = ("Severity tag `HI" + char + "GH` must be read as written.\n") * 3
    d, cli, full = _send(tmp_path, monkeypatch, role, prompt=prompt)
    assert len(full.encode("utf-8")) < 8 * 1024
    _assert_pointer(d, cli, full, role)
    name = f"i1-a0-{role.value}.prompt.md"
    assert char.encode("utf-8") in (d.answers_dir / name).read_bytes()


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_paste_unsafe_character_at_the_end_uses_pointer(tmp_path, monkeypatch, role):
    """The whole prompt is examined, not a prefix: U+200B as the very last
    character of the prompt part of an 8,000-byte full prompt."""
    d, cli, full = _send(tmp_path, monkeypatch, role, 8000, tail=chr(0x200B))
    _assert_pointer(d, cli, full, role)


def test_every_listed_invisible_code_point_is_paste_unsafe():
    """Exhaustive: every Default_Ignorable code point, every space separator
    but U+0020, and U+2800 are flagged; printable ASCII and the newline are
    not."""
    import unicodedata

    missed = [f"U+{cp:04X}" for lo, hi in DEFAULT_IGNORABLE
              for cp in range(lo, hi + 1)
              if not dispatchers._paste_unsafe("a" + chr(cp) + "b")]
    spaces = [cp for cp in range(0x110000)
              if unicodedata.category(chr(cp)) == "Zs" and cp != 0x20]
    missed += [f"U+{cp:04X}" for cp in spaces + [0x2800]
               if not dispatchers._paste_unsafe("a" + chr(cp) + "b")]
    assert missed == []
    assert not dispatchers._paste_unsafe(
        "".join(chr(c) for c in range(0x20, 0x7F)) + "\n")


def test_plain_multiline_unicode_prompt_stays_inline(tmp_path, monkeypatch):
    """Control: newlines, umlauts, an em dash, an arrow, a dotted capital I, a
    warning sign without a variation selector and a combining acute accent are
    not paste-unsafe; a short prompt with them goes inline unchanged."""
    line = ("Pr\u00fcfung \u2014 Gr\u00f6\u00dfe \u2192 \u00dcbergabe, H\u0130GH x, "
            + chr(0x26A0) + " e" + chr(0x0301) + ", `-HIGH x`.\n")
    d, cli, full = _send(tmp_path, monkeypatch, Role.QA, prompt=line * 20)
    _assert_inline(d, cli, full)


def test_pointer_itself_is_short_and_paste_safe(tmp_path):
    """The pointer plus the answer instruction never needs a pointer of its
    own: far below the threshold for a deep path, ASCII, and printable except
    for newlines -- checked without `_paste_unsafe`."""
    deep = tmp_path.joinpath(*(["d" * 40] * 8)) / "answers"
    pointer = dispatchers._pointer_prompt(deep / "i9-a9-planner.prompt.md",
                                          10 ** 6, "f" * 64)
    pointer += _answer_instruction(deep / "i9-a9-planner.json")
    assert len(pointer.encode("utf-8")) < dispatchers.PROMPT_FILE_THRESHOLD // 2
    assert pointer.isascii()
    assert not [c for c in pointer if c != "\n" and not c.isprintable()]


def test_resume_across_the_threshold_change_fails_closed(tmp_path, monkeypatch):
    """An answer left by an older dispatcher for a 20 KB prompt it sent inline
    is not collected now that the same prompt would go through a prompt file
    that was never written: the refusal is non-transient and nothing is sent."""
    d, cli = _dispatcher(tmp_path, monkeypatch)
    state = _state()
    prompt = _pad(d, Role.PLANNER, 20_000)
    answer = d._answer_path(Role.PLANNER, state)
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text('{"ok": true}\n', encoding="utf-8")
    monkeypatch.setattr(d, "_resumable_answer", lambda *a, **k: True)
    with pytest.raises(DispatchError) as caught:
        d.dispatch(Role.PLANNER, prompt, state=state)
    assert caught.value.transient is False
    assert "i1-a0-planner.prompt.md" in str(caught.value)
    assert cli.calls == []


# --------------------------------------------------------------------------- #
# Source rules
# --------------------------------------------------------------------------- #

def test_sources_hold_no_paste_unsafe_character():
    """Neither the dispatcher nor this file holds a literal character of the
    classes it routes: they are named as U+XXXX or built with chr()."""
    for rel in ("src/hoh/dispatchers.py", "tests/test_dispatch_prompt_threshold.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        bad = [(n, f"U+{ord(c):04X}") for n, line in enumerate(text.splitlines(), 1)
               for c in line if _independently_unsafe(c)]
        assert bad == [], (rel, bad[:5])


def _import_time_uses(tree: ast.AST, name: str) -> list[int]:
    """Lines where `name` is imported, or named by a string, outside every
    function: whatever runs at import time (module level, `try`, `if`, a class
    body), in every form including `__import__` and `importlib`."""
    found: list[int] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inside = in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if not inside:
                if isinstance(child, ast.Import) and any(
                        a.name.split(".")[0] == name for a in child.names):
                    found.append(child.lineno)
                if (isinstance(child, ast.ImportFrom)
                        and (child.module or "").split(".")[0] == name):
                    found.append(child.lineno)
                if isinstance(child, ast.Constant) and child.value == name:
                    found.append(child.lineno)
            visit(child, inside)

    visit(tree, False)
    return found


def test_unicodedata_not_imported_at_module_level():
    """`unicodedata` is imported only inside `_paste_unsafe`, away from the
    module import block other strands edit. The scan covers the whole tree, so
    a `try:` or `if:` at module level does not hide an import; the scanner is
    checked on eight snippets first."""
    tree = ast.parse((ROOT / "src/hoh/dispatchers.py").read_text(encoding="utf-8"))
    assert _import_time_uses(tree, "unicodedata") == []
    snippets = (
        ("import unicodedata\n", True),
        ("from unicodedata import category\n", True),
        ("try:\n    import unicodedata\nexcept ImportError:\n    pass\n", True),
        ("if True:\n    import unicodedata\n", True),
        ("class C:\n    import unicodedata\n", True),
        ("x = __import__('unicodedata')\n", True),
        ("def f():\n    import unicodedata\n", False),
        ("class C:\n    def f(self):\n        import unicodedata\n", False),
    )
    for source, hit in snippets:
        assert bool(_import_time_uses(ast.parse(source), "unicodedata")) is hit, source


# --------------------------------------------------------------------------- #
# What a stalled prompt does now that HoH types a short pointer
# --------------------------------------------------------------------------- #

def _utf16_units(text: str) -> int:
    """JavaScript's `String.length`, the unit Claude Code counts in."""
    return len(text.encode("utf-16-le")) // 2


def _claude_shows(typed: str) -> str:
    """What the input box keeps of `typed`: the stripped text, or above the
    collapse limit its first and last KEEP characters around a marker."""
    text = typed.strip()
    if _utf16_units(text) <= CLAUDE_COLLAPSE_CHARS:
        return text
    middle = text[CLAUDE_COLLAPSE_KEEP:-CLAUDE_COLLAPSE_KEEP]
    return (text[:CLAUDE_COLLAPSE_KEEP]
            + f"[...Truncated text #1 +{middle.count(chr(10))} lines...]"
            + text[-CLAUDE_COLLAPSE_KEEP:])


def _wrap(line: str, columns: int) -> list[str]:
    """Soft wrap of one line as the box does it: words at single spaces fill a
    row, the space at the end of a full row is dropped, and a word longer than
    the row is broken exactly at its width, starting on the current row."""
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
        if len(rows[-1]) + len(word) > columns and rows[-1]:
            rows.append("")
        rows[-1] += word
    return [row.rstrip() for row in rows]


def _claude_box(typed: str, width: int | None = None) -> str:
    """A pane view of the input box holding `typed`, unsent. Without `width`
    the pane is as wide as the longest line needs, so nothing wraps: the test's
    own temporary path is longer than a real pane is wide, and how a long path
    wraps is pinned by run qaw3's recorded box (`QAW3_RESIDUE` in
    test_dispatch_robustness.py)."""
    shown = _claude_shows(typed).split("\n")
    if width is None:
        width = max(88, max(len(line) for line in shown) + 4)
    rule = "\u2500" * width
    out = []
    for n, line in enumerate(shown):
        if not line:
            out.append("")
            continue
        pieces = _wrap(line, width - 4)
        out.append(("\u276f " if n == 0 else "  ") + pieces[0])
        out.extend("  " + c for c in pieces[1:])
    return (f"\u273b Baked for 0s\n{rule}\n" + "\n".join(out)
            + f"\n{rule}\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n")


EMPTY_BOX = ("\u273b Baked for 0s\n" + "\u2500" * 88 + "\n\u276f \n" + "\u2500" * 88
             + "\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n")


class _Clock:
    """The dispatcher's `time`: `monotonic()` advances a second per call,
    `sleep` only records, and `time()` is fixed ten seconds in the past so a
    dispatch marker is older than any file written after it."""

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


class _StallingCli:
    """Claude Code's pane as Herdr shows it, for a prompt whose send stalls.

    The first `agent prompt` types its text into the box and fails with the
    transient `agent_prompt_stalled`; a second one fails the test. Until Enter
    is pressed every screen read shows the box as Claude Code shows it
    (`shown` overrides the text, for a box holding something else); `pane
    send-keys <pane> Enter` submits it: the box empties and a structured role's
    answer file is written. Any other call fails the test.
    """

    def __init__(self, answer: Path | None) -> None:
        self.calls: list[list[str]] = []
        self.prompts = 0
        self.typed = ""
        self.shown: str | None = None
        self.entered = False
        self.answer = answer

    @property
    def enters(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["pane", "send-keys"]]

    def __call__(self, args, purpose, *, text=False):
        self.calls.append(list(args))
        verb = args[1:3]
        if verb == ["agent", "prompt"]:
            self.prompts += 1
            if self.prompts > 1:
                pytest.fail("a stalled prompt was sent again: it would append to the unsent one")
            self.typed = args[4]
            raise DispatchError(f"{purpose} failed: agent_prompt_stalled", transient=True,
                                retry_after=dispatchers.STALL_RETRY_AFTER_S)
        if verb == ["agent", "get"]:
            return {"result": {"agent": {"pane_id": TARGET, "workspace_id": "w0",
                                         "tab_id": "w0:t1", "agent": "claude",
                                         "agent_status": "idle", "cwd": "/tmp"}}}
        if verb in (["agent", "read"], ["pane", "read"]):
            assert text and args[-2:] == ["--source", "visible"]
            if self.entered:
                return EMPTY_BOX
            return _claude_box(self.typed if self.shown is None else self.shown)
        if verb == ["pane", "send-keys"]:
            assert args[3:] == [TARGET, "Enter"]
            self.entered = True
            if self.answer is not None:
                self.answer.write_text('{"ok": true}', encoding="utf-8")
            return ""
        pytest.fail(f"unexpected herdr call {args}")


def _stalling(tmp_path: Path, monkeypatch, role: Role):
    d, _ = _dispatcher(tmp_path, monkeypatch)
    d.own_panes = [TARGET]
    state = _state()
    answer = d._answer_path(role, state) if role in dispatchers.STRUCTURED_ROLES else None
    cli = _StallingCli(answer)
    monkeypatch.setattr(d, "_cli", cli)
    monkeypatch.setattr(dispatchers, "time", _Clock())
    return d, cli, state


def test_claude_code_collapse_model(tmp_path):
    """The model the stall tests stand on, pinned: a box of at most 10,000
    characters is shown whole (stripped); one more keeps the first and last 500
    around a marker that counts the lines between; a 50,073-character prompt
    leaves about the 1,073 characters qfu's box held."""
    whole = "x" * (CLAUDE_COLLAPSE_CHARS - 1) + "\n"
    assert _claude_shows(whole) == whole.strip()
    assert _claude_shows("x" * CLAUDE_COLLAPSE_CHARS) == "x" * CLAUDE_COLLAPSE_CHARS
    text = ("line\n" * 4000)[:CLAUDE_COLLAPSE_CHARS + 1]
    stripped = text.strip()
    middle = stripped[CLAUDE_COLLAPSE_KEEP:-CLAUDE_COLLAPSE_KEEP]
    assert _claude_shows(text) == (
        stripped[:500] + f"[...Truncated text #1 +{middle.count(chr(10))} lines...]"
        + stripped[-500:])
    qfu_sized = ("A line of a planner prompt of the size of qfu's.\n" * 1100)[:50_073]
    pending = dispatchers._pending_input(_claude_box(qfu_sized))
    assert pending is not None and 1000 < len(pending) < 1200


def test_collapsed_box_proves_nothing_but_the_pointer_box_does(tmp_path):
    """`_same_input` cannot recognise a collapsed box (run qfu's refusal) and
    does recognise the pointer's box. A property of the comparator and the
    model, independent of the routing; the pointer's box with one digest
    character changed is not the pointer."""
    prompt = "Plain line of a planner prompt.\n" * 1500          # about 48 KB
    answers = tmp_path / "answers"
    pointer = (dispatchers._pointer_prompt(answers / "i1-a0-planner.prompt.md",
                                           len(prompt), "e" * 64)
               + _answer_instruction(answers / "i1-a0-planner.json"))
    for sent, expected in ((prompt, False), (pointer, True)):
        view = _claude_box(sent)
        pending = dispatchers._pending_input(view)
        assert dispatchers._same_input(pending, sent, dispatchers._box_width(view)) is expected
    view = _claude_box(pointer.replace("e" * 64, "e" * 63 + "f"))
    assert dispatchers._same_input(dispatchers._pending_input(view), pointer,
                                   dispatchers._box_width(view)) is False


@pytest.mark.parametrize("size", [pytest.param(20_000, id="probe-20k"),
                                  pytest.param(45_000, id="qfu-45k"),
                                  pytest.param(70_000, id="above-64k")])
@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_stalled_prompt_is_resubmitted_not_refused(tmp_path, monkeypatch, role, size):
    """A prompt between 8 and 64 KiB used to go inline, stall, show collapsed,
    and be refused by the stalled-submit recovery. Through the pointer the box
    holds the pointer whole: the retry after the back-off finds exactly this
    dispatch's text, presses Enter once, sends nothing again, and collects the
    answer. The 70,000-byte case is the control: it went through the file
    before this change too."""
    d, cli, state = _stalling(tmp_path, monkeypatch, role)
    prompt = _pad(d, role, size)
    full = _full(d, role, prompt, state)
    assert len(full.encode("utf-8")) == size
    with pytest.raises(DispatchError) as stalled:
        d.dispatch(role, prompt, state=state)
    assert stalled.value.transient
    assert stalled.value.retry_after == dispatchers.STALL_RETRY_AFTER_S
    result = d.dispatch(role, prompt, state=state)       # the controller asks again
    assert cli.prompts == 1
    assert cli.enters == [["herdr", "pane", "send-keys", TARGET, "Enter"]]
    assert "Enter is pressed once" in "\n".join(state.history)
    assert d._unsettled() == {} and d._stalls() == {}
    if role in dispatchers.STRUCTURED_ROLES:
        assert result == '{"ok": true}'
    else:
        assert result.startswith("developer run finished")
    name = f"i1-a0-{role.value}.prompt.md"
    assert full not in cli.typed
    assert str(d.answers_dir.resolve() / name) in cli.typed
    assert (d.answers_dir / name).read_bytes() == full.encode("utf-8")


@pytest.mark.parametrize("role", [Role.PLANNER, Role.DEVELOPER], ids=lambda r: r.value)
def test_stalled_prompt_with_another_text_in_the_box_is_refused(tmp_path, monkeypatch, role):
    """Control of the rig: the same stalled 45,000-byte prompt, but the box
    holds the text sent with its first "a" changed to "b". The retry is refused,
    Enter is not pressed, nothing is sent again."""
    d, cli, state = _stalling(tmp_path, monkeypatch, role)
    prompt = _pad(d, role, 45_000)
    with pytest.raises(DispatchError):
        d.dispatch(role, prompt, state=state)
    other = cli.typed.replace("a", "b", 1)
    assert other != cli.typed
    cli.shown = other
    with pytest.raises(PaneNotClean) as refused:
        d.dispatch(role, prompt, state=state)
    assert "Enter alone is not pressed" in str(refused.value)
    assert cli.enters == [] and cli.prompts == 1


@pytest.mark.parametrize("size", [8192, 8193, 20_000, 45_000, 70_000], ids=str)
@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_nothing_sent_exceeds_what_claude_code_shows_whole(tmp_path, monkeypatch, role, size):
    """The text of every `agent prompt` stays within what Claude Code shows
    uncollapsed. Up to 8 KiB it is the prompt itself (8,192 bytes are at most
    8,192 UTF-16 units); above, a pointer -- about 1.2 KB and 25 lines on a run
    path; the bounds here (2,000 characters, 40 lines) only guard against bloat
    and include the test's temporary path."""
    _d, cli, full = _send(tmp_path, monkeypatch, role, size)
    sent = cli.calls[0][4]
    assert _utf16_units(sent.strip()) <= CLAUDE_COLLAPSE_CHARS
    if size <= 8192:
        assert sent == full
    else:
        assert len(sent) < 2000 and sent.count("\n") <= 40, (len(sent), sent.count("\n"))


def test_pointer_with_both_paths_at_the_path_limit_is_shown_whole():
    """Worst case: the prompt file's and the answer file's paths are each
    PATH_MAX - 1 bytes. The pointer with its answer instruction is then 9,154
    characters, still below the collapse limit."""
    tail = "/answers/i1-a0-planner.prompt.md"
    deep = Path("/" + "d" * (PATH_MAX - 1 - len(tail) - 1)) / "answers"
    prompt_path = deep / "i1-a0-planner.prompt.md"
    answer_path = deep / "i1-a0-planner.json"
    assert len(str(prompt_path)) == PATH_MAX - 1
    pointer = (dispatchers._pointer_prompt(prompt_path, 10 ** 9, "f" * 64)
               + _answer_instruction(answer_path))
    assert _utf16_units(pointer.strip()) < CLAUDE_COLLAPSE_CHARS
