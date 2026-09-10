#!/usr/bin/env python3
"""union_gate.py -- the five union invariants (U1..U5) over a repository's combined state.

Where `tools/check_claims.py` checks a claims ledger against the run tree it
was written from, this checks the repository as a whole against itself --
the five invariants proposed in `dogfood/VORSCHLAG_UNION_GATE.md`, each
anchored in a real defect that a per-candidate check could not have caught
because no single candidate saw the combined state.

It reports. It never repairs -- no file is written anywhere under the
repository path it is pointed at; any build output it needs (a wheel, for
U3) is produced in a fresh system temp directory and never touches the
repository. Where an invariant cannot be checked here -- U4 needs a `runs/`
tree, U3 needs to build a wheel -- it prints SKIPPED with the reason instead
of silently passing.

Usage:
    python3 tools/union_gate.py [REPO_PATH]

REPO_PATH defaults to '.'. Exit code is 0 unless at least one invariant is
FAIL; a SKIPPED invariant never affects the exit code, but always appears in
the printed output.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"

# Every FAIL/SKIPPED reason is capped to this many characters before it is
# collected onto a Result -- the runner truncates check output at 4 MB, and a
# gate should never approach that on its own account. DUG2 measured a U3
# reason grow from 1.3 MB to 1.96 MB as `runs/` grew, from one aggregating
# shutil.copytree() call that built the whole message before anything printed
# it; capping only at print time would not have prevented that. A named
# constant, not a magic number, so every call site (U1..U5) shares one budget.
REASON_BUDGET = 300

# The capped-message suffix reserves this many characters for its own
# "omitted" count so the total never exceeds REASON_BUDGET even when the
# omitted count itself runs to several digits.
_REASON_SUFFIX_RESERVE = 40


def _cap_reason(text: str) -> str:
    """Caps `text` to REASON_BUDGET characters, stating how many were cut.

    Called from Result.fail/Result.skipped -- i.e. at the point a reason is
    collected, not where the gate later prints it -- so a reason built from a
    huge collection (thousands of skipped paths, a giant subprocess tail) is
    always bounded before it ever reaches a Result.
    """
    if len(text) <= REASON_BUDGET:
        return text
    keep = max(0, REASON_BUDGET - _REASON_SUFFIX_RESERVE)
    omitted = len(text) - keep
    capped = f"{text[:keep]} [+{omitted} chars omitted]"
    if len(capped) > REASON_BUDGET:
        capped = capped[:REASON_BUDGET]
    return capped


@dataclass(frozen=True)
class Result:
    status: str
    detail: str | None = None

    @staticmethod
    def ok() -> Result:
        return Result(STATUS_PASS, None)

    @staticmethod
    def fail(detail: str) -> Result:
        return Result(STATUS_FAIL, _cap_reason(detail))

    @staticmethod
    def skipped(detail: str) -> Result:
        return Result(STATUS_SKIPPED, _cap_reason(detail))


@dataclass(frozen=True)
class WheelInfo:
    """The outcome of one isolated wheel build, shared by U1's optional
    METADATA check and U3 so the build only ever runs once."""

    attempted: bool
    built: bool
    reason: str | None = None
    wheel_path: Path | None = None
    license_expression: str | None = None
    licenses_dir_basenames: frozenset[str] | None = None
    declared_license_files: tuple[str, ...] | None = None
    skipped_paths: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #


def _oneline(text: str) -> str:
    return " ".join(text.split())


def _tail_nonempty_line(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def _head_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _load_toml(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"cannot parse {path.name}: {exc}"


def _project_license(data: dict) -> str | None:
    project = data.get("project")
    value = project.get("license") if isinstance(project, dict) else None
    return value if isinstance(value, str) else None


def _project_license_files(data: dict) -> tuple[str, ...] | None:
    project = data.get("project")
    entries = project.get("license-files") if isinstance(project, dict) else None
    if not isinstance(entries, list):
        return None
    return tuple(str(e) for e in entries)


_CITATION_LICENSE_RE = re.compile(r"(?m)^license:\s*(.+?)\s*$")


def _citation_license(text: str) -> str | None:
    m = _CITATION_LICENSE_RE.search(text)
    if not m:
        return None
    value = m.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


# --------------------------------------------------------------------------- #
# isolated wheel build -- feeds U1's optional METADATA check and U3
# --------------------------------------------------------------------------- #

# `pip wheel --no-build-isolation` on a local source directory writes
# `build/` and `*.egg-info` into that directory as a side effect of the
# setuptools backend -- so the build never runs against REPO_PATH itself,
# only against a throwaway copy under the caller's temp work_dir. Names/
# suffixes a setuptools build produces or caches itself and never needs as
# *input* -- skipped wherever they turn up inside a declared source directory.
_IGNORE_NAMES = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"})
_IGNORE_SUFFIXES = (".egg-info",)


def _should_ignore(name: str) -> bool:
    return name in _IGNORE_NAMES or name.endswith(_IGNORE_SUFFIXES)


def _packages_find_where(pyproject_data: dict) -> tuple[str, ...]:
    """The source directories setuptools searches for packages in, per
    [tool.setuptools.packages.find].where -- derived from the parsed TOML
    rather than hardcoded. Falls back to the conventional ("src",) only when
    the project declares no such table at all."""
    tool = pyproject_data.get("tool")
    setuptools_cfg = tool.get("setuptools") if isinstance(tool, dict) else None
    packages = setuptools_cfg.get("packages") if isinstance(setuptools_cfg, dict) else None
    find = packages.get("find") if isinstance(packages, dict) else None
    where = find.get("where") if isinstance(find, dict) else None
    if isinstance(where, list) and where:
        return tuple(str(w) for w in where)
    return ("src",)


def _resolve_license_files(repo_path: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Expands each project.license-files entry (a literal name or a PEP 639
    glob, e.g. "LICENSE*") against repo_path into the concrete files that
    exist today. A pattern matching nothing is not an error here -- an
    essential that turns out truly missing surfaces later, from pip wheel
    itself, exactly as it did before this input set was narrowed."""
    resolved: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for candidate in sorted(repo_path.glob(pattern)):
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                resolved.append(candidate)
    return resolved


