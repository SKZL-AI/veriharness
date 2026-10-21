"""The supervisor's drift channel carries measured drift, or says it was not measured.

Review findings F10 and M7: `ReviewCapsule.changed_source_digests` was declared,
bounded and documented as "item_id -> new digest", and nothing produced it -- every
capsule carried `{}`, which reads exactly like "drift was measured and there was
none". That is NOT_RUN rounded to a pass.

One case per rule, each next to a negative control that changes only the condition
under test. Every case builds a throwaway repository in `tmp_path` with a small
hand-built register and matrix -- `reconciled` accepts those, as
`tests/test_programme_supervisor.py` already relies on -- and the files the matrix
evidence names. Files are moved away by renaming, never removed.

Only the two K6 tests read the real `dogfood/p2-gate`, read-only (Revision 5 rule 7,
Revision 6 item 2); every other case keeps its reviews under `tmp_path`. This file
starts no child process (Revision 6a item 1).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "programme_supervisor", ROOT / "tools" / "programme_supervisor.py")
ps = importlib.util.module_from_spec(_spec)
sys.modules["programme_supervisor"] = ps
_spec.loader.exec_module(ps)

from hoh.supervisor import ReviewCapsule  # noqa: E402 - after the path set-up above

PHASE = "PX"
EMPTY = ps._sha256([])


def _req(rid: str) -> dict:
    return {"id": rid, "name": f"requirement {rid}", "target_phase": PHASE,
            "authoritative_item": f"{PHASE}.{rid}", "core_required_1_0": True,
            "optional_provider": False}


def _register(*ids: str) -> dict:
    return {"plan_sha256": "plandigest",
            "requirements": [_req(i) for i in (ids or ("X-01", "X-02"))]}


def _matrix(evidence: dict[str, list[str]]) -> dict:
    return {"capabilities": [{"id": rid, "status": "PROVEN", "evidence": ev}
                             for rid, ev in evidence.items()]}


def _repo(tmp_path: Path) -> Path:
    """A repository with one source file per item, named by `path:line` evidence."""
    for name in ("x01", "x02"):
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / f"{name}.py").write_text(f"# {name}\n", encoding="utf-8")
    return tmp_path


REGISTER = _register()
MATRIX = _matrix({"X-01": ["src/x01.py:1"], "X-02": ["src/x02.py:3"]})


def _capsule(repo: Path, baseline=None, review_id=None, **kw):
    return ps.capsule_for(PHASE, register=kw.pop("register", REGISTER),
                          matrix=kw.pop("matrix", MATRIX),
                          previous_source_digests=baseline,
                          previous_review_id=review_id, repo=repo, **kw)


def _preserve(root: Path, stamp: str, capsule=None, *, raw: dict | None = None,
              findings: bool = True, text: str | None = None) -> Path:
    """Write a preserved review where `drift_baseline_for` looks for it: the capsule
    and, unless `findings=False`, the `findings.json` that makes it a *reviewed* brief.
    `text` writes the capsule file verbatim (a corrupt one)."""
    d = root / stamp
    d.mkdir(parents=True, exist_ok=True)
    if text is None:
        body = raw if raw is not None else json.loads(
            capsule.model_dump_json(by_alias=True))
        text = json.dumps(body)
    (d / f"capsule-{PHASE.lower()}.json").write_text(text, encoding="utf-8")
    if findings:
        (d / "findings.json").write_text("[]", encoding="utf-8")
    return d


def _write(repo: Path, rel: str, text: str = "") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or f"# {rel}\n", encoding="utf-8")
    return path


#: Open watch lists of `_touched`; the audit hook below appends to each.
_WATCHES: list[list[str]] = []
_HOOKED: list[bool] = []


def _audit(event: str, args: tuple) -> None:
    if _WATCHES and event in ("open", "os.listdir", "os.scandir") and args and \
            isinstance(args[0], (str, bytes, os.PathLike)):
        for seen in _WATCHES:
            seen.append(os.fsdecode(args[0]))


@contextlib.contextmanager
def _touched():
    """Every path opened, listed or scanned inside the block, as it was passed (not
    resolved). An audit hook cannot be removed, so one is installed once and records
    only while a block is open."""
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED.append(True)
    seen: list[str] = []
    _WATCHES.append(seen)
    try:
        yield seen
    finally:
        _WATCHES.remove(seen)


def _reached(seen: Iterable[str], place: Path) -> list[str]:
    """The recorded paths that are `place` or lie under it, as passed or resolved."""
    place_s, real = str(place), os.path.realpath(place)
    return [p for p in seen
            if p == place_s or p.startswith(place_s + os.sep)
            or os.path.realpath(p) == real or os.path.realpath(p).startswith(real + os.sep)]


# --------------------------------------------------------------------------- #
# K1 -- drift is measured against a baseline
# --------------------------------------------------------------------------- #


def test_an_edited_evidence_file_is_measured_as_changed(tmp_path):
    """Catches `changed_source_digests` staying `{}` whatever happened to the source:
    editing the file X-01's evidence names must put X-01 (and only X-01) in it.
    Control: the same capsule over the same baseline with no edit names nothing."""
    repo = _repo(tmp_path)
    baseline = _capsule(repo).selection.source_digests

    unedited = _capsule(repo, baseline, "20260925T0000Z")
    assert unedited.changed_source_digests == {}
    assert unedited.selection.drift == "MEASURED"

    (repo / "src/x01.py").write_text("# x01, edited\n", encoding="utf-8")
    edited = _capsule(repo, baseline, "20260925T0000Z")
    assert set(edited.changed_source_digests) == {"X-01"}
    assert edited.changed_source_digests["X-01"] == edited.selection.source_digests["X-01"]
    assert edited.changed_source_digests["X-01"] != baseline["X-01"]
    # The product's own bound check ran over the produced mapping and accepted it.
    ReviewCapsule.model_validate(edited.model_dump(by_alias=True, exclude={"selection"}))


def test_an_item_absent_from_the_baseline_is_measured_as_changed(tmp_path):
    """Catches `baseline.get(item) != digest` being written as `item in baseline and
    ...`, which would report an item the previous review never saw as unchanged.
    Control: the same item present in the baseline with an equal digest is not."""
    repo = _repo(tmp_path)
    full = _capsule(repo).selection.source_digests

    present = _capsule(repo, dict(full), "20260925T0000Z")
    assert "X-02" not in present.changed_source_digests

    absent = _capsule(repo, {"X-01": full["X-01"]}, "20260925T0000Z")
    assert absent.changed_source_digests == {"X-02": full["X-02"]}


def test_a_missing_evidence_file_digest_differs_from_the_present_file_measured(tmp_path):
    """Catches a missing file being skipped instead of digested as `<missing>`: the
    row would then digest like a row naming nothing, and a file vanishing would not be
    drift. Control: the file present gives a different digest, and moving it back
    restores that digest exactly."""
    repo = _repo(tmp_path)
    row = {"id": "X-01", "evidence": ["src/x01.py:1"]}
    present = ps.item_source_digest(row, repo)

    (repo / "src/x01.py").rename(repo / "src/x01.py.moved")
    missing = ps.item_source_digest(row, repo)
    assert missing != present
    assert missing != EMPTY
    assert missing == ps._sha256([["src/x01.py", ps.MISSING_FILE]])

    (repo / "src/x01.py.moved").rename(repo / "src/x01.py")
    assert ps.item_source_digest(row, repo) == present


def test_with_a_baseline_drift_is_measured_and_names_the_review(tmp_path):
    """Catches a capsule built over a baseline still calling itself NOT_RUN, or not
    saying which review it compared against -- the control of the NOT_RUN case below,
    changing only whether a baseline is passed."""
    repo = _repo(tmp_path)
    baseline = _capsule(repo).selection.source_digests
    (repo / "src/x02.py").write_text("# x02, edited\n", encoding="utf-8")

    capsule = _capsule(repo, baseline, "20260925T0000Z")
    assert capsule.selection.drift == "MEASURED"
    assert capsule.selection.drift_baseline == "20260925T0000Z"
    line = capsule.evidence_summaries[0]
    assert line.startswith("SELECTION:")
    assert "drift MEASURED: 1 of 2 item(s) changed against 20260925T0000Z" in line
    assert "drift NOT_RUN" not in line


@pytest.mark.parametrize("evidence, named", [
    ("src/a.py:12", ["src/a.py"]),
    ("test tests/test_a.py: collected 3 item(s)", ["tests/test_a.py"]),
    ("record dogfood/a/RECORD.json: satisfied=True, re-derivation ok",
     ["dogfood/a/RECORD.json"]),
    ("record dogfood/a/RECORD.json [stressor:DS2]: ok", ["dogfood/a/RECORD.json"]),
    ("defined in an entry point: tools/a.py:735", ["tools/a.py"]),
    ("named in an import statement by src/a.py, tools/b.py, tools/c.py "
     "(text, not execution -- see O222)", ["src/a.py", "tools/b.py", "tools/c.py"]),
])
def test_each_evidence_format_contributes_its_files_measured(tmp_path, evidence, named):
    """Catches an evidence format whose files are never digested: editing any file it
    names must change the item digest. Control: before the edit the digest is stable,
    and it is not the empty-list digest a row naming nothing gets."""
    for rel in named:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"# {rel}\n", encoding="utf-8")
    row = {"id": "X-01", "evidence": [evidence]}
    assert ps.evidence_paths([evidence]) == sorted(named)
    before = ps.item_source_digest(row, tmp_path)
    assert before == ps.item_source_digest(row, tmp_path)
    assert before != EMPTY
    for rel in named:
        (tmp_path / rel).write_text(f"# {rel}, edited\n", encoding="utf-8")
        after = ps.item_source_digest(row, tmp_path)
        assert after != before, f"editing {rel} did not change the digest"
        before = after


@pytest.mark.parametrize("prose", [
    "CAPPED at PARTIAL: a per-project flock, not a renewable lease",
    "7 hit(s) for 'check' outside decisioncheck.py ignored (O220/F7)",
    "no importer named in any entry point (text, not execution -- see O225/C4)",
])
def test_a_prose_only_row_is_measured_as_the_empty_list_digest(tmp_path, prose):
    """Catches a prose token that names no file of the repository being digested
    anyway (as `<missing>`): `decisioncheck.py` here exists only as
    `src/hoh/decisioncheck.py`, so the bare name is a guess that hits nothing, and a row
    of such prose must digest as the empty list (Revision 4 item 1 counts a prose token
    only when the file exists). Control: the same row with a `path:line` entry added
    does not."""
    _write(tmp_path, "src/hoh/decisioncheck.py", "x = 1\n")
    assert ps.item_source_digest({"evidence": [prose]}, tmp_path) == EMPTY
    assert ps.item_source_digest({"evidence": []}, tmp_path) == EMPTY
    assert ps.item_source_digest(None, tmp_path) == EMPTY
    assert ps.item_source_digest(
        {"evidence": [prose, "src/hoh/decisioncheck.py:1"]}, tmp_path) != EMPTY


def test_an_evidence_path_outside_the_repo_is_measured_as_missing(tmp_path):
    """Catches evidence reading a file outside the repository (`..`, an absolute
    path): it is digested as `<missing>`, never read. Control: the same file inside
    the repository is read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("secret\n", encoding="utf-8")
    (repo / "inside.py").write_text("secret\n", encoding="utf-8")
    assert ps._file_digest("../outside.py", repo) == ps.MISSING_FILE
    assert ps._file_digest(str(tmp_path / "outside.py"), repo) == ps.MISSING_FILE
    assert ps._file_digest("inside.py", repo) != ps.MISSING_FILE


