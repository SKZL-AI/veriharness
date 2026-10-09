"""Concrete `RoleDispatcher` implementations.

Up to this point there was only the Protocol and the tests' `FakeDispatcher` --
an adversarial review rightly pointed out that `firstmate.py` and `herdr.py`
were not imported by a single line, which left A01/A02/A12 unprovable.

Two adapters, with different evidentiary weight:

* **`HerdrDispatcher`** starts every role as its own agent in its own Herdr
  pane. That yields the exact session, workspace and pane IDs that A01
  demands, and it satisfies "no silent tmux fallback": `require_herdr()`
  refuses to start outside of Herdr.
* **`HarnessDispatcher`** calls the same harness CLIs non-interactively as a
  subprocess. More robust and runnable without Herdr -- but **without a Herdr
  endpoint and therefore no A01 evidence**. It is the honest route for
  development and for machines without Herdr, not for acceptance.

## Why the answer goes into a file

The structured role output is **not** read from the terminal text. A pane
contains TUI frames, status lines and wrapped lines; fishing JSON out of that
is brittle, and the handoff explicitly declines to call terminal text a
dependable state. Instead every role is handed a file path and writes its
answer there. The dispatcher reads the file -- deterministic,
checkable, and the path shows up as evidence in the receipt directory.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path
from time import monotonic as _real_monotonic
from typing import NamedTuple

from . import herdr, pane_ledger, procs
from .agentcontrol import (OWNED_FILE, AgentControlAdapter, AgentInfo, HerdrAgentControl,
                           OwnedAgentControl, OwnedAgentRegistry, ProgramAgentRole)
from .contracts import Role, RunState, utcnow
from .controller import AnswerMissing, DispatchError, PaneNotClean, WaitingForApproval

class PaneRelease(NamedTuple):
    """What `HerdrDispatcher.release_role` answers: `closed` is True only when
    the role's pane is closed **and** what the role left running is proven
    gone. A `(False, "...")` tuple is truthy: test `.closed`, never the tuple."""

    closed: bool
    why: str


#: Roles whose answer has to come back structured. The developer leaves its
#: result in the artifact, not in an answer file.
STRUCTURED_ROLES = (Role.PLANNER, Role.QA)

#: Deadline for one role run. 15 minutes were too tight: in iteration 6 the
#: planner demonstrably spent 16+ minutes checking its own criteria against the
#: baseline ("does the mutant die on the annotation test instead of on the
#: arithmetic?") -- which is exactly the care that the discrimination check
#: demands of it. A deadline that cuts thorough work short trains shallow work.
DEFAULT_TIMEOUT_MS = 2_700_000        # 45 minutes
POLL_SECONDS = 2.0

#: How long the answer file may take to appear after a structured role's turn
#: ended, before the turn counts as ended without it (`AnswerMissing`).
ANSWER_GRACE_SECONDS = 30.0

#: The interval between two status reads while a role that ended its turn
#: without the answer has resumed by itself (`HerdrDispatcher.follow_up`). One
#: `herdr agent get` per interval, never a tight loop: never below 10 s.
STATUS_POLL_SECONDS = 30.0

#: Consecutive failed status reads that end that passive wait. A single failed
#: read inside it is retried at the next interval.
STATUS_READ_ATTEMPTS = 3

#: The back-off before a stalled prompt is sent again. Herdr reports
#: `agent_prompt_stalled` when the pane showed no state change within 5 s;
#: asking again at once asks the same pane in the same state.
STALL_RETRY_AFTER_S = 30.0

#: After Enter was pressed on a stalled prompt, how long the agent gets to show
#: that its turn started -- Herdr's own rule for `agent prompt --wait` is five
#: seconds (`herdr agent prompt --help`, 0.8.0).
SUBMIT_CONFIRM_SECONDS = 5.0

#: Before the first prompt of an agent HoH started itself, the dispatcher waits
#: until the agent shows an idle input box that has held still: one read every
#: READY_POLL_SECONDS, ready after READY_QUIET_POLLS further reads that agree
#: with the one before (about 3 s), given up after READY_MAX_POLLS reads (about
#: 60 s) -- counted in reads, never by the clock (`HerdrDispatcher._await_ready`).
#: `herdr agent start` returning says the agent is idle, not that its input
#: takes a submit: under Claude Code 2.1.292 every first prompt sent at once
#: stalled (probe, runs qgg to qfu4), with the text typed in a drawn box. No
#: version number is consulted. The quiet window is the margin of the probe
#: that went through (an idle wait and 3 s); it is SUSPECTED to be enough, and
#: a prompt that still stalls is recovered as before (`_settle`,
#: `_submit_stalled`).
READY_POLL_SECONDS = 1.0
READY_QUIET_POLLS = 3
READY_MAX_POLLS = 60

#: The wall-clock ceiling of the same wait, in seconds, on the **real** monotonic
#: clock (`_real_monotonic`, bound at import, so a test that replaces this
#: module's `time` -- the fakes step it by 1000 s a call -- does not move it).
#: The reads are the bound that matters; this one is for a Herdr read that
#: hangs (each read has the wrapper timeout, 180 s, and is checked between two
#: polls). Overridable per dispatcher: `ready_ceiling_s`, `ready_clock`.
READY_CEILING_SECONDS = 15 * 60.0

#: What Claude Code shows in an empty box of a fresh session (pane view of
#: run sstew4: `Try "fix lint errors"`): one line, `Try` and a quoted
#: suggestion. Read as an empty box, for an agent HoH started itself.
_PLACEHOLDER = re.compile(r'Try "[^"\n]*"')

#: Above this many UTF-8 bytes a role prompt is not sent as an argument but
#: through a prompt file in the answers directory, with a short pointer in
#: argv. Two reasons, one threshold:
#:
#: * Claude Code 2.1.292 keeps an input box longer than 10,000 characters as
#:   its first 500 characters, a marker and its last 500. A first prompt into
#:   a fresh pane stalls (runs ugt, qgg, qgg2, qfu, qfu2, qfu3: the prompt
#:   stayed unsent in the box); the stalled-submit recovery (`_settle`,
#:   `_submit_stalled`) presses Enter only when the box holds exactly the text
#:   this dispatch sent, and it refused qfu's collapsed 1,073 characters. The
#:   pointer is about 1.2 KiB and 25 lines (9,154 characters with both paths
#:   at the Linux path limit), never collapsed, so a stalled pointer is
#:   recovered. An inline prompt of at most 8 KiB is at most 8,192 UTF-16
#:   units and is shown whole.
#: * The argv limit is separate: Linux limits one argument to 131071 bytes
#:   plus its NUL (`MAX_ARG_STRLEN`); run 20261002t1501z-sstew4 stopped at its
#:   first developer dispatch with `[Errno 7] Argument list too long:
#:   'herdr'`. `ARGV_ELEMENT_LIMIT` below guards that, independent of this.
PROMPT_FILE_THRESHOLD = 8 * 1024

#: Characters a paste into Claude Code's input box may drop (run qgg2's box
#: read "Removed 4 invisible characters"). A prompt holding one goes through
#: the prompt file, whatever its size. The list is deliberately conservative:
#: format, control, private-use, unassigned, line and paragraph separators and
#: space separators (the newline and U+0020 are exempt), plus the complete
#: Default_Ignorable_Code_Point set and U+2800. Written as numbers only: this
#: source never holds one of these characters itself.
_PASTE_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp", "Zs"})
_PASTE_UNSAFE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C),
    (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F),
    (0x2800, 0x2800), (0x3164, 0x3164), (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)


def _paste_unsafe(text: str) -> bool:
    """True if `text` holds a character a paste may drop (see above)."""
    import unicodedata
    for ch in text:
        code = ord(ch)
        if ch == "\n" or 0x20 <= code <= 0x7E:
            continue
        if unicodedata.category(ch) in _PASTE_UNSAFE_CATEGORIES:
            return True
        if any(low <= code <= high for low, high in _PASTE_UNSAFE_RANGES):
            return True
    return False


#: The guard on every argv element `HerdrDispatcher.dispatch` builds, in UTF-8
#: bytes. Its own constant, deliberately independent of PROMPT_FILE_THRESHOLD:
#: a threshold raised (or a change that routes a long prompt inline again) must
#: still not reach the kernel as one oversized argument.
ARGV_ELEMENT_LIMIT = 64 * 1024

#: The audit trail of every prompt sent through a file, in the run directory.
#: Appended to before the dispatch, never read for a decision.
PROMPT_BINDINGS = "prompt-bindings.jsonl"

#: Upper bound for reading a dispatch marker: it is a small JSON object, and a
#: planted huge file must not exhaust the dispatcher.
_MARKER_READ_LIMIT = 1 << 20


def _refuse(message: str) -> DispatchError:
    return DispatchError(message, transient=False)


def _open_dir(path: Path, what: str) -> int:
    """Open a directory of this run from its lexical path, link-free at the
    final component (`O_NOFOLLOW`). Every leaf operation of a dispatch happens
    relative to the descriptor, so a name inside it is resolved exactly once,
    in the directory that was checked. Any failure -- `ELOOP` for a symlink,
    `ENOTDIR`, `ENOENT` -- is final: retrying does not change what is there.
    """
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _refuse(
            f"dispatch: the {what} {path} cannot be opened as a real directory "
            f"(not a symlink, not a file): {exc}. Nothing was written.") from exc


def _version_name(name: str, entry: str) -> bool:
    """Is `entry` a version name of `name`: `<name>.v<stamp>.<counter>`?"""
    if not entry.startswith(name + ".v"):
        return False
    stamp, _, counter = entry[len(name) + 2:].rpartition(".")
    return bool(stamp) and counter.isdigit()


def _check_leaf(dir_fd: int, name: str, where: str) -> str | None:
    """Examine the current name before anything is written to it.

    Returns "absent", "present" (a regular file with exactly one link, to be
    parked before it is replaced) or the version name under which it is
    already parked. Anything else is refused: a symlink, a directory, a FIFO, a
    device -- and a regular file with a second link, which may be a file
    outside this directory that a role linked in. The one second link that is
    accepted is a versioning that crashed between `os.link` and `os.replace`:
    a version name of this very name, in the same directory, same inode.
    """
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise _refuse(f"dispatch: refusing {name!r} in {where}: lstat failed: "
                      f"{exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        kind = "a symbolic link" if stat.S_ISLNK(st.st_mode) else "not a regular file"
        raise _refuse(f"dispatch: refusing {name!r} in {where}: it is {kind}. "
                      "Nothing is written through it.")
    if st.st_nlink == 1:
        return "present"
    if st.st_nlink == 2:
        for entry in sorted(os.listdir(dir_fd)):
            if not _version_name(name, entry):
                continue
            try:
                other = os.lstat(entry, dir_fd=dir_fd)
            except OSError:
                continue
            if (stat.S_ISREG(other.st_mode)
                    and (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino)):
                return entry
    raise _refuse(f"dispatch: refusing {name!r} in {where}: it has {st.st_nlink} "
                  "links, and the second is not an interrupted versioning of it. "
                  "Nothing is written through it.")


def _write_leaf(dir_fd: int, name: str, data: bytes, where: str) -> str | None:
    """Make `data` the content of `name` (relative to `dir_fd`) without losing
    what was there, and without following a link at any name.

    Order: the current name is examined (`_check_leaf`); the new content goes
    to a fresh temp file (`O_EXCL | O_NOFOLLOW`, fsynced); an existing current
    file is hard-linked to a version name -- timestamp plus a counter, so
    several writes within one second each keep their own version; only then is
    the temp file `os.replace`d onto the name and the directory fsynced.
    `os.link` never creates over an existing name, a taken version name moves
    on to the next counter, and any other link failure fails the dispatch:
    there is no copy fallback. Nothing is unlinked -- a temp file left by a
    failure stays, and the error names it.

    **The condition that carries this:** the current file is never written in
    place. A hard link shares the inode, so an in-place write would change the
    parked version along with it; the `os.replace` gives the current name a
    new inode instead. Returns the version name, or None if there was nothing
    to park.
    """
    state = _check_leaf(dir_fd, name, where)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    while True:
        tmp = f".{name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise _refuse(f"dispatch: no temp file for {name!r} in {where}: "
                          f"{exc}") from exc
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except OSError as exc:
        raise _refuse(f"dispatch: writing the temp file {tmp!r} in {where} for "
                      f"{name!r} failed (left in place): {exc}") from exc
    finally:
        os.close(fd)

    parked = None if state in ("absent", "present") else state
    if state == "present":
        stamp = utcnow()
        counter = 1
        while True:
            candidate = f"{name}.v{stamp}.{counter}"
            try:
                # The name itself is parked, not what a symlink points to.
                os.link(name, candidate, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                        follow_symlinks=False)
            except FileExistsError:
                counter += 1
                continue
            except OSError as exc:
                raise _refuse(
                    f"dispatch: parking {name!r} in {where} as {candidate!r} "
                    f"failed: {exc}. No copy fallback; the temp file {tmp!r} "
                    "is left in place.") from exc
            parked = candidate
            break
    try:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        raise _refuse(f"dispatch: replacing {name!r} in {where} with the temp "
                      f"file {tmp!r} failed (left in place): {exc}") from exc
    return parked


def _append_audit(dir_fd: int, record: dict, where: str) -> None:
    """Append one JSON line to the run's prompt-binding audit file.

    Opened link-free relative to the run directory and refused unless it is a
    regular file with exactly one link, so a hard link to a file elsewhere is
    never appended to. `O_NONBLOCK` keeps a planted FIFO from blocking.
    """
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
             | os.O_NONBLOCK)
    try:
        fd = os.open(PROMPT_BINDINGS, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise _refuse(f"dispatch: refusing {PROMPT_BINDINGS!r} in {where}: "
                      f"{exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise _refuse(
                f"dispatch: refusing {PROMPT_BINDINGS!r} in {where}: not a "
                f"regular file with exactly one link (links: {st.st_nlink}). "
                "Nothing was appended.")
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except OSError as exc:
        raise _refuse(f"dispatch: appending to {PROMPT_BINDINGS!r} in {where} "
                      f"failed: {exc}") from exc
    finally:
        os.close(fd)
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise _refuse(f"dispatch: fsync of {where} failed: {exc}") from exc


def _check_audit(dir_fd: int, where: str) -> None:
    """The audit file examined before anything of the dispatch is written."""
    try:
        st = os.lstat(PROMPT_BINDINGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _refuse(f"dispatch: refusing {PROMPT_BINDINGS!r} in {where}: "
                      f"{exc}") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise _refuse(f"dispatch: refusing {PROMPT_BINDINGS!r} in {where}: not a "
                      f"regular file with exactly one link (links: {st.st_nlink}). "
                      "Nothing is written through it.")


def _rehash(dir_fd: int, name: str, size: int) -> str:
    """SHA-256 of `name` relative to `dir_fd`, or a description of why there is
    none -- which never equals a digest, so it counts as a mismatch.

    Opened `O_NOFOLLOW | O_NONBLOCK` and read only if `fstat` shows a regular
    file of exactly the expected size: a FIFO, a device or a huge hard-linked
    file can neither block nor exhaust the dispatcher. At most `size + 1`
    bytes are read, so a file growing after the `fstat` still mismatches.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=dir_fd)
    except OSError as exc:
        return f"(unreadable: {exc})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "(not a regular file)"
        if st.st_size != size:
            return f"(size {st.st_size} bytes, {size} expected)"
        digest = hashlib.sha256()
        left = size + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            digest.update(chunk)
            left -= len(chunk)
        return digest.hexdigest()
    except OSError as exc:
        return f"(unreadable: {exc})"
    finally:
        os.close(fd)


def _pointer_prompt(prompt_path: Path, size: int, digest: str) -> str:
    """The short prompt that stands in for one sent through a prompt file.

    Its words are pinned by the recorded box of run qaw3
    (`QAW3_RESIDUE` in tests/test_dispatch_robustness.py); "too long for one
    command-line argument" no longer names the only reason (see
    PROMPT_FILE_THRESHOLD)."""
    return f"""[prompt-file]
Your instructions for this turn are too long for one command-line argument,
so they were written to a file. That file **is** your prompt; this message
only points to it.

  path:    {prompt_path}
  bytes:   {size} (UTF-8)
  sha256:  {digest}

Read the **whole** file before you do anything else. A long file comes back
in parts: read it page by page, passing an offset each time, and keep going
until you have reached the end of the file -- not just its beginning. Then
follow it as your instructions. Do not change, move or delete the file: the
dispatcher checks its SHA-256 again after your turn, and a changed file fails
the dispatch.
"""


