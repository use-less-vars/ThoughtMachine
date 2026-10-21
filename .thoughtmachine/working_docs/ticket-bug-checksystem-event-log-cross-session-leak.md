# Ticket: bug/checksystem-event-log-cross-session-leak

**Filed:** Gen 47, 2026-10-21
**Verified at:** not recorded in the relay (pen-test ran in workspace lively-bear-27)
**Source:** gen-47 pen-test run — finding F17 relayed by the operator.
**Severity:** CRITICAL — cross-tenant content disclosure to any agent in any workspace.
**Status:** filed, not dispatched.
**Report cross-ref:** Findings F17–F22 were relayed by the operator from the gen-47 pen-test run; the report containing appendices A and B is NOT on this filesystem, so no line numbers are cited and none may be invented.

## Defect

`CheckSystem query='event_log'` returns 50 events drawn from **all** sessions on the machine — unfiltered and unredacted. Observed from a workspace-scoped agent:

- the operator's verbatim prompt text for the pen-test task
- another session's `ContainerExecTool` invocation running pytest inside a container named `emfit-build`
- foreign session IDs
- foreign token counters

Any agent in any workspace can read another tenant's working content by asking.

## Relationship to the existing ticket (not a duplicate)

`ticket-bug-event-log-global-no-workspace-scope.md` (Gen 44) already diagnosed the **cause**: `agent/logging/event_logger.py::_on_event` drops `session_id` and `workspace_id` at **write** time, so no read-side filter can be both complete and non-leaking, and it prescribes the writer-side fix. That ticket is filed `Low-Medium — cross-workspace event visibility`, framed as a scoping/impossibility abstract.

This ticket adds what that one predicted but could not demonstrate: **real prompt bodies and real tool-invocation bodies cross the boundary**. The Gen 44 ticket rated the exposure by the record *shape* (no ownership field → filtering impossible); the observation shows the record *contents* are conversational and operational working material. That moves severity from Low-Medium to CRITICAL, and it raises a question the Gen 44 ticket did not need to answer — once records are attributable, should the bodies be in the agent-readable tail at all?

## Fix shape (not written)

Two halves, in order.

**(i) Writer-side ownership field** at `event_logger.py::_on_event`, populated from the producer's context. **This is a prerequisite for (ii), not an alternative to it** — with no attribution field, every read filter is either incomplete (leaks) or lossy (caller loses its own non-container history).

**(ii) Redaction policy decision.** Either log only event type + metadata (never `user_query` / `tool_result` / `turn` bodies in an agent-readable tail), or keep the bodies but scope strictly to the caller's `session_id`. The first is safer and loses diagnostic value to agents; the second preserves value and makes correctness depend on (i) being right everywhere.

## Bearing

Trust-boundary read integrity, not a security-boundary breach: no credentials, no host escape, read-only. The operator's framing — payloads scoped for the operator's eyes and then allowlisted to agents without re-scoping — is the correct one, and it is the same root as `ticket-bug-checksystem-running-workers-global-leak.md` and `ticket-bug-checksystem-vault-status-issues-unscoped.md`.
