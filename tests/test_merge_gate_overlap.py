"""`hoh merge-gate check`: what a carry compares (MG-RB2, requirement row P5-36; analysis rows R-2 and R-3).

MG-RB1 carried a review across a HEAD move when no path the candidate changes was touched in between. Three
things that rule got wrong, each pinned here in the product gate and in `tools/merge_gate.py`, the
cross-check, on the same cases:

* a register is one file, so every pair of slices that edit `program/v3_3/REQUIREMENTS.json` refused each
  other: it is compared by **row id** now (and by top-level key), and an unreadable register refuses;
* records need not be re-reviewed when the base moved, but only the append-only ledger may overlap: a
  records-only candidate carries over `DOGFOOD_LEDGER.md` when both sides only add lines and the merged file
  keeps them all; any other record (a spec, a diff, a closure record, the candidate's own notes) is what a
  reviewer bound or a tool re-derives, and an overlap on it refuses; and **every candidate that edits
  register rows** carries only if those rows read the same -- status and evidence, line numbers aside --
  on the reviewers' merge tree and on the current one;
* file-disjoint slices can interact through an import: the candidate's touched code files and the modules
  they import (one level) are compared with the range's, in both directions, each named in the refusal.

Every test builds its own repository under `tmp_path` (a miniature of this one: a register, `src/hoh`,
`tools/capability_matrix.py`, `tests`, a ledger) and its own reports; nothing starts Herdr or an agent.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from hoh import mergegate
from test_merge_gate import World, _git, _load, _row, _snapshot
from test_merge_gate_rebind import (Case, _branch, _bound, _commit, _contents, _main, _run_product,
                                    _run_tool, _suite)

ROOT = Path(__file__).resolve().parent.parent
REG = "program/v3_3/REQUIREMENTS.json"
LEDGER = "DOGFOOD_LEDGER.md"
SPEC = "docs/spec.md"
TOOL = ROOT / "tools" / "merge_gate.py"


# --- a miniature repository -------------------------------------------------------------------


def _row_of(rid: str, **probe) -> dict:
    return {"id": rid, "name": f"name of {rid}", "note": f"note of {rid}", "authoritative_item": f"item.{rid}",
            "probe": probe or {"kind": "none"}}


def _register(*, edit: dict[str, dict] | None = None, top: dict | None = None, drop: tuple[str, ...] = (),
              add: tuple[dict, ...] = (), first: tuple[str, ...] = ()) -> str:
    rows = [
        _row_of("R-A", kind="symbol", symbol="alpha", module="alpha.py", test="tests/test_alpha.py::test_alpha"),
        _row_of("R-B"),
        _row_of("R-C", kind="symbol", symbol="gamma", module="gamma.py", test="tests/test_gamma.py::test_gamma"),
        _row_of("R-D"),
        _row_of("R-E", kind="evidence", path="dogfood/x/RECORD.json", evidence_dir="dogfood/x"),
    ]
    rows = [{**r, **((edit or {}).get(r["id"], {}))} for r in rows if r["id"] not in drop] + list(add)
    rows.sort(key=lambda r: first.index(r["id"]) if r["id"] in first else len(first))
    body = {"what": "register", "plan_snapshot_commit": "s0", "requirements": rows, "retired": []}
    body.update(top or {})
    return json.dumps(body, indent=1) + "\n"


_LEDGER = "# ledger\n" + "".join(f"entry {i}\n" for i in range(1, 25))
_SRC = {
    "src/hoh/__init__.py": "",
    "src/hoh/alpha.py": "def alpha():\n    return 1\n",
    "src/hoh/gamma.py": "def gamma():\n    return 2\n",
    "src/hoh/cli.py": "from . import alpha, gamma\n\n\ndef main():\n    return alpha.alpha()\n",
    "src/hoh/orch.py": "from . import wave\n\n\ndef run():\n    return wave.run()\n",
    "src/hoh/lazy.py": "def go():\n    from . import wave\n    return wave.run()\n",
    "src/hoh/wave.py": "def run():\n    return 1\n",
    "src/hoh/unrelated.py": "def noop():\n    return 0\n",
    "src/hoh/chain_a.py": "from . import chain_b\n",
    "src/hoh/chain_b.py": "from . import chain_c\n",
    "src/hoh/chain_c.py": "def c():\n    return 3\n",
    "tools/uses_wave.py": "from hoh import wave\n",
    "tests/test_alpha.py": "def test_alpha():\n    pass\n",
    "tests/test_gamma.py": "def test_gamma():\n    pass\n",
    "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json": '{"items": []}\n',
    "program/v3_3/OTHER.md": "".join(f"line {i}\n" for i in range(1, 25)),
    "dogfood/x/NOTES.md": "".join(f"note {i}\n" for i in range(1, 25)),
    "dogfood/x/spec.md": "".join(f"spec line {i}\n" for i in range(1, 25)),
    "dogfood/x/fix.diff": "".join(f"diff line {i}\n" for i in range(1, 25)),
    "dogfood/p2-gate/CLOSURE.json": json.dumps({f"k{i:02d}": i for i in range(24)}, indent=1) + "\n",
    SPEC: "# spec\n",
}


@pytest.fixture
def world(tmp_path: Path) -> World:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    files = {**_SRC, REG: _register(), LEDGER: _LEDGER,
             "tools/capability_matrix.py": (ROOT / "tools" / "capability_matrix.py").read_text(encoding="utf-8")}
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    return World(repo=repo, tmp=tmp_path, base=base, cand=base, spec=SPEC,
                 spec_sha=hashlib.sha256(b"# spec\n").hexdigest())


def _case(w: World, files: dict[str, str], *, name: str = "cand", eligible: bool = True, why: str = "",
          bound: str | None = None) -> Case:
    sha, spec_sha = _branch(w, name, files)
    return Case(sha, "HEAD", _bound(w, sha, spec_sha, bound or w.base), eligible, why)


def _case_bytes(w: World, relpath: str, data: bytes, *, why: str) -> Case:
    """A candidate that writes `data` (any bytes) to `relpath`."""
    _git(w.repo, "checkout", "-q", "-b", "cand-bytes", w.base)
    (w.repo / relpath).write_bytes(data)
    _git(w.repo, "add", "-A")
    _git(w.repo, "commit", "-qm", "candidate")
    sha = _git(w.repo, "rev-parse", "HEAD")
    _main(w)
    return Case(sha, "HEAD", _bound(w, sha, w.spec_sha, w.base), False, why)


def _edit(w: World, name: str, old: str, new: str) -> str:
    """The text of `name` as it is on the current branch with `old` replaced once by `new`."""
    text = (w.repo / name).read_text(encoding="utf-8")
    assert text.count(old) == 1, (name, old)
    return text.replace(old, new, 1)


# --- the cases: the candidate is branched from the base; `main` moves after the reviews ---------------

CARRIED = "carried"


def _rows_disjoint(w):
    case = _case(w, {REG: _register(edit={"R-B": {"note": "candidate note"}})})
    _commit(w, {REG: _register(edit={"R-D": {"note": "main note"}})}, "main edits R-D")
    return case, {"register_rows": {"candidate": ["R-B"], "range": ["R-D"], "overlap": []},
                  "matrix": {"rows": ["R-B"]}}


def _register_one_side(w):
    case = _case(w, {REG: _register(edit={"R-B": {"note": "candidate note"}})})
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    return case, {"register_rows": {"candidate": ["R-B"], "range": [], "overlap": []}}


def _top_level_one_side(w):
    case = _case(w, {REG: _register(edit={"R-B": {"note": "candidate note"}})})
    _commit(w, {REG: _register(top={"plan_snapshot_commit": "s1"})}, "main moves the snapshot")
    return case, {"register_rows": {"candidate": ["R-B"], "range": ["<plan_snapshot_commit>"], "overlap": []}}


def _same_row_other_field(w):
    case = _case(w, {REG: _register(edit={"R-B": {"name": "candidate name"}})},
                 eligible=False, why="register row(s) the candidate changes are also changed by the "
                                     "commits in between: R-B")
    _commit(w, {REG: _register(edit={"R-B": {"note": "main note"}})}, "main edits R-B's note")
    return case, None


def _top_level_both(w):
    case = _case(w, {REG: _register(top={"plan_snapshot_commit": "s-candidate"})}, eligible=False,
                 why="register row(s) the candidate changes are also changed by the commits in between: "
                     "<plan_snapshot_commit>")
    _commit(w, {REG: _register(top={"plan_snapshot_commit": "s-main"})}, "main moves the snapshot too")
    return case, None


def _row_added_both(w):
    case = _case(w, {REG: _register(add=(_row_of("R-N", note="candidate"),))}, eligible=False,
                 why="register row(s) the candidate changes are also changed by the commits in between: R-N")
    _commit(w, {REG: _register(add=(_row_of("R-N", note="main"),))}, "main adds R-N too")
    return case, None


def _row_removed_and_changed(w):
    case = _case(w, {REG: _register(edit={"R-D": {"note": "candidate"}})}, eligible=False,
                 why="register row(s) the candidate changes are also changed by the commits in between: R-D")
    _commit(w, {REG: _register(drop=("R-D",))}, "main removes R-D")
    return case, None


def _order_on_both_sides(w):
    case = _case(w, {REG: _register(first=("R-D",))}, eligible=False,
                 why="register row(s) the candidate changes are also changed by the commits in between: <order>")
    _commit(w, {REG: _register(first=("R-C",))}, "main moves another row to the front")
    return case, None


def _ledger_both_clean(w):
    """Both sides only add lines to the ledger (one appends, one inserts near the top): carried. The
    candidate's own record is not overlapped by anybody."""
    case = _case(w, {LEDGER: _LEDGER + "candidate entry\n", "dogfood/x/NOTES.md":
                     _SRC["dogfood/x/NOTES.md"].replace("note 20\n", "candidate note 20\n")})
    _commit(w, {LEDGER: _LEDGER.replace("entry 2\n", "entry 2\nmain entry added\n")}, "main adds a ledger line")
    return case, {"records_only_overlap_tolerated": [LEDGER]}


