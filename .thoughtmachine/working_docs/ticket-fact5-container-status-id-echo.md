# Ticket: fact5-container-status-id-echo

**Filed:** Gen 45, 2026-10-17, from the manual container protocol.
**Severity:** low — Fact 5 (Observability) gap.
**Status:** filed.

## Finding

`ContainerStatusTool.container_id` echoes the caller's input string
(e.g. `"proto-test-1"`), not the real 64-char hex ID. Only
`ContainerListTool` surfaces the real hex.

Consequence: `ContainerStatusTool` cannot be used to verify identity —
which is the criterion the `repro-survive-1` note uses for detecting
silent recreate ("a different id means silent recreate").

## Fix

Return the real hex ID from the record or daemon state. The record has
it (`docker_id` field); surface it as `container_id` or add a
`docker_id` sibling field.
