# Ticket: bug/session-workspace-attribution-unresolved

**Filed:** 2026-10-23.
**Verified at:** dev @ 9ef9cfd0. Every line number below is a first-hand read of this commit: `session/models.py`, `session/store.py`, `web_ui/backend/server.py`, `web_ui/backend/global_routes.py`, `web_ui/backend/session_routes.py`, `web_ui/frontend/src/App.jsx`, `web_ui/frontend/src/components/GlobalSessions.jsx`, `web_ui/frontend/src/components/WorkspaceSelector.jsx`, `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx`.
**Severity:** **HIGH** — sessions that predate workspace-scoped storage are invisible in every per-workspace view and are silently dropped from the tab strip, and there is no path that re-attributes them.
**Status:** filed, not dispatched. No fix on dev.

## One sentence

A session has no authoritative workspace if it predates workspace-scoped storage, and there is no migration.

## The three faces of it

### (a) The one surface that shows everything at once is `GlobalSessions.jsx`

The grouped list buckets rows purely on the field the server sent:

```js
const wsId = s.workspace_id
if (!groups.has(wsId)) groups.set(wsId, [])
groups.get(wsId).push(s)
```
* `web_ui/frontend/src/components/GlobalSessions.jsx:23-25` — grouping key is `s.workspace_id` verbatim.
* `GlobalSessions.jsx:32` — `const title = (ws && ws.label) || wsId || 'Unknown workspace'`; an empty id therefore renders one heading, **`'Unknown workspace'`**, over an arbitrary number of unrelated sessions.

Its data never touches the session store, so nothing about *where a session file lives* can rescue the grouping: `web_ui/backend/global_routes.py:229-239` builds the list from `SessionRegistry.get_default().get_all()` filtered on `is_open`, and `:286-298` emits `"workspace_id": s.get("workspace_id", "")`. The frontend reads it at `web_ui/frontend/src/components/WorkspaceSelector.jsx:119` and renders it at `:272`.

### (b) The tab-strip fix drops unattributed sessions by design

`web_ui/frontend/src/App.jsx:453-457`:

```js
const ws = s.workspace_id || knownWs || sessionWorkspacesRef.current[s.session_id]
// FAIL CLOSED. Guessing "whatever workspace is on screen" (the old
// lastKnownWorkspaceRef || currentWsRef tail) is exactly what piled
// every workspace's open sessions into a single strip.
if (!ws) return
```

An empty `workspace_id` is falsy *and* poisons the first fallback, so an unattributed session is dropped rather than shown under a guess. The two fallbacks that could still rescue it do not fire: `knownWs` comes from the `sessions_list` mapping (`App.jsx:452`, learned at `:379`), and `list_sessions` refuses to answer a socket whose workspace cannot be bound — see `web_ui/backend/server.py:2239-2252` (`_ls_scope = bridge.workspace_id if bridge is not None else None`; `if not _ls_scope:` → `{"sessions": []}` + "this connection is not bound to a workspace yet"). The hub socket is exactly such a socket. Meanwhile the server has already flattened "unknown" to the empty string: `server.py:2915` `"workspace_id": meta.get("workspace_id") or ""`.

The user-visible result is not a misfiled tab but a missing one: a session that is open on the server can be absent from the strip after restore, with no message. (Tabs already persisted in `localStorage` survive; it is the server-driven restore/merge that drops them.)

### (c) `rebind_workspace` exists and is wired only for the live path

`session/models.py:444` defines `Session.rebind_workspace(workspace_id, *, reason="")`, documented as "the ONLY sanctioned way to move a session between workspaces" (it bypasses the immutability guard set in `__setattr__` at `models.py:185-192`).

It has exactly one call site: `web_ui/backend/server.py:1735`, inside the `apply_config` handler —

```python
existing_session.rebind_workspace(workspace_id, reason="apply_config workspace switch")
```

i.e. the capability to re-attribute was built, but only for a **live, in-memory** session whose tab switches workspace. No code path calls it for a session that is already stored without attribution, and there is no backfill. That is the gap: the verb exists, the migration does not.

## Root cause

A session's workspace is whatever directory it was found in — `session/store.py:578-590` pairs each candidate directory with the workspace it encodes (`candidates.append((self.sessions_dir, None))` at `:584` for the legacy flat dir; `(ws_sessions, ws_dir.name)` at `:590` for workspace-scoped ones) and stamps it at `:618` (`'workspace_id': dir_workspace_id`). Sessions written before workspace-scoped storage therefore have **no** workspace, only `None`, and no migration has ever assigned one.

Two compounding details:

1. **`None` is re-spelled as `""` at the wire.** `server.py:2915` (`meta.get("workspace_id") or ""`) and `global_routes.py:289` (`s.get("workspace_id", "")`).
2. **`""` is falsy, so every truthiness guard converts uncertainty into absence.** `App.jsx:457` drops the row; `GlobalSessions.jsx:32` files it under a catch-all heading; `server.py:2240` refuses on the same falsy test.

Also worth recording: the batch scan is first-match-wins and the legacy directory is scanned **first** (`store.py:584` before `:585-590`; guard `results.get(sid) is None` at `:606`), so a session that exists in both the flat dir and a workspace dir is attributed to legacy — i.e. to `""` — even though a correct answer was on disk.

## Overlap

`web_ui/backend/session_routes.py:206-234` (`GET /list`, `workspace_id: Optional[str] = Query(None)`): the registry is the source (`:217-219`), and when a workspace is supplied the rows are filtered at `:229` (`sessions = [s for s in sessions if s.get('workspace_id') == workspace_id]`) before the store is consulted with the same scope (`:234`, `load_sessions_metadata_batch(session_ids, workspace_id=workspace_id)`). A registry row with no `workspace_id` matches no workspace filter and is invisible in **every** per-workspace HTTP view.

This is fail-closed, not leaky — but the same missing attribution that hides a session also silently denies it. One field, two symptoms: the user cannot see their own session, and any scoped read behaves as if the session did not exist.

## What is NOT claimed

* **No data leakage.** No cross-workspace read was demonstrated or is implied here. Every guard inspected fails closed: `server.py:2240` (unscoped socket gets nothing), `App.jsx:457` (unresolved tab is dropped), `session_routes.py:229` (no workspace filter match, no row).
* **The failure mode is invisibility and denial**, in that order: sessions vanish from lists and are excluded from scoped queries; they are not exposed to the wrong workspace.
* **Not claimed:** that the workspace detail page shows foreign sessions. It has no session list at all — only a count: `web_ui/frontend/src/components/workspace/WorkspaceDetailPage.jsx:566` (`const activeSessions = summary?.active_sessions?.length || 0`), rendered at `:671` and `:757`. That count is computed server-side under a per-workspace filter, so it cannot over-report; if anything it under-reports for the same reason as everything else.
* **Not claimed:** any particular file list for the tab-strip fix; this ticket describes the code as it stands at `9ef9cfd0`.

## Relation to security/client-claim-surface-auth-model

That corpus (and its per-workspace isolation claims) assumes session→workspace attribution is **settled**: it asks whether a session's data can be reached across a workspace boundary, and answers using a mapping it takes as given. If the mapping can be absent, the premise is not established and the analysis has an unexamined hole. This ticket is therefore a **prerequisite** to that corpus, not a duplicate of it: fix attribution first, then the isolation argument has something to stand on. Filed separately so the dependency is visible rather than buried in a security write-up.

## Open questions

1. **Sentinel.** Should "unknown workspace" stay `""` on the wire, or become an explicit `null` / `"unattributed"` state that consumers must handle? As it stands, every layer independently re-derives "unknown" from falsiness, and every derivation fails silently in a different direction (dropped / catch-all heading / refused). A named state would make the condition observable in tests and in logs; `""` cannot be distinguished from "known to be empty" at any call site.
2. **`rebind_workspace`.** Wire it into a re-attribution path, or delete it? It is currently reachable only from `apply_config` (`server.py:1735`); keeping it without any backlog path invites the reasonable but wrong reading that re-attribution is possible.
3. **Migration.** Is a one-shot migration in scope, or is the right answer to leave legacy sessions visible only in the global view? *I would argue for a one-shot migration with an explicit audit line per moved session* — a session the user still owns should not disappear from their own workspace, and the global view is where it is currently piled under `'Unknown workspace'`, which is precisely the confusing surface. Second line: the alternative (leave them, document the global view) is defensible only if the drop at `App.jsx:457` is also made visible to the user; silently absent is not an acceptable steady state.

## Options sketch (not a decision)

Three shapes seen while reading, none approved: (i) attribute at read time by deriving the workspace from the registry entry that already carries one for live sessions, leaving storage untouched; (ii) attribute at rest with a one-shot backfill that calls `rebind_workspace` and saves into the workspace-scoped directory; (iii) keep storage as-is and make the unattributed state explicit end-to-end (`null`/named sentinel, an "unattributed" group, no silent drops). They differ in whether existing operator data moves, and in whether the fix can be a pure read-path change. Nothing here is a design — the open questions above decide the shape, and this ticket is a filing.

## Filed

Filed 2026-10-23 by the main agent. Base `dev @ 9ef9cfd0`. Evidence is first-hand file reads only. No reproduction test was written (backend paths require a live server plus a vault carrying pre-migration sessions in the legacy flat `sessions/` directory); the tab-strip drop and the `'Unknown workspace'` grouping are, however, directly readable in the cited lines.
