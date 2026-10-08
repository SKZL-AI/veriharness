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
# The role's own answers directory (run 20260925t1645z-pguard2)
# --------------------------------------------------------------------------- #
#
# A planner under `auto` asked for permission to write its own answer file and
# the run blocked. The settings now carry one scoped rule for the answers
# directory. These cases check the rule as written; whether a live session then
# writes without a dialog is the operator's K5 check, not something a test here
# can show.

def _answers_expected(answers_dir) -> str:
    """The rule spelled out independently of the module: `Edit(` plus the
    resolved absolute path with one extra leading slash."""
    return "Edit(/" + str(Path(answers_dir).resolve()) + "/**)"


def _answers_read_expected(answers_dir) -> str:
    """The Read rule spelled out the same way: `Read(` plus the resolved
    absolute path with one extra leading slash."""
    return "Read(/" + str(Path(answers_dir).resolve()) + "/**)"


def _rule_root(rule: str) -> Path:
    """The directory an `Edit(//<abs>/**)` or `Read(//<abs>/**)` rule grants,
    for containment checks."""
    kind = rule.split("(", 1)[0]
    assert kind in ("Edit", "Read"), rule
    assert rule.startswith(kind + "(//") and rule.endswith("/**)"), rule
    return Path(rule[len(kind + "(/"):-len("/**)")])


def _written(settings: Path) -> dict:
    return json.loads(settings.read_text(encoding="utf-8"))["permissions"]


def _policy_with(role: str, rules: list[str]):
    doc = _doc()
    doc["roles"][role]["allow"] = rules
    return parse(doc)


def test_answers_dir_grants_exactly_one_edit_rule_in_double_slash_form(tmp_path):
    """Catches a missing rule, a `Write(` rule (not matched by file-write
    checks), a single-slash path (read relative to the settings root), a
    triple slash, or an extra rule beside the one intended."""
    answers = tmp_path / "run" / "answers"
    answers.mkdir(parents=True)
    policy = _policy_with("planner", ["Bash(pytest:*)"])
    settings, _ = policy.write_role_files(tmp_path / "run", "planner", answers_dir=answers)
    allow = _written(settings)["allow"]
    expected = "Edit(/" + str(answers.resolve()) + "/**)"
    expected_read = "Read(/" + str(answers.resolve()) + "/**)"
    assert allow == ["Bash(pytest:*)", expected, expected_read]
    added = allow[1]
    assert added.startswith("Edit(//") and not added.startswith("Edit(///")
    added_read = allow[2]
    assert added_read.startswith("Read(//") and not added_read.startswith("Read(///")
    assert sum(r.startswith("Edit(") for r in allow) == 1
    assert sum(r.startswith("Read(") for r in allow) == 1
    assert not any(r.startswith("Write(") for r in allow)
    # Negative control: the same call without answers_dir writes the policy's
    # list and nothing else, as before this change.
    settings, _ = policy.write_role_files(tmp_path / "run", "planner")
    assert _written(settings)["allow"] == ["Bash(pytest:*)"]


def test_answers_dir_relative_and_symlinked_paths_are_resolved(tmp_path, monkeypatch):
    """Catches a rule written with the path as given: a relative path would be
    read against the settings root, a symlink would name a directory other than
    the one the session actually writes into."""
    real = tmp_path / "real" / "answers"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    policy = load(DEFAULT)

    monkeypatch.chdir(tmp_path)
    settings, _ = policy.write_role_files(tmp_path / "run", "developer",
                                          answers_dir=Path("real/answers"))
    assert _written(settings)["allow"] == [_answers_expected(real),
                                           _answers_read_expected(real)]
    assert _rule_root(_written(settings)["allow"][0]).is_absolute()
    assert _rule_root(_written(settings)["allow"][1]).is_absolute()

    settings, _ = policy.write_role_files(tmp_path / "run", "developer",
                                          answers_dir=link / "answers")
    assert _written(settings)["allow"] == [_answers_expected(real),
                                           _answers_read_expected(real)]
    # Negative control: the unresolved spellings are not what was written.
    written = json.dumps(_written(settings)["allow"])
    assert str(link) not in written
    assert "Edit(/real/answers/**)" not in written
    assert "Read(/real/answers/**)" not in written


def test_answers_dir_policy_rule_not_duplicated(tmp_path):
    """Catches an unconditional append: a policy that already declares the
    exact rule would list it twice. Also catches reordering of the policy's
    own rules, which must keep coming first."""
    answers = tmp_path / "answers"
    answers.mkdir()
    rule = _answers_expected(answers)
    read = _answers_read_expected(answers)
    own = ["Bash(pytest:*)", rule, read, "Edit(src/**)"]
    policy = _policy_with("qa", own)
    settings, _ = policy.write_role_files(tmp_path, "qa", answers_dir=answers)
    allow = _written(settings)["allow"]
    assert allow.count(rule) == 1
    assert allow.count(read) == 1
    assert allow == own
    # A policy that declares only the Edit rule keeps it once and gets the
    # Read rule appended once, after its own rules.
    own = ["Bash(pytest:*)", rule, "Edit(src/**)"]
    policy = _policy_with("qa", own)
    settings, _ = policy.write_role_files(tmp_path, "qa", answers_dir=answers)
    allow = _written(settings)["allow"]
    assert allow.count(rule) == 1
    assert allow == own + [read]
    # Negative control: the same policy without the rules gets them appended
    # once, after its own rules in their original order.
    policy = _policy_with("qa", ["Bash(pytest:*)", "Edit(src/**)"])
    settings, _ = policy.write_role_files(tmp_path, "qa", answers_dir=answers)
    assert _written(settings)["allow"] == ["Bash(pytest:*)", "Edit(src/**)", rule, read]


