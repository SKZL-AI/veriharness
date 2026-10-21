"""P3-21 slice 3: the three gaps slice 2 left in `HohRunLauncher.depends_on` -- imports of imports,
dependencies that are not Python imports, and the textual overlap of in-flight changes (lesson L13).

* `hoh.derivedreads` as a pure function of supplied sources: the import closure (bounded in depth and in
  files, a cycle ends, a file that cannot be read is a problem), the data files a path literal names,
  the dependency manifests an outside package reads. Every ceiling is tested exactly at the bound.
* `HohRunLauncher.depends_on` on a real directory and a real git repository: the pairs the one-level
  rule missed, the candidate changes of a node's branch (`git diff <mainline>...<branch>`, counted as
  writes), what stays independent, and every fail-closed answer.
* The production path: `hoh project drive` through `cli.main` (the real launcher, `drive_governed`,
  `ProjectController`, the frontier planner, the wave executor); only the process boundary and `merge`
  are replaced, as in `tests/test_derivedreads.py`. No agent, no Herdr, no network; everything under
  `tmp_path`.

This slice adds no loop, retry or recovery and writes no record: it is a pure relation consulted once per
pair per planning round, and every loop it adds (the closure, the literal scan, the changed-file list) is
bounded by a constant tested at the bound. `test_the_consult_reads_git_and_writes_nothing` pins that a
consult leaves the repository byte for byte as it was.

Mutant ids (M.., G.., W1) are the spec's table; each test says which it kills.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from test_continuation import _contract, _graph, _stop
from test_derivedreads import Gates, RecordingLauncher, _launcher, _node, _repo, git

from hoh import cli, conflict, derivedreads, governance
from hoh.derivedreads import (
    MANIFESTS, MAX_CLOSURE_FILES, MAX_IMPORT_DEPTH, MAX_LITERAL_CHARS, MAX_LITERALS,
)
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore

#: the driver factory reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}


# --------------------------------------------------------------------------- #
# derivedreads: a pure function of supplied sources
# --------------------------------------------------------------------------- #

def _expander(files: dict):
    def expand(path):
        prefix = f"{path}/" if path else ""
        beneath = sorted(f for f in files if f.startswith(prefix) and f.endswith(".py"))
        anything = any(f.startswith(prefix) for f in files) and path not in files
        return tuple(beneath) if anything else None
    return expand


def _derive(files: dict, *declared: str, **kw):
    return derivedreads.derive(
        declared, lambda p: files.get(p), lambda p: p in files, _expander(files), **kw)


def _chain(links: int) -> dict:
    """`src/a.py` imports `m1`, which imports `m2`, ... `m<links>` (which imports nothing)."""
    files = {"src/a.py": "import m1\n"}
    for i in range(1, links + 1):
        files[f"src/m{i}.py"] = f"import m{i + 1}\n" if i < links else ""
    return files


def test_the_closure_follows_imports_of_imports_and_a_cycle_ends():
    """Kills M1 (the derivation stops after one level, as slice 2 did): a imports b, b imports c, c imports
    d. A cycle terminates and a file that imports a declared file reads it back."""
    files = {"src/a.py": "import b\n", "src/b.py": "import c\n", "src/c.py": "import d\n", "src/d.py": ""}
    got = _derive(files, "src/a.py")
    assert got.problems == () and got.reads == ("src/b.py", "src/c.py", "src/d.py")
    cycle = {"src/a.py": "import b\n", "src/b.py": "import a\n"}
    again = _derive(cycle, "src/a.py")
    assert again.problems == () and again.reads == ("src/a.py", "src/b.py")


def test_an_import_by_a_literal_module_name_is_an_import_and_one_built_at_run_time_is_not():
    """Kills M10 (`import_module("x")` is not an import): the literal forms resolve like an `import`
    statement; a relative name, a variable and an f-string name nothing."""
    files = {
        "src/a.py": ("import importlib\nm = importlib.import_module('b')\nn = __import__('c.d')\n"
                     "o = importlib.import_module('.rel')\np = importlib.import_module(name)\n"
                     "q = importlib.import_module(f'e{x}')\n"),
        "src/b.py": "", "src/c/d.py": "", "src/rel.py": "", "src/e1.py": "",
    }
    assert _derive(files, "src/a.py").reads == ("src/b.py", "src/c/d.py")


def test_the_closure_follows_what_a_declared_directory_holds_too():
    files = {"src/pkg/a.py": "import util\n", "src/util.py": "import deep\n", "src/deep.py": ""}
    assert _derive(files, "src/pkg").reads == ("src/deep.py", "src/util.py")


def test_the_import_depth_ceiling_is_exact():
    """Kills M2 and M2b (the depth ceiling one level too high or too low): a chain
    of `MAX_IMPORT_DEPTH` imports is derived whole; one more is a problem and a node held apart."""
    assert MAX_IMPORT_DEPTH == 8
    exact = _derive(_chain(MAX_IMPORT_DEPTH), "src/a.py")
    assert exact.problems == ()
    assert exact.reads == tuple(sorted(f"src/m{i}.py" for i in range(1, MAX_IMPORT_DEPTH + 1)))
    over = _derive(_chain(MAX_IMPORT_DEPTH + 1), "src/a.py")
    assert len(over.problems) == 1 and f"deeper than {MAX_IMPORT_DEPTH} levels" in over.problems[0]


def test_the_closure_file_ceiling_is_exact():
    """Kills M3 (the ceiling off by one): `MAX_CLOSURE_FILES` imported files
    are read; one more is a problem, never a truncated list."""
    assert MAX_CLOSURE_FILES == 128

    def fan(n):
        names = [f"l{i}" for i in range(n)]
        return {"src/a.py": "import " + ", ".join(names) + "\n", **{f"src/{x}.py": "" for x in names}}

    exact = _derive(fan(MAX_CLOSURE_FILES), "src/a.py")
    assert exact.problems == () and len(exact.reads) == MAX_CLOSURE_FILES
    over = _derive(fan(MAX_CLOSURE_FILES + 1), "src/a.py")
    assert len(over.problems) == 1 and f"more than {MAX_CLOSURE_FILES} files" in over.problems[0]


@pytest.mark.parametrize("text", ["def (:\n", "x = 1\0\n", "import " + "m, " * 600 + "m\n"],
                         ids=["syntax-error", "nul", "too-many-imports"])
def test_an_imported_file_that_cannot_be_read_is_a_problem_not_a_gap(text):
    """Kills M1 on a closure that trusts what it cannot read (a transitive file that does not parse is read as no imports: the fail-open of a closure
    that trusts what it cannot read)."""
    files = {"src/a.py": "import b\n", "src/b.py": f"{text}", "src/c.py": ""}
    got = _derive(files, "src/a.py")
    assert got.problems and "src/b.py" in got.problems[0], got.problems


def test_an_imported_file_whose_reader_fails_is_a_problem_not_a_crash():
    def read(path):
        if path == "src/b.py":
            raise OSError("boom")
        return {"src/a.py": "import b\n"}.get(path)

    got = derivedreads.derive(["src/a.py"], read, lambda p: p in {"src/a.py", "src/b.py"},
                              lambda p: None)
    assert got.reads == ("src/b.py",) and len(got.problems) == 1 and "could not be read" in got.problems[0]


def test_data_files_named_by_a_literal_path_are_reads():
    """Kills M5 (a literal is not looked for) and M6 (only the repository root, not beside the file). What
    is built at run time names nothing."""
    files = {
        "src/pkg/loader.py": (
            "import json\n"
            'A = open("data/a.json")\n'
            'B = Path("data", "b.json")\n'
            'C = Path("data") / "c.json"\n'
            'D = Path(__file__).parent / "d.json"\n'
            "E = open(name)\n"
            'F = Path(f"data/{x}.json")\n'
            'G = os.path.join("data", "g.json")\n'
            'H = open("/data/h.json")\n'
            'I = open("data")\n'
            'J = open("data/nothing-yet.json")\n'),
        "data/a.json": "", "data/b.json": "", "data/c.json": "", "src/pkg/d.json": "",
        "data/g.json": "", "data/h.json": "",
    }
    got = _derive(files, "src/pkg/loader.py")
    assert got.problems == ()
    assert got.reads == ("data/a.json", "data/b.json", "data/c.json", "src/pkg/d.json")


def test_every_path_type_and_every_path_like_string_naming_a_repository_file_is_a_read():
    """Kills M12 (only `Path` is a path type), M13 (a path-like string is a read only under `open`/`Path`/`/`)
    and M15 (a `/` chain with a variable base reads only its last operand): the other
    path types, a literal handed to any other callee, a table of names, a `/` chain with a variable base,
    and the things that must still read nothing (a file another node is going to create, text with a
    space, a directory, a name with no `/` and no extension)."""
    files = {
        "src/m.py": (
            'A = PurePath("data", "a.json")\n'
            'B = PosixPath("data/b.json")\n'
            'C = PurePosixPath("data") / "c.json"\n'
            'D = pandas.read_csv("data/d.csv")\n'
            'shutil.copy("tools/e.cfg", dest)\n'
            'subprocess.run(["python", "scripts/f.py"])\n'
            'runpy.run_path("scripts/g.py")\n'
            'H = BASE / "data" / "h.json"\n'
            'TABLE = {"k": "data/i.json"}\n'
            'J = open("Makefile")\n'
            'NEW = "data/not-yet.json"\n'
            'SAID = "see data/a.json for details"\n'
            'DIR = "data"\n'
            'WORD = "plain"\n'),
        "data/a.json": "", "data/b.json": "", "data/c.json": "", "data/d.csv": "", "tools/e.cfg": "",
        "scripts/f.py": "def (:\n", "scripts/g.py": "def (:\n", "data/h.json": "", "data/i.json": "",
        "Makefile": "", "plain": "",
    }
    got = _derive(files, "src/m.py")
    assert got.problems == ()
    assert got.reads == ("Makefile", "data/a.json", "data/b.json", "data/c.json", "data/d.csv", "data/h.json",
                         "data/i.json", "scripts/f.py", "scripts/g.py", "tools/e.cfg")


def test_the_packages_on_the_way_to_a_module_are_read_too():
    """Kills M14 (the `__init__.py` of a parent package is not a dependency): `import pkg.sub.mod` runs
    `pkg/__init__.py` and `pkg/sub/__init__.py`; so does `from pkg.sub import mod`; a relative import
    runs the importing file's own package. What `pkg/__init__.py` imports is in the closure."""
    files = {"src/a.py": "import pkg.sub.mod\n", "src/b.py": "from pkg.sub import mod\n",
             "src/pkg/__init__.py": "from . import core\n", "src/pkg/core.py": "",
             "src/pkg/sub/__init__.py": "", "src/pkg/sub/mod.py": "",
             "src/pkg/rel.py": "from .core import thing\n"}
    for declared in ("src/a.py", "src/b.py"):
        assert _derive(files, declared).reads == ("src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/sub/__init__.py",
                                                  "src/pkg/sub/mod.py"), declared
    assert _derive(files, "src/pkg/rel.py").reads == ("src/pkg/__init__.py", "src/pkg/core.py")


