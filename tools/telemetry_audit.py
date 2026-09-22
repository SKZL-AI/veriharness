#!/usr/bin/env python3
"""Does the dispatch log say what it claims to say, on a real run?

`telemetry.py` is careful in the right places -- a missing token count stays
`None`, an aggregate says how many inputs were unknown, a proxy is labelled as
a proxy -- and `tests/test_taxonomy_telemetry.py` pins all of that at the unit
level. None of it proves the fields are *filled* when three agents actually
run. A record model that would refuse to lie, wired to a controller that never
passes it anything, produces a log of well-typed blanks.

So this reads a real `telemetry.jsonl` and sorts every field of every record
into one of three states, which are deliberately not two:

* **MEASURED** -- a value is there and came from somewhere.
* **NOT_AVAILABLE** -- explicitly unknown, because the harness does not report
  it. `None` for a token count is this, and it is a legitimate answer.
* **MISSING** -- the field is at its type's empty default (`""`, `0`) in a
  situation where something was knowable. This is the interesting one: `0`
  receipts on a dispatch that produced two is not a measurement of zero, it is
  the absence of a measurement wearing a number, and that is the exact shape of
  this project's fourth false green.

An empty string for `model` is MISSING rather than NOT_AVAILABLE for the reason
`DispatchRecord.model` gives about itself: "whatever the harness defaults to"
is a genuine and unhelpful state. It is knowable and nobody asked.

Usage:
    python3 tools/telemetry_audit.py --run-root PATH --run-id ID [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

MEASURED, NOT_AVAILABLE, MISSING = "MEASURED", "NOT_AVAILABLE", "MISSING"

#: The literal a record writes when a backend cannot report an identity. It is
#: the same string as the state, and deliberately so: the field says what the
#: audit would otherwise have to guess.

#: Fields whose `None` means "the harness does not report this", which is an
#: answer. Everything else at an empty default is a gap in the wiring.
#: `failure_class` is deliberately *not* here: on a dispatch that failed, the
#: class is derivable from the failure, so a `None` there is nobody having
#: asked `taxonomy.classify`, not the harness staying silent.
MAY_BE_UNKNOWN = {
    "tokens_in", "tokens_out", "quota_proxy", "wallclock_seconds",
    # `None` on these means the record predates the field, which the record's
    # own docstring says is not the same as zero. The audit has to agree with
    # it, or the two contradict each other on every older line.
    "witnessed_trees", "witnessed_listings",
}

#: Fields where 0 is a measurement rather than an empty default. A dispatch
#: that needed no retry really did need none, and the planner really does run
#: at attempt 0. Treating those as gaps would make the audit cry wolf, which
#: is how an audit stops being read.
#: `discriminating` and `artefactual` belong here for a subtler reason: zero
#: criteria that discriminated is a real and common answer, so insisting on a
#: non-zero would flag honest runs. `receipts` is the field that proves the
#: count reached the record at all -- a verification that ran wrote receipts,
#: so a QA dispatch reporting none is unambiguous where these two are not.
NULL_IS_A_VALUE = {"attempt", "iteration", "retries", "waited_seconds",
                     "repair_depth", "discriminating", "artefactual",
                     # QA watches no directory listing: its working directory
                     # is the one it would be watching, so zero is the policy's
                     # answer rather than an unfilled field (limit 12d).
                     "witnessed_listings"}

#: Fields where the **empty string** is a real state rather than an unfilled
#: default. A bare `hoh run` has no project above it, and saying so with an
#: empty id is correct; `node_id` and `repair_of` are then judged against it
#: rather than against themselves.
EMPTY_IS_A_VALUE = {"project_id"}

#: Fields that only mean something for a dispatch that ended a particular way,
#: so their default is not evidence of anything.
#: Fields that are only meaningful in some runs, and whose emptiness is
#: therefore not evidence of a gap. Each condition is asked of a **different**
#: field than the one being judged: four of these once read
#: `lambda r: bool(r.get(<the same field>))`, which exempts a field exactly
#: when it is empty and can never fire. An audit whose checks cannot fail is
#: the tick-box this project rejects one level up.
CONDITIONAL = {
    "failure_class": lambda r: r.get("outcome") != "ok",
    "detail": lambda r: r.get("outcome") != "ok",
    # A dispatch the budget refused reaches no provider, so `0` is what it
    # cost and not an unfilled field. Judged on a *different* field than
    # itself, per this table's own rule: where the dispatch ended `ok`, zero
    # provider calls is still a gap, because a dispatch that answered must
    # have reached somebody.
    "provider_calls": lambda r: r.get("outcome") == "ok",
    # A repair chain is named by `repair_of`; its depth only means something
    # when there is one. The presence question is asked of the id, and the
    # id itself is judged by whether the run had a project above it.
    "repair_depth": lambda r: bool(r.get("repair_of")),
    "repair_of": lambda r: bool(r.get("project_id")),
    "node_id": lambda r: bool(r.get("project_id")),
    "quota_proxy_kind": lambda r: r.get("quota_proxy") is not None,
    # Checks run during verification, so only that dispatch can carry their
    # count. A planner record with 0 receipts is not a gap; a QA record with
    # 0 receipts on a run that wrote two is exactly the "absence wearing a
    # number" this file is looking for.
    "receipts": lambda r: r.get("role") == "qa",
}


def _state(field_: str, value_, sentence: dict) -> str:
    if field_ in CONDITIONAL and not CONDITIONAL[field_](sentence):
        return MEASURED          # not applicable here; nothing was withheld
    if value_ == NOT_AVAILABLE:
        # Stated rather than blank: the backend was asked and cannot say.
        return NOT_AVAILABLE
    if value_ is None:
        return NOT_AVAILABLE if field_ in MAY_BE_UNKNOWN else MISSING
    if field_ in NULL_IS_A_VALUE or field_ in EMPTY_IS_A_VALUE:
        return MEASURED
    if value_ == "" or (isinstance(value_, int) and not isinstance(value_, bool) and value_ == 0):
        return MISSING
    return MEASURED


def check(sentences: list[dict]) -> dict:
    fields_: dict[str, dict[str, int]] = {}
    for sentence in sentences:
        for field_, value_ in sentence.items():
            if field_ == "schema_version":
                continue
            z = _state(field_, value_, sentence)
            fields_.setdefault(field_, {MEASURED: 0, NOT_AVAILABLE: 0, MISSING: 0})
            fields_[field_][z] += 1
    return fields_


#: Coverage entries that are about a **shape of event** rather than a field.
#: A campaign in which nothing failed has not validated the failure record --
#: and it has not failed the audit either. The two are reported apart, because
#: rolling them together would either hide a real wiring gap or make an
#: uneventful campaign look defective. Not observed is never "passed".
EVENTS = {"failure_record", "retry_record", "failure_taxonomy"}


def coverage_(sentences: list[dict]) -> dict:
    """The shapes §8 asks to see at least once each in a real campaign."""
    roles_ = {s.get("role") for s in sentences}
    return {
        "planner_record": "planner" in roles_,
        "developer_record": "developer" in roles_,
        "qa_record": "qa" in roles_,
        "failure_record": any(s.get("outcome") != "ok" for s in sentences),
        "retry_record": any(s.get("retries", 0) > 0 for s in sentences),
        "provider_identity": any(s.get("provider") for s in sentences),
        "model_identity": all(s.get("model") for s in sentences),
        "effort_stated": all(s.get("effort") for s in sentences),
        # Asked only of records whose dispatch actually ran. A dispatch
        # refused at the budget never takes a witness, so `None` there is the
        # truthful record of "no witness was armed" -- and requiring one would
        # push the controller towards writing a coverage number for a witness
        # it never took, which is the fabricated measurement the record's own
        # docstring forbids. A record that predates `provider_calls` carries
        # `None`, not `0`, and is still asked.
        "witness_coverage_recorded": all(
            s.get("witnessed_trees") is not None
            for s in sentences if s.get("provider_calls") != 0),
        "wallclock": all(s.get("wallclock_seconds") is not None for s in sentences),
        "attempt": all("attempt" in s for s in sentences),
        "outcome": all(s.get("outcome") for s in sentences),
        "failure_taxonomy": any(
            s.get("failure_class") for s in sentences if s.get("outcome") != "ok"),
        "tokens_measured_or_explicitly_unavailable": all(
            ("tokens_in" in s and "tokens_out" in s) for s in sentences),
        "quota_proxy_labelled": all(
            bool(s.get("quota_proxy_kind")) or s.get("quota_proxy") is None
            for s in sentences),
    }


def aggregation_control(sentences: list[dict], run: Path | None = None) -> dict:
    """The negative control: can a missing measurement become a 0 in a sum?

    Tokens are aggregated twice -- once the way `Summary` does it, once the
    naive way -- and the two have to disagree whenever anything is unknown. If
    they agree, either every record is known (and the control did not run) or
    the careful path has stopped being careful.

    Tokens were the only field this checked, and the same defect sat unguarded
    on four others: `Summary` sums `receipts`, `retries`, `discriminating` and
    `artefactual` with no unknown accounting at all, so a run that wrote two
    receipts and recorded none printed "0 receipt(s)" in the line a reader
    quotes. Those have no `None` state to lose, so the check against them is
    external: the receipt **files** the run produced.
    """
    from hoh.telemetry import DispatchRecord, summarise

    objekte = [DispatchRecord.model_validate(s) for s in sentences]
    cautious = summarise(objekte)
    naive_ = sum((s.get("tokens_in") or 0) + (s.get("tokens_out") or 0)
               for s in sentences)
    unknown_ = sum(1 for s in sentences
                    if s.get("tokens_in") is None and s.get("tokens_out") is None)
    on_disk = None
    if run is not None and (run / "receipts").is_dir():
        on_disk = len(list((run / "receipts").glob("*.json")))
    return {
        "ran": bool(sentences),
        "records": len(sentences),
        "records_with_unknown_tokens": unknown_,
        "careful_total": cautious.tokens,
        "naive_total": naive_,
        "unknown_did_not_become_zero": (
            cautious.tokens is None if unknown_ else cautious.tokens == naive_),
        "summary_counts_the_unknowns": getattr(cautious, "tokens_unknown", None),
        "receipts_summed": cautious.receipts,
        "receipt_files_on_disk": on_disk,
        "receipts_agree_with_the_files": (
            None if on_disk is None else cautious.receipts == on_disk),
        "summary_line": cautious.line(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    file_path = args.run_root.resolve() / args.run_id / "telemetry.jsonl"
    sentences = [json.loads(z) for z in file_path.read_text().splitlines() if z.strip()]

    report = {
        "run_id": args.run_id,
        "telemetry_path": str(file_path),
        "records": len(sentences),
        "fields": check(sentences),
        "coverage": coverage_(sentences),
        "aggregation_control": aggregation_control(sentences, file_path.parent),
    }
    gaps = sorted(f for f, z in report["fields"].items() if z[MISSING])
    missing_ = sorted(k for k, v in report["coverage"].items() if not v)
    report["fields_with_gaps"] = gaps
    report["coverage_gaps"] = [k for k in missing_ if k not in EVENTS]
    report["shapes_not_observed"] = [k for k in missing_ if k in EVENTS]
    k = report["aggregation_control"]
    fields_ok = (not gaps and not report["coverage_gaps"]
                 and k["unknown_did_not_become_zero"]
                 and k["receipts_agree_with_the_files"] is not False)
    ok = fields_ok and not report["shapes_not_observed"]
    report["fields_validated_on_real_dispatches"] = "yes" if fields_ok else "no"
    report["telemetry_validated_on_real_dispatches"] = "yes" if ok else "no"
    if not ok and fields_ok:
        report["reason"] = (
            "every field is filled on every record; these shapes did not occur "
            "in this campaign and are therefore NOT_OBSERVED, which is not a "
            "pass: " + ", ".join(report["shapes_not_observed"]))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{len(sentences)} record(s) from {file_path}")
        for field_, z in sorted(report["fields"].items()):
            text_line = " ".join(f"{k}={v}" for k, v in z.items() if v)
            marke = "GAP " if z[MISSING] else "    "
            print(f"  {marke}{field_:<28s} {text_line}")
        print("  coverage:")
        for name, v in report["coverage"].items():
            print(f"    {'yes' if v else 'NO ':<4s} {name}")
        print(f"  aggregation control: {k['records_with_unknown_tokens']} unknown, "
              f"careful={k['careful_total']} naive={k['naive_total']} "
              f"-> unknown_did_not_become_zero={k['unknown_did_not_become_zero']}")
        print(f"  receipts summed {k['receipts_summed']} vs "
              f"{k['receipt_files_on_disk']} file(s) on disk "
              f"-> agree={k['receipts_agree_with_the_files']}")
        print(f"  summary line: {k['summary_line']}")
        print(f"  fields_validated_on_real_dispatches    = "
              f"{report['fields_validated_on_real_dispatches']}")
        print(f"  telemetry_validated_on_real_dispatches = "
              f"{report['telemetry_validated_on_real_dispatches']}")
        if report.get("reason"):
            print(f"  {report['reason']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
