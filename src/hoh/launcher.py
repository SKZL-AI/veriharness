"""A `RunLauncher` bound to real Herdr and real HoH runs.

`orchestrator.py` defines the control loop against two narrow protocols so it
can be exercised without agent quota. This module is the other side: the
implementation that actually dispatches, reads back a real `RunState`, and
decides what the run's outcome was.

The whole difficulty is in that last step, and it is worth being explicit
about why, because getting it wrong is how an orchestrator merges something
that was never accepted. A run's state does not carry a field saying
"accepted". It carries a stage, a condition, and a `last_accepted_candidate`
that may be left over from an *earlier* iteration. So:

* `CHECKPOINTED` with a `last_accepted_candidate` that advanced during this
  dispatch is an acceptance.
* `CHECKPOINTED` with the same candidate as before the dispatch is not: the
  run ended where it started, and treating that as acceptance would merge a
  candidate twice.
* `VERIFYING` or `DEVELOPING` still active is not a verdict at all -- the run
  is unfinished, and saying anything else about it is guessing.
* `BLOCKED` with a quota reason is a provider outage, not a rejection. The
  distinction matters because recording it as a rejection burns a node's
  retry budget on somebody else's downtime.

Every one of those cases is a halt class the controller already knows how to
name, so the mapping is a translation rather than a judgement.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .approval import Approval, ApprovalProvider, NoApprovalProvider
from .contracts import Condition, Stage
from .orchestrator import (
    BudgetExhausted, MergeFailure, MergeResult, RunLauncher, RunOutcome,
    RunVerdict,
)
from .failures import AUTOMATIC_KINDS
from .controller import open_seal_arenas
from .durable_io import DurableWriteNotSynced, durable_create
from .project import ActionClass, TaskNode
from .stages import violation_unresolved
from .store import LockBusy, RunStore, StoreError

#: Blocked kinds that mean the provider, not the work.
PROVIDER_BLOCKED = frozenset({"usage_quota", "quota", "rate_limit", "provider",
                              # What `stages.block` actually writes for an
                              # exhausted quota. Missing here, a real quota wait
                              # read as UNDETERMINED and halted the project
                              # AMBIGUOUS instead of parking it (found 2026-09-25
                              # once the durability fixture stopped re-implementing
                              # this mapping).
                              "usage_limit"}) | AUTOMATIC_KINDS

#: Phrases with which `hoh run` refuses to start, or a run stops, because
#: storage admission is not usable (STORAGE-B3). A run that says one of them
#: is STORAGE_UNAVAILABLE (NEEDS_YOU), never UNDETERMINED. The blocked kind of
#: a storage park, `storage_pressure`, is deliberately *not* in
#: PROVIDER_BLOCKED: it is its own class with its own consumer.
STORAGE_REFUSED = ("storage state root is not initialised", "WSL: configure host_paths",
                   "WSL: the VHDX volume is not observed", "storage admission unavailable",
                   "storage pressure: the recheck timer could not be armed")

#: Phrases in a stop reason that mean a person has to answer something. Matched
#: on the reason text rather than a code because HoH records this case in prose:
#: `blocked_kind` was None for the trust dialog that first exposed it.
APPROVAL_WAITING = frozenset({"waits for an approval", "trust this folder",
                              "blocked dialog", "awaiting approval"})


def accepted_identity_problem(git, sha: str, outcome: RunOutcome) -> str | None:
    """Why commit `sha` is not the accepted candidate, or None when it is.

    The accepted identity is the verified *tree* when the outcome carries one:
    `sha` must have exactly that tree. Without a tree (an older launcher) it is
    the candidate commit itself. `git` is a callable running git in the
    repository and returning a CompletedProcess.
    """
    if outcome.candidate_tree:
        tree = git("rev-parse", "--verify", "--quiet", sha + "^{tree}").stdout.strip()
        if tree != outcome.candidate_tree:
            return (f"branch head {sha[:12]} carries tree {tree[:12] or '?'}, but the "
                    f"accepted tree is {outcome.candidate_tree[:12]}: the branch moved "
                    f"after acceptance, or the accepted work was never committed onto it")
        return None
    if outcome.candidate and outcome.candidate != sha:
        return (f"branch head {sha[:12]} is not the accepted candidate "
                f"{outcome.candidate[:12]}, and no verified tree was recorded to "
                f"show they carry the same content")
    return None


def _timer_form(stamp: str | None) -> str | None:
    """`RunState.retry_after` (ISO 8601 with offset) in the timer form, or None.

    None, not a guess, when it cannot be read: the node then keeps a human
    owner instead of parking on a time nobody can check.

    A fractional moment is rounded *up* to the next whole second. The timer
    form has no fractions, and cutting them off made the timer due up to one
    second before the provider's reset: the tick resumed a run the provider
    still refused, while `quota.is_due` said not due (O237). The resume loop's
    copy calls this one, so both round the same way.
    """
    if not stamp:
        return None
    from datetime import UTC, datetime, timedelta

    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    moment = moment.astimezone(UTC)
    if moment.microsecond:
        moment = moment.replace(microsecond=0) + timedelta(seconds=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def storage_refusal(text: str | None) -> str | None:
    """The first line of `text` that names a storage refusal, or None."""
    for line in (text or "").splitlines():
        if any(phrase in line for phrase in STORAGE_REFUSED):
            return line.strip()[:300]
    return None


@dataclass
class Dispatch:
    """One `hoh run` invocation's raw result."""

    exit_code: int
    stdout: str
    stderr: str