def test_a_data_file_is_a_read_but_never_read_as_source():
    """Kills M9 (a path literal that names a Python file is followed as an import): the file below does not
    parse, and nothing says it should."""
    files = {"src/a.py": 'open("tools/template.py")\n', "tools/template.py": "def (:\n"}
    got = _derive(files, "src/a.py")
    assert got.problems == () and got.reads == ("tools/template.py",)


def test_the_literal_ceilings_are_exact():
    """Kills M8 (the count off by one) and M8b (a literal longer than a path is looked up): `MAX_LITERALS`
    literals are read; one more is a problem; one character past `MAX_LITERAL_CHARS` names nothing."""
    def many(n):
        return {"src/a.py": "".join(f'open("d{i}")\n' for i in range(n))}

    assert _derive(many(MAX_LITERALS), "src/a.py").problems == ()
    over = _derive(many(MAX_LITERALS + 1), "src/a.py")
    assert len(over.problems) == 1 and f"more than {MAX_LITERALS} path literals" in over.problems[0]
    long_name = "x" * MAX_LITERAL_CHARS
    got = derivedreads.derive(
        ["src/a.py"], lambda p: f'open("{long_name}")\nopen("{long_name}y")\n', lambda p: True,
        lambda p: None)
    assert long_name in got.reads and long_name + "y" not in got.reads and got.problems == ()


