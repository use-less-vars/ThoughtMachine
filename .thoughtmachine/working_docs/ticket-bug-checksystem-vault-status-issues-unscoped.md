# Ticket: bug/checksystem-vault-status-issues-unscoped

**Filed:** Gen 47, 2026-10-21
**Verified at:** not recorded in the relay (pen-test ran in workspace lively-bear-27)
**Source:** gen-47 pen-test run — finding F19 relayed by the operator.
**Severity:** HIGH — foreign workspace identifiers disclosed to any agent.
**Status:** filed, not dispatched.
**Report cross-ref:** Findings F17–F22 were relayed by the operator from the gen-47 pen-test run; the report containing appendices A and B is NOT on this filesystem, so no line numbers are cited and none may be invented.

## Defect

In the `CheckSystem query='vault_status'` payload, `files` honours the `scoped_to` filter correctly, but the **`issues[]` array does not** — it listed 14 foreign workspace IDs. Names only: no file content, no drift detail.

## Incomplete-fix follow-up, not a new discovery

`ticket-bug-checksystem-vault-status-cross-tenant-leak.md` (Gen 44) is marked **SHIPPED — PR #119**, and its recommended fix was "scope `vault_status` to the caller's own workspace". This finding shows that fix was **applied to `files` only and missed `issues[]`**. The shipped ticket is the place the remainder was expected to be handled; it closed with a partially applied recommendation.

That is the informative part of this ticket: not the 14 identifiers, but that a payload was declared scoped while one of its fields was not. Same `displayed ≠ enforced` pattern the corpus tracks elsewhere, one layer up — in the *evidence* rather than the machinery.

## Fix shape (not written)

1. Apply the same caller-workspace filter to `issues[]`.
2. Separate question, one paragraph, needs an operator decision: the payload advertises `check(apply_repairs=True)` as a backfill of foreign configs, while the repair query itself is denied and repairs are operator-panel only. So the affordance is **disclosure plus misleading advertisement**, not escalation. Should `apply_repairs` be removed from the agent-facing payload entirely?

## Bearing

Names-only disclosure — lower than F17/F18, higher than a hygiene note, because tenant identifiers are the join key for the content leaks above. Sits with `ticket-bug-checksystem-event-log-cross-session-leak.md` and `ticket-bug-checksystem-running-workers-global-leak.md` as one re-scoping pass.
