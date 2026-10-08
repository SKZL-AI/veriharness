"""Proof classes: what evidence would settle a requirement, declared before the
status is observed.

One `PROVEN` flag was doing the work of five different claims -- built,
unit-tested, integrated, behaviourally proven, phase-gate satisfied -- and twelve
P2 items were registered PROVEN for modules nothing calls. Capping a status when
no entry point imports the module caught all twelve, but it cannot be the rule:
an import inside `if False:` satisfies it, and a boundary contract's real consumer
lives outside this repository, so a correct implementation of one can never pass.

So every requirement carries an explicit `ProofClass`, and the class says what
evidence would settle it (`WHAT_WOULD_SETTLE_IT`).

A proof class is an escape hatch unless it is constrained: "this is only a
schema, so no consumer is needed" restores every one of the twelve overclaims in
better vocabulary. So a class is not merely declared. It is justified against the
plan's own words, and that justification is machine-checked here:

* the quote must be a whole-word, in-order run of the plan item's text
  (case and whitespace normalised), at least `MIN_QUOTE_WORDS` words long;
* the plan item is the one the requirement maps to -- `fsck` looks it up from
  `requirement_plan_items`, never from the declaration itself;
* the declaration names the plan digest it was made against;
* the two classes that remove an evidence requirement (`ARTEFACT`,
  `BOUNDARY_CONTRACT`) must quote at least one word of
  `EVIDENCE_REMOVAL_VOCABULARY`.

A reclassification that makes a capability easier to prove is visible as
exactly that: `assignment_is_a_relaxation`.

This module is the model and the checks only. Nothing imports `hoh.proofclass`
yet: wiring it into the capability matrix is a separate, separately reviewed
increment, and the real register and plan are handed in as arguments -- nothing
here reads a file.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class ProofClass(StrEnum):
    """What kind of claim a requirement makes, and so what evidence settles it.

    The order is fixed by the spec; a serialised register depends on it.
    """

    ARTEFACT = "ARTEFACT"
    INTERNAL_RUNTIME = "INTERNAL_RUNTIME"
    CONTROL_PLANE = "CONTROL_PLANE"
    BOUNDARY_CONTRACT = "BOUNDARY_CONTRACT"
    COMPOSITE = "COMPOSITE"
    SUBSTITUTE = "SUBSTITUTE"


WHAT_WOULD_SETTLE_IT: Mapping[ProofClass, str] = MappingProxyType({
    ProofClass.ARTEFACT: (
        "A deterministic artefact, schema, parser or pure transformation. Settled "
        "by a module-scoped symbol, executable tests with negative controls, and "
        "deterministic re-derivation where the artefact is generated. No runtime "
        "consumer is required, because the requirement does not promise one."
    ),
    ProofClass.INTERNAL_RUNTIME: (
        "Behaviour of VeriHarness itself. The real controller or CLI path must "
        "reach it, and the strongest available evidence is that removing the "
        "wiring makes a behavioural probe fail. An import statement is not "
        "sufficient: an import under `if False:` satisfies an import check and "
        "reaches nothing."
    ),
    ProofClass.CONTROL_PLANE: (
        "The declared product surface is a tool or command. The tool's own path "
        "is the evidence; nothing needs to import the module for the tool to be "
        "the surface."
    ),
    ProofClass.BOUNDARY_CONTRACT: (
        "An interface whose consumer may live outside this repository. Settled by "
        "contract validity, authority and ownership semantics, deterministic "
        "conflict behaviour, and fail-closed behaviour on unknown or invalid "
        "external state. An internal importer is not required, and a manufactured "
        "importer does not count as evidence."
    ),
    ProofClass.COMPOSITE: (
        "The plan promises a whole operational loop, and only the loop settles "
        "it: a set of independently tested components is not the composite "
        "capability. Example: the unattended-resumption chain is settled only by "
        "an unattended resumption running end to end, never by tests of its "
        "parts one at a time."
    ),
    ProofClass.SUBSTITUTE: (
        "What exists stands in for what was asked. Caps at PARTIAL, always, "
        "everywhere -- the doctrine the capability matrix has always stated; no "
        "evidence about the substitute lifts the cap."
    ),
})

# Measured against the plain bar "a symbol exists and its tests are collected".
RAISES_THE_BAR: frozenset[ProofClass] = frozenset({
    ProofClass.SUBSTITUTE, ProofClass.INTERNAL_RUNTIME, ProofClass.COMPOSITE})
LOWERS_THE_BAR: frozenset[ProofClass] = frozenset({
    ProofClass.ARTEFACT, ProofClass.CONTROL_PLANE, ProofClass.BOUNDARY_CONTRACT})

# The classes that remove an evidence requirement, and the words their quote
# must contain. A module constant so the list is reviewable, not a judgement
# made per call. Matched as whole words of the quote, and any dash-like
# character (`WORD_JOINERS`) is part of a word: "database", "baseline",
# "base-line" and "base\u2010line" do not contain the word "base", "data-model"
# not the word "model". This refuses rather than admits hyphenated (and plural)
# forms of the vocabulary -- it fails closed, and widening it would widen what
# can lower the bar, so it is a known limit, not an oversight.
EVIDENCE_REMOVING_CLASSES: frozenset[ProofClass] = frozenset({
    ProofClass.ARTEFACT, ProofClass.BOUNDARY_CONTRACT})
EVIDENCE_REMOVAL_VOCABULARY: frozenset[str] = frozenset({
    "schema", "contract", "interface", "identifier", "projection", "base", "model"})

# A quote of one or two words ("e", "the schema") satisfies "verbatim" and says
# nothing about what the plan item promises.
MIN_QUOTE_WORDS = 3

# How long a plan item's text can be. A plan item is one line or bullet of the
# plan: the real inventory's longest `text` is 153 characters over 306 items.
# 1000 leaves more than six times that as headroom and still refuses a "quote"
# that pastes a whole section of the plan -- which no single item's text is.
# This is only the static ceiling, checked at construction where no plan item
# is at hand. The per-item bound -- a quote no longer than the text of the item
# it is handed -- is enforced in `justification_problems`.
MAX_PLAN_ITEM_TEXT_CHARS = 1000

# The characters that join two runs of word characters into one word: the ASCII
# hyphen, every dash of Unicode category Pd (U+2010 hyphen, U+2013 en dash,
# U+2014 em dash, U+FF0D fullwidth hyphen-minus, ...), U+00AD soft hyphen and
# U+2212 minus sign. Built deterministically from the running interpreter's
# Unicode table, so no dash can split a word that the ASCII hyphen joins --
# which is how 'for the base' out of 'base\u2010line' escaped the ASCII-only rule.
# An unspaced dash between words ("the schema\u2014and the model") therefore
# makes one word: that refuses rather than admits, it fails closed. A spaced
# dash ("A \u2013 B"), a comma and a slash still separate words.
WORD_JOINERS: frozenset[str] = frozenset(
    {"-", "\u00ad", "\u2212"}
    | {chr(n) for n in range(sys.maxunicode + 1) if unicodedata.category(chr(n)) == "Pd"})

if not (RAISES_THE_BAR.isdisjoint(LOWERS_THE_BAR)
        and RAISES_THE_BAR | LOWERS_THE_BAR == frozenset(ProofClass)):
    raise RuntimeError("RAISES_THE_BAR and LOWERS_THE_BAR must partition ProofClass")
if set(WHAT_WOULD_SETTLE_IT) != set(ProofClass):
    raise RuntimeError("WHAT_WOULD_SETTLE_IT must have exactly one entry per ProofClass")
if not EVIDENCE_REMOVING_CLASSES <= LOWERS_THE_BAR:
    raise RuntimeError("an evidence-removing class must lower the bar")

_WORD = re.compile(r"\w+")
# One character class for a word character or a joiner, used by both the
# whole-word boundary and the vocabulary tokeniser so the two cannot disagree.
_WORD_OR_JOINER = r"[\w" + "".join(re.escape(c) for c in sorted(WORD_JOINERS)) + "]"
# A word for the vocabulary burden: every joiner is word-internal.
_JOINED_WORD = re.compile(_WORD_OR_JOINER + "+")
_NOT_AFTER_A_WORD = "(?<!" + _WORD_OR_JOINER + ")"
_NOT_BEFORE_A_WORD = "(?!" + _WORD_OR_JOINER + ")"
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Casefold, collapse every whitespace run to one space, strip.

    Every comparison in this module goes through it, so case and line wrapping
    never decide whether a quote is the plan's words.
    """
    return _WHITESPACE.sub(" ", text.casefold()).strip()


