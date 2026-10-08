"""The CLI's authoritative commands on a governed project (AC7, review A P4/P5).

* Acting *as* a master needs its token, not just its session id and epoch --
  `hoh project authority` prints those two.
* With no live master, an operator command acquires an epoch of its own
  (reason OPERATOR), acts under it and releases it: an orderly-halted project
  must have a way out of BLOCKED.
* With a live master and no credential, the command is refused.
"""

from __future__ import annotations

import json

from hoh import cli
from hoh.epoch import EpochStore
from hoh.project import Lifecycle, ProjectState, TaskNode
from hoh.projectstore import ProjectStore
from hoh.succession import MasterAuthority


def _governed(tmp_path, *, live: bool):
    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[
        TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="merge conflict")]))
    a = MasterAuthority(s.dir, "sA", serialize=s.lock)
    a.acquire()
    if not live:
        a.release(reason="orderly halt")
    return s, a


def _cli(tmp_path, *argv):
    return cli.main(["--root", str(tmp_path), "project", *argv])


def test_ac7_session_and_epoch_without_the_token_are_refused(tmp_path):
    s, a = _governed(tmp_path, live=True)
    assert _cli(tmp_path, "handoff", "p", "--session", "sA", "--epoch", "1",
                "--to", "intruder", "--reason", "x") == 5
    assert a.problems() == []                           # the live master is untouched


def test_the_master_token_authorises_a_handoff(tmp_path):
    s, a = _governed(tmp_path, live=True)
    assert _cli(tmp_path, "handoff", "p", "--session", "sA", "--epoch", "1",
                "--token", a.token, "--to", "sB", "--reason", "rotate") == 0
    assert a.problems()                                 # handed over: stale now


def test_a_live_master_blocks_an_anonymous_unblock(tmp_path):
    s, a = _governed(tmp_path, live=True)
    assert _cli(tmp_path, "unblock", "p", "a", "--reason", "x") == 5
    assert s.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_without_a_live_master_an_operator_epoch_unblocks_and_is_released(tmp_path, capsys):
    s, a = _governed(tmp_path, live=False)
    assert _cli(tmp_path, "unblock", "p", "a", "--reason", "conflict resolved") == 0
    assert s.read_state().node("a").lifecycle is Lifecycle.READY
    chain = EpochStore(s.dir).chain()
    assert [e.reason.value for e in chain] == ["INITIAL", "OPERATOR"]
    assert s.read_state().master_epoch == 2
    capsys.readouterr()
    assert _cli(tmp_path, "authority", "p") == 0
    assert json.loads(capsys.readouterr().out)["holder_state"] == "RELEASED"


def test_an_old_epoch_with_its_old_token_is_refused_after_takeover(tmp_path):
    s, a = _governed(tmp_path, live=False)
    old_token = a.token
    b = MasterAuthority(s.dir, "sB", serialize=s.lock)
    b.acquire()
    assert _cli(tmp_path, "unblock", "p", "a", "--reason", "x", "--session", "sA",
                "--epoch", "1", "--token", old_token) == 5


def test_drive_writes_its_token_owner_only(tmp_path):
    import os
    import stat
    from pathlib import Path

    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[]))
    (s.dir / "driver.json").write_text(json.dumps(
        {"factory": "durability_fixture:build",
         "path": str(Path(__file__).resolve().parent.parent / "tools")}))
    tok = tmp_path / "tok"
    rc = _cli(tmp_path, "drive", "p", "--session", "sA", "--token-out", str(tok))
    assert rc in (0, 1)
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600 and len(tok.read_text()) == 32


def test_token_out_to_an_unwritable_path_releases_and_refuses(tmp_path):
    from pathlib import Path

    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[]))
    (s.dir / "driver.json").write_text(json.dumps(
        {"factory": "durability_fixture:build",
         "path": str(Path(__file__).resolve().parent.parent / "tools")}))
    rc = _cli(tmp_path, "drive", "p", "--session", "sA",
              "--token-out", str(tmp_path / "no" / "such" / "dir" / "tok"))
    assert rc == 5
    lease = json.loads((s.dir / "lease.json").read_text())
    assert lease["released_at"] and "token-out" in lease["released_reason"]


def test_token_out_tightens_an_existing_file(tmp_path):
    import os
    import stat
    from pathlib import Path

    s = ProjectStore(tmp_path, "p")
    s.create(ProjectState(project_id="p", repo_path=str(tmp_path), nodes=[]))
    (s.dir / "driver.json").write_text(json.dumps(
        {"factory": "durability_fixture:build",
         "path": str(Path(__file__).resolve().parent.parent / "tools")}))
    tok = tmp_path / "tok"
    tok.write_text("old")
    tok.chmod(0o644)
    _cli(tmp_path, "drive", "p", "--session", "sA", "--token-out", str(tok))
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600
