"""Declared role routing (P3-20, slice 1): which model, effort and provider each role runs on,
written once in a policy file instead of typed again as `--role-model` / `--role-effort` pairs.

What this module is, and is not. `docs/ROUTING.md` records that no routing *recommendation* is
adopted (`DEFERRED_ON_EVIDENCE`): no per-role cost or failure-class history exists to choose
from, and the document refuses "QA is cheap" and "the planner must be frontier" by name. Nothing
here changes that. A policy is the operator's own declaration, read from a file the operator
wrote; this module has **no default policy and no table of what a role should run on**. It does
two things: it refuses a declaration that is ambiguous or unsafe to read, and it turns a valid one
into the per-role tables the dispatcher already consumes (`models`, `efforts`), recording where
each value came from so that two verdicts stay comparable.

Precedence, strongest first: an explicit per-role value of the caller (`--role-model`,
`--role-effort`) > the policy's role entry > the policy's `default` > the harness's own default
(stated as `NOT_AVAILABLE`, never guessed). A provider is not chosen by `resolve` but asserted: a
policy that names a provider for a role must equal the harness profile the run configures for that
role, or the run is refused -- the record would otherwise name a provider the role did not run on.
(`resolve_failover`, below, is the one place a provider is chosen, and only among the routes the
policy declares; what it returns as `profiles` is for building a dispatcher, never for resolving
again: every resolution takes the **declared** profiles.)

Every refusal is fixed text naming a field, never a value that was read: a policy is an
operator's file and a model slot is exactly where a pasted key lands.

`dispatchrecovery` has a different, unrelated notion of routing (where a *failed* dispatch
goes next); none of its names is reused here.

Failover (P3-41, slice 1). A role entry may also *declare* an ordered list of `fallbacks`: whole
routes (provider and model both named) that the operator has approved for that role, and nothing
else. `resolve_failover` reads the cross-run provider health (`health.read_health`) once, when a
run first resolves its routing, and chooses the first declared route whose provider has no
pending limit or availability wait. The choice -- including "stay on the declared route" -- is
written once to the run directory (`routing-failover.json`, create-only) and every later
resolution of that run reads it back instead of reading health again, so a resume neither flips
to a fallback nor back. It fails closed: a record that is unreadable or bound to another policy
is `RoutingRefused` (so are health that cannot be read completely and no healthy declared route, for
the strict one-shot function; the product's plan stays on the declared route instead). It is
a decision, not a dispatch: nothing here parks, waits, retries, ramps or starts anything, and
nothing calls it yet. `resolve` itself does not read `fallbacks`: only `resolve_failover` acts on
them. Health is evidence, not authority: a caller that acts on this decision binds each park
(`dispatchrecovery.verify_binding`) first -- this module has no programme or master input.

Slice 2 (P3-41): the caller. `hoh run` plans the decision before it builds its dispatcher
(`plan_failover`: the dispatcher is built with the tables and the provider the plan chose) and
commits it -- one create-only record -- after the master is declared and before the first
dispatch (`FailoverPlan.commit`), so a stale master and a refused invocation leave no record. The
caller passes the *verified* reading (`health.read_verified_health`: only parks that
`verify_binding` still calls bound are evidence) without the run's own park; a governed master
that is not the project's current one cannot decide. The declared route is returned to by the
**next** run, never inside one (the decision is sticky), and not before `RETURN_DWELL_SECONDS`
after a limit's stated reset (state `SETTLING`): the hysteresis of this module.

Slice 3 (P3-41): the park. A run parked by a provider wait (`hoh resume-quota`) whose pinned policy
declares a fallback for the parked role does not have to wait for the reset. `record_park_failover`
decides, from the same verified health plus the run's own verified park (which is the evidence that
its provider is out), whether a declared route that is not limited exists, and records the answer
-- create-only, one numbered file per park (`routing-failover-park-<n>.json`, at most
`MAX_PARK_FAILOVERS` per run) -- *before* the resume loop lifts the park. The next `hoh run` of the
run reads **every unspent** such record in `plan_failover` (the newest per role wins) over its
start-time record, and builds its dispatcher on the fallbacks until each record's `return_at` (the
stated reset of the route the run returns to, plus `RETURN_DWELL_SECONDS`); after that the record
is spent and its role is on the route it began on: the declared one, or the route of its start-time
record. A failure that is a short wait (anything but a usage limit with less than
`MIN_FAILOVER_WAIT_SECONDS` left) waits instead: it must not spend a record of the run's budget. Without a healthy declared fallback, or for anything this function cannot
verify, it refuses (`RoutingRefused`) and the park waits for its reset as it always did: a provider
stop is never a halt.

Slice 4 (P3-41): receipts, route kinds, trust. Every route change this module decides -- a run's
start and a park -- carries a **route receipt** inside its decision (`route_receipt`): the old and the
new route, the reason, the role, the capabilities the role requires and the route satisfies, the
trust class of the route, the policy (schema and digest) and whether additional verification is
required. The receipt is reproduced from the policy when a record is read: a record that claims a
trust class, a capability set or a verification the policy does not give is refused, and a record
written before this slice (no receipt) gets the receipt derived from the policy, so a run that
began on a weaker route is held to the same rule. A fallback may be an **account** route (the same
provider under another operator-registered account handle) or a **local** route (a harness profile
running a local model whose capabilities the policy declares); `_decide` reads the health of the
(provider, account) pair, and a route that lacks a capability its role requires is refused when
the policy is read. A fallback is **weaker** unless the policy declares it `equivalent`: a run with
a role on a weaker route must meet the policy's `verification` (`min_discriminating`,
`no_open_gaps`; a documented default when it declares none) before the controller accepts a
candidate -- the receipt says it, `Resolved.evidence["verification"]` carries it, the controller
acts on it. A park failover is also evidence for the routes it passed over until their stated
reset and the dwell after it (`_park_evidence`: another run's record counts for the provider and account it passed
over); `failover_summary` shows the park records. What this slice does not do: it never touches a
credential (an account handle is a name the operator registered; binding a harness to it is the
operator's), and it makes no durable park when no route is healthy (slice 5).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from . import health as health_mod
from . import quota
from .contracts import Role
from .dispatchrecovery import (
    CAPABILITY_REQUIREMENTS, MasterBinding, RecoveryError, check_account_handle,
    stale_master_reason,
)
from .durable_io import DurableWriteNotSynced, durable_create, settled_stat
from .telemetry import NOT_AVAILABLE

SCHEMA = "hoh-routing-policy/1"

FAILOVER_SCHEMA = "hoh-routing-failover/2"
FAILOVER_FILE = "routing-failover.json"
"""The one record a run keeps of its failover decision, in the run directory."""

MAX_FALLBACKS = 3
"""At most this many declared fallbacks per role."""

MAX_FAILOVER_BYTES = 64 * 1024
"""A failover record names at most three roles with at most four routes each."""

PARK_FAILOVER_SCHEMA = "hoh-routing-park-failover/1"
PARK_FAILOVER_FILE = "routing-failover-park-{n}.json"
"""The record of one park failover, in the run directory; `n` is 1 for the run's first."""

MIN_FAILOVER_WAIT_SECONDS = 900
"""A park that is not a usage limit (a rate limit, a provider or network failure) fails over only
when at least this many seconds of its wait are left: longer than the 5 and 2 minutes of
`failures._WAIT`, so a blip waits and spends no record. A usage limit states its own reset and
fails over whenever one is left. Inclusive."""

MAX_PARK_FAILOVERS = 8
"""At most this many park failovers per run, counted by the records on disk. The record is written
before the park is lifted, so an attempt that crashes after it still counts; with the resume
loop's own bound (`quota.MAX_RESUME_ATTEMPTS` consecutive unproductive resumes) this is the whole
bound of the path. Past it a parked run waits for its reset."""

RETURN_DWELL_SECONDS = 600
"""The hysteresis of the return: a limit whose stated reset passed less than this many seconds ago
is `SETTLING`, not `RESET_DUE` -- the provider has not yet been asked again, and the route that
was failed over to is kept for the runs that start inside the window. At the bound itself
(exactly this many seconds) the window is over. One bound, in seconds, for every provider."""

NO_EVIDENCE = "NO_EVIDENCE"
SETTLING = "SETTLING"
UNHEALTHY = frozenset({health_mod.HealthState.LIMITED.value, health_mod.HealthState.DEGRADED.value,
                       SETTLING})
"""The states that move a role off a route: a limit or an availability failure whose stated retry
time has not come, and a limit whose reset passed less than `RETURN_DWELL_SECONDS` ago.
`RESET_DUE` beyond the dwell is the original route after its retry time (stays), `GIVEN_UP` is a
person's decision (stays); both are recorded as read."""
_STATES = frozenset({NO_EVIDENCE, SETTLING} | {s.value for s in health_mod.HealthState})
_WORST_FIRST = ("LIMITED", "DEGRADED", SETTLING, "RESET_DUE", "GIVEN_UP")

MAX_BYTES = 64 * 1024
"""A routing policy names at most three roles and three words each; a larger file is not one."""

PROVIDERS = ("claude", "codex", "kimi", "pi")
"""The harness profiles a role can be started under: the keys of the subprocess adapter's
`HarnessDispatcher.COMMANDS` (a test pins the two together)."""

EFFORTS = ("low", "medium", "high", "xhigh", "max")
"""The effort words `--role-effort` documents; a policy takes no other."""

ROUTE_KINDS = ("provider", "account", "local")
"""What a declared fallback is (P3-41 slice 4): another approved model or provider (`provider`,
the default), the same provider under another operator-registered account handle (`account`), or
a harness profile running an approved local model (`local`)."""

QUALITIES = ("equivalent", "weaker")
"""The trust/quality class of a fallback. The declared route is the reference (`primary`); a
fallback is **weaker** unless the policy says `equivalent`: trust is never inherited by silence."""

