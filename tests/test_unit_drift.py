"""Does the unit-drift check see the failure that went unseen for 18 days?

#37. `~/.config/systemd/user/hoh-fortsetzen.service` called a CLI verb that had
been renamed, failed on every tick from 2026-09-07 on, and nothing looked.
These tests pin the comparison that would have looked, and -- as important --
that looking never changes anything: the check is handed fixture directories
and a fake `systemctl show`, and it is asserted to leave both untouched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "unit_drift", ROOT / "tools" / "unit_drift.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("unit_drift", mod)
    spec.loader.exec_module(mod)
    return mod


ud = _load()

SERVICE = """[Unit]
Description=HoH: resume runs that are waiting on quota
Documentation=file://%h/hoh/docs/OPERATIONS.md
After=network-online.target

[Service]
Type=oneshot
# a comment
ExecStart=%h/hoh/plugin/bin/hoh resume-quota
ReadWritePaths=%h/hoh/runs
"""
TIMER = """[Timer]
OnUnitActiveSec=15min
Persistent=true
"""
VERBS = {"resume-quota", "run", "status"}


def _tree(tmp_path: Path, installed: dict[str, str]) -> tuple[Path, Path]:
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "hoh-resume-quota.service").write_text(SERVICE)
    (ops / "hoh-resume-quota.timer").write_text(TIMER)
    inst = tmp_path / "installed"
    inst.mkdir()
    for name, text in installed.items():
        (inst / name).write_text(text)
    return ops, inst


def _healthy(_unit):
    return {"ActiveState": "active", "Result": "success",
            "UnitFileState": "enabled"}


def _check(ops, inst, **kw):
    kw.setdefault("show", _healthy)
    return ud.check(ops=ops, installed=inst, home="/h", verbs=VERBS,
                    exists=lambda p: True, **kw)


def _codes(rep):
    return sorted(f.code for f in rep.findings)


def test_identical_installation_is_clean(tmp_path):
    ops, inst = _tree(tmp_path, {"hoh-resume-quota.service": SERVICE,
                                 "hoh-resume-quota.timer": TIMER})
    rep = _check(ops, inst, expect="installed")
    assert rep.findings == []
    assert rep.exit_code() == 0


def test_comments_description_and_documentation_are_not_material(tmp_path):
    translated = (SERVICE.replace("resume runs that are waiting on quota",
                                  "Laeufe wieder aufnehmen")
                  .replace("docs/OPERATIONS.md", "RUNBOOK.md")
                  .replace("# a comment", "# ein Kommentar\n\n# noch einer"))
    ops, inst = _tree(tmp_path, {"hoh-resume-quota.service": translated,
                                 "hoh-resume-quota.timer": TIMER})
    assert _check(ops, inst, expect="installed").findings == []


def test_the_real_incident_is_seen_four_ways(tmp_path):
    """The installed shape of 2026-09-07: renamed unit, obsolete verb, failed."""
    old = SERVICE.replace("resume-quota", "fortsetzen")
    ops, inst = _tree(tmp_path, {"hoh-fortsetzen.service": old,
                                 "hoh-fortsetzen.timer": TIMER})

    def show(unit):
        if unit.endswith(".service"):
            return {"ActiveState": "failed", "Result": "exit-code"}
        return _healthy(unit)

    rep = _check(ops, inst, show=show)
    codes = _codes(rep)
    assert codes.count("OBSOLETE_UNIT") == 2
    assert "UNKNOWN_VERB" in codes
    assert "UNIT_FAILED" in codes
    assert rep.exit_code() == 1
    # under "any" not installing the canonical unit is a fact, not a failure
    assert all(f.code != "NOT_INSTALLED" for f in rep.failing())


def test_material_drift_is_named_with_both_sides(tmp_path):
    drifted = SERVICE.replace("ReadWritePaths=%h/hoh/runs", "ReadWritePaths=%h")
    ops, inst = _tree(tmp_path, {"hoh-resume-quota.service": drifted,
                                 "hoh-resume-quota.timer": TIMER})
    rep = _check(ops, inst)
    [f] = [f for f in rep.findings if f.code == "MATERIAL_DRIFT"]
    assert "ReadWritePaths=%h/hoh/runs" in f.detail
    assert "ReadWritePaths=%h']" in f.detail
    assert rep.exit_code() == 1


def test_reordered_directives_are_drift(tmp_path):
    two = SERVICE.replace("ExecStart=%h/hoh/plugin/bin/hoh resume-quota",
                          "ExecStart=/bin/a\nExecStart=/bin/b")
    swapped = SERVICE.replace("ExecStart=%h/hoh/plugin/bin/hoh resume-quota",
                              "ExecStart=/bin/b\nExecStart=/bin/a")
    ops, inst = _tree(tmp_path, {"hoh-resume-quota.service": swapped,
                                 "hoh-resume-quota.timer": TIMER})
    (ops / "hoh-resume-quota.service").write_text(two)
    assert "MATERIAL_DRIFT" in _codes(_check(ops, inst))


def test_not_installed_fails_only_when_installation_is_expected(tmp_path):
    ops, inst = _tree(tmp_path, {})
    assert _check(ops, inst, expect="any").exit_code() == 0
    rep = _check(ops, inst, expect="installed")
    assert rep.exit_code() == 1
    assert {f.code for f in rep.failing()} == {"REQUIRED_NOT_INSTALLED"}


def test_missing_canonical_unit_fails(tmp_path):
    ops, inst = _tree(tmp_path, {})
    for p in ops.iterdir():
        p.rename(tmp_path / (p.name + ".v1.parked"))
    rep = _check(ops, inst)
    assert _codes(rep) == ["CANONICAL_MISSING"]
    assert rep.exit_code() == 1


def test_missing_executable_and_inactive_timer(tmp_path):
    ops, inst = _tree(tmp_path, {"hoh-resume-quota.service": SERVICE,
                                 "hoh-resume-quota.timer": TIMER})

    def show(unit):
        if unit.endswith(".timer"):
            return {"ActiveState": "inactive", "UnitFileState": "disabled"}
        return _healthy(unit)

    rep = ud.check(ops=ops, installed=inst, home="/h", verbs=VERBS,
                   exists=lambda p: False, show=show)
    codes = _codes(rep)
    assert "MISSING_EXECUTABLE" in codes
    assert "TIMER_INACTIVE" in codes


def test_unreadable_systemd_is_not_run_not_a_pass(tmp_path):
    ops, inst = _tree(tmp_path, {"hoh-resume-quota.service": SERVICE,
                                 "hoh-resume-quota.timer": TIMER})
    rep = _check(ops, inst, show=lambda unit: None)
    assert not rep.systemd_readable
    assert rep.exit_code() == 2


def test_the_verb_list_comes_from_the_real_parser():
    verbs = ud.cli_verbs()
    assert "resume-quota" in verbs
    assert "fortsetzen" not in verbs


def test_the_check_writes_nothing(tmp_path):
    old = SERVICE.replace("resume-quota", "fortsetzen")
    ops, inst = _tree(tmp_path, {"hoh-fortsetzen.service": old,
                                 "hoh-fortsetzen.timer": TIMER})

    def digest():
        h = hashlib.sha256()
        for p in sorted(tmp_path.rglob("*")):
            h.update(str(p).encode())
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()

    before = digest()
    _check(ops, inst)
    assert digest() == before


def test_the_source_calls_no_state_changing_systemctl_verb():
    src = (ROOT / "tools" / "unit_drift.py").read_text()
    code = "\n".join(z for z in src.splitlines()
                     if not z.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]          # past the module docstring
    for verb in ("enable", "disable", "start", "stop", "restart",
                 "daemon-reload", "reenable", "link", "mask"):
        assert f'"{verb}"' not in body, verb


def _readiness():
    if "readiness" in sys.modules:
        return sys.modules["readiness"]
    spec = importlib.util.spec_from_file_location(
        "readiness", ROOT / "tools" / "readiness.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["readiness"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_readiness_row_is_blocking_on_installed_breakage(monkeypatch):
    rd = _readiness()
    payload = ('{"installed": ["hoh-fortsetzen.service"], "failing": '
               '[{"code": "UNKNOWN_VERB", "unit": "hoh-fortsetzen.service", '
               '"detail": "x"}]}')
    monkeypatch.setattr(rd, "_py", lambda *a, **k: (1, payload))
    row = rd.row_unit_drift()
    assert row.state == rd.FAIL and not row.advisory
    assert "UNKNOWN_VERB" in row.value_


def test_readiness_row_passes_when_nothing_is_installed(monkeypatch):
    rd = _readiness()
    monkeypatch.setattr(rd, "_py", lambda *a, **k: (
        0, '{"installed": [], "failing": []}'))
    assert rd.row_unit_drift().state == rd.PASS


def test_readiness_row_unreadable_is_not_run(monkeypatch):
    rd = _readiness()
    monkeypatch.setattr(rd, "_py", lambda *a, **k: (2, '{"installed": ["a"]}'))
    assert rd.row_unit_drift().state == rd.NOT_RUN
    monkeypatch.setattr(rd, "_py", lambda *a, **k: (1, "Traceback: boom"))
    assert rd.row_unit_drift().state == rd.NOT_RUN


def test_readiness_lists_the_row():
    rd = _readiness()
    import inspect
    assert "row_unit_drift()" in inspect.getsource(rd.row_list)


CONTINUE = SERVICE.replace("resume-quota", "resume-quota --drive --recover")


def _driven_root(tmp_path, closed=False):
    """A HoH root holding one open project that registers a driver."""
    from hoh.project import Lifecycle, ProjectState, TaskNode
    from hoh.projectstore import ProjectStore

    root = tmp_path / "root"
    s = ProjectStore(root, "p")
    st = ProjectState(project_id="p", repo_path=str(tmp_path),
                      nodes=[TaskNode(id="a", lifecycle=Lifecycle.READY)])
    s.create(st)
    (s.dir / "driver.json").write_text('{"factory": "x:y"}')
    return root


def _with_continue(tmp_path, installed):
    ops, inst = _tree(tmp_path, installed)
    (ops / "hoh-continue.service").write_text(CONTINUE)
    (ops / "hoh-continue.timer").write_text(TIMER)
    return ops, inst


def test_auto_mode_without_driven_projects_is_supervised_and_absence_passes(tmp_path):
    ops, inst = _with_continue(tmp_path, {})
    rep = _check(ops, inst, expect="auto", root=tmp_path / "empty")
    assert rep.mode == "supervised" and rep.exit_code() == 0


def test_auto_mode_with_a_driven_project_requires_the_continuation_unit(tmp_path):
    ops, inst = _with_continue(tmp_path, {})
    rep = _check(ops, inst, expect="auto", root=_driven_root(tmp_path))
    assert rep.mode == "unattended"
    assert {(f.code, f.unit) for f in rep.failing()} == {
        ("REQUIRED_NOT_INSTALLED", "hoh-continue.service"),
        ("REQUIRED_NOT_INSTALLED", "hoh-continue.timer")}
    # the state-only quota unit is not what unattended continuation needs
    assert all(f.unit.startswith("hoh-continue") for f in rep.failing())


def test_auto_mode_unattended_passes_only_when_installed_canonical_and_healthy(tmp_path):
    ops, inst = _with_continue(tmp_path, {"hoh-continue.service": CONTINUE,
                                          "hoh-continue.timer": TIMER})
    root = _driven_root(tmp_path)
    assert _check(ops, inst, expect="auto", root=root).exit_code() == 0

    def failed(unit):
        return {"ActiveState": "failed", "Result": "exit-code"} if unit.endswith(
            ".service") else _healthy(unit)
    assert "UNIT_FAILED" in _codes(_check(ops, inst, expect="auto", root=root,
                                          show=failed))
    (inst / "hoh-continue.service").write_text(SERVICE)      # the state-only command
    assert "MATERIAL_DRIFT" in _codes(_check(ops, inst, expect="auto", root=root))


def test_the_repository_ships_both_units_and_the_continuation_one_drives():
    ops = ROOT / "ops"
    text = (ops / "hoh-continue.service").read_text()
    assert ud.exec_starts(text) == ["%h/hoh/plugin/bin/hoh resume-quota --drive --recover"]
    assert (ops / "hoh-continue.timer").is_file()
    assert "--drive" not in (ops / "hoh-resume-quota.service").read_text().split(
        "ExecStart=")[1].splitlines()[0]
