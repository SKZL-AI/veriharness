"""Derived reads (P3-21 slices 2 and 3): the repository files a work item's Python files depend on.

`conflict.detect` compares what two work items *declare*. A node that declares `src/hoh/a.py` as its
write and says nothing about the module `a.py` imports is independent of a node that rewrites that
module -- and the two merge into a broken tree (lesson L13, and the failure `TaskNode.semantic_reads`
was invented for). This module derives the missing reads from the code: for every declared Python
file that exists, the repository files it depends on.

What counts as a dependency (slice 3 completes the three kinds the register row names):

* **Imports, to a closure.** The repository files its `import` statements -- and the calls of `import_module`
  / `__import__` with a literal absolute module name -- name, **with the `__init__.py` of every package on
  the way** (`import pkg.sub.mod` runs `pkg/__init__.py` and `pkg/sub/__init__.py`), then the files
  *those* import, and so on, up to `MAX_IMPORT_DEPTH` levels and `MAX_CLOSURE_FILES` files; a closure
  that is bigger or deeper is a *problem* (fail closed), never a truncated list. A cycle terminates.
* **Data files named by a literal.** Every string constant that can be a repository path -- characters of a
  file name only, no whitespace, a `/` or a file extension, whatever calls it (`open`, `pandas.read_csv`,
  `shutil.copy`, a `subprocess` or `runpy` script path, a table of names) -- and the literal operands of a
  `Path(...)` / `open(...)` / `/` expression (`Path("data") / "x.json"`, `BASE / "data" / "x.json"` with any
  base, `Path(__file__).parent / "x.json"`) is looked for beside the file that holds it and from the
  repository root; every existing regular file it names is a read. A constant that names a directory, a
  path built at run time (a variable, an f-string, `os.path.join`, `importlib.resources`, an environment
  variable), an absolute path and a file that does not exist yet (another node is going to create it) name
  nothing here.
* **A declared dependency manifest.** A file that imports a name the repository does not hold and the
  standard library does not either (a package installed from outside) reads the manifests the repository
  keeps for its dependencies (`MANIFESTS`: `pyproject.toml`, `requirements*.txt`, `setup.py`,
  `setup.cfg`), those that exist. It is over-approximate on purpose: a manifest write meets every node that
  uses any outside package, because which distribution provides which import name is not decidable here.

A declared **directory or glob** is not skipped: `conflict.footprint` widens a glob to the directory
before it, and writing a directory nests over every path beneath it, but that says nothing about what
the files *beneath it import from outside it* (a node that rewrites `src/pkg/` and a node that rewrites
`src/util.py`, which `src/pkg/a.py` imports, would be admitted together -- the L13 failure). So the
caller's `expand(path)` lists the existing `.py` files beneath a directory and each of them is read
like a declared file.

Three rules, as in `conflict.py`:

* **Fail closed.** A declared Python file that exists and cannot be read as source (not UTF-8, a NUL,
  a syntax error, larger than `MAX_SOURCE_BYTES`, more than `MAX_IMPORTS` import statements, an
  unreadable file, more than `MAX_DECLARED_PY` of them on one item), an imported file that cannot be
  read the same way, a closure beyond its bounds, a directory that cannot be listed
  or holds more than `MAX_EXPANDED_PY` Python files, and a path that still holds a glob character are
  *problems*; the caller turns a problem into an UNDECIDABLE footprint, which conflicts with
  everything. A file or directory that does not exist is something the item is going to create: it
  has no imports yet, which is not a problem.
* **Bounded.** Every number below is a ceiling on work, not on correctness: the answer for a bigger
  input is a problem, never a truncated list of reads.
* **Pure.** No filesystem access here: the caller hands in `read_text(path)`, `exists(path)` and
  `expand(path)`. `parse_imports` is pure in the text; the resolution is pure in `exists` (and in the
  interpreter's `sys.stdlib_module_names`, which tells an outside package from the standard library).

What it is not: it does not evaluate `sys.path` or an installed package (limit L3: a name resolves to a
repository file, to a manifest read, or to nothing), runs no code (an import whose module name is built at
run time is not an import here), and does not read what a run *writes* -- the candidate changes of a
node are the launcher's (`HohRunLauncher._candidate_changes`).
"""

from __future__ import annotations

import ast
import posixpath
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass

