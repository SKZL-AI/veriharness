"""The EvidenceClaim schema and its linkage into the digital thread (V3.3 P2-32).

One case per rule of the spec, and every refusal sits next to an accepted control
that changes only the condition under test: a validator that refused everything
would pass every refusal in this file, and the controls are what keep it honest.

The field oracles are **literal** copies of the plan's lists, written here, never
`PLAN_FIELDS` or `PLAN_REQUIRED` alone -- a test that compares a constant with
itself checks nothing (ecspec-b 4).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hoh.evidence import EvidenceItem, EvidenceStatus
from hoh.evidenceclaim import (
    EXECUTION_KINDS,
    PLAN_FIELDS,
    PLAN_REQUIRED,
    REF_RE,
    ClaimType,
    EvidenceClaim,
    Freshness,
    claim_from_item,
    invalidated,
    link_into,
    thread_node,
)
from hoh.thread import (
    AmbiguousIdentifier,
    DigitalThread,
    ThreadEdge,
    ThreadNode,
    ThreadStage,
    UnknownIdentifier,
    trace,
    trace_by,
)

ROOT = Path(__file__).resolve().parent.parent

# The plan's own lists, copied by hand from
# specs/full_stack_contracts_r1/04_EVIDENCE_CLAIM_AND_INVALIDATION.md (sha256 fc580ba6...cfd0)
# and schemas/evidence-claim.schema.json (sha256 1e566077...e4b9).
THE_PLANS_FIELDS = [
    "claim_id", "claim_type", "subject_type", "subject_id", "candidate_sha",
    "base_sha", "requirement_ref", "criterion_ref", "evidence_refs", "read_set",
    "dependency_set", "environment_digest", "check_version", "spec_digest",
    "policy_digest", "status", "freshness", "created_at", "invalidated_at",
    "invalidation_reason",
]
THE_PLANS_REQUIRED = [
    "claim_id", "claim_type", "subject_type", "subject_id", "evidence_refs",
    "spec_digest", "policy_digest", "status", "freshness",
]
THE_PLANS_CLASSES = {
    "IMPLEMENTATION_EXISTS", "CRITERION_PASSES", "NEGATIVE_CONTROL_DISCRIMINATES",
    "PRESERVATION_PASSES", "INTEGRATION_PASSES",
    "RELEASE_ARTIFACT_MATCHES_ACCEPTED_TREE", "AUTONOMY_EVENT_ATTRIBUTION",
}

AT = "2026-10-05T01:00:00Z"
ITEM_KW = dict(subject_type="work_package", subject_id="WP-1", candidate_sha="NEWSHA3",
               spec_digest="s" * 16, policy_digest="p" * 16,
               created_at="2026-10-05T00:00:00Z")


def _data(**over) -> dict:
    base = {
        "claim_id": "EC-1",
        "claim_type": "CRITERION_PASSES",
        "subject_type": "work_package",
        "subject_id": "WP-1",
        "candidate_sha": "NEWSHA3",
        "evidence_refs": ["receipt:R-1"],
        "spec_digest": "s" * 16,
        "policy_digest": "p" * 16,
        "status": "VERIFIED",
        "freshness": "FRESH",
        "created_at": "2026-10-05T00:00:00Z",
    }
    base.update(over)
    return base


def _claim(**over) -> EvidenceClaim:
    return EvidenceClaim.model_validate(_data(**over))


def _item(*, last: int, first: int = 1, status=EvidenceStatus.VERIFIED,
          receipts=("R-1",), check: str | None = "K1") -> EvidenceItem:
    return EvidenceItem(item_id="E-K1", status=status, claim="the criterion passes",
                        check_id=check, receipt_ids=list(receipts),
                        first_seen_iteration=first, last_seen_iteration=last)


_CHAIN = (ThreadStage.REQUIREMENT, ThreadStage.PROJECT_CONTRACT,
          ThreadStage.WORK_PACKAGE, ThreadStage.CHECKLIST_REQUIREMENT,
          ThreadStage.TASK_SPEC_AMENDMENT, ThreadStage.POLICY_ROUTING_DECISION,
          ThreadStage.RUN, ThreadStage.CANDIDATE)


def _chain_thread(sha: str = "NEWSHA3") -> DigitalThread:
    """A walkable chain from REQUIREMENT to one CANDIDATE whose record is `sha`."""
    nodes = [ThreadNode(node_id=f"n-{s.value.lower()}", stage=s,
                        record_ref=f"rec/{s.value.lower()}", digest=f"d-{s.value}")
             for s in _CHAIN[:-1]]
    nodes.append(ThreadNode(node_id="cand-0", stage=ThreadStage.CANDIDATE,
                            record_ref=sha, digest="d-cand-0"))
    edges = [ThreadEdge(from_id=a.node_id, to_id=b.node_id, relation="next",
                        digest=f"e-{a.node_id}-{b.node_id}")
             for a, b in zip(nodes, nodes[1:])]
    return DigitalThread(thread_id="T", generation=0, nodes=tuple(nodes),
                         edges=tuple(edges), projected_from=("runs",))


def _tool():
    spec = importlib.util.spec_from_file_location(
        "evidence_claims_tool", ROOT / "tools" / "evidence_claims.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _matrix():
    spec = importlib.util.spec_from_file_location(
        "capability_matrix_for_evidenceclaim", ROOT / "tools" / "capability_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# the plan's fields and classes
# --------------------------------------------------------------------------- #


def test_the_fields_are_the_plans_fields():
    assert list(EvidenceClaim.model_fields) == THE_PLANS_FIELDS
    assert list(PLAN_FIELDS) == THE_PLANS_FIELDS
    assert len(set(THE_PLANS_FIELDS)) == 20


def test_the_plans_required_fields_are_required():
    assert list(PLAN_REQUIRED) == THE_PLANS_REQUIRED
    required = {n for n, f in EvidenceClaim.model_fields.items() if f.is_required()}
    assert required == set(THE_PLANS_REQUIRED) | {"created_at"}
    # Each required field, dropped, is refused ...
    for name in sorted(required):
        data = _data()
        data.pop(name)
        with pytest.raises(ValidationError):
            EvidenceClaim.model_validate(data)
    # ... and the control: the full claim stands, and so does one missing an
    # optional field.
    assert _claim().claim_id == "EC-1"
    data = _data()
    data.pop("candidate_sha")
    assert EvidenceClaim.model_validate(data).candidate_sha is None


def test_the_claim_classes_are_the_plans_seven_and_no_other():
    assert {c.value for c in ClaimType} == THE_PLANS_CLASSES
    assert len(list(ClaimType)) == 7
    for name in sorted(THE_PLANS_CLASSES):
        assert _claim(claim_type=name).claim_type is ClaimType(name)
    for name in ("SOMETHING_ELSE", "criterion_passes", "TEST_EXISTS", ""):
        with pytest.raises(ValidationError):
            _claim(claim_type=name)


def test_an_unknown_field_is_refused():
    with pytest.raises(ValidationError, match="extra"):
        _claim(verified_by="me")
    with pytest.raises(ValidationError):
        _claim(holds=True)
    assert _claim().holds()


# --------------------------------------------------------------------------- #
# the evidence rules
# --------------------------------------------------------------------------- #


def test_a_verified_claim_without_evidence_is_refused():
    with pytest.raises(ValidationError, match="names no evidence"):
        _claim(evidence_refs=[])
    # Controls: an open claim may name nothing, and a VERIFIED one with a receipt
    # stands.
    assert _claim(status="GAP", evidence_refs=[]).evidence_refs == ()
    assert _claim(status="INSUFFICIENT", evidence_refs=[]).status is EvidenceStatus.INSUFFICIENT
    assert _claim(evidence_refs=["receipt:R-1"]).holds()


OUTSIDE_THE_GRAMMAR = [
    "src/hoh/a.py",
    "FILE:a.py",
    "Receipt:R-1",
    "path:a.py",
    "cite:paper-2026-section-3",
    "receipt:",
    "receipt: R-1",
    "receipt:R 1",
    "   ",
    ":R-1",
    "receipt:R\u200b1",  # an invisible zero-width space (Revision 2a, ecspec-b r2 N3)
]


@pytest.mark.parametrize("status", ["VERIFIED", "GAP"])
@pytest.mark.parametrize("ref", OUTSIDE_THE_GRAMMAR)
def test_an_evidence_reference_outside_the_grammar_is_refused(ref, status):
    # Beside a well-formed receipt, so only the grammar can be what refuses it.
    with pytest.raises(ValidationError, match="not of the form"):
        _claim(status=status, evidence_refs=["receipt:R-ok", ref])
    assert REF_RE.fullmatch(ref.strip()) is None
    # Control: the same claim without the bad reference stands.
    assert _claim(status=status, evidence_refs=["receipt:R-ok"]).evidence_refs == (
        "receipt:R-ok",)


@pytest.mark.parametrize("ref,kind", [
    ("receipt:R-1", "receipt"),
    ("run:20261005t0148z-ecl/i1", "run"),
    ("file:src/hoh/a.py", "file"),
    ("test:tests/test_a.py::test_b", "test"),
])
def test_every_kind_of_the_grammar_is_accepted(ref, kind):
    assert REF_RE.fullmatch(ref).group("kind") == kind
    assert _claim(status="GAP", evidence_refs=[ref]).evidence_refs == (ref,)
    # Surrounding whitespace is stripped, as everywhere in the model.
    assert _claim(status="GAP", evidence_refs=[f"  {ref} "]).evidence_refs == (ref,)
    assert (kind in EXECUTION_KINDS) == (kind in {"receipt", "run"})


@pytest.mark.parametrize("claim_type", sorted(THE_PLANS_CLASSES))
def test_file_existence_alone_never_verifies(claim_type):
    existence = ["file:src/hoh/a.py", "test:tests/test_a.py::test_b"]
    with pytest.raises(ValidationError, match="file existence alone"):
        _claim(claim_type=claim_type, evidence_refs=existence)
    with pytest.raises(ValidationError, match="file existence alone"):
        _claim(claim_type=claim_type, evidence_refs=["test:tests/test_a.py::test_b"])
    # Controls: an execution beside them verifies, a receipt or a run alike ...
    assert _claim(claim_type=claim_type,
                  evidence_refs=[*existence, "receipt:R-1"]).holds()
    assert _claim(claim_type=claim_type,
                  evidence_refs=[*existence, "run:r-1"]).holds()
    # ... and an open claim may rest on existence alone, because it claims nothing.
    assert _claim(claim_type=claim_type, status="GAP",
                  evidence_refs=existence).status is EvidenceStatus.GAP


def test_invalidation_is_all_or_nothing():
    with pytest.raises(ValidationError, match="set together"):
        _claim(invalidated_at=AT)
    with pytest.raises(ValidationError, match="set together"):
        _claim(invalidation_reason="tree changed")
    with pytest.raises(ValidationError, match="set together"):
        _claim(freshness="INVALIDATED", invalidated_at=AT)
    with pytest.raises(ValidationError, match="INVALIDATED exactly"):
        _claim(freshness="INVALIDATED")
    with pytest.raises(ValidationError, match="INVALIDATED exactly"):
        _claim(invalidated_at=AT, invalidation_reason="tree changed")
    with pytest.raises(ValidationError, match="INVALIDATED exactly"):
        _claim(freshness="STALE", invalidated_at=AT, invalidation_reason="tree changed")
    # Controls: both with INVALIDATED, and neither with FRESH or STALE.
    c = _claim(freshness="INVALIDATED", invalidated_at=AT, invalidation_reason="tree changed")
    assert c.freshness is Freshness.INVALIDATED
    assert _claim().invalidated_at is None
    assert _claim(freshness="STALE").freshness is Freshness.STALE


def test_holds_needs_verified_and_fresh():
    assert _claim().holds()
    assert not _claim(freshness="STALE").holds()
    assert not _claim(freshness="INVALIDATED", invalidated_at=AT,
                      invalidation_reason="tree changed").holds()
    for status in ("GAP", "REGRESSION", "INSUFFICIENT"):
        assert not _claim(status=status).holds()
    assert "holds" not in EvidenceClaim.model_fields


def test_a_claim_is_frozen():
    c = _claim()
    with pytest.raises(ValidationError):
        c.status = EvidenceStatus.GAP
    with pytest.raises(ValidationError):
        c.freshness = Freshness.FRESH
    assert isinstance(c.evidence_refs, tuple)
    assert c.status is EvidenceStatus.VERIFIED


def test_invalidated_is_a_new_claim_and_never_overwrites_a_reason():
    c = _claim()
    before = c.model_dump()
    i = invalidated(c, at=AT, reason="tree changed")
    assert i is not c
    assert i.freshness is Freshness.INVALIDATED
    assert (i.invalidated_at, i.invalidation_reason) == (AT, "tree changed")
    assert not i.holds()
    assert c.model_dump() == before and c.holds()
    # A second invalidation is refused, and the first reason stands.
    with pytest.raises(ValueError, match="already invalidated"):
        invalidated(i, at="2026-10-05T02:00:00Z", reason="something else")
    assert i.invalidation_reason == "tree changed"
    # Built through validation, so a missing or empty time or reason is refused.
    for at, reason in ((None, "r"), ("", "r"), ("  ", "r"),
                       (AT, None), (AT, ""), (AT, "  ")):
        with pytest.raises(ValueError):
            invalidated(c, at=at, reason=reason)


@pytest.mark.parametrize("name", ["candidate_sha", "base_sha", "requirement_ref",
                                  "criterion_ref", "environment_digest",
                                  "check_version"])
def test_an_optional_field_is_absent_or_non_empty(name):
    with pytest.raises(ValidationError):
        _claim(**{name: ""})
    with pytest.raises(ValidationError):
        _claim(**{name: "   "})
    # Controls: absent, explicitly None, and a value all stand.
    data = _data()
    data.pop(name, None)
    assert getattr(EvidenceClaim.model_validate(data), name) is None
    assert getattr(_claim(**{name: None}), name) is None
    assert getattr(_claim(**{name: " x1 "}), name) == "x1"
    # Required strings are non-empty too, and stripped.
    with pytest.raises(ValidationError):
        _claim(claim_id=" ")
    assert _claim(claim_id=" EC-1 ").claim_id == "EC-1"


def test_the_digest_is_stable_and_covers_every_field():
    c = _claim()
    assert c.digest() == _claim().digest()
    assert c.digest() == EvidenceClaim.model_validate(c.model_dump(mode="json")).digest()
    assert len(c.digest()) == 64 and int(c.digest(), 16) >= 0
    other = {
        "claim_id": "EC-2",
        "claim_type": "IMPLEMENTATION_EXISTS",
        "subject_type": "requirement",
        "subject_id": "WP-2",
        "candidate_sha": "OTHERSHA",
        "base_sha": "BASESHA",
        "requirement_ref": "P2-32",
        "criterion_ref": "K2",
        "evidence_refs": ["run:r-2"],
        "read_set": ["src/hoh/a.py"],
        "dependency_set": ["pydantic==2"],
        "environment_digest": "env-2",
        "check_version": "v2",
        "spec_digest": "t" * 16,
        "policy_digest": "q" * 16,
        "status": "GAP",
        "freshness": "STALE",
        "created_at": "2026-10-06T00:00:00Z",
    }
    for name, value in other.items():
        assert _claim(**{name: value}).digest() != c.digest(), name
    i = invalidated(c, at=AT, reason="tree changed")
    assert invalidated(c, at="2026-10-05T02:00:00Z", reason="tree changed").digest() != i.digest()
    assert invalidated(c, at=AT, reason="spec changed").digest() != i.digest()
    assert set(other) | {"invalidated_at", "invalidation_reason"} == set(THE_PLANS_FIELDS)


# --------------------------------------------------------------------------- #
# claim_from_item
# --------------------------------------------------------------------------- #


def test_claim_from_item_maps_a_passing_criterion():
    item = _item(first=3, last=3, receipts=("R-1", "R-2"))
    before = item.model_dump()
    c = claim_from_item(item, iteration=3, requirement_ref="P2-32", **ITEM_KW)
    assert c.claim_id == "E-K1@NEWSHA3"
    assert c.claim_type is ClaimType.CRITERION_PASSES
    assert c.criterion_ref == "K1"
    assert c.evidence_refs == ("receipt:R-1", "receipt:R-2")
    assert c.status is EvidenceStatus.VERIFIED and c.freshness is Freshness.FRESH
    assert c.holds()
    assert (c.subject_type, c.subject_id, c.candidate_sha) == ("work_package", "WP-1", "NEWSHA3")
    assert (c.spec_digest, c.policy_digest) == ("s" * 16, "p" * 16)
    assert c.created_at == "2026-10-05T00:00:00Z" and c.requirement_ref == "P2-32"
    assert claim_from_item(item, iteration=3, **ITEM_KW).requirement_ref is None
    assert item.model_dump() == before
    # A failing criterion projects as what it is.
    gap = claim_from_item(_item(last=3, status=EvidenceStatus.GAP, receipts=()),
                          iteration=3, **ITEM_KW)
    assert gap.status is EvidenceStatus.GAP and not gap.holds()


def test_claim_from_item_without_receipts_is_insufficient_not_verified():
    c = claim_from_item(_item(last=3, receipts=()), iteration=3, **ITEM_KW)
    assert c.status is EvidenceStatus.INSUFFICIENT
    assert not c.holds() and c.evidence_refs == ()
    # Control: the same item with a receipt stays VERIFIED.
    assert claim_from_item(_item(last=3), iteration=3, **ITEM_KW).status is EvidenceStatus.VERIFIED


def test_claim_from_item_refuses_an_item_this_iteration_did_not_observe():
    # ecspec-a F1, the reviewer's own case: carried forward from iteration 1 with
    # its old receipt, bound to iteration 3's candidate.
    stale = _item(first=1, last=1, receipts=("rcpt-iter1",))
    with pytest.raises(ValueError, match="last observed in iteration 1, not in iteration 3"):
        claim_from_item(stale, iteration=3, **ITEM_KW)
    # Only the iteration that observed it may bind it -- not a later one, and not
    # an earlier one either.
    with pytest.raises(ValueError, match="not in iteration 0"):
        claim_from_item(stale, iteration=0, **ITEM_KW)
    # Controls: iteration 1 holds; an item first seen in 1 and re-observed in 3
    # binds to iteration 3 (Revision 2a: `last_seen_iteration` decides).
    assert claim_from_item(stale, iteration=1, **ITEM_KW).holds()
    seen_again = _item(first=1, last=3, receipts=("rcpt-iter3",))
    c = claim_from_item(seen_again, iteration=3, **ITEM_KW)
    assert c.holds() and c.evidence_refs == ("receipt:rcpt-iter3",)


def test_claim_from_item_refuses_a_gap_about_no_criterion():
    with pytest.raises(ValueError, match="names no check"):
        claim_from_item(_item(last=3, status=EvidenceStatus.GAP, receipts=(), check=None),
                        iteration=3, **ITEM_KW)
    with pytest.raises(ValueError, match="names no check"):
        claim_from_item(_item(last=3, check=None), iteration=3, **ITEM_KW)
    # Control: the same gap with a check projects.
    c = claim_from_item(_item(last=3, status=EvidenceStatus.GAP, receipts=()),
                        iteration=3, **ITEM_KW)
    assert c.criterion_ref == "K1" and c.status is EvidenceStatus.GAP


# --------------------------------------------------------------------------- #
# the thread
# --------------------------------------------------------------------------- #


def test_link_into_joins_a_claim_to_its_candidate_and_traces_complete():
    t = _chain_thread()
    before = t.model_dump()
    c = _claim()
    t2 = link_into(t, [c], record_set="claims.json")
    assert t.model_dump() == before
    assert t2.generation == t.generation + 1
    assert t2.projected_from == ("runs", "claims.json")

    node = t2.node("evidence-claim:EC-1")
    assert node == thread_node(c, record_set="claims.json")
    assert node.stage is ThreadStage.CHECKS_RECEIPTS
    assert node.record_ref == "claims.json#EC-1"
    assert node.label == "CRITERION_PASSES"
    assert node.digest == c.digest()

    new = [e for e in t2.edges if e not in t.edges]
    assert len(new) == 1
    edge = new[0]
    assert (edge.from_id, edge.to_id, edge.relation) == ("cand-0", node.node_id, "evidenced_by")
    assert edge.digest == hashlib.sha256(f"d-cand-0->{node.digest}".encode()).hexdigest()

    tr = trace(t2, node_id=node.node_id)
    assert tr.complete, (tr.missing, tr.unwalkable, tr.fsck)
    assert trace_by(t2, stage=ThreadStage.CHECKS_RECEIPTS,
                    record_ref="claims.json#EC-1").complete

    # A different claim gets a different edge digest, and linking into the same
    # record set again does not repeat it in projected_from (Revision 2a).
    t3 = link_into(t2, [_claim(claim_id="EC-2")], record_set="claims.json")
    assert t3.projected_from == ("runs", "claims.json")
    assert t3.generation == 2
    newer = [e for e in t3.edges if e not in t2.edges]
    assert len(newer) == 1 and newer[0].digest != edge.digest


def test_an_unlinked_claim_never_traces_complete():
    t = _chain_thread()
    data = _data(claim_id="EC-free")
    data.pop("candidate_sha")
    free = EvidenceClaim.model_validate(data)
    t2 = link_into(t, [free], record_set="claims.json")
    assert t2.edges == t.edges
    tr = trace(t2, node_id="evidence-claim:EC-free")
    assert not tr.complete
    assert ThreadStage.REQUIREMENT in tr.missing
    # Control: the same claim bound to the candidate does trace complete.
    bound = link_into(t, [_claim(claim_id="EC-free")], record_set="claims.json")
    assert trace(bound, node_id="evidence-claim:EC-free").complete


def test_link_into_an_unknown_candidate_raises():
    t = _chain_thread()
    with pytest.raises(UnknownIdentifier, match="NOPE"):
        link_into(t, [_claim(candidate_sha="NOPE")], record_set="claims.json")
    # A RUN node carrying the sha is not a candidate.
    with pytest.raises(UnknownIdentifier):
        link_into(t, [_claim(candidate_sha="rec/run")], record_set="claims.json")
    # Control: the known candidate links.
    assert len(link_into(t, [_claim()], record_set="claims.json").edges) == len(t.edges) + 1


def test_link_into_an_ambiguous_candidate_raises():
    def thread(second_sha: str) -> DigitalThread:
        return DigitalThread(
            thread_id="T", generation=0, projected_from=("runs",),
            nodes=(ThreadNode(node_id="cand-0", stage=ThreadStage.CANDIDATE,
                              record_ref="NEWSHA3", digest="dc0"),
                   ThreadNode(node_id="cand-1", stage=ThreadStage.CANDIDATE,
                              record_ref=second_sha, digest="dc1")))
    with pytest.raises(AmbiguousIdentifier, match="cand-0.*cand-1"):
        link_into(thread("NEWSHA3"), [_claim()], record_set="claims.json")
    # Control: distinct shas resolve, each to its own candidate.
    t = link_into(thread("OTHERSHA"), [_claim(candidate_sha="OTHERSHA")],
                  record_set="claims.json")
    assert [e.from_id for e in t.edges] == ["cand-1"]


# --------------------------------------------------------------------------- #
# the entry point
# --------------------------------------------------------------------------- #


def test_the_tool_check_accepts_valid_and_refuses_invalid(tmp_path, capsys):
    tool = _tool()
    good = _claim().model_dump(mode="json")

    def run(payload=None, raw=None) -> tuple[int, str]:
        f = tmp_path / "claims.json"
        f.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
        code = tool.main(["check", str(f)])
        return code, capsys.readouterr().out

    code, out = run([good])
    assert code == 0 and "ok [0] EC-1" in out
    code, out = run({"claims": [good, {**good, "claim_id": "EC-2"}]})
    assert code == 0 and "ok [1] EC-2" in out
    assert run([])[0] == 1
    assert run({"claims": []})[0] == 1
    code, out = run([good, good])
    assert code == 1 and "appears twice" in out
    # Whitespace-equal ids are the same id (Revision 2a).
    code, out = run([good, {**good, "claim_id": " EC-1"}])
    assert code == 1 and "REFUSED [1]" in out and "appears twice" in out
    code, out = run([good, {**good, "claim_id": "EC-2", "evidence_refs": ["file:a.py"]}])
    assert code == 1 and "ok [0]" in out and "REFUSED [1]" in out
    assert "file existence alone" in out
    assert run(raw="{broken")[0] == 2
    assert tool.main(["check", str(tmp_path / "absent.json")]) == 2


def test_the_tool_schema_names_every_plan_field(capsys):
    assert _tool().main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert list(schema["properties"]) == THE_PLANS_FIELDS
    assert set(schema["required"]) == set(THE_PLANS_REQUIRED) | {"created_at"}
    assert schema["additionalProperties"] is False


def test_the_tool_from_bundle_projects_criterion_items(tmp_path, capsys):
    tool = _tool()
    args = ["--subject-type", "work_package", "--subject-id", "WP-1",
            "--candidate-sha", "NEWSHA3", "--spec-digest", "s" * 16,
            "--policy-digest", "p" * 16, "--created-at", "2026-10-05T00:00:00Z"]

    def run(payload=None, raw=None) -> tuple[int, str, str]:
        f = tmp_path / "evidence.json"
        f.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
        code = tool.main(["from-bundle", str(f), *args])
        got = capsys.readouterr()
        return code, got.out, got.err

    def bundle(*items) -> dict:
        return {"run_id": "r", "iteration": 3,
                "items": [i.model_dump(mode="json") for i in items]}

    gap = _item(last=3, status=EvidenceStatus.GAP, receipts=(), check=None)
    code, out, err = run(bundle(_item(last=3), gap))
    assert code == 0
    claims = json.loads(out)
    assert [c["claim_id"] for c in claims] == ["E-K1@NEWSHA3"]
    assert EvidenceClaim.model_validate(claims[0]).holds()
    assert "not projected" in err and "names no check" in err
    # Nothing projected is exit 1, and still a JSON list.
    code, out, err = run(bundle(gap))
    assert code == 1 and json.loads(out) == []
    code, out, err = run(bundle(_item(first=1, last=1, receipts=("rcpt-iter1",))))
    assert code == 1 and json.loads(out) == []
    assert "not projected" in err and "not in iteration 3" in err
    code, out, err = run({"x": 1})
    assert code == 1 and json.loads(out) == []
    assert run(raw="{broken")[0] == 2


def test_the_entry_point_is_what_the_matrix_measures():
    cm = _matrix()
    req = {"id": "PX-32", "name": "EvidenceClaim schema", "target_phase": "PX",
           "authoritative_item": "PX.not-a-plan-item",
           "probe": {"kind": "symbol", "symbol": "EvidenceClaim",
                     "module": "evidenceclaim.py",
                     "test": "tests/test_evidenceclaim.py"}}
    collection = ("tests/test_evidenceclaim.py::test_the_fields_are_the_plans_fields\n", True)
    symbols = cm._symbols()
    graph = cm._import_graph()
    assert "tools/evidence_claims.py" in graph["evidenceclaim"]
    out = cm.classify(req, symbols, collection, {}, graph)
    assert out["status"] == cm.PROVEN, out["evidence"]
    assert any("tools/evidence_claims.py" in e for e in out["evidence"])
    # Control: with no importer the same row is capped -- the PROVEN above rests
    # on the entry point's import statement and on nothing else.
    starved = {**graph, "evidenceclaim": set()}
    out = cm.classify(req, symbols, collection, {}, starved)
    assert out["status"] == cm.PARTIAL
    assert any("CAPPED" in e for e in out["evidence"])
