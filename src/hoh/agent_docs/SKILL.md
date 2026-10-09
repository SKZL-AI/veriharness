---
name: veriharness
description: "Use when asked to implement, fix or change code in a git project and the hoh command (VeriHarness) is installed. Routes the work through hoh start and hoh run so that a planner, a developer and an independent QA role each work in their own Herdr tab, and counts nothing as done until the harness verdict says so. Needs Herdr (HERDR_ENV=1)."
---

# VeriHarness: do development work through the harness

VeriHarness (`hoh`) runs plan, develop, independent QA, evidence, accept or reject, replan. The
roles are separate agent sessions, each in its own Herdr tab that `hoh` creates and then closes (or keeps and reports).
Acceptance rests on receipts a deterministic runner produced, never on a model saying it is done.
You are the operator's orchestrating session: you write the specification, start the run, read the
verdict, and report it. You do not write the code that the run is supposed to produce, and you do
not judge your own work.

<!-- DELEGATION -->

## Rules (these are not negotiable; the ids are stable)

- RULE outside-herdr: if `HERDR_ENV` is not `1` or `herdr` is not on the PATH, do not start a run. Say so, and give the user the line to run inside Herdr. Do not use `--no-herdr` (the product itself says such a run does not count as evidence) unless the user asks for it by name, and then call the result what it is. Do not do the work by hand and call it done; if you continue without a run, say plainly that the result is not harness-verified.
- RULE verdict: a change is accepted only when `hoh status <run-id>` shows an `accepted_candidate` for it. Your own test run, a green log or a role's claim is not a verdict. A run that is not accepted is a result, not a failure to hide; read it and plan the next step from it.
- RULE no-bypass: when `hoh` refuses (`NEEDS_YOU:`, a storage or isolation refusal, a capability violation, an integrity violation), report the message verbatim and stop. Do not work around a refusal, edit run state, or answer a role's permission dialog in its tab.
- RULE delivery: `hoh deliver <run-id> --approve` is the human's decision. Never pass `--approve`, push, tag or publish without the user's explicit yes in this conversation.
- RULE own-panes: `hoh` creates the role tabs and closes them, or keeps one open and reports it. Do not create, type into, or close panes or tabs for the planner, developer or QA roles, and never touch a tab you did not create.
- RULE user-config: `hoh start` writes the user's agent configuration: it adds trust entries for HoH's run directories to `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json` and leaves a `.hoh-*.bak` backup beside it. Before the first `hoh start`, get the user's explicit yes to that write. This holds under the `standing` delegation as well, and `--trust-worktree` needs its own yes.
- RULE no-delete: delete nothing. No recursive removal, no cleanup of run directories, arenas or worktrees; whatever must go is renamed with a version suffix.

## When to use it, and when not

Use it for a change that needs code and has checks that can fail: a feature, a bug fix with a
reproduction, a refactor with a test net. Do not use it for questions, reading, planning talk, a
one-line edit the user asked you to make directly, or anything without a command that can prove it.
A run costs three role sessions per iteration plus one control run per new acceptance criterion;
say so before you start, and keep `--iterations` and `--max-iterations` small.
`hoh start` also registers the run's own directories as trusted in the agent harness's user-level
configuration file (`${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json` for Claude Code). That is the product's
design, and it is a write to the user's configuration: ask for the yes of RULE user-config before the first
`hoh start`, even when the delegation is standing.

## Before the first run (each step is a command you can check)

```sh
test "${HERDR_ENV:-}" = 1
command -v herdr
command -v hoh
git -C <project> rev-parse --is-inside-work-tree
```

`hoh run` also needs an initialised storage root. If it prints `storage state root is not initialised`,
tell the user the path it names and ask before creating it once:

```sh
hoh storage init --root <storage-root>
```

Roles need agent programs that exist on this machine: pass `--planner`, `--developer` and `--qa`
explicitly (the default QA agent is `kimi`). Prefer `--isolation strict`; if the machine cannot
provide it (no bubblewrap, a restricted user namespace, macOS) `hoh run` refuses before dispatching
anything. Then ask the user before using `--isolation none`.

## The path

1. Write the specification as a file: goal, scope, and acceptance criteria as plain commands or test
   ids that fail before the change and pass after it. A criterion that is already green proves nothing.
2. With the yes of RULE user-config in hand, create the isolated worktree (needs Herdr) and the run record.
   Nothing runs yet.

```sh
hoh worktree --repo <project> --branch <branch>
hoh start --repo <worktree> --spec <spec-file> --run-id <run-id> --max-iterations 3
```

3. Run. This is the command that dispatches the roles and spends quota. It lasts minutes to an hour,
   so start it as a background job (or in a pane of your own) and poll; do not hold a foreground
   call that your tool will time out, and do not end your turn while the run is active.

```sh
hoh run <run-id> --iterations 1 --planner claude --developer claude --qa claude --isolation strict
```

   The first developer dispatch can stop at a folder-trust dialog nobody can answer from a pane. The
   remedy is the flag below, but it **writes the user's agent configuration** (it adds a trust entry for the
   worktree to `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json`, with a backup beside it), so add it only after the
   user says yes to exactly that:

```sh
hoh run <run-id> --iterations 1 --planner claude --developer claude --qa claude --isolation strict --trust-worktree
```

4. Read the state. It is JSON; read the fields, not only the exit code.

```sh
hoh status <run-id>
hoh report <run-id>
hoh list
```

   The fields that decide: `stage`, `condition`, `accepted_candidate`, `blocked_reason`, `stop_reason`,
   `open_items`, `budget`. `INCONCLUSIVE` on a check means it did not run; it is not a rejection.
5. Not accepted: read the evidence in the report, change the specification where the criteria were
   wrong, and run the next iteration. Accepted: tell the user, with the candidate id, and ask about delivery.

## Stuck, paused, or after a break

```sh
hoh pause <run-id> --reason "<reason>"
hoh resume <run-id>
hoh unblock <run-id> --reason "<reason>"
hoh cancel <run-id> --reason "<reason>"
hoh resume-quota
```

A block is a waiting state. `resume-quota` resumes only runs that wait on a provider usage limit.
`unblock` needs a reason the user would accept; a role waiting on a permission dialog is the user's to answer.
A fresh session continues by reading `hoh list` and `hoh status <run-id>` first, never by starting over.
Projects with several runs exist only when `hoh project list` shows one; then `hoh project resume <project>`
says what to do next. Never drive a project that is not listed.

```sh
hoh project list
hoh project resume <project>
```

## More, in the files next to this one

- `references/method.md`: how larger work is run (parallel strands, specification drafting, two independent adversarial reviewers, the merge gate, lean verification, records by addition, safety rules).
- `references/rotation.md`: when this session's context is too long, and how to hand over to a successor.
- `templates/`: handoff, acknowledgement, drafting brief and reviewer rules, with placeholders to fill.

If a command above is refused as unknown, trust `hoh --help` over this file and tell the user to
re-run `hoh agent-setup --install` with the installed version.
