#!/usr/bin/env python3
"""Deterministic merge gate over required independent reviews (orchestrator tooling, 2026-10-07).

Cross-check of the product's gate (`hoh merge-gate check`, src/hoh/mergegate.py): an independent, much
smaller implementation of the same rule that shares no code with it, so that a slip in one is seen by the
other. Both must say MERGE_ELIGIBLE before the orchestrator merges (tools/merge_strand.sh). Run it from
the repository (or pass --repo). It reads the repository and writes only the receipt.

Introduced after the qaw8 slip: a merge carried one reviewer's minors while the other reviewer's report
held PROVEN findings. Aggregating verdicts must not depend on the orchestrator reading both files.

Usage:
  merge_gate.py --candidate <ref-or-sha> --base <ref-or-sha> --spec <path-in-candidate> \
                --review <id>=<report.md> [--review ...] --receipt <out.json> [--repo <dir>]

Every required report must end with this footer (last non-empty lines, exact keys, any order):
  VERDICT: RELEASE_YES | RELEASE_NO
  CANDIDATE: <full sha of the reviewed candidate commit>
  BASE: <full sha of the integration base>
  SPEC_SHA256: <sha256 of the spec file as reviewed>
  BLOCKING_OPEN: <integer>

Merge eligible only if every required review is present, unique, footer-complete, RELEASE_YES with
BLOCKING_OPEN 0, and its CANDIDATE/BASE/SPEC_SHA256 equal the values recomputed now. Anything else:
exit 1, NOT MERGE ELIGIBLE. The receipt names and digests every report and the aggregate verdict.

Mechanical re-bind (MG-RB1, 2026-10-14; the same rule as `hoh.mergegate.measure_carry`): a review whose
BASE is not the current base is still accepted when this tool measures, itself, that (1) BASE is a full
object id of a commit that is an ancestor of the current base, (2) at most CARRY_MAX_COMMITS commits lie
between them, (3) the candidate has exactly one merge base with the current base and changes at least one
path relative to it, (4) no commit between them touches such a path (merge commits included; a merge counts a
path of its diff against a parent only if the path also differs from BASE's, see `touched_between`, MG-RB3), and (5)
`git merge-tree --write-tree` of the base and the candidate is clean (in a private object store; directory
renames are conflicts). Anything else leaves the review stale. The receipt row then carries
`carried_forward` (BASE, current base, the commits between, the path counts and the overlap, empty).
Reports written before the footer existed are refused (fail closed); re-review or a footer added by the
same reviewer session is required.
"""
import argparse, ast, hashlib, io, json, os, re, subprocess, sys, tarfile, tempfile, time

KEYS = ("VERDICT", "CANDIDATE", "BASE", "SPEC_SHA256", "BLOCKING_OPEN")
REPO = "."
CARRY_MAX_COMMITS = 256
CARRY_MAX_BYTES = 4 * 1024 * 1024


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, check=True).stdout


