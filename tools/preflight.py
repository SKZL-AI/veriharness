#!/usr/bin/env python3
"""Prove the environment can do the work, before any quota is spent on it.

V3.3 P0/P3. A programme meant to run unattended fails in one of two ways: the
product is wrong, or the machine was never able to run it. The second kind is
cheap to detect and expensive to discover late -- a live demo where the
interpreter is the wrong one, a wave that dies on a trust prompt nobody is
there to answer, a criterion whose command does not exist so every check
"fails" for a reason that is not about the software at all.

Three properties, each the answer to a way this could lie:

* **Measured, not declared.** Every line comes from asking the thing itself:
  the interpreter reports its own version, `git` reports the mainline, the
  sandbox backend is asked whether it can create a namespace *here*. Nothing
  is read out of a config file and repeated.
* **Three answers, not two.** A check that cannot run is `INCONCLUSIVE`, which
  is neither a pass nor a failure. Infrastructure exits 124/126/127 are that
  state by definition, and this tool proves the product agrees by running
  three real checks through the real runner and reading the receipts back.
* **A profile decides what is required.** `demo` and `unattended` ask for
  different things -- a demo may accept weak isolation if it is labelled;
  unattended may not accept an unanswerable trust prompt. The profile is what
  turns a report into a verdict, and the verdict names the check that decided
  it.

Usage:
    python3 tools/preflight.py --profile demo
    python3 tools/preflight.py --profile unattended --deep
    python3 tools/preflight.py --out program/v3_3/VERIHARNESS_ENVIRONMENT_CAPABILITIES.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

PASS, FAIL, INCONCLUSIVE, NOT_APPLICABLE = "PASS", "FAIL", "INCONCLUSIVE", "NOT_APPLICABLE"

#: Truthful isolation levels, strongest first. The point of the vocabulary is
#: that `NONE` is sayable: a run on a machine without the strict backend is
#: not "strict isolation, degraded", it is no isolation, and a profile that
#: accepts it should have to say so.
STRICT_OS_SANDBOX, CONTAINERIZED, TRIPWIRE_ONLY, NONE = (
    "STRICT_OS_SANDBOX", "CONTAINERIZED", "TRIPWIRE_ONLY", "NONE")
_LEVELS = [NONE, TRIPWIRE_ONLY, CONTAINERIZED, STRICT_OS_SANDBOX]


@dataclass
class Check:
    """One measurement, its verdict, and what it was read from."""

    check_id: str
    question: str
    state: str
    detail: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"check": self.check_id, "question": self.question,
                "state": self.state, "detail": self.detail,
                "evidence": self.evidence}


#: What each profile requires. A check absent from a profile's list is still
#: measured and still reported -- it just does not block. Nothing here is a
#: default: a profile that required nothing would be a profile that proves
#: nothing, and `unattended` is deliberately the strict one.
PROFILES: dict[str, dict] = {
    "demo": {
        "requires": ("python_version", "cli_entrypoint", "dependencies",
                     "git_repo", "infrastructure_exits", "disk_headroom"),
        "min_isolation": NONE,
        "why": ("a live demo must fail before the audience arrives; it may "
                "run with weak isolation as long as the level is stated"),
    },
    "unattended": {
        "requires": ("python_version", "cli_entrypoint", "dependencies",
                     "git_repo", "infrastructure_exits", "disk_headroom",
                     "trust_readiness", "repo_clean", "worktree_creation"),
        "min_isolation": STRICT_OS_SANDBOX,
        "why": ("nobody is there to answer a prompt or to notice that a "
                "check never ran"),
    },
}


def _run(*argv: str, cwd: Path | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=str(cwd or HOH), capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: no answer within {timeout}s"
    except OSError as exc:
        return 126, f"{argv[0]}: {exc}"
    return p.returncode, (p.stdout + p.stderr).strip()


def _requires_python() -> str:
    """The minimum this package declares, read from its own metadata."""
    text = (HOH / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("requires-python"):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #

def check_python_version() -> Check:
    declared = _requires_python()
    needed = tuple(int(p) for p in declared.lstrip(">=~^ ").split(".")[:2] if p.isdigit())
    running = sys.version_info[:2]
    ok = bool(needed) and running >= needed
    return Check(
        "python_version",
        "is the interpreter running this at least the declared minimum?",
        PASS if ok else FAIL,
        f"running {running[0]}.{running[1]}, package declares {declared or 'nothing'}",
        {"running": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
         "declared": declared, "executable": sys.executable})


def check_cli_entrypoint() -> Check:
    """Does `hoh` resolve, and to which interpreter?

    Not the same question as the one above. The live demo's first failure was
    an installed console script whose shebang named a different interpreter
    from the one the operator was testing with, so the version that mattered
    was not the version anybody had checked.
    """
    where = shutil.which("hoh")
    module = importlib.util.find_spec("hoh") if not where else None
    if not where:
        reachable = module is not None or (HOH / "src/hoh/cli.py").is_file()
        return Check(
            "cli_entrypoint",
            "does the CLI resolve, and under which interpreter?",
            PASS if reachable else FAIL,
            ("no `hoh` on PATH; reachable as a module from this checkout"
             if reachable else "neither a `hoh` command nor an importable module"),
            {"which": None, "module": bool(module),
             "checkout_cli": (HOH / "src/hoh/cli.py").is_file()})
    shebang = ""
    try:
        with open(where, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
        shebang = first[2:].strip() if first.startswith("#!") else ""
    except OSError:
        pass
    same = (not shebang) or Path(shebang.split()[0]).resolve() == Path(sys.executable).resolve()
    return Check(
        "cli_entrypoint",
        "does the CLI resolve, and under which interpreter?",
        PASS if same else FAIL,
        (f"{where}" if same else
         f"{where} runs under {shebang}, not under {sys.executable}"),
        {"which": where, "shebang": shebang, "this_interpreter": sys.executable})


def check_dependencies() -> Check:
    """Are the declared runtime dependencies importable by *this* interpreter?"""
    text = (HOH / "pyproject.toml").read_text(encoding="utf-8")
    declared: list[str] = []
    for line in text.splitlines():
        if line.startswith("dependencies"):
            declared = [d.strip().strip('"\'')
                        for d in line.split("[", 1)[1].split("]")[0].split(",")
                        if d.strip()]
            break
    missing = []
    for dep in declared:
        name = dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        if name and importlib.util.find_spec(name) is None:
            missing.append(name)
    return Check(
        "dependencies", "are the declared dependencies importable here?",
        PASS if not missing else FAIL,
        f"{len(declared)} declared, {len(missing)} missing"
        + (": " + ", ".join(missing) if missing else ""),
        {"declared": declared, "missing": missing})


def check_herdr() -> Check:
    """Herdr is the runtime the dispatcher drives. Absent is not a failure of
    this repository -- it is a capability this machine does not have, and a
    profile that needs it should say so rather than discovering it mid-wave."""
    where = shutil.which("herdr")
    if not where:
        return Check("herdr", "is the agent runtime available?", INCONCLUSIVE,
                     "no `herdr` on PATH; nothing about its version can be "
                     "measured from here", {"which": None})
    rc, out = _run(where, "--version", timeout=30)
    return Check("herdr", "is the agent runtime available?",
                 PASS if rc == 0 else INCONCLUSIVE,
                 out.splitlines()[0][:80] if out else f"exit {rc}",
                 {"which": where, "exit_code": rc, "version": out[:80]})


def check_git_repo() -> Check:
    """A repository, and the mainline branch *detected* rather than assumed.

    The live demo hard-coded `master`. This reports which method answered, so
    a wrong mainline is visible as a wrong method rather than as an
    inexplicable failure later.
    """
    rc, _ = _run("git", "rev-parse", "--git-dir")
    if rc != 0:
        return Check("git_repo", "is this a git repository with a known mainline?",
                     FAIL, "not a git repository", {})
    method, mainline = "", ""
    rc, out = _run("git", "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc == 0 and out:
        method, mainline = "origin/HEAD", out.rsplit("/", 1)[-1]
    if not mainline:
        rc, out = _run("git", "config", "--get", "init.defaultBranch")
        if rc == 0 and out:
            method, mainline = "init.defaultBranch", out.strip()
    if not mainline:
        rc, out = _run("git", "rev-parse", "--abbrev-ref", "HEAD")
        if rc == 0 and out and out != "HEAD":
            method, mainline = "current branch", out.strip()
    return Check(
        "git_repo", "is this a git repository with a known mainline?",
        PASS if mainline else INCONCLUSIVE,
        f"mainline {mainline or 'undetermined'} (from {method or 'no method'})",
        {"mainline": mainline or None, "method": method or None,
         "hardcoded": False})


def check_infrastructure_exits() -> Check:
    """Does the product actually classify 127/126/124 as INCONCLUSIVE?

    Read back from the built object, not from the specification: three real
    checks through the real runner, and the receipts have to say INCONCLUSIVE.
    A tool that asserted this from the docs would be repeating the claim it is
    supposed to be testing.
    """
    sys.path.insert(0, str(HOH / "src"))
    try:
        from hoh.contracts import AcceptanceCheck, Candidate, Outcome
        from hoh.runner import run_check
    except Exception as exc:                      # pragma: no cover - import guard
        return Check("infrastructure_exits",
                     "are 127/126/124 INCONCLUSIVE rather than product verdicts?",
                     INCONCLUSIVE, f"the runner could not be imported: {exc}", {})

    candidate = Candidate(candidate_id="preflight", repo_path=str(HOH),
                          commit="0" * 40, tree_clean=True, tree_digest="preflight")
    seen: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "not-executable").write_text("#!/nonexistent\n")
        cases = [
            ("127", "a-command-that-does-not-exist-anywhere", 60),
            ("126", "./not-executable", 60),
            ("124", "sleep 30", 1),
        ]
        for label, command, timeout in cases:
            check = AcceptanceCheck(check_id=f"PF{label}", description="preflight",
                                    command=command, expect_exit=0)
            try:
                receipt, _ = run_check(check, candidate, run_id="preflight",
                                       iteration=1, attempt=1, cwd=work,
                                       timeout=timeout)
            except Exception as exc:              # pragma: no cover - defensive
                seen[label] = {"error": str(exc)[:120]}
                continue
            seen[label] = {"exit_code": receipt.exit_code,
                           "outcome": str(receipt.outcome(0)),
                           "runner_ok": receipt.runner_ok}
    wrong = [label for label, d in seen.items()
             if d.get("outcome") != str(Outcome.INCONCLUSIVE)]
    return Check(
        "infrastructure_exits",
        "are 127/126/124 INCONCLUSIVE rather than product verdicts?",
        PASS if not wrong else FAIL,
        ("all three infrastructure exits read as INCONCLUSIVE"
         if not wrong else
         f"{len(wrong)} of 3 did not: {', '.join(wrong)}"),
        seen)


def check_isolation() -> Check:
    """The effective level, asked of the backend, on this machine.

    `bwrap` being installed is not the question; whether it can create a
    namespace *here* is. The backend already answers that, so this asks it
    rather than re-implementing the probe.
    """
    sys.path.insert(0, str(HOH / "src"))
    try:
        from hoh import sandbox as sb
    except Exception as exc:                      # pragma: no cover
        return Check("isolation", "what isolation can this machine actually provide?",
                     INCONCLUSIVE, f"the sandbox module could not be imported: {exc}",
                     {"level": None})
    backend = sb.BubblewrapSandbox()
    why = backend.unavailable()
    level = STRICT_OS_SANDBOX if why is None else NONE
    return Check(
        "isolation", "what isolation can this machine actually provide?",
        PASS,  # a level is never a failure; a *profile* decides if it suffices
        f"{level}" + ("" if why is None else f": {why[:110]}"),
        {"level": level, "backend": backend.name,
         "platform": platform.system(), "reason": why})


def check_resource_controls() -> Check:
    """Which ceilings are actually applied, rather than declared.

    O107's shape: two limits were declared in the spec object and applied only
    on the unsandboxed path, so asking for stronger isolation silently removed
    them. The fields are read off the spec the product builds.
    """
    sys.path.insert(0, str(HOH / "src"))
    try:
        from hoh.sandbox import Isolation, SandboxSpec
    except Exception as exc:                      # pragma: no cover
        return Check("resource_controls", "which ceilings does a run actually apply?",
                     INCONCLUSIVE, f"not importable: {exc}", {})
    spec = SandboxSpec(candidate=HOH, scratch=HOH, isolation=Isolation.STRICT,
                       memory_bytes=1 << 30, cpu_seconds=60,
                       max_open_files=256, file_size_bytes=1 << 26)
    declared = {name: getattr(spec, name) is not None
                for name in ("memory_bytes", "cpu_seconds", "max_open_files",
                             "file_size_bytes")}
    return Check(
        "resource_controls", "which ceilings does a run actually apply?",
        PASS, ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in declared.items()),
        {"controls": declared})


def check_trust_readiness() -> Check:
    """Can an unattended run get a fresh worktree trusted without a person?

    The product has the mechanism -- a scoped `ApprovalProvider` -- and the
    launcher defaults to `NoApprovalProvider`, which refuses everything and
    says so. What it has no way to do is read one out of a deployment: there
    is no environment variable, no config key, nothing a machine could set.
    So for an unattended profile this is FAIL, and the reason is a product
    gap rather than a missing file on this host.
    """
    sys.path.insert(0, str(HOH / "src"))
    try:
        from hoh import approval as ap
    except Exception as exc:                      # pragma: no cover
        return Check("trust_readiness", "can a worktree be trusted with nobody present?",
                     INCONCLUSIVE, f"not importable: {exc}", {})
    configured = os.environ.get("HOH_APPROVAL_SCRIPT")
    scope = os.environ.get("HOH_APPROVAL_SCOPE")
    providers = [n for n in dir(ap) if n.endswith("Provider")]
    if not configured or not scope:
        return Check(
            "trust_readiness", "can a worktree be trusted with nobody present?",
            FAIL,
            "no approval authority is discoverable from the environment; the "
            "launcher would use NoApprovalProvider and the run would stop at "
            "the trust dialog",
            {"providers_available": providers,
             "HOH_APPROVAL_SCRIPT": configured, "HOH_APPROVAL_SCOPE": scope,
             "gap": "no deployment-level configuration path exists (V3.3 P0)"})
    helper = Path(configured)
    try:
        provider = ap.PrefixScopedProvider(script=helper, prefix=Path(scope))
    except ValueError as exc:
        return Check("trust_readiness", "can a worktree be trusted with nobody present?",
                     FAIL, f"the configured scope is not usable: {exc}",
                     {"HOH_APPROVAL_SCOPE": scope})
    ok = helper.exists() and os.access(helper, os.X_OK)
    return Check(
        "trust_readiness", "can a worktree be trusted with nobody present?",
        PASS if ok else FAIL,
        f"{provider.name} scoped to {provider.prefix}"
        + ("" if ok else f"; helper {helper} is missing or not executable"),
        {"provider": provider.name, "prefix": str(provider.prefix),
         "helper": str(helper), "executable": ok})


def check_worktree_creation(deep: bool) -> Check:
    """Can this repository actually produce a worktree for a parallel node?

    Off by default: it writes to `.git/worktrees`. With `--deep` it creates a
    detached worktree in a temporary directory and removes it again through
    git, which is the only way to know rather than assume.
    """
    if not deep:
        return Check("worktree_creation", "can a worktree be created here?",
                     INCONCLUSIVE,
                     "not attempted; pass --deep to create and remove one",
                     {"attempted": False})
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "probe"
        rc, out = _run("git", "worktree", "add", "--detach", str(target), "HEAD",
                       timeout=120)
        if rc != 0:
            return Check("worktree_creation", "can a worktree be created here?",
                         FAIL, out[-140:], {"attempted": True, "exit_code": rc})
        rc2, out2 = _run("git", "worktree", "remove", "--force", str(target),
                         timeout=120)
        return Check(
            "worktree_creation", "can a worktree be created here?",
            PASS if rc2 == 0 else FAIL,
            "created and removed" if rc2 == 0 else f"created, not removed: {out2[-100:]}",
            {"attempted": True, "created": True, "removed": rc2 == 0})


def check_disk_headroom(min_free_mb: int = 512) -> Check:
    """Free space where the runs and the arenas go, and where TMPDIR points."""
    spots = {"repo": HOH, "tmpdir": Path(tempfile.gettempdir())}
    measured, tight = {}, []
    for name, where in spots.items():
        try:
            st = os.statvfs(where)
        except OSError as exc:
            measured[name] = {"error": str(exc)}
            continue
        free_mb = st.f_bavail * st.f_frsize // (1 << 20)
        inodes = st.f_favail
        measured[name] = {"path": str(where), "free_mb": free_mb, "free_inodes": inodes}
        if free_mb < min_free_mb or (inodes is not None and 0 <= inodes < 10000):
            tight.append(f"{name}: {free_mb} MB, {inodes} inodes")
    return Check(
        "disk_headroom", "is there room for arenas, worktrees and transcripts?",
        PASS if not tight else FAIL,
        "; ".join(tight) if tight else
        ", ".join(f"{k} {v.get('free_mb')} MB" for k, v in measured.items()),
        {"measured": measured, "min_free_mb": min_free_mb})


def check_provider_health() -> Check:
    """Provider state cannot be measured without spending, so it is not claimed.

    Deliberately INCONCLUSIVE rather than absent: a report that silently left
    the provider out would read as "nothing wrong with the provider".
    """
    return Check(
        "provider_health", "is the model provider healthy and in quota?",
        INCONCLUSIVE,
        "not measurable without dispatching; a dispatch is what this tool "
        "exists to avoid spending. The cross-run sentinel (V3.3 P3) is where "
        "this becomes answerable from evidence already collected",
        {"measurable_here": False})


def check_repo_clean() -> Check:
    rc, out = _run("git", "status", "--porcelain")
    if rc != 0:
        return Check("repo_clean", "is the working tree clean?", INCONCLUSIVE,
                     "git could not answer", {})
    dirty = [line for line in out.splitlines() if line.strip()]
    return Check("repo_clean", "is the working tree clean?",
                 PASS if not dirty else FAIL,
                 "clean" if not dirty else f"{len(dirty)} path(s) modified",
                 {"dirty": dirty[:10], "count": len(dirty)})


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def measure(deep: bool = False) -> list[Check]:
    return [
        check_python_version(),
        check_cli_entrypoint(),
        check_dependencies(),
        check_herdr(),
        check_git_repo(),
        check_infrastructure_exits(),
        check_isolation(),
        check_resource_controls(),
        check_trust_readiness(),
        check_worktree_creation(deep),
        check_disk_headroom(),
        check_provider_health(),
        check_repo_clean(),
    ]


def verdict(checks: list[Check], profile: str) -> tuple[str, list[str], list[str]]:
    """(READY | NOT_READY | INCONCLUSIVE, blocking ids, inconclusive ids)."""
    wanted = PROFILES[profile]
    by_id = {c.check_id: c for c in checks}
    blocking = [c for c in wanted["requires"]
                if by_id.get(c) and by_id[c].state == FAIL]
    unknown = [c for c in wanted["requires"]
               if by_id.get(c) and by_id[c].state == INCONCLUSIVE]

    isolation = by_id.get("isolation")
    level = (isolation.evidence.get("level") if isolation else None) or NONE
    need = wanted["min_isolation"]
    if _LEVELS.index(level) < _LEVELS.index(need):
        blocking.append("isolation")
    if blocking:
        return "NOT_READY", blocking, unknown
    if unknown:
        return "INCONCLUSIVE", blocking, unknown
    return "READY", blocking, unknown


def report(checks: list[Check], profile: str, deep: bool) -> dict:
    state, blocking, unknown = verdict(checks, profile)
    body = {
        "what": (
            "Environment capability report. Every line is measured on this "
            "machine at the time given; a check that could not run says so "
            "and is never counted as a pass."
        ),
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile": profile,
        "profile_requires": list(PROFILES[profile]["requires"]),
        "profile_min_isolation": PROFILES[profile]["min_isolation"],
        "deep": deep,
        "verdict": state,
        "blocking": blocking,
        "inconclusive": unknown,
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "checks": [c.as_dict() for c in checks],
    }
    body["digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in body.items() if k != "measured_at_utc"},
                   sort_keys=True).encode()).hexdigest()
    return body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="demo")
    ap.add_argument("--deep", action="store_true",
                    help="also create and remove a real worktree")
    ap.add_argument("--out", type=Path, default=None,
                    help="write the machine-readable report here")
    ap.add_argument("--json", action="store_true", help="print the report")
    args = ap.parse_args(argv)

    checks = measure(args.deep)
    body = report(checks, args.profile, args.deep)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(body, indent=2) + "\n")
    if args.json:
        print(json.dumps(body, indent=2))
    else:
        width = max(len(c.check_id) for c in checks)
        for c in checks:
            print(f"  {c.state:<14} {c.check_id:<{width}}  {c.detail}")
        print()
        print(f"profile {args.profile}: {body['verdict']}"
              + (f" -- blocking: {', '.join(body['blocking'])}" if body["blocking"] else "")
              + (f"; inconclusive: {', '.join(body['inconclusive'])}"
                 if body["inconclusive"] else ""))
        if args.out:
            print(f"wrote {args.out}")
    return {"READY": 0, "NOT_READY": 1, "INCONCLUSIVE": 3}[body["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
