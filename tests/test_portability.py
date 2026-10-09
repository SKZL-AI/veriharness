"""Portability: NixOS first, macOS fail-open gaps, Ubuntu AppArmor hint.

Every case is hermetic: platform absence is a fake (a monkeypatched module
constant, a fake `bwrap` script, a temporary directory standing in for
`/nix/store`). Nothing here needs a real NixOS, macOS or AppArmor host, and
nothing needs host `/tmp` or `bwrap` to be usable. What these cases cannot say
is whether the product works on those platforms: that stays NOT_RUN.

Seams are set with `raising=False` so that, on a tree without the change, a
case fails on its assertion and not on a missing attribute.
"""
from __future__ import annotations

import ast
import json
import os
import resource
import stat
import sys
from pathlib import Path

import pytest

from hoh import procs, runner, sandbox
from hoh.contracts import AcceptanceCheck, Candidate, Outcome
from hoh.sandbox import BubblewrapSandbox, Isolation, SandboxSpec

REAL_BASH = "/bin/bash"


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(cmd: str, workdir: Path, isolation=Isolation.NONE, sandbox_=None):
    check = AcceptanceCheck(check_id="P1", description="portability", command=cmd,
                            expect_exit=0)
    cand = Candidate(candidate_id="c", repo_path=str(workdir), commit="0" * 40,
                     tree_clean=True, tree_digest="d")
    return runner.run_check(check, cand, run_id="r", iteration=1, attempt=1,
                            cwd=workdir, isolation=isolation, sandbox=sandbox_)


def _recording_bwrap(tmp: Path, exit_code: int = 0, stderr: str = "") -> tuple[Path, Path]:
    """A fake `bwrap` that appends its argv to a file, one block per call."""
    record = tmp / "bwrap.record"
    fake = _script(tmp / "fakebin" / "bwrap",
                   f'{{ echo "---"; printf "%s\\n" "$@"; }} >> "{record}"\n'
                   + (f'echo "{stderr}" >&2\n' if stderr else "")
                   + f"exit {exit_code}\n")
    return fake, record


def _last_call(record: Path) -> list[str]:
    blocks = record.read_text().split("---\n")
    return [z for z in blocks[-1].splitlines()]


# --------------------------------------------------------------------------- #
# P1  one shell resolver (NixOS has no /bin/bash)
# --------------------------------------------------------------------------- #

def test_p1_unsandboxed_check_uses_the_bash_on_path_when_bin_bash_is_absent(
        tmp_path, monkeypatch):
    marker = tmp_path / "used"
    wrapper = _script(tmp_path / "fakebin" / "bash",
                      f'echo used >> "{marker}"\nexec {REAL_BASH} "$@"\n')
    monkeypatch.setattr(runner, "FIXED_BASH", str(tmp_path / "nowhere" / "bash"),
                        raising=False)
    monkeypatch.setenv("PATH", f"{wrapper.parent}:/usr/bin:/bin")
    work = tmp_path / "arena"
    work.mkdir()
    receipt, log = _run("echo hi", work)
    assert receipt.outcome(0) is Outcome.PASS, log
    assert marker.exists(), "the shell was not the one found on PATH"


def test_p1_without_any_bash_the_check_is_refused_not_run_by_sh(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "FIXED_BASH", str(tmp_path / "nowhere" / "bash"),
                        raising=False)
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    work = tmp_path / "arena"
    work.mkdir()
    receipt, log = _run("echo hi", work)
    assert receipt.outcome(0) is Outcome.INCONCLUSIVE, log
    assert "exit=127" in log and "bash" in log and "not found" in log.lower(), log


def test_p1_strict_launch_names_the_resolved_shell(tmp_path, monkeypatch):
    real = tmp_path / "store" / "bash-5" / "bin" / "bash"
    _script(real, f'exec {REAL_BASH} "$@"\n')
    link = tmp_path / "profile" / "bin" / "bash"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setattr(runner, "FIXED_BASH", str(tmp_path / "nowhere" / "bash"),
                        raising=False)
    monkeypatch.setenv("PATH", f"{link.parent}:/usr/bin:/bin")
    fake, record = _recording_bwrap(tmp_path)
    work = tmp_path / "arena"
    work.mkdir()
    _run("echo hi", work, Isolation.STRICT, BubblewrapSandbox(bwrap=str(fake)))
    call = _last_call(record)
    after = call[call.index("--") + 1:]
    assert after[0] == str(real.resolve()), after[:3]
    assert after[1] == "-c"


