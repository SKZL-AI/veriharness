"""Permission rules that Claude Code would match differently from how they read.

A role started under the default policy printed, from Claude Code 2.1.289:
`Bash(find * -delete:*) mixes * with the trailing :* prefix syntax, so it is
matched as a literal prefix (the * is not expanded) and will likely never
match.` A deny rule that matches nothing is a deny rule that does not exist,
and nothing in the suite noticed, because every check compared rule *strings*.

These cases scan every rule HoH writes for a role, pin the corrected `find`
rules, and check them against a small port of Claude Code's own matcher. No
agent is started and no command is run: commands appear here only as strings
handed to a matcher, and every target is a made-up relative name.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from hoh.approval_policy import MANDATORY_DENY, PolicyRefused, load, parse, rule_defect
from hoh.contracts import Role
from hoh.dispatchers import _Base

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "policy" / "role_approval.default.json"
OLD_FIND = "Bash(find * -delete:*)"
NEW_FIND = ("Bash(find * -delete*)", "Bash(find -delete*)")
#: The neighbours the two `find` rules sit between, where the old rule stood.
FIND_BEFORE, FIND_AFTER = "Bash(mkfs:*)", "Bash(truncate:*)"


def _doc() -> dict:
    return json.loads(DEFAULT.read_text(encoding="utf-8"))


def _content(rule: str) -> str | None:
    m = re.fullmatch(r"\w+\((.*)\)", rule, flags=re.S)
    return m.group(1) if m else None


def _escaped(text: str, i: int) -> bool:
    """Whether the character at `i` stands after an odd number of backslashes."""
    head = text[:i]
    return (len(head) - len(head.rstrip("\\"))) % 2 == 1


def _has_unescaped_star(text: str) -> bool:
    return any(ch == "*" and not _escaped(text, i) for i, ch in enumerate(text))


def _mixes_star_with_trailing_prefix(rule: str) -> bool:
    """Independent of the module: content ends in `:*` and an unescaped `*`
    stands before it."""
    content = _content(rule)
    if content is None or not content.endswith(":*"):
        return False
    return _has_unescaped_star(content[:-2])


def _colon_star_not_at_end(rule: str) -> bool:
    content = _content(rule)
    return content is not None and ":*" in content and not content.endswith(":*")


def _every_written_rule() -> list[tuple[str, str, str]]:
    """(role, behavior, rule) for every rule in every role's settings file, as
    the dispatcher writes them, answers-directory rules included. The files are
    written into a temporary directory that is gone when this returns."""
    found = []
    with tempfile.TemporaryDirectory() as scratch:
        run = Path(scratch) / "run"
        d = _Base(answers_dir=run / "answers", profiles={r: "claude" for r in Role})
        d.approval_policy = load(DEFAULT)
        for role in Role:
            d._agent_args(role)
            settings = run / "approval" / f"{role.value}.settings.json"
            perms = json.loads(settings.read_text(encoding="utf-8"))["permissions"]
            for behavior in ("deny", "allow"):
                found += [(role.value, behavior, r) for r in perms[behavior]]
    assert {r for r, _, _ in found} == {r.value for r in Role}
    return found


def _written_deny_lists() -> dict[str, list[str]]:
    lists: dict[str, list[str]] = {}
    for role, behavior, rule in _every_written_rule():
        if behavior == "deny":
            lists.setdefault(role, []).append(rule)
    return lists


def _assert_find_rules_in_place(rules, where: str) -> None:
    """Both rules, in this order, exactly where the old one stood."""
    rules = list(rules)
    i = rules.index(FIND_BEFORE)
    assert rules[i + 1:i + 4] == [*NEW_FIND, FIND_AFTER], where
    assert sum(r in NEW_FIND for r in rules) == 2, where


def test_no_generated_rule_mixes_star_with_trailing_prefix():
    """Catches the old `find` rule (or any rule of its form) in the mandatory
    list, in the shipped policy, or in any role's written settings."""
    written = _every_written_rule()
    doc = _doc()
    sources = ([("MANDATORY_DENY", "deny", r) for r in MANDATORY_DENY]
               + [("default policy", "deny", r) for r in doc["deny"]]
               + [(f"default policy {k}", "allow", r)
                  for k, v in doc["roles"].items() for r in v.get("allow") or ()]
               + written)
    bad = [s for s in sources
           if _mixes_star_with_trailing_prefix(s[2]) or _colon_star_not_at_end(s[2])]
    assert bad == []
    allowed = [r for _, b, r in written if b == "allow"]
    assert allowed, "the answers rules must be scanned"
    assert any(r.startswith("Edit(//") for r in allowed)
    assert any(r.startswith("Read(//") for r in allowed)

    # No denied rule reaches a `find` that does not delete: the fix must not
    # widen into denying `find` as such (an extra `Bash(find:*)`, say).
    denied = [r for _, b, r in sources if b == "deny" and r.startswith("Bash(")]
    for command in NOT_DELETING_FIND:
        assert not any(_cc_matches(r, command) for r in denied), command

    # Order and position: the two rules stand where the old one stood.
    _assert_find_rules_in_place(MANDATORY_DENY, "MANDATORY_DENY")
    _assert_find_rules_in_place(doc["deny"], "default policy")
    for role, rules in _written_deny_lists().items():
        _assert_find_rules_in_place(rules, role)

    # Negative control: the scan fires on the forms it exists to catch.
    assert _mixes_star_with_trailing_prefix(OLD_FIND)
    assert _mixes_star_with_trailing_prefix("Bash(pytest * -x :*)")
    assert not _mixes_star_with_trailing_prefix("Bash(echo \\*:*)")
    assert _colon_star_not_at_end("Bash(git:* status)")


