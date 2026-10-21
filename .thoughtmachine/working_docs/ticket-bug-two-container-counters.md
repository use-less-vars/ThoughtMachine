# Ticket: bug/two-container-counters-disagree

**Filed:** Gen 45, 2026-10-17, from admit() coverage read.
**Severity:** medium (Inv.6 — two functions computing the same value).
**Status:** filed; needs runtime test to confirm divergence.

## Finding

Two independent counters measure "how many containers this workspace is
consuming":
- `ContainerRegistry._count_consuming_slots`
- `admission_gate.workspace_container_count`

They may disagree (different queries, different predicates). Inv.6:
two functions computing the same value must share implementation.

Needs: a runtime test that constructs edge states (persistent + ephemeral
+ terminal-status mix) and asserts both counters return the same value,
or a spec that names them as two different facts (with distinct names).

## Reference

`.thoughtmachine/working_docs/admit-coverage-read.md` §C6
