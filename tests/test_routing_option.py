"""P3-20 slice 2: `--routing-policy` on `hoh run` and `hoh start`, through the product's own
command surface (`cli.main`) to the dispatcher the run builds.

Slice 1 (`tests/test_routing.py`) made a declared policy resolvable and put `routing=` on
`build_dispatcher`; nothing in production passed it. These tests drive the real parser and the
real `_run_in` / `cmd_start` with `--iterations 0 --no-herdr` (the real `HarnessDispatcher` is
built, no role is started, no agent runs, nothing is written outside `tmp_path`), and read back:
what the dispatcher was built with, what the run directory holds before the first dispatch, and
what a resumed run does with a policy that is not the one it began under.

Mutant ids (N1..) are the spec's table; each test says which it kills.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from test_failure_classes import build  # noqa: F401
from test_failure_classes import repo as repo  # a fixture; the alias form is an explicit re-export
from test_governance import _initialised_storage_root

from hoh import cli, controller as controller_mod, detach, dispatchers, herdr, routing
from hoh.contracts import Role


def _doc(**over) -> dict:
    doc = {"schema": routing.SCHEMA, "default": {"model": "sonnet", "effort": "high"},
           "roles": {"qa": {"provider": "kimi", "model": "opus"}}}
    doc.update(over)
    return doc


def _objects(text: str) -> list[dict]:
    """The JSON objects `hoh` printed one after another (`_print` indents them)."""
    decoder, at, found = json.JSONDecoder(), 0, []
    while (at := text.find("{", at)) != -1:
        try:
            obj, at = decoder.raw_decode(text, at)
        except ValueError:
            at += 1
            continue
        found.append(obj)
    return found


def _write(path: Path, doc) -> Path:
    path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8"))
    return path


class World:
    """One run `r1` in `tmp_path/runs`, a storage root, and a spy on `build_dispatcher`."""

    def __init__(self, tmp_path, repo, monkeypatch, capsys):
        _initialised_storage_root(tmp_path, monkeypatch)
        _, self.state, self.store, _ = build(tmp_path, repo)
        self.tmp = tmp_path
        self.root = str(tmp_path / "runs")
        self.capsys = capsys
        self.seen: list[dict] = []
        self.built: list = []
        real = dispatchers.build_dispatcher

        self.pin_at_controller: list[bool] = []

        def spy(**kw):
            self.seen.append(kw)
            self.built.append(real(**kw))
            return self.built[-1]

        monkeypatch.setattr(dispatchers, "build_dispatcher", spy)
        real_controller = controller_mod.Controller

        class SpyController(real_controller):
            def __init__(inner, *a, **kw):
                self.pin_at_controller.append(self.pin_path.exists())
                super().__init__(*a, **kw)

        monkeypatch.setattr(controller_mod, "Controller", SpyController)
        monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)

    def run(self, *extra, run_id: str = "r1"):
        rc = cli.main(["--root", self.root, "run", run_id, "--iterations", "0", "--no-herdr",
                       *extra])
        got = self.capsys.readouterr()
        return rc, got.out, got.err

    @property
    def pin_path(self) -> Path:
        return self.store.dir / cli.ROUTING_RECORD

    def pin(self) -> dict:
        return json.loads(self.pin_path.read_text(encoding="utf-8"))


@pytest.fixture
def world(tmp_path, repo, monkeypatch, capsys):
    return World(tmp_path, repo, monkeypatch, capsys)


# --------------------------------------------------------------------------- #
# hoh run: the policy reaches the dispatcher, and is on disk before any dispatch
# --------------------------------------------------------------------------- #

def test_hoh_run_with_a_routing_policy_builds_the_dispatcher_with_the_routed_tables(world):
    """Kills N1 (the policy is loaded and not passed to `build_dispatcher`)."""
    policy_file = _write(world.tmp / "policy.json", _doc())
    rc, out, err = world.run("--routing-policy", str(policy_file))
    assert rc == 0, err
    (kw,) = world.seen
    policy = routing.load(policy_file)
    assert kw["routing"] is not None and kw["routing"].digest == policy.digest
    d = world.built[0]
    assert d.models == {Role.PLANNER: "sonnet", Role.DEVELOPER: "sonnet", Role.QA: "opus"}
    assert d.efforts == {Role.PLANNER: "high", Role.DEVELOPER: "high", Role.QA: "high"}
    assert d._agent_args(Role.QA) == ["--model", "opus", "--effort", "high"]
    assert d.routing_evidence["digest"] == policy.digest
    assert d.profiles == {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}
    (line,) = [o["routing_policy"] for o in _objects(out) if "routing_policy" in o]
    assert line["digest"] == policy.digest and line["pinned_before"] is False
    assert line["resolved"]["qa"]["model"] == {"value": "opus", "source": "policy:role"}


def test_the_fake_harness_is_started_with_the_routed_model(world, monkeypatch):
    """Kills N1 at the process boundary: the command the real `HarnessDispatcher.dispatch` hands
    the (stood-in) agent CLI carries the policy's model and effort for QA."""
    policy_file = _write(world.tmp / "policy.json", _doc())
    assert world.run("--routing-policy", str(policy_file))[0] == 0
    commands: list[list[str]] = []

    def agent(command, **kw):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    class Proc:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        run = staticmethod(agent)

    monkeypatch.setattr(dispatchers, "subprocess", Proc())
    d = world.built[0]
    d.dispatch(Role.QA, "a prompt", state=world.state)
    (command,) = commands
    assert command[:2] == ["kimi", "-p"]
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"


