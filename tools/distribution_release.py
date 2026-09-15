"""Fail-closed checks for the immutable v0.1.0 distribution.

This tool never builds, uploads, or changes a Git ref. Publish jobs consume
the one artifact bundle checked here; an existing index release is accepted
only when its complete filename/digest set is identical.
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


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args])


def metadata(raw):
    data = BytesParser().parsebytes(raw)
    assert data["Name"] == "hoh" and data["Version"] == "0.1.0"
    assert data["Requires-Python"] == ">=3.11"
    assert data["License-Expression"] == "MIT"
    assert "pydantic>=2.0" in data.get_all("Requires-Dist", [])
    assert f"Repository, {REPOSITORY}" in data.get_all("Project-URL", [])
    assert data["Description-Content-Type"] == "text/markdown"
    assert "VeriHarness" in data.get_payload()


def identity(source, release_ref):
    assert release_ref == TAG, "Only the audited immutable release is allowed"
    for args, expected in ((["cat-file", "-t", TAG], "tag"),
                           (["rev-parse", TAG], TAG_OBJECT),
                           (["rev-parse", f"{TAG}^{{}}"], COMMIT),
                           (["rev-parse", "HEAD"], COMMIT)):
        assert git(source, *args).decode().strip() == expected
    assert not git(source, "status", "--porcelain", "--untracked-files=all").strip()
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hoh" and project["version"] == TAG[1:]
    assert project["requires-python"] == ">=3.11" and project["license"] == "MIT"
    assert project["dependencies"] == ["pydantic>=2.0"]
    assert project["scripts"] == {"hoh": "hoh.cli:main"}
    assert project["urls"]["Repository"] == REPOSITORY
    # main supplies only workflow tooling. Any package change needs a new audit.
    changed = git(source, "diff", "--name-only", TAG, "origin/main").decode().splitlines()
    packaging = {"pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in"}
    assert not [p for p in changed if p.startswith("src/") or p in packaging]
    print(json.dumps({"source_tag": TAG, "source_commit": COMMIT,
                      "tag_object": TAG_OBJECT, "identity": "VERIFIED",
                      "post_tag_package_diff": "empty"}))


def archive(source, bundle):
    dist = bundle / "dist"
    assert {p.name for p in dist.iterdir()} == FILES
    tracked = set(git(source, "ls-tree", "-r", "--name-only", TAG).decode().splitlines())
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
            relative = name if file.suffix == ".whl" else str(path.relative_to("hoh-0.1.0"))
            if file.suffix == ".whl":
                if relative.startswith("hoh/"):
                    original = "src/" + relative
                else:
                    assert relative.startswith("hoh-0.1.0.dist-info/")
                    leaf = relative.removeprefix("hoh-0.1.0.dist-info/")
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
                assert raw == git(source, "show", f"{TAG}:{original}"), f"Source mismatch: {name}"
            if relative.endswith(("METADATA", "PKG-INFO")):
                metadata(raw)
        expected_policy = {"hoh/policy/dangerous-patterns.txt", "hoh/policy/house-rules-patterns.txt"}
        if file.suffix == ".whl":
            assert expected_policy <= members.keys()
        else:
            assert {"hoh-0.1.0/src/" + p for p in expected_policy} <= members.keys()
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
    receipt = {"release": TAG, "package": "hoh", "version": "0.1.0",
               "source_tag": TAG, "source_commit": COMMIT, "tag_object": TAG_OBJECT,
               "workflow": ".github/workflows/publish-pypi.yml",
               "workflow_run_id": os.environ.get("GITHUB_RUN_ID"), "sha256": hashes}
    (bundle / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


def expected_hashes(bundle):
    hashes = {}
    for line in (bundle / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert name in FILES and name not in hashes
        assert len(digest) == 64
        hashes[name] = digest
    assert hashes.keys() == FILES
    for name, digest in hashes.items():
        assert hashlib.sha256((bundle / "dist" / name).read_bytes()).hexdigest() == digest
    return hashes


def index(bundle, index_name, allow_missing):
    expected = expected_hashes(bundle)
    domain = "pypi.org" if index_name == "pypi" else "test.pypi.org"
    for attempt in range(12):
        try:
            with urllib.request.urlopen(f"https://{domain}/pypi/hoh/0.1.0/json", timeout=30) as response:
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
    assert info["name"] == "hoh" and info["version"] == "0.1.0"
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
    parser.add_argument("command", choices=["identity", "archive", "index", "hashes"])
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--bundle", type=Path, default=Path("bundle"))
    parser.add_argument("--release-ref", default=TAG)
    parser.add_argument("--index", choices=["testpypi", "pypi"], default="testpypi")
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    if args.command == "identity":
        identity(args.source, args.release_ref)
    elif args.command == "archive":
        archive(args.source, args.bundle)
    elif args.command == "hashes":
        print(expected_hashes(args.bundle))
    else:
        index(args.bundle, args.index, args.allow_missing)
