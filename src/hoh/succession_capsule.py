"""The succession capsule: what a fresh orchestrator session may be handed, and
what it has to re-derive before it is allowed to become the master.

Layer A of context rollover (P2-07 session succession, P2-08 master epoch,
P2-09 leader lease, P2-14 context rotation, P2-20 context capsule). The
orchestrator conversation of a long programme grows until every request
re-reads a very large context; the cure is to replace the session, not to
compress it. A prose hand-off is not a protocol: nothing in it is checked, and
the successor believes whatever it was told. This module is the part that is
checked.

The capsule is a *frozen, versioned document* (`hoh-succession-capsule/1`),
written once with `durable_create`, that binds the predecessor's epoch and
lease token, the **intended next epoch**, the successor's session, the exact
repository HEAD and branch, and a set of **classed entries**:

* `DERIVED_FACT` -- facts of the programme (a file's digest, the set of live
  runs, whether a candidate is merged). The *product* fills their values, at
  freezing, by running a named deriver on an `Observer`; the author only says
  *what* to derive. The successor runs the same derivers again before it takes
  anything, and reports every difference.
* `OPERATOR_DECISION` -- the operator's words, kept byte for byte, with their
  digest, open or decided. Copied, never answered, never rewritten.
* `ADVISORY_CONTEXT` -- the author's own notes. Bound by the capsule digest
  (so it cannot be swapped unnoticed) but never consulted by verification and
  absent from every section digest: an unchecked claim has no authority.

The **programme generation** is read from the lease at freezing; the author
cannot supply one (`CapsuleInput` has no such field and refuses it).

Nothing here takes a lease, writes outside `rollover/`, or reads a transcript.
`verify_capsule` is read-only, so a successor can look before it touches
anything. Nothing is deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, ValidationError, field_validator, model_validator

from . import capsule as hoh_capsule
from .contracts import Strict
from .durable_io import durable_create
from .succession import Lease, MasterAuthority

SCHEMA = "hoh-succession-capsule/1"
ACK_SCHEMA = "hoh-succession-ack/1"
RECEIPT_SCHEMA = "hoh-succession-receipt/1"
ROLLOVER_DIRNAME = "rollover"

#: The only git subcommands the real observer will run. Validating a capsule
#: must never write to the integration checkout (O177): `rev-parse` and
#: `merge-base` read refs and objects and nothing else.
GIT_READ_ONLY = frozenset({"rev-parse", "merge-base"})

MISSING = "MISSING"

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class CapsuleError(ValueError):
    """A capsule that cannot be frozen, read or checked, for a reason that is
    not a difference between the capsule and the world."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def canonical(value: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, characters kept."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise CapsuleError(f"not representable as JSON: {exc}") from exc


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def sha256_of(value: Any) -> str:
    return sha256_text(canonical(value))


def plus_seconds(ts: str, seconds: int | float) -> str:
    return (datetime.strptime(ts, _TIME_FORMAT).replace(tzinfo=UTC)
            + timedelta(seconds=seconds)).strftime(_TIME_FORMAT)


def new_capsule_id(now: str) -> str:
    """`rc-<UTC stamp>-<8 hex>`: the random part keeps two cycles started in
    one second apart, and the name is safe as a file name."""
    stamp = now.replace("-", "").replace(":", "")
    return f"rc-{stamp}-{secrets.token_hex(4)}"


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

#: A capsule id names files under `rollover/`, so it is a plain name: letters,
#: digits, `_` and `-`, at most 64 characters, no dot, no separator. `new_capsule_id`
#: makes `rc-<stamp>-<8 hex>`. A capsule read from disk is untrusted: an id such
#: as `../../x` would otherwise place the successor's acknowledgement outside the
#: rollover directory.
_CAPSULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def check_capsule_id(capsule_id: object) -> str:
    """The id, or `CapsuleError`. The single choke point for every path below."""
    if not isinstance(capsule_id, str) or not _CAPSULE_ID.fullmatch(capsule_id):
        raise CapsuleError(f"{capsule_id!r} is not a capsule id: letters, digits, '_' and '-' "
                           f"only, 1-64 characters, starting with a letter or digit")
    return capsule_id


def rollover_dir(directory: Path | str) -> Path:
    return Path(directory) / ROLLOVER_DIRNAME


def capsule_path(directory: Path | str, capsule_id: str) -> Path:
    return rollover_dir(directory) / f"{check_capsule_id(capsule_id)}.capsule.json"


def ack_path(directory: Path | str, capsule_id: str) -> Path:
    return rollover_dir(directory) / f"{check_capsule_id(capsule_id)}.ack.json"


