#!/usr/bin/env python3
"""The whole programme, read out of the plan rather than out of the register.

O196. The requirement register was tested for completeness by asserting that
it contained phases P1, P2 and P3 -- which the register itself supplied. A
register that silently dropped P4 through P12 would have passed that test, and
because the capability matrix, the gap register, the build order and the
checklist are all derived from the register, every one of them would have
stayed internally consistent and green while describing a quarter of the work.

So the expected side is computed from somewhere else: the pinned production
line itself, parsed from its bytes, bound to its digest. This tool does not
read `REQUIREMENTS.json` to build the inventory. It reads it only to compare.

What the parser does, and refuses to do:

* **Every line of the plan that says something becomes an item.** Bullets,
  numbered steps, lines inside fenced blocks, and prose paragraphs -- P11 is
  written entirely in prose, and a parser that read only bullets would make a
  whole capability phase disappear, which is the failure this exists to catch.
* **Every item gets a kind from the section it sits in**, through one table.
  A section the table does not know is `UNCLASSIFIED`, and the gate fails on
  it. Silence is not a classification.
* **Prose is a constraint unless the table says otherwise**, and the few
  places it says otherwise are listed by item id with the reason.

Usage:
    python3 tools/program_scope.py --out program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json
    python3 tools/program_scope.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
PLAN_ENV = "VERIHARNESS_PLAN_ROOT"
PLAN_FILE = ("VERIHARNESS_CLI_MASTER_ONBOARDING_V3_3_PARALLELISM_FINAL_2026-09-21/"
             "02_FINAL_PRODUCTION_LINE_V3_3.md")
#: The package's machine-readable summary. Its `new_in_v3_3` list names the
#: V3.3 contracts; several of them are not a line of the production line, so
#: they are checked as *contracts carried by some requirement* rather than as
#: items -- a second independent parse of the pinned plan.
MASTER_FILE = ("VERIHARNESS_CLI_MASTER_ONBOARDING_V3_3_PARALLELISM_FINAL_2026-09-21/"
               "MASTER_PLAN_V3_3.json")
SCOPE = HOH / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"
REGISTER = HOH / "program/v3_3/REQUIREMENTS.json"
#: Operator amendments to the production line (A1: 2026-09-30, the storage
#: lifecycle). They live in this repository, so unlike the plan they are always
#: reachable; each is pinned by digest in the index, together with the
#: operator's directive that authorises it. See `load_amendments`.
AMENDMENTS_DIR = HOH / "program/v3_3/amendments"
AMENDMENTS_INDEX_NAME = "AMENDMENTS.json"
AMENDMENTS_OUT = HOH / "program/v3_3/PROGRAMME_AMENDMENTS.json"

#: Section label (lower case, markdown stripped, trailing colon removed) -> kind.
#: This is the one authored judgement in the inventory, and it classifies; it
#: never adds or removes an item.
LABEL_KINDS: dict[str, str] = {
    "deliver": "baseline_deliverable",
    "measure": "measurement",
    "build/verify": "capability",
    "core": "capability",
    "build": "capability",
    "formalize": "capability",
    "do": "capability",
    "product": "product_capability",
    "product requirement": "acceptance_path",
    "system-one": "optional_capability",
    "autonomy": "autonomy_capability",
    "checklist gate": "gate",
    "exit includes": "gate",
    "required invariant": "invariant",
    "build a thin read-only mission control, not the full ui": "ui_capability",
    "purpose": "rationale",
    "run endurance": "validation",
    "must include": "validation",
    "at least one real self-build must demonstrate": "validation",
    "at least one bounded reference program must demonstrate": "validation",
    "at least one real program": "validation",
    "core 1.0 must define stable interfaces for": "release_interface",
    "core 1.0 must be": "release_property",
    "must pass with": "release_gate",
    "product modes": "ui_capability",
    "needs you": "ui_capability",
    "veriharness owns semantics": "architecture",
    # Things to build, not a statement about ownership: each backend named
    # here is an implementation P12 asks for.
    "execution persistence backend should be pluggable, e.g.": "capability",
}

#: Kinds that name something to be built. Each item of one of these kinds must
#: map to exactly one requirement in the register; everything else must only
#: be classified.
CAPABILITY_KINDS = frozenset({
    "capability", "product_capability", "optional_capability",
    "autonomy_capability", "ui_capability", "release_interface",
})

#: Prose paragraphs that are capabilities rather than constraints. Keyed by the
#: first words of the paragraph so the override survives re-parsing, and each
#: says why -- a prose override is exactly the kind of quiet decision that
#: needs to be readable.
PROSE_KINDS: dict[str, tuple[str, str]] = {
    "promote only specific low-risk decision classes": (
        "optional_capability",
        "P11 is written entirely in prose; this sentence is its capability"),
    "core 1.0 must natively execute safe project-level parallel waves": (
        "release_property",
        "the native-parallelism claim Core 1.0 rests on"),
}


@dataclass
class Item:
    item_id: str
    phase: str
    version: str | None
    section: str
    kind: str
    text: str
    line: int

    def as_dict(self) -> dict:
        return {"id": self.item_id, "phase": self.phase, "version": self.version,
                "section": self.section, "kind": self.kind, "text": self.text,
                "line": self.line,
                "capability": self.kind in CAPABILITY_KINDS}


def plan_path(root: Path | None = None) -> Path | None:
    base = root or (Path(os.environ[PLAN_ENV]) if os.environ.get(PLAN_ENV) else None)
    if base is None:
        return None
    p = base / PLAN_FILE
    return p if p.is_file() else None


def _clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _label_key(text: str) -> str:
    return _clean(text).rstrip(":").strip().lower()


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", _clean(text).lower())
    return "-".join(words)[:60] or "item"


_PHASE = re.compile(r"^# (P\d+) — (.*)$")
_VERSION = re.compile(r"\bv?(\d+\.\d+)\b")


def parse(text: str) -> tuple[list[dict], list[Item]]:
    """(phases, items) from the plan's own text."""
    phases: list[dict] = []
    items: list[Item] = []
    phase = version = None
    section = "(none)"
    fence = False
    prose: list[tuple[int, str]] = []
    seen: dict[str, int] = {}

    def add(kind: str, raw: str, line: int) -> None:
        if phase is None:
            return
        base = f"{phase}.{_slug(section)[:24]}.{_slug(raw)}"
        seen[base] = seen.get(base, 0) + 1
        item_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        # The same line twice in one section is not two items; it is a copy
        # somebody did not notice. Kept with a suffix so nothing is lost, and
        # marked so the gate refuses it rather than counting it twice. The
        # same text in a *different* section has a different id and is not a
        # duplicate: "Product:" appears in P4 and P5 and means two things.
        kind_ = "DUPLICATE" if seen[base] > 1 else kind
        items.append(Item(item_id, phase, version, section, kind_, _clean(raw), line))

    def flush_prose() -> None:
        if not prose:
            return
        line, raw = prose[0][0], " ".join(t for _, t in prose)
        key = next((k for k in PROSE_KINDS if _clean(raw).lower().startswith(k)), None)
        kind = PROSE_KINDS[key][0] if key else (
            LABEL_KINDS.get(_label_key(section), "constraint")
            if section != "(none)" and _label_key(section) == "purpose" else "constraint")
        add(kind, raw, line)
        prose.clear()

    for n, raw_line in enumerate(text.splitlines(), 1):
        s = raw_line.strip()
        if s.startswith("```"):
            flush_prose()
            fence = not fence
            continue
        if not s or s == "---":
            flush_prose()
            continue
        m = _PHASE.match(s)
        if m:
            flush_prose()
            phase, title = m.group(1), m.group(2)
            vs = [v for v in _VERSION.findall(title)]
            version = ("/".join(vs) if vs else
                       ("1.0" if "Core 1.0" in title else None))
            section = "(none)"
            phases.append({"phase": phase, "title": _clean(title),
                           "version": version, "line": n})
            continue
        if phase is None:
            continue
        if s.startswith("## "):
            flush_prose()
            section = s[3:].strip()
            continue
        if fence:
            if re.fullmatch(r"[-> |]+", s):
                continue            # a bare arrow joins two steps; it says nothing
            kind = LABEL_KINDS.get(_label_key(section), "UNCLASSIFIED")
            add(kind, s, n)
            continue
        if re.match(r"^(- |\d+\. )", s):
            flush_prose()
            body = re.sub(r"^(- |\d+\. )", "", s)
            add(LABEL_KINDS.get(_label_key(section), "UNCLASSIFIED"), body, n)
            continue
        if s.endswith(":"):
            flush_prose()
            section = s
            continue
        prose.append((n, s))
    flush_prose()
    return phases, items