def test_a_package_from_outside_reads_the_dependency_manifests_that_exist():
    """Kills M7 (no manifest is read), M7b (the standard library counts as outside) and M7c (a name the
    repository holds counts as outside). A relative import and a manifest that does not exist read nothing."""
    base = {"src/util.py": "", "src/sub/__init__.py": "", "src/sub/rel.py": "from . import sibling\n",
            "src/sub/sibling.py": ""}
    for source, manifests in (
            ("import yaml\n", ("pyproject.toml", "requirements.txt")),
            ("from yaml import safe_load\n", ("pyproject.toml", "requirements.txt")),
            ("import json, os.path, xml.etree.ElementTree\nfrom collections import abc\n", ()),
            ("import util\n", ()),
            ("import __future__\n", ())):
        files = {**base, "src/app.py": source, "pyproject.toml": "[project]\n", "requirements.txt": "x\n"}
        assert _derive(files, "src/app.py").reads == tuple(
            sorted((*manifests, *(["src/util.py"] if "util" in source else [])))), source
    assert _derive({**base, "src/app.py": "import yaml\n"}, "src/app.py").reads == ()
    assert _derive({**base, "src/sub/rel.py": "from . import sibling\n"}, "src/sub/rel.py").reads == (
        "src/sub/__init__.py", "src/sub/sibling.py")
    assert set(MANIFESTS) == {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py",
                              "setup.cfg"}


def test_the_closure_carries_data_files_and_manifests_of_the_files_it_reaches():
    """Kills M1, M5 and M7 on the closure: the literals and outside imports of a file reached by an import count."""
    files = {"src/a.py": "import b\n", "src/b.py": 'import yaml\nopen("data/x.json")\n',
             "data/x.json": "", "pyproject.toml": ""}
    assert _derive(files, "src/a.py").reads == ("data/x.json", "pyproject.toml", "src/b.py")


# --------------------------------------------------------------------------- #
# the launcher's relation, on a real directory
# --------------------------------------------------------------------------- #

def test_a_write_two_imports_away_conflicts_and_an_unrelated_write_does_not(tmp_path):
    """Kills M1 on the launcher: slice 2 answered False for the pair below."""
    launcher = _launcher(_repo(tmp_path, {
        "src/pkg/a.py": "from pkg import b\n", "src/pkg/b.py": "from pkg import c\n",
        "src/pkg/c.py": "VALUE = 1\n", "docs/x.md": "# x\n"}))
    a, c = _node("a", ["src/pkg/a.py"]), _node("c", ["src/pkg/c.py"])
    assert launcher.depends_on(a, c) is True and launcher.depends_on(c, a) is True
    assert [str(x) for x in conflict.detect(launcher._footprint(a), launcher._footprint(c))] == [
        "WRITE_READ: c writes 'src/pkg/c.py'; a reads 'src/pkg/c.py'"]
    assert launcher.depends_on(a, _node("x", ["docs/x.md"])) is False


def test_a_data_file_a_module_opens_by_a_literal_conflicts_with_a_write_of_it(tmp_path):
    """Kills M5 on the launcher; the control pair (another data file) stays independent."""
    launcher = _launcher(_repo(tmp_path, {
        "src/loader.py": 'import json\nCONF = open("data/conf.json")\n',
        "data/conf.json": "{}", "data/other.json": "{}"}))
    loader = _node("loader", ["src/loader.py"])
    assert launcher.depends_on(loader, _node("conf", ["data/conf.json"])) is True
    assert launcher.depends_on(loader, _node("other", ["data/other.json"])) is False


def test_a_manifest_write_conflicts_with_a_node_that_uses_an_outside_package(tmp_path):
    """Kills M7 and M7b on the launcher; a node that imports only the standard library is independent of it."""
    launcher = _launcher(_repo(tmp_path, {
        "src/app.py": "import yaml\n", "src/plain.py": "import os\n", "pyproject.toml": "[project]\n"}))
    manifest = _node("manifest", ["pyproject.toml"])
    assert launcher.depends_on(_node("app", ["src/app.py"]), manifest) is True
    assert launcher.depends_on(_node("plain", ["src/plain.py"]), manifest) is False


@pytest.mark.parametrize("what, files", [
    ("deeper", _chain(MAX_IMPORT_DEPTH + 1)),
    ("syntax", {"src/a.py": "import b\n", "src/b.py": "def (:\n"}),
    ("not-utf8", {"src/a.py": "import b\n"}),
], ids=["deeper-than-the-bound", "unparseable-transitive-file", "non-utf8-transitive-file"])
def test_a_closure_that_cannot_be_trusted_holds_the_node_apart_from_everything(tmp_path, what, files):
    """Kills M2 and M4 (a launcher that reads a transitive file leniently) on the launcher: the node
    conflicts with a node that touches nothing it imports; the same node one step inside the bound
    does not."""
    repo = _repo(tmp_path, files)
    if what == "not-utf8":
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src/b.py").write_bytes(b"x = '\xff\xfe'\n")
    launcher = _launcher(repo)
    other = _node("other", ["docs/other.md"])
    assert launcher.depends_on(_node("a", ["src/a.py"]), other) is True
    assert launcher.depends_on(other, _node("a", ["src/a.py"])) is True