def receipt_path(directory: Path | str, capsule_id: str) -> Path:
    return rollover_dir(directory) / f"{check_capsule_id(capsule_id)}.receipt.json"


def aborted_path(directory: Path | str, capsule_id: str) -> Path:
    return rollover_dir(directory) / f"{check_capsule_id(capsule_id)}.aborted.json"


# --------------------------------------------------------------------------- #
# classes and sections
# --------------------------------------------------------------------------- #


class EntryClass(StrEnum):
    DERIVED_FACT = "DERIVED_FACT"
    OPERATOR_DECISION = "OPERATOR_DECISION"
    ADVISORY_CONTEXT = "ADVISORY_CONTEXT"


class Section(StrEnum):
    REGISTER = "REGISTER"
    RUNS = "RUNS"
    CANDIDATES = "CANDIDATES"
    REVIEWS = "REVIEWS"
    BLOCKERS = "BLOCKERS"
    DECISIONS = "DECISIONS"
    BACKGROUND = "BACKGROUND"
    DO_NOT_TOUCH = "DO_NOT_TOUCH"
    CONTEXT = "CONTEXT"


#: The class a section holds. The class fixes what may be in an entry.
SECTION_CLASS: dict[Section, EntryClass] = {
    Section.REGISTER: EntryClass.DERIVED_FACT,
    Section.RUNS: EntryClass.DERIVED_FACT,
    Section.CANDIDATES: EntryClass.DERIVED_FACT,
    Section.REVIEWS: EntryClass.DERIVED_FACT,
    Section.BLOCKERS: EntryClass.DERIVED_FACT,
    Section.DECISIONS: EntryClass.OPERATOR_DECISION,
    Section.BACKGROUND: EntryClass.ADVISORY_CONTEXT,
    Section.DO_NOT_TOUCH: EntryClass.ADVISORY_CONTEXT,
    Section.CONTEXT: EntryClass.ADVISORY_CONTEXT,
}

#: Which derivers a derived section accepts: a register or a review is a file
#: digest, the runs are a run set, a candidate is a ref's state.
SECTION_DERIVERS: dict[Section, frozenset[str]] = {
    Section.REGISTER: frozenset({"file_digest"}),
    Section.RUNS: frozenset({"run_set"}),
    Section.CANDIDATES: frozenset({"ref_state"}),
    Section.REVIEWS: frozenset({"file_digest"}),
    Section.BLOCKERS: frozenset({"file_digest", "run_set", "ref_state"}),
}

DERIVER_PARAMS: dict[str, frozenset[str]] = {
    "file_digest": frozenset({"path"}),
    "run_set": frozenset({"runs_root"}),
    "ref_state": frozenset({"repo", "ref"}),
}

DECISION_STATUS = ("open", "decided")


class Entry(Strict):
    """One entry of a capsule. Frozen, and *not* whitespace-stripped: an
    operator's words keep their trailing newline."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    entry_id: str
    klass: EntryClass
    section: Section
    deriver: str | None = None
    params: dict[str, Any] | None = None
    value: Any = None
    text: str | None = None
    source: str | None = None
    at: str | None = None
    status: Literal["open", "decided"] | None = None
    text_sha256: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Entry":
        if not self.entry_id or self.entry_id != self.entry_id.strip():
            raise ValueError("entry_id must be non-empty and carry no edge whitespace")
        wanted = SECTION_CLASS[self.section]
        if self.klass is not wanted:
            raise ValueError(f"section {self.section.value} holds {wanted.value} "
                             f"entries, not {self.klass.value}")
        derived = {"deriver": self.deriver, "params": self.params, "value": self.value}
        decision = {"text": self.text, "source": self.source, "at": self.at,
                    "status": self.status, "text_sha256": self.text_sha256}
        if self.klass is EntryClass.DERIVED_FACT:
            if not self.deriver or self.deriver not in DERIVER_PARAMS:
                raise ValueError(f"unknown deriver {self.deriver!r}")
            if self.deriver not in SECTION_DERIVERS[self.section]:
                raise ValueError(f"section {self.section.value} does not take the "
                                 f"deriver {self.deriver!r}")
            if self.params is None or set(self.params) != set(DERIVER_PARAMS[self.deriver]):
                raise ValueError(f"deriver {self.deriver} takes exactly the params "
                                 f"{sorted(DERIVER_PARAMS[self.deriver])}")
            if self.value is None:
                raise ValueError("a derived fact has a value; the product fills it")
            if any(v is not None for v in decision.values()):
                raise ValueError("a derived fact carries no decision fields")
        elif self.klass is EntryClass.OPERATOR_DECISION:
            if not isinstance(self.text, str) or not self.text.strip():
                raise ValueError("an operator decision carries its text")
            if not self.source or not self.at:
                raise ValueError("an operator decision names its source and its time")
            if self.status not in DECISION_STATUS:
                raise ValueError("an operator decision is open or decided")
            if not self.text_sha256:
                raise ValueError("an operator decision carries the digest of its text")
            if any(v is not None for v in derived.values()):
                raise ValueError("an operator decision carries no derived fields")
        else:
            if not isinstance(self.text, str) or not self.text.strip():
                raise ValueError("advisory context carries text")
            others = {k: v for k, v in {**derived, **decision}.items() if k != "text"}
            if any(v is not None for v in others.values()):
                raise ValueError("advisory context carries text and nothing else")
        return self


class Capsule(Strict):
    """The frozen document. `capsule_digest` is sha256 over the canonical JSON
    of everything else, advisory context included."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: str = SCHEMA
    capsule_id: str
    project_id: str
    programme_generation: int
    predecessor_session: str
    predecessor_epoch: int
    predecessor_token_sha256: str
    successor_session: str
    successor_epoch: int
    successor_model: str
    repo_path: str
    branch: str
    head: str
    created_at: str
    ack_deadline: str
    entries: tuple[Entry, ...]
    capsule_digest: str = ""

    @field_validator("capsule_id")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        return check_capsule_id(value)

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"capsule_digest"})

    def compute_digest(self) -> str:
        return sha256_of(self.body())

    def facts(self) -> list[Entry]:
        return [e for e in self.entries if e.klass is EntryClass.DERIVED_FACT]

    def section_digests(self) -> dict[str, str]:
        return section_digests(self.entries)


