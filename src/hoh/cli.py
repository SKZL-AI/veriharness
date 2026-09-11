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
from pathlib import Path

from . import stages
from .contracts import Budgets, Condition, Role, Stage
from .controller import Controller, new_run_id
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

        # What gets registered is where the roles actually sit: the arena
        # root. The run directory is deliberately **no longer** handed to them
        # as a working directory -- that is where the evidence lives.
        registered = [
            path for path in (store.arenas_dir, store.dir)
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
    """Resumes runs that are stuck on the usage quota.

    The counter-design to "HoH sleeps by itself": a controller that holds the
    lock while sleeping for hours is indistinguishable from a hung one.
    Instead the run stays visibly blocked and carries the reason in
    machine-readable form; this command picks it up once the wait is over.
    Callable by hand, from cron or as a Herdr plugin action.

    Only `blocked_kind='usage_limit'` is resumed. Every other block has a
    cause a human has to clear -- stepping over it automatically would be
    exactly the silent widening of permissions the rest of this is built
    against.
    """
    from . import quota

    ids = [args.run_id] if args.run_id else list(list_runs(args.root))
    report = []
    for rid in ids:
        store = RunStore(args.root, rid)
        try:
            with store.lock():
                st = store.read_state()
                if st.condition is not Condition.BLOCKED or st.blocked_kind != "usage_limit":
                    continue
                if st.resume_attempts >= quota.MAX_RESUME_ATTEMPTS:
                    report.append({
                        "run_id": rid, "action": "given_up",
                        "reason": f"{st.resume_attempts} unsuccessful resume attempts -- "
                                  "an automatic retry without a limit costs money "
                                  "without making progress. 'hoh unblock' is the "
                                  "captain's decision.",
                    })
                    continue
                if not quota.is_due(st.retry_after):
                    report.append({"run_id": rid, "action": "waiting",
                                   "earliest": st.retry_after})
                    continue
                st.resume_attempts += 1
                stages.unblock(st, f"quota retry {st.resume_attempts}")
                store.write_state(st)
                report.append({"run_id": rid, "action": "resumed",
                               "attempt": st.resume_attempts,
                               "next_step": f"hoh --root {args.root} run {rid}"})
        except (LockBusy, StoreError) as exc:
            report.append({"run_id": rid, "action": "skipped", "reason": str(exc)[:120]})

    _print({"checked": len(ids), "results": report})
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
        budgets=Budgets(max_iterations=args.max_iterations),
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
            findings = _reconcile(state)

            unclear = [b for b in findings if b[1] == "block"]
            if unclear and not args.force:
                stages.block(
                    state,
                    "resume unclear: " + "; ".join(f"{b[0]} {b[2]}" for b in unclear),
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
            stages.unblock(state, args.reason)
            store.write_state(state)
    except (StoreError, LockBusy, stages.TransitionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Block on {args.run_id} lifted: {args.reason}")
    return 0


def _inconclusive_checks(store, state, iteration: int) -> list[tuple[str, str]]:
    """The criteria of one iteration whose receipts say they never ran.

    Read from the receipts, not from the loop's own summary: the summary knows
    "not accepted", and only the receipt knows whether that was a product
    defect or an infrastructure refusal.
    """
    raus: list[tuple[str, str]] = []
    verzeichnis = store.dir / "receipts"
    if not verzeichnis.is_dir():
        return raus
    for f in sorted(verzeichnis.glob(f"{state.run_id}-i{iteration}-*.json")):
        if f.stem.endswith("-basis"):
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("runner_ok", True) and d.get("exit_code") not in (124, 126, 127):
            continue
        grund = {
            124: "timeout", 126: "refused or not executable", 127: "not found",
        }.get(d.get("exit_code"), "infrastructure error")
        raus.append((d.get("check_id", f.stem), grund))
    return raus


def _isolation_for_run(state, angefordert: str | None):
    """The isolation for this invocation, reconciled with the run's own.

    Three cases, and only one of them is a judgement call:

    * no flag -- the run keeps what it was started with;
    * the same value -- nothing to decide;
    * a different value -- allowed upwards, refused downwards. Lowering it
      mid-run would mean the accepted candidate was verified partly under a
      sandbox and partly not, and the run record would carry a single word for
      two different regimes.
    """
    gespeichert = getattr(state, "isolation", "none") or "none"
    if angefordert is None or angefordert == gespeichert:
        return _isolation(gespeichert)
    neu = _isolation(angefordert)
    rang = {"none": 0, "strict": 1}
    if rang.get(angefordert, 0) < rang.get(gespeichert, 0):
        raise ValueError(
            f"this run was started under --isolation {gespeichert} and asking "
            f"for {angefordert} now would verify part of it under a sandbox "
            "and part of it without one. Start a new run if that is what you "
            "want; a run's isolation is a property of the run."
        )
    state.isolation = angefordert
    return neu


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
        erlaubt = ", ".join(i.value for i in Isolation)
        raise ValueError(f"unknown isolation {name!r}; expected one of: {erlaubt}") from None


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
    """
    from .controller import WaitingForApproval

    return isinstance(exc, WaitingForApproval) or "blocked" in str(exc)


def cmd_run(args) -> int:
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
    try:
        dispatcher = build_dispatcher(
            answers_dir=store.dir / "answers",
            profiles=profile,
            cwd=state.repo_path,
            prefer_herdr=not args.no_herdr,
            timeout_ms=args.role_timeout * 1000,
            models=models,
            efforts=efforts,
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
    gewuenscht = isolation.value if isolation is not None else "none"
    if state.isolation != gewuenscht:
        state.isolation = gewuenscht
        with store.lock():
            store.write_state(state)

    controller = Controller(
        store, dispatcher, spec_path=state.spec_path, isolation=isolation
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
                stages.block(fresh, f"iteration aborted: {exc}")
                store.write_state(fresh)
            print(f"Iteration aborted and run blocked: {exc}", file=sys.stderr)
            # HoH does **not** clear away a pending dialog. The error path used
            # to close its own tabs even when an agent was waiting for an
            # approval -- which removed exactly the window in which the captain
            # could have answered, and afterwards the reason for the block was
            # no longer determinable.
            waiting = _waiting_for_approval(exc)
            if waiting:
                print(
                    "The role tabs stay open so that the approval can be granted "
                    "in the pane. After that: hoh unblock <run-id> and hoh run.",
                    file=sys.stderr,
                )
            elif hasattr(dispatcher, "close_own"):
                try:
                    dispatcher.close_own()
                except Exception:
                    pass
            return 1

        ran += 1
        if state.resume_attempts:
            # An iteration that actually ran is the proof that the quota is
            # back. The counter measures consecutive unsuccessful attempts.
            state.resume_attempts = 0
        waiting_for_approval = waiting_for_approval or out.waiting_for_approval
        label = "accepted" if out.accepted else "not accepted"
        print(f"Iteration {out.iteration}: {label} -- {out.reason}")
        unklar = _inconclusive_checks(store, state, out.iteration)
        if unklar and not out.accepted:
            # "Not accepted" is true and useless when the reason is that
            # nothing ran. An iteration whose criteria were refused by the
            # guard, timed out, or could not be isolated has measured nothing
            # about the product -- and a transcript that renders that as a
            # product rejection is the laundering this project exists to stop.
            # A reviewer read exactly that off a real run's log.
            print(
                f"  {len(unklar)} of these did not run at all "
                f"(INCONCLUSIVE, not a verdict): "
                + ", ".join(f"{cid} [{grund}]" for cid, grund in unklar)
            )

        if state.condition is not Condition.ACTIVE:
            print(f"Run is now {state.condition.value}: {state.stop_reason or ''}")
            break
        if out.accepted and args.until_accepted:
            break

    # Apply the retention limit (§7): parked intermediate states and old
    # arenas move to attic/, receipts and evidence never do.
    try:
        with store.lock():
            pruned = store.prune()
    except Exception:
        pruned = {}

    # Clear away our own tabs -- otherwise they would be left standing after
    # every run and clutter up the multiplexer the captain works in.
    #
    # Unless a role is still sitting at an unanswered permission dialog. The
    # dogfood run found this on itself on 2026-09-08: the exception route
    # already kept the tabs open for that case, but `_verify` turns exactly
    # that exception into an *outage* -- correctly, because an unanswered QA
    # is a missing verdict and not a failed run. So the blocked dialog took
    # the normal route, and this line closed the one window in which it could
    # have been answered. The protection guarded one route while the traffic
    # took the other.
    closed_tabs = []
    if waiting_for_approval:
        print(
            "The role tabs stay open: a role is waiting for an approval in "
            "its pane. Answer it there, then: hoh unblock "
            f"{args.run_id} --reason \"...\" (only if BLOCKED) and hoh run "
            f"{args.run_id}.",
            file=sys.stderr,
        )
    elif hasattr(dispatcher, "close_own"):
        try:
            closed_tabs = dispatcher.close_own()
        except Exception:
            closed_tabs = []

    _print(
        {
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
    )
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
            if state.stage is not Stage.CHECKPOINTED:
                print(
                    f"Stage is {state.stage.value}. Only an accepted "
                    "checkpoint is delivered.",
                    file=sys.stderr,
                )
                return 2

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


# --------------------------------------------------------------------------- #
# The project layer: which runs happen at all
# --------------------------------------------------------------------------- #


def cmd_project(args) -> int:
    """Inspect the orchestration layer above a single run.

    Deliberately read-only for now. Driving a project needs a `RunLauncher`
    bound to real Herdr dispatch, and shipping a command that *looks* like it
    orchestrates while executing nothing would be worse than not shipping it:
    the thing this project keeps finding is claims that outrun what was
    measured. `status` and `resume` answer the question a returning session
    actually has -- what does this state say to do next -- and they answer it
    from the persisted file alone.
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
                verdikt, _ = resume_decision(st)
                offen = sum(1 for n in st.nodes if not n.settled)
                print(f"{pid}  {verdikt:9s}  nodes={len(st.nodes)} open={offen} "
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

    if args.project_cmd == "record-action":
        try:
            st = record_external_action(
                store, actor=args.actor, reason=args.reason,
                repo_path=st.repo_path, node=args.node,
                head_before=args.head_before or "",
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
            st = unblock(store, args.node, args.reason)
        except StoreError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 4
        n = st.node(args.node)
        print(f"{args.node} is now {n.lifecycle.value}; recorded as {st.decisions[-1].id}")
        return 0

    verdikt, grund = resume_decision(st)

    if args.project_cmd == "resume":
        print(json.dumps({"project_id": st.project_id, "verdict": verdikt,
                          "reason": grund, "measurement_head": st.measurement_head,
                          "closure_generation": st.closure_generation,
                          "rc_closed": st.rc_closed()}, indent=2))
        return 0

    # status
    print(f"project {st.project_id}  ({st.repo_path})")
    print(f"  verdict          {verdikt}")
    print(f"  reason           {grund}")
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
        letzte = {}
        for g in st.gates:
            letzte[g.name] = g
        print("  gates (most recent per name):")
        for name, g in sorted(letzte.items()):
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
            bewegt = "" if r.changed_the_tree else "  (tree unchanged)"
            print(f"    {r.action_id} {r.actor}: {r.head_before or '?'} -> "
                  f"{r.head_after or '?'}{bewegt}  {r.reason[:60]}")
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
    r.add_argument("--role-timeout", type=int, default=2700, metavar="SEC",
                   help="deadline per role run in seconds (default 2700 = 45 min). "
                        "A thorough differential review takes time; too tight a "
                        "deadline trains superficial work.")
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
                    help="without an argument: check every run")
    fs.set_defaults(func=cmd_resume_quota)

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
    ra = ps.add_parser(
        "record-action",
        help="Record a repository change made outside the loop, so closure can be traced",
    )
    ra.add_argument("project_id")
    ra.add_argument("--actor", required=True,
                    help="'human', 'operator', or the tool that made the change")
    ra.add_argument("--reason", required=True, help="why it was made")
    ra.add_argument("--node", default=None, help="the node it relates to, if any")
    ra.add_argument("--head-before", default="",
                    help="the repository head before the change -- it cannot be "
                         "measured afterwards, so it has to be supplied")
    pr.set_defaults(func=cmd_project, run_id=None, project_id=None, node=None,
                    reason=None, actor=None, head_before=None)

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
