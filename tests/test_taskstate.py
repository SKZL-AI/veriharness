"""Compact task state (V3.3 P2-13): a projection that cannot lie by omission.

One case per rule of `compact`, `stale_against` and `render`, each next to a
negative control that changes only the condition under test and shows the
verdict flipping. Most controls are the *quiet* side: a budget large enough
records no overrun, a state with nothing dropped is not truncated, a current
state is not stale. Without them, a compactor that always said
`budget_exceeded` or always set `truncated` would pass every loud case here.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hoh.contracts import Strict
from hoh.taskstate import (
    BUDGET_EXCEEDED,
    DROPPED,
    CompactState,
    Health,
    NodeLine,
    Omission,
    compact,
    render,
    stale_against,
)

AS_OF = "2026-09-24T00:00:00Z"


def _line(node_id, health=Health.READY, *, title=None, reason=None, waiting_on=None):
    if reason is None and health in (Health.FAILED, Health.BLOCKED):
        reason = f"{node_id} broke"
    return NodeLine(node_id=node_id, title=title or f"title of {node_id}", health=health,
                    attempts=1, last_event_at=AS_OF, blocked_reason=reason,
                    waiting_on=waiting_on)


def _compact(nodes, max_lines, **kw):
    kw.setdefault("program_id", "prog")
    kw.setdefault("generation", 7)
    kw.setdefault("as_of", AS_OF)
    return compact(nodes, max_lines=max_lines, **kw)


def _ids(state):
    return [l.node_id for l in state.lines]


def _mixed():
    return [_line("d1", Health.DONE), _line("r1", Health.READY), _line("r2", Health.READY),
            _line("u1", Health.RUNNING), _line("b1", Health.BLOCKED),
            _line("f1", Health.FAILED)]


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_health_has_exactly_the_specified_members_in_order():
    """A member added, renamed or reordered changes what a serialised state
    means; WAITING_FOR_DECISION must stay its own member, not a BLOCKED."""
    assert [h.value for h in Health] == [
        "READY", "RUNNING", "BLOCKED", "WAITING_FOR_DECISION", "DONE", "FAILED"]
    assert Health.WAITING_FOR_DECISION is not Health.BLOCKED


def test_models_sit_on_the_strict_base_and_refuse_unknown_fields():
    """Without Strict a misspelt `blocked_reson=` would be dropped silently and
    the line would read as a blocked node with no reason."""
    assert issubclass(NodeLine, Strict) and issubclass(CompactState, Strict)
    with pytest.raises(ValidationError):
        NodeLine(**_line("a").model_dump(), blocked_reson="x")
    # Control: the same fields without the extra one construct.
    NodeLine(**_line("a").model_dump())


@pytest.mark.parametrize("health", [Health.BLOCKED, Health.FAILED])
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_a_blocked_or_failed_line_without_a_reason_is_refused(health, reason):
    """A blocked node nobody can act on is exactly the omission this
    component exists to prevent; an empty or blank reason is no reason."""
    with pytest.raises(ValidationError, match="blocked_reason"):
        NodeLine(node_id="n", title="t", health=health, attempts=1,
                 last_event_at=None, blocked_reason=reason, waiting_on=None)


@pytest.mark.parametrize("health", [Health.BLOCKED, Health.FAILED])
def test_control_the_same_blocked_or_failed_line_with_a_reason_is_accepted(health):
    """Control: only the reason differs from the refused case. A validator that
    refused every BLOCKED line would pass the refusal test alone."""
    line = NodeLine(node_id="n", title="t", health=health, attempts=1,
                    last_event_at=None, blocked_reason="disk full", waiting_on=None)
    assert line.blocked_reason == "disk full"


def test_control_a_ready_line_needs_no_reason():
    """Control: the reason is required only where a node is stuck. A READY line
    that had to invent a reason would teach callers to write filler."""
    assert NodeLine(node_id="n", title="t", health=Health.READY, attempts=0,
                    last_event_at=None, blocked_reason=None, waiting_on=None)


def test_compact_state_is_frozen_so_the_projection_is_not_a_second_source_of_truth():
    """A compact state is a projection of the programme, not a record of it. If
    it could be edited, a reader could fix up a line or clear `truncated` and
    the edited copy would then disagree with the programme while looking just
    as authoritative -- a second source of truth nobody reconciles. So
    assignment, in-place mutation of `counts` and edits to a line all raise."""
    state = _compact(_mixed(), max_lines=2)
    with pytest.raises(ValidationError):
        state.truncated = False
    with pytest.raises(TypeError):
        state.counts[Health.FAILED] = 0
    with pytest.raises(TypeError):
        state.source_digests["x"] = "y"
    with pytest.raises(ValidationError):
        state.lines[0].health = Health.DONE
    assert isinstance(state.lines, tuple) and isinstance(state.omissions, tuple)


def test_a_hand_built_state_that_hides_a_node_is_refused():
    """Building a CompactState by hand is the other way to drop a line without
    an omission; the model itself checks lines + dropped add up to counts."""
    good = _compact(_mixed(), max_lines=10)
    body = dict(good)
    with pytest.raises(ValidationError, match="add up"):
        CompactState(**{**body, "lines": good.lines[:-1]})
    # Control: the unedited fields construct.
    assert CompactState(**body) == good


# --------------------------------------------------------------------------- #
# Rule 1 -- counts are over everything
# --------------------------------------------------------------------------- #


def test_counts_over_a_truncated_state_equal_counts_over_the_untruncated_one():
    """Counts computed after truncation would describe the excerpt, and a
    reader seeing three lines could not tell nine blocked nodes were hidden."""
    nodes = _mixed() + [_line(f"r{i}", Health.READY) for i in range(3, 9)]
    full = _compact(nodes, max_lines=100)
    cut = _compact(nodes, max_lines=3)
    assert cut.truncated and not full.truncated          # the condition differs ...
    assert dict(cut.counts) == dict(full.counts)         # ... the counts do not
    assert cut.counts[Health.READY] == 8 and sum(cut.counts.values()) == len(nodes)
    assert len(cut.lines) < sum(cut.counts.values())


def test_counts_name_every_health_including_zero():
    """A missing key reads as 'unknown', not as 'none'; every state is counted,
    zero included, so absence in counts cannot be mistaken for an omission."""
    state = _compact([_line("r1")], max_lines=1)
    assert set(state.counts) == set(Health)
    assert state.counts[Health.FAILED] == 0 and state.counts[Health.READY] == 1


# --------------------------------------------------------------------------- #
# Rule 2 -- attention first
# --------------------------------------------------------------------------- #


def test_a_failed_node_added_last_still_appears_first():
    """A summary that kept input or alphabetical order would bury a failure
    added late -- or truncate it away."""
    nodes = [_line("a", Health.DONE), _line("b", Health.READY), _line("c", Health.RUNNING),
             _line("d", Health.WAITING_FOR_DECISION, waiting_on="owner"),
             _line("e", Health.BLOCKED), _line("z", Health.FAILED)]
    state = _compact(nodes, max_lines=10)
    assert [l.health for l in state.lines] == [
        Health.FAILED, Health.BLOCKED, Health.WAITING_FOR_DECISION,
        Health.RUNNING, Health.READY, Health.DONE]
    assert _ids(state)[0] == "z"
    # Control: without the failure, the first line is the next state in order.
    assert _ids(_compact(nodes[:-1], max_lines=10))[0] == "e"


def test_attention_order_is_stable_within_one_state():
    """Two runs over the same input must list equal-health nodes in the same
    order, or the digest would move without the state moving."""
    nodes = [_line("r2"), _line("r1"), _line("r3")]
    assert _ids(_compact(nodes, max_lines=10)) == ["r2", "r1", "r3"]


# --------------------------------------------------------------------------- #
# Rule 3 -- keep wins
# --------------------------------------------------------------------------- #


def test_a_kept_node_survives_truncation_that_would_otherwise_drop_it():
    """The caller knows a node matters (the one it is resuming); a budget must
    not drop it because its health sorts last."""
    nodes = [_line("u1", Health.RUNNING), _line("d1", Health.DONE)]
    state = _compact(nodes, max_lines=1, keep=["d1"])
    assert "d1" in _ids(state)


def test_control_without_keep_the_same_node_is_dropped():
    """Control: only `keep` differs. Without it, the DONE node is the one the
    budget drops -- so the case above really shows `keep` winning."""
    nodes = [_line("u1", Health.RUNNING), _line("d1", Health.DONE)]
    state = _compact(nodes, max_lines=1)
    assert "d1" not in _ids(state) and _ids(state) == ["u1"]


def test_keep_naming_an_unknown_node_raises():
    """A keep that names nothing would silently keep nothing; the caller asked
    to see a node this state cannot show, and is told so."""
    with pytest.raises(ValueError, match="ghost"):
        _compact([_line("a")], max_lines=1, keep=["ghost"])


# --------------------------------------------------------------------------- #
# Rule 4 -- every reduction is recorded
# --------------------------------------------------------------------------- #


def test_nothing_dropped_means_no_omissions_and_not_truncated():
    """An omission or a truncated flag on a complete state would send a reader
    looking for nodes that do not exist."""
    nodes = [_line("a"), _line("b")]
    state = _compact(nodes, max_lines=2)
    assert state.omissions == () and state.truncated is False


def test_control_one_dropped_means_an_omission_and_truncated():
    """Control: one more node than the budget. A compactor that dropped it with
    no record is the lie this module exists to prevent."""
    nodes = [_line("a"), _line("b"), _line("c")]
    state = _compact(nodes, max_lines=2)
    assert state.truncated is True
    assert [(o.kind, o.count) for o in state.omissions] == [(DROPPED, 1)]
    assert "READY" in state.omissions[0].why


def test_each_dropped_health_state_gets_its_own_omission():
    """One lump 'n dropped' would not tell a reader whether what went missing
    was finished work or pending work."""
    nodes = [_line("u1", Health.RUNNING), _line("r1"), _line("d1", Health.DONE)]
    state = _compact(nodes, max_lines=1)
    assert _ids(state) == ["u1"]
    whys = {o.why.split()[4]: o.count for o in state.omissions if o.kind == DROPPED}
    assert whys == {"READY": 1, "DONE": 1}


def test_a_truncated_state_without_an_omission_is_refused():
    """The spec's own refusal: `truncated=True` with nothing saying what was
    left out is refused at construction."""
    state = _compact([_line("a"), _line("b")], max_lines=1)
    body = dict(state)
    with pytest.raises(ValidationError):
        CompactState(**{**body, "omissions": ()})
    # Control: with its omission it constructs.
    CompactState(**body)


# --------------------------------------------------------------------------- #
# Rule 5 -- a budget cannot suppress a failure
# --------------------------------------------------------------------------- #


def test_budget_of_one_keeps_all_three_failures_and_records_the_overrun():
    """With max_lines=1 a naive compactor keeps one failure and hides two. The
    budget must be exceeded instead, and the overrun must be on record."""
    nodes = [_line("r1"), _line("f1", Health.FAILED), _line("f2", Health.FAILED),
             _line("f3", Health.FAILED)]
    state = _compact(nodes, max_lines=1)
    assert _ids(state) == ["f1", "f2", "f3"]
    over = [o for o in state.omissions if o.kind == BUDGET_EXCEEDED]
    assert len(over) == 1 and over[0].count == 2
    assert state.truncated is True     # the READY node was dropped


def test_control_budget_large_enough_records_no_overrun():
    """Control: only max_lines differs. A compactor that always reported
    budget_exceeded would pass the case above."""
    nodes = [_line("r1"), _line("f1", Health.FAILED), _line("f2", Health.FAILED),
             _line("f3", Health.FAILED)]
    state = _compact(nodes, max_lines=3)
    assert _ids(state) == ["f1", "f2", "f3"]
    assert not any(o.kind == BUDGET_EXCEEDED for o in state.omissions)


def test_budget_exceeded_without_anything_dropped_is_recorded_but_not_truncated():
    """Three failures and a budget of one: nothing is dropped, so the state is
    not truncated, but a reader asked for one line and got three -- that is
    still recorded, not silently absorbed."""
    nodes = [_line(f"f{i}", Health.FAILED) for i in range(3)]
    state = _compact(nodes, max_lines=1)
    assert len(state.lines) == 3 and state.truncated is False
    assert [o.kind for o in state.omissions] == [BUDGET_EXCEEDED]


def test_budget_cannot_drop_a_node_waiting_for_a_decision():
    """The chosen reading of 'needs attention': WAITING_FOR_DECISION is kept
    like a failure. A question nobody knows is pending is never answered."""
    nodes = [_line("f1", Health.FAILED),
             _line("w1", Health.WAITING_FOR_DECISION, waiting_on="owner")]
    state = _compact(nodes, max_lines=1)
    assert _ids(state) == ["f1", "w1"]
    assert any(o.kind == BUDGET_EXCEEDED for o in state.omissions)
    # Control: a RUNNING node in the same place is dropped by the same budget.
    ctrl = _compact([nodes[0], _line("u1", Health.RUNNING)], max_lines=1)
    assert _ids(ctrl) == ["f1"] and ctrl.truncated


# --------------------------------------------------------------------------- #
# Rule 6 -- max_lines of zero or less raises
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("max_lines", [0, -1])
def test_max_lines_of_zero_or_less_raises(max_lines):
    """A budget of nothing is not a request for a state; answering it with an
    empty excerpt would be the most complete lie by omission possible."""
    with pytest.raises(ValueError, match="max_lines"):
        _compact([_line("a")], max_lines=max_lines)


def test_control_max_lines_of_one_does_not_raise():
    """Control: the smallest valid budget constructs."""
    assert _ids(_compact([_line("a")], max_lines=1)) == ["a"]


def test_duplicate_node_ids_raise():
    """Two nodes under one id would be counted twice but be one line to look
    up; which one a reader meant could not be told apart."""
    with pytest.raises(ValueError, match="duplicate"):
        _compact([_line("a"), _line("a", Health.DONE)], max_lines=5)


# --------------------------------------------------------------------------- #
# state_digest
# --------------------------------------------------------------------------- #


def test_state_digest_moves_with_health_but_not_with_title():
    """The digest is over state, not prose: rewording a title must not look
    like progress, and a health change must not look like nothing."""
    base = _compact([_line("a", Health.RUNNING)], max_lines=1)
    retitled = _compact([_line("a", Health.RUNNING, title="new words")], max_lines=1)
    moved = _compact([_line("a", Health.DONE)], max_lines=1)
    assert retitled.state_digest == base.state_digest
    assert moved.state_digest != base.state_digest


def test_state_digest_moves_with_generation_and_as_of():
    """Two states of different generations with one digest would let a stale
    state pass for a current one."""
    base = _compact([_line("a")], max_lines=1)
    assert _compact([_line("a")], max_lines=1, generation=8).state_digest != base.state_digest
    assert _compact([_line("a")], max_lines=1, as_of="later").state_digest != base.state_digest
    # Control: equal inputs, equal digest.
    assert _compact([_line("a")], max_lines=1).state_digest == base.state_digest


def test_a_state_digest_that_does_not_match_the_state_is_refused():
    """A hand-edited digest would let two different states claim to be one."""
    body = dict(_compact([_line("a")], max_lines=1))
    with pytest.raises(ValidationError, match="state_digest"):
        CompactState(**{**body, "state_digest": "0" * 64})


# --------------------------------------------------------------------------- #
# stale_against
# --------------------------------------------------------------------------- #

SOURCES = {"ledger": "L1", "runs": "R1"}


def _sourced():
    return _compact([_line("a")], max_lines=1, source_digests=SOURCES)


def test_stale_against_is_empty_when_everything_matches():
    """Control for the three cases below: a checker that always said stale
    would pass them and be useless."""
    assert stale_against(_sourced(), current=dict(SOURCES), current_generation=7) == []


def test_stale_against_reports_a_changed_digest():
    """Relying on a state built from an older ledger reads current-looking
    numbers that are not current."""
    reasons = stale_against(_sourced(), current={**SOURCES, "ledger": "L2"},
                            current_generation=7)
    assert len(reasons) == 1 and "ledger" in reasons[0] and "changed" in reasons[0]


def test_stale_against_reports_a_missing_source():
    """Absence is not agreement: a source that vanished from `current` cannot
    confirm the state, so a checker that compared only shared keys fails."""
    reasons = stale_against(_sourced(), current={"runs": "R1"}, current_generation=7)
    assert len(reasons) == 1 and "ledger" in reasons[0] and "missing" in reasons[0]


def test_stale_against_reports_a_moved_generation():
    """Same sources, newer generation: the programme moved on underneath."""
    reasons = stale_against(_sourced(), current=dict(SOURCES), current_generation=8)
    assert len(reasons) == 1 and "generation" in reasons[0]


def test_stale_against_without_a_generation_fails_closed():
    """A check that was not made is not a generation that did not move."""
    reasons = stale_against(_sourced(), current=dict(SOURCES))
    assert len(reasons) == 1 and "current_generation" in reasons[0]


def test_stale_against_reports_a_source_the_state_never_saw():
    """A new source in `current` is something this state cannot speak for."""
    reasons = stale_against(_sourced(), current={**SOURCES, "decisions": "D1"},
                            current_generation=7)
    assert len(reasons) == 1 and "decisions" in reasons[0]


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def test_render_of_a_truncated_state_contains_every_omissions_why():
    """A renderer that drops the omissions re-introduces the lie the model
    prevents: the reader sees a short list and no hint it is short."""
    nodes = _mixed() + [_line(f"f{i}", Health.FAILED) for i in range(2, 5)]
    state = _compact(nodes, max_lines=1)
    assert state.truncated and len(state.omissions) >= 2
    assert {o.kind for o in state.omissions} >= {DROPPED, BUDGET_EXCEEDED}
    text = render(state)
    for omission in state.omissions:
        assert omission.why in text
    # Omissions come last.
    assert text.rstrip().splitlines()[-1].strip().endswith(state.omissions[-1].why)


def test_render_shows_header_counts_lines_and_says_none_when_nothing_was_omitted():
    """Control: an untruncated state renders an explicit 'none', so a missing
    omissions section can never be read as 'nothing omitted'."""
    state = _compact([_line("b1", Health.BLOCKED, reason="disk full"),
                      _line("w1", Health.WAITING_FOR_DECISION, waiting_on="owner")],
                     max_lines=5)
    text = render(state)
    assert "generation 7" in text and AS_OF in text and "prog" in text
    assert "BLOCKED=1" in text and "FAILED=0" in text
    assert "disk full" in text and "waiting on: owner" in text
    assert text.rstrip().endswith("omissions:\n  none")


def test_omission_refuses_a_blank_why():
    """An omission that does not say why is a shrug, not a record."""
    with pytest.raises(ValidationError):
        Omission(kind=DROPPED, count=1, why="  ")
    assert Omission(kind=DROPPED, count=1, why="budget").why == "budget"
