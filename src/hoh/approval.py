"""Who may grant a worktree the trust a run needs, and for which worktree.

HoH refuses to trust a directory it was merely pointed at. That refusal is not
an oversight to work around: trusting on request would let anything that can
name a path obtain an agent's access to it. So a fresh worktree stops at a
trust dialog, and the run waits.

That is correct and it is also where an otherwise unattended loop stops. This
module is the narrow way through: an authority that a deployment can configure,
which may approve **only the worktrees this project created**, and which does
not exist by default.

Three properties, and each is the answer to a way this could go wrong:

* **Absent by default.** No provider means the run blocks and a person
  answers. Nothing about adding this module changes what happens to a
  deployment that does not configure one.
* **Scoped, not general.** A provider is given the worktrees it may approve.
  Asked about anything else it refuses, so a compromised or careless caller
  cannot turn a trust helper into a general grant.
* **It says what it did.** Approval returns a record, not a boolean, because
  "trust was granted" without saying to what is the same shape of claim this
  project keeps having to correct.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Approval:
    """The outcome of asking for a worktree to be trusted."""

    granted: bool
    worktree: Path
    provider: str
    detail: str = ""

    def as_reason(self) -> str:
        zustand = "granted" if self.granted else "refused"
        return f"{self.provider} {zustand} trust for {self.worktree}" + (
            f": {self.detail}" if self.detail else ""
        )


class ApprovalProvider(Protocol):
    """An authority that can answer a trust prompt for specific worktrees."""

    name: str

    def may_approve(self, worktree: Path) -> bool: ...

    def approve(self, worktree: Path, repo: Path) -> Approval: ...


@dataclass
class NoApprovalProvider(ApprovalProvider):
    """The default. Refuses everything, and says so usefully.

    Its whole job is to make "nobody is authorised to answer this" a distinct,
    readable state rather than an unexplained halt.
    """

    name: str = "none"

    def may_approve(self, worktree: Path) -> bool:
        return False

    def approve(self, worktree: Path, repo: Path) -> Approval:
        return Approval(
            granted=False, worktree=worktree, provider=self.name,
            detail=(
                "no approval authority is configured; a person has to answer the "
                "trust prompt, or the deployment has to name one explicitly"
            ),
        )


@dataclass
class ScopedScriptProvider(ApprovalProvider):
    """Runs a configured helper, for a fixed set of worktrees and no others.

    The scope is the point. A helper that can trust any path is a helper that
    turns "HoH will not trust arbitrary directories" back into "HoH will trust
    arbitrary directories, via this". Here the deployment names the worktrees
    when it constructs the provider -- normally the ones the project itself
    created -- and anything outside that set is refused without the helper
    being run at all.

    There is no default script path. A deployment that wants this configures
    it; a hardcoded location would make one machine's layout a product
    requirement.
    """

    script: Path
    allowed: tuple[Path, ...] = ()
    name: str = "scoped-script"
    timeout: int = 300
    #: Worktrees approved so far, for the record.
    granted: list[Path] = field(default_factory=list)

    def may_approve(self, worktree: Path) -> bool:
        ziel = worktree.resolve()
        return any(ziel == p.resolve() for p in self.allowed)

    def approve(self, worktree: Path, repo: Path) -> Approval:
        if not self.may_approve(worktree):
            return Approval(
                granted=False, worktree=worktree, provider=self.name,
                detail=(
                    f"{worktree} is not in this provider's scope; it may approve "
                    f"{len(self.allowed)} worktree(s) created for this project and "
                    "nothing else"
                ),
            )
        if not self.script.exists():
            return Approval(
                granted=False, worktree=worktree, provider=self.name,
                detail=f"the configured approval helper {self.script} does not exist",
            )
        p = subprocess.run(
            [str(self.script), str(worktree), str(repo)],
            capture_output=True, text=True, timeout=self.timeout,
        )
        if p.returncode != 0:
            return Approval(
                granted=False, worktree=worktree, provider=self.name,
                detail=f"helper exited {p.returncode}: {(p.stderr or p.stdout)[-160:]}",
            )
        self.granted.append(worktree)
        return Approval(granted=True, worktree=worktree, provider=self.name,
                        detail=f"approved by {self.script.name}")
