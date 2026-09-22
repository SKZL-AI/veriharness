"""The preflight: does it ever say ready when it is not?

Each case here is a way the report could be comfortable and wrong. The
profile logic gets the most attention, because it is the part that turns
measurements into a decision somebody acts on without reading the rest.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "preflight_probe", ROOT / "tools" / "preflight.py")
pf = importlib.util.module_from_spec(_spec)
# Registered before execution: a `@dataclass` resolves its annotations through
# `sys.modules[cls.__module__]`, and a module loaded from a path without being
# registered there fails at class-creation time with an AttributeError that
# says nothing about the cause.
sys.modules["preflight_probe"] = pf
_spec.loader.exec_module(pf)


def _check(check_id: str, state: str, **evidence) -> pf.Check:
    return pf.Check(check_id, "q", state, "d", evidence)


def _base(profile: str, **overrides) -> list[pf.Check]:
    """Every required check passing, plus an isolation level that suffices."""
    checks = [_check(c, pf.PASS) for c in pf.PROFILES[profile]["requires"]]
    checks.append(_check("isolation", pf.PASS,
                         level=pf.PROFILES[profile]["min_isolation"]))
    for check_id, state in overrides.items():
        for i, c in enumerate(checks):
            if c.check_id == check_id:
                checks[i] = _check(check_id, state, **c.evidence)
    return checks


def test_the_positive_control_a_clean_environment_is_ready():
    """Without this, every case below would also pass against a preflight that
    always refuses."""
    for profile in pf.PROFILES:
        state, blocking, unknown = pf.verdict(_base(profile), profile)
        assert state == "READY", (profile, blocking, unknown)


def test_a_required_check_that_failed_blocks_and_is_named():
    for profile in pf.PROFILES:
        required = pf.PROFILES[profile]["requires"][0]
        state, blocking, _ = pf.verdict(_base(profile, **{required: pf.FAIL}), profile)
        assert state == "NOT_READY" and required in blocking


def test_an_inconclusive_required_check_is_not_rounded_to_ready():
    """The whole vocabulary exists for this line: a check that could not run
    has measured nothing, and a profile whose requirement was not measured is
    not a profile that was met."""
    for profile in pf.PROFILES:
        required = pf.PROFILES[profile]["requires"][0]
        state, blocking, unknown = pf.verdict(
            _base(profile, **{required: pf.INCONCLUSIVE}), profile)
        assert state == "INCONCLUSIVE"
        assert required in unknown and not blocking


def test_isolation_below_the_profile_minimum_blocks():
    """A demo may run with none of it as long as the level is stated; an
    unattended profile may not, and the difference is the whole point of
    reporting a level instead of a boolean."""
    checks = [c for c in _base("unattended") if c.check_id != "isolation"]
    checks.append(_check("isolation", pf.PASS, level=pf.TRIPWIRE_ONLY))
    state, blocking, _ = pf.verdict(checks, "unattended")
    assert state == "NOT_READY" and "isolation" in blocking

    demo = [c for c in _base("demo") if c.check_id != "isolation"]
    demo.append(_check("isolation", pf.PASS, level=pf.NONE))
    assert pf.verdict(demo, "demo")[0] == "READY"


def test_a_check_outside_the_profile_is_reported_and_does_not_block():
    """`herdr` and `provider_health` are measured for every profile and
    required by neither. A report that hid them would read as though they
    were fine."""
    checks = _base("demo") + [_check("herdr", pf.INCONCLUSIVE),
                              _check("provider_health", pf.INCONCLUSIVE)]
    state, blocking, unknown = pf.verdict(checks, "demo")
    assert state == "READY" and not blocking and not unknown
    body = pf.report(checks, "demo", deep=False)
    assert {"herdr", "provider_health"} <= {c["check"] for c in body["checks"]}


def test_unattended_requires_more_than_demo():
    """Not a tautology: it is the one property that makes two profiles worth
    having, and it would be quietly lost by an edit to either list."""
    demo = set(pf.PROFILES["demo"]["requires"])
    unattended = set(pf.PROFILES["unattended"]["requires"])
    assert demo < unattended
    assert {"trust_readiness", "worktree_creation"} <= unattended - demo


def test_the_mainline_is_detected_and_never_assumed():
    """The live demo hard-coded `master`. The check reports the method that
    answered, so a wrong mainline is visible as a wrong method."""
    c = pf.check_git_repo()
    assert c.evidence.get("hardcoded") is False
    assert c.evidence.get("method"), "no method named"
    source = (ROOT / "tools" / "preflight.py").read_text()
    assert '"master"' not in source and "'master'" not in source


def test_the_infrastructure_exits_are_read_back_from_the_runner():
    """127, 126 and 124 through the real runner, and the receipts have to say
    INCONCLUSIVE. Asserting this from the specification would repeat the claim
    under test."""
    c = pf.check_infrastructure_exits()
    assert c.state == pf.PASS, c.detail
    assert set(c.evidence) == {"127", "126", "124"}
    for label, seen in c.evidence.items():
        assert seen["exit_code"] == int(label), (label, seen)
        assert seen["outcome"] == "INCONCLUSIVE", (label, seen)


def test_a_worktree_is_not_claimed_unless_it_was_created():
    c = pf.check_worktree_creation(deep=False)
    assert c.state == pf.INCONCLUSIVE and c.evidence["attempted"] is False


def test_trust_readiness_fails_when_nothing_is_configured(monkeypatch):
    """Today this is the answer on every machine, and the reason is a product
    gap rather than a missing file: there is no configuration path at all."""
    monkeypatch.delenv("HOH_APPROVAL_SCRIPT", raising=False)
    monkeypatch.delenv("HOH_APPROVAL_SCOPE", raising=False)
    c = pf.check_trust_readiness()
    assert c.state == pf.FAIL
    assert "NoApprovalProvider" in c.detail
    assert c.evidence["gap"]


def test_trust_readiness_passes_only_with_an_executable_helper_and_a_real_scope(
        monkeypatch, tmp_path):
    scope = tmp_path / "worktrees" / "project"
    scope.mkdir(parents=True)
    helper = tmp_path / "approve.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HOH_APPROVAL_SCOPE", str(scope))
    monkeypatch.setenv("HOH_APPROVAL_SCRIPT", str(helper))

    # Not executable yet: a helper that cannot run cannot approve.
    assert pf.check_trust_readiness().state == pf.FAIL
    helper.chmod(0o755)
    assert pf.check_trust_readiness().state == pf.PASS

    # And a scope wide enough to be decorative is refused, not accepted.
    monkeypatch.setenv("HOH_APPROVAL_SCOPE", str(Path.home()))
    c = pf.check_trust_readiness()
    assert c.state == pf.FAIL and "scope" in c.detail


def test_the_digest_ignores_the_clock_and_not_the_content():
    """Two reports of the same machine must be comparable; a timestamp in the
    digest would make every report unique and the field useless."""
    a = pf.report(_base("demo"), "demo", deep=False)
    b = pf.report(_base("demo"), "demo", deep=False)
    assert a["digest"] == b["digest"]
    c = pf.report(_base("demo", **{pf.PROFILES["demo"]["requires"][0]: pf.FAIL}),
                  "demo", deep=False)
    assert c["digest"] != a["digest"]


def test_the_cli_exit_code_carries_the_verdict(tmp_path):
    """0 ready, 1 not ready, 3 inconclusive -- as a real invocation, because a
    caller in a script reads the code and nothing else."""
    out = tmp_path / "report.json"
    p = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "preflight.py"),
         "--profile", "unattended", "--out", str(out)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, "HOH_APPROVAL_SCRIPT": "", "HOH_APPROVAL_SCOPE": ""})
    assert p.returncode in (1, 3), p.stdout
    body = json.loads(out.read_text())
    assert body["verdict"] in ("NOT_READY", "INCONCLUSIVE")
    assert body["profile"] == "unattended"
    assert body["digest"]
