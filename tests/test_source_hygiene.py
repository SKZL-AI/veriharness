"""Meta-tests: properties of the source itself, not of its behaviour.

Both tests exist because a real defect got through in a way no ordinary test
could have caught, and both were **sharpened after an adversarial reviewer
built cases they missed**. What each one covers, and where it stops, is stated
in its docstring -- the first version of this file claimed each test "guards a
whole class", which was more than either could carry.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "hoh"
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "tools"))

import export_manifest as em  # noqa: E402


# --------------------------------------------------------------------------- #
# 1 - a placeholder in a message that is never formatted
# --------------------------------------------------------------------------- #

#: A placeholder, tolerating inner spaces (`{ path.parent }`) and positional
#: forms (`{0}`) -- both were shown to slip past the first version.
_PLACEHOLDER = re.compile(r"\{\s*[A-Za-z_0-9][A-Za-z0-9_.\[\]'\"() ]*\}")


def _message_constants(tree: ast.AST) -> list[tuple[int, str, str]]:
    """String constants in a **message** position.

    Restricting to messages is what makes this precise. Scanning every
    constant flags `'HEAD^{tree}'` (git revision syntax) and a docstring that
    merely mentions `{ARENA}`; scanning only f-strings misses the case where
    *neither* half carries the `f`, because then no `JoinedStr` node exists at
    all and there is nothing to look at.
    """
    formatted: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            for part in ast.walk(node.func):
                if isinstance(part, ast.Constant):
                    formatted.add(part.lineno)

    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        context = None
        if isinstance(node, ast.Raise):
            context = "raise"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                context = "print"
            elif isinstance(func, ast.Attribute) and func.attr in ("note", "write_text"):
                context = f".{func.attr}"
        elif isinstance(node, ast.JoinedStr):
            context = "f-string"
        if context is None:
            continue
        for part in ast.walk(node):
            if (
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and part.lineno not in formatted
            ):
                out.append((part.lineno, context, part.value))
    return out


def test_no_unformatted_placeholders_in_messages():
    """A `{...}` in a message that never gets formatted just stays there.

    Found on 2026-09-08 in `store.py`: the second half of a message carried no
    `f` prefix, so the operator literally received "please check
    {path.parent}" instead of a path. Python concatenates the halves silently
    and both forms look identical in the source. The site was reachable only
    after 999 name collisions inside one second, so no ordinary test covered
    it.

    Validated in both directions: it finds the original defect in `ae3ded4`,
    and it finds all three variants an adversarial reviewer built against the
    first version -- no `f` on either half, a positional `{0}`, and
    `{ path.parent }` with inner spaces. Templates used through `.format()`
    (such as `runner.FOREIGN_RUNNER`) are not flagged.

    **Where it stops:** only `raise`, `print`, `.note()`, `.write_text()` and
    f-string parts are inspected. A message assembled through a helper of its
    own would escape.
    """
    findings: list[str] = []
    for module in sorted(PACKAGE.glob("*.py")):
        for lineno, context, value in _message_constants(
            ast.parse(module.read_text(encoding="utf-8"))
        ):
            hit = _PLACEHOLDER.search(value)
            if hit:
                findings.append(f"{module.name}:{lineno} [{context}] {hit.group(0)}")

    assert not findings, (
        "placeholder in a message that is never formatted -- an f prefix is "
        f"missing there: {sorted(set(findings))}"
    )


# --------------------------------------------------------------------------- #
# 2 - an assertion still checking for German text
# --------------------------------------------------------------------------- #

#: German function words, plus the technical terms and participles this
#: repository really used. The word list grew after an adversarial reviewer
#: showed that `"Auslieferung verweigert"`, `"HAUSREGEL-VERSTOSS"` and
#: `"Reparaturbudget erschoepft"` -- three phrases the run had itself named as
#: its blind spot -- carried no function word at all and passed straight
#: through. The suffix branch catches German nouns the list does not name.
_GERMAN_WORDS = (
    r"der|die|das|den|dem|des|und|oder|nicht|kein|keine|keinen|wird|werden"
    r"|ist|sind|war|waren|eine|einen|einem|eines|mit|ohne|fuer|über|ueber"
    r"|auf|aus|von|vom|zum|zur|bei|nach|noch|schon|bereits|selbst|dass|sich"
    r"|darf|muss|soll|kann|hat|haben|belegt|gruen"
    r"|verweigert|verweigern|erschoepft|verstoss|hausregel|auslieferung"
    r"|reparaturbudget|kandidat|bindung|freigabe|gebunden|festgeschrieben"
    r"|abgewiesen|angenommen|ungeprueft|verdikte|inkrement|fortschritt"
    r"|arbeitsbaum|verzeichnis|uebergang|unlesbar|beschaedigt|schreiber"
    r"|dateiname|geloescht|ueberschrieben|abgelehnt|gescheitert|erfuellt"
)
_GERMAN = re.compile(
    rf"\b({_GERMAN_WORDS})\b|\w{{3,}}(ung|keit|heit|schaft|nis)\b", re.I
)

#: Comparisons that read a message. `startswith`/`endswith` and `is None`
#: were missing from the first version -- both are ordinary ways to check a
#: message, and `re.search(...) is None` is a negative assertion like any
#: other.
_MESSAGE_OPS = (ast.In, ast.NotIn, ast.Eq, ast.NotEq, ast.Is, ast.IsNot)

#: Deliberately German test *data*: the goalbook fixture from before the
#: translation (the object under test for the field migration) and the
#: historical reason string. These are inputs, not assertions about a message
#: the source produces.
_ALLOWED = {
    "vor der Uebersetzung geschrieben",
    "Alter Eintrag",
    "Rahmen bestaetigt",
    "Kandidatenbindung verletzt",
    # O183. Not product output: a sample of the ledger, which is German and
    # internal, fed to the pattern that reads it. The vacuity this gate exists
    # to catch is a German *expectation* about an English source; this is a
    # German *input* with an English expectation about it, and it fails the
    # moment the pattern gets looser -- which is what it is there for.
    "Priorität dieser Sache ist offen",
    "Priorität: mittel",
    "ein getracktes Todo bleibt offen",
}

#: German *input* in a decorator argument (a `parametrize` row), reviewed one by
#: one: each is data fed to the code under test, with an English expectation
#: about it, so it fails the moment the code stops reading it. Keyed by the test
#: file **and** the literal, unlike `_ALLOWED`: the same words as a comparison,
#: or in another file, are still reported (p243bf2 spec review: a literal-only
#: entry would let `assert "..." not in message()` pass elsewhere). A decorator
#: literal is read because `parametrize` is how a table of expectations is
#: written; adding a row here is the review that decides it is input.
_ALLOWED_INPUT = {
    ("test_developer_limit_screen.py", "Kontingent erschoepft | 1759600000"):
        "a pane line the provider list reads; the expectation is a failure class",
    ("test_developer_limit_screen.py", "● Kontingent erschoepft · resets 3:20am"):
        "a pane line the provider list reads; the expectation is a failure class",
    ("test_failure_classes.py", "Kontingent erschoepft"):
        "the pinned residual of the provider list; the expectation is a failure class",
    ("test_phase2.py", "Kontingent erschoepft"):
        "quota detection input; the expectation is a boolean",
    ("test_orchestrator_parity.py", "S2 rotes gate erzeugt reparatur, danach gruen"):
        "a scenario name of the German prototype oracle; it is a label, not a message",
    ("test_project_resume.py", "kurz vor dem Merge"):
        "a phase name passed to the resume logic; the expectation is a verdict",
}

#: Five characters, not seven. `"belegt"` is exactly six and sat in the word
#: list while the length filter excluded it.
_MIN_LEN = 5


def _compared_literals(node: ast.AST) -> list[tuple[int, str]]:
    return [
        (n.lineno, n.value)
        for n in ast.walk(node)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and len(n.value) >= _MIN_LEN
    ]


def _decorator_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """`(line, text)` of every string constant (of the gate's length) in the
    decorators of a function, an async function or a class of `tree`: the rows
    of a `parametrize`. The one reading of "a decorator row", shared by the
    German gate and by `_stale_input_entries`."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                out += _compared_literals(decorator)
    return out


def _german_findings(name: str, source: str, *, allowed_input=_ALLOWED_INPUT) -> list[str]:
    """The German-prose findings of one test module, `name` its file name.

    Read: comparisons, `match=`, `startswith`/`endswith`, module-level string
    assignments (not in this file, see below) and the string constants of
    **decorator arguments** (`parametrize` rows; `allowed_input` lists the
    reviewed ones). Not read: docstrings -- prose about a test, not an
    expectation, and the four on record are quotations or English homographs."""
    tree = ast.parse(source)
    candidates: list[tuple[int, str]] = []
    decorated = _decorator_literals(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, _MESSAGE_OPS) for op in node.ops
        ):
            candidates += _compared_literals(node)
        elif isinstance(node, ast.keyword) and node.arg == "match":
            candidates += _compared_literals(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("startswith", "endswith")
        ):
            candidates += _compared_literals(node)

    # Module-level constants, for the literal-behind-a-name case. This
    # file is skipped for that pass: its own word list *is* a module-level
    # German string, so scanning itself reports itself. A detector that
    # cannot survive being pointed at its own definition would be reported
    # as a finding, not shipped -- naming the exception is the honest form.
    if name != Path(__file__).name:
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                candidates += _compared_literals(node)

    findings: list[str] = []
    for reviewed, found in ((False, candidates), (True, decorated)):
        for lineno, literal in found:
            if literal in _ALLOWED or (reviewed and (name, literal) in allowed_input):
                continue
            words = sorted({m.group(0).lower() for m in _GERMAN.finditer(literal)})
            if words:
                findings.append(f"{name}:{lineno} {words} {literal[:56]!r}")
    return findings


