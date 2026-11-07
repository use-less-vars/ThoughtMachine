# Ticket: bug/checksystem-running-workers-global-leak

**Filed:** Gen 47, 2026-10-21
**Verified at:** not recorded in the relay (pen-test ran in workspace lively-bear-27)
**Source:** gen-47 pen-test run — finding F18 relayed by the operator.
**Severity:** CRITICAL — cross-tenant task-content disclosure to any agent.
**Status:** filed, not dispatched.
**Report cross-ref:** Findings F17–F22 were relayed by the operator from the gen-47 pen-test run; the report containing appendices A and B is NOT on this filesystem, so no line numbers are cited and none may be invented.

## Defect

`CheckSystem query='running_workers'` is machine-global, not session-scoped. Observed: two workers listed, **neither belonging to the calling agent's session**; one exposed another project's current task text verbatim — a TeX path, `docs/report/sec_outlook.tex`, together with its line range.

That is the live work-in-progress of a different project, readable by any agent that asks.

## Fix shape (not written)

Two changes, both small:

1. Filter the listing to the caller's `session_id`.
2. For any worker that is still returned, drop `current_task`. Task text is not needed to reason about capacity, which is the only legitimate use of this payload.

The operator's reading stands: this is a one-line filter, and it is therefore **the cheapest CRITICAL close available**. The ticket says so deliberately — the fix is small and the residual risk of leaving it is not, which is exactly the combination that gets deferred indefinitely unless it is written down.

## Bearing

Same family as `ticket-bug-checksystem-event-log-cross-session-leak.md` and `ticket-bug-checksystem-vault-status-issues-unscoped.md`: `CheckSystem` payloads were scoped for the operator's eyes and then allowlisted to agents without re-scoping. Each of the three needs the same test — for every field in every agent-reachable payload, "is this the caller's own data?" — and none of them has one.
