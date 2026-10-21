# Ticket: bug/container-turn-start-reap-kills-running

**Filed:** Gen 43, 2026-10-14 at `d378c206`
**Source:** field report from a live agent session (workspace `lively-bear-27`),
not a dispatched wave. Verified by direct reproduction.
**Severity:** High — silent, irreversible loss of in-flight work.
**Status:** CONFIRMED, reproduced 2/2.

## Defect

At each turn start, the runtime reaps **running** `free_use` containers in
the workspace. Exited containers are left as recoverable tombstones.

The trigger is the incoming user query. Therefore **the act of asking for a
job's results destroys the job.**

## Evidence

Two independent runs, two different budgets, both killed within ~1s before
the next user query:

| Run | Budget | Ran for | Lost | Delta to next query |
|---|---|---|---|---|
| 1 | 1800s | 91.70s | 95% | 0.567s |
| 3 | 3600s | 56.10s | 98.4% | 0.778s |

Both output files ended on a heartbeat (no `END`, no `ABORT`) — external
kill, not completion or crash.

Control: `eng-test-2` (exited) survived **both** sweeps. The running/exited
selectivity held twice.

## Environment

- Image `agent-executor:latest`
- build_hash `eac432c7edbf1abca8fc9538a147af4a8de78a7492df6e4cf856edde9a815664`
- Workspace `lively-bear-27`
- Container labels observed: `free_use` type; **no session label**

## Sub-findings (separate bugs, same report)

1. **Two teardown paths, opposite destructiveness.** Auto-sweep → `404`
   (`docker rm`, unrecoverable). Explicit `ContainerStopTool` → `exited`
   (`docker stop`, recoverable, listed, note intact). Backwards.
2. **Zero audit trail** — no `container_created`, no `container_removed`
   in the event log, including across the turn where a running container
   was destroyed. This actively misled diagnosis in v1 of the report.
3. **Diagnostic surface blocked.** `CheckSystem query='containers'` is
   denied by the workspace allowlist — the exact diagnostic needed for
   this feature is unavailable to the agent.

## Suggested fixes (agent-proposed; not yet ruled)

1. Guard the sweep — don't reap a container with live work / recent activity.
2. Auto-teardown should `stop`, not `rm`.
3. Session-label containers so the reaper can be session-keyed.
4. Emit lifecycle events on create and on every teardown, with reason.
5. Unblock `CheckSystem query='containers'`.
6. Publish the contract if cross-turn persistence is unsupported.

## Bearing

Wave 9 (friend fix) territory — container-lifecycle / Delivery / Layer 5.
Not yet established as *the* friend incident. Mandatory input to Wave 9's
read-first. Related to but distinct from `bug/container-quota-two-counters-
one-fact` and `bug/class-of-degradation-defeats-session-filter`.

## Report

Full report (v3, final) in the session transcript dated 2026-10-14. Not
yet canonicalised to a path under `.thoughtmachine/reports/` — see
operator's next move.

## Not applicable

- Adversarial verify: not applicable — empirical reproduction, not a claim.
- Dispatch wave: field report, not dispatched.
