# Ticket: bug/checksystem-containers-denied-by-allowlist

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** sub-finding of bug/container-turn-start-reap-kills-running
**Status:** filed, not dispatched.

## Defect

CheckSystem query='containers' is denied by the workspace allowlist. The agent cannot inspect container state — the exact diagnostic needed for the container feature. Inv.5 violation: agent cannot inspect the mechanism that refuses it. May fold into the broader Inv.5 work; placement is Infra's call. Surfaced by bug/container-turn-start-reap-kills-running.

## Bearing

Filed alongside the primary ticket. Not to be dispatched until the Wave 9
read-first (turn-start reap) returns — the primary read may reshape these.

## Amendment 2026-10-20 (gen-47 run)

**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F9 (line 368); run evidence §Step 9 (line 273), §Step 10 (line 306).

The split is unguessable by construction, not merely by accident. `CheckSystem query='runtime_state'` reports `"allowlist_count": 14` but never enumerates the members — it publishes the *cardinality* of the policy and not the policy — while `event_log` is allowed and `version` and `containers` are denied (live-verified 2026-10-20 on `124477b7`: `version` → `"Query \"version\" not allowed by system configuration"`, `status: denied`).

Fix shape: the denial should enumerate what IS allowed, and/or `runtime_state` should expose the list rather than its length. Otherwise every future protocol step rediscovers this from scratch by trial, and the operator has no way to answer "is query X permitted?" without running it.
