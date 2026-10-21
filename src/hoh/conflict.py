"""Whether two planned work items may run in parallel -- and what to do when the
declaration of either cannot be trusted.

`HohRunLauncher.depends_on` compares the declared paths of two items as exact
strings. That misses every pair that touches the same file under a different
spelling (`src/hoh` against `src/hoh/a.py`, `./a//b` against `a/b`, `README.md`
against `readme.md`, `docs/*.md` against `docs/x.md`), and it calls an item
that declares nothing independent of everything. This module is the sound
replacement for that relation: it turns the *declared* footprints of two items
into a typed, ordered, symmetric list of reasons they must not run together.

Three rules shape every decision here.

* **Fail closed.** A declaration that cannot be read as a set of clean
  repository paths makes the item UNDECIDABLE, and an UNDECIDABLE item
  conflicts with everything. An omission is not independence, and a typo must
  not look like freedom. The only error `footprint` can make is to serialise
  more than necessary.
* **Never trust a declared value.** The module never calls `str()` or `repr()`
  on a value it was handed, reads at most `MAX_PATHS + 1` entries of any
  iterable, and keeps every report bounded, so a hostile or enormous
  declaration can neither raise out of `footprint` nor flood a report.
* **Path text only.** Nothing here touches the filesystem. A path is compared
  as text, component by component; a glob is widened to the directory before
  it, which can only serialise more. Widening is sound only while nothing
  behind the glob can climb out of that directory, so a glob component that
  *may match* `..` under some shell is refused (see `normalize_path`).

"Conflict" means exactly one thing in this module: two planned items that must
not run in parallel. It is not a git merge conflict and not a contract verdict
conflict (`projectcontract`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice
from typing import Any

__all__ = [
    "MAX_PATH_BYTES", "MAX_DEPTH", "MAX_PATHS", "MAX_EVIDENCE",
    "MAX_ITEM_CHARS", "MAX_PROBLEMS", "MAX_BRACE_ALTERNATIVES", "MAX_EXPANDED_CHARS",
    "MAX_GLOB_COMPONENT_CHARS",
    "ConflictInputError", "Kind", "Conflict", "Footprint",
    "normalize_path", "footprint", "footprint_of", "detect",
]

#: UTF-8 bytes of one declared path.
MAX_PATH_BYTES = 4096
#: Components of one path after normalisation.
MAX_DEPTH = 64
#: Paths on one side (writes or reads) of one item.
MAX_PATHS = 1000
#: Records returned by one `detect`.
MAX_EVIDENCE = 20
#: Characters of an item id.
MAX_ITEM_CHARS = 256
#: Problems kept on one footprint (one more line says how many were cut).
MAX_PROBLEMS = 20

#: Alternatives of the brace expansions of one path that are checked against
#: `..` (a pattern that needs more is refused, not analysed).
MAX_BRACE_ALTERNATIVES = 64
#: Characters of all those alternatives together, per path.
MAX_EXPANDED_CHARS = 2048
#: A glob component longer than this is refused without being analysed.
MAX_GLOB_COMPONENT_CHARS = 128

#: How much of a declared value a problem may quote.
_QUOTE_CHARS = 40
#: How much of a hand-built problem string a report may carry.
_PROBLEM_CHARS = 200

_GLOB_CHARS = frozenset("*?[]{}")
_ASCII_CONTROL = re.compile("[\x00-\x1f\x7f]")
#: An extended-glob operator (`@(..|a)`, `+(.)`, `!(x)`, `?(.)`, `*(.)`).
_EXTGLOB = re.compile(r"[?*+@!]\(")
#: Unicode categories no item id may hold: control, format (U+202E, U+200B,
#: U+FEFF), lone surrogate, line and paragraph separator (U+2028, U+2029).
_FORBIDDEN_ID_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class ConflictInputError(ValueError):
    """A path or a hand-built footprint that this module refuses."""


class Kind(StrEnum):
    WRITE_WRITE = "WRITE_WRITE"
    WRITE_READ = "WRITE_READ"
    UNDECIDABLE = "UNDECIDABLE"


# --------------------------------------------------------------------------- #
# describing a value without ever calling its own str() or repr()
# --------------------------------------------------------------------------- #

def _printable(text: str, limit: int) -> str:
    """At most `limit` characters of an exact `str`, anything but printable
    ASCII replaced, so a report line cannot be forged or flooded."""
    return "".join(ch if " " <= ch <= "~" else "?" for ch in text[:limit])


def _type_name(value: object) -> str:
    try:
        return _printable(str.__str__(type(value).__name__), _QUOTE_CHARS)
    except Exception:
        return "object"


def _describe(value: object) -> str:
    """Name an offending value: at most 40 characters of a `str`, the type
    name of anything else. The value's own `__str__`/`__repr__` is not called."""
    try:
        if isinstance(value, str):
            plain = str.__str__(value)
            more = "..." if len(plain) > _QUOTE_CHARS else ""
            return "'" + _printable(plain, _QUOTE_CHARS) + "'" + more
    except Exception:
        return "an unreadable value"
    return "a value of type " + _type_name(value)