__all__ = [
    "MAX_DECLARED_PY", "MAX_SOURCE_BYTES", "MAX_IMPORTS", "MAX_CANDIDATES", "MAX_DERIVED",
    "MAX_EXPANDED_PY", "MAX_WALK_ENTRIES", "MAX_WALK_DEPTH", "MAX_IMPORT_DEPTH", "MAX_CLOSURE_FILES",
    "MAX_LITERALS", "MAX_LITERAL_CHARS", "MANIFESTS", "SKIPPED_DIRECTORIES", "ROOTS", "Imports",
    "Derived", "parse_imports", "resolve_imports", "derive",
]

#: Declared Python files per item whose imports are read.
MAX_DECLARED_PY = 64
#: Characters of one source file. (256 KiB until slice 3: the closure reads what is imported, and this
#: repository's own `src/hoh/cli.py` is 281 KB -- under the old ceiling every file that imports it, directly
#: or not, was a problem: 22 of the 109 modules of `src/hoh` and 128 of the 243 test files.)
MAX_SOURCE_BYTES = 512 * 1024
#: Import statements read from one file.
MAX_IMPORTS = 512
#: Paths asked of `exists` for one item (16384 until slice 3: a closure asks for the imports of up to
#: `MAX_CLOSURE_FILES` more files; each distinct path is asked of the caller once).
MAX_CANDIDATES = 65536
#: Derived reads of one item.
MAX_DERIVED = 1000
#: Python files found beneath the declared directories and globs of one item.
MAX_EXPANDED_PY = 128
#: Directory entries examined while listing the declared directories of one item.
MAX_WALK_ENTRIES = 2048
#: Directory levels descended beneath a declared directory.
MAX_WALK_DEPTH = 32
#: Import levels followed beyond a declared file: its imports are level 1, theirs level 2, and so on.
#: A closure with a file at level `MAX_IMPORT_DEPTH + 1` is a problem.
MAX_IMPORT_DEPTH = 8
#: Repository Python files read only because something imports them, per item.
MAX_CLOSURE_FILES = 128
#: Path literals read from one file.
MAX_LITERALS = 512
#: A literal longer than this names nothing (a path of the platform is shorter).
MAX_LITERAL_CHARS = 4096
#: The files a repository keeps its outside dependencies in: read by a file that imports a package it
#: does not hold itself.
MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg")
#: Directory names whose contents are not repository sources.
SKIPPED_DIRECTORIES = frozenset({".git", "__pycache__"})
#: Where an absolute import is looked for, besides the importing file's own directory (tests import
#: their siblings by bare name). Most specific first is irrelevant: every hit is a read.
ROOTS = ("src", "")

_GLOB = frozenset("*?[]{}")
#: Callees whose literal argument names a path.
#: Callees that import the module their literal first argument names (`importlib.import_module("a.b")`).
_IMPORTERS = frozenset({"import_module", "__import__"})
#: A string constant that can be a repository path whatever calls it: characters of a file name only,
#: no whitespace, and a `/` or a file extension. Over-approximate on purpose (`"a.b"` is a module name
#: and also a path); a candidate that is no file reads nothing.
_PATHLIKE = re.compile(r"[A-Za-z0-9_.@+~/-]+")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}\Z")
_PATH_OPENERS = frozenset({"open"})
_PATH_TYPES = frozenset({"Path", "PurePath", "PosixPath", "PurePosixPath"})
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())


@dataclass(frozen=True)
class Imports:
    """What one file imports, as written: `("abs", module, names)` or
    `("rel", level, module, names)`; `names` are the `from` aliases (`*` dropped)."""

    path: str
    specs: tuple
    problems: tuple = ()
    #: Path literals, as written (`open("x")`, `Path("a", "b")`, `... / "c"`), sorted and de-duplicated.
    data: tuple = ()


@dataclass(frozen=True)
class Derived:
    """`reads`: sorted, de-duplicated canonical paths. `problems`: why the answer cannot be trusted."""

    reads: tuple
    problems: tuple = ()
    #: The members of `reads` that are Python files an `import` named (the ones whose own imports
    #: are followed next); the rest are data files and manifests.
    imported: tuple = ()


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _spelled(node: ast.expr) -> str | None:
    """The path a literal expression spells: `"x"`, `Path("a", "b")`, `Path("a") / "b" / "c"`; None
    for anything that is not built from literals alone."""
    text = _string(node)
    if text is not None:
        return text
    if isinstance(node, ast.Call) and _callee(node.func) in _PATH_TYPES and node.args:
        parts = [_string(a) for a in node.args]
        return posixpath.join(*parts) if all(p is not None for p in parts) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _spelled(node.left), _string(node.right)
        if left is not None and right is not None:
            return posixpath.join(left, right)
    return None