def test_a_selection_claiming_measured_drift_without_a_baseline_is_refused(tmp_path):
    """Catches a Selection whose drift fields contradict each other: MEASURED without
    naming what it measured against or with a reason for not measuring, NOT_RUN while
    naming a baseline or -- once it carries source digests -- without saying why
    (Revision 5 rule 4). Control: the consistent combinations load."""
    body = _capsule(_repo(tmp_path)).selection.model_dump()
    assert body["drift_reason"] == "no preserved baseline" and body["source_digests"]
    measured = {"drift": "MEASURED", "drift_baseline": "R", "drift_reason": None}
    for bad in ({**measured, "drift_baseline": None},
                {**measured, "drift_reason": "no preserved baseline"},
                {"drift": "NOT_RUN", "drift_baseline": "R"},
                {"drift": "NOT_RUN", "drift_baseline": None, "drift_reason": None},
                {"drift": "NOT_RUN", "drift_baseline": None, "drift_reason": ""},
                {"drift": "SKIPPED", "drift_baseline": None}):
        with pytest.raises(ValidationError):
            ps.Selection.model_validate({**body, **bad})
    ps.Selection.model_validate({**body, **measured})
    ps.Selection.model_validate({**body, "drift": "NOT_RUN", "drift_baseline": None})


def test_canonical_brief_is_measured_against_the_latest_review_strictly_before(tmp_path):
    """Catches the gate's rebuild using the review under check as its own baseline
    (Revision 3), or an older review being preferred to the latest earlier one:
    with `before=S`, a review stamped at S or later is ignored and the most recent one
    before S is used. Control: `before=None` takes the newest of them all."""
    repo = _repo(tmp_path)
    root = repo / "dogfood/p2-gate"
    brief = lambda before: ps.canonical_brief(  # noqa: E731
        register=REGISTER, matrix=MATRIX, phase=PHASE, review_root=root, before=before)

    _preserve(root, "20260924T0030Z", brief(None))            # recorded untouched
    (repo / "src/x01.py").write_text("# x01, edited\n", encoding="utf-8")
    _preserve(root, "20260925T101500Z", brief(None))           # recorded after the edit
    _preserve(root, "20260926T0000Z", brief(None))             # the one under check

    under_check = brief("20260926T0000Z")
    assert under_check.selection.drift == "MEASURED"
    assert under_check.selection.drift_baseline == "20260925T101500Z"
    assert under_check.changed_source_digests == {}

    older = brief("20260925T1015Z")                             # same instant: excluded
    assert older.selection.drift_baseline == "20260924T0030Z"
    assert set(older.changed_source_digests) == {"X-01"}

    assert brief(None).selection.drift_baseline == "20260926T0000Z"


# --------------------------------------------------------------------------- #
# K2 -- without a baseline the capsule says NOT_RUN
# --------------------------------------------------------------------------- #


def test_no_baseline_is_not_run_and_the_summary_says_so(tmp_path):
    """Catches the F10/M7 defect itself: a capsule with no baseline carrying `{}` and
    saying nothing, which reads as "measured, nothing changed". Control: the case
    above, which passes a baseline and gets MEASURED."""
    repo = _repo(tmp_path)
    (repo / "src/x01.py").write_text("# x01, edited\n", encoding="utf-8")
    capsule = _capsule(repo)
    assert capsule.selection.drift == "NOT_RUN"
    assert capsule.selection.drift_baseline is None
    assert capsule.changed_source_digests == {}
    assert set(capsule.selection.source_digests) == {"X-01", "X-02"}
    line = capsule.evidence_summaries[0]
    assert line.startswith("SELECTION:")
    assert "drift NOT_RUN: no preserved baseline" in line
    assert "nothing left out" in line


def test_the_not_run_clause_survives_the_left_out_list_being_cut(tmp_path):
    """Catches the drift clause landing after the left-out ids, where the summary's
    length cut removes it first. Control: the left-out count is still reported."""
    repo = _repo(tmp_path)
    ids = [f"X-{n:02d}" for n in range(1, 13)]
    capsule = _capsule(repo, register=_register(*ids),
                       matrix=_matrix({i: [] for i in ids}),
                       max_items=2, max_summary_chars=260)
    line = capsule.evidence_summaries[0]
    assert len(line) <= 260
    assert "drift NOT_RUN: no preserved baseline" in line
    assert "10 left out" in line


def test_a_predecessor_without_source_digests_is_not_run(tmp_path):
    """Catches a baseline being invented: when the latest earlier reviewed brief
    predates `source_digests`, drift is NOT_RUN with the reason `baseline <id> carries
    no source digests` -- no fallback to an even older review that has them, no `{}`
    passed off as a baseline. Control: once that review is not the latest earlier one,
    the older one is used."""
    repo = _repo(tmp_path)
    root = repo / "dogfood/p2-gate"
    brief = lambda before: ps.canonical_brief(  # noqa: E731
        register=REGISTER, matrix=MATRIX, phase=PHASE, review_root=root, before=before)
    _preserve(root, "20260924T0030Z", brief(None))
    legacy = json.loads(brief(None).model_dump_json(by_alias=True))
    for key in ("source_digests", "drift", "drift_baseline", "drift_reason"):
        legacy["selection"].pop(key)
    _preserve(root, "20260925T0000Z", raw=legacy)
    (repo / "src/x01.py").write_text("# x01, edited\n", encoding="utf-8")

    reason = "baseline 20260925T0000Z carries no source digests"
    assert ps.drift_baseline_for(PHASE, before="20260926T0000Z", root=root) == (
        None, None, reason)
    capsule = brief("20260926T0000Z")
    assert capsule.selection.drift == "NOT_RUN"
    assert capsule.selection.drift_reason == reason
    assert capsule.changed_source_digests == {}
    assert capsule.evidence_summaries[0].startswith(f"SELECTION: drift NOT_RUN: {reason};")

    # Control: before the legacy review, the one with digests is the baseline.
    control = brief("20260925T0000Z")
    assert control.selection.drift_baseline == "20260924T0030Z"
    assert set(control.changed_source_digests) == {"X-01"}


def test_no_preserved_review_at_all_is_not_run(tmp_path):
    """Catches the baseline helper returning `{}` (a baseline in which every item is
    absent) when there is nothing preserved: it answers `no preserved baseline`.
    Control: a reviewed brief whose directory name is not a stamp is not a review
    either, while the same brief under a stamp is."""
    root = tmp_path / "dogfood/p2-gate"
    nothing = (None, None, "no preserved baseline")
    assert ps.drift_baseline_for(PHASE, before=None, root=root) == nothing
    capsule = _capsule(_repo(tmp_path))
    _preserve(root, "draft", capsule)
    assert ps.drift_baseline_for(PHASE, before=None, root=root) == nothing
    _preserve(root, "20260925T0000Z", capsule)
    assert ps.drift_baseline_for(PHASE, before=None, root=root) == (
        capsule.selection.source_digests, "20260925T0000Z", None)


def test_a_capsule_preserved_before_the_drift_fields_still_loads_as_not_run(tmp_path):
    """Catches the new Selection keys being required: capsules preserved under
    dogfood/p2-gate before they existed must still load. Built under `tmp_path`; the
    real preserved capsules are loaded by the K6 test, the only place that reads them
    (Revision 6 item 2). Control: the same body with its keys loads as well."""
    full = json.loads(_capsule(_repo(tmp_path)).model_dump_json(by_alias=True))
    ps.ProgrammeCapsule.model_validate(full)
    body = json.loads(json.dumps(full))
    for key in ("source_digests", "drift", "drift_baseline"):
        body["selection"].pop(key)
    loaded = ps.ProgrammeCapsule.model_validate(body)
    assert loaded.selection.drift == "NOT_RUN"
    assert loaded.selection.source_digests == {}


# --------------------------------------------------------------------------- #
# main -- the CLI passes the preserved baseline, and never invents one
# --------------------------------------------------------------------------- #


