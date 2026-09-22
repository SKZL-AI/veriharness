"""Herdr agent names: can two runs ever be given the same one?

O195. The name was `hoh-{run_id[:12]}-{role}`, so every pair of runs whose ids
share twelve characters shared an agent name -- `repair-3-feature-a` and
`repair-3-feature-b` both became `hoh-repair-3-fea-developer`. The ownership
guard refuses a name that is taken elsewhere, so nothing was ever adopted;
instead the second run could not start at all. That is safe for runs taken
one at a time, and it is a hard stop for V3.3, whose whole point is running
independent nodes at the same time.

No agent is started here. The name is a pure function of the run id, and
that is the thing under test -- the house rules forbid real agent runs in a
test suite, and none is needed.
"""
from __future__ import annotations

from hoh.contracts import Role
from hoh.dispatchers import HerdrDispatcher


class _State:
    """The only field the name reads."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


def _name(run_id: str, role: Role = Role.DEVELOPER) -> str:
    return HerdrDispatcher._agent_name(None, role, _State(run_id))


#: Pairs that collided under the old scheme, and one that differs only in its
#: last character -- the shape a wave of sibling nodes naturally has.
SIBLINGS = [
    ("repair-3-feature-a", "repair-3-feature-b"),
    ("benchmark-v3-arm-a-rep1", "benchmark-v3-arm-b-rep1"),
    ("node-authentication-1", "node-authentication-2"),
]

#: Herdr truncates agent names at about 24 characters (house rules,
#: 2026-09-21). A name that is unique only after that point is not unique.
TRUNCATION = 24


def test_sibling_runs_get_different_names():
    for a, b in SIBLINGS:
        assert _name(a) != _name(b), (a, b, _name(a))


def test_the_names_differ_before_herdr_truncates_them():
    """The unique part has to come first. Uniqueness at character 30 is
    uniqueness Herdr throws away."""
    for a, b in SIBLINGS:
        for role in Role:
            na, nb = _name(a, role), _name(b, role)
            assert na[:TRUNCATION] != nb[:TRUNCATION], (na, nb)


def test_the_roles_of_one_run_are_distinct_within_the_truncation():
    names = {_name("repair-3-feature-a", role)[:TRUNCATION] for role in Role}
    assert len(names) == len(Role)


def test_the_name_is_a_pure_function_of_the_run_id():
    """After a restart the dispatcher looks for its own agent by name. A name
    that changed between two calls would make every restart start a second
    agent beside the first."""
    assert _name("repair-3-feature-a") == _name("repair-3-feature-a")


def test_the_name_is_short_and_safe_for_herdr():
    for rid in ("a", "x" * 80, "Mixed_Case_ID", "repair-3-feature-a"):
        for role in Role:
            name = _name(rid, role)
            assert len(name) <= 32, name
            assert name == name.lower(), name
            assert "_" not in name, name
            assert all(c.isalnum() or c == "-" for c in name), name


def test_the_run_is_still_readable_in_the_name():
    """An operator looking at `herdr agent list` should be able to tell which
    run a pane belongs to without a lookup table."""
    assert "repair" in _name("repair-3-feature-a")