def _quote_word_count(text: str) -> int:
    """Whitespace-separated tokens that contain a word character.

    "a-b-c" is one word here and "the - end" is two: punctuation neither adds
    words nor counts as one.
    """
    return sum(1 for token in normalise(text).split(" ") if _WORD.search(token))


def _occurs_on_word_boundaries(quote: str, text: str) -> bool:
    """True when `quote` is a whole-word, in-order run of `text` (both normalised).

    The match may be neither preceded nor followed by a word character or a
    word joiner (`WORD_JOINERS`: the hyphen and every dash-like character),
    whatever the quote's own first and last characters are. Any dash is part
    of a word, so "base" does not occur in "database", "baseline", "base-line"
    or "base\u2013line", "model" not in "remodel"; and a quote led or ended by
    punctuation cannot splice into a word either ("-line interface" and
    "for the base-" are not whole words of "for the base-line interface").
    The whole hyphenated word ("for the base-line interface") does occur.
    Treating every dash as word-internal fails closed: an unspaced dash only
    ever makes a quote harder to accept, never easier.
    """
    if not quote:
        return False
    return re.search(_NOT_AFTER_A_WORD + re.escape(quote) + _NOT_BEFORE_A_WORD,
                     text) is not None


class ProofDeclaration(BaseModel):
    """What the register carries for one requirement's proof class. Frozen.

    Construction refuses a blank id or digest, an empty `why`, an empty quote, a
    quote longer than any plan item's text can be (`MAX_PLAN_ITEM_TEXT_CHARS`)
    and a quote of fewer than `MIN_QUOTE_WORDS` words. Whether the quote is the
    plan's words is not decidable here -- the plan item is not handed in -- and
    is `justification_problems`' job.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    requirement_id: str
    plan_item_id: str
    proof_class: ProofClass
    quoted_plan_text: str
    why: str
    declared_at: datetime
    declared_against_plan_digest: str

    @field_validator("requirement_id", "plan_item_id", "declared_against_plan_digest", "why")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("quoted_plan_text")
    @classmethod
    def _a_quote_that_can_mean_something(cls, value: str) -> str:
        if not value:
            raise ValueError("quoted_plan_text must not be empty")
        if len(value) > MAX_PLAN_ITEM_TEXT_CHARS:
            raise ValueError(
                f"quoted_plan_text is {len(value)} characters; no plan item's text "
                f"is longer than {MAX_PLAN_ITEM_TEXT_CHARS}, so this is not a quote of one")
        words = _quote_word_count(value)
        if words < MIN_QUOTE_WORDS:
            raise ValueError(
                f"quoted_plan_text has {words} word(s); at least {MIN_QUOTE_WORDS} "
                "are required for a quote to say what the plan item promises")
        return value


def _plan_item_id(plan_item: Any) -> tuple[str | None, str | None]:
    """(id, None) for a readable plan item, else (None, the problem).

    The id is read from `id` (the key the real inventory uses) with `item_id`
    accepted as an alias. An item carrying both with different values is
    ambiguous and refused.
    """
    if not isinstance(plan_item, Mapping):
        return None, f"plan item {plan_item!r} is not a mapping"
    ids = [plan_item[k] for k in ("id", "item_id") if k in plan_item]
    if not ids:
        return None, (f"plan item without an 'id' or 'item_id' key: "
                      f"text={plan_item.get('text')!r}")
    if len(set(map(repr, ids))) > 1:
        return None, (f"plan item has 'id' {ids[0]!r} and a different 'item_id' "
                      f"{ids[1]!r}: text={plan_item.get('text')!r}")
    if not isinstance(ids[0], str) or not ids[0].strip():
        return None, (f"plan item id {ids[0]!r} is not a non-empty string: "
                      f"text={plan_item.get('text')!r}")
    return ids[0], None


def justification_problems(declaration: ProofDeclaration, *, plan_item: Mapping[str, Any],
                           plan_digest: str) -> list[str]:
    """Why `declaration` is not supported by `plan_item`, or [] when it is.

    `plan_item` must be the item the requirement maps to; `fsck` looks it up
    from the requirement mapping, never from the declaration. One message per
    violated rule:

    1. the normalised quote occurs in the normalised item text as a whole-word,
       in-order run -- a fragment of a longer word ("base" out of "baseline"
       or out of "base-line": a hyphen, like every dash-like character in
       `WORD_JOINERS`, is part of a word), a quote whose
       leading or trailing punctuation splices into a word, a paraphrase or
       the same words reordered is prose, not the plan;
    2. `plan_item_id` is the item's id (`id`, alias `item_id`);
    3. `declared_against_plan_digest` is `plan_digest`;
    4. for `EVIDENCE_REMOVING_CLASSES`, the quote's words -- every
       `WORD_JOINERS` dash word-internal, so "base-line" and "base\u2010line"
       are one word and not "base" -- include a
       word of `EVIDENCE_REMOVAL_VOCABULARY`;
    5. the quote has at least `MIN_QUOTE_WORDS` words -- checked again here so
       an instance built without validation cannot skip it;
    6. the normalised quote is no longer than the normalised text of the item
       it is handed (the whole item quoted is the longest quote there is);
       `MAX_PLAN_ITEM_TEXT_CHARS` is only the static ceiling at construction.

    Never raises on a malformed plan item or an unvalidated declaration; those
    become problems.

    Known limits of rule 4, which is a word list, not a reading:

    * a negated quote such as "no schema here" still meets the burden. The
      quote is at least verbatim, so the negation is in the register for a
      reviewer to see;
    * plural forms ("contracts", "models") do not count as the vocabulary word;
    * hyphenated or dashed forms ("base-line", "data\u2013model") do not
      count either, because any dash-like character joins words.

    The last two refuse rather than admit: they fail closed.
    """
    problems: list[str] = []
    item_id, item_problem = _plan_item_id(plan_item)
    if item_problem:
        problems.append(item_problem)
    text = plan_item.get("text") if isinstance(plan_item, Mapping) else None
    if not isinstance(text, str):
        problems.append(f"plan item {item_id!r} has no text to quote from")
        text = ""

    quote = getattr(declaration, "quoted_plan_text", None)
    quote = normalise(quote) if isinstance(quote, str) else ""
    words = _quote_word_count(quote)
    if words < MIN_QUOTE_WORDS:
        problems.append(
            f"the quote has {words} word(s); at least {MIN_QUOTE_WORDS} are required")
    normalised_text = normalise(text)
    if len(quote) > len(normalised_text):
        problems.append(
            f"the quote is {len(quote)} characters, longer than the text of plan "
            f"item {item_id!r} ({len(normalised_text)} characters), so it cannot be "
            "a quote of that item")
    if not _occurs_on_word_boundaries(quote, normalised_text):
        problems.append(
            f"the quote {quote!r} does not occur verbatim, as whole words in order, "
            f"in the text of plan item {item_id!r}")

    declared_item = getattr(declaration, "plan_item_id", None)
    if item_id is not None and declared_item != item_id:
        problems.append(
            f"declared against plan item {declared_item!r}, but the requirement maps "
            f"to plan item {item_id!r}")

    declared_digest = getattr(declaration, "declared_against_plan_digest", None)
    if declared_digest != plan_digest:
        problems.append(
            f"declared against plan digest {declared_digest!r}, not the plan in hand "
            f"({plan_digest!r})")

    try:
        proof_class = ProofClass(getattr(declaration, "proof_class", None))
    except (ValueError, TypeError):
        problems.append(
            f"unknown proof class {getattr(declaration, 'proof_class', None)!r}")
    else:
        if (proof_class in EVIDENCE_REMOVING_CLASSES
                and EVIDENCE_REMOVAL_VOCABULARY.isdisjoint(_JOINED_WORD.findall(quote))):
            problems.append(
                f"{proof_class.value} removes an evidence requirement, so its quote must "
                f"contain at least one of the words "
                f"{', '.join(sorted(EVIDENCE_REMOVAL_VOCABULARY))}; it contains none")
    return problems


def assignment_is_a_relaxation(previous: ProofClass, current: ProofClass) -> bool:
    """True when moving a requirement from `previous` to `current` lowers the bar.

    A reclassification that makes a capability easier to prove is exactly the
    move that would launder an overclaim, so it must be visible as that rather
    than as a routine register edit. The rule:

    * any move from a class in `RAISES_THE_BAR` to one in `LOWERS_THE_BAR`, and
    * any move out of `SUBSTITUTE` -- a substitute is capped at PARTIAL, and
      leaving the class lifts the cap.

    So the relaxing moves are exactly these eleven:

        INTERNAL_RUNTIME -> ARTEFACT, CONTROL_PLANE, BOUNDARY_CONTRACT
        COMPOSITE        -> ARTEFACT, CONTROL_PLANE, BOUNDARY_CONTRACT
        SUBSTITUTE       -> ARTEFACT, CONTROL_PLANE, BOUNDARY_CONTRACT,
                            INTERNAL_RUNTIME, COMPOSITE

    A no-op and every other move (e.g. COMPOSITE -> SUBSTITUTE, ARTEFACT ->
    INTERNAL_RUNTIME, ARTEFACT -> CONTROL_PLANE) is not a relaxation. An unknown
    class raises ValueError: an unknown class is never a default.
    """
    previous, current = ProofClass(previous), ProofClass(current)
    return bool(
        (previous in RAISES_THE_BAR and current in LOWERS_THE_BAR)
        or (previous is ProofClass.SUBSTITUTE and current is not ProofClass.SUBSTITUTE))


def fsck(declarations: Iterable[ProofDeclaration], *, plan_items: Iterable[Mapping[str, Any]],
         plan_digest: str, capability_ids: Iterable[str],
         requirement_plan_items: Mapping[str, str]) -> list[str]:
    """Every structural problem of a set of declarations, or [].

    `plan_items` is the inventory's `items` list (each item read by `id`, alias
    `item_id`); `requirement_plan_items` maps a requirement id to the plan item
    it belongs to. The plan item a declaration is justified against is the
    *mapped* one: a declaration that names a different item is refused, so a
    requirement cannot borrow a convenient item's words.

    Reported, in this order, each group sorted:

    * a malformed argument, and each plan item without a readable id (named by
      its text) or with an id another item already has;
    * each declaration that fails re-validation (`model_validate` of its dump),
      so an instance built with `model_construct` cannot pass with an empty
      `why` or a missing field;
    * each capability with no valid declaration -- an unknown proof class is a
      failure, never a default;
    * each requirement declared more than once;
    * per declaration: a requirement that is not a capability; one absent from
      `requirement_plan_items`; a declared or mapped plan item id not in
      `plan_items`; and every `justification_problems` message against the
      mapped item, prefixed with the requirement id.

    Pure: reads nothing from disk, mutates nothing, never raises on bad input.
    """
    structural: list[str] = []

    if isinstance(capability_ids, (str, bytes)) or not isinstance(capability_ids, Iterable):
        structural.append(
            f"capability_ids must be a collection of ids, not {capability_ids!r}")
        capabilities: set[str] = set()
    else:
        try:
            capabilities = set(capability_ids)
        except TypeError as exc:
            structural.append(f"capability_ids could not be read as a set of ids: {exc}")
            capabilities = set()

    if not isinstance(requirement_plan_items, Mapping):
        structural.append(
            f"requirement_plan_items must be a mapping, not {type(requirement_plan_items).__name__}")
        mapping: Mapping[str, str] = {}
    else:
        mapping = requirement_plan_items

    items_by_id: dict[str, Mapping[str, Any]] = {}
    item_problems: list[str] = []
    if isinstance(plan_items, (str, bytes, Mapping)) or not isinstance(plan_items, Iterable):
        structural.append(f"plan_items must be a list of plan items, not {plan_items!r}")
        plan_items = ()
    for item in plan_items:
        item_id, problem = _plan_item_id(item)
        if problem:
            item_problems.append(problem)
        elif item_id in items_by_id:
            item_problems.append(f"plan item id {item_id!r} occurs more than once")
        else:
            items_by_id[item_id] = item

    invalid: list[str] = []
    valid: list[ProofDeclaration] = []
    if isinstance(declarations, (str, bytes, Mapping)) or not isinstance(declarations, Iterable):
        structural.append(f"declarations must be a list of declarations, not {declarations!r}")
        declarations = ()
    for declaration in declarations:
        name = getattr(declaration, "requirement_id", None)
        try:
            if not isinstance(declaration, ProofDeclaration):
                raise TypeError(f"not a ProofDeclaration: {type(declaration).__name__}")
            valid.append(ProofDeclaration.model_validate(declaration.model_dump(warnings=False)))
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'model'}: {e['msg']}"
                               for e in exc.errors())
            invalid.append(f"{name!r}: declaration does not re-validate: {detail}")
        except Exception as exc:  # any failure to re-validate is a problem, never a crash
            detail = " ".join(str(exc).split())
            invalid.append(f"{name!r}: declaration does not re-validate: {detail}")

    counts = Counter(d.requirement_id for d in valid)
    missing = [f"{cap!r}: capability has no proof declaration"
               for cap in capabilities if cap not in counts]
    duplicates = [f"{req!r}: declared {n} times; one requirement has one declaration"
                  for req, n in counts.items() if n > 1]

    per_declaration: list[str] = []
    for d in valid:
        req = d.requirement_id
        if req not in capabilities:
            per_declaration.append(f"{req!r}: declared, but not a capability requirement")
        if d.plan_item_id not in items_by_id:
            per_declaration.append(
                f"{req!r}: declared against plan item {d.plan_item_id!r}, which is not "
                "in the plan")
        if req not in mapping:
            per_declaration.append(
                f"{req!r}: requirement is not in requirement_plan_items, so no plan "
                "item can justify it")
            continue
        mapped = mapping[req]
        if not isinstance(mapped, str) or mapped not in items_by_id:
            per_declaration.append(
                f"{req!r}: maps to plan item {mapped!r}, which is not in the plan")
            continue
        per_declaration.extend(
            f"{req!r}: {p}" for p in justification_problems(
                d, plan_item=items_by_id[mapped], plan_digest=plan_digest))

    return (structural + sorted(item_problems) + sorted(invalid) + sorted(missing, key=str)
            + sorted(duplicates) + sorted(per_declaration))
