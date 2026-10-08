#!/usr/bin/env python3
"""What this repository has, measured requirement by requirement.

V3.3 P0. The audit doctrine the plan carries is short and hard: a capability
is `PROVEN` only with an implementation, a test, relevant positive evidence
*and* a discriminating control where one applies. Everything else has a weaker
word, and the weaker words are not interchangeable -- `MISSING` and
`PREPARED` and `PARTIAL` say different things to whoever plans the next phase.

The register lives beside this tool and names, for each requirement, a probe
this repository can actually run: a symbol, a collected test, a readiness row.
Nothing here reads a status out of a file. The one status this tool adds is
`NOT_DETERMINABLE`, which is not a status of the software: it means the probe
could not run, and it is never rounded to `MISSING`.

Usage:
    python3 tools/capability_matrix.py
    python3 tools/capability_matrix.py --out program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json
    python3 tools/capability_matrix.py --gaps program/v3_3/VERIHARNESS_GAP_REGISTER.md
"""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
REGISTER = HOH / "program/v3_3/REQUIREMENTS.json"

PROVEN = "PROVEN"
IMPLEMENTED_NOT_PROVEN = "IMPLEMENTED_NOT_PROVEN"
PARTIAL = "PARTIAL"
PREPARED = "PREPARED"
MISSING = "MISSING"
NOT_DETERMINABLE = "NOT_DETERMINABLE"

#: Ordered weakest to strongest, so a summary can be counted without a second
#: opinion about which word is better than which.
ORDER = [MISSING, PREPARED, PARTIAL, IMPLEMENTED_NOT_PROVEN, PROVEN]


def _symbols() -> dict[str, list[str]]:
    """Every class, function and method defined under src/ and tools/."""
    found: dict[str, list[str]] = {}
    for base in ("src/hoh", "tools"):
        for p in sorted((HOH / base).glob("*.py")):
            rel = str(p.relative_to(HOH))
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:                    # pragma: no cover
                continue
            for n in ast.walk(tree):
                if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
                    found.setdefault(n.name, []).append(f"{rel}:{n.lineno}")
    return found


def _collected() -> tuple[str, bool]:
    """pytest's own collection, so a named test is checked and not assumed."""
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                       cwd=str(HOH), capture_output=True, text=True)
    return p.stdout, p.returncode == 0


