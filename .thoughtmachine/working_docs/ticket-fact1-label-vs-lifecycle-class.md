# Ticket: fact1-label-vs-lifecycle-class

**Filed:** Gen 45, 2026-10-17, from the manual container protocol.
**Severity:** Inv.1 (one owner per fact) — reconciliation, not a fix.
**Status:** filed; needs a read.

## Finding

The same container reports two different values for "what class is this":

- Docker label `thoughtmachine.container_type`: **`free_use`**
- `ContainerStatusTool.lifecycle_class`: **`persistent`**

Empirical result: the remove-gate refuses on `lifecycle_class=persistent`,
ignoring the `free_use` label. So `lifecycle_class` is authoritative for
gating. But the label is still present and different.

## Read needed

Which is authoritative for "what class is this container"? Then:
- Unify (label derives from lifecycle_class), or
- Drop the redundant label, or
- Establish them as two different facts with different names
  (`container_type` = purpose axis, `lifecycle_class` = lifetime axis,
  per contract §1 axes).

Contract §1 defines two orthogonal axes (purpose, lifetime). If the
label and the status field are answering those two different questions,
the fix is naming, not unification. If they're answering the same
question, Inv.1 violation.

## Reference

`design_container_lifecycle_contract.md` §1
