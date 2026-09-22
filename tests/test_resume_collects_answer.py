"""O199: resuming after an approved dialog collects the finished answer.

The first live P1-16 run showed the defect: the operator approved the
planner's prompt, the planner wrote a valid plan, and the resumed run parked
that plan as "an earlier round" and asked again -- every approval enabled work
the next resume discarded. These cases pin the four conditions under which a
finished answer is collected, and the ways each can fail back to parking.
No agent is started; the decision is a function of files on disk.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from hoh.contracts import Role
from hoh.dispatchers import _Base

PROMPT = "plan iteration 1, attempt 0, for spec 85cacc7e"


def _setup(tmp_path):
    d = _Base(answers_dir=tmp_path / "answers", profiles={r: "claude" for r in Role})
    answer = tmp_path / "answers" / "i1-a0-planner.json"
    return d, answer


def _dispatched(d, answer, prompt=PROMPT, when=None):
    d._write_dispatch_marker(answer, prompt)
    if when is not None:
        marker = d._marker_for(answer)
        import json
        meta = json.loads(marker.read_text())
        meta["dispatched_at"] = when
        marker.write_text(json.dumps(meta))


def test_the_answer_to_the_same_prompt_written_after_it_was_asked_is_collected(tmp_path):
    d, answer = _setup(tmp_path)
    _dispatched(d, answer, when=time.time() - 60)
    answer.write_text('{"plan": "six checks"}')
    assert d._resumable_answer(Role.PLANNER, answer, PROMPT) is True


def test_a_different_prompt_means_a_different_question(tmp_path):
    """A changed iteration, attempt, amendment or spec changes the prompt; an
    answer to the old one must be parked, not reused."""
    d, answer = _setup(tmp_path)
    _dispatched(d, answer, when=time.time() - 60)
    answer.write_text('{"plan": "old"}')
    assert d._resumable_answer(Role.PLANNER, answer, PROMPT + " (amended)") is False


def test_an_answer_older_than_the_dispatch_is_not_its_answer(tmp_path):
    d, answer = _setup(tmp_path)
    answer.write_text('{"plan": "stale"}')
    old = time.time() - 3600
    os.utime(answer, (old, old))
    _dispatched(d, answer, when=time.time() - 60)
    assert d._resumable_answer(Role.PLANNER, answer, PROMPT) is False


def test_no_marker_means_no_evidence_it_was_asked(tmp_path):
    """Every answer written before this repair has no marker; they park."""
    d, answer = _setup(tmp_path)
    answer.write_text('{"plan": "x"}')
    assert d._resumable_answer(Role.PLANNER, answer, PROMPT) is False


def test_an_empty_answer_is_not_an_answer(tmp_path):
    d, answer = _setup(tmp_path)
    _dispatched(d, answer, when=time.time() - 60)
    answer.write_text("   \n")
    assert d._resumable_answer(Role.PLANNER, answer, PROMPT) is False


def test_the_developer_is_never_collected_this_way(tmp_path):
    """The developer delivers into the worktree, not a file; its delivery is
    measured by the tree digest, so this path does not apply to it."""
    d, answer = _setup(tmp_path)
    _dispatched(d, answer, when=time.time() - 60)
    answer.write_text('{"x": 1}')
    assert d._resumable_answer(Role.DEVELOPER, answer, PROMPT) is False


def test_a_second_dispatch_keeps_the_first_marker(tmp_path):
    """House rules: the earlier marker is versioned, never overwritten."""
    d, answer = _setup(tmp_path)
    _dispatched(d, answer)
    _dispatched(d, answer, prompt=PROMPT + " again")
    kept = list(Path(tmp_path / "answers").glob("i1-a0-planner.json.dispatch.json.v*"))
    assert len(kept) == 1
