"""The MCP tool gateway (V3.3 P2-18): MCP1 to MCP7, plus UNKNOWN, coherence,
unregistered ids and record-keeping.

Each positive case sits next to a negative control that changes only the
condition under test and shows the verdict flipping, in the style of
tests/test_checklist.py. A gateway that blocked everything would pass every
"blocked" case here; one that allowed everything would pass every "allowed"
case. Only the pairs tell the two apart.

Every `execute` is an in-process fake that counts its calls. Nothing here
opens a socket, starts a process or writes to disk.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest
from pydantic import ValidationError

from hoh import gateway as gateway_module
from hoh.gateway import (
    PROVIDER_READ_ONLY,
    Approval,
    Gateway,
    Outcome,
    PolicyMapping,
    ToolAuthError,
    ToolContentError,
    ToolDescriptor,
    ToolEffect,
    ToolPolicy,
    ToolTransportError,
)

NOW = "2026-09-24T00:00:00Z"


def _tool(tool_id="t.read", effect=ToolEffect.READ_ONLY, digest="schema-1",
          server="srv-a") -> ToolDescriptor:
    return ToolDescriptor(tool_id=tool_id, server_id=server, name=tool_id,
                          effect=effect, schema_digest=digest,
                          description=f"fake {tool_id}")


class Fake:
    """An execute function that counts calls and raises or returns on cue."""

    def __init__(self, *results):
        self.results = list(results) or [{"rows": [1, 2, 3]}]
        self.calls = 0
        self.seen = []

    def __call__(self, arguments):
        self.calls += 1
        self.seen.append(arguments)
        result = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(result, BaseException):
            raise result
        return result


def _policy(**kw) -> ToolPolicy:
    fields = dict(allowed_effects={ToolEffect.READ_ONLY},
                  approval_required_effects={ToolEffect.IRREVERSIBLE},
                  denied_effects=set(), max_transport_retries=2,
                  policy_version="p1")
    fields.update(kw)
    return ToolPolicy(**fields)


def _call(gw, tool_id="t.read", fake=None, **kw):
    fake = fake if fake is not None else Fake()
    rec = gw.call(tool_id, arguments={"q": 1}, execute=fake, now=NOW, **kw)
    return rec, fake


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


def test_the_enums_have_exactly_the_specified_members_in_order():
    """A member added, renamed or reordered silently changes what a recorded
    outcome means; the spec fixes both lists, so both are pinned here."""
    assert [e.value for e in ToolEffect] == [
        "READ_ONLY", "REVERSIBLE_WRITE", "IRREVERSIBLE", "EXTERNAL_PUBLISH", "UNKNOWN"]
    assert [o.value for o in Outcome] == [
        "OK", "DENIED_BY_POLICY", "NEEDS_APPROVAL", "AUTH_FAILURE",
        "TRANSPORT_FAILURE", "CONTENT_FAILURE", "SCHEMA_STALE"]
    assert all(m.name == m.value for m in [*ToolEffect, *Outcome])


@pytest.mark.parametrize("digest", ["", "   "])
def test_a_descriptor_with_an_empty_schema_digest_is_refused(digest):
    """MCP3 is only checkable if the schema a decision was made against can
    be named. A descriptor accepted with a blank digest would make every
    schema change look like no change at all."""
    with pytest.raises(ValidationError):
        _tool(digest=digest)
    # Control: the same descriptor with a digest is accepted.
    assert _tool(digest="schema-1").schema_digest == "schema-1"


def test_an_unknown_effect_is_what_an_undeclared_descriptor_gets():
    """A descriptor that does not declare its effect must not default to
    READ_ONLY -- that would make "nobody said" mean "harmless"."""
    d = ToolDescriptor(tool_id="t.x", server_id="s", name="x", schema_digest="d")
    assert d.effect is ToolEffect.UNKNOWN
    assert d.effect is not ToolEffect.READ_ONLY


def test_the_unknown_docstring_says_it_is_at_least_as_dangerous_as_irreversible():
    """The spec requires the classification's meaning to be written down where
    the enum is; a later reader treating UNKNOWN as a placeholder would weaken
    it to READ_ONLY."""
    doc = ToolEffect.__doc__
    assert "IRREVERSIBLE" in doc and "never as `READ_ONLY`" in doc


# --------------------------------------------------------------------------- #
# MCP1 -- read-only allowed
# --------------------------------------------------------------------------- #


def test_mcp1_read_only_tool_is_allowed_by_default_policy_and_execute_runs():
    """MCP1: a READ_ONLY tool under the default policy returns OK and the
    tool really runs. A gateway that reported OK without calling execute
    would pass an outcome-only check."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    rec, fake = _call(gw)
    assert rec.outcome is Outcome.OK
    assert fake.calls == 1 and rec.attempts == 1 and rec.executed
    assert fake.seen == [{"q": 1}]


