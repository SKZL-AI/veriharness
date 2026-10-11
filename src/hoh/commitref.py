"""A commit named by a full id that the product resolved itself -- the one resolver of the pin-and-import family.

Amendment A6 says it twice: `hoh pin` builds on "a full sha that the product resolved (a typed id or a ref name is not
accepted)" and `hoh review start` takes "a pin ... resolved to its full sha by the product ... and a typed id is not
accepted". Four slips of this build came from a short id, a ref name or a suffix that was typed by hand and was not the
commit the typist meant. So there is one rule, in one small module that every command of the family imports:

* what a caller types is a **full object id**: 40 or 64 lower-case hexadecimal digits. A short id, an upper-case id, a
  branch, a tag name, `HEAD`, a revision expression and a tag object are all refused, never expanded;
* the repository is then asked, by the product, whether that id is a **commit** that it holds, and the answer must be the
  id itself (so an annotated tag, which peels to a commit with another id, is refused);
* where a command needs the repository's present tip it asks for it with `head_commit`, which resolves `HEAD` here, in the
  product, and returns the full id to be recorded -- the caller never types `HEAD`.

The module also holds `git`, the one place where the family runs git: no inherited repository, index or configuration
(`archive_bundle._git_env`), no hook, no fsmonitor, no automatic maintenance (`NO_SIDE_EFFECTS`), a time limit, and optional
stdin and extra environment, and `require_new_ref`, the create-only check of a ref name.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping

from .archive_bundle import _QUIET, _git_env

#: A full object id as git prints it (SHA-1 or SHA-256 repositories), lower case only.
FULL_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

GIT_TIMEOUT = 120.0

#: Configuration given on the command line of every git call of the family (it beats every configuration file): no hook and
#: no fsmonitor program can run, and **no automatic maintenance**: a fetch otherwise runs `gc --auto`, which prunes
#: unreachable objects (the trees a merge preview writes, the objects of a refused import) -- "deletes nothing" would be false.
NO_SIDE_EFFECTS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
                   "-c", "maintenance.auto=false", "-c", "fetch.writeCommitGraph=false")


class CommitRefused(Exception):
    """A commit reference that the product does not accept, or a git call that failed. Callers turn it
    into their own refusal (exit 2) and create nothing."""


def git(repo: str, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None,
        allow: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """git in `repo` under the product's scrubbed environment; a return code outside `allow` is a refusal.

    `env` adds variables (an index file, an identity) on top of the scrubbed environment. Nothing is run
    through a shell and no hook or fsmonitor program can run."""
    command = ["git", *_QUIET, *NO_SIDE_EFFECTS, "-C", repo, *args]
    environment = _git_env()
    if env:
        environment.update(env)
    try:
        done = subprocess.run(command, capture_output=True, env=environment, input=stdin,
                              stdin=None if stdin is not None else subprocess.DEVNULL,
                              timeout=GIT_TIMEOUT, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise CommitRefused(f"git cannot be run in {repo!r}: {exc}") from exc
    if done.returncode not in allow:
        raise CommitRefused(f"git {args[0]} failed in {repo!r}: "
                            + done.stderr.decode("utf-8", "replace").strip()[:300])
    return done


def require_full_id(what: str, typed: object) -> str:
    """`typed` itself when it is a full lower-case object id; nothing is expanded or looked up."""
    if not isinstance(typed, str) or FULL_ID.fullmatch(typed) is None:
        raise CommitRefused(f"{what} {typed!r} is not a full commit id (40 or 64 lower-case hex digits): "
                            "a short id, a ref or a name is not accepted")
    return typed


def resolve_commit(repo: str, what: str, typed: object) -> str:
    """The commit `typed` names, resolved now in `repo`; `typed` must already be that full id.

    Raises `CommitRefused` for anything else: a short id, a name, an id the repository does not hold, an
    id of a tree, blob or tag (a tag that peels to a commit included)."""
    full = require_full_id(what, typed)
    done = git(repo, "rev-parse", "--verify", "--quiet", full + "^{commit}", allow=(0, 1))
    got = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or not got:
        raise CommitRefused(f"{what} {full[:12]} is not a commit of the repository")
    if got != full:
        raise CommitRefused(f"{what} {full[:12]} names an object that peels to {got[:12]}, not a commit "
                            "itself")
    return got


def head_commit(repo: str) -> str:
    """The full id of the commit `HEAD` is on, resolved here, now (what a command records as "the base")."""
    done = git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", allow=(0, 1))
    got = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or FULL_ID.fullmatch(got) is None:
        raise CommitRefused(f"HEAD of {repo!r} is not a commit")
    return got


def require_new_ref(repo: str, ref: str) -> None:
    """Refuse a ref name that exists or that a ref under it (`<ref>/...`) already occupies: `update-ref` would fail
    on it only after the objects were written. Checked before the first write."""
    if git(repo, "rev-parse", "--verify", "--quiet", ref, allow=(0, 1)).returncode == 0:
        raise CommitRefused(f"{ref} exists: it is created, never moved")
    if git(repo, "for-each-ref", "--count=1", "--format=%(refname)", ref + "/").stdout.strip():
        raise CommitRefused(f"{ref} would collide with a ref under {ref}/")