def _stale_input_entries(root: Path, allowed_input=_ALLOWED_INPUT) -> list[str]:
    """The `allowed_input` entries that exempt nothing: the file `name` is not in
    `root`, or none of its decorators holds `literal` -- exactly, as the gate
    matches it -- as a string constant (`_decorator_literals`). An entry binds a
    file **and** a literal; one that matches no decorator row is a standing
    exemption for a row somebody may write later."""
    stale = []
    for name, literal in sorted(allowed_input):
        path = root / name
        rows: set[str] = set()
        if path.is_file():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rows = {text for _, text in _decorator_literals(tree)}
        if literal not in rows:
            stale.append(f"{name} {literal[:40]!r}")
    return stale


def test_no_assertion_checks_for_german_text():
    """A negative assertion on German prose passes vacuously once the source
    is English.

    Two of them were found on 2026-09-08, both in the findings catalogue:
    `"belegt das Inkrement nicht" not in ...` and `"keine Verdikte" not in
    ...`. Once `controller.py` spoke English the German substring was
    trivially absent, so both checks passed while testing nothing, and two
    recorded findings were silently dead. Pulling assertions through by
    running the suite cannot find this class -- a vacuous assertion does not
    fail.

    Validated in four directions: it finds both dead assertions in the
    pre-fix revision, every German assertion in the pre-translation revision
    `ae3ded4`, five of the six escape forms an adversarial reviewer built
    against the first version, and it reports nothing on the translated tree.

    **Where it stops:** a literal held in a module constant and compared
    through that name would need dataflow analysis to follow. Module-level
    string assignments in the test files are therefore scanned as well, which
    covers the ordinary shape of that case but not one built at runtime. The
    same holds for a decorator argument that is compared through its parameter
    name: the literal is read where it is written, in the decorator, and the
    reviewed German input is listed in `_ALLOWED_INPUT`. Docstrings are not
    read (`_german_findings`)."""
    findings: list[str] = []
    for module in sorted(TESTS.glob("*.py")):
        findings += _german_findings(module.name, module.read_text(encoding="utf-8"))

    assert not findings, (
        "an assertion is comparing against German prose -- after the "
        f"translation the source no longer produces it: {sorted(set(findings))}"
    )


