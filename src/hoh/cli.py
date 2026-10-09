"""Operator surface: hoh start/status/pause/resume/cancel/report.

Handoff §13: these names are the desired operator commands, **not** claimed
Herdr subcommands. They are documented here together with their
implementation and not before.

Every mutating operation takes the controller lock. A second call on the same
run fails visibly instead of writing concurrently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from . import pane_ledger, stages
from .contracts import Budgets, Condition, Role, Stage
from .controller import Controller, new_run_id
from .durable_io import DiskFullError, DurableWriteNotSynced, durable_write
from .store import LockBusy, RunStore, StoreError, list_runs

DEFAULT_ROOT = Path(os.environ.get("HOH_RUNS", Path.home() / "hoh" / "runs"))


def _store(args) -> RunStore:
    return RunStore(args.root, args.run_id)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_worktree(args) -> int:
    """Creates the isolated worktree the developer writes in.

    Handoff §3 and §5 require it; Herdr can create worktrees itself
    (`herdr worktree create`). Rebuilding that with `git worktree add` would be
    exactly the duplicated work this project has already paid for twice.
    """
    from . import herdr as herdr_mod

    if not herdr_mod.available():
        print(
            "Herdr is not running here (HERDR_ENV != 1). The worktree is "
            "created through Herdr so that it stays visible in Herdr's own "
            "bookkeeping.",
            file=sys.stderr,
        )
        return 2
    try:
        result = herdr_mod.worktree_create(
            cwd=str(Path(args.repo).expanduser().resolve()),
            branch=args.branch,
            base=args.base,
            label=f"hoh {args.branch}",
        )
    except Exception as exc:
        print(f"Could not create the worktree: {exc}", file=sys.stderr)
        return 2

    path = (result.get("worktree") or {}).get("path") or result.get("path")
    _print({"worktree": path, "branch": args.branch, "raw": result})
    if path:
        print(
            f"\nNext step:\n"
            f"  hoh --root {args.root} start --repo {path} --spec <spec.md> --run-id <id>",
            file=sys.stderr,
        )
    return 0


def _register_trust(store, root) -> str | None:
    """Registers this run's own directories as trusted with the harness.

    Without it the planner goes to `blocked` on the very first prompt and the
    run stalls -- on a dialog nobody can answer from inside a pane. A failed
    registration is no reason to abort: the captain can grant it in the tab.
    The message then says what to do.
    """
    try:
        from . import trust

        # What gets registered is where the roles actually sit. The arena root
        # for the developer and QA; the run directory is deliberately **no
        # longer** handed to them as a working directory -- that is where the
        # evidence lives.
        #
        # And the planner's own root, which O125 separated out. It has to be
        # registered here rather than left to be discovered: a directory the
        # harness has never seen stops the agent at a dialog nobody in a pane
        # can answer, and moving the planner somewhere safer would otherwise
        # have cost every run its first iteration. Created now, because
        # registering a path that does not exist registers nothing.
        planner_root = store.arenas_dir / "planner"
        planner_root.mkdir(parents=True, exist_ok=True)
        # The **answers directory**, not the run directory -- and this is a
        # tidiness change, not a control. A reviewer read the harness's own
        # resolution: trust is searched from a session's working directory
        # **upwards**, so registering a child of a directory nobody works in
        # grants nothing, and removing the parent takes nothing away either.
        # Every role has a shell, which the policy says of itself
        # (`declared_but_unenforced`), so `checks.json` was never out of reach.
        # What actually stops a write there is the witness in `capability.py`,
        # which fails the run closed. This narrows what HoH asks for to what
        # its roles need: a place to put their answer.
        answers = store.dir / "answers"
        answers.mkdir(parents=True, exist_ok=True)
        registered = [
            path for path in (store.arenas_dir, planner_root, answers)
            if trust.register(path, owned_root=root)
        ]
        if registered:
            return "registered as trusted with the harness: " + ", ".join(
                str(x) for x in registered
            )
        return None
    except Exception as exc:
        return (
            f"trust could not be granted automatically ({exc}) -- confirm once "
            "in the tab if needed"
        )


def cmd_goal(args) -> int:
    """Goalbook: list, propose, approve, drop, complete.

    Separating `propose` from `approve` is the core of it: a proposed
    objective just sits there and does nothing. An agent may propose --
    it may not decide.
    """
    from .goalbook import Goalbook, Objective

    gb = Goalbook(args.root)

    if args.goal_command == "list":
        objectives = gb.read()
        nxt = gb.next_open()
        _print({
            "count": len(objectives),
            "next": (nxt.objective_id if nxt else None),
            "objectives": [
                {"id": o.objective_id, "status": o.status, "title": o.title,
                 "runs": o.runs, "evidence": len(o.evidence_refs),
                 "decided_by": o.decided_by, "reason": o.reason}
                for o in objectives
            ],
        })
        return 0

    if args.goal_command == "propose":
        new_objectives = [Objective(
            objective_id=args.objective_id, title=args.title,
            description=args.description or "",
            spec_path=args.spec, proposed_by=args.by,
        )]
        try:
            check = gb.propose(new_objectives)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print({
            "proposed": args.objective_id,
            "status": "PROPOSED",
            "frame_gate": check.required,
            "report": check.report(),
            "next_step": (
                f"hoh --root {args.root} goal approve {args.objective_id} "
                f"--by <name> --reason \"...\""
            ),
            "note": (
                "A proposed objective does nothing. Only the approval makes it "
                "workable -- even when no frame gate was needed."
            ),
        })
        return 0

    status = {"approve": "OPEN", "drop": "DROPPED",
              "activate": "ACTIVE", "complete": "DONE"}[args.goal_command]
    try:
        changed = gb.decide(
            args.objective_id, status=status, by=args.by, reason=args.reason
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print({"status": status, "by": args.by, "reason": args.reason,
            "objectives": [o.objective_id for o in changed]})
    return 0


def cmd_resume_quota(args) -> int:
    """Resumes branches that are waiting on the usage quota once the wait is over.

    The counter-design to "HoH sleeps by itself": a controller that holds the
    lock while sleeping for hours is indistinguishable from a hung one.
    Instead the branch stays visibly blocked, a durable timer records when it
    may retry, and this command -- by hand, from the systemd timer in `ops/`,
    or as a Herdr action -- consumes the due timers (`hoh.resumeloop`).

    Only the automatic waits are resumed: `usage_limit` and the other kinds of
    `hoh.failures.AUTOMATIC_KINDS` (rate limit, provider unavailable, network,
    prompt stall, role timeout). A branch parked by the autonomy contract, a
    question collected for the digest, an operator-required failure (expired
    credential, account action, model or local runtime missing) or a capability
    violation is reported and left alone: each of those has an owner who is
    not a timer. With `--drive`, a project whose branch was resumed is driven
    on by a new governed master (a new epoch), if it registered a driver.
    """
    from .resumeloop import recover_projects, resume_projects, resume_runs

    # Each phase on its own: a failure in one is reported and the others still
    # run -- the systemd unit is the only thing keeping a quota wait moving
    # (review A4, G4).
    def phase(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as exc:                          # noqa: BLE001 - reported
            return [{"scope": fn.__name__, "action": "skipped",
                     "why": f"{type(exc).__name__}: {exc}"[:200]}]
    runs = phase(resume_runs, args.root, only=args.run_id)
    projects = [] if args.run_id else phase(resume_projects, args.root, drive=args.drive)
    if args.recover and not args.run_id:
        projects += phase(recover_projects, args.root)
    _print({"checked": len({r.get("target") for r in runs}), "results": runs,
            "projects": projects})
    return 0


# --------------------------------------------------------------------------- #
# Storage admission wiring (STORAGE-B3, P2-40)
# --------------------------------------------------------------------------- #

#: Refusal texts of `hoh run`; `hoh.launcher.STORAGE_REFUSED` matches them.
STORAGE_NOT_INITIALISED = ("storage state root is not initialised: run "
                           "`hoh storage init --root {root}` or pass --storage-root")
WSL_NO_HOST_PATHS = "WSL: configure host_paths (e.g. the Windows volume mount)"
WSL_NO_VHDX_VOLUME = "WSL: the VHDX volume is not observed"
STORAGE_UNAVAILABLE = "storage admission unavailable"


def resolve_storage_root(option: str | None) -> Path:
    """`--storage-root`, else `$HOH_STATE_ROOT`, else
    `${XDG_STATE_HOME:-~/.local/state}/veriharness`. Only resolved -- never
    created here."""
    if option:
        return Path(option).expanduser()
    env = os.environ.get("HOH_STATE_ROOT")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg).expanduser() / "veriharness"


class StorageRefused(Exception):
    """`hoh run` / `hoh resume-storage` will not start: the message says why."""


def _same_path(a: str, b: str) -> bool:
    return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(os.path.expanduser(b))


def storage_admission_for(option: str | None, *, claim: bool = True):
    """The admission a host entry point uses, built from the resolved state
    root -- or `StorageRefused` with the operator-facing reason.

    Refused: a root without the registry's `OWNER.json` (never created here:
    no second ledger is invented); on WSL, a policy without `host_paths`, or
    a missing `storage-host.json` / a `wsl_host_volume` not among
    `host_paths`; a policy file, ledger or boot claim that cannot be used
    ("storage admission unavailable"). The boot is claimed before the first
    admission (`claim_boot`).

    WSL or native is decided by the merged host adapter
    (`storage_host.detect_host_profile`, looked up on the module so tests
    inject it there); a profile `unknown` is refused, never read as native.
    On WSL the admission observes the host through the WSL adapter built with
    `HostConfig(windows_mount=<wsl_host_volume>)`; on a native host as
    STORAGE-B2 builds it from the policy.
    """
    from . import storage_host
    from .storage_pressure import (GuestObserver, StorageAdmission, StoragePolicy,
                                   StoragePolicyError)

    root = resolve_storage_root(option)
    if not (root / "OWNER.json").is_file():
        raise StorageRefused(STORAGE_NOT_INITIALISED.format(root=root))
    policy_file = root / "storage-policy.json"
    try:
        policy = StoragePolicy.load(policy_file) if policy_file.is_file() else StoragePolicy()
    except StoragePolicyError as exc:
        raise StorageRefused(f"{STORAGE_UNAVAILABLE}: {exc}") from exc
    try:
        profile = storage_host.detect_host_profile()
    except Exception:                                     # noqa: BLE001 - fail closed
        profile = storage_host.PROFILE_UNKNOWN
    if profile not in (storage_host.PROFILE_WSL2, storage_host.PROFILE_NATIVE):
        raise StorageRefused(f"{STORAGE_UNAVAILABLE}: host profile unknown ({profile!r})")
    observer = None
    if profile == storage_host.PROFILE_WSL2:
        if not policy.host_paths:
            raise StorageRefused(WSL_NO_HOST_PATHS)
        host_file = root / "storage-host.json"
        try:
            host = json.loads(host_file.read_text(encoding="utf-8"))
            volume = host["wsl_host_volume"]
            if not isinstance(volume, str) or not volume:
                raise ValueError("wsl_host_volume must be a non-empty path")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageRefused(f"{WSL_NO_VHDX_VOLUME}: {host_file}: {exc}") from exc
        if not any(_same_path(volume, p) for p in policy.host_paths):
            raise StorageRefused(f"{WSL_NO_VHDX_VOLUME}: wsl_host_volume {volume} is not "
                                 f"among host_paths {list(policy.host_paths)}")
        try:
            adapter = storage_host.adapter_for_profile(
                profile, storage_host.HostConfig(
                    windows_mount=os.path.normpath(os.path.expanduser(volume))))
        except (ValueError, storage_host.StorageHostError) as exc:
            raise StorageRefused(f"{WSL_NO_VHDX_VOLUME}: {host_file}: {exc}") from exc
        observer = GuestObserver(root, adapter.host_observer(policy))
    try:
        admission = StorageAdmission.for_state_root(root, policy, observer)
        if claim:
            admission.claim_boot()
    except Exception as exc:                              # noqa: BLE001 - fail closed
        raise StorageRefused(f"{STORAGE_UNAVAILABLE}: {type(exc).__name__}: {exc}") from exc
    return admission, root


def cmd_resume_storage(args) -> int:
    """Resumes runs and project nodes parked on storage pressure (STORAGE-B3).

    Owns only `blocked_kind='storage_pressure'`; a quota wait is left to
    `hoh resume-quota`. A due STORAGE_RECHECK timer resumes its branch only
    when the state is better than CRITICAL and a role run would be admitted
    now; otherwise a new timer is armed and the branch stays parked. The boot
    is claimed first. Nothing is cleaned, ever.
    """
    from .resumeloop import resume_storage

    try:
        admission, root = storage_admission_for(args.storage_root, claim=False)
    except StorageRefused as exc:
        print(str(exc), file=sys.stderr)
        _print({"checked": 0, "results": [{"scope": "storage", "action": "refused",
                                           "verdict": "NEEDS_YOU", "why": str(exc)}]})
        return 2
    try:
        results = resume_storage(args.root, admission=admission, only=args.run_id)
    except Exception as exc:                              # noqa: BLE001 - reported
        results = [{"scope": "resume_storage", "action": "skipped",
                    "why": f"{type(exc).__name__}: {exc}"[:200]}]
    _print({"storage_root": str(root),
            "checked": len({r.get("target") for r in results}), "results": results})
    return 0


def cmd_storage(args) -> int:
    """`hoh storage init --root PATH`: initialise a storage state root (the
    registry's `OWNER.json`, written before any child)."""
    from .storage_registry import StorageRegistry

    try:
        record = StorageRegistry(Path(args.storage_root_path).expanduser()).init()
    except Exception as exc:                              # noqa: BLE001 - reported
        print(f"storage init refused: {exc}", file=sys.stderr)
        return 2
    _print({"storage_root": str(Path(args.storage_root_path).expanduser()), "owner": record})
    return 0


def cmd_storage_adopt_project(args) -> int:
    """`hoh storage adopt-project --project P --project-root PATH --anchor
    COMMIT:FILE --storage-root PATH`: adopt the existing project directory
    `<storage root>/projects/<P>/` through the adoption anchor committed in
    the trust root (`StorageRegistry.adopt_project`, the only call made).

    A one-time operator act: `--storage-root` is required and has no
    fallback to `$HOH_STATE_ROOT` or the XDG default, the state root is never
    initialised here, and no storage admission is needed (a few hundred
    bytes are written). Exit 0 with `{"storage_root", "project"}` printed;
    exit 2 with `storage adopt-project refused: <message>` on stderr."""
    import re

    from .storage_registry import StorageOwnershipError, StorageRegistry

    commit, sep, file = args.anchor.partition(":")
    if not sep or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        print(f"storage adopt-project refused: --anchor {args.anchor!r} is not COMMIT:FILE "
              f"with a hex commit id", file=sys.stderr)
        return 2
    root = Path(args.storage_root).expanduser()
    try:
        record = StorageRegistry(root).adopt_project(
            args.project, args.project_root, {"commit": commit, "file": file})
    except (StorageOwnershipError, OSError) as exc:
        print(f"storage adopt-project refused: {exc}", file=sys.stderr)
        return 2
    _print({"storage_root": str(root), "project": record})
    return 0


def cmd_storage_survey_legacy(args) -> int:
    """`hoh storage survey-legacy --project P --reviews DIR --out FILE
    --storage-root PATH`: draft a legacy-roots anchor for the reviewer dirs
    in DIR (read-only; writes only the new file FILE). Exit 0 with a summary
    printed; exit 2 with `storage survey-legacy refused: <message>`."""
    from .legacy_scratch import survey, write_survey
    from .storage_registry import StorageOwnershipError, StorageRegistry

    root = Path(args.storage_root).expanduser()
    try:
        record = survey(args.reviews, registry=StorageRegistry(root), project_id=args.project)
        out = write_survey(args.out, record)
    except (StorageOwnershipError, OSError) as exc:
        print(f"storage survey-legacy refused: {exc}", file=sys.stderr)
        return 2
    _print({"storage_root": str(root), "out": str(out), "roots": len(record["roots"]),
            "refused": len(record["refused"])})
    return 0


def cmd_storage_adopt_legacy(args) -> int:
    """`hoh storage adopt-legacy --project P --anchor COMMIT:FILE
    --storage-root PATH`: register the legacy roots a committed anchor lists
    (`legacy_scratch.adopt`). Exit 0 when every root is REGISTERED, 2 when
    any is refused (each outcome printed) or the anchor is unusable."""
    import re

    from .legacy_scratch import REFUSED, adopt
    from .storage_registry import StorageOwnershipError, StorageRegistry

    commit, sep, file = args.anchor.partition(":")
    if not sep or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        print(f"storage adopt-legacy refused: --anchor {args.anchor!r} is not COMMIT:FILE "
              f"with a hex commit id", file=sys.stderr)
        return 2
    root = Path(args.storage_root).expanduser()
    try:
        outcomes = adopt({"commit": commit, "file": file}, registry=StorageRegistry(root),
                         project_id=args.project)
    except (StorageOwnershipError, OSError) as exc:
        print(f"storage adopt-legacy refused: {exc}", file=sys.stderr)
        return 2
    _print({"storage_root": str(root), "outcomes": outcomes})
    return 2 if any(o["status"] == REFUSED for o in outcomes) or not outcomes else 0


STEWARD_POLICY_FILE = "steward-policy.json"
ARCHIVE_TARGETS_FILE = "archive-targets.json"
TARGET_IDENTITY_FILE = "VH_TARGET.json"


def _steward_policy(root: Path, project_id: str):
    """The project's steward policy from ``<state_root>/steward-policy.json``
    (``{"projects": {"<p>": {...StewardPolicy fields}}}``), or None when the
    project has none (no steward, no event -- B3's library-use convention)."""
    from .storage_steward import StewardPolicy

    path = root / STEWARD_POLICY_FILE
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        entry = record["projects"][project_id]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(entry, dict):
        return None
    try:
        return StewardPolicy(**entry)
    except TypeError:
        return None


def _hook_steward(root: Path, project_id: str, admission):
    """The recording-only steward `hoh run` gives its controller, or None."""
    from .storage_registry import StorageRegistry
    from .storage_steward import StorageSteward

    policy = _steward_policy(root, project_id)
    if policy is None:
        return None
    try:
        registry = StorageRegistry(root)
        if registry._project_record(project_id) is None:
            return None
        return StorageSteward(project_id, registry, admission, None, policy)
    except Exception:                                     # noqa: BLE001 - no steward
        return None


def _file_identity_probe(root: Path):
    """Interim identity probe until STORAGE-F's observer exists: the mapping
    in ``<target root>/VH_TARGET.json`` (written by the operator when the
    target was set up). Unreadable: None (identity not confirmed)."""
    try:
        record = json.loads((Path(root) / TARGET_IDENTITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _archive_targets(root: Path, registry, session_id: str) -> tuple[list, list[str]]:
    """The archive targets of ``<state_root>/archive-targets.json`` (a list of
    ``{target_id, root, failure_domain, expected_identity}``) and the problems
    that keep the file from being read whole (STORAGE-D Revision 10 item 6):
    the file present but unreadable or not JSON, not a list, an entry that is
    not a mapping, a ``root`` that is not an absolute path (checked before
    `ArchiveTarget` would make it absolute against the cwd), an entry
    `ArchiveTarget` refuses (with its reason), two entries with one
    ``target_id``. Nothing is skipped silently: while any problem is listed
    the relocation refuses every item. An absent file is no targets and no
    problem. Health is ``unknown`` until STORAGE-F observes it.

    The file cannot block the steward (Revision 11 item 3): it is opened
    ``O_RDONLY | O_NONBLOCK | O_NOFOLLOW``, must be a regular file by
    ``fstat`` (``S_ISREG``) and is read through that descriptor -- a FIFO, a
    symlink (``ELOOP``), a directory or any other type is a problem, never
    waited on or followed."""
    import stat as _stat

    from . import archive_target as at

    path = root / ARCHIVE_TARGETS_FILE
    if not os.path.lexists(path):
        return [], []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        return [], [(f"{ARCHIVE_TARGETS_FILE} cannot be opened as a regular file "
                     f"(no link followed): {type(exc).__name__}: {exc}")]
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            return [], [(f"{ARCHIVE_TARGETS_FILE} is not a regular file "
                         f"(mode {_stat.filemode(info.st_mode)}); not read")]
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        entries = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"{ARCHIVE_TARGETS_FILE} cannot be read as JSON: "
                    f"{type(exc).__name__}: {exc}"]
    finally:
        os.close(fd)
    if not isinstance(entries, list):
        return [], [f"{ARCHIVE_TARGETS_FILE} is not a list but {type(entries).__name__}"]
    targets = []
    problems: list[str] = []
    seen: set[str] = set()
    for n, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"entry {n} is not a mapping but {type(entry).__name__}")
            continue
        configured = entry.get("root")
        if not isinstance(configured, str) or not os.path.isabs(configured):
            problems.append(f"entry {n}: root {configured!r} is not an absolute path")
            continue
        try:
            target = at.ArchiveTarget({
                **entry, "identity_probe": _file_identity_probe,
                "health_probe": lambda _root: at.UNKNOWN,
                "registry": registry, "session_id": session_id})
        except Exception as exc:                          # noqa: BLE001 - a problem, said
            problems.append(f"entry {n} refused: {getattr(exc, 'reason', None) or 'refused'}: "
                            f"{exc}")
            continue
        if target.target_id in seen:
            problems.append(f"entry {n}: target_id {target.target_id!r} is listed twice")
            continue
        seen.add(target.target_id)
        targets.append(target)
    return targets, problems


def cmd_storage_steward(args) -> int:
    """`hoh storage steward --project P --once [--plan-only]`: exactly one
    optimisation pass of the project's storage steward (STORAGE-D, directive
    section P), with its report printed. ``--once`` is required and emits
    the ``periodic`` event (the ops timer runs it so); without it the command
    refuses with exit 2 -- the timer is the only scheduler. ``--plan-only``
    plans and reports but never executes. Nothing outside the project's
    registered namespace is touched; the only removing operation is
    VERIFIED_RELOCATION."""
    from .archive_target import RedundancyLedger
    from .storage_registry import StorageRegistry
    from .storage_steward import StewardPolicy, StorageSteward
    from .succession import boot_id
    from .verified_relocation import VerifiedRelocation

    if not getattr(args, "once", False):
        print("storage steward refused: --once is required -- the "
              "hoh-storage-steward@<project>.timer is the only scheduler (no daemon, no loop); "
              "run `hoh storage steward --project P --once` for one pass", file=sys.stderr)
        return 2
    project_id = args.project
    try:
        admission, root = storage_admission_for(getattr(args, "storage_root", None))
    except StorageRefused as exc:
        print(f"NEEDS_YOU: {exc}", file=sys.stderr)
        return 2
    registry = StorageRegistry(root)
    if registry._project_record(project_id) is None:
        print(f"storage steward refused: project {project_id!r} is not registered",
              file=sys.stderr)
        return 2
    try:
        lease = registry.open_session(project_id, "steward", pid=os.getpid(), boot_id=boot_id())
        receipts = registry.scratch_dir(project_id, "steward", "receipts")
    except Exception as exc:                              # noqa: BLE001 - reported
        print(f"storage steward refused: {exc}", file=sys.stderr)
        return 2
    try:
        targets, target_problems = _archive_targets(root, registry, "steward")
        relocation = VerifiedRelocation(registry, targets,
                                        RedundancyLedger(receipts / "redundancy.jsonl"),
                                        receipts, admission=admission,
                                        target_problems=target_problems)
        policy = _steward_policy(root, project_id) or StewardPolicy()
        steward = StorageSteward(project_id, registry, admission, relocation, policy)
        report = steward.run_once(plan_only=args.plan_only, events=("periodic",))
    finally:
        try:
            registry.close_session(project_id, "steward", lease)
        except Exception as exc:                          # noqa: BLE001 - said
            print(f"closing the steward session failed: {exc}", file=sys.stderr)
    _print({"storage_root": str(root), "steward": report})
    return 0


def _worktree_trust_hint(repo_path: str) -> str | None:
    """Says up front how the worktree gets trusted.

    The developer sits in the worktree, and that lies outside the HoH root --
    `trust.register` rightly refuses it. Without the trust grant the first
    developer run blocks on a trust dialog that cannot be answered from a
    pane. That is not a bug, but finding it out afterwards costs a run: on
    `a03` it happened exactly that way.

    firstmate's script is the right way to do it -- it registers **only**
    worktrees and refuses on a primary checkout. That refusal is the same
    boundary HoH draws itself.
    """
    from pathlib import Path as _P

    if (_P(repo_path) / ".git").is_file():          # linked worktree
        # The path comes from the environment, not from the source. Until
        # 2026-09-07 an absolute path into the author's home directory was
        # hard-coded here, pointing at firstmate's trust script -- correct on
        # that one machine and, in a public repo, a line that gives away the
        # author and points nowhere for everybody else. The exact value is
        # deliberately not quoted: it is in the git history, and a neutralized
        # path presented as a verbatim quote would be a small untruth in a
        # comment whose whole subject is a path that was wrong. Without
        # HOH_TRUST_SCRIPT the general hint remains; it is not worse, only
        # less convenient.
        from_env = os.environ.get("HOH_TRUST_SCRIPT", "").strip()
        if from_env:
            script = _P(from_env).expanduser()
            if script.is_file():
                return f"{script} {repo_path} <project-main-checkout>"
        return (
            f"The worktree {repo_path} needs a one-time trust approval in the "
            "tab before the developer can work there."
        )
    return None


def cmd_start(args) -> int:
    run_id = args.run_id or new_run_id()
    store = RunStore(args.root, run_id)
    if store.state_exists():
        print(f"Run {run_id} already exists -- use 'hoh resume'.", file=sys.stderr)
        return 2

    # `hoh start` used to accept a non-existing directory and a non-git
    # directory without a word of complaint.
    repo = Path(args.repo).expanduser()
    if not repo.is_dir():
        print(f"Not a directory: {repo}", file=sys.stderr)
        return 2
    from .workspace import is_git_repo

    if not is_git_repo(repo):
        print(
            f"{repo} is not a git worktree. The candidate binding needs git.",
            file=sys.stderr,
        )
        return 2
    if not Path(args.spec).expanduser().is_file():
        print(f"Specification not found: {args.spec}", file=sys.stderr)
        return 2

    state = Controller.new_state(
        run_id=run_id,
        repo_path=args.repo,
        project_name=args.project or Path(args.repo).name,
        spec_path=args.spec,
        budgets=Budgets(max_iterations=args.max_iterations,
                        max_dispatches=getattr(args, "max_dispatches", None)),
        delivery_mode=args.mode,
    )
    with store.lock():
        store.write_state(state)
    _print(
        {
            "run_id": run_id,
            "stage": state.stage.value,
            "repo": state.repo_path,
            "spec_digest": state.spec_digest,
            "delivery_mode": state.delivery_mode,
            "yolo": state.yolo,
            "trust": _register_trust(store, args.root),
            "next_step": (
                f"hoh --root {args.root} run {run_id} --iterations 1"
            ),
            "worktree_trust": _worktree_trust_hint(state.repo_path),
            "note": (
                "HoH grants trust only for its own directories. The worktree "
                "lies outside and is deliberately not trusted automatically "
                "-- otherwise HoH could obtain access to an arbitrary "
                "directory simply by pointing at it. HoH never answers trust "
                "dialogs itself."
            ),
        }
    )
    return 0


def cmd_status(args) -> int:
    store = _store(args)
    try:
        state = store.read_state()
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    evidence = store.read_evidence(expect_digest=state.evidence_digest)
    recent = state.history[-3:] if state.history else []
    _print(
        {
            "run_id": state.run_id,
            "stage": state.stage.value,
            "condition": state.condition.value,
            "iteration": state.iteration,
            "attempt": state.attempt,
            "working_candidate": state.working_candidate.candidate_id
            if state.working_candidate
            else None,
            "accepted_candidate": state.last_accepted_candidate.candidate_id
            if state.last_accepted_candidate
            else None,
            "open_items": {
                "to_repair": [i.item_id for i in evidence.to_repair()],
                "insufficient": [i.item_id for i in evidence.insufficient()],
                "validated": [i.item_id for i in evidence.to_preserve()],
            },
            "budget": {
                "iterations": f"{state.usage.iterations}/{state.budgets.max_iterations}",
                "without_progress": f"{state.usage.loops_without_progress}/"
                f"{state.budgets.max_loops_without_progress}",
                "schema_repairs": state.usage.schema_repairs,
                "transient_retries": state.usage.transient_retries,
                "dispatches": state.usage.dispatches,
                "exhausted": state.budget_exhausted(),
            },
            "blocked_reason": state.blocked_reason,
            "stop_reason": state.stop_reason,
            "last_progress": state.updated_at,
            "receipts": len(store.receipts()),
            "usage_bytes": store.usage_bytes(),
            "panes": pane_ledger.summary(store.dir),
            "recent_events": recent,
        }
    )
    return 0


def _halt(args, kind: str) -> int:
    """Pause or cancel -- also while an iteration is running.

    The controller holds the lock for a whole iteration. One fix had thereby
    made `pause` and `cancel` unusable: they tried the same lock
    non-blockingly and gave up with "already held". The rule now is: if the
    lock can be taken, the stop takes effect immediately; if an iteration is
    running, the request is recorded and takes effect at the next safe
    boundary between two phases.
    """
    store = _store(args)
    verb = {"pause": stages.pause, "cancel": stages.cancel}[kind]
    try:
        with store.lock():
            state = store.read_state()
            verb(state, args.reason)
            store.write_state(state)
    except LockBusy:
        try:
            store.request_stop(kind, args.reason)
        except StoreError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"An iteration is running right now. {kind.capitalize()} request "
            f"recorded ({args.reason}); it takes effect at the next safe "
            f"boundary.\nCheck the state with: hoh --root {args.root} status "
            f"{args.run_id}"
        )
        return 0
    except (StoreError, stages.TransitionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if kind == "pause":
        print(f"Run {args.run_id} paused at a safe boundary: {args.reason}")
        return 0

    print(f"Run {args.run_id} canceled: {args.reason}")
    open_tasks = [t.task_id for t in state.active_tasks]
    if open_tasks:
        print(
            "Tasks still registered: "
            + ", ".join(open_tasks)
            + "\nStopping goes through firstmate's control plane "
            "(fm-control.sh <id> exit); other Herdr panes stay untouched."
        )
    return 0


def cmd_pause(args) -> int:
    return _halt(args, "pause")


def cmd_resume(args) -> int:
    """Resume with reconciliation, not with a blind restart.

    Handoff §7: *"On resume, first reconcile the existing task/endpoint
    identity and liveness: a running worker is re-attached, a demonstrably
    finished one is evaluated, an unclear state blocks. Do not restart
    blindly."*
    """
    store = _store(args)
    try:
        with store.lock():
            state = store.read_state()
            if stages.violation_unresolved(state):
                reason = (state.blocked_reason or state.stop_reason or "").splitlines()
                first = reason[0][:300] if reason else ""
                if state.condition is Condition.PAUSED:
                    # `hoh pause` accepts a BLOCKED run and keeps the kind, so a
                    # held PAUSED run needs a way back. It goes to BLOCKED, with
                    # no reconciliation: that could only re-block the run and
                    # rewrite a reason that names the violation. Only a
                    # person's `hoh unblock` lifts the hold.
                    stages.resume(state)
                    store.write_state(state)
                    print(f"Run {args.run_id} resumed into BLOCKED: it is still held "
                          f"by a capability violation: {first}\n"
                          f"Only 'hoh unblock {args.run_id} --reason \"...\"' lifts that.")
                    return 0
                # Every other condition, with or without --force: nothing is
                # written. `stages.unblock` would clear a hold that only a
                # person who has looked may clear.
                if state.condition is Condition.BLOCKED:
                    way = (f"'hoh resume' does not lift that. After investigating: "
                           f"hoh unblock {args.run_id} --reason \"...\"")
                else:
                    way = ("'hoh unblock' lifts a BLOCKED run only, so the run stays "
                           "held and nothing of it is merged or delivered.")
                print(f"Run {args.run_id} is {state.condition.value} and held by a "
                      f"capability violation: {first}\n{way}", file=sys.stderr)
                return 2
            findings = _reconcile(state)

            unclear = [b for b in findings if b[1] == "block"]
            if unclear and not args.force:
                from .governance import ContractRefused, bind, block_under_contract

                try:
                    resume_governor = bind(store.dir)
                except ContractRefused as exc:
                    print(f"autonomy contract refused: {exc}", file=sys.stderr)
                    return 2
                block_under_contract(
                    state,
                    "resume unclear: " + "; ".join(f"{b[0]} {b[2]}" for b in unclear),
                    resume_governor,
                )
                store.write_state(state)
                print(
                    "Resume blocked because the state is not decidable:",
                    file=sys.stderr,
                )
                for tid, _, reason in unclear:
                    print(f"  - {tid}: {reason}", file=sys.stderr)
                print(
                    "\nCheck the endpoints named above. If you are sure nothing "
                    "is running any more: run again with --force.",
                    file=sys.stderr,
                )
                return 2

            if args.force and findings:
                state.active_tasks = []
                state.note(f"resume forced; {len(findings)} open task references discarded")

            if state.condition is Condition.PAUSED:
                stages.resume(state)
            elif state.condition is Condition.BLOCKED:
                stages.unblock(state, args.reason or "resume after reconciliation")
            store.write_state(state)
    except (StoreError, LockBusy, stages.TransitionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for tid, verdict, reason in findings:
        print(f"  {tid}: {verdict} -- {reason}")
    print(
        f"Run {args.run_id} resumed (stage {state.stage.value}, "
        f"condition {state.condition.value})."
    )
    if state.stage in (Stage.DEVELOPING, Stage.VERIFYING):
        print(
            "The run was interrupted in the middle of an iteration. "
            "'hoh run' replans it conservatively."
        )
    return 0


def _reconcile(state) -> list[tuple[str, str, str]]:
    """Reconciles every persisted start intent against Herdr's own state.

    This used to parse `herdr agent list` itself and read every state other
    than "working"/"blocked" as *finished*. Herdr's docs say about `unknown`
    explicitly *"does not prove completion"*, and Handoff §7 requires that an
    unclear state **blocks**. `herdr.liveness()` now answers that from
    `herdr api snapshot` -- the machine contract Herdr itself offers for it.
    """
    from . import herdr as herdr_mod

    findings: list[tuple[str, str, str]] = []
    for ref in state.active_tasks:
        if not ref.herdr_pane_id:
            findings.append(
                (ref.task_id, "block",
                 "start intent without a confirmed endpoint -- whether the role "
                 "ever ran is not decidable from here")
            )
            continue
        if not herdr_mod.available():
            findings.append(
                (ref.task_id, "block",
                 f"endpoint {ref.herdr_pane_id} not checkable: Herdr is not running here")
            )
            continue
        verdict, reason = herdr_mod.liveness(ref.herdr_pane_id)
        findings.append((ref.task_id, verdict, f"{ref.herdr_pane_id}: {reason}"))
    return findings


def cmd_cancel(args) -> int:
    return _halt(args, "cancel")


def cmd_unblock(args) -> int:
    """BLOCKED was a dead end: `resume` only admits PAUSED."""
    store = _store(args)
    try:
        with store.lock():
            state = store.read_state()
            # R2: a branch the contract parked is cleared by a person, and the
            # contract log is where a weeks-long run is reviewed. The delivery
            # override was recorded; this one used to leave no trace there.
            parked_by = state.blocked_kind
            if parked_by in ("parked_by_contract", "collected_for_digest",
                             "contract_abort", "contract_stop"):
                from .autonomy import Handling, Routing
                from .governance import ContractRefused, bind, record_override

                try:
                    governor = bind(store.dir)
                except ContractRefused as exc:
                    print(f"autonomy contract refused: {exc}", file=sys.stderr)
                    return 2
                record_override(
                    governor,
                    routing=Routing(decision=state.blocked_reason or "blocked",
                                    kind="decision", handling=Handling.PARK_BRANCH,
                                    delegable=False, why=f"cleared by hoh unblock "
                                                         f"({parked_by})"),
                    by="hoh unblock", what=args.reason)
            stages.unblock(state, args.reason)
            store.write_state(state)
    except (StoreError, LockBusy, stages.TransitionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Block on {args.run_id} lifted: {args.reason}")
    return 0


def cmd_amend(args) -> int:
    """Record a change to a running specification, with what it costs.

    Limit 15 said a specification is immutable for the life of a run and there
    is no supported path to amend one. That was a safe default and a real
    obstruction: requirements are discovered by working on them, and a run
    that cannot absorb a correction has to be thrown away, losing its
    evidence, its budget and the history of why the correction was needed.

    This is the supported path, and it is deliberately not a shortcut. The old
    text is parked and stays readable; the new text is a version beside it; an
    acceptance-affecting amendment names the criteria it touches, and the
    controller then refuses to accept a candidate until each of them has been
    planned and measured again. Nothing here judges whether an amendment is
    honest -- it makes the shape, the author, the reason and the cost visible
    in one place so that a reader can.
    """
    from .amendment import AmendmentKind, AmendmentLedger, park_and_amend, text_digest

    store = _store(args)
    new_text = Path(args.spec_file).expanduser().read_text(encoding="utf-8")
    try:
        with store.lock():
            state = store.read_state()
            chain = store.read_amendments(origin_digest=state.spec_digest)
            if not chain.amendments and chain.origin_digest != state.spec_digest:
                chain = AmendmentLedger(run_id=state.run_id,
                                        origin_digest=state.spec_digest)
            affected = [c.strip() for c in (args.affects or "").split(",") if c.strip()]
            after_acceptance = state.last_accepted_candidate is not None
            amendment = park_and_amend(
                state.spec_path, new_text,
                run_id=state.run_id, amendment_id=args.amendment_id,
                kind=AmendmentKind(args.kind), actor=args.actor,
                reason=args.reason, affected_criteria=affected,
                evidence=[e for e in (args.evidence or "").split(",") if e],
                after_acceptance=after_acceptance, write_seq=state.write_seq,
            )
            # Built before the chain is written, so a duplicate id or a
            # broken chain is refused with nothing changed. The amendment
            # itself already validated before the specification moved.
            chain = AmendmentLedger(
                run_id=chain.run_id, origin_digest=chain.origin_digest,
                amendments=[*chain.amendments, amendment],
            )
            store.write_amendments(chain)
            state.note(f"specification amended: {amendment.summary()}")
            state.spec_digest = text_digest(new_text)
            store.write_state(state)
    except (StoreError, LockBusy, ValueError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(chain.report())
    needed = chain.revalidation_needed()
    if needed:
        print(f"\nAcceptance is withheld until {', '.join(sorted(needed))} "
              "has been planned and measured against the new text.")
    return 0


def _inconclusive_checks(store, state, iteration: int) -> list[tuple[str, str]]:
    """The criteria of one iteration whose receipts say they never ran.

    Read from the receipts, not from the loop's own summary: the summary knows
    "not accepted", and only the receipt knows whether that was a product
    defect or an infrastructure refusal.
    """
    out_list: list[tuple[str, str]] = []
    directory = store.dir / "receipts"
    if not directory.is_dir():
        return out_list
    for f in sorted(directory.glob(f"{state.run_id}-i{iteration}-*.json")):
        if f.stem.endswith("-basis"):
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("runner_ok", True) and d.get("exit_code") not in (124, 126, 127):
            continue
        why = {
            124: "timeout", 126: "refused or not executable", 127: "not found",
        }.get(d.get("exit_code"), "infrastructure error")
        out_list.append((d.get("check_id", f.stem), why))
    return out_list


def _isolation_for_run(state, requested_: str | None):
    """The isolation for this invocation, reconciled with the run's own.

    Three cases, and only one of them is a judgement call:

    * no flag -- the run keeps what it was started with;
    * the same value -- nothing to decide;
    * a different value -- allowed upwards, refused downwards. Lowering it
      mid-run would mean the accepted candidate was verified partly under a
      sandbox and partly not, and the run record would carry a single word for
      two different regimes.
    """
    stored = getattr(state, "isolation", "none") or "none"
    if requested_ is None or requested_ == stored:
        return _isolation(stored)
    created = _isolation(requested_)
    rank = {"none": 0, "strict": 1}
    if rank.get(requested_, 0) < rank.get(stored, 0):
        raise ValueError(
            f"this run was started under --isolation {stored} and asking "
            f"for {requested_} now would verify part of it under a sandbox "
            "and part of it without one. Start a new run if that is what you "
            "want; a run's isolation is a property of the run."
        )
    state.isolation = requested_
    return created


def _isolation(name: str):
    """The requested isolation, or None for the historical path.

    Returns None rather than `Isolation.NONE` for "none" so that the runner
    takes exactly the code path it took before this flag existed. The two are
    equivalent in effect; keeping them literally the same path means the
    default cannot drift because someone changed what NONE does.
    """
    if name in ("", "none"):
        return None
    from .sandbox import Isolation

    try:
        return Isolation(name)
    except ValueError:
        allowed_ = ", ".join(i.value for i in Isolation)
        raise ValueError(f"unknown isolation {name!r}; expected one of: {allowed_}") from None


def _master_epoch(value: str) -> int:
    """`--master-epoch`: a positive integer. argparse echoes the text of a
    `ValueError` from `type=int` (`invalid int value: '<what was typed>'`); a key
    pasted into the wrong option must not reach a terminal."""
    try:
        epoch = int(value)
    except ValueError:
        epoch = 0
    if epoch < 1:
        raise argparse.ArgumentTypeError("takes a positive integer (the value is not shown)")
    return epoch


def _per_role(pairs: list[str]) -> dict[Role, str]:
    """Parses `role=value` pairs into a per-role mapping.

    `all=` sets every role at once, and an explicit role after it wins -- so
    `--role-model all=sonnet --role-model qa=opus` reads the way it looks.
    That combination is the interesting one: the runbook states that "a
    different model in a fresh context is the strongest setup for the
    independent review", and QA is the role whose independence carries the
    acceptance.
    """
    roles = {r.value: r for r in Role}
    out: dict[Role, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"expected role=value, got {pair!r}")
        key, value = (x.strip() for x in pair.split("=", 1))
        if not value:
            raise ValueError(f"empty value in {pair!r}")
        if key == "all":
            out = {r: value for r in Role} | out
        elif key in roles:
            out[roles[key]] = value
        else:
            raise ValueError(
                f"unknown role {key!r} -- known: all, {', '.join(sorted(roles))}"
            )
    return out


def _waiting_for_approval(exc: BaseException) -> bool:
    """Is the run stuck on a permission dialog -- or did it fail?

    The distinction decides whether the role tabs stay open. This used to read
    `"Freigabe" in str(exc)`: a coupling to German prose text produced by
    another module, with no test. `WaitingForApproval` turns that into a
    question of type. The word "blocked" stays as a second trail -- it is
    Herdr's own status value and does not change with the language of the
    source.

    A `PaneNotClean` keeps the tabs open too, by type: the dispatcher refused
    to prompt into a pane it could not prove free, and the pane is what the
    operator has to look at.
    """
    from .controller import PaneNotClean, WaitingForApproval

    return (isinstance(exc, (WaitingForApproval, PaneNotClean))
            or "blocked" in str(exc))


def _kept_open_notice(exc: BaseException) -> str:
    """What the operator is told when the role tabs stay open."""
    from .controller import PaneNotClean

    if isinstance(exc, PaneNotClean):
        return ("The role tabs stay open so the pane can be inspected; nothing "
                "in it was sent. After that: hoh unblock <run-id> and hoh run. "
                "The resumed run checks the pane before any prompt and replaces "
                "a restored pane whose input box is not provably empty by a "
                "fresh agent.")
    return ("The role tabs stay open so that the approval can be granted "
            "in the pane. After that: hoh unblock <run-id> and hoh run.")


def _compare_kept_seals(controller) -> list:
    """Compare the seals that stay in place, and report a touch; never raises.

    A seal that stays (tabs kept open, a tab not confirmed closed, a close or
    a release that failed) is compared at the end of this `hoh run` instead of
    at some later verification that may never come. A touch blocks the run
    (`Controller.compare_open_seals`); the arenas are returned so the run
    exits 1.
    """
    try:
        result = controller.compare_open_seals()
    except Exception as exc:
        print(f"candidate arena seals could not be compared: {exc}", file=sys.stderr)
        return []
    if result.get("refused"):
        print(f"candidate arena seals refused: {result['refused']}", file=sys.stderr)
    touched = list(result.get("touched") or [])
    if touched:
        print("CAPABILITY VIOLATION: a sealed candidate arena was touched while "
              "its seal stayed in place: " + ", ".join(touched), file=sys.stderr)
    return touched


def _close_own_and_release(dispatcher, controller) -> tuple[list, list]:
    """Close this run's own tabs, then lift the QA seals -- in that order only.

    A sealed candidate arena is unsealed only once no pane that could write
    into it may still be open: `close_own()` returned, and nothing it closed
    is left unconfirmed. A failed close releases nothing; a release that
    fails is reported, never raised. Returns the closed tabs and the arenas
    found touched. In the three cases where the seals stay in place (the close
    raised, a tab is not confirmed closed, the release raised) they are still
    compared (`_compare_kept_seals`), so a touch fails this run.
    """
    try:
        closed = dispatcher.close_own()
    except Exception:
        return [], _compare_kept_seals(controller)
    unconfirmed = getattr(dispatcher, "unconfirmed", ()) or ()
    if unconfirmed:
        print(f"The candidate arena seals stay: tabs not confirmed closed: "
              f"{list(unconfirmed)}", file=sys.stderr)
        return closed, _compare_kept_seals(controller)
    try:
        result = controller.release_open_seals()
    except Exception as exc:
        print(f"candidate arenas could not be unsealed: {exc}", file=sys.stderr)
        return closed, _compare_kept_seals(controller)
    if result.get("refused"):
        print(f"candidate arenas not unsealed: {result['refused']}", file=sys.stderr)
    if result.get("kept"):
        print("The candidate arena seals stay: the QA agent is still present: "
              + ", ".join(result["kept"]), file=sys.stderr)
    touched = list(result.get("touched") or [])
    if touched:
        print("CAPABILITY VIOLATION: a sealed candidate arena was touched before "
              "the QA pane closed: " + ", ".join(touched), file=sys.stderr)
    return closed, touched


def _prune(store) -> dict:
    """Apply the retention limit under the run lock; nothing on any failure."""
    try:
        with store.lock():
            return store.prune()
    except Exception:
        return {}


def _end_of_run(store, dispatcher, controller, waiting_for_approval: bool,
                run_id: str) -> tuple[list, dict, list]:
    """Close and release first, then prune.

    The prune runs last, so it never meets an arena that is about to be
    unsealed -- and can file away one that just was.
    """
    # Clear away our own tabs -- otherwise they would be left standing after
    # every run and clutter up the multiplexer the captain works in.
    #
    # Unless a role is still sitting at an unanswered permission dialog. The
    # dogfood run found this on itself on 2026-09-08: the exception route
    # already kept the tabs open for that case, but `_verify` turns exactly
    # that exception into an *outage* -- correctly, because an unanswered QA is
    # a missing verdict and not a failed run. So the blocked dialog took the
    # normal route, and this line closed the one window in which it could have
    # been answered. The protection guarded one route while the traffic took
    # the other.
    closed_tabs: list = []
    touched: list = []
    if waiting_for_approval:
        print(
            "The role tabs stay open: a role is waiting for an approval in "
            "its pane. Answer it there, then: hoh unblock "
            f"{run_id} --reason \"...\" (only if BLOCKED) and hoh run "
            f"{run_id}.",
            file=sys.stderr,
        )
        # The tabs stay open, so the seals stay: compare them now.
        touched = _compare_kept_seals(controller)
    elif hasattr(dispatcher, "close_own"):
        closed_tabs, touched = _close_own_and_release(dispatcher, controller)
    # Apply the retention limit (§7): parked intermediate states and old
    # arenas move to attic/, receipts and evidence never do.
    pruned = _prune(store)
    return closed_tabs, pruned, touched


def _finish_run(store, dispatcher, controller, state, *, waiting_for_approval: bool,
                run_id: str, ran: int) -> tuple[dict, int]:
    """The end of `hoh run`: close, release, prune, and report what is on disk.

    The state is re-read after the release, because the release may have
    blocked the run: printing the in-memory state would report an ACTIVE run
    that is in fact blocked by a capability violation. A touch found at the
    release makes the exit code 1.
    """
    closed_tabs, pruned, touched = _end_of_run(
        store, dispatcher, controller, waiting_for_approval, run_id)
    try:
        state = store.read_state()
    except Exception as exc:
        print(f"the run state could not be re-read, reporting the last one "
              f"in memory: {exc}", file=sys.stderr)
    payload = {
        "iterations_run": ran,
        "closed_tabs": closed_tabs,
        "unconfirmed_open": getattr(dispatcher, "unconfirmed", []),
        "cost": state.cost_disclosure(),
        "rotated": pruned,
        "stage": state.stage.value,
        "condition": state.condition.value,
        "accepted_candidate": state.last_accepted_candidate.candidate_id
        if state.last_accepted_candidate
        else None,
        "endpoints": {
            r.value: dispatcher.endpoint_evidence(r) for r in Role
        },
    }
    if touched:
        payload["capability_violation"] = touched
    return payload, (1 if touched else 0)


def _write_record(path: Path, payload: dict) -> int | None:
    """Write one run record whole or not at all. None: it is in place (a
    directory entry that may not be durable is a warning). 2: it could not be
    written; the old file, if any, is untouched."""
    try:
        durable_write(path, json.dumps(payload, indent=2) + "\n", mode=None)
    except DurableWriteNotSynced as exc:
        print(f"warning: {path.name} is written, but its directory entry may not "
              f"be durable: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"NEEDS_YOU: {path.name} could not be written: {exc}", file=sys.stderr)
        return 2
    return None


def _park_stale_record(path: Path) -> None:
    """Set an earlier record aside under `<name>.v<UTC>.<n>` by a rename (which
    needs no free space; nothing is deleted), so that it can never be read as
    the current one after a write failed."""
    if not os.path.lexists(path):
        return
    from .contracts import utcnow
    stamp = utcnow().replace(":", "-")
    n = 1
    while True:
        aside = path.with_name(f"{path.name}.v{stamp}.{n}")
        if not os.path.lexists(aside):
            break
        n += 1
    try:
        path.rename(aside)
    except OSError as exc:
        print(f"the earlier {path.name} could not be set aside either: {exc}",
              file=sys.stderr)


def _integrity_close(store, before) -> bool:
    """Verify the inventory after the roles ran. False when the rule broke.

    A violation fails the run with the exact paths and is never repaired
    first: the run that deleted something stays the run on record as having
    done it.
    """
    if before is None:
        return True
    from . import integrity
    problems = integrity.verify(before)
    record = {"held": not problems, "violations": problems,
              "checked": ["pre-existing tracked files", "refs", "own branch history",
                          "other worktrees"]}
    approval = store.dir / "approval"
    try:
        approval.mkdir(parents=True, exist_ok=True)
        durable_write(approval / "integrity.json",
                      json.dumps(record, indent=2) + "\n", mode=None)
    except DurableWriteNotSynced as exc:
        print(f"warning: integrity.json is written, but its directory entry may "
              f"not be durable: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"the integrity record could not be written: {exc}", file=sys.stderr)
        _park_stale_record(approval / "integrity.json")
        if not problems:
            return False
    if not problems:
        return True
    print("INTEGRITY VIOLATION -- the house rule was broken by this run:",
          file=sys.stderr)
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    try:
        with store.lock():
            fresh = store.read_state()
            fresh.note("INTEGRITY VIOLATION: " + "; ".join(problems)[:900])
            try:
                stages.fail(fresh, "integrity violation: " + problems[0][:200])
            except stages.TransitionError:
                pass    # the note above is on the record either way
            store.write_state(fresh)
    except Exception as exc:
        print(f"could not record the violation in the run state: {exc}", file=sys.stderr)
    return False


def cmd_run(args) -> int:
    """`hoh run`, with a process marker around the whole of it (review B3, C):
    a replacement master can tell a run still being executed between two
    iterations -- when its lock is free -- from one whose process is gone."""
    store = RunStore(args.root, args.run_id)
    if not store.state_exists():
        return _cmd_run(args)
    try:
        with store.process_marker():
            return _cmd_run(args)
    except LockBusy as exc:
        if "already being executed" not in str(exc):
            raise
        print(str(exc), file=sys.stderr)
        return 2


def _cmd_run(args) -> int:
    """Runs iterations. The operator path that was missing until now.

    Without this command `hoh start` created a run that stayed in NEW:
    `run_iteration` was called from tests only, which left A01/A02/A12
    impossible to demonstrate.
    """
    from .controller import Controller
    from .dispatchers import build_dispatcher

    store = _store(args)
    try:
        state = store.read_state()
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # STORAGE-B3: storage admission is real wiring, decided before anything
    # else is written or dispatched -- an uninitialised root, WSL without the
    # host volume or an unusable ledger refuses the start (fail closed, never
    # skipped), and the boot is claimed before the first admission.
    try:
        storage_admission, storage_root = storage_admission_for(
            getattr(args, "storage_root", None))
    except StorageRefused as exc:
        unpersisted = ""
        if str(exc).startswith(STORAGE_UNAVAILABLE):
            try:
                from .storage_park import stop_needs_you

                with store.lock():
                    fresh = store.read_state()
                    stop_needs_you(fresh, store, str(exc)[:400])
            except Exception as persist_exc:              # noqa: BLE001 - said, not swallowed
                # The run could not record its own stop. Said on the same line,
                # which still names the refusal, so the launcher's mapping to
                # STORAGE_UNAVAILABLE holds and a person sees both facts.
                unpersisted = (f" (the NEEDS_YOU state could not be persisted: "
                               f"{type(persist_exc).__name__}: {persist_exc})")
        print(f"NEEDS_YOU: {exc}{unpersisted}", file=sys.stderr)
        return 2
    _print({"storage_admission": {"state_root": str(storage_root),
                                  "ledger": str(storage_admission.ledger)}})

    profile = {
        Role.PLANNER: args.planner,
        Role.DEVELOPER: args.developer,
        Role.QA: args.qa,
    }
    try:
        models = _per_role(args.role_model)
        efforts = _per_role(args.role_effort)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if models or efforts:
        _print({
            "role_models": {r.value: m for r, m in models.items()},
            "role_efforts": {r.value: e for r, e in efforts.items()},
            "note": "recorded here because a verdict is only comparable when "
                    "it is known which model produced it",
        })
    # P1-09. Both decided before any role starts, both written into the run
    # directory, so the record says what the agents could do and which
    # directory was trusted on whose instruction.
    policy = None
    approval_dir = store.dir / "approval"
    if getattr(args, "approval_policy", None):
        from .approval_policy import PolicyRefused, load
        try:
            policy = load(args.approval_policy)
        except PolicyRefused as exc:
            print(f"approval policy refused: {exc}", file=sys.stderr)
            return 2
        approval_dir.mkdir(parents=True, exist_ok=True)
        if (rc := _write_record(approval_dir / "policy.json", policy.evidence())) is not None:
            return rc
        _print({"approval_policy": {"mode": policy.mode, "digest": policy.digest,
                                    "denied": len(policy.deny),
                                    "source": policy.source}})
    # P2-24. Pinned once and read back on every later call: a resumed run obeys
    # the contract it began under, and a different one offered later is refused
    # rather than adopted.
    governor = None
    if True:  # noqa: SIM108 - kept as a block so the imports stay local
        from .governance import ContractRefused, bind

        offered = None
        if getattr(args, "autonomy", None):
            from .autonomy import AutonomyContract

            try:
                offered = AutonomyContract.model_validate_json(
                    Path(args.autonomy).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"autonomy contract refused: {exc}", file=sys.stderr)
                return 2
        try:
            governor = bind(store.dir, contract=offered,
                            program_epoch=getattr(args, "program_epoch", None))
        except ContractRefused as exc:
            print(f"autonomy contract refused: {exc}", file=sys.stderr)
            return 2
        except DiskFullError as exc:
            print(f"NEEDS_YOU: the autonomy contract could not be pinned: {exc}",
                  file=sys.stderr)
            return 2
        if governor is not None:
            asked = getattr(args, "program_epoch", None)
            if asked is not None and governor.contract.program_epoch != asked:
                print(f"--program-epoch {asked} ignored: this run is pinned at epoch "
                      f"{governor.contract.program_epoch}", file=sys.stderr)
            approval_dir.mkdir(parents=True, exist_ok=True)
            if (rc := _write_record(approval_dir / "autonomy.json",
                                    governor.evidence())) is not None:
                return rc
            _print({"autonomy_contract": governor.evidence()})
    # P2-43b. The declaration is validated here, before anything is written
    # and before any role starts; it is *recorded* just before the controller
    # is built, under the run's lock (below). Every message is fixed text: a
    # pasted key must not reach a terminal or a log.
    from . import dispatchrecovery

    try:
        recovery = dispatchrecovery.RecoveryContext(
            accounts=dispatchrecovery.parse_role_accounts(
                getattr(args, "role_account", None) or []))
        dispatchrecovery.check_master_args(getattr(args, "master_session", None),
                                           getattr(args, "master_epoch", None))
    except ValueError as exc:
        print(f"recovery declaration refused: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "trust_worktree", False):
        from .trust import TrustRefused, trust_run_worktree
        try:
            granted = trust_run_worktree(state.repo_path)
        except TrustRefused as exc:
            print(f"worktree trust refused: {exc}", file=sys.stderr)
            return 2
        approval_dir.mkdir(parents=True, exist_ok=True)
        if (rc := _write_record(approval_dir / "worktree_trust.json", granted)) is not None:
            return rc
        _print({"worktree_trust": granted})
    try:
        dispatcher = build_dispatcher(
            answers_dir=store.dir / "answers",
            profiles=profile,
            cwd=state.repo_path,
            prefer_herdr=not args.no_herdr,
            timeout_ms=args.role_timeout * 1000,
            models=models,
            efforts=efforts,
            approval_policy=policy,
        )
    except Exception as exc:   # DispatchError and HerdrUnavailable
        print(str(exc), file=sys.stderr)
        return 2

    if args.no_herdr:
        print(
            "NOTE: started without Herdr. No Herdr endpoint is created, so "
            "this run does NOT count as evidence for A01/A02/A12.",
            file=sys.stderr,
        )

    try:
        isolation = _isolation_for_run(state, args.isolation)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isolation is not None:
        # Resolve the backend *before* any role is dispatched. Discovering at
        # the first acceptance check that the machine cannot provide the
        # isolation asked for means an entire planner and developer round has
        # already been spent on a run that can only end INCONCLUSIVE.
        from .sandbox import SandboxUnavailable, select

        try:
            backend = select(isolation)
        except SandboxUnavailable as exc:
            print(
                f"--isolation {args.isolation} was requested and cannot be "
                f"provided here: {exc}\n"
                "Nothing has been dispatched. Ask for --isolation none "
                "explicitly if you accept running unsandboxed.",
                file=sys.stderr,
            )
            return 2
        _print({
            "isolation": isolation.value,
            "backend": backend.name,
            "note": "every acceptance check in this run, candidate and "
                    "baseline alike, executes under this isolation",
        })

    # Persisted here, before a single role is dispatched. The guard that used
    # to stand here compared `state.isolation` against the value
    # `_isolation_for_run` had *just assigned to it*, so it was never true and
    # the write never happened. In the happy path the value reached disk by
    # accident, on the controller's first checkpoint; anything that stopped
    # before that -- an unreadable spec, a stop request, `--iterations 0` --
    # left the run recorded as unsandboxed, and the next `hoh run` continued it
    # that way without a word.
    wanted = isolation.value if isolation is not None else "none"
    if state.isolation != wanted:
        state.isolation = wanted
        with store.lock():
            store.write_state(state)

    # The captain's standing rule, measured rather than trusted: an inventory
    # of what the roles must leave alone, taken before the first role starts.
    from . import integrity
    try:
        integrity_before = integrity.snapshot(state.repo_path)
    except RuntimeError as exc:
        integrity_before = None
        print(f"integrity inventory not measurable here: {exc}", file=sys.stderr)
    if integrity_before is not None:
        (store.dir / "approval").mkdir(parents=True, exist_ok=True)
        if (rc := _write_record(store.dir / "approval" / "integrity-before.json",
                                integrity_before)) is not None:
            return rc

    # P2-43b: who this run works for, recorded under the run's lock after every
    # other refusal. A bare run records `master: NONE` -- stated, not absent.
    try:
        with store.lock():
            master = dispatchrecovery.declare_master(
                store.dir, session=getattr(args, "master_session", None),
                epoch=getattr(args, "master_epoch", None))
    except LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"recovery declaration refused: {exc}", file=sys.stderr)
        return 2
    _print({"recovery": {"master": master.model_dump(),
                         "accounts": {r.value: h for r, h in recovery.accounts.items()},
                         "route_policy": f"{dispatchrecovery.ROUTE_POLICY_ID}/"
                                         f"{dispatchrecovery.ROUTE_POLICY_VERSION}"}})

    controller = Controller(
        store, dispatcher, spec_path=state.spec_path, isolation=isolation,
        storage_admission=storage_admission,
        steward=_hook_steward(storage_root, state.project_name or state.run_id,
                              storage_admission),
        recovery=recovery,
    )

    ran = 0
    waiting_for_approval = False
    while ran < args.iterations:
        try:
            out = controller.run_iteration(state)
        except LockBusy as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            # Handoff §8: end bounded and visibly, not with a traceback.
            with store.lock():
                fresh = store.read_state()
                # P2-24: what a block means is the contract's decision, not a
                # constant. Without one this is exactly the old behaviour.
                from .governance import ContractRefused, bind, block_under_contract

                try:
                    governor = bind(store.dir)
                except ContractRefused as contract_exc:
                    # The block is written whatever the contract file says: a
                    # handler whose job is to end bounded must not be the place
                    # a contract problem surfaces.
                    print(f"autonomy contract unreadable, blocking without it: "
                          f"{contract_exc}", file=sys.stderr)
                    governor = None
                # The exception's text is not ours: it carries a role's pane and a
                # role's words. It is shown, never read as a quota statement.
                routing = block_under_contract(
                    fresh, f"iteration aborted: {exc}", governor,
                    scan_for_quota=False)
                store.write_state(fresh)
                if routing is not None:
                    _print({"autonomy": {"handling": routing.handling.value,
                                         "kind": routing.kind,
                                         "blocks_branch": routing.blocks_branch,
                                         "why": routing.why}})
            print(f"Iteration aborted and run blocked: {exc}", file=sys.stderr)
            # HoH does **not** clear away a pending dialog. The error path used
            # to close its own tabs even when an agent was waiting for an
            # approval -- which removed exactly the window in which the captain
            # could have answered, and afterwards the reason for the block was
            # no longer determinable.
            waiting = _waiting_for_approval(exc)
            if waiting:
                print(_kept_open_notice(exc), file=sys.stderr)
            elif hasattr(dispatcher, "close_own"):
                _close_own_and_release(dispatcher, controller)
            _integrity_close(store, integrity_before)
            return 1

        ran += 1
        if state.resume_attempts:
            # An iteration that actually ran is the proof that the quota is
            # back. The counter measures consecutive unsuccessful attempts.
            state.resume_attempts = 0
        waiting_for_approval = waiting_for_approval or out.waiting_for_approval
        label = "accepted" if out.accepted else "not accepted"
        print(f"Iteration {out.iteration}: {label} -- {out.reason}")
        undecided = _inconclusive_checks(store, state, out.iteration)
        if undecided and not out.accepted:
            # "Not accepted" is true and useless when the reason is that
            # nothing ran. An iteration whose criteria were refused by the
            # guard, timed out, or could not be isolated has measured nothing
            # about the product -- and a transcript that renders that as a
            # product rejection is the laundering this project exists to stop.
            # A reviewer read exactly that off a real run's log.
            print(
                f"  {len(undecided)} of these did not run at all "
                f"(INCONCLUSIVE, not a verdict): "
                + ", ".join(f"{cid} [{why}]" for cid, why in undecided)
            )

        if state.condition is not Condition.ACTIVE:
            print(f"Run is now {state.condition.value}: {state.stop_reason or ''}")
            break
        if out.accepted and args.until_accepted:
            break

    if not _integrity_close(store, integrity_before):
        return 3

    # Clear away our own tabs -- otherwise they would be left standing after
    # every run and clutter up the multiplexer the captain works in. Then lift
    # the QA seals, then prune (`_end_of_run`), and report the state on disk.
    payload, rc = _finish_run(store, dispatcher, controller, state,
                              waiting_for_approval=waiting_for_approval,
                              run_id=args.run_id, ran=ran)
    _print(payload)
    return rc


def cmd_agent_setup(args) -> int:
    """`hoh agent-setup`: print, or install into a named directory, the text that tells a
    coding-agent session to use VeriHarness. Starts nothing; writes only new files."""
    from . import agent_setup as setup

    kinds = ["skill", "snippet"] if args.kind == "all" else [args.kind]
    try:
        if args.print_text:
            if args.kind == "all":
                raise setup.SetupRefused("--print shows one kind at a time: skill or snippet")
            sys.stdout.write(setup.render(args.kind, args.delegation))
            return 0
        written = setup.install(args.install, kinds, args.delegation,
                                allow_user_config=args.allow_user_config)
    except setup.SetupFailed as exc:
        print(f"agent-setup failed: {exc}", file=sys.stderr)
        return 2
    except setup.SetupRefused as exc:
        print(f"agent-setup refused: {exc}", file=sys.stderr)
        return 2
    _print({
        "written": [str(p) for p in written],
        "note": "files only; nothing was started and no existing file was changed",
        "next": ("a skill is loaded by a harness that reads SKILL.md directories from the "
                 "directory you chose; the snippet is a file to include from, or paste into, "
                 "the project's CLAUDE.md or AGENTS.md (this command never edits those)"),
    })
    return 0


def cmd_deliver(args) -> int:
    """The transition from the internal checkpoint to delivery.

    Up to here `delivery_mode` and `yolo` were fields without effect. Now they
    decide -- and `READY_FOR_DELIVERY` becomes reachable at all.
    """
    from .delivery import DeliveryRefused, deliver

    store = _store(args)
    try:
        with store.lock():
            state = store.read_state()
            if stages.violation_unresolved(state):
                # The first check, before the stage, the open seals, the contract
                # and any git operation: whatever the condition, a pause or a
                # cancel keeps the hold, and only a person's `hoh unblock` lifts
                # it (run qaw7: a held run was fast-forwarded).
                reason = (state.blocked_reason or state.stop_reason or "").splitlines()
                print("Delivery refused: the run is held by a capability violation "
                      f"({state.condition.value}): {reason[0][:300] if reason else ''}\n"
                      f"After investigating: hoh unblock {args.run_id} --reason \"...\"",
                      file=sys.stderr)
                return 2
            if state.stage is not Stage.CHECKPOINTED:
                print(
                    f"Stage is {state.stage.value}. Only an accepted "
                    "checkpoint is delivered.",
                    file=sys.stderr,
                )
                return 2
            from .controller import open_seal_arenas

            still_open = open_seal_arenas(store.dir)
            if still_open:
                # A sealed arena has not been compared and released yet: its
                # touch, if any, is not known. A later `hoh run` that closes
                # the tabs releases it.
                print("Delivery refused: a candidate arena sealed for QA is still "
                      f"open: {', '.join(still_open)}. A later 'hoh run' that "
                      "closes the tabs releases it.", file=sys.stderr)
                return 2

            from .governance import (
                ContractRefused, bind, forbidden_outright, may_release, record_override,
            )

            try:
                governor = bind(store.dir)
            except ContractRefused as exc:
                print(f"autonomy contract refused: {exc}", file=sys.stderr)
                return 2
            routing = may_release(governor, what=f"deliver {args.run_id} into {args.into}",
                                  mode=state.delivery_mode)
            if routing is not None and routing.blocks_branch:
                # The contract is consulted whether or not a human passed
                # --approve. A reviewer measured the previous shape: with
                # --approve the contract was skipped, without it delivery
                # already refused anyway, so the gate could not change a single
                # outcome -- correct code that decided nothing.
                if forbidden_outright(routing) or not args.approve:
                    print(f"Delivery refused by the run's autonomy contract "
                          f"({routing.handling.value}): {routing.why}", file=sys.stderr)
                    _print({"autonomy": {"handling": routing.handling.value,
                                         "kind": routing.kind, "why": routing.why,
                                         "overridable": not forbidden_outright(routing),
                                         "contract": governor.contract.digest()}})
                    return 2
                # A human overrode a gate the contract has. Allowed, and
                # recorded as an override -- the log used to say "parked" while
                # the release happened.
                record_override(governor, routing=routing, by="--approve",
                                what=f"deliver {args.run_id} into {args.into}")
                state.note(f"autonomy: --approve overrode {routing.handling.value} "
                           f"for {routing.kind}")
                # Written now: a later DeliveryRefused returns without saving,
                # and the override belongs in the state whether or not the
                # delivery went on to succeed.
                store.write_state(state)
            try:
                result = deliver(
                    repo=state.repo_path,
                    mode=state.delivery_mode,
                    yolo=state.yolo,
                    approved=args.approve,
                    target_branch=args.into,
                    candidate_id=state.last_accepted_candidate.candidate_id
                    if state.last_accepted_candidate
                    else None,
                    # Binds delivery to the **content** of the accepted
                    # candidate, not just to its name.
                    tree_digest=state.last_accepted_candidate.tree_digest
                    if state.last_accepted_candidate
                    else None,
                )
            except DeliveryRefused as exc:
                print(f"Delivery refused: {exc}", file=sys.stderr)
                return 2

            stages.transition(
                state, Stage.READY_FOR_DELIVERY, reason=result.description
            )
            store.write_state(state)
    except (StoreError, LockBusy, stages.TransitionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _print(
        {
            "delivered": result.delivered,
            "mode": state.delivery_mode,
            "yolo": state.yolo,
            "branch": result.branch,
            "target": result.target,
            "description": result.description,
            "stage": state.stage.value,
        }
    )
    return 0


def cmd_backup(args) -> int:
    store = _store(args)
    try:
        store.read_state()
        path = store.backup(args.out)
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print({"backup": str(path), "size_bytes": path.stat().st_size})
    return 0


def cmd_restore(args) -> int:
    store = _store(args)
    try:
        target = store.restore(args.archive, overwrite=args.overwrite)
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"NEEDS_YOU: the restore did not complete: {exc}", file=sys.stderr)
        return 2
    _print({"restored_to": str(target), "run_id": target.name})
    return 0


def cmd_report(args) -> int:
    """Report/status show at least: role, iteration, candidate, last accepted
    state, open checks, budget usage, last progress and blocked reason
    (Handoff §7)."""
    store = _store(args)
    try:
        state = store.read_state()
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    evidence = store.read_evidence(expect_digest=state.evidence_digest)

    lines = [
        f"# HoH report -- {state.run_id}",
        "",
        f"- Project: {state.project_name} ({state.repo_path})",
        f"- State: {state.stage.value} / {state.condition.value}",
        f"- Iteration {state.iteration}, attempt {state.attempt}",
        f"- Working candidate: "
        f"{state.working_candidate.candidate_id if state.working_candidate else '--'}",
        f"- Last accepted candidate: "
        f"{state.last_accepted_candidate.candidate_id if state.last_accepted_candidate else '--'}",
        f"- Delivery: mode={state.delivery_mode}, yolo={state.yolo}",
        f"- Last progress: {state.updated_at}",
        f"- Blocked reason: {state.blocked_reason or '--'}",
        "",
        "## Open checks",
    ]
    open_items = evidence.to_repair() + evidence.insufficient()
    lines += [f"- {i.item_id} [{i.status.value}]: {i.claim}" for i in open_items] or ["- none"]
    lines += ["", "## Validated (preservation requirements)"]
    lines += [f"- {i.item_id}: {i.claim}" for i in evidence.to_preserve()] or ["- none"]
    lines += [
        "",
        "## Budget",
        f"- Iterations: {state.usage.iterations}/{state.budgets.max_iterations}",
        f"- Loops without progress: {state.usage.loops_without_progress}/"
        f"{state.budgets.max_loops_without_progress}",
        f"- Schema repairs: {state.usage.schema_repairs}",
        f"- Transient retries: {state.usage.transient_retries}",
        "",
        "## Receipts",
        f"- {len(store.receipts())} receipts, {len(store.results())} role results",
        f"- Evidence: {state.evidence_ref or '--'} (digest {state.evidence_digest or '--'})",
        f"- Parked states: {len(store.parked_states())}",
    ]
    text = "\n".join(lines)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(text)
    return 0


def cmd_timers(args) -> int:
    """What this root is waiting for, and what is late.

    A programme that waits is not a programme that has stopped, and the
    difference has to be readable without attaching to anything: every wait is
    a row here, with what it is for and when it is due.
    """
    from .sentinel import stuck_timers
    from .timers import TimerStore

    store = TimerStore(args.root)
    try:
        timers = store.read()
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        print(f"the timer store under {args.root} is not readable: {exc}", file=sys.stderr)
        return 2
    if not timers:
        print(f"no timers under {args.root}")
        return 0
    for t in timers:
        print(f"{t.timer_id}  {t.status.value:8}  due {t.due_at}  {t.kind.value}"
              + (f"  run={t.run_id}" if t.run_id else "")
              + (f"  {t.reason}" if t.reason else ""))
    late = stuck_timers(timers)
    for s_ in late:
        print(f"LATE: {s_}", file=sys.stderr)
    return 1 if late else 0


def cmd_sentinel(args) -> int:
    """What is due now, and what should happen about it.

    Proposals, not actions: this command is the replaceable wake-up -- a cron,
    a plugin or the next session can be what runs it -- and acting on a
    proposal is the controller's business, because that is where the lock and
    the policy are.
    """
    from .sentinel import sweep
    from .timers import TimerStore

    out = sweep(TimerStore(args.root), program_epoch=args.program_epoch,
                session_epoch=args.session_epoch)
    _print({
        "at": out.at,
        "due": [{"timer": p.timer.timer_id, "kind": p.timer.kind.value,
                 "run_id": p.timer.run_id, "action": p.action, "why": p.why}
                for p in out.due],
        "refused": [{"timer": t.timer_id, "why": why} for t, why in out.refused],
        "defects": out.defects,
    })
    return 0


def cmd_stuck(args) -> int:
    """The never-stuck invariant, measured over every run under this root.

    Every non-terminal object must have a next action, a retry time or a named
    human owner. One with none of the three is a control-plane defect, and this
    is the command that finds it instead of waiting for somebody to notice that
    nothing has moved.
    """
    from .projectstore import ProjectStore, list_projects
    from .sentinel import stuck_nodes, stuck_run, stuck_timers
    from .timers import TimerStore

    try:
        timers = TimerStore(args.root).read()
    except Exception:                             # noqa: BLE001 - absent or unreadable
        timers = []
    found = list(stuck_timers(timers))
    for rid in list_runs(args.root):
        try:
            state = RunStore(args.root, rid).read_state()
        except StoreError as exc:
            print(f"{rid}  UNREADABLE: {exc}", file=sys.stderr)
            continue
        found += stuck_run(state, timers=timers)
    for project in list_projects(args.root):
        try:
            found += stuck_nodes(ProjectStore(args.root, project).read_state(), timers=timers)
        except StoreError as exc:
            print(f"{project}  UNREADABLE: {exc}", file=sys.stderr)
    for defect in found:
        print(str(defect))
    if not found:
        print("nothing is stuck: every non-terminal object has a way out")
    return 1 if found else 0


def cmd_decision(args) -> int:
    """`hoh decision check MAP [MAP ...]` (P2-21): the decision graph fsck.

    Each MAP is a `DecisionMap` as JSON, exactly what `DecisionMap.model_dump(mode="json")`
    writes; several files form one graph (the cross-map checks need that). Read-only:
    the inputs are read and nothing is written, except `--receipt FILE`, a new file
    that is never replaced. Prints the `CheckResult` (the receipt: status, errors,
    warnings, checked_generation, decision_graph_digest, validator_version) as JSON.

    Exit 0: PASS (warnings never fail). Exit 1: FAIL, at least one ERROR finding.
    Exit 2: refused (argparse usage errors included: they print a usage line, not
    `decision check refused:`) -- an input that is not a DecisionMap, a malformed option, a
    receipt that cannot be created, a checker that raised. A graph the models refuse
    to construct (a dangling `blocked_by`, a cycle) is a refusal, not a FAIL: it cannot
    be represented, so it cannot be checked. Nothing is printed to stdout on a refusal.

    What this is not: it does not discover, locate or persist a decision graph (the
    product keeps none; a MAP file is the model's own JSON, no new format), it is wired
    into no run, controller, gate or readiness path, and `check` is structure, not truth.
    """
    from datetime import datetime

    from pydantic import TypeAdapter, ValidationError

    from .decisioncheck import VALIDATOR_VERSION, ProgramRef, check
    from .decisions import DecisionMap
    from .durable_io import durable_create

    def refuse(message: str) -> int:
        print(f"decision check refused: {message}", file=sys.stderr)
        return 2

    def read(path: str) -> str:
        return Path(path).expanduser().read_text(encoding="utf-8")

    def why(exc: Exception) -> str:
        if not isinstance(exc, ValidationError):
            return str(exc) or repr(exc)
        errors = exc.errors()
        message = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'map'}: {e['msg']}"
                            for e in errors[:3])
        return message + (f"; and {len(errors) - 3} more" if len(errors) > 3 else "")

    now = None
    if args.now is not None:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            return refuse(f"--now {args.now!r} is not an ISO 8601 instant")
        if now.tzinfo is None:
            return refuse(f"--now {args.now!r} has no timezone offset; an instant without a "
                          f"zone is not an instant")
    maps = []
    for path in args.maps:
        try:
            maps.append(DecisionMap.model_validate_json(read(path)))
        except (OSError, ValueError, RuntimeError) as exc:   # ValidationError, UnicodeDecodeError,
            # and expanduser() on an unknown ~user
            return refuse(f"{path}: {why(exc)}")
    refs: list = []
    if args.program_refs is not None:
        try:
            refs = TypeAdapter(list[ProgramRef]).validate_json(read(args.program_refs))
        except (OSError, ValueError, RuntimeError) as exc:
            return refuse(f"{args.program_refs}: {why(exc)}")
    try:
        result = check(maps, validator_version=VALIDATOR_VERSION, program_refs=refs,
                       settled_refs=args.settled_ref, now=now)
    except Exception as exc:                      # noqa: BLE001 - never read as a FAIL
        return refuse(f"the checker itself failed: {type(exc).__name__}: {exc}")
    text = json.dumps(result.model_dump(mode="json"), indent=2) + "\n"
    if args.receipt is not None:
        try:
            durable_create(Path(args.receipt).expanduser(), text)
        except (OSError, ValueError, RuntimeError) as exc:
            return refuse(f"--receipt {args.receipt}: {exc}")
    sys.stdout.write(text)
    return 1 if result.errors else 0


def cmd_list(args) -> int:
    runs = list_runs(args.root)
    if not runs:
        print(f"no runs under {args.root}")
        return 0
    for rid in runs:
        try:
            st = RunStore(args.root, rid).read_state()
            print(f"{rid}  {st.stage.value}/{st.condition.value}  it={st.iteration}  {st.project_name}")
        except StoreError as exc:
            print(f"{rid}  UNREADABLE: {exc}")
    return 0


def cmd_migrate(args) -> int:
    """`hoh migrate [--apply]`: bring the durable state under --root to the
    current schema version (P2-12).

    The records are the one state file each run (`<id>/state.json`) and each
    project (`projects/<id>/project.json`) keeps -- the files the two stores
    read through the version gate. A dry run is the default and writes nothing,
    not even a lock file; `--apply` takes each store's own lock, refuses a run
    that a process is executing, parks the previous bytes beside the original as
    `<name>.v<old>.<UTC>` and replaces the record atomically
    (`migrations.migrate`). Nothing is deleted. The plan is made over every
    record first: one refusal (a record from the future, without a readable
    version, unparseable in any way -- including nesting or digits no parser
    will take) and nothing at all is applied.

    A run or project directory, the `projects` directory, a record file or a
    lock file that is a symbolic link, or whose resolved location leaves the
    root, is a refusal (`migrations.confine`), at the plan and again just before
    each store's locks are taken. Hard links and a same-user race between that
    re-check and the open are not closed.
    The run's `process.json` is never rewritten: the apply holds the run's
    `.process.lock` (`RunStore.process_lock`) and writes no marker, so the
    marker a crashed run left behind stays as it was.

    Exit 0 = every record is at the current version or was migrated; 1 = at
    least one record is refused; 2 = the command could not run (unusable --now,
    a run being executed, a store locked).

    What this is not: it changes no schema and no version number, registers no
    migration step itself (the steps live in `migrations.STATE_REGISTRIES`: none
    for runs, the project step of P2-15), does not migrate on read (a state behind the current version is refused with this
    command named), and does not touch amendments, goalbooks, timers, epochs,
    capsules or any string-versioned record. The controller and the gates do not
    run it.
    """
    import contextlib
    from datetime import datetime, timezone

    from . import migrations
    from .projectstore import ProjectStore, list_projects

    def stop(reason: str) -> int:
        print(f"migrate refused: {reason}", file=sys.stderr)
        return 2

    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            return stop(f"--now is not an ISO 8601 instant: {args.now!r}")
        if now.tzinfo is None:
            return stop(f"--now needs an offset (for example Z): {args.now!r}")
    else:
        now = datetime.now(timezone.utc)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        # A root that is not there answers about nothing; so does a typo.
        return stop(f"--root is not a directory: {root}")

    def owned(kind, store):
        """Every path an apply opens or writes for this store: all of them must stay
        inside the root, with no symbolic link on the way (`migrations.confine`)."""
        paths = [store.dir, store.state_path, store.lock_path]
        if kind == "run":
            paths.append(store.process_lock_path)
        return paths

    refused: list[dict] = []
    targets = []
    for kind, ids, store_class in (("run", list_runs(root), RunStore),
                                   ("project", list_projects(root), ProjectStore)):
        for ident in ids:
            try:
                store = store_class(root, ident)
                migrations.confine(store.root, *owned(kind, store))
                targets.append((kind, ident, store))
            except (StoreError, migrations.MigrationError) as exc:
                # a directory the store would not name, or a link out of the root
                refused.append({"kind": kind, "id": ident,
                                "file": migrations.STATE_FILES[kind], "reason": str(exc)})

    def run_one(kind, store, *, dry_run):
        return migrations.migrate(
            store.dir, registry=migrations.STATE_REGISTRIES[kind], now=now,
            dry_run=dry_run, only=[migrations.STATE_FILES[kind]])

    def line(kind, ident, entry) -> dict:
        return {"kind": kind, "id": ident, "file": entry.file,
                "direction": entry.direction.name,
                "from_version": entry.from_version, "to_version": entry.to_version,
                "steps": list(entry.steps), "digest_before": entry.digest_before,
                "digest_after": entry.digest_after, "preserved_to": entry.preserved_to}

    records: list[dict] = []
    forward: list[tuple[str, str, object]] = []
    for kind, ident, store in targets:
        try:
            report = run_one(kind, store, dry_run=True)
        except migrations.PlanRefused as exc:
            refused += [{"kind": kind, "id": ident, "file": str(r.key),
                         "reason": r.reason} for r in exc.plan.refusals]
            continue
        except (migrations.MigrationError, OSError, *migrations.UNPARSEABLE) as exc:
            refused.append({"kind": kind, "id": ident,
                            "file": migrations.STATE_FILES[kind], "reason": str(exc)})
            continue
        for entry in report.files:
            records.append(line(kind, ident, entry))
            if entry.direction is migrations.Direction.FORWARD:
                forward.append((kind, ident, store))

    applied = False
    if args.apply and forward and not refused:
        for kind, ident, store in forward:
            if kind == "run" and store.running_process() is not None:
                return stop(f"run {ident} is being executed by a process; nothing was migrated")
        done: list[str] = []
        records = [r for r in records if r["direction"] != "FORWARD"]
        try:
            for kind, ident, store in forward:
                # The plan was made a moment ago; nothing may have become a link since.
                migrations.confine(store.root, *owned(kind, store))
                with contextlib.ExitStack() as held:
                    held.enter_context(store.lock())
                    if kind == "run":
                        # Holds the run's `.process.lock` for the write: a `hoh run`
                        # starting now is refused (LockBusy) instead of reading a
                        # state half-way. The pre-check above is for all-or-nothing;
                        # this is the exclusion. Only the lock: the run's
                        # `process.json` -- the marker a crashed run left behind
                        # included -- is not rewritten, so nothing is replaced
                        # unparked. A project has no liveness marker; its
                        # orchestrator lock is the only guard.
                        held.enter_context(store.process_lock())
                    report = run_one(kind, store, dry_run=False)
                records += [line(kind, ident, e) for e in report.files]
                done.append(f"{kind} {ident}")
        except (LockBusy, migrations.MigrationError, OSError, StoreError) as exc:
            return stop(f"{exc}; already migrated: {done or 'none'} (rerun resumes)")
        applied = True

    _print({"dry_run": not args.apply, "applied": applied, "root": str(root),
            "to_versions": {k: r.current_version
                            for k, r in migrations.STATE_REGISTRIES.items()},
            "records": sorted(records, key=lambda r: (r["kind"], r["id"])),
            "refused": refused,
            "counts": {"records": len(records),
                       "forward": sum(r["direction"] == "FORWARD" for r in records),
                       "refused": len(refused)}})
    return 1 if refused else 0


# --------------------------------------------------------------------------- #
# The project layer: which runs happen at all
# --------------------------------------------------------------------------- #


def cmd_project(args) -> int:
    """The orchestration layer above a single run.

    `status` and `resume` answer the question a returning session actually has
    -- what does this state say to do next -- from the persisted file alone.
    `drive` makes the calling session the governed master and runs the
    controller; it refuses unless the project registers a driver
    (`driver.json`), because a command that *looks* like it orchestrates while
    guessing how to launch runs would be worse than none. `handoff` and
    `authority` are master rollover and its read-only report. On a governed
    project, `unblock` and `record-action` need the current master's session
    and epoch (AC7).
    """
    from .projectstore import (
        ProjectStore, list_projects, record_external_action, resume_decision, unblock,
    )

    if args.project_cmd == "list":
        projekte = list_projects(args.root)
        if not projekte:
            print(f"no projects under {args.root}")
            return 0
        for pid in projekte:
            try:
                st = ProjectStore(args.root, pid).read_state()
                verdict_text, _ = resume_decision(st)
                open_ = sum(1 for n in st.nodes if not n.settled)
                print(f"{pid}  {verdict_text:9s}  nodes={len(st.nodes)} open={open_} "
                      f"closure_gen={st.closure_generation}")
            except StoreError as exc:
                print(f"{pid}  UNREADABLE: {exc}")
        return 0

    store = ProjectStore(args.root, args.project_id)
    if not store.exists():
        print(f"project {args.project_id} has no state under {args.root}", file=sys.stderr)
        return 2
    try:
        st = store.read_state()
    except StoreError as exc:
        # A damaged state blocks rather than being replaced, and the exit code
        # says so: a reader scripting against this must not see 0.
        print(f"UNREADABLE: {exc}", file=sys.stderr)
        return 3

    if args.project_cmd == "authority":
        from .succession import status as authority_status

        print(json.dumps(authority_status(store.dir), indent=2))
        return 0

    if args.project_cmd == "drive":
        return _project_drive(args, store, st)

    if args.project_cmd == "hold":
        return _project_hold(args, store, st)

    if args.project_cmd == "rollover":
        return _project_rollover(args, store, st)

    if args.project_cmd == "rollover-verify":
        return _project_rollover_verify(args, store, st)

    if args.project_cmd == "rollover-accept":
        return _project_rollover_accept(args, store, st)

    if args.project_cmd == "handoff":
        authority, refusal = _claimed_master(args, store)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 5
        from .succession import LeaseError, StaleMaster

        try:
            lease = authority.handoff(args.to, reason=args.reason)
        except (LeaseError, StaleMaster) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 5
        print(f"epoch {lease.master_epoch} handed over by {lease.leader_session_id} "
              f"to {lease.successor}; {args.session} may no longer write")
        return 0

    # An authoritative command on a governed project needs authority: the
    # live master's own credential, or -- when no master is alive -- an
    # operator epoch of its own (review A, P5: otherwise an orderly-halted
    # governed project had no way out of BLOCKED through the CLI at all).
    command_authority, operator = None, False
    if args.project_cmd in ("record-action", "unblock"):
        command_authority, operator, refusal = _command_authority(args, store)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 5

    if args.project_cmd == "record-action":
        try:
            with _authority_section(store, command_authority, operator,
                                    "record-action"):
                st = record_external_action(
                    store, actor=args.actor, reason=args.reason,
                    repo_path=st.repo_path, node=args.node,
                    head_before=args.head_before or "", authority=command_authority,
                )
        except StoreError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 4
        r = st.external_actions[-1]
        print(f"{r.action_id} recorded: {r.actor} -- {r.head_before or '?'} -> "
              f"{r.head_after or '?'}{' (tree unchanged)' if not r.changed_the_tree else ''}")
        return 0

    if args.project_cmd == "unblock":
        try:
            with _authority_section(store, command_authority, operator, "unblock"):
                st = unblock(store, args.node, args.reason, authority=command_authority)
        except StoreError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 4
        n = st.node(args.node)
        print(f"{args.node} is now {n.lifecycle.value}; recorded as {st.decisions[-1].id}")
        return 0

    verdict_text, why = resume_decision(st)

    from .taskstate import PROJECT_SCOPE, from_project, render

    # Layer 0 for a fresh session. A state it cannot be built from (a duplicate or
    # blank node id, a lifecycle with no mapping) is reported next to the verdict,
    # never in place of it: the verdict is what this command has always answered
    # and must not start failing. The message is one line, whatever raised it.
    try:
        compact_, compact_error = from_project(st), None
    except ValueError as exc:
        compact_, compact_error = None, " ".join(str(exc).split())[:300]

    if args.project_cmd == "resume":
        print(json.dumps({"project_id": st.project_id, "verdict": verdict_text,
                          "reason": why, "measurement_head": st.measurement_head,
                          "closure_generation": st.closure_generation,
                          "rc_closed": st.rc_closed(),
                          "gates_green": st.gates_green(),
                          "task_state_scope": PROJECT_SCOPE,
                          "task_state": (compact_.model_dump(mode="json")
                                         if compact_ is not None else None),
                          "task_state_error": compact_error}, indent=2))
        return 0

    # status
    print(f"project {st.project_id}  ({st.repo_path})")
    print(f"  verdict          {verdict_text}")
    print(f"  reason           {why}")
    print(f"  dag terminal     {st.dag_terminal()}")
    print(f"  gates green      {st.gates_green()}")
    print(f"  RC_CLOSED        {st.rc_closed()}")
    print(f"  measured at      {st.measurement_head or '(nothing recorded)'}")
    print(f"  closure gen      {st.closure_generation}")
    print(f"  write_seq        {st.write_seq}")
    print("  nodes:")
    for n in st.nodes:
        marke = "repair" if n.repair_of else ""
        print(f"    {n.id:24s} {n.lifecycle.value:10s} {n.action_class.value:8s} "
              f"rej={n.rejections} {marke}")
    if st.gates:
        last_ = {}
        for g in st.gates:
            last_[g.name] = g
        print("  gates (most recent per name):")
        for name, g in sorted(last_.items()):
            print(f"    {name:24s} {g.outcome.value:8s} @{g.subject}")
    if st.decisions:
        print(f"  decisions: {len(st.decisions)} recorded, latest:")
        d = st.decisions[-1]
        print(f"    {d.id} {d.kind.value} by {d.actor}: {d.reason}")
    if st.external_actions:
        # Shown separately, and shown at all: a repository change that is
        # invisible here is one a later closure measures without anyone being
        # able to say where it came from.
        print(f"  external actions: {len(st.external_actions)} recorded")
        for r in st.external_actions[-3:]:
            moved_ = "" if r.changed_the_tree else "  (tree unchanged)"
            print(f"    {r.action_id} {r.actor}: {r.head_before or '?'} -> "
                  f"{r.head_after or '?'}{moved_}  {r.reason[:60]}")
    print("  compact task state (Layer 0):")
    if compact_ is None:
        print(f"    unavailable: {compact_error}")
    else:
        for line_ in render(compact_).splitlines():
            print(f"    {line_}")
    print(f"    scope: {PROJECT_SCOPE}")
    if not st.gates_green():
        print("    NOT ALL CLEAR: the gates are not all green (see the verdict and the "
              "gates above); this block covers nodes only")
    return 0


def cmd_project_contract(args) -> int:
    """The PM boundary (P2-29, P2-30), read-only.

    `check` validates an imported contract and prints its digest over content;
    `impact` describes what an amendment would change and never applies it;
    `owner` answers who decides a class of state from the ownership table, and
    refuses a class nobody owns; `conflicts` lists where an external DONE meets
    no verdict here. Nothing is written: no run root, no project store, no file.

    A contract without `effective_at` is refused rather than checked. The
    module's default for it is the moment of reading, so its digest would be a
    timestamp presented as an identity.
    """
    from pydantic import ValidationError

    from .projectcontract import (
        OWNERSHIP, ProjectContract, UnknownState, authority_for, conflicts, impact_of,
    )

    def load(path: str) -> ProjectContract:
        contract = ProjectContract.model_validate_json(
            Path(path).read_text(encoding="utf-8"))
        if "effective_at" not in contract.model_fields_set:
            raise ValueError(
                f"{path} does not state effective_at; its default is the time of "
                f"reading, so the digest would change on every check")
        return contract

    def summary(contract: ProjectContract) -> dict:
        return {"project_contract_id": contract.project_contract_id,
                "version": contract.version,
                "source_system": contract.source_system,
                "source_revision": contract.source_revision,
                "digest": contract.digest(),
                "work_packages": [w.work_package_id for w in contract.work_packages],
                "milestones": sorted(contract.milestones)}

    def internal_statuses(raw: str | None, contract: ProjectContract) -> dict[str, str]:
        # No map is an empty map: absence of a verdict here is not agreement.
        if raw is None:
            return {}
        try:
            statuses = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"--internal is not JSON: {exc}") from exc
        if not isinstance(statuses, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in statuses.items()):
            raise ValueError("--internal must be a JSON object of work package id to "
                             "status string")
        unknown = sorted(set(statuses) - {w.work_package_id for w in contract.work_packages})
        if unknown:
            raise ValueError(f"--internal names work packages this contract does not "
                             f"contain: {unknown}")
        return statuses

    try:
        if args.contract_cmd == "schema":
            out, rc = ProjectContract.model_json_schema(), 0
        elif args.contract_cmd == "check":
            out, rc = summary(load(args.file)), 0
        elif args.contract_cmd == "impact":
            previous, proposed = load(args.file), load(args.proposed)
            impact = impact_of(previous, proposed)
            if proposed.version <= previous.version and proposed.digest() != previous.digest():
                raise ValueError(
                    f"an amendment goes forward: version {previous.version} -> "
                    f"{proposed.version} with different content would rewrite a version "
                    f"that has already been imported")
            # Only the two files are inputs: evidence, work in flight, the autonomy
            # contract and the release state are not, so those fields are defaults
            # and say nothing.
            out, rc = {"previous": summary(previous), "proposed": summary(proposed),
                       "impact": impact.model_dump(mode="json"),
                       "disturbs_anything": impact.disturbs_anything,
                       "not_evaluated": ["evidence_invalidated", "active_work_affected",
                                         "beyond_autonomy", "release_affected"]}, 0
        elif args.contract_cmd == "owner":
            if args.state_class is None:
                out = {name: authority.value for name, authority in OWNERSHIP.items()}
            else:
                out = {"state_class": args.state_class,
                       "authority": authority_for(args.state_class).value}
            rc = 0
        else:  # conflicts
            contract = load(args.file)
            found = conflicts(contract, internal=internal_statuses(args.internal, contract))
            out = {"project_contract_id": contract.project_contract_id,
                   "digest": contract.digest(),
                   "conflicts": [c.model_dump(mode="json") | {"statement": str(c)}
                                 for c in found]}
            rc = 1 if found else 0
    except ValidationError as exc:
        errors = exc.errors()
        message = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'contract'}: {e['msg']}"
            for e in errors[:3])
        if len(errors) > 3:
            message += f"; and {len(errors) - 3} more"
        print(f"contract refused: {message}", file=sys.stderr)
        return 2
    except UnknownState as exc:
        print(f"contract refused: {exc.args[0]}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:     # UnicodeDecodeError is a ValueError
        print(f"contract refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return rc


def _read_token(args) -> str | None:
    if getattr(args, "token", None):
        return args.token.strip()
    path = getattr(args, "token_file", None)
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def _claimed_master(args, store):
    """(authority, None) when the caller proves it is the live master holding
    `--epoch`, else (None, why).

    AC7: an old session epoch issues no authoritative command. Knowing the
    session id and epoch is not proof -- `hoh project authority` prints both --
    so the token handed out when that epoch was acquired is required too.
    """
    from .orchestrator import write_lock
    from .succession import LeaseError, MasterAuthority

    token = _read_token(args)
    if not args.session or args.epoch is None or not token:
        return None, ("acting as a master needs --session, --epoch and its token "
                      "(--token or --token-file, as written by `project drive "
                      "--token-out`)")
    try:
        authority = MasterAuthority(store.dir, args.session,
                                    serialize=lambda: write_lock(store))
    except LeaseError as exc:
        return None, str(exc)
    authority.present(epoch=int(args.epoch), token=token)
    problems = authority.problems()
    if problems:
        return None, "stale master: " + "; ".join(problems)
    return authority, None


def _command_authority(args, store):
    """(authority, is_operator_epoch, refusal) for unblock / record-action.

    * An ungoverned project with no credential: the old behaviour.
    * A credential given: it must be the live master's (`_claimed_master`).
    * Governed, no credential, and a master is alive: refused -- act through
      that master or have it hand over.
    * Governed, no credential, no master alive (released, lost, none): the
      command acquires an epoch of its own (reason OPERATOR), acts under it,
      and releases it.
    """
    from .orchestrator import write_lock
    from .succession import LeaseError, LeaseHeld, MasterAuthority, Reason, utc_now

    governed = (store.dir / "epochs.json").is_file()
    if args.session or args.epoch is not None or _read_token(args):
        authority, refusal = _claimed_master(args, store)
        return authority, False, refusal
    if not governed:
        return None, False, None
    probe = MasterAuthority(store.dir, "cli-probe")
    try:
        holder, why = probe.holder_state(probe.read_lease(), utc_now())
    except LeaseError as exc:
        return None, False, str(exc)
    if holder == "LIVE":
        return None, False, (f"a live master holds this project ({why}); act with its "
                             "--session/--epoch/--token, or have it hand over")
    authority = MasterAuthority(store.dir, f"operator-{os.getpid()}",
                                serialize=lambda: write_lock(store))
    try:
        authority.acquire(program_generation=store.read_state().program_generation,
                          reason=Reason.OPERATOR,
                          note=f"operator command {args.project_cmd}: {args.reason}")
    except (LeaseHeld, LeaseError, LockBusy) as exc:
        return None, False, str(exc)
    return authority, True, None


@contextmanager
def _authority_section(store, authority, operator: bool, what: str):
    """The program lock around a stamped command write (review A, S2), and the
    release of an operator epoch afterwards."""
    from .orchestrator import write_lock

    if authority is None:
        yield
        return
    try:
        with write_lock(store):
            authority.check()
            yield
    finally:
        if operator:
            try:
                authority.release(reason=f"operator command {what} done")
            except Exception:                          # noqa: BLE001 - best effort
                pass


def _program_lock_free(store) -> bool:
    """Is the program's write lock free right now? A rotation starts only at a
    safe point: while a write is in flight it is recorded as pending instead."""
    try:
        with store.lock():
            return True
    except LockBusy:
        return False


def _project_hold(args, store, st) -> int:
    """Become the governed master and *stay* one: renew the lease until it is
    fenced, released, or the session's own process is gone.

    A lease is judged by its holder's pid, and a conversation is not a
    long-lived process: acquired by a short command it reads LOST once that
    command has exited. This is the bootstrap of the first generation (acquire
    without driving) and what a session runs in the background to keep its
    program. A live holder elsewhere refuses it.
    """
    from .orchestrator import write_lock
    from .rollover import RolloverError, hand_token_over, hold_lease
    from .succession import LeaseError, LeaseHeld, MasterAuthority
    import math

    if args.interval is not None and not (0 < args.interval and math.isfinite(args.interval)):
        # before the lease is taken: a renewal period that is not positive and finite
        # would busy-renew or fail after the acquire (`sleep(nan)`, `sleep(inf)`,
        # `sleep(-1)`) and leave a LOST lease behind
        print(f"refused: --interval must be positive and finite, got {args.interval}",
              file=sys.stderr)
        return 2
    try:
        authority = MasterAuthority(store.dir, args.session, ttl_seconds=args.ttl,
                                    serialize=lambda: write_lock(store))
        epoch = authority.acquire(program_generation=st.program_generation,
                                  note=args.note or "")
        hand_token_over(authority, args.token_out)      # gives the lease back on failure
    except (LeaseHeld, LeaseError, LockBusy, RolloverError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    _print({"project_id": st.project_id, "held": True, "session": args.session,
            "epoch": epoch.epoch, "epoch_reason": epoch.reason.value,
            "token_out": args.token_out})
    sys.stdout.flush()
    result = hold_lease(authority, interval=args.interval, watch_pid=args.watch_pid,
                        hold_for=args.hold_for)
    _print({"project_id": st.project_id, "ended": result.reason,
            "renewals": result.renewals, "released": result.released})
    return 0


def _rollover_default_input(store, st, args, model: str | None) -> dict:
    """The capsule's author input when none is given: the project's own state
    file, the run set and the repository's HEAD -- three facts the product
    derives itself."""
    return {
        "successor_model": model or "",
        "derive": [
            {"entry_id": "project-state", "section": "REGISTER", "deriver": "file_digest",
             "params": {"path": str(store.state_path)}},
            {"entry_id": "run-set", "section": "RUNS", "deriver": "run_set",
             # absolute: the successor starts in its own directory, where a
             # relative `--root` would name another place and re-derive another set
             "params": {"runs_root": str(Path(args.root).expanduser().resolve())}},
            {"entry_id": "repo-head", "section": "CANDIDATES", "deriver": "ref_state",
             "params": {"repo": st.repo_path, "ref": "HEAD"}},
        ],
    }


def _project_rollover(args, store, st) -> int:
    """The master rolls the orchestrator session over to a fresh one.

    Exit 0: done (the successor holds the next epoch) or nothing was due;
    5: the rotation did not happen and this session stays (or is again) the
    master, or the command was refused; 6: another session is master; 7: no
    safe point yet, recorded as pending. See `hoh.rollover.run_rollover`.
    """
    import math

    from .rollover import (
        HerdrLauncher, Outcome, RolloverError, TriggerError, evaluate_trigger,
        hand_token_over, run_rollover,
    )
    from .succession_capsule import CapsuleInput, LocalObserver

    if not (math.isfinite(args.prompt_delay) and 0 <= args.prompt_delay <= 300):
        print(f"refused: --prompt-delay must be between 0 and 300 seconds, got "
              f"{args.prompt_delay}", file=sys.stderr)
        return 2
    if args.prompt_delay > 0 and args.prompt_delay >= args.ack_timeout:
        print(f"refused: --prompt-delay ({args.prompt_delay}) must be shorter than "
              f"--ack-timeout ({args.ack_timeout}): the delay and the agent start come out "
              "of the acknowledgement window", file=sys.stderr)
        return 2
    successor_args: list[str] = []
    if args.successor_permission_mode:
        successor_args += ["--permission-mode", args.successor_permission_mode]
    if args.successor_effort:
        successor_args += ["--effort", args.successor_effort]

    authority, refusal = _claimed_master(args, store)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 5
    try:
        if args.input:
            data = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"the capsule input must be a JSON object, got "
                                 f"{type(data).__name__}")
            if args.model:
                data["successor_model"] = args.model
        else:
            data = _rollover_default_input(store, st, args, args.model)
        if not data.get("successor_model"):
            print("refused: the successor's model is not chosen here; give --model "
                  "(or `successor_model` in --input)", file=sys.stderr)
            return 2
        inp = CapsuleInput.model_validate(data)
    except (OSError, ValueError) as exc:
        print(f"refused: the capsule input is not usable: {exc}", file=sys.stderr)
        return 2

    trigger = None
    if args.transcript:
        def trigger():
            return evaluate_trigger(args.transcript, threshold=args.threshold)

    successor = args.successor or new_run_id("succ")
    try:
        result = run_rollover(
            authority, store.dir, inp=inp, observer=LocalObserver(),
            launcher=HerdrLauncher(agent_args=tuple(successor_args),
                                   prompt_delay_s=args.prompt_delay),
            root=str(store.root), repo=st.repo_path,
            project_id=st.project_id, successor_session=successor, trigger=trigger,
            safe=lambda: _program_lock_free(store), generation=st.program_generation,
            ack_timeout_s=args.ack_timeout, takeover_s=args.takeover, poll_s=args.poll)
    except (TriggerError, RolloverError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if result.outcome is Outcome.RECLAIMED and args.token_out:
        try:
            hand_token_over(authority, args.token_out)
        except RolloverError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 5
    _print(result.as_dict())
    if result.outcome in (Outcome.COMPLETE, Outcome.NOT_TRIGGERED):
        return 0
    if result.outcome is Outcome.DEFERRED:
        return 7
    if result.outcome is Outcome.SUPERSEDED:
        return 6
    return 5


def _project_rollover_verify(args, store, st) -> int:
    """Read-only: re-derive a capsule and print every difference. Exit 0, or 5
    on any difference."""
    from .succession import MasterAuthority
    from .succession_capsule import CapsuleError, LocalObserver, load_capsule, verify_capsule

    try:
        cap = load_capsule(args.capsule)
    except CapsuleError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    if cap.project_id != st.project_id:
        print(f"refused: the capsule is for project {cap.project_id!r}, not "
              f"{st.project_id!r}", file=sys.stderr)
        return 5
    ver = verify_capsule(cap, probe=MasterAuthority(store.dir, args.session),
                         observer=LocalObserver(), session=args.session,
                         pointer_digest=args.digest, handover=args.handover,
                         project_generation=st.program_generation)
    _print(ver.as_dict())
    return 0 if ver.ok else 5


def _project_rollover_accept(args, store, st) -> int:
    """The successor: verify, acknowledge, wait for the handover, derive
    everything again, take the lease, then hold it. Exit 5 on any refusal --
    nothing is taken."""
    from .orchestrator import write_lock
    from .rollover import RolloverError, Successor, hold_lease
    from .succession import LeaseError, MasterAuthority
    from .succession_capsule import CapsuleError, LocalObserver, load_capsule
    import math

    if args.interval is not None and not (0 < args.interval and math.isfinite(args.interval)):
        print(f"refused: --interval must be positive and finite, got {args.interval}",
              file=sys.stderr)
        return 2
    try:
        cap = load_capsule(args.capsule)
        if cap.project_id != st.project_id:
            raise RolloverError(f"the capsule is for project {cap.project_id!r}, not "
                                f"{st.project_id!r}")
        authority = MasterAuthority(store.dir, args.session, ttl_seconds=args.ttl,
                                    serialize=lambda: write_lock(store))
        successor = Successor(
            authority, args.capsule, observer=LocalObserver(), pointer_digest=args.digest,
            project_generation=lambda: store.read_state().program_generation,
            poll_s=args.poll, wait_s=args.wait)
        took = successor.accept(token_out=args.token_out)
    except (RolloverError, CapsuleError, LeaseError, LockBusy, StoreError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    print(f"took over: session {took.session} holds epoch {took.epoch} of project "
          f"{st.project_id}; receipt {took.receipt_file}")
    sys.stdout.flush()
    if args.no_hold:
        return 0
    result = hold_lease(took.authority, interval=args.interval, watch_pid=args.watch_pid,
                        hold_for=args.hold_for)
    print(f"hold ended: {result.reason} after {result.renewals} renewals")
    return 0


def _project_drive(args, store, st) -> int:
    """Become the governed master of a project and drive it (P2-07/08/09).

    Whoever calls this -- a Claude Code or Codex session, a Herdr pane, the
    resume loop -- *becomes* the master by acquiring the lease, which begins a
    new epoch. Being the master is that registered status, not the kind of
    process. A live holder elsewhere refuses the acquisition; a holder that
    was killed, rebooted away or let its lease lapse is replaced (RECOVERY),
    and a named successor takes a handed-over lease (SUCCESSION).
    """
    from .orchestrator import HaltClass, write_lock
    from .resumeloop import drive_governed, load_driver
    from .succession import LeaseError, LeaseHeld, MasterAuthority

    factory = load_driver(store.dir)
    if factory is None:
        print(f"refused: project {st.project_id} registers no driver "
              f"({store.dir / 'driver.json'}); nothing here guesses how to "
              "launch its runs", file=sys.stderr)
        return 4
    try:
        authority = MasterAuthority(store.dir, args.session, ttl_seconds=args.ttl,
                                    serialize=lambda: write_lock(store))
        epoch = authority.acquire(program_generation=st.program_generation,
                                  note=args.note or "")
        if args.token_out:
            # The token is this epoch's credential for commands issued on the
            # master's behalf (handoff, unblock); written owner-only, and only
            # when asked for. A file that already exists is forced to 0600
            # before the token goes in (review A2, N4).
            try:
                out = Path(args.token_out).expanduser()
                fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(authority.token or "")
            except OSError as exc:
                # Acquired and then failed: give the lease back, or it reads
                # LOST and the continuation unit would drive a project whose
                # command failed.
                try:
                    authority.release(reason=f"--token-out failed: {exc}")
                except Exception:                  # noqa: BLE001 - best effort
                    pass
                print(f"refused: cannot write the token to {args.token_out}: {exc}",
                      file=sys.stderr)
                return 5
    except LeaseHeld as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    except (LeaseError, LockBusy) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    result = drive_governed(store, authority, factory, max_rounds=args.max_rounds)
    _print({"project_id": st.project_id, "master": args.session,
            "epoch": epoch.epoch, "epoch_reason": epoch.reason.value,
            "halt": result.halt.value, "reason": result.reason,
            "steps": [f"{s.kind}:{s.node or ''}" for s in result.steps]})
    if result.halt is HaltClass.CLOSED:
        return 0
    return 6 if result.halt is HaltClass.SUPERSEDED else 1


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hoh",
        description="Harness-of-Harness on top of Herdr: evidence-bound acceptance for\n"
        "long-horizon agent runs.",
    )
    p.add_argument("--root", default=str(DEFAULT_ROOT), help="directory holding the runs")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser(
        "worktree",
        help="Create the isolated worktree through Herdr (the developer writes there)",
    )
    w.add_argument("--repo", required=True, help="project checkout")
    w.add_argument("--branch", required=True, help="branch for the worktree")
    w.add_argument("--base", help="base ref (default: current HEAD)")
    w.set_defaults(func=cmd_worktree, run_id=None)

    s = sub.add_parser("start", help="Create a new run")
    s.add_argument("--repo", required=True, help="project worktree (git)")
    s.add_argument("--spec", required=True, help="path to the specification")
    s.add_argument("--project", help="project name (default: directory name)")
    s.add_argument("--run-id", help="own run id instead of a generated one")
    s.add_argument("--max-iterations", type=int, default=10)
    s.add_argument(
        "--max-dispatches", type=int, default=None,
        help="ceiling on role dispatches for this run. The field existed and "
             "nothing could set it, so a project that wanted to hold a node "
             "and its repair runs to one shared budget had no way to say so -- "
             "which is how a matched-budget benchmark came to give one arm "
             "twice the resource")
    s.add_argument(
        "--mode",
        choices=["no-mistakes", "direct-PR", "local-only"],
        default="local-only",
        help="delivery mode. yolo always stays off.",
    )
    s.set_defaults(func=cmd_start)

    r = sub.add_parser("run", help="Run iterations")
    r.add_argument("run_id")
    r.add_argument("--iterations", type=int, default=1, help="how many iterations")
    r.add_argument("--until-accepted", action="store_true",
                   help="stop after the first acceptance")
    r.add_argument("--planner", default="claude")
    r.add_argument("--developer", default="claude")
    r.add_argument("--qa", default="kimi",
                   help="a different model in a fresh context is the strongest "
                        "setup for the independent review")
    r.add_argument("--no-herdr", action="store_true",
                   help="run without Herdr -- no acceptance value for A01/A02/A12")
    r.add_argument(
        "--isolation", default=None, choices=("none", "strict"),
        help="how acceptance checks are executed. 'none' is the historical "
             "path: a reduced environment and setrlimit, which is a tripwire "
             "and not a boundary. 'strict' runs every check inside a "
             "filesystem- and network-isolated sandbox and refuses to start "
             "the run at all if the machine cannot provide one.",
    )
    r.add_argument(
        "--role-model", action="append", default=[], metavar="ROLE=MODEL",
        help="model per role, repeatable: planner=sonnet, qa=opus. "
             "'all=sonnet' sets every role. Without this, each role runs on "
             "whatever the harness defaults to -- which is invisible in the "
             "run record.",
    )
    r.add_argument(
        "--role-effort", action="append", default=[], metavar="ROLE=LEVEL",
        help="effort level per role, same shape: low, medium, high, xhigh, max",
    )
    r.add_argument(
        "--role-account", action="append", default=[], metavar="ROLE=HANDLE",
        help="the operator-registered handle of the account a role runs under, "
             "same shape as --role-model. A name, never a credential: a value "
             "that looks like a key is refused. Recorded with every dispatch "
             "(P2-43); without it the record says NOT_AVAILABLE.")
    r.add_argument(
        "--master-session", default=None, metavar="ID",
        help="the governed master this run works for: its session id. Given "
             "together with --master-epoch by a master that launches runs "
             "(P2-43). A park is recorded as bound to it. Without both, the "
             "run records 'master: NONE'.")
    r.add_argument(
        "--master-epoch", type=_master_epoch, default=None, metavar="N",
        help="the master's epoch (with --master-session)")
    r.add_argument("--role-timeout", type=int, default=2700, metavar="SEC",
                   help="deadline per role run in seconds (default 2700 = 45 min). "
                        "A thorough differential review takes time; too tight a "
                        "deadline trains superficial work.")
    r.add_argument("--approval-policy", metavar="PATH", default=None,
                   help="what each role may do without asking, decided up front "
                        "(P1-09); e.g. policy/role_approval.default.json. Without "
                        "it a role inherits the operator's interactive defaults "
                        "and every unapproved action stops the run for a human.")
    r.add_argument("--autonomy", metavar="PATH",
                   help="the autonomy contract this run is governed by. Pinned to the "
                        "run on first use; a resumed run obeys the contract it began "
                        "under, and a different one offered later is refused.")
    r.add_argument("--program-epoch", type=int, default=None,
                   help="the programme epoch the contract is pinned to")
    r.add_argument("--trust-worktree", action="store_true",
                   help="pre-grant folder trust for this run's own development "
                        "worktree -- only a linked worktree Herdr created, below "
                        "its worktree root -- so the developer does not stop at "
                        "the trust dialog. Recorded in the run directory.")
    r.add_argument("--storage-root", metavar="PATH", default=None,
                   help="the storage state root (STORAGE-B3): its ledger admits or "
                        "refuses every role dispatch. Default: $HOH_STATE_ROOT, else "
                        "${XDG_STATE_HOME:-~/.local/state}/veriharness. Must be "
                        "initialised (`hoh storage init --root PATH`); never created here.")
    r.set_defaults(func=cmd_run)

    for name, fn, help_ in (
        ("status", cmd_status, "Current state as JSON"),
        ("report", cmd_report, "Report as markdown"),
    ):
        c = sub.add_parser(name, help=help_)
        c.add_argument("run_id")
        if name == "report":
            c.add_argument("--out", help="write to a file instead of stdout")
        c.set_defaults(func=fn)

    for name, fn, help_, needs_reason in (
        ("pause", cmd_pause, "Halt at a safe boundary", True),
        ("resume", cmd_resume, "Resume with reconciliation", False),
        ("unblock", cmd_unblock, "Lift the block (out of BLOCKED, with a reason)", True),
        ("cancel", cmd_cancel, "Cancel", True),
    ):
        c = sub.add_parser(name, help=help_)
        c.add_argument("run_id")
        if needs_reason:
            c.add_argument("--reason", default="requested by the captain")
        if name == "resume":
            c.add_argument("--reason", default="resume after reconciliation")
            c.add_argument(
                "--force", action="store_true",
                help="discard unclear task references -- only if you have "
                     "checked that nothing is running any more",
            )
        c.set_defaults(func=fn)

    am = sub.add_parser(
        "amend", help="Change a running specification, on the record")
    am.add_argument("run_id")
    am.add_argument("--spec-file", required=True,
                    help="the new text. The old one is parked, never replaced")
    am.add_argument("--amendment-id", required=True)
    am.add_argument("--kind", required=True,
                    choices=["clarify", "narrow", "widen", "correct"],
                    help="clarify claims not to change what would pass, and is "
                         "held to that claim; the other three do")
    am.add_argument("--actor", required=True, help="who is amending, by name")
    am.add_argument("--reason", required=True)
    am.add_argument("--affects", default="",
                    help="comma-separated check ids this amendment touches. "
                         "Their evidence stops counting and they have to be "
                         "measured again before a candidate can be accepted")
    am.add_argument("--evidence", default="",
                    help="comma-separated references that prompted this")
    am.set_defaults(func=cmd_amend)

    gl = sub.add_parser("goal", help="Goalbook spanning run boundaries")
    gs = gl.add_subparsers(dest="goal_command", required=True)
    gs.add_parser("list", help="All objectives with their status")
    gp = gs.add_parser("propose", help="File a new objective as PROPOSED")
    gp.add_argument("objective_id")
    gp.add_argument("--title", required=True)
    gp.add_argument("--description", default="")
    gp.add_argument("--spec", default=None, help="Path to the specification")
    gp.add_argument("--by", default="agent",
                    help="who proposes -- an agent may propose, not decide")
    for name, helptext in (
        ("approve", "PROPOSED -> OPEN. The captain's decision."),
        ("activate", "OPEN -> ACTIVE. This is what is being worked on now."),
        ("complete", "-> DONE. Requires evidence, not a model's assertion."),
        ("drop", "-> DROPPED. Stays on the record with a reason; nothing is deleted."),
    ):
        g = gs.add_parser(name, help=helptext)
        g.add_argument("objective_id", nargs="+")
        g.add_argument("--by", required=True, help="who decides")
        g.add_argument("--reason", required=True)
    gl.set_defaults(func=cmd_goal)

    fs = sub.add_parser(
        "resume-quota",
        help="Resume runs stuck on the usage quota once the wait is over",
    )
    fs.add_argument("run_id", nargs="?", default=None,
                    help="without an argument: check every run and project")
    fs.add_argument("--drive", action="store_true",
                    help="drive a resumed project on, as a new governed master, "
                         "when it registers a driver")
    fs.add_argument("--recover", action="store_true",
                    help="also replace the master of a project whose lease holder "
                         "died (killed, rebooted away, lease expired), with a new "
                         "governed epoch; a released lease or a live holder is left alone")
    fs.set_defaults(func=cmd_resume_quota)

    rsg = sub.add_parser(
        "resume-storage",
        help="Resume runs parked on storage pressure once a role run would be admitted",
    )
    rsg.add_argument("run_id", nargs="?", default=None,
                     help="without an argument: check every run and project")
    rsg.add_argument("--storage-root", metavar="PATH", default=None,
                     help="the storage state root (same resolution as `hoh run`)")
    rsg.set_defaults(func=cmd_resume_storage)

    sg = sub.add_parser("storage", help="The storage state root (STORAGE-A/B)")
    sgs = sg.add_subparsers(dest="storage_cmd", required=True)
    sgi = sgs.add_parser("init", help="Initialise a storage state root (writes OWNER.json)")
    sgi.add_argument("--root", dest="storage_root_path", required=True, metavar="PATH")
    sg.set_defaults(func=cmd_storage, run_id=None)
    sgw = sgs.add_parser("steward", help="One optimisation pass of a project's storage "
                                         "steward (STORAGE-D); prints its report")
    sgw.add_argument("--project", required=True, metavar="P")
    sgw.add_argument("--once", action="store_true",
                     help="required: exactly one pass with the periodic event (what the ops "
                          "timer runs); the timer is the only scheduler")
    sgw.add_argument("--plan-only", action="store_true",
                     help="plan and report, never execute VERIFIED_RELOCATION")
    sgw.add_argument("--storage-root", metavar="PATH", default=None,
                     help="the storage state root (same resolution as `hoh run`)")
    sgw.set_defaults(func=cmd_storage_steward)
    sga = sgs.add_parser("adopt-project",
                         help="Adopt an existing project directory through an adoption anchor "
                              "committed in the trust root (a one-time operator act)")
    sga.add_argument("--project", required=True, metavar="P")
    sga.add_argument("--project-root", required=True, metavar="PATH",
                     help="the trust-root repository the anchor is committed in")
    sga.add_argument("--anchor", required=True, metavar="COMMIT:FILE",
                     help="the adoption anchor: a commit (an ancestor of HEAD) and a file")
    sga.add_argument("--storage-root", required=True, metavar="PATH",
                     help="the storage state root, named explicitly (no fallback)")
    sga.set_defaults(func=cmd_storage_adopt_project)
    sgl = sgs.add_parser("survey-legacy",
                         help="Draft a legacy-roots anchor for legacy reviewer dirs (read-only)")
    sgl.add_argument("--project", required=True, metavar="P")
    sgl.add_argument("--reviews", required=True, metavar="DIR")
    sgl.add_argument("--out", required=True, metavar="FILE", help="a new file; never replaced")
    sgl.add_argument("--storage-root", required=True, metavar="PATH",
                     help="the storage state root, named explicitly (no fallback)")
    sgl.set_defaults(func=cmd_storage_survey_legacy)
    sgr = sgs.add_parser("adopt-legacy",
                         help="Register the legacy roots a committed legacy-roots anchor lists")
    sgr.add_argument("--project", required=True, metavar="P")
    sgr.add_argument("--anchor", required=True, metavar="COMMIT:FILE")
    sgr.add_argument("--storage-root", required=True, metavar="PATH",
                     help="the storage state root, named explicitly (no fallback)")
    sgr.set_defaults(func=cmd_storage_adopt_legacy)

    asu = sub.add_parser(
        "agent-setup",
        help="Print or install the text that makes an agent session use VeriHarness",
    )
    asg = asu.add_mutually_exclusive_group(required=True)
    asg.add_argument("--print", dest="print_text", action="store_true",
                     help="print one text to stdout and write nothing")
    asg.add_argument("--install", metavar="DIR", default=None,
                     help="write new files into this existing directory; never replaces a file")
    asu.add_argument("--kind", choices=("skill", "snippet", "all"), default="skill")
    asu.add_argument("--delegation", choices=("ask", "standing"), default="ask",
                     help="ask: offer a run and wait for a yes; standing: the operator delegated "
                          "starting runs for development tasks")
    asu.add_argument("--allow-user-config", action="store_true",
                     help="permit a DIR inside ~/.claude, ~/.codex, ~/.agents, ~/.config or "
                          "$CLAUDE_CONFIG_DIR")
    asu.set_defaults(func=cmd_agent_setup, run_id=None)

    dl = sub.add_parser("deliver", help="Deliver an accepted checkpoint")
    dl.add_argument("run_id")
    dl.add_argument("--approve", action="store_true",
                    help="the captain's explicit approval -- without it nothing happens")
    dl.add_argument("--into", default="main", help="target branch for local-only")
    dl.set_defaults(func=cmd_deliver)

    b = sub.add_parser("backup", help="Back up the run state (without arenas)")
    b.add_argument("run_id")
    b.add_argument("--out", help="target directory (default: <root>/backups)")
    b.set_defaults(func=cmd_backup)

    rs = sub.add_parser("restore", help="Restore a backed-up run")
    rs.add_argument("run_id")
    rs.add_argument("--archive", required=True)
    rs.add_argument("--overwrite", action="store_true",
                    help="replace the existing run instead of restoring alongside it")
    rs.set_defaults(func=cmd_restore)

    c = sub.add_parser("list", help="All runs")
    c.set_defaults(func=cmd_list, run_id=None)

    mg = sub.add_parser(
        "migrate",
        help="Bring run and project state to the current schema version "
             "(dry run unless --apply; the old form is parked, never deleted)")
    mg.add_argument("--apply", action="store_true",
                    help="write the migrated records (default: report only)")
    mg.add_argument("--now", default=None, metavar="ISO8601",
                    help="the instant that names the parked copies (default: now)")
    mg.set_defaults(func=cmd_migrate, run_id=None)

    t = sub.add_parser("timers", help="Durable waits under this root")
    t.set_defaults(func=cmd_timers, run_id=None)

    se = sub.add_parser("sentinel", help="What is due now, and what it asks for")
    se.add_argument("--program-epoch", type=int, default=None,
                    help="refuse timers armed under a different program epoch")
    se.add_argument("--session-epoch", type=int, default=None,
                    help="refuse timers armed by a superseded session")
    se.set_defaults(func=cmd_sentinel, run_id=None)

    st = sub.add_parser("stuck", help="Non-terminal objects with no way out")
    st.set_defaults(func=cmd_stuck, run_id=None)

    dc = sub.add_parser(
        "decision",
        help="The decision exploration plane, read-only (P2-21)",
    )
    dcs = dc.add_subparsers(dest="decision_cmd", required=True)
    dk = dcs.add_parser(
        "check",
        help="Decision graph fsck: validate the structure of one or more DecisionMap "
             "files; exit 0 PASS, 1 FAIL, 2 refused",
    )
    dk.add_argument("maps", nargs="+", metavar="MAP",
                    help="a DecisionMap as JSON (model_dump(mode='json')); several files "
                         "are one graph")
    dk.add_argument("--program-refs", default=None, metavar="FILE",
                    help="JSON list of ProgramRef: which maps a programme graduated")
    dk.add_argument("--settled-ref", action="append", default=[], metavar="ID",
                    help="a decision id offered as settled provenance (repeatable)")
    dk.add_argument("--now", default=None, metavar="ISO8601",
                    help="instant with an offset for the lease checks; without it the "
                         "lease-expiry check is not evaluated")
    dk.add_argument("--receipt", default=None, metavar="FILE",
                    help="also write the result to this new file (never replaced)")
    dc.set_defaults(func=cmd_decision, run_id=None)

    pr = sub.add_parser(
        "project",
        help="The orchestration layer above a run: which runs happen at all",
    )
    ps = pr.add_subparsers(dest="project_cmd", required=True)
    ps.add_parser("list", help="All projects with their resume verdict")
    for name, helptext in (
        ("status", "Full state: nodes, gates, closure, decisions"),
        ("resume", "What a fresh session should do, as JSON"),
    ):
        sp = ps.add_parser(name, help=helptext)
        sp.add_argument("project_id")
    ub = ps.add_parser(
        "unblock",
        help="Return a BLOCKED node to READY, with a reason that goes on the record",
    )
    ub.add_argument("project_id")
    ub.add_argument("node")
    ub.add_argument("--reason", required=True,
                    help="why it is safe to proceed -- becomes a decision record")
    for cmd_parser in (ub,):
        cmd_parser.add_argument("--session", default=None,
                                help="the current master's session id (governed projects)")
        cmd_parser.add_argument("--epoch", type=int, default=None,
                                help="the current master's epoch (governed projects)")
        cmd_parser.add_argument("--token", default=None, help="the current master's token")
        cmd_parser.add_argument("--token-file", default=None,
                                help="file holding the current master's token")
    ra = ps.add_parser(
        "record-action",
        help="Record a repository change made outside the loop, so closure can be traced",
    )
    ra.add_argument("project_id")
    ra.add_argument("--actor", required=True,
                    help="'human', 'operator', or the tool that made the change")
    ra.add_argument("--reason", required=True, help="why it was made")
    ra.add_argument("--node", default=None, help="the node it relates to, if any")
    ra.add_argument("--session", default=None,
                    help="the current master's session id (governed projects)")
    ra.add_argument("--epoch", type=int, default=None,
                    help="the current master's epoch (governed projects)")
    ra.add_argument("--token", default=None, help="the current master's token")
    ra.add_argument("--token-file", default=None, help="file holding that token")
    ra.add_argument("--head-before", default="",
                    help="the repository head before the change -- it cannot be "
                         "measured afterwards, so it has to be supplied")
    au = ps.add_parser("authority", help="Who holds the project: lease and epoch chain")
    au.add_argument("project_id")
    dr = ps.add_parser(
        "drive",
        help="Become the governed master (new epoch) and drive the project",
    )
    dr.add_argument("project_id")
    dr.add_argument("--session", required=True,
                    help="this session's id; it becomes the master's identity")
    dr.add_argument("--ttl", type=int, default=1800, help="lease length in seconds")
    dr.add_argument("--max-rounds", type=int, default=40)
    dr.add_argument("--note", default="", help="recorded on the epoch")
    dr.add_argument("--token-out", default=None,
                    help="write this epoch's token (owner-only) for commands issued "
                         "on the master's behalf")
    ho = ps.add_parser(
        "handoff",
        help="Hand the lease to a named successor (master rollover)",
    )
    ho.add_argument("project_id")
    ho.add_argument("--session", required=True, help="the current master's session id")
    ho.add_argument("--epoch", type=int, required=True, help="the current master's epoch")
    ho.add_argument("--to", required=True, help="the successor's session id")
    ho.add_argument("--reason", required=True, help="why the rollover happens")
    ho.add_argument("--token", default=None,
                    help="the current master's token (visible in the process list; "
                         "prefer --token-file)")
    ho.add_argument("--token-file", default=None, help="file holding that token")
    hd = ps.add_parser(
        "hold",
        help="Become the governed master and stay one: renew the lease until fenced "
             "(the first generation's bootstrap; run it in the background)",
    )
    hd.add_argument("project_id")
    hd.add_argument("--session", required=True, help="this session's id")
    hd.add_argument("--ttl", type=int, default=1800, help="lease length in seconds")
    hd.add_argument("--note", default="", help="recorded on the epoch")
    hd.add_argument("--token-out", default=None,
                    help="write this epoch's token (owner-only); if that fails the "
                         "lease is given back")
    hd.add_argument("--watch-pid", type=int, default=None,
                    help="release the lease once this process (the session's) is gone")
    hd.add_argument("--hold-for", type=float, default=None,
                    help="release the lease after this many seconds")
    hd.add_argument("--interval", type=float, default=None,
                    help="seconds between renewals (default: a third of the ttl)")
    ro = ps.add_parser(
        "rollover",
        help="Roll the orchestrator session over: freeze a capsule, start a successor "
             "with a pointer, hand over once it re-derived the capsule",
    )
    ro.add_argument("project_id")
    ro.add_argument("--session", required=True, help="the current master's session id")
    ro.add_argument("--epoch", type=int, required=True, help="the current master's epoch")
    ro.add_argument("--token", default=None,
                    help="the current master's token (visible in the process list; "
                         "prefer --token-file)")
    ro.add_argument("--token-file", default=None, help="file holding that token")
    ro.add_argument("--successor", default=None, help="the successor's session id")
    ro.add_argument("--model", default=None, help="the successor session's model")
    ro.add_argument("--input", default=None,
                    help="JSON file: what to derive, the operator's decisions, advisory "
                         "context (default: the project state, the run set, HEAD)")
    ro.add_argument("--transcript", default=None,
                    help="only rotate if this transcript's last request is over the "
                         "threshold")
    ro.add_argument("--threshold", type=int, default=250_000,
                    help="context tokens at which --transcript fires")
    ro.add_argument("--ack-timeout", type=int, default=600,
                    help="seconds the successor has to acknowledge")
    ro.add_argument("--takeover", type=int, default=300,
                    help="seconds the handover stands before this session takes it back")
    ro.add_argument("--poll", type=float, default=1.0, help="seconds between polls")
    ro.add_argument("--token-out", default=None,
                    help="write the new token here if the handover lapses and this "
                         "session takes the program back")
    ro.add_argument("--successor-permission-mode", default=None,
                    choices=("auto",),
                    help="the successor's Claude permission mode. Only `auto` is offered: "
                         "the successor must run its first command unattended, and the "
                         "role deny-list and house-rules prompt of a role session do not "
                         "travel with it, so bypassPermissions is not offered and dontAsk "
                         "could not run the pointer's command. Without it Claude's own "
                         "default applies and the successor waits at its first approval, "
                         "unacknowledged (NO_ACK)")
    ro.add_argument("--successor-effort", default=None,
                    choices=("low", "medium", "high", "xhigh", "max"),
                    help="the successor's Claude effort level (without it, Claude's default)")
    ro.add_argument("--prompt-delay", type=float, default=0.0,
                    help="seconds to idle between starting the successor and sending its "
                         "pointer (0 to 300, and shorter than --ack-timeout; default 0). "
                         "The delay and the agent start come out of the acknowledgement "
                         "window, which is fixed when the capsule is frozen")
    rv = ps.add_parser(
        "rollover-verify",
        help="Read-only: re-derive a succession capsule and print every difference",
    )
    rv.add_argument("project_id")
    rv.add_argument("--capsule", required=True, help="the capsule file")
    rv.add_argument("--session", required=True, help="the verifying session's id")
    rv.add_argument("--digest", default=None, help="the capsule digest the pointer named")
    rv.add_argument("--handover", action="store_true",
                    help="verify as the successor after the handover")
    ra2 = ps.add_parser(
        "rollover-accept",
        help="The successor: verify, acknowledge, take the handover only if everything "
             "re-derives, then hold the lease",
    )
    ra2.add_argument("project_id")
    ra2.add_argument("--capsule", required=True, help="the capsule file")
    ra2.add_argument("--session", required=True, help="this (the successor's) session id")
    ra2.add_argument("--digest", required=True, help="the capsule digest the pointer named")
    ra2.add_argument("--ttl", type=int, default=1800, help="lease length in seconds")
    ra2.add_argument("--token-out", default=None,
                     help="write the new epoch's token (owner-only); if that fails the "
                          "lease is given back")
    ra2.add_argument("--no-hold", action="store_true",
                     help="return after taking over instead of holding the lease")
    ra2.add_argument("--watch-pid", type=int, default=None,
                     help="release the lease once this process (the session's) is gone")
    ra2.add_argument("--hold-for", type=float, default=None,
                     help="release the lease after this many seconds")
    ra2.add_argument("--interval", type=float, default=None,
                     help="seconds between renewals (default: a third of the ttl)")
    ra2.add_argument("--poll", type=float, default=1.0,
                     help="seconds between polls for the handover")
    ra2.add_argument("--wait", type=float, default=900.0,
                     help="seconds to wait for the handover before giving up")
    pc = ps.add_parser(
        "contract",
        help="The PM boundary, read-only: check an imported contract, describe an "
             "amendment, ask who owns a state class, list unverified external DONEs",
    )
    pcs = pc.add_subparsers(dest="contract_cmd", required=True)
    pcs.add_parser("schema", help="The contract's JSON schema")
    pck = pcs.add_parser("check", help="Validate a contract file and print its digest")
    pck.add_argument("file")
    pci = pcs.add_parser("impact",
                         help="Describe what a proposed version would change; applies nothing")
    pci.add_argument("file", help="the imported version")
    pci.add_argument("proposed", help="the proposed next version")
    pco = pcs.add_parser("owner", help="Who decides a class of state (all of them without one)")
    pco.add_argument("state_class", nargs="?", default=None)
    pcf = pcs.add_parser("conflicts",
                         help="External DONEs with no verified verdict here (exit 1 if any)")
    pcf.add_argument("file")
    pcf.add_argument("--internal", default=None,
                     help="JSON object of work package id to this product's status; "
                          "without it, no package has a verdict here")
    # Overrides the group's func, so cmd_project never sees `contract` and no
    # project store is opened.
    pc.set_defaults(func=cmd_project_contract)
    pr.set_defaults(func=cmd_project, run_id=None, project_id=None, node=None,
                    reason=None, actor=None, head_before=None, session=None,
                    epoch=None, token=None, token_file=None)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