CAPABILITIES = tuple(sorted({c for needs in CAPABILITY_REQUIREMENTS.values() for c in needs}))
"""The capability words of the role contracts (`dispatchrecovery.CAPABILITY_REQUIREMENTS`)."""

MAX_DISCRIMINATING = 20
"""The most `min_discriminating` a verification policy can ask for."""

DEFAULT_WEAKER_VERIFICATION = MappingProxyType({"no_open_gaps": True})
"""What a weaker route must meet when the policy declares no `verification`: QA must report no
open gap. A policy that wants more (or declares a route `equivalent`) says so."""

RECEIPT_SCHEMA = "hoh-route-receipt/1"

_TOP_KEYS = frozenset({"schema", "default", "roles", "verification"})
_DEFAULT_KEYS = frozenset({"model", "effort"})
_ROLE_KEYS = frozenset({"provider", "model", "effort"})
_FALLBACK_KEYS = _ROLE_KEYS | {"kind", "account", "capabilities", "quality"}
_VERIFICATION_KEYS = frozenset({"min_discriminating", "no_open_gaps"})
_ROLE_BY_NAME = {role.value: role for role in Role}
_MODEL_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]\-]{0,63}")


class RoutingRefused(ValueError):
    """The routing declaration cannot be applied. The message names a field, never a value."""


@dataclass(frozen=True)
class Route:
    """What one entry of a policy declares; a field it leaves out is `None`."""

    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    #: P3-41 slice 4, fallbacks only: what kind of route it is, the account handle of an
    #: `account` route, the capabilities a route declares (sorted; `None` = the harness
    #: profile's role contracts) and its trust class (`None` on a fallback = `weaker`).
    kind: str | None = None
    account: str | None = None
    capabilities: tuple | None = None
    quality: str | None = None


@dataclass(frozen=True)
class RoutingPolicy:
    default: Route
    roles: Mapping  # Role -> Route; a read-only mapping once `parse` has built it
    source: str
    digest: str
    file_sha256: str
    #: Role -> tuple of whole `Route`s the operator approved as fallbacks, in order; a read-only
    #: mapping with no entry for a role that declares none.
    fallbacks: Mapping = dataclass_field(default_factory=lambda: MappingProxyType({}))
    #: P3-41 slice 4: what a run with a role on a weaker route must meet before a candidate is
    #: accepted (`min_discriminating`, `no_open_gaps`); empty when the policy declares none.
    verification: Mapping = dataclass_field(default_factory=lambda: MappingProxyType({}))

    def route(self, role: Role) -> Route:
        """The role's entry over the default, field by field. The provider comes only from the
        entry: a default cannot assert one."""
        own = self.roles.get(role, Route())
        return Route(provider=own.provider,
                     model=own.model if own.model is not None else self.default.model,
                     effort=own.effort if own.effort is not None else self.default.effort)

    def evidence(self) -> dict:
        """What was declared, as a new dict each time (a caller cannot edit the policy by it)."""
        roles = {}
        for role in Role:
            if role not in self.roles:
                continue
            roles[role.value] = _declared(self.roles[role])
            if self.fallbacks.get(role):
                roles[role.value]["fallbacks"] = [_declared(r) for r in self.fallbacks[role]]
        return {"schema": SCHEMA, "source": self.source, "digest": self.digest,
                "file_sha256": self.file_sha256, "default": _declared(self.default),
                "roles": roles,
                **({"verification": {"weaker_route": dict(self.verification)}}
                   if self.verification else {})}


@dataclass(frozen=True)
class Resolved:
    """The per-role tables the dispatcher consumes, and where each value came from."""

    models: dict
    efforts: dict
    evidence: dict
    #: Role -> the provider each role runs on after routing: the run's profile, or the provider of
    #: the fallback a failover chose. A caller that built its profiles before routing uses this.
    profiles: dict = dataclass_field(default_factory=dict)
    #: Role -> the account handle of the `account` route a failover chose (P3-41 slice 4); a role
    #: that stays or fails over to another kind of route has no entry. A caller that records
    #: which account a role runs under (`--role-account`) records this one.
    accounts: dict = dataclass_field(default_factory=dict)


def _declared(route: Route) -> dict:
    out = {name: value
           for name, value in (("provider", route.provider), ("model", route.model),
                               ("effort", route.effort), ("kind", route.kind),
                               ("account", route.account), ("quality", route.quality))
           if value is not None}
    if route.capabilities is not None:
        out["capabilities"] = list(route.capabilities)
    return out


