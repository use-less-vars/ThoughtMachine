# Ticket: bug/logging-config-unauthenticated-write

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass; findings drawn from `redteam-surface-complete.md` in this corpus.
**Severity:** HIGH
**Status:** draft; verified by read — the read is the code path, the request-on-the-wire is inferred, not observed — `UNRUN: no live session`.
**Sibling:** `ticket-bug-unauthenticated-routes-beyond-health` — pointer, not owner: that ticket is about which routes exist unauthenticated; this one is about a route whose authorised effect is to destroy evidence. The anti-forensics class is not named anywhere else in the corpus.

## Finding

`PUT /api/logging/config` sets the process-global log level and truncation limits. No caller identity, no ownership check, no origin check on the path.

## Bearing

The audit trail that this corpus — and every conclusion in the three companion documents — is reasoned from is itself unauthenticated-writable. A caller can lower the level to silence what would have been recorded, or shrink the retained window so that what was recorded is gone. This is a constraint on the evidence base, not merely a confidentiality or integrity defect: it bounds what any incident review downstream can establish.

## Blast radius

Every security event the server would otherwise record, suppressible or truncatable by any client that can reach the port. Applies to the whole deployment, since the setting is process-global rather than per-workspace.

## What this is not

- Not a scoping defect — it is global by design, which is the problem.
- Not the same as `ticket-bug-unauthenticated-routes-beyond-health` in class: that ticket is about which routes exist unauthenticated, this one is about a route whose authorised effect is to destroy evidence.

## Fix shape

Caller identity on the route, an audit entry for every mutation of the logging configuration that is itself unaffected by the level being set, and a floor on retention that a request cannot lower.
