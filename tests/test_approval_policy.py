"""P1-09, the role approval policy: can it ever be weaker than it looks?

O198: roles inherited the operator's interactive defaults, so the first
fresh-clone run stopped for a human ten seconds in. The policy decides up
front what a role may do. Every case here tries to get a weaker policy past
the loader, or to get a policy applied where HoH cannot express it. No agent
is started: the house rules forbid real agent runs in a test suite, and none
is needed to check what a role would be started with.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hoh.approval_policy import (MANDATORY_DENY, REFUSED_MODES, PolicyRefused,
                                 load, parse)
from hoh.contracts import Role
from hoh.controller import DispatchError
from hoh.dispatchers import _Base
from hoh.trust import TrustRefused, trust_run_worktree

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "policy" / "role_approval.default.json"


def _doc() -> dict:
    return json.loads(DEFAULT.read_text(encoding="utf-8"))


def test_the_shipped_default_loads_and_waits_for_nobody():
    policy = load(DEFAULT)
    assert policy.mode == "auto"
    assert set(MANDATORY_DENY) <= set(policy.deny)
    assert "never delete" in policy.house_rules.lower()


@pytest.mark.parametrize("mode", sorted(REFUSED_MODES))
def test_a_mode_that_would_stop_for_a_human_is_refused(mode):
    doc = _doc()
    doc["mode"] = mode
    with pytest.raises(PolicyRefused, match=mode):
        parse(doc)


def test_yolo_is_allowed_only_by_name_and_keeps_the_deny_list():
    """The captain may choose bypassPermissions. The deny-list and the house
    rules do not go away with it."""
    doc = _doc()
    doc["mode"] = "bypassPermissions"
    policy = parse(doc)
    assert policy.mode == "bypassPermissions"
    assert set(MANDATORY_DENY) <= set(policy.deny)


def test_dropping_any_mandatory_deny_is_refused():
    for victim in ("Bash(rm:*)", "Bash(git push:*)", "Bash(nvidia-smi:*)", "WebFetch"):
        doc = _doc()
        doc["deny"] = [d for d in doc["deny"] if d != victim]
        with pytest.raises(PolicyRefused, match="mandatory"):
            parse(doc)


@pytest.mark.parametrize("rule", ["Bash", "Bash(*)", "Bash(:*)", "Write", "Edit(*)",
                                  "NotebookEdit"])
def test_an_unscoped_allow_rule_is_refused(rule):
    doc = _doc()
    doc["roles"]["developer"]["allow"] = [rule]
    with pytest.raises(PolicyRefused, match="unscoped"):
        parse(doc)


def test_network_and_contradictions_are_refused():
    doc = _doc()
    doc["roles"]["planner"]["allow"] = ["WebSearch"]
    with pytest.raises(PolicyRefused):
        parse(doc)
    doc = _doc()
    doc["roles"]["qa"]["allow"] = ["Bash(rm:*)"]
    with pytest.raises(PolicyRefused, match="both allowed and denied"):
        parse(doc)


def test_the_house_rules_cannot_be_emptied_into_a_formality():
    doc = _doc()
    doc["house_rules"] = "Be careful."
    with pytest.raises(PolicyRefused, match="never delete"):
        parse(doc)


def test_the_role_files_carry_the_rules_and_the_command_line_carries_only_paths(tmp_path):
    """Herdr types the agent command into a shell. A deny rule the shell ate
    would be a deny rule that silently does not exist, so the rules live in a
    file and the arguments are plain tokens."""
    policy = load(DEFAULT)
    settings, prompt = policy.write_role_files(tmp_path, "developer")
    written = json.loads(settings.read_text())
    assert written["permissions"]["defaultMode"] == "auto"
    assert set(MANDATORY_DENY) <= set(written["permissions"]["deny"])
    assert "never delete" in prompt.read_text().lower()
    args = policy.args("developer", settings, prompt)
    assert args[:2] == ["--permission-mode", "auto"]
    for token in args:
        assert not any(c in token for c in "()*;|&$`\"'"), token


def _base(tmp_path, kind="claude"):
    return _Base(answers_dir=tmp_path / "run" / "answers",
                 profiles={r: kind for r in Role})


def test_without_a_policy_nothing_changes(tmp_path):
    """The historical behaviour stays the default: no policy, no flags."""
    assert _base(tmp_path)._agent_args(Role.DEVELOPER) == []


def test_with_a_policy_every_role_starts_under_it(tmp_path):
    d = _base(tmp_path)
    d.approval_policy = load(DEFAULT)
    for role in Role:
        args = d._agent_args(role)
        assert "--permission-mode" in args and "--settings" in args, role
        assert (tmp_path / "run" / "approval" / f"{role.value}.settings.json").is_file()


def test_a_policy_is_not_quietly_dropped_for_a_harness_that_cannot_take_it(tmp_path):
    d = _base(tmp_path, kind="kimi")
    d.approval_policy = load(DEFAULT)
    with pytest.raises(DispatchError, match="Claude roles only"):
        d._agent_args(Role.QA)


# --------------------------------------------------------------------------- #
# Pre-granted worktree trust
# --------------------------------------------------------------------------- #

def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def herdr_layout(tmp_path, monkeypatch):
    """A project repo, Herdr's worktree root with one linked worktree in it,
    and a Claude config that is a temporary file, never the operator's."""
    config = tmp_path / "config"
    config.mkdir()
    (config / ".claude.json").write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo = tmp_path / "project"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("x\n")
    _git("add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i", cwd=repo)
    root = tmp_path / "worktrees"
    wt = root / "project" / "hoh-branch"
    wt.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "hoh-branch", str(wt), cwd=repo)
    return {"root": root, "worktree": wt, "repo": repo, "config": config}