def _no_repeated_key(pairs: list) -> dict:
    """`json.loads` keeps the last of two equal keys, so a file that says two things about one
    role would be read as saying one."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise RoutingRefused("a key appears twice in an object of the policy")
        out[key] = value
    return out


def _model(value: object, field: str) -> str:
    if (not isinstance(value, str) or value == NOT_AVAILABLE
            or _MODEL_SHAPE.fullmatch(value) is None):
        raise RoutingRefused(f"{field} is not a plain model name (letters, digits and "
                             "._:@/[]- , at most 64 characters)")
    try:
        # The credential-shape tests of an account handle. A model name may carry brackets
        # (`opus[1m]`) and a handle may not, so they are mapped for this check only.
        check_account_handle(value.replace("[", ".").replace("]", "."))
    except RecoveryError:
        raise RoutingRefused(f"{field} looks like a credential or names one") from None
    return value


def _entry(body: object, field: str, allowed: frozenset) -> Route:
    if not isinstance(body, dict):
        raise RoutingRefused(f"{field} is not an object")
    if not set(body) <= allowed:
        raise RoutingRefused(f"{field} has a key this policy does not know "
                             f"(allowed: {', '.join(sorted(allowed))})")
    fields: dict = {}
    if "provider" in body:
        if body["provider"] not in PROVIDERS:
            raise RoutingRefused(f"{field}.provider is not one of {', '.join(PROVIDERS)}")
        fields["provider"] = body["provider"]
    if "model" in body:
        fields["model"] = _model(body["model"], f"{field}.model")
    if "effort" in body:
        if body["effort"] not in EFFORTS:
            raise RoutingRefused(f"{field}.effort is not one of {', '.join(EFFORTS)}")
        fields["effort"] = body["effort"]
    if "kind" in body:
        if body["kind"] not in ROUTE_KINDS:
            raise RoutingRefused(f"{field}.kind is not one of {', '.join(ROUTE_KINDS)}")
        fields["kind"] = body["kind"]
    if "account" in body:
        handle = body["account"]
        try:
            if (not isinstance(handle, str) or not handle.strip() or handle == NOT_AVAILABLE
                    or check_account_handle(handle) != handle):
                raise RecoveryError("no handle")        # `check_account_handle` strips: not here
            fields["account"] = handle
        except RecoveryError:
            raise RoutingRefused(f"{field}.account is not a plain account handle (a short name "
                                 "the operator registered; never a credential)") from None
    if "capabilities" in body:
        caps = body["capabilities"]
        if (not isinstance(caps, list) or not caps or len(caps) > len(CAPABILITIES)
                or not all(isinstance(c, str) and c in CAPABILITIES for c in caps)
                or len(set(caps)) != len(caps)):
            raise RoutingRefused(f"{field}.capabilities is not a non-empty list of distinct "
                                 f"words of {', '.join(CAPABILITIES)}")
        fields["capabilities"] = tuple(sorted(caps))
    if "quality" in body:
        if body["quality"] not in QUALITIES:
            raise RoutingRefused(f"{field}.quality is not one of {', '.join(QUALITIES)}")
        fields["quality"] = body["quality"]
    return Route(**fields)


def _fallbacks(value: object, field_name: str, role: str) -> tuple:
    """The declared fallbacks of one role: a non-empty list of at most `MAX_FALLBACKS` routes,
    each naming its provider **and** its model. A fallback that inherited its model from the
    default would be a route nobody declared. P3-41 slice 4: an `account` route names its
    handle (and no other kind does), a `local` route declares its capabilities, and a route that
    declares capabilities must satisfy everything the role requires of a route."""
    if not isinstance(value, list) or not value:
        raise RoutingRefused(f"{field_name}.fallbacks is not a non-empty list")
    if len(value) > MAX_FALLBACKS:
        raise RoutingRefused(f"{field_name}.fallbacks has more than {MAX_FALLBACKS} routes")
    chain = []
    for i, item in enumerate(value):
        where = f"{field_name}.fallbacks[{i}]"
        route = _entry(item, where, _FALLBACK_KEYS)
        if route.provider is None or route.model is None:
            raise RoutingRefused(f"{where} must name both provider and model")
        if (route.kind == "account") != (route.account is not None):
            raise RoutingRefused(f"{where}: an account is named by exactly the routes of kind "
                                 "account")
        if route.kind == "local" and route.capabilities is None:
            raise RoutingRefused(f"{where}: a local route declares its capabilities")
        missing = [c for c in CAPABILITY_REQUIREMENTS[role]
                   if route.capabilities is not None and c not in route.capabilities]
        if missing:
            raise RoutingRefused(f"{where} lacks a capability the role requires "
                                 f"({', '.join(missing)})")
        chain.append(route)
    return tuple(chain)


def _verification(value: object) -> Mapping:
    """The `verification` clause: `{"weaker_route": {"min_discriminating": n, "no_open_gaps": b}}`
    -- the measures the controller can evaluate itself, and nothing it cannot (a requirement word
    this product does not act on is refused, never accepted and ignored)."""
    if not isinstance(value, dict) or set(value) != {"weaker_route"}:
        raise RoutingRefused("verification is not an object with exactly the key weaker_route")
    body = value["weaker_route"]
    if not isinstance(body, dict) or not body or not set(body) <= _VERIFICATION_KEYS:
        raise RoutingRefused("verification.weaker_route is not a non-empty object of "
                             "min_discriminating and no_open_gaps")
    measures: dict = {}
    if "min_discriminating" in body:
        n = body["min_discriminating"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_DISCRIMINATING:
            raise RoutingRefused("verification.weaker_route.min_discriminating is not a whole "
                                 f"number from 1 to {MAX_DISCRIMINATING}")
        measures["min_discriminating"] = n
    if "no_open_gaps" in body:
        if body["no_open_gaps"] is not True:
            raise RoutingRefused("verification.weaker_route.no_open_gaps is not true (leave it "
                                 "out to ask for nothing)")
        measures["no_open_gaps"] = True
    return MappingProxyType(measures)


def _distinct(primary: Route, chain: tuple, default: Route, field_name: str) -> None:
    """No fallback repeats the declared route or another fallback (by provider, model and
    account: the same pair at another effort is the same route, and failing over to it moves
    nothing; the same pair under another account is another route)."""
    seen = {(primary.provider, primary.model if primary.model is not None else default.model,
             primary.account)}
    for i, route in enumerate(chain):
        key = (route.provider, route.model, route.account)
        if key in seen:
            raise RoutingRefused(f"{field_name}.fallbacks[{i}] repeats a route the role already "
                                 "declares")
        seen.add(key)


def parse(data: object, source: str = "", file_sha256: str = "") -> RoutingPolicy:
    """Validates a policy document; refuses rather than applying less than it declares."""
    if not isinstance(data, dict):
        raise RoutingRefused("the policy is not a JSON object")
    if data.get("schema") != SCHEMA:
        raise RoutingRefused(f"schema is not {SCHEMA!r}")
    if not set(data) <= _TOP_KEYS:
        raise RoutingRefused("the policy has a top-level key it does not know "
                             "(allowed: default, roles, schema, verification)")
    default = Route()
    if "default" in data:
        default = _entry(data["default"], "default", _DEFAULT_KEYS)
        if default == Route():
            raise RoutingRefused("default declares nothing")
    declared = data.get("roles", {})
    if not isinstance(declared, dict):
        raise RoutingRefused("roles is not an object")
    roles: dict = {}
    fallbacks: dict = {}
    for name, body in declared.items():
        if name not in _ROLE_BY_NAME:
            raise RoutingRefused("roles names a role that is not planner, developer or qa "
                                 "(exact lower-case names; 'all' is the `default` entry)")
        chain: tuple = ()
        if isinstance(body, dict) and "fallbacks" in body:
            chain = _fallbacks(body["fallbacks"], f"roles.{name}", name)
            body = {k: v for k, v in body.items() if k != "fallbacks"}
        route = _entry(body, f"roles.{name}", _ROLE_KEYS)
        if route == Route():
            raise RoutingRefused(f"roles.{name} declares nothing")
        if chain:
            if route.provider is None:
                raise RoutingRefused(f"roles.{name}.fallbacks needs roles.{name}.provider: a "
                                     "fallback is a route away from a declared provider")
            _distinct(route, chain, default, f"roles.{name}")
            fallbacks[_ROLE_BY_NAME[name]] = chain
        roles[_ROLE_BY_NAME[name]] = route
    if default == Route() and not roles:
        raise RoutingRefused("the policy declares no route: no default and no role entry")
    verification = _verification(data["verification"]) if "verification" in data \
        else MappingProxyType({})
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return RoutingPolicy(default=default, roles=MappingProxyType(roles), source=source,
                         digest=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
                         file_sha256=file_sha256, fallbacks=MappingProxyType(fallbacks),
                         verification=verification)


def load(path: Path | str) -> RoutingPolicy:
    """Reads and validates a policy file.

    Fails closed on anything but one regular, non-link file of at most `MAX_BYTES` that holds one
    strict-UTF-8 JSON object with no repeated key and a nesting the parser can take: a missing
    file, a directory, a FIFO, a link (dangling or not), an unreadable or oversize file, another
    encoding, a BOM, NUL. Only `RoutingRefused` leaves this function. Ancestors of the file are
    not checked (stated limit)."""
    if not isinstance(path, (str, os.PathLike)) or not isinstance(os.fspath(path), str):
        raise RoutingRefused("the policy path is not a path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        raise RoutingRefused("the policy file cannot be opened (missing, a link, unreadable "
                             "or not a path)") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RoutingRefused("the policy file is not a regular file")
        raw = b""
        while len(raw) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        raise RoutingRefused("the policy file cannot be read") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise RoutingRefused(f"the policy file is larger than {MAX_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_repeated_key)
    except RoutingRefused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        # a balanced 40 000-byte `[`...`]` is under the size ceiling and still exhausts the
        # parser's recursion limit: the parser's own exception is caught here, not the size
        raise RoutingRefused("the policy file is not one UTF-8 JSON document") from None
    return parse(data, source=str(path), file_sha256=hashlib.sha256(raw).hexdigest())


def resolve(policy: RoutingPolicy, *, profiles: Mapping,
            models: Mapping | None = None, efforts: Mapping | None = None) -> Resolved:
    """The per-role `models` and `efforts` of a run under `policy`, as new dicts, and the
    evidence of where each value came from.

    `profiles`, `models` and `efforts` are what the caller configured explicitly; they are not
    changed. A role the policy does not reach gets no table entry and the evidence says
    `NOT_AVAILABLE` / `harness-default`."""
    explicit = {"model": dict(models or {}), "effort": dict(efforts or {})}
    tables: dict = {"model": {}, "effort": {}}
    evidence = policy.evidence()
    evidence["resolved"] = resolved = {}
    for role in Role:
        route = policy.route(role)
        own = policy.roles.get(role, Route())
        profile = (profiles or {}).get(role)
        if route.provider is not None and profile != route.provider:
            raise RoutingRefused(f"{role.value}: the policy declares a provider that is not "
                                 "the harness profile this run configures for the role")
        seen: dict = {"provider": {
            "value": profile or NOT_AVAILABLE,
            "source": "policy:role" if route.provider is not None else "run-profile"}}
        for field in ("model", "effort"):
            declared = getattr(route, field)
            if role in explicit[field]:
                value = explicit[field][role]
                tables[field][role] = value
                seen[field] = {"value": value, "source": "explicit"}
                if declared is not None and declared != value:
                    seen[field]["policy_value_overridden"] = declared
            elif declared is not None:
                tables[field][role] = declared
                seen[field] = {"value": declared,
                               "source": ("policy:role" if getattr(own, field) is not None
                                          else "policy:default")}
            else:
                seen[field] = {"value": NOT_AVAILABLE, "source": "harness-default"}
        resolved[role.value] = seen
    return Resolved(models=tables["model"], efforts=tables["effort"], evidence=evidence,
                    profiles=dict(profiles or {}))


# --------------------------------------------------------------------------- #
# Failover: one durable decision per run, taken from the cross-run provider health
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Decision:
    """What one role runs on: the declared route (`index` None) or the `index`-th fallback, and
    what was read about every route looked at, in order, up to the chosen one."""

    outcome: str
    index: int | None
    route: Route
    considered: tuple  # of (provider, model, state, not_before, account)
    #: P3-41 slice 4: the route receipt of a FALLBACK (`_receipt`), or None (a DECLARED outcome
    #: changes no route; a record written before this slice has none and is given a derived one
    #: when it is read, `derived`).
    receipt: dict | None = None
    derived: bool = False

    @property
    def reason(self) -> str:
        return self.considered[0][2]

    def as_dict(self) -> dict:
        out = {"outcome": self.outcome, "index": self.index, "route": _declared(self.route),
               "reason": self.reason, "trust_assessed": False,
               "considered": [{"provider": p, "model": m, "state": st, "not_before": nb,
                               **({"account": acct} if acct is not None else {})}
                              for p, m, st, nb, acct in self.considered]}
        if self.receipt is not None and not self.derived:
            out["receipt"] = self.receipt
        return out

    @property
    def verification_required(self) -> bool:
        """Does the run on this decision's route have to meet the weaker-route verification?"""
        return bool(self.receipt is not None and self.receipt["additional_verification_required"])


def _shown(decision: _Decision) -> dict:
    """A decision as the caller is shown it: with its receipt, derived or recorded."""
    body = decision.as_dict()
    if decision.receipt is not None:
        body["receipt"] = {**decision.receipt, **({"source": "derived"} if decision.derived
                                                  else {})}
    return body


def _core(decisions: dict) -> dict:
    """The decisions of a record without their receipts: what two resolutions are compared by."""
    out = {}
    for role, decision in decisions.items():
        body = decision.as_dict()
        body.pop("receipt", None)
        out[role] = body
    return out


def _routes(policy: RoutingPolicy, role: Role) -> list:
    """The declared route of `role`, then its fallbacks, in the order they are tried."""
    return [policy.route(role), *policy.fallbacks.get(role, ())]


def _provider_state(report, provider: str, now: datetime, account: str | None = None) -> tuple:
    """`(state, not_before)`: the worst thing the report says about any account of `provider`.
    A route that names no account counts a pending wait on any account of the provider against
    it. A route of kind `account` counts the waits on its own handle **and** on a group whose
    account is unknown (`NOT_AVAILABLE`: nothing says it is another account), never a wait on
    another named account. A `RESET_DUE` group whose stated reset is less than
    `RETURN_DWELL_SECONDS` old is `SETTLING`; `not_before` is the stated time of the group that
    decided (None for no evidence)."""
    found: dict[str, str | None] = {}
    for group in report.providers:
        if group.provider != provider:
            continue
        if account is not None and group.account_handle not in (account, NOT_AVAILABLE):
            continue
        state, stamp = group.state.value, group.not_before
        if state == "RESET_DUE" and stamp is not None:
            try:
                moment = datetime.fromisoformat(stamp)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if now - moment < timedelta(seconds=RETURN_DWELL_SECONDS):
                    state = SETTLING
            except ValueError:
                pass
        if state not in found or (stamp is not None and (found[state] is None or stamp > found[state])):
            found[state] = stamp
    for state in _WORST_FIRST:
        if state in found:
            return state, found[state]
    return NO_EVIDENCE, None