def _answer_instruction(path: Path) -> str:
    return f"""

[answer-file]
Write your JSON answer **as a file** to:

  {path}

Only the JSON object, nothing else -- no code fence, no surrounding text. Reply
in the chat with a single sentence saying that the file is written. The file is
authoritative; your chat text is not evaluated.
"""


#: The first characters that put a coding agent's input box into another mode
#: instead of taking a prompt: `/` a slash command, `!` a shell command. Run
#: padopt: a pointer beginning with `/prompt-file` was appended to an unsent
#: one, and Claude Code 2.1.288 answered `Unknown command: /prompt-file` --
#: no turn started. No dispatched message starts with one of them.
INPUT_MODE_PREFIXES = ("/", "!")

#: The mark of a pane Herdr restored and HoH adopted (`_ensure_agent`). It
#: holds no digest: what is in its input box was typed by an earlier process,
#: or by nobody, and is checked before its first prompt (`_check_restored`).
RESTORED = "restored"

#: The horizontal rule above and below Claude Code's input box, and the mark
#: that opens the box's first line (pane view of padopt at 01:17:47Z).
_BOX_RULE = "\u2500"
_INPUT_MARK = "\u276f"


def _first_line_guard(text: str, what: str) -> None:
    """Refuse a message whose first character switches the input mode.

    Raised before anything is sent: a refused message is never sent.
    """
    head = text.lstrip()
    if head.startswith(INPUT_MODE_PREFIXES):
        raise _refuse(
            f"{what}: the message starts with {head[:1]!r}, which an agent's "
            "input box reads as a command (a slash or shell command), not as "
            f"a prompt: {head[:80]!r}. It is not sent.")


def _pending_input(view: str) -> str | None:
    """The text in Claude Code's input box, read off a pane view.

    The last two rule lines frame the box, and its first body line must start
    with the input mark. Returns the box's text, `""` for an empty box, or
    None when no such box is found -- another agent kind, a dialog, a view cut
    off -- which proves nothing about what is in the box.
    """
    lines = view.splitlines()
    rules = [i for i, line in enumerate(lines)
             if len(line.strip()) >= 3 and set(line.strip()) == {_BOX_RULE}]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2], rules[-1]
    body = lines[top + 1:bottom]
    if not body or not body[0].lstrip().startswith(_INPUT_MARK):
        return None
    first = body[0].lstrip()[len(_INPUT_MARK):]
    text = "\n".join([first, *body[1:]])
    return text.strip()


def _box_width(view: str) -> int | None:
    """The width a line of Claude Code's input box can take, read off the
    pane view: the box's bottom rule spans the pane, and a line breaks two
    columns short of it (runs qaw3 and qaw4: rule 88 columns, every broken
    line 86). None when no rule is found."""
    rules = [ln.rstrip() for ln in view.splitlines()
             if len(ln.strip()) >= 3 and set(ln.strip()) == {_BOX_RULE}]
    return len(rules[-1]) - 2 if rules else None


def _claude_rows(line: str, columns: int) -> list[str]:
    """The rows Claude Code's input box shows for one line of text (no newline)
    in a box whose text rows are `columns` wide.

    Read off the recorded boxes of runs qaw3, qaw4 and qfu4. The words of the
    line, split at single spaces (so a run of spaces is empty words), fill a
    row; a word that does not fit goes to the next row, and the spaces before
    it are dropped (recorded: `sha256:` + two spaces + a digest);
    nothing else is a break point -- not a hyphen: a path was cut mid-token at
    the full width after a hyphen. A word longer than a row starts on the
    current row and is cut exactly at `columns`, then continues on further
    rows. A row that is full, or that a run of spaces fills, drops **one**
    space and no more: further spaces of the run start the next row (the
    model's reading, never recorded -- if Claude Code drops them all, the
    comparison refuses, which is the closed side). The indentation of the rows after the first is not part of
    `columns`: it is the box's, two columns.
    """
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
    return [row.rstrip(" ") for row in rows[:-1]] + rows[-1:]


def _same_input(box: str, sent: str, width: int | None) -> bool:
    """Does Claude Code's input box hold exactly `sent`, as typed?

    `box` is `_pending_input`'s text and `width` is `_box_width` of the same
    pane view. The comparison is a forward model: the rows the box would show
    for `sent` are computed (`_claude_rows`, per line of `sent`, at `width - 2`
    text columns -- the input mark and the indentation of the later rows are
    two columns), and the box must be exactly those rows. Every row after the
    first carries the two-space indentation, a blank line of `sent` is a blank
    box row, and no row is left over. So the only differences from `sent` that
    are accepted are the ones Claude Code itself makes: where a line breaks
    (at a space that does not fit, or mid-word for a word longer than a row),
    the spaces dropped at a break, and the indentation. The screen cannot show
    a dropped space, so a space (or a longer run) at such a break is not told
    from none: `"x" * 74 + " tail"` and `"x" * 74 + "tail"` draw the same box
    in a 76-wide pane, and so do one and two spaces where a word moves to the
    next row. That is the whole of the whitespace the comparison forgives; a
    longer run than the row has room for is left to the closed side.
    Anything else -- other text, a missing or extra character or space inside
    a row, a break where Claude Code makes none, an unindented row, an unknown
    width -- is a mismatch.
    """
    if not width or width < 3:
        return False
    want: list[str] = []
    for line in sent.strip().split("\n"):
        want.extend(_claude_rows(line, width - 2) if line else [""])
    raw = box.split("\n")
    got = [raw[0]]
    for ln in raw[1:]:
        if ln.strip() == "":
            got.append("")
        elif ln.startswith("  "):
            got.append(ln[2:])
        else:
            return False
    return got == want


class _Resubmit:
    """`_settle`'s answer when the box holds exactly this dispatch's stalled
    prompt: `refusal` is what it would have raised, raised by
    `_submit_stalled` if any of its own checks fails."""

    def __init__(self, refusal: BaseException) -> None:
        self.refusal = refusal


