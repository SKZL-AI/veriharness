# Recovery: crashes, blocked dialogs, exhausted quotas

Long-running agent loops fail in the middle of things. A role's process dies,
a permission dialog sits unanswered in a pane, a provider's quota runs out
mid-iteration. This document describes what HoH does in each case, and why
"restart and hope" is not one of the options.

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
