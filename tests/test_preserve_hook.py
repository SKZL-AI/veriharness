"""The no-delete policy on the path a command actually travels.

The property under test is not "a denylist contains rm". It is:

    project- and evidence-owned state is not silently destroyed by an autonomous
    development run.

So every case here goes through the adapter with the payload shape the harness
really sends, because that is the only version of the policy that has any effect.
A test that asserted the contents of a JSON file would pass while the hook was
unwired.

The role path is guarded elsewhere and tested elsewhere
(`tests/test_approval_policy.py` for the policy that cannot be shortened,
`src/hoh/integrity.py` for the pre/post inventory that is the independent
backstop). This file is about the orchestrator's own commands, which is where the
2026-09-23 removal happened.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "preserve_hook", ROOT / "tools" / "preserve_hook.py")
hook = importlib.util.module_from_spec(_spec)
sys.modules["preserve_hook"] = hook
_spec.loader.exec_module(hook)


def payload(command: str, *, shape: str = "tool_input") -> str:
    if shape == "tool_input":
        return json.dumps({"tool_input": {"command": command}})
    if shape == "camel":
        return json.dumps({"toolInput": {"command": command}})
    if shape == "flat":
        return json.dumps({"command": command})
    return json.dumps({"input": {"command": command}})


def through_the_hook(command: str, monkeypatch, *, shape: str = "tool_input"):
    """Exactly what the harness does: JSON on stdin, exit code out."""
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload(command, shape=shape)))
    return hook.main()


def run_dir(base: Path) -> Path:
    made = base / "20260923t1451z-p227"
    made.mkdir(parents=True)
    (made / "state.json").write_text("{}", encoding="utf-8")
    return made


# --- the four behaviours the policy has to have ----------------------------

def test_destroying_evidence_is_refused(tmp_path, monkeypatch, capsys):
    made = run_dir(tmp_path)
    assert through_the_hook(f"rm -rf {made}", monkeypatch) == 2
    assert "no-delete policy" in capsys.readouterr().err


def test_a_versioned_rename_inside_the_owned_scope_is_allowed(tmp_path, monkeypatch):
    """The safe operation must stay available, or the refusal has nowhere to send
    the caller and the policy becomes something to route around."""
    made = run_dir(tmp_path)
    assert through_the_hook(f"mv {made} {made}.v1.20260924T000000Z", monkeypatch) == 0
    assert through_the_hook(
        f"python3 tools/preserve.py archive {made}", monkeypatch) == 0


def test_ordinary_replacement_inside_the_owned_scope_still_works(tmp_path, monkeypatch):
    """Writing and copying into protected directories is the daily work of the
    programme. A guard that stopped it would be removed within a day."""
    made = run_dir(tmp_path)
    assert through_the_hook(f"cp new.json {made}/state.json", monkeypatch) == 0
    assert through_the_hook(f"cat {made}/state.json", monkeypatch) == 0
    assert through_the_hook(f"tar -cf out.tar {made}", monkeypatch) == 0
    assert through_the_hook(f"ls -la {made}", monkeypatch) == 0


def test_unrelated_temporary_cleanup_is_not_blocked(tmp_path, monkeypatch):
    """The global denylist's design rule, preserved deliberately: recoverable
    local cleanup stays free. Widening this policy to everything would trade a
    real guard for a disabled one."""
    junk = tmp_path / "project" / "node_modules"
    junk.mkdir(parents=True)
    assert through_the_hook(f"rm -rf {junk}", monkeypatch) == 0
    assert through_the_hook(f"rm -rf {tmp_path / 'project' / 'build'}",
                            monkeypatch) == 0


# --- the ways a string hides a command -------------------------------------

def test_a_protected_path_behind_a_conjunction_is_still_caught(tmp_path, monkeypatch):
    made = run_dir(tmp_path)
    for joiner in ("&&", ";", "||", "|"):
        assert through_the_hook(f"echo hi {joiner} rm -rf {made}", monkeypatch) == 2, \
            joiner


def test_an_unparseable_destructive_command_fails_closed(tmp_path, monkeypatch, capsys):
    """Deciding a safety question by reading shell text lost four rounds in the
    P1-16 acceptance record. The honest answer to "I cannot read this" is not
    "then it is fine"."""
    assert through_the_hook("rm -rf 'unterminated", monkeypatch) == 2
    assert "has not been shown to be safe" in capsys.readouterr().err


def test_an_unparseable_harmless_command_is_allowed(monkeypatch):
    """The negative control: only the program changes. This hook is not the
    boundary, and one that blocked everything it could not lex would be switched
    off, taking the cases that matter with it."""
    assert through_the_hook("echo 'unterminated", monkeypatch) == 0


# --- a missing policy is not permission ------------------------------------

def test_a_missing_policy_blocks_destruction_rather_than_assuming_the_best(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hook, "POLICY", tmp_path / "not-here.py")
    assert through_the_hook(f"rm -rf {tmp_path / 'some-runs'}", monkeypatch) == 2
    assert "is not permission" in capsys.readouterr().err


def test_a_missing_policy_does_not_block_everything(tmp_path, monkeypatch):
    """The other half: a broken policy must not brick the session either, or the
    first thing anyone does is unwire the hook."""
    monkeypatch.setattr(hook, "POLICY", tmp_path / "not-here.py")
    assert through_the_hook("python3 -m pytest -q", monkeypatch) == 0


def test_the_policy_loaded_is_the_repositorys_own(monkeypatch):
    """Point of the whole arrangement: the authority is in this tree, versioned
    with the capability that depends on it, not in machine-global configuration
    that other repositories and sessions share."""
    assert hook.POLICY == ROOT / "tools" / "preserve.py"
    assert hook.POLICY.is_file()
    assert hook._policy() is not None


# --- payload shapes and non-payloads ---------------------------------------

@pytest.mark.parametrize("shape", ["tool_input", "camel", "flat", "nested"])
def test_every_payload_shape_the_harness_sends_is_read(tmp_path, monkeypatch, shape):
    made = run_dir(tmp_path)
    assert through_the_hook(f"rm -rf {made}", monkeypatch, shape=shape) == 2, shape


def test_something_that_is_not_a_payload_is_not_this_hooks_business(monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert hook.main() == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert hook.main() == 0


def test_a_payload_with_no_command_is_allowed(monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Read"})))
    assert hook.main() == 0


# --- the word matching -----------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("rm -rf x", True),
    ("git clean -fdx", True),
    ("find . -delete", True),
    ("echo format", False),
    ("./rm-safe --all", False),
    ("python3 -m pytest", False),
    ("cargo build --release", False),
])
def test_destructive_words_match_on_boundaries(text, expected):
    """`format` is not `rm`, and `rm-safe` is not `rm`. A substring match here
    would make the fail-closed branch fire on ordinary work, which is how a
    fail-closed guard becomes a disabled one."""
    assert hook.looks_destructive(text) is expected


def test_decide_is_callable_without_a_payload_for_direct_checks(tmp_path):
    made = run_dir(tmp_path)
    code, message = hook.decide(f"rm -rf {made}")
    assert code == 2 and "protected" in message
    assert hook.decide("") == (0, "")
