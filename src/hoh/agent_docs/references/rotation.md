# Rotating the orchestrating session

An orchestrating session works for hours. Its context grows until each request is slow and costly
and its own earlier decisions blur. The remedy is a deliberate handover to a fresh session, not a
longer session. Two ways exist; know which one is proven.

## What is proven, and what is not

- The product has the protocol: `hoh project hold`, `hoh project rollover`, `hoh project
  rollover-verify` and `hoh project rollover-accept`. It freezes a capsule whose entries are derived
  facts the successor must re-derive, the operator's decisions kept verbatim, and advisory context
  with no authority; a successor takes the master epoch only if everything re-derives, and a stale
  predecessor is fenced. It has been tested with fakes. **No live succession has been run, so
  nothing here is proven by it.** It also needs a governed project record, and `hoh` has no command
  that creates one yet; do not improvise one.
- The manual protocol below needs nothing but files and a second Herdr tab. It is a fallback with
  weaker guarantees (the successor checks a digest, not re-derived facts) and no live evidence in this
  repository either.

## When to rotate

- Only at a quiet point: no run in flight, nothing half merged, nothing to write that only you know.
- When each request has grown to a few hundred thousand tokens, or when you notice yourself
  re-reading your own notes to remember what you decided.
- Never mid-iteration, and never because a run is slow.

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
