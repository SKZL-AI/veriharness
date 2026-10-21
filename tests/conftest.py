"""Shared fixtures, and one fail-closed rule about evidence the export withholds.

This project publishes its tools and its results and deliberately does not
publish some of the material those tools measure. A test that measures one of
those has nothing to measure in a published clone, and must say so and skip.

The dangerous version of that is `path missing -> skip`. Under it, an export
that **lost** a path it was supposed to ship would turn the affected tests
green-by-skipping instead of red, and the check would be fail-open exactly
where it is meant to be strict. So a skip is earned, not assumed, and all of
these have to hold:

1. the path really is absent;
2. `EXPORT_MANIFEST.json` is present and parses;
3. that path -- or, for a directory, the files under it -- is in the manifest;
4. the manifest says `EXCLUDE`, with the rule this kind of artifact is
   expected to carry;
5. the skip names the artifact and why it is withheld, in this artifact's own
   words rather than a generic "evidence missing".

Anything else fails:

    missing + expected EXCLUDE rule   -> skip, environment gap
    missing + INCLUDE                 -> fail, the export lost a public path
    missing + unclassified            -> fail, nobody decided about it
    missing + manifest absent/broken  -> fail, nothing authorises the skip
    missing + a different EXCLUDE rule-> fail, withheld for a reason that is
                                         not the one this test expects
    present                           -> no skip; the test runs

`unclassified` failing matters: it is the manifest's conservative default, the
answer it gives when no rule claimed a path. An undecided path is not a
declared withholding and must never buy a green test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "EXPORT_MANIFEST.json"


class Withheld:
    """One kind of artifact the export withholds, and why."""

    def __init__(self, path: str, rule_: str, reason: str) -> None:
        self.path, self.rule_, self.reason = path, rule_, reason


#: Each kind names its own reason. "evidence missing" would be true of all of
#: them and useful about none.
HIDDEN_FIXTURES = Withheld(
    "dogfood/benchmark/tasks", "internal-working-document",
    "hidden benchmark fixtures intentionally withheld to preserve future "
    "benchmark secrecy -- the protocol's premise is that the hidden suite is "
    "the verdict and no arm sees it")
RUN_EVIDENCE_V2 = Withheld(
    "dogfood/benchmark/results-v2", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS_v2.md carries "
    "what they measured")
RUN_EVIDENCE_V1 = Withheld(
    "dogfood/benchmark/results", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS.md carries "
    "what they measured")
CONFINEMENT_EVIDENCE = Withheld(
    "dogfood/planner-confinement", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/EVIDENCE_INDEX.md publishes the "
    "tree digests and every path-free field")
SELFHOST_EVIDENCE = Withheld(
    "dogfood/selfhost-e2e", "internal-working-document",
    "excluded because the run tree records the worktree each dispatch ran in; "
    "dogfood/ATTRIBUTION.json names what it established")
RUN_EVIDENCE_V3 = Withheld(
    "dogfood/benchmark/results-v3", "internal-working-document",
    "excluded because raw receipts contain machine-local paths and immutable "
    "digests that redaction would break; docs/BENCHMARK_RESULTS_v3.md carries "
    "what they measured")

#: V3.3 P0. The requirement register is a reading of the operator's planning
#: package, which is not published: the plan is the customer's, the tree is
#: the product's. A published clone therefore cannot derive the capability
#: matrix, and must say so rather than pretend the matrix is empty.
PROGRAM_REGISTER = Withheld(
    "program/v3_3/REQUIREMENTS.json", "unclassified",
    "the V3.3 requirement register is derived from an internal planning "
    "package and stays in the internal tree; without it a capability matrix "
    "cannot be derived here, and an empty matrix would read as a repository "
    "with no requirements")


def _manifest() -> dict[str, tuple[str, str]]:
    """path -> (decision, rule). Raises if the manifest cannot authorise anything."""
    if not MANIFEST.is_file():
        pytest.fail(
            f"{MANIFEST.name} is absent, so nothing authorises an "
            f"environment-gap skip. A missing path with no manifest is a "
            f"broken tree, not a withheld artifact.")
    try:
        data_ = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except ValueError as exc:
        pytest.fail(f"{MANIFEST.name} does not parse ({exc}), so nothing "
                    f"authorises an environment-gap skip")
    entries = data_.get("entries") if isinstance(data_, dict) else data_
    return {e["path"]: (e["decision"], e["rule"]) for e in entries or []}


def needs_evidence(art: Withheld) -> None:
    """Skip only if the manifest declares this artifact withheld, for its
    expected reason. Fail in every other absent case; do nothing if present."""
    target = ROOT / art.path
    if target.exists():
        return

    entries = _manifest()
    affected = {p: v for p, v in entries.items()
                 if p == art.path or p.startswith(art.path + "/")}
    if not affected:
        pytest.fail(
            f"{art.path} is absent and the manifest does not classify it. An "
            f"undecided path is not a declared withholding, so this is a "
            f"broken tree rather than an environment gap.")

    included_ = sorted(p for p, (d, _) in affected.items() if d == "INCLUDE")
    if included_:
        pytest.fail(
            f"{art.path} is absent but the manifest says INCLUDE for "
            f"{len(included_)} path(s) under it (e.g. "
            f"{included_[0]}). The export lost a path it was supposed to "
            f"ship; that is a failure, not a gap.")

    wrong_ = sorted({r for _, (d, r) in affected.items()
                      if d == "EXCLUDE" and r != art.rule_})
    if wrong_:
        pytest.fail(
            f"{art.path} is absent and excluded, but under rule(s) "
            f"{', '.join(wrong_)} rather than the expected {art.rule_!r}. "
            f"A skip may only rest on the reason this test expects.")

    pytest.skip(f"{art.path} is not in this tree -- {art.reason}")


def needs_parked_predecessor(baseline: str) -> None:
    """The superseded-artifact case: a `<name>.v<stamp>` sibling of `basis`."""
    folder = (ROOT / baseline).parent
    if folder.is_dir() and sorted(folder.glob(Path(baseline).name + ".v*")):
        return
    entries = _manifest()
    parked = {p: v for p, v in entries.items()
               if p.startswith(baseline + ".v")}
    if not parked:
        pytest.fail(
            f"no parked predecessor of {baseline} and none in the manifest "
            f"either: an undecided absence is not a declared withholding")
    wrong = sorted({r for _, (d, r) in parked.items()
                     if d != "EXCLUDE" or r != "parked-predecessor"})
    if wrong:
        pytest.fail(
            f"a parked predecessor of {baseline} is classified {wrong} rather "
            f"than EXCLUDE/parked-predecessor")
    pytest.skip(
        f"no parked predecessor of {baseline} in this tree -- excluded "
        f"historical predecessor; the binding registration is published, and "
        f"that the two carry different blobs is checkable from git in the "
        f"repository that produced them")


@pytest.fixture
def benchmark_tasks() -> Path:
    needs_evidence(HIDDEN_FIXTURES)
    return ROOT / HIDDEN_FIXTURES.path


# --------------------------------------------------------------------------- #
# Tests of the development tree that read records the export withholds
# --------------------------------------------------------------------------- #

#: Added 2026-10-14. These tests were written in the development repository and read records that the export
#: withholds: the requirement register and what is derived from it, the pinned scope of the planning package, one acceptance
#: record and the preserved reviews of the P2 gate under `dogfood/`, one runbook. In a published clone they failed with a missing
#: file from the day the first of them was exported, and the public CI was red on every push behind a collection error that hid
#: them. The table names, per test, what it needs; `needs_evidence` then does what it does for the benchmark fixtures: it skips
#: only when the path is absent **and** the manifest declares it withheld under the rule named here, and fails in every other
#: absent case. In the development tree nothing is absent, so nothing is skipped. A test that reads a withheld record and is not
#: listed fails in a published clone, which is the intended pressure: it is a new test to classify.
PROGRAM_MATRIX = Withheld(
    "program/v3_3/VERIHARNESS_CAPABILITY_MATRIX.json", "unclassified",
    "the committed capability matrix is derived from the requirement register, which stays in the internal tree")
PROGRAM_SCOPE = Withheld(
    "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json", "unclassified",
    "the pinned scope is a reading of the operator's planning package, which is not published")
P1_16_ACCEPTANCE = Withheld(
    "dogfood/p1-16/P1_16_ACCEPTANCE.json", "internal-working-document",
    "an acceptance record of the development repository's working tree")
P2_GATE_REVIEWS = Withheld(
    "dogfood/p2-gate", "internal-working-document",
    "the independent reviews of the P2 gate are working records of the development repository; the closure record names them")
STORAGE_RUNBOOK = Withheld(
    "docs/STORAGE_HOST_RUNBOOK.md", "foreign-subject",
    "the runbook names a path on the machine it was written for and is withheld for that reason")

NEEDS_WITHHELD: dict[str, tuple[Withheld, ...]] = {
    "tests/test_acceptance_record.py::test_an_unheld_run_re_derives_without_the_new_keys": (P1_16_ACCEPTANCE,),
    "tests/test_budget_invariant.py::test_the_register_binds_the_invariant_to_p3_04_with_evidence_that_exists_in_this_tree": (PROGRAM_REGISTER,),
    "tests/test_build_plan_closure.py::test_the_committed_closure_record_of_p2_is_well_formed": (P2_GATE_REVIEWS,),
    "tests/test_capability_caps.py::test_exactly_these_plan_items_are_base_exempt": (PROGRAM_SCOPE,),
    "tests/test_capability_caps.py::test_the_live_register_has_no_ambiguous_probe": (PROGRAM_MATRIX,),
    "tests/test_capacity_admission.py::test_the_register_reads_p3_17_proven_and_p3_16_and_p3_19_partial_with_what_is_left_on_this_tree": (PROGRAM_REGISTER,),
    "tests/test_conflict_closure.py::test_the_register_names_the_probe_this_slice_earns_and_keeps_the_earlier_ones": (PROGRAM_REGISTER,),
    "tests/test_ddapply.py::test_the_register_row_names_the_function_a_person_applies_with_and_keeps_its_history": (PROGRAM_REGISTER,),
    "tests/test_ddverify.py::test_the_register_row_names_the_function_the_pass_calls_and_keeps_its_equivalent_and_history": (PROGRAM_REGISTER,),
    "tests/test_derivedreads.py::test_the_register_keeps_the_probe_slice_2_earned_by_addition": (PROGRAM_REGISTER,),
    "tests/test_dispatch_budget_knob.py::test_the_register_reads_p3_22_p3_23_and_p3_24_proven_on_this_tree": (PROGRAM_REGISTER,),
    "tests/test_docs_numbers.py::test_the_committed_table_carries_the_v10d_sentence_the_tool_derives": (PROGRAM_MATRIX,),
    "tests/test_durability_reboot.py::CrashAndRebootRequirementTest::test_p2_11_is_a_requirement_of_the_record_bound_to_its_plan_item": (PROGRAM_REGISTER,),
    "tests/test_external_executor.py::test_the_register_row_names_the_probe_the_matrix_reads": (PROGRAM_REGISTER,),
    "tests/test_limit_policies.py::test_the_register_row_of_this_slice_names_the_probe_the_matrix_reads": (PROGRAM_REGISTER,),
    "tests/test_missioncontrol_views.py::test_the_register_names_the_three_probes_the_views_earn": (PROGRAM_REGISTER,),
    "tests/test_parallelism_modes.py::test_the_register_reads_p3_25_and_p3_26_proven_on_this_tree": (PROGRAM_REGISTER,),
    "tests/test_park_failover.py::test_the_register_probe_of_p3_41_names_this_slice": (PROGRAM_REGISTER,),
    "tests/test_parked_continuation.py::test_the_register_row_of_this_slice_names_the_probe_the_matrix_reads": (PROGRAM_REGISTER,),
    "tests/test_parked_continuation.py::test_the_register_rows_of_the_resolver_slice_name_the_probes_the_matrix_reads": (PROGRAM_REGISTER,),
    "tests/test_preflight_identity.py::test_the_register_row_names_the_probe_and_keeps_the_history_of_its_probes": (PROGRAM_REGISTER,),
    "tests/test_program_scope.py::test_the_real_register_covers_the_real_acceptance_path": (PROGRAM_REGISTER,),
    "tests/test_programme_supervisor_drift.py::test_legacy_capsule_digest_stable_on_the_preserved_reviews": (P2_GATE_REVIEWS,),
    "tests/test_programme_supervisor_phase_seal.py::test_the_preserved_capsule_of_the_sealing_review_keeps_its_digest": (P2_GATE_REVIEWS,),
    "tests/test_readiness_supervisor_row.py::test_a_brief_for_another_phase_does_not_close_p2": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_capsule_from_a_different_state_does_not_satisfy_the_row": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_carried_finding_the_snapshot_cannot_hold_is_left_out": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_complete_current_p2_review_passes": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_critical_no_action_is_not_a_finding_of_nothing": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_hand_forged_complete_is_refused_instead_of_published": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_multi_line_exception_becomes_one_bounded_line": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_named_reviewer_still_cannot_discharge_a_phase_with_one_character": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_one_character_rejection_does_not_discharge_a_phase_finding": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_pipe_in_an_exception_message_cannot_break_the_table": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_reasoned_rejection_by_someone_else_still_does_not_settle_it": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_record_carrying_a_finding_about_an_unknown_item_is_refused": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_register_less_tree_is_not_run_rather_than_fail": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_register_nothing_vouches_for_is_named": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_review_root_outside_the_repository_fails_the_row": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_review_that_found_nothing_over_the_whole_phase_passes": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_review_with_nothing_preserved_does_not_satisfy_the_row": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_a_zero_finding_review_of_another_phase_does_not_close_p2": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_an_applied_finding_whose_evidence_names_nothing_stays_blocking": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_an_apply_never_settles_a_phase_level_finding_even_with_evidence": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_an_emptied_validation_failures_array_does_not_make_a_record_complete": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_an_unstamped_directory_cannot_shadow_a_preserved_review": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_deleting_the_inconvenient_findings_is_caught_by_the_digest": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_every_computed_field_is_compared_not_just_the_first_three": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_no_review_record_is_not_run_and_is_not_a_pass": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_board_names_four_unnamed_answers_and_then_counts_the_rest": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_board_says_why_a_no_action_that_names_no_raiser_resolved_nothing": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_brief_counts_the_phase_by_the_field_the_selection_uses": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_brief_shows_the_supervisor_every_open_finding": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_canonical_brief_is_the_one_the_cli_prints": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_capsule_limits_are_recorded_so_a_rebuild_is_exact": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_cell_names_the_review_it_read_on_every_outcome": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_control_is_a_no_action_whose_severity_agrees_with_it": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_defendant_cannot_sign_off_its_own_phase_by_respelling_its_name": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_detail_line_names_the_cause_and_not_only_the_verdict": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_matrix_never_says_a_module_is_unused_only_that_nothing_names_it": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_party_under_review_cannot_sign_off_its_own_phase": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_row_asks_the_loader_and_not_the_file": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_row_re_validates_the_findings_and_not_only_their_accounting": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_the_row_reports_a_stale_review_as_stale": (PROGRAM_REGISTER,),
    "tests/test_readiness_supervisor_row.py::test_tied_newest_reviews_fail_the_row_and_name_the_ambiguity": (PROGRAM_REGISTER,),
    "tests/test_run_lease_consult.py::test_the_register_probes_the_readers_of_the_run_lease": (PROGRAM_REGISTER,),
    "tests/test_storage_host.py::test_never_runs_a_host_command": (STORAGE_RUNBOOK,),
}

#: Tests that drive a real Herdr session (`HERDR_ENV=1` and the `herdr` command). The workflow does not stand one up, deliberately
#: (see `.github/workflows/ci.yml`: a mock would test the mock). They skip where there is none and run where there is.
NEEDS_HERDR: frozenset[str] = frozenset({
    "tests/test_governance.py::test_the_run_command_accepts_and_pins_a_contract_file",
})


@pytest.fixture(autouse=True)
def _records_the_export_withholds(request):
    key = request.node.nodeid.split("[", 1)[0]
    for art in NEEDS_WITHHELD.get(key, ()):
        needs_evidence(art)
    if key in NEEDS_HERDR:
        import shutil
        if os.environ.get("HERDR_ENV") != "1" or shutil.which("herdr") is None:
            pytest.skip("needs a real Herdr session (HERDR_ENV=1 and the herdr command); this environment has none")


# --------------------------------------------------------------------------- #
# The test boundary: a guard regression must fail a test, never run a command
# --------------------------------------------------------------------------- #

#: Payloads the test suite uses to exercise the guard. They are here so that,
#: if the guard ever stops refusing one of them, the suite fails instead of
#: executing it. 2026-09-25: an operator-side relaxation silently weakened the
#: product guard, and guard tests really launched their payload (among them an
#: endless GPU query loop, left orphaned on the machine for over an hour).
#: `hoh-never-execute-sentinel` is the neutral payload for tests in which this
#: stub is meant to be the only barrier.
NEVER_EXECUTE_IN_TESTS = ("nvidia-smi", "hoh-never-execute-sentinel")


def _executes(script: str, token: str) -> bool:
    """Fail closed: `token` as a word anywhere in an executed script counts as
    executing it -- `eval T`, `bash -c "T"`, `sudo T`, `xargs T`, `! T` and
    every other form alike (review round 3: a command-position matcher missed
    eleven of them). The one exception is a single command, with no shell
    separator or substitution, whose first word only prints or searches text
    (`grep T file`, `echo '... T ...'`): a mention, not an execution (review
    round 2: plain substring matching failed those falsely)."""
    import re

    if not re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", script):
        return False
    words = script.strip().split()
    single = re.search(r"[;&|`\n<>]|\$\(", script) is None
    mention_only = {"grep", "egrep", "fgrep", "echo", "printf"}  # not rg: `rg --pre T` runs T
    if single and words and os.path.basename(words[0]) in mention_only:
        return False
    return True


def _executed_scripts(args, executable=None) -> list[str]:
    """What a spawn would execute: argv[0] (and `executable=`), the rest of
    argv as one line, and every script handed to a shell with `-c`, each on
    its own. A string is a shell command line."""
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)] + ([os.fsdecode(executable)] if executable else [])
    argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]
    shells = {"sh", "bash", "dash", "zsh", "ksh"}
    script_at: set[int] = set()
    for i, a in enumerate(argv):
        if os.path.basename(a) not in shells:
            continue
        # The shell's own options up to the first operand: `-c` (or a cluster
        # such as `-lc`) makes the next argument the script. Options that take
        # a value (`-O extglob`, `-o pipefail`) and long options are skipped.
        j = i + 1
        while j < len(argv) and argv[j].startswith("-"):
            opt = argv[j]
            if not opt.startswith("--") and "c" in opt[1:] and j + 1 < len(argv):
                script_at.add(j + 1)
                break
            j += 2 if opt in ("-O", "+O", "-o", "+o") else 1
    # The whole argv as one line as well -- minus the `-c` scripts, which are
    # checked on their own: a payload later in a plain argv list (`["env", T]`,
    # `["timeout", "5", T]`, `bwrap ... -- T`) is executed too (review round
    # 4). Fail closed: a role prompt naming a payload, passed as argv, is
    # refused as well -- no test spawns one.
    rest = " ".join(a for k, a in enumerate(argv) if k not in script_at)
    return (argv[:1] + [rest] + [argv[k] for k in sorted(script_at)]
            + ([os.fsdecode(executable)] if executable else []))


def _refuse_if_listed(args, attempts: list[str], executable=None) -> None:
    scripts = _executed_scripts(args, executable)
    hit = [p for p in NEVER_EXECUTE_IN_TESTS
           if any(_executes(s, p) for s in scripts)]
    if hit:
        text = " | ".join(scripts)
        attempts.append(text)
        # pytest.fail raises a BaseException, so `except OSError` /
        # `except Exception` in the code under test cannot turn it into a
        # receipt: the test fails at this line and nothing is started.
        pytest.fail(
            f"guard regression: code under test tried to EXECUTE a command the "
            f"policy must refuse ({hit[0]!r}); refused, not started: {text!r}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _no_refused_command_is_ever_executed(monkeypatch):
    """A stub in front of the process-spawn APIs of the pytest process.

    Covered: `subprocess.Popen` itself (replaced by a subclass, so isinstance
    and subclassing keep working), hence `subprocess.run`, `check_output` and
    friends however they are imported, including `executable=`; `os.system`;
    `os.posix_spawn[p]`; `os.spawn*`; `os.exec*`. **Not** covered: `os.fork`
    followed by exec, including `pty.spawn` -- the refusal would be raised in
    the forked child, which would then run the rest of the session, while the
    parent sees no failure (review round 3); nothing in src/, tests/ or tools/
    forks today -- and child Python processes, which do not inherit this
    fixture (both tracked in the dogfood ledger, O235). This is a safety net
    for the suite's own guard payloads, not a classifier: see `_executes`.
    """
    import subprocess as _subprocess

    attempts: list[str] = []
    real_popen = _subprocess.Popen

    class _GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            _refuse_if_listed(args, attempts, kw.get("executable"))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(_subprocess, "Popen", _GuardedPopen)
    real_system = os.system
    monkeypatch.setattr(os, "system",
                        lambda cmd: (_refuse_if_listed(cmd, attempts), real_system(cmd))[1])
    def _wrap_path_argv(real):          # f(path, argv, ...) -- posix_spawn*, execv*
        def wrapped(path, argv, *a, **kw):
            _refuse_if_listed([*argv], attempts, path)
            return real(path, argv, *a, **kw)
        return wrapped

    def _wrap_path_varargs(real):       # f(path, arg0, arg1, ...) -- execl*
        def wrapped(path, *args):
            _refuse_if_listed([a for a in args if isinstance(a, (str, bytes, os.PathLike))],
                              attempts, path)
            return real(path, *args)
        return wrapped

    def _wrap_spawn(real, varargs):     # f(mode, path, ...) -- spawnv*/spawnl*
        def wrapped(mode, path, *rest):
            argv = rest if varargs else rest[0]
            _refuse_if_listed([a for a in argv if isinstance(a, (str, bytes, os.PathLike))],
                              attempts, path)
            return real(mode, path, *rest)
        return wrapped

    for name in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_path_argv(getattr(os, name)))
    for name in ("execl", "execle", "execlp", "execlpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_path_varargs(getattr(os, name)))
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_spawn(getattr(os, name), False))
    for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _wrap_spawn(getattr(os, name), True))
    try:                                   # modules that did `from subprocess import Popen`
        from hoh import runner as _runner
        if getattr(_runner, "Popen", None) is real_popen:
            monkeypatch.setattr(_runner, "Popen", _GuardedPopen)
    except Exception:
        pass
    yield attempts
    assert not attempts, f"refused commands reached the executor: {attempts}"


@pytest.fixture(autouse=True)
def _the_default_preflight_is_off_unless_the_module_is_about_it(request):
    """P3-11 slice 3: a project that declares nothing is gated by the product default
    (`hoh.preflightgate.default_requirement`: a report of this machine, for this repository and
    runs directory, at `<project>/preflight-report.json`). The tests of other features drive
    projects through the real launcher and have no such report and no business with one, so they
    run with the default switched off here -- by the same name the launcher uses. A module that
    is about the default (or about what it replaces) says so with
    `TESTS_THE_DEFAULT_PREFLIGHT = True` and gets the real one; nothing else changes.

    P3-11 slice 4: the same switch covers the refusal of a launcher the default cannot bind
    (`hoh.launcher.unbound_preflight_refusal`, asked by the governed drive): the stand-in
    launchers of other features have no `bind_routing` and no business with a waiver.

    A patch of its own, not the test's `monkeypatch`: a test that calls `monkeypatch.undo()` (to
    put a fault back) must not switch the default on in the middle of its drive."""
    if getattr(request.module, "TESTS_THE_DEFAULT_PREFLIGHT", False):
        yield
        return
    patch = pytest.MonkeyPatch()
    patch.setattr("hoh.launcher.default_requirement", lambda project_dir: None, raising=False)
    patch.setattr("hoh.launcher.unbound_preflight_refusal", lambda launcher, project_dir: None,
                  raising=False)
    try:
        yield
    finally:
        patch.undo()