def _cli_tree(tmp_path, monkeypatch) -> Path:
    """A register and matrix whose evidence names no file, and the CLI's tree (`HOH`)
    and review root both under `tmp_path`: `main` checks its review root against its
    own tree (Revision 6a item 3), and no case here may reach this checkout's
    `dogfood/p2-gate` (rule 7)."""
    register, matrix = _register("X-01", "X-02"), _matrix({"X-01": [], "X-02": []})
    (tmp_path / "REQ.json").write_text(json.dumps(register), encoding="utf-8")
    (tmp_path / "MAT.json").write_text(json.dumps(matrix), encoding="utf-8")
    monkeypatch.setattr(ps, "REGISTER", tmp_path / "REQ.json")
    monkeypatch.setattr(ps, "MATRIX", tmp_path / "MAT.json")
    monkeypatch.setattr(ps, "HOH", tmp_path)
    root = tmp_path / "dogfood/p2-gate"
    monkeypatch.setattr(ps, "REVIEW_ROOT", root)
    return root


def _cli(capsys) -> dict:
    assert ps.main(["--phase", PHASE]) == 0
    return json.loads(capsys.readouterr().out)


def test_main_is_measured_against_the_latest_preserved_review(tmp_path, monkeypatch, capsys):
    """Catches `main` building its capsule without the preserved baseline. The
    baseline is a reviewed brief whose digests hold X-01 only, so X-02 counts as
    changed. Control: see the NOT_RUN case below."""
    root = _cli_tree(tmp_path, monkeypatch)
    body = json.loads(ps.capsule_for(
        PHASE, register=_register("X-01", "X-02"),
        matrix=_matrix({"X-01": [], "X-02": []}), repo=tmp_path).model_dump_json(by_alias=True))
    body["selection"]["source_digests"] = {"X-01": EMPTY}
    _preserve(root, "20260924T0030Z", raw=body)
    out = _cli(capsys)
    assert out["selection"]["drift"] == "MEASURED"
    assert out["selection"]["drift_baseline"] == "20260924T0030Z"
    assert out["changed_source_digests"] == {"X-02": EMPTY}


def test_main_without_a_preserved_review_is_not_run(tmp_path, monkeypatch, capsys):
    """Catches `main` inventing a baseline (e.g. `{}`) when nothing is preserved, which
    would report every item as changed. Control: the MEASURED case above."""
    _cli_tree(tmp_path, monkeypatch)
    out = _cli(capsys)
    assert out["selection"]["drift"] == "NOT_RUN"
    assert out["changed_source_digests"] == {}
    assert "drift NOT_RUN: no preserved baseline" in out["evidence_summaries"][0]


def test_main_reason_printed_for_an_ambiguous_baseline(tmp_path, monkeypatch, capsys):
    """Catches the non-canonical CLI dropping `drift_baseline_for`'s reason and printing
    the default `no preserved baseline` instead (mutant `main_reason_dropped`): with two
    reviewed briefs at the same parsed time it prints `ambiguous baseline:
    20260925T101500Z and 20260925T1015Z`, the names sorted. Control: one second apart,
    the later one is the baseline and drift is MEASURED."""
    root = _cli_tree(tmp_path, monkeypatch)
    capsule = ps.capsule_for(PHASE, register=_register("X-01", "X-02"),
                             matrix=_matrix({"X-01": [], "X-02": []}), repo=tmp_path)
    _preserve(root, "20260925T1015Z", capsule)
    tied = _preserve(root, "20260925T101500Z", capsule)
    reason = "ambiguous baseline: 20260925T101500Z and 20260925T1015Z"

    out = _cli(capsys)
    assert out["selection"]["drift"] == "NOT_RUN"
    assert out["selection"]["drift_reason"] == reason
    assert out["evidence_summaries"][0].startswith(f"SELECTION: drift NOT_RUN: {reason};")
    assert "no preserved baseline" not in out["evidence_summaries"][0]

    tied.rename(root / "20260925T101501Z")
    control = _cli(capsys)
    assert control["selection"]["drift"] == "MEASURED"
    assert control["selection"]["drift_baseline"] == "20260925T101501Z"


# --------------------------------------------------------------------------- #
# K4 -- every path an item rests on is digested, and nothing else
# --------------------------------------------------------------------------- #


STALE_ROW = ("record dogfood/rec.json [P2-33]: satisfied=True, re-derivation FAILED, "
             "STALE (code changed since: src/hoh/a.py, src/hoh/b.py:12-20)")


def test_stale_paths_digested_from_the_stale_clause(tmp_path):
    """Catches the files inside an evidence string's `STALE (code changed since: ...)`
    part never being digested -- the refused run's F1, where the channel read
    `MEASURED: 0 changed` over a changed `src/hoh/runner.py`. Editing a file the clause
    names must change the item digest. Control: editing a file the string does not name
    does not; and without a repository to check against, only the record is named."""
    for rel in ("dogfood/rec.json", "src/hoh/a.py", "src/hoh/b.py", "src/hoh/c.py"):
        _write(tmp_path, rel)
    row = {"evidence": [STALE_ROW]}
    assert ps.evidence_paths([STALE_ROW], tmp_path) == [
        "dogfood/rec.json", "src/hoh/a.py", "src/hoh/b.py"]
    assert ps.evidence_paths([STALE_ROW]) == ["dogfood/rec.json"]
    before = ps.item_source_digest(row, tmp_path)

    _write(tmp_path, "src/hoh/c.py", "# c, edited\n")           # control: not named
    assert ps.item_source_digest(row, tmp_path) == before

    _write(tmp_path, "src/hoh/b.py", "# b, edited\n")
    assert ps.item_source_digest(row, tmp_path) != before


PROSE_ROW = ("CAPPED at PARTIAL: no entry point (tools/ or src/hoh/cli.py) names "
             "src/hoh/x.py in an import statement, and ops/hoh-unit, outside "
             "decisioncheck.py (O220/F4)")


def test_prose_paths_digested_when_the_file_exists(tmp_path):
    """Catches paths named in prose (`... names src/hoh/x.py in an import statement`)
    never being digested, and an extensionless one (`ops/hoh-unit`) being ignored:
    editing either must change the item digest. Control: a prose token naming no file
    of the repository (`decisioncheck.py`, which exists only under `src/hoh/`, and the
    directory `tools/`) is not digested at all -- not even as `<missing>` -- so editing
    the file it does not name changes nothing."""
    for rel in ("src/hoh/cli.py", "src/hoh/x.py", "ops/hoh-unit",
                "src/hoh/decisioncheck.py", "tools/t.py"):
        _write(tmp_path, rel)
    row = {"evidence": [PROSE_ROW]}
    assert ps.item_source_paths(row, tmp_path) == [
        "ops/hoh-unit", "src/hoh/cli.py", "src/hoh/x.py"]
    before = ps.item_source_digest(row, tmp_path)

    _write(tmp_path, "src/hoh/decisioncheck.py", "# edited\n")  # control: not named
    assert ps.item_source_digest(row, tmp_path) == before

    _write(tmp_path, "src/hoh/x.py", "# x, edited\n")
    after_x = ps.item_source_digest(row, tmp_path)
    assert after_x != before
    _write(tmp_path, "ops/hoh-unit", "# unit, edited\n")
    assert ps.item_source_digest(row, tmp_path) != after_x


def _durability_repo(repo: Path) -> Path:
    for rel in ("dogfood/rec.json", "dogfood/ev/scenario.json", "src/hoh/named.py",
                "src/hoh/unnamed.py", "src/hoh/sub/deep.py", "tools/durability_e2e.py",
                "tools/durability_fixture.py", "ops/hoh-continue.service"):
        _write(repo, rel)
    return repo


DURABILITY_PROBE = {"kind": "evidence", "path": "dogfood/rec.json",
                    "evidence_dir": "dogfood/ev", "deriver": "durability_e2e.py"}
RECORD_ROW = {"evidence": ["record dogfood/rec.json [P2-33]: satisfied=True, "
                           "re-derivation ok, STALE (code changed since: src/hoh/named.py)"]}


def test_record_sources_digested_under_a_durability_probe(tmp_path):
    """Catches a durability-record item resting only on the three paths its STALE clause
    happens to name (Revision 5): under a probe whose deriver is `durability_e2e.py`, an
    edit to an *unnamed* file under `RELEVANT_PATHS` must change the item, and a file
    entry such as `tools/durability_e2e.py` is in the set as itself. Control: the same
    row under another deriver does not move; and the record's evidence directory is
    never digested."""
    repo = _durability_repo(tmp_path)
    other = {**DURABILITY_PROBE, "deriver": "capability_matrix.py"}
    paths = ps.item_source_paths(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    assert {"dogfood/rec.json", "src/hoh/named.py", "src/hoh/unnamed.py",
            "src/hoh/sub/deep.py", "tools/durability_e2e.py",
            "tools/durability_fixture.py", "ops/hoh-continue.service"} <= set(paths)
    assert not any(p.startswith("dogfood/ev") for p in paths)
    assert ps.item_source_paths(RECORD_ROW, repo, probe=other) == [
        "dogfood/rec.json", "src/hoh/named.py"]

    durable = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    plain = ps.item_source_digest(RECORD_ROW, repo, probe=other)
    _write(repo, "dogfood/ev/scenario.json", "{\"edited\": true}\n")
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == durable

    _write(repo, "src/hoh/sub/deep.py", "# deep, edited\n")
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) != durable
    assert ps.item_source_digest(RECORD_ROW, repo, probe=other) == plain


def test_record_sources_digested_through_capsule_for_from_the_register_probe(tmp_path):
    """Catches `capsule_for` not passing each item's register probe on: an edit to an
    unnamed file under `RELEVANT_PATHS` must reach `changed_source_digests` for the item
    whose register probe is a durability record. Control: the item without such a probe
    stays unchanged."""
    repo = _durability_repo(tmp_path)
    register = _register()
    register["requirements"][0]["probe"] = DURABILITY_PROBE
    matrix = _matrix({"X-01": RECORD_ROW["evidence"], "X-02": ["src/hoh/named.py:1"]})
    baseline = _capsule(repo, register=register, matrix=matrix).selection.source_digests
    _write(repo, "src/hoh/unnamed.py", "# unnamed, edited\n")
    capsule = _capsule(repo, baseline, "20260925T0000Z", register=register, matrix=matrix)
    assert set(capsule.changed_source_digests) == {"X-01"}


