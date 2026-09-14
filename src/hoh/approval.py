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

import stat
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
class PrefixScopedProvider(ApprovalProvider):
    """Approves worktrees beneath one directory, and nothing else.

    A fixed list of worktrees cannot cover work that does not exist yet. A
    repair node is created by a gate failure, gets its own worktree, and needs
    that worktree trusted -- all after any list would have been written. So the
    scope is a directory the deployment names: normally the place Herdr puts
    this project's worktrees.

    That is a wider grant than a fixed list, and because this provider can hand
    an agent write access to a directory, the containment check is written as
    if someone were trying to get past it:

    * **Canonical both sides.** Both paths are `resolve()`d, so `..` cannot
      walk out and a symlink inside the scope pointing at `/etc` resolves to
      `/etc` and is refused.
    * **Component-wise, not string-wise.** `/scope/foo` does not contain
      `/scope/foobar`. A `startswith` check would have said it does.
    * **The target must already exist, and be a directory.** `resolve()` is
      non-strict: a path that is not there resolves cleanly, so a bare prefix
      check would approve a name nobody has created yet -- and whoever creates
      it afterwards decides what it is.
    * **Re-checked at the last moment.** The identity (device and inode) is
      taken during the check and compared again immediately before the helper
      runs. A path swapped for a symlink in between is refused rather than
      approved on the strength of what it used to be.
    * **Optionally bound to one repository.** With `expected_repo` set, the
      worktree must actually be a linked worktree of that repository. A scope
      says where; this says whose.

    A prefix of `/`, a home directory, or anything else that would make the
    scope decorative is refused on construction.
    """

    script: Path
    prefix: Path
    #: When set, only worktrees whose git common directory belongs to this
    #: repository are approved. "Somewhere under ~/.herdr/worktrees" is a
    #: location; this is an owner.
    expected_repo: Path | None = None
    name: str = "prefix-scoped-script"
    timeout: int = 300
    granted: list[Path] = field(default_factory=list)

    #: Prefixes that would make the scope decorative. Refused on construction
    #: rather than at approval time, so a mistake is loud immediately.
    VERBOTEN = ("/", "/home", "/root", "/tmp", "/usr", "/etc", "/var", "/opt",
                "/srv", "/mnt", "/media", "/Users")

    def __post_init__(self) -> None:
        aufgeloest = self.prefix.expanduser().resolve()
        zuhause = Path.home().resolve()
        if (
            str(aufgeloest) in self.VERBOTEN
            or aufgeloest == zuhause
            or aufgeloest in zuhause.parents
            or len(aufgeloest.parts) < 3
        ):
            raise ValueError(
                f"{aufgeloest} is too broad to be a scope: a provider allowed "
                "to approve anything under it is a provider with no scope at all"
            )
        self.prefix = aufgeloest
        if self.expected_repo is not None:
            self.expected_repo = self.expected_repo.expanduser().resolve()

    # -- Containment --------------------------------------------------------- #

    def _identity(self, ziel: Path):
        """(device, inode) of a real directory, or None."""
        try:
            st = ziel.stat()
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        return (st.st_dev, st.st_ino)

    def _in_scope(self, worktree: Path) -> tuple[Path | None, str]:
        """The resolved target when it is inside the scope, else a reason."""
        try:
            ziel = worktree.expanduser().resolve()
        except OSError as exc:
            return None, f"{worktree} cannot be resolved: {exc}"
        if ziel == self.prefix:
            return None, f"{ziel} is the scope itself, not a worktree inside it"
        if self.prefix not in ziel.parents:
            return None, f"{ziel} is not beneath this provider's scope {self.prefix}"
        if self._identity(ziel) is None:
            # Non-strict resolution means a path nobody has created resolves
            # perfectly well. Approving one would grant trust to whatever the
            # next writer decides to put there.
            return None, (
                f"{ziel} does not exist as a directory; a scope check on a path "
                "that is not there approves whoever creates it next"
            )
        return ziel, ""

    def _belongs_to_repo(self, ziel: Path) -> str:
        """Empty when the worktree belongs to `expected_repo`, else a reason."""
        if self.expected_repo is None:
            return ""
        p = subprocess.run(
            ["git", "-C", str(ziel), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            return f"{ziel} is not a git worktree: {(p.stderr or p.stdout).strip()[:120]}"
        gemeinsam = Path(p.stdout.strip())
        if not gemeinsam.is_absolute():
            gemeinsam = (ziel / gemeinsam).resolve()
        erwartet = self.expected_repo
        if erwartet.name != ".git":
            erwartet = erwartet / ".git"
        if gemeinsam.resolve() != erwartet.resolve():
            return (
                f"{ziel} belongs to {gemeinsam}, not to this project's "
                f"{erwartet}"
            )
        return ""

    def may_approve(self, worktree: Path) -> bool:
        ziel, _ = self._in_scope(worktree)
        return ziel is not None and not self._belongs_to_repo(ziel)

    def approve(self, worktree: Path, repo: Path) -> Approval:
        def nein(detail: str) -> Approval:
            return Approval(granted=False, worktree=worktree, provider=self.name,
                            detail=detail)

        ziel, grund = self._in_scope(worktree)
        if ziel is None:
            return nein(grund)
        vorher = self._identity(ziel)
        grund = self._belongs_to_repo(ziel)
        if grund:
            return nein(grund)
        if not self.script.exists():
            return nein(f"the configured approval helper {self.script} does not exist")

        # Last look before handing the path to something that grants access.
        # Between the scope check and this line the directory could have been
        # replaced by a symlink to somewhere else entirely; the window is small
        # and the consequence is an agent with write access to that somewhere.
        if self._identity(ziel) != vorher or vorher is None:
            return nein(f"{ziel} changed identity between the scope check and the grant")

        p = subprocess.run([str(self.script), str(ziel), str(repo)],
                           capture_output=True, text=True, timeout=self.timeout)
        if p.returncode != 0:
            return nein(f"helper exited {p.returncode}: {(p.stderr or p.stdout)[-160:]}")
        self.granted.append(ziel)
        return Approval(granted=True, worktree=ziel, provider=self.name,
                        detail=f"approved by {self.script.name} under {self.prefix}")


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
