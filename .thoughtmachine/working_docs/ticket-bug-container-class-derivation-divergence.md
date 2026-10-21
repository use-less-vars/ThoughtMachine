# Ticket: bug/container-class-derivation-divergence

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** Wave 9 read-first (spare agent).
**Severity:** Medium — Inv.6 violation. Feeds 9a.
**Status:** filed, not dispatched.

## Defect

Two functions compute "what class is this container" with different
answers:

- `_derive_class_owner` maps `container_type == "free_use"` → `EPHEMERAL`.
- `lifecycle_class_from_labels` never consults `container_type` at all,
  defaults to `PERSISTENT`.

**Inv.6 violation**: two functions compute the same value with different
derivations. No shared implementation.

## Consequence

Any backfilled or pre-`3179ebc6` free-use record sits on a 900s TTL —
i.e. in the wrong lifecycle state, more likely to be caught by 9a's
envelope-mismatch path. This is the *root* that made 9a fire often.

## Fix shape (not written)

Unify the derivation. One owner for "what class is this container."
Enumerate every caller. Do not touch the class taxonomy itself — the
fix is shared implementation, not new rules.

