# Ticket: bug/load-session-workspace-pivot

**Filed:** 2026-10-21, from the session-load pass at `9d5ed869`, while tracing what `workspace_id` actually scopes on the read side.
**Severity:** HIGH — `load_session` is the one command that redefines a connection's workspace, its only input is a session id, and the "workspace-scoped" store lookup silently degrades to a vault-wide scan that will find the id in someone else's workspace.
**Status:** open; verified by read at `9d5ed869`, not exercised at runtime. NOT blocking, NOT to be fixed in this wave.

## Finding

The WS handler reads exactly one field (`web_ui/backend/server.py:2102-2106`):

```python
elif command == "load_session":
    session_id = msg.get("session_id", "")
    if not session_id:
        await ws.send_json({"type": "status_message", "text": "⚠ session_id is required."})
        continue
```

No `workspace_id`, no ownership check. The warm-bridge cache it consults is keyed on the same single field (`server.py:1029`: `_session_bridges: Dict[str, Any] = {}  # session_id → WebAgentBridge (warm cache)`; used at `:2113`: `existing = _session_bridges.get(session_id)`), so a cached bridge belonging to another workspace is handed to this socket (`:2119-2127`) without the sid being qualified.

The bridge then passes *its own* workspace — which is the right instinct — into a lookup that does not enforce it (`web_ui/backend/bridge.py:1893`):

```python
session = self._session_store.load_session(session_id, workspace_id=self._workspace_id)
```

and the store treats that argument as a *preference*, not a constraint (`session/store.py:367-384`):

```python
def load_session(self, session_id: str, workspace_id: Optional[str] = None) -> Optional[Session]:
    """... If workspace_id is provided, the workspace-scoped path is checked first,
    falling back to the legacy sessions_dir. ..."""
    if workspace_id:
        ws_path = self._resolve_session_path(session_id, workspace_id)
        if ws_path.exists():
            path = ws_path
        else:
            # Fall back to finding by scanning (handles friendly filenames)
            path = self._find_session_path(session_id)
    else:
        path = self._find_session_path(session_id)
```

`_find_session_path` is the vault-wide scan (`session/store.py:188-195`):

```python
# Collect all candidate directories
candidates = [self.sessions_dir]
workspaces_root = self._base_dir / "workspaces"
if workspaces_root.exists():
    for ws_dir in workspaces_root.iterdir():
        ws_sessions = ws_dir / "sessions"
        if ws_sessions.is_dir():
            candidates.append(ws_sessions)
```

So the mismatch case — id not in *my* workspace — does not fail closed; it escalates to every workspace and finds the session there. The fallback is justified in the comment as "handles friendly filenames", i.e. it exists so a filename that does not match the id can still be found in *the same* directory; scanning sibling workspaces is a side effect of reusing `_find_session_path`, not a requirement.

Then the pivot happens (`web_ui/backend/bridge.py:1899-1901`):

```python
self._session = session
self._history_version = session.conversation_version
self._workspace_id = session.workspace_id
```

The bridge adopts the loaded session's workspace. This is not a one-off: `_workspace_id` is assigned from a loaded session in three places — `:1868` (`create_session`), `:1901` (`load_session`), `:2003` (resolve-from-`workspace_path` fallback) — so the bridge's workspace identity is a mutable value derived from whatever was last loaded, not a property of the connection.

Everything downstream then runs against the *other* workspace, and the code says so explicitly:

- `bridge.py:1904-1919` — backfills `_workspace_path` from the workspace registry for `self._workspace_id` (the other workspace's root).
- `bridge.py:1925` — `self._load_worker_contexts()`, i.e. the other workspace's persisted worker contexts become this connection's view.
- `bridge.py:1949-1968` — reads the session's raw grants (`read_grants_or_deny_all(session.session_id, self._workspace_id)` at `:1951-1953`) and re-caps them through **that** workspace's ceiling (`_load_workspace_permission_ceiling(self._workspace_id)` at `:1958`, `cap(...)` at `:1962-1964`). The permission ceiling in force after a load is the loaded session's workspace ceiling.

The same "computed then dropped" shape appears one method earlier, which is why this is filed as a class rather than a branch: `create_session` validates the caller's requested workspace (`bridge.py:1857-1858` `if workspace_id is not None: _validate_workspace_id(workspace_id)`) and then loads the new session with `self._workspace_id` instead of the validated argument (`:1863`).

## Why it matters

Workspace isolation in this codebase is *path-shaped*: `~/.thoughtmachine/workspaces/<ws_id>/sessions/`, with the store's other two read paths (`list_sessions` `:496-506`) treating `workspace_id` as a hard filter. `load_session` is the path that a client drives, and it treats the same argument as a hint that is abandoned on miss. The result is that the single command which changes what a connection *is* can be pointed at another workspace by supplying nothing but an id — and the connection then inherits that workspace's root path, worker contexts, and permission ceiling, and shares a cached bridge object with whoever else loaded the same id.

Note the ceiling re-cap at `:1954-1968` currently acts as an accidental damper: a pivoted connection gets the *victim's* ceiling applied, so it does not gain the victim's grants beyond that ceiling. That is a compensating control, not a scope check — it limits the blast radius of a pivot while leaving the pivot itself unchallenged, and it is the reason this is filed HIGH rather than as an immediate privilege-escalation path.

## Disposition options

- **A. Make the scoped branch fail closed.** When `workspace_id` is given and the session is not in that workspace, return `None` (`session/store.py:375-382`). Clean, but it strands legacy sessions written to the flat `sessions_dir` before workspace scoping, and it changes behaviour for every internal caller of `load_session` that relies on today's leniency.
- **B. Narrow the fallback to the legacy directory only.** Keep the "friendly filenames" behaviour (`_find_session_path` semantics) but search `[self.sessions_dir]` — never sibling workspaces — when a `workspace_id` was supplied. *This is the recommendation*: it preserves the documented legacy reason for the fallback, closes the cross-workspace case, and leaves the unscoped callers (`workspace_id=None`) exactly as they are.
- **C. Leave the lookup, fix the identity.** Accept that a session may be loaded from anywhere and make the *connection's* workspace explicit and sticky: key the bridge cache by `(workspace_id, session_id)` (`server.py:1029`, `:2113`, `:2391`), and refuse a load whose `session.workspace_id` differs from the connection's workspace. Larger, but it is the version that survives someone reusing `load_session` for a "switch workspace by loading a session" feature.

Recommendation: **B** now (one branch, no new policy), with **C**'s cache keying as the follow-up — a cache keyed on a value that is not unique per tenant is the part most likely to bite in production, because it hands one connection another's live bridge object.

## MUST NOT TOUCH

- The ceiling re-cap block (`bridge.py:1940-1968`) and its deliberately-uncapped persistence snapshot (`:1935-1939`, `:1970-1975`). It is a working compensating control; the comments explain that capping the persisted dump would permanently collapse stored grants.
- `store.load_session`'s unscoped branch (`:383-384`) and `_resolve_session_path` (`:155-169`) — internal callers with no workspace (legacy sessions) must keep working.
- The `_find_session_path` TTL cache (`:175-196`) — invalidated on save (`:356-358`) and delete (`:779-780`); changing its candidate set changes its invalidation assumptions.
- The WS handler's "reuse cached bridge" path (`server.py:2114-2127`) and its long comment about why `bridge.load_session()` must not be re-run for a cached bridge — that logic is load-bearing for multi-tab behaviour.
