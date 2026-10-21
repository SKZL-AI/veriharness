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
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import pane_ledger, stages
from .contracts import Budgets, Condition, Role, Stage
from .controller import Controller, new_run_id
from .durable_io import DurableWriteNotSynced, durable_write
from .store import LockBusy, RunStore, StoreError, UnsafeRunId, list_runs

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
    # Born registered: the workspaces Herdr makes for the worktree are recorded in the record of
    # this runs root (`surfacebook.register_workspaces`) -- the ones the snapshot after the
    # creation holds and the snapshot before it did not. A snapshot that cannot be had costs the
    # record, never the worktree: an unrecorded workspace is never closed.
    from . import surfacebook, surfaces

    repo_path = str(Path(args.repo).expanduser().resolve())
    notes: list[str] = []
    control, before = None, None
    try:
        control = surfaces.make_control()
        before = control.snapshot()
    except Exception as exc:                      # noqa: BLE001 - the worktree is still made
        notes.append(f"Herdr's snapshot before the creation is unavailable ({exc}): the "
                     "workspaces will not be recorded")
    try:
        result = herdr_mod.worktree_create(
            cwd=repo_path,
            branch=args.branch,
            base=args.base,
            label=f"hoh {args.branch}",
        )
    except Exception as exc:
        print(f"Could not create the worktree: {exc}", file=sys.stderr)
        return 2

    path = (result.get("worktree") or {}).get("path") or result.get("path")
    recorded: list[dict] = []
    if before is not None:
        try:
            after = control.snapshot()
        except Exception as exc:                  # noqa: BLE001
            notes.append(f"Herdr's snapshot after the creation is unavailable ({exc}): the "
                         "workspaces were not recorded")
        else:
            caller_pane, caller_tab, _inside = surfaces.own_location()
            recorded, more = surfacebook.register_workspaces(
                root=args.root, repo=repo_path, worktree=path, before=before, after=after,
                backend=control.backend, caller_pane=caller_pane, caller_tab=caller_tab)
            notes.extend(more)
    for note in notes:
        print(f"worktree: {note}", file=sys.stderr)
    _print({"worktree": path, "branch": args.branch, "workspaces": recorded, "raw": result})
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

    The double-down on blocks (P3-42, `hoh.doubledown`) runs in the same call and is ON by default,
    so the installed continuation unit, which passes no flag, gets it: every block keeps a dossier;
    the repair spec of a proposal is measured (`--verify-proposals`, on) and a verified fix is
    recorded as WITHHELD for a person (`--apply-proposals`, on: the pass never applies a fix; only
    `hoh double-down apply --approved-by` does). Analysts are the one part that stays opt-in
    (`--analyst-harness`: a provider spend). `--no-double-down`, `--no-verify-proposals` and
    `--no-apply-proposals` opt out, and an opt-out is recorded.
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
    refused = _double_down_switches(args)
    if refused is not None:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    runs = phase(resume_runs, args.root, only=args.run_id)
    projects = [] if args.run_id else phase(resume_projects, args.root, drive=args.drive)
    if args.recover and not args.run_id:
        projects += phase(recover_projects, args.root)
    out = {"checked": len({r.get("target") for r in runs}), "results": runs,
           "projects": projects}
    opted = _double_down_optouts(args)
    kept = phase(_record_double_down_optout, args, opted)[0]
    rows = [kept] if opted else []
    if args.double_down:
        passed = phase(_double_down_pass, args)
        out["double_down_defects"] = sum(1 for r in passed if r.get("defect"))
        out["double_down"] = rows + passed
    elif rows:
        out["double_down"] = rows
    _print(out)
    return 0


def _double_down_switches(args) -> str | None:
    """Settle the double-down's switches for one `hoh resume-quota` call (P3-42, slice 3), or say
    why they contradict each other. The double-down, the measurement of proposals and the
    application of a proven fix are ON by default -- the installed continuation unit passes no
    flag, so this is how it picks them up -- and each has an explicit opt-out
    (`--no-double-down`, `--no-verify-proposals`, `--no-apply-proposals`), which is recorded
    (`_record_double_down_optout`). The positive flags stay as they were: they are accepted and
    change nothing. Sets `args.double_down`, `args.verify_proposals` and `args.apply_proposals`
    to what is in force."""
    off, no_verify, no_apply = args.no_double_down, args.no_verify_proposals, args.no_apply_proposals
    for flag, positive, negative in (("double-down", args.double_down, off),
                                     ("verify-proposals", args.verify_proposals, no_verify),
                                     ("apply-proposals", args.apply_proposals, no_apply)):
        if positive and negative:
            return f"--{flag} and --no-{flag} contradict each other"
    if off and (args.analyst_harness or args.analyst_model or args.analyst_effort):
        return "--analyst-* only applies with the double-down on (not with --no-double-down)"
    if not 1 <= args.analyst_timeout <= 3600:
        return "--analyst-timeout is 1 to 3600 seconds"
    if off and (args.verify_proposals or args.verify_isolation or args.apply_proposals):
        return "--verify-* and --apply-proposals only apply with the double-down on (not with --no-double-down)"
    if args.verify_isolation and no_verify:
        return "--verify-isolation only applies with --verify-proposals (not with --no-verify-proposals)"
    if args.apply_proposals and no_verify:
        return "--apply-proposals needs the measurement of proposals (not --no-verify-proposals)"
    args.double_down = not off
    args.verify_proposals = not off and not no_verify
    args.apply_proposals = not off and not no_verify and not no_apply
    return None


def _double_down_optouts(args) -> list[str]:
    """The parts of the double-down this call turned off, sorted by name."""
    return sorted(name for name, flag in (("double-down", args.no_double_down),
                                          ("verify-proposals", args.no_verify_proposals),
                                          ("apply-proposals", args.no_apply_proposals)) if flag)


def _record_double_down_optout(args, opted: list[str]) -> list[dict]:
    """The row of a call that turned (part of) the double-down off, with the record of it
    (`doubledown.record_optout`): an opt-out is a decision, not silence. Asked on every call, also
    one that opts out of nothing: the call that ends an opt-out is the opt-in that is recorded."""
    from . import doubledown

    return [{"scope": "double_down", "action": "opted_out", "opted_out": opted,
             "record": doubledown.record_optout(args.root, opted)}]


def _double_down_pass(args) -> list[dict]:
    """The double-down tick of `hoh resume-quota` (P3-42, slices 1 to 3): every block under the
    root is classified and keeps a dossier; analysts are dispatched only with
    `--analyst-harness`, confined (`_analyst_sessions`); the repair spec of a proposal is
    measured (`--verify-proposals`, on by default) and a verified fix is recorded as WITHHELD for a
    person (`--apply-proposals`, on by default; it never applies one). Writes dossiers only: a run's
    specification and block change through `ddapply` when a person approves, never in the pass."""
    from . import doubledown

    analysts = _analyst_sessions(args) if args.analyst_harness else None
    verifier = _proposal_verifier(args) if getattr(args, "verify_proposals", False) else None
    applier = _proposal_applier(args) if getattr(args, "apply_proposals", False) else None
    return doubledown.double_down_pass(args.root, analysts=analysts,
                                       provider=args.analyst_harness or "claude",
                                       only_run=args.run_id, verifier=verifier, applier=applier)


def _proposal_applier(args):
    """`applier(owner_dir, halt_id, now)` for the pass (P3-42, slice 3; on by default, off with
    `--no-apply-proposals`): `ddapply.apply_if_due` records what is decided about a verified fix
    without a person (a refusal no measurement can change) or that it is WITHHELD for one. It
    measures nothing and applies nothing."""
    from . import ddapply

    session = _verification_sessions(args)

    def applier(owner_dir, halt_id, now):
        return ddapply.apply_if_due(args.root, owner_dir, halt_id, session=session, now=now)
    return applier


def _proposal_verifier(args):
    """`verifier(owner_dir, halt_id, now)` for the pass (P3-42, slice 2; opt-in by
    `--verify-proposals`): the repair spec of a dossier that ended in a PROPOSAL is measured on a
    scratch copy of the run's candidate (`ddverify.verify_if_due`), under `--verify-isolation`
    (strict unless said otherwise). A criterion is a command an analyst proposed: spending the
    runner on it is the operator's decision, as spending the provider on analysts is."""
    from . import ddverify
    from .sandbox import Isolation

    isolation = Isolation(getattr(args, "verify_isolation", None) or "strict")
    session = _verification_sessions(args)

    def verifier(owner_dir, halt_id, now):
        return ddverify.verify_if_due(args.root, owner_dir, halt_id, session=session,
                                      isolation=isolation, now=now)
    return verifier


def _verification_sessions(args):
    """`session(block)` for `ddverify`: opens, for one block, the registered temporary root of its
    project (the storage state root must be initialised and admit a write; the block's run or
    project must be a registered project) and yields `make(purpose, name, estimate_bytes)`, a
    context manager for one admitted, recorded workspace that is given back when it ends. A
    refusal is a `ddverify.VerifyRefused`: nothing is measured, nothing is recorded."""
    from contextlib import contextmanager

    from . import ddverify

    shared: dict = {}

    @contextmanager
    def sessions(block):
        facts = block.get("facts") or {}
        try:
            if "admission" not in shared:
                shared["admission"], shared["root"] = storage_admission_for(
                    getattr(args, "storage_root", None))
            ephemeral, how = _open_ephemeral(
                shared["root"], str(facts.get("project_name") or block["object_id"]),
                str(facts.get("repo_path") or ""), f"ddv-{block['halt_id']}", shared["admission"])
        except StorageRefused as exc:
            raise ddverify.VerifyRefused(f"storage: {exc}"[:300]) from exc
        if ephemeral is None:
            raise ddverify.VerifyRefused(
                f"no registered temporary root for this block ({how}): nowhere outside the run "
                f"to measure")

        @contextmanager
        def make(purpose, name, estimate_bytes):
            try:
                workspace = ephemeral.roots.create(creator="hoh.ddverify", purpose=purpose,
                                                   name=name, estimate_bytes=estimate_bytes)
            except Exception as exc:                      # noqa: BLE001 - admission, ownership
                raise ddverify.VerifyRefused(
                    f"temporary workspace: {type(exc).__name__}: {exc}"[:300]) from exc
            try:
                yield workspace.path
            finally:
                ephemeral.roots.release(workspace)
        try:
            yield make
        finally:
            _close_ephemeral(ephemeral)

    return sessions


