"""`hoh pin`: a spec-only pin commit, built by plumbing on a base the product resolved (A6 increment I6, plan row P5-38).

A reviewer or a run is started on a **pin**: the base commit plus the specification and its reference files, in one
commit that nobody commits to. Until now the orchestrator made it with a shell script. This module is the same
operation with its slips closed:

* the base is a **full commit id** that `commitref.resolve_commit` proved to be a commit of the repository (a short id,
  a ref name or a tag is refused, never expanded);
* the commit is built with **no index at all and no temporary file**: `ls-tree` of the base's directories, `mktree` bottom-up
  for the directories the pin touches, `commit-tree`. No branch, no index and no working tree is read for writing, and `HEAD`
  does not move;
* the pin is published as **`refs/pins/<name>`**, created only if absent (`update-ref <ref> <commit> ""`); an existing
  pin is never moved;
* a pin is **spec-only**: every file it adds is under `dogfood/` at a path the base does not have, and no file
  replaces or hides anything the base holds; only regular files of a bounded size and count, at plain relative paths, are taken;
* the message gets a trailer block the product writes: `Spec`, `Spec-SHA256` (the digest of the exact bytes pinned,
  which is what `hoh merge-gate` recomputes), `Base`, and the `Co-Authored-By` attribution the caller names. A message
  that already carries one of these keys is refused, so the digest cannot be forged;
* the specification (and the message and the co-author line) are checked for the **invisible characters**, **this user's home directory**
  and **scratch paths of an orchestrator or a drafter** that have each leaked into a spec in this build. A finding
  refuses the pin unless the caller names the kind with `--allow`, and then the receipt records that it was allowed.

It merges nothing, deletes nothing, and writes the receipt create-only (never replacing one).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .commitref import CommitRefused, git, require_new_ref, resolve_commit
from .durable_io import DurableWriteNotSynced, durable_create
from .mergegate import Outcome

SCHEMA = "hoh-pin/1"
REF_PREFIX = "refs/pins/"
#: Where a pin may add files: the specification directories of the repository.
PIN_PREFIX = "dogfood/"

#: What the check looks for. `encoding` (bytes that are not UTF-8, a NUL) can never be allowed.
KINDS = ("home-path", "scratch-path", "invisible")
ENCODING = "encoding"

MAX_FILES = 200
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MESSAGE_BYTES = 8192

#: The identity of a pin commit; the same the product uses for the commits of its runs.
IDENTITY = {"GIT_AUTHOR_NAME": "hoh", "GIT_AUTHOR_EMAIL": "hoh@localhost",
            "GIT_COMMITTER_NAME": "hoh", "GIT_COMMITTER_EMAIL": "hoh@localhost"}

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}")
_CO_AUTHOR = re.compile(r"[^<>\r\n]+ <[^<>\s@]+@[^<>\s]+>")
_TRAILER_KEY = re.compile(r"^(Spec|Spec-SHA256|Base|Co-Authored-By):", re.IGNORECASE | re.MULTILINE)
#: A scratch path of an orchestrator or a drafter: a session directory (`scratch/` and eight hex digits), the agent
#: temporary tree, a drafter's `specs-draft` directory, a Herdr worktree.
_SCRATCH = re.compile(r"(?<![0-9A-Za-z])scratch/[0-9a-f]{8}(?![0-9A-Za-z])|/tmp/claude-[0-9]+"
                      r"|(?<![0-9A-Za-z_-])specs-draft(?![0-9A-Za-z_-])|\.herdr/worktrees/")
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Zs", "Co", "Cs"})
_INVISIBLE_EXTRA = frozenset({0x3164, 0x115F, 0x1160, 0xFFA0, 0x2800})


class PinRefused(Exception):
    """The pin cannot be made: bad input, a finding, an existing name. Nothing was published."""


Finding = tuple[str, int, str]  # (kind, line, what)


def scan_text(data: bytes, *, home: str | None = None) -> list[Finding]:
    """The findings in a text, at most one per kind and line, in line order (pure).

    `home` is this user's home directory (the default is `~` expanded now). Bytes that are not UTF-8 or contain a NUL
    are an `encoding` finding and nothing else is looked at."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [(ENCODING, 0, "not valid UTF-8")]
    if "\x00" in text:
        return [(ENCODING, text[:text.index("\x00")].count("\n") + 1, "NUL byte")]
    home = os.path.expanduser("~") if home is None else home
    home_re = None
    if home and home.rstrip("/"):
        home_re = re.compile(r"(?<![0-9A-Za-z_.-])" + re.escape(home.rstrip("/")) + r"(?![0-9A-Za-z_.-])")
    found: list[Finding] = []
    for number, line in enumerate(text.split("\n"), 1):
        if home_re is not None and home_re.search(line):
            found.append(("home-path", number, "this user's home directory"))
        match = _SCRATCH.search(line)
        if match:
            found.append(("scratch-path", number, match.group(0)[:60]))
        for char in line:
            if char in "\t ":
                continue
            if (unicodedata.category(char) in _INVISIBLE_CATEGORIES or ord(char) in _INVISIBLE_EXTRA
                    or 0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF):
                found.append(("invisible", number, f"U+{ord(char):04X}"))
                break
    return found