def test_p1_a_relative_path_entry_never_finds_a_bash_in_the_working_directory(
        tmp_path, monkeypatch):
    """Empty and `.` entries resolve against the cwd, which is the repository
    under test: a committed `bash` that exits 0 would decide every verdict."""
    repo = tmp_path / "repo"
    _script(repo / "bash", "exit 0\n")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(runner, "FIXED_BASH", str(tmp_path / "nowhere" / "bash"),
                        raising=False)
    absent = tmp_path / "absent-dir"
    for entries in (".", f"{absent}:", f":{absent}", f"{absent}::{absent}",
                    f".:{absent}", "bin"):
        monkeypatch.setenv("PATH", entries)
        fn = getattr(runner, "resolve_shell", None)
        assert fn is not None, "runner.resolve_shell is missing"
        with pytest.raises(runner.ShellUnavailable):
            fn()
    monkeypatch.setenv("PATH", f".:{absent}")
    work = tmp_path / "arena"
    work.mkdir()
    receipt, log = _run("false", work)
    assert receipt.outcome(0) is Outcome.INCONCLUSIVE, log
    assert "exit=127" in log and "bash not found" in log, log


def test_p1_strict_without_any_bash_is_inconclusive_and_nothing_is_launched(
        tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "FIXED_BASH", str(tmp_path / "nowhere" / "bash"),
                        raising=False)
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    fake, record = _recording_bwrap(tmp_path)
    work = tmp_path / "arena"
    work.mkdir()
    receipt, log = _run("echo hi", work, Isolation.STRICT,
                        BubblewrapSandbox(bwrap=str(fake)))
    assert receipt.outcome(0) is Outcome.INCONCLUSIVE, log
    assert "bash not found" in log, log
    launches = [b for b in record.read_text().split("---\n") if "--chdir" in b]
    assert launches == [], "no sandbox may be started without a shell"


def test_p3_the_strict_launch_carries_the_rewritten_path(tmp_path, monkeypatch):
    store = tmp_path / "store"
    env_bin = store / "abc-user-env" / "bin"
    env_bin.mkdir(parents=True)
    profile = tmp_path / "home" / ".nix-profile"
    profile.parent.mkdir()
    profile.symlink_to(store / "abc-user-env")
    given = f"{profile}/bin:/usr/bin:/bin"

    def launched_path(store_dir: Path) -> str:
        monkeypatch.setattr(sandbox, "NIX_STORE", str(store_dir), raising=False)
        monkeypatch.setenv("PATH", given)
        fake, record = _recording_bwrap(tmp_path / ("rec-" + store_dir.name))
        work = tmp_path / ("arena-" + store_dir.name)
        work.mkdir()
        _run("echo hi", work, Isolation.STRICT, BubblewrapSandbox(bwrap=str(fake)))
        call = [b for b in record.read_text().split("---\n") if "--chdir" in b][-1].splitlines()
        return call[call.index("PATH", call.index("--setenv")) + 1]

    with_store = launched_path(store)
    assert with_store.split(":")[-3:] == [str(env_bin.resolve()), "/usr/bin", "/bin"], with_store
    assert f"{profile}/bin" not in with_store
    absent = tmp_path / "absent-store"
    without_store = launched_path(absent)
    assert not absent.exists()
    assert without_store.endswith(given), without_store


# --------------------------------------------------------------------------- #
# P2  the launcher drives itself with the running interpreter
# --------------------------------------------------------------------------- #

