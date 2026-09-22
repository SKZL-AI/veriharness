"""Registering a HoH-owned run directory as trusted with the harness.

Claude Code puts every new folder behind a trust dialog. From inside a pane
that dialog cannot be answered -- the agent goes to `blocked`, and HoH never
answers someone else's trust dialog on its own. For directories that **HoH
created itself**, though, this is not a trust question at all: the captain
started the run, and the folder contains nothing but HoH's own artifacts.

The mechanism is taken from `firstmate/bin/fm-claude-trust.sh`: the entry
`projects[<path>].hasTrustDialogAccepted = true` in
`${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json`.

The structural boundary is the point of this file: **only paths below the HoH
root**. Trusting an arbitrary directory would be exactly the silent widening
of permissions that the rest of this project is built against. firstmate's
script carries the same refusal, and there it was the hint that the developer
belongs in an isolated worktree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .contracts import utcnow


class TrustRefused(RuntimeError):
    """The path does not belong to HoH -- no trust granted."""


def config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home())
    return Path(base) / ".claude.json"


def register(path: Path | str, *, owned_root: Path | str) -> bool:
    """Registers `path` as trusted. True when something was written.

    `owned_root` is the root below which HoH owns directories -- typically
    `--root`. Anything outside is refused, not silently passed over.
    """
    target = Path(path).resolve()
    root = Path(owned_root).resolve()
    if not target.is_relative_to(root):
        raise TrustRefused(
            f"{target} does not lie below the HoH root {root}. "
            "HoH grants trust only for its own run directories; someone "
            "else's folders are the captain's decision, in the tab."
        )
    if not target.is_dir():
        raise TrustRefused(f"{target} is not a directory")

    conf = config_path()
    if not conf.exists():
        return False

    raw = conf.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrustRefused(f"{conf} is not readable ({exc}) -- nothing touched") from exc

    projects = data.setdefault("projects", {})
    entry = projects.setdefault(str(target), {})
    if entry.get("hasTrustDialogAccepted") is True:
        return False

    # Nothing is deleted: the previous state is parked next to it with a
    # version stamp.
    backup = conf.with_name(f".claude.json.hoh-{utcnow()}.bak")
    backup.write_text(raw, encoding="utf-8")

    entry["hasTrustDialogAccepted"] = True
    tmp = conf.with_name(f".claude.json.hoh-tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, conf)
    return True


def herdr_worktree_root() -> Path:
    """Where `hoh worktree` has Herdr put the worktrees it creates."""
    return Path.home() / ".herdr" / "worktrees"


def trust_run_worktree(path: Path | str, *, herdr_root: Path | str | None = None) -> dict:
    """Pre-grant trust for a run's own development worktree (P1-09).

    The developer's worktree is the one directory a run needs that HoH did not
    create below its own root, so `register` refuses it and the developer
    stops at the trust dialog. `hoh run --trust-worktree` asks for it to be
    granted up front instead -- explicitly, per run, and only for a directory
    that is all three of:

    * inside Herdr's managed worktree root, one project level down, so the
      grant cannot reach the root itself or anything beside it;
    * a *linked* git worktree (its git dir differs from the common dir), so an
      ordinary checkout or an arbitrary folder is refused;
    * the path the caller passes, which `hoh run` takes from the run's own
      recorded repository and nowhere else.

    Returns what was granted, for the run's evidence. Refuses with the reason
    otherwise; nothing is written on a refusal.
    """
    import subprocess

    target = Path(path).resolve()
    root = Path(herdr_root or herdr_worktree_root()).resolve()
    if not target.is_dir():
        raise TrustRefused(f"{target} is not a directory")
    if target == root or not target.is_relative_to(root):
        raise TrustRefused(
            f"{target} is not below Herdr's worktree root {root}; only a "
            "worktree Herdr created for this project can be pre-trusted")
    rel = target.relative_to(root)
    if len(rel.parts) < 2:
        raise TrustRefused(
            f"{target} is a project directory, not a worktree inside one")

    def git(*a: str) -> str:
        p = subprocess.run(["git", "-C", str(target), *a],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise TrustRefused(f"{target} is not a git worktree: "
                               f"{(p.stderr or p.stdout).strip()[:120]}")
        return p.stdout.strip()

    git_dir = Path(git("rev-parse", "--absolute-git-dir")).resolve()
    common = Path(git("rev-parse", "--git-common-dir"))
    common = (common if common.is_absolute() else target / common).resolve()
    if git_dir == common:
        raise TrustRefused(
            f"{target} is a main checkout, not a linked worktree; a checkout "
            "somebody works in is the captain's to trust, in the tab")
    project_root = root / rel.parts[0]
    written = register(target, owned_root=project_root)
    return {"worktree": str(target), "herdr_root": str(root),
            "project_root": str(project_root), "git_common_dir": str(common),
            "registered": written, "at": utcnow()}
