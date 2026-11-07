# Ticket: bug/create-session-drops-workspace-id

**Filed:** 2026-10-21, from the session-persistence pass at `9d5ed869` (found while checking the *write* side of the session/workspace pivot; the read side is `ticket-bug-load-session-workspace-pivot.md`, the store's delete sweep is `ticket-bug-session-delete-ignores-workspace-scope.md`). Line numbers are measured in this tree, whose `server.py` is 9 lines longer than the numbering used in the pass brief — `server.py` citations below are mine, not the brief's.
**Severity:** MEDIUM — `SessionManager.create_session` accepts `workspace_id`, never uses it for the write, and persists every new session into the legacy sessions directory with `workspace_id: None`. Callers paper over it by re-saving after the fact; one caller does not, and one branch duplicates the record instead of moving it.
**Status:** open; verified by read at `9d5ed869`, not exercised at runtime. NOT blocking, NOT to be fixed in this wave.

## Finding

The parameter is accepted and dropped. `web_ui/backend/session_manager.py:71-79`:

```python
def create_session(
    self,
    mode: str = "custom",
    workspace_path: Optional[str] = None,
    audit_source: str = 'user',
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
```

and then `:89` builds a bare `Session()` (whose `workspace_id` defaults to `None` — `session/models.py:159`), `:136-138` persists it with that default:

```python
self._session_store.save_session(
    new_session, workspace_id=new_session.workspace_id
)
```

`new_session.workspace_id` is always `None` here, so this call always takes `store.py`'s legacy branch. The `workspace_id` argument reaches exactly one thing, `_seed_permissions_sidecar` (`:147-152`). The code says so itself (`:141-146`):

```python
# above is written to the legacy sessions dir (workspace_id is layered
# onto the session only later by the caller), so a disk-mode permission
```

Three call-site classes follow from that split:

1. **Manual repair, in 4 places.** `web_ui/backend/session_routes.py:126` loads unscoped, `:140` sets `session.workspace_id = entry.id`, `:158-160` re-saves so that "save_session moves the file to the workspace-scoped location". Same pattern at `server.py:1546-1563`, `server.py:1600-1612`, and `server.py:2841-2860` (`set_project`). Every one of them depends on `session/models.py:185-192`, where `workspace_id` is immutable once set (a second assignment raises `AttributeError`) — so the repair only ever works on a freshly created `None`, which is precisely the state this bug produces.
2. **No repair.** `server.py:2693` calls `bridge.create_session(mode=mode, workspace_id=workspace_id)` and `:2695-2697` sets `new_session.workspace_id` in memory; there is no `save_session` before the `session_loaded` payload at `:2727`. The record on disk therefore stays in the legacy directory with `"workspace_id": null` until the first ordinary save, at which point `store.py:316-317` back-fills and moves it. Any reader that looks in the workspace-scoped directory before that first save finds nothing.
3. **Masked by a scoped re-load.** `web_ui/backend/bridge.py:1845-1888` validates the id (`:1857-1858`), hands it to `SessionManager` (`:1859-1861`, dropped), then re-loads with `_scope = workspace_id or self._workspace_id` (`:1866-1867`) and refuses a record belonging to a different workspace (`:1868-1877`). The re-load succeeds because an unscoped lookup falls back to the legacy candidate set — so the drop is invisible from the outside.

There is also a duplicate-copy hazard in the reuse branch, `server.py:1594`: `save_session(existing_session, workspace_id=workspace_id)` where `existing_session.workspace_id` is already the *old* id. `save_session` resolves the previous location with `_find_session_path(id, workspace_id=<new>)` (`session/store.py:209-217`), which does not look in the old workspace's directory, so `old_path` is `None` (`:381-391`), the move logic is skipped, and a **second** copy is written into the new workspace's directory while the original stays behind. Worse, the copy's payload carries the old id, because `to_persistable_dict` writes `'workspace_id': self.workspace_id` (`session/models.py:326`) — so the new workspace's directory now contains a file that claims to belong to the old one.

## Why it matters

The pivot to workspace-scoped sessions is half-wired: the store supports it (`session/store.py:158-168`, `:304-317`, `:366-391`), the manager's signature promises it, and the callers implement it by hand afterwards. Each of those repairs is a place where a future caller forgets, and one already has. The failure is quiet — the session loads, the UI shows it — and surfaces later as a session that cannot be listed (`store.py:637-640` only reads the scoped dir when told to) or as a duplicate that lists twice.

The duplicate-copy branch is the sharper end: it writes a record whose payload disagrees with its location, which nothing in the store's invariants expects.

## Disposition options

- **A. Fix the write, keep the repairs.** Pass the argument through: build the `Session` and set `workspace_id` before the persist, or `save_session(new_session, workspace_id=workspace_id)`. Smallest change, makes the four manual repairs no-ops (they re-save the same id to the same place). *This is the recommendation.*
- **B. Remove the parameter** and document that scoping happens at the caller. Honest, but it makes call class 2 above a permanent bug and leaves three of the four repairs load-bearing.
- **C. A plus the duplicate-copy fix** — additionally teach `save_session`'s move logic to look in the *session's own* previous workspace (`session/store.py:209-217`) so the payload's id and the target directory agree. Larger, and the move logic is shared with paths that are already correct.

Recommendation: **A** now, and **C**'s second half only if the reuse branch at `server.py:1594` is reached in practice — which needs a runtime check this pass did not do. Track the surface change as the write-side companion to `ticket-bug-load-session-workspace-pivot.md`; do not fold it into `ticket-bug-session-delete-ignores-workspace-scope.md`, which is about the store's *unscoped deletes*, not about who writes where.

## MUST NOT TOUCH

- `session/models.py:185-192` — `workspace_id`'s write-once guard. Every manual repair site depends on it; relaxing it turns the repairs into silent re-homing.
- `session/store.py:316-317` — the save-side back-fill. It is what rescues the `server.py:2693` path on first save.
- `web_ui/backend/session_routes.py:126,140,158-160` — the reference implementation of the repair; keep it working even after the write is fixed.
- `web_ui/backend/bridge.py:1868-1877` — the foreign-workspace refusal. It is the only guard standing between a mis-scoped record and a cross-workspace load.
- `agent/presenter/session_lifecycle.py:110-124`, `:151-167` — the presenter already resolves and stamps a workspace id on create; it must not be "simplified" to match the manager's dropped parameter.
