#!/usr/bin/env python3
"""unit_drift.py -- does the installed automation still match the repository?

#37. The repository owns `ops/hoh-resume-quota.service` and its timer. What
actually runs on a machine is whatever an operator copied into
`~/.config/systemd/user` at some point, and nothing compared the two. The
copy installed on 2026-09-07 still called `hoh fortsetzen` after the verb was
renamed to `resume-quota`; it failed on every tick for eighteen days, across
three reboots, and neither the product nor the readiness board nor eight
review rounds noticed. The failure was loud in the journal and silent
everywhere anybody looked.

This tool is that comparison, and it is **read-only by construction**: it
reads unit files and asks `systemctl --user show` for state. It never writes
to `~/.config/systemd/user`, never enables, starts, reloads or reinstalls a
unit. Installing or activating the operator-level unit is a separate
operational action with its own consequences -- it switches on an automation
-- and a readiness check that did it as a side effect would be exactly the
silent widening this project refuses elsewhere.

What it reports, per finding code:

* `CANONICAL_MISSING` -- a unit the repository is supposed to own is absent
  from `ops/`. Nothing else can be judged against it.
* `NOT_INSTALLED` -- a canonical unit is not installed. A fact, not a
  failure, unless the operating mode requires it (below).
* `REQUIRED_NOT_INSTALLED` -- the operating mode requires unattended
  continuation and the unit that provides it is not installed.

**Operating mode.** Not installing any automation is legitimate for a
supervised or manual machine, and a readiness check must not demand it there.
It must not *satisfy* an unattended claim either. So the mode is derived, not
assumed (`--expect auto`, the default): when the HoH root holds a project that
is not closed and registers a driver -- a project somebody set up to be driven
without them -- the mode is `unattended`, and `hoh-continue.{service,timer}`
(the consumer that resumes due waits *and* replaces dead masters) must be
installed, canonical, its timer active and its service not failed. Otherwise
the mode is `supervised` and absence is reported, not failed.
`--expect installed` requires every canonical unit; `--expect any` requires
none.
* `MATERIAL_DRIFT` -- the installed unit's directives differ from the
  canonical ones. Comments, blank lines, `Description=` and `Documentation=`
  are not material; everything else is, since any other line can change what
  runs, when, or with which permissions.
* `OBSOLETE_UNIT` -- an installed `hoh-*` unit the repository does not own at
  all (the renamed `hoh-fortsetzen.*` is the case that motivated this).
* `UNKNOWN_VERB` -- an installed unit's `ExecStart` calls a `hoh` verb the
  current CLI does not have. Checked against the real argument parser, not a
  list typed here, so a rename cannot leave this check behind the way it left
  the unit behind.
* `MISSING_EXECUTABLE` -- the program an installed `ExecStart` names does not
  exist.
* `UNIT_FAILED` / `TIMER_INACTIVE` -- systemd reports the service failed, or
  an installed timer is not active. Only evaluated for units that are
  installed: a failed state of an automation nobody installed is not a thing.

Exit codes: 0 clean, 1 at least one finding that fails under the declared
mode, 2 systemd user state could not be read at all (NOT_RUN, never a pass).

Usage:
    python3 tools/unit_drift.py [--expect auto|installed|any] [--root DIR] [--json]
        [--ops DIR] [--installed DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOH = HERE.parent

#: Directives whose difference does not change what runs, when, or how.
COSMETIC_KEYS = frozenset({"Description", "Documentation"})

#: Unit-name prefix that identifies this project's automation. An installed
#: unit with this prefix that the repository does not own is obsolete.
PREFIX = "hoh-"

FAILING = {"CANONICAL_MISSING", "MATERIAL_DRIFT", "OBSOLETE_UNIT",
           "UNKNOWN_VERB", "MISSING_EXECUTABLE", "UNIT_FAILED",
           "TIMER_INACTIVE", "REQUIRED_NOT_INSTALLED"}

#: The units that provide unattended continuation.
CONTINUATION_UNITS = ("hoh-continue.service", "hoh-continue.timer")


@dataclass
class Finding:
    code: str
    unit: str
    detail: str


@dataclass
class Report:
    expect: str
    canonical: list[str]
    installed: list[str]
    findings: list[Finding] = field(default_factory=list)
    systemd_readable: bool = True
    mode: str = "supervised"
    mode_why: str = ""

    def failing(self) -> list[Finding]:
        return [f for f in self.findings if f.code in FAILING]

    def exit_code(self) -> int:
        if self.failing():
            return 1
        if not self.systemd_readable and self.installed:
            return 2
        return 0


def directives(text: str) -> list[tuple[str, str, str]]:
    """(section, key, value) for every directive, comments and blanks dropped.

    Order is kept: systemd treats repeated keys (several `ExecStart=` in a
    oneshot, `ReadWritePaths=` accumulating) as a sequence, so a reordering
    can be a material change.
    """
    section = ""
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if not sep:
            out.append((section, line, ""))
            continue
        out.append((section, key.strip(), value.strip()))
    return out


def material(text: str) -> list[tuple[str, str, str]]:
    return [d for d in directives(text) if d[1] not in COSMETIC_KEYS]


def exec_starts(text: str) -> list[str]:
    return [v for s, k, v in directives(text) if s == "Service" and k == "ExecStart"]


def cli_verbs() -> set[str]:
    """The verbs the current CLI really has, read from its parser."""
    sys.path.insert(0, str(HOH / "src"))
    from hoh.cli import build_parser  # noqa: PLC0415

    parser = build_parser()
    verbs: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 -- argparse has no public API for this
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            verbs |= set(choices)
    return verbs


def _expand(token: str, home: str) -> str:
    return token.replace("%h", home)


def exec_problems(unit: str, text: str, *, home: str, verbs: set[str],
                  exists: Callable[[str], bool]) -> list[Finding]:
    found: list[Finding] = []
    for line in exec_starts(text):
        # systemd prefixes such as '-' or '@' modify error handling, not the program.
        try:
            argv = shlex.split(line.lstrip("-@:+!"))
        except ValueError as exc:
            found.append(Finding("UNKNOWN_VERB", unit,
                                 f"ExecStart cannot be parsed: {exc}"))
            continue
        if not argv:
            continue
        program = _expand(argv[0], home)
        if not exists(program):
            found.append(Finding("MISSING_EXECUTABLE", unit,
                                 f"ExecStart program does not exist: {program}"))
        if Path(program).name == "hoh":
            verb = next((a for a in argv[1:] if not a.startswith("-")), None)
            if verb is not None and verb not in verbs:
                found.append(Finding(
                    "UNKNOWN_VERB", unit,
                    f"ExecStart calls `hoh {verb}`, which the current CLI "
                    f"does not have"))
    return found


def systemctl_show(unit: str) -> dict[str, str] | None:
    """`systemctl --user show` for one unit, or None when unreadable.

    `show` is a query. Nothing here calls start, enable, daemon-reload or any
    other verb that changes state.
    """
    try:
        p = subprocess.run(
            ["systemctl", "--user", "show", unit, "--no-pager",
             "-p", "LoadState,ActiveState,SubState,Result,UnitFileState"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    props: dict[str, str] = {}
    for line in p.stdout.splitlines():
        k, _, v = line.partition("=")
        props[k] = v
    return props


def operating_mode(root: Path | None) -> tuple[str, str]:
    """("unattended" | "supervised", why), derived from the HoH root."""
    if root is None or not (root / "projects").is_dir():
        return "supervised", f"no projects under {root}"
    sys.path.insert(0, str(HOH / "src"))
    from hoh.projectstore import ProjectStore, list_projects  # noqa: PLC0415

    driven = []
    for pid in list_projects(root):
        store = ProjectStore(root, pid)
        if not (store.dir / "driver.json").is_file():
            continue
        try:
            if store.read_state().rc_closed():
                continue
        except Exception:                                   # noqa: BLE001
            pass                    # an unreadable driven project still needs its driver
        driven.append(pid)
    if driven:
        return "unattended", (f"{len(driven)} open project(s) register a driver: "
                              + ", ".join(driven[:5]))
    return "supervised", "no open project registers a driver"


def check(*, ops: Path, installed: Path, expect: str = "any",
          root: Path | None = None,
          home: str | None = None,
          show: Callable[[str], dict[str, str] | None] = systemctl_show,
          verbs: set[str] | None = None,
          exists: Callable[[str], bool] = os.path.exists) -> Report:
    home = home if home is not None else str(Path.home())
    verbs = verbs if verbs is not None else cli_verbs()
    canonical = sorted(p.name for p in ops.glob(f"{PREFIX}*")
                       if p.suffix in (".service", ".timer"))
    have = sorted(p.name for p in installed.glob(f"{PREFIX}*")
                  if p.suffix in (".service", ".timer")) if installed.is_dir() else []
    rep = Report(expect=expect, canonical=canonical, installed=have)
    if expect == "auto":
        rep.mode, rep.mode_why = operating_mode(root)
    required: set[str] = set()
    if expect == "installed":
        required = set(canonical)
    elif expect == "auto" and rep.mode == "unattended":
        required = set(CONTINUATION_UNITS)

    if not canonical:
        rep.findings.append(Finding(
            "CANONICAL_MISSING", str(ops),
            "the repository owns no hoh-* unit in ops/; installed units "
            "cannot be judged against anything"))

    for name in canonical:
        inst = installed / name
        if not inst.is_file():
            if name in required:
                rep.findings.append(Finding(
                    "REQUIRED_NOT_INSTALLED", name,
                    f"not installed under {installed}, and the operating mode "
                    f"requires it ({rep.mode}: {rep.mode_why or expect})"))
            else:
                rep.findings.append(Finding(
                    "NOT_INSTALLED", name, f"not installed under {installed}"))
            continue
        want = material((ops / name).read_text(encoding="utf-8"))
        got = material(inst.read_text(encoding="utf-8"))
        if want != got:
            only_want = [f"{s}.{k}={v}" for s, k, v in want if (s, k, v) not in got]
            only_got = [f"{s}.{k}={v}" for s, k, v in got if (s, k, v) not in want]
            rep.findings.append(Finding(
                "MATERIAL_DRIFT", name,
                "installed differs from ops/: "
                f"repo-only {only_want[:4]}, installed-only {only_got[:4]}"
                + ("" if only_want or only_got else " (order differs)")))

    for name in have:
        text = (installed / name).read_text(encoding="utf-8")
        if name not in canonical:
            rep.findings.append(Finding(
                "OBSOLETE_UNIT", name,
                "installed hoh-* unit that the repository does not own"))
        rep.findings.extend(exec_problems(name, text, home=home, verbs=verbs,
                                          exists=exists))
        props = show(name)
        if props is None:
            rep.systemd_readable = False
            continue
        if name.endswith(".service") and (
                props.get("ActiveState") == "failed"
                or props.get("Result", "success") not in ("success", "")):
            rep.findings.append(Finding(
                "UNIT_FAILED", name,
                f"systemd: ActiveState={props.get('ActiveState')} "
                f"Result={props.get('Result')}"))
        if name.endswith(".timer") and props.get("ActiveState") != "active":
            rep.findings.append(Finding(
                "TIMER_INACTIVE", name,
                f"systemd: ActiveState={props.get('ActiveState')} "
                f"UnitFileState={props.get('UnitFileState')}"))
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expect", choices=("auto", "installed", "any"), default="auto")
    ap.add_argument("--root", type=Path, default=None,
                    help="HoH root whose projects decide the mode (default: $HOH_RUNS "
                         "or ~/hoh/runs)")
    ap.add_argument("--ops", type=Path, default=HOH / "ops")
    ap.add_argument("--installed", type=Path,
                    default=Path.home() / ".config/systemd/user")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = a.root or Path(os.environ.get("HOH_RUNS", Path.home() / "hoh" / "runs"))
    rep = check(ops=a.ops, installed=a.installed, expect=a.expect, root=root)
    if a.json:
        d = asdict(rep)
        d["failing"] = [asdict(f) for f in rep.failing()]
        print(json.dumps(d, indent=2))
    else:
        if rep.expect == "auto":
            print(f"mode:      {rep.mode} ({rep.mode_why})")
        print(f"canonical: {', '.join(rep.canonical) or '-'}")
        print(f"installed: {', '.join(rep.installed) or '-'}")
        for f in rep.findings:
            mark = "FAIL" if f in rep.failing() else "info"
            print(f"  {mark} {f.code:<18} {f.unit}: {f.detail}")
        if not rep.systemd_readable:
            print("  systemd user state could not be read for some unit")
        print("clean" if rep.exit_code() == 0 else
              "DRIFT" if rep.exit_code() == 1 else "NOT_RUN")
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