def _read_health(run_dir: Path, now: datetime, reader, exclude: frozenset):
    """The complete cross-run reading of the runs root `run_dir` lives in, or a refusal: a
    reading that failed, or that could not read every run, is not evidence of health. `reader`
    is `health.read_verified_health` (only parks that are still bound count) unless the caller
    gives another; the run's own park is left out, whatever the reader."""
    try:
        report = (reader or health_mod.read_verified_health)(run_dir.parent, now=now,
                                                              exclude_runs=exclude)
    except Exception:  # noqa: BLE001 - whatever the reader raises, no route is chosen on it
        raise RoutingRefused("provider health cannot be read: no route is chosen") from None
    if not report.complete:
        raise RoutingRefused(f"provider health is incomplete ({len(report.problems)} thing(s) "
                             "under the runs root could not be read): no route is chosen")
    extra = _park_evidence(run_dir, now, exclude)
    return dataclass_replace(report, providers=(*report.providers, *extra)) if extra else report


def _decide(policy: RoutingPolicy, role: Role, report, now: datetime) -> _Decision:
    routes = _routes(policy, role)
    considered = []
    for position, route in enumerate(routes):
        state, stamp = _provider_state(report, route.provider, now, route.account)
        considered.append((route.provider, route.model, state, stamp, route.account))
        if state not in UNHEALTHY:
            return _Decision("DECLARED" if position == 0 else "FALLBACK",
                             None if position == 0 else position - 1, route, tuple(considered))
    raise RoutingRefused(f"{role.value}: no route the policy declares for the role is healthy "
                         "(a limit or an availability failure is pending on each)")


def route_receipt(policy: RoutingPolicy, role: Role, route: Route, *, reason: str, cause: str,
                  old: dict | None, failure_class: str | None) -> dict:
    """The route receipt of one route change (A5: "every route change emits a durable routing
    receipt"): the old and the new route, the reason, the role, the capabilities the role requires
    and the route satisfies, the trust class of the route, the policy it was decided under and
    whether additional verification is required. A pure function of the policy and of what the
    decision looked at, so a reader can reproduce it: `_bind_one` recomputes it from the policy
    and refuses a record whose receipt says otherwise. `old` is the route the role was on (None
    when a record written before this slice does not say); `cause` is `start` or `park`."""
    required = list(CAPABILITY_REQUIREMENTS[role.value])
    held = list(route.capabilities) if route.capabilities is not None else list(CAPABILITIES)
    quality = route.quality or "weaker"
    needs = quality != "equivalent"
    return {"schema": RECEIPT_SCHEMA, "cause": cause, "role": role.value, "old_route": old,
            "new_route": _declared(route), "reason": reason, "failure_class": failure_class,
            "required_capabilities": required,
            "satisfied_capabilities": [c for c in required if c in held],
            "capabilities_declared": route.capabilities is not None,
            "trust_class": quality,
            "policy": {"schema": SCHEMA, "digest": policy.digest},
            "additional_verification_required": needs,
            "verification": (dict(policy.verification or DEFAULT_WEAKER_VERIFICATION)
                             if needs else None)}


def _with_receipt(policy: RoutingPolicy, role: Role, decision: _Decision, *, cause: str,
                  old: dict | None, failure_class: str | None) -> _Decision:
    """`decision` with its route receipt: only a FALLBACK is a route change."""
    if decision.index is None:
        return decision
    return dataclass_replace(decision, receipt=route_receipt(
        policy, role, decision.route, reason=decision.reason, cause=cause, old=old,
        failure_class=failure_class))


def _failover_document(policy: RoutingPolicy, decisions: dict, decided_at: str) -> bytes:
    body = {"schema": FAILOVER_SCHEMA, "policy_digest": policy.digest, "decided_at": decided_at,
            "decisions": {role.value: decisions[role].as_dict() for role in Role
                          if role in decisions}}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode("ascii")).hexdigest()
    return (json.dumps({**body, "digest": digest}, sort_keys=True, indent=1, ensure_ascii=True)
            + "\n").encode("ascii")


def _create(dir_fd: int, name: str, document: bytes) -> None:
    """The one create of this module, for both kinds of record: `name` in the run directory
    `dir_fd` is open on, mode 0600, linked in only if it does not exist (`FileExistsError`)."""
    durable_create(Path(name), document, mode=0o600, dir_fd=dir_fd)


