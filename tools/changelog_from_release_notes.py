#!/usr/bin/env python3
"""changelog_from_release_notes.py -- the changelog section of a release is
generated from the release notes, not written a second time.

`RELEASE_NOTES.md` is the one place a release is described. `CHANGELOG.md`
lists what changed by version, and a hand-kept second copy of the same account
drifts (this project has already corrected figures that two documents agreed on
and both had wrong). So the section of the newest release is a deterministic
transform of the notes:

* the heading is `## [<version>] - <date or "unreleased"> (pre-release)`, the
  version and the pre-release mark read from the notes' own heading
  `# Release notes -- v<version> (pre-release)`;
* the introduction of the notes (everything before their first `##`) is copied
  verbatim;
* every notes section whose title starts with `Added`, `Changed`, `Fixed` or
  `Known limitations` is copied verbatim under the same title one level down;
  other sections (how to check the release, say) stay in the notes only.

The generated section sits between two marker comments. `check` regenerates it
and fails when the file differs (the date may be `unreleased` or a calendar
date) or when the version has a second, hand-written section.

Usage:
    python3 tools/changelog_from_release_notes.py generate [--date YYYY-MM-DD]
    python3 tools/changelog_from_release_notes.py check

At the cut, run `generate --date <day of the cut>`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

NOTES_HEAD = re.compile(
    r"^# Release notes -- v(?P<version>[0-9][^\s]*)(?P<pre> \(pre-release\))?\s*$",
    re.M)
INCLUDED = ("Added", "Changed", "Fixed", "Known limitations")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NotesError(Exception):
    pass


def parse_notes(text: str) -> dict:
    """The newest release section of the notes: version, mark, intro, sections."""
    m = NOTES_HEAD.search(text)
    if not m:
        raise NotesError("no heading '# Release notes -- v<version>' in the notes")
    start = m.end()
    stop = len(text)
    nxt = re.search(r"^# Release notes -- ", text[start:], re.M)
    if nxt:
        stop = start + nxt.start()
    body = text[start:stop]
    body = re.sub(r"\n---\s*\n*\Z", "\n", body.rstrip("\n") + "\n")
    parts = re.split(r"^## ", body, flags=re.M)
    intro = parts[0].strip("\n")
    sections = []
    for part in parts[1:]:
        title, _, rest = part.partition("\n")
        sections.append((title.strip(), rest.strip("\n")))
    return {"version": m.group("version"), "prerelease": bool(m.group("pre")),
            "intro": intro, "sections": sections}


def generate_section(notes: dict, date: str) -> str:
    if not (date == "unreleased" or DATE_RE.match(date)):
        raise NotesError(f"date must be YYYY-MM-DD or 'unreleased', not {date!r}")
    version = notes["version"]
    mark = " (pre-release)" if notes["prerelease"] else ""
    chosen = [(t, b) for t, b in notes["sections"]
              if t.startswith(INCLUDED)]
    if not chosen:
        raise NotesError("the notes have no Added, Changed, Fixed or Known "
                         "limitations section to generate from")
    out = [f"<!-- BEGIN GENERATED: tools/changelog_from_release_notes.py "
           f"{version} -->",
           f"## [{version}] - {date}{mark}", "",
           notes["intro"], ""]
    for title, body in chosen:
        out += [f"### {title}", "", body, ""]
    out.append("<!-- END GENERATED: tools/changelog_from_release_notes.py -->")
    return "\n".join(out)


def _block_span(text: str, version: str):
    begin = (f"<!-- BEGIN GENERATED: tools/changelog_from_release_notes.py "
             f"{version} -->")
    end = "<!-- END GENERATED: tools/changelog_from_release_notes.py -->"
    i = text.find(begin)
    if i == -1:
        return None
    j = text.find(end, i)
    if j == -1:
        return None
    return i, j + len(end)


_BEGIN_RE = re.compile(
    r"^<!-- BEGIN GENERATED: tools/changelog_from_release_notes\.py [^\s>]+ -->[ \t]*\n?\Z",
    re.M)


def splice(changelog: str, section: str, version: str) -> str:
    span = _block_span(changelog, version)
    if span:
        return changelog[:span[0]] + section + changelog[span[1]:]
    m = re.search(r"^## \[", changelog, re.M)
    if not m:
        return changelog.rstrip("\n") + "\n\n" + section + "\n"
    at = m.start()
    # When that heading belongs to an earlier generated section, its BEGIN
    # marker stands on the line above it: the new section goes above the whole
    # earlier block, not between the marker and its heading (which would leave
    # the earlier block without its start and `check` unable to find it).
    marker = _BEGIN_RE.search(changelog[:at])
    if marker:
        at = marker.start()
    return changelog[:at] + section + "\n\n" + changelog[at:]


def _current_date(changelog: str, version: str) -> str:
    m = re.search(r"^## \[" + re.escape(version) + r"\] - (\S+)", changelog,
                  re.M)
    return m.group(1) if m else "unreleased"


def check(root: Path) -> list[str]:
    problems = []
    try:
        notes = parse_notes((root / "RELEASE_NOTES.md").read_text(encoding="utf-8"))
    except (OSError, NotesError) as exc:
        return [f"release notes unreadable: {exc}"]
    version = notes["version"]
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    span = _block_span(text, version)
    if span is None:
        return [f"CHANGELOG.md has no generated section for {version}; run "
                "`python3 tools/changelog_from_release_notes.py generate`"]
    date = _current_date(text, version)
    try:
        fresh = generate_section(notes, date)
    except NotesError as exc:
        return [str(exc)]
    if text[span[0]:span[1]] != fresh:
        problems.append("the generated CHANGELOG.md section differs from what "
                        "the release notes produce; regenerate it, and edit "
                        "RELEASE_NOTES.md rather than the section")
    outside = text[:span[0]] + text[span[1]:]
    if re.search(r"^## \[" + re.escape(version) + r"\]", outside, re.M):
        problems.append(f"CHANGELOG.md carries a second, hand-written section "
                        f"for {version}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--date", default=None,
                   help="YYYY-MM-DD, or 'unreleased' (default: the date "
                        "already in the file, else 'unreleased')")
    sub.add_parser("check")
    ap.add_argument("--root", type=Path, default=HOH, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if args.cmd == "check":
        problems = check(root)
        for p in problems:
            print(p)
        if not problems:
            print("OK: the CHANGELOG.md section is what the release notes "
                  "produce")
        return 1 if problems else 0
    try:
        notes = parse_notes((root / "RELEASE_NOTES.md").read_text(encoding="utf-8"))
        path = root / "CHANGELOG.md"
        text = path.read_text(encoding="utf-8")
        date = args.date or _current_date(text, notes["version"])
        section = generate_section(notes, date)
    except (OSError, NotesError) as exc:
        print(f"cannot generate: {exc}", file=sys.stderr)
        return 2
    path.write_text(splice(text, section, notes["version"]), encoding="utf-8")
    print(f"wrote the section for {notes['version']} ({date}) into CHANGELOG.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
