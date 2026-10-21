"""P3-21 slice 2: derived reads -- `HohRunLauncher.depends_on` compares what two nodes declare *and*
the repository files their declared Python files import, so that two nodes with disjoint declared
writes are not admitted together when one rewrites a module the other imports.

Three layers, each named by its tests:

* `hoh.derivedreads` as a pure function of supplied sources (no filesystem): the import forms it
  resolves, the first level of the closure (slice 3 follows it further: `tests/test_conflict_closure.py`),
  and every refusal (a problem the caller turns into an UNDECIDABLE footprint) with the bound that
  triggers it, exact at the bound.
* `HohRunLauncher.depends_on` on a real directory: the pair the exact-string rule missed, the
  pairs that stay independent, the fail-closed pairs, the parse cache.
* The production path: `hoh project drive` through `cli.main` (the driver registered by name, the
  real launcher, `drive_governed`, `ProjectController`, the frontier planner, the wave executor);
  only the process boundary (`_run_cli`) and `merge` are replaced. No agent, no Herdr, no
  network; everything under `tmp_path`.

This slice adds no loop, retry or recovery: it is a pure relation consulted once per pair per
planning round. The bounds below are ceilings on work, tested at the bound; no record is written
(`test_the_consult_writes_nothing`).

Mutant ids (D1.., L1..) are the spec's table; each test says which it kills.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hoh import cli, conflict, derivedreads
from hoh.contracts import Candidate, Condition, RunState, Stage
from hoh.derivedreads import (
    MAX_CANDIDATES, MAX_DECLARED_PY, MAX_DERIVED, MAX_EXPANDED_PY, MAX_IMPORTS, MAX_SOURCE_BYTES,
    MAX_WALK_DEPTH, MAX_WALK_ENTRIES,
)
from hoh.launcher import Dispatch, HohRunLauncher
from hoh.orchestrator import GateRunner, MergeResult
from hoh.project import GateOutcome, GateResult, Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.store import RunStore

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
#: the driver factory reads its world from here (imported by name through `driver.json`)
CONFIG: dict = {}


def git(repo, *args):
    done = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


# --------------------------------------------------------------------------- #
# derivedreads: a pure function of supplied sources
# --------------------------------------------------------------------------- #

def _expander(files: dict):
    """`expand` over a dict standing in for the repository: the `.py` files beneath a directory
    (`''`: the root), or None when the path names no directory."""
    def expand(path):
        prefix = f"{path}/" if path else ""
        beneath = sorted(f for f in files if f.startswith(prefix) and f.endswith(".py"))
        anything = any(f.startswith(prefix) for f in files) and path not in files
        return tuple(beneath) if anything else None
    return expand


def _derive(files: dict, *declared: str, **kw):
    """`derive` over a dict standing in for the repository."""
    return derivedreads.derive(
        declared, lambda p: files.get(p), lambda p: p in files, _expander(files), **kw)


def test_every_import_form_resolves_to_the_repository_file_it_names():
    """Kills D3 (the names of `from x import a, b` are not tried as submodules) and D2 (a relative
    import climbs one directory too far or too few)."""
    files = {
        "src/hoh/a.py": ("import hoh.b\nfrom hoh import c\nfrom hoh.d import thing\n"
                         "from . import e\nfrom .f import g\nfrom ..top import h\n"
                         "import os, json, hoh.pkg\n"),
        "src/hoh/b.py": "", "src/hoh/c.py": "", "src/hoh/d.py": "", "src/hoh/e.py": "",
        "src/hoh/f.py": "", "src/top.py": "", "src/hoh/pkg/__init__.py": "",
        "src/hoh/__init__.py": "",
    }
    got = _derive(files, "src/hoh/a.py")
    assert got.problems == ()
    assert got.reads == ("src/hoh/__init__.py", "src/hoh/b.py", "src/hoh/c.py", "src/hoh/d.py",
                         "src/hoh/e.py", "src/hoh/f.py", "src/hoh/pkg/__init__.py",
                         "src/top.py")


def test_lazy_imports_and_aliases_are_dependencies_too():
    files = {"src/x.py": ("def f():\n    import y as why\n    try:\n        from z import w\n"
                          "    except ImportError:\n        pass\n"),
             "src/y.py": "", "src/z.py": ""}
    assert _derive(files, "src/x.py").reads == ("src/y.py", "src/z.py")


def test_a_test_file_reads_its_sibling_modules_by_bare_name():
    """Kills D4 (the importing file's own directory is not a search root: `tests/` modules import
    each other by bare name)."""
    files = {"tests/test_a.py": "from test_b import helper\nimport conftest\n",
             "tests/test_b.py": "", "tests/conftest.py": ""}
    assert _derive(files, "tests/test_a.py").reads == ("tests/conftest.py", "tests/test_b.py")


def test_imports_of_imports_are_followed_and_a_cycle_terminates():
    """Slice 3 lifts limit L1 (it was `test_only_one_level_is_derived`): a imports b, b imports c; a's
    derived reads are b and c. The bounds of the closure are pinned in `tests/test_conflict_closure.py`."""
    files = {"src/a.py": "import b\n", "src/b.py": "import c\n", "src/c.py": ""}
    assert _derive(files, "src/a.py").reads == ("src/b.py", "src/c.py")
    cyc = {"src/a.py": "import b\n", "src/b.py": "import a\n"}
    assert _derive(cyc, "src/a.py").reads == ("src/a.py", "src/b.py"), "a cycle terminates"


def test_a_file_that_does_not_exist_yet_has_nothing_to_derive_and_no_problem():
    got = _derive({"src/b.py": ""}, "src/new.py")
    assert got == derivedreads.Derived((), ())


def test_only_plain_python_paths_are_read():
    """Kills D9 (every declared path is parsed: a JSON or Markdown file becomes a problem). A file
    that is not Python is not a directory either: nothing to derive and no problem."""
    files = {"docs/x.md": "this is ( not python", "data/x.json": "{", "src/b.py": ""}
    got = _derive(files, "docs/x.md", "data/x.json", "src/b.py", "src/nothing-yet")
    assert got == derivedreads.Derived((), ())


def test_a_glob_that_reaches_derive_is_a_problem_not_a_skip():
    """Kills E4 (a path holding a glob character is skipped again: the fail-open of review A's
    B1). The caller widens globs to their directory first (`conflict.footprint`); a glob that gets
    here anyway is not something to read, so it holds the item apart."""
    files = {"src/g1.py": "import (", "src/b.py": ""}
    for glob in ("src/g*.py", "src/**", "src/{a,b}.py", "src/g?.py", "src/g[1].py"):
        got = _derive(files, glob)
        assert got.problems and "glob" in got.problems[0] and got.reads == (), glob
    assert _derive(files, 7).problems and "not text" in _derive(files, 7).problems[0]


def test_a_relative_import_beyond_the_repository_root_names_nothing():
    files = {"top.py": "from .. import x\nfrom ... import y\nfrom . import z\n", "z.py": ""}
    assert _derive(files, "top.py").reads == ("z.py",)


def test_the_derived_reads_are_sorted_and_without_duplicates():
    files = {"src/a.py": "import c\nimport b\nimport c\nfrom b import x\n",
             "src/b.py": "", "src/c.py": ""}
    assert _derive(files, "src/a.py", "src/a.py").reads == ("src/b.py", "src/c.py")


@pytest.mark.parametrize("label,text", [
    ("syntax error", "def (:\n"), ("NUL", "import a\x00"), ("indent", "  x = 1\n"),
    ("deep nesting", "(" * 5000 + ")" * 5000 + "\n"),
    ("not text", None),
], ids=lambda v: v if isinstance(v, str) and len(v) < 20 else "case")
def test_a_declared_python_file_that_is_not_source_is_a_problem(label, text):
    """Kills D5 (a file that does not parse is read as having no imports): the caller turns the
    problem into an UNDECIDABLE footprint. The source is never quoted in the problem."""
    files = {"src/a.py": "SECRET-IN-SOURCE" if text is None else text}
    got = derivedreads.derive(
        ["src/a.py"], lambda p: files[p] if text is not None else b"bytes",
        lambda p: p in files, lambda p: None)
    assert got.problems and "src/a.py" in got.problems[0]
    assert "SECRET" not in " ".join(got.problems) and got.reads == ()


def test_a_reader_or_a_lookup_that_fails_is_a_problem_not_a_crash():
    """Kills D6 (a failing read is skipped)."""
    def bad_read(path):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    got = derivedreads.derive(["src/a.py"], bad_read, lambda p: True, lambda p: None)
    assert got.problems and "could not be read" in got.problems[0]

    def boom(path):
        raise OSError("gone")

    ok = derivedreads.derive(["src/a.py"], lambda p: "import b\n", boom, lambda p: None)
    assert ok.problems and "asking whether" in ok.problems[0]


def test_the_source_ceiling_is_exact():
    """Kills D10 (the ceiling moved by one, widened or removed)."""
    assert MAX_SOURCE_BYTES == 512 * 1024
    exact = "x = 1\n" * 100
    exact = exact + "#" * (MAX_SOURCE_BYTES - len(exact))
    assert len(exact) == MAX_SOURCE_BYTES
    files = {"src/a.py": exact}
    assert _derive(files, "src/a.py").problems == ()
    files = {"src/a.py": exact + "#"}
    assert "larger than" in _derive(files, "src/a.py").problems[0]


def test_the_import_ceiling_is_exact():
    """Kills D7 (the bound moved by one)."""
    assert MAX_IMPORTS == 512
    at = "".join(f"import m{i}\n" for i in range(MAX_IMPORTS))
    assert _derive({"src/a.py": at}, "src/a.py").problems == ()
    over = at + "import one_more\n"
    assert "import statements" in _derive({"src/a.py": over}, "src/a.py").problems[0]


def test_the_declared_python_file_ceiling_is_exact():
    """Kills D8 (the bound moved by one)."""
    assert MAX_DECLARED_PY == 64
    names = [f"src/m{i}.py" for i in range(MAX_DECLARED_PY + 1)]
    files = {n: "" for n in names}
    assert _derive(files, *names[:MAX_DECLARED_PY]).problems == ()
    assert "declared Python files" in _derive(files, *names).problems[0]


def test_the_candidate_and_the_derived_ceilings_are_problems_not_truncations():
    """Kills D11 (a bigger need is cut silently instead of being a problem)."""
    assert MAX_CANDIDATES == 65536 and MAX_DERIVED == 1000
    many = "".join(f"import a{i}\n" for i in range(MAX_IMPORTS))     # 3 candidates x 3 roots each
    files = {f"src/m{i}.py": many for i in range(MAX_DECLARED_PY)}
    got = _derive(files, *files)
    assert any("candidate paths" in p for p in got.problems)
    # more existing targets than MAX_DERIVED: no partial list
    targets = {f"src/t{i}.py": "" for i in range(MAX_DERIVED + 1)}
    sources = {f"src/s{k}.py": "".join(f"import t{i}\n" for i in range(k * 300, min((k + 1) * 300, MAX_DERIVED + 1)))
               for k in range(4)}
    got = _derive({**targets, **sources}, *sources)
    assert any("derived reads" in p for p in got.problems) and got.reads == ()


def test_a_declared_directory_is_expanded_over_the_python_files_beneath_it():
    """Kills E1 (a directory derives nothing: the fail-open of review A's B1): what the files
    beneath a declared directory import from OUTSIDE it is a read of the node."""
    files = {"src/pkg/a.py": "import util\nfrom pkg import a2\n", "src/pkg/a2.py": "",
             "src/pkg/sub/c.py": "import other\n", "src/util.py": "", "src/other.py": "",
             "src/pkg/data.json": "{", "src/unrelated.py": "import util\n"}
    got = _derive(files, "src/pkg")
    assert got.problems == ()
    assert got.reads == ("src/other.py", "src/pkg/a2.py", "src/util.py")
    # the repository root (what a `**` or `*.py` widens to) is a directory too
    assert _derive(files, "").reads == ("src/other.py", "src/pkg/a2.py", "src/util.py")
    # a directory that does not exist yet has nothing to derive and no problem
    assert _derive(files, "src/new") == derivedreads.Derived((), ())


def test_a_directory_that_cannot_be_listed_is_a_problem_not_a_crash():
    """Kills E7 (a failing listing is dropped)."""
    def boom(path):
        raise OSError("gone")

    got = derivedreads.derive(["src/pkg"], lambda p: None, lambda p: True, boom)
    assert got.problems and "could not be listed" in got.problems[0] and got.reads == ()


def test_the_expansion_ceiling_is_exact_and_counts_every_directory_of_the_item():
    """Kills E2 (the ceiling moved by one or removed) and E8 (counted per directory, not per item)."""
    assert MAX_EXPANDED_PY == 128
    files = {f"d/m{i}.py": "" for i in range(MAX_EXPANDED_PY)}
    assert _derive(files, "d").problems == ()
    files["d/one_more.py"] = ""
    got = _derive(files, "d")
    assert got.problems and "Python files beneath" in got.problems[0] and got.reads == ()
    two = {**{f"x/m{i}.py": "" for i in range(80)}, **{f"y/m{i}.py": "" for i in range(80)}}
    assert _derive(two, "x").problems == () and _derive(two, "y").problems == ()
    assert "Python files beneath" in _derive(two, "x", "y").problems[0]


# --------------------------------------------------------------------------- #
# the launcher's relation, on a real directory
# --------------------------------------------------------------------------- #

def exact_string_rule(a, b) -> bool:
    """`HohRunLauncher.depends_on` before this slice (a frozen copy): the pairs it called independent
    are the ones this slice is about."""
    a_w, b_w = set(a.writes), set(b.writes)
    return bool(a_w & b_w or a_w & set(b.semantic_reads) or b_w & set(a.semantic_reads))


def _repo(tmp_path, files: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return repo


def _launcher(repo: Path) -> HohRunLauncher:
    launcher = HohRunLauncher.__new__(HohRunLauncher)       # no constructor: only `repo_path` is read
    launcher.repo_path = repo
    return launcher


def _node(name, writes=(), reads=()):
    return TaskNode(id=name, writes=list(writes), semantic_reads=list(reads))


LIB = {"src/pkg/a.py": "from pkg import b\n", "src/pkg/b.py": "VALUE = 1\n",
       "src/pkg/c.py": "import json\n", "docs/x.md": "# x\n"}


def test_two_nodes_with_disjoint_declared_writes_conflict_when_one_rewrites_what_the_other_imports(
        tmp_path):
    """Kills L1 (`depends_on` keeps the exact-string rule): a writes `a.py`, which imports `b.py`;
    b writes `b.py`. The exact rule calls them independent; the relation serialises them, in both
    orders, as a real bool."""
    launcher = _launcher(_repo(tmp_path, LIB))
    a, b = _node("a", ["src/pkg/a.py"]), _node("b", ["src/pkg/b.py"])
    assert exact_string_rule(a, b) is False and exact_string_rule(b, a) is False
    assert launcher.depends_on(a, b) is True
    assert launcher.depends_on(b, a) is True
    detail = [str(x) for x in conflict.detect(launcher._footprint(a), launcher._footprint(b))]
    assert detail == ["WRITE_READ: b writes 'src/pkg/b.py'; a reads 'src/pkg/b.py'"]


def test_unrelated_nodes_and_two_readers_of_one_module_stay_independent():
    """Kills L2 (derived reads are added to the *writes*: two readers would conflict) and the
    over-serialisation of the unrelated pair."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), {**LIB, "src/pkg/d.py": "from pkg import b\n"})
        launcher = _launcher(repo)
        assert launcher.depends_on(_node("a", ["src/pkg/a.py"]), _node("x", ["docs/x.md"])) is False
        # a and d both import b and neither writes it: read/read is not a conflict
        assert launcher.depends_on(_node("a", ["src/pkg/a.py"]), _node("d", ["src/pkg/d.py"])) is False
        assert launcher.depends_on(_node("c", ["src/pkg/c.py"]), _node("b", ["src/pkg/b.py"])) is False


def test_a_file_a_node_is_going_to_create_derives_nothing_which_is_a_stated_limit(tmp_path):
    """A node that declares a file it has not written yet has no imports to read (limit L4): the
    declared reads of the node are what protect it."""
    launcher = _launcher(_repo(tmp_path, LIB))
    new = _node("new", ["src/pkg/new.py"])
    assert launcher.depends_on(new, _node("b", ["src/pkg/b.py"])) is False
    assert launcher.depends_on(_node("new", ["src/pkg/new.py"], ["src/pkg/b.py"]),
                               _node("b", ["src/pkg/b.py"])) is True


def test_a_node_whose_source_cannot_be_read_conflicts_with_everything_and_one_that_declares_nothing_with_nothing(
        tmp_path):
    """Kills L3 (the derived problems are dropped: an unparseable source is read as no imports) and
    L10 (a node that declares nothing is serialised with everything: most project nodes declare
    nothing, and the rule they were planned under is kept)."""
    launcher = _launcher(_repo(tmp_path, {**LIB, "src/pkg/broken.py": "def (:\n"}))
    other = _node("x", ["docs/x.md"])
    assert launcher.depends_on(_node("e"), other) is False
    assert launcher.depends_on(other, _node("e")) is False
    assert launcher.depends_on(_node("e"), _node("f")) is False
    broken = _node("broken", ["src/pkg/broken.py"])
    assert launcher.depends_on(broken, other) is True and launcher.depends_on(other, broken) is True
    fp = launcher._footprint(broken)
    assert fp.problems and "src/pkg/broken.py" in fp.problems[0]


def test_a_link_a_fifo_a_directory_and_an_oversize_file_are_problems_without_hanging(tmp_path):
    """Kills L5 (a symbolic link is followed) and L8 (a FIFO is opened blocking); the oversize
    file is refused twice over (the reader and `parse_imports`). The read is bounded on a thread, so a regression fails instead of hanging."""
    repo = _repo(tmp_path, {**LIB, "big.py": "#" * (MAX_SOURCE_BYTES + 1)})
    (repo / "link.py").symlink_to(repo / "src/pkg/b.py")
    os.mkfifo(repo / "fifo.py")
    (repo / "dir.py").mkdir()
    launcher = _launcher(repo)
    other = _node("x", ["docs/x.md"])
    for name in ("link.py", "fifo.py", "dir.py", "big.py"):
        box: list = []
        worker = threading.Thread(
            target=lambda n=name: box.append(launcher.depends_on(_node("n", [n]), other)),
            daemon=True)
        worker.start()
        worker.join(10)
        assert box == [True], name


def test_a_call_without_a_launcher_compares_the_declarations_only(tmp_path):
    """Kills L9 (no repository is read as a source that cannot be read: the unbound call the
    existing tests use as "the product's rule" would serialise every node that writes Python).
    A real launcher always has a repository; this is the stand-in's reading, stated as a limit."""
    writer, reader = _node("w", ["src/pkg/a.py"]), _node("r", reads=["src/pkg/a.py"])
    assert HohRunLauncher.depends_on(None, writer, reader) is True
    assert HohRunLauncher.depends_on(None, writer, _node("o", ["src/pkg/z.py"])) is False
    bare = HohRunLauncher.__new__(HohRunLauncher)
    assert bare.depends_on(writer, _node("o", ["src/pkg/z.py"])) is False
    assert not bare._footprint(_node("a", ["src/pkg/a.py"])).problems


def test_an_edited_source_is_parsed_again_and_the_cache_is_bounded(tmp_path):
    """Kills L4 (the parse cache is keyed by path alone: an edit is not seen)."""
    repo = _repo(tmp_path, LIB)
    launcher = _launcher(repo)
    a, b = _node("a", ["src/pkg/a.py"]), _node("b", ["src/pkg/b.py"])
    assert launcher.depends_on(a, b) is True
    (repo / "src/pkg/a.py").write_text("from pkg import c\n", encoding="utf-8")
    assert launcher.depends_on(a, b) is False
    assert launcher.depends_on(a, _node("c", ["src/pkg/c.py"])) is True
    cache = launcher.__dict__["_import_cache"]
    for i in range(launcher._IMPORT_CACHE_MAX + 5):
        (repo / f"m{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
        launcher.depends_on(_node("n", [f"m{i}.py"]), b)
    assert len(cache) <= launcher._IMPORT_CACHE_MAX


DIR_REPO = {"src/pkg/a.py": "import util\nfrom pkg import a2\n", "src/pkg/a2.py": "", "src/util.py": "X = 1\n",
            "docs/x.md": "# x\n"}


@pytest.mark.parametrize("declared", [
    "src/pkg", "src/pkg/", "./src/pkg/", "src/pkg/*.py", "src/pkg/**", "src/pkg/a?.py", "src/pkg/[a].py",
], ids=lambda v: v)
def test_a_declared_directory_or_glob_conflicts_with_a_write_of_what_its_files_import(tmp_path, declared):
    """Kills E1 (a directory derives nothing): review A's probe, one node per shape. The node that
    rewrites `src/pkg` (however spelled) and the node that rewrites `src/util.py`, which
    `src/pkg/a.py` imports, are NOT independent -- both ways round. The control: the same
    directory against a write of something no file beneath it imports stays independent."""
    launcher = _launcher(_repo(tmp_path, DIR_REPO))
    pkg, util = _node("pkg", [declared]), _node("u", ["src/util.py"])
    assert launcher.depends_on(pkg, util) is True and launcher.depends_on(util, pkg) is True
    assert launcher.depends_on(pkg, _node("d", ["docs/x.md"])) is False
    assert launcher.depends_on(_node("d", ["docs/x.md"]), pkg) is False


def test_a_declared_directory_that_is_only_read_derives_its_reads_too(tmp_path):
    """Kills MD-style regressions (`_footprint` derives from the writes only) for directories."""
    launcher = _launcher(_repo(tmp_path, DIR_REPO))
    reader, writer = _node("r", reads=["src/pkg/"]), _node("w", ["src/util.py"])
    assert launcher.depends_on(reader, writer) is True and launcher.depends_on(writer, reader) is True


def test_a_file_that_is_read_derives_its_imports_like_one_that_is_written(tmp_path):
    """Kills MD (a node that only READS a Python file: its imports are not derived)."""
    launcher = _launcher(_repo(tmp_path, DIR_REPO))
    reader, writer = _node("r", reads=["src/pkg/a.py"]), _node("w", ["src/util.py"])
    assert launcher.depends_on(reader, writer) is True and launcher.depends_on(writer, reader) is True


def test_a_directory_that_is_too_big_holds_the_pair_apart_and_one_at_the_bound_does_not(tmp_path):
    """Kills E2 (the expansion ceiling is not enforced) at the launcher: 128 Python files are
    read; 129 make the node UNDECIDABLE (conflicts with everything, even an unrelated write)."""
    files = {f"big/m{i}.py": "" for i in range(MAX_EXPANDED_PY)} | {"docs/x.md": ""}
    launcher = _launcher(_repo(tmp_path, files))
    other = _node("d", ["docs/x.md"])
    assert launcher.depends_on(_node("big", ["big"]), other) is False
    (launcher.repo_path / "big/one_more.py").write_text("", encoding="utf-8")
    assert launcher.depends_on(_node("big", ["big"]), other) is True
    fp = launcher._footprint(_node("big", ["big"]))
    assert any(f"more than {MAX_EXPANDED_PY} Python files" in p for p in fp.problems), fp.problems


def test_the_directory_walk_is_bounded_in_entries_and_in_depth(tmp_path):
    """Kills E3 (the entry ceiling is not enforced) and E6 (the depth ceiling is not enforced)."""
    launcher = _launcher(_repo(tmp_path, {"docs/x.md": ""}))
    other = _node("d", ["docs/x.md"])
    wide = launcher.repo_path / "wide"
    wide.mkdir()
    for i in range(MAX_WALK_ENTRIES):
        (wide / f"f{i}.txt").write_text("", encoding="utf-8")
    assert launcher.depends_on(_node("w", ["wide"]), other) is False
    (wide / "one_more.txt").write_text("", encoding="utf-8")
    assert launcher.depends_on(_node("w", ["wide"]), other) is True
    assert any("could not be listed" in p for p in launcher._footprint(_node("w", ["wide"])).problems)
    deep = launcher.repo_path / "deep"
    deep.mkdir()
    here = deep
    for _ in range(MAX_WALK_DEPTH):
        here = here / "d"
        here.mkdir()
    assert launcher.depends_on(_node("p", ["deep"]), other) is False
    (here / "d").mkdir()
    assert launcher.depends_on(_node("p", ["deep"]), other) is True


def test_a_link_beneath_a_declared_directory_or_as_one_holds_the_pair_apart(tmp_path):
    """Kills E9 (links are followed or ignored when listing): a link to a directory, a link named
    `.py`, and a declared directory that is itself a link are problems of the node."""
    launcher = _launcher(_repo(tmp_path, {**DIR_REPO, "elsewhere/z.py": "import util\n"}))
    other = _node("d", ["docs/x.md"])
    assert launcher.depends_on(_node("p", ["src/pkg"]), other) is False
    (launcher.repo_path / "src/pkg/dirlink").symlink_to(launcher.repo_path / "elsewhere")
    assert launcher.depends_on(_node("p", ["src/pkg"]), other) is True
    (launcher.repo_path / "src/pkg/dirlink").unlink()
    (launcher.repo_path / "src/pkg/lnk.py").symlink_to(launcher.repo_path / "src/util.py")
    assert launcher.depends_on(_node("p", ["src/pkg"]), other) is True
    (launcher.repo_path / "src/pkg/lnk.py").unlink()
    (launcher.repo_path / "src/pkgalias").symlink_to(launcher.repo_path / "src/pkg")
    assert launcher.depends_on(_node("p", ["src/pkgalias"]), other) is True


def test_repository_metadata_and_bytecode_directories_are_not_sources(tmp_path):
    """Kills E5 (`.git` and `__pycache__` are walked): a Python file under them is no import of the node."""
    files = {**DIR_REPO, "src/pkg2/ok.py": "X = 1\n", "src/pkg2/.git/hooks/h.py": "import util\n",
             "src/pkg2/__pycache__/c.py": "import util\n"}
    launcher = _launcher(_repo(tmp_path, files))
    assert launcher.depends_on(_node("p", ["src/pkg2"]), _node("u", ["src/util.py"])) is False


def test_an_import_of_a_file_nobody_reads_through_a_nested_directory_is_found(tmp_path):
    """Kills E1 for nesting: the file that imports the written module is two levels down."""
    files = {"src/pkg/inner/deep/z.py": "import util\n", "src/util.py": ""}
    launcher = _launcher(_repo(tmp_path, files))
    assert launcher.depends_on(_node("p", ["src/pkg"]), _node("u", ["src/util.py"])) is True


def test_a_python_source_that_is_not_utf8_is_a_problem_at_the_launcher(tmp_path):
    """Kills MH (`decode(errors="replace")`): a Latin-1 file that imports nothing is still not
    source this reader trusts, so its node conflicts with an unrelated write."""
    launcher = _launcher(_repo(tmp_path, {"docs/x.md": ""}))
    (launcher.repo_path / "src").mkdir()
    (launcher.repo_path / "src/latin1.py").write_bytes("# caf\xe9\n".encode("latin-1"))
    assert launcher.depends_on(_node("l", ["src/latin1.py"]), _node("d", ["docs/x.md"])) is True


def test_an_import_by_bare_name_reaches_the_repository_root_from_a_nested_file(tmp_path):
    """Kills MP (the repository root is not a search root): `tools/x.py` imports `helper`, which
    lives at the root."""
    launcher = _launcher(_repo(tmp_path, {"tools/x.py": "import helper\n", "helper.py": ""}))
    assert launcher.depends_on(_node("x", ["tools/x.py"]), _node("h", ["helper.py"])) is True


def test_more_problems_than_a_footprint_reports_are_cut_with_a_count(tmp_path):
    """Kills MC (the problem list is not bounded)."""
    broken = {f"src/b{i}.py": "def (:\n" for i in range(conflict.MAX_PROBLEMS + 5)}
    launcher = _launcher(_repo(tmp_path, broken))
    fp = launcher._footprint(_node("n", list(broken)))
    assert len(fp.problems) == conflict.MAX_PROBLEMS + 1
    assert fp.problems[-1] == "5 more problems not listed"


def test_the_consult_writes_nothing(tmp_path):
    """No record, no file: the relation is consulted, not recorded (nothing to re-process in a
    later drive). The repository listing is the same after two hundred consults."""
    repo = _repo(tmp_path, LIB)
    launcher = _launcher(repo)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    for _ in range(200):
        launcher.depends_on(_node("a", ["src/pkg/a.py"]), _node("b", ["src/pkg/b.py"]))
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


# --------------------------------------------------------------------------- #
# the production path: hoh project drive -> drive_governed -> planner / wave -> depends_on
# --------------------------------------------------------------------------- #

class Grants:
    name = "test"

    def may_approve(self, worktree):
        return True

    def approve(self, worktree, repo):
        from hoh.approval import Approval
        return Approval(granted=True, worktree=worktree, provider=self.name)


class Gates(GateRunner):
    def subject(self) -> str:
        return "abc1234"

    def run(self, subject):
        return [GateResult(name="union", outcome=GateOutcome.GREEN, subject=subject,
                           exit_code=0, detail="")]


class RecordingLauncher(HohRunLauncher):
    """The real launcher; only the process boundary and the merge are replaced. `events` is the
    order of every `hoh run` start and end; a barrier of two proves two runs were inside `hoh run`
    together."""

    def __init__(self, root, repo, barrier=None, **kw):
        super().__init__(root, repo, approvals=Grants(), **kw)
        self.barrier = barrier
        self.events: list[tuple] = []
        self.merged: list[str] = []
        self._lock = threading.Lock()

    def _run_cli(self, argv):
        if "worktree" in argv:
            branch = argv[argv.index("--branch") + 1]
            path = self.root.parent / "worktrees" / branch
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                git(self.repo_path, "worktree", "add", "-q", "-b", branch, str(path))
            return Dispatch(0, json.dumps({"path": str(path)}), "")
        if "start" in argv:
            run_id = argv[argv.index("--run-id") + 1]
            state = RunState(run_id=run_id, repo_path=argv[argv.index("--repo") + 1],
                             project_name="p", spec_path=argv[argv.index("--spec") + 1],
                             spec_digest="s" * 16, policy_digest="p" * 16,
                             profile_digest="f" * 16, stage=Stage.PLANNING,
                             condition=Condition.ACTIVE)
            RunStore(self.root, run_id).write_state(state)
            return Dispatch(0, "", "")
        run_id = argv[argv.index("run") + 1]
        store = RunStore(self.root, run_id)
        with self._lock:
            self.events.append(("start", run_id, time.monotonic()))
        if self.barrier is not None:
            self.barrier.wait(timeout=20)       # BrokenBarrierError unless both runs overlap
        time.sleep(0.05)
        state = store.read_state()
        store.write_state(state.model_copy(update={
            "stage": Stage.CHECKPOINTED,
            "last_accepted_candidate": Candidate(
                candidate_id=f"{run_id}-i1", repo_path=state.repo_path, commit="a" * 40,
                tree_clean=True, tree_digest="d" * 16)}))
        with self._lock:
            self.events.append(("end", run_id, time.monotonic()))
        return Dispatch(0, "", "")

    def merge(self, node, outcome):
        self.merged.append(node.id)
        return MergeResult(landed=True, branch=node.branch or "", integrated="b" * 40,
                           candidate=outcome.candidate or "")


def build_driver(store):
    """The `driver.json` factory: the real launcher, a fake process boundary; width and barrier from
    CONFIG (the width reaches the loop through `HOH_WAVE_WIDTH`, no code of the driver names it)."""
    cfg = CONFIG
    launcher = RecordingLauncher(cfg["root"], cfg["repo"], barrier=cfg.get("barrier"))
    cfg["launcher"] = launcher
    return launcher, Gates()


def _world(tmp_path, nodes: dict, files: dict):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
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
        "factory": "test_derivedreads:build_driver", "path": str(Path(__file__).resolve().parent)}))
    return repo, root, store


