# Ticket: bug/bridge-not-evicted-on-new-session

**Filed:** 2026-10-23, Gen 49, while fixing the cross-session unsubscribe in
`fix/worker-panel-cross-session-unsubscribe`. Line numbers read from the working
tree of `ThoughtMachine-dev`, dev `892d2780`. **Recorded, not fixed here.**
**Severity:** MEDIUM — a stopped `WebAgentBridge` stays reachable through the
warm cache, so a reconnect (or a second tab) can be handed a bridge whose
controller has already been stopped: the tab looks connected, accepts input and
silently answers nothing until it is reloaded. Not a data-loss or a security
defect (the workspace guard still applies), but it is another "success-shaped
failure" on the same surface the worker-panel tickets live on.
**Separate small fix** — deliberately *not* bundled into the cross-session
unsubscribe change; different file, different blast radius.

## The defect

`new_session` stops the old bridge and caches the new one, but never evicts the
old session id from the warm cache:

- `web_ui/backend/server.py:3066` — `bridge.stop()` (the "Stop any existing
  bridge" step of the `new_session` command handler; the workspace-scope refusal
  immediately above at `:3047-3064` runs first, correctly, before any side
  effect).
- `web_ui/backend/server.py:3179` — `_session_bridges[session_id] = bridge`
  caches the **new** bridge under the **new** id. Nothing ever pops the **old**
  id.

So `_session_bridges[old_session_id]` keeps pointing at the bridge that was just
stopped. The cache is consulted by `_cached_bridge_for` (`server.py:1075`,
`cached = _session_bridges.get(session_id)` at `:1086`) and by the reconnect /
`load_session` path at `:2387`. Its only rejection rule for a stale entry is:

- `server.py:1094-1099` — `if cached.session is None: ... return None` (the
  "refused or failed load left an empty entry" guard).

`WebAgentBridge.stop()` (`web_ui/backend/bridge.py:1682-1694`) clears
`_pending_config`, calls `unregister()`, `_unsubscribe_security_events()`,
`_unsubscribe_worker_events()`, and stops the controller — it does **not** clear
`_session`, `_session_id` or `_workspace_id`. A stopped bridge therefore still
passes the `session is None` guard *and* the workspace-scoping check two lines
below (`:1100-1106`), and `_cached_bridge_for(old_session_id)` returns it as a
perfectly good warm cache hit.

## Symptom

User starts a session, then sends `new_session` in the same tab (or switches
sessions and comes back to the old one). Any later reconnect that asks for the
**old** session id — a browser reload of a stale tab, a second tab restoring the
open-session list, a client that re-sends `load_session` with the previous id —
receives the stopped bridge instead of a freshly constructed one. The socket
handshake succeeds, the session id and history look right (the session object is
still attached), and the agent never answers, because `_controller` is stopped.
The only trace is that nothing happens.

## Proposed fix

In the `new_session` path, immediately after `bridge.stop()` at
`server.py:3066`, evict the old id from the cache — by value, the same way the
close path already does it (`server.py:2994-3003`):

```python
for _old_sid, _old_bridge in list(_session_bridges.items()):
    if _old_bridge is bridge:
        del _session_bridges[_old_sid]
```

Doing it by **value** rather than by `bridge.session.session_id` is what the
existing close path chose (`:2994-2996`, with the comment "Remove the bridge
from `_session_bridges` by value (since the key …)"), and it also covers a
bridge that was cached under an id other than its own session's, plus a bridge
whose `_session` is already `None`.

Second, independent hardening (either alone closes the reported symptom): make
`_cached_bridge_for` reject a stopped bridge explicitly, so no *other* path can
reintroduce the same hand-out. There is no `is_stopped` flag today; the minimal
honest check is the controller/stop-event pair that `stop()` already touches —
if a "stopped" predicate is added it should live on the bridge
(`WebAgentBridge.is_stopped`) and be set in `stop()` / cleared in `start()`, not
inferred by the caller.

## Why not fixed here

The branch `fix/worker-panel-cross-session-unsubscribe` is a two-line guard in
`web_ui/backend/bridge.py` closing a cross-session *unsubscribe* (a foreign
`WORKER_COMPLETED` tearing down this bridge's per-worker handlers). Touching
`server.py`'s session cache would widen that branch into session-lifecycle
territory, which has its own guards (`:3047-3064`) and its own test surface, and
would make the RED/GREEN evidence for the unsubscribe fix harder to read. It
also wants its own test: construct a bridge, cache it, stop it, assert
`_cached_bridge_for` no longer returns it.

## Bearing

- Related, not the same: `ticket-bug-load-session-workspace-pivot.md` (the
  workspace-scoping rule `_cached_bridge_for` enforces) and
  `ticket-bug-create-session-drops-workspace-id.md`.
- The eviction shape already exists one screen away in the close path
  (`server.py:2994-3003`) — reuse it rather than inventing a second one.