IGNORED_ADDITIONS = [
    "src/hoh/__pycache__/x.cpython-312.pyc", "src/hoh/x.pyc", "src/hoh/x.py.bak",
    "src/hoh/.pytest_cache/x", "src/hoh/runs/x.py", "src/hoh/demo/x.py",
    "src/hoh/.venv/x.py", "src/hoh/.ruff_cache/x", "src/hoh/.cache/x",
    "src/hoh/build/x.py", "src/hoh/pkg.egg-info/x", "src/hoh/c4-probe/x",
    "src/hoh/.hoh-repair-x.md",
    # Revision 6 item 4: as in git, the no-slash file patterns ignore directories too.
    "src/hoh/old.bak/a.py", "src/hoh/old.pyc/a.py", "src/hoh/.hoh-repair-x.md/a.py",
]


@pytest.mark.parametrize("added", IGNORED_ADDITIONS)
def test_ignored_names_not_digested(tmp_path, added):
    """Catches the source walk digesting what `durability_e2e.stale_paths` never sees
    -- the `.gitignore` patterns under `RELEVANT_PATHS` (`git ls-files --others
    --exclude-standard` drops them): adding any of them must leave the item digest and
    its path set unchanged, so a test run's caches are not drift. Control: the case
    below, an untracked `.py`, which does move it."""
    repo = _durability_repo(tmp_path)
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    _write(repo, added)
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == before
    assert added not in ps.item_source_paths(RECORD_ROW, repo, probe=DURABILITY_PROBE)


def test_ignored_names_not_digested_but_an_untracked_py_is(tmp_path):
    """Catches an ignore list so broad that real sources fall out of the walk: an
    added untracked `src/hoh/new.py` -- which `stale_paths` reports -- must change the
    item digest. The control of the ignored cases above."""
    repo = _durability_repo(tmp_path)
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    _write(repo, "src/hoh/new.py")
    assert "src/hoh/new.py" in ps.item_source_paths(RECORD_ROW, repo,
                                                    probe=DURABILITY_PROBE)
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) != before


def test_ignored_names_not_digested_but_a_plain_old_directory_is_walked(tmp_path):
    """Catches the directory form of the file patterns (Revision 6 item 4) matching too
    much -- a substring or prefix match dropping `old/` along with `old.bak/`: a file
    under a directory named `old` must still be in the set and move the item. The
    control of the `old.bak/`, `old.pyc/` cases above."""
    repo = _durability_repo(tmp_path)
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    _write(repo, "src/hoh/old/a.py")
    assert "src/hoh/old/a.py" in ps.item_source_paths(RECORD_ROW, repo,
                                                      probe=DURABILITY_PROBE)
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) != before


def test_pycache_non_pyc_ignored_as_a_directory(tmp_path):
    """Catches `__pycache__` missing from the ignored directories (mutant
    `no_ignore_pycache`), which the `.pyc` case cannot see because `*.pyc` drops that
    file anyway: a file under `__pycache__/` that is not a `.pyc` must leave the item
    unchanged. Control: the same file one level up is digested and moves it."""
    repo = _durability_repo(tmp_path)
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    _write(repo, "src/hoh/__pycache__/notes.txt")
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == before
    assert not any("__pycache__" in p for p in
                   ps.item_source_paths(RECORD_ROW, repo, probe=DURABILITY_PROBE))

    _write(repo, "src/hoh/notes.txt")
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) != before


def test_record_path_digested_when_the_evidence_does_not_name_it(tmp_path):
    """Catches a durability item resting on the record only because its evidence names
    it (mutant `no_record_path`): under a durability probe whose record the evidence
    does not mention -- and which lies outside `RELEVANT_PATHS` -- editing the record
    must change the item. Control: under another deriver the record is not in the set
    and the same edit changes nothing."""
    repo = _durability_repo(tmp_path)
    _write(repo, "dogfood/other/RECORD.json", "{}\n")
    probe = {**DURABILITY_PROBE, "path": "dogfood/other/RECORD.json"}
    other = {**probe, "deriver": "capability_matrix.py"}
    row = {"evidence": ["src/hoh/named.py:1"]}
    assert "dogfood/other/RECORD.json" in ps.item_source_paths(row, repo, probe=probe)
    assert "dogfood/other/RECORD.json" not in ps.item_source_paths(row, repo, probe=other)
    durable = ps.item_source_digest(row, repo, probe=probe)
    plain = ps.item_source_digest(row, repo, probe=other)

    _write(repo, "dogfood/other/RECORD.json", "{\"edited\": true}\n")
    assert ps.item_source_digest(row, repo, probe=probe) != durable
    assert ps.item_source_digest(row, repo, probe=other) == plain


def test_symlink_marked_not_followed_and_retargeting_is_drift(tmp_path):
    """Catches the walk following a symlink -- out of the repository, or into a
    directory it would then digest -- or skipping it silently, or marking it with a
    constant: a link contributes `<symlink>` plus its link text, so editing what it
    points to changes nothing and retargeting it changes the digest. Control: the same
    name as a regular file digests its content."""
    repo = _durability_repo(tmp_path / "repo")
    _write(tmp_path, "outside/one.py", "one\n")
    _write(tmp_path, "outside/two.py", "two\n")
    link = repo / "src/hoh/link.py"
    os.symlink("../../../outside/one.py", link)
    os.symlink("../../../outside", repo / "src/hoh/linkdir")

    paths = ps.item_source_paths(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    assert "src/hoh/link.py" in paths and "src/hoh/linkdir" in paths
    assert not any(p.startswith("src/hoh/linkdir/") for p in paths)
    assert ps._file_digest("src/hoh/link.py", repo) == "<symlink>../../../outside/one.py"
    assert ps._file_digest("src/hoh/linkdir", repo) == "<symlink>../../../outside"
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)

    _write(tmp_path, "outside/one.py", "one, edited\n")        # not followed
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == before

    # Retarget: the old link is set aside under an ignored name, never removed.
    link.rename(repo / "src/hoh/link.py.v1.bak")
    os.symlink("../../../outside/two.py", link)
    retargeted = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    assert retargeted != before

    # Control: a regular file under the same name is read.
    link.rename(repo / "src/hoh/link.py.v2.bak")
    _write(repo, "src/hoh/link.py", "two\n")
    assert ps._file_digest("src/hoh/link.py", repo) == ps.hashlib.sha256(b"two\n").hexdigest()


# --------------------------------------------------------------------------- #
# K5 -- the baseline is the latest reviewed brief, unambiguous, or NOT_RUN
# --------------------------------------------------------------------------- #


def test_reviewed_baseline_only_counts(tmp_path):
    """Catches any stamped directory holding a parseable capsule serving as the
    baseline -- the refused run's F2 -- when it was never reviewed (no `findings.json`),
    is a symlink, or is not stamped: each such later directory is passed over and the
    reviewed brief stays the baseline. Control: giving the later directory its
    `findings.json` makes it the baseline."""
    repo = _repo(tmp_path)
    root = repo / "dogfood/p2-gate"
    capsule = _capsule(repo)
    _preserve(root, "20260925T0000Z", capsule)
    unreviewed = _preserve(root, "20260926T0000Z", capsule, findings=False)
    elsewhere = _preserve(tmp_path / "elsewhere", "20260927T0000Z", capsule)
    os.symlink(elsewhere, root / "20260927T0000Z")
    _preserve(root, "latest", capsule)

    assert ps.drift_baseline_for(PHASE, root=root)[1] == "20260925T0000Z"
    (unreviewed / "findings.json").write_text("[]", encoding="utf-8")
    assert ps.drift_baseline_for(PHASE, root=root)[1] == "20260926T0000Z"


@pytest.mark.parametrize("corrupt", ['{"selection": {"source_digests": {"X-01": "d"}}}',
                                     "{not json", "[]"])
def test_corrupt_latest_not_run_and_no_older_review_consulted(tmp_path, corrupt):
    """Catches a corrupt latest reviewed brief being skipped in favour of an older one,
    which would then be reported as MEASURED against a review that is not the one before
    this: drift is NOT_RUN with `baseline <id> is not a readable capsule`, and
    `changed_source_digests` is `{}` even over an edited file. Control: with `before`
    set to that review, the older one is the latest candidate and is measured."""
    repo = _repo(tmp_path)
    root = repo / "dogfood/p2-gate"
    _preserve(root, "20260925T0000Z", _capsule(repo))
    _preserve(root, "20260926T0000Z", text=corrupt)
    (repo / "src/x01.py").write_text("# x01, edited\n", encoding="utf-8")
    brief = lambda before: ps.canonical_brief(  # noqa: E731
        register=REGISTER, matrix=MATRIX, phase=PHASE, review_root=root, before=before)

    reason = "baseline 20260926T0000Z is not a readable capsule"
    assert ps.drift_baseline_for(PHASE, root=root) == (None, None, reason)
    capsule = brief(None)
    assert capsule.selection.drift == "NOT_RUN"
    assert capsule.selection.drift_reason == reason
    assert capsule.changed_source_digests == {}

    control = brief("20260926T0000Z")
    assert control.selection.drift_baseline == "20260925T0000Z"
    assert set(control.changed_source_digests) == {"X-01"}