def cmd_double_down(args) -> int:
    """`hoh double-down verify|renew|apply|revert HALT_ID (--run RUN | --project PROJECT)` (P3-42).

    `verify`: measure the repair spec of the dossier's proposal on a scratch copy of the run's
    candidate and record RED_REPRODUCED, REFUTED or (when nothing could be measured) INCONCLUSIVE
    (`hoh.ddverify`); `--isolation strict` (the default) or `none`. `renew`: a person renews the
    bound a dossier is parked on, naming who and why (`doubledown.renew_bound`). `apply`: prove the
    fix of a RED_REPRODUCED proposal on a scratch copy and, when a person approves it
    (`--approved-by` and `--reason`; without them the fix is WITHHELD and nothing is measured),
    apply it as a new iteration of the blocked run (`hoh.ddapply`). `revert`: a person puts back the
    specification text an application superseded. Each prints one row. Exit 0: the verdict, the
    renewal, the decision or the reversal is on record; 1: decided but the record could not be
    written; 2: refused, withheld, or an attempt that measured nothing (one may be left).
    Only `apply` and `revert` write a run's specification or state, and only `apply` lifts its
    block; neither writes its candidate, receipts or results."""
    from . import ddverify, doubledown
    from .projectstore import ProjectStore
    from .sandbox import Isolation

    try:
        owner = (RunStore(args.root, args.run).dir if args.run
                 else ProjectStore(args.root, args.project).dir)
    except (StoreError, UnsafeRunId, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.dd_cmd == "renew":
        row = doubledown.renew_bound(owner, args.halt_id, by=args.by, reason=args.reason)
        _print(row)
        return 0 if row["action"] == "renewed" else 2
    if args.dd_cmd == "revert":
        from . import ddapply

        row = ddapply.revert(args.root, owner, args.halt_id, by=args.by, reason=args.reason)
        _print(row)
        return 0 if row["action"] in ("reverted", "closed") else 2
    if args.dd_cmd == "apply":
        from . import ddapply

        if bool(args.approved_by) != bool(args.reason):
            print("refused: --approved-by and --reason go together (a person's approval names who "
                  "and why)", file=sys.stderr)
            return 2
        row = ddapply.apply(args.root, owner, args.halt_id, session=_verification_sessions(args),
                            approved_by=args.approved_by, approval_reason=args.reason)
        _print(row)
        if row["action"] == "unrecorded":
            return 1
        decided = row["action"] in ("applied", "not_applied", "closed")
        return 0 if decided else 2           # withheld, refused, inconclusive: nothing is decided
    row = ddverify.verify(args.root, owner, args.halt_id, session=_verification_sessions(args),
                          isolation=Isolation(args.isolation))
    _print(row)
    if row["action"] == "unrecorded":
        return 1
    terminal = (row["action"] in ("red_reproduced", "refuted", "closed", "inconclusive")
                and "attempts_left" not in row)
    return 0 if terminal else 2


def _analyst_sessions(args):
    """`analysts(halt)` for the double-down pass: a context manager that, for one block, opens
    what an analyst session may live in and yields the confined analysts.

    In this order, each refusal an `AnalystRefused` (nothing is dispatched, the row says why):
    the harness must be one a role approval policy can be expressed to; the storage state root
    must be initialised and admit a write; the block's run or project must be a registered
    project (its registered temporary root is where the session works -- outside the run and
    the project directories); a workspace of that root is created, admitted and recorded
    before the session starts, and given back when the round ends."""
    from contextlib import contextmanager

    from . import doubledown

    harness = args.analyst_harness
    shared: dict = {}

    @contextmanager
    def sessions(halt):
        if harness not in doubledown.CONFINABLE:
            raise doubledown.AnalystRefused(
                f"harness {harness!r} cannot be confined: a role approval policy can be "
                f"expressed to {', '.join(sorted(doubledown.CONFINABLE))} only")
        try:
            if "admission" not in shared:
                shared["admission"], shared["root"] = storage_admission_for(
                    getattr(args, "storage_root", None))
            admission, state_root = shared["admission"], shared["root"]
            ephemeral, how = _open_ephemeral(
                state_root, str(halt.facts.get("project_name") or halt.object_id),
                str(halt.facts.get("repo_path") or ""), f"dd-{halt.halt_id}", admission)
        except StorageRefused as exc:
            raise doubledown.AnalystRefused(f"storage: {exc}"[:300]) from exc
        if ephemeral is None:
            raise doubledown.AnalystRefused(
                f"no registered temporary root for this block ({how}): an analyst has nowhere "
                f"outside the run to work")
        try:
            try:
                workspace = ephemeral.roots.create(
                    creator="hoh.doubledown", purpose="reviewer", name=f"dd-{halt.halt_id}",
                    estimate_bytes=doubledown.ANALYST_SCRATCH_ESTIMATE)
            except Exception as exc:                      # noqa: BLE001 - admission, ownership
                raise doubledown.AnalystRefused(
                    f"temporary workspace: {type(exc).__name__}: {exc}"[:300]) from exc
            try:
                yield doubledown.build_analysts(
                    workspace.path, harness=harness, model=args.analyst_model,
                    effort=args.analyst_effort, timeout_s=args.analyst_timeout)
            finally:
                ephemeral.roots.release(workspace)
        finally:
            _close_ephemeral(ephemeral)

    return sessions


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
    when the state is better than CRITICAL and a role run, held together with
    the iteration's temporary workspace, would be admitted now; otherwise a new
    timer is armed and the branch stays parked. The boot is claimed first.
    Nothing is cleaned, ever.
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


def cmd_storage_new_target(args) -> int:
    """`hoh storage new-target --parent-project Q --path ABS --storage-root PATH`:
    the registry creates an EMPTY directory for a target clone
    (`StorageRegistry.create_external_root`, kind `target_clone`, the only call
    made): its index entry is written `PENDING` before the directory exists and
    `CREATED` after, and its parent must be one the operator listed in the state
    root's `CONFIG.json`. The caller then clones into it and registers it
    (`hoh storage register-project`). Exit 0 with `{"storage_root", "root"}`
    printed; exit 2 with `storage new-target refused: <message>` on stderr.
    `--storage-root` is required and has no fallback; the state root is never
    initialised here."""
    from .storage_registry import StorageOwnershipError, StorageRegistry

    if not os.path.isabs(args.path):
        print(f"storage new-target refused: --path {args.path!r} is not an absolute path",
              file=sys.stderr)
        return 2
    root = Path(args.storage_root).expanduser()
    try:
        entry = StorageRegistry(root).create_external_root(
            args.parent_project, args.path, "target_clone")
    except (StorageOwnershipError, OSError) as exc:
        print(f"storage new-target refused: {exc}", file=sys.stderr)
        return 2
    _print({"storage_root": str(root), "root": entry})
    return 0


def cmd_storage_register_project(args) -> int:
    """`hoh storage register-project --project P --repo ABS --storage-root PATH`:
    register a FRESH project whose repository is a target clone the registry
    created (`hoh storage new-target`) -- `StorageRegistry.register_project(P,
    ABS, fresh_target=True)`, the only call made. Create-only: a project id or a
    root that is registered already is refused, never returned. Exit 0 with
    `{"storage_root", "project"}` printed; exit 2 with `storage register-project
    refused: <message>` on stderr. `--storage-root` is required and has no
    fallback; the state root is never initialised here."""
    from .storage_registry import StorageOwnershipError, StorageRegistry

    if not os.path.isabs(args.repo):
        print(f"storage register-project refused: --repo {args.repo!r} is not an absolute path",
              file=sys.stderr)
        return 2
    root = Path(args.storage_root).expanduser()
    try:
        record = StorageRegistry(root).register_project(args.project, args.repo,
                                                        fresh_target=True)
    except (StorageOwnershipError, OSError) as exc:
        print(f"storage register-project refused: {exc}", file=sys.stderr)
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


class SessionHeld(StorageRefused):
    """The run's temporary-root session is held by a live lease of another process: that
    process is executing this run (it holds the run's process marker, which `cmd_run` takes
    before any storage code -- this is the same answer by construction should the lease ever be
    seen first). Nothing is written: the run's state is that process's, and a refused caller
    must not pause it."""


def _open_ephemeral(root: Path, project_name: str, repo_path: str, run_id: str, admission):
    """``(session, how)``: the registered temporary root of this ``hoh run``
    process (P2-42) -- a `storage_ephemeral.RunSession`, the registry lease of
    this process for the run's session and the project's ``tmp`` root for it,
    registered before anything else is written -- and how the run's project was
    found (``name`` or ``repository``); or ``(None, why)`` when the run belongs
    to no registered project (there is no root of a project to put a workspace
    under: the caller says so and the runner's sibling scratch is used, as
    before). A run of a registered project whose session or root cannot be had
    is `StorageRefused`: it does not start and does not fall back to a scratch
    beside the arena; a session held by a **live lease of another process** is
    `SessionHeld` (a different thing: nobody's storage is wrong, the run is being
    executed)."""
    from .storage_ephemeral import RunSession, project_for_run
    from .storage_registry import LIVE_HOLDER, StorageRegistry

    try:
        registry = StorageRegistry(root)
        project_id, how = project_for_run(registry, project_name, repo_path)
        if project_id is None:
            return None, how
        return RunSession.begin(registry, project_id, run_id, admission=admission), how
    except Exception as exc:                              # noqa: BLE001 - fail closed
        if getattr(exc, "reason", None) == LIVE_HOLDER:
            raise SessionHeld(f"Run {run_id} is already being executed by another process: "
                              f"the run's temporary-root session holds a live lease ({exc})") from exc
        raise StorageRefused(f"{STORAGE_UNAVAILABLE}: the temporary root of the run's project "
                             f"cannot be opened: {type(exc).__name__}: {exc}") from exc


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


def cmd_storage_account(args) -> int:
    """`hoh storage account --project P [--storage-root PATH] [--budget SECONDS] [--no-tiers]`:
    the storage account of the project (I10b, `hoh.storage_account`): bytes and entries of its
    registered namespace per root, run, session, tier and reclaimable now, printed as JSON. Read
    only: no session, no lease, no admission, nothing written (the state root is never created).
    A partial account is printed with `complete: false` and exit 0 -- a partial report is never
    a verdict. Exit 2 with `storage account refused: <why>` on stderr and nothing on stdout for a
    budget outside (0, 3600], a state root that is not initialised and a project that is not
    registered."""
    import math

    from . import storage_account
    from .storage_registry import StorageRegistry

    root = resolve_storage_root(getattr(args, "storage_root", None))
    budget = args.budget
    if not (isinstance(budget, float) and math.isfinite(budget)
            and 0 < budget <= storage_account.MAX_BUDGET_S):
        print(f"storage account refused: --budget must be a number of seconds in "
              f"(0, {storage_account.MAX_BUDGET_S:g}], not {budget!r}", file=sys.stderr)
        return 2
    registry = StorageRegistry(root)
    if not registry._owner_valid():
        print("storage account refused: " + STORAGE_NOT_INITIALISED.format(root=root),
              file=sys.stderr)
        return 2
    if registry._project_record(args.project) is None:
        print(f"storage account refused: project {args.project!r} is not registered",
              file=sys.stderr)
        return 2
    try:
        account = storage_account.take_account(
            registry, args.project, budget_s=budget, tiers=not getattr(args, "no_tiers", False))
    except ValueError as exc:
        print(f"storage account refused: {exc}", file=sys.stderr)
        return 2
    # A name that is not UTF-8 reaches the account as a surrogate escape: ASCII JSON escapes
    # it (`\udcXX`); the shared `_print` (ensure_ascii=False) would crash on it.
    print(json.dumps({"storage_root": str(root), "account": account}, indent=2,
                     ensure_ascii=True))
    return 0


# --------------------------------------------------------------------------- #
# The capacity registry's command (P3-16, slice 2)
# --------------------------------------------------------------------------- #


def _capacity_refused(verb: str, why: str) -> int:
    """`capacity <verb> refused: <why>` on stderr, bounded and escaped (`capacity.printable`); exit code 2."""
    from .capacity import printable

    print(f"capacity {verb} refused: {printable(why)}", file=sys.stderr)
    return 2


def _capacity_target(args, verb: str):
    """`(state_root, record_file, None)` for the resolved state root (`--storage-root`, else `$HOH_STATE_ROOT`,
    else the XDG default: the resolution of `hoh run`), or `(None, None, 2)` once the refusal is printed: an empty
    `--storage-root`, or a directory that is not an initialised VeriHarness state root
    (`StorageRegistry._require_init`: a valid `OWNER.json` of this owner, plain `projects/` and `ledger/`).
    Nothing is created."""
    from . import capacity
    from .storage_registry import StorageOwnershipError, StorageRegistry

    option = getattr(args, "storage_root", None)
    if option is not None and option == "":
        return None, None, _capacity_refused(verb, "--storage-root is empty (it would fall back to another root)")
    root = resolve_storage_root(option)
    try:
        registry = StorageRegistry(root)
        registry._require_init()
    except (StorageOwnershipError, OSError, ValueError):
        return None, None, _capacity_refused(verb, STORAGE_NOT_INITIALISED.format(root=root))
    return registry.state_root, capacity.location(registry.state_root), None


@contextmanager
def _capacity_lock(root: Path):
    """An exclusive, non-blocking `flock` on the state root directory itself: no lock file is made (the record is
    the one file `hoh capacity` creates). Any holder is refused (`CapacityError`, `busy`), never waited for: another
    `hoh capacity set`, or the runner's shared lock on an owner-only ancestor of a check's scratch."""
    import fcntl

    from .capacity import CapacityError

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CapacityError("busy", "the state root is locked by another process; run it again") from exc
        yield
    finally:
        os.close(fd)


def cmd_capacity_set(args) -> int:
    """`hoh capacity set KIND NAME LIMIT UNIT [--storage-root PATH]`: declare one capacity in the host's capacity
    registry, `<state root>/capacity-registry.json` (P3-16 slice 2). The entry is `declared` (what the operator
    states; `measured` is a probe's) and `as_of` is the clock of this command -- the module reads none. The
    registry is read, the entry put in place of the one of its kind and name, and the whole record written through
    `capacity.write`, all under one non-blocking lock. An absent record starts an empty registry; any other
    record that `capacity.read` refuses (corrupt, non-canonical, oversize, a link, a directory ...) is refused
    and left exactly as it is -- the command never replaces what it cannot read. Exit 0 with a JSON object
    (`storage_root`, `path`, `durable`, `entry`, `replaced`, `entries`, `digest`) on stdout; exit 2 with
    `capacity set refused: <why>` on stderr and the old record in place. `durable: false` (and a warning) means
    the new record is in place and only its directory entry may not be durable. The state root is never
    created or initialised here."""
    from datetime import datetime, timezone

    from . import capacity

    root, path, refused = _capacity_target(args, "set")
    if refused is not None:
        return refused
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    durable = True
    try:
        entry = capacity.Entry(kind=args.kind, name=args.name, limit=capacity.parse_limit(args.limit),
                               unit=args.unit, source="declared", as_of=as_of)
        with _capacity_lock(root):
            try:
                current = capacity.read(path)
            except capacity.CapacityError as exc:
                if exc.reason != "missing":
                    raise
                current = None
            entries, previous = capacity.set_entry(current, entry)
            try:
                capacity.write(path, entries)
            except DurableWriteNotSynced as exc:
                durable = False
                print(f"warning: the capacity registry is written, but its directory entry may not be "
                      f"durable: {capacity.printable(str(exc))}", file=sys.stderr)
    except capacity.CapacityError as exc:
        return _capacity_refused("set", str(exc))
    except OSError as exc:
        return _capacity_refused("set", f"the old record stands; {type(exc).__name__}: {exc}")
    print(json.dumps({
        "storage_root": str(root), "path": str(path), "durable": durable, "entry": entry.as_dict(),
        "replaced": None if previous is None else previous.as_dict(), "entries": len(entries),
        "digest": capacity.parse(capacity.render(entries)).digest,
    }, indent=2, ensure_ascii=True))
    return 0


def cmd_capacity_show(args) -> int:
    """`hoh capacity show [--source] [--storage-root PATH]`: the host's capacity registry as JSON (`storage_root`,
    `path`, `digest`, `entries`), read through `capacity.read` -- so a record it refuses is refused here with its
    reason, and an absent record is a refusal, never an empty registry. Read-only: no lock, nothing is created or
    written. Exit 0, or 2 with `capacity show refused: <why>` on stderr and nothing on stdout. The output is ASCII
    JSON of at most `MAX_ENTRIES` entries; stderr text is bounded and escaped.

    `--source` (P3-16 slice 3) adds the **capacity source** under the key `source`: the snapshot of the registry,
    of the provider state the runs under `--root` say (`health.read_health`) and of the slots in use there
    (`capacitysource.take`), each fact with its observation time, its source, its age and its bound, and UNKNOWN
    (no value) where it is older than the bound or could not be read. Without the flag the output is exactly what it
    was."""
    from datetime import datetime, timezone

    from . import capacity, capacitysource

    root, path, refused = _capacity_target(args, "show")
    if refused is not None:
        return refused
    try:
        current = capacity.read(path)
    except capacity.CapacityError as exc:
        return _capacity_refused("show", str(exc))
    shown = {"storage_root": str(root), "path": str(path), **capacity.view(current)}
    if getattr(args, "source", False):
        # `now` is read after the registry was (broker.decide: a fact written after `now` is `future`)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        shown["source"] = capacitysource.view(
            capacitysource.take(current, runs_root=Path(args.root).expanduser(), now=now), now=now)
    print(json.dumps(shown, indent=2, ensure_ascii=True))
    return 0


def cmd_capacity_check(args) -> int:
    """`hoh capacity check KIND NAME AMOUNT UNIT [--max-age-declared S] [--max-age-measured S] [--storage-root PATH]`
    (P3-17 slice 2): may AMOUNT UNIT of the capacity KIND/NAME be taken now? One `broker.decide` over the
    registry of the state root (`capacity.read`), the live holds the product records under the runs root
    (`--root`: the run leases of every project and the open budget reservations, `capacitycheck.live_holds`) and
    the clock of this command. Read-only: no lock, nothing created or written. Exit 0 `grant`, 3 `wait` (fits
    once what is held is given back), 4 `refuse` (the reason is in the JSON); exit 2 with
    `capacity check refused: <why>` on stderr and nothing on stdout when no decision can be taken (a root that is
    not an initialised state root, a registry or a hold source that cannot be read, a malformed argument).
    stdout is one ASCII JSON object: `storage_root`, `path`, `policy` (the ages used), `holds` and `decision`.
    A `grant` is a judgement at this moment, not a reservation. **Sessions read zero until something takes a run
    lease**: nothing in `src/hoh` acquires one yet (P3-18 slice 2), so on a real host a `grant` beside
    `machine/host.sessions` does not count the sessions that are running.

    Since P3-16 slice 3 the decision is taken on the capacity source (`capacitysource.take` / `decide`): what the
    provider state of the runs under `--root` says narrows a wanted `provider` capacity by holding part of it
    (`narrowed` in the JSON: the capacity, the rule, the amount held and why), so a limited provider is `wait`, and the
    registry digest the decision quotes stays the record's. Holds that are UNKNOWN make no decision (exit 2)."""
    from datetime import datetime, timezone

    from . import capacity, capacitycheck, capacitysource
    from .broker import Amount, BrokerError

    root, path, refused = _capacity_target(args, "check")
    if refused is not None:
        return refused
    try:
        want = Amount(args.kind, args.name, capacity.parse_limit(args.amount), args.unit)
        ages = {}
        for key, option, default in (("declared", args.max_age_declared, capacitycheck.DEFAULT_DECLARED_AGE),
                                     ("measured", args.max_age_measured, capacitycheck.DEFAULT_MEASURED_AGE)):
            ages[key] = default if option is None else capacity.parse_limit(option)
        used = capacitycheck.policy(**ages)
        registry = capacity.read(path)
        # `now` is read after the registry was (broker.decide: a fact written after `now` is `future`)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = capacitysource.take(registry, runs_root=Path(args.root).expanduser(), now=now, policy=used)
        decision, narrowed = capacitysource.decide(snapshot, [want], now=now)
    except (capacity.CapacityError, BrokerError, capacitycheck.CheckError) as exc:
        return _capacity_refused("check", str(exc))
    print(json.dumps({"storage_root": str(root), "path": str(path), "policy": ages,
                      "holds": snapshot.holds.as_dict(), "narrowed": [item.as_dict() for item in narrowed],
                      "decision": decision.as_dict()}, indent=2, ensure_ascii=True))
    return {"grant": 0, "wait": 3, "refuse": 4}[decision.verdict]


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