def test_the_pin_is_in_the_run_directory_before_the_first_dispatch(world):
    """Kills N2 (the record is not written) and N19 (the record is written after the controller
    exists, i.e. possibly after a dispatch)."""
    policy_file = _write(world.tmp / "policy.json", _doc())
    assert not world.pin_path.exists()
    rc, _out, err = world.run("--routing-policy", str(policy_file))
    assert rc == 0, err
    # the record was on disk at the moment the controller was built, i.e. after every refusal
    # that precedes the first dispatch and before any dispatch can happen
    assert world.pin_at_controller == [True]
    pin = world.pin()
    policy = routing.load(policy_file)
    assert pin["schema"] == "hoh-routing-pin/1" and pin["digest"] == policy.digest
    assert pin["file_sha256"] == policy.file_sha256
    assert pin["source"] == str(policy_file) and os.path.isabs(pin["source"])
    assert pin["pinned_by"] == "hoh run" and pin["pinned_at"]
    assert pin["policy"]["roles"]["qa"] == {"provider": "kimi", "model": "opus"}


def test_a_relative_path_is_pinned_as_an_absolute_one(world, monkeypatch):
    """Kills N10b (the source is recorded as typed: a resumed run in another directory would
    read another file)."""
    _write(world.tmp / "policy.json", _doc())
    monkeypatch.chdir(world.tmp)
    assert world.run("--routing-policy", "policy.json")[0] == 0
    assert world.pin()["source"] == str(world.tmp / "policy.json")


def test_without_a_policy_nothing_is_routed_and_nothing_is_written(world):
    """Kills N9 (a changed default profile): a run that declared nothing is the historical run,
    byte for byte -- no record, `routing=None`, the three defaults, no output."""
    rc, out, err = world.run()
    assert rc == 0, err
    assert world.seen[0]["routing"] is None and not world.pin_path.exists()
    d = world.built[0]
    assert d.profiles == {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}
    assert d.models == {} and d.efforts == {} and d.routing_evidence is None
    assert "routing_policy" not in out


def test_an_explicit_per_role_option_wins_and_the_record_says_it_overrode_the_policy(world):
    """The slice-1 precedence reaches the command line: `--role-model qa=haiku` over the policy's
    opus, recorded."""
    policy_file = _write(world.tmp / "policy.json", _doc())
    rc, out, err = world.run("--routing-policy", str(policy_file), "--role-model", "qa=haiku")
    assert rc == 0, err
    d = world.built[0]
    assert d.models[Role.QA] == "haiku"
    assert d.routing_evidence["resolved"]["qa"]["model"] == {
        "value": "haiku", "source": "explicit", "policy_value_overridden": "opus"}


