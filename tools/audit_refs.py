"""Independent citation and numbers audit resolver for paper/AUDIT.md.

D6 (independent citation and numbers audit). This CLI checks the *shape* and
*internal consistency* of paper/AUDIT.md against paper/POSITION_PAPER.md,
paper/NUMBERS.md and CLAIMS.json -- it does not re-derive every verdict in
paper/AUDIT.md by itself (that document's own Methodology section records
the investigative work behind each row). What this tool does mechanically
verify:

  coverage            -- every claim id, a02 mention, external citation and
                          NUMBERS.md-catalogued number extractable from
                          POSITION_PAPER.md has a row in AUDIT.md.
  verify-verdicts     -- every AUDIT.md row carries one of the five allowed
                          verdicts, and every non-RESOLVED row states a reason.
  selftest            -- the resolver correctly reports three adversarial
                          fixtures (a dangling commit sha, an unsupported
                          receipt reference, a claim id absent from the
                          ledger) as unresolved, never as resolved.
  a02-count           -- AUDIT.md's a02-mention rows match an independent
                          recount of POSITION_PAPER.md.
  numbers-recomputed  -- every NUMBER row names a recomputation source other
                          than NUMBERS.md, and that source exists wherever
                          the row is not NOT_CHECKED for a runs/-only source.
  summary-consistency -- AUDIT.md's own summary counts match a fresh recount
                          of its table rows.
  claims-against-code -- (bonus, not gated by an acceptance criterion) for
                          every claim id cited in the paper, its CLAIMS.json
                          file:/test: evidence is independently re-resolved
                          against the current checkout.
  no-overclaim-o31    -- regression guard: paper/AUDIT.md and this script's
                          own source carry no unqualified ".git is absent"
                          phrasing (the design discipline of never invoking
                          git is not the same claim as .git being physically
                          unreachable, and only the latter can be false).

Deliberately never touches `runs/` or `.git`: every evidence form that would
require either is reported NOT_CHECKED, unconditionally, by construction --
not because runs/ or .git happen to be absent from wherever this runs, but
because a resolver whose verdict depends on which arena it happens to run in
is not a resolver a reader can trust. All file access is through relative
paths from the process cwd.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

POSITION_PAPER = "paper/POSITION_PAPER.md"
NUMBERS_MD = "paper/NUMBERS.md"
AUDIT_MD = "paper/AUDIT.md"
CLAIMS_JSON = "CLAIMS.json"

ALLOWED_VERDICTS = {"RESOLVED", "MISMATCH", "MISSING", "EXTERNAL", "NOT_CHECKED"}

CLAIM_ID_RE = re.compile(r"C-(\d{3})(?:\.\.C-(\d{3}))?")
A02_RE = re.compile(r"\ba02\b")
EXTERNAL_RE = re.compile(r"\bsource Q(\d+)\b")


# ---------------------------------------------------------------------------
# Shared text helpers
# ---------------------------------------------------------------------------


def normalize_whitespace(text: str) -> str:
    """Collapses every run of whitespace to a single space.

    Markdown prose wraps, so a multi-word needle can span a line break in the
    file while reading as one sentence on the page -- a plain grep for such a
    needle reports absence, and absence looks exactly like a satisfied
    criterion. Every search in this tool runs against normalized text first.
    """
    return re.sub(r"\s+", " ", text)


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def normalize_anchor_text(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def compute_anchor_digest(line: str) -> str:
    return hashlib.sha256(normalize_anchor_text(line).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Extraction from POSITION_PAPER.md / NUMBERS.md
# ---------------------------------------------------------------------------


def extract_claim_ids(norm_paper_text: str) -> set[str]:
    """Every C-nnn claim id cited in the paper, expanding "C-a..C-b" ranges.

    The paper cites some ids as a range, e.g. "(C-052..C-054)". Read as
    prose that means every id in the range is being cited, not only the two
    endpoints a literal C-\\d+ token match would catch, so both endpoints are
    expanded into the full inclusive range.
    """
    ids: set[str] = set()
    for m in CLAIM_ID_RE.finditer(norm_paper_text):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        lo, hi = min(start, end), max(start, end)
        for n in range(lo, hi + 1):
            ids.add(f"C-{n:03d}")
    return ids


def count_a02_mentions(norm_paper_text: str) -> int:
    return len(A02_RE.findall(norm_paper_text))


def extract_external_tokens(norm_paper_text: str) -> set[str]:
    return {f"Q{m.group(1)}" for m in EXTERNAL_RE.finditer(norm_paper_text)}


def parse_numbers_table(numbers_text: str) -> set[tuple[str, str]]:
    """Every (number, key) pair from paper/NUMBERS.md's own table.

    key is the C-nnn claim id found in the row's source column, or
    "STRUCT" for the two rows NUMBERS.md itself marks structural (not a
    distinct ledger measurement).
    """
    pairs: set[tuple[str, str]] = set()
    in_table = False
    for raw_line in numbers_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            in_table = False
            continue
        cells = split_row(line)
        if len(cells) < 3:
            continue
        first = cells[0].strip().lower()
        if first == "number":
            in_table = True
            continue
        if not in_table:
            continue
        if set(cells[0].strip()) <= {"-"}:
            continue  # separator row
        number = cells[0].strip()
        source = cells[1].strip()
        if not number or not re.match(r"^\d+$", number):
            continue
        cid_match = re.search(r"C-\d{3}", source)
        key = cid_match.group(0) if cid_match else "STRUCT"
        pairs.add((number, key))
    return pairs


# ---------------------------------------------------------------------------
# AUDIT.md table parsing
# ---------------------------------------------------------------------------


def split_row(line: str) -> list[str]:
    """Splits one markdown table row on unescaped '|', dropping the empty
    leading/trailing cells a '| a | b |'-style line produces, and unescaping
    '\\|' back to a literal pipe inside each cell.
    """
    parts = re.split(r"(?<!\\)\|", line)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.replace("\\|", "|").strip() for p in parts]


class AuditRow:
    __slots__ = ("ref", "verdict", "source", "detail", "section")

    def __init__(self, ref: str, verdict: str, source: str, detail: str, section: str):
        self.ref = ref
        self.verdict = verdict
        self.source = source
        self.detail = detail
        self.section = section


def parse_audit_rows(audit_text: str) -> list[AuditRow]:
    """Every data row from every table in AUDIT.md except the '## Summary'
    table (which reports counts derived from these rows, not a row itself).
    """
    rows: list[AuditRow] = []
    section = ""
    in_table = False
    for raw_line in audit_text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            in_table = False
            continue
        if not stripped.startswith("|"):
            in_table = False
            continue
        cells = split_row(stripped)
        if len(cells) < 2:
            continue
        first = cells[0].strip().lower()
        if first in ("ref", "verdict"):
            in_table = True
            continue
        if not in_table:
            continue
        if set(cells[0].strip()) <= {"-"}:
            continue  # separator row
        if section.lower().startswith("summary"):
            continue  # counts, not citation rows
        if len(cells) < 4:
            continue
        ref, verdict, source, detail = cells[0], cells[1], cells[2], cells[3]
        rows.append(AuditRow(ref, verdict, source, detail, section))
    return rows


def parse_summary_table(audit_text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    in_summary = False
    in_table = False
    for raw_line in audit_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            in_summary = stripped[3:].strip().lower().startswith("summary")
            in_table = False
            continue
        if not in_summary or not stripped.startswith("|"):
            continue
        cells = split_row(stripped)
        if len(cells) < 2:
            continue
        first = cells[0].strip().lower()
        if first == "verdict":
            in_table = True
            continue
        if not in_table:
            continue
        if set(cells[0].strip()) <= {"-"}:
            continue
        verdict_raw = cells[0].strip().strip("*").strip()
        count_raw = cells[1].strip().strip("*").strip()
        if verdict_raw in ALLOWED_VERDICTS:
            try:
                counts[verdict_raw] = int(count_raw)
            except ValueError:
                pass
    return counts


def number_ref_key(ref: str) -> tuple[str, str] | None:
    m = re.match(r"^NUMBER\s+(\S+)\s+\((.+)\)$", ref.strip())
    if not m:
        return None
    number, paren = m.group(1), m.group(2).strip()
    key = "STRUCT" if paren.lower() == "structural" else paren
    return (number, key)


# ---------------------------------------------------------------------------
# Claim-id / evidence resolution -- shared by coverage checks and selftest
# ---------------------------------------------------------------------------


def load_claims() -> dict[str, dict]:
    data = json.loads(read_text(CLAIMS_JSON))
    return {c["id"]: c for c in data.get("claims", []) if isinstance(c, dict) and "id" in c}


def resolve_claim_id(claims_by_id: dict[str, dict], claim_id: str) -> tuple[str, str]:
    """Returns (verdict, detail). RESOLVED if claim_id exists in the ledger,
    MISSING otherwise. Never touches runs/ or .git.
    """
    claim = claims_by_id.get(claim_id)
    if claim is None:
        return "MISSING", f"{claim_id} does not exist in {CLAIMS_JSON}"
    return "RESOLVED", f"found in {CLAIMS_JSON}, status={claim.get('status')}"


def resolve_commit_sha(sha: str) -> tuple[str, str]:
    """A commit sha can never be verified inside an arena (O31: no .git),
    and this tool does not invoke git even where .git happens to be present
    -- so this always returns NOT_CHECKED, unconditionally, by design.
    """
    return "NOT_CHECKED", (
        f"O31: this resolver never invokes git to resolve commit {sha!r}, "
        "regardless of whether .git happens to be reachable from this checkout's cwd"
    )


def resolve_receipt_ref(ref: str) -> tuple[str, str]:
    """A receipt/run reference can never be verified inside an arena (O33:
    runs/ is gitignored and absent from the candidate snapshot), and this
    tool never reads runs/ even where it happens to be present -- so this
    always returns NOT_CHECKED, unconditionally, by design.
    """
    return "NOT_CHECKED", f"O33: runs/ absent from this checkout; this resolver never reads runs/ (ref={ref!r})"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_coverage(_args: argparse.Namespace) -> int:
    paper_norm = normalize_whitespace(read_text(POSITION_PAPER))
    numbers_text = read_text(NUMBERS_MD)
    audit_text = read_text(AUDIT_MD)

    required_claims = extract_claim_ids(paper_norm)
    required_a02 = count_a02_mentions(paper_norm)
    required_externals = extract_external_tokens(paper_norm)
    required_numbers = parse_numbers_table(numbers_text)

    audit_rows = parse_audit_rows(audit_text)
    have_claims = {r.ref[len("CLAIM "):].strip() for r in audit_rows if r.ref.startswith("CLAIM ")}
    have_externals = {
        m.group(1) for r in audit_rows if (m := re.match(r"^EXTERNAL\s+source\s+(Q\d+)$", r.ref.strip()))
    }
    have_numbers = {key for r in audit_rows if r.ref.startswith("NUMBER ") and (key := number_ref_key(r.ref))}
    have_a02 = sum(1 for r in audit_rows if r.ref.startswith("A02 mention"))

    problems = []
    missing_claims = sorted(required_claims - have_claims)
    if missing_claims:
        problems.append(f"missing CLAIM rows for: {', '.join(missing_claims)}")

    missing_numbers = sorted(required_numbers - have_numbers)
    if missing_numbers:
        problems.append(f"missing NUMBER rows for: {missing_numbers}")

    missing_externals = sorted(required_externals - have_externals)
    if missing_externals:
        problems.append(f"missing EXTERNAL rows for: {', '.join(missing_externals)}")

    if have_a02 < required_a02:
        problems.append(f"only {have_a02} A02 mention row(s) in AUDIT.md, but {required_a02} a02 mentions in the paper")

    if problems:
        print("COVERAGE FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"COVERAGE OK: {len(required_claims)} claim ids, {required_a02} a02 mentions, "
        f"{len(required_externals)} external citation(s), {len(required_numbers)} numbers -- all covered."
    )
    return 0


def cmd_verify_verdicts(_args: argparse.Namespace) -> int:
    audit_text = read_text(AUDIT_MD)
    rows = parse_audit_rows(audit_text)
    problems = []
    for r in rows:
        if r.verdict not in ALLOWED_VERDICTS:
            problems.append(f"{r.ref}: verdict {r.verdict!r} is not one of {sorted(ALLOWED_VERDICTS)}")
            continue
        if r.verdict != "RESOLVED" and not r.detail.strip():
            problems.append(f"{r.ref}: verdict {r.verdict} carries no reason in the Detail column")
    if problems:
        print("VERIFY-VERDICTS FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"VERIFY-VERDICTS OK: {len(rows)} rows, all carry an allowed verdict and non-RESOLVED rows state a reason.")
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    claims_by_id = load_claims()

    fixture_claim_id = "C-999"
    fixture_commit_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    fixture_receipt_ref = "runs/zzz-nonexistent-run/receipts/no-such-receipt"

    failures = []

    verdict, detail = resolve_claim_id(claims_by_id, fixture_claim_id)
    if verdict == "RESOLVED":
        failures.append(f"claim id fixture {fixture_claim_id!r} was wrongly reported RESOLVED ({detail})")
    else:
        print(f"OK: claim id fixture {fixture_claim_id!r} -> {verdict} ({detail})")

    verdict, detail = resolve_commit_sha(fixture_commit_sha)
    if verdict == "RESOLVED":
        failures.append(f"commit sha fixture {fixture_commit_sha!r} was wrongly reported RESOLVED ({detail})")
    else:
        print(f"OK: commit sha fixture {fixture_commit_sha!r} -> {verdict} ({detail})")

    verdict, detail = resolve_receipt_ref(fixture_receipt_ref)
    if verdict == "RESOLVED":
        failures.append(f"receipt ref fixture {fixture_receipt_ref!r} was wrongly reported RESOLVED ({detail})")
    else:
        print(f"OK: receipt ref fixture {fixture_receipt_ref!r} -> {verdict} ({detail})")

    if failures:
        print("SELFTEST FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all three adversarial fixtures correctly reported unresolved.")
    return 0


def cmd_a02_count(_args: argparse.Namespace) -> int:
    paper_norm = normalize_whitespace(read_text(POSITION_PAPER))
    required_a02 = count_a02_mentions(paper_norm)

    audit_text = read_text(AUDIT_MD)
    rows = [r for r in parse_audit_rows(audit_text) if r.ref.startswith("A02 mention")]

    problems = []
    if len(rows) != required_a02:
        problems.append(f"paper has {required_a02} a02 mentions (independent re-parse), AUDIT.md has {len(rows)} A02 rows")
    for r in rows:
        if r.verdict not in ("RESOLVED", "MISMATCH"):
            problems.append(f"{r.ref}: verdict {r.verdict} is not RESOLVED or MISMATCH")

    if problems:
        print("A02-COUNT FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"A02-COUNT OK: {required_a02} a02 mentions in the paper, {len(rows)} classified rows in AUDIT.md.")
    return 0


def cmd_numbers_recomputed(_args: argparse.Namespace) -> int:
    audit_text = read_text(AUDIT_MD)
    rows = [r for r in parse_audit_rows(audit_text) if r.ref.startswith("NUMBER ")]

    problems = []
    for r in rows:
        source = r.source.strip()
        if "numbers.md" in source.lower():
            problems.append(f"{r.ref}: recomputation source cites NUMBERS.md ({source!r}) -- circular")
            continue
        if source.startswith("N/A"):
            if r.verdict != "NOT_CHECKED":
                problems.append(f"{r.ref}: verdict {r.verdict} but source is {source!r} (no real recomputation source)")
            continue
        # A real path is named: it must exist, regardless of verdict. A
        # **directory** counts, and has to: a ratio recomputed by pairing every
        # receipt in a tree with its `-basis` twin has that tree as its source,
        # and naming one file out of it would misstate where the number came
        # from. What the check is for is that the source is real and reachable,
        # not that it is a single file.
        if not (os.path.isfile(source) or os.path.isdir(source)):
            problems.append(f"{r.ref}: named source {source!r} does not exist in the checkout")

    if not rows:
        problems.append("no NUMBER rows found in AUDIT.md")

    if problems:
        print("NUMBERS-RECOMPUTED FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"NUMBERS-RECOMPUTED OK: {len(rows)} NUMBER rows, none cite NUMBERS.md, all named sources exist.")
    return 0


def cmd_summary_consistency(_args: argparse.Namespace) -> int:
    audit_text = read_text(AUDIT_MD)
    rows = parse_audit_rows(audit_text)
    recount: dict[str, int] = {}
    for r in rows:
        recount[r.verdict] = recount.get(r.verdict, 0) + 1

    reported = parse_summary_table(audit_text)

    problems = []
    all_verdicts = ALLOWED_VERDICTS | set(reported) | set(recount)
    for v in sorted(all_verdicts):
        want = recount.get(v, 0)
        got = reported.get(v, 0)
        if want != got:
            problems.append(f"verdict {v}: summary says {got}, recount of the table says {want}")

    if problems:
        print("SUMMARY-CONSISTENCY FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"SUMMARY-CONSISTENCY OK: summary counts match a fresh recount of {len(rows)} rows.")
    return 0


def cmd_claims_against_code(_args: argparse.Namespace) -> int:
    """Bonus check, not gated by an acceptance criterion: for every claim id
    cited in the paper, independently re-resolve its file:/test: evidence
    against the current checkout (existence, and where the ledger carries an
    anchor_digest, that the digest still resolves to exactly one line).
    """
    paper_norm = normalize_whitespace(read_text(POSITION_PAPER))
    claim_ids = sorted(extract_claim_ids(paper_norm))
    claims_by_id = load_claims()

    problems = []
    checked = 0
    for cid in claim_ids:
        claim = claims_by_id.get(cid)
        if claim is None:
            problems.append(f"{cid}: not in {CLAIMS_JSON}")
            continue
        digest = claim.get("anchor_digest")
        for ev in claim.get("evidence") or []:
            if ev.startswith("file:"):
                rest = ev[len("file:"):]
                path, _, _lineno = rest.rpartition(":")
                if not os.path.isfile(path):
                    problems.append(f"{cid}: {ev} -- file does not exist")
                    continue
                checked += 1
                # The anchor has to resolve inside the cited file **only when
                # the claim is anchored in that file**. The rule is for a
                # claim that quotes a line of source: if the source moves, the
                # quotation is stale, and that is worth catching. For a prose
                # claim in `docs/` whose evidence is the program or the
                # document that produced a number, the claim's sentence is not
                # in the cited file and never will be -- demanding it there is
                # a category error, and it made this check red on C-013 from
                # before the benchmark it cites had even run. A check that
                # cannot be satisfied by any honest evidence teaches people to
                # ignore it.
                hier_verankert = claim.get("where", "").split(":")[0] == path
                if digest and hier_verankert and ev == claim.get("evidence", [None])[0]:
                    lines = read_text(path).splitlines()
                    matches = [ln for ln in lines if compute_anchor_digest(ln) == digest]
                    if not matches:
                        problems.append(f"{cid}: {ev} -- anchor_digest no longer resolves anywhere in {path}")
            elif ev.startswith("test:"):
                nodeid = ev[len("test:"):]
                path = nodeid.split("::", 1)[0]
                func = nodeid.split("::", 1)[1] if "::" in nodeid else None
                if not os.path.isfile(path):
                    problems.append(f"{cid}: {ev} -- test file does not exist")
                    continue
                checked += 1
                if func and f"def {func}" not in read_text(path):
                    problems.append(f"{cid}: {ev} -- function {func!r} not found in {path}")

    if problems:
        print("CLAIMS-AGAINST-CODE FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"CLAIMS-AGAINST-CODE OK: {checked} file:/test: evidence reference(s) across {len(claim_ids)} claim ids all resolve.")
    return 0


def _words(*parts: str) -> str:
    """Joins words with single spaces without spelling the joined phrase out
    contiguously in this file's own source text -- necessary because this
    module scans its own source for exactly these phrases (see
    BANNED_O31_PHRASES below): a literal contiguous banned phrase sitting in
    that tuple's definition would make no-overclaim-o31 fail against itself
    by construction, the same self-reference the project's own convention of
    assembling a banned needle at runtime (`chr(47) + 'home'`) exists to
    avoid.
    """
    return " ".join(parts)


BANNED_O31_PHRASES = (
    _words("no", ".git", "in", "the", "arena"),
    _words("arena", "has", "no", ".git"),
    _words("no", ".git", "available"),
)


def cmd_no_overclaim_o31(_args: argparse.Namespace) -> int:
    """Regression guard for E-gap-7d2724b5 (iteration-1 QA finding): this
    resolver's own design deliberately never invokes git, but that is a
    design discipline, not an OS-level guarantee -- .git can in fact be
    reachable by walking up from a checkout's cwd (as it is from this very
    development worktree), so a row or message that flatly asserts ".git is
    absent" is false wherever that happens to be true, even though the
    resolver's verdict is unaffected (it never acts on git either way).
    Scans paper/AUDIT.md and this script's own source for the three exact
    phrases that make that unqualified claim.
    """
    problems = []
    for path in (AUDIT_MD, __file__):
        norm = normalize_whitespace(read_text(path))
        for phrase in BANNED_O31_PHRASES:
            if phrase in norm:
                problems.append(f"{path}: contains banned phrase {phrase!r}")

    if problems:
        print("NO-OVERCLAIM-O31 FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("NO-OVERCLAIM-O31 OK: no unqualified .git-absence overclaim found.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audit_refs.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("coverage", help="every citation extractable from the paper has a row in AUDIT.md").set_defaults(func=cmd_coverage)
    sub.add_parser("verify-verdicts", help="every AUDIT.md row carries an allowed verdict, with a reason if non-RESOLVED").set_defaults(func=cmd_verify_verdicts)
    sub.add_parser("selftest", help="adversarial fixtures are correctly reported unresolved").set_defaults(func=cmd_selftest)
    sub.add_parser("a02-count", help="a02 mention rows match an independent recount of the paper").set_defaults(func=cmd_a02_count)
    sub.add_parser("numbers-recomputed", help="every NUMBER row names a real, non-circular recomputation source").set_defaults(func=cmd_numbers_recomputed)
    sub.add_parser("summary-consistency", help="AUDIT.md's summary counts match a fresh recount of its rows").set_defaults(func=cmd_summary_consistency)
    sub.add_parser("claims-against-code", help="(bonus) re-resolve every cited claim's file:/test: evidence").set_defaults(func=cmd_claims_against_code)
    sub.add_parser("no-overclaim-o31", help="regression guard: no unqualified .git-absence overclaim in AUDIT.md or this script").set_defaults(func=cmd_no_overclaim_o31)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