#: Source text for the next test, held as module-level data (this file's own
#: module-level strings are not scanned, and none of it is a comparison).
_SYNTH_DECORATED = (
    "import pytest\n"
    "@pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft'])\n"
    "def test_x(expected):\n"
    "    assert expected in output()\n"
)
_SYNTH_DOCSTRING = (
    "def test_x():\n"
    '    """Reparaturbudget erschoepft, as the old catalogue said."""\n'
    "    assert output()\n"
)
_SYNTH_COMPARED = (
    "def test_x():\n"
    "    assert 'Reparaturbudget erschoepft' not in output()\n"
)
_SYNTH_WORD = "erschoepft"
_SYNTH_LITERAL = "Reparaturbudget erschoepft"


def test_the_german_gate_reads_decorator_literals_and_scopes_its_allowlist():
    """A German expectation written as a `parametrize` row passed the gate
    (p243bf2 spec review, tracked): the row is a decorator argument, not a
    comparison. Now a decorator's string constants are read; the reviewed
    German *input* is allowed per (file, literal), not per literal.

    The cases: the row is reported; it is not once its (file, literal) is
    reviewed; the same literal in another file is reported again (a
    literal-only entry would be global); the same literal in a *comparison*
    of the reviewed file is still reported (the review covers the decorator
    row, not a vacuous `assert ... not in message()` beside it); an English
    row is not reported (control); a German docstring is not reported (the
    gate does not read prose about a test)."""
    found = _german_findings("test_probe.py", _SYNTH_DECORATED, allowed_input={})
    assert len(found) == 1 and _SYNTH_WORD in found[0], found
    reviewed = {("test_probe.py", _SYNTH_LITERAL): "input"}
    assert _german_findings("test_probe.py", _SYNTH_DECORATED, allowed_input=reviewed) == []
    assert len(_german_findings("test_other.py", _SYNTH_DECORATED, allowed_input=reviewed)) == 1
    assert len(_german_findings("test_probe.py", _SYNTH_COMPARED, allowed_input=reviewed)) == 1
    english = _SYNTH_DECORATED.replace(_SYNTH_LITERAL, "budget exhausted for this task")
    assert _german_findings("test_probe.py", english, allowed_input={}) == []
    assert _german_findings("test_probe.py", _SYNTH_DOCSTRING, allowed_input={}) == []


