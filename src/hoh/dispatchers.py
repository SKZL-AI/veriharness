"""Concrete `RoleDispatcher` implementations.

Up to this point there was only the Protocol and the tests' `FakeDispatcher` --
an adversarial review rightly pointed out that `firstmate.py` and `herdr.py`
were not imported by a single line, which left A01/A02/A12 unprovable.

Two adapters, with different evidentiary weight:

* **`HerdrDispatcher`** starts every role as its own agent in its own Herdr
  pane. That yields the exact session, workspace and pane IDs that A01
  demands, and it satisfies "no silent tmux fallback": `require_herdr()`
  refuses to start outside of Herdr.
* **`HarnessDispatcher`** calls the same harness CLIs non-interactively as a
  subprocess. More robust and runnable without Herdr -- but **without a Herdr
  endpoint and therefore no A01 evidence**. It is the honest route for
  development and for machines without Herdr, not for acceptance.

## Why the answer goes into a file

The structured role output is **not** read from the terminal text. A pane
contains TUI frames, status lines and wrapped lines; fishing JSON out of that
is brittle, and the handoff explicitly declines to call terminal text a
dependable state. Instead every role is handed a file path and writes its
answer there. The dispatcher reads the file -- deterministic,
checkable, and the path shows up as evidence in the receipt directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from . import herdr
from .contracts import Role, RunState, utcnow
from .controller import DispatchError, WaitingForApproval

#: Roles whose answer has to come back structured. The developer leaves its
#: result in the artifact, not in an answer file.
STRUCTURED_ROLES = (Role.PLANNER, Role.QA)

#: Deadline for one role run. 15 minutes were too tight: in iteration 6 the
#: planner demonstrably spent 16+ minutes checking its own criteria against the
#: baseline ("does the mutant die on the annotation test instead of on the
#: arithmetic?") -- which is exactly the care that the discrimination check
#: demands of it. A deadline that cuts thorough work short trains shallow work.
DEFAULT_TIMEOUT_MS = 2_700_000        # 45 minutes
POLL_SECONDS = 2.0


def _answer_instruction(path: Path) -> str:
    return f"""

/answer-file
Write your JSON answer **as a file** to:

  {path}

