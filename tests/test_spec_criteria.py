"""The criteria a specification declares reach the frozen plan as written, or the plan is refused.

Nothing compared the planner's `acceptance_checks` with the specification: a
planner closed a quoted command with a stray backslash-quote, added a
`--deselect`, wrapped criteria in `python3 -c`, or left some out, and the
controller accepted the altered plan. `spec_criteria` reads one fenced block of
the specification and the controller refuses a plan that does not carry every
criterion of it verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hoh import roles
from hoh.contracts import AcceptanceCheck, Budgets, DevelopmentPlan, Role
from hoh.controller import DispatchError
from hoh.evidence import EvidenceBundle
from hoh.spec_criteria import (
    Criterion,
    CriterionFidelityError,
    CriteriaBlockError,
    declared_criteria,
    fidelity_findings,
)

# The fixtures and the fake dispatcher of the controller tests are the ones used here.
from test_controller import FakeDispatcher, build, plan_for, qa_pass, repo, spec  # noqa: F401

FENCE = "`" * 3
INCIDENT = 'python3 -B -c "import sys; sys.exit(0 if 1 + 1 == 2 else 1)"'


def block(*lines: str) -> str:
    return "\n".join([f"{FENCE}criteria", *lines, FENCE])


def spec_with(*lines: str) -> str:
    return f"# Spec\n\nProse before.\n\n{block(*lines)}\n\nProse after.\n"


def make_checks(**commands: str) -> list[AcceptanceCheck]:
    return [AcceptanceCheck(check_id=cid, description="d", command=cmd)
            for cid, cmd in commands.items()]


# --- Reading the block ----------------------------------------------------- #


def test_a_declared_block_gives_its_criteria_verbatim():
    commands = {
        "K1": 'python3 -B -m pytest -q -p no:cacheprovider tests/test_a.py::test_x -k "a or b"',
        "K2": INCIDENT,
        "K3": r"python3 -B -c 'print(\"x\")' --deselect tests/t.py::test_y  $HOME {ARENA}",
        "R.1-b_2": "true",
    }
    text = spec_with(f"K1 {commands['K1']}", "", f"K2    {commands['K2']}   ",
                     f"K3 {commands['K3']}", f"R.1-b_2 {commands['R.1-b_2']}")
    expected = tuple(Criterion(cid, cmd) for cid, cmd in commands.items())
    assert declared_criteria(text) == expected
    assert declared_criteria(text.replace("\n", "\r\n")) == expected


def test_a_specification_without_a_block_declares_nothing():
    assert declared_criteria("") == ()
    assert declared_criteria("# Spec\n\nK1 python3 -B -m pytest\n") == ()
    other = f"{FENCE}python\nK1 python3 -B -m pytest\n{FENCE}\n\n{FENCE}\nK2 true\n{FENCE}\n"
    assert declared_criteria(other) == ()


@pytest.mark.parametrize("text", [
    pytest.param("`" * 4 + "\n" + block("K1 true") + "\n" + "`" * 4 + "\n",
                 id="an-example-inside-a-longer-fence"),
    pytest.param("~~~criteria\nK1 true\n~~~\n", id="a-tilde-fence"),
    pytest.param(f"- item\n\n   {FENCE}criteria\n   K1 true\n   {FENCE}\n", id="an-indented-fence"),
    pytest.param(f"{FENCE}criteria-example\nK1 true\n{FENCE}\n", id="another-info-string"),
    pytest.param(f"{FENCE}Criteria\nK1 true\n{FENCE}\n", id="another-case"),
    pytest.param(f"{FENCE}criteria{FENCE} K1 true\n", id="inline-code"),
])
def test_only_a_criteria_fence_at_the_start_of_a_line_is_a_block(text):
    assert declared_criteria(text) == ()


REAL = block("K1 true")


@pytest.mark.parametrize("before", [
    pytest.param(f"{FENCE}criteria{FENCE} K2 false\n", id="inline-code-that-names-criteria"),
    pytest.param("`" * 4 + "\n" + block("K2 false") + "\n" + "`" * 4 + "\n", id="a-longer-example-fence"),
    pytest.param(f"{FENCE}sh\n    {FENCE}\nK2 false\n{FENCE}\n", id="an-indented-line-does-not-close-a-fence"),
    pytest.param(f"{FENCE}sh\n{FENCE} not a closer\nK2 false\n{FENCE}\n", id="a-fence-line-with-words-does-not-close-a-fence"),
    pytest.param("~~~\n" + FENCE + "\nK2 false\n~~~\n", id="a-tilde-fence-holding-backticks"),
])
def test_a_real_block_after_a_fence_like_line_is_still_found(before):
    """A fence the reader misjudges swallows the real block that follows it: the specification
    would then read as 'declares nothing' -- the quiet failure this reader must not have."""
    assert declared_criteria(f"# Spec\n\n{before}\n{REAL}\n") == (Criterion("K1", "true"),)


def test_a_criteria_block_after_other_fences_is_found():
    text = f"{FENCE}\nK9 nope\n{FENCE}\n\n" + block("K1 true") + f"\n\n{FENCE}sh\nK8 nope\n{FENCE}\n"
    assert declared_criteria(text) == (Criterion("K1", "true"),)


@pytest.mark.parametrize("text", [
    pytest.param(block("K1 true") + "\n\n" + block("K2 true"), id="two-blocks"),
    pytest.param(f"{FENCE}criteria\nK1 true\n", id="not-closed"),
    pytest.param(block(), id="empty"),
    pytest.param(block("", "  "), id="blank-lines-only"),
    pytest.param(block("K1 true", "K1 false"), id="duplicate-id"),
    pytest.param(block("K1!x true"), id="invalid-id"),
    pytest.param(block("K" * 65 + " true"), id="id-too-long"),
    pytest.param(block("K1"), id="id-without-a-command"),
    pytest.param(block("K1    "), id="id-with-blanks-only"),
    pytest.param(block(" K1 true"), id="indented-line"),
    pytest.param(block("K1\ttrue"), id="tab-separator"),
    pytest.param(block("K1 true\x00"), id="control-character"),
    pytest.param(block("K1 true\rK2 true"), id="lone-carriage-return"),
    pytest.param(block("K1 true\u00a0"), id="trailing-no-break-space"),
    pytest.param(block("K1 true\u2028"), id="trailing-line-separator"),
    pytest.param(block("K1 true\u0085"), id="trailing-next-line"),
    pytest.param(block("K1 \u3000true"), id="leading-ideographic-space"),
])
def test_a_block_that_cannot_be_read_is_refused_by_name(text):
    with pytest.raises(CriteriaBlockError, match="SPEC_CRITERIA_UNREADABLE"):
        declared_criteria(text)


# --- Comparing a plan with the block --------------------------------------- #

K1 = 'python3 -B -m pytest -q -p no:cacheprovider tests/test_a.py::test_x -k "a or b"'
K2 = "python3 -B -m pytest -q -p no:cacheprovider tests/test_b.py"
DECLARED = (Criterion("K1", K1), Criterion("K2", K2))


def plan_of(checks: list[AcceptanceCheck]) -> DevelopmentPlan:
    return DevelopmentPlan(
        run_id="r", iteration=1, base_candidate_id="b", spec_digest="d",
        objective="o", targets=["t"], acceptance_checks=checks)


def test_a_plan_with_every_criterion_as_written_is_faithful():
    checks = make_checks(K2=K2, K1=K1 + "  \n", G1="python3 -B -m pytest tests/test_c.py")
    assert fidelity_findings(DECLARED, checks) == []
    from hoh.spec_criteria import assert_faithful
    assert_faithful(DECLARED, plan_of(checks))


def test_the_plan_schema_trims_a_command_so_what_is_compared_is_what_runs():
    """`Strict` has `str_strip_whitespace`: a trailing U+00A0 never reaches the comparison or the
    runner. That is why a declared command that ends in one is refused when the block is read."""
    assert AcceptanceCheck(check_id="K1", description="d", command=K1 + "\u00a0\u2028").command == K1
    assert fidelity_findings(DECLARED, make_checks(K1=K1 + "\u00a0", K2=K2 + "\u0085")) == []


@pytest.mark.parametrize("tail", ["\u00a0", "\u2028", "\u0085", "\u3000"])
def test_only_ascii_blanks_around_a_command_are_not_a_change(tail):
    """The comparison itself trims ASCII blanks and nothing else, whatever the object it is given."""
    def check(cmd):
        return SimpleNamespace(check_id="K1", command=cmd, expect_exit=0)

    assert fidelity_findings(DECLARED[:1], [check(" \t" + K1 + " \t\r\n")]) == []
    for altered in (K1 + tail, tail + K1):
        findings = fidelity_findings(DECLARED[:1], [check(altered)])
        assert [f.split(":")[0] for f in findings] == ["K1"] and "differs" in findings[0]


ALTERATIONS = {
    "stray-trailing-quote": (K1 + '\\"', "K1"),
    "an-appended-deselect": (K1 + " --deselect tests/test_a.py::test_y", "K1"),
    "a-prepended-option": (K1.replace("-q ", "-q -x "), "K1"),
    "a-python-wrapper": ("python3 -B -c \"import pytest, sys; sys.exit(pytest.main(['-q', "
                         "'tests/test_a.py::test_x', '-k', 'a or b']))\"", "K1"),
    "an-interior-blank": (K1.replace("-q -p", "-q  -p"), "K1"),
    "another-criteria-command": (K2, "K1"),
    "the-command-of-true": ("true", "K1"),
}


@pytest.mark.parametrize("name", sorted(ALTERATIONS))
def test_every_way_a_planner_altered_a_criterion_is_refused(name):
    altered, cid = ALTERATIONS[name]
    checks = make_checks(**{cid: altered, "K2": K2})
    assert [f.split(":")[0] for f in fidelity_findings(DECLARED, checks)] == [cid]
    from hoh.spec_criteria import assert_faithful
    with pytest.raises(CriterionFidelityError) as raised:
        assert_faithful(DECLARED, plan_of(checks))
    message = str(raised.value)
    assert message.startswith("CRITERION_FIDELITY: ")
    assert f"{cid}: the command differs" in message and K1 in message
    assert isinstance(raised.value, roles.RoleOutputError)


def test_a_dropped_criterion_is_refused_even_when_a_deselecting_copy_stands_in():
    checks = make_checks(K2=K2, P1b=K1 + " --deselect tests/test_a.py::test_y")
    assert fidelity_findings(DECLARED, checks) == [f"K1: missing from the plan "
                                                  f"(the specification's command is: {K1})"]
    assert [f.split(":")[0] for f in fidelity_findings(DECLARED, [])] == ["K1", "K2"]


def test_a_criterion_expects_exit_zero():
    checks = make_checks(K1=K1, K2=K2)
    checks[0] = AcceptanceCheck(check_id="K1", description="d", command=K1,
                                expect_exit=1, expect_reason="a relabelled success")
    assert fidelity_findings(DECLARED, checks) == [
        "K1: expect_exit is 1, a criterion expects 0"]


def test_a_criterion_validated_in_an_earlier_iteration_needs_no_entry():
    assert fidelity_findings(DECLARED, make_checks(K2=K2), frozen={"K1"}) == []
    assert fidelity_findings(DECLARED, [], frozen={"K1"}) == [
        f"K2: missing from the plan (the specification's command is: {K2})"]
    # The plan's version of a frozen id is discarded by the controller, which says so.
    assert fidelity_findings(DECLARED, make_checks(K1="true", K2=K2), frozen={"K1"}) == []


# --- Through the real controller ------------------------------------------- #

MARKER_CHECK = f"test -f {FakeDispatcher.MARKER}"


class Recording(FakeDispatcher):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.planner_prompts: list[str] = []

    def dispatch(self, role, prompt, *, state):
        if role is Role.PLANNER:
            self.planner_prompts.append(prompt)
        return super().dispatch(role, prompt, state=state)


def write_spec(path, *lines: str) -> None:
    path.write_text(spec_with(*lines) if lines else "# Spec\nadd(a,b) must add.\n",
                    encoding="utf-8")


def test_a_wrapped_criterion_is_sent_back_once_with_the_command_and_then_accepted(
        tmp_path, repo, spec):  # noqa: F811
    write_spec(spec, f"K1 {MARKER_CHECK}")
    answers = iter([f"sh -c '{MARKER_CHECK}'", MARKER_CHECK])
    d = Recording(repo=repo, plan_json=lambda s: plan_for(s, command=next(answers)),
                  qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert out.accepted, out.reason
    assert "NOT_RUN" not in out.reason, "a compared plan is not a NOT_RUN acceptance"
    assert state.usage.schema_repairs == 1
    assert len(d.planner_prompts) == 2
    repair = d.planner_prompts[1]
    assert "CRITERION_FIDELITY" in repair and f"is: {MARKER_CHECK}" in repair
    assert "Nothing about the content should change" not in repair
    assert any("schema repair for planner: CRITERION_FIDELITY" in h for h in state.history)


def test_a_plan_that_still_alters_a_criterion_after_the_repair_ends_the_iteration(
        tmp_path, repo, spec):  # noqa: F811
    write_spec(spec, f"K1 {MARKER_CHECK}")
    d = Recording(repo=repo, plan_json=lambda s: plan_for(s, command=MARKER_CHECK + " --x"),
                  qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    with pytest.raises(DispatchError, match="CRITERION_FIDELITY"):
        ctrl.run_iteration(state)

    assert d.calls == [Role.PLANNER, Role.PLANNER], "no developer, no QA"


def test_a_specification_without_a_block_is_not_compared_and_says_so(
        tmp_path, repo, spec):  # noqa: F811
    d = Recording(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    out = ctrl.run_iteration(state)

    assert out.accepted, out.reason
    assert state.usage.schema_repairs == 0
    assert any("criterion fidelity NOT_RUN" in h for h in state.history)
    assert out.reason.startswith("all criteria demonstrated") and "criterion fidelity NOT_RUN" in out.reason, (
        "an acceptance without the comparison says so: an unrun check is NOT_RUN, never passed")


def test_without_a_repair_budget_the_first_unfaithful_plan_ends_the_iteration(
        tmp_path, repo, spec):  # noqa: F811
    write_spec(spec, f"K1 {MARKER_CHECK}")
    d = Recording(repo=repo, plan_json=lambda s: plan_for(s, command=MARKER_CHECK + " --x"),
                  qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)
    state.budgets = Budgets(max_iterations=5, max_schema_repairs=0)

    with pytest.raises(DispatchError, match="CRITERION_FIDELITY"):
        ctrl.run_iteration(state)

    assert d.calls == [Role.PLANNER], "one planner turn, no repair, nothing after"
    assert state.usage.schema_repairs == 0


def test_an_unreadable_block_ends_the_iteration_before_any_role_starts(
        tmp_path, repo, spec):  # noqa: F811
    spec.write_text(block("K1 true") + "\n" + block("K2 true") + "\n", encoding="utf-8")
    d = Recording(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, _ = build(tmp_path, repo, spec, d)

    with pytest.raises(CriteriaBlockError, match="SPEC_CRITERIA_UNREADABLE"):
        ctrl.run_iteration(state)

    assert d.calls == []


def test_a_criterion_the_run_already_validated_is_not_planned_again(
        tmp_path, repo, spec):  # noqa: F811
    write_spec(spec, f"K1 {MARKER_CHECK}")
    d = Recording(repo=repo, plan_json=plan_for, qa_json=qa_pass)
    ctrl, state, store = build(tmp_path, repo, spec, d)
    assert ctrl.run_iteration(state).accepted
    assert set(store.read_checks()) == {"K1"}

    d.plan_json = lambda s: plan_for(s, command="true", check_id="G2")      # no K1 in it
    ctrl.run_iteration(state)

    assert state.usage.schema_repairs == 0
    assert d.calls.count(Role.PLANNER) == 2 and d.calls.count(Role.DEVELOPER) == 2


# --- What the roles are told ----------------------------------------------- #


def planner_prompt_for(spec_text: str, iteration: int) -> str:
    return roles.planner_prompt(
        iteration=iteration, spec_text=spec_text,
        evidence=EvidenceBundle(run_id="r", iteration=iteration), repo_path=f"p{iteration}",
        base_candidate_id=f"c{iteration}", spec_digest="d", run_id="r")


def test_the_planner_is_told_to_copy_the_criteria_and_not_to_wrap_them():
    a, b = planner_prompt_for("Spec one", 1), planner_prompt_for("Another spec", 7)
    prefix = a[: next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)]
    told = " ".join(prefix.split())              # the wrapping of the lines is not the contract
    for phrase in (
        f"fenced block that opens with {FENCE}criteria",
        "copied character for character",
        '"expect_exit": 0',
        "wrapped (in a `python3 -c` call, for instance)",
        "`--deselect`",
        "a HOME and TMPDIR inside the run's own directories",
        "never change it.",
    ):
        assert phrase in told, phrase


def test_a_repair_for_the_fidelity_changes_the_commands_other_repairs_only_the_form():
    fidelity = roles.repair_prompt(Role.PLANNER, "CRITERION_FIDELITY: K1: the command differs",
                                   "{}")
    other = roles.repair_prompt(Role.PLANNER, "invalid JSON: x", "{}")
    assert "Nothing about the content should change" not in fidelity
    assert "copy each one exactly as the specification has it" in fidelity
    assert "Nothing about the content should change, only the form." in other
    assert "copy each one exactly" not in other
