"""`tools/changelog_from_release_notes.py`: the changelog section of a release is
generated from the release notes, so a release is described in one place only.

The fixtures are small notes and changelogs in `tmp_path`. The last test also
reads the repository's own two files, because the property the tool exists for
-- the committed `CHANGELOG.md` is what the committed notes produce -- is a
property of those two files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import changelog_from_release_notes as cg  # noqa: E402

NOTES = """\
# Release notes -- v1.2.0a1 (pre-release)

**This is a pre-release.** Second paragraph of the intro follows.

More intro.

## Added

* one thing
* another thing

## Changed

* a change

## Check this release yourself

* run a command

## Known limitations at this pre-release

* a limit

---

# Release notes -- v1.0.0

Old notes that must not be read.

## Added

* ancient
"""

CHANGELOG = """\
# Changelog

Intro of the changelog.

## [1.0.0] - 2026-09-14

First stable.
"""


def make(tmp_path: Path, notes: str = NOTES, changelog: str = CHANGELOG) -> Path:
    (tmp_path / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def test_the_newest_section_is_read_and_only_it():
    notes = cg.parse_notes(NOTES)
    assert notes["version"] == "1.2.0a1" and notes["prerelease"] is True
    assert [t for t, _ in notes["sections"]] == [
        "Added", "Changed", "Check this release yourself",
        "Known limitations at this pre-release"]
    assert "Old notes" not in notes["intro"]
    assert notes["intro"].startswith("**This is a pre-release.**")


def test_only_the_named_sections_are_generated_in_order():
    section = cg.generate_section(cg.parse_notes(NOTES), "unreleased")
    assert section.startswith("<!-- BEGIN GENERATED:")
    assert "## [1.2.0a1] - unreleased (pre-release)" in section
    titles = [x for x in section.splitlines() if x.startswith("### ")]
    assert titles == ["### Added", "### Changed",
                      "### Known limitations at this pre-release"]
    assert "run a command" not in section
    assert "ancient" not in section


def test_a_stable_release_has_no_prerelease_mark():
    notes = cg.parse_notes(NOTES.replace(" (pre-release)", ""))
    assert "(pre-release)" not in cg.generate_section(notes, "2026-10-08")


def test_a_bad_date_is_refused():
    with pytest.raises(cg.NotesError):
        cg.generate_section(cg.parse_notes(NOTES), "8 October")


def test_notes_without_a_generatable_section_are_refused():
    notes = cg.parse_notes("# Release notes -- v2.0.0\n\nintro\n\n## How\n\nx\n")
    with pytest.raises(cg.NotesError):
        cg.generate_section(notes, "unreleased")


def test_the_section_goes_above_the_newest_existing_one(tmp_path):
    root = make(tmp_path)
    assert cg.main(["--root", str(root), "generate"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## [1.2.0a1]") < text.index("## [1.0.0]")
    assert text.startswith("# Changelog\n\nIntro of the changelog.")
    assert "First stable." in text


def test_generating_twice_changes_nothing_and_keeps_the_date(tmp_path):
    root = make(tmp_path)
    assert cg.main(["--root", str(root), "generate", "--date", "2026-10-08"]) == 0
    first = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [1.2.0a1] - 2026-10-08 (pre-release)" in first
    assert cg.main(["--root", str(root), "generate"]) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == first


def test_check_passes_after_generate_and_fails_when_the_notes_move(tmp_path):
    root = make(tmp_path)
    cg.main(["--root", str(root), "generate"])
    assert cg.main(["--root", str(root), "check"]) == 0
    (root / "RELEASE_NOTES.md").write_text(
        NOTES.replace("* one thing", "* one different thing"), encoding="utf-8")
    assert cg.main(["--root", str(root), "check"]) == 1


def test_check_fails_when_the_generated_section_is_edited_by_hand(tmp_path):
    root = make(tmp_path)
    cg.main(["--root", str(root), "generate"])
    path = root / "CHANGELOG.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "* a change", "* a change that someone improved"), encoding="utf-8")
    assert cg.main(["--root", str(root), "check"]) == 1


def test_check_fails_without_a_generated_section(tmp_path, capsys):
    root = make(tmp_path)
    assert cg.main(["--root", str(root), "check"]) == 1
    assert "no generated section" in capsys.readouterr().out


def test_check_fails_on_a_second_hand_written_section_for_the_version(tmp_path):
    root = make(tmp_path)
    cg.main(["--root", str(root), "generate"])
    path = root / "CHANGELOG.md"
    path.write_text(path.read_text(encoding="utf-8") +
                    "\n## [1.2.0a1] - 2026-10-09\n\nwritten twice\n",
                    encoding="utf-8")
    assert cg.main(["--root", str(root), "check"]) == 1


def test_a_date_other_than_unreleased_survives_the_check(tmp_path):
    root = make(tmp_path)
    cg.main(["--root", str(root), "generate", "--date", "2026-10-08"])
    assert cg.main(["--root", str(root), "check"]) == 0


NOTES_TWO = """\
# Release notes -- v1.3.0a2 (pre-release)

Second alpha intro.

## Added

* the second alpha thing

---

# Release notes -- v1.2.0a1 (pre-release)

First alpha intro.

## Added

* the first alpha thing, as rewritten in the notes
"""


def test_a_newer_section_goes_above_the_whole_earlier_generated_block(tmp_path):
    """The earlier section's BEGIN marker stands on the line above its heading:
    the new section must not land between the two, or the earlier block loses
    its start and the generator can no longer find it."""
    root = make(tmp_path)
    assert cg.main(["--root", str(root), "generate", "--date", "2026-10-07"]) == 0
    (root / "RELEASE_NOTES.md").write_text(NOTES_TWO, encoding="utf-8")
    assert cg.main(["--root", str(root), "generate", "--date", "2026-10-09"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    begin_a2 = "<!-- BEGIN GENERATED: tools/changelog_from_release_notes.py 1.3.0a2 -->"
    begin_a1 = "<!-- BEGIN GENERATED: tools/changelog_from_release_notes.py 1.2.0a1 -->"
    end = "<!-- END GENERATED: tools/changelog_from_release_notes.py -->"
    assert text.count(begin_a2) == 1 and text.count(begin_a1) == 1
    assert text.count(end) == 2
    # a2 block, then the a1 block, each marker directly above its own heading
    assert text.index(begin_a2) < text.index("## [1.3.0a2]") < text.index(end)
    assert text.index(end) < text.index(begin_a1) < text.index("## [1.2.0a1]")
    assert text.index(begin_a1) + len(begin_a1) + 1 == text.index("## [1.2.0a1]")
    assert text.index("## [1.2.0a1]") < text.index("## [1.0.0]")
    assert text.startswith("# Changelog\n\nIntro of the changelog.")
    # the older generated block is untouched, and the newest one checks out
    assert "* one thing" in text and "* another thing" in text
    assert cg.main(["--root", str(root), "check"]) == 0
    # generating again changes nothing
    assert cg.main(["--root", str(root), "generate"]) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == text


def test_the_repositorys_own_changelog_is_what_its_notes_produce():
    assert cg.check(ROOT) == []