def build(plan: Path) -> dict:
    data = plan.read_bytes()
    phases, items = parse(data.decode("utf-8"))
    by_phase: dict[str, dict] = {p["phase"]: {**p, "items": 0, "capabilities": 0}
                                 for p in phases}
    for it in items:
        by_phase[it.phase]["items"] += 1
        if it.kind in CAPABILITY_KINDS:
            by_phase[it.phase]["capabilities"] += 1
    for p in by_phase.values():
        p["phase_kind"] = "capability" if p["capabilities"] else (
            "baseline" if p["phase"] == "P0" else "non-capability")
    return {
        "what": ("The authoritative programme inventory, parsed from the pinned "
                 "production line. The register is compared against this; it "
                 "is never the source of it."),
        "plan_file": PLAN_FILE,
        "plan_sha256": hashlib.sha256(data).hexdigest(),
        "phases": list(by_phase.values()),
        "items": [it.as_dict() for it in items],
        "counts": {
            "phases": len(phases), "items": len(items),
            "capability_items": sum(1 for it in items if it.kind in CAPABILITY_KINDS),
            "unclassified": sum(1 for it in items if it.kind == "UNCLASSIFIED"),
        },
    }


# --------------------------------------------------------------------------- #
# Operator amendments
# --------------------------------------------------------------------------- #
#
# A1 (2026-09-30). The completeness gate below refuses a requirement the plan
# does not name -- "invented work" -- and it is right to: a register that could
# add its own items would describe whatever it liked. But the operator is the
# plan's authority, and the storage incident of 2026-09-25 needed work the
# pinned production line does not name. So the plan can be *amended*, and an
# amendment is held to the same standard as the plan:
#
# * it is a file in this repository, parsed by the plan's own parser, so its
#   items are classified by the same table and nothing is hand-listed;
# * it is pinned by digest in AMENDMENTS.json, together with the operator
#   directive that authorises it, stored verbatim -- an edit to either without
#   a new pin is a problem, exactly as an edited plan is;
# * every file in the amendments directory must be named by the index, so an
#   unpinned file cannot sit there looking authoritative;
# * its item ids carry the amendment's name, so they can never collide with,
#   or be mistaken for, an item of the pinned plan;
# * it may add items to the plan's phases, never a phase of its own.