# --------------------------------------------------------------------------- #
# the role options keep their parser defaults (repair slice P3-20s2f)
# --------------------------------------------------------------------------- #

def test_the_role_options_keep_their_parser_defaults_and_a_typed_equal_word_is_explicit(world):
    """Kills F1 (a parser default counts as typed: the provider is never filled), F2 (the default
    is a plain str: a typed word equal to it is unset), F3 (the default is None again: by default
    QA no longer runs on a different model), F5 (a typed word equal to the default is read as
    unset) and F8 (an args object without the option fails instead of taking the default)."""
    parser = cli.build_parser()
    bare = parser.parse_args(["--root", "r", "run", "r1"])
    assert (bare.planner, bare.developer, bare.qa) == ("claude", "claude", "kimi")
    assert bare.qa != bare.developer, "by default QA runs on a different model"
    assert not [v for v in (bare.planner, bare.developer, bare.qa) if cli.profile_typed(v)]
    typed = parser.parse_args(["--root", "r", "run", "r1", "--planner", "claude",
                               "--developer", "claude", "--qa", "kimi"])
    assert (typed.planner, typed.developer, typed.qa) == ("claude", "claude", "kimi")
    assert all(cli.profile_typed(v) for v in (typed.planner, typed.developer, typed.qa))
    # the one place the default is applied: an args object without the option (None) and the
    # parser's default both give the harness name, as a plain str (kills F8)
    assert cli._profile_of(Role.QA, None) == "kimi"
    assert type(cli._profile_of(Role.QA, bare.qa)) is str and cli._profile_of(Role.QA, bare.qa) == "kimi"
    # through `hoh run`: unset takes the policy's provider, the same word typed is explicit
    policy_file = _write(world.tmp / "policy.json",
                         _doc(roles={"qa": {"provider": "codex", "model": "o"}}))
    rc, _out, err = world.run("--routing-policy", str(policy_file))
    assert rc == 0, err
    assert world.built[-1].profiles[Role.QA] == "codex"
    rc, _out, err = world.run("--routing-policy", str(policy_file), "--qa", "kimi")
    assert rc == 2 and "routing policy refused" in err
    assert len(world.built) == 1


def test_a_run_without_role_options_is_the_run_it_was_before_the_slice(world):
    """The default-profile property through the real path (`cli.main run`, the real
    `build_dispatcher` call): the three defaults, QA on another harness than the developer."""
    rc, _out, err = world.run()
    assert rc == 0, err
    (kw,) = world.seen
    assert kw["profiles"] == {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}
    assert kw["profiles"][Role.QA] != kw["profiles"][Role.DEVELOPER]
    assert type(kw["profiles"][Role.QA]) is str
    assert kw["routing"] is None and not world.pin_path.exists()


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("option", ["--planner", "--developer", "--qa"])
def test_an_empty_role_option_is_refused_and_never_replaced_by_the_default(
        world, option, value):
    """Kills F4 (an empty value is replaced by the default) and F7 (only `""` is empty).
    Before the slice the empty profile reached the first dispatch and failed there; after
    Revision 1 of the slice it was silently replaced. It is refused at the start of `hoh run`,
    before the run directory is touched, with nothing built and nothing pinned."""
    policy_file = _write(world.tmp / "policy.json", _doc())
    for extra in ((), ("--routing-policy", str(policy_file))):
        rc, _out, err = world.run(*extra, f"{option}={value}")
        assert rc == 2 and "names no harness" in err and option in err, (extra, err)
        assert world.seen == [] and not world.pin_path.exists()


# --------------------------------------------------------------------------- #
# provider: an unset profile takes the policy's, an explicit different one is refused
# --------------------------------------------------------------------------- #