#: P3-11 slice 2: the three options that together name an environment requirement.
_PREFLIGHT_OPTIONS = ("preflight_report", "preflight_profile", "preflight_max_age")


def _add_preflight_options(parser) -> None:
    parser.add_argument(
        "--preflight-report", metavar="PATH", default=None,
        help="the report `tools/preflight.py --out` wrote: this command starts only if it says "
             "READY for --preflight-profile and is at most --preflight-max-age seconds old; "
             "otherwise it exits 2 and writes nothing. The three options go together.")
    parser.add_argument(
        "--preflight-profile", metavar="NAME", default=None,
        help="the profile the report must have been measured for (`demo`, `unattended`)")
    parser.add_argument(
        "--preflight-max-age", metavar="SECONDS", type=int, default=None,
        help="the oldest the report may be, in seconds (1 to 604800)")


def _preflight_gate(args) -> int | None:
    """The environment preflight of a run start (P3-11 slice 2): None to go on, else the exit code.

    No option, no gate: what every start was. Given, the gate is asked **first** -- before a run
    directory, a process marker, a storage admission or a tab -- and a refusal is exit 2 with the
    reason on stderr, written nowhere else: a terminal, visible halt by name, never a skipped
    check and never a retry (nothing here loops; the report is read once per start)."""
    given = [getattr(args, name, None) for name in _PREFLIGHT_OPTIONS]
    if all(value is None for value in given):
        return None
    if any(value is None for value in given):
        print("environment preflight refused: --preflight-report, --preflight-profile and "
              "--preflight-max-age go together (a report with no profile or no age is no gate)",
              file=sys.stderr)
        return 2
    from .preflightgate import PreflightRefused, PreflightRequirement, check_preflight

    try:
        requirement = PreflightRequirement(report=args.preflight_report,
                                           profile=args.preflight_profile,
                                           max_age_seconds=args.preflight_max_age)
    except ValueError as exc:
        print(f"environment preflight refused: {exc}", file=sys.stderr)
        return 2
    try:
        found = check_preflight(requirement)
    except PreflightRefused as exc:
        print(f"the environment preflight does not allow a start: {exc}. "
              f"`tools/preflight.py --profile {requirement.profile} "
              f"--out {ascii(str(requirement.report))}` writes a new report"[:600],
              file=sys.stderr)
        return 2
    _print({"preflight": {"profile": found.profile, "measured_at": found.measured_at,
                          "age_seconds": found.age_seconds}})
    return None


def cmd_start(args) -> int:
    refused = _preflight_gate(args)
    if refused is not None:
        return refused
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
    # P3-20 slice 2: a policy offered at the start is validated before the run exists and
    # pinned to it right after; a refusal creates nothing.
    start_policy = None
    if getattr(args, "routing_policy", None) is not None:
        from . import routing as routing_mod

        try:
            start_policy = routing_mod.load(
                os.path.abspath(os.path.expanduser(str(args.routing_policy))))
        except routing_mod.RoutingRefused as exc:
            print(f"routing policy refused: {exc}", file=sys.stderr)
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
    if start_policy is not None:
        if (rc := _write_record(store.dir / ROUTING_RECORD,
                                _routing_pin_record(start_policy, "hoh start"))) is not None:
            print(f"run {run_id} EXISTS and is NOT bound to the routing policy: `hoh run "
                  f"{run_id} --routing-policy {start_policy.source}` binds it; a `hoh run` "
                  "without the option would run it unrouted", file=sys.stderr)
            return rc
        _print({"routing_policy": {"digest": start_policy.digest,
                                   "source": start_policy.source, "pinned": True}})
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
    from . import routing as routing_mod

    failover_seen = routing_mod.failover_summary(store.dir)
    _print(
        {
            **({"routing_failover": failover_seen} if failover_seen is not None else {}),
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
    # P3-18 slice 2: a run another driver holds is not touched, `--force` or not.
    from . import rundrive

    if (held := rundrive.refusal_for_resume(args.root, args.run_id)) is not None:
        print(held, file=sys.stderr)
        return 2
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


#: What a role runs under when neither `--planner/--developer/--qa` nor a routing policy says.
_DEFAULT_PROFILE = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}


class _UnsetProfile(str):
    """The parser default of `--planner/--developer/--qa`: the harness name a role runs on when
    nobody says (`"kimi"` for QA -- by default QA runs on a different model than the developer),
    distinguishable from the same word typed. A routing policy's provider may fill an unset one
    and must not replace a typed one. It is a `str`, equal to the word and hashing as it, so
    every reader of `args.qa` sees exactly what it saw before P3-20 slice 2; `profile_typed`
    is the one question that tells the two apart.

    The mark is an **attribute** of the value, read by name, not the identity of this class:
    `python3 -m hoh.cli` runs this module as `__main__` and `detach` imports a second copy as
    `hoh.cli`, whose `_UnsetProfile` is another class -- an `isinstance` test across the two
    would read every default as typed and type all three options into a detached tab."""

    __slots__ = ()
    hoh_unset_default = True


def profile_typed(value) -> bool:
    """Was a role option given on the command line? `None` (an args object that does not carry
    the option) and the parser default are not."""
    return value is not None and not getattr(value, "hoh_unset_default", False)


def _profile_of(role: Role, value) -> str:
    """The harness a role runs on: the typed word, else the parser's default. A typed empty or
    blank value is refused here -- before the run directory is touched and before anything is
    built or pinned -- and never replaced by the default (it used to reach the first dispatch
    and fail there: "no harness configured for role")."""
    if value is None:
        return _DEFAULT_PROFILE[role]
    if not str(value).strip():
        raise ValueError(f"--{role.value} names no harness: an empty value is refused "
                         "(leave the option out for the default)")
    return str(value)


#: P3-20 slice 2. The run directory's record of the routing policy the run is bound to.
ROUTING_RECORD = "routing.json"
ROUTING_PIN_SCHEMA = "hoh-routing-pin/1"
_ROUTING_PIN_MAX_BYTES = 64 * 1024


class RoutingPinRefused(ValueError):
    """The run's routing record cannot be read, or the policy offered is not the one the run is
    bound to. Said by name; nothing is re-routed silently."""


def _read_routing_pin(run_dir) -> dict | None:
    """`<run>/routing.json` as a dict; None when the run has none. Anything that is there and is
    not one small regular, link-free file holding a UTF-8 JSON object of this schema with a
    sha256 digest and an absolute source path is refused: a damaged pin is not "no pin"."""
    import re

    from .durable_io import UnsafeRecordFile, read_single_link

    path = Path(run_dir) / ROUTING_RECORD
    try:
        raw = read_single_link(path)
    except UnsafeRecordFile:
        raise RoutingPinRefused(
            f"{ROUTING_RECORD} is a link or not one regular file; the policy a run is bound to "
            "lives in its run directory") from None
    except OSError as exc:
        raise RoutingPinRefused(
            f"{ROUTING_RECORD} cannot be read ({type(exc).__name__})") from None
    if raw is None:
        return None
    if len(raw) > _ROUTING_PIN_MAX_BYTES:
        raise RoutingPinRefused(f"{ROUTING_RECORD} is larger than {_ROUTING_PIN_MAX_BYTES} bytes")
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RoutingPinRefused(f"{ROUTING_RECORD} is not one UTF-8 JSON document") from None
    if not (isinstance(rec, dict) and rec.get("schema") == ROUTING_PIN_SCHEMA
            and isinstance(rec.get("digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", rec["digest"])
            and isinstance(rec.get("source"), str) and os.path.isabs(rec["source"])):
        raise RoutingPinRefused(
            f"{ROUTING_RECORD} is not a routing pin of this version (schema "
            f"{ROUTING_PIN_SCHEMA}, a sha256 digest, an absolute source path)")
    return rec


def _routing_decide(run_dir, offered):
    """`(policy | None, pin | None)` for a run: the policy `hoh run` / `hoh start` works under.

    Nothing offered and nothing pinned: no routing (what every run had before). Offered and
    nothing pinned: the offered policy, to be pinned by the caller. Pinned: the policy whose
    digest is the pinned one -- the offered file, or, with none offered, the file the pin names;
    a different digest, or a pinned file that is gone or invalid, is `RoutingPinRefused`. Reads
    only; the caller writes the pin. `routing.RoutingRefused` is raised for an offered file that
    is not a valid policy."""
    from . import routing

    pin = _read_routing_pin(run_dir)
    if offered is None and pin is None:
        return None, None
    if offered is None:
        try:
            policy = routing.load(pin["source"])
        except routing.RoutingRefused as exc:
            raise RoutingPinRefused(
                f"routing policy changed: this run is bound to policy {pin['digest'][:12]} "
                f"({pin['source']}), which cannot be read again: {exc}") from None
    else:
        policy = routing.load(os.path.abspath(os.path.expanduser(str(offered))))
    if pin is not None and policy.digest != pin["digest"]:
        raise RoutingPinRefused(
            f"routing policy changed: this run is bound to policy {pin['digest'][:12]} "
            f"({pin['source']}), but {'the policy offered' if offered is not None else 'that file'}"
            f" declares {policy.digest[:12]}; a bound run is not re-routed -- start a new run "
            "for another policy")
    return policy, pin


def _routing_pin_record(policy, by: str) -> dict:
    from .contracts import utcnow

    return {"schema": ROUTING_PIN_SCHEMA, "digest": policy.digest,
            "file_sha256": policy.file_sha256, "source": policy.source, "pinned_by": by,
            "pinned_at": utcnow(), "policy": policy.evidence()}


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
        path.parent.mkdir(parents=True, exist_ok=True)
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


def _close_ephemeral(session) -> None:
    """End the lease of the run's temporary-root session; a failure is said on
    stderr and changes no exit code (the run's work is done)."""
    why = session.close()
    if why:
        print(f"closing the temporary-root session {session.session_id!r} failed: {why}",
              file=sys.stderr)


def _surface_run_dirs(args) -> list[Path]:
    """The run directories a `hoh surface` command looks at: the named run, or
    every run under the root (a listing of the root, nothing else is read)."""
    if args.run_id:
        return [RunStore(args.root, args.run_id).dir]
    return [RunStore(args.root, name).dir for name in list_runs(args.root)]


def cmd_surface_open(args) -> int:
    """`hoh surface open RUN`: open a Herdr tab for the run and record it as the
    run's own (`surfaces.open_surface`). Refuses, creating nothing, for a run that
    does not exist or is already over (a checkpointed run is not)."""
    from . import herdr as herdr_mod
    from . import surfaces
    from .controller import DispatchError

    try:
        store = _store(args)
    except StoreError as exc:
        print(f"surface open refused: {exc}", file=sys.stderr)
        return 2
    if not store.state_exists():
        print(f"surface open refused: there is no run {args.run_id} under {args.root}",
              file=sys.stderr)
        return 2
    over, why = surfaces.run_over(store.dir)
    if over:
        print(f"surface open refused: the run is already over ({why}); a surface "
              "opened now would have nothing to wait for", file=sys.stderr)
        return 2
    try:
        program = store.read_state().project_name
        surface = surfaces.open_surface(store.dir, role=args.role, label=args.label,
                                        cwd=args.cwd, program=program)
    except ValueError as exc:
        print(f"surface open refused: {exc}", file=sys.stderr)
        return 2
    except (herdr_mod.HerdrError, DispatchError, StoreError, OSError) as exc:
        print(f"surface open failed: {exc}", file=sys.stderr)
        return 2
    _print({"run_id": args.run_id, "role": f"program:{args.role}",
            "pane_id": surface.pane_id, "tab_id": surface.tab_id,
            "registry": str(store.dir / surfaces.SURFACES_FILE)})
    return 0


def cmd_surface_list(args) -> int:
    """`hoh surface list [RUN]`: the surfaces the registries own or owned. Asks
    Herdr nothing."""
    from . import surfaces

    try:
        dirs = _surface_run_dirs(args)
    except StoreError as exc:
        print(f"surface list refused: {exc}", file=sys.stderr)
        return 2
    rows: list[dict] = []
    for run_dir in dirs:
        if os.path.lexists(run_dir / surfaces.SURFACES_FILE):
            rows.extend(surfaces.listing(run_dir))
    _print({"surfaces": rows})
    return 0


def cmd_surface_reap(args) -> int:
    """`hoh surface reap [RUN] [--close]`: for every run that is over and has owned
    surfaces, close them (`--close`) or say what would be closed. Exit 1 when a
    surface is refused (unprovable), 2 when Herdr cannot be reached."""
    from . import herdr as herdr_mod
    from . import surfaces
    from .controller import DispatchError

    try:
        dirs = _surface_run_dirs(args)
    except StoreError as exc:
        print(f"surface reap refused: {exc}", file=sys.stderr)
        return 2
    results: list[dict] = []
    failed = False
    pane, tab, inside = surfaces.own_location()
    for run_dir in dirs:
        if not os.path.lexists(run_dir / surfaces.SURFACES_FILE):
            continue
        try:
            results.extend(surfaces.reap(
                run_dir, close=bool(args.close),
                self_pane=pane, self_tab=tab, self_inside=inside))
        except (herdr_mod.HerdrError, DispatchError, OSError) as exc:
            print(f"surface reap failed for {run_dir.name}: {exc}", file=sys.stderr)
            failed = True
            break
    _print({"close": bool(args.close), "surfaces": results})
    if failed:
        return 2
    return 1 if any(r["outcome"] == "refused" for r in results) else 0


def cmd_surface_register(args) -> int:
    """`hoh surface register --project P --kind reviewer|drafter|orchestrator [--pane ID]
    [--strand S --perspective a] [--round N] [--candidate FULL-SHA] [--storage-root PATH]`:
    record a surface that exists (the pane; `--pane` is required for a reviewer or a drafter, an
    orchestrator defaults to `$HERDR_PANE_ID`) in the project's surface book, after Herdr's
    snapshot showed it. Exit 0 with the record on stdout; exit 2 with
    `surface register refused: <reason>` on stderr and nothing written."""
    from . import herdr as herdr_mod
    from . import surfacebook, surfaces
    from .controller import DispatchError

    try:
        if not args.pane and args.kind in (surfacebook.KIND_REVIEWER, surfacebook.KIND_DRAFTER):
            raise surfacebook.Refused(
                "--pane is required for a reviewer or a drafter: the default, $HERDR_PANE_ID, is "
                "the pane of the process that registers (the orchestrator's), not the agent's")
        pane = args.pane or surfaces.own_location()[0]
        registry = surfacebook.open_registry(resolve_storage_root(args.storage_root))
        info = surfacebook.register(
            registry=registry, project_id=args.project, control=surfaces.make_control(),
            kind=args.kind, pane_id=pane, strand=args.strand, perspective=args.perspective,
            round_=args.round, candidate=args.candidate, into=args.into)
    except surfacebook.Refused as exc:
        print(f"surface register refused: {exc}", file=sys.stderr)
        return 2
    except (herdr_mod.HerdrError, DispatchError, OSError) as exc:
        print(f"surface register refused: {exc}", file=sys.stderr)
        return 2
    _print(info)
    return 0


def cmd_surface_release(args) -> int:
    """`hoh surface release --project P --strand S --reason TEXT [--storage-root PATH]`: the
    explicit terminal fact of a strand -- its reviewers and drafters are over and the next
    `hoh reap --apply` closes them. Idempotent."""
    from . import surfacebook

    try:
        registry = surfacebook.open_registry(resolve_storage_root(args.storage_root))
        info = surfacebook.release_strand(registry=registry, project_id=args.project,
                                          strand=args.strand, reason=args.reason)
    except (surfacebook.Refused, OSError) as exc:
        print(f"surface release refused: {exc}", file=sys.stderr)
        return 2
    _print(info)
    return 0


def cmd_reap(args) -> int:
    """`hoh reap [--project P] [--storage-root PATH] [--runs-root DIR]... [--strand S] [--apply]`:
    judge every surface of the project's surface book and of the runs roots by its lifecycle
    (a reviewer's strand is merged or released; a run workspace's runs have all ended; an
    orchestrator is never reaped) and, with `--apply`, close those the reaper proves safe. A dry
    run otherwise. Exit 1 when a surface is refused (unprovable), 2 when the input cannot be used
    or Herdr cannot be reached."""
    from . import herdr as herdr_mod
    from . import surfacebook, surfaces
    from .controller import DispatchError

    roots = [Path(r).expanduser() for r in (args.runs_roots or [])]
    if not roots and not args.project:
        roots = [Path(args.root).expanduser()]
    pane, tab, inside = surfaces.own_location()
    try:
        registry = (surfacebook.open_registry(resolve_storage_root(args.storage_root))
                    if args.project else None)
        result = surfacebook.sweep(registry=registry, project_id=args.project or "",
                                   roots=roots, strand=args.strand or "",
                                   apply=bool(args.apply), self_pane=pane, self_tab=tab,
                                   self_inside=inside)
    except surfacebook.Refused as exc:
        print(f"reap refused: {exc}", file=sys.stderr)
        return 2
    except (herdr_mod.HerdrError, DispatchError, OSError) as exc:
        print(f"reap failed: {exc}", file=sys.stderr)
        return 2
    # ASCII JSON: a reason may quote a name from a record, and a record is not trusted to be encodable
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if any(r["outcome"] == "refused" for r in result["surfaces"]) else 0


def cmd_run(args) -> int:
    """`hoh run`, with a process marker around the whole of it (review B3, C):
    a replacement master can tell a run still being executed between two
    iterations -- when its lock is free -- from one whose process is gone."""
    detaching = (getattr(args, "detach", False) or getattr(args, "detach_timeout", None) is not None
                 or getattr(args, "require_contract", False))
    # P3-INV1: the dispatch ceiling is checked before anything is written or opened (a tab
    # included: `detach.run_argv` types it into the tab's own `hoh run`).
    ceiling = getattr(args, "dispatch_ceiling", None)
    if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, int)
                                or ceiling < 1):
        print(f"--dispatch-ceiling must be a whole number of at least 1, not {ceiling!r}",
              file=sys.stderr)
        return 2
    # P3-11 slice 2: the environment is asked before anything is written or opened -- before a
    # tab as well: a refused start is refused here, where the operator is, and the tab's own
    # `hoh run` (the options are typed into it, `detach.run_argv`) asks again when it starts.
    refused = _preflight_gate(args)
    if refused is not None:
        return refused
    if detaching:
        # P5-42 slice 1: the run starts in a tab of its own (`hoh.detach`); nothing below runs here.
        from . import detach
        return detach.cmd_detach(args)
    store = RunStore(args.root, args.run_id)
    if not store.state_exists():
        return _cmd_run(args)
    # P3-18 slice 2: the run's lease is held for as long as this process drives it, taken inside the process
    # lock and before the run body writes anything; a lease another holder has refuses the run. Slice 3: a driver a
    # master started (`--master-epoch`) says so in its lease.
    from . import rundrive

    try:
        with store.process_marker(), rundrive.drive_lease(
                args.root, args.run_id, master_epoch=getattr(args, "master_epoch", None)) as lease:
            args.run_lease = lease
            rc = _cmd_run(args)
    except LockBusy as exc:
        if "already being executed" not in str(exc):
            raise
        print(str(exc), file=sys.stderr)
        return 2
    except rundrive.DriveRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # The marker is released, so `run_over` can now say the run is over: the
    # first moment the surfaces opened for it (`hoh surface open`) can be closed.
    # Never raises, never changes `rc`; a run with no surfaces record does nothing.
    from . import surfacebook, surfaces
    surfaces.reap_after_run(store.dir)
    # ... and the workspaces `hoh worktree` recorded for this runs root, once every run there has
    # ended (`surfacebook.runs_over`). Same promise: never raises, never changes `rc`.
    surfacebook.reap_root_after_run(store.dir.parent)
    return rc


