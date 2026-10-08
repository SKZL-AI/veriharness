"""Legacy reviewer scratch (STORAGE-L): survey and adoption.

`survey` is read-only: it lists the direct child directories of a legacy
``reviews/`` directory and drafts, for each, the entry of a
`veriharness.storage-legacy-roots/1` anchor -- the committed designations and
reports that name it, and the content binding (`children`) -- or the reasons
it cannot be adopted. The draft is reviewed and committed by people; nothing
here commits. `adopt` registers every root of a committed anchor through
`StorageRegistry.register_legacy_root`, the one door, and reports each
outcome. Nothing here removes anything, and there is no override.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from hoh.durable_io import durable_create
from hoh.storage_registry import (
    LEGACY_ROOTS_SCHEMA,
    REGISTERED,
    StorageOwnershipError,
    StorageRegistry,
)

REFUSED = "REFUSED"
_KIND = "legacy_review"


def _committed_naming(registry: StorageRegistry, repo: Path, path: Path
                      ) -> tuple[list[dict], list[dict]]:
    """Committed files under ``dogfood/`` at HEAD that contain ``<path>/``:
    (designations, reports), each ``{commit, file, blob}``. Only committed
    blobs are read; the working tree never is."""
    head = registry._git(repo, "rev-parse", "--verify", "HEAD^{commit}", ceiling=repo.parent)
    if head.returncode != 0:
        raise StorageOwnershipError(f"cannot read HEAD of {repo}")
    commit = head.stdout.strip()
    found = registry._git(repo, "grep", "-l", "-z", "-F", "-e", f"{path}/", commit, "--",
                          "dogfood", ceiling=repo.parent)
    if found.returncode not in (0, 1):
        raise StorageOwnershipError(f"git grep failed in {repo}: {found.stderr.strip()}")
    designations, reports = [], []
    for name in found.stdout.split("\0"):
        if not name:
            continue
        file = name.partition(":")[2]
        blob = registry._git(repo, "rev-parse", f"{commit}:{file}", ceiling=repo.parent)
        cited = {"commit": commit, "file": file, "blob": blob.stdout.strip()}
        (reports if "-report" in Path(file).name else designations).append(cited)
    return designations, reports


def survey(reviews_dir: os.PathLike[str] | str, *, registry: StorageRegistry,
           project_id: str) -> dict[str, Any]:
    """The anchor draft for the direct child directories of `reviews_dir`:
    ``{"schema", "roots": [...], "refused": [{"path", "reasons"}]}``.
    Read-only."""
    project = registry._require_project(project_id)
    repo = Path(project["project_root"])
    base = Path(reviews_dir)
    roots, refused = [], []
    with os.scandir(base) as listing:
        names = sorted(item.name for item in listing)
    for name in names:
        path = base / name
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            continue
        reasons = []
        designations, reports = _committed_naming(registry, repo, path)
        if not designations:
            reasons.append("no committed designation names it")
        if not reports:
            reasons.append("no committed report names it")
        binding: dict[str, Any] = {}
        try:
            binding = registry.legacy_survey_root(path)
        except (StorageOwnershipError, OSError) as exc:
            reasons.append(f"content cannot be bound: {exc}")
        if reasons:
            refused.append({"path": str(path), "reasons": reasons})
            continue
        roots.append({"path": str(path), "kind": _KIND, "project_id": project_id,
                      "designations": designations, "reports": reports, **binding})
    return {"schema": LEGACY_ROOTS_SCHEMA, "roots": roots, "refused": refused}


def write_survey(out: os.PathLike[str] | str, record: dict[str, Any]) -> Path:
    """Write the survey to `out`, a new file, through
    `durable_io.durable_create`: an existing name is `FileExistsError`, and a
    file that appears meanwhile is never replaced (the publish is a link)."""
    target = Path(out)
    durable_create(target, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return target


def adopt(anchor: dict[str, str], *, registry: StorageRegistry, project_id: str
          ) -> list[dict[str, Any]]:
    """Register every `legacy_review` root of `project_id` that the committed
    anchor lists; one record per root, `REGISTERED` or `REFUSED` with the
    reason. A refusal never stops the others and is never swallowed."""
    project = registry._require_project(project_id)
    record, _, _ = registry._read_committed_json(Path(project["project_root"]), anchor)
    if not isinstance(record, dict) or record.get("schema") != LEGACY_ROOTS_SCHEMA:
        raise StorageOwnershipError(f"anchor does not carry schema {LEGACY_ROOTS_SCHEMA}")
    out = []
    for root in record.get("roots") or []:
        if not isinstance(root, dict) or root.get("kind") != _KIND or \
                root.get("project_id") != project_id:
            continue
        try:
            entry = registry.register_legacy_root(project_id, root.get("path"), _KIND, anchor)
            out.append({"path": root.get("path"), "status": REGISTERED, "entry": entry})
        except (StorageOwnershipError, OSError) as exc:
            out.append({"path": root.get("path"), "status": REFUSED, "reason": str(exc)})
    return out


__all__ = ["REFUSED", "adopt", "survey", "write_survey"]
