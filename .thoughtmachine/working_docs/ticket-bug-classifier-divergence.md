# Ticket: bug/classifier-divergence

**Filed:** Gen 45, 2026-10-17, from P0 fixes wave.
**Severity:** medium — Inv.6 duplicated derivation.
**Status:** filed; ships as follow-up wave, not blocking container close.
**Renamed from:** `ticket-decide-migration-freeuse-class` per Main's
2026-10-17 ruling (it's a fix, not a decision).

## The defect

`_derive_class_owner` (migration.py) and `lifecycle_class_from_labels`
(api.py) compute the same fact with different implementations.

- `lifecycle_class_from_labels` was fixed in #128 (dropped
  `free_use ⇒ EPHEMERAL` for record-less containers).
- `_derive_class_owner` still maps `free_use ⇒ EPHEMERAL` when
  **synthesising** a record from scratch.

Since the synthesised record's own class wins, migrated free-use
containers still escape the ceiling count. That is Inv.6: two functions
computing the same value must share implementation.

## The fix

`_derive_class_owner` delegates to `lifecycle_class_from_labels`. One
implementation, one answer.

**Log the reclassification** when a synthesised record changes class
(was EPHEMERAL, now PERSISTENT). Not a policy guard — visibility so the
operator can see which containers moved and which workspaces went over
ceiling. Same shape as the lifecycle events.

## The "triggers ceiling on next create" risk — not a risk

If a workspace actually has 5 persistent containers and the ceiling is 5,
creating a 6th *should* refuse. The workspace shows correctly in the
panel. New creates refuse until cleanup. That's the state the ceiling
exists to enforce. "The workspace now shows as full" is not a problem —
it's the ceiling working.

## On pre-existing records

No bulk migration wave. Records that already carry a class are unchanged.
When a record is synthesised in the future (drift recovery, orphan
rebuild), it uses the corrected classifier. As records age out, they're
naturally retired. The correction propagates through synthesis, not
through a bulk rewrite.

## Reference

`thoughtmachine/container_record/migration.py:123-132`,
`thoughtmachine/container_record/api.py:61-106`,
`tests/test_lifecycle_policy.py:224-244` (currently pins the fail-open).
