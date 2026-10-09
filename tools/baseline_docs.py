#!/usr/bin/env python3
"""The three P0 baselines that are summaries of measurements already taken.

V3.3 P0 asks for a current baseline, a product baseline and a contract trace.
None of them is a new measurement: the capability matrix, the readiness board,
the export manifest and the CLI's own parser already know the answers. So
these are renderings, and the only thing that can go wrong is a rendering that
says more than its source.

Two rules hold that line:

* **Every figure names where it came from.** A number without a source is not
  written; the line says `NOT_MEASURED` and why instead.
* **`NOT_MEASURED` is used, often.** "Time to first verified result" cannot be
  read out of this repository: it depends on a provider, a machine and a
  person. A product baseline that guessed it would be the most quoted number
  in the document and the only invented one.

Usage:
    python3 tools/baseline_docs.py --out-dir program/v3_3
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

NOT_MEASURED = "NOT_MEASURED"

CURRENT_BASELINE = "VERIHARNESS_CURRENT_BASELINE.md"

#: The five `measurement` items of phase P0 and the heading each one is
#: written under in the current baseline (a test pins the keys against the
#: plan's own inventory).
MEASUREMENT_SECTIONS = {
    "P0.measure.fresh-install-path": "Fresh install path",
    "P0.measure.current-cli-commands": "Current CLI commands",
    "P0.measure.current-service-runtime-model":
        "Current service and runtime model",
    "P0.measure.current-human-intervention-points":
        "Current human intervention points",
    "P0.measure.existing-long-horizon-dogfood-evidence":
        "Existing long-horizon dogfood evidence",
}

#: Matched against upper-cased *names*, never against prose.
HUMAN_MARKERS = ("HUMAN", "WAITING_FOR_DECISION", "OPERATOR", "CAPTAIN",
                 "NEEDS_A_HUMAN")

#: `path:line` inside a backticked source. No extension is required:
#: `Makefile:1` is a cite like any other.
_CITE = re.compile(r"(?<![\w./@\-])[\w./@\-]+:\d+")
_PROC = ("subprocess.run", "subprocess.Popen", "subprocess.check_output",
         "subprocess.check_call")
_LOCK = ("fcntl.flock", "fcntl.lockf")
_RESIDENT = ("socket.socket", "socketserver", "http.server", "asyncio.run",
             "os.fork", "multiprocessing.Process")

#: Seconds a `python3 -m hoh.cli ... --help` child may take.
CLI_TIMEOUT = 60
#: A `{a,b,...}` list, as argparse prints the subparsers' metavar.
_BRACES = re.compile(r"\{([\w,\-]+)\}")
_POSITIONAL_BRACES = re.compile(r"\s+\{([\w,\-]+)\}\s*")


def _matrix():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix", HERE / "capability_matrix.py")
    cm = importlib.util.module_from_spec(spec)
    sys.modules["capability_matrix"] = cm
    spec.loader.exec_module(cm)
    return cm


def _subcommands(help_text: str) -> list[str] | None:
    """The subcommands a `--help` text announces, or None.

    Only the subparsers positional counts: the line of the `positional
    arguments:` block that is exactly an indented `{a,b,...}`. A brace list
    in the usage line (`[--isolation {none,strict}]`), in an `options:` entry
    or in a description is a list of *choices*, never of subcommands. A text
    without a `positional arguments:` line (a stub) is read from its usage
    paragraph, with every `[...]` group removed first.
    """
    lines = help_text.splitlines()
    if any(z.strip() == "positional arguments:" for z in lines):
        start = next(i for i, z in enumerate(lines)
                     if z.strip() == "positional arguments:")
        for z in lines[start + 1:]:
            if z.strip() and not z[:1].isspace():
                break
            m = _POSITIONAL_BRACES.fullmatch(z)
            if m:
                return sorted(m.group(1).split(","))
        return None
    usage = []
    for z in lines:
        if not z.strip():
            break
        usage.append(z)
    flat = "\n".join(usage)
    while True:
        shorter = re.sub(r"\[[^\[\]]*\]", "", flat)
        if shorter == flat:
            break
        flat = shorter
    m = _BRACES.search(flat)
    return sorted(m.group(1).split(",")) if m else None


def cli_surface() -> dict:
    """The CLI's commands, read from the parser rather than from the README.

    A command list in a document is a claim about software; a command list
    from `argparse` is the software. Every top-level command is then asked for
    its own `--help`: a command whose help does not answer is *not measured*
    (its subcommands are unknown, which is not the same as none).
    """
    # HOME: under the STRICT sandbox there is neither a HOME nor a passwd
    # entry, and `hoh.runner` fails on `Path.home()` at import -- the command
    # list then read NOT_MEASURED for a reason that had nothing to do with the CLI.
    env = {"PYTHONPATH": "src", "HERDR_ENV": "1",
           # O241: without this the child writes src/hoh/__pycache__
           # into whatever tree is measured -- a protected arena
           # included, which stopped run 20261001t1255z-sdurw2.
           "PYTHONDONTWRITEBYTECODE": "1",
           "HOME": os.environ.get("HOME") or str(HOH),
           "PATH": os.environ.get("PATH", "")}

    def ask(*args):
        return subprocess.run([sys.executable, "-m", "hoh.cli", *args],
                              cwd=str(HOH), capture_output=True, text=True,
                              env=env, timeout=CLI_TIMEOUT)

    try:
        p = ask("--help")
    except subprocess.TimeoutExpired:
        return {"commands": [], "source": NOT_MEASURED, "groups": {},
                "group_failures": {},
                "why": f"the CLI did not answer --help: timed out after "
                       f"{CLI_TIMEOUT} s"}
    except OSError as e:
        return {"commands": [], "source": NOT_MEASURED, "groups": {},
                "group_failures": {},
                "why": f"the CLI did not answer --help: {type(e).__name__}"}
    if p.returncode != 0:
        return {"commands": [], "source": NOT_MEASURED, "groups": {},
                "group_failures": {},
                "why": f"the CLI did not answer --help: {p.stderr.strip()[:90]}"}
    commands = _subcommands(p.stdout) or []
    groups: dict[str, list[str]] = {}
    failures: dict[str, str] = {}
    for command in commands:
        try:
            q = ask(command, "--help")
        except subprocess.TimeoutExpired:
            failures[command] = (f"`{command} --help` timed out after "
                                 f"{CLI_TIMEOUT} s")
            continue
        except OSError as e:
            failures[command] = f"`{command} --help` failed: {type(e).__name__}"
            continue
        if q.returncode != 0:
            failures[command] = f"`{command} --help` exited {q.returncode}"
            continue
        subs = _subcommands(q.stdout)
        if subs is not None:
            groups[command] = subs
    return {"commands": commands, "source": "python3 -m hoh.cli --help",
            "groups": groups, "group_failures": failures}


def package_shape() -> dict:
    modules, classes, functions = [], 0, 0
    for f in sorted((HOH / "src/hoh").glob("*.py")):
        modules.append(f.name)
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                classes += 1
            elif isinstance(n, ast.FunctionDef):
                functions += 1
    return {"modules": modules, "classes": classes, "functions": functions,
            "source": "ast over src/hoh/*.py"}


def board_rows() -> dict:
    doc = HOH / "docs/READINESS.md"
    if not doc.is_file():
        return {"rows": {}, "source": NOT_MEASURED, "why": "no readiness board"}
    rows = {}
    for line in doc.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([a-z_0-9]+)` \| ([A-Z_ ()a-z]+) \|", line)
        if m:
            rows[m.group(1)] = m.group(2).strip()
    verdict = re.search(r"TECHNICALLY_STABLE_READY = (\w+)",
                        doc.read_text(encoding="utf-8"))
    return {"rows": rows, "verdict": verdict.group(1) if verdict else None,
            "source": "docs/READINESS.md"}


def manifest_shape() -> dict:
    p = HOH / "EXPORT_MANIFEST.json"
    if not p.is_file():
        return {"source": NOT_MEASURED, "why": "no export manifest"}
    entries = json.loads(p.read_text())["entries"]
    include = [e for e in entries if e["decision"] == "INCLUDE"]
    return {"entries": len(entries), "included": len(include),
            "python_included": len([e for e in include
                                    if e["path"].endswith(".py")]),
            "source": "EXPORT_MANIFEST.json"}


def configuration_decisions() -> dict:
    """Environment variables the tools and the package read.

    The plan asks how many configuration decisions an operator faces. This is
    the answerable part of it: every variable something here reads is a
    decision somebody can be asked to make.
    """
    names: dict[str, list[str]] = {}
    for base in ("src/hoh", "tools"):
        for f in sorted((HOH / base).glob("*.py")):
            rel = str(f.relative_to(HOH))
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:                    # pragma: no cover
                continue
            # Module-level `NAME = "SOME_VAR"`, because a variable read
            # through a constant is still a variable. A regular expression
            # over the source saw only the literal spelling and missed
            # `VERIHARNESS_EXPORT_CHECKOUT`, which is exactly the shape of
            # under-counting this document exists to avoid.
            constants = {
                t.id: n.value.value
                for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            }

            def _name(node) -> str | None:
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value
                if isinstance(node, ast.Name):
                    return constants.get(node.id)
                return None

            for n in ast.walk(tree):
                target = None
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("get", "setdefault")
                        and isinstance(n.func.value, ast.Attribute)
                        and n.func.value.attr == "environ" and n.args):
                    target = _name(n.args[0])
                elif (isinstance(n, ast.Subscript)
                      and isinstance(n.value, ast.Attribute)
                      and n.value.attr == "environ"):
                    target = _name(n.slice)
                if target and re.fullmatch(r"[A-Z][A-Z_0-9]*", target):
                    names.setdefault(target, []).append(rel)
    return {"variables": {k: sorted(set(v)) for k, v in sorted(names.items())},
            "count": len(names),
            "source": "os.environ reads in src/ and tools/, resolved through "
                      "module-level constants"}


def _lines(root: Path, rel: str) -> list[str] | None:
    """The lines of `root/rel`, or None if it cannot be read."""
    try:
        return (root / rel).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None


def cite(root: Path, rel: str, needle: str) -> str | None:
    """`rel:line` of the first line of `root/rel` containing `needle`.

    None if the file or the needle is missing: a row whose citation is None
    is not written, because a figure without a line is a figure without a source.
    """
    lines = _lines(root, rel)
    if lines is None or not needle:
        return None
    for n, line in enumerate(lines, 1):
        if needle in line:
            return f"{rel}:{n}"
    return None


def _not_measured(why: str) -> dict:
    return {"source": NOT_MEASURED, "why": why, "rows": []}


def _toml(root: Path, rel: str) -> dict | None:
    try:
        data = tomllib.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):               # TOMLDecodeError is a ValueError
        return None
    return data if isinstance(data, dict) else None


def _json(root: Path, rel: str):
    try:
        return json.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _toml_scripts(data: dict | None) -> dict[str, str]:
    project = (data or {}).get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(v, str)}


def _py_files(root: Path) -> tuple[list[tuple[str, ast.Module]], list[str]]:
    """Every `src/hoh/*.py` that parses, and the ones that do not."""
    parsed: list[tuple[str, ast.Module]] = []
    failed: list[str] = []
    try:
        files = sorted((root / "src/hoh").glob("*.py"))
    except OSError:
        files = []
    for f in files:
        rel = f"src/hoh/{f.name}"
        try:
            parsed.append((rel, ast.parse(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError, SyntaxError, RecursionError):
            failed.append(rel)
    return parsed, failed


def _dotted(node) -> str | None:
    """`a.b.c` for a chain of attributes ending in a name; None otherwise."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _aliases(tree: ast.Module) -> dict[str, str]:
    """What an imported name stands for: `import x as y` -> y: x,
    `from x import y as z` -> z: x.y, `import a.b` -> a: a."""
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    out[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and (n.module or n.level):
            # a relative import is this package's module, never the standard
            # library's: its dotted name starts with `.` and matches no target
            base = "." * n.level + (n.module or "")
            for a in n.names:
                if a.name != "*":
                    out[a.asname or a.name] = (f"{base}.{a.name}" if n.module
                                               else f"{base}{a.name}")
    return out


def _is(name: str, target: str) -> bool:
    return name == target or name.startswith(target + ".")


def _refs(tree: ast.Module, targets) -> list[tuple[int, str, str]]:
    """Every maximal dotted name that resolves, through the module's imports,
    to one of `targets` (or to a name below it). A name bound by assignment
    (`s = socket.socket; s()`) is *not* followed."""
    aliases = _aliases(tree)
    inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    out = []
    for n in ast.walk(tree):
        if (not isinstance(n, (ast.Attribute, ast.Name)) or id(n) in inner
                or not isinstance(n.ctx, ast.Load)):
            continue
        d = _dotted(n)
        if d is None:
            continue
        head, _, rest = d.partition(".")
        full = aliases.get(head, head) + ("." + rest if rest else "")
        for t in targets:
            if _is(full, t):
                out.append((n.lineno, full, t))
                break
    return out


def _member_names(cls: ast.ClassDef):
    """(name, line) of every member assigned in a class body."""
    for st in cls.body:
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    yield t.id, st.lineno
        elif isinstance(st, ast.AnnAssign) and st.value is not None \
                and isinstance(st.target, ast.Name):
            yield st.target.id, st.lineno


def _day(text: str) -> date | None:
    """A calendar date, or None if `text` is not one (`2026-02-30`)."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _section(lines: list[str], title: str) -> list[str] | None:
    """The lines after `## <title>` up to the next line that starts with `#`
    (any level) -- the only end rule. None if there is no such heading."""
    try:
        start = lines.index(f"## {title}")
    except ValueError:
        return None
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        body.append(line)
    return body


# --- 1. fresh install path ---------------------------------------------------

_PIP = re.compile(r"^(python3? -m pip|pip) install\b")
_STEP = re.compile(r"^\s+(\d)\. (.+)$")


def fresh_install_path(root: Path = HOH) -> dict:
    """What a fresh install reads, derived offline from the files that say it.

    No subprocess, no network, no install, no build. The recorded clean-install
    evidence is *read*, not re-run.
    """
    data = _toml(root, "pyproject.toml")
    if data is None:
        return _not_measured("pyproject.toml is missing or does not parse")
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    rows: list[tuple[str, str]] = []
    limits: list[tuple[str, str]] = []

    def add(text: str, rel: str, needle: str) -> bool:
        src = cite(root, rel, needle)
        if src:
            rows.append((text, src))
        return bool(src)

    if isinstance(project.get("requires-python"), str):
        add(f"python {project['requires-python']}", "pyproject.toml",
            "requires-python")
    deps = project.get("dependencies")
    if isinstance(deps, list):
        add("runtime dependencies: "
            + (", ".join(str(d) for d in deps) or "none"),
            "pyproject.toml", "dependencies = ")
    extras = project.get("optional-dependencies")
    for name in sorted(extras if isinstance(extras, dict) else {}):
        reqs = extras[name]
        if isinstance(reqs, list):
            add(f"optional extra `{name}`: " + ", ".join(str(r) for r in reqs),
                "pyproject.toml", f"{name} = [")
    scripts = _toml_scripts(data)
    for name in sorted(scripts):
        add(f"console script `{name}` -> `{scripts[name]}`", "pyproject.toml",
            f"{name} = ")
    backend = (data.get("build-system") or {}).get("build-backend") \
        if isinstance(data.get("build-system"), dict) else None
    if isinstance(backend, str):
        add(f"build backend: {backend}", "pyproject.toml", "build-backend")

    pip_found = False
    for rel in ("README.md", "docs/OPERATIONS.md"):
        for n, line in enumerate(_lines(root, rel) or [], 1):
            if _PIP.match(line.strip()):
                rows.append((f"documented command `{line.strip()}`",
                             f"{rel}:{n}"))
                pip_found = True
    if not pip_found:
        limits.append(("no `pip install` command was found in README.md or "
                       "docs/OPERATIONS.md", "README.md; docs/OPERATIONS.md"))

    step_found = False
    for n, line in enumerate((_lines(root, "tools/clean_install_check.py")
                              or [])[:40], 1):
        m = _STEP.match(line)
        if m:
            rows.append((f"clean-install check step {m.group(1)}: "
                         f"{m.group(2).strip()}",
                         f"tools/clean_install_check.py:{n}"))
            step_found = True
    if not step_found:
        limits.append(("no numbered step was found in the clean-install check",
                       "tools/clean_install_check.py"))

    recorded: list[tuple[str, str]] = []
    ci = _json(root, "dogfood/external-ci/EXACT_HEAD_CI.json")
    if isinstance(ci, dict) and isinstance(ci.get("jobs"), list):
        for job in ci["jobs"]:
            if (isinstance(job, dict) and isinstance(job.get("name"), str)
                    and "clean install" in job["name"].lower()):
                at = cite(root, "dogfood/external-ci/EXACT_HEAD_CI.json",
                          json.dumps(job["name"]))
                if (at and isinstance(job.get("conclusion"), str)
                        and ci.get("run_id") not in (None, "")
                        and isinstance(ci.get("measured_at"), str)):
                    recorded.append((f"CI job `{job['name']}`: "
                                     f"{job['conclusion']}, run {ci['run_id']}, "
                                     f"recorded {ci['measured_at']}", at))
                break
    rel_receipt = ".github/releases/v0.1.0.json"
    rc = _json(root, rel_receipt)
    if isinstance(rc, dict):
        files = rc.get("files")
        published = (files[0].get("published_at")
                     if isinstance(files, list) and files
                     and isinstance(files[0], dict) else None)
        at = cite(root, rel_receipt, '"production_pypi_result"')
        if (at and isinstance(rc.get("testpypi_result"), str)
                and isinstance(rc.get("production_pypi_result"), str)
                and isinstance(published, str)):
            recorded.append((f"v0.1.0 receipt: TestPyPI {rc['testpypi_result']}"
                             f", PyPI {rc['production_pypi_result']}, "
                             f"published {published[:10]}", at))
    if not recorded:
        limits.append(("no recorded clean-install evidence was found",
                       "dogfood/external-ci/EXACT_HEAD_CI.json; "
                       ".github/releases/v0.1.0.json"))
    if not rows:
        return _not_measured("no figure of the install path could be cited "
                             "from pyproject.toml")
    return {"rows": rows, "limits": limits, "recorded": recorded,
            "source": "derived offline from the files cited on each line",
            "why": ""}


# --- 2. service and runtime model --------------------------------------------

def runtime_model(root: Path = HOH) -> dict:
    """The runtime model as `ops/`, `plugin/` and `src/hoh` state it.

    Printed means read: a row says "runs `X`" because a line of the source
    says so, and says nothing about whether a process stays resident unless a
    cited line says it.
    """
    parsed, failed = _py_files(root)
    if not parsed:
        return _not_measured("no module of src/hoh could be read and parsed")
    trees = dict(parsed)
    rows: list[tuple[str, str]] = []
    limits: list[tuple[str, str]] = []

    # 1. entry points
    for name, target in sorted(_toml_scripts(_toml(root, "pyproject.toml")).items()):
        src = cite(root, "pyproject.toml", f"{name} = ")
        if src:
            rows.append((f"entry point `{name}` -> `{target}`", src))

    # 2. Herdr plugin actions, each with the command it carries
    plugin = "plugin/herdr-plugin.toml"
    pdata = _toml(root, plugin)
    if pdata is None:
        limits.append((f"{plugin} is missing or does not parse", plugin))
    else:
        actions = pdata.get("actions")
        for act in actions if isinstance(actions, list) else []:
            if not (isinstance(act, dict) and isinstance(act.get("id"), str)
                    and isinstance(act.get("command"), list)):
                continue
            src = cite(root, plugin, f'id = "{act["id"]}"')
            if src:
                rows.append((f"Herdr plugin action `{act['id']}` runs `"
                             + " ".join(str(c) for c in act["command"]) + "`",
                             src))

    # 3. systemd timers
    timers = []
    try:
        timers = sorted((root / "ops").glob("*.timer"))
    except OSError:
        pass
    if not timers:
        limits.append(("no systemd timer was found", "ops"))
    for t in timers:
        stem = t.stem
        tl = _lines(root, f"ops/{stem}.timer") or []
        every = next(((i, z.split("=", 1)[1].strip()) for i, z in enumerate(tl, 1)
                      if z.startswith("OnUnitActiveSec=")), None)
        # the service a timer starts is the unit it names, else its namesake
        unit = f"{stem}.service"
        for z in tl:
            if z.startswith("Unit="):
                unit = z.split("=", 1)[1].strip()
                break
        if "/" in unit or "\\" in unit or not unit.endswith(".service"):
            limits.append((f"systemd timer `{stem}` names a unit `{unit}` that "
                           "is not a service file of ops/",
                           f"ops/{stem}.timer"))
            continue
        if every is None:
            limits.append((f"systemd timer `{stem}` has no OnUnitActiveSec "
                           "line", f"ops/{stem}.timer"))
            continue
        sl = _lines(root, f"ops/{unit}")
        start = next(((i, z.split("=", 1)[1].strip())
                      for i, z in enumerate(sl or [], 1)
                      if z.startswith("ExecStart=")), None)
        if start is None:
            limits.append((f"systemd timer `{stem}` has no service with an "
                           "ExecStart line", f"ops/{unit}"))
            continue
        text = (f"systemd timer `{stem}` every {every[1]} runs `{start[1]}`")
        src = f"ops/{stem}.timer:{every[0]}; ops/{unit}:{start[0]}"
        type_line = next(((i, z.strip()) for i, z in enumerate(sl or [], 1)
                    if z.startswith("Type=")), None)
        if type_line:
            text += f" (`{type_line[1]}`)"
            src += f"; ops/{unit}:{type_line[0]}"
        rows.append((text, src))

    # 4. roles
    tree = trees.get("src/hoh/contracts.py")
    for st in (tree.body if tree else []):
        if isinstance(st, ast.ClassDef) and st.name == "Role":
            values = [v.value.value for v in st.body
                      if isinstance(v, ast.Assign)
                      and isinstance(v.value, ast.Constant)
                      and isinstance(v.value.value, str)]
            if values:
                rows.append((f"roles: {', '.join(values)}",
                             f"src/hoh/contracts.py:{st.lineno}"))
            break

    # 5. dispatchers
    tree = trees.get("src/hoh/dispatchers.py")
    if tree:
        for st in tree.body:
            if isinstance(st, ast.ClassDef) and st.name.endswith("Dispatcher"):
                rows.append((f"role dispatcher `{st.name}`",
                             f"src/hoh/dispatchers.py:{st.lineno}"))
        for st in tree.body:
            if isinstance(st, ast.FunctionDef) and st.name == "build_dispatcher":
                rows.append(("dispatcher chosen by `build_dispatcher`",
                             f"src/hoh/dispatchers.py:{st.lineno}"))

    # 6. threads
    threads = []
    for rel, tree in parsed:
        aliases = _aliases(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            d = _dotted(n.func)
            if d is None:
                continue
            head, _, rest = d.partition(".")
            if aliases.get(head, head) + ("." + rest if rest else "") \
                    != "threading.Thread":
                continue
            kind, label = "thread", ""
            for kw in n.keywords:
                if kw.arg == "daemon":
                    if not isinstance(kw.value, ast.Constant):
                        kind = "thread (daemon argument is not a literal)"
                    elif kw.value.value is True:
                        kind = "daemon thread"
                elif (kw.arg == "name" and isinstance(kw.value, ast.Constant)
                      and isinstance(kw.value.value, str)):
                    label = f" `{kw.value.value}`"
            threads.append((rel, n.lineno,
                            f"{kind}{label} inside the calling process"))
    for rel, line, text in sorted(threads):
        rows.append((text, f"{rel}:{line}"))

    if not rows:
        return _not_measured("no entry point, role, dispatcher, timer, thread "
                             "or plugin action was found")

    # 7-10: counts, not core evidence
    rows.append((f"modules parsed: {len(parsed)}", "ast over src/hoh/*.py"))
    if failed:
        limits.append((f"{len(failed)} module(s) of src/hoh could not be "
                       "parsed and are not counted", "ast over src/hoh/*.py"))

    def count(targets):
        hits = sorted((rel, line) for rel, tree in parsed
                      for line, _, _ in _refs(tree, targets))
        return hits, len({rel for rel, _ in hits})

    hits, mods = count(_PROC)
    text = ("child-process references (`subprocess.run|Popen|check_output|"
            f"check_call`, called or passed on): {len(hits)} in {mods} module(s)")
    if hits:
        rows.append((f"{text} (first: `{hits[0][0]}`)",
                     f"{hits[0][0]}:{hits[0][1]}"))
    else:
        rows.append((text, "ast over src/hoh/*.py"))

    hits, mods = count(_LOCK)
    text = f"file-lock references: {len(hits)} in {mods} module(s)"
    rows.append((text, f"{hits[0][0]}:{hits[0][1]}" if hits
                 else "ast over src/hoh/*.py"))

    hits, _ = count(_RESIDENT)
    rows.append(("resident listener or fork references (`socket.socket`, "
                 "`socketserver`, `http.server`, `asyncio.run`, `os.fork`, "
                 "`multiprocessing.Process`; a name or a dotted name below it, "
                 "imports followed, relative imports never match; names "
                 "bound by assignment and calls on a call result such as "
                 "`get_context().Process` are not followed): "
                 f"{len(hits)}",
                 f"{hits[0][0]}:{hits[0][1]}" if hits
                 else "ast over src/hoh/*.py"))
    return {"rows": rows, "limits": limits,
            "source": "ops/, plugin/ and ast over src/hoh/*.py", "why": ""}


# --- 3. human intervention points --------------------------------------------

_HELP = re.compile(r"\b(captain|operator|human|who decides)\b", re.IGNORECASE)


def _marked(name: str) -> bool:
    up = name.upper()
    return any(m in up for m in HUMAN_MARKERS)


def human_intervention_points(root: Path = HOH) -> dict:
    """Where a human can be asked, as the code names it. A marker search over
    *names*, not a classification."""
    parsed, failed = _py_files(root)
    if not parsed:
        return _not_measured("no module of src/hoh could be read and parsed")
    found: set[tuple[str, int, str]] = set()
    for rel, tree in parsed:
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for name, line in _member_names(n):
                    if _marked(name):
                        found.add((rel, line, f"`{n.name}.{name}`"))
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda)):
                a = n.args
                for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                            *([a.vararg] if a.vararg else []),
                            *([a.kwarg] if a.kwarg else [])]:
                    if "human" in arg.arg.lower():
                        found.add((rel, arg.lineno, f"parameter `{arg.arg}`"))
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("add_parser", "add_argument")):
                lits = [x.value for x in n.args[:1]
                        if isinstance(x, ast.Constant) and isinstance(x.value, str)]
                if not lits:
                    continue
                if n.func.attr == "add_parser":
                    if "unblock" in lits[0] or "approve" in lits[0]:
                        found.add((rel, n.args[0].lineno,
                                   f"CLI subcommand `{lits[0]}`"))
                else:
                    for kw in n.keywords:
                        if (kw.arg == "help" and isinstance(kw.value, ast.Constant)
                                and isinstance(kw.value.value, str)
                                and _HELP.search(kw.value.value)):
                            found.add((rel, n.args[0].lineno,
                                       f"CLI argument `{lits[0]}`: "
                                       f"{kw.value.value[:70]}"))
        for st in tree.body:
            targets = []
            if isinstance(st, ast.Assign):
                targets = [t for t in st.targets if isinstance(t, ast.Name)]
            elif isinstance(st, ast.AnnAssign) and st.value is not None \
                    and isinstance(st.target, ast.Name):
                targets = [st.target]
            for t in targets:
                if t.id.startswith("_") or not _marked(t.id):
                    continue
                attrs = [a.attr for a in sorted(
                    (x for x in ast.walk(st.value) if isinstance(x, ast.Attribute)),
                    key=lambda x: (x.lineno, x.col_offset))]
                found.add((rel, st.lineno, f"`{t.id}`"
                           + (" = {" + ", ".join(attrs) + "}" if attrs else "")))
    if not found:
        return _not_measured("no name, parameter or CLI definition carrying a "
                             "human marker was found")
    rows = [(text, f"{rel}:{line}") for rel, line, text in sorted(found)]
    rows.append((f"modules searched: {len(parsed)}", "ast over src/hoh/*.py"))
    limits = []
    if failed:
        limits.append((f"{len(failed)} module(s) of src/hoh could not be parsed "
                       "and are not searched", "ast over src/hoh/*.py"))
    return {"rows": rows, "limits": limits,
            "source": "ast over src/hoh/*.py", "why": ""}


# --- 4. dogfood evidence ------------------------------------------------------

_STAMP = re.compile(r"^\d{8}T\d{4}Z$")
_ISO = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_ITER = re.compile(r"-i(\d+)-a\d+\.json$")


def _stamp_day(name: str) -> date | None:
    try:
        return datetime.strptime(name, "%Y%m%dT%H%MZ").date()
    except ValueError:
        return None


def _ledger_headings(lines: list[str]) -> list[tuple[int, str]]:
    """(line, text) of every `#` heading outside a fenced code block."""
    out, fenced = [], False
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            out.append((n, line))
    return out


def dogfood_evidence(root: Path = HOH) -> dict:
    """What the tree itself says about the long-horizon runs. No git, no
    `runs/`: only what a clone holds."""
    base = root / "dogfood"
    if not base.is_dir():
        return _not_measured("there is no dogfood/ directory in this tree")
    ledger = _lines(root, "DOGFOOD_LEDGER.md")
    if ledger is None:
        return _not_measured("DOGFOOD_LEDGER.md is not readable")
    runs: list[tuple[str, str, Path]] = []
    skipped = 0
    links = 0
    try:
        strands = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        strands = []
    for strand in strands:
        if strand.is_symlink():
            links += 1
            continue
        try:
            names = sorted(p for p in strand.iterdir() if p.is_dir())
        except OSError:
            continue
        for r in names:
            if r.is_symlink():
                links += 1
                continue
            if not _STAMP.match(r.name):
                continue
            if _stamp_day(r.name) is None:
                skipped += 1
            else:
                runs.append((strand.name, r.name, r))
    if not runs:
        return _not_measured("no run directory dogfood/<strand>/<stamp> with "
                             "a valid date was found")
    rows: list[tuple[str, str]] = []
    limits: list[tuple[str, str]] = []

    evidence = results = 0
    depth: list[tuple[int, str]] = []
    for strand, stamp, path in runs:
        res = path / "run-evidence" / "results"
        if (path / "run-evidence").is_dir():
            evidence += 1
            try:
                files = sorted(res.glob("*.json")) if res.is_dir() else []
            except OSError:
                files = []
            results += len(files)
            its = {m.group(1) for f in files if (m := _ITER.search(f.name))}
            depth.append((len(its), f"{strand}/{stamp}"))
    rows.append((f"{len(runs)} run directories in "
                 f"{len({s for s, _, _ in runs})} strand(s); {evidence} with "
                 f"run-evidence; {results} role-result files",
                 "dogfood/<strand>/<UTC stamp>/run-evidence/results/*.json"))

    first = min(runs, key=lambda r: (r[1], r[0]))
    last = max(runs, key=lambda r: (r[1], r[0]))
    span = (_stamp_day(last[1]) - _stamp_day(first[1])).days
    rows.append((f"stamps {first[1]} (`{first[0]}`) to {last[1]} (`{last[0]}`): "
                 f"{span} calendar day(s) between first and last",
                 "directory names under dogfood/"))

    dated, bad = [], 0
    for n, line in _ledger_headings(ledger):
        days = [(_day(m), m) for m in _ISO.findall(line)]
        if any(d is None for d, _ in days):
            bad += 1
        else:
            dated += [(d, n) for d, _ in days]
    if dated:
        lo = min(dated, key=lambda x: (x[0], x[1]))
        hi = max(dated, key=lambda x: (x[0], -x[1]))
        rows.append((f"ledger headings are dated {lo[0].isoformat()} to "
                     f"{hi[0].isoformat()}: {(hi[0] - lo[0]).days} calendar "
                     "day(s)",
                     f"DOGFOOD_LEDGER.md:{lo[1]}; DOGFOOD_LEDGER.md:{hi[1]}"))
    else:
        limits.append(("no ledger heading carries a valid date",
                       "DOGFOOD_LEDGER.md"))
    if bad:
        limits.append((f"{bad} ledger heading(s) with an impossible date are "
                       "not counted", "DOGFOOD_LEDGER.md"))

    if depth:
        top = depth[0]
        for d in depth[1:]:
            if d[0] > top[0]:
                top = d
        rows.append((f"deepest run: {top[0]} iteration(s) (`{top[1]}`); "
                     f"{sum(1 for d in depth if d[0] >= 2)} run(s) with two or "
                     "more",
                     "dogfood/<strand>/<UTC stamp>/run-evidence/results/"
                     "*-i<N>-a<M>.json"))

    entries = [(n, int(m.group(1))) for n, line in _ledger_headings(ledger)
               if (m := re.match(r"^#{2,3} O(\d+) ", line))]
    if entries:
        nums = [k for _, k in entries]
        rows.append((f"{len(entries)} ledger entries O{min(nums)} to "
                     f"O{max(nums)}",
                     f"DOGFOOD_LEDGER.md:{entries[0][0]} to "
                     f"DOGFOOD_LEDGER.md:{entries[-1][0]}"))
    else:
        limits.append(("no ledger entry heading of the form `## O<n> ` was "
                       "found", "DOGFOOD_LEDGER.md"))

    attribution = _json(root, "dogfood/ATTRIBUTION.json")
    items = attribution.get("entries") if isinstance(attribution, dict) else None
    if (isinstance(items, list) and items
            and all(isinstance(e, dict) and isinstance(e.get("category"), str)
                    for e in items)):
        cats: dict[str, int] = {}
        for e in items:
            cats[e["category"]] = cats.get(e["category"], 0) + 1
        dev = sum(1 for e in items if e.get("is_development_node") is True)
        rows.append((f"{len(items)} attribution entries, {dev} development "
                     "node(s): " + ", ".join(f"{k} {cats[k]}"
                                              for k in sorted(cats)),
                     "dogfood/ATTRIBUTION.json"))
    else:
        limits.append(("dogfood/ATTRIBUTION.json is missing or malformed and "
                       "is not counted", "dogfood/ATTRIBUTION.json"))

    limits.append(("run directories before the first stamp and `runs/` are "
                   "not in a clone", "dogfood/"))
    if links:
        limits.append((f"{links} symbolic link(s) to a directory under "
                       "dogfood/ are not followed", "dogfood/"))
    if skipped:
        limits.append((f"{skipped} run director(ies) with an impossible date "
                       "are not counted", "directory names under dogfood/"))
    return {"rows": rows, "limits": limits,
            "source": "the dogfood/ tree and DOGFOOD_LEDGER.md", "why": ""}


# --- rendering and read-back --------------------------------------------------

_HUMAN_NOTE = ("A hit is an anchor, not a classification: whether a wait is "
               "required or only possible is not decided here.")


def _fig(text: str, source: str) -> str:
    return f"* {text} — `{source}`"


def _lim(text: str, source: str) -> str:
    return f"* limit: {text} — `{source}`"


def _render(result: dict, intro: str | None = None) -> list[str]:
    if result.get("source") == NOT_MEASURED:
        return [f"* {NOT_MEASURED} — {result.get('why') or 'no reason given'}"]
    out = [intro, ""] if intro else []
    out += [_fig(t, s) for t, s in result.get("rows", [])]
    out += [_lim(t, s) for t, s in result.get("limits", [])]
    return out


def measurement_sections(body: dict) -> list[str]:
    """The five measurements V3.3 P0 asks for, one `##` section each, in the
    table's order. A NOT_MEASURED result is one line saying why."""
    titles = list(MEASUREMENT_SECTIONS.values())
    out = ["# Measurements asked for by V3.3 P0", ""]

    install = body.get("install") or _not_measured("not measured")
    block = _render(install)
    if install.get("source") != NOT_MEASURED and install.get("recorded"):
        block += ["", "Recorded clean-install evidence (not re-run by this "
                  "document):", ""]
        block += [_fig(t, s) for t, s in install["recorded"]]
    cli = body.get("cli") or {}
    if not cli.get("commands") or cli.get("source") == NOT_MEASURED:
        cli_block = [f"* {NOT_MEASURED} — "
                     + (cli.get("why") or "no command list was read from --help")]
    else:
        cmds = cli["commands"]
        top = "python3 -m hoh.cli --help"
        cli_block = [_fig(f"{len(cmds)} top-level commands: {', '.join(cmds)}",
                          cli.get("source") or top)]
        for g, subs in sorted((cli.get("groups") or {}).items()):
            cli_block.append(_fig(f"`{g}` has {len(subs)} subcommand(s): "
                                  f"{', '.join(subs)}",
                                  f"python3 -m hoh.cli {g} --help"))
        for g, why in sorted((cli.get("group_failures") or {}).items()):
            cli_block.append(_lim(f"`{g}` is not measured: {why}",
                                  f"python3 -m hoh.cli {g} --help"))
    blocks = [
        block,
        cli_block,
        _render(body.get("runtime") or _not_measured("not measured")),
        _render(body.get("human") or _not_measured("not measured"), _HUMAN_NOTE),
        _render(body.get("dogfood") or _not_measured("not measured")),
    ]
    for title, lines in zip(titles, blocks, strict=True):
        out += [f"## {title}", ""] + lines + [""]
    return out


_TRAILING_SOURCE = re.compile(r"`([^`]+)`\s*$")


def _inside(root: Path, rel: str) -> bool:
    """Is `root/rel` a location inside `root`? False for an absolute path, for
    a path that resolves elsewhere (`..`, a symbolic link out) and for one that
    cannot be resolved."""
    try:
        if Path(rel).is_absolute():
            return False
        top = root.resolve()
        return (top / rel).resolve().is_relative_to(top)
    except (OSError, ValueError, RuntimeError):
        return False


def section_status(text: str | None, title: str,
                   root: Path = HOH) -> tuple[bool, str]:
    """Does the section `title` of the generated document read back as measured?

    A structural read-back, independent of how the section was written. It
    does not check that a figure's value is right, and it does not say the
    document is fresh: that is the board's `baseline_docs` row.
    """
    if text is None:
        return False, f"{CURRENT_BASELINE} is missing"
    body = _section(text.splitlines(), title)
    if body is None:
        return False, f"no section `{title}`"
    bullets = [z for z in body if z.startswith("* ")]
    if any(z.startswith("* NOT_MEASURED") for z in bullets):
        return False, "the section reports NOT_MEASURED"
    figures = [z for z in bullets if not z.startswith("* limit:")
               and _TRAILING_SOURCE.search(z)]
    if not figures:
        return False, "the section has no figure with a source"
    sizes: dict[str, int | None] = {}
    for z in bullets:
        m = _TRAILING_SOURCE.search(z)
        if not m:
            continue
        for cited in _CITE.findall(m.group(1)):
            path, _, n = cited.rpartition(":")
            if not _inside(root, path):
                return False, f"cited path {path} is outside the repository"
            if path not in sizes:
                found = _lines(root, path)
                sizes[path] = None if found is None else len(found)
            if sizes[path] is None or not 1 <= int(n) <= sizes[path]:
                return False, f"cited line {cited} does not exist"
    return True, f"{len(figures)} sourced figure(s)"


def current_baseline(body: dict) -> str:
    cli, pkg = body["cli"], body["package"]
    counts = body["matrix_counts"]
    out = ["# Current baseline (V3.3 P0)", "",
           "Generated by `python3 tools/baseline_docs.py`. Every figure names",
           "the command or file it was read from. Nothing here is a figure",
           "somebody remembered.", "",
           f"Measured at {body['measured_at_utc']}.", "",
           "## What the package is", "",
           f"* {len(pkg['modules'])} modules, {pkg['classes']} classes, "
           f"{pkg['functions']} module-level functions — `{pkg['source']}`",
           f"* CLI commands: {', '.join('`' + c + '`' for c in cli['commands']) or NOT_MEASURED}"
           f" — `{cli.get('source', NOT_MEASURED)}`",
           f"* export manifest: {body['manifest'].get('entries', NOT_MEASURED)} "
           f"entries, {body['manifest'].get('included', NOT_MEASURED)} published"
           f" — `{body['manifest']['source']}`", "",
           "## The board", "",
           "Not reproduced here. `docs/READINESS.md` is its own generated",
           "document, and embedding its verdict made this one depend on it",
           "while the board checks this one -- a cycle that never settled",
           "(O197).", "",
           "## What the capability matrix says", ""]
    for status in ("PROVEN", "IMPLEMENTED_NOT_PROVEN", "PARTIAL", "PREPARED",
                   "MISSING", "NOT_DETERMINABLE"):
        out.append(f"* {status}: {counts.get(status, 0)} — "
                   f"`{body['matrix_source']}`")
    out.append("")
    out += measurement_sections(body)
    return "\n".join(out)


def product_baseline(body: dict) -> str:
    cfg = body["config"]
    out = ["# Product baseline (V3.3 P0)", "",
           "Generated by `python3 tools/baseline_docs.py`. The plan asks for",
           "installation steps, time to first run, time to first verified",
           "result, manual interventions, documentation touchpoints and the",
           "number of configuration decisions. Three of those are properties",
           "of a person and a machine rather than of this repository, and they",
           "are marked `NOT_MEASURED` with the reason rather than estimated.",
           "", f"Measured at {body['measured_at_utc']}.", "",
           "| question | answer | source |", "|---|---|---|"]
    # Pointers, not copied values (O197): the board checks this document, so
    # this document cannot carry the board's current state without the two
    # chasing each other.
    out += [
        "| does a fresh clone install and import | see board row "
        "`clean_install` | `tools/clean_install_check.py` |",
        "| does a fresh environment pass preflight | see board row "
        "`preflight` | `tools/preflight.py --profile demo` |",
        f"| configuration decisions an operator can face | {cfg['count']} "
        f"environment variables | `{cfg['source']}` |",
        f"| time to first run | {NOT_MEASURED} | depends on a provider, a "
        f"machine and a person; measuring it here would produce a number about "
        f"this machine and publish it as a number about the product |",
        f"| time to first verified result | {NOT_MEASURED} | same, and it also "
        f"depends on the task |",
        f"| manual interventions per run | {NOT_MEASURED} | the run ledger "
        f"records halts; a rate needs a campaign, and the campaigns this "
        f"repository has are benchmarks rather than product usage |",
        "", "## The configuration decisions, named", "",
        "| variable | read by |", "|---|---|"]
    for name, files in cfg["variables"].items():
        out.append(f"| `{name}` | {', '.join('`' + f + '`' for f in files[:3])} |")
    out += ["",
            "A variable with no default is a decision the operator must make;",
            "one with a default is a decision they may ignore. That",
            "distinction is not derivable from a regular expression over",
            "`os.environ`, so it is not claimed here.", ""]
    return "\n".join(out)


def contract_trace(body: dict) -> str:
    out = ["# Contract trace baseline (V3.3 P0)", "",
           "Generated by `python3 tools/baseline_docs.py`. One row per",
           "requirement in the register, with the symbol or board row that",
           "answers it. A requirement with no trace is not a failure of this",
           "document: it is the plan describing work that has not been done.",
           "", f"Measured at {body['measured_at_utc']}.", "",
           "| id | plan item | requirement | status | traces to |",
           "|---|---|---|---|---|"]
    for cap in body["capabilities"]:
        trace = "; ".join(cap.get("evidence") or []) or "—"
        item = cap.get("authoritative_item") or "—"
        out.append(f"| `{cap['id']}` | `{item}` | {cap['name'][:60]} | "
                   f"{cap['status']} | {trace[:90]} |")
    out.append("")
    return "\n".join(out)


def measure() -> dict:
    cm = _matrix()
    matrix = cm.measure()
    return {
        "measured_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cli": cli_surface(),
        "package": package_shape(),
        "board": board_rows(),
        "manifest": manifest_shape(),
        "config": configuration_decisions(),
        "install": fresh_install_path(),
        "runtime": runtime_model(),
        "human": human_intervention_points(),
        "dogfood": dogfood_evidence(),
        "matrix_counts": matrix["counts"],
        "matrix_source": matrix["source_register"],
        "capabilities": matrix["capabilities"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    cm = _matrix()
    try:
        body = measure()
    except cm.RegisterMissing as exc:
        print(str(exc), file=sys.stderr)
        return 3
    documents = {
        "VERIHARNESS_CURRENT_BASELINE.md": current_baseline(body),
        "VERIHARNESS_PRODUCT_BASELINE.md": product_baseline(body),
        "VERIHARNESS_CONTRACT_TRACE_BASELINE.md": contract_trace(body),
    }
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in documents.items():
            (args.out_dir / name).write_text(text + "\n")
            print(f"wrote {args.out_dir / name}")
    else:
        for name in documents:
            print(f"  would write {name}")
    print(f"{body['config']['count']} configuration variable(s); "
          f"{len(body['cli']['commands'])} CLI command(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