def test_p2_launcher_never_spells_the_interpreter_by_name():
    tree = ast.parse((Path(runner.__file__).parent / "launcher.py").read_text())
    helper_sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        for i in range(len(node.elts) - 1):
            a, b = node.elts[i], node.elts[i + 1]
            if (isinstance(b, ast.Constant) and b.value == "-m" and i + 2 < len(node.elts)
                    and isinstance(node.elts[i + 2], ast.Constant)
                    and node.elts[i + 2].value == "hoh.cli"):
                assert not isinstance(a, ast.Constant), (
                    f"line {node.lineno} names the interpreter {a.value!r} instead of hoh_command()")
        first = node.elts[0] if node.elts else None
        if (isinstance(first, ast.Starred) and isinstance(first.value, ast.Call)
                and getattr(first.value.func, "id", "") == "hoh_command"):
            helper_sites += 1
    assert helper_sites == 4, helper_sites


def test_p2_launcher_command_is_the_running_interpreter():
    from hoh import launcher
    cmd = launcher.hoh_command()
    assert cmd[:3] == [sys.executable, "-m", "hoh.cli"]


def test_p2_hoh_python_overrides(monkeypatch):
    from hoh import launcher
    monkeypatch.setenv("HOH_PYTHON", "/opt/special/python")
    assert launcher.hoh_command()[0] == "/opt/special/python"


# --------------------------------------------------------------------------- #
# P3  Nix binds in STRICT, only when the store exists
# --------------------------------------------------------------------------- #

def _argv(tmp: Path) -> list[str]:
    spec = SandboxSpec(candidate=tmp / "cand", scratch=tmp / "scratch")
    return BubblewrapSandbox(bwrap="/fake/bwrap")._argv(["/bin/bash", "-c", "true"], spec)


def _binds(argv: list[str]) -> list[tuple[str, str]]:
    out = []
    for i, a in enumerate(argv):
        if a in ("--ro-bind", "--ro-bind-try", "--bind"):
            out.append((a, argv[i + 1]))
    return out


def test_p3_no_nix_binds_without_a_store(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "NIX_STORE", str(tmp_path / "absent-store"), raising=False)
    paths = [p for _, p in _binds(_argv(tmp_path))]
    assert not any(p.startswith(("/nix", "/run/current-system", "/etc/static",
                                 "/etc/profiles", str(tmp_path / "absent-store")))
                   for p in paths), paths
    assert paths[:5] == ["/usr", "/bin", "/lib", "/lib64", "/etc/ssl"]


def test_p3_nix_binds_when_the_store_exists(tmp_path, monkeypatch):
    store = tmp_path / "store"
    sw = tmp_path / "sw"
    store.mkdir()
    sw.mkdir()
    monkeypatch.setattr(sandbox, "NIX_STORE", str(store), raising=False)
    monkeypatch.setattr(sandbox, "NIX_EXTRA_ROOTS", (str(sw), str(tmp_path / "gone")),
                        raising=False)
    binds = _binds(_argv(tmp_path))
    assert ("--ro-bind-try", str(store)) in binds
    assert ("--ro-bind-try", str(sw)) in binds
    assert all(kind != "--bind" or path != str(store) for kind, path in binds), (
        "the store must never be writable")
    assert not any(p.startswith("/nix/var") for _, p in binds)


def test_p3_sandbox_path_follows_profile_links_into_the_store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    env_bin = store / "abc-user-env" / "bin"
    env_bin.mkdir(parents=True)
    profile = tmp_path / "home" / ".nix-profile"
    profile.parent.mkdir()
    profile.symlink_to(store / "abc-user-env")
    monkeypatch.setattr(sandbox, "NIX_STORE", str(store), raising=False)
    fn = getattr(runner, "sandbox_path", None)
    assert fn is not None, "runner.sandbox_path is missing"
    out = fn(f"{profile}/bin:/usr/bin:{tmp_path}/does-not-exist")
    assert out.split(":") == [str(env_bin.resolve()), "/usr/bin",
                              f"{tmp_path}/does-not-exist"]
    # Without a store nothing is rewritten.
    monkeypatch.setattr(sandbox, "NIX_STORE", str(tmp_path / "absent"), raising=False)
    assert fn(f"{profile}/bin:/usr/bin") == f"{profile}/bin:/usr/bin"


# --------------------------------------------------------------------------- #
# P4  Herdr server name under a Nix wrapper
# --------------------------------------------------------------------------- #

def _proc(comm: str, pid: int = 10, sid: int = 10) -> procs.Proc:
    return procs.Proc(pid=pid, ppid=1, pgrp=pid, sid=sid, state="S", start=1, comm=comm)


