# Ticket: bug/event-log-global-no-workspace-scope (RE-SCOPED)

**Filed:** Gen 44, 2026-10-15. **Re-scoped:** 2026-10-16 after Wave HIGH Phase 3.
**Severity:** Low-Medium — cross-workspace event visibility.
**Status:** filed, not dispatched. **Writer-side fix.**
**Prior shape (wrong):** "scope the log read to the caller's workspace." Verified
impossible at read time.

## The real finding

`agent/logging/event_logger.py::_on_event` (line ~142) writes records shaped:
`{timestamp, event_type, event_id, source, data}`.

- **`session_id` is dropped at write time.** Zero occurrences of `session_id`
  or `workspace_id` in the entire file.
- `metadata.session_id` is set by producers but not persisted.
- `conversation_id` never exists in the record.

Consequence: no read-time filter can be both complete and non-leaking.
Filter on nothing → the caller loses its own non-container history. Filter on
`data` → foreign `agent_start` prompts still leak.

## The fix (writer-side)

Add a top-level ownership field to the event record at write time:
`session_id` and/or `workspace_id`, populated from the event producer's
context. Then the read-time filter in `_query_event_log` becomes a
one-line `WHERE workspace_id = caller`.

This is a **shared-writer change** (event_logger.py + every producer that
should attribute events) and needs its own wave.

## Related

- The re-scoped ticket supersedes the original (a)/(b) framing.
- Cross-references `ticket-bug-checksystem-vault-status-cross-tenant-leak.md`
  (now shipped — read-side scoping worked there because vault_status reads a
  directory tree, not a shared log).

## MUST NOT TOUCH
Standard list.