def test_mcp1_control_the_same_tool_as_reversible_write_never_reaches_execute():
    """Negative control for MCP1: change only the effect and the default
    policy stops the call before the tool -- so it is the effect, not the
    tool's name or shape, that allowed it."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool(effect=ToolEffect.REVERSIBLE_WRITE)])
    rec, fake = _call(gw)
    assert rec.outcome is Outcome.NEEDS_APPROVAL
    assert fake.calls == 0 and rec.attempts == 0 and not rec.executed


def test_mcp1_classification_rests_on_effect_not_on_name():
    """A tool named like a reader but declared IRREVERSIBLE is not allowed;
    a gateway keying on names ("get_", "read") would let it through."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool(tool_id="read_only_get",
                                            effect=ToolEffect.IRREVERSIBLE)])
    rec, fake = _call(gw, "read_only_get")
    assert rec.outcome is not Outcome.OK and fake.calls == 0


# --------------------------------------------------------------------------- #
# MCP2 -- irreversible blocked pending approval
# --------------------------------------------------------------------------- #


def test_mcp2_irreversible_call_never_reaches_the_tool_without_approval():
    """MCP2: an IRREVERSIBLE tool returns NEEDS_APPROVAL and execute is not
    called at all -- asserted on the counter, because a gateway that ran the
    tool and then reported NEEDS_APPROVAL has already done the damage."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool("t.drop", ToolEffect.IRREVERSIBLE)])
    rec, fake = _call(gw, "t.drop")
    assert rec.outcome is Outcome.NEEDS_APPROVAL
    assert fake.calls == 0
    assert rec.attempts == 0 and rec.executed is False
    assert "not executed" in rec.reason


def test_mcp2_control_with_the_matching_approval_the_irreversible_call_proceeds():
    """Negative control for MCP2: supply only the matching approval and the
    same call runs. Without this, a gateway that blocked IRREVERSIBLE forever
    would pass MCP2."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool("t.drop", ToolEffect.IRREVERSIBLE)])
    ok = Approval(tool_id="t.drop", schema_digest="schema-1", approved_by="captain")
    rec, fake = _call(gw, "t.drop", approvals=[ok])
    assert rec.outcome is Outcome.OK and fake.calls == 1


def test_mcp2_an_approval_for_another_tool_does_not_unblock_this_one():
    """An approval is for one tool. A gateway that treated "any approval
    present" as consent would let one approval open every irreversible tool."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool("t.drop", ToolEffect.IRREVERSIBLE)])
    other = Approval(tool_id="t.other", schema_digest="schema-1")
    rec, fake = _call(gw, "t.drop", approvals=[other, "t.other"])
    assert rec.outcome is Outcome.NEEDS_APPROVAL and fake.calls == 0


def test_mcp2_an_approval_granted_against_an_older_schema_is_schema_stale():
    """An approval names the schema it was granted against. After the schema
    changed, the approved tool is not the registered tool, and the call is
    SCHEMA_STALE rather than silently approved."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool("t.drop", ToolEffect.IRREVERSIBLE, "schema-2")])
    old = Approval(tool_id="t.drop", schema_digest="schema-1")
    rec, fake = _call(gw, "t.drop", approvals=[old])
    assert rec.outcome is Outcome.SCHEMA_STALE and fake.calls == 0


def test_mcp2_a_denied_effect_never_reaches_execute_even_with_approval():
    """An effect in denied_effects is denied outright; an approval does not
    lift it. Otherwise "denied" would only mean "approval required"."""
    policy = _policy(denied_effects={ToolEffect.EXTERNAL_PUBLISH})
    gw = Gateway(policy, [_tool("t.pub", ToolEffect.EXTERNAL_PUBLISH)])
    rec, fake = _call(gw, "t.pub", approvals=["t.pub"])
    assert rec.outcome is Outcome.DENIED_BY_POLICY and fake.calls == 0