def _copy_declared_tree(src_dir: Path, dst_dir: Path, skipped: list[str], *, base: Path) -> None:
    """Copies src_dir to dst_dir entry by entry -- never one aggregating
    shutil.copytree() over the whole subtree -- so a single path vanishing or
    dangling mid-walk (the DUG2 defect: a transient pytest tempdir under
    runs/) is recorded and skipped instead of aborting the entire build, and
    instead of shutil.Error aggregating one entry per failure across the
    whole recursive walk into a single giant message."""
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        entries = sorted(src_dir.iterdir())
    except OSError:
        skipped.append(str(src_dir.relative_to(base)))
        return

    for entry in entries:
        if _should_ignore(entry.name):
            continue
        dst_entry = dst_dir / entry.name
        try:
            if entry.is_symlink() and not entry.exists():
                skipped.append(str(entry.relative_to(base)))
                continue
            if entry.is_dir():
                _copy_declared_tree(entry, dst_entry, skipped, base=base)
            else:
                shutil.copy2(entry, dst_entry)
        except OSError:
            skipped.append(str(entry.relative_to(base)))


def build_wheel(repo_path: Path, work_dir: Path) -> WheelInfo:
    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return WheelInfo(attempted=False, built=False, reason="no pyproject.toml")

    pyproject_data, err = _load_toml(pyproject_path)
    if pyproject_data is None:
        return WheelInfo(attempted=True, built=False, reason=f"cannot parse pyproject.toml: {err}")

    where_dirs = _packages_find_where(pyproject_data)
    missing_where = [w for w in where_dirs if not (repo_path / w).is_dir()]
    if missing_where:
        noun = "directory" if len(missing_where) == 1 else "directories"
        return WheelInfo(
            attempted=True, built=False,
            reason=f"declared source {noun} missing: {', '.join(missing_where)}",
        )

    build_src = work_dir / "build_src"
    out_dir = work_dir / "wheel_out"
    build_src.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copy2(pyproject_path, build_src / "pyproject.toml")
    except OSError as exc:
        return WheelInfo(attempted=True, built=False, reason=f"could not copy pyproject.toml: {exc}")

    # The build input is a declared set -- these source directories plus
    # pyproject.toml plus the declared license files -- never the whole
    # repository. Nothing else (a decoy directory, runs/, anything not named
    # above) is ever read, let alone copied.
    skipped_paths: list[str] = []
    for where in where_dirs:
        _copy_declared_tree(repo_path / where, build_src / where, skipped_paths, base=repo_path)

    license_patterns = _project_license_files(pyproject_data) or ()
    for lic_path in _resolve_license_files(repo_path, license_patterns):
        rel = lic_path.relative_to(repo_path)
        dst = build_src / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lic_path, dst)
        except OSError:
            skipped_paths.append(str(rel))

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "wheel", str(build_src),
                "--no-deps", "--no-build-isolation", "-w", str(out_dir),
            ],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WheelInfo(attempted=True, built=False, reason=f"pip wheel could not run: {exc}")

    if proc.returncode != 0:
        tail = _tail_nonempty_line(proc.stdout) or _tail_nonempty_line(proc.stderr) or "no output"
        return WheelInfo(
            attempted=True, built=False, reason=f"pip wheel exited {proc.returncode}: {tail}",
            skipped_paths=tuple(skipped_paths),
        )

    wheels = sorted(out_dir.glob("*.whl"))
    if not wheels:
        return WheelInfo(
            attempted=True, built=False, reason="pip wheel reported success but produced no .whl file",
            skipped_paths=tuple(skipped_paths),
        )
    wheel_path = wheels[0]

    try:
        with zipfile.ZipFile(wheel_path) as zf:
            names = zf.namelist()
            metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
            if not metadata_names:
                return WheelInfo(
                    attempted=True, built=False, reason=f"{wheel_path.name} has no dist-info/METADATA",
                    skipped_paths=tuple(skipped_paths),
                )
            dist_info_prefix = metadata_names[0][: -len("METADATA")]
            metadata_text = zf.read(metadata_names[0]).decode("utf-8", errors="replace")
            licenses_prefix = dist_info_prefix + "licenses/"
            licenses_basenames = frozenset(
                Path(n).name for n in names if n.startswith(licenses_prefix) and not n.endswith("/")
            )
    except (OSError, zipfile.BadZipFile) as exc:
        return WheelInfo(
            attempted=True, built=False, reason=f"cannot read {wheel_path.name}: {exc}",
            skipped_paths=tuple(skipped_paths),
        )

    license_expression = None
    for line in metadata_text.splitlines():
        if line.startswith("License-Expression:"):
            license_expression = line.split(":", 1)[1].strip()
            break

    declared = _project_license_files(pyproject_data)

    return WheelInfo(
        attempted=True,
        built=True,
        wheel_path=wheel_path,
        license_expression=license_expression,
        licenses_dir_basenames=licenses_basenames,
        declared_license_files=declared,
        skipped_paths=tuple(skipped_paths),
    )