_NOT_APPEND_ONLY = "(the ledger is carried over only when both sides only add lines"


def _ledger_lines_added_by_a_candidate_that_is_not_records_only(w):
    """Both sides only add lines, but the candidate is code too: no tolerance (MG-RB1's refusal stays)."""
    case = _case(w, {LEDGER: _LEDGER + "candidate entry\n", "src/hoh/unrelated.py": "def noop():\n    return 5\n"},
                 eligible=False, why="touched by the commits in between: 'DOGFOOD_LEDGER.md'")
    _commit(w, {LEDGER: _LEDGER.replace("entry 2\n", "entry 2\nmain entry added\n")}, "main adds a ledger line")
    return case, None


def _ledger_changed_by_the_range(w):
    case = _case(w, {LEDGER: _LEDGER + "candidate entry\n"}, eligible=False,
                 why="'DOGFOOD_LEDGER.md' " + _NOT_APPEND_ONLY)
    _commit(w, {LEDGER: _LEDGER.replace("entry 2\n", "main rewrote entry 2\n")}, "main rewrites a ledger line")
    return case, None


def _ledger_changed_by_the_candidate(w):
    case = _case(w, {LEDGER: _LEDGER.replace("entry 20\n", "candidate rewrote entry 20\n")}, eligible=False,
                 why="'DOGFOOD_LEDGER.md' " + _NOT_APPEND_ONLY)
    _commit(w, {LEDGER: _LEDGER.replace("entry 2\n", "entry 2\nmain entry added\n")}, "main adds a ledger line")
    return case, None


