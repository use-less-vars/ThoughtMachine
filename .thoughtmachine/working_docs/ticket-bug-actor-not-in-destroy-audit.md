# Ticket: bug/actor-not-in-destroy-audit

**Filed:** Gen 45, 2026-10-18, from operator question "why no 'removed by X' log?"
**Severity:** medium — auditability gap, not enforcement.
**Status:** filed.

## Finding

The three destroy sinks (lifecycle logger, CONTAINER_DESTROY audit,
CONTAINER_REMOVED event bus) log **what, when, and reason**. None logs
**who**.

Evidence from the field: `test-survive-l27` and `eng-test-11` were
destroyed, the log shows `container removed container_id=...` with
`session_id=-` (empty) — no actor. We cannot distinguish "operator
clicked Remove" from "agent tool called remove" from "a background
sweep did it" from the audit log alone.

## Fix shape

Thread `actor` through the destroy path:
- `manager.remove(container_id, ..., actor="operator"|"agent:session:...")`
- `CONTAINER_DESTROY | reason=... source=... actor=...`
- `CONTAINER_REMOVED` event payload gains `actor` field

Source of actor:
- HTTP routes: `_resolve_actor` (already extracted, currently attribution-only)
- Agent tool paths: `session_id` from the tool executor
- Background sweeps: `actor="system:sweep:<name>"`
- Internal reapers: `actor="system:<site>"`

## Why this matters

- Operator asked "who removed X?" — the answer requires docker inspect +
  record json + guesswork. Should be one grep.
- Distinguishing operator clicks from agent tools from sweep is the
  foundation for any future "audit: who deleted what" surface.
- The F2 wave added reason and source but not actor — this closes the gap.

## Reference

`infra/container_destroy_audit.py`, `web_ui/backend/server.py::_resolve_actor`.