def _read_failover(dir_fd: int, name: str = FAILOVER_FILE) -> dict | None:
    """The recorded document `name` of the run directory `dir_fd` is open on, or None when the run
    has none yet. Anything that is not one regular, single-name, strict-UTF-8 JSON object of at
    most `MAX_FAILOVER_BYTES` is a refusal: a link (dangling or not) is never 'no record'."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise RoutingRefused("the failover record cannot be opened (a link or unreadable)") \
            from None
    try:
        st = settled_stat(fd)           # a creator may be between its link and its drop
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise RoutingRefused("the failover record is not one regular file with one name")
        raw = b""
        while len(raw) <= MAX_FAILOVER_BYTES:
            chunk = os.read(fd, MAX_FAILOVER_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        raise RoutingRefused("the failover record cannot be read") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_FAILOVER_BYTES:
        raise RoutingRefused(f"the failover record is larger than {MAX_FAILOVER_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_repeated_key)
    except RoutingRefused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RoutingRefused("the failover record is not one UTF-8 JSON document") from None
    if not isinstance(doc, dict):
        raise RoutingRefused("the failover record is not a JSON object")
    return doc


def _bind(doc: dict, policy: RoutingPolicy) -> tuple:
    """Check a recorded document against the policy and return `(decided_at, decisions)`.

    The route of every decision is taken from the **policy**, never from the record: a record
    that names a route the policy does not declare, a position that does not match what it says
    was looked at, or a chosen route that its own reading calls unhealthy, is refused. Only
    `RoutingRefused` leaves: a value of a shape this module never writes is a refusal too."""
    try:
        return _bind_checked(doc, policy)
    except RoutingRefused:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise RoutingRefused("the failover record is not in the shape this module writes") \
            from None


def _bind_checked(doc: dict, policy: RoutingPolicy) -> tuple:
    if set(doc) != {"schema", "policy_digest", "decided_at", "decisions", "digest"}:
        raise RoutingRefused("the failover record does not have exactly its keys")
    if doc["schema"] != FAILOVER_SCHEMA:
        raise RoutingRefused(f"the failover record schema is not {FAILOVER_SCHEMA!r}")
    body = {k: v for k, v in doc.items() if k != "digest"}
    try:
        again = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True).encode("ascii")).hexdigest()
    except (TypeError, ValueError, RecursionError):
        raise RoutingRefused("the failover record is not plain JSON") from None
    if doc["digest"] != again:
        raise RoutingRefused("the failover record does not match its own digest")
    if doc["policy_digest"] != policy.digest:
        raise RoutingRefused("the failover record was made under another routing policy")
    decided_at = doc["decided_at"]
    try:
        if (not isinstance(decided_at, str) or len(decided_at) > 64
                or datetime.fromisoformat(decided_at).tzinfo is None):
            raise ValueError
    except ValueError:
        raise RoutingRefused("the failover record has no timezone-aware decision time") from None
    wanted = {role for role in Role if policy.fallbacks.get(role)}
    recorded = doc["decisions"]
    if not isinstance(recorded, dict) or set(recorded) != {r.value for r in wanted}:
        raise RoutingRefused("the failover record decides other roles than the policy gives "
                             "fallbacks")
    decisions = {role: _bind_one(role, recorded[role.value], _routes(policy, role), policy,
                                 "start")
                 for role in Role if role in wanted}
    return decided_at, decisions


_DECISION_KEYS = frozenset({"outcome", "index", "route", "reason", "trust_assessed", "considered"})
_CONSIDERED_KEYS = frozenset({"provider", "model", "state", "not_before"})


def _checked_old(old: object, role: Role, policy: RoutingPolicy, cause: str) -> dict | None:
    """The `old_route` a stored receipt names: for a start it is the declared route (the policy
    gives it); for a park it is what the parked dispatch ran on, which only a park's own receipt
    could say, so it is checked for shape (a provider the policy declares for the role, a model
    name, an optional account handle) and nothing else."""
    if cause == "start":
        if old != _declared(policy.route(role)):
            raise RoutingRefused("a route receipt names an old route other than the declared one")
        return old
    if (not isinstance(old, dict) or not set(old) <= {"provider", "model", "account"}
            or not {"provider", "model"} <= set(old)
            or old["provider"] not in {r.provider for r in _routes(policy, role)}
            or not isinstance(old["model"], str)
            or (old["model"] != NOT_AVAILABLE and _MODEL_SHAPE.fullmatch(old["model"]) is None)):
        raise RoutingRefused("a route receipt names an old route that is not one the policy "
                             "declares for the role")
    if "account" in old:
        try:
            if not isinstance(old["account"], str) or check_account_handle(old["account"]) \
                    != old["account"] or old["account"] == NOT_AVAILABLE:
                raise RecoveryError("handle")
        except RecoveryError:
            raise RoutingRefused("a route receipt names an old account that is not a plain "
                                 "handle") from None
    return old


def _check_receipt(stored: object, role: Role, chosen: Route, reason: str, policy: RoutingPolicy,
                   cause: str) -> dict:
    """A stored route receipt, reproduced: its two inputs a record alone can give (the old route of
    a park and the failure class) are checked for shape, then the whole receipt is recomputed from
    the policy and must be equal."""
    if not isinstance(stored, dict) or set(stored) != {
            "schema", "cause", "role", "old_route", "new_route", "reason", "failure_class",
            "required_capabilities", "satisfied_capabilities", "capabilities_declared",
            "trust_class", "policy", "additional_verification_required", "verification"}:
        raise RoutingRefused("a route receipt does not have exactly its keys")
    if stored["cause"] != cause:
        raise RoutingRefused("a route receipt names another cause than the record that holds it")
    old = _checked_old(stored["old_route"], role, policy, cause)
    failure = stored["failure_class"]
    if cause == "start":
        if failure is not None:
            raise RoutingRefused("a route receipt of a start names a failure class")
    else:
        try:
            if (not isinstance(failure, str)
                    or health_mod.PROVIDER_EVIDENCE[health_mod.NormalizedFailure(failure)] is None):
                raise ValueError
        except (ValueError, KeyError):
            raise RoutingRefused("a route receipt of a park names a failure class that is no "
                                 "provider evidence") from None
    again = route_receipt(policy, role, chosen, reason=reason, cause=cause, old=old,
                     failure_class=failure)
    if stored != again:
        raise RoutingRefused("a route receipt says other than the policy gives for the route it "
                             "names (trust class, capabilities, verification or policy)")
    return again


def _bind_one(role: Role, body: object, routes: list, policy: RoutingPolicy | None = None,
              cause: str = "start") -> _Decision:
    where = f"the failover record of {role.value}"
    if not isinstance(body, dict) or set(body) - {"receipt"} != _DECISION_KEYS:
        raise RoutingRefused(f"{where} does not have exactly its keys")
    outcome, index, considered = body["outcome"], body["index"], body["considered"]
    if outcome == "DECLARED":
        position = 0
        if index is not None:
            raise RoutingRefused(f"{where} names a fallback for the declared route")
    elif outcome == "FALLBACK":
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(routes) - 1:
            raise RoutingRefused(f"{where} names a fallback the policy does not declare")
        position = index + 1
    else:
        raise RoutingRefused(f"{where} has an outcome that is neither DECLARED nor FALLBACK")
    if body["trust_assessed"] is not False:
        raise RoutingRefused(f"{where} claims a trust assessment this increment does not make")
    if not isinstance(considered, list) or len(considered) != position + 1:
        raise RoutingRefused(f"{where} does not list the routes it looked at")
    seen = []
    for at, item in enumerate(considered):
        route = routes[at]
        if (not isinstance(item, dict)
                or set(item) != _CONSIDERED_KEYS | ({"account"} if route.account is not None
                                                    else set())
                or item["provider"] != route.provider or item["model"] != route.model
                or item.get("account") != route.account
                or not isinstance(item["state"], str) or item["state"] not in _STATES):
            raise RoutingRefused(f"{where} lists a route the policy does not declare")
        if (item["state"] in UNHEALTHY) != (at < position):
            raise RoutingRefused(f"{where} chose a route its own reading calls unhealthy, or "
                                 "passed over a healthy one")
        stamp = item["not_before"]
        try:
            if stamp is not None and (not isinstance(stamp, str) or len(stamp) > 64
                                      or datetime.fromisoformat(stamp).tzinfo is None):
                raise ValueError
        except ValueError:
            raise RoutingRefused(f"{where} has a reset time that is no aware time") from None
        seen.append((item["provider"], item["model"], item["state"], stamp, route.account))
    chosen = routes[position]
    if body["route"] != _declared(chosen) or body["reason"] != seen[0][2]:
        raise RoutingRefused(f"{where} names a route or a reason that is not what it looked at")
    receipt, derived = None, False
    if "receipt" in body:
        if outcome != "FALLBACK" or policy is None:
            raise RoutingRefused(f"{where} carries a route receipt it has no route change for")
        receipt = _check_receipt(body["receipt"], role, chosen, seen[0][2], policy, cause)
    elif outcome == "FALLBACK" and policy is not None:
        # written before route receipts existed: the receipt the policy gives, for the run to be
        # held to it (`derived`: it is not on disk and is not shown as recorded)
        receipt = route_receipt(policy, role, chosen, reason=seen[0][2], cause=cause,
                           old=_declared(policy.route(role)) if cause == "start" else None,
                           failure_class=None)
        derived = True
    return _Decision(outcome, index, chosen, tuple(seen), receipt, derived)


def _apply(base: Resolved, policy: RoutingPolicy, decisions: dict, *, models: Mapping | None,
           efforts: Mapping | None, decided_at: str) -> Resolved:
    """`base` with each failed-over role's provider, model and effort replaced by the fallback
    the decision chose (a role that stays is left as `resolve` made it)."""
    tables = {"model": dict(base.models), "effort": dict(base.efforts)}
    profiles = dict(base.profiles)
    accounts = dict(base.accounts)
    evidence = base.evidence
    weaker: list = []
    for role, decision in decisions.items():
        if decision.index is None:
            continue
        if decision.verification_required:
            weaker.append(role.value)
        if decision.route.account is not None:
            accounts[role] = decision.route.account
        if role in (models or {}):
            raise RoutingRefused(f"{role.value}: the role fails over and the caller's explicit "
                                 "model is bound to the declared route")
        route, seen = decision.route, evidence["resolved"][role.value]
        declared_provider = seen["provider"]["value"]
        tables["model"][role] = route.model
        seen["provider"] = {"value": route.provider, "source": "policy:failover",
                            "declared_provider": declared_provider,
                            **({"account": route.account} if route.account is not None else {})}
        seen["model"] = {"value": route.model, "source": "policy:failover"}
        profiles[role] = route.provider
        if role in (efforts or {}):
            continue                      # an explicit effort still wins, as in `resolve`
        value = route.effort if route.effort is not None else policy.default.effort
        if value is None:
            tables["effort"].pop(role, None)
            seen["effort"] = {"value": NOT_AVAILABLE, "source": "harness-default"}
        else:
            tables["effort"][role] = value
            seen["effort"] = {"value": value,
                              "source": ("policy:failover" if route.effort is not None
                                         else "policy:default")}
    evidence["failover"] = {"schema": FAILOVER_SCHEMA, "file": FAILOVER_FILE,
                            "policy_digest": policy.digest, "decided_at": decided_at,
                            "decisions": {role.value: _shown(decisions[role])
                                          for role in Role if role in decisions}}
    if weaker:
        # what the controller acts on: a candidate of a run with a role on a weaker route is
        # accepted only when the policy's verification is met (`controller`)
        evidence["verification"] = {
            "required": True, "roles": weaker,
            "measures": dict(policy.verification or DEFAULT_WEAKER_VERIFICATION)}
    return Resolved(models=tables["model"], efforts=tables["effort"], evidence=evidence,
                    profiles=profiles, accounts=accounts)


class FailoverPlan:
    """A failover decision that has been made and not yet recorded.

    `resolved` is what the caller builds its dispatcher from. `commit()` makes the decision
    durable -- one create-only record in the run directory this plan opened -- and must be
    called before the first dispatch; until then nothing is written, so an invocation refused
    in between leaves no record. The plan holds the run directory open (a descriptor, not a
    name): `close()` releases it (idempotent; also at garbage collection)."""

    def __init__(self, *, resolved: Resolved, dir_fd: int, document: bytes | None,
                 decisions: dict, policy: RoutingPolicy, profiles: Mapping,
                 models: Mapping | None, efforts: Mapping | None,
                 stayed: str | None = None) -> None:
        self.resolved = resolved
        #: Why no decision was made and the run keeps its declared route (None: one was made, or
        #: nothing was asked). Never a refusal: the run starts as it did before this module.
        self.stayed = stayed
        self._dir_fd: int | None = dir_fd
        self._document = document
        self._decisions = decisions
        self._policy = policy
        self._args = (profiles, models, efforts)

    @property
    def decided(self) -> bool:
        """Does this run have a failover decision (made now or replayed)?"""
        return "failover" in self.resolved.evidence

    @property
    def stays(self) -> bool:
        """Was a decision asked for and not made (the run keeps its declared route, unrecorded)?"""
        return self.stayed is not None

    @property
    def pending(self) -> bool:
        """Is there a decision of this call that is not on disk yet?"""
        return self._document is not None

    def close(self) -> None:
        fd, self._dir_fd = self._dir_fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:  # pragma: no cover - a plan nobody closed
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def commit(self, *, adopt: bool = False) -> Resolved:
        """Record the decision, or return at once when there is nothing to record.

        A lost create race (another resolution of the run recorded first) is read: the same
        decisions are fine, other decisions are `RoutingRefused` -- the caller has built on
        this plan -- unless `adopt` (the one-shot `resolve_failover`, which has built nothing)
        takes the winner's. `DurableWriteNotSynced` -- raised after the record was linked in --
        refuses with the record in place; any other write failure refuses and leaves none."""
        if self._document is None:
            return self.resolved
        if self._dir_fd is None:
            raise RoutingRefused("the failover plan was closed before it was recorded")
        profiles, models, efforts = self._args
        try:
            _create(self._dir_fd, FAILOVER_FILE, self._document)
        except FileExistsError:
            doc = _read_failover(self._dir_fd)
            if doc is None:
                raise RoutingRefused("the failover record vanished while it was being "
                                     "written") from None
            decided_at, won = _bind(doc, self._policy)
            if _core(won) != _core(self._decisions):
                if not adopt:
                    raise RoutingRefused("another resolution of this run recorded a different "
                                         "failover decision first; this one is not used")
                self.resolved = _apply(
                    resolve(self._policy, profiles=profiles, models=models, efforts=efforts),
                    self._policy, won, models=models, efforts=efforts, decided_at=decided_at)
                self._decisions = won
            self._document = None
            return self.resolved
        except DurableWriteNotSynced:
            self._document = None
            raise RoutingRefused("the failover record is in place but could not be made "
                                 "durable: nothing is returned; resolving again replays the "
                                 "record") from None
        except OSError:
            raise RoutingRefused("the failover decision cannot be recorded: no route is "
                                 "chosen") from None
        self._document = None
        return self.resolved


def _open_run(run_dir) -> tuple:
    """`(absolute run directory, descriptor)` of a run directory that is a real directory (a link
    is not followed), or `RoutingRefused`."""
    if not isinstance(run_dir, (str, os.PathLike)) or not isinstance(os.fspath(run_dir), str):
        raise RoutingRefused("the run directory is not a path")
    if not os.fspath(run_dir):
        raise RoutingRefused("the run directory is not a path")
    try:
        # absolute and lexically normal, so that `.` and `a/..` have the parent they really have
        run = Path(os.path.abspath(run_dir))
    except (OSError, ValueError):
        raise RoutingRefused("the run directory cannot be read") from None
    if run.parent == run:
        raise RoutingRefused("the filesystem root is no run directory")
    try:
        # one descriptor, opened without following a link and only if it is a directory: the
        # record is read and created **inside this directory**, whatever the name is made to mean
        # afterwards (`durable_create(dir_fd=)`)
        return run, os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, ValueError):
        raise RoutingRefused("the run directory cannot be opened (missing, a link, not a "
                             "directory or unreadable)") from None


def plan_failover(policy: RoutingPolicy, *, run_dir: Path | str, profiles: Mapping,
                  models: Mapping | None = None, efforts: Mapping | None = None,
                  now: datetime | None = None, reader=None,
                  master: MasterBinding | None = None, begun: bool = False,
                  strict: bool = False) -> FailoverPlan:
    """`resolve`, then the run's failover decision for every role that declares fallbacks --
    decided, **not yet recorded** (`FailoverPlan.commit`).

    A run that has a record replays it (health is not read, nothing is pending); so does a run
    with a park failover (`record_park_failover`) that has not reached its `return_at`, whatever
    else it has or has begun: every unspent record applies over the start record, the newest of
    a role wins. A run that has none: the policy declares no fallback ->
    `resolve`'s result, nothing read, nothing pending; `begun` (the run has iteration history) ->
    it keeps its declared route, nothing read, nothing recorded (`stayed` says so: a run is never
    moved in the middle of its life, except by a park failover); else `master` (a governed
    `MasterBinding`) must be the current master of the project that owns the run
    (`dispatchrecovery.stale_master_reason`; a stale master cannot fail over),
    `reader(root, now=, exclude_runs=)` (default `health.read_verified_health`) reads the runs
    root completely without this run's own park, and each role takes the first declared route
    that is not `LIMITED`, `DEGRADED` or `SETTLING`.

    **A provider stop is never a halt.** With no healthy route for a role, a reading that could not
    be completed, or an explicit `models` value that the failover would orphan, the plan is the
    declared route, **nothing is recorded**, and `stayed` says why: the run starts as it did before
    this module and meets the provider's limit the way every run does (a park with a timer). Only
    with `strict=True` -- the one-shot `resolve_failover` -- are these refusals. Everything else
    (a stale master, a record that is not ours, a record that cannot be written, a bad run
    directory) is `RoutingRefused` in both modes."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise RoutingRefused("now must be a timezone-aware datetime")
    run, dir_fd = _open_run(run_dir)
    try:
        base = resolve(policy, profiles=profiles, models=models, efforts=efforts)
        doc = _read_failover(dir_fd)
        wanted = [role for role in Role if policy.fallbacks.get(role)]
        def stay(why: str) -> FailoverPlan:
            return FailoverPlan(resolved=base, dir_fd=dir_fd, document=None, decisions={},
                                policy=policy, profiles=profiles, models=models,
                                efforts=efforts, stayed=why)

        if doc is None and not wanted:
            return FailoverPlan(resolved=base, dir_fd=dir_fd, document=None, decisions={},
                                policy=policy, profiles=profiles, models=models, efforts=efforts)
        # P3-41 slice 3: the newest park failover of the run moves its role until `return_at`,
        # over the start record and for a run that has begun; after it the record is spent.
        parks = _read_parks(dir_fd, policy) if wanted else []
        live = [park for park in parks if now < park.return_at]
        if doc is not None or live:
            decided_at, decisions = _bind(doc, policy) if doc is not None else (None, {})
            for park in live:                  # in number order: the newest of a role wins
                decided_at, decisions = park.decided_at, {**decisions, **park.decisions}
            resolved = _apply(base, policy, decisions, models=models, efforts=efforts,
                              decided_at=decided_at)
            if live:
                resolved.evidence["failover"]["park_failovers"] = [
                    {"file": PARK_FAILOVER_FILE.format(n=park.number), "park_id": park.park_id,
                     "return_at": park.return_at.isoformat()} for park in live]
            return FailoverPlan(resolved=resolved, dir_fd=dir_fd, document=None,
                                decisions=decisions, policy=policy, profiles=profiles,
                                models=models, efforts=efforts)
        if begun:
            return stay("the run has already begun: it keeps its declared route")
        if master is not None and master.kind == "GOVERNED":
            try:
                stale = stale_master_reason(run, master,
                                            now=now.astimezone(timezone.utc)
                                            .strftime("%Y-%m-%dT%H:%M:%SZ"))
            except Exception:  # noqa: BLE001 - a master that cannot be judged cannot decide
                stale = "the declared master cannot be confirmed"
            if stale is not None:
                raise RoutingRefused(f"a stale master cannot fail over: {stale}")
        try:
            report = _read_health(run, now, reader, frozenset({run.name}))
            decisions = {role: _with_receipt(
                policy, role, _decide(policy, role, report, now), cause="start",
                old=_declared(policy.route(role)), failure_class=None) for role in wanted}
            decided_at = now.isoformat()
            resolved = _apply(base, policy, decisions, models=models, efforts=efforts,
                              decided_at=decided_at)
        except RoutingRefused as exc:
            if strict:
                raise
            return stay(str(exc))
        return FailoverPlan(resolved=resolved, dir_fd=dir_fd,
                            document=_failover_document(policy, decisions, decided_at),
                            decisions=decisions, policy=policy, profiles=profiles,
                            models=models, efforts=efforts)
    except BaseException:
        os.close(dir_fd)
        raise