def _imported_as(repo: Path, kind: str) -> None:
    """`src/b.py`, the module `src/a.py` imports, as something odd."""
    target = repo / "src/b.py"
    if kind == "utf16":
        target.write_bytes("x = 1\n".encode("utf-16"))
    elif kind == "latin1":
        target.write_bytes(b"x = '\xe9'\n")
    elif kind == "oversize":
        target.write_bytes(b"#" * (derivedreads.MAX_SOURCE_BYTES + 1))
    elif kind == "mode-000":
        target.write_text("x = 1\n")
        target.chmod(0)
    elif kind == "link":
        (repo / "real.py").write_text("x = 1\n")
        os.symlink("../real.py", target)
    elif kind == "dangling-link":
        os.symlink("nowhere.py", target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()


@pytest.mark.parametrize("kind", ["utf16", "latin1", "oversize", "mode-000", "link"])
def test_an_imported_file_that_cannot_be_read_as_source_holds_the_node_apart(tmp_path, kind):
    """Kills M4 (a lenient decode) on the launcher and the fail-open of every odd input on the closure: the
    imported module is UTF-16, Latin-1, one byte over the ceiling, unreadable or a link."""
    if kind == "mode-000" and os.geteuid() == 0:
        pytest.skip("root reads a file of mode 000")
    repo = _repo(tmp_path, {"src/a.py": "import b\n", "docs/other.md": "# o\n"})
    _imported_as(repo, kind)
    launcher = _launcher(repo)
    assert launcher.depends_on(_node("a", ["src/a.py"]), _node("other", ["docs/other.md"])) is True


@pytest.mark.parametrize("kind", ["dangling-link", "fifo", "directory"])
def test_a_path_that_is_not_a_regular_file_is_not_a_module(tmp_path, kind):
    """A dangling link, a FIFO and a directory named `b.py` cannot be imported, so they are not read (and
    nothing opens the FIFO): the declared case of slice 2 is stricter because a declared path is read as
    asked."""
    repo = _repo(tmp_path, {"src/a.py": "import b\n", "docs/other.md": "# o\n"})
    _imported_as(repo, kind)
    launcher = _launcher(repo)
    assert launcher.depends_on(_node("a", ["src/a.py"]), _node("other", ["docs/other.md"])) is False


@pytest.mark.parametrize("literal", ['"a\\x00b"', '"\\ud800"', '"../../etc/passwd"', '"/etc/passwd"',
                                     '"../outside.txt"', '"src/../../outside.txt"',
                                     '"' + "x" * (MAX_LITERAL_CHARS + 1) + '"'],
                         ids=["nul", "lone-surrogate", "escapes-the-repository", "absolute",
                              "escapes-to-a-file-that-exists", "escapes-after-a-descent", "longer-than-a-path"])
def test_a_path_literal_that_cannot_name_a_repository_file_reads_nothing_and_breaks_nothing(tmp_path, literal):
    """Kills M11 (a path that climbs out of the repository is asked of the file system: the file that
    exists beside the repository below would be a read)."""
    (tmp_path / "outside.txt").write_text("not the repository's\n", encoding="utf-8")
    repo = _repo(tmp_path, {"src/a.py": f"open({literal})\n", "docs/other.md": "# o\n"})
    launcher = _launcher(repo)
    footprint = launcher._footprint(_node("a", ["src/a.py"]))
    assert footprint.problems == () and footprint.reads == ()
    assert launcher.depends_on(_node("a", ["src/a.py"]), _node("other", ["docs/other.md"])) is False


def test_a_derived_read_the_footprint_refuses_is_a_problem_not_a_dropped_read(tmp_path):
    """Kills G23 (slice 2's `_footprint` replaced the problems of the rebuilt footprint with its own and so
    lost what `conflict.footprint` refused of a derived path): a data file whose name holds a control
    character exists and is named by a literal; the footprint cannot hold it, and says so."""
    name = "data/x\x01.json"
    repo = _repo(tmp_path, {"src/a.py": 'open("data/x\\x01.json")\n', name: "{}"})
    launcher = _launcher(repo)
    footprint = launcher._footprint(_node("a", ["src/a.py"]))
    assert len(footprint.problems) == 1 and "refused" in footprint.problems[0], footprint.problems
    assert launcher.depends_on(_node("a", ["src/a.py"]), _node("other", ["docs/other.md"])) is True


def test_a_closure_exactly_at_the_depth_bound_does_not_hold_a_node_apart(tmp_path):
    launcher = _launcher(_repo(tmp_path, _chain(MAX_IMPORT_DEPTH)))
    assert launcher.depends_on(_node("a", ["src/a.py"]), _node("other", ["docs/other.md"])) is False
    assert launcher.depends_on(_node("a", ["src/a.py"]),
                               _node("last", [f"src/m{MAX_IMPORT_DEPTH}.py"])) is True


# --------------------------------------------------------------------------- #
# candidate changes: the files a node's branch has already changed (lesson L13)
# --------------------------------------------------------------------------- #

def _git_repo(tmp_path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    return repo


def _commit_on(repo: Path, branch: str, changes: dict, *, start: str = "master") -> None:
    """A branch from `start` with one commit that writes `changes`."""
    git(repo, "checkout", "-q", "-b", branch, start)
    for rel, text in changes.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(repo, "add", rel)
    git(repo, "commit", "-qm", f"work of {branch}", "--allow-empty")
    git(repo, "checkout", "-q", "master")


def _commit_bytes(repo: Path, branch: str, changes: dict) -> None:
    """Like `_commit_on`, for files whose content is bytes."""
    git(repo, "checkout", "-q", "-b", branch, "master")
    for rel, data in changes.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
        git(repo, "add", rel)
    git(repo, "commit", "-qm", f"work of {branch}")
    git(repo, "checkout", "-q", "master")


def _git_launcher(repo: Path, mainline: str = "master") -> HohRunLauncher:
    launcher = _launcher(repo)
    launcher.mainline = mainline
    return launcher


BASE = {"shared.py": "X = 1\nY = 2\n", "a.txt": "a\n", "b.txt": "b\n", "lib.py": "L = 1\n",
        "src/x.py": "import y\n", "src/y.py": "Y = 1\n", "other.py": "O = 1\n"}


def test_two_branches_that_changed_the_same_file_are_held_apart_though_the_nodes_declare_nothing(tmp_path):
    """Kills G1 (the candidate changes are not consulted), G2 (a node that declares nothing is independent
    even though its branch changed files: the fail-open of D5 on this path) and G3 (the changes are
    not counted as writes: two writers would not meet). The control pair changed different files."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"shared.py": "X = 10\nY = 2\n"})
    _commit_on(repo, "hoh-b", {"shared.py": "X = 1\nY = 20\n"})
    _commit_on(repo, "hoh-c", {"b.txt": "bb\n"})
    launcher = _git_launcher(repo)
    a, b, c = _node("a"), _node("b"), _node("c")
    assert launcher.depends_on(a, b) is True and launcher.depends_on(b, a) is True
    assert [str(x) for x in conflict.detect(launcher._footprint(a), launcher._footprint(b))] == [
        "WRITE_WRITE: a writes 'shared.py'; b also writes 'shared.py'"]
    assert launcher.depends_on(a, c) is False and launcher.depends_on(c, a) is False


def test_a_branch_that_changed_what_another_node_declares_is_a_conflict(tmp_path):
    """Kills G1, G2 and G3 on both directions: a writer of the changed file and a reader of it. A node with no branch
    is judged by its declaration alone."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"lib.py": "L = 2\n"})
    launcher = _git_launcher(repo)
    a = _node("a")
    assert launcher.depends_on(a, _node("writer", ["lib.py"])) is True
    assert launcher.depends_on(a, _node("reader", [], ["lib.py"])) is True
    assert launcher.depends_on(a, _node("unrelated", ["a.txt"])) is False
    assert [str(x) for x in conflict.detect(launcher._footprint(a),
                                            launcher._footprint(_node("reader", [], ["lib.py"])))] == [
        "WRITE_READ: a writes 'lib.py'; reader reads 'lib.py'"]


def test_the_imports_of_a_changed_file_are_read_like_those_of_a_declared_one(tmp_path):
    """Kills G4 (the changed files are not handed to the derivation): the branch changed `src/x.py`, which
    imports `src/y.py`; a node that writes `src/y.py` meets it."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"src/x.py": "import y\nZ = 1\n"})
    launcher = _git_launcher(repo)
    assert launcher.depends_on(_node("a"), _node("y", ["src/y.py"])) is True


def test_a_node_that_has_changed_nothing_is_still_independent_of_everything(tmp_path):
    """The compatibility rule of slice 2 (D5), kept where it is true: no branch, a branch at the mainline,
    and a branch the mainline already contains declare nothing."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-empty", {})                          # a commit with no change
    git(repo, "branch", "hoh-at-master", "master")
    _commit_on(repo, "hoh-merged", {"a.txt": "merged\n"})
    git(repo, "merge", "-q", "--ff-only", "hoh-merged")
    launcher = _git_launcher(repo)
    writer = _node("w", ["a.txt", "shared.py"])
    for name in ("none", "empty", "at-master", "merged"):
        node = TaskNode(id=name, branch={"none": None, "empty": "hoh-empty", "at-master": "hoh-at-master",
                                         "merged": "hoh-merged"}[name], run_id=None)
        assert launcher.depends_on(node, writer) is False, name
        assert launcher.depends_on(writer, node) is False, name
        assert launcher._candidate_changes(node) == ((), ()), name


def test_the_branch_is_the_one_merge_would_integrate_and_the_changes_are_the_work_since_the_base(tmp_path):
    """Kills G5 (two dots instead of three: what the mainline did since the branch point counts as the
    branch's work), G6 (the branch name is not the node's `branch`/`hoh-<run id>`) and G7 (a renamed file
    is one change instead of the old path and the new one)."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"a.txt": "aa\n", "lib.py": "L = 2\n"})
    (repo / "other.py").write_text("O = 2\n", encoding="utf-8")                        # the mainline moves on
    git(repo, "add", "other.py")
    git(repo, "commit", "-qm", "mainline work")
    launcher = _git_launcher(repo)
    by_default = launcher._candidate_changes(TaskNode(id="a"))
    by_run_id = launcher._candidate_changes(TaskNode(id="zzz", run_id="a"))
    by_branch = launcher._candidate_changes(TaskNode(id="zzz", branch="hoh-a"))
    assert by_default == by_run_id == by_branch == (("a.txt", "lib.py"), ())
    git(repo, "checkout", "-q", "-b", "hoh-renamed", "master~1")
    git(repo, "mv", "b.txt", "c.txt")
    git(repo, "commit", "-qm", "rename")
    git(repo, "checkout", "-q", "master")
    assert launcher._candidate_changes(TaskNode(id="renamed")) == (("b.txt", "c.txt"), ())


def test_a_git_that_cannot_answer_holds_the_node_apart(tmp_path, monkeypatch):
    """Kills G8 (a failing git reads as no changes: the fail-open), G9 (an unresolvable mainline reads as
    no changes while the branch exists) and G10 (a name that starts with a dash reaches git)."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))      # no repository above the test's own
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"a.txt": "aa\n"})
    other = _node("other", ["docs/other.md"])
    # the mainline is not a branch of this repository, the branch exists
    gone = _git_launcher(repo, mainline="nowhere")
    assert gone.depends_on(_node("a"), other) is True
    # a node that DECLARES a path: only the candidate problem can hold it apart (kills G17, the problems of
    # the candidate changes are dropped from the footprint: for a node that declares nothing `detect` says
    # "declares no path" anyway)
    assert gone.depends_on(_node("a", ["lib/zz.py"]), other) is True
    assert gone.depends_on(_node("a", ["lib/zz.py"]), _node("nobody-else", ["docs/o.md"])) is True
    assert "could not resolve the mainline" in gone._candidate_changes(_node("a"))[1][0]
    assert gone._candidate_changes(_node("nobody")) == ((), ())          # no branch: nothing started
    # a directory that is not a repository: the first question fails
    broken_dir = tmp_path / "broken"
    (broken_dir / ".git").mkdir(parents=True)                    # a `.git` that is not a repository
    broken = _git_launcher(broken_dir)
    assert broken.depends_on(_node("a"), other) is True
    assert "could not resolve the branch" in broken._candidate_changes(_node("a"))[1][0]
    # a directory with no `.git` entry holds no branches: nothing to read, no problem (slice 2's launchers
    # on plain directories keep their answers)
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _git_launcher(plain_dir)
    assert plain._candidate_changes(_node("a")) == ((), ())
    assert plain.depends_on(_node("a"), other) is False
    # names git could read as an option
    for launcher, node in ((_git_launcher(repo), TaskNode(id="x", branch="-x")),
                           (_git_launcher(repo, mainline="--output=evil"), TaskNode(id="a"))):
        files, problems = launcher._candidate_changes(node)
        assert files == () and problems and "not a usable name" in problems[0], node.id
    assert not (repo / "evil").exists()


