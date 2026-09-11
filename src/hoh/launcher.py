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
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contracts import Condition, Stage
from .orchestrator import RunLauncher, RunOutcome, RunVerdict
from .project import ActionClass, TaskNode
from .store import RunStore, StoreError

#: Blocked kinds that mean the provider, not the work.
PROVIDER_BLOCKED = frozenset({"usage_quota", "quota", "rate_limit", "provider"})

#: Phrases in a stop reason that mean a person has to answer something. Matched
#: on the reason text rather than a code because HoH records this case in prose:
#: `blocked_kind` was None for the trust dialog that first exposed it.
APPROVAL_WAITING = frozenset({"waits for an approval", "trust this folder",
                              "blocked dialog", "awaiting approval"})


@dataclass
class Dispatch:
    """One `hoh run` invocation's raw result."""

    exit_code: int
    stdout: str
    stderr: str


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
        trust_script: Path | str | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.mainline = mainline
        self.iterations = iterations
        # Named explicitly, never defaulted. `hoh run --qa` defaults to a
        # harness whose credential expired here, and the resulting run produced
        # no QA verdict at all while looking like an ordinary dispatch.
        self.planner, self.developer, self.qa = planner, developer, qa
        self.timeout = timeout
        self.dry_run = dry_run
        # Granting a worktree trust is an operator action, not something HoH
        # does: HoH trusts only its own directories, on purpose, because
        # otherwise it could obtain access to any directory simply by being
        # pointed at it. A deployment layer may hold that authority; this is
        # where it is named, so it is visible rather than implicit.
        self.trust_script = Path(trust_script).expanduser() if trust_script else None

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

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True,
            cwd=str(cwd or self.repo_path),
        )

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
        run_id = node.run_id or node.id
        zweig = node.branch or f"hoh-{run_id}"
        store = RunStore(self.root, run_id)

        if store.state_exists():
            # The run exists, but that does not mean it can proceed. Blocks
            # come in a chain: the project node, the run, and the worktree's
            # trust. Measured the first time a repair node was dispatched for
            # real -- the node was unblocked, the run was still BLOCKED from
            # its previous attempt, and `hoh run` refused to start at all.
            return self._ensure_runnable(run_id, zweig, store)
        spec_pfad = Path(node.spec_path) if node.spec_path else (
            self.repo_path / f".hoh-repair-{run_id}.md"
        )
        if not spec_pfad.exists():
            if not node.repair_of:
                return (
                    f"node {run_id} has no run and no specification at "
                    f"{spec_pfad}; there is nothing to dispatch"
                )
            spec_pfad.write_text(self._repair_spec(node), encoding="utf-8")

        wt = self._run_cli([
            "python3", "-m", "hoh.cli", "--root", str(self.root),
            "worktree", "--repo", str(self.repo_path), "--branch", zweig,
        ])
        if wt.exit_code != 0 and "already exists" not in (wt.stdout + wt.stderr):
            return f"could not create the worktree for {run_id}: {(wt.stderr or wt.stdout)[-200:]}"
        worktree = self._worktree_path(wt.stdout) or (
            Path.home() / ".herdr" / "worktrees" / self.repo_path.name / zweig
        )
        if not worktree.is_dir():
            return f"no worktree at {worktree} for {run_id}"

        # Without this the run starts and then stops at a trust dialog, which
        # HoH will not answer. Measured the first time a repair node was
        # dispatched for real: the run reached DEVELOPING and blocked on
        # "Yes, I trust this folder".
        if self.trust_script and self.trust_script.exists():
            tr = subprocess.run(
                [str(self.trust_script), str(worktree), str(self.repo_path)],
                capture_output=True, text=True, timeout=300,
            )
            if tr.returncode != 0:
                return (
                    f"could not grant trust for {worktree}: "
                    f"{(tr.stderr or tr.stdout)[-200:]}"
                )
        elif self.trust_script:
            return (
                f"no trust helper at {self.trust_script}; the run would start and "
                "then stop at a trust dialog, which HoH does not answer"
            )

        st = self._run_cli([
            "python3", "-m", "hoh.cli", "--root", str(self.root), "start",
            "--repo", str(worktree), "--spec", str(spec_pfad), "--run-id", run_id,
        ])
        if st.exit_code != 0:
            return f"could not start run {run_id}: {(st.stderr or st.stdout)[-200:]}"
        node.branch = zweig
        node.run_id = run_id
        node.spec_path = str(spec_pfad)
        return None

    def _ensure_runnable(self, run_id: str, zweig: str, store: "RunStore") -> str | None:
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
        if state.condition is not Condition.BLOCKED:
            return None

        grund = (state.blocked_reason or state.stop_reason or "")
        if not any(k in grund.lower() for k in APPROVAL_WAITING):
            return (
                f"run {run_id} is blocked for a reason this launcher does not "
                f"clear: {grund.splitlines()[0][:160] if grund else 'no reason recorded'}"
            )

        worktree = Path.home() / ".herdr" / "worktrees" / self.repo_path.name / zweig
        if self.trust_script and self.trust_script.exists() and worktree.is_dir():
            tr = subprocess.run(
                [str(self.trust_script), str(worktree), str(self.repo_path)],
                capture_output=True, text=True, timeout=300,
            )
            if tr.returncode != 0:
                return f"could not grant trust for {worktree}: {(tr.stderr or tr.stdout)[-160:]}"
        else:
            return (
                f"run {run_id} waits on a trust approval for {worktree} and no "
                "trust helper is configured to grant it"
            )

        ub = self._run_cli([
            "python3", "-m", "hoh.cli", "--root", str(self.root), "unblock", run_id,
        ])
        if ub.exit_code != 0:
            return f"trust granted but run {run_id} stayed blocked: {(ub.stderr or ub.stdout)[-160:]}"
        return None

    @staticmethod
    def _worktree_path(stdout: str) -> Path | None:
        try:
            daten = json.loads(stdout)
        except Exception:
            return None

        def suche(obj):
            if isinstance(obj, dict):
                p = obj.get("path")
                if isinstance(p, str) and "worktree" in p:
                    return p
                for v in obj.values():
                    gefunden = suche(v)
                    if gefunden:
                        return gefunden
            return None

        p = suche(daten)
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
        # A controller still holding the run's lock means the work is live, not
        # crashed. Taking a verdict from a run in flight would be a guess.
        try:
            with store.lock():
                pass
        except Exception:
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run {node.run_id or node.id} is still held by a live controller",
            )
        return self._verdict_from_baseline(state, node.accepted_before)

    def launch(self, node: TaskNode) -> RunOutcome:
        if self.dry_run:
            return RunOutcome(RunVerdict.NOT_RUN, "dry run: nothing dispatched")

        run_id = node.run_id or node.id
        store = RunStore(self.root, run_id)

        # The baseline the controller recorded before this dispatch. Falling
        # back to reading it here covers a launcher used without a controller.
        vorher = node.accepted_before or self.accepted_baseline(node)

        d = self._run_cli([
            "python3", "-m", "hoh.cli", "--root", str(self.root), "run", run_id,
            "--iterations", str(self.iterations),
            "--planner", self.planner, "--developer", self.developer, "--qa", self.qa,
        ])

        try:
            state = store.read_state()
        except StoreError as exc:
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run {run_id} left no readable state (cli exit {d.exit_code}): {exc}",
            )

        return self._verdict(state, vorher, d)

    def _verdict_from_baseline(self, state, vorher_id: str | None) -> RunOutcome:
        return self._verdict(state, vorher_id, Dispatch(0, "", ""))

    def _verdict(self, state, vorher, d: Dispatch) -> RunOutcome:
        # `vorher` is a candidate id (from project state) or a Candidate (from a
        # direct read). Normalised here so both callers share one comparison.
        vorher_id = getattr(vorher, "candidate_id", vorher)
        nachher = state.last_accepted_candidate

        if state.condition is Condition.BLOCKED:
            art = (state.blocked_kind or "").lower()
            grund_text = (state.blocked_reason or state.stop_reason or "").lower()
            # A trust dialog is not an unclassifiable state. HoH deliberately
            # never answers one itself -- granting trust to a directory it was
            # merely pointed at is exactly the capability it refuses to take --
            # so the run stops and waits. Naming that separately is the
            # difference between "answer the prompt" and "go find the defect".
            if any(k in grund_text for k in APPROVAL_WAITING):
                return RunOutcome(
                    RunVerdict.NEEDS_APPROVAL,
                    (state.blocked_reason or state.stop_reason or "awaiting approval")
                    .splitlines()[0][:200],
                )
            if any(k in art for k in PROVIDER_BLOCKED):
                return RunOutcome(
                    RunVerdict.PROVIDER_UNAVAILABLE,
                    f"blocked on the provider: {state.blocked_reason or art}",
                )
            # Blocked for some other reason is not a rejection either -- it is
            # a state nobody has classified, and guessing is what this whole
            # design exists to avoid.
            return RunOutcome(
                RunVerdict.UNDETERMINED,
                f"run blocked ({state.blocked_kind}): {state.blocked_reason}",
            )

        if state.condition in (Condition.FAILED, Condition.CANCELLED):
            return RunOutcome(RunVerdict.UNDETERMINED,
                              f"run ended {state.condition.value}: {state.stop_reason}")

        if state.stage in (Stage.CHECKPOINTED, Stage.READY_FOR_DELIVERY):
            if nachher is None:
                return RunOutcome(RunVerdict.UNDETERMINED,
                                  "run checkpointed with no accepted candidate recorded")
            if vorher_id is not None and nachher.candidate_id == vorher_id:
                # The run ended where it started. Merging on this would apply
                # the same candidate a second time.
                return RunOutcome(
                    RunVerdict.REJECTED,
                    f"no new candidate: still {nachher.candidate_id}",
                    candidate=nachher.commit,
                )
            return RunOutcome(RunVerdict.ACCEPTED,
                              f"accepted {nachher.candidate_id}", candidate=nachher.commit)

        if state.stage in (Stage.PLANNING, Stage.DEVELOPING, Stage.VERIFYING):
            if state.budget_exhausted():
                return RunOutcome(RunVerdict.PROVIDER_UNAVAILABLE,
                                  f"budget exhausted in {state.stage.value}")
            return RunOutcome(
                RunVerdict.REJECTED,
                f"iterations spent without a checkpoint (stage {state.stage.value})",
            )

        return RunOutcome(RunVerdict.UNDETERMINED,
                          f"stage {state.stage.value}/{state.condition.value} "
                          f"(cli exit {d.exit_code})")

    # -- merge ------------------------------------------------------------- #

    def merge(self, node: TaskNode, outcome: RunOutcome) -> bool:
        """Merges the node's branch into the mainline, idempotently.

        Returns False rather than raising when the merge does not land: the
        controller has a halt class for exactly that, and it is one of the
        cases that must not be guessed at.
        """
        if self.dry_run:
            return False
        zweig = node.branch or f"hoh-{node.run_id or node.id}"

        if self._git("rev-parse", "--verify", "--quiet", zweig).returncode != 0:
            return False

        # Already contained: a resumed session must not merge twice, and the
        # cheapest way to know is to ask whether the branch is an ancestor.
        if self._git("merge-base", "--is-ancestor", zweig, self.mainline).returncode == 0:
            return True

        if self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != self.mainline:
            if self._git("checkout", self.mainline).returncode != 0:
                return False

        p = self._git("merge", "--no-ff", zweig, "-m",
                      f"Take accepted candidate {node.id} ({outcome.detail})")
        if p.returncode != 0:
            # Leave no half-merge behind for the next session to puzzle over.
            self._git("merge", "--abort")
            return False
        return True