# --------------------------------------------------------------------------- #
# MCP3 -- schema change invalidates the cached mapping
# --------------------------------------------------------------------------- #


def _mapping(digest="schema-1", version="p1"):
    return PolicyMapping(tool_id="t.read", schema_digest=digest,
                         policy_version=version, decision=Outcome.OK,
                         mapped_at=NOW)


def test_mcp3_mapping_is_invalid_after_the_schema_digest_changes():
    """MCP3: a mapping made against schema-1 does not hold for schema-2. A
    cache keyed on tool_id alone would serve the old decision."""
    assert not _mapping().is_valid_for(_tool(digest="schema-2"), _policy())


def test_mcp3_mapping_is_invalid_after_the_policy_version_changes():
    """MCP3: a mapping made under policy p1 does not hold under p2, or a
    tightened policy would keep serving yesterday's allow."""
    assert not _mapping().is_valid_for(_tool(), _policy(policy_version="p2"))


def test_mcp3_control_mapping_is_valid_when_neither_changed():
    """Negative control for MCP3: same digest, same version -- valid. An
    `is_valid_for` that always said False would pass both cases above."""
    assert _mapping().is_valid_for(_tool(), _policy())


def test_mcp3_gateway_redecides_after_a_schema_change_instead_of_serving_cache():
    """MCP3 end to end: a tool first registered READ_ONLY is decided OK and
    cached. Re-registered with a new schema that is IRREVERSIBLE, the cached
    OK must not be served; the call never reaches the tool. The control is
    the second decide *before* the change, which is served from cache."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    first = gw.decide("t.read", now=NOW)
    again = gw.decide("t.read", now=NOW)
    assert first.outcome is Outcome.OK and not first.cached
    assert again.outcome is Outcome.OK and again.cached  # control: cache used

    gw.register(_tool(effect=ToolEffect.IRREVERSIBLE, digest="schema-2"))
    after = gw.decide("t.read", now=NOW)
    assert after.outcome is Outcome.NEEDS_APPROVAL
    assert not after.cached and after.schema_digest == "schema-2"
    assert gw.mappings["t.read"].schema_digest == "schema-2"
    rec, fake = _call(gw)
    assert fake.calls == 0 and rec.schema_digest == "schema-2"


def test_mcp3_gateway_redecides_after_a_policy_change():
    """MCP3 for the policy side: the decision records the version it was
    made against, and a new version is not served from the old mapping."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    assert gw.decide("t.read", now=NOW).policy_version == "provider-read-only-v1"
    gw.set_policy(_policy(allowed_effects=set(),
                          approval_required_effects={ToolEffect.READ_ONLY}))
    d = gw.decide("t.read", now=NOW)
    assert d.outcome is Outcome.NEEDS_APPROVAL and d.policy_version == "p1"
    assert not d.cached


# --------------------------------------------------------------------------- #
# MCP4 -- auth failure != content failure
# --------------------------------------------------------------------------- #


def test_mcp4_auth_failure_is_auth_failure_after_exactly_one_attempt():
    """MCP4: "you may not" is AUTH_FAILURE and is never retried, even with
    retries available. A gateway collapsing it into transport would retry a
    permission error until the limit."""
    gw = Gateway(_policy(max_transport_retries=5), [_tool()])
    rec, fake = _call(gw, fake=Fake(ToolAuthError("401")))
    assert rec.outcome is Outcome.AUTH_FAILURE
    assert rec.outcome is not Outcome.CONTENT_FAILURE
    assert rec.attempts == 1 and fake.calls == 1


def test_mcp4_control_content_failure_is_content_failure_not_auth():
    """Negative control for MCP4: change only the failure kind and the
    outcome flips to CONTENT_FAILURE. One code for both would pass the auth
    case on its own."""
    gw = Gateway(_policy(max_transport_retries=5), [_tool()])
    rec, fake = _call(gw, fake=Fake(ToolContentError("malformed")))
    assert rec.outcome is Outcome.CONTENT_FAILURE
    assert rec.outcome is not Outcome.AUTH_FAILURE
    assert rec.attempts == 1 and fake.calls == 1