def _plain(value: object) -> str | None:
    """`value` as an exact `str`, or None when it is not a real one. An object
    that only claims `__class__ = str` passes `isinstance` and fails here."""
    try:
        if isinstance(value, str):
            return str.__str__(value)
    except Exception:
        pass
    return None


def _escaped(value: object) -> str:
    """Text for a report line: every character that is not printable
    (U+2028, U+202E, a lone surrogate, a control character) written as an
    escape, so no field of a `Conflict` can break or forge a line."""
    text = _plain(value)
    if text is None:
        return "<" + _type_name(value) + ">"
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii")
                   for ch in text)


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

class _Dangerous(Exception):
    """Raised inside the judge: this component may match `..`, or cannot be
    analysed within the budget (both are refusals)."""


def _check_braces(text: str) -> None:
    """Refuse every brace construct that is not *simple*, on the whole string.

    A shell expands braces before it splits a word at `/` and before it
    matches a pattern, so a group may span components and alternatives may
    hold `/`: `docs/{/.,}./x` is `docs//../x` and `docs/./x`. The rule is
    therefore decided on the whole text, by construction:

    * a `{` with no `}` anywhere after it is a literal (and so is a `}` that no
      group closes: bash keeps both);
    * otherwise the text from the `{` to the **first** `}` after it is a
      group, and it must be *simple*: no `{`, no `/` and at least one `,`.
      Anything else (`{}`, `{a}`, `{1..3}`, `{a{b,c}}`, `{a/b,c}`, a group
      that spans a `/`) is refused: bash treats such text in ways that are
      not worth modelling, and a refusal only serialises.

    After this check every group lies inside one component, its alternatives
    are plain text between commas, and expansion can neither create nor remove
    a `/`."""
    start = text.find("{")
    while start >= 0:
        end = text.find("}", start + 1)
        if end < 0:
            return
        body = text[start + 1:end]
        if "{" in body or "/" in body or "," not in body:
            raise ConflictInputError("a brace group must be a simple comma list inside one path "
                                     "component (no nesting, no '/', no sequence expression)")
        start = text.find("{", end + 1)


class _Budget:
    """What one path may still spend on the analysis of its glob components."""

    __slots__ = ("alternatives", "chars")

    def __init__(self) -> None:
        self.alternatives = MAX_BRACE_ALTERNATIVES
        self.chars = MAX_EXPANDED_CHARS