def _dogfood_overlap(path: str, label: str):
    """Two edits of one dogfood file, far apart, so that git merges them cleanly: refused all the same, because
    what the reviewers bound (a spec, a diff, a closure record, the candidate's own notes) is what ships."""
    def build(w):
        text = _SRC[path]
        first = text.replace("3\n", "3 candidate\n", 1) if path.endswith(".md") or path.endswith(".diff") else \
            text.replace('"k03": 3', '"k03": 33', 1)
        second = text.replace("20\n", "20 main\n", 1) if path.endswith(".md") or path.endswith(".diff") else \
            text.replace('"k20": 20', '"k20": 200', 1)
        assert first != text and second != text
        case = _case(w, {path: first}, eligible=False, why=f"touched by the commits in between: '{path}'")
        _commit(w, {path: second}, f"main edits {label} too")
        return case, None
    return build


def _range_row_changed_before_the_reviewed_base(w):
    """The candidate was branched from M, the reviewers' base B is later, and the range B..H edits R-C. The row
    set of the range is B -> H (R-C), not M -> H (R-D and R-C): R-D, which the candidate edits too, was
    changed before the reviewers looked."""
    cand = _case(w, {REG: _register(edit={"R-D": {"authoritative_item": "candidate item"}})})
    reviewed = _commit(w, {REG: _register(edit={"R-D": {"name": "main name"}})}, "main edits R-D before the review")
    _commit(w, {REG: _register(edit={"R-D": {"name": "main name"}, "R-C": {"note": "main note"}})},
            "main edits R-C after it")
    case = Case(cand.candidate, "HEAD", _bound(w, cand.candidate, w.spec_sha, reviewed), True)
    return case, {"register_rows": {"candidate": ["R-D"], "range": ["R-C"], "overlap": []}}


def _added_row_unwired(w):
    """A new row is an edit: its reading is compared (the commonest records edit)."""
    row = _row_of("R-N", kind="symbol", symbol="alpha", module="alpha.py", test="tests/test_alpha.py::test_alpha")
    case = _case(w, {REG: _register(add=(row,))}, eligible=False, why="R-N: PROVEN -> PARTIAL")
    _commit(w, {"src/hoh/cli.py": "def main():\n    return 0\n"}, "main unwires alpha")
    return case, None


def _range_adds_an_importer_of_the_candidates_module(w):
    """The reading is of the merge tree of the base and the candidate: the candidate's module `delta` is
    imported by a second tool after the range, which changes the evidence of the row it adds."""
    row = _row_of("R-N", kind="symbol", symbol="delta", module="delta.py", test="tests/test_delta.py::test_delta")
    case = _case(w, {REG: _register(add=(row,)), "src/hoh/delta.py": "def delta():\n    return 4\n",
                     "tests/test_delta.py": "def test_delta():\n    pass\n",
                     "tools/uses_delta.py": "from hoh import delta\n"},
                 eligible=False, why="R-N: PROVEN -> PROVEN (evidence differs)")
    _commit(w, {"tools/also_uses_delta.py": "from hoh import delta\n"}, "main adds a second importer")
    return case, None


def _ledger_tolerated_and_status_changed(w):
    """The tolerance and the matrix reading combine: an append-only ledger overlap does not excuse a row whose
    reading changed."""
    case = _case(w, {LEDGER: _LEDGER + "candidate entry\n", REG: _register(edit={"R-A": {"note": "candidate note"}})},
                 eligible=False, why="R-A: PROVEN -> PARTIAL")
    _commit(w, {LEDGER: _LEDGER.replace("entry 2\n", "entry 2\nmain entry added\n"),
                "src/hoh/cli.py": "def main():\n    return 0\n"}, "main adds a ledger line and unwires alpha")
    return case, None


def _ledger_both_not_records_only(w):
    case = _case(w, {LEDGER: _LEDGER + "candidate entry\n", "src/hoh/unrelated.py": "def noop():\n    return 5\n"},
                 eligible=False, why="touched by the commits in between: 'DOGFOOD_LEDGER.md'")
    _commit(w, {LEDGER: _LEDGER.replace("entry 2\n", "main entry 2\n")}, "main edits the ledger")
    return case, None


def _program_file_both(w):
    case = _case(w, {"program/v3_3/OTHER.md": _SRC["program/v3_3/OTHER.md"].replace("line 20\n", "cand 20\n")},
                 eligible=False, why="touched by the commits in between: 'program/v3_3/OTHER.md'")
    _commit(w, {"program/v3_3/OTHER.md": _SRC["program/v3_3/OTHER.md"].replace("line 2\n", "main 2\n")},
            "main edits another programme document")
    return case, None


def _matrix_equal_line_shift(w):
    case = _case(w, {REG: _register(edit={"R-A": {"note": "candidate note"}})})
    _commit(w, {"src/hoh/alpha.py": "# a comment above\n\n" + _SRC["src/hoh/alpha.py"]}, "main shifts alpha")
    return case, {"matrix": {"rows": ["R-A"], "statuses": {"R-A": "PROVEN"}}}


def _matrix_status_changed(w):
    case = _case(w, {REG: _register(edit={"R-A": {"note": "candidate note"}})}, eligible=False,
                 why="R-A: PROVEN -> PARTIAL")
    _commit(w, {"src/hoh/cli.py": "def main():\n    return 0\n"}, "main unwires alpha")
    return case, None


def _matrix_status_changed_code_candidate(w):
    """A candidate that is not records-only and edits a row is held to the same reading."""
    case = _case(w, {REG: _register(edit={"R-A": {"note": "candidate note"}}),
                     "src/hoh/unrelated.py": "def noop():\n    return 8\n"},
                 eligible=False, why="R-A: PROVEN -> PARTIAL")
    _commit(w, {"src/hoh/cli.py": "def main():\n    return 0\n"}, "main unwires alpha")
    return case, None


def _matrix_evidence_only(w):
    case = _case(w, {REG: _register(edit={"R-A": {"note": "candidate note"}})}, eligible=False,
                 why="R-A: PROVEN -> PROVEN (evidence differs)")
    _commit(w, {"tools/zz.py": "def alpha():\n    return 9\n"}, "main defines another alpha")
    return case, None


