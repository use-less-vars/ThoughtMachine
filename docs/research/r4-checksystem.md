# Research: Phase 7 — CheckSystem Tool (r4)

## Status: ✅ Updated for Phase 7

### Summary

`CheckSystem` is the introspection tool for inspecting the agent's runtime environment — permissions, container status, workspace metadata, network diagnostics, and event bus configuration.

### Key Changes (Phase 7)

No direct changes to `CheckSystem` were made in Phase 7. However, the following related components were verified:

1. **Worker template simplification**: `CheckSystem` can still inspect worker definitions via `query='workers'` and `query='worker/<name>'`. With the consolidation to a single `default.json`, these queries now return only one template — but the API shape is unchanged.

2. **Tools tab locking**: In Agent/Engineer modes, the Tools tab in ConfigPanel now shows tool checkboxes as disabled with a lock banner (instead of hiding them). This does not affect `CheckSystem` — the tool continues to report available tools as before.

3. **System prompt consolidation**: The single consolidated `system_prompt` tab does not affect `CheckSystem`.

### Relevance

`CheckSystem` remains the reference tool for understanding the current environment. No migration required.
