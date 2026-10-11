"""The successor's Herdr agent name obeys Herdr's own rule (strand rlname).

The A7 dress rehearsal of 2026-10-11 launched a real successor and Herdr refused it:
`invalid_agent_name` for `41da3c-succ-20261011T000`, because the random hex part led (a digit five
times in eight) and the run id carries an upper-case 'T' and 'Z'. Every earlier test used a fake
`herdr` that accepted any name. The fake here answers exactly like Herdr does: a name must match
`[a-z][a-z0-9_-]{0,31}`.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from hoh import rollover
from hoh.controller import new_run_id
from hoh.rollover import HerdrLauncher

HERDR_RULE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class StrictHerdr:
    """A fake `herdr` that refuses an agent name the way the real one does."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        key = (argv[1], argv[2])
        if key == ("tab", "create"):
            out = {"result": {"root_pane": {"pane_id": "w1:p9"}, "tab": {"tab_id": "w1:t9"}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        if key in {("agent", "start"), ("agent", "prompt")} and not HERDR_RULE.match(argv[3]):
            err = {"error": {"code": "invalid_agent_name", "message": "agent name must start with a "
                             "lowercase letter and contain only lowercase letters, digits, '-' or '_' "
                             "(1-32 characters)"}, "id": "cli:agent:start"}
            return subprocess.CompletedProcess(argv, 1, "", json.dumps(err))
        if key == ("agent", "start"):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agent": {}}}), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


def launch(tmp_path, session_id: str) -> tuple[str, StrictHerdr]:
    herdr = StrictHerdr()
    launcher = HerdrLauncher(run=herdr, workspace="w1", settle_s=0, sleep=lambda s: None)
    handle = launcher.start(session_id=session_id, model="claude-opus-5-5", cwd=tmp_path,
                            prompt="P", label="successor-rc-1")
    return handle.agent, herdr


# N1 -- the rehearsal's failure, reproduced with its own token and the real session-id form
def test_a_digit_leading_token_and_a_real_run_id_still_start_the_successor(tmp_path, monkeypatch):
    monkeypatch.setattr(rollover.secrets, "token_hex", lambda n: "41da3c")
    sid = new_run_id("succ")                                   # e.g. succ-20261011T000140Z-9f3716
    name, herdr = launch(tmp_path, sid)
    assert HERDR_RULE.match(name), name
    [start] = [c for c in herdr.calls if c[1:3] == ["agent", "start"]]
    [prompt] = [c for c in herdr.calls if c[1:3] == ["agent", "prompt"]]
    assert start[3] == prompt[3] == name
    assert name == ("s41da3c-" + sid.lower())[:24]


# N2 -- any session id gives a name Herdr accepts, never longer than 24 characters
@pytest.mark.parametrize("sid", [
    "succ-20261011T000140Z-9f3716", "SUCC", "a/b c.d:e", "Äpfel-ünd-Öl", "x" * 100, "-", "",
    "tab\tnew\nline", "ﬁ-ligature", "K-kelvin"])
@pytest.mark.parametrize("token", ["41da3c", "abcdef", "000000", "f0f0f0"])
def test_every_session_id_maps_to_a_name_herdr_accepts(sid, token):
    name = rollover.successor_agent_name(sid, token)
    assert HERDR_RULE.match(name), (sid, token, name)
    assert len(name) <= 24


# N3 -- the random part leads, behind exactly one fixed letter; the character map is exact
def test_the_random_part_follows_one_fixed_letter_and_leads_the_session_id():
    assert rollover.successor_agent_name("succ-X", "41da3c") == "s41da3c-succ-x"
    # every other character becomes exactly one '-' (not dropped, not '_'); an '_' stays; nothing is stripped
    assert rollover.successor_agent_name("a/b c.d:e_f/", "41da3c") == "s41da3c-a-b-c-d-e_f-"
    a = rollover.successor_agent_name("s" * 40, "aaaaaa")
    b = rollover.successor_agent_name("s" * 40, "bbbbbb")
    assert a != b and a[1:7] == "aaaaaa" and b[1:7] == "bbbbbb"


# N4 -- the strict fake really refuses what Herdr refuses (the fake is not vacuous)
@pytest.mark.parametrize("bad", ["41da3c-succ", "sUCC", "s.x", "", "s" * 33])
def test_the_strict_fake_refuses_an_invalid_name(bad):
    proc = StrictHerdr()(["herdr", "agent", "start", bad])
    assert proc.returncode == 1 and "invalid_agent_name" in proc.stderr