def _board() -> dict[str, tuple[str, str]]:
    """The readiness board's rows, read from the document it writes."""
    doc = HOH / "docs/READINESS.md"
    if not doc.is_file():
        return {}
    rows: dict[str, tuple[str, str]] = {}
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([a-z_0-9]+)` \| ([A-Z_ ()a-z]+) \| (.*?) \|", line)
        if m:
            rows[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
    return rows


def _inside(rel: str) -> Path | None:
    """`rel` resolved under the tree, or None when it is empty, absolute or
    escapes it. Reviewer B: evidence from outside the tree could make a
    requirement PROVEN through a register edit."""
    if not rel or Path(rel).is_absolute():
        return None
    path = (HOH / rel).resolve()
    if not path.is_relative_to(HOH.resolve()):
        return None
    # STORAGE-C2 (S18): rehydrated evidence counts only while a committed
    # receipt records its digest and the tree recomputes to it now. Judged on
    # the resolved path, so an in-tree symlink into .rehydrated/ is caught too.
    if path.is_relative_to(HOH.resolve() / ".rehydrated"):
        trusted, reason = _rehydrated_trusted(path)
        if not trusted:
            _inside_refusals.append(f"rehydrated evidence not trusted: {reason}")
        return path if trusted else None
    return path


#: Why `_inside()` refused a `.rehydrated/` path since `evidence_status` last
#: cleared it, so the matrix row's evidence can say so (Revision 8c item 1).
_inside_refusals: list[str] = []


def _rehydrated_trusted(path: Path) -> tuple[bool, str]:
    """`(rehydration.is_trusted_evidence_path(path, HOH), reason)`.

    Fails closed, returning the reason, whenever `hoh.rehydration` cannot be
    imported or the check raises. `sys.path` is never touched (Revision 8c
    item 1): an already imported ``hoh`` (and ``hoh.rehydration``) is reused
    from `sys.modules`, never loaded a second time; otherwise the package is
    located under ``HOH / "src"`` with
    `importlib.machinery.PathFinder.find_spec` and loaded through that spec."""
    try:
        rehydration = sys.modules.get("hoh.rehydration")
        if rehydration is None:
            if sys.modules.get("hoh") is None:
                src = HOH / "src"
                spec = importlib.machinery.PathFinder.find_spec("hoh", [str(src)])
                if spec is None or spec.loader is None:
                    raise ImportError(f"no hoh package under {src}")
                package = importlib.util.module_from_spec(spec)
                sys.modules["hoh"] = package
                try:
                    spec.loader.exec_module(package)
                except BaseException:
                    sys.modules.pop("hoh", None)
                    raise
            rehydration = importlib.import_module("hoh.rehydration")
        trusted = rehydration.is_trusted_evidence_path(path, HOH) is True
    except Exception as exc:  # noqa: BLE001 -- any failure is a refusal
        return False, f"hoh.rehydration unavailable ({type(exc).__name__}: {exc})"
    if not trusted:
        return False, (f"{path} lies in no .rehydrated/<digest>/ whose digest a "
                       "committed receipt records and whose content recomputes to it now")
    return True, ""


#: Tools that may derive an acceptance record from preserved evidence. Named,
#: not taken from the register: a register line must not be able to point the
#: re-derivation at an arbitrary program.
DERIVERS = {"acceptance_record.py", "durability_e2e.py"}


def evidence_status(probe: dict, *, requirement: dict | None = None) -> tuple[str, str]:
    """The status of an `evidence` probe, and one line saying why.

    Fails per requirement, never for the whole matrix: a malformed record used
    to raise out of `measure()` and take all 184 rows down with it.

    A record may speak for several requirements. `key` then names which part
    of it this probe reads: a requirement id (`requirements[key].satisfied`) or
    `stressor:<ID>` (`stressors[ID] == "PASS"`). Without a key the record's
    top-level `requirement_satisfied` is read, as for P1-16. `deriver` names
    the tool that re-derives the record (default `acceptance_record.py`), from
    the fixed set above. A durability record is additionally refused when it
    was taken on a dirty tree or when the code it is about changed since.

    The key is bound to the requirement that carries the probe, because nothing
    else stopped a register line for P2-33 keyed `P2-35` from reading PROVEN.
    `requirement` is the register entry being measured; when it is given, a
    requirement-id key is read only if the record's entry for that key names
    the same `authoritative_item` as the register entry, and a `stressor:<ID>`
    key only if the probe says `"stressor_for": <requirement id>` -- a stressor
    stands in for a requirement by an explicit, reviewable choice, never by a
    copied probe. Without `requirement` both paths read as they always did.
    A keyless probe on a durability record is refused either way: that record
    speaks for several requirements, and its top-level flag is their
    conjunction, not any one of them.
    """
    _inside_refusals.clear()
    record, evid = _inside(probe.get("path", "")), _inside(probe.get("evidence_dir", ""))
    if record is None or evid is None:
        why = "".join(f"; {reason}" for reason in _inside_refusals)
        return NOT_DETERMINABLE, (f"evidence probe paths must lie inside the tree: "
                                  f"{probe.get('path')!r}, {probe.get('evidence_dir')!r}"
                                  + why)
    deriver = probe.get("deriver", "acceptance_record.py")
    if deriver not in DERIVERS:
        return NOT_DETERMINABLE, f"deriver {deriver!r} is not one of {sorted(DERIVERS)}"
    key = probe.get("key")
    if key is None and deriver == "durability_e2e.py":
        return NOT_DETERMINABLE, (
            f"record {probe.get('path')}: a durability record speaks for several "
            "requirements, so a keyless probe would take their conjunction for any "
            "one of them; the probe must name its requirement in `key`")
    if not record.is_file():
        return MISSING, f"no acceptance record at {probe.get('path')}"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return NOT_DETERMINABLE, f"record {probe.get('path')} unreadable: {exc}"
    if not isinstance(data, dict):
        return NOT_DETERMINABLE, f"record {probe.get('path')} is not an object"
    if requirement is not None and key is not None:
        refused = _key_not_bound(probe, key, requirement, data)
        if refused:
            return NOT_DETERMINABLE, f"record {probe.get('path')} [{key}]: {refused}"
    try:
        again = subprocess.run(
            # The derivation tool sits beside this one, wherever the tree
            # under test is. Resolving it through HOH made a missing tool
            # indistinguishable from a forged record.
            [sys.executable, str(Path(__file__).resolve().parent / deriver),
             *(["derive"] if deriver == "durability_e2e.py" else []),
             "--evidence", str(evid), "--out", str(record), "--check"],
            capture_output=True, text=True, cwd=str(HOH), timeout=600)
        rederived = again.returncode == 0
    except subprocess.TimeoutExpired:
        rederived = False
    if key is None:
        satisfied = data.get("requirement_satisfied") is True
    elif str(key).startswith("stressor:"):
        satisfied = (data.get("stressors") or {}).get(str(key)[9:]) == "PASS"
    else:
        satisfied = ((data.get("requirements") or {}).get(key) or {}).get("satisfied") is True
    stale = ""
    if deriver == "durability_e2e.py":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from durability_e2e import stale_paths  # noqa: PLC0415

        changed = stale_paths(data.get("evidence_head"))
        if data.get("evidence_dirty"):
            stale = "taken on a dirty tree"
        elif changed is None:
            stale = "evidence commit unknown"
        elif changed:
            stale = "code changed since: " + ", ".join(changed[:3])
    note = (f"record {probe.get('path')}{f' [{key}]' if key else ''}: "
            f"satisfied={satisfied}, re-derivation {'ok' if rederived else 'FAILED'}"
            + (f", STALE ({stale})" if stale else ""))
    if satisfied and rederived and not stale:
        return PROVEN, note
    return IMPLEMENTED_NOT_PROVEN, note


def _key_not_bound(probe: dict, key, requirement: dict, data: dict) -> str:
    """Why `key` does not belong to `requirement`, or "" when it does."""
    rid = requirement.get("id")
    if str(key).startswith("stressor:"):
        if "stressor_for" not in probe:
            return (f"stressor key {key!r} on {rid} carries no `stressor_for`; a "
                    f"stressor speaks for a requirement only when the probe says so")
        if probe.get("stressor_for") != rid:
            return (f"stressor key {key!r} is declared for "
                    f"{probe.get('stressor_for')!r}, not for {rid}")
        return ""
    entries = data.get("requirements")
    entry = entries.get(key) if isinstance(entries, dict) and isinstance(key, str) else None
    theirs = entry.get("authoritative_item") if isinstance(entry, dict) else None
    ours = requirement.get("authoritative_item")
    # Missing is absent, None or "" on either side; two missing items are not
    # a match (`None == None` bound in the pkeybind review's mutant).
    record_lacks, register_lacks = theirs in (None, ""), ours in (None, "")
    if record_lacks and register_lacks:
        return (f"neither the record entry for {key!r} nor the register entry for "
                f"{rid} names an authoritative_item; a probe reads only its own "
                f"requirement")
    if record_lacks:
        return (f"the record entry for {key!r} names no authoritative_item, so it "
                f"cannot be bound to {rid}")
    if register_lacks:
        return (f"the register entry for {rid} names no authoritative_item, so key "
                f"{key!r} cannot be bound to it")
    if theirs != ours:
        return (f"key {key!r} names authoritative_item {theirs!r} in the record, "
                f"but {rid} is {ours!r}; a probe reads only its own requirement")
    return ""


#: The parsed plan inventory, used for the "base" exemption below. Read rather than
#: trusted: a register may not declare an item a base, the *plan* has to say so.
SCOPE_FILE = HOH / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"


def _import_graph() -> dict[str, set[str]]:
    """Which files import each `src/hoh` module, over `src/hoh` and `tools`.

    A module's own file never counts as importing itself, and a test never counts:
    `tests/` is how a module is *tested*, not how the product *uses* it, and
    conflating the two is exactly the overclaim this graph exists to prevent.
    """
    import ast

    modules = {f.stem for f in (HOH / "src/hoh").glob("*.py")}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for f in list((HOH / "src/hoh").glob("*.py")) + list((HOH / "tools").glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                # `node.module` is None for `from . import stages`, which is the
                # dominant style in this package -- the first version of this graph
                # required a module and so recorded none of them, capping the
                # controller transition gate and session succession as unconsumed
                # when the CLI imports both. The names are the payload either way.
                if node.module:
                    names.append(node.module.split(".")[-1])
                names += [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names += [a.name.split(".")[-1] for a in node.names]
            for name in names:
                if name in graph and f.stem != name:
                    graph[name].add(str(f.relative_to(HOH)))
    return graph


#: What counts as a place the product or the programme actually *does* something.
#: `src/hoh/cli.py` is the product's own command surface and is the consumer that
#: matters most -- the first version of this rule looked only under `tools/` and so
#: capped the sentinel, session succession and the controller transition gate, all of
#: which the CLI reaches. Anything under `tools/` counts because that is where this
#: programme's own gates live.
def _is_entry_point(rel: str) -> bool:
    return rel.startswith("tools/") or rel == "src/hoh/cli.py"


def _reachable_from_an_entry_point(module: str, graph: dict[str, set[str]]) -> list[str]:
    """The consumers of `module`, following the graph until it reaches an entry point.

    A chain that ends inside `src/hoh` with nothing importing its root is not
    consumption. `decisions` is imported by `decisioncheck` and `decisioncheck` by
    nothing, so no entry point *names* either of them -- and a one-hop check would
    have called the first of them consumed.

    O225/C4, and the sentence stood uncorrected for a round longer than the ledger
    said it did: an earlier form read "so neither is used by the product", which is
    the same overclaim in the negative direction that the evidence line was corrected
    for in the positive one. This walks import *statements*, so it can report that
    nothing names a module, never that nothing reaches it -- `_import_graph` cannot
    see `importlib.util.spec_from_file_location`, and `tools/readiness.py` loads
    `programme_supervisor` exactly that way. Saying more than that was asymmetric
    honesty: a caveat on the answer I did not like, none on the answer I did.
    """
    seen: set[str] = set()
    frontier = [module]
    consumers: list[str] = []
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        for who in sorted(graph.get(current, ())):
            if _is_entry_point(who):
                consumers.append(who)
            else:
                stem = Path(who).stem
                if stem not in seen:
                    frontier.append(stem)
    return sorted(set(consumers))


def _plan_calls_it_a_base(item_id: str) -> bool:
    """Does the *plan* scope this item as a base?

    O229/F6 asked for the base rule as data rather than prose. **Four** plan items
    match on a whole word, and the first version of this docstring named three:

        P2.build-verify.mcp-gateway-base                        (P2-18)
        P2.build-verify.decision-exploration-base               (P2-19)
        P2.build-verify.digital-thread-identifier-projection-base (P2-31)
        P2.build-verify.evidenceclaim-schema-linkage-base       (P2-32)

    For each, the plan's own words scope the requirement as a base, and the base
    existing is the requirement. P2-21 is "Decision fsck" and carries no such
    qualifier, so the exemption does not reach it.

    P2-32 is the one the earlier docstring missed, and it is PARTIAL today only
    because the equivalent cap runs last and unconditionally -- delete the
    `equivalent` from its probe and it becomes an exempt, unconsumed PROVEN. A
    reviewer found that, and `tests/test_capability_caps.py` now pins the exact set so
    a fifth exemption is a change someone has to make on purpose.

    Read from the parsed inventory, so a register cannot grant itself the exemption.
    """
    if not SCOPE_FILE.is_file():
        return False
    try:
        scope = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for item in scope.get("items") or []:
        if str(item.get("id")) == item_id:
            # A **whole word**, not a substring. `"base" in text` also matched
            # "baseline", "database" and "codebase": a reviewer showed
            # `P4.core.shared-baseline-health-classification` earning the exemption
            # from the word "baseline", and then used it to bring an unconsumed
            # module to PROVEN. O229/F6 asked for this rule as data precisely so it
            # could not be granted by accident, and a substring match on prose is not
            # that.
            words = re.findall(r"[a-z]+", str(item.get("text", "")).lower())
            return "base" in words
    return False


def classify(req: dict, symbols, collection, board,
             graph: dict | None = None) -> dict:
    """One requirement's status, and the evidence that produced it."""
    probe = req.get("probe") or {}
    kind = probe.get("kind", "none")
    equivalent = probe.get("equivalent")
    evidence: list[str] = []

    symbol_hits = symbols.get(probe.get("symbol", ""), []) if probe.get("symbol") else []
    # O220/F7: a bare name matches anywhere under src/hoh and tools, so
    # `authority_for` found `assurance.py` before `projectcontract.py` and P2-30 was
    # measured against a different function of the same name. A probe may name the
    # file its symbol belongs to, and then only hits in that file count -- a probe
    # that names one and finds nothing there has not found its symbol.
    wanted = str(probe.get("module") or "").strip()
    if wanted and probe.get("symbol"):
        # Compared as a filename, not a suffix. `endswith` let
        # `tools/programme_checklist.py` satisfy a probe naming `checklist.py`, and
        # seven such collisions exist in this tree (`*_evidence.py`, `projectstore.py`
        # against `store.py`, and so on). Worse, a mis-scoped hit landing under
        # `tools/` sets `at_an_entry_point` below and bypasses the consumption cap
        # outright -- a reviewer demonstrated a probe naming `evidence.py` reaching
        # PROVEN off `tools/meta_evidence.py`. The four probes that name a module were
        # correct by luck, which is not a property to keep.
        scoped = [h for h in symbol_hits
                  if Path(h.split(":")[0]).name == wanted]
        if len(symbol_hits) > len(scoped):
            evidence.append(
                f"{len(symbol_hits) - len(scoped)} hit(s) for "
                f"{probe.get('symbol')!r} outside {wanted} ignored (O220/F7)")
        symbol_hits = scoped
    elif len(symbol_hits) > 1:
        evidence.append(
            f"AMBIGUOUS: {probe.get('symbol')!r} is defined in {len(symbol_hits)} "
            "places and the probe names no module, so the first was measured "
            "(O220/F7)")
    if symbol_hits:
        evidence += symbol_hits[:2]

    test_ref = probe.get("test")
    test_seen = None
    if test_ref:
        if collection is None:
            test_seen = None
        else:
            text, ok = collection
            test_seen = (test_ref in text) or (test_ref.split("::")[-1] in text)
            evidence.append(f"test {test_ref}: "
                            + ("collected" if test_seen else "NOT collected"))

    row_name = probe.get("row")
    row_state = None
    if row_name:
        if not board:
            row_state = None
        else:
            row = board.get(row_name)
            row_state = row[0] if row else "ABSENT"
            evidence.append(f"board row `{row_name}`: {row_state}")

    # --- the decision, in the order the plan's vocabulary implies ----------
    if kind == "none":
        status = PARTIAL if equivalent else MISSING
    elif kind in ("symbol", "method"):
        if not symbol_hits:
            status = PARTIAL if equivalent else MISSING
        elif test_seen is None and test_ref:
            status = NOT_DETERMINABLE
        elif test_ref and not test_seen:
            status = IMPLEMENTED_NOT_PROVEN
        elif test_ref:
            status = PROVEN
        else:
            status = IMPLEMENTED_NOT_PROVEN
    elif kind == "test":
        if collection is None:
            status = NOT_DETERMINABLE
        else:
            status = PROVEN if test_seen else MISSING
    elif kind == "evidence":
        # A durable acceptance record derived from a preserved run (P1-16):
        # PROVEN only when it exists, says the requirement is satisfied, and
        # re-derives from its evidence -- a hand-edited record fails the last
        # condition. Durable evidence, not operational state (O197).
        status, note = evidence_status(probe, requirement=req)
        evidence.append(note)
        if status == MISSING and equivalent:
            status = PARTIAL
    elif kind == "row":
        # O197. A board row answers "is this green right now", which is
        # operational state; this matrix answers "does the capability exist
        # and is it tested". Reading the one to decide the other made the
        # board and the matrix a cycle: `succession` flipped, P2-07 flipped
        # with it, the matrix went stale, its own row flipped, and the board
        # never reached a fixpoint in four passes. Refused rather than
        # supported, so the cycle cannot come back through a register edit.
        status = NOT_DETERMINABLE
        evidence.append("row probes are refused: the matrix does not read the "
                        "board (O197)")
    else:
        status = NOT_DETERMINABLE
        evidence.append(f"unknown probe kind {kind!r}")

    # O220/F4, F5, F6: a module nothing consumes is not a capability the product has.
    #
    # The P2 gate's own supervisor review found twelve items PROVEN for modules that
    # nothing in `src/hoh` or `tools` imports -- six of which said so in their own
    # register note. The precedent was already this programme's: P2-26 was held at
    # PARTIAL until `tools/programme_checklist.py` consumed it. It had never been
    # derivable, only applied by hand, and so it was applied unevenly.
    #
    # Now it is measured -- but measured **at the level of import text**, and O222
    # records how far short of the doctrine that falls. A symbol or method probe
    # reaching PROVEN is capped at PARTIAL unless the module its symbol lives in is
    # named in an import statement by a file under `tools/` or by `src/hoh/cli.py`, or
    # the *plan* scopes the item as a base. `tests/` deliberately does not count: a
    # test is how a module is tested, not how the product uses it.
    #
    # What this is **not**: evidence that any code runs. An import inside
    # `if False:` satisfies it, and a reviewer used exactly that to reverse all nine
    # demotions. It is a necessary condition, cheaply checked, and it caught twelve
    # real overclaims on its first run. It is not the sufficient one, and the
    # sufficient one -- a named test that is *executed and passes* -- is O220/F7,
    # still unimplemented because `_collected()` binds pytest's return code and
    # discards it.
    if status == PROVEN and kind in ("symbol", "method") and graph is not None:
        module = ""
        at_an_entry_point = False
        if symbol_hits:
            first = symbol_hits[0].split(":")[0].lstrip("./")
            # The symbol may *be* in an entry point: `tools/succession.py` is how this
            # programme writes its own capsule, and `src/hoh/cli.py` is the product's
            # command surface. Asking who consumes those is asking who consumes the
            # consumer -- nothing imports the top of a graph. Two earlier versions of
            # this rule got that wrong in turn, capping session succession and then
            # basic routing.
            if _is_entry_point(first):
                at_an_entry_point = True
            elif "src/hoh/" in first:
                module = Path(first).stem
        consumers = _reachable_from_an_entry_point(module, graph) if module else []
        if at_an_entry_point:
            evidence.append(f"defined in an entry point: {symbol_hits[0]}")
        elif consumers:
            # "named in an import statement by", not "consumed by". A reviewer wrote a
            # two-line file whose import can never execute --
            # `if False: from hoh import capsule, ...` -- and all nine capped items
            # flipped back to PROVEN, each carrying "consumed by tools/zz_dead.py" as
            # its evidence. Ruff clean, suite clean, checklist clean. This is an AST
            # scan for import *statements*: nothing is executed and nothing is called,
            # and the evidence line must say what was measured rather than what one
            # would like it to mean.
            evidence.append(
                f"named in an import statement by {', '.join(consumers[:3])} "
                "(text, not execution -- see O222)")
        elif _plan_calls_it_a_base(str(req.get("authoritative_item") or "")):
            evidence.append(
                "no importer named in any entry point (text, not execution -- "
                "dynamic loads are invisible to this scan; see O225/C4), and the "
                "plan scopes this item as a base: the base existing is the "
                "requirement")
        else:
            status = PARTIAL
            evidence.append(
                f"CAPPED at PARTIAL: no entry point (tools/ or src/hoh/cli.py) names "
                f"src/hoh/{module}.py in an import statement, so nothing shows the "
                "product using it -- text, not execution, and a dynamic load would "
                "be invisible here (O220/F4, O225/C4; the P2-26 precedent, measured "
                "rather than judged)")

    # An equivalent named in the register raises MISSING to PARTIAL **and caps
    # anything higher at PARTIAL**. The second half is O220/F3 and it makes this
    # comment true: it always said "it never reaches PROVEN: a thing that stands in
    # for another thing has not been shown to be that thing", and the code only ever
    # raised. So a row could declare an equivalent and still reach PROVEN on a symbol
    # hit plus a collected test, which is what P2-09, P2-17 and P2-32 did.
    #
    # The first version of this comment offered the other seven equivalent-carrying
    # rows as a control, and a reviewer was right that they are not one: four have
    # `kind: none` and the other three name symbols that do not exist in the tree, so
    # all seven were MISSING raised to PARTIAL and none was ever a PROVEN candidate.
    # They show the rule is *consistent*, not that it discriminates. What justifies
    # the cap is the doctrine itself, which this module has always stated and never
    # enforced: a thing that stands in for another thing has not been shown to be
    # that thing.
    # `status in ORDER` first, because NOT_DETERMINABLE is deliberately not in it and
    # `ORDER.index` raises on it -- which took the whole matrix down, all 184 rows,
    # exactly the failure `evidence_status`'s docstring says was removed. A reviewer
    # reproduced it three ways: a `row` probe, an unknown kind, and a damaged evidence
    # record, each with one word added. Four rows already carry an `equivalent` on a
    # `kind: none` probe, so this was one register edit from being live.
    if equivalent and status in ORDER and ORDER.index(status) > ORDER.index(PARTIAL):
        evidence.append(
            f"CAPPED at PARTIAL by the equivalent named in the register: "
            f"{str(equivalent)[:80]}")
        status = PARTIAL
    elif status == MISSING and equivalent:
        status = PARTIAL
    out = dict(req)
    out.pop("probe", None)
    out["status"] = status
    out["evidence"] = evidence
    if equivalent:
        out["equivalent_here"] = equivalent
    return out


class RegisterMissing(FileNotFoundError):
    """The register is internal and the tools are published, so a clone can
    have this tool without having anything for it to measure. That is an
    environment gap with a name, not an empty matrix."""


def measure() -> dict:
    if not REGISTER.is_file():
        raise RegisterMissing(
            f"{REGISTER.relative_to(HOH)} is not in this tree. It is derived "
            "from an internal planning package and is not exported; without "
            "it there is nothing to measure, and an empty matrix would read "
            "as a repository with no requirements.")
    register = json.loads(REGISTER.read_text(encoding="utf-8"))
    symbols = _symbols()
    collection = _collected()
    board: dict = {}   # O197: never read; see the `row` branch in classify
    graph = _import_graph()   # O220/F4: consumption is measured, not judged
    rows = [classify(r, symbols, collection, board, graph)
            for r in register["requirements"]]
    counts: dict[str, int] = {s: 0 for s in ORDER + [NOT_DETERMINABLE]}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "what": ("Capability matrix. Each status is derived from a probe this "
                 "repository ran at the time given, not read from a document."),
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_register": str(REGISTER.relative_to(HOH)),
        "plan_snapshot_commit": register.get("plan_snapshot_commit"),
        "counts": counts,
        "capabilities": rows,
    }


def gap_register(body: dict) -> str:
    """The gaps, ordered by what a release would need first."""
    # Everything that is not PROVEN, and `IMPLEMENTED_NOT_PROVEN` above all:
    # a capability that exists and has never been shown to work is the one a
    # reader is most likely to assume is fine.
    rows = [r for r in body["capabilities"] if r["status"] != PROVEN]
    rows.sort(key=lambda r: (not r["core_required_1_0"], r["target_phase"], r["id"]))
    out = ["# Gap register (V3.3 P0)", "",
           "Generated by `python3 tools/capability_matrix.py --gaps`. Every row",
           "is a requirement whose probe did not find a proven capability, with",
           "what the probe saw. `PARTIAL` means something here does part of the",
           "job under another name and that name is given; `NOT_DETERMINABLE`",
           "means the probe could not run and is not a statement about the",
           "software.", "",
           f"Measured at {body['measured_at_utc']}.", "",
           "| id | requirement | phase | status | core | what the probe saw |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        saw = r.get("equivalent_here") or (r["evidence"][0] if r["evidence"] else "nothing")
        out.append(f"| `{r['id']}` | {r['name']} | {r['target_phase']} | "
                   f"{r['status']} | {'yes' if r['core_required_1_0'] else 'no'} | "
                   f"{saw[:90]} |")
    counts = body["counts"]
    out += ["", "## Counts", ""]
    for s in ORDER + [NOT_DETERMINABLE]:
        out.append(f"* {s}: {counts.get(s, 0)}")
    out += ["",
            "A `MISSING` row is not a rejected idea. It is a requirement whose",
            "probe found neither a symbol nor a named equivalent, which is the",
            "state most of V3.3's parallelism contracts are in by design: the",
            "plan is the thing that has not been built yet.", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--gaps", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        body = measure()
    except RegisterMissing as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(body, indent=2))
    else:
        for s in ORDER + [NOT_DETERMINABLE]:
            print(f"  {s:<24} {body['counts'].get(s, 0)}")
        core_missing = [r["id"] for r in body["capabilities"]
                        if r["core_required_1_0"] and r["status"] == MISSING]
        print(f"\n{len(core_missing)} core-required capabilities are MISSING: "
              + ", ".join(core_missing))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(body, indent=2) + "\n")
        print(f"wrote {args.out}")
    if args.gaps:
        args.gaps.parent.mkdir(parents=True, exist_ok=True)
        args.gaps.write_text(gap_register(body) + "\n")
        print(f"wrote {args.gaps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