def test_tied_stamps_not_run_as_an_ambiguous_baseline(tmp_path):
    """Catches two reviewed briefs at the same parsed time (`T1015Z` and `T101500Z`)
    being resolved by name or directory order: drift is NOT_RUN with `ambiguous
    baseline: <id> and <id>`. Control: one second later (`T101501Z`) the two are
    ordered by parsed time -- not ASCII, where `1015Z` sorts after `101501Z` -- and the
    later one is the baseline."""
    repo = _repo(tmp_path)
    root = repo / "dogfood/p2-gate"
    capsule = _capsule(repo)
    _preserve(root, "20260925T1015Z", capsule)
    tied = _preserve(root, "20260925T101500Z", capsule)
    assert ps.drift_baseline_for(PHASE, root=root) == (
        None, None, "ambiguous baseline: 20260925T101500Z and 20260925T1015Z")
    brief = ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                               review_root=root)
    assert brief.selection.drift == "NOT_RUN"
    assert brief.selection.drift_reason.startswith("ambiguous baseline: ")

    tied.rename(root / "20260925T101501Z")
    assert ps.drift_baseline_for(PHASE, root=root)[1:] == ("20260925T101501Z", None)


def test_repo_derived_or_refused_never_guessed(tmp_path, monkeypatch):
    """Catches `canonical_brief` digesting a tree other than the one its review root
    belongs to (Revision 5 rule 5): a root shaped `<dir>/dogfood/p2-gate` digests
    `<dir>`; an explicit `repo` wins over the root; with neither, `HOH`; and a root of
    any other shape without `repo` is refused, not mapped to `parents[1]`."""
    repo = _repo(tmp_path / "a")
    other = _repo(tmp_path / "b")
    (other / "src/x01.py").write_text("# x01 in b\n", encoding="utf-8")
    brief = lambda **kw: ps.canonical_brief(  # noqa: E731
        register=REGISTER, matrix=MATRIX, phase=PHASE, **kw).selection.source_digests
    in_a, in_b = _capsule(repo).selection.source_digests, _capsule(other).selection.source_digests
    assert in_a != in_b

    assert brief(review_root=repo / "dogfood/p2-gate") == in_a
    assert brief(review_root=repo / "dogfood/p2-gate", repo=other) == in_b
    assert brief(review_root=tmp_path / "reviews", repo=other) == in_b
    monkeypatch.setattr(ps, "HOH", other)
    monkeypatch.setattr(ps, "REVIEW_ROOT", other / "dogfood/p2-gate")
    assert brief() == in_b
    with pytest.raises(ValueError, match="pass repo"):
        brief(review_root=repo / "reviews" / "p2-gate")


@pytest.mark.parametrize("shape", ["reviews", "dogfood/other", "p2-gate",
                                   "dogfood/p2-gate/20260925T0000Z"])
def test_canonical_brief_refuses_unshaped_review_root(tmp_path, shape):
    """Catches a review root not shaped `<dir>/dogfood/p2-gate` being mapped to a tree
    by guesswork (the unchecked `review_root.parent.parent` of the refused candidate):
    without `repo` it raises ValueError. Control: the same root with `repo` given
    builds the brief."""
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="pass repo"):
        ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                           review_root=tmp_path / shape)
    ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                       review_root=tmp_path / shape, repo=repo)


# --------------------------------------------------------------------------- #
# K6 -- capsules preserved before the drift fields keep their digest
# --------------------------------------------------------------------------- #


GATE = ROOT / "dogfood/p2-gate"
RECORDED_DIGESTS = {"20260924T0030Z": "dcb933dec61b054f",
                    "20260924T130000Z": "8643917591af63b2"}


