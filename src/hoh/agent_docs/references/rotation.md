# Rotating the orchestrating session

An orchestrating session works for hours. Its context grows until each request is slow and costly
and its own earlier decisions blur. The remedy is a deliberate handover to a fresh session, not a
longer session. Two ways exist; know which one is proven.

## What is proven, and what is not

- The product has the protocol: `hoh project hold`, `hoh project rollover`, `hoh project
  rollover-verify` and `hoh project rollover-accept`. It freezes a capsule whose entries are derived
  facts the successor must re-derive, the operator's decisions kept verbatim, and advisory context
  with no authority; a successor takes the master epoch only if everything re-derives, and a stale
  predecessor is fenced. It is tested with fakes **and has been run live**: the rotation log of the
  project that built this product (`rotation/policy.log.jsonl`) records the live successions, one
  `rotation_finished` row with the outcome COMPLETE for each (count them in your own log; the first
  ones are dated 2026-10-11). Requests also expired unanswered there (`rotation_request_expired`, a
  run of them on 2026-10-19 and 2026-10-20); the most likely cause is that the master
  ran the hold as a plain background job that nothing read, so the request never reached it. That is
  why the product now types each request into the master's own pane when that pane is reachable, and
  otherwise writes it to a file and records why (below). It also needs a governed
  project record, which `hoh project create PROJECT --repo DIR` makes (once; it never replaces one).
- The manual protocol below needs nothing but files and a second Herdr tab. It is a fallback with
  weaker guarantees (the successor checks a digest, not re-derived facts) and no live evidence of its
  own.

## When to rotate

- Only at a quiet point: no run in flight, nothing half merged, nothing to write that only you know.
- When each request has grown to a few hundred thousand tokens, or when you notice yourself
  re-reading your own notes to remember what you decided.
- Never mid-iteration, and never because a run is slow.

## Automatic rotation: a policy decides, not a person

`hoh project hold --watch-pid PID --rotate-policy FILE` (PID is your own session's process; the
hold refuses a policy without it) applies a rotation policy at every renewal of the lease, and only
inside Herdr. The policy is a small JSON document written once: the threshold in context
tokens, where the master's own transcript is, the successor's model, and a capsule input (what to
derive, the operator's decisions) that names exactly one file kept for the successor, the entry
`handoff`; that file is yours alone, never one the product or the harness writes (not inside the
project's directory, not your transcript), or the policy is refused. When the context is at the
threshold the process prints one line, `rotation_requested`, and asks for that file.

**The request is typed into your pane when it can be.** Inside Herdr the hold also types a short notice,
starting `[hoh rotation request rq-...]`, into the pane it was started in (`HERDR_PANE_ID`: your own pane,
because the hold is your child process). This is a deliberate exception to the rule that the product talks
only to agents it started: the pane is the hold's own parent. Before every typing it reads Herdr's state
again and types nothing while the agent is `blocked` (a dialog is open) or `unknown`, when the pane does not
hold exactly one agent with a plain name that no other agent shares, or when the agent changed since the
hold started. **Herdr's state alone is not trusted for dialogs:** Claude Code's autocompact choice ("Want to
autocompact at a lower threshold instead? ... enter confirm, esc not now") was open while `herdr agent get` reported
the agent `idle` and `herdr agent read` showed a stale screen; only the pane's visible screen showed it, a typed
line was swallowed, and the Enter of `agent prompt` would have confirmed the highlighted option. So before
every typing the hold reads the pane's visible screen (`herdr pane read --source visible`) and types nothing
when the last twenty non-blank lines carry "esc not now", "enter confirm", "enter to confirm", "esc to cancel",
"do you want to" or a selected numbered option (an arrow on a numbered choice), or when the screen cannot be read or is
empty; the refusal is a `rotation_undeliverable` row ("a dialog is on the target's screen" or "screen
unreadable") and the request file. Everything typed is composed by the product: fixed words, the request id, the deadline and
the handoff path -- and the path only if it is plain (absolute, letters, digits and `._+-`, no `..`, at most
three hundred characters); any other path is not typed, and the notice names the request file
`rotation/requests/<request id>.request.md` instead (written first, owner-only). It sends the notice when the
request is made, once more at half of `request_ttl_s` if no handoff has appeared, and when the request ends
(`rotation_request_expired`, `rotation_withdrawn`, `rotation_finished`, `rotation_error`; the ending only where
a pane is known); never more. Every attempt is one row of the rotation log: `rotation_delivered` (Herdr took
the keystrokes; it does not say you read them) or `rotation_undeliverable` with the reason. A request that
could not be typed is also written to the request file, and the `rotation_undeliverable` line names it: look
there if you receive nothing. If neither the pane nor the file can be used, the request exists only as the
`rotation_requested` row and the line on stdout. The one row `rotation_delivery_target` at the start says which
pane and agent the hold found. Typing is `herdr agent prompt`, which **submits**: a draft the operator has
typed and not sent in your input box is sent together with the notice. A notice typed while you are busy is
assumed to be queued by your harness until your turn ends (not run live here); if it is lost, the reminder and
the file answer it. If your harness cannot take typed text, or you do not want it, pass `--rotate-no-deliver`
to `hoh project hold` (it is recorded in the log and carried to the successor) and keep a monitor on the
process's output instead: the line on stdout stays in every case. With delivery on, `--ttl` must be at least
two minutes (a delivery may block a renewal for up to three quarters of a minute).
Writing the handoff is your consent to be rotated out, and it answers
that request only if its **first line names the request id** the line carries (for example `# Handoff
rq-...`). Write it, then start no run, merge or commit until a `rotation_finished`,
`rotation_request_expired`, `rotation_withdrawn` or `rotation_error` line follows. A
`rotation_finished` line with the outcome COMPLETE means the successor holds the lease and you are
fenced: do nothing more and leave your tab to the operator. If you are not at a point where you can
stop, do not write the handoff: the request expires, nothing rotates, and a new request comes only
after a cool-down. With the handoff, the product runs the protocol above, binds
a copy of the handoff into the capsule, starts the successor with the same policy (the pointer
carries the policy's digest: a policy file that changed meanwhile is refused), and records
every decision in `rotation/policy.log.jsonl` beside the project's state. Your lease is kept alive
while the successor starts. Every failure leaves you the master.

## The manual protocol

1. Write the handover capsule from `templates/handoff.template.md`: binding rules in reading order,
   what happened since the last handover, the work items and their next steps, what is running,
   the key facts a successor would otherwise rediscover, the order of work, and the acknowledgement
   instruction. Facts only; label each as measured or reasoned.
2. Write its sha256 next to it (`<name>.sha256`).
3. With the operator's yes, and only inside Herdr (`HERDR_ENV` is `1`), open a new Herdr tab and start
   the successor session there with a pointer prompt only: the path
   of the capsule and the instruction to follow its section 7. Never paste the capsule into the prompt.
   Check the pane is wide enough that the pointer line does not wrap.
4. The successor writes the acknowledgement from `templates/ack.template.json` (capsule name, the
   capsule digest it computed, its session id, the time), after comparing the digest with the
   `.sha256` file, and appends one line to the shared ledger that it took over.
5. You wait for that file. No file, or a different digest: you remain the orchestrator. Resend the
   pointer once; do not hand over twice.
6. Once the acknowledgement matches, you stop acting: no new run, no merge, no edit. Leave your tab
   to the operator. Monitors die with you; the successor re-arms its own.

The invariant: at no moment are there two orchestrators. If in doubt, the predecessor stays.
