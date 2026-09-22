#!/usr/bin/env python3
"""An end-to-end acceptance, recorded from raw captures and nothing else.

P1-16 is the first requirement whose proof is not a symbol and a test but a
real run: fresh clone -> documented install -> doctor -> a small verified
programme, accepted. The capability matrix must not read an operational board
to decide it (O197), so the proof is a durable record derived from a preserved
evidence directory, and this tool is the derivation -- run it again and the
record must come out the same, which is what `--check` asserts.

v3. Two adversarial rounds each refused the previous version with forged
evidence it still called satisfied (v1 and v2 are parked in `.archiv/`). The
rules, each one a hole a reviewer demonstrated:

* **Raw captures only.** `steps.jsonl`, written by
  `dogfood/p1-16/drive_acceptance.py` as it ran each step (argv, exit, full
  output). No hand-written summary is read. A step name that appears twice is
  refused -- the last row must not silently win.
* **Everything read lies inside the evidence directory**, and the directory
  holds no symlink: a record that depends on files outside the tree is not
  durable, and a link is how a run from elsewhere is spliced in.
* **The clone is of the published head and stays unmodified:** `git
  rev-parse HEAD` equals `git ls-remote` of main, and `git status
  --porcelain` is empty after the clone and after the run.
* **Every command is the one the cloned README documents**, token for token;
  a `<placeholder>` matches any value, the documented run id matches the
  driver's. That covers the install, the doctor, `hoh worktree`, `start`,
  `run` (strict, the approval policy, `--trust-worktree`) and `report`.
* **The run is this drive's run of this spec:** its run id is the driver's,
  its spec path lies in the fresh clone, its spec digest is the digest of the
  spec as cloned, and it was never amended.
* **Every criterion the spec names has a PASS verdict and a receipt**, the
  verdicts come from the accepted candidate's QA, receipts equal verdicts, and
  each receipt is bound to the accepted candidate, strict, verified from
  inside, without fallback.
* **The interpreter.** `observed_python3` (O203) is what PATH resolved
  `python3` to when the check's shell started -- not a record of every
  interpreter the command might invoke. So a receipt counts only if every
  simple command in it starts with an allowed program (`python3`, `test`,
  `[`) after assignments other than PATH, with no substitution; then a
  `python3` in it is the observed one, and that must be the fresh venv's.
  (v4 used a blocklist, and reviewer B walked around it with `env -i`.)
* **A pytest criterion shows pytest's own summary line** (`N passed ... in
  X.XXs`), which unittest never prints.
* **No intervention through HoH:** no unblock, resume, pause or amendment in
  the history, which must be present. What a person might have typed into a
  role's pane is *not* observable from this evidence and is not claimed.

v4 (reviewer A, round 3): each spec criterion's receipt must run the spec's
command (its tokens in order); exit-code masking (`|| true`, `; exit 0`) and
a pytest summary with failures or errors are refused; `command -p`, `which`
and command substitution count as choosing one's own interpreter; the README
and spec copies must have the git blob ids `git rev-parse HEAD:<path>` gave
in the clone; the run's repo must be the worktree the documented step
created, and its base commit the target's head; HOH_RUNS must be set on every
hoh step; a README that documents a step twice is refused.

v5 (both reviewers, round 4): matching the planner's check text to the spec
lost four rounds to shell grammar (`!`, an else branch, a second `-c`, `[`
operands). The spec binding is now **independent re-execution**: the drive
checks out the accepted commit in a clean clone of the target -- its tree
must be the accepted candidate's -- and runs each spec criterion verbatim,
`bash -c '<the spec's command>'`, under the fresh venv; each must exit 0, and
the pytest criterion must print a summary with no failed, error, skipped,
deselected, xfailed or xpassed. The receipts remain HoH's own evidence (strict,
bound, observed interpreter, no `!`, only harmless assignments); `carries()`
is kept for the record but no longer decides anything.

v6 (both reviewers, round 5): the re-execution ran agent-written code on the
host, in an environment nobody recorded (PYTHONPATH and BASH_ENV were live
in the operator's shell; pytest walked up into an unrelated pyproject.toml).
Now each spec criterion runs through the fresh install's own strict runner
(`RECHECK_SCRIPT`, from `python3 -I`), whose receipt must show exit 0,
verified strict isolation and the venv's interpreter; every step starts from
an allowlisted environment and records all of it; the recheck steps' argv and
cwd are pinned; and the evidence names the driver and this tool by their
committed blob ids, with no uncommitted change to either.

**What this record can and cannot show.** Every file here was written on
this machine. The only anchors outside it are the clone head and the README
and spec blob ids, all checkable against the published commit. The record
proves that the captures are complete and consistent with each other and
with that commit -- not that nobody edited them with that in mind. A quota
auto-unblock would count as an intervention: that errs toward refusing.

A field that cannot be read is recorded as missing, and a record with a
missing field does not satisfy the requirement.

Usage:
    python3 tools/acceptance_record.py --evidence dogfood/p1-16/<stamp> --out dogfood/p1-16/P1_16_ACCEPTANCE.json
    python3 tools/acceptance_record.py --evidence ... --out ... --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
PUBLISHED = "https://github.com/SKZL-AI/veriharness.git"
STRICT_MODES = ("auto", "dontAsk", "bypassPermissions")
DOCUMENTED_RUN_ID = "minimal-demo"
#: The operator variables a step may inherit (plus HERDR_*, which hoh reads to
#: find its own pane). PATH, VIRTUAL_ENV and HOH_RUNS are set per step.
ENV_BASE_KEYS = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL", "PATH",
                 "TMPDIR", "XDG_RUNTIME_DIR"}
ENV_STEP_KEYS = {"PATH", "VIRTUAL_ENV", "HOH_RUNS"}
#: How the drive re-executes one spec criterion: the fresh install's own
#: strict runner, from `python3 -I` so no PYTHON* variable reaches it. It
#: prints the receipt and the transcript as JSON. Kept here so the record can
#: require the step's argv to be exactly this.
RECHECK_SCRIPT = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from hoh import runner\n"
    "from hoh.contracts import AcceptanceCheck, Candidate\n"
    "from hoh.sandbox import Isolation\n"
    "cid, command, tree, sha = sys.argv[1:5]\n"
    "check = AcceptanceCheck(check_id=cid, description='spec criterion, re-run verbatim',\n"
    "                        command=command, expect_exit=0)\n"
    "cand = Candidate(candidate_id='recheck', repo_path=tree, commit=sha, tree_clean=True,\n"
    "                 tree_digest='recheck')\n"
    "receipt, log = runner.run_check(check, cand, run_id='recheck', iteration=1, attempt=1,\n"
    "                                cwd=Path(tree), isolation=Isolation.STRICT)\n"
    "print(json.dumps({'receipt': json.loads(receipt.model_dump_json()), 'log': log}))\n"
)
#: Which driver step each documented command is, by its leading tokens.
STEP_OF = {("pip", "install"): "install", ("python3", "tools/preflight.py"): "doctor",
           ("hoh", "worktree"): "worktree", ("hoh", "start"): "start",
           ("hoh", "run"): "run", ("hoh", "report"): "report"}
PYTEST_SUMMARY = re.compile(r"(?m)^=*\s*\d+ passed\b[^\n]* in \d+(\.\d+)?s\b")
INTERVENTION = re.compile(r"\bunblock|\bBLOCKED\b|\bresumed?\b|\bpaused?\b|specification amended",
                          re.I)
PYTEST_BAD = re.compile(r"\b(failed|errors?|xpassed|xfailed|skipped|deselected)\b")


def _inside(root: Path, path: Path) -> Path | None:
    try:
        p = path.resolve()
    except OSError:
        return None
    return p if p.is_relative_to(root) else None


def _read(root: Path, path: Path) -> str | None:
    p = _inside(root, path)
    if p is None or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _json(root: Path, path: Path) -> dict:
    text = _read(root, path)
    try:
        data = json.loads(text) if text is not None else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _symlinks(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def documented_commands(readme: str) -> list[list[str]]:
    """The shell commands in the README's ```sh blocks, as argv lists.
    Continuation lines are joined; comments and `export` lines are dropped."""
    out = []
    for block in re.findall(r"```sh\n(.*?)```", readme, re.S):
        joined = re.sub(r"\\\n\s*", " ", block)
        for line in joined.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("export "):
                continue
            out.append(shlex.split(line))
    return out


def _argv_matches(documented: list[str], actual: list[str], run_id: str | None) -> bool:
    if len(documented) != len(actual):
        return False
    for d, a in zip(documented, actual):
        if re.fullmatch(r"<[^>]+>", d):
            if not a:
                return False
        elif d == DOCUMENTED_RUN_ID:
            if a != run_id:
                return False
        elif d != a:
            return False
    return True


#: The only programs a counted criterion may start (reviewer B, round 3: a
#: blocklist of interpreter tricks missed `env -i python3`; an allowlist
#: cannot be walked around by a spelling nobody listed).
ALLOWED_PROGRAMS = {"python3", "test", "["}
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi"}
#: Environment assignments a counted command may make; PATH and PYTHONPATH
#: would choose another interpreter or other packages (reviewer A, r15).
ALLOWED_ASSIGNMENTS = {"PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "LC_ALL", "LANG"}
#: A separator after which the next word may be a keyword: `if a; then b; fi`.
_STRUCTURAL_AFTER_SEMI = {"then", "else", "elif", "fi"}


def command_problems(command: str) -> list[str]:
    """Why a criterion's command cannot be counted, or [] when it can.

    Every simple command in it must start -- after `VAR=value` assignments
    other than PATH -- with an allowed program, so a bare `python3` is the
    interpreter and `observed_python3` names it. Its exit code must be its
    checks': no `||`, no pipe, no `;` except the structural ones of
    `if ...; then ...; else ...; fi`, no `set +e`, no substitution.
    """
    if "$(" in command or "`" in command:
        return ["command substitution"]
    # An unquoted newline separates commands for the shell but is whitespace
    # to shlex: `python3 x<newline>echo ok` would read as one command whose
    # exit code is the checks' -- it is echo's.
    quote = None
    for i, ch in enumerate(command):
        if quote:
            if ch == quote and (quote == "'" or command[i - 1] != "\\"):
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            return ["an unquoted newline separates commands"]
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError as exc:
        return [f"unparseable: {exc}"]
    problems, start, i = [], True, 0
    while i < len(toks):
        t = toks[i]
        if t in ("||", "|", "&", "|&"):
            problems.append(f"'{t}' decides the exit code")
            start = True
        elif t == ";":
            if i + 1 < len(toks) and toks[i + 1] in _STRUCTURAL_AFTER_SEMI:
                pass
            else:
                problems.append("';' discards an exit code")
            start = True
        elif t == "&&":
            start = True
        elif start:
            if t in SHELL_KEYWORDS:
                pass
            elif t == "!":
                problems.append("'!' inverts the exit code")
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):
                if t.split("=", 1)[0] not in ALLOWED_ASSIGNMENTS:
                    problems.append(f"sets {t.split('=', 1)[0]}")
            else:
                if t not in ALLOWED_PROGRAMS:
                    problems.append(f"starts {t!r}, not one of {sorted(ALLOWED_PROGRAMS)}")
                start = False
        i += 1
    if re.search(r"\bset\s+\+e\b", command):
        problems.append("set +e")
    return problems


def _tokens(command: str) -> list[str]:
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    return [t.rstrip(";") for t in toks if t.rstrip(";")]


def carries(receipt_command: str, spec_command: str) -> bool:
    """Does the receipt's command run the spec's command? Its tokens must occur
    in order (a planner may add flags such as `-p no:cacheprovider`, or an
    environment assignment). Reviewer A, r1: matching criteria by id alone let
    a K1 whose command was `true` stand for the spec's K1."""
    want, have = _tokens(spec_command), _tokens(receipt_command)
    i = 0
    for t in have:
        if i < len(want) and t == want[i]:
            i += 1
    return bool(want) and i == len(want)


