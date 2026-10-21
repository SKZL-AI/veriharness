"""P3-20 slice 1: a declared routing policy decides each role's model and effort, and nothing else
does it silently.

`docs/ROUTING.md` keeps routing *recommendations* deferred on evidence; these tests are about
the mechanism a policy the operator wrote goes through: what is refused, what wins over what,
that it reaches the argument list a role is started with and the dispatch record, and that
without a policy nothing changes. Nothing here starts Herdr or an agent.

Mutant ids (M1..) are the spec's table; each test says which it kills.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import threading
from pathlib import Path

import pytest

from hoh import herdr, routing
from hoh.contracts import Role
from hoh.dispatchers import HarnessDispatcher, build_dispatcher
from hoh.routing import RoutingRefused
from hoh.telemetry import NOT_AVAILABLE

PROFILES = {Role.PLANNER: "claude", Role.DEVELOPER: "claude", Role.QA: "kimi"}


def _doc(**over) -> dict:
    doc = {"schema": routing.SCHEMA,
           "default": {"model": "sonnet", "effort": "high"},
           "roles": {"qa": {"model": "opus"}}}
    doc.update(over)
    return doc


def _write(directory: Path, content, name: str = "routing.json") -> Path:
    path = directory / name
    path.write_bytes(content if isinstance(content, bytes)
                     else json.dumps(content).encode("utf-8"))
    return path


def _refused(doc) -> bool:
    try:
        routing.parse(doc)
    except RoutingRefused:
        return True
    return False


def _load_refused(path) -> bool:
    try:
        routing.load(path)
    except RoutingRefused:
        return True
    return False


# --------------------------------------------------------------------------- #
# What a policy decides, and what wins
# --------------------------------------------------------------------------- #

def test_the_default_and_the_role_entries_decide_each_roles_model_and_effort():
    """Kills M3 (a role entry replaces the default whole, or the default wins over it)."""
    got = routing.resolve(routing.parse(_doc()), profiles=PROFILES)
    assert got.models == {Role.PLANNER: "sonnet", Role.DEVELOPER: "sonnet", Role.QA: "opus"}
    assert got.efforts == {Role.PLANNER: "high", Role.DEVELOPER: "high", Role.QA: "high"}
    seen = got.evidence["resolved"]
    assert seen["qa"]["model"] == {"value": "opus", "source": "policy:role"}
    assert seen["qa"]["effort"] == {"value": "high", "source": "policy:default"}
    assert seen["planner"]["model"] == {"value": "sonnet", "source": "policy:default"}
    assert seen["qa"]["provider"] == {"value": "kimi", "source": "run-profile"}


def test_a_role_the_policy_does_not_reach_keeps_the_harness_default_and_says_so():
    """Kills M12 (an invented value for a role nobody declared)."""
    doc = {"schema": routing.SCHEMA, "roles": {"qa": {"effort": "max"}}}
    got = routing.resolve(routing.parse(doc), profiles=PROFILES)
    assert got.models == {} and got.efforts == {Role.QA: "max"}
    planner = got.evidence["resolved"]["planner"]
    assert planner["model"] == {"value": NOT_AVAILABLE, "source": "harness-default"}
    assert planner["effort"] == {"value": NOT_AVAILABLE, "source": "harness-default"}


def test_an_explicit_per_role_value_wins_and_the_overridden_policy_value_is_recorded():
    """Kills M1 (the policy beats `--role-model` / `--role-effort`)."""
    policy = routing.parse(_doc())
    got = routing.resolve(policy, profiles=PROFILES,
                          models={Role.QA: "haiku", Role.PLANNER: "sonnet"},
                          efforts={Role.DEVELOPER: "low"})
    assert got.models[Role.QA] == "haiku" and got.models[Role.DEVELOPER] == "sonnet"
    assert got.efforts[Role.DEVELOPER] == "low" and got.efforts[Role.QA] == "high"
    seen = got.evidence["resolved"]
    assert seen["qa"]["model"] == {"value": "haiku", "source": "explicit",
                                   "policy_value_overridden": "opus"}
    assert seen["planner"]["model"] == {"value": "sonnet", "source": "explicit"}
    assert seen["developer"]["effort"] == {"value": "low", "source": "explicit",
                                           "policy_value_overridden": "high"}


def test_a_provider_the_run_does_not_configure_is_refused_and_a_match_is_recorded():
    """Kills M4 (the declared provider is not compared with the run's profile)."""
    doc = {"schema": routing.SCHEMA, "roles": {"qa": {"provider": "kimi", "model": "k2"}}}
    policy = routing.parse(doc)
    ok = routing.resolve(policy, profiles=PROFILES)
    assert ok.evidence["resolved"]["qa"]["provider"] == {"value": "kimi",
                                                         "source": "policy:role"}
    # a profile that is not one of `routing.PROVIDERS` (the CLI takes any word) can never be
    # asserted: the policy refuses it too, so such a run fails closed
    for profiles in ({**PROFILES, Role.QA: "claude"}, {**PROFILES, Role.QA: "gpt"},
                     {Role.PLANNER: "claude"}, {}):
        with pytest.raises(RoutingRefused):
            routing.resolve(policy, profiles=profiles)


def test_resolve_leaves_its_inputs_as_they_were_and_returns_new_tables():
    """Kills M11 (the caller's tables are edited in place)."""
    profiles, models, efforts = dict(PROFILES), {Role.QA: "haiku"}, {Role.QA: "low"}
    before = copy.deepcopy((profiles, models, efforts))
    got = routing.resolve(routing.parse(_doc()), profiles=profiles, models=models,
                          efforts=efforts)
    assert (profiles, models, efforts) == before
    assert got.models is not models and got.efforts is not efforts


# --------------------------------------------------------------------------- #
# What is refused: the document
# --------------------------------------------------------------------------- #

def test_a_document_that_declares_less_or_more_than_it_looks_like_is_refused():
    """Kills M8 (role names normalised), M9 (any effort word), M5' (unknown keys ignored)."""
    ok = {"schema": routing.SCHEMA, "default": {"model": "sonnet"}}
    assert not _refused(ok)
    bad = {
        "not an object (list)": [],
        "not an object (null)": None,
        "not an object (str)": "x",
        "schema missing": {"default": {"model": "sonnet"}},
        "schema other": {**ok, "schema": "hoh-routing-policy/2"},
        "unknown top-level key": {**ok, "role": {"qa": {"model": "opus"}}},
        "unknown role entry key": {**ok, "roles": {"qa": {"modle": "opus"}}},
        "provider in default": {**ok, "default": {"provider": "claude"}},
        "role Qa": {**ok, "roles": {"Qa": {"model": "opus"}}},
        "role QA": {**ok, "roles": {"QA": {"model": "opus"}}},
        "role ' qa'": {**ok, "roles": {" qa": {"model": "opus"}}},
        "role 'qa\\n'": {**ok, "roles": {"qa\n": {"model": "opus"}}},
        "role all": {**ok, "roles": {"all": {"model": "opus"}}},
        "role empty": {**ok, "roles": {"": {"model": "opus"}}},
        "role entry empty": {**ok, "roles": {"qa": {}}},
        "role entry not an object": {**ok, "roles": {"qa": "opus"}},
        "roles not an object": {**ok, "roles": []},
        "default empty": {"schema": routing.SCHEMA, "default": {}},
        "default empty beside a role entry": {"schema": routing.SCHEMA, "default": {},
                                              "roles": {"qa": {"model": "opus"}}},
        "default not an object": {"schema": routing.SCHEMA, "default": "sonnet"},
        "declares nothing": {"schema": routing.SCHEMA},
        "declares nothing (empty roles)": {"schema": routing.SCHEMA, "roles": {}},
        "provider unknown": {**ok, "roles": {"qa": {"provider": "gpt"}}},
        "provider case": {**ok, "roles": {"qa": {"provider": "Claude"}}},
        "provider null": {**ok, "roles": {"qa": {"provider": None}}},
        "effort case": {**ok, "default": {"effort": "HIGH"}},
        "effort space": {**ok, "default": {"effort": "high "}},
        "effort newline": {**ok, "default": {"effort": "high\n"}},
        "effort unknown": {**ok, "default": {"effort": "ultra"}},
        "effort number": {**ok, "default": {"effort": 3}},
        "model number": {**ok, "default": {"model": 7}},
        "model empty": {**ok, "default": {"model": ""}},
        "model leading dash": {**ok, "default": {"model": "-sonnet"}},
        "model flag": {**ok, "default": {"model": "--effort"}},
        "model space": {**ok, "default": {"model": "a b"}},
        "model newline": {**ok, "default": {"model": "sonnet\n"}},
        "model NUL": {**ok, "default": {"model": "so\x00net"}},
        "model non-ASCII": {**ok, "default": {"model": "sonnét"}},
        "model lone surrogate": {**ok, "default": {"model": "\ud800"}},
        "model the stated unknown": {**ok, "default": {"model": NOT_AVAILABLE}},
        "model 65 characters": {**ok, "default": {"model": "model-" * 10 + "abcde"}},
    }
    wrongly_accepted = [label for label, doc in bad.items() if not _refused(doc)]
    assert wrongly_accepted == []
    assert not _refused({**ok, "default": {"model": "model-" * 10 + "abcd"}})
    for effort in routing.EFFORTS:
        assert not _refused({**ok, "default": {"effort": effort}})


def test_a_model_that_looks_like_a_credential_is_refused_and_never_echoed():
    """Kills M10 (the credential-shape check is dropped), M29 (a refusal quotes what it read)."""
    secrets = ["sk-ant-api03-AbCdEf0123456789xyz", "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
               "AKIAIOSFODNN7EXAMPLE", "my-secret-1", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4",
               "eyJhbGciOiJIUzI1NiJ9"]
    for secret in secrets:
        with pytest.raises(RoutingRefused) as err:
            routing.parse({"schema": routing.SCHEMA, "default": {"model": secret}})
        assert secret not in str(err.value)
    key = secrets[0]
    for doc in ({"schema": routing.SCHEMA, "default": {key: "x"}},
                {"schema": routing.SCHEMA, "roles": {key: {"model": "opus"}}},
                {"schema": routing.SCHEMA, "roles": {"qa": {key: "x"}}},
                {key: 1}):
        with pytest.raises(RoutingRefused) as err:
            routing.parse(doc)
        assert key not in str(err.value)
    for name in ("sonnet", "claude-opus-5-5", "claude-sonnet-5-5", "opus[1m]", "kimi-k2",
                 "gpt-4.1", "org/qwen3-coder:30b"):
        assert not _refused({"schema": routing.SCHEMA, "default": {"model": name}}), name


# --------------------------------------------------------------------------- #
# What is refused: the file
# --------------------------------------------------------------------------- #

def test_a_duplicate_key_is_refused_rather_than_the_last_one_applied(tmp_path):
    """Kills M5 (`json.loads` keeps the last of two equal keys)."""
    twice = ('{"schema": "%s", "roles": {"qa": {"model": "cheap"}, "qa": {"model": "opus"}}}'
             % routing.SCHEMA)
    assert _load_refused(_write(tmp_path, twice.encode()))
    schema_twice = ('{"schema": "x", "schema": "%s", "default": {"model": "sonnet"}}'
                    % routing.SCHEMA)
    assert _load_refused(_write(tmp_path, schema_twice.encode(), "b.json"))
    assert routing.load(_write(tmp_path, _doc(), "c.json")).default.model == "sonnet"


def _load_within(path, seconds: float = 10.0):
    """`routing.load` on a thread: an open that would block fails the test instead of
    hanging it."""
    box: list = []
    worker = threading.Thread(target=lambda: box.append(_load_refused(path)), daemon=True)
    worker.start()
    worker.join(seconds)
    return box[0] if box else None


def test_a_file_that_is_not_one_readable_regular_file_is_refused(tmp_path):
    """Kills M6 (a link is followed), M13 (a FIFO is opened blocking), M32 (the regular-file
    check is dropped: a FIFO that holds a valid policy is read)."""
    good = _write(tmp_path, _doc(), "good.json")
    assert routing.load(good).default.model == "sonnet"
    assert _load_refused(tmp_path / "missing.json")
    assert _load_refused(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(good)
    assert _load_refused(link)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "nowhere.json")
    assert _load_refused(dangling)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    assert _load_within(fifo) is True
    # a FIFO that holds a complete, valid policy and has no writer left: only the regular-file
    # check refuses it (the read would otherwise succeed and reach EOF)
    stocked = tmp_path / "stocked.json"
    os.mkfifo(stocked)
    held = os.open(stocked, os.O_RDONLY | os.O_NONBLOCK)
    try:
        writer = os.open(stocked, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, json.dumps(_doc()).encode("utf-8"))
        finally:
            os.close(writer)
        assert _load_within(stocked) is True
    finally:
        os.close(held)
    for bad in (None, "", "a\x00b", b"routing.json", 7):
        with pytest.raises(RoutingRefused):
            routing.load(bad)


def test_a_file_that_is_not_one_utf8_json_object_is_refused(tmp_path):
    """Kills M14 (a BOM-tolerant decoder), M5'' (non-objects, NaN or trailing text accepted),
    M31 (the parser's `RecursionError` is not caught: nesting under the size ceiling that
    reaches the recursion limit escapes as a bare exception)."""
    text = json.dumps(_doc())
    cases = {
        "empty": b"",
        "latin-1": text.replace("sonnet", "café").encode("latin-1"),
        "utf-16": text.encode("utf-16"),
        "utf-8 BOM": b"\xef\xbb\xbf" + text.encode("utf-8"),
        "NUL": text.encode().replace(b"sonnet", b"son\x00net"),
        "invalid utf-8": text.encode().replace(b"sonnet", b"son\xffnet"),
        "corrupt": b'{"schema": ',
        "array": b"[]",
        "string": b'"x"',
        "null": b"null",
        "number": b"1",
        "NaN": ('{"schema": "%s", "default": NaN}' % routing.SCHEMA).encode(),
        "nesting over the size ceiling": b"[" * 200000,
        "nesting under the size ceiling (lists)": b"[" * 20000 + b"]" * 20000,
        "nesting under the size ceiling (objects)": b'{"":' * 12000 + b"1" + b"}" * 12000,
        "trailing text": text.encode() + b" x",
    }
    under = [label for label in cases if label.startswith("nesting under")]
    assert len(under) == 2 and all(len(cases[k]) <= routing.MAX_BYTES for k in under), (
        "the nesting cases must reach the parser, not the size ceiling")
    accepted = [label for label, raw in cases.items()
                if not _load_refused(_write(tmp_path, raw, "case.json"))]
    assert accepted == []


def test_a_file_without_read_permission_is_refused(tmp_path):
    """Pins: an unreadable file refuses (no mutant of its own: the clause that refuses a missing
    file is the same one, and `test_a_file_that_is_not_one_readable_regular_file_is_refused`
    kills its removal)."""
    path = _write(tmp_path, _doc())
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("this process can read a mode-000 file (superuser)")
        assert _load_refused(path)
    finally:
        path.chmod(0o600)


def test_the_size_ceiling_is_exactly_max_bytes(tmp_path):
    """Kills M7 (the bound moved by one, widened or removed)."""
    assert routing.MAX_BYTES == 64 * 1024
    body = json.dumps(_doc()).encode()
    exact = body + b" " * (routing.MAX_BYTES - len(body))
    assert len(exact) == routing.MAX_BYTES
    assert routing.load(_write(tmp_path, exact, "exact.json")).default.model == "sonnet"
    assert _load_refused(_write(tmp_path, exact + b" ", "over.json"))
    assert _load_refused(_write(tmp_path, exact + b" " * routing.MAX_BYTES, "far.json"))


def test_the_digest_binds_the_declaration_and_the_file_hash_binds_the_bytes(tmp_path):
    """Kills M16 (a digest over something the content does not decide)."""
    one = routing.load(_write(tmp_path, _doc(), "one.json"))
    spaced = routing.load(_write(
        tmp_path, json.dumps(dict(reversed(list(_doc().items()))), indent=4).encode(), "two.json"))
    other = routing.load(_write(
        tmp_path, _doc(default={"model": "haiku", "effort": "high"}), "three.json"))
    assert len(one.digest) == 64 and set(one.digest) <= set("0123456789abcdef")
    assert one.digest == spaced.digest and one.file_sha256 != spaced.file_sha256
    assert one.digest != other.digest
    assert one.evidence()["source"] == str(tmp_path / "one.json")
    assert one.evidence()["file_sha256"] == one.file_sha256


def test_the_policy_is_read_only_after_parse():
    """Kills M34 (`roles` is a plain dict: an assignment after `parse` changes the routing
    while the digest stays)."""
    policy = routing.parse(_doc())
    digest, qa, evidence = policy.digest, policy.route(Role.QA), policy.evidence()
    with pytest.raises(TypeError):
        policy.roles[Role.QA] = routing.Route(model="evil")
    with pytest.raises(TypeError):
        policy.roles[Role.PLANNER] = routing.Route(model="evil")
    with pytest.raises((AttributeError, TypeError)):
        policy.roles.update({Role.QA: routing.Route(model="evil")})
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.default = routing.Route(model="evil")
    policy.evidence()["roles"]["qa"]["model"] = "evil"
    assert (policy.digest, policy.route(Role.QA), policy.evidence()) == (digest, qa, evidence)


def test_the_provider_names_are_the_harness_profiles_the_dispatcher_can_start():
    """Kills M17 (the two tables drift)."""
    assert set(routing.PROVIDERS) == set(HarnessDispatcher.COMMANDS)


# --------------------------------------------------------------------------- #
# Through the dispatcher and into the record
# --------------------------------------------------------------------------- #

def _build(tmp_path: Path, **kw):
    return build_dispatcher(answers_dir=tmp_path / "run" / "answers", profiles=dict(PROFILES),
                            cwd=tmp_path, prefer_herdr=False, **kw)


def test_the_policy_reaches_the_argument_list_each_role_is_started_with(tmp_path):
    """Kills M2 (the policy is resolved and then not used)."""
    policy = routing.parse(_doc(roles={"qa": {"model": "opus", "effort": "max"}}))
    d = _build(tmp_path, routing=policy)
    assert d._agent_args(Role.QA) == ["--model", "opus", "--effort", "max"]
    assert d._agent_args(Role.PLANNER) == ["--model", "sonnet", "--effort", "high"]
    assert d.routing_evidence["digest"] == policy.digest
    assert d.routing_evidence["resolved"]["qa"]["model"]["source"] == "policy:role"
    d2 = _build(tmp_path / "again", routing=policy, models={Role.QA: "haiku"})
    assert d2._agent_args(Role.QA) == ["--model", "haiku", "--effort", "max"]


def test_without_a_policy_the_dispatcher_is_exactly_what_it_was(tmp_path):
    """Kills M12 (a routing nobody declared is applied)."""
    d = _build(tmp_path)
    assert d.models == {} and d.efforts == {} and d.routing_evidence is None
    assert d._agent_args(Role.QA) == []
    d2 = _build(tmp_path / "b", models={Role.QA: "haiku"}, efforts={Role.PLANNER: "low"})
    assert d2.models == {Role.QA: "haiku"} and d2.efforts == {Role.PLANNER: "low"}
    assert d2.routing_evidence is None


def test_a_refused_routing_builds_nothing(tmp_path, monkeypatch):
    """Kills M18 (the policy is checked after the adapter and its directories exist) and M18b
    (checked after the Herdr availability check). Herdr is stated, not inherited, so the result
    does not depend on whether the test runs inside a Herdr tab."""
    doc = {"schema": routing.SCHEMA, "roles": {"qa": {"provider": "codex"}}}
    # (use Herdr, Herdr there): the third row is where a policy checked after the Herdr
    # check would surface `DispatchError` instead of the policy's own refusal
    for use_herdr, herdr_there in ((False, True), (True, True), (True, False)):
        monkeypatch.setattr(herdr, "available", lambda there=herdr_there: there)
        monkeypatch.setattr(herdr, "require_herdr", lambda: None)
        with pytest.raises(RoutingRefused):
            build_dispatcher(answers_dir=tmp_path / "run" / "answers", profiles=dict(PROFILES),
                             cwd=tmp_path, prefer_herdr=use_herdr, routing=routing.parse(doc))
        assert not (tmp_path / "run").exists() and not (tmp_path / "_arenas").exists()


def test_a_herdr_dispatcher_carries_the_routed_tables_and_the_evidence(tmp_path, monkeypatch):
    """Kills M33 (the Herdr return path drops `routing_evidence`), M2 (resolved tables dropped)
    on the Herdr adapter, the one a real run uses."""
    from hoh.dispatchers import HerdrDispatcher

    monkeypatch.setattr(herdr, "available", lambda: True)
    monkeypatch.setattr(herdr, "require_herdr", lambda: None)
    policy = routing.parse(_doc(roles={"qa": {"model": "opus", "effort": "max"}}))
    d = build_dispatcher(answers_dir=tmp_path / "run" / "answers", profiles=dict(PROFILES),
                         cwd=tmp_path, prefer_herdr=True, routing=policy,
                         models={Role.PLANNER: "haiku"})
    assert isinstance(d, HerdrDispatcher)
    assert d._agent_args(Role.QA) == ["--model", "opus", "--effort", "max"]
    assert d._agent_args(Role.PLANNER) == ["--model", "haiku", "--effort", "high"]
    assert d.routing_evidence["digest"] == policy.digest
    assert d.routing_evidence["resolved"]["planner"]["model"]["source"] == "explicit"
    plain = build_dispatcher(answers_dir=tmp_path / "run2" / "answers", profiles=dict(PROFILES),
                             cwd=tmp_path, prefer_herdr=True)
    assert plain.routing_evidence is None and plain._agent_args(Role.QA) == []


def test_the_dispatch_record_names_the_routed_model_effort_and_provider(tmp_path):
    """Kills M2 and M19 (the record reads something other than what the role ran on)."""
    from hoh.controller import Controller
    from hoh.store import RunStore

    d = _build(tmp_path / "w", routing=routing.parse(_doc()))
    store = RunStore(tmp_path / "root", "r")
    store.dir.mkdir(parents=True, exist_ok=True)
    c = Controller.__new__(Controller)
    c.store = store
    c.dispatcher = d
    for role in ("qa", "planner"):
        c.note_dispatch(role=role, run_id="r", iteration=1, attempt=1,
                        started_at="2026-10-12T10:00:00Z", ended_at="2026-10-12T10:00:02Z",
                        backend="subprocess", outcome="ok", usage={})
    qa, planner = c.telemetry().read()
    assert (qa.provider, qa.model, qa.effort) == ("kimi", "opus", "high")
    assert (planner.provider, planner.model, planner.effort) == ("claude", "sonnet", "high")
