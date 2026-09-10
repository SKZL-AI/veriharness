# VeriHarness — Figures

Diagrams only, no image files. Both are referenced from `POSITION_PAPER.md`
and use the same run and finding names as the prose.

## Figure 1 — per-run correctness composes; post-merge correctness does not

Illustrates §1 of the position paper: two runs, each independently
receipt-verified against its own base, merge into a state neither run's own
acceptance criteria ever examined. The license case is instance one, found
by a person; the coverage-anchor case is instance two, found by
`tools/check_claims.py` itself. Both are read from the project's own
unsupported-by-design historical count (C-052..C-054): a project-wide
consistency property that no evidence form in this ledger's schema can
positively verify from per-run receipts alone.

```mermaid
flowchart TD
    subgraph RunA["Run d1 -- packaging"]
        A1[Acceptance criteria checked\nagainst run d1's own base] --> A2[d1 accepted:\nreceipt-bound, locally correct]
    end
    subgraph RunB["Run d2 -- governance files"]
        B1[Acceptance criteria checked\nagainst run d2's own base] --> B2[d2 accepted:\nreceipt-bound, locally correct]
    end
    A2 --> M[Merge into master]
    B2 --> M
    M --> U[Union state:\nno criterion of either run\never examines the combined tree]
    U --> F1["Instance 1 (human-found):\nLICENSE, pyproject.toml, CITATION.cff\ndisagree on the project's own licence"]
    U --> F2["Instance 2 (machine-found):\ncheck_claims.py exit 0 on the candidate,\nfive failures on the merged state"]
    F1 --> N["Neither run was wrong on its own terms --\nper-run evidence binding was necessary,\nnot sufficient"]
    F2 --> N
```

## Figure 2 — the arena's isolation makes `runs/` invisible to a criterion

Illustrates §6 of the position paper and `docs/LIMITATIONS.md` limit 11: a
candidate arena is materialized with `git archive <tree>`, which snapshots
exactly the tracked tree and nothing else. `runs/` is gitignored, so it is
absent from that snapshot before a check command ever runs, independent of
what the check tries to assert -- two historical counts (a `git archive`
entry count and a per-run criterion-failure count) that `CLAIMS.json` marks
unsupported rather than confirmed, since this checkout's evidence forms
cannot positively re-derive either one on their own (C-056, C-057).

```mermaid
flowchart LR
    R["Project working tree\n(tracked files + runs/, gitignored)"] -->|git archive tip| S["Candidate snapshot\n(tracked files only)"]
    S --> Ar["Check-run arena\n(no .git, no runs/, no history)"]
    Ar --> C1["Criterion asserting a property\nof the candidate itself"]
    Ar --> C2["Criterion asserting a property\nof runs/ evidence"]
    C1 --> Ok["Resolvable inside the arena"]
    C2 --> Gap["Unresolvable: runs/ was never\ncopied in -- by design, not oversight"]
    Gap --> Why["A candidate must never reach the\nevidence that judges it (B-G1, section 2) --\nso verification of the evidence\nhappens outside this loop entirely"]
```
