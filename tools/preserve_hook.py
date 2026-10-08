#!/usr/bin/env python3
"""The repository's destructive-operation policy, on the orchestrator's own path.

Two paths run commands in this programme, and they were not equally guarded.

The **role path** already is: a self-hosted run loads
`policy/role_approval.default.json`, which denies `rm`, `shred`, `git clean` and
the rest for every role in every mode and is refused on load if any mandatory
entry is missing, and `src/hoh/integrity.py` takes a pre/post inventory around
the run as the independent backstop. Policy is not confinement and confinement is
not integrity evidence; those are three separate things and all three are there.

The **orchestrator path** was not. The only guard in front of the outer session is
the machine-global denylist, whose own design rule is to block the catastrophic
and let recoverable local deletion through -- and by that rule it was right to
allow `rm -rf` on a parked duplicate of a run's evidence on 2026-09-23. "Project-
and evidence-owned" was not expressed anywhere a guard could read.

`tools/preserve.py` expresses it. This file is the adapter that puts it in front
of the orchestrator's commands, and it is deliberately **repository-owned and
project-local**: wired through `.claude/settings.json` in this tree, versioned
with the capability that depends on it, affecting no other repository, session or
project. The machine-global configuration is not modified, and this programme's
correctness claims do not rest on whatever happens to be installed there.

A hook is handed a shell *string*, and deciding a safety question by parsing shell
text is what cost four rounds in the P1-16 acceptance record. So:

* `shlex` is a real lexer, and its result is checked as an argument vector;
* a command that cannot be lexed and names a destructive program is **blocked**,
  not waved through -- an unparseable command has not been shown to be safe;
* every other unparseable command is allowed, because this hook is not the
  boundary and a guard that blocks everything it does not understand gets
  switched off;
* errors in this file itself allow the command, with one exception: if
  `tools/preserve.py` cannot be loaded at all, a destructive program is blocked
  and the message says the policy is missing. A missing policy must not read as
  permission.

Exit codes: 0 allow, 2 block with the message on stderr (what the harness's
PreToolUse contract treats as a refusal the caller sees).
"""
from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
POLICY = HOH / "tools" / "preserve.py"

#: Programs whose presence makes an unparseable command worth blocking. Kept as
#: a literal list rather than imported, because the import is exactly what may
#: have failed when this matters.
DESTRUCTIVE_WORDS = ("rm", "rmdir", "shred", "dd", "truncate", "unlink",
                     "git clean", "find")

#: Shell constructs that make one string into several commands. Each is lexed
#: and checked on its own; a protected path behind `&&` is still a protected
#: path.
SPLIT = re.compile(r"(?:\|\||&&|;|\||\n)")


def _policy():
    """`tools/preserve.py`, or None when it cannot be loaded."""
    try:
        spec = importlib.util.spec_from_file_location("preserve", POLICY)
        module = importlib.util.module_from_spec(spec)
        sys.modules["preserve"] = module
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, SyntaxError, AttributeError, ValueError):
        return None


def looks_destructive(text: str) -> bool:
    """Does this text name a program that destroys?

    Word-boundary matching on purpose: `format` should not match `rm`, and
    `rm-safe` is not `rm`.
    """
    for word in DESTRUCTIVE_WORDS:
        if re.search(rf"(^|[\s;&|(]){re.escape(word)}([\s;&|)]|$)", text):
            return True
    return False


def decide(command: str) -> tuple[int, str]:
    """`(exit code, message)` for one shell string.

    The policy module answers about argument vectors; this decides what vectors
    the string contains, and what to do when it cannot tell.
    """
    if not command.strip():
        return 0, ""
    policy = _policy()
    if policy is None:
        if looks_destructive(command):
            return 2, (f"BLOCKED: the project's destructive-operation policy "
                       f"({POLICY}) could not be loaded, and this command names a "
                       "program that destroys. A missing policy is not "
                       "permission. Restore the policy, or run the command "
                       "yourself after checking what it would remove")
        return 0, ""

    problems: list[str] = []
    for piece in SPLIT.split(command):
        piece = piece.strip()
        if not piece:
            continue
        try:
            argv = shlex.split(piece)
        except ValueError:
            if looks_destructive(piece):
                problems.append(
                    f"BLOCKED: `{piece[:120]}` could not be parsed as a command, "
                    "and it names a program that destroys. An unparseable "
                    "command has not been shown to be safe. Rewrite it so it can "
                    "be read, or use tools/preserve.py supersede|archive")
            continue
        problems.extend(policy.command_problems(argv))
    if problems:
        return 2, "\n".join(f"BLOCKED by the project's no-delete policy: {p}"
                            if not p.startswith("BLOCKED") else p
                            for p in problems)
    return 0, ""


def main(argv=None) -> int:
    raw = sys.stdin.read() if argv is None else ""
    if argv:                                   # a direct call, for the tests
        code, message = decide(" ".join(argv))
    else:
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return 0                           # not our payload; not our business
        command = ""
        for path in (("tool_input", "command"), ("toolInput", "command"),
                     ("command",), ("input", "command")):
            spot = payload
            for key in path:
                spot = spot.get(key) if isinstance(spot, dict) else None
            if isinstance(spot, str) and spot:
                command = spot
                break
        code, message = decide(command)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