def _recorded_digest(stamp: str) -> str:
    """The `capsule_digest` the preserved review records of `stamp` carry: every
    `REVIEW*.json` (the current record and its rotated versions) whose `review_id` starts
    with `stamp-`, which must all say one digest."""
    found = set()
    for path in sorted(GATE.glob("REVIEW*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if str(record.get("review_id", "")).startswith(stamp + "-"):
            found.add(record["capsule_digest"])
    assert len(found) == 1, (stamp, sorted(found))
    return next(iter(found))


def test_legacy_capsule_digest_stable_on_the_preserved_reviews():
    """Catches the new Selection fields entering the digest of a capsule preserved
    before they existed, which would unbind every recorded review from its brief:
    `dogfood/p2-gate/20260924T134525Z/capsule-p2.json` digests to the `capsule_digest`
    its `REVIEW.json` records, and the two older ones to theirs; and every preserved
    capsule of the tree still loads (moved here from the NOT_RUN case, Revision 6 item
    2). Read-only. Control: the newest one with a drift field set no longer does.

    Re-stated after the v0.3.0 tag (strand ptests, O267 section 8(a)) because the review
    record rotated at the sealing commit: `REVIEW.json` is now the closure review of
    `20261013T093422Z` and the `20260924T134525Z` record is `REVIEW.v12.*`. The digest of
    each stamp is read from the record whose `review_id` starts with that stamp, so the
    pin follows the records and not the file name; the closure review's capsule carries
    the drift fields and the legacy ones do not, and each digests to what its record
    says. The preserved records do not move: this pins no live state."""
    preserved = sorted(GATE.glob("*/capsule-p2.json"))
    assert {p.parent.name for p in preserved} >= set(RECORDED_DIGESTS)
    for path in preserved:
        ps.ProgrammeCapsule.model_validate(json.loads(path.read_text(encoding="utf-8")))
    legacy = {**RECORDED_DIGESTS,
              "20260924T134525Z": _recorded_digest("20260924T134525Z")}
    want = {**legacy, "20261013T093422Z": _recorded_digest("20261013T093422Z")}
    for stamp, digest in want.items():
        path = GATE / stamp / "capsule-p2.json"
        assert path.is_file(), stamp
        body = json.loads(path.read_text(encoding="utf-8"))
        capsule = ps.ProgrammeCapsule.model_validate(body)
        assert capsule.selection.has_legacy_drift_fields() is (stamp in legacy), stamp
        assert ps.capsule_digest(capsule).startswith(digest), stamp
    # Control: the legacy capsule with a drift field set is no longer the recorded one.
    stamp = "20260924T134525Z"
    body = json.loads((GATE / stamp / "capsule-p2.json").read_text(encoding="utf-8"))
    body["selection"]["drift_reason"] = "not measured"
    assert not ps.capsule_digest(ps.ProgrammeCapsule.model_validate(body)).startswith(
        legacy[stamp])


def test_legacy_drift_fields_exempt_only_at_their_defaults(tmp_path):
    """Catches either half of the legacy exemption going wrong: a capsule whose four
    drift fields hold their defaults must validate (no reason required) and digest
    without them; one carrying source digests under NOT_RUN without a reason is
    refused. Control: a single field off its default puts all four into the digest."""
    body = json.loads(_capsule(_repo(tmp_path)).model_dump_json(by_alias=True))
    for key in ("source_digests", "drift", "drift_baseline", "drift_reason"):
        body["selection"].pop(key)
    legacy = ps.ProgrammeCapsule.model_validate(body)
    assert legacy.selection.drift == "NOT_RUN" and legacy.selection.drift_reason is None
    assert ps.capsule_digest(legacy) == ps._sha256(body)

    with pytest.raises(ValidationError):
        ps.Selection.model_validate({**body["selection"], "source_digests": {"X-01": EMPTY}})

    reasoned = ps.ProgrammeCapsule.model_validate(
        {**body, "selection": {**body["selection"], "drift_reason": "no preserved baseline"}})
    assert ps.capsule_digest(reasoned) != ps.capsule_digest(legacy)
    assert ps.capsule_digest(reasoned) == ps._sha256(
        reasoned.model_dump(mode="json", by_alias=True))


# --------------------------------------------------------------------------- #
# K7 -- the drift clause comes first, and is never cut
# --------------------------------------------------------------------------- #


def test_drift_clause_first_directly_after_the_label(tmp_path):
    """Catches the drift clause sitting after the coverage text, or before the
    `SELECTION:` label: both states read `SELECTION: drift <state>: ...; this capsule
    shows ...`. Control: with items left out and a limit that forces the left-out list
    to be cut, the clause is still whole and first while the list is shortened."""
    repo = _repo(tmp_path)
    baseline = _capsule(repo).selection.source_digests
    (repo / "src/x01.py").write_text("# x01, edited\n", encoding="utf-8")
    assert _capsule(repo).evidence_summaries[0].startswith(
        "SELECTION: drift NOT_RUN: no preserved baseline; this capsule shows all 2 ")
    assert _capsule(repo, baseline, "R1").evidence_summaries[0].startswith(
        "SELECTION: drift MEASURED: 1 of 2 item(s) changed against R1; this capsule shows ")

    ids = [f"X-{n:02d}" for n in range(1, 13)]
    line = _capsule(repo, register=_register(*ids), matrix=_matrix({i: [] for i in ids}),
                    max_items=2, max_summary_chars=200).evidence_summaries[0]
    assert len(line) <= 200
    assert line.startswith("SELECTION: drift NOT_RUN: no preserved baseline; this capsule "
                           "shows 2 of the 12 ")
    assert "more, full list in selection" in line or not line.endswith("X-12")


def test_summary_too_short_refused_rather_than_cut(tmp_path):
    """Catches a `max_summary_chars` too short for the drift clause cutting it -- the
    one statement a supervisor must not lose: `capsule_for` raises ValueError. Control:
    one character more builds, and the line is exactly the label and the clause."""
    repo = _repo(tmp_path)
    clause = "SELECTION: drift NOT_RUN: no preserved baseline"
    with pytest.raises(ValueError, match="refused rather than cut"):
        _capsule(repo, max_summary_chars=len(clause) - 1)
    assert _capsule(repo, max_summary_chars=len(clause)).evidence_summaries[0] == clause


# --------------------------------------------------------------------------- #
# K10 -- the baseline rules no case pinned (Revision 6 item 3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", ["inside", "outside"])
def test_symlinked_baseline_refused_as_a_newest_review_directory(tmp_path, target):
    """Catches a symlinked review directory counting as a candidate (mutant
    `baseline_symlink_dir_ok`) or being read through: the newest stamped name is a link
    -- to a stamped directory inside the root, or to a complete review outside it -- and
    the older real review stays the baseline, with nothing opened or listed through the
    link. With no real review beside it, the answer is `no preserved baseline`. Control:
    the same review as a real directory is the baseline."""
    repo = _repo(tmp_path / "repo")
    root = repo / "dogfood/p2-gate"
    capsule = _capsule(repo)
    held = (root / "held" if target == "inside" else tmp_path / "outside")
    _preserve(held, "20260927T0000Z", capsule)
    link = root / "20260927T0000Z"
    root.mkdir(parents=True, exist_ok=True)
    os.symlink(held / "20260927T0000Z", link)

    with _touched() as seen:
        alone = ps.drift_baseline_for(PHASE, root=root)
    assert alone == (None, None, "no preserved baseline")
    assert not _reached(seen, link) and not _reached(seen, held / "20260927T0000Z")

    _preserve(root, "20260925T0000Z", capsule)
    with _touched() as seen:
        older = ps.drift_baseline_for(PHASE, root=root)
    assert older == (capsule.selection.source_digests, "20260925T0000Z", None)
    assert not _reached(seen, link) and not _reached(seen, held / "20260927T0000Z")

    # Control: the link set aside (renamed, not removed), a real directory in its place.
    link.rename(root / "20260927T0000Z.v1.link")
    _preserve(root, "20260927T0000Z", capsule)
    assert ps.drift_baseline_for(PHASE, root=root)[1] == "20260927T0000Z"


def test_symlinked_baseline_refused_as_a_capsule_file_and_never_opened(tmp_path):
    """Catches a newest reviewed brief whose `capsule-<phase>.json` is a symlink being
    read through the link (mutant `baseline_symlink_capsule_read`): the link points at a
    perfectly valid capsule, so reading it would make it the baseline; instead drift is
    NOT_RUN with `baseline <id> is not a readable capsule`, no older review is
    consulted, and the target is never opened. Control: the same capsule as a regular
    file is the baseline."""
    repo = _repo(tmp_path / "repo")
    root = repo / "dogfood/p2-gate"
    capsule = _capsule(repo)
    _preserve(root, "20260925T0000Z", capsule)
    held = _preserve(tmp_path / "outside", "20260926T0000Z", capsule)
    newest = root / "20260926T0000Z"
    newest.mkdir(parents=True)
    (newest / "findings.json").write_text("[]", encoding="utf-8")
    target = held / f"capsule-{PHASE.lower()}.json"
    os.symlink(target, newest / f"capsule-{PHASE.lower()}.json")

    with _touched() as seen:
        got = ps.drift_baseline_for(PHASE, root=root)
    assert got == (None, None, "baseline 20260926T0000Z is not a readable capsule")
    assert not _reached(seen, target)
    assert not _reached(seen, root / "20260925T0000Z" / f"capsule-{PHASE.lower()}.json")

    (newest / f"capsule-{PHASE.lower()}.json").rename(newest / "capsule.v1.link")
    _preserve(root, "20260926T0000Z", capsule)
    assert ps.drift_baseline_for(PHASE, root=root) == (
        capsule.selection.source_digests, "20260926T0000Z", None)


def test_empty_baseline_refused_rather_than_measured(tmp_path):
    """Catches `{}` being accepted as a baseline (mutant `empty_baseline_measured`):
    every item is absent from it, so the capsule would say MEASURED with everything
    changed against a review that recorded nothing. `capsule_for` raises ValueError.
    Control: a baseline with one entry builds a MEASURED capsule."""
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="empty drift baseline"):
        _capsule(repo, {}, "X")
    digests = _capsule(repo).selection.source_digests
    measured = _capsule(repo, {"X-01": digests["X-01"]}, "X")
    assert measured.selection.drift == "MEASURED"
    assert set(measured.changed_source_digests) == {"X-02"}


# --------------------------------------------------------------------------- #
# K8 -- nothing outside the repository reaches a digest or a baseline
# --------------------------------------------------------------------------- #


def _outside_tree(tmp_path: Path) -> Path:
    """`repo/a -> ../outside`, `outside/evil -> OUTSIDE-LINK-TEXT`, a regular
    `outside/f.py`, and in-repo controls `repo/ok -> INSIDE-TEXT` and `repo/in/f.py`."""
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    os.symlink("../outside", repo / "a")
    os.symlink("OUTSIDE-LINK-TEXT", outside / "evil")
    _write(outside, "f.py", "outside\n")
    os.symlink("INSIDE-TEXT", repo / "ok")
    _write(repo, "in/f.py", "inside\n")
    return repo


def _sha_inputs(monkeypatch) -> list:
    """Every value `_sha256` digests from here on -- the digest inputs."""
    inputs: list = []
    real = ps._sha256

    def spy(value):
        inputs.append(value)
        return real(value)
    monkeypatch.setattr(ps, "_sha256", spy)
    return inputs


def test_outside_link_not_read_through_a_symlinked_directory(tmp_path, monkeypatch):
    """Catches `_file_digest` calling `os.readlink` before asking whether the path's
    directory lies inside the repository (supdrift-a B1, supdrift-b F2): `a/evil`, with
    `a -> ../outside`, must digest as `<missing>` -- through `_file_digest`, through
    `item_source_paths` with the evidence `test a/evil: x`, and through the
    `RELEVANT_PATHS` walk -- and no digest input may hold the foreign link text.
    Control: the in-repo link `ok` still gives `<symlink>INSIDE-TEXT`."""
    repo = _outside_tree(tmp_path)
    inputs = _sha_inputs(monkeypatch)
    assert ps._file_digest("a/evil", repo) == ps.MISSING_FILE
    assert ps._file_digest("ok", repo) == "<symlink>INSIDE-TEXT"

    row = {"evidence": ["test a/evil: x"]}
    assert ps.item_source_paths(row, repo) == ["a/evil"]
    assert ps.item_source_digest(row, repo) == ps._sha256([["a/evil", ps.MISSING_FILE]])
    assert ps.item_source_digest({"evidence": ["test ok: x"]}, repo) == ps._sha256(
        [["ok", "<symlink>INSIDE-TEXT"]])

    probe = {**DURABILITY_PROBE, "path": "rec.json"}
    monkeypatch.setattr(ps, "_relevant_paths", lambda: ("a/evil",))
    walked = ps.item_source_digest({"evidence": []}, repo, probe=probe)
    assert walked == ps._sha256([["a/evil", ps.MISSING_FILE], ["rec.json", ps.MISSING_FILE]])
    monkeypatch.setattr(ps, "_relevant_paths", lambda: ("ok",))
    assert ps.item_source_digest({"evidence": []}, repo, probe=probe) == ps._sha256(
        [["ok", "<symlink>INSIDE-TEXT"], ["rec.json", ps.MISSING_FILE]])

    # Through `capsule_for`, both ways at once: the item's evidence and its walk.
    monkeypatch.setattr(ps, "_relevant_paths", lambda: ("a/evil", "a"))
    register = _register()
    register["requirements"][0]["probe"] = probe
    capsule = _capsule(repo, register=register,
                       matrix=_matrix({"X-01": ["test a/evil: x"], "X-02": ["test ok: x"]}))
    assert "OUTSIDE-LINK-TEXT" not in capsule.model_dump_json()
    assert "OUTSIDE-LINK-TEXT" not in json.dumps(inputs, default=str)
    assert "INSIDE-TEXT" in json.dumps(inputs, default=str)


def test_outside_layers_each_relative_refuses_dotdot_and_absolute():
    """Catches `_relative` letting `..` or an absolute path through (mutant
    `relative_allows_dotdot`), the first layer: such a token is not a repository path.
    Control: a path that normalises inside the repository is kept."""
    assert ps._relative("../x") is None
    assert ps._relative("/x") is None
    assert ps._relative("a/../../x") is None
    assert ps._relative("src/x.py") == "src/x.py"
    assert ps._relative("a/../x") == "x"


def test_outside_layers_each_exists_in_refuses_a_file_through_an_outside_directory(tmp_path):
    """Catches `_exists_in` answering yes for a file -- or a link -- reached through a
    symlinked directory that leaves the repository (mutant `exists_no_inside_check`),
    which would let a prose token name a foreign file. Control: the same names inside
    the repository exist, and only those reach `evidence_paths`."""
    repo = _outside_tree(tmp_path)
    assert not ps._exists_in("a/f.py", repo)
    assert not ps._exists_in("a/evil", repo)
    assert ps._exists_in("in/f.py", repo)
    assert ps._exists_in("ok", repo)
    assert ps.evidence_paths(["see a/f.py and in/f.py"], repo) == ["in/f.py"]


def test_outside_layers_each_file_digest_refuses_a_regular_file_through_it(tmp_path):
    """Catches `_file_digest` reading a regular file reached through a symlinked
    directory outside the repository: it is `<missing>`, never read. Control: the same
    file name inside the repository is read. (With the parent check first, removing the
    later `target.is_relative_to(repo)` alone is an equivalent mutant, Revision 6a item
    4.)"""
    repo = _outside_tree(tmp_path)
    with _touched() as seen:
        assert ps._file_digest("a/f.py", repo) == ps.MISSING_FILE
    assert not _reached(seen, tmp_path / "outside")
    assert ps._file_digest("in/f.py", repo) == ps.hashlib.sha256(b"inside\n").hexdigest()


def test_review_root_outside_refused_and_its_capsule_never_opened(tmp_path):
    """Catches `drift_baseline_for` checking review directories only against the root,
    so a symlinked `dogfood/p2-gate` leading out of the repository makes it measure
    against a foreign capsule (sdspec-a r3 B1): `repo/dogfood/p2-gate ->
    ../../outside/gate`, holding a complete review, gives NOT_RUN with `review root
    outside the repository` -- directly and through `canonical_brief` -- and the foreign
    capsule is never opened. The same with an explicit `repo` the root lies outside of
    (Revision 6a item 3). Control: the same review under a real `repo/dogfood/p2-gate`
    is the baseline."""
    reason = (None, None, "review root outside the repository")
    repo = _repo(tmp_path / "repo")
    capsule = _capsule(repo)
    gate = tmp_path / "outside/gate"
    foreign = _preserve(gate, "20260925T0000Z", capsule) / f"capsule-{PHASE.lower()}.json"
    (repo / "dogfood").mkdir()
    os.symlink("../../outside/gate", repo / "dogfood/p2-gate")
    root = repo / "dogfood/p2-gate"

    with _touched() as seen:
        assert ps.drift_baseline_for(PHASE, root=root) == reason
        assert ps.drift_baseline_for(PHASE, root=root, repo=repo) == reason
        brief = ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                                   review_root=root)
    assert (brief.selection.drift, brief.selection.drift_reason) == (
        "NOT_RUN", "review root outside the repository")
    assert brief.changed_source_digests == {}
    assert not _reached(seen, foreign)

    # Explicit `repo`: a well-shaped root of another tree is outside this one.
    elsewhere = tmp_path / "elsewhere/dogfood/p2-gate"
    _preserve(elsewhere, "20260925T0000Z", capsule)
    with _touched() as seen:
        assert ps.drift_baseline_for(PHASE, root=elsewhere, repo=repo) == reason
        brief = ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                                   review_root=elsewhere, repo=repo)
    assert brief.selection.drift_reason == "review root outside the repository"
    assert not _reached(seen, elsewhere / "20260925T0000Z" / f"capsule-{PHASE.lower()}.json")
    # ... while the tree it belongs to measures against it.
    assert ps.drift_baseline_for(PHASE, root=elsewhere)[1] == "20260925T0000Z"

    # Control: the same review under a real review root of the repository.
    control = _repo(tmp_path / "control")
    _preserve(control / "dogfood/p2-gate", "20260925T0000Z", capsule)
    assert ps.drift_baseline_for(PHASE, root=control / "dogfood/p2-gate") == (
        capsule.selection.source_digests, "20260925T0000Z", None)
    assert ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                              review_root=control / "dogfood/p2-gate"
                              ).selection.drift_baseline == "20260925T0000Z"