def resolve_failover(policy: RoutingPolicy, *, run_dir: Path | str, profiles: Mapping,
                     models: Mapping | None = None, efforts: Mapping | None = None,
                     now: datetime | None = None, reader=None,
                     master: MasterBinding | None = None, begun: bool = False) -> Resolved:
    """`plan_failover` and `FailoverPlan.commit` in one call, for a caller that builds nothing
    from the plan in between: the result is on disk before it is returned, and a lost create race
    takes the winner's decision.

    The one-shot function is **strict** (`plan_failover(strict=True)`): where the plan of a caller
    that builds a dispatcher stays on the declared route, this refuses.

    Refuses (`RoutingRefused`, nothing started): everything `resolve` refuses; a `run_dir` that is
    not a real directory, or is a link; a stale governed `master`; health that cannot be read
    completely; no healthy declared route for a role (a durable park is a later slice: a caller
    must not read this as a verdict on the work); a failed-over role with an explicit `models`
    value; a record that is a link, not one regular file, over `MAX_FAILOVER_BYTES`, not UTF-8
    JSON, not its own digest, made under another policy, or naming a route the policy does not
    declare; a record that cannot be written. Always pass the **declared** `profiles` (never a
    previous result's `profiles`): `resolve` asserts the declared provider. A refusal after the
    record's link was made (`DurableWriteNotSynced`) leaves the record in place: the next call
    replays it, so the refusal is "not known to be durable", not "nothing was chosen". `now`
    must be timezone-aware."""
    plan = plan_failover(policy, run_dir=run_dir, profiles=profiles, models=models,
                         efforts=efforts, now=now, reader=reader, master=master, begun=begun,
                         strict=True)
    try:
        return plan.commit(adopt=True)
    finally:
        plan.close()


def _receipt_view(decision: dict) -> dict:
    """The part of a recorded route receipt a person reading a run is shown."""
    receipt = decision.get("receipt")
    if not isinstance(receipt, dict):
        return {}
    return {"trust_class": receipt.get("trust_class"),
            "additional_verification_required": receipt.get("additional_verification_required")}


def _park_summary(dir_fd: int) -> list:
    """The park failover records of the run directory `dir_fd` is open on, in number order, as
    a person is shown them (the file, the park, the role, the route it moved to, the reason, the
    failure class, when it was decided, when it returns, and what its receipt says of the trust
    class). Only what the record alone can say (`_park_facts`); `RoutingRefused` for a record that
    is not its own."""
    out = []
    for number in range(1, MAX_PARK_FAILOVERS + 1):
        doc = _read_failover(dir_fd, PARK_FAILOVER_FILE.format(n=number))
        if doc is None:
            continue
        _returns, name, decision = _park_facts(doc, number)
        receipt = decision.get("receipt") if isinstance(decision.get("receipt"), dict) else {}
        out.append({"file": PARK_FAILOVER_FILE.format(n=number), "number": number,
                    "park_id": doc["park_id"], "role": name[:16],
                    "route": decision.get("route"), "reason": decision.get("reason"),
                    "failure_class": receipt.get("failure_class"),
                    "decided_at": doc["decided_at"], "return_at": doc["return_at"],
                    **_receipt_view(decision)})
    return out