def _drive(root, capsys):
    rc = cli.main(["--root", str(root), "project", "drive", "p", "--session", "sA"])
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_hoh_project_drive_serialises_two_nodes_whose_derived_reads_meet_a_write(
        tmp_path, monkeypatch, capsys):
    """The production path, end to end. Kills L1 (the launcher's relation is the exact-string one: the
    two nodes would be launched together and the barrier of two would be passed), L6 (a truthy
    non-bool) and the planner/wave wiring: with `HOH_WAVE_WIDTH=2` the two nodes declare disjoint
    writes, `a.py` imports `b.py`, and they are launched one after the other; the visible trace is
    the `SERIALISED` step naming both."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(tmp_path, {"a": (["src/pkg/a.py"], []), "b": (["src/pkg/b.py"], [])},
                               LIB)
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert any(s.startswith("SERIALISED:") and "a" in s and "b" in s for s in out["steps"]), out
    assert not any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    events = CONFIG["launcher"].events
    assert [e[0] for e in events] == ["start", "end", "start", "end"], events
    assert sorted(CONFIG["launcher"].merged) == ["a", "b"]
    assert {n.lifecycle for n in store.read_state().nodes} == {Lifecycle.MERGED}


def test_hoh_project_drive_still_launches_independent_nodes_together(tmp_path, monkeypatch, capsys):
    """The control of the test above: the same width, nodes whose reads do not meet a write. The barrier of two is
    passed only when both runs are inside `hoh run` at once, so this fails (BrokenBarrier) if the
    derived reads over-serialise. Kills L2 (read/read counted as a conflict)."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(tmp_path, {"a": (["src/pkg/a.py"], []), "d": (["src/pkg/d.py"], [])},
                               {**LIB, "src/pkg/d.py": "from pkg import b\n"})
    CONFIG["barrier"] = threading.Barrier(2)
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    assert not any(s.startswith("SERIALISED:") for s in out["steps"]), out["steps"]