def test_an_unset_profile_takes_the_policys_provider_and_an_explicit_other_one_is_refused(world):
    """Kills N6 (the provider is not filled in) and N5 (the pin is written before the policy
    is found not to fit the run)."""
    policy_file = _write(world.tmp / "policy.json",
                         _doc(roles={"qa": {"provider": "codex", "model": "o"}}))
    rc, _out, err = world.run("--routing-policy", str(policy_file), "--qa", "kimi")
    assert rc == 2 and "routing policy refused" in err and "qa" in err
    assert not world.pin_path.exists() and world.seen == []
    rc, _out, err = world.run("--routing-policy", str(policy_file))
    assert rc == 0, err
    assert world.built[-1].profiles[Role.QA] == "codex"
    assert world.built[-1].profiles[Role.PLANNER] == "claude"
    assert world.pin_path.exists()


def test_an_explicit_profile_that_matches_the_policys_is_accepted(world):
    policy_file = _write(world.tmp / "policy.json",
                         _doc(roles={"qa": {"provider": "codex", "model": "o"}}))
    rc, _out, err = world.run("--routing-policy", str(policy_file), "--qa", "codex")
    assert rc == 0, err
    assert world.built[0].profiles[Role.QA] == "codex"


def test_every_role_takes_its_provider_from_the_policy_when_its_option_is_unset(world):
    """Kills N20 (the fill is restricted to one role)."""
    policy_file = _write(world.tmp / "policy.json", _doc(
        roles={"planner": {"provider": "pi", "model": "p"},
               "developer": {"provider": "codex", "model": "d"},
               "qa": {"provider": "claude", "model": "q"}}))
    rc, _out, err = world.run("--routing-policy", str(policy_file))
    assert rc == 0, err
    assert world.built[0].profiles == {Role.PLANNER: "pi", Role.DEVELOPER: "codex",
                                       Role.QA: "claude"}
    # an explicit option stays what it is: --developer claude clashes with the policy's codex
    rc, _out, err = world.run("--routing-policy", str(policy_file), "--developer", "claude")
    assert rc == 2 and "routing policy refused" in err and "developer" in err


def test_a_refused_invocation_leaves_no_pin_whatever_it_is_refused_by(world, monkeypatch):
    """Kills N5 and N19b (the pin is written before the refusals that follow the policy: an
    approval policy, an autonomy contract, the dispatcher, the isolation). A later invocation
    with another policy then binds normally."""
    from hoh import sandbox

    first = _write(world.tmp / "first.json", _doc())
    second = _write(world.tmp / "second.json", _doc(default={"model": "haiku", "effort": "low"}))
    missing = str(world.tmp / "nowhere.json")
    refusals = {
        "approval policy": ["--approval-policy", missing],
        "autonomy contract": ["--autonomy", missing],
        "isolation": ["--isolation", "strict"],
    }

    def unavailable(isolation):
        raise sandbox.SandboxUnavailable("no sandbox on this machine")

    real_select, real_available = sandbox.select, herdr.available
    monkeypatch.setattr(sandbox, "select", unavailable)
    for label, extra in refusals.items():
        rc, _out, err = world.run("--routing-policy", str(first), *extra)
        assert rc == 2, (label, err)
        assert not world.pin_path.exists(), label
    # the dispatcher itself refuses: Herdr is asked for and is not there
    monkeypatch.setattr(herdr, "available", lambda: False)
    rc = cli.main(["--root", world.root, "run", "r1", "--iterations", "0",
                   "--routing-policy", str(first)])
    world.capsys.readouterr()
    assert rc == 2 and not world.pin_path.exists()
    monkeypatch.setattr(sandbox, "select", real_select)
    monkeypatch.setattr(herdr, "available", real_available)
    # nothing was bound, so another policy binds
    assert world.run("--routing-policy", str(second))[0] == 0
    assert world.pin()["digest"] == routing.load(second).digest


# --------------------------------------------------------------------------- #
# resume: the digest is pinned
# --------------------------------------------------------------------------- #

