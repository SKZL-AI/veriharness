"""A release gate must reject changed bytes and unexpected index files."""
import hashlib
import re
import subprocess
import sys
import io
import json
import urllib.error

import pytest

from tools import distribution_release as release


@pytest.fixture
def bundle(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    hashes = {}
    for name in release.FILES:
        raw = name.encode()
        (dist / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    (tmp_path / "SHA256SUMS.txt").write_text("".join(f"{v}  {k}\n" for k, v in hashes.items()))
    return tmp_path


def test_changed_local_artifact_is_refused(bundle):
    (bundle / "dist" / "hoh-0.1.0.tar.gz").write_bytes(b"changed")
    with pytest.raises(AssertionError):
        release.expected_hashes(bundle)


@pytest.mark.parametrize("fault", ["digest", "extra", "missing", "duplicate"])
def test_existing_index_must_match_complete_file_set(bundle, monkeypatch, fault):
    hashes = release.expected_hashes(bundle)
    files = [{"filename": k, "digests": {"sha256": v}} for k, v in hashes.items()]
    if fault == "digest":
        files[0]["digests"]["sha256"] = "0" * 64
    elif fault == "extra":
        files.append({"filename": "unexpected.whl", "digests": {"sha256": "0" * 64}})
    elif fault == "missing":
        files.pop()
    else:
        files.append(files[0])
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps({"urls": files}).encode()))
    with pytest.raises(AssertionError, match="Published files differ"):
        release.index(bundle, "testpypi", False)


def test_only_404_can_mean_absent(bundle, monkeypatch):
    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org", 403, "Forbidden", {}, None)
    monkeypatch.setattr(release.urllib.request, "urlopen", unavailable)
    with pytest.raises(urllib.error.HTTPError):
        release.index(bundle, "pypi", True)


def test_other_release_ref_is_refused(tmp_path):
    with pytest.raises(AssertionError, match="Only the audited"):
        release.identity(tmp_path, "main")


# --- the release table (2026-10-14): a later audited tag, never a guessed one ---------------------------------

import os
import urllib.error as _urlerror


#: A synthetic release that is declared and not pinned. The real table is never changed for it: it is added to the table of the
#: process under test (`monkeypatch.setitem`), or to a copy of the tool (`_tool_declaring_an_unpinned_release`).
UNPINNED = "v0.0.2"
UNPINNED_RECORD = {"version": "0.0.2", "commit": None, "tag_object": None, "main_has_no_package_diff": True, "prerelease": True}


def test_unknown_and_unpinned_releases_are_refused(monkeypatch):
    with pytest.raises(AssertionError, match="Only the audited"):
        release.release_for("v9.9.9")
    # a release that is declared and not pinned is refused until its two identity values are added. The example is a synthetic
    # one: v0.4.0a1 was the example until its identity was pinned (130fbb4e), and a test that names a real release as "the
    # unpinned one" goes red the day that release is pinned
    monkeypatch.setitem(release.RELEASES, UNPINNED, dict(UNPINNED_RECORD))
    with pytest.raises(AssertionError, match="not pinned"):
        release.release_for(UNPINNED)
    # one value is not a pin: each of the two identity values missing alone is refused as well
    for half in ({"commit": "c" * 40}, {"tag_object": "d" * 40}):
        monkeypatch.setitem(release.RELEASES, UNPINNED, {**UNPINNED_RECORD, **half})
        with pytest.raises(AssertionError, match="not pinned"):
            release.release_for(UNPINNED)
    # the other direction: the same record with both identity values is accepted, so the refusal is the missing pin and nothing else
    monkeypatch.setitem(release.RELEASES, UNPINNED, {**UNPINNED_RECORD, "commit": "c" * 40, "tag_object": "d" * 40})
    assert release.release_for(UNPINNED)["version"] == "0.0.2"
    # the real pre-release is pinned: it is accepted, with both identity values
    pinned = release.release_for("v0.4.0a1")
    assert re.fullmatch(r"[0-9a-f]{40}", pinned["commit"]) and re.fullmatch(r"[0-9a-f]{40}", pinned["tag_object"])
    # v0.4.0 is reserved for the closure of P3 and is not declared at all
    with pytest.raises(AssertionError, match="Only the audited"):
        release.release_for("v0.4.0")
    monkeypatch.setitem(release.RELEASES, "v0.0.1", {"version": "0.0.1", "commit": None, "tag_object": None,
                                                    "main_has_no_package_diff": True, "prerelease": False})
    with pytest.raises(AssertionError, match="not pinned"):
        release.release_for("v0.0.1")