_AMENDMENT_ID = re.compile(r"^A\d+$")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_amendment(amendment_id: str, text: str) -> tuple[list[dict], list[Item]]:
    """(phases, items) of one amendment, with ids in its own namespace.

    The plan's parser, unchanged, then `P2.build-verify.x` becomes
    `P2.a1.build-verify.x`: the phase stays first, so everything keyed by the
    phase prefix still works, and the amendment's name follows it."""
    phases, items = parse(text)
    ns = amendment_id.lower()
    for it in items:
        rest = it.item_id.split(".", 1)[1] if "." in it.item_id else it.item_id
        it.item_id = f"{it.phase}.{ns}.{rest}"
    return phases, items


_PULL_FORWARD = re.compile(r"^pull-forward:\s+(\S+)\s*$")


def pulled_forward_items(text: str) -> list[str]:
    """The item ids an amendment pulls ahead of their phase, read from the
    amendment's own (pinned) text: one `pull-forward: <item id>` line each,
    above the first phase heading. The register may only mark a requirement
    `pulled_forward` when its item is listed here (O238 review, splan-a B1):
    otherwise the unpinned register alone could release any plan item early."""
    out = []
    for line in text.splitlines():
        if _PHASE.match(line.strip()):
            break
        m = _PULL_FORWARD.match(line.strip())
        if m:
            out.append(m.group(1))
    return out


def _git_index_history(index_path: Path) -> list[dict] | None:
    """Every committed version of the amendments index, oldest first, or None
    when there is no git history to compare with (a published clone).

    Round 2 (splan-a R1, splan-b B1): git runs from the repository's top level.
    Round 3 (splan-a B1, splan-b R3-1): the top level is found from the nearest
    *existing* ancestor of the configured path -- so a renamed or missing
    amendments directory is still compared with its history -- and the path is
    taken as configured, never through `resolve()`, so a symlinked index or
    directory cannot point the lookup elsewhere.

    The limit, stated: history that does not contain the commit (an orphan or
    rewritten branch, a copy without .git) cannot be checked, and is not."""
    import subprocess
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    probe = Path(os.path.abspath(index_path)).parent
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    try:
        top_run = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=probe,
                                 capture_output=True, text=True, env=env)
        if top_run.returncode:
            return None
        top = Path(top_run.stdout.strip())
        rel = os.path.relpath(os.path.abspath(index_path), os.path.realpath(top))
        if rel.startswith(".."):
            return None
        log = subprocess.run(["git", "log", "--format=%H", "--", rel],
                             cwd=top, capture_output=True, text=True, env=env)
        out = []
        for sha in reversed(log.stdout.split()):
            shown = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=top,
                                   capture_output=True, text=True, env=env)
            if shown.returncode == 0:
                try:
                    out.append(json.loads(shown.stdout))
                except ValueError:
                    pass
        return out
    except (OSError, ValueError):
        return None


