# Recovery: crashes, blocked dialogs, exhausted quotas

Long-running agent loops fail in the middle of things. A role's process dies,
a permission dialog sits unanswered in a pane, a provider's quota runs out
mid-iteration. This document describes what HoH does in each case, and why
"restart and hope" is not one of the options.

*The section at the end, "Since v0.1.0", describes the recovery paths added
after the tag and corrects one statement below by addition.*

## The rule behind all of it: an unclear state blocks, it does not restart

Handoff §7, the founding design note this project builds from, states the
governing rule directly: *"On resume, first reconcile the existing
task/endpoint identity and liveness: a running worker is re-attached, a
demonstrably finished one is evaluated, an unclear state blocks. Do not
restart blindly."* Restarting blindly into a state you cannot account for
risks double-starting a role that is actually still running, or silently
discarding evidence about one that already finished. Blocking costs nothing
but time; guessing wrong costs correctness.

## The three verdicts

`herdr.liveness()` in `src/hoh/herdr.py` answers, for one Herdr pane, exactly
one of three verdicts:

- **`attach`** -- the agent's status in Herdr's own snapshot is `working` or
  `blocked`. The session is genuinely still running; HoH re-attaches to it
  rather than starting a competing one.
- **`evaluate`** -- the status is `idle` or `done` (the pane is responsive),
  or the pane no longer exists at all. Either way, the role is demonstrably
  finished and HoH can move on to evaluating what it produced.
- **`block`** -- everything else: a status Herdr itself reports as
  something other than the above, or a pane that Herdr's snapshot shows
  existing but recognizes no agent in. `liveness()`'s own comment is explicit
  about why this case exists at all -- an earlier version of this logic
  treated any status other than `working`/`blocked` as "finished," even
  though Herdr's own documentation says of an `unknown` status, in so many
  words, that it "does not prove completion." Reading `unknown` as `evaluate`
  would have been exactly the blind restart the rule above forbids.

`cli._reconcile` calls `liveness()` for every task HoH has a persisted
start-intent for (`RunState.active_tasks`) when `hoh resume` runs. If **any**
task comes back `block` and `--force` was not passed, the whole resume is
refused and the run itself transitions into `BLOCKED`
(`stages.block`) rather than proceeding on a partial picture. `--force`
exists for the case where a human has independently confirmed nothing is
actually running any more; it discards the unclear task references rather
than trusting them.

## Crash during DEVELOPING or VERIFYING

If the process driving the loop dies mid-iteration, the persisted
`RunState.stage` is left wherever it was -- most often `DEVELOPING` or
`VERIFYING`, since those are the stages that dispatch to an LLM role and are
therefore the most likely to be interrupted. `stages.begin_iteration`
recognizes both as recoverable: rather than refusing (the state machine's
normal edges do not permit `DEVELOPING -> PLANNING` or
`VERIFYING -> PLANNING` directly), it performs a documented `RECOVERY`
transition back to `PLANNING`, thaws the frozen candidate binding
(`_thaw`), and logs the recovery in the run's history. The iteration counter
is **not** reset -- `attempt` stays where it was, because resetting it would
let the retried attempt collide with receipt IDs the failed attempt already
wrote, and the immutability boundary on evidence files would then block the
very restart this mechanism exists to allow. The attempt that was interrupted
is not booked as progress either way.

## A permission dialog blocking in a role's pane

HoH never answers a trust or permission dialog itself -- that would be
exactly the kind of silent privilege escalation the rest of this project
guards against. When a role is stuck on one, `WaitingForApproval`
(`src/hoh/controller.py`) is raised as its own exception type rather than
being described only in an error string. That distinction has a direct
operational consequence in `cli.cmd_run`: on any other kind of failure, the
CLI closes its own Herdr tabs on the way out so they do not clutter the
multiplexer; when the failure is specifically `WaitingForApproval`, it
leaves the role tabs open instead, because closing them would remove the one
place a human could still answer the dialog. This project's own dogfood run
found the failure mode this guards against on itself: an earlier version
matched on the German word for "approval" appearing in a log message, a
coupling between two modules that no test covered, and it broke silently
when the message was translated to English. The fix was to make the
distinction a type the controller raises, not a word a caller greps for.

Recovery, once the dialog is answered: `hoh unblock <run-id> --reason "..."`
(only if the run ended up `BLOCKED`), then `hoh run <run-id>` again.

## An exhausted provider quota

From the outside, a quota exhaustion looks exactly like a failure -- and
treating it as one is a real defect this project found in itself. In an
earlier internal run, an outage during QA was reported as `"not accepted:
K1..K9"`, phrased as though nine criteria had been examined and rejected,
when in truth QA had never answered a single one. `src/hoh/quota.py` exists
specifically to tell these apart. `quota.is_quota_exhausted()` recognizes
provider phrasings for rate limits and exhausted quotas (including,
deliberately, a German phrase -- these patterns match text emitted by
*provider CLIs*, not by this project's own code, so a non-English harness
message still has to be recognized). `stages.block()` marks the run
`BLOCKED` with `blocked_kind="usage_limit"` and a machine-readable
`retry_after` computed by `quota.next_attempt_at()` from whatever reset time
the provider's message named (falling back to a one-hour default otherwise).

