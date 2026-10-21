"""The acceptance criteria a specification declares, and whether a plan carries them as written.

Until now nothing compared the planner's `acceptance_checks` with the
specification. The planner re-typed the criteria, and in runs of this project it
(1) closed a quoted command with a stray backslash-quote, so six criteria could
not even start; (2) added a `--deselect` to a criterion that was red on the
basis, and the controller accepted the altered check; (3) wrapped criteria in
`python3 -c` calls to give pytest a base temporary directory; (4) left criteria
out while the run claimed that all were demonstrated. Each was found by a
reviewer who ran the specification's own text, never by the product.

A specification can now **declare** its criteria in one fenced block, and the
controller refuses a plan that does not carry every one of them verbatim:

    ```criteria
    K1 python3 -B -m pytest -q -p no:cacheprovider tests/test_x.py::test_a
    K2 python3 -B -m pytest -q -p no:cacheprovider tests/test_y.py
    ```

One line per criterion: the id (the `check_id` pattern), one or more spaces,
then the command, which is the rest of the line. There is no macro, no
continuation and no escaping, so there is nothing to expand or to re-type: the
line is the command. Only a fence that opens at the very start of a line counts,
and fences are read the way CommonMark reads them, so an example inside a longer
fence, in a tilde fence or in an indented list item is not a block.

A specification that declares no block is not checked, and says so: that is
`NOT_RUN`, never "passed". A block that cannot be read (two blocks, an
unterminated one, an empty one, a duplicate or invalid id, a line without a
command, a control character) is refused before any role is started.

The criteria name tests; they do not say which bytes those tests have. A
specification can therefore also **pin its test contract** in one more fenced
block, `test-pins` (FID1, below): the sha256 of every test file its criteria
run. The controller refuses a candidate whose test files are not those bytes --
before QA is asked, again at the acceptance, and `hoh import` once more on the
accepted tree.
"""

from __future__ import annotations

import ast
import hashlib
import posixpath
import re
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .roles import CRITERION_FIDELITY, RoleOutputError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .contracts import DevelopmentPlan