def test_a_branch_that_added_a_file_whose_name_is_not_text_holds_the_node_apart(tmp_path):
    """Kills G18 (the generic `except` of `_candidate` answers `((), ())`: the silent fail-open): git
    lists a file name that is not UTF-8, the answer cannot be decoded, and the node is held apart --
    declaring a path or not."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {os.fsdecode(b"\xff\xfe.txt"): "x\n"})
    launcher = _git_launcher(repo)
    assert launcher._candidate_changes(TaskNode(id="a")) == (
        (), ("the candidate changes of this node could not be read",))
    assert launcher.depends_on(_node("a", ["lib/zz.py"]), _node("other", ["docs/o.md"])) is True
    assert launcher.depends_on(_node("a"), _node("other", ["docs/o.md"])) is True


def test_a_linked_worktree_whose_git_entry_is_a_file_reads_its_branches(tmp_path):
    """Kills G19 (a `.git` that is a file, as in every linked worktree, reads no branches: the
    fail-open the first revision had)."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"shared.py": "X = 10\n"})
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "--detach", str(linked), "master")
    assert (linked / ".git").is_file()
    launcher = _git_launcher(linked)
    assert launcher._candidate_changes(TaskNode(id="a")) == (("shared.py",), ())
    assert launcher.depends_on(_node("a"), _node("w", ["shared.py"])) is True


