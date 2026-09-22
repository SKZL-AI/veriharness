#!/usr/bin/env python3
"""Freezes a benchmark campaign's protocol and instrument, and checks the freeze.

A pre-registration that lives only in prose is a promise. This turns it into a
file: `freeze` records which commit the protocol was frozen at, which commit
the instrument was frozen at, and the digest of every file the campaign is not
allowed to change while it runs. `check` compares the working tree against
that record and names what moved.

The list of frozen files is the answer to "what could change the measurement",
and it is written down rather than inferred: the runner, the budget semantics
the matched-budget premise rests on, the tasks with their visible and hidden
suites, and the protocol text itself. Documentation, ledgers and the readiness
tool are deliberately *not* frozen -- no arm reads them, and freezing them
would mean a campaign could not record its own findings while it ran.

Why this is not a git tag: a tag says which commit, not which properties. The
question a reader of a campaign result has is "was the thing that produced
these numbers the thing that was declared", and that is a digest question.

Usage:
    python3 tools/prereg.py freeze --campaign v3 [--note TEXT]
    python3 tools/prereg.py check  --campaign v3 [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent

#: Everything a campaign may not change while it runs, and why each is here.
#: A path that names a directory freezes every file under it.
FROZEN: dict[str, str] = {
    "docs/BENCHMARK_PROTOCOL.md":
        "the protocol itself -- design, budget, repetitions, stopping rules",
    "dogfood/benchmark/tasks":
        "the five tasks, their specifications, their visible tests and their "
        "hidden suites",
    "src/hoh":
        "the whole package. Naming three modules was not enough and an "
        "adversarial review proved it: `roles.py` holds the literal prompt "
        "text every arm receives and the parser that decides whether a schema "
        "repair -- a dispatch -- happens; `telemetry.py` defines the field the "
        "protocol names as the source of the cost figure; `dispatchers.py` "
        "decides which provider is reached at all. A frozen set assembled by "
        "listing the modules one happens to think of is the same defect as a "
        "cost figure asserted beside a result",
    "tools/benchmark.py":
        "the runner, the evaluator, the false-accept definition and the "
        "outcome definition",
    "tools/repetition_plan.py":
        "how a campaign's completeness and budget conformance are decided",
    "tools/readiness.py":
        "how a campaign's result becomes a release verdict",
    "tools/budget_evidence.py":
        "the control that establishes the budget is enforced at all",
    "tools/prereg.py":
        "this file. A freeze whose own checker may change during the campaign "
        "checks nothing",
    "pyproject.toml":
        "the dependency floors and the package's own build metadata",
}

#: Deliberately **not** frozen, stated so that its absence is a decision
#: rather than an omission: `tools/check_claims.py`, `export_manifest.py`,
#: `evidence_index.py`, `attribution.py`, `audit_refs.py`, `meta_evidence.py`,
#: `union_gate.py`, `telemetry_audit.py`, `confinement_evidence.py`,
#: `clean_install_check.py`, `collect_strict_evidence.py`, every document
#: outside the protocol, and the ledgers. None of them is read by an arm, none
#: evaluates a campaign cell, and freezing them would mean a campaign could not
#: record its own findings while it ran.


def _env_capture() -> dict:
    """What the campaign is running *on*, recorded because it is not frozen.

    A digest set says the files did not change. It says nothing about the
    interpreter, the installed packages, the environment variables that are
    interpolated into role prompts, or -- the one that matters most -- which
    model answered. `tools/benchmark.py` dispatches the profile "claude",
    which is whatever that CLI resolves to on the day. So the instrument on
    the day of cell 1 and the day of cell 45 is not *provably* the same
    instrument, and this records enough for a reader to see whether it was.
    """
    import platform

    def _version(*argv: str) -> str:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"unavailable: {type(exc).__name__}"
        return (p.stdout or p.stderr).strip().splitlines()[0][:120] if (
            p.stdout or p.stderr).strip() else "unavailable: no output"

    pakete = {}
    for name in ("pydantic",):
        try:
            from importlib.metadata import version

            pakete[name] = version(name)
        except Exception as exc:                    # pragma: no cover - exotic
            pakete[name] = f"unavailable: {type(exc).__name__}"

    # Variables that change what a role is sent. Their *content* is not
    # recorded (it can be long and is not this file's business); their
    # presence and digest are, because a prompt that differs between cell 1
    # and cell 45 is a different instrument.
    environment_ = {}
    for name in ("HOH_HOUSE_RULES", "HERDR_ENV", "HOH_TRUST_HELPER",
                 "HOH_WORKTREE_ROOT"):
        value_ = os.environ.get(name)
        environment_[name] = (
            "unset" if value_ is None
            else hashlib.sha256(value_.encode()).hexdigest()[:16])

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": pakete,
        "agent_cli": _version("claude", "--version"),
        "git": _version("git", "--version"),
        "environment_digests": environment_,
        "model": "not pinned -- tools/benchmark.py dispatches the profile "
                 "'claude' and the CLI chooses. Recorded as a limitation, not "
                 "as a guarantee.",
    }


def _commit() -> str:
    p = subprocess.run(["git", "-C", str(HOH), "rev-parse", "HEAD"],
                       capture_output=True, text=True, check=False)
    return p.stdout.strip()


def _dirty() -> list[str]:
    p = subprocess.run(["git", "-C", str(HOH), "status", "--porcelain", "-uall"],
                       capture_output=True, text=True, check=False)
    return [z for z in p.stdout.splitlines() if z.strip()]


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def digeste() -> tuple[dict[str, str], list[str]]:
    """(one digest per frozen file, problems found while walking).

    Directories are walked, and the walk includes everything -- a hidden test
    *added* under a task directory changes the campaign as surely as an edited
    one, and a frozen set that only notices modifications would miss it.

    Symlinked directories are recorded and reported rather than followed.
    `Path.rglob` does not descend into them, so files placed behind one would
    be invisible to the digest set while being perfectly visible to the
    benchmark. Following them would invite a cycle; naming them lets the
    check refuse instead of quietly under-covering.
    """
    out_list: dict[str, str] = {}
    problems: list[str] = []

    def _file(f: Path) -> None:
        out_list[str(f.relative_to(HOH))] = (
            f"SYMLINK:{os.readlink(f)}" if f.is_symlink() else _digest(f))

    for entry in sorted(FROZEN):
        p = HOH / entry
        if p.is_dir():
            for root, directories, files in os.walk(p, followlinks=False):
                w = Path(root)
                directories[:] = [d for d in sorted(directories)
                                    if d != "__pycache__"]
                for d in list(directories):
                    if (w / d).is_symlink():
                        problems.append(
                            f"{(w / d).relative_to(HOH)} is a symlinked "
                            f"directory inside a frozen tree: its contents are "
                            f"not covered by any digest")
                        out_list[str((w / d).relative_to(HOH))] = \
                            f"SYMLINK:{os.readlink(w / d)}"
                        directories.remove(d)
                for name in sorted(files):
                    _file(w / name)
        elif p.is_file():
            _file(p)
        else:
            out_list[entry] = "ABSENT"
    return out_list, problems


def digests_in_commit(commit: str) -> dict[str, str]:
    """The same digests, computed from a **commit** instead of the tree.

    This is what makes the registration self-covering. `PREREGISTRATION.json`
    is not in its own digest set -- it cannot be, it is written after them --
    and since the repetition count is now read out of the commit that file
    names, rewriting one field would silently redefine the campaign. An
    adversarial review did exactly that: a 45-run design became a 15-run
    design with no drift signal.

    So the commit is checked too. A rewritten commit field points at a tree
    whose files hash differently, and the comparison says so.
    """
    p = subprocess.run(["git", "-C", str(HOH), "ls-tree", "-r", "--long",
                        commit],
                       capture_output=True, text=True, check=False)
    if p.returncode != 0:
        return {}
    out_list: dict[str, str] = {}
    for line in p.stdout.splitlines():
        try:
            head, path = line.split("\t", 1)
            _mode, art, _blob, _size = head.split()
        except ValueError:                          # pragma: no cover - exotic
            continue
        if art != "blob":
            continue
        if not any(path == e or path.startswith(e + "/") for e in FROZEN):
            continue
        if "__pycache__" in path:
            continue
        content_ = subprocess.run(
            ["git", "-C", str(HOH), "show", f"{commit}:{path}"],
            capture_output=True, check=False)
        out_list[path] = hashlib.sha256(content_.stdout).hexdigest()[:16]
    return out_list


def path_for(campaign_: str) -> Path:
    return HOH / "docs" / "benchmarks" / campaign_ / "PREREGISTRATION.json"


def cmd_freeze(args) -> int:
    target = path_for(args.campaign)
    debris = _dirty()
    if debris and not args.allow_dirty:
        print("the working tree is not clean, so a commit would not describe "
              "what is being frozen:")
        for z in debris[:20]:
            print("  " + z)
        print("commit first, or pass --allow-dirty and accept that the "
              "recorded commit is not the recorded state")
        return 1
    stamp_digests, problems = digeste()
    data_ = {
        "campaign_id": args.campaign,
        "frozen_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        f"benchmark_{args.campaign}_protocol_commit": _commit(),
        f"benchmark_{args.campaign}_instrument_commit": _commit(),
        "working_tree_clean_at_freeze": not debris,
        "note": args.note or "",
        "frozen_paths": FROZEN,
        "walk_problems": problems,
        "environment": _env_capture(),
        "digests": stamp_digests,
    }
    if target.exists():
        # Never overwritten. A re-freeze of a campaign that already has one is
        # either a mistake or a new campaign, and both deserve to be visible.
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        parked = target.with_name(f"{target.name}.v{stamp}")
        target.rename(parked)
        data_["replaces"] = parked.name
        print(f"parked the previous registration as {parked.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data_, indent=2) + "\n", encoding="utf-8")
    print(f"froze {len(data_['digests'])} file(s) for campaign "
          f"{args.campaign} at "
          f"{data_[f'benchmark_{args.campaign}_protocol_commit'][:12]}")
    for x in problems:
        print(f"  problem: {x}")
    print(f"wrote {target.relative_to(HOH)}")
    return 0


def _tracked(rel: str) -> bool:
    """Is this path committed? A registration that exists only in a working
    directory is a draft, not a pre-registration."""
    return subprocess.run(
        ["git", "-C", str(HOH), "ls-files", "--error-unmatch", rel],
        capture_output=True, text=True, check=False).returncode == 0


def _commit_field(reg: dict, campaign_: str, art: str) -> str:
    return str(reg.get(f"benchmark_{campaign_}_{art}_commit")
               or reg.get(f"{art}_commit") or "").strip()


def comparison(campaign_: str) -> dict:
    target = path_for(campaign_)
    if not target.is_file():
        return {"campaign_id": campaign_, "registered": False,
                "verdict": "NOT_REGISTERED",
                "detail": f"no {target.relative_to(HOH)}"}
    reg = json.loads(target.read_text())
    old = reg.get("digests") or {}
    fresh, problems = digeste()
    changed = sorted(k for k in old if k in fresh and old[k] != fresh[k])
    vanished = sorted(k for k in old if k not in fresh)
    alongside = sorted(k for k in fresh if k not in old)

    log_ = _commit_field(reg, campaign_, "protocol")
    instrument = _commit_field(reg, campaign_, "instrument")
    # The protocol says this compares the working tree against both commits.
    # It did not: it compared digests and never invoked git, while the
    # repetition count is read out of the commit the registration names. So a
    # rewritten commit field redefined the campaign with no drift signal.
    in_commit = digests_in_commit(log_) if log_ else {}
    commit_deviation = sorted(
        k for k in old
        if k in in_commit and in_commit[k] != old[k]
        and not old[k].startswith("SYMLINK:"))
    commit_missing = sorted(k for k in old
                          if k not in in_commit and old[k] != "ABSENT")
    # And the registration itself: tracked, committed, unmodified. An
    # untracked or dirty registration is a pre-registration that exists only
    # in someone's working directory.
    rel = str(target.relative_to(HOH))
    debris = [z for z in _dirty() if rel in z]
    tracked_ = _tracked(rel)

    ok = not (changed or vanished or alongside or problems
              or commit_deviation or commit_missing or debris or not tracked_
              or not log_ or not reg.get("working_tree_clean_at_freeze"))
    return {
        "campaign_id": campaign_,
        "registered": True,
        "frozen_at": reg.get("frozen_at"),
        "protocol_commit": log_,
        "instrument_commit": instrument,
        "files_frozen": len(old),
        "changed": changed,
        "removed": vanished,
        "added": alongside,
        "walk_problems": problems,
        "differs_from_the_named_commit": commit_deviation,
        "not_in_the_named_commit": commit_missing,
        "registration_is_tracked": tracked_,
        "registration_is_clean": not debris,
        "working_tree_clean_at_freeze": bool(
            reg.get("working_tree_clean_at_freeze")),
        "replaces": reg.get("replaces", ""),
        "environment": reg.get("environment") or {},
        "verdict": "FROZEN" if ok else "DRIFTED",
    }


def cmd_check(args) -> int:
    v = comparison(args.campaign)
    if args.json:
        print(json.dumps(v, indent=2))
    else:
        print(f"  campaign            {v['campaign_id']}")
        print(f"  verdict             {v['verdict']}")
        if v.get("registered"):
            print(f"  frozen at           {v['frozen_at']} "
                  f"({(v['protocol_commit'] or '')[:12]})")
            print(f"  files frozen        {v['files_frozen']}")
            print(f"  registration        "
                  f"{'tracked' if v['registration_is_tracked'] else 'UNTRACKED'}, "
                  f"{'clean' if v['registration_is_clean'] else 'MODIFIED'}")
            if not v["working_tree_clean_at_freeze"]:
                print("  frozen from a DIRTY working tree: the recorded "
                      "commit does not describe the recorded state")
            if v.get("replaces"):
                print(f"  replaces            {v['replaces']}")
            for art in ("changed", "removed", "added", "walk_problems",
                        "differs_from_the_named_commit",
                        "not_in_the_named_commit"):
                for f in v.get(art) or []:
                    print(f"  {art:<19s} {f}")
        else:
            print(f"  {v['detail']}")
    return 0 if v["verdict"] == "FROZEN" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="record the freeze for a campaign")
    f.add_argument("--campaign", required=True)
    f.add_argument("--note", default="")
    f.add_argument("--allow-dirty", action="store_true")
    f.set_defaults(func=cmd_freeze)
    c = sub.add_parser("check", help="compare the tree against the freeze")
    c.add_argument("--campaign", required=True)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
