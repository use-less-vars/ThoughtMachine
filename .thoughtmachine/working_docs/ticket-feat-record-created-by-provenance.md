# Ticket: feat/record-created-by-provenance

**Filed:** Gen 45, 2026-10-18, from wave 3 created_by ruling.
**Severity:** low — provenance across recreates.
**Status:** filed; needs schema bump + migration.

## Finding

Wave 3 (`8f967491`) populated `created_by` as a Docker label
(`thoughtmachine.created_by`), not a Record field. Trade-off accepted:

- **Label** = "who owns the container I'm looking at right now" (re-stamped
  on every restart, so a container an agent created and the operator later
  resized reads "operator", not "agent:session:...").
- **Record field** = "who originally created this record" (persists across
  recreates, needs `SCHEMA_VERSION_CURRENT` 5→6 + migration + three pinned
  schema assertions to update).

The label is correct for the destroy audit (who to blame for *this*
container). The Record field is needed if we ever want "who first
created the container that later drifted into a bad state?" — a
provenance question across recreates.

## When to build

Only if a real question needs it. Candidates: post-mortem on field bugs,
audit reports that need original-creator attribution, or if V3's
resource-first model needs creator-attribution independent of runtime
lifecycle.

## Reference

Wave 3 report `8f967491`; `bug/actor-not-in-destroy-audit` shares the root.