def _matrix_row_not_comparable(w):
    case = _case(w, {REG: _register(edit={"R-E": {"note": "candidate note"}})}, eligible=False,
                 why="its probe cannot be read here: R-E")
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    return case, None


def _matrix_other_row_changed(w):
    """Only the rows the candidate edits are compared: R-C reads worse on main, R-B is what is edited."""
    case = _case(w, {REG: _register(edit={"R-B": {"note": "candidate note"}})})
    _commit(w, {"src/hoh/cli.py": "from . import alpha\n"}, "main unwires gamma")
    return case, {"matrix": {"rows": ["R-B"]}}


def _no_rows_no_matrix(w):
    case = _case(w, {LEDGER: _LEDGER + "candidate entry\n"})
    _commit(w, {"src/hoh/cli.py": "def main():\n    return 0\n"}, "main unwires alpha")
    return case, {"_absent": ["matrix", "register_rows"]}


def _candidate_imports_touched(w):
    case = _case(w, {"src/hoh/orch.py": _SRC["src/hoh/orch.py"] + "# candidate\n"}, eligible=False,
                 why="src/hoh/orch.py (candidate) imports src/hoh/wave.py, touched in between")
    _commit(w, {"src/hoh/wave.py": "def run():\n    return 2\n"}, "main changes wave")
    return case, None


def _range_imports_changed(w):
    case = _case(w, {"src/hoh/wave.py": "def run():\n    return 7\n"}, eligible=False,
                 why="src/hoh/orch.py (touched in between) imports src/hoh/wave.py, which the candidate "
                     "changes")
    _commit(w, {"src/hoh/orch.py": _SRC["src/hoh/orch.py"] + "# main\n"}, "main edits orch")
    return case, None


def _lazy_import(w):
    case = _case(w, {"src/hoh/lazy.py": _SRC["src/hoh/lazy.py"] + "# candidate\n"}, eligible=False,
                 why="src/hoh/lazy.py (candidate) imports src/hoh/wave.py, touched in between")
    _commit(w, {"src/hoh/wave.py": "def run():\n    return 2\n"}, "main changes wave")
    return case, None


def _tools_file_imports(w):
    case = _case(w, {"tools/uses_wave.py": "from hoh import wave\n# candidate\n"}, eligible=False,
                 why="tools/uses_wave.py (candidate) imports src/hoh/wave.py, touched in between")
    _commit(w, {"src/hoh/wave.py": "def run():\n    return 2\n"}, "main changes wave")
    return case, None


def _imports_one_level_only(w):
    case = _case(w, {"src/hoh/chain_a.py": "from . import chain_b\n# candidate\n"})
    _commit(w, {"src/hoh/chain_c.py": "def c():\n    return 4\n"}, "main changes the module two steps away")
    return case, {"imports": {"candidate_code_files": 1, "range_code_files": 1}}


def _imports_unrelated(w):
    case = _case(w, {"src/hoh/orch.py": _SRC["src/hoh/orch.py"] + "# candidate\n"})
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits an unrelated module")
    return case, {"imports": {"candidate_code_files": 1, "range_code_files": 1}}


def _range_file_not_python(w):
    case = _case(w, {"src/hoh/orch.py": _SRC["src/hoh/orch.py"] + "# candidate\n"}, eligible=False,
                 why="not measurable: src/hoh/unrelated.py at ")
    _commit(w, {"src/hoh/unrelated.py": "def (:\n"}, "main breaks a module")
    return case, None


def _candidate_file_not_python(w):
    case = _case(w, {"src/hoh/broken.py": "def (:\n"}, eligible=False,
                 why="not measurable: src/hoh/broken.py at ")
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    return case, None


def _candidate_file_not_utf8(w):
    case = _case_bytes(w, "src/hoh/binary.py", b"\xff\xfe\x00", why="not measurable: src/hoh/binary.py at ")
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    return case, None


def _ledger_case_variant(w):
    """Only `DOGFOOD_LEDGER.md` and `dogfood/` are tolerated, byte for byte: another spelling is any file."""
    case = _case(w, {"dogfood_ledger.md": "candidate\n"}, eligible=False,
                 why="touched by the commits in between: 'dogfood_ledger.md'")
    _commit(w, {"dogfood_ledger.md": "main\n"}, "main adds the same name")
    return case, None


def _plain_text_carry(w):
    case = _case(w, {"docs/other.md": "text\n"}, name="cand-plain")
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    return case, {"_absent": ["matrix", "register_rows", "records_only_overlap_tolerated"]}


