"""Shared fixtures, and one fail-closed rule about evidence the export withholds.

This project publishes its tools and its results and deliberately does not
publish some of the material those tools measure. A test that measures one of
those has nothing to measure in a published clone, and must say so and skip.

The dangerous version of that is `path missing -> skip`. Under it, an export
that **lost** a path it was supposed to ship would turn the affected tests
green-by-skipping instead of red, and the check would be fail-open exactly
where it is meant to be strict. So a skip is earned, not assumed, and all of
these have to hold:

1. the path really is absent;
2. `EXPORT_MANIFEST.json` is present and parses;
3. that path -- or, for a directory, the files under it -- is in the manifest;
4. the manifest says `EXCLUDE`, with the rule this kind of artifact is
   expected to carry;
5. the skip names the artifact and why it is withheld, in this artifact's own
   words rather than a generic "evidence missing".

Anything else fails:

    missing + expected EXCLUDE rule   -> skip, environment gap
    missing + INCLUDE                 -> fail, the export lost a public path
    missing + unclassified            -> fail, nobody decided about it
    missing + manifest absent/broken  -> fail, nothing authorises the skip
    missing + a different EXCLUDE rule-> fail, withheld for a reason that is
                                         not the one this test expects
    present                           -> no skip; the test runs

`unclassified` failing matters: it is the manifest's conservative default, the
answer it gives when no rule claimed a path. An undecided path is not a
declared withholding and must never buy a green test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "EXPORT_MANIFEST.json"


class Withheld:
    """One kind of artifact the export withholds, and why."""

    def __init__(self, path: str, rule_: str, reason: str) -> None:
        self.path, self.rule_, self.reason = path, rule_, reason


#: Each kind names its own reason. "evidence missing" would be true of all of
#: them and useful about none.
HIDDEN_FIXTURES = Withheld(
    "dogfood/benchmark/tasks", "internal-working-document",
    "hidden benchmark fixtures intentionally withheld to preserve future "
    "benchmark secrecy -- the protocol's premise is that the hidden suite is "
    "the verdict and no arm sees it")
RUN_EVIDENCE_V2 = Withheld(
    "dogfood/benchmark/results-v2", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS_v2.md carries "
    "what they measured")
RUN_EVIDENCE_V1 = Withheld(
    "dogfood/benchmark/results", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS.md carries "
    "what they measured")
CONFINEMENT_EVIDENCE = Withheld(
    "dogfood/planner-confinement", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/EVIDENCE_INDEX.md publishes the "
    "tree digests and every path-free field")
SELFHOST_EVIDENCE = Withheld(
    "dogfood/selfhost-e2e", "internal-working-document",
    "excluded because the run tree records the worktree each dispatch ran in; "
    "dogfood/ATTRIBUTION.json names what it established")
RUN_EVIDENCE_V3 = Withheld(
    "dogfood/benchmark/results-v3", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS_v3.md carries "
    "what they measured")

#: V3.3 P0. The requirement register is a reading of the operator's planning
#: package, which is not published: the plan is the customer's, the tree is
#: the product's. A published clone therefore cannot derive the capability
#: matrix, and must say so rather than pretend the matrix is empty.
PROGRAM_REGISTER = Withheld(
    "program/v3_3/REQUIREMENTS.json", "unclassified",
    "the V3.3 requirement register is derived from an internal planning "
    "package and stays in the internal tree; without it a capability matrix "
    "cannot be derived here, and an empty matrix would read as a repository "
    "with no requirements")


def _manifest() -> dict[str, tuple[str, str]]:
    """path -> (decision, rule). Raises if the manifest cannot authorise anything."""
    if not MANIFEST.is_file():
        pytest.fail(
            f"{MANIFEST.name} is absent, so nothing authorises an "
            f"environment-gap skip. A missing path with no manifest is a "
            f"broken tree, not a withheld artifact.")
    try:
        data_ = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except ValueError as exc:
        pytest.fail(f"{MANIFEST.name} does not parse ({exc}), so nothing "
                    f"authorises an environment-gap skip")
    entries = data_.get("entries") if isinstance(data_, dict) else data_
    return {e["path"]: (e["decision"], e["rule"]) for e in entries or []}


def needs_evidence(art: Withheld) -> None:
    """Skip only if the manifest declares this artifact withheld, for its
    expected reason. Fail in every other absent case; do nothing if present."""
    target = ROOT / art.path
    if target.exists():
        return

    entries = _manifest()
    affected = {p: v for p, v in entries.items()
                 if p == art.path or p.startswith(art.path + "/")}
    if not affected:
        pytest.fail(
            f"{art.path} is absent and the manifest does not classify it. An "
            f"undecided path is not a declared withholding, so this is a "
            f"broken tree rather than an environment gap.")

    included_ = sorted(p for p, (d, _) in affected.items() if d == "INCLUDE")
    if included_:
        pytest.fail(
            f"{art.path} is absent but the manifest says INCLUDE for "
            f"{len(included_)} path(s) under it (e.g. "
            f"{included_[0]}). The export lost a path it was supposed to "
            f"ship; that is a failure, not a gap.")

    wrong_ = sorted({r for _, (d, r) in affected.items()
                      if d == "EXCLUDE" and r != art.rule_})
    if wrong_:
        pytest.fail(
            f"{art.path} is absent and excluded, but under rule(s) "
            f"{', '.join(wrong_)} rather than the expected {art.rule_!r}. "
            f"A skip may only rest on the reason this test expects.")

    pytest.skip(f"{art.path} is not in this tree -- {art.reason}")


def needs_parked_predecessor(baseline: str) -> None:
    """The superseded-artifact case: a `<name>.v<stamp>` sibling of `basis`."""
    folder = (ROOT / baseline).parent
    if folder.is_dir() and sorted(folder.glob(Path(baseline).name + ".v*")):
        return
    entries = _manifest()
    parked = {p: v for p, v in entries.items()
               if p.startswith(baseline + ".v")}
    if not parked:
        pytest.fail(
            f"no parked predecessor of {baseline} and none in the manifest "
            f"either: an undecided absence is not a declared withholding")
    wrong = sorted({r for _, (d, r) in parked.items()
                     if d != "EXCLUDE" or r != "parked-predecessor"})
    if wrong:
        pytest.fail(
            f"a parked predecessor of {baseline} is classified {wrong} rather "
            f"than EXCLUDE/parked-predecessor")
    pytest.skip(
        f"no parked predecessor of {baseline} in this tree -- excluded "
        f"historical predecessor; the binding registration is published, and "
        f"that the two carry different blobs is checkable from git in the "
        f"repository that produced them")


@pytest.fixture
def benchmark_tasks() -> Path:
    needs_evidence(HIDDEN_FIXTURES)
    return ROOT / HIDDEN_FIXTURES.path


# --------------------------------------------------------------------------- #
# The test boundary: a guard regression must fail a test, never run a command
# --------------------------------------------------------------------------- #

#: Payloads the test suite uses to exercise the guard. They are here so that,
#: if the guard ever stops refusing one of them, the suite fails instead of
#: executing it. 2026-09-25: an operator-side relaxation silently weakened the
#: product guard, and guard tests really launched their payload (among them an
#: endless GPU query loop, left orphaned on the machine for over an hour).
#: `hoh-never-execute-sentinel` is the neutral payload for tests in which this
#: stub is meant to be the only barrier.
NEVER_EXECUTE_IN_TESTS = ("nvidia-smi", "hoh-never-execute-sentinel")


def _executes(script: str, token: str) -> bool:
    """Fail closed: `token` as a word anywhere in an executed script counts as
    executing it -- `eval T`, `bash -c "T"`, `sudo T`, `xargs T`, `! T` and
    every other form alike (review round 3: a command-position matcher missed
    eleven of them). The one exception is a single command, with no shell
    separator or substitution, whose first word only prints or searches text
    (`grep T file`, `echo '... T ...'`): a mention, not an execution (review
    round 2: plain substring matching failed those falsely)."""
    import re

    if not re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", script):
        return False
    words = script.strip().split()
    single = re.search(r"[;&|`\n<>]|\$\(", script) is None
    mention_only = {"grep", "egrep", "fgrep", "echo", "printf"}  # not rg: `rg --pre T` runs T
    if single and words and os.path.basename(words[0]) in mention_only:
        return False
    return True


def _executed_scripts(args, executable=None) -> list[str]:
    """What a spawn would execute: argv[0] (and `executable=`), the rest of
    argv as one line, and every script handed to a shell with `-c`, each on
    its own. A string is a shell command line."""
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)] + ([os.fsdecode(executable)] if executable else [])
    argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]
    shells = {"sh", "bash", "dash", "zsh", "ksh"}
    script_at: set[int] = set()
    for i, a in enumerate(argv):
        if os.path.basename(a) not in shells:
            continue
        # The shell's own options up to the first operand: `-c` (or a cluster
        # such as `-lc`) makes the next argument the script. Options that take
        # a value (`-O extglob`, `-o pipefail`) and long options are skipped.
        j = i + 1
        while j < len(argv) and argv[j].startswith("-"):
            opt = argv[j]
            if not opt.startswith("--") and "c" in opt[1:] and j + 1 < len(argv):
                script_at.add(j + 1)
                break
            j += 2 if opt in ("-O", "+O", "-o", "+o") else 1
    # The whole argv as one line as well -- minus the `-c` scripts, which are
    # checked on their own: a payload later in a plain argv list (`["env", T]`,
    # `["timeout", "5", T]`, `bwrap ... -- T`) is executed too (review round
    # 4). Fail closed: a role prompt naming a payload, passed as argv, is
    # refused as well -- no test spawns one.
    rest = " ".join(a for k, a in enumerate(argv) if k not in script_at)
    return (argv[:1] + [rest] + [argv[k] for k in sorted(script_at)]
            + ([os.fsdecode(executable)] if executable else []))


def _refuse_if_listed(args, attempts: list[str], executable=None) -> None:
    scripts = _executed_scripts(args, executable)
    hit = [p for p in NEVER_EXECUTE_IN_TESTS
           if any(_executes(s, p) for s in scripts)]
    if hit:
        text = " | ".join(scripts)
        attempts.append(text)
        # pytest.fail raises a BaseException, so `except OSError` /
        # `except Exception` in the code under test cannot turn it into a
        # receipt: the test fails at this line and nothing is started.
        pytest.fail(
            f"guard regression: code under test tried to EXECUTE a command the "
            f"policy must refuse ({hit[0]!r}); refused, not started: {text!r}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _no_refused_command_is_ever_executed(monkeypatch):
    """A stub in front of the process-spawn APIs of the pytest process.

    Covered: `subprocess.Popen` itself (replaced by a subclass, so isinstance
    and subclassing keep working), hence `subprocess.run`, `check_output` and
    friends however they are imported, including `executable=`; `os.system`;
    `os.posix_spawn[p]`; `os.spawn*`; `os.exec*`. **Not** covered: `os.fork`
    followed by exec, including `pty.spawn` -- the refusal would be raised in
    the forked child, which would then run the rest of the session, while the
    parent sees no failure (review round 3); nothing in src/, tests/ or tools/
    forks today -- and child Python processes, which do not inherit this
    fixture (both tracked in the dogfood ledger, O235). This is a safety net
    for the suite's own guard payloads, not a classifier: see `_executes`.
    """
    import subprocess as _subprocess

    attempts: list[str] = []
    real_popen = _subprocess.Popen

    class _GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            _refuse_if_listed(args, attempts, kw.get("executable"))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(_subprocess, "Popen", _GuardedPopen)
    real_system = os.system
    monkeypatch.setattr(os, "system",
                        lambda cmd: (_refuse_if_listed(cmd, attempts), real_system(cmd))[1])
    def _wrap_path_argv(real):          # f(path, argv, ...) -- posix_spawn*, execv*
        def wrapped(path, argv, *a, **kw):
            _refuse_if_listed([*argv], attempts, path)
            return real(path, argv, *a, **kw)
        return wrapped

    def _wrap_path_varargs(real):       # f(path, arg0, arg1, ...) -- execl*
        def wrapped(path, *args):
            _refuse_if_listed([a for a in args if isinstance(a, (str, bytes, os.PathLike))],
                              attempts, path)
            return real(path, *args)
        return wrapped

    def _wrap_spawn(real, varargs):     # f(mode, path, ...) -- spawnv*/spawnl*
        def wrapped(mode, path, *rest):
            argv = rest if varargs else rest[0]
            _refuse_if_listed([a for a in argv if isinstance(a, (str, bytes, os.PathLike))],
                              attempts, path)
            return real(mode, path, *rest)
        return wrapped

    for name in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_path_argv(getattr(os, name)))
    for name in ("execl", "execle", "execlp", "execlpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_path_varargs(getattr(os, name)))
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_spawn(getattr(os, name), False))
    for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_spawn(getattr(os, name), True))
    try:                                   # modules that did `from subprocess import Popen`
        from hoh import runner as _runner
        if getattr(_runner, "Popen", None) is real_popen:
            monkeypatch.setattr(_runner, "Popen", _GuardedPopen)
    except Exception:
        pass
    yield attempts
    assert not attempts, f"refused commands reached the executor: {attempts}"