def git_blob(text: str | None) -> str | None:
    """The git blob id of a file's content -- what `git rev-parse HEAD:<path>`
    names, and what anyone can check against the published commit."""
    if text is None:
        return None
    raw = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def spec_criteria(spec: str) -> dict[str, str]:
    """`K1 -> command` as the spec states them."""
    out = {}
    for m in re.finditer(r"\*\*(K\d+)\b.*?Command: `([^`]+)`", spec, re.S):
        out[m.group(1)] = m.group(2)
    return out


def _recheck(step: dict | None, out: str | None) -> dict:
    """One re-executed spec criterion: the step as recorded and the receipt
    the fresh install's strict runner printed."""
    step = step or {}
    try:
        data = json.loads(out or "")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else {}
    iso = receipt.get("isolation") if isinstance(receipt.get("isolation"), dict) else {}
    log = data.get("log") if isinstance(data.get("log"), str) else ""
    m = PYTEST_SUMMARY.search(log)
    return {"argv": step.get("argv"), "cwd": step.get("cwd"), "exit": step.get("exit"),
            "path": (step.get("env_set") or {}).get("PATH"),
            "receipt_exit": receipt.get("exit_code"), "runner_ok": receipt.get("runner_ok"),
            "effective": iso.get("effective"),
            "verified_from_inside": iso.get("verified_from_inside"),
            "fallback_to_none": iso.get("fallback_to_none"),
            "observed_python3": iso.get("observed_python3"),
            "pytest_summary": m.group(0).strip() if m else None}