def git_rc(*a, env=None):
    """(exit code, stdout bytes) of one git command; an unrunnable git is exit 255."""
    try:
        done = subprocess.run(["git", "-C", REPO, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                               *a], capture_output=True, stdin=subprocess.DEVNULL, timeout=120,
                              env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", **(env or {})})
    except (OSError, subprocess.SubprocessError):
        return 255, b""
    return done.returncode, done.stdout


def names(raw):
    return {n for n in raw.split(b"\0") if n}


def quoted(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def shown(paths):
    ordered = sorted(paths)
    text = ", ".join(ascii(n.decode("utf-8", "replace")) for n in ordered[:5])
    return text + (f" and {len(ordered) - 5} more" if len(ordered) > 5 else "")


def merge_tree(base, cand, want=(), blobs=None):
    """The tree id of a clean merge of `cand` into `base`, or None (conflicts, or git failed). Run in a
    private object store; directory renames are conflicts. The content of each path of `want` in the merged
    tree (None when absent) goes into `blobs` while the store exists."""
    rc, out = git_rc("rev-parse", "--path-format=absolute", "--git-path", "objects")
    if rc != 0:
        return None
    with tempfile.TemporaryDirectory(prefix="vh-merge-gate-", ignore_cleanup_errors=True) as store:
        env = {"GIT_OBJECT_DIRECTORY": store, "GIT_ALTERNATE_OBJECT_DIRECTORIES": quoted(out.decode().strip())}
        rc, tree = git_rc("-c", "merge.directoryRenames=conflict", "merge-tree", "--write-tree", "--name-only",
                          "--no-messages", base, cand, env=env)
        tree = tree.decode().split()
        if rc != 0 or len(tree) != 1 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree[0]):
            return None
        if blobs is not None:
            for path in want:
                blobs[path] = blob(tree[0], path, env)
    return tree[0]


SUITE_SCHEMA = "hoh-suite-receipt/1"
SUITE_KEYS = {"schema", "base", "candidate", "base_tree", "merged_tree", "counts", "new_failures", "accepted",
              "measured_at"}
OID = r"[0-9a-f]{40}|[0-9a-f]{64}"


def _no_dup(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def suite_check(path, base, cand, carried_tree):
    """(record, [problem text]) of the suite receipt at `path` against the base, the candidate, the base's tree
    and the merge tree measured now (or `carried_tree`, the one the carry measured). Same rule as the product:
    stale base, stale candidate, base tree, merged tree, and every new failure must be accepted by name."""
    rec = {"arg": path, "path": None, "sha256": None, "receipt": None, "actual_base_tree": None,
           "actual_merged_tree": None}
    try:
        real = os.path.realpath(path)
        rec["path"] = real
        if not os.path.exists(real):
            return rec, [f"SUITE_RECEIPT_MISSING: no suite receipt at {path!r}"]
        if not os.path.isfile(real):
            return rec, ["SUITE_RECEIPT_UNREADABLE: not a regular file"]
        if os.path.getsize(real) > 8 * 1024 * 1024:
            return rec, ["SUITE_RECEIPT_UNREADABLE: larger than 8 MiB"]
        raw = open(real, "rb").read()
    except OSError as exc:
        return rec, [f"SUITE_RECEIPT_UNREADABLE: {exc}"]
    rec["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup)
    except (ValueError, RecursionError):
        return rec, ["SUITE_RECEIPT_UNREADABLE: not JSON"]
    ok = isinstance(body, dict) and set(body) == SUITE_KEYS and body["schema"] == SUITE_SCHEMA
    ok = ok and all(isinstance(body[k], str) and re.fullmatch(OID, body[k]) for k in
                    ("base", "candidate", "base_tree", "merged_tree"))
    c = body.get("counts") if ok else None
    ok = ok and isinstance(c, dict) and set(c) == {"base_failed", "candidate_failed", "new"} and all(
        type(v) is int and v >= 0 for v in c.values())
    for k in ("new_failures", "accepted"):
        v = body.get(k) if ok else None
        ok = ok and isinstance(v, list) and len(set(map(str, v))) == len(v) and all(
            isinstance(n, str) and 0 < len(n) <= 1024 and not re.search(r"[\x00-\x1f\x7f]", n) for n in v)
    ok = ok and c["new"] == len(body["new_failures"]) and c["new"] >= c["candidate_failed"] - c["base_failed"]
    ok = ok and isinstance(body["measured_at"], str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", body["measured_at"]) is not None
    if not ok:
        return rec, ["SUITE_RECEIPT_UNREADABLE: not a well-formed suite receipt"]
    rec["receipt"] = body
    rc, out = git_rc("rev-parse", f"{base}^{{tree}}")
    base_tree = out.decode().strip() if rc == 0 and re.fullmatch(OID, out.decode().strip()) else None
    merged = carried_tree or merge_tree(base, cand)
    rec["actual_base_tree"], rec["actual_merged_tree"] = base_tree, merged
    out = []
    if body["base"] != base:
        out.append("SUITE_STALE_BASE: the suite was run for another base")
    if body["candidate"] != cand:
        out.append("SUITE_STALE_CANDIDATE: the suite was run for another candidate")
    if base_tree is None or body["base_tree"] != base_tree:
        out.append("SUITE_BASE_TREE_MISMATCH: the suite's base tree is not the base's tree")
    if merged is None:
        out.append("SUITE_MERGED_TREE_MISMATCH: there is no merge tree now")
    elif body["merged_tree"] != merged:
        out.append("SUITE_MERGED_TREE_MISMATCH: the suite was run on another merged tree")
    bad = [n for n in body["new_failures"] if n not in set(body["accepted"])]
    if bad:
        out.append(f"SUITE_NEW_FAILURES: {len(bad)} new failure(s) are not accepted")
    return rec, out


REGISTER = b"program/v3_3/REQUIREMENTS.json"
LEDGER = b"DOGFOOD_LEDGER.md"
RECORD_FILES = (REGISTER, LEDGER)
RECORD_PREFIXES = (b"dogfood/", b"program/v3_3/")
CARRY_MAX_CODE_FILES = 256
MATRIX_ARCHIVE_MAX = 64 * 1024 * 1024
MATRIX_TIMEOUT = 120
MATRIX_DRIVER = r"""
import ast, importlib.util, json, re, sys
from pathlib import Path
root, rows_file = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("cm", root / "tools" / "capability_matrix.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)
nodes = []
for path in sorted((root / "tests").rglob("*.py")):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        continue
    rel = path.relative_to(root).as_posix()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            nodes.append(f"{rel}::{node.name}")
        elif isinstance(node, ast.ClassDef):
            nodes.append(f"{rel}::{node.name}")
            nodes += [f"{rel}::{node.name}::{m.name}" for m in node.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]
collection = ("\n".join(nodes) + "\n", True)
symbols, graph = cm._symbols(), cm._import_graph()
out = {}
for row in json.loads(rows_file.read_text(encoding="utf-8")):
    kind = (row.get("probe") or {}).get("kind", "none")
    if kind not in ("none", "symbol", "method", "test"):
        out[row["id"]] = {"status": "NOT_COMPARABLE", "evidence": [f"probe kind {kind!r}"]}
        continue
    got = cm.classify(row, symbols, collection, {}, graph)
    out[row["id"]] = {"status": got["status"],
                      "evidence": [re.sub(r"(?<=\.py):[0-9]+", "", e) for e in got["evidence"]]}
sys.stdout.write(json.dumps(out, sort_keys=True))
"""


def blob(rev, path, env=None):
    """Content of `path` at `rev`; None when absent; raises ValueError when git fails or it is too big."""
    name = os.fsdecode(path)
    rc, out = git_rc("ls-tree", "-z", rev, "--", name, env=env)
    if rc != 0:
        raise ValueError(f"git ls-tree failed for {name}")
    if not out:
        return None
    rc, raw = git_rc("cat-file", "blob", f"{rev}:{name}", env=env)
    if rc != 0 or len(raw) > CARRY_MAX_BYTES:
        raise ValueError(f"{name} at {rev[:12]} cannot be read or is too big")
    return raw


def only_adds(old, new):
    """Every line of `old` is in `new`, in order."""
    if not old:
        return True
    wanted = iter(old.split(b"\n"))
    pending = next(wanted, None)
    for line in (new or b"").split(b"\n"):
        if pending is None:
            return True
        if line == pending:
            pending = next(wanted, None)
    return pending is None


def register(rev):
    return parse_register(blob(rev, REGISTER), rev[:12])


def parse_register(raw, where):
    if raw is None:
        return {"rows": {}, "order": [], "top": {}}
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup)
    except (ValueError, RecursionError):
        raise ValueError(f"{REGISTER.decode()} at {where} is not readable JSON")
    if not isinstance(body, dict) or not isinstance(body.get("requirements"), list):
        raise ValueError(f"{REGISTER.decode()} at {where} is not a register of requirements")
    rows, order = {}, []
    for row in body["requirements"]:
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or not rid or rid in rows:
            raise ValueError(f"{REGISTER.decode()} at {where} has a row without a distinct string id")
        rows[rid] = row
        order.append(rid)
    return {"rows": rows, "order": order, "top": {k: v for k, v in body.items() if k != "requirements"}}


def changed(old, new):
    c = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"))
    out = {i for i in set(old["rows"]) | set(new["rows"]) if c(old["rows"].get(i)) != c(new["rows"].get(i))}
    if [r for r in old["order"] if r in new["rows"]] != [r for r in new["order"] if r in old["rows"]]:
        out.add("<order>")
    out |= {f"<{k}>" for k in set(old["top"]) | set(new["top"]) if c(old["top"].get(k)) != c(new["top"].get(k))}
    return out


def is_code(p):
    return p.endswith(b".py") and ((p.startswith(b"src/hoh/") and p.count(b"/") == 2)
                                   or (p.startswith(b"tools/") and p.count(b"/") == 1))


def modules_at(rev):
    rc, out = git_rc("ls-tree", "-z", "--name-only", rev, "src/hoh/")
    if rc != 0:
        raise ValueError("git ls-tree failed")
    return {os.path.basename(n)[:-3] for n in out.decode("utf-8", "surrogateescape").split("\0")
            if n.endswith(".py") and n.count("/") == 2}


def imports_of(rev, path, modules):
    raw = blob(rev, path)
    if raw is None:
        return set()
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (ValueError, SyntaxError, RecursionError):
        raise ValueError(f"{os.fsdecode(path)} at {rev[:12]} is not python source")
    names_ = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names_.add(node.module.split(".")[-1])
            names_.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names_.update(a.name.split(".")[-1] for a in node.names)
    return names_ & modules


def coupling(base, cand, mine, seen):
    ours = sorted(p for p in mine if is_code(p))
    theirs = sorted(p for p in seen if is_code(p))
    if not ours and not theirs:
        return [], {}
    if max(len(ours), len(theirs)) > CARRY_MAX_CODE_FILES:
        raise ValueError(f"more than {CARRY_MAX_CODE_FILES} code files are touched on one side")
    mc, mb = modules_at(cand), modules_at(base)
    stem = lambda p: os.path.basename(os.fsdecode(p))[:-3]
    t_theirs = {stem(p) for p in theirs if p.startswith(b"src/hoh/")}
    t_ours = {stem(p) for p in ours if p.startswith(b"src/hoh/")}
    reasons = [f"{os.fsdecode(p)} (candidate) imports src/hoh/{m}.py, touched in between"
               for p in ours for m in sorted(imports_of(cand, p, mc) & t_theirs)]
    reasons += [f"{os.fsdecode(p)} (touched in between) imports src/hoh/{m}.py, which the candidate changes"
                for p in theirs for m in sorted(imports_of(base, p, mb) & t_ours)]
    return reasons, {"candidate_code_files": len(ours), "range_code_files": len(theirs)}


def matrix_reading(rev, cand, rows):
    rc, objs = git_rc("rev-parse", "--path-format=absolute", "--git-path", "objects")
    if rc != 0:
        raise ValueError("git failed (rev-parse)")
    with tempfile.TemporaryDirectory(prefix="vh-merge-gate-", ignore_cleanup_errors=True) as scratch:
        work = os.path.join(scratch)
        os.mkdir(os.path.join(work, "objects"))
        os.mkdir(os.path.join(work, "tree"))
        env = {"GIT_OBJECT_DIRECTORY": os.path.join(work, "objects"),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": quoted(objs.decode().strip())}
        rc, out = git_rc("-c", "merge.directoryRenames=conflict", "merge-tree", "--write-tree", "--no-messages",
                         rev, cand, env=env)
        lines = out.decode("utf-8", "replace").splitlines()
        if rc != 0 or len(lines) != 1 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", lines[0]):
            raise ValueError(f"the merge of the candidate into {rev[:12]} does not give one tree")
        shipped = ("src/hoh", "tools", "tests", "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json")
        rc, listing = git_rc("ls-tree", "-r", "-l", "-z", lines[0], "--", *shipped, env=env)
        if rc != 0:
            raise ValueError("the merge tree cannot be listed")
        total = 0
        for entry in listing.split(b"\0"):
            fields = entry.partition(b"\t")[0].split()
            if len(fields) == 4 and fields[3].isdigit():
                total += int(fields[3])
        if total > MATRIX_ARCHIVE_MAX:
            raise ValueError(f"the merge tree's code and tests are larger than {MATRIX_ARCHIVE_MAX} bytes")
        rc, tar_bytes = git_rc("archive", "--format=tar", lines[0], "--", *shipped, env=env)
        if rc != 0 or not hasattr(tarfile, "data_filter"):
            raise ValueError("the merge tree cannot be written out")
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
                tar.extractall(os.path.join(work, "tree"), filter="data")
        except (tarfile.TarError, OSError):
            raise ValueError("the merge tree could not be written out")
        tool = blob(rev, b"tools/capability_matrix.py")      # the base's classifier, never the candidate's
        if tool is None:
            raise ValueError("tools/capability_matrix.py does not exist at the base")
        open(os.path.join(work, "tool.new"), "wb").write(tool)
        os.replace(os.path.join(work, "tool.new"), os.path.join(work, "tree", "tools", "capability_matrix.py"))
        open(os.path.join(work, "driver.py"), "w", encoding="utf-8").write(MATRIX_DRIVER)
        open(os.path.join(work, "rows.json"), "w", encoding="utf-8").write(json.dumps(rows))
        try:
            done = subprocess.run([sys.executable, "-I", os.path.join(work, "driver.py"), os.path.join(work, "tree"),
                                   os.path.join(work, "rows.json")], cwd=os.path.join(work, "tree"),
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=MATRIX_TIMEOUT,
                                  env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError):
            raise ValueError("the matrix reading could not be run")
    if done.returncode != 0 or len(done.stdout) > 1024 * 1024:
        raise ValueError(f"the matrix reading failed at {rev[:12]}")
    try:
        got = json.loads(done.stdout.decode("utf-8"))
    except ValueError:
        raise ValueError("the matrix reading is not JSON")
    if not isinstance(got, dict) or set(got) != {r["id"] for r in rows}:
        raise ValueError("the matrix reading does not answer exactly the rows asked")
    return got


def matrix_compare(b, base, cand, rows):
    then, now = matrix_reading(b, cand, rows), matrix_reading(base, cand, rows)
    unfit = sorted(i for i in now if now[i]["status"] == "NOT_COMPARABLE")
    if unfit:
        return None, "its probe cannot be read here: " + ", ".join(unfit[:5])
    differ = sorted(i for i in then if then[i] != now[i])
    if differ:
        return None, "; ".join(f"{i}: {then[i]['status']} -> {now[i]['status']}"
                               + ("" if then[i]["status"] != now[i]["status"] else " (evidence differs)")
                               for i in differ[:5])
    digest = hashlib.sha256(json.dumps(then, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"rows": sorted(then), "digest": digest, "statuses": {i: then[i]["status"] for i in sorted(then)}}, ""


def touched_between(b, base, root, merges):
    """The paths of the commits in `b..base`, whole and byte for byte, or None (git failed, or too many).
    A merge commit counts a path of its diff against a parent only if its version of the path also differs
    from `b`'s -- the diff against a parent that forked before `b` lists work the reviewers saw -- and only
    when `b` is `root` (the candidate's merge base) or a descendant of it; otherwise `log -m` as before."""
    quiet = ("-c", "log.showSignature=false", "-c", "diff.ignoreSubmodules=none")
    if not merges or git_rc("merge-base", "--is-ancestor", root, b)[0] != 0:
        rc, out = git_rc(*quiet, "log", "-m", "--no-renames", "--name-only", "-z", "--format=", f"{b}..{base}")
        return names(out) if rc == 0 and len(out) <= CARRY_MAX_BYTES else None
    seen, used = set(), 0

    def take(*a):
        nonlocal used
        rc, out = git_rc(*quiet, *a)
        used += len(out)
        if rc != 0 or used > CARRY_MAX_BYTES:
            raise ValueError("unavailable or too many")
        return names(out)

    try:
        seen = take("log", "--no-merges", "--no-renames", "--name-only", "-z", "--format=", f"{b}..{base}")
        for merge, *parents in merges:
            since = take("diff-tree", "-r", "--no-renames", "--name-only", "-z", b, merge)
            for parent in parents:
                seen |= take("diff-tree", "-r", "--no-renames", "--name-only", "-z", parent, merge) & since
    except ValueError:
        return None
    return seen


def carried(cand, base, b):
    try:
        return _carried(cand, base, b)
    except ValueError as exc:
        return None, f"not measurable: {exc}"


def _carried(cand, base, b):
    """(record, "") when a review bound to `b` stands for `base`, else (None, why). Fail closed."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", b):
        return None, "reviewed base is not a full object id"
    rc, out = git_rc("rev-parse", "--verify", "--quiet", b + "^{commit}")
    if rc != 0 or out.decode().strip() != b:
        return None, "reviewed base is not a commit"
    rc, _ = git_rc("merge-base", "--is-ancestor", b, base)
    if rc != 0:
        return None, "reviewed base is not an ancestor of the base" if rc == 1 else "git failed (merge-base)"
    rc, out = git_rc("rev-list", f"--max-count={CARRY_MAX_COMMITS + 1}", "--parents", f"{b}..{base}")
    listed = [line.split() for line in out.decode().splitlines()]
    between = [line[0] for line in listed]
    merges = [line for line in listed if len(line) > 2]
    if rc != 0 or len(between) > CARRY_MAX_COMMITS:
        return None, f"more than {CARRY_MAX_COMMITS} commits between (or git failed)"
    rc, out = git_rc("merge-base", "--all", base, cand)
    roots = out.decode().split()
    if rc not in (0, 1):
        return None, "git failed (merge-base)"
    if not roots:
        return None, "the candidate has no merge base with the base"
    if len(roots) > 1:
        return None, "the candidate has more than one merge base with the base: not measured"
    rc, out = git_rc("diff-tree", "-r", "--no-renames", "--name-only", "-z", roots[0], cand)
    mine = names(out)
    if rc != 0 or len(out) > CARRY_MAX_BYTES:
        return None, "the candidate's changed paths are unavailable or too many"
    if not mine:
        return None, "the candidate changes no path relative to its merge base: nothing to measure"
    seen = touched_between(b, base, roots[0], merges)
    if seen is None:
        return None, "the paths of the commits between are unavailable or too many"
    overlap, notes, edited, new_rows = set(mine & seen), {}, [], {}
    if REGISTER in mine:
        new = register(cand)
        new_rows = new["rows"]
        theirs_rows = set()
        ours_rows = changed(register(roots[0]), new)
        edited = sorted(i for i in ours_rows if i in new["rows"])
        if REGISTER in overlap:
            theirs_rows = changed(register(b), register(base))
            shared = ours_rows & theirs_rows
            if shared:
                return None, (f"{len(shared)} register row(s) the candidate changes are also changed by the "
                              f"commits in between: {', '.join(sorted(shared)[:5])}"
                              + (f" and {len(shared) - 5} more" if len(shared) > 5 else ""))
            overlap.discard(REGISTER)
        notes["register_rows"] = {"candidate": sorted(ours_rows), "range": sorted(theirs_rows), "overlap": []}
    ledger_pending = False
    if LEDGER in overlap and all(p in RECORD_FILES or p.startswith(RECORD_PREFIXES) for p in mine):
        overlap.discard(LEDGER)             # the only tolerated overlap, and only when append-only (below)
        ledger_pending = True
    if overlap:
        return None, (f"{len(overlap)} path(s) the candidate changes are touched by the commits in "
                      f"between: {shown(overlap)}")
    reasons, coupled = coupling(base, cand, mine, seen)
    if reasons:
        return None, (f"{len(reasons)} import coupling(s) between the candidate and the commits in between: "
                      + "; ".join(reasons[:5]) + (f" and {len(reasons) - 5} more" if len(reasons) > 5 else ""))
    if coupled:
        notes["imports"] = coupled
    merged_blobs = {}
    tree = merge_tree(base, cand, ([LEDGER] if ledger_pending else []) + ([REGISTER] if REGISTER in mine else []),
                      merged_blobs)
    if tree is None:
        return None, "merging the candidate into the base conflicts (or git failed)"
    if REGISTER in mine:
        parse_register(merged_blobs.get(REGISTER), "the merge tree")
    if ledger_pending:
        m_, c_, b_, h_ = (blob(r, LEDGER) for r in (roots[0], cand, b, base))
        mg = merged_blobs.get(LEDGER)
        if not (only_adds(m_, c_) and only_adds(b_, h_) and only_adds(c_, mg) and only_adds(h_, mg)):
            return None, ("1 path(s) the candidate changes are touched by the commits in between: "
                          f"{ascii(os.fsdecode(LEDGER))} (the ledger is carried over only when both sides only "
                          "add lines and the merged file keeps every line of both)")
        notes["records_only_overlap_tolerated"] = [os.fsdecode(LEDGER)]
    if edited:
        matrix, why = matrix_compare(b, base, cand, [new_rows[i] for i in edited])
        if matrix is None:
            return None, ("the register rows the candidate edits do not read the same on the reviewers' "
                          f"merge tree and on the current one: {why}")
        notes["matrix"] = matrix
    return {"review_base": b, "base": base, "commits": between, "merge_base": roots[0],
            "candidate_paths": len(mine), "touched_paths": len(seen), "overlap": [],
            "merge_tree": "clean", "merged_tree": tree, **notes}, ""


def footer(text):
    found = {}
    for line in [l for l in text.splitlines() if l.strip()][-12:]:
        m = re.match(r"^\s*([A-Z0-9_]+):\s*(\S+)\s*$", line)
        if m and m.group(1) in KEYS:
            if m.group(1) in found:
                return None, f"duplicate footer key {m.group(1)}"
            found[m.group(1)] = m.group(2)
    missing = [k for k in KEYS if k not in found]
    return (found, None) if not missing else (None, f"footer incomplete, missing {missing}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--review", action="append", required=True)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--suite-receipt", default=None)
    a = ap.parse_args()
    global REPO
    REPO = a.repo
    cand = git("rev-parse", "--verify", a.candidate + "^{commit}").strip()
    base = git("rev-parse", "--verify", a.base + "^{commit}").strip()
    spec_sha = hashlib.sha256(subprocess.run(["git", "-C", REPO, "show", f"{cand}:{a.spec}"],
                                             capture_output=True, check=True).stdout).hexdigest()
    problems, rows, ids, paths = [], [], set(), set()
    for item in a.review:
        rid, _, path = item.partition("=")
        if not rid or not path:
            problems.append(f"bad --review {item!r}"); continue
        real = os.path.realpath(path)
        if rid in ids or real in paths:
            problems.append(f"{rid}: review id or report file used twice (collision)"); continue
        ids.add(rid); paths.add(real)
        if not os.path.isfile(real):
            problems.append(f"{rid}: report missing: {path}"); continue
        data = open(real, "rb").read()
        f, err = footer(data.decode("utf-8", "replace"))
        row = {"review": rid, "report": real, "report_sha256": hashlib.sha256(data).hexdigest(),
               "mtime": os.path.getmtime(real), "footer": f}
        rows.append(row)
        if err:
            problems.append(f"{rid}: {err}"); continue
        before = len(problems)
        if f["VERDICT"] not in ("RELEASE_YES", "RELEASE_NO"):
            problems.append(f"{rid}: ambiguous VERDICT {f['VERDICT']!r}")
        elif f["VERDICT"] != "RELEASE_YES":
            problems.append(f"{rid}: VERDICT {f['VERDICT']}")
        if not f["BLOCKING_OPEN"].isdigit() or int(f["BLOCKING_OPEN"]) != 0:
            problems.append(f"{rid}: BLOCKING_OPEN {f['BLOCKING_OPEN']}")
        if f["CANDIDATE"] != cand:
            problems.append(f"{rid}: reviewed candidate {f['CANDIDATE'][:12]} != {cand[:12]} (stale)")
        stale_base = f["BASE"] != base
        if f["SPEC_SHA256"] != spec_sha:
            problems.append(f"{rid}: reviewed spec digest differs (stale)")
        if stale_base:
            record, why = (None, "other problems") if len(problems) > before else carried(cand, base, f["BASE"])
            if record:
                row["carried_forward"] = record
            else:
                problems.append(f"{rid}: reviewed base {f['BASE'][:12]} != {base[:12]} (stale; not carried: {why})")
    carried_rows = [r["carried_forward"] for r in rows if "carried_forward" in r and not problems]
    suite = None
    if a.suite_receipt is not None:
        suite, found = suite_check(a.suite_receipt, base, cand,
                                   carried_rows[0]["merged_tree"] if carried_rows else None)
        problems.extend(f"suite: {p}" for p in found)
    elif any("carried_forward" in r for r in rows):
        problems.append("suite: SUITE_RECEIPT_MISSING: a carried review stands only with a suite receipt "
                        "bound to this merge (--suite-receipt)")
    eligible = not problems and len(rows) == len(a.review)
    receipt = {"schema": "vh-merge-gate/1", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "candidate": cand, "base": base, "spec": a.spec, "spec_sha256": spec_sha,
               "required_reviews": [r.partition("=")[0] for r in a.review], "reviews": rows,
               "aggregate": "MERGE_ELIGIBLE" if eligible else "NOT_MERGE_ELIGIBLE", "problems": problems}
    if suite is not None:
        receipt["suite"] = suite
    with open(a.receipt, "x") as out:
        json.dump(receipt, out, indent=2)
    print(receipt["aggregate"], *problems, sep="\n  ")
    sys.exit(0 if eligible else 1)


if __name__ == "__main__":
    main()
