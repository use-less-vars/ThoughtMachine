# Ticket: bug/worker-blueprint-prompt-write-unauthenticated

**Filed:** 2026-10-25, from the Gen 50 client-claim red team pass at `24012a89` (base commit; handler read in full).
**Severity:** **HIGH** — an unauthenticated caller can author the system prompt and tool set of any workspace's worker definition, honoured on later execution.
**Status:** open; verified by read at `24012a89`, not exercised at runtime — `UNRUN: no worker spawned from a modified blueprint`. Not to be fixed in this wave.

## Finding

`PATCH /api/workspace/{ws_id}/workers/blueprints/{name}` (`web_ui/backend/workspace_routes.py`, decorator `:738`, handler `:739-799`) merges a caller-supplied body over a stored worker definition and writes it back. The decisive line is `merged.update(body)`, which runs **last** — every key in the body overrides what was stored.

```python
merged = dict(existing) if isinstance(existing, dict) else {"name": existing}
merged.update(body)
updated = WorkerDefinition.model_validate(merged)
```

A template entry is appended when `name` is absent; the merged dict is validated into a `WorkerDefinition`; `raw[index] = updated.model_dump()` is persisted to `path = _workspace_dir(ws_id) / "workers.json"` by `_atomic_write_json(raw, path)`, after `ensure_workspace_dirs(ws_id)` when the parent is missing.

`system_prompt` is a **required** field of `WorkerDefinition` (`agent/models/worker_definition.py:35`), and its docstring (`:20-23`) states a worker's prompt "is required and is never inherited from the main agent". This endpoint is therefore the only way a worker's prompt comes into existence, and it accepts one from any caller. `tools` and `permission_footprint` are equally writable: the merge covers the whole definition, not a whitelist.

Absent: ownership predicate, origin or CSRF check (the only relevant middleware is `CORSMiddleware`, `web_ui/backend/server.py:1194`, `allow_credentials=False`; `origin_policy` at `:206` is used only for websockets, `:1247`), permission lookup, `confirmed` flag, registry membership check.

Present, and only this: the inherited workspace-id **shape** check via `_workspace_dir` — `thoughtmachine/workspace_capabilities.py:198-200` → `_validate_workspace_id` (`:190-196`) → `validate_single_path_segment(..., kind="workspace id")` (`thoughtmachine/validation.py:30`) — plus `RESERVED_WORKSPACE_IDS`. Shape validation confines `ws_id` to one directory; it says nothing about who may write there.

## Bearing

The write authors agent instructions, not inert data. A worker definition is the prompt and tool list a spawned worker runs under, so a successful `PATCH` is authorship of an agent's behaviour by a caller with no credential and no tie to the workspace. It is durable twice over: written atomically to disk, so it survives restart, and scoped per blueprint name, so it can be aimed at one worker while the rest of the definition stands. It is separable from the two rows beside it: the Dockerfile write needs `rebuild_container` (F7) to become execution, and `ticket-bug-prompt-injection-unauthenticated.md` (F12) writes the vault-global `custom_system_prompt.txt` read by `load_custom_system_prompt()`, applying to every session.

## Blast radius

Any client that can route to the port can author the prompt and tool list of a named blueprint in any workspace id it names. Because `ensure_workspace_dirs(ws_id)` runs when the parent is missing, the same request also **creates** the workspace tree. It persists across restart, and the sibling `GET` returns it.

## What this is not

- Not remote code execution: the write is a data write, and what an authored prompt or tool list can cause depends on the untraced spawn path — `UNRUN: no worker spawned from a modified blueprint`.
- Not a path-traversal finding: `ws_id` is shape-validated. Whether the `{name}` segment receives validation beyond that is **not established** — it was not traced, and this ticket asserts nothing either way. `UNRUN: {name} segment validation not traced`.
- Not a demonstrated exploit chain: no request reached a running backend; every claim rests on reading the handler in full.
- Not the same row as the Dockerfile or prompt writes: different handler, route and store, no shared code path.

## Fix shape

Not specified here. Any fix must bind the caller to the target workspace through the single authority the rest of the server should use, and must cover the whole merged definition rather than `system_prompt` alone — see `docs/design-input-auth-layer-prerequisite.md` for why a per-route check is not sufficient.
