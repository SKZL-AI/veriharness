"""Run outside the checkout, with the interpreter of the installed distribution."""
import importlib.metadata
import importlib.resources
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import hoh
from hoh.runner import HouseRuleViolation, assert_command_allowed


def main():
    distribution = importlib.metadata.distribution("hoh")
    assert distribution.version == "0.1.0"
    assert Path(hoh.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert any(e.name == "hoh" and e.value == "hoh.cli:main"
               for e in distribution.entry_points if e.group == "console_scripts")
    assert distribution.metadata["Requires-Python"] == ">=3.11"
    assert distribution.metadata["License-Expression"] == "MIT"
    assert "Repository, https://github.com/SKZL-AI/veriharness" in distribution.metadata.get_all("Project-URL")
    assert "pydantic>=2.0" in distribution.requires
    assert distribution.metadata["Description-Content-Type"] == "text/markdown"
    assert "VeriHarness" in distribution.metadata.get_payload()
    policy = importlib.resources.files("hoh").joinpath("policy")
    for name in ("dangerous-patterns.txt", "house-rules-patterns.txt"):
        assert policy.joinpath(name).read_text().strip()
    assert_command_allowed("python3 -m pytest -q")
    try:
        assert_command_allowed("nvidia-smi")
    except HouseRuleViolation:
        pass
    else:
        raise AssertionError("Forbidden command was accepted")
    executable = shutil.which("hoh")
    assert executable and Path(executable).resolve().is_relative_to(Path(sys.prefix).resolve())
    subprocess.run([executable, "--help"], check=True)
    with tempfile.TemporaryDirectory() as scratch:
        subprocess.run([executable, "--root", scratch, "list"], cwd=scratch, check=True)
    print(json.dumps({"version": distribution.version, "python": sys.version.split()[0],
                      "import": "PASS", "entry_point": "PASS", "policy": "PASS",
                      "guard": "PASS", "cli_list": "PASS", "metadata": "PASS"}))


if __name__ == "__main__":
    main()