def test_a_resumed_run_obeys_the_policy_it_began_under_and_refuses_another(world):
    """Kills N3 (digest not compared), N11 (a pinned run with no option is run unrouted: a
    silent re-route) and N13 (a changed file under the pinned name is applied)."""
    a = _write(world.tmp / "a.json", _doc())
    assert world.run("--routing-policy", str(a))[0] == 0
    first = world.pin_path.read_bytes()
    digest = world.pin()["digest"]

    # no option: the run adopts the policy it is bound to
    rc, out, err = world.run()
    assert rc == 0, err
    d = world.built[-1]
    assert d.models[Role.QA] == "opus" and d.routing_evidence["digest"] == digest
    assert [o["routing_policy"]["pinned_before"]
            for o in _objects(out) if "routing_policy" in o] == [True]

    # the same content from another path is the same policy
    b_same = _write(world.tmp / "same-content.json", json.dumps(_doc(), indent=4).encode())
    assert world.run("--routing-policy", str(b_same))[0] == 0

    # another policy offered: refused by name, nothing built, nothing re-pinned
    built_before = len(world.built)
    other = _write(world.tmp / "b.json", _doc(default={"model": "haiku", "effort": "low"}))
    rc, _out, err = world.run("--routing-policy", str(other))
    assert rc == 2 and "routing policy changed" in err
    assert digest[:12] in err and routing.load(other).digest[:12] in err
    assert len(world.built) == built_before and world.pin_path.read_bytes() == first

    # the pinned file edited in place: a run with no option refuses too
    _write(a, _doc(default={"model": "haiku", "effort": "low"}))
    rc, _out, err = world.run()
    assert rc == 2 and "routing policy changed" in err
    assert len(world.built) == built_before and world.pin_path.read_bytes() == first


def test_a_pinned_policy_file_that_is_gone_or_unreadable_refuses_by_name(world):
    a = _write(world.tmp / "a.json", _doc())
    assert world.run("--routing-policy", str(a))[0] == 0
    gone = world.tmp / "moved.json"
    a.rename(gone)
    rc, _out, err = world.run()
    assert rc == 2 and "routing policy changed" in err and "cannot be read again" in err
    gone.rename(a)
    a.chmod(0)
    try:
        if not os.access(a, os.R_OK):
            rc, _out, err = world.run()
            assert rc == 2 and "cannot be read again" in err
    finally:
        a.chmod(0o600)


@pytest.mark.parametrize("label", ["link", "directory", "fifo", "corrupt", "list", "schema",
                                   "digest", "digest-newline", "relative", "oversize", "latin1",
                                   "second-name"])
def test_a_routing_record_that_is_not_a_pin_refuses_and_dispatches_nothing(world, label):
    """Kills N4 (an unreadable pin is read as no pin) and N10 (a relative source is accepted).
    The refusal names the record and no dispatcher is built."""
    real = {"schema": "hoh-routing-pin/1", "digest": "a" * 64, "source": "/x/p.json"}
    path = world.pin_path
    if label == "link":
        target = world.tmp / "elsewhere.json"
        _write(target, real)
        path.symlink_to(target)
    elif label == "directory":
        path.mkdir()
    elif label == "fifo":
        os.mkfifo(path)
    elif label == "second-name":
        _write(path, real)
        os.link(path, world.tmp / "another-name.json")
    else:
        body = {"corrupt": b'{"schema": ', "list": b"[]",
                "schema": json.dumps({**real, "schema": "x"}).encode(),
                "digest": json.dumps({**real, "digest": "A" * 64}).encode(),
                "digest-newline": json.dumps({**real, "digest": "a" * 64 + "\n"}).encode(),
                "relative": json.dumps({**real, "source": "p.json"}).encode(),
                "oversize": json.dumps({**real, "pad": "x" * (70 * 1024)}).encode(),
                "latin1": json.dumps({**real, "note": "café"}, ensure_ascii=False)
                .encode("latin-1")}[label]
        _write(path, body)
    rc, _out, err = world.run()
    assert rc == 2 and cli.ROUTING_RECORD in err, (label, err)
    assert world.seen == []