CASES: dict[str, Callable] = {
    "rows-disjoint": _rows_disjoint,
    "register-edited-by-the-candidate-only": _register_one_side,
    "top-level-key-on-one-side": _top_level_one_side,
    "same-row-other-field": _same_row_other_field,
    "top-level-key-on-both-sides": _top_level_both,
    "row-order-changed-on-both-sides": _order_on_both_sides,
    "row-added-on-both-sides": _row_added_both,
    "row-removed-and-changed": _row_removed_and_changed,
    "ledger-lines-added-on-both-sides-of-a-records-only-candidate": _ledger_both_clean,
    "ledger-lines-added-by-a-candidate-that-is-not-records-only": _ledger_lines_added_by_a_candidate_that_is_not_records_only,
    "ledger-line-changed-by-the-range": _ledger_changed_by_the_range,
    "ledger-line-changed-by-the-candidate": _ledger_changed_by_the_candidate,
    "dogfood-notes-of-the-candidate-overlap": _dogfood_overlap("dogfood/x/NOTES.md", "the same notes"),
    "dogfood-spec-overlap": _dogfood_overlap("dogfood/x/spec.md", "the same spec"),
    "dogfood-fix-diff-overlap": _dogfood_overlap("dogfood/x/fix.diff", "the same diff"),
    "dogfood-closure-record-overlap": _dogfood_overlap("dogfood/p2-gate/CLOSURE.json", "the closure record"),
    "range-row-changed-before-the-reviewed-base": _range_row_changed_before_the_reviewed_base,
    "added-row-whose-reading-changed": _added_row_unwired,
    "range-adds-an-importer-of-the-candidates-module": _range_adds_an_importer_of_the_candidates_module,
    "ledger-overlap-tolerated-and-a-reading-changed": _ledger_tolerated_and_status_changed,
    "ledger-overlap-of-a-candidate-that-is-not-records-only": _ledger_both_not_records_only,
    "programme-document-overlap": _program_file_both,
    "matrix-equal-after-a-line-shift": _matrix_equal_line_shift,
    "matrix-status-changed": _matrix_status_changed,
    "matrix-status-changed-for-a-code-candidate": _matrix_status_changed_code_candidate,
    "matrix-evidence-only-changed": _matrix_evidence_only,
    "matrix-row-with-an-evidence-probe": _matrix_row_not_comparable,
    "matrix-of-a-row-the-candidate-does-not-edit": _matrix_other_row_changed,
    "no-edited-rows-no-matrix": _no_rows_no_matrix,
    "candidate-imports-a-module-touched-in-between": _candidate_imports_touched,
    "range-file-imports-a-module-the-candidate-changes": _range_imports_changed,
    "lazy-import": _lazy_import,
    "tools-file-imports": _tools_file_imports,
    "imports-are-followed-one-level-only": _imports_one_level_only,
    "imports-of-unrelated-modules": _imports_unrelated,
    "touched-file-that-is-not-python": _range_file_not_python,
    "candidate-file-that-is-not-python": _candidate_file_not_python,
    "candidate-file-that-is-not-utf-8": _candidate_file_not_utf8,
    "ledger-spelled-another-way": _ledger_case_variant,
    "no-code-no-register": _plain_text_carry,
}


def _subset(want, got) -> bool:
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and _subset(v, got[k]) for k, v in want.items())
    return want == got