#: Data for the binding tests below (module-level: this file's own strings are
#: not scanned; a German word compared inside a test body **is** read, so the
#: words are named here).
_SYNTH_TWO_ROWS = (
    "import pytest\n"
    "@pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft', 'Auslieferung verweigert'])\n"
    "def test_x(expected):\n"
    "    assert expected in output()\n"
)
_SYNTH_CLASS = (
    "import pytest\n"
    "@pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft'])\n"
    "class TestX:\n"
    "    def test_x(self, expected):\n"
    "        assert expected in output()\n"
)
_SYNTH_ASYNC = (
    "import pytest\n"
    "@pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft'])\n"
    "async def test_x(expected):\n"
    "    assert expected in output()\n"
)
_SYNTH_CONSTANT = (
    "import pytest\n"
    "ROWS = ['Reparaturbudget erschoepft']\n"
    "@pytest.mark.parametrize('expected', ROWS)\n"
    "def test_x(expected):\n"
    "    assert expected in output()\n"
)
_SYNTH_OTHER_LITERAL = "Auslieferung verweigert"
_SYNTH_OTHER_WORD = "verweigert"
_SYNTH_PREFIX = "Reparaturbudget"
_SYNTH_TAIL = " heute"
#: Longer than 40 characters, so that a comparison of only the first 40 (the
#: length the stale check prints) cannot tell it from a row changed after that.
_SYNTH_LONG = "Reparaturbudget erschoepft und die Auslieferung verweigert"
_SYNTH_METHOD = (
    "import pytest\n"
    "class TestX:\n"
    "    @pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft'])\n"
    "    def test_x(self, expected):\n"
    "        assert expected in output()\n"
)
#: Stacked decorators, the row on the second and on the first.
_SYNTH_SKIP_OVER = (
    "import pytest\n"
    "@pytest.mark.skipif(False, reason='not here')\n"
    "@pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft'])\n"
    "def test_x(expected):\n"
    "    assert expected in output()\n"
)
_SYNTH_SKIP_UNDER = (
    "import pytest\n"
    "@pytest.mark.parametrize('expected', ['Reparaturbudget erschoepft'])\n"
    "@pytest.mark.skipif(False, reason='not here')\n"
    "def test_x(expected):\n"
    "    assert expected in output()\n"
)


def _synth_with_row(row: str) -> str:
    return _SYNTH_DECORATED.replace(_SYNTH_LITERAL, row)


