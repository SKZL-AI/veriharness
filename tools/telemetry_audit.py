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


HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent / "src"))

MEASURED, NOT_AVAILABLE, MISSING = "MEASURED", "NOT_AVAILABLE", "MISSING"

#: The literal a record writes when a backend cannot report an identity. It is
#: the same string as the state, and deliberately so: the field says what the
#: audit would otherwise have to guess.

#: Fields whose `None` means "the harness does not report this", which is an
#: answer. Everything else at an empty default is a gap in the wiring.
#: `failure_class` is deliberately *not* here: on a dispatch that failed, the
#: class is derivable from the failure, so a `None` there is nobody having
#: asked `taxonomy.classify`, not the harness staying silent.
DARF_UNBEKANNT_SEIN = {
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
NULL_IST_EIN_WERT = {"attempt", "iteration", "retries", "waited_seconds",
                     "repair_depth", "discriminating", "artefactual",
                     # QA watches no directory listing: its working directory
                     # is the one it would be watching, so zero is the policy's
                     # answer rather than an unfilled field (limit 12d).
                     "witnessed_listings"}

#: Fields where the **empty string** is a real state rather than an unfilled
#: default. A bare `hoh run` has no project above it, and saying so with an
#: empty id is correct; `node_id` and `repair_of` are then judged against it
#: rather than against themselves.
LEER_IST_EIN_WERT = {"project_id"}

#: Fields that only mean something for a dispatch that ended a particular way,
#: so their default is not evidence of anything.
#: Fields that are only meaningful in some runs, and whose emptiness is
#: therefore not evidence of a gap. Each condition is asked of a **different**
#: field than the one being judged: four of these once read
#: `lambda r: bool(r.get(<the same field>))`, which exempts a field exactly
#: when it is empty and can never fire. An audit whose checks cannot fail is
#: the tick-box this project rejects one level up.
BEDINGT = {
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


def _zustand(feld: str, wert, satz: dict) -> str:
    if feld in BEDINGT and not BEDINGT[feld](satz):
        return MEASURED          # not applicable here; nothing was withheld
    if wert == NOT_AVAILABLE:
        # Stated rather than blank: the backend was asked and cannot say.
        return NOT_AVAILABLE
    if wert is None:
        return NOT_AVAILABLE if feld in DARF_UNBEKANNT_SEIN else MISSING
    if feld in NULL_IST_EIN_WERT or feld in LEER_IST_EIN_WERT:
        return MEASURED
    if wert == "" or (isinstance(wert, int) and not isinstance(wert, bool) and wert == 0):
        return MISSING
    return MEASURED


def pruefe(saetze: list[dict]) -> dict:
    felder: dict[str, dict[str, int]] = {}
    for satz in saetze:
        for feld, wert in satz.items():
            if feld == "schema_version":
                continue
            z = _zustand(feld, wert, satz)
            felder.setdefault(feld, {MEASURED: 0, NOT_AVAILABLE: 0, MISSING: 0})
            felder[feld][z] += 1
    return felder


#: Coverage entries that are about a **shape of event** rather than a field.
#: A campaign in which nothing failed has not validated the failure record --
#: and it has not failed the audit either. The two are reported apart, because
#: rolling them together would either hide a real wiring gap or make an
#: uneventful campaign look defective. Not observed is never "passed".
EREIGNISSE = {"failure_record", "retry_record", "failure_taxonomy"}


def abdeckung(saetze: list[dict]) -> dict:
    """The shapes §8 asks to see at least once each in a real campaign."""
    rollen = {s.get("role") for s in saetze}
    return {
        "planner_record": "planner" in rollen,
        "developer_record": "developer" in rollen,
        "qa_record": "qa" in rollen,
        "failure_record": any(s.get("outcome") != "ok" for s in saetze),
        "retry_record": any(s.get("retries", 0) > 0 for s in saetze),
        "provider_identity": any(s.get("provider") for s in saetze),
        "model_identity": all(s.get("model") for s in saetze),
        "effort_stated": all(s.get("effort") for s in saetze),
        # Asked only of records whose dispatch actually ran. A dispatch
        # refused at the budget never takes a witness, so `None` there is the
        # truthful record of "no witness was armed" -- and requiring one would
        # push the controller towards writing a coverage number for a witness
        # it never took, which is the fabricated measurement the record's own
        # docstring forbids. A record that predates `provider_calls` carries
        # `None`, not `0`, and is still asked.
        "witness_coverage_recorded": all(
            s.get("witnessed_trees") is not None
            for s in saetze if s.get("provider_calls") != 0),
        "wallclock": all(s.get("wallclock_seconds") is not None for s in saetze),
        "attempt": all("attempt" in s for s in saetze),
        "outcome": all(s.get("outcome") for s in saetze),
        "failure_taxonomy": any(
            s.get("failure_class") for s in saetze if s.get("outcome") != "ok"),
        "tokens_measured_or_explicitly_unavailable": all(
            ("tokens_in" in s and "tokens_out" in s) for s in saetze),
        "quota_proxy_labelled": all(
            bool(s.get("quota_proxy_kind")) or s.get("quota_proxy") is None
            for s in saetze),
    }


def aggregation_kontrolle(saetze: list[dict], lauf: Path | None = None) -> dict:
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

    objekte = [DispatchRecord.model_validate(s) for s in saetze]
    vorsichtig = summarise(objekte)
    naiv = sum((s.get("tokens_in") or 0) + (s.get("tokens_out") or 0)
               for s in saetze)
    unbekannt = sum(1 for s in saetze
                    if s.get("tokens_in") is None and s.get("tokens_out") is None)
    auf_platte = None
    if lauf is not None and (lauf / "receipts").is_dir():
        auf_platte = len(list((lauf / "receipts").glob("*.json")))
    return {
        "ran": bool(saetze),
        "records": len(saetze),
        "records_with_unknown_tokens": unbekannt,
        "careful_total": vorsichtig.tokens,
        "naive_total": naiv,
        "unknown_did_not_become_zero": (
            vorsichtig.tokens is None if unbekannt else vorsichtig.tokens == naiv),
        "summary_counts_the_unknowns": getattr(vorsichtig, "tokens_unknown", None),
        "receipts_summed": vorsichtig.receipts,
        "receipt_files_on_disk": auf_platte,
        "receipts_agree_with_the_files": (
            None if auf_platte is None else vorsichtig.receipts == auf_platte),
        "summary_line": vorsichtig.line(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-root", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    pfad = args.run_root.resolve() / args.run_id / "telemetry.jsonl"
    saetze = [json.loads(z) for z in pfad.read_text().splitlines() if z.strip()]

    bericht = {
        "run_id": args.run_id,
        "telemetry_path": str(pfad),
        "records": len(saetze),
        "fields": pruefe(saetze),
        "coverage": abdeckung(saetze),
        "aggregation_control": aggregation_kontrolle(saetze, pfad.parent),
    }
    luecken = sorted(f for f, z in bericht["fields"].items() if z[MISSING])
    fehlend = sorted(k for k, v in bericht["coverage"].items() if not v)
    bericht["fields_with_gaps"] = luecken
    bericht["coverage_gaps"] = [k for k in fehlend if k not in EREIGNISSE]
    bericht["shapes_not_observed"] = [k for k in fehlend if k in EREIGNISSE]
    k = bericht["aggregation_control"]
    felder_ok = (not luecken and not bericht["coverage_gaps"]
                 and k["unknown_did_not_become_zero"]
                 and k["receipts_agree_with_the_files"] is not False)
    ok = felder_ok and not bericht["shapes_not_observed"]
    bericht["fields_validated_on_real_dispatches"] = "yes" if felder_ok else "no"
    bericht["telemetry_validated_on_real_dispatches"] = "yes" if ok else "no"
    if not ok and felder_ok:
        bericht["reason"] = (
            "every field is filled on every record; these shapes did not occur "
            "in this campaign and are therefore NOT_OBSERVED, which is not a "
            "pass: " + ", ".join(bericht["shapes_not_observed"]))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(bericht, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.json:
        print(json.dumps(bericht, indent=2))
    else:
        print(f"{len(saetze)} record(s) from {pfad}")
        for feld, z in sorted(bericht["fields"].items()):
            zeile = " ".join(f"{k}={v}" for k, v in z.items() if v)
            marke = "GAP " if z[MISSING] else "    "
            print(f"  {marke}{feld:<28s} {zeile}")
        print("  coverage:")
        for name, v in bericht["coverage"].items():
            print(f"    {'yes' if v else 'NO ':<4s} {name}")
        print(f"  aggregation control: {k['records_with_unknown_tokens']} unknown, "
              f"careful={k['careful_total']} naive={k['naive_total']} "
              f"-> unknown_did_not_become_zero={k['unknown_did_not_become_zero']}")
        print(f"  receipts summed {k['receipts_summed']} vs "
              f"{k['receipt_files_on_disk']} file(s) on disk "
              f"-> agree={k['receipts_agree_with_the_files']}")
        print(f"  summary line: {k['summary_line']}")
        print(f"  fields_validated_on_real_dispatches    = "
              f"{bericht['fields_validated_on_real_dispatches']}")
        print(f"  telemetry_validated_on_real_dispatches = "
              f"{bericht['telemetry_validated_on_real_dispatches']}")
        if bericht.get("reason"):
            print(f"  {bericht['reason']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
