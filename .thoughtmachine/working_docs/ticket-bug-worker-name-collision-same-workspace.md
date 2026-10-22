# bug/worker-name-collision-same-workspace

**Confirmed 2026-10-19 by field agent in fierce-owl-43:** two sessions in the
same workspace, both spawning worker named `default` with `force=True`, kill
each other's worker. Field agent worked around it by switching to `default2`.

**Open question:** does the same happen cross-workspace? Registry keying unknown.
If keyed by `(workspace_id, worker_name)`, no cross-workspace bug. If keyed by
`worker_name` alone, cross-workspace kill is live.

**Read first:**
1. Where is the worker registry? What's the key?
2. `WorkerThread.spawn(force=True)` — filter by workspace_id or not?
3. Test both scenarios.

**Fix shape:** registry keyed by `(workspace_id, worker_name)`. Same-workspace
same-name across sessions should either refuse or scope by `session_id` too.

**Filed by:** Gen 46, 2026-10-19.

## Sequential-session shadowing in `_worker_bus_subs` — **recorded, not fixed**

**Recorded 2026-10-22, NOT fixed** in `fix/worker-panel-payload-drop`: a second, narrower same-workspace symptom of the same session-blind identity, this one in the *bridge* rather than in the worker registry. `_worker_bus_subs` (`web_ui/backend/bridge.py:483`) is keyed by instance label only (`_worker_instance_label`, `:159-166`), never by session, so a bridge reused across two sessions in one workspace — `server.py:1703` and `:1763` rebind the same `WebAgentBridge` to a new session id with no `bridge.stop()` in between — carries session A's entry for worker `default` into session B. When session B spawns its own `default`, the payload-drop fix's re-bind logic cannot tell the two apart: the session guard at `bridge.py:836` compares the *event's* session id with `self._session_id`, and both are now session B, so it passes; `_worker_bus_is_stale` then resolves the registry using the **stored** session id, which is still A (`bridge.py:683` `session_id = entry.get('session_id') or self._session_id or ''`), and reports not-stale either way it can go — if session A's bus is still registered the object matches, and if it is gone `_stored_entry_is_stale` returns `False` by construction (`bridge.py:662-663`, `None` counts as "cannot tell, keep it"). The duplicate guard at `:853` therefore short-circuits `_on_worker_spawned` and returns: **no re-bind, no bounded retry, and no `worker:system_notification`** — the degradation notice only exists on the retry path, so it never fires — leaving session B's `default` silently attached to session A's dead bus. That is the same silent-loss shape as `ticket-bug-worker-panel-intermittent-missing-tool-calls.md`, one layer up: the worker panel shows nothing and nothing reports itself. *Simultaneous* sessions are **not** affected by this: nothing here is a singleton — each `WebAgentBridge` owns its own dict, the per-worker handlers built in `_make_handler` (`bridge.py:580-616`) are per-instance closures over `self`, and `create_bridge()` (`bridge.py:3055-3057`) is defined but never called — so two live sessions simply keep two independent maps. Candidate follow-up, **not addressed in this branch**: key `_worker_bus_subs` on `(session_id, label)`, or clear/re-scope it in `stop()`, in `close_session()` (`bridge.py:2532`, which reaches `:2579`) and on every server-side rebind path (`server.py:1703`, `:1763`).

