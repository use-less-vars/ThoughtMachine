# Ticket: bug/panel-actions-no-op

**Filed:** Gen 45, 2026-10-17, from the manual container protocol.
**Severity:** high — Fact 4 (Control) gap. Blocks container-topic close.
**Status:** recon complete. Root cause identified; fix wave dispatched.

## Root cause (recon at HEAD 586aa2df)

Panel POSTs record-keyed routes:
- `POST /api/workspace/{ws}/containers/{record_id}/action {action: stop|start|restart}`
- `PATCH /api/workspace/{ws}/containers/{record_id}/resources {mem_limit}`

Those handlers call `_resolve_actor` FIRST - body `actor`, else header
`X-Actor`, else 401 "actor required". The panel sends neither. So the
request is rejected before the permission gate, before the container
manager, before Docker. `dispatch` never checks `res.ok`, so the 401
is invisible to the operator.

Contrast the panel's own `load()` path in the same file: it DOES check
`res.ok` and surfaces errors via `setError`. The pattern exists.

The name-keyed curl routes (`POST /containers/{name}/stop`) work because
they have no actor gate and no permission gate.

## Modal copy

`confirmMessage()` in `ContainerListPanel.jsx` interpolates the verb, but
`SHARED_TITLE = "Stopping affects other sessions in this workspace."` is a
hard-coded constant appended regardless of verb. Hence "start X? Stopping
affects..."

## Finding

Panel's container action buttons are silent no-ops for all four mutating verbs:

| Verb | Target | Result |
|---|---|---|
| Stop | proto-test-1 (running) | agent confirms still running |
| Start | proto-test-1 (exited) | no change |
| Restart | proto-test-1 (exited) | no change |
| Resize | proto-test-1 | memory reverts to 512m on F5 |

Modal copy is action-agnostic and self-contradictory:
"Are you sure you want to start X? Stopping affects other sessions in
this workspace."

## Working reference

Agent tools and name-based curl endpoints work:
- `curl POST /api/workspace/{ws}/containers/{name}/stop` → `{"status":"stopped"}`
- Agent `ContainerStopTool` → `{"success": true, "status": "stopped"}`
- Agent `ContainerRemoveTool` on persistent without grain → clean refusal

## Main's fix ruling

One wave, one dispatch path for all four verbs. Shape:

1. Funnel the four action handlers through one dispatch path.
2. Every action checks `res.ok` and surfaces failure to the operator.
3. Every action refetches after success (the panel currently doesn't
   update state after an action).
4. Modal copy generated from the action, not hard-coded.

Same shape as the endpoint display funnel (#124).

## Folds in

`bug/panel-stop-no-feedback` — same fix.

## Reference

Diagnostic read spec: `.thoughtmachine/working_docs/panel-action-path-read.md`
Protocol evidence: `.thoughtmachine/working_docs/test-protocol-user-agent-container.md`