def _alternatives(component: str, budget: _Budget) -> list[str]:
    """The brace expansion of one component (groups are simple, see
    `_check_braces`), within the path's budget; `_Dangerous` past it."""
    results = [""]
    index = 0
    size = len(component)
    while index < size:
        char = component[index]
        end = component.find("}", index + 1) if char == "{" else -1
        if end >= 0:
            options = component[index + 1:end].split(",")
            index = end + 1
        else:
            options = [char]
            index += 1
        if len(results) * len(options) > budget.alternatives:
            raise _Dangerous
        results = [done + option for done in results for option in options]
        if sum(map(len, results)) > budget.chars:
            raise _Dangerous
    budget.alternatives -= len(results)
    budget.chars -= sum(map(len, results))
    return results


def _class_may_match_dot(variant: str, start: int) -> tuple[int, bool, bool] | None:
    """Read the bracket expression that opens at `variant[start]` (a `[`).

    Returns `(index after the closing bracket, may match '.', negated)`, or
    None when there is no closing bracket (the `[` is then a literal).
    Raises `_Dangerous` for POSIX classes, collating symbols and equivalence
    classes (`[[:punct:]]`, `[[.x.]]`, `[[=x=]]`: not modelled) and treats a
    reversed range as able to match anything. Linear in the text."""
    size = len(variant)
    index = start + 1
    negated = index < size and variant[index] in "!^"       # `^` negates in shells (fnmatch reads it literally)
    if negated:
        index += 1
    members: set[str] = set()
    covers_dot = False
    unknown = False
    first = True
    while index < size:
        char = variant[index]
        if char == "]" and not first:
            has_dot = covers_dot or "." in members
            if unknown:
                return index + 1, True, negated
            return index + 1, (not has_dot) if negated else has_dot, negated
        first = False
        if char == "[" and index + 1 < size and variant[index + 1] in ":.=":
            raise _Dangerous
        if index + 2 < size and variant[index + 1] == "-" and variant[index + 2] != "]":
            low, high = char, variant[index + 2]
            if low > high:
                unknown = True                          # a reversed range: shells differ
            elif low <= "." <= high:
                covers_dot = True
            index += 3
            continue
        members.add(char)
        index += 1
    return None


def _can_be_parent(variant: str) -> bool:
    """Whether one brace-free pattern can match the two-character name `..`.

    The pattern is read as a shell reads it: `*` any run, `?` any one
    character, `[...]` a bracket expression (`!` or `^` negates), everything
    else literal. A shell never lets a *leading* `*`, `?` or negated class
    match a leading period (`.` and `..` must be matched explicitly, also
    under bash's `dotglob`), so such an alternative cannot be `..`; a literal
    dot or a positive class first can."""
    tokens: list[tuple[str, bool]] = []   # ("lit", is_dot) | ("any", _) | ("star", _) | ("cls", may_dot)
    index = 0
    size = len(variant)
    first_wild = False
    while index < size:
        char = variant[index]
        if char == "*":
            if not tokens:
                first_wild = True
            if not tokens or tokens[-1][0] != "star":
                tokens.append(("star", False))
            index += 1
        elif char == "?":
            if not tokens:
                first_wild = True
            tokens.append(("any", False))
            index += 1
        elif char == "[":
            parsed = _class_may_match_dot(variant, index)
            if parsed is None:
                tokens.append(("lit", False))
                index += 1
            else:
                index, may_dot, negated = parsed
                if not tokens and negated:
                    first_wild = True
                tokens.append(("cls", may_dot))
        else:
            tokens.append(("lit", char == "."))
            index += 1
    if first_wild:
        return False
    reachable = {0}
    for kind, flag in tokens:
        step: set[int] = set()
        for pos in reachable:
            if kind == "star":
                step.update(range(pos, 3))
            elif pos < 2 and (kind == "any" or flag):
                step.add(pos + 1)
        reachable = step
        if not reachable:
            return False
    return 2 in reachable