def test_mcp4_classification_is_by_type_not_by_message():
    """The message of an auth error mentioning "content" or "timeout" must
    not change its class; string matching would misfile it."""
    gw = Gateway(_policy(), [_tool()])
    rec, _ = _call(gw, fake=Fake(ToolAuthError("bad content, timeout, retry")))
    assert rec.outcome is Outcome.AUTH_FAILURE
    rec2, _ = _call(gw, fake=Fake(PermissionError("x")))
    assert rec2.outcome is Outcome.AUTH_FAILURE


# --------------------------------------------------------------------------- #
# MCP5 -- transport failures retry per policy
# --------------------------------------------------------------------------- #


def test_mcp5_transport_failure_retries_to_the_limit_then_reports_attempts():
    """MCP5: with two retries, a persistent transport failure is tried three
    times and then reported as TRANSPORT_FAILURE with that count. A gateway
    that gave up on the first blip, or retried forever, fails this."""
    gw = Gateway(_policy(max_transport_retries=2), [_tool()])
    rec, fake = _call(gw, fake=Fake(ToolTransportError("reset")))
    assert rec.outcome is Outcome.TRANSPORT_FAILURE
    assert rec.attempts == 3 and fake.calls == 3


def test_mcp5_control_zero_retries_does_not_retry():
    """Negative control for MCP5: change only the policy's retry limit to 0
    and the same failure is tried once."""
    gw = Gateway(_policy(max_transport_retries=0), [_tool()])
    rec, fake = _call(gw, fake=Fake(ToolTransportError("reset")))
    assert rec.outcome is Outcome.TRANSPORT_FAILURE
    assert rec.attempts == 1 and fake.calls == 1


def test_mcp5_a_transport_blip_followed_by_success_is_ok():
    """A retry exists to recover: one failure then content is OK after two
    attempts. A retry loop that recorded the first failure would fail this."""
    gw = Gateway(_policy(max_transport_retries=2), [_tool()])
    rec, fake = _call(gw, fake=Fake(TimeoutError(), {"v": 1}))
    assert rec.outcome is Outcome.OK and rec.attempts == 2 and fake.calls == 2
    assert rec.content_digest


def test_mcp5_a_negative_retry_limit_is_refused():
    """max_transport_retries < 0 has no meaning; accepting it would let a
    loop bound go below one attempt."""
    with pytest.raises(ValidationError):
        _policy(max_transport_retries=-1)


# --------------------------------------------------------------------------- #
# MCP6 -- provenance and digest
# --------------------------------------------------------------------------- #


def test_mcp6_content_digest_changes_when_content_changes():
    """MCP6: different content, different digest. A constant or empty digest
    would make every result look the same."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    a, _ = _call(gw, fake=Fake({"v": 1}))
    b, _ = _call(gw, fake=Fake({"v": 2}))
    assert a.content_digest and b.content_digest
    assert a.content_digest != b.content_digest


def test_mcp6_control_content_digest_is_stable_for_equal_content():
    """Negative control for MCP6: equal content -- even with keys in another
    order -- gives the same digest; a digest over object identity or
    insertion order would not be reproducible."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    a, _ = _call(gw, fake=Fake({"a": 1, "b": [1, 2]}))
    b, _ = _call(gw, fake=Fake({"b": [1, 2], "a": 1}))
    assert a.content_digest == b.content_digest
    assert len(a.content_digest) == 64


def test_mcp6_provenance_names_the_server_and_the_tool():
    """MCP6: the record says where the content came from. Without the server,
    two servers exposing the same tool name would be indistinguishable."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool(server="srv-b")])
    rec, _ = _call(gw)
    assert rec.provenance.server_id == "srv-b"
    assert rec.provenance.tool_id == "t.read"


def test_mcp6_undigestable_content_is_content_failure_not_ok_with_empty_digest():
    """MCP6: content with no canonical serialisation is CONTENT_FAILURE.
    A gateway that swallowed the error would record OK with no digest."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    rec, fake = _call(gw, fake=Fake({"x": object()}))
    assert rec.outcome is Outcome.CONTENT_FAILURE
    assert rec.content_digest is None and fake.calls == 1
    nan, _ = _call(gw, fake=Fake({"x": float("nan")}))
    assert nan.outcome is Outcome.CONTENT_FAILURE