#: The `check_id` pattern of `contracts.AcceptanceCheck`.
_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_OPEN = re.compile(r"^(`{3,}|~{3,})(.*)$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: What "surrounding blanks" means: ASCII only. `str.strip()` also trims U+00A0,
#: U+2028, U+0085 and the like, which bash passes on as part of the last word.
_BLANKS = " \t\r\n"

#: The verdict name of a block that cannot be read.
SPEC_CRITERIA_UNREADABLE = "SPEC_CRITERIA_UNREADABLE"
NOT_DECLARED = (
    "criterion fidelity NOT_RUN: the specification declares no criteria block, "
    "so the plan's checks are not compared with it"
)


class CriteriaBlockError(ValueError):
    """The specification's criteria block cannot be read (fail closed)."""


class CriterionFidelityError(RoleOutputError):
    """The plan does not carry the specification's criteria as written."""


@dataclass(frozen=True)
class Criterion:
    criterion_id: str
    command: str


def _closes(line: str, char: str, length: int) -> bool:
    body = line.strip(" ")
    return (
        len(line) - len(line.lstrip(" ")) <= 3
        and len(body) >= length
        and set(body) == {char}
    )


def _fenced_blocks(
    spec_text: str, info_string: str, label: str, error: type = CriteriaBlockError,
    code: str = SPEC_CRITERIA_UNREADABLE,
) -> list[list[tuple[int, str]]]:
    """The bodies of the fenced blocks whose info string is `info_string`, as `(line number, line)`.

    Only a fence that opens at the very start of a line counts, and fences are read the way
    CommonMark reads them. A block that is not closed is refused (`label` names it)."""
    blocks: list[list[tuple[int, str]]] = []
    fence: tuple[str, int, list[tuple[int, str]] | None] | None = None
    for number, line in enumerate(spec_text.replace("\r\n", "\n").split("\n"), 1):
        if fence is None:
            opened = _OPEN.match(line)
            if opened is None:
                continue
            marker, info = opened.group(1), opened.group(2).strip(" \t")
            if marker[0] == "`" and "`" in info:
                continue                       # inline code, not a fence
            body = [] if (marker == "```" and info == info_string) else None
            fence = (marker[0], len(marker), body)
            continue
        char, length, body = fence
        if _closes(line, char, length):
            if body is not None:
                blocks.append(body)
            fence = None
        elif body is not None:
            body.append((number, line))
    if fence is not None and fence[2] is not None:
        raise error(f"{code}: the {label} block is not closed")
    return blocks


def declared_criteria(spec_text: str) -> tuple[Criterion, ...]:
    """The criteria the specification declares; `()` when it declares none."""
    blocks = _fenced_blocks(spec_text, "criteria", "criteria")
    if not blocks:
        return ()
    if len(blocks) > 1:
        raise CriteriaBlockError(
            f"{SPEC_CRITERIA_UNREADABLE}: the specification has {len(blocks)} criteria "
            "blocks; it must have exactly one, restated in full when it is revised")
    found: list[Criterion] = []
    for number, line in blocks[0]:
        if not line.strip(" "):
            continue
        if _CONTROL.search(line):
            raise CriteriaBlockError(
                f"{SPEC_CRITERIA_UNREADABLE}: line {number} of the criteria block "
                "contains a control character")
        head, _, rest = line.partition(" ")
        command = rest.strip(" ")
        if command != command.strip():
            raise CriteriaBlockError(
                f"{SPEC_CRITERIA_UNREADABLE}: the command on line {number} of the criteria "
                "block starts or ends with a space that is not an ASCII blank; the plan "
                "schema trims it, so no plan could ever carry the command as declared")
        if not _ID.match(head) or not command:
            raise CriteriaBlockError(
                f"{SPEC_CRITERIA_UNREADABLE}: line {number} of the criteria block is "
                "not '<id> <command>' with an id of letters, digits, '.', '_' or '-' "
                "(1 to 64 characters) and a command")
        if any(c.criterion_id == head for c in found):
            raise CriteriaBlockError(
                f"{SPEC_CRITERIA_UNREADABLE}: the criteria block declares {head} twice")
        found.append(Criterion(head, command))
    if not found:
        raise CriteriaBlockError(
            f"{SPEC_CRITERIA_UNREADABLE}: the criteria block declares no criterion")
    return tuple(found)


def fidelity_findings(
    declared: Iterable[Criterion], checks: Iterable, frozen: Iterable[str] = ()
) -> list[str]:
    """What is wrong with `checks` (a plan's acceptance checks) against `declared`.

    A criterion whose id is `frozen` -- already validated in an earlier iteration,
    so the persisted version runs and the plan's is discarded -- needs no entry.
    Checks under other ids are the planner's own and are not examined: they
    cannot stand in for a criterion, because the criterion itself must be there.
    """
    by_id = {c.check_id: c for c in checks}
    frozen = set(frozen)
    findings: list[str] = []
    for criterion in declared:
        if criterion.criterion_id in frozen:
            continue
        check = by_id.get(criterion.criterion_id)
        if check is None:
            findings.append(f"{criterion.criterion_id}: missing from the plan "
                            f"(the specification's command is: {criterion.command})")
        elif check.command.strip(_BLANKS) != criterion.command:
            findings.append(f"{criterion.criterion_id}: the command differs from the "
                            f"specification's, which is: {criterion.command}")
        elif check.expect_exit != 0:
            findings.append(f"{criterion.criterion_id}: expect_exit is {check.expect_exit}, "
                            "a criterion expects 0")
    return findings


def assert_faithful(
    declared: Iterable[Criterion], plan: "DevelopmentPlan", frozen: Iterable[str] = ()
) -> None:
    """Raise `CriterionFidelityError` unless the plan carries every declared criterion."""
    findings = fidelity_findings(declared, plan.acceptance_checks, frozen)
    if findings:
        raise CriterionFidelityError(
            f"{CRITERION_FIDELITY}: the plan does not carry the specification's "
            "acceptance criteria as written. Every criterion of the criteria block "
            "goes into acceptance_checks under its own check_id, its command copied "
            "character for character and expect_exit 0; checks of your own go under "
            "other ids. " + "; ".join(findings))


# --------------------------------------------------------------------------- #
# Test-content fidelity (FID1)
# --------------------------------------------------------------------------- #
#
# `assert_faithful` proves the plan carries the criteria's commands. The commands are node
# ids, and a node id names a test, not its bytes: in the P3-02s2rf run the developer wrote
# the implementation *and* the tests under the same node ids, QA accepted against tests
# nobody had reviewed, and only the orchestrator's byte comparison after the import stopped
# it. A specification can therefore **pin its test contract**, in one more fenced block:
#
#     ```test-pins
#     <sha256 of the file, 64 lower-case hex digits>  tests/test_x.py
#     absent  tests/conftest.py
#     ```
#
# One line per file: a digest (or the word `absent`), blanks, a plain relative path. A
# specification without such a block is not compared, and says so (`NO_TEST_PINS`): never
# "passed". The product refuses -- by name, with the file and both digests -- when, in the
# candidate tree: a pinned file differs, is missing, is not a regular file or is present
# although pinned `absent`; a path differs from a pinned one only in case; a test file a
# criterion runs is not pinned; or a file that changes what the criteria's command runs
# without being named is not byte-identical to the run's base and not pinned either. That
# last CLASS is closed by rule, not by a list of three names (`_findings`, rule P5/P7):
#
#   * pytest 9 reads its configuration from the first of `pytest.toml`, `.pytest.toml`,
#     `pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg` (`_pytest/config/
#     findpaths.py`), and `conftest.py` / `__init__.py` on every directory of the collection path;
#   * the interpreter puts the working directory (and pytest the first directory without an
#     `__init__.py` above each test file) first on `sys.path`, so a module or package there
#     shadows the standard library (`sys.stdlib_module_names`), pytest's own runtime
#     (`pytest`, `_pytest`, `pluggy`, `py`, ...), a plugin pytest autoloads, and any module
#     a pinned test imports that the base does not itself provide there;
#   * distribution metadata on `sys.path` (`*.dist-info`, `*.egg-info`) registers plugins
#     (the `pytest11` entry point group), and `.pytest_cache` / `.hypothesis` replay state;
#   * `sitecustomize.py`, `usercustomize.py`, `.pth`, a compiled module or a bare `.pyc`
#     is imported in place of source; `.python-version` selects the interpreter;
#     `.gitattributes` changes what `git archive` puts in the arena.

TEST_CONTENT_FIDELITY = "TEST_CONTENT_FIDELITY"
#: The verdict name of a pins block that cannot be read, or that does not cover the files the
#: criteria name.
TEST_PINS_UNREADABLE = "SPEC_TEST_PINS_UNREADABLE"
NO_TEST_PINS = (
    "test content fidelity NO_TEST_PINS: the specification pins no test files, so the "
    "candidate's test bytes are not compared with anything"
)

ABSENT = "absent"
MAX_PINS = 200
#: A pinned file larger than this is refused, not read: the pin names test sources.
MAX_PIN_BYTES = 4 * 1024 * 1024
MAX_LISTED = 12

_PIN_DIGEST = re.compile(r"(?:[0-9a-f]{64}|absent)")
_PIN_PATH = re.compile(r"[A-Za-z0-9._+@=,-]+(?:/[A-Za-z0-9._+@=,-]+)*")
_TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test|conftest)\.py\Z")
#: What the test run reads on its way down from the repository root to a test file, besides
#: the file itself (see the header comment for the sources).
CONTROL_NAMES = frozenset({
    # pytest's configuration, in pytest 9's search order (_pytest/config/findpaths.py)
    "pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini",
    "setup.cfg",
    # collection hooks, package markers, `python3 <directory>`
    "conftest.py", "__init__.py", "__main__.py",
    # interpreter start-up and interpreter selection
    "sitecustomize.py", "usercustomize.py", ".python-version", ".tool-versions",
    # what `git archive` applies to the arena
    ".gitattributes",
})
_REGULAR_MODES = ("100644", "100755")
#: What the interpreter loads in place of, or before, a source module.
_LOADABLE = (".so", ".pyd", ".pth", ".pyc", ".pyo")
#: Directories whose content pytest or a plugin replays.
STATE_DIRS = (".pytest_cache", ".hypothesis")
#: Distribution metadata on `sys.path`: entry points register plugins.
_METADATA_SUFFIXES = (".dist-info", ".egg-info", ".egg", ".egg-link")
#: The modules pytest's own runtime imports (pytest 9.0.3 `Requires-Dist` and itself).
PYTEST_RUNTIME = frozenset({
    "pytest", "_pytest", "py", "pluggy", "iniconfig", "packaging", "pygments", "exceptiongroup",
    "tomli", "colorama",
})
_SOURCE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyw", ".pyd", ".so")