# --------------------------------------------------------------------------- #
# U1 -- every licence statement in the repository agrees (O27)
# --------------------------------------------------------------------------- #


def check_u1(repo_path: Path, wheel_info: WheelInfo) -> Result:
    pyproject_path = repo_path / "pyproject.toml"
    citation_path = repo_path / "CITATION.cff"
    missing = [
        name for name, path in (("pyproject.toml", pyproject_path), ("CITATION.cff", citation_path))
        if not path.is_file()
    ]
    if missing:
        return Result.skipped(f"missing {', '.join(missing)}")

    pyproject_data, err = _load_toml(pyproject_path)
    if err:
        return Result.skipped(err)
    pyproject_license = _project_license(pyproject_data)
    if pyproject_license is None:
        return Result.skipped("pyproject.toml has no string project.license value")

    try:
        citation_text = citation_path.read_text(encoding="utf-8")
    except OSError as exc:
        return Result.skipped(f"cannot read CITATION.cff: {exc}")
    citation_license = _citation_license(citation_text)
    if citation_license is None:
        return Result.skipped("CITATION.cff has no top-level license: field")

    if pyproject_license != citation_license:
        return Result.fail(
            f"pyproject.toml license={pyproject_license!r} disagrees with "
            f"CITATION.cff license={citation_license!r}"
        )
    value = pyproject_license

    license_path = repo_path / "LICENSE"
    if not license_path.is_file():
        return Result.fail(f"LICENSE is missing but pyproject.toml and CITATION.cff both declare {value!r}")
    try:
        license_text = license_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Result.fail(f"LICENSE could not be read: {exc}")
    if value not in license_text:
        return Result.fail(f"LICENSE does not contain the declared license value {value!r}")

    if wheel_info.built and wheel_info.license_expression is not None:
        if wheel_info.license_expression != value:
            return Result.fail(
                f"{wheel_info.wheel_path.name}: dist-info METADATA License-Expression="
                f"{wheel_info.license_expression!r} disagrees with {value!r}"
            )

    return Result.ok()


