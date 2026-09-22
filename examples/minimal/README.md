# A minimal HoH run -- and a real rejection

This directory has two purposes. `spec.md` is a specification small enough to
run through HoH in about five minutes -- an estimate, nobody has measured
this -- so you can watch the loop work end to end. `recorded-rejection/` is
not something you run -- it is a real rejection, copied unmodified from this
project's own dogfood evidence, so that you can see what "the loop refuses
your candidate" actually looks like before you invest the five minutes --
again an estimate, not measured -- yourself.

Both matter. A specification and a walkthrough could describe a HoH run that
always succeeds; that would be a demo, not evidence. HoH's whole reason to
exist is that it sometimes says no -- and a no is exactly what the recorded
rejection shows.

## Running the live example yourself

You need Herdr running (`HERDR_ENV=1`). Everything else is below, in order,
run from the root of your clone of this repository -- the spec path is
relative to it. `hoh` is installed with its test extra: this spec's K3 runs
`python3 -m pytest`, and a strict check runs under the interpreter HoH is
installed into, so that interpreter needs pytest. `HOH_RUNS` says where run
directories go; without it they go to `~/hoh/runs`. The doctor
(`tools/preflight.py`) ends with `profile demo: READY`, or names what is
missing before any quota is spent.

```sh
pip install -e ".[test]"        # into a fresh virtual environment
export HOH_RUNS=<a-directory-for-your-runs>
python3 tools/preflight.py --profile demo
hoh worktree --repo <your-project-checkout> --branch hoh-minimal-example
hoh start --repo <worktree-path> --spec examples/minimal/spec.md --run-id minimal-demo
hoh run minimal-demo --iterations 2 --until-accepted --planner claude --developer claude --qa claude \
    --isolation strict --approval-policy policy/role_approval.default.json --trust-worktree
hoh report minimal-demo
```

Because `greet.py` does not exist yet, the first iteration should be
straightforward: the planner writes acceptance checks for it, the developer
writes `greet.py` and `test_greet.py`, and QA re-executes the checks
independently before accepting. Not every run needs two iterations, and yours
may accept on the first one -- the point of `--iterations 2` is headroom, not
a promise of a rejection. `recorded-rejection/` below is where the guaranteed
rejection lives.

## What's in `recorded-rejection/`

These files are not a mockup. They are copied byte-for-byte from
`runs/d2/` in this repository's own dogfood history -- an internal HoH run
(`d2`, iteration 1, attempt 1) that this very project's own developers ran
against a real specification for governance documents (D2 in the dogfood
plan). `runs/` is not published: it is gitignored and excluded from every
export this project ships, so it is not there for a reader of this
repository to open and check against. `SOURCE.json` is the provenance
record instead -- it names exactly where each bundled file came from, and
the operator verified every file below byte-identical against that
(unpublished) run before bundling it here, so the claim is checkable
against `SOURCE.json` and the files themselves, not merely asserted.

| File | What it is |
|---|---|
| `qa_result.json` | The full QA role result for iteration 1, copied from runs/d2/results/qa-i1-a1.json (unpublished; see `SOURCE.json`). Nine criteria (`K1`-`K9`), each with a `PASS`/`FAIL`/`INCONCLUSIVE` outcome. |
| `receipts/d2-i1-a1-K7.json` | The runner's receipt for check `K7` on the candidate: exit code 1. |
| `receipts/d2-i1-a1-K7-basis.json` | The same check run against the *predecessor* state (the "control run" mentioned in the README): exit code 1 there too. |
| `state_history.txt` | The relevant lines from runs/d2/state.json's history (unpublished; see `SOURCE.json`), ending in `candidate rejected: not accepted: K7`. |
| `SOURCE.json` | Which run and iteration these files came from, so the citation is checkable, not asserted. |

### Why this is a rejection, not a success

Look at `qa_result.json`'s `verdicts` array: check `K7` ("the test suite stays
green," `python3 -m pytest -q`) has `"outcome": "FAIL"`. Everything else in
that plan passed, but HoH's acceptance rule (`RoleResult.accepted()`) requires
**every** criterion to pass -- one red preservation check is enough to reject
the whole candidate. `state_history.txt` records exactly that:
`candidate rejected: not accepted: K7`.

The two receipts tell you *why* the loop rejected rather than blamed the
candidate for something outside its scope: `K7.json` shows the test suite
failing on the developer's candidate, and `K7-basis.json` shows the **same**
two tests already failing on the state the candidate started from -- a
control run against the predecessor, taken before the candidate's own change
is judged. QA's own summary in `qa_result.json` draws the right conclusion:
the regression predates this candidate and sits in `src/hoh/workspace.py`,
which this run (`d2`, scoped to governance documents) was not permitted to
touch. The candidate is still rejected -- HoH does not accept "not my fault"
as a substitute for a green preservation suite -- but the evidence makes
clear who owns the fix, instead of leaving that as a guess.

This is what `docs/EVIDENCE_MODEL.md` means by "receipts, not self-reports":
nobody had to trust QA's word that `K7` failed, or the developer's word that
it wasn't their change that broke it. Both claims are checkable against the
receipts.

(As it happens, this exact rejection was resolved: runs/d2/state.json
(unpublished) shows iteration 2 of the same run was later accepted, once the
concurrent run that owned `src/hoh/` had fixed the regression. That is not
shown here -- this directory documents the rejection, not the recovery from
it.)