def test_every_written_rule_is_one_claude_code_matches_as_written():
    """Catches any rule form `rule_defect` knows, in any role's settings."""
    defects = [(role, b, r, rule_defect(r, b)) for role, b, r in _every_written_rule()
               if rule_defect(r, b)]
    assert defects == []


def test_find_delete_is_spelled_as_wildcard_rules():
    """Pins the corrected rules where they are declared, shipped and written."""
    shipped = _doc()["deny"]
    for rule in NEW_FIND:
        assert rule in MANDATORY_DENY
        assert rule in shipped
    assert OLD_FIND not in MANDATORY_DENY and OLD_FIND not in shipped
    written = _written_deny_lists()
    assert set(written) == {r.value for r in Role}
    for role, rules in written.items():
        assert set(NEW_FIND) <= set(rules), role
        assert OLD_FIND not in rules, role


# --- a port of Claude Code 2.1.289's Bash rule matcher ----------------------
#
# `gtt` decides the rule kind: content ending `:*` (with at least one character
# before it) is a literal prefix; otherwise an unescaped `*` makes it a
# wildcard; otherwise it is exact. A prefix matches a command equal to it or
# followed by a space; a wildcard is an anchored regex in which `*` is `.*`, an
# escaped `\*` stays a literal `*`, and a single trailing ` *` is optional.
# Runs of blanks are collapsed first.

_ESC_STAR, _ESC_BACKSLASH = "\x00ESCAPED_STAR\x00", "\x00ESCAPED_BACKSLASH\x00"


def _wildcard_regex(pattern: str) -> str:
    out, i = "", 0
    while i < len(pattern):
        if pattern[i] == "\\" and i + 1 < len(pattern) and pattern[i + 1] in "*\\":
            out += _ESC_STAR if pattern[i + 1] == "*" else _ESC_BACKSLASH
            i += 2
            continue
        out += pattern[i]
        i += 1
    stars = out.count("*")
    # Claude Code escapes exactly these characters (not blanks) before `*` -> `.*`.
    body = re.sub(r"""([.+?^${}()|\[\]\\'"])""", r"\\\1", out).replace("*", ".*")
    body = body.replace(_ESC_STAR, r"\*").replace(_ESC_BACKSLASH, r"\\")
    if body.endswith(" .*") and stars == 1:
        body = body[:-3] + "( .*)?"
    return body


def _cc_matches(rule: str, command: str) -> bool:
    content = _content(rule)
    command = re.sub(r"[ \t]+", " ", command)
    m = re.fullmatch(r"(.+):\*", content, flags=re.S)
    if m:
        prefix = m.group(1)
        return command == prefix or command.startswith(prefix + " ")
    if not _has_unescaped_star(content):
        return command == content
    pattern = re.sub(r"[ \t]+", " ", content.strip())
    return re.fullmatch(_wildcard_regex(pattern), command, flags=re.S) is not None


# Data only: these strings are handed to a matcher and never executed. Every
# target is a made-up relative name; no command here names a real or
# fundamental path.
DELETING = ["find scratch-x -delete", "find scratch-x -name y -delete",
            "find scratch-x/sub -type f -delete", "find -delete",
            "find  scratch-x   -delete", "find scratch-x -delete -print"]
NOT_DELETING = ["find scratch-x -name y -print", "find scratch-x -name '*-delete*'",
                "findx scratch-x -delete"]
NOT_DELETING_FIND = ["find scratch-x -name y -print", "find scratch-x -type f",
                     "find scratch-x"]


