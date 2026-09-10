# Changelog

All notable changes to this project are documented here. Formatting loosely follows Keep a Changelog conventions; versioning follows SemVer once a public release process exists.

## [0.1.0] - 2026-09-08

Interface changes made during this run, each replacing an earlier German-named equivalent that existed only inside this repository (never a public release):

### Changed -- CLI subcommands

- The goalbook subcommand family is now `hoh goal ...` (`list`, `propose`, `approve`, and related decisions), over a module renamed from `zielbuch.py` to `goalbook.py`.
- The quota-resume subcommand is now `resume-quota`, over a module renamed from `kontingent.py` to `quota.py`.

### Changed -- CLI flags

- Flags on the affected subcommands are now `--title`, `--description`, `--by`, and `--reason`, replacing their earlier German-named equivalents.

### Changed -- JSON output

- JSON output keys emitted by the goal and quota-resume commands were renamed to English -- for example `id`, `status`, `title`, `decided_by`, `by`, and `reason` -- so no command emits a German JSON key any more.

### Changed -- persisted schema

- The goalbook's persisted schema identifier is now `hoh-goalbook.v2`. The identifier is actually read and enforced on load: an unrecognized identifier is rejected by name instead of being guessed at, and a pre-existing `hoh-goalbook.v1` file is migrated rather than silently reinterpreted under the new field names.

### Added -- governance

- Added the seven public-repository governance files: `LICENSE`, `PROVENANCE.md`, `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md` (this file), and `CITATION.cff`. `LICENSE` documents that no license has been chosen yet rather than asserting one.
## [0.1.0] - 2026-09-10 (release candidate)

### Added -- release-candidate documentation

- `RELEASE_NOTES.md`, stating the research-preview positioning, the real hurdles to using HoH, what changed on the way to this release candidate, and what is not yet claimed.
- `RC_GATE.md`, reporting the release-candidate gate condition by condition, each with a status and either structured evidence or the explicit `NOT_RUN` marker -- including the rows only an operator can fill in, such as git status and the still-undecided repository host.
- This section of `CHANGELOG.md`, naming the release candidate without disturbing the section above it.

### Not changed

No file outside these three was created, edited, deleted, or renamed to produce this release candidate. Nothing under `src/hoh/`, `tests/`, or `tools/` changed; `pyproject.toml` and `CITATION.cff` are untouched; the section above stands exactly as it was written.
