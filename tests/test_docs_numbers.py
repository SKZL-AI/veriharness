"""`tools/docs_numbers.py`: the second paper's figures are derived, rendered and
checked, never typed.

Every test builds its own small repository or ledger in `tmp_path`; none reads
the real ledger, the real history or the real table, so none depends on what
the project looks like today. The tool's own claim is narrow and so are these
tests: a figure that a tool can derive is derived; a historical figure must
still be in its ledger entry; a tag must name a row and its figure must be in
the text that carries it; a refusal list entry must be recorded as a refusal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import docs_numbers as dn  # noqa: E402


# --------------------------------------------------------------------------
# the ledger side
# --------------------------------------------------------------------------

LEDGER = """\
# Ledger

### O1 -- first entry

The figure 1,234 is here.

## A sub-heading of the first entry

The sub-heading must not end the entry; 5,678 is below it.

### O2 — second entry (em dash)

Only the second entry says 9,999.

## P2-26 -- a capability entry

iteration 2   K1..K8 PASS, accepted

### O3 -- third entry

Something else.
"""


def lines():
    return LEDGER.split("\n")


def test_an_entry_runs_to_the_next_entry_heading_not_the_next_heading():
    span = dn._entry_section(lines(), "O1")
    assert span is not None
    body = "\n".join(lines()[span[0]:span[1]])
    assert "5,678" in body and "9,999" not in body


def test_both_dash_forms_and_the_capability_heading_are_found():
    assert dn._entry_section(lines(), "O2") is not None
    assert dn._entry_section(lines(), "P2-26") is not None
    assert dn._entry_section(lines(), "O9") is None


def test_a_needle_counts_only_inside_its_own_entry():
    assert dn.ledger_find(lines(), "O1", "1,234") is not None
    assert dn.ledger_find(lines(), "O1", "9,999") is None
    assert dn.ledger_find(lines(), "O2", "9,999") is not None


def test_a_row_whose_ledger_text_is_missing_is_named():
    rows = [dict(tag="V99", ledger=[("O1", "1,234"), ("O1", "absent text")],
                 needles=[], figure="x", r=False)]
    problems = dn.check_ledger_rows(rows, lines())
    assert len(problems) == 1
    assert "V99" in problems[0] and "absent text" in problems[0]


def test_the_refusal_list_is_verified_against_the_entries(monkeypatch):
    ledger = ["### O1 -- entry", "alpha was refused at integration",
              "beta was accepted and merged", "### O2 -- entry",
              "gamma: release no by one reviewer"]
    monkeypatch.setattr(dn, "REFUSED", (("alpha", "O1"), ("gamma", "O2")))
    assert dn.check_refusals(ledger) == []
    monkeypatch.setattr(dn, "REFUSED", (("beta", "O1"),))
    problems = dn.check_refusals(ledger)
    assert problems and "beta" in problems[0]
    monkeypatch.setattr(dn, "REFUSED", (("alpha", "O2"),))
    assert dn.check_refusals(ledger)
    monkeypatch.setattr(dn, "REFUSED", (("alpha", "O1"), ("alpha", "O1")))
    assert any("twice" in p for p in dn.check_refusals(ledger))


def test_a_name_inside_a_longer_name_is_not_the_candidate(monkeypatch):
    ledger = ["### O1 -- entry", "sstew2 was refused at integration"]
    monkeypatch.setattr(dn, "REFUSED", (("sstew", "O1"),))
    assert dn.check_refusals(ledger)


# --------------------------------------------------------------------------
# the tag side
# --------------------------------------------------------------------------

def rows_for_tags():
    return [dict(tag="V01", needles=["42"], figure="42", ledger=[], r=True),
            dict(tag="V02", needles=["30"], figure="30", ledger=[], r=True),
            dict(tag="V03", needles=[], figure="x", ledger=[], r=False,
                 expects=("all MISSING",
                          lambda fx: fx["status"] == "MISSING"))]


def write_docs(root: Path, readme: str, extra: str = ""):
    (root / "README.md").write_text(readme, encoding="utf-8")
    if extra:
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs/LIMITATIONS.md").write_text(extra, encoding="utf-8")


def test_a_correct_tag_passes_and_every_row_must_be_used(tmp_path):
    write_docs(tmp_path, "There were 42 merges `[V01]` and 30 of them `[V02]`.\n"
                         "Reads `[V03]`.\n")
    assert dn.check_tags(tmp_path, rows_for_tags(), {"status": "x"}) == []


def test_a_tag_without_a_row_is_reported(tmp_path):
    write_docs(tmp_path, "42 `[V01]` 30 `[V02]` `[V03]` and 7 `[V77]`.\n")
    problems = dn.check_tags(tmp_path, rows_for_tags(), {"status": "x"})
    assert any("V77" in p and "names no row" in p for p in problems)


def test_a_stale_figure_in_the_text_is_reported(tmp_path):
    write_docs(tmp_path, "There were 41 merges `[V01]`; 30 `[V02]`; `[V03]`.\n")
    problems = dn.check_tags(tmp_path, rows_for_tags(), {"status": "x"})
    assert any("V01" in p and "42" in p for p in problems)


def test_the_figure_may_sit_in_another_paragraph_of_the_same_document(tmp_path):
    write_docs(tmp_path, "42 merges `[V01]`, 30 `[V02]`.\n\nLater: `[V01]` again, "
                         "no figure.\n\n`[V03]`\n")
    assert dn.check_tags(tmp_path, rows_for_tags(), {"status": "x"}) == []


def test_an_unused_row_is_reported(tmp_path):
    write_docs(tmp_path, "42 `[V01]` and 30 `[V02]`.\n")
    problems = dn.check_tags(tmp_path, rows_for_tags(), {"status": "x"})
    assert any("V03" in p and "used by no document" in p for p in problems)


def test_a_phrase_the_derived_figure_contradicts_is_reported(tmp_path):
    write_docs(tmp_path, "42 `[V01]`, 30 `[V02]`; all MISSING `[V03]`.\n")
    ok = dn.check_tags(tmp_path, rows_for_tags(), {"status": "MISSING"})
    assert ok == []
    bad = dn.check_tags(tmp_path, rows_for_tags(), {"status": "PROVEN"})
    assert any("all MISSING" in p for p in bad)


def test_only_the_prerelease_section_of_the_notes_is_read_for_tags(tmp_path):
    (tmp_path / "RELEASE_NOTES.md").write_text(
        "# Release notes -- v1.0.0a1 (pre-release)\n\n42 `[V01]` 30 `[V02]` "
        "`[V03]`\n\n---\n\n# Release notes -- v0.1.0\n\nold text `[V99]`\n",
        encoding="utf-8")
    assert dn.check_tags(tmp_path, rows_for_tags(), {"status": "x"}) == []


# --------------------------------------------------------------------------
# the generated audit block
# --------------------------------------------------------------------------

def test_the_audit_block_is_replaced_in_place_and_the_rest_is_kept():
    rows = [dict(tag="V01", r=True, ledger=[]), dict(tag="V02", r=False,
            ledger=[("O1", "x")])]
    verdicts = {"V01": ("RESOLVED", "m1"), "V02": ("MISMATCH", "m2")}
    block = dn.audit_block(rows, verdicts)
    doc = "before\n\n" + dn.AUDIT_BEGIN + "\nold\n" + dn.AUDIT_END + "\n\nafter\n"
    new = dn.splice_audit(doc, block)
    assert new.startswith("before") and new.endswith("after\n")
    assert "| V02 | ledger text | MISMATCH | m2 |" in new
    assert "old" not in new
    assert dn.splice_audit(new, block) == new


def test_a_row_named_in_a_problem_reads_mismatch_not_resolved():
    rows = [dict(tag="V01", r=True, ledger=[]), dict(tag="V02", r=False,
            ledger=[("O1", "x")])]
    out = dn.row_verdicts(rows, ["V02: ledger entry O1 does not contain 'x'"],
                          True, True)
    assert out["V01"][0] == "RESOLVED" and out["V02"][0] == "MISMATCH"
    out = dn.row_verdicts(rows, [], False, False)
    assert out["V01"][0] == "NOT_RUN" and out["V02"][0] == "NOT_RUN"


# --------------------------------------------------------------------------
# the git side, in a repository of the test's own
# --------------------------------------------------------------------------

def git(repo: Path, *args: str, when: str | None = None) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    p = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True,
                       text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(repo: Path, subject: str, when: str, files: dict | None = None):
    for rel, text in (files or {"note.txt": subject}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", subject, when=when)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "root", "2026-09-10T10:00:00+0300")
    return r


def test_merges_are_counted_by_title_and_src_changes_by_diff(repo):
    # a product-run merge that touches src/hoh, one that does not, one outside
    # the window, and a differently titled merge
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, "feature a", "2026-09-26T10:00:00+0300",
           {"src/hoh/a.py": "x = 1\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "feat", "-m",
        "Merge VeriHarness run r1 (touches src)",
        )
    git(repo, "checkout", "-q", "-b", "docs")
    commit(repo, "docs only", "2026-09-27T10:00:00+0300", {"README.md": "r\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "docs", "-m",
        "Merge VeriHarness run r2 (docs only)")
    git(repo, "checkout", "-q", "-b", "other")
    commit(repo, "other", "2026-09-28T10:00:00+0300", {"o.txt": "o\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "other", "-m", "Merge something else")
    base = git(repo, "rev-parse", "HEAD")
    facts = dn.derive_git(repo, base)
    assert facts["merges"] == 2
    assert facts["merges_src"] == 1
    assert facts["candidates_imported"] == 0


def test_the_product_run_merges_with_another_title_are_counted(repo):
    """`caa732db` ("Merge role-pane-lifecycle"), `a7d73b8e` ("Merge p217o"),
    `b7cf790b` ("Merge rbtf") and `a4e8fbad` ("Merge p243b + ...") are
    product-run merges whose title does not begin "Merge VeriHarness run"; all
    four are counted, and a merge with an unrelated title is not."""
    for i, title in enumerate(("Merge role-pane-lifecycle (rpl)",
                               "Merge p217o: P2-17 owned contracts",
                               "Merge rbtf: role scratch follow-up",
                               "Merge p243b + p243bf + p243bf2: slice b",
                               "Merge p2170 lookalike",
                               "Merge rbt-lookalike")):
        git(repo, "checkout", "-q", "-b", f"f{i}")
        commit(repo, f"feature {i}", f"2026-10-0{i + 1}T10:00:00+0300",
               {f"src/hoh/m{i}.py": "x = 1\n"})
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "--no-ff", "-q", f"f{i}", "-m", title)
    base = git(repo, "rev-parse", "HEAD")
    facts = dn.derive_git(repo, base)
    assert facts["merges"] == 4 and facts["merges_src"] == 4


def test_the_candidate_phrase_is_counted_only_inside_its_window(repo):
    commit(repo, "import candidate as the product wrote it (x)",
           "2026-09-24T10:00:00+0300")
    commit(repo, "import candidate as the product wrote it (y)",
           "2026-09-30T10:00:00+0300")
    base = git(repo, "rev-parse", "HEAD")
    assert dn.derive_git(repo, base)["candidates_imported"] == 1


def test_an_unknown_base_is_an_environment_gap_not_a_pass(repo):
    (repo / dn.NUMBERS).parent.mkdir(parents=True, exist_ok=True)
    (repo / dn.NUMBERS).write_text("Base commit: `" + "0" * 40 + "`\n",
                                   encoding="utf-8")
    code, lines_, _ = dn.run_check(repo)
    assert code == 3 and "environment gap" in lines_[0]


def test_a_table_without_a_base_fails(repo):
    code, lines_, _ = dn.run_check(repo)
    assert code == 1 and "names no base" in lines_[0]


def test_the_integrity_gate_needs_the_merge_to_be_an_ancestor(repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(dn, "INTEGRITY_GATE_COMMIT", base[:8])
    assert dn.gate_check(repo, base) == []
    monkeypatch.setattr(dn, "INTEGRITY_GATE_COMMIT", "deadbeef")
    assert dn.gate_check(repo, base)


def test_a_working_tree_that_moved_in_the_derivation_inputs_is_named(repo):
    base = git(repo, "rev-parse", "HEAD")
    assert dn.moved_inputs(repo, base) == []
    (repo / "src/hoh").mkdir(parents=True)
    (repo / "src/hoh/new.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    assert dn.moved_inputs(repo, base) == ["src/hoh/new.py"]


def test_the_repositorys_own_table_names_a_base_in_its_history():
    """The committed table is read, not re-derived: that is `check`'s job and
    needs the ledger. Here only that the file names a 40-hex base."""
    text = (ROOT / dn.NUMBERS).read_text(encoding="utf-8")
    assert dn.BASE_RE.search(text)


def test_a_merge_that_names_a_strand_the_documents_call_unmerged_is_reported(
        repo, monkeypatch):
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, "feature", "2026-10-01T10:00:00+0300")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "feat", "-m",
        "Merge VeriHarness run r9 (failure-classes: quota is never a verdict)")
    base = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(dn, "UNMERGED_STRANDS", ("failure-classes",))
    problems = dn.unmerged_check(repo, base)
    assert problems and "failure-classes" in problems[0]
    monkeypatch.setattr(dn, "UNMERGED_STRANDS", ("role-pane",))
    assert dn.unmerged_check(repo, base) == []


def test_the_name_in_a_commit_that_is_not_a_product_run_merge_is_not_a_merge(
        repo, monkeypatch):
    commit(repo, "Draft the failure-classes specification",
           "2026-10-01T10:00:00+0300")
    base = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(dn, "UNMERGED_STRANDS", ("failure-classes",))
    assert dn.unmerged_check(repo, base) == []


def test_a_skipped_row_is_blanked_in_both_texts_before_they_are_compared():
    a = "| V12 | 17 |\n| V13 | TECHNICALLY_STABLE_READY = no | x |\n"
    b = "| V12 | 17 |\n| V13 | TECHNICALLY_STABLE_READY = yes | y |\n"
    assert dn._blank(a, {"V13"}) == dn._blank(b, {"V13"})
    assert dn._blank(a, set()) != dn._blank(b, set())
    assert dn._blank(a, {"V12"}) != dn._blank(b, {"V12"})