def test_find_delete_rules_deny_what_they_mean():
    """Catches the old literal-prefix rule (it matches none of these), and a
    fix that drops the path-less form `find -delete`."""
    bash_deny = [r for r in MANDATORY_DENY if r.startswith("Bash(")]
    for command in DELETING:
        assert any(_cc_matches(r, command) for r in bash_deny), command
    for command in NOT_DELETING:
        assert not any(_cc_matches(r, command) for r in NEW_FIND), command
    # The second rule is needed: the first cannot match the path-less form.
    assert not _cc_matches(NEW_FIND[0], "find -delete")
    assert _cc_matches(NEW_FIND[1], "find -delete")
    # Negative control: the old rule matched none of the deleting commands,
    # only the literal text it spelled.
    assert not any(_cc_matches(OLD_FIND, c) for c in DELETING)
    assert _cc_matches(OLD_FIND, "find * -delete")
    # The port agrees with the prefix rules the policy relies on.
    assert _cc_matches("Bash(rm:*)", "rm scratch-x")
    assert not _cc_matches("Bash(rm:*)", "rmdir scratch-x")
    # Only an unescaped `*` is a wildcard; `\*` is a literal star.
    assert _cc_matches("Bash(echo *)", "echo scratch-x")
    assert not _cc_matches("Bash(echo \\*)", "echo scratch-x")
    assert _cc_matches("Bash(echo \\* *)", "echo * scratch-x")
    assert not _cc_matches("Bash(echo \\* *)", "echo scratch-x scratch-y")


# --- the loader refuses a rule Claude Code would misread --------------------

@pytest.mark.parametrize("rule", [OLD_FIND, "Bash(pytest * -x:*)", "Bash(git:* status)",
                                  "Bash(:*)", "Read(src:*)"])
def test_a_misread_deny_rule_is_refused(rule):
    doc = _doc()
    doc["deny"].append(rule)
    with pytest.raises(PolicyRefused, match=re.escape(rule)):
        parse(doc)


@pytest.mark.parametrize("rule", ["Bash(pytest * -x:*)", "Bash(git * push)",
                                  "Write(src/**)", "Glob(src/**)", "Edit(src:*)"])
def test_a_misread_allow_rule_is_refused(rule):
    doc = _doc()
    doc["roles"]["developer"]["allow"] = [rule]
    with pytest.raises(PolicyRefused, match=re.escape(rule)) as refused:
        parse(doc)
    assert "developer" in str(refused.value)


@pytest.mark.parametrize("behavior,rule", [
    ("deny", "Bash(echo \\*:*)"), ("deny", "Bash(npm run test:*)"),
    ("deny", "Bash(find * -delete*)"), ("allow", "Bash(pytest:*)"),
    ("allow", "Bash(git status *)"), ("allow", "Edit(src/**)"),
    ("allow", "Read(//abs/answers/**)"),
])
def test_a_rule_that_matches_as_written_is_accepted(behavior, rule):
    """Negative control for the two refusals above: the check is not a blanket
    ban on `*` or `:*`."""
    doc = _doc()
    if behavior == "deny":
        doc["deny"].append(rule)
    else:
        doc["roles"]["developer"]["allow"] = [rule]
    policy = parse(doc)
    assert rule in (policy.deny if behavior == "deny" else policy.allow["developer"])


@pytest.mark.parametrize("behavior,entry", [
    ("deny", 7), ("deny", None), ("deny", ["Bash(rm:*)"]),
    ("allow", 7), ("allow", {"Bash": "pytest"}),
])
def test_a_non_string_entry_is_refused(behavior, entry):
    doc = _doc()
    if behavior == "deny":
        doc["deny"].append(entry)
    else:
        doc["roles"]["developer"]["allow"] = [entry]
    with pytest.raises(PolicyRefused, match="not a permission rule string"):
        parse(doc)


def test_rule_defect_reads_like_claude_codes_validator():
    """The reasons themselves, rule by rule, beside the loader tests above."""
    assert rule_defect("WebFetch", "deny") is None
    assert rule_defect(OLD_FIND, "deny")
    assert rule_defect("Bash(echo \\\\*:*)", "deny")  # `\\*`: the star is unescaped
    assert rule_defect("Bash(echo \\*:*)", "deny") is None
    assert rule_defect("Bash(git * push)", "deny") is None  # allow-only check
    assert rule_defect("Bash(git * push)", "allow")
    assert rule_defect("Bash(git -C * push)", "allow")
    assert rule_defect("Bash(git * -v)", "allow") is None
    assert rule_defect("Bash(git * | wc)", "allow") is None
    assert rule_defect("Bash(* push x)", "allow") is None
    assert rule_defect("Bash(git push)", "allow") is None
    assert rule_defect("Cd(src:*)", "allow")
    assert rule_defect("NotebookEdit(src/**)", "allow")
    assert rule_defect("MultiEdit(src/**)", "allow")
    assert rule_defect("Read(src/**)", "allow") is None