def _may_match_parent(component: str, budget: _Budget) -> bool:
    """Whether a glob component can match the name `..` under some shell, or
    cannot be analysed within the budget. A True is a refusal.

    * a component longer than `MAX_GLOB_COMPONENT_CHARS` is not analysed;
    * an extended-glob operator (`@(..|a)`, `+(.)`, `!(x)`, `?(.)`, `*(.)`) or
      a POSIX class is not modelled;
    * otherwise its brace alternatives (at most `budget`: `MAX_BRACE_ALTERNATIVES`
      and `MAX_EXPANDED_CHARS` per path) are each read as a shell pattern and
      refused when one can be `..` (`_can_be_parent`): `{..,a}`, `.*`, `.?`,
      `.[.]`, `.[^a]`, `[.][.]`, `{.,x}{.,y}`, `.{*,a}`.

    `*.*.*`, `data.*.json`, `v*.*.*`, `*..*`, `*.md`, `[!_]*` are not refused:
    they begin with a wildcard or a literal other than a dot and cannot be `..`."""
    if len(component) > MAX_GLOB_COMPONENT_CHARS:
        return True
    if _EXTGLOB.search(component):
        return True
    try:
        return any(_can_be_parent(variant) for variant in _alternatives(component, budget))
    except _Dangerous:
        return True


def normalize_path(raw: object) -> str:
    """The canonical spelling of a declared repository path.

    Refuses (`ConflictInputError`) whatever cannot be a clean path: a non-`str`,
    the empty string, an ASCII control character (a trailing newline
    included), a leading or trailing space, a backslash, a leading `/` or `:`,
    a lone surrogate, more than `MAX_PATH_BYTES` UTF-8 bytes, a `..` component
    *anywhere* (behind a glob too: the widened directory would no longer
    contain the target), a component of only dots and spaces (`...`, `.. `),
    more than `MAX_DEPTH` components; shell syntax that
    is not a path (a leading `~`, a `$` or a backtick, a quote, a drive
    letter such as `C:`); every brace construct that is not a simple comma
    list inside one component (a group that spans `/`, `{}`, `{a}`, a nested
    group, `{1..3}`: decided on the whole string, see `_check_braces`); and
    **every glob component that may match `..`** under some shell, wherever it
    stands (see `_may_match_parent`: `{..,a}`, `{.,..}`, `[.][.]`, `..*`,
    `.*`, `.?`, `.[.]`, `.[^a]`, `.{.,a}`, `{.,x}{.,y}`,
    `[[:punct:]][[:punct:]]`, `@(..|a)`), because the widened directory would
    no longer contain what the pattern can reach.

    Otherwise empty and `.` components are dropped and the path is cut at the
    first component that holds a glob character, so a pattern is widened to
    the directory before it and a literal component behind a glob never
    narrows it again. `.`, `./` and a pattern whose first component is a glob
    give `""`, the repository root, which contains everything.
    """
    if not isinstance(raw, str):
        raise ConflictInputError(f"a path must be a str, not {_type_name(raw)}")
    try:
        text = str.__str__(raw)
    except Exception:
        raise ConflictInputError("a path must be a str, not an object that only claims to be one") from None
    if not text:
        raise ConflictInputError("a path must not be empty")
    if len(text) > MAX_PATH_BYTES:
        raise ConflictInputError(f"a path may hold at most {MAX_PATH_BYTES} UTF-8 bytes")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ConflictInputError("a path must be encodable as UTF-8") from None
    if size > MAX_PATH_BYTES:
        raise ConflictInputError(f"a path may hold at most {MAX_PATH_BYTES} UTF-8 bytes")
    if _ASCII_CONTROL.search(text):
        raise ConflictInputError("a path must not hold an ASCII control character")
    if text[0] == " " or text[-1] == " ":
        raise ConflictInputError("a path must not start or end with a space")
    if "\\" in text:
        raise ConflictInputError("a path must not hold a backslash")
    if text[0] == "/":
        raise ConflictInputError("a path must be relative to the repository, not absolute")
    if text[0] == ":":
        raise ConflictInputError("a path must not start with a colon")
    if text[0] == "~":
        raise ConflictInputError("a path must not start with a tilde (home expansion)")
    if "$" in text or "`" in text:
        raise ConflictInputError("a path must not hold a shell expansion character ($ or a backtick)")
    if "'" in text or '"' in text:
        raise ConflictInputError("a path must not hold a quote (shell quoting is not a path)")
    if len(text) > 1 and text[1] == ":" and text[0].isascii() and text[0].isalpha():
        raise ConflictInputError("a path must not start with a drive letter")
    _check_braces(text)
    parts = text.split("/")
    if ".." in parts:
        raise ConflictInputError("a path must not hold a '..' component")
    budget = _Budget()
    kept: list[str] = []
    cut = False
    for part in parts:
        if part in ("", "."):
            continue
        if not part.strip(". "):
            raise ConflictInputError("a path component of only dots and spaces may name the parent "
                                     "directory (Windows drops trailing dots and spaces)")
        has_glob = not _GLOB_CHARS.isdisjoint(part)
        if (has_glob or _EXTGLOB.search(part)) and _may_match_parent(part, budget):
            raise ConflictInputError("a glob component may match '..' or is too large to analyse "
                                     "(the widened directory would no longer contain it)")
        if cut:
            continue
        if has_glob:
            cut = True
            continue
        kept.append(part)
    if len(kept) > MAX_DEPTH:
        raise ConflictInputError(f"a path may hold at most {MAX_DEPTH} components")
    return "/".join(kept)