def load_amendments(directory: Path | None = None, *, history: list[dict] | None = None,
                    use_git: bool = True) -> dict:
    """Every pinned amendment and its items, and every way they fail.

    Returns ``{"amendments": [...], "items": [...], "problems": [...]}``.
    No index at all is not a problem -- a programme with no amendments --
    but an amendment file without an index is, because it would be read by
    nothing and look authoritative to a person.

    What the pins give is **integrity, not authority** (O238 review, splan-b
    B1): the index and the files are written in this repository by the same
    actor. Authority comes from the operator, whose directive is stored
    verbatim as the amendment's `authority` and is reviewed before the commit
    that adds it. The structural rules below only make it impossible to change
    an amendment, or to let it authorise itself, without that being visible:

    * the amendment, its authority and its record are three distinct,
      non-empty files, and no file serves two amendments;
    * the index is append-only against every committed version of it: an entry
      once committed must stay byte-for-byte (compared through git history
      where there is one);
    * anything in the directory the index does not name -- a file or a
      subdirectory -- is a problem."""
    directory = directory or AMENDMENTS_DIR
    out: dict = {"amendments": [], "items": [], "problems": [], "pulled_forward": {}}
    problems = out["problems"]
    index_path = directory / AMENDMENTS_INDEX_NAME
    # Round 3 (splan-a B1): a symlinked directory or index would let the
    # history lookup -- and the reading -- follow an edited copy elsewhere.
    # Round 4 (splan-b R4-1, splan-a minor): the same holds for any symlinked
    # ancestor (`program/v3_3` pointing at a copy outside the repository). The
    # amendments are read only through a path with no symlink in it at all.
    existing = Path(os.path.abspath(directory))
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if directory.is_symlink() or index_path.is_symlink() or \
            os.path.realpath(existing) != str(existing):
        problems.append(f"{directory.name}/ or its {AMENDMENTS_INDEX_NAME} is a symlink "
                        "or is reached through a symlink -- the amendments must be read "
                        "where they are pinned, through a path with no symlink in it")
        return out
    present = sorted(directory.iterdir()) if directory.is_dir() else []
    if not index_path.is_file():
        if [x for x in present if x.name != AMENDMENTS_INDEX_NAME]:
            problems.append(f"{directory.name}/ holds {len(present)} entr(ies) but no "
                            f"{AMENDMENTS_INDEX_NAME}: nothing pins them")
        # O238 review round 2 (splan-a R1): an index that was committed and is now
        # gone is a removal of every amendment, not "no amendments".
        if history is None and use_git:
            history = _git_index_history(index_path)
        if any((h or {}).get("amendments") for h in history or []):
            problems.append(f"{AMENDMENTS_INDEX_NAME} was committed with amendments and "
                            "is now missing -- the index is append-only")
        return out
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{AMENDMENTS_INDEX_NAME} is unreadable: {exc}")
        return out
    entries = index.get("amendments") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        problems.append(f"{AMENDMENTS_INDEX_NAME} has no list of amendments")
        return out

    # Append-only: every entry of every committed version must still be there,
    # unchanged. A correction is a new amendment, never an edit.
    if history is None and use_git:
        history = _git_index_history(index_path)
    for old in history or []:
        for old_entry in (old.get("amendments") or []) if isinstance(old, dict) else []:
            if old_entry not in entries:
                problems.append(f"{AMENDMENTS_INDEX_NAME}: committed entry "
                                f"{old_entry.get('id')!r} was changed or removed -- the "
                                "index is append-only")

    named: set[str] = {AMENDMENTS_INDEX_NAME}
    owner_of_file: dict[str, str] = {}
    owner_of_digest: dict[str, str] = {}
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(f"index entry {entry!r} is not an object")
            continue
        aid = str(entry.get("id") or "")
        if not _AMENDMENT_ID.match(aid):
            problems.append(f"amendment id {aid!r} is not of the form A<n>")
            continue
        if aid in seen_ids:
            problems.append(f"amendment {aid} is listed twice")
            continue
        seen_ids.add(aid)
        authority = entry.get("authority") if isinstance(entry.get("authority"), dict) else {}
        record = entry.get("record") if isinstance(entry.get("record"), dict) else {}
        ok = True
        files: dict[str, str] = {}
        digests: dict[str, str] = {}
        for role, pin in (("amendment", {"file": entry.get("file"),
                                         "sha256": entry.get("sha256")}),
                          ("authority", authority), ("record", record)):
            name, want = pin.get("file"), pin.get("sha256")
            if role == "record" and not name and not want:
                continue            # an incident record is optional
            if not name or not want:
                problems.append(f"{aid}: its {role} is not pinned (file and sha256)")
                ok = False
                continue
            if Path(name).name != name or name in (".", ".."):
                problems.append(f"{aid}: {role} file {name!r} must be a bare name "
                                f"inside {directory.name}/")
                ok = False
                continue
            if name in files.values():
                problems.append(f"{aid}: its {role} is the same file as its "
                                f"{[r for r, f in files.items() if f == name][0]} "
                                f"({name}) -- an amendment cannot authorise itself")
                ok = False
                continue
            if name in owner_of_file and owner_of_file[name] != aid:
                problems.append(f"{aid}: {role} file {name} already belongs to "
                                f"{owner_of_file[name]}")
                ok = False
                continue
            files[role] = name
            owner_of_file[name] = aid
            named.add(name)
            path = directory / name
            if not path.is_file():
                problems.append(f"{aid}: {role} file {name} is missing")
                ok = False
                continue
            if path.is_symlink():
                problems.append(f"{aid}: {role} file {name} is a symlink -- pinned files "
                                "must be regular files")
                ok = False
                continue
            data = path.read_bytes()
            if not data.strip():
                problems.append(f"{aid}: {role} file {name} is empty")
                ok = False
                continue
            got = hashlib.sha256(data).hexdigest()
            if got in digests.values():
                # O238 review round 2 (splan-a minor 3): a byte-identical copy of the
                # amendment under another name is still the amendment authorising itself.
                problems.append(f"{aid}: its {role} has the same content as its "
                                f"{[r for r, d in digests.items() if d == got][0]}")
                ok = False
                continue
            if got in owner_of_digest and owner_of_digest[got] != aid:
                # Round 3 (splan-b minor): one text cannot authorise -- or be --
                # two amendments under different file names.
                problems.append(f"{aid}: its {role} has the same content as a file of "
                                f"{owner_of_digest[got]}")
                ok = False
                continue
            digests[role] = got
            owner_of_digest[got] = aid
            if got != want:
                problems.append(f"{aid}: {role} file {name} is {got[:12]} but the "
                                f"index pins {str(want)[:12]}: an unpinned edit")
                ok = False
        if not str(entry.get("authorised_by") or "").strip() or \
                not str(entry.get("authorised_on") or "").strip():
            problems.append(f"{aid}: no authorised_by / authorised_on")
            ok = False
        if not ok:
            continue
        text = (directory / entry["file"]).read_text(encoding="utf-8")
        phases, items = parse_amendment(aid, text)
        if not items:
            problems.append(f"{aid}: the amendment names no item")
        ids = {it.item_id for it in items}
        pf = pulled_forward_items(text)
        for item_id in pf:
            if item_id not in ids:
                problems.append(f"{aid}: pull-forward names {item_id!r}, which is not "
                                "an item of this amendment")
        out["pulled_forward"][aid] = [i for i in pf if i in ids]
        out["amendments"].append({
            "id": aid, "file": entry["file"], "sha256": entry["sha256"],
            "authority": authority, "record": record or None,
            "authorised_by": entry["authorised_by"],
            "authorised_on": entry["authorised_on"],
            "phases": [p["phase"] for p in phases],
            "items": len(items),
            "capability_items": sum(1 for i in items if i.kind in CAPABILITY_KINDS),
            "pulled_forward": out["pulled_forward"][aid],
        })
        for it in items:
            out["items"].append({**it.as_dict(), "amendment": aid})
    for entry in present:
        if entry.name not in named:
            what = "a subdirectory" if entry.is_dir() else "an unpinned file"
            problems.append(f"{directory.name}/{entry.name} is not named by "
                            f"{AMENDMENTS_INDEX_NAME}: {what}")
    return out