def test_the_imports_of_a_changed_file_come_from_the_branch_not_the_checkout(tmp_path):
    """Kills G20 (a changed file is read from the checkout): the checkout's `src/x.py` imports nothing;
    the branch's version imports `src/c.py`, which another node's branch changed. A file that is new on the
    branch (not in the checkout at all) is read too."""
    repo = _git_repo(tmp_path, {**BASE, "src/x.py": "X = 1\n", "src/c.py": "C = 1\n"})
    _commit_on(repo, "hoh-a", {"src/x.py": "import c\n"})
    _commit_on(repo, "hoh-n", {"src/new.py": "import c\n"})
    _commit_on(repo, "hoh-c", {"src/c.py": "C = 2\n"})
    launcher = _git_launcher(repo)
    changed = _node("c")
    assert launcher.depends_on(_node("a"), changed) is True and launcher.depends_on(changed, _node("a")) is True
    assert launcher.depends_on(_node("n"), changed) is True
    assert [str(x) for x in conflict.detect(launcher._footprint(_node("a")), launcher._footprint(changed))] == [
        "WRITE_READ: c writes 'src/c.py'; a reads 'src/c.py'"]
    unrelated = _node("u", ["docs/o.md"])
    assert launcher.depends_on(_node("a"), unrelated) is False


def test_a_file_the_branch_deleted_is_not_read_and_one_it_cannot_describe_is_a_problem(tmp_path):
    """Kills G21 (a file the branch deleted is read from the checkout) and G22 (the branch's version of a
    changed Python file is not UTF-8 or is over the ceiling: read as no imports): the checkout's
    `src/old.py` does not parse, the branch moved it away (a delete and an add); a branch that adds a
    Latin-1 file or one byte over the ceiling holds the node apart."""
    repo = _git_repo(tmp_path, {**BASE, "src/old.py": "def (:\n", "src/c.py": "C = 1\n"})
    git(repo, "checkout", "-q", "-b", "hoh-r", "master")
    git(repo, "mv", "src/old.py", "src/ok.py")
    (repo / "src/ok.py").write_text("import c\n", encoding="utf-8")
    git(repo, "add", "src/ok.py")
    git(repo, "commit", "-qm", "moved")
    git(repo, "checkout", "-q", "master")
    _commit_bytes(repo, "hoh-l", {"src/l.py": b"x = '\xe9'\n"})
    _commit_bytes(repo, "hoh-big", {"src/big.py": b"#" * (derivedreads.MAX_SOURCE_BYTES + 1)})
    launcher = _git_launcher(repo)
    moved = launcher._footprint(_node("r"))
    assert moved.problems == () and ("src/c.py" in [s for _, s in moved.reads]) and \
        {s for _, s in moved.writes} == {"src/ok.py", "src/old.py"}
    other = _node("other", ["docs/o.md"])
    for name in ("l", "big"):
        assert launcher._footprint(_node(name)).problems, name
        assert launcher.depends_on(_node(name), other) is True, name


def test_the_changed_file_ceiling_is_exact(tmp_path):
    """Kills G11 (the ceiling off by one) and G11b (never enforced): `CANDIDATE_MAX_FILES` changed files are
    listed; one more is a problem, never a truncated list."""
    repo = _git_repo(tmp_path, BASE)
    limit = HohRunLauncher.CANDIDATE_MAX_FILES
    assert limit == 256
    _commit_on(repo, "hoh-exact", {f"bulk/f{i}.txt": "x\n" for i in range(limit)})
    _commit_on(repo, "hoh-over", {f"bulk/f{i}.txt": "x\n" for i in range(limit + 1)})
    launcher = _git_launcher(repo)
    files, problems = launcher._candidate_changes(TaskNode(id="exact"))
    assert problems == () and len(files) == limit
    files, problems = launcher._candidate_changes(TaskNode(id="over"))
    assert files == () and len(problems) == 1 and f"more than {limit} files changed" in problems[0]
    assert launcher.depends_on(TaskNode(id="over"), _node("other", ["docs/other.md"])) is True


def test_every_git_question_carries_the_timeout_and_a_timeout_is_a_problem(tmp_path):
    """Kills G12 (the timeout is not forwarded: a hung git would hold the drive) and G12b (a timed-out
    git, exit 124, reads as an empty answer)."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"a.txt": "aa\n"})
    launcher = _git_launcher(repo)
    assert launcher.CANDIDATE_GIT_TIMEOUT == 30
    seen: list = []
    real = launcher._git

    def spy(*args, **kw):
        seen.append(kw.get("timeout"))
        return real(*args, **kw)

    launcher._git = spy
    assert launcher._candidate_changes(TaskNode(id="a"))[0] == ("a.txt",)
    assert seen == [30, 30, 30], seen

    def hung(*args, **kw):
        return subprocess.CompletedProcess(args, 124, "", "git timed out after 30 s")

    launcher._git = hung
    files, problems = launcher._candidate_changes(TaskNode(id="a"))
    assert files == () and len(problems) == 1 and "exit 124" in problems[0]


def test_a_git_that_answers_wrongly_at_any_step_is_a_problem_never_an_empty_list(tmp_path):
    """Kills G8b (a failing `git diff` reads as no changes) and G15 (an answer that is not a commit id
    is used): each of the three questions is made to fail in turn, the other two answering truthfully."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"a.txt": "aa\n"})
    launcher = _git_launcher(repo)
    real = launcher._git

    def failing(step, replacement):
        calls = []

        def spy(*args, **kw):
            calls.append(args)
            return replacement if len(calls) == step else real(*args, **kw)
        return spy

    bad_exit = subprocess.CompletedProcess([], 128, "", "fatal: nope")
    bad_id = subprocess.CompletedProcess([], 0, "not-a-commit-id\n", "")
    for step, replacement, words in (
            (1, bad_exit, "could not resolve the branch"), (2, bad_exit, "could not resolve the mainline"),
            (3, bad_exit, "could not list the changes"), (1, bad_id, "did not answer with commit ids"),
            (2, bad_id, "did not answer with commit ids")):
        launcher._git = failing(step, replacement)
        files, problems = launcher._candidate_changes(TaskNode(id="a"))
        assert files == () and len(problems) == 1 and words in problems[0], (step, problems)
        launcher._git = failing(step, replacement)             # a fresh count for the relation's own ask
        assert launcher.depends_on(TaskNode(id="a"), _node("other", ["docs/other.md"])) is True
    launcher._git = real
    assert launcher._candidate_changes(TaskNode(id="a")) == (("a.txt",), ())


