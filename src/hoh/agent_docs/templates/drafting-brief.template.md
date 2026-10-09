# Drafting brief for specification drafters (binding)

## Safety boundary
<PASTE THE SAFETY RULES OF references/method.md, section 7, VERBATIM>
Your only writable place is <STRAND_DIR>. Make one archive copy of <REPO> at <PIN> there and run
everything in that copy. Use a base temporary directory inside a self-removing temporary directory.

## Method
- Re-measure every claim on the pin and label it PROVEN (measured or read at a location) or SUSPECTED.
- Write spec.md: Goal; Scope (exact files and functions); What to build; acceptance criteria as test
  ids or plain commands (no selections that pass when a test is missing, no own shell, no `..`, no
  absolute paths outside the arena), each with its measured baseline on the pin and the mutants it
  kills; a mutant table (at most ten) for the integration review; a note to the roles; the overlaps
  with the other strands.
- Measure every criterion through the product's runner under both isolation settings. A criterion
  that is red on the pin for a wrong reason is a defect of the specification.
- Prototype the change in your copy: criteria green, each named mutant killed. The prototype is evidence.

## Deliverables
spec.md; NOTES.md (commands, outputs, baselines, mutants); QUESTIONS.md (each open decision with a
recommendation). Reply in under 250 words: scope, criteria with baselines, mutants killed, open
questions, file overlaps, scratch size.

## Lean
No complete mutant tables, no full-suite comparisons, no repeated stress campaigns while drafting.