def _fold(component: str) -> str:
    """Canonical caseless matching: NFD(casefold(NFD(c))). NFC-then-casefold
    would split canonically equivalent names that a case-insensitive checkout
    merges."""
    decomposed = unicodedata.normalize("NFD", component)
    return unicodedata.normalize("NFD", decomposed.casefold())


def _key(canonical: str) -> tuple[str, ...]:
    """The comparison key of a canonical spelling: its folded components. The
    repository root is the empty key."""
    if not canonical:
        return ()
    return tuple(_fold(part) for part in canonical.split("/"))


# --------------------------------------------------------------------------- #
# footprints
# --------------------------------------------------------------------------- #

def _has_forbidden(text: str) -> bool:
    """Whether an item id holds a control, format, surrogate or line/paragraph
    separator character (U+2028 would forge a report line, U+202E reorder it,
    a lone surrogate cannot be written as UTF-8)."""
    return any(unicodedata.category(ch) in _FORBIDDEN_ID_CATEGORIES for ch in text)


def _refuse_item(item: object, problems: tuple[str, ...]) -> str:
    """The item id of a hand-built footprint as an exact `str`, or a refusal."""
    text = _plain(item)
    if text is None:
        raise ConflictInputError(f"an item id must be a str, not {_type_name(item)}")
    if len(text) > MAX_ITEM_CHARS:
        raise ConflictInputError(f"an item id may hold at most {MAX_ITEM_CHARS} characters")
    if _has_forbidden(text):
        raise ConflictInputError("an item id must not hold a control, format, surrogate "
                                 "or separator character")
    if not text and not problems:
        raise ConflictInputError("an item id must not be empty unless a problem says why")
    return text