def test_v010_keeps_its_pins_and_its_file_set():
    r = release.release_for("v0.1.0")
    assert (r["commit"], r["tag_object"], r["version"]) == (release.COMMIT, release.TAG_OBJECT, "0.1.0")
    assert r["main_has_no_package_diff"] is True and r["prerelease"] is False
    assert release.files_of("0.1.0") == release.FILES


def test_v040a1_is_a_declared_pre_release_with_a_pep440_version(monkeypatch):
    """The pre-release is declared, is a pre-release, and its version string is a PEP 440 pre-release version (what
    `pip install --pre` selects), whose file names are the ones setuptools writes."""
    r = release.RELEASES["v0.4.0a1"]
    assert r["version"] == "0.4.0a1" and r["prerelease"] is True and r["main_has_no_package_diff"] is True
    assert re.fullmatch(r"\d+\.\d+\.\d+(a|b|rc)\d+", r["version"])
    assert release.files_of("0.4.0a1") == {"hoh-0.4.0a1-py3-none-any.whl", "hoh-0.4.0a1.tar.gz"}
    # the tag is the version with a leading v, and the workflow derives the version from the tag that way
    assert "v0.4.0a1".removeprefix("v") == r["version"]
    monkeypatch.setitem(release.RELEASES, "v0.4.0a1", {**r, "commit": "c" * 40, "tag_object": "d" * 40})
    assert release.release_for("v0.4.0a1")["prerelease"] is True


def _tool_declaring_an_unpinned_release(tmp_path):
    """A copy of `tools/distribution_release.py` whose `RELEASES` also holds `UNPINNED`: the program is run as a program, so a
    table changed in this process does not reach it. The table line is replaced exactly once, or this fails loudly."""
    source = Path(release.__file__).read_text(encoding="utf-8")
    assert source.count("RELEASES = {\n") == 1
    copy = tmp_path / "distribution_release_copy.py"
    copy.write_text(source.replace("RELEASES = {\n", f"RELEASES = {{\n    {UNPINNED!r}: {UNPINNED_RECORD!r},\n"), encoding="utf-8")
    return copy


