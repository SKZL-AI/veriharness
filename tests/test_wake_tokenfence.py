"""P3-WAKE slice 3: the working directory, the brief and the answer of a wake keep clear
of the token file the caller presented (`hoh.wake.token_file_fence`).

After a session took its own lapsed lease again the live token is no longer in the
rollover directory (it is wherever a later `--token-out` put it), so the rollover-directory
guards of slice 1 do not cover it. The successions and re-arms are the real ones of
`test_wake.py` / `test_wake_rearm.py`; the command is run through `cli.main`; the control
adapter is never reached (every case here is a plan, or a refusal before one).
Nothing starts Herdr, an agent or a subprocess, reads `~/.agents`, or writes outside
`tmp_path`.
"""
from __future__ import annotations

import json
import os

import pytest
from test_wake import RID, Program, run_cli, wk
from test_wake_rearm import rearm

from hoh.orchestrator import write_lock
from hoh.succession import MasterAuthority


@pytest.fixture
def no_herdr(monkeypatch):
    def boom():
        raise AssertionError("a plan must not reach for Herdr")
    monkeypatch.setattr(wk(), "make_control", boom)


class World:
    """A -> B -> C, then C took its own lapsed lease twice (epoch 5, RECOVERY): the
    live token is written wherever a test says, as `--token-out` would."""

    def __init__(self, tmp_path, *, chain=3) -> None:
        self.tmp = tmp_path
        self.p = Program(tmp_path)
        self.p.first("orch-a")
        self.p.hand_over("orch-a", "orch-b")
        if chain == 3:
            self.p.hand_over("orch-b", "orch-c")
            self.master = "orch-c"
        else:
            self.master = "orch-b"
        rearm(self.p, self.master)
        rearm(self.p, self.master)
        self.epoch = self.p.masters[self.master].epoch
        self.tokens = tmp_path / "tokens"
        self.tokens.mkdir()
        self.token = self.tokens / "live.token"
        self.token.write_text(self.p.masters[self.master].token)

    def argv(self, *, brief=None, answer=None, cwd=None, token_args=None):
        if token_args is None:
            token_args = ["--token-file", str(self.token)]
        out = ["--root", str(self.p.root), "project", "wake", "p", "--session", self.master,
               "--epoch", str(self.epoch), *token_args, "--resume-id", RID,
               "--brief", brief or self.p.brief(), "--answer", answer or self.p.answer()]
        if cwd is not None:
            out += ["--cwd", cwd]
        return out

    def run(self, **kw):
        return run_cli(self.argv(**kw))


@pytest.fixture
def world(tmp_path, no_herdr):
    return World(tmp_path)


@pytest.fixture
def first_master_world(tmp_path, no_herdr):
    """A -> B, B re-armed: the records do not know where A was started, so `--cwd` is
    needed and is a value of the caller's."""
    return World(tmp_path, chain=2)


def refused_by_the_fence(rc, out, err):
    return rc == 2 and out == "" and "token file" in err and err.startswith("refused:")


def test_a_brief_an_answer_and_a_directory_beside_the_token_file_are_not_in_the_way(world):
    beside = world.tokens / "brief.md"
    beside.write_text("what did you decide?\n")
    answers = world.tokens / "answers"
    answers.mkdir()
    rc, out, err = world.run(brief=str(beside), answer=str(answers / "a.md"))
    assert rc == 0, err
    doc = json.loads(out)
    assert doc["executed"] is False and doc["succession"]["predecessor_session"] == "orch-b"
    assert doc["succession"]["rearmed_epochs"] == [4, 5]
    assert str(world.token) not in out                          # the plan never names it


def test_an_answer_in_the_token_files_own_directory_is_allowed(world):
    """Siblings are allowed by design: the fence keeps the three values off the token
    file, it does not empty its directory."""
    rc, out, err = world.run(answer=str(world.tokens / "a.md"))
    assert rc == 0, err