# --------------------------------------------------------------------------- #
# what the author supplies
# --------------------------------------------------------------------------- #


class DeriveIn(Strict):
    """What to derive -- never a value."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    entry_id: str
    section: Section
    deriver: str
    params: dict[str, Any]


class DecisionIn(Strict):
    """An operator decision, copied. Whitespace is part of the words."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    entry_id: str
    text: str
    source: str
    at: str
    status: Literal["open", "decided"] = "open"


class AdvisoryIn(Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    entry_id: str
    section: Section
    text: str


class CapsuleInput(Strict):
    """Everything the author may say. There is no programme generation here and
    none can be added: `extra="forbid"` refuses it, because the generation is
    the lease's, read at freezing, and a capsule that let its author pick one
    would carry a number nothing checks."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    successor_model: str
    derive: tuple[DeriveIn, ...]
    decisions: tuple[DecisionIn, ...] = ()
    advisory: tuple[AdvisoryIn, ...] = ()


# --------------------------------------------------------------------------- #
# the observer seam and its derivers
# --------------------------------------------------------------------------- #


class Observer(Protocol):
    """How the product looks at the world. `LocalObserver` is the real one;
    tests substitute a fake. Every method is read-only."""

    def file_digest(self, path: str) -> str: ...
    def run_set(self, runs_root: str) -> list[dict[str, Any]]: ...
    def ref_state(self, repo: str, ref: str) -> dict[str, Any]: ...
    def head(self, repo: str) -> str: ...
    def branch(self, repo: str) -> str: ...


def derive(observer: Observer, deriver: str, params: Mapping[str, Any]) -> Any:
    """Run one named deriver. Unknown deriver or params are refused, not
    guessed; the value is checked to be JSON before it is believed."""
    if deriver not in DERIVER_PARAMS:
        raise CapsuleError(f"unknown deriver {deriver!r}")
    if set(params) != set(DERIVER_PARAMS[deriver]):
        raise CapsuleError(f"deriver {deriver} takes exactly the params "
                           f"{sorted(DERIVER_PARAMS[deriver])}, got {sorted(params)}")
    value = getattr(observer, deriver)(**{k: params[k] for k in DERIVER_PARAMS[deriver]})
    canonical(value)                       # refuses anything that is not JSON
    if value is None:
        raise CapsuleError(f"deriver {deriver} produced nothing")
    return json.loads(canonical(value))


class LocalObserver:
    """The real observer: files, run directories and *read-only* git.

    `git` is run through `_git`, which refuses any subcommand outside
    `GIT_READ_ONLY`, so validating a capsule cannot write to the integration
    checkout. Run states are read as raw JSON, never through `RunStore`, whose
    `read_state` may migrate (and so write) an old state.
    """

    def __init__(self, *, git_timeout: float = 30.0,
                 run: Callable[..., Any] | None = None) -> None:
        self.git_timeout = git_timeout
        self._run = run or subprocess.run

    def _git(self, repo: str, *args: str) -> Any:
        if not args or args[0] not in GIT_READ_ONLY:
            raise CapsuleError(f"git {args[0] if args else '(nothing)'} is not on the "
                               f"read-only whitelist {sorted(GIT_READ_ONLY)}")
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        return self._run(["git", "-C", str(repo), *args], capture_output=True,
                         text=True, timeout=self.git_timeout, env=env, check=False)

    def head(self, repo: str) -> str:
        proc = self._git(repo, "rev-parse", "--verify", "HEAD")
        if proc.returncode != 0:
            raise CapsuleError(f"cannot read HEAD of {repo}: {proc.stderr.strip()[:200]}")
        return proc.stdout.strip()

    def branch(self, repo: str) -> str:
        proc = self._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if proc.returncode != 0:
            raise CapsuleError(f"cannot read the branch of {repo}: "
                               f"{proc.stderr.strip()[:200]}")
        return proc.stdout.strip()

    def file_digest(self, path: str) -> str:
        p = Path(path)
        if not p.is_file():
            return MISSING
        return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()

    def ref_state(self, repo: str, ref: str) -> dict[str, Any]:
        if not ref or ref.startswith("-"):
            raise CapsuleError(f"not a ref: {ref!r}")
        found = self._git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if found.returncode != 0:
            return {"sha": MISSING, "merged": False}
        sha = found.stdout.strip()
        # "Merged" is: reachable from this repository's HEAD. An accepted but
        # unmerged candidate is `merged: false`.
        ancestor = self._git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
        if ancestor.returncode not in (0, 1):
            raise CapsuleError(f"cannot tell whether {ref} is merged: "
                               f"{ancestor.stderr.strip()[:200]}")
        return {"sha": sha, "merged": ancestor.returncode == 0}

    def run_set(self, runs_root: str) -> list[dict[str, Any]]:
        root = Path(runs_root)
        out: list[dict[str, Any]] = []
        if not root.is_dir():
            return out
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            state_file = entry / "state.json"
            if not state_file.is_file():
                continue
            try:
                raw = json.loads(state_file.read_text(encoding="utf-8"))
                condition = str(raw.get("condition", ""))
                if condition not in ("ACTIVE", "PAUSED", "BLOCKED"):
                    continue
                cand = raw.get("working_candidate") or raw.get("last_accepted_candidate")
                out.append({
                    "run_id": str(raw.get("run_id", entry.name)),
                    "stage": str(raw.get("stage", "")),
                    "condition": condition,
                    "iteration": raw.get("iteration"),
                    "attempt": raw.get("attempt"),
                    "retry_after": raw.get("retry_after"),
                    "candidate": (cand.get("candidate_id") if isinstance(cand, dict)
                                  else None),
                })
            except (OSError, ValueError, AttributeError) as exc:
                # An unreadable run is part of the picture, not a gap in it.
                out.append({"run_id": entry.name, "stage": "", "condition": "UNREADABLE",
                            "iteration": None, "attempt": None, "retry_after": None,
                            "candidate": None, "why": str(exc)[:200]})
        return out


# --------------------------------------------------------------------------- #
# section digests
# --------------------------------------------------------------------------- #


def section_digests(entries: Iterable[Entry], *,
                    values: Mapping[str, Any] | None = None) -> dict[str, str]:
    """A digest per section that holds a derived fact or a decision.

    `values` substitutes observed values for the stored ones (the successor's
    re-derivation). A decision contributes the digest of its *text as it is*,
    recomputed, so a changed text changes its section. Advisory context is in
    no section digest at all.
    """
    grouped: dict[str, list[Any]] = {}
    for e in entries:
        if e.klass is EntryClass.ADVISORY_CONTEXT:
            continue
        if e.klass is EntryClass.DERIVED_FACT:
            value = e.value if values is None or e.entry_id not in values else values[e.entry_id]
            item = [e.entry_id, "fact", e.deriver, e.params, value]
        else:
            item = [e.entry_id, "decision", sha256_text(e.text or ""), e.status,
                    e.source, e.at]
        grouped.setdefault(e.section.value, []).append(item)
    return {name: sha256_of(sorted(items, key=lambda it: it[0]))
            for name, items in sorted(grouped.items())}


def structure_problems(entries: Iterable[Entry]) -> list[str]:
    """What a capsule must have: unique entry ids, a run set in RUNS (an empty
    set is a fact, a missing one is not) and at least one REGISTER entry."""
    entries = list(entries)
    problems: list[str] = []
    seen: set[str] = set()
    for e in entries:
        if e.entry_id in seen:
            problems.append(f"duplicate entry id {e.entry_id!r}")
        seen.add(e.entry_id)
    if not any(e.section is Section.RUNS and e.deriver == "run_set" for e in entries):
        problems.append("no run_set is bound into RUNS (an empty run set is a fact; "
                        "an absent one is not)")
    if not any(e.section is Section.REGISTER for e in entries):
        problems.append("no REGISTER entry: a capsule must bind at least one source "
                        "of the programme's record")
    return problems


# --------------------------------------------------------------------------- #
# freezing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FrozenCapsule:
    capsule: Capsule
    path: Path
    file_sha256: str


def freeze_capsule(authority: MasterAuthority, inp: CapsuleInput, *,
                   directory: Path | str, observer: Observer, repo: str,
                   project_id: str, successor_session: str,
                   generation: int | None = None, ack_window_s: int = 600,
                   capsule_id: str | None = None) -> FrozenCapsule:
    """Derive, bind and write the capsule. Only the live master freezes.

    The derived values come from `observer`; the programme generation comes
    from the lease. `generation`, when the caller knows the project state's
    own, must equal it -- a disagreement means the state and the lease are not
    describing the same programme, and no capsule is written for that.
    """
    authority.check()                         # StaleMaster: only the live master freezes
    lease: Lease | None = authority.read_lease()
    if lease is None or authority.epoch is None:
        raise CapsuleError("the master holds no readable lease; nothing to bind")
    if generation is not None and generation != lease.program_generation:
        raise CapsuleError(f"the project state is at programme generation {generation} "
                           f"but the lease is at {lease.program_generation}: refusing "
                           f"to freeze a capsule for a programme that is unclear")
    successor_session = str(successor_session).strip()
    if not successor_session or successor_session == authority.session_id:
        raise CapsuleError("a capsule names a successor session other than the master")
    if ack_window_s <= 0:
        raise CapsuleError("the acknowledgement window must be positive")

    entries: list[Entry] = []
    for d in inp.derive:
        entries.append(Entry(entry_id=d.entry_id, klass=EntryClass.DERIVED_FACT,
                             section=d.section, deriver=d.deriver, params=dict(d.params),
                             value=derive(observer, d.deriver, d.params)))
    for dec in inp.decisions:
        entries.append(Entry(entry_id=dec.entry_id, klass=EntryClass.OPERATOR_DECISION,
                             section=Section.DECISIONS, text=dec.text, source=dec.source,
                             at=dec.at, status=dec.status,
                             text_sha256=sha256_text(dec.text)))
    for adv in inp.advisory:
        entries.append(Entry(entry_id=adv.entry_id, klass=EntryClass.ADVISORY_CONTEXT,
                             section=adv.section, text=adv.text))
    problems = structure_problems(entries)
    if problems:
        raise CapsuleError("not frozen: " + "; ".join(problems))

    now = authority.clock()
    cid = check_capsule_id(capsule_id or new_capsule_id(now))
    cap = Capsule(
        capsule_id=cid,
        project_id=project_id,
        programme_generation=lease.program_generation,
        predecessor_session=authority.session_id,
        predecessor_epoch=int(authority.epoch),
        predecessor_token_sha256=lease.token_sha256,
        successor_session=successor_session,
        successor_epoch=int(authority.epoch) + 1,
        successor_model=inp.successor_model,
        repo_path=str(repo),
        branch=observer.branch(repo),
        head=observer.head(repo),
        created_at=now,
        ack_deadline=plus_seconds(now, ack_window_s),
        entries=tuple(entries),
    )
    cap = cap.model_copy(update={"capsule_digest": cap.compute_digest()})
    payload = cap.model_dump_json(indent=2) + "\n"
    target = capsule_path(directory, cid)
    target.parent.mkdir(parents=True, exist_ok=True)
    durable_create(target, payload, mode=0o600)
    return FrozenCapsule(capsule=cap, path=target,
                         file_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest())


def load_capsule(path: Path | str) -> Capsule:
    """Read a capsule file. Does not check its digest -- `verify_capsule` does,
    and names the difference; a tampered capsule must load to be refused."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        return Capsule.model_validate_json(text)
    except (OSError, ValueError, ValidationError) as exc:
        raise CapsuleError(f"{path} is not a readable succession capsule: {exc}") from exc


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Difference:
    kind: str          # schema digest pointer successor epoch deadline chain lease
                       # head branch fact decision structure generation freshness
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}[{self.subject}]: {self.detail}"


