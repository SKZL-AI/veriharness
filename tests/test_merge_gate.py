"""`hoh merge-gate`: the merge gate as a product command (Amendment A6, requirement row P5-36).

Until now the gate was an orchestrator script. These tests run the product's command through
`hoh.cli.main` on real git repositories made under `tmp_path`, with review reports written as files,
and check the decision, the exit code and the receipt. They pin what the script let through:

* a report that is partial (`COMPLETENESS: PARTIAL`), or was modified a moment ago, or changed while
  it was read, is **no verdict** -- never `RELEASE_YES` and never `RELEASE_NO`;
* a footer is the trailing block of the report, six lines, a closed grammar;
* a receipt is created and never replaced, and a gate that could not decide exits 2, never 1;
* `verify` refuses a report that changed after the gate ran.

Every test builds its own repository and reports; nothing starts Herdr or an agent, nothing reads a
real run root, and the only paths written are under `tmp_path`.
"""
from __future__ import annotations

import errno
import hashlib
import importlib
import itertools
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoh import cli

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "src" / "hoh" / "agent_docs"
HOUR = 3600
SIX = ("VERDICT", "CANDIDATE", "BASE", "SPEC_SHA256", "BLOCKING_OPEN", "COMPLETENESS")
_counter = itertools.count(1)


def _mg():
    """The module under test. Imported inside each test, so that a tree without it fails every test
    (an assertion failure of its own) instead of the whole file at collection."""
    return importlib.import_module("hoh.mergegate")


# --- a repository, reports, and the command ---------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


