## VeriHarness: how development work is done in this project

<!-- DELEGATION -->

- Work that changes code and has checks that can fail goes through VeriHarness (`hoh start`, then `hoh run`), not through your own edit-and-declare-done loop. Load the `veriharness` skill (`veriharness/SKILL.md`) before you start.
- RULE outside-herdr: if `HERDR_ENV` is not `1`, do not start a run; tell the user to relaunch inside Herdr. Do not use `--no-herdr` unless the user asks for it by name, and do not do the work by hand and call it done (say it is not harness-verified).
- RULE verdict: work counts as accepted only when `hoh status <run-id>` shows an `accepted_candidate`. Your own test run is not a verdict.
- RULE no-bypass: when `hoh` refuses, report the message verbatim and stop; do not work around it.
- RULE delivery: never run `hoh deliver --approve`, push, tag or publish without the user's explicit yes.
- RULE own-panes: `hoh` creates the role tabs and closes or keeps them itself; never create, type into or close role panes by hand.
- RULE user-config: `hoh start` adds trust entries to the user-level `.claude.json` (with a `.hoh-*.bak` backup beside it); get the user's explicit yes before the first `hoh start`, also under a standing delegation.
- RULE no-delete: delete nothing; rename with a version suffix instead.
