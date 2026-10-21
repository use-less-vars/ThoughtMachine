# Ticket: bug/session-delete-ignores-workspace-scope

**Filed:** 2026-10-21, from the session-persistence pass at `9d5ed869` (found while checking what `delete_session` does with the `workspace_id` its callers pass).
**Severity:** HIGH — `delete_session` accepts a `workspace_id`, is documented as "scoped deletion", and then searches every workspace in the vault and unlinks the first session whose id matches.
**Status:** open; verified by read at `9d5ed869`, not exercised at runtime. NOT blocking, NOT to be fixed in this wave.

## Finding

The scope argument is carried through four layers and dropped in the fifth.

REST entry point (`web_ui/backend/session_routes.py:296-308`):

```python
@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    workspace_id: Optional[str] = Query(None, description="Workspace ID for scoped deletion"),
    ...
) -> ...:
    """... Optionally accepts ``workspace_id`` for scoped deletion ..."""
    ...
    store.delete_session(session_id, workspace_id=workspace_id)
```

Manager (`web_ui/backend/session_manager.py:373-385`) declares and forwards it:

```python
def delete_session(self, session_id: str, workspace_id: Optional[str] = None) -> bool:
    ...
    return self._session_store.delete_session(session_id, workspace_id=workspace_id)
```

Store (`session/store.py:765`):

```python
def delete_session(self, session_id: str, workspace_id: Optional[str] = None) -> bool:
    """Delete a session file from ALL locations ... prevents session resurrection"""
```

and the body never mentions `workspace_id` again. It builds a candidate list that is deliberately *unscoped* (`session/store.py:782-789`):

```python
# Collect ALL candidate directories (legacy + every workspace)
candidates = [self.sessions_dir]
workspaces_root = self._base_dir / "workspaces"
if workspaces_root.exists():
    for ws_dir in workspaces_root.iterdir():
        ws_sessions = ws_dir / "sessions"
        if ws_sessions.is_dir():
            candidates.append(ws_sessions)
```

then walks them and unlinks the first content match (`session/store.py:794-805`), plus the metadata file (`:809-813`). So `DELETE /api/sessions/<id>?workspace_id=<A>` issued in workspace A deletes the session record that lives in workspace B. The docstring's stated purpose — avoid resurrection by removing *all* copies — is what makes the parameter unimplementable as written: an "all locations" implementation cannot also be a scoped one.

Two corroborations that the id, not the workspace, is the real key:

- The lightweight metadata file is addressed by id alone, in the legacy directory (`session/store.py:171-173`): `return self.sessions_dir / f"_meta_{session_id}.json"`. Deleting a record from another workspace therefore also removes that session's meta pointer.
- `list_sessions` *does* honour the scope (`session/store.py:496-506`: `if workspace_id: candidates = [self._base_dir / "workspaces" / workspace_id / "sessions"]`, else the global sweep), and `load_session` at least *prefers* the scoped path (`session/store.py:375-384`). `delete_session` is the single outlier of the three: it is the only one where the parameter has no effect at all.

The WebSocket path is worse in a second way — it cannot even deliver the scope. The handler reads it (`web_ui/backend/server.py:2380`: `workspace_id = msg.get("workspace_id")`) and passes it (`:2385`: `session_store.delete_session(session_id, workspace_id=workspace_id)`), but the bridge method in the middle drops it (`web_ui/backend/bridge.py:2102-2104`):

```python
def delete_session(self, session_id: str) -> bool:
    ...
    return self._session_manager.delete_session(session_id)
```

So the WS delete is unconditionally global even if a future fix made the store honour the argument, and the fix would silently not apply to the socket. The WS branch also tears down process-global state keyed by the same id (`server.py:2386` `remove_open_session`, `:2389` `registry.remove(session_id)`, `:2391` `_session_bridges.pop(session_id, None)`, `:2393-2394` `cached_bridge._cleanly_closed = True; cached_bridge.stop()`, `:2396` `_explicitly_closed_sessions.add`) — so a delete reaches the cached bridge and the session registry as well as the file.

## Why it matters

`delete_session` is destructive, irreversible, and gated only on knowing a session id. Workspace scoping is the only tenancy boundary the vault has for session storage (path shape `~/.thoughtmachine/workspaces/<ws_id>/sessions/`), and this is the one operation on that storage that ignores it — including for the copy in a workspace the caller has nothing to do with. The counter-argument ("ids are unguessable, so who can hit this") changes who can trigger it, not whether the enforcement exists; the same argument would retire every other scope check in the store, and the WS layer that delivers it is unauthenticated (see cross-references).

It also makes the parameter actively misleading: three layers advertise a scoped delete, and a caller who reads the signature (`session_routes.py:299`, `session_manager.py:373`) has no reason to suspect that passing it changes nothing. That is the failure mode where a future feature (delete-on-workspace-teardown, say) is built on a check that was never there.

## Disposition options

- **A. Implement the scope.** `candidates = [self._base_dir / "workspaces" / workspace_id / "sessions"]` when `workspace_id` is given, and keep the metadata unlink. Cheap, but it makes legacy (unscoped) sessions undeletable through the scoped route, and it contradicts the docstring's resurrection rationale for the cross-workspace copies.
- **B. Enforce, don't sweep.** Resolve the session once (as `load_session` does), compare its owning workspace against the requested one, and refuse with 404/409 on mismatch; only the unscoped internal callers keep the sweep. *This is the recommendation* — it preserves the anti-resurrection behaviour for the paths that legitimately need it (bridge teardown) while making the public route mean what it says.
- **C. Declare the id globally unique and remove the lie.** Drop `workspace_id` from all four signatures (`session_routes.py:299`, `session_manager.py:373`, `session/store.py:765`, `:83`) and document that a session id names exactly one session vault-wide. Cheapest, honest, and wrong if workspace-scoped ids ever stop being globally unique — but note that the metadata filenames already assume global uniqueness (`:171-173`), so this is arguably just documenting current reality.

Recommendation: **B**, then optionally **C** for the WS path so the parameter stops being passed where it cannot be honoured. Whichever is chosen, `bridge.py:2102-2104` must be brought along or the socket keeps the global behaviour.

## MUST NOT TOUCH

- `_find_session_path` / `_resolve_session_path` caching and the 5-second TTL (`session/store.py:175-196`, `:155-169`) — several call sites depend on the cache being invalidated exactly at `save` (`:356-358`) and `delete` (`:779-780`).
- The "prevents session resurrection" rationale itself (`session/store.py:766-773`). Whatever else changes, the sweep that removes a session's copies in *other* workspaces may still be needed by the teardown callers; only the client-reachable route's semantics are in question here.
- The metadata filename convention (`:171-173`) and the `_meta_` unlink (`:809-813`).
- `session_routes.py:308` / `session_manager.py:383-385` forwarding — they are correct today; the defect is downstream.