def _read_json_leaf(dir_fd: int, name: str) -> dict | None:
    """A small JSON record, read link-free relative to `dir_fd`; None for
    anything but a regular file holding a JSON object."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, 1 << 16)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class _Base:
    """Shared parts: answer path, reading the file, role profiles."""

    def __init__(
        self,
        *,
        answers_dir: Path,
        profiles: dict[Role, str] | None = None,
        models: dict[Role, str] | None = None,
        efforts: dict[Role, str] | None = None,
    ) -> None:
        #: Model and effort level per role. Empty means "whatever the harness
        #: defaults to" -- which is what HoH did exclusively until 2026-09-08.
        self.models = models or {}
        self.efforts = efforts or {}
        #: P1-09. What each role may do without asking, decided before the run
        #: and written into the run directory. None keeps the historical
        #: behaviour: the role inherits the operator's interactive defaults and
        #: a prompt stops the run for a human (O198).
        self.approval_policy = None
        self.answers_dir = Path(answers_dir)
        self.answers_dir.mkdir(parents=True, exist_ok=True)
        #: The run directory. Boundary for reusing an agent that Herdr
        #: restored: only what sits below here belongs to this run.
        self.store_dir = self.answers_dir.parent
        #: Root of the check directories. Planner and QA work there so that
        #: they do not sit in the evidence directory -- which is why it counts
        #: as owned by this run.
        self.arenas_dir = self.store_dir.parent / "_arenas" / self.store_dir.name
        self.profiles = profiles or {}
        self.endpoints: dict[Role, str] = {}
        #: Panes whose cleanup could not be confirmed. Lives here in the base
        #: class so that **every** dispatcher has the attribute: otherwise the
        #: reader in `cli.cmd_run` fell back to an empty default and reported
        #: "nothing unconfirmed" without any error becoming visible. For the
        #: `HarnessDispatcher` the empty list is the *correct* answer -- it
        #: starts no panes and therefore has nothing left open.
        self.unconfirmed: list[str] = []
        #: Per-role working directory. QA belongs in the arena, not in the
        #: live tree: a prompt is not enforcement (handoff §5), and its own
        #: artifacts otherwise broke the candidate binding.
        self.role_cwd: dict[Role, str] = {}
        #: Per-role scratch: the `TMPDIR` of the role's pane (and so of its pytest), so no
        #: role touches the shared `/tmp/pytest-of-<user>` (`role_scratch_env`). Empty
        #: until `register_role_scratch` runs: `build_dispatcher` does it, and a
        #: dispatcher built any other way (a test, `__new__`) makes no directory.
        self.role_scratch: dict[Role, Path] = {}

    def register_role_scratch(self) -> None:
        """Make each role's scratch directory under the run's arena root, **before
        any dispatch** (the witness compares that root's listing around one), or
        raise `DispatchError` -- never fall back to `/tmp`. Idempotent: a resumed run
        finds its directories again."""
        self.role_scratch = _role_scratch_dirs(self.arenas_dir)

    def ensure_role_scratch(self, role: Role) -> None:
        """Make `role`'s scratch directory again if it is gone (`exist_ok`), or raise
        `DispatchError`. The controller calls it **before** it takes the capability
        witness of a dispatch: a directory that appears during one is reported as a
        new child of the arena root. A pane whose `TMPDIR` names a missing directory
        makes Python fall back to `/tmp` -- the incident this change exists to prevent --
        so nothing may leave the directory missing at dispatch time. A no-op for a
        dispatcher nobody registered (`role_scratch == {}`)."""
        if role in self.__dict__.get("role_scratch", {}):
            self.role_scratch.update(_role_scratch_dirs(self.arenas_dir, (role,)))

    def _scratch_env(self, role: Role) -> dict[str, str]:
        """What `role_scratch_env` gives this role; empty for a dispatcher built
        without `__init__` (several tests do) that has registered no scratch."""
        scratch = self.__dict__.get("role_scratch", {}).get(role)
        return role_scratch_env(scratch) if scratch else {}

    def _answer_path(self, role: Role, state: RunState) -> Path:
        return self.answers_dir / f"i{state.iteration}-a{state.attempt}-{role.value}.json"

    #: Panes left blocked by a role that had already delivered. Kept so the
    #: situation stays visible rather than becoming invisible once it stops
    #: failing the run.
    _note_blocked_but_answered: dict = {}

    def _answered(self, path: Path, role: Role) -> bool:
        """Did the role deliver through its declared output channel?

        A structured role answers through exactly one file. Whether it answered
        is therefore a question about that file, and not about what its pane
        looks like afterwards.
        """
        if role not in STRUCTURED_ROLES:
            return False
        try:
            return bool(path.exists() and path.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    def _delivered(self, role: Role, before: str | None) -> bool:
        """Did an unstructured role deliver the thing it delivers?

        The developer's answer is not a file in the answers directory -- it is
        the artifact in the arena. So the question "did it deliver" is answered
        the same way as for the structured roles, by looking at the declared
        output rather than at the pane: did the working tree change.

        Measured on a real confinement run. The developer wrote `fib.py`,
        checked its own hash against the acceptance criterion, said *"Done --
        artifact left in the working tree for QA to execute independently"* and
        left its pane at a prompt. Herdr reports such a pane as `blocked`, and
        the run stopped on a dialog that was not there.
        """
        if role in STRUCTURED_ROLES or before is None:
            return False
        from .capability import tree_digest

        destination = self.role_cwd.get(role) or getattr(self, "cwd", None)
        return bool(destination) and tree_digest(Path(destination)) != before

    # -- O199: collecting the answer an approved dialog enabled -------------- #

    @staticmethod
    def _prompt_digest(full: str) -> str:
        import hashlib
        return hashlib.sha256(full.encode("utf-8")).hexdigest()

    @staticmethod
    def _marker_for(answer_path: Path) -> Path:
        return answer_path.with_name(answer_path.name + ".dispatch.json")

    def _write_dispatch_marker(self, answer_path: Path, full: str,
                               dir_fd: int | None = None) -> None:
        """Remember what was asked, so a later dispatch can tell whether the
        answer on disk answers *this* question. A previous marker is kept with
        a version suffix, never overwritten -- and, like the prompt marker,
        the new one goes to a temp file that is `os.replace`d onto the name,
        relative to the answers directory's descriptor, never through a link.
        """
        own = dir_fd is None
        if own:
            dir_fd = _open_dir(answer_path.parent, "answers directory")
        try:
            _write_leaf(dir_fd, self._marker_for(answer_path).name, (json.dumps({
                "prompt_sha256": self._prompt_digest(full),
                "dispatched_at": time.time(),
            }) + "\n").encode("utf-8"), str(answer_path.parent))
        finally:
            if own:
                os.close(dir_fd)

    def _read_marker(self, answer_path: Path, dir_fd: int | None) -> bytes:
        """The dispatch marker's bytes, read link-free relative to the answers
        directory: `O_NOFOLLOW | O_NONBLOCK`, a regular file, bounded in size.
        Anything else raises OSError, which `_resumable_answer` reads as "no
        evidence it was asked"."""
        own = dir_fd is None
        if own:
            dir_fd = os.open(answer_path.parent,
                             os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(self._marker_for(answer_path).name,
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "dispatch marker is not a regular file")
                data = b""
                while len(data) <= _MARKER_READ_LIMIT:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        return data
                    data += chunk
                raise OSError(errno.EFBIG, "dispatch marker is implausibly large")
            finally:
                os.close(fd)
        finally:
            if own:
                os.close(dir_fd)

    def _resumable_answer(self, role: Role, answer_path: Path, full: str,
                          dir_fd: int | None = None) -> bool:
        """Is the answer on disk the completed answer to this very prompt?

        O199. A dialog aborted the iteration; the operator answered it; the
        role finished and wrote its answer; the resumed run then parked that
        answer as "an earlier round" and asked again -- so every approval
        enabled work the next resume threw away, and the supervised path
        could not converge.

        All four must hold, and any doubt means the answer is parked as
        before rather than trusted: the role answers through a file; a marker
        from the dispatch exists; that dispatch asked exactly this prompt
        (same digest -- a changed iteration, attempt, amendment or spec
        changes the prompt); and the answer was written after it was asked.
        """
        if role not in STRUCTURED_ROLES:
            return False
        try:
            meta = json.loads(self._read_marker(answer_path, dir_fd))
            written_at = answer_path.stat().st_mtime
            nonempty = bool(answer_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        if not isinstance(meta, dict):
            return False
        return (nonempty
                and meta.get("prompt_sha256") == self._prompt_digest(full)
                and written_at > float(meta.get("dispatched_at") or 0))

    def _read_answer(self, path: Path, role: Role) -> str:
        if not path.exists():
            raise DispatchError(
                f"{role.value} left no answer file behind ({path}). "
                "Without a structured answer there is nothing to evaluate."
            )
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise DispatchError(f"{role.value} wrote an empty answer file ({path})")
        return text

    def _agent_args(self, role: Role) -> list[str]:
        """Extra arguments for the harness CLI, per role.

        `herdr agent start` forwards everything after `--` to the agent's own
        executable, and the `claude` CLI accepts `--model` and `--effort`.
        Until 2026-09-08 HoH used neither, so every role ran on whatever the
        operator's default happened to be -- invisible in the run record, and
        not selectable per role.

        Model diversity is not a detail here. The runbook already states that
        "a different model in a fresh context is the strongest setup for the
        independent review": the QA role is the one whose independence carries
        the acceptance, so being able to give it a different model from the
        developer's is a property worth having rather than a convenience.

        Whatever is chosen lands in the receipt trail through the role
        endpoint, so a later reader can see which model produced which
        verdict.
        """
        extra: list[str] = []
        model = self.models.get(role)
        effort = self.efforts.get(role)
        if model:
            extra += ["--model", model]
        if effort:
            extra += ["--effort", effort]
        policy = getattr(self, "approval_policy", None)
        if policy is not None:
            kind = (self.profiles or {}).get(role)
            if kind != "claude":
                # Fail closed: a policy HoH cannot express to this harness is
                # not quietly dropped, it stops the run before the role starts.
                raise DispatchError(
                    f"the approval policy can be applied to Claude roles only; "
                    f"{role.value} is configured as {kind!r}. Run it without a "
                    "policy, or give the role the claude harness.")
            settings, prompt = policy.write_role_files(self.store_dir, role.value,
                                                       answers_dir=self.answers_dir)
            extra += policy.args(role.value, settings, prompt)
        return extra

    def endpoint_evidence(self, role: Role) -> str:
        return self.endpoints.get(role, "(no endpoint recorded)")


#: Variables every role pane is launched with.
#:
#: `PYTHONDONTWRITEBYTECODE` is here because of O215: a role session's own python
#: wrote `src/hoh/__pycache__/*.pyc` into a protected arena during its dispatch,
#: and the capability witness correctly reported that a protected tree had
#: changed. The check runner already suppresses bytecode on both of its paths
#: (`runner.py` for the unsandboxed one, `sandbox.py` for STRICT); role sessions
#: had no environment set at all.
#:
#: **This is hygiene, not enforcement, and the difference matters.** An agent that
#: wants to write bytecode still can -- `sys.dont_write_bytecode = False`,
#: `py_compile`, `python -m compileall`, or simply `env -u
#: PYTHONDONTWRITEBYTECODE python3` -- each verified to defeat it. What this
#: removes is the *accidental* artifact that was blocking honest runs. The
#: witness is unchanged and still fires on a planted `.pyc`, which is the
#: property that must not be traded away.
ROLE_PANE_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


def role_environment(scratch_env: "dict[str, str] | None" = None) -> dict[str, str]:
    """The environment for a role launched as a **direct** subprocess.

    Used by `HarnessDispatcher`, which starts the agent itself, so the child does
    inherit this. It is **not** the path that matters for O215: `HerdrDispatcher`
    is the only backend an evidencing run may use (`build_dispatcher` refuses
    anything else unless `--no-herdr` is asked for, and says in as many words that
    `--no-herdr` carries no acceptance value), and there the pane is a child of
    the Herdr server rather than of any process this module starts. That path is
    reached through `ROLE_PANE_ENV` and `--env` at tab create instead.

    The first version of this repair set only this function and was **inert on the
    mandatory path** -- caught by an independent reviewer, who measured that every
    pane is a direct child of a server started days earlier and that a pane's
    environment carries nothing from the CLI client.

    Everything else is inherited unchanged; passing the environment explicitly
    adds exactly one variable and removes none, which was measured rather than
    argued.
    """
    env = {**os.environ, **ROLE_PANE_ENV}
    if scratch_env:
        env.update(scratch_env)
    return env


#: Name prefix of a role's own scratch directory, a direct child of the run's arena
#: root. It carries the runner's own `.hoh-scratch-` prefix on purpose: the capability
#: witness (`controller._policy`) and `store.prune` already treat that prefix as "a
#: scratch sibling, not an arena", so a pytest tree growing in it during a dispatch is not
#: a protected-tree change.
ROLE_SCRATCH_PREFIX = ".hoh-scratch-role-"


def role_scratch_env(scratch: "Path | str") -> dict[str, str]:
    """`TMPDIR` for a role whose scratch directory is `scratch` -- and nothing else.

    Without it a role's `pytest` takes the shared default basetemp
    `/tmp/pytest-of-<user>`, which is foreign to the run and which pytest's own
    retention prunes -- the other sessions' numbered directories included (house
    rules, amendment A3 point 3). With it pytest's default becomes
    `$TMPDIR/pytest-of-<user>` inside the role's own directory: shared with no one,
    its retention touches only that role's earlier runs, and a run that is still
    alive holds a lock file that retention respects.

    **No `PYTEST_ADDOPTS`, no `--basetemp`, on purpose.** pytest empties an existing
    `--basetemp` before every run, and a static option can name only one path, so two
    pytest runs of one role would wipe each other's trees. The rule allows a `--basetemp`
    only as a fresh, not-yet-existing path per run, which an environment variable cannot
    express. A test holds that the product sets none.

    **Residual, stated and measured (`TMPDIR` is long).** A role directory below the
    run's arena root is 117-130 characters on the real layout, and a Unix socket path may
    not exceed 107 bytes: `multiprocessing.Manager()` and the `forkserver` start method
    make theirs below `TMPDIR` (`pymp-XXXXXXXX/listener-XXXXXXXX`, 32 more characters), so
    they fail with `AF_UNIX path too long` once `TMPDIR` is over 75. The runner's own check
    scratch is as long (117-123 unsandboxed, 137-153 sandboxed), so a check fails the same
    way a role does; the role prompt says so. No directory under the run's tree can be
    that short, and one outside it would be scratch the run does not own (the storage
    lifecycle registers a root before anything lives in it).
    """
    return {"TMPDIR": str(scratch)}


def _role_scratch_dirs(arenas_dir: Path, roles: "tuple[Role, ...] | None" = None) -> "dict[Role, Path]":
    """One scratch directory per role under the run's arena root, **made before any
    dispatch**: the witness compares the arena root's listing around a dispatch, so
    a directory created during the first one would be reported. Refuses (never
    falls back to `/tmp`) a name that is a link, not a directory, not ours, or not readable,
    writable and searchable by this user: Python skips such a `TMPDIR` without a word."""
    made: dict[Role, Path] = {}
    for role in (roles if roles is not None else tuple(Role)):
        directory = arenas_dir / f"{ROLE_SCRATCH_PREFIX}{role.value}"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = os.lstat(directory)
        except OSError as exc:
            raise DispatchError(f"the scratch directory of the {role.value} role cannot be "
                                f"made ({directory}): {exc}") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise DispatchError(f"{directory} is not a directory of this user's own; the "
                                f"{role.value} role gets no scratch from it")
        # A directory that exists is not yet a `TMPDIR`: `tempfile.gettempdir()` tries to
        # create a file in the candidate and, when it cannot, **silently takes the next one
        # -- `/tmp`** (the incident again). The kernel's answer for the effective user (mode
        # bits, a read-only file system, an ACL) is what Python's own probe will meet; root
        # is let through by the kernel, and for root Python uses the directory as well.
        if not os.access(directory, os.R_OK | os.W_OK | os.X_OK,
                         effective_ids=os.access in os.supports_effective_ids):
            raise DispatchError(
                f"the scratch directory of the {role.value} role, {directory} (mode "
                f"{stat.S_IMODE(info.st_mode):04o}), cannot be read, written and searched by "
                f"this user, and its tools would silently use /tmp instead; give it mode 0700 "
                f"(or move it aside) and resume the run")
        made[role] = directory
    return made


class HarnessDispatcher(_Base):
    """Calls the harness CLI non-interactively.

    **No A01 evidence**: no Herdr endpoint comes into being here. For
    development and for machines without Herdr; acceptance needs
    `HerdrDispatcher`.
    """

    #: Non-interactive invocation form per harness, checked against the
    #: installed CLI.
    COMMANDS: dict[str, list[str]] = {
        "claude": ["claude", "-p"],
        "pi": ["pi", "-p"],
        "kimi": ["kimi", "-p"],
        "codex": ["codex", "exec", "--skip-git-repo-check"],
    }

    def __init__(
        self,
        *,
        answers_dir: Path,
        profiles: dict[Role, str],
        cwd: Path | str,
        timeout: int = 900,
        models: dict[Role, str] | None = None,
        efforts: dict[Role, str] | None = None,
    ) -> None:
        super().__init__(answers_dir=answers_dir, profiles=profiles,
                         models=models, efforts=efforts)
        self.cwd = Path(cwd)
        self.timeout = timeout

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        """The answer arrives over **stdout**, not through a file.

        The first version had an answer file written here as well -- and failed
        immediately: `claude -p` runs with restricted permissions and may not
        create anything without approval ("Die Schreibberechtigung wurde nicht
        erteilt", the CLI's own wording). In non-interactive mode stdout is the
        intended and dependable channel; `roles.extract_json` tolerates
        surrounding text.
        """
        harness = self.profiles.get(role)
        if harness not in self.COMMANDS:
            raise DispatchError(
                f"no known harness for {role.value}: {harness!r}. "
                f"Known: {', '.join(sorted(self.COMMANDS))}"
            )

        try:
            proc = subprocess.run(
                [*self.COMMANDS[harness], *self._agent_args(role), prompt],
                cwd=self.role_cwd.get(role, self.cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=role_environment(self._scratch_env(role)),
            )
        except subprocess.TimeoutExpired as exc:
            raise DispatchError(
                f"{role.value} on {harness} gave no answer after {self.timeout}s",
                transient=True,
            ) from exc
        except OSError as exc:
            raise DispatchError(f"{harness} could not be started: {exc}") from exc

        self.endpoints[role] = f"subprocess:{harness}/rc={proc.returncode}"
        text = (proc.stdout or "").strip()

        if proc.returncode != 0 and not text:
            raise DispatchError(
                f"{harness} exited with {proc.returncode} and without output: "
                f"{(proc.stderr or '')[:300]}",
                transient=proc.returncode in (124, 143),
            )
        if role in STRUCTURED_ROLES and not text:
            raise DispatchError(f"{role.value} on {harness} produced no output")

        # Record the answer -- it is evidence even when it arrived over stdout.
        answer_path = self._answer_path(role, state)
        answer_path.write_text(text or "(no text)", encoding="utf-8")
        return text or "(no text)"


class HerdrDispatcher(_Base):
    """Starts every role as its own agent in its own Herdr pane.

    This is the route that can prove A01: the pane, tab and workspace IDs are
    real, verifiable endpoints. `require_herdr()` refuses to start outside a
    Herdr session -- a silent tmux fallback explicitly does not fulfill the
    assignment.
    """

    def __init__(
        self,
        *,
        answers_dir: Path,
        profiles: dict[Role, str],
        cwd: Path | str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        models: dict[Role, str] | None = None,
        efforts: dict[Role, str] | None = None,
        control: AgentControlAdapter | None = None,
        registry: OwnedAgentRegistry | None = None,
    ) -> None:
        super().__init__(answers_dir=answers_dir, profiles=profiles,
                         models=models, efforts=efforts)
        #: The durable record of the agents this run owns (`agentcontrol`).
        #: With one, every acting verb goes through `OwnedAgentControl` and a
        #: pane that is not in it is never closed; without one the ownership is
        #: the in-memory tables below only (what a test that seeds `own_panes`
        #: relies on). `build_dispatcher` always passes one.
        self.__dict__["_owned_registry"] = registry
        if control is None:
            herdr.require_herdr()
        else:
            self.__dict__["_control"] = (OwnedAgentControl(control, registry)
                                         if registry is not None else control)
        self.cwd = str(Path(cwd).resolve())
        self.timeout_ms = timeout_ms
        self.agents: dict[Role, str] = {}
        self.own_panes: list[str] = []
        self.own_tabs: list[str] = []
        self.unconfirmed: list[str] = []

    # -- Pane and agent management ------------------------------------------- #

    def _agent_name(self, role: Role, state: RunState) -> str:
        """The Herdr agent name for one role of one run.

        The unique part comes first. The earlier form was
        `hoh-{run_id[:12]}-{role}`, so any two runs whose ids shared twelve
        characters shared an agent name (O195): `repair-3-feature-a` and
        `repair-3-feature-b` both became `hoh-repair-3-fea-developer`. The
        ownership check below refuses a name taken elsewhere, so nothing was
        ever adopted -- the second run simply could not start. Harmless one run
        at a time; a hard stop for independent nodes running together.

        A digest of the *whole* run id leads, because Herdr truncates agent
        names at about 24 characters and uniqueness after that point is
        uniqueness it throws away. The readable id follows for the operator.
        """
        digest = hashlib.sha256(state.run_id.encode("utf-8")).hexdigest()[:7]
        readable = state.run_id.lower().replace("_", "-")
        name = f"h{digest}-{role.value}-{readable}"
        return "".join(c if (c.isalnum() or c == "-") else "-" for c in name)[:32]

    def _ensure_agent(self, role: Role, state: RunState) -> str:
        if role in self.agents:
            return self.agents[role]

        kind = self.profiles.get(role)
        if not kind:
            raise DispatchError(f"no harness configured for role {role.value}")

        # HoH uses exclusively agents that it started itself.
        #
        # An earlier version adopted any idle session of the same harness here
        # ("the captain often keeps an open Kimi session for QA"). But
        # `find_agent` returns the FIRST matching agent anywhere in Herdr --
        # and that was the captain's running research session, which then got
        # handed a role prompt that did not belong to it.
        #
        # That is an intrusion into someone else's work and falls under the
        # same boundary as "foreign panes are never closed". Convenience is no
        # reason to write into a session that is not ours.
        name = self._agent_name(role, state)

        # After a restart Herdr restores panes and agents by itself. After a
        # hard reset `hoh-a02-planner` was therefore already back -- and HoH
        # tried to hand out the same name a second time ("agent_name_taken").
        # The reuse here is **not** the intrusion that was removed earlier:
        # what is looked up is not "any idle agent of the same harness" but
        # exactly the one deterministic name from HoH's own namespace, and only
        # if it also sits in this run's directory. Everything else is rejected
        # instead of adopted.
        with self._acting_as(role, state):
            restored = self._reusable_agent(name, state)
        if restored is not None:
            # Known gap: **O215 does not reach a reused pane.** ROLE_PANE_ENV is
            # applied by `_new_tab_pane` through `herdr tab create --env`, and a
            # pane Herdr restored was created before that -- possibly before the
            # setting existed at all. Its environment cannot be changed from here;
            # `herdr agent start` has no `--env`, and rewriting a live shell's
            # environment from outside is not something to do quietly. So a
            # restored pane may still leave bytecode in a protected tree, the
            # witness will still catch it, and the run will still block -- which
            # is the correct outcome, just not the improved one.
            #
            # The same holds for TMPDIR (`role_scratch_env`): a pane restored from before
            # that setting has no role scratch, so its pytest takes the shared
            # `/tmp/pytest-of-<user>`, and nothing here or in the controller notices. Only
            # the role prompt does: it tells the role to stop unless `$TMPDIR` names a
            # `.hoh-scratch-role-` directory it can write.
            self.agents[role] = name
            self.endpoints[role] = restored
            self._unsettled()[name] = (RESTORED, 0.0, None)
            adopted = self._adopted().get(name)
            if adopted and adopted[1]:
                # The adopted pane is the role's, so its turn end can release it.
                self._role_panes()[role] = adopted[1]
            return name

        # One mark per pane, minted here and handed to `_new_tab_pane` through
        # an attribute (the signature tests stub does not change). Every process
        # the role starts in the pane inherits it (`procs`).
        mark = procs.mint(self.store_dir.name, role.value)
        made_in = self.role_cwd.get(role) or getattr(self, "cwd", None) or ""
        self.__dict__["_pane_mark"] = mark
        self.__dict__["_pane_scratch_env"] = self._scratch_env(role)
        try:
            with self._acting_as(role, state):
                pane = self._new_tab_pane(f"hoh {role.value}", cwd=self.role_cwd.get(role))
        finally:
            self.__dict__.pop("_pane_mark", None)
            self.__dict__.pop("_pane_scratch_env", None)
        self.own_panes.append(pane)
        # Recorded **before** `agent start`: a start that fails after Herdr
        # registered the agent leaves a pane that the run end can still
        # recognise as ours (identity check, `_close_own_pane`).
        self._role_panes()[role] = pane
        self._pane_marks()[pane] = mark
        self._pane_names().setdefault(pane, set()).add(name)
        if made_in:
            self._pane_dirs()[pane] = str(made_in)
        self._start_agent(name, kind, pane, self._agent_args(role))
        self.agents[role] = name
        # No prompt has been sent to it yet: `_await_ready` looks at it first.
        self._fresh().add(name)
        return name

    def _reusable_agent(self, name: str, state: RunState) -> str | None:
        """A HoH agent that Herdr restored -- or None.

        Returns the endpoint if an agent with **exactly this name** exists and
        its working directory belongs to this run. Raises if the name is taken
        but sits somewhere else: then it is not a restored agent of our own,
        and adopting it would be reaching into someone else's work.
        """
        try:
            snapshot = self.control.snapshot()
        except Exception:
            return None                      # no state readable -> start normally

        # A run has **two** legitimate places, not one: the run directory
        # (where planner and QA sit) and the authorized work tree (where the
        # developer sits). The first version knew only the run directory and
        # therefore rejected our own developer as a "foreign session". An
        # ownership test that does not know half of our own roles is not a
        # protection but a malfunction.
        # The ownership list is **derived**, not enumerated. Twice exactly that
        # enumeration went stale after a role had moved: first the developer's
        # work tree was missing, then the arena root where planner and QA sit
        # now. Both times the guard rejected one of our own roles as a "foreign
        # session" -- a protection that does not know our own roles is none.
        own_roots = self._own_roots(state)
        for row in snapshot.get("agents", []) or []:
            if str(row.get("name") or "") != name:
                continue
            cwd = str(row.get("foreground_cwd") or row.get("cwd") or "")
            if not cwd or not os.path.isabs(cwd):
                # An empty or relative directory resolves against HoH's own
                # working directory, so whether the agent counted as this
                # run's depended on where `hoh run` was started. Unknown is
                # no-touch: nothing is read, closed or sent.
                raise PaneNotClean(
                    f"The agent name {name} is taken, but Herdr reports no "
                    f"absolute directory for it ({cwd!r}), so it cannot be "
                    "proven to belong to this run. HoH does not adopt it; "
                    "nothing in its pane was read, closed or sent -- check the "
                    "pane and close it deliberately.")
            if own_roots and not self._is_under(cwd, own_roots):
                raise DispatchError(
                    f"The agent name {name} is taken, but the agent sits in {cwd!r} "
                    f"instead of in a directory of this run ({', '.join(own_roots)}). "
                    "HoH does not adopt a foreign session -- check the pane and "
                    "close it deliberately."
                )
            status = str(row.get("agent_status", "unknown"))
            if status in ("working", "blocked"):
                error = DispatchError if status == "working" else WaitingForApproval
                raise error(
                    f"{name} {'is still working' if status == 'working' else 'waits for an approval'} "
                    f"(pane {row.get('pane_id')}). A running worker is reattached, "
                    "not overwritten -- wait first or check the pane."
                )
            if status not in ("idle", "done"):
                # `unknown` proves no completion -- and here no liveness
                # either. After the hard reset Herdr had restored the pane and
                # the name binding, but the agent process inside it was dead:
                # the start failed with `agent_name_taken`, prompting after
                # that with `agent_not_ready`. A revived name without an agent
                # is a dead end that only clearing away our **own** pane leads
                # out of -- cwd is checked above.
                #
                # The **pane** is closed, never its tab: what else sits in
                # that tab is not ours to close (`_close_dead`). The result is
                # recorded, not swallowed.
                dead_pane = str(row.get("pane_id") or "")
                if dead_pane:
                    self._pane_dirs()[dead_pane] = cwd
                    self._close_dead(name, dead_pane, str(row.get("tab_id") or ""))
                return None
            ep = AgentInfo(
                backend=self.control.backend,
                name=name,
                status=status,
                pane_id=str(row.get("pane_id", "")),
                tab_id=str(row.get("tab_id", "")),
                workspace_id=str(row.get("workspace_id", "")),
                cwd=cwd,
            )
            tab = str(row.get("tab_id") or "")
            if tab and tab not in self.own_tabs:
                self.own_tabs.append(tab)
            self._register_adoption(name, tab, str(row.get("pane_id") or ""), cwd)
            self._adopted()[name] = (tab, str(row.get("pane_id") or ""))
            # What the live identity check compares with: the directory this
            # pane was found in, and the one agent name HoH adopted there.
            adopted_pane = str(row.get("pane_id") or "")
            if adopted_pane:
                self._pane_dirs()[adopted_pane] = cwd
                self._pane_names().setdefault(adopted_pane, set()).add(name)
            return ep.evidence()
        return None

    # -- Closing what HoH opened: only what it can prove is its own ---------- #
    #
    # Roles' panes live for one role turn (`release_role`) or until the run's
    # end (`close_own`). Every Herdr close goes through `_close_own_pane`
    # (the one stated exception is `_replace_restored`, which keeps its own,
    # stricter proof). Whatever cannot be proven is **kept and recorded**.

    def _lazy(self, name: str, factory):
        """A bookkeeping table made on first use, so a dispatcher built with
        `__new__` has it too (like `_unsettled`)."""
        value = self.__dict__.get(name)
        if value is None:
            value = self.__dict__[name] = factory()
        return value

    @property
    def control(self) -> AgentControlAdapter:
        """How this dispatcher starts, prompts, observes and stops an agent
        (`agentcontrol`). Herdr unless one was handed in; built on first use,
        like the tables above, so a dispatcher made with `__new__` has it.
        The Herdr one runs through `_cli`, which owns the process, the
        deadline and the error classification."""
        def build():
            base = HerdrAgentControl(lambda *a, **k: self._cli(*a, **k))
            registry = self.registry
            return OwnedAgentControl(base, registry) if registry is not None else base
        return self._lazy("_control", build)

    @property
    def registry(self) -> OwnedAgentRegistry | None:
        """The owned-agent registry, or None (see `__init__`)."""
        return self.__dict__.get("_owned_registry")

    def _registry_owns(self, pane: str) -> bool:
        """May this pane be closed as far as the registry is concerned? With no
        registry there is nothing to ask; with one, only an owned pane."""
        registry = self.registry
        return registry is None or registry.owns_pane(pane)

    def _recall_owned(self) -> None:
        """Take back into the tables what an earlier process of this run created
        and the registry still holds as owned: its pane, its tab, the agent name
        and the directory the identity check compares with. Only the registry's
        word, then the same checks as for any pane (`_close_own_pane`); a
        registry that cannot be read gives back nothing."""
        registry = self.registry
        if registry is None:
            return
        for owned in registry.owned():
            pane = owned.pane_id
            if pane not in self.own_panes:
                self.own_panes.append(pane)
            if owned.origin == "created" and owned.tab_id and owned.tab_id not in self.own_tabs:
                self.own_tabs.append(owned.tab_id)
            if owned.name:
                self._pane_names().setdefault(pane, set()).add(owned.name)
            if owned.cwd:
                self._pane_dirs().setdefault(pane, owned.cwd)

    def _acting_as(self, role: Role, state: RunState):
        """The role the agent being created or adopted is owned under."""
        control = self.control
        if not isinstance(control, OwnedAgentControl):
            return contextlib.nullcontext()
        return control.acting_as(ProgramAgentRole.pipeline(role),
                                 program=state.project_name, run_id=state.run_id)

    def _register_adoption(self, name: str, tab: str, pane: str, cwd: str) -> None:
        """Record a restored agent that was proven this run's as owned; a record
        that cannot be written fails the adoption (nothing is then acted on)."""
        control = self.control
        if not isinstance(control, OwnedAgentControl) or not pane:
            return
        try:
            control.adopt(pane_id=pane, tab_id=tab, name=name, cwd=cwd)
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise DispatchError(f"the restored agent {name} in pane {pane} could not "
                                f"be recorded as owned ({exc}); it is not adopted") from exc

    def _role_panes(self) -> dict:
        """role -> the pane HoH created for it or adopted for it."""
        return self._lazy("_role_pane_ids", dict)

    def _kept(self) -> dict:
        """pane -> why it is kept open (`keep_open`)."""
        return self._lazy("_kept_panes", dict)

    def _pane_marks(self) -> dict:
        """pane -> the mark its processes carry (`procs.mint`)."""
        return self._lazy("_pane_mark_ids", dict)

    def _pane_names(self) -> dict:
        """pane -> the agent names HoH recorded for it (the role's, recorded
        **before** `agent start`, and a dead agent's)."""
        return self._lazy("_pane_agent_names", dict)

    def _pane_dirs(self) -> dict:
        """pane -> the directory HoH made it in (or found it in, if adopted)."""
        return self._lazy("_pane_made_in", dict)

    def _own_pane_ids(self) -> set[str]:
        """Every pane that is HoH's own: created by it (`own_panes`) or the
        pane of an adopted agent. The record alone is not trusted for a close:
        `_identity_problem` looks at what is in the pane now."""
        ids = {str(p) for p in self.own_panes}
        ids.update(str(pane) for _tab, pane in self._adopted().values() if pane)
        return ids

    def _roots(self) -> list[str]:
        """The run's directories, for a pane with no record of where it was made."""
        return [str(p) for p in (
            getattr(self, "store_dir", None),
            getattr(self, "arenas_dir", None),
            getattr(self, "cwd", None),
            *self.role_cwd.values(),
        ) if p]

    def _record(self, event: str, **fields) -> bool:
        """One ledger line in the run directory; False if it cannot be written."""
        return pane_ledger.append(self.store_dir, event, **fields)

    @staticmethod
    def _rows(snapshot: dict, key: str) -> list[dict]:
        return [r for r in (snapshot.get(key) or []) if isinstance(r, dict)]

    def _read_snapshot(self) -> tuple[dict | None, str]:
        """`(snapshot, "")`, or `(None, why)` when it proves nothing: unreadable,
        lacking any of the lists `agents`, `panes`, `tabs`, or listing **no pane
        at all** (`hoh run` sits in a Herdr pane itself, so an empty snapshot is
        a failed read, never "the pane is gone")."""
        try:
            snapshot = self.control.snapshot()
        except Exception as exc:          # noqa: BLE001 - fail closed
            return None, f"the Herdr snapshot cannot be read ({exc})"
        if not isinstance(snapshot, dict):
            return None, "the Herdr snapshot is not an object"
        for key in ("agents", "panes", "tabs"):
            if not isinstance(snapshot.get(key), list):
                return None, f"the Herdr snapshot has no {key} list"
        if not self._rows(snapshot, "panes"):
            return None, "the Herdr snapshot lists no pane at all"
        return snapshot, ""

    def _listed(self, snapshot: dict) -> set[str]:
        """Every pane id the snapshot lists, as a pane or as an agent's pane."""
        return {str(r.get("pane_id") or "")
                for key in ("panes", "agents") for r in self._rows(snapshot, key)}

    def _herdr_says_gone(self, pane: str) -> bool:
        """Does Herdr itself answer `pane_not_found` for this pane?

        A snapshot that lists only some of the panes does not make a live pane
        gone: the snapshot **and** this answer decide."""
        try:
            self.control.pane_screen(pane, source=None, purpose=f"read pane {pane}")
        except DispatchError as exc:
            return "pane_not_found" in str(exc)
        except Exception:                 # noqa: BLE001 - unknown is not gone
            return False
        return False

    def keep_open(self, role: Role, why: str) -> None:
        """Keep the role's pane open until a later dispatch to it returns
        normally: a pane that waits for an approval, or whose state is not
        proven, is what the operator has to look at. Refused by **every** close.
        With no pane recorded for the role it does nothing."""
        pane = self._role_panes().get(role)
        if not pane:
            return
        self._kept()[pane] = why
        self._record("kept", role=role.value, pane=pane, why=why)

    def _lift_keep(self, role: Role) -> None:
        pane = self._role_panes().get(role)
        if pane:
            self._kept().pop(pane, None)

    def _close_dead(self, name: str, pane: str, tab: str) -> bool:
        """Close the pane of a restored agent whose process is dead -- the
        pane, **never its tab**. The pane is adopted for the length of the
        call; the tab is not in `own_tabs`, so it is not closed whole either.
        If the close is refused or unconfirmed the pane goes into `own_panes`
        (it is proven this run's), so the run end retries it, and the agent
        name stays recorded with the pane so the identity check recognises its
        row, which is likely still listed."""
        self._register_adoption(name, tab, pane, self._pane_dirs().get(pane, ""))
        self._pane_names().setdefault(pane, set()).add(name)
        self._adopted()[name] = (tab, pane)
        try:
            closed = self._close_own_pane(pane, role=None, reason="dead restored agent")
        finally:
            self._adopted().pop(name, None)
        if not closed and pane not in self.own_panes:
            self.own_panes.append(pane)
        return closed

    def _identity_problem(self, pane: str, snapshot: dict) -> str:
        """Why the pane is not what HoH recorded -- or "" if it is.

        Herdr documents that closed ids are not reused for a running server,
        not across a restart, so the recorded id alone is not trusted. If an
        agent sits in the pane, its name must be one HoH recorded for that pane.
        If none does, the pane's own directory must be absolute and under the
        directory HoH made that pane in (for a pane with no record: the run's
        directories). **Not a proof**: an operator's shell in the very same
        directory passes (`docs/LIMITATIONS.md`)."""
        known = self._pane_names().get(pane, set())
        sitting = [r for r in self._rows(snapshot, "agents")
                   if str(r.get("pane_id") or "") == pane]
        for row in sitting:
            name = str(row.get("name") or "")
            if name not in known:
                return (f"an agent {name!r} that HoH did not start for this pane "
                        "sits in it")
        if sitting:
            return ""
        rows = [r for r in self._rows(snapshot, "panes")
                if str(r.get("pane_id") or "") == pane]
        if not rows:
            return "the snapshot has no row for the pane"
        made_in = self._pane_dirs().get(pane)
        roots = [made_in] if made_in else self._roots()
        for row in rows:
            reported = [str(row.get(k) or "") for k in ("foreground_cwd", "cwd")]
            reported = [c for c in reported if c]
            if not reported:
                return "Herdr reports no directory for the pane"
            for cwd in reported:
                if not os.path.isabs(cwd):
                    return f"Herdr reports no absolute directory for it ({cwd!r})"
                if not roots or not self._is_under(cwd, roots):
                    return (f"its directory {cwd!r} is not under the directory "
                            f"HoH made it in ({', '.join(roots) or 'unknown'})")
        return ""

    def _refused(self, pane: str, role: Role | None, reason: str, why: str,
                 **extra) -> bool:
        """Record a refusal and remember its reason (`release_role` answers
        with it). Always False."""
        self.__dict__["_close_why"] = why
        self._record("refused", role=role.value if role else None, pane=pane,
                     reason=reason, why=why, **extra)
        return False

    def _close_own_pane(self, pane: str, *, role: Role | None, reason: str) -> bool:
        """The one place a pane is closed (one stated exception: `_replace_restored`).

        In order: a kept pane is never closed; a pane that is not an own pane
        is never closed; the snapshot must be readable, complete and non-empty;
        a pane the snapshot no longer lists **and** Herdr says is gone is
        `closed` (`already_gone`), no command sent; the live identity; a whole
        tab only when the snapshot proves every pane in it is HoH's own, passes
        the identity check and is not kept, and the tab row counts exactly
        those panes -- otherwise the pane alone; the `closing` line is written
        **before** the command or nothing is attempted; what Herdr answers is
        not the proof, a second snapshot and `pane_not_found` are; after a
        confirmed close the pane is forgotten. True only for a confirmed close.
        """
        self.__dict__["_close_why"] = ""
        label = role.value if role else None
        if pane in self._kept():
            why = self._kept()[pane]
            self.__dict__["_close_why"] = f"the pane is kept open: {why}"
            self._record("kept", role=label, pane=pane, reason=reason, why=why)
            return False
        if pane not in self._own_pane_ids():
            return self._refused(pane, role, reason,
                                 "the pane is not one HoH created or adopted")
        if not self._registry_owns(pane):
            return self._refused(pane, role, reason,
                                 "the pane is not in the owned-agent registry")
        snapshot, why = self._read_snapshot()
        if snapshot is None:
            return self._refused(pane, role, reason, why)
        if pane not in self._listed(snapshot):
            if self._herdr_says_gone(pane):
                self._record("closed", role=label, pane=pane, reason=reason,
                             already_gone=True)
                self._forget_pane(pane)
                return True
            return self._refused(
                pane, role, reason,
                "the snapshot does not list the pane, but Herdr does not say it "
                "is gone: a snapshot that lists only some panes does not make a "
                "live pane gone")
        problem = self._identity_problem(pane, snapshot)
        if problem:
            return self._refused(pane, role, reason, problem)

        # A whole tab only when the snapshot proves all of it is ours.
        tab = next((str(r.get("tab_id") or "") for r in self._rows(snapshot, "panes")
                    if str(r.get("pane_id") or "") == pane), "")
        targets = [pane]
        scope = "pane"
        close_target = pane
        if tab and tab in self.own_tabs:
            mine = [str(r.get("pane_id") or "") for r in self._rows(snapshot, "panes")
                    if str(r.get("tab_id") or "") == tab]
            tab_rows = [r for r in self._rows(snapshot, "tabs")
                        if str(r.get("tab_id") or "") == tab]
            whole = (len(tab_rows) == 1
                     and tab_rows[0].get("pane_count") == len(mine)
                     and pane in mine
                     and all(m in self._own_pane_ids()
                             and self._registry_owns(m)
                             and m not in self._kept()
                             and not self._identity_problem(m, snapshot)
                             for m in mine))
            if whole:
                targets = mine
                scope = "tab"
                close_target = tab

        # Written before the command; no line, no attempt.
        for target in targets:
            if not self._record("closing", role=label if target == pane else None,
                                pane=target, tab=tab or None, reason=reason,
                                scope=scope):
                return self._refused(
                    pane, role, reason,
                    "the close cannot be recorded in the ledger, so it is not "
                    "attempted")
        failure = ""
        try:
            if scope == "tab":
                self.control.close_tab(close_target, purpose=f"close tab {close_target}")
            else:
                self.control.close_pane(close_target, purpose=f"close pane {close_target}")
        except Exception as exc:          # noqa: BLE001 - the snapshot decides
            failure = str(exc)

        # What Herdr answers is not the proof: a second snapshot is.
        after, why = self._read_snapshot()
        if after is None:
            return self._refused(
                pane, role, reason,
                f"the close was asked for but cannot be confirmed: {why}"
                + (f" (the command said: {failure})" if failure else ""))
        still = [t for t in targets if t in self._listed(after)]
        answering = [t for t in targets if t not in still and not self._herdr_says_gone(t)]
        if still or answering:
            return self._refused(
                pane, role, reason,
                "the close is not confirmed: "
                + ("the snapshot still lists " + ", ".join(still) if still
                   else "Herdr still answers for " + ", ".join(answering))
                + (f" (the command said: {failure})" if failure else ""))
        for target in targets:
            self._record("closed", role=label if target == pane else None,
                         pane=target, tab=tab or None, reason=reason)
        for target in targets:
            self._forget_pane(target)
        self._settle_tab(tab, after)
        return True

    def _settle_tab(self, tab: str, after: dict) -> None:
        """Account for the tab of a pane that was just closed: a tab that is
        gone leaves `own_tabs`; one that survives with **none** of its
        remaining panes ours also leaves it, and a `kept` line says which
        panes hold it."""
        if not tab or tab not in self.own_tabs:
            return
        present = {str(t.get("tab_id") or "") for t in self._rows(after, "tabs")}
        if tab not in present:
            self.own_tabs.remove(tab)
            return
        remaining = [str(r.get("pane_id") or "") for r in self._rows(after, "panes")
                     if str(r.get("tab_id") or "") == tab]
        if any(p in self._own_pane_ids() for p in remaining):
            return
        self.own_tabs.remove(tab)
        if remaining:
            self._record("kept", tab=tab, why="the tab stays open: it still holds "
                         "panes that are not HoH's: " + ", ".join(remaining))

    def _forget_pane(self, pane: str) -> None:
        """Everything HoH remembers of a pane that is gone: its own-pane entry,
        the adopted map, the role-to-pane map, the keep and the marks, the
        role's cached agent (its next turn starts a fresh one) and the stall and
        settle marks of its agent."""
        if pane in self.own_panes:
            self.own_panes[:] = [p for p in self.own_panes if p != pane]
        registry = self.registry
        if registry is not None:
            try:
                registry.release(pane, reason="gone")
            except Exception:             # noqa: BLE001 - a stale record names a gone pane
                pass
        adopted = self._adopted()
        for name in [n for n, (_tab, p) in adopted.items() if p == pane]:
            adopted.pop(name, None)
        names = self._pane_names().pop(pane, set())
        for role in [r for r, p in self._role_panes().items() if p == pane]:
            self._role_panes().pop(role, None)
            self.agents.pop(role, None)
        for name in names:
            self._unsettled().pop(name, None)
            self._stalls().pop(name, None)
        self._kept().pop(pane, None)
        self._pane_marks().pop(pane, None)
        self._pane_dirs().pop(pane, None)

    # -- The role's processes ------------------------------------------------- #

    def _proc_table(self):
        """The process table (`procs.ProcTable`); a test stands in for it."""
        return procs.ProcTable()

    def _scan_processes(self, mark: str, listed: bool) -> procs.RoleScan:
        """What the role's pane holds, taken **before** the close, while its
        shell is alive. A pane that is already gone (`listed` is False) has no
        root and none is looked for: its live marked processes are named and
        the release is refused; with none left it is quiescent."""
        try:
            table = self._proc_table().snapshot()
        except Exception as exc:          # noqa: BLE001 - fail closed
            return procs.RoleScan(
                problem=f"the process table cannot be read before the close ({exc})")
        skip = procs.excluded(table, os.getpid())
        if not listed:
            left = procs.marked_left(table, mark, skip)
            if left:
                return procs.RoleScan(
                    escaped=tuple(left),
                    problem=("the pane is already gone, but processes still carry "
                             "its mark (pids "
                             + ", ".join(str(p.pid) for p in left)
                             + "); nothing anchors them to it, so none is signalled"))
            return procs.RoleScan()
        # With a root, `escaped` is not a problem of the scan: the role's own
        # command is still ended, and `_end_processes` refuses the release.
        return procs.scan(table, mark, skip)

    def _end_processes(self, mark: str, found: procs.RoleScan, *, role: Role | None,
                       pane: str, reason: str) -> str:
        """End what the role left running after its pane was closed, and say
        what is left: "" when nothing is, else the problem. Recorded."""
        label = role.value if role else None
        table = self._proc_table()
        try:
            skip = procs.excluded(table.snapshot(), os.getpid())
            outcome = procs.end(
                table, mark, found, skip,
                sleep=getattr(table, "sleep", time.sleep),
                clock=getattr(table, "clock", time.monotonic))
        except Exception as exc:          # noqa: BLE001 - fail closed
            problem = f"the process table cannot be read after the close ({exc})"
            self._record("refused", role=label, pane=pane, reason=reason, why=problem)
            return problem
        if outcome.gone:
            self._record("ended", role=label, pane=pane, reason=reason,
                         pids=list(outcome.gone), count=len(outcome.gone))
        problems: list[str] = []
        extra: dict = {}
        if outcome.survivors:
            pids = [p.pid for p in outcome.survivors]
            problems.append("processes the role left running are still alive after "
                            "SIGKILL (pids " + ", ".join(map(str, pids)) + ")")
            extra["pids"] = pids
        escaped = sorted({p.pid for p in (*found.escaped, *outcome.escaped)})
        if escaped:
            problems.append("marked processes outside the role's pane (pids "
                            + ", ".join(map(str, escaped))
                            + ") were named and never signalled")
            extra["escaped"] = escaped
        if not problems:
            return ""
        problem = "; ".join(problems)
        self._record("refused", role=label, pane=pane, reason=reason, why=problem,
                     **extra)
        return problem

    def _close_and_end_processes(self, role: Role | None, pane: str, reason: str, *,
                                 require_mark: bool = True) -> tuple[bool, str]:
        """`(pane closed, problem)`: close the pane, then end what its role left
        running. In order: take the root and the members while the agent and
        its shell are alive; close the pane (`_close_own_pane`); only if the
        close is confirmed, end the members and prove them gone. The problem is
        "" when nothing is left, else says what is (the pane **is** closed then).
        A pane with no mark (an adopted one) cannot be told from the
        operator's: with `require_mark` that is a problem, without it nothing
        is looked for."""
        mark = self._pane_marks().get(pane)
        found: procs.RoleScan | None = None
        problem = ""
        if mark is None:
            if require_mark:
                problem = ("the pane has no mark (HoH did not create it), so what "
                           "its role left running cannot be told")
        else:
            snapshot, _why = self._read_snapshot()
            listed = True if snapshot is None else pane in self._listed(snapshot)
            found = self._scan_processes(mark, listed)
            problem = found.problem
        if not self._close_own_pane(pane, role=role, reason=reason):
            return False, ""
        if mark is None or found is None or found.problem or found.root is None:
            if problem:
                self._record("refused", role=role.value if role else None, pane=pane,
                             reason=reason, why=problem,
                             **({"escaped": [p.pid for p in found.escaped]}
                                if found is not None and found.escaped else {}))
            return True, problem
        return True, self._end_processes(mark, found, role=role, pane=pane,
                                         reason=reason)

    # -- The turn end ----------------------------------------------------------- #

    def release_role(self, role: Role, state: RunState, *,
                     reason: str = "turn finished") -> PaneRelease:
        """End the role's session: its turn is over and the run will not use
        this pane again. **Never raises.**

        Refuses (records, returns `closed=False` with the reason) when no pane
        is recorded for the role; the pane is kept; the snapshot is unreadable,
        partial or empty; more than one agent carries the role's name; the
        agent sits in another pane than the one HoH created; the agent is
        `working` again; its status does not prove the turn ended (`blocked`
        is released: a dialog met **after** the answer was delivered is not
        what anything HoH asked is waiting for); the close is refused
        (`_close_own_pane`); or, after the pane is closed, what the role left
        running cannot be told or is not all gone (the pane **is** closed then).
        An agent that is no longer listed does not stop the release."""
        pane = ""
        try:
            pane = self._role_panes().get(role) or ""

            def refuse(why: str) -> PaneRelease:
                self._record("refused", role=role.value, pane=pane or None,
                             reason=reason, why=why)
                return PaneRelease(False, why)

            if not pane:
                return refuse(f"no pane is recorded for {role.value}")
            if pane in self._kept():
                why = self._kept()[pane]
                self._record("kept", role=role.value, pane=pane, reason=reason, why=why)
                return PaneRelease(False, f"the pane is kept open: {why}")
            snapshot, why = self._read_snapshot()
            if snapshot is None:
                return refuse(why)
            name = self.agents.get(role) or self._agent_name(role, state)
            rows = [r for r in self._rows(snapshot, "agents")
                    if str(r.get("name") or "") == name]
            if len(rows) > 1:
                return refuse(f"the snapshot shows {len(rows)} agents named {name}, "
                              "not one")
            if rows:
                row = rows[0]
                where = str(row.get("pane_id") or "")
                if where != pane:
                    return refuse(f"{name} sits in pane {where!r}, not in pane "
                                  f"{pane!r} where HoH put it")
                status = str(row.get("agent_status") or "unknown")
                if status == "working":
                    return refuse(f"{name} is working again after its turn ended")
                if status not in ("idle", "done", "blocked"):
                    return refuse(f"{name}'s status {status!r} does not prove that "
                                  "its turn ended")
            closed, problem = self._close_and_end_processes(role, pane, reason)
            if not closed:
                return PaneRelease(False, self.__dict__.get("_close_why")
                                   or "the pane was not closed")
            if problem:
                return PaneRelease(False, problem)
            return PaneRelease(True, "")
        except Exception as exc:          # noqa: BLE001 - release never raises
            why = f"the release failed: {type(exc).__name__}: {exc}"
            try:
                self._record("refused", role=role.value, pane=pane or None,
                             reason=reason, why=why)
            except Exception:             # noqa: BLE001
                pass
            return PaneRelease(False, why)

    # -- The run end -------------------------------------------------------------- #

    def close_own(self) -> list[str]:
        """Closes what this adapter opened, **only** what it can prove is its
        own, and **confirms** the termination.

        Handoff §7: *"`cancel` uses the existing controlled stop path for our
        own worker including its child processes; confirm the termination ...
        An unconfirmed stop stays visibly open."*

        Every own pane -- `own_panes` first, then adopted panes -- goes through
        `_close_own_pane` (reason `run end`); the processes of every pane it
        closes that has a mark are ended too (recorded, nothing raised). A pane
        alone in its tab closes with the tab; a tab that holds anything else is
        never closed. Returns the tabs among `own_tabs` that are **gone**;
        `own_tabs` is reduced to the tabs that still hold an own pane, and
        `unconfirmed` is those tabs plus every own pane still listed that is in
        none of them. Foreign tabs and panes stay untouched.
        """
        self._recall_owned()
        by_pane = {pane: role for role, pane in self._role_panes().items()}
        tabs_before = list(self.own_tabs)
        for pane in [*self.own_panes,
                     *(p for _tab, p in self._adopted().values() if p)]:
            if pane not in self._own_pane_ids():
                continue                  # an earlier close took it with its tab
            try:
                self._close_and_end_processes(by_pane.get(pane), pane, "run end",
                                              require_mark=False)
            except Exception:             # noqa: BLE001 - one pane must not stop the rest
                continue

        snapshot, _why = self._read_snapshot()
        if snapshot is None:
            # Not checkable does not mean confirmed.
            self.unconfirmed = [*self.own_tabs, *self.own_panes]
            return []
        present = {str(t.get("tab_id") or "") for t in self._rows(snapshot, "tabs")}
        mine = self._own_pane_ids()
        holding: dict[str, list[str]] = {}
        for row in self._rows(snapshot, "panes"):
            pane = str(row.get("pane_id") or "")
            if pane in mine:
                holding.setdefault(str(row.get("tab_id") or ""), []).append(pane)
        closed = [t for t in tabs_before if t not in present]
        still_open = [t for t in self.own_tabs if t in holding]
        covered = {p for t in still_open for p in holding[t]}
        loose = [p for p in self._listed(snapshot) if p in mine and p not in covered]
        self.own_tabs = still_open
        self.unconfirmed = [*still_open, *loose]
        return closed

    def _tab_gone(self, tab: str) -> bool:
        """Looks, instead of assuming."""
        try:
            snapshot = self.control.snapshot()
        except Exception:
            return False   # not checkable does not mean confirmed
        present = {str(x.get("tab_id")) for x in (snapshot.get("tabs", []) or [])}
        return tab not in present

    def role_gone(self, role: Role, state: RunState) -> bool:
        """Is this run's agent for `role` provably absent from Herdr?

        Looked up by its deterministic name, so an agent Herdr restored from
        an earlier `hoh run`, which this process never reattached, counts as
        present too. QA's sealed arena is lifted only once this is true.
        Unreadable counts as present: a snapshot that cannot be read, an
        `agents` that is not a list, or any row without a non-empty string
        `name` -- a row that cannot be matched could be the agent.
        """
        try:
            snapshot = self.control.snapshot()
        except Exception:
            return False   # not checkable does not mean gone
        rows = snapshot.get("agents") if isinstance(snapshot, dict) else None
        if not isinstance(rows, list):
            return False
        if not all(isinstance(row, dict) and isinstance(row.get("name"), str)
                   and row["name"] for row in rows):
            return False
        name = self._agent_name(role, state)
        return not any(row["name"] == name for row in rows)

    def _new_tab_pane(self, label: str, *, cwd: str | None = None) -> str:
        """Every role gets its **own tab**, no further split.

        The first version split our own pane to the right over and over. After
        three roles every pane was a quarter wide, and Kimi's TUI stood at four
        characters of width -- the prompt arrived but was never processed
        ("agent_prompt_stalled"). The Herdr docs warn explicitly against
        repeated splits in the same direction.

        A tab of its own is also more polite: it does not push into the window
        the captain is currently working in.
        """
        # O215, and the only place it can go. Herdr is client/server: every pane
        # is a direct child of the long-lived `herdr server`, so the environment
        # of this short-lived CLI client never reaches the role session. Measured
        # rather than assumed -- the server has run since before this change, 27
        # panes are its children, and a pane's environ differs from the server's
        # by six HERDR_* variables and nothing else. `--env` on tab create is the
        # documented way to reach the launched process.
        env = dict(ROLE_PANE_ENV)
        # The role's own scratch (`role_scratch_env`): its pytest must not reach the
        # shared `/tmp/pytest-of-<user>`. Handed over like the mark below.
        env.update(self.__dict__.get("_pane_scratch_env") or {})
        # The mark of this pane (`_ensure_agent` mints it): every process the
        # role starts inherits it, which is how what a closed pane left running
        # is found (`procs`). Handed over through an attribute so the
        # signature that tests stub does not change.
        mark = self.__dict__.get("_pane_mark")
        if mark:
            env[procs.MARK_ENV] = mark
        surface = self.control.open_surface(
            cwd=cwd or self.cwd, label=label, env=env,
            workspace=os.environ.get("HERDR_WORKSPACE_ID") or None,
            purpose="create tab")
        pane = surface.pane_id
        tab = surface.tab_id
        if tab:
            self.own_tabs.append(str(tab))
        time.sleep(2.0)   # the shell of the new tab needs a moment
        return str(pane)

    def _start_agent(self, name: str, kind: str, pane: str,
                     extra: list[str] | None = None) -> None:
        # A freshly split pane needs a moment before the shell is at the
        # prompt. Otherwise Herdr rejects an agent start with
        # "not an available shell".
        info = None
        last_err = ""
        for attempt in range(10):
            try:
                info = self.control.start(name, kind=kind, pane_id=pane,
                                          args=extra or (), timeout_ms=240000,
                                          purpose=f"start agent {name}")
                break
            except DispatchError as exc:
                last_err = str(exc)
                if "not an available shell" not in last_err and "busy" not in last_err:
                    raise
                time.sleep(1.0 + attempt * 0.5)
        if info is None:
            raise DispatchError(
                f"agent {name} could not be started after 10 attempts: {last_err}"
            )
        if not info.pane_id:
            raise DispatchError(f"agent {name} reports no pane -- start not evidenced")

    #: Margin on top of the deadline handed to Herdr. The subprocess has to
    #: wait **longer** than the command it is waiting for -- otherwise the
    #: wrapper aborts the very wait that it just requested.
    WRAPPER_OVERHEAD_S = 60.0

    @classmethod
    def _wrapper_timeout(cls, args: list[str], *, minimum: float = 180.0) -> float:
        """Derives the subprocess timeout from the `--timeout` in the command.

        A fixed value was a genuine operational defect here: `_cli` aborted
        after 180 s while the same call handed `--timeout 900000` to Herdr. The
        developer step legitimately takes longer than three minutes, and the
        run stalled on a deadline that nobody had set. The wrapper's deadline
        is therefore **derived**, not guessed.
        """
        try:
            ms = float(args[args.index("--timeout") + 1])
        except (ValueError, IndexError):
            return minimum
        return max(minimum, ms / 1000.0 + cls.WRAPPER_OVERHEAD_S)

    def _cli(self, args: list[str], purpose: str, *, text: bool = False):
        """One Herdr call: its JSON as a dict, or with `text=True` its stdout
        as a string (`agent read` writes the screen, not JSON)."""
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self._wrapper_timeout(args), env=role_environment(),
            )
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                # Deterministic: the same argv fails the same way on every
                # retry. Run 20261002t1501z-sstew4 retried it twice.
                raise DispatchError(
                    f"{purpose} failed: the command line exceeds the kernel's "
                    "argument-length limit (one argument may hold at most "
                    f"131071 bytes, MAX_ARG_STRLEN): {exc}",
                    transient=False) from exc
            raise DispatchError(f"{purpose} failed: {exc}", transient=True) from exc
        except subprocess.TimeoutExpired as exc:
            raise DispatchError(f"{purpose} failed: {exc}", transient=True) from exc
        if proc.returncode != 0:
            # Herdr's error code, from the JSON it writes to stderr
            # (`{"error": {"code": ..., "message": ...}, "id": ...}`) -- never
            # matched in the prose of the message. Stderr that is not that
            # JSON keeps the classification below.
            try:
                error = json.loads((proc.stderr or "").strip()).get("error")
                code = error.get("code") if isinstance(error, dict) else None
            except (ValueError, AttributeError):
                code = None
            if code == "agent_prompt_stalled":
                # Run 20260925t1801z-pqagate2: the prompt produced no observed
                # state change within 5 s on an idle pane. Not a decision
                # about the role: the only code that is transient, and it is
                # asked again after a back-off, not at once.
                raise DispatchError(f"{purpose} failed: {proc.stderr.strip()[:300]}",
                                    transient=True, retry_after=STALL_RETRY_AFTER_S)
            raise DispatchError(f"{purpose} failed: {proc.stderr.strip()[:300]}")
        if text:
            return proc.stdout or ""
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise DispatchError(f"{purpose}: Herdr returned no JSON ({exc})") from exc

    @staticmethod
    def _is_under(path: str, roots: list[str]) -> bool:
        """A real path boundary instead of a string prefix.

        `cwd.startswith("/home/someone/hoh")` also matched
        `/home/someone/hoh-other` -- a sibling with a separator -- **and**
        `/home/someone/hohmirror`, where the prefix runs straight into the next
        word and no separator exists to notice. Those are two different shapes
        of the same defect, and the second is the one a reader is likely to
        dismiss. A reviewer evidenced four such cases as adopted, and with
        `status=unknown` a foreign tab was closed on top of that. `trust.register` does it right
        with `is_relative_to`, the dispatcher did not. This gets realistic with
        worktree siblings: `...-iter1` is a prefix of `...-iter10`.
        """
        try:
            target = Path(path).resolve()
        except (OSError, ValueError):
            return False
        for root in roots:
            try:
                if (target == Path(root).resolve()
                        or target.is_relative_to(Path(root).resolve())):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _pane_view(self, target: str, *, lines: int = 40) -> str:
        """The visible pane content as diagnostic text.

        Terminal text is **not** dependable run state -- the snapshot is
        responsible for that (handoff §3). As evidence for the cause of a
        failed dispatch it is, on the other hand, often the only thing there
        is. Never raises: a diagnosis must not replace the error it is meant
        to explain with a second one.
        """
        try:
            view = self.control.transcript(target, lines=lines).strip()
        except Exception as exc:
            view = f"(pane not readable: {exc})"
        tail = "\n".join(ln for ln in view.splitlines() if ln.strip())[-1200:]
        return f"--- Visible in pane ---\n{tail or '(empty)'}\n-----------------------"

    # -- Is the pane free to take a prompt? ---------------------------------- #

    def _unsettled(self) -> dict:
        """target -> (digest, monotonic send time, developer tree digest taken
        before that send) for a target whose last prompt did not return, or
        `(RESTORED, 0.0, None)` for a pane Herdr restored. Lazy, so a
        dispatcher built with `__new__` has it too."""
        marks = self.__dict__.get("_unsettled_marks")
        if marks is None:
            marks = self.__dict__["_unsettled_marks"] = {}
        return marks

    def _stalls(self) -> dict:
        """target -> (sha256 of the full prompt, sha256 of the text sent) of a
        send that ended in `agent_prompt_stalled` in this process. The only
        licence to press Enter (`_submit_stalled`), used at most once. Lazy,
        like `_unsettled`."""
        stalls = self.__dict__.get("_stalled_sends")
        if stalls is None:
            stalls = self.__dict__["_stalled_sends"] = {}
        return stalls

    def _adopted(self) -> dict:
        """agent name -> (tab id, pane id) of a restored agent this process
        adopted (`_reusable_agent`): the only pane `_replace_restored` may
        close. Lazy, like `_unsettled`."""
        adopted = self.__dict__.get("_adopted_panes")
        if adopted is None:
            adopted = self.__dict__["_adopted_panes"] = {}
        return adopted

    def _fresh(self) -> set:
        """Agent names HoH started and has not yet sent a prompt to
        (`_ensure_agent`, `_await_ready`). Lazy, like `_unsettled`."""
        return self._lazy("_fresh_agents", set)

    def _own_roots(self, state: RunState) -> list[str]:
        """The directories that belong to this run (see `_reusable_agent`)."""
        return [str(p) for p in (
            getattr(self, "store_dir", None),
            getattr(self, "arenas_dir", None),
            state.repo_path,
            *self.role_cwd.values(),
        ) if p]

    def _settle(self, role: Role, target: str, full_sha: str,
                state: RunState, sent: str | None = None):
        """Is a pane whose last prompt did not return free for the next one?

        Run padopt: a prompt stayed unsent in the input box, the stall retry
        was sent without looking and appended to it, and the joined text began
        with a slash command the agent did not know. Called before O199 and
        before any write. An unmarked target, or a restored one (that is
        `_check_restored`'s), returns None with no Herdr call.

        Returns None when the pane is free -- idle or done, with an empty
        input box. Returns `(read, send time, tree digest)` of the stalled
        send when the agent turned out to be working on exactly this prompt
        and its turn has now ended (T-DR1): nothing is sent again. Anything
        else raises, and nothing is sent.
        """
        mark = self._unsettled().get(target)
        if mark is None or mark[0] == RESTORED:
            return None
        digest, sent_at, _earlier = mark
        try:
            status = self._agent_state(role, target)
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise PaneNotClean(
                f"{role.value} dispatch not sent: the earlier prompt to {target} "
                f"did not return, and the agent's status cannot be read ({exc}). "
                "A pane whose state is unknown is never prompted.\n"
                f"{self._pane_view(target)}") from exc
        if status == "blocked":
            raise WaitingForApproval(
                f"{role.value} waits for an approval in pane {target}; the "
                "earlier prompt did not return, and nothing is sent into a "
                "dialog. A blocked dialog is not answered automatically.\n"
                f"{self._pane_view(target)}")
        if status == "working":
            if digest == full_sha:
                state.note(
                    f"{role.value}: the earlier prompt to {target} did not "
                    "return, and the agent is working on exactly that prompt; "
                    "its turn is awaited, nothing is sent again")
                return self._await_turn(role, target,
                                        sent_at + self.timeout_ms / 1000)
            raise PaneNotClean(
                f"{role.value} dispatch not sent: the agent in {target} is "
                "working, but not on this prompt (its last prompt did not "
                "return and differs from this one). Nothing is sent into a "
                f"live turn.\n{self._pane_view(target)}")
        if status not in ("idle", "done"):
            raise PaneNotClean(
                f"{role.value} dispatch not sent: the earlier prompt to {target} "
                f"did not return, and the agent's status {status!r} does not "
                f"prove its input is free.\n{self._pane_view(target)}")
        view = ""
        try:
            view = self.control.screen(target, purpose=f"{role.value} input read")
            pending = _pending_input(view)
            unread = None
        except Exception as exc:          # noqa: BLE001 - fail closed
            pending, unread = None, exc
        if pending is None:
            why = (f"its input could not be read ({unread})" if unread is not None
                   else "no input box was found on its screen")
            raise PaneNotClean(
                f"{role.value} dispatch not sent: the earlier prompt to {target} "
                f"did not return, and {why}, so the box is not proven empty.\n"
                f"{self._pane_view(target)}")
        if pending:
            refusal = PaneNotClean(
                f"{role.value} dispatch not sent: the earlier prompt to {target} "
                f"did not return, and its input box still holds {len(pending)} "
                f"characters of unsent text, beginning {pending[:120]!r}. "
                "Nothing is sent on top of it, and Enter alone is not pressed: "
                "the screen cannot prove the text is the prompt.\n"
                f"whole input:\n{pending}\n{self._pane_view(target)}")
            # The one exception: this process's own send of exactly this
            # prompt stalled, and the box holds exactly that text.
            if (sent is not None and self._stalls().get(target) == (
                    full_sha, hashlib.sha256(sent.encode("utf-8")).hexdigest())
                    and digest == full_sha
                    and _same_input(pending, sent, _box_width(view))):
                return _Resubmit(refusal)
            raise refusal
        state.note(f"{role.value}: the earlier prompt to {target} did not "
                   "return; the pane was checked before the next one: idle, "
                   "input box empty")
        self._unsettled().pop(target, None)
        return None

    def _await_turn(self, role: Role, target: str,
                    deadline: float) -> tuple[AgentInfo, float, str | None]:
        """Wait, passively, for the turn of an agent that started a stalled
        prompt late. One status read per `max(STATUS_POLL_SECONDS, 10)` s,
        `STATUS_READ_ATTEMPTS` failed reads in a row end it, and so does the
        role timeout counted from the stalled send. Returns the last read with
        the stalled send's time and tree digest, once the turn ended."""
        failures = 0
        while True:
            if time.monotonic() >= deadline:
                raise _refuse(
                    f"{role.value} was still working at the role timeout "
                    f"({self.timeout_ms} ms after its stalled prompt was sent). "
                    f"Nothing was sent again.\n{self._pane_view(target)}")
            time.sleep(max(STATUS_POLL_SECONDS, 10.0))
            try:
                data = self.control.status(target, purpose=f"{role.value} status read")
                status = data.status
                failures = 0
            except Exception as exc:      # noqa: BLE001 - counted, then fail closed
                failures += 1
                if failures >= STATUS_READ_ATTEMPTS:
                    raise PaneNotClean(
                        f"{role.value}: the status of {target} could not be "
                        f"read {failures} times in a row while its turn was "
                        f"awaited ({exc}). Nothing was sent again.\n"
                        f"{self._pane_view(target)}") from exc
                continue
            if status == "working":
                continue
            if status not in ("idle", "done", "blocked"):
                raise PaneNotClean(
                    f"{role.value}: while its turn was awaited, {target} "
                    f"reported {status!r}, which does not prove the turn "
                    f"ended. Nothing was sent again.\n{self._pane_view(target)}")
            _digest, sent_at, earlier = self._unsettled().pop(target)
            return data, sent_at, earlier

    def _submit_stalled(self, role: Role, state: RunState, target: str,
                        full_sha: str, sent: str, refusal: BaseException,
                        tampered, changed, marker_ok
                        ) -> tuple[dict, float, str | None]:
        """Press Enter once on this dispatch's own stalled prompt.

        Runs qaw3 and qaw4: `agent prompt --wait` returned
        `agent_prompt_stalled`, and the prompt stayed typed but unsent in the
        box. `_settle` found the box holding exactly the text this process
        sent in that stalled send. Before Enter: the stall record is spent
        (never a second Enter), the prompt file and its marker still hold the
        prompt, and the agent is idle or done in one of this run's panes. After
        Enter the turn must be seen to start within SUBMIT_CONFIRM_SECONDS;
        it is then awaited like the stalled send's own turn (`sent_at` and
        the tree digest are the stalled send's). Anything else raises, and
        nothing more is pressed or sent.
        """
        stall = self._stalls().pop(target, None)
        if stall != (full_sha, hashlib.sha256(sent.encode("utf-8")).hexdigest()):
            raise refusal
        now = tampered()
        if now is not None:
            raise changed(now, "before Enter was pressed on the stalled prompt")
        if not marker_ok():
            raise refusal
        try:
            data = self.control.status(target, purpose=f"{role.value} status read")
            status = data.status
            pane = data.pane_id
        except Exception:                 # noqa: BLE001 - fail closed
            raise refusal from None
        if status == "blocked":
            raise WaitingForApproval(
                f"{role.value} waits for an approval in pane {pane or target}; its "
                "stalled prompt is still typed in the box, and Enter is not "
                "pressed: a blocked dialog is not answered automatically.\n"
                f"{self._pane_view(target)}")
        own = set(getattr(self, "own_panes", []) or []) | {
            p for _t, p in self._adopted().values() if p}
        if status not in ("idle", "done") or not pane or pane not in own:
            raise refusal
        # The box is read again right before Enter, from the very pane Enter
        # goes to: what Enter submits is what was just compared.
        try:
            view = self.control.pane_screen(pane, source="visible",
                                            purpose=f"{role.value} input read")
        except Exception:                 # noqa: BLE001 - fail closed
            raise refusal from None
        pending = _pending_input(view)
        if pending is None or not _same_input(pending, sent, _box_width(view)):
            raise refusal
        state.note(
            f"{role.value}: the prompt to {target} stalled and stayed in its "
            "input box; the box holds exactly the text this dispatch sent "
            f"(sha256 {stall[1]}), pane {pane}: Enter is pressed once, "
            "nothing is sent again")
        try:
            self.control.press(pane, "Enter",
                               purpose=f"{role.value} Enter on the stalled prompt")
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise PaneNotClean(
                f"{role.value}: Enter on the stalled prompt in {target} failed "
                f"({exc}); whether it reached the pane is unknown. Nothing more "
                f"is pressed or sent.\n{self._pane_view(target)}") from exc
        time.sleep(SUBMIT_CONFIRM_SECONDS)
        try:
            data = self.control.status(target, purpose=f"{role.value} status read")
            status = data.status
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise PaneNotClean(
                f"{role.value}: after Enter on the stalled prompt, the status of "
                f"{target} cannot be read ({exc}). Nothing more is pressed or "
                f"sent.\n{self._pane_view(target)}") from exc
        if status == "working":
            _d, sent_at, _e = self._unsettled()[target]
            return self._await_turn(role, target, sent_at + self.timeout_ms / 1000)
        if status in ("idle", "done"):
            try:
                pending = _pending_input(self.control.screen(
                    target, purpose=f"{role.value} input read"))
            except Exception:             # noqa: BLE001 - fail closed
                pending = None
            if pending != "":
                raise PaneNotClean(
                    f"{role.value}: Enter was pressed once on the stalled prompt "
                    f"in {target}, and the agent still shows no turn "
                    f"({status!r}, input box {'not readable' if pending is None else 'still holding text'}). "
                    f"Nothing more is pressed or sent.\n{self._pane_view(target)}")
        elif status != "blocked":
            raise PaneNotClean(
                f"{role.value}: after Enter on the stalled prompt, {target} "
                f"reported {status!r}, which does not prove a turn. Nothing more "
                f"is pressed or sent.\n{self._pane_view(target)}")
        _digest, sent_at, earlier = self._unsettled().pop(target)
        return data, sent_at, earlier

    def _await_ready(self, role: Role, state: RunState, target: str) -> None:
        """Before the first prompt of an agent HoH started: wait for an idle
        input box that holds still, or fail closed.

        A target that is not in `_fresh` returns at once with no Herdr call.
        Otherwise, every `READY_POLL_SECONDS`, at most `READY_MAX_POLLS` times:
        the agent's status is read (`idle` or `done`; `blocked` is a dialog,
        `WaitingForApproval`), and for a Claude agent its box is read off the
        pane: found (`_pending_input` is not None), empty or showing the
        placeholder of a fresh session, with a width. The agent is ready when
        `READY_QUIET_POLLS` reads in a row agree with the one before. After
        `READY_MAX_POLLS` reads, or `READY_CEILING_SECONDS` on the real clock
        (checked between two polls), it raises `PaneNotClean`, and nothing was
        sent, written or pressed: no Enter, ever. Another agent kind has no box to
        read here; its status alone is held.
        """
        if target not in self._fresh():
            return
        boxless = self.profiles.get(role) != "claude"
        clock = self.__dict__.get("ready_clock") or _real_monotonic
        ceiling = self.__dict__.get("ready_ceiling_s", READY_CEILING_SECONDS)
        began = clock()
        last, held, polls = None, 0, 0
        while True:
            polls += 1
            signature, why = None, ""
            try:
                status = self._agent_state(role, target)
                if status == "blocked":
                    raise WaitingForApproval(
                        f"{role.value}: the new agent {target} waits for an "
                        "approval before its first prompt; nothing is sent into "
                        "a dialog. A blocked dialog is not answered "
                        f"automatically.\n{self._pane_view(target)}")
                if status not in ("idle", "done"):
                    why = f"its status is {status!r}"
                elif boxless:
                    signature = (status,)
                else:
                    view = self.control.screen(target, purpose=f"{role.value} input read")
                    pending = _pending_input(view)
                    if pending is None:
                        why = "no input box is drawn on its screen"
                    elif pending and not _PLACEHOLDER.fullmatch(pending):
                        why = f"its input box already holds text: {pending[:80]!r}"
                    else:
                        signature = (status, pending, _box_width(view))
            except WaitingForApproval:
                raise
            except Exception as exc:      # noqa: BLE001 - counted, then fail closed
                why = f"a read failed ({exc})"
            held = held + 1 if signature is not None and signature == last else 0
            last = signature
            if signature is not None and held >= READY_QUIET_POLLS:
                self._fresh().discard(target)
                state.note(f"{role.value}: the new agent {target} showed an idle "
                           f"input box that held still for {held} reads before "
                           "its first prompt")
                return
            waited = clock() - began
            if polls >= READY_MAX_POLLS or waited >= ceiling:
                raise PaneNotClean(
                    f"{role.value} dispatch not sent: the new agent {target} did "
                    "not show an idle input box that held still within "
                    f"{polls} reads and {waited:.0f} s "
                    f"({why or 'the reads kept changing'}). "
                    f"Nothing was sent, written or pressed.\n{self._pane_view(target)}")
            time.sleep(READY_POLL_SECONDS)

    def _check_restored(self, role: Role, state: RunState, target: str) -> str:
        """The target to prompt: a restored pane proven idle with an empty
        input box, or a fresh agent in its place. Runs after the O199
        collection and before anything is written. A target that is not a
        restored pane is returned with no Herdr call -- a freshly started
        agent's box may not be drawn yet."""
        mark = self._unsettled().get(target)
        if mark is None or mark[0] != RESTORED:
            return target
        try:
            status = self._agent_state(role, target)
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise PaneNotClean(
                f"{role.value}: the restored agent {target} is not prompted, its "
                f"status cannot be read ({exc}).\n{self._pane_view(target)}") from exc
        if status == "blocked":
            raise WaitingForApproval(
                f"{role.value}: the restored agent {target} waits for an "
                "approval; nothing is sent into a dialog. A blocked dialog is "
                f"not answered automatically.\n{self._pane_view(target)}")
        if status not in ("idle", "done"):
            raise PaneNotClean(
                f"{role.value}: the restored agent {target} is not prompted, its "
                f"status {status!r} does not prove its input is free.\n"
                f"{self._pane_view(target)}")
        try:
            view = self.control.screen(target, purpose=f"{role.value} input read")
            pending = _pending_input(view)
            reason = ("no input box was found on its screen (another agent "
                      "kind, a dialog, or a view cut off)") if pending is None else ""
        except Exception as exc:          # noqa: BLE001 - recorded, then replaced
            pending, reason = None, f"its input could not be read ({exc})"
        if pending == "":
            self._unsettled().pop(target, None)
            state.note(f"{role.value}: restored pane {target} checked before its "
                       "first prompt: input box empty")
            return target
        if pending:
            reason = (f"its input box holds {len(pending)} characters of text "
                      f"typed by an earlier process:\n{pending}\n")
        return self._replace_restored(role, state, target, reason)

    def _replace_restored(self, role: Role, state: RunState, target: str,
                          reason: str) -> str:
        """Close the restored pane HoH adopted, and start a fresh agent.

        Only the pane this process adopted is ever closed, and only while the
        snapshot still shows that agent in exactly that tab and pane, the tab
        is one of `own_tabs`, and its directory is absolute and lies under the
        run's roots. Any doubt refuses with `PaneNotClean`, and nothing is
        closed or sent. The tab is never closed: a pane the operator opened
        next to the agent survives.
        """
        state.note(f"restored pane {target} not reused: {reason}; nothing in it "
                   f"is sent, it is replaced by a fresh agent\n{self._pane_view(target)}")
        ledger_reason = "restored pane replaced"
        seen: dict[str, str] = {"pane": ""}

        def refuse(why: str) -> PaneNotClean:
            self._record("refused", role=role.value, pane=seen["pane"] or None,
                         agent=target, reason=ledger_reason, why=why)
            return PaneNotClean(
                f"{role.value}: the restored agent {target} is not prompted "
                f"({reason}), and it is not replaced: {why}. Nothing was closed "
                "and nothing was sent; check the pane and close it "
                "deliberately.")

        adopted = self._adopted().get(target)
        if adopted is None:
            raise refuse("this process did not record adopting it")
        tab, pane = adopted
        seen["pane"] = pane
        if not tab or not pane:
            raise refuse(f"the adopted tab or pane id is empty ({tab!r}, {pane!r})")
        if pane in self._kept():
            kept_why = self._kept()[pane]
            self._record("kept", role=role.value, pane=pane, agent=target,
                         reason=ledger_reason, why=kept_why)
            raise PaneNotClean(
                f"{role.value}: the restored agent {target} is not prompted "
                f"({reason}), and it is not replaced: the pane is kept open "
                f"({kept_why}). Nothing was closed and nothing was sent; check "
                "the pane and close it deliberately.")
        if tab not in self.own_tabs:
            raise refuse(f"its tab {tab} is not one of this run's tabs")
        if not self._registry_owns(pane):
            raise refuse(f"its pane {pane} is not in the owned-agent registry")
        try:
            snapshot = self.control.snapshot()
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise refuse(f"the Herdr snapshot cannot be read ({exc})") from exc
        rows = [r for r in (snapshot.get("agents") or [])
                if str(r.get("name") or "") == target]
        if len(rows) != 1:
            raise refuse(f"the snapshot shows {len(rows)} agents of that name, "
                         "not exactly one")
        row = rows[0]
        now = (str(row.get("tab_id") or ""), str(row.get("pane_id") or ""))
        if now != (tab, pane):
            raise refuse(f"it sits in tab {now[0]!r}, pane {now[1]!r}, not in "
                         f"the adopted tab {tab!r}, pane {pane!r}")
        roots = self._own_roots(state)
        reported = [str(row.get(k) or "") for k in ("foreground_cwd", "cwd")]
        reported = [c for c in reported if c] or [""]
        for cwd in reported:
            if not cwd or not os.path.isabs(cwd):
                raise refuse(f"Herdr reports no absolute directory for it ({cwd!r})")
            if not roots or not self._is_under(cwd, roots):
                raise refuse(f"its directory {cwd!r} is not under this run's "
                             f"directories ({', '.join(roots)})")
        # Written before the command; no line, no attempt.
        if not self._record("closing", role=role.value, pane=pane, tab=tab,
                            agent=target, reason=ledger_reason, scope="pane"):
            raise refuse("the close cannot be recorded in the ledger, so it is "
                         "not attempted")

        def failed(why: str, message: str) -> PaneNotClean:
            self._record("refused", role=role.value, pane=pane, agent=target,
                         reason=ledger_reason, why=why)
            return PaneNotClean(message)

        try:
            self.control.close_pane(pane, purpose=f"close the restored pane of {target}")
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise failed(
                f"closing the restored pane failed ({exc})",
                f"{role.value}: closing the restored pane {pane} of {target} "
                f"failed ({exc}); nothing was sent.") from exc
        try:
            after = self.control.snapshot()
        except Exception as exc:          # noqa: BLE001 - fail closed
            raise failed(
                f"the snapshot cannot confirm the close ({exc})",
                f"{role.value}: the restored pane {pane} of {target} was closed, "
                f"but the Herdr snapshot cannot confirm it ({exc}); nothing was "
                "sent.") from exc
        listed = {str(x.get("pane_id") or "") for x in
                  [*(after.get("panes") or []), *(after.get("agents") or [])]}
        named = [r for r in (after.get("agents") or [])
                 if str(r.get("name") or "") == target]
        if pane in listed or named:
            still = "the pane" if pane in listed else "an agent of that name"
            raise failed(
                f"the snapshot still lists {still}",
                f"{role.value}: the restored pane {pane} of {target} was closed, "
                f"but the snapshot still lists {still}; nothing was sent.")
        self._record("closed", role=role.value, pane=pane, tab=tab, agent=target,
                     reason=ledger_reason)
        if tab in self.own_tabs:
            self.own_tabs.remove(tab)
        if pane in self.own_panes:
            self.own_panes.remove(pane)
        if tab in {str(t.get("tab_id") or "") for t in (after.get("tabs") or [])}:
            state.note(f"tab {tab} still holds other panes after the restored "
                       "agent's pane was closed; it is left alone")
        self._adopted().pop(target, None)
        self._unsettled().pop(target, None)
        self.agents.pop(role, None)
        self._forget_pane(pane)
        fresh = self._ensure_agent(role, state)
        if target in self._adopted() or self._unsettled().get(fresh, ("",))[0] == RESTORED:
            raise PaneNotClean(
                f"{role.value}: the restored pane {pane} of {target} was closed, "
                "but the old agent was adopted again instead of a fresh one "
                "being started; nothing was sent.")
        state.note(f"{role.value}: restored pane {pane} closed, a fresh agent "
                   f"{fresh} started in its place")
        return fresh

    # -- Dispatch ------------------------------------------------------------ #

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        # The answers directory and the run directory, opened link-free once
        # per dispatch. Every write and re-read of the prompt file, the prompt
        # marker, the dispatch marker and the audit file goes through these
        # descriptors (Revision 2, item 0). The chain above them is assumed
        # not to be swapped during the run (T-LP5).
        answers_fd = _open_dir(self.answers_dir, "answers directory")
        try:
            run_fd = _open_dir(self.answers_dir.parent, "run directory")
        except DispatchError:
            os.close(answers_fd)
            raise
        try:
            answer = self._dispatch(role, prompt, state, answers_fd, run_fd)
        except WaitingForApproval:
            self.keep_open(role, "an approval is waiting in it")
            raise
        except PaneNotClean:
            self.keep_open(role, "its state is not proven; kept for inspection")
            raise
        finally:
            os.close(run_fd)
            os.close(answers_fd)
        # A dispatch that came back with its answer ends the keep: the dialog
        # that held the pane open was answered.
        self._lift_keep(role)
        return answer

    def _dispatch(self, role: Role, prompt: str, state: RunState,
                  answers_fd: int, run_fd: int) -> str:
        where = str(self.answers_dir)
        target = self._ensure_agent(role, state)
        answer_path = self._answer_path(role, state)
        full = prompt + (
            _answer_instruction(answer_path) if role in STRUCTURED_ROLES else ""
        )
        # The binding of record: sha256(full), held here in memory. Every
        # comparison below is against it -- never against a record on disk,
        # which a role can write as easily as the prompt file.
        full_bytes = full.encode("utf-8")
        full_sha = hashlib.sha256(full_bytes).hexdigest()
        # The threshold counts UTF-8 bytes, the unit the kernel limits; a
        # character a paste may drop routes a short prompt through the file too.
        through_file = (len(full_bytes) > PROMPT_FILE_THRESHOLD
                        or _paste_unsafe(full))
        stem = answer_path.name[:-len(".json")]
        prompt_name = f"{stem}.prompt.md"
        prompt_marker_name = f"{stem}.prompt.json"

        # What this dispatch types into the box. A prompt longer than
        # PROMPT_FILE_THRESHOLD, or one holding a character a paste may drop
        # (`_paste_unsafe`), goes through a file; both
        # markers, the audit record and the run history bind `full`, never
        # the pointer. Absolute for the role; the leaf itself is never
        # resolved, so a link planted there cannot redirect the pointer.
        sent = full
        prompt_file = self.answers_dir.resolve() / prompt_name
        if through_file:
            sent = _pointer_prompt(prompt_file, len(full_bytes), full_sha) + (
                _answer_instruction(answer_path) if role in STRUCTURED_ROLES else "")

        def tampered() -> str | None:
            """The digest on disk if the prompt file no longer holds `full`."""
            if not through_file:
                return None
            now = _rehash(answers_fd, prompt_name, len(full_bytes))
            return None if now == full_sha else now

        def changed(now: str, when: str) -> DispatchError:
            return _refuse(
                f"{role.value} dispatch: the prompt file {prompt_name} changed "
                f"during the dispatch (sha256 {full_sha} when sent, {now} "
                f"{when}). What the role was asked is no longer what is on "
                "disk; the run does not go on from here.")

        def marker_ok() -> bool:
            """The prompt marker still names exactly the prompt typed."""
            if not through_file:
                return True
            meta = _read_json_leaf(answers_fd, prompt_marker_name)
            return bool(meta) and (meta.get("prompt_sha256") == full_sha
                                   and meta.get("bytes") == len(full_bytes)
                                   and meta.get("prompt_path") == str(prompt_file))

        # A target whose last prompt did not return is read before anything
        # else happens: sent into, it would append to an unsent prompt
        # (padopt). `settled` is the stalled send's own turn, ended while it
        # was awaited: then nothing is written and nothing is sent.
        settled = self._settle(role, target, full_sha, state, sent)
        if isinstance(settled, _Resubmit):
            settled = self._submit_stalled(role, state, target, full_sha, sent,
                                           settled.refusal, tampered, changed, marker_ok)
        sending = settled is None

        if (sending and answer_path.exists()
                and self._resumable_answer(role, answer_path, full, answers_fd)):
            if through_file:
                # The answer was given to whatever the prompt file said. It is
                # collected only if that is still exactly `full`.
                now = _rehash(answers_fd, prompt_name, len(full_bytes))
                if now != full_sha:
                    raise _refuse(
                        f"{role.value} dispatch: the answer {answer_path.name} "
                        f"would be collected, but the prompt file {prompt_name} "
                        f"no longer holds what is asked (sha256 {full_sha} "
                        f"expected, {now} on disk). It is not collected.")
            # O199: the answer to this exact prompt, finished after an
            # operator answered the dialog that interrupted it. Collected,
            # not asked again -- and recorded as collected.
            self.endpoints[role] = (
                f"collected after an approved dialog: {answer_path.name} "
                f"written by {target} in answer to the same prompt")
            self.collected_after_dialog = getattr(
                self, "collected_after_dialog", []) + [role.value]
            return self._read_answer(answer_path, role)

        # A pane Herdr restored is checked only now, after an answer it gave
        # to exactly this prompt was collected above, and before anything is
        # written: used if its input box is empty, replaced otherwise.
        if sending:
            target = self._check_restored(role, state, target)
            self._await_ready(role, state, target)

        if sending:
            # Refused before anything is written: a message the input box
            # reads as a command is never sent.
            _first_line_guard(sent, f"{role.value} dispatch")

        # Every name this dispatch writes is examined before the first write,
        # so a planted link fails the dispatch with nothing written at all.
        if sending and role in STRUCTURED_ROLES:
            _check_leaf(answers_fd, self._marker_for(answer_path).name, where)
        if sending and through_file:
            _check_leaf(answers_fd, prompt_name, where)
            _check_leaf(answers_fd, prompt_marker_name, where)
            _check_audit(run_fd, str(self.answers_dir.parent))

        if sending and answer_path.exists():
            # Do not reuse an answer from an earlier round.
            answer_path.rename(answer_path.with_name(f"{answer_path.name}.v{utcnow()}"))
        if sending and role in STRUCTURED_ROLES:
            self._write_dispatch_marker(answer_path, full, answers_fd)

        if sending and through_file:
            dispatched_at = time.time()
            _write_leaf(answers_fd, prompt_name, full_bytes, where)
            _write_leaf(answers_fd, prompt_marker_name, (json.dumps({
                "prompt_sha256": full_sha,
                "bytes": len(full_bytes),
                "prompt_path": str(prompt_file),
                "dispatched_at": dispatched_at,
            }) + "\n").encode("utf-8"), where)
            # Before the dispatch, so it survives one that is aborted. An
            # audit trail only: no decision ever reads it.
            _append_audit(run_fd, {
                "role": role.value,
                "iteration": state.iteration,
                "attempt": state.attempt,
                "prompt_file": prompt_name,
                "prompt_sha256": full_sha,
                "bytes": len(full_bytes),
                "dispatched_at": dispatched_at,
            }, str(self.answers_dir.parent))
            # The binding in the run's own record, persisted with state.json
            # by the controller's next write_state -- not only in the answers
            # directory, which the role can write.
            state.note(f"prompt {role.value}: sha256={full_sha} "
                       f"bytes={len(full_bytes)} file={prompt_name}")

        sent_bytes = len(sent.encode("utf-8"))
        if sent_bytes > ARGV_ELEMENT_LIMIT:
            raise _refuse(
                f"{role.value} dispatch: an argument of {sent_bytes} bytes "
                f"exceeds {ARGV_ELEMENT_LIMIT}; refusing to build it")

        # Witnessed before the dispatch, so an unstructured role's delivery can
        # be measured rather than read off its pane. Cheap: a digest over the
        # arena, which is the tree the role is expected to change.
        earlier = None
        if sending and role not in STRUCTURED_ROLES:
            from .capability import tree_digest

            destination = self.role_cwd.get(role) or getattr(self, "cwd", None)
            if destination:
                earlier = tree_digest(Path(destination))
        if not sending:
            # The stalled send's turn, awaited to its end: judged with that
            # send's time and that send's tree digest, never re-sent.
            data, sent_at, earlier = settled
        else:
            # The role timeout is counted from here, by a follow-up's passive
            # wait as much as by Herdr.
            sent_at = time.monotonic()
        try:
            if sending:
                data = self.control.prompt(target, sent, timeout_ms=self.timeout_ms,
                                           purpose=f"{role.value} dispatch")
        except Exception as exc:
            # The mark comes first, whatever the failure: until the pane is
            # read, it is not known whether the prompt is in its input box,
            # in a turn, or nowhere (`_settle`).
            self._unsettled()[target] = (full_sha, sent_at, earlier)
            # Only `agent_prompt_stalled` carries a back-off (`_cli`): the one
            # failure after which the prompt may sit typed in the box.
            if getattr(exc, "retry_after", 0):
                self._stalls()[target] = (
                    full_sha, hashlib.sha256(sent.encode("utf-8")).hexdigest())
            else:
                self._stalls().pop(target, None)
            # A tampered prompt is never retried as transient, whatever the
            # failure that ended the turn.
            now = tampered()
            if now is not None:
                raise changed(now, f"when the dispatch failed with: {exc}") from exc
            if not isinstance(exc, DispatchError):
                raise
            # Every dispatch failure takes the pane content with it, not just
            # a block. A "timed out waiting for agent status" says nothing by
            # itself: behind it there can be an exhausted quota, a crash, a
            # question back, or genuine compute time. Three times an A02 run
            # failed on a cause that was no longer determinable afterwards,
            # because the tab had been cleaned up.
            raise DispatchError(f"{exc}\n{self._pane_view(target)}",
                                transient=getattr(exc, "transient", False),
                                retry_after=getattr(exc, "retry_after", 0.0)) from exc
        self._unsettled().pop(target, None)
        self._stalls().pop(target, None)

        self.endpoints[role] = data.evidence()

        # Compared with the digest held here, never with the marker on disk:
        # the role can rewrite that marker as easily as the file.
        now = tampered()
        if now is not None:
            raise changed(now, "after the turn")

        status = data.status
        delivered_ = (
            self._answered(answer_path, role) or self._delivered(role, earlier)
        )
        if status == "blocked" and not delivered_:
            # **Read** the dialog before anything is cleaned up. Twice a run
            # came to a halt at an approval, and both times it was afterwards
            # not determinable which one -- the pane was gone. A blocker
            # without a cause is not a finding but a dead end. `agent read`
            # exists for exactly that; as run state the text is still no good,
            # as a diagnosis very much so.
            raise WaitingForApproval(
                f"{role.value} waits for an approval in pane {data.pane_id}. "
                "A blocked dialog is not answered automatically.\n"
                f"{self._pane_view(target)}"
            )
        if status == "blocked":
            # The pane is blocked and the role has **already answered**. A
            # planner that wrote its plan and then left its pane at an input
            # prompt is finished; the pane's state is a diagnosis, not a
            # verdict. Measured on a real confinement run: the plan file was
            # written, the transcript said so, and the run blocked anyway.
            #
            # This is what `RoleExecutionPolicy.output_channel` is for. A role
            # answers through exactly one file, so that is where the question
            # "did it answer" is decided -- not in a screen scrape.
            self._note_blocked_but_answered[role] = data.pane_id

        if role not in STRUCTURED_ROLES:
            return f"developer run finished (status {status})"

        # The file can appear shortly after the end of the turn.
        deadline = time.monotonic() + ANSWER_GRACE_SECONDS
        while (not self._answered(answer_path, role)
               and time.monotonic() < deadline):
            time.sleep(POLL_SECONDS)
        # Collected only while the prompt file still holds `full`; a change
        # during the grace wait wins over the answer and over its absence.
        now = tampered()
        if now is not None:
            raise changed(now, "after the turn")
        if self._answered(answer_path, role):
            return self._read_answer(answer_path, role)
        # Idle is not done. The turn ended without the answer file -- a role
        # that left work running in the background ends exactly like this.
        # Raised as its own type, carrying what this dispatch sent, so the
        # controller can ask once more (`follow_up`) instead of booking an
        # outage. The pane view goes with it, as with every dispatch failure.
        raise AnswerMissing(
            f"{role.value} ended its turn without writing the answer file "
            f"({answer_path}): it is missing or empty after "
            f"{ANSWER_GRACE_SECONDS:.0f} s. Without a structured answer there "
            f"is nothing to evaluate.\n{self._pane_view(target)}",
            answer_path=answer_path, target=target, prompt_sha256=full_sha,
            prompt_bytes=len(full_bytes),
            prompt_file=prompt_name if through_file else None, sent_at=sent_at)


    # -- Idle is not done: one follow-up ------------------------------------ #

    def _agent_state(self, role: Role, target: str) -> str:
        """The agent's status, `result.agent.agent_status` of `herdr agent get`.

        Read through `_cli`, the seam every test fakes, and never through
        `herdr.agent_status`, which refuses outside Herdr. Raises whatever
        `_cli` raises; the caller decides what an unreadable status means.
        """
        return self.control.status(target, purpose=f"{role.value} status read").status

    def follow_up(self, role: Role, state: RunState, missing: AnswerMissing, *,
                  charge) -> str:
        """`_follow_up`, and a pane that waits for an approval, or whose state
        is not proven, is kept open exactly as `dispatch` keeps it."""
        try:
            answer = self._follow_up(role, state, missing, charge=charge)
        except WaitingForApproval:
            self.keep_open(role, "an approval is waiting in it")
            raise
        except PaneNotClean:
            self.keep_open(role, "its state is not proven; kept for inspection")
            raise
        self._lift_keep(role)
        return answer

    def _follow_up(self, role: Role, state: RunState, missing: AnswerMissing, *,
                   charge) -> str:
        """Ask a structured role once more for the answer its turn ended without.

        Idle is not done: four iterations were lost to a role that started its
        work in the background and ended its turn, and in `spark` the answer
        arrived minutes after the iteration had been closed. Called by the
        controller for an `AnswerMissing`, at most once per dispatch; `charge`
        is the controller's callback that checks the witness and spends one
        dispatch from the budget, raising if either refuses.

        In order: the answer is re-checked (it may have arrived meanwhile) and
        collected for free; the agent's status is read once, fail-closed; an
        agent that resumed by itself (`working`) is waited for passively,
        uncharged, up to the role timeout; a `blocked` pane is never prompted,
        and neither is one whose status does not prove the turn ended; only
        then is one dispatch charged and one short inline prompt sent.

        Every collection first re-hashes the prompt file against the digest
        `missing` carries from the dispatch that sent it, and refuses on a
        mismatch. Nothing is written in `answers/`: no marker, no prompt file,
        no audit record, no rename -- so the dispatch marker still binds the
        original prompt, and O199 collects a late answer on a resume.
        """
        answer_path = missing.answer_path
        target = missing.target
        answers_fd = _open_dir(self.answers_dir, "answers directory")
        try:
            def check_prompt(when: str) -> None:
                if missing.prompt_file is None:
                    return
                now = _rehash(answers_fd, missing.prompt_file, missing.prompt_bytes)
                if now != missing.prompt_sha256:
                    raise _refuse(
                        f"{role.value} follow-up: the prompt file "
                        f"{missing.prompt_file} changed {when} (sha256 "
                        f"{missing.prompt_sha256} when sent, {now} now). What "
                        "the role was asked is no longer what is on disk; the "
                        "answer is not collected, and the run does not go on "
                        "from here.")

            def collect(when: str, how: str) -> str | None:
                check_prompt(when)
                if not self._answered(answer_path, role):
                    return None
                state.note(f"{role.value} answer {answer_path.name} {how}")
                return self._read_answer(answer_path, role)

            def grace() -> None:
                deadline = time.monotonic() + ANSWER_GRACE_SECONDS
                while (not self._answered(answer_path, role)
                       and time.monotonic() < deadline):
                    time.sleep(POLL_SECONDS)

            def unreadable(exc: BaseException) -> DispatchError:
                return _refuse(
                    f"{role.value} follow-up not sent: the agent's status could "
                    f"not be read ({exc}). A turn whose state is unknown is "
                    f"never prompted.\n{self._pane_view(target)}")

            # The answer may have arrived after the grace wait. Collected for
            # free: no status read, no prompt, no charge.
            answer = collect("before the follow-up",
                             "collected before the follow-up: it arrived after "
                             "the grace wait; nothing was sent")
            if answer is not None:
                return answer

            # One status read, fail-closed, before anything is charged.
            try:
                status = self._agent_state(role, target)
            except Exception as exc:
                raise unreadable(exc) from exc

            if status == "working":
                # The agent resumed by itself. Waiting for its turn to end is
                # not a dispatch: nothing is charged and nothing is sent. The
                # answer is not taken mid-turn -- the role would go on working
                # unwitnessed, and the next dispatch could reach it.
                state.note(f"follow-up to {role.value} not sent, the agent "
                           "resumed by itself; waiting for its turn to end")
                deadline = missing.sent_at + self.timeout_ms / 1000
                failures = 0
                while status == "working" and time.monotonic() < deadline:
                    time.sleep(max(STATUS_POLL_SECONDS, 10.0))
                    try:
                        status = self._agent_state(role, target)
                        failures = 0
                    except Exception as exc:
                        # One failed read does not end the wait; it counts as
                        # still working and is read again next interval.
                        failures += 1
                        if failures >= STATUS_READ_ATTEMPTS:
                            raise unreadable(exc) from exc
                if status == "working":
                    answer = collect("during the wait for the agent's turn",
                                     "collected at the role timeout while the "
                                     "agent still reported working")
                    if answer is not None:
                        return answer
                    raise _refuse(
                        f"{role.value} was still working at the role timeout "
                        f"({self.timeout_ms} ms after the prompt was sent), and "
                        f"its answer file {answer_path} is missing or empty. "
                        f"Nothing was sent.\n{self._pane_view(target)}")
                grace()
                answer = collect("during the wait for the agent's turn",
                                 "collected after the agent's own turn ended "
                                 f"(status {status}); nothing was sent")
                if answer is not None:
                    return answer

            if status == "blocked":
                # Never a follow-up into a dialog.
                raise WaitingForApproval(
                    f"{role.value} waits for an approval in pane {target}; its "
                    "turn ended without the answer file, and no follow-up is "
                    "sent into a dialog. A blocked dialog is not answered "
                    f"automatically.\n{self._pane_view(target)}")
            if status not in ("idle", "done"):
                # Only idle and done prove that a turn ended
                # (`herdr.liveness`). A follow-up could land in a live turn.
                raise _refuse(
                    f"{role.value} follow-up not sent: the agent's status "
                    f"{status!r} does not prove that its turn ended.\n"
                    f"{self._pane_view(target)}")

            text = f"""[answer-file-missing]
Your turn ended, but your answer file was not written:

  {answer_path}

If you started work in the background, wait for it to finish -- do not end
your turn while it is still running -- and then write your JSON answer to
exactly that path, as your instructions say. Reply in the chat with a single
sentence saying that the file is written.
"""
            text_bytes = len(text.encode("utf-8"))
            if text_bytes > ARGV_ELEMENT_LIMIT:
                raise _refuse(
                    f"{role.value} follow-up: an argument of {text_bytes} "
                    f"bytes exceeds {ARGV_ELEMENT_LIMIT}; refusing to build it")

            # Refused before the charge: a refused message is never sent, and
            # never paid for.
            _first_line_guard(text, f"{role.value} follow-up")
            # Charged directly before the send: a charge never buys no prompt.
            charge()
            try:
                data = self.control.prompt(target, text, timeout_ms=self.timeout_ms,
                                           purpose=f"{role.value} follow-up")
            except Exception as exc:
                # Like a dispatch that did not return: the next prompt to this
                # pane reads it first (`_settle`).
                self._unsettled()[target] = (
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    time.monotonic(), None)
                check_prompt(f"when the follow-up failed with: {exc}")
                # Never retried: one follow-up per dispatch.
                raise _refuse(
                    f"{exc}\n{role.value} follow-up failed; a follow-up is not "
                    f"retried.\n{self._pane_view(target)}") from exc

            check_prompt("after the follow-up turn")
            self.endpoints[role] = data.evidence()
            if (data.status == "blocked"
                    and not self._answered(answer_path, role)):
                raise WaitingForApproval(
                    f"{role.value} waits for an approval in pane "
                    f"{data.pane_id} after the follow-up. A blocked "
                    "dialog is not answered automatically.\n"
                    f"{self._pane_view(target)}")

            grace()
            answer = collect("after the follow-up turn", "collected after the follow-up")
            if answer is not None:
                return answer
            raise AnswerMissing(
                f"{role.value} ended its turn without writing the answer file "
                f"({answer_path}), even after one follow-up. Without a "
                f"structured answer there is nothing to evaluate.\n"
                f"{self._pane_view(target)}",
                answer_path=answer_path, target=target,
                prompt_sha256=missing.prompt_sha256,
                prompt_bytes=missing.prompt_bytes,
                prompt_file=missing.prompt_file, sent_at=missing.sent_at,
                followed_up=True)
        finally:
            os.close(answers_fd)


def build_dispatcher(
    *,
    answers_dir: Path,
    profiles: dict[Role, str],
    cwd: Path | str,
    prefer_herdr: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    models: dict[Role, str] | None = None,
    efforts: dict[Role, str] | None = None,
    approval_policy=None,
) -> _Base:
    """Picks the adapter -- and says honestly when it is not the evidencing one."""
    if prefer_herdr:
        if not herdr.available():
            raise DispatchError(
                "Herdr is not available (HERDR_ENV != 1 or the CLI is missing). "
                "A01 demands the real Herdr endpoint; a silent fallback to a "
                "subprocess does not fulfill the assignment. With --no-herdr one "
                "can deliberately work without any acceptance value."
            )
        run_dir = Path(answers_dir).parent
        registry = OwnedAgentRegistry(run_dir / OWNED_FILE, run_id=run_dir.name)
        d = HerdrDispatcher(answers_dir=answers_dir, profiles=profiles, cwd=cwd,
                            timeout_ms=timeout_ms, models=models, efforts=efforts,
                            registry=registry)
        d.approval_policy = approval_policy
        d.register_role_scratch()
        return d
    # The subprocess adapter counts in seconds, not in milliseconds.
    d = HarnessDispatcher(answers_dir=answers_dir, profiles=profiles, cwd=cwd,
                          timeout=max(1, timeout_ms // 1000),
                          models=models, efforts=efforts)
    d.approval_policy = approval_policy
    d.register_role_scratch()
    return d