def failover_summary(run_dir: Path | str) -> dict | None:
    """What a person reading a run is shown: None when the run has no failover record and no park
    failover record, else `{"decided_at", "roles": {role: {"outcome", "route", "reason",
    "not_before"}}, "park_failovers": [...]}` (the park records, P3-41 slice 4; the key is absent
    without one; a run with park records and no start record shows only them), or
    `{"unreadable": <fixed text>}` for a record that is not what this module writes (a link, over
    the bound, not its own digest). Reads only; never raises."""
    try:
        _, dir_fd = _open_run(run_dir)
    except RoutingRefused as exc:
        return {"unreadable": str(exc)}
    try:
        doc = _read_failover(dir_fd)
        parks = _park_summary(dir_fd)
        if doc is None:
            return {"park_failovers": parks} if parks else None
        if doc.get("schema") != FAILOVER_SCHEMA:
            return {"unreadable": f"the failover record schema is not {FAILOVER_SCHEMA!r}"}
        body = {k: v for k, v in doc.items() if k != "digest"}
        again = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True).encode("ascii")).hexdigest()
        if doc.get("digest") != again:
            return {"unreadable": "the failover record does not match its own digest"}
        roles = {}
        for name, d in doc["decisions"].items():
            roles[str(name)[:16]] = {"outcome": d["outcome"], "route": d["route"],
                                     "reason": d["reason"],
                                     "not_before": d["considered"][0]["not_before"],
                                     **_receipt_view(d)}
        return {"decided_at": doc["decided_at"], "roles": roles,
                **({"park_failovers": parks} if parks else {})}
    except RoutingRefused as exc:
        return {"unreadable": str(exc)}
    except (TypeError, ValueError, KeyError, AttributeError, IndexError, RecursionError):
        return {"unreadable": "the failover record is not in the shape this module writes"}
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------- #
# The park: a run parked on a provider wait is resumed on a declared fallback (P3-41, slice 3)
# --------------------------------------------------------------------------- #

_PARK_KEYS = frozenset({"schema", "policy_digest", "decided_at", "park_id", "number",
                        "return_at", "decisions", "digest"})
_PARK_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class _Park:
    """One read-back park failover record: a single role moved to the `index`-th fallback."""

    number: int
    park_id: str
    decided_at: str
    return_at: datetime
    decisions: dict  # Role -> _Decision; exactly one role, and always a FALLBACK


@dataclass(frozen=True)
class ParkFailover:
    """What `record_park_failover` recorded, or found recorded, for one park."""

    role: Role
    number: int
    route: Route
    return_at: str
    reused: bool


def _stamp(value: object, where: str) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError
    except ValueError:
        raise RoutingRefused(f"{where} is no timezone-aware time") from None
    return moment


def _body_digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def _park_document(policy: RoutingPolicy, *, park_id: str, number: int, role: Role,
                   decision: _Decision, decided_at: str, return_at: str) -> bytes:
    body = {"schema": PARK_FAILOVER_SCHEMA, "policy_digest": policy.digest,
            "decided_at": decided_at, "park_id": park_id, "number": number,
            "return_at": return_at, "decisions": {role.value: decision.as_dict()}}
    return (json.dumps({**body, "digest": _body_digest(body)}, sort_keys=True, indent=1,
                       ensure_ascii=True) + "\n").encode("ascii")


def _bind_park(doc: dict, policy: RoutingPolicy, number: int) -> _Park:
    """Check a park failover record against the policy and the number of its file. As for the
    start record the route comes from the **policy**, never from the record. Only
    `RoutingRefused` leaves."""
    try:
        return _bind_park_checked(doc, policy, number)
    except RoutingRefused:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise RoutingRefused("a park failover record is not in the shape this module "
                             "writes") from None


def _bind_park_checked(doc: dict, policy: RoutingPolicy, number: int) -> _Park:
    if set(doc) != _PARK_KEYS:
        raise RoutingRefused("a park failover record does not have exactly its keys")
    if doc["schema"] != PARK_FAILOVER_SCHEMA:
        raise RoutingRefused(f"a park failover record schema is not {PARK_FAILOVER_SCHEMA!r}")
    try:
        again = _body_digest({k: v for k, v in doc.items() if k != "digest"})
    except (TypeError, ValueError, RecursionError):
        raise RoutingRefused("a park failover record is not plain JSON") from None
    if doc["digest"] != again:
        raise RoutingRefused("a park failover record does not match its own digest")
    if doc["policy_digest"] != policy.digest:
        raise RoutingRefused("a park failover record was made under another routing policy")
    if isinstance(doc["number"], bool) or doc["number"] != number:
        raise RoutingRefused("a park failover record names another number than its file")
    if not isinstance(doc["park_id"], str) or _PARK_ID.fullmatch(doc["park_id"]) is None:
        raise RoutingRefused("a park failover record has no park id")
    decided = _stamp(doc["decided_at"], "the decision time of a park failover record")
    returns = _stamp(doc["return_at"], "the return time of a park failover record")
    if returns < decided:
        raise RoutingRefused("a park failover record returns before it was decided")
    recorded = doc["decisions"]
    if not isinstance(recorded, dict) or len(recorded) != 1:
        raise RoutingRefused("a park failover record decides other than exactly one role")
    (name,) = recorded
    role = _ROLE_BY_NAME.get(name)
    if role is None or not policy.fallbacks.get(role):
        raise RoutingRefused("a park failover record decides a role the policy gives no "
                             "fallbacks")
    decision = _bind_one(role, recorded[name], _routes(policy, role), policy, "park")
    if decision.index is None:
        raise RoutingRefused("a park failover record names the declared route: it moves a role "
                             "away from it")
    return _Park(number=number, park_id=doc["park_id"], decided_at=doc["decided_at"],
                 return_at=returns, decisions={role: decision})


def _read_parks(dir_fd: int, policy: RoutingPolicy) -> list:
    """Every park failover record of the run, in number order (`RoutingRefused` for a damaged
    one). Only the names `1..MAX_PARK_FAILOVERS` are looked at: any other file is not read."""
    found = []
    for number in range(1, MAX_PARK_FAILOVERS + 1):
        doc = _read_failover(dir_fd, PARK_FAILOVER_FILE.format(n=number))
        if doc is not None:
            found.append(_bind_park(doc, policy, number))
    return found


def _park_facts(doc: dict, number: int) -> tuple:
    """`(return_at, role name, decision)` of a park failover record as far as the record alone can
    say: its schema, its own digest, its number, its park id, its two times, exactly one decision. No
    policy is consulted (the routes of another run's policy are not this run's to read): a reader
    that needs more binds the record (`_bind_park`). `RoutingRefused` for anything else."""
    try:
        if (set(doc) != _PARK_KEYS or doc["schema"] != PARK_FAILOVER_SCHEMA
                or doc["digest"] != _body_digest({k: v for k, v in doc.items() if k != "digest"})
                or isinstance(doc["number"], bool) or doc["number"] != number
                or not isinstance(doc["park_id"], str)
                or _PARK_ID.fullmatch(doc["park_id"]) is None
                or not isinstance(doc["policy_digest"], str)
                or not isinstance(doc["decisions"], dict) or len(doc["decisions"]) != 1):
            raise RoutingRefused("a park failover record is not what this module writes")
        decided = _stamp(doc["decided_at"], "the decision time of a park failover record")
        returns = _stamp(doc["return_at"], "the return time of a park failover record")
        if returns < decided:
            raise RoutingRefused("a park failover record returns before it was decided")
        ((name, decision),) = doc["decisions"].items()
        if (name not in _ROLE_BY_NAME or not isinstance(decision, dict)
                or decision.get("outcome") != "FALLBACK"
                or not isinstance(decision.get("considered"), list) or not decision["considered"]):
            raise RoutingRefused("a park failover record decides other than one fallback")
        return returns, name, decision
    except RoutingRefused:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise RoutingRefused("a park failover record is not in the shape this module "
                             "writes") from None