def test_an_allowlist_entry_binds_its_file_and_its_literal_both():
    """hyg2 (tracked since the hyg merge): the first version of the gate pinned
    the scoping in the literal dimension only -- a clause that exempted *every*
    decorator literal of a reviewed file stayed green. An entry is an **exact**
    (file, literal) pair, and each half is checked here against the other being
    right, in both directions of "contains":

    * the reviewed literal in its own file, also on a class or an async
      function: exempt (controls);
    * a second German row in the same file: reported, and the finding names that
      row;
    * rows that are a substring, a prefix, a suffix, a case-changed form, a
      longer form (either side) or a whitespace-changed form (a trailing or
      leading space, a doubled inner space; and the space on the entry instead
      of the row) of the reviewed literal: reported;
    * the reviewed row in a method of a class, and under or over another
      decorator (`skipif`): read like a row on a plain function;
    * the reviewed literal in another file: reported -- also when that file's
      name contains the reviewed name, is contained in it, starts with it or
      differs in case;
    * two entries in two files: each exempts its own literal in its own file
      only (a flat set of files and a flat set of literals would let both
      through)."""
    mine, theirs = "test_probe.py", "test_other.py"
    reviewed = {(mine, _SYNTH_LITERAL): "input"}
    for source in (_SYNTH_DECORATED, _SYNTH_CLASS, _SYNTH_ASYNC, _SYNTH_METHOD,
                   _SYNTH_SKIP_OVER, _SYNTH_SKIP_UNDER):
        assert _german_findings(mine, source, allowed_input=reviewed) == [], source
        assert len(_german_findings(theirs, source, allowed_input=reviewed)) == 1, source
    two = _german_findings(mine, _SYNTH_TWO_ROWS, allowed_input=reviewed)
    assert len(two) == 1 and _SYNTH_OTHER_WORD in two[0] and _SYNTH_WORD not in two[0], two

    rows = (_SYNTH_WORD, _SYNTH_PREFIX, _SYNTH_LITERAL.upper(), _SYNTH_LITERAL.lower(),
            _SYNTH_LITERAL + _SYNTH_TAIL, _SYNTH_TAIL.strip() + " " + _SYNTH_LITERAL,
            _SYNTH_LITERAL + " ", " " + _SYNTH_LITERAL, _SYNTH_LITERAL.replace(" ", "  "))
    for row in rows:
        found = _german_findings(mine, _synth_with_row(row), allowed_input=reviewed)
        assert len(found) == 1, (row, found)
    spaced = {(mine, _SYNTH_LITERAL + " "): "input"}        # the entry carries the space
    assert _german_findings(mine, _synth_with_row(_SYNTH_LITERAL + " "), allowed_input=spaced) == []
    assert len(_german_findings(mine, _SYNTH_DECORATED, allowed_input=spaced)) == 1
    for other in ("my_" + mine, mine[len("test_"):], mine + ".bak", mine.upper()):
        found = _german_findings(other, _SYNTH_DECORATED, allowed_input=reviewed)
        assert len(found) == 1, (other, found)

    both = {(mine, _SYNTH_LITERAL): "input", (theirs, _SYNTH_OTHER_LITERAL): "input"}
    here = _german_findings(mine, _SYNTH_TWO_ROWS, allowed_input=both)
    there = _german_findings(theirs, _SYNTH_TWO_ROWS, allowed_input=both)
    assert len(here) == 1 and _SYNTH_OTHER_WORD in here[0], here
    assert len(there) == 1 and _SYNTH_WORD in there[0], there


