# Ticket: hygiene/checksystem-payload-fingerprint

**Filed:** Gen 47, 2026-10-21
**Verified at:** not recorded in the relay (pen-test ran in workspace lively-bear-27)
**Source:** gen-47 pen-test run — finding F20 relayed by the operator.
**Severity:** LOW — judgement call, not a defect.
**Status:** filed, not dispatched.
**Report cross-ref:** Findings F17–F22 were relayed by the operator from the gen-47 pen-test run; the report containing appendices A and B is NOT on this filesystem, so no line numbers are cited and none may be invented.

## Finding

The `CheckSystem` payloads advertise `runtime_commit`, the model name, the sandbox image name, and `allowlist_count: 14`. Together these form a targeting kit: an agent can fingerprint the exact build it is running under and how many allowlist entries exist, without gaining anything it needs to do its work.

## Fix shape (not written)

Consider dropping these from the agent-facing payload, or returning them only to an operator-grain caller.

**Explicitly a judgement call, not a defect.** The value to an honest agent is marginal; the value to a confused one is nil; the value to a caller reasoning about the machine's misbehaviour is real, which is why `ticket-feat-checkSystem-runtime-version.md` added `runtime_commit` deliberately and records it as "the right instinct" (a container status that pins the runtime which described it). That is the tension this ticket names: the same field is diagnostic gold for the operator and unnecessary surface for the agent, and the current payload does not distinguish the two callers.

Do not close this by removing `runtime_commit` outright without re-reading that ticket.

## Bearing

Class: **payload minimalism**. Every field in an agent-reachable payload should have an answer to "what decision does the caller make with this?", and the answer for `allowlist_count` and the image name is currently "fingerprinting".