def _cmd_run(args) -> int:
    """Runs iterations. The operator path that was missing until now.

    Without this command `hoh start` created a run that stayed in NEW:
    `run_iteration` was called from tests only, which left A01/A02/A12
    impossible to demonstrate.

    What the run opens that has to be closed on every way out -- the registered
    session of its temporary root (P2-42) -- is closed by `scope`, however
    `_run_in` returns or raises.
    """
    with ExitStack() as scope:
        return _run_in(args, scope)


def _lease_lost(args) -> str | None:
    """P3-18 slice 2: why the keepalive of this `hoh run` gave its run lease up, or None."""
    lease = getattr(args, "run_lease", None)
    return None if lease is None or not lease.lost else f"run lease lost: {lease.lost}"


def _run_in(args, scope: ExitStack) -> int:
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
        # P2-42: the temporary root is part of the same decision. Opened last,
        # so a refusal above leaves nothing open.
        ephemeral, matched = _open_ephemeral(storage_root, state.project_name or state.run_id,
                                             state.repo_path, state.run_id, storage_admission)
    except SessionHeld as exc:
        # Another process is executing this run: nothing is written (no NEEDS_YOU, no pause), and
        # the words are the ones the launcher already reads as "in flight".
        print(str(exc), file=sys.stderr)
        return 2
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
    if ephemeral is not None:
        # registered at once: nothing below, a print included, can leave the lease open
        scope.callback(_close_ephemeral, ephemeral)
    _print({"storage_admission": {"state_root": str(storage_root),
                                  "ledger": str(storage_admission.ledger)}})
    if ephemeral is not None:
        state.note(f"temporary roots: project {ephemeral.project_id!r} (by {matched}), "
                   f"session {ephemeral.session_id!r}: every acceptance check, every "
                   "iteration and every role's scratch (TMPDIR) of this process works in "
                   "a workspace of its tmp root")
        _print({"ephemeral_roots": {"project": ephemeral.project_id,
                                    "session": ephemeral.session_id,
                                    "root": str(ephemeral.root), "matched_by": matched}})
    else:
        state.note(f"temporary roots: {matched}; the runner's sibling scratch "
                   "(<arena>.scratch, .hoh-scratch-<receipt>) is used")
        _print({"ephemeral_roots": {"state": "not registered", "why": matched,
                                    "scratch": "runner sibling scratch"}})

    try:
        profile = {
            Role.PLANNER: _profile_of(Role.PLANNER, args.planner),
            Role.DEVELOPER: _profile_of(Role.DEVELOPER, args.developer),
            Role.QA: _profile_of(Role.QA, args.qa),
        }
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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
    # P3-20 slice 2. The routing policy is decided here, before any role can start, and pinned
    # to the run (below) after the last refusal that precedes the first dispatch: a refused
    # invocation -- the policy not fitting the roles' options, an approval policy, a contract,
    # a recovery declaration, a trust grant, the dispatcher, the isolation -- leaves no pin.
    from . import routing as routing_mod

    routing_policy = None
    failover = None
    try:
        routing_policy, routing_pin = _routing_decide(
            store.dir, getattr(args, "routing_policy", None))
        if routing_policy is not None:
            for role_, given in ((Role.PLANNER, args.planner), (Role.DEVELOPER, args.developer),
                                 (Role.QA, args.qa)):
                declared = routing_policy.roles.get(role_)
                if (not profile_typed(given) and declared is not None
                        and declared.provider is not None):
                    profile[role_] = declared.provider      # an unset profile takes the policy's
            routed = routing_mod.resolve(routing_policy, profiles=profile, models=models,
                                         efforts=efforts)
            # P3-41 slice 2. The failover decision is *planned* here, from the verified provider
            # health without this run's own park, and committed below -- after the master is
            # declared and before the first dispatch -- so that a stale master and an invocation
            # refused in between leave no record. A policy with no fallback and a run with no
            # record read no health and change nothing.
            from . import dispatchrecovery as _recovery
            from .health import read_verified_health

            try:
                master_ = (_recovery.check_master_args(
                    getattr(args, "master_session", None), getattr(args, "master_epoch", None))
                    or _recovery.read_master(store.dir, recorded_only=True))
            except ValueError:
                master_ = None          # refused in full below, before anything is committed
            failover = routing_mod.plan_failover(
                routing_policy, run_dir=store.dir, profiles=profile, models=models,
                efforts=efforts, reader=read_verified_health, master=master_,
                begun=bool(state.iteration or state.usage.iterations))
            scope.callback(failover.close)
            if failover.decided:
                routed = failover.resolved
    except RoutingPinRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except routing_mod.RoutingRefused as exc:
        print(f"routing policy refused: {exc}", file=sys.stderr)
        return 2
    if routing_policy is not None:
        _print({"routing_policy": {"digest": routing_policy.digest,
                                   "source": routing_policy.source,
                                   "pinned_before": routing_pin is not None,
                                   "resolved": routed.evidence["resolved"],
                                   **({"failover": routed.evidence["failover"]["decisions"]}
                                      if failover is not None and failover.decided else {}),
                                   **({"verification_required": routed.evidence["verification"]}
                                      if failover is not None and failover.decided
                                      and "verification" in routed.evidence else {}),
                                   **({"failover_not_decided": failover.stayed}
                                      if failover is not None and failover.stays else {})}})
        if failover is not None and failover.stays:
            print(f"routing failover: not decided ({failover.stayed}); the run starts on its "
                  "declared route", file=sys.stderr)
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
        except OSError as exc:
            print(f"NEEDS_YOU: the autonomy contract could not be pinned: {exc}",
                  file=sys.stderr)
            return 2
        if governor is not None:
            asked = getattr(args, "program_epoch", None)
            if asked is not None and governor.contract.program_epoch != asked:
                print(f"--program-epoch {asked} ignored: this run is pinned at epoch "
                      f"{governor.contract.program_epoch}", file=sys.stderr)
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
    # P3-41 slice 4: a role that failed over to an `account` route runs under that route's
    # operator-registered handle, so its receipts and the health reading name it. A handle typed
    # for the same role with --role-account (`hoh start` forwards it to every `hoh run`) named the
    # declared route's account: it is replaced by the route's handle, and said -- a provider stop
    # is not turned into a refusal by an option the operator typed for the route it left.
    if failover is not None and failover.decided:
        for role_, handle in failover.resolved.accounts.items():
            if role_ in recovery.accounts and recovery.accounts[role_] != handle:
                print(f"routing failover: --role-account for {role_.value} is not used: the role "
                      f"runs under the account route {handle}", file=sys.stderr)
            recovery.accounts[role_] = handle
    if getattr(args, "trust_worktree", False):
        from .trust import TrustRefused, trust_run_worktree
        try:
            granted = trust_run_worktree(state.repo_path)
        except TrustRefused as exc:
            print(f"worktree trust refused: {exc}", file=sys.stderr)
            return 2
        if (rc := _write_record(approval_dir / "worktree_trust.json", granted)) is not None:
            return rc
        _print({"worktree_trust": granted})
    try:
        # a decided failover: the dispatcher is built from the plan's own tables and providers
        # (`routing=` would assert the declared provider against them)
        failed_over = failover is not None and failover.decided
        dispatcher = build_dispatcher(
            answers_dir=store.dir / "answers",
            profiles=failover.resolved.profiles if failed_over else profile,
            cwd=state.repo_path,
            prefer_herdr=not args.no_herdr,
            timeout_ms=args.role_timeout * 1000,
            models=failover.resolved.models if failed_over else models,
            efforts=failover.resolved.efforts if failed_over else efforts,
            approval_policy=policy,
            routing=None if failed_over else routing_policy,
            # P2-42: a run of a registered project keeps its roles' scratch in the iteration's
            # registered workspace (`Controller._run_temp` binds it); any other run keeps the
            # `.hoh-scratch-role-<role>` directories of the arena root.
            defer_role_scratch=ephemeral is not None,
        )
    except Exception as exc:   # DispatchError and HerdrUnavailable
        print(str(exc), file=sys.stderr)
        return 2
    if failed_over:
        dispatcher.routing_evidence = failover.resolved.evidence

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

    # P3-41 slice 2: the failover decision is made durable here -- after the master declaration
    # (a stale master was refused above, with nothing written) and before the first dispatch. A
    # decision that cannot be recorded is a refusal, never a silent fallback.
    made_now = failover is not None and failover.pending
    if made_now:
        try:
            failover.commit()
        except routing_mod.RoutingRefused as exc:
            print(f"routing policy refused: {exc}", file=sys.stderr)
            return 2
    if made_now and failover.decided:
        said = ", ".join(
            f"{r} runs on {d['route']['provider']}/{d['route'].get('model', '-')}"
            f" ({d['reason']} at the declared provider)"
            for r, d in failover.resolved.evidence["failover"]["decisions"].items()
            if d["outcome"] == "FALLBACK")
        if said:
            state.note(f"routing failover: {said}")
            with store.lock():
                store.write_state(state)
            print(f"routing failover: {said}", file=sys.stderr)

    # P3-20 slice 2: the pin, last, after every refusal above; nothing has been dispatched.
    if routing_policy is not None and routing_pin is None and (rc := _write_record(
            store.dir / ROUTING_RECORD,
            _routing_pin_record(routing_policy, "hoh run"))) is not None:
        return rc

    # P3-INV1: what this invocation was told, read from the arguments (not back from the controller: the
    # tests that drive `cmd_run` with stand-in controllers have none to give)
    told_ceiling = getattr(args, "dispatch_ceiling", None)
    controller = Controller(
        store, dispatcher, spec_path=state.spec_path, isolation=isolation,
        storage_admission=storage_admission,
        steward=_hook_steward(storage_root, state.project_name or state.run_id,
                              storage_admission),
        recovery=recovery,
        ephemeral_roots=ephemeral.roots if ephemeral is not None else None,
        dispatch_ceiling=told_ceiling,
    )
    if told_ceiling is not None:
        _print({"dispatch_ceiling": {"ceiling": told_ceiling,
                                     "dispatched": state.usage.dispatches,
                                     "run_max_dispatches": state.budgets.max_dispatches}})

    ran = 0
    waiting_for_approval = False
    lease_lost = False
    while ran < args.iterations:
        if (gone := _lease_lost(args)) is not None:
            # P3-18 slice 2: a holder that was replaced starts no further iteration.
            print(f"{gone}; no further iteration is started", file=sys.stderr)
            lease_lost = True
            break
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

    if not lease_lost and (gone := _lease_lost(args)) is not None:
        print(f"{gone}; the run ended under it", file=sys.stderr)      # the last iteration ran under no lease
        lease_lost = True
    if not _integrity_close(store, integrity_before):
        return 3

    # Clear away our own tabs -- otherwise they would be left standing after
    # every run and clutter up the multiplexer the captain works in. Then lift
    # the QA seals, then prune (`_end_of_run`), and report the state on disk.
    payload, rc = _finish_run(store, dispatcher, controller, state,
                              waiting_for_approval=waiting_for_approval,
                              run_id=args.run_id, ran=ran)
    _print(payload)
    return 2 if lease_lost and rc == 0 else rc


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


