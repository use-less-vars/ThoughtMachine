# Ticket: bug/event-log-global-no-workspace-scope (RE-SCOPED)

**Filed:** Gen 44, 2026-10-15. **Re-scoped:** 2026-10-16 after Wave HIGH Phase 3.
**Severity:** Low-Medium — cross-workspace event visibility.
**Status:** filed, not dispatched. **Writer-side fix.**
**Prior shape (wrong):** "scope the log read to the caller's workspace." Verified
impossible at read time.

**Update (2026-10-31):** the "`session_id` is dropped at write time / zero occurrences of
`session_id` or `workspace_id` in the entire file" claim below is **stale**.
`agent/logging/event_logger.py:162-169` now writes both keys at the top level of every
record — `session_id` from `EventMetadata.session_id`, and `workspace_id` present but
always `None` (the field this ticket asks for still does not exist on `EventMetadata`).
So the read-side filter has a session key today; the writer-side **workspace**
attribution this ticket prescribes is still missing. See
`ticket-test-checksystem-event-log-session-workspace-conflation.md`.

**Update (2026-10-31, sequencing):** two things a reader needs before dispatching this.

- **It is a SCHEMA change, so it needs its own wave.** Adding a real `workspace_id` to `EventMetadata` changes
  the record format of every line in the shared `event_log.jsonl` — the one file every workspace appends to.
  Old lines carry no such key (and today's carry `workspace_id: None`, `event_logger.py:162-169`), so every
  reader must tolerate both shapes, and with 5 MB × 1 backup rotation (`event_logger.py:81-90`) mixed
  generations coexist on disk for as long as the rotation window lasts. It cannot ride along inside a security
  fix.
- **It is NOT a security urgency.** The read path is already fail-closed by *session*: `check_system.py:1098-1105`
  refuses to serve anything without a session scope, and the per-record filter (`_event_belongs_to_session`,
  `:1030-1055`) drops every record that is not the caller's own. Nothing is exposed while this waits. The gap is
  that **workspace** isolation of the log cannot be claimed — not that something is leaking today. The
  alternative is equally legitimate: keep session-as-proxy and stop claiming workspace isolation of the event log
  in the docs. That is a decision, not a bug, and it should be made explicitly rather than by leaving this ticket
  open indefinitely.

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