def test_the_routing_record_ceiling_is_exactly_64_kib(world):
    """Kills N15b (the bound moved by one) and N15c (widened): a pin of exactly 64 KiB is read
    (the run then refuses only because the file it names is not there), one byte more is not."""
    real = {"schema": "hoh-routing-pin/1", "digest": "a" * 64, "source": "/x/p.json", "pad": ""}
    room = 64 * 1024 - len(json.dumps(real).encode())
    exact = json.dumps({**real, "pad": "x" * room}).encode()
    assert len(exact) == 64 * 1024
    _write(world.pin_path, exact)
    rc, _out, err = world.run()
    assert rc == 2 and "cannot be read again" in err
    _write(world.pin_path, json.dumps({**real, "pad": "x" * (room + 1)}).encode())
    rc, _out, err = world.run()
    assert rc == 2 and "larger than 65536 bytes" in err


# --------------------------------------------------------------------------- #
# the policy offered is refused the way slice 1 refuses it
# --------------------------------------------------------------------------- #

def test_an_invalid_policy_is_refused_before_the_dispatcher_and_pins_nothing(world):
    fifo = world.tmp / "fifo.json"
    os.mkfifo(fifo)
    bad = {"missing": world.tmp / "nowhere.json", "directory": world.tmp, "fifo": fifo,
           "unknown key": _write(world.tmp / "k.json", _doc(role={})),
           "key": _write(world.tmp / "s.json", _doc(default={"model": "sk-ant-api03-AbCdEf0123456789xyz"}))}
    for label, path in bad.items():
        rc, out, err = world.run("--routing-policy", str(path))
        assert rc == 2 and "routing policy refused" in err, (label, err)
        assert "AbCdEf0123456789xyz" not in out + err
    rc, _out, err = world.run("--routing-policy", "")
    assert rc == 2 and "routing policy refused" in err
    assert world.seen == [] and not world.pin_path.exists()


# --------------------------------------------------------------------------- #
# hoh start
# --------------------------------------------------------------------------- #

def test_hoh_start_validates_the_policy_before_the_run_exists_and_pins_it(
        tmp_path, repo, monkeypatch, capsys):
    """Kills N8 (a refused policy at the start leaves a run behind or starts it unrouted)."""
    monkeypatch.setattr(cli, "_register_trust", lambda *a, **kw: None)
    spec = _write(tmp_path / "spec.md", b"# spec\n")
    root = tmp_path / "runs"
    base = ["--root", str(root), "start", "--repo", str(repo), "--spec", str(spec)]
    bad = _write(tmp_path / "bad.json", _doc(roles={"nobody": {"model": "x"}}))
    rc = cli.main([*base, "--run-id", "s0", "--routing-policy", str(bad)])
    assert rc == 2 and "routing policy refused" in capsys.readouterr().err
    assert not (root / "s0").exists()
    good = _write(tmp_path / "good.json", _doc())
    rc = cli.main([*base, "--run-id", "s1", "--routing-policy", str(good)])
    out = capsys.readouterr().out
    assert rc == 0 and [o for o in _objects(out) if "routing_policy" in o]
    pin = json.loads((root / "s1" / cli.ROUTING_RECORD).read_text(encoding="utf-8"))
    assert pin["digest"] == routing.load(good).digest and pin["pinned_by"] == "hoh start"
    assert pin["source"] == str(good)
    # without the option no record is made
    assert cli.main([*base, "--run-id", "s2"]) == 0
    capsys.readouterr()
    assert not (root / "s2" / cli.ROUTING_RECORD).exists()
    # a relative path is made absolute at the start: a pin that named `good.json` would be read
    # against whichever directory the later `hoh run` has (kills N21)
    monkeypatch.chdir(tmp_path)
    assert cli.main([*base, "--run-id", "s3", "--routing-policy", "good.json"]) == 0
    capsys.readouterr()
    pin = json.loads((root / "s3" / cli.ROUTING_RECORD).read_text(encoding="utf-8"))
    assert pin["source"] == str(good)
    # a pin that cannot be written leaves a run that exists: said by name, exit 2 (kills N22)
    real_write = cli._write_record
    monkeypatch.setattr(cli, "_write_record", lambda path, payload: 2)
    rc = cli.main([*base, "--run-id", "s4", "--routing-policy", str(good)])
    err = capsys.readouterr().err
    assert rc == 2 and "s4 EXISTS and is NOT bound" in err and "--routing-policy" in err
    assert (root / "s4").is_dir() and not (root / "s4" / cli.ROUTING_RECORD).exists()
    monkeypatch.setattr(cli, "_write_record", real_write)