def test_a_name_that_only_starts_like_the_token_file_is_not_the_token_file(world):
    """`live.token.md` next to `live.token`, a directory `tokens-old` beside `tokens`: a
    string prefix is not containment."""
    near = world.tokens / "live.token.md"
    near.write_text("what did you decide?\n")
    rc, out, err = world.run(brief=str(near))
    assert rc == 0, err
    old_dir = world.tmp / "tokens-old"
    old_dir.mkdir()
    rc, out, err = world.run(answer=str(old_dir / "a.md"))
    assert rc == 0, err


@pytest.mark.parametrize("how", ["itself", "through-a-link-to-its-directory", "hard-link"])
def test_a_brief_that_is_the_token_file_is_refused_in_every_spelling(world, how):
    if how == "itself":
        brief = str(world.token)
    elif how == "through-a-link-to-its-directory":
        link = world.tmp / "alias"
        link.symlink_to(world.tokens)
        brief = str(link / "live.token")
    else:
        other = world.tmp / "elsewhere"
        other.mkdir()
        os.link(world.token, other / "brief.md")
        brief = str(other / "brief.md")
    rc, out, err = world.run(brief=brief)
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--brief" in err


def test_the_token_file_may_be_named_through_a_link_and_the_brief_by_its_real_path(world):
    link = world.tmp / "alias"
    link.symlink_to(world.tokens)
    rc, out, err = world.run(brief=str(world.token),
                             token_args=["--token-file", str(link / "live.token")])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)


def test_a_token_file_named_through_a_link_keeps_the_real_directory_out_as_a_working_dir(
        first_master_world):
    """The links are followed for the token file too: `--token-file alias/live.token`
    and `--cwd` the real directory."""
    w = first_master_world
    alias = w.tmp / "alias"
    alias.symlink_to(w.tokens)
    rc, out, err = w.run(cwd=str(w.tokens), token_args=["--token-file", str(alias / "live.token")])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--cwd" in err


def test_a_symlinked_token_file_in_the_working_directory_is_the_credential_there(
        first_master_world):
    """`--token-file /o/tlink` with `tlink` a link to the token elsewhere: `/o` holds the
    credential under that name, so `--cwd /o` is refused (the presented spelling)."""
    w = first_master_world
    o = w.tmp / "o"
    o.mkdir()
    (o / "tlink").symlink_to(w.token)
    rc, out, err = w.run(cwd=str(o), token_args=["--token-file", str(o / "tlink")])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--cwd" in err


def test_a_token_file_spelled_with_a_trailing_slash_is_the_file_the_command_read(world):
    """The command's read normalises `live.token/`; so does the fence (it is not
    'cannot be resolved'), and the file is still kept clear of."""
    spelled = str(world.token) + "/"
    rc, out, err = world.run(token_args=["--token-file", spelled])
    assert rc == 0, err
    rc, out, err = world.run(brief=str(world.token), token_args=["--token-file", spelled])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)


@pytest.mark.parametrize("where", ["the-directory-of-the-token-file", "a-directory-above-it",
                                   "a-link-to-its-directory"])
def test_a_working_directory_that_holds_the_token_file_is_refused(first_master_world, where):
    w = first_master_world
    if where == "the-directory-of-the-token-file":
        cwd = w.tokens
    elif where == "a-directory-above-it":
        cwd = w.tmp
    else:
        cwd = w.tmp / "alias"
        cwd.symlink_to(w.tokens)
    rc, out, err = w.run(cwd=str(cwd))
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--cwd" in err


def test_a_working_directory_beside_the_token_file_is_fine(first_master_world):
    w = first_master_world
    start = w.tmp / "started-here"
    start.mkdir()
    rc, out, err = w.run(cwd=str(start))
    assert rc == 0, err
    assert json.loads(out)["cwd"] == os.path.realpath(start)


def test_a_working_directory_whose_name_starts_like_the_tokens_directory_is_fine(
        first_master_world):
    w = first_master_world
    near = w.tmp / "tok"                        # a prefix of `tokens`
    near.mkdir()
    rc, out, err = w.run(cwd=str(near))
    assert rc == 0, err