def _refuse_side(name: str, side: object) -> tuple[tuple[tuple[str, ...], str], ...]:
    """The entries of a hand-built `writes` or `reads`, rebuilt as exact
    values: each spelling canonical and each key the key of its spelling."""
    if type(side) is not tuple:
        raise ConflictInputError(f"{name} must be a tuple")
    if len(side) > MAX_PATHS:
        raise ConflictInputError(f"{name} may hold at most {MAX_PATHS} entries")
    rebuilt = []
    for entry in side:
        if type(entry) is not tuple or len(entry) != 2:
            raise ConflictInputError(f"every entry of {name} must be a (key, spelling) pair")
        key, spelling = entry
        if not isinstance(spelling, str):
            raise ConflictInputError(f"a spelling in {name} must be a str")
        if type(key) is not tuple or not all(isinstance(part, str) for part in key):
            raise ConflictInputError(f"a key in {name} must be a tuple of str")
        try:
            plain = str.__str__(spelling)
            plain_key = tuple(str.__str__(part) for part in key)
            if plain:
                canonical = normalize_path(plain) == plain
            else:
                canonical = True
            if not canonical:
                raise ConflictInputError(f"a spelling in {name} is not canonical")
            if plain_key != _key(plain):
                raise ConflictInputError(f"a key in {name} is not the key of its spelling")
        except ConflictInputError:
            raise
        except Exception:
            raise ConflictInputError(f"an entry of {name} could not be checked") from None
        rebuilt.append((plain_key, plain))
    return tuple(rebuilt)


@dataclass(frozen=True)
class Footprint:
    """What one item declares it will write and read, in comparable form.

    `writes` and `reads` hold `(key, canonical spelling)` pairs, de-duplicated
    and sorted; `problems` says why a declaration could not be trusted. Build
    one with `footprint` or `footprint_of`, which never raise. A hand-built
    footprint is validated here: it cannot claim a spelling that
    normalisation would not produce, or a key that does not belong to it.
    """

    item: str
    writes: tuple
    reads: tuple
    problems: tuple = ()

    def __post_init__(self) -> None:
        problems = self.problems
        if type(problems) is not tuple:
            raise ConflictInputError("problems must be a tuple of str")
        plain = tuple(_plain(p) for p in problems)
        if any(p is None for p in plain):
            raise ConflictInputError("problems must be a tuple of str")
        problems = plain
        object.__setattr__(self, "problems", problems)
        object.__setattr__(self, "item", _refuse_item(self.item, problems))
        object.__setattr__(self, "writes", _refuse_side("writes", self.writes))
        object.__setattr__(self, "reads", _refuse_side("reads", self.reads))


