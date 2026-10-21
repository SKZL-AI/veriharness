"""`hoh.conflict`: whether two planned work items may run in parallel.

The module under test replaces the exact-string comparison of
`HohRunLauncher.depends_on` with a relation over *declared* footprints that
sees nested paths, spelling variants and globs, and that refuses (UNDECIDABLE,
which conflicts with everything) whenever a declaration cannot be trusted.

Each test names the property it pins. The pairs in `TABLE` are the ones the
exact-string rule gets wrong on the pin; every one of them is asserted twice,
in the order asked and reversed, because the relation is symmetric.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import random
import sys
import types
import unicodedata
from pathlib import Path

import pytest

from hoh import conflict as c
from hoh.project import TaskNode

DOTS = ".."
WW, WR, UN = c.Kind.WRITE_WRITE, c.Kind.WRITE_READ, c.Kind.UNDECIDABLE


def fp(item, writes=(), reads=()):
    return c.footprint(item, list(writes), list(reads))


def kinds(result):
    return {x.kind for x in result}


def both_ways(a, b):
    """`detect` in both argument orders; the two must be equal."""
    one, two = c.detect(a, b), c.detect(b, a)
    assert one == two, "the relation must be symmetric"
    return one


class Hostile:
    """A value whose own text methods must never be called."""

    def __str__(self):
        raise RuntimeError("__str__ was called")

    def __repr__(self):
        raise RuntimeError("__repr__ was called")


def exact_string_rule(a, b) -> bool:
    """`HohRunLauncher.depends_on` as it was before P3-21 slice 2: exact-string intersection of
    declared paths. A frozen copy, so that these tests keep measuring the relation against the rule
    it replaced now that the launcher delegates to `detect` (the launcher's own tests are
    `tests/test_derivedreads.py`)."""
    a_w, b_w = set(a.writes), set(b.writes)
    if a_w & b_w:
        return True
    return bool(a_w & set(b.semantic_reads) or b_w & set(a.semantic_reads))


#: (a writes, a reads, b writes, b reads, expected kinds). The first rows are
#: the pairs the pin's `depends_on` calls independent although they touch one
#: file.
TABLE = [
    (["src/hoh"], [], [], ["src/hoh/a.py"], {WR}),
    (["src/hoh/a.py"], [], [], ["src/hoh"], {WR}),
    (["./src//hoh/a.py"], [], ["src/hoh/a.py"], [], {WW}),
    (["README.md"], [], ["readme.md"], [], {WW}),
    (["docs/*.md"], [], ["docs/x.md"], [], {WW}),
    (["docs/*/x.md"], [], ["docs/a/x.md"], [], {WW}),
    (["docs/"], [], ["docs/x.md"], [], {WW}),
    ([], [], ["docs/x.md"], [], {UN}),
    (["/etc/passwd"], [], ["docs/x.md"], [], {UN}),
    ([DOTS + "/x"], [], ["docs/x.md"], [], {UN}),
    (["docs/*/" + DOTS + "/" + DOTS + "/README.md"], [], ["README.md"], [], {UN}),
    (["README.md"], [], ["README.md"], [], {WW}),
    (["docs/a.md"], ["src/hoh/a.py"], ["docs/b.md"], ["src/hoh/b.py"], set()),
]


# --------------------------------------------------------------------------- #
# R1 - the core relation
# --------------------------------------------------------------------------- #

def test_equal_write_sets_conflict():
    result = both_ways(
        fp("a", ["README.md", "docs/x.md"]), fp("b", ["docs/x.md", "README.md"]),
    )
    assert [(x.kind, x.writer, x.other, x.path, x.other_path) for x in result] == [
        (WW, "a", "b", "README.md", "README.md"),
        (WW, "a", "b", "docs/x.md", "docs/x.md"),
    ]


def test_writing_what_the_other_reads_conflicts_in_both_directions():
    writer = fp("a", ["src/hoh/a.py"])
    reader = fp("b", [], ["src/hoh/a.py"])
    result = both_ways(writer, reader)
    assert [(x.kind, x.writer, x.other) for x in result] == [(WR, "a", "b")]
    # the roles swapped: the item named `b` writes, the item named `a` reads
    result = both_ways(fp("a", [], ["src/hoh/a.py"]), fp("b", ["src/hoh/a.py"]))
    assert [(x.kind, x.writer, x.other) for x in result] == [(WR, "b", "a")]
    # one item that writes and reads, against one that does the same
    mutual = both_ways(fp("a", ["x.py"], ["y.py"]), fp("b", ["y.py"], ["x.py"]))
    assert {(x.writer, x.other) for x in mutual} == {("a", "b"), ("b", "a")}


def test_a_directory_contains_its_files():
    for directory, inside in [("src/hoh", "src/hoh/a.py"), ("docs", "docs/deep/er/x.md")]:
        for a, b in [
            (fp("a", [directory]), fp("b", [inside])),
            (fp("a", [inside]), fp("b", [directory])),
            (fp("a", [directory]), fp("b", [], [inside])),
            (fp("a", [inside]), fp("b", [], [directory])),
        ]:
            assert both_ways(a, b), (directory, inside)
    # the repository root contains everything
    assert kinds(both_ways(fp("a", ["."]), fp("b", ["any/path.txt"]))) == {WW}


def test_nesting_is_by_component_not_by_string_prefix():
    assert both_ways(fp("a", ["src/hoh/a"]), fp("b", ["src/hoh/ab"])) == ()
    assert both_ways(fp("a", ["src/hoh"]), fp("b", ["src/hohx/a.py"])) == ()
    assert both_ways(fp("a", ["docs"]), fp("b", [], ["docs-old/x.md"])) == ()
    assert both_ways(fp("a", ["src/hoh/a"]), fp("b", ["src/hoh/a/b"]))


def test_reads_never_conflict_with_reads():
    result = both_ways(
        fp("a", ["docs/a.md"], ["src/hoh/shared.py"]),
        fp("b", ["docs/b.md"], ["src/hoh/shared.py", "src/hoh"]),
    )
    assert result == ()
    # an item may read what it writes itself; that is no conflict with anyone
    assert both_ways(fp("a", ["x.py"], ["x.py"]), fp("b", ["y.py"], ["y.py"])) == ()


def test_independent_items_are_independent():
    a = fp("a", ["docs/a.md"], ["src/hoh/a.py"])
    b = fp("b", ["docs/b.md"], ["src/hoh/b.py"])
    assert both_ways(a, b) == ()


def test_the_relation_is_symmetric_and_deterministic():
    for aw, ar, bw, br, expected in TABLE:
        a, b = fp("a", aw, ar), fp("b", bw, br)
        result = both_ways(a, b)
        assert kinds(result) == expected, (aw, ar, bw, br)
        assert c.detect(a, b) == result, "a second call gives the same answer"
        # the declaration order does not matter either
        shuffled = both_ways(fp("a", aw[::-1], ar[::-1]), fp("b", bw[::-1], br[::-1]))
        assert shuffled == result
    # a WRITE_WRITE pair is reported in id order whichever way it was asked
    result = c.detect(fp("zeta", ["x"]), fp("alpha", ["x"]))
    assert (result[0].writer, result[0].other) == ("alpha", "zeta")
    # the table rows the exact-string rule calls independent
    weaker = 0
    for aw, ar, bw, br, expected in TABLE:
        a = TaskNode(id="a", writes=aw, semantic_reads=ar)
        b = TaskNode(id="b", writes=bw, semantic_reads=br)
        if expected and not exact_string_rule(a, b):
            weaker += 1
    assert weaker >= 10, "the exact-string rule misses most of these pairs"


def test_the_report_names_the_writers_path_first_and_prints_every_kind():
    ww = c.detect(fp("b", ["README.md"]), fp("a", ["README.md"]))
    assert [str(x) for x in ww] == ["WRITE_WRITE: a writes 'README.md'; b also writes 'README.md'"]
    wr = c.detect(fp("b", [], ["src/hoh/a.py"]), fp("a", ["src/hoh"]))
    assert [str(x) for x in wr] == ["WRITE_READ: a writes 'src/hoh'; b reads 'src/hoh/a.py'"]
    # the writer's spelling is `path`, the reader's is `other_path`
    assert (wr[0].path, wr[0].other_path) == ("src/hoh", "src/hoh/a.py")
    # the writer is the later id here, and still named first
    wr = c.detect(fp("a", [], ["x/y.py"]), fp("b", ["x"]))
    assert str(wr[0]) == "WRITE_READ: b writes 'x'; a reads 'x/y.py'"
    un = c.detect(fp("b", ["/abs"]), fp("a", ["x"]))
    assert len(un) == 1 and un[0].kind is UN
    assert (un[0].writer, un[0].other, un[0].path, un[0].other_path) == ("a", "b", "", "")
    assert str(un[0]).startswith("UNDECIDABLE a <-> b: ")
    assert str(un[0]) == f"UNDECIDABLE a <-> b: {un[0].detail}"


# --------------------------------------------------------------------------- #
# R2 - spelling, Unicode, case, globs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, canonical", [
    ("src/hoh/a.py", "src/hoh/a.py"),
    ("./src//hoh/a.py", "src/hoh/a.py"),
    ("src/./hoh/./a.py", "src/hoh/a.py"),
    ("docs/", "docs"),
    ("docs//", "docs"),
    ("./docs/./", "docs"),
    (".", ""),
    ("./", ""),
    (".//.", ""),
])
def test_spelling_variants_are_the_same_path(raw, canonical):
    assert c.normalize_path(raw) == canonical
    # the variant meets the clean spelling in the relation
    other = canonical or "any/file.txt"
    assert kinds(both_ways(fp("a", [raw]), fp("b", [other]))) == {WW}


def test_unicode_normal_forms_are_the_same_path():
    # a precomposed letter and its decomposed spelling; the angstrom sign, the
    # Latin capital A with ring, and A + combining ring are one name
    for x, y in [("é.txt", "é.txt"), ("Å/f", "Å/f"), ("å/f", "Å/f")]:
        assert kinds(both_ways(fp("a", [x]), fp("b", [y]))) == {WW}, (x, y)
    # one spelling is kept for the two, the smaller string
    assert [s for _k, s in fp("a", ["é", "é"]).writes] == ["é"]
    # different letters stay different
    assert both_ways(fp("a", ["é"]), fp("b", ["e"])) == ()


def test_the_caseless_key_is_the_canonical_caseless_match():
    cases = [
        ("README.md", "readme.md"),
        ("STRASSE", "straße"),
        # canonical caseless matching is NFD(casefold(NFD(c))): NFC then
        # casefold leaves these two apart (iota with dialytika and tonos
        # against capital iota with dialytika plus a combining acute)
        ("ΐ", "Ϊ́"),
        # and dropping the inner NFD leaves these two apart (a precomposed
        # letter with a mark against its precomposed equivalent)
        ("ᾀ", "ᾀ"),
    ]
    for x, y in cases:
        assert kinds(both_ways(fp("a", [x]), fp("b", [y]))) == {WW}, (x, y)
        reference = unicodedata.normalize(
            "NFD", unicodedata.normalize("NFD", x).casefold()
        ) == unicodedata.normalize(
            "NFD", unicodedata.normalize("NFD", y).casefold()
        )
        assert reference, "the test's own cases are caseless-equal by the recipe"
    # the comparison is per component: the same letters in another order differ
    assert both_ways(fp("a", ["ab/c"]), fp("b", ["a/bc"])) == ()


def test_a_glob_widens_to_its_directory():
    assert c.normalize_path("docs/*.md") == "docs"
    assert c.normalize_path("docs/*/x.md") == "docs"
    assert c.normalize_path("src/hoh/**/*.py") == "src/hoh"
    assert kinds(both_ways(fp("a", ["docs/*.md"]), fp("b", ["docs/x.md"]))) == {WW}
    assert kinds(both_ways(fp("a", ["docs/*/x.md"]), fp("b", ["docs/a/x.md"]))) == {WW}
    assert kinds(both_ways(fp("a", ["docs/*.md"]), fp("b", [], ["docs/deep/y.md"]))) == {WR}
    # widening stops at the directory: a sibling is not touched
    assert both_ways(fp("a", ["docs/*.md"]), fp("b", ["src/x.md"])) == ()


def test_a_glob_ends_the_path_at_its_first_glob_component():
    assert c.normalize_path("a/b*/c/d") == "a"
    assert c.normalize_path("a/*/b/*/c") == "a"
    assert c.normalize_path("a/b/c?/d/e") == "a/b"
    # a literal component behind a glob never narrows the path again
    wide = fp("a", ["docs/*/x.md"])
    assert [s for _k, s in wide.writes] == ["docs"]
    assert kinds(both_ways(wide, fp("b", ["docs/other/y.md"]))) == {WW}


@pytest.mark.parametrize("glob", list("*?[]{}"))
def test_every_glob_character_widens(glob):
    raw = f"docs/a{glob}b/x.md"
    assert c.normalize_path(raw) == "docs"
    assert kinds(both_ways(fp("a", [raw]), fp("b", ["docs/q/z.md"]))) == {WW}
    # in the middle of a later component the same
    assert c.normalize_path(f"a/b/c{glob}") == "a/b"


def test_a_brace_alternative_at_the_start_widens_to_the_root():
    assert c.normalize_path("{a,b}/x") == ""
    assert c.normalize_path("*.md") == ""
    assert c.normalize_path("?/x") == ""
    anywhere = fp("a", ["{a,b}/x"])
    assert [s for _k, s in anywhere.writes] == [""]
    assert kinds(both_ways(anywhere, fp("b", ["some/far/away.txt"]))) == {WW}
    assert kinds(both_ways(anywhere, fp("b", [], ["README.md"]))) == {WR}


# --------------------------------------------------------------------------- #
# R3 - every untrusted declaration fails closed
# --------------------------------------------------------------------------- #

#: Twenty-six spellings `normalize_path` refuses.
UNREADABLE = [
    "",
    "/etc/passwd",
    "/",
    DOTS,
    DOTS + "/x",
    "a/" + DOTS,
    "a/" + DOTS + "/b",
    "docs/*/" + DOTS + "/" + DOTS + "/README.md",
    "docs/*/" + DOTS + "/x",
    "{a,b}/" + DOTS + "/x",
    "docs/a?b/" + DOTS,
    "a\n",
    "a\x00b",
    "a\tb",
    "a\x7f",
    " a",
    "a ",
    "a" + chr(92) + "b",
    ":x",
    "\ud800",
    "x" * (c.MAX_PATH_BYTES + 1),
    "é" * (c.MAX_PATH_BYTES // 2 + 1),
    "/".join(["a"] * (c.MAX_DEPTH + 1)),
    "/".join(["a"] * c.MAX_DEPTH + ["."] * 5 + ["b"]),
    "\r\n",
    "a\x1fb",
]


@pytest.mark.parametrize("raw", UNREADABLE)
def test_an_unreadable_path_makes_the_item_conflict_with_everything(raw):
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)
    bad = c.footprint("a", ["fine/file.py", raw], [])
    assert bad.problems and bad.item == "a"
    assert [s for _k, s in bad.writes] == ["fine/file.py"], "the entry is dropped, the rest kept"
    if len(raw) > 100:
        assert not any(raw in p for p in bad.problems), "a long value is quoted, not echoed whole"
    for other in (fp("b", ["docs/x.md"]), fp("b", [], ["anything"]), fp("b", ["fine/file.py"])):
        result = both_ways(bad, other)
        assert len(result) == 1 and result[0].kind is UN
        assert (result[0].writer, result[0].other) == ("a", "b")


@pytest.mark.parametrize("raw", UNREADABLE)
def test_an_unreadable_read_path_is_equally_undecidable(raw):
    bad = c.footprint("a", ["fine/file.py"], [raw])
    assert bad.problems and bad.reads == ()
    result = both_ways(bad, fp("b", ["docs/x.md"]))
    assert len(result) == 1 and result[0].kind is UN
    assert "reads" in result[0].detail


@pytest.mark.parametrize("declared", ["src/hoh/a.py", b"src/hoh/a.py", bytearray(b"x"), 5, None, object(), 1.5])
def test_a_collection_that_is_not_a_collection_is_undecidable(declared):
    on_write = c.footprint("a", declared, ["r.py"])
    on_read = c.footprint("a", ["w.py"], declared)
    for bad in (on_write, on_read):
        assert bad.problems
        result = both_ways(bad, fp("b", ["z.py"]))
        assert [x.kind for x in result] == [UN]
    # a bare string is not read character by character
    assert c.footprint("a", "src", ["r.py"]).writes == ()


def test_a_huge_declaration_is_cut_not_followed():
    pulled = itertools.count()

    def endless():
        for i in range(100_000):
            next(pulled)
            yield f"f{i}.py"

    bad = c.footprint("a", endless(), ["r.py"])
    assert bad.problems
    assert next(pulled) <= c.MAX_PATHS + 1, "at most MAX_PATHS + 1 entries pulled"
    assert len(bad.writes) <= c.MAX_PATHS
    # a long list is cut the same way, on the reading side too
    assert c.footprint("a", ["w.py"], [f"r{i}" for i in range(5000)]).problems
    assert [x.kind for x in c.detect(bad, fp("b", ["z.py"]))] == [UN]


def test_an_exploding_declaration_is_undecidable():
    def explodes_at_once():
        raise ValueError("first read")
        yield  # pragma: no cover

    def explodes_midway():
        yield "a.py"
        raise RuntimeError("second read")

    class BadIter:
        def __iter__(self):
            raise OSError("no iterator")

    class BadNext:
        def __iter__(self):
            return self

        def __next__(self):
            raise KeyError("no next")

    for make in (explodes_at_once, explodes_midway, BadIter, BadNext):
        # a generator is spent once read: a fresh one for each side
        for bad in (c.footprint("a", make(), ["r"]), c.footprint("a", ["w"], make())):
            assert bad.problems
            assert [x.kind for x in both_ways(bad, fp("b", ["z"]))] == [UN]
    hostile = c.footprint("a", [Hostile(), "ok.py"], [Hostile()])
    assert hostile.problems and [s for _k, s in hostile.writes] == ["ok.py"]
    # the problem names the type, not the value
    assert any("Hostile" in p for p in hostile.problems)


def test_building_a_footprint_never_raises():
    class Lying:
        __class__ = str  # isinstance(x, str) is True; it is not a str

    class Unsized(str):
        def __len__(self):
            raise RuntimeError("len")

        def __str__(self):
            raise RuntimeError("str")

        def __repr__(self):
            raise RuntimeError("repr")

        def encode(self, *a, **k):
            raise RuntimeError("encode")

        def split(self, *a, **k):
            raise RuntimeError("split")

    class Angry:
        def __getattr__(self, name):
            raise RuntimeError(name)

    odd = [None, 0, 1.5, True, b"x", "x", (), [], {}, set(), object(), Hostile(),
           Lying(), Unsized("x"), Angry(), len, int, c, c.Kind, ["a", None, b"x", 3, Hostile()],
           {"a": 1}, {"docs/x.md"}, range(5), iter(()), "x" * 100_000]
    for item, writes, reads in itertools.product(odd, odd, odd):
        result = c.footprint(item, writes, reads)
        assert isinstance(result, c.Footprint)
        if not result.item:
            assert result.problems
    for node in odd + [types.SimpleNamespace(), types.SimpleNamespace(id="a")]:
        result = c.footprint_of(node)
        assert isinstance(result, c.Footprint) and result.problems


def test_an_item_id_cannot_flood_or_forge_the_report():
    flooding = c.footprint("x" * 100_000, ["a"], [])
    assert flooding.item == "" and flooding.problems
    assert all(len(p) < 400 for p in flooding.problems), "the id is quoted by at most 40 characters"
    forged = c.footprint("a\nWRITE_WRITE: x writes 'y'; z also writes 'y'", ["a"], [])
    assert forged.item == "" and "\n" not in "".join(forged.problems)
    # a bad id against an honest item: one bounded UNDECIDABLE line
    result = c.detect(forged, fp("b", ["z"]))
    assert len(result) == 1 and result[0].kind is UN
    assert "\n" not in str(result[0]) and len(str(result[0])) < 1500
    # a hand-built footprint cannot smuggle a control character in either
    for bad in ("a\nb", "a\x00", "a\x85b"):
        with pytest.raises(c.ConflictInputError):
            c.Footprint(item=bad, writes=(), reads=(), problems=())
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item="i" * (c.MAX_ITEM_CHARS + 1), writes=(), reads=(), problems=())
    assert c.Footprint(item="i" * c.MAX_ITEM_CHARS, writes=(), reads=(), problems=())
    # hand-built problem text is cleaned and cut before it reaches a report
    loud = c.Footprint(item="a", writes=(), reads=(), problems=("p\nq" + "z" * 5000,) * 50)
    report = c.detect(loud, fp("b", ["z"]))[0]
    assert "\n" not in report.detail and len(report.detail) < 5000


def test_the_problems_of_one_item_are_cut():
    assert c.MAX_PROBLEMS == 20
    many = c.footprint("a", [f"/abs{i}" for i in range(50)], [])
    assert len(many.problems) == c.MAX_PROBLEMS + 1
    assert many.problems[-1] == "30 more problems not listed"
    exactly = c.footprint("a", [f"/abs{i}" for i in range(c.MAX_PROBLEMS)], [])
    assert len(exactly.problems) == c.MAX_PROBLEMS
    assert not any("more problems" in p for p in exactly.problems)
    one_more = c.footprint("a", [f"/abs{i}" for i in range(c.MAX_PROBLEMS + 1)], [])
    assert one_more.problems[-1] == "1 more problems not listed"
    # a cut footprint is still UNDECIDABLE, and its report is bounded
    report = c.detect(many, fp("b", ["z"]))
    assert len(report) == 1 and report[0].kind is UN and len(report[0].detail) < 5000


def test_declaring_nothing_is_not_independence():
    nothing = c.footprint("a", [], [])
    assert nothing.problems == () and nothing.writes == () and nothing.reads == ()
    for other in (fp("b", ["docs/x.md"]), fp("b", [], ["docs/x.md"])):
        result = both_ways(nothing, other)
        assert [x.kind for x in result] == [UN]
        assert "declares no path" in result[0].detail
    # judged in `detect`, so a hand-built empty footprint is caught too
    built = c.Footprint(item="a", writes=(), reads=(), problems=())
    assert [x.kind for x in both_ways(built, fp("b", ["docs/x.md"]))] == [UN]
    # and a node that declares nothing
    node = TaskNode(id="a")
    assert [x.kind for x in both_ways(c.footprint_of(node), fp("b", ["x"]))] == [UN]
    # two empty items are UNDECIDABLE as well, once
    assert [x.kind for x in c.detect(nothing, c.footprint("b", [], []))] == [UN]


@pytest.mark.parametrize("bad_id", [None, 7, "", "x" * (c.MAX_ITEM_CHARS + 1), "a\nb", "a\x00", b"a", ("a",), Hostile()])
def test_an_unreadable_item_id_is_undecidable(bad_id):
    result = c.footprint(bad_id, ["docs/x.md"], [])
    assert result.item == "" and result.problems
    assert [x.kind for x in both_ways(result, fp("b", ["elsewhere.md"]))] == [UN]
    # two such items share the id "" and are UNDECIDABLE for that reason too
    assert [x.kind for x in c.detect(result, c.footprint(None, ["q"], []))] == [UN]


def test_the_same_item_id_twice_is_undecidable():
    result = both_ways(fp("a", ["x"]), fp("a", ["y"]))
    assert [x.kind for x in result] == [UN]
    assert (result[0].writer, result[0].other) == ("a", "a")
    assert "both items are called 'a'" in result[0].detail
    # the same footprint against itself is no exception
    same = fp("a", ["x"])
    assert [x.kind for x in c.detect(same, same)] == [UN]


def test_a_hand_built_footprint_cannot_claim_what_normalisation_would_refuse():
    good = c.footprint("a", ["x/y"], ["r"])
    key_w, key_r = good.writes[0][0], good.reads[0][0]
    assert c.Footprint(item="a", writes=good.writes, reads=good.reads, problems=()) == good
    refused = [
        dict(item="a", writes=((key_w, "./x/y"),), reads=(), problems=()),
        dict(item="a", writes=((key_w, "x/y/"),), reads=(), problems=()),
        dict(item="a", writes=((key_w, "x/*"),), reads=(), problems=()),
        dict(item="a", writes=((key_w, "/x/y"),), reads=(), problems=()),
        dict(item="a", writes=((("x", "y"), "x/" + DOTS),), reads=(), problems=()),
        dict(item="a", writes=(), reads=((key_r, "r/"),), problems=()),
        dict(item="a", writes=(), reads=((key_r, "x/y"),), problems=()),
        dict(item="a", writes=((key_r, "x/y"),), reads=(), problems=()),
        dict(item="a", writes=((("X", "Y"), "x/y"),), reads=(), problems=()),
        dict(item="a", writes=(("x/y", "x/y"),), reads=(), problems=()),
        dict(item="a", writes=(key_w,), reads=(), problems=()),
        dict(item="a", writes=[(key_w, "x/y")], reads=(), problems=()),
        dict(item="a", writes=(), reads=[(key_r, "r")], problems=()),
        dict(item="a", writes=(), reads=(), problems=["p"]),
        dict(item="a", writes=(), reads=(), problems=(5,)),
        dict(item="", writes=good.writes, reads=(), problems=()),
        dict(item=None, writes=good.writes, reads=(), problems=()),
        dict(item=7, writes=good.writes, reads=(), problems=()),
    ]
    for kwargs in refused:
        with pytest.raises(c.ConflictInputError):
            c.Footprint(**kwargs)
    # the root has the empty spelling and the empty key, and is accepted
    root = c.Footprint(item="a", writes=(((), ""),), reads=(), problems=())
    assert [x.kind for x in c.detect(root, fp("b", ["anything"]))] == [WW]
    # an empty id is fine when a problem says why
    assert c.Footprint(item="", writes=(), reads=(), problems=("why",))


def test_detect_refuses_what_is_not_a_footprint():
    good = fp("a", ["x"])
    for bad in (None, "a", ["x"], {"item": "a"}, ("a", "x"), 5, types.SimpleNamespace(item="a")):
        with pytest.raises(TypeError):
            c.detect(bad, good)
        with pytest.raises(TypeError):
            c.detect(good, bad)
    with pytest.raises(TypeError):
        c.detect(Hostile(), good)


# --------------------------------------------------------------------------- #
# R4 - the ceilings are exact
# --------------------------------------------------------------------------- #

def test_the_path_and_collection_ceilings_are_exact():
    assert (c.MAX_PATH_BYTES, c.MAX_DEPTH, c.MAX_PATHS) == (4096, 64, 1000)
    assert (c.MAX_EVIDENCE, c.MAX_ITEM_CHARS, c.MAX_PROBLEMS) == (20, 256, 20)
    assert c.normalize_path("x" * 4096) == "x" * 4096
    with pytest.raises(c.ConflictInputError):
        c.normalize_path("x" * 4097)
    at_limit = c.footprint("a", [f"f{i}.py" for i in range(c.MAX_PATHS)], [])
    assert at_limit.problems == () and len(at_limit.writes) == c.MAX_PATHS
    one_past = c.footprint("a", [f"f{i}.py" for i in range(c.MAX_PATHS + 1)], [])
    assert one_past.problems and len(one_past.writes) == c.MAX_PATHS
    # the same on the reading side
    assert c.footprint("a", ["w"], [f"r{i}" for i in range(c.MAX_PATHS)]).problems == ()
    assert c.footprint("a", ["w"], [f"r{i}" for i in range(c.MAX_PATHS + 1)]).problems
    # duplicates count as entries: the ceiling is on what is declared
    assert c.footprint("a", ["same"] * (c.MAX_PATHS + 1), []).problems
    assert c.footprint("a", ["same"] * c.MAX_PATHS, []).problems == ()


def test_the_depth_ceiling_counts_components_after_normalisation():
    deepest = "/".join(["a"] * c.MAX_DEPTH)
    assert c.normalize_path(deepest) == deepest
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(deepest + "/a")
    # empty and `.` components do not count; many of them are still fine
    padded = "./" + "/./".join(["a"] * c.MAX_DEPTH) + "//" * 10
    assert c.normalize_path(padded) == deepest
    # a glob ends the path first, so what stays is what is counted
    assert c.normalize_path("/".join(["a"] * 3) + "/*/" + "/".join(["b"] * 100)) == "a/a/a"
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(deepest + "/a/*")


def test_the_path_ceiling_counts_bytes_not_characters():
    two_bytes = "é"  # 1 character, 2 bytes
    assert c.normalize_path(two_bytes * (c.MAX_PATH_BYTES // 2)) == two_bytes * (c.MAX_PATH_BYTES // 2)
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(two_bytes * (c.MAX_PATH_BYTES // 2 + 1))
    three_bytes = "€"
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(three_bytes * (c.MAX_PATH_BYTES // 3 + 1))
    four_bytes = "\U0001f600"
    assert c.normalize_path(four_bytes * (c.MAX_PATH_BYTES // 4))
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(four_bytes * (c.MAX_PATH_BYTES // 4) + "a")


def test_the_evidence_is_cut_after_sorting_and_the_verdict_is_not_cut():
    names = [f"d/f{i:02d}" for i in range(30)]
    wide, narrow = fp("a", names), fp("b", ["d"])
    result = both_ways(wide, narrow)
    assert len(result) == c.MAX_EVIDENCE
    order = lambda x: (x.kind.value, x.writer, x.other, x.path, x.other_path)  # noqa: E731
    assert list(result) == sorted(result, key=order)
    # the kept ones are the first twenty of the *sorted* whole, not of the input
    kept = {x.path for x in result}
    assert kept == set(sorted(names)[: c.MAX_EVIDENCE])
    # a kind that sorts early but is found late survives the cut: the cut is
    # applied to the sorted whole (WRITE_READ sorts before WRITE_WRITE)
    late = both_ways(fp("a", names, ["e"]), fp("b", ["d", "e/x"]))
    assert len(late) == c.MAX_EVIDENCE
    assert late[0].kind is WR and late[0].writer == "b"
    assert list(late) == sorted(late, key=order)
    # many reasons of several kinds: still a conflict, still cut, still sorted
    mixed = both_ways(fp("a", names, names), fp("b", names, names))
    assert len(mixed) == c.MAX_EVIDENCE and mixed
    assert list(mixed) == sorted(mixed, key=order)
    # an item with many conflicts reversed into the other direction is the same
    assert c.detect(narrow, wide) == result


def test_one_spelling_is_kept_per_key_whatever_the_iteration_order():
    spellings = ["readme.md", "README.md", "ReadMe.md", "./README.md", "README.md/"]
    results = set()
    for permutation in itertools.permutations(spellings):
        built = c.footprint("a", list(permutation), [])
        assert [s for _k, s in built.writes] == ["README.md"]
        results.add(built)
    assert len(results) == 1
    # a set (no order at all) gives the same footprint
    assert c.footprint("a", set(spellings), []) == next(iter(results))
    # keys are distinct entries, and the entries are sorted
    two = c.footprint("a", ["b/x", "A/y", "B/X"], [])
    assert [s for _k, s in two.writes] == ["A/y", "B/X"]
    assert two.writes == tuple(sorted(two.writes))


# --------------------------------------------------------------------------- #
# R5 - a real node, the launcher's rule, purity
# --------------------------------------------------------------------------- #

def test_a_task_node_is_read_by_its_declared_fields():
    node = TaskNode(
        id="p3-01", writes=["src/hoh/frontier.py", "./tests//test_frontier.py"],
        semantic_reads=["src/hoh/launcher.py"],
        produces=["src/hoh/elsewhere.py"], invalidates=["src/hoh/elsewhere.py"],
    )
    got = c.footprint_of(node)
    assert got == c.footprint("p3-01", node.writes, node.semantic_reads)
    assert got.item == "p3-01" and got.problems == ()
    assert [s for _k, s in got.writes] == ["src/hoh/frontier.py", "tests/test_frontier.py"]
    assert [s for _k, s in got.reads] == ["src/hoh/launcher.py"]
    # `produces` and `invalidates` are not consulted
    other = TaskNode(id="x", writes=["src/hoh/other.py"], semantic_reads=["docs/a.md"])
    assert both_ways(got, c.footprint_of(other)) == ()
    # a duck-typed object works as well
    duck = types.SimpleNamespace(id="d", writes=("src/hoh/launcher.py",), semantic_reads=())
    assert [(x.kind, x.writer) for x in both_ways(got, c.footprint_of(duck))] == [(WR, "d")]


def test_a_node_lacking_one_declared_field_is_undecidable():
    full = dict(id="a", writes=["x.py"], semantic_reads=["r.py"])
    for missing in ("id", "writes", "semantic_reads"):
        fields = {k: v for k, v in full.items() if k != missing}
        built = c.footprint_of(types.SimpleNamespace(**fields))
        assert built.problems and any(missing in p for p in built.problems), missing
        assert [x.kind for x in both_ways(built, fp("b", ["z.py"]))] == [UN]

    class Raises:
        id = "a"
        writes = ["x.py"]

        @property
        def semantic_reads(self):
            raise ValueError("no")

    built = c.footprint_of(Raises())
    assert built.problems and [x.kind for x in c.detect(built, fp("b", ["z"]))] == [UN]
    assert c.footprint_of(None).problems


def test_it_never_serialises_less_than_the_exact_string_rule_did():
    rng = random.Random(20261012)
    vocabulary = ["README.md", "readme.md", "docs", "docs/x.md", "docs/a/x.md", "src/hoh",
                  "src/hoh/a.py", "src/hoh/b.py", "CLAIMS.json", "tests", "tests/t.py", "./docs/"]

    def node(name):
        return TaskNode(
            id=name,
            writes=rng.sample(vocabulary, rng.randint(0, 3)),
            semantic_reads=rng.sample(vocabulary, rng.randint(0, 3)),
        )

    serialised = 0
    for _ in range(400):
        a, b = node("a"), node("b")
        if exact_string_rule(a, b):
            serialised += 1
            assert c.detect(c.footprint_of(a), c.footprint_of(b)), (a, b)
    assert serialised > 20, "the sample must exercise the property"
    # the table: every pair the launcher serialises, the relation does too
    for aw, ar, bw, br, _expected in TABLE:
        a = TaskNode(id="a", writes=aw, semantic_reads=ar)
        b = TaskNode(id="b", writes=bw, semantic_reads=br)
        if exact_string_rule(a, b):
            assert c.detect(c.footprint_of(a), c.footprint_of(b))


#: The only modules `src/hoh/conflict.py` may import, and what it may take from them.
PURE_IMPORTS = {"__future__", "dataclasses", "enum", "itertools", "re", "typing", "unicodedata"}
PURE_FROM = {("__future__", "annotations"), ("dataclasses", "dataclass"), ("enum", "StrEnum"),
             ("itertools", "islice"), ("typing", "Any")}
#: The only attributes the module may read off an imported module.
PURE_MODULE_ATTRIBUTES = {("re", "compile"), ("unicodedata", "category"), ("unicodedata", "normalize")}
#: The only dunder attributes the module may read.
PURE_DUNDERS = {"__name__", "__setattr__", "__str__"}
#: Names that would reach the outside world, or run text, from a module that
#: only reads strings.
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "print", "__import__", "input", "breakpoint",
                   "__builtins__", "globals", "locals", "vars", "dir", "setattr", "delattr"}


def test_the_module_stays_pure():
    """The whole module, read as a syntax tree: a fixed list of standard-library
    imports and of what is taken from them, no name that opens, runs or prints,
    no attribute chain through an imported module, no dunder reach, `getattr`
    only on the plan node, and nothing at module level but imports,
    definitions, constants and the docstring."""
    path = Path(c.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, taken = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            taken |= {(node.module, alias.name) for alias in node.names}
    assert modules <= PURE_IMPORTS, sorted(modules - PURE_IMPORTS)
    assert taken <= PURE_FROM, sorted(taken - PURE_FROM)
    imported = modules | {m for m, _ in taken}
    assert imported <= PURE_IMPORTS
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & FORBIDDEN_NAMES, sorted(names & FORBIDDEN_NAMES)
    # an imported module is only ever used as `module.attribute`, from the list, never passed
    # around or chained (`fnmatch.os.listdir`, `re.functools`)
    bare = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in modules]
    as_value = [n.value for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id in modules]
    assert len(bare) == len(as_value), "a module is used other than as `module.attribute`"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in modules:
                assert (node.value.id, node.attr) in PURE_MODULE_ATTRIBUTES, (node.value.id, node.attr)
            if node.attr.startswith("__") and node.attr.endswith("__"):
                assert node.attr in PURE_DUNDERS, node.attr
            if isinstance(node.value, ast.Attribute):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                assert not (isinstance(root, ast.Name) and root.id in modules), "a chain through a module"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            assert isinstance(node.args[0], ast.Name) and node.args[0].id == "node"
    for index, node in enumerate(tree.body):
        if index == 0:
            assert isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant), "the docstring"
        else:
            assert isinstance(node, (ast.ImportFrom, ast.Import, ast.Assign, ast.AnnAssign,
                                     ast.FunctionDef, ast.ClassDef)), ast.dump(node)[:80]
    # executing the file under another name loads no other hoh module
    before = {m for m in sys.modules if m == "hoh" or m.startswith("hoh.")}
    spec = importlib.util.spec_from_file_location("conflict_probe_copy", path)
    probe = importlib.util.module_from_spec(spec)
    sys.modules["conflict_probe_copy"] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.modules.pop("conflict_probe_copy", None)
    assert {m for m in sys.modules if m == "hoh" or m.startswith("hoh.")} == before
    assert not hasattr(c, "conflicts"), "the name is taken by the contract verb"
    assert issubclass(c.ConflictInputError, ValueError)


# --------------------------------------------------------------------------- #
# R6 - a glob component that may match the parent directory is refused (F1)
# --------------------------------------------------------------------------- #

#: Components that can be, or assemble, the name `..` under some shell. Bash
#: 5.2 expands only the brace forms to it; older bash, POSIX sh and other
#: shells also match the pattern forms, and the module cannot know which shell
#: a planner's text will meet, so every one is refused.
PARENT_SHAPES = [
    "docs/{" + DOTS + ",a}/x.md",              # the reviewed escape
    "docs/{a," + DOTS + "}/x.md",
    "docs/{.," + DOTS + "}/x",
    "docs/{{" + DOTS + ",b},a}/x",             # nested braces
    "docs/{{" + DOTS + "},a}/x",
    "docs/{a,{b," + DOTS + "}}/x",
    "docs/[.][.]/x",
    "docs/" + DOTS + "*/x",
    "docs/.*/x",
    "docs/.?/x",
    "docs/.[.]/x",
    "docs/.{.,a}/x",
    "docs/.{*,a}/x",
    "docs/{.,x}{.,y}/z",
    "docs/{.,}{.,}/z",
    "docs/[.]?/x",
    "docs/[[:punct:]][[:punct:]]/x",
    "docs/[[.period.]][[.period.]]/x",
    "docs/@(" + DOTS + "|a)/x",                # extended glob
    "docs/+(.)/x",
    "docs/!(a)/x",
    "docs/{1" + DOTS + "3}/x",                 # a brace sequence expression
    "docs/*/{" + DOTS + ",a}/{" + DOTS + ",a}/x.md",   # behind the cut
    "docs/*/.*/x",
    "{" + DOTS + ",a}/x",                      # first component
    "x/{.," + DOTS + "}",                      # last component
    "docs/{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}/x",      # more alternatives than the budget
]


@pytest.mark.parametrize("raw", PARENT_SHAPES)
def test_a_glob_component_that_may_match_the_parent_is_refused(raw):
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)
    bad = c.footprint("a", [raw], [])
    assert bad.problems and bad.writes == ()
    # it really is an escape: item a writes the repository root's x.md, which a
    # widened `docs` would not have contained
    for other in (fp("b", ["x.md"]), fp("b", [], ["docs/q/y"])):
        result = both_ways(bad, other)
        assert [x.kind for x in result] == [UN]
    assert [x.kind for x in both_ways(c.footprint("a", ["ok.md", raw], []), fp("b", ["z"]))] == [UN]


#: Patterns that are NOT refused: widened to the directory before the first
#: glob component, or to the root.
ORDINARY_PATTERNS = [
    ("*", ""), ("?", ""), ("*/x", ""), ("[!_]*", ""), ("[a-z]*", ""), ("*.{md,py}", ""), ("{a,b}/x", ""),
    (".*ignore", ""), ("docs/*.md", "docs"), ("src/**/test_[ab].py", "src"), ("tests/test_*.py", "tests"),
    ("docs/{a,b}{c,d}/x", "docs"), (".github/*", ".github"), ("a/.hidden/*", "a/.hidden"),
    ("x/...y/*", "x/...y"), ("@types/node/*", "@types/node"), ("file(1).md", "file(1).md"),
    ("a+b/c*", "a+b"), ("docs/a{b/x", "docs"),
    # spellings that look like `..` once the wildcards are deleted but begin with a wildcard or a
    # letter, so no shell can read them as `..`
    ("*.*.*", ""), ("data.*.json", ""), ("v*.*.*", ""), ("release-*.*.md", ""), ("a.*.py", ""),
    ("docs/*..*/x", "docs"), ("docs/*.*/x", "docs"), ("lib.*.so", ""),
    # a leading dot followed by something that cannot be a second dot
    ("docs/.[!a]x/y", "docs"), ("docs/.[a-c]*/y", "docs"), ("docs/.[^.]x/y", "docs"),
    ("docs/.?x/y", "docs"), (".[a-c]*", ""), ("docs/.{a,b}/y", "docs"),
]


@pytest.mark.parametrize("raw, canonical", ORDINARY_PATTERNS)
def test_an_ordinary_glob_is_widened_not_refused(raw, canonical):
    assert c.normalize_path(raw) == canonical
    assert c.footprint("a", [raw], []).problems == ()


def test_the_alternative_budget_is_exact_and_shared_by_the_path():
    assert c.MAX_BRACE_ALTERNATIVES == 64
    eight = "{a,b,c,d,e,f,g,h}"
    assert c.normalize_path(f"d/{eight}{eight}/x") == "d"                    # 8 x 8 = 64
    five, thirteen = "{a,b,c,d,e}", "{f,g,h,i,j,k,l,m,n,o,p,q,r}"
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(f"d/{five}{thirteen}/x")                            # 5 x 13 = 65
    # two components of one path draw on the same budget: 40 + 24, and 40 + 25
    forty, twenty_four, twenty_five = f"{five}{eight}", "{a,b,c,d}{e,f,g,h,i,j}", f"{five}{five}"
    assert c.normalize_path(f"d/{forty}/{twenty_four}/x") == "d"
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(f"d/{forty}/{twenty_five}/x")
    # an unmatched brace is a literal, as in bash, and costs one alternative
    assert c.normalize_path("d/a{b/x") == "d"
    assert c.normalize_path("d/a}b/x") == "d"
    # the budget of characters: 32 alternatives of 64 characters is exactly 2048, one more glob
    # component (one character) is 2049
    group = "{" + ",".join("abcdefghijklmnopqrstuvwxyzABCDEF") + "}"          # 32 alternatives
    exact = "z" * (c.MAX_GLOB_COMPONENT_CHARS - len(group)) + group           # 128 characters of text
    assert len(exact) == 128 and c.normalize_path(f"d/{exact}") == "d"
    assert c.normalize_path(f"d/{exact}/q") == "d"
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(f"d/{exact}/*")
    # and the length of one glob component: 128 characters, not 129
    assert c.normalize_path("d/*" + "a" * 127) == "d"
    with pytest.raises(c.ConflictInputError):
        c.normalize_path("d/*" + "a" * 128)
    # every glob component costs at least one alternative: 64 plain ones, not 65
    assert c.normalize_path("/".join(["*"] * c.MAX_BRACE_ALTERNATIVES)) == ""
    with pytest.raises(c.ConflictInputError):
        c.normalize_path("/".join(["*"] * (c.MAX_BRACE_ALTERNATIVES + 1)))


def test_the_analysis_cost_is_bounded_by_size_not_only_by_count():
    """Review a B3: 64 alternatives of 3.6 KB each cost 163 ms per path, and
    4000 `[` in one component cost 306 ms per path (1000 paths: 46 s). Both are
    refused by size now, before any analysis; the third shape stays within the
    budget of characters and is analysed."""
    import time
    assert (c.MAX_GLOB_COMPONENT_CHARS, c.MAX_EXPANDED_CHARS) == (128, 2048)
    big_alternatives = lambda i: "d/" + ("a" * 600 + "{b,c}") * 6 + str(i)       # one 3.6 KB component
    brackets = lambda i: f"d{i}/" + "[" * 4000
    many_groups = lambda i: "docs/" + "{a,b}" * 8 + f"/p{i}"                       # 256 alternatives
    wide_alternatives = lambda i: "docs/" + "x" * 60 + "{a,b,c,d,e,f,g,h}" * 2 + f"/p{i}"  # 64 x 62 chars
    within = lambda i: f"docs/{{a,b,c,d}}{{e,f,g,h}}.md/p{i}"                      # 16 alternatives
    started = time.monotonic()
    for make, refused in ((big_alternatives, True), (brackets, True), (many_groups, True),
                          (wide_alternatives, True), (within, False)):
        declared = [make(i) for i in range(c.MAX_PATHS)]
        built = c.footprint("a", declared, [])
        assert bool(built.problems) is refused, make.__name__
        if refused:
            assert built.writes == ()
            for one in declared[:5]:
                with pytest.raises(c.ConflictInputError):
                    c.normalize_path(one)
        else:
            assert len(built.writes) == 1
    assert time.monotonic() - started < 10     # measured: about 0.4 s for all five lists


# --------------------------------------------------------------------------- #
# R7 - the minor findings of the integration review
# --------------------------------------------------------------------------- #

FORBIDDEN_IDS = ["a\u2028b", "a\u2029b", "a\u202eb", "a\u200bb", "a\ufeffb", "a\ud800", "a\u0085b", "a\x00b",
                 "a\u200db", "a\u200cb"]    # U+200D and U+200C are format characters too: emoji
                                              # sequences, Persian and Indic ids are refused (stated limit)


@pytest.mark.parametrize("bad_id", FORBIDDEN_IDS)
def test_an_item_id_with_a_format_separator_or_surrogate_is_refused(bad_id):
    built = c.footprint(bad_id, ["x"], [])
    assert built.item == "" and built.problems
    assert [x.kind for x in both_ways(built, fp("b", ["y"]))] == [UN]
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item=bad_id, writes=(), reads=(), problems=())
    report = str(c.detect(built, fp("b", ["y"]))[0])
    assert len(report.splitlines()) == 1
    report.encode("utf-8")                         # no lone surrogate reaches a log


def test_an_ordinary_non_ascii_item_id_is_kept():
    for name in ("\u00fcber-\u00fc", "\u4efb\u52a1-1", "caf\u00e9"):
        assert c.footprint(name, ["x"], []).item == name


def test_a_report_line_escapes_whatever_a_hand_built_conflict_carries():
    forged = c.Conflict(WW, "a\u2028WRITE_WRITE: x writes 'y'; z also writes 'y'", "b\u202e", "p\ud800", "q\x00",
                        "d\u2029e")
    for kind in (WW, WR, UN):
        text = str(c.Conflict(kind, forged.writer, forged.other, forged.path, forged.other_path, forged.detail))
        assert len(text.splitlines()) == 1
        assert not any(ch in text for ch in "\u2028\u2029\u202e\ud800\x00")
        text.encode("utf-8")
    assert str(c.Conflict(WW, 5, None, object(), 7, 1.5)).startswith("WRITE_WRITE: <int> writes")


def test_the_canonical_spelling_rule_of_a_hand_built_footprint_is_checked_on_its_own():
    """Each entry below carries a key that IS the key of its spelling, so only
    the rule that the spelling be canonical can refuse it."""
    for spelling in ("x/*", "x/", "x/.", "x//y", "./x", "x/{a,b}", "x/..", "/x"):
        key = c._key(spelling)
        assert key == c._key(spelling)
        with pytest.raises(c.ConflictInputError):
            c.Footprint(item="a", writes=((key, spelling),), reads=(), problems=())
        with pytest.raises(c.ConflictInputError):
            c.Footprint(item="a", writes=(), reads=((key, spelling),), problems=())


def test_a_hand_built_footprint_is_bounded_like_a_declared_one():
    entries = tuple(sorted(c.footprint("a", [f"d/f{i}" for i in range(c.MAX_PATHS)], []).writes))
    assert len(entries) == c.MAX_PATHS
    assert c.Footprint(item="a", writes=entries, reads=(), problems=())
    assert c.Footprint(item="a", writes=(), reads=entries, problems=())
    extra = entries + ((("d", "extra"), "d/extra"),)
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item="a", writes=extra, reads=(), problems=())
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item="a", writes=(), reads=extra, problems=())


class Liar:
    """Not a `str`, but `isinstance(x, str)` is true."""

    @property
    def __class__(self):
        return str


def test_an_object_that_claims_to_be_a_str_is_refused_not_raised():
    assert isinstance(Liar(), str)
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item=Liar(), writes=(), reads=(), problems=("why",))
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item="a", writes=(), reads=(), problems=(Liar(),))
    with pytest.raises(c.ConflictInputError):
        c.Footprint(item="a", writes=(((), Liar()),), reads=(), problems=())
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(Liar())
    built = c.footprint(Liar(), [Liar(), "ok"], [Liar()])
    assert built.item == "" and built.problems
    assert [x.kind for x in both_ways(built, fp("b", ["z"]))] == [UN]
    with pytest.raises(TypeError):
        c.detect(Liar(), fp("b", ["z"]))


@pytest.mark.parametrize("raw", ["~", "~/x", "~user/x", "$HOME/x", "a/$x", "a/${x}/b", "a/`b`", "C:/Windows/x",
                                 "c:x", "a/'b'", 'a/"b"', "a/b's.md"])
def test_shell_syntax_is_not_a_repository_path(raw):
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)
    assert [x.kind for x in both_ways(c.footprint("a", [raw], []), fp("b", ["z"]))] == [UN]


@pytest.mark.parametrize("raw", ["a/b~c", "a/~b", "a/b:c", "ab:c", "dir/c:/x", "a/b#c", "a/b%20c", "a/b=c"])
def test_text_that_only_resembles_shell_syntax_is_a_path(raw):
    assert c.normalize_path(raw) == raw


@pytest.mark.parametrize("raw", ["a/.. /b", "a/.../b", "a/ /b", "a/. /b", "a/.../", ". ./x"])
def test_a_component_of_only_dots_and_spaces_may_name_the_parent_and_is_refused(raw):
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)
    assert [x.kind for x in both_ways(c.footprint("a", [raw], []), fp("b", ["z"]))] == [UN]


def test_a_component_with_dots_among_other_characters_is_a_path():
    for raw in (".github/x", "a/.b/c", "a/b./c", "a/...y/c", "a/.y./c", "a/. b/c"):
        assert c.normalize_path(raw) == raw


def test_a_nested_brace_group_is_refused_at_any_depth_without_recursion():
    for depth in (2, 3, 20, 1500):
        deep = "d/" + "{" * depth + "a,b" + "}" * depth
        with pytest.raises(c.ConflictInputError):
            c.normalize_path(deep)
        assert c.footprint("a", [deep, "ok"], []).problems
    assert c.normalize_path("d/{a,b}{c,d}/x") == "d"        # side by side is fine


# --------------------------------------------------------------------------- #
# R8 - the rule against real bash, and by construction (review round 2)
# --------------------------------------------------------------------------- #

#: Every string of length 1-5 over `. / { } , [ ] ^ ! * ? a` (all of them), and every 14th string of length 6
#: over the same alphabet, for which real bash 5.2 -- with `globskipdots` on and off and `dotglob` on and off,
#: in a directory that holds `docs/` and a file `x` beside it -- expands `docs/<shape>/x` (form 1) or `docs/<shape>`
#: (form 2) to a word with a `..` component, plus the shapes the two reviewers sent. Each entry is
#: `<forms>:<shape>`; the data is the output of the sweep, the test does not need a shell.
BASH_ESCAPES = (
    "12:.* 12:.. 12:.? 12:.** 12:.*. 12:.*/ 12:.*? 12:..* 12:../ 12:.?* 12:.?/ 12:/.* 12:/.. 12:/.? "
    "12:!/.. 12:*/.* 12:*/.. 12:*/.? 12:,/.. 12:.*** 12:.**. 12:.**/ 12:.**? 12:.*.* 12:.*./ 12:.*/* "
    "12:.*/. 12:.*// 2:.*/? 12:.*?* 12:.*?/ 12:..** 12:..*/ 12:../! 12:../* 12:../, 12:../. 12:..// "
    "12:../? 12:../[ 12:../] 12:../^ 12:../a 12:../{ 12:../} 12:./.* 12:./.. 12:./.? 12:.?** 12:.?*/ "
    "12:.?/* 12:.?/. 12:.?// 2:.?/? 12:.[.] 12:/.** 12:/.*. 12:/.*/ 12:/.*? 12:/..* 12:/../ 12:/.?* "
    "12:/.?/ 12://.* 12://.. 12://.? 12:?/.* 12:?/.. 12:?/.? 12:[/.. 12:]/.. 12:^/.. 12:a/.* 12:a/.. "
    "12:a/.? 12:{/.. 12:}/.. 12:!!/.. 12:!*/.. 12:!,/.. 12:!./.. 12:!/../ 12:!//.. 12:!?/.. 12:![/.. "
    "12:!]/.. 12:!^/.. 12:!a/.. 12:!{/.. 12:!}/.. 12:*!/.. 12:**/.* 12:**/.. 12:**/.? 12:*,/.. 12:*./.. "
    "12:*/.** 12:*/.*. 12:*/.*/ 12:*/.*? 12:*/..* 12:*/../ 12:*/.?* 12:*/.?/ 12:*//.* 12:*//.. 12:*//.? "
    "12:*?/.* 12:*?/.. 12:*?/.? 12:*[/.. 12:*]/.. 12:*^/.. 12:*a/.* 12:*a/.. 12:*a/.? 12:*{/.. 12:*}/.. "
    "12:,!/.. 12:,*/.. 12:,,/.. 12:,./.. 12:,/../ 12:,//.. 12:,?/.. 12:,[/.. 12:,]/.. 12:,^/.. 12:,a/.. "
    "12:,{/.. 12:,}/.. 12:.!/.. 12:.**** 12:.***. 12:.***/ 12:.***? 12:.**.* 12:.**./ 12:.**/* 12:.**/. "
    "12:.**// 2:.**/? 12:.**?* 12:.**?/ 12:.*.** 12:.*.*/ 12:.*./* 12:.*./. 12:.*.// 2:.*./? 12:.*/** "
    "12:.*/*/ 12:.*/*? 12:.*/.* 12:.*/.. 12:.*/./ 12:.*/.? 12:.*//* 12:.*//. 12:.*/// 2:.*//? 12:.*/?* "
    "12:.*?** 12:.*?*/ 12:.*?/* 12:.*?/. 12:.*?// 2:.*?/? 12:.*[.] 12:.*{,} 12:.,/.. 12:..*** 12:..**/ "
    "12:..*/* 12:..*/. 12:..*// 2:..*/? 12:../!! 12:../!* 12:../!, 12:../!. 12:../!/ 12:../!? 12:../![ "
    "12:../!] 12:../!^ 12:../!a 12:../!{ 12:../!} 12:../*! 12:../** 12:../*, 12:../*. 12:../*/ 12:../*? "
    "12:../*[ 12:../*] 12:../*^ 12:../*a 12:../*{ 12:../*} 12:../,! 12:../,* 12:../,, 12:../,. 12:../,/ "
    "12:../,? 12:../,[ 12:../,] 12:../,^ 12:../,a 12:../,{ 12:../,} 12:../.! 12:../.* 12:../., 12:../.. "
    "12:.././ 12:../.? 12:../.[ 12:../.] 12:../.^ 12:../.a 12:../.{ 12:../.} 12:..//! 12:..//* 12:..//, "
    "12:..//. 12:../// 12:..//? 12:..//[ 12:..//] 12:..//^ 12:..//a 12:..//{ 12:..//} 12:../?! 12:../?* "
    "12:../?, 12:../?. 12:../?/ 12:../?? 12:../?[ 12:../?] 12:../?^ 12:../?a 12:../?{ 12:../?} 12:../[! "
    "12:../[* 12:../[, 12:../[. 12:../[/ 12:../[? 12:../[[ 12:../[] 12:../[^ 12:../[a 12:../[{ 12:../[} "
    "12:../]! 12:../]* 12:../], 12:../]. 12:../]/ 12:../]? 12:../][ 12:../]] 12:../]^ 12:../]a 12:../]{ "
    "12:../]} 12:../^! 12:../^* 12:../^, 12:../^. 12:../^/ 12:../^? 12:../^[ 12:../^] 12:../^^ 12:../^a "
    "12:../^{ 12:../^} 12:../a! 12:../a* 12:../a, 12:../a. 12:../a/ 12:../a? 12:../a[ 12:../a] 12:../a^ "
    "12:../aa 12:../a{ 12:../a} 12:../{! 12:../{* 12:../{, 12:../{. 12:../{/ 12:../{? 12:../{[ 12:../{] "
    "12:../{^ 12:../{a 12:../{{ 12:../{} 12:../}! 12:../}* 12:../}, 12:../}. 12:../}/ 12:../}? 12:../}[ "
    "12:../}] 12:../}^ 12:../}a 12:../}{ 12:../}} 12:..{,} 12:./.** 12:./.*. 12:./.*/ 12:./.*? 12:./..* "
    "12:./../ 12:./.?* 12:./.?/ 12:.//.* 12:.//.. 12:.//.? 12:.?*** 12:.?**/ 12:.?*/* 12:.?*/. 12:.?*// "
    "2:.?*/? 12:.?/** 12:.?/*/ 12:.?/*? 12:.?/.* 12:.?/.. 12:.?/./ 12:.?/.? 12:.?//* 12:.?//. 12:.?/// "
    "2:.?//? 12:.?/?* 12:.?{,} 12:.[!!] 12:.[!*] 12:.[!,] 12:.[!?] 12:.[![] 12:.[!]] 12:.[!^] 12:.[!a] "
    "12:.[!{] 12:.[!}] 12:.[*.] 12:.[,.] 12:.[.!] 12:.[.*] 12:.[.,] 12:.[..] 12:.[.?] 12:.[.[] 12:.[.]* "
    "12:.[.]/ 12:.[.^] 12:.[.a] 12:.[.{] 12:.[.}] 12:.[/.. 12:.[?.] 12:.[].] 12:.[^!] 12:.[^*] 12:.[^,] "
    "12:.[^?] 12:.[^[] 12:.[^]] 12:.[^^] 12:.[^a] 12:.[^{] 12:.[^}] 12:.[a.] 12:.[{.] 12:.[}.] 12:.]/.. "
    "12:.^/.. 12:.a/.. 12:.{*,} 12:.{,*} 12:.{,.} 12:.{,?} 12:.{,}* 12:.{,}. 12:.{,}? 12:.{.,} 12:.{/.. "
    "12:.{?,} 12:.}/.. 12:/!/.. 12:/*/.* 12:/*/.. 12:/*/.? 12:/,/.. 12:/.*** 12:/.**. 12:/.**/ 12:/.**? "
    "12:/.*.* 12:/.*./ 12:/.*/* 12:/.*/. 12:/.*// 2:/.*/? 12:/.*?* 12:/.*?/ 12:/..** 12:/..*/ 12:/../! "
    "12:/../* 12:/../, 12:/../. 12:/..// 12:/../? 12:/../[ 12:/../] 12:/../^ 12:/../a 12:/../{ 12:/../} "
    "12:/./.* 12:/./.. 12:/./.? 12:/.?** 12:/.?*/ 12:/.?/* 12:/.?/. 12:/.?// 2:/.?/? 12:/.[.] 12://.** "
    "12://.*. 12://.*/ 12://.*? 12://..* 12://../ 12://.?* 12://.?/ 12:///.* 12:///.. 12:///.? 12:/?/.* "
    "12:/?/.. 12:/?/.? 12:/[/.. 12:/]/.. 12:/^/.. 12:/a/.* 12:/a/.. 12:/a/.? 12:/{/.. 12:/}/.. 12:?!/.. "
    "12:?*/.* 12:?*/.. 12:?*/.? 12:?,/.. 12:?./.. 12:?/.** 12:?/.*. 12:?/.*/ 12:?/.*? 12:?/..* 12:?/../ "
    "12:?/.?* 12:?/.?/ 12:?//.* 12:?//.. 12:?//.? 12:??/.* 12:??/.. 12:??/.? 12:?[/.. 12:?]/.. 12:?^/.. "
    "12:?a/.. 12:?{/.. 12:?}/.. 12:[!/.. 12:[*/.. 12:[,/.. 12:[./.. 12:[/../ 12:[//.. 12:[?/.. 12:[[/.. "
    "12:[]/.. 12:[^/.. 12:[a/.. 12:[{/.. 12:[}/.. 12:]!/.. 12:]*/.. 12:],/.. 12:]./.. 12:]/../ 12:]//.. "
    "12:]?/.. 12:][/.. 12:]]/.. 12:]^/.. 12:]a/.. 12:]{/.. 12:]}/.. 12:^!/.. 12:^*/.. 12:^,/.. 12:^./.. "
    "12:^/../ 12:^//.. 12:^?/.. 12:^[/.. 12:^]/.. 12:^^/.. 12:^a/.. 12:^{/.. 12:^}/.. 12:a!/.. 12:a*/.* "
    "12:a*/.. 12:a*/.? 12:a,/.. 12:a./.. 12:a/.** 12:a/.*. 12:a/.*/ 12:a/.*? 12:a/..* 12:a/../ 12:a/.?* "
    "12:a/.?/ 12:a//.* 12:a//.. 12:a//.? 12:a?/.. 12:a[/.. 12:a]/.. 12:a^/.. 12:aa/.. 12:a{/.. 12:a}/.. "
    "12:{!/.. 12:{*/.. 12:{,.*} 12:{,..} 12:{,.?} 12:{,.}* 12:{,.}. 12:{,.}? 12:{,/.. 12:{,}.* 12:{,}.. "
    "12:{,}.? 12:{.*,} 12:{.,}* 12:{.,}. 12:{.,}? 12:{..,} 12:{./.. 12:{.?,} 12:{/../ 12:{//.. 12:{?/.. "
    "12:{[/.. 12:{]/.. 12:{^/.. 12:{a/.. 12:{{/.. 12:{}/.. 12:}!/.. 12:}*/.. 12:},/.. 12:}./.. 12:}/../ "
    "12:}//.. 12:}?/.. 12:}[/.. 12:}]/.. 12:}^/.. 12:}a/.. 12:}{/.. 12:}}/.. 12:!!!/.. 12:!**/.. "
    "12:!,,/.. 12:!../.. 12:!/../* 12:!/?/.. 12:!?[/.. 12:![]/.. 12:!]^/.. 12:!^a/.. 12:!a{/.. 12:!{}/.. "
    "12:*!!/.. 12:***/.* 12:**//.. 12:*,*/.. 12:*.*/.. 12:*.}/.. 2:*/.*/a 12:*/../a 12:*//.** 12:*/^/.. "
    "12:*?/..* 12:*?{/.. 12:*[}/.. 12:*^!/.. 12:*a*/.* 12:*a//.. 12:*{//.. 12:*}?/.. 12:,![/.. 12:,*]/.. "
    "12:,,^/.. 12:,.a/.. 12:,/../^ 12:,?!/.. 12:,[*/.. 12:,],/.. 12:,^./.. 12:,a/../ 12:,{//.. 12:,}?/.. "
    "12:.![/.. 12:.***/. 12:.**/.* 2:.**?/? 12:.*./.. 12:.*/*// 12:.*/.// 2:.*///? 12:.*?/*/ 12:.*[!,] "
    "12:.*[.?] 12:.*[^?] 12:.*{*,} 12:.*{,}? 12:.,./.. 12:..**/. 12:..*/?* 12:../!!{ 12:../!,! 12:../!., "
    "12:../!// 12:../!?[ 12:../![^ 12:../!]{ 12:../!a! 12:../!{, 12:../!}/ 12:../*![ 12:../**^ 12:../*,{ "
    "12:../*/! 12:../*?, 12:../*[/ 12:../*][ 12:../*^^ 12:../*a{ 12:../*}! 12:../,!, 12:../,*/ 12:../,,[ "
    "12:../,.^ 12:../,/{ 12:../,[! 12:../,], 12:../,^/ 12:../,a[ 12:../,{^ 12:../,}{ 12:../.*! 12:../.,, "
    "12:../../ 12:.././[ 12:../.?^ 12:../.[{ 12:../.^! 12:../.a, 12:../.{/ 12:../.}[ 12:..//!^ 12:..//*{ "
    "12:..//.! 12:..///, 12:..//?/ 12:..//[[ 12:..//]^ 12:..//^{ 12:..//{! 12:..//}, 12:../?!/ 12:../?*[ "
    "12:../?,^ 12:../?.{ 12:../??! 12:../?[, 12:../?]/ 12:../?^[ 12:../?a^ 12:../?{{ 12:../[!! 12:../[*, "
    "12:../[,/ 12:../[.[ 12:../[/^ 12:../[?{ 12:../[]! 12:../[^, 12:../[a/ 12:../[{[ 12:../[}^ 12:../]!{ "
    "12:../],! 12:../]., 12:../]// 12:../]?[ 12:../][^ 12:../]]{ 12:../]a! 12:../]{, 12:../]}/ 12:../^![ "
    "12:../^*^ 12:../^,{ 12:../^/! 12:../^?, 12:../^[/ 12:../^][ 12:../^^^ 12:../^a{ 12:../^}! 12:../a!, "
    "12:../a*/ 12:../a,[ 12:../a.^ 12:../a/{ 12:../a[! 12:../a], 12:../a^/ 12:../aa[ 12:../a{^ 12:../a}{ "
    "12:../{*! 12:../{,, 12:../{./ 12:../{/[ 12:../{?^ 12:../{[{ 12:../{^! 12:../{a, 12:../{{/ 12:../{}[ "
    "12:../}!^ 12:../}*{ 12:../}.! 12:../}/, 12:../}?/ 12:../}[[ 12:../}]^ 12:../}^{ 12:../}{! 12:../}}, "
    "12:..a/.. 12:..{,}/ 12:./*/.? 12:./..** 12:././.* 12:.//..* 12:./a/.. 12:.?*/.* 12:.?/**? 12:.?/../ "
    "12:.?///* 12:.?a/.. 12:.?{,}/ 12:.[!!?] 12:.[!*]/ 12:.[!,{] 12:.[![!] 12:.[!]?] 12:.[!^]/ 12:.[!a{] "
    "12:.[!}*] 12:.[*.*] 12:.[*a.] 12:.[,.^] 12:.[.!?] 12:.[.*]* 12:.[.,^] 12:.[..{] 12:.[.?}] 12:.[.]/* "
    "12:.[.^{] 12:.[.{!] 12:.[.},] 12:.[?,.] 12:.[??.] 12:.[]!.] 12:.[].}] 12:.[^!^] 12:.[^*}] 12:.[^?*] "
    "12:.[^[[] 12:.[^]^] 12:.[^^}] 12:.[^{,] 12:.[^}]* 12:.[a.[] 12:.[{*.] 12:.[{/.. 12:.[}.[] 12:.]*/.. "
    "12:.^,/.. 12:.a./.. 12:.{!,}. 12:.{*,}* 12:.{,*/} 12:.{,./} 12:.{,/}. 12:.{,?}/ 12:.{,}** 12:.{.,?} "
    "12:.{/,.} 12:.{/,}* 12:.{/,}. 12:.{?,^} 12:.{[/.. 12:.{^/.. 12:.{},.} 12:.{},}? 12:.}}/.. 12:/*!/.. "
    "12:/*//.* 12:/,!/.. 12:/.**** 12:/.*./* 2:/.*//? 12:/..*/. 12:/../!} 12:/../,* 12:/../.. 12:/..//? "
    "12:/../?] 12:/../[a 12:/../]} 12:/../a* 12:/../{. 12:/../}? 12:/./.?* 12:/.?/.* 12:/.[![] 12:/.[.]* "
    "12:/.[^]] 12:/.{,?} 12://.**. 12://../* 12://.?** 12:///.?/ 12://}/.. 12:/?/.?/ 12:/[*/.. 12:/],/.. "
    "12:/^./.. 12:/a,/.. 12:/a[/.. 12:/{,/.. 12:/{[/.. 12:/}]/.. 12:?!^/.. 12:?*/..* 12:?*{/.. 12:?,}/.. "
    "12:?/!/.. 12:?/.*?* 12:?/../{ 12:?//.*. 12:?/a/.. 12:??/../ 12:?[*/.. 12:?],/.. 12:?^./.. 12:?a/../ "
    "12:?{//.. 12:?}?/.. 12:[![/.. 12:[*]/.. 12:[,^/.. 12:[.a/.. 12:[/../^ 12:[?!/.. 12:[[*/.. 12:[],/.. "
    "12:[^./.. 12:[a/../ 12:[{./.. 12:[}/../ 12:]!//.. 12:]*?/.. 12:],[/.. 12:].]/.. 12:]/../[ 12:]/{/.. "
    "12:]?}/.. 12:]]!/.. 12:]^*/.. 12:]a,/.. 12:]{./.. 12:]}/../ 12:^!//.. 12:^*?/.. 12:^,[/.. 12:^.]/.. "
    "12:^/../[ 12:^/{/.. 12:^?}/.. 12:^]!/.. 12:^^*/.. 12:^a,/.. 12:^{./.. 12:^}/../ 12:a!//.. 12:a*/.** "
    "12:a*^/.. 12:a,a/.. 12:a.{/.. 2:a/.*/? 12:a/../^ 12:a/.[.] 12:a/]/.. 12:a?^/.. 12:a[a/.. 12:a]{/.. "
    "12:a^}/.. 12:a{!/.. 12:a}*/.. 12:{!,.*} 12:{![/.. 12:{*,/.. 12:{,!/.. 12:{,,/.. 12:{,.,}. 12:{,.}*. "
    "12:{,/.}. 12:{,]/.. 12:{,}.*. 12:{.*,,} 12:{.*/.. 12:{.,.}* 12:{.,/}. 12:{.,]}. 12:{.,}./ 12:{..,a} "
    "12:{.?,[} 12:{/!/.. 12:{/.,}. 12:{/../} 12:{?,.*} 12:{?[/.. 12:{[,/.. 12:{]!/.. 12:{]//.. 12:{^,.}. "
    "12:{^{/.. 12:{a./.. 12:{{/../ 12:{},.}* 12:{}a/.. 12:}!{/.. 12:}*}/.. 12:}.!/.. 12:}/*/.. 12:}/./.. "
    "12:}?/../ 12:}[//.. 12:}]?/.. 12:}^[/.. 12:}a]/.. 12:}{^/.. 12:}}a/.. "
).split()


def test_the_sweep_corpus_is_what_it_says():
    assert len(BASH_ESCAPES) == 998
    lengths = {len(entry.split(":", 1)[1]) for entry in BASH_ESCAPES}
    assert lengths == {2, 3, 4, 5, 6}
    assert {entry.split(":", 1)[0] for entry in BASH_ESCAPES} <= {"1", "2", "12"}


def test_every_shape_real_bash_expands_to_a_parent_is_refused_or_widened_to_the_root():
    unsafe = []
    for entry in BASH_ESCAPES:
        forms, shape = entry.split(":", 1)
        templates = ([f"docs/{shape}/x"] if "1" in forms else []) + ([f"docs/{shape}"] if "2" in forms else [])
        for template in templates:
            try:
                widened = c.normalize_path(template)
            except c.ConflictInputError:
                continue
            if widened != "":
                unsafe.append((template, widened))
    assert unsafe == []


#: Brace groups that span a separator (review round 2, a-B1 and b-F1). Each was expanded by bash 5.2.21 to a word
#: with a `..` component.
SPANNING = [
    "docs/{/.,}./x", "docs/{/.,x}./y", "docs/.{/,}./x", "docs/.{,/}./x", "docs/{.,/}./x", "docs/.{},.}/x",
    "docs/.{/,.}/x", "docs/.{,./}/x", "docs/{./.,a}./y.md", "src/hoh/{./.,a}./x.py", "docs/{./.,a}{,.}/y.md",
    "docs/{.,a}{./.,b}/y.md", "docs/{./.,a}*/y.md", "docs/{a,./.}./y.md", "docs/{a,b/..}/x",
]


@pytest.mark.parametrize("raw", SPANNING)
def test_a_brace_group_that_spans_a_separator_is_refused(raw):
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)
    assert [x.kind for x in both_ways(c.footprint("a", [raw], []), fp("b", ["x", "y.md", "src/x.py"]))] == [UN]


@pytest.mark.parametrize("raw, canonical", [
    ("docs/a{b/x", "docs"),             # a `{` that nothing closes is a literal
    ("docs/a}b/x", "docs"),             # so is a `}` that nothing opens
    ("docs/{a,b}}/x", "docs"),          # and a second `}`
    ("}docs/{a,b}/x", ""),              # the `}` is a glob character: widened to the root
    ("d/{a,b}{c,d}/x", "d"),
    ("d/x{a,b,}y/z", "d"),
])
def test_a_brace_that_cannot_form_a_group_is_a_literal_and_a_simple_group_is_accepted(raw, canonical):
    assert c.normalize_path(raw) == canonical


@pytest.mark.parametrize("raw", ["d/{}/x", "d/{a}/x", "d/{1..3}/x", "d/{a,b{c,d}}/x", "d/{a,b/c}/x", "d/x{a/b,c}",
                                 "d/{a,b}/{c}/x", "{a}/x"])
def test_a_brace_construct_that_is_not_a_simple_comma_list_is_refused(raw):
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)


@pytest.mark.parametrize("raw", [
    "docs/.[^a]/x", "docs/.[^!]/x", "docs/.[^]]/x", "docs/.[!a]/x", "docs/.[^a-c]/x", "docs/.[^[]/x",
    "docs/{.,a}[^a]/x", "docs/.[^*]/x", "docs/.[!]a]/x", "docs/.[^]a]/x",
])
def test_a_caret_negates_a_bracket_expression_as_a_shell_reads_it(raw):
    """`fnmatch` reads `^` literally; bash <= 5.1 (and 5.2 with `globskipdots` off)
    expands `docs/.[^a]/x` to `docs/../x` (review a B2)."""
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)


@pytest.mark.parametrize("raw", ["docs/.[^.]x/y", "docs/.[^.]/y", "docs/.[.]x/y", "docs/.[a^]/y", "docs/.[]]/y",
                                 "docs/.[!.]/y", "docs/[^a]*/y", "docs/[^.]x/y", "docs/.[]a]/y"])
def test_a_bracket_expression_that_cannot_hold_a_dot_second_is_not_refused(raw):
    assert c.normalize_path(raw) == "docs"


@pytest.mark.parametrize("raw", ["docs/.[z-a]/x", "docs/.[,-0]/x", "docs/.[--0]/x", "docs/.[!a-c]/x",
                                 "docs/.[a-c.]/x", "docs/.[0--]/x"])
def test_a_range_that_may_hold_a_dot_second_is_refused(raw):
    """A range from `,` to `0` holds the dot; a reversed range is read by shells in different ways, so
    it may hold anything."""
    with pytest.raises(c.ConflictInputError):
        c.normalize_path(raw)


@pytest.mark.parametrize("raw", ["docs/.[a-c]/y", "docs/.[!,-0]/y", "docs/.[+--]/y", "docs/.[a-]/y", "docs/.[-a]x/y", "docs/.[0-9a-f]/y"])
def test_a_range_that_cannot_hold_a_dot_second_is_not_refused(raw):
    assert c.normalize_path(raw) == "docs"