Only the JSON object, nothing else -- no code fence, no surrounding text. Reply
in the chat with a single sentence saying that the file is written. The file is
authoritative; your chat text is not evaluated.
"""


class _Base:
    """Shared parts: answer path, reading the file, role profiles."""

    def __init__(
        self,
        *,
        answers_dir: Path,
        profiles: dict[Role, str] | None = None,
        models: dict[Role, str] | None = None,
        efforts: dict[Role, str] | None = None,
    ) -> None:
        #: Model and effort level per role. Empty means "whatever the harness
        #: defaults to" -- which is what HoH did exclusively until 2026-09-08.
        self.models = models or {}
        self.efforts = efforts or {}
        #: P1-09. What each role may do without asking, decided before the run
        #: and written into the run directory. None keeps the historical
        #: behaviour: the role inherits the operator's interactive defaults and
        #: a prompt stops the run for a human (O198).
        self.approval_policy = None
        self.answers_dir = Path(answers_dir)
        self.answers_dir.mkdir(parents=True, exist_ok=True)
        #: The run directory. Boundary for reusing an agent that Herdr
        #: restored: only what sits below here belongs to this run.
        self.store_dir = self.answers_dir.parent
        #: Root of the check directories. Planner and QA work there so that
        #: they do not sit in the evidence directory -- which is why it counts
        #: as owned by this run.
        self.arenas_dir = self.store_dir.parent / "_arenas" / self.store_dir.name
        self.profiles = profiles or {}
        self.endpoints: dict[Role, str] = {}
        #: Panes whose cleanup could not be confirmed. Lives here in the base
        #: class so that **every** dispatcher has the attribute: otherwise the
        #: reader in `cli.cmd_run` fell back to an empty default and reported
        #: "nothing unconfirmed" without any error becoming visible. For the
        #: `HarnessDispatcher` the empty list is the *correct* answer -- it
        #: starts no panes and therefore has nothing left open.
        self.unconfirmed: list[str] = []
        #: Per-role working directory. QA belongs in the arena, not in the
        #: live tree: a prompt is not enforcement (handoff §5), and its own
        #: artifacts otherwise broke the candidate binding.
        self.role_cwd: dict[Role, str] = {}

    def _answer_path(self, role: Role, state: RunState) -> Path:
        return self.answers_dir / f"i{state.iteration}-a{state.attempt}-{role.value}.json"

    #: Panes left blocked by a role that had already delivered. Kept so the
    #: situation stays visible rather than becoming invisible once it stops
    #: failing the run.
    _note_blocked_but_answered: dict = {}

    def _answered(self, path: Path, role: Role) -> bool:
        """Did the role deliver through its declared output channel?

        A structured role answers through exactly one file. Whether it answered
        is therefore a question about that file, and not about what its pane
        looks like afterwards.
        """
        if role not in STRUCTURED_ROLES:
            return False
        try:
            return bool(path.exists() and path.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    def _delivered(self, role: Role, before: str | None) -> bool:
        """Did an unstructured role deliver the thing it delivers?

        The developer's answer is not a file in the answers directory -- it is
        the artifact in the arena. So the question "did it deliver" is answered
        the same way as for the structured roles, by looking at the declared
        output rather than at the pane: did the working tree change.

        Measured on a real confinement run. The developer wrote `fib.py`,
        checked its own hash against the acceptance criterion, said *"Done --
        artifact left in the working tree for QA to execute independently"* and
        left its pane at a prompt. Herdr reports such a pane as `blocked`, and
        the run stopped on a dialog that was not there.
        """
        if role in STRUCTURED_ROLES or before is None:
            return False
        from .capability import tree_digest

        destination = self.role_cwd.get(role) or getattr(self, "cwd", None)
        return bool(destination) and tree_digest(Path(destination)) != before

    # -- O199: collecting the answer an approved dialog enabled -------------- #

    @staticmethod
    def _prompt_digest(full: str) -> str:
        import hashlib
        return hashlib.sha256(full.encode("utf-8")).hexdigest()

    @staticmethod
    def _marker_for(answer_path: Path) -> Path:
        return answer_path.with_name(answer_path.name + ".dispatch.json")

    def _write_dispatch_marker(self, answer_path: Path, full: str) -> None:
        """Remember what was asked, so a later dispatch can tell whether the
        answer on disk answers *this* question. A previous marker is kept with
        a version suffix, never overwritten."""
        marker = self._marker_for(answer_path)
        if marker.exists():
            marker.rename(marker.with_name(f"{marker.name}.v{utcnow()}"))
        marker.write_text(json.dumps({
            "prompt_sha256": self._prompt_digest(full),
            "dispatched_at": time.time(),
        }) + "\n", encoding="utf-8")

    def _resumable_answer(self, role: Role, answer_path: Path, full: str) -> bool:
        """Is the answer on disk the completed answer to this very prompt?

        O199. A dialog aborted the iteration; the operator answered it; the
        role finished and wrote its answer; the resumed run then parked that
        answer as "an earlier round" and asked again -- so every approval
        enabled work the next resume threw away, and the supervised path
        could not converge.

        All four must hold, and any doubt means the answer is parked as
        before rather than trusted: the role answers through a file; a marker
        from the dispatch exists; that dispatch asked exactly this prompt
        (same digest -- a changed iteration, attempt, amendment or spec
        changes the prompt); and the answer was written after it was asked.
        """
        if role not in STRUCTURED_ROLES:
            return False
        marker = self._marker_for(answer_path)
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
            written_at = answer_path.stat().st_mtime
            nonempty = bool(answer_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        return (nonempty
                and meta.get("prompt_sha256") == self._prompt_digest(full)
                and written_at > float(meta.get("dispatched_at") or 0))

    def _read_answer(self, path: Path, role: Role) -> str:
        if not path.exists():
            raise DispatchError(
                f"{role.value} left no answer file behind ({path}). "
                "Without a structured answer there is nothing to evaluate."
            )
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise DispatchError(f"{role.value} wrote an empty answer file ({path})")
        return text

    def _agent_args(self, role: Role) -> list[str]:
        """Extra arguments for the harness CLI, per role.

        `herdr agent start` forwards everything after `--` to the agent's own
        executable, and the `claude` CLI accepts `--model` and `--effort`.
        Until 2026-09-08 HoH used neither, so every role ran on whatever the
        operator's default happened to be -- invisible in the run record, and
        not selectable per role.

        Model diversity is not a detail here. The runbook already states that
        "a different model in a fresh context is the strongest setup for the
        independent review": the QA role is the one whose independence carries
        the acceptance, so being able to give it a different model from the
        developer's is a property worth having rather than a convenience.

        Whatever is chosen lands in the receipt trail through the role
        endpoint, so a later reader can see which model produced which
        verdict.
        """
        extra: list[str] = []
        model = self.models.get(role)
        effort = self.efforts.get(role)
        if model:
            extra += ["--model", model]
        if effort:
            extra += ["--effort", effort]
        policy = getattr(self, "approval_policy", None)
        if policy is not None:
            kind = (self.profiles or {}).get(role)
            if kind != "claude":
                # Fail closed: a policy HoH cannot express to this harness is
                # not quietly dropped, it stops the run before the role starts.
                raise DispatchError(
                    f"the approval policy can be applied to Claude roles only; "
                    f"{role.value} is configured as {kind!r}. Run it without a "
                    "policy, or give the role the claude harness.")
            settings, prompt = policy.write_role_files(self.store_dir, role.value)
            extra += policy.args(role.value, settings, prompt)
        return extra

    def endpoint_evidence(self, role: Role) -> str:
        return self.endpoints.get(role, "(no endpoint recorded)")


class HarnessDispatcher(_Base):
    """Calls the harness CLI non-interactively.

    **No A01 evidence**: no Herdr endpoint comes into being here. For
    development and for machines without Herdr; acceptance needs
    `HerdrDispatcher`.
    """

    #: Non-interactive invocation form per harness, checked against the
    #: installed CLI.
    COMMANDS: dict[str, list[str]] = {
        "claude": ["claude", "-p"],
        "pi": ["pi", "-p"],
        "kimi": ["kimi", "-p"],
        "codex": ["codex", "exec", "--skip-git-repo-check"],
    }

    def __init__(
        self,
        *,
        answers_dir: Path,
        profiles: dict[Role, str],
        cwd: Path | str,
        timeout: int = 900,
        models: dict[Role, str] | None = None,
        efforts: dict[Role, str] | None = None,
    ) -> None:
        super().__init__(answers_dir=answers_dir, profiles=profiles,
                         models=models, efforts=efforts)
        self.cwd = Path(cwd)
        self.timeout = timeout

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        """The answer arrives over **stdout**, not through a file.

        The first version had an answer file written here as well -- and failed
        immediately: `claude -p` runs with restricted permissions and may not
        create anything without approval ("Die Schreibberechtigung wurde nicht
        erteilt", the CLI's own wording). In non-interactive mode stdout is the
        intended and dependable channel; `roles.extract_json` tolerates
        surrounding text.
        """
        harness = self.profiles.get(role)
        if harness not in self.COMMANDS:
            raise DispatchError(
                f"no known harness for {role.value}: {harness!r}. "
                f"Known: {', '.join(sorted(self.COMMANDS))}"
            )

        try:
            proc = subprocess.run(
                [*self.COMMANDS[harness], *self._agent_args(role), prompt],
                cwd=self.role_cwd.get(role, self.cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DispatchError(
                f"{role.value} on {harness} gave no answer after {self.timeout}s",
                transient=True,
            ) from exc
        except OSError as exc:
            raise DispatchError(f"{harness} could not be started: {exc}") from exc

        self.endpoints[role] = f"subprocess:{harness}/rc={proc.returncode}"
        text = (proc.stdout or "").strip()

        if proc.returncode != 0 and not text:
            raise DispatchError(
                f"{harness} exited with {proc.returncode} and without output: "
                f"{(proc.stderr or '')[:300]}",
                transient=proc.returncode in (124, 143),
            )
        if role in STRUCTURED_ROLES and not text:
            raise DispatchError(f"{role.value} on {harness} produced no output")

        # Record the answer -- it is evidence even when it arrived over stdout.
        answer_path = self._answer_path(role, state)
        answer_path.write_text(text or "(no text)", encoding="utf-8")
        return text or "(no text)"


class HerdrDispatcher(_Base):
    """Starts every role as its own agent in its own Herdr pane.

    This is the route that can prove A01: the pane, tab and workspace IDs are
    real, verifiable endpoints. `require_herdr()` refuses to start outside a
    Herdr session -- a silent tmux fallback explicitly does not fulfill the
    assignment.
    """

    def __init__(
        self,
        *,
        answers_dir: Path,
        profiles: dict[Role, str],
        cwd: Path | str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        models: dict[Role, str] | None = None,
        efforts: dict[Role, str] | None = None,
    ) -> None:
        super().__init__(answers_dir=answers_dir, profiles=profiles,
                         models=models, efforts=efforts)
        herdr.require_herdr()
        self.cwd = str(Path(cwd).resolve())
        self.timeout_ms = timeout_ms
        self.agents: dict[Role, str] = {}
        self.own_panes: list[str] = []
        self.own_tabs: list[str] = []
        self.unconfirmed: list[str] = []

    # -- Pane and agent management ------------------------------------------- #

    def _agent_name(self, role: Role, state: RunState) -> str:
        """The Herdr agent name for one role of one run.

        The unique part comes first. The earlier form was
        `hoh-{run_id[:12]}-{role}`, so any two runs whose ids shared twelve
        characters shared an agent name (O195): `repair-3-feature-a` and
        `repair-3-feature-b` both became `hoh-repair-3-fea-developer`. The
        ownership check below refuses a name taken elsewhere, so nothing was
        ever adopted -- the second run simply could not start. Harmless one run
        at a time; a hard stop for independent nodes running together.

        A digest of the *whole* run id leads, because Herdr truncates agent
        names at about 24 characters and uniqueness after that point is
        uniqueness it throws away. The readable id follows for the operator.
        """
        digest = hashlib.sha256(state.run_id.encode("utf-8")).hexdigest()[:7]
        readable = state.run_id.lower().replace("_", "-")
        name = f"h{digest}-{role.value}-{readable}"
        return "".join(c if (c.isalnum() or c == "-") else "-" for c in name)[:32]

    def _ensure_agent(self, role: Role, state: RunState) -> str:
        if role in self.agents:
            return self.agents[role]

        kind = self.profiles.get(role)
        if not kind:
            raise DispatchError(f"no harness configured for role {role.value}")

        # HoH uses exclusively agents that it started itself.
        #
        # An earlier version adopted any idle session of the same harness here
        # ("the captain often keeps an open Kimi session for QA"). But
        # `find_agent` returns the FIRST matching agent anywhere in Herdr --
        # and that was the captain's running research session, which then got
        # handed a role prompt that did not belong to it.
        #
        # That is an intrusion into someone else's work and falls under the
        # same boundary as "foreign panes are never closed". Convenience is no
        # reason to write into a session that is not ours.
        name = self._agent_name(role, state)

        # After a restart Herdr restores panes and agents by itself. After a
        # hard reset `hoh-a02-planner` was therefore already back -- and HoH
        # tried to hand out the same name a second time ("agent_name_taken").
        # The reuse here is **not** the intrusion that was removed earlier:
        # what is looked up is not "any idle agent of the same harness" but
        # exactly the one deterministic name from HoH's own namespace, and only
        # if it also sits in this run's directory. Everything else is rejected
        # instead of adopted.
        restored = self._reusable_agent(name, state)
        if restored is not None:
            self.agents[role] = name
            self.endpoints[role] = restored
            return name

        pane = self._new_tab_pane(f"hoh {role.value}", cwd=self.role_cwd.get(role))
        self.own_panes.append(pane)
        self._start_agent(name, kind, pane, self._agent_args(role))
        self.agents[role] = name
        return name

    def _reusable_agent(self, name: str, state: RunState) -> str | None:
        """A HoH agent that Herdr restored -- or None.

        Returns the endpoint if an agent with **exactly this name** exists and
        its working directory belongs to this run. Raises if the name is taken
        but sits somewhere else: then it is not a restored agent of our own,
        and adopting it would be reaching into someone else's work.
        """
        try:
            snapshot = herdr.snapshot()
        except Exception:
            return None                      # no state readable -> start normally

        # A run has **two** legitimate places, not one: the run directory
        # (where planner and QA sit) and the authorized work tree (where the
        # developer sits). The first version knew only the run directory and
        # therefore rejected our own developer as a "foreign session". An
        # ownership test that does not know half of our own roles is not a
        # protection but a malfunction.
        # The ownership list is **derived**, not enumerated. Twice exactly that
        # enumeration went stale after a role had moved: first the developer's
        # work tree was missing, then the arena root where planner and QA sit
        # now. Both times the guard rejected one of our own roles as a "foreign
        # session" -- a protection that does not know our own roles is none.
        own_roots = [str(p) for p in (
            getattr(self, "store_dir", None),
            getattr(self, "arenas_dir", None),
            state.repo_path,
            *self.role_cwd.values(),
        ) if p]
        for row in snapshot.get("agents", []) or []:
            if str(row.get("name") or "") != name:
                continue
            cwd = str(row.get("foreground_cwd") or row.get("cwd") or "")
            if own_roots and not self._is_under(cwd, own_roots):
                raise DispatchError(
                    f"The agent name {name} is taken, but the agent sits in {cwd!r} "
                    f"instead of in a directory of this run ({', '.join(own_roots)}). "
                    "HoH does not adopt a foreign session -- check the pane and "
                    "close it deliberately."
                )
            status = str(row.get("agent_status", "unknown"))
            if status in ("working", "blocked"):
                error = DispatchError if status == "working" else WaitingForApproval
                raise error(
                    f"{name} {'is still working' if status == 'working' else 'waits for an approval'} "
                    f"(pane {row.get('pane_id')}). A running worker is reattached, "
                    "not overwritten -- wait first or check the pane."
                )
            if status not in ("idle", "done"):
                # `unknown` proves no completion -- and here no liveness
                # either. After the hard reset Herdr had restored the pane and
                # the name binding, but the agent process inside it was dead:
                # the start failed with `agent_name_taken`, prompting after
                # that with `agent_not_ready`. A revived name without an agent
                # is a dead end that only clearing away our **own** pane leads
                # out of -- cwd is checked above.
                dead_tab = str(row.get("tab_id") or "")
                if dead_tab:
                    try:
                        self._cli(["herdr", "tab", "close", dead_tab], "close dead tab")
                    except DispatchError:
                        pass
                return None
            ep = herdr.Endpoint(
                pane_id=str(row.get("pane_id", "")),
                workspace_id=str(row.get("workspace_id", "")),
                tab_id=str(row.get("tab_id", "")),
                agent=row.get("agent"),
                status=status,
                cwd=cwd or None,
            )
            tab = str(row.get("tab_id") or "")
            if tab and tab not in self.own_tabs:
                self.own_tabs.append(tab)
            return ep.evidence()
        return None

    def close_own(self) -> list[str]:
        """Closes only the tabs this adapter created itself, and **confirms**
        the termination.

        Handoff §7: *"`cancel` uses the existing controlled stop path for our
        own worker including its child processes; confirm the termination ...
        An unconfirmed stop stays visibly open."* An earlier version only
        closed and reported success without looking.

        Foreign tabs and panes stay untouched.
        """
        closed: list[str] = []
        still_open: list[str] = []
        for tab in self.own_tabs:
            try:
                subprocess.run(["herdr", "tab", "close", tab],
                               capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                still_open.append(tab)
                continue
            if self._tab_gone(tab):
                closed.append(tab)
            else:
                still_open.append(tab)

        self.own_tabs = still_open
        self.unconfirmed = still_open
        return closed

    def _tab_gone(self, tab: str) -> bool:
        """Looks, instead of assuming."""
        try:
            snapshot = herdr.snapshot()
        except Exception:
            return False   # not checkable does not mean confirmed
        present = {str(x.get("tab_id")) for x in (snapshot.get("tabs", []) or [])}
        return tab not in present

    def _new_tab_pane(self, label: str, *, cwd: str | None = None) -> str:
        """Every role gets its **own tab**, no further split.

        The first version split our own pane to the right over and over. After
        three roles every pane was a quarter wide, and Kimi's TUI stood at four
        characters of width -- the prompt arrived but was never processed
        ("agent_prompt_stalled"). The Herdr docs warn explicitly against
        repeated splits in the same direction.

        A tab of its own is also more polite: it does not push into the window
        the captain is currently working in.
        """
        args = ["herdr", "tab", "create", "--cwd", cwd or self.cwd,
                "--label", label, "--no-focus"]
        ws = os.environ.get("HERDR_WORKSPACE_ID")
        if ws:
            args += ["--workspace", ws]
        data = self._cli(args, "create tab")
        result = data.get("result", {})
        pane = result.get("root_pane", {}).get("pane_id")
        tab = result.get("tab", {}).get("tab_id")
        if not pane:
            raise DispatchError(f"Herdr returned no pane ID for the tab: {data}")
        if tab:
            self.own_tabs.append(str(tab))
        time.sleep(2.0)   # the shell of the new tab needs a moment
        return str(pane)

    def _start_agent(self, name: str, kind: str, pane: str,
                     extra: list[str] | None = None) -> None:
        # A freshly split pane needs a moment before the shell is at the
        # prompt. Otherwise Herdr rejects an agent start with
        # "not an available shell".
        data = None
        last_err = ""
        for attempt in range(10):
            try:
                args = ["herdr", "agent", "start", name, "--kind", kind,
                        "--pane", pane, "--timeout", "240000"]
                if extra:
                    args += ["--", *extra]
                data = self._cli(args, f"start agent {name}")
                break
            except DispatchError as exc:
                last_err = str(exc)
                if "not an available shell" not in last_err and "busy" not in last_err:
                    raise
                time.sleep(1.0 + attempt * 0.5)
        if data is None:
            raise DispatchError(
                f"agent {name} could not be started after 10 attempts: {last_err}"
            )
        agent = data.get("result", {}).get("agent", {})
        if not agent.get("pane_id"):
            raise DispatchError(f"agent {name} reports no pane -- start not evidenced")

    #: Margin on top of the deadline handed to Herdr. The subprocess has to
    #: wait **longer** than the command it is waiting for -- otherwise the
    #: wrapper aborts the very wait that it just requested.
    WRAPPER_OVERHEAD_S = 60.0

    @classmethod
    def _wrapper_timeout(cls, args: list[str], *, minimum: float = 180.0) -> float:
        """Derives the subprocess timeout from the `--timeout` in the command.

        A fixed value was a genuine operational defect here: `_cli` aborted
        after 180 s while the same call handed `--timeout 900000` to Herdr. The
        developer step legitimately takes longer than three minutes, and the
        run stalled on a deadline that nobody had set. The wrapper's deadline
        is therefore **derived**, not guessed.
        """
        try:
            ms = float(args[args.index("--timeout") + 1])
        except (ValueError, IndexError):
            return minimum
        return max(minimum, ms / 1000.0 + cls.WRAPPER_OVERHEAD_S)

    def _cli(self, args: list[str], purpose: str) -> dict:
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=self._wrapper_timeout(args)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DispatchError(f"{purpose} failed: {exc}", transient=True) from exc
        if proc.returncode != 0:
            raise DispatchError(f"{purpose} failed: {proc.stderr.strip()[:300]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise DispatchError(f"{purpose}: Herdr returned no JSON ({exc})") from exc

    @staticmethod
    def _is_under(path: str, roots: list[str]) -> bool:
        """A real path boundary instead of a string prefix.

        `cwd.startswith("/home/someone/hoh")` also matched
        `/home/someone/hoh-other` -- a sibling with a separator -- **and**
        `/home/someone/hohmirror`, where the prefix runs straight into the next
        word and no separator exists to notice. Those are two different shapes
        of the same defect, and the second is the one a reader is likely to
        dismiss. A reviewer evidenced four such cases as adopted, and with
        `status=unknown` a foreign tab was closed on top of that. `trust.register` does it right
        with `is_relative_to`, the dispatcher did not. This gets realistic with
        worktree siblings: `...-iter1` is a prefix of `...-iter10`.
        """
        try:
            target = Path(path).resolve()
        except (OSError, ValueError):
            return False
        for root in roots:
            try:
                if (target == Path(root).resolve()
                        or target.is_relative_to(Path(root).resolve())):
                    return True
            except (OSError, ValueError):
                continue
        return False

    @staticmethod
    def _pane_view(target: str, *, lines: int = 40) -> str:
        """The visible pane content as diagnostic text.

        Terminal text is **not** dependable run state -- the snapshot is
        responsible for that (handoff §3). As evidence for the cause of a
        failed dispatch it is, on the other hand, often the only thing there
        is. Never raises: a diagnosis must not replace the error it is meant
        to explain with a second one.
        """
        try:
            view = herdr.read_agent(target, lines=lines).strip()
        except Exception as exc:
            view = f"(pane not readable: {exc})"
        tail = "\n".join(ln for ln in view.splitlines() if ln.strip())[-1200:]
        return f"--- Visible in pane ---\n{tail or '(empty)'}\n-----------------------"

    # -- Dispatch ------------------------------------------------------------ #

    def dispatch(self, role: Role, prompt: str, *, state: RunState) -> str:
        target = self._ensure_agent(role, state)
        answer_path = self._answer_path(role, state)
        full = prompt + (
            _answer_instruction(answer_path) if role in STRUCTURED_ROLES else ""
        )
        if answer_path.exists():
            if self._resumable_answer(role, answer_path, full):
                # O199: the answer to this exact prompt, finished after an
                # operator answered the dialog that interrupted it. Collected,
                # not asked again -- and recorded as collected.
                self.endpoints[role] = (
                    f"collected after an approved dialog: {answer_path.name} "
                    f"written by {target} in answer to the same prompt")
                self.collected_after_dialog = getattr(
                    self, "collected_after_dialog", []) + [role.value]
                return self._read_answer(answer_path, role)
            # Do not reuse an answer from an earlier round.
            answer_path.rename(answer_path.with_name(f"{answer_path.name}.v{utcnow()}"))
        if role in STRUCTURED_ROLES:
            self._write_dispatch_marker(answer_path, full)

        # Witnessed before the dispatch, so an unstructured role's delivery can
        # be measured rather than read off its pane. Cheap: a digest over the
        # arena, which is the tree the role is expected to change.
        earlier = None
        if role not in STRUCTURED_ROLES:
            from .capability import tree_digest

            destination = self.role_cwd.get(role) or getattr(self, "cwd", None)
            if destination:
                earlier = tree_digest(Path(destination))
        try:
            data = self._cli(
                ["herdr", "agent", "prompt", target, full, "--wait",
                 "--timeout", str(self.timeout_ms)],
                f"{role.value} dispatch",
            )
        except DispatchError as exc:
            # Every dispatch failure takes the pane content with it, not just
            # a block. A "timed out waiting for agent status" says nothing by
            # itself: behind it there can be an exhausted quota, a crash, a
            # question back, or genuine compute time. Three times an A02 run
            # failed on a cause that was no longer determinable afterwards,
            # because the tab had been cleaned up.
            raise DispatchError(f"{exc}\n{self._pane_view(target)}",
                                transient=getattr(exc, "transient", False)) from exc

        agent = data.get("result", {}).get("agent", {})
        self.endpoints[role] = herdr.Endpoint(
            pane_id=str(agent.get("pane_id", "")),
            workspace_id=str(agent.get("workspace_id", "")),
            tab_id=str(agent.get("tab_id", "")),
            agent=agent.get("agent"),
            status=agent.get("agent_status"),
            cwd=agent.get("cwd"),
        ).evidence()

        status = str(agent.get("agent_status", ""))
        delivered_ = (
            self._answered(answer_path, role) or self._delivered(role, earlier)
        )
        if status == "blocked" and not delivered_:
            # **Read** the dialog before anything is cleaned up. Twice a run
            # came to a halt at an approval, and both times it was afterwards
            # not determinable which one -- the pane was gone. A blocker
            # without a cause is not a finding but a dead end. `agent read`
            # exists for exactly that; as run state the text is still no good,
            # as a diagnosis very much so.
            raise WaitingForApproval(
                f"{role.value} waits for an approval in pane {agent.get('pane_id')}. "
                "A blocked dialog is not answered automatically.\n"
                f"{self._pane_view(target)}"
            )
        if status == "blocked":
            # The pane is blocked and the role has **already answered**. A
            # planner that wrote its plan and then left its pane at an input
            # prompt is finished; the pane's state is a diagnosis, not a
            # verdict. Measured on a real confinement run: the plan file was
            # written, the transcript said so, and the run blocked anyway.
            #
            # This is what `RoleExecutionPolicy.output_channel` is for. A role
            # answers through exactly one file, so that is where the question
            # "did it answer" is decided -- not in a screen scrape.
            self._note_blocked_but_answered[role] = str(agent.get("pane_id"))

        if role not in STRUCTURED_ROLES:
            return f"developer run finished (status {status})"

        # The file can appear shortly after the end of the turn.
        deadline = time.monotonic() + 30
        while not answer_path.exists() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        return self._read_answer(answer_path, role)


def build_dispatcher(
    *,
    answers_dir: Path,
    profiles: dict[Role, str],
    cwd: Path | str,
    prefer_herdr: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    models: dict[Role, str] | None = None,
    efforts: dict[Role, str] | None = None,
    approval_policy=None,
) -> _Base:
    """Picks the adapter -- and says honestly when it is not the evidencing one."""
    if prefer_herdr:
        if not herdr.available():
            raise DispatchError(
                "Herdr is not available (HERDR_ENV != 1 or the CLI is missing). "
                "A01 demands the real Herdr endpoint; a silent fallback to a "
                "subprocess does not fulfill the assignment. With --no-herdr one "
                "can deliberately work without any acceptance value."
            )
        d = HerdrDispatcher(answers_dir=answers_dir, profiles=profiles, cwd=cwd,
                            timeout_ms=timeout_ms, models=models, efforts=efforts)
        d.approval_policy = approval_policy
        return d
    # The subprocess adapter counts in seconds, not in milliseconds.
    d = HarnessDispatcher(answers_dir=answers_dir, profiles=profiles, cwd=cwd,
                          timeout=max(1, timeout_ms // 1000),
                          models=models, efforts=efforts)
    d.approval_policy = approval_policy
    return d