def test_a_linked_worktree_under_the_herdr_root_is_trusted_and_the_old_config_is_kept(
        herdr_layout):
    granted = trust_run_worktree(herdr_layout["worktree"], herdr_root=herdr_layout["root"])
    assert granted["registered"] is True
    data = json.loads((herdr_layout["config"] / ".claude.json").read_text())
    entry = data["projects"][str(herdr_layout["worktree"].resolve())]
    assert entry["hasTrustDialogAccepted"] is True
    # Nothing is overwritten without a copy: the previous config is parked.
    assert list(herdr_layout["config"].glob(".claude.json.hoh-*.bak"))


def test_a_main_checkout_is_refused_even_inside_the_root(herdr_layout, tmp_path):
    checkout = herdr_layout["root"] / "project" / "plain"
    checkout.mkdir()
    _git("init", "-q", cwd=checkout)
    with pytest.raises(TrustRefused, match="not a linked worktree"):
        trust_run_worktree(checkout, herdr_root=herdr_layout["root"])


def test_anything_outside_the_root_or_the_root_itself_is_refused(herdr_layout):
    with pytest.raises(TrustRefused, match="not below"):
        trust_run_worktree(herdr_layout["repo"], herdr_root=herdr_layout["root"])
    with pytest.raises(TrustRefused, match="not below"):
        trust_run_worktree(herdr_layout["root"], herdr_root=herdr_layout["root"])
    with pytest.raises(TrustRefused, match="project directory"):
        trust_run_worktree(herdr_layout["root"] / "project",
                           herdr_root=herdr_layout["root"])


def test_a_refusal_writes_nothing(herdr_layout):
    before = (herdr_layout["config"] / ".claude.json").read_text()
    with pytest.raises(TrustRefused):
        trust_run_worktree(herdr_layout["repo"], herdr_root=herdr_layout["root"])
    assert (herdr_layout["config"] / ".claude.json").read_text() == before
    assert not list(herdr_layout["config"].glob(".claude.json.hoh-*.bak"))


def test_the_cli_offers_both_flags():
    from hoh.cli import build_parser
    help_text = build_parser().format_help()
    run_help = subprocess.run(
        ["python3", "-m", "hoh.cli", "run", "--help"], capture_output=True, text=True,
        cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}).stdout
    assert "--approval-policy" in run_help and "--trust-worktree" in run_help
    assert help_text