def _pathlike(text: str) -> bool:
    return (0 < len(text) <= 200 and _PATHLIKE.fullmatch(text) is not None and not text.startswith("-")
            and ("/" in text or _EXTENSION.search(text) is not None))


def _suffix(node: ast.expr) -> str | None:
    """The literal tail of a `/` chain whose head may be anything: `BASE / "data" / "x.json"` is
    `data/x.json`, `Path("a") / "b"` is `a/b`; None when the last operand is not a literal."""
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        operand = _string(node.right)
        if operand is None:
            break
        parts.append(operand)
        node = node.left
    else:
        head = _spelled(node)
        if head is not None:
            parts.append(head)
    return posixpath.join(*reversed(parts)) if parts else None


def _literals(node: ast.AST) -> list[str]:
    """The path literals this one AST node holds (its children are visited by the caller)."""
    out: list[str] = []
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and _pathlike(node.value):
            out.append(node.value)
    elif isinstance(node, ast.Call):
        name = _callee(node.func)
        if name in _PATH_OPENERS and node.args:
            spelled = _spelled(node.args[0])
            if spelled is not None:
                out.append(spelled)
        elif name in _PATH_TYPES and node.args:
            spelled = _spelled(node)
            if spelled is not None:
                out.append(spelled)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        tail = _suffix(node)
        if tail is not None:
            out.append(tail)
    return out


def parse_imports(path: str, text: object) -> Imports:
    """The import statements of one Python file (inside functions and `try` too: a lazy import is
    still a dependency). A source that cannot be read is a problem, not an empty list."""
    label = ascii(path)[:120]
    if not isinstance(text, str):
        return Imports(path, (), (f"{label}: the source is not text",))
    if len(text) > MAX_SOURCE_BYTES:
        return Imports(path, (), (f"{label}: larger than {MAX_SOURCE_BYTES} characters",))
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return Imports(path, (), (f"{label}: not Python source ({type(exc).__name__})",))
    specs: list[tuple] = []
    data: set[str] = set()
    for node in ast.walk(tree):
        data.update(_literals(node))
        if len(data) > MAX_LITERALS:
            return Imports(path, (), (f"{label}: more than {MAX_LITERALS} path literals",))
        if isinstance(node, ast.Import):
            for alias in node.names:
                specs.append(("abs", alias.name, ()))
        elif isinstance(node, ast.Call) and _callee(node.func) in _IMPORTERS and node.args:
            named = _string(node.args[0])
            if named and not named.startswith("."):
                specs.append(("abs", named, ()))
        elif isinstance(node, ast.ImportFrom):
            names = tuple(a.name for a in node.names if a.name != "*")
            if node.level:
                specs.append(("rel", node.level, node.module or "", names))
            else:
                specs.append(("abs", node.module or "", names))
        if len(specs) > MAX_IMPORTS:
            return Imports(path, (), (f"{label}: more than {MAX_IMPORTS} import statements",))
    return Imports(path, tuple(specs), data=tuple(sorted(data)))


def _module_files(base: str, parts: list[str]) -> list[str]:
    stem = posixpath.join(base, *parts) if parts else base
    if not parts:
        return [posixpath.join(base, "__init__.py")] if base else ["__init__.py"]
    return [stem + ".py", posixpath.join(stem, "__init__.py")]


def _candidates(path: str, spec: tuple) -> list[str]:
    """The repository paths one import statement may name (all of them are asked of `exists`)."""
    here = posixpath.dirname(path)
    out: list[str] = []
    if spec[0] == "abs":
        _, module, names = spec
        parts = [p for p in module.split(".") if p] if module else []
        for root in dict.fromkeys((*ROOTS, here)):
            out += _module_files(root, parts)
            for name in names:
                out += _module_files(root, [*parts, name])
        return out
    _, level, module, names = spec
    base = here
    for _ in range(level - 1):
        if not base:
            return out                       # beyond the repository root: names nothing in it
        base = posixpath.dirname(base)
    parts = [p for p in module.split(".") if p] if module else []
    out += _module_files(base, parts)
    for name in names:
        out += _module_files(base, [*parts, name])
    return out


