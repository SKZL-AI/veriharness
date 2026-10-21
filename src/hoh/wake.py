"""Consultation wake: the master asks its predecessor one question.

A rollover (`hoh.rollover`) replaces the orchestrator conversation: the old
session is stale the moment the successor holds the next epoch, and goes idle.
Sometimes the successor needs what only the old conversation knows. The
operator's protocol until now was manual: resume the predecessor's Claude
session in a new surface, hand it a brief as a file, let it answer into one new
file, and never close its surface. This module is the part of that protocol that
can be checked.

Three things, in this order:

* `read_succession` -- a read model over the succession records
  (`rollover/<id>.receipt.json`, `<id>.capsule.json`, the epoch chain): who the
  caller's predecessor was, at which epoch, and in which working directory that
  session was started (the directory `run_rollover` made for it, when it was
  itself a successor). Every record is read without following links and without
  blocking; a record that is odd in any way refuses -- there is no best-effort
  reading of a succession. A master that took its own program again after a
  lapse (`Reason.RECOVERY`, any number of times, always the same session) is
  traced back to the hand-over that made it the master (`anchor_through_recovery`);
  every other way of holding an epoch without a receipt is refused.
* `plan_wake` -- **only the live master** gets a plan: the caller's authority is
  checked first, then the records are read, then the arguments are validated.
  The plan is a value: the surface label (it carries "(keep)"), the working
  directory, the agent name, the arguments of `claude`, the one prompt and the
  one file the answer goes into. Planning writes nothing and starts nothing.
* `perform_wake` -- opens the surface, starts the agent and sends the prompt,
  all through `AgentControlAdapter` (`hoh.agentcontrol`); this module spells no
  Herdr command. It never closes anything: a predecessor session and its
  surfaces are the operator's to close, also when the wake failed half-way.
  `make_control` builds the adapter's Herdr implementation with a transport
  whose deadline follows each command's own `--timeout` (the shared
  `surfaces.herdr_transport` gives every command 30 s, which fails a
  consultation turn of any length).

What the wake does and does not do about authority, measured and not claimed
beyond that. The woken session's **own** credentials are those of an epoch that
has ended, so every act it attempts through the product's commands and handles
(`handoff`, `release`, `renew`, `stamp`) is refused as a stale master by code
that already exists (`hoh.succession`, `hoh.epoch`); nothing here makes it so.
The plan hands it nothing else: its working directory is its own launch
directory, its brief and its answer lie outside the rollover directory (which
holds the live master's token file) and none of the three is, contains or lies
in the token file the caller presented (`token_file_fence`: after a re-arm the
live token is no longer in the rollover directory), and the prompt carries no
credential and no path to one. **This is not isolation.** The session runs as the same OS user
as the master: the master's token file is readable to it, a raw write to the
state file is refused only once the master has written at its epoch, and a
merge, a ref or a release is bound by the prompt's words and the operator's
rules, not by this product (limits L11-L13 of the spec).

Nothing is deleted and no file is written here.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import herdr
from .agentcontrol import AgentControlAdapter, HerdrAgentControl, Surface
from .controller import DispatchError
from .durable_io import read_single_link
from .epoch import EpochError, MasterEpoch, Reason
from .rollover import HERDR_AGENT_NAME_CHARS
from .succession import StaleMaster
from .succession_capsule import (
    RECEIPT_SCHEMA, Capsule, CapsuleError, check_capsule_id, rollover_dir, sha256_of,
)

SCHEMA = "hoh-wake-plan/1"

#: The harness whose session is resumed. Only Claude Code has `--resume`.
KIND = "claude"

#: One record file is read at most this large; a bigger one is refused unread.
MAX_RECORD_BYTES = 1024 * 1024

#: The prompt is a pointer to two files, not a briefing.
PROMPT_MAX_CHARS = 2000

#: How long `perform_wake` waits for the predecessor's turn to end (Herdr's
#: `agent prompt --wait`), and the longest it may be asked to.
DEFAULT_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 3600.0

#: `agent start` waits for the agent to be ready: Herdr's own timeout for it, passed
#: explicitly so that the transport's deadline is derived from a pinned value.
START_TIMEOUT_MS = 240000

#: The process that runs a Herdr command gives up after the command's own
#: `--timeout` plus this overhead, and after this floor when it has none.
TRANSPORT_FLOOR_S = 30.0
TRANSPORT_OVERHEAD_S = 30.0

#: A new tab's shell needs a moment; `agent start` is asked again, this many
#: times in all, while Herdr says the shell is not available yet.
START_ATTEMPTS = 5
SETTLE_S = 2.0

#: The surface carries this word in its label: the operator's rule is that a
#: predecessor's surface is never closed by an orchestrator.
KEEP_MARK = "(keep)"

#: What the plan says must be true before it is performed.
PRECONDITION = ("the predecessor's own surface is closed (a consultation resumes its session; "
                "if that surface is still open, message it instead); pass "
                "--predecessor-surface-closed with --execute")

#: A session id from a record becomes part of a label and an agent name: plain
#: characters only, so a record cannot put a line break or an escape there.
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RESUME_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NONCE = re.compile(r"[0-9a-f]{6}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECEIPT_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.receipt\.json")


class WakeRefused(RuntimeError):
    """The wake is refused. Nothing was started and nothing was written."""


class WakeUsage(WakeRefused):
    """The wake is refused because of a value the caller supplied (or the
    environment it runs in), not because of the programme's records."""