class World(SimpleNamespace):
    """repo, tmp, base, cand, spec, spec_sha"""


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A base commit on `main` (HEAD), a candidate commit on branch `cand` that changes the
    specification file, so that the candidate's digest differs from the base's and from nothing else."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "docs" / "spec.md").write_text("# spec\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "cand")
    (repo / "docs" / "spec.md").write_text("# spec\nrevision 2\n", encoding="utf-8")
    (repo / "a.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    cand = _git(repo, "rev-parse", "HEAD")
    spec_sha = hashlib.sha256(b"# spec\nrevision 2\n").hexdigest()
    _git(repo, "checkout", "-q", "main")
    return World(repo=repo, tmp=tmp_path, base=base, cand=cand, spec="docs/spec.md",
                 spec_sha=spec_sha)


def _footer_text(w: World, **over: str | None) -> str:
    footer = {"VERDICT": "RELEASE_YES", "CANDIDATE": w.cand, "BASE": w.base,
              "SPEC_SHA256": w.spec_sha, "BLOCKING_OPEN": "0", "COMPLETENESS": "COMPLETE"}
    footer.update(over)
    return "".join(f"{k}: {v}\n" for k, v in footer.items() if v is not None)


def _age(path: Path, seconds: float) -> None:
    """Make the file `seconds` old (whole seconds, so that boundaries are exact)."""
    stamp = int(time.time()) - int(seconds)
    os.utime(path, ns=(stamp * 10**9, stamp * 10**9))


def _write(w: World, name: str, text: str, *, age: float = HOUR) -> str:
    path = w.tmp / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    _age(path, age)
    return str(path)


def _report(w: World, name: str, *, age: float = HOUR, body: str = "Findings: none.",
            **over: str | None) -> str:
    """A report that ends with the footer; `over` replaces a key's value, `None` drops the key."""
    text = f"# report {name}\n\n{body}\n\nrelease yes\n\n" + _footer_text(w, **over)
    return _write(w, name, text, age=age)


def _gate(w: World, capsys, reviews, *extra: str, receipt: str | None = None,
          candidate: str | None = None, base: str | None = None, spec: str | None = None,
          repo: str | None = None):
    receipt = receipt or str(w.tmp / f"receipt-{next(_counter)}.json")
    argv = ["--root", str(w.tmp / "runs"), "merge-gate", "check",
            "--repo", repo or str(w.repo), f"--candidate={candidate or w.cand}",
            f"--base={base or w.base}", f"--spec={spec or w.spec}",
            *[arg for r in reviews for arg in ("--review", r)], "--receipt", receipt, *extra]
    capsys.readouterr()
    rc = cli.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err, receipt


def _verify(w: World, capsys, receipt: str, *extra: str):
    capsys.readouterr()
    rc = cli.main(["--root", str(w.tmp / "runs"), "merge-gate", "verify", receipt, *extra])
    out, err = capsys.readouterr()
    return rc, out, err


def _pair(w: World, suffix: str = "", **over_b: str | None) -> list[str]:
    """Reviews a and b, whose reports are the files a<suffix>.md and b<suffix>.md; `over_b` is b's."""
    return [f"a={_report(w, 'a' + suffix)}", f"b={_report(w, 'b' + suffix, **over_b)}"]


def _load(receipt: str) -> dict:
    return json.loads(Path(receipt).read_text(encoding="utf-8"))


def _row(body: dict, review: str) -> dict:
    return next(r for r in body["reviews"] if r["review"] == review)


def _refused(result) -> str:
    rc, out, err = result[0], result[1], result[2]
    assert rc == 2, result
    assert out == "", "a refusal prints nothing on stdout"
    assert err.startswith("merge-gate refused: "), err
    return err


# --- G1: the decision, the receipt, the lines for the merge message --------------------------


def test_two_bound_complete_stable_yes_reports_are_merge_eligible(world, capsys):
    w = world
    reviews = _pair(w)
    rc, out, err, receipt = _gate(w, capsys, reviews)
    assert (rc, err) == (0, "")
    raw = Path(receipt).read_bytes()
    body = json.loads(raw)
    assert out.splitlines() == [
        f"Merge gate: MERGE_ELIGIBLE, receipt {receipt} sha256 {hashlib.sha256(raw).hexdigest()}",
        "Reviews: a RELEASE_YES (0 blocking), b RELEASE_YES (0 blocking)"]
    # The receipt is what the module says it is: a fixed shape, the values recomputed from git.
    assert raw == (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert set(body) == {"schema", "at", "repo", "candidate_arg", "base_arg", "candidate", "base",
                         "spec", "spec_sha256", "min_reviews", "min_age_seconds",
                         "required_reviews", "reviews", "aggregate", "problems"}
    assert body["schema"] == "hoh-merge-gate/1" == _mg().SCHEMA
    assert (body["candidate"], body["base"]) == (w.cand, w.base)
    assert (len(body["candidate"]), body["spec"], body["spec_sha256"]) == (40, w.spec, w.spec_sha)
    assert body["candidate_arg"] == w.cand and body["base_arg"] == w.base
    assert (body["min_reviews"], body["min_age_seconds"]) == (2, 60.0)
    assert body["repo"] == str(w.repo.resolve())
    assert body["required_reviews"] == ["a", "b"]
    assert (body["aggregate"], body["problems"]) == ("MERGE_ELIGIBLE", [])
    time.strptime(body["at"], "%Y-%m-%dT%H:%M:%SZ")
    for row, item in zip(body["reviews"], reviews, strict=True):
        path = Path(item.partition("=")[2])
        assert set(row) == {"review", "report", "report_sha256", "size", "mtime_ns", "footer",
                            "status", "problems"}
        assert row["report"] == str(path.resolve())
        assert row["report_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert (row["size"], row["mtime_ns"]) == (path.stat().st_size, path.stat().st_mtime_ns)
        assert row["footer"] == {"VERDICT": "RELEASE_YES", "CANDIDATE": w.cand, "BASE": w.base,
                                 "SPEC_SHA256": w.spec_sha, "BLOCKING_OPEN": "0",
                                 "COMPLETENESS": "COMPLETE"}
        assert (row["status"], row["problems"]) == ("RELEASE_YES", [])


# --- G2: one report that is not a clean yes ---------------------------------------------------

_NOT_CLEAN = {
    "release-no": ({"VERDICT": "RELEASE_NO", "BLOCKING_OPEN": "1"}, "RELEASE_NO",
                   ["VERDICT_NO", "BLOCKING_OPEN"]),
    "yes-with-open-blockers": ({"BLOCKING_OPEN": "2"}, "RELEASE_NO", ["BLOCKING_OPEN"]),
    "no-without-blockers": ({"VERDICT": "RELEASE_NO"}, "RELEASE_NO", ["VERDICT_NO"]),
    "stale-candidate": ({"CANDIDATE": "1" * 40}, "NO_VERDICT", ["STALE_CANDIDATE"]),
    "stale-base": ({"BASE": "2" * 40}, "NO_VERDICT", ["STALE_BASE"]),
    "stale-spec": ({"SPEC_SHA256": "3" * 64}, "NO_VERDICT", ["STALE_SPEC"]),
    "a-prefix-is-not-the-full-sha": ({"CANDIDATE": "@"}, "NO_VERDICT", ["STALE_CANDIDATE"]),
}


@pytest.mark.parametrize("case", sorted(_NOT_CLEAN))
def test_a_report_that_is_not_a_clean_yes_makes_the_merge_not_eligible(world, capsys, case):
    w = world
    over, status, codes = _NOT_CLEAN[case]
    if over.get("CANDIDATE") == "@":
        over = {"CANDIDATE": w.cand[:12]}
    reviews = _pair(w, **over)
    rc, out, err, receipt = _gate(w, capsys, reviews)
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"
    assert (_row(body, "b")["status"], _row(body, "b")["problems"]) == (status, codes)
    assert (_row(body, "a")["status"], _row(body, "a")["problems"]) == ("RELEASE_YES", [])
    lines = out.splitlines()
    assert lines[0].startswith("Merge gate: NOT_MERGE_ELIGIBLE, receipt ")
    assert lines[1].startswith("Reviews: a RELEASE_YES (0 blocking), b " + status)
    for code in codes:
        assert any(ln.startswith(f"  b: {code}: ") for ln in lines[2:]), (code, out)
    assert [(p["review"], p["code"]) for p in body["problems"]] == [("b", c) for c in codes]


# --- G3: completeness (OP-07) -------------------------------------------------------------------


def test_a_partial_report_is_no_verdict_whatever_else_it_says(world, capsys):
    w = world
    cases = [
        ({"COMPLETENESS": "PARTIAL"}, ["INCOMPLETE"]),
        ({"COMPLETENESS": "PARTIAL", "VERDICT": "RELEASE_NO", "BLOCKING_OPEN": "1"},
         ["INCOMPLETE", "VERDICT_NO", "BLOCKING_OPEN"]),
        ({"COMPLETENESS": None}, ["FOOTER_INCOMPLETE"]),         # the five-line footer of before
        ({"COMPLETENESS": "complete"}, ["FOOTER_AMBIGUOUS"]),
        ({"COMPLETENESS": "COMPLETE-ish"}, ["FOOTER_AMBIGUOUS"]),
    ]
    for over, codes in cases:
        rc, out, err, receipt = _gate(w, capsys, _pair(w, **over))
        assert (rc, err) == (1, ""), over
        row = _row(_load(receipt), "b")
        assert (row["status"], row["problems"]) == ("NO_VERDICT", codes), over
        assert out.splitlines()[1] == "Reviews: a RELEASE_YES (0 blocking), b NO_VERDICT", (over, out)
    # control: the same reports with COMPLETE are the eligible pair
    assert _gate(w, capsys, _pair(w))[0] == 0


# --- G4: a report that is still being written is not stable (OP-04) -------------------------


def test_a_report_modified_a_moment_ago_is_not_stable(world, capsys):
    w = world
    fresh = [f"a={_report(w, 'a')}", f"b={_report(w, 'b', age=5)}"]
    rc, out, err, receipt = _gate(w, capsys, fresh)
    assert (rc, err) == (1, "")
    row = _row(_load(receipt), "b")
    assert (row["status"], row["problems"]) == ("NO_VERDICT", ["REPORT_UNSTABLE"])
    assert "  b: REPORT_UNSTABLE: " in out and "b NO_VERDICT" in out
    # a report dated in the future is not stable either
    future = [f"a={_report(w, 'a2')}", f"b={_report(w, 'b2', age=-HOUR)}"]
    assert _row(_load(_gate(w, capsys, future)[3]), "b")["problems"] == ["REPORT_UNSTABLE"]
    # the operator can lower the age; the receipt records that the rule was lowered
    rc, out, err, receipt = _gate(w, capsys, fresh, "--min-age", "0")
    assert (rc, err) == (0, "")
    assert _load(receipt)["min_age_seconds"] == 0.0
    assert out.splitlines()[2:] == ["Rules lowered by the operator: --min-age 0 (default 60)"]


def test_the_age_rule_is_exact_at_its_boundary(world):
    w = world
    mg = _mg()
    pair = [("a", _report(w, "a")), ("b", _report(w, "b"))]
    stamp = 1_700_000_000
    for _, path in pair:
        os.utime(path, ns=(stamp * 10**9, stamp * 10**9))

    def judged(now: float) -> list[str]:
        body = mg.evaluate(str(w.repo), w.cand, w.base, w.spec, pair, min_age=60.0, now=now)
        return sorted({c for r in body["reviews"] for c in r["problems"]})

    assert judged(stamp + 60) == []                       # exactly the minimum age: stable
    assert judged(stamp + 59.9) == ["REPORT_UNSTABLE"]    # a moment younger: not
    assert judged(stamp + 61) == []


def test_a_report_that_changes_while_it_is_read_is_unstable(world, capsys, monkeypatch):
    w = world
    mg = _mg()
    reviews = _pair(w)
    assert _gate(w, capsys, reviews)[0] == 0               # control, unpatched
    calls: list[int] = []

    def writer_active(fd: int):
        real = os.fstat(fd)
        calls.append(fd)
        if len(calls) % 2 == 0:                             # the stat after the read: it moved on
            return SimpleNamespace(st_size=real.st_size, st_mtime_ns=real.st_mtime_ns + 1,
                                   st_dev=real.st_dev, st_ino=real.st_ino)
        return real

    monkeypatch.setattr(mg, "_fstat", writer_active)
    rc, _out, err, receipt = _gate(w, capsys, reviews)
    assert (rc, err) == (1, "")
    body = _load(receipt)
    for review in ("a", "b"):
        assert (_row(body, review)["status"], _row(body, review)["problems"]) == (
            "NO_VERDICT", ["REPORT_UNSTABLE"])
    assert len(calls) == 4, "each report is stat'ed once before and once after its read"


# --- G5: the footer is the trailing block, a closed grammar ---------------------------------

_RECORDED = ("VERDICT: RELEASE_YES\n"
             "CANDIDATE: 8b819e741fb045a3a2b8f632b5085626b026e786\n"
             "BASE: b0fd76141cad7353538564f80cc4571afd5e9065\n"
             "SPEC_SHA256: 1087f95d7099f600114c583012207f6ea0017209a8629333cfde1beb75886785\n"
             "BLOCKING_OPEN: 0\n")


def test_the_footer_is_the_trailing_block_and_nothing_else_reads_as_one():
    mg = _mg()
    parse = mg.parse_footer
    assert mg.FOOTER_KEYS == SIX
    body = "# report\n\nrelease yes\n\n"
    ok, bad = parse(body + _RECORDED + "COMPLETENESS: COMPLETE\n")
    assert bad is None and ok == {
        "VERDICT": "RELEASE_YES", "CANDIDATE": "8b819e741fb045a3a2b8f632b5085626b026e786",
        "BASE": "b0fd76141cad7353538564f80cc4571afd5e9065",
        "SPEC_SHA256": "1087f95d7099f600114c583012207f6ea0017209a8629333cfde1beb75886785",
        "BLOCKING_OPEN": "0", "COMPLETENESS": "COMPLETE"}
    # the footer recorded in the build's own reports (five lines) is incomplete, and says what is missing
    assert parse(body + _RECORDED)[1][0] == "FOOTER_INCOMPLETE"
    assert "COMPLETENESS" in parse(body + _RECORDED)[1][1]
    # any order, blank lines between, CRLF line endings, trailing blanks
    shuffled = "COMPLETENESS: PARTIAL\n\n" + _RECORDED.replace("\n", "\r\n") + "\r\n\r\n   \n"
    assert parse(body + shuffled)[0]["COMPLETENESS"] == "PARTIAL"
    one = lambda text: parse(text)[1] and parse(text)[1][0]     # the code, or None
    full = _RECORDED + "COMPLETENESS: COMPLETE\n"
    # a sentence after the footer: the report kept growing, so it is not final
    assert one(body + full + "one more thing\n") == "FOOTER_INCOMPLETE"
    # the keys are in the report but not as the trailing block (the old tool's twelve-line window)
    assert one(body + full + "see above\nand above\n") == "FOOTER_INCOMPLETE"
    # keys quoted in the body do not count; only the block at the end does
    assert one(body + full + "\n```\nVERDICT: RELEASE_NO\n```\n") == "FOOTER_INCOMPLETE"
    assert one("") == "FOOTER_INCOMPLETE"
    # a footer line hidden behind a Unicode line separator is not a footer line: str.splitlines() would
    # split there and read this report as complete (the sixth line is "inside" the fifth)
    for sep in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"):
        hidden = _RECORDED.rstrip("\n") + sep + "COMPLETENESS: COMPLETE\n"
        assert one(body + hidden) == "FOOTER_INCOMPLETE", repr(sep)
    # a lone CR is a line break; blanks around a footer line are ignored
    assert parse(body + full.replace("\n", "\r"))[1] is None
    assert parse(body + "".join(f"  \t{ln}  \n" for ln in full.splitlines()))[1] is None
    # markdown decoration is not a footer line
    assert one(body + full.replace("VERDICT:", "**VERDICT:**")) == "FOOTER_INCOMPLETE"
    # repeated and unknown keys
    assert one(body + full + "BLOCKING_OPEN: 0\n") == "FOOTER_DUPLICATE"
    assert one(body + "REVIEWER: x\n" + full) == "FOOTER_UNKNOWN_KEY"
    assert one(body + full + "NOTE: fine\n") == "FOOTER_UNKNOWN_KEY"
    # the earlier of two faults decides which is reported
    assert one(body + "VERDICT: RELEASE_YES\n" + full + "NOTE: fine\n") == "FOOTER_DUPLICATE"
    assert one(body + "NOTE: fine\n" + full + "VERDICT: RELEASE_YES\n") == "FOOTER_UNKNOWN_KEY"
    # values outside the closed vocabulary; BLOCKING_OPEN is ASCII digits only
    for tail in ("VERDICT: release_yes", "VERDICT: RELEASE_MAYBE", "BLOCKING_OPEN: \u00b2",
                 "BLOCKING_OPEN: \u0660", "BLOCKING_OPEN: -1", "BLOCKING_OPEN: 1.0",
                 "BLOCKING_OPEN: 0x0", "BLOCKING_OPEN: 1e0", "COMPLETENESS: complete"):
        key = tail.partition(":")[0]
        lines = [ln for ln in full.splitlines() if not ln.startswith(key + ":")]
        assert one(body + "\n".join(lines) + "\n" + tail + "\n") == "FOOTER_AMBIGUOUS", tail
    # a large count is a number, not an error
    assert parse(body + full.replace("BLOCKING_OPEN: 0", "BLOCKING_OPEN: " + "9" * 30))[1] is None


# --- G6: collisions ---------------------------------------------------------------------------


def test_two_reviews_that_are_one_report_collide(world, capsys):
    w = world
    a = Path(_report(w, "a"))
    b = Path(_report(w, "b"))
    (w.tmp / "alias.md").symlink_to(a)
    os.link(a, w.tmp / "hard.md")
    copy = w.tmp / "copy.md"
    copy.write_bytes(a.read_bytes())
    _age(copy, HOUR)
    cases = {
        "same id, two files": [f"a={a}", f"a={b}"],
        "same file, two ids": [f"a={a}", f"b={a}"],
        "a symlink to the same file": [f"a={a}", f"b={w.tmp / 'alias.md'}"],
        "a hard link to the same file": [f"a={a}", f"b={w.tmp / 'hard.md'}"],
        "a copy with the same bytes": [f"a={a}", f"b={copy}"],
    }
    for label, reviews in cases.items():
        rc, _out, err, receipt = _gate(w, capsys, reviews)
        assert (rc, err) == (1, ""), label
        body = _load(receipt)
        assert [r["problems"] for r in body["reviews"]] == [["COLLISION"], ["COLLISION"]], label
        assert [r["status"] for r in body["reviews"]] == ["NO_VERDICT"] * 2, label
        assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"
        # the number of required reviews is the number of distinct ids: one id twice is one review
        assert any(p["code"] == "TOO_FEW_REVIEWS" for p in body["problems"]) == (label == "same id, two files")
    assert _gate(w, capsys, [f"a={a}", f"b={b}"])[0] == 0         # control: two different reports


# --- G7: how many reviews ---------------------------------------------------------------------


def test_at_least_two_reviews_unless_the_operator_says_otherwise(world, capsys):
    w = world
    a, b = _report(w, "a"), _report(w, "b")
    rc, out, err, receipt = _gate(w, capsys, [f"a={a}"])
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert body["aggregate"] == "NOT_MERGE_ELIGIBLE"
    assert [(p["review"], p["code"]) for p in body["problems"]] == [(None, "TOO_FEW_REVIEWS")]
    assert "  gate: TOO_FEW_REVIEWS: " in out
    assert _row(body, "a")["status"] == "RELEASE_YES"
    rc, out, err, receipt = _gate(w, capsys, [f"a={a}"], "--min-reviews", "1")
    assert (rc, err) == (0, "")
    assert _load(receipt)["min_reviews"] == 1
    assert out.splitlines()[2:] == ["Rules lowered by the operator: --min-reviews 1 (default 2)"]
    assert _gate(w, capsys, [f"a={a}", f"b={b}"], "--min-reviews", "3")[0] == 1
    _refused(_gate(w, capsys, [f"a={a}", f"b={b}"], "--min-reviews", "0"))


# --- G8: a report that cannot be read -----------------------------------------------------------


def test_a_missing_or_unreadable_report_is_no_verdict_not_a_crash(world, capsys):
    w = world
    good = _report(w, "a")
    (w.tmp / "adir").mkdir()
    cases = {"missing": (str(w.tmp / "nothing.md"), "REPORT_MISSING"),
             "a directory": (str(w.tmp / "adir"), "REPORT_UNREADABLE"),
             "a NUL in the name": (str(w.tmp / "x\x00y.md"), "REPORT_UNREADABLE"),
             "empty": (_write(w, "empty", ""), "FOOTER_INCOMPLETE")}
    if Path("/dev/null").is_char_device():
        cases["a device"] = ("/dev/null", "REPORT_UNREADABLE")
    for label, (path, code) in cases.items():
        rc, _out, err, receipt = _gate(w, capsys, [f"a={good}", f"b={path}"])
        assert (rc, err) == (1, ""), label
        row = _row(_load(receipt), "b")
        assert (row["status"], row["problems"]) == ("NO_VERDICT", [code]), label
        assert row["report_sha256"] is None or code == "FOOTER_INCOMPLETE", label
        assert _row(_load(receipt), "a")["status"] == "RELEASE_YES", label
    # bytes that are not UTF-8 in the body do not make the report unreadable
    rough = w.tmp / "rough.md"
    rough.write_bytes(b"# report \xff\xfe\n\nrelease yes\n\n" + _footer_text(w).encode())
    _age(rough, HOUR)
    assert _gate(w, capsys, [f"a={good}", f"b={rough}"])[0] == 0


# --- G9: the receipt is created, never replaced; no decision is exit 2 -----------------------


def test_the_receipt_is_created_never_replaced_and_no_decision_is_not_a_no(world, capsys):
    w = world
    reviews = _pair(w)
    rc, _out, _err, receipt = _gate(w, capsys, reviews)
    assert rc == 0
    before = Path(receipt).read_bytes()
    # the 2026-10-08 slip: a second run onto the same name found the first receipt and the shell went on
    _refused(_gate(w, capsys, reviews, receipt=receipt))
    assert Path(receipt).read_bytes() == before
    # a decision of "no" whose receipt cannot be created is also exit 2, never exit 1
    bad_pair = _pair(w, "-no", VERDICT="RELEASE_NO")
    _refused(_gate(w, capsys, bad_pair, receipt=receipt))
    assert Path(receipt).read_bytes() == before
    other = w.tmp / "other.json"
    other.write_text("keep", encoding="utf-8")
    _refused(_gate(w, capsys, reviews, receipt=str(other)))
    assert other.read_text(encoding="utf-8") == "keep"
    dangling = w.tmp / "dangling.json"
    dangling.symlink_to(w.tmp / "target-that-must-not-appear.json")
    _refused(_gate(w, capsys, reviews, receipt=str(dangling)))
    assert not (w.tmp / "target-that-must-not-appear.json").exists()
    _refused(_gate(w, capsys, reviews, receipt=str(w.tmp / "no-such-dir" / "r.json")))
    assert not (w.tmp / "no-such-dir").exists()
    _refused(_gate(w, capsys, reviews, receipt=str(w.tmp / "x\x00y.json")))


# --- G10: inputs the gate cannot use ------------------------------------------------------------

_REFUSED_INPUTS = {
    "unknown candidate": {"candidate": "no-such-ref"},
    "unknown base": {"base": "no-such-ref"},
    "an option as candidate": {"candidate": "--all"},
    "an option as base": {"base": "-x"},
    "a spec that is not in the candidate": {"spec": "docs/nothing.md"},
    "a spec above the repository": {"spec": "../spec.md"},
    "a spec with a leading ./": {"spec": "./docs/spec.md"},
    "a spec with a .. component": {"spec": "docs/../docs/spec.md"},
    "a spec with a trailing slash": {"spec": "docs/"},
    "an absolute spec": {"spec": "/etc/hostname"},
    "a spec that is a directory": {"spec": "docs"},
    "not a repository": {"repo": "@tmp"},
}


@pytest.mark.parametrize("case", sorted(_REFUSED_INPUTS))
def test_inputs_the_gate_cannot_use_are_refused_and_write_nothing(world, capsys, case):
    w = world
    over = dict(_REFUSED_INPUTS[case])
    if over.get("repo") == "@tmp":
        over["repo"] = str(w.tmp / "adir")
        (w.tmp / "adir").mkdir()
    result = _gate(w, capsys, _pair(w), **over)
    _refused(result)
    assert not Path(result[3]).exists(), "a refusal leaves no receipt"


def test_review_and_age_options_the_gate_cannot_use_are_refused(world, capsys):
    w = world
    a = _report(w, "a")
    for item in ("noequals", f"={a}", f"a b={a}", "a=", ".a=" + a):
        result = _gate(w, capsys, [f"b={_report(w, 'b')}", item])
        _refused(result)
        assert not Path(result[3]).exists(), item
    for value in ("nan", "-1", "inf", "-inf"):
        result = _gate(w, capsys, _pair(w), f"--min-age={value}")
        _refused(result)
        assert not Path(result[3]).exists(), value
    # an id has at most 64 characters
    b = _report(w, "b")
    assert _gate(w, capsys, [f"{'x' * 64}={a}", f"b={b}"])[0] == 0
    result = _gate(w, capsys, [f"{'x' * 65}={a}", f"b={b}"])
    _refused(result)
    assert not Path(result[3]).exists()


def test_a_failure_of_the_gate_itself_is_exit_2_never_exit_1(world, capsys, monkeypatch):
    w = world
    mg = _mg()

    def boom(*args, **kwargs):
        raise RuntimeError("deliberate")

    monkeypatch.setattr(mg, "evaluate", boom)
    result = _gate(w, capsys, _pair(w))
    assert "RuntimeError: deliberate" in _refused(result)
    assert not Path(result[3]).exists()


# --- G11: recomputed now, from git --------------------------------------------------------------


def test_candidate_base_and_spec_are_recomputed_from_git_now(world, capsys, monkeypatch):
    w = world
    # a ref is resolved now and the receipt keeps the full sha and the argument
    rc, _out, err, receipt = _gate(w, capsys, _pair(w), candidate="cand", base="HEAD")
    assert (rc, err) == (0, "")
    body = _load(receipt)
    assert (body["candidate"], body["candidate_arg"], body["base"], body["base_arg"]) == (
        w.cand, "cand", w.base, "HEAD")
    # the digest is of the specification in the candidate commit, not of the work tree
    assert (w.repo / w.spec).read_bytes() != b"# spec\nrevision 2\n"
    assert body["spec_sha256"] == w.spec_sha
    (w.repo / w.spec).write_text("# spec, edited and not committed\n", encoding="utf-8")
    assert _gate(w, capsys, _pair(w))[0] == 0
    # the environment cannot point git somewhere else
    elsewhere = w.tmp / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    assert _gate(w, capsys, _pair(w))[0] == 0


def test_a_base_that_moved_since_the_review_is_stale(world, capsys):
    w = world
    reviews = _pair(w)
    _git(w.repo, "commit", "--allow-empty", "-qm", "another merge")      # HEAD moves
    rc, _out, err, receipt = _gate(w, capsys, reviews, base="HEAD")
    assert (rc, err) == (1, "")
    body = _load(receipt)
    assert [r["problems"] for r in body["reviews"]] == [["STALE_BASE"], ["STALE_BASE"]]
    assert body["base"] != w.base
    # a pasted sha is the base of the review by construction: the argument must be a ref to see this
    assert _gate(w, capsys, reviews, base=w.base)[0] == 0


# --- G12: it writes nothing but the receipt ---------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    out = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode))
    return out


def test_the_command_changes_nothing_but_the_receipt(world, capsys):
    w = world
    reviews = _pair(w)
    mixed = _pair(w, "-mixed", VERDICT="RELEASE_NO")
    for label, args, expect in (("eligible", reviews, 0), ("not eligible", mixed, 1)):
        before = _snapshot(w.tmp)
        refs = _git(w.repo, "for-each-ref")
        rc, _out, _err, receipt = _gate(w, capsys, args)
        assert rc == expect, label
        after = _snapshot(w.tmp)
        assert set(after) - set(before) == {os.path.relpath(receipt, w.tmp)}, label
        assert all(after[k] == v for k, v in before.items()), label    # no file touched
        assert _git(w.repo, "for-each-ref") == refs
        assert _git(w.repo, "status", "--porcelain") == ""
    result = _gate(w, capsys, reviews, "--min-age", "nan")           # a refusal writes nothing at all
    _refused(result)


# --- G13: verify (OP-04: a report that changes after the gate ran) -------------------------------


def test_verify_accepts_an_untouched_receipt_and_names_a_changed_report(world, capsys):
    w = world
    a, b = _report(w, "a"), _report(w, "b")
    rc, out, err, receipt = _gate(w, capsys, [f"a={a}", f"b={b}"])
    assert rc == 0
    before = _snapshot(w.tmp)
    digest = hashlib.sha256(Path(receipt).read_bytes()).hexdigest()
    rc, out, err = _verify(w, capsys, receipt)
    assert (rc, err) == (0, "")
    assert out == f"Merge gate verify: VERIFIED, receipt {receipt} sha256 {digest}\n"
    assert _snapshot(w.tmp) == before, "verify writes nothing"
    # the body of a report is edited after the gate ran; the footer stays valid and the old mtime is
    # put back, so that only the digest tells
    path = Path(b)
    stat_before = path.stat()
    path.write_text(path.read_text(encoding="utf-8").replace("Findings: none.", "Findings: none (edited)."),
                    encoding="utf-8")
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    rc, out, err = _verify(w, capsys, receipt)
    assert (rc, err) == (1, "")
    lines = out.splitlines()
    assert lines[0].startswith("Merge gate verify: NOT_VERIFIED, receipt ")
    assert lines[1:] == [lines[1]] and lines[1].startswith("  b: REPORT_CHANGED: ")
    # a report written to again right now is changed and unstable
    _age(path, 5)
    out = _verify(w, capsys, receipt)[1]
    assert "  b: REPORT_CHANGED: " in out and "  b: REPORT_UNSTABLE: " in out
    # a report that was removed after the gate ran
    os.replace(path, w.tmp / "b.moved")
    out = _verify(w, capsys, receipt)[1]
    assert "  b: REPORT_MISSING: " in out and "  b: REPORT_CHANGED: " in out


def test_verify_refuses_a_moved_base_and_a_receipt_that_said_no(world, capsys):
    w = world
    reviews = _pair(w)
    rc, out, err, receipt = _gate(w, capsys, reviews, base="HEAD")
    assert rc == 0 and _verify(w, capsys, receipt)[0] == 0
    _git(w.repo, "commit", "--allow-empty", "-qm", "another merge")
    rc, out, err = _verify(w, capsys, receipt)
    assert (rc, err) == (1, "")
    assert "  a: STALE_BASE: " in out and "  b: STALE_BASE: " in out
    # --repo overrides the repository the receipt names
    assert _verify(w, capsys, receipt, "--repo", str(w.repo))[0] == 1
    # a receipt that said NOT_MERGE_ELIGIBLE is never verified, even when the reports are fine now
    no_receipt = _gate(w, capsys, _pair(w, "-no", VERDICT="RELEASE_NO"), base=w.base)[3]
    assert _load(no_receipt)["aggregate"] == "NOT_MERGE_ELIGIBLE"
    rc, out, err = _verify(w, capsys, no_receipt)
    assert rc == 1 and "  gate: RECEIPT_NOT_ELIGIBLE: " in out


def test_verify_refuses_what_is_not_a_receipt(world, capsys):
    w = world
    good = _gate(w, capsys, _pair(w))[3]
    body = _load(good)
    broken = {
        "not json": "{",
        "a list": "[]",
        "another schema": json.dumps({**body, "schema": "hoh-merge-gate/0"}),
        "no reviews": json.dumps({**body, "reviews": []}),
        "no spec": json.dumps({k: v for k, v in body.items() if k != "spec"}),
        "a review without a report": json.dumps(
            {**body, "reviews": [{"review": "a"}]}),
        "an aggregate nobody knows": json.dumps({**body, "aggregate": "MAYBE"}),
    }
    for text in broken.values():
        path = w.tmp / f"broken-{next(_counter)}.json"
        path.write_text(text, encoding="utf-8")
        _refused(_verify(w, capsys, str(path)))
    _refused(_verify(w, capsys, str(w.tmp / "missing.json")))
    _refused(_verify(w, capsys, str(w.tmp)))                        # a directory


# --- G14: the words the reviewers are given, and the command surface ---------------------------


def test_the_reviewer_rules_and_the_method_name_the_footer_the_gate_reads():
    mg = _mg()
    rules = (DOCS / "templates" / "reviewer-rules.template.md").read_text(encoding="utf-8")
    method = (DOCS / "references" / "method.md").read_text(encoding="utf-8")
    # the template prints exactly the gate's keys, in the gate's order, one per line
    assert re.findall(r"^([A-Z][A-Z0-9_]*): ", rules, flags=re.MULTILINE) == list(mg.FOOTER_KEYS)
    assert "exactly these six lines" in rules and "five lines" not in rules
    assert "COMPLETENESS: COMPLETE or PARTIAL" in rules
    flat = " ".join(rules.split())
    assert "a PARTIAL report is no verdict" in flat and "modified in the last minute" in flat
    section = " ".join(method.split("## 4.")[1].split("## 5.")[0].split())
    for key in mg.FOOTER_KEYS:
        assert f"`{key}`" in section, key
    assert "and a sixth line, `COMPLETENESS`" in section
    assert "hoh merge-gate check" in section and "hoh merge-gate verify" in section
    assert "--candidate" in section and "--base" in section
    assert "partial or unstable report is no verdict" in section


def test_the_command_group_has_check_and_verify_and_nothing_merges():
    parser = cli.build_parser()
    check = parser.parse_args(["merge-gate", "check", "--candidate", "c", "--base", "b",
                               "--spec", "s", "--review", "a=x", "--review", "b=y",
                               "--receipt", "r"])
    assert (check.merge_gate_cmd, check.review, check.repo) == ("check", ["a=x", "b=y"], ".")
    assert (check.min_reviews, check.min_age, check.func) == (2, 60.0, cli.cmd_merge_gate)
    verify = parser.parse_args(["merge-gate", "verify", "r.json"])
    assert (verify.merge_gate_cmd, verify.receipt, verify.repo) == ("verify", "r.json", None)
    for argv in (["merge-gate"], ["merge-gate", "merge"], ["merge-gate", "check"]):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(argv)
        assert caught.value.code == 2, argv
    source = (ROOT / "src" / "hoh" / "mergegate.py").read_text(encoding="utf-8")
    assert not re.search(r"""["']merge["']|["']commit["']|["']push["']|["']checkout["']""", source), (
        "the module runs read-only git commands only")


# --- Revision 2: a count of any length, the receipt's last steps, verify's pair, the reading, the clock -----


def test_a_blocking_count_of_any_length_is_a_count_not_an_overflow(world, capsys):
    w = world
    # zero written with 5000 digits is zero blockers; it must not overflow int() and exit 2 for everyone
    rc, out, err, receipt = _gate(w, capsys, _pair(w, BLOCKING_OPEN="0" * 5000))
    assert (rc, err) == (0, "")
    assert out.splitlines()[1] == "Reviews: a RELEASE_YES (0 blocking), b RELEASE_YES (0 blocking)"
    # a count of 5000 digits is a count: a release no, and the printed lines stay short
    rc, out, err, receipt = _gate(w, capsys, _pair(w, "-big", BLOCKING_OPEN="1" + "0" * 4999))
    assert (rc, err) == (1, "")
    row = _row(_load(receipt), "b")
    assert (row["status"], row["problems"]) == ("RELEASE_NO", ["BLOCKING_OPEN"])
    assert max(len(line) for line in out.splitlines()[2:]) < 200
    assert "b RELEASE_NO (10000000000000000000... blocking)" in out
    # leading zeros are not part of the count
    rc, out, err, receipt = _gate(w, capsys, _pair(w, "-lead", BLOCKING_OPEN="0007"))
    assert rc == 1 and "b RELEASE_NO (7 blocking)" in out


def _publish_that(monkeypatch, behave):
    """Replace the module's `durable_create` by `behave(real, path, payload)`."""
    mg = _mg()
    real = mg.durable_create
    monkeypatch.setattr(mg, "durable_create", lambda path, payload, **kw: behave(real, path, payload))


def test_a_receipt_in_place_but_not_synced_still_carries_its_decision(world, capsys, monkeypatch):
    from hoh import durable_io
    w = world

    def created_then_unsynced(real, path, payload):
        real(path, payload)
        raise durable_io.DurableWriteNotSynced(path, OSError(errno.EIO, "Input/output error"))

    _publish_that(monkeypatch, created_then_unsynced)
    for label, reviews, code, head in (("eligible", _pair(w), 0, "MERGE_ELIGIBLE"),
                                       ("not eligible", _pair(w, "-no", VERDICT="RELEASE_NO"), 1,
                                        "NOT_MERGE_ELIGIBLE")):
        rc, out, err, receipt = _gate(w, capsys, reviews)
        assert rc == code, label                           # the decision, not "no decision"
        assert out.startswith(f"Merge gate: {head}, receipt {receipt} sha256 "
                              f"{hashlib.sha256(Path(receipt).read_bytes()).hexdigest()}\n"), label
        assert err.startswith("merge-gate warning: the receipt is in place and was read back"), label
        assert err.count("\n") == 1 and "Input/output error" in err, label
        # the file is whole, and verify -- which only reads it -- treats it like any other receipt
        assert _verify(w, capsys, receipt)[0] == code, label
    # a failure before the link is still no decision and leaves nothing

    def full_disk(real, path, payload):
        raise durable_io.DiskFullError(path, len(payload))

    _publish_that(monkeypatch, full_disk)
    result = _gate(w, capsys, _pair(w))
    _refused(result)
    assert not Path(result[3]).exists()


def test_a_receipt_that_cannot_be_read_back_as_written_is_refused(world, capsys, monkeypatch):
    w = world
    _publish_that(monkeypatch, lambda real, path, payload: Path(path).write_bytes(b"{}"))
    result = _gate(w, capsys, _pair(w))
    err = _refused(result)
    assert "what was read back is not what was written" in err and "do not use it" in err
    # the one case in which exit 2 leaves a file; the message says so, and verify refuses the file
    assert Path(result[3]).read_bytes() == b"{}"
    _refused(_verify(w, capsys, result[3]))


def test_verify_judges_with_the_rules_the_receipt_recorded(world, capsys):
    w = world
    young = _report(w, "a", age=5)                         # younger than the default minimum age
    rc, out, err, receipt = _gate(w, capsys, [f"a={young}"], "--min-age", "0", "--min-reviews", "1")
    assert (rc, err) == (0, "")
    rc, out, err = _verify(w, capsys, receipt)
    assert (rc, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].startswith("Merge gate verify: VERIFIED, receipt ")
    assert lines[1:] == [("Rules lowered by the operator: --min-age 0 (default 60), "
                          "--min-reviews 1 (default 2)")]


def test_verify_ties_the_receipt_to_the_pair_being_merged(world, capsys):
    w = world
    receipt = _gate(w, capsys, _pair(w))[3]                # the base was given as a sha
    assert _verify(w, capsys, receipt, "--candidate", w.cand, "--base", w.base)[0] == 0
    assert _verify(w, capsys, receipt, "--candidate", "cand", "--base", "HEAD")[0] == 0
    rc, out, err = _verify(w, capsys, receipt, "--candidate", w.base)
    assert (rc, err) == (1, "") and "  gate: CANDIDATE_MISMATCH: " in out
    rc, out, err = _verify(w, capsys, receipt, "--base", w.cand)
    assert (rc, err) == (1, "") and "  gate: BASE_MISMATCH: " in out
    # the tip moves after the gate: a base given as a sha did not see it (limit L6) and a plain verify
    # does not either; --base HEAD does
    _git(w.repo, "commit", "--allow-empty", "-qm", "another merge")
    assert _verify(w, capsys, receipt)[0] == 0
    rc, out, err = _verify(w, capsys, receipt, "--base", "HEAD")
    assert (rc, err) == (1, "")
    assert "  gate: BASE_MISMATCH: " in out and "STALE" not in out
    # a pair that is not a commit is no decision
    _refused(_verify(w, capsys, receipt, "--candidate", "no-such-ref"))
    _refused(_verify(w, capsys, receipt, "--base=--all"))


def test_the_size_of_a_report_is_compared_before_and_after_and_with_the_bytes_read(
        world, capsys, monkeypatch):
    w = world
    mg = _mg()
    reviews = _pair(w)
    calls: list[int] = []

    def resized(extra_after: int, extra_before: int):
        def fake(fd: int):
            real = os.fstat(fd)
            calls.append(fd)
            extra = extra_after if len(calls) % 2 == 0 else extra_before
            return SimpleNamespace(st_size=real.st_size + extra, st_mtime_ns=real.st_mtime_ns,
                                   st_dev=real.st_dev, st_ino=real.st_ino)
        return fake

    # the size differs between the two stats (the second one agrees with the bytes read), the
    # modification time does not; and the two stats agree with each other but not with the bytes read
    for label, shift in (("size changed while reading", (0, -1)), ("bytes read differ from the size", (1, 1))):
        monkeypatch.setattr(mg, "_fstat", resized(*shift))
        rc, _out, err, receipt = _gate(w, capsys, reviews)
        assert (rc, err) == (1, ""), label
        body = _load(receipt)
        assert [r["problems"] for r in body["reviews"]] == [["REPORT_UNSTABLE"]] * 2, label
    monkeypatch.setattr(mg, "_fstat", resized(0, 0))      # control: the same wrapper, nothing shifted
    assert _gate(w, capsys, reviews)[0] == 0


def test_at_is_utc_and_the_local_zone_is_never_consulted(world, monkeypatch):
    w = world
    mg = _mg()
    pair = [("a", _report(w, "a")), ("b", _report(w, "b"))]

    def local_time(*args):
        raise AssertionError("the receipt's time must not depend on the local zone")

    with monkeypatch.context() as patched:
        patched.setattr(mg.time, "localtime", local_time)
        body = mg.evaluate(str(w.repo), w.cand, w.base, w.spec, pair, now=1_700_000_000)
    assert body["at"] == "2023-11-14T22:13:20Z"


def test_git_runs_with_the_hardening_the_spec_names(world, capsys, monkeypatch):
    w = world
    elsewhere = w.tmp / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    calls = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), kwargs))
        return real_run(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "run", spy)
        rc = _gate(w, capsys, _pair(w))[0]
    assert rc == 0
    assert len(calls) == 3                                  # two revisions and the specification blob
    for argv, kwargs in calls:
        joined = " ".join(argv)
        assert argv[0] == "git", argv
        for setting in ("core.fsmonitor=false", "core.hooksPath=/dev/null",
                        "advice.graftFileDeprecated=false"):
            assert f"-c {setting}" in joined, (setting, argv)
        assert argv[argv.index("-C") + 1] == str(w.repo.resolve()), argv
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["capture_output"] is True
        assert kwargs["timeout"] > 0
        assert "GIT_DIR" not in kwargs["env"] and kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