def test_answers_scoped_no_rule_matches_unscoped_or_leaves_the_directory(tmp_path):
    """Catches a grant broader than the answers directory: an unscoped
    `Edit`/`Write`, a Bash rule, or a path that reaches the run directory, a
    sibling, or `answers/../x`."""
    from hoh.approval_policy import _UNSCOPED
    run = tmp_path / "run"
    answers = run / "answers"
    answers.mkdir(parents=True)
    policy = load(DEFAULT)
    settings, _ = policy.write_role_files(run, "developer", answers_dir=answers)
    allow = _written(settings)["allow"]
    assert allow, "the answers rule must be written"
    assert sorted(r.split("(", 1)[0] for r in allow) == ["Edit", "Read"], allow
    root = answers.resolve()
    for rule in allow:
        assert not _UNSCOPED.match(rule.strip()), rule
        assert not rule.startswith("Bash"), rule
        assert not rule.startswith("Write("), rule
        granted = _rule_root(rule)
        assert granted == root, rule
    outside = [answers / ".." / "x", run / "answers2" / "f.json", run / "f.json",
               tmp_path / "elsewhere"]
    for path in outside:
        target = Path(path).resolve()
        assert not any(target.is_relative_to(_rule_root(r)) for r in allow), target
    assert (answers / "i1-a0-developer.json").resolve().is_relative_to(root)
    # Negative control: the checks above do fire on the grants they exclude.
    assert _UNSCOPED.match("Edit") and _UNSCOPED.match("Write(*)")
    assert (answers / ".." / "x").resolve().is_relative_to(_rule_root("Edit(/" + str(run.resolve()) + "/**)"))
    assert (answers / ".." / "x").resolve().is_relative_to(_rule_root("Read(/" + str(run.resolve()) + "/**)"))
    for unscoped in ("Read", "Read(**)", "Read(/**)"):
        with pytest.raises(AssertionError):
            _rule_root(unscoped)


def test_deny_untouched_by_answers_dir(tmp_path):
    """Catches any change to the deny list when the answers rule is added: a
    dropped, reordered or rewritten deny rule would let the scoped allow reach
    something the house rules forbid."""
    answers = tmp_path / "answers"
    answers.mkdir()
    policy = load(DEFAULT)
    expected = json.dumps(list(policy.deny))
    with_dir, _ = policy.write_role_files(tmp_path / "a", "developer", answers_dir=answers)
    without, _ = policy.write_role_files(tmp_path / "b", "developer")
    assert json.dumps(_written(with_dir)["deny"]) == expected
    assert json.dumps(_written(without)["deny"]) == expected
    # Negative control: only the allow list differs between the two files.
    assert _written(with_dir)["allow"] != _written(without)["allow"]
    assert {k: v for k, v in _written(with_dir).items() if k != "allow"} == \
        {k: v for k, v in _written(without).items() if k != "allow"}


def test_dispatcher_passes_answers_dir(tmp_path):
    """Catches a dispatcher that calls write_role_files without its
    answers_dir: the role would start with no rule for its answer file."""
    d = _base(tmp_path)
    d.approval_policy = load(DEFAULT)
    d._agent_args(Role.PLANNER)
    settings = tmp_path / "run" / "approval" / f"{Role.PLANNER.value}.settings.json"
    assert _written(settings)["allow"] == [_answers_expected(d.answers_dir),
                                           _answers_read_expected(d.answers_dir)]
    # Negative control: the same policy written without the keyword has no
    # such rule, so the rules above can only have come from the dispatcher.
    bare, _ = d.approval_policy.write_role_files(tmp_path / "bare", Role.PLANNER.value)
    assert _answers_expected(d.answers_dir) not in _written(bare)["allow"]
    assert _answers_read_expected(d.answers_dir) not in _written(bare)["allow"]


def test_answers_dir_leaves_the_policy_evidence_unchanged(tmp_path):
    """Catches the answers rule leaking into the policy's recorded evidence:
    the run records what the policy declares; the settings file records the
    added rule."""
    policy = load(DEFAULT)
    before = json.dumps(policy.evidence(), sort_keys=True)
    policy.write_role_files(tmp_path, "planner", answers_dir=tmp_path / "answers")
    assert json.dumps(policy.evidence(), sort_keys=True) == before
    # Negative control: the settings file does carry the rule.
    settings = tmp_path / "approval" / "planner.settings.json"
    assert _answers_expected(tmp_path / "answers") in _written(settings)["allow"]


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
