"""O215: a role session must not leave derived artifacts in a protected tree.

The defect: a role session's own python wrote `src/hoh/__pycache__/*.pyc` into a
protected arena during its dispatch, and the capability witness correctly reported
that a protected tree had changed. The run blocked.

**The first repair was inert, and the first version of this file could not tell.**
It set the environment on the `subprocess.run` that invokes the `herdr` CLI -- but
Herdr is client/server, every pane is a child of a long-lived server, and the
client's environment is discarded at the socket boundary. Two independent
reviewers proved it, and one of them also proved that all three tests here passed
against a mutant in which the fix was entirely absent: a source-text
`count("env=role_environment()") == 2` is satisfied by a comment and by dead code,
and a test that spawns its own subprocess measures CPython rather than this module.

So the cases below assert the two things that can actually go wrong:

* the **argv** this module emits carries `--env`, because that is the only channel
  that reaches a Herdr-launched pane;
* and the variable does what it is being used for, which is a property of CPython
  and is tested as such rather than as evidence about the wiring.

The distinction is the whole lesson: a test that cannot fail when the mechanism is
removed is not evidence that the mechanism is there.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hoh import dispatchers

ROOT = Path(__file__).resolve().parent.parent


# --- the channel that actually reaches a pane -------------------------------

class _Recorder:
    """Stands in for `_cli`, keeping the argv it was handed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, purpose):
        self.calls.append(list(args))
        return {"result": {"root_pane": {"pane_id": "w0:p1"},
                           "tab": {"tab_id": "w0:t1"}}}


def test_tab_create_carries_the_environment_for_the_launched_process(monkeypatch):
    """The finding both reviewers made, turned into a check. A pane is a child of
    the Herdr server, so the only way to set its environment is `--env` on tab
    create -- measured by a reviewer, who showed a pane created without it writes
    bytecode and the same pane created with it does not."""
    recorder = _Recorder()
    dispatcher = dispatchers.HerdrDispatcher.__new__(dispatchers.HerdrDispatcher)
    dispatcher.cwd = "/tmp"
    dispatcher.own_tabs = []
    monkeypatch.setattr(dispatcher, "_cli", recorder, raising=False)

    dispatcher._new_tab_pane(label="probe", cwd="/tmp")

    assert recorder.calls, "no CLI call was made at all"
    args = recorder.calls[0]
    assert args[:3] == ["herdr", "tab", "create"]
    for key, value in dispatchers.ROLE_PANE_ENV.items():
        assert "--env" in args, "the pane is launched with no environment at all"
        assert f"{key}={value}" in args, f"{key} does not reach the launched pane"
        # And it is the *value* of an --env flag, not a coincidence elsewhere.
        assert args[args.index(f"{key}={value}") - 1] == "--env"


def test_the_variable_this_project_needs_is_the_one_being_set():
    """If this ever grows, the ledger entry and the docstrings have to grow with
    it, so the test names the current contents rather than accepting any dict."""
    assert dispatchers.ROLE_PANE_ENV == {"PYTHONDONTWRITEBYTECODE": "1"}


def test_the_direct_subprocess_path_carries_it_too():
    """`HarnessDispatcher` starts the agent itself, so inheritance works there.
    It is not the path that matters for O215 -- an evidencing run may not use it --
    but it should not be the odd one out."""
    env = dispatchers.role_environment()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env.get("PATH") == os.environ.get("PATH"), "the rest is inherited"
    source = (ROOT / "src/hoh/dispatchers.py").read_text(encoding="utf-8")
    assert "env=role_environment()" in source


# --- what the variable does, tested as a fact about CPython ----------------

def test_the_variable_suppresses_bytecode(tmp_path):
    """The mechanism, on a real subprocess. This is *not* evidence that the
    wiring is right -- that is the argv test above -- and the first version of
    this file confused the two."""
    (tmp_path / "leaves_bytecode.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "go.py").write_text("import leaves_bytecode\n", encoding="utf-8")

    plain = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    subprocess.run([sys.executable, "go.py"], cwd=tmp_path, env=plain, check=True)
    assert (tmp_path / "__pycache__").is_dir(), "without it, bytecode appears"

    import shutil
    shutil.move(str(tmp_path / "__pycache__"), str(tmp_path / "pycache.v1.moved"))
    subprocess.run([sys.executable, "go.py"], cwd=tmp_path,
                   env=dispatchers.role_environment(), check=True)
    assert not (tmp_path / "__pycache__").exists()


@pytest.mark.parametrize("how", [
    ["-c", "import sys; sys.dont_write_bytecode = False; import target"],
    ["-c", "import py_compile; py_compile.compile('target.py')"],
    ["-m", "compileall", "-q", "."],
])
def test_it_is_hygiene_and_not_enforcement(tmp_path, how):
    """Both reviewers established this and it belongs in the tests, not only in a
    docstring: an agent that means to write bytecode still can. What the setting
    removes is the *accidental* artifact that was blocking honest runs. The
    witness, not this, is what would catch a planted `.pyc`."""
    (tmp_path / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([sys.executable, *how], cwd=tmp_path,
                   env=dispatchers.role_environment(), check=True)
    assert list(tmp_path.glob("**/*.pyc")), (
        "if this ever stops writing bytecode, the claim in ROLE_PANE_ENV's "
        "comment has become too weak and should be strengthened")


def test_a_reused_pane_is_a_known_gap_and_is_named_as_one():
    """Reviewer A's residual finding: `_reusable_agent` re-adopts a pane Herdr
    restored, skipping `_new_tab_pane` entirely, so a pane created before this
    setting keeps its old environment. `--env` at creation cannot cover that, and
    pretending otherwise is the overclaim this file exists to prevent."""
    source = (ROOT / "src/hoh/dispatchers.py").read_text(encoding="utf-8")
    assert "O215 does not reach a reused pane" in source, (
        "the reuse branch must say in the source that the setting does not "
        "reach it")