def merged(scope: dict, amendments: dict) -> dict:
    """The plan's inventory with the amendments' items added to its phases.

    The plan's own `items` and `plan_sha256` are kept as they are; the result
    says which amendments it carries. Phase counts are recomputed so a phase
    an amendment adds capabilities to stops reading as a non-capability one."""
    out = dict(scope)
    items = list(scope.get("items") or []) + list(amendments.get("items") or [])
    out["items"] = items
    phases = []
    for p in scope.get("phases") or []:
        mine = [i for i in items if i["phase"] == p["phase"]]
        caps = sum(1 for i in mine if i["kind"] in CAPABILITY_KINDS)
        q = {**p, "items": len(mine), "capabilities": caps}
        if caps and q.get("phase_kind") == "non-capability":
            q["phase_kind"] = "capability"
        phases.append(q)
    out["phases"] = phases
    out["amendments"] = [{"id": a["id"], "sha256": a["sha256"]}
                         for a in amendments.get("amendments") or []]
    return out


def amendment_problems(scope: dict | None, amendments: dict,
                       register: dict) -> list[str]:
    """What the amendments break, checked without the pinned plan.

    Pure over its inputs. With a `scope` (the plan's inventory), an amendment
    item must sit in one of the plan's phases and must not reuse an id of it;
    without one, only what the repository alone can answer is checked. Either
    way: every amendment capability maps to exactly one requirement, every
    requirement naming an amendment item names one that exists in its phase,
    and every `pulled_forward` names an amendment that exists and says why."""
    problems = list(amendments.get("problems") or [])
    a_items = {i["id"]: i for i in amendments.get("items") or []}
    known = {a["id"] for a in amendments.get("amendments") or []}
    if scope is not None:
        phases = {p["phase"] for p in scope.get("phases") or []}
        plan_ids = {i["id"] for i in scope.get("items") or []}
        for i in a_items.values():
            if i["phase"] not in phases:
                problems.append(f"{i['id']}: amendment {i['amendment']} adds an item "
                                f"to {i['phase']}, which is not a phase of the plan "
                                "-- an amendment may not create a phase")
            if i["id"] in plan_ids:
                problems.append(f"{i['id']}: collides with an item of the plan")
    for i in a_items.values():
        if i["kind"] in ("UNCLASSIFIED", "DUPLICATE"):
            problems.append(f"{i['id']}: {i['kind']} in amendment {i['amendment']}")
    mapped: dict[str, list[str]] = {}
    for r in register.get("requirements") or []:
        rid, target = r.get("id"), r.get("authoritative_item") or ""
        parts = target.split(".")
        if len(parts) > 2 and _AMENDMENT_ID.match(parts[1].upper()):
            if target not in a_items:
                problems.append(f"{rid}: authoritative_item {target!r} names an "
                                "amendment item that no pinned amendment contains")
            elif a_items[target]["phase"] != r.get("target_phase"):
                problems.append(f"{rid}: says {r.get('target_phase')} but its "
                                f"amendment item is in {a_items[target]['phase']}")
            else:
                mapped.setdefault(target, []).append(rid)
        pf = r.get("pulled_forward")
        allowed = amendments.get("pulled_forward") or {}
        if pf is not None:
            if not isinstance(pf, dict) or pf.get("amendment") not in known:
                problems.append(f"{rid}: pulled_forward must name a pinned "
                                f"amendment, got {pf!r}")
            elif not str(pf.get("why") or "").strip():
                problems.append(f"{rid}: pulled_forward carries no reason")
            elif target not in (allowed.get(pf["amendment"]) or []):
                # O238 review (splan-a B1): the release of an item ahead of its
                # phase is authorised by the pinned amendment text, never by
                # the register alone.
                problems.append(f"{rid}: pulled_forward cites {pf['amendment']}, but "
                                f"{pf['amendment']} does not pull forward {target!r}")
        elif any(target in v for v in allowed.values()):
            problems.append(f"{rid}: its item is pulled forward by an amendment but "
                            "the requirement carries no pulled_forward")
    for target, rids in sorted(mapped.items()):
        if len(rids) > 1:
            problems.append(f"{target} is claimed by {len(rids)} requirements: "
                            + ", ".join(rids))
    for i in a_items.values():
        if i["kind"] in CAPABILITY_KINDS and i["id"] not in mapped:
            problems.append(f"{i['id']} ({i['phase']}, {i['kind']}): an amendment "
                            "capability with no requirement")
    return problems