# --------------------------------------------------------------------------- #
# K11 -- supervisor-followups: the tracked items of the supervisor-drift merge
# --------------------------------------------------------------------------- #

import hashlib  # noqa: E402 -- appended block; the pin's header stays byte-identical
import struct  # noqa: E402


CRAFTED_PHASES = ["x/../../../../../outside/y", "../PX", "PX/..", "P.2", "", " PX",
                  "PX\n", "P2/../../x"]


@pytest.mark.parametrize("crafted", CRAFTED_PHASES)
def test_phase_validated_before_the_review_root_is_read(tmp_path, monkeypatch, crafted):
    """Catches `drift_baseline_for` joining `capsule-{phase}.json` from an unchecked
    phase (supdrift2-a M1): with an in-repo `capsule-x/` directory, the phase
    `x/../../../../../outside/y` opened `outside/y.json` and returned its digests as
    the baseline. A phase that is not a name is refused with ValueError **before**
    the review root is listed, directly and through the non-canonical CLI (`main`
    looked the baseline up first), and nothing outside the repository is opened.
    Control: `PX` still reaches the review root and finds its baseline."""
    root = _cli_tree(tmp_path, monkeypatch)
    repo = tmp_path
    capsule = _capsule(repo)
    review = _preserve(root, "20260925T0000Z", capsule)
    (review / "capsule-x").mkdir()
    body = json.loads(capsule.model_dump_json(by_alias=True))
    body["selection"]["source_digests"] = {"X-01": "FOREIGN"}
    _write(tmp_path.parent / f"{tmp_path.name}-outside", "y.json", json.dumps(body))
    os.symlink(f"../{tmp_path.name}-outside", tmp_path / "outside")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    with _touched() as seen:
        with pytest.raises(ValueError, match="is not a phase name"):
            ps.drift_baseline_for(crafted, root=root, repo=repo)
        assert ps.main(["--phase", crafted]) == 1
    assert not _reached(seen, outside)
    assert not _reached(seen, root)

    with _touched() as seen:
        assert ps.drift_baseline_for(PHASE, root=root, repo=repo)[1] == "20260925T0000Z"
    assert _reached(seen, root)


SYMLINK_NAMES = ["build", "runs", "demo", "__pycache__", ".venv", ".pytest_cache",
                 ".ruff_cache", ".cache", "pkg.egg-info", "c4-probe"]


@pytest.mark.parametrize("name", SYMLINK_NAMES)
def test_symlink_named_like_an_ignored_directory_is_listed(tmp_path, name):
    """Catches `_walk` applying the directory patterns before the symlink test
    (supdrift2-a M2): git sees a symlink as a file, so `ls-files --others` lists a
    link named `build`, `runs` or `__pycache__` -- the directory-only patterns
    (`build/`, `runs/`, ...) do not match it -- and `stale_paths` sees it. The walk
    must list it as `<symlink>` plus its text, so retargeting it is drift. Control:
    the case below, where a file pattern (`*.bak`, `*.pyc`) does drop a link."""
    repo = _durability_repo(tmp_path / "repo")
    (repo / "real").mkdir()
    link = repo / "src/hoh" / name
    os.symlink("../../real", link)
    assert f"src/hoh/{name}" in ps.item_source_paths(RECORD_ROW, repo,
                                                     probe=DURABILITY_PROBE)
    assert ps._file_digest(f"src/hoh/{name}", repo) == "<symlink>../../real"
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    link.rename(repo / "src/hoh/set-aside.v1.bak")         # never removed
    os.symlink("../../real/other", link)
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) != before


@pytest.mark.parametrize("name", ["x.bak", "x.pyc", ".hoh-repair-x.md"])
def test_symlink_named_like_an_ignored_file_is_not_listed(tmp_path, name):
    """The control of the case above: the unanchored file patterns match a link as
    git matches it, so a link named `x.bak` is dropped like a file of that name, and
    adding it leaves the item unchanged."""
    repo = _durability_repo(tmp_path / "repo")
    (repo / "real").mkdir()
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    os.symlink("../../real", repo / "src/hoh" / name)
    assert f"src/hoh/{name}" not in ps.item_source_paths(RECORD_ROW, repo,
                                                         probe=DURABILITY_PROBE)
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == before


def test_reviewed_baseline_only_counts_a_newer_findings_only_directory_is_passed_over(
        tmp_path):
    """Catches the candidate predicate requiring only `findings.json` (supdrift2-a M3,
    mutant `baseline_needs_only_findings`): a newer stamped directory holding
    `findings.json` but no capsule is not a reviewed brief, so the older review stays
    the baseline -- the mutant made the newer one the latest candidate and gave NOT_RUN
    (`not a readable capsule`). Control: giving it its capsule makes it the baseline."""
    repo = _repo(tmp_path)
    root = repo / "dogfood/p2-gate"
    capsule = _capsule(repo)
    _preserve(root, "20260925T0000Z", capsule)
    newer = root / "20260926T0000Z"
    newer.mkdir(parents=True)
    (newer / "findings.json").write_text("[]", encoding="utf-8")
    assert ps.drift_baseline_for(PHASE, root=root) == (
        capsule.selection.source_digests, "20260925T0000Z", None)
    _preserve(root, "20260926T0000Z", capsule)
    assert ps.drift_baseline_for(PHASE, root=root)[1] == "20260926T0000Z"


def _varint(value: int) -> bytes:
    """git's offset varint (`varint.c`, `encode_varint`)."""
    out = [value & 0x7F]
    while value >> 7:
        value = (value >> 7) - 1
        out.append(0x80 | (value & 0x7F))
    return bytes(reversed(out))


def _index_bytes(paths: Iterable[str], *, version: int = 2, algo: str = "sha1",
                 extended: Iterable[str] = (), extensions: bytes = b"",
                 corrupt: bool = False, modes: dict[str, int] | None = None) -> bytes:
    """A git index file listing `paths`, written by hand -- this file starts no git
    (Revision 6a item 1). Stat fields are zero; `extended` entries carry the
    intent-to-add flag in version 3's extended flags; `modes` overrides an entry's mode
    (`0o040000` is a sparse directory entry)."""
    hash_len = hashlib.new(algo).digest_size
    extended, body, previous = set(extended), b"", b""
    names = sorted(paths)
    for path in names:
        name = path.encode("utf-8")
        flags = min(len(name), 0xFFF) | (0x4000 if path in extended else 0)
        mode = (modes or {}).get(path, 0o100644)
        entry = (struct.pack(">10I", 0, 0, 0, 0, 0, 0, mode, 0, 0, 0)
                 + b"\0" * hash_len + struct.pack(">H", flags))
        if path in extended:
            entry += struct.pack(">H", 0x2000)
        if version == 4:
            common = len(os.path.commonprefix([previous, name]))
            entry += _varint(len(previous) - common) + name[common:] + b"\0"
        else:
            entry += name + b"\0"
            entry += b"\0" * (-len(entry) % 8)
        body += entry
        previous = name
    data = b"DIRC" + struct.pack(">II", version, len(names)) + body + extensions
    digest = hashlib.new(algo, data).digest()
    if corrupt:
        digest = bytes([digest[0] ^ 1]) + digest[1:]
    return data + digest


def _write_index(repo: Path, paths: Iterable[str], **kw) -> Path:
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    index = repo / ".git/index"
    index.write_bytes(_index_bytes(paths, **kw))
    return index


FORCED = ["src/hoh/keep.bak", "src/hoh/build/t.py"]


def _forced_repo(tmp_path: Path) -> Path:
    """A durability repository with two files `.gitignore` matches but git tracks
    (`git add -f`): `src/hoh/keep.bak` and `src/hoh/build/t.py`."""
    repo = _durability_repo(tmp_path / "repo")
    for rel in FORCED:
        _write(repo, rel, "v1\n")
    return repo