def test_the_prerelease_command_prints_what_the_github_release_must_say(tmp_path):
    """`prerelease --release-ref` is what the workflow asks before it compares the GitHub release: false for v0.1.0, and a refusal (not a
    guess) for a release whose identity is not pinned yet."""
    tool = release.__file__
    ok = subprocess.run([sys.executable, tool, "prerelease", "--release-ref", "v0.1.0"], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "false"
    # the pinned pre-release answers true: what the workflow compares the GitHub release with
    pre = subprocess.run([sys.executable, tool, "prerelease", "--release-ref", "v0.4.0a1"], capture_output=True, text=True)
    assert pre.returncode == 0 and pre.stdout.strip() == "true", pre.stderr
    # a release that is declared and not pinned is a refusal, not a guess: the tool itself, run as a program, with a synthetic
    # unpinned release added to a copy of its table
    copy = _tool_declaring_an_unpinned_release(tmp_path)
    unpinned = subprocess.run([sys.executable, str(copy), "prerelease", "--release-ref", UNPINNED], capture_output=True, text=True)
    assert unpinned.returncode != 0 and "not pinned" in unpinned.stderr and unpinned.stdout == ""
    # and the copy is a faithful one: it answers a pinned release as the original does
    same = subprocess.run([sys.executable, str(copy), "prerelease", "--release-ref", "v0.1.0"], capture_output=True, text=True)
    assert same.returncode == 0 and same.stdout.strip() == "false"


def test_the_file_set_and_the_index_url_follow_the_version(bundle, monkeypatch):
    monkeypatch.setitem(release.RELEASES, "v0.4.0a1", {**release.RELEASES["v0.4.0a1"], "commit": "c" * 40, "tag_object": "d" * 40})
    assert release.files_of("0.4.0a1") == {"hoh-0.4.0a1-py3-none-any.whl", "hoh-0.4.0a1.tar.gz"}
    seen = []

    def absent(url, *args, **kwargs):
        seen.append(url)
        raise _urlerror.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(release.urllib.request, "urlopen", absent)
    # a bundle holding the 0.1.0 files is refused for 0.4.0a1 before any request is made
    with pytest.raises(AssertionError):
        release.index(bundle, "pypi", True, "v0.4.0a1")
    assert seen == []
    dist = bundle / "dist"
    hashes = {}
    for name in release.files_of("0.4.0a1"):
        (dist / name).write_bytes(name.encode())
        hashes[name] = hashlib.sha256(name.encode()).hexdigest()
    (bundle / "SHA256SUMS.txt").write_text("".join(f"{v}  {k}\n" for k, v in hashes.items()))
    release.index(bundle, "pypi", True, "v0.4.0a1")
    assert seen == ["https://pypi.org/pypi/hoh/0.4.0a1/json"]


import tomllib
from pathlib import Path

# the licence identifier is read from the repository's own pyproject.toml, which the gate compares against
_LICENSE = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["license"]

PYPROJECT = """[project]
name = "hoh"
version = "{version}"
requires-python = ">=3.11"
license = "{license}"
dependencies = ["pydantic>=2.0"]

[project.scripts]
hoh = "hoh.cli:main"

[project.urls]
Repository = "https://github.com/SKZL-AI/veriharness"
"""


def _git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": os.environ["PATH"], "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _tagged(tmp_path, monkeypatch, *, main_changes_src):
    """A repository tagged v0.9.0 (annotated, pinned in the table) whose `origin/main` is the tag, or the tag plus a source change."""
    repo = tmp_path / "src-repo"
    (repo / "src" / "hoh").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src" / "hoh" / "a.py").write_text("x = 1\n")
    (repo / "pyproject.toml").write_text(PYPROJECT.format(version="0.9.0", license=_LICENSE))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "-a", "-m", "v0.9.0", "v0.9.0")
    monkeypatch.setitem(release.RELEASES, "v0.9.0", {
        "version": "0.9.0", "commit": _git(repo, "rev-parse", "v0.9.0^{}"), "tag_object": _git(repo, "rev-parse", "v0.9.0"),
        "main_has_no_package_diff": True, "prerelease": False})
    if main_changes_src:
        (repo / "src" / "hoh" / "a.py").write_text("x = 2\n")
        _git(repo, "commit", "-q", "-am", "main moves the source")
        main = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/main", main)
        _git(repo, "checkout", "-q", "--detach", "v0.9.0^{}")
    else:
        _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "v0.9.0^{}"))
    return repo


def test_a_pinned_tag_with_a_main_that_only_adds_tooling_is_accepted(tmp_path, monkeypatch, capsys):
    repo = _tagged(tmp_path, monkeypatch, main_changes_src=False)
    release.identity(repo, "v0.9.0")
    assert '"identity": "VERIFIED"' in capsys.readouterr().out


def test_a_main_that_changes_the_source_is_refused(tmp_path, monkeypatch):
    repo = _tagged(tmp_path, monkeypatch, main_changes_src=True)
    with pytest.raises(AssertionError):
        release.identity(repo, "v0.9.0")
