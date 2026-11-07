# bug/container-type-label-vocabulary-collides-with-lifecycle

Correction to `bug/container-type-label-vs-lifecycle-class.md`. The label is not
written wrong and the class is not drifting: **two different vocabularies share
the string `free_use`,** and one of them reads like the other's answer.

## The label is a TYPE, not a lifecycle statement

Label values ever written: only `free_use` and `resource`
(`agent/config/defaults.py:129-130`). Write sites:
- `infra/container_registry.py:513` -> `free_use`
- `infra/container_manager.py:2205` -> `free_use`
- `infra/containers/executor.py:1045` -> `free_use`
- `infra/container_registry.py:699`, `infra/resource_container_manager.py:952` -> `resource`

The record's own `container_type` field uses a DIFFERENT vocabulary:
`("user", "resource", "mcp", "proxy")` (`agent/config/defaults.py:140`). So the
label `thoughtmachine.container_type: free_use` says "this is a free_use-kind
container", not "this container is free to use / reclaimable". Reading it as a
lifecycle claim is the natural mistake, and the field agent made exactly it.

## Lifecycle is derived elsewhere, and fail-closed

`thoughtmachine/container_record/api.py:60-97 lifecycle_class_from_labels()`:
resource-like (RESOURCE_LABEL / `tm-res-` name prefix / resource image) ->
`LIFECYCLE_RESOURCE`; a container carrying `RECORD_LABEL_KEY` whose record
declares a non-empty `lifecycle_class` -> that class; **anything else —
including a record-less container whose label says `free_use` — is
`LIFECYCLE_PERSISTENT`**. The record's `lifecycle_class` is always an explicit
`record_creation(lifecycle_class=...)` argument
(`thoughtmachine/container_record/hook.py:247-299`), never derived from the
label. `ContainerManager.start()` defaults it to `LIFECYCLE_PERSISTENT`
(`infra/container_manager.py:1406`) while stamping label `free_use` (`:2205`).
So label and record disagree by construction for every ordinary container.

## The genuine two-authority divergence

Same input label `free_use`, opposite verdicts:
- `thoughtmachine/container_record/migration.py:117-132 _derive_class_owner()`
  -> `LIFECYCLE_EPHEMERAL` (does NOT consume a slot)
- `thoughtmachine/container_record/api.py:60-97 lifecycle_class_from_labels()`
  -> `LIFECYCLE_PERSISTENT` (consumes a slot)

Only the second is on the admission path. Anything reasoning through the
migration helper will believe a container is reclaimable that the ceiling
treats as permanent — the disagreement is silent and in the permissive
direction, which is the wrong direction to be wrong in.

## Fix shape

1. Rename the label value (`free_use` -> `user`) so a type word cannot be read
   as a lifecycle verdict. Cheap, and it removes the trap rather than
documenting it.
2. One authority for class: delete or align `migration.py:130`, so no code path
   can conclude EPHEMERAL from a label the ceiling reads as PERSISTENT.
3. When the gate refuses on a class that came from the fail-closed DEFAULT
   rather than from a record, say so in the message. "Persistent" that means
   "we could not find a record so we assumed the strict case" is a different
   fact from "the record says persistent", and it is actionable in a different
   way.

**Filed by:** Gen 46, 2026-10-20, after a source read of the allocator.
