"""Agent-session integration: the text that tells a coding-agent session to use VeriHarness.

A fresh machine can have `hoh` and Herdr and still have an agent session that never uses them:
nothing told it to. This module holds that text (a skill, a short rules snippet, the method
references and templates beside the skill) as package data under `hoh/agent_docs/`, and the two
operations of `hoh agent-setup`: print a text, or install it into a directory the caller named.

Installing writes only new files, only inside the named directory, through `durable_create`; it
never replaces, appends to or edits a file. A directory that is a user-level agent configuration
(`~/.claude`, `~/.codex`, `~/.agents`, `~/.config`, `$CLAUDE_CONFIG_DIR`) is refused unless the
caller says `--allow-user-config`. Nothing here starts Herdr, an agent or a run.
"""
from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from .durable_io import durable_create

KINDS = ("skill", "snippet")
DELEGATIONS = ("ask", "standing")
SKILL_DIR = "veriharness"
SNIPPET_FILE = "veriharness-agent-rules.md"
#: Relative to `hoh/agent_docs/`; the skill directory is installed with exactly these.
SKILL_FILES = (
    "SKILL.md",
    "references/method.md",
    "references/rotation.md",
    "templates/ack.template.json",
    "templates/drafting-brief.template.md",
    "templates/handoff.template.md",
    "templates/reviewer-rules.template.md",
)
SNIPPET_SOURCE = "snippet.md"
PLACEHOLDER = "<!-- DELEGATION -->"
USER_CONFIG_DIRS = (".claude", ".codex", ".agents", ".config")

DELEGATION_TEXT = {
    "ask": (
        "Consent: the operator installed this so that you offer VeriHarness for development work. "
        "Before the first `hoh run` of a task, say what it will spend (three role sessions per "
        "iteration, the `--iterations` you intend) and wait for a yes. A yes covers further "
        "iterations of the same task up to the count you named. It does not cover RULE user-config "
        "(a write to the user's agent configuration): that needs its own yes."
    ),
    "standing": (
        "Consent: the operator installed this as a standing delegation. Start runs for development "
        "tasks without asking, with `--max-iterations 3` unless the user names another number, and "
        "report the cost afterwards. Ask before going past that budget, and before anything the "
        "rules below reserve for the user. This delegation does not cover RULE user-config "
        "(a write to the user's agent configuration): that needs its own yes."
    ),
}


class SetupRefused(Exception):
    """The command will not do this; the message says why. Nothing was written."""


class SetupFailed(SetupRefused):
    """An I/O error after the checks passed (a full disk): some files may have been written.
    The message lists them; nothing is removed."""


def _read(rel: str) -> str:
    node = resources.files("hoh").joinpath("agent_docs")
    for part in rel.split("/"):
        node = node.joinpath(part)
    return node.read_text(encoding="utf-8")


def render(kind: str, delegation: str = "ask") -> str:
    """The text of `kind`, with the delegation paragraph filled in."""
    if kind not in KINDS:
        raise SetupRefused(f"unknown kind {kind!r}")
    if delegation not in DELEGATIONS:
        raise SetupRefused(f"unknown delegation {delegation!r}")
    text = _read("SKILL.md" if kind == "skill" else SNIPPET_SOURCE)
    if text.count(PLACEHOLDER) != 1:
        raise SetupRefused(f"the bundled {kind} text is damaged (delegation placeholder)")
    return text.replace(PLACEHOLDER, DELEGATION_TEXT[delegation])


def files_for(kind: str, delegation: str = "ask") -> list[tuple[str, str]]:
    """(path relative to the install directory, content) for one kind."""
    if kind == "snippet":
        return [(SNIPPET_FILE, render("snippet", delegation))]
    out = []
    for rel in SKILL_FILES:
        content = render("skill", delegation) if rel == "SKILL.md" else _read(rel)
        out.append((f"{SKILL_DIR}/{rel}", content))
    return out


def user_config_roots() -> list[Path]:
    """Resolved user-level agent configuration directories (which need the explicit flag)."""
    home = Path(os.path.expanduser("~"))
    roots = [home / name for name in USER_CONFIG_DIRS]
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        extra = os.environ.get(var)
        if extra:
            roots.append(Path(extra).expanduser())
    return [Path(os.path.realpath(r)) for r in roots]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def install(directory: str, kinds: list[str], delegation: str = "ask",
            allow_user_config: bool = False) -> list[Path]:
    """Write the files of `kinds` under `directory` (which must exist); return the paths written."""
    try:
        target = Path(directory).expanduser()
        real = Path(os.path.realpath(target))
    except (RuntimeError, ValueError) as exc:
        raise SetupRefused(f"the directory cannot be used: {exc}") from exc
    if not target.is_dir():
        raise SetupRefused(f"not an existing directory: {directory} (it is never created here)")
    if not allow_user_config:
        for root in user_config_roots():
            if _inside(real, root):
                raise SetupRefused(
                    f"{directory} is inside the user-level agent configuration {root}; "
                    "name a project directory, or pass --allow-user-config")
    planned: list[tuple[Path, str]] = []
    for kind in kinds:
        for rel, content in files_for(kind, delegation):
            planned.append((real / rel, content))
    for path, _ in planned:
        if os.path.lexists(path):
            raise SetupRefused(f"{path} already exists; this command never replaces a file")
        for d in (path.parent.parent, path.parent):
            if d == real or real not in d.parents:
                continue
            if os.path.islink(d):
                raise SetupRefused(f"{d} is a symbolic link; this command writes only inside {real}")
            if os.path.lexists(d) and not os.path.isdir(d):
                raise SetupRefused(f"{d} exists and is not a directory; nothing was written")
    written: list[Path] = []
    try:
        for path, content in planned:
            chain = [d for d in (path.parent.parent, path.parent)
                     if d != real and real in d.parents]
            for d in chain:
                if not d.exists():
                    os.mkdir(d)
            durable_create(path, content)
            written.append(path)
    except OSError as exc:
        done = ", ".join(str(p) for p in written) or "nothing"
        raise SetupFailed(f"install stopped on {exc}; written before it: {done}") from exc
    return written
