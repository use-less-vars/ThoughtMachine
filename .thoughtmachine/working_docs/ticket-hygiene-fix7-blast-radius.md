# Ticket: hygiene/fix7-blast-radius

**Filed:** Gen 45, 2026-10-17, from the P0 fixes wave.
**Severity:** low — behaviour change to audit and test.
**Status:** filed.

## Finding

Fix 7 (`free_use ⇒ PERSISTENT` for record-less containers) touches three
read sites:
- `security/admission_gate.py:252` (`_counts_toward_ceiling`)
- `infra/container_manager.py:810` (`_counts_toward_limit`)
- `container_manager.py:4882` (class helper feeding `_gc_should_skip` and
  the sweeps)

Behaviour: record-less free-use containers now consume a ceiling slot and
classify persistent in GC/sweep. Natively created containers unaffected
(`start` stamps `thoughtmachine.container_id`).

## Verify

Run the admission + GC/sweep suites, confirm the three sites agree on the
new classification. Test additions if gaps exist.