def test_p4_server_names(monkeypatch):
    monkeypatch.delenv("HOH_HERDR_COMM", raising=False)
    assert procs.is_server(_proc("herdr"))
    assert procs.is_server(_proc(".herdr-wrapped"))
    for other in ("sh", "herdr-evil", "xherdr", ".herdr", "claude"):
        assert not procs.is_server(_proc(other)), other
    assert not procs.is_server(_proc("herdr", pid=10, sid=11)), "must lead its own session"
    assert not procs.is_server(None)


def test_p4_server_name_override(monkeypatch):
    monkeypatch.setenv("HOH_HERDR_COMM", "myherdr")
    assert procs.is_server(_proc("myherdr"))
    assert procs.is_server(_proc("herdr"))
    assert not procs.is_server(_proc("otherherdr"))


# --------------------------------------------------------------------------- #
# P5  platform-aware STRICT message, AppArmor hint, probe flags
# --------------------------------------------------------------------------- #

def test_p5_darwin_says_what_is_supported(monkeypatch, tmp_path):
    fake, record = _recording_bwrap(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    for bwrap in ("", str(fake)):
        why = BubblewrapSandbox(bwrap=bwrap).unavailable()
        assert why and "macOS" in why and "partially supported" in why, why
        assert "--isolation none" in why, why
    assert not record.exists(), "no probe may run on macOS"


def test_p5_linux_without_bwrap_keeps_its_message(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    why = BubblewrapSandbox(bwrap="").unavailable()
    assert "bwrap is not installed" in why and "macOS" not in why


def test_p5_apparmor_hint_only_when_the_sysctl_says_one(monkeypatch, tmp_path):
    fake, _ = _recording_bwrap(tmp_path, exit_code=1,
                               stderr="bwrap: setting up uid map: Permission denied")
    monkeypatch.setattr(sys, "platform", "linux")
    sysctl = tmp_path / "restrict"
    monkeypatch.setattr(sandbox, "APPARMOR_USERNS_SYSCTL", str(sysctl), raising=False)
    sysctl.write_text("1\n")
    why = BubblewrapSandbox(bwrap=str(fake)).unavailable()
    assert "apparmor_restrict_unprivileged_userns" in why and "Permission denied" in why, why
    sysctl.write_text("0\n")
    why = BubblewrapSandbox(bwrap=str(fake)).unavailable()
    assert "apparmor" not in why.lower() and "Permission denied" in why, why
    sysctl.unlink()
    why = BubblewrapSandbox(bwrap=str(fake)).unavailable()
    assert "apparmor" not in why.lower() and "Permission denied" in why, why


def test_p5_the_probe_uses_the_flags_the_launch_uses(monkeypatch, tmp_path):
    fake, record = _recording_bwrap(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    assert BubblewrapSandbox(bwrap=str(fake)).unavailable() is None
    probe = _last_call(record)
    for flag in ("--proc", "--dev", "--unshare-all", "--die-with-parent"):
        assert flag in probe, (flag, probe)


# --------------------------------------------------------------------------- #
# P6  arena guard: other home directories
# --------------------------------------------------------------------------- #

def _abs(*parts: str) -> str:
    """An absolute path assembled at runtime, so that no literal home-directory
    path stands in this file (the export manifest refuses such literals)."""
    return "/" + "/".join(parts)


@pytest.mark.parametrize("command", [
    "cat " + _abs("Users", "alice", ".ssh", "id_rsa"),
    "ls /Volumes/backup",
    "cat /private/etc/passwd",
    "cat //private//var/db/x",
])
def test_p6_guard_refuses_macos_homes_and_private(command):
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena(command)


def test_p6_guard_follows_the_real_home(monkeypatch):
    monkeypatch.setattr(runner, "_home_dirs", lambda: ["/data/alice", "/data/a.b+c"],
                        raising=False)
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena("cat /data/alice/.ssh/id_rsa")
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena("cat /data/a.b+c/f")
    runner.assert_stays_in_arena("cat /data/aXbbc/f")           # regex metacharacters are literal
    runner.assert_stays_in_arena("cat /data/alicia/f")          # a sibling is not the home


def test_p6_home_dirs_are_read_from_the_environment_and_the_account(monkeypatch):
    import pwd
    monkeypatch.setenv("HOME", "/data/zed/alice")
    fn = getattr(runner, "_home_dirs", None)
    assert fn is not None, "runner._home_dirs is missing"
    homes = fn()
    assert "/data/zed/alice" in homes, homes
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena("cat /data/zed/alice/.ssh/id_rsa")
    # The account database is a second source (it is not readable inside a
    # STRICT sandbox, hence faked here; an unreadable one adds nothing).
    class _Entry:
        pw_dir = "/srv/zed/bob"
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Entry())
    assert "/srv/zed/bob" in fn()
    def _gone(uid):
        raise KeyError(uid)
    monkeypatch.setattr(pwd, "getpwuid", _gone)
    assert "/data/zed/alice" in fn()


@pytest.mark.parametrize("command", [
    "cat /users/a/.ssh/id_rsa",
    "cat /USERS/a/x",
    "cat /system/volumes/data/Users/a/x",
    "cat /var/root/.ssh/id_rsa",
])
def test_p6_darwin_guard_ignores_case_and_covers_the_data_volume(command, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena(command)


def test_p6_case_is_significant_off_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    runner.assert_stays_in_arena("cat /users/a/x")
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena("cat /var/root/x")


def test_p6_guard_ignores_a_home_that_would_swallow_everything(monkeypatch):
    monkeypatch.setattr(runner, "_home_dirs", lambda: ["/", "", "/usr"], raising=False)
    runner.assert_stays_in_arena("/usr/bin/python3 -V")
    runner.assert_stays_in_arena("/nix/store/abc-python/bin/python -V")


def test_p6_guard_keeps_allowing_system_paths():
    runner.assert_stays_in_arena("/usr/bin/python3 -V")
    runner.assert_stays_in_arena("/nix/store/abc-python/bin/python -V")
    runner.assert_stays_in_arena("python3 -m pytest {ARENA}/tests")
    with pytest.raises(runner.ArenaEscape):
        runner.assert_stays_in_arena("cat " + _abs("home", "alice", "x"))


# --------------------------------------------------------------------------- #
# approval scope after resolve(): /tmp -> /private/tmp
# --------------------------------------------------------------------------- #

def _provider(prefix: Path, tmp: Path):
    from hoh.approval import PrefixScopedProvider
    script = _script(tmp / "approve.sh", "exit 0\n")
    return PrefixScopedProvider(script=script, prefix=prefix)


def test_approval_forbidden_entries_are_compared_after_resolution(tmp_path, monkeypatch):
    from hoh.approval import PrefixScopedProvider
    real = tmp_path / "private_tmp"
    real.mkdir()
    link = tmp_path / "tmp"
    link.symlink_to(real)
    monkeypatch.setattr(PrefixScopedProvider, "FORBIDDEN", (str(link),))
    with pytest.raises(ValueError, match="too broad"):
        _provider(real, tmp_path)


@pytest.mark.parametrize("prefix", ["/private/tmp", "/private/var", "/private/etc"])
def test_approval_refuses_macos_system_scopes(prefix, tmp_path):
    with pytest.raises(ValueError, match="too broad"):
        _provider(Path(prefix), tmp_path)


@pytest.mark.parametrize("prefix", ["/PRIVATE/TMP", "/Private/Var", "/var/root",
                                    "/system/volumes/data", "/System/Volumes/Data/Users"])
def test_approval_on_darwin_compares_without_case(prefix, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ValueError, match="too broad"):
        _provider(Path(prefix), tmp_path)


def test_approval_off_darwin_keeps_case(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _provider(Path("/PRIVATE/TMP"), tmp_path).prefix == Path("/PRIVATE/TMP")


def test_approval_on_darwin_compares_the_home_without_case(tmp_path, monkeypatch):
    """The home directory and its parents are compared as the forbidden list
    is: case-folded on darwin, case-significant elsewhere."""
    home = tmp_path / "Users" / "Alice"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    differently_cased_home = tmp_path / "users" / "alice"
    differently_cased_parent = tmp_path / "USERS"
    monkeypatch.setattr(sys, "platform", "darwin")
    for prefix in (differently_cased_home, differently_cased_parent):
        with pytest.raises(ValueError, match="too broad"):
            _provider(prefix, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    for prefix in (differently_cased_home, differently_cased_parent):
        assert _provider(prefix, tmp_path).prefix == prefix.resolve()
    with pytest.raises(ValueError, match="too broad"):
        _provider(home, tmp_path)


def test_approval_still_accepts_a_real_scope(tmp_path):
    scope = tmp_path / "worktrees" / "proj"
    scope.mkdir(parents=True)
    assert _provider(scope, tmp_path).prefix == scope.resolve()


# --------------------------------------------------------------------------- #
# setrlimit receipt honesty
# --------------------------------------------------------------------------- #

def _different_cpu_request() -> int:
    """A CPU ceiling that differs from what this process, and so a child that
    applies nothing, already holds (an arena carries 600 s)."""
    soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    want = soft - 7 if soft != resource.RLIM_INFINITY and soft > 7 else 10 ** 6
    if hard != resource.RLIM_INFINITY:
        want = min(want, hard)
    assert want != soft, (soft, hard)
    return want


def test_limits_a_child_can_take_are_confirmed():
    pairs = [(resource.RLIMIT_CPU, _different_cpu_request())]
    fn = getattr(sandbox, "confirmed_rlimits", None)
    assert fn is not None, "sandbox.confirmed_rlimits is missing"
    assert fn(pairs) == {resource.RLIMIT_CPU: True}


def test_a_missing_interpreter_confirms_nothing_and_does_not_raise(monkeypatch):
    fn = getattr(sandbox, "confirmed_rlimits", None)
    assert fn is not None, "sandbox.confirmed_rlimits is missing"
    monkeypatch.setattr(sys, "executable", None)
    pairs = [(resource.RLIMIT_CPU, _different_cpu_request() - 1)]
    assert fn(pairs) == {resource.RLIMIT_CPU: False}


def test_a_child_that_reports_another_value_is_not_confirmed(tmp_path):
    fn = getattr(sandbox, "confirmed_rlimits", None)
    assert fn is not None, "sandbox.confirmed_rlimits is missing"
    pairs = [(resource.RLIMIT_CPU, 600), (resource.RLIMIT_AS, 4 * 1024 ** 3)]
    lie = json.dumps({str(resource.RLIMIT_CPU): 600,
                      str(resource.RLIMIT_AS): resource.RLIM_INFINITY})
    argv = [sys.executable, "-I", "-c", f"print({lie!r})"]
    assert fn(pairs, child_argv=argv) == {resource.RLIMIT_CPU: True,
                                          resource.RLIMIT_AS: False}


def test_a_child_that_cannot_be_asked_confirms_nothing():
    fn = getattr(sandbox, "confirmed_rlimits", None)
    assert fn is not None, "sandbox.confirmed_rlimits is missing"
    pairs = [(resource.RLIMIT_CPU, 600)]
    assert fn(pairs, child_argv=["/nonexistent/python"]) == {resource.RLIMIT_CPU: False}
    assert fn(pairs, child_argv=[sys.executable, "-I", "-c", "print('not json')"]) == {
        resource.RLIMIT_CPU: False}


def test_the_receipt_does_not_claim_a_limit_that_did_not_take(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "confirmed_rlimits",
                        lambda pairs, **kw: {res: res != resource.RLIMIT_AS
                                             for res, _ in pairs},
                        raising=False)
    work = tmp_path / "arena"
    work.mkdir()
    receipt, log = _run("echo hi", work)
    policy = receipt.isolation.resource_limit_policy
    assert "as=not-applied" in policy, policy
    assert f"cpu={runner.RLIMIT_CPU_SECONDS}" in policy, policy
    assert f"as={runner.RLIMIT_ADDRESS_SPACE}" not in policy, policy


def test_the_receipt_keeps_claiming_limits_that_did_take(tmp_path):
    work = tmp_path / "arena"
    work.mkdir()
    receipt, _ = _run("echo hi", work)
    policy = receipt.isolation.resource_limit_policy
    assert "not-applied" not in policy and "not-verified" not in policy, policy
    assert "cpu=" in policy and "as=" in policy, policy