def test_the_directory_the_records_name_is_held_to_it_too(world):
    """No `--cwd`: the plan's directory is the predecessor's own launch directory, from
    the records. A token file written into it (a `--token-out` there) is refused."""
    launch = world.p.dir / "rollover" / f"{world.p.capsules[0]}.cwd"
    inside = launch / "live.token"
    inside.write_text(world.p.masters[world.master].token)
    rc, out, err = world.run(token_args=["--token-file", str(inside)])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--cwd" in err


def test_the_directory_the_records_name_is_held_to_it_through_a_link_to_the_token_file(world):
    launch = world.p.dir / "rollover" / f"{world.p.capsules[0]}.cwd"
    (launch / "live.token").write_text(world.p.masters[world.master].token)
    alias = world.tmp / "alias"
    alias.symlink_to(launch)
    rc, out, err = world.run(token_args=["--token-file", str(alias / "live.token")])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--cwd" in err


def test_a_relative_token_file_is_resolved_from_the_working_directory_as_read(
        world, monkeypatch):
    monkeypatch.chdir(world.tokens)
    rc, out, err = world.run(brief=str(world.token), token_args=["--token-file", "live.token"])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)


def named_directory(world):
    folder = world.tmp / "named"
    folder.mkdir()
    token = world.p.masters[world.master].token
    return folder, ["--token", token, "--token-file", str(folder)]


def test_an_answer_under_a_directory_named_as_the_token_file_is_refused(world):
    """With the token given inline the `--token-file` may name a directory; an answer
    under it is under the token file."""
    folder, inline = named_directory(world)
    rc, out, err = world.run(answer=str(folder / "a.md"), token_args=inline)
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--answer" in err


def test_an_answer_through_a_link_to_the_directory_named_as_the_token_file_is_refused(world):
    folder, inline = named_directory(world)
    alias = world.tmp / "alias2"
    alias.symlink_to(folder)
    rc, out, err = world.run(answer=str(alias / "a.md"), token_args=inline)
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--answer" in err


def test_an_answer_outside_the_directory_named_as_the_token_file_is_not_the_fences_refusal(world):
    folder, inline = named_directory(world)
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    rc, out, err = world.run(answer=str(elsewhere / "a.md"), token_args=inline)
    assert rc == 0, err


def test_an_answer_that_is_the_token_file_is_refused_by_the_existing_rule(world):
    rc, out, err = world.run(answer=str(world.token))
    assert (rc, out) == (2, "") and "already exists" in err


def test_a_brief_below_a_directory_named_as_the_token_file_is_refused_through_a_link(world):
    """The same-file test cannot see a brief that is a different file in the directory
    the (inline-token) `--token-file` names: its real path can."""
    folder = world.tmp / "named"
    folder.mkdir()
    (folder / "brief.md").write_text("what did you decide?\n")
    alias = world.tmp / "alias"
    alias.symlink_to(folder)
    token = world.p.masters[world.master].token
    rc, out, err = world.run(brief=str(alias / "brief.md"),
                             token_args=["--token", token, "--token-file", str(folder)])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "--brief" in err


@pytest.mark.parametrize("what", ["missing", "dangling-link", "link-loop"])
def test_a_token_file_that_cannot_be_resolved_refuses_the_plan(world, what):
    if what == "missing":
        target = world.tmp / "nowhere.token"
    elif what == "dangling-link":
        target = world.tmp / "dangling.token"
        target.symlink_to(world.tmp / "nowhere.token")
    else:
        target = world.tmp / "loop.token"
        target.symlink_to(target)
    token = world.p.masters[world.master].token
    rc, out, err = world.run(token_args=["--token", token, "--token-file", str(target)])
    assert (rc, out) == (2, "") and "cannot be resolved" in err, (rc, out, err)


def test_a_token_given_inline_has_no_file_to_keep_clear_of(world):
    """Stated limit: nothing to compare. The plan is made, with the brief wherever it is."""
    token = world.p.masters[world.master].token
    rc, out, err = world.run(token_args=["--token", token])
    assert rc == 0, err