def test_a_reviewed_input_entry_must_name_a_live_file_and_decorator_row(tmp_path):
    """The other half of "binds both": an entry whose file is gone, or whose
    literal is no longer -- exactly -- a decorator row of that file, exempts
    nothing and is reported. A row that was reworded (longer or shorter), moved
    into a comparison or into a module constant is stale; a row on a class or an
    async function is live. The mechanism on a made-up tree, then the real table
    against the real `tests/` (five other test files are read: a strand that
    rewords one of their reviewed rows must change its entry in the same step)."""
    made_up = {
        "test_live.py": _SYNTH_DECORATED,
        "test_class.py": _SYNTH_CLASS,
        "test_async.py": _SYNTH_ASYNC,
        "test_compared.py": _SYNTH_COMPARED,
        "test_constant.py": _SYNTH_CONSTANT,
        "test_longer.py": _synth_with_row(_SYNTH_LITERAL + _SYNTH_TAIL),
        "test_shorter.py": _synth_with_row(_SYNTH_WORD),
        "test_method.py": _SYNTH_METHOD,
        "test_over.py": _SYNTH_SKIP_OVER,
        "test_under.py": _SYNTH_SKIP_UNDER,
    }
    for file_name, text in made_up.items():
        (tmp_path / file_name).write_text(text, encoding="utf-8")
    entries = {(file_name, _SYNTH_LITERAL): "entry" for file_name in made_up}
    entries[("test_gone.py", _SYNTH_LITERAL)] = "file missing"
    entries[("test_live.py", _SYNTH_OTHER_LITERAL)] = "literal absent"
    # Exact to the character: whitespace and case count, and so does a change
    # after the 40th character (the length the report prints, not the one matched).
    assert len(_SYNTH_LONG) > 45
    near_misses = {
        "test_space.py": (_synth_with_row(_SYNTH_LITERAL + " "), _SYNTH_LITERAL),
        "test_nospace.py": (_SYNTH_DECORATED, _SYNTH_LITERAL + " "),
        "test_case.py": (_SYNTH_DECORATED, _SYNTH_LITERAL.upper()),
        "test_longrow.py": (_synth_with_row(_SYNTH_LONG[:-1] + "x"), _SYNTH_LONG),
        "test_long.py": (_synth_with_row(_SYNTH_LONG), _SYNTH_LONG),        # control: live
    }
    for file_name, (text, literal) in near_misses.items():
        (tmp_path / file_name).write_text(text, encoding="utf-8")
        entries[(file_name, literal)] = "entry"
    stale = _stale_input_entries(tmp_path, entries)
    assert [s.split()[0] for s in stale] == [
        "test_case.py", "test_compared.py", "test_constant.py", "test_gone.py", "test_live.py",
        "test_longer.py", "test_longrow.py", "test_nospace.py", "test_shorter.py",
        "test_space.py"], stale
    # The report names the file and the first 40 characters of the literal, quoted;
    # two entries of one file are told apart, in sorted order.
    two = _stale_input_entries(tmp_path, {("test_gone.py", _SYNTH_LITERAL): "a",
                                          ("test_gone.py", _SYNTH_LONG): "b"})
    assert two == ["test_gone.py " + repr(_SYNTH_LITERAL), "test_gone.py " + repr(_SYNTH_LONG[:40])], two
    assert _stale_input_entries(tmp_path, {("test_live.py", _SYNTH_LITERAL): "live"}) == []

    assert _ALLOWED_INPUT, "no reviewed entries -- the check would pass vacuously"
    assert _stale_input_entries(TESTS) == []


# --------------------------------------------------------------------------- #
# 3 - a private session id in the published code
# --------------------------------------------------------------------------- #

#: SHA-256 of the ids that must not appear in the code this repository
#: publishes: the names of the orchestrator sessions' private scratch
#: directories. The ids are held as digests so that this file does not itself
#: write the id it forbids (`tests/test_storage_registry_adopt.py` carried one
#: as its session name). That is obfuscation only: a digest of eight hex
#: characters can be brute-forced in seconds, so it hides nothing from anyone
#: who tries. To add one:
#: `python3 -c "import hashlib; print(hashlib.sha256(b'<id>').hexdigest())"`.
_PRIVATE_ID_DIGESTS = em.PRIVATE_ID_DIGESTS

#: An id is a whole token of eight hex characters: `scratch/<id>/x`, `<id>-4e..`
#: and a bare `"<id>"` all match; a longer hex run or a word that merely
#: contains eight hex letters does not.
_ID_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}(?![0-9A-Za-z])")

#: The folders whose `*.py` files are published code, relative to the
#: repository root.
_PUBLISHED_FOLDERS = ("src/hoh", "tests", "tools")