def amendments_document(amendments: dict, problems: list[str]) -> dict:
    """PROGRAMME_AMENDMENTS.json: what the tools that read the inventory merge
    in. Written only from pinned files; its `problems` say whether to trust it."""
    return {
        "what": ("Operator amendments to the pinned production line, parsed from "
                 "program/v3_3/amendments/ with the plan's own parser and pinned "
                 "by digest in its AMENDMENTS.json. Tools that read the programme "
                 "inventory merge these items into it; the plan's own inventory "
                 "(AUTHORITATIVE_PROGRAM_SCOPE.json) is not changed."),
        "amendments": amendments.get("amendments") or [],
        "pulled_forward": amendments.get("pulled_forward") or {},
        "items": amendments.get("items") or [],
        "problems": problems,
    }


def load_merged_scope(scope_file: Path | None = None,
                      amendments_file: Path | None = None) -> dict | None:
    """The inventory the other tools should read: the plan's, plus the
    amendments document when there is one. A document that records problems
    is refused (ValueError), so nothing downstream is derived from a broken
    amendment."""
    scope_file = scope_file or SCOPE
    amendments_file = amendments_file or AMENDMENTS_OUT
    if not scope_file.is_file():
        return None
    scope = json.loads(scope_file.read_text(encoding="utf-8"))
    if amendments_file.is_file():
        doc = json.loads(amendments_file.read_text(encoding="utf-8"))
        if doc.get("problems"):
            # O238 review (splan-a minor): merging a document that records
            # problems let the build plan go on from a broken amendment.
            raise ValueError(f"{amendments_file.name} records {len(doc['problems'])} "
                             "problem(s); the amendments are not merged until fixed")
        return merged(scope, doc)
    return scope


# --------------------------------------------------------------------------- #
# The completeness gate
# --------------------------------------------------------------------------- #

def _also_covers(requirement: dict, items: dict, phase: str,
                 mapped: dict[str, list[str]]) -> list[str]:
    """Extra plan items one requirement genuinely covers, validated.

    O204. The plan's acceptance path is three items -- `fresh-clone`, `doctor`,
    `run-small-verified-program` -- and P1-16's record covers all three in one
    run, because that is what the path *is*: a sequence, proven end to end or
    not at all. The register could name one item per requirement, so two of the
    three had no requirement tracking them and the dispositions table said
    nothing about them.

    Added rather than changed: `authoritative_item` keeps its meaning as the
    primary item, and `also_covers` is the additional set. A requirement that
    claims an item it does not cover would be worse than the gap, so each claim
    has to carry its own justification and the single-claim rule still holds:

    * the item must exist in the plan and sit in the requirement's phase;
    * it may not be the requirement's own `authoritative_item` -- listing it
      twice would make one item look doubly covered;
    * `also_covers_why` must carry a non-empty reason for it, so the claim is
      readable by someone checking it rather than inferred from a list;
    * and because the extra items go into `mapped`, the existing rule that an
      item is claimed by exactly one requirement applies to them unchanged.
    """
    problems: list[str] = []
    extra = requirement.get("also_covers") or []
    why = requirement.get("also_covers_why") or {}
    rid = requirement.get("id")
    if not isinstance(extra, list):
        return [f"{rid}: also_covers must be a list of plan item ids"]
    for target in extra:
        if target == requirement.get("authoritative_item"):
            problems.append(f"{rid}: also_covers repeats its own "
                            f"authoritative_item {target!r}")
            continue
        if target not in items:
            problems.append(f"{rid}: also_covers {target!r} is not an item of "
                            "the plan")
            continue
        if items[target]["phase"] != phase:
            problems.append(f"{rid}: also_covers {target!r} is in "
                            f"{items[target]['phase']}, not {phase}")
            continue
        if not str(why.get(target) or "").strip():
            problems.append(f"{rid}: also_covers {target!r} carries no reason in "
                            "also_covers_why -- a claim to cover a plan item "
                            "that nobody can check is how the register would "
                            "start absorbing items it does not prove")
            continue
        mapped.setdefault(target, []).append(rid)
    for target in why:
        if target not in extra:
            problems.append(f"{rid}: also_covers_why explains {target!r}, which "
                            "is not in also_covers")
    return problems