class PinsBlockError(CriteriaBlockError):
    """The specification's `test-pins` block cannot be read, or does not cover its criteria."""


@dataclass(frozen=True)
class PinnedFile:
    path: str
    digest: str          # 64 hex digits, or `ABSENT`


def _show(path: str) -> str:
    """A path of the candidate as one printable word: a file name can carry a newline, a lone
    surrogate or the text of a finding, and it ends up in a reason, a gap and a prompt."""
    return repr(path)[1:-1]


def _key(path: str) -> str:
    """A path as a case-insensitive filesystem would see it (pinned paths are ASCII)."""
    return path.casefold()


def declared_pins(spec_text: str) -> tuple[PinnedFile, ...]:
    """The test files the specification pins; `()` when it pins none.

    Raises `PinsBlockError` for a block that cannot be read (two blocks, one that is not
    closed, an empty one, a line that is not `<digest> <path>`, a path that is not plain
    and relative, a path twice -- also in another case -- more than `MAX_PINS` lines), and
    for pins that leave a test file named by a criterion without one (checked on the names;
    a directory a criterion runs is checked against the tree, `pin_findings`)."""
    blocks = _fenced_blocks(spec_text, "test-pins", "test-pins", PinsBlockError,
                            TEST_PINS_UNREADABLE)
    if not blocks:
        return ()
    if len(blocks) > 1:
        raise PinsBlockError(
            f"{TEST_PINS_UNREADABLE}: the specification has {len(blocks)} test-pins blocks; "
            "it must have exactly one, restated in full when it is revised")
    found: list[PinnedFile] = []
    seen: dict[str, str] = {}
    for number, line in blocks[0]:
        if not line.strip(" "):
            continue
        if _CONTROL.search(line):
            raise PinsBlockError(
                f"{TEST_PINS_UNREADABLE}: line {number} of the test-pins block contains a "
                "control character")
        head, _, rest = line.strip(" ").partition(" ")
        path = rest.strip(" ")
        if not _PIN_DIGEST.fullmatch(head) or not _PIN_PATH.fullmatch(path) or len(path) > 200 \
                or any(part in (".", "..") or part.casefold() == ".git"
                       for part in path.split("/")):
            raise PinsBlockError(
                f"{TEST_PINS_UNREADABLE}: line {number} of the test-pins block is not "
                "'<sha256 | absent> <path>' with a lower-case 64-digit digest and a plain "
                "relative path of letters, digits and . _ + @ = , - (no '..', no '.git', "
                "no blank)")
        if _key(path) in seen:
            raise PinsBlockError(
                f"{TEST_PINS_UNREADABLE}: the test-pins block pins {path} and "
                f"{seen[_key(path)]}, which are one path to a case-insensitive filesystem")
        seen[_key(path)] = path
        found.append(PinnedFile(path, head))
    if not found:
        raise PinsBlockError(f"{TEST_PINS_UNREADABLE}: the test-pins block pins no file")
    if len(found) > MAX_PINS:
        raise PinsBlockError(
            f"{TEST_PINS_UNREADABLE}: the test-pins block has {len(found)} lines, more than "
            f"{MAX_PINS}")
    named = _named_test_files(declared_criteria(spec_text))
    missing = [p for p in sorted(named) if _key(p) not in seen]
    if missing:
        raise PinsBlockError(
            f"{TEST_PINS_UNREADABLE}: a criterion runs test file(s) the test-pins block "
            f"does not pin: {', '.join(missing[:MAX_LISTED])}")
    pinned_absent = [p.path for p in found
                     if p.digest == ABSENT and _key(p.path) in {_key(n) for n in named}]
    if pinned_absent:
        raise PinsBlockError(
            f"{TEST_PINS_UNREADABLE}: {', '.join(pinned_absent)} is run by a criterion and "
            "pinned absent")
    return tuple(found)


