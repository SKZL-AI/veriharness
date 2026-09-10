# Operations

This is the English successor to this project's original operational notes,
not their replacement -- those notes stay exactly as they are, in German, as
the original operational record. (That framing traces back to the
specification that commissioned this document.) This document covers the
same ground for readers who need it in
English: prerequisites, the usual path through a run, how to intervene while
one is active, a map of what lives where on disk, and a troubleshooting
table for when something does not behave as expected.

## Prerequisites

| | |
|---|---|
| Python | 3.11 or newer, with `pydantic` installed |
| Herdr | A session must be running; `HERDR_ENV=1` is required, and there is deliberately no tmux fallback |
| Git | The target project must be a git working tree |

Install for operation from the project root:

```sh
python3 -m pip install -e .
```

This registers the `hoh` console script (`hoh = "hoh.cli:main"`), so `hoh`
becomes available directly rather than needing `python3 -m hoh.cli`.

## The usual path, start to finish

`--root` goes **before** the subcommand (it defaults to a `hoh/runs`
directory under your home directory, `$HOME`, overridable with the
`HOH_RUNS` environment variable).

```sh
# 1. Create an isolated worktree -- Herdr manages it
hoh worktree --repo <project> --branch hoh-work

# 2. Register trust for the fresh worktree ahead of time, so the developer
#    role does not stall on a permission dialog nobody can answer from a pane
<your-harness-trust-registration-step> <worktree> <project>

# 3. Create the run
hoh start --repo <worktree> --spec <spec.md> --run-id <id> [--max-iterations 10]

# 4. Run iterations -- THIS is the command that does the work
hoh run <id> --iterations 2 --planner claude --developer claude --qa claude

# 5. Check the state
hoh status <id>
hoh report <id> [--out report.md]

# 6. Deliver -- only with an explicit approval
hoh deliver <id> --approve --into main
```

Step 2 is harness-specific (this project's own operators use a companion
trust-registration script that is not part of HoH itself); whatever your
harness uses to pre-approve a fresh working directory belongs there. Without
it, the first developer dispatch can block on a trust dialog that nothing in
a non-interactive pane can answer.

`hoh start` only ever creates the run record -- **nothing runs
automatically**. Without step 4, the run sits in stage `NEW` indefinitely.

### Objectives across run boundaries

```sh
hoh goal list
hoh goal propose <id> --title "..." [--spec <path>] [--by agent]
hoh goal approve <id> --by <name> --reason "..."
hoh goal activate|complete|drop <id> --by <name> --reason "..."
```

See `docs/GOALBOOK.md` for the semantics: a proposed objective does nothing
until approved, and a proposal that meaningfully widens the frame surfaces a
frame-gate report that has to be confirmed as a whole.

### Resuming runs stuck on a quota

```sh
hoh resume-quota          # check every run
hoh resume-quota <id>
```

Resumes only `blocked_kind = usage_limit`, and only once `retry_after` has
passed. See `docs/RECOVERY.md` for the full mechanism. Suitable for a cron
job or a Herdr plugin action, precisely because it never resumes anything a
human has not already implicitly approved by way of the original run
configuration.

### As a Herdr plugin

```sh
herdr plugin link ./plugin --enabled
herdr plugin action invoke {list,status,report,resume-quota} --plugin hoh
```

See `docs/HERDR_INTEGRATION.md` for why the action list stops there: `run`,
`start`, `deliver`, and `cancel` need an explicit run identifier or an
explicit approval, and Herdr's plugin actions cannot carry arguments.

### Intervening in a running loop

```sh
hoh pause   <id> --reason "..."   # takes effect at the next safe boundary, even mid-iteration
hoh resume  <id> [--force]        # reconciles against Herdr's own state before resuming
hoh unblock <id> --reason "..."   # lifts a block once its cause is resolved
hoh cancel  <id> --reason "..."
hoh list
```

`pause` and `cancel` work even while an iteration is running: the controller
holds its lock for the whole iteration, so the stop request is recorded
without the lock and takes effect at the next safe boundary between phases
(never in the middle of a running check).

`hoh run --no-herdr` runs the roles as plain subprocesses instead of inside
Herdr panes. Useful for local development; it creates no Herdr endpoint, so
such a run carries no acceptance value as evidence of a real, demonstrable
agent session.

