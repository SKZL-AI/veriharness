#!/usr/bin/env python3
"""Builds the distribution, installs it into a fresh virtual environment, and
proves the installed copy actually works.

This exists because `RC_GATE.md` carried "clean install" as `NOT_RUN` from the
first release candidate onward -- an honest admission, but an admission. The
gap it names is specific and has bitten this project once already: a wheel can
build, install, import, and still be unable to execute a single acceptance
check, because the guard pattern files were not packaged. Importing `hoh`
proves nothing about that. Running the guard does.

So the check below is deliberately not "does it import". In order:

  1. build a wheel and an sdist from a clean copy of the tree
  2. create a fresh virtual environment, install the wheel into it
  3. run the console script's own `--help`
  4. read the packaged policy files back **out of the installed copy**
  5. call the guard and require it to allow a harmless command and refuse a
     forbidden one

Step 5 is the one that matters. Steps 1-4 can all pass on a build that is
useless in practice.

Exit code 0 only if every step passes. Any failure prints the step, its exit
code, and the tail of its output.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# A command the guard must allow, and one it must refuse. The refusal is the
# load-bearing half: it can only work if the pattern files were packaged.
HARMLOS = "python3 -m pytest -q"
VERBOTEN = "nvidia-smi"


class Schritt:
    def __init__(self) -> None:
        self.rot = 0

    def __call__(self, name: str, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        status = "OK  " if p.returncode == 0 else "ROT "
        print(f"  {status} {name} (rc={p.returncode})")
        if p.returncode != 0:
            self.rot += 1
            for line in (p.stdout + p.stderr).strip().splitlines()[-8:]:
                print(f"        {line}")
        return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=".", help="tree to build from")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ap.add_argument("--dist", help="test existing wheel/sdist without rebuilding them")
    args = ap.parse_args()

    quelle = Path(args.source).resolve()
    if not (quelle / "pyproject.toml").exists():
        print(f"ROT  {quelle} carries no pyproject.toml")
        return 2

    arbeit = Path(tempfile.mkdtemp(prefix="hoh-clean-install-"))
    schritt = Schritt()
    try:
        print(f"=== clean install check, source {quelle} ===")

        # 1. Build from a copy: building in place leaves build/ and *.egg-info
        # behind in the tree under test, which is exactly the sort of residue a
        # "clean checkout" claim must not depend on.
        baum = arbeit / "source"
        if args.dist:
            dist = Path(args.dist).resolve()
        else:
            shutil.copytree(
                quelle, baum,
                ignore=shutil.ignore_patterns(
                    ".git", ".pytest_cache", ".ruff_cache", "__pycache__",
                    "build", "dist", "*.egg-info", "runs", "demo",
                ),
            )
            schritt("build wheel and sdist", [sys.executable, "-m", "build", str(baum)], cwd=baum)
            dist = baum / "dist"
        raeder = sorted(dist.glob("*.whl"))
        quellen = sorted(dist.glob("*.tar.gz"))
        print(f"  {'OK  ' if raeder else 'ROT '} wheel produced: {[w.name for w in raeder] or 'none'}")
        print(f"  {'OK  ' if quellen else 'ROT '} sdist produced: {[s.name for s in quellen] or 'none'}")
        if not raeder:
            schritt.rot += 1
            return 1 if schritt.rot else 0

        # 2. Fresh environment. Not the ambient interpreter: the whole point is
        # to see what a stranger gets.
        venv = arbeit / "venv"
        schritt("create fresh venv", [sys.executable, "-m", "venv", str(venv)])
        pip = venv / "bin" / "pip"
        py = venv / "bin" / "python"
        if not pip.exists():                       # Windows layout
            pip, py = venv / "Scripts" / "pip.exe", venv / "Scripts" / "python.exe"
        schritt("install the wheel", [str(pip), "install", "--quiet", str(raeder[0])])

        # 3. The console script, as an installed user would invoke it.
        schritt("console script --help", [str(py), "-m", "hoh.cli", "--help"])

        # 4/5. Read the packaged policy back out of the INSTALLED copy, then
        # exercise the guard. Written as one probe so a partial pass cannot be
        # mistaken for a working install.
        probe = (
            "import pathlib, sys, hoh\n"
            "p = pathlib.Path(hoh.__file__).parent / 'policy'\n"
            "dateien = sorted(f.name for f in p.glob('*.txt')) if p.is_dir() else []\n"
            "if not dateien:\n"
            "    print('policy files missing from the installed package'); sys.exit(1)\n"
            "print('packaged policy:', dateien)\n"
            "from hoh.runner import assert_command_allowed\n"
            f"assert_command_allowed({HARMLOS!r})\n"
            "print('harmless command: allowed')\n"
            "try:\n"
            f"    assert_command_allowed({VERBOTEN!r})\n"
            "except Exception as e:\n"
            "    print('forbidden command: refused ->', type(e).__name__)\n"
            "else:\n"
            "    print('forbidden command was ALLOWED -- the guard is inert'); sys.exit(1)\n"
        )
        p = schritt("guard works from the install", [str(py), "-c", probe])
        for line in p.stdout.strip().splitlines():
            print(f"        {line}")

        print(f"=== {schritt.rot} red step(s) ===")
        return 1 if schritt.rot else 0
    finally:
        if args.keep:
            print(f"    scratch kept at {arbeit}")
        else:
            shutil.rmtree(arbeit, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