@dataclass
class Verification:
    ok: bool
    differences: list[Difference] = field(default_factory=list)
    observed_head: str | None = None
    observed_branch: str | None = None
    observed_sections: dict[str, str] = field(default_factory=dict)
    freshness: list[str] = field(default_factory=list)
    programme_generation: int | None = None
    lease_generation: int | None = None
    project_generation: int | None = None

    def kinds(self) -> set[str]:
        return {d.kind for d in self.differences}

    def subjects(self) -> set[str]:
        return {d.subject for d in self.differences}

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok,
                "differences": [{"kind": d.kind, "subject": d.subject, "detail": d.detail}
                                for d in self.differences],
                "observed_head": self.observed_head,
                "observed_branch": self.observed_branch,
                "observed_sections": self.observed_sections,
                "freshness": self.freshness,
                "programme_generation": self.programme_generation,
                "lease_generation": self.lease_generation,
                "project_generation": self.project_generation}


def _parse_time(ts: str) -> datetime:
    return datetime.strptime(ts, _TIME_FORMAT).replace(tzinfo=UTC)


def _freshness_capsule(cap: Capsule) -> hoh_capsule.ContextCapsule | None:
    """The capsule's derived facts as P2-20 sources, so the existing freshness
    rule (`may_dispatch`, `stale_sources`) judges them. None when the capsule
    has no derived fact to bind (then `structure` already reported it)."""
    items = []
    for e in cap.facts():
        items.append(hoh_capsule.CapsuleItem(
            item_id=e.entry_id, layer=hoh_capsule.Layer.COMPACT,
            title=f"{e.section.value} {e.entry_id}",
            gist=f"{e.section.value}: {e.deriver}",
            source=hoh_capsule.SourceRef(
                source_id=e.entry_id, kind=str(e.deriver),
                digest=sha256_of(e.value), generation=cap.programme_generation),
            cost=0))
    if not items:
        return None
    return hoh_capsule.assemble(
        items, program_id=cap.project_id, generation=cap.programme_generation,
        role=hoh_capsule.Role.PLANNER, token_budget=0, policy_version=SCHEMA,
        assembled_at=_parse_time(cap.created_at))