def test_an_empty_token_file_option_is_no_path(world):
    token = world.p.masters[world.master].token
    rc, out, err = world.run(token_args=["--token", token, "--token-file", ""])
    assert rc == 0, err


def test_a_home_relative_token_file_is_resolved_as_the_command_reads_it(world, monkeypatch):
    home = world.tmp / "home"
    home.mkdir()
    (home / "live.token").write_text(world.p.masters[world.master].token)
    monkeypatch.setenv("HOME", str(home))
    rc, out, err = world.run(brief=str(home / "live.token"),
                             token_args=["--token-file", "~/live.token"])
    assert refused_by_the_fence(rc, out, err), (rc, out, err)
    assert "cannot be resolved" not in err


def stale_authority(world):
    stale = MasterAuthority(world.p.dir, "orch-c", serialize=lambda: write_lock(world.p.store))
    stale.present(epoch=3, token=world.p.masters["orch-c"].token)   # an epoch it left behind
    return stale


def test_a_caller_that_is_not_the_master_learns_nothing_about_the_paths_module(world):
    with pytest.raises(wk().WakeRefused, match="not the master"):
        wk().plan_wake(stale_authority(world), world.p.dir, project_id="p", resume_id=RID,
                       brief=str(world.token), answer=world.p.answer(), nonce="a1b2c3",
                       token_file=str(world.token))


def test_a_caller_that_is_not_the_master_learns_nothing_about_the_paths_command(world):
    world.epoch = 3                                             # the epoch it left behind
    rc, out, err = world.run(brief=str(world.token))
    assert (rc, out) == (5, "") and "stale master" in err


def _live_authority(world):
    live = world.p.masters[world.master]
    mine = world.p.authority(world.master)
    mine.present(epoch=live.epoch, token=live.token)
    return mine


def _plan_kwargs(world):
    return dict(project_id="p", resume_id=RID, brief=world.p.brief(), answer=world.p.answer(),
                nonce="a1b2c3")


def test_the_module_plans_as_before_without_a_token_file(world):
    plan = wk().plan_wake(_live_authority(world), world.p.dir, **_plan_kwargs(world))
    assert plan.succession.predecessor_session == "orch-b"


def test_the_module_treats_an_empty_token_file_as_none_whatever_the_process_directory(
        world, monkeypatch):
    monkeypatch.chdir(world.tmp)        # an empty path read as a path would be this directory
    plan = wk().plan_wake(_live_authority(world), world.p.dir, **_plan_kwargs(world),
                          token_file="")
    assert plan.cwd


def test_the_module_refuses_a_token_file_with_a_nul(world):
    with pytest.raises(wk().WakeUsage, match="cannot be resolved"):
        wk().plan_wake(_live_authority(world), world.p.dir, **_plan_kwargs(world),
                       token_file="a\x00b")


def test_the_module_refuses_a_brief_that_is_the_token_file_when_it_is_told_the_path(world):
    kw = _plan_kwargs(world)
    kw["brief"] = str(world.token)
    with pytest.raises(wk().WakeUsage, match="token file"):
        wk().plan_wake(_live_authority(world), world.p.dir, **kw, token_file=str(world.token))


def test_the_fence_judges_a_working_directory_spelled_through_a_link_by_its_real_path(world):
    """Called directly: the three values come in as given, so a link-spelled directory is
    compared by where it leads."""
    alias = world.tmp / "alias"
    alias.symlink_to(world.tokens)
    other = world.tmp / "elsewhere"
    other.mkdir()
    (other / "b.md").write_text("what did you decide?\n")
    with pytest.raises(wk().WakeUsage, match="--cwd"):
        wk().token_file_fence(str(world.token), cwd=str(alias), brief=str(other / "b.md"),
                              answer=str(other / "a.md"))
    wk().token_file_fence(str(world.token), cwd=str(other), brief=str(other / "b.md"),
                          answer=str(other / "a.md"))        # and clear places pass
