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
"""

from __future__ import annotations

import re
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


def declared_criteria(spec_text: str) -> tuple[Criterion, ...]:
    """The criteria the specification declares; `()` when it declares none."""
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
            body = [] if (marker == "```" and info == "criteria") else None
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
        raise CriteriaBlockError(
            f"{SPEC_CRITERIA_UNREADABLE}: the criteria block is not closed")
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