# --------------------------------------------------------------------------- #
# MCP7 -- no second path
# --------------------------------------------------------------------------- #


def _execute_paths(source: str) -> list[str]:
    """Qualified names of every function that invokes `execute` or accepts it."""
    found: list[str] = []

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + [child.name]
                if not isinstance(child, ast.ClassDef):
                    params = [a.arg for a in (*child.args.posonlyargs, *child.args.args,
                                              *child.args.kwonlyargs)]
                    if "execute" in params:
                        found.append("accepts:" + ".".join(inner))
                walk(child, inner)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                if (isinstance(f, ast.Name) and f.id == "execute") or (
                        isinstance(f, ast.Attribute) and f.attr == "execute"):
                    found.append("invokes:" + ".".join(scope))
            walk(child, scope)

    walk(ast.parse(source), [])
    return found


def test_mcp7_the_only_execute_path_in_the_module_is_gateway_call():
    """MCP7: the UI cannot bypass the gateway because the module offers no
    second path: `execute` is invoked in exactly one place, inside
    Gateway.call, and no other function accepts one. A helper like
    `run_tool(execute, ...)` added later would fail here."""
    source = inspect.getsource(gateway_module)
    assert _execute_paths(source) == ["accepts:Gateway.call", "invokes:Gateway.call"]
    doc = " ".join(gateway_module.__doc__.split())
    assert "`tests/test_gateway.py` defends" in doc


def test_mcp7_control_the_check_catches_a_second_path():
    """Negative control for MCP7: the same check over the module with one
    bypass appended reports it. A checker that found nothing in any source
    would pass the case above vacuously."""
    source = inspect.getsource(gateway_module) + textwrap.dedent("""
        def run_direct(execute, arguments):
            return execute(arguments)
    """)
    paths = _execute_paths(source)
    assert "invokes:run_direct" in paths and "accepts:run_direct" in paths


def test_mcp7_the_module_imports_no_transport():
    """The safety half has no transport; a network import here would mean a
    tool could be reached from inside the gateway without `execute`."""
    tree = ast.parse(inspect.getsource(gateway_module))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for a in n.names}
    mods |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom)}
    assert not mods & {"socket", "subprocess", "urllib", "http", "asyncio", "ssl",
                       "requests", "httpx"}


# --------------------------------------------------------------------------- #
# UNKNOWN
# --------------------------------------------------------------------------- #


def test_unknown_effect_is_not_allowed_by_a_policy_that_allows_read_only():
    """A tool whose effect was never declared is not treated as READ_ONLY:
    under a policy that allows READ_ONLY and lists UNKNOWN nowhere, it is
    denied and execute never runs."""
    policy = _policy(approval_required_effects=set())
    gw = Gateway(policy, [ToolDescriptor(tool_id="t.u", server_id="s", name="u",
                                         schema_digest="d")])
    rec, fake = _call(gw, "t.u")
    assert rec.outcome is Outcome.DENIED_BY_POLICY
    assert fake.calls == 0


def test_unknown_control_the_same_tool_declared_read_only_is_allowed():
    """Negative control for UNKNOWN: change only the declared effect to
    READ_ONLY and the same policy allows it -- so the denial above is the
    classification's doing, not a broken policy."""
    policy = _policy(approval_required_effects=set())
    gw = Gateway(policy, [ToolDescriptor(tool_id="t.u", server_id="s", name="u",
                                         schema_digest="d",
                                         effect=ToolEffect.READ_ONLY)])
    rec, fake = _call(gw, "t.u")
    assert rec.outcome is Outcome.OK and fake.calls == 1


