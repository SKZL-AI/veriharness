"""Meta-tests: properties of the source itself, not of its behaviour.

Both tests exist because a real defect got through in a way no ordinary test
could have caught, and both were **sharpened after an adversarial reviewer
built cases they missed**. What each one covers, and where it stops, is stated
in its docstring -- the first version of this file claimed each test "guards a
whole class", which was more than either could carry.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "hoh"
TESTS = Path(__file__).resolve().parent


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
    covers the ordinary shape of that case but not one built at runtime.
    """
    findings: list[str] = []
    for module in sorted(TESTS.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        candidates: list[tuple[int, str]] = []

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
        if module.name != Path(__file__).name:
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    candidates += _compared_literals(node)

        for lineno, literal in candidates:
            if literal in _ALLOWED:
                continue
            words = sorted({m.group(0).lower() for m in _GERMAN.finditer(literal)})
            if words:
                findings.append(f"{module.name}:{lineno} {words} {literal[:56]!r}")

    assert not findings, (
        "an assertion is comparing against German prose -- after the "
        f"translation the source no longer produces it: {sorted(set(findings))}"
    )


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