def _parent_inits(path: str, spec: tuple) -> list[str]:
    """The `__init__.py` files Python runs on the way to the module an import names: those of the
    packages above it (`import pkg.sub.mod` runs `pkg/__init__.py` and `pkg/sub/__init__.py`), and, for
    a relative import, the importing file's own package. They are dependencies too: a write to
    `pkg/__init__.py` changes what every importer of `pkg.x` gets."""
    here = posixpath.dirname(path)
    out: list[str] = []
    if spec[0] == "abs":
        _, module, _names = spec
        parts = [p for p in module.split(".") if p] if module else []
        for root in dict.fromkeys((*ROOTS, here)):
            for k in range(1, len(parts)):
                out.append(posixpath.join(root, *parts[:k], "__init__.py"))
        return out
    _, level, module, _names = spec
    base = here
    for _ in range(level - 1):
        if not base:
            return out
        base = posixpath.dirname(base)
    parts = [p for p in module.split(".") if p] if module else []
    out.append(posixpath.join(base, "__init__.py") if base else "__init__.py")
    for k in range(1, len(parts)):
        out.append(posixpath.join(base, *parts[:k], "__init__.py"))
    return out


def _outside(module: str) -> bool:
    """Whether `module` (an absolute import) names a package from outside: its top-level name is not
    in the standard library of the running interpreter. A name the repository holds is asked of
    `exists` first; this is only consulted for a name that resolved to nothing."""
    top = module.split(".")[0]
    return bool(top) and top not in _STDLIB


def _data_candidates(path: str, literal: str) -> list[str]:
    """The repository paths a path literal of the file `path` may name: from the repository root and
    beside the file. An absolute literal, an empty one, one with a NUL and one longer than a path can
    be name nothing in the repository."""
    if not literal or len(literal) > MAX_LITERAL_CHARS or "\0" in literal or literal.startswith("/"):
        return []
    here = posixpath.dirname(path)
    return [literal, posixpath.join(here, literal)] if here else [literal]


def resolve_imports(parsed: Imports, exists: Callable[[str], bool], budget: list[int]) -> Derived:
    """The existing repository files `parsed` depends on: the Python files its imports name
    (`imported`), the data files its path literals name, and -- when it imports a package the
    repository does not hold -- the dependency manifests. `budget[0]` is what the item may still
    ask of `exists`; it is spent here and a bigger need is a problem."""
    found: set[str] = set()                                    # Python files an import named
    other: set[str] = set()                                    # data files and manifests
    label = ascii(parsed.path)[:120]

    def stop(why: str) -> Derived:
        return Derived(tuple(sorted(found | other)), (f"{label}: {why}",), tuple(sorted(found)))

    def ask(candidate: str) -> tuple[str | None, str | None]:
        """(the path when it exists, why the question could not be answered)."""
        if budget[0] <= 0:
            return None, f"more than {MAX_CANDIDATES} candidate paths for one item"
        budget[0] -= 1
        clean = posixpath.normpath(candidate) if candidate else ""
        if not clean or clean == "." or clean.startswith(".."):
            return None, None
        try:
            hit = bool(exists(clean))
        except Exception:                                      # noqa: BLE001 - a problem, not a crash
            return None, f"asking whether {ascii(clean)[:120]} exists failed"
        return (clean if hit else None), None

    outside = False
    for spec in parsed.specs:
        resolved = False
        for candidate in _candidates(parsed.path, spec):
            clean, why = ask(candidate)
            if why is not None:
                return stop(why)
            if clean is not None:
                resolved = True
                if clean != parsed.path:
                    found.add(clean)
        if not resolved and spec[0] == "abs" and _outside(spec[1]):
            outside = True
        for candidate in _parent_inits(parsed.path, spec):
            clean, why = ask(candidate)
            if why is not None:
                return stop(why)
            if clean is not None and clean != parsed.path:
                found.add(clean)
    for literal in parsed.data:
        for candidate in _data_candidates(parsed.path, literal):
            clean, why = ask(candidate)
            if why is not None:
                return stop(why)
            if clean is not None and clean != parsed.path:
                other.add(clean)
    if outside:
        for manifest in MANIFESTS:
            clean, why = ask(manifest)
            if why is not None:
                return stop(why)
            if clean is not None and clean != parsed.path:
                other.add(clean)
    return Derived(tuple(sorted(found | other)), (), tuple(sorted(found)))


