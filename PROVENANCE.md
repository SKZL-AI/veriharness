# Provenance

This project ("hoh") builds on external work in two distinct ways: it drives an external runtime as a subprocess, and its own workflow design is adapted from published prior work.

This file cites a source for every license statement it makes: a fact that exists locally on this machine, a dated external citation naming the exact endpoint or URL read, or -- where neither is available -- the `UNVERIFIED` marker. A citation without a date is not a source; it is a claim wearing a source's clothes, so every dated citation below names the day it was read. See `LICENSE` for this project's own licensing status.

## Herdr -- runtime dependency

- This project invokes an external `herdr` binary as a subprocess at runtime rather than importing it as a library; see `src/hoh/herdr.py` for every call site and the `require_herdr()`/`available()` checks that guard them.
- Herdr's project license is Apache-2.0, per the GitHub API license endpoint `repos/herdrdev/herdr/license` (spdx_id: "Apache-2.0", path: LICENSE, 11357 bytes; default branch master), which the project homepage herdr.dev states the same way; read 2026-09-08.
- That project-level statement is about the upstream Herdr project, not necessarily about the build actually used here: the `herdr` binary installed in this worktree is version 0.8.0 (confirmed via `herdr --version` -> `herdr 0.8.0`), it ships with no bundled LICENSE file of its own, and it is the v0.9.0 Homebrew formula -- one version ahead of what is installed here -- that independently states Apache-2.0. This project relies on the upstream project statement above to cover the 0.8.0 binary; there is no local license file for 0.8.0 to read instead.
  Because `hoh` only shells out to the `herdr` binary as a subprocess and never links or vendors its code (see the call-site bullet above), Herdr's license does not attach to `hoh`'s own MIT-licensed source; this is a factual observation about how the code calls out, not a legal opinion.
- No affiliation with, or endorsement by, the authors or maintainers of Herdr is implied by this project's use of it as a runtime.

## Harness-of-Harness -- prior work / conceptual basis

- This project's workflow -- repeated planning, development, and independent QA over persisted, evidence-bound state -- is adapted from published Harness-of-Harness (HoH) work. Two dated external citations back that adaptation, the weakest of the three source forms named above, and both are given here in full rather than through a pointer to a local source.
- **The paper (Q1):** https://arxiv.org/html/2609.01481v1, read as HTML at that arXiv address on 2026-09-06. It is the source for the role sequence, the artifact and evidence state, the independent check, and progressive disclosure. It was not built or executed locally; reading a paper is not running its code.
- **The repository README (Q2):** https://github.com/Flesymeb/HarnessOfHarness/blob/main/README.md, checked on 2026-09-06 at recorded blob `4142d6aa7b931fd4d8644d55ebc6f0b343ddadc1`. What it establishes is a negative: HoH-lite was still only announced there, not shipped. That is why this project claims no dependency on an installable HoH package -- there was none to depend on.
- Harness-of-Harness's license is MIT, per the GitHub API license endpoint `repos/Flesymeb/HarnessOfHarness/license` (spdx_id: "MIT", path: LICENSE, 1067 bytes; the file's own text reads "Copyright (c) 2026 Hyoung Yan"); read 2026-09-08.
- This is an independent adaptation, not an official Harness-of-Harness release, not an installable HoH package, and not a full replication of the paper.
- No affiliation with, or endorsement by, the authors of the Harness-of-Harness paper or repository is implied by this project's use of their concepts.

## This project's own copyright status

- This repository's own license is MIT; the complete, unmodified text is in `LICENSE`, with a copyright line naming the year and the holder read from `git config user.name`.
- Apache-2.0 was the alternative the captain considered and did not choose: choosing MIT deliberately gives up Apache-2.0's express patent grant and its NOTICE-file mechanics; this is a stated trade-off, not a claim that MIT is "the standard" license.
- MIT is compatible with what this project builds on: see the Herdr subprocess-only observation above, and the Harness-of-Harness and pydantic/pydantic-core entries in this file and in `THIRD_PARTY_NOTICES.md`, none of which this project links against or vendors.

## Everything else this project imports

- Every other third-party name this project's own source imports at runtime is listed with its own license status in `THIRD_PARTY_NOTICES.md`; this file does not repeat that list.
