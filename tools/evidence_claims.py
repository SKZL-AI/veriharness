#!/usr/bin/env python3
"""The EvidenceClaim entry point (V3.3 P2-32): reads what it is handed, prints.

Usage:
    python3 tools/evidence_claims.py schema
    python3 tools/evidence_claims.py check FILE
    python3 tools/evidence_claims.py from-bundle EVIDENCE_JSON \\
        --subject-type T --subject-id ID --candidate-sha SHA \\
        --spec-digest D --policy-digest D --created-at TS [--requirement-ref R]

`schema` prints the claim model's JSON schema (exit 0).

`check` takes a JSON list of claims, or `{"claims": [...]}`, and prints one line
per claim: `ok [i] ...` or `REFUSED [i] ...`. A `claim_id` that appears twice --
after whitespace is stripped, as the model strips it -- is refused. Exit 0 when
there is at least one claim and none is refused; 1 when one is refused or the set
is empty; 2 when the file cannot be read as JSON.

`from-bundle` validates an `EvidenceBundle` and projects each of its items into a
claim of the bundle's own iteration, so an item that iteration did not re-observe
is refused. It prints the projected claims as a JSON list and `not projected: ...`
on stderr for each refused item. Exit 0 when at least one claim was projected; 1
when the input is not a bundle or nothing was projected (it still prints `[]`); 2
when the file cannot be read as JSON.

The tool writes no file and judges no state of this repository. It imports
`hoh.evidenceclaim` with a static `from ... import` statement on purpose: the
capability matrix measures consumption from import statements, and a dynamic
import would be invisible to it (O225/C4).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOH / "src"))

from pydantic import ValidationError  # noqa: E402

from hoh.evidence import EvidenceBundle  # noqa: E402
from hoh.evidenceclaim import EvidenceClaim, claim_from_item  # noqa: E402


def _read_json(path: str):
    """`(data, True)`, or `(None, False)` after saying on stderr why the file
    is unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), True
    except (OSError, ValueError) as exc:
        print(f"unreadable JSON in {path}: {exc}", file=sys.stderr)
        return None, False


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'claim'}: {e['msg']}"
            for e in exc.errors())
    return str(exc)


def schema() -> int:
    print(json.dumps(EvidenceClaim.model_json_schema(), indent=2))
    return 0


def check_file(path: str) -> int:
    data, ok = _read_json(path)
    if not ok:
        return 2
    if isinstance(data, dict) and isinstance(data.get("claims"), list):
        raw = data["claims"]
    elif isinstance(data, list):
        raw = data
    else:
        print("REFUSED: expected a list of claims or {\"claims\": [...]}")
        return 1
    if not raw:
        print("REFUSED: the set holds no claim, and an empty set evidences nothing")
        return 1
    refused = 0
    first_at: dict[str, int] = {}
    for i, entry in enumerate(raw):
        try:
            claim = EvidenceClaim.model_validate(entry)
        except ValueError as exc:
            refused += 1
            print(f"REFUSED [{i}] {_reason(exc)}")
            continue
        if claim.claim_id in first_at:
            refused += 1
            print(f"REFUSED [{i}] claim_id {claim.claim_id!r} appears twice "
                  f"(first at [{first_at[claim.claim_id]}])")
            continue
        first_at[claim.claim_id] = i
        print(f"ok [{i}] {claim.claim_id} {claim.claim_type} {claim.status} "
              f"{claim.freshness}")
    return 1 if refused else 0


def from_bundle(path: str, *, subject_type: str, subject_id: str,
                candidate_sha: str, spec_digest: str, policy_digest: str,
                created_at: str, requirement_ref: str | None) -> int:
    data, ok = _read_json(path)
    if not ok:
        return 2
    try:
        bundle = EvidenceBundle.model_validate(data)
    except ValidationError as exc:
        print(f"not projected: {path} is not an evidence bundle: {_reason(exc)}",
              file=sys.stderr)
        print("[]")
        return 1
    claims = []
    for item in bundle.items:
        try:
            claims.append(claim_from_item(
                item, iteration=bundle.iteration, subject_type=subject_type,
                subject_id=subject_id, candidate_sha=candidate_sha,
                spec_digest=spec_digest, policy_digest=policy_digest,
                created_at=created_at, requirement_ref=requirement_ref))
        except ValueError as exc:
            print(f"not projected: {item.item_id}: {_reason(exc)}", file=sys.stderr)
    print(json.dumps([c.model_dump(mode="json") for c in claims], indent=2))
    return 0 if claims else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="evidence_claims.py",
        description="Validate EvidenceClaims and project evidence bundles into them.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the EvidenceClaim JSON schema")
    p_check = sub.add_parser("check", help="validate a file of claims")
    p_check.add_argument("file")
    p_fb = sub.add_parser("from-bundle", help="project an EvidenceBundle into claims")
    p_fb.add_argument("evidence_json")
    for opt in ("subject-type", "subject-id", "candidate-sha", "spec-digest",
                "policy-digest", "created-at"):
        p_fb.add_argument(f"--{opt}", required=True)
    p_fb.add_argument("--requirement-ref", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "schema":
        return schema()
    if args.cmd == "check":
        return check_file(args.file)
    return from_bundle(
        args.evidence_json, subject_type=args.subject_type,
        subject_id=args.subject_id, candidate_sha=args.candidate_sha,
        spec_digest=args.spec_digest, policy_digest=args.policy_digest,
        created_at=args.created_at, requirement_ref=args.requirement_ref)


if __name__ == "__main__":
    sys.exit(main())