def _private_id_findings(name: str, text: str, digests=_PRIVATE_ID_DIGESTS) -> list[str]:
    """`name:line` for each line of `text` that holds a token whose digest is in
    `digests`. Raw text, not the syntax tree: a decorator argument, a docstring,
    a comment and an f-string all count, because all of them are published. The
    token itself is never put in a finding."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _ID_TOKEN.finditer(line):
            if hashlib.sha256(match.group(0).lower().encode()).hexdigest() in digests:
                out.append(f"{name}:{lineno}")
    return out


def _published_code_findings(root: Path, folders=_PUBLISHED_FOLDERS,
                             digests=_PRIVATE_ID_DIGESTS) -> list[str]:
    """The id findings of every `*.py` under `folders` of `root` (not
    `__pycache__`), as `relative/path:line`."""
    findings: list[str] = []
    for folder in folders:
        for path in sorted((root / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            findings += _private_id_findings(
                path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"), digests)
    return findings


def test_no_private_session_id_in_the_published_code():
    """A private orchestrator session id slipped into a published test as a
    fixture value (`SESSION = "<id>"`, found in a review of the storage tests):
    nothing looked for ids, because the home-path and token scans of the export
    manifest know paths and 24-character secrets, not an eight-character name.

    Every `*.py` under `src/hoh`, `tests` and `tools` is read as text. The
    non-vacuity assertion keeps an emptied digest set from passing.

    **Where it stops:** only the listed digests are refused, so a new
    orchestrator session needs its digest added by hand; only `*.py` files are
    read (not Markdown, JSON or shell scripts, and not other folders); an id
    must be a whole token of eight hex characters, so one glued to letters or
    digits (`x<id>`) or written in another length is not found; and a digest of
    eight hex characters is obfuscation, not secrecy."""
    assert _PRIVATE_ID_DIGESTS, "no ids to refuse -- the detector would pass vacuously"
    findings = _published_code_findings(TESTS.parent)
    assert not findings, (
        "a private session id is written in the published code (use a neutral "
        f"name such as 's-test-01'): {sorted(findings)}"
    )


def test_a_private_id_is_found_wherever_it_is_written(tmp_path):
    """The mechanism, with an id of its own (made up here, digest computed at
    run time): found in a decorator argument, a docstring, a comment, an
    f-string, a path and the first group of a UUID; not found in a longer
    token, as another id, or with one character fewer. The finding names the
    line and never the token. The tree scan reads every published folder (one
    file in each of `src/hoh`, `tests`, `tools` is found), skips
    `__pycache__`, and does not read other folders or other file types."""
    made_up = "c0ffee11"
    digests = frozenset({hashlib.sha256(made_up.encode()).hexdigest()})
    written = {
        "decorator": f"@pytest.mark.parametrize('s', ['{made_up}'])\n",
        "docstring": f'def f():\n    """session {made_up} ran it"""\n',
        "comment": f"x = 1  # from {made_up}\n",
        "f-string": f"y = f'projects/p/scratch/{made_up}/{{name}}'\n",
        "uuid": f"z = '{made_up}-3e42-4b92-9dd3-e4977b6e535c'\n",
        "upper": f"w = '{made_up.upper()}'\n",
    }
    for label, text in written.items():
        found = _private_id_findings("probe.py", text, digests)
        assert found and made_up not in "".join(found), (label, found)
    not_it = [f"a = '{made_up}2'\n", f"b = 'x{made_up}'\n", f"c = '{made_up[:-1]}'\n",
              "d = 'c0ffee12'\n"]
    for text in not_it:
        assert _private_id_findings("probe.py", text, digests) == [], text

    published = ("src/hoh", "tests", "tools")          # written out: the constant is under test
    carrier = f"x = '{made_up}'\n"
    for folder in (*published, "docs", "src/hoh/__pycache__"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / folder / "mod.py").write_text(carrier, encoding="utf-8")
    (tmp_path / "tests" / "notes.md").write_text(carrier, encoding="utf-8")
    found = _published_code_findings(tmp_path, digests=digests)
    assert sorted(found) == sorted(f"{folder}/mod.py:1" for folder in published), found


#: `CacheStatus` is declared for a KV-cache decision this project does not
#: make. It is unused, it says so in its own docstring, and nothing is deleted
#: here -- so it is allowed to stay. What it is not allowed to do is set a
#: precedent.
_KNOWN_UNUSED_CONTRACTS = {"CacheStatus"}


def test_no_new_dead_contract_schema():
    """O13: a declared type nobody reads is a claim nobody checks.

    Counted over the AST rather than with `grep`, because a name can be used
    inside its own module -- `Usage` is, four times, and a file-level text
    search called it dead. The definition itself does not count as a use.
    """
    import ast
    import collections
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    contracts = root / "src" / "hoh" / "contracts.py"
    declared = [
        node.name
        for node in ast.parse(contracts.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert declared, "no contracts found -- the detector would pass vacuously"

    own = Path(__file__).resolve()
    files = [
        path
        for folder in ("src/hoh", "tests")
        for path in (root / folder).rglob("*.py")
        if "__pycache__" not in str(path) and path.resolve() != own
    ]
    # This file is excluded on purpose. `_KNOWN_UNUSED_CONTRACTS` holds the
    # name as a *string*, and the string-annotation branch below would count
    # it as a use -- the detector would then declare its own allowlist stale.
    # The German-word detector in this module skips itself for the same
    # reason; a checker that counts its own test data is measuring itself.
    uses: collections.Counter = collections.Counter()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name) and node.id in declared:
                uses[node.id] += 1
            elif isinstance(node, ast.Attribute) and node.attr in declared:
                uses[node.attr] += 1
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in declared
            ):
                uses[node.value] += 1        # string annotations

    dead = {name for name in declared if uses[name] == 0}
    unexpected = dead - _KNOWN_UNUSED_CONTRACTS
    assert not unexpected, (
        f"contract schemas nobody reads: {sorted(unexpected)}. Either wire them "
        f"up or write down in the docstring why they are dead, and add them to "
        f"_KNOWN_UNUSED_CONTRACTS with that reason."
    )
    # The other direction: an allowlist entry that has come alive is stale
    # bookkeeping, and stale bookkeeping is how the next reader is misled.
    revived = {name for name in _KNOWN_UNUSED_CONTRACTS if uses.get(name, 0) > 0}
    assert not revived, (
        f"these are on the unused allowlist but are used now: {sorted(revived)} "
        f"-- remove them from _KNOWN_UNUSED_CONTRACTS."
    )


def test_the_hygiene_gate_reads_the_exports_denylist():
    """leakid: one list of private-id digests, in `tools/export_manifest.py`;
    the published-code gate above and the export scan both read it, so a digest
    added once is refused in both places."""
    assert _PRIVATE_ID_DIGESTS is em.PRIVATE_ID_DIGESTS


# --------------------------------------------------------------------------- #
# 4 - the development line stays clean under `ruff check --select F,E9` (lint1)
# --------------------------------------------------------------------------- #


def test_the_wave_gate_of_the_parallelism_mode_is_named_for_the_type_checker_only():
    """`ModeGate.gate` is annotated `"WaveGate | None"` (a string, under `from __future__ import annotations`)
    and ruff (F821) wants the name bound. The module does not import the gate it is handed (it is a
    parameter of `admit_wave`), so the name is bound under `if TYPE_CHECKING:` and `import hoh.parallelmode`
    must not load `hoh.backpressure`. Catches: a runtime import of the gate, a seam the module's contract
    excludes and one edit from an import cycle. Mutant: the import moved out of the `if`."""
    tree = ast.parse((PACKAGE / "parallelmode.py").read_text(encoding="utf-8"))
    bound = [(parent, node) for parent in ast.walk(tree) for node in ast.iter_child_nodes(parent)
             if isinstance(node, ast.ImportFrom) and any(alias.name == "WaveGate" for alias in node.names)]
    assert len(bound) == 1, "WaveGate must be bound exactly once in src/hoh/parallelmode.py"
    parent, node = bound[0]
    assert (node.module, node.level) == ("backpressure", 1)
    assert isinstance(parent, ast.If) and isinstance(parent.test, ast.Name) and parent.test.id == "TYPE_CHECKING"
    probe = subprocess.run(
        [sys.executable, "-B", "-c", "import sys, hoh.parallelmode; print('hoh.backpressure' in sys.modules)"],
        env={**os.environ, "PYTHONPATH": str(PACKAGE.parent)}, capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", "importing hoh.parallelmode loaded hoh.backpressure"


def test_a_fixture_taken_by_name_from_another_test_module_is_that_very_fixture():
    """Three test modules use a fixture that another test module defines, and say so with an alias
    (`world = _rebind_world`) instead of a shadowed import (ruff F811 reads every test parameter named like
    an imported name as a redefinition). The alias must stay the object it names: a stand-in under the
    same name would silently replace the shared fixture. Mutant: an alias to another object."""
    for holder, name, owner in (("test_merge_gate_floor", "world", "test_merge_gate_rebind"),
                                ("test_route_receipts", "spec", "test_controller"),
                                ("test_surface_book", "fake", "test_storage_registry")):
        taken = getattr(importlib.import_module(holder), name)
        defined = getattr(importlib.import_module(owner), name)
        assert taken is defined, f"{holder}.{name} is not {owner}.{name}"
