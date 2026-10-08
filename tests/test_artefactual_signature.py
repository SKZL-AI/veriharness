"""A baseline that never ran the criterion is recognised whatever pytest's casing.

`artefactual_reason` names a red baseline that did not execute the criterion,
so the run record does not count "the candidate added a file" as a
demonstrated increment. Run `20260925t1801z-ptimer` recorded five criteria as
discriminating whose baselines failed only because the test file did not exist:
pytest 9 prints `ERROR: file or directory not found: <path>` (exit 4), the
exact signature list held only the lower-case form, and in the read-only arena
pytest's cache warning replaced the `no tests ran` summary with `1 warning`.

The rule under test:

* the existing `ARTEFACTUAL_SIGNATURES` keep today's exact, case-sensitive
  matching at every exit code, exit 4 included;
* `ARTEFACTUAL_SIGNATURES_EXIT_4` is matched case-insensitively and only at
  exit 4, pytest's usage-error code, where a missing path or node id lands;
* so a test that merely prints one of those lines and fails (exit 1), or a
  module that prints one and then raises at import (exit 2), stays a real
  failure.

Every transcript is real: each test runs the installed pytest in its own
temporary directory, stderr merged into stdout as the runner does, and feeds
the exit code and output to `artefactual_reason`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hoh.runner import artefactual_reason

_PYPROJECT = "[tool.pytest.ini_options]\n"
_PASSING = "def test_a():\n    pass\n"
_PRINTS_MISSING_PATH = "print('ERROR: file or directory not found: x.py')\n"


def _pytest(root: Path, files: dict[str, str], args: list[str], *,
            read_only: bool = False) -> subprocess.CompletedProcess:
    """Run pytest on `files` written into `root`; stderr merged into stdout.

    `read_only` makes `root` mode 0o555 after the files are written, as the
    STRICT arena is, and restores it afterwards. Those runs keep the cache
    provider, because its warning is what replaces `no tests ran`; the others
    disable it so they write nothing beside the files.
    """
    for name, text in files.items():
        (root / name).write_text(text)
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    cmd = [sys.executable, "-m", "pytest", "-q"]
    if not read_only:
        cmd += ["-p", "no:cacheprovider"]
    mode = root.stat().st_mode
    if read_only:
        os.chmod(root, 0o555)
    try:
        return subprocess.run(
            cmd + args, cwd=root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    finally:
        os.chmod(root, mode)


def _assert_read_only_precondition(r: subprocess.CompletedProcess) -> None:
    # Writable, or run as root, pytest prints `no tests ran` and the old exact
    # list already matches: the test would then prove nothing.
    assert r.returncode == 4, r.stdout
    assert "no tests ran" not in r.stdout.lower(), (
        "the rootdir was writable for pytest (or this runs as root): the "
        "summary is not the warning form\n" + r.stdout
    )


# --------------------------------------------------------------------------- #
# Exit 4: nothing executed
# --------------------------------------------------------------------------- #


def test_upper_case_missing_path(tmp_path):
    """pytest 9's `ERROR: file or directory not found: <path>` at exit 4, on a
    read-only rootdir whose summary is `1 warning in ...`, is artefactual.

    This test fails on the current code (before `ARTEFACTUAL_SIGNATURES_EXIT_4`
    existed): its exact list held this message only in lower case, and the
    read-only rootdir turns the empty-run summary into the warning form.

    (This source is printed in the traceback when the test fails, so it quotes
    no exact signature: a red run of it must not itself read as artefactual.)
    """
    r = _pytest(tmp_path, {"pyproject.toml": _PYPROJECT},
                ["tests/test_zz_no_such_file.py"], read_only=True)
    _assert_read_only_precondition(r)
    assert "ERROR: file or directory not found" in r.stdout, r.stdout
    assert artefactual_reason(r.returncode, r.stdout), r.stdout


def test_missing_node_id_is_artefactual(tmp_path):
    r = _pytest(tmp_path, {"pyproject.toml": _PYPROJECT, "test_a.py": _PASSING},
                ["test_a.py::test_zz_no_such_test"], read_only=True)
    _assert_read_only_precondition(r)
    assert "ERROR: not found:" in r.stdout, r.stdout
    assert artefactual_reason(r.returncode, r.stdout), r.stdout


def test_conftest_module_not_found_at_exit_4_stays_artefactual(tmp_path):
    """An existing exact signature (the missing-module error) still counts at
    exit 4; the new tuple adds to it, it does not replace it there."""
    r = _pytest(tmp_path, {"conftest.py": "import zz_no_such_module_q\n",
                           "test_a.py": _PASSING}, ["test_a.py"])
    assert r.returncode == 4, r.stdout
    assert artefactual_reason(r.returncode, r.stdout), r.stdout


def test_conftest_runtime_error_at_exit_4_is_a_real_failure(tmp_path):
    r = _pytest(tmp_path, {"conftest.py": "raise RuntimeError('boom')\n",
                           "test_a.py": _PASSING}, ["test_a.py"])
    assert r.returncode == 4, r.stdout
    assert artefactual_reason(r.returncode, r.stdout) == "", r.stdout


def test_unknown_option_at_exit_4_is_not_artefactual(tmp_path):
    """Exit 4 alone is not a signature: a usage error that is not about a
    missing path or test stays a real failure."""
    r = _pytest(tmp_path, {"test_a.py": _PASSING},
                ["--zz-no-such-option", "test_a.py"])
    assert r.returncode == 4, r.stdout
    assert artefactual_reason(r.returncode, r.stdout) == "", r.stdout


# --------------------------------------------------------------------------- #
# Exit 1 and 2: tests ran, or a file was there and failed
# --------------------------------------------------------------------------- #


def test_real_failure_not_artefactual(tmp_path):
    """An ordinary assertion failure, and a test that prints both pytest
    `ERROR:` lines before failing: tests ran, so neither is artefactual."""
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _pytest(plain, {"test_a.py": "def test_a():\n    assert 1 == 2\n"},
                ["test_a.py"])
    assert r.returncode == 1, r.stdout
    assert "1 failed" in r.stdout, r.stdout
    assert artefactual_reason(r.returncode, r.stdout) == "", r.stdout

    prints = tmp_path / "prints"
    prints.mkdir()
    r = _pytest(prints, {"test_a.py": (
        "def test_a():\n"
        "    " + _PRINTS_MISSING_PATH +
        "    print('ERROR: not found: y')\n"
        "    assert False\n"
    )}, ["test_a.py"])
    assert r.returncode == 1, r.stdout
    assert "1 failed" in r.stdout, r.stdout
    assert "ERROR: file or directory not found" in r.stdout, r.stdout
    assert "ERROR: not found:" in r.stdout, r.stdout
    assert artefactual_reason(r.returncode, r.stdout) == "", r.stdout


def test_import_runtime_error_is_a_real_failure(tmp_path):
    r = _pytest(tmp_path, {"test_a.py": "raise RuntimeError('boom')\n"},
                ["test_a.py"])
    assert r.returncode == 2, r.stdout
    assert artefactual_reason(r.returncode, r.stdout) == "", r.stdout


def test_printed_signature_before_import_error_is_a_real_failure(tmp_path):
    """The new signatures count only at exit 4: a collection error at exit 2
    whose module printed one first is still a real failure."""
    r = _pytest(tmp_path, {"test_a.py":
                           _PRINTS_MISSING_PATH + "raise RuntimeError('boom')\n"},
                ["test_a.py"])
    assert r.returncode == 2, r.stdout
    assert "ERROR: file or directory not found" in r.stdout, r.stdout
    assert artefactual_reason(r.returncode, r.stdout) == "", r.stdout


def test_import_module_not_found_stays_artefactual(tmp_path):
    r = _pytest(tmp_path, {"test_a.py": "import zz_no_such_module_q\n"},
                ["test_a.py"])
    assert r.returncode == 2, r.stdout
    assert artefactual_reason(r.returncode, r.stdout), r.stdout


def test_import_cannot_import_name_stays_artefactual(tmp_path):
    r = _pytest(tmp_path, {"test_a.py": "from os import zz_no_such_name\n"},
                ["test_a.py"])
    assert r.returncode == 2, r.stdout
    assert artefactual_reason(r.returncode, r.stdout), r.stdout