def derive(paths: Iterable[object], read_text: Callable[[str], str | None],
           exists: Callable[[str], bool],
           expand: Callable[[str], Iterable[str] | None],
           parse: Callable[[str, object], Imports] = parse_imports) -> Derived:
    """The derived reads of an item whose declared (canonical) paths are `paths`.

    For every path that ends in `.py` and holds no glob character: `read_text(path)` is the source
    (`None`: the file does not exist yet -- nothing to derive); its imports are resolved against
    `exists`. For every other path (a directory, a glob widened to its directory, `''` for the
    repository root, or a file that is not Python): `expand(path)` is the existing `.py` files
    beneath it, or `None` when it is not a directory -- those files are read the same way. A path
    that still holds a glob character is a problem (the caller widens globs first).

    **The closure (slice 3).** The Python files those declared files import are read in turn, level
    by level (a file is read once, so a cycle ends): their imports, path literals and manifests are
    reads too. A file at level `MAX_IMPORT_DEPTH + 1`, or more than `MAX_CLOSURE_FILES` files read
    only because something imports them, is a problem -- and so is any of those files that cannot be
    read as source. Data files and manifests are reads but are not read as source.

    `parse` is `parse_imports` or a caching wrapper of it. Never raises on `read_text` / `exists` /
    `expand` failing: that is a problem. `reads` is cut nowhere -- more than `MAX_DERIVED` is a
    problem."""
    reads: set[str] = set()
    problems: list[str] = []
    budget = [MAX_CANDIDATES]
    known: dict[str, bool] = {}                                # one answer per path per item

    def asked(path: str) -> bool:
        if path not in known:
            known[path] = bool(exists(path))
        return known[path]

    seen: set[str] = set()
    python_files = 0
    expanded_files = 0
    level: set[str] = set()                                    # imported by a declared file

    def read_one(path: str) -> tuple[str, ...]:
        """Reads one Python file; the Python files its imports name (the next level of the closure)."""
        try:
            text = read_text(path)
        except Exception as exc:                               # noqa: BLE001 - a problem, not a crash
            problems.append(f"{ascii(path)[:120]}: could not be read ({type(exc).__name__})")
            return ()
        if text is None:
            return ()
        parsed = parse(path, text)
        problems.extend(parsed.problems)
        resolved = resolve_imports(parsed, asked, budget)
        reads.update(resolved.reads)
        problems.extend(resolved.problems)
        return resolved.imported

    for raw in paths:
        if not isinstance(raw, str):
            problems.append("a declared path is not text: not read")
            continue
        path = raw
        if _GLOB & set(path):
            problems.append(f"{ascii(path)[:120]}: a glob is not read (widen it to its directory)")
            continue
        if path in seen:
            continue
        seen.add(path)
        if path.endswith(".py"):
            python_files += 1
            if python_files > MAX_DECLARED_PY:
                problems.append(f"more than {MAX_DECLARED_PY} declared Python files: not read")
                break
            level.update(read_one(path))
            continue
        try:
            listed = expand(path)
            members = None if listed is None else sorted(set(listed))
        except Exception as exc:                               # noqa: BLE001 - a problem, not a crash
            problems.append(f"{ascii(path)[:120]}: the directory could not be listed "
                            f"({type(exc).__name__})")
            continue
        if members is None:
            continue                                           # a file that is not Python, or nothing yet
        fresh = [m for m in members if m not in seen]
        expanded_files += len(fresh)
        if expanded_files > MAX_EXPANDED_PY:
            problems.append(f"more than {MAX_EXPANDED_PY} Python files beneath the declared "
                            "directories: not read")
            break
        for member in fresh:
            seen.add(member)
            level.update(read_one(member))
    closure_files = 0
    too_many = False
    for _ in range(MAX_IMPORT_DEPTH):
        following = sorted(p for p in level if p not in seen)
        level = set()
        for member in following:
            closure_files += 1
            if closure_files > MAX_CLOSURE_FILES:
                problems.append(f"more than {MAX_CLOSURE_FILES} files read only because something "
                                "imports them: the closure is not followed")
                too_many = True
                break
            seen.add(member)
            level.update(read_one(member))
        if too_many or not following:
            break
    if not too_many and any(p not in seen for p in level):
        problems.append(f"the import closure is deeper than {MAX_IMPORT_DEPTH} levels: not followed")
    if len(reads) > MAX_DERIVED:
        problems.append(f"more than {MAX_DERIVED} derived reads: not listed")
        reads = set()
    return Derived(tuple(sorted(reads)), tuple(problems))