def _park_evidence(run_dir: Path, now: datetime, exclude: frozenset) -> tuple:
    """The `ProviderHealth` groups the park failovers of the **other** runs under the root say
    (P3-41 slice 4, s3's Q3): a run that failed over at a park lifted that park, so the reading of
    waits no longer shows its provider as out; the record does, for every route it passed over
    that was `LIMITED` or `DEGRADED` with a stated reset, until that reset and for the
    `RETURN_DWELL_SECONDS` after it (`SETTLING`). A record counts only when it is its own
    (`_park_facts`) and, for a run that has a pin (`routing.json`, every run `hoh run` bound to a
    policy), made under the policy that run is pinned to (the digest). A record that cannot be
    read or does not hold is no evidence **and** an incomplete reading (`RoutingRefused`, no route
    is chosen): the fail-closed rule of the reading it extends. Reads only."""
    root = run_dir.parent
    try:
        names = sorted(os.listdir(root))
    except OSError:
        raise RoutingRefused("provider health cannot be read: no route is chosen") from None
    groups = []
    for name in names:
        if (name in exclude or name.startswith(health_mod.RESERVED_PREFIXES)
                or name in health_mod.RESERVED_RUN_IDS):
            continue
        try:
            if not stat.S_ISDIR(os.lstat(root / name).st_mode):
                continue                    # a file or a link: the reading of waits says so
            fd = os.open(root / name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue                        # it went away while the root was being read
        except OSError:
            raise RoutingRefused("a run directory under the runs root cannot be opened: no "
                                 "route is chosen") from None
        try:
            try:
                listing = set(os.listdir(fd))
            except OSError:
                raise RoutingRefused("a run directory under the runs root cannot be read: no "
                                     "route is chosen") from None
            for number in range(1, MAX_PARK_FAILOVERS + 1):
                file_name = PARK_FAILOVER_FILE.format(n=number)
                if file_name not in listing:
                    continue
                doc = _read_failover(fd, file_name)
                if doc is None:
                    continue
                _returns, _role, decision = _park_facts(doc, number)
                pin = _read_failover(fd, "routing.json")
                if pin is not None and pin.get("digest") != doc["policy_digest"]:
                    raise RoutingRefused("a park failover record of another run is not made "
                                         "under the policy that run is pinned to")
                for item in decision["considered"][:-1]:
                    state = item.get("state") if isinstance(item, dict) else None
                    if state not in ("LIMITED", "DEGRADED") or item.get("not_before") is None:
                        continue            # no stated reset: nothing to bound the evidence by
                    # a provider's own limit lasts at most `quota.MAX_STATED_DATE_WAIT` from the
                    # moment it was decided on: a reset further out is read as that far, never longer
                    until = min(
                        _stamp(item.get("not_before"), "the reset of a route passed over"),
                        _stamp(doc["decided_at"], "the decision time of a park failover record")
                        + quota.MAX_STATED_DATE_WAIT)
                    provider, account = item.get("provider"), item.get("account", NOT_AVAILABLE)
                    if provider not in PROVIDERS or not isinstance(account, str):
                        continue
                    # the route's own clock, as for a wait that is lifted: out until its stated
                    # reset, then `RESET_DUE` (which `_provider_state` reads as `SETTLING` for
                    # `RETURN_DWELL_SECONDS`), then nothing. The record's `return_at` is the
                    # run's own business and does not end what the record said about a route.
                    if now < until:
                        shown = health_mod.HealthState(state)
                    elif now < until + timedelta(seconds=RETURN_DWELL_SECONDS):
                        shown = health_mod.HealthState.RESET_DUE
                    else:
                        continue
                    groups.append(health_mod.ProviderHealth(
                        provider=provider, account_handle=account, state=shown,
                        not_before=until.isoformat(), runs=(name,),
                        failures=(("PARK_FAILOVER", 1),)))
        finally:
            os.close(fd)
    return tuple(groups)


def record_park_failover(policy: RoutingPolicy, *, run_dir: Path | str, receipt,
                         now: datetime | None = None, reader=None) -> ParkFailover:
    """Decide, and record, that the run parked on `receipt` resumes on a declared fallback.

    `receipt` is the `dispatchrecovery.RoutingDecision` of a park the caller has already verified
    (`verify_binding` BOUND, not yet lifted) and whose stated reset has not come. The parked role
    is the receipt's; its provider is out until `receipt.retry_at` (evidence that is the park
    itself, which the verified reading of the *other* runs under the root does not contain). The
    role takes the first route it declares whose provider is not `LIMITED`, `DEGRADED` or
    `SETTLING` (`_decide`); the decision is recorded create-only as
    `routing-failover-park-<n>.json`, with `return_at`, before the caller lifts the park. A record
    that already names this park is returned as it is (`reused`) while it has not reached its
    `return_at`, and refuses once it has (a spent record is no decision, and a second one for the
    same park is never written): an attempt that crashed after the record and before the lift is
    finished, not repeated.

    Refuses (`RoutingRefused`, nothing recorded) -- and the caller leaves the park to wait for its
    reset -- when: the role declares no fallback; the park is on a route the policy does not
    declare for the role; the failure class says nothing about a provider (a role timeout), or is
    a short wait (not a usage limit, and under `MIN_FAILOVER_WAIT_SECONDS` left); the reset is
    not after `now` or is further than a provider's own limit can be
    (`quota.MAX_STATED_DATE_WAIT`);
    health cannot be read completely; no route of the role is healthy, or the first healthy one is
    the declared route; the route the run returns to (its start-record route for the role, else
    the declared one) is not one that was passed over, or states no reset; `MAX_PARK_FAILOVERS` records
    exist; a record of the run is damaged or made under another policy; the record cannot be
    written (`DurableWriteNotSynced` leaves it in place: the next call reuses it)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise RoutingRefused("now must be a timezone-aware datetime")
    run, dir_fd = _open_run(run_dir)
    try:
        prov = receipt.provenance
        role = _ROLE_BY_NAME.get(prov.role)
        if role is None or not policy.fallbacks.get(role):
            raise RoutingRefused("the parked role declares no fallback route")
        if prov.provider not in {route.provider for route in _routes(policy, role)}:
            raise RoutingRefused("the park is on a route the policy does not declare for the role")
        parks = _read_parks(dir_fd, policy)
        if parks and parks[-1].park_id == receipt.park_id:
            again = parks[-1]
            if not now < again.return_at:
                raise RoutingRefused("the park failover record of this park is spent")
            (decision,) = again.decisions.values()
            return ParkFailover(role=next(iter(again.decisions)), number=again.number,
                                route=decision.route, return_at=again.return_at.isoformat(),
                                reused=True)
        if parks and parks[-1].number >= MAX_PARK_FAILOVERS:
            raise RoutingRefused(f"the run has used its {MAX_PARK_FAILOVERS} park failovers")
        try:
            evidence = health_mod.PROVIDER_EVIDENCE[health_mod.NormalizedFailure(receipt.reason)]
        except (KeyError, ValueError):
            evidence = None
        if evidence is None:
            raise RoutingRefused("the parked failure says nothing about the provider")
        retry_at = _stamp(receipt.retry_at, "the reset of the park")
        if not now < retry_at <= now + quota.MAX_STATED_DATE_WAIT:
            raise RoutingRefused("the reset of the park is past or further away than a "
                                 "provider's limit lasts")
        if (receipt.reason != health_mod.NormalizedFailure.USAGE_LIMIT.value
                and retry_at - now < timedelta(seconds=MIN_FAILOVER_WAIT_SECONDS)):
            raise RoutingRefused("the park is a short wait: it waits, and spends no record")
        report = _read_health(run, now, reader, frozenset({run.name}))
        out = health_mod.ProviderHealth(
            provider=prov.provider, account_handle=prov.account_handle,
            state=(health_mod.HealthState.LIMITED if evidence is health_mod.Evidence.LIMIT
                   else health_mod.HealthState.DEGRADED),
            not_before=receipt.retry_at, runs=(run.name,), failures=((receipt.reason, 1),))
        report = dataclass_replace(report, providers=(*report.providers, out))
        decision = _decide(policy, role, report, now)
        if decision.index is None:
            raise RoutingRefused("the first healthy route of the role is the declared one: "
                                 "nothing to fail over to")
        # the route receipt: the route the parked dispatch ran on is the old route (a model name
        # this module would not read back is recorded as unknown, never as a record it refuses)
        model = prov.model if (prov.model == NOT_AVAILABLE or (
            isinstance(prov.model, str) and _MODEL_SHAPE.fullmatch(prov.model))) else NOT_AVAILABLE
        decision = _with_receipt(
            policy, role, decision, cause="park", failure_class=receipt.reason,
            old={"provider": prov.provider, "model": model,
                 **({"account": prov.account_handle}
                    if prov.account_handle != NOT_AVAILABLE else {})})
        # the route the run returns to when the record is spent: its start record's for the role,
        # else the declared one; the record ends when THAT route is back (not the earliest of the
        # routes passed over: another one may be back long before it)
        doc = _read_failover(dir_fd)
        began = _bind(doc, policy)[1].get(role) if doc is not None else None
        back = (began.route if began is not None else _routes(policy, role)[0])
        stated = [nb for provider, model, _state, nb, account in decision.considered[:-1]
                  if (provider, model, account) == (back.provider, back.model, back.account)]
        if not stated or stated[0] is None:
            raise RoutingRefused("the route the run returns to was not passed over or states no "
                                 "reset: no return can be bound")
        returns = _stamp(stated[0], "the reset of the route returned to") \
            + timedelta(seconds=RETURN_DWELL_SECONDS)
        number = (parks[-1].number if parks else 0) + 1
        document = _park_document(policy, park_id=receipt.park_id, number=number, role=role,
                                  decision=decision, decided_at=now.isoformat(),
                                  return_at=returns.isoformat())
        try:
            _create(dir_fd, PARK_FAILOVER_FILE.format(n=number), document)
        except FileExistsError:
            raise RoutingRefused("another resolution of this run recorded a park failover "
                                 "first") from None
        except DurableWriteNotSynced:
            raise RoutingRefused("the park failover record is in place but could not be made "
                                 "durable: nothing is lifted; the next call reuses it") from None
        except OSError:
            raise RoutingRefused("the park failover cannot be recorded") from None
        return ParkFailover(role=role, number=number, route=decision.route,
                            return_at=returns.isoformat(), reused=False)
    finally:
        os.close(dir_fd)


def latest_park_failover(run_dir: Path | str) -> dict | None:
    """`{"number", "park_id", "return_at"}` of the newest park failover record of a run whose own
    digest holds, else None -- also for a run with no record, a damaged one, or a run directory
    that cannot be opened. Reads only; never raises. The policy is not consulted: this is what a
    caller that does not hold the policy (the project pass) may ask, not a decision."""
    try:
        _, dir_fd = _open_run(run_dir)
    except RoutingRefused:
        return None
    try:
        best = None
        for number in range(1, MAX_PARK_FAILOVERS + 1):
            doc = _read_failover(dir_fd, PARK_FAILOVER_FILE.format(n=number))
            if doc is None:
                continue
            if (doc.get("schema") != PARK_FAILOVER_SCHEMA or doc.get("number") != number
                    or doc.get("digest") != _body_digest(
                        {k: v for k, v in doc.items() if k != "digest"})
                    or not isinstance(doc.get("park_id"), str)
                    or _PARK_ID.fullmatch(doc["park_id"]) is None):
                return None
            best = {"number": number, "park_id": doc["park_id"], "return_at": doc["return_at"]}
        return best
    except (RoutingRefused, TypeError, ValueError, AttributeError, RecursionError):
        return None
    finally:
        os.close(dir_fd)