class _Unreadable:
    """A declared field that could not even be fetched (a node lacking it)."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason


class _Problems:
    """The problems of one footprint, cut at `MAX_PROBLEMS`."""

    def __init__(self) -> None:
        self._kept: list[str] = []
        self._more = 0

    def add(self, text: str) -> None:
        if len(self._kept) < MAX_PROBLEMS:
            self._kept.append(text)
        else:
            self._more += 1

    def done(self) -> tuple[str, ...]:
        out = list(self._kept)
        if self._more:
            out.append(f"{self._more} more problems not listed")
        return tuple(out)


def _read_item(raw: object, problems: _Problems) -> str:
    if type(raw) is _Unreadable:
        problems.add(f"item id: {raw.reason}")
        return ""
    text = _plain(raw)
    if text is not None and text and len(text) <= MAX_ITEM_CHARS and not _has_forbidden(text):
        return text
    problems.add(
        f"item id {_describe(raw)} is not a non-empty string of at most "
        f"{MAX_ITEM_CHARS} characters without control, format, surrogate "
        "or separator characters"
    )
    return ""


def _read_side(label: str, raw: object, problems: _Problems) -> dict[tuple[str, ...], str]:
    """The paths of one declared side, one spelling per key (the smaller
    string, so the result never depends on iteration order). Every refusal is a
    problem; the refused entry is dropped."""
    found: dict[tuple[str, ...], str] = {}
    if type(raw) is _Unreadable:
        problems.add(f"{label}: {raw.reason}")
        return found
    try:
        if isinstance(raw, (str, bytes, bytearray, memoryview)):
            problems.add(f"{label} is a {_type_name(raw)}, not a collection of paths")
            return found
        iterator = iter(raw)
    except Exception:
        problems.add(f"{label} is a {_type_name(raw)}, not an iterable of paths")
        return found
    try:
        entries = list(islice(iterator, MAX_PATHS + 1))
    except Exception as exc:
        problems.add(f"{label}: reading the declaration raised {_type_name(exc)}")
        return found
    if len(entries) > MAX_PATHS:
        problems.add(f"{label} declares more than {MAX_PATHS} paths")
        del entries[MAX_PATHS:]
    for entry in entries:
        try:
            spelling = normalize_path(entry)
        except ConflictInputError as exc:
            problems.add(f"{label}: {_describe(entry)} refused: {exc}")
            continue
        except Exception:
            problems.add(f"{label}: {_describe(entry)} could not be read")
            continue
        key = _key(spelling)
        prior = found.get(key)
        if prior is None or spelling < prior:
            found[key] = spelling
    return found


def _build(item: object, writes: object, reads: object) -> Footprint:
    problems = _Problems()
    try:
        name = _read_item(item, problems)
        write_side = _read_side("writes", writes, problems)
        read_side = _read_side("reads", reads, problems)
        return Footprint(
            item=name,
            writes=tuple(sorted(write_side.items())),
            reads=tuple(sorted(read_side.items())),
            problems=problems.done(),
        )
    except Exception:
        return Footprint(
            item="", writes=(), reads=(),
            problems=("the declaration could not be read",),
        )


def footprint(item: object, writes: object, reads: object) -> Footprint:
    """The footprint of one item's declaration. Never raises on any argument
    (it catches `Exception`, not `BaseException`): what cannot be read is
    recorded in `problems` and the entry dropped, and the id of a footprint
    with a bad id is `""`."""
    return _build(item, writes, reads)


def _attribute(node: object, name: str) -> object:
    try:
        return getattr(node, name)
    except AttributeError:
        return _Unreadable(f"node attribute '{name}' is missing")
    except Exception as exc:
        return _Unreadable(f"reading node attribute '{name}' raised {_type_name(exc)}")


def footprint_of(node: Any) -> Footprint:
    """The footprint of a plan node, read by duck typing: `node.id`,
    `node.writes`, `node.semantic_reads`. `produces` and `invalidates` are not
    consulted. Never raises; a missing or failing attribute is a problem."""
    return _build(
        _attribute(node, "id"),
        _attribute(node, "writes"),
        _attribute(node, "semantic_reads"),
    )


# --------------------------------------------------------------------------- #
# the relation
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Conflict:
    """One reason two items must not run in parallel.

    For WRITE_READ `writer` writes what `other` reads: `path` is the writer's
    spelling and `other_path` the reader's. For WRITE_WRITE and UNDECIDABLE the
    two ids are in id order whichever way `detect` was asked. `path` and
    `other_path` are the canonical spellings that met (`""` for UNDECIDABLE,
    whose `detail` carries the reasons).
    """

    kind: Kind
    writer: str
    other: str
    path: str
    other_path: str
    detail: str = ""

    def __str__(self) -> str:
        writer, other = _escaped(self.writer), _escaped(self.other)
        if self.kind == Kind.WRITE_WRITE:
            return (f"WRITE_WRITE: {writer} writes {_escaped(self.path)!r}; "
                    f"{other} also writes {_escaped(self.other_path)!r}")
        if self.kind == Kind.WRITE_READ:
            return (f"WRITE_READ: {writer} writes {_escaped(self.path)!r}; "
                    f"{other} reads {_escaped(self.other_path)!r}")
        return f"UNDECIDABLE {writer} <-> {other}: {_escaped(self.detail)}"


def _side(entries: tuple) -> dict[tuple[str, ...], str]:
    """A side as one spelling per key (the smaller string)."""
    out: dict[tuple[str, ...], str] = {}
    for key, spelling in entries:
        prior = out.get(key)
        if prior is None or spelling < prior:
            out[key] = spelling
    return out


def _order(fp: Footprint) -> tuple:
    return (fp.item, fp.problems, fp.writes, fp.reads)


def _undecidable(first: Footprint, second: Footprint) -> str:
    """Why the pair cannot be decided, or `""`."""
    reasons: list[str] = []
    if first.item == second.item:
        reasons.append(f"both items are called {first.item!r}")
    for fp in (first, second):
        if fp.problems:
            listed = "; ".join(
                _printable(p, _PROBLEM_CHARS) for p in fp.problems[:MAX_PROBLEMS + 1]
            )
            reasons.append(f"item {fp.item!r} cannot be trusted: {listed}")
        elif not fp.writes and not fp.reads:
            reasons.append(f"item {fp.item!r} declares no path (an omission is not independence)")
    return "; ".join(reasons)


def _nested(left: dict, right: dict) -> list[tuple[tuple, str, tuple, str]]:
    """Every `(left key, left spelling, right key, right spelling)` whose two
    keys are nested (one a component-wise prefix of the other, equality
    included). A walk over the prefixes of each key against the other side's
    set, never a pairwise product."""
    pairs: dict[tuple[str, str], tuple] = {}
    for key, spelling in left.items():
        for n in range(len(key) + 1):
            other = right.get(key[:n])
            if other is not None:
                pairs[(spelling, other)] = (key, spelling, key[:n], other)
    for key, spelling in right.items():
        for n in range(len(key) + 1):
            other = left.get(key[:n])
            if other is not None:
                pairs[(other, spelling)] = (key[:n], other, key, spelling)
    return list(pairs.values())


def _relation(left_key: tuple, right_key: tuple, left_name: str, right_name: str) -> str:
    if left_key == right_key:
        return "the same path"
    if len(left_key) < len(right_key):
        return f"the {left_name}'s path contains the {right_name}'s"
    return f"the {right_name}'s path contains the {left_name}'s"


def detect(a: Footprint, b: Footprint) -> tuple[Conflict, ...]:
    """Why `a` and `b` must not run in parallel; `()` when they may.

    Raises `TypeError` when an argument is not a `Footprint` (loud, never a
    quiet "independent"). Otherwise exactly one UNDECIDABLE conflict when the
    two ids are equal, either footprint has problems, or either has neither a
    write nor a read. Otherwise WRITE_WRITE for each nested pair of a write of
    one item and a write of the other, and WRITE_READ for each nested pair of a
    write of one item and a read of the other, in both directions; read/read is
    never a conflict. Sorted by `(kind, writer, other, path, other_path)` and
    cut to `MAX_EVIDENCE` *after* sorting, so "conflict or not" never depends
    on the cut. Symmetric: `detect(a, b) == detect(b, a)`.
    """
    if not isinstance(a, Footprint) or not isinstance(b, Footprint):
        raise TypeError(
            "detect compares two Footprint objects, not "
            f"{_type_name(a)} and {_type_name(b)}"
        )
    first, second = sorted((a, b), key=_order)
    why = _undecidable(first, second)
    if why:
        return (Conflict(Kind.UNDECIDABLE, first.item, second.item, "", "", why),)

    first_writes, first_reads = _side(first.writes), _side(first.reads)
    second_writes, second_reads = _side(second.writes), _side(second.reads)
    found: list[Conflict] = []
    for lk, ls, rk, rs in _nested(first_writes, second_writes):
        found.append(Conflict(
            Kind.WRITE_WRITE, first.item, second.item, ls, rs,
            _relation(lk, rk, "first", "second"),
        ))
    for lk, ls, rk, rs in _nested(first_writes, second_reads):
        found.append(Conflict(
            Kind.WRITE_READ, first.item, second.item, ls, rs,
            _relation(lk, rk, "writer", "reader"),
        ))
    for lk, ls, rk, rs in _nested(second_writes, first_reads):
        found.append(Conflict(
            Kind.WRITE_READ, second.item, first.item, ls, rs,
            _relation(lk, rk, "writer", "reader"),
        ))
    found.sort(key=lambda c: (c.kind.value, c.writer, c.other, c.path, c.other_path))
    return tuple(found[:MAX_EVIDENCE])