def verify_capsule(cap: Capsule, *, probe: MasterAuthority, observer: Observer,
                   session: str | None = None, pointer_digest: str | None = None,
                   handover: bool = False,
                   project_generation: int | None = None) -> Verification:
    """Re-derive everything and report **every** difference.

    Read-only: `probe` is used only for `read_lease` / `epochs`, nothing is
    acquired, renewed or written. `ok` only when nothing differs.

    `handover=True` is the successor's second look, after the predecessor has
    handed over: the lease must then be *released to this session* (same token
    digest, same predecessor) instead of live, and the acknowledgement
    deadline no longer applies. Every other check is unchanged.
    """
    bad: list[Difference] = []
    now = probe.clock()

    if cap.schema_version != SCHEMA:
        bad.append(Difference("schema", "capsule", f"schema {cap.schema_version!r}, "
                                                   f"expected {SCHEMA!r}"))
    if cap.capsule_digest != cap.compute_digest():
        bad.append(Difference("digest", "capsule",
                              "the capsule's digest does not match its content: it "
                              "was changed after it was frozen"))
    if pointer_digest is not None and pointer_digest != cap.capsule_digest:
        bad.append(Difference("pointer", "capsule",
                              f"the pointer named digest {pointer_digest[:16]}..., the "
                              f"capsule is {cap.capsule_digest[:16]}..."))
    if session is not None and session != cap.successor_session:
        bad.append(Difference("successor", session,
                              f"the capsule names {cap.successor_session!r} as its "
                              f"successor, not {session!r}"))
    if cap.successor_epoch != cap.predecessor_epoch + 1:
        bad.append(Difference("epoch", "successor_epoch",
                              f"the intended epoch {cap.successor_epoch} is not the "
                              f"predecessor's {cap.predecessor_epoch} + 1"))
    if not handover and now > cap.ack_deadline:
        bad.append(Difference("deadline", "ack_deadline",
                              f"the acknowledgement was due by {cap.ack_deadline}, "
                              f"it is {now}"))
    for problem in structure_problems(cap.entries):
        bad.append(Difference("structure", "entries", problem))

    # --- the epoch chain and the lease ------------------------------------
    lease: Lease | None = None
    try:
        current = probe.epochs.current()
        lease = probe.read_lease()
    except Exception as exc:                                # noqa: BLE001 - reported
        current = None
        bad.append(Difference("chain", "epochs", f"authority is unreadable: {exc}"))
    else:
        if current is None:
            bad.append(Difference("chain", "epochs", "no epoch has begun"))
        else:
            if current.epoch != cap.predecessor_epoch:
                bad.append(Difference("chain", "epoch",
                                      f"the current epoch is {current.epoch} (held by "
                                      f"{current.master_id!r}), the capsule was frozen at "
                                      f"{cap.predecessor_epoch}: it belongs to another "
                                      f"hand-over"))
            elif current.master_id != cap.predecessor_session:
                bad.append(Difference("chain", "master",
                                      f"epoch {current.epoch} is held by "
                                      f"{current.master_id!r}, not by "
                                      f"{cap.predecessor_session!r}"))
        if lease is None:
            bad.append(Difference("lease", "lease", "there is no lease"))
        else:
            if lease.token_sha256 != cap.predecessor_token_sha256:
                bad.append(Difference("lease", "token",
                                      "the lease's token digest is not the one this "
                                      "capsule was frozen against: another hand-over"))
            if lease.leader_session_id != cap.predecessor_session \
                    or lease.master_epoch != cap.predecessor_epoch:
                bad.append(Difference("lease", "holder",
                                      f"the lease names {lease.leader_session_id!r} at "
                                      f"epoch {lease.master_epoch}, the capsule "
                                      f"{cap.predecessor_session!r} at "
                                      f"{cap.predecessor_epoch}"))
            if handover:
                if not lease.released_at:
                    bad.append(Difference("lease", "released",
                                          "the predecessor has not handed over"))
                elif lease.successor != cap.successor_session:
                    bad.append(Difference("lease", "successor",
                                          f"the lease was handed to {lease.successor!r}, "
                                          f"not to {cap.successor_session!r}"))
                if lease.expires_at <= now:
                    bad.append(Difference("lease", "handover",
                                          f"the handover lapsed at {lease.expires_at}"))
            else:
                if lease.released_at:
                    bad.append(Difference("lease", "released",
                                          f"the lease was released at {lease.released_at}"))
                else:
                    state, why = probe.holder_state(lease, now)
                    if state != "LIVE":
                        bad.append(Difference("lease", "live", f"{state}: {why}"))

    # --- where the repository stands ---------------------------------------
    observed_head: str | None = None
    observed_branch: str | None = None
    try:
        observed_head = observer.head(cap.repo_path)
        if observed_head != cap.head:
            bad.append(Difference("head", cap.repo_path,
                                  f"HEAD moved: frozen at {cap.head}, now {observed_head}"))
    except Exception as exc:                                # noqa: BLE001 - reported
        bad.append(Difference("head", cap.repo_path, f"cannot read HEAD: {exc}"))
    try:
        observed_branch = observer.branch(cap.repo_path)
        if observed_branch != cap.branch:
            bad.append(Difference("branch", cap.repo_path,
                                  f"branch moved: frozen on {cap.branch!r}, now on "
                                  f"{observed_branch!r}"))
    except Exception as exc:                                # noqa: BLE001 - reported
        bad.append(Difference("branch", cap.repo_path, f"cannot read the branch: {exc}"))

    # --- every derived fact, derived again ---------------------------------
    observed: dict[str, Any] = {}
    for e in cap.facts():
        try:
            observed[e.entry_id] = derive(observer, e.deriver, e.params)
        except Exception as exc:                            # noqa: BLE001 - reported
            bad.append(Difference("fact", e.entry_id,
                                  f"{e.section.value}: cannot derive {e.deriver}: {exc}"))
            continue
        if canonical(observed[e.entry_id]) != canonical(e.value):
            bad.append(Difference("fact", e.entry_id,
                                  f"{e.section.value}: {e.deriver} {e.params} was "
                                  f"{canonical(e.value)[:200]}, is "
                                  f"{canonical(observed[e.entry_id])[:200]}"))

    # --- the operator's words ----------------------------------------------
    for e in cap.entries:
        if e.klass is EntryClass.OPERATOR_DECISION and sha256_text(e.text or "") != e.text_sha256:
            bad.append(Difference("decision", e.entry_id,
                                  "the decision's text no longer matches its digest"))

    # --- the programme generation, derived from the lease -------------------
    lease_generation = lease.program_generation if lease is not None else None
    current_generation = lease_generation
    if current_generation is None or cap.programme_generation != current_generation:
        bad.append(Difference("generation", "programme_generation",
                              f"the capsule is for programme generation "
                              f"{cap.programme_generation}, the lease is at "
                              f"{lease_generation}"))
    if project_generation is not None and project_generation != cap.programme_generation:
        bad.append(Difference("generation", "project_generation",
                              f"the project state is at programme generation "
                              f"{project_generation}, the capsule at "
                              f"{cap.programme_generation}"))

    # --- P2-20's freshness rule ----------------------------------------------
    stale: list[str] = []
    try:
        fresh = _freshness_capsule(cap)
        if fresh is not None:
            seen_now = {i: (sha256_of(v), cap.programme_generation)
                        for i, v in observed.items()}
            # The generation handed to the rule is the *derived* one -- the
            # lease's -- never the capsule's own claim about itself.
            reasons = hoh_capsule.may_dispatch(
                fresh, current=seen_now,
                program_generation=current_generation if current_generation is not None else -1)
            stale = hoh_capsule.stale_sources(fresh, current=seen_now)
            for reason in reasons:
                named = re.match(r"source '(.+?)' ", reason)
                bad.append(Difference("freshness",
                                      named.group(1) if named else "programme_generation",
                                      reason))
    except Exception as exc:                                # noqa: BLE001 - reported
        bad.append(Difference("freshness", "capsule", f"cannot judge freshness: {exc}"))

    return Verification(ok=not bad,
                        differences=bad,
                        observed_head=observed_head,
                        observed_branch=observed_branch,
                        observed_sections=section_digests(cap.entries, values=observed),
                        freshness=stale,
                        programme_generation=cap.programme_generation,
                        lease_generation=lease_generation,
                        project_generation=project_generation)