def test_a_node_that_cannot_be_read_does_not_stop_the_project_it_is_only_serialised(
        tmp_path, monkeypatch, capsys):
    """Progress: a node whose declared Python file does not parse conflicts with everything, and the
    drive still closes -- the first node of the frontier is always chosen, the other follows when
    the first has merged. Nothing waits for anything that cannot happen."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(
        tmp_path, {"broken": (["src/pkg/broken.py"], []), "x": (["docs/x.md"], [])},
        {**LIB, "src/pkg/broken.py": "def (:\n"})
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert any(s.startswith("SERIALISED:") for s in out["steps"]), out["steps"]
    assert sorted(CONFIG["launcher"].merged) == ["broken", "x"]
    events = CONFIG["launcher"].events
    assert [e[0] for e in events] == ["start", "end", "start", "end"], events


def test_hoh_project_drive_serialises_a_directory_node_and_a_node_writing_a_module_its_files_import(
        tmp_path, monkeypatch, capsys):
    """The production path for review A's B1: with `HOH_WAVE_WIDTH=2`, a node that rewrites the
    directory `src/pkg/` and a node that rewrites `src/util.py` (imported by `src/pkg/a.py`) are
    launched one after the other, and the `SERIALISED` step names both. Kills E1 on the path."""
    monkeypatch.setenv("HOH_WAVE_WIDTH", "2")
    repo, root, store = _world(tmp_path, {"pkgdir": (["src/pkg/"], []), "u": (["src/util.py"], [])},
                               {"src/pkg/a.py": "import util\n", "src/util.py": "X = 1\n"})
    rc, out = _drive(root, capsys)
    assert rc == 0 and out["halt"] == "CLOSED", out
    assert any(s.startswith("SERIALISED:") and "pkgdir" in s and "u" in s for s in out["steps"]), out
    assert not any(s.startswith("WAVE:") for s in out["steps"]), out["steps"]
    assert [e[0] for e in CONFIG["launcher"].events] == ["start", "end", "start", "end"]
    assert sorted(CONFIG["launcher"].merged) == ["pkgdir", "u"]


# --------------------------------------------------------------------------- #
# the register text this slice earns (R-1)
# --------------------------------------------------------------------------- #

def test_the_register_keeps_the_probe_slice_2_earned_by_addition():
    """The probe of P3-21 that slice 2 earned (R-1) is kept by addition as `previous_probe` when slice 3
    replaces it (`tests/test_conflict_closure.py` pins the probe that stands): a symbol of `conflict.py`,
    this file's test that drives `hoh project drive`, and the equivalent that stated what was not
    detected, word for word."""
    root = Path(__file__).resolve().parent.parent
    register = json.loads((root / "program/v3_3/REQUIREMENTS.json").read_text(encoding="utf-8"))
    row = {r["id"]: r for r in register["requirements"]}["P3-21"]
    probe = row["previous_probe"]
    assert set(probe) == {"kind", "symbol", "module", "test", "equivalent", "note", "replaced_on", "by",
                          "why"}, sorted(probe)
    assert (probe["kind"], probe["symbol"], probe["module"]) == ("symbol", "detect", "conflict.py")
    path, _, node = probe["test"].partition("::")
    assert path == "tests/test_derivedreads.py" and f"\ndef {node}(" in Path(__file__).read_text(
        encoding="utf-8")
    assert "\ndef detect(" in (root / "src/hoh/conflict.py").read_text(encoding="utf-8")
    for word in ("imports of imports", "not Python", "L13"):
        assert word in probe["equivalent"], word
    earlier = row["probe_before_p321s2"]
    assert earlier["kind"] == "none" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", earlier["replaced_on"])
    assert earlier["by"] == "P3-21 slice 2 (R-1)" and earlier["why"]
