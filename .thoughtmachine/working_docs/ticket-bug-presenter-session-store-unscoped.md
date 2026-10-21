# Ticket: bug/presenter-session-store-unscoped

**Filed:** 2026-10-21, from the session-persistence pass at `9d5ed869` (found while tracing which store calls carry a `workspace_id` and which do not; the presenter builds its store with no workspace and never passes one on read, list or delete).
**Severity:** LOW — the presenter's *writes* are workspace-scoped (via the session fallback), so nothing is misfiled today. The unscoped calls are reads, a list, and a delete; the delete is the one with teeth (it sweeps every workspace), but the presenter is not the code path the web UI uses for session deletion.
**Status:** open; verified by read at `9d5ed869`, not exercised at runtime. NOT blocking, NOT to be fixed in this wave.

## Finding

The store is constructed without a workspace, `agent/presenter/session_lifecycle.py:29`:

```python
self.session_store = FileSystemSessionStore()
```

The presenter *does* know its workspace: `:110-124` (`start`) and `:151-167` (`new_session`) call `resolve_workspace_id(ws_path)` (`thoughtmachine/workspace_capabilities.py:221-251`, which matches the registry `root` field) and pass `workspace_id=` into the `Session(...)` they build. And `session/store.py:316-317` back-fills the identifier on save:

```python
# Resolve workspace_id from session if not explicitly passed
if workspace_id is None and session.workspace_id:
    workspace_id = session.workspace_id
```

So the save path is **not** broken: whenever a workspace path is configured, a presenter session is written under `~/.thoughtmachine/workspaces/<id>/sessions/`. The bug is everything that has no `Session` object to fall back on:

- `session_lifecycle.py:352` `load_session`, `:448` `list_sessions`, `:462` `delete_session`; `:480` `load` and `:485` `save` in the rename path.
- `agent/presenter/agent_presenter.py:240` `save` (delegates), `:253` `load`, `:263` `load_session_by_id`, `:267` `session_store.get_current_session_id()`, `:284` `list`, `:288` `delete`.

Three consequences, all asymmetric:

1. **Unscoped load reads across workspaces.** `_find_session_path` (`session/store.py:218-225`) accepts the legacy dir *plus every workspace dir* when no `workspace_id` is given.
2. **Unscoped list sees only the legacy dir** — `session/store.py:637-640`: `if workspace_id: target_dir = .../workspaces/<id>/sessions` / `else: target_dir = self.sessions_dir`. So the presenter's own workspace-scoped saves are invisible to its own `list_sessions()`. This is the sharpest observable symptom: save a session through the presenter, list it, and it is not there.
3. **Unscoped delete sweeps every workspace.** `session/store.py:846-852` builds the candidate list from the legacy dir plus every `workspaces/*/sessions`, and `:857-876` unlinks *every* `*.json` whose `session_id` matches in *every* candidate, plus each `_meta_<id>.json`. Not "first match" — all of them.

`get_current_session_id` (`session/store.py:961-993`) reads the global `~/.thoughtmachine/state/.current_session`, unscoped by design — left alone here, but it is why `agent_presenter.py:267` cannot be fixed by threading a parameter.

The web bridge has the same shape and at least one instance of it: `web_ui/backend/bridge.py:1606-1612` `list_sessions()` takes no `workspace_id` and calls the store unscoped even though the bridge already holds `self._workspace_id` (`:326`, property `:987-989`); by contrast `delete_session` (`:2153-2159`) forwards a workspace id and `rename_session` (`:2163`) passes `self._workspace_id`. Scoped callers exist and are the norm elsewhere — `web_ui/backend/session_manager.py:136,191,294,366,383,411,426,437`.

The injection seam already exists: `agent/presenter/agent_presenter.py:379-386` exposes a `session_store` property with a setter ("Allow GUI to inject a session store"), so a workspace-bound store can be supplied without touching the presenter's construction site.

## Why it matters

The list/delete divergence is the kind that reads as a UI bug rather than a scoping bug: sessions that exist are not listed, and a delete issued from the unscoped path destroys every workspace's copy of a matching id. Both are silent — the list returns `[]`, the delete reports success.

## Disposition options

- **A. Thread `workspace_id` through the presenter API** — `load_session`, `list_sessions`, `delete_session`, `load`, `save` gain a parameter; callers supply it from `resolve_workspace_id`, as the write path already does. *This is the recommendation*, because it is the same shape the store already supports and the bridge already uses for `delete_session`/`rename_session`.
- **B. Bind the store to a workspace at construction.** Fixes `session_lifecycle.py:29` once, but the presenter serves several workspaces over its lifetime, so this trades an unscoped call for a stale-scoped one — and `agent_presenter.py:379-386` shows the store is meant to be replaceable.
- **C. Fail closed.** Make the unscoped store branches raise for `load`/`list`/`delete` instead of falling back to a sweep. Cheapest way to stop the destructive case, but it breaks every remaining unscoped caller at once (`bridge.py:1606-1612` included) in the same change.

Recommendation: **A** for the presenter and `bridge.py:1606-1612`, with **C**'s delete-side guard (`session/store.py:846-852` raising instead of sweeping) deferred to `ticket-bug-session-delete-ignores-workspace-scope.md`, which owns the store's behaviour. Cross-reference: `ticket-bug-create-session-drops-workspace-id.md` is the write-side defect in the same area — that one files a session in the legacy dir, which is the state this ticket then cannot list.

## MUST NOT TOUCH

- `session/store.py:316-317` — the save-side fallback. It is what keeps the presenter's writes correctly scoped; removing it turns a LOW ticket into a data-placement bug.
- `session/store.py:961-993` — `get_current_session_id`'s global state file, read-only legacy fallback, unscoped by design.
- `agent/presenter/agent_presenter.py:379-386` — the injectable `session_store` property. It is the seam a fix uses.
- `web_ui/backend/bridge.py:2153-2159`, `:2163` — already-correct scoped `delete_session`/`rename_session`; do not "simplify" them to match the unscoped `list_sessions`.