class WakeFailed(RuntimeError):
    """The wake was started and did not complete. `surface` is what was opened
    (None if nothing was): it is left as it is, never closed."""

    def __init__(self, message: str, *, surface: Surface | None, agent_started: bool) -> None:
        super().__init__(message)
        self.surface = surface
        self.agent_started = agent_started


# --------------------------------------------------------------------------- #
# reading records
# --------------------------------------------------------------------------- #


def _read_record(path: Path) -> dict[str, Any]:
    """One record file as a JSON object, or `WakeRefused`.

    Not followed if it is a link, not read if it is not a regular file with one
    name (a FIFO cannot hang this), not read if it is over `MAX_RECORD_BYTES`,
    and not guessed at if it is not UTF-8 JSON that is an object."""
    try:
        size = os.lstat(path).st_size
    except OSError as exc:
        raise WakeRefused(f"{path.name} cannot be examined: {exc.strerror}") from exc
    if size > MAX_RECORD_BYTES:
        raise WakeRefused(f"{path.name} is larger than {MAX_RECORD_BYTES} bytes (not read)")
    try:
        raw = read_single_link(path)
    except OSError as exc:
        raise WakeRefused(f"{path.name} cannot be read: {exc.strerror or exc}") from exc
    if raw is None:
        raise WakeRefused(f"{path.name} vanished while it was being read")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise WakeRefused(f"{path.name} is not UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WakeRefused(f"{path.name} is not a JSON object")
    return data


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 \
        else None


@dataclass(frozen=True)
class Receipt:
    capsule_id: str
    capsule_digest: str
    predecessor_session: str
    predecessor_epoch: int
    successor_session: str
    successor_epoch: int
    lease_token_sha256: str
    file: Path


def _receipt(path: Path, capsule_id: str) -> Receipt:
    data = _read_record(path)
    name = path.name
    if data.get("schema_version") != RECEIPT_SCHEMA:
        raise WakeRefused(f"{name} is not a {RECEIPT_SCHEMA} record")
    digest = data.get("receipt_digest")
    body = {k: v for k, v in data.items() if k != "receipt_digest"}
    try:
        recomputed = sha256_of(body)
    except CapsuleError as exc:
        raise WakeRefused(f"{name} cannot be digested: {exc}") from exc
    if not isinstance(digest, str) or digest != recomputed:
        raise WakeRefused(f"{name} does not match its own digest: it was changed after "
                          f"it was written")
    if data.get("capsule_id") != capsule_id:
        raise WakeRefused(f"{name} names capsule {data.get('capsule_id')!r}, not the one "
                          f"its file name says")
    pred, succ = data.get("predecessor_session"), data.get("successor_session")
    pred_epoch, succ_epoch = _int(data.get("predecessor_epoch")), _int(data.get("successor_epoch"))
    token, cap_digest = data.get("lease_token_sha256"), data.get("capsule_digest")
    if not (isinstance(pred, str) and pred and pred == pred.strip()
            and isinstance(succ, str) and succ and succ == succ.strip()):
        raise WakeRefused(f"{name} names a session that is not a non-empty string without "
                          f"edge whitespace")
    if pred_epoch is None or succ_epoch is None:
        raise WakeRefused(f"{name} carries an epoch that is not a positive integer")
    if not (isinstance(token, str) and _SHA256.fullmatch(token)
            and isinstance(cap_digest, str) and _SHA256.fullmatch(cap_digest)):
        raise WakeRefused(f"{name} carries a digest that is not a sha256")
    return Receipt(capsule_id, cap_digest, pred, pred_epoch, succ, succ_epoch, token, path)


def _receipts(directory: Path | str) -> list[Receipt]:
    """Every receipt of the programme, each one read and checked. One that
    cannot be is a refusal, not a gap: the receipt that matters may be it."""
    rdir = rollover_dir(directory)
    try:
        info = os.lstat(rdir)
    except OSError as exc:
        raise WakeRefused(f"no succession records: {rdir.name} cannot be examined: "
                          f"{exc.strerror}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise WakeRefused(f"{rdir.name} is not a directory of its own (a link or a file)")
    out: list[Receipt] = []
    try:
        names = sorted(os.listdir(rdir))
    except OSError as exc:
        raise WakeRefused(f"{rdir.name} cannot be listed: {exc.strerror}") from exc
    for name in names:
        m = _RECEIPT_NAME.fullmatch(name)
        if m:
            out.append(_receipt(rdir / name, m.group(1)))
    return out


@dataclass(frozen=True)
class Succession:
    """What the records say about the hand-over to the current master."""

    capsule_id: str
    predecessor_session: str
    predecessor_epoch: int
    successor_session: str
    successor_epoch: int
    #: The directory the predecessor's own session was started in, when the
    #: records know it (it was itself launched by a rollover); else None.
    predecessor_cwd: str | None
    receipt_file: str
    #: The epochs after `successor_epoch` that the same session began under
    #: `Reason.RECOVERY` (ascending), when the receipt is that of an earlier epoch
    #: of this session; empty when the receipt names the current epoch itself.
    rearmed_epochs: tuple[int, ...] = ()

    @property
    def current_epoch(self) -> int:
        """The epoch the successor holds now."""
        return self.rearmed_epochs[-1] if self.rearmed_epochs else self.successor_epoch

    def as_dict(self) -> dict[str, Any]:
        return {"capsule_id": self.capsule_id,
                "predecessor_session": self.predecessor_session,
                "predecessor_epoch": self.predecessor_epoch,
                "successor_session": self.successor_session,
                "successor_epoch": self.successor_epoch,
                "predecessor_cwd": self.predecessor_cwd,
                "receipt_file": self.receipt_file,
                "rearmed_epochs": list(self.rearmed_epochs)}


def _no_receipt(session: str, epoch: int, why: str = "") -> WakeRefused:
    text = (f"no receipt names {session!r} as the successor at epoch {epoch}: "
            f"nobody handed this epoch over by a rollover, so there is no "
            f"predecessor to wake")
    return WakeRefused(f"{text} ({why})" if why else text)


def _rollover_note(capsule_id: str) -> str:
    """The note `Successor.take` (`hoh.rollover`) puts on the epoch it begins."""
    return f"rollover {capsule_id}"


def anchor_through_recovery(receipts: Sequence[Receipt], *, session: str, epoch: int,
                            epochs: Sequence[MasterEpoch]) -> tuple[Receipt, tuple[int, ...]]:
    """The receipt behind an epoch that `session` holds by re-arming itself, and
    the epochs it re-armed, ascending; or `WakeRefused`.

    Called when no receipt names `session` at `epoch`. The epoch chain is
    followed downwards: every epoch from `epoch` on must be held by `session` and
    have begun as `Reason.RECOVERY` (the session took its own program again after
    its lease lapsed), until an epoch that a receipt names `session` as the
    successor of. That epoch must be the one the rollover began: reason
    `SUCCESSION` and the note `Successor.take` writes for the receipt's capsule --
    the receipt's own lease-token digest binds the lease of *that* epoch, which a
    re-arm has replaced, so the chain and the note are what bind it now. Another
    holder, a missing epoch, any other reason, a second receipt for the anchor, or
    the end of the chain without a receipt is a refusal: nothing is guessed."""
    by_epoch: dict[int, MasterEpoch] = {}
    for rec in epochs:
        if rec.epoch in by_epoch:
            raise WakeRefused(f"the epoch chain lists epoch {rec.epoch} twice: ambiguous, "
                              f"nothing is guessed")
        by_epoch[rec.epoch] = rec
    rearmed: list[int] = []
    at = epoch
    while True:
        rec = by_epoch.get(at)
        if rec is None:
            raise _no_receipt(session, epoch, f"epoch {at} is not in the epoch chain")
        if rec.master_id != session:
            raise _no_receipt(session, epoch, f"epoch {at} was held by {rec.master_id!r}")
        if rec.reason is not Reason.RECOVERY:
            raise _no_receipt(session, epoch, f"epoch {at} began as {rec.reason.value}, "
                                              f"not as a re-arm of the session's own lease")
        rearmed.append(at)
        at -= 1
        before = by_epoch.get(at)
        if before is None or before.master_id != session:
            held = before.master_id if before is not None else None
            raise _no_receipt(session, epoch, f"epoch {at} is "
                              + (f"held by {held!r}" if held else "not in the epoch chain"))
        named = [r for r in receipts
                 if r.successor_session == session and r.successor_epoch == at]
        if len(named) > 1:
            raise WakeRefused(f"{len(named)} receipts name {session!r} at epoch {at}: "
                              f"ambiguous, nothing is guessed")
        if named:
            receipt = named[0]
            if before.reason is not Reason.SUCCESSION \
                    or before.note != _rollover_note(receipt.capsule_id):
                raise WakeRefused(f"{receipt.file.name} names epoch {at}, which the epoch "
                                  f"chain does not say that rollover began "
                                  f"(reason {before.reason.value})")
            return receipt, tuple(reversed(rearmed))


def read_succession(directory: Path | str, *, project_id: str, session: str, epoch: int,
                    token_sha256: str, chain: Mapping[int, str],
                    epochs: Sequence[MasterEpoch] | None = None) -> Succession:
    """The hand-over that made `session` the master at `epoch`, from the records.

    Read-only, and it grants nothing: the authority check is `plan_wake`'s.
    `token_sha256` is the digest of the token of the lease the caller holds and
    `chain` the epoch chain (epoch -> master id): the receipt must be the one
    written when *this* lease was taken, and the chain must say that the
    receipt's predecessor held the receipt's predecessor epoch. Anything that
    does not fit is `WakeRefused`.

    `epochs` is the epoch chain with each epoch's reason and note. Given, a
    session that holds `epoch` because it re-armed its own lease under
    `Reason.RECOVERY` is traced back to its hand-over (`anchor_through_recovery`)
    and the receipt's token digest, which that re-arm made stale, is not
    compared; without it only a receipt that names `epoch` itself is read."""
    directory = Path(directory)
    receipts = _receipts(directory)
    mine = [r for r in receipts if r.successor_session == session and r.successor_epoch == epoch]
    rearmed: tuple[int, ...] = ()
    if not mine:
        if epochs is None:
            raise _no_receipt(session, epoch)
        receipt, rearmed = anchor_through_recovery(receipts, session=session, epoch=epoch,
                                                   epochs=epochs)
        mine = [receipt]
    if len(mine) > 1:
        raise WakeRefused(f"{len(mine)} receipts name {session!r} at epoch {epoch}: "
                          f"ambiguous, nothing is guessed")
    receipt = mine[0]
    # The plain-name rule is for the sessions the plan will print (a label, an agent
    # name, a prompt): the two of the selected receipt. The product accepts any
    # non-empty session id, so another receipt's odd id is not this plan's concern.
    for who in (receipt.predecessor_session, receipt.successor_session):
        if not _SESSION.fullmatch(who):
            raise WakeRefused(f"{receipt.file.name} names a session that is not a plain id "
                              f"({_SESSION.pattern}); a label and a name cannot be made "
                              f"from it")
    if not rearmed and receipt.lease_token_sha256 != token_sha256:
        raise WakeRefused(f"{receipt.file.name} was written for another lease than the "
                          f"one {session!r} holds now")
    if receipt.predecessor_session == session:
        raise WakeRefused(f"{receipt.file.name} names the caller as its own predecessor")
    if chain.get(receipt.predecessor_epoch) != receipt.predecessor_session:
        raise WakeRefused(f"the epoch chain says epoch {receipt.predecessor_epoch} was held "
                          f"by {chain.get(receipt.predecessor_epoch)!r}, not by "
                          f"{receipt.predecessor_session!r} as {receipt.file.name} says")
    cap_file = rollover_dir(directory) / f"{receipt.capsule_id}.capsule.json"
    cap_data = _read_record(cap_file)
    try:
        cap = Capsule.model_validate(cap_data)
    except ValueError as exc:
        raise WakeRefused(f"{cap_file.name} is not a succession capsule: {exc}") from exc
    if cap.compute_digest() != cap.capsule_digest or cap.capsule_digest != receipt.capsule_digest:
        raise WakeRefused(f"{cap_file.name} is not the capsule {receipt.file.name} binds "
                          f"(digest)")
    if (cap.project_id != project_id or cap.capsule_id != receipt.capsule_id
            or cap.predecessor_session != receipt.predecessor_session
            or cap.predecessor_epoch != receipt.predecessor_epoch
            or cap.successor_session != receipt.successor_session
            or cap.successor_epoch != receipt.successor_epoch):
        raise WakeRefused(f"{cap_file.name} and {receipt.file.name} disagree about the "
                          f"project, the sessions or the epochs")
    cwd: str | None = None
    earlier = [r for r in receipts if r.successor_session == receipt.predecessor_session
               and r.successor_epoch == receipt.predecessor_epoch]
    if len(earlier) > 1:
        raise WakeRefused(f"{len(earlier)} receipts name {receipt.predecessor_session!r} "
                          f"at epoch {receipt.predecessor_epoch}: ambiguous")
    if earlier:
        cwd = _launch_dir(directory, earlier[0].capsule_id)
    return Succession(receipt.capsule_id, receipt.predecessor_session,
                      receipt.predecessor_epoch, receipt.successor_session,
                      receipt.successor_epoch, cwd, str(receipt.file), rearmed)


def _launch_dir(directory: Path, capsule_id: str) -> str:
    """`rollover/<capsule id>.cwd`: the directory `run_rollover` started the
    session in. It must exist, as a directory of its own, directly under the
    rollover directory (a session's transcripts are kept per directory: a
    resume from anywhere else does not find them)."""
    rdir = rollover_dir(directory)
    path = rdir / f"{check_capsule_id(capsule_id)}.cwd"
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise WakeRefused(f"the predecessor's directory {path.name} cannot be examined: "
                          f"{exc.strerror}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise WakeRefused(f"{path.name} is not a directory of its own (a link or a file)")
    return os.path.join(os.path.realpath(rdir), path.name)


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #


def new_nonce() -> str:
    """Six hex digits: tells two consultations of one predecessor apart."""
    return secrets.token_hex(3)


def _plain(text: str) -> bool:
    return text.isascii() and text.isprintable()


def wake_agent_name(session: str, nonce: str) -> str:
    """The Herdr agent name: a fixed letter, the random part, the session.
    The random part comes first because Herdr cuts names at about 24
    characters, and the whole is lower-cased onto Herdr's alphabet."""
    raw = f"w{nonce}-{session}".lower()
    return "".join(c if c in HERDR_AGENT_NAME_CHARS else "-" for c in raw)[:24]


@dataclass(frozen=True)
class WakePlan:
    project_id: str
    succession: Succession
    cwd: str
    label: str
    agent_name: str
    kind: str
    agent_args: tuple[str, ...]
    resume_id: str
    brief_file: str
    answer_file: str
    prompt: str

    def as_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "project_id": self.project_id,
                "succession": self.succession.as_dict(), "cwd": self.cwd,
                "label": self.label, "agent_name": self.agent_name, "kind": self.kind,
                "agent_args": list(self.agent_args), "resume_id": self.resume_id,
                "brief_file": self.brief_file, "answer_file": self.answer_file,
                "precondition": PRECONDITION,
                "prompt": self.prompt}


def _consult_prompt(*, predecessor: str, successor: str, project_id: str, epoch: int,
                    brief: str, answer: str) -> str:
    text = (
        f"You are the predecessor orchestrator session {predecessor}, resumed for ONE "
        f"consultation by your successor {successor} (project {project_id}, epoch {epoch}).\n"
        "You are not the master: your epoch has ended and the product's commands refuse "
        "your own credentials. This is a read-and-answer consultation "
        "only. Do not run any command that acts as the master (handoff, rollover, drive, "
        "hold, unblock, record-action); do not merge, tag or release anything; do not "
        "start runs or agents; do not read or use any token or credential file of the "
        "current master.\n"
        "\n"
        f"1. Read the brief: {brief}\n"
        f"2. Write your answer to exactly one file, which must not exist yet: {answer}\n"
        "3. Then stop and wait. Do nothing else.\n")
    if not text.isascii():
        raise WakeRefused("the consultation prompt must be ASCII; a value in it is not")
    if len(text) >= PROMPT_MAX_CHARS:
        raise WakeRefused(f"the consultation prompt is {len(text)} characters, the limit "
                          f"is {PROMPT_MAX_CHARS}")
    return text


def _inside(path: str, root: str) -> bool:
    """`path` is `root` or lies below it (both already `realpath`s)."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _plain_path(value: str, flag: str) -> str:
    """`value` as a normalized absolute path in plain ASCII (no `~`, no `..`, no
    `//`, no trailing slash), or `WakeUsage`."""
    if (not isinstance(value, str) or not value or not os.path.isabs(value)
            or not _plain(value) or os.path.normpath(value) != value):
        raise WakeUsage(f"{flag} must be a normalized absolute path in plain ASCII "
                        f"(no '~', no '..', no '//', no trailing '/'), got {value!r}")
    return value


def token_file_fence(token_file: str, *, cwd: str, brief: str, answer: str) -> None:
    """`WakeUsage` unless the working directory, the brief and the answer are all
    clear of the token file the caller presented.

    The token file is the path the command read the token from (its caller has
    expanded a `~`; this module expands none), normalised as the command's own
    read normalises it (`Path`: a trailing `/` and `//` go), and judged by **both**
    its real path (links followed, **strict**: a path that cannot be resolved is
    refused, not guessed) and its presented spelling made absolute (a link to the
    live token lying in the working directory is the credential's name there).
    Each of `cwd`, `brief` and `answer` (plain absolute paths) is compared by its
    real path: it may not be the token file under either name, contain it, or lie
    below it. A brief that is a second name of the token file (a hard link) is the
    same file and is refused too. The answer does not exist yet, so its
    directory's real path and its name are compared."""
    spelled = os.fspath(Path(token_file))
    try:
        token_real = os.path.realpath(spelled, strict=True)
        token_info = os.stat(token_real)
    except (OSError, ValueError) as exc:
        raise WakeUsage(f"--token-file cannot be resolved ({exc.__class__.__name__}): "
                        f"nothing can be said to be clear of it") from exc
    names = (token_real, os.path.abspath(spelled))
    places = (("--cwd", os.path.realpath(cwd)),
              ("--brief", os.path.realpath(brief)),
              ("--answer", os.path.join(os.path.realpath(os.path.dirname(answer)),
                                        os.path.basename(answer))))
    for flag, real in places:
        for name in names:
            if _inside(real, name) or _inside(name, real):
                raise WakeUsage(f"{flag} is, contains or lies in the token file the "
                                f"caller presented ({name}): the predecessor would be "
                                f"handed the live master's credential; move the token "
                                f"file out of that place")
    try:
        brief_info = os.stat(brief)
    except OSError as exc:
        raise WakeUsage(f"--brief cannot be examined: {exc.strerror}") from exc
    if os.path.samestat(brief_info, token_info):
        raise WakeUsage("--brief is another name (a hard link) of the token file the "
                        "caller presented: the predecessor would be handed the live "
                        "master's credential")


def plan_wake(authority, directory: Path | str, *, project_id: str, resume_id: str,
              brief: str, answer: str, nonce: str, cwd: str | None = None,
              token_file: str | None = None) -> WakePlan:
    """The wake plan for the caller's predecessor, or `WakeRefused`.

    `authority` is the caller's `MasterAuthority`, already presented with its
    epoch and token (`present`). Order: the caller must be the live master
    (`check`), then the records are read, then the arguments are validated --
    a session that is not the master learns nothing about the records.

    `brief` and `answer` are the caller's two files (absolute, normalized, plain
    ASCII): neither may lie in the programme's rollover directory, which holds the
    live master's token file; an explicit `cwd` may not either. `token_file` is the
    path of the token file the caller presented (None when it gave the token inline,
    which has no file to keep clear of): the working directory, the brief and the
    answer must be clear of it (`token_file_fence`) -- the rollover directory only
    holds the token of the hand-over, not that of a lease taken again since."""
    try:
        authority.check()
    except StaleMaster as exc:
        raise WakeRefused(f"not the master: {exc}") from exc
    # `check` passed, so the lease exists and is the caller's: these guards are
    # defensive (a lease that vanished between the two reads).
    lease = authority.read_lease()
    if lease is None or authority.epoch is None:
        raise WakeRefused("the caller's lease cannot be read; nothing to bind a "
                          "succession to")
    if not isinstance(project_id, str) or not _SESSION.fullmatch(project_id):
        raise WakeUsage(f"the project id must be a plain name ({_SESSION.pattern}), "
                        f"got {project_id!r}")
    directory = Path(directory)
    try:
        history = authority.epochs.chain()
    except EpochError as exc:
        raise WakeRefused(f"the epoch chain cannot be read: {exc}") from exc
    chain = {e.epoch: e.master_id for e in history}
    succession = read_succession(
        directory, project_id=project_id, session=authority.session_id,
        epoch=authority.epoch, token_sha256=lease.token_sha256, chain=chain,
        epochs=history)
    if not _RESUME_ID.fullmatch(resume_id or ""):
        raise WakeUsage("--resume-id must be the predecessor's Claude session id: a "
                        "lower-case UUID (the records do not hold it)")
    if not _NONCE.fullmatch(nonce or ""):
        raise WakeUsage("the nonce must be six lower-case hex digits")
    rollover = os.path.realpath(rollover_dir(directory))
    # the directory the predecessor's session was started in: its own
    if succession.predecessor_cwd is not None:
        if cwd is not None and os.path.realpath(cwd) != os.path.realpath(
                succession.predecessor_cwd):
            raise WakeUsage(f"--cwd {cwd!r} is not the directory the records say the "
                            f"predecessor was started in ({succession.predecessor_cwd}); "
                            f"a resume from another directory finds no transcript")
        where = succession.predecessor_cwd
    elif cwd is None:
        raise WakeUsage("the records do not say where the predecessor was started (it was "
                        "not launched by a rollover): give --cwd, the directory its Claude "
                        "session was started in")
    else:
        where = os.path.realpath(cwd)
        if not os.path.isabs(cwd) or not os.path.isdir(where):
            raise WakeUsage(f"--cwd must be an absolute path of an existing directory, "
                            f"got {cwd!r}")
        if _inside(where, rollover):
            raise WakeUsage("--cwd may not be in the programme's rollover directory: it "
                            "holds the live master's token file")
    if not _plain(where):
        raise WakeUsage("the working directory must be plain ASCII text")
    # the brief: a file the successor wrote, which the predecessor will read
    brief_path = _plain_path(brief, "--brief")
    try:
        info = os.lstat(brief_path)
    except OSError as exc:
        raise WakeUsage(f"--brief cannot be examined: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WakeUsage("--brief must be a regular file (not a link, directory or FIFO)")
    if _inside(os.path.realpath(brief_path), rollover):
        raise WakeUsage("--brief may not be in the programme's rollover directory: it "
                        "holds the live master's token file")
    # the answer: one file that must not exist yet, in a directory that does
    answer_path = _plain_path(answer, "--answer")
    if answer_path == brief_path:
        raise WakeUsage("--answer is the brief: one file cannot be both")
    try:
        os.lstat(answer_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WakeUsage(f"--answer cannot be examined: {exc.strerror}") from exc
    else:
        raise WakeUsage(f"{answer_path} already exists: one answer per consultation, and "
                        f"an existing file is never overwritten")
    parent = os.path.realpath(os.path.dirname(answer_path))
    if not os.path.isdir(parent):
        raise WakeUsage(f"the directory of --answer does not exist: {os.path.dirname(answer_path)}")
    if _inside(parent, rollover):
        raise WakeUsage("--answer may not be in the programme's rollover directory: it "
                        "holds the live master's token file")
    if token_file:
        token_file_fence(token_file, cwd=where, brief=brief_path, answer=answer_path)
    pred = succession.predecessor_session
    label = f"VH \u25b8 predecessor {pred} (consult only) {KEEP_MARK}"
    prompt = _consult_prompt(predecessor=pred, successor=succession.successor_session,
                             project_id=project_id, epoch=succession.current_epoch,
                             brief=brief_path, answer=answer_path)
    return WakePlan(project_id=project_id, succession=succession, cwd=where, label=label,
                    agent_name=wake_agent_name(pred, nonce), kind=KIND,
                    agent_args=("--resume", resume_id), resume_id=resume_id,
                    brief_file=brief_path, answer_file=answer_path, prompt=prompt)


# --------------------------------------------------------------------------- #
# performing it
# --------------------------------------------------------------------------- #


def _shell_not_ready(exc: Exception) -> bool:
    text = str(exc)
    return "busy" in text or "not an available shell" in text


def validate_timeout(timeout_s: Any) -> float:
    """`timeout_s` as a float, or `WakeUsage`: finite, above 0, at most `MAX_TIMEOUT_S`."""
    # NaN and infinity fail the chained comparison, so no separate finiteness test
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not 0 < timeout_s <= MAX_TIMEOUT_S):
        raise WakeUsage(f"--timeout must be a number of seconds above 0 and at most "
                        f"{MAX_TIMEOUT_S:g}, got {timeout_s!r}")
    return float(timeout_s)


def perform_wake(plan: WakePlan, control: AgentControlAdapter, *,
                 predecessor_surface_closed: bool,
                 workspace: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S,
                 settle_s: float | None = None,
                 sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Open the surface, start the resumed session, send the prompt.

    `predecessor_surface_closed` is the caller's statement that the predecessor's
    own surface is gone: two processes on one transcript is not a consultation.
    This module cannot see Herdr's panes, so the statement is required and not
    inferred. Opens a surface of its own and acts only on that one. Closes
    nothing, on success or failure: on failure `WakeFailed` says what was opened and the
    surface stays for the operator. `timeout_s` bounds how long the predecessor's
    turn is waited for (`0 < timeout_s <= MAX_TIMEOUT_S`)."""
    if predecessor_surface_closed is not True:
        raise WakeUsage("a wake resumes the predecessor's Claude session, which must not be "
                        "running anywhere else: say that its own surface is closed "
                        "(--predecessor-surface-closed), or message that surface instead")
    validate_timeout(timeout_s)
    pause = sleep if sleep is not None else time.sleep
    settle = SETTLE_S if settle_s is None else settle_s
    surface: Surface | None = None
    started = False
    try:
        surface = control.open_surface(
            cwd=plan.cwd, label=plan.label, workspace=workspace,
            purpose="open the predecessor's consultation surface")
        pause(settle)
        for attempt in range(START_ATTEMPTS):
            try:
                control.start(plan.agent_name, kind=plan.kind, pane_id=surface.pane_id,
                              args=plan.agent_args, timeout_ms=START_TIMEOUT_MS,
                              purpose=f"resume the predecessor as {plan.agent_name}")
                started = True
                break
            except DispatchError as exc:
                if attempt == START_ATTEMPTS - 1 or not _shell_not_ready(exc):
                    raise
                pause(1.0 + attempt * 0.5)
        info = control.prompt(plan.agent_name, plan.prompt,
                              timeout_ms=int(timeout_s * 1000),
                              purpose=f"send the consultation to {plan.agent_name}")
    except DispatchError as exc:
        raise WakeFailed(str(exc), surface=surface, agent_started=started) from exc
    return {"outcome": "WOKEN", "agent": plan.agent_name,
            "pane_id": surface.pane_id, "tab_id": surface.tab_id,
            "status": info.status, "answer_file": plan.answer_file}


# --------------------------------------------------------------------------- #
# the Herdr transport
# --------------------------------------------------------------------------- #


def transport_deadline_s(argv: Sequence[str]) -> float:
    """How long this process waits for one Herdr command: `TRANSPORT_FLOOR_S`, or
    the command's own `--timeout` (milliseconds; the last one before a bare `--`)
    plus `TRANSPORT_OVERHEAD_S` when that is more -- never more than
    `MAX_TIMEOUT_S` plus the overhead. A command that asks Herdr to wait for an
    agent (`agent start`, `agent prompt --wait`) must be waited for that long: a
    deadline shorter than Herdr's own turns a slow answer into a failed wake while
    the agent keeps running."""
    words = list(argv)
    if "--" in words:
        words = words[:words.index("--")]
    ms = None
    for i in range(len(words) - 1, -1, -1):
        if words[i] == "--timeout" and i + 1 < len(words) and words[i + 1].isdecimal() \
                and words[i + 1].isascii():
            ms = int(words[i + 1])
            break
    if ms is None:
        return TRANSPORT_FLOOR_S
    seconds = min(ms / 1000.0, MAX_TIMEOUT_S)
    return max(TRANSPORT_FLOOR_S, seconds + TRANSPORT_OVERHEAD_S)


def herdr_transport(argv: Sequence[str], purpose: str, text: bool = False):
    """Run one Herdr command (no shell) under `transport_deadline_s(argv)`: its
    JSON, or with `text=True` its stdout. Failures are `DispatchError`, as
    `surfaces.herdr_transport` (whose 30 s it replaces for this module)."""
    deadline = transport_deadline_s(argv)
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(f"{purpose}: herdr did not answer within {deadline:g} s",
                            transient=True) from exc
    except OSError as exc:
        raise DispatchError(f"{purpose}: herdr could not be run: {exc}",
                            transient=True) from exc
    if done.returncode != 0:
        raise DispatchError(f"{purpose}: herdr exit {done.returncode}: "
                            f"{(done.stderr or '').strip()[:300]}")
    if text:
        return done.stdout
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise DispatchError(f"{purpose}: herdr returned no JSON: {exc}") from exc


def make_control() -> AgentControlAdapter:
    """The Herdr agent control for a wake; `HerdrUnavailable` outside Herdr
    (`herdr.require_herdr`)."""
    herdr.require_herdr()
    return HerdrAgentControl(herdr_transport)