# --------------------------------------------------------------------------- #
# the acknowledgement and the receipt
# --------------------------------------------------------------------------- #


def _with_digest(body: dict[str, Any], key: str) -> dict[str, Any]:
    return {**body, key: sha256_of(body)}


def build_ack(cap: Capsule, ver: Verification, *, at: str) -> dict[str, Any]:
    """What the successor observed, bound to the capsule it looked at. Written
    also when verification failed, so the predecessor sees the refusal at once."""
    body = {
        "schema_version": ACK_SCHEMA,
        "capsule_id": cap.capsule_id,
        "capsule_digest": cap.capsule_digest,
        "verified": bool(ver.ok),
        "mismatches": [str(d) for d in ver.differences],
        "predecessor_session": cap.predecessor_session,
        "predecessor_epoch": cap.predecessor_epoch,
        "successor_session": cap.successor_session,
        "successor_epoch": cap.successor_epoch,
        "observed_head": ver.observed_head,
        "observed_branch": ver.observed_branch,
        "section_digests": dict(ver.observed_sections),
        "at": at,
    }
    return _with_digest(body, "ack_digest")


def ack_problems(cap: Capsule, ack: Any) -> list[str]:
    """Every way `ack` is not the acknowledgement of exactly this frozen
    capsule. The predecessor trusts nothing the successor wrote that it can
    compare with what it froze itself."""
    if not isinstance(ack, dict):
        return ["the acknowledgement is not a JSON object"]
    problems: list[str] = []
    if ack.get("schema_version") != ACK_SCHEMA:
        problems.append(f"acknowledgement schema {ack.get('schema_version')!r}")
    body = {k: v for k, v in ack.items() if k != "ack_digest"}
    try:
        if ack.get("ack_digest") != sha256_of(body):
            problems.append("the acknowledgement's own digest does not match its content")
    except CapsuleError as exc:
        problems.append(f"the acknowledgement is not canonical JSON: {exc}")
    if ack.get("verified") is not True:
        problems.append("the successor did not verify the capsule"
                        + (f": {'; '.join(map(str, ack.get('mismatches') or []))[:300]}"
                           if ack.get("mismatches") else ""))
    elif ack.get("mismatches"):
        problems.append("the acknowledgement says verified and lists mismatches")
    expected = {
        "capsule_id": cap.capsule_id, "capsule_digest": cap.capsule_digest,
        "predecessor_session": cap.predecessor_session,
        "predecessor_epoch": cap.predecessor_epoch,
        "successor_session": cap.successor_session,
        "successor_epoch": cap.successor_epoch,
        "observed_head": cap.head, "observed_branch": cap.branch,
    }
    for key, want in expected.items():
        if ack.get(key) != want:
            problems.append(f"acknowledgement {key} is {ack.get(key)!r}, the frozen "
                            f"capsule has {want!r}")
    if ack.get("section_digests") != cap.section_digests():
        problems.append("acknowledgement section_digests (what the successor observed) "
                        "are not the frozen ones")
    return problems


def build_receipt(cap: Capsule, ack: Mapping[str, Any], *, lease: Lease,
                  at: str) -> dict[str, Any]:
    """The record that the role moved: the capsule, the acknowledgement, both
    sessions and epochs, and the new lease's token digest and start."""
    body = {
        "schema_version": RECEIPT_SCHEMA,
        "capsule_id": cap.capsule_id,
        "capsule_digest": cap.capsule_digest,
        "ack_digest": ack.get("ack_digest"),
        "predecessor_session": cap.predecessor_session,
        "predecessor_epoch": cap.predecessor_epoch,
        "successor_session": cap.successor_session,
        "successor_epoch": lease.master_epoch,
        "lease_token_sha256": lease.token_sha256,
        "lease_acquired_at": lease.acquired_at,
        "at": at,
    }
    return _with_digest(body, "receipt_digest")