def pins_block(spec_text: str) -> tuple[str, ...]:
    """The lines of the `test-pins` block, blanks normalised, `()` for a specification without
    one: two texts of one specification are compared on this (an amendment that changes it
    changes what the tests must be). A block that cannot be read compares as itself."""
    try:
        blocks = _fenced_blocks(spec_text, "test-pins", "test-pins", PinsBlockError,
                                TEST_PINS_UNREADABLE)
    except PinsBlockError:
        return ("<unreadable>", hashlib.sha256(spec_text.encode("utf-8", "surrogatepass")).hexdigest())
    return tuple(" ".join(line.split()) for block in blocks for _, line in block if line.strip())


def _split(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _words(command: str) -> list[str]:
    """The words of a criterion, and -- to three levels -- the words of every quoted word that
    holds blanks (`sh -c 'python3 -B tests/x.py'`): a wrapper hides no test file."""
    try:
        words = _split(command)
    except ValueError as exc:
        raise PinsBlockError(
            f"{TEST_PINS_UNREADABLE}: the criterion `{command[:80]}` cannot be split into "
            f"words ({exc}), so the files it runs cannot be named") from exc
    out = list(words)
    level = words
    for _ in range(3):
        inner: list[str] = []
        for word in level:
            if " " in word or "\t" in word:
                try:
                    inner += _split(word)
                except ValueError:
                    continue            # prose or code in quotes, not a command: kept as one word
        out += inner
        level = inner
    return out


def _is_pytest(words: list[str]) -> bool:
    return any(w == "pytest" or w.endswith("/pytest") for w in words)


def _named_words(criteria: Iterable[Criterion]) -> Iterable[tuple[str, bool]]:
    """`(path argument, command is a pytest run)` for each non-option word of each criterion."""
    for criterion in criteria:
        words = _words(criterion.command)
        pytest = _is_pytest(words)
        for word in words:
            if word.startswith("-") or not word:
                continue
            path = word.split("::", 1)[0]
            while path.startswith("./"):
                path = path[2:]
            yield path.rstrip("/"), pytest


#: Broader than the pin path grammar on purpose: a test file whose name the block cannot spell
#: (a non-ASCII letter) is found here, cannot be pinned, and so refuses the specification.
_PY_PATH = re.compile(r"[^\s'\"`=:;|&<>(){}\[\]]+\.py")


def _named_test_files(criteria: Iterable[Criterion]) -> set[str]:
    """The test files a criterion names: any `*.py` path in its words -- inside a quoted
    `sh -c '...'` or a `--deselect=` value too, before any `::`. In a pytest command **every**
    such path is a test file (pytest runs `tests/check_b.py::test_y` whatever its name is);
    in any other command only a path whose file name is a test file's (`test_*.py`,
    `*_test.py`, `conftest.py`) is."""
    found: set[str] = set()
    for criterion in criteria:
        words = _words(criterion.command)
        pytest = _is_pytest(words)
        for word in words:
            for path in _PY_PATH.findall(word):
                while path.startswith("./"):
                    path = path[2:]
                if pytest or _TEST_FILE.search(path):
                    found.add(path)
    return found


def _option(words: list[str], index: int, short: str, long: str) -> str | None:
    """The value of the option at `words[index]` (`-p X`, `-pX`, `--long X`, `--long=X`), or None."""
    word = words[index]
    if word == short or word == long:
        return words[index + 1] if index + 1 < len(words) else None
    if word.startswith(long + "="):
        return word[len(long) + 1:]
    if short and word.startswith(short) and len(word) > len(short) and not word.startswith("--"):
        return word[len(short):]
    return None


def _launch_options(criteria: Iterable[Criterion]) -> tuple[set[str], set[str], set[str]]:
    """`(plugin modules, configuration files, directories)` the pytest options of the criteria
    name: `-p NAME` loads a module from `sys.path`, `-c FILE` / `--config-file` replaces the
    configuration, `--rootdir` / `--confcutdir` move where configuration and conftests are
    looked for."""
    plugins: set[str] = set(); configs: set[str] = set(); dirs: set[str] = set()
    for criterion in criteria:
        words = _words(criterion.command)
        if not _is_pytest(words):
            continue
        for i in range(len(words)):
            for short, long, into in (("-p", "", plugins), ("-c", "--config-file", configs),
                                      ("", "--rootdir", dirs), ("", "--confcutdir", dirs)):
                value = _option(words, i, short, long or "\0")
                if value and ":" not in value:
                    into.add(value.strip("/") if into is dirs else value)
    return plugins, configs, dirs


@dataclass(frozen=True)
class TreeEntries:
    """The files of a git tree: `{path: (mode, object id)}`, read once."""
    repo: str
    entries: dict[str, tuple[str, str]]


def read_tree(repo: str, tree: str) -> TreeEntries:
    """Every file of `tree` (a tree id of `repo`), by `git ls-tree -r`: the object database,
    never a working directory, so a symbolic link is a link and a case-variant is another path."""
    from .commitref import FULL_ID, git

    if FULL_ID.fullmatch(tree) is None:
        raise ValueError(f"{tree!r} is not a full tree id")
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", tree).stdout
    entries: dict[str, tuple[str, str]] = {}
    for row in out.split(b"\0"):
        if not row:
            continue
        meta, _, name = row.partition(b"\t")
        mode, _, oid = meta.decode("ascii").split(" ")
        entries[name.decode("utf-8", "surrogateescape")] = (mode, oid)
    return TreeEntries(repo, entries)


def _blob(tree: TreeEntries, path: str) -> bytes:
    from .commitref import git

    mode, oid = tree.entries[path]
    size = int(git(tree.repo, "cat-file", "-s", oid).stdout.decode("ascii").strip())
    if size > MAX_PIN_BYTES:
        raise ValueError(f"{path} is {size} bytes, more than the {MAX_PIN_BYTES} a pin reads")
    return git(tree.repo, "cat-file", "blob", oid).stdout


def pin_findings(
    spec_text: str, *, repo: str, tree: str, base_tree: str | None
) -> list[str]:
    """What is wrong with the candidate `tree` against the specification's test pins.

    `[]` when the specification pins nothing (the caller says `NO_TEST_PINS`) or when every
    rule holds. Every other outcome is a finding, and an unreadable block, an unreadable
    tree or a missing base is one too: nothing here fails open. `base_tree` is the tree the
    run started from; files the tests load without being named are compared with it."""
    try:
        pins = declared_pins(spec_text)
        if not pins:
            return []
        if base_tree is None:
            return [f"{TEST_CONTENT_FIDELITY}: the run records no base tree, so the files the "
                    "tests load without being named cannot be compared with it"]
        candidate = read_tree(repo, tree)
        base = read_tree(repo, base_tree)
        return _findings(pins, declared_criteria(spec_text), candidate, base)
    except CriteriaBlockError as exc:
        return [str(exc)]
    except Exception as exc:  # noqa: BLE001 - an unreadable tree is a refusal, never a pass
        return [f"{TEST_CONTENT_FIDELITY}: the candidate's test files could not be read: "
                f"{type(exc).__name__}: {str(exc)[:200]}"]


def _module_name(segment: str) -> str:
    """The module a directory entry names: `json.py`, `json.cpython-313-x86_64-linux-gnu.so`,
    `json.pyc` and a directory `json/` are all `json`."""
    return segment.split(".", 1)[0] if segment.endswith(_SOURCE_SUFFIXES) else segment


def _imported_names(sources: Iterable[bytes]) -> set[str]:
    """Top-level names the given Python sources import (absolute imports, anywhere in them)."""
    names: set[str] = set()
    for data in sources:
        try:
            tree = ast.parse(data.decode("utf-8"))
        except (ValueError, SyntaxError, RecursionError):
            continue                       # pinned bytes that are not Python: nothing to read
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _resolvable(name: str) -> bool:
    """Can this interpreter resolve the top-level module `name` without the working directory?"""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _autoloaded_plugins() -> set[str]:
    """Top-level modules of the plugins this interpreter's pytest autoloads (`pytest11`)."""
    try:
        from importlib.metadata import entry_points

        return {e.value.split(":")[0].split(".")[0] for e in entry_points(group="pytest11")}
    except Exception:  # noqa: BLE001 - an unreadable metadata set protects the fixed names only
        return set()


def _findings(
    pins: tuple[PinnedFile, ...], criteria: tuple[Criterion, ...],
    tree: TreeEntries, base: TreeEntries,
) -> list[str]:
    out: list[str] = []
    keyed = {_key(p.path): p for p in pins}
    sources: list[bytes] = []
    # P1/P2. each pin, byte for byte
    for pin in pins:
        entry = tree.entries.get(pin.path)
        if pin.digest == ABSENT:
            if entry is not None:
                out.append(f"{pin.path}: pinned absent, but the candidate has it")
        elif entry is None:
            out.append(f"{pin.path}: missing from the candidate (deleted, renamed or "
                       f"ignored); expected sha256 {pin.digest}")
        elif entry[0] not in _REGULAR_MODES:
            out.append(f"{pin.path}: is not a regular file (mode {entry[0]}); expected "
                       f"sha256 {pin.digest}")
        else:
            data = _blob(tree, pin.path)
            found = hashlib.sha256(data).hexdigest()
            if found != pin.digest:
                out.append(f"{pin.path}: expected sha256 {pin.digest}, found {found}")
            elif pin.path.endswith(".py"):
                sources.append(data)
    # P3. a path that is a pinned one only to a case-insensitive filesystem
    for path in sorted(tree.entries):
        pin = keyed.get(_key(path))
        if pin is not None and path != pin.path:
            out.append(f"{_show(path)}: differs from the pinned {pin.path} only in case")
    # P4. the files the criteria run: named, directories expanded, plugins and configuration
    named: set[str] = set(_named_test_files(criteria))
    plugins, configs, launch_dirs = _launch_options(criteria)
    for path, pytest in _named_words(criteria):
        if pytest and path and not path.endswith(".py"):
            named.update(p for p in tree.entries
                         if p.startswith(path + "/") and p.endswith(".py"))
    for module in plugins:                 # `-p NAME`: NAME.py / NAME/__init__.py on sys.path
        stem = module.replace(".", "/")
        named.update(p for p in (stem + ".py", stem + "/__init__.py") if p in tree.entries)
    named.update(c for c in configs if c in tree.entries)
    for path in sorted(named):
        if _key(path) not in keyed:
            out.append(f"{_show(path)}: run by a criterion but not pinned")

    def changed(path: str) -> str | None:
        if _key(path) in keyed or tree.entries.get(path) == base.entries.get(path):
            return None
        return ("deleted" if path not in tree.entries
                else "new" if path not in base.entries else "changed")

    # P5/P7. what the command loads without being named, unchanged from the base or pinned
    files = sorted(named | {p.path for p in pins if p.path.endswith(".py")})
    test_dirs = {posixpath.dirname(p) for p in list(named) + [p.path for p in pins]}
    ancestors: set[str] = set(launch_dirs)
    for directory in test_dirs | launch_dirs:
        while True:
            ancestors.add(directory)
            if not directory:
                break
            directory = posixpath.dirname(directory)
    # the directories the interpreter and pytest put first on sys.path: the working directory
    # (the arena root) and, for every test file, the first directory above it without an
    # `__init__.py` (pytest's `prepend` import mode; the script directory is the test's own)
    roots = {""}
    for f in files:
        directory = posixpath.dirname(f)
        while directory and any(posixpath.join(directory, "__init__.py") in t.entries
                                for t in (tree, base)):
            directory = posixpath.dirname(directory)
        roots.add(directory)
    # names a pinned test imports that this interpreter can resolve (stdlib, site-packages, the
    # product itself) and that the base does not itself provide in the root: a module there
    # shadows them. A module the slice adds for the first time is the implementation, not a
    # shadow, and nothing resolves it yet.
    imported = {n for n in _imported_names(sources) if _resolvable(n)}
    runtime = set(getattr(sys, "stdlib_module_names", ())) | PYTEST_RUNTIME | _autoloaded_plugins()
    provided_by_root = {
        root: {_module_name(q[len(root) + 1:].split("/")[0] if root else q.split("/")[0])
               for q in base.entries if not root or q.startswith(root + "/")}
        for root in roots
    }
    shadows = tuple(p[:-3] + "/" for p in files)          # a package shadows a module of its name
    listed: dict[str, str] = {}
    for path in sorted(set(tree.entries) | set(base.entries)):
        what = changed(path)
        if what is None:
            continue
        directory, name = posixpath.split(path)
        why = None
        if directory in test_dirs and directory != "":
            why = "beside a pinned or named test file"
        elif name in CONTROL_NAMES and directory in ancestors:
            why = "read by pytest or the interpreter on the way to a test file"
        elif path.startswith(shadows):
            why = "a package that shadows a pinned or named module"
        elif any(directory == posixpath.join(d, "__pycache__")
                 or directory.startswith(posixpath.join(d, "__pycache__") + "/")
                 for d in test_dirs):
            why = "bytecode beside a test file"
        else:
            for root in roots:
                if root and not path.startswith(root + "/"):
                    continue
                rel = path[len(root) + 1:] if root else path
                parts = rel.split("/")
                top = parts[0]
                if top == "__pycache__" and len(parts) > 1:
                    top = parts[1]
                provided = provided_by_root[root]
                module = _module_name(top.split(".cpython-")[0] if ".cpython-" in top else top)
                if len(parts) == 1 and (top in CONTROL_NAMES or top.endswith(_LOADABLE)):
                    why = "read by pytest or the interpreter from the working directory"
                elif top in STATE_DIRS:
                    why = "state pytest or a plugin replays"
                elif top.endswith(_METADATA_SUFFIXES):
                    why = "distribution metadata on sys.path (entry points register plugins)"
                elif module in runtime or (module in imported and module not in provided):
                    why = (f"can shadow the module {module!r} the criteria's command or a "
                           "pinned test imports")
                if why:
                    break
        if why:
            listed[path] = f"{_show(path)}: {what} against the run's base and not pinned ({why})"
    out.extend(listed.values())
    if len(out) > MAX_LISTED:
        out = out[:MAX_LISTED] + [f"... and {len(out) - MAX_LISTED} more"]
    return out


def refusal(findings: Iterable[str]) -> str:
    return (f"{TEST_CONTENT_FIDELITY}: the candidate's test files are not the ones the "
            "specification pins. " + "; ".join(findings))