# --- the product's decision ---------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_what_a_carry_compares_is_decided_from_the_trees_and_named_when_refused(world, capsys, name):
    """Kills: register rows compared as one file (`rows-disjoint`), row overlap ignored (`same-row-other-field`,
    `top-level-key-on-both-sides`, `row-added-on-both-sides`, `row-removed-and-changed`), the records-only
    tolerance applied to everyone (`ledger-overlap-of-a-candidate-that-is-not-records-only`) or to more than
    the ledger and dogfood (`programme-document-overlap`), the matrix digest not compared
    (`matrix-status-changed`, `matrix-evidence-only-changed`), line numbers compared
    (`matrix-equal-after-a-line-shift`), every row compared (`matrix-of-a-row-the-candidate-does-not-edit`),
    imports not followed in one direction (`candidate-imports-...`, `range-file-imports-...`), imports followed
    more than one level (`imports-are-followed-one-level-only`), imports inside functions missed (`lazy-import`)."""
    w = world
    case, expect = CASES[name](w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert err == ""
    body = _load(receipt)
    if case.eligible:
        assert rc == 0, out
        assert body["aggregate"] == "MERGE_ELIGIBLE" and body["problems"] == []
        for review in ("a", "b"):
            carried = _row(body, review)["carried_forward"]
            assert carried["review_base"] == _row(body, review)["footer"]["BASE"] and carried["overlap"] == []
            assert _subset({k: v for k, v in expect.items() if k != "_absent"}, carried), (expect, carried)
            for key in expect.get("_absent", ()):
                assert key not in carried, (key, carried)
        if "matrix" in expect:
            matrix = _row(body, "a")["carried_forward"]["matrix"]
            assert len(matrix["digest"]) == 64 and matrix["rows"] == expect["matrix"]["rows"]
    else:
        assert rc == 1, out
        for review in ("a", "b"):
            row = _row(body, review)
            assert (row["status"], row["problems"]) == ("NO_VERDICT", ["STALE_BASE"])
            assert "carried_forward" not in row
        details = [p["detail"] for p in body["problems"]]
        assert len(details) == 2 and all("; not carried forward: " in d and case.why in d for d in details)


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_cross_check_tool_agrees_on_every_case(world, capsys, name):
    """The tool decides the same, names the same reason, and records the same carry (the whole record,
    register rows, tolerated paths, imports and the matrix digest included)."""
    w = world
    case, _expect = CASES[name](w)
    product_rc, _o, _e, receipt = _run_product(w, capsys, case)
    tool_rc, text, tool = _run_tool(w, case)
    assert product_rc == tool_rc == (0 if case.eligible else 1), text
    for review in ("a", "b"):
        assert _row(tool, review).get("carried_forward") == _row(_load(receipt), review).get("carried_forward")
    if not case.eligible:
        assert case.why in text, text


def test_the_merge_message_says_what_was_compared_and_the_plain_line_is_unchanged(world, capsys):
    """A carry over rows, records or imports says so in the message (`carried over:` line, the first line no
    longer claims that no path was touched); a plain carry keeps the MG-RB1 line byte for byte."""
    w = world
    case, _ = _ledger_both_clean(w)
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (0, ""), out
    head = _load(receipt)["base"]
    lines = [ln for ln in out.splitlines() if ln.startswith(("Carried forward", "  carried over"))]
    first = (f"Carried forward mechanically: {{}} was reviewed at base {w.base[:12]}, the base is {head[:12]} "
             f"(1 commit(s) between, overlap compared as listed, merge clean)")
    second = "  carried over: ledger lines added on both sides, merged by git and all kept: DOGFOOD_LEDGER.md"
    assert lines == [first.format("a"), second, first.format("b"), second]
    plain = _case(w, {"docs/other.md": "text\n"}, name="cand-plain")
    _commit(w, {"docs/third.md": "third\n"}, "main edits another document")
    rc, out, err, _receipt = _run_product(w, capsys, plain)
    assert (rc, err) == (0, "")
    assert all("overlap compared" not in ln and "carried over" not in ln for ln in out.splitlines())
    assert "no candidate path touched, merge clean)" in out


# --- an unreadable register is a refusal, never a pass --------------------------------------------


def _forms() -> dict[str, str | bytes]:
    good = json.loads(_register())
    dup = _register().replace('"plan_snapshot_commit": "s0",', '"plan_snapshot_commit": "s0", "plan_snapshot_commit": "s1",')
    return {
        "not-json": "{not json",
        "an-array": "[]\n",
        "no-requirements": json.dumps({"what": "x"}) + "\n",
        "requirements-not-a-list": json.dumps({"requirements": {}}) + "\n",
        "row-without-an-id": json.dumps({**good, "requirements": [{"name": "x"}]}) + "\n",
        "row-id-twice": json.dumps({**good, "requirements": [good["requirements"][0]] * 2}) + "\n",
        "repeated-key": dup,
        "non-utf8": b"\xff\xfe{}",
    }


@pytest.mark.parametrize("form", sorted(_forms()))
@pytest.mark.parametrize("side", ["range", "candidate"])
def test_a_register_that_cannot_be_read_refuses_the_carry_in_both_gates(world, capsys, form, side):
    w = world
    content = _forms()[form]
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    if side == "candidate":
        case = _case_bytes(w, REG, data, why="not measurable: ")
        _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    else:
        case = _case(w, {REG: _register(edit={"R-B": {"note": "candidate note"}})}, eligible=False,
                     why="not measurable: ")
        (w.repo / REG).write_bytes(data)
        _git(w.repo, "add", "-A")
        _git(w.repo, "commit", "-qm", "main damages the register")
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert [_row(body, r)["problems"] for r in ("a", "b")] == [["STALE_BASE"], ["STALE_BASE"]]
    assert all("not measurable: " in p["detail"] and "REQUIREMENTS.json" in p["detail"] for p in body["problems"])
    assert all("carried_forward" not in row for row in body["reviews"])
    tool_rc, text, _body = _run_tool(w, case)
    assert tool_rc == 1 and "not measurable: " in text and "REQUIREMENTS.json" in text


# --- bounds, the report class, and what the gate does not run -----------------------------------------


def test_the_code_file_bound_is_exactly_256_in_both_gates(world, capsys):
    """256 touched code files on a side are followed, 257 are refused. Kills a bound of 255 or 257."""
    w = world
    assert mergegate.CARRY_MAX_CODE_FILES == 256
    files = {f"src/hoh/gen_{i:03d}.py": f"def f{i}():\n    return {i}\n" for i in range(257)}
    for count, expected in ((256, True), (257, False)):
        name = f"cand{count}"
        sub = dict(list(files.items())[:count])
        case = _case(w, sub, name=name, eligible=expected,
                     why="more than 256 code files are touched on one side")
        _commit(w, {"src/hoh/unrelated.py": f"def noop():\n    return {count}\n"}, f"main edits code {count}")
        rc, out, err, receipt = _run_product(w, capsys, case)
        assert (rc, err) == (0 if expected else 1, ""), out
        tool_rc, text, _body = _run_tool(w, case)
        assert tool_rc == (0 if expected else 1), text
        if not expected:
            assert all(case.why in p["detail"] for p in _load(receipt)["problems"])


def test_the_register_size_bound_is_exact_in_both_gates(world, capsys, monkeypatch):
    """A register of exactly `CARRY_MAX_BYTES` bytes is read, one byte more is refused (both gates); the matrix
    reading's output is at most `_MATRIX_OUT_MAX` (1 MiB) bytes. Kills `>` -> `>=`."""
    w = world
    # the candidate edits a top-level key only (no row is edited: no matrix is read), the range edits a row
    case = _case(w, {REG: _register(top={"plan_snapshot_commit": "s-candidate"})})
    main_text = _register(edit={"R-D": {"note": "main note"}})
    _commit(w, {REG: main_text}, "main edits R-D")
    size = max(len(t.encode()) for t in (_register(), main_text,
                                         _register(top={"plan_snapshot_commit": "s-candidate"})))
    assert mergegate.CARRY_MAX_BYTES == 4 * 1024 * 1024 and mergegate._MATRIX_OUT_MAX == 1024 * 1024
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_size", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    head = _git(w.repo, "rev-parse", "HEAD")
    for limit, expected in ((size, True), (size - 1, False)):
        with monkeypatch.context() as patched:
            patched.setattr(mergegate, "CARRY_MAX_BYTES", limit)
            product = mergegate.measure_carry(str(w.repo.resolve()), w.base, head, case.candidate)
        assert product.carried is expected, (limit, product.detail)
        tool.CARRY_MAX_BYTES = limit
        assert (tool.carried(case.candidate, head, w.base)[0] is not None) is expected
        if not expected:
            assert "larger than" in product.detail
            assert "too big" in tool.carried(case.candidate, head, w.base)[1]


def test_the_matrix_reading_output_is_bounded(world, monkeypatch):
    """The driver's answer is at most `_MATRIX_OUT_MAX` bytes; more is a reading that failed."""
    w = world
    case, _ = _rows_disjoint(w)
    head = _git(w.repo, "rev-parse", "HEAD")
    assert mergegate._MATRIX_OUT_MAX == 1024 * 1024
    assert mergegate.measure_carry(str(w.repo.resolve()), w.base, head, case.candidate).carried is True
    with monkeypatch.context() as patched:
        patched.setattr(mergegate, "_MATRIX_OUT_MAX", 10)
        product = mergegate.measure_carry(str(w.repo.resolve()), w.base, head, case.candidate)
    assert product.carried is False and "the matrix reading failed" in product.detail


def test_the_report_class_is_the_strands_naming_the_gate_reads_any_review_id(world, capsys):
    """`rev-a` / `rev-b` / `post-a` are review ids like any other: the carry does not look at them."""
    w = world
    case, _ = _rows_disjoint(w)
    for old, new in (("a=", "rev-a="), ("b=", "post-b=")):
        case.reviews = [new + r[len(old):] if r.startswith(old) else r for r in case.reviews]
    assert [r.split("=", 1)[0] for r in case.reviews] == ["rev-a", "post-b"]
    rc, out, err, receipt = _run_product(w, capsys, case)
    assert (rc, err) == (0, "") and {r["review"] for r in _load(receipt)["reviews"]} == {"rev-a", "post-b"}
    assert _run_tool(w, case)[0] == 0


def test_the_matrix_reading_is_one_isolated_run_with_the_bases_classifier(world, capsys, monkeypatch):
    """The reading runs `python -I` with no stdin and `_MATRIX_TIMEOUT` (120 s), writes the base's
    `tools/capability_matrix.py` over the merge tree's, and executes nothing of the candidate: a candidate
    that rewrites the classifier to leave a marker leaves none. Kills a classifier taken from the candidate."""
    w = world
    marker = w.tmp / "executed.txt"
    evil = f"open({str(marker)!r}, 'w').write('x')\n" + (ROOT / "tools" / "capability_matrix.py").read_text("utf-8")
    case = _case(w, {REG: _register(edit={"R-B": {"note": "candidate note"}}),
                     "tools/capability_matrix.py": evil})
    _commit(w, {"src/hoh/unrelated.py": "def noop():\n    return 1\n"}, "main edits code")
    suite = _suite(w, case)
    assert mergegate._MATRIX_TIMEOUT == 120.0
    calls = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), kwargs))
        return real(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", spy)
        rc, out, err, receipt = _run_product(w, capsys, case, suite=suite)
    assert (rc, err) == (0, ""), out
    assert not marker.exists()
    tool_rc, text, _tool = _run_tool(w, case, suite=suite)         # and the tool's own reading
    assert tool_rc == 0, text
    assert not marker.exists()
    readings = [(argv, kw) for argv, kw in calls if argv[:1] == [sys.executable]]
    assert len(readings) == 2                              # the reviewers' tree and the current one
    for argv, kw in readings:
        assert argv[1] == "-I" and kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == 120.0
        assert kw["capture_output"] is True and set(kw["env"]) == {"PATH", "LC_ALL"}


def test_a_carry_with_rows_writes_nothing_into_the_repository_and_leaves_no_scratch(world, capsys,
                                                                                      monkeypatch):
    w = world
    case, _ = _rows_disjoint(w)
    suite = _suite(w, case)
    scratch = w.tmp / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    before = _snapshot(w.repo)
    rc, out, err, receipt = _run_product(w, capsys, case, suite=suite)
    assert (rc, err) == (0, ""), out
    assert _contents(_snapshot(w.repo)) == _contents(before)
    assert list(scratch.iterdir()) == []


def test_a_matrix_reading_that_fails_is_a_stale_base_and_never_a_crash(world, capsys, monkeypatch):
    """The driver exits non-zero, times out, or prints something that is not one object of the rows asked:
    the review stays stale (exit 1) with the reason."""
    w = world
    case, _ = _rows_disjoint(w)
    real = subprocess.run

    def fail(argv, *args, **kwargs):
        if argv[:2] == [sys.executable, "-I"]:
            raise subprocess.TimeoutExpired(argv, 1)
        return real(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", fail)
        rc, out, err, receipt = _run_product(w, capsys, case, suite=False)
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert all("the matrix reading could not be run" in p["detail"] for p in body["problems"])
    assert all("carried_forward" not in row for row in body["reviews"])


def test_every_code_file_import_check_is_the_one_the_capability_matrix_uses(world):
    """`_imports` reads the names the matrix's import graph reads (`from . import m`, `from .m import x`,
    `import hoh.m`), anywhere in the file, and only the package's own modules."""
    w = world
    _commit(w, {"src/hoh/probe.py": "import json\nfrom . import wave\nfrom .alpha import alpha\n"
                                    "def f():\n    import hoh.gamma\n    from hoh.cli import main\n"}, "probe")
    head = _git(w.repo, "rev-parse", "HEAD")
    modules = {"wave", "alpha", "gamma", "cli", "orch"}
    got = mergegate._imports(str(w.repo), head, b"src/hoh/probe.py", modules)
    assert got == {"wave", "alpha", "gamma", "cli"}
    assert mergegate._imports(str(w.repo), head, b"src/hoh/missing.py", modules) == set()
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_ov", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    assert tool.imports_of(head, b"src/hoh/probe.py", modules) == got
    assert os.fspath(w.repo)


# --- revision 1: the range side of the bound, the archive bound, the merged register, the ledger rule ------


def test_the_code_file_bound_is_exactly_256_on_the_range_side_too(world, capsys):
    """The candidate edits one code file; the range edits 256 (followed) or 257 (not measurable), in both
    gates. Kills a bound that only the candidate's side obeys."""
    w = world
    names = [f"src/hoh/rgen_{i:03d}.py" for i in range(257)]
    for count, expected in ((256, True), (257, False)):
        case = _case(w, {"src/hoh/orch.py": _SRC["src/hoh/orch.py"] + f"# candidate {count}\n"},
                     name=f"cand-r{count}", eligible=expected,
                     why="more than 256 code files are touched on one side")
        _commit(w, {n: f"def f():\n    return {count}\n" for n in names[:count]}, f"main adds {count} files")
        rc, out, err, receipt = _run_product(w, capsys, case)
        assert (rc, err) == (0 if expected else 1, ""), out
        tool_rc, text, _body = _run_tool(w, case)
        assert tool_rc == (0 if expected else 1), text
        if not expected:
            assert all(case.why in p["detail"] for p in _load(receipt)["problems"]) and case.why in text


def test_the_archive_of_the_merge_tree_is_bounded_exactly_in_both_gates(world, monkeypatch):
    """`git ls-tree -l` of what the reading would archive is summed first: at the bound it is read, one byte
    less refuses, before any archive is made. Kills an unbounded archive."""
    w = world
    case, _ = _rows_disjoint(w)
    head = _git(w.repo, "rev-parse", "HEAD")
    tree = _git(w.repo, "merge-tree", "--write-tree", head, case.candidate).splitlines()[0]
    listing = subprocess.run(["git", "-C", str(w.repo), "ls-tree", "-r", "-l", tree, "--", "src/hoh", "tools",
                              "tests", "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json"],
                             capture_output=True, text=True, check=True).stdout
    total = sum(int(line.split("\t")[0].split()[3]) for line in listing.splitlines())
    assert mergegate._MATRIX_ARCHIVE_MAX == 64 * 1024 * 1024
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_arch", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    assert tool.MATRIX_ARCHIVE_MAX == 64 * 1024 * 1024
    for limit, expected in ((total, True), (total - 1, False)):
        with monkeypatch.context() as patched:
            patched.setattr(mergegate, "_MATRIX_ARCHIVE_MAX", limit)
            product = mergegate.measure_carry(str(w.repo.resolve()), w.base, head, case.candidate)
        assert product.carried is expected, (limit, product.detail)
        tool.MATRIX_ARCHIVE_MAX = limit
        assert (tool.carried(case.candidate, head, w.base)[0] is not None) is expected
        if not expected:
            assert "larger than" in product.detail
            assert "larger than" in tool.carried(case.candidate, head, w.base)[1]


def test_a_merged_register_that_git_made_unreadable_refuses_in_both_gates(world, monkeypatch):
    """Rows disjoint and a clean textual merge are not enough: the merged register is read too. A merge that
    produced something that is not a register (simulated: the blob of the merge tree is replaced) is not
    measurable. Kills a merged register that is never parsed."""
    w = world
    case, _ = _rows_disjoint(w)
    head = _git(w.repo, "rev-parse", "HEAD")
    real_product = mergegate._merge_tree

    def broken_product(repo, base, candidate, want=(), blobs=None):
        got = real_product(repo, base, candidate, want, blobs)
        if blobs is not None and REG.encode() in blobs:
            blobs[REG.encode()] = b'{"requirements": [{"id": "R-A"}, {"id": "R-A"}]}'
        return got

    with monkeypatch.context() as patched:
        patched.setattr(mergegate, "_merge_tree", broken_product)
        product = mergegate.measure_carry(str(w.repo.resolve()), w.base, head, case.candidate)
    assert product.carried is False
    assert "not measurable" in product.detail and "the merge tree" in product.detail
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_merged", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    real_tool = tool.merge_tree

    def broken_tool(base, cand, want=(), blobs=None):
        got = real_tool(base, cand, want, blobs)
        if blobs is not None and REG.encode() in blobs:
            blobs[REG.encode()] = b"{"
        return got

    tool.merge_tree = broken_tool
    record, why = tool.carried(case.candidate, head, w.base)
    assert record is None and "not measurable" in why and "the merge tree" in why


@pytest.mark.parametrize("m,c,b,h,merged,expected", [
    (b"a\nb\n", b"a\nb\nc\n", b"a\nb\n", b"a\nx\nb\n", b"a\nx\nb\nc\n", True),       # both add, kept
    (b"a\nb\n", b"a\nb\nc\n", b"a\nb\n", b"a\nb\n", b"a\nb\nc\n", True),               # the range adds nothing
    (b"", b"c\n", b"", b"x\n", b"x\nc\n", True),                                      # an empty or absent base
    (b"a\nb\n", b"a\nB\nc\n", b"a\nb\n", b"a\nx\nb\n", b"a\nx\nB\nc\n", False),   # the candidate edits a line
    (b"a\nb\n", b"a\nb\nc\n", b"a\nb\n", b"a\nB\n", b"a\nB\nc\n", False),            # the range edits a line
    (b"a\nb\n", b"a\nc\n", b"a\nb\n", b"a\nb\nx\n", b"a\nc\nx\n", False),            # the candidate removes one
    (b"a\nb\n", b"a\nb\nc\n", b"a\nb\n", b"a\nx\nb\n", b"a\nx\nb\n", False),        # the merge lost the candidate's line
    (b"a\nb\n", b"a\nb\nc\n", b"a\nb\n", b"a\nx\nb\n", b"a\nb\nc\n", False),        # the merge lost the range's line
])
def test_the_ledger_is_append_only_when_both_sides_add_and_the_merge_keeps_every_line(m, c, b, h, merged,
                                                                                   expected):
    """The pure rule of the one tolerated overlap, in the product and in the tool."""
    assert mergegate._append_only(m, c, b, h, merged) is expected
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_append", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    got = (tool.only_adds(m, c) and tool.only_adds(b, h) and tool.only_adds(c, merged)
           and tool.only_adds(h, merged))
    assert got is expected


def test_the_tool_refuses_a_merged_ledger_that_lost_a_line(world, monkeypatch):
    """The composition in the tool: both sides add, and the merged ledger (simulated) lacks the range's line.
    Kills a tool that compares the two sides but not the merged file."""
    w = world
    case, _ = _ledger_both_clean(w)
    head = _git(w.repo, "rev-parse", "HEAD")
    spec = importlib.util.spec_from_file_location("cross_check_merge_gate_ledger", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.REPO = str(w.repo)
    assert tool.carried(case.candidate, head, w.base)[0] is not None
    real = tool.merge_tree

    def lossy(base, cand, want=(), blobs=None):
        got = real(base, cand, want, blobs)
        if blobs is not None and LEDGER.encode() in blobs:
            blobs[LEDGER.encode()] = (blobs[LEDGER.encode()] or b"").replace(b"main entry added\n", b"")
        return got

    tool.merge_tree = lossy
    record, why = tool.carried(case.candidate, head, w.base)
    assert record is None and "'DOGFOOD_LEDGER.md'" in why and "only add lines" in why