`hoh resume-quota` (callable by hand, from cron, or as the Herdr plugin's
`resume-quota` action) is the counter-design to "HoH sleeps by itself": a
controller that holds its lock while sleeping for hours would be
indistinguishable from one that has hung. Instead the run stays visibly
`BLOCKED`, and `resume-quota` picks it up once `quota.is_due(retry_after)`
says the wait is over. It resumes **only** `blocked_kind="usage_limit"` --
every other block has a cause a human has to clear, and stepping over that
automatically would reintroduce exactly the silent-automation risk this
project is built against. After `MAX_RESUME_ATTEMPTS` (5) unsuccessful
automatic resumes in a row, the run stops trying and stays blocked, because
unbounded automatic retries can spend money without making any progress.

## `BLOCKED` is not a dead end

An earlier version of this state machine made `BLOCKED` unrecoverable:
`stages.resume()` only accepts `PAUSED`, and there was no other exit.
`stages.unblock()` exists to close that gap -- it requires a non-empty
`resolution` string, records who lifted the block and why in the run's
history, and returns the run to `ACTIVE`. The next `hoh run` invocation then
replans the interrupted iteration conservatively rather than resuming
mid-flight, following the same `begin_iteration` recovery path described
above.

## Since v0.1.0

Added for the v0.3.0a1 pre-release; the sections above stay as written.

**Idle is not done.** The three liveness verdicts above are unchanged. On the
dispatch side, a role that ended its turn without writing its answer file --
typically while a background job it started was still running -- used to cost
the whole iteration as "no answer". Now the dispatcher sends that agent **one**
follow-up naming the exact answer path and waits again; only a second missing
answer fails the dispatch, with the pane view attached. A Herdr
`agent_prompt_stalled` failure is now transient and retried rather than
blocking the run. After a stalled submit the dispatcher presses Enter once, on
this dispatch's own prompt, and never types again; a pane that still holds
unsent input is refused rather than typed into, and prompts no longer begin
with `/`, which the agent interface read as an unknown command. Known gap: the
first prompt after an agent starts can still be typed and not submitted -- the
dispatch recovers, the startup race is not fixed (`docs/LIMITATIONS.md`,
*Current open limitations*). Every role prompt above 8 KiB, or holding a
character the input box treats specially, now goes through a pointer file so the
recovery can verify the box; the recovery still refuses, as designed, when
Claude Code word-wraps the pointer line in a narrow pane (it blocked one run
before any candidate, and the readiness fix is the next P2 strand).

**Durable waits and an acting resume loop.** A wait is now a durable timer on
disk, not a sleeping process. `hoh resume-quota` acts on due timers; with
`--drive --recover`, the unattended continuation unit drives a resumed project
on under a new governed master, and recovers a project whose master died.
Only a holder that is provably gone is replaced: session succession and the
master epoch make a returning old master's writes non-authoritative. A retry
timer is rounded up, so it is never due before the provider's reset.

**A correction to "every other block has a cause a human has to clear".**
That still holds for the quota path it describes, and there is now a second
automatic consumer: a run parked under storage pressure (`STORAGE_PRESSURE`)
is resumed by `hoh resume-storage` once admission allows it again. A
capability violation, a contract park and a question for a human keep their
owners: a run held for a capability violation is refused by `hoh resume` in
every state but PAUSED, is never delivered, and only `hoh unblock` lifts the
hold.

**Interrupted state at the orchestrator level.** When the machine's disk
filled on 2026-09-25, an uncommitted working tree of the development
repository held single-copy evidence. It was preserved as a commit object on a
preserve reference without touching the working tree, and recovered days later
by merging that commit with the line that had moved on, with no history
rewritten and nothing removed. This is the orchestrating session's practice,
not a product feature; the product's own storage lifecycle exists so that the
next incident is admitted, parked and resumed instead.

**A fresh orchestrating session (context rollover, Layer A).** The master
epoch can be handed to a successor session without a gap and without two
masters: the outgoing session freezes a capsule of facts, the successor
re-derives every one, acknowledges, and takes the epoch only if nothing moved;
otherwise the outgoing session stays master or takes the programme back, and a
stale one is fenced (`docs/OPERATIONS.md`, *Rolling the orchestrating session
over*). Merged and tested with fakes, **not run on a live succession**, and not
a proof of any register row.

**Not yet proven:** recovery across a real reboot (stressor DS2) has never
been run, and the end-to-end durability evidence for the unattended path is
stale since the code changed. A provider usage limit that stops a QA role is
still booked as a rejected iteration rather than a resumable block, and a
role's background processes, which closing its pane does not end, are ended by
the merged role-pane lifecycle only by a mark anchored to the pane, with the
limits `docs/LIMITATIONS.md` states.