def _snapshot(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        out[str(path.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def test_the_consult_reads_git_and_writes_nothing(tmp_path):
    """Kills W1 (the consult leaves a file or a ref behind): the repository, `.git` included, is the same
    before and after, and the answer is the same on a second call and for a pair asked in the other
    order -- no state, nothing to re-process."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-a", {"shared.py": "X = 10\n"})
    _commit_on(repo, "hoh-b", {"shared.py": "Y = 20\n"})
    launcher = _git_launcher(repo)
    before = _snapshot(repo)
    first = [launcher.depends_on(_node("a"), _node("b")), launcher.depends_on(_node("b"), _node("a"))]
    second = [launcher.depends_on(_node("a"), _node("b")), launcher.depends_on(_node("b"), _node("a"))]
    assert first == second == [True, True]
    assert _snapshot(repo) == before


def test_a_call_without_a_launcher_still_compares_the_declarations_only():
    """The unbound call of slice 2 (D6): no repository and no mainline, so no source and no branch."""
    a, b = _node("a", ["src/a.py"]), _node("b", ["src/a.py"])
    assert HohRunLauncher.depends_on(None, a, b) is True
    assert HohRunLauncher.depends_on(None, _node("a", ["x.py"]), _node("b", ["y.py"])) is False
    assert HohRunLauncher.depends_on(None, _node("a"), _node("b")) is False


# --------------------------------------------------------------------------- #
# the production path: hoh project drive -> drive_governed -> planner / wave -> depends_on
# --------------------------------------------------------------------------- #

class BranchLauncher(RecordingLauncher):
    """`RecordingLauncher` for nodes whose branch already exists (the fake `hoh worktree` checks it out
    instead of creating it); everything else is the real launcher."""

    def _run_cli(self, argv):
        if "worktree" in argv:
            branch = argv[argv.index("--branch") + 1]
            path = self.root.parent / "worktrees" / branch
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                known = subprocess.run(
                    ["git", "-C", str(self.repo_path), "rev-parse", "--verify", "--quiet",
                     f"refs/heads/{branch}"], capture_output=True).returncode == 0
                git(self.repo_path, "worktree", "add", "-q",
                    *([str(path), branch] if known else ["-b", branch, str(path)]))
            return Dispatch(0, json.dumps({"path": str(path)}), "")
        return super()._run_cli(argv)


def build_driver(store):
    cfg = CONFIG
    launcher = BranchLauncher(cfg["root"], cfg["repo"], barrier=cfg.get("barrier"))
    cfg["launcher"] = launcher
    return launcher, Gates()


def _world(tmp_path, nodes: dict, files: dict, branches: dict | None = None):
    """A git repository on `master` holding `files`; a branch `hoh-<id>` per entry of `branches`
    (`{file: text}` committed on it); one READY node per entry of `nodes` (`(writes, reads)`)."""
    repo = _git_repo(tmp_path, files)
    for name, changes in (branches or {}).items():
        _commit_on(repo, f"hoh-{name}", changes)
    root = tmp_path / "root"
    built = []
    for name, (writes, reads) in nodes.items():
        spec = tmp_path / f"{name}.md"
        spec.write_text(f"# {name}\n", encoding="utf-8")
        built.append(TaskNode(id=name, spec_path=str(spec), writes=list(writes),
                              semantic_reads=list(reads)))
    store = ProjectStore(root, "p")
    store.create(ProjectState(project_id="p", repo_path=str(repo), nodes=built))
    CONFIG.clear()
    CONFIG.update(root=root, repo=repo)
    (store.dir / "driver.json").write_text(json.dumps({
        "factory": "test_conflict_closure:build_driver", "path": str(Path(__file__).resolve().parent)}))
    return repo, root, store


def _drive(root, capsys):
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    return rc, json.loads(capsys.readouterr().out)


def _serialised(out, *names):
    return any(s.startswith("SERIALISED:") and all(n in s for n in names) for s in out["steps"])


def test_hoh_project_drive_serialises_two_nodes_whose_branches_changed_the_same_file(
        tmp_path, monkeypatch, capsys):
    """The production path for the textual overlap of in-flight changes. With `HOH_WAVE_WIDTH=2` two READY
    nodes declare nothing, and their branches (already holding work) both changed `shared.py`: they are
    launched one after the other and the `SERIALISED` step names both. Kills G1, G2, G3 on the path and the
    wiring of `depends_on` into the planner."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(
        tmp_path, {"a": ([], []), "b": ([], [])}, BASE,
        {"a": {"shared.py": "X = 10\nY = 2\n"}, "b": {"shared.py": "X = 1\nY = 20\n"}})
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert _serialised(out, "a", "b"), out["steps"]
    assert not any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    assert [e[0] for e in CONFIG["launcher"].events] == ["start", "end", "start", "end"]
    assert sorted(CONFIG["launcher"].merged) == ["a", "b"]
    assert {n.lifecycle for n in store.read_state().nodes} == {Lifecycle.MERGED}


def test_hoh_project_drive_still_launches_nodes_together_when_their_branches_changed_different_files(
        tmp_path, monkeypatch, capsys):
    """The control: the same two nodes, branches that changed different files. The barrier of two is passed
    only when both runs are inside `hoh run` at once, so this fails if the candidate changes
    over-serialise. Kills G16 (a node whose branch holds work is held apart from everything)."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(
        tmp_path, {"a": ([], []), "b": ([], [])}, BASE,
        {"a": {"a.txt": "aa\n"}, "b": {"b.txt": "bb\n"}})
    CONFIG["barrier"] = threading.Barrier(2)
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    assert not any(s.startswith("SERIALISED:") for s in out["steps"]), out["steps"]


def test_hoh_project_drive_serialises_two_nodes_that_meet_through_an_import_of_an_import(
        tmp_path, monkeypatch, capsys):
    """The production path for the transitive closure: `a.py` imports `b.py`, which imports `c.py`; one
    node writes `a.py`, the other `c.py`. Slice 2 launched them together. Kills M1 on the path."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(
        tmp_path, {"a": (["src/pkg/a.py"], []), "c": (["src/pkg/c.py"], [])},
        {"src/pkg/a.py": "from pkg import b\n", "src/pkg/b.py": "from pkg import c\n",
         "src/pkg/c.py": "VALUE = 1\n"})
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert _serialised(out, "a", "c"), out["steps"]
    assert not any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    assert [e[0] for e in CONFIG["launcher"].events] == ["start", "end", "start", "end"]


def _project_facts(store) -> tuple:
    """What a drive decided about the nodes and the waves it recorded (the drive's own bookkeeping --
    the master lease, the epoch log, a write sequence number -- changes with every drive and is not
    a decision about a node)."""
    nodes = [(n.id, n.lifecycle, n.branch, n.run_id, n.rejections) for n in store.read_state().nodes]
    waves = {p.name: p.read_bytes() for p in sorted((store.dir / "waves").glob("*"))}
    return nodes, waves


def test_a_further_drive_over_a_closed_project_reprocesses_nothing(tmp_path, monkeypatch, capsys):
    """The quiet terminal of the termination checklist: after the drive above ends CLOSED, a second drive
    starts no run, serialises nothing, and leaves every node and every wave record exactly as it was.
    (The slice keeps no counter and no record, so there is nothing that could be re-processed.)"""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(
        tmp_path, {"a": ([], []), "b": ([], [])}, BASE,
        {"a": {"shared.py": "X = 10\nY = 2\n"}, "b": {"shared.py": "X = 1\nY = 20\n"}})
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED" and _serialised(out, "a", "b"), out
    assert len(CONFIG["launcher"].events) == 4
    before = _project_facts(store)
    assert {n[1] for n in before[0]} == {Lifecycle.MERGED}
    rc, again = _drive(root, capsys)
    assert rc == 0 and again["halt"] == "CLOSED", again
    assert not any(s.startswith(("SERIALISED:", "WAVE:")) for s in again["steps"]), again["steps"]
    assert CONFIG["launcher"].events == [] and CONFIG["launcher"].merged == [], "the second drive built a launcher of its own and started nothing"
    assert _project_facts(store) == before


@pytest.mark.parametrize("files, writer", [
    ({"src/loader.py": 'CONF = open("data/conf.json")\n', "data/conf.json": "{}"}, "data/conf.json"),
    ({"src/app.py": "import yaml\n", "pyproject.toml": "[project]\n"}, "pyproject.toml"),
], ids=["a-data-file-opened-by-a-literal", "a-dependency-manifest"])
def test_hoh_project_drive_serialises_a_module_and_the_writer_of_a_file_that_is_not_python(
        tmp_path, monkeypatch, capsys, files, writer):
    """The production path for the dependencies that are not Python imports. Kills M5 and M7 on the path."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    module = next(f for f in files if f.startswith("src/"))
    repo, root, store = _world(tmp_path, {"m": ([module], []), "w": ([writer], [])}, files)
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert _serialised(out, "m", "w"), out["steps"]
    assert not any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    assert sorted(CONFIG["launcher"].merged) == ["m", "w"]


def test_a_node_whose_closure_cannot_be_read_does_not_stop_the_project_it_is_only_serialised(
        tmp_path, monkeypatch, capsys):
    """Progress: the head of the frontier is always chosen, so a node held apart by an unreadable transitive
    file delays its sibling by one round and the drive still closes (the termination of the slice: no
    counter, no retry, nothing re-processed; the second node follows the first's merge)."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(
        tmp_path, {"broken": (["src/a.py"], []), "x": (["docs/x.md"], [])},
        {"src/a.py": "import b\n", "src/b.py": "def (:\n", "docs/x.md": "# x\n"})
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert _serialised(out, "broken", "x"), out["steps"]
    assert sorted(CONFIG["launcher"].merged) == ["broken", "x"]
    assert [e[0] for e in CONFIG["launcher"].events] == ["start", "end", "start", "end"]


def test_the_continuation_beside_a_parked_branch_consults_that_branchs_changes(tmp_path):
    """The other production consult: `ProjectController` hands `launcher.depends_on` to
    `governance.continue_by_policy`, which asks it (through `plan_continuation`) of every READY node that
    carries on against every BLOCKED, RUNNING or held node -- so the unmerged work of a parked branch is
    consulted. The parked node's branch changed `shared.py`; of the two independent READY nodes the one whose
    branch changed `shared.py` too is withheld behind it, the other carries on. Kills G1 and G3 on this
    path."""
    repo = _git_repo(tmp_path, BASE)
    _commit_on(repo, "hoh-rp", {"shared.py": "X = 10\n"})           # the parked node P (run id `rp`)
    _commit_on(repo, "hoh-I1", {"shared.py": "Y = 20\n"})
    _commit_on(repo, "hoh-I2", {"b.txt": "bb\n"})
    runs = tmp_path / "runs"
    runs.mkdir()
    _stop(runs, "rp")
    launcher = _git_launcher(repo)
    got = governance.plan_continuation(_graph(), _contract(), wave_id="w1", runs_dir=runs,
                                       depends_on=launcher.depends_on)
    assert got.outcome == "CONTINUE", got
    assert got.continued == ("I2",)
    assert [(w.node_id, w.reason, w.blocked_by) for w in got.withheld] == [
        ("I1", "SEMANTIC_DEPENDENCY", "P")]


# --------------------------------------------------------------------------- #
# the register text this slice earns (R-1)
# --------------------------------------------------------------------------- #

def test_the_register_names_the_probe_this_slice_earns_and_keeps_the_earlier_ones():
    """The probe of P3-21 after slice 3 (R-1, with the slice that earns it): a symbol of `conflict.py`, the
    test of this file that drives `hoh project drive`, **no equivalent** (the three gaps the earlier one
    named are closed; what remains is a stated limit in the note, not a missing capability), and the two
    earlier probes kept by addition."""
    root = Path(__file__).resolve().parent.parent
    register = json.loads((root / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    row = {r["id"]: r for r in register["requirements"]}["P3-21"]
    probe = row["probe"]
    assert set(probe) == {"kind", "symbol", "module", "test", "note"}, sorted(probe)
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "detect", "conflict.py")
    path, _, node = probe["test"].partition("::")
    assert path == "tests/test_conflict_closure.py" and f"\ndef {node}(" in Path(__file__).read_text(
        encoding="utf-8")
    assert "\ndef detect(" in (root / "src/hoh/conflict.py").read_text(encoding="utf-8")
    for word in ("transitive", "candidate", "manifest", "stays out"):
        assert word in probe["note"], word
    previous = row["previous_probe"]
    assert previous["kind"] == "symbol" and previous["symbol"] == "detect"
    assert previous["test"].startswith("tests/test_derivedreads.py::") and "imports of imports" in previous[
        "equivalent"]
    assert previous["by"] == "P3-21 slice 3 (R-1)" and previous["replaced_on"] == "2026-10-19"
    before = row["probe_before_p321s2"]
    assert before["kind"] == "none" and before["by"] == "P3-21 slice 2 (R-1)"
    assert os.path.isfile(root / path)