def hoh_command() -> list[str]:
    """How this launcher starts `hoh` itself: the interpreter that is running
    it (`sys.executable`), or `$HOH_PYTHON` when the operator names one.

    The bare `python3` found on `PATH` is not the interpreter HoH was
    installed into on Nix profiles, pipx installs or any host with several
    Pythons: it lacks `hoh` and `pydantic`, and every dispatched node fails.
    """
    return [os.environ.get("HOH_PYTHON") or sys.executable, "-m", "hoh.cli"]


class HohRunLauncher(RunLauncher):
    """Drives real runs through the `hoh` CLI and the ordinary git mainline."""

    def __init__(
        self,
        root: Path | str,
        repo_path: Path | str,
        *,
        mainline: str = "master",
        iterations: int = 2,
        planner: str = "claude",
        developer: str = "claude",
        qa: str = "claude",
        timeout: int = 7200,
        dry_run: bool = False,
        approvals: "ApprovalProvider | None" = None,
        isolation: str = "none",
        dispatch_budget: int | None = None,
        storage_root: Path | str | None = None,
        storage_admission_factory=None,
        master_session: str | None = None,
        master_epoch: int | None = None,
        role_accounts: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        #: P2-43b: the governed master this launcher works for, passed to every
        #: `hoh run` it starts (`--master-session`, `--master-epoch`) so the
        #: run's parks are bound to it. None: the runs record `master: NONE`.
        self.master_session = master_session
        self.master_epoch = master_epoch
        #: role name -> operator-registered account handle (`--role-account`).
        from .contracts import Role
        from .dispatchrecovery import RecoveryError, check_account_handle

        known = {r.value for r in Role}
        if any(str(r) not in known for r in (role_accounts or {})):
            raise RecoveryError("role_accounts names a role that is not planner, "
                                "developer or qa (the name is not shown)")
        self.role_accounts = {str(r): check_account_handle(h)
                              for r, h in (role_accounts or {}).items()}
        #: STORAGE-B3: the storage state root every spawned `hoh run` is given
        #: (`--storage-root`): this launcher's own configuration, else
        #: `$HOH_STATE_ROOT` as resolved now, at construction. None leaves the
        #: child to resolve its own default -- which refuses an uninitialised
        #: root rather than inventing one.
        configured = storage_root or os.environ.get("HOH_STATE_ROOT") or None
        self.storage_root = Path(configured).expanduser() if configured else None
        #: Builds the admission whose boot the launcher claims before it
        #: spawns (Revision 9 item 7); injectable for tests.
        self._storage_admission_factory = storage_admission_factory
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.mainline = mainline
        self.iterations = iterations
        #: A ceiling on role dispatches shared by **every run this launcher
        #: starts**, node and repair nodes alike. Without it each run got its
        #: own ceiling, so a node that spawned one repair spent twice what a
        #: single run may -- which is how a benchmark whose premise is a
        #: matched budget handed one arm double the resource. `None` keeps the
        #: old behaviour: no ceiling at all.
        self.dispatch_budget = dispatch_budget
        # Named explicitly, never defaulted. `hoh run --qa` defaults to a
        # harness whose credential expired here, and the resulting run produced
        # no QA verdict at all while looking like an ordinary dispatch.
        self.planner, self.developer, self.qa = planner, developer, qa
        self.timeout = timeout
        self.dry_run = dry_run
        # How the acceptance checks of every run this launcher starts are
        # executed. Passed on the command line and then persisted on the run,
        # so a resumed run keeps it -- a project whose nodes were verified
        # under different isolation regimes would carry one word for two
        # different claims.
        self.isolation = isolation
        # Granting a worktree trust is an authority a deployment may hold, and
        # HoH deliberately does not: trusting on request would let anything
        # that can name a path obtain an agent's access to it. Absent by
        # default -- with no provider the run blocks and a person answers,
        # which is what happened before this existed.
        self.approvals = approvals or NoApprovalProvider()
        #: Every answer the authority gave, so a halt can quote it.
        self.approvals_given: list[Approval] = []

    def bind_master(self, session: str, epoch: int) -> None:
        """Tell the launcher which master it launches for (the governed drive
        calls this once the lease is held)."""
        self.master_session, self.master_epoch = session, int(epoch)

    # -- classification ---------------------------------------------------- #

    def action_class(self, node: TaskNode) -> ActionClass:
        """The node's declared class. Not re-derived here on purpose.

        Whether a node is externally irreversible is a policy decision that
        belongs in the state, where a human can see it and a restart preserves
        it -- not something re-inferred from a spec on every round, where a
        wording change could quietly downgrade it.
        """
        return node.action_class

    def depends_on(self, a: TaskNode, b: TaskNode) -> bool:
        """Whether two nodes may not proceed in parallel.

        A write/write overlap is the obvious case. The one that actually bit
        this project is write/semantic-read: two runs with entirely disjoint
        write sets still conflict when one writes a file whose content the
        other's correctness depends on. Both merges that produced the union
        gate had exactly that shape.
        """
        a_w, b_w = set(a.writes), set(b.writes)
        if a_w & b_w:
            return True
        return bool(a_w & set(b.semantic_reads) or b_w & set(a.semantic_reads))

    # -- dispatch ---------------------------------------------------------- #

    def _run_cli(self, argv: list[str]) -> Dispatch:
        env = dict(os.environ)
        env.setdefault("HERDR_ENV", "1")
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout,
            cwd=str(self.repo_path), env=env,
        )
        return Dispatch(p.returncode, p.stdout, p.stderr)

    #: A merge runs under the program lock (it must not race a successor's),
    #: so a git that hangs -- a hook, a signing prompt, a credential helper --
    #: must not hold that lock for ever (review A3, N3).
    MERGE_TIMEOUT = 600

    def _git(self, *args: str, cwd: Path | None = None,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True,
                cwd=str(cwd or self.repo_path), timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["git", *args], 124, "",
                                               f"git timed out after {timeout} s")

    def prepare(self, node: TaskNode) -> str | None:
        """Creates the worktree, specification and run for a node that has none.

        A repair node arrives from a gate failure carrying what went wrong and
        nothing else. Turning that into runnable work means writing a
        specification and starting a run, and during this project's own
        campaign a person did that by hand every time -- which is precisely
        where the loop stopped being able to continue on its own.

        The generated specification is deliberately thin: it states the failing
        gate, its output, and that the fix must make that gate green without
        changing what the gate measures. It does not try to diagnose. Guessing
        at a cause and writing it into a specification would hand the planner a
        conclusion instead of a problem.
        """
        if self.dry_run:
            return "dry run: nothing is prepared"
        # Asked before anything is created. `budget_argumente` raises this
        # too, but by then a worktree exists and a trust approval has been
        # granted for a run that will never start -- side effects for work
        # that was refused.
        self.budget_argumente()
        run_id = node.run_id or node.id
        branch_ = node.branch or f"hoh-{run_id}"
        store = RunStore(self.root, run_id)

        if store.state_exists():
            # The run exists, but that does not mean it can proceed. Blocks
            # come in a chain: the project node, the run, and the worktree's
            # trust. Measured the first time a repair node was dispatched for
            # real -- the node was unblocked, the run was still BLOCKED from
            # its previous attempt, and `hoh run` refused to start at all.
            return self._ensure_runnable(run_id, branch_, store)
        # A repair node's specification belongs to the **run**, not to the
        # repository under test. It used to be written to
        # `<repo>/.hoh-repair-<id>.md`, where it is untracked, unprotected and
        # in the way: this project's own export refused it as an unclassified
        # path, the cleanup that followed moved it aside, and a repair run that
        # was live at the time blocked on "specification is missing". A file
        # that a run depends on must not sit where tidying the repository can
        # remove it.
        #
        # Under `--root` it travels with the run's receipts and state, is
        # readable afterwards, and no repository ever has to ignore it.
        spec_file = Path(node.spec_path) if node.spec_path else (
            self.root / run_id / f"repair-{run_id}.md"
        )
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        if not spec_file.exists():
            if not node.repair_of:
                return (
                    f"node {run_id} has no run and no specification at "
                    f"{spec_file}; there is nothing to dispatch"
                )
            try:
                durable_create(spec_file, self._repair_spec(node), mode=None)
            except FileExistsError:
                if not spec_file.exists():      # a dangling link holds the name
                    return (f"the repair specification path {spec_file} is a dangling "
                            f"link; nothing to dispatch for {run_id}")
            except DurableWriteNotSynced:
                # The specification is in place and whole; only its directory
                # entry is not known to be durable.
                pass
            except OSError as exc:
                return f"could not write the repair specification {spec_file} for {run_id}: {exc}"

        wt = self._run_cli([
            *hoh_command(), "--root", str(self.root),
            "worktree", "--repo", str(self.repo_path), "--branch", branch_,
        ])
        if wt.exit_code != 0 and "already exists" not in (wt.stdout + wt.stderr):
            return f"could not create the worktree for {run_id}: {(wt.stderr or wt.stdout)[-200:]}"
        worktree = self._worktree_path(wt.stdout) or self._worktree_for(branch_)
        if not worktree.is_dir():
            return f"no worktree at {worktree} for {run_id}"

        # Without an approval the run starts and then stops at a trust dialog,
        # which HoH will not answer. Measured the first time a repair node was
        # dispatched for real: the run reached DEVELOPING and blocked on
        # "Yes, I trust this folder".
        release_ = self._approve(worktree)
        if not release_.granted:
            return release_.as_reason()

        argumente = [
            *hoh_command(), "--root", str(self.root), "start",
            "--repo", str(worktree), "--spec", str(spec_file), "--run-id", run_id,
        ]
        argumente += self.budget_argumente()
        st = self._run_cli(argumente)
        if st.exit_code != 0:
            return f"could not start run {run_id}: {(st.stderr or st.stdout)[-200:]}"
        node.branch = branch_
        node.run_id = run_id
        node.spec_path = str(spec_file)
        return None

    def spent_budget(self) -> int:
        """Dispatches every run under this root has already spent.

        Read from the **persisted run states**, not from a counter in this
        process. A counter in memory is reset by a restart, and a budget a
        restart resets is not a budget: a node that crashed after eight
        dispatches would come back with nine more. The runs count their own
        dispatches and write them down, so the number survives the process
        that produced it and cannot be lowered by starting a new one.
        """
        total = 0
        if not self.root.is_dir():
            return 0
        # `rglob`, not `glob`. A one-level glob sees only runs sitting
        # directly under the root, and a run one directory deeper -- which
        # nothing in this file forbids -- would have spent dispatches that
        # the next run was then handed again.
        for verdict_state in sorted(self.root.rglob("state.json")):
            try:
                data_ = json.loads(verdict_state.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # Fails **closed**. Skipping an unreadable state counted it as
                # zero, which is the most dangerous possible reading: the one
                # run whose record cannot be read is exactly the one that may
                # have spent the budget. A budget that cannot be computed is
                # not a budget, so nothing more is handed out.
                raise BudgetExhausted(
                    f"the shared dispatch budget cannot be computed: "
                    f"{verdict_state} is unreadable ({exc}). Nothing further is "
                    f"started, because a spend that cannot be read is not a "
                    f"spend of zero") from exc
            total += int((data_.get("usage") or {}).get("dispatches", 0) or 0)
        return total

    def remaining_budget(self) -> int:
        """What a run started now may still spend. Never negative."""
        if self.dispatch_budget is None:           # pragma: no cover - guarded
            return 0
        return max(self.dispatch_budget - self.spent_budget(), 0)

    def budget_argumente(self) -> list[str]:
        """The ceiling the next run is started with, as CLI arguments.

        A named method rather than two lines inside `prepare` so that the
        instrument control can measure the *wiring* and not only the
        arithmetic: `remaining_budget` returning the right number proves
        nothing if `prepare` passes a constant.

        Raises `BudgetExhausted` when nothing is left. It used to return
        `--max-dispatches 0`, which `Budgets` refuses (a ceiling of zero is
        not a budget, it is a stop), and the run then failed to start with a
        validation error that the orchestrator booked as a missing
        dependency. That is the ambiguous halt at precisely the moment the
        shared budget binds -- the case the shared budget was built for.
        """
        if self.dispatch_budget is None:
            return []
        remaining_ = self.remaining_budget()
        if remaining_ <= 0:
            raise BudgetExhausted(
                f"the shared dispatch budget is spent: "
                f"{self.spent_budget()}/{self.dispatch_budget} used by "
                f"the runs under {self.root}")
        return ["--max-dispatches", str(remaining_)]

    def _ensure_runnable(self, run_id: str, branch_: str, store: "RunStore") -> str | None:
        """Clears what an earlier attempt left in the way, or says what remains.

        Only two things are cleared, and both are operator actions rather than
        HoH's: granting the worktree trust, and lifting a run block that was
        *caused by* that missing trust. A block for any other reason is
        reported, not cleared -- unblocking a run whose block nobody
        understands is how a controller talks itself past a real problem.
        """
        try:
            state = store.read_state()
        except StoreError as exc:
            return f"run {run_id} has an unreadable state: {exc}"
        # Before any approval or unblock: a capability violation is lifted by
        # a person who has looked, never by this launcher -- whatever else the
        # block reason happens to mention.
        if violation_unresolved(state):
            reason = (state.blocked_reason or state.stop_reason or "").splitlines()
            return (f"run {run_id} is blocked by a capability violation; only a "
                    f"person lifts that (hoh unblock): "
                    f"{reason[0][:200] if reason else 'no reason recorded'}")
        if state.condition is not Condition.BLOCKED:
            return None

        reason = (state.blocked_reason or state.stop_reason or "")
        if not any(k in reason.lower() for k in APPROVAL_WAITING):
            return (
                f"run {run_id} is blocked for a reason this launcher does not "
                f"clear: {reason.splitlines()[0][:160] if reason else 'no reason recorded'}"
            )

        worktree = self._worktree_for(branch_)
        release_ = self._approve(worktree)
        if not release_.granted:
            return f"run {run_id} waits on a trust approval -- {release_.as_reason()}"

        ub = self._run_cli([
            *hoh_command(), "--root", str(self.root), "unblock", run_id,
        ])
        if ub.exit_code != 0:
            return f"trust granted but run {run_id} stayed blocked: {(ub.stderr or ub.stdout)[-160:]}"
        return None

    def _approve(self, worktree: Path) -> Approval:
        """Asks the configured authority, and records what it answered."""
        release_ = self.approvals.approve(worktree, self.repo_path)
        self.approvals_given.append(release_)
        return release_

    def _worktree_for(self, branch_: str) -> Path:
        """Where Herdr puts a worktree for this repository and branch."""
        return Path.home() / ".herdr" / "worktrees" / self.repo_path.name / branch_

    @staticmethod
    def _worktree_path(stdout: str) -> Path | None:
        try:
            data_ = json.loads(stdout)
        except Exception:
            return None

        def search_(obj):
            if isinstance(obj, dict):
                p = obj.get("path")
                if isinstance(p, str) and "worktree" in p:
                    return p
                for v in obj.values():
                    found = search_(v)
                    if found:
                        return found
            return None

        p = search_(data_)
        return Path(p) if p else None

    @staticmethod
    def _repair_spec(node: TaskNode) -> str:
        return (
            f"# Repair: {node.id}\n\n"
            f"A global gate failed on the merged state after every planned run "
            f"had been accepted. The individual runs were not wrong -- each was "
            f"checked against its own base and its own scope. What failed is the "
            f"combination.\n\n"
            f"## What failed\n\n```\n{node.note or '(no detail recorded)'}\n```\n\n"
            f"## What this run must do\n\n"
            f"Make that gate pass on the merged state, without changing what the "
            f"gate measures. Weakening the check instead of fixing the state would "
            f"satisfy this specification and defeat its purpose.\n\n"
            f"## Acceptance criteria\n\n"
            f"1. The command the gate runs exits 0.\n"
            f"2. The existing test suite still passes.\n\n"
            f"No cause is stated above on purpose: diagnosing it is this run's "
            f"work, and a specification that guesses hands the planner a "
            f"conclusion instead of a problem.\n"
        )

    def accepted_baseline(self, node: TaskNode) -> str | None:
        """The candidate id the run had already accepted before a dispatch."""
        store = RunStore(self.root, node.run_id or node.id)
        if not store.state_exists():
            return None
        try:
            k = store.read_state().last_accepted_candidate
        except StoreError:
            return None
        return k.candidate_id if k else None

    def evaluate(self, node: TaskNode) -> RunOutcome:
        """The verdict of an already-dispatched run, read rather than re-run.

        `node.accepted_before` is the baseline the dispatching session wrote
        into project state before it launched. Comparing against it is what
        makes a resume exactly-once: without it, a CHECKPOINTED run looks the
        same whether this dispatch accepted something or something was accepted
        three iterations earlier.
        """
        if self.dry_run:
            return RunOutcome(RunVerdict.NOT_RUN, "dry run: nothing to evaluate")
        store = RunStore(self.root, node.run_id or node.id)
        if not store.state_exists():
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"node {node.id} is marked RUNNING but no run state exists -- the "
                "dispatch may never have started",
            )
        try:
            state = store.read_state()
        except StoreError as exc:
            return RunOutcome(RunVerdict.UNDETERMINED, f"run state unreadable: {exc}")
        # A run still being executed by a live process is in flight -- not
        # crashed, and not something to take a verdict from. Two witnesses:
        # the process marker `hoh run` keeps for its whole lifetime (the run
        # lock alone is free between iterations, review B3 C), and the lock
        # itself for a run started by a version without the marker. Only a
        # busy lock counts; any other error probing it is UNDETERMINED, since
        # an unreadable lock file is not a live controller (review A3, F2).
        live = store.running_process()
        if live is not None:
            return RunOutcome(
                RunVerdict.IN_FLIGHT,
                f"run {node.run_id or node.id} is still executed by pid {live.get('pid')} "
                f"(since {live.get('since')})")
        try:
            with store.lock():
                pass
        except LockBusy as exc:
            return RunOutcome(RunVerdict.IN_FLIGHT, str(exc)[:200])
        except Exception as exc:                          # noqa: BLE001 - classified
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"the run lock could not be probed: {exc}"[:200])
        return self._verdict_from_baseline(
            state, node.accepted_before,
            open_seals=bool(open_seal_arenas(store.dir)))

    def launch(self, node: TaskNode) -> RunOutcome:
        if self.dry_run:
            return RunOutcome(RunVerdict.NOT_RUN, "dry run: nothing dispatched")

        run_id = node.run_id or node.id
        store = RunStore(self.root, run_id)

        # The baseline the controller recorded before this dispatch. Falling
        # back to reading it here covers a launcher used without a controller.
        before = node.accepted_before or self.accepted_baseline(node)

        argv = [
            *hoh_command(), "--root", str(self.root), "run", run_id,
            "--iterations", str(self.iterations),
            "--planner", self.planner, "--developer", self.developer, "--qa", self.qa,
            "--isolation", self.isolation,
        ]
        if self.master_session is not None and self.master_epoch is not None:
            argv += ["--master-session", str(self.master_session),
                     "--master-epoch", str(self.master_epoch)]
        for role_name, handle in sorted(self.role_accounts.items()):
            argv += ["--role-account", f"{role_name}={handle}"]
        if self.storage_root is not None:
            argv += ["--storage-root", str(self.storage_root)]
            refused = self._claim_storage_boot()
            if refused is not None:
                return RunOutcome(RunVerdict.STORAGE_UNAVAILABLE, refused)
        d = self._run_cli(argv)
        if d.exit_code == 2 and (d.stderr or "").lstrip().startswith(
                f"Run {run_id} is already being executed"):
            # Another process executes this run (its process lock refused ours):
            # the run is in flight, not rejected by this dispatch (review A5, M1).
            return RunOutcome(RunVerdict.IN_FLIGHT, d.stderr.strip()[:200])
        if d.exit_code != 0:
            said = storage_refusal(d.stderr)
            if said is not None:
                # `hoh run` refused to start for its storage configuration:
                # nothing ran, and a person has to fix the configuration.
                return RunOutcome(RunVerdict.STORAGE_UNAVAILABLE, said)

        try:
            state = store.read_state()
        except StoreError as exc:
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run {run_id} left no readable state (cli exit {d.exit_code}): {exc}",
            )

        return self._verdict(state, before, d,
                             open_seals=bool(open_seal_arenas(store.dir)))

    def _claim_storage_boot(self) -> str | None:
        """Claim the boot on the storage ledger before spawning (Revision 9
        item 7: every host entry point that starts runs claims first). Returns
        a refusal text, or None when claimed. An uninitialised root is left
        for the child to refuse, in its own words."""
        root = self.storage_root
        if root is None or not (root / "OWNER.json").is_file():
            return None
        try:
            if self._storage_admission_factory is not None:
                admission = self._storage_admission_factory(root)
            else:
                from .storage_pressure import StorageAdmission, StoragePolicy

                policy_file = root / "storage-policy.json"
                policy = (StoragePolicy.load(policy_file) if policy_file.is_file()
                          else StoragePolicy())
                admission = StorageAdmission.for_state_root(root, policy)
            admission.claim_boot()
        except Exception as exc:                          # noqa: BLE001 - classified
            return f"storage admission unavailable: the launcher cannot claim the boot: {exc}"
        return None

    def _open_seals_of(self, state) -> bool:
        """Does this run's own seal ledger leave a candidate arena sealed?

        For a caller that did not say (`open_seals=None`). A missing ledger is
        "no". An unreadable ledger, or a run id that names no directory of the
        root, is "yes": a gate that cannot tell holds.
        """
        try:
            run_dir = RunStore(self.root, state.run_id).dir
        except Exception:                                 # noqa: BLE001 - fail closed
            return True
        return bool(open_seal_arenas(run_dir))

    def _verdict_from_baseline(self, state, previous_id: str | None, *,
                               open_seals: bool | None = None) -> RunOutcome:
        return self._verdict(state, previous_id, Dispatch(0, "", ""),
                             open_seals=open_seals)

    def _verdict(self, state, before, d: Dispatch, *,
                 open_seals: bool | None = None) -> RunOutcome:
        # `vorher` is a candidate id (from project state) or a Candidate (from a
        # direct read). Normalised here so both callers share one comparison.
        previous_id = getattr(before, "candidate_id", before)
        after = state.last_accepted_candidate

        # Ahead of the rule that an acceptance is durable: a run held by a
        # capability violation has evidence that was interfered with, so
        # nothing it accepted is merged until a person has looked. UNDETERMINED
        # halts the node for that person.
        if violation_unresolved(state):
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run blocked by a capability violation; nothing it accepted is "
                f"merged until a person has looked: "
                f"{state.blocked_reason or state.stop_reason or ''}"[:400],
            )

        # A candidate arena still sealed for QA has not been compared at the
        # release: whether it was touched is not known yet, so nothing is
        # merged until a `hoh run` has closed the tabs and released it. The
        # flag is read from the run's own ledger when the caller did not say.
        if open_seals is None:
            open_seals = self._open_seals_of(state)
        if open_seals:
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                "a candidate arena sealed for QA is still open; nothing is "
                "merged until a later 'hoh run' has closed the tabs and "
                "released it (compared first)",
            )

        # STORAGE-B3, checked before the generic fallbacks. A recorded
        # acceptance still wins (it is durable, below); otherwise a storage
        # park is a wait of its own class and a storage refusal needs a
        # person. Neither ever reads as a provider wait or as UNDETERMINED.
        new_acceptance = after is not None and (
            previous_id is None or after.candidate_id != previous_id)
        if not new_acceptance:
            if state.condition is Condition.BLOCKED \
                    and (state.blocked_kind or "") == "storage_pressure":
                return RunOutcome(
                    RunVerdict.STORAGE_WAIT,
                    f"parked on storage pressure: {state.blocked_reason or ''}"[:300],
                    retry_after=_timer_form(state.retry_after),
                )
            said = storage_refusal(state.stop_reason) if state.condition in (
                Condition.PAUSED, Condition.BLOCKED) else None
            if said is not None:
                return RunOutcome(RunVerdict.STORAGE_UNAVAILABLE, said)

        # Handled first, and deliberately: an acceptance that a run recorded is
        # a fact about that run, and nothing that happens afterwards -- a
        # block, a spent budget, another iteration -- un-records it.
        if state.condition is Condition.BLOCKED and after is None:
            art = (state.blocked_kind or "").lower()
            reason_text = (state.blocked_reason or state.stop_reason or "").lower()
            # A trust dialog is not an unclassifiable state. HoH deliberately
            # never answers one itself -- granting trust to a directory it was
            # merely pointed at is exactly the capability it refuses to take --
            # so the run stops and waits. Naming that separately is the
            # difference between "answer the prompt" and "go find the defect".
            if any(k in reason_text for k in APPROVAL_WAITING):
                return RunOutcome(
                    RunVerdict.NEEDS_APPROVAL,
                    (state.blocked_reason or state.stop_reason or "awaiting approval")
                    .splitlines()[0][:200],
                )
            if any(k in art for k in PROVIDER_BLOCKED):
                return RunOutcome(
                    RunVerdict.PROVIDER_UNAVAILABLE,
                    f"blocked on the provider: {state.blocked_reason or art}",
                    retry_after=_timer_form(state.retry_after),
                )
            # Blocked with nothing accepted is not a rejection either -- it is
            # a state nobody has classified, and guessing is what this whole
            # design exists to avoid. A block *after* an acceptance is handled
            # above, by the rule that an acceptance is durable.
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run blocked ({state.blocked_kind}): {state.blocked_reason}",
            )

        if state.condition in (Condition.FAILED, Condition.CANCELLED):
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"run ended {state.condition.value}: {state.stop_reason}")

        if after is not None:
            if previous_id is not None and after.candidate_id == previous_id:
                # The run ended where it started. Merging on this would apply
                # the same candidate a second time.
                return RunOutcome(
                    RunVerdict.REJECTED,
                    f"no new candidate: still {after.candidate_id}",
                    candidate=after.commit, candidate_tree=after.tree_digest,
                )
            # **An acceptance is durable.** It does not depend on the stage the
            # run happened to stop in afterwards. This check used to live
            # inside the `CHECKPOINTED` branch, so a run that accepted a
            # candidate and then kept iterating -- because the budget allowed
            # it -- was reported by the stage it ran out in: `REJECTED` from
            # PLANNING, "iterations spent without a checkpoint". The candidate
            # was in the state the whole time.
            #
            # Measured twice in the benchmark, on two different branches of
            # this function (O122). A verdict that discards a recorded
            # acceptance costs the work, not just the report.
            postscript = ""
            if state.stage not in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
                postscript = (
                    f", and the run then continued to "
                    f"{state.stage.value}/{state.condition.value}"
                )
                if state.condition is Condition.BLOCKED:
                    postscript += f": {state.blocked_reason}"
            return RunOutcome(RunVerdict.ACCEPTED,
                              f"accepted {after.candidate_id}{postscript}",
                              candidate=after.commit, candidate_tree=after.tree_digest)

        if state.stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
            return RunOutcome(RunVerdict.UNDETERMINED,
                              "run checkpointed with no accepted candidate recorded")

        # A run that exists and has not begun. The most knowable state there
        # is, and reporting it as unclassifiable deadlocked an unattended run:
        # a process died between marking the node RUNNING and dispatching it,
        # and every later round re-read the same NEW state and halted on it.
        if state.stage is Stage.NEW and not state.iteration and after is None:
            return RunOutcome(
                RunVerdict.NOT_STARTED,
                "the run exists and has not begun: no iteration, no candidate",
            )

        if state.stage in (Stage.PLANNING, Stage.DEVELOPING, Stage.VERIFYING):
            if state.budget_exhausted():
                # A budget this project set for itself is not the provider
                # being unavailable. Reporting it as one put a policy ceiling
                # and somebody else's downtime under the same word, and the
                # two want opposite responses: wait, or decide whether the
                # work is worth more budget.
                return RunOutcome(RunVerdict.BUDGET_EXHAUSTED,
                                  f"budget exhausted in {state.stage.value}: "
                                  f"{state.budget_exhausted()}")
            return RunOutcome(
                RunVerdict.REJECTED,
                f"iterations spent without a checkpoint (stage {state.stage.value})",
            )

        return RunOutcome(RunVerdict.UNDETERMINED,
                          f"stage {state.stage.value}/{state.condition.value} "
                          f"(cli exit {d.exit_code})")

    # -- merge ------------------------------------------------------------- #

    def merge(self, node: TaskNode, outcome: RunOutcome) -> MergeResult:
        """Applies an accepted candidate, idempotently, and reports the failure.

        Nothing here resolves a conflict. What it does is refuse to throw away
        what git already said: the conflicting paths, the head the target was
        at, and the merge base are all recorded, so the halt names a state
        rather than a mystery.

        Classification is by what git reports, and only three shapes are
        claimed. Anything else stays UNKNOWN rather than being squeezed into
        the nearest label -- a misclassified failure is worse than an
        unclassified one, because it sends the reader somewhere specific and
        wrong.
        """
        branch_ = node.branch or f"hoh-{node.run_id or node.id}"
        result = MergeResult(landed=False, branch=branch_, candidate=outcome.candidate or "",
                             accepted_tree=outcome.candidate_tree or "")
        if self.dry_run:
            result.failure = MergeFailure.UNKNOWN
            result.detail = "dry run: no merge attempted"
            return result

        head = self._git("rev-parse", "--verify", "--quiet", branch_ + "^{commit}")
        if head.returncode != 0:
            result.failure = MergeFailure.NO_BRANCH
            result.detail = f"branch {branch_} does not exist in {self.repo_path}"
            return result
        # Resolved once, here, and used by identity from now on: the branch is
        # a mutable name, and everything after this line must be about the one
        # commit that was checked against the acceptance.
        sha = head.stdout.strip()
        problem = accepted_identity_problem(self._git, sha, outcome)
        if problem:
            result.failure = MergeFailure.CANDIDATE_MISMATCH
            result.detail = problem
            return result
        result.integrated = sha

        result.target_head_before = self._git(
            "rev-parse", "--short", self.mainline).stdout.strip()
        result.merge_base = self._git(
            "merge-base", sha, self.mainline).stdout.strip()[:12]

        # Already contained: a resumed session must not merge twice, and the
        # cheapest way to know is to ask whether the accepted commit is an
        # ancestor of the mainline.
        if self._git("merge-base", "--is-ancestor", sha, self.mainline).returncode == 0:
            result.landed = True
            result.detail = "already contained in the mainline"
            return result

        if self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != self.mainline:
            out = self._git("checkout", self.mainline)
            if out.returncode != 0:
                result.failure = MergeFailure.OBSTRUCTED
                result.detail = (out.stderr or out.stdout).strip().splitlines()[:1][0][:200] \
                    if (out.stderr or out.stdout).strip() else "checkout failed"
                return result

        # Taken on the mainline, after any switch to it: the switch alone must
        # not read as something the merge left behind (review A6, 3).
        status_before = self._git("status", "--porcelain",
                                  "--untracked-files=no").stdout.strip()
        p = self._git("merge", "--no-ff", sha, "-m",
                      f"Take accepted candidate {node.id} ({sha}): {outcome.detail}",
                      timeout=self.MERGE_TIMEOUT)
        if p.returncode == 0:
            result.landed = True
            return result

        text = (p.stdout or "") + (p.stderr or "")
        result.conflicting_paths = self._conflicting_paths(text)
        result.detail = self._first_meaningful(text)
        result.failure = self._classify(text)
        # Leave no half-merge behind for the next session to puzzle over --
        # where git lets us. `merge --abort` needs MERGE_HEAD; a git killed by
        # the timeout (inside a hook, say) may have staged the candidate
        # without writing it, and then nothing here may guess at a cleanup of
        # the integration checkout. That is said, not hidden (review A4, G3).
        merge_head = self._git("rev-parse", "-q", "--verify", "MERGE_HEAD")
        if merge_head.returncode == 0:
            self._git("merge", "--abort", timeout=self.MERGE_TIMEOUT)
        leftover = self._git("status", "--porcelain", "--untracked-files=no").stdout.strip()
        # Only what the merge itself left: changes that were there before it
        # (an operator's edit in the way) keep their own classification
        # (review A5, M2).
        if leftover and leftover != status_before:
            result.failure = MergeFailure.UNKNOWN
            result.detail = (f"{result.detail or 'merge failed'}; the mainline checkout "
                             f"is left with changes git did not undo ("
                             f"{', '.join(leftover.splitlines()[:4])}) -- a person must "
                             f"inspect it before any further merge")
        return result

    @staticmethod
    def _classify(text: str) -> MergeFailure:
        """What git said, mapped to a shape -- or UNKNOWN.

        The obstruction check comes first: git prints "would be overwritten by
        merge" for a dirty or occupied working tree *before* it attempts any
        content merge, so that message never accompanies a real conflict.
        """
        further_down = text.lower()
        if ("would be overwritten by merge" in further_down
                or "your local changes to the following files" in further_down
                or "please commit your changes or stash them" in further_down):
            return MergeFailure.OBSTRUCTED
        if "conflict (" in further_down or "automatic merge failed" in further_down:
            return MergeFailure.CONFLICT
        return MergeFailure.UNKNOWN

    @staticmethod
    def _conflicting_paths(text: str) -> tuple[str, ...]:
        paths: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"CONFLICT \([^)]*\): (.+?) (deleted in|added in|merge conflict)", s)
            if m:
                paths.append(m.group(1))
                continue
            if s and not s.startswith(("error:", "CONFLICT", "Auto", "Please", "Aborting",
                                       "Merge", "warning:", "hint:")) and "/" in s or (
                    s.endswith((".py", ".txt", ".md", ".json", ".pyc"))):
                if s not in paths and len(s) < 200 and " " not in s:
                    paths.append(s)
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _first_meaningful(text: str) -> str:
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith(("hint:", "warning:")):
                return s[:200]
        return "git produced no output"