def _refusing(findings: Sequence[Finding], allow: Sequence[str], what: str) -> list[Finding]:
    """The findings the caller did not allow; any `encoding` finding always counts."""
    open_ = [f for f in findings if f[0] == ENCODING or f[0] not in allow]
    if open_:
        shown = "; ".join(f"{kind} line {line}: {detail}" for kind, line, detail in open_[:5])
        raise PinRefused(f"{what} is not clean ({len(open_)} finding(s)): {shown}")
    return [f for f in findings if f[0] in allow]


def collect_files(root: str) -> list[tuple[str, bytes]]:
    """`(relative path, bytes)` for every file under `root`, sorted; each file is read once.

    Only regular files at plain relative paths are taken: a symlink, a device, a FIFO, a name with a character outside
    `A-Za-z0-9._+-`, a name that starts with a dot or a dash, or a directory entry that is not a directory or a file
    is a refusal; each file is opened without following links and read once. The bounds are `MAX_FILES`, `MAX_FILE_BYTES` and `MAX_TOTAL_BYTES`."""
    base = Path(root)
    if not base.is_dir() or base.is_symlink():
        raise PinRefused(f"--files-root {root!r} is not a directory")
    found: list[tuple[str, bytes]] = []
    total = 0
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        with os.scandir(base / rel_dir if rel_dir else base) as entries:
            listing = sorted(entries, key=lambda e: e.name)
        for entry in listing:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if _COMPONENT.fullmatch(entry.name) is None:
                raise PinRefused(f"{rel!r}: a name the pin does not take (letters, digits, '._+-', no leading dot)")
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                pending.append(rel)
            elif stat.S_ISREG(mode):
                size = entry.stat(follow_symlinks=False).st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(found) >= MAX_FILES:
                    raise PinRefused(f"{rel!r}: over the bounds of a pin ({MAX_FILES} files, "
                                     f"{MAX_FILE_BYTES} bytes a file, {MAX_TOTAL_BYTES} in all)")
                try:
                    fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError as exc:
                    raise PinRefused(f"{rel!r} cannot be read as a regular file: {exc.strerror}") from exc
                with os.fdopen(fd, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise PinRefused(f"{rel!r} changed while it was read")
                    data = handle.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise PinRefused(f"{rel!r}: over the bounds of a pin")
                found.append((rel, data))
            else:
                raise PinRefused(f"{rel!r} is neither a regular file nor a directory")
    if not found:
        raise PinRefused(f"--files-root {root!r} holds no file")
    found.sort()
    return found


def _paths_free_in_base(repo: str, base: str, paths: Sequence[str]) -> None:
    """Refuse a path the base already has, and a path whose parent the base holds as a file."""
    asks: list[str] = []
    for path in paths:
        parts = path.split("/")
        asks += [f"{base}:{'/'.join(parts[:i])}" for i in range(1, len(parts) + 1)]
    out = git(repo, "cat-file", "--batch-check", stdin=("\n".join(asks) + "\n").encode()).stdout.decode()
    kinds = {}
    for ask, row in zip(asks, out.splitlines()):
        kinds[ask.split(":", 1)[1]] = row.split()[1] if not row.endswith("missing") else None
    for path in paths:
        parts = path.split("/")
        if kinds[path] is not None:
            raise PinRefused(f"{path!r} already exists in the base: a pin only adds, it never replaces")
        for i in range(1, len(parts)):
            if kinds["/".join(parts[:i])] not in (None, "tree"):
                raise PinRefused(f"{path!r} would sit under {'/'.join(parts[:i])!r}, which the base has as a file")


def _nest(blobs: Sequence[tuple[str, str, int]]) -> dict:
    """`{name: blob id | {name: ...}}` for the paths of the pin."""
    root: dict = {}
    for path, oid, _ in blobs:
        *folders, leaf = path.split("/")
        node = root
        for folder in folders:
            node = node.setdefault(folder, {})
        node[leaf] = oid
    return root


def _write_tree(repo: str, base_tree: str | None, node: dict) -> str:
    """The tree id of `base_tree` with `node` added, written bottom-up with `ls-tree` and `mktree`: only the directories
    the pin touches are rewritten, every other entry (mode, type, id) is carried over unchanged, and no index and no
    temporary file is involved. A name the base already has as a non-directory, or a file where the base has a
    directory, is refused (the caller has checked it already)."""
    entries: dict[bytes, tuple[str, str, str]] = {}
    if base_tree is not None:
        for record in git(repo, "ls-tree", "-z", base_tree).stdout.split(b"\0"):
            if record:
                meta, _, name = record.partition(b"\t")
                mode, kind, oid = meta.decode("ascii").split()
                entries[name] = (mode, kind, oid)
    for name, child in node.items():
        key = name.encode("utf-8")
        old = entries.get(key)
        if isinstance(child, str):
            if old is not None:
                raise PinRefused(f"{name!r} already exists in the base: a pin only adds, it never replaces")
            entries[key] = ("100644", "blob", child)
        else:
            if old is not None and old[1] != "tree":
                raise PinRefused(f"{name!r} is a file in the base: a directory cannot take its place")
            entries[key] = ("040000", "tree", _write_tree(repo, old[2] if old else None, child))
    data = b"".join(f"{mode} {kind} {oid}\t".encode("ascii") + name + b"\0" for name, (mode, kind, oid) in entries.items())
    return git(repo, "mktree", "-z", "--missing", stdin=data).stdout.decode().strip()


def make_pin(*, repo: str, base: str, files_root: str, spec: str, name: str, message_file: str,
             co_author: str, allow: Sequence[str] = (), home: str | None = None) -> dict[str, Any]:
    """Build the pin and publish its ref; the receipt body. Raises `PinRefused` before anything is published."""
    if _NAME.fullmatch(name) is None or name.endswith(".lock") or ".." in name:
        raise PinRefused(f"--ref {name!r} is not a pin name (letters, digits, '.', '_' and '-')")
    unknown = sorted(set(allow) - set(KINDS))
    if unknown:
        raise PinRefused(f"--allow {', '.join(unknown)}: not a kind that can be allowed ({', '.join(KINDS)})")
    if _CO_AUTHOR.fullmatch(co_author or "") is None:
        raise PinRefused(f"--co-author {co_author!r} is not 'Name <address>'")
    try:
        base = resolve_commit(repo, "--base", base)
        ref = REF_PREFIX + name
        git(repo, "check-ref-format", ref)
        require_new_ref(repo, ref)
        try:
            raw_message = Path(message_file).read_bytes()
        except OSError as exc:
            raise PinRefused(f"--message-file {message_file!r}: {exc}") from exc
        if len(raw_message) > MAX_MESSAGE_BYTES or not raw_message.strip():
            raise PinRefused(f"--message-file {message_file!r} is empty or over {MAX_MESSAGE_BYTES} bytes")
        allowed_findings = _refusing(scan_text(raw_message, home=home), allow, "the message")
        allowed_findings += _refusing(scan_text(co_author.encode("utf-8", "surrogateescape"), home=home), allow, "--co-author")
        message = raw_message.decode("utf-8").rstrip("\n")
        if _TRAILER_KEY.search(message):
            raise PinRefused("the message already carries a Spec, Spec-SHA256, Base or Co-Authored-By line: "
                             "the product writes those")
        files = collect_files(files_root)
        outside = [path for path, _ in files if not path.startswith(PIN_PREFIX)]
        if outside:
            raise PinRefused(f"{outside[0]!r}: a pin adds files under {PIN_PREFIX} only")
        by_path = dict(files)
        if spec not in by_path:
            raise PinRefused(f"--spec {spec!r} is not one of the files under --files-root")
        allowed_findings += _refusing(scan_text(by_path[spec], home=home), allow, f"the specification {spec}")
        _paths_free_in_base(repo, base, [path for path, _ in files])

        blobs = []
        for path, data in files:
            oid = git(repo, "hash-object", "-w", "--stdin", stdin=data).stdout.decode().strip()
            blobs.append((path, oid, len(data)))
        spec_sha256 = hashlib.sha256(by_path[spec]).hexdigest()
        tree = _write_tree(repo, git(repo, "rev-parse", base + "^{tree}").stdout.decode().strip(), _nest(blobs))
        body = (message + f"\n\nSpec: {spec}\nSpec-SHA256: {spec_sha256}\nBase: {base}\n"
                f"Co-Authored-By: {co_author}\n")
        commit = git(repo, "commit-tree", tree, "-p", base, "-F", "-", stdin=body.encode(),
                     env=IDENTITY).stdout.decode().strip()
        git(repo, "update-ref", ref, commit, "")
    except CommitRefused as exc:
        raise PinRefused(str(exc)) from exc
    return {"schema": SCHEMA, "pin": commit, "tree": tree, "ref": ref, "base": base, "spec": spec,
            "spec_sha256": spec_sha256, "co_author": co_author,
            "files": [{"path": p, "blob": o, "bytes": n} for p, o, n in blobs],
            "allowed": sorted({kind for kind, _, _ in allowed_findings}),
            "message_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def run_pin(*, repo: str, base: str, files_root: str, spec: str, name: str, message_file: str,
            co_author: str, receipt: str, allow: Sequence[str] = ()) -> Outcome:
    """Exit 0: the pin exists and its receipt was written. Exit 2: refused, no pin, no ref, no receipt. Exit 3: the
    pin exists (the output names it) but the receipt could not be written."""
    try:
        target = Path(receipt)
        if os.path.lexists(target) or not target.parent.is_dir():
            raise PinRefused(f"--receipt {receipt}: exists already, or its directory does not")
        result = make_pin(repo=repo, base=base, files_root=files_root, spec=spec, name=name,
                          message_file=message_file, co_author=co_author, allow=allow)
    except PinRefused as exc:
        return Outcome(2, "", f"pin refused: {exc}\n")
    except Exception as exc:                      # noqa: BLE001 - a crash must not read as a pin
        return Outcome(2, "", f"pin refused: the command itself failed: {type(exc).__name__}: {exc}\n")
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    summary = (f"PIN: {result['pin']}\nTREE: {result['tree']}\nREF: {result['ref']}\n"
               f"SPEC_SHA256: {result['spec_sha256']}\n")
    warning = ""
    try:
        try:
            durable_create(target, text)
        except DurableWriteNotSynced as exc:
            warning = f"pin warning: the receipt is in place but its directory entry may not be durable ({exc.strerror})\n"
        if target.read_bytes() != text.encode("utf-8"):
            raise OSError("what was read back is not what was written")
    except (OSError, ValueError, RuntimeError) as exc:
        return Outcome(3, summary, f"pin made, receipt NOT written ({exc}); the pin stands at {result['ref']}\n")
    return Outcome(0, summary + f"RECEIPT: {receipt}\n", warning)