def completeness(scope: dict, register: dict, plan_sha256: str | None,
                 contracts: list[str] | None = None) -> list[str]:
    """Every way the register can fail to describe the programme. Empty = pass.

    Pure over its three inputs, so every negative control is a fixture edit
    rather than a change to this repository.
    """
    problems: list[str] = []
    if plan_sha256 is not None and scope.get("plan_sha256") != plan_sha256:
        problems.append(
            f"the inventory was parsed from plan {str(scope.get('plan_sha256'))[:12]} "
            f"but the pinned plan is {plan_sha256[:12]}: re-derive it")

    phases = {p["phase"] for p in scope.get("phases") or []}
    items = {i["id"]: i for i in scope.get("items") or []}
    order = [p["phase"] for p in scope.get("phases") or []]
    numbers = [int(x[1:]) for x in order if re.fullmatch(r"P\d+", x)]
    if len(numbers) != len(order) or numbers != list(range(len(numbers))):
        problems.append(f"the plan's phases are not P0..P{len(order) - 1} in "
                        f"order: {order} -- a phase was skipped, repeated or "
                        "renamed, and a later one could be missing unnoticed")
    for i in items.values():
        if i["kind"] == "DUPLICATE":
            problems.append(f"{i['id']}: the same line appears twice in one "
                            "section of the plan")
    for i in items.values():
        if i["kind"] == "UNCLASSIFIED":
            problems.append(f"{i['id']}: section {i['section']!r} has no kind; "
                            "every item must be classified")
    for p in scope.get("phases") or []:
        if not p.get("items"):
            problems.append(f"{p['phase']}: a phase with no items at all")

    reqs = register.get("requirements") or []
    ids = [r.get("id") for r in reqs]
    for rid in sorted({x for x in ids if ids.count(x) > 1}):
        problems.append(f"requirement id {rid} is used {ids.count(rid)} times")

    mapped: dict[str, list[str]] = {}
    for r in reqs:
        phase = r.get("target_phase")
        if phase not in phases:
            problems.append(f"{r.get('id')}: target_phase {phase!r} is not a "
                            "phase of the plan")
        target = r.get("authoritative_item")
        if not target:
            problems.append(f"{r.get('id')}: no authoritative_item -- a "
                            "requirement the plan does not name is invented work")
            continue
        if target not in items:
            problems.append(f"{r.get('id')}: authoritative_item {target!r} is "
                            "not an item of the plan")
            continue
        if items[target]["phase"] != phase:
            problems.append(f"{r.get('id')}: says {phase} but its item is in "
                            f"{items[target]['phase']}")
        mapped.setdefault(target, []).append(r.get("id"))
        problems.extend(_also_covers(r, items, phase, mapped))

    for target, rids in sorted(mapped.items()):
        if len(rids) > 1:
            problems.append(f"{target} is claimed by {len(rids)} requirements: "
                            + ", ".join(rids))
    for i in items.values():
        if i["kind"] in CAPABILITY_KINDS and i["id"] not in mapped:
            problems.append(f"{i['id']} ({i['phase']}, {i['kind']}): an "
                            "authoritative capability with no requirement")
    # Retired entries are kept, not deleted, and must say what replaced them.
    live = set(ids)
    for rt in register.get("retired") or []:
        if rt.get("id") in live:
            problems.append(f"{rt.get('id')} is both retired and live")
        sup = rt.get("superseded_by")
        if not sup or (sup not in live and sup not in items):
            problems.append(f"retired {rt.get('id')}: superseded_by {sup!r} is "
                            "neither a live requirement nor a plan item")
        if not rt.get("reason"):
            problems.append(f"retired {rt.get('id')}: no reason")

    # Every V3.3 contract the package names must be carried by a requirement.
    if contracts is not None:
        carried = {c for r in reqs for c in (r.get("contracts") or [])}
        for c in contracts:
            if c not in carried:
                problems.append(f"V3.3 contract {c!r} is carried by no "
                                "requirement")

    covered = {items[t]["phase"] for t in mapped if t in items}
    for p in scope.get("phases") or []:
        if p.get("capabilities") and p["phase"] not in covered:
            problems.append(f"{p['phase']}: a capability phase with no "
                            "requirement mapped to it at all")
    return problems