def test_a_run_started_under_a_policy_runs_under_it_without_the_option(world, repo):
    """The start's pin is the run's pin: `hoh run` with no option adopts it (this is the path a
    resume loop and the launcher take: they type no routing option)."""
    spec = _write(world.tmp / "spec2.md", b"# spec\n")
    good = _write(world.tmp / "good.json", _doc())
    assert cli.main(["--root", world.root, "start", "--repo", str(repo), "--spec", str(spec),
                     "--run-id", "s1", "--routing-policy", str(good)]) == 0
    world.capsys.readouterr()
    rc, out, err = world.run(run_id="s1")
    assert rc == 0, err
    assert world.built[-1].models[Role.QA] == "opus"
    assert [o["routing_policy"]["pinned_before"]
            for o in _objects(out) if "routing_policy" in o] == [True]


# --------------------------------------------------------------------------- #
# --detach: the option is forwarded, an unset role option is not typed, a changed policy refuses
# --------------------------------------------------------------------------- #

def _parse(*argv):
    return cli.build_parser().parse_args(["--root", "runs", *argv])


def test_the_detached_command_carries_the_policy_and_types_no_unset_role_option(tmp_path,
                                                                                  monkeypatch):
    """Kills N7 (the option is dropped, or `--planner=None` is typed for an unset role)."""
    monkeypatch.chdir(tmp_path)
    args = _parse("run", "r1", "--detach", "--routing-policy", "p.json")
    argv = detach.run_argv(args, ["python3", "-m", "hoh.cli"])
    assert f"--routing-policy={tmp_path / 'p.json'}" in argv
    assert not [w for w in argv if w.startswith(("--planner", "--developer", "--qa"))]
    again = cli.build_parser().parse_args(argv[3:])
    assert again.routing_policy == str(tmp_path / "p.json")
    # the tab's parser gives the same defaults, and they are still "not typed"
    assert (again.planner, again.developer, again.qa) == ("claude", "claude", "kimi")
    assert not [v for v in (again.planner, again.developer, again.qa) if cli.profile_typed(v)]
    explicit = _parse("run", "r1", "--detach", "--qa", "codex")
    argv = detach.run_argv(explicit, ["python3", "-m", "hoh.cli"])
    assert "--qa=codex" in argv and not [w for w in argv if w.startswith("--planner")]
    plain = detach.run_argv(_parse("run", "r1", "--detach"), ["python3", "-m", "hoh.cli"])
    assert not [w for w in plain if w.startswith("--routing-policy")]
    # a path the tab's shell would have to quote is refused, as for every other path option
    spaced = detach.run_argv(_parse("run", "r1", "--detach", "--routing-policy", "a b.json"),
                             ["python3", "-m", "hoh.cli"])
    with pytest.raises(detach.DetachRefused):
        detach.command_text(spaced, "/x/log")


