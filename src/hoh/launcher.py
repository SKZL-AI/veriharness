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
