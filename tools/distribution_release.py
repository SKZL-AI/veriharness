"""Fail-closed checks for an immutable, audited hoh distribution.

This tool never builds, uploads, or changes a Git ref. Publish jobs consume
the one artifact bundle checked here; an existing index release is accepted
only when its complete filename/digest set is identical.

A release is accepted only if it is listed in `RELEASES` with its pinned
identity (tag object, commit). `v0.1.0` is the audited release this file was
written for and keeps every check it always had. A later release is added by
a records commit that pins its identity *after* the tag exists, in the same
way the v0.1.0 pins were; a listed release whose identity is not pinned yet is
refused, not guessed.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile

TAG = "v0.1.0"
COMMIT = "a0b6af164be12817a69173ecc23f0b3e6c0ca2c7"
TAG_OBJECT = "f25ef7bffed2456b14c6d0fe488855d2c546ee55"
FILES = {"hoh-0.1.0-py3-none-any.whl", "hoh-0.1.0.tar.gz"}
REPOSITORY = "https://github.com/SKZL-AI/veriharness"

#: The audited releases. `commit` / `tag_object` of None means "declared, not
#: pinned yet": every command refuses it. `main_has_no_package_diff` is the
#: rule v0.1.0 was published under: the workflow file lives on `main`, which
#: may supply nothing but workflow tooling, so `src/` and the packaging files
#: of `main` must equal the tag's. `prerelease` is what the GitHub release of
#: the tag must say about itself (and what PyPI's PEP 440 version says: only a
#: pre-release version is installed by `pip install --pre`).
RELEASES = {
    "v0.1.0": {
        "version": "0.1.0",
        "commit": COMMIT,
        "tag_object": TAG_OBJECT,
        "main_has_no_package_diff": True,
        "prerelease": False,
    },
    # 0.4.0a1: a PRE-release of the v0.4 line (operator decision of 2026-10-20);
    # v0.4.0 stays reserved for the closure of phase P3 and is declared when it
    # exists. The two identity values are added after the tag exists; until
    # then every command refuses it.
    "v0.4.0a1": {
        "version": "0.4.0a1",
        "commit": "332e1db10d970584dc8a1f672912f5c11c6b4db0",
        "tag_object": "17e061153ef6d070dc39042549cb5d6e1a94fd45",
        "main_has_no_package_diff": True,
        "prerelease": True,
    },
    # 0.4.0a2: the next PRE-release of the v0.4 line (operator decision of 2026-10-21: PyPI starts with it; 0.4.0a1 was never uploaded).
    # Declared and NOT pinned: the two identity values are added by a records commit after the tag exists; until then every command
    # refuses it.
    "v0.4.0a2": {
        "version": "0.4.0a2",
        "commit": None,
        "tag_object": None,
        "main_has_no_package_diff": True,
        "prerelease": True,
    },
}


def release_for(release_ref):
    """The pinned record of `release_ref`, or an AssertionError. Never a default."""
    assert release_ref in RELEASES, f"Only the audited immutable releases are allowed: {sorted(RELEASES)}"
    release = RELEASES[release_ref]
    assert release["commit"] and release["tag_object"], (
        f"{release_ref} is declared but its identity is not pinned: pin the tag object and the commit "
        "in RELEASES after the tag exists, then publish")
    return release


def files_of(version):
    return {f"hoh-{version}-py3-none-any.whl", f"hoh-{version}.tar.gz"}


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args])


def metadata(raw, version="0.1.0"):
    data = BytesParser().parsebytes(raw)
    assert data["Name"] == "hoh" and data["Version"] == version
    assert data["Requires-Python"] == ">=3.11"
    assert data["License-Expression"] == "MIT"
    assert "pydantic>=2.0" in data.get_all("Requires-Dist", [])
    assert f"Repository, {REPOSITORY}" in data.get_all("Project-URL", [])
    assert data["Description-Content-Type"] == "text/markdown"
    assert "VeriHarness" in data.get_payload()


def identity(source, release_ref):
    release = release_for(release_ref)
    tag, commit, tag_object, version = release_ref, release["commit"], release["tag_object"], release["version"]
    for args, expected in ((["cat-file", "-t", tag], "tag"),
                           (["rev-parse", tag], tag_object),
                           (["rev-parse", f"{tag}^{{}}"], commit),
                           (["rev-parse", "HEAD"], commit)):
        assert git(source, *args).decode().strip() == expected
    assert not git(source, "status", "--porcelain", "--untracked-files=all").strip()
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hoh" and project["version"] == version
    assert project["requires-python"] == ">=3.11" and project["license"] == "MIT"
    assert project["dependencies"] == ["pydantic>=2.0"]
    assert project["scripts"] == {"hoh": "hoh.cli:main"}
    assert project["urls"]["Repository"] == REPOSITORY
    if release["main_has_no_package_diff"]:
        # main supplies only workflow tooling. Any package change needs a new audit.
        changed = git(source, "diff", "--name-only", tag, "origin/main").decode().splitlines()
        packaging = {"pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in"}
        assert not [p for p in changed if p.startswith("src/") or p in packaging]
    print(json.dumps({"source_tag": tag, "source_commit": commit,
                      "tag_object": tag_object, "identity": "VERIFIED",
                      "post_tag_package_diff": "empty"}))


def archive(source, bundle, release_ref=TAG):
    release = release_for(release_ref)
    tag, commit, tag_object, version = release_ref, release["commit"], release["tag_object"], release["version"]
    files = files_of(version)
    dist = bundle / "dist"
    assert {p.name for p in dist.iterdir()} == files
    tracked = set(git(source, "ls-tree", "-r", "--name-only", tag).decode().splitlines())
    inventory = {}
    for file in sorted(dist.iterdir()):
        if file.suffix == ".whl":
            with zipfile.ZipFile(file) as z:
                members = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
        else:
            with tarfile.open(file) as t:
                assert all(m.isfile() or m.isdir() for m in t.getmembers())
                members = {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
        for name, raw in members.items():
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts and "\\" not in name
            relative = name if file.suffix == ".whl" else str(path.relative_to(f"hoh-{version}"))
            if file.suffix == ".whl":
                if relative.startswith("hoh/"):
                    original = "src/" + relative
                else:
                    assert relative.startswith(f"hoh-{version}.dist-info/")
                    leaf = relative.removeprefix(f"hoh-{version}.dist-info/")
                    assert leaf in {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt", "licenses/LICENSE"}
                    original = "LICENSE" if leaf == "licenses/LICENSE" else None
            else:
                original = relative if relative in tracked else None
                if original is None:
                    assert relative in {"PKG-INFO", "setup.cfg"} or relative.startswith("src/hoh.egg-info/")
                if original:
                    assert relative in {"README.md", "LICENSE", "pyproject.toml"} or relative.startswith(("src/", "tests/"))
            if original:
                assert original in tracked
                assert raw == git(source, "show", f"{tag}:{original}"), f"Source mismatch: {name}"
            if relative.endswith(("METADATA", "PKG-INFO")):
                metadata(raw, version)
        expected_policy = {"hoh/policy/dangerous-patterns.txt", "hoh/policy/house-rules-patterns.txt"}
        if file.suffix == ".whl":
            assert expected_policy <= members.keys()
        else:
            assert {f"hoh-{version}/src/" + p for p in expected_policy} <= members.keys()
        inventory[file.name] = sorted(members)
    # Reuse the repository's leak scanner on the exact source files represented
    # by the archives. Metadata is validated separately; RECORD is hash data.
    from export_manifest import scan_include_for_leaks
    archive_sources = [p for p in tracked if p.startswith(("src/", "tests/")) or
                       p in {"README.md", "LICENSE", "pyproject.toml"}]
    findings = scan_include_for_leaks([
        {"path": p, "decision": "INCLUDE", "rule": "public-docs" if p == "README.md" else "package"}
        for p in archive_sources], source)
    assert not findings, f"Archive source leak scan failed: {findings}"
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dist.iterdir())}
    (bundle / "SHA256SUMS.txt").write_text("".join(f"{v}  {k}\n" for k, v in hashes.items()))
    (bundle / "archive-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    receipt = {"release": tag, "package": "hoh", "version": version,
               "source_tag": tag, "source_commit": commit, "tag_object": tag_object,
               "workflow": ".github/workflows/publish-pypi.yml",
               "workflow_run_id": os.environ.get("GITHUB_RUN_ID"), "sha256": hashes}
    (bundle / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


def expected_hashes(bundle, files=FILES):
    hashes = {}
    for line in (bundle / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert name in files and name not in hashes
        assert len(digest) == 64
        hashes[name] = digest
    assert hashes.keys() == files
    for name, digest in hashes.items():
        assert hashlib.sha256((bundle / "dist" / name).read_bytes()).hexdigest() == digest
    return hashes


def index(bundle, index_name, allow_missing, release_ref=TAG):
    version = release_for(release_ref)["version"]
    expected = expected_hashes(bundle, files_of(version))
    domain = "pypi.org" if index_name == "pypi" else "test.pypi.org"
    for attempt in range(12):
        try:
            with urllib.request.urlopen(f"https://{domain}/pypi/hoh/{version}/json", timeout=30) as response:
                release = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            if allow_missing:
                print("ABSENT")
                output = os.environ.get("GITHUB_OUTPUT")
                if output:
                    with open(output, "a") as f:
                        f.write("exists=false\n")
                return
            if attempt == 11:
                raise
            time.sleep(10)
    actual = {f["filename"]: f["digests"]["sha256"] for f in release["urls"]}
    assert len(release["urls"]) == 2 and actual == expected, "Published files differ from canonical build"
    info = release["info"]
    assert info["name"] == "hoh" and info["version"] == version
    assert info["requires_python"] == ">=3.11"
    assert info.get("license_expression") == "MIT"
    assert info["project_urls"]["Repository"] == REPOSITORY
    for file in release["urls"]:
        assert not file["yanked"]
        with urllib.request.urlopen(file["url"], timeout=60) as response:
            digest = hashlib.sha256(response.read()).hexdigest()
        assert digest == expected[file["filename"]]
    (bundle / f"{index_name}-verification.json").write_text(json.dumps(release, indent=2) + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as f:
            f.write("exists=true\n")
    print(f"{index_name}: complete filename set, metadata and downloaded SHA-256 verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["identity", "archive", "index", "hashes", "prerelease"])
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--bundle", type=Path, default=Path("bundle"))
    parser.add_argument("--release-ref", default=TAG)
    parser.add_argument("--index", choices=["testpypi", "pypi"], default="testpypi")
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    if args.command == "identity":
        identity(args.source, args.release_ref)
    elif args.command == "archive":
        archive(args.source, args.bundle, args.release_ref)
    elif args.command == "prerelease":
        print("true" if release_for(args.release_ref)["prerelease"] else "false")
    elif args.command == "hashes":
        print(expected_hashes(args.bundle, files_of(release_for(args.release_ref)["version"])))
    else:
        index(args.bundle, args.index, args.allow_missing, args.release_ref)