def test_force_tracked_ignored_file_digested_from_the_index(tmp_path):
    """Catches T-PSD3: `stale_paths` sees a tracked file that `.gitignore` matches
    (`git diff` reports its edit), while the walk dropped it by name, so the edit moved
    the record's staleness and not drift -- the unsafe direction. With an index that
    tracks it, the file is in the item's set and editing it is drift; an untracked
    ignored file beside it still is not. Control: the same repository without an index
    (renamed away) keeps today's walk, and the edit moves nothing."""
    repo = _forced_repo(tmp_path)
    _write(repo, "src/hohx.py")                 # a sibling of `src/hoh`, not under it
    tracked = ["src/hoh/named.py", "src/hoh/unnamed.py", "src/hoh/sub/deep.py", *FORCED,
               "src/hohx.py"]
    index = _write_index(repo, tracked)
    paths = ps.item_source_paths(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    assert set(FORCED) <= set(paths)
    assert "src/hohx.py" not in paths           # under the entry means `entry/`
    before = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    _write(repo, "src/hoh/other.bak", "untracked\n")
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == before
    for rel in FORCED:
        _write(repo, rel, "v2\n")
        moved = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
        assert moved != before
        before = moved

    index.rename(repo / ".git/index.v1.set-aside")
    walk_only = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    assert not set(FORCED) & set(ps.item_source_paths(RECORD_ROW, repo,
                                                      probe=DURABILITY_PROBE))
    _write(repo, FORCED[0], "v3\n")
    assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == walk_only


@pytest.mark.parametrize("case", ["bad_checksum", "split_index", "sparse_index",
                                  "git_file", "git_symlink_outside", "index_symlink",
                                  "not_an_index", "unknown_version", "sparse_entry",
                                  "trailing_bytes", "v2_extended_flag", "too_large"])
def test_index_unreadable_falls_back_to_the_walk(tmp_path, monkeypatch, case):
    """Catches an index trusted when it cannot be read exactly, or read from outside
    the repository: a wrong checksum, a split index (`link`) or a sparse one (`sdir`)
    whose entries are not the whole tracked set, a `.git` *file* (a linked worktree,
    whose index lies elsewhere), a `.git` or `index` that is a symlink, a file that is
    not an index, an unknown version (5), a sparse directory entry, bytes left over
    before the checksum, an extended flag in a version 2 index, or an index above the
    size cap -- each gives no tracked set, and the digest is the walk's alone.
    The foreign index behind a symlinked `.git` is never opened. Control: the same
    entries in a sound index add the force-tracked files (the case above)."""
    repo = _forced_repo(tmp_path)
    tracked = ["src/hoh/named.py", *FORCED]
    no_index = ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE)
    elsewhere = tmp_path / "elsewhere"
    if case == "bad_checksum":
        _write_index(repo, tracked, corrupt=True)
    elif case == "split_index":
        _write_index(repo, tracked, extensions=b"link" + struct.pack(">I", 20) + b"\0" * 20)
    elif case == "sparse_index":
        _write_index(repo, tracked, extensions=b"sdir" + struct.pack(">I", 0))
    elif case == "git_file":
        _write(repo, ".git", "gitdir: ../elsewhere\n")
        _write_index(elsewhere, tracked)
    elif case == "git_symlink_outside":
        _write_index(elsewhere, tracked)
        os.symlink("../elsewhere/.git", repo / ".git")
    elif case == "index_symlink":
        _write_index(elsewhere, tracked)
        (repo / ".git").mkdir()
        os.symlink("../../elsewhere/.git/index", repo / ".git/index")
    elif case == "unknown_version":
        _write_index(repo, tracked, version=5)
    elif case == "sparse_entry":
        _write_index(repo, tracked, modes={"src/hoh/named.py": 0o040000})
    elif case == "trailing_bytes":
        _write_index(repo, tracked, extensions=b"\0\0\0")
    elif case == "v2_extended_flag":
        _write_index(repo, tracked, version=2, extended={"src/hoh/named.py"})
    elif case == "too_large":
        _write_index(repo, tracked)
        monkeypatch.setattr(ps, "_INDEX_MAX_BYTES", 64)
    else:
        _write(repo, ".git/index", "not an index\n")
    with _touched() as seen:
        assert ps._tracked_paths(repo) is None
        assert ps.item_source_digest(RECORD_ROW, repo, probe=DURABILITY_PROBE) == no_index
    assert not _reached(seen, elsewhere)


@pytest.mark.parametrize("version, algo", [(2, "sha1"), (3, "sha1"), (4, "sha1"),
                                           (2, "sha256"), (4, "sha256")])
def test_index_versions_parsed_exactly(tmp_path, version, algo):
    """Catches a reader that gets an index format wrong -- version 2/3 padding, version
    3's extended flags, version 4's prefix compression with a strip longer than one
    varint byte, a SHA-256 repository's longer object ids -- by parsing a hand-written
    index of each kind back to exactly its paths. Control: the extension area after the
    entries (a `TREE` cache here) is skipped, not read as an entry."""
    deep = "src/hoh/" + "d" * 150 + ".py"
    paths = {"a.py", "src/hoh/x.py", "src/hoh/build/t.py", "src/hoh/keep.bak", deep,
             "src/hoh/e.py", "z/ü.py"}
    extended = {"z/ü.py"} if version >= 3 else set()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_index(repo, paths, version=version, algo=algo, extended=extended,
                 extensions=b"TREE" + struct.pack(">I", 3) + b"abc")
    assert ps._tracked_paths(repo) == frozenset(paths)


def _gitignore_patterns(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_gitignore_copy_matches_the_real_gitignore():
    """Catches T-PSD3's first half: the walk's ignore list is a copy of `.gitignore`,
    and a later rule that can match under `RELEVANT_PATHS` would leave the two
    disagreeing silently. Reads this tree's `.gitignore` (not a preserved review, so
    rule 7 does not apply) and requires: every unanchored pattern is in the copy, as a
    directory pattern when it ends in `/` and a file pattern otherwise; every anchored
    pattern lies outside `RELEVANT_PATHS`; and the copy holds nothing `.gitignore` does
    not. Control: the copy's own sizes, so an emptied copy cannot pass vacuously."""
    patterns = _gitignore_patterns((ROOT / ".gitignore").read_text(encoding="utf-8"))
    relevant = ps._relevant_paths()
    dirs = set(ps.IGNORED_DIRS) | set(ps.IGNORED_DIR_PATTERNS)
    files = set(ps.IGNORED_FILE_PATTERNS)
    seen_dirs, seen_files = set(), set()
    for raw in patterns:
        pattern = raw.lstrip("!")
        body = pattern.rstrip("/")
        if "/" in body:                              # anchored: a path, not a name
            assert not any(body.startswith(r.rstrip("/") + "/") or body == r or
                           r.startswith(body.split("*")[0]) for r in relevant), raw
            continue
        if pattern.endswith("/"):
            assert body in dirs, raw
            seen_dirs.add(body)
        else:
            assert body in files, raw
            seen_files.add(body)
    assert seen_dirs == dirs and seen_files == files
    assert len(dirs) == 10 and len(files) == 3


def test_preserved_reviews_refuse_a_review_root_outside_the_repository(tmp_path):
    """Catches T-PSD4 in the tool: `preserved_reviews` checked each directory only
    against `root.resolve()`, so a symlinked `dogfood/p2-gate` leading out of the
    repository carried a foreign review's open finding into the canonical brief --
    inside the digest the gate compares. An outside root holds no review of this tree:
    `preserved_reviews` gives `[]`, the brief carries no foreign finding, and the
    foreign `findings.json` is never opened; the same with an explicit `repo` the root
    lies outside of (`canonical_brief` passes its `repo` on). Control: the same review
    under a real `dogfood/p2-gate` is carried."""
    finding = {"finding_id": "F-FOREIGN", "review_id": "R", "kind": "PROPOSE_REOPEN",
               "severity": "critical", "item_ids": ["X-01"], "reason": "a foreign reason",
               "proposed_action": "a foreign action",
               "supervisor_provider": "independent-supervisor"}
    repo = _repo(tmp_path / "repo")
    gate = tmp_path / "outside/gate"
    held = _preserve(gate, "20260925T0000Z", _capsule(repo))
    (held / "findings.json").write_text(json.dumps([finding]), encoding="utf-8")
    (repo / "dogfood").mkdir()
    os.symlink("../../outside/gate", repo / "dogfood/p2-gate")
    root = repo / "dogfood/p2-gate"
    with _touched() as seen:
        assert ps.preserved_reviews(root) == []
        assert ps.preserved_reviews(root, repo=repo) == []
        brief = ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                                   review_root=root)
        assert brief.open_findings == []
    assert not _reached(seen, held / "findings.json")

    elsewhere = tmp_path / "elsewhere/dogfood/p2-gate"
    foreign = _preserve(elsewhere, "20260925T0000Z", _capsule(repo))
    (foreign / "findings.json").write_text(json.dumps([finding]), encoding="utf-8")
    with _touched() as seen:
        explicit = ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                                      review_root=elsewhere, repo=repo)
    assert explicit.open_findings == []
    assert not _reached(seen, foreign / "findings.json")
    # ... while the tree that root belongs to carries it.
    assert [n for n, _ in ps.preserved_reviews(elsewhere)] == ["20260925T0000Z"]

    control = _repo(tmp_path / "control")
    real = _preserve(control / "dogfood/p2-gate", "20260925T0000Z", _capsule(control))
    (real / "findings.json").write_text(json.dumps([finding]), encoding="utf-8")
    carried = ps.canonical_brief(register=REGISTER, matrix=MATRIX, phase=PHASE,
                                 review_root=control / "dogfood/p2-gate")
    assert [f.finding_id for f in carried.open_findings] == ["F-FOREIGN"]
