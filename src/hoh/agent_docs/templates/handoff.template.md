# Orchestrator handoff: <PROJECT>, session <OLD_SESSION> to successor (<UTC>)

You are the successor orchestrator of <PROJECT>. Session <OLD_SESSION> hands over because its
context reached about <TOKENS> tokens per request. After your acknowledgement (section 7) you are the
only orchestrator; <OLD_SESSION> acts no more.

## 1. Binding rules (read first, in this order)
1. <RULES_FILE_1>
2. <RULES_FILE_2>
3. The ledger <LEDGER_FILE>: read from the line "<LAST_HANDOVER_MARKER>" to the end.
4. Reviewer rules <REVIEWER_RULES_FILE>; drafting brief <DRAFTING_BRIEF_FILE>.

Operator preferences: <LANGUAGE_OF_CHAT>; reports short; never push, tag or release without the
operator's yes, and show the commit first; quota exhaustion is never a verdict; delete nothing
(rename with a version suffix); foreign data is no-touch; subagents run on <SUBAGENT_MODEL>.

Repository <REPO>, branch <BRANCH>, head <HEAD_SHA>.

## 2. What happened since the last handover
- <COMMIT>: <one line: what, which gate, which receipt>
- Slips (recorded): <what went wrong, effect, fix>

## 3. Work items
| item | state | next step |
|---|---|---|
| <ITEM> | <STATE: not started / drafted / reviewed / running / accepted / merged> | <NEXT> |

## 4. Running processes and agents
- <DASHBOARD_OR_MONITOR_TAB>: never close it.
- Reviewers, drafters, product runs: <NONE or LIST WITH TAB LABELS>.
- Monitors die with this session; re-arm your own.

## 5. Key facts (each labelled measured or reasoned)
- <FACT>

## 6. Order of work
1. Acknowledge (section 7).
2. Re-arm your monitors.
3. Tell the operator in <LANGUAGE_OF_CHAT>, in three to five lines.
4. <NEXT_ITEMS>

## 7. Acknowledgement (do this first)
Write <ACK_FILE> with exactly this shape:
`{"handoff": "<HANDOFF_FILE>", "handoff_sha256": "<sha256 of this file>", "successor_session": "<your session id>", "at": "<UTC>"}`.
Compare the digest with <HANDOFF_FILE>.sha256. If they differ, write nothing and tell the operator.
Append to <LEDGER_FILE>: "<UTC> successor orchestrator <your session id> took over (handoff <HANDOFF_FILE> sha256 ...)".
