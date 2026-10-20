# Ticket: feat/container-resize-tool-for-agent

**Filed:** Gen 47, 2026-10-20 at `124477b7`
**Source:** gen-47 container-ops run (F6); code verified at `124477b7`
**Report cross-ref:** `.thoughtmachine/working_docs/report-gen47-container-ops-run-2026-10-20.md` §F6 under `## Findings` (line 349); run evidence `### Step 7` (line 145).
**Status:** filed, not dispatched.

## Defect

The agent tool surface exposes nine container verbs and no resize.

- Declared surface: `tools/container_control.py:208,341,432,484,579,779,823,876,956` — `tool: Literal[...]` = `ContainerStartTool`, `ContainerExecTool`, `ContainerStopTool`, `ContainerRestartTool`, `ContainerStatusTool`, `ContainerListTool`, `ContainerBuildTool`, `ContainerLogsTool`, `ContainerRemoveTool`; registered at `tools/__init__.py:152-170`.
- No resize verb in the manager either: `resize` appears in `infra/container_manager.py` only in comments (`:690`, `:1463`, `:5115`, `:5507`).
- The capability is operator-only: `PATCH /api/workspace/{workspace_id}/containers/{record_id}/resources` at `web_ui/backend/server.py:4746`, handler `workspace_container_resources` (`:4747-4943`).
- `mem_limit`/`cpu_quota` in the agent surface are create-time only: `tools/container_control.py:160`, `:233` (and `tools/docker_code_runner.py:165,400` for the code runner). Re-passing a different limit on an existing container is a no-op, because limits are applied only on the fresh-create branch: `infra/container_manager.py:1626-1640`, `:1756-1767` (`mem_limit=_reuse_mem_limit, cpu_quota=_reuse_cpu_quota` are consumed only by `_fresh_start`).

Consequence: protocol step 7 (resource change) could not be performed agent-alone and had to be driven by the operator through the panel route.

Fix shape: add `ContainerResizeTool` that delegates to the same recreate sequence the route uses — persist `intent_snapshot`, `remove(keep_record=True)`, `start(allow_fresh=True)` — so the two surfaces cannot drift. `allow_fresh` is operator-only by current policy (`infra/container_manager.py:1623` comment; `ContainerStartTool` deliberately does not expose it, `tools/container_control.py:271-276`), so the tool must sit behind the same write gate the route uses (`_container_write_allowed`, `web_ui/backend/server.py:4787-4789`).

## Bearing

Protocol step 7 is operator-only until this lands. It also constrains `ticket-bug-container-created-by-not-a-record-field.md`: a tool must reproduce `audit_reason="resize_recreate"` and `audit_source="server.workspace_container_resources"` (`web_ui/backend/server.py:4919-4923`) or its destroys will be indistinguishable from user removals in the audit trail.