def dispositions(scope: dict, register: dict) -> list[dict]:
    """One row per plan item, saying what became of it. Silence is not a
    disposition: a capability names its requirement, anything else names its
    non-capability kind, and nothing is left blank.

    Pure over scope and register. The measured status of each requirement is
    the capability matrix's business and is joined in by the build plan, so
    this function never reads a generated document.
    """
    by_item: dict[str, list[str]] = {}
    for r in register.get("requirements") or []:
        if r.get("authoritative_item"):
            by_item.setdefault(r["authoritative_item"], []).append(r["id"])
        # O204: the items a requirement additionally covers appear here too, or
        # the disposition table would go on saying nothing about two thirds of
        # the acceptance path while a requirement proves all of it.
        for extra in r.get("also_covers") or []:
            by_item.setdefault(extra, []).append(r["id"])
    order = [p["phase"] for p in scope.get("phases") or []]
    rows = []
    for i in scope.get("items") or []:
        reqs = by_item.get(i["id"], [])
        idx = order.index(i["phase"]) if i["phase"] in order else -1
        gate = order[idx - 1] if idx > 0 else None
        if i["capability"]:
            disp = (f"mapped to {reqs[0]}" if len(reqs) == 1 else
                    ("UNMAPPED" if not reqs else f"MAPPED {len(reqs)} TIMES"))
        else:
            disp = f"non-capability: {i['kind']}" + (
                f" (tracked by {reqs[0]})" if reqs else "")
        rows.append({"item": i["id"], "phase": i["phase"],
                     "version": i.get("version"), "section": i["section"],
                     "line": i.get("line"), "kind": i["kind"],
                     "text": i["text"],
                     "capability": i["capability"],
                     "amendment": i.get("amendment"),
                     "requirement": reqs[0] if len(reqs) == 1 else None,
                     "disposition": disp, "waits_for_gate": gate})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-root", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--check", action="store_true",
                    help="compare the register against the inventory")
    ap.add_argument("--amendments-out", type=Path, default=None,
                    help="write the pinned operator amendments (runs without "
                         "the plan)")
    ap.add_argument("--check-amendments", action="store_true",
                    help="check the amendments' pins and their mapping into the "
                         "register (runs without the plan)")
    args = ap.parse_args(argv)

    if args.amendments_out or args.check_amendments or args.check:
        # A1: the amendments are in this repository, so this half of the gate
        # always runs -- it does not wait for a plan that may not be reachable.
        amendments = load_amendments()
        register = (json.loads(REGISTER.read_text(encoding="utf-8"))
                    if REGISTER.is_file() else None)
        if register is None:
            print("no register to check the amendments against", file=sys.stderr)
            return 3
        plan_scope = (json.loads(SCOPE.read_text(encoding="utf-8"))
                      if SCOPE.is_file() else None)
        problems = amendment_problems(plan_scope, amendments, register)
        n = len(amendments["amendments"])
        print(f"{n} amendment(s), {len(amendments['items'])} item(s), "
              f"{sum(1 for i in amendments['items'] if i['capability'])} capability item(s)")
        if args.amendments_out:
            args.amendments_out.parent.mkdir(parents=True, exist_ok=True)
            args.amendments_out.write_text(json.dumps(
                amendments_document(amendments, problems), indent=2,
                ensure_ascii=False) + "\n")
            print(f"wrote {args.amendments_out}")
        for p in problems[:40]:
            print(f"  PROBLEM: {p}")
        print(f"{'AMENDMENTS CONSISTENT' if not problems else 'AMENDMENTS INCONSISTENT'}"
              f": {len(problems)} problem(s)")
        if not (args.out or args.check):
            return 0 if not problems else 1
        if problems:
            # O238 review (splan-b B1): an inconsistent amendment fails `--check`
            # even where the pinned plan is not reachable -- the board row must
            # not read NOT_RUN while an amendment is broken.
            return 1

    plan = plan_path(args.plan_root)
    if plan is None:
        print(f"the pinned plan is not reachable; set ${PLAN_ENV}. The "
              "inventory is derived from it and nothing is claimed without it",
              file=sys.stderr)
        return 3
    scope = build(plan)
    c = scope["counts"]
    print(f"{c['phases']} phases, {c['items']} items, "
          f"{c['capability_items']} capability items, "
          f"{c['unclassified']} unclassified")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(scope, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {args.out}")
    if args.check:
        if not REGISTER.is_file():
            print("no register to check", file=sys.stderr)
            return 3
        register = json.loads(REGISTER.read_text(encoding="utf-8"))
        master = plan.parent.parent / MASTER_FILE
        contracts = (json.loads(master.read_text(encoding="utf-8")).get("new_in_v3_3")
                     if master.is_file() else None)
        if contracts is None:
            print("MASTER_PLAN_V3_3.json is not reachable; the contract check "
                  "did not run", file=sys.stderr)
            return 3
        # The inventory on disk must be the one this plan produces: a stale
        # file would let the register be compared against yesterday's scope.
        if SCOPE.is_file():
            on_disk = json.loads(SCOPE.read_text(encoding="utf-8"))
            if on_disk.get("items") != scope["items"]:
                print("  PROBLEM: AUTHORITATIVE_PROGRAM_SCOPE.json does not match "
                      "a fresh parse of the pinned plan; re-derive it")
                return 1
        # A1: the register is compared against the plan *and* its pinned
        # amendments; a requirement for an amendment item is not invented work.
        amendments = load_amendments()
        problems = completeness(merged(scope, amendments), register,
                                scope["plan_sha256"], contracts)
        problems += amendment_problems(scope, amendments, register)
        for p in problems[:40]:
            print(f"  PROBLEM: {p}")
        if len(problems) > 40:
            print(f"  ... and {len(problems) - 40} more")
        print(f"{'COMPLETE' if not problems else 'INCOMPLETE'}: "
              f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