def test_unknown_under_the_default_policy_never_reaches_the_tool():
    """Under PROVIDER_READ_ONLY an UNKNOWN tool waits for an approval, like
    IRREVERSIBLE; it never runs on its own."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool("t.u", ToolEffect.UNKNOWN)])
    rec, fake = _call(gw, "t.u")
    assert rec.outcome is Outcome.NEEDS_APPROVAL and fake.calls == 0


def test_unknown_is_never_ok_even_if_an_incoherent_policy_allows_it():
    """A policy that allows UNKNOWN outright is refused by the gateway; if it
    were accepted, "nobody declared the effect" would become "allowed"."""
    bad = _policy(allowed_effects={ToolEffect.READ_ONLY, ToolEffect.UNKNOWN})
    with pytest.raises(ValueError, match="UNKNOWN"):
        Gateway(bad, [_tool("t.u", ToolEffect.UNKNOWN)])


def test_an_unlisted_effect_is_never_allowed():
    """An effect named in no policy set (here REVERSIBLE_WRITE) needs an
    approval; a gateway that treated "not denied" as allowed fails this."""
    gw = Gateway(_policy(), [_tool("t.w", ToolEffect.REVERSIBLE_WRITE)])
    rec, fake = _call(gw, "t.w")
    assert rec.outcome is Outcome.NEEDS_APPROVAL and fake.calls == 0
    ok, fake2 = _call(gw, "t.w", approvals=["t.w"])
    assert ok.outcome is Outcome.OK and fake2.calls == 1  # control


# --------------------------------------------------------------------------- #
# coherence()
# --------------------------------------------------------------------------- #


def test_coherence_reports_an_effect_listed_in_two_sets():
    """An effect both allowed and denied has no meaning; coherence must name
    it rather than leave precedence to whichever branch runs first."""
    p = _policy(denied_effects={ToolEffect.READ_ONLY})
    reasons = p.coherence()
    assert any("READ_ONLY" in r and "denied_effects" in r for r in reasons)


def test_coherence_reports_a_policy_allowing_unknown_or_irreversible():
    """Allowing UNKNOWN or IRREVERSIBLE outright must be an explicit approval,
    never a default."""
    assert any("UNKNOWN" in r for r in _policy(
        allowed_effects={ToolEffect.UNKNOWN}).coherence())
    assert any("IRREVERSIBLE" in r for r in _policy(
        allowed_effects={ToolEffect.IRREVERSIBLE},
        approval_required_effects=set()).coherence())


def test_coherence_control_the_default_policy_is_coherent():
    """Negative control: the default and a plain policy report nothing. A
    coherence() that always complained would pass both cases above."""
    assert PROVIDER_READ_ONLY.coherence() == []
    assert _policy().coherence() == []
    assert PROVIDER_READ_ONLY.allowed_effects == {ToolEffect.READ_ONLY}


# --------------------------------------------------------------------------- #
# unregistered ids, records
# --------------------------------------------------------------------------- #


def test_an_unregistered_tool_id_is_refused_and_never_reaches_execute():
    """An unknown tool is not an implicit allow, not even under a policy that
    allows READ_ONLY. The call is refused, execute never runs, and the record
    says why."""
    gw = Gateway(PROVIDER_READ_ONLY, [_tool()])
    assert gw.decide("t.ghost", now=NOW).outcome is not Outcome.OK
    rec, fake = _call(gw, "t.ghost")
    assert rec.outcome is Outcome.DENIED_BY_POLICY and fake.calls == 0
    assert "not registered" in rec.reason and rec.schema_digest is None
    # Control: the registered id goes through.
    ok, fake2 = _call(gw, "t.read")
    assert ok.outcome is Outcome.OK and fake2.calls == 1


def test_every_decision_and_every_call_leaves_a_record():
    """Each decide() appends to the decision log and each call() to records,
    blocked or not. A gateway that only logged successes would leave no trace
    of the calls that mattered most."""
    gw = Gateway(PROVIDER_READ_ONLY,
                 [_tool(), _tool("t.drop", ToolEffect.IRREVERSIBLE)])
    gw.decide("t.read", now=NOW)
    gw.decide("t.ghost", now=NOW)
    assert len(gw.decisions) == 2 and gw.records == []
    ok, _ = _call(gw)
    blocked, _ = _call(gw, "t.drop")
    failed, _ = _call(gw, fake=Fake(ToolAuthError()))
    assert gw.records == [ok, blocked, failed]
    assert len(gw.decisions) == 5
    for rec in gw.records:
        assert rec.recorded_at == NOW
        assert rec.policy_version == PROVIDER_READ_ONLY.policy_version
    assert blocked.schema_digest == "schema-1"