## Where things live

```
<HOH_RUNS>/<run_id>/
  state.json               the one authoritative pointer to the current state
  state.json.v<n>.<ts>     earlier states -- never deleted, only rotated aside
  evidence.json            E_t, the evidence state that crosses iteration boundaries
  checks.json              the preservation suite: once-validated criteria that
                            run again on every further candidate
  stop-request.json        a recorded pause/cancel request (written without the lock)
  results/plan-i<n>.json   frozen plans
  results/qa-i<n>.json     QA role results
  receipts/<id>.json       runner receipts, immutable
  logs/<id>.txt            raw stdout of every check
  answers/<i>-<role>.json  the raw role answers
<HOH_RUNS>/_arenas/<run_id>/
  <digest>/                verification directories -- next to the run directory,
                            never inside it (see docs/ARCHITECTURE.md for why).
                            Directory names are digests derived from a per-run
                            secret, so a check command cannot recognize the
                            control run from its own working directory path.
  attic/                   rotated states and arenas -- parked, not deleted
  .lock                    the controller lock
```

**Space:** an arena is roughly the size of the object under test, once per
iteration and attempt -- for a real project this can be hundreds of
megabytes. `hoh run` applies a retention limit after every run: the most
recent 20 states and 3 arenas stay in place; older ones move to `attic/`.
**Receipts, logs, role results, and `evidence.json` are never rotated.**
`hoh status` reports current usage by category.

The project's live workspace is never touched by verification itself: every
iteration materializes an isolated copy into its own arena. Whatever QA
leaves behind there -- caches, build artifacts -- cannot affect the candidate
binding.

## Limits that hold in operation

- **`yolo` is always `off`.** No developer task can auto-merge into a
  protected branch ahead of the independent QA step. This project grants no
  additional push, merge, deploy, or purchase rights.
- **One controller per run**, enforced by a cross-process lock (`flock`).
- **A `PASS` needs a supporting receipt.** See `docs/EVIDENCE_MODEL.md`.
- **`nvidia-smi` is refused before execution**, even disguised as an
  acceptance criterion.
- **Nothing is deleted.** Superseded states are parked with a version
  suffix, never removed.

## Troubleshooting

| Symptom | Meaning | What to do |
|---|---|---|
| `LockBusy` | Another controller holds this run | Do not force it. `hoh status` shows the state; confirm the other controller is actually alive first. |
| `condition: BLOCKED` | Unclear state, or an exhausted budget | Read the reason in `hoh status`. A block is a waiting state, not a failure. |
| "candidate binding violated" | Sources changed between freeze and verdict | The verdict is invalid. Do not overwrite it -- find the cause first. |
| State file unreadable | `state.json` is corrupted | The most recent parked state sits alongside it (`state.json.v*`). Copy it back deliberately; do not delete anything. |
| "stale writer" rejected | A second controller was working from an outdated state | Do not force it. The current on-disk state wins; the other controller has to reload. |
| Role waiting on an approval in its pane | The harness is asking for a permission | Open the tab, decide, then `hoh unblock` and `hoh run` again. HoH never answers such dialogs itself. |
| "does not demonstrate the increment" | A criterion was already green on the predecessor state | Not a bug -- this is the point: the plan has to measure what actually changed. |
| "check command changes the directory" | A plan tried to `cd` out of the arena | Rewrite the criterion; use the `{ARENA}` placeholder for the path instead. |
| `Delivery refused` | Missing approval, a dirty tree, or no fast-forward | The stated reason is the answer. HoH performs no rebase and no force-push of its own. |

## The way back

An accepted checkpoint is an internal HoH state, not a delivery -- the way
back is always available:

1. `hoh report <run-id>` shows the most recently accepted candidate.
2. A rejected candidate is kept, evidence and all -- it is never discarded.
3. HoH's only writer to the project's working tree is the developer role,
   inside the isolated worktree Herdr created for the run; ordinary `git`
   operations in that worktree are the normal way back.
4. The parked `state.json.v*` files let you reconstruct any earlier
   workflow state.

## Turning it off

HoH is a separate project and does not hook into anything by default. Simply
not invoking it is sufficient -- the harness, Herdr, and every other project
keep working unchanged. See `README.md` for why that has to remain true by
design, not just by absence of a background process today.
