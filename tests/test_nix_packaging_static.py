"""flake.nix, checked statically.

There is no Nix binary and no network where this runs, so nothing here
evaluates or builds the flake. These cases only catch a flake that has drifted
from `pyproject.toml`, lost a runtime tool, or is structurally broken text. Whether it builds and
runs on NixOS is NOT_RUN.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAKE = (ROOT / "flake.nix").read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The text without `#` comments and without string contents."""
    text = re.sub(r"''.*?''", "''''", text, flags=re.S)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    return re.sub(r"#[^\n]*", "", text)


def test_brackets_balance():
    code = _code(FLAKE)
    for opener, closer in ("{}", "[]", "()"):
        assert code.count(opener) == code.count(closer), (opener, closer)
    depth = 0
    for ch in code:
        depth += (ch == "{") - (ch == "}")
        assert depth >= 0


def test_the_flake_reads_name_and_version_from_pyproject():
    assert "builtins.fromTOML (builtins.readFile ./pyproject.toml)" in FLAKE
    assert "pyproject.project.name" in FLAKE and "pyproject.project.version" in FLAKE
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "hoh"


def test_declared_dependencies_are_nix_dependencies():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in data["project"]["dependencies"]:
        name = re.split(r"[<>=!~\[ ]", requirement)[0].lower()
        assert f"pkgs.python3Packages.{name}" in FLAKE, name
    assert "setuptools" in FLAKE and "pyproject = true;" in FLAKE


def test_the_runtime_tools_are_on_the_wrapped_path():
    block = FLAKE[FLAKE.index("makeWrapperArgs"):FLAKE.index("pythonImportsCheck")]
    for tool in ("pkgs.git", "pkgs.bash", "pkgs.bubblewrap"):
        assert tool in block, tool


def test_outputs_exist_for_a_package_a_dev_shell_and_an_app():
    for attr in ("packages = ", "devShells = ", "apps = ", "default = "):
        assert attr in FLAKE, attr
    assert 'systems = [ "x86_64-linux" "aarch64-linux" ];' in FLAKE


def test_the_shipped_policy_files_are_in_the_source_set():
    assert "./policy" in FLAKE and "./src" in FLAKE
    assert (ROOT / "src" / "hoh" / "policy" / "dangerous-patterns.txt").is_file()


def test_the_flake_says_it_was_never_built():
    assert "NOT_RUN" in FLAKE
    assert "doCheck = false;" in FLAKE


def test_no_fetch_with_a_made_up_hash():
    code = _code(FLAKE)
    assert "fakeHash" not in code and "fetchurl" not in code and "fetchFromGitHub" not in code


def test_readme_has_platform_notes_that_do_not_overclaim():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("### Platform notes")
    notes = text[start:text.index("## Running it", start)]
    for needle in ("NixOS", "macOS", "partially supported", "--isolation none",
                   "apparmor_restrict_unprivileged_userns", "NOT_RUN", "flake.nix",
                   "HOH_HERDR_COMM", "readable by a strict check", "closure-only binds are deferred",
                   "preferred remedy", "system-wide", "narrowed"):
        assert needle in notes, needle
    assert "Operating System :: MacOS" not in (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"), "macOS is partially supported; no classifier claims it"
