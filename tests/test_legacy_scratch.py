"""STORAGE-L: `legacy_scratch.survey` / `adopt` and the CLI
(`hoh storage survey-legacy`, `hoh storage adopt-legacy`) -- revision 3, K8.

Everything under `tmp_path` (`World` of `test_storage_registry_legacy`).
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from hoh import cli, legacy_scratch as ls
from hoh import storage_registry as sr
from hoh import storage_tiers as st
from test_storage_registry import fake, git, isolated_env  # noqa: F401  (fixtures)
from test_storage_registry_legacy import World, archive_into, commit_file
from test_verified_relocation import _removal_functions

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(tmp_path, fake) -> World:
    return World(tmp_path, fake)


def snapshot(base: Path) -> list[tuple]:
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        for name in sorted(dirnames + filenames):
            full = Path(dirpath) / name
            s = os.lstat(full)
            out.append((full.relative_to(base).as_posix(), s.st_size, s.st_mtime_ns, s.st_ino))
    return sorted(out)


def designate(world: World) -> None:
    world.brief()
    commit_file(world.repo, "dogfood/x/r1-report.md", f"Reviewed in {world.root}/tree\n")


def test_survey_is_read_only(world):
    """Catches: the survey writing into the reviews tree. Mutant A1: a marker
    written beside each surveyed root."""
    designate(world)
    before = snapshot(world.reviews.parent)
    record = ls.survey(world.reviews, registry=world.reg, project_id="a")
    assert snapshot(world.reviews.parent) == before
    assert [r["path"] for r in record["roots"]] == [str(world.root)]


def test_survey_uses_committed_blobs_only(world):
    """Catches: a local (uncommitted) brief taken as a designation. Mutant A2:
    the survey greps the working tree (`--untracked`)."""
    text = f"Scratch: {world.root}/\n"
    (world.reviews / "r1.md").write_text(text, encoding="utf-8")
    (world.repo / "dogfood" / "x").mkdir(parents=True)
    (world.repo / "dogfood" / "x" / "r1.md").write_text(text, encoding="utf-8")
    commit_file(world.repo, "dogfood/x/r1-report.md", f"Reviewed in {world.root}/tree\n")
    record = ls.survey(world.reviews, registry=world.reg, project_id="a")
    assert record["roots"] == []
    assert "no committed designation names it" in record["refused"][0]["reasons"]


def test_survey_draft_adopts_end_to_end(world):
    """Catches: a survey draft that the registry refuses (binding forms drift
    apart). Mutant A3: the survey's child digest taken over the root, not
    per child."""
    designate(world)
    record = ls.survey(world.reviews, registry=world.reg, project_id="a")
    cited = commit_file(world.repo, "dogfood/storage-anchors/legacy.json", json.dumps(record))
    outcomes = ls.adopt({"commit": cited["commit"], "file": cited["file"]},
                        registry=world.reg, project_id="a")
    assert [o["status"] for o in outcomes] == ["REGISTERED"]
    assert outcomes[0]["entry"]["bytes_allocated"] > 0
    assert world.reg.owner_of(world.root / "tree" / "README").status == sr.OWNED
    (world.tmp / "proc").mkdir()
    decision = st.classify_tier(world.root / "tree", registry=world.reg, sources=st.EvidenceSources(
        world.reg, "a", proc_root=str(world.tmp / "proc")))
    assert decision.refused is None, decision.reasons     # the survey's binding is the tier's


def test_adopt_reports_each_refusal(world):
    """Catches: a refused root swallowed or stopping the others. Mutant A4:
    a refusal skipped silently."""
    designate(world)
    other = world.reviews / "r2"
    archive_into(world.repo, world.old, other / "tree")
    commit_file(world.repo, "dogfood/x/r2.md", f"Scratch: {other}/\n")
    commit_file(world.repo, "dogfood/x/r2-report.md", f"Reviewed in {other}/tree\n")
    record = ls.survey(world.reviews, registry=world.reg, project_id="a")
    assert len(record["roots"]) == 2
    (other / "tree" / "late.txt").write_text("after the survey\n", encoding="utf-8")
    cited = commit_file(world.repo, "dogfood/storage-anchors/legacy.json", json.dumps(record))
    outcomes = ls.adopt({"commit": cited["commit"], "file": cited["file"]},
                        registry=world.reg, project_id="a")
    assert [(Path(o["path"]).name, o["status"]) for o in outcomes] == [
        ("r1", "REGISTERED"), ("r2", "REFUSED")]
    assert "children differ" in outcomes[1]["reason"]


def test_survey_out_never_replaced_and_durable(world, monkeypatch):
    """Catches: an existing --out replaced, a file that appears between the
    check and the publish replaced (the `lexists` + `os.replace` race), or the
    file written outside `durable_io`. Mutants A5 (`durable_write`, which
    replaces) and A6 (`write_text`)."""
    import hoh.durable_io as dio

    designate(world)
    record = ls.survey(world.reviews, registry=world.reg, project_id="a")
    calls = []
    real = ls.durable_create
    monkeypatch.setattr(ls, "durable_create",
                        lambda p, d, **k: (calls.append(p), real(p, d, **k))[1])
    out = world.tmp / "survey.json"
    ls.write_survey(out, record)
    assert calls == [out] and json.loads(out.read_text())["roots"]
    with pytest.raises(FileExistsError):
        ls.write_survey(out, {"roots": []})
    assert json.loads(out.read_text())["roots"]
    raced = world.tmp / "raced.json"
    original = dio._precheck

    def racer(path, needed, when):
        raced.write_text("someone else's file\n", encoding="utf-8")
        return original(path, needed, when)

    monkeypatch.setattr(dio, "_precheck", racer)
    with pytest.raises(FileExistsError):
        ls.write_survey(raced, record)
    assert raced.read_text() == "someone else's file\n"


def test_no_override_parameters():
    """Catches: a `force`/`override`/`skip` keyword on the adoption path.
    Mutant A7: a `force=False` parameter added to `register_legacy_root`."""
    for fn in (sr.StorageRegistry.register_legacy_root, ls.adopt, ls.survey,
               st.git_tree_proof):
        names = set(inspect.signature(fn).parameters)
        assert not names & {"force", "override", "skip", "unsafe", "trust"}, fn


def test_removal_only_in_execute_covers_new_module():
    """Catches: a removal call in the new module. Mutant A8: an `unlink` in
    `adopt`. Negative control: the scanner flags a probe."""
    source = (ROOT / "src" / "hoh" / "legacy_scratch.py").read_text(encoding="utf-8")
    removers, _, loose = _removal_functions(source)
    assert removers == {} and loose == []
    assert _removal_functions("import os\ndef adopt(p):\n    os.unlink(p)\n")[0] == {
        "adopt": {"unlink"}}


def test_cli_survey_and_adopt_legacy(world, capsys):
    """Catches: the CLI reporting success while a root is refused, or accepting
    a malformed anchor. Mutant A9: `adopt-legacy` exits 0 on a refusal."""
    designate(world)
    state = str(world.reg.state_root)
    out = world.tmp / "draft.json"
    assert cli.main(["storage", "survey-legacy", "--project", "a", "--reviews",
                     str(world.reviews), "--out", str(out), "--storage-root", state]) == 0
    assert cli.main(["storage", "adopt-legacy", "--project", "a", "--anchor", "nothex:x",
                     "--storage-root", state]) == 2
    record = json.loads(out.read_text())
    record["roots"][0]["children"]["tree"]["entries_digest"] = "0" * 64
    bad = commit_file(world.repo, "dogfood/storage-anchors/bad.json", json.dumps(record))
    assert cli.main(["storage", "adopt-legacy", "--project", "a", "--anchor",
                     f"{bad['commit']}:{bad['file']}", "--storage-root", state]) == 2
    good = commit_file(world.repo, "dogfood/storage-anchors/good.json", out.read_text())
    assert cli.main(["storage", "adopt-legacy", "--project", "a", "--anchor",
                     f"{good['commit']}:{good['file']}", "--storage-root", state]) == 0