def derive(evidence: Path) -> dict:
    ev = Path(evidence)
    root = ev.resolve()
    driver = _json(root, ev / "driver.json")
    steps: dict[str, dict] = {}
    duplicates = []
    for line in (_read(root, ev / "steps.jsonl") or "").splitlines():
        try:
            s = json.loads(line)
        except ValueError:
            continue
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if name in steps:
            duplicates.append(name)
        out_rel = s.get("output")
        s["text"] = (_read(root, ev / out_rel)
                     if isinstance(out_rel, str) and not os.path.isabs(out_rel) else None)
        steps[name] = s

    def text(name):
        return (steps.get(name) or {}).get("text")

    run_id = driver.get("run_id")
    readme = _read(root, ev / "README.as-cloned.md") or ""
    documented = documented_commands(readme)
    argv_check = {}
    documented_twice = []
    for doc in documented:
        # `pip install` is run with the venv's own pip; compare from `install` on.
        key = ("pip", "install") if doc[:2] == ["pip", "install"] else tuple(doc[:2])
        name = STEP_OF.get(key)
        if not name:
            continue
        actual = list((steps.get(name) or {}).get("argv") or [])
        if name == "install" and actual:
            actual = ["pip"] + actual[1:] if actual[0].endswith("/pip") else actual
        if name in argv_check:
            documented_twice.append(name)
        argv_check[name] = _argv_matches(doc, actual, run_id)

    runs = sorted(p for p in (ev / "run-evidence").iterdir()
                  if p.is_dir()) if (ev / "run-evidence").is_dir() else []
    run = runs[0] if len(runs) == 1 else None
    state = _json(root, run / "state.json") if run else {}
    spec_text = _read(root, ev / "spec.as-cloned.md")
    criteria = spec_criteria(spec_text or "")
    fresh = driver.get("fresh")
    doctor_m = re.search(r"profile demo: (\w+)", text("doctor") or "")
    wt_text = text("worktree") or ""
    try:
        worktree = json.loads(wt_text[wt_text.index("{"):wt_text.rindex("}") + 1]).get("worktree")
    except (ValueError, AttributeError):
        worktree = None
    base = state.get("base_candidate") or {}

    accepted = state.get("last_accepted_candidate") or {}
    cand_id = accepted.get("candidate_id")
    binding = None
    if accepted.get("commit") and accepted.get("tree_digest") is not None:
        binding = (f"{accepted['commit']}:{accepted['tree_digest']}:"
                   f"{'clean' if accepted.get('tree_clean') else 'dirty'}")
    qa, qa_file = {}, None
    if run and cand_id:
        for f in sorted((run / "results").glob("qa-i*-a*.json"),
                        key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)]):
            q = _json(root, f)
            if q.get("candidate_id") == cand_id and q.get("role") == "qa":
                qa, qa_file = q, f.name      # the last attempt on the accepted candidate
    verdicts = {v.get("check_id"): v.get("outcome") for v in qa.get("verdicts") or []
                if isinstance(v, dict)}
    receipts: dict[str, dict] = {}
    if run and qa:
        prefix = f"{state.get('run_id')}-i{qa.get('iteration')}-a{qa.get('attempt')}-"
        for f in sorted((run / "receipts").glob(prefix + "*.json")):
            if f.name.endswith("-basis.json"):
                continue
            r = _json(root, f)
            iso = r.get("isolation") if isinstance(r.get("isolation"), dict) else {}
            sp = r.get("stdout_path")
            log = (_read(root, run / sp) if isinstance(sp, str) and not os.path.isabs(sp)
                   else None) or ""
            command = r.get("command") or ""
            m = PYTEST_SUMMARY.search(log)
            receipts[r.get("check_id") or f.stem] = {
                "exit_code": r.get("exit_code"), "runner_ok": r.get("runner_ok"),
                "candidate_binding": r.get("candidate_binding"),
                "command": command,
                "effective": iso.get("effective"),
                "verified_from_inside": iso.get("verified_from_inside"),
                "fallback_to_none": iso.get("fallback_to_none"),
                "observed_python3": iso.get("observed_python3"),
                "command_problems": command_problems(command),
                "pytest_summary": m.group(0).strip() if m else None,
            }
    history = state.get("history")
    integrity = _json(root, run / "approval" / "integrity.json") if run else {}
    p = _json(root, run / "approval" / "policy.json") if run else {}
    return {
        "what": "P1-16: fresh clone -> documented install -> doctor -> small verified "
                "programme, derived from raw captures",
        "record_version": 6,
        "run_id": state.get("run_id"),
        "driver_run_id": run_id,
        "symlinks": _symlinks(root),
        "duplicate_steps": duplicates,
        "clone_url": (((steps.get("clone") or {}).get("argv") or [])[2:3] or [None])[0],
        "clone_head": (text("head") or "").strip() or None,
        "published_head": ((text("published_head") or "").split() or [None])[0],
        "status_after_clone": text("status_after_clone"),
        "status_after_run": text("status_after_run"),
        "documented_commands": [" ".join(d) for d in documented],
        "documented_twice": documented_twice,
        "readme_blob_published": (text("readme_blob") or "").strip() or None,
        "readme_blob_copy": git_blob(readme or None),
        "spec_blob_published": (text("spec_blob") or "").strip() or None,
        "spec_blob_copy": git_blob(spec_text),
        "hoh_runs_set": {n: "HOH_RUNS" in ((steps.get(n) or {}).get("env_set") or {})
                         for n in ("worktree", "start", "run", "report")},
        "worktree": worktree,
        "repo_path_of_run": state.get("repo_path"),
        "target_head": (text("target_head") or "").strip() or None,
        "base_commit": base.get("commit"),
        "accepted_tree": accepted.get("tree_digest"),
        "recheck_dir": driver.get("recheck"),
        "recheck_tree": (text("recheck_tree") or "").strip() or None,
        "recheck_checkout_cwd": (steps.get("recheck_checkout") or {}).get("cwd"),
        "recheck": {cid: _recheck(steps.get(f"recheck_{cid}"), text(f"recheck_{cid}"))
                    for cid in criteria},
        "step_env_keys": {n: (sorted((st_.get("env") or {}).keys()) if "env" in st_ else None)
                          for n, st_ in steps.items()},
        "steps_argv_cwd": {n: {"argv": (steps.get(n) or {}).get("argv"),
                               "cwd": (steps.get(n) or {}).get("cwd")}
                           for n in ("accepted_head", "recheck_clone", "recheck_checkout",
                                     "recheck_tree", "driver_blob", "tool_blob",
                                     "driver_status")},
        "accepted_head": (text("accepted_head") or "").strip() or None,
        "driver_target": driver.get("target"),
        "driver_worktree": driver.get("worktree"),
        "driver_blob_committed": (text("driver_blob") or "").strip() or None,
        "driver_blob_running": driver.get("driver_blob_running"),
        "tool_blob_committed": (text("tool_blob") or "").strip() or None,
        "tool_blob_running": driver.get("tool_blob_running"),
        "driver_status": text("driver_status"),
        "anchor_steps": {n: {"argv": (steps.get(n) or {}).get("argv"),
                             "cwd": (steps.get(n) or {}).get("cwd")}
                         for n in ("readme_blob", "spec_blob", "target_head")},
        "argv_matches_readme": argv_check,
        "venv": driver.get("venv"),
        "interpreter_step": (text("interpreter") or "").strip() or None,
        "doctor_verdict": doctor_m.group(1) if doctor_m else None,
        "step_exits": {n: s.get("exit") for n, s in steps.items()},
        "spec_digest_as_cloned": (hashlib.sha256(spec_text.encode("utf-8")).hexdigest()[:16]
                                  if spec_text is not None else None),
        "spec_digest_of_run": state.get("spec_digest"),
        "spec_path_of_run": state.get("spec_path"),
        "fresh": fresh,
        "spec_criteria": criteria,
        "amendments_seen": state.get("amendments_seen"),
        "stage": state.get("stage"),
        "accepted_candidate": cand_id,
        "accepted_binding": binding,
        "qa_file": qa_file,
        "verdicts": verdicts,
        "receipts": receipts,
        "history_present": isinstance(history, list) and bool(history),
        "interventions": [h for h in history or [] if isinstance(h, str)
                          and INTERVENTION.search(h)],
        "dispatches": (state.get("usage") or {}).get("dispatches"),
        "integrity_held": integrity.get("held"),
        "integrity_violations": integrity.get("violations"),
        "approval_policy": {"mode": p.get("mode"), "digest": p.get("digest"),
                            "denied": len(p.get("deny") or [])},
    }