def cmd_wait(args) -> int:
    """`hoh wait [RUN | --report PATH | --pid N] [--until ...] [--timeout S] [--poll S]`:
    block until the run is at rest, the review report is final, or the process has ended.

    Read-only: it reads the run's state and its process lock (a run), a report through the
    merge gate's reader (`--report`) or `/proc/N/stat` (`--pid`), and writes nothing but one
    JSON line on stdout (`hoh.runwait`). Exactly one target is named; `--until` is for a run
    only. Run: exit 0 accepted or the asked-for stage or condition reached, 1 failed or
    cancelled, 3 blocked, 4 parked, 5 paused, 6 idle, 7 died, 8 held. Report: 0 final and
    RELEASE_YES (not a merge approval), 1 final and no, 8 partial or invalid. Process: 0
    ended, 9 not there at the first look. 124 timeout; 2 refused or the command itself
    failed (no JSON line, also when the answer cannot be written). Exit 0 and 1 always come
    with a JSON line: a stdout that is closed at the start is exit 2 before any look.
    """
    from . import runwait

    if sys.stdout is None:                            # `>&-`: print would do nothing and exit 0 say "answered"
        print("wait refused: stdout is closed, the answer could not be written", file=sys.stderr)
        return runwait.REFUSED
    report, pid, run_id = args.report, args.pid, args.run_id
    named = [name for name, value in (("RUN", run_id), ("--report", report), ("--pid", pid))
             if value is not None]
    try:
        if len(named) != 1:
            raise runwait.WaitRefused(
                "name exactly one target: RUN, --report PATH or --pid N"
                + (f" (got {' and '.join(named)})" if named else ""))
        if report is not None:
            if args.until is not None:
                raise runwait.WaitRefused("--until applies to a run only, not to --report")
            result = runwait.wait_report(report, timeout=args.timeout, poll=args.poll)
            line = runwait.render_report(result)
        elif pid is not None:
            if args.until is not None:
                raise runwait.WaitRefused("--until applies to a run only, not to --pid")
            result = runwait.wait_pid(pid, timeout=args.timeout, poll=args.poll)
            line = runwait.render_pid(result)
        else:
            until = runwait.parse_until(args.until)
            result = runwait.wait(_store(args), until, timeout=args.timeout, poll=args.poll)
            line = runwait.render(result)
    except (runwait.WaitRefused, StoreError) as exc:
        print(f"wait refused: {exc}", file=sys.stderr)
        return runwait.REFUSED
    except Exception as exc:                          # noqa: BLE001 - exit 1 is an outcome, never a crash
        print(f"wait failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return runwait.REFUSED
    try:
        print(line, flush=True)
    except (OSError, ValueError) as exc:               # stdout closed or full: the answer was not given
        print(f"wait failed: the answer could not be written: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        try:                       # so that the flush at exit does not fail again: stdout goes to a pipe nobody reads
            _unread, sink = os.pipe()
            os.dup2(sink, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return runwait.REFUSED
    return result.exit_code


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


def cmd_merge_gate(args) -> int:
    """`hoh merge-gate check|verify` (A6, requirement row P5-36): may this candidate be merged?

    `check` decides over the required review reports and writes the decision as a receipt that is
    created and never replaced; `verify` decides the receipt's inputs again, now. Neither merges
    anything. Exit 0: eligible / verified. Exit 1: a decision was made and it is no (the receipt
    exists). Exit 2: no decision (bad input, unreadable repository, a receipt that cannot be
    created, a failure of the gate itself): nothing on stdout, no receipt. The logic and its
    contract are in `hoh.mergegate`.
    """
    from . import mergegate

    if args.merge_gate_cmd == "verify":
        outcome = mergegate.run_verify(args.receipt, repo=args.repo, candidate=args.candidate,
                                       base=args.base)
    else:
        outcome = mergegate.run_check(
            repo=args.repo, candidate=args.candidate, base=args.base, spec=args.spec,
            reviews=args.review, receipt=args.receipt, min_reviews=args.min_reviews,
            min_age=args.min_age, suite_receipt=args.suite_receipt)
    sys.stdout.write(outcome.out)
    sys.stderr.write(outcome.err)
    return outcome.code


def cmd_phase(args) -> int:
    """`hoh phase close|status|reopen` (amendment A7): a phase is closed at a commit.

    `close PHASE --record ENTRY` verifies the closure entry against the blobs at its closing
    commit and writes the receipt (exit 0; exit 1 refused, nothing written). `status [PHASE]`
    reads the committed receipt and re-derives it: CLOSED at the commit with the regression watch
    beside it, or OPEN / REOPENED with the reason (exit 0 every phase asked about is closed, 1 one
    is not, 2 the repository cannot be judged). `reopen PHASE --request FILE --reason TEXT`
    records the operator's decision to reopen (exit 0; exit 1 refused). Nothing commits, and
    nothing else reopens a phase. The logic and its contract are in `hoh.phases`.
    """
    from . import phases

    if args.phase_cmd == "close":
        outcome = phases.run_close(repo=args.repo, phase=args.phase, record=args.record,
                                   gate_dir=args.gate_dir)
    elif args.phase_cmd == "reopen":
        outcome = phases.run_reopen(repo=args.repo, phase=args.phase, request=args.request,
                                    reason=args.reason, gate_dir=args.gate_dir)
    else:
        outcome = phases.run_status(repo=args.repo, phase=args.phase, gate_dir=args.gate_dir,
                                    as_json=args.json)
    sys.stdout.write(outcome.out)
    sys.stderr.write(outcome.err)
    return outcome.code


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


def cmd_mission(args) -> int:
    """`hoh mission overview [--json]` (rows P3-33/37/38, slice 1): one read-only look at every run
    under the root, row by row, from what the product already writes (`hoh.missioncontrol`).
    `hoh mission board|needs-you --project ID [--json]` (rows P3-35/36, slice 1): the same for one
    project -- what runs, what can run, what waits, what waits for a person.
    `hoh mission map|timeline|evidence --project ID [--json]` (rows P3-34/37/38): the project's
    graph as a structure, its dated records in order, and the health of its gates and evidence.
    Exit 0 everything looked at, 1 something could not be (a row or a listed log that says why),
    2 refused (the root or the project is not usable, or the arguments do not fit the view).
    Writes nothing."""
    from . import missioncontrol

    return missioncontrol.run(args.view, args.root, project=args.project, as_json=args.json)


def cmd_pin(args) -> int:
    """`hoh pin` (A6 I6, requirement row P5-38): a spec-only pin commit by plumbing on a base the product resolved.

    Exit 0: the pin and its receipt exist. Exit 2: refused (nothing was published). Exit 3: the pin exists but
    its receipt could not be written. The logic and its contract are in `hoh.pin`.
    """
    from . import pin

    outcome = pin.run_pin(
        repo=args.repo, base=args.base, files_root=args.files_root, spec=args.spec, name=args.ref,
        message_file=args.message_file, co_author=args.co_author, receipt=args.receipt, allow=args.allow)
    sys.stdout.write(outcome.out)
    sys.stderr.write(outcome.err)
    return outcome.code


def cmd_import(args) -> int:
    """`hoh import` (A6 I6, requirement row P5-38): fetch an accepted candidate, prove its tree, preview the merge.

    Exit 0: imported, every preview clean. Exit 1: imported, a preview is a conflict or an error. Exit 2: refused
    (no ref). Exit 3: the import ref exists but its receipt could not be written. The logic is in `hoh.importref`.
    """
    from . import importref

    outcome = importref.run_import(
        repo=args.repo, run_state=args.run_state, commit=args.commit, name=args.id, receipt=args.receipt,
        base=args.base, against=args.against, against_unmerged=args.against_unmerged, spec=args.spec)
    sys.stdout.write(outcome.out)
    sys.stderr.write(outcome.err)
    return outcome.code


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
        except (LockBusy, migrations.MigrationError, OSError, StoreError,
                *migrations.UNPARSEABLE) as exc:
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


def _project_leases(args) -> int:
    """`hoh project leases [--archive]` (P3-18 slice 3): the run leases `hoh run` keeps under `--root`, one line each,
    judged now; with `--archive` the ones that ended at least `--older-than-days` ago are moved aside (never
    removed) under `projects/hoh-runs/run-leases-archive/<stamp>/` with a manifest. Exit 0; 3 when a lease file
    cannot be read exactly (listed, never skipped); 4 when the archive is refused."""
    from . import rundrive
    from .leaseadmin import MAX_ARCHIVE_FILES
    from .runlease import RunLeaseError

    try:
        if args.archive:
            done = rundrive.archive(args.root, args.older_than_days * 86400)
            if done.directory is None:
                print(f"nothing to archive: no lease ended {args.older_than_days} day(s) ago or more")
            else:
                print(f"archived {len(done.moved)} file(s) of {len(done.runs)} run(s) to {done.directory}")
            for run_id in done.deferred:
                print(f"deferred {run_id}: a pass moves at most {MAX_ARCHIVE_FILES} files; run it again")
            for run_id, why in done.skipped:
                print(f"skipped {run_id}: {why}")
            return 0
        rows = rundrive.listing(args.root)
    except (RunLeaseError, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 4
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
        lease = row.lease
        detail = ("" if lease is None else
                  f" gen={lease.generation} holder={lease.holder} epoch={lease.master_epoch or '-'}"
                  f" parked={row.parked}")
        print(f"{row.run_id}  {row.state:10s}{detail}  {'reclaimable' if row.reclaimable else ''}".rstrip())
    print(f"{len(rows)} lease(s): " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
    return 3 if counts.get("UNREADABLE") else 0


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
    from . import rundrive
    from .projectstore import (
        ProjectStore, UnsafeProjectId, list_projects, record_external_action, resume_decision,
        unblock,
    )

    if args.project_cmd == "leases":
        return _project_leases(args)

    if args.project_cmd == "list":
        projekte = list_projects(args.root)
        if not projekte:
            print(f"no projects under {args.root}")
            return 0
        for pid in projekte:
            try:
                st = ProjectStore(args.root, pid).read_state()
                verdict_text, _ = resume_decision(st, rundrive.consult_nodes(args.root, st))
                open_ = sum(1 for n in st.nodes if not n.settled)
                print(f"{pid}  {verdict_text:9s}  nodes={len(st.nodes)} open={open_} "
                      f"closure_gen={st.closure_generation}")
            except StoreError as exc:
                print(f"{pid}  UNREADABLE: {exc}")
        return 0

    try:
        store = ProjectStore(args.root, args.project_id)
    except UnsafeProjectId as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
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

    if args.project_cmd == "budget":
        return _project_budget(args, store)

    if args.project_cmd == "wake":
        return _project_wake(args, store, st)

    if args.project_cmd == "route-decisions":
        return _project_route_decisions(args, store)

    if args.project_cmd == "continue":
        return _project_continue(args, store)

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

    # P3-18 slice 3: what the RUNNING nodes' run leases say (read-only).
    verdict_text, why = resume_decision(st, rundrive.consult_nodes(args.root, st))

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


def _project_route_decisions(args, store) -> int:
    """`hoh project route-decisions ID --autonomy PATH` (row P3-31, slice 1): route the open
    EXTERNAL decisions of a project through an autonomy contract and record the ones the
    contract hands to an automatic resolver (`governance.route_project_decisions`).

    It approves nothing and acts on nothing: the record names a resolver. A decision the
    contract does not hand to one stays open for the operator. Needs the live master's
    credential (or, on a governed project with no master alive, an operator epoch of its
    own) unless `--dry-run`, which writes nothing. Exit 0: every open decision routed or
    left open for a reason that is a fact about it; 1: something could not be read or
    recorded (the report says what); 2: refused (contract file, digest, graph); 5: no
    authority."""
    from . import governance
    from .store import StaleWrite
    from .succession import LeaseError

    try:
        contract = governance.load_contract_file(args.autonomy, program_epoch=args.program_epoch)
    except governance.RoutingRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    authority, operator = None, False
    if not args.dry_run:
        authority, operator, refusal = _command_authority(args, store)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 5
    try:
        with _authority_section(store, authority, operator, "route-decisions"):
            report = governance.route_project_decisions(
                store, contract, args.root, expect_digest=args.expect_digest,
                dry_run=args.dry_run)
    except governance.RoutingRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (StaleWrite, LeaseError, LockBusy, StoreError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    _print(report.as_json())
    return 1 if report.failed else 0


def _project_continue(args, store) -> int:
    """`hoh project continue ID --autonomy PATH --wave WAVE` (row P3-32, slice 1): what a wave
    may continue under the contract, written down once (`governance.continue_by_policy`).

    Consumes the routing records of `route-decisions` only as a way out for the decision they
    name -- never as an approval -- and holds the wave, with the reason, when the contract is
    not pinned by the project's runs, a limit is reached, a node has no way out, or the wave
    itself must hold. Dispatches nothing: the loop reads the record. Authority as
    `route-decisions` (`--dry-run` takes none and writes nothing). Exit 0: the record says
    CONTINUE; 1: it says HOLD or could not be written (the record or the report says why);
    2: refused before anything was read of a log; 5: no authority."""
    from . import governance
    from .store import StaleWrite
    from .succession import LeaseError

    try:
        contract = governance.load_contract_file(args.autonomy, program_epoch=args.program_epoch)
    except governance.RoutingRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    authority, operator = None, False
    if not args.dry_run:
        authority, operator, refusal = _command_authority(args, store)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 5
    try:
        with _authority_section(store, authority, operator, "continue"):
            result = governance.continue_by_policy(
                store, contract, args.root, wave_id=args.wave, dry_run=args.dry_run)
    except governance.RoutingRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except governance.ContinuationError as exc:
        print(f"not recorded: {exc}", file=sys.stderr)
        return 1
    except (StaleWrite, LeaseError, LockBusy, StoreError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    _print({**result.as_json(), "dry_run": args.dry_run})
    return 0 if result.outcome == "CONTINUE" else 1


def _project_wake(args, store, st) -> int:
    """The master consults its predecessor: print the wake plan, and with
    `--execute` open a surface, resume the predecessor's Claude session in it and
    send it one prompt. Exit 0: planned or woken; 2: a value of the command line or
    the environment is not usable; 5: not the live master, the succession records
    do not bear it out, or the wake failed after a surface was opened (it is left
    open, never closed). See `hoh.wake`."""
    from . import wake

    authority, refusal = _claimed_master(args, store)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 5
    try:
        plan = wake.plan_wake(authority, store.dir, project_id=st.project_id,
                              resume_id=args.resume_id, brief=args.brief,
                              answer=args.answer, nonce=wake.new_nonce(), cwd=args.cwd,
                              token_file=os.path.expanduser(args.token_file or ""))
        wake.validate_timeout(args.timeout)
        if not args.execute:
            print(json.dumps({**plan.as_dict(), "executed": False}, indent=2))
            return 0
        if not args.predecessor_surface_closed:
            raise wake.WakeUsage(
                "--execute needs --predecessor-surface-closed: a wake resumes the "
                "predecessor's session, which must not be running in another surface "
                "(if it is, message that surface instead)")
        from .herdr import HerdrUnavailable

        try:
            control = wake.make_control()
        except HerdrUnavailable as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        done = wake.perform_wake(plan, control, timeout_s=args.timeout,
                                 predecessor_surface_closed=True,
                                 workspace=os.environ.get("HERDR_WORKSPACE_ID") or None)
    except wake.WakeFailed as exc:
        out = {"outcome": "FAILED", "detail": str(exc), "agent": plan.agent_name,
               "agent_started": exc.agent_started,
               "pane_id": exc.surface.pane_id if exc.surface else None,
               "tab_id": exc.surface.tab_id if exc.surface else None,
               "answer_file": plan.answer_file,
               "hint": (
                   "the predecessor's session may be running in that surface now (it may "
                   "still write the answer file): do NOT run this command again with "
                   "--predecessor-surface-closed while it is open -- message that surface, "
                   "or close it yourself first"
                   if exc.agent_started else
                   "no agent was started; the surface (if any) is yours to close, and the "
                   "command may be run again")}
        print(json.dumps(out, indent=2))
        print("refused: the wake failed; any surface it opened is left open (this command "
              "never closes a predecessor's surface)", file=sys.stderr)
        return 5
    except wake.WakeUsage as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except wake.WakeRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    print(json.dumps({**plan.as_dict(), "executed": True, **done}, indent=2))
    return 0


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


def cmd_project_create(args) -> int:
    """Create the governed record of a project: the state file that `hold`, `rollover`
    and `rollover-accept` need, and nothing else. Idempotent -- the project that exists
    is reported, never replaced -- and it refuses a repository that is not the one the
    record was made for. Exit 0 created or already there, 2 refused, 3 unreadable."""
    from .project import ProjectState
    from .projectstore import _NAME_RE, ProjectStore

    # a whole name: `fullmatch`, because `$` also matches before a final newline and
    # `projects/x\n` would be a directory of its own
    if not isinstance(args.project_id, str) or not _NAME_RE.fullmatch(args.project_id):
        print(f"refused: Invalid project id {args.project_id!r}: expected "
              "[A-Za-z0-9][A-Za-z0-9._-]{0,63}", file=sys.stderr)
        return 2
    try:
        repo = Path(args.repo).expanduser().resolve()
        store = ProjectStore(args.root, args.project_id)
    except (OSError, RuntimeError, ValueError, StoreError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if not repo.is_dir() or not (repo / ".git").exists():
        print(f"refused: {repo} is not a git checkout; a governed record names the "
              "repository whose HEAD a capsule binds", file=sys.stderr)
        return 2
    try:
        existed = store.exists()
        if existed:
            st = store.read_state()
        else:
            with store.lock():
                created_state = store.create(ProjectState(project_id=args.project_id,
                                                          repo_path=str(repo)))
            st = created_state
    except LockBusy as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except StoreError as exc:
        print(f"UNREADABLE: {exc}", file=sys.stderr)
        return 3
    if st.repo_path != str(repo):
        print(f"refused: project {st.project_id} already exists for the repository "
              f"{st.repo_path}, not {repo}; nothing was changed", file=sys.stderr)
        return 2
    _print({"created": not existed, "project_id": st.project_id, "state": str(store.state_path),
            "repo_path": st.repo_path, "nodes": len(st.nodes),
            "program_generation": st.program_generation, "write_seq": st.write_seq,
            "master_epoch": st.master_epoch})
    return 0


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
    from .rollover import RolloverError, hand_token_over, hold_lease, period_problem
    from .succession import LeaseError, LeaseHeld, MasterAuthority

    if problem := period_problem("--interval", args.interval):
        # before the lease is taken: a renewal period that is not positive and finite
        # would busy-renew or fail after the acquire (`sleep(nan)`, `sleep(inf)`,
        # `sleep(-1)`, `sleep(1e308)`) and leave a LOST lease behind
        print(f"refused: {problem}", file=sys.stderr)
        return 2
    rotation, refusal = _rotation_policy(args, store)
    if refusal:
        # also before the lease is taken: a policy that cannot be applied is no policy
        print(f"refused: {refusal}", file=sys.stderr)
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
                        hold_for=args.hold_for,
                        on_tick=_rotation_tick(store, authority, rotation, args.token_out,
                                               deliver=not args.rotate_no_deliver))
    _print({"project_id": st.project_id, "ended": result.reason,
            "renewals": result.renewals, "released": result.released})
    return 0


def _rotation_policy(args, store):
    """(loaded policy, None), (None, None) when no policy was asked for, or (None, why).

    The policy is read, and the capsule input it names checked (against this project's
    directory too: the handoff must be a file only the master writes), before any lease is
    taken or any capsule acknowledged; and it is armed only where its successor can be
    started, which is inside Herdr. `--rotate-policy-sha256` (the successor's pointer
    carries it) names the digest of the policy file that started the rotation: a file that
    changed since is refused, so that the policy that authorised a rotation is the policy
    its successor runs under."""
    import re

    from . import herdr as herdr_mod
    from .rotation import MIN_TTL_WITH_DELIVERY_S, PolicyError, load_policy

    want = getattr(args, "rotate_policy_sha256", None)
    if not getattr(args, "rotate_policy", None):
        if want is not None:
            return None, ("--rotate-policy-sha256 names the digest of the policy file and "
                          "needs --rotate-policy")
        if getattr(args, "rotate_no_deliver", False):
            return None, ("--rotate-no-deliver opts out of a rotation policy's notices and "
                          "needs --rotate-policy")
        return None, None
    if want is not None and not re.fullmatch(r"[0-9a-f]{64}", want):
        return None, f"--rotate-policy-sha256 must be 64 lower-case hex digits, got {want!r}"
    if getattr(args, "watch_pid", None) is None:
        return None, ("a rotation policy needs --watch-pid: the hold must know when the "
                      "session it rotates is gone, or a dead master's lease would be kept alive")
    if (not getattr(args, "rotate_no_deliver", False)
            and getattr(args, "ttl", None) is not None and args.ttl < MIN_TTL_WITH_DELIVERY_S):
        return None, (f"--ttl {args.ttl} is shorter than {MIN_TTL_WITH_DELIVERY_S} s: a notice "
                      "may block a renewal for up to 45 s while Herdr hangs; use a longer lease "
                      "or --rotate-no-deliver")
    try:
        loaded = load_policy(args.rotate_policy, directory=store.dir)
    except PolicyError as exc:
        return None, str(exc)
    if want is not None and loaded.sha256 != want:
        return None, (f"the rotation policy {loaded.path} is not the file that started this "
                      f"rotation (sha256 {loaded.sha256}, the pointer names {want}): it "
                      "changed since; nothing is applied")
    if not herdr_mod.available():
        return None, ("a rotation policy starts the successor in Herdr, and Herdr is not "
                      "available here (HERDR_ENV != 1, or no herdr on PATH)")
    return loaded, None


def _rotation_event(row: dict) -> None:
    """One event, one line, flushed: a monitor on this process reads them as they come."""
    try:
        print(json.dumps(row, sort_keys=True), flush=True)
    except (OSError, ValueError):
        pass                    # a closed stdout never ends a hold; the row is in the log


def _rotation_tick(store, authority, loaded, token_out, *, fresh=False, keepalive_s=None,
                   deliver=True):
    """The hold loop's `on_tick`: the rotation policy applied at every renewal, or None.

    The controller decides (`hoh.rotation`); the move is `run_rollover`, the same call
    `hoh project rollover` makes, with the successor's pointer carrying the policy so
    that it rotates the same way in its turn. `fresh` is the successor's: it starts in a
    cool-down (`RotationController.arm`). The rotation runs under the lease's keepalive
    (renewed every `keepalive_s`, default a third of the ttl): starting the successor can
    take longer than the lease is valid, and the hold loop cannot renew while it waits.

    `deliver` (on unless the operator passed `--rotate-no-deliver`): the controller types its
    requests and their outcomes into the pane this hold was started in (`HERDR_PANE_ID`, the
    master's own) through Herdr, besides the line on stdout; `False` is recorded in the log and
    the successor's command carries the opt-out on."""
    if loaded is None:
        return None
    from .rollover import HerdrNotifier, hand_token_over
    from .rotation import RotationController, make_trigger

    policy = loaded.policy

    def perform(inp, trigger):
        from .rollover import HerdrLauncher, run_rollover
        from .succession_capsule import LocalObserver

        st = store.read_state()
        with authority.keepalive(interval=keepalive_s):
            return run_rollover(
                authority, store.dir, inp=inp, observer=LocalObserver(),
                launcher=HerdrLauncher(agent_args=policy.successor_args(),
                                       prompt_delay_s=policy.prompt_delay_s),
                root=str(store.root), repo=st.repo_path, project_id=st.project_id,
                successor_session=new_run_id("succ"), trigger=trigger,
                safe=lambda: _program_lock_free(store), generation=st.program_generation,
                ack_timeout_s=policy.ack_timeout_s, takeover_s=policy.takeover_s,
                poll_s=policy.poll_s,
                accept_args=("--rotate-policy", str(loaded.path),
                             "--rotate-policy-sha256", loaded.sha256,
                             *(() if deliver else ("--rotate-no-deliver",))))

    controller = RotationController(
        loaded, directory=store.dir, project_id=store.project_id,
        session=authority.session_id, trigger=make_trigger(policy), perform=perform,
        on_reclaimed=lambda: hand_token_over(authority, token_out), emit=_rotation_event,
        notifier=HerdrNotifier(), pane_id=os.environ.get("HERDR_PANE_ID") or None,
        deliver=deliver)
    controller.arm(fresh=fresh)
    return controller.tick


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
        hand_token_over, period_problem, run_rollover,
    )
    from .succession_capsule import CapsuleInput, LocalObserver

    if problem := period_problem("--poll", args.poll):
        # before anything is frozen or launched: a poll the clock cannot sleep would
        # end the command after the successor was started
        print(f"refused: {problem}", file=sys.stderr)
        return 2
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
    from .rollover import RolloverError, Successor, hold_lease, period_problem
    from .succession import LeaseError, MasterAuthority
    from .succession_capsule import CapsuleError, LocalObserver, load_capsule

    for flag, value in (("--interval", args.interval), ("--poll", args.poll)):
        if problem := period_problem(flag, value):
            print(f"refused: {problem}", file=sys.stderr)
            return 2
    rotation, refusal = _rotation_policy(args, store)
    if refusal:
        # before the capsule is read or acknowledged: the predecessor then sees no
        # acknowledgement and stays master
        print(f"refused: {refusal}", file=sys.stderr)
        return 5
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
                        hold_for=args.hold_for,
                        on_tick=_rotation_tick(store, took.authority, rotation, args.token_out,
                                               fresh=True, deliver=not args.rotate_no_deliver))
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


def _project_budget(args, store) -> int:
    """`hoh project budget`: declare (or read back) the project's dispatch budget (P3-23).

    With `--dispatch-budget` the declaration `<project>/dispatch-budget.json` is written, under
    the command authority of `unblock` (the live master's credential, or an operator epoch when
    none is alive); without it the declaration is printed. Every drive binds it to its launcher.
    The epoch it records is the one it acted under (the master's, or the operator epoch it took),
    `null` on an ungoverned project. A declaration that is not readable is not repaired by this verb
    (it reads the current one first and refuses, exit 4): a person renames the file by a version.
    Exit 0 done, 2 usage, 4 refused value or unreadable declaration, 5 authority."""
    from dataclasses import asdict

    from . import budgetconfig
    from .succession import LeaseError, StaleMaster

    try:
        current = budgetconfig.read_config(store.dir)
        if args.dispatch_budget is None:
            if args.dispatch_reservation is not None or args.qa_reserve is not None:
                print("refused: --dispatch-reservation and --qa-reserve go with --dispatch-budget",
                      file=sys.stderr)
                return 2
            _print({"project_id": args.project_id,
                    "declared": None if current is None else asdict(current)})
            return 0
        if not args.reason:
            print("refused: --reason is required to declare a budget", file=sys.stderr)
            return 2
        budgetconfig.check_values(args.dispatch_budget, args.dispatch_reservation,
                                  args.qa_reserve or 0)
        authority, operator, refusal = _command_authority(args, store)
        if refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 5
        with _authority_section(store, authority, operator, "budget"):
            made = budgetconfig.write_config(
                store.dir, dispatch_budget=args.dispatch_budget,
                dispatch_reservation=args.dispatch_reservation, qa_reserve=args.qa_reserve or 0,
                set_by=args.session or f"operator-{os.getpid()}",
                epoch=getattr(authority, "epoch", None), reason=args.reason)
    except budgetconfig.BudgetConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 4
    except (LeaseError, StaleMaster, StoreError) as exc:     # superseded between the claim and the write
        print(f"refused: {exc}", file=sys.stderr)
        return 5
    _print({"project_id": args.project_id, "declared": asdict(made),
            "replaced": None if current is None else asdict(current)})
    return 0


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
    s.add_argument(
        "--routing-policy", metavar="PATH", default=None,
        help="a routing policy (schema hoh-routing-policy/1) the run is bound to from its "
             "start: model, effort and, optionally, provider per role. Validated before the "
             "run exists, pinned in <run>/routing.json; `hoh run` then works under it.")
    _add_preflight_options(s)
    s.set_defaults(func=cmd_start)

    r = sub.add_parser("run", help="Run iterations")
    r.add_argument("run_id")
    r.add_argument("--iterations", type=int, default=1, help="how many iterations")
    r.add_argument("--until-accepted", action="store_true",
                   help="stop after the first acceptance")
    r.add_argument("--planner", default=_UnsetProfile(_DEFAULT_PROFILE[Role.PLANNER]),
                   help="harness of the planner (default claude; a routing policy's provider "
                        "for the role fills it when the option is not typed)")
    r.add_argument("--developer", default=_UnsetProfile(_DEFAULT_PROFILE[Role.DEVELOPER]),
                   help="harness of the developer (default claude; a routing policy's "
                        "provider for the role fills it when the option is not typed)")
    r.add_argument("--qa", default=_UnsetProfile(_DEFAULT_PROFILE[Role.QA]),
                   help="harness of QA (default kimi; a routing policy's provider for the "
                        "role fills it when the option is not typed). A different model in a "
                        "fresh context is the strongest setup for the independent review")
    r.add_argument("--routing-policy", metavar="PATH", default=None,
                   help="a routing policy (schema hoh-routing-policy/1): model, effort and, "
                        "optionally, provider per role, read fail-closed. Pinned to the run "
                        "in <run>/routing.json on first use; a resumed run obeys the policy "
                        "it began under, and a policy with another digest is refused, never "
                        "applied. Explicit --role-model/--role-effort values win and are "
                        "recorded as overriding it.")
    r.add_argument("--dispatch-ceiling", type=int, default=None, metavar="N",
                   help="the most dispatches (the run's total, `usage.dispatches`) this process "
                        "may reach: the shared dispatch budget's hold for this launch, which a "
                        "launcher with a `dispatch_budget` types. Never written to the run's "
                        "state; the run's own `--max-dispatches` still applies, the lower of the "
                        "two first. N is a whole number of at least 1")
    _add_preflight_options(r)
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
    r.add_argument("--detach", action="store_true",
                   help="start the run in a Herdr tab of its own (an owned run host) and return "
                        "once its process has written a new marker; the output goes to the tab "
                        "and to logs/detached-<UTC>.log. Exit 0 started, 2 refused (nothing "
                        "created), 3 the tab exists but the run was not seen starting")
    r.add_argument("--detach-timeout", type=float, default=None, metavar="SEC",
                   help="with --detach: how long to wait for the run's process (1 to 600, "
                        "default 60)")
    r.add_argument("--require-contract", action="store_true",
                   help="with --detach: refuse unless the run has an autonomy contract, pinned "
                        "or offered with --autonomy, that `hoh run` would accept")
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
    fs.add_argument("--double-down", action="store_true",
                    help="double down on blocks (P3-42; ON by default, this flag changes nothing): "
                         "classify every blocked run and node, keep a create-only dossier under "
                         "dd/ and, with --analyst-harness, run analyst rounds; proposals and "
                         "escalations show in `hoh mission needs-you`")
    fs.add_argument("--no-double-down", action="store_true",
                    help="opt out of the double-down altogether for this call (the opt-out is "
                         "recorded in <root>/.double-down-optouts.jsonl and said in the output)")
    fs.add_argument("--no-verify-proposals", action="store_true",
                    help="opt out of measuring the repair spec of proposals (recorded); this "
                         "also leaves the application of fixes off, which needs the measurement")
    fs.add_argument("--apply-proposals", action="store_true",
                    help="record what is decided about a verified fix without a person: WITHHELD "
                         "for one, or NOT_APPLIED with a reason. The pass never applies a fix; only "
                         "`hoh double-down apply --approved-by` does (ON by default, this flag "
                         "changes nothing)")
    fs.add_argument("--no-apply-proposals", action="store_true",
                    help="opt out of recording what becomes of a verified fix (recorded): it is "
                         "measured and left for a person")
    fs.add_argument("--analyst-harness", choices=("claude", "codex", "kimi", "pi"), default=None,
                    help="with --double-down: the harness the two analysts of a round run on, "
                         "non-interactive and read-only under a role approval policy, in a "
                         "registered temporary workspace outside the run. Only claude can be "
                         "confined; another harness is refused, never started unconfined. "
                         "Without it dossiers are opened and no analyst is dispatched")
    fs.add_argument("--analyst-model", default=None, metavar="MODEL",
                    help="with --double-down: the analysts' model")
    fs.add_argument("--analyst-effort", default=None, metavar="LEVEL",
                    help="with --double-down: the analysts' effort level")
    fs.add_argument("--analyst-timeout", type=int, default=900, metavar="SEC",
                    help="with --double-down: deadline per analyst answer, 1 to 3600 "
                         "(default 900)")
    fs.add_argument("--storage-root", metavar="PATH", default=None,
                    help="with --analyst-harness: the storage state root whose admission and "
                         "registered temporary roots the analysts work under, as for `hoh run` "
                         "(default: $HOH_STATE_ROOT, else the XDG state directory). Without an "
                         "initialised root no analyst is started")
    fs.add_argument("--verify-proposals", action="store_true",
                    help="measure the repair spec of every proposal that carries one, on a "
                         "scratch copy of the run's candidate in a registered temporary "
                         "workspace, and record RED_REPRODUCED, REFUTED or INCONCLUSIVE (a "
                         "criterion is a command the analysts proposed: it runs only under the "
                         "runner's guards and --verify-isolation). ON by default, this flag "
                         "changes nothing")
    fs.add_argument("--verify-isolation", choices=("strict", "none"), default=None,
                    help="with --verify-proposals: how the criteria are executed (default "
                         "strict: a read-only candidate, no network; none: no sandbox)")
    fs.set_defaults(func=cmd_resume_quota)

    ddp = sub.add_parser(
        "double-down",
        help="Act on a double-down dossier: verify a proposal's repair spec, renew a bound, "
             "apply a proven fix, revert an application",
    )
    ddc = ddp.add_subparsers(dest="dd_cmd", required=True)
    for name, helptext in (
        ("verify", "Measure the repair spec of a PROPOSAL dossier on a scratch copy of the "
                   "run's candidate and record RED_REPRODUCED, REFUTED or INCONCLUSIVE"),
        ("renew", "A person renews the bound a dossier is parked on (names who and why)"),
        ("apply", "Prove the fix of a RED_REPRODUCED proposal on a scratch copy and, if the run's "
                  "autonomy contract allows it, apply it as a new iteration of the blocked run"),
        ("revert", "A person puts back the specification text an application superseded "
                   "(names who and why)"),
    ):
        dc = ddc.add_parser(name, help=helptext)
        dc.add_argument("halt_id", help="the dossier: dd/<halt_id> of the run or project")
        owner_ = dc.add_mutually_exclusive_group(required=True)
        owner_.add_argument("--run", default=None, help="the run whose dossier it is")
        owner_.add_argument("--project", default=None, help="the project whose dossier it is")
        if name in ("verify", "apply"):
            if name == "verify":
                dc.add_argument("--isolation", choices=("strict", "none"), default="strict",
                                help="how the criteria are executed (default strict)")
            dc.add_argument("--storage-root", metavar="PATH", default=None,
                            help="the storage state root whose admission and registered "
                                 "temporary roots the measurement works under, as for `hoh run`")
            if name == "apply":
                dc.add_argument("--approved-by", default=None, metavar="NAME",
                                help="a person's approval: the fix is proven on a scratch copy and, if "
                                     "proven, applied, and the approval is recorded (without "
                                     "--approved-by and --reason the fix is only WITHHELD)")
                dc.add_argument("--reason", default=None, help="why the person approves it")
        else:
            dc.add_argument("--by", required=True, help="who renews")
            dc.add_argument("--reason", required=True, help="why the double-down may go on")
        dc.set_defaults(func=cmd_double_down)

    rsg = sub.add_parser(
        "resume-storage",
        help="Resume runs parked on storage pressure once a role run and the iteration's "
             "temporary workspace would be admitted",
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
    sgc = sgs.add_parser("account", help="Bytes and entries (inodes) of a project's registered "
                                         "storage namespace per root, run, session, tier and "
                                         "reclaimable now (read-only; I10b)")
    sgc.add_argument("--project", required=True, metavar="P")
    sgc.add_argument("--storage-root", metavar="PATH", default=None,
                     help="the storage state root (same resolution as `hoh run`); never created")
    sgc.add_argument("--budget", type=float, default=120.0, metavar="SECONDS",
                     help="time budget of the whole account, in (0, 3600] (default 120); what "
                          "is not read in time reads UNMEASURED")
    sgc.add_argument("--no-tiers", action="store_true",
                     help="skip the tier classification (it hashes every file of an item)")
    sgc.set_defaults(func=cmd_storage_account)
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
    sgn = sgs.add_parser("new-target",
                         help="Create an empty, registered directory for a target clone "
                              "(its owner record is written before the directory exists)")
    sgn.add_argument("--parent-project", required=True, metavar="Q",
                     help="the registered project that owns the new root")
    sgn.add_argument("--path", required=True, metavar="ABS",
                     help="the new directory: absolute, not existing, its parent listed in "
                          "the state root's CONFIG.json")
    sgn.add_argument("--storage-root", required=True, metavar="PATH",
                     help="the storage state root, named explicitly (no fallback)")
    sgn.set_defaults(func=cmd_storage_new_target)
    sgp = sgs.add_parser("register-project",
                         help="Register a fresh project whose repository is a target clone "
                              "made by `new-target` (create-only)")
    sgp.add_argument("--project", required=True, metavar="P")
    sgp.add_argument("--repo", required=True, metavar="ABS",
                     help="the clone: exactly the directory `new-target` created")
    sgp.add_argument("--storage-root", required=True, metavar="PATH",
                     help="the storage state root, named explicitly (no fallback)")
    sgp.set_defaults(func=cmd_storage_register_project)
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

    cg = sub.add_parser("capacity", help="The host's capacity registry (P3-16): what the scheduler may count "
                                         "on, declared by the operator")
    cgs = cg.add_subparsers(dest="capacity_cmd", required=True)
    cgt = cgs.add_parser("set", help="Declare one capacity (read-modify-write of the registry under a lock; "
                                     "as_of is the time of this command)")
    cgt.add_argument("kind", metavar="KIND", help="dependency, machine or provider")
    cgt.add_argument("name", metavar="NAME", help="a lower-case token: [a-z][a-z0-9_.-], at most 64 characters")
    cgt.add_argument("limit", metavar="LIMIT",
                     help="a whole number from 0 to 10**15 (digits only: no sign, space or leading zero)")
    cgt.add_argument("unit", metavar="UNIT", help="bytes, items, requests, sessions or tokens")
    cgt.add_argument("--storage-root", metavar="PATH", default=None,
                     help="the storage state root (same resolution as `hoh run`); never created")
    cgt.set_defaults(func=cmd_capacity_set)
    cgw = cgs.add_parser("show", help="Print the capacity registry (read-only; an absent or unreadable "
                                      "record is refused, never shown as empty)")
    cgw.add_argument("--source", action="store_true",
                     help="also print the capacity source: the registry, the provider state and the slots in use "
                          "under --root, each fact with its time, source and age; UNKNOWN where older than its bound")
    cgw.add_argument("--storage-root", metavar="PATH", default=None,
                     help="the storage state root (same resolution as `hoh run`); never created")
    cgw.set_defaults(func=cmd_capacity_show)
    cgc = cgs.add_parser("check", help="One broker decision (P3-17): may AMOUNT UNIT of KIND/NAME be taken "
                                       "now, beside the live run leases and open budget reservations under "
                                       "--root? Exit 0 grant, 3 wait, 4 refuse, 2 no decision (read-only). "
                                       "Sessions read zero until something takes a run lease",
                         description="One broker decision over the capacity registry and the live holds under "
                                     "--root (the runs root). Exit 0 grant, 3 wait, 4 refuse, 2 no decision. "
                                     "Read-only. Sessions read zero until something takes a run lease: nothing "
                                     "acquires one yet, so a grant does not count the sessions that are running.")
    cgc.add_argument("kind", metavar="KIND", help="dependency, machine or provider")
    cgc.add_argument("name", metavar="NAME", help="the capacity's name in the registry")
    cgc.add_argument("amount", metavar="AMOUNT", help="a whole number (digits only) of UNITs wanted")
    cgc.add_argument("unit", metavar="UNIT", help="bytes, items, requests, sessions or tokens")
    cgc.add_argument("--max-age-declared", metavar="SECONDS", default=None,
                     help="how old a declared limit may be (default 604800: a week)")
    cgc.add_argument("--max-age-measured", metavar="SECONDS", default=None,
                     help="how old a measured figure may be (default 300: five minutes)")
    cgc.add_argument("--storage-root", metavar="PATH", default=None,
                     help="the storage state root (same resolution as `hoh run`); never created")
    cgc.set_defaults(func=cmd_capacity_check)

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

    mc = sub.add_parser(
        "mission",
        help="A thin read-only view over the runs and projects under the root (writes nothing; "
             "exit 0, 1 a run or a log could not be read, 2 refused)")
    mc.add_argument("view", choices=["overview", "board", "needs-you", "map", "timeline", "evidence"],
                    help="overview: one row per run -- its phase, stage, condition and process, "
                         "as `hoh wait` would answer; board: the nodes of one project as running, "
                         "ready, queued, blocked; needs-you: the open decisions of one project "
                         "and what each holds; map: the nodes of one project as a graph -- "
                         "level, place, dependencies, cycles; timeline: its dated records, "
                         "oldest first; evidence: its gates against the head they measured and "
                         "the evidence bundle of each run its nodes name")
    mc.add_argument("--project", default=None, metavar="ID",
                    help="the project of the project views (refused for overview)")
    mc.add_argument("--json", action="store_true", help="the same rows as one JSON document")
    mc.set_defaults(func=cmd_mission, run_id=None)

    from . import runwait

    wt = sub.add_parser(
        "wait",
        help="Block until a run is at rest or has reached a stage or condition, a review "
             "report is final, or a process has ended (read-only; one JSON line; the exit "
             "code names the outcome)")
    wt.add_argument("run_id", nargs="?", default=None,
                    help="the run to wait for (exactly one of RUN, --report, --pid)")
    wt.add_argument("--report", default=None, metavar="PATH",
                    help="wait until this review report is final: a regular file, not modified "
                         "for the merge gate's stable age, ending with a well-formed footer")
    wt.add_argument("--pid", type=runwait.pid_arg, default=None, metavar="N",
                    help="wait until this process id is gone, taken by another process, or a "
                         "zombie (read from /proc/N/stat; nothing is signalled)")
    wt.add_argument(
        "--until", default=None, metavar="WHAT",
        help="settled (default): the run is at rest -- accepted, failed, cancelled, blocked, "
             "parked, held, paused, idle or died; terminal: accepted, failed or cancelled only; "
             "stage=NAME or condition=NAME: that stage or condition holds (a run only)")
    wt.add_argument("--timeout", type=runwait.timeout_arg, default=runwait.DEFAULT_TIMEOUT, metavar="SEC",
                    help="give up after this many seconds (default 3600; 0 = look once)")
    wt.add_argument("--poll", type=runwait.poll_arg, default=runwait.DEFAULT_POLL, metavar="SEC",
                    help="seconds between two looks (default 10, at least 1)")
    wt.set_defaults(func=cmd_wait)

    pnp = sub.add_parser(
        "pin",
        help="Build a spec-only pin commit by plumbing on a base commit the product resolved (moves no branch, "
             "index or working tree); exit 0 pinned, 2 refused, 3 pinned but no receipt",
    )
    pnp.add_argument("--base", required=True, metavar="FULL_ID",
                     help="the commit to build on: a full 40/64-digit lower-case id, never a ref or a short id")
    pnp.add_argument("--files-root", required=True, metavar="DIR",
                     help="a directory laid out like the repository; every file in it is added to the pin")
    pnp.add_argument("--spec", required=True, metavar="PATH",
                     help="the specification, a path relative to --files-root (its digest is recorded)")
    pnp.add_argument("--ref", required=True, metavar="NAME",
                     help="the pin's name; the ref refs/pins/NAME is created, never moved")
    pnp.add_argument("--message-file", required=True, metavar="FILE", help="the commit message")
    pnp.add_argument("--co-author", required=True, metavar="'NAME <ADDRESS>'",
                     help="the attribution trailer (Co-Authored-By) the product appends")
    pnp.add_argument("--receipt", required=True, metavar="FILE",
                     help="the receipt to create; an existing file is never replaced")
    pnp.add_argument("--allow", action="append", default=[], metavar="KIND",
                     help="accept findings of this kind in the spec (home-path, scratch-path, invisible); repeatable, "
                          "recorded in the receipt")
    pnp.add_argument("--repo", default=".", metavar="DIR", help="the repository (default: .)")
    pnp.set_defaults(func=cmd_pin, run_id=None)

    imp = sub.add_parser(
        "import",
        help="Fetch an accepted candidate into refs/imports/ID, prove its tree is the accepted one and preview "
             "the merge; exit 0 clean, 1 a preview conflicts, 2 refused, 3 imported but no receipt",
    )
    imp.add_argument("--run-state", required=True, metavar="FILE",
                     help="the run's state.json (its last accepted candidate names the tree and the worktree)")
    imp.add_argument("--commit", required=True, metavar="FULL_ID",
                     help="the candidate's commit in the run's worktree: a full lower-case id")
    imp.add_argument("--id", required=True, metavar="ID", help="the import's name; refs/imports/ID is created, never moved")
    imp.add_argument("--receipt", required=True, metavar="FILE",
                     help="the receipt to create; an existing file is never replaced")
    imp.add_argument("--base", default=None, metavar="FULL_ID",
                     help="the integration tip to preview against (a full id; default: HEAD, resolved by the product)")
    imp.add_argument("--against", action="append", default=[], metavar="ID",
                     help="also preview against refs/imports/ID (repeatable)")
    imp.add_argument("--against-unmerged", action="store_true",
                     help="also preview against every other import that is not yet in the base")
    imp.add_argument("--spec", default=None, metavar="FILE",
                     help="a copy of the specification the run was accepted under, when its spec_path is gone "
                          "(its digest must be the run's spec_digest); the test pins are read from it")
    imp.add_argument("--repo", default=".", metavar="DIR", help="the repository (default: .)")
    imp.set_defaults(func=cmd_import, run_id=None)

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

    sf = sub.add_parser(
        "surface",
        help="The Herdr tabs opened for a run: open one (recorded as the run's), "
             "list them, close them once the run is over")
    sfs = sf.add_subparsers(dest="surface_cmd", required=True)
    so = sfs.add_parser("open", help="Open a tab for RUN and record it as the run's own")
    so.add_argument("run_id")
    so.add_argument("--role", default="run-host",
                    help="the program role the tab is owned under: a lowercase slug "
                         "(default run-host)")
    so.add_argument("--label", required=True, help="the tab's label")
    so.add_argument("--cwd", required=True, help="absolute directory the tab starts in")
    so.set_defaults(func=cmd_surface_open)
    sl = sfs.add_parser("list", help="The surfaces recorded for RUN (every run without one)")
    sl.add_argument("run_id", nargs="?", default=None)
    sl.set_defaults(func=cmd_surface_list)
    sr = sfs.add_parser(
        "reap",
        help="Close the surfaces of runs that are over that the registry owns "
             "(a dry run unless --close)")
    sr.add_argument("run_id", nargs="?", default=None)
    sr.add_argument("--close", action="store_true",
                    help="actually close; without it only say what would be closed")
    sr.set_defaults(func=cmd_surface_reap)
    sgr = sfs.add_parser(
        "register",
        help="Record a surface that exists (a reviewer, a drafter, an orchestrator session) in "
             "the project's surface book")
    sgr.add_argument("--project", required=True)
    sgr.add_argument("--kind", required=True, help="reviewer, drafter or orchestrator")
    sgr.add_argument("--pane", default=None,
                     help="the pane id (required for a reviewer or a drafter; an orchestrator "
                          "defaults to $HERDR_PANE_ID)")
    sgr.add_argument("--strand", default="", help="the work item the surface belongs to")
    sgr.add_argument("--perspective", default="", help="the reviewer's or drafter's name, e.g. a")
    sgr.add_argument("--round", type=int, default=1)
    sgr.add_argument("--candidate", default="",
                     help="a full commit id whose merge ends the strand")
    sgr.add_argument("--into", default="",
                     help="the branch (refs/heads/<name>) the candidate is merged into; default "
                          "the branch HEAD of the project repository is on")
    sgr.add_argument("--storage-root", metavar="PATH", default=None)
    sgr.set_defaults(func=cmd_surface_register, run_id=None)
    sgl = sfs.add_parser(
        "release",
        help="Declare a strand finished: its reviewers and drafters are over (explicit, "
             "recorded, idempotent)")
    sgl.add_argument("--project", required=True)
    sgl.add_argument("--strand", required=True)
    sgl.add_argument("--reason", required=True)
    sgl.add_argument("--storage-root", metavar="PATH", default=None)
    sgl.set_defaults(func=cmd_surface_release, run_id=None)

    rp = sub.add_parser(
        "reap",
        help="Close the surfaces whose lifecycle is over -- by the records, never by a name or an "
             "agent's status (a dry run unless --apply)")
    rp.add_argument("--project", default=None,
                    help="the project whose surface book is swept")
    rp.add_argument("--storage-root", metavar="PATH", default=None)
    rp.add_argument("--runs-root", dest="runs_roots", action="append", default=None,
                    metavar="DIR", help="a runs root (or a directory of them) whose workspace "
                    "records are swept; default the --root of hoh when no --project is given")
    rp.add_argument("--strand", default=None, help="only this strand's surfaces")
    rp.add_argument("--apply", action="store_true",
                    help="actually close; without it only say what would be closed")
    rp.set_defaults(func=cmd_reap, run_id=None)

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

    mgp = sub.add_parser(
        "merge-gate",
        help="Decide whether a candidate may be merged from its review reports (merges nothing)",
    )
    mgs = mgp.add_subparsers(dest="merge_gate_cmd", required=True)
    mgc = mgs.add_parser(
        "check",
        help="Judge the required review reports and write the decision as a new receipt; "
             "exit 0 MERGE_ELIGIBLE, 1 not eligible, 2 no decision",
    )
    mgc.add_argument("--candidate", required=True, metavar="REF",
                     help="the candidate commit (a ref is resolved now, the receipt keeps the sha)")
    mgc.add_argument("--base", required=True, metavar="REF",
                     help="the integration base; pass a ref such as HEAD so that a base that moved "
                          "since the review is seen")
    mgc.add_argument("--spec", required=True, metavar="PATH",
                     help="the specification file, a path inside the candidate commit")
    mgc.add_argument("--review", action="append", required=True, metavar="ID=FILE",
                     help="a required review and its report (repeatable)")
    mgc.add_argument("--receipt", required=True, metavar="FILE",
                     help="the receipt to create; an existing file is never replaced")
    mgc.add_argument("--repo", default=".", metavar="DIR", help="the repository (default: .)")
    mgc.add_argument("--min-reviews", type=int, default=2, metavar="N",
                     help="how many distinct required reviews must be named (default 2)")
    mgc.add_argument("--min-age", type=float, default=60.0, metavar="SECONDS",
                     help="a report modified more recently than this is not stable (default 60)")
    mgc.add_argument("--suite-receipt", default=None, metavar="FILE",
                     help="the suite-comparison receipt of the merge (hoh-suite-receipt/1); required "
                          "when a review is carried forward from an older base")
    mgv = mgs.add_parser(
        "verify",
        help="Decide a receipt's inputs again, now; exit 0 verified, 1 not verified, 2 not a receipt",
    )
    mgv.add_argument("receipt", metavar="RECEIPT")
    mgv.add_argument("--repo", default=None, metavar="DIR",
                     help="the repository (default: the one the receipt names)")
    mgv.add_argument("--candidate", default=None, metavar="REF",
                     help="the commit about to be merged: it must be the receipt's candidate")
    mgv.add_argument("--base", default=None, metavar="REF",
                     help="the tip about to receive it (pass HEAD): it must be the receipt's base")
    mgp.set_defaults(func=cmd_merge_gate, run_id=None)

    phs = sub.add_parser(
        "phase",
        help="A phase is closed at a commit (A7): close it, read its state, reopen it by decision",
    )
    phss = phs.add_subparsers(dest="phase_cmd", required=True)
    phc = phss.add_parser(
        "close",
        help="Verify a closure entry against the blobs at its closing commit and write the "
             "receipt (commit it yourself); exit 0 written, 1 refused",
    )
    phc.add_argument("phase", metavar="PHASE", help="P0 .. P99")
    phc.add_argument("--record", required=True, metavar="ENTRY",
                     help="the closure entry: a JSON file (hoh-phase-closure-entry/1) naming the "
                          "closing commit, the sealed snapshot and the evidence files with sha256")
    phst = phss.add_parser(
        "status",
        help="Read the committed receipt and re-derive it: CLOSED at a commit with its watch, or "
             "OPEN / REOPENED with the reason; exit 0 closed, 1 not closed, 2 not judged",
    )
    phst.add_argument("phase", metavar="PHASE", nargs="?", default=None,
                      help="a phase (default: every phase with a record under .hoh/phases)")
    phst.add_argument("--json", action="store_true", help="print the states as JSON")
    phr = phss.add_parser(
        "reopen",
        help="Record the operator's decision to reopen the standing closure, answering a reopen "
             "request (commit it yourself); exit 0 written, 1 refused",
    )
    phr.add_argument("phase", metavar="PHASE", help="P0 .. P99")
    phr.add_argument("--request", required=True, metavar="FILE",
                     help="the reopen request (REOPEN-REQUEST-*.json) in the phase's gate "
                          "directory, committed")
    phr.add_argument("--reason", required=True, metavar="TEXT",
                     help="why the closure is withdrawn (1 to 2000 characters)")
    for sp in (phc, phst, phr):
        sp.add_argument("--repo", default=".", metavar="DIR",
                        help="the top of the project's git repository (default: .)")
        sp.add_argument("--gate-dir", default=None, metavar="PATH",
                        help="where the phase's records live, relative to the repository "
                             "(default: .hoh/phases/<phase>)")
    phs.set_defaults(func=cmd_phase, run_id=None)

    pr = sub.add_parser(
        "project",
        help="The orchestration layer above a run: which runs happen at all",
    )
    ps = pr.add_subparsers(dest="project_cmd", required=True)
    ps.add_parser("list", help="All projects with their resume verdict")
    pls = ps.add_parser(
        "leases",
        help="The run leases `hoh run` keeps (one per run) and what each says; --archive moves the old "
             "ended ones aside, never removing them",
    )
    pls.add_argument("--archive", action="store_true",
                     help="move the leases that ended at least --older-than-days ago, with their parked "
                          "copies, to projects/hoh-runs/run-leases-archive/<stamp>/ with a manifest")
    pls.add_argument("--older-than-days", type=int, default=7, choices=range(1, 3651), metavar="DAYS",
                     help="with --archive: how long ago a lease ended at the latest (1 to 3650, default 7)")
    pcr = ps.add_parser(
        "create",
        help="Create a project's governed record (idempotent): what hold and rollover need",
    )
    pcr.add_argument("project_id")
    pcr.add_argument("--repo", required=True, metavar="DIR",
                     help="the git checkout whose HEAD the project's capsules bind")
    # Overrides the group's func, like `contract`: cmd_project refuses a project without state.
    pcr.set_defaults(func=cmd_project_create)
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
    bg = ps.add_parser(
        "budget",
        help="Declare the project's dispatch budget, share and QA reserve (recorded in the "
             "project; every drive binds it to its launcher), or read it back",
    )
    bg.add_argument("project_id")
    bg.add_argument("--dispatch-budget", type=int, default=None,
                    help="the ceiling on role dispatches shared by every run of the project")
    bg.add_argument("--dispatch-reservation", type=int, default=None,
                    help="the most one run holds of it (default: everything that is left)")
    bg.add_argument("--qa-reserve", type=int, default=None,
                    help="dispatches never granted to a node that is not a repair (default 0)")
    bg.add_argument("--reason", default="", help="why; recorded in the declaration")
    bg.add_argument("--session", default=None,
                    help="the current master's session id (governed projects)")
    bg.add_argument("--epoch", type=int, default=None,
                    help="the current master's epoch (governed projects)")
    bg.add_argument("--token", default=None, help="the current master's token")
    bg.add_argument("--token-file", default=None, help="file holding the current master's token")
    rd = ps.add_parser(
        "route-decisions",
        help="Route a project's open EXTERNAL decisions through an autonomy contract and record "
             "the ones it hands to an automatic resolver (approves nothing; P3-31)",
    )
    rd.add_argument("project_id")
    rd.add_argument("--autonomy", required=True, metavar="PATH",
                    help="the autonomy contract to route under (as `hoh run --autonomy`)")
    rd.add_argument("--program-epoch", type=int, default=None, metavar="N",
                    help="pin the contract to this programme epoch before routing")
    rd.add_argument("--expect-digest", default=None, metavar="DIGEST",
                    help="refuse unless the contract digests to this (what a loop planned under)")
    rd.add_argument("--dry-run", action="store_true",
                    help="report what would be recorded; write nothing, take no authority")
    rd.add_argument("--reason", default="route open decisions",
                    help="noted on an operator epoch taken for the command")
    rd.add_argument("--session", default=None,
                    help="the current master's session id (governed projects)")
    rd.add_argument("--epoch", type=int, default=None,
                    help="the current master's epoch (governed projects)")
    rd.add_argument("--token", default=None, help="the current master's token")
    rd.add_argument("--token-file", default=None,
                    help="file holding the current master's token")
    cn = ps.add_parser(
        "continue",
        help="What a wave of a project may continue under an autonomy contract, within its "
             "horizon and budget, written down once; dispatches nothing (P3-32)",
    )
    cn.add_argument("project_id")
    cn.add_argument("--autonomy", required=True, metavar="PATH",
                    help="the autonomy contract to continue under (as `hoh run --autonomy`)")
    cn.add_argument("--wave", required=True, metavar="WAVE",
                    help="the wave id: its record is created once under this name")
    cn.add_argument("--program-epoch", type=int, default=None, metavar="N",
                    help="pin the contract to this programme epoch first")
    cn.add_argument("--dry-run", action="store_true",
                    help="decide and report; write nothing, take no authority")
    cn.add_argument("--reason", default="continue by policy",
                    help="noted on an operator epoch taken for the command")
    cn.add_argument("--session", default=None,
                    help="the current master's session id (governed projects)")
    cn.add_argument("--epoch", type=int, default=None,
                    help="the current master's epoch (governed projects)")
    cn.add_argument("--token", default=None, help="the current master's token")
    cn.add_argument("--token-file", default=None,
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
    wk = ps.add_parser(
        "wake",
        help="The master consults its predecessor: plan (default) or, with --execute, "
             "resume the predecessor's session in a surface of its own and send it one "
             "prompt; the surface is never closed",
    )
    wk.add_argument("project_id")
    wk.add_argument("--session", required=True, help="the current master's session id")
    wk.add_argument("--epoch", type=int, required=True, help="the current master's epoch")
    wk.add_argument("--token", default=None,
                    help="the current master's token (visible in the process list; "
                         "prefer --token-file)")
    wk.add_argument("--token-file", default=None, help="file holding that token")
    wk.add_argument("--resume-id", required=True,
                    help="the predecessor's Claude session id (a UUID; the records do not "
                         "hold it)")
    wk.add_argument("--brief", required=True,
                    help="a file the predecessor reads: the question (a normalized "
                         "absolute path, outside the project's rollover directory)")
    wk.add_argument("--answer", required=True,
                    help="the file the predecessor writes its answer to: an absolute "
                         "path that does not exist yet, in a directory that does, outside "
                         "the project's rollover directory")
    wk.add_argument("--cwd", default=None,
                    help="the directory the predecessor's session was started in; needed "
                         "only when the records do not know it")
    wk.add_argument("--execute", action="store_true",
                    help="open the surface and send the prompt (without it: print the plan)")
    wk.add_argument("--predecessor-surface-closed", action="store_true",
                    help="with --execute: the predecessor's own surface is closed (its "
                         "session must not run in two places)")
    wk.add_argument("--timeout", type=float, default=600.0,
                    help="seconds to wait for the predecessor's turn to end (0 to 3600)")
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
    hd.add_argument("--rotate-policy", default=None, metavar="FILE",
                    help="apply this rotation policy at every renewal: when the master's "
                         "context is over its threshold, ask for a handoff and rotate the "
                         "session (inside Herdr only). Each request is also typed into this "
                         "session's own Herdr pane (see --rotate-no-deliver)")
    hd.add_argument("--rotate-no-deliver", action="store_true",
                    help="do not type rotation requests into this session's pane: the line on "
                         "stdout is then the only notice (recorded in the rotation log)")
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
    ra2.add_argument("--rotate-policy", default=None, metavar="FILE",
                     help="keep applying this rotation policy while holding the lease "
                          "(the predecessor's pointer carries it)")
    ra2.add_argument("--rotate-no-deliver", action="store_true",
                     help="do not type rotation requests into this session's pane (the "
                          "predecessor's pointer carries the opt-out on)")
    ra2.add_argument("--rotate-policy-sha256", default=None, metavar="HEX",
                     help="the sha256 of the policy file that started this rotation; a "
                          "policy file that changed since is refused (the pointer carries it)")
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
    except UnsafeRunId as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