def test_the_defaults_stay_unset_when_the_parser_is_a_second_copy_of_the_module(tmp_path):
    """Kills F9 (`profile_typed` compares class identity). `python3 -m hoh.cli` runs the module as
    `__main__` while `detach` imports `hoh.cli`: two copies, two `_UnsetProfile` classes. A parser
    from a second copy must still read as "not typed" for `detach.run_argv`, or every default is
    typed into the detached tab and a policy's provider is never filled there."""
    other = runpy.run_module("hoh.cli", run_name="hoh_cli_second_copy")
    assert other["_UnsetProfile"] is not cli._UnsetProfile
    parser = other["build_parser"]()
    args = parser.parse_args(["--root", "runs", "run", "r1", "--detach",
                              "--routing-policy", "p.json"])
    assert (args.planner, args.developer, args.qa) == ("claude", "claude", "kimi")
    argv = detach.run_argv(args, ["python3", "-m", "hoh.cli"])
    assert not [w for w in argv if w.startswith(("--planner", "--developer", "--qa"))]
    typed = parser.parse_args(["--root", "runs", "run", "r1", "--detach", "--qa", "kimi"])
    assert "--qa=kimi" in detach.run_argv(typed, ["python3", "-m", "hoh.cli"])


def test_python_dash_m_hoh_cli_detach_types_only_what_was_typed(tmp_path, monkeypatch):
    """Kills F9 on the real entry: the module run as `__main__` (`python3 -m hoh.cli`, the form
    docs/OPERATIONS.md names) parses `run r1 --detach`; `detach.cmd_detach` is replaced by a spy
    (no tab, no agent) that feeds the parsed namespace to the real `detach.run_argv`."""
    typed: list[list[str]] = []
    monkeypatch.setattr(detach, "cmd_detach",
                        lambda args: typed.append(detach.run_argv(args, ["python3", "-m", "hoh.cli"])) or 0)
    monkeypatch.setattr(sys, "argv", ["hoh.cli", "--root", str(tmp_path / "runs"), "run", "r1",
                                      "--detach", "--routing-policy", "p.json", "--developer", "pi"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("hoh.cli", run_name="__main__")
    assert stopped.value.code == 0
    (argv,) = typed
    assert "--developer=pi" in argv
    assert not [w for w in argv if w.startswith(("--planner", "--qa"))]
    assert [w for w in argv if w.startswith("--routing-policy=")]


@pytest.mark.parametrize("option", ["--planner", "--developer", "--qa"])
def test_a_blank_role_option_under_detach_is_refused_before_a_tab_is_opened(world, option):
    """Kills F10 (the detach preflight does not apply the role-option refusal to a blank value;
    before it, `--qa "   "` was typed into the tab and refused there). Blank: the words `hoh run`
    gives; empty: refused by `detach` as before (its own words are pinned by `tests/test_detach.py`).
    Nothing typed."""
    for value, words in (("   ", "names no harness"), ("", "run --detach refused")):
        rc = cli.main(["--root", world.root, "run", "r1", "--detach", f"{option}={value}"])
        err = world.capsys.readouterr().err
        assert rc == 2 and "run --detach refused" in err and words in err, (value, err)
    assert world.seen == []


def test_detach_refuses_a_changed_policy_before_any_tab_is_opened(world):
    """Kills N14 (the detach preflight does not look at the routing record)."""
    a = _write(world.tmp / "a.json", _doc())
    assert world.run("--routing-policy", str(a))[0] == 0
    other = _write(world.tmp / "b.json", _doc(default={"model": "haiku", "effort": "low"}))
    args = cli.build_parser().parse_args(["--root", world.root, "run", "r1", "--detach",
                                          "--routing-policy", str(other)])
    with pytest.raises(detach.DetachRefused) as caught:
        detach.preflight(args, world.store)
    assert "routing policy" in str(caught.value) and "changed" in str(caught.value)
    # and through the command: `hoh run --detach` exits 2 with the same words, no tab opened
    rc = cli.main(["--root", world.root, "run", "r1", "--detach", "--routing-policy", str(other)])
    err = world.capsys.readouterr().err
    assert rc == 2 and "run --detach refused" in err and "routing policy changed" in err


def test_the_run_option_set_is_the_one_the_detach_drift_guard_knows():
    """`--routing-policy` is a `hoh run` option and a `hoh start` option and nothing else's."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name in ("run", "start"):
        flags = {s for act in sub.choices[name]._actions for s in act.option_strings}
        assert "--routing-policy" in flags, name