def satisfied(record: dict) -> list[str]:
    """What P1-16 requires of the record. Empty = proven."""
    problems = []
    if record.get("symlinks"):
        problems.append(f"the evidence holds symlinks: {record['symlinks'][:5]}")
    if record.get("duplicate_steps"):
        problems.append(f"steps recorded twice: {record['duplicate_steps']}")
    exits = record.get("step_exits") or {}
    for needed in ("clone", "head", "published_head", "status_after_clone", "readme_blob",
                   "spec_blob", "target_head", "accepted_head", "recheck_clone",
                   "driver_blob", "tool_blob", "driver_status",
                   "recheck_checkout", "recheck_tree", "install",
                   "interpreter", "doctor", "worktree", "start", "run", "report",
                   "status_after_run"):
        if exits.get(needed) != 0:
            problems.append(f"step {needed} exited {exits.get(needed)}")
    if record.get("clone_url") != PUBLISHED:
        problems.append(f"not a clone of {PUBLISHED}: {record.get('clone_url')}")
    head, pub = record.get("clone_head"), record.get("published_head")
    if not head or not re.fullmatch(r"[0-9a-f]{40}", head) or head != pub:
        problems.append(f"clone head {head} is not the published head {pub}")
    for when in ("status_after_clone", "status_after_run"):
        if record.get(when) is None or record.get(when).strip():
            problems.append(f"the clone was not clean ({when}): {record.get(when)!r}")
    matches = record.get("argv_matches_readme") or {}
    for name in ("install", "doctor", "worktree", "start", "run", "report"):
        if matches.get(name) is not True:
            problems.append(f"step {name} is not the command the README documents "
                            f"({'not documented' if name not in matches else 'differs'})")
    if record.get("documented_twice"):
        problems.append(f"the README documents a step twice: {record['documented_twice']}")
    # R2: every step ran from the allowlisted environment, and says so.
    for n, keys in (record.get("step_env_keys") or {}).items():
        extra = [k for k in (keys or []) if k not in ENV_BASE_KEYS | ENV_STEP_KEYS
                 and not k.startswith("HERDR_")] if keys is not None else ["<not recorded>"]
        if extra:
            problems.append(f"step {n} ran with environment outside the allowlist: {extra}")
    # R3: the recheck steps are the commands they claim, in the places they claim.
    sac = record.get("steps_argv_cwd") or {}
    rd, ah = record.get("recheck_dir"), record.get("accepted_head")
    pinned = {
        "accepted_head": (["git", "rev-parse", "HEAD"], record.get("driver_worktree")),
        "recheck_clone": (["git", "clone", "-q", "--no-local", record.get("driver_target"), rd],
                          str(Path(rd).parent) if rd else None),
        "recheck_checkout": (["git", "checkout", "-q", ah], rd),
        "recheck_tree": (["git", "rev-parse", "HEAD^{tree}"], rd),
    }
    for n, (argv, cwd) in pinned.items():
        got = sac.get(n) or {}
        if None in argv or not cwd or got.get("argv") != argv or got.get("cwd") != cwd:
            problems.append(f"step {n} is not the pinned command in the pinned place: {got}")
    if not record.get("driver_worktree") or record.get("driver_worktree") != record.get("worktree"):
        problems.append("the drive's worktree is not the one the documented step created")
    # R4: the evidence names the driver and record tool that produced it, as committed.
    for which in ("driver", "tool"):
        c, r = record.get(f"{which}_blob_committed"), record.get(f"{which}_blob_running")
        if not c or c != r:
            problems.append(f"the {which} that ran is not the committed one ({r} != {c})")
    if record.get("driver_status") is None or record.get("driver_status").strip():
        problems.append(f"the driver or record tool had uncommitted changes: "
                        f"{record.get('driver_status')!r}")
    at = record.get("accepted_tree")
    if not at or record.get("recheck_tree") != at \
            or record.get("recheck_checkout_cwd") != record.get("recheck_dir"):
        problems.append(f"the recheck clone is not the accepted candidate's tree "
                        f"({record.get('recheck_tree')} != {at})")
    anchors = record.get("anchor_steps") or {}
    fresh_ = record.get("fresh")
    want_anchor = {
        "readme_blob": ["git", "rev-parse", "HEAD:examples/minimal/README.md"],
        "spec_blob": ["git", "rev-parse", "HEAD:examples/minimal/spec.md"],
        "target_head": ["git", "rev-parse", "HEAD"]}
    for n, argv in want_anchor.items():
        a = anchors.get(n) or {}
        if a.get("argv") != argv or (n != "target_head" and a.get("cwd") != fresh_):
            problems.append(f"step {n} is not the anchoring command in the clone: {a}")
    for which in ("readme", "spec"):
        pub, copy = record.get(f"{which}_blob_published"), record.get(f"{which}_blob_copy")
        if not pub or pub != copy:
            problems.append(f"the {which} copy is not the published one ({copy} != {pub})")
    for name, ok in (record.get("hoh_runs_set") or {}).items():
        if ok is not True:
            problems.append(f"step {name} ran without the documented HOH_RUNS")
    if not record.get("hoh_runs_set"):
        problems.append("no step records HOH_RUNS")
    wt = record.get("worktree")
    if not wt or record.get("repo_path_of_run") != wt:
        problems.append(f"the run's repo {record.get('repo_path_of_run')} is not the worktree "
                        f"the documented step created ({wt})")
    th = record.get("target_head")
    if not th or record.get("base_commit") != th:
        problems.append(f"the run did not start from the target the drive created "
                        f"({record.get('base_commit')} != {th})")
    venv = record.get("venv")
    if not venv or record.get("interpreter_step") != venv:
        problems.append(f"the steps did not run under the fresh venv {venv}: "
                        f"{record.get('interpreter_step')}")
    if record.get("doctor_verdict") != "READY":
        problems.append(f"doctor said {record.get('doctor_verdict')}")
    if not record.get("run_id") or record.get("run_id") != record.get("driver_run_id"):
        problems.append(f"the preserved run {record.get('run_id')} is not this drive's "
                        f"{record.get('driver_run_id')}")
    fresh, spec_path = record.get("fresh"), record.get("spec_path_of_run")
    if not fresh or not spec_path or not Path(os.path.normpath(spec_path)).is_relative_to(
            os.path.normpath(fresh)):
        problems.append(f"the run's spec {spec_path} is not in the fresh clone {fresh}")
    if not record.get("spec_digest_as_cloned") \
            or record.get("spec_digest_of_run") != record.get("spec_digest_as_cloned"):
        problems.append("the run's spec digest is not the digest of the spec as cloned")
    if record.get("amendments_seen") not in (0, []):
        problems.append(f"the spec was amended during the run, or the run does not say: "
                        f"{record.get('amendments_seen')}")
    if not record.get("history_present"):
        problems.append("the run has no history")
    if record.get("interventions"):
        problems.append(f"the run was intervened in: {record.get('interventions')}")
    if not record.get("accepted_candidate") or record.get("stage") != "CHECKPOINTED":
        problems.append(f"not accepted (stage {record.get('stage')})")
    if not record.get("qa_file"):
        problems.append("no QA result for the accepted candidate")
    verdicts = record.get("verdicts") or {}
    if not verdicts or any(v != "PASS" for v in verdicts.values()):
        problems.append(f"not every criterion passed: {verdicts}")
    receipts = record.get("receipts") or {}
    criteria = record.get("spec_criteria") or {}
    if not criteria:
        problems.append("no criteria read from the spec as cloned")
    for cid, command in criteria.items():
        if verdicts.get(cid) != "PASS":
            problems.append(f"the spec's {cid} has no PASS verdict")
        rc = (record.get("recheck") or {}).get(cid) or {}
        rd, ah = record.get("recheck_dir"), record.get("accepted_head")
        want = ([f"{venv}/bin/python3", "-I", "-c", RECHECK_SCRIPT, cid, command, rd, ah]
                if venv and rd and ah else None)
        if want is None or rc.get("argv") != want or rc.get("cwd") != rd or rc.get("exit") != 0:
            problems.append(f"the spec's {cid} was not re-run verbatim through the fresh "
                            f"install's strict runner in the recheck clone (exit {rc.get('exit')})")
        if not venv or not str(rc.get("path") or "").startswith(f"{venv}/bin:"):
            problems.append(f"the recheck of {cid} did not run under the fresh venv")
        if rc.get("receipt_exit") != 0 or rc.get("runner_ok") is not True:
            problems.append(f"the spec's {cid}, re-run on the accepted commit, did not pass "
                            f"(exit {rc.get('receipt_exit')})")
        if rc.get("effective") != "strict" or rc.get("verified_from_inside") is not True \
                or rc.get("fallback_to_none") is not False:
            problems.append(f"the recheck of {cid} did not run under verified strict isolation")
        if not venv or rc.get("observed_python3") != f"{venv}/bin/python3":
            problems.append(f"the recheck of {cid} ran under {rc.get('observed_python3')}")
        if "pytest" in command and (not rc.get("pytest_summary")
                                    or PYTEST_BAD.search(rc.get("pytest_summary") or "")):
            problems.append(f"the recheck of {cid} shows no clean pytest summary: "
                            f"{rc.get('pytest_summary')}")
    if set(receipts) != set(verdicts) or not receipts:
        problems.append(f"receipts {sorted(receipts)} do not match verdicts {sorted(verdicts)}")
    want_py = f"{venv}/bin/python3" if venv else None
    for cid, r in receipts.items():
        if r.get("exit_code") != 0 or r.get("runner_ok") is not True:
            problems.append(f"{cid} did not exit cleanly")
        if r.get("candidate_binding") != record.get("accepted_binding"):
            problems.append(f"{cid} is bound to {r.get('candidate_binding')}, not the "
                            f"accepted candidate {record.get('accepted_binding')}")
        if r.get("effective") != "strict" or r.get("verified_from_inside") is not True \
                or r.get("fallback_to_none") is not False:
            problems.append(f"{cid} did not execute under verified strict isolation")
        if r.get("command_problems") != []:
            problems.append(f"{cid}'s command cannot be counted: {r.get('command_problems')}")
        if not want_py or r.get("observed_python3") != want_py:
            problems.append(f"{cid} ran under {r.get('observed_python3')}, not the fresh "
                            f"install {want_py}")
    for cid, command in criteria.items():
        if "pytest" in command:
            r = receipts.get(cid) or {}
            summary = r.get("pytest_summary")
            if "python3 -m pytest" not in (r.get("command") or "") or not summary:
                problems.append(f"{cid} is the spec's pytest criterion, and its receipt "
                                f"shows no pytest run with a summary line")
            elif PYTEST_BAD.search(summary):
                problems.append(f"{cid}'s pytest summary reports problems: {summary}")
    if record.get("integrity_held") is not True:
        problems.append(f"integrity not held: {record.get('integrity_violations')}")
    if (record.get("approval_policy") or {}).get("mode") not in STRICT_MODES:
        problems.append("no approval policy on record")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    record = derive(args.evidence)
    record["unsatisfied"] = satisfied(record)
    record["requirement_satisfied"] = not record["unsatisfied"]
    if args.check:
        try:
            on_disk = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"no readable record at {args.out}: {exc}", file=sys.stderr)
            return 1
        if on_disk != record:
            print("the record on disk does not re-derive from its evidence", file=sys.stderr)
            return 1
        print("re-derives identically")
        return 0
    args.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: satisfied={record['requirement_satisfied']}"
          + ("" if record["requirement_satisfied"] else f" {record['unsatisfied']}"))
    return 0 if record["requirement_satisfied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