# --------------------------------------------------------------------------- #
# U2 -- no shipped file references a file that is not shipped (export survey)
# --------------------------------------------------------------------------- #

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def _link_target(raw: str) -> str:
    s = raw.strip()
    if s.startswith("<"):
        end = s.find(">")
        if end != -1:
            return s[1:end]
    m = re.match(r"""^(\S+)(?:\s+(?:"[^"]*"|'[^']*'))?$""", s)
    if m:
        return m.group(1)
    parts = s.split()
    return parts[0] if parts else s


def _normalize_relpath(path: Path) -> str:
    out: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


def check_u2(repo_path: Path) -> Result:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result.skipped(f"could not run git: {exc}")
    if proc.returncode != 0:
        return Result.skipped(f"{repo_path} is not a git repository")

    shipped = {line for line in proc.stdout.splitlines() if line}
    md_files = sorted(f for f in shipped if f.endswith(".md"))

    findings: list[str] = []
    for rel in md_files:
        try:
            text = (repo_path / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ref_dir = Path(rel).parent
        for m in _LINK_RE.finditer(text):
            target = _link_target(m.group(1))
            if not target or target.startswith("#"):
                continue
            target_no_frag = target.split("#", 1)[0]
            if not target_no_frag or _SCHEME_RE.match(target_no_frag):
                continue
            resolved = _normalize_relpath(ref_dir / target_no_frag)
            if resolved not in shipped:
                findings.append(f"{rel} -> {target}")

    if findings:
        shown = findings[:5]
        suffix = f" (+{len(findings) - 5} more)" if len(findings) > 5 else ""
        return Result.fail(f"{len(findings)} dangling reference(s): " + "; ".join(shown) + suffix)
    return Result.ok()


# --------------------------------------------------------------------------- #
# U3 -- what the built artifact ships matches what the repo claims (O29)
# --------------------------------------------------------------------------- #


def check_u3(repo_path: Path, wheel_info: WheelInfo) -> Result:
    if not (repo_path / "pyproject.toml").is_file():
        return Result.skipped("no pyproject.toml")
    if not wheel_info.built:
        return Result.skipped(f"could not build a wheel: {wheel_info.reason}")

    declared = set(wheel_info.declared_license_files or ())
    shipped = set(wheel_info.licenses_dir_basenames or ())

    if declared != shipped:
        extra = sorted(shipped - declared)
        missing = sorted(declared - shipped)
        parts = []
        if extra:
            parts.append(f"wheel ships undeclared {extra}")
        if missing:
            parts.append(f"declared but not shipped {missing}")
        return Result.fail(
            f"{wheel_info.wheel_path.name}: pyproject.toml license-files={sorted(declared)} vs "
            f"shipped dist-info/licenses/={sorted(shipped)} (" + "; ".join(parts) + ")"
        )
    return Result.ok()


# --------------------------------------------------------------------------- #
# U4 -- every evidence reference in the claims ledger still resolves (O37)
# --------------------------------------------------------------------------- #


def check_u4(repo_path: Path) -> Result:
    runs_dir = repo_path / "runs"
    checker_path = repo_path / "tools" / "check_claims.py"
    ledger_path = repo_path / "CLAIMS.json"

    missing = []
    if not runs_dir.is_dir():
        missing.append("runs/")
    if not checker_path.is_file():
        missing.append("tools/check_claims.py")
    if not ledger_path.is_file():
        missing.append("CLAIMS.json")
    if missing:
        return Result.skipped(f"missing {', '.join(missing)}")

    # Delegation only: this subprocess call and its exit code are the entire
    # contract. Nothing from check_claims.py is imported or inspected beyond
    # what it prints and how it exits -- see the module docstring.
    try:
        proc = subprocess.run(
            [sys.executable, str(checker_path), "check", "all"],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result.fail(f"could not run check_claims.py: {exc}")

    if proc.returncode == 0:
        return Result.ok()

    finding = _tail_nonempty_line(proc.stdout) or _tail_nonempty_line(proc.stderr) or "no output"
    return Result.fail(f"check_claims.py check all exited {proc.returncode}: {finding!r}")


# --------------------------------------------------------------------------- #
# U5 -- test suite and linter are green on the combined state
# --------------------------------------------------------------------------- #


def _no_bytecode_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def check_u5(repo_path: Path) -> Result:
    if not (repo_path / "tests").is_dir():
        return Result.skipped("no tests/ directory")

    # `-p no:cacheprovider` and PYTHONDONTWRITEBYTECODE keep pytest from
    # leaving `.pytest_cache/` or `__pycache__/` inside REPO_PATH; neither
    # flag changes what counts as a pass or a fail.
    try:
        pytest_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=600,
            env=_no_bytecode_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result.fail(f"could not run pytest: {exc}")
    if pytest_proc.returncode != 0:
        tail = _tail_nonempty_line(pytest_proc.stdout) or _tail_nonempty_line(pytest_proc.stderr) or "no output"
        return Result.fail(f"pytest: {tail}")

    subset = [name for name in ("src", "tests", "tools") if (repo_path / name).exists()]
    if subset:
        try:
            ruff_proc = subprocess.run(
                ["ruff", "check", "--no-cache", "--select", "F,E9", *subset],
                cwd=str(repo_path),
                capture_output=True, text=True, timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result.fail(f"could not run ruff: {exc}")
        if ruff_proc.returncode != 0:
            first = _head_nonempty_line(ruff_proc.stdout) or _head_nonempty_line(ruff_proc.stderr) or "no output"
            return Result.fail(f"ruff: {first}")

    return Result.ok()


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #


def _format_line(n: int, result: Result) -> str:
    if result.status == STATUS_PASS:
        return f"U{n}: PASS"
    detail = _oneline(result.detail or "") or "no further detail given"
    return f"U{n}: {result.status}: {detail}"


def run_gate(repo_path: Path) -> tuple[list[Result], int]:
    with tempfile.TemporaryDirectory(prefix="union_gate_") as tmp:
        work_dir = Path(tmp)
        wheel_info = build_wheel(repo_path, work_dir)
        results = [
            check_u1(repo_path, wheel_info),
            check_u2(repo_path),
            check_u3(repo_path, wheel_info),
            check_u4(repo_path),
            check_u5(repo_path),
        ]
    exit_code = 1 if any(r.status == STATUS_FAIL for r in results) else 0
    return results, exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="union_gate.py",
        description=(
            "Checks the five union invariants (U1..U5) proposed in "
            "dogfood/VORSCHLAG_UNION_GATE.md against a repository's combined "
            "state: one PASS/FAIL/SKIPPED line per invariant. Reports only "
            "-- it never repairs, and writes nothing outside its own stdout."
        ),
    )
    parser.add_argument(
        "repo_path",
        nargs="?",
        default=".",
        help="the repository whose combined state is checked (default: '.')",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    results, exit_code = run_gate(Path(args.repo_path))
    for i, result in enumerate(results, start=1):
        print(_format_line(i, result))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
